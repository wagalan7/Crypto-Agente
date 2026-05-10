"""
Scheduler de confirmações automáticas.

Lógica:
- Roda a cada 30 minutos
- A partir das 17h: envia confirmação para TODAS as consultas de amanhã
  que ainda não receberam confirmação (confirmation_sent = 0)
- A partir das 8h no dia da sessão: envia followup + política de cobrança
  para consultas de hoje que ainda não foram confirmadas (followup_sent = 0)
- Marca os flags após envio para não duplicar
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import whatsapp_service as wa
import calendar_service as cal

_TZ = ZoneInfo("America/Sao_Paulo")

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 30 * 60   # checa a cada 30 minutos
_SEND_AFTER_HOUR  = 17        # confirmações de amanhã a partir das 17h
_FOLLOWUP_HOUR    = 8         # followup no dia da sessão a partir das 8h


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


def _followup_message(tenant: dict, appt: dict) -> str:
    formatted = cal.format_appointment(appt)
    name = appt["patient_name"].split()[0]
    return (
        f"Olá, {name}! 😊 Sua sessão de *hoje* ainda não foi confirmada:\n\n"
        f"📅 {formatted}\n\n"
        f"Por favor responda *SIM* para confirmar sua presença, ou me avise "
        f"caso precise cancelar ou remarcar.\n\n"
        f"⚠️ *Lembrete:* conforme combinado na primeira sessão, sessões não "
        f"canceladas com antecedência e com ausência serão cobradas normalmente, "
        f"pois o horário fica reservado exclusivamente para você.\n\n"
        f"— {tenant['psychologist_name']}"
    )


async def _run_confirmations():
    now = datetime.now(_TZ)  # sempre no horário de Brasília
    tenants = db.list_tenants()
    for tenant in tenants:
        # ── Confirmações com 24h de antecedência ──────────────────────────────
        # A query retorna consultas que estão entre 23h e 25h no futuro.
        # Não há restrição de horário fixo: se a sessão é às 10h, a mensagem
        # sai às 10h do dia anterior (quando o scheduler bater nessa janela).
        appts = db.get_appointments_for_confirmation(tenant["id"])
        for appt in appts:
            msg = _confirmation_message(tenant, appt)
            sent = await wa.send_message(tenant, appt["phone"], msg)
            if sent:
                db.mark_confirmation_sent(appt["id"])
                logger.info(f"[{tenant['slug']}] ✓ Confirmação 24h → {appt['patient_name']} ({appt['scheduled_at']})")
            else:
                logger.warning(f"[{tenant['slug']}] ✗ Falha confirmação → {appt['phone']}")

        # ── Followup no dia (não confirmados) ──────────────────────────────────
        if now.hour >= _FOLLOWUP_HOUR:
            appts_hoje = db.get_appointments_today_unconfirmed(tenant["id"])
            for appt in appts_hoje:
                msg = _followup_message(tenant, appt)
                sent = await wa.send_message(tenant, appt["phone"], msg)
                if sent:
                    db.mark_followup_sent(appt["id"])
                    logger.info(f"[{tenant['slug']}] ✓ Followup → {appt['patient_name']}")
                else:
                    logger.warning(f"[{tenant['slug']}] ✗ Falha followup → {appt['phone']}")


async def run_confirmations_now():
    """Disparo manual (endpoint admin). Usa a mesma janela de 24h."""
    tenants = db.list_tenants()
    results = []
    for tenant in tenants:
        # Confirmações com 24h de antecedência
        appts = db.get_appointments_for_confirmation(tenant["id"])
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
                "type": "confirmation",
            })
        # Followup de hoje
        appts_hoje = db.get_appointments_today_unconfirmed(tenant["id"])
        for appt in appts_hoje:
            msg = _followup_message(tenant, appt)
            sent = await wa.send_message(tenant, appt["phone"], msg)
            if sent:
                db.mark_followup_sent(appt["id"])
            results.append({
                "tenant": tenant["slug"],
                "patient": appt["patient_name"],
                "phone": appt["phone"],
                "sent": sent,
                "type": "followup",
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
    logger.info("Scheduler iniciado (intervalo: 30 min | confirmações: 17h | followup: 8h)")
