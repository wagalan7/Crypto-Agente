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

Cache: 30s.
"""
from __future__ import annotations
import asyncio
import os
import time
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel

from services.binance_service import fetch_top_volume_symbols, fetch_ohlcv, fetch_ticker
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns, record_breakouts_and_retests
from services.signal_service import build_trade_signal, determine_direction
from services.derivatives_service import analyze_derivatives
from services.mtf_service import analyze_mtf
from models.trade_signal import TradeSignal, SignalDirection


SCAN_TFS = ["15m", "1h", "4h"]   # TFs varridos por símbolo

# ── Chase estrutural (gated) ─────────────────────────────────────────────────
# Mede o esticamento do preço desde a BASE do movimento (pivot estrutural), não
# só do plano de entrada. DEFAULT-OFF: sem a flag, struct_chase_* fica None e
# nada muda. Tetos em múltiplos de ATR (a perna inteira é mais larga que a
# distância entry→preço, por isso os limites são maiores que o anti-chase de plano).
STRUCT_CHASE_ENABLED = os.getenv("STRUCT_CHASE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
STRUCT_CHASE_CHASING_ATR = float(os.getenv("STRUCT_CHASE_CHASING_ATR", "5.0"))   # 🔴 esticado demais
STRUCT_CHASE_EXTENDED_ATR = float(os.getenv("STRUCT_CHASE_EXTENDED_ATR", "3.0")) # 🟡 levemente esticado

# ── TFs altos: padrões de swing/posição (gated) ──────────────────────────────
# O scan base só vê 15m/1h/4h. Padrões gráficos relevantes (cunhas, triângulos,
# OCO, canais) aparecem MUITO nos TFs altos — e um rompimento limpo em 12h/1D é
# mais confiável que ruído de 15m. Aqui adicionamos 12h/1D/3D/1W ao leque de TFs
# avaliados por símbolo. DEFAULT-OFF: com a flag off, HIGH_TF_LIST não é varrido
# e nada muda (mesmos 3 TFs de sempre).
#
# Custo: candles altos mudam devagar, então cada (símbolo,TF alto) é cacheado com
# TTL proporcional ao TF (12h re-avalia ~30min, 1W ~6h) — evita refetch dos 4 TFs
# extras a cada ciclo de 90s. Só o ciclo frio paga o fetch completo.
#
# Relevância: TF maior = mais relevante ("do maior para o menor"). Multiplicador
# de score >1.0 só para os TFs altos (15m/1h/4h ficam 1.0 → não mexe no balanço
# atual). Aplicado em _compute_score, gated pela mesma flag (no-op quando off).
HIGH_TF_PATTERNS_ENABLED = os.getenv("HIGH_TF_PATTERNS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
HIGH_TF_LIST = [tf.strip() for tf in os.getenv("HIGH_TF_LIST", "12h,1d,3d,1w").split(",") if tf.strip()]
# TTL de cache por TF alto (segundos). Candle de 12h não muda em 90s.
_HTF_TTL = {"12h": 1800, "1d": 3600, "3d": 10800, "1w": 21600}
_HTF_TTL_DEFAULT = 1800
# Peso de relevância por TF (multiplicador de score). >1.0 só nos TFs altos.
_HTF_WEIGHT = {"12h": 1.05, "1d": 1.10, "3d": 1.15, "1w": 1.20}
# Cache TTL de sinal por (símbolo, TF alto): (expira_em, signal|None)
_HTF_SIGNAL_CACHE: Dict[Tuple[str, str], Tuple[float, Optional["TradeSignal"]]] = {}


def _htf_relevance_mult(tf: str) -> float:
    """Multiplicador de relevância por TF. 1.0 (no-op) quando a flag está off
    ou para os TFs base (15m/1h/4h). Só TFs altos recebem prêmio (>1.0)."""
    if not HIGH_TF_PATTERNS_ENABLED:
        return 1.0
    return _HTF_WEIGHT.get(tf, 1.0)


async def _htf_signal_cached(symbol: str, tf: str, make_coro) -> Optional["TradeSignal"]:
    """Avalia um TF alto com cache TTL. make_coro é um callable 0-arg que cria a
    coroutine de análise (cliente ou server). Fail-soft: erro → None cacheado curto."""
    now = time.time()
    ck = (symbol, tf)
    hit = _HTF_SIGNAL_CACHE.get(ck)
    if hit and hit[0] > now:
        return hit[1]
    try:
        sig = await make_coro()
    except Exception:
        sig = None
    _HTF_SIGNAL_CACHE[ck] = (now + _HTF_TTL.get(tf, _HTF_TTL_DEFAULT), sig)
    return sig


# ── Confirmação cruzada por TF alto (Passo 4 — gated) ────────────────────────
# Quando um TF alto (12h/1D/3D/1W) tem rompimento confirmado OU retest ativo numa
# direção, um setup de TF BAIXO na MESMA direção ganha um boost de score. Combina
# convicção estrutural do TF alto com a entrada precisa do TF baixo (RR melhor) —
# o caso HYPE: cunha rompida no 12h + entrada limpa no 4h/1h. Depende de
# HIGH_TF_PATTERNS_ENABLED (sem os TFs altos varridos não há o que confirmar).
HIGH_TF_CONFIRM_ENABLED = os.getenv("HIGH_TF_CONFIRM_ENABLED", "false").strip().lower() in ("1", "true", "yes")
HIGH_TF_CONFIRM_BONUS = float(os.getenv("HIGH_TF_CONFIRM_BONUS", "6.0"))


def _sig_has_htf_trigger(sig: Optional["TradeSignal"]) -> bool:
    """True se o sinal tem algum padrão com rompimento confirmado ou retest ativo
    na MESMA direção do sinal (gatilho técnico forte)."""
    if not sig or not getattr(sig, "patterns", None):
        return False
    for p in sig.patterns:
        if (
            (getattr(p, "breakout_confirmed", False) or getattr(p, "retest_active", False))
            and p.direction == sig.direction
        ):
            return True
    return False


def _htf_confirm_dirs(results) -> set:
    """Direções confirmadas por um gatilho (rompimento/retest) em algum TF alto."""
    dirs = set()
    for sig in results:
        if sig is None:
            continue
        if sig.timeframe in HIGH_TF_LIST and _sig_has_htf_trigger(sig):
            dirs.add(sig.direction)
    return dirs


def _score_with_htf_confirm(sig: "TradeSignal", base_score: float, confirm_dirs: set) -> float:
    """Aplica o boost de confirmação cruzada a um setup de TF baixo cuja direção
    é confirmada por um TF alto. No-op quando confirm_dirs vazio (flag off) ou
    quando o próprio sinal já é de TF alto (o boost dele já vem da relevância)."""
    if (
        confirm_dirs
        and sig.timeframe not in HIGH_TF_LIST
        and sig.direction in confirm_dirs
    ):
        return min(100.0, base_score + HIGH_TF_CONFIRM_BONUS)
    return base_score
# Concorrência do server-scan (símbolos em paralelo). Default 6. Com top_n alto
# (ex.: 500) via proxy, subir p/ ~14 mantém o ciclo perto de 90s. Env-driven.
try:
    SCAN_CONCURRENCY = max(1, int(os.getenv("SCAN_CONCURRENCY", "6")))
except (TypeError, ValueError):
    SCAN_CONCURRENCY = 6
CACHE_TTL = 15                    # segundos (era 30 — push do scan tava com delay, agora mais fresco)
MIN_RR = 1.5                      # filtro mínimo absoluto
MIN_CONFIDENCE_B = 0.55           # tier B mínimo

# ── Gate por timeframe × tier ────────────────────────────────────────────
# Corta setups de baixa qualidade nos TFs mais ruidosos pra reduzir stops.
# Histórico (441 trades): 4h ~93% WR, 1h ~72%, 15m ~73% — mas o 15m é 58% do
# volume e concentra a maioria dos stops (scalp = stop apertado = whipsaw).
# Aqui exigimos um tier MÍNIMO por TF: 15m só passa se A+; 1h só A/A+; 4h
# aceita todos (confiável até em B). Config via env CSV "tf:tier", reversível
# sem deploy. TFs ausentes do mapa = sem restrição. TF_TIER_GATE_ENABLED=0
# desliga o gate inteiro.
TF_TIER_GATE_ENABLED = os.getenv("TF_TIER_GATE_ENABLED", "1").lower() in ("1", "true", "yes", "on")
_TIER_RANK = {"B": 0, "A": 1, "A+": 2}


def _parse_tf_min_tier(raw: str) -> Dict[str, int]:
    """Parseia "15m:A+,1h:A,4h:B" → {"15m": 2, "1h": 1, "4h": 0}. Ignora lixo."""
    out: Dict[str, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        tf, tier = part.split(":", 1)
        tf = tf.strip()
        tier = tier.strip().upper()
        if tf and tier in _TIER_RANK:
            out[tf] = _TIER_RANK[tier]
    return out


TF_MIN_TIER: Dict[str, int] = _parse_tf_min_tier(
    os.getenv("TF_MIN_TIER", "15m:A+,1h:A,4h:B")
)


def _norm_sym(s: str) -> str:
    """Reduz um símbolo só à moeda base, pra casar o denylist em qualquer
    formato. 'BTC/USDT:USDT'→'BTC', 'BTCUSDT'→'BTC', 'BTC-USDT-SWAP'→'BTC',
    'BTC'→'BTC', '1000PEPE/USDT:USDT'→'1000PEPE'."""
    s = (s or "").upper().strip()
    s = s.split(":", 1)[0].split("/", 1)[0]          # tira /USDT:USDT
    s = s.replace("-USDT-SWAP", "").replace("-SWAP", "")
    for suf in ("USDT", "USD", "PERP"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s.replace("-", "").replace("_", "").strip()


# Denylist de símbolos: memecoins/junk que pumpam volume mas dão setups erráticos
# (NEIRO, GALA...) — NÃO são filtrados por volume (justamente têm volume alto),
# então exigem exclusão explícita. CSV de moedas-base, reversível sem deploy.
# Vazio (SYMBOL_DENYLIST="") = sem denylist. Aplicado no chokepoint _classify_tier
# (cobre live, server-scan e auto-trade) + pré-filtro no scan (economia de CPU).
SYMBOL_DENYLIST: set = {
    _norm_sym(x) for x in os.getenv("SYMBOL_DENYLIST", "NEIRO,GALA").split(",") if x.strip()
}

# Portfolio guard (caps correlação/exposição: 5 posições / 2 por categoria / 5%
# risco) — regra de EXECUÇÃO/sizing, não de display. Default ON (PRD: execução
# real precisa do cap). Desligar SÓ no DEV (ambiente de observação/estudo) pra
# o painel 👁 OBSERVAÇÃO mostrar o universo amplo SEM o corte pras ~5 melhores.
# Env-gated: PRD fica intocado (default true).
PORTFOLIO_GUARD_ENABLED: bool = os.getenv(
    "PORTFOLIO_GUARD_ENABLED", "true"
).strip().lower() not in ("0", "false", "no", "off")


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
    # ── Chase ESTRUTURAL (gated STRUCT_CHASE_ENABLED) ─────────────────────
    # O chase_atr acima mede distância do PLANO de entrada — não pega o setup
    # que já nasce esticado (entry≈mercado após pernada longa). Este mede o
    # esticamento desde a BASE do movimento (pivot estrutural): para long,
    # (preço − pivot_low)/ATR; para short, (pivot_high − preço)/ATR. Alto =
    # comprou/vendeu longe da origem → pior expectância (caso HYPE).
    struct_chase_atr: Optional[float] = None
    struct_chase_level: Optional[str] = None  # "ok" | "extended" | "chasing"
    # Origem retest re-arm: a rec veio de um pullback à linha rompida (entrada limpa).
    retest_armed: Optional[bool] = None
    # P(TP1) calibrada via calibration_service — None se calib não está pronta
    # (precisa de ≥30 snapshots resolvidos nos últimos 90 dias).
    prob_tp1: Optional[float] = None          # 0..1 — exibido no card como %
    # P(TP2) calibrada — prob de correr até o TP2 (won_tp2). Subconjunto de
    # prob_tp1 (sempre <=). Usada no sizing por convicção (#2a) como sinal
    # aditivo. None se calib imatura.
    prob_tp2: Optional[float] = None          # 0..1
    # ── Position sizing dinâmico (Issue #4 — Kelly fracionado) ────────────
    # Tamanho sugerido em % da banca, baseado em prob_tp1 × RR × score × volatilidade.
    # Diferente de risk_pct (que é o % de PERDA aceitável se o stop bater).
    # Cap [0.25%, 1.0%]. None se não foi possível computar (calib não pronta etc).
    suggested_size_pct: Optional[float] = None
    size_rationale: Optional[str] = None      # explicação curta PT-BR (UI tooltip)
    # ── Liquidez (do ticker da varredura) + veredito de execução do bot ───
    quote_vol_usd: Optional[float] = None     # volume 24h em USD (alimenta o gate de liquidez)
    spread_pct: Optional[float] = None        # spread bid/ask em %
    # Veredito READ-ONLY dos gates de qualidade do bot (R:R, P(TP1), liquidez),
    # computado com a MESMA lógica/limites do loop de execução (fonte única).
    # {ok, blocked_by, reason, checks}. None se não foi possível avaliar.
    bot_verdict: Optional[dict] = None
    # Grau de entrada manual — fonte única consumida pelo push e pelo app.
    # Combina o veredito de qualidade (bot_verdict) com o piso de auto-execução
    # (SCORE_MIN). Distingue "o bot abriria" de "só dá pra entrar manual":
    #   "good"   = qualidade ok E score ≥ SCORE_MIN (o bot abriria sozinho)
    #   "manual" = qualidade ok, mas score < SCORE_MIN (só entrada manual)
    #   "avoid"  = não passa nos gates de qualidade do bot
    entry_grade: Optional[str] = None
    # ── Edges (sinais que historicamente elevam o win-rate) ───────────────
    # Fundamentado no learning-insights (701 snapshots): A+ ~92% wr, funding em
    # squeeze ~100%, padrão forte ~90%, MTF alinhado ~82% — vs baseline ~72%.
    # Tags legíveis (ex: ["A+","funding"]) + contagem. Alimentam o sizing por
    # edge na execução (shadow_trade_service) e a transparência no app/push.
    edge_tags: List[str] = []
    edge_score: int = 0


_cache: Dict[str, Any] = {"ts": 0, "data": None}

# Cache compartilhado pra alimentar o endpoint /api/recommendations (consumido
# pelo app) com EXATAMENTE as recs que o scan loop server-side gera — as mesmas
# que abrem shadow/auto-trade. O loop (a cada 90s) chama set_api_recommendations_cache().
# O app refresca a cada 120s → cache sempre quente, zero scan extra. TTL governa só
# o fallback (se o loop estiver atrasado/parado, o endpoint faz um scan próprio).
_api_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
API_CACHE_TTL = 150.0  # segundos


def set_api_recommendations_cache(recs: "List[Recommendation]") -> None:
    """Chamado pelo scan loop após cada varredura — mantém o cache do app alinhado
    ao que o bot realmente gerou. Idempotente e fail-soft."""
    try:
        _api_cache["data"] = list(recs or [])
        _api_cache["ts"] = time.time()
    except Exception:
        pass


async def get_recommendations_cached_for_api(top_n: int = 50) -> "List[Recommendation]":
    """Serve o endpoint /api/recommendations. Retorna o último scan do loop (cache).
    Se o cache estiver frio/expirado (ex.: app abre antes do 1º scan, ou loop parado),
    faz um scan via vision como fallback e popula o cache."""
    now = time.time()
    c = _api_cache
    if c["data"] is not None and (now - c["ts"]) < API_CACHE_TTL:
        return c["data"]
    recs = await get_recommendations_via_vision(top_n=top_n)
    c["data"] = recs
    c["ts"] = now
    return recs


# ── Score V2 (atrás de flag, default OFF — NO-OP até ativar pós-teste 0.50) ──
# Diagnóstico (score_analysis_service): a fórmula legada tem AUC≈0.52 (≈moeda).
# Confluence sozinho (0.579) já bate o composto; RR é INVERSO (0.442) e MTF é
# ruído com 24% de cobertura. V2 mantém só os componentes preditivos
# (confluence + ADX) + derivatives leve, e RENORMALIZA sobre os presentes
# (dado faltante NÃO ancora em 50). Pesos e normalizações ESPELHAM
# score_analysis_service._norm_components p/ que live == reweight-sim == backfill.
SCORE_FORMULA_V2 = os.getenv("SCORE_FORMULA_V2", "false").lower() in ("1", "true", "yes", "on")
try:
    SCORE_V2_W_CONF = float(os.getenv("SCORE_V2_W_CONF", "0.60"))
    SCORE_V2_W_ADX = float(os.getenv("SCORE_V2_W_ADX", "0.30"))
    SCORE_V2_W_DER = float(os.getenv("SCORE_V2_W_DER", "0.10"))
except (TypeError, ValueError):
    SCORE_V2_W_CONF, SCORE_V2_W_ADX, SCORE_V2_W_DER = 0.60, 0.30, 0.10

# Cortes de tier sob a V2 (a V2 comprime a faixa: max≈71, p50≈46 → os cortes
# legados 75/65/52 esvaziariam A+/A). Derivados via /api/score/tier-sim pra
# PRESERVAR o mix A+/A/B atual (mesmo volume de execução) e ainda assim dar um
# gradiente de win-rate monotônico (A+ 92% / A 78% / B 68%, vs legado A≈B flat).
# Só valem quando SCORE_FORMULA_V2=on; com a flag OFF, mantém-se 75/65/52.
try:
    SCORE_V2_TIER_APLUS = float(os.getenv("SCORE_V2_TIER_APLUS", "65"))
    SCORE_V2_TIER_A = float(os.getenv("SCORE_V2_TIER_A", "46"))
    SCORE_V2_TIER_B = float(os.getenv("SCORE_V2_TIER_B", "18"))
except (TypeError, ValueError):
    SCORE_V2_TIER_APLUS, SCORE_V2_TIER_A, SCORE_V2_TIER_B = 65.0, 46.0, 18.0


def _compute_score_v2(
    conf_pct: Optional[float],
    adx_raw: Optional[float],
    funding_pct: Optional[float],
) -> Optional[float]:
    """Score 0–100 V2. Renormaliza sobre componentes presentes. Retorna None
    quando nada é computável (sem confluence E sem adx E sem funding) → o caller
    faz fallback pra fórmula legada. ESPELHA exatamente as normalizações de
    score_analysis_service._norm_components (mesma matemática em live/sim/backfill)."""
    conf_n = conf_pct if conf_pct is not None else None
    adx_n = (max(0.0, min(adx_raw, 50.0)) / 50.0 * 100.0) if adx_raw is not None else None
    der_n = (50.0 - max(-1.0, min(funding_pct / 0.05, 1.0)) * 50.0) if funding_pct is not None else None
    comps = (
        (conf_n, SCORE_V2_W_CONF),
        (adx_n, SCORE_V2_W_ADX),
        (der_n, SCORE_V2_W_DER),
    )
    num = den = 0.0
    for val, w in comps:
        if w > 0 and val is not None:
            num += w * val
            den += w
    if den == 0:
        return None
    return round(max(0.0, min(100.0, num / den)), 1)


def _compute_score(sig: TradeSignal) -> float:
    """
    Score 0–100. Combina confluence + MTF + R:R + win-rate histórico + derivatives.

    Mudanças vs versão anterior (ceiling 75):
      • RR cap 5→3: RR=3 (excelente) agora vale 100% do componente. Antes
        RR=3 dava só 60% — A+ era inalcançável organicamente.
      • Pesos rebalanceados: conf 0.45→0.35, mtf 0.30→0.25, RR 0.20→0.25,
        + novo der 0.10. Mantém soma=1.0 + win_bonus aditivo.
      • Derivatives entram via _derivatives_score (-15 a +15):
        - funding neutro/contra-trade = bom; extremo a favor = ruim
        - OI a favor da direção = bom

    V2 (flag SCORE_FORMULA_V2): usa só confluence+ADX+derivatives renormalizados.
    Fallback transparente pra fórmula legada se V2 não for computável.
    """
    if SCORE_FORMULA_V2:
        conf_pct = sig.confluence.pct if sig.confluence else None
        adx_raw = sig.indicators.adx if sig.indicators else None
        der = sig.derivatives
        funding_pct = None
        if der:
            funding_pct = (
                der.get("funding_rate_pct") if isinstance(der, dict)
                else getattr(der, "funding_rate_pct", None)
            )
        v2 = _compute_score_v2(conf_pct, adx_raw, funding_pct)
        if v2 is not None:
            # Relevância por TF (gated, no-op quando off) também na fórmula V2.
            return round(max(0.0, min(100.0, v2 * _htf_relevance_mult(sig.timeframe))), 1)
        # V2 não computável → cai na fórmula legada abaixo.

    conf_score = (sig.confluence.pct if sig.confluence else sig.confidence * 100)
    mtf_score = 50.0
    if sig.mtf:
        # mtf.alignment_score vai de -1 a +1 → mapeia pra 0–100
        mtf_score = (sig.mtf.get("alignment_score", 0) + 1) * 50
    # RR cap em 3 (era 5). RR=3 vira 100, RR=2 vira 67. Mais diferenciação.
    rr_score = min(sig.risk_reward / 3.0, 1.0) * 100
    win_bonus = 0.0
    if sig.pattern_stats and sig.pattern_stats.get("stats"):
        win_rates = [
            s.get("win_rate", 0) for s in sig.pattern_stats["stats"].values()
            if s.get("occurrences", 0) >= 10
        ]
        if win_rates:
            avg = sum(win_rates) / len(win_rates)
            win_bonus = (avg - 0.5) * 20   # ±10 pontos
    # Derivatives score: 50 = neutro, 0–100. Penaliza crowded trades.
    der_score = _derivatives_score(sig)
    # Bonus de rompimento confirmado com volume — gatilho técnico forte.
    # +5pts se algum padrão a favor da direção tem breakout_confirmed=True.
    # Cap em +5 (não acumula entre múltiplos padrões).
    # Inclui retest_active (pullback à linha rompida) — só fica True quando
    # RETEST_REARM_ENABLED, então é naturalmente gated (no-op com a flag off).
    breakout_bonus = 0.0
    if sig.patterns:
        for p in sig.patterns:
            if (
                (getattr(p, "breakout_confirmed", False) or getattr(p, "retest_active", False))
                and p.direction == sig.direction
            ):
                breakout_bonus = 5.0
                break
    # Pesos: conf 0.35, MTF 0.25, R:R 0.25, der 0.10 (soma 0.95) + win ±5 + breakout +5
    score = (
        conf_score * 0.35
        + mtf_score * 0.25
        + rr_score * 0.25
        + der_score * 0.10
        + win_bonus * 0.5
        + breakout_bonus
    )
    # Relevância por TF (gated): TF maior = mais relevante. Mult 1.0 (no-op)
    # quando HIGH_TF_PATTERNS_ENABLED off ou TF base. Cap final em 100.
    score = score * _htf_relevance_mult(sig.timeframe)
    return round(max(0.0, min(100.0, score)), 1)


def _derivatives_score(sig: TradeSignal) -> float:
    """
    Score 0–100 baseado em sentimento de derivatives. 50 = neutro/sem dados.

    Lógica:
      • Funding contra a direção do trade (short squeeze pra LONG / long
        squeeze pra SHORT) → +20 (gatilho de movimento a favor).
      • Funding neutro → +10 (sem crowded trade).
      • Funding moderado a favor (bullish_squeeze p/ long) → -10 (crowded).
      • Funding extremo a favor → -25 (alto risco de liquidação contrária).
      • OI a favor da direção (LONG + oi bullish, SHORT + oi bearish) → +10.
      • OI contra → -10.
    """
    der = sig.derivatives
    if not der:
        return 50.0

    score = 50.0
    funding_sent = getattr(der, "funding_sentiment", None) or (
        der.get("funding_sentiment") if isinstance(der, dict) else None
    )
    oi_sent = getattr(der, "oi_sentiment", None) or (
        der.get("oi_sentiment") if isinstance(der, dict) else None
    )

    is_long = sig.direction == SignalDirection.LONG
    is_short = sig.direction == SignalDirection.SHORT

    # Funding
    if funding_sent == "neutral":
        score += 10
    elif is_long and funding_sent == "extreme_long":
        score -= 25
    elif is_short and funding_sent == "extreme_short":
        score -= 25
    elif is_long and funding_sent == "bullish_squeeze":
        score -= 10
    elif is_short and funding_sent == "bearish_squeeze":
        score -= 10
    elif is_long and funding_sent in ("extreme_short", "bearish_squeeze"):
        score += 20   # short squeeze a favor do long
    elif is_short and funding_sent in ("extreme_long", "bullish_squeeze"):
        score += 20

    # OI
    if is_long and oi_sent == "bullish":
        score += 10
    elif is_short and oi_sent == "bearish":
        score += 10
    elif is_long and oi_sent == "bearish":
        score -= 10
    elif is_short and oi_sent == "bullish":
        score -= 10

    return max(0.0, min(100.0, score))


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


def _derivatives_tier_penalty(sig: TradeSignal) -> int:
    """
    Penalidade GRADUAL baseada em derivativos. Retorna número de "downgrades"
    a aplicar no tier (0 = sem penalidade, 1 = A+→A, 2 = A→B, etc).

    Regras:
      • Funding moderado (0.02–0.05%) na MESMA direção → -1 (crowded trade)
      • OI bearish em LONG (preço caindo + OI subindo = shorts institucionais
        pesados) → -1
      • OI bullish em SHORT (preço subindo + OI subindo) → -1

    Não retorna mais que 2 (evita downgrade triplo em casos limítrofes).
    Adiciona warnings ao trade_plan pra exibir no card.
    """
    der = sig.derivatives
    if not der:
        return 0

    funding_sent = getattr(der, "funding_sentiment", None) or (
        der.get("funding_sentiment") if isinstance(der, dict) else None
    )
    oi_sent = getattr(der, "oi_sentiment", None) or (
        der.get("oi_sentiment") if isinstance(der, dict) else None
    )
    funding_pct = getattr(der, "funding_rate_pct", None) or (
        der.get("funding_rate_pct") if isinstance(der, dict) else None
    )

    penalty = 0
    warnings: List[str] = []

    # Funding moderado same-direction → crowded
    if sig.direction == SignalDirection.LONG and funding_sent == "bullish_squeeze":
        penalty += 1
        f_str = f"{funding_pct:+.3f}%" if funding_pct is not None else "moderado"
        warnings.append(f"⚠ Funding {f_str} positivo — long crowded, downgrade de tier.")
    elif sig.direction == SignalDirection.SHORT and funding_sent == "bearish_squeeze":
        penalty += 1
        f_str = f"{funding_pct:+.3f}%" if funding_pct is not None else "moderado"
        warnings.append(f"⚠ Funding {f_str} negativo — short crowded, downgrade de tier.")

    # OI adverso: institucional posicionado contra o trade
    if sig.direction == SignalDirection.LONG and oi_sent == "bearish":
        penalty += 1
        warnings.append("⚠ OI subindo com preço caindo — shorts institucionais pesados contra o long.")
    elif sig.direction == SignalDirection.SHORT and oi_sent == "bullish":
        penalty += 1
        warnings.append("⚠ OI subindo com preço subindo — dinheiro novo long contra o short.")

    # Propaga warnings pro trade_plan (UI lê de lá)
    if warnings and isinstance(sig.trade_plan, dict):
        existing = sig.trade_plan.get("quality_warnings") or []
        sig.trade_plan["quality_warnings"] = existing + warnings

    return min(penalty, 2)


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
    # Denylist: junk/memecoin explicitamente excluído (chokepoint único —
    # cobre live endpoint, server-scan e, por consequência, o auto-trade).
    if SYMBOL_DENYLIST and _norm_sym(sig.symbol) in SYMBOL_DENYLIST:
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
    # Thresholds ajustados após rebalance do _compute_score (cap RR 5→3,
    # derivatives entram). Score equivalente "muito bom" caiu de ~80 pra ~75.
    # Gates de qualidade (MTF, RR, pattern, warnings) preservados — apenas
    # o score numérico foi recalibrado.
    # Cortes numéricos do score: V2 usa faixa comprimida (65/46/18); legado 75/65/52.
    # Os GATES de qualidade (mtf/rr/pattern/warnings) são idênticos nos dois — só o
    # número do score muda, porque a distribuição do score V2 é outra.
    c_aplus, c_a, c_b = (
        (SCORE_V2_TIER_APLUS, SCORE_V2_TIER_A, SCORE_V2_TIER_B)
        if SCORE_FORMULA_V2 else (75.0, 65.0, 52.0)
    )
    if (
        score >= c_aplus and mtf_score >= 0.5 and sig.risk_reward >= 2.5
        and not has_critical_warning and _has_confirming_pattern(sig)
    ):
        tier = "A+"
    elif score >= c_a and mtf_score >= 0.0 and sig.risk_reward >= 2.0:
        tier = "A"
    elif score >= c_b and sig.confidence >= MIN_CONFIDENCE_B and sig.risk_reward >= MIN_RR:
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

    # Derivativos: funding moderado same-dir / OI adverso → downgrade gradual
    deriv_penalty = _derivatives_tier_penalty(sig)
    tier_order = ["A+", "A", "B"]
    if tier in tier_order and deriv_penalty > 0:
        idx = tier_order.index(tier) + deriv_penalty
        if idx >= len(tier_order):
            return None  # nem B sobrevive → rejeita
        tier = tier_order[idx]

    # Gate TF × tier: exige tier mínimo conforme a confiabilidade do TF.
    # Ex.: scalp 15m só publica se A+; 1h só A/A+; 4h aceita todos. Corta o
    # grosso dos stops sem mexer no SL (que já é estrutural).
    if TF_TIER_GATE_ENABLED and tier is not None:
        min_rank = TF_MIN_TIER.get(sig.timeframe)
        if min_rank is not None and _TIER_RANK.get(tier, 0) < min_rank:
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


def _liquidity_from_ticker(ticker: dict) -> tuple[Optional[float], Optional[float]]:
    """Extrai (quote_vol_usd, spread_pct) de um ticker da binance_service (facade
    OKX). quote_vol_usd = volume_base × last; spread_pct = (ask-bid)/mid × 100.
    Reaproveita o ticker JÁ buscado na varredura (zero chamada externa extra) pra
    alimentar o veredito de liquidez do bot. Fail-soft: devolve (None, None)."""
    try:
        last = float(ticker.get("last") or 0)
        vol_base = float(ticker.get("volume") or 0)
        qvol = vol_base * last if (last > 0 and vol_base > 0) else None
        bid = float(ticker.get("bid") or 0)
        ask = float(ticker.get("ask") or 0)
        spread = None
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid * 100 if mid > 0 else None
        return qvol, spread
    except Exception:
        return None, None


async def _analyze_symbol_tf(symbol: str, tf: str) -> Optional[TradeSignal]:
    """Retorna o TradeSignal completo para (symbol, tf) ou None se falhar."""
    try:
        df = await fetch_ohlcv(symbol, tf, 300)
        if df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        # Retest re-arm (gated): re-dispara entrada no pullback à linha rompida.
        record_breakouts_and_retests(symbol, tf, df, patterns)
        current = float(df["close"].iloc[-1])
        primary_dir = determine_direction(ind, patterns, current)

        # Derivativos + MTF em paralelo
        liq_vol = liq_spread = None
        try:
            ticker = await fetch_ticker(symbol)
            change_24h = ticker.get("change", 0.0)
            liq_vol, liq_spread = _liquidity_from_ticker(ticker)
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
        sig = build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=derivatives, mtf=mtf, with_backtest=False,
        )
        if sig is not None:
            sig.quote_vol_usd = liq_vol
            sig.spread_pct = liq_spread
        return sig
    except Exception:
        return None


async def _best_tf_for_symbol(symbol: str) -> Optional[tuple]:
    """Roda SCAN_TFS em paralelo, devolve (signal, score) do melhor TF.
    Quando HIGH_TF_PATTERNS_ENABLED, adiciona os TFs altos (12h/1D/3D/1W),
    avaliados com cache TTL e peso de relevância (no-op com a flag off)."""
    results = list(await asyncio.gather(*[_analyze_symbol_tf(symbol, tf) for tf in SCAN_TFS]))
    if HIGH_TF_PATTERNS_ENABLED:
        results += list(await asyncio.gather(*[
            _htf_signal_cached(symbol, tf, lambda tf=tf: _analyze_symbol_tf(symbol, tf))
            for tf in HIGH_TF_LIST
        ]))
    confirm_dirs = _htf_confirm_dirs(results) if (HIGH_TF_PATTERNS_ENABLED and HIGH_TF_CONFIRM_ENABLED) else set()
    scored: List[tuple] = []
    for sig in results:
        if sig is None or sig.direction == SignalDirection.NEUTRAL:
            continue
        score = _score_with_htf_confirm(sig, _compute_score(sig), confirm_dirs)
        scored.append((sig, score))
    if not scored:
        return None
    return max(scored, key=lambda x: x[1])


# ── Position sizing dinâmico (Issue #4 — Fase 1.2) ───────────────────────────
# Constantes:
#  - Kelly fracionário 25%: Kelly cheio é estatisticamente "ótimo" mas leva a
#    drawdowns brutais; fracionário reduz variância. Indústria usa 25-50%.
#  - ATR de referência 2%: típico de cripto liquid; size cresce/encolhe em
#    proporção inversa. Cap em [0.5, 2.0] pra evitar explosão.
#  - WR fallback por tier quando prob_tp1 não está pronta (calib < 30 trades).
KELLY_FRACTION = 0.25
ATR_REFERENCE_PCT = 0.02
ATR_MULT_FLOOR = 0.5
ATR_MULT_CEIL = 2.0
SIZE_MIN_PCT = 0.25
SIZE_MAX_PCT = 1.0

# Fallback de WR por tier (alinhado com backtests recentes).
# Usado quando prob_tp1 está None (calibração ainda imatura).
_TIER_WR_FALLBACK = {"A+": 0.62, "A": 0.55, "B": 0.50}


# ── Edge tagging (sinais com win-rate historicamente acima do baseline) ──────
# Funding alinhado: o squeeze/extremo a favor da direção do trade. Mapeamento:
#   bullish_squeeze (shorts pagando → tende a esticar p/ CIMA)  → favorece LONG
#   extreme_short   (shorts lotados → risco de squeeze p/ cima) → favorece LONG
#   bearish_squeeze (longs pagando → tende a esticar p/ BAIXO)  → favorece SHORT
#   extreme_long    (longs lotados → risco de squeeze p/ baixo) → favorece SHORT
# 'neutral' NÃO conta como edge (é o baseline, n grande). Conf de padrão e nº de
# TFs alinhados são env-tunáveis.
_FUNDING_EDGE_LONG = {"bullish_squeeze", "extreme_short"}
_FUNDING_EDGE_SHORT = {"bearish_squeeze", "extreme_long"}
_EDGE_PATTERN_MIN_CONF = float(os.getenv("EDGE_PATTERN_MIN_CONF", "0.65"))
_EDGE_MTF_MIN_COUNT = int(os.getenv("EDGE_MTF_MIN_COUNT", "2"))


def _compute_edges(sig: TradeSignal, tier: str) -> tuple[list[str], int]:
    """Retorna (tags legíveis, contagem). Read-only, fail-soft, SEMPRE roda —
    barato e usado tanto pra sizing quanto pra exibição no app/push."""
    tags: list[str] = []
    direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)

    # 1. Tier A+ — o diferenciador mais forte do histórico (~92% wr).
    if tier == "A+":
        tags.append("A+")

    # 2. Funding alinhado (squeeze/extremo a favor da direção).
    try:
        der = sig.derivatives
        fs = getattr(der, "funding_sentiment", None) if der is not None else None
        if fs:
            fav = _FUNDING_EDGE_LONG if direction == "long" else _FUNDING_EDGE_SHORT
            if fs in fav:
                tags.append("funding")
    except Exception:
        pass

    # 3. Padrão forte alinhado à direção (confiança >= piso).
    try:
        for p in (sig.patterns or []):
            p_dir = p.direction.value if hasattr(p.direction, "value") else str(p.direction)
            if p_dir == direction and float(p.confidence or 0) >= _EDGE_PATTERN_MIN_CONF:
                tags.append("padrão")
                break
    except Exception:
        pass

    # 4. MTF alinhado (TFs superiores a favor) — gate histórico 82% wr.
    try:
        mtf = sig.mtf
        ac = getattr(mtf, "aligned_count", None) if mtf is not None else None
        if ac is not None and int(ac) >= _EDGE_MTF_MIN_COUNT:
            tags.append("MTF")
    except Exception:
        pass

    return tags, len(tags)


def _compute_dynamic_size(
    score: float,
    tier: str,
    risk_reward: float,
    prob_tp1: Optional[float],
    atr_pct: Optional[float],
) -> tuple[Optional[float], str]:
    """
    Position sizing dinâmico via Kelly fracionado × score × volatilidade.

    Fórmula:
        kelly = (p × b − (1−p)) / b   onde b = RR
        size  = kelly × KELLY_FRACTION × (score/100) × vol_mult
        vol_mult = clamp(ATR_REF / atr_pct, FLOOR, CEIL)

    Cap final [SIZE_MIN, SIZE_MAX].

    Returns (size_pct, rationale_text). Retorna (None, motivo) se inputs
    insuficientes — caller pode optar por usar risk_pct fixo como fallback.
    """
    # p_win: prob calibrada, ou fallback por tier
    p = prob_tp1 if prob_tp1 is not None else _TIER_WR_FALLBACK.get(tier)
    if p is None or risk_reward <= 0:
        return None, "Dados insuficientes para sizing dinâmico"

    # Kelly cheio
    b = max(risk_reward, 0.5)  # RR muito baixo torna Kelly negativo → clamp
    kelly = (p * b - (1.0 - p)) / b
    if kelly <= 0:
        return None, f"Kelly negativo (p={p:.2f}, RR={b:.1f}) — setup sem edge esperado"

    # Multiplicador de volatilidade (ATR menor → posição maior; ATR maior → menor)
    if atr_pct is None or atr_pct <= 0:
        vol_mult = 1.0
        vol_note = "ATR n/d"
    else:
        raw_mult = ATR_REFERENCE_PCT / atr_pct
        vol_mult = max(ATR_MULT_FLOOR, min(ATR_MULT_CEIL, raw_mult))
        vol_note = f"ATR {atr_pct*100:.1f}% → mult {vol_mult:.2f}"

    score_mult = max(0.0, min(1.0, score / 100.0))

    raw_size = kelly * KELLY_FRACTION * score_mult * vol_mult * 100.0  # em %
    final_size = max(SIZE_MIN_PCT, min(SIZE_MAX_PCT, raw_size))

    rationale = (
        f"p={p*100:.0f}% × RR {b:.1f} → Kelly {kelly*100:.1f}% × "
        f"{KELLY_FRACTION:.0%} × score {score_mult:.2f} × {vol_note} "
        f"= {raw_size:.2f}% (cap → {final_size:.2f}%)"
    )
    return round(final_size, 3), rationale


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

    # Chase ESTRUTURAL (gated): esticamento desde a base do movimento.
    struct_chase_atr = None
    struct_chase_level = None
    if STRUCT_CHASE_ENABLED and cp is not None and atr and atr > 0 and sig.indicators:
        base = sig.indicators.pivot_low if is_long else sig.indicators.pivot_high
        if base is not None and base > 0:
            sdelta = (cp - base) if is_long else (base - cp)
            struct_chase_atr = round(sdelta / atr, 2)
            if struct_chase_atr >= STRUCT_CHASE_CHASING_ATR:
                struct_chase_level = "chasing"
            elif struct_chase_atr >= STRUCT_CHASE_EXTENDED_ATR:
                struct_chase_level = "extended"
            else:
                struct_chase_level = "ok"

    # Origem retest re-arm: algum padrão a favor da direção é um retest ativo?
    retest_armed = None
    try:
        if sig.patterns:
            retest_armed = any(
                getattr(p, "retest_active", False) and p.direction == sig.direction
                for p in sig.patterns
            ) or None
    except Exception:
        retest_armed = None

    warnings = (tp or {}).get("quality_warnings", []) if tp else []
    if chase_level == "chasing":
        warnings = warnings + [
            f"⚠ Preço {chase_atr}×ATR acima do entry — esperar pullback à zona ou pular."
        ]
    if struct_chase_level == "chasing":
        warnings = warnings + [
            f"⚠ Preço {struct_chase_atr}×ATR acima da base do movimento — pernada esticada, risco de comprar topo."
        ]

    # P(TP1)/P(TP2) calibradas — lookup sync no cache (None se calib não pronta)
    try:
        from services.calibration_service import (
            prob_tp1_for_score_sync, prob_tp2_for_score_sync,
        )
        prob_tp1 = prob_tp1_for_score_sync(score)
        prob_tp2 = prob_tp2_for_score_sync(score)
    except Exception:
        prob_tp1 = None
        prob_tp2 = None

    # Edges (A+/funding/padrão/MTF) — computados ANTES do sizing/veredito pra
    # alimentar ambos (read-only, alimenta exibição no app + sizing no bot).
    try:
        edge_tags, edge_score = _compute_edges(sig, tier)
    except Exception:
        edge_tags, edge_score = [], 0

    # Position sizing dinâmico (Issue #4) — Kelly fracionado × score × volatilidade
    atr_pct_val = sig.indicators.atr_pct if sig.indicators else None
    suggested_size_pct, size_rationale = _compute_dynamic_size(
        score=score,
        tier=tier,
        risk_reward=sig.risk_reward,
        prob_tp1=prob_tp1,
        atr_pct=atr_pct_val,
    )
    # Espelha o EDGE_SIZING do bot no tamanho EXIBIDO (app conta a mesma história
    # que o bot). Gated: NO-OP quando EDGE_SIZING_ENABLED=false. Re-clampa ao teto
    # documentado [0.25%, 1.0%]. Fail-soft — qualquer erro mantém o size original.
    if suggested_size_pct is not None:
        try:
            from services.shadow_trade_service import _edge_mult
            _em, _ = _edge_mult({"edge_tags": edge_tags, "edge_score": edge_score})
            if _em != 1.0:
                suggested_size_pct = round(min(max(suggested_size_pct * _em, 0.25), 1.0), 2)
                size_rationale = (size_rationale or "size dinâmico") + f" · edge ×{_em:.2f}"
        except Exception:
            pass
        # Espelha o LIQ_TIER_SIZING do bot — mão menor em moeda magra. Mesma
        # história no app. Gated: NO-OP quando LIQ_TIER_SIZING_ENABLED=false.
        try:
            from services.shadow_trade_service import _liq_tier_mult
            _lm, _ = _liq_tier_mult({"quote_vol_usd": getattr(sig, "quote_vol_usd", None)})
            if _lm != 1.0:
                suggested_size_pct = round(min(max(suggested_size_pct * _lm, 0.25), 1.0), 2)
                size_rationale = (size_rationale or "size dinâmico") + f" · liq ×{_lm:.2f}"
        except Exception:
            pass

    # Veredito do bot (mesma lógica/limites do loop de execução — fonte única).
    # Read-only: NÃO toca no loop real; só anexa "o bot operaria / não operaria"
    # à recomendação, pra o app exibir a indicação vetada pelo critério do bot.
    q_vol = getattr(sig, "quote_vol_usd", None)
    sp_pct = getattr(sig, "spread_pct", None)
    try:
        from services.shadow_trade_service import exec_verdict
        bot_verdict = exec_verdict({
            "symbol": sig.symbol,
            "entry": sig.entry,
            "stop_loss": sig.stop_loss,
            "tp1": sig.tp1,
            "tp2": sig.tp2,
            "prob_tp1": prob_tp1,
            "quote_vol_usd": q_vol,
            "spread_pct": sp_pct,
            "score": score,
            "edge_score": edge_score,
        })
    except Exception:
        bot_verdict = None

    # Grau de entrada manual (fonte única p/ push + app). "avoid" se reprova na
    # qualidade; senão "good" se score ≥ piso de auto-execução, "manual" abaixo.
    try:
        from services.shadow_trade_service import SCORE_MIN as _EXEC_SCORE_MIN
    except Exception:
        _EXEC_SCORE_MIN = 57.0
    if bot_verdict is not None and bot_verdict.get("ok") is False:
        entry_grade = "avoid"
    elif score >= _EXEC_SCORE_MIN:
        entry_grade = "good"
    else:
        entry_grade = "manual"

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
        struct_chase_atr=struct_chase_atr,
        struct_chase_level=struct_chase_level,
        retest_armed=retest_armed,
        prob_tp1=prob_tp1,
        prob_tp2=prob_tp2,
        suggested_size_pct=suggested_size_pct,
        size_rationale=size_rationale,
        quote_vol_usd=q_vol,
        spread_pct=sp_pct,
        bot_verdict=bot_verdict,
        entry_grade=entry_grade,
        edge_tags=edge_tags,
        edge_score=edge_score,
    )


async def _analyze_candles_for_tf(
    symbol: str,
    tf: str,
    df,
    *,
    derivatives_cached=None,  # já resolvido por símbolo (compartilhado entre TFs)
    derivatives_done: bool = False,
    change_24h_cached: Optional[float] = None,
    liquidity_cached: Optional[tuple] = None,  # (quote_vol_usd, spread_pct) resolvido por símbolo
) -> Optional[TradeSignal]:
    """Variante de _analyze_symbol_tf que recebe candles já baixados (frontend).

    Ticker e derivatives são por SÍMBOLO (não dependem do TF), então o caller
    resolve uma vez e passa pré-computado — economiza ~67% das chamadas externas
    quando processa 3 TFs do mesmo símbolo.
    """
    try:
        if df is None or df.empty or len(df) < 80:
            return None
        ind = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        # Retest re-arm (gated): re-dispara entrada no pullback à linha rompida.
        record_breakouts_and_retests(symbol, tf, df, patterns)
        current = float(df["close"].iloc[-1])
        primary_dir = determine_direction(ind, patterns, current)

        liq_vol, liq_spread = (liquidity_cached or (None, None))

        # Resolve ticker/derivatives só se o caller não passou (fallback)
        if not derivatives_done:
            try:
                from services.binance_service import fetch_ticker
                ticker = await fetch_ticker(symbol)
                change_24h_cached = ticker.get("change", 0.0)
                liq_vol, liq_spread = _liquidity_from_ticker(ticker)
            except Exception:
                change_24h_cached = 0.0
            try:
                derivatives_cached = await analyze_derivatives(symbol, change_24h_cached or 0.0)
            except Exception:
                derivatives_cached = None

        # MTF depende do TF — sempre por-TF
        try:
            mtf = await analyze_mtf(symbol, tf, primary_dir)
        except Exception:
            mtf = None

        sig = build_trade_signal(
            symbol, tf, df, ind, patterns,
            derivatives=derivatives_cached, mtf=mtf, with_backtest=False,
        )
        if sig is not None:
            sig.quote_vol_usd = liq_vol
            sig.spread_pct = liq_spread
        return sig
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

    # Limita concorrência por símbolo (3 TFs em paralelo, 12 símbolos em paralelo)
    sem_sym = asyncio.Semaphore(12)

    async def _process_symbol(symbol: str, tfs: List[tuple]) -> Optional[tuple]:
        async with sem_sym:
            # Ticker + derivatives são por-símbolo — resolve 1x e compartilha
            # entre todos os TFs do mesmo símbolo (corta ~67% das chamadas
            # externas pesadas quando o batch tem 3 TFs/símbolo).
            liq = (None, None)
            try:
                from services.binance_service import fetch_ticker
                ticker = await fetch_ticker(symbol)
                change_24h = ticker.get("change", 0.0)
                liq = _liquidity_from_ticker(ticker)
            except Exception:
                change_24h = 0.0
            try:
                derivatives_cached = await analyze_derivatives(symbol, change_24h)
            except Exception:
                derivatives_cached = None

            results = await asyncio.gather(*[
                _analyze_candles_for_tf(
                    symbol, tf, df,
                    derivatives_cached=derivatives_cached,
                    derivatives_done=True,
                    change_24h_cached=change_24h,
                    liquidity_cached=liq,
                )
                for tf, df in tfs
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

    # Auto-learning (nível 2): multiplicadores + block list por bucket.
    # Dormente por bucket — só desperta quando bucket atinge amostra mínima.
    # Falha aberta se DB/learning indisponível.
    auto_adj = {}
    try:
        from services.learning_service import compute_auto_adjustments
        # days omitido ⇒ usa LEARNING_LOOKBACK_DAYS (default 0 = todo o histórico)
        auto_adj = await compute_auto_adjustments()
    except Exception as e:
        import logging as _log
        _log.warning(f"[learning] auto-adjust falhou (fail-open): {e}")

    for best in best_per_symbol:
        if best is None:
            continue
        sig, score = best
        # Classifica tier provisório (pra lookup de bucket tier_tf antes do ajuste)
        tier_prov = _classify_tier(sig, score)

        # Aplica auto-learning: multiplica score, checa block list
        if auto_adj.get("enabled"):
            try:
                from services.learning_service import apply_score_adjustment
                adj_res = apply_score_adjustment(sig, score, auto_adj, tier_provisional=tier_prov)
                if adj_res.get("blocked"):
                    import logging as _log
                    _log.info(f"[learning] BLOCK {sig.symbol}: {adj_res.get('block_reason')}")
                    continue
                if adj_res.get("multiplier", 1.0) != 1.0:
                    import logging as _log
                    score = adj_res["score"]
                    _log.info(
                        f"[learning] adjust {sig.symbol} {sig.timeframe}: "
                        f"score×{adj_res['multiplier']:.2f} → {score:.1f} "
                        f"({', '.join(adj_res.get('matched_buckets') or [])})"
                    )
            except Exception as e:
                import logging as _log
                _log.warning(f"[learning] apply_score_adjustment falhou (fail-open): {e}")

        # Re-classifica com score ajustado (pode subir/descer tier)
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

    # Portfolio risk guard (#5): aplica caps de correlação/exposição
    recommendations = await _apply_portfolio_guard(recommendations)
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
        # Retest re-arm (gated): re-dispara entrada no pullback à linha rompida.
        record_breakouts_and_retests(symbol, tf, df, patterns)
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
    results = list(await asyncio.gather(*[
        _analyze_symbol_tf_server(svc, symbol, tf) for tf in SCAN_TFS
    ]))
    # TFs altos (gated): avaliados com cache TTL + peso de relevância.
    if HIGH_TF_PATTERNS_ENABLED:
        results += list(await asyncio.gather(*[
            _htf_signal_cached(symbol, tf, lambda tf=tf: _analyze_symbol_tf_server(svc, symbol, tf))
            for tf in HIGH_TF_LIST
        ]))
    confirm_dirs = _htf_confirm_dirs(results) if (HIGH_TF_PATTERNS_ENABLED and HIGH_TF_CONFIRM_ENABLED) else set()
    scored: List[tuple] = []
    for sig in results:
        if sig is None or sig.direction == SignalDirection.NEUTRAL:
            continue
        score = _score_with_htf_confirm(sig, _compute_score(sig), confirm_dirs)
        scored.append((sig, score))
    if not scored:
        return None
    return max(scored, key=lambda x: x[1])


async def get_recommendations_via_vision(
    top_n: int = 30, apply_guard: bool = True
) -> List[Recommendation]:
    """
    Versão server-side pro Railway. Escolhe fonte dinamicamente:
      • BINANCE_PROXY_URL setado → Binance Futures (mesmos dados do app)
      • Senão → Binance Vision (spot)

    Nome mantido por compat — agora é "via server" mais genérico.

    `apply_guard` (default True): aplica o portfolio_guard (corta pras ~melhores
    por exposição/correlação). Passe False pra obter a lista AMPLA crua (display/
    observação) — a execução aplica o guard separadamente (ver EXEC_UNIVERSE_DECOUPLE).
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

    # Pré-filtro do denylist: descarta junk antes da análise (economia de CPU/HTTP).
    # A garantia real está no _classify_tier; isto é só otimização.
    if SYMBOL_DENYLIST:
        _before = len(symbols)
        symbols = [s for s in symbols if _norm_sym(s) not in SYMBOL_DENYLIST]
        if len(symbols) != _before:
            _log.info(f"[server-scan] denylist removeu {_before - len(symbols)} símbolo(s)")
        if not symbols:
            return []

    recommendations: List[Recommendation] = []
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

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

    # Auto-learning (igual ao caminho batch)
    auto_adj = {}
    try:
        from services.learning_service import compute_auto_adjustments
        # days omitido ⇒ usa LEARNING_LOOKBACK_DAYS (default 0 = todo o histórico)
        auto_adj = await compute_auto_adjustments()
    except Exception as e:
        _log.warning(f"[learning] auto-adjust falhou (fail-open): {e}")

    for _symbol, best in all_results:
        if best is None:
            continue
        sig, score = best
        tier_prov = _classify_tier_vision(sig, score)

        # Auto-learning: bloqueia bucket catastrófico + ajusta score
        if auto_adj.get("enabled"):
            try:
                from services.learning_service import apply_score_adjustment
                adj_res = apply_score_adjustment(sig, score, auto_adj, tier_provisional=tier_prov)
                if adj_res.get("blocked"):
                    _log.info(f"[server-scan][learning] BLOCK {sig.symbol}: {adj_res.get('block_reason')}")
                    continue
                if adj_res.get("multiplier", 1.0) != 1.0:
                    score = adj_res["score"]
                    _log.info(
                        f"[server-scan][learning] adjust {sig.symbol} {sig.timeframe}: "
                        f"×{adj_res['multiplier']:.2f} → {score:.1f} "
                        f"({', '.join(adj_res.get('matched_buckets') or [])})"
                    )
            except Exception as e:
                _log.warning(f"[learning] apply falhou (fail-open): {e}")

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

    # Portfolio risk guard (#5) — pulável pro caminho de display amplo (decouple).
    if apply_guard:
        recommendations = await _apply_portfolio_guard(recommendations)
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

    # Portfolio risk guard (#5)
    recommendations = await _apply_portfolio_guard(recommendations)

    _cache["ts"] = now
    _cache["data"] = recommendations
    return recommendations


