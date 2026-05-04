"""
WhatsApp provider abstraction — tenant-aware.
Supports: Evolution API, Twilio, Mock (for testing).
"""
from __future__ import annotations
import httpx
import logging

logger = logging.getLogger(__name__)


async def send_message(tenant: dict, phone: str, text: str) -> bool:
    provider = tenant.get("whatsapp_provider", "mock")
    if provider == "evolution":
        return await _send_evolution(tenant, phone, text)
    elif provider == "twilio":
        return await _send_twilio(tenant, phone, text)
    else:
        logger.info(f"[MOCK][{tenant['slug']}] → {phone}: {text}")
        return True


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


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


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
        if text and phone:
            return phone, text
    except Exception:
        pass
    return None


def extract_message_twilio(form: dict) -> tuple[str, str] | None:
    try:
        phone = form.get("From", "").replace("whatsapp:", "")
        text = form.get("Body", "")
        if text and phone:
            return phone, text
    except Exception:
        pass
    return None
