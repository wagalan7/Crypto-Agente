"""
Recommendation Service — varre top-N perpétuos USDT por volume, escolhe o
melhor TF para cada (maior score composto), filtra por qualidade e classifica
em 3 tiers (A+, A, B).

Score composto:
  - confluence.pct          (0–100, peso forte)
  - mtf.alignment_score     (-1 a +1, normalizado pra 0–100)
  - trade_plan.risk_reward  (cap em 5, normalizado pra 0–100)
  - pattern_stats win_rate  (bonus se amostra ≥ 10)

Tiers:
  - A+ : score ≥ 80, MTF score ≥ 0.5, R:R ≥ 2.5, zero warnings críticos
  - A  : score ≥ 70, MTF score ≥ 0.0, R:R ≥ 2.0
  - B  : score ≥ 55, R:R ≥ 1.5

Cache: 90s.
"""
from __future__ import annotations
import asyncio
import time
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

from services.binance_service import fetch_top_volume_symbols, fetch_ohlcv, fetch_ticker
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns
from services.signal_service import build_trade_signal, determine_direction
from services.derivatives_service import analyze_derivatives
from services.mtf_service import analyze_mtf
from models.trade_signal import TradeSignal, SignalDirection


SCAN_TFS = ["15m", "1h", "4h"]   # TFs varridos por símbolo
CACHE_TTL = 90                    # segundos
MIN_RR = 1.5                      # filtro mínimo absoluto
MIN_CONFIDENCE_B = 0.55           # tier B mínimo


class Recommendation(BaseModel):
    tier: str                     # "A+" | "A" | "B"
    score: float                  # 0–100 composto
    symbol: str
    timeframe: str
    direction: str                # long | short
    confidence: float
    risk_reward: float
    entry: float
    stop_loss: float
    tp2: float
    summary: str                  # 1 linha PT-BR (principal razão)
    warnings: List[str] = []
    signal: TradeSignal           # objeto completo (frontend usa pra carregar painel)


_cache: Dict[str, Any] = {"ts": 0, "data": None}


def _compute_score(sig: TradeSignal) -> float:
    """Score 0–100. Combina confluence + MTF + R:R + win-rate histórico."""
    conf_score = (sig.confluence.pct if sig.confluence else sig.confidence * 100)
    mtf_score = 50.0
    if sig.mtf:
        # mtf.alignment_score vai de -1 a +1 → mapeia pra 0–100
        mtf_score = (sig.mtf.get("alignment_score", 0) + 1) * 50
    rr_score = min(sig.risk_reward / 5.0, 1.0) * 100
    win_bonus = 0.0
    if sig.pattern_stats and sig.pattern_stats.get("stats"):
        win_rates = [
            s.get("win_rate", 0) for s in sig.pattern_stats["stats"].values()
            if s.get("occurrences", 0) >= 10
        ]
        if win_rates:
            avg = sum(win_rates) / len(win_rates)
            win_bonus = (avg - 0.5) * 20   # ±10 pontos
    # Pesos: confluence 0.45, MTF 0.30, R:R 0.20, win-rate ±5
    score = conf_score * 0.45 + mtf_score * 0.30 + rr_score * 0.20 + win_bonus * 0.5
    return round(max(0.0, min(100.0, score)), 1)


def _classify_tier(sig: TradeSignal, score: float) -> Optional[str]:
    """Retorna 'A+' | 'A' | 'B' | None (rejeitado)."""
    if sig.direction == SignalDirection.NEUTRAL:
        return None
    if sig.risk_reward < MIN_RR:
        return None

    mtf_score = sig.mtf.get("alignment_score", 0) if sig.mtf else 0
    has_critical_warning = False
    if sig.trade_plan and sig.trade_plan.get("quality_warnings"):
        # Considera crítico se contém "R:R" abaixo do mínimo
        for w in sig.trade_plan["quality_warnings"]:
            if "R:R" in w and "abaixo" in w:
                has_critical_warning = True
                break

    if score >= 80 and mtf_score >= 0.5 and sig.risk_reward >= 2.5 and not has_critical_warning:
        return "A+"
    if score >= 70 and mtf_score >= 0.0 and sig.risk_reward >= 2.0:
        return "A"
    if score >= 55 and sig.confidence >= MIN_CONFIDENCE_B and sig.risk_reward >= MIN_RR:
        return "B"
    return None


