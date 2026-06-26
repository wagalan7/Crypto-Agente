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
import logging
import httpx
import pandas as pd
from typing import List, Dict, Optional, Set

log = logging.getLogger(__name__)

PROXY_URL = os.getenv("BINANCE_PROXY_URL", "").strip() or None
PROXY_ENABLED = bool(PROXY_URL)

# Alvo real das chamadas; o proxy (se houver) é aplicado no cliente httpx.
FAPI_BASE = "https://fapi.binance.com"

TOP_VOLUME_TTL = 120
TICKER_TTL = 60
OHLCV_TTL = float(os.getenv("BINANCE_OHLCV_TTL", "60"))  # cache klines (corta dup intra-scan)
PERP_BASES_TTL = 3600  # universo de perps muda devagar; 1h de cache basta

_BLACKLIST_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDS",
    "USD1", "RLUSD", "PYUSD", "USDD", "USTC",
}

_client: Optional[httpx.AsyncClient] = None
_top_cache: Dict[str, tuple] = {}
_ticker_cache: Dict[str, tuple] = {}
_ohlcv_cache: Dict[str, tuple] = {}
_perp_bases_cache: Dict[str, tuple] = {}


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


async def _proxied_get(url: str, params: Optional[dict] = None) -> httpx.Response:
    """GET pelo proxy compartilhado COM o rate-gate do binance_signed_service.
    Este caminho público (klines/ticker) sai pelo MESMO IP das chamadas
    assinadas, então PRECISA respeitar o mesmo controle de peso/ban — senão
    estoura o limite do IP e escala o ban (era a causa raiz do loop de ban).

      - banido → levanta RuntimeError (caller cai no fallback vision/spot e NÃO
        bate no IP banido);
      - throttling → dorme o necessário antes de chamar;
      - 418/429 → arma o ban global (+ alerta) e levanta;
      - sempre registra o x-mbx-used-weight-1m no freio compartilhado.
    """
    from services import binance_signed_service as _signed
    if not await _signed.await_rate_gate():
        raise RuntimeError("rate-limit ban ativo — pulando chamada pública (anti-escalada)")
    r = await _get_client().get(url, params=params)
    try:
        uw = r.headers.get("x-mbx-used-weight-1m") or r.headers.get("X-MBX-USED-WEIGHT-1M")
        _signed.record_external_weight(uw)
    except Exception:  # noqa: BLE001
        pass
    if r.status_code in (418, 429):
        retry_after = None
        try:
            retry_after = float(r.headers.get("Retry-After") or 0) or None
        except (TypeError, ValueError):
            retry_after = None
        _signed.arm_ban_external(r.status_code, retry_after, origin="público")
        raise RuntimeError(f"rate-limit {r.status_code} no caminho público — ban armado")
    r.raise_for_status()
    return r


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

    r = await _proxied_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr")
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
    """OHLCV da Binance Futures (klines). Cache curto (OHLCV_TTL) corta klines
    redundantes no mesmo ciclo de scan (ex.: best-TF + análise pegam o mesmo
    symbol/TF), aliviando o peso no IP compartilhado."""
    if not PROXY_ENABLED:
        raise RuntimeError("BINANCE_PROXY_URL não configurado")
    fut_sym = to_fut(symbol)
    cache_key = f"{fut_sym}|{timeframe}|{limit}"
    now = time.time()
    hit = _ohlcv_cache.get(cache_key)
    if hit and (now - hit[0]) < OHLCV_TTL:
        return hit[1].copy()
    r = await _proxied_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        params={"symbol": fut_sym, "interval": timeframe, "limit": limit},
    )
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    _ohlcv_cache[cache_key] = (now, df)
    return df.copy()


async def fetch_ticker(symbol: str) -> Dict:
    if not PROXY_ENABLED:
        raise RuntimeError("BINANCE_PROXY_URL não configurado")
    fut_sym = to_fut(symbol)
    now = time.time()
    if fut_sym in _ticker_cache:
        ts, data = _ticker_cache[fut_sym]
        if now - ts < TICKER_TTL:
            return data
    r = await _proxied_get(
        f"{FAPI_BASE}/fapi/v1/ticker/24hr", params={"symbol": fut_sym}
    )
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
        r = await _proxied_get(
            f"{FAPI_BASE}/fapi/v1/premiumIndex", params={"symbol": fut_sym}
        )
        j = r.json()
        return float(j.get("lastFundingRate", 0))
    except Exception:
        return None


async def fetch_open_interest(symbol: str) -> Optional[float]:
    if not PROXY_ENABLED:
        return None
    fut_sym = to_fut(symbol)
    try:
        r = await _proxied_get(
            f"{FAPI_BASE}/fapi/v1/openInterest", params={"symbol": fut_sym}
        )
        j = r.json()
        return float(j.get("openInterest", 0))
    except Exception:
        return None


def perp_bases_source() -> str:
    """Origem do último set de perp bases servido: 'live' (fapi ao vivo) ou
    'snapshot' (fallback embutido — DEV geobloqueado). 'none' se nunca buscou."""
    return _perp_bases_cache.get("source", "none")


