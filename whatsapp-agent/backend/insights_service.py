"""Motor de Insights administrativos — Fase 1 (só leitura, determinístico).

Transforma o dado que o sistema JÁ tem (agenda, contratos, cobrança) num
resumo inteligente de GESTÃO do consultório. Princípio inegociável: a IA/analytics
olha SÓ informação administrativa (status, datas, valores) — NUNCA conteúdo
clínico. Este módulo não lê `conversations.content` nem o campo `notes`; só toca
colunas de status/horário/preço.

Características:
- READ-ONLY: nenhuma escrita no banco, nenhum envio de WhatsApp. Deploy sem risco.
- Fail-safe: cada indicador é isolado; se um falhar, os outros seguem. Nunca
  levanta exceção para o endpoint (retorna o que conseguiu montar).
- Determinístico: as frases são montadas por regra (sem IA por enquanto). A
  camada narrativa com IA é uma fase futura, e mesmo lá a IA só veria os NÚMEROS
  agregados daqui — jamais paciente/conteúdo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import database as db

logger = logging.getLogger("insights")

_TZ = ZoneInfo("America/Sao_Paulo")

# Limiares (ajustáveis) das regras administrativas.
_OCCUPANCY_LOW = 40        # abaixo disso: agenda "vazia" (atenção)
_OCCUPANCY_HIGH = 90       # acima disso: agenda "muito cheia" (atenção)
_NO_RETURN_RECENT_DAYS = 45   # teve sessão nos últimos N dias…
_NO_RETURN_ALERT_DAYS = 21    # …e sem próximo agendamento há ≥ N dias → destaque


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _month_bounds(ref: datetime) -> tuple[str, str]:
    y, m = ref.year, ref.month
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return start.isoformat(), end.isoformat()


def _day_bounds(ref: datetime) -> tuple[str, str]:
    start = datetime(ref.year, ref.month, ref.day)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _prev_month_bounds(ref: datetime) -> tuple[str, str]:
    first = datetime(ref.year, ref.month, 1)
    prev_end = first
    prev_last_day = first - timedelta(days=1)
    prev_start = datetime(prev_last_day.year, prev_last_day.month, 1)
    return prev_start.isoformat(), prev_end.isoformat()


def _greeting(tenant: dict, now: datetime) -> str:
    h = now.hour
    if h < 12:
        saud = "Bom dia"
    elif h < 18:
        saud = "Boa tarde"
    else:
        saud = "Boa noite"
    nome = (tenant.get("psychologist_name") or tenant.get("name") or "").strip()
    first = nome.split()[0] if nome else ""
    return f"{saud}, {first}!" if first else f"{saud}!"


def _brl(v: float) -> str:
    # R$ 1.234,56 (formato pt-BR simples)
    s = f"{v:,.2f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {s}"


def _active(appts: list[dict]) -> list[dict]:
    """Só agendamentos que 'contam' (não cancelados, não avisou-que-não-vem,
    fora dos placeholders de novo paciente 2099)."""
    out = []
    for a in appts:
        if a.get("cancelled"):
            continue
        if (a.get("attendance") or "pending") == "missed_with_notice":
            continue
        sa = a.get("scheduled_at") or ""
        if sa >= "2099-01-01":
            continue
        out.append(a)
    return out


# ── Indicadores individuais (cada um devolve um dict de "card" ou None) ───────

def _insight_confirmations(tenant: dict) -> dict | None:
    """Consultas de amanhã ainda sem confirmação enviada/respondida."""
    pend = db.get_pending_confirmations_for_tomorrow(tenant["id"])
    n = len(pend)
    if n <= 0:
        return None
    plural = "pacientes ainda não confirmaram" if n > 1 else "paciente ainda não confirmou"
    return {
        "id": "confirmations", "icon": "📩", "severity": "attention",
        "text": f"{n} {plural} a presença de amanhã.",
        "count": n,
    }


def _insight_occupancy(tenant: dict, now: datetime) -> dict | None:
    """Ocupação dos próximos 7 dias = ocupados / (ocupados + livres)."""
    import calendar_service as cal
    free = len(cal.get_available_slots(tenant, days_ahead=7, limit=500))
    end = now + timedelta(days=7)
    booked = len(_active(db.get_appointments_in_range(
        tenant["id"], now.isoformat(), end.isoformat())))
    capacity = free + booked
    # Sem nada agendado nos próximos 7 dias → não há "ocupação" que valha comentar
    # (consultório novo/parado); evita o ruído de "0% de ocupação".
    if capacity <= 0 or booked <= 0:
        return None
    pct = round(100 * booked / capacity)
    if pct >= _OCCUPANCY_HIGH:
        sev, extra = "attention", " Sua agenda está bem cheia — avalie abrir mais horários ou pausas."
    elif pct <= _OCCUPANCY_LOW:
        sev, extra = "attention", " Há bastante espaço livre — bom momento para reagendar retornos."
    else:
        sev, extra = "info", ""
    return {
        "id": "occupancy", "icon": "📊", "severity": sev,
        "text": f"Sua agenda dos próximos 7 dias está com {pct}% de ocupação.{extra}",
        "value": pct, "booked": booked, "free": free,
    }


def _insight_no_return(tenant: dict, now: datetime) -> dict | None:
    """Pacientes que tiveram sessão recente mas estão sem próximo agendamento."""
    tid = tenant["id"]
    recent_start = (now - timedelta(days=_NO_RETURN_RECENT_DAYS)).isoformat()
    # Passado recente (sessões que ocorreram) e futuro (qualquer agendamento à frente).
    past = _active(db.get_appointments_in_range(tid, recent_start, now.isoformat()))
    future = _active(db.get_appointments_in_range(
        tid, now.isoformat(), (now + timedelta(days=400)).isoformat()))
    future_phones = {a.get("phone") for a in future}
    # Última sessão passada por telefone (mais recente).
    last_by_phone: dict[str, str] = {}
    for a in past:
        ph = a.get("phone")
        sa = a.get("scheduled_at") or ""
        if ph and sa > last_by_phone.get(ph, ""):
            last_by_phone[ph] = sa
    orphans = []  # (phone, dias_sem_retorno)
    for ph, sa in last_by_phone.items():
        if ph in future_phones:
            continue
        try:
            dt = datetime.fromisoformat(sa)
        except Exception:
            continue
        days = (now - dt).days
        orphans.append((ph, days))
    if not orphans:
        return None
    orphans.sort(key=lambda t: t[1], reverse=True)
    n = len(orphans)
    oldest = orphans[0][1]
    if oldest < _NO_RETURN_ALERT_DAYS and n == 0:
        return None
    plural = "pacientes ativos estão" if n > 1 else "paciente ativo está"
    txt = f"{n} {plural} sem próximo agendamento"
    if oldest >= _NO_RETURN_ALERT_DAYS:
        txt += f" (o mais antigo há {oldest} dias)"
    txt += "."
    sev = "attention" if oldest >= _NO_RETURN_ALERT_DAYS else "info"
    return {"id": "no_return", "icon": "🔁", "severity": sev, "text": txt,
            "count": n, "oldest_days": oldest}


def _forecast_revenue(tid: int, m_start: str, m_end: str) -> float:
    """Faturamento previsto do mês inteiro (sessões cobráveis × preço do paciente),
    mesmo critério da cobrança. Inclui sessões futuras já agendadas no mês."""
    counts = db.get_session_counts_by_month(tid, m_start, m_end)  # {phone: n}
    total = 0.0
    for p in db.get_patients_with_price(tid):
        n = counts.get(p.get("phone"), 0)
        if n:
            total += (p.get("session_price") or 0) * n
    return round(total, 2)


def _insight_revenue(tenant: dict, now: datetime) -> dict | None:
    """Faturamento previsto do mês e variação vs. mês anterior."""
    tid = tenant["id"]
    m_start, m_end = _month_bounds(now)
    p_start, p_end = _prev_month_bounds(now)
    cur = _forecast_revenue(tid, m_start, m_end)
    prev = _forecast_revenue(tid, p_start, p_end)
    if cur <= 0 and prev <= 0:
        return None
    txt = f"Faturamento previsto do mês: {_brl(cur)}"
    trend = None
    if prev > 0:
        pct = round(100 * (cur - prev) / prev)
        trend = pct
        if pct > 0:
            txt += f" (+{pct}% vs. mês passado)."
        elif pct < 0:
            txt += f" ({pct}% vs. mês passado)."
        else:
            txt += " (estável vs. mês passado)."
    else:
        txt += "."
    return {"id": "revenue", "icon": "💰", "severity": "info", "text": txt,
            "value": cur, "prev": prev, "trend_pct": trend}


def _insight_contracts(tenant: dict) -> dict | None:
    """Contratos aguardando assinatura e vencidos."""
    rows = db.list_contracts_for_tenant(tenant["id"])
    # Só o contrato mais recente por telefone conta (histórico não polui).
    latest: dict[str, dict] = {}
    for c in rows:  # já vem ordenado por created_at DESC
        ph = c.get("phone")
        if ph and ph not in latest:
            latest[ph] = c
    waiting = sum(1 for c in latest.values() if c.get("status") in ("enviado", "pendente"))
    expired = sum(1 for c in latest.values() if c.get("status") == "expirado")
    if waiting == 0 and expired == 0:
        return None
    parts = []
    if waiting:
        parts.append(f"{waiting} aguardando assinatura")
    if expired:
        parts.append(f"{expired} vencido{'s' if expired > 1 else ''}")
    txt = "Contratos: " + " · ".join(parts) + "."
    return {"id": "contracts", "icon": "📄", "severity": "attention", "text": txt,
            "waiting": waiting, "expired": expired}


def _insight_no_shows(tenant: dict, now: datetime) -> dict | None:
    """Taxa de falta (sem aviso) do mês corrente."""
    tid = tenant["id"]
    m_start, m_end = _month_bounds(now)
    stats = db.get_dashboard_stats(tid, m_start, m_end, now.isoformat())
    total = stats.get("total") or 0
    missed = stats.get("missed_no_notice") or 0
    if total < 4 or missed == 0:
        return None  # amostra pequena/irrelevante → não vira ruído
    pct = round(100 * missed / total)
    if pct < 10:
        return None
    return {"id": "no_shows", "icon": "⚠️", "severity": "attention",
            "text": f"Taxa de falta (sem aviso) do mês: {pct}% ({missed} de {total} sessões).",
            "value": pct, "missed": missed, "total": total}


# ── Fase 2 — cards adicionais ────────────────────────────────────────────────

def _insight_today(tenant: dict, now: datetime) -> dict | None:
    """Resumo do dia: quantas sessões hoje e quantas já confirmadas."""
    d_start, d_end = _day_bounds(now)
    appts = _active(db.get_appointments_in_range(tenant["id"], d_start, d_end))
    n = len(appts)
    if n <= 0:
        return None
    conf = sum(1 for a in appts
               if a.get("confirmed") or (a.get("attendance") == "attended"))
    plural = "sessões" if n > 1 else "sessão"
    if conf >= n:
        extra = " Todas confirmadas ✓"
    elif conf > 0:
        extra = f" {conf} já confirmada{'s' if conf > 1 else ''}."
    else:
        extra = " Nenhuma confirmada ainda."
    return {"id": "today", "icon": "📅", "severity": "info",
            "text": f"Hoje você tem {n} {plural}.{extra}",
            "count": n, "confirmed": conf}


def _insight_reminder_effectiveness(tenant: dict, now: datetime) -> dict | None:
    """Efetividade dos lembretes no mês: % que confirma após o envio.
    Só aparece com amostra mínima (dado acumula a partir da ativação da Fase 4)."""
    m_start, m_end = _month_bounds(now)
    data = db.get_reminder_effectiveness(tenant["id"], m_start, m_end)
    sent = data.get("sent") or 0
    confirmed = data.get("confirmed") or 0
    if sent < 5:
        return None  # amostra pequena → não vira ruído
    pct = round(100 * confirmed / sent)
    if pct >= 70:
        sev = "info"
        txt = (f"Seus lembretes estão funcionando: {pct}% confirmam após o envio "
               f"({confirmed} de {sent}).")
    else:
        sev = "attention"
        txt = (f"Só {pct}% confirmam após o lembrete ({confirmed} de {sent}) — "
               f"vale revisar horário ou texto do disparo.")
    return {"id": "reminder_effectiveness", "icon": "📲", "severity": sev,
            "text": txt, "value": pct, "sent": sent, "confirmed": confirmed}


def _insight_reschedules(tenant: dict, now: datetime) -> dict | None:
    """Remarcações do mês (sinal operacional; só informativo)."""
    m_start, m_end = _month_bounds(now)
    n = db.get_reschedules_in_range(tenant["id"], m_start, m_end)
    if n <= 0:
        return None
    plural = "remarcações" if n > 1 else "remarcação"
    return {"id": "reschedules", "icon": "🔀", "severity": "info",
            "text": f"{n} {plural} este mês.", "count": n}


def _insight_cancellations(tenant: dict, now: datetime) -> dict | None:
    """Cancelamentos do mês, destacando os feitos em cima da hora (<24h)."""
    m_start, m_end = _month_bounds(now)
    data = db.get_cancellations_in_range(tenant["id"], m_start, m_end)
    total = data.get("total") or 0
    late = data.get("late") or 0
    if total <= 0:
        return None
    plural = "cancelamentos" if total > 1 else "cancelamento"
    if late > 0:
        sev = "attention"
        txt = (f"{total} {plural} este mês — {late} em cima da hora "
               f"(menos de 24h de antecedência).")
    else:
        sev = "info"
        txt = f"{total} {plural} este mês, todos com antecedência."
    return {"id": "cancellations", "icon": "🚫", "severity": sev,
            "text": txt, "total": total, "late": late}


_BUILDERS_NOW = (_insight_today, _insight_occupancy, _insight_no_return,
                 _insight_reminder_effectiveness, _insight_reschedules,
                 _insight_cancellations, _insight_revenue, _insight_no_shows)
_BUILDERS_SIMPLE = (_insight_confirmations, _insight_contracts)


def build_home_insights(tenant: dict) -> dict:
    """Monta o resumo da Home. Nunca levanta exceção (fail-safe)."""
    now = _now()
    insights: list[dict] = []
    for fn in _BUILDERS_SIMPLE:
        try:
            card = fn(tenant)
            if card:
                insights.append(card)
        except Exception:
            logger.exception("insight %s falhou", getattr(fn, "__name__", "?"))
    for fn in _BUILDERS_NOW:
        try:
            card = fn(tenant, now)
            if card:
                insights.append(card)
        except Exception:
            logger.exception("insight %s falhou", getattr(fn, "__name__", "?"))

    # Ordena: atenção primeiro, mantendo a ordem de inserção dentro de cada grupo.
    order = {"attention": 0, "info": 1}
    insights.sort(key=lambda c: order.get(c.get("severity"), 2))

    attention = sum(1 for c in insights if c.get("severity") == "attention")
    return {
        "greeting": _greeting(tenant, now),
        "generated_at": now.isoformat(timespec="minutes"),
        "insights": insights,
        "attention_count": attention,
        "all_clear": len(insights) == 0,
    }


# ── Fase 3 — camada narrativa com IA (opcional, atrás de flag) ────────────────
#
# REGRA INEGOCIÁVEL: a IA só recebe os NÚMEROS/frases agregados que este módulo já
# montou (status, datas, valores). Ela NUNCA vê conversa, nome de paciente por
# contexto clínico, nem o campo `notes`. Os próprios cards já são livres de
# conteúdo clínico. A IA só reescreve/prioriza esses fatos em um parágrafo curto.

_NARRATIVE_SYSTEM = (
    "Você é o assistente de GESTÃO de um consultório de psicologia. Recebe apenas "
    "indicadores ADMINISTRATIVOS já calculados (agenda, confirmações, faturamento "
    "previsto, contratos, cancelamentos). Escreva um resumo curto e acolhedor em "
    "português do Brasil (2 a 4 frases), em tom profissional e leve, para a própria "
    "psicóloga ler ao abrir o painel.\n"
    "REGRAS: (1) Use SOMENTE os fatos fornecidos — nunca invente números, nomes ou "
    "situações. (2) Priorize o que pede atenção. (3) Não repita todos os itens em "
    "lista; conecte os mais importantes de forma natural. (4) Jamais mencione "
    "conteúdo clínico, diagnóstico ou detalhe de sessão — você não tem acesso a isso. "
    "(5) Não use markdown nem títulos; apenas o parágrafo."
)


def build_home_narrative(tenant: dict, payload: dict | None = None) -> str:
    """Gera um parágrafo-resumo com IA a partir dos indicadores agregados.
    Fail-open: retorna "" se a flag estiver desligada, sem chave de API, sem
    indicadores ou em qualquer erro — a Home segue mostrando os cards normais."""
    try:
        if not tenant.get("ai_narrative_enabled"):
            return ""
        data = payload or build_home_insights(tenant)
        cards = data.get("insights") or []
        if not cards:
            return ""  # nada a narrar; o card "tudo em dia" já cobre
        greeting = data.get("greeting") or ""
        linhas = []
        for c in cards:
            marca = "[ATENÇÃO] " if c.get("severity") == "attention" else ""
            linhas.append(f"- {marca}{c.get('text', '')}")
        contexto = (
            f"Saudação sugerida: {greeting}\n"
            f"Indicadores administrativos de hoje:\n" + "\n".join(linhas)
        )
        import agent  # import tardio: evita custo/ciclo no boot
        txt = agent._call_llm(
            _NARRATIVE_SYSTEM,
            [{"role": "user", "content": contexto}],
            max_tokens=320,
            force_json=False,
        )
        return (txt or "").strip()
    except Exception:
        logger.exception("narrativa da Home falhou (fail-open)")
        return ""
