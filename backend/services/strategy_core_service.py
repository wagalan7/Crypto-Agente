"""R07D/R08D — núcleo de estratégia PURO, compartilhado por pesquisa.

`CANDIDATE_POLICY`: um único conjunto de regras que simulação, replay e (no
futuro) seleção operacional consomem — sem cópia independente por consumidor.

Contrato de pureza: nada aqui lê banco, rede, arquivo, cache ou relógio do
sistema. O estado ponto-no-tempo, a configuração e o instante da decisão são
SEMPRE injetados. A única leitura de ambiente é o seletor de modo, e o modo
default é `inactive`: por padrão este núcleo não produz nada aproveitável.

O que ele NÃO faz: mutar a recomendação champion, escrever score/tier no
snapshot, chamar o executor, emitir ordem, sizing, probabilidade ou tier.
`selection_adapter` existe para a FUTURA seleção operacional e devolve sempre
`executable=False`: a rota real permanece inacessível ao candidato.

Vocabulário: `side` é `long`/`short`; preços são preços do ativo; `atr` está na
unidade do preço; `adx` é o ADX bruto. Evidência essencial ausente vira
`UNKNOWN`, nunca zero e nunca sinal neutro inventado.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

CORE_VERSION = "R07D_STRATEGY_CORE_V1"
CONTRACT = "CANDIDATE_POLICY"
MODE_ENV = "R07_STRATEGY_CORE_MODE"
MODE_INACTIVE = "inactive"
MODE_SIMULATION = "simulation"

SIDE_LONG = "long"
SIDE_SHORT = "short"
SIDES = (SIDE_LONG, SIDE_SHORT)

PLAYBOOK_TREND_PULLBACK = "TREND_PULLBACK"
PLAYBOOK_TREND_BREAKOUT = "TREND_BREAKOUT"
PLAYBOOK_RANGE_REVERSION = "RANGE_REVERSION"
PLAYBOOKS = (PLAYBOOK_TREND_PULLBACK, PLAYBOOK_TREND_BREAKOUT,
             PLAYBOOK_RANGE_REVERSION)
PLAYBOOK_VERSIONS = {
    PLAYBOOK_TREND_PULLBACK: "TREND_PULLBACK_V1",
    PLAYBOOK_TREND_BREAKOUT: "TREND_BREAKOUT_V1",
    PLAYBOOK_RANGE_REVERSION: "RANGE_REVERSION_V1",
}

STATE_ELIGIBLE = "ELIGIBLE"
STATE_INELIGIBLE = "INELIGIBLE"
STATE_UNKNOWN = "UNKNOWN"
DECISION_STATES = (STATE_ELIGIBLE, STATE_INELIGIBLE, STATE_UNKNOWN)

# ── Vocabulário fechado de motivos ──────────────────────────────────────────
OK = "OK"
# estado / relógio
BARS_INSUFFICIENT = "BARS_INSUFFICIENT"
BARS_UNORDERED = "BARS_UNORDERED"
BARS_GAPPED = "BARS_GAPPED"
BAR_NOT_CLOSED = "BAR_NOT_CLOSED"
DECISION_BEFORE_BAR_CLOSE = "DECISION_BEFORE_BAR_CLOSE"
STATE_STALE = "STATE_STALE"
# evidência
FEATURE_MISSING = "FEATURE_MISSING"
TREND_UNKNOWN = "TREND_UNKNOWN"
TREND_NOT_CONFIRMED = "TREND_NOT_CONFIRMED"
RANGE_UNKNOWN = "RANGE_UNKNOWN"
RANGE_NOT_PROVEN = "RANGE_NOT_PROVEN"
PULLBACK_NOT_AT_STRUCTURE = "PULLBACK_NOT_AT_STRUCTURE"
TRIGGER_ABSENT = "TRIGGER_ABSENT"
BREAKOUT_NOT_CONFIRMED = "BREAKOUT_NOT_CONFIRMED"
VOLUME_UNKNOWN = "VOLUME_UNKNOWN"
VOLUME_INSUFFICIENT = "VOLUME_INSUFFICIENT"
RETEST_UNKNOWN = "RETEST_UNKNOWN"
RETEST_MISSING = "RETEST_MISSING"
BORDER_UNAVAILABLE = "BORDER_UNAVAILABLE"
REJECTION_NOT_CONFIRMED = "REJECTION_NOT_CONFIRMED"
# geometria
STOP_STRUCTURE_UNAVAILABLE = "STOP_STRUCTURE_UNAVAILABLE"
TARGET_STRUCTURE_UNAVAILABLE = "TARGET_STRUCTURE_UNAVAILABLE"
TP_SEPARATION_BELOW_FLOOR = "TP_SEPARATION_BELOW_FLOOR"
RR_BELOW_FLOOR = "RR_BELOW_FLOOR"
GEOMETRY_INVALID = "GEOMETRY_INVALID"
# HTF / contratendência
HTF_CONFLICT_BLOCKED = "HTF_CONFLICT_BLOCKED"
HTF_UNKNOWN = "HTF_UNKNOWN"
HTF_EVIDENCE_UNPROVEN = "HTF_EVIDENCE_UNPROVEN"
# arbitragem / modo
ARBITRATION_SIDE_CONFLICT = "ARBITRATION_SIDE_CONFLICT"
NO_ELIGIBLE_PLAYBOOK = "NO_ELIGIBLE_PLAYBOOK"
CORE_INACTIVE = "CORE_INACTIVE"
LIVE_ROUTE_UNAVAILABLE = "LIVE_ROUTE_UNAVAILABLE"

REASON_CODES = frozenset({
    OK, BARS_INSUFFICIENT, BARS_UNORDERED, BARS_GAPPED, BAR_NOT_CLOSED,
    DECISION_BEFORE_BAR_CLOSE, STATE_STALE, FEATURE_MISSING,
    TREND_UNKNOWN, TREND_NOT_CONFIRMED, RANGE_UNKNOWN, RANGE_NOT_PROVEN,
    PULLBACK_NOT_AT_STRUCTURE, TRIGGER_ABSENT, BREAKOUT_NOT_CONFIRMED,
    VOLUME_UNKNOWN, VOLUME_INSUFFICIENT, RETEST_UNKNOWN, RETEST_MISSING,
    BORDER_UNAVAILABLE, REJECTION_NOT_CONFIRMED, STOP_STRUCTURE_UNAVAILABLE,
    TARGET_STRUCTURE_UNAVAILABLE, TP_SEPARATION_BELOW_FLOOR, RR_BELOW_FLOOR,
    GEOMETRY_INVALID, HTF_CONFLICT_BLOCKED, HTF_UNKNOWN, HTF_EVIDENCE_UNPROVEN,
    ARBITRATION_SIDE_CONFLICT, NO_ELIGIBLE_PLAYBOOK, CORE_INACTIVE,
    LIVE_ROUTE_UNAVAILABLE,
})

LIMITATIONS = [
    "Decisão de pesquisa: não é ordem, sizing, probabilidade nem tier.",
    "Regra executada sobre vela FECHADA; vela em formação não decide.",
    "Ausência de evidência essencial vira UNKNOWN — nunca zero, nunca neutro.",
    "Stop vem da estrutura: R:R insuficiente reprova o setup, não alarga o stop.",
    "Nome de padrão não prova rompimento; regime NORMAL não prova lateralidade.",
    "Conflito HTF confirmado bloqueia continuação — não é apenas rebaixamento.",
]


def selected_mode() -> str:
    """Modo do núcleo. Desconhecido ou `live` ⇒ `inactive` (fail-closed)."""
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_SIMULATION if value == MODE_SIMULATION else MODE_INACTIVE


def core_active() -> bool:
    return selected_mode() == MODE_SIMULATION


# ── Primitivas numéricas ────────────────────────────────────────────────────
def _finite(value: Any, name: str, *, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: número finito obrigatório")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}: número finito obrigatório")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name}: abaixo do mínimo {minimum}")
    return number


def _optional_finite(value: Any, name: str, *, minimum: Optional[float] = None) -> Optional[float]:
    return None if value is None else _finite(value, name, minimum=minimum)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: inteiro obrigatório")
    if value < minimum:
        raise ValueError(f"{name}: abaixo do mínimo {minimum}")
    return value


def _sign(side: str) -> int:
    return 1 if side == SIDE_LONG else -1


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ── Estado ponto-no-tempo ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    closed: bool = True

    def __post_init__(self) -> None:
        _integer(self.open_time_ms, "open_time_ms")
        for key in ("open", "high", "low", "close"):
            _finite(getattr(self, key), key, minimum=1e-12)
        _optional_finite(self.volume, "volume", minimum=0.0)
        if not isinstance(self.closed, bool):
            raise ValueError("closed: booleano obrigatório")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC inconsistente")

    @property
    def bar_range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class HigherTimeframeView:
    """Leitura de um timeframe superior, com instante de observação provado."""
    timeframe: str
    as_of_ms: int
    direction: Optional[str] = None   # long/short; None = desconhecido
    confirmed: bool = False
    strength: Optional[float] = None  # 0..1; None = força desconhecida

    def __post_init__(self) -> None:
        if not isinstance(self.timeframe, str) or not self.timeframe.strip():
            raise ValueError("timeframe: obrigatório")
        _integer(self.as_of_ms, "as_of_ms")
        if self.direction is not None and self.direction not in SIDES:
            raise ValueError("direction: long, short ou None")
        if not isinstance(self.confirmed, bool):
            raise ValueError("confirmed: booleano obrigatório")
        _optional_finite(self.strength, "strength", minimum=0.0)


@dataclass(frozen=True)
class MarketState:
    """Fotografia ponto-no-tempo. Campo ausente é `None`, jamais 0."""
    symbol: str
    timeframe: str
    bar_ms: int
    as_of_ms: int
    bars: Tuple[Candle, ...]
    atr: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    adx: Optional[float] = None
    rsi: Optional[float] = None
    volume_ma: Optional[float] = None
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_touches: Optional[int] = None
    retest_confirmed: Optional[bool] = None
    target_levels: Tuple[float, ...] = ()
    higher_timeframes: Tuple[HigherTimeframeView, ...] = ()
    regime_label: Optional[str] = None
    funding_pct: Optional[float] = None
    patterns: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for key in ("symbol", "timeframe"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 64:
                raise ValueError(f"{key}: identificador inválido")
        _integer(self.bar_ms, "bar_ms", minimum=1)
        _integer(self.as_of_ms, "as_of_ms")
        if not isinstance(self.bars, tuple) or not self.bars:
            raise ValueError("bars: tupla não vazia obrigatória")
        if any(not isinstance(bar, Candle) for bar in self.bars):
            raise ValueError("bars: apenas Candle")
        for key in ("atr", "ema_fast", "ema_slow", "swing_high", "swing_low",
                    "range_high", "range_low"):
            _optional_finite(getattr(self, key), key, minimum=1e-12)
        for key in ("adx", "rsi", "funding_pct"):
            _optional_finite(getattr(self, key), key)
        _optional_finite(self.volume_ma, "volume_ma", minimum=0.0)
        if self.range_touches is not None:
            _integer(self.range_touches, "range_touches")
        if self.retest_confirmed is not None and not isinstance(self.retest_confirmed, bool):
            raise ValueError("retest_confirmed: booleano ou None")
        if not isinstance(self.target_levels, tuple):
            raise ValueError("target_levels: tupla")
        for level in self.target_levels:
            _finite(level, "target_level", minimum=1e-12)
        if not isinstance(self.higher_timeframes, tuple):
            raise ValueError("higher_timeframes: tupla")
        if any(not isinstance(view, HigherTimeframeView) for view in self.higher_timeframes):
            raise ValueError("higher_timeframes: apenas HigherTimeframeView")
        if not isinstance(self.patterns, tuple):
            raise ValueError("patterns: tupla")

    @property
    def trigger_bar(self) -> Candle:
        """Última vela — só vira gatilho depois de `validate_state`."""
        return self.bars[-1]


# ── Configuração explícita e congelável ─────────────────────────────────────
@dataclass(frozen=True)
class CoreConfig:
    min_bars: int = 60
    max_state_age_bars: int = 2
    adx_trend_min: float = 25.0
    adx_range_max: float = 20.0
    atr_stop_buffer: float = 0.3
    entry_band_atr: float = 0.4
    pullback_max_distance_pct: float = 0.04
    pullback_lookback_bars: int = 6
    structure_lookback_bars: int = 10
    breakout_lookback_bars: int = 20
    breakout_min_atr: float = 0.25
    breakout_volume_mult: float = 1.2
    require_retest: bool = False
    range_min_touches: int = 4
    range_min_width_atr: float = 2.0
    range_border_tolerance_atr: float = 0.25
    rejection_wick_fraction: float = 0.5
    range_target_fraction: float = 0.9
    min_rr_tp1: float = 1.0
    min_rr_tp2: float = 1.8
    min_tp_separation_atr: float = 0.5
    require_htf_alignment: bool = True
    htf_conflict_min_strength: float = 0.6
    playbook_priority: Tuple[str, ...] = PLAYBOOKS

    def __post_init__(self) -> None:
        for name in ("min_bars", "max_state_age_bars", "pullback_lookback_bars",
                     "structure_lookback_bars", "breakout_lookback_bars",
                     "range_min_touches"):
            _integer(getattr(self, name), name, minimum=0)
        for name in ("adx_trend_min", "adx_range_max", "atr_stop_buffer",
                     "entry_band_atr", "pullback_max_distance_pct", "breakout_min_atr",
                     "breakout_volume_mult", "range_min_width_atr",
                     "range_border_tolerance_atr", "rejection_wick_fraction",
                     "range_target_fraction", "min_rr_tp1", "min_rr_tp2",
                     "min_tp_separation_atr", "htf_conflict_min_strength"):
            _finite(getattr(self, name), name, minimum=0.0)
        for name in ("require_retest", "require_htf_alignment"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name}: booleano obrigatório")
        if self.min_bars < 5:
            raise ValueError("min_bars: mínimo 5 barras")
        if self.adx_range_max > self.adx_trend_min:
            raise ValueError("adx_range_max não pode exceder adx_trend_min")
        if not 0.0 < self.range_target_fraction <= 1.0:
            raise ValueError("range_target_fraction deve estar em (0, 1]")
        if not 0.0 < self.rejection_wick_fraction <= 1.0:
            raise ValueError("rejection_wick_fraction deve estar em (0, 1]")
        if self.min_rr_tp2 < self.min_rr_tp1:
            raise ValueError("min_rr_tp2 não pode ser menor que min_rr_tp1")
        if tuple(sorted(self.playbook_priority)) != tuple(sorted(PLAYBOOKS)):
            raise ValueError("playbook_priority deve conter exatamente os playbooks")

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for spec in fields(self):
            value = getattr(self, spec.name)
            payload[spec.name] = list(value) if isinstance(value, tuple) else value
        return payload

    def config_hash(self) -> str:
        return _hash({"core_version": CORE_VERSION, "config": self.as_dict()})


DEFAULT_CONFIG = CoreConfig()

#: Procedência de CADA parâmetro. `engineering_choice` é escolha explícita
#: registrada ANTES de consultar qualquer outcome — não é otimização, não foi
#: validada e não está aprovada para produção.
PARAMETER_PROVENANCE: Dict[str, Dict[str, str]] = {
    "adx_trend_min": {"origin": "production_equivalent",
                      "source": "confluence_service: ADX>25 = tendência consistente"},
    "adx_range_max": {"origin": "production_equivalent",
                      "source": "confluence_service: ADX<20 = sem tendência"},
    "atr_stop_buffer": {"origin": "production_equivalent",
                        "source": "entry_planner.ATR_BUFFER = 0.3"},
    "entry_band_atr": {"origin": "production_equivalent",
                       "source": "entry_planner.ATR_ENTRY_BAND = 0.4"},
    "pullback_max_distance_pct": {"origin": "production_equivalent",
                                  "source": "entry_planner.EMA_PULLBACK_MAX_DIST = 0.04"},
    "min_rr_tp2": {"origin": "production_equivalent",
                   "source": "entry_planner.MIN_RR_TP2 = 1.8"},
    "min_tp_separation_atr": {"origin": "production_equivalent",
                              "source": "entry_planner.MIN_TP_SEP_ATR = 0.5"},
    "min_bars": {"origin": "engineering_choice",
                 "rationale": "amostra mínima para EMA 12/26 e swings estabilizarem"},
    "max_state_age_bars": {"origin": "engineering_choice",
                           "rationale": "estado mais velho que 2 barras não é ponto-no-tempo útil"},
    "pullback_lookback_bars": {"origin": "engineering_choice",
                               "rationale": "janela curta o bastante para o recuo ser o mesmo evento"},
    "structure_lookback_bars": {"origin": "engineering_choice",
                                "rationale": "estrutura recente que o stop precisa respeitar"},
    "breakout_lookback_bars": {"origin": "engineering_choice",
                               "rationale": "referência de máxima/mínima anterior ao rompimento"},
    "breakout_min_atr": {"origin": "engineering_choice",
                         "rationale": "fechamento além da referência por fração de ATR, não por 1 tick"},
    "breakout_volume_mult": {"origin": "engineering_choice",
                             "rationale": "rompimento pede volume acima da média da janela"},
    "require_retest": {"origin": "engineering_choice",
                       "rationale": "reteste opcional na config inicial; quando exigido, é evidência obrigatória"},
    "range_min_touches": {"origin": "engineering_choice",
                          "rationale": "duas bordas testadas duas vezes cada"},
    "range_min_width_atr": {"origin": "engineering_choice",
                            "rationale": "range estreito demais não paga custo"},
    "range_border_tolerance_atr": {"origin": "engineering_choice",
                                   "rationale": "toque na borda com tolerância de ruído"},
    "rejection_wick_fraction": {"origin": "engineering_choice",
                                "rationale": "metade da vela em pavio para a rejeição ser visível"},
    "range_target_fraction": {"origin": "engineering_choice",
                              "rationale": "alvo antes da borda oposta, compatível com o range"},
    "min_rr_tp1": {"origin": "engineering_choice",
                   "rationale": "piso do primeiro alvo; o piso do TP2 vem da produção"},
    "require_htf_alignment": {"origin": "engineering_choice",
                              "rationale": "continuação exige HTF provado; desconhecido não alinha"},
    "htf_conflict_min_strength": {"origin": "engineering_choice",
                                  "rationale": "força mínima para chamar o HTF de conflito forte"},
    "playbook_priority": {"origin": "engineering_choice",
                          "rationale": "ordem determinística de desempate entre playbooks do mesmo lado"},
}


def config_manifest(config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Manifest CONGELADO da configuração, emitido antes de qualquer outcome."""
    cfg = config or DEFAULT_CONFIG
    parameters = {}
    for name, value in cfg.as_dict().items():
        provenance = PARAMETER_PROVENANCE.get(
            name, {"origin": "engineering_choice",
                   "rationale": "parâmetro explícito sem equivalente em produção"})
        parameters[name] = {"value": value, **provenance}
    return {
        "core_version": CORE_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "config_hash": cfg.config_hash(),
        "playbooks": {name: PLAYBOOK_VERSIONS[name] for name in PLAYBOOKS},
        "parameters": parameters,
        "outcomes_consulted": False,
        "optimized": False,
        "approved_for_production": False,
        "limitations": list(LIMITATIONS),
    }


