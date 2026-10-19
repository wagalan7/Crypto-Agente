from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import database as db

_TZ = ZoneInfo("America/Sao_Paulo")


def _now() -> datetime:
    """Agora em horário de Brasília, naive. Função separada de propósito: é o
    único ponto de leitura do relógio no cálculo de horários, o que permite
    fixar o tempo em teste sem mexer no fuso do processo."""
    return datetime.now(_TZ).replace(tzinfo=None)

_DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4]   # Seg–Sex
_DEFAULT_BLOCKED_HOURS = [12, 13, 14]      # Almoço


def _working_days(tenant: dict) -> list[int]:
    raw = tenant.get("working_days", "") or ""
    try:
        return [int(d) for d in raw.split(",") if d.strip().isdigit()]
    except Exception:
        return _DEFAULT_WORKING_DAYS


def _blocked_hours(tenant: dict) -> list[int]:
    raw = tenant.get("blocked_hours", "") or ""
    try:
        return [int(h) for h in raw.split(",") if h.strip().isdigit()]
    except Exception:
        return _DEFAULT_BLOCKED_HOURS


def _blocked_dates(tenant: dict) -> set[str]:
    """Datas bloqueadas (feriados/férias) — set de YYYY-MM-DD."""
    raw = tenant.get("blocked_dates", "") or ""
    return {d.strip() for d in raw.split(",") if d.strip()}


