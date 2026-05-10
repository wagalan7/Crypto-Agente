from __future__ import annotations
import logging
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _is_configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _client_config(redirect_uri: str) -> dict:
    return {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def get_auth_url(slug: str, redirect_uri: str) -> str:
    """Retorna a URL de autorização OAuth2 do Google."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=slug,
        include_granted_scopes="true",
    )
    return url


def exchange_code(slug: str, code: str, redirect_uri: str) -> bool:
    """Troca o código OAuth2 pelo refresh_token e salva no banco."""
    import database as db
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    creds = flow.credentials
    if creds.refresh_token:
        db.update_tenant(slug, google_refresh_token=creds.refresh_token)
        return True
    return False


def _get_service(tenant: dict):
    """Retorna o serviço autenticado do Google Calendar ou None se não configurado."""
    if not _is_configured():
        return None
    refresh_token = tenant.get("google_refresh_token", "")
    if not refresh_token:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=config.GOOGLE_CLIENT_ID,
            client_secret=config.GOOGLE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"[gcal] Falha ao autenticar tenant {tenant.get('slug')}: {e}")
        return None


def create_event(tenant: dict, patient_name: str, scheduled_at: str, duration_minutes: int) -> str | None:
    """Cria um evento no Google Calendar e retorna o event_id."""
    service = _get_service(tenant)
    if not service:
        return None
    try:
        start = datetime.fromisoformat(scheduled_at)
        end = start + timedelta(minutes=duration_minutes)
        cal_id = tenant.get("google_calendar_id") or "primary"
        event = {
            "summary": f"Sessão — {patient_name}",
            "description": "Agendado via Agente WhatsApp",
            "start": {"dateTime": start.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "America/Sao_Paulo"},
        }
        result = service.events().insert(calendarId=cal_id, body=event).execute()
        logger.info(f"[gcal] Evento criado: {result['id']} para {patient_name}")
        return result.get("id")
    except Exception as e:
        logger.warning(f"[gcal] Erro ao criar evento: {e}")
        return None


def update_event(tenant: dict, event_id: str, patient_name: str,
                 scheduled_at: str, duration_minutes: int) -> bool:
    """Atualiza um evento existente no Google Calendar."""
    service = _get_service(tenant)
    if not service or not event_id:
        return False
    try:
        start = datetime.fromisoformat(scheduled_at)
        end = start + timedelta(minutes=duration_minutes)
        cal_id = tenant.get("google_calendar_id") or "primary"
        event = {
            "summary": f"Sessão — {patient_name}",
            "start": {"dateTime": start.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end":   {"dateTime": end.isoformat(),   "timeZone": "America/Sao_Paulo"},
        }
        service.events().update(calendarId=cal_id, eventId=event_id, body=event).execute()
        logger.info(f"[gcal] Evento atualizado: {event_id}")
        return True
    except Exception as e:
        logger.warning(f"[gcal] Erro ao atualizar evento {event_id}: {e}")
        return False


def delete_event(tenant: dict, event_id: str) -> bool:
    """Remove um evento do Google Calendar."""
    service = _get_service(tenant)
    if not service or not event_id:
        return False
    try:
        cal_id = tenant.get("google_calendar_id") or "primary"
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        logger.info(f"[gcal] Evento removido: {event_id}")
        return True
    except Exception as e:
        logger.warning(f"[gcal] Erro ao remover evento {event_id}: {e}")
        return False
