"""
Scheduler de confirmações automáticas.

Lógica:
- Roda a cada 30 minutos
- A partir das 18h, envia confirmação para TODAS as consultas de amanhã
  que ainda não receberam confirmação (confirmation_sent = 0)
- Marca confirmation_sent = 1 após envio para não duplicar
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
from datetime import datetime

import database as db
import whatsapp_service as wa
import calendar_service as cal

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 30 * 60   # checa a cada 30 minutos
_SEND_AFTER_HOUR  = 17        # só envia a partir das 17h


def _confirmation_message(tenant: dict, appt: dict) -> str:
    formatted = cal.format_appointment(appt)
    name = appt["patient_name"].split()[0]
    return (
        f"Olá, {name}! 😊 Passando para confirmar sua sessão de amanhã:\n\n"
        f"📅 {formatted}\n\n"
        f"Você pode confirmar presença? Responda *SIM* para confirmar "
        f"ou me avise se precisar remarcar. 🙏\n\n"
        f"— {tenant['psychologist_name']}"
    )


async def _run_confirmations():
    now = datetime.now()

    # Só envia a partir das 17h
    if now.hour < _SEND_AFTER_HOUR:
        logger.debug(f"Scheduler: ainda não são {_SEND_AFTER_HOUR}h, aguardando.")
        return

    tenants = db.list_tenants()
    for tenant in tenants:
        appts = db.get_appointments_for_tomorrow(tenant["id"])
        if not appts:
            continue
        logger.info(f"[{tenant['slug']}] {len(appts)} consulta(s) amanhã para confirmar")
        for appt in appts:
            msg = _confirmation_message(tenant, appt)
            sent = await wa.send_message(tenant, appt["phone"], msg)
            if sent:
                db.mark_confirmation_sent(appt["id"])
                logger.info(f"[{tenant['slug']}] ✓ Confirmação enviada → {appt['patient_name']} ({appt['phone']})")
            else:
                logger.warning(f"[{tenant['slug']}] ✗ Falha ao enviar para {appt['phone']}")


async def run_confirmations_now():
    """Disparo manual (endpoint admin). Ignora restrição de horário."""
    tenants = db.list_tenants()
    results = []
    for tenant in tenants:
        appts = db.get_appointments_for_tomorrow(tenant["id"])
        for appt in appts:
            msg = _confirmation_message(tenant, appt)
            sent = await wa.send_message(tenant, appt["phone"], msg)
            if sent:
                db.mark_confirmation_sent(appt["id"])
            results.append({
                "tenant": tenant["slug"],
                "patient": appt["patient_name"],
                "phone": appt["phone"],
                "sent": sent,
            })
    return results


def _scheduler_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(_run_confirmations())
        except Exception as e:
            logger.exception(f"Erro no scheduler: {e}")
        time.sleep(_INTERVAL_SECONDS)


def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="confirmation-scheduler")
    t.start()
    logger.info("Scheduler de confirmações iniciado (intervalo: 30 min, dispara a partir das 17h)")
