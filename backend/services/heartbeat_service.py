"""
Heartbeat Service (#6) — write/read do last_alive_ts.

`tick()` deve ser chamado a cada server-scan loop. `get_health()`
expõe estado pra endpoint /api/admin/health: gap atual, último source,
contador de ticks.

Gap > HEALTH_ALERT_GAP_SEC = backend está degradado.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db import DB_ENABLED, get_session
from models.heartbeat import Heartbeat

log = logging.getLogger(__name__)

HEALTH_ALERT_GAP_SEC = 300  # 5min sem tick → alerta


async def _get_or_create(session) -> Heartbeat:
    stmt = select(Heartbeat).where(Heartbeat.id == 1)
    hb = (await session.execute(stmt)).scalar_one_or_none()
    if hb is None:
        hb = Heartbeat(
            id=1,
            last_alive_ts=datetime.now(timezone.utc),
            last_source="init",
            tick_count=0,
        )
        session.add(hb)
        await session.flush()
    return hb


async def tick(source: str = "server-scan") -> None:
    """Bate o heartbeat. Silencia erro pra não derrubar o loop."""
    if not DB_ENABLED:
        return
    try:
        async with get_session() as session:
            hb = await _get_or_create(session)
            hb.last_alive_ts = datetime.now(timezone.utc)
            hb.last_source = source
            hb.tick_count = (hb.tick_count or 0) + 1
            await session.commit()
    except Exception as e:
        log.warning(f"[heartbeat] tick falhou: {e}")


async def get_health() -> dict:
    """
    Estado do heartbeat. Retorna gap em segundos, severidade e
    metadados pra alerta/painel.
    """
    if not DB_ENABLED:
        return {
            "enabled": False,
            "status": "unknown",
            "reason": "DB desabilitado — sem heartbeat persistido",
        }
    async with get_session() as session:
        hb = await _get_or_create(session)
        await session.commit()
        now = datetime.now(timezone.utc)
        last = hb.last_alive_ts
        gap = (now - last).total_seconds() if last else None

        if gap is None:
            status = "unknown"
        elif gap > HEALTH_ALERT_GAP_SEC:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "enabled": True,
            "status": status,
            "last_alive_ts": last.isoformat() if last else None,
            "gap_seconds": round(gap, 1) if gap is not None else None,
            "gap_alert_threshold": HEALTH_ALERT_GAP_SEC,
            "last_source": hb.last_source,
            "tick_count": hb.tick_count,
            "now": now.isoformat(),
        }
