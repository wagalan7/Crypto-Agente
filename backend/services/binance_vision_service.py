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
import re
import time
import asyncio
from datetime import datetime, timezone
import httpx
import pandas as pd
from typing import List, Dict, Optional

BASE = "https://data-api.binance.vision"
# Listing S3 público do arquivo (futures UM). Estático: sem proxy, sem weight,
# NUNCA bane IP — ao contrário do exchangeInfo via fapi/proxy (que está 418).
S3_LIST_BASE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
TOP_VOLUME_TTL = 120
TICKER_TTL = 60
PERP_VISION_TTL = 3600

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
_perp_symbols_vision_cache: Dict[str, tuple] = {}
_perp_onboard_vision_cache: Dict[str, tuple] = {}


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


def _norm_base_vision(symbol: str) -> str:
    """Base normalizada (sem prefixo '1000') — espelha _norm_base do universe
    service, pra dedup/ordenação casarem com a allowlist."""
    b = symbol.split("/")[0].upper()
    return b[4:] if b.startswith("1000") and len(b) > 4 else b


async def fetch_perp_symbols_vision() -> List[str]:
    """Símbolos CCXT de TODOS os perps USDT do ARQUIVO data.binance.vision
    (futures UM), via listing S3 público — SEM proxy, SEM weight, NUNCA bane IP.
    (O exchangeInfo via fapi/proxy está 418-banido; este caminho contorna.)

    Lista os CommonPrefixes de data/futures/um/daily/klines/ (1 dir por símbolo),
    filtra *USDT (descarta stablecoin/fiat e pares *USDC), dedup por base e devolve
    em from_bv ('1000BONK/USDT:USDT' — mantém prefixo 1000, casa com o arquivo do
    vision e com o símbolo gravado em symbol_backtest_stats). Cache 1h."""
    now = time.time()
    cached = _perp_symbols_vision_cache.get("list")
    if cached and now - cached[0] < PERP_VISION_TTL:
        return cached[1]
    client = _get_client()
    prefix = "data/futures/um/daily/klines/"
    marker = ""
    seen_base: set = set()
    out: List[str] = []
    for _ in range(50):  # guarda-chuva anti-loop (hoje cabe em 1 página)
        params = {"delimiter": "/", "prefix": prefix}
        if marker:
            params["marker"] = marker
        r = await client.get(S3_LIST_BASE, params=params)
        r.raise_for_status()
        body = r.text
        page = re.findall(rf"<Prefix>{re.escape(prefix)}([^<]+?)/</Prefix>", body)
        for sym in page:
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in _BLACKLIST_BASES:
                continue
            nb = base[4:] if base.startswith("1000") and len(base) > 4 else base
            if nb in seen_base:
                continue
            seen_base.add(nb)
            out.append(from_bv(sym))
        if "<IsTruncated>true</IsTruncated>" not in body:
            break
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", body)
        marker = m.group(1) if m else (f"{prefix}{page[-1]}/" if page else "")
        if not marker:
            break
    if out:
        _perp_symbols_vision_cache["list"] = (now, out)
    return out


async def _earliest_ms_vision(client, sym_bv: str, sem, tf: str) -> Optional[int]:
    """Mês mais antigo (ms) do arquivo monthly/klines/{sym}/{tf} no vision."""
    prefix = f"data/futures/um/monthly/klines/{sym_bv}/{tf}/"
    async with sem:
        try:
            r = await client.get(S3_LIST_BASE, params={"prefix": prefix})
            r.raise_for_status()
            months = re.findall(r"-(\d{4})-(\d{2})\.zip", r.text)
        except Exception:
            return None
    if not months:
        return None
    y, mo = min((int(a), int(b)) for a, b in months)
    return int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() * 1000)


async def fetch_perp_onboard_dates_vision(symbols: List[str], tf: str = "4h") -> dict:
    """Mapa {NORM_BASE: onboard_ms} derivado do mês MAIS ANTIGO no arquivo vision
    (proxy-free) — pra ORDENAR o sweep do histórico mais antigo p/ o mais novo SEM
    tocar no fapi/proxy banido. Best-effort/concorrente: símbolo sem arquivo fica
    ausente (vai pro fim na ordenação). Cache 1h."""
    now = time.time()
    cached = _perp_onboard_vision_cache.get("map")
    base_map: dict = dict(cached[1]) if (cached and now - cached[0] < PERP_VISION_TTL) else {}
    todo = [s for s in symbols if _norm_base_vision(s) not in base_map]
    if todo:
        client = _get_client()
        sem = asyncio.Semaphore(12)
        results = await asyncio.gather(
            *[_earliest_ms_vision(client, to_bv(s), sem, tf) for s in todo]
        )
        for s, ms in zip(todo, results):
            if ms is not None:
                base_map[_norm_base_vision(s)] = ms
    _perp_onboard_vision_cache["map"] = (now, base_map)
    return base_map
