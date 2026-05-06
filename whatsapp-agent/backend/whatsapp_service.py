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


def extract_selfmessage_zapi(payload: dict) -> tuple[str, str, str] | None:
    """
    Detecta mensagens enviadas PELA psicóloga (fromMe: true) via Z-API.
    Retorna (phone, text, message_id) para poder deletar a mensagem.
    Funciona com: ReceivedCallback, SentCallback, DeliveryCallback.
    """
    try:
        if not payload.get("fromMe"):
            return None
        phone = payload.get("phone", "").replace("+", "").replace("-", "")
        text = (payload.get("text") or {}).get("message", "")
        # Z-API usa zaapId ou messageId para identificar mensagens
        msg_id = (
            payload.get("zaapId")
            or payload.get("messageId")
            or payload.get("id")
            or ""
        )
        if phone and text:
            return phone, text, msg_id
    except Exception:
        pass
    return None


async def delete_message_zapi(tenant: dict, phone: str, msg_id: str) -> bool:
    """Deleta uma mensagem enviada via Z-API (para todos)."""
    if not msg_id:
        return False
    instance_id = tenant.get("evolution_instance", "")
    token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")

    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/messages/{msg_id}"
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(url, headers=headers)
            return r.status_code < 300
    except Exception as e:
        logger.warning(f"[{tenant['slug']}] Não foi possível deletar mensagem {msg_id}: {e}")
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
    Retorna (phone, text) onde text pode ser transcrito de áudio.
    """
    try:
        if payload.get("fromMe"):
            return None
        if payload.get("type") not in ("ReceivedCallback", None):
            if payload.get("type") and "Received" not in payload.get("type", ""):
                return None
        phone = payload.get("phone", "").replace("+", "").replace("-", "")
        if not phone:
            return None

        # Texto direto
        text = (payload.get("text") or {}).get("message", "")

        # Caption de imagem
        if not text:
            text = (payload.get("image") or {}).get("caption", "")

        # Áudio → retorna URL para transcrição assíncrona
        if not text:
            audio_url = (payload.get("audio") or {}).get("audioUrl", "")
            if audio_url:
                return phone, f"__AUDIO__:{audio_url}"

        if phone and text:
            return phone, text
    except Exception:
        pass
    return None


async def transcribe_audio_groq(audio_url: str) -> str | None:
    """
    Baixa o áudio do Z-API e transcreve usando Groq Whisper (gratuito).
    Retorna o texto transcrito ou None se falhar.
    """
    import config as cfg
    if not cfg.GROQ_API_KEY:
        return None
    try:
        # Baixar o arquivo de áudio
        async with httpx.AsyncClient(timeout=30) as client:
            audio_resp = await client.get(audio_url)
            audio_resp.raise_for_status()
            audio_bytes = audio_resp.content

        # Enviar para Groq Whisper
        import io
        files = {"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")}
        data = {"model": "whisper-large-v3-turbo", "language": "pt"}
        headers = {"Authorization": f"Bearer {cfg.GROQ_API_KEY}"}

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
            )
            r.raise_for_status()
            return r.json().get("text", "").strip()
    except Exception as e:
        logger.warning(f"Transcrição de áudio falhou: {e}")
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
