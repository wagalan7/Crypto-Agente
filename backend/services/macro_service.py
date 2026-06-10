"""
Contexto macro: BTC dominance + DXY + S&P500 + Nasdaq via yfinance.
"""
from __future__ import annotations
import asyncio
import time
import httpx
from typing import Optional

_http: Optional[httpx.AsyncClient] = None

# Cache dos totais (TOTAL/TOTAL2/TOTAL3 + dominâncias). O /global muda devagar
# e o CoinGecko free tem rate limit baixo → cache de 10min.
_totals_cache: dict = {"ts": 0.0, "data": None}
_TOTALS_TTL = 600.0


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http


async def get_btc_dominance() -> Optional[float]:
    """Busca dominância do BTC via CoinGecko (sem API key)."""
    try:
        r = await _get_http().get("https://api.coingecko.com/api/v3/global")
        r.raise_for_status()
        pct = r.json()["data"]["market_cap_percentage"].get("btc")
        return round(float(pct), 2) if pct else None
    except Exception:
        return None


async def get_crypto_totals() -> dict:
    """
    Capitalização total do mercado cripto + dominâncias, via CoinGecko /global
    (a MESMA chamada do get_btc_dominance — aqui só extraímos mais campos).

    Retorna (USD):
      total_usd   = TOTAL  (cap. total do mercado)
      total2_usd  = TOTAL2 (exclui BTC)        = TOTAL × (1 − BTC.D)
      total3_usd  = TOTAL3 (exclui BTC e ETH)  = TOTAL × (1 − BTC.D − ETH.D)
      btc_dominance / eth_dominance / usdt_dominance (em %)

    Cache 10min. Fail-soft: retorna {} se a API falhar (não quebra o caller).
    """
    now = time.time()
    if _totals_cache["data"] is not None and (now - _totals_cache["ts"]) < _TOTALS_TTL:
        return _totals_cache["data"]
    try:
        r = await _get_http().get("https://api.coingecko.com/api/v3/global")
        r.raise_for_status()
        d = r.json()["data"]
        total = float(d["total_market_cap"]["usd"])
        mcp = d.get("market_cap_percentage", {}) or {}
        btc_d = float(mcp.get("btc") or 0.0)
        eth_d = float(mcp.get("eth") or 0.0)
        usdt_d = float(mcp.get("usdt") or 0.0)
        out = {
            "total_usd": total,
            "total2_usd": total * (1.0 - btc_d / 100.0),
            "total3_usd": total * (1.0 - (btc_d + eth_d) / 100.0),
            "btc_dominance": round(btc_d, 2),
            "eth_dominance": round(eth_d, 2),
            "usdt_dominance": round(usdt_d, 2),
        }
        _totals_cache["data"] = out
        _totals_cache["ts"] = now
        return out
    except Exception:
        return {}