async def _apply_portfolio_guard(recommendations: List[Recommendation]) -> List[Recommendation]:
    """
    Filtra recomendações aplicando caps de correlação/exposição (Issue #5).
    Posições "abertas" = snapshots com status='open' (proxy até #11 trazer
    integração Bybit). Logs drops com motivo claro pra debug.
    """
    if not recommendations:
        return recommendations
    # Observação/estudo (DEV): guard OFF → painel mostra o universo amplo inteiro,
    # sem o corte pras ~5 melhores. PRD mantém default ON (execução real).
    if not PORTFOLIO_GUARD_ENABLED:
        import logging as _log
        _log.info(
            f"[portfolio-guard] DESLIGADO (PORTFOLIO_GUARD_ENABLED=false) — "
            f"passando {len(recommendations)} recs sem cap (modo observação/DEV)"
        )
        return recommendations
    try:
        from services import portfolio_service
        positions = await portfolio_service.get_open_positions()
        summary = portfolio_service._summarize(positions)
        kept, dropped = portfolio_service.filter_recommendations(
            recommendations, open_summary=summary,
        )
        for d in dropped:
            import logging as _log
            _log.info(
                f"[portfolio-guard] DROP {d['symbol']} {d['direction']} "
                f"({d.get('tier')}): {d['reason']}"
            )
        return kept
    except Exception as e:
        import logging as _log
        _log.warning(f"[portfolio-guard] falhou (passando todos): {e}")
        return recommendations
