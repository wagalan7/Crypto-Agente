"""
Binance Spot via data-api.binance.vision — endpoint público sem geo-block,
usado EXCLUSIVAMENTE pelo server-side scan no Railway (que não consegue
acessar fapi.binance.com nem api.binance.com — ambos retornam 451).

Spot acompanha futures em >99% dos movimentos pra os pares USDT principais,
então pra fins de varredura técnica e push notifications é equivalente.

Símbolos: CCXT "BTC/USDT:USDT" ↔ Binance "BTCUSDT".
Timeframes: "15m", "1h", "4h", "1d" são compatíveis 1:1.
"""
from __future__ import annotations
import time
import httpx
import pandas as pd
from typing import List, Dict, Optional

BASE = "https://data-api.binance.vision"
TOP_VOLUME_TTL = 120
TICKER_TTL = 60

# Pares a ignorar (stablecoins, fiat pairs — sem oportunidade técnica)
_BLACKLIST_BASES = {
    # Stablecoins
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDS", "PAX",
    "USD1", "RLUSD", "PYUSD", "USDD", "USTC", "GUSD", "USDQ",
    # Fiat
    "EUR", "GBP", "TRY", "BRL", "JPY", "RUB", "ARS", "AUD", "CAD", "PLN", "ZAR", "MXN",
}

_client: Optional[httpx.AsyncClient] = None
_top_cache: Dict[str, tuple] = {}
_ticker_cache: Dict[str, tuple] = {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (CryptoAI-Agent)"},
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=30),
        )
    return _client


def to_bv(symbol: str) -> str:
    """CCXT 'BTC/USDT:USDT' → Binance 'BTCUSDT'."""
    base = symbol.split("/")[0]
    return f"{base}USDT"


def from_bv(bv_symbol: str) -> str:
    """Binance 'BTCUSDT' → CCXT 'BTC/USDT:USDT'."""
    if bv_symbol.endswith("USDT"):
        base = bv_symbol[:-4]
        return f"{base}/USDT:USDT"
    return bv_symbol


async def close():
    global _client
    if _client:
        await _client.aclose()
        _client = None


async def fetch_top_volume_symbols(limit: int = 30) -> List[str]:
    """Top-N símbolos USDT por volume 24h (spot)."""
    cache_key = f"top_{limit}"
    now = time.time()
    if cache_key in _top_cache:
        ts, data = _top_cache[cache_key]
        if now - ts < TOP_VOLUME_TTL:
            return data

    client = _get_client()
    r = await client.get(f"{BASE}/api/v3/ticker/24hr")
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
        # Pula tokens alavancados (UP/DOWN/BULL/BEAR)
        if any(base.endswith(s) for s in ("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4:
            continue
        try:
            vol_usd = float(t.get("quoteVolume", 0))
        except Exception:
            vol_usd = 0
        if vol_usd <= 0:
            continue
        usdt_rows.append((from_bv(sym), vol_usd))

    usdt_rows.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in usdt_rows[:limit]]
    _top_cache[cache_key] = (now, top)
    return top


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """OHLCV da Binance Spot."""
    bv_sym = to_bv(symbol)
    client = _get_client()
    r = await client.get(
        f"{BASE}/api/v3/klines",
        params={"symbol": bv_sym, "interval": timeframe, "limit": limit},
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
    """Ticker 24h pra calcular price_change."""
    bv_sym = to_bv(symbol)
    now = time.time()
    if bv_sym in _ticker_cache:
        ts, data = _ticker_cache[bv_sym]
        if now - ts < TICKER_TTL:
            return data
    client = _get_client()
    r = await client.get(f"{BASE}/api/v3/ticker/24hr", params={"symbol": bv_sym})
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
    _ticker_cache[bv_sym] = (now, out)
    return out
