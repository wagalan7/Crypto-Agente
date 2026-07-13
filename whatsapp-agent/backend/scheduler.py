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
    "Oi, {nome}! 😊 Tudo bem?\n\n"
    "Passando pra lembrar da sua sessão {quando}:\n\n"
    "📅 {data_hora}\n\n"
    "Consegue confirmar sua presença pra mim? É só responder *SIM* 💖 "
    "Se precisar remarcar, também pode me avisar sem problema.\n\n"
    "Até breve!\n"
    "— {psicologa}"
)

_DEFAULT_FOLLOWUP = (
    "Oi, {nome}! 😊 Vi aqui que a sua sessão de *hoje* ainda não foi confirmada:\n\n"
    "📅 {data_hora}\n\n"
    "Consegue confirmar sua presença? É só responder *SIM* 💖 — e se precisar "
    "cancelar ou remarcar, é só me falar, combinado?\n\n"
    "⚠️ *Lembrete:* como combinamos na primeira sessão, o horário "
    "fica reservado só pra você, então sessões não canceladas com antecedência "
    "acabam sendo cobradas normalmente.\n\n"
    "Até logo!\n"
    "— {psicologa}"
)

_DEFAULT_BILLING = (
    "Oi, {nome}! 🌷\n"
    "Espero que esteja tudo bem por aí.\n\n"
    "Passando só pra compartilhar o valor das sessões do último mês:\n\n"
    "Fechamos em R$ {total} 💖"
)

# ── Defaults genéricos multi-segmento (Track B) ──
# Usados apenas quando o tenant NÃO é psicologia. Usam "agendamento"
# (masculino, neutro) para evitar erro de concordância com o gênero de
# {servico} (que pode ser "consulta"/f, "sessão"/f, "atendimento"/m).
# O placeholder {servico} continua disponível para templates customizados.
_GENERIC_CONFIRMATION = (
    "Oi, {nome}! 😊 Tudo bem?\n\n"
    "Passando pra lembrar do seu agendamento {quando}:\n\n"
    "📅 {data_hora}\n\n"
    "Consegue confirmar sua presença pra mim? É só responder *SIM* 💖 "
    "Se precisar remarcar, também pode me avisar sem problema.\n\n"
    "Até breve!\n"
    "— {profissional}"
)

_GENERIC_FOLLOWUP = (
    "Oi, {nome}! 😊 Vi aqui que o seu agendamento de *hoje* ainda não foi confirmado:\n\n"
    "📅 {data_hora}\n\n"
    "Consegue confirmar sua presença? É só responder *SIM* 💖 — e se precisar "
    "cancelar ou remarcar, é só me falar, combinado?\n\n"
    "⚠️ *Lembrete:* como combinamos, o horário fica reservado só pra "
    "você, então horários não cancelados com antecedência acabam sendo cobrados "
    "normalmente.\n\n"
    "Até logo!\n"
    "— {profissional}"
)

_GENERIC_BILLING = (
    "Oi, {nome}! 🌷\n"
    "Espero que esteja tudo bem por aí.\n\n"
    "Passando só pra compartilhar o valor do último mês:\n\n"
    "Fechamos em R$ {total} 💖"
)


def _is_psychology(tenant: dict) -> bool:
    """True para o segmento clínico padrão (psicologia). Preserva 100% os
    textos atuais. Espelha agent._is_psychology (duplicado de propósito para
    evitar dependência de import entre scheduler e agent)."""
    return (tenant.get("segment") or "psicologia").strip().lower() in ("", "psicologia")


def _servico(tenant: dict) -> str:
    return (tenant.get("service_noun") or "").strip() or "atendimento"


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
    default = _DEFAULT_CONFIRMATION if _is_psychology(tenant) else _GENERIC_CONFIRMATION
    tpl = (tenant.get("confirmation_msg_template") or "").strip() or default
    return _apply_template(
        tpl,
        nome=_first_name(appt.get("patient_name")),
        data_hora=cal.format_appointment(appt),
        quando=_quando_label(appt),
        psicologa=tenant["psychologist_name"],
        profissional=tenant["psychologist_name"],
        servico=_servico(tenant),
    )


def _followup_message(tenant: dict, appt: dict) -> str:
    default = _DEFAULT_FOLLOWUP if _is_psychology(tenant) else _GENERIC_FOLLOWUP
    tpl = (tenant.get("followup_msg_template") or "").strip() or default
    return _apply_template(
        tpl,
        nome=_first_name(appt.get("patient_name")),
        data_hora=cal.format_appointment(appt),
        quando="de hoje",
        psicologa=tenant["psychologist_name"],
        profissional=tenant["psychologist_name"],
        servico=_servico(tenant),
    )


