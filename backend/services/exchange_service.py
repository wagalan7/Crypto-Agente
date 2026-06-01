"""
Exchange dispatcher (#11) — abstrai bybit_signed_service vs binance_signed_service.

Seleção via env var EXCHANGE (default: "binance"). Permite trocar de
corretora sem mexer no resto do app — quem importa este service nunca
sabe qual implementação está rodando.

  EXCHANGE=binance  → backend/services/binance_signed_service
  EXCHANGE=bybit    → backend/services/bybit_signed_service

Re-exporta as funções do cliente escolhido com a mesma assinatura.
"""
from __future__ import annotations
import os
import logging

log = logging.getLogger(__name__)

ACTIVE_EXCHANGE = os.getenv("EXCHANGE", "binance").strip().lower()

if ACTIVE_EXCHANGE == "bybit":
    from services import bybit_signed_service as _client
elif ACTIVE_EXCHANGE == "binance":
    from services import binance_signed_service as _client
else:
    log.warning(f"[exchange] EXCHANGE={ACTIVE_EXCHANGE!r} desconhecido — caindo pra binance")
    from services import binance_signed_service as _client
    ACTIVE_EXCHANGE = "binance"


def is_configured() -> bool:
    return _client.is_configured()


def env_info() -> dict:
    info = _client.env_info()
    info["exchange"] = ACTIVE_EXCHANGE
    return info


# Re-export — mesma assinatura entre clientes
get_wallet_balance = _client.get_wallet_balance
get_positions = _client.get_positions
place_order = _client.place_order
cancel_order = _client.cancel_order
set_leverage = _client.set_leverage
get_order_history = _client.get_order_history
get_executions = _client.get_executions
close_client = _client.close_client
