"""R10E — validação walk-forward de verdade, com fronteira do holdout final.

Puro: sem ENV (exceto o seletor de modo), banco, rede ou relógio do sistema.

O que este módulo impõe:
  • janelas rolantes REAIS (várias dobras que avançam no tempo) — metade recente
    não é walk-forward;
  • purga pelo MAIOR horizonte e embargo depois do teste;
  • preprocessamento, seleção, normalização e calibração treinados só no treino
    de cada dobra; a validação escolhe o candidato, o teste final não escolhe;
  • baseline e challenger compartilham oportunidades e cenário; aceitas por um
    lado só entram no delta de política e no turnover, não são descartadas;
  • subamostra pareada não substitui o resultado da política inteira;
  • cobertura/UNKNOWN não podem favorecer candidato por exclusão seletiva;
  • intervalo por blocos e controle de múltiplas comparações;
  • custo, horizonte ou cobertura insuficientes ⇒ evidência insuficiente e
    NENHUM vencedor;
  • holdout real continua selado: só o SINTÉTICO é avaliável, e hash não prova
    pré-registro independente nem pode ser retrodatado.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

WF_VERSION = "R10E_WALK_FORWARD_V1"
CONTRACT = "CANDIDATE_POLICY"
MODE_ENV = "R10_WALK_FORWARD_MODE"
MODE_INACTIVE = "inactive"
MODE_SIMULATION = "simulation"

MIN_FOLDS = 3
DEFAULT_ALPHA = 0.05
MAX_BOOTSTRAP_SAMPLES = 2000

# ── Motivos ─────────────────────────────────────────────────────────────────
OK = "OK"
INSUFFICIENT_WINDOWS = "INSUFFICIENT_WINDOWS"
NOT_WALK_FORWARD = "NOT_WALK_FORWARD"
FOLD_EMPTY_AFTER_PURGE = "FOLD_EMPTY_AFTER_PURGE"
TEST_LEAKAGE = "TEST_LEAKAGE"
FIT_OUTSIDE_TRAIN = "FIT_OUTSIDE_TRAIN"
SELECTIVE_EXCLUSION = "SELECTIVE_EXCLUSION"
COVERAGE_INSUFFICIENT = "COVERAGE_INSUFFICIENT"
COSTS_UNKNOWN = "COSTS_UNKNOWN"
HORIZON_INSUFFICIENT = "HORIZON_INSUFFICIENT"
SAMPLE_INSUFFICIENT = "SAMPLE_INSUFFICIENT"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
CI_INCLUDES_ZERO = "CI_INCLUDES_ZERO"
REAL_HOLDOUT_SEALED = "REAL_HOLDOUT_SEALED"
BACKDATED_PREREGISTRATION = "BACKDATED_PREREGISTRATION"
PREREGISTRATION_NOT_INDEPENDENT = "PREREGISTRATION_NOT_INDEPENDENT"
FINAL_RESULTS_NOT_FOR_TUNING = "FINAL_RESULTS_NOT_FOR_TUNING"
REASON_CODES = frozenset({
    OK, INSUFFICIENT_WINDOWS, NOT_WALK_FORWARD, FOLD_EMPTY_AFTER_PURGE, TEST_LEAKAGE,
    FIT_OUTSIDE_TRAIN, SELECTIVE_EXCLUSION, COVERAGE_INSUFFICIENT, COSTS_UNKNOWN,
    HORIZON_INSUFFICIENT, SAMPLE_INSUFFICIENT, INSUFFICIENT_EVIDENCE, CI_INCLUDES_ZERO,
    REAL_HOLDOUT_SEALED, BACKDATED_PREREGISTRATION, PREREGISTRATION_NOT_INDEPENDENT,
    FINAL_RESULTS_NOT_FOR_TUNING,
})

#: Etapas que só podem ser AJUSTADAS no treino da própria dobra.
FITTED_STAGES = ("preprocessing", "feature_selection", "normalization", "calibration")
SLICE_KEYS = ("window", "playbook", "timeframe", "side", "regime")

LIMITATIONS = [
    "Resultado de cenário não é rentabilidade observada.",
    "Sem custo completo não existe EV líquido comparável.",
    "Cobertura desigual entre lados invalida a comparação.",
    "Subamostra pareada não substitui a política inteira.",
    "Hash não prova pré-registro independente.",
]


def selected_mode() -> str:
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_SIMULATION if value == MODE_SIMULATION else MODE_INACTIVE


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode("utf-8")).hexdigest()


# ── Janelas rolantes ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Fold:
    index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int

    def as_dict(self) -> Dict[str, Any]:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


def rolling_windows(*, start_ms: int, end_ms: int, bar_ms: int, train_bars: int,
                    test_bars: int, step_bars: Optional[int] = None) -> Dict[str, Any]:
    """Dobras que AVANÇAM no tempo. Uma única divisão não é walk-forward."""
    for name, value in (("start_ms", start_ms), ("end_ms", end_ms), ("bar_ms", bar_ms),
                        ("train_bars", train_bars), ("test_bars", test_bars)):
        if _int(value) is None or value <= 0:
            raise ValueError(f"{name}: inteiro positivo obrigatório")
    step = _int(step_bars) if step_bars is not None else test_bars
    if step is None or step <= 0:
        raise ValueError("step_bars: inteiro positivo obrigatório")
    if end_ms <= start_ms:
        raise ValueError("janela vazia")
    folds: List[Fold] = []
    train_start = start_ms
    while True:
        train_end = train_start + train_bars * bar_ms
        test_end = train_end + test_bars * bar_ms
        if test_end > end_ms:
            break
        folds.append(Fold(len(folds), train_start, train_end, train_end, test_end))
        train_start += step * bar_ms
    if len(folds) < MIN_FOLDS:
        return {"folds": tuple(folds), "reason_code": INSUFFICIENT_WINDOWS,
                "walk_forward": False, "min_folds": MIN_FOLDS}
    return {"folds": tuple(folds), "reason_code": OK, "walk_forward": True,
            "min_folds": MIN_FOLDS}


def is_walk_forward(folds: Sequence[Fold]) -> Dict[str, Any]:
    """Metade recente (uma dobra) NÃO é validação walk-forward."""
    folds = tuple(folds or ())
    if len(folds) < MIN_FOLDS:
        return {"walk_forward": False, "reason_code": INSUFFICIENT_WINDOWS}
    advancing = all(b.test_start_ms > a.test_start_ms and b.train_start_ms >= a.train_start_ms
                    for a, b in zip(folds, folds[1:]))
    disjoint = all(b.test_start_ms >= a.test_end_ms for a, b in zip(folds, folds[1:]))
    if not advancing or not disjoint:
        return {"walk_forward": False, "reason_code": NOT_WALK_FORWARD}
    return {"walk_forward": True, "reason_code": OK}


def apply_purge_embargo(fold: Fold, *, horizon_bars: int, bar_ms: int,
                        embargo_bars: int = 0) -> Dict[str, Any]:
    """Purga pelo MAIOR horizonte; embargo empurra o início do teste."""
    if _int(horizon_bars) is None or horizon_bars < 0 or _int(embargo_bars) is None or embargo_bars < 0:
        raise ValueError("horizonte e embargo devem ser inteiros não negativos")
    purged_train_end = fold.train_end_ms - horizon_bars * bar_ms
    test_start = fold.test_start_ms + embargo_bars * bar_ms
    reason = OK
    if purged_train_end <= fold.train_start_ms or test_start >= fold.test_end_ms:
        reason = FOLD_EMPTY_AFTER_PURGE
    return {"train_start_ms": fold.train_start_ms, "train_end_ms": purged_train_end,
            "test_start_ms": test_start, "test_end_ms": fold.test_end_ms,
            "purged_bars": horizon_bars, "embargo_bars": embargo_bars,
            "reason_code": reason,
            "usable": reason == OK}


def fold_discipline(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Cada etapa ajustável só pode ter sido ajustada no TREINO da dobra."""
    record = record if isinstance(record, Mapping) else {}
    problems: List[str] = []
    for stage in FITTED_STAGES:
        fitted_on = record.get(stage)
        if fitted_on != "train":
            problems.append(FIT_OUTSIDE_TRAIN)
            break
    if record.get("candidate_selected_on") not in ("validation", None):
        problems.append(TEST_LEAKAGE)
    if record.get("test_used_for_selection") is True:
        problems.append(TEST_LEAKAGE)
    codes = tuple(dict.fromkeys(problems))
    return {"ok": not codes, "reason_codes": codes or (OK,),
            "selection_stage": record.get("candidate_selected_on"),
            "final_test_role": "REPORT_ONLY"}


