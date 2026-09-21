"""R11C — política robusta VERSIONADA, avaliável só em simulação.

Núcleo PURO: sem I/O, sem relógio implícito (o instante é injetado), sem ENV
dentro da matemática. O default operacional continua na política anterior; este
módulo não aplica universo, relearn, cache aprendido nem sizing na operação.

Trata os achados R11A que faltavam: A2/M5 (histerese com período E evidência
nova), A3/M4/M7 (janelas disjuntas, direção inválida, cache vazio × erro),
M1 (uma referência temporal causal), M3 (geração de aprendizado atômica),
M6 (liquidez indisponível não promove) e M8/B3 (identidade/dedupe e populações
separadas). Rótulos B1/B2 corrigidos nos veredictos.

`1.0` significa CAMADA NÃO APLICADA — não é redução garantida de risco: remover
um multiplicador antigo de 0,75 pode AUMENTAR o tamanho. Por isso a política
nova nunca é ativada automaticamente.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

POLICY_VERSION = "R11C_ROBUST_V1"
POLICY_SELECTOR_ENV = "R11_POLICY_VERSION"
POLICY_LEGACY = "legacy"
#: Populações separadas por mecanismo: nunca misturar dinheiro real com papel.
POPULATION_REAL = "REAL"
POPULATION_SHADOW = "SHADOW"
POPULATION_BACKTEST = "BACKTEST"
POPULATIONS = (POPULATION_REAL, POPULATION_SHADOW, POPULATION_BACKTEST)
#: Rótulos (B1/B2): neutro COM amostra não é "dormente"; ausência não é zero.
LABEL_NEUTRAL = "NEUTRAL_WITH_SAMPLE"
LABEL_DORMANT = "DORMANT_NO_SAMPLE"
LABEL_NO_EVIDENCE = "NO_EVIDENCE"
VERDICT_REDUCE = "REDUCE_EXPOSURE"
VERDICT_HOLD = "HOLD"
VERDICT_PROMOTE = "PROMOTE_CANDIDATE"
VERDICT_INELIGIBLE = "INELIGIBLE"


def selected_policy() -> str:
    """Política vigente. Qualquer valor desconhecido ⇒ legado (nova inativa)."""
    value = (os.getenv(POLICY_SELECTOR_ENV, POLICY_LEGACY) or "").strip()
    return POLICY_VERSION if value == POLICY_VERSION else POLICY_LEGACY


def robust_policy_enabled() -> bool:
    return selected_policy() == POLICY_VERSION


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode()).hexdigest()[:32]


# ── M8/B3: identidade da amostra ───────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """Uma OPORTUNIDADE observada. Tentativas repetidas do mesmo setup não são
    trades independentes: a identidade é (símbolo, TF, lado, vela do gatilho)."""

    symbol: str
    timeframe: str
    direction: str
    trigger_ms: int
    outcome_r: Optional[float]
    resolved_at_ms: int
    population: str = POPULATION_SHADOW

    @property
    def identity(self) -> str:
        return _digest([self.symbol, self.timeframe, self.direction, self.trigger_ms])


@dataclass(frozen=True)
class Sample:
    population: str
    observations: Tuple[Observation, ...] = ()
    duplicates_dropped: int = 0
    invalid_dropped: int = 0

    @property
    def n(self) -> int:
        return len(self.observations)

    @property
    def net_r(self) -> float:
        return sum(float(o.outcome_r) for o in self.observations)

    @property
    def mean_r(self) -> Optional[float]:
        return self.net_r / self.n if self.n else None

    @property
    def evidence_key(self) -> str:
        """Muda quando entra evidência NOVA; repetição não muda."""
        return _digest(sorted(o.identity for o in self.observations))

    @property
    def label(self) -> str:
        if self.n == 0:
            return LABEL_NO_EVIDENCE
        return LABEL_NEUTRAL


def build_sample(observations: Iterable[Any], *, population: str,
                 direction: Optional[str] = None) -> Sample:
    """Dedupe por identidade e separação de população. Direção inválida NÃO
    entra e não completa amostra (M4). Resultado não finito é descartado."""
    seen: set = set()
    kept, duplicates, invalid = [], 0, 0
    for item in observations or ():
        if not isinstance(item, Observation):
            invalid += 1
            continue
        if item.population != population:
            invalid += 1
            continue
        if item.direction not in ("long", "short"):
            invalid += 1
            continue
        if direction is not None and item.direction != direction:
            invalid += 1
            continue
        if _finite(item.outcome_r) is None:
            invalid += 1
            continue
        if item.identity in seen:
            duplicates += 1
            continue
        seen.add(item.identity)
        kept.append(item)
    kept.sort(key=lambda o: (o.resolved_at_ms, o.identity))
    return Sample(population=population, observations=tuple(kept),
                  duplicates_dropped=duplicates, invalid_dropped=invalid)


# ── M1: uma referência temporal causal ─────────────────────────────────────
TIME_REFERENCE = "TRIGGER_CANDLE_CLOSE_UTC"


def session_of(trigger_ms: int) -> Optional[str]:
    """Sessão pela MESMA referência usada para aprender e para aplicar."""
    if isinstance(trigger_ms, bool) or not isinstance(trigger_ms, int) or trigger_ms <= 0:
        return None
    hour = datetime.fromtimestamp(trigger_ms / 1000, tz=timezone.utc).hour
    if hour < 7:
        return "Asia"
    if hour < 14:
        return "Europe"
    if hour < 21:
        return "NY"
    return "Off-hours"


def time_reference_coverage(observations: Sequence[Observation]) -> Dict[str, Any]:
    """Cobertura da referência nova: histórico antigo NÃO é reinterpretado."""
    total = len(observations or ())
    covered = sum(1 for o in observations or () if session_of(o.trigger_ms) is not None)
    return {"reference": TIME_REFERENCE, "version": POLICY_VERSION,
            "observations": total, "covered": covered,
            "coverage_pct": round(100.0 * covered / total, 1) if total else None,
            "legacy_reinterpreted": False}


# ── A3/M4/M7: decay com janelas disjuntas e cache explícito ────────────────
@dataclass(frozen=True)
class DecayConfig:
    baseline_days: int = 60
    recent_days: int = 14
    min_recent: int = 8
    min_baseline: int = 20
    baseline_edge_min: float = 0.10
    r_floor: float = 0.0
    r_full: float = -0.30
    mult_min: float = 0.5
    ttl_seconds: int = 1800


def split_windows(sample: Sample, *, now_ms: int, config: DecayConfig) -> Tuple[Sample, Sample]:
    """Baseline ANTERIOR e janela recente DISJUNTOS (A3): o prejuízo recente
    não contamina a própria referência."""
    recent_start = now_ms - config.recent_days * 86_400_000
    baseline_start = now_ms - config.baseline_days * 86_400_000
    recent = tuple(o for o in sample.observations if o.resolved_at_ms >= recent_start)
    baseline = tuple(o for o in sample.observations
                     if baseline_start <= o.resolved_at_ms < recent_start)
    return (Sample(sample.population, baseline), Sample(sample.population, recent))


def decay_multiplier(sample: Sample, *, now_ms: int, config: DecayConfig = DecayConfig()):
    """Multiplicador ≤ 1.0. Só REDUZ exposição; nunca aumenta."""
    baseline, recent = split_windows(sample, now_ms=now_ms, config=config)
    if recent.n < config.min_recent:
        return 1.0, {"applied": False, "reason_code": "RECENT_SAMPLE_INSUFFICIENT",
                     "recent_n": recent.n, "baseline_n": baseline.n}
    if baseline.n < config.min_baseline:
        return 1.0, {"applied": False, "reason_code": "BASELINE_SAMPLE_INSUFFICIENT",
                     "recent_n": recent.n, "baseline_n": baseline.n}
    baseline_mean, recent_mean = baseline.mean_r, recent.mean_r
    if baseline_mean is None or recent_mean is None:
        return 1.0, {"applied": False, "reason_code": "MEAN_UNAVAILABLE"}
    if baseline_mean < config.baseline_edge_min:
        return 1.0, {"applied": False, "reason_code": "BASELINE_WITHOUT_EDGE",
                     "baseline_mean_r": baseline_mean, "recent_mean_r": recent_mean}
    if recent_mean >= config.r_floor:
        return 1.0, {"applied": False, "reason_code": "RECENT_OK",
                     "baseline_mean_r": baseline_mean, "recent_mean_r": recent_mean}
    span = max(1e-9, config.r_floor - config.r_full)
    fraction = min(1.0, max(0.0, (config.r_floor - recent_mean) / span))
    multiplier = round(1.0 - fraction * (1.0 - config.mult_min), 4)
    multiplier = min(1.0, max(config.mult_min, multiplier))
    return multiplier, {"applied": True, "reason_code": "EDGE_DECAYED",
                        "baseline_mean_r": baseline_mean, "recent_mean_r": recent_mean,
                        "recent_n": recent.n, "baseline_n": baseline.n}


def cache_verdict(*, cached_at_ms: Optional[int], now_ms: int, config: DecayConfig,
                  last_error: bool = False, empty: bool = False) -> str:
    """Cache VAZIO válido respeita o TTL (M7); erro continua diferente de vazio."""
    if last_error:
        return "ERROR_KEEP_PREVIOUS"
    if cached_at_ms is None:
        return "REFRESH_REQUIRED"
    fresh = (now_ms - cached_at_ms) < config.ttl_seconds * 1000
    if fresh:
        return "FRESH_EMPTY" if empty else "FRESH"
    return "REFRESH_REQUIRED"


# ── A2/M5: histerese por período E evidência nova ──────────────────────────
@dataclass(frozen=True)
class HysteresisProgress:
    symbol: str
    action: str
    periods: int = 0
    last_period: Optional[str] = None
    last_evidence: Optional[str] = None
    universe_source: Optional[str] = None


def period_key(now_ms: int, *, period_seconds: int) -> str:
    """Período elegível: janelas fixas do relógio injetado, não "chamadas"."""
    if period_seconds <= 0:
        raise ValueError("period_seconds deve ser positivo")
    return f"P{now_ms // (period_seconds * 1000)}"


def advance_hysteresis(progress: Optional[HysteresisProgress], *, symbol: str, action: str,
                       now_ms: int, period_seconds: int, evidence_key: str,
                       required_periods: int, universe_source: str,
                       contrary_evidence: bool = False) -> Tuple[HysteresisProgress, Dict[str, Any]]:
    """Avança SOMENTE com período elegível E evidência nova.

    Repetir a chamada, rodar preview, reiniciar o processo ou ter dois workers
    no mesmo período não acelera nada. Evidência contrária reinicia a sequência.
    """
    key = period_key(now_ms, period_seconds=period_seconds)
    current = progress if isinstance(progress, HysteresisProgress) else None
    if contrary_evidence:
        return (HysteresisProgress(symbol, action, 0, key, evidence_key, universe_source),
                {"ready": False, "reason_code": "CONTRARY_EVIDENCE_RESET", "periods": 0})
    if current is None or current.action != action or current.symbol != symbol:
        return (HysteresisProgress(symbol, action, 1, key, evidence_key, universe_source),
                {"ready": required_periods <= 1, "reason_code": "SEQUENCE_STARTED", "periods": 1})
    if current.universe_source not in (None, universe_source):
        return (HysteresisProgress(symbol, action, 1, key, evidence_key, universe_source),
                {"ready": required_periods <= 1, "reason_code": "UNIVERSE_SOURCE_CHANGED",
                 "periods": 1})
    if current.last_period == key:
        return (current, {"ready": False, "reason_code": "SAME_PERIOD", "periods": current.periods})
    if current.last_evidence == evidence_key:
        return (replace(current, last_period=key),
                {"ready": False, "reason_code": "NO_NEW_EVIDENCE", "periods": current.periods})
    periods = current.periods + 1
    advanced = HysteresisProgress(symbol, action, periods, key, evidence_key, universe_source)
    return advanced, {"ready": periods >= required_periods,
                      "reason_code": "PERIOD_WITH_NEW_EVIDENCE", "periods": periods}


# ── M3: geração de aprendizado publicada atomicamente ──────────────────────
def generation_eligible(row_generation: Any, current_generation: Any) -> Tuple[bool, str]:
    """Linha de geração antiga/incompatível não é elegível (sem auto-aplicar
    o melhor TF em outro timeframe na política nova)."""
    if not isinstance(current_generation, str) or not current_generation:
        return False, "GENERATION_UNKNOWN"
    if not isinstance(row_generation, str) or not row_generation:
        return False, "ROW_WITHOUT_GENERATION"
    if row_generation != current_generation:
        return False, "GENERATION_STALE"
    return True, "GENERATION_CURRENT"


def learned_multiplier(rows: Dict[str, Any], *, timeframe: str, current_generation: str,
                       min_confidence: float, clamp: Tuple[float, float]) -> Tuple[float, Dict[str, Any]]:
    """Só o TF EXATO e da geração vigente aplica; caso contrário `1.0`.

    `1.0` = camada não aplicada. Isso pode AUMENTAR o tamanho em relação a uma
    política antiga que reduzia — está explícito no diff de política.
    """
    row = (rows or {}).get(timeframe)
    if not isinstance(row, dict):
        return 1.0, {"applied": False, "reason_code": "NO_ROW_FOR_TIMEFRAME"}
    eligible, reason = generation_eligible(row.get("generation"), current_generation)
    if not eligible:
        return 1.0, {"applied": False, "reason_code": reason}
    confidence = _finite(row.get("confidence"))
    multiplier = _finite(row.get("size_quality_mult"))
    if confidence is None or not 0.0 <= confidence <= 1.0 or multiplier is None or multiplier <= 0:
        return 1.0, {"applied": False, "reason_code": "ROW_NUMERICALLY_INVALID"}
    if confidence < min_confidence:
        return 1.0, {"applied": False, "reason_code": "CONFIDENCE_BELOW_MINIMUM"}
    low, high = clamp
    return round(min(high, max(low, multiplier)), 4), {"applied": True,
                                                       "reason_code": "APPLIED_EXACT_TIMEFRAME"}


# ── M6: liquidez indisponível não promove ──────────────────────────────────
def liquidity_verdict(base: str, *, liquidity_universe: Optional[Sequence[str]],
                      available: bool) -> Tuple[bool, str]:
    if not available or liquidity_universe is None:
        return False, "LIQUIDITY_UNAVAILABLE"
    if not liquidity_universe:
        return False, "LIQUIDITY_UNIVERSE_EMPTY"
    return (base in set(liquidity_universe),
            "LIQUIDITY_OK" if base in set(liquidity_universe) else "BELOW_LIQUIDITY_FLOOR")


# ── Política de mérito: reduzir cedo, aumentar só com evidência ────────────
@dataclass(frozen=True)
class MeritConfig:
    min_total_sample: int = 30
    min_per_window: int = 10
    min_windows_stable: int = 3
    min_net_ev_r: float = 0.05
    max_uncertainty_r: float = 0.25
    temporal_discount: float = 0.85          # desconto por janela mais antiga
    candidates_considered: int = 1           # controle de múltiplas hipóteses
    required_periods: int = 3
    quarantine_windows: int = 1


def discounted_ev(window_means: Sequence[Optional[float]], *, config: MeritConfig) -> Optional[float]:
    """EV com desconto temporal explícito: janela antiga pesa menos."""
    weights, total = 0.0, 0.0
    for index, mean in enumerate(reversed(list(window_means or ()))):
        value = _finite(mean)
        if value is None:
            return None
        weight = config.temporal_discount ** index
        total += value * weight
        weights += weight
    return total / weights if weights else None


def bonferroni_threshold(config: MeritConfig) -> float:
    """Correção pré-registrada por número de candidatos contabilizados."""
    candidates = max(1, int(config.candidates_considered))
    return config.min_net_ev_r * candidates ** 0.5


def merit_verdict(*, windows: Sequence[Sample], net_ev_r: Optional[float],
                  uncertainty_r: Optional[float], costs_known: bool,
                  liquidity_ok: bool, quarantine_done: bool,
                  config: MeritConfig = MeritConfig()) -> Dict[str, Any]:
    """Veredicto de mérito. Reduzir pode responder antes; aumentar exige mais."""
    total = sum(window.n for window in windows or ())
    per_window = [window.n for window in windows or ()]
    means = [window.mean_r for window in windows or ()]
    reasons = []
    if not costs_known:
        reasons.append("NET_EV_NOT_COMPARABLE")
    if not liquidity_ok:
        reasons.append("LIQUIDITY_NOT_PROVEN")
    if not quarantine_done:
        reasons.append("OBSERVATION_QUARANTINE_PENDING")
    if total < config.min_total_sample:
        reasons.append("SAMPLE_BELOW_MINIMUM")
    if len(per_window) < config.min_windows_stable or any(n < config.min_per_window for n in per_window):
        reasons.append("WINDOW_SAMPLE_INSUFFICIENT")
    discounted = discounted_ev(means, config=config)
    threshold = bonferroni_threshold(config)
    value = _finite(net_ev_r)
    spread = _finite(uncertainty_r)
    if value is None or discounted is None:
        reasons.append("EV_UNAVAILABLE")
    elif value < threshold or discounted < threshold:
        reasons.append("EV_BELOW_CORRECTED_THRESHOLD")
    if spread is None or spread > config.max_uncertainty_r:
        reasons.append("UNCERTAINTY_TOO_WIDE")
    if not any(mean is not None and mean > 0 for mean in means):
        reasons.append("NO_POSITIVE_WINDOW")
    stable = all(mean is not None and mean > 0 for mean in means) if means else False
    verdict = VERDICT_PROMOTE if not reasons and stable else (
        VERDICT_INELIGIBLE if not means else VERDICT_HOLD)
    return {"policy_version": POLICY_VERSION, "verdict": verdict,
            "reason_codes": sorted(set(reasons)), "sample_total": total,
            "per_window": per_window, "discounted_ev_r": discounted,
            "ev_threshold_r": threshold, "stable_windows": stable,
            "risk_limits_changed": False, "applies_live": False}


def reduction_verdict(multiplier: float, details: Dict[str, Any]) -> Dict[str, Any]:
    """Redução pode responder antes do mérito completo — e nunca aumenta cap."""
    value = _finite(multiplier)
    applied = bool(details.get("applied")) and value is not None and value < 1.0
    return {"policy_version": POLICY_VERSION,
            "verdict": VERDICT_REDUCE if applied else VERDICT_HOLD,
            "multiplier": value if applied else 1.0,
            "reason_code": details.get("reason_code"),
            "meaning": "1.0 = camada NÃO aplicada (não é redução garantida)",
            "risk_limits_changed": False, "applies_live": False}
