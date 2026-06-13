from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import database as db

_TZ = ZoneInfo("America/Sao_Paulo")

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


def get_available_slots(tenant: dict, days_ahead: int = 7, limit: int = 10) -> list[datetime]:
    tenant_id = tenant["id"]
    start_h = tenant["working_hours_start"]
    end_h = tenant["working_hours_end"]
    duration = tenant["session_minutes"]
    working_days = _working_days(tenant)
    blocked_hours = _blocked_hours(tenant)
    blocked_dates = _blocked_dates(tenant)
    blocked_by_day = _blocked_hours_by_day(tenant)

    now = datetime.now(_TZ).replace(tzinfo=None)  # naive, horário de Brasília
    check = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_date = now + timedelta(days=days_ahead)

    booked = db.get_appointments_in_range(tenant_id, now.isoformat(), end_date.isoformat())
    booked_dts = []
    for r in booked:
        # Horário fica LIVRE quando a consulta foi cancelada ou o paciente
        # avisou que não vem (missed_with_notice) — nesses casos a vaga deve
        # voltar a ser ofertada para outro paciente.
        if r.get("cancelled"):
            continue
        if (r.get("attendance") or "pending") == "missed_with_notice":
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

    slots = []
    while check <= end_date and len(slots) < limit:
        if (
            check.weekday() in working_days
            and start_h <= check.hour < end_h
            and check.hour not in blocked_hours
            and check.hour not in blocked_by_day.get(check.weekday(), set())
            and check.strftime("%Y-%m-%d") not in blocked_dates
            and not _conflicts(check)
        ):
            slots.append(check)
        check += timedelta(minutes=duration)

    return slots


def is_slot_bookable(tenant: dict, dt: datetime, exclude_id: int | None = None) -> tuple[bool, str]:
    """Valida se `dt` pode receber uma sessão: precisa ser futuro, em dia
    atendido, dentro do expediente, fora de horários/datas bloqueados e sem
    conflito com outra consulta. Retorna (ok, motivo)."""
    now = datetime.now(_TZ).replace(tzinfo=None)
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
