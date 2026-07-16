"""
Risk Service — circuit breaker de drawdown.

Computa daily/weekly DD a partir dos `recommendation_snapshots` resolvidos
(realized_r != null) e mantém o singleton `RiskState` atualizado.

Triggers (Fase 1):
  - daily_dd_pct  <= -3%   →  pausa "DD diário"
  - weekly_dd_pct <= -6%   →  pausa "DD semanal"

DD aqui é P&L total em % da banca, somando contribuição de cada trade
resolvido na janela. Cada trade contribui `realized_r * risk_pct`
(consistente com como `DailyPnLPanel` calcula `pct = r * risk_pct`).

Reset automático na virada do dia/semana UTC: se `current_day_utc`
no DB difere de hoje, daily_dd zera. Mesmo pra semana.

Pause manual (kill switch) NÃO é resetado automaticamente — fica
ligado até o usuário explicitamente desligar via endpoint.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func

from db import DB_ENABLED, get_session
from models.risk_state import RiskState
from models.risk_event import RiskEvent
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

# Thresholds — alinhados com ROADMAP Fase 1.1
DAILY_DD_LIMIT_PCT = -3.0
WEEKLY_DD_LIMIT_PCT = -6.0


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_week_utc() -> str:
    """ISO year-week, ex: '2026-W22'. Vira na segunda 00:00 UTC."""
    return datetime.now(timezone.utc).strftime("%G-W%V")


async def _get_or_create_state(session) -> RiskState:
    """Garante que existe a linha singleton id=1."""
    stmt = select(RiskState).where(RiskState.id == 1)
    state = (await session.execute(stmt)).scalar_one_or_none()
    if state is None:
        state = RiskState(
            id=1,
            trading_paused=False,
            current_day_utc=_today_utc(),
            current_week_utc=_this_week_utc(),
        )
        session.add(state)
        await session.flush()
    return state


def _log_event(session, state: RiskState, event_type: str, reason: str | None) -> None:
    """Grava evento de transição na tabela risk_events (snapshot das métricas)."""
    ev = RiskEvent(
        event_type=event_type,
        reason=reason,
        daily_dd_pct=state.daily_dd_pct,
        weekly_dd_pct=state.weekly_dd_pct,
        daily_trades=state.daily_trades,
        weekly_trades=state.weekly_trades,
    )
    session.add(ev)


async def _compute_window_dd(session, hours: int) -> tuple[float, int]:
    """
    Soma (realized_r * risk_pct) de trades resolvidos nas últimas `hours`.
    Retorna (dd_pct, trade_count).
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(
            func.coalesce(
                func.sum(RecommendationSnapshot.realized_r * RecommendationSnapshot.risk_pct),
                0.0,
            ),
            func.count(RecommendationSnapshot.id),
        )
        .where(RecommendationSnapshot.outcome_at >= since)
        .where(RecommendationSnapshot.realized_r.is_not(None))
    )
    row = (await session.execute(stmt)).one()
    return float(row[0]), int(row[1])