async def get_global_market_data() -> dict:
    """
    Retorna DXY, S&P500 e Nasdaq (variação % do último dia).
    Usa yfinance em thread separada para não bloquear o event loop.
    """
    def _fetch():
        try:
            import yfinance as yf
            result = {}
            tickers_map = {"dxy": "^DXY", "sp500": "^GSPC", "nasdaq": "^IXIC"}
            for key, ticker in tickers_map.items():
                try:
                    data = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=True)
                    if data is not None and not data.empty and len(data) >= 2:
                        # Handle MultiIndex columns from yfinance
                        close_col = None
                        if hasattr(data.columns, 'levels'):
                            # MultiIndex: ('Close', '^DXY') etc.
                            for col in data.columns:
                                if col[0] == 'Close':
                                    close_col = col
                                    break
                        else:
                            if 'Close' in data.columns:
                                close_col = 'Close'
                        if close_col is None:
                            continue
                        last = float(data[close_col].iloc[-1])
                        prev = float(data[close_col].iloc[-2])
                        change = (last - prev) / prev * 100 if prev else 0
                        result[key] = {"price": round(last, 2), "change": round(change, 2)}
                except Exception:
                    pass
            return result
        except ImportError:
            return {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return {}


def build_macro_context(
    btc_direction: str,
    btc_rsi: Optional[float],
    btc_adx: Optional[float],
    btc_supertrend: Optional[int],
    btc_dominance: Optional[float],
    symbol: str,
    market_data: Optional[dict] = None,
) -> str:
    md = market_data or {}
    lines = ["=== CONTEXTO MACRO ==="]

    # BTC estrutura
    lines.append(f"BTC Viés: {btc_direction.upper()}")
    if btc_rsi:
        zone = "sobrevendido" if btc_rsi < 30 else "sobrecomprado" if btc_rsi > 70 else "neutro"
        lines.append(f"BTC RSI: {btc_rsi:.1f} ({zone})")
    if btc_adx:
        strength = "tendência forte" if btc_adx > 25 else "sem tendência"
        lines.append(f"BTC ADX: {btc_adx:.1f} ({strength})")
    if btc_supertrend is not None:
        lines.append(f"BTC Supertrend: {'ALTA' if btc_supertrend == 1 else 'BAIXA'}")

    # Dominância BTC
    if btc_dominance:
        lines.append(f"Dominância BTC: {btc_dominance:.1f}%")
        if btc_dominance > 55:
            lines.append("→ Alta dominância: BTC favorecido sobre altcoins.")
        elif btc_dominance < 45:
            lines.append("→ Baixa dominância: possível rotação para altcoins.")

    # Capitalização total do mercado (TOTAL / TOTAL2 / TOTAL3) + USDT.D
    total = md.get("total_usd")
    if total:
        def _t(v) -> str:
            try:
                v = float(v)
            except (TypeError, ValueError):
                return "?"
            if v >= 1e12:
                return f"${v / 1e12:.2f}T"
            if v >= 1e9:
                return f"${v / 1e9:.1f}B"
            return f"${v:,.0f}"
        lines.append(f"Cap. Total (TOTAL): {_t(total)}")
        if md.get("total2_usd"):
            lines.append(f"Cap. Alts ex-BTC (TOTAL2): {_t(md['total2_usd'])}")
        if md.get("total3_usd"):
            lines.append(f"Cap. Alts ex-BTC/ETH (TOTAL3): {_t(md['total3_usd'])}")
        usdt_d = md.get("usdt_dominance")
        if usdt_d:
            lines.append(f"Dominância USDT (USDT.D): {usdt_d:.2f}%")
            if usdt_d > 5.0:
                lines.append("→ USDT.D elevada: capital parado em stablecoin (risk-off / medo).")
            elif usdt_d < 3.5:
                lines.append("→ USDT.D baixa: capital alocado em risco (risk-on).")

    # DXY
    dxy = md.get("dxy")
    if dxy:
        trend = "fortalecendo" if dxy["change"] > 0 else "enfraquecendo"
        lines.append(f"DXY (Dólar): {dxy['price']:.2f} ({'+' if dxy['change'] >= 0 else ''}{dxy['change']:.2f}%) — dólar {trend}")
        if dxy["change"] > 0.5:
            lines.append("→ Dólar forte: pressão vendedora em cripto geralmente.")
        elif dxy["change"] < -0.5:
            lines.append("→ Dólar fraco: ambiente favorável a ativos de risco.")

    # S&P 500
    sp = md.get("sp500")
    if sp:
        lines.append(f"S&P 500: {sp['price']:,.0f} ({'+' if sp['change'] >= 0 else ''}{sp['change']:.2f}%)")
        if sp["change"] > 1:
            lines.append("→ Bolsa americana em alta: apetite por risco elevado.")
        elif sp["change"] < -1:
            lines.append("→ Bolsa americana em queda: aversão a risco, cuidado com cripto.")

    # Nasdaq
    nq = md.get("nasdaq")
    if nq:
        lines.append(f"Nasdaq: {nq['price']:,.0f} ({'+' if nq['change'] >= 0 else ''}{nq['change']:.2f}%)")

    # Força relativa do par vs BTC
    if symbol != "BTC/USDT:USDT":
        base = symbol.split("/")[0]
        lines.append(f"Par analisado: {symbol} — verificar força relativa de {base} vs BTC no TF 1D.")

    return "\n".join(lines)