async def _notify_owner_failures(tenant: dict, failures: list, kind: str = "confirmação"):
    """Avisa a psicóloga, no WhatsApp DELA, quais envios falharam e o motivo REAL.
    Best-effort: se o próprio WhatsApp estiver desconectado, o aviso também não sai
    (mas o motivo continua visível no painel)."""
    try:
        psy_phone = "".join(c for c in (tenant.get("psychologist_phone") or "") if c.isdigit())
        if not psy_phone or not failures:
            return
        linhas = "\n".join(f"• {nome}: {motivo}" for nome, motivo in failures[:15])
        n = len(failures)
        msg = (
            f"⚠️ *Aviso do seu assistente*\n\n"
            f"{n} {kind}(ões) de consulta não {'foi' if n == 1 else 'foram'} enviada(s):\n\n"
            f"{linhas}\n\n"
            f"Resolva o ponto indicado acima e reenvie pelo painel em "
            f"*Enviar confirmações agora*."
        )
        await wa.send_message(tenant, psy_phone, msg)
        logger.info(f"[{tenant['slug']}] Psicóloga avisada de {n} falha(s) de {kind}")
    except Exception as e:
        logger.warning(f"[{tenant.get('slug')}] Falha ao avisar psicóloga sobre erros: {e}")


def _dedup_failures(failures: list) -> list:
    """Remove duplicatas (mesmo paciente com 2 sessões geraria 2 avisos iguais)."""
    seen, uniq = set(), []
    for nome, motivo in failures:
        if (nome, motivo) not in seen:
            seen.add((nome, motivo))
            uniq.append((nome, motivo))
    return uniq


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

        # ── Coleta o que precisa enviar (confirmações + followups) ────────────
        appts = db.get_appointments_for_confirmation(tenant["id"])
        do_followup = now.hour >= _FOLLOWUP_HOUR
        appts_hoje = db.get_appointments_today_unconfirmed(tenant["id"]) if do_followup else []

        # Motivos de falha REAIS deste consultório (para avisar a psicóloga)
        failures: list = []

        # ── Checa a conexão UMA vez se há algo a enviar ───────────────────────
        _disc = ""  # motivo pré-computado se a instância estiver desconectada
        if appts or appts_hoje:
            conn = await wa.check_connection(tenant)
            if conn.get("ok") and conn.get("connected") is False:
                _disc = ("WhatsApp desconectado — é preciso reconectar "
                         "(reler o QR Code) no painel")

        # ── Confirmações com 24h de antecedência ──────────────────────────────
        for appt in appts:
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                logger.info(f"[{tenant['slug']}] ⏸ Confirmação pulada (agente pausado) → {appt['phone']}")
                continue
            if _disc:
                failures.append((appt.get("patient_name") or appt["phone"], _disc))
                logger.warning(f"[{tenant['slug']}] ✗ Falha confirmação → {appt['phone']}: {_disc}")
                continue
            msg = _confirmation_message(tenant, appt)
            sent, reason = await wa.send_message_ex(tenant, appt["phone"], msg)
            if sent:
                db.mark_confirmation_sent(appt["id"])
                logger.info(f"[{tenant['slug']}] ✓ Confirmação 24h → {appt['patient_name']} ({appt['scheduled_at']})")
            else:
                failures.append((appt.get("patient_name") or appt["phone"], reason))
                logger.warning(f"[{tenant['slug']}] ✗ Falha confirmação → {appt['phone']}: {reason}")

        # ── Followup no dia (não confirmados) ──────────────────────────────────
        if do_followup:
            for appt in appts_hoje:
                if db.is_agent_paused(tenant["id"], appt["phone"]):
                    logger.info(f"[{tenant['slug']}] ⏸ Followup pulado (agente pausado) → {appt['phone']}")
                    continue
                if _disc:
                    failures.append((appt.get("patient_name") or appt["phone"], _disc))
                    logger.warning(f"[{tenant['slug']}] ✗ Falha followup → {appt['phone']}: {_disc}")
                    continue
                msg = _followup_message(tenant, appt)
                sent, reason = await wa.send_message_ex(tenant, appt["phone"], msg)
                if sent:
                    db.mark_followup_sent(appt["id"])
                    logger.info(f"[{tenant['slug']}] ✓ Followup → {appt['patient_name']}")
                else:
                    failures.append((appt.get("patient_name") or appt["phone"], reason))
                    logger.warning(f"[{tenant['slug']}] ✗ Falha followup → {appt['phone']}: {reason}")

        # ── Avisa a psicóloga (no WhatsApp DELA) sobre as falhas reais ─────────
        if failures:
            await _notify_owner_failures(tenant, _dedup_failures(failures))


