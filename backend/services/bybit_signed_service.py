"""
Bybit V5 — cliente ASSINADO (HMAC-SHA256) pra endpoints privados (#11.1).

Cobre o mínimo pra trading: wallet balance, positions, place/cancel order,
order history. Modo testnet por padrão (BYBIT_TESTNET=true) — só vai pra
produção real quando explicitamente desativado.

Auth: HMAC-SHA256 sobre `timestamp + api_key + recv_window + payload`
(query string pra GET, body JSON pra POST). Headers:
  X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-RECV-WINDOW, X-BAPI-SIGN.

Refs: https://bybit-exchange.github.io/docs/v5/intro

Env vars:
  BYBIT_API_KEY        — API key gerada em api.bybit.com (ou testnet)
  BYBIT_API_SECRET     — secret correspondente
  BYBIT_TESTNET        — "true" usa api-testnet.bybit.com (default), "false" real
  BYBIT_RECV_WINDOW    — janela em ms (default 5000)

Se as keys não estiverem definidas, todas as funções retornam erro estruturado
ao invés de crashar — assim o resto do app continua funcionando sem trading.
"""
from __future__ import annotations
import hmac
import hashlib
import os
import time
import json
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
_TESTNET = os.getenv("BYBIT_TESTNET", "true").strip().lower() in ("1", "true", "yes")
_RECV_WINDOW = os.getenv("BYBIT_RECV_WINDOW", "5000").strip()

BASE = "https://api-testnet.bybit.com" if _TESTNET else "https://api.bybit.com"

_http_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    return bool(_API_KEY and _API_SECRET)


def env_info() -> dict:
    """Diagnóstico — não vaza secrets."""
    return {
        "configured": is_configured(),
        "testnet": _TESTNET,
        "base_url": BASE,
        "key_prefix": _API_KEY[:4] + "..." if _API_KEY else None,
        "recv_window_ms": _RECV_WINDOW,
    }


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0)
    return _http_client


