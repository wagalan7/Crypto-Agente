"""
Usa a API pública REST da OKX (www.okx.com/api/v5/) para perpétuos USDT.
OKX não bloqueia IPs de cloud providers.
A interface pública permanece igual — nada muda no resto do código.
"""
from __future__ import annotations
import asyncio
import time
from typing import List, Dict, Optional

import httpx
import pandas as pd

BASE = "https://www.okx.com"

_http_client: Optional[httpx.AsyncClient] = None
_symbols_cache: List[str] = []
_symbols_cache_time: float = 0
CACHE_TTL = 300

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "6h": "6H", "8h": "8H", "12h": "12H",
    "1d": "1D", "3d": "3D",
}


def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def to_okx(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTC-USDT-SWAP'"""
    base = symbol.split(":")[0].split("/")[0]
    return f"{base}-USDT-SWAP"


def from_okx(inst_id: str) -> str:
    """'BTC-USDT-SWAP' → 'BTC/USDT:USDT'"""
    base = inst_id.replace("-USDT-SWAP", "")
    return f"{base}/USDT:USDT"


async def get_perpetual_symbols() -> List[str]:
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < CACHE_TTL:
        return _symbols_cache

    client = get_client()
    symbols: List[str] = []
    r = await client.get(
        f"{BASE}/api/v5/public/instruments",
        params={"instType": "SWAP"},
    )
    r.raise_for_status()
    data = r.json()
    for item in data.get("data", []):
        inst_id = item.get("instId", "")
        settle = item.get("settleCcy", "")
        state = item.get("state", "")
        if inst_id.endswith("-USDT-SWAP") and settle == "USDT" and state == "live":
            symbols.append(from_okx(inst_id))

    symbols.sort()
    _symbols_cache = symbols
    _symbols_cache_time = now
    return symbols


async def fetch_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    bar = TIMEFRAME_MAP.get(timeframe, "1H")
    inst_id = to_okx(symbol)

    client = get_client()
    r = await client.get(
        f"{BASE}/api/v5/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": min(limit, 300)},
    )
    r.raise_for_status()
    raw = r.json().get("data", [])  # newest first

    raw = list(reversed(raw))
    # OKX columns: ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume", "volCcy", "volCcyQuote", "confirm"])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    return df


async def fetch_ticker(symbol: str) -> Dict:
    inst_id = to_okx(symbol)
    client = get_client()
    r = await client.get(
        f"{BASE}/api/v5/market/ticker",
        params={"instId": inst_id},
    )
    r.raise_for_status()
    t = r.json()["data"][0]
    last = float(t.get("last", 0))
    open24 = float(t.get("open24h", last))
    change = ((last - open24) / open24 * 100) if open24 else 0
    return {
        "symbol": symbol,
        "last": last,
        "change": round(change, 2),
        "volume": float(t.get("volCcy24h", 0)),
        "high": float(t.get("high24h", 0)),
        "low": float(t.get("low24h", 0)),
        "bid": float(t.get("bidPx", 0)),
        "ask": float(t.get("askPx", 0)),
    }


async def fetch_multiple_tickers(symbols: List[str]) -> List[Dict]:
    tasks = [fetch_ticker(s) for s in symbols[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    try:
        inst_id = to_okx(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/api/v5/public/funding-rate",
            params={"instId": inst_id},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return float(data[0].get("fundingRate", 0)) if data else None
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    try:
        inst_id = to_okx(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/api/v5/public/open-interest",
            params={"instType": "SWAP", "instId": inst_id},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return float(data[0].get("oi", 0)) if data else None
    except Exception:
        return None


async def close_exchange():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
