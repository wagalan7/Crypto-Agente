"""Criptografia de campos sensíveis em repouso (LGPD).

Cifra credenciais (Z-API/Twilio/Google/CalDAV), chave PIX e o conteúdo das
conversas antes de gravar no SQLite. É **transparente e retrocompatível**:

- Sem ``FIELD_ENCRYPTION_KEY`` configurada → fail-open: grava/lê em TEXTO PURO
  (comportamento idêntico ao anterior — nada quebra).
- Valores sem o prefixo ``enc:v1:`` são tratados como texto puro (dados legados
  gravados antes da criptografia). Assim a migração é gradual: leituras antigas
  continuam funcionando e novas gravações passam a ser cifradas.
- ``encrypt()`` é idempotente (não recriptografa um valor já cifrado).
- Rotação de chave: ``FIELD_ENCRYPTION_KEY`` aceita várias chaves separadas por
  vírgula (a 1ª cifra; todas decifram) via ``MultiFernet``.

Gerar uma chave:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
_fernet = None

try:
    _raw = os.getenv("FIELD_ENCRYPTION_KEY", "").strip()
    if _raw:
        from cryptography.fernet import Fernet, MultiFernet

        _keys = [k.strip() for k in _raw.split(",") if k.strip()]
        _fernets = [Fernet(k.encode()) for k in _keys]
        _fernet = MultiFernet(_fernets) if len(_fernets) > 1 else _fernets[0]
        logger.info("Criptografia de campos ATIVA (%d chave(s)).", len(_fernets))
    else:
        logger.warning(
            "FIELD_ENCRYPTION_KEY ausente — campos sensíveis gravados em TEXTO PURO."
        )
except Exception as e:  # chave inválida, cryptography ausente, etc.
    _fernet = None
    logger.error("Falha ao inicializar criptografia (%s) — usando TEXTO PURO.", e)


def is_enabled() -> bool:
    """True se há chave válida configurada (criptografia ativa)."""
    return _fernet is not None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(value):
    """Cifra uma string. Idempotente e fail-open.

    Retorna o valor inalterado se: não for string, for vazio, já estiver cifrado
    ou não houver chave configurada.
    """
    if not isinstance(value, str) or value == "":
        return value
    if value.startswith(_PREFIX):
        return value  # já cifrado
    if _fernet is None:
        return value  # fail-open: texto puro
    try:
        return _PREFIX + _fernet.encrypt(value.encode("utf-8")).decode("ascii")
    except Exception as e:
        logger.error("encrypt falhou (%s) — mantendo texto puro.", e)
        return value


def decrypt(value):
    """Decifra um valor cifrado. Valores em texto puro (legado) passam direto."""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value  # texto puro / legado / não-string
    if _fernet is None:
        logger.error("Valor cifrado encontrado, mas sem FIELD_ENCRYPTION_KEY para decifrar.")
        return value
    token = value[len(_PREFIX):]
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as e:
        logger.error("decrypt falhou (%s).", e)
        return value
