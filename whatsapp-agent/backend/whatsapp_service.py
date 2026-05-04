"""
WhatsApp provider abstraction — tenant-aware.
Supports: Evolution API, Z-API, Twilio, Mock (for testing).
"""
from __future__ import annotations
import httpx
import logging

logger = logging.getLogger(__name__)


async def send_message(tenant: dict, phone: str, text: str) -> bool:
    provider = tenant.get("whatsapp_provider", "mock")
    if provider == "evolution":
        return await _send_evolution(tenant, phone, text)
    elif provider == "zapi":
        return await _send_zapi(tenant, phone, text)
    elif provider == "twilio":
        return await _send_twilio(tenant, phone, text)
    else:
        logger.info(f"[MOCK][{tenant['slug']}] → {phone}: {text}")
        return True


# ── Evolution API ──────────────────────────────────────────────────────────────

async def _send_evolution(tenant: dict, phone: str, text: str) -> bool:
    url = f"{tenant['evolution_url']}/message/sendText/{tenant['evolution_instance']}"
    headers = {"apikey": tenant["evolution_key"], "Content-Type": "application/json"}
    payload = {"number": _normalize_phone(phone), "textMessage": {"text": text}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"[{tenant['slug']}] Evolution error: {e}")
        return False


def extract_message_evolution(payload: dict) -> tuple[str, str] | None:
    try:
        data = payload.get("data", {})
        msg = data.get("message", {})
        text = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text")
            or ""
        )
        phone = data.get("key", {}).get("remoteJid", "").split("@")[0]
        if text and phone and not data.get("key", {}).get("fromMe"):
            return phone, text
    except Exception:
        pass
    return None


# ── Z-API ──────────────────────────────────────────────────────────────────────

async def _send_zapi(tenant: dict, phone: str, text: str) -> bool:
    """
    Z-API send-text endpoint:
    POST https://api.z-api.io/instances/{instance_id}/token/{token}/send-text
    Body: {"phone": "5511999990000", "message": "texto"}
    """
    instance_id = tenant.get("evolution_instance", "")
    token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")  # optional security header

    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    payload = {"phone": _normalize_phone(phone), "message": text}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"[{tenant['slug']}] Z-API error: {e}")
        return False


def extract_message_zapi(payload: dict) -> tuple[str, str] | None:
    """
    Z-API webhook payload (ReceivedCallback):
    {
      "phone": "5511999990000",
      "fromMe": false,
      "type": "ReceivedCallback",
      "text": {"message": "Olá"}
    }
    """
    try:
        if payload.get("fromMe"):
            return None
        if payload.get("type") not in ("ReceivedCallback", None):
            # ignore delivery receipts etc.
            if payload.get("type") and "Received" not in payload.get("type", ""):
                return None
        phone = payload.get("phone", "").replace("+", "").replace("-", "")
        text = (payload.get("text") or {}).get("message", "")
        if not text:
            # image caption fallback
            text = (payload.get("image") or {}).get("caption", "")
        if phone and text:
            return phone, text
    except Exception:
        pass
    return None


# ── Twilio ─────────────────────────────────────────────────────────────────────

async def _send_twilio(tenant: dict, phone: str, text: str) -> bool:
    from twilio.rest import Client
    client = Client(tenant["twilio_sid"], tenant["twilio_token"])
    try:
        client.messages.create(
            body=text,
            from_=tenant["twilio_from"],
            to=f"whatsapp:{_normalize_phone(phone)}",
        )
        return True
    except Exception as e:
        logger.error(f"[{tenant['slug']}] Twilio error: {e}")
        return False


def extract_message_twilio(form: dict) -> tuple[str, str] | None:
    try:
        phone = form.get("From", "").replace("whatsapp:", "")
        text = form.get("Body", "")
        if text and phone:
            return phone, text
    except Exception:
        pass
    return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits
