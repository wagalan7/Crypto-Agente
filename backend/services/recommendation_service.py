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
    # ── Entry zone (sprint B do entry_planner) + flag de chase ────────────
    entry_zone_low: Optional[float] = None    # piso da zona de pullback
    entry_zone_high: Optional[float] = None   # teto da zona
    entry_zone_type: Optional[str] = None     # "limit_pullback" | "limit_ob" | "market" | ...
    current_price: Optional[float] = None     # preço de mercado no momento da varredura
    chase_atr: Optional[float] = None         # múltiplos de ATR entre current_price e entry (signed a favor)
    chase_level: Optional[str] = None         # "ok" | "extended" | "chasing"


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


def _has_liquidity_squeeze_risk(sig: TradeSignal) -> bool:
    """
    Detecta cluster de liquidação iminente contra a direção do trade.

    Heurística: funding rate extremo na MESMA direção do trade → posições
    muito alavancadas a favor → squeeze provável. Long com funding >0.05%
    (longs pagando muito → muitos longs) ou short com funding <-0.05%
    (shorts pagando → muitos shorts) viram alvo de caça de liquidez.

    Não bloqueia se funding extremo é CONTRA o trade — aí o squeeze
    favorece a entrada (short squeeze impulsiona long).
    """
    der = sig.derivatives
    if not der:
        return False
    sentiment = getattr(der, "funding_sentiment", None) or (
        der.get("funding_sentiment") if isinstance(der, dict) else None
    )
    if not sentiment:
        return False
    if sig.direction == SignalDirection.LONG and sentiment == "extreme_long":
        return True
    if sig.direction == SignalDirection.SHORT and sentiment == "extreme_short":
        return True
    return False


def _volume_gate_pass(sig: TradeSignal, tier: str) -> bool:
    """
    Volume confirmation por tier — quanto melhor o tier, mais exigente.
      A+: volume_ratio >= 1.0 (não pode estar abaixo da média)
      A:  volume_ratio >= 0.8
      B:  volume_ratio >= 0.6  (rejeita só fantasmas)
    Se volume_ratio = None (indicador antigo / sem dado), passa.
    """
    ind = sig.indicators
    if not ind:
        return True
    ratio = getattr(ind, "volume_ratio", None)
    if ratio is None:
        return True
    if tier == "A+" and ratio < 1.0:
        return False
    if tier == "A" and ratio < 0.8:
        return False
    if tier == "B" and ratio < 0.6:
        return False
    return True


def _is_chasing(sig: TradeSignal) -> bool:
    """
    Detecta "chasing": entrar depois do movimento já ter rodado.
    Se displacement das últimas 3 velas, no sentido do trade, > 2.0× ATR,
    o setup está estendido — risco alto de pullback estopar a entrada.
    """
    ind = sig.indicators
    if not ind:
        return False
    disp_atr = getattr(ind, "displacement_3c_atr", None)
    if disp_atr is None:
        return False
    if sig.direction == SignalDirection.LONG and disp_atr > 2.0:
        return True
    if sig.direction == SignalDirection.SHORT and disp_atr < -2.0:
        return True
    return False


# Volatilidade mínima por TF (ATR/preço). Abaixo disso, mercado está parado
# e o R esperado fica menor que o spread/slippage.
MIN_ATR_PCT_BY_TF = {
    "1m": 0.0008, "3m": 0.0010, "5m": 0.0012, "15m": 0.0018, "30m": 0.0025,
    "1h": 0.0030, "2h": 0.0040, "4h": 0.0050, "6h": 0.0060,
    "8h": 0.0070, "12h": 0.0080, "1d": 0.0100,
}


def _is_dead_market(sig: TradeSignal) -> bool:
    """True se ATR% abaixo do mínimo do TF (mercado lateral/morto)."""
    ind = sig.indicators
    if not ind:
        return False
    atr_pct = getattr(ind, "atr_pct", None)
    if atr_pct is None:
        return False
    threshold = MIN_ATR_PCT_BY_TF.get(sig.timeframe, 0.003)
    return atr_pct < threshold


