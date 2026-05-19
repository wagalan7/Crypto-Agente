"""
Fonte de dados: Bybit V5 (api.bybit.com) — perpétuos USDT lineares.

Trocado de OKX para Bybit pq Bybit lista ~3× mais perps USDT (~600 vs ~210),
e a API V5 deles aceita IPs de cloud providers (Railway etc).
Binance Futures também tem cobertura ampla, mas o `fapi.binance.com` retorna 451
para IPs de cloud — por isso fica reservado pro frontend (browser do usuário).

A interface pública permanece IGUAL — nada muda no resto do código.
Funções helper auxiliares (`to_okx`/`from_okx`) viraram aliases pra Bybit.
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

# Bybit intervals: 1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M
TIMEFRAME_MAP = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "6h": "360", "8h": "480", "12h": "720",
    "1d": "D", "3d": "D",   # 3d não existe na Bybit — degrada pra D
}


def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def to_bybit(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'BTCUSDT'"""
    return symbol.split(":")[0].replace("/", "")


def from_bybit(bb_symbol: str) -> str:
    """'BTCUSDT' → 'BTC/USDT:USDT' (assume sufixo USDT)"""
    if bb_symbol.endswith("USDT"):
        base = bb_symbol[:-4]
        return f"{base}/USDT:USDT"
    return bb_symbol


# Aliases retro-compatíveis (recommendation_service e outros podem chamar)
to_okx = to_bybit
from_okx = from_bybit


async def get_perpetual_symbols() -> List[str]:
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < CACHE_TTL:
        return _symbols_cache

    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/instruments-info",
        params={"category": "linear", "limit": 1000},
    )
    r.raise_for_status()
    payload = r.json()
    items = payload.get("result", {}).get("list", [])

    symbols: List[str] = []
    for item in items:
        sym = item.get("symbol", "")
        quote = item.get("quoteCoin", "")
        status = item.get("status", "")
        contract_type = item.get("contractType", "")
        # LinearPerpetual + USDT + Trading
        if quote == "USDT" and status == "Trading" and contract_type == "LinearPerpetual":
            symbols.append(from_bybit(sym))

    symbols.sort()
    _symbols_cache = symbols
    _symbols_cache_time = now
    return symbols


async def fetch_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    interval = TIMEFRAME_MAP.get(timeframe, "60")
    bb_symbol = to_bybit(symbol)

    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/kline",
        params={
            "category": "linear",
            "symbol": bb_symbol,
            "interval": interval,
            "limit": min(limit, 1000),   # Bybit aceita até 1000
        },
    )
    r.raise_for_status()
    payload = r.json()
    raw = payload.get("result", {}).get("list", [])  # newest first
    raw = list(reversed(raw))

    # Bybit kline: [start, open, high, low, close, volume, turnover]
    if not raw:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    return df


async def fetch_ticker(symbol: str) -> Dict:
    bb_symbol = to_bybit(symbol)
    client = get_client()
    r = await client.get(
        f"{BASE}/v5/market/tickers",
        params={"category": "linear", "symbol": bb_symbol},
    )
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("result", {}).get("list", [])
    if not rows:
        raise ValueError(f"Ticker vazio para {symbol}")
    t = rows[0]
    last = float(t.get("lastPrice", 0))
    change_pct = float(t.get("price24hPcnt", 0)) * 100   # vem em fração
    return {
        "symbol": symbol,
        "last": last,
        "change": round(change_pct, 2),
        "volume": float(t.get("turnover24h", 0)),    # turnover em USDT
        "high": float(t.get("highPrice24h", 0)),
        "low": float(t.get("lowPrice24h", 0)),
        "bid": float(t.get("bid1Price", 0)),
        "ask": float(t.get("ask1Price", 0)),
    }


_top_volume_cache: Dict[str, tuple] = {}
TOP_VOLUME_TTL = 120


async def fetch_top_volume_symbols(limit: int = 30) -> List[str]:
    """Retorna os top-N símbolos perp USDT por volume 24h (cache 2 min)."""
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
            vol_usd = float(t.get("turnover24h", 0))
        except Exception:
            vol_usd = 0
        usdt_rows.append((from_bybit(sym), vol_usd))
    usdt_rows.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in usdt_rows[:limit]]
    _top_volume_cache[cache_key] = (now, top)
    return top


async def fetch_multiple_tickers(symbols: List[str]) -> List[Dict]:
    tasks = [fetch_ticker(s) for s in symbols[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    """Bybit retorna funding atual junto com o ticker."""
    try:
        bb_symbol = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/tickers",
            params={"category": "linear", "symbol": bb_symbol},
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        if not rows:
            return None
        return float(rows[0].get("fundingRate", 0))
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    """OI mais recente. Bybit retorna histórico — pegamos o último ponto."""
    try:
        bb_symbol = to_bybit(symbol)
        client = get_client()
        r = await client.get(
            f"{BASE}/v5/market/open-interest",
            params={
                "category": "linear",
                "symbol": bb_symbol,
                "intervalTime": "1h",
                "limit": 1,
            },
        )
        r.raise_for_status()
        data = r.json().get("result", {}).get("list", [])
        return float(data[0].get("openInterest", 0)) if data else None
    except Exception:
        return None


async def close_exchange():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
