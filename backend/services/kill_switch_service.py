"""
Kill-Switch / Circuit Breaker (#11.4) — proteção pré-execução pra trades reais.

Antes de QUALQUER ordem real (auto ou shadow→real), passa pelo `check_can_trade()`.
Se algum limite estourar, bloqueia + grava motivo + retorna {allowed: False}.

Checks (todos com env var override):
  KILL_SWITCH=true                  → bloqueio manual global (default false)
  KILL_MAX_OPEN_POSITIONS=5         → bloqueia se já há N posições abertas
  KILL_MAX_DAILY_LOSS_USD=200       → bloqueia se P&L do dia <= -X USD (piso/fallback)
  KILL_MAX_DAILY_LOSS_PCT=0         → se >0, limite = pct × equity real (escala c/ banca;
                                      cai pro _USD se equity indisponível)
  KILL_MAX_CONSEC_LOSSES=3          → bloqueia após N losses seguidos
  KILL_COOLDOWN_HOURS=12            → janela em que o bloqueio por consec_losses fica ativo
  KILL_MAX_DAILY_TRADES=20          → bloqueia após N trades abertos hoje

Estado é derivado das tabelas (RealTrade) — sem cache em memória, sobrevive
restart. Cada call recomputa os contadores.

API:
  await check_can_trade() → {"allowed": bool, "reason": str|None, "checks": {...}}
  await status()          → mesmo shape + thresholds configurados (pra UI)
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func, desc

from db import DB_ENABLED, get_session
from models.real_trade import RealTrade

log = logging.getLogger(__name__)

# Estado em memoria pra dedupe de notificacao Telegram (uma por dia).
_KILL_NOTIFIED_DAY: Optional[str] = None

# Slot livre pós-TP1/BE: quando ON, posições já no breakeven (phase=='post_tp1')
# não contam pro teto de posições abertas — espelha portfolio_service. DEFAULT OFF.
SLOT_FREE_AFTER_TP1_BE = os.getenv("SLOT_FREE_AFTER_TP1_BE", "false").strip().lower() in ("1", "true", "yes", "on")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except Exception:
        return default


def _daily_reset_at() -> Optional[datetime]:
    """Âncora opcional de reset (KILL_DAILY_RESET_AT, ISO). Quando setada, o PnL
    diário e o streak de losses contam SÓ a partir desse instante — permite
    'zerar' os contadores sob demanda (ex.: após repor/ajustar capital pra testar)
    sem esperar a virada do dia UTC. Sem a env, comportamento normal. Aplicada só
    se a âncora for mais recente que a janela natural (não estende pra trás)."""
    raw = os.getenv("KILL_DAILY_RESET_AT", "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def thresholds() -> dict:
    return {
        "kill_switch": _env_bool("KILL_SWITCH", False),
        "max_open_positions": _env_int("KILL_MAX_OPEN_POSITIONS", 5),
        "max_daily_loss_usd": _env_float("KILL_MAX_DAILY_LOSS_USD", 200.0),
        "max_daily_loss_pct": _env_float("KILL_MAX_DAILY_LOSS_PCT", 0.0),
        "max_consec_losses": _env_int("KILL_MAX_CONSEC_LOSSES", 3),
        "cooldown_hours": _env_int("KILL_COOLDOWN_HOURS", 12),
        "max_daily_trades": _env_int("KILL_MAX_DAILY_TRADES", 20),
    }


async def _count_open() -> int:
    if not DB_ENABLED:
        return 0
    async with get_session() as session:
        stmt = select(func.count(RealTrade.id)).where(RealTrade.status == "open")
        if SLOT_FREE_AFTER_TP1_BE:
            # Posição no BE (post_tp1) não ocupa slot. IS DISTINCT FROM trata NULL
            # como at-risk (conta), só exclui exatamente 'post_tp1'.
            stmt = stmt.where(RealTrade.phase.is_distinct_from("post_tp1"))
        return int((await session.execute(stmt)).scalar() or 0)


async def _daily_pnl_usd() -> float:
    """Soma de pnl_usd de trades FECHADOS hoje (UTC)."""
    if not DB_ENABLED:
        return 0.0
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = _daily_reset_at()
    if anchor and anchor > start:
        start = anchor
    async with get_session() as session:
        stmt = (
            select(func.coalesce(func.sum(RealTrade.pnl_usd), 0.0))
            .where(RealTrade.closed_at >= start)
            .where(RealTrade.status != "open")
        )
        return float((await session.execute(stmt)).scalar() or 0.0)


async def _daily_opens() -> int:
    """Conta trades ABERTOS hoje (pra limitar volume)."""
    if not DB_ENABLED:
        return 0
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session() as session:
        stmt = select(func.count(RealTrade.id)).where(RealTrade.opened_at >= start)
        return int((await session.execute(stmt)).scalar() or 0)


async def _recent_losses_streak(hours: int) -> tuple[int, Optional[datetime]]:
    """
    Conta losses CONSECUTIVOS nas últimas `hours` horas, partindo do mais recente.
    Retorna (count, last_close_time). Para na primeira win.
    """
    if not DB_ENABLED:
        return (0, None)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    anchor = _daily_reset_at()
    if anchor and anchor > since:
        since = anchor
    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.closed_at >= since)
            .where(RealTrade.status != "open")
            .order_by(desc(RealTrade.closed_at))
            .limit(50)
        )
        rows = (await session.execute(stmt)).scalars().all()
    streak = 0
    last_close = None
    for t in rows:
        # Um trade que EMBOLSOU o TP1 (tp1_realized_usd>0) NÃO é loss pro streak:
        # o SL já subiu pro BE estrutural, então fechar a sobra em stop/BE é um
        # "TP1 + BE" (capital protegido), não uma perda crua. Sem isso, um trade
        # vencedor com TP1 batido (ex.: ATOM tp1=$2,55, sobra -$0,17) inflava a
        # contagem e disparava o kill-switch indevidamente. Só conta loss real:
        # sem TP1 embolsado E com PnL líquido negativo (BE em r=0 não conta).
        tp1_banked = t.tp1_realized_usd is not None and float(t.tp1_realized_usd) > 0
        if tp1_banked:
            is_loss = False
        elif t.pnl_usd is not None:
            is_loss = float(t.pnl_usd) < 0
        elif t.realized_r is not None:
            is_loss = float(t.realized_r) < 0
        else:
            is_loss = t.status == "closed_stop"
        if is_loss:
            streak += 1
            if last_close is None:
                last_close = t.closed_at
        else:
            break
    return (streak, last_close)


async def _resolve_daily_loss_limit_usd(th: dict) -> tuple[float, str]:
    """
    Resolve o limite de perda diária em USD (go-live #3). Se KILL_MAX_DAILY_LOSS_PCT
    > 0, escala com o equity real da exchange; senão (ou se equity indisponível),
    usa o piso absoluto KILL_MAX_DAILY_LOSS_USD.
    Retorna (limite_usd, descrição_origem).
    """
    pct = th.get("max_daily_loss_pct") or 0.0
    floor = float(th["max_daily_loss_usd"])
    if pct > 0:
        try:
            from services import exchange_service
            eq = await exchange_service.get_equity()
            if eq.get("ok") and float(eq.get("total_usd") or 0) > 0:
                equity = float(eq["total_usd"])
                return equity * (pct / 100.0), f"{pct:.1f}% × equity ${equity:.0f}"
        except Exception as e:
            log.warning(f"[kill] equity p/ daily-loss-pct falhou: {e}")
        return floor, f"piso USD (equity indisponível; pct {pct:.1f}% ignorado)"
    return floor, "USD absoluto"


async def check_can_trade() -> dict:
    """
    Chamado ANTES de cada ordem real. Retorna:
      {"allowed": bool, "reason": str|None, "checks": {...}, "thresholds": {...}}
    """
    th = thresholds()
    checks = {}
    blocked_reasons = []

    # 1. Manual kill-switch
    if th["kill_switch"]:
        blocked_reasons.append("KILL_SWITCH=true (manual override)")
    checks["kill_switch_manual"] = th["kill_switch"]

    # 2. Posições abertas
    open_count = await _count_open()
    checks["open_positions"] = open_count
    if open_count >= th["max_open_positions"]:
        blocked_reasons.append(
            f"max_open_positions: {open_count}/{th['max_open_positions']}"
        )

    # 3. P&L diário (limite escala com equity se KILL_MAX_DAILY_LOSS_PCT > 0)
    pnl_today = await _daily_pnl_usd()
    daily_loss_limit, limit_src = await _resolve_daily_loss_limit_usd(th)
    checks["daily_pnl_usd"] = round(pnl_today, 2)
    checks["daily_loss_limit_usd"] = round(daily_loss_limit, 2)
    checks["daily_loss_limit_src"] = limit_src
    _anchor = _daily_reset_at()
    checks["daily_reset_at"] = _anchor.isoformat() if _anchor else None
    if pnl_today <= -daily_loss_limit:
        blocked_reasons.append(
            f"daily_loss: ${pnl_today:.2f} <= -${daily_loss_limit:.2f} ({limit_src})"
        )

    # 4. Losses consecutivos (cooldown)
    streak, last_close = await _recent_losses_streak(th["cooldown_hours"])
    checks["consec_losses"] = streak
    checks["last_loss_close"] = last_close.isoformat() if last_close else None
    if streak >= th["max_consec_losses"]:
        blocked_reasons.append(
            f"consec_losses: {streak}/{th['max_consec_losses']} "
            f"(cooldown {th['cooldown_hours']}h)"
        )

    # 5. Trades abertos hoje (volume cap)
    opens_today = await _daily_opens()
    checks["daily_opens"] = opens_today
    if opens_today >= th["max_daily_trades"]:
        blocked_reasons.append(
            f"daily_opens: {opens_today}/{th['max_daily_trades']}"
        )

    allowed = len(blocked_reasons) == 0
    reason = " | ".join(blocked_reasons) if blocked_reasons else None

    # Notifica Telegram quando o kill-switch (por daily_loss) ativa, uma vez por dia.
    if not allowed and pnl_today <= -daily_loss_limit:
        global _KILL_NOTIFIED_DAY
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _KILL_NOTIFIED_DAY != today_key:
            _KILL_NOTIFIED_DAY = today_key
            try:
                from services.notification_service import send_telegram, fmt_kill_switch
                await send_telegram(
                    fmt_kill_switch(pnl_today, daily_loss_limit),
                    event_type="kill",
                )
            except Exception as e:
                log.warning(f"[notify] telegram kill falhou: {e}")

    return {
        "allowed": allowed,
        "reason": reason,
        "checks": checks,
        "thresholds": th,
    }


async def status() -> dict:
    """Endpoint-friendly: estado atual sem efeito colateral."""
    return await check_can_trade()