def _billing_message(tenant: dict, patient_name: str, total: float, sessions_count: int) -> str:
    first_name = patient_name.split()[0] if patient_name else "paciente"
    total_fmt = f"{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    default = _DEFAULT_BILLING if _is_psychology(tenant) else _GENERIC_BILLING
    tpl = (tenant.get("billing_msg_template") or "").strip() or default
    return _apply_template(
        tpl,
        nome=first_name,
        total=total_fmt,
        sessoes=sessions_count,
        psicologa=tenant.get("psychologist_name", ""),
        profissional=tenant.get("psychologist_name", ""),
        servico=_servico(tenant),
    )


async def _send_override_only(tenant: dict, month_str: str, month_start: str,
                              month_end: str, now_str: str,
                              paid_phones: set, priced_variants: set) -> list[dict]:
    """Cobra pacientes que NÃO têm preço cadastrado mas tiveram um VALOR TOTAL
    do mês definido na prévia (override). Sem isso, o valor digitado na linha
    'sem valor' nunca seria disparado. Respeita pausa/pago/já-enviado e não
    duplica quem já foi tratado no loop de preço (priced_variants)."""
    results: list[dict] = []
    tid = tenant["id"]
    for ov in db.get_billing_overrides_for_month(tid, month_str):
        phone = (ov.get("phone") or "").strip()
        if not phone:
            continue
        nd = db._norm_digits(phone)
        if nd in priced_variants:
            continue  # já cobrado no loop de preço (override aplicado lá)
        if db.billing_already_sent(tid, phone, month_str):
            continue
        if nd in paid_phones:
            continue
        if db.is_agent_paused(tid, phone) or db.is_patient_billing_paused(tid, phone):
            continue
        total = float(ov.get("total_amount") or 0)
        if total <= 0:
            continue
        sessions = db.get_valid_sessions_for_month(tid, phone, month_start, month_end, now_str)
        count = len(sessions)
        patient_name = sessions[0]["patient_name"] if sessions else phone
        msg = _billing_message(tenant, patient_name, total, count)
        sent = await wa.send_message(tenant, phone, msg)
        if sent:
            db.save_billing_log(tid, phone, patient_name, month_str, count, total, "whatsapp")
            logger.info(f"[{tenant['slug']}] ✓ Cobrança {month_str} (valor do mês) → {patient_name} R${total:.2f}")
        else:
            logger.warning(f"[{tenant['slug']}] ✗ Falha cobrança (valor do mês) → {phone}")
        results.append({"phone": phone, "patient_name": patient_name, "sessions": count,
                        "total": total, "sent": sent})
    return results