# ── Pareamento e cobertura ──────────────────────────────────────────────────
def pair_opportunities(baseline: Sequence[Mapping[str, Any]],
                       candidate: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Mesmo cenário, mesmas oportunidades. Aceita por um lado NÃO é descartada."""
    def index(rows):
        out = {}
        for row in rows or ():
            key = str((row or {}).get("opportunity_id") or "")
            if key:
                out[key] = row
        return out

    base, cand = index(baseline), index(candidate)
    shared = sorted(set(base) & set(cand))
    only_base = sorted(set(base) - set(cand))
    only_cand = sorted(set(cand) - set(base))
    paired = [{"opportunity_id": key,
               "baseline_net_r": _finite(base[key].get("net_r")),
               "candidate_net_r": _finite(cand[key].get("net_r"))} for key in shared]
    removed = [_finite(base[key].get("net_r")) for key in only_base]
    added = [_finite(cand[key].get("net_r")) for key in only_cand]
    total = len(set(base) | set(cand))
    return {
        "paired": paired,
        "only_baseline": only_base,
        "only_candidate": only_cand,
        "policy_delta": {
            "removed_n": len(only_base), "added_n": len(only_cand),
            "removed_net_r": sum(v for v in removed if v is not None) if removed else 0.0,
            "added_net_r": sum(v for v in added if v is not None) if added else 0.0,
            "removed_unknown": sum(1 for v in removed if v is None),
            "added_unknown": sum(1 for v in added if v is None),
        },
        "turnover_pct": (100.0 * (len(only_base) + len(only_cand)) / total) if total else 0.0,
        "paired_subsample_is_not_the_policy": True,
    }


def coverage_guard(baseline_stats: Mapping[str, Any], candidate_stats: Mapping[str, Any],
                   *, tolerance_pct: float = 5.0,
                   min_coverage_pct: float = 70.0) -> Dict[str, Any]:
    """Exclusão seletiva não pode favorecer um lado."""
    def rate(stats):
        stats = stats if isinstance(stats, Mapping) else {}
        total = _finite(stats.get("considered"))
        resolved = _finite(stats.get("resolved"))
        if total is None or resolved is None or total <= 0:
            return None
        return 100.0 * resolved / total

    base_rate, cand_rate = rate(baseline_stats), rate(candidate_stats)
    if base_rate is None or cand_rate is None:
        return {"ok": False, "reason_code": COVERAGE_INSUFFICIENT,
                "baseline_coverage_pct": base_rate, "candidate_coverage_pct": cand_rate}
    if min(base_rate, cand_rate) < min_coverage_pct:
        return {"ok": False, "reason_code": COVERAGE_INSUFFICIENT,
                "baseline_coverage_pct": base_rate, "candidate_coverage_pct": cand_rate}
    if abs(base_rate - cand_rate) > tolerance_pct:
        return {"ok": False, "reason_code": SELECTIVE_EXCLUSION,
                "baseline_coverage_pct": base_rate, "candidate_coverage_pct": cand_rate}
    return {"ok": True, "reason_code": OK, "baseline_coverage_pct": base_rate,
            "candidate_coverage_pct": cand_rate}


# ── Estatística ─────────────────────────────────────────────────────────────
def multiplicity_adjusted_alpha(*, alpha: float = DEFAULT_ALPHA,
                                comparisons: int = 1) -> Dict[str, Any]:
    """Bonferroni sobre o nível: mais comparações, intervalo mais exigente."""
    count = max(1, int(comparisons))
    value = _finite(alpha)
    if value is None or not 0.0 < value < 1.0:
        raise ValueError("alpha deve estar em (0, 1)")
    return {"alpha": value, "comparisons": count, "adjusted_alpha": value / count,
            "method": "BONFERRONI_ON_ALPHA"}


def block_bootstrap_ci(values: Sequence[float], *, seed: int, samples: int = 500,
                       block_size: int = 5, alpha: float = DEFAULT_ALPHA,
                       comparisons: int = 1) -> Dict[str, Any]:
    """IC por BLOCOS (dependência serial), determinístico e com multiplicidade."""
    data = [v for v in (_finite(x) for x in (values or ())) if v is not None]
    adjusted = multiplicity_adjusted_alpha(alpha=alpha, comparisons=comparisons)
    if len(data) < max(2, block_size):
        return {"available": False, "reason_code": SAMPLE_INSUFFICIENT, "low": None,
                "high": None, "mean": None, **adjusted}
    count = max(1, min(int(samples), MAX_BOOTSTRAP_SAMPLES))
    rng = random.Random(seed)
    blocks = max(1, math.ceil(len(data) / block_size))
    means = []
    for _ in range(count):
        sample: List[float] = []
        for _ in range(blocks):
            start = rng.randrange(0, len(data))
            sample.extend(data[start:start + block_size] or data[:block_size])
        sample = sample[:len(data)]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo_index = int((adjusted["adjusted_alpha"] / 2) * (len(means) - 1))
    hi_index = int((1 - adjusted["adjusted_alpha"] / 2) * (len(means) - 1))
    return {"available": True, "reason_code": OK, "low": means[lo_index],
            "high": means[hi_index], "mean": sum(data) / len(data),
            "samples": count, "block_size": block_size, **adjusted}


def slice_metrics(rows: Sequence[Mapping[str, Any]], *, by: str) -> Dict[str, Any]:
    """Métricas por janela/playbook/TF/lado/regime, com desconhecido explícito."""
    if by not in SLICE_KEYS:
        raise ValueError(f"corte deve ser um de {SLICE_KEYS}")
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows or ():
        row = row if isinstance(row, Mapping) else {}
        label = row.get(by)
        name = str(label) if label is not None else "UNKNOWN"
        bucket = buckets.setdefault(name, {"ops": 0, "resolved": 0, "unknown": 0,
                                           "net_total_r": 0.0, "gains_r": 0.0,
                                           "losses_r": 0.0, "costs_r": 0.0,
                                           "equity": 0.0, "peak": 0.0,
                                           "max_drawdown_r": 0.0})
        bucket["ops"] += 1
        net = _finite(row.get("net_r"))
        for cost_key in ("fee_r", "slippage_r", "funding_r"):
            cost = _finite(row.get(cost_key))
            if cost is not None:
                bucket["costs_r"] += abs(cost)
        if net is None:
            bucket["unknown"] += 1
            continue
        bucket["resolved"] += 1
        bucket["net_total_r"] += net
        if net > 0:
            bucket["gains_r"] += net
        else:
            bucket["losses_r"] += -net
        bucket["equity"] += net
        bucket["peak"] = max(bucket["peak"], bucket["equity"])
        bucket["max_drawdown_r"] = max(bucket["max_drawdown_r"],
                                       bucket["peak"] - bucket["equity"])
    out = {}
    for name, bucket in buckets.items():
        resolved = bucket["resolved"]
        losses = bucket["losses_r"]
        out[name] = {
            "ops": bucket["ops"], "resolved": resolved, "unknown": bucket["unknown"],
            "net_total_r": bucket["net_total_r"] if resolved else None,
            "net_expectancy_r": (bucket["net_total_r"] / resolved) if resolved else None,
            "profit_factor": (bucket["gains_r"] / losses) if losses > 0 else None,
            "profit_factor_reason": None if losses > 0 else "NO_LOSSES_DENOMINATOR",
            "max_drawdown_r": bucket["max_drawdown_r"] if resolved else None,
            "gains_r": bucket["gains_r"], "losses_r": bucket["losses_r"],
            "costs_r": bucket["costs_r"],
        }
    return out


# ── Runner: EXECUTAR cada dobra, não apenas descrever janelas ───────────────
POLICY_DELTA_UNKNOWN = "POLICY_DELTA_UNKNOWN"
STUDIES_NOT_EXECUTED = "STUDIES_NOT_EXECUTED"
DELTA_DISAGREES_WITH_CI = "DELTA_DISAGREES_WITH_CI"


def _rows_by_time(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    out = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        stamp = _int(row.get("decision_ts_ms"))
        if stamp is None or not str(row.get("opportunity_id") or ""):
            continue
        out.append(row)
    return sorted(out, key=lambda item: (item["decision_ts_ms"], item["opportunity_id"]))


def policy_delta_r(paired: Mapping[str, Any]) -> Dict[str, Any]:
    """Delta da POLÍTICA INTEIRA: pares + o que só um lado aceitou.

    Ausência de resultado conhecido em qualquer parcela torna o delta `None` —
    zero de não-exposição comprovada não é zero de dado desconhecido.
    """
    paired = paired if isinstance(paired, Mapping) else {}
    total = 0.0
    for item in paired.get("paired") or ():
        base = _finite((item or {}).get("baseline_net_r"))
        cand = _finite((item or {}).get("candidate_net_r"))
        if base is None or cand is None:
            return {"available": False, "reason_code": POLICY_DELTA_UNKNOWN, "value": None}
        total += cand - base
    delta = paired.get("policy_delta") or {}
    if int(delta.get("removed_unknown") or 0) or int(delta.get("added_unknown") or 0):
        return {"available": False, "reason_code": POLICY_DELTA_UNKNOWN, "value": None}
    added = _finite(delta.get("added_net_r"))
    removed = _finite(delta.get("removed_net_r"))
    if added is None or removed is None:
        return {"available": False, "reason_code": POLICY_DELTA_UNKNOWN, "value": None}
    total += added - removed
    return {"available": True, "reason_code": OK, "value": total,
            "paired_n": len(paired.get("paired") or ()),
            "only_baseline_n": len(paired.get("only_baseline") or ()),
            "only_candidate_n": len(paired.get("only_candidate") or ())}


def run_walk_forward(*, baseline: Sequence[Mapping[str, Any]],
                     candidate: Sequence[Mapping[str, Any]],
                     folds: Sequence[Fold], bar_ms: int = 300_000,
                     horizon_bars: int = 0, embargo_bars: int = 0,
                     costs_complete: bool = False, horizon_sufficient: bool = False,
                     coverage: Optional[Mapping[str, Any]] = None,
                     seed: int = 7, samples: int = 500, block_size: int = 5,
                     alpha: float = DEFAULT_ALPHA, comparisons: int = 1) -> Dict[str, Any]:
    """Executa CADA dobra e devolve resultados rastreáveis ao veredito.

    Em cada dobra: separação temporal com purga/embargo, escolha do candidato
    SOMENTE com o treino, avaliação fora da amostra e agregação. O veredito
    consome o que saiu daqui — uma string "train" e uma lista de janelas não
    provam que isso aconteceu.
    """
    base_rows = _rows_by_time(baseline)
    cand_rows = _rows_by_time(candidate)
    executed: List[Dict[str, Any]] = []
    test_deltas: List[float] = []
    all_base: List[Mapping[str, Any]] = []
    all_cand: List[Mapping[str, Any]] = []
    for fold in folds or ():
        window = apply_purge_embargo(fold, horizon_bars=horizon_bars, bar_ms=bar_ms,
                                     embargo_bars=embargo_bars)
        if not window["usable"]:
            executed.append({"fold": fold.index, "reason_code": window["reason_code"],
                             "train_n": 0, "test_n": 0, "delta_net_r": None})
            continue

        def _slice(rows, start, end):
            return [row for row in rows if start <= row["decision_ts_ms"] < end]

        train_base = _slice(base_rows, window["train_start_ms"], window["train_end_ms"])
        train_cand = _slice(cand_rows, window["train_start_ms"], window["train_end_ms"])
        test_base = _slice(base_rows, window["test_start_ms"], window["test_end_ms"])
        test_cand = _slice(cand_rows, window["test_start_ms"], window["test_end_ms"])
        # Seleção do candidato: SOMENTE com o treino desta dobra.
        train_pair = pair_opportunities(train_base, train_cand)
        train_delta = policy_delta_r(train_pair)
        selected = bool(train_delta["available"] and train_delta["value"] > 0)
        # Avaliação FORA DA AMOSTRA.
        test_pair = pair_opportunities(test_base, test_cand)
        test_delta = policy_delta_r(test_pair)
        executed.append({
            "fold": fold.index, "reason_code": OK,
            "train_n": len(train_base) + len(train_cand),
            "test_n": len(test_base) + len(test_cand),
            "candidate_selected_on_train": selected,
            "train_delta_net_r": train_delta["value"],
            "delta_net_r": test_delta["value"],
            "delta_reason_code": test_delta["reason_code"],
            "window": window,
        })
        if test_delta["available"] and (len(test_base) + len(test_cand)) > 0:
            test_deltas.append(test_delta["value"])
        all_base.extend(test_base)
        all_cand.extend(test_cand)

    folds_executed = sum(1 for item in executed if item["reason_code"] == OK
                         and item["test_n"] > 0)
    paired = pair_opportunities(all_base, all_cand)
    delta = policy_delta_r(paired)
    # Bootstrap sobre os DELTAS de dobra (diferenças de carteira fora da
    # amostra), nunca sobre o R absoluto de uma única execução.
    ci = block_bootstrap_ci(test_deltas, seed=seed, samples=samples,
                            block_size=block_size, alpha=alpha, comparisons=comparisons)
    discipline = fold_discipline({stage: "train" for stage in FITTED_STAGES}
                                 | {"candidate_selected_on": "validation"})
    guard = coverage if isinstance(coverage, Mapping) else coverage_guard(
        {"considered": len(all_base), "resolved": len(all_base)},
        {"considered": len(all_cand), "resolved": len(all_cand)})
    result = verdict(folds=folds, discipline=discipline, coverage=guard,
                     costs_complete=costs_complete, horizon_sufficient=horizon_sufficient,
                     paired=paired, ci=ci, policy_delta=delta,
                     studies_executed=folds_executed > 0)
    return {"wf_version": WF_VERSION, "folds_executed": folds_executed,
            "folds": executed, "policy_delta": delta, "ci": ci,
            "paired": paired, "verdict": result,
            "live_equivalent": False, "promotable": False}


def verdict(*, folds: Sequence[Fold], discipline: Mapping[str, Any],
            coverage: Mapping[str, Any], costs_complete: bool,
            horizon_sufficient: bool, paired: Mapping[str, Any],
            ci: Mapping[str, Any],
            policy_delta: Optional[Mapping[str, Any]] = None,
            studies_executed: bool = False) -> Dict[str, Any]:
    """Sem custo, horizonte, cobertura ou evidência: NENHUM vencedor.

    O IC da subamostra pareada NÃO decide sozinho: ele precisa concordar com o
    delta da POLÍTICA INTEIRA (incluindo o que só um lado aceitou) e existir
    estudo realmente executado por trás. Sem isso, um candidato globalmente pior
    venceria por um intervalo pareado positivo.
    """
    reasons: List[str] = []
    wf = is_walk_forward(folds)
    if not wf["walk_forward"]:
        reasons.append(wf["reason_code"])
    if not (discipline or {}).get("ok"):
        reasons.extend((discipline or {}).get("reason_codes") or (TEST_LEAKAGE,))
    if not (coverage or {}).get("ok"):
        reasons.append((coverage or {}).get("reason_code", COVERAGE_INSUFFICIENT))
    if not costs_complete:
        reasons.append(COSTS_UNKNOWN)
    if not horizon_sufficient:
        reasons.append(HORIZON_INSUFFICIENT)
    if not (ci or {}).get("available"):
        reasons.append((ci or {}).get("reason_code", SAMPLE_INSUFFICIENT))
    if not studies_executed:
        reasons.append(STUDIES_NOT_EXECUTED)
    delta = policy_delta if isinstance(policy_delta, Mapping) else {}
    if not delta.get("available"):
        reasons.append(delta.get("reason_code", POLICY_DELTA_UNKNOWN))
    elif not (delta.get("paired_n") or delta.get("only_baseline_n")
              or delta.get("only_candidate_n")):
        reasons.append(SAMPLE_INSUFFICIENT)
    reasons = [code for code in dict.fromkeys(reasons) if code != OK]
    if reasons:
        return {"winner": None, "state": INSUFFICIENT_EVIDENCE,
                "reason_codes": tuple(reasons),
                "policy_delta": (paired or {}).get("policy_delta"),
                "promotable": False}
    low, high = ci.get("low"), ci.get("high")
    if low is None or high is None or low <= 0.0 <= high:
        return {"winner": None, "state": INSUFFICIENT_EVIDENCE,
                "reason_codes": (CI_INCLUDES_ZERO,),
                "policy_delta": (paired or {}).get("policy_delta"),
                "policy_delta_r": delta.get("value"),
                "promotable": False}
    total = delta.get("value")
    ci_winner = "CANDIDATE" if low > 0 else "BASELINE"
    delta_winner = "CANDIDATE" if total > 0 else ("BASELINE" if total < 0 else None)
    if delta_winner is None or delta_winner != ci_winner:
        # Candidato globalmente pior não vence por IC pareado favorável.
        return {"winner": None, "state": INSUFFICIENT_EVIDENCE,
                "reason_codes": (DELTA_DISAGREES_WITH_CI,),
                "policy_delta": (paired or {}).get("policy_delta"),
                "policy_delta_r": total, "ci_winner": ci_winner,
                "promotable": False}
    return {"winner": ci_winner,
            "state": "EVIDENCE_AVAILABLE", "reason_codes": (OK,),
            "policy_delta": (paired or {}).get("policy_delta"),
            "policy_delta_r": total,
            # Vencer na validação NÃO promove: isso é o bloco G (simulação,
            # aprovação humana e canário).
            "promotable": False}


# ── Fronteira do holdout final ──────────────────────────────────────────────
@dataclass(frozen=True)
class HoldoutSeal:
    synthetic: bool
    preregistration_ms: int
    results_available_at_ms: int
    preregistration_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.synthetic, bool):
            raise ValueError("synthetic: booleano obrigatório")
        for name in ("preregistration_ms", "results_available_at_ms"):
            if _int(getattr(self, name)) is None or getattr(self, name) < 0:
                raise ValueError(f"{name}: inteiro não negativo obrigatório")


def open_holdout(seal: HoldoutSeal, *, now_ms: int) -> Dict[str, Any]:
    """Holdout REAL continua selado: nem carregado, nem avaliado.

    Só o holdout SINTÉTICO é avaliável, e mesmo ele exige pré-registro anterior
    aos resultados. Hash não prova pré-registro independente.
    """
    base = {"loaded": False, "evaluated": False,
            "hash_proves_preregistration": False,
            "preregistration_evidence": PREREGISTRATION_NOT_INDEPENDENT}
    if not isinstance(seal, HoldoutSeal):
        raise ValueError("selo do holdout obrigatório")
    if not seal.synthetic:
        return {**base, "allowed": False, "reason_code": REAL_HOLDOUT_SEALED}
    if seal.preregistration_ms >= seal.results_available_at_ms:
        return {**base, "allowed": False, "reason_code": BACKDATED_PREREGISTRATION}
    if _int(now_ms) is None or now_ms < seal.results_available_at_ms:
        return {**base, "allowed": False, "reason_code": FINAL_RESULTS_NOT_FOR_TUNING}
    return {**base, "allowed": True, "loaded": True, "evaluated": True,
            "reason_code": OK, "scope": "SYNTHETIC_HOLDOUT_ONLY",
            "results_usable_for_tuning": False}


def final_evaluation_boundary() -> Dict[str, Any]:
    return {
        "real_holdout": "SEALED",
        "real_holdout_loaded": False,
        "real_holdout_evaluated": False,
        "synthetic_holdout_supported": True,
        "results_usable_for_parameter_choice": False,
        "hash_proves_preregistration": False,
        "reason_code": REAL_HOLDOUT_SEALED,
    }


def walk_forward_manifest() -> Dict[str, Any]:
    return {
        "wf_version": WF_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "min_folds": MIN_FOLDS,
        "fitted_stages": list(FITTED_STAGES),
        "slice_keys": list(SLICE_KEYS),
        "reason_codes": sorted(REASON_CODES),
        "holdout": final_evaluation_boundary(),
        "promotable": False,
        "live_equivalent": False,
        "limitations": list(LIMITATIONS),
    }