def _build_summary(sig: TradeSignal) -> str:
    bits = []
    dir_word = "LONG" if sig.direction == SignalDirection.LONG else "SHORT"
    bits.append(f"{dir_word} {sig.timeframe}")
    if sig.confluence:
        bits.append(f"confluência {sig.confluence.pct:.0f}%")
    if sig.mtf and sig.mtf.get("aligned_count") is not None:
        bits.append(f"MTF {sig.mtf['aligned_count']}/{len(sig.mtf.get('higher_tfs', []))}")
    bits.append(f"R:R 1:{sig.risk_reward}")
    if sig.trade_plan and sig.trade_plan.get("entry_zone"):
        zone_type = sig.trade_plan["entry_zone"].get("type", "")
        if zone_type and zone_type != "market":
            label_map = {
                "limit_pullback": "pullback EMA/VWAP",
                "limit_ob": "Order Block",
                "limit_fvg_fill": "preenchimento FVG",
                "limit_value_area": "Value Area",
                "limit_retest": "retest breakout",
            }
            bits.append(label_map.get(zone_type, zone_type))
    return " · ".join(bits)


async def _analyze_symbol_tf(symbol: str, tf: str) -> Optional[TradeSignal]:
    """Retorna o TradeSignal completo para (symbol, tf) ou None se falhar."""
    try:
        df = await fetch_ohlcv(symbol, tf, 300)
        if df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        current = float(df["close"].iloc[-1])
        primary_dir = determine_direction(ind, patterns, current)

        # Derivativos + MTF em paralelo
        try:
            ticker = await fetch_ticker(symbol)
            change_24h = ticker.get("change", 0.0)
        except Exception:
            change_24h = 0.0

        try:
            derivatives, mtf = await asyncio.gather(
                analyze_derivatives(symbol, change_24h),
                analyze_mtf(symbol, tf, primary_dir),
                return_exceptions=True,
            )
            if isinstance(derivatives, Exception):
                derivatives = None
            if isinstance(mtf, Exception):
                mtf = None
        except Exception:
            derivatives = None
            mtf = None

        # with_backtest=False evita martelar — recomendação não precisa de stats finos
        return build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=derivatives, mtf=mtf, with_backtest=False,
        )
    except Exception:
        return None


async def _best_tf_for_symbol(symbol: str) -> Optional[tuple]:
    """Roda SCAN_TFS em paralelo, devolve (signal, score) do melhor TF."""
    results = await asyncio.gather(*[_analyze_symbol_tf(symbol, tf) for tf in SCAN_TFS])
    scored: List[tuple] = []
    for sig in results:
        if sig is None or sig.direction == SignalDirection.NEUTRAL:
            continue
        score = _compute_score(sig)
        scored.append((sig, score))
    if not scored:
        return None
    return max(scored, key=lambda x: x[1])


async def get_recommendations(top_n: int = 30) -> List[Recommendation]:
    """Endpoint principal. Cacheado por 90s."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    try:
        symbols = await fetch_top_volume_symbols(limit=top_n)
    except Exception:
        symbols = []
    if not symbols:
        return []

    # Limita concorrência pra não saturar API (chunks de 6)
    recommendations: List[Recommendation] = []
    sem = asyncio.Semaphore(6)

    async def _bounded(sym: str):
        async with sem:
            return sym, await _best_tf_for_symbol(sym)

    all_results = await asyncio.gather(*[_bounded(s) for s in symbols])

    for symbol, best in all_results:
        if best is None:
            continue
        sig, score = best
        tier = _classify_tier(sig, score)
        if tier is None:
            continue
        recommendations.append(Recommendation(
            tier=tier,
            score=score,
            symbol=sig.symbol,
            timeframe=sig.timeframe,
            direction=sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction),
            confidence=sig.confidence,
            risk_reward=sig.risk_reward,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            tp2=sig.tp2,
            summary=_build_summary(sig),
            warnings=(sig.trade_plan or {}).get("quality_warnings", []) if sig.trade_plan else [],
            signal=sig,
        ))

    # Ordena por tier (A+ > A > B) e depois score
    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))

    _cache["ts"] = now
    _cache["data"] = recommendations
    return recommendations
