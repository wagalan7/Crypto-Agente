"""
Contexto macro: dominância BTC + análise BTC como referência de mercado.
"""
from __future__ import annotations
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


def build_macro_context(
    btc_direction: str,
    btc_rsi: Optional[float],
    btc_adx: Optional[float],
    btc_supertrend: Optional[int],
    btc_dominance: Optional[float],
    symbol: str,
) -> str:
    lines = ["=== CONTEXTO MACRO ==="]
    lines.append(f"BTC Viés: {btc_direction.upper()}")
    if btc_rsi:
        zone = "sobrevendido" if btc_rsi < 30 else "sobrecomprado" if btc_rsi > 70 else "neutro"
        lines.append(f"BTC RSI: {btc_rsi:.1f} ({zone})")
    if btc_adx:
        strength = "tendência forte" if btc_adx > 25 else "sem tendência"
        lines.append(f"BTC ADX: {btc_adx:.1f} ({strength})")
    if btc_supertrend is not None:
        lines.append(f"BTC Supertrend: {'ALTA' if btc_supertrend == 1 else 'BAIXA'}")
    if btc_dominance:
        lines.append(f"Dominância BTC: {btc_dominance:.1f}%")
        if btc_dominance > 55:
            lines.append("→ Alta dominância BTC favorece BTC sobre altcoins.")
        elif btc_dominance < 45:
            lines.append("→ Baixa dominância BTC pode indicar rotação para altcoins.")
    if symbol != "BTC/USDT:USDT":
        lines.append(f"Nota: {symbol} correlacionado ao BTC — confirmar força relativa.")
    return "\n".join(lines)
