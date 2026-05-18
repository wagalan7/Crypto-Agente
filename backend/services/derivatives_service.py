"""
Análise de derivativos: Funding Rate + Open Interest.

Funding rate alto/positivo = longs pagando shorts (excesso de comprados,
risco de long-squeeze). Negativo = inverso.
OI crescendo com preço subindo = dinheiro novo entrando (saudável).
OI crescendo com preço caindo = shorts pesados (squeeze potencial).
"""
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel
import httpx

from services.binance_service import to_okx, fetch_funding_rate, fetch_open_interest

BASE = "https://www.okx.com"


class DerivativesData(BaseModel):
    funding_rate: Optional[float] = None         # ex: 0.0001 (0.01%)
    funding_rate_pct: Optional[float] = None     # já em %
    funding_sentiment: str = "neutral"           # bullish_squeeze | bearish_squeeze | neutral | extreme_long | extreme_short
    open_interest: Optional[float] = None
    oi_change_24h_pct: Optional[float] = None
    oi_sentiment: str = "neutral"                # bullish | bearish | neutral
    description: str = ""
    warnings: List[str] = []


async def _fetch_oi_history(symbol: str) -> Optional[float]:
    """Busca histórico de OI nas últimas 24h e retorna variação %."""
    try:
        inst_id = to_okx(symbol)
        # OKX rubik stats: period 1H, 24 valores
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BASE}/api/v5/rubik/stat/contracts/open-interest-volume",
                params={"ccy": inst_id.split("-")[0], "period": "1H"},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if len(data) < 24:
                return None
            # Cada item: [ts, oi, vol]. Mais recente último.
            oi_now = float(data[-1][1])
            oi_24h_ago = float(data[-24][1])
            if oi_24h_ago == 0:
                return None
            return ((oi_now - oi_24h_ago) / oi_24h_ago) * 100
    except Exception:
        return None


async def analyze_derivatives(symbol: str, price_change_24h: float = 0.0) -> DerivativesData:
    """
    Busca funding + OI e interpreta sentimento.
    `price_change_24h` em % é usado para cruzar com OI delta.
    """
    funding = await fetch_funding_rate(symbol)
    oi = await fetch_open_interest(symbol)
    oi_change = await _fetch_oi_history(symbol)

    data = DerivativesData(
        funding_rate=funding,
        funding_rate_pct=round(funding * 100, 4) if funding is not None else None,
        open_interest=oi,
        oi_change_24h_pct=round(oi_change, 2) if oi_change is not None else None,
    )

    # ── Funding sentiment ─────────────────────────────────────────────────
    if funding is not None:
        f_pct = funding * 100
        if f_pct > 0.05:
            data.funding_sentiment = "extreme_long"
            data.warnings.append(f"Funding extremamente positivo ({f_pct:.3f}%) — longs pagando muito, risco alto de long-squeeze.")
        elif f_pct > 0.02:
            data.funding_sentiment = "bullish_squeeze"
        elif f_pct < -0.05:
            data.funding_sentiment = "extreme_short"
            data.warnings.append(f"Funding extremamente negativo ({f_pct:.3f}%) — shorts pagando muito, risco alto de short-squeeze.")
        elif f_pct < -0.02:
            data.funding_sentiment = "bearish_squeeze"
        else:
            data.funding_sentiment = "neutral"

    # ── OI sentiment ──────────────────────────────────────────────────────
    if oi_change is not None:
        if oi_change > 5 and price_change_24h > 0:
            data.oi_sentiment = "bullish"  # dinheiro novo long entrando
        elif oi_change > 5 and price_change_24h < 0:
            data.oi_sentiment = "bearish"  # shorts adicionados pesados
            data.warnings.append("OI crescendo com preço caindo — pressão vendedora institucional.")
        elif oi_change < -5:
            data.oi_sentiment = "neutral"  # fechamento de posições
        else:
            data.oi_sentiment = "neutral"

    # ── Descrição PT-BR ───────────────────────────────────────────────────
    parts = []
    if data.funding_rate_pct is not None:
        parts.append(f"Funding {data.funding_rate_pct:+.3f}%/8h")
    if data.oi_change_24h_pct is not None:
        parts.append(f"OI {data.oi_change_24h_pct:+.1f}% (24h)")
    if data.funding_sentiment == "extreme_long":
        parts.append("longs sobreaquecidos")
    elif data.funding_sentiment == "extreme_short":
        parts.append("shorts sobreaquecidos")
    data.description = " · ".join(parts) if parts else "Sem dados de derivativos."

    return data
