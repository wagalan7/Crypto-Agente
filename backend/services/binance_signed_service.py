"""
Binance Futures (USDT-M) — cliente ASSINADO (HMAC-SHA256) — #11.

Interface 100% compatível com bybit_signed_service.py (mesmas funções,
mesmo shape de retorno) — o resto do app não precisa saber qual exchange
está em uso. Selecione via env var EXCHANGE=binance|bybit.

Auth (Binance):
  - Sign = HMAC-SHA256(secret, querystring) onde querystring inclui timestamp
  - Anexa "&signature=<hex>" no final da URL (GET/POST/DELETE)
  - Header X-MBX-APIKEY: <key>
  - GET/POST/DELETE — todos signed seguem o mesmo padrão

Refs: https://binance-docs.github.io/apidocs/futures/en/

Env:
  BINANCE_API_KEY        — API key
  BINANCE_API_SECRET     — secret
  BINANCE_TESTNET        — "true" (default) → testnet.binancefuture.com
  BINANCE_RECV_WINDOW    — janela em ms (default 5000)

Restrição regulatória: residentes BR não conseguem acessar futures na
conta mainnet via Binance global desde 2023 (CVM). Testnet funciona
normalmente — útil pra validar bot. Pra mainnet em BR, considere Bybit/OKX.
"""
from __future__ import annotations
import hmac
import hashlib
import os
import time
import logging
from typing import Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
# BINANCE_MODE: "demo" (default — demo-fapi.binance.com, conta principal Binance),
#               "testnet" (testnet.binancefuture.com — sistema legado, GitHub login),
#               "mainnet" (fapi.binance.com — produção real)
# Backward-compat: se BINANCE_MODE não setado, usa BINANCE_TESTNET (true=demo, false=mainnet).
_MODE = os.getenv("BINANCE_MODE", "").strip().lower()
if not _MODE:
    _TESTNET_LEGACY = os.getenv("BINANCE_TESTNET", "true").strip().lower() in ("1", "true", "yes")
    _MODE = "demo" if _TESTNET_LEGACY else "mainnet"

_BASE_BY_MODE = {
    "demo":    "https://demo-fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "mainnet": "https://fapi.binance.com",
}
BASE = _BASE_BY_MODE.get(_MODE, "https://demo-fapi.binance.com")
_TESTNET = _MODE in ("demo", "testnet")  # mantém flag pra compat com env_info
_RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))

_http_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    return bool(_API_KEY and _API_SECRET)


def env_info() -> dict:
    return {
        "configured": is_configured(),
        "mode": _MODE,
        "testnet": _TESTNET,
        "base_url": BASE,
        "key_prefix": _API_KEY[:4] + "..." if _API_KEY else None,
        "recv_window_ms": _RECV_WINDOW,
    }


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15.0, headers={"X-MBX-APIKEY": _API_KEY})
    return _http_client


def _sign(qs: str) -> str:
    return hmac.new(_API_SECRET.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()


def _build_signed_url(path: str, params: Optional[dict] = None) -> str:
    """Monta querystring + timestamp + signature. Funciona pra GET/POST/DELETE."""
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = _RECV_WINDOW
    # Remove None
    p = {k: v for k, v in p.items() if v is not None}
    qs = urlencode(p)
    sig = _sign(qs)
    return f"{BASE}{path}?{qs}&signature={sig}"


async def _signed_request(method: str, path: str, params: Optional[dict] = None) -> dict:
    if not is_configured():
        return {"ok": False, "error": "BINANCE_API_KEY/SECRET não configurados"}
    url = _build_signed_url(path, params)
    try:
        r = await _get_client().request(method, url)
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "error": f"resposta não-JSON ({r.status_code}): {r.text[:200]}"}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("code") and data.get("code") < 0):
            log.warning(f"[binance] {method} {path} status={r.status_code} resp={data}")
            return {"ok": False, "code": data.get("code") if isinstance(data, dict) else r.status_code,
                    "msg": data.get("msg") if isinstance(data, dict) else r.text, "raw": data}
        return {"ok": True, "result": data, "raw": data}
    except Exception as e:
        log.exception(f"[binance] {method} {path} falhou")
        return {"ok": False, "error": str(e)}


# ─── Symbol helpers ────────────────────────────────────────────────────────────