async def fetch_perp_tradeable_bases() -> Optional[Set[str]]:
    """Set de BASES (normalizadas, sem prefixo '1000') com par PERPÉTUO USDT em
    status TRADING na Binance Futures (via /fapi/v1/exchangeInfo).

    Serve pra cruzar com o ranking de backtest e marcar `perp_tradeable`: a
    allowlist do bot é de PERPS, mas o backtest enumera SPOT (binance.vision),
    então tickers só-spot / delistados / rebrandeados (ex.: TOMO, TVK) viram
    candidatos fantasma. Aqui a gente filtra.

    Usa o proxy de egress se configurado (PRD); senão tenta direto (DEV). Se o
    fetch ao vivo falhar (geoblock 451 no DEV, timeout), cai no SNAPSHOT embutido
    (perp_bases_snapshot.PERP_BASES_SNAPSHOT) — assim o DEV ainda marca
    perp_tradeable. Cache de 1h. `perp_bases_source()` diz a origem."""
    now = time.time()
    cached = _perp_bases_cache.get("set")
    if cached and now - cached[0] < PERP_BASES_TTL:
        return cached[1]

    own_client = False
    try:
        if PROXY_ENABLED:
            client = _get_client()
        else:
            client = httpx.AsyncClient(
                timeout=20.0, headers={"User-Agent": "CryptoAgent/1.0"}
            )
            own_client = True
        try:
            r = await client.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            d = r.json()
        finally:
            if own_client:
                await client.aclose()
        bases: Set[str] = set()
        for s in d.get("symbols", []):
            if (s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"):
                b = (s.get("baseAsset") or "").upper()
                if b.startswith("1000") and len(b) > 4:
                    b = b[4:]
                if b:
                    bases.add(b)
        if bases:
            _perp_bases_cache["set"] = (now, bases)
            _perp_bases_cache["source"] = "live"
            return bases
        raise ValueError("exchangeInfo sem símbolos perp")
    except Exception as e:
        log.warning(f"[perp-bases] fetch ao vivo falhou ({e}); usando snapshot.")
        # Cache vencido ainda é melhor que snapshot estático.
        if cached:
            return cached[1]
        try:
            from services.perp_bases_snapshot import PERP_BASES_SNAPSHOT
            _perp_bases_cache["set"] = (now, PERP_BASES_SNAPSHOT)
            _perp_bases_cache["source"] = "snapshot"
            return PERP_BASES_SNAPSHOT
        except Exception as e2:
            log.error(f"[perp-bases] snapshot indisponível: {e2}")
            return None


_perp_onboard_cache: dict = {}


async def fetch_perp_onboard_dates() -> dict:
    """Mapa {BASE: onboardDate_ms} dos perps USDT TRADING (via exchangeInfo).
    Serve pra ORDENAR o sweep por histórico (listagem mais antiga = mais dados =
    backtest mais confiável). Mesma via/proxy de fetch_perp_tradeable_bases. Se
    falhar, devolve {} (chamador cai na ordem padrão por volume). Cache 1h."""
    now = time.time()
    cached = _perp_onboard_cache.get("map")
    if cached and now - cached[0] < PERP_BASES_TTL:
        return cached[1]
    own_client = False
    try:
        if PROXY_ENABLED:
            client = _get_client()
        else:
            client = httpx.AsyncClient(
                timeout=20.0, headers={"User-Agent": "CryptoAgent/1.0"}
            )
            own_client = True
        try:
            r = await client.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            d = r.json()
        finally:
            if own_client:
                await client.aclose()
        out: dict = {}
        for s in d.get("symbols", []):
            if (s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"):
                b = (s.get("baseAsset") or "").upper()
                if b.startswith("1000") and len(b) > 4:
                    b = b[4:]
                ob = s.get("onboardDate")
                if b and ob is not None:
                    # se a base repetir, fica com a listagem mais antiga
                    if b not in out or ob < out[b]:
                        out[b] = ob
        if out:
            _perp_onboard_cache["map"] = (now, out)
        return out
    except Exception as e:
        log.warning(f"[perp-onboard] fetch falhou ({e}); ordenação por histórico indisponível.")
        return cached[1] if cached else {}


_perp_symbols_cache: dict = {}


async def fetch_perp_symbols_exchangeinfo() -> List[str]:
    """Lista de símbolos CCXT ('1000BONK/USDT:USDT') de TODOS os perps USDT
    TRADING, via exchangeInfo (weight 1). MANTÉM o prefixo 1000 (usa from_fut,
    igual a fetch_top_volume_symbols) → casa com o símbolo gravado em
    symbol_backtest_stats E com o arquivo do data.binance.vision. Enumeração
    LEVE pro sweep bulk não bater no ticker/24hr (weight 40). Cache 1h."""
    now = time.time()
    cached = _perp_symbols_cache.get("list")
    if cached and now - cached[0] < PERP_BASES_TTL:
        return cached[1]
    own_client = False
    try:
        if PROXY_ENABLED:
            client = _get_client()
        else:
            client = httpx.AsyncClient(
                timeout=20.0, headers={"User-Agent": "CryptoAgent/1.0"}
            )
            own_client = True
        try:
            r = await client.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
            r.raise_for_status()
            d = r.json()
        finally:
            if own_client:
                await client.aclose()
        syms: List[str] = []
        for s in d.get("symbols", []):
            if (s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"):
                fut = s.get("symbol") or ""
                if fut:
                    syms.append(from_fut(fut))
        if syms:
            _perp_symbols_cache["list"] = (now, syms)
        return syms
    except Exception as e:
        log.warning(f"[perp-symbols] exchangeInfo falhou ({e}).")
        return cached[1] if cached else []