def _has_confirming_pattern(sig: TradeSignal) -> bool:
    """True se existe pelo menos 1 padrão detectado alinhado à direção
    do trade (long pattern em LONG, short pattern em SHORT). Padrão é a
    diferença entre 'setup numérico' e 'estrutura visível' — A+ exige."""
    if not sig.patterns:
        return False
    for p in sig.patterns:
        if getattr(p, "direction", None) == sig.direction:
            return True
    return False


def _classify_tier(sig: TradeSignal, score: float) -> Optional[str]:
    """Retorna 'A+' | 'A' | 'B' | None (rejeitado)."""
    if sig.direction == SignalDirection.NEUTRAL:
        return None
    if sig.risk_reward < MIN_RR:
        return None

    # Liquidation squeeze risk: funding extremo a favor da posição → caça
    # de liquidez provável. Bloqueia completamente.
    if _has_liquidity_squeeze_risk(sig):
        return None

    # Anti-chase: setup já rodou demais nas últimas 3 velas. Entrar agora
    # é comprar o topo / vender o fundo do impulso → stop logo na primeira
    # correção. Rejeita.
    if _is_chasing(sig):
        return None

    # Min ATR%: mercado parado, R não compensa custo. Rejeita.
    if _is_dead_market(sig):
        return None

    mtf_score = sig.mtf.get("alignment_score", 0) if sig.mtf else 0
    has_critical_warning = False
    if sig.trade_plan and sig.trade_plan.get("quality_warnings"):
        # Considera crítico se contém "R:R" abaixo do mínimo
        for w in sig.trade_plan["quality_warnings"]:
            if "R:R" in w and "abaixo" in w:
                has_critical_warning = True
                break

    tier: Optional[str] = None
    if (
        score >= 80 and mtf_score >= 0.5 and sig.risk_reward >= 2.5
        and not has_critical_warning and _has_confirming_pattern(sig)
    ):
        tier = "A+"
    elif score >= 70 and mtf_score >= 0.0 and sig.risk_reward >= 2.0:
        tier = "A"
    elif score >= 55 and sig.confidence >= MIN_CONFIDENCE_B and sig.risk_reward >= MIN_RR:
        tier = "B"
    else:
        return None

    # Volume confirmation (downgrade em cascata, rejeita se nem B passa)
    while tier is not None and not _volume_gate_pass(sig, tier):
        if tier == "A+":
            tier = "A"
        elif tier == "A":
            tier = "B"
        else:
            return None
    return tier


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


def _apply_btc_correlation_throttle(
    recommendations: List["Recommendation"],
    regime: Dict[str, Any],
) -> None:
    """
    Quando BTC está indeciso (|24h pct| ≤ 1%) e temos vários alt LONGs,
    todos sofrem do mesmo risco correlato: uma reversão do BTC estopa
    todos juntos. Reduz risk_pct (e portanto leverage proporcionalmente)
    pra limitar exposição agregada.

    Throttle factors:
      ≥3 alt longs simultâneos: 0.66× risk_pct
      ≥5 alt longs simultâneos: 0.50× risk_pct
    Mutação in-place. Anexa warning explicando.
    """
    try:
        btc_24h = regime.get("btc_24h_pct")
        if btc_24h is None or abs(btc_24h) > 1.0:
            return  # BTC tem viés claro: correlação alts-BTC bate normal
        from services.regime_service import is_btc_symbol
        alt_longs = [
            r for r in recommendations
            if r.direction == "long" and not is_btc_symbol(r.symbol)
        ]
        if len(alt_longs) < 3:
            return
        factor = 0.5 if len(alt_longs) >= 5 else 0.66
        msg = (
            f"BTC indeciso ({btc_24h:+.2f}% / 24h) + {len(alt_longs)} alt longs "
            f"simultâneos: risco reduzido {int((1-factor)*100)}% por correlação"
        )
        for r in alt_longs:
            r.risk_pct = round(r.risk_pct * factor, 3)
            # Re-escala leverage proporcionalmente (mantém stop_dist)
            r.leverage = max(1, int(round(r.leverage * factor)))
            if msg not in (r.warnings or []):
                r.warnings = (r.warnings or []) + [msg]
        import logging as _log
        _log.info(
            f"[corr-throttle] aplicado em {len(alt_longs)} alt longs "
            f"(fator {factor})"
        )
    except Exception as e:
        import logging as _log
        _log.warning(f"[corr-throttle] falhou: {e}")


