"""
Scheduler de confirmações automáticas (24h antes).
Verifica todos os tenants ativos a cada 30 min.
"""
from __future__ import annotations
import asyncio
import logging
import threading
from datetime import datetime, timedelta

import database as db
import whatsapp_service as wa
import calendar_service as cal

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 30 * 60


def _confirmation_message(tenant: dict, appt: dict) -> str:
    formatted = cal.format_appointment(appt)
    name = appt["patient_name"].split()[0]
    return (
        f"Olá, {name}! 😊 Passando para confirmar sua sessão:\n"
        f"📅 {formatted}\n\n"
        f"Você pode confirmar presença? Responda *SIM* para confirmar "
        f"ou me avise se precisar remarcar. 🙏\n\n"
        f"— {tenant['name']}"
    )


async def _run_confirmations():
    tenants = db.list_tenants()
    now = datetime.now()
    window_start = (now + timedelta(hours=23)).isoformat()
    window_end = (now + timedelta(hours=25)).isoformat()

    for tenant in tenants:
        appts = db.get_appointments_in_range(tenant["id"], window_start, window_end)
        pending = [a for a in appts if not a["confirmed"]]
        for appt in pending:
            msg = _confirmation_message(tenant, appt)
            sent = await wa.send_message(tenant, appt["phone"], msg)
            status = "OK" if sent else "FALHOU"
            logger.info(f"[{tenant['slug']}] Confirmação {status} → {appt['phone']} (id={appt['id']})")


def _scheduler_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(_run_confirmations())
        except Exception as e:
            logger.exception(f"Erro no scheduler: {e}")
        import time
        time.sleep(_INTERVAL_SECONDS)


def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="confirmation-scheduler")
    t.start()
    logger.info("Scheduler de confirmações iniciado (intervalo: 30 min)")
