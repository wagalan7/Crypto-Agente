"""
Score de confluência ponderado e transparente.

Cada categoria contribui com pontos para a direção do sinal.
Soma final é mapeada para % de confiança. A diferença vs o `confidence`
antigo é que aqui CADA fator é justificado individualmente, o que dá
à IA contexto muito mais rico para gerar análise assertiva.
"""

from __future__ import annotations
from typing import List, Optional
import pandas as pd

from models.trade_signal import (
    Indicator,
    DetectedPattern,
    SignalDirection,
    ConfluenceFactor,
    ConfluenceScore,
)
from services.smc_service import SMCAnalysis
from services.derivatives_service import DerivativesData
from services.backtest_service import PatternStats
from services.divergence_service import Divergence
from services.vp_service import VPVWAPAnalysis
from services.mtf_service import MTFAlignment


# ─── Pesos por categoria ──────────────────────────────────────────────────────
WEIGHTS = {
    "momentum":     30,   # RSI + Stochastic
    "trend":        40,   # EMAs + ADX + Supertrend
    "macd":         20,   # MACD cross + histograma
    "volume":       20,   # Volume vs média
    "bollinger":    15,   # Posição BB
    "pattern":      35,   # Padrões clássicos detectados
    "structure":    20,   # Pivot levels, suporte/resistência
    "volatility":   10,   # ATR / regime
    "smc":          25,   # Order Blocks, FVG, Liquidity Sweeps, BOS/CHoCH
    "derivatives":  15,   # Funding rate + OI
    "divergence":   20,   # Divergências RSI/MACD
    "vp_vwap":      20,   # Volume Profile (POC/VAH/VAL) + VWAP
    "mtf":          30,   # Multi-timeframe alignment
}
MAX_TOTAL = sum(WEIGHTS.values())   # = 300


def _sign_for_direction(direction: SignalDirection, val: int) -> int:
    """Retorna 1 se o sinal indicador alinha com a direção, -1 se contrário, 0 se neutro."""
    if direction == SignalDirection.LONG:
        return val
    if direction == SignalDirection.SHORT:
        return -val
    return 0