def _build_recommendation(sig: TradeSignal, score: float, tier: str) -> Optional[Recommendation]:
    # Supressão: se o preço atual já passou de TP1 a favor da direção,
    # o trade "foi embora" — não faz sentido recomendar entrada agora
    # (entry tá obsoleto e R:R restante ficou ruim).
    is_long_dir = (sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)) == "long"
    if sig.current_price is not None and sig.tp1:
        if is_long_dir and sig.current_price >= sig.tp1:
            return None
        if (not is_long_dir) and sig.current_price <= sig.tp1:
            return None

    lev = _compute_leverage(sig.entry, sig.stop_loss, tier)

    # Entry zone (do TradePlan, se houver) — usuário usa pra colocar limit order
    entry_zone_low = entry_zone_high = entry_zone_type = None
    tp = sig.trade_plan if isinstance(sig.trade_plan, dict) else None
    if tp and tp.get("entry_zone"):
        ez = tp["entry_zone"]
        entry_zone_low = ez.get("bottom")
        entry_zone_high = ez.get("top")
        entry_zone_type = ez.get("type")

    # Flag de "chase": preço atual já passou a favor da direção, demais do entry?
    # Mede em múltiplos de ATR (signed: positivo = a favor → mais chase).
    chase_atr = None
    chase_level = None
    cp = sig.current_price
    atr = sig.indicators.atr if sig.indicators else None
    is_long = (sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)) == "long"
    if cp is not None and atr and atr > 0 and sig.entry:
        delta = (cp - sig.entry) if is_long else (sig.entry - cp)
        chase_atr = round(delta / atr, 2)
        if chase_atr >= 0.8:
            chase_level = "chasing"     # 🔴 já passou demais, espera pullback
        elif chase_atr >= 0.4:
            chase_level = "extended"    # 🟡 levemente estendido, ainda aceitável
        else:
            chase_level = "ok"          # 🟢 perto do entry ou abaixo

    warnings = (tp or {}).get("quality_warnings", []) if tp else []
    if chase_level == "chasing":
        warnings = warnings + [
            f"⚠ Preço {chase_atr}×ATR acima do entry — esperar pullback à zona ou pular."
        ]

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
        warnings=warnings,
        signal=sig,
        leverage=lev["leverage"],
        risk_pct=lev["risk_pct"],
        margin_pct=lev["margin_pct"],
        stop_distance_pct=lev["stop_dist_pct"],
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        entry_zone_type=entry_zone_type,
        current_price=cp,
        chase_atr=chase_atr,
        chase_level=chase_level,
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

    # News blackout: durante janela de FOMC/CPI/NFP etc. NÃO gera novas recs.
    # Volatilidade extrema estopa setups técnicos bons. Falha aberta (sem
    # bloquear) se o filtro estiver desabilitado ou sem dados.
    try:
        from services import news_filter_service as nfs
        blackout = await nfs.get_blackout_status()
    except Exception as e:
        import logging as _log
        _log.warning(f"[news] check falhou (fail-open): {e}")
        blackout = {"active": False}

    recommendations: List[Recommendation] = []
    if blackout.get("active"):
        import logging as _log
        _log.info(
            f"[news] BLACKOUT ativo: {blackout.get('event')} ({blackout.get('country')}) "
            f"— retomando em {blackout.get('minutes_until_resume')}min. "
            "Nenhuma rec será gerada nesta janela."
        )
        return recommendations

    # Macro regime gate: bloqueia tudo se RISK_OFF; alt longs se ALT_DANGER.
    try:
        from services import regime_service as rs
        regime = await rs.get_regime_status()
    except Exception as e:
        import logging as _log
        _log.warning(f"[regime] check falhou (fail-open): {e}")
        regime = {"regime": "NORMAL", "block_all": False, "block_alt_longs": False, "downgrade_alt_longs": False}

    if regime.get("block_all"):
        import logging as _log
        _log.info(f"[regime] {regime.get('regime')} — bloqueia tudo: {regime.get('reasons')}")
        return recommendations

    # Cooldown por símbolo: bloqueia recs em símbolos que estoparam/expiraram
    # nas últimas 6h (evita "revenge entry" no mesmo nível derrotado).
    try:
        from services.snapshot_service import get_recently_stopped_symbols
        cooldown_symbols = await get_recently_stopped_symbols(hours=6)
    except Exception:
        cooldown_symbols = set()

    for best in best_per_symbol:
        if best is None:
            continue
        sig, score = best
        tier = _classify_tier(sig, score)
        if tier is None:
            continue

        # Cooldown: símbolo estopado nas últimas 6h → skip
        if sig.symbol in cooldown_symbols:
            import logging as _log
            _log.info(f"[cooldown] skip {sig.symbol}: estopado nas últimas 6h")
            continue

        # Regime filter por rec
        try:
            from services.regime_service import should_block_recommendation, is_btc_symbol
            block_reason = should_block_recommendation(regime, sig.symbol, sig.direction)
            if block_reason:
                import logging as _log
                _log.info(f"[regime] skip {sig.symbol} {sig.direction}: {block_reason}")
                continue
            # Downgrade alt longs em BTC_DOMINANT
            if regime.get("downgrade_alt_longs") and sig.direction == "long" and not is_btc_symbol(sig.symbol):
                if tier == "A+":
                    tier = "A"
                elif tier == "A":
                    tier = "B"
                elif tier == "B":
                    continue  # B downgrade vira reject
        except Exception:
            pass

        _rec = _build_recommendation(sig, score, tier)
        if _rec is not None:
            recommendations.append(_rec)

    # BTC correlation throttle: vários alt longs + BTC indeciso → reduz size
    _apply_btc_correlation_throttle(recommendations, regime)

    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))
    return recommendations