def to_binance(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT' (mesma convenção Bybit)."""
    return symbol.split(":")[0].replace("/", "")


# ─── Precision (stepSize/tickSize) cache ──────────────────────────────────────
# Binance Futures rejeita ordens com qty/preço fora do stepSize/tickSize do
# símbolo (erro "Precision is over the maximum"). Buscamos exchangeInfo 1x
# e cacheamos os filtros por símbolo — depois truncamos qty/SL/TP antes do
# submit. ExchangeInfo é público; usa o mesmo BASE.

_filters_cache: dict = {}  # sym → {"step": float, "tick": float, "min_qty": float}
_filters_lock = None  # lazy: criado no primeiro uso pra herdar o loop ativo


async def _load_exchange_info() -> dict:
    """Pega /fapi/v1/exchangeInfo (público, sem assinar) e popula o cache."""
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/exchangeInfo")
        data = r.json()
        for s in (data.get("symbols") or []):
            sym = s.get("symbol")
            if not sym:
                continue
            step = 0.0
            tick = 0.0
            min_qty = 0.0
            for f in (s.get("filters") or []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize") or 0)
                    min_qty = float(f.get("minQty") or 0)
                elif f.get("filterType") == "PRICE_FILTER":
                    tick = float(f.get("tickSize") or 0)
            _filters_cache[sym] = {"step": step, "tick": tick, "min_qty": min_qty}
        log.info(f"[binance] exchangeInfo carregado: {len(_filters_cache)} símbolos")
    except Exception as e:
        log.warning(f"[binance] exchangeInfo falhou (segue sem precisão): {e}")
    return _filters_cache


async def _get_symbol_filters(sym: str) -> dict:
    if sym in _filters_cache:
        return _filters_cache[sym]
    import asyncio as _aio
    global _filters_lock
    if _filters_lock is None:
        _filters_lock = _aio.Lock()
    async with _filters_lock:
        if sym in _filters_cache:
            return _filters_cache[sym]
        if not _filters_cache:
            await _load_exchange_info()
    return _filters_cache.get(sym, {"step": 0.0, "tick": 0.0, "min_qty": 0.0})


def _floor_to_step(value: float, step: float) -> float:
    """Trunca (não arredonda) pro múltiplo de step mais próximo abaixo.
    Ex: floor(61651.676, 1) = 61651; floor(0.123456, 0.001) = 0.123.
    Usa string formatting pra evitar drift de float."""
    if step <= 0:
        return value
    n = int(value / step)  # floor implícito (truncamento)
    out = n * step
    # Acerta casas decimais — quantas tem o step
    # ex: step=0.001 → 3 casas; step=1 → 0 casas
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    decimals = len(s.split(".")[1]) if "." in s else 0
    return round(out, decimals)


async def _round_qty(sym: str, qty: float) -> float:
    f = await _get_symbol_filters(sym)
    step = f.get("step", 0.0)
    if step <= 0:
        return qty
    return _floor_to_step(qty, step)


async def _round_price(sym: str, price: float) -> float:
    f = await _get_symbol_filters(sym)
    tick = f.get("tick", 0.0)
    if tick <= 0:
        return price
    return _floor_to_step(price, tick)


# ─── High-level endpoints (mesma interface do bybit_signed_service) ───────────


async def get_wallet_balance(account_type: str = "UNIFIED") -> dict:
    """
    Saldo Futures USDT-M. Binance não tem o conceito 'UNIFIED' como Bybit —
    parâmetro é aceito por compat mas ignorado. Retorna mesmo shape.
    """
    _ = account_type
    res = await _signed_request("GET", "/fapi/v2/account")
    if not res.get("ok"):
        return res
    acc = res["result"] or {}
    return {
        "ok": True,
        "equity_usd": float(acc.get("totalMarginBalance") or 0),
        "available_usd": float(acc.get("availableBalance") or 0),
        "wallet_balance_usd": float(acc.get("totalWalletBalance") or 0),
        "margin_used_usd": float(acc.get("totalInitialMargin") or 0),
        "coins": [
            {
                "coin": a.get("asset"),
                "balance": float(a.get("walletBalance") or 0),
                "equity": float(a.get("marginBalance") or 0),
                "usd_value": float(a.get("walletBalance") or 0)
                if a.get("asset") in ("USDT", "BUSD", "USDC") else None,
            }
            for a in (acc.get("assets") or [])
            if float(a.get("walletBalance") or 0) > 0
        ],
        "testnet": _TESTNET,
        "exchange": "binance",
    }


async def get_positions(symbol: Optional[str] = None) -> dict:
    params = {}
    if symbol:
        params["symbol"] = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v2/positionRisk", params or None)
    if not res.get("ok"):
        return res
    rows = res["result"] or []
    positions = []
    for p in rows:
        size = abs(float(p.get("positionAmt") or 0))
        if size <= 0:
            continue
        amt = float(p.get("positionAmt") or 0)
        side = "Buy" if amt > 0 else "Sell"
        positions.append({
            "symbol": p.get("symbol"),
            "side": side,
            "size": size,
            "entry_price": float(p.get("entryPrice") or 0),
            "mark_price": float(p.get("markPrice") or 0),
            "unrealized_pnl": float(p.get("unRealizedProfit") or 0),
            "leverage": float(p.get("leverage") or 0),
            "position_value": float(p.get("notional") or 0),
            "take_profit": None,  # Binance não retorna TP/SL nesse endpoint
            "stop_loss": None,
        })
    return {"ok": True, "positions": positions, "count": len(positions),
            "testnet": _TESTNET, "exchange": "binance"}


async def place_order(
    symbol: str,
    side: str,           # "Buy" | "Sell" (Bybit-compat)
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
    Cria ordem em futures USDT-M. Aceita "Buy/Sell" (Bybit-style) e traduz pra
    "BUY/SELL" (Binance). Para TP/SL, Binance exige ordens SEPARADAS — emitidas
    aqui em sequência após a entry.
    """
    sym = to_binance(symbol) if "/" in symbol else symbol

    # Arredonda qty/SL/TP pro stepSize/tickSize do símbolo. Sem isso, Binance
    # rejeita com "Precision is over the maximum defined for this asset"
    # (ex: DOGE só aceita qty inteiro, qty=61651.676 → erro).
    qty_rounded = await _round_qty(sym, float(qty))
    if qty_rounded <= 0:
        f = await _get_symbol_filters(sym)
        return {"ok": False, "error": f"qty arredondado virou 0 (step={f.get('step')}, min={f.get('min_qty')}, raw={qty})"}
    if qty_rounded != qty:
        log.info(f"[binance] qty arredondado {sym}: {qty} → {qty_rounded}")

    if leverage is not None:
        await set_leverage(sym, leverage)

    binance_side = side.upper()  # BUY | SELL
    binance_type = "MARKET" if order_type == "Market" else "LIMIT"

    params = {
        "symbol": sym,
        "side": binance_side,
        "type": binance_type,
        "quantity": qty_rounded,
    }
    if binance_type == "LIMIT":
        if price is None:
            return {"ok": False, "error": "LIMIT exige price"}
        params["price"] = await _round_price(sym, float(price))
        params["timeInForce"] = "GTC"
    if reduce_only:
        params["reduceOnly"] = "true"
    if client_order_id:
        params["newClientOrderId"] = client_order_id

    entry_res = await _signed_request("POST", "/fapi/v1/order", params)
    if not entry_res.get("ok"):
        return entry_res

    # TP/SL em ordens separadas (Binance pattern). Side invertido + closePosition.
    extras = []
    counter_side = "SELL" if binance_side == "BUY" else "BUY"
    if stop_loss is not None:
        sl_price = await _round_price(sym, float(stop_loss))
        sl = await _signed_request("POST", "/fapi/v1/order", {
            "symbol": sym, "side": counter_side, "type": "STOP_MARKET",
            "stopPrice": sl_price, "closePosition": "true",
        })
        extras.append({"stop_loss": sl})
    if take_profit is not None:
        tp_price = await _round_price(sym, float(take_profit))
        tp = await _signed_request("POST", "/fapi/v1/order", {
            "symbol": sym, "side": counter_side, "type": "TAKE_PROFIT_MARKET",
            "stopPrice": tp_price, "closePosition": "true",
        })
        extras.append({"take_profit": tp})

    return {"ok": True, "result": entry_res["result"], "extras": extras, "raw": entry_res["raw"]}


async def cancel_order(symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> dict:
    sym = to_binance(symbol) if "/" in symbol else symbol
    params = {"symbol": sym}
    if order_id:
        params["orderId"] = order_id
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        return {"ok": False, "error": "informe order_id ou client_order_id"}
    return await _signed_request("DELETE", "/fapi/v1/order", params)


async def set_leverage(symbol: str, leverage: int) -> dict:
    res = await _signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol, "leverage": leverage,
    })
    return res


async def get_order_history(symbol: Optional[str] = None, limit: int = 50) -> dict:
    if not symbol:
        return {"ok": False, "error": "Binance allOrders exige symbol"}
    sym = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v1/allOrders", {"symbol": sym, "limit": limit})
    if not res.get("ok"):
        return res
    rows = res["result"] or []
    orders = [
        {
            "order_id": str(o.get("orderId")),
            "client_order_id": o.get("clientOrderId"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "order_type": o.get("type"),
            "qty": float(o.get("origQty") or 0),
            "price": float(o.get("price") or 0),
            "avg_fill_price": float(o.get("avgPrice") or 0),
            "status": o.get("status"),
            "created_at": str(o.get("time")),
            "updated_at": str(o.get("updateTime")),
        }
        for o in rows
    ]
    return {"ok": True, "orders": orders, "count": len(orders)}


async def get_executions(symbol: Optional[str] = None, limit: int = 50) -> dict:
    if not symbol:
        return {"ok": False, "error": "Binance userTrades exige symbol"}
    sym = to_binance(symbol) if "/" in symbol else symbol
    res = await _signed_request("GET", "/fapi/v1/userTrades", {"symbol": sym, "limit": limit})
    if not res.get("ok"):
        return res
    rows = res["result"] or []
    fills = [
        {
            "exec_id": str(e.get("id")),
            "order_id": str(e.get("orderId")),
            "symbol": e.get("symbol"),
            "side": e.get("side"),
            "qty": float(e.get("qty") or 0),
            "price": float(e.get("price") or 0),
            "fee": float(e.get("commission") or 0),
            "is_maker": e.get("maker"),
            "time": str(e.get("time")),
        }
        for e in rows
    ]
    return {"ok": True, "fills": fills, "count": len(fills)}


async def close_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ─── Diagnostic (debug auth issues) ────────────────────────────────────────────


async def diagnostic() -> dict:
    """
    Diagnóstico verboso pra debug de auth Binance — não vaza secret.
    Inclui lengths e SHA1 de key/secret pra comparar bit-a-bit com painel.
    Bybit keys são 18/36 chars; Binance Futures testnet keys são 64/64 chars.
    """
    if not is_configured():
        return {"ok": False, "error": "BINANCE_API_KEY/SECRET não configurados"}
    key_has_nonascii = any(ord(c) > 127 or ord(c) < 32 for c in _API_KEY)
    secret_has_nonascii = any(ord(c) > 127 or ord(c) < 32 for c in _API_SECRET)
    key_sha1 = hashlib.sha1(_API_KEY.encode("utf-8")).hexdigest()[:12]
    secret_sha1 = hashlib.sha1(_API_SECRET.encode("utf-8")).hexdigest()[:12]
    out = {
        "exchange": "binance",
        "mode": _MODE,
        "base_url": BASE,
        "testnet": _TESTNET,
        "key_prefix": _API_KEY[:4] + "...",
        "key_len": len(_API_KEY),
        "secret_len": len(_API_SECRET),
        "key_has_nonascii": key_has_nonascii,
        "secret_has_nonascii": secret_has_nonascii,
        "key_sha1_12": key_sha1,
        "secret_sha1_12": secret_sha1,
        "_hint": "Binance Futures testnet keys = 64 chars cada. Compare local: echo -n 'X' | shasum | cut -c1-12",
        "tests": [],
    }
    # Test 1: public ping (network)
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/ping")
        out["tests"].append({"name": "public_ping", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        out["tests"].append({"name": "public_ping", "error": str(e)})
    # Test 2: server time (clock drift)
    try:
        r = await _get_client().get(f"{BASE}/fapi/v1/time")
        try:
            data = r.json()
            server_ms = int(data.get("serverTime") or 0)
            local_ms = int(time.time() * 1000)
            drift = local_ms - server_ms
            out["tests"].append({"name": "server_time", "status": r.status_code,
                                 "server_ms": server_ms, "local_ms": local_ms, "drift_ms": drift})
        except Exception:
            out["tests"].append({"name": "server_time", "status": r.status_code, "body": r.text[:200]})
    except Exception as e:
        out["tests"].append({"name": "server_time", "error": str(e)})
    # Test 3: signed — account (auth)
    res = await _signed_request("GET", "/fapi/v2/account")
    out["tests"].append({"name": "signed_account", "ok": res.get("ok"),
                         "code": res.get("code"), "msg": res.get("msg")})
    # Test 4: signed — balance (alt endpoint, sometimes auth differs)
    res2 = await _signed_request("GET", "/fapi/v2/balance")
    out["tests"].append({"name": "signed_balance", "ok": res2.get("ok"),
                         "code": res2.get("code"), "msg": res2.get("msg")})
    return out
