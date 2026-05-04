"""
Contexto macro: BTC dominance + DXY + S&P500 + Nasdaq via yfinance.
"""
from __future__ import annotations
import asyncio
import httpx
from typing import Optional

_http: Optional[httpx.AsyncClient] = None


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
