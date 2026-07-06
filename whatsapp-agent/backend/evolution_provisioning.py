"""Provisionamento automático de WhatsApp via Evolution API (Track B — aditivo).

Objetivo: o cliente conecta o WhatsApp lendo **só um QR code** — sem colar
Instance ID nem Token. Diferente do fluxo Z-API (que exige o cliente criar a
instância e copiar credenciais), aqui a PLATAFORMA cria a instância sozinha,
usando uma chave-mestra global (``EVOLUTION_API_KEY``) de um servidor Evolution
self-hosted (``EVOLUTION_API_URL``).

Regras de segurança / isolamento:
- **Dormant por padrão.** Sem as 2 env vars da plataforma, ``is_enabled()`` é
  False → nada muda; o fluxo Z-API manual continua sendo o único caminho.
- A chave global NUNCA é gravada no tenant nem exposta ao painel. Guardamos só o
  **token por instância** (``hash`` devolvido no create) em ``evolution_key``.
- Operações administrativas (create/connect/webhook/delete) usam a chave global
  no header ``apikey``. O envio de mensagens usa o token da instância.

Este módulo faz **apenas** as chamadas HTTP à Evolution API. A orquestração com
o banco (gravar colunas do tenant, webhook_token) fica na rota em main.py.
"""
from __future__ import annotations

import logging

import httpx

import config

logger = logging.getLogger(__name__)

_TIMEOUT = 20


def is_enabled() -> bool:
    """True só quando a plataforma tem um servidor Evolution configurado."""
    return bool(config.EVOLUTION_API_URL and config.EVOLUTION_API_KEY)


def _base() -> str:
    return config.EVOLUTION_API_URL.rstrip("/")


def _admin_headers() -> dict:
    return {"apikey": config.EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _as_data_uri(b64: str | None) -> str | None:
    """Normaliza o QR para data URI (a Evolution às vezes já manda com prefixo)."""
    if not b64:
        return None
    if b64.startswith("data:"):
        return b64
    return f"data:image/png;base64,{b64}"


async def create_instance(instance_name: str, webhook_url: str = "") -> dict:
    """Cria a instância na Evolution e já devolve o 1º QR.

    Retorna: {ok, instance_token, qr, error}
      - ok: conseguiu falar com a Evolution
      - instance_token: ``hash`` da instância (guardar em evolution_key)
      - qr: data URI do QR (pode vir vazio; nesse caso use ``connect``)
    """
    payload = {
        "instanceName": instance_name,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
    }
    # Evolution v2 aceita configurar o webhook já no create.
    if webhook_url:
        payload["webhook"] = {
            "url": webhook_url,
            "byEvents": False,
            "base64": True,
            "events": ["MESSAGES_UPSERT"],
        }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_base()}/instance/create", json=payload, headers=_admin_headers()
            )
            if r.status_code >= 400:
                # 403/409 => instância já existe: não é erro fatal, seguimos p/ connect.
                exists = r.status_code in (400, 403, 409)
                return {
                    "ok": exists,
                    "already_exists": exists,
                    "instance_token": "",
                    "qr": None,
                    "error": f"HTTP {r.status_code}: {r.text[:200]}",
                }
            data = r.json()
            token = data.get("hash")
            if isinstance(token, dict):  # algumas versões: {"apikey": "..."}
                token = token.get("apikey") or token.get("hash") or ""
            qr = None
            qrobj = data.get("qrcode") or {}
            if isinstance(qrobj, dict):
                qr = _as_data_uri(qrobj.get("base64") or qrobj.get("code"))
            return {"ok": True, "already_exists": False,
                    "instance_token": token or "", "qr": qr, "error": None}
    except Exception as e:
        logger.error(f"[evolution] create_instance({instance_name}) falhou: {e}")
        return {"ok": False, "already_exists": False,
                "instance_token": "", "qr": None, "error": str(e)[:200]}


async def connect(instance_name: str) -> dict:
    """Busca o QR atual / detecta se já conectou.

    Retorna: {ok, connected, qr, error}
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # 1) Estado da conexão
            state = None
            try:
                rs = await client.get(
                    f"{_base()}/instance/connectionState/{instance_name}",
                    headers=_admin_headers(),
                )
                if rs.status_code < 400:
                    inst = (rs.json() or {}).get("instance") or {}
                    state = inst.get("state")
            except Exception:
                pass
            if state == "open":
                return {"ok": True, "connected": True, "qr": None, "error": None}

            # 2) Pede o QR (connect)
            r = await client.get(
                f"{_base()}/instance/connect/{instance_name}",
                headers=_admin_headers(),
            )
            if r.status_code >= 400:
                return {"ok": False, "connected": False, "qr": None,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json() or {}
            # Já conectado costuma vir com {"instance":{"state":"open"}}
            inst = data.get("instance") or {}
            if inst.get("state") == "open":
                return {"ok": True, "connected": True, "qr": None, "error": None}
            qr = _as_data_uri(data.get("base64") or data.get("code")
                              or (data.get("qrcode") or {}).get("base64"))
            return {"ok": True, "connected": False, "qr": qr, "error": None}
    except Exception as e:
        logger.error(f"[evolution] connect({instance_name}) falhou: {e}")
        return {"ok": False, "connected": False, "qr": None, "error": str(e)[:200]}


async def set_webhook(instance_name: str, webhook_url: str) -> dict:
    """(Re)configura o webhook de mensagens da instância. Idempotente."""
    payload = {
        "webhook": {
            "enabled": True,
            "url": webhook_url,
            "byEvents": False,
            "base64": True,
            "events": ["MESSAGES_UPSERT"],
        }
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_base()}/webhook/set/{instance_name}",
                json=payload, headers=_admin_headers(),
            )
            if r.status_code >= 400:
                # fallback p/ formato v1 (sem o wrapper "webhook")
                r = await client.post(
                    f"{_base()}/webhook/set/{instance_name}",
                    json={"enabled": True, "url": webhook_url,
                          "events": ["MESSAGES_UPSERT"]},
                    headers=_admin_headers(),
                )
            return {"ok": r.status_code < 400,
                    "error": None if r.status_code < 400 else f"HTTP {r.status_code}"}
    except Exception as e:
        logger.warning(f"[evolution] set_webhook({instance_name}) falhou: {e}")
        return {"ok": False, "error": str(e)[:200]}


async def delete_instance(instance_name: str) -> dict:
    """Remove a instância (logout + delete). Usado em limpeza/troca de número."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                await client.delete(f"{_base()}/instance/logout/{instance_name}",
                                    headers=_admin_headers())
            except Exception:
                pass
            r = await client.delete(f"{_base()}/instance/delete/{instance_name}",
                                    headers=_admin_headers())
            return {"ok": r.status_code < 400,
                    "error": None if r.status_code < 400 else f"HTTP {r.status_code}"}
    except Exception as e:
        logger.warning(f"[evolution] delete_instance({instance_name}) falhou: {e}")
        return {"ok": False, "error": str(e)[:200]}
