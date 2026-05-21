"""
Bybit V5 public REST (api.bybit.com/v5/market/*) para perpétuos linear USDT.

Interface 100% compatível com binance_service.py (mesmo nome de funções,
mesmo shape de retorno) — qualquer consumidor pode trocar o import sem
mais ajustes. Universo de pares ~2x maior que OKX (700+ vs ~350 alts).

Endpoints usados:
  GET /v5/market/tickers?category=linear            (lista + volume 24h)
  GET /v5/market/kline?category=linear&symbol=..&interval=..&limit=..
  GET /v5/market/funding/history?category=linear&symbol=..&limit=1
  GET /v5/market/open-interest?category=linear&symbol=..&intervalTime=1h&limit=1

Resposta: {retCode, retMsg, result: {list: [...]}, time}
Kline: list de [start, open, high, low, close, volume, turnover] (strings,
NEWEST FIRST — invertemos pra ordenação cronológica).
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

# Bybit interval values: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M
TIMEFRAME_MAP = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "3d": "D",  # 3d não existe nativo; cai pra D (consumidor reagrupa se precisar)
    "1w": "W", "1M": "M",
}


def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def to_bybit(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT'"""
    base = symbol.split(":")[0].split("/")[0]
    return f"{base}USDT"


def from_bybit(bybit_sym: str) -> str:
    """'BTCUSDT' → 'BTC/USDT:USDT'"""
    if bybit_sym.endswith("USDT"):
        base = bybit_sym[:-4]
        return f"{base}/USDT:USDT"
    return bybit_sym


# Aliases retro-compat (código pode importar to_okx do binance_service)
to_okx = to_bybit
from_okx = from_bybit


async def get_perpetual_symbols() -> List[str]:
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < CACHE_TTL:
        return _symbols_cache

    client = get_client()
    r = await client.get(f"{BASE}/v5/market/tickers", params={"category": "linear"})
    r.raise_for_status()
    rows = r.json().get("result", {}).get("list", [])
    symbols: List[str] = []
    for t in rows:
        sym = t.get("symbol", "")
        if sym.endswith("USDT"):
            symbols.append(from_bybit(sym))
    symbols.sort()
    _symbols_cache = symbols
    _symbols_cache_time = now
    return symbols


async def fetch_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    interval = TIMEFRAME_MAP.get(timeframe, "60")
    sym = to_bybit(symbol)

    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/kline",
        params={"category": "linear", "symbol": sym, "interval": interval, "limit": min(limit, 1000)},
    )
    r.raise_for_status()
    raw = r.json().get("result", {}).get("list", [])  # newest first
    raw = list(reversed(raw))
    # Bybit kline cols: start, open, high, low, close, volume, turnover (strings)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    return df


async def fetch_ticker(symbol: str) -> Dict:
    sym = to_bybit(symbol)
    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/tickers",
        params={"category": "linear", "symbol": sym},
    )
    r.raise_for_status()
    rows = r.json().get("result", {}).get("list", [])
    if not rows:
        return {"symbol": symbol, "last": 0, "change": 0, "volume": 0, "high": 0, "low": 0, "bid": 0, "ask": 0}
    t = rows[0]
    last = float(t.get("lastPrice", 0))
    # price24hPcnt vem como decimal (ex: "0.0472"), converte pra %
    try:
        change_pct = float(t.get("price24hPcnt", 0)) * 100
    except Exception:
        change_pct = 0
    return {
        "symbol": symbol,
        "last": last,
        "change": round(change_pct, 2),
        "volume": float(t.get("turnover24h", 0)),
        "high": float(t.get("highPrice24h", 0)),
        "low": float(t.get("lowPrice24h", 0)),
        "bid": float(t.get("bid1Price", 0) or 0),
        "ask": float(t.get("ask1Price", 0) or 0),
    }


_top_volume_cache: Dict[str, tuple] = {}
TOP_VOLUME_TTL = 120


async def fetch_top_volume_symbols(limit: int = 30) -> List[str]:
    """Top-N perp linear USDT por turnover 24h (cache 2 min)."""
    cache_key = f"top_{limit}"
    now = time.time()
    if cache_key in _top_volume_cache:
        ts, data = _top_volume_cache[cache_key]
        if now - ts < TOP_VOLUME_TTL:
            return data
    client = get_client()
    r = await client.get(f"{BASE}/v5/market/tickers", params={"category": "linear"})
    r.raise_for_status()
    rows = r.json().get("result", {}).get("list", [])
    usdt_rows = []
    for t in rows:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            turnover = float(t.get("turnover24h", 0))
        except Exception:
            turnover = 0
        usdt_rows.append((from_bybit(sym), turnover))
    usdt_rows.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in usdt_rows[:limit]]
    _top_volume_cache[cache_key] = (now, top)
    return top


async def fetch_multiple_tickers(symbols: List[str]) -> List[Dict]:
    tasks = [fetch_ticker(s) for s in symbols[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    try:
        sym = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/funding/history",
            params={"category": "linear", "symbol": sym, "limit": 1},
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        return float(rows[0].get("fundingRate", 0)) if rows else None
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    try:
        sym = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/open-interest",
            params={"category": "linear", "symbol": sym, "intervalTime": "1h", "limit": 1},
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        return float(rows[0].get("openInterest", 0)) if rows else None
    except Exception:
        return None


async def close_exchange():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
