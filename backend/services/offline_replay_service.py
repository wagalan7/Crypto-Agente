"""R10A: replay OHLCV limitado e puro, NÃO equivalente ao bot live.

Sem ENV, relógio, I/O, DB, provider ou import do motor live. O chamador fornece
oportunidades ponto-no-tempo, barras completas e custos explícitos. Nenhuma
função promove candidato ou aplica probabilidades/calibração da V2.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
from itertools import islice
import json
import math
import random
from typing import Any, Mapping, Sequence

# O laboratório estrutural do R08A NÃO é importado aqui: o replay de preços
# também roda no processo live (resolver R09). Só o comparador offline o
# carrega, tardiamente, no ramo STRUCTURAL_CONF_ONLY.

SCHEMA_VERSION = "R10A_OFFLINE_REPLAY_V1"
MAX_OPPORTUNITIES = 1000
MAX_BARS = 4096
MAX_BOOTSTRAP_SAMPLES = 2000
# Vocabulário fechado de `replay_opportunity(...)["status"]`.
CLOSED_STATUSES = ("CLOSED_STOP", "CLOSED_RUNNER_STOP", "CLOSED_TP2",
                   "CLOSED_TIME_STOP", "CLOSED_MAX_HOLD")
REPLAY_STATUSES = CLOSED_STATUSES + ("NOT_FILLED", "AMBIGUOUS_ENTRY_BAR",
                                     "MISSING_OR_UNORDERED_BARS", "INSUFFICIENT_DATA")
# MANAGEMENT_ONLY pode divergir do baseline em NO MÁXIMO um destes campos.
# Timeframe, janela de entrada, limite computacional e schema ficam idênticos.
MANAGEMENT_PARAMETERS = ("pre_tp1_time_stop_bars", "max_holding_bars", "tp1_fraction",
                         "be_lock_fraction", "trail_atr_multiple", "trail_activation_atr")


def _finite(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: número finito obrigatório")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"{name}: valor fora do domínio")
    return number


def _integer(value: Any, name: str, minimum: int, maximum: int = 10**16) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name}: inteiro entre {minimum} e {maximum} obrigatório")


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        _integer(self.timestamp_ms, "timestamp_ms", 0)
        for key in ("open", "high", "low", "close"):
            _finite(getattr(self, key), key, 1e-12)
        _finite(self.volume, "volume", 0)
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC inconsistente")


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    symbol: str
    direction: str
    decision_ts_ms: int
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    atr: float | None = None
    confluence_pct: float | None = None
    adx: float | None = None
    funding_pct: float | None = None
    features_asof_ms: int | None = None

    def __post_init__(self) -> None:
        for key in ("opportunity_id", "symbol"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{key}: identificador inválido")
        if self.direction not in ("long", "short"):
            raise ValueError("direction deve ser long ou short")
        _integer(self.decision_ts_ms, "decision_ts_ms", 0)
        for key in ("entry", "stop_loss", "tp1", "tp2"):
            _finite(getattr(self, key), key, 1e-12)
        levels = (self.stop_loss, self.entry, self.tp1, self.tp2)
        if self.direction == "short":
            levels = tuple(reversed(levels))
        if not all(a < b for a, b in zip(levels, levels[1:])):
            raise ValueError("níveis devem satisfazer stop < entrada < TP1 < TP2 (long), invertido short")
        if self.atr is not None:
            _finite(self.atr, "atr", 1e-12)
        for key in ("confluence_pct", "adx", "funding_pct"):
            if getattr(self, key) is not None:
                _finite(getattr(self, key), key)
        if self.features_asof_ms is not None:
            _integer(self.features_asof_ms, "features_asof_ms", 0, self.decision_ts_ms)
        if any(getattr(self, key) is not None for key in ("confluence_pct", "adx", "funding_pct")):
            if self.features_asof_ms is None:
                raise ValueError("features_asof_ms obrigatório para scores ponto-no-tempo")


@dataclass(frozen=True)
class ReplayConfig:
    bar_ms: int = 300_000
    entry_window_bars: int = 3
    pre_tp1_time_stop_bars: int = 12
    max_holding_bars: int = 24
    tp1_fraction: float = 0.45
    be_lock_fraction: float = 0.20
    trail_atr_multiple: float = 2.2
    trail_activation_atr: float = 0.5
    max_bars: int = 2048
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("schema_version não suportada")
        _integer(self.bar_ms, "bar_ms", 1, 86_400_000)
        _integer(self.max_bars, "max_bars", 1, MAX_BARS)
        for key in ("entry_window_bars", "pre_tp1_time_stop_bars", "max_holding_bars"):
            _integer(getattr(self, key), key, 1, self.max_bars)
        if self.pre_tp1_time_stop_bars > self.max_holding_bars:
            raise ValueError("time-stop deve caber no horizonte")
        if self.entry_window_bars + self.max_holding_bars - 1 > self.max_bars:
            raise ValueError("janela de entrada + horizonte excede max_bars")
        _finite(self.tp1_fraction, "tp1_fraction", 0)
        if not 0 < self.tp1_fraction < 1:
            raise ValueError("tp1_fraction deve estar entre 0 e 1, exclusivo")
        _finite(self.be_lock_fraction, "be_lock_fraction", 0)
        if self.be_lock_fraction > 1:
            raise ValueError("be_lock_fraction deve ser <= 1")
        _finite(self.trail_atr_multiple, "trail_atr_multiple", 1e-12)
        _finite(self.trail_activation_atr, "trail_activation_atr", 0)

    def manifest(self) -> dict:
        values = asdict(self)
        return {**values, "config_hash": _hash(values)}


@dataclass(frozen=True)
class CostConfig:
    """Bps (= 0,01%). Funding: cenário assinado por abertura, não histórico.

    Positivo debita long/credita short. Incide no restante apenas nas aberturas
    seguintes à entrada, antes das saídas. None nunca é convertido em zero.
    """
    fee_bps_per_side: float | None = None
    slippage_bps_per_side: float | None = None
    funding_bps_per_bar: float | None = None

    def __post_init__(self) -> None:
        for key in ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar"):
            value = getattr(self, key)
            if value is None:
                continue
            number = _finite(value, key, None if key.startswith("funding") else 0)
            if abs(number) >= 10_000:
                raise ValueError(f"{key}: bps absolutos devem ser < 10000")

    def manifest(self) -> dict:
        values = asdict(self)
        return {**values, "config_hash": _hash(values),
                "complete": all(value is not None for value in values.values()),
                "funding_model": "SIGNED_SCENARIO_AT_SUBSEQUENT_BAR_OPEN"}


@dataclass(frozen=True)
class CandidateRegistration:
    candidate_id: str
    registered_at_ms: int
    kind: str = "STRUCTURAL_CONF_ONLY"
    replay_config: ReplayConfig | None = None
    baseline_score_weights: tuple[float, float, float] = (0.60, 0.30, 0.10)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip() or len(self.candidate_id) > 128:
            raise ValueError("candidate_id inválido")
        _integer(self.registered_at_ms, "registered_at_ms", 0)
        if self.kind not in ("STRUCTURAL_CONF_ONLY", "MANAGEMENT_ONLY"):
            raise ValueError("tipo de candidato não registrado")
        if self.kind == "MANAGEMENT_ONLY" and not isinstance(self.replay_config, ReplayConfig):
            raise ValueError("MANAGEMENT_ONLY exige replay_config explícita")
        if self.kind == "STRUCTURAL_CONF_ONLY" and self.replay_config is not None:
            raise ValueError("ablação estrutural não possui regra econômica executável")
        if not isinstance(self.baseline_score_weights, tuple) or len(self.baseline_score_weights) != 3:
            raise ValueError("baseline_score_weights exige tupla congelada conf/adx/der")
        weights = [_finite(value, "weight", 0) for value in self.baseline_score_weights]
        if not 0 < sum(weights) < 1e6:
            raise ValueError("soma dos pesos inválida")

    def manifest(self) -> dict:
        values = asdict(self)
        return {**values, "candidate_hash": _hash(values), "research_only": True,
                "calibrated": False, "promotable": False}


@dataclass(frozen=True)
class ChronologicalSplit:
    train_start_ms: int
    validation_start_ms: int
    holdout_start_ms: int
    purge_bars: int = 1

    def __post_init__(self) -> None:
        for key in ("train_start_ms", "validation_start_ms", "holdout_start_ms"):
            _integer(getattr(self, key), key, 0)
        if not self.train_start_ms < self.validation_start_ms < self.holdout_start_ms:
            raise ValueError("split deve ser cronológico e estritamente crescente")
        _integer(self.purge_bars, "purge_bars", 1, MAX_BARS)


@dataclass(frozen=True)
class BootstrapConfig:
    seed: int = 20260916
    samples: int = 500
    block_size: int = 4

    def __post_init__(self) -> None:
        _integer(self.seed, "seed", 0)
        _integer(self.samples, "samples", 100, MAX_BOOTSTRAP_SAMPLES)
        _integer(self.block_size, "block_size", 1, MAX_OPPORTUNITIES)


def management_diff(baseline: ReplayConfig, candidate: ReplayConfig) -> list[str]:
    """Campos alterados. Levanta se algo fora da gestão mudou ou se >1 mudou.

    Config idêntica é permitida (controle A/A) e devolve lista vazia.
    """
    if not isinstance(baseline, ReplayConfig) or not isinstance(candidate, ReplayConfig):
        raise ValueError("configs tipadas obrigatórias")
    changed = [f.name for f in fields(ReplayConfig)
               if getattr(baseline, f.name) != getattr(candidate, f.name)]
    if any(name not in MANAGEMENT_PARAMETERS for name in changed):
        raise ValueError("MANAGEMENT_ONLY preserva timeframe, janela de entrada, max_bars e schema")
    if len(changed) > 1:
        raise ValueError("MANAGEMENT_ONLY altera no máximo um parâmetro comportamental")
    return changed


def replay_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "mode": "LOCAL_RESEARCH_ONLY",
        "promotable": False, "live_equivalent": False, "holdout_policy": "SEALED",
        "supports": ["long_short_touch_entry", "partial_tp1", "causal_next_bar_be_trail",
                     "time_stop", "explicit_cost_scenarios", "paired_management_comparison",
                     "structural_r08_ablation", "purged_chronological_split"],
        "candidate_types": ["STRUCTURAL_CONF_ONLY", "MANAGEMENT_ONLY"],
        "statuses": list(REPLAY_STATUSES),
        "management_parameters": list(MANAGEMENT_PARAMETERS),
        "management_max_changed_parameters": 1,
        "comparison_criteria": ["net_expectancy_r", "paired_delta_block_bootstrap_ci",
                                "profit_factor", "coverage", "sequential_drawdown_r"],
        "limits": {"max_opportunities": MAX_OPPORTUNITIES, "max_bars_per_opportunity": MAX_BARS,
                   "max_bootstrap_samples": MAX_BOOTSTRAP_SAMPLES, "max_candidates": 1},
        "limitations": [
            "Adaptador independente, não replica scanner, execução, portfolio, latência ou liquidez.",
            "Stop wick-based difere do classificador de snapshots (pré-TP1 close-based).",
            "Entrada touch-market hipotética; OHLCV não prova fila, fill ou ordem intrabar real.",
            "Entrada intrabar com saída possível na mesma vela fica ambígua, sem R.",
            "Stop/target intrabar após entrada confirmada usa stop-first e sinaliza ambiguidade.",
            "BE/trail calculados no fechamento só vigoram na próxima barra.",
            "Funding é cenário constante por abertura, não histórico nem custo inferido.",
            "IC por blocos é diagnóstico; amostras pequenas/dependência longa exigem revisão.",
            "Hashes provam identidade, não registro independente ou ausência de tuning externo.",
        ],
    }


def replay_opportunity(opportunity: Opportunity, bars: Sequence[Candle],
                       config: ReplayConfig, costs: CostConfig) -> dict:
    """Replay de barras completas a partir de ceil(decision/bar_ms).

    R usa distância PLANEJADA entry-stop; bruto usa preços de referência. Líquido
    subtrai fees, slippage adverso por perna e funding assinado. Dado incompleto,
    entrada ambígua ou custo ausente => líquido None, nunca R=0 fabricado.
    """
    if not isinstance(opportunity, Opportunity) or not isinstance(config, ReplayConfig) or not isinstance(costs, CostConfig):
        raise ValueError("contratos tipados obrigatórios")
    if len(bars) > config.max_bars:
        raise ValueError("input excede max_bars")
    o = opportunity
    direction = 1 if o.direction == "long" else -1
    risk = abs(o.entry - o.stop_loss)
    first_ms = ((o.decision_ts_ms + config.bar_ms - 1) // config.bar_ms) * config.bar_ms
    result = {
        "schema_version": SCHEMA_VERSION, "opportunity_id": o.opportunity_id,
        "mode": "LOCAL_RESEARCH_ONLY", "live_equivalent": False, "promotable": False,
        "config_hash": config.manifest()["config_hash"],
        "cost_config_hash": costs.manifest()["config_hash"],
        "status": "INSUFFICIENT_DATA", "reason_codes": [], "filled": False,
        "entry_reference_price": None, "entry_fill_price": None, "entry_ts_ms": None,
        "exit_ts_ms": None, "tp1_hit": False, "bars_observed": 0, "bars_held": 0,
        "gross_r": None, "net_r": None, "fee_r": None, "slippage_r": None,
        "funding_r": None, "risk_price_units": risk, "exits": [],
        "cost_status": "KNOWN_SCENARIO" if costs.manifest()["complete"] else "UNKNOWN",
        "funding_model": costs.manifest()["funding_model"],
    }
    reasons = result["reason_codes"]
    if not costs.manifest()["complete"]:
        reasons.append("COST_COMPONENT_UNKNOWN")
    remaining, realized, funding, active_stop = 1.0, 0.0, 0.0, o.stop_loss
    peak = None
    slip = None if costs.slippage_bps_per_side is None else costs.slippage_bps_per_side / 10_000

    def close_fraction(price: float, quantity: float, reason: str, timestamp: int) -> None:
        nonlocal remaining, realized
        realized += quantity * direction * (price - o.entry) / risk
        remaining = max(0.0, remaining - quantity)
        result["exits"].append({"reason": reason, "fraction": quantity,
                                "reference_price": price,
                                "fill_price": None if slip is None else price * (1 - direction * slip),
                                "timestamp_ms": timestamp})

    def finish(status: str, timestamp: int) -> dict:
        result["status"] = status
        result["exit_ts_ms"] = timestamp
        result["gross_r"] = realized
        if slip is not None:
            exit_notional = sum(x["fraction"] * x["reference_price"] for x in result["exits"])
            result["slippage_r"] = slip * (o.entry + exit_notional) / risk
        if costs.fee_bps_per_side is not None and slip is not None:
            paid_notional = result["entry_fill_price"] + sum(x["fraction"] * x["fill_price"] for x in result["exits"])
            result["fee_r"] = costs.fee_bps_per_side / 10_000 * paid_notional / risk
        if costs.funding_bps_per_bar is not None:
            result["funding_r"] = funding
        if result["cost_status"] == "KNOWN_SCENARIO":
            result["net_r"] = realized - result["slippage_r"] - result["fee_r"] - funding
        if not all(value is None or math.isfinite(value) for value in
                   (result["gross_r"], result["net_r"], result["fee_r"], result["slippage_r"], result["funding_r"])):
            raise ValueError("overflow no cálculo econômico")
        return result

    for index, bar in enumerate(bars):
        if not isinstance(bar, Candle):
            raise ValueError("bars exige Candle validada")
        if bar.timestamp_ms != first_ms + index * config.bar_ms:
            result["status"] = "MISSING_OR_UNORDERED_BARS"
            reasons.append("EXPECTED_CONTIGUOUS_CLOSED_BARS")
            return result
        result["bars_observed"] += 1
        newly_filled = False
        if not result["filled"]:
            if bar.low <= o.entry <= bar.high:
                result["filled"] = True
                newly_filled = True
                result["entry_reference_price"] = o.entry
                result["entry_fill_price"] = None if slip is None else o.entry * (1 + direction * slip)
                result["entry_ts_ms"] = bar.timestamp_ms
                touches_exit = (bar.low <= o.stop_loss or bar.high >= o.tp1) if direction == 1 else (bar.high >= o.stop_loss or bar.low <= o.tp1)
                if bar.open != o.entry and touches_exit:
                    result["status"] = "AMBIGUOUS_ENTRY_BAR"
                    reasons.append("PRE_ENTRY_EXTREMES_ORDER_UNKNOWN")
                    return result
                if bar.open != o.entry:
                    reasons.append("ENTRY_INTRABAR_TIME_UNKNOWN")
            elif index + 1 == config.entry_window_bars:
                result["status"] = "NOT_FILLED"
                reasons.append("ENTRY_NOT_TOUCHED_WITHIN_WINDOW")
                return result
            else:
                continue
        result["bars_held"] += 1
        if not newly_filled and costs.funding_bps_per_bar is not None:
            funding += direction * remaining * bar.open * costs.funding_bps_per_bar / 10_000 / risk
        adverse = bar.low if direction == 1 else bar.high
        favorable = bar.high if direction == 1 else bar.low
        stop_hit = direction * (adverse - active_stop) <= 0
        target = o.tp2 if result["tp1_hit"] else o.tp1
        target_hit = direction * (favorable - target) >= 0
        gap_stop = not newly_filled and direction * (bar.open - active_stop) <= 0
        if stop_hit:
            if target_hit:
                reasons.append("INTRABAR_STOP_TARGET_AMBIGUITY_STOP_FIRST")
            exit_price = bar.open if gap_stop else active_stop
            if gap_stop:
                reasons.append("ADVERSE_GAP_STOP_AT_OPEN")
            close_fraction(exit_price, remaining, "RUNNER_STOP" if result["tp1_hit"] else "STOP", bar.timestamp_ms)
            return finish("CLOSED_RUNNER_STOP" if result["tp1_hit"] else "CLOSED_STOP", bar.timestamp_ms)
        if not result["tp1_hit"] and direction * (favorable - o.tp1) >= 0:
            result["tp1_hit"] = True
            close_fraction(o.tp1, config.tp1_fraction, "TP1", bar.timestamp_ms)
        if result["tp1_hit"] and direction * (favorable - o.tp2) >= 0:
            close_fraction(o.tp2, remaining, "TP2", bar.timestamp_ms)
            return finish("CLOSED_TP2", bar.timestamp_ms)
        time_stop = not result["tp1_hit"] and result["bars_held"] >= config.pre_tp1_time_stop_bars
        if time_stop or result["bars_held"] >= config.max_holding_bars:
            close_fraction(bar.close, remaining, "TIME_STOP" if time_stop else "MAX_HOLD", bar.timestamp_ms)
            return finish("CLOSED_TIME_STOP" if time_stop else "CLOSED_MAX_HOLD", bar.timestamp_ms)
        if result["tp1_hit"]:
            peak = favorable if peak is None else (max(peak, favorable) if direction == 1 else min(peak, favorable))
            next_stop = o.entry + config.be_lock_fraction * (o.tp1 - o.entry)
            if o.atr is not None and direction * (peak - o.tp1) >= config.trail_activation_atr * o.atr:
                trail = peak - direction * config.trail_atr_multiple * o.atr
                next_stop = max(next_stop, trail) if direction == 1 else min(next_stop, trail)
            active_stop = max(active_stop, next_stop) if direction == 1 else min(active_stop, next_stop)
    reasons.append("INCOMPLETE_ENTRY_WINDOW" if not result["filled"] else "INCOMPLETE_FORWARD_HORIZON")
    return result


def _metrics(values: Sequence[float]) -> dict:
    if not values:
        return {"resolved_n": 0, "net_expectancy_r": None, "net_total_r": None,
                "win_rate_pct": None, "profit_factor": None,
                "profit_factor_reason": "NO_RESOLVED_SAMPLE", "sequential_drawdown_r": None}
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"resolved_n": len(values), "net_expectancy_r": sum(values) / len(values),
            "net_total_r": sum(values), "win_rate_pct": 100 * sum(v > 0 for v in values) / len(values),
            "profit_factor": wins / losses if losses > 0 else None,
            "profit_factor_reason": None if losses > 0 else "NO_LOSSES_DENOMINATOR",
            "sequential_drawdown_r": drawdown}


def _paired_ci(pairs: Sequence[tuple[float, float]], config: BootstrapConfig) -> dict | None:
    if len(pairs) < max(2, config.block_size * 2):
        return None
    differences = [candidate - baseline for baseline, candidate in pairs]
    rng = random.Random(config.seed)
    n = len(differences)
    means = []
    for _ in range(config.samples):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(differences[(start + offset) % n] for offset in range(config.block_size))
        means.append(sum(sample[:n]) / n)
    means.sort()
    return {"point": sum(differences) / n, "low": means[int(.025 * (len(means) - 1))],
            "high": means[int(.975 * (len(means) - 1))], "confidence": .95,
            "method": "PAIRED_CIRCULAR_BLOCK_BOOTSTRAP", **asdict(config)}


def compare_registered_candidate(opportunities: Sequence[Opportunity],
                                 bars_by_id: Mapping[str, Sequence[Candle]],
                                 baseline_config: ReplayConfig,
                                 candidate: CandidateRegistration,
                                 costs: CostConfig, split: ChronologicalSplit,
                                 bootstrap: BootstrapConfig = BootstrapConfig()) -> dict:
    """Um candidato, mesmas oportunidades/custos; holdout nunca acessado.

    Purga usa horizonte MÁXIMO possível de ambos, não saída observada. Métricas
    comparáveis usam pares resolvidos e reportam cobertura. Não treina parâmetros.
    """
    if len(opportunities) > MAX_OPPORTUNITIES:
        raise ValueError("excesso de oportunidades")
    if not isinstance(candidate, CandidateRegistration):
        raise ValueError("exatamente uma CandidateRegistration obrigatória")
    if candidate.registered_at_ms > split.train_start_ms:
        raise ValueError("candidato deve estar registrado antes do início do treino")
    if len({o.opportunity_id for o in opportunities}) != len(opportunities):
        raise ValueError("opportunity_id duplicado")
    if not isinstance(baseline_config, ReplayConfig) or not isinstance(costs, CostConfig):
        raise ValueError("contratos tipados obrigatórios")
    if not isinstance(split, ChronologicalSplit) or not isinstance(bootstrap, BootstrapConfig):
        raise ValueError("contratos tipados obrigatórios")
    candidate_config = candidate.replay_config
    changed = management_diff(baseline_config, candidate_config) if candidate_config is not None else None
    if candidate.kind == "STRUCTURAL_CONF_ONLY":
        # Import tardio e exclusivo deste ramo; o replay de preços não precisa dele.
        from services.score_research_service import score_v2_raw, score_v3_conf_only
    configs = [baseline_config] + ([candidate_config] if candidate_config else [])
    horizon_bars = max(c.entry_window_bars + c.max_holding_bars - 1 for c in configs)
    experiment = {"baseline": baseline_config.manifest(), "candidate": candidate.manifest(),
                  "costs": costs.manifest(), "split": asdict(split), "bootstrap": asdict(bootstrap),
                  "management_changed_parameters": changed}
    output = {
        "schema_version": SCHEMA_VERSION, "mode": "LOCAL_RESEARCH_ONLY", "promotable": False,
        "live_equivalent": False, "holdout_policy": "SEALED", "holdout_outcomes_loaded": False,
        "experiment": experiment, "experiment_hash": _hash(experiment),
        "decision": "NO_PROMOTION_RESEARCH_ONLY", "winner": None,
        "economic_comparison_status": "AVAILABLE_IF_PAIRED_KNOWN_COSTS" if candidate_config else "UNAVAILABLE_STRUCTURAL_CANDIDATE",
        "counts": {"training": 0, "validation": 0, "purged": 0, "holdout_sealed": 0, "before_training": 0},
        "splits": {}, "rows": [], "limitations": replay_manifest()["limitations"],
    }
    grouped: dict[str, list] = {"training": [], "validation": []}
    for opportunity in sorted(opportunities, key=lambda o: (o.decision_ts_ms, o.opportunity_id)):
        decision = opportunity.decision_ts_ms
        if decision >= split.holdout_start_ms:
            output["counts"]["holdout_sealed"] += 1
            continue
        if decision < split.train_start_ms:
            output["counts"]["before_training"] += 1
            continue
        name = "training" if decision < split.validation_start_ms else "validation"
        boundary = split.validation_start_ms if name == "training" else split.holdout_start_ms
        first_ms = ((decision + baseline_config.bar_ms - 1) // baseline_config.bar_ms) * baseline_config.bar_ms
        if first_ms + (horizon_bars + split.purge_bars) * baseline_config.bar_ms > boundary:
            output["counts"]["purged"] += 1
            continue
        output["counts"][name] += 1
        # Só as barras do horizonte máximo são lidas (islice); as posteriores,
        # que poderiam cruzar a fronteira, nem chegam a ser materializadas.
        bars = tuple(islice(iter(bars_by_id.get(opportunity.opportunity_id, ())), horizon_bars))
        if any(not isinstance(bar, Candle) or bar.timestamp_ms + baseline_config.bar_ms > boundary
               for bar in bars):
            raise ValueError("barra cruza a fronteira do split; purga violada")
        baseline = replay_opportunity(opportunity, bars, baseline_config, costs)
        alternative = replay_opportunity(opportunity, bars, candidate_config, costs) if candidate_config else None
        row = {"opportunity_id": opportunity.opportunity_id, "split": name,
               "baseline": baseline, "candidate": alternative}
        if candidate.kind == "STRUCTURAL_CONF_ONLY":
            features = {key: getattr(opportunity, key) for key in ("confluence_pct", "adx", "funding_pct")}
            baseline_score = score_v2_raw(**features, weights=dict(zip(("conf", "adx", "der"), candidate.baseline_score_weights)))
            candidate_score = score_v3_conf_only(**features)
            row["structural_scores"] = {"baseline": baseline_score, "candidate": candidate_score,
                                        "economic_rule_available": False, "calibrated": False}
        grouped[name].append(row)
        output["rows"].append(row)
    for name, rows in grouped.items():
        baseline_values = [r["baseline"]["net_r"] for r in rows if r["baseline"]["net_r"] is not None]
        pairs = [(r["baseline"]["net_r"], r["candidate"]["net_r"]) for r in rows
                 if r["candidate"] is not None and r["baseline"]["net_r"] is not None and r["candidate"]["net_r"] is not None]
        output["splits"][name] = {
            "opportunities_n": len(rows), "paired_resolved_n": len(pairs),
            "excluded_or_unpaired_n": len(rows) - len(pairs),
            "baseline_descriptive": _metrics(baseline_values),
            "baseline_paired": _metrics([a for a, _ in pairs]),
            "candidate_paired": _metrics([b for _, b in pairs]),
            "paired_delta_ci": _paired_ci(pairs, bootstrap),
            "ci_status": "AVAILABLE" if len(pairs) >= max(2, bootstrap.block_size * 2) else "INSUFFICIENT_BLOCKS",
        }
    return output


def run_payload(payload: dict) -> dict:
    """Adaptador JSON puro para CLI local. Não lê arquivo e não aceita holdout.

    Replay: mode/config/costs/opportunity/bars. Compare: mode/baseline_config/
    costs/candidate/split/bootstrap/opportunities/bars_by_id. Compare decodifica
    barras somente após purga; arquivo de entrada não deve conter dados holdout.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload deve ser objeto")
    mode = payload.get("mode", "replay")
    if mode == "replay":
        allowed = {"mode", "config", "costs", "opportunity", "bars"}
        if set(payload) - allowed:
            raise ValueError("chave desconhecida no payload")
        config = ReplayConfig(**payload.get("config", {}))
        rows = payload.get("bars", [])
        if not isinstance(rows, list) or len(rows) > config.max_bars:
            raise ValueError("barras excedem limite ou formato inválido")
        return replay_opportunity(Opportunity(**payload["opportunity"]), [Candle(**bar) for bar in rows],
                                  config, CostConfig(**payload.get("costs", {})))
    if mode != "compare":
        raise ValueError("mode não suportado")
    allowed = {"mode", "baseline_config", "costs", "candidate", "split", "bootstrap", "opportunities", "bars_by_id"}
    if set(payload) - allowed:
        raise ValueError("chave desconhecida no payload")
    raw_opportunities = payload.get("opportunities", [])
    if not isinstance(raw_opportunities, list) or len(raw_opportunities) > MAX_OPPORTUNITIES:
        raise ValueError("oportunidades excedem limite ou formato inválido")
    opportunities = [Opportunity(**row) for row in raw_opportunities]
    split = ChronologicalSplit(**payload["split"])
    raw_bars = payload.get("bars_by_id", {})
    if not isinstance(raw_bars, dict) or len(raw_bars) > MAX_OPPORTUNITIES:
        raise ValueError("bars_by_id inválido")
    known_ids = {o.opportunity_id for o in opportunities}
    if set(raw_bars) - known_ids:
        raise ValueError("barras sem oportunidade declarada")
    if any(o.decision_ts_ms >= split.holdout_start_ms and o.opportunity_id in raw_bars for o in opportunities):
        raise ValueError("payload não pode conter outcomes/barras de holdout")
    for rows in raw_bars.values():
        if not isinstance(rows, list):
            raise ValueError("barras em formato inválido")
        for bar in rows:
            stamp = bar.get("timestamp_ms") if isinstance(bar, dict) else None
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                raise ValueError("timestamp_ms inteiro obrigatório")
            if stamp >= split.holdout_start_ms:
                raise ValueError("payload não pode conter barras do período holdout")
    baseline = ReplayConfig(**payload.get("baseline_config", {}))
    raw_candidate = dict(payload["candidate"])
    if raw_candidate.get("replay_config") is not None:
        raw_candidate["replay_config"] = ReplayConfig(**raw_candidate["replay_config"])
    if "baseline_score_weights" in raw_candidate:
        raw_candidate["baseline_score_weights"] = tuple(raw_candidate["baseline_score_weights"])
    candidate = CandidateRegistration(**raw_candidate)

    class DecodedBars(Mapping):
        def __getitem__(self, key):
            rows = raw_bars[key]
            if len(rows) > baseline.max_bars:
                raise ValueError("barras excedem limite")
            return tuple(Candle(**bar) for bar in rows)

        def __iter__(self):
            return iter(raw_bars)

        def __len__(self):
            return len(raw_bars)

    return compare_registered_candidate(opportunities, DecodedBars(), baseline, candidate,
                                        CostConfig(**payload.get("costs", {})), split,
                                        BootstrapConfig(**payload.get("bootstrap", {})))
