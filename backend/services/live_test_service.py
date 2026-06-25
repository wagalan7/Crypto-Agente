"""
Live Test Service — contador do "teste do canário a 0.50".

Objetivo (pedido do usuário): a partir do flip pra LIVE_SIZE_MULT=0.50, contar
os auto-trades REAIS em sequência e avisar:
  - a cada novo auto-trade: "🧪 Teste 0.50 · auto #N/alvo — SYMBOL"
  - quando bater o ALVO de N auto-trades  → 🏁 "hora de analisar"
  - quando fechar a JANELA de X dias        → 🏁 "hora de analisar"

Design:
  - CONTAGEM derivada ao vivo do banco (RealTrade source='auto', opened_at >=
    start) — sobrevive a redeploy sem dessincronizar.
  - Idempotência dos marcos via LiveTestState (singleton id=1): flag persistente
    pra não reenviar push a cada scan / após redeploy.

Config (env):
  LIVE_TEST_ENABLED   (default "true")
  LIVE_TEST_START_AT  ISO 8601 (default "2026-06-13T12:00:00+00:00" = 09:00 BRT)
  LIVE_TEST_TARGET    nº de auto-trades pra fechar o teste (default 10)
  LIVE_TEST_DAYS      janela em dias (default 7)
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func

from db import DB_ENABLED, get_session
from models.real_trade import RealTrade
from models.live_test_state import LiveTestState

log = logging.getLogger(__name__)

LIVE_TEST_ENABLED = os.getenv("LIVE_TEST_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_DEFAULT_START = "2026-06-13T12:00:00+00:00"  # 09:00 BRT de 13/06
LIVE_TEST_TARGET = int(os.getenv("LIVE_TEST_TARGET", "10"))
LIVE_TEST_DAYS = int(os.getenv("LIVE_TEST_DAYS", "7"))
# Escopo FORA-only (pedido do usuário 2026-06-25): o canário passa a contar só
# auto-trades FORA da allowlist (notes "[filler]"), pra avaliar isoladamente o
# P&L dos próximos N FORA e decidir capital/canário. Rótulo e unidade ficam
# configuráveis pro frontend exibir o size certo (não há mais "0.50" cravado).
LIVE_TEST_FILLER_ONLY = os.getenv("LIVE_TEST_FILLER_ONLY", "true").strip().lower() in ("1", "true", "yes")
LIVE_TEST_LABEL = os.getenv("LIVE_TEST_LABEL", "Teste FORA 0,75x").strip() or "Teste FORA 0,75x"
LIVE_TEST_UNIT = os.getenv("LIVE_TEST_UNIT", "trades FORA").strip() or "trades FORA"


def _start_at() -> datetime:
    raw = os.getenv("LIVE_TEST_START_AT", _DEFAULT_START).strip() or _DEFAULT_START
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.fromisoformat(_DEFAULT_START)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_filler_trade(trade: dict) -> bool:
    """True se o auto-trade é FORA da allowlist (marcado [filler])."""
    if trade.get("_is_filler"):
        return True
    return "[filler]" in (trade.get("notes") or "")


def _parse_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _count_auto_since_start(session) -> int:
    start = _start_at()
    stmt = (
        select(func.count())
        .select_from(RealTrade)
        .where(RealTrade.source == "auto")
        .where(RealTrade.opened_at >= start)
    )
    if LIVE_TEST_FILLER_ONLY:
        stmt = stmt.where(RealTrade.notes.like("%[filler]%"))
    return int((await session.execute(stmt)).scalar() or 0)


async def _load_state(session) -> LiveTestState:
    st = (await session.execute(
        select(LiveTestState).where(LiveTestState.id == 1)
    )).scalar_one_or_none()
    if st is None:
        st = LiveTestState(id=1)
        session.add(st)
        await session.flush()
    return st


async def _push_done(reason: str, n: int) -> None:
    """Push do marco de conclusão do teste."""
    try:
        from services import push_service
        days = LIVE_TEST_DAYS
        await push_service.notify_alert(
            title=f"🏁 {LIVE_TEST_LABEL} — hora de analisar",
            body=(
                f"{reason} ({n}/{LIVE_TEST_TARGET} {LIVE_TEST_UNIT}). "
                f"Bora somar o P&L desses FORA e decidir: colocar mais capital, "
                f"subir/descer o canário ou ajustar o size. Janela: {days} dias."
            ),
            tag="livetest-done",
        )
    except Exception as e:
        log.warning(f"[live-test] push done falhou: {e}")


async def on_auto_trade_opened(trade: Optional[dict]) -> None:
    """Chamado logo após um auto-trade real abrir. Conta e dispara o push de
    progresso; se bater o alvo, dispara o marco (idempotente). Fail-soft."""
    if not LIVE_TEST_ENABLED or not DB_ENABLED or not trade:
        return
    try:
        if trade.get("source") != "auto":
            return
        # FORA-only: só conta/avisa trades fora da allowlist ([filler]).
        if LIVE_TEST_FILLER_ONLY and not _is_filler_trade(trade):
            return
        opened = _parse_dt(trade.get("opened_at"))
        start = _start_at()
        # Trade anterior ao marco não conta nem notifica.
        if opened is not None and opened < start:
            return

        async with get_session() as session:
            n = await _count_auto_since_start(session)

        sym = (trade.get("symbol") or "?").split("/")[0]
        side = (trade.get("side") or "").upper()
        entry = trade.get("entry_price")
        sl = trade.get("planned_stop")
        tp1 = trade.get("planned_tp1")

        # Push de progresso (1 por trade — evento único de abertura).
        try:
            from services import push_service
            await push_service.notify_alert(
                title=f"🧪 {LIVE_TEST_LABEL} · #{n}/{LIVE_TEST_TARGET}",
                body=f"{sym} {side} · entry {entry} · SL {sl} · TP1 {tp1}",
                tag=f"livetest-{n}",
            )
        except Exception as e:
            log.warning(f"[live-test] push progresso falhou: {e}")

        # Marco por contagem (idempotente via flag).
        if n >= LIVE_TEST_TARGET:
            async with get_session() as session:
                st = await _load_state(session)
                if not st.count_milestone_notified:
                    st.count_milestone_notified = True
                    st.updated_at = datetime.now(timezone.utc)
                    await session.commit()
                    await _push_done(f"Bateu o alvo de {LIVE_TEST_TARGET} auto-trades", n)
    except Exception as e:
        log.warning(f"[live-test] on_auto_trade_opened falhou: {e}")


async def check_milestones() -> None:
    """Chamado periodicamente (snapshot loop). Cobre o marco de TEMPO (janela de
    X dias) e, como rede de segurança, o de contagem. Idempotente. Fail-soft."""
    if not LIVE_TEST_ENABLED or not DB_ENABLED:
        return
    try:
        async with get_session() as session:
            n = await _count_auto_since_start(session)
            st = await _load_state(session)
            now = datetime.now(timezone.utc)
            start = _start_at()
            deadline = start + timedelta(days=LIVE_TEST_DAYS)

            fire_count = (n >= LIVE_TEST_TARGET) and not st.count_milestone_notified
            fire_time = (now >= deadline) and not st.time_milestone_notified

            if fire_count:
                st.count_milestone_notified = True
            if fire_time:
                st.time_milestone_notified = True
            if fire_count or fire_time:
                st.updated_at = now
                await session.commit()

        if fire_count:
            await _push_done(f"Bateu o alvo de {LIVE_TEST_TARGET} auto-trades", n)
        if fire_time:
            await _push_done(f"Fechou a janela de {LIVE_TEST_DAYS} dias", n)
    except Exception as e:
        log.warning(f"[live-test] check_milestones falhou: {e}")


async def status() -> dict:
    """Snapshot do progresso do teste — pro painel e pra inspeção."""
    start = _start_at()
    now = datetime.now(timezone.utc)
    deadline = start + timedelta(days=LIVE_TEST_DAYS)
    n = 0
    flags = {"count": False, "time": False}
    if DB_ENABLED:
        try:
            async with get_session() as session:
                n = await _count_auto_since_start(session)
                st = (await session.execute(
                    select(LiveTestState).where(LiveTestState.id == 1)
                )).scalar_one_or_none()
                if st:
                    flags = {
                        "count": st.count_milestone_notified,
                        "time": st.time_milestone_notified,
                    }
        except Exception as e:
            log.warning(f"[live-test] status falhou: {e}")
    days_left = max(0.0, (deadline - now).total_seconds() / 86400.0)
    return {
        "enabled": LIVE_TEST_ENABLED,
        "label": LIVE_TEST_LABEL,
        "unit": LIVE_TEST_UNIT,
        "filler_only": LIVE_TEST_FILLER_ONLY,
        "start_at": start.isoformat(),
        "deadline_at": deadline.isoformat(),
        "target": LIVE_TEST_TARGET,
        "days": LIVE_TEST_DAYS,
        "count": n,
        "remaining": max(0, LIVE_TEST_TARGET - n),
        "days_left": round(days_left, 2),
        "count_done": n >= LIVE_TEST_TARGET,
        "time_done": now >= deadline,
        "notified": flags,
    }
