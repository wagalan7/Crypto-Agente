from __future__ import annotations
import ccxt.async_support as ccxt
import asyncio
import time
from typing import List, Dict, Optional
import pandas as pd
from config import BINANCE_API_KEY, BINANCE_SECRET_KEY

_exchange: Optional[ccxt.binance] = None
_symbols_cache: List[str] = []
_symbols_cache_time: float = 0
CACHE_TTL = 300


async def get_exchange() -> ccxt.binance:
    global _exchange
    if _exchange is None:
        config: dict = {
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,  # skip auth-required call
            },
            "enableRateLimit": True,
        }
        if BINANCE_API_KEY and BINANCE_API_KEY != "your_binance_api_key_here":
            config["apiKey"] = BINANCE_API_KEY
            config["secret"] = BINANCE_SECRET_KEY
        _exchange = ccxt.binance(config)
    return _exchange


async def get_perpetual_symbols() -> List[str]:
    global _symbols_cache, _symbols_cache_time
    now = time.time()
    if _symbols_cache and (now - _symbols_cache_time) < CACHE_TTL:
        return _symbols_cache

    exchange = await get_exchange()
    markets = await exchange.load_markets()
    symbols = [
        s for s, m in markets.items()
        if m.get("type") == "swap"
        and m.get("active", True)
        and m.get("quote") == "USDT"
        and ":" in s
    ]
    symbols.sort()
    _symbols_cache = symbols
    _symbols_cache_time = now
    return symbols


async def fetch_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    exchange = await get_exchange()
    ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = df["timestamp"].astype(int)
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    return df


async def fetch_ticker(symbol: str) -> Dict:
    exchange = await get_exchange()
    ticker = await exchange.fetch_ticker(symbol)
    return {
        "symbol": symbol,
        "last": ticker.get("last", 0),
        "change": ticker.get("percentage", 0),
        "volume": ticker.get("quoteVolume", 0),
        "high": ticker.get("high", 0),
        "low": ticker.get("low", 0),
        "bid": ticker.get("bid", 0),
        "ask": ticker.get("ask", 0),
    }


async def fetch_multiple_tickers(symbols: List[str]) -> List[Dict]:
    exchange = await get_exchange()
    tasks = [fetch_ticker(s) for s in symbols[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


async def fetch_funding_rate(symbol: str) -> Optional[float]:
    try:
        exchange = await get_exchange()
        funding = await exchange.fetch_funding_rate(symbol)
        return funding.get("fundingRate")
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    try:
        exchange = await get_exchange()
        oi = await exchange.fetch_open_interest(symbol)
        return oi.get("openInterest")
    except Exception:
        return None


async def close_exchange():
    global _exchange
    if _exchange:
        await _exchange.close()
        _exchange = None
