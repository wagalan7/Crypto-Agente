"""
Usa a API pública REST da Bybit (api.bybit.com) para perpétuos USDT.
Bybit não bloqueia IPs de cloud providers como a Binance faz.
A interface pública permanece igual — nada muda no resto do código.
"""
from __future__ import annotations
import asyncio
import time
from typing import List, Dict, Optional

import httpx
import pandas as pd

BASE = "https://api.bybit.com"

_http_client: Optional[httpx.AsyncClient] = None
_symbols_cache: List[str] = []
_symbols_cache_time: float = 0
CACHE_TTL = 300

TIMEFRAME_MAP = {
    "1m": "1", "5m": "5", "15m": "15",
    "1h": "60", "4h": "240", "1d": "D",
}


def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def to_bybit(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT'"""
    return symbol.split(":")[0].replace("/", "")


def from_bybit(symbol: str) -> str:
    """'BTCUSDT' → 'BTC/USDT:USDT'"""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT:USDT"
    return symbol


async def get_perpetual_symbols() -> List[str]:
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < CACHE_TTL:
        return _symbols_cache

    client = get_client()
    symbols: List[str] = []
    cursor = ""
    while True:
        params: dict = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"{BASE}/v5/market/instruments-info", params=params)
        r.raise_for_status()
        data = r.json()
        result = data.get("result", {})
        for item in result.get("list", []):
            sym = item.get("symbol", "")
            if (
                sym.endswith("USDT")
                and item.get("quoteCoin") == "USDT"
                and item.get("contractType") == "LinearPerpetual"
                and item.get("status") == "Trading"
            ):
                symbols.append(from_bybit(sym))
        cursor = result.get("nextPageCursor", "")
        if not cursor:
            break

    symbols.sort()
    _symbols_cache = symbols
    _symbols_cache_time = now
    return symbols


async def fetch_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    interval = TIMEFRAME_MAP.get(timeframe, "60")
    bybit_sym = to_bybit(symbol)

    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/kline",
        params={"category": "linear", "symbol": bybit_sym, "interval": interval, "limit": limit},
    )
    r.raise_for_status()
    raw = r.json()["result"]["list"]  # newest first

    # reverse to oldest-first and build DataFrame
    raw = list(reversed(raw))
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    return df


async def fetch_ticker(symbol: str) -> Dict:
    bybit_sym = to_bybit(symbol)
    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/tickers",
        params={"category": "linear", "symbol": bybit_sym},
    )
    r.raise_for_status()
    t = r.json()["result"]["list"][0]
    last = float(t.get("lastPrice", 0))
    prev = float(t.get("prevPrice24h", last))
    change = ((last - prev) / prev * 100) if prev else 0
    return {
        "symbol": symbol,
        "last": last,
        "change": round(change, 2),
        "volume": float(t.get("turnover24h", 0)),
        "high": float(t.get("highPrice24h", 0)),
        "low": float(t.get("lowPrice24h", 0)),
        "bid": float(t.get("bid1Price", 0)),
        "ask": float(t.get("ask1Price", 0)),
    }


async def fetch_multiple_tickers(symbols: List[str]) -> List[Dict]:
    tasks = [fetch_ticker(s) for s in symbols[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    try:
        bybit_sym = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/tickers",
            params={"category": "linear", "symbol": bybit_sym},
        )
        r.raise_for_status()
        return float(r.json()["result"]["list"][0].get("fundingRate", 0))
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    try:
        bybit_sym = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/open-interest",
            params={"category": "linear", "symbol": bybit_sym, "intervalTime": "1h", "limit": 1},
        )
        r.raise_for_status()
        items = r.json()["result"]["list"]
        return float(items[0]["openInterest"]) if items else None
    except Exception:
        return None


async def close_exchange():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
