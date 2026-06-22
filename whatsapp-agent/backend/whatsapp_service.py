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


async def get_zapi_status(tenant: dict) -> dict:
    """
    Consulta o status da instância Z-API.
    GET https://api.z-api.io/instances/{instance}/token/{token}/status

    Retorna sempre um dict:
      {"ok": True,  "connected": True/False, "raw": {...}}   — resposta válida da Z-API
      {"ok": False, "connected": None, "error": "..."}        — falha de rede/HTTP/credencial

    'ok' indica se conseguimos falar com a Z-API (não confundir com 'connected',
    que é se o WhatsApp do celular está pareado/online).
    """
    instance_id = tenant.get("evolution_instance", "")
    token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")
    if not instance_id or not token:
        return {"ok": False, "connected": None, "error": "credenciais ausentes"}

    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/status"
    headers = {}
    if client_token:
        headers["Client-Token"] = client_token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                return {"ok": False, "connected": None, "error": f"HTTP {r.status_code}", "raw": r.text[:200]}
            data = r.json()
            # Z-API: {"connected": true, "error": null, "smartphoneConnected": true}
            connected = bool(data.get("connected"))
            return {"ok": True, "connected": connected, "raw": data}
    except Exception as e:
        return {"ok": False, "connected": None, "error": str(e)[:200]}


async def get_zapi_qr(tenant: dict) -> dict:
    """
    Retorna o QR code para parear o WhatsApp da instância Z-API — pra exibir
    direto no painel (onboarding self-serve, sem mandar o cliente pro site da Z-API).

    Retorna sempre um dict:
      {"ok": True, "connected": True,  "qr": None}                 — já pareado, não precisa de QR
      {"ok": True, "connected": False, "qr": "data:image/png;..."} — escaneie este QR
      {"ok": False, "connected": None/False, "error": "..."}       — falha/credencial

    GET https://api.z-api.io/instances/{id}/token/{token}/qr-code/image
    A Z-API responde ora como JSON ({"value":"data:image/png;base64,..."}),
    ora como bytes de imagem — tratamos os dois.
    """
    import base64

    instance_id = tenant.get("evolution_instance", "")
    token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")
    if not instance_id or not token:
        return {"ok": False, "connected": None, "error": "credenciais ausentes"}

    # Se já está conectado, não há QR a mostrar.
    st = await get_zapi_status(tenant)
    if st.get("ok") and st.get("connected"):
        return {"ok": True, "connected": True, "qr": None}

    url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/qr-code/image"
    headers = {}
    if client_token:
        headers["Client-Token"] = client_token
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers)
            if r.status_code >= 400:
                return {"ok": False, "connected": False, "error": f"HTTP {r.status_code}", "raw": r.text[:200]}
            ctype = (r.headers.get("content-type") or "").lower()
            if "application/json" in ctype:
                data = r.json()
                if data.get("connected"):
                    return {"ok": True, "connected": True, "qr": None}
                val = data.get("value") or data.get("qrcode") or data.get("qrCode") or ""
                if not val:
                    return {"ok": True, "connected": False, "qr": None, "error": data.get("error") or "QR indisponível"}
                qr = val if str(val).startswith("data:") else f"data:image/png;base64,{val}"
                return {"ok": True, "connected": False, "qr": qr}
            # bytes de imagem
            b64 = base64.b64encode(r.content).decode()
            return {"ok": True, "connected": False, "qr": f"data:{ctype or 'image/png'};base64,{b64}"}
    except Exception as e:
        return {"ok": False, "connected": None, "error": str(e)[:200]}


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


