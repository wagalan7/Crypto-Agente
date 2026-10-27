"""Autenticação de callbacks e proteção de logs locais (não do ingresso Railway)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import logging
import re
from urllib.parse import urlsplit, urlunsplit

import config

HEADER = 'X-Webhook-Token'
# Deadline absoluto: reinício NÃO abre outra janela. Sem configuração, só headers.
_TRANSITION_MAX = timedelta(hours=48)


def query_transition_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        deadline = datetime.fromisoformat(config.WEBHOOK_QUERY_TOKEN_UNTIL.replace('Z', '+00:00'))
        if deadline.tzinfo is None:
            return False
        return now < deadline <= now + _TRANSITION_MAX
    except (ValueError, TypeError, AttributeError):
        return False


def valid_webhook_token(tenant: dict, request, rotation: dict | None = None) -> bool:
    expected = (tenant.get('webhook_token') or '').strip()
    if not expected:
        return False
    rotation = rotation or {}
    provided = []
    for name in (HEADER, 'Client-Token'):
        values = request.headers.getlist(name)
        if len(values) > 1:
            return False
        if values:
            provided.append(values[0].strip())
    query = request.query_params.getlist('token')
    if query:
        if rotation.get('header_only') or len(query) != 1 or not query_transition_open():
            return False
        provided.append(query[0].strip())
    try:
        # Todas as credenciais presentes devem conferir; sem fallback permissivo.
        candidates = [expected]
        # Credencial em preparo nunca é aceita na query, só em header.
        if not query and rotation.get('pending_token'):
            try:
                until = datetime.fromisoformat(rotation['pending_until'])
                if until.tzinfo is not None and datetime.now(timezone.utc) < until:
                    candidates.append(rotation['pending_token'])
            except (ValueError, TypeError, KeyError):
                pass
        return bool(provided) and any(
            all(v and hmac.compare_digest(v, candidate) for v in provided)
            for candidate in candidates)
    except TypeError:  # entradas não ASCII não podem derrubar o webhook
        return False


def callback_headers(url: str, token: str) -> dict:
    """Configuração Evolution 2.3.7; nunca transportar segredo em URL."""
    parts = urlsplit(url)
    if (parts.scheme not in ('https', 'http') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('URL de webhook deve ser absoluta, sem query, credencial ou fragmento')
    if not token or not token.isascii() or any(c in token for c in '\r\n'):
        raise ValueError('Token de webhook inválido')
    return {HEADER: token}


def safe_url(url: str) -> str:
    """Diagnóstico nunca devolve query, fragmento ou userinfo do provedor."""
    try:
        p = urlsplit(url)
        netloc = p.netloc.rsplit('@', 1)[-1]
        return urlunsplit((p.scheme, netloc, p.path, '[REDACTED]' if p.query else '', ''))
    except (ValueError, TypeError):
        return '[URL inválida]'


class AccessQueryRedaction(logging.Filter):
    """Remove a query do request-line do Uvicorn; o edge já viu a URL original."""
    def filter(self, record):
        if record.name == 'uvicorn.access' and isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].startswith('/webhook/'):
                args[2] = args[2].split('?', 1)[0]
            record.args = tuple(args)
        else:
            # httpx INFO inclui URLs. Reduz exposição local de query e token de Z-API.
            message = record.getMessage()
            message = re.sub(r'(https?://[^\s?\"\']+)\?[^\s\"\']+', r'\1?[REDACTED]', message)
            message = re.sub(r'(/token/)[^/\s\"\']+', r'\1[REDACTED]', message)
            record.msg, record.args = message, ()
        return True


def install_log_redaction():
    for name in ('uvicorn.access', 'httpx'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, AccessQueryRedaction) for f in logger.filters):
            logger.addFilter(AccessQueryRedaction())


def redact_sentry_event(event, hint):
    """Header de callback não integra a lista padrão de todo coletor de erros."""
    sensitive = {'x-webhook-token', 'client-token', 'authorization', 'cookie', 'apikey'}
    request = event.get('request') or {}
    headers = request.get('headers') or {}
    if isinstance(headers, dict):
        request['headers'] = {k: '[REDACTED]' if k.lower() in sensitive else v
                              for k, v in headers.items()}
    elif isinstance(headers, (list, tuple)):
        request['headers'] = [(k, '[REDACTED]' if str(k).lower() in sensitive else v)
                              for k, v in headers]
    if '/webhook/' in str(request.get('url', '')):
        request['url'] = safe_url(request['url'])
        request['query_string'] = '[REDACTED]' if request.get('query_string') else ''
        # Webhook inclui conteúdo de paciente; não anexar corpo ao evento de erro.
        request.pop('data', None)
    if request:
        event['request'] = request
    return event