def _priced_variants(patients: list[dict]) -> set:
    """Conjunto de variantes (dígitos) dos telefones dos pacientes com preço —
    para o loop de override não recobrar quem já foi tratado."""
    out: set = set()
    for patient in patients:
        ph = patient.get("phone") or ""
        if ph:
            out |= db._phone_variants(ph) | {db._norm_digits(ph)}
    return out


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
        # Pausa global de cobrança do consultório → não dispara nada
        if db.is_tenant_billing_paused(tenant["id"]):
            logger.info(f"[{tenant['slug']}] ⏸ Cobrança {month_str} pausada (global)")
            continue
        patients = db.get_patients_with_price(tenant["id"])
        # Telefones já marcados como PAGO no mês (✓ pago no painel) → não recebem
        # cobrança: se a psicóloga já recebeu e marcou, mandar mensagem seria
        # constrangedor. Conjunto normalizado (só-dígitos, tolerante a variantes).
        paid_phones = db.get_paid_phones_for_month(tenant["id"], month_str)
        # Dedup: cada agendamento é cobrado UMA vez no run, mesmo que o contato
        # exista como 2 cadastros em variantes do telefone (evita cobrança dupla).
        billed_ids: set = set()
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
            if patient.get("billing_paused") or db.is_patient_billing_paused(tenant["id"], phone):
                logger.info(f"[{tenant['slug']}] ⏸ Cobrança pulada (paciente pausado) → {phone}")
                continue
            if db._norm_digits(phone) in paid_phones:
                logger.info(f"[{tenant['slug']}] ⏸ Cobrança pulada (já marcado como pago) → {phone}")
                continue
            sessions = db.get_valid_sessions_for_month(
                tenant["id"], phone, month_start, month_end, now_str
            )
            sessions = [s for s in sessions if s.get("id") not in billed_ids]
            if not sessions:
                logger.info(f"[{tenant['slug']}] Sem sessões válidas para {patient['name']} em {month_str}")
                continue
            for s in sessions:
                if s.get("id") is not None:
                    billed_ids.add(s["id"])
            count = len(sessions)
            patient_name = sessions[0]["patient_name"] if sessions else patient.get("name", "Paciente")
            # Override manual do total (desconto/complemento combinado) substitui o cálculo
            override = db.get_billing_override(tenant["id"], phone, month_str)
            if override is not None:
                total = float(override["total_amount"])
            else:
                total = count * patient["session_price"]
            msg = _billing_message(tenant, patient_name, total, count)
            sent = await wa.send_message(tenant, phone, msg)
            if sent:
                db.save_billing_log(tenant["id"], phone, patient_name, month_str, count, total, "whatsapp")
                logger.info(f"[{tenant['slug']}] ✓ Cobrança {month_str} → {patient_name} R${total:.2f} ({count} sessões)")
            else:
                logger.warning(f"[{tenant['slug']}] ✗ Falha cobrança → {phone}")

        # Pacientes sem preço mas com VALOR TOTAL do mês definido na prévia.
        await _send_override_only(
            tenant, month_str, month_start, month_end, now_str,
            paid_phones, _priced_variants(patients),
        )

        # Cobranças avulsas (manuais) do mês — após o loop regular do tenant.
        await _send_manual_billing_entries(tenant, month_str)


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
    # Pausa global de cobrança → disparo manual também respeita
    if db.is_tenant_billing_paused(tenant_id):
        return []
    patients = db.get_patients_with_price(tenant_id)
    # Telefones já marcados como PAGO no mês → pulados também no disparo manual.
    paid_phones = db.get_paid_phones_for_month(tenant_id, month_str)
    results = []
    # Dedup: cada agendamento cobrado UMA vez (evita dupla em contato com
    # 2 cadastros em variantes do telefone).
    billed_ids: set = set()
    for patient in patients:
        phone = patient["phone"]
        if not phone:
            continue
        # Trava anti-dupla: se já foi cobrado neste mês (✓ enviado na prévia),
        # não recobra mesmo que o botão seja clicado de novo. (Avulsas têm
        # dedup próprio por sent_at e não entram nessa checagem.)
        if db.billing_already_sent(tenant_id, phone, month_str):
            continue
        # Pausa individual de cobrança do paciente
        if patient.get("billing_paused") or db.is_patient_billing_paused(tenant_id, phone):
            continue
        # Já marcado como pago no painel → não cobrar
        if db._norm_digits(phone) in paid_phones:
            continue
        sessions = db.get_valid_sessions_for_month(tenant_id, phone, month_start, month_end, now_str)
        sessions = [s for s in sessions if s.get("id") not in billed_ids]
        if not sessions:
            continue
        for s in sessions:
            if s.get("id") is not None:
                billed_ids.add(s["id"])
        count = len(sessions)
        patient_name = sessions[0]["patient_name"] if sessions else patient.get("name", "Paciente")
        override = db.get_billing_override(tenant_id, phone, month_str)
        if override is not None:
            total = float(override["total_amount"])
        else:
            total = count * patient["session_price"]
        msg = _billing_message(tenant, patient_name, total, count)
        sent = await wa.send_message(tenant, phone, msg)
        if sent:
            db.save_billing_log(tenant_id, phone, patient_name, month_str, count, total, "whatsapp")
        results.append({"phone": phone, "patient_name": patient_name, "sessions": count, "total": total, "sent": sent})

    # Pacientes sem preço mas com VALOR TOTAL do mês definido na prévia (override).
    results += await _send_override_only(
        tenant, month_str, month_start, month_end, now_str,
        paid_phones, _priced_variants(patients),
    )

    # Cobranças avulsas (manuais) do mês — sessão extra/paciente novo.
    results += await _send_manual_billing_entries(tenant, month_str)
    return results


