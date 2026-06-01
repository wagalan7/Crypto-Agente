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
from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import whatsapp_service as wa
import calendar_service as cal

_TZ = ZoneInfo("America/Sao_Paulo")

logger = logging.getLogger(__name__)
_INTERVAL_SECONDS = 30 * 60   # checa a cada 30 minutos
_SEND_AFTER_HOUR  = 7         # janela de envio de confirmações: a partir das 7h
_SEND_BEFORE_HOUR = 22        # janela de envio: até as 22h (NÃO enviar 23h–6h59)
_FOLLOWUP_HOUR    = 7         # followup no dia da sessão a partir das 7h


_DIAS_SEMANA = {
    0: "segunda-feira",
    1: "terça-feira",
    2: "quarta-feira",
    3: "quinta-feira",
    4: "sexta-feira",
    5: "sábado",
    6: "domingo",
}


def _quando_label(appt: dict) -> str:
    """Retorna 'de amanhã', 'de hoje' ou 'de [dia da semana]' conforme a data da consulta."""
    try:
        appt_dt = datetime.fromisoformat(appt["scheduled_at"])
    except Exception:
        return "de amanhã"
    today_br = datetime.now(_TZ).replace(tzinfo=None).date()
    appt_date = appt_dt.date()
    delta_days = (appt_date - today_br).days
    if delta_days <= 0:
        return "de hoje"
    if delta_days == 1:
        return "de amanhã"
    # 2+ dias à frente → usa nome do dia da semana
    return f"de {_DIAS_SEMANA[appt_date.weekday()]}"


_DEFAULT_CONFIRMATION = (
    "Olá, {nome}! 😊 Passando para confirmar sua sessão {quando}:\n\n"
    "📅 {data_hora}\n\n"
    "Você pode confirmar presença? Responda *SIM* para confirmar "
    "ou me avise se precisar remarcar. 🙏\n\n"
    "— {psicologa}"
)

_DEFAULT_FOLLOWUP = (
    "Olá, {nome}! 😊 Sua sessão de *hoje* ainda não foi confirmada:\n\n"
    "📅 {data_hora}\n\n"
    "Por favor responda *SIM* para confirmar sua presença, ou me avise "
    "caso precise cancelar ou remarcar.\n\n"
    "⚠️ *Lembrete:* conforme combinado na primeira sessão, sessões não "
    "canceladas com antecedência e com ausência serão cobradas normalmente, "
    "pois o horário fica reservado exclusivamente para você.\n\n"
    "— {psicologa}"
)

_DEFAULT_BILLING = (
    "{nome} 🌷\n"
    "Espero que esteja tudo bem.\n\n"
    "Gostaria de informar o valor das sessões do último mês.\n\n"
    "Fechamos em R$ {total} 🩷\n\n"
    "Qualquer dúvida estou à disposição."
)


def _apply_template(template: str, **kwargs) -> str:
    """Substitui {variavel} no template. Ignora chaves não reconhecidas."""
    try:
        return template.format(**kwargs)
    except KeyError:
        # template tem variável desconhecida — aplica o que conseguir
        import re
        result = template
        for k, v in kwargs.items():
            result = result.replace("{" + k + "}", str(v))
        return result


def _first_name(patient_name: str | None) -> str:
    """Primeiro nome do paciente, ou fallback genérico se vazio."""
    parts = (patient_name or "").strip().split()
    return parts[0] if parts else "tudo bem"


def _confirmation_message(tenant: dict, appt: dict) -> str:
    tpl = (tenant.get("confirmation_msg_template") or "").strip() or _DEFAULT_CONFIRMATION
    return _apply_template(
        tpl,
        nome=_first_name(appt.get("patient_name")),
        data_hora=cal.format_appointment(appt),
        quando=_quando_label(appt),
        psicologa=tenant["psychologist_name"],
    )


def _followup_message(tenant: dict, appt: dict) -> str:
    tpl = (tenant.get("followup_msg_template") or "").strip() or _DEFAULT_FOLLOWUP
    return _apply_template(
        tpl,
        nome=_first_name(appt.get("patient_name")),
        data_hora=cal.format_appointment(appt),
        quando="de hoje",
        psicologa=tenant["psychologist_name"],
    )