def _classify_tier_vision(sig: TradeSignal, score: float) -> Optional[str]:
    """
    Vision usa exatamente os MESMOS thresholds estritos do classifier
    principal — preferimos zero push do que push de setup fraco. Quando o
    spot gerar um setup que passe nos critérios rigorosos, aí sim dispara
    notificação. Menos pushes, mas cada um vale.
    """
    return _classify_tier(sig, score)


def _get_server_data_source():
    """
    Escolhe a fonte de dados pro server-scan, em ordem de preferência:
      1. Binance Futures via Cloudflare Worker (se BINANCE_PROXY_URL setado)
      2. binance_service (OKX perp) — DEFAULT. Bybit/Binance Futures dão 403
         do IP do Railway; OKX é a única exchange perp que aceita cloud IPs.
      3. Bybit V5 — só se DATA_SOURCE=bybit explícito (útil pra teste local
         ou se Bybit liberar IPs cloud no futuro)

    Painel via browser (RecommendationsPanel) continua usando Bybit
    independente — IP residencial do user passa sem geo-block.

    Override via env var: DATA_SOURCE=bybit (default okx).
    """
    import os
    from services import binance_futures_service as bfs
    if bfs.PROXY_ENABLED:
        return bfs, "binance-futures-proxy"

    preferred = os.getenv("DATA_SOURCE", "okx").strip().lower()
    if preferred == "bybit":
        from services import bybit_service as bys
        return bys, "bybit-linear"
    # Default: OKX (única que passa do Railway sem proxy)
    from services import binance_service as bs
    return bs, "okx-perp"


async def _analyze_symbol_tf_server(svc, symbol: str, tf: str) -> Optional[TradeSignal]:
    """Análise server-side usando a fonte escolhida (futures via proxy ou spot)."""
    try:
        df = await svc.fetch_ohlcv(symbol, tf, 300)
        if df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        # Derivativos/MTF: só se a fonte for futures (tem funding/OI)
        derivatives = None
        mtf = None
        return build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=derivatives, mtf=mtf, with_backtest=False,
        )
    except Exception:
        return None