def calculate_confluence(
    ind: Indicator,
    patterns: List[DetectedPattern],
    df: pd.DataFrame,
    direction: SignalDirection,
    current_price: float,
    smc: Optional[SMCAnalysis] = None,
    derivatives: Optional[DerivativesData] = None,
    pattern_stats: Optional[PatternStats] = None,
    divergences: Optional[List[Divergence]] = None,
    vp_vwap: Optional[VPVWAPAnalysis] = None,
    mtf: Optional[MTFAlignment] = None,
) -> ConfluenceScore:
    factors: List[ConfluenceFactor] = []
    warnings: List[str] = []

    # ── 1. Momentum: RSI ──────────────────────────────────────────────────────
    if ind.rsi is not None:
        if direction == SignalDirection.LONG:
            if ind.rsi < 30:
                factors.append(ConfluenceFactor(
                    name="RSI em sobrevenda",
                    category="momentum",
                    points=18, max_points=18, aligned=True,
                    description=f"RSI={ind.rsi:.1f} (<30) — pressão compradora provável após exaustão da venda."
                ))
            elif ind.rsi < 45:
                factors.append(ConfluenceFactor(
                    name="RSI saindo da zona neutra-baixa",
                    category="momentum",
                    points=10, max_points=18, aligned=True,
                    description=f"RSI={ind.rsi:.1f} — momentum lateral inclinando para alta."
                ))
            elif ind.rsi > 75:
                factors.append(ConfluenceFactor(
                    name="RSI extremamente sobrecomprado",
                    category="momentum",
                    points=-12, max_points=18, aligned=False,
                    description=f"RSI={ind.rsi:.1f} (>75) — risco de pullback no curto prazo, entrada tardia."
                ))
                warnings.append("RSI muito alto — considerar aguardar pullback antes de comprar.")
        elif direction == SignalDirection.SHORT:
            if ind.rsi > 70:
                factors.append(ConfluenceFactor(
                    name="RSI em sobrecompra",
                    category="momentum",
                    points=18, max_points=18, aligned=True,
                    description=f"RSI={ind.rsi:.1f} (>70) — pressão vendedora provável após exaustão da alta."
                ))
            elif ind.rsi > 55:
                factors.append(ConfluenceFactor(
                    name="RSI saindo da zona neutra-alta",
                    category="momentum",
                    points=10, max_points=18, aligned=True,
                    description=f"RSI={ind.rsi:.1f} — momentum inclinando para baixa."
                ))
            elif ind.rsi < 25:
                factors.append(ConfluenceFactor(
                    name="RSI extremamente sobrevendido",
                    category="momentum",
                    points=-12, max_points=18, aligned=False,
                    description=f"RSI={ind.rsi:.1f} (<25) — risco de quique no curto prazo."
                ))
                warnings.append("RSI muito baixo — considerar aguardar repique antes de vender.")

    # Stochastic
    if ind.stoch_k is not None and ind.stoch_d is not None:
        if direction == SignalDirection.LONG and ind.stoch_k < 20 and ind.stoch_d < 20:
            factors.append(ConfluenceFactor(
                name="Stochastic em sobrevenda",
                category="momentum",
                points=12, max_points=12, aligned=True,
                description=f"K={ind.stoch_k:.1f} / D={ind.stoch_d:.1f} — duplo sinal de fundo."
            ))
        elif direction == SignalDirection.SHORT and ind.stoch_k > 80 and ind.stoch_d > 80:
            factors.append(ConfluenceFactor(
                name="Stochastic em sobrecompra",
                category="momentum",
                points=12, max_points=12, aligned=True,
                description=f"K={ind.stoch_k:.1f} / D={ind.stoch_d:.1f} — duplo sinal de topo."
            ))

    # ── 2. Trend: EMAs ────────────────────────────────────────────────────────
    if ind.ema9 and ind.ema21 and ind.ema50:
        if direction == SignalDirection.LONG and ind.ema9 > ind.ema21 > ind.ema50:
            factors.append(ConfluenceFactor(
                name="EMAs alinhadas em alta (9>21>50)",
                category="trend",
                points=18, max_points=18, aligned=True,
                description="Estrutura de médias confirma tendência de alta em múltiplas janelas."
            ))
        elif direction == SignalDirection.SHORT and ind.ema9 < ind.ema21 < ind.ema50:
            factors.append(ConfluenceFactor(
                name="EMAs alinhadas em baixa (9<21<50)",
                category="trend",
                points=18, max_points=18, aligned=True,
                description="Estrutura de médias confirma tendência de baixa em múltiplas janelas."
            ))
        elif direction != SignalDirection.NEUTRAL:
            factors.append(ConfluenceFactor(
                name="EMAs sem alinhamento claro",
                category="trend",
                points=0, max_points=18, aligned=False,
                description="EMAs entrelaçadas — sem confirmação de tendência."
            ))

    # EMA 200 (golden/death cross)
    if ind.ema50 and ind.ema200:
        if direction == SignalDirection.LONG and ind.ema50 > ind.ema200 and current_price > ind.ema200:
            factors.append(ConfluenceFactor(
                name="Golden cross e preço acima da EMA 200",
                category="trend",
                points=12, max_points=12, aligned=True,
                description="Confirmação de tendência primária de alta no longo prazo."
            ))
        elif direction == SignalDirection.SHORT and ind.ema50 < ind.ema200 and current_price < ind.ema200:
            factors.append(ConfluenceFactor(
                name="Death cross e preço abaixo da EMA 200",
                category="trend",
                points=12, max_points=12, aligned=True,
                description="Confirmação de tendência primária de baixa no longo prazo."
            ))
        elif direction == SignalDirection.LONG and current_price < ind.ema200:
            warnings.append("Compra contra a tendência primária (preço abaixo da EMA 200).")
            factors.append(ConfluenceFactor(
                name="Preço abaixo da EMA 200",
                category="trend",
                points=-8, max_points=12, aligned=False,
                description="Operação contra-tendência primária. Risco maior, alvos menores."
            ))

    # ADX (força da tendência)
    if ind.adx is not None:
        if ind.adx > 35:
            factors.append(ConfluenceFactor(
                name="ADX em tendência muito forte",
                category="trend",
                points=10, max_points=10, aligned=True,
                description=f"ADX={ind.adx:.1f} (>35) — tendência muito definida e respeitada."
            ))
        elif ind.adx > 25:
            factors.append(ConfluenceFactor(
                name="ADX em tendência forte",
                category="trend",
                points=6, max_points=10, aligned=True,
                description=f"ADX={ind.adx:.1f} (>25) — tendência consistente."
            ))
        elif ind.adx < 20:
            factors.append(ConfluenceFactor(
                name="ADX baixo — mercado lateral",
                category="trend",
                points=-3, max_points=10, aligned=False,
                description=f"ADX={ind.adx:.1f} (<20) — sem tendência, evitar breakouts."
            ))
            warnings.append("Mercado lateralizado (ADX baixo) — sinais direcionais menos confiáveis.")

    # Supertrend
    if ind.supertrend_direction is not None:
        st_aligned = (
            (direction == SignalDirection.LONG and ind.supertrend_direction == 1) or
            (direction == SignalDirection.SHORT and ind.supertrend_direction == -1)
        )
        factors.append(ConfluenceFactor(
            name="Supertrend alinhado" if st_aligned else "Supertrend contrário",
            category="trend",
            points=10 if st_aligned else -6, max_points=10, aligned=st_aligned,
            description="Supertrend confirma a direção." if st_aligned else "Supertrend aponta na direção contrária ao sinal."
        ))

    # ── 3. MACD ───────────────────────────────────────────────────────────────
    if ind.macd is not None and ind.macd_signal is not None and ind.macd_hist is not None:
        macd_bull = ind.macd > ind.macd_signal
        macd_strong = abs(ind.macd_hist) > 0
        if direction == SignalDirection.LONG and macd_bull:
            pts = 18 if macd_strong and ind.macd_hist > 0 else 10
            factors.append(ConfluenceFactor(
                name="MACD bullish",
                category="macd",
                points=pts, max_points=20, aligned=True,
                description=f"MACD cruzou acima da linha de sinal{' com histograma positivo crescente' if pts == 18 else ''}."
            ))
        elif direction == SignalDirection.SHORT and not macd_bull:
            pts = 18 if macd_strong and ind.macd_hist < 0 else 10
            factors.append(ConfluenceFactor(
                name="MACD bearish",
                category="macd",
                points=pts, max_points=20, aligned=True,
                description=f"MACD cruzou abaixo da linha de sinal{' com histograma negativo' if pts == 18 else ''}."
            ))
        else:
            factors.append(ConfluenceFactor(
                name="MACD contra o sinal",
                category="macd",
                points=-6, max_points=20, aligned=False,
                description="MACD não confirma a direção do sinal."
            ))

    # ── 4. Volume ─────────────────────────────────────────────────────────────
    if ind.volume_avg and len(df) >= 3:
        last_vol = float(df["volume"].iloc[-1])
        ratio = last_vol / ind.volume_avg if ind.volume_avg else 0
        if ratio > 2.0:
            factors.append(ConfluenceFactor(
                name="Volume explosivo (>2x média)",
                category="volume",
                points=20, max_points=20, aligned=True,
                description=f"Volume atual {ratio:.1f}x a média — institucionais ativos."
            ))
        elif ratio > 1.5:
            factors.append(ConfluenceFactor(
                name="Volume acima da média",
                category="volume",
                points=14, max_points=20, aligned=True,
                description=f"Volume {ratio:.1f}x a média — interesse acima do normal."
            ))
        elif ratio < 0.5:
            factors.append(ConfluenceFactor(
                name="Volume muito baixo",
                category="volume",
                points=-5, max_points=20, aligned=False,
                description=f"Volume apenas {ratio:.1f}x a média — falta convicção no movimento."
            ))
            warnings.append("Volume baixo — movimento pode não se sustentar.")

    # ── 5. Bollinger Bands ────────────────────────────────────────────────────
    if ind.bb_upper and ind.bb_lower and ind.bb_middle:
        if direction == SignalDirection.LONG and current_price <= ind.bb_lower * 1.005:
            factors.append(ConfluenceFactor(
                name="Preço na banda inferior",
                category="bollinger",
                points=15, max_points=15, aligned=True,
                description=f"Preço tocando BB inferior ({ind.bb_lower:.6g}) — zona de compra estatística."
            ))
        elif direction == SignalDirection.SHORT and current_price >= ind.bb_upper * 0.995:
            factors.append(ConfluenceFactor(
                name="Preço na banda superior",
                category="bollinger",
                points=15, max_points=15, aligned=True,
                description=f"Preço tocando BB superior ({ind.bb_upper:.6g}) — zona de venda estatística."
            ))

    # ── 6. Padrões gráficos ───────────────────────────────────────────────────
    aligned_patterns = [p for p in patterns if p.direction == direction]
    if aligned_patterns:
        # Pega até 2 padrões mais confiantes
        top = sorted(aligned_patterns, key=lambda p: p.confidence, reverse=True)[:2]
        for p in top:
            pts = round(p.confidence * 17, 1)  # max ~17 por padrão (35 total)
            factors.append(ConfluenceFactor(
                name=f"Padrão: {p.type.value.replace('_', ' ').title()}",
                category="pattern",
                points=pts, max_points=17, aligned=True,
                description=p.description,
            ))
    # Padrões contrários = warning
    contrary = [p for p in patterns if p.direction != direction and p.direction != SignalDirection.NEUTRAL]
    if contrary:
        warnings.append(f"{len(contrary)} padrão(ões) contrário(s) detectado(s) — verificar antes de operar.")

    # ── 7. Estrutura (pivots) ────────────────────────────────────────────────
    if ind.pivot_low and ind.pivot_high:
        range_size = ind.pivot_high - ind.pivot_low
        if range_size > 0:
            position = (current_price - ind.pivot_low) / range_size
            if direction == SignalDirection.LONG and position < 0.3:
                factors.append(ConfluenceFactor(
                    name="Preço próximo do fundo do range",
                    category="structure",
                    points=15, max_points=20, aligned=True,
                    description=f"Preço a {position*100:.0f}% do range (fundo em {ind.pivot_low:.6g}) — compra com stop curto."
                ))
            elif direction == SignalDirection.SHORT and position > 0.7:
                factors.append(ConfluenceFactor(
                    name="Preço próximo do topo do range",
                    category="structure",
                    points=15, max_points=20, aligned=True,
                    description=f"Preço a {position*100:.0f}% do range (topo em {ind.pivot_high:.6g}) — venda com stop curto."
                ))
            elif direction == SignalDirection.LONG and position > 0.85:
                factors.append(ConfluenceFactor(
                    name="Preço próximo do topo do range",
                    category="structure",
                    points=-8, max_points=20, aligned=False,
                    description=f"Compra próxima de resistência ({ind.pivot_high:.6g}) — risco de rejeição."
                ))
                warnings.append("Entrada próxima do topo do range — risco de rejeição.")
            elif direction == SignalDirection.SHORT and position < 0.15:
                factors.append(ConfluenceFactor(
                    name="Preço próximo do fundo do range",
                    category="structure",
                    points=-8, max_points=20, aligned=False,
                    description=f"Venda próxima de suporte ({ind.pivot_low:.6g}) — risco de quique."
                ))
                warnings.append("Entrada próxima do fundo do range — risco de quique.")

    # ── 8. Volatilidade ──────────────────────────────────────────────────────
    if ind.atr and current_price > 0:
        atr_pct = (ind.atr / current_price) * 100
        if 1.0 <= atr_pct <= 4.0:
            factors.append(ConfluenceFactor(
                name="Volatilidade saudável",
                category="volatility",
                points=10, max_points=10, aligned=True,
                description=f"ATR {atr_pct:.2f}% — range operacional adequado."
            ))
        elif atr_pct > 8.0:
            factors.append(ConfluenceFactor(
                name="Volatilidade excessiva",
                category="volatility",
                points=-5, max_points=10, aligned=False,
                description=f"ATR {atr_pct:.2f}% — risco elevado, reduzir tamanho de posição."
            ))
            warnings.append(f"Volatilidade alta ({atr_pct:.1f}%) — usar tamanho reduzido.")

    # ── 9. Smart Money Concepts ──────────────────────────────────────────────
    if smc is not None and direction != SignalDirection.NEUTRAL:
        dir_word = "bullish" if direction == SignalDirection.LONG else "bearish"

        # Order Blocks alinhados (preço dentro/perto)
        aligned_obs = [z for z in smc.order_blocks if z.direction == dir_word]
        if aligned_obs:
            ob = aligned_obs[0]
            near = (ob.bottom * 0.99 <= current_price <= ob.top * 1.01)
            pts = 10 if near else 6
            factors.append(ConfluenceFactor(
                name=f"Order Block {dir_word} ativo",
                category="smc",
                points=pts, max_points=10, aligned=True,
                description=ob.description + (" — preço dentro da zona." if near else " — atrás de suporte/resistência institucional."),
            ))

        # FVG alinhado (preço pode buscar preencher)
        aligned_fvgs = [z for z in smc.fvgs if z.direction == dir_word]
        if aligned_fvgs:
            fvg = aligned_fvgs[0]
            factors.append(ConfluenceFactor(
                name=f"FVG {dir_word}",
                category="smc",
                points=6, max_points=8, aligned=True,
                description=fvg.description,
            ))

        # Liquidity sweep recente alinhado
        aligned_sweeps = [z for z in smc.liquidity_sweeps if z.direction == dir_word]
        if aligned_sweeps:
            sw = aligned_sweeps[0]
            factors.append(ConfluenceFactor(
                name=f"Liquidity Sweep {dir_word}",
                category="smc",
                points=7, max_points=7, aligned=True,
                description=sw.description,
            ))

        # BOS/CHoCH
        if smc.structure is not None:
            s = smc.structure
            if s.direction == dir_word:
                pts = 8 if s.type == "BOS" else 6  # CHoCH = reversão (menos consolidado)
                factors.append(ConfluenceFactor(
                    name=f"{s.type} {dir_word}",
                    category="smc",
                    points=pts, max_points=8, aligned=True,
                    description=s.description,
                ))
            else:
                factors.append(ConfluenceFactor(
                    name=f"{s.type} contrário",
                    category="smc",
                    points=-5, max_points=8, aligned=False,
                    description=f"Estrutura ({s.type}) aponta na direção oposta — atenção.",
                ))
                warnings.append(f"{s.type} contrário ao sinal — estrutura de mercado divergente.")

        # Trend bias estrutural contrário
        if smc.trend_bias != "neutral" and smc.trend_bias != dir_word:
            warnings.append(f"Bias estrutural ({smc.trend_bias}) contrário ao sinal.")

    # ── 10. Derivativos ─────────────────────────────────────────────────────
    if derivatives is not None and direction != SignalDirection.NEUTRAL:
        # Funding extremo NA direção do sinal = perigoso (squeeze contrário)
        if derivatives.funding_sentiment == "extreme_long":
            if direction == SignalDirection.LONG:
                factors.append(ConfluenceFactor(
                    name="Funding extremo positivo",
                    category="derivatives",
                    points=-8, max_points=8, aligned=False,
                    description=f"Funding {derivatives.funding_rate_pct:.3f}% — longs sobreaquecidos, risco de squeeze.",
                ))
                warnings.append("Longs sobreaquecidos — entrada de compra arriscada.")
            else:  # SHORT
                factors.append(ConfluenceFactor(
                    name="Setup contra-tendência (longs esticados)",
                    category="derivatives",
                    points=8, max_points=8, aligned=True,
                    description="Funding muito positivo favorece reversão para baixa.",
                ))
        elif derivatives.funding_sentiment == "extreme_short":
            if direction == SignalDirection.SHORT:
                factors.append(ConfluenceFactor(
                    name="Funding extremo negativo",
                    category="derivatives",
                    points=-8, max_points=8, aligned=False,
                    description=f"Funding {derivatives.funding_rate_pct:.3f}% — shorts sobreaquecidos, risco de squeeze.",
                ))
                warnings.append("Shorts sobreaquecidos — entrada de venda arriscada.")
            else:  # LONG
                factors.append(ConfluenceFactor(
                    name="Setup contra-tendência (shorts esticados)",
                    category="derivatives",
                    points=8, max_points=8, aligned=True,
                    description="Funding muito negativo favorece reversão para alta.",
                ))
        elif derivatives.funding_sentiment in ("bullish_squeeze", "bearish_squeeze"):
            factors.append(ConfluenceFactor(
                name="Funding moderado",
                category="derivatives",
                points=2, max_points=4, aligned=True,
                description=f"Funding {derivatives.funding_rate_pct:.3f}% — sentimento sem extremos.",
            ))

        # OI sentiment
        if derivatives.oi_sentiment == "bullish" and direction == SignalDirection.LONG:
            factors.append(ConfluenceFactor(
                name="OI subindo com preço",
                category="derivatives",
                points=7, max_points=7, aligned=True,
                description=f"OI {derivatives.oi_change_24h_pct:+.1f}% (24h) — dinheiro novo entrando comprado.",
            ))
        elif derivatives.oi_sentiment == "bearish" and direction == SignalDirection.SHORT:
            factors.append(ConfluenceFactor(
                name="OI subindo com preço caindo",
                category="derivatives",
                points=7, max_points=7, aligned=True,
                description=f"OI {derivatives.oi_change_24h_pct:+.1f}% (24h) — shorts pesados ainda abrindo.",
            ))

    # ── 11. Backtest histórico de padrões ───────────────────────────────────
    if pattern_stats is not None and patterns:
        # Aplica modificador baseado em win-rate dos padrões alinhados
        for p in patterns[:2]:
            if p.direction != direction:
                continue
            stat = pattern_stats.stats.get(p.type.value)
            if not stat or stat.sample_size_warning:
                continue
            wr = stat.win_rate
            if wr >= 0.6:
                factors.append(ConfluenceFactor(
                    name=f"Win-rate histórico {p.type.value.replace('_', ' ')}",
                    category="pattern",
                    points=8, max_points=8, aligned=True,
                    description=f"Histórico: {stat.wins}/{stat.occurrences} ({wr*100:.0f}% win-rate) neste ativo+TF.",
                ))
            elif wr < 0.4:
                factors.append(ConfluenceFactor(
                    name=f"Win-rate histórico baixo",
                    category="pattern",
                    points=-5, max_points=8, aligned=False,
                    description=f"Padrão {p.type.value} só {wr*100:.0f}% win-rate ({stat.occurrences} amostras) neste contexto.",
                ))
                warnings.append(f"Padrão {p.type.value} historicamente fraco neste ativo/TF ({wr*100:.0f}%).")

    # ── 12. Divergências RSI/MACD ────────────────────────────────────────────
    if divergences and direction != SignalDirection.NEUTRAL:
        dir_word = "bullish" if direction == SignalDirection.LONG else "bearish"
        aligned = [d for d in divergences if d.direction == dir_word]
        contrary = [d for d in divergences if d.direction != dir_word]

        # Até 2 divergências alinhadas
        for d in aligned[:2]:
            # Regular = mais peso (reversão clara), hidden = continuação
            base = 10 if d.type == "regular" else 7
            pts = round(base * d.strength + 3, 1)  # min 3 pts mesmo strength baixo
            factors.append(ConfluenceFactor(
                name=f"Divergência {d.type} {d.indicator} ({dir_word})",
                category="divergence",
                points=pts, max_points=10, aligned=True,
                description=d.description,
            ))

        # Divergências contrárias = penalidade + warning
        for d in contrary[:1]:
            factors.append(ConfluenceFactor(
                name=f"Divergência contrária no {d.indicator}",
                category="divergence",
                points=-6, max_points=10, aligned=False,
                description=d.description,
            ))
            warnings.append(f"Divergência {d.type} {d.indicator} oposta ao sinal — risco de reversão.")

    # ── 13. Volume Profile + VWAP ────────────────────────────────────────────
    if vp_vwap is not None and direction != SignalDirection.NEUTRAL:
        vp = vp_vwap.volume_profile
        vw = vp_vwap.vwap

        # ── POC como ímã ────────────────────────────────────────────────
        poc_dist_pct = abs(current_price - vp.poc) / vp.poc * 100 if vp.poc else 0
        if poc_dist_pct < 0.3:
            factors.append(ConfluenceFactor(
                name="Preço no POC",
                category="vp_vwap",
                points=4, max_points=8, aligned=True,
                description=f"Preço colado no POC ({vp.poc:.6g}) — zona de equilíbrio, alta liquidez.",
            ))

        # ── Value Area: dentro = equilíbrio, fora = extensão ────────────
        if vp.val <= current_price <= vp.vah:
            # Compras perto do VAL ou vendas perto do VAH = setups premium
            if direction == SignalDirection.LONG and current_price <= vp.val * 1.005:
                factors.append(ConfluenceFactor(
                    name="Compra no VAL",
                    category="vp_vwap",
                    points=10, max_points=10, aligned=True,
                    description=f"Preço no Value Area Low ({vp.val:.6g}) — base de demanda, alvo POC ({vp.poc:.6g}).",
                ))
            elif direction == SignalDirection.SHORT and current_price >= vp.vah * 0.995:
                factors.append(ConfluenceFactor(
                    name="Venda no VAH",
                    category="vp_vwap",
                    points=10, max_points=10, aligned=True,
                    description=f"Preço no Value Area High ({vp.vah:.6g}) — topo de oferta, alvo POC ({vp.poc:.6g}).",
                ))
        else:
            # Fora do VA: extensão — bom para reversão (mean reversion ao POC)
            if direction == SignalDirection.LONG and current_price < vp.val:
                factors.append(ConfluenceFactor(
                    name="Compra abaixo do Value Area",
                    category="vp_vwap",
                    points=7, max_points=10, aligned=True,
                    description=f"Preço extendido abaixo do VAL ({vp.val:.6g}) — reversão estatística para POC.",
                ))
            elif direction == SignalDirection.SHORT and current_price > vp.vah:
                factors.append(ConfluenceFactor(
                    name="Venda acima do Value Area",
                    category="vp_vwap",
                    points=7, max_points=10, aligned=True,
                    description=f"Preço extendido acima do VAH ({vp.vah:.6g}) — reversão estatística para POC.",
                ))
            elif direction == SignalDirection.LONG and current_price > vp.vah:
                # Compra esticada
                factors.append(ConfluenceFactor(
                    name="Compra esticada (fora do VA)",
                    category="vp_vwap",
                    points=-4, max_points=10, aligned=False,
                    description=f"Preço já está acima do VAH ({vp.vah:.6g}) — entrada tardia, risco de pullback.",
                ))
                warnings.append("Entrada de compra fora do Value Area — risco de mean reversion ao POC.")
            elif direction == SignalDirection.SHORT and current_price < vp.val:
                factors.append(ConfluenceFactor(
                    name="Venda esticada (fora do VA)",
                    category="vp_vwap",
                    points=-4, max_points=10, aligned=False,
                    description=f"Preço já está abaixo do VAL ({vp.val:.6g}) — entrada tardia.",
                ))
                warnings.append("Entrada de venda fora do Value Area — risco de mean reversion ao POC.")

        # ── VWAP como suporte/resistência dinâmico ─────────────────────
        if abs(vw.distance_pct) < 0.5:
            factors.append(ConfluenceFactor(
                name="Preço no VWAP",
                category="vp_vwap",
                points=4, max_points=6, aligned=True,
                description=f"Preço colado no VWAP ({vw.vwap:.6g}) — referência institucional, S/R dinâmico.",
            ))
        elif direction == SignalDirection.LONG and current_price > vw.vwap:
            factors.append(ConfluenceFactor(
                name="Preço acima do VWAP",
                category="vp_vwap",
                points=6, max_points=6, aligned=True,
                description=f"Preço {vw.distance_pct:+.1f}% acima do VWAP — bias institucional comprador.",
            ))
        elif direction == SignalDirection.SHORT and current_price < vw.vwap:
            factors.append(ConfluenceFactor(
                name="Preço abaixo do VWAP",
                category="vp_vwap",
                points=6, max_points=6, aligned=True,
                description=f"Preço {vw.distance_pct:+.1f}% abaixo do VWAP — bias institucional vendedor.",
            ))
        elif direction == SignalDirection.LONG and current_price < vw.vwap:
            factors.append(ConfluenceFactor(
                name="Compra abaixo do VWAP",
                category="vp_vwap",
                points=-3, max_points=6, aligned=False,
                description="Compra contra o bias institucional (preço abaixo do VWAP).",
            ))
        elif direction == SignalDirection.SHORT and current_price > vw.vwap:
            factors.append(ConfluenceFactor(
                name="Venda acima do VWAP",
                category="vp_vwap",
                points=-3, max_points=6, aligned=False,
                description="Venda contra o bias institucional (preço acima do VWAP).",
            ))

        # ── Extremos VWAP ±2σ = reversão estatística ───────────────────
        if direction == SignalDirection.LONG and current_price <= vw.lower_2sd:
            factors.append(ConfluenceFactor(
                name="VWAP -2σ tocado",
                category="vp_vwap",
                points=6, max_points=6, aligned=True,
                description=f"Preço no extremo inferior do VWAP (banda -2σ {vw.lower_2sd:.6g}) — reversão provável.",
            ))
        elif direction == SignalDirection.SHORT and current_price >= vw.upper_2sd:
            factors.append(ConfluenceFactor(
                name="VWAP +2σ tocado",
                category="vp_vwap",
                points=6, max_points=6, aligned=True,
                description=f"Preço no extremo superior do VWAP (banda +2σ {vw.upper_2sd:.6g}) — reversão provável.",
            ))

    # ── 14. Multi-TF Alignment ──────────────────────────────────────────────
    if mtf is not None and direction != SignalDirection.NEUTRAL:
        # alignment_score ∈ [-1, 1]
        if mtf.alignment_score >= 0.99:
            # Todos os TFs superiores alinhados
            factors.append(ConfluenceFactor(
                name="MTF totalmente alinhado",
                category="mtf",
                points=30, max_points=30, aligned=True,
                description=mtf.summary,
            ))
        elif mtf.alignment_score >= 0.5:
            factors.append(ConfluenceFactor(
                name="MTF majoritariamente alinhado",
                category="mtf",
                points=20, max_points=30, aligned=True,
                description=mtf.summary,
            ))
        elif mtf.alignment_score > 0:
            factors.append(ConfluenceFactor(
                name="MTF parcialmente alinhado",
                category="mtf",
                points=8, max_points=30, aligned=True,
                description=mtf.summary,
            ))
        elif mtf.alignment_score == 0:
            factors.append(ConfluenceFactor(
                name="MTF neutro",
                category="mtf",
                points=0, max_points=30, aligned=False,
                description=mtf.summary,
            ))
            warnings.append("Timeframes superiores neutros — falta confirmação macro.")
        elif mtf.alignment_score >= -0.5:
            factors.append(ConfluenceFactor(
                name="MTF parcialmente contrário",
                category="mtf",
                points=-10, max_points=30, aligned=False,
                description=mtf.summary,
            ))
            warnings.append("Maioria dos TFs superiores contraria o sinal — operação contra-tendência.")
        else:
            # Todos contra: vetar fortemente
            factors.append(ConfluenceFactor(
                name="MTF totalmente contrário",
                category="mtf",
                points=-25, max_points=30, aligned=False,
                description=mtf.summary,
            ))
            warnings.append("Todos os TFs superiores apontam direção contrária — risco extremo, evitar operar.")

    # ── Total ────────────────────────────────────────────────────────────────
    total = sum(f.points for f in factors)
    # Cap entre 0 e MAX_TOTAL
    total = max(0, min(total, MAX_TOTAL))
    pct = round((total / MAX_TOTAL) * 100, 1) if MAX_TOTAL > 0 else 0.0

    return ConfluenceScore(
        total=round(total, 1),
        max_total=float(MAX_TOTAL),
        pct=pct,
        factors=factors,
        warnings=warnings,
    )