async def _run_confirmations():
    now = datetime.now(_TZ)  # sempre no horário de Brasília

    # ── Trava de horário: NUNCA envia entre 22h e 7h59 ─────────────────────────
    if not (_SEND_AFTER_HOUR <= now.hour < _SEND_BEFORE_HOUR + 1):
        logger.info(f"[scheduler] Fora da janela de envio ({now.hour}h) — pulando")
        return

    tenants = db.list_tenants()
    for tenant in tenants:
        # ── Pular consultórios suspensos ───────────────────────────────────────
        if tenant.get("status") == "suspended" and not db.is_tenant_exempt(tenant):
            logger.info(f"[{tenant['slug']}] Suspenso — confirmações e followups ignorados")
            continue

        # ── Confirmações com 24h de antecedência ──────────────────────────────
        appts = db.get_appointments_for_confirmation(tenant["id"])
        for appt in appts:
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                logger.info(f"[{tenant['slug']}] ⏸ Confirmação pulada (agente pausado) → {appt['phone']}")
                continue
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
                if db.is_agent_paused(tenant["id"], appt["phone"]):
                    logger.info(f"[{tenant['slug']}] ⏸ Followup pulado (agente pausado) → {appt['phone']}")
                    continue
                msg = _followup_message(tenant, appt)
                sent = await wa.send_message(tenant, appt["phone"], msg)
                if sent:
                    db.mark_followup_sent(appt["id"])
                    logger.info(f"[{tenant['slug']}] ✓ Followup → {appt['patient_name']}")
                else:
                    logger.warning(f"[{tenant['slug']}] ✗ Falha followup → {appt['phone']}")


def _billing_message(tenant: dict, patient_name: str, total: float, sessions_count: int) -> str:
    first_name = patient_name.split()[0] if patient_name else "paciente"
    total_fmt = f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    tpl = (tenant.get("billing_msg_template") or "").strip() or _DEFAULT_BILLING
    return _apply_template(
        tpl,
        nome=first_name,
        total=total_fmt,
        sessoes=sessions_count,
        psicologa=tenant.get("psychologist_name", ""),
    )


async def _run_billing():
    """Executa no último dia do mês às 20h."""
    now = datetime.now(_TZ)
    last_day = monthrange(now.year, now.month)[1]
    if now.day != last_day or now.hour != 20:
        return

    # Mês de referência = mês atual
    month_str = now.strftime("%Y-%m")
    month_start = f"{now.year}-{now.month:02d}-01T00:00:00"
    last_day_dt = now.replace(day=last_day, hour=23, minute=59, second=59)
    month_end = last_day_dt.strftime("%Y-%m-%dT23:59:59")
    now_str = now.replace(tzinfo=None).isoformat(timespec="seconds")

    tenants = db.list_tenants()
    for tenant in tenants:
        if tenant.get("status") == "suspended" and not db.is_tenant_exempt(tenant):
            continue
        patients = db.get_patients_with_price(tenant["id"])
        for patient in patients:
            phone = patient["phone"]
            if db.billing_already_sent(tenant["id"], phone, month_str):
                logger.info(f"[{tenant['slug']}] Cobrança {month_str} já enviada para {phone}")
                continue
            if not phone:
                continue
            if db.is_agent_paused(tenant["id"], phone):
                logger.info(f"[{tenant['slug']}] ⏸ Cobrança pulada (agente pausado) → {phone}")
                continue
            sessions = db.get_valid_sessions_for_month(
                tenant["id"], phone, month_start, month_end, now_str
            )
            if not sessions:
                logger.info(f"[{tenant['slug']}] Sem sessões válidas para {patient['name']} em {month_str}")
                continue
            count = len(sessions)
            total = count * patient["session_price"]
            patient_name = sessions[0]["patient_name"] if sessions else patient.get("name", "Paciente")
            msg = _billing_message(tenant, patient_name, total, count)
            sent = await wa.send_message(tenant, phone, msg)
            if sent:
                db.save_billing_log(tenant["id"], phone, patient_name, month_str, count, total, "whatsapp")
                logger.info(f"[{tenant['slug']}] ✓ Cobrança {month_str} → {patient_name} R${total:.2f} ({count} sessões)")
            else:
                logger.warning(f"[{tenant['slug']}] ✗ Falha cobrança → {phone}")