async def configure_webhook_zapi(tenant: dict, webhook_url: str) -> dict:
    """
    Configura o webhook de mensagens recebidas no Z-API automaticamente.
    Retorna {"ok": True} ou {"ok": False, "error": "..."}
    """
    instance_id = tenant.get("evolution_instance", "")
    token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")

    if not instance_id or not token:
        return {"ok": False, "error": "Instance ID ou Token não configurados"}

    base = f"https://api.z-api.io/instances/{instance_id}/token/{token}"
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    errors = []
    # Z-API tem endpoints separados para cada tipo de webhook
    endpoints = [
        ("update-webhook-received",  webhook_url),  # mensagens recebidas
        ("update-webhook-delivery",  webhook_url),  # confirmação de entrega (opcional)
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        for ep, url_val in endpoints:
            try:
                r = await client.put(
                    f"{base}/{ep}",
                    json={"value": url_val},
                    headers=headers,
                )
                if r.status_code >= 400:
                    errors.append(f"{ep}: HTTP {r.status_code} — {r.text[:120]}")
            except Exception as e:
                errors.append(f"{ep}: {e}")

    if errors:
        logger.warning(f"[zapi-webhook] Erros ao configurar: {errors}")
        return {"ok": False, "error": "; ".join(errors)}

    logger.info(f"[zapi-webhook] Webhook configurado → {webhook_url}")
    return {"ok": True}


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
    # Tipos de mensagem que o Z-API pode enviar para mídias recebidas
    _MEDIA_TYPES = {
        "documentmessage", "imagemessage", "audiomessage",
        "document", "image", "audio", "file", "pdf",
    }

    try:
        if payload.get("fromMe"):
            return None
        _t = (payload.get("type") or "").lower()
        if _t and _t not in ("receivedcallback",) and "received" not in _t:
            # Permite passar tipos de mídia mesmo sem "Received" no nome
            if _t not in _MEDIA_TYPES and "pix" not in _t and "payment" not in _t:
                return None
        phone = payload.get("phone", "").replace("+", "").replace("-", "")
        if not phone:
            return None

        # Comprovante PIX nativo do WhatsApp (Z-API pode entregar como tipo
        # específico "pix", "paymentMessage", "pixMessage", etc., ou com payload
        # que contém um objeto pix/payment).
        if (
            "pix" in _t or "payment" in _t
            or payload.get("pix") or payload.get("payment")
            or payload.get("pixMessage") or payload.get("paymentMessage")
        ):
            logger.info(f"[ZAPI] Comprovante PIX nativo detectado de {phone} (type={_t})")
            return phone, "__COMPROVANTE_PIX__"

        # Texto direto
        text = (payload.get("text") or {}).get("message", "")

        # Heurística: às vezes o comprovante chega como texto contendo dados da transação
        if text:
            _low = text.lower()
            _markers = ("chave pix", "id da transação", "id da transacao",
                        "autenticação", "autenticacao", "comprovante de",
                        "transferência realizada", "transferencia realizada")
            if sum(1 for m in _markers if m in _low) >= 2:
                logger.info(f"[ZAPI] Comprovante detectado por texto de {phone}")
                return phone, "__COMPROVANTE_PIX__"

        # Imagem → captura URL para análise por visão computacional
        if not text:
            image = payload.get("image") or {}
            if image:
                caption = image.get("caption", "").strip()
                if caption:
                    text = caption
                else:
                    img_url = (
                        image.get("imageUrl") or image.get("url")
                        or image.get("link") or image.get("mediaUrl") or ""
                    )
                    text = f"__IMAGEM__:{img_url}" if img_url else "__IMAGEM__"
                logger.info(f"[ZAPI] Imagem recebida de {phone} — caption={caption!r}")

        # Documento / PDF → tratado como provável comprovante (extensão indica intenção)
        if not text:
            doc = (
                payload.get("document")
                or payload.get("file")
                or payload.get("pdf")
                or {}
            )
            if doc:
                caption = (doc.get("caption") or doc.get("fileName") or "").strip()
                text = caption if caption else "__DOCUMENTO__"
                logger.info(f"[ZAPI] Documento recebido de {phone} — caption={caption!r}")

        # Qualquer outro tipo de mídia não reconhecida
        if not text and _t in _MEDIA_TYPES:
            text = "__IMAGEM__"
            logger.info(f"[ZAPI] Mídia genérica de {phone} — type={payload.get('type')} keys={list(payload.keys())}")

        # Áudio → tenta vários campos que o Z-API pode usar
        if not text:
            audio = payload.get("audio") or {}
            audio_url = (
                audio.get("audioUrl")
                or audio.get("url")
                or audio.get("link")
                or audio.get("base64")
                or ""
            )
            if audio_url:
                logger.info(f"[ZAPI] Áudio detectado para {phone}: {audio_url[:60]}")
                return phone, f"__AUDIO__:{audio_url}"
            logger.info(f"[ZAPI] Payload sem texto/áudio/imagem para {phone}: keys={list(payload.keys())}")

        if phone and text:
            return phone, text
    except Exception as e:
        logger.warning(f"[ZAPI] Erro ao extrair mensagem: {e}")
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
