"""
Binance Futures via proxy de saída (forward proxy).

Se BINANCE_PROXY_URL estiver setada, batemos direto em fapi.binance.com COM o
proxy de egress (mesmo padrão de binance_signed_service: httpx `proxy=`), pra a
whitelist da Binance não quebrar quando o IP do host muda. Formato esperado:
"http://user:pass@host:porta" (forward proxy: tinyproxy/squid/socks5).

IMPORTANTE: NÃO anexar o path no proxy (`{PROXY}/fapi/...`) — isso trata o
forward proxy como reverse proxy e o tinyproxy responde 407. O proxy vai no
cliente httpx (`proxy=`), e a URL alvo é o fapi.binance.com real.

Senão (sem proxy), falha graceful e o caller usa `binance_vision_service`
(spot) como fallback.

Símbolos: CCXT "BTC/USDT:USDT" ↔ Binance Futures "BTCUSDT".
"""
from __future__ import annotations
import os
import time
import httpx
import pandas as pd
from typing import List, Dict, Optional

PROXY_URL = os.getenv("BINANCE_PROXY_URL", "").strip() or None
PROXY_ENABLED = bool(PROXY_URL)

# Alvo real das chamadas; o proxy (se houver) é aplicado no cliente httpx.
FAPI_BASE = "https://fapi.binance.com"

TOP_VOLUME_TTL = 120
TICKER_TTL = 60

_BLACKLIST_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDS",
    "USD1", "RLUSD", "PYUSD", "USDD", "USTC",
}

_client: Optional[httpx.AsyncClient] = None
_top_cache: Dict[str, tuple] = {}
_ticker_cache: Dict[str, tuple] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        kwargs: dict = {
            "timeout": 20.0,
            "headers": {"User-Agent": "CryptoAgent/1.0"},
            "limits": httpx.Limits(max_keepalive_connections=10, max_connections=30),
        }
        if PROXY_ENABLED:
            kwargs["proxy"] = PROXY_URL  # forward proxy (mesmo padrão do signed_service)
        _client = httpx.AsyncClient(**kwargs)
    return _client


def to_fut(symbol: str) -> str:
    base = symbol.split("/")[0]
    return f"{base}USDT"


def from_fut(fut_symbol: str) -> str:
    if fut_symbol.endswith("USDT"):
        base = fut_symbol[:-4]
        return f"{base}/USDT:USDT"
    return fut_symbol


async def close():
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def fetch_top_volume_symbols(limit: int = 40) -> List[str]:
    """Top-N símbolos PERPÉTUOS USDT por volume 24h (Binance Futures)."""
    if not PROXY_ENABLED:
        raise RuntimeError("BINANCE_PROXY_URL não configurado")
    cache_key = f"top_{limit}"
    now = time.time()
    if cache_key in _top_cache:
        ts, data = _top_cache[cache_key]
        if now - ts < TOP_VOLUME_TTL:
            return data

    client = _get_client()
    r = await client.get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
    r.raise_for_status()
    rows = r.json()

    usdt_rows = []
    for t in rows:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in _BLACKLIST_BASES:
            continue
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4:
            continue
        # Pula símbolos com pouca liquidez ou desativados
        try:
            vol = float(t.get("quoteVolume", 0))
        except Exception:
            vol = 0
        if vol <= 0:
            continue
        usdt_rows.append((from_fut(sym), vol))

    usdt_rows.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in usdt_rows[:limit]]
    _top_cache[cache_key] = (now, top)
    return top


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """OHLCV da Binance Futures."""
    if not PROXY_ENABLED:
        raise RuntimeError("BINANCE_PROXY_URL não configurado")
    fut_sym = to_fut(symbol)
    client = _get_client()
    r = await client.get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": fut_sym, "interval": timeframe, "limit": limit},
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    return df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })


async def fetch_ticker(symbol: str) -> Dict:
    if not PROXY_ENABLED:
        raise RuntimeError("BINANCE_PROXY_URL não configurado")
    fut_sym = to_fut(symbol)
    now = time.time()
    if fut_sym in _ticker_cache:
        ts, data = _ticker_cache[fut_sym]
        if now - ts < TICKER_TTL:
            return data
    client = _get_client()
    r = await client.get(
        f"{FAPI_BASE}/fapi/v1/ticker/24hr", params={"symbol": fut_sym}
    )
    r.raise_for_status()
    j = r.json()
    out = {
        "symbol": symbol,
        "last": float(j.get("lastPrice", 0)),
        "change": float(j.get("priceChangePercent", 0)),
        "volume": float(j.get("quoteVolume", 0)),
        "high": float(j.get("highPrice", 0)),
        "low": float(j.get("lowPrice", 0)),
    }
    _ticker_cache[fut_sym] = (now, out)
    return out


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    if not PROXY_ENABLED:
        return None
    fut_sym = to_fut(symbol)
    try:
        client = _get_client()
        r = await client.get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex", params={"symbol": fut_sym}
        )
        r.raise_for_status()
        j = r.json()
        return float(j.get("lastFundingRate", 0))
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    if not PROXY_ENABLED:
        return None
    fut_sym = to_fut(symbol)
    try:
        client = _get_client()
        r = await client.get(
            f"{FAPI_BASE}/fapi/v1/openInterest", params={"symbol": fut_sym}
        )
        r.raise_for_status()
        j = r.json()
        return float(j.get("openInterest", 0))
    except Exception:
        return None
