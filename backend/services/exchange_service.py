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
# cancel_algo_order só existe no cliente Binance (algo orders são feature deles).
# Se o cliente ativo for outro, expõe um stub que retorna erro.
if hasattr(_client, "cancel_algo_order"):
    cancel_algo_order = _client.cancel_algo_order
else:
    async def cancel_algo_order(algo_id: str) -> dict:  # type: ignore
        return {"ok": False, "error": f"cancel_algo_order não suportado em {ACTIVE_EXCHANGE}"}
set_leverage = _client.set_leverage
get_order_history = _client.get_order_history
get_executions = _client.get_executions
close_client = _client.close_client


# ─── Equity cache (#11.5) ─────────────────────────────────────────────────────
# Sizing precisa de equity atualizado pra dimensionar qty, mas chamar a
# exchange a cada rec é caro. Cache curto (60s default) resolve — o scan
# roda a cada minuto, mas várias recs podem cair no mesmo ciclo.

import time as _time
import asyncio as _asyncio

_EQUITY_CACHE_TTL = float(os.getenv("EXCHANGE_EQUITY_CACHE_SEC", "60"))
_equity_cache: dict = {"ts": 0.0, "data": None}
_equity_lock = _asyncio.Lock()


async def get_equity(force: bool = False) -> dict:
    """
    Retorna o equity USD atual da conta na exchange ativa, com cache de 60s.
    Shape: {
        "ok": bool,
        "total_usd": float,        # walletBalance + uPnL — usado pro sizing
        "available_usd": float,    # margem livre
        "wallet_usd": float,       # saldo nu (sem uPnL)
        "margin_used_usd": float,
        "source": "live" | "cache" | "fallback",
        "exchange": str,
        "age_sec": float | None,
    }
    Se a chamada falhar, retorna source="fallback" com total=0 + erro.
    O caller decide se usa fallback estático.
    """
    now = _time.time()
    age = now - _equity_cache["ts"]
    if not force and _equity_cache["data"] is not None and age < _EQUITY_CACHE_TTL:
        out = dict(_equity_cache["data"])
        out["source"] = "cache"
        out["age_sec"] = round(age, 1)
        return out

    async with _equity_lock:
        # Re-check após adquirir o lock (outro waiter pode ter atualizado)
        now = _time.time()
        age = now - _equity_cache["ts"]
        if not force and _equity_cache["data"] is not None and age < _EQUITY_CACHE_TTL:
            out = dict(_equity_cache["data"])
            out["source"] = "cache"
            out["age_sec"] = round(age, 1)
            return out

        try:
            res = await _client.get_wallet_balance()
        except Exception as e:
            log.warning(f"[equity] get_wallet_balance falhou: {e}")
            return {
                "ok": False,
                "total_usd": 0.0, "available_usd": 0.0, "wallet_usd": 0.0,
                "margin_used_usd": 0.0,
                "source": "fallback", "exchange": ACTIVE_EXCHANGE,
                "age_sec": None, "error": str(e),
            }

        if not res.get("ok"):
            log.warning(f"[equity] resposta não-ok: {res}")
            return {
                "ok": False,
                "total_usd": 0.0, "available_usd": 0.0, "wallet_usd": 0.0,
                "margin_used_usd": 0.0,
                "source": "fallback", "exchange": ACTIVE_EXCHANGE,
                "age_sec": None,
                "error": res.get("error") or res.get("msg"),
            }

        data = {
            "ok": True,
            "total_usd": float(res.get("equity_usd") or 0),
            "available_usd": float(res.get("available_usd") or 0),
            "wallet_usd": float(res.get("wallet_balance_usd") or 0),
            "margin_used_usd": float(res.get("margin_used_usd") or 0),
            "exchange": ACTIVE_EXCHANGE,
        }
        _equity_cache["data"] = data
        _equity_cache["ts"] = now
        out = dict(data)
        out["source"] = "live"
        out["age_sec"] = 0.0
        return out