# ── Validação do estado ─────────────────────────────────────────────────────
def validate_state(state: MarketState, config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Verdito do estado. Vela aberta ou relógio incoerente NÃO decide."""
    cfg = config or DEFAULT_CONFIG
    reasons: List[str] = []
    bars = state.bars
    if len(bars) < cfg.min_bars:
        reasons.append(BARS_INSUFFICIENT)
    times = [bar.open_time_ms for bar in bars]
    if any(b <= a for a, b in zip(times, times[1:])):
        reasons.append(BARS_UNORDERED)
    elif any(b - a != state.bar_ms for a, b in zip(times, times[1:])):
        reasons.append(BARS_GAPPED)
    if any(not bar.closed for bar in bars):
        reasons.append(BAR_NOT_CLOSED)
    last_close_ms = times[-1] + state.bar_ms
    if state.as_of_ms < last_close_ms:
        # A última vela ainda estaria em formação no instante da decisão.
        reasons.append(DECISION_BEFORE_BAR_CLOSE)
    elif state.as_of_ms - last_close_ms > cfg.max_state_age_bars * state.bar_ms:
        reasons.append(STATE_STALE)
    for view in state.higher_timeframes:
        if view.as_of_ms > state.as_of_ms:
            reasons.append(HTF_EVIDENCE_UNPROVEN)
            break
    ok = not reasons
    return {"ok": ok, "reason_codes": tuple(dict.fromkeys(reasons)),
            "trigger_candle_ms": times[-1] if ok else None,
            "decision_ts_ms": state.as_of_ms}


# ── Regime ──────────────────────────────────────────────────────────────────
def trend_verdict(state: MarketState, config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Lado da tendência confirmada, ou None com motivo. ADX é FORÇA."""
    cfg = config or DEFAULT_CONFIG
    missing = [name for name in ("ema_fast", "ema_slow", "adx")
               if getattr(state, name) is None]
    if missing:
        return {"side": None, "reason_code": TREND_UNKNOWN, "missing": tuple(missing)}
    close = state.trigger_bar.close
    if state.adx < cfg.adx_trend_min:
        return {"side": None, "reason_code": TREND_NOT_CONFIRMED, "missing": ()}
    if state.ema_fast > state.ema_slow and close > state.ema_slow:
        side = SIDE_LONG
    elif state.ema_fast < state.ema_slow and close < state.ema_slow:
        side = SIDE_SHORT
    else:
        return {"side": None, "reason_code": TREND_NOT_CONFIRMED, "missing": ()}
    return {"side": side, "reason_code": OK, "missing": ()}


def range_verdict(state: MarketState, config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Range PROVADO. `regime_label` (inclusive NORMAL) não prova lateralidade,
    e ausência de evidência de tendência também não."""
    cfg = config or DEFAULT_CONFIG
    missing = [name for name in ("range_high", "range_low", "range_touches", "atr", "adx")
               if getattr(state, name) is None]
    if missing:
        return {"proven": False, "reason_code": RANGE_UNKNOWN, "missing": tuple(missing)}
    width = state.range_high - state.range_low
    if width <= 0:
        return {"proven": False, "reason_code": GEOMETRY_INVALID, "missing": ()}
    if state.range_touches < cfg.range_min_touches:
        return {"proven": False, "reason_code": RANGE_NOT_PROVEN, "missing": ()}
    if width < cfg.range_min_width_atr * state.atr:
        return {"proven": False, "reason_code": RANGE_NOT_PROVEN, "missing": ()}
    if state.adx > cfg.adx_range_max:
        return {"proven": False, "reason_code": RANGE_NOT_PROVEN, "missing": ()}
    return {"proven": True, "reason_code": OK, "missing": (), "width": width}


# ── Contratendência / HTF ───────────────────────────────────────────────────
def htf_verdict(state: MarketState, side: str, *, continuation: bool,
                config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Conflito HTF confirmado BLOQUEIA — não rebaixa. Vale também para
    `RANGE_REVERSION`: não existe exceção de reversão neste lote."""
    cfg = config or DEFAULT_CONFIG
    views = state.higher_timeframes
    if not views:
        return {"allowed": not continuation, "reason_code": HTF_UNKNOWN,
                "conflict": False, "aligned": False}
    aligned = False
    for view in views:
        if not view.confirmed or view.direction is None:
            continue
        if view.direction == side:
            aligned = True
            continue
        # Oposto e confirmado: força desconhecida é tratada como conflito
        # (fail-closed), nunca como zero.
        if view.strength is None or view.strength >= cfg.htf_conflict_min_strength:
            return {"allowed": False, "reason_code": HTF_CONFLICT_BLOCKED,
                    "conflict": True, "aligned": False,
                    "timeframe": view.timeframe}
    if continuation and cfg.require_htf_alignment and not aligned:
        return {"allowed": False, "reason_code": HTF_UNKNOWN,
                "conflict": False, "aligned": False}
    return {"allowed": True, "reason_code": OK, "conflict": False, "aligned": aligned}


# ── Geometria ───────────────────────────────────────────────────────────────
def structural_stop(*, side: str, entry: float, atr: float,
                    levels: Sequence[float], buffer_atr: float) -> Dict[str, Any]:
    """Stop na estrutura mais próxima ALÉM da entrada, com folga de ATR.

    Não existe alargamento para fabricar R:R: o stop sai da estrutura e o R:R
    é consequência.
    """
    sign = _sign(side)
    beyond = [float(level) for level in levels if sign * (entry - level) > 0]
    if not beyond:
        return {"price": None, "reason_code": STOP_STRUCTURE_UNAVAILABLE, "level": None}
    reference = max(beyond) if side == SIDE_LONG else min(beyond)
    price = reference - sign * buffer_atr * atr
    if sign * (entry - price) <= 0 or price <= 0:
        return {"price": None, "reason_code": GEOMETRY_INVALID, "level": reference}
    return {"price": price, "reason_code": OK, "level": reference}


def structural_targets(*, side: str, entry: float, atr: float,
                       levels: Sequence[float], min_separation_atr: float) -> Dict[str, Any]:
    """Dois alvos de ESTRUTURA na direção do trade, com separação mínima.

    Nenhum alvo é inventado: sem estrutura suficiente, o setup fica inelegível.
    """
    sign = _sign(side)
    separation = min_separation_atr * atr
    ordered = sorted({float(level) for level in levels}, reverse=side == SIDE_SHORT)
    ahead = [level for level in ordered if sign * (level - entry) >= separation]
    if not ahead:
        return {"tp1": None, "tp2": None, "reason_code": TARGET_STRUCTURE_UNAVAILABLE}
    tp1 = ahead[0]
    for level in ahead[1:]:
        if sign * (level - tp1) >= separation:
            return {"tp1": tp1, "tp2": level, "reason_code": OK}
    return {"tp1": tp1, "tp2": None, "reason_code": TP_SEPARATION_BELOW_FLOOR}


def rr_verdict(*, side: str, entry: float, stop: float, tp1: float, tp2: float,
               config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """R:R é CONSEQUÊNCIA da geometria — abaixo do piso reprova o setup."""
    cfg = config or DEFAULT_CONFIG
    sign = _sign(side)
    risk = sign * (entry - stop)
    if risk <= 0:
        return {"ok": False, "reason_code": GEOMETRY_INVALID, "rr1": None, "rr2": None}
    rr1 = sign * (tp1 - entry) / risk
    rr2 = sign * (tp2 - entry) / risk
    if rr1 < cfg.min_rr_tp1 or rr2 < cfg.min_rr_tp2:
        return {"ok": False, "reason_code": RR_BELOW_FLOOR, "rr1": rr1, "rr2": rr2,
                "risk": risk}
    return {"ok": True, "reason_code": OK, "rr1": rr1, "rr2": rr2, "risk": risk}


def _decision(playbook: str, state_value: str, reasons: Sequence[str], *,
              side: Optional[str] = None, missing: Sequence[str] = (),
              levels: Optional[Dict[str, float]] = None,
              evidence: Optional[Dict[str, Any]] = None,
              invalidation: Optional[str] = None) -> Dict[str, Any]:
    codes = tuple(dict.fromkeys(reasons))
    unknown = set(codes) - REASON_CODES
    if unknown:
        raise AssertionError(f"reason_code fora do vocabulário: {sorted(unknown)}")
    return {
        "playbook": playbook,
        "playbook_version": PLAYBOOK_VERSIONS[playbook],
        "state": state_value,
        "side": side,
        "reason_codes": codes,
        "missing_features": tuple(missing),
        "levels": dict(levels) if levels else None,
        "invalidation": invalidation,
        "evidence": dict(evidence) if evidence else {},
    }


# ── Playbooks ───────────────────────────────────────────────────────────────
def evaluate_trend_pullback(state: MarketState,
                            config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Tendência confirmada, recuo à estrutura e retomada em vela FECHADA.

    O oscilador é filtro: RSI jamais inverte o lado da tendência.
    """
    cfg = config or DEFAULT_CONFIG
    book = PLAYBOOK_TREND_PULLBACK
    trend = trend_verdict(state, cfg)
    if trend["side"] is None:
        unknown = trend["reason_code"] == TREND_UNKNOWN
        return _decision(book, STATE_UNKNOWN if unknown else STATE_INELIGIBLE,
                         [trend["reason_code"]], missing=trend["missing"])
    side = trend["side"]
    sign = _sign(side)
    if state.atr is None:
        return _decision(book, STATE_UNKNOWN, [FEATURE_MISSING], side=side, missing=("atr",))
    if len(state.bars) < 2:
        return _decision(book, STATE_UNKNOWN, [BARS_INSUFFICIENT], side=side)
    atr, ema_fast = state.atr, state.ema_fast
    trigger, prior = state.bars[-1], state.bars[-2]
    window = state.bars[-(cfg.pullback_lookback_bars + 1):-1]
    band = cfg.entry_band_atr * atr
    touched = any(sign * ((bar.low if side == SIDE_LONG else bar.high) - ema_fast) <= band
                  for bar in window)
    distance_pct = abs(trigger.close - ema_fast) / trigger.close
    if not touched or distance_pct > cfg.pullback_max_distance_pct:
        return _decision(book, STATE_INELIGIBLE, [PULLBACK_NOT_AT_STRUCTURE], side=side,
                         evidence={"distance_pct": distance_pct, "touched": touched})
    reference = prior.high if side == SIDE_LONG else prior.low
    retaken = sign * (trigger.close - reference) > 0 and sign * (trigger.close - ema_fast) > 0
    if not retaken:
        return _decision(book, STATE_INELIGIBLE, [TRIGGER_ABSENT], side=side,
                         evidence={"trigger_close": trigger.close, "reference": reference})
    htf = htf_verdict(state, side, continuation=True, config=cfg)
    if not htf["allowed"]:
        return _decision(book, STATE_INELIGIBLE if htf["conflict"] else STATE_UNKNOWN,
                         [htf["reason_code"]], side=side)
    entry = trigger.close
    structure = [bar.low if side == SIDE_LONG else bar.high
                 for bar in state.bars[-cfg.structure_lookback_bars:]]
    if side == SIDE_LONG and state.swing_low is not None:
        structure.append(state.swing_low)
    if side == SIDE_SHORT and state.swing_high is not None:
        structure.append(state.swing_high)
    stop = structural_stop(side=side, entry=entry, atr=atr, levels=structure,
                           buffer_atr=cfg.atr_stop_buffer)
    if stop["price"] is None:
        return _decision(book, STATE_INELIGIBLE, [stop["reason_code"]], side=side)
    targets = structural_targets(side=side, entry=entry, atr=atr,
                                 levels=state.target_levels,
                                 min_separation_atr=cfg.min_tp_separation_atr)
    if targets["tp2"] is None:
        return _decision(book, STATE_INELIGIBLE, [targets["reason_code"]], side=side)
    rr = rr_verdict(side=side, entry=entry, stop=stop["price"], tp1=targets["tp1"],
                    tp2=targets["tp2"], config=cfg)
    if not rr["ok"]:
        return _decision(book, STATE_INELIGIBLE, [rr["reason_code"]], side=side,
                         levels={"entry": entry, "stop_loss": stop["price"],
                                 "tp1": targets["tp1"], "tp2": targets["tp2"]},
                         evidence={"rr1": rr["rr1"], "rr2": rr["rr2"]})
    return _decision(book, STATE_ELIGIBLE, [OK], side=side,
                     levels={"entry": entry, "stop_loss": stop["price"],
                             "tp1": targets["tp1"], "tp2": targets["tp2"]},
                     invalidation=f"fechamento além de {stop['level']:.10g} invalida o recuo",
                     evidence={"adx": state.adx, "rsi": state.rsi,
                               "oscillator_role": "filter_only",
                               "distance_pct": distance_pct,
                               "rr1": rr["rr1"], "rr2": rr["rr2"],
                               "htf_aligned": htf["aligned"]})


def evaluate_trend_breakout(state: MarketState,
                            config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Rompimento CONFIRMADO em vela fechada, com volume e (quando exigido)
    reteste. Nome de padrão, sozinho, não prova rompimento."""
    cfg = config or DEFAULT_CONFIG
    book = PLAYBOOK_TREND_BREAKOUT
    trend = trend_verdict(state, cfg)
    if trend["side"] is None:
        unknown = trend["reason_code"] == TREND_UNKNOWN
        return _decision(book, STATE_UNKNOWN if unknown else STATE_INELIGIBLE,
                         [trend["reason_code"]], missing=trend["missing"])
    side = trend["side"]
    sign = _sign(side)
    if state.atr is None:
        return _decision(book, STATE_UNKNOWN, [FEATURE_MISSING], side=side, missing=("atr",))
    atr = state.atr
    trigger = state.bars[-1]
    window = state.bars[-(cfg.breakout_lookback_bars + 1):-1]
    if not window:
        return _decision(book, STATE_UNKNOWN, [BARS_INSUFFICIENT], side=side)
    reference = (max(bar.high for bar in window) if side == SIDE_LONG
                 else min(bar.low for bar in window))
    if sign * (trigger.close - reference) < cfg.breakout_min_atr * atr:
        return _decision(book, STATE_INELIGIBLE, [BREAKOUT_NOT_CONFIRMED], side=side,
                         evidence={"reference": reference, "close": trigger.close,
                                   "patterns_are_not_evidence": list(state.patterns)})
    if trigger.volume is None or state.volume_ma is None:
        missing = [name for name, value in (("bar_volume", trigger.volume),
                                            ("volume_ma", state.volume_ma))
                   if value is None]
        return _decision(book, STATE_UNKNOWN, [VOLUME_UNKNOWN], side=side, missing=missing)
    if trigger.volume < cfg.breakout_volume_mult * state.volume_ma:
        return _decision(book, STATE_INELIGIBLE, [VOLUME_INSUFFICIENT], side=side,
                         evidence={"volume": trigger.volume, "volume_ma": state.volume_ma})
    if cfg.require_retest:
        if state.retest_confirmed is None:
            return _decision(book, STATE_UNKNOWN, [RETEST_UNKNOWN], side=side,
                             missing=("retest_confirmed",))
        if not state.retest_confirmed:
            return _decision(book, STATE_INELIGIBLE, [RETEST_MISSING], side=side)
    htf = htf_verdict(state, side, continuation=True, config=cfg)
    if not htf["allowed"]:
        return _decision(book, STATE_INELIGIBLE if htf["conflict"] else STATE_UNKNOWN,
                         [htf["reason_code"]], side=side)
    entry = trigger.close
    stop = structural_stop(side=side, entry=entry, atr=atr, levels=[reference],
                           buffer_atr=cfg.atr_stop_buffer)
    if stop["price"] is None:
        return _decision(book, STATE_INELIGIBLE, [stop["reason_code"]], side=side)
    targets = structural_targets(side=side, entry=entry, atr=atr,
                                 levels=state.target_levels,
                                 min_separation_atr=cfg.min_tp_separation_atr)
    if targets["tp2"] is None:
        return _decision(book, STATE_INELIGIBLE, [targets["reason_code"]], side=side)
    rr = rr_verdict(side=side, entry=entry, stop=stop["price"], tp1=targets["tp1"],
                    tp2=targets["tp2"], config=cfg)
    if not rr["ok"]:
        return _decision(book, STATE_INELIGIBLE, [rr["reason_code"]], side=side,
                         levels={"entry": entry, "stop_loss": stop["price"],
                                 "tp1": targets["tp1"], "tp2": targets["tp2"]},
                         evidence={"rr1": rr["rr1"], "rr2": rr["rr2"]})
    return _decision(book, STATE_ELIGIBLE, [OK], side=side,
                     levels={"entry": entry, "stop_loss": stop["price"],
                             "tp1": targets["tp1"], "tp2": targets["tp2"]},
                     invalidation=f"retorno para dentro de {reference:.10g} invalida o rompimento",
                     evidence={"reference": reference, "volume": trigger.volume,
                               "volume_ma": state.volume_ma,
                               "retest_required": cfg.require_retest,
                               "retest_confirmed": state.retest_confirmed,
                               "rr1": rr["rr1"], "rr2": rr["rr2"],
                               "htf_aligned": htf["aligned"]})


def evaluate_range_reversion(state: MarketState,
                             config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Range provado, borda disponível e rejeição confirmada em vela fechada."""
    cfg = config or DEFAULT_CONFIG
    book = PLAYBOOK_RANGE_REVERSION
    verdict = range_verdict(state, cfg)
    if not verdict["proven"]:
        unknown = verdict["reason_code"] == RANGE_UNKNOWN
        return _decision(book, STATE_UNKNOWN if unknown else STATE_INELIGIBLE,
                         [verdict["reason_code"]], missing=verdict["missing"])
    atr = state.atr
    trigger = state.bars[-1]
    tolerance = cfg.range_border_tolerance_atr * atr
    at_low = trigger.low <= state.range_low + tolerance
    at_high = trigger.high >= state.range_high - tolerance
    if at_low == at_high:
        # Nenhuma borda tocada, ou a vela cobriu as duas: sem borda utilizável.
        return _decision(book, STATE_INELIGIBLE, [BORDER_UNAVAILABLE],
                         evidence={"at_low": at_low, "at_high": at_high})
    side = SIDE_LONG if at_low else SIDE_SHORT
    sign = _sign(side)
    bar_range = trigger.bar_range
    if bar_range <= 0:
        return _decision(book, STATE_INELIGIBLE, [REJECTION_NOT_CONFIRMED], side=side)
    wick = (trigger.close - trigger.low) if side == SIDE_LONG else (trigger.high - trigger.close)
    inside = (trigger.close > state.range_low) if side == SIDE_LONG else (trigger.close < state.range_high)
    if not inside or wick / bar_range < cfg.rejection_wick_fraction:
        return _decision(book, STATE_INELIGIBLE, [REJECTION_NOT_CONFIRMED], side=side,
                         evidence={"wick_fraction": wick / bar_range, "closed_inside": inside})
    htf = htf_verdict(state, side, continuation=False, config=cfg)
    if not htf["allowed"]:
        # Reversão contra HTF forte NÃO recebe exceção por ser range.
        return _decision(book, STATE_INELIGIBLE, [htf["reason_code"]], side=side)
    entry = trigger.close
    border = state.range_low if side == SIDE_LONG else state.range_high
    opposite = state.range_high if side == SIDE_LONG else state.range_low
    stop = structural_stop(side=side, entry=entry, atr=atr, levels=[border],
                           buffer_atr=cfg.atr_stop_buffer)
    if stop["price"] is None:
        return _decision(book, STATE_INELIGIBLE, [stop["reason_code"]], side=side)
    span = opposite - entry
    if sign * span <= 0:
        return _decision(book, STATE_INELIGIBLE, [GEOMETRY_INVALID], side=side)
    tp1 = entry + span * 0.5
    tp2 = entry + span * cfg.range_target_fraction
    if sign * (tp2 - tp1) < cfg.min_tp_separation_atr * atr:
        return _decision(book, STATE_INELIGIBLE, [TP_SEPARATION_BELOW_FLOOR], side=side,
                         levels={"entry": entry, "stop_loss": stop["price"],
                                 "tp1": tp1, "tp2": tp2})
    rr = rr_verdict(side=side, entry=entry, stop=stop["price"], tp1=tp1, tp2=tp2, config=cfg)
    if not rr["ok"]:
        return _decision(book, STATE_INELIGIBLE, [rr["reason_code"]], side=side,
                         levels={"entry": entry, "stop_loss": stop["price"],
                                 "tp1": tp1, "tp2": tp2},
                         evidence={"rr1": rr["rr1"], "rr2": rr["rr2"]})
    return _decision(book, STATE_ELIGIBLE, [OK], side=side,
                     levels={"entry": entry, "stop_loss": stop["price"],
                             "tp1": tp1, "tp2": tp2},
                     invalidation=f"fechamento fora de {border:.10g} invalida o range",
                     evidence={"range_high": state.range_high, "range_low": state.range_low,
                               "touches": state.range_touches, "adx": state.adx,
                               "wick_fraction": wick / bar_range,
                               "rr1": rr["rr1"], "rr2": rr["rr2"]})


EVALUATORS = {
    PLAYBOOK_TREND_PULLBACK: evaluate_trend_pullback,
    PLAYBOOK_TREND_BREAKOUT: evaluate_trend_breakout,
    PLAYBOOK_RANGE_REVERSION: evaluate_range_reversion,
}


# ── Arbitragem e identidade da oportunidade ─────────────────────────────────
def opportunity_key(*, symbol: str, timeframe: str, side: str,
                    trigger_candle_ms: int) -> str:
    """Identidade da OPORTUNIDADE — a mesma vela de gatilho gera uma única
    entrada econômica, qualquer que seja o snapshot que a observou (P03)."""
    if side not in SIDES:
        raise ValueError("side deve ser long ou short")
    _integer(trigger_candle_ms, "trigger_candle_ms", minimum=1)
    return _hash({"symbol": symbol.strip().upper(), "timeframe": timeframe.strip(),
                  "side": side, "trigger_candle_ms": trigger_candle_ms,
                  "core_version": CORE_VERSION})[:32]


def arbitrate(decisions: Sequence[Dict[str, Any]],
              config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Arbitragem DETERMINÍSTICA: sem soma de votos incompatíveis.

    Lados opostos elegíveis ao mesmo tempo bloqueiam a oportunidade; mesmo lado
    resolve pela ordem de prioridade declarada na config.
    """
    cfg = config or DEFAULT_CONFIG
    eligible = [item for item in decisions if item["state"] == STATE_ELIGIBLE]
    sides = {item["side"] for item in eligible}
    if len(sides) > 1:
        return {"selected": None, "reason_code": ARBITRATION_SIDE_CONFLICT,
                "conflicting_sides": tuple(sorted(sides))}
    if not eligible:
        return {"selected": None, "reason_code": NO_ELIGIBLE_PLAYBOOK,
                "conflicting_sides": ()}
    order = {name: index for index, name in enumerate(cfg.playbook_priority)}
    winner = sorted(eligible, key=lambda item: (order[item["playbook"]], item["playbook"]))[0]
    return {"selected": winner, "reason_code": OK, "conflicting_sides": ()}


def decide(state: MarketState, config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    """Decisão completa do núcleo: validação → playbooks → arbitragem.

    Saída sempre com decisão, motivos, features ausentes, playbook/versão e
    trace — inclusive quando nada é elegível.
    """
    cfg = config or DEFAULT_CONFIG
    base = {
        "core_version": CORE_VERSION,
        "contract": CONTRACT,
        "config_hash": cfg.config_hash(),
        "symbol": state.symbol,
        "timeframe": state.timeframe,
        "decision_ts_ms": state.as_of_ms,
        "mode": selected_mode(),
        "executable": False,
    }
    validation = validate_state(state, cfg)
    if not validation["ok"]:
        return {**base, "state": STATE_UNKNOWN, "side": None, "playbook": None,
                "playbook_version": None, "reason_codes": validation["reason_codes"],
                "missing_features": (), "levels": None, "invalidation": None,
                "opportunity_key": None, "trigger_candle_ms": None,
                "considered": (), "trace": {"validation": validation}}
    considered = tuple(EVALUATORS[name](state, cfg) for name in PLAYBOOKS)
    verdict = arbitrate(considered, cfg)
    trigger_ms = validation["trigger_candle_ms"]
    winner = verdict["selected"]
    if winner is None:
        unknown = (verdict["reason_code"] == NO_ELIGIBLE_PLAYBOOK
                   and any(item["state"] == STATE_UNKNOWN for item in considered))
        return {**base,
                "state": STATE_UNKNOWN if unknown else STATE_INELIGIBLE,
                "side": None, "playbook": None, "playbook_version": None,
                "reason_codes": (verdict["reason_code"],),
                "missing_features": tuple(dict.fromkeys(
                    name for item in considered for name in item["missing_features"])),
                "levels": None, "invalidation": None, "opportunity_key": None,
                "trigger_candle_ms": trigger_ms, "considered": considered,
                "trace": {"validation": validation, "arbitration": verdict}}
    return {**base, "state": STATE_ELIGIBLE, "side": winner["side"],
            "playbook": winner["playbook"], "playbook_version": winner["playbook_version"],
            "reason_codes": winner["reason_codes"], "missing_features": (),
            "levels": winner["levels"], "invalidation": winner["invalidation"],
            "opportunity_key": opportunity_key(symbol=state.symbol, timeframe=state.timeframe,
                                               side=winner["side"], trigger_candle_ms=trigger_ms),
            "trigger_candle_ms": trigger_ms, "considered": considered,
            "trace": {"validation": validation, "arbitration": verdict,
                      "evidence": winner["evidence"]}}


# ── Adaptador para a FUTURA seleção operacional ─────────────────────────────
def selection_adapter(decision: Dict[str, Any], *, mode: Optional[str] = None) -> Dict[str, Any]:
    """Ponte para a seleção operacional futura — sempre NÃO executável.

    Em `simulation` devolve o candidato para o replay/simulador. Em qualquer
    outro modo devolve recusa. Nunca emite `client_order_id`, quantidade,
    alavancagem ou qualquer campo que o executor real aceite.
    """
    active = (selected_mode() if mode is None else
              (MODE_SIMULATION if str(mode).strip().lower() == MODE_SIMULATION else MODE_INACTIVE))
    envelope = {"core_version": CORE_VERSION, "contract": CONTRACT,
                "executable": False, "live_route": "UNAVAILABLE", "mode": active}
    if active != MODE_SIMULATION:
        return {**envelope, "route": "NONE", "reason_code": CORE_INACTIVE, "candidate": None}
    if not isinstance(decision, dict) or decision.get("state") != STATE_ELIGIBLE:
        return {**envelope, "route": "SIMULATION", "reason_code": NO_ELIGIBLE_PLAYBOOK,
                "candidate": None}
    levels = decision.get("levels") or {}
    candidate = {
        "opportunity_key": decision.get("opportunity_key"),
        "symbol": decision.get("symbol"),
        "timeframe": decision.get("timeframe"),
        "side": decision.get("side"),
        "playbook": decision.get("playbook"),
        "playbook_version": decision.get("playbook_version"),
        "trigger_candle_ms": decision.get("trigger_candle_ms"),
        "decision_ts_ms": decision.get("decision_ts_ms"),
        "entry": levels.get("entry"),
        "stop_loss": levels.get("stop_loss"),
        "tp1": levels.get("tp1"),
        "tp2": levels.get("tp2"),
        "config_hash": decision.get("config_hash"),
    }
    return {**envelope, "route": "SIMULATION", "reason_code": LIVE_ROUTE_UNAVAILABLE,
            "candidate": candidate}


def core_manifest(config: Optional[CoreConfig] = None) -> Dict[str, Any]:
    manifest = config_manifest(config)
    manifest["reason_codes"] = sorted(REASON_CODES)
    manifest["states"] = list(DECISION_STATES)
    manifest["executable"] = False
    return manifest