async def _send_manual_billing_entries(tenant: dict, month_str: str) -> list[dict]:
    """Envia as cobranças AVULSAS do mês que tenham telefone e ainda não foram
    enviadas. Cada registro é enviado no máximo UMA vez (controle por sent_at na
    própria entry). Entradas sem telefone ficam só como registro (não enviam).
    Respeita pausa do agente e pausa individual de cobrança."""
    results: list[dict] = []
    tid = tenant["id"]
    for e in db.get_manual_billing_entries(tid, month_str):
        if e.get("sent_at"):
            continue  # já enviada antes
        phone = (e.get("phone") or "").strip()
        name = e.get("patient_name") or "Paciente"
        count = int(e.get("sessions_count") or 0)
        total = float(e.get("total_amount") or 0)
        if not phone:
            # Só registro (paciente de número pessoal): entra no total, não envia.
            results.append({"phone": "", "patient_name": name, "sessions": count,
                            "total": total, "sent": False, "manual": True, "skipped": "sem_telefone"})
            continue
        if db.is_agent_paused(tid, phone) or db.is_patient_billing_paused(tid, phone):
            results.append({"phone": phone, "patient_name": name, "sessions": count,
                            "total": total, "sent": False, "manual": True, "skipped": "pausado"})
            continue
        msg = _billing_message(tenant, name, total, count)
        sent = await wa.send_message(tenant, phone, msg)
        if sent:
            db.mark_manual_billing_entry_sent(tid, e["id"])
            db.save_billing_log(tid, phone, name, month_str, count, total, "avulsa")
            logger.info(f"[{tenant['slug']}] ✓ Cobrança avulsa {month_str} → {name} R${total:.2f}")
        else:
            logger.warning(f"[{tenant['slug']}] ✗ Falha cobrança avulsa → {phone}")
        results.append({"phone": phone, "patient_name": name, "sessions": count,
                        "total": total, "sent": sent, "manual": True})
    return results


async def run_confirmations_now():
    """Disparo manual (endpoint admin). Envia para TODAS as consultas de
    amanhã ainda não confirmadas — não usa a janela estrita de 23-25h, pois
    a psicóloga clica querendo cobrir o dia inteiro de amanhã."""
    tenants = db.list_tenants()
    results = []
    for tenant in tenants:
        # Confirmações: dia inteiro de amanhã (não a janela estrita 23-25h)
        appts = db.get_pending_confirmations_for_tomorrow(tenant["id"])
        appts_hoje = db.get_appointments_today_unconfirmed(tenant["id"])

        # Checa a conexão UMA vez se há algo a enviar → motivo real na falha
        _disc = ""
        if appts or appts_hoje:
            conn = await wa.check_connection(tenant)
            if conn.get("ok") and conn.get("connected") is False:
                _disc = ("WhatsApp desconectado — é preciso reconectar "
                         "(reler o QR Code) no painel")

        for appt in appts:
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "confirmation",
                                "skipped": "paused"})
                continue
            if _disc:
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "confirmation",
                                "error": _disc})
                continue
            msg = _confirmation_message(tenant, appt)
            sent, reason = await wa.send_message_ex(tenant, appt["phone"], msg)
            if sent:
                db.mark_confirmation_sent(appt["id"])
            results.append({
                "tenant": tenant["slug"],
                "patient": appt["patient_name"],
                "phone": appt["phone"],
                "sent": sent,
                "type": "confirmation",
                "error": ("" if sent else reason),
            })
        # Followup de hoje
        for appt in appts_hoje:
            if db.is_agent_paused(tenant["id"], appt["phone"]):
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "followup",
                                "skipped": "paused"})
                continue
            if _disc:
                results.append({"tenant": tenant["slug"], "patient": appt["patient_name"],
                                "phone": appt["phone"], "sent": False, "type": "followup",
                                "error": _disc})
                continue
            msg = _followup_message(tenant, appt)
            sent, reason = await wa.send_message_ex(tenant, appt["phone"], msg)
            if sent:
                db.mark_followup_sent(appt["id"])
            results.append({
                "tenant": tenant["slug"],
                "patient": appt["patient_name"],
                "phone": appt["phone"],
                "sent": sent,
                "type": "followup",
                "error": ("" if sent else reason),
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


async def _run_billing_reminders():
    """Lembretes de vencimento (contas do operador + Z-API por consultório).
    Roda 1x/dia na hora configurada (BRT). Fail-open e idempotente por design
    (o log de lembretes é chaveado por vencimento, não pelo dia do envio)."""
    import config
    now = datetime.now(_TZ)
    if now.hour != getattr(config, "BILLING_REMINDER_HOUR", 9):
        return
    try:
        import billing_reminders
        sent = await billing_reminders.run_reminders()
        if sent:
            logger.info(f"[scheduler] Lembretes de vencimento enviados: {len(sent)}")
    except Exception as e:
        logger.exception(f"[scheduler] Erro em lembretes de vencimento: {e}")


async def _run_all():
    await _run_confirmations()
    await _run_billing()
    await _run_billing_reminders()
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

    # Monitor de saúde das instâncias (thread própria, cadência mais curta).
    try:
        import instance_monitor
        instance_monitor.start_monitor()
    except Exception as e:
        logger.warning(f"Não foi possível iniciar o monitor de instâncias: {e}")
