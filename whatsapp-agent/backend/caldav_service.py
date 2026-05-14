"""
CalDAV Calendar Service
========================
Integração genérica via protocolo CalDAV (RFC 4791).

Compatível com:
  - Apple iCloud Calendar
  - Outlook.com (conta pessoal Microsoft)
  - Nextcloud Calendar
  - Yahoo Calendar
  - Fastmail
  - Qualquer servidor CalDAV padrão

NÃO compatível via CalDAV:
  - Microsoft 365 (empresarial) — usa Microsoft Graph API (OAuth separado)
  - Google Calendar — use a integração OAuth dedicada

Campos necessários no tenant:
  - caldav_url      : URL base do calendário (ex: https://caldav.icloud.com/123/calendars/Consultório/)
  - caldav_username : e-mail ou usuário (ex: usuario@icloud.com)
  - caldav_password : senha de app (nunca a senha principal da conta)
"""
from __future__ import annotations
import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_TZ_BR = ZoneInfo("America/Sao_Paulo")
_CALENDAR_CONTENT_TYPE = "text/calendar; charset=utf-8"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_configured(tenant: dict) -> bool:
    return bool(
        tenant.get("caldav_url")
        and tenant.get("caldav_username")
        and tenant.get("caldav_password")
    )


def _auth_header(tenant: dict) -> str:
    creds = f"{tenant['caldav_username']}:{tenant['caldav_password']}"
    encoded = base64.b64encode(creds.encode()).decode()
    return f"Basic {encoded}"


def _format_dt(dt: datetime) -> str:
    """Formata datetime para iCal (ex: 20260515T091000)."""
    return dt.strftime("%Y%m%dT%H%M%S")


def _make_ical(uid: str, patient_name: str, start: datetime, end: datetime) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Consultório Inteligente//PT-BR\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now_utc}\r\n"
        f"DTSTART;TZID=America/Sao_Paulo:{_format_dt(start)}\r\n"
        f"DTEND;TZID=America/Sao_Paulo:{_format_dt(end)}\r\n"
        f"SUMMARY:Sessão — {patient_name}\r\n"
        "DESCRIPTION:Consulta agendada pelo Consultório Inteligente\r\n"
        "STATUS:CONFIRMED\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _event_url(tenant: dict, uid: str) -> str:
    base = tenant["caldav_url"].rstrip("/")
    return f"{base}/{uid}.ics"


# ── API pública ────────────────────────────────────────────────────────────────

def create_event(tenant: dict, patient_name: str, scheduled_at: str, duration_minutes: int) -> str | None:
    """
    Cria evento no calendário CalDAV.
    Retorna o UID do evento (usado para atualizar/deletar depois).
    """
    if not _is_configured(tenant):
        return None
    try:
        start = datetime.fromisoformat(scheduled_at)
        end   = start + timedelta(minutes=duration_minutes)
        uid   = str(uuid.uuid4())
        ical  = _make_ical(uid, patient_name, start, end)

        resp = httpx.put(
            _event_url(tenant, uid),
            content=ical.encode("utf-8"),
            headers={
                "Authorization": _auth_header(tenant),
                "Content-Type": _CALENDAR_CONTENT_TYPE,
            },
            timeout=10,
        )
        if resp.status_code in (200, 201, 204):
            logger.info(f"[caldav] Evento criado: {uid} ({patient_name} às {scheduled_at})")
            return uid
        else:
            logger.warning(f"[caldav] Falha ao criar evento: HTTP {resp.status_code} — {resp.text[:200]}")
            return None
    except Exception as e:
        logger.warning(f"[caldav] Erro ao criar evento: {e}")
        return None


def update_event(tenant: dict, uid: str, patient_name: str, scheduled_at: str, duration_minutes: int) -> bool:
    """Atualiza evento existente (PUT sobrescreve)."""
    if not _is_configured(tenant) or not uid:
        return False
    try:
        start = datetime.fromisoformat(scheduled_at)
        end   = start + timedelta(minutes=duration_minutes)
        ical  = _make_ical(uid, patient_name, start, end)

        resp = httpx.put(
            _event_url(tenant, uid),
            content=ical.encode("utf-8"),
            headers={
                "Authorization": _auth_header(tenant),
                "Content-Type": _CALENDAR_CONTENT_TYPE,
            },
            timeout=10,
        )
        ok = resp.status_code in (200, 201, 204)
        if ok:
            logger.info(f"[caldav] Evento atualizado: {uid}")
        else:
            logger.warning(f"[caldav] Falha ao atualizar: HTTP {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"[caldav] Erro ao atualizar evento: {e}")
        return False


def delete_event(tenant: dict, uid: str) -> bool:
    """Remove evento do calendário."""
    if not _is_configured(tenant) or not uid:
        return False
    try:
        resp = httpx.delete(
            _event_url(tenant, uid),
            headers={"Authorization": _auth_header(tenant)},
            timeout=10,
        )
        ok = resp.status_code in (200, 204, 404)  # 404 = já não existe, ok
        if ok:
            logger.info(f"[caldav] Evento removido: {uid}")
        else:
            logger.warning(f"[caldav] Falha ao remover: HTTP {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"[caldav] Erro ao remover evento: {e}")
        return False


def test_connection(tenant: dict) -> dict:
    """
    Testa a conexão CalDAV fazendo uma requisição OPTIONS.
    Retorna {"ok": bool, "message": str}.
    """
    if not _is_configured(tenant):
        return {"ok": False, "message": "Configuração incompleta (URL, usuário ou senha ausente)"}
    try:
        resp = httpx.request(
            "OPTIONS",
            tenant["caldav_url"],
            headers={"Authorization": _auth_header(tenant)},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return {"ok": True, "message": "Conexão bem-sucedida!"}
        elif resp.status_code == 401:
            return {"ok": False, "message": "Credenciais inválidas (usuário ou senha incorretos)"}
        else:
            return {"ok": False, "message": f"Servidor retornou HTTP {resp.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "message": "Não foi possível conectar ao servidor. Verifique a URL."}
    except Exception as e:
        return {"ok": False, "message": f"Erro: {e}"}
