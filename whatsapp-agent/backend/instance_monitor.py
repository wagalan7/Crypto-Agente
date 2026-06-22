"""
Monitor de saúde das instâncias de WhatsApp (Z-API).

Problema que resolve: quando o WhatsApp de um consultório desconecta (celular
desligado, sessão derrubada, QR expirado), o agente para de responder os
pacientes — silenciosamente. O cliente percebe tarde e fica bravo. Este monitor
detecta a queda e avisa AUTOMATICAMENTE:
  - você (operador): WhatsApp (via uma instância que ainda funcione) + e-mail;
  - a psicóloga: e-mail (o WhatsApp dela está fora do ar, não adianta tentar).

Roda em thread própria a cada N minutos (config.INSTANCE_MONITOR_INTERVAL_MIN).

Máquina de estados por consultório (tabela instance_health):
  - 'connected' = 1 (online), 0 (caiu), NULL (desconhecido — falha ao checar).
  - Falha de REDE/HTTP é ambígua → só vira "queda" após FAIL_THRESHOLD checagens
    seguidas sem sucesso (evita alarme falso por blip de rede).
  - 'connected: false' definitivo da Z-API → queda imediata.
  - Alerta 1x por episódio de queda; re-alerta a cada REALERT_HOURS se continuar.
  - Volta a ficar online → avisa o operador ("reconectado") e zera o estado.

Tudo fail-open: qualquer exceção é logada e engolida, nunca derruba a thread.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import database as db
import whatsapp_service as wa
import email_service
import billing_reminders  # reusa canais do operador (_send_operator_whatsapp/_email)

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("America/Sao_Paulo")


def _now_str() -> str:
    return datetime.now(_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        then = datetime.fromisoformat(iso)
        now = datetime.now(_TZ).replace(tzinfo=None)
        return (now - then).total_seconds() / 3600.0
    except Exception:
        return 1e9


def _is_monitorable(tenant: dict) -> bool:
    """Só monitora consultórios operando de fato com Z-API."""
    if (tenant.get("whatsapp_provider") or "").lower() != "zapi":
        return False
    if not (tenant.get("evolution_instance") and tenant.get("evolution_key")):
        return False
    # Suspenso por inadimplência (e sem isenção) já está com o bot off de propósito.
    if tenant.get("status") == "suspended" and not db.is_tenant_exempt(tenant):
        return False
    return True


# ── Alertas ────────────────────────────────────────────────────────────────────

def _br_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except Exception:
        return iso


async def _alert_down(tenant: dict, definitive: bool, down_since: str, last_error: str):
    nome = tenant.get("name") or tenant.get("slug")
    quando = _br_time(down_since)
    causa = ("desconectou" if definitive
             else "está sem resposta (possível queda / instabilidade)")
    motivo = f"\nDetalhe técnico: {last_error}" if (not definitive and last_error) else ""

    # 1) Operador — WhatsApp (best-effort, via instância que funcione) + e-mail.
    op_text = (
        f"🔴 *ALERTA — WhatsApp fora do ar*\n\n"
        f"O WhatsApp do consultório *{nome}* {causa}.\n"
        f"O agente parou de responder os pacientes.\n"
        f"Desde: *{quando}*.{motivo}\n\n"
        f"Ação: reconectar o QR code no painel da Z-API."
    )
    try:
        await billing_reminders._send_operator_whatsapp(op_text)
    except Exception as e:
        logger.warning(f"[monitor] falha WhatsApp operador: {e}")

    subj = f"🔴 WhatsApp fora do ar — {nome}"
    html = (
        f"<h2>WhatsApp do consultório fora do ar</h2>"
        f"<p>O WhatsApp do consultório <strong>{nome}</strong> {causa}. "
        f"O agente <strong>parou de responder os pacientes</strong>.</p>"
        f"<p>Desde: <strong>{quando}</strong></p>"
        + (f"<p>Detalhe técnico: {last_error}</p>" if (not definitive and last_error) else "")
        + "<p>Ação: reconectar o QR code no painel da Z-API.</p>"
    )
    try:
        billing_reminders._send_operator_email(subj, html, f"WhatsApp de {nome} fora do ar desde {quando}.")
    except Exception as e:
        logger.warning(f"[monitor] falha e-mail operador: {e}")

    # 2) Psicóloga — e-mail (o WhatsApp dela está fora; não dá pra avisar por lá).
    psy_email = (tenant.get("email") or "").strip()
    if psy_email and "@" in psy_email:
        first = (tenant.get("psychologist_name") or "").split()[0] if tenant.get("psychologist_name") else ""
        saud = f"Olá, {first}!" if first else "Olá!"
        psy_html = (
            f"<h2>{saud}</h2>"
            f"<p>Identificamos que o <strong>WhatsApp do seu consultório desconectou</strong> "
            f"e o seu agente não está conseguindo responder os pacientes no momento.</p>"
            f"<p>Para voltar a funcionar, reconecte o WhatsApp escaneando o QR code "
            f"novamente (Configurações → WhatsApp / Z-API).</p>"
            f"<p>Se precisar de ajuda, é só chamar a gente.</p>"
        )
        try:
            email_service.send_email(
                to=psy_email,
                subject="⚠️ Seu WhatsApp desconectou — agente fora do ar",
                html=email_service._base_html("WhatsApp desconectado", psy_html),
                text=f"{saud} Seu WhatsApp desconectou e o agente não está respondendo. "
                     f"Reconecte o QR code no painel.",
            )
        except Exception as e:
            logger.warning(f"[monitor] falha e-mail psicóloga: {e}")

    logger.warning(f"[monitor] 🔴 ALERTA queda enviado — {tenant['slug']} (definitive={definitive})")


async def _alert_recovered(tenant: dict, down_since: str):
    nome = tenant.get("name") or tenant.get("slug")
    txt = (
        f"🟢 *WhatsApp reconectado*\n\n"
        f"O WhatsApp do consultório *{nome}* voltou a ficar online.\n"
        f"(estava fora desde {_br_time(down_since)})"
    )
    try:
        await billing_reminders._send_operator_whatsapp(txt)
        billing_reminders._send_operator_email(
            f"🟢 WhatsApp reconectado — {nome}",
            f"<p>O WhatsApp do consultório <strong>{nome}</strong> voltou a ficar online.</p>",
            f"WhatsApp de {nome} reconectado.",
        )
    except Exception as e:
        logger.warning(f"[monitor] falha aviso recuperação: {e}")
    logger.info(f"[monitor] 🟢 {tenant['slug']} reconectado")


# ── Núcleo ─────────────────────────────────────────────────────────────────────

async def _check_tenant(tenant: dict) -> dict:
    """Checa um consultório e aplica a máquina de estados. Retorna resumo."""
    tid = tenant["id"]
    row = db.instance_health_get(tid) or {}
    prev_connected = row.get("connected")           # 1 / 0 / None
    prev_fail = int(row.get("fail_count") or 0)
    down_since = row.get("down_since")
    alerted_at = row.get("alerted_at")
    now = _now_str()

    status = await wa.get_zapi_status(tenant)
    if status["ok"] and status["connected"] is True:
        effective = "up"
    elif status["ok"] and status["connected"] is False:
        effective = "down"
    else:
        effective = "unknown"

    last_error = status.get("error", "") if effective != "up" else ""

    if effective == "up":
        if prev_connected == 0:
            await _alert_recovered(tenant, down_since)
        db.instance_health_upsert(tid, 1, 0, None, None, now, "")
        return {"slug": tenant["slug"], "state": "up"}

    if effective == "unknown":
        fail = prev_fail + 1
        if fail < config.INSTANCE_MONITOR_FAIL_THRESHOLD:
            # ainda ambíguo — não alarma, só conta
            db.instance_health_upsert(tid, prev_connected, fail, down_since, alerted_at, now, last_error)
            return {"slug": tenant["slug"], "state": "unknown", "fail_count": fail}
        # cruzou o limite → tratar como queda (não-definitiva)
        definitive = False
    else:  # effective == "down"
        definitive = True

    # ── tratamento de QUEDA (definitiva ou por limite de falhas) ────────────────
    already_down = (prev_connected == 0)
    if not down_since:
        down_since = now
    need_alert = (not already_down) or (_hours_since(alerted_at) >= config.INSTANCE_MONITOR_REALERT_HOURS)
    if need_alert:
        await _alert_down(tenant, definitive, down_since, last_error)
        alerted_at = now
    db.instance_health_upsert(tid, 0, 0, down_since, alerted_at, now, last_error)
    return {"slug": tenant["slug"], "state": "down", "definitive": definitive, "alerted": need_alert}


async def monitor_once() -> list[dict]:
    """Uma passada por todos os consultórios monitoráveis. Fail-open por tenant."""
    out = []
    for tenant in db.list_tenants():
        if not _is_monitorable(tenant):
            continue
        try:
            out.append(await _check_tenant(tenant))
        except Exception as e:
            logger.exception(f"[monitor] erro ao checar {tenant.get('slug')}: {e}")
    return out


# ── Thread ─────────────────────────────────────────────────────────────────────

def _loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    interval = max(2, config.INSTANCE_MONITOR_INTERVAL_MIN) * 60
    while True:
        try:
            res = loop.run_until_complete(monitor_once())
            down = [r for r in res if r.get("state") == "down"]
            if down:
                logger.warning(f"[monitor] {len(down)} instância(s) caída(s): {[d['slug'] for d in down]}")
        except Exception as e:
            logger.exception(f"[monitor] erro no loop: {e}")
        time.sleep(interval)


def start_monitor():
    if not config.INSTANCE_MONITOR_ENABLED:
        logger.info("[monitor] desativado (INSTANCE_MONITOR_ENABLED=0)")
        return
    t = threading.Thread(target=_loop, daemon=True, name="instance-monitor")
    t.start()
    logger.info(f"[monitor] iniciado (intervalo: {config.INSTANCE_MONITOR_INTERVAL_MIN} min)")