def _blocked_hours_by_day(tenant: dict) -> dict[int, set[int]]:
    """Horas bloqueadas em dias específicos da semana.
    Retorna {weekday(0=Seg): {hora, …}}. Se o campo estiver vazio, retorna {}."""
    raw = (tenant.get("blocked_hours_by_day") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out: dict[int, set[int]] = {}
        for k, v in data.items():
            try:
                wd = int(k)
            except Exception:
                continue
            if 0 <= wd <= 6 and isinstance(v, list):
                out[wd] = {int(h) for h in v if isinstance(h, (int, str)) and str(h).isdigit()}
        return out
    except Exception:
        return {}


PERIODOS = {"manha": (6, 12), "tarde": (12, 18), "noite": (18, 24)}


def periodo_da_hora(hora: int) -> str | None:
    for nome, (ini, fim) in PERIODOS.items():
        if ini <= hora < fim:
            return nome
    return None


def find_slots(tenant: dict, *, days_ahead: int = 14, on_date=None,
               period: str | None = None, per_day: int | None = None,
               max_total: int | None = None,
               exclude_id: int | None = None) -> list[datetime]:
    """Horários livres do consultório, com horizonte (dias) e limite de
    quantidade **separados**.

    Por que isso existe: `get_available_slots` corta a varredura no N-ésimo
    horário livre. Com a agenda dos próximos dias cheia, esse corte consumia
    toda a cota antes de alcançar o dia que o paciente pediu, e o dia pedido
    simplesmente desaparecia das opções — o agente então marcava outro dia
    (ver docs/planejamento/EVIDENCIAS/01.md §3).

    Aqui a varredura cobre o horizonte inteiro e os limites são aplicados por
    dia (`per_day`) e por amostragem equilibrada (`max_total`), nunca cortando
    os dias do fim.

    - `on_date`: restringe a uma data. Se ela estiver além de `days_ahead`, o
      horizonte é esticado para alcançá-la — um pedido explícito não se perde.
    - `period`: 'manha' | 'tarde' | 'noite'.
    - `per_day`: no máximo N horários por dia de calendário.
    - `max_total`: amostra equilibrada entre os dias (round-robin), para o
      contexto do modelo ficar pequeno sem perder dias.
    - `exclude_id`: ignora a própria consulta (remarcação).
    """
    tenant_id = tenant["id"]
    start_h = tenant["working_hours_start"]
    end_h = tenant["working_hours_end"]
    duration = tenant["session_minutes"]
    working_days = _working_days(tenant)
    blocked_hours = _blocked_hours(tenant)
    blocked_dates = _blocked_dates(tenant)
    blocked_by_day = _blocked_hours_by_day(tenant)

    now = _now()  # naive, horário de Brasília
    if on_date is not None:
        # Garante que a data pedida caiba no horizonte varrido.
        days_ahead = max(days_ahead, (on_date - now.date()).days + 1)
    if days_ahead <= 0:
        return []

    check = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_date = now + timedelta(days=days_ahead)

    booked = db.get_appointments_in_range(tenant_id, now.isoformat(), end_date.isoformat())
    booked_dts = []
    for r in booked:
        # Horário fica LIVRE quando a consulta foi cancelada ou quando a sessão
        # não vai acontecer, com ou sem aviso prévio — nesses casos a vaga deve
        # voltar a ser ofertada para outro paciente. Mesma regra de has_conflict:
        # cobrança é decidida em outro lugar e não entra aqui.
        if r.get("cancelled"):
            continue
        if (r.get("attendance") or "pending") in db.ATTENDANCE_LIBERA_HORARIO:
            continue
        if exclude_id is not None and r.get("id") == exclude_id:
            continue
        try:
            booked_dts.append(datetime.fromisoformat(r["scheduled_at"]))
        except Exception:
            pass

    def _conflicts(slot: datetime) -> bool:
        """Slot conflita se está a menos de `duration` minutos de qualquer consulta existente."""
        for b in booked_dts:
            if abs((slot - b).total_seconds()) < duration * 60:
                return True
        return False

    faixa = PERIODOS.get(period or "", None)
    por_dia: dict = {}
    while check <= end_date:
        dia = check.date()
        if (on_date is None or dia == on_date) and (
            check.weekday() in working_days
            and start_h <= check.hour < end_h
            and check.hour not in blocked_hours
            and check.hour not in blocked_by_day.get(check.weekday(), set())
            and check.strftime("%Y-%m-%d") not in blocked_dates
            and (faixa is None or faixa[0] <= check.hour < faixa[1])
            and not _conflicts(check)
        ):
            do_dia = por_dia.setdefault(dia, [])
            if per_day is None or len(do_dia) < per_day:
                do_dia.append(check)
        check += timedelta(minutes=duration)

    dias = sorted(por_dia)
    if max_total is None:
        return [s for d in dias for s in por_dia[d]]

    # Amostra equilibrada: pega o 1º de cada dia, depois o 2º de cada dia, etc.
    # Assim 24 vagas cobrem ~24 dias em vez de dois dias cheios.
    escolhidos: list[datetime] = []
    profundidade = 0
    while len(escolhidos) < max_total and any(
        len(por_dia[d]) > profundidade for d in dias
    ):
        for d in dias:
            if len(escolhidos) >= max_total:
                break
            if len(por_dia[d]) > profundidade:
                escolhidos.append(por_dia[d][profundidade])
        profundidade += 1
    return sorted(escolhidos)


def get_available_slots(tenant: dict, days_ahead: int = 7, limit: int = 10) -> list[datetime]:
    """Primeiros `limit` horários livres em ordem cronológica.

    Mantida com o comportamento original (corte cronológico) porque o painel e
    os indicadores dependem dele: "próximos horários livres" e "quantas vagas
    existem". O agente **não** usa mais esta função para montar as opções do
    paciente — ver `find_slots`.
    """
    todos = find_slots(tenant, days_ahead=days_ahead)
    return todos[:limit]


def is_slot_bookable(tenant: dict, dt: datetime, exclude_id: int | None = None) -> tuple[bool, str]:
    """Valida se `dt` pode receber uma sessão: precisa ser futuro, em dia
    atendido, dentro do expediente, fora de horários/datas bloqueados e sem
    conflito com outra consulta. Retorna (ok, motivo)."""
    now = _now()
    if dt <= now:
        return False, "passado"
    if dt.weekday() not in _working_days(tenant):
        return False, "dia_nao_atendido"
    start_h = tenant["working_hours_start"]
    end_h = tenant["working_hours_end"]
    if not (start_h <= dt.hour < end_h):
        return False, "fora_expediente"
    if dt.hour in _blocked_hours(tenant):
        return False, "horario_bloqueado"
    if dt.hour in _blocked_hours_by_day(tenant).get(dt.weekday(), set()):
        return False, "horario_bloqueado"
    if dt.strftime("%Y-%m-%d") in _blocked_dates(tenant):
        return False, "data_bloqueada"
    duration = tenant.get("session_minutes", 50)
    if db.has_conflict(tenant["id"], dt, duration, exclude_id=exclude_id):
        return False, "ocupado"
    return True, "ok"


def suggest_slots_near(tenant: dict, target_dt: datetime, n: int = 3) -> list[datetime]:
    """Sugere até `n` horários livres próximos da data pedida — prioriza o
    MESMO dia (mais perto do horário pedido) e depois os dias seguintes."""
    slots = get_available_slots(tenant, days_ahead=14, limit=80)
    same_day = sorted(
        [s for s in slots if s.date() == target_dt.date()],
        key=lambda s: abs((s - target_dt).total_seconds()),
    )
    rest = [s for s in slots if s.date() != target_dt.date()]
    return (same_day + rest)[:n]


def format_slots(slots: list[datetime]) -> list[str]:
    day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    return [
        f"{day_names[s.weekday()]}, {s.strftime('%d/%m')} às {s.strftime('%H:%M')}"
        for s in slots
    ]


def get_next_appointment(tenant_id: int, phone: str) -> dict | None:
    appts = db.get_appointments_by_phone(tenant_id, phone)
    return appts[0] if appts else None


def format_appointment(appt: dict) -> str:
    dt = datetime.fromisoformat(appt["scheduled_at"])
    day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    return f"{day_names[dt.weekday()]}, {dt.strftime('%d/%m')} às {dt.strftime('%H:%M')}"