async def run_billing_now(tenant_id: int, month_str: str | None = None) -> list[dict]:
    """Disparo manual de cobrança."""
    now = datetime.now(_TZ).replace(tzinfo=None)
    if not month_str:
        month_str = now.strftime("%Y-%m")
    year, month = int(month_str[:4]), int(month_str[5:7])
    month_start = f"{year}-{month:02d}-01T00:00:00"
    last_day = monthrange(year, month)[1]
    month_end = f"{year}-{month:02d}-{last_day:02d}T23:59:59"
    now_str = now.isoformat(timespec="seconds")
    tenant = db.get_tenant_by_id(tenant_id)
    if not tenant:
        return []
    patients = db.get_patients_with_price(tenant_id)
    results = []
    for patient in patients:
        phone = patient["phone"]
        if not phone:
            continue
        sessions = db.get_valid_sessions_for_month(tenant_id, phone, month_start, month_end, now_str)
        if not sessions:
            continue
        count = len(sessions)
        total = count * patient["session_price"]
        patient_name = sessions[0]["patient_name"] if sessions else patient.get("name", "Paciente")
        msg = _billing_message(tenant, patient_name, total, count)
        sent = await wa.send_message(tenant, phone, msg)
        if sent:
            db.save_billing_log(tenant_id, phone, patient_name, month_str, count, total, "whatsapp")
        results.append({"phone": phone, "patient_name": patient_name, "sessions": count, "total": total, "sent": sent})
    return results


async def run_confirmations_now():
    """Disparo manual (endpoint admin). Usa a mesma janela de 24h."""
    tenants = db.list_tenants()
    results = []
    for tenant in tenants:
        # Confirmações com 24h de antecedência
        appts = db.get_appointments_for_confirmation(tenant["id"])
        for appt in appts:
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "confirmation",
                                "skipped": "paused"})
                continue
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
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "followup",
                                "skipped": "paused"})
                continue
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


async def _run_backup():
    """Backup diário do SQLite às 3h da manhã, com rotação (mantém últimos 7 dias).
    Usa o BACKUP API do SQLite (consistente mesmo com escritas concorrentes)."""
    now = datetime.now(_TZ)
    if now.hour != 3:
        return
    import os, sqlite3 as _sql, shutil
    src = db.DB_PATH
    backup_dir = os.path.join(os.path.dirname(src), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    dest = os.path.join(backup_dir, f"consultorio-{stamp}.db")
    if os.path.exists(dest):
        return  # já feito hoje
    try:
        src_conn = _sql.connect(src)
        dst_conn = _sql.connect(dest)
        with dst_conn:
            src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        size_mb = os.path.getsize(dest) / (1024 * 1024)
        logger.info(f"[backup] ✓ {dest} ({size_mb:.1f} MB)")

        # Rotação: remove backups > 7 dias
        import time as _t
        cutoff = _t.time() - 7 * 86400
        for fname in os.listdir(backup_dir):
            fpath = os.path.join(backup_dir, fname)
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                logger.info(f"[backup] rotacionado: {fname}")
    except Exception as e:
        logger.exception(f"[backup] FALHOU: {e}")

    # Upload off-site para S3/R2 (idempotente — só sobe se ainda não subiu hoje)
    try:
        import backup_service
        result = backup_service.run_backup_if_due()
        if result.get("status") == "ok":
            logger.info(f"[backup-offsite] ✓ {result.get('key')}")
        elif result.get("status") == "skipped" and result.get("reason") == "not_configured":
            pass  # silencioso quando S3 não está configurado
        elif result.get("status") == "error":
            logger.warning(f"[backup-offsite] erro: {result.get('reason')}")
    except Exception as e:
        logger.warning(f"[backup-offsite] exceção: {e}")


async def _run_all():
    await _run_confirmations()
    await _run_billing()
    await _run_backup()


def _scheduler_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            loop.run_until_complete(_run_all())
        except Exception as e:
            logger.exception(f"Erro no scheduler: {e}")
        time.sleep(_INTERVAL_SECONDS)


def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="confirmation-scheduler")
    t.start()
    logger.info(f"Scheduler iniciado (intervalo: 30 min | janela de envio: {_SEND_AFTER_HOUR}h-{_SEND_BEFORE_HOUR}h | followup: {_FOLLOWUP_HOUR}h)")
