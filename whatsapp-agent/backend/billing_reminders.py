"""
Lembretes proativos de vencimento.

Dois tipos de conta, com responsáveis diferentes:

1) Contas de infraestrutura do OPERADOR (você) — Railway, Anthropic, domínio, etc.
   Cadastradas manualmente na tabela `op_bills` (o app não enxerga vencimentos de
   terceiros automaticamente). Aviso vai para o WhatsApp + e-mail do operador.

2) Instância Z-API de cada CONSULTÓRIO — responsabilidade da psicóloga.
   Vencimento em `tenants.zapi_expires_at`. Aviso vai para o WhatsApp da própria
   psicóloga (pela instância dela, que ainda está ativa porque enviamos ANTES de
   vencer — evita o paradoxo de avisar por um canal já expirado).

Disparo: N dias antes do vencimento (config.BILLING_REMINDER_DAYS_BEFORE, default 5).
Idempotência: tabela `bill_reminders_log` com chave (kind, ref_id, due_date) — mesmo
rodando várias vezes ao dia, não duplica. Recorrência (monthly/yearly) é rolada
automaticamente assim que o vencimento passa.

Tudo fail-open: qualquer exceção é logada e engolida, nunca derruba o scheduler.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
import database as db
import whatsapp_service as wa
import email_service

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("America/Sao_Paulo")


# ── Datas ─────────────────────────────────────────────────────────────────────

def _today() -> date:
    return datetime.now(_TZ).date()


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _next_occurrence(due: date, recurrence: str, today: date) -> date:
    """Avança o vencimento para a próxima ocorrência futura (> hoje)."""
    if recurrence == "monthly":
        while due <= today:
            due = _add_months(due, 1)
    elif recurrence == "yearly":
        while due <= today:
            due = date(due.year + 1, due.month, min(due.day, monthrange(due.year + 1, due.month)[1]))
    return due


def _br_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _fmt_amount(amount: float) -> str:
    return f"{float(amount or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ── Canal de WhatsApp do operador ─────────────────────────────────────────────

def _operator_whatsapp_tenant() -> dict | None:
    """Escolhe um consultório com Z-API ativo para enviar o WhatsApp ao operador."""
    slug = (config.OPERATOR_WHATSAPP_TENANT_SLUG or "").strip()
    if slug:
        t = db.get_tenant(slug)
        if t:
            return t
    for t in db.list_tenants():
        prov = (t.get("whatsapp_provider") or "").lower()
        if prov in ("zapi", "evolution") and (t.get("evolution_instance") or t.get("evolution_url")):
            return t
    return None


async def _send_operator_whatsapp(text: str) -> bool:
    phone = (config.OPERATOR_PHONE or "").strip()
    if not phone:
        return False
    tenant = _operator_whatsapp_tenant()
    if not tenant:
        logger.warning("[billing-reminder] Sem consultório com Z-API ativo para avisar o operador")
        return False
    try:
        return await wa.send_message(tenant, phone, text)
    except Exception as e:
        logger.warning(f"[billing-reminder] Falha WhatsApp operador: {e}")
        return False


def _send_operator_email(subject: str, html: str, text: str) -> bool:
    to = (config.OPERATOR_EMAIL or "").strip()
    if not to:
        return False
    try:
        return email_service.send_email(to=to, subject=subject, html=html, text=text)
    except Exception as e:
        logger.warning(f"[billing-reminder] Falha e-mail operador: {e}")
        return False


# ── Mensagens ─────────────────────────────────────────────────────────────────

def _op_bill_text(bill: dict, due: date, dias: int) -> str:
    valor = f" no valor de *R$ {_fmt_amount(bill['amount'])}*" if bill.get("amount") else ""
    quando = "hoje" if dias == 0 else ("amanhã" if dias == 1 else f"em {dias} dias")
    linha_obs = f"\n📝 {bill['notes']}" if bill.get("notes") else ""
    return (
        f"⏰ *Lembrete de vencimento*\n\n"
        f"A conta *{bill['label']}*{valor} vence {quando} "
        f"(*{_br_date(due)}*).{linha_obs}\n\n"
        f"Garanta o pagamento para o app continuar no ar. 🚀"
    )


def _op_bill_email(bill: dict, due: date, dias: int) -> tuple[str, str, str]:
    valor = f"R$ {_fmt_amount(bill['amount'])}" if bill.get("amount") else "—"
    quando = "hoje" if dias == 0 else ("amanhã" if dias == 1 else f"em {dias} dias")
    subject = f"⏰ Vencimento {bill['label']} {quando} ({_br_date(due)})"
    html = (
        f"<h2>Lembrete de vencimento</h2>"
        f"<p>A conta <strong>{bill['label']}</strong> vence <strong>{quando}</strong> "
        f"(<strong>{_br_date(due)}</strong>).</p>"
        f"<p>Valor: <strong>{valor}</strong></p>"
        + (f"<p>Observação: {bill['notes']}</p>" if bill.get("notes") else "")
        + "<p>Garanta o pagamento para o app continuar no ar.</p>"
    )
    text = f"Vencimento {bill['label']} {quando} ({_br_date(due)}). Valor: {valor}."
    return subject, html, text


def _zapi_text(tenant: dict, due: date, dias: int) -> str:
    nome = (tenant.get("psychologist_name") or "").split()[0] if tenant.get("psychologist_name") else ""
    saud = f"Olá, {nome}! 😊\n\n" if nome else "Olá! 😊\n\n"
    quando = "hoje" if dias == 0 else ("amanhã" if dias == 1 else f"em {dias} dias")
    return (
        f"{saud}"
        f"⏰ Passando para lembrar que a *assinatura do seu WhatsApp (Z-API)* "
        f"vence {quando} (*{_br_date(due)}*).\n\n"
        f"Para o seu agente continuar respondendo aos pacientes sem interrupção, "
        f"renove a assinatura antes do vencimento. 🌸"
    )


# ── Varreduras ────────────────────────────────────────────────────────────────

async def _scan_op_bills(today: date, window: int) -> list[dict]:
    results = []
    for bill in db.op_bills_list(only_active=True):
        due = _parse_date(bill.get("due_date"))
        if not due:
            continue
        recurrence = bill.get("recurrence") or "monthly"

        # Recorrência: se já passou, rola para a próxima ocorrência futura.
        if due < today and recurrence in ("monthly", "yearly"):
            due = _next_occurrence(due, recurrence, today)
            if due.isoformat() != (bill.get("due_date") or "")[:10]:
                db.op_bill_update(bill["id"], due_date=due.isoformat())

        dias = (due - today).days
        if 0 <= dias <= window:
            if db.bill_reminder_already_sent("op_bill", bill["id"], due.isoformat()):
                continue
            wa_ok = await _send_operator_whatsapp(_op_bill_text(bill, due, dias))
            subj, html, txt = _op_bill_email(bill, due, dias)
            mail_ok = _send_operator_email(subj, html, txt)
            if wa_ok or mail_ok:
                ch = ("whatsapp" if wa_ok else "") + ("+email" if mail_ok else "")
                db.mark_bill_reminder_sent("op_bill", bill["id"], due.isoformat(), ch.strip("+"))
                logger.info(f"[billing-reminder] ✓ op_bill '{bill['label']}' venc {due} (wa={wa_ok} mail={mail_ok})")
                results.append({"kind": "op_bill", "label": bill["label"], "due": due.isoformat(),
                                "whatsapp": wa_ok, "email": mail_ok})
            else:
                logger.warning(f"[billing-reminder] ✗ op_bill '{bill['label']}' — nenhum canal enviou")
    return results


async def _scan_zapi(today: date, window: int) -> list[dict]:
    results = []
    for tenant in db.list_tenants():
        due = _parse_date(tenant.get("zapi_expires_at"))
        if not due:
            continue

        # Z-API costuma ser mensal — rola se já passou.
        if due < today:
            due = _next_occurrence(due, "monthly", today)
            if due.isoformat() != (tenant.get("zapi_expires_at") or "")[:10]:
                db.update_tenant(tenant["slug"], zapi_expires_at=due.isoformat())

        dias = (due - today).days
        if 0 <= dias <= window:
            if db.bill_reminder_already_sent("zapi", tenant["id"], due.isoformat()):
                continue
            phone = (tenant.get("psychologist_phone") or "").strip()
            if not phone:
                logger.warning(f"[billing-reminder] {tenant['slug']}: zapi vence {due} mas sem psychologist_phone")
                continue
            try:
                wa_ok = await wa.send_message(tenant, phone, _zapi_text(tenant, due, dias))
            except Exception as e:
                logger.warning(f"[billing-reminder] {tenant['slug']}: falha WhatsApp Z-API: {e}")
                wa_ok = False
            if wa_ok:
                db.mark_bill_reminder_sent("zapi", tenant["id"], due.isoformat(), "whatsapp")
                logger.info(f"[billing-reminder] ✓ zapi {tenant['slug']} venc {due}")
                results.append({"kind": "zapi", "tenant": tenant["slug"], "due": due.isoformat(), "whatsapp": True})
    return results


async def run_reminders(force: bool = False) -> list[dict]:
    """Varre vencimentos e dispara lembretes. force=True ignora o gate de horário
    (usado pelo endpoint admin de teste)."""
    window = max(0, int(config.BILLING_REMINDER_DAYS_BEFORE))
    today = _today()
    out = []
    try:
        out += await _scan_op_bills(today, window)
    except Exception as e:
        logger.exception(f"[billing-reminder] erro em op_bills: {e}")
    try:
        out += await _scan_zapi(today, window)
    except Exception as e:
        logger.exception(f"[billing-reminder] erro em zapi: {e}")
    return out