def _sign(ts: str, payload: str) -> str:
    """Bybit V5 signature: HMAC-SHA256(secret, ts + key + recv + payload)."""
    raw = f"{ts}{_API_KEY}{_RECV_WINDOW}{payload}"
    return hmac.new(
        _API_SECRET.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers(ts: str, sign: str) -> dict:
    return {
        "X-BAPI-API-KEY": _API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json",
    }


async def _signed_get(path: str, params: Optional[dict] = None) -> dict:
    if not is_configured():
        return {"ok": False, "error": "BYBIT_API_KEY/SECRET não configurados"}
    ts = str(int(time.time() * 1000))
    # GET payload = query string ordenada (sem leading ?)
    qs = ""
    if params:
        # Bybit aceita não-ordenado, mas convencionamos ordenar pra debug determinístico
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
    sign = _sign(ts, qs)
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    try:
        r = await _get_client().get(url, headers=_headers(ts, sign))
        try:
            data = r.json()
        except Exception:
            log.warning(f"[bybit] GET {path} resposta não-JSON status={r.status_code} body={r.text[:300]}")
            return {"ok": False, "error": f"resposta não-JSON ({r.status_code}): {r.text[:200]}"}
        ret_code = data.get("retCode")
        if ret_code != 0:
            log.warning(f"[bybit] {path} retCode={ret_code} msg={data.get('retMsg')}")
            return {"ok": False, "code": ret_code, "msg": data.get("retMsg"), "raw": data}
        return {"ok": True, "result": data.get("result"), "raw": data}
    except Exception as e:
        log.exception(f"[bybit] GET {path} falhou")
        return {"ok": False, "error": str(e)}


async def _signed_post(path: str, body: dict) -> dict:
    if not is_configured():
        return {"ok": False, "error": "BYBIT_API_KEY/SECRET não configurados"}
    ts = str(int(time.time() * 1000))
    payload = json.dumps(body, separators=(",", ":"), sort_keys=False)
    sign = _sign(ts, payload)
    url = f"{BASE}{path}"
    try:
        r = await _get_client().post(url, headers=_headers(ts, sign), content=payload)
        try:
            data = r.json()
        except Exception:
            log.warning(f"[bybit] POST {path} resposta não-JSON status={r.status_code} body={r.text[:300]}")
            return {"ok": False, "error": f"resposta não-JSON ({r.status_code}): {r.text[:200]}"}
        ret_code = data.get("retCode")
        if ret_code != 0:
            log.warning(f"[bybit] POST {path} retCode={ret_code} msg={data.get('retMsg')}")
            return {"ok": False, "code": ret_code, "msg": data.get("retMsg"), "raw": data}
        return {"ok": True, "result": data.get("result"), "raw": data}
    except Exception as e:
        log.exception(f"[bybit] POST {path} falhou")
        return {"ok": False, "error": str(e)}


# ─── High-level endpoints ──────────────────────────────────────────────────────


async def get_wallet_balance(account_type: str = "UNIFIED") -> dict:
    """
    Saldo da carteira. Bybit V5 unified margin (default).
    Retorna {ok, equity_usd, available_usd, raw}.
    """
    res = await _signed_get("/v5/account/wallet-balance", {"accountType": account_type})
    if not res.get("ok"):
        return res
    accounts = (res["result"] or {}).get("list", [])
    if not accounts:
        return {"ok": True, "equity_usd": 0, "available_usd": 0, "coins": [], "raw": res["raw"]}
    acc = accounts[0]
    return {
        "ok": True,
        "equity_usd": float(acc.get("totalEquity") or 0),
        "available_usd": float(acc.get("totalAvailableBalance") or 0),
        "wallet_balance_usd": float(acc.get("totalWalletBalance") or 0),
        "margin_used_usd": float(acc.get("totalInitialMargin") or 0),
        "coins": [
            {
                "coin": c.get("coin"),
                "balance": float(c.get("walletBalance") or 0),
                "equity": float(c.get("equity") or 0),
                "usd_value": float(c.get("usdValue") or 0),
            }
            for c in (acc.get("coin") or [])
            if float(c.get("walletBalance") or 0) > 0
        ],
        "testnet": _TESTNET,
    }


async def get_positions(symbol: Optional[str] = None) -> dict:
    """
    Posições abertas em linear (perp USDT). Se symbol=None, retorna todas.
    """
    params = {"category": "linear", "settleCoin": "USDT"}
    if symbol:
        # symbol em formato Bybit (BTCUSDT). Aceita formato interno (BTC/USDT:USDT) também.
        from services.bybit_service import to_bybit
        params["symbol"] = to_bybit(symbol) if "/" in symbol else symbol
    res = await _signed_get("/v5/position/list", params)
    if not res.get("ok"):
        return res
    rows = (res["result"] or {}).get("list", [])
    positions = []
    for p in rows:
        size = float(p.get("size") or 0)
        if size <= 0:
            continue
        positions.append({
            "symbol": p.get("symbol"),
            "side": p.get("side"),  # "Buy" | "Sell"
            "size": size,
            "entry_price": float(p.get("avgPrice") or 0),
            "mark_price": float(p.get("markPrice") or 0),
            "unrealized_pnl": float(p.get("unrealisedPnl") or 0),
            "leverage": float(p.get("leverage") or 0),
            "position_value": float(p.get("positionValue") or 0),
            "take_profit": float(p.get("takeProfit") or 0) or None,
            "stop_loss": float(p.get("stopLoss") or 0) or None,
        })
    return {"ok": True, "positions": positions, "count": len(positions), "testnet": _TESTNET}


async def place_order(
    symbol: str,
    side: str,           # "Buy" | "Sell"
    qty: float,
    order_type: str = "Market",  # "Market" | "Limit"
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    reduce_only: bool = False,
    leverage: Optional[int] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    """
    Cria ordem em linear (perp USDT).
    Side: "Buy" (long open / short close) | "Sell" (short open / long close).
    Se leverage informado, ajusta antes da ordem (set-leverage é endpoint separado).
    """
    from services.bybit_service import to_bybit
    sym = to_bybit(symbol) if "/" in symbol else symbol

    if leverage is not None:
        await set_leverage(sym, leverage)

    body = {
        "category": "linear",
        "symbol": sym,
        "side": side,
        "orderType": order_type,
        "qty": str(qty),
        "timeInForce": "IOC" if order_type == "Market" else "GTC",
    }
    if order_type == "Limit" and price is not None:
        body["price"] = str(price)
    if stop_loss is not None:
        body["stopLoss"] = str(stop_loss)
    if take_profit is not None:
        body["takeProfit"] = str(take_profit)
    if reduce_only:
        body["reduceOnly"] = True
    if client_order_id:
        body["orderLinkId"] = client_order_id

    return await _signed_post("/v5/order/create", body)


async def cancel_order(symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> dict:
    from services.bybit_service import to_bybit
    sym = to_bybit(symbol) if "/" in symbol else symbol
    body = {"category": "linear", "symbol": sym}
    if order_id:
        body["orderId"] = order_id
    elif client_order_id:
        body["orderLinkId"] = client_order_id
    else:
        return {"ok": False, "error": "informe order_id ou client_order_id"}
    return await _signed_post("/v5/order/cancel", body)


async def set_leverage(symbol: str, leverage: int) -> dict:
    """
    Define alavancagem. Bybit V5 separa buy/sell leverage — usamos o mesmo valor.
    """
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    res = await _signed_post("/v5/position/set-leverage", body)
    # retCode 110043 = "leverage not modified" (já estava nesse valor) — não é erro
    if not res.get("ok") and res.get("code") == 110043:
        return {"ok": True, "noop": True}
    return res


async def get_order_history(symbol: Optional[str] = None, limit: int = 50) -> dict:
    params = {"category": "linear", "limit": limit}
    if symbol:
        from services.bybit_service import to_bybit
        params["symbol"] = to_bybit(symbol) if "/" in symbol else symbol
    res = await _signed_get("/v5/order/history", params)
    if not res.get("ok"):
        return res
    rows = (res["result"] or {}).get("list", [])
    orders = [
        {
            "order_id": o.get("orderId"),
            "client_order_id": o.get("orderLinkId"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "order_type": o.get("orderType"),
            "qty": float(o.get("qty") or 0),
            "price": float(o.get("price") or 0),
            "avg_fill_price": float(o.get("avgPrice") or 0),
            "status": o.get("orderStatus"),
            "created_at": o.get("createdTime"),
            "updated_at": o.get("updatedTime"),
        }
        for o in rows
    ]
    return {"ok": True, "orders": orders, "count": len(orders)}


async def get_executions(symbol: Optional[str] = None, limit: int = 50) -> dict:
    """Fills executados (útil pra calcular slippage real)."""
    params = {"category": "linear", "limit": limit}
    if symbol:
        from services.bybit_service import to_bybit
        params["symbol"] = to_bybit(symbol) if "/" in symbol else symbol
    res = await _signed_get("/v5/execution/list", params)
    if not res.get("ok"):
        return res
    rows = (res["result"] or {}).get("list", [])
    fills = [
        {
            "exec_id": e.get("execId"),
            "order_id": e.get("orderId"),
            "symbol": e.get("symbol"),
            "side": e.get("side"),
            "qty": float(e.get("execQty") or 0),
            "price": float(e.get("execPrice") or 0),
            "fee": float(e.get("execFee") or 0),
            "is_maker": e.get("isMaker"),
            "time": e.get("execTime"),
        }
        for e in rows
    ]
    return {"ok": True, "fills": fills, "count": len(fills)}


async def _try_endpoint(base: str, label: str) -> dict:
    """Tenta query-api num endpoint específico — usado pra confirmar qual
    sistema da Bybit reconhece a key (testnet vs demo)."""
    ts = str(int(time.time() * 1000))
    sign = _sign(ts, "")
    headers = _headers(ts, sign)
    try:
        r = await _get_client().get(f"{base}/v5/user/query-api", headers=headers)
        try:
            data = r.json()
        except Exception:
            data = {"_raw_text": (r.text or "")[:300]}
        return {"label": label, "base": base, "status": r.status_code, "response": data}
    except Exception as e:
        return {"label": label, "base": base, "error": str(e)}


async def diagnostic_endpoints() -> dict:
    """
    Tenta a MESMA key em todos os endpoints Bybit conhecidos pra descobrir
    qual sistema a reconhece:
      - api-testnet.bybit.com (testnet padrão)
      - api-demo.bybit.com    (demo trading dentro da conta principal)
      - api.bybit.com         (mainnet — só pra confirmar que não é key real)
    """
    if not is_configured():
        return {"ok": False, "error": "BYBIT_API_KEY/SECRET não configurados"}
    out = {"key_prefix": _API_KEY[:4] + "...", "tests": []}
    for base, label in [
        ("https://api-testnet.bybit.com", "testnet"),
        ("https://api-demo.bybit.com", "demo"),
        ("https://api.bybit.com", "mainnet"),
    ]:
        out["tests"].append(await _try_endpoint(base, label))
    return out


async def diagnostic() -> dict:
    """
    Diagnóstico verboso pra debug de auth: chama /v5/user/query-api (endpoint
    que retorna metadados da própria key — perms, type, status) e devolve a
    resposta crua + headers do request. Não vaza secret, mas mostra o que a
    Bybit responde EXATAMENTE pra cada tentativa.

    Útil quando "API key is invalid" persiste mesmo com key recriada — pode
    ser permission level, account type (Standard vs Unified) ou outro detalhe
    que o retMsg genérico não revela.
    """
    if not is_configured():
        return {"ok": False, "error": "BYBIT_API_KEY/SECRET não configurados"}

    out = {"key_prefix": _API_KEY[:4] + "...", "testnet": _TESTNET, "base_url": BASE,
           "tests": []}

    # 1) Public time — confirma alcance
    try:
        r = await _get_client().get(f"{BASE}/v5/market/time")
        out["tests"].append({"name": "public_time", "status": r.status_code,
                              "body": (r.text or "")[:200]})
    except Exception as e:
        out["tests"].append({"name": "public_time", "error": str(e)})

    # 2) /v5/user/query-api — RETORNA INFO DA KEY (perms, status, type)
    ts = str(int(time.time() * 1000))
    sign = _sign(ts, "")
    headers = _headers(ts, sign)
    try:
        r = await _get_client().get(f"{BASE}/v5/user/query-api", headers=headers)
        try:
            data = r.json()
        except Exception:
            data = {"_raw_text": (r.text or "")[:300]}
        out["tests"].append({
            "name": "query_api_info", "status": r.status_code,
            "request_headers_sent": {
                "X-BAPI-API-KEY": _API_KEY[:4] + "...",
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
                "X-BAPI-SIGN": sign[:12] + "...",
            },
            "server_time_used": ts,
            "response": data,
        })
    except Exception as e:
        out["tests"].append({"name": "query_api_info", "error": str(e)})

    # 3) Wallet balance (UNIFIED) — o que estava falhando
    ts2 = str(int(time.time() * 1000))
    qs = "accountType=UNIFIED"
    sign2 = _sign(ts2, qs)
    headers2 = _headers(ts2, sign2)
    try:
        r = await _get_client().get(f"{BASE}/v5/account/wallet-balance?{qs}", headers=headers2)
        try:
            data = r.json()
        except Exception:
            data = {"_raw_text": (r.text or "")[:300]}
        out["tests"].append({
            "name": "wallet_unified", "status": r.status_code, "response": data,
        })
    except Exception as e:
        out["tests"].append({"name": "wallet_unified", "error": str(e)})

    # 4) Wallet balance (CONTRACT) — caso conta seja Standard (não Unified)
    ts3 = str(int(time.time() * 1000))
    qs3 = "accountType=CONTRACT"
    sign3 = _sign(ts3, qs3)
    headers3 = _headers(ts3, sign3)
    try:
        r = await _get_client().get(f"{BASE}/v5/account/wallet-balance?{qs3}", headers=headers3)
        try:
            data = r.json()
        except Exception:
            data = {"_raw_text": (r.text or "")[:300]}
        out["tests"].append({
            "name": "wallet_contract", "status": r.status_code, "response": data,
        })
    except Exception as e:
        out["tests"].append({"name": "wallet_contract", "error": str(e)})

    return out


async def close_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