async def _best_tf_for_symbol_server(svc, symbol: str) -> Optional[tuple]:
    results = await asyncio.gather(*[
        _analyze_symbol_tf_server(svc, symbol, tf) for tf in SCAN_TFS
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
    """
    Versão server-side pro Railway. Escolhe fonte dinamicamente:
      • BINANCE_PROXY_URL setado → Binance Futures (mesmos dados do app)
      • Senão → Binance Vision (spot)

    Nome mantido por compat — agora é "via server" mais genérico.
    """
    import logging as _log
    svc, source_name = _get_server_data_source()
    _log.info(f"[server-scan] fonte: {source_name}")

    # News blackout: pula scan inteiro durante janela de evento high-impact.
    # Economiza chamadas pra exchange E evita push notifications no pior
    # momento possível (FOMC/CPI/NFP estopam setups técnicos).
    try:
        from services import news_filter_service as nfs
        blackout = await nfs.get_blackout_status()
        if blackout.get("active"):
            _log.info(
                f"[server-scan] BLACKOUT: {blackout.get('event')} "
                f"({blackout.get('country')}) — skip scan. "
                f"Retoma em {blackout.get('minutes_until_resume')}min."
            )
            return []
    except Exception as e:
        _log.warning(f"[server-scan] news check falhou (fail-open): {e}")

    try:
        symbols = await svc.fetch_top_volume_symbols(limit=top_n)
    except Exception as e:
        _log.warning(f"[server-scan] fetch_top_volume falhou ({source_name}): {e}")
        symbols = []

    # Fallback híbrido: se a fonte for futures-proxy mas o endpoint de listagem
    # em massa (/fapi/v1/ticker/24hr) der 451 do DC do Railway (geo-block só
    # do bulk, endpoints individuais passam), usa Vision pra descobrir os top
    # símbolos e mantém Futures pro OHLCV de cada um.
    if not symbols and source_name == "binance-futures-proxy":
        try:
            from services import binance_vision_service as _bvs
            symbols = await _bvs.fetch_top_volume_symbols(limit=top_n)
            _log.info(f"[server-scan] usando lista do Vision ({len(symbols)} símbolos), "
                      f"OHLCV vem do Futures-proxy")
        except Exception as e:
            _log.warning(f"[server-scan] fallback Vision-list também falhou: {e}")
            symbols = []

    if not symbols:
        return []

    recommendations: List[Recommendation] = []
    sem = asyncio.Semaphore(6)

    async def _bounded(sym: str):
        async with sem:
            return sym, await _best_tf_for_symbol_server(svc, sym)

    all_results = await asyncio.gather(*[_bounded(s) for s in symbols])

    # Macro regime gate
    try:
        from services import regime_service as rs
        regime = await rs.get_regime_status()
    except Exception as e:
        _log.warning(f"[server-scan] regime check falhou (fail-open): {e}")
        regime = {"regime": "NORMAL", "block_all": False, "block_alt_longs": False, "downgrade_alt_longs": False}

    if regime.get("block_all"):
        _log.info(f"[server-scan] regime {regime.get('regime')} — skip: {regime.get('reasons')}")
        return []

    # Cooldown por símbolo (6h pós-stop/expire)
    try:
        from services.snapshot_service import get_recently_stopped_symbols
        cooldown_symbols = await get_recently_stopped_symbols(hours=6)
    except Exception:
        cooldown_symbols = set()

    for _symbol, best in all_results:
        if best is None:
            continue
        sig, score = best
        tier = _classify_tier_vision(sig, score)
        if tier is None:
            continue
        if sig.symbol in cooldown_symbols:
            _log.info(f"[server-scan] cooldown skip {sig.symbol}")
            continue
        # Regime filter
        try:
            from services.regime_service import should_block_recommendation, is_btc_symbol
            block_reason = should_block_recommendation(regime, sig.symbol, sig.direction)
            if block_reason:
                _log.info(f"[server-scan] skip {sig.symbol} {sig.direction}: {block_reason}")
                continue
            if regime.get("downgrade_alt_longs") and sig.direction == "long" and not is_btc_symbol(sig.symbol):
                if tier == "A+":
                    tier = "A"
                elif tier == "A":
                    tier = "B"
                elif tier == "B":
                    continue
        except Exception:
            pass
        _rec = _build_recommendation(sig, score, tier)
        if _rec is not None:
            recommendations.append(_rec)

    # BTC correlation throttle (idem batch)
    _apply_btc_correlation_throttle(recommendations, regime)

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
        _rec = _build_recommendation(sig, score, tier)
        if _rec is not None:
            recommendations.append(_rec)

    # Ordena por tier (A+ > A > B) e depois score
    tier_order = {"A+": 0, "A": 1, "B": 2}
    recommendations.sort(key=lambda r: (tier_order[r.tier], -r.score))

    _cache["ts"] = now
    _cache["data"] = recommendations
    return recommendations