async def update_and_check() -> dict:
    """
    Atualiza métricas de DD + decide se aciona pausa automática.
    Chamada periodicamente (a cada scan loop) e exposta via endpoint.

    Retorna dict com estado atual pra UI/log.
    """
    if not DB_ENABLED:
        return {"enabled": False}

    async with get_session() as session:
        state = await _get_or_create_state(session)

        # Reset por virada de dia/semana
        today = _today_utc()
        week = _this_week_utc()
        if state.current_day_utc != today:
            state.current_day_utc = today
            # Não zera weekly aqui — semana tem seu próprio rollover
        if state.current_week_utc != week:
            state.current_week_utc = week
            # Reseta pausa AUTOMÁTICA se estava ativa (não a manual)
            if state.trading_paused and not state.pause_manual:
                log.info("Virada de semana — pausa automática resetada.")
                state.trading_paused = False
                state.pause_reason = None
                state.paused_at = None
                _log_event(session, state, "auto_resume", "Virada de semana UTC")

        # Recalcula DD
        daily_dd, daily_trades = await _compute_window_dd(session, hours=24)
        weekly_dd, weekly_trades = await _compute_window_dd(session, hours=24 * 7)
        state.daily_dd_pct = daily_dd
        state.weekly_dd_pct = weekly_dd
        state.daily_trades = daily_trades
        state.weekly_trades = weekly_trades

        # ── Auto-resume do circuit breaker AUTOMÁTICO ───────────────────────
        # BUGFIX: a virada de DIA atualizava a data mas NÃO despausava — só a
        # virada de SEMANA fazia. Uma pausa DIÁRIA (DD -3%) ficava presa até
        # segunda-feira (ou resume manual), deixando o bot parado por dias.
        # Regra: se a pausa é automática (não manual), disparou num dia UTC
        # ANTERIOR e AMBOS os DD já voltaram pra dentro do limite → retoma.
        # Exigir weekly saudável impede soltar uma pausa SEMANAL cedo demais.
        if state.trading_paused and not state.pause_manual and state.paused_at:
            _pa = state.paused_at
            if _pa.tzinfo is None:
                _pa = _pa.replace(tzinfo=timezone.utc)
            _pa_day = _pa.astimezone(timezone.utc).strftime("%Y-%m-%d")
            if (_pa_day != today
                    and daily_dd > DAILY_DD_LIMIT_PCT
                    and weekly_dd > WEEKLY_DD_LIMIT_PCT):
                log.info(
                    f"[circuit-breaker] AUTO-RESUME: dia virou e DD recuperou "
                    f"(d={daily_dd:.2f}% w={weekly_dd:.2f}%)"
                )
                state.trading_paused = False
                state.pause_reason = None
                state.paused_at = None
                _log_event(session, state, "auto_resume", "Virada de dia UTC + DD recuperado")

        # Aciona pausa automática se cruzou limite (e não estava já pausado)
        _just_auto_paused = False
        if not state.trading_paused:
            if daily_dd <= DAILY_DD_LIMIT_PCT:
                state.trading_paused = True
                state.pause_manual = False
                state.pause_reason = (
                    f"DD diário {daily_dd:.2f}% atingiu limite "
                    f"{DAILY_DD_LIMIT_PCT}%"
                )
                state.paused_at = datetime.now(timezone.utc)
                log.warning(f"[circuit-breaker] AUTO-PAUSE: {state.pause_reason}")
                _log_event(session, state, "auto_pause", state.pause_reason)
                _just_auto_paused = True
            elif weekly_dd <= WEEKLY_DD_LIMIT_PCT:
                state.trading_paused = True
                state.pause_manual = False
                state.pause_reason = (
                    f"DD semanal {weekly_dd:.2f}% atingiu limite "
                    f"{WEEKLY_DD_LIMIT_PCT}%"
                )
                state.paused_at = datetime.now(timezone.utc)
                log.warning(f"[circuit-breaker] AUTO-PAUSE: {state.pause_reason}")
                _log_event(session, state, "auto_pause", state.pause_reason)
                _just_auto_paused = True

        state.updated_at = datetime.now(timezone.utc)
        await session.commit()

        result = _to_dict(state)
        # Sinaliza a TRANSIÇÃO pra pausa (o scan loop dispara push de alerta).
        result["just_auto_paused"] = _just_auto_paused
        return result


async def get_status() -> dict:
    """Lê estado atual sem recomputar (rápido, pra endpoint público)."""
    if not DB_ENABLED:
        return {"enabled": False}
    async with get_session() as session:
        state = await _get_or_create_state(session)
        await session.commit()
        return _to_dict(state)


async def is_paused() -> bool:
    """Atalho pra gates de push/scan."""
    if not DB_ENABLED:
        return False
    async with get_session() as session:
        state = await _get_or_create_state(session)
        await session.commit()
        return bool(state.trading_paused)


async def set_manual_pause(paused: bool, reason: Optional[str] = None) -> dict:
    """Kill switch manual — usuário liga/desliga via UI."""
    if not DB_ENABLED:
        return {"enabled": False}
    async with get_session() as session:
        state = await _get_or_create_state(session)
        was_paused = bool(state.trading_paused)
        state.trading_paused = paused
        state.pause_manual = paused
        state.pause_reason = (reason or "Pausa manual via kill switch") if paused else None
        state.paused_at = datetime.now(timezone.utc) if paused else None
        state.updated_at = datetime.now(timezone.utc)
        # Log apenas em transições reais
        if paused and not was_paused:
            _log_event(session, state, "manual_pause", state.pause_reason)
        elif (not paused) and was_paused:
            _log_event(session, state, "manual_resume", reason or "Retomado manualmente")
        await session.commit()
        log.warning(f"[circuit-breaker] MANUAL pause={paused} reason={reason}")
        return _to_dict(state)


async def list_events(days: int = 30, limit: int = 200) -> list[dict]:
    """Lista eventos do circuit breaker dos últimos N dias (mais recentes primeiro)."""
    if not DB_ENABLED:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    async with get_session() as session:
        stmt = (
            select(RiskEvent)
            .where(RiskEvent.ts >= since)
            .order_by(RiskEvent.ts.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "event_type": r.event_type,
                "reason": r.reason,
                "daily_dd_pct": round(r.daily_dd_pct, 3) if r.daily_dd_pct is not None else None,
                "weekly_dd_pct": round(r.weekly_dd_pct, 3) if r.weekly_dd_pct is not None else None,
                "daily_trades": r.daily_trades,
                "weekly_trades": r.weekly_trades,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ]


def _to_dict(state: RiskState) -> dict:
    return {
        "enabled": True,
        "trading_paused": state.trading_paused,
        "pause_reason": state.pause_reason,
        "pause_manual": state.pause_manual,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "daily_dd_pct": round(state.daily_dd_pct, 3),
        "weekly_dd_pct": round(state.weekly_dd_pct, 3),
        "daily_trades": state.daily_trades,
        "weekly_trades": state.weekly_trades,
        "daily_limit_pct": DAILY_DD_LIMIT_PCT,
        "weekly_limit_pct": WEEKLY_DD_LIMIT_PCT,
        "current_day_utc": state.current_day_utc,
        "current_week_utc": state.current_week_utc,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }
