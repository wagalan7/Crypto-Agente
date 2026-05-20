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
    # ── Gestão de risco / alavancagem ─────────────────────────────────────
    leverage: int = 1             # alavancagem sugerida (inteiro)
    risk_pct: float = 1.0         # % da banca arriscado por trade
    margin_pct: float = 10.0      # % da banca a usar como margem
    stop_distance_pct: float = 0.0  # distância do entry até o stop em %


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


def _compute_leverage(entry: float, stop_loss: float, tier: str) -> dict:
    """
    Calcula alavancagem sugerida com gestão de risco proporcional ao tier.

    Modelo: usuário aloca ~10% da banca como margem isolada. A alavancagem é
    dimensionada para que, se o stop bater, a perda total seja `risk_pct`%
    da banca. Cap de segurança por tier (A+ mais agressivo, B mais conservador).

    Fórmula: leverage = risk_pct / (margin_pct × stop_dist_pct), tudo em frações.
    """
    if entry <= 0 or stop_loss <= 0:
        return {"leverage": 1, "risk_pct": 1.0, "margin_pct": 10.0, "stop_dist_pct": 0.0}
    stop_dist = abs(entry - stop_loss) / entry
    if stop_dist <= 0:
        return {"leverage": 1, "risk_pct": 1.0, "margin_pct": 10.0, "stop_dist_pct": 0.0}

    # Por tier: risco que aceitamos perder e teto de alavancagem
    profile = {
        "A+": {"risk_pct": 1.5, "cap": 15},
        "A":  {"risk_pct": 1.0, "cap": 10},
        "B":  {"risk_pct": 0.5, "cap": 5},
    }.get(tier, {"risk_pct": 1.0, "cap": 5})

    margin_pct = 10.0  # 10% da banca como margem isolada
    risk_frac = profile["risk_pct"] / 100
    margin_frac = margin_pct / 100

    raw_lev = risk_frac / (margin_frac * stop_dist)
    leverage = max(1, min(profile["cap"], int(round(raw_lev))))

    return {
        "leverage": leverage,
        "risk_pct": profile["risk_pct"],
        "margin_pct": margin_pct,
        "stop_dist_pct": round(stop_dist * 100, 3),
    }


def _build_recommendation(sig: TradeSignal, score: float, tier: str) -> Recommendation:
    lev = _compute_leverage(sig.entry, sig.stop_loss, tier)
    return Recommendation(
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
        leverage=lev["leverage"],
        risk_pct=lev["risk_pct"],
        margin_pct=lev["margin_pct"],
        stop_distance_pct=lev["stop_dist_pct"],
    )


async def _analyze_candles_for_tf(symbol: str, tf: str, df) -> Optional[TradeSignal]:
    """Variante de _analyze_symbol_tf que recebe candles já baixados (frontend)."""
    try:
        if df is None or df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        current = float(df["close"].iloc[-1])
        primary_dir = determine_direction(ind, patterns, current)

        # Derivativos + MTF: backend ainda chama OKX pra esses dois (rate-limit-friendly).
        # Se falhar (símbolo só existe na Bybit), seguimos sem.
        try:
            from services.binance_service import fetch_ticker
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

        return build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=derivatives, mtf=mtf, with_backtest=False,
        )
    except Exception:
        return None


async def get_recommendations_from_batch(
    items: List[dict],
) -> List[Recommendation]:
    """
    Entrada: lista de {symbol, timeframe, candles: [{timestamp,open,high,low,close,volume}, ...]}
    O frontend baixa candles direto da Bybit e envia em lote — backend só processa.
    Agrupa por símbolo, escolhe o melhor TF, classifica em tiers.
    """
    import pandas as pd

    # Agrupa por símbolo
    per_symbol: Dict[str, List[tuple]] = {}
    for item in items:
        symbol = item.get("symbol")
        tf = item.get("timeframe")
        candles = item.get("candles", [])
        if not symbol or not tf or len(candles) < 80:
            continue
        try:
            df = pd.DataFrame(candles)
            df = df.astype({
                "timestamp": int, "open": float, "high": float,
                "low": float, "close": float, "volume": float,
            })
        except Exception:
            continue
        per_symbol.setdefault(symbol, []).append((tf, df))

    if not per_symbol:
        return []

    # Limita concorrência por símbolo (3 TFs em paralelo, 6 símbolos em paralelo)
    sem_sym = asyncio.Semaphore(6)

    async def _process_symbol(symbol: str, tfs: List[tuple]) -> Optional[tuple]:
        async with sem_sym:
            results = await asyncio.gather(*[
                _analyze_candles_for_tf(symbol, tf, df) for tf, df in tfs
            ])
            scored = []
            for sig in results:
                if sig is None or sig.direction == SignalDirection.NEUTRAL:
                    continue
                scored.append((sig, _compute_score(sig)))
            if not scored:
                return None
            return max(scored, key=lambda x: x[1])

    best_per_symbol = await asyncio.gather(*[
        _process_symbol(sym, tfs) for sym, tfs in per_symbol.items()
    ])

    recommendations: List[Recommendation] = []
    for best in best_per_symbol:
        if best is None:
            continue
        sig, score = best
        tier = _classify_tier(sig, score)
        if tier is None:
            continue
        recommendations.append(_build_recommendation(sig, score, tier))

    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))
    return recommendations


async def _analyze_symbol_tf_via_vision(symbol: str, tf: str) -> Optional[TradeSignal]:
    """Variante de _analyze_symbol_tf que usa Binance Vision (spot). Pula
    derivativos (não tem em spot) e MTF (consistência da fonte) — vai
    pontuar levemente abaixo, mas aceitável pro server-scan."""
    from services import binance_vision_service as bvs
    try:
        df = await bvs.fetch_ohlcv(symbol, tf, 300)
        if df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        return build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=None, mtf=None, with_backtest=False,
        )
    except Exception:
        return None


async def _best_tf_for_symbol_via_vision(symbol: str) -> Optional[tuple]:
    results = await asyncio.gather(*[
        _analyze_symbol_tf_via_vision(symbol, tf) for tf in SCAN_TFS
    ])
    scored: List[tuple] = []
    for sig in results:
        if sig is None or sig.direction == SignalDirection.NEUTRAL:
            continue
        score = _compute_score(sig)
        scored.append((sig, score))
    if not scored:
        return None
    return max(scored, key=lambda x: x[1])


async def get_recommendations_via_vision(top_n: int = 30) -> List[Recommendation]:
    """Versão server-side via Binance Spot (data-api.binance.vision).
    Cache próprio (não compartilha com get_recommendations)."""
    from services import binance_vision_service as bvs
    try:
        symbols = await bvs.fetch_top_volume_symbols(limit=top_n)
    except Exception:
        symbols = []
    if not symbols:
        return []

    recommendations: List[Recommendation] = []
    sem = asyncio.Semaphore(6)

    async def _bounded(sym: str):
        async with sem:
            return sym, await _best_tf_for_symbol_via_vision(sym)

    all_results = await asyncio.gather(*[_bounded(s) for s in symbols])

    for _symbol, best in all_results:
        if best is None:
            continue
        sig, score = best
        tier = _classify_tier(sig, score)
        if tier is None:
            continue
        recommendations.append(_build_recommendation(sig, score, tier))

    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))
    return recommendations


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
        recommendations.append(_build_recommendation(sig, score, tier))

    # Ordena por tier (A+ > A > B) e depois score
    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))

    _cache["ts"] = now
    _cache["data"] = recommendations
    return recommendations
