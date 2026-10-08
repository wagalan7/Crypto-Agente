"""Versioned, offline acceptance policy; never an order or LIVE licence.

The public hash is an integrity binding, NOT a signature. Full deterministic
recomputation is mandatory at official ingestion. Cheap verification is for
records read from that controlled store and then bound by human approvals.
No client-supplied receipt may become authority via cheap verification.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Mapping

POLICY_VERSION = "R13_V3_ACCEPTANCE_POLICY_V1"
ACCEPTANCE_CONTRACT = "R13_RESEARCH_ACCEPTANCE_V1"
PRODUCER_CONTRACT = "R13_OFFLINE_ACCEPTANCE_EVALUATOR_V1"
DAY_MS = 86_400_000
VALID_FOR_MS = 30 * DAY_MS
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
INVALID = "INVALID"
_MISSING_CI = object()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def _integer(value, minimum=0):
    return value if type(value) is int and value >= minimum else None


def _clock(value):
    return int(time.time() * 1000) if value is None else _integer(value, 1)


def _same_number(left, right):
    return (_number(left) is not None and _number(right) is not None
            and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12))


def _same_percent(left, right):
    # Prospective producer publishes certain rates rounded to six decimals.
    return (_number(left) is not None and _number(right) is not None
            and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-6))


def policy_manifest():
    """The frozen hypothesis and criteria, separate from all V1 artifacts."""
    from services import preselection_experiment_service as catalog
    from services import score_v3_service as score
    body = {"policy_version": POLICY_VERSION, "producer_contract": PRODUCER_CONTRACT,
        "hypothesis": {"comparison_scope": "SELECTION_ONLY", "isolated_change": "SCORE_MODEL",
            "model": score.SCORE_VERSION, "score_config": score.DEFAULT_CONFIG.as_dict(),
            "min_score": 70.0, "playbook": "TREND_PULLBACK", "event": "P_TP1_BEFORE_STOP"},
        "training": {"min_global": 200, "min_per_bin": 30, "bin_width": 10},
        "calibration": {"min_global_oos": 100, "min_per_authorized_bin": 30,
            "authorized_bins": [7, 8, 9], "min_coverage": 0.9, "folds": 4,
            "min_per_fold": 20, "max_ece": 0.05, "max_bin_error": 0.10,
            "max_fold_ece": 0.10, "min_nonnegative_fold_gains": 3,
            "brier_ci_lower_strictly_positive": True},
        "uncertainty": {"method": "UTC_DAY_BLOCK_BOOTSTRAP", "samples": 2000,
            "seed": 7, "confidence": 0.95, "quantile": "LINEAR_ORDER_STATISTIC",
            "resampling": "DAYS_WITH_REPLACEMENT_KEEP_ALL_ROWS"},
        "economics": {"criteria": catalog.GoNoGoCriteria().as_dict(),
            "criteria_hash": catalog.GoNoGoCriteria().criteria_hash(),
            "wf_winner": "CANDIDATE", "drawdown_not_worse_than_baseline": True,
            "min_baseline_trades_preserved": 0.70},
        "protection": {"scope": "SHADOW_SIMULATED", "purpose": "PROMOTION",
            "first_registration_only": True, "proves_real_sl": False,
            "authorizes_canary": False, "min_observation_coverage_pct": 90.0},
        "valid_for_ms": VALID_FOR_MS,
        "trust_boundary": "FULL_RECOMPUTATION_AT_OFFICIAL_INGESTION_THEN_CONTROLLED_STORE",
        "live_license": False}
    return {**body, "policy_hash": digest(body)}


class _Problem(ValueError):
    def __init__(self, reason, state=INVALID):
        self.reason, self.state = reason, state


def _component(state, reasons=(), *, checks=None, metrics=None):
    return {"state": state, "reason_codes": list(dict.fromkeys(reasons)),
            "checks": checks or {}, "metrics": metrics or {}}


def _outcome(checks, missing=()):
    failures = [name for name, passed in checks.items() if passed is not True]
    return _component(INSUFFICIENT if missing else REJECTED if failures else ACCEPTED,
                      [*missing, *failures], checks=checks)


def _context(report):
    from services import research_manifest_service as rm
    from services import research_study_service as study
    from services import score_v3_service as score
    from services import preselection_experiment_service as catalog
    from services import strategy_evidence_service as evidence
    if not isinstance(report, Mapping) or report.get("ok") is not True \
            or report.get("version") != study.STUDY_VERSION or report.get("kind") != study.PAYLOAD_KIND \
            or report.get("holdout_status") != "SEALED" or report.get("live_changed") is not False \
            or report.get("promotable") is not False:
        raise _Problem("ACCEPTANCE_STUDY_INVALID")
    verified = rm.verify_manifest(report.get("manifest"))
    if not verified["ok"]:
        raise _Problem("ACCEPTANCE_MANIFEST_DRIFT")
    manifest = verified["manifest"]
    identity = report.get("identity")
    if not isinstance(identity, Mapping):
        raise _Problem("ACCEPTANCE_STUDY_IDENTITY_INVALID")
    expected = {"manifest_hash": manifest["manifest_hash"],
        "dataset_hash": identity.get("dataset_hash"), "price_hash": identity.get("price_hash"),
        "calibration_request": identity.get("calibration_request"),
        "baseline": manifest["baseline"]["management_config"],
        "candidate": manifest["candidate"]["management_config"],
        "costs": manifest["costs"]["config"], "split": manifest["split"]}
    if digest(identity) != digest(expected) or report.get("study_key") != study.digest(expected) \
            or any(not isinstance(identity.get(key), str) or len(identity[key]) != 64
                   or any(c not in "0123456789abcdef" for c in identity[key])
                   for key in ("dataset_hash", "price_hash")) \
            or report.get("real_study_allowed") is not rm.authorized_comparison(manifest)["real_study_allowed"]:
        raise _Problem("ACCEPTANCE_STUDY_IDENTITY_INVALID")
    official = report.get("study")
    if not isinstance(official, Mapping):
        raise _Problem("ACCEPTANCE_OFFICIAL_STUDY_MISSING")
    contract = official.get("contract")
    if not isinstance(contract, Mapping):
        raise _Problem("ACCEPTANCE_CONTRACT_MISSING")
    envelope = catalog.build_preselection_envelope(replay_config=contract.get("candidate_config"),
        contract_hash=contract.get("contract_hash"), selection_config=contract.get("selection_config"))
    checked = evidence.verify_study_identity(official, candidate_config=envelope,
        fingerprint=identity["dataset_hash"], cutoff=datetime(1970, 1, 1, tzinfo=timezone.utc)
        + timedelta(milliseconds=manifest["split"]["as_of_ms"]))
    if not checked.get("ok") or official.get("evidence_key") != report["study_key"]:
        raise _Problem("ACCEPTANCE_OFFICIAL_STUDY_IDENTITY_INVALID")
    candidate = manifest["candidate"]
    fingerprint = score.model_fingerprint(playbook=candidate["selection_rule"]["playbook"],
                                          config=score.ScoreConfig(**candidate["score_config"]))
    return manifest, identity, contract, fingerprint


def _hypothesis(manifest, request):
    from services import score_v3_service as score
    from services import research_manifest_service as rm
    from services import research_study_service as study
    c, b = manifest["candidate"], manifest["baseline"]
    if (manifest["comparison_scope"] != "SELECTION_ONLY"
            or manifest["isolated_change"]["component"] != "SCORE_MODEL"
            or c["score_version"] != score.SCORE_VERSION
            or c["score_config"] != score.DEFAULT_CONFIG.as_dict()
            or c["score_config"].get("include_composite_confluence") is not False
            or not _same_number(c["score_config"].get("min_evidence_fraction"), 0.6)
            or c["selection_rule"]["kind"] != rm.RULE_SCORE_V3_MIN
            or not _same_number(c["selection_rule"]["min_score"], 70.0)
            or c["selection_rule"]["playbook"] != "TREND_PULLBACK"
            or any(c[k] != b[k] for k in ("core_version", "core_config_hash", "management_config", "playbooks"))):
        raise _Problem("ACCEPTANCE_HYPOTHESIS_NOT_APPROVED")
    if request is None:
        raise _Problem("ACCEPTANCE_CALIBRATION_REQUEST_MISSING", INSUFFICIENT)
    try:
        expected = study.calibration_request(event=request["event"], valid_for_ms=request["valid_for_ms"])
    except (KeyError, TypeError, ValueError):
        raise _Problem("ACCEPTANCE_CALIBRATION_REQUEST_INVALID")
    if request != expected or request["event"] != "P_TP1_BEFORE_STOP" \
            or request["valid_for_ms"] > VALID_FOR_MS:
        raise _Problem("ACCEPTANCE_EVENT_OR_VALIDITY_NOT_APPROVED")


def _reliability(rows):
    from services import score_v3_calibration_service as calib
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["bin"]].append(row)
    bins = []
    for index, items in sorted(grouped.items()):
        n, successes = len(items), sum(row["label"] is True for row in items)
        prediction = sum(row["prediction"] for row in items) / n
        interval = calib.wilson_interval(successes, n)
        bins.append({"bin": index, "n": n, "successes": successes,
            "predicted": prediction, "observed": successes / n,
            "error": abs(prediction - successes / n),
            "wilson_low": interval[0], "wilson_high": interval[1]})
    return bins, sum(b["n"] * b["error"] for b in bins) / len(rows) if rows else None


def _brier(rows, field):
    return sum((r[field] - int(r["label"])) ** 2 for r in rows) / len(rows) if rows else None


def _bootstrap(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["decision_ts_ms"] // DAY_MS].append(
            (row["constant_prediction"] - int(row["label"])) ** 2
            - (row["prediction"] - int(row["label"])) ** 2)
    blocks = [(sum(grouped[d]), len(grouped[d])) for d in sorted(grouped)]
    recipe = policy_manifest()["uncertainty"]
    if len(blocks) < 2:
        return {**recipe, "available": False, "reason_code": "ACCEPTANCE_BOOTSTRAP_BLOCKS_INSUFFICIENT",
                "blocks": len(blocks), "low": None, "high": None, "point": None}
    rng, draws = random.Random(7), []
    for _ in range(2000):
        selected = [blocks[rng.randrange(len(blocks))] for _ in blocks]
        draws.append(sum(s for s, _ in selected) / sum(n for _, n in selected))
    draws.sort()
    def quantile(q):
        position = q * (len(draws) - 1)
        lo, hi = math.floor(position), math.ceil(position)
        return draws[lo] + (draws[hi] - draws[lo]) * (position - lo)
    return {**recipe, "available": True, "reason_code": "OK", "blocks": len(blocks),
            "low": quantile(0.025), "high": quantile(0.975),
            "point": sum(s for s, _ in blocks) / sum(n for _, n in blocks)}


def _ci_contract(ci, rows):
    recipe = policy_manifest()["uncertainty"]
    blocks = len({r["decision_ts_ms"] // DAY_MS for r in rows})
    if not isinstance(ci, Mapping) or set(ci) != set(recipe) | {
            "available", "reason_code", "blocks", "low", "high", "point"} \
            or any(ci.get(k) != v or type(ci.get(k)) is not type(v) for k, v in recipe.items()) \
            or _integer(ci.get("blocks")) != blocks:
        raise _Problem("ACCEPTANCE_BOOTSTRAP_CONTRACT_INVALID")
    if blocks < 2:
        if ci.get("available") is not False or any(ci.get(k) is not None for k in ("low", "high", "point")) \
                or ci.get("reason_code") != "ACCEPTANCE_BOOTSTRAP_BLOCKS_INSUFFICIENT":
            raise _Problem("ACCEPTANCE_BOOTSTRAP_CONTRACT_INVALID")
        return ci
    point = _brier(rows, "constant_prediction") - _brier(rows, "prediction")
    if ci.get("available") is not True or ci.get("reason_code") != "OK" \
            or not _same_number(ci.get("point"), point) \
            or _number(ci.get("low")) is None or _number(ci.get("high")) is None \
            or not -1 <= ci["low"] <= ci["high"] <= 1:
        raise _Problem("ACCEPTANCE_BOOTSTRAP_CONTRACT_INVALID")
    return dict(ci)


def _calibration(report, context, now, supplied_ci=None):
    from services import score_v3_calibration_service as calib
    from services import research_study_service as study
    manifest, identity, _, fingerprint = context
    request = identity["calibration_request"]
    _hypothesis(manifest, request)
    calibration = report.get("calibration")
    if not isinstance(calibration, Mapping) or report.get("artifact") != calibration.get("artifact"):
        raise _Problem("ACCEPTANCE_CALIBRATION_IDENTITY_INVALID")
    folds = calibration.get("folds")
    population = calibration.get("acceptance_population")
    if not isinstance(folds, list) or len(folds) < 4 or not isinstance(population, Mapping):
        raise _Problem("ACCEPTANCE_FOLDS_OR_POPULATION_MISSING", INSUFFICIENT)
    if len(folds) != 4:
        raise _Problem("ACCEPTANCE_FOLDS_INVALID")
    complete_denominator = population.get("export_denominator_complete")
    if complete_denominator is not None and type(complete_denominator) is not bool:
        raise _Problem("ACCEPTANCE_EXPORT_DENOMINATOR_INVALID")
    if complete_denominator is not True:
        raise _Problem("ACCEPTANCE_EXPORT_DENOMINATOR_UNRECONCILED", INSUFFICIENT)
    split, management = manifest["split"], manifest["candidate"]["management_config"]
    stop = min(split["holdout_start_ms"], split["as_of_ms"])
    width = (stop - split["validation_start_ms"]) // 4
    if width <= 0:
        raise _Problem("ACCEPTANCE_FOLD_CHRONOLOGY_INVALID")
    counts, keys = [0] * 4, set()
    index = population.get("index")
    if not isinstance(index, list):
        raise _Problem("ACCEPTANCE_POPULATION_INDEX_MISSING", INSUFFICIENT)
    for item in index:
        if not isinstance(item, Mapping) or set(item) != {"opportunity_key", "decision_ts_ms"}:
            raise _Problem("ACCEPTANCE_POPULATION_INDEX_INVALID")
        key, stamp = item["opportunity_key"], _integer(item["decision_ts_ms"], 1)
        if not isinstance(key, str) or not key or key in keys or stamp is None \
                or not split["train_start_ms"] <= stamp < stop:
            raise _Problem("ACCEPTANCE_POPULATION_INDEX_INVALID")
        keys.add(key)
        if stamp >= split["validation_start_ms"]:
            counts[min(3, (stamp - split["validation_start_ms"]) // width)] += 1
    if population.get("contract") != "R13_ACCEPTANCE_POPULATION_V1" \
            or population.get("fold_eligible_counts") != counts \
            or _integer(population.get("eligible_count")) != sum(counts) \
            or _integer(population.get("source_rows")) != len(index) \
            or _integer(population.get("replay_rows")) is None \
            or _integer(population.get("excluded_by_selection")) is None \
            or population["replay_rows"] + population["excluded_by_selection"] != sum(counts):
        raise _Problem("ACCEPTANCE_POPULATION_DENOMINATOR_INVALID")
    selected = report.get("selection") or {}
    if (selected.get("coverage") or {}).get("rows_total") != len(index):
        raise _Problem("ACCEPTANCE_POPULATION_SELECTION_MISMATCH")
    definition = {"event": request["event"], "population": "RESEARCH_SHADOW",
        "horizon_bars": management["entry_window_bars"] + management["max_holding_bars"] - 1,
        "bar_ms": management["bar_ms"], "censoring": request["censoring"],
        "payoff_ref": management["config_hash"], "interchangeable_with_other_events": False}
    versions = {"study": study.STUDY_VERSION, "manifest": manifest["manifest_hash"],
                "prices": identity["price_hash"], "costs": manifest["costs"]["config"]["config_hash"]}
    all_rows, fold_metrics, seen = [], [], set()
    index_by_key = {r["opportunity_key"]: r["decision_ts_ms"] for r in index}
    for idx, fold in enumerate(folds):
        lo, hi = split["validation_start_ms"] + idx * width, stop if idx == 3 else split["validation_start_ms"] + (idx + 1) * width
        cut = lo - (split["purge_bars"] + split["embargo_bars"]) * management["bar_ms"]
        if not isinstance(fold, Mapping) or _integer(fold.get("fold")) != idx \
                or any(fold.get(k) != expected for k, expected in (
                    ("train_cutoff_ms", cut), ("oos_start_ms", lo), ("oos_end_ms", hi))):
            raise _Problem("ACCEPTANCE_FOLD_CHRONOLOGY_INVALID")
        artifact = fold.get("artifact")
        if artifact is None or artifact.get("reason_code") == calib.SAMPLE_INSUFFICIENT:
            raise _Problem("ACCEPTANCE_TRAIN_SAMPLE_INSUFFICIENT", INSUFFICIENT)
        checked = calib.verify_artifact(artifact, now_ms=now, model_fingerprint=fingerprint,
            score_config_hash=fingerprint, population="RESEARCH_SHADOW", event=request["event"],
            dataset_hash=identity["dataset_hash"], expected_event_definition=definition)
        if not checked.get("ok"):
            raise _Problem(checked["reason_code"])
        if artifact["state"] != calib.STATE_OOS_VALIDATED:
            raise _Problem("ACCEPTANCE_FOLD_NOT_EVALUATED", INSUFFICIENT)
        if artifact["versions"] != versions or artifact["training"]["cutoff_ms"] != cut \
                or artifact["generation"]["generated_at_ms"] != report["observed_at_ms"] \
                or artifact["generation"]["valid_until_ms"] != report["observed_at_ms"] + request["valid_for_ms"] \
                or artifact["source"] != "R10A_REPLAY_FROM_OFFICIAL_EXPORT":
            raise _Problem("ACCEPTANCE_FOLD_ARTIFACT_IDENTITY_INVALID")
        inputs = fold.get("acceptance_inputs")
        if not isinstance(inputs, Mapping):
            raise _Problem("ACCEPTANCE_FOLD_INPUTS_MISSING", INSUFFICIENT)
        if set(inputs) != {"eligible_count", "oos_rows"} \
                or _integer(inputs.get("eligible_count")) != counts[idx] \
                or not isinstance(inputs.get("oos_rows"), list):
            raise _Problem("ACCEPTANCE_FOLD_INPUTS_INVALID")
        rows = inputs["oos_rows"]
        train_n = sum(b["n"] for b in artifact["bins"])
        constant = sum(b["successes"] for b in artifact["bins"]) / train_n
        train_keys = set(artifact["training"]["opportunity_keys"])
        # Um hash refeito não prova que o treino veio desta população. O
        # mesmo índice sem outcomes que vincula OOS também delimita os IDs,
        # instantes e extremos do treino de cada dobra, antes de seu corte.
        train_stamps = [index_by_key.get(key) for key in train_keys]
        if any(_integer(stamp, 1) is None or not split["train_start_ms"] <= stamp < cut
               for stamp in train_stamps) \
                or artifact["training"]["decision_min_ms"] != min(train_stamps) \
                or artifact["training"]["decision_max_ms"] != max(train_stamps):
            raise _Problem("ACCEPTANCE_TRAIN_IDENTITY_OR_LEAKAGE_INVALID")
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {"opportunity_key", "score", "label", "prediction",
                    "constant_prediction", "bin", "decision_ts_ms", "label_available_ts_ms"}:
                raise _Problem("ACCEPTANCE_OOS_ROW_INVALID")
            key, stamp, available = row["opportunity_key"], _integer(row["decision_ts_ms"], 1), _integer(row["label_available_ts_ms"], 1)
            index_bin = calib.bin_index(row["score"])
            if type(row["label"]) is not bool or stamp is None or available is None \
                    or index_bin is None or _integer(row["bin"]) != index_bin \
                    or not isinstance(key, str) or not key or key in seen or key in train_keys \
                    or index_by_key.get(key) != stamp or not cut < lo <= stamp < hi \
                    or not stamp <= available <= min(hi, now) \
                    or available > ((stamp + management["bar_ms"] - 1) // management["bar_ms"]) * management["bar_ms"] \
                        + definition["horizon_bars"] * management["bar_ms"]:
                raise _Problem("ACCEPTANCE_OOS_IDENTITY_OR_LEAKAGE_INVALID")
            b = artifact["bins"][index_bin]
            if b["supported"] is not True or not _same_number(row["prediction"], b["p"]) \
                    or not _same_number(row["constant_prediction"], constant):
                raise _Problem("ACCEPTANCE_PREDICTION_OR_TRAIN_REFERENCE_INVALID")
            seen.add(key)
        oos = artifact["metrics"]["oos"]
        bins, ece = _reliability(rows)
        brier, reference = _brier(rows, "prediction"), _brier(rows, "constant_prediction")
        if len(rows) > counts[idx] or oos["predictions"] != len(rows) \
                or oos["opportunity_keys"] != sorted(r["opportunity_key"] for r in rows) \
                or not _same_number(oos["brier"], brier) \
                or oos["decision_min_ms"] != min((r["decision_ts_ms"] for r in rows), default=None) \
                or oos["label_max_ms"] != max((r["label_available_ts_ms"] for r in rows), default=None):
            raise _Problem("ACCEPTANCE_ARTIFACT_OOS_METRICS_MISMATCH")
        if len(bins) != len(oos["reliability"]):
            raise _Problem("ACCEPTANCE_ARTIFACT_OOS_METRICS_MISMATCH")
        for actual, expected in zip(oos["reliability"], bins):
            if any(actual.get(k) != expected[k] for k in ("bin", "n", "successes")) \
                    or any(not _same_number(actual.get(k), expected[k]) for k in
                           ("predicted", "observed", "wilson_low", "wilson_high")):
                raise _Problem("ACCEPTANCE_ARTIFACT_OOS_METRICS_MISMATCH")
        fold_metrics.append({"fold": idx, "n": len(rows), "eligible_count": counts[idx],
            "ece": ece, "brier": brier, "constant_brier": reference,
            "brier_gain": reference - brier if brier is not None else None,
            "artifact_hash": artifact["artifact_hash"]})
        all_rows.extend(rows)
    last = report.get("artifact")
    if last != folds[-1]["artifact"]:
        raise _Problem("ACCEPTANCE_SERVED_ARTIFACT_NOT_LAST_FOLD")
    bins, ece = _reliability(all_rows)
    authorized = [b for b in bins if b["bin"] >= 7]
    coverage = len(all_rows) / sum(counts) if sum(counts) else None
    ci = _bootstrap(all_rows) if supplied_ci is None else _ci_contract(supplied_ci, all_rows)
    checks = {"OOS_GLOBAL_100": len(all_rows) >= 100,
        "OOS_AUTHORIZED_BIN_SUPPORT_30": len(authorized) == 3 and all(b["n"] >= 30 for b in authorized),
        "SERVED_TRAIN_AUTHORIZED_BIN_SUPPORT_30": all(last["bins"][idx]["supported"] is True for idx in (7, 8, 9)),
        "COVERAGE_90": coverage is not None and coverage >= 0.9,
        "FOUR_FOLDS_MIN_20": all(f["n"] >= 20 for f in fold_metrics),
        "ECE_MAX_005": ece is not None and ece <= 0.05 + 1e-12,
        "BIN_ERROR_MAX_010": all(b["error"] <= 0.10 + 1e-12 for b in authorized),
        "BRIER_CI_LOW_POSITIVE": ci["available"] is True and ci["low"] > 0,
        "THREE_FOLD_GAINS_NONNEGATIVE": sum(f["brier_gain"] is not None and f["brier_gain"] >= -1e-12 for f in fold_metrics) >= 3,
        "FOLD_ECE_MAX_010": all(f["ece"] is not None and f["ece"] <= 0.10 + 1e-12 for f in fold_metrics)}
    missing = [k for k in ("OOS_GLOBAL_100", "OOS_AUTHORIZED_BIN_SUPPORT_30", "SERVED_TRAIN_AUTHORIZED_BIN_SUPPORT_30",
                           "COVERAGE_90", "FOUR_FOLDS_MIN_20") if not checks[k]]
    if not ci["available"]:
        missing.append(ci["reason_code"])
    result = _outcome(checks, missing)
    result["metrics"] = {"oos_unique": len(all_rows), "eligible_count": sum(counts), "coverage": coverage,
        "ece": ece, "bins": bins, "folds": fold_metrics, "brier": _brier(all_rows, "prediction"),
        "constant_brier": _brier(all_rows, "constant_prediction"), "brier_gain_ci": ci,
        "dependence_sensitivity": {"utc_day_blocks": ci["blocks"],
                                  "cross_symbol_independence_assumed": False}}
    return result


def _replay_metrics(replay):
    from services import portfolio_replay_service as portfolio
    if not isinstance(replay, Mapping) or not isinstance(replay.get("trades"), list):
        raise _Problem("ACCEPTANCE_REPLAY_MISSING", INSUFFICIENT)
    admitted, values, keys = [], [], set()
    for trade in replay["trades"]:
        if not isinstance(trade, Mapping) or type(trade.get("admitted")) is not bool:
            raise _Problem("ACCEPTANCE_REPLAY_ROW_INVALID")
        if not trade["admitted"]:
            continue
        key = trade.get("opportunity_id")
        if not isinstance(key, str) or not key or key in keys:
            raise _Problem("ACCEPTANCE_REPLAY_DUPLICATE_OR_IDENTITY_INVALID")
        keys.add(key)
        admitted.append(trade)
        net = trade.get("net_r")
        if net is not None and _number(net) is None:
            raise _Problem("ACCEPTANCE_REPLAY_NUMERIC_INVALID")
        if net is not None:
            values.append(net)
    metrics = replay.get("metrics")
    expected = portfolio.portfolio_metrics(values)
    if not isinstance(metrics, Mapping) or _integer(replay.get("admitted")) != len(admitted):
        raise _Problem("ACCEPTANCE_REPLAY_METRICS_INVALID")
    for key in ("resolved_n", "net_total_r", "net_expectancy_r", "max_drawdown_r"):
        if expected[key] is None:
            if metrics.get(key) is not None:
                raise _Problem("ACCEPTANCE_REPLAY_METRICS_INVALID")
        elif not _same_number(metrics.get(key), expected[key]):
            raise _Problem("ACCEPTANCE_REPLAY_METRICS_INVALID")
    return admitted, metrics


def _prospective(snapshot, report, context, now):
    """Validate the official-store cohort snapshot, not an arbitrary metric dict.

    Governance owns reconciliation with annotation rows and the active start;
    this pure layer binds its frozen result and verifies numeric semantics.
    """
    from services import preselection_experiment_service as catalog
    from services import strategy_evidence_service as evidence_service
    if snapshot is None:
        return None
    keys = {"version", "source", "available", "identity", "cutoff_ms", "cohort_hash",
            "cohort_commitments", "evidence", "gate", "measurements", "data_quality", "snapshot_hash"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != keys \
            or snapshot.get("version") != "R13_ACCEPTANCE_PROSPECTIVE_V2" \
            or snapshot.get("source") != "REAL_PROSPECTIVE" or snapshot.get("available") is not True \
            or digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"}) != snapshot.get("snapshot_hash"):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_SNAPSHOT_INVALID")
    identity = snapshot.get("identity")
    fields = {"experiment_id", "experiment_key", "candidate_hash", "champion_hash", "generation",
              "approval_id", "started_at_ms", "manifest_hash", "contract_hash", "study_key", "bundle_hash"}
    if not isinstance(identity, Mapping) or set(identity) != fields \
            or _integer(identity.get("experiment_id"), 1) is None or _integer(identity.get("generation")) is None \
            or not isinstance(identity.get("experiment_key"), str) or not identity["experiment_key"] \
            or _integer(identity.get("started_at_ms"), 1) is None:
        raise _Problem("ACCEPTANCE_PROSPECTIVE_IDENTITY_INVALID")
    for field in ("candidate_hash", "champion_hash", "approval_id", "manifest_hash", "contract_hash", "study_key", "bundle_hash"):
        value = identity.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise _Problem("ACCEPTANCE_PROSPECTIVE_IDENTITY_INVALID")
    manifest, _, contract, _ = context
    envelope = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
        contract_hash=contract["contract_hash"], selection_config=contract.get("selection_config"))
    if identity["candidate_hash"] != evidence_service.canonical_hash(envelope) \
            or identity["champion_hash"] != evidence_service.canonical_hash(contract["baseline_config"]) \
            or identity["manifest_hash"] != manifest["manifest_hash"] \
            or identity["contract_hash"] != contract["contract_hash"] or identity["study_key"] != report["study_key"]:
        raise _Problem("ACCEPTANCE_PROSPECTIVE_IDENTITY_DRIFT")
    cohort_hash, cutoff = snapshot.get("cohort_hash"), _integer(snapshot.get("cutoff_ms"), 1)
    measured, quality, evidence = snapshot.get("measurements"), snapshot.get("data_quality"), snapshot.get("evidence")
    if not isinstance(cohort_hash, str) or len(cohort_hash) != 64 or any(c not in "0123456789abcdef" for c in cohort_hash) \
            or cutoff is None or not identity["started_at_ms"] <= cutoff <= now \
            or not isinstance(measured, Mapping) or measured.get("source") != "PROSPECTIVE_ANNOTATIONS_OFFICIAL_RESOLVER" \
            or measured.get("measured_until_ms") != cutoff or not isinstance(quality, Mapping) \
            or not isinstance(evidence, Mapping) or not isinstance(snapshot.get("gate"), Mapping):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_SOURCE_INVALID")
    raw, valid, excluded = _integer(quality.get("raw")), _integer(quality.get("valid")), quality.get("excluded_by_reason")
    if set(quality) != {"raw", "valid", "excluded_by_reason"} or raw is None or valid is None or valid > raw \
            or not isinstance(excluded, Mapping) or any(not isinstance(k, str) or _integer(v) is None for k, v in excluded.items()) \
            or raw != valid + sum(excluded.values()):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_COVERAGE_INVALID")
    commitments = snapshot.get("cohort_commitments")
    if not isinstance(commitments, list) or len(commitments) != raw or len(commitments) > 5000:
        raise _Problem("ACCEPTANCE_PROSPECTIVE_COMMITMENTS_INVALID")
    for item in commitments:
        if not isinstance(item, Mapping) or set(item) != {
                "opportunity_key", "annotation_hash", "observation_hash", "included"} \
                or not isinstance(item.get("opportunity_key"), str) or not item["opportunity_key"] \
                or type(item.get("included")) is not bool \
                or any(not isinstance(item.get(k), str) or len(item[k]) != 64
                       or any(c not in "0123456789abcdef" for c in item[k])
                       for k in ("annotation_hash", "observation_hash")):
            raise _Problem("ACCEPTANCE_PROSPECTIVE_COMMITMENTS_INVALID")
    committed_keys = [item["opportunity_key"] for item in commitments]
    if len(set(committed_keys)) != raw or committed_keys != sorted(committed_keys) \
            or sum(item["included"] is True for item in commitments) != valid \
            or digest(commitments) != cohort_hash:
        raise _Problem("ACCEPTANCE_PROSPECTIVE_COMMITMENTS_INVALID")
    coverage = 100 * valid / raw if raw else None
    if (coverage is None and evidence.get("coverage_pct") is not None) \
            or (coverage is not None and not _same_number(evidence.get("coverage_pct"), coverage)):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_COVERAGE_INVALID")
    gate = catalog.go_no_go(evidence)
    if digest(snapshot["gate"]) != digest(gate):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_GATE_MISMATCH")
    for field in ("operational_failures", "economic_duplicates", "unresolved_protection_failures"):
        if evidence.get(field) != measured.get(field) \
                or (evidence.get(field) is not None and _integer(evidence[field]) is None):
            raise _Problem("ACCEPTANCE_PROSPECTIVE_MEASUREMENT_MISMATCH")
    if measured.get("economic_duplicates") is not None \
            and measured["economic_duplicates"] != excluded.get("DUPLICATE_OPPORTUNITY", 0):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_MEASUREMENT_MISMATCH")
    for field in ("fidelity_discrepancy_pct", "protection_coverage_pct", "fidelity_coverage_pct"):
        if measured.get(field) is not None and _number(measured[field]) is None:
            raise _Problem("ACCEPTANCE_PROSPECTIVE_NUMERIC_INVALID")
    for field in ("fidelity_comparable", "fidelity_denominator", "fidelity_divergences",
                  "fidelity_unknown", "fidelity_observed", "protection_applicable",
                  "protection_observed", "protection_pending"):
        if measured.get(field) is not None and _integer(measured[field]) is None:
            raise _Problem("ACCEPTANCE_PROSPECTIVE_NUMERIC_INVALID")
    if evidence.get("fidelity_discrepancy_pct") != measured.get("fidelity_discrepancy_pct"):
        raise _Problem("ACCEPTANCE_PROSPECTIVE_MEASUREMENT_MISMATCH")
    return snapshot


def _economic_temporal(report, manifest, base, cand, *, recompute):
    """Reconcile the executed full policy from raw trades without a replay.

    Only offline construction/full ingestion calculates the economic bootstrap;
    controlled-store reads check its frozen recipe and linear sufficient stats.
    """
    from services import research_study_service as study
    from services import walk_forward_service as wf
    protocol = report.get("evaluation_protocol")
    if not isinstance(protocol, Mapping):
        raise _Problem("ACCEPTANCE_ECONOMIC_PROTOCOL_MISSING", INSUFFICIENT)
    expected_protocol = study._evaluation_protocol(manifest)
    if digest(protocol) != digest(expected_protocol):
        raise _Problem("ACCEPTANCE_ECONOMIC_PROTOCOL_DRIFT")
    index = ((report.get("calibration") or {}).get("acceptance_population") or {}).get("index")
    if not isinstance(index, list):
        raise _Problem("ACCEPTANCE_ECONOMIC_TEMPORAL_INDEX_MISSING", INSUFFICIENT)
    timestamps = {r["opportunity_key"]: r["decision_ts_ms"] for r in index}
    labels = {}
    for side, trades in (("baseline", base), ("candidate", cand)):
        rows = []
        for trade in trades:
            key = trade["opportunity_id"]
            if key not in timestamps or _integer(timestamps[key], 1) is None:
                raise _Problem("ACCEPTANCE_ECONOMIC_TEMPORAL_IDENTITY_INVALID")
            available = trade.get("result_available_ts_ms")
            if trade.get("net_r") is not None and available is None:
                raise _Problem("ACCEPTANCE_ECONOMIC_RESULT_TIME_MISSING", INSUFFICIENT)
            if available is not None and (_integer(available, 1) is None
                    or not timestamps[key] <= available <= report["observed_at_ms"]):
                raise _Problem("ACCEPTANCE_ECONOMIC_RESULT_CHRONOLOGY_INVALID")
            rows.append({"opportunity_id": key, "decision_ts_ms": timestamps[key],
                "net_r": trade.get("net_r"), "result_available_ts_ms": trade.get("result_available_ts_ms")})
        labels[side] = wf._rows_by_time(rows)
    management = manifest["candidate"]["management_config"]
    horizon = max(manifest[s]["management_config"]["entry_window_bars"]
        + manifest[s]["management_config"]["max_holding_bars"] - 1 for s in ("baseline", "candidate")) + manifest["split"]["purge_bars"]
    expected_folds, test_deltas, all_base, all_evaluated = [], [], [], []
    for definition in protocol["economic_fold_plan"]:
        fold = wf.Fold(**definition)
        window = wf.apply_purge_embargo(fold, horizon_bars=horizon, bar_ms=management["bar_ms"],
                                       embargo_bars=manifest["split"]["embargo_bars"])
        if not window["usable"]:
            expected_folds.append({"fold": fold.index, "reason_code": window["reason_code"],
                                   "train_n": 0, "test_n": 0, "delta_net_r": None})
            continue
        def sliced(rows, start, end):
            return [r for r in rows if start <= r["decision_ts_ms"] < end]
        train_window = {s: sliced(labels[s], window["train_start_ms"], window["train_end_ms"]) for s in labels}
        tests = {s: sliced(labels[s], window["test_start_ms"], window["test_end_ms"]) for s in labels}
        prepared = {s: wf.split_train_labels(train_window[s], cut_ms=window["train_end_ms"]) for s in labels}
        reasons = sorted(set(prepared["baseline"]["reason_codes"]) | set(prepared["candidate"]["reason_codes"]))
        observed = wf.observed_horizon_bars(train_window["baseline"] + train_window["candidate"]
            + tests["baseline"] + tests["candidate"], bar_ms=management["bar_ms"])
        if observed is not None and observed > horizon:
            reasons = sorted(set(reasons) | {wf.HORIZON_BELOW_OBSERVED})
        train_delta = wf.policy_delta_r(wf.pair_opportunities(prepared["baseline"]["rows"], prepared["candidate"]["rows"]))
        selected = bool(train_delta["available"] and train_delta["value"] > 0
                        and (prepared["baseline"]["available"] or prepared["candidate"]["available"]))
        evaluated = tests["candidate"] if selected else tests["baseline"]
        test_delta = wf.policy_delta_r(wf.pair_opportunities(tests["baseline"], evaluated))
        expected_folds.append({"fold": fold.index, "reason_code": "OK",
            "train_n": len(prepared["baseline"]["rows"]) + len(prepared["candidate"]["rows"]),
            "test_n": len(tests["baseline"]) + len(tests["candidate"]),
            "candidate_selected_on_train": selected,
            "evaluated_policy": wf.POLICY_CANDIDATE if selected else wf.POLICY_BASELINE_FALLBACK,
            "fallback_contract": wf.POLICY_BASELINE_FALLBACK,
            "candidate_test_n": len(tests["candidate"]),
            "train_labels_available": prepared["baseline"]["available"] + prepared["candidate"]["available"],
            "train_labels_withheld": prepared["baseline"]["withheld"] + prepared["candidate"]["withheld"],
            "train_reason_codes": reasons, "observed_horizon_bars": observed,
            "train_delta_net_r": train_delta["value"], "delta_net_r": test_delta["value"],
            "delta_reason_code": test_delta["reason_code"], "window": window})
        if test_delta["available"] and len(tests["baseline"]) + len(tests["candidate"]) > 0:
            test_deltas.append(test_delta["value"])
        all_base.extend(tests["baseline"])
        all_evaluated.extend(evaluated)
    walk = report["walk_forward"]
    paired = wf.pair_opportunities(all_base, all_evaluated)
    expected_stages = {stage: wf.STAGE_NOT_APPLICABLE for stage in wf.FITTED_STAGES}
    expected_stages["candidate_selection"] = wf.STAGE_EXECUTED_ON_TRAIN
    if digest(walk["folds"]) != digest(expected_folds) or digest(walk["paired"]) != digest(paired) \
            or walk.get("folds_executed") != sum(f["reason_code"] == "OK" and f["test_n"] > 0 for f in expected_folds) \
            or walk.get("folds_running_candidate") != sum(f.get("evaluated_policy") == wf.POLICY_CANDIDATE for f in expected_folds) \
            or walk.get("stages") != expected_stages:
        raise _Problem("ACCEPTANCE_ECONOMIC_EXECUTION_MISMATCH")
    ci = walk["ci"]
    recipe = protocol["economic_bootstrap"]
    if recompute:
        expected_ci = wf.block_bootstrap_ci(test_deltas, **recipe)
        if digest(ci) != digest(expected_ci):
            raise _Problem("ACCEPTANCE_ECONOMIC_CI_RECOMPUTE_MISMATCH")
    elif ci.get("available") is True:
        if any(ci.get(k) != recipe[k] for k in ("samples", "block_size", "alpha", "comparisons")) \
                or ci.get("method") != "BONFERRONI_ON_ALPHA" \
                or not _same_number(ci.get("adjusted_alpha"), recipe["alpha"] / recipe["comparisons"]) \
                or not test_deltas or not _same_number(ci.get("mean"), sum(test_deltas) / len(test_deltas)):
            raise _Problem("ACCEPTANCE_ECONOMIC_CI_CONTRACT_INVALID")


def _economics(report, context, prospective=None, protection=None, *, recompute=True):
    from services import preselection_experiment_service as catalog
    from services import walk_forward_service as wf
    from services import portfolio_replay_service as portfolio
    from services import offline_replay_service as replay
    manifest, _, _, _ = context
    evidence = report["study"].get("evidence")
    if not isinstance(evidence, Mapping):
        raise _Problem("ACCEPTANCE_ECONOMIC_EVIDENCE_MISSING", INSUFFICIENT)
    evidence = dict(evidence)
    if report.get("real_study_allowed") is False:
        # Identity-only marker: TEST_ONLY can satisfy analytical thresholds,
        # but strict operational verification always refuses this population.
        evidence["essential_gaps"] = [g for g in evidence.get("essential_gaps", ())
                                      if g != "TEST_ONLY_NOT_REAL_EVIDENCE"]
    if prospective is not None:
        # Statistics of the offline full-management policy stay offline. Only
        # controls that require actual forward observation come from the
        # independently reconciled official cohort; neither cohort is summed.
        measured, forward = prospective["measurements"], prospective["evidence"]
        for field in ("operational_failures", "economic_duplicates", "unresolved_protection_failures", "fidelity_discrepancy_pct"):
            evidence[field] = measured.get(field)
        for field in ("fidelity_comparable", "fidelity_denominator", "fidelity_divergences", "fidelity_coverage_pct"):
            evidence[field] = measured.get(field)
        evidence["operational_applicable"] = prospective["data_quality"]["raw"]
        evidence["operational_observed"] = prospective["data_quality"]["valid"]
        evidence["operational_coverage_pct"] = forward.get("coverage_pct")
        evidence["essential_gaps"] = list(dict.fromkeys([*evidence.get("essential_gaps", ()), *forward.get("essential_gaps", ())]))
        if protection is not None and protection["state"] == ACCEPTED:
            evidence["essential_gaps"] = [g for g in evidence["essential_gaps"] if g != "BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED"]
    count_fields = ("total_shadow_trades", "business_days", "operational_failures",
                    "economic_duplicates", "unresolved_protection_failures")
    numeric_fields = ("calendar_days", "coverage_pct", "net_ev_r", "uncertainty_r",
                      "drawdown_r", "stability_ratio", "fidelity_discrepancy_pct")
    for key in count_fields:
        if evidence.get(key) is not None and _integer(evidence[key]) is None:
            raise _Problem("ACCEPTANCE_ECONOMIC_NUMERIC_INVALID")
    for key in numeric_fields:
        if evidence.get(key) is not None and _number(evidence[key]) is None:
            raise _Problem("ACCEPTANCE_ECONOMIC_NUMERIC_INVALID")
    per = evidence.get("trades_per_playbook")
    if per is not None and (not isinstance(per, Mapping) or any(
            not isinstance(k, str) or _integer(v) is None for k, v in per.items())):
        raise _Problem("ACCEPTANCE_ECONOMIC_NUMERIC_INVALID")
    base, bm = _replay_metrics(report.get("baseline_replay"))
    cand, cm = _replay_metrics(report.get("candidate_replay"))
    _economic_temporal(report, manifest, base, cand, recompute=recompute)
    candidate = report["candidate_replay"]
    costs = manifest["costs"]["config"]
    # run_study explicitly uses the portfolio/model defaults (not LIVE
    # parity). The replay fingerprint freezes those effective research knobs.
    costs_instance = replay.CostConfig(**{k: costs[k] for k in (
        "fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")})
    for side in ("baseline", "candidate"):
        recorded = report[side + "_replay"]
        expected_config_hash = portfolio._hash({"model": portfolio.ExecutionModel().as_dict(),
            "portfolio": portfolio.PortfolioConfig().as_dict(),
            "replay": manifest[side]["management_config"]["config_hash"], "costs": costs["config_hash"]})
        if recorded.get("costs") != portfolio.cost_status(costs_instance) \
                or recorded.get("config_hash") != expected_config_hash:
            raise _Problem("ACCEPTANCE_ECONOMIC_COST_IDENTITY_INVALID")
    if evidence.get("total_shadow_trades") != len(cand) \
            or report["study"].get("replay_admitted") != len(cand):
        raise _Problem("ACCEPTANCE_ECONOMIC_REPLAY_IDENTITY_INVALID")
    for field, expected in (("net_ev_r", cm.get("net_expectancy_r")), ("drawdown_r", cm.get("max_drawdown_r"))):
        if expected is not None and not _same_number(evidence.get(field), expected):
            raise _Problem("ACCEPTANCE_ECONOMIC_REPLAY_METRICS_MISMATCH")
    walk = report.get("walk_forward")
    if not isinstance(walk, Mapping) or walk.get("wf_version") != wf.WF_VERSION \
            or walk.get("selection_governs_evaluation") is not True:
        raise _Problem("ACCEPTANCE_WALK_FORWARD_MISSING", INSUFFICIENT)
    folds, paired, declared = walk.get("folds"), walk.get("paired"), walk.get("policy_delta")
    if not isinstance(folds, list) or not isinstance(paired, Mapping) or not isinstance(declared, Mapping):
        raise _Problem("ACCEPTANCE_WALK_FORWARD_INVALID")
    if any(not isinstance(f, Mapping) or (f.get("delta_net_r") is not None and _number(f["delta_net_r"]) is None)
           for f in folds):
        raise _Problem("ACCEPTANCE_WALK_FORWARD_NUMERIC_INVALID")
    delta = wf.policy_delta_r(paired)
    if declared != delta:
        raise _Problem("ACCEPTANCE_WALK_FORWARD_DELTA_MISMATCH")
    verdict, ci = walk.get("verdict"), walk.get("ci")
    if not isinstance(verdict, Mapping) or not isinstance(ci, Mapping):
        raise _Problem("ACCEPTANCE_WALK_FORWARD_INVALID")
    for key in ("low", "high", "mean"):
        if ci.get(key) is not None and _number(ci[key]) is None:
            raise _Problem("ACCEPTANCE_WALK_FORWARD_NUMERIC_INVALID")
    ci_available = ci.get("available") is True
    if ci_available and (ci.get("low") is None or ci.get("high") is None or ci["low"] > ci["high"]):
        raise _Problem("ACCEPTANCE_WALK_FORWARD_CI_INVALID")
    executed = [f for f in folds if f.get("reason_code") == "OK" and _number(f.get("delta_net_r")) is not None]
    stability = sum(f["delta_net_r"] > 0 for f in executed) / len(executed) if executed else None
    if stability is not None and not _same_number(evidence.get("stability_ratio"), stability):
        raise _Problem("ACCEPTANCE_ECONOMIC_STABILITY_MISMATCH")
    if ci_available and not _same_number(evidence.get("uncertainty_r"), abs(ci["high"] - ci["low"]) / 2):
        raise _Problem("ACCEPTANCE_ECONOMIC_UNCERTAINTY_MISMATCH")
    if any(report["study"].get(key) != expected for key, expected in (
            ("wf_state", verdict.get("state")), ("wf_winner", verdict.get("winner")),
            ("folds_executed", walk.get("folds_executed")))):
        raise _Problem("ACCEPTANCE_ECONOMIC_WALK_FORWARD_IDENTITY_MISMATCH")
    source = evidence.get("source")
    if not isinstance(source, Mapping) or source.get("derived_from_computed_results") is not True \
            or source.get("replay_version") != candidate.get("portfolio_version") \
            or source.get("walk_forward_version") != walk["wf_version"]:
        raise _Problem("ACCEPTANCE_ECONOMIC_SOURCE_INVALID")
    gate = catalog.go_no_go(evidence)
    # Observation must be applicable and measured: zero by omission is not proof.
    operational_observed = prospective is not None and all(_integer(evidence.get(k), 1) is not None for k in (
        "operational_applicable", "operational_observed"))
    operational_coverage = _number(evidence.get("operational_coverage_pct"))
    fidelity_n = _integer(evidence.get("fidelity_comparable"), 1)
    fidelity_den = _integer(evidence.get("fidelity_denominator"), 1)
    fidelity_div = _integer(evidence.get("fidelity_divergences"))
    fidelity_coverage = _number(evidence.get("fidelity_coverage_pct"))
    fidelity_observed = (prospective is not None and fidelity_n is not None and fidelity_den is not None and fidelity_div is not None
        and fidelity_n <= fidelity_den and fidelity_div <= fidelity_n
        and _same_percent(fidelity_coverage, 100 * fidelity_n / fidelity_den)
        and _same_percent(evidence.get("fidelity_discrepancy_pct"), 100 * fidelity_div / fidelity_n))
    if operational_observed and (evidence["operational_observed"] > evidence["operational_applicable"]
            or not _same_number(operational_coverage, 100 * evidence["operational_observed"] / evidence["operational_applicable"])):
        raise _Problem("ACCEPTANCE_OPERATIONAL_OBSERVATION_INVALID")
    if any(evidence.get(k) is not None and _number(evidence[k]) is None for k in (
            "operational_coverage_pct", "fidelity_coverage_pct")):
        raise _Problem("ACCEPTANCE_OPERATIONAL_OBSERVATION_INVALID")
    denominator = len(base)
    preserved = len(cand) / denominator if denominator else None
    checks = {"GO_NO_GO_EXISTING": gate.get("verdict") == "GO_CANDIDATE",
        "WF_CANDIDATE_FULL_POLICY": verdict.get("winner") == "CANDIDATE" and ci_available
            and ci["low"] > 0 and delta.get("available") is True and _number(delta.get("value")) is not None and delta["value"] > 0,
        "DRAWDOWN_NOT_WORSE": _number(bm.get("max_drawdown_r")) is not None
            and _number(cm.get("max_drawdown_r")) is not None and cm["max_drawdown_r"] <= bm["max_drawdown_r"],
        "BASELINE_TRADES_PRESERVED_70": preserved is not None and preserved >= 0.70,
        "OPERATIONAL_OBSERVATION_90": operational_observed and operational_coverage is not None and operational_coverage >= 90,
        "FIDELITY_OBSERVATION_90": fidelity_observed and fidelity_coverage is not None and fidelity_coverage >= 90,
        "COSTS_COMPLETE": costs.get("complete") is True,
        "SHADOW_ONLY": report["study"].get("population") == "SHADOW"}
    forward_gate = None
    if prospective is not None:
        forward_evidence = dict(prospective["evidence"])
        if protection is not None and protection["state"] == ACCEPTED:
            forward_evidence["essential_gaps"] = [g for g in forward_evidence.get("essential_gaps", ())
                                                 if g != "BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED"]
        forward_gate = catalog.go_no_go(forward_evidence)
    checks["PROSPECTIVE_GO_NO_GO_EXISTING"] = forward_gate is not None and forward_gate["verdict"] == "GO_CANDIDATE"
    missing = ["ACCEPTANCE_ECONOMIC_" + k.upper() + "_MISSING" for k in (*count_fields, *numeric_fields) if evidence.get(k) is None]
    if not denominator:
        missing.append("ACCEPTANCE_BASELINE_TRADE_DENOMINATOR_MISSING")
    if not operational_observed:
        missing.append("ACCEPTANCE_OPERATIONAL_OBSERVATION_MISSING")
    if not fidelity_observed:
        missing.append("ACCEPTANCE_FIDELITY_OBSERVATION_MISSING")
    # Pisos de suporte/tempo/cobertura dizem que ainda falta evidência,
    # não que a hipótese perdeu economicamente. Preserva o NO_GO e todos
    # os limites; reprovações substantivas com suporte completo continuam
    # REJECTED. As duas coortes permanecem separadas, inclusive nos motivos.
    insufficient_codes = {catalog.SAMPLE_INSUFFICIENT, catalog.PLAYBOOK_SAMPLE_INSUFFICIENT,
        catalog.DURATION_INSUFFICIENT, catalog.BUSINESS_DAYS_INSUFFICIENT, catalog.COVERAGE_INSUFFICIENT}
    for prefix, measured_gate in (("", gate), ("PROSPECTIVE_", forward_gate)):
        if measured_gate is not None:
            missing.extend("ACCEPTANCE_ECONOMIC_" + prefix + code
                           for code in measured_gate.get("reason_codes", ()) if code in insufficient_codes)
    if operational_observed and not checks["OPERATIONAL_OBSERVATION_90"]:
        missing.append("ACCEPTANCE_OPERATIONAL_OBSERVATION_INSUFFICIENT")
    if fidelity_observed and not checks["FIDELITY_OBSERVATION_90"]:
        missing.append("ACCEPTANCE_FIDELITY_OBSERVATION_INSUFFICIENT")
    result = _outcome(checks, missing)
    result["reason_codes"] = list(dict.fromkeys(code for code in [*result["reason_codes"],
        *gate.get("reason_codes", ()), *((forward_gate or {}).get("reason_codes", ()))] if code != "OK"))
    result["metrics"] = {"criteria_hash": gate.get("criteria_hash"), "gate": gate,
        "baseline_trades": denominator, "candidate_trades": len(cand), "trades_preserved": preserved,
        "baseline_drawdown_r": bm.get("max_drawdown_r"), "candidate_drawdown_r": cm.get("max_drawdown_r"),
        "net_ev_r": evidence.get("net_ev_r"), "wf_winner": verdict.get("winner"),
        "full_policy_delta_r": delta.get("value"), "wf_ci": dict(ci), "prospective_gate": forward_gate}
    return result


def _protection(report, prospective=None):
    evidence = prospective["measurements"] if prospective is not None else {}
    fields = ("protection_applicable", "protection_observed", "protection_pending")
    for key in fields:
        if evidence.get(key) is not None and _integer(evidence[key]) is None:
            raise _Problem("ACCEPTANCE_PROTECTION_NUMERIC_INVALID")
    applicable = _integer(evidence.get("protection_applicable"), 1)
    observed = _integer(evidence.get("protection_observed"))
    coverage = _number(evidence.get("protection_coverage_pct"))
    pending = _integer(evidence.get("protection_pending"))
    if evidence.get("protection_coverage_pct") is not None and coverage is None:
        raise _Problem("ACCEPTANCE_PROTECTION_NUMERIC_INVALID")
    if applicable is not None and observed is not None and (observed > applicable
            or not _same_percent(coverage, 100 * observed / applicable)):
        raise _Problem("ACCEPTANCE_PROTECTION_COVERAGE_INVALID")
    checks = {"SIMULATED_SCOPE_EXPLICIT": evidence.get("protection_scope") == "SHADOW_SIMULATED",
        "NO_REAL_SL_CLAIM": evidence.get("protection_proves_real_sl") is False,
        "OBSERVATION_90": applicable is not None and observed is not None and coverage is not None and coverage >= 90,
        "NO_UNRESOLVED_FAILURES": pending == 0 and evidence.get("unresolved_protection_failures") == 0}
    missing = ["ACCEPTANCE_PROTECTION_OBSERVATION_MISSING"] if applicable is None or observed is None or pending is None else []
    result = _outcome(checks, missing)
    result.update(scope="SHADOW_SIMULATED", purpose="PROMOTION", first_registration_only=True,
                  proves_real_sl=False, authorizes_canary=False)
    result["metrics"] = {"applicable": applicable, "observed": observed, "coverage_pct": coverage, "pending": pending}
    return result


def _evidence_hash(report):
    if not isinstance(report, Mapping):
        raise ValueError("report mapping required")
    return digest({k: v for k, v in report.items() if k != "acceptance"})


def _binding(report, context):
    manifest, identity, contract, fingerprint = context
    artifact = report.get("artifact") if isinstance(report.get("artifact"), Mapping) else {}
    calibration = report.get("calibration") if isinstance(report.get("calibration"), Mapping) else {}
    folds = calibration.get("folds") if isinstance(calibration.get("folds"), list) else []
    return {"study_key": report["study_key"], "study_id": manifest["study_id"],
        "manifest_hash": manifest["manifest_hash"], "bundle_hash": manifest["hashes"]["bundle_hash"],
        "contract_hash": contract["contract_hash"], "dataset_hash": identity["dataset_hash"],
        "price_hash": identity["price_hash"], "costs_hash": manifest["costs"]["config"]["config_hash"],
        "artifact_hash": artifact.get("artifact_hash"),
        "fold_artifact_hashes": [(f["artifact"].get("artifact_hash") if isinstance(f.get("artifact"), Mapping) else None)
                                 for f in folds if isinstance(f, Mapping)],
        "model_fingerprint": fingerprint, "event": (identity["calibration_request"] or {}).get("event"),
        "event_definition": artifact.get("event_definition"), "comparison_scope": manifest["comparison_scope"],
        "authority": manifest["decision"]["authority"], "reference": manifest["decision"]["reference"]}


def _assemble(report, now, supplied_ci=None, prospective_evidence=None):
    policy = policy_manifest()
    context, reason = None, None
    try:
        context = _context(report)
    except (_Problem, KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        reason = exc.reason if isinstance(exc, _Problem) else "ACCEPTANCE_STUDY_INVALID"
    prospective, prospective_problem = None, None
    if context is not None:
        try:
            prospective = _prospective(prospective_evidence, report, context, now)
        except (_Problem, KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            prospective_problem = exc.reason if isinstance(exc, _Problem) else "ACCEPTANCE_PROSPECTIVE_SNAPSHOT_INVALID"
    components = {}
    for name, operation in (("calibration", lambda: _calibration(report, context, now, supplied_ci)),
                            ("protection", lambda: _protection(report, prospective)),
                            ("economics", lambda: _economics(report, context, prospective, components["protection"],
                                                              recompute=supplied_ci is None))):
        try:
            if context is None:
                raise _Problem(reason)
            if prospective_problem is not None and name in ("economics", "protection"):
                raise _Problem(prospective_problem)
            components[name] = operation()
        except (_Problem, KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            components[name] = _component(exc.state if isinstance(exc, _Problem) else INVALID,
                [exc.reason if isinstance(exc, _Problem) else "ACCEPTANCE_" + name.upper() + "_INVALID"])
            if name == "protection":
                components[name].update(scope="SHADOW_SIMULATED", purpose="PROMOTION", first_registration_only=True,
                                        proves_real_sl=False, authorizes_canary=False)
    states = [components[k]["state"] for k in ("calibration", "economics", "protection")]
    state = INVALID if INVALID in states else INSUFFICIENT if INSUFFICIENT in states else REJECTED if REJECTED in states else ACCEPTED
    observed = _integer(report.get("observed_at_ms"), 1) if isinstance(report, Mapping) else None
    generated = prospective["cutoff_ms"] if prospective is not None else observed
    valid_until = observed + VALID_FOR_MS if observed is not None else None
    if context is not None:
        request = context[1]["calibration_request"]
        if isinstance(request, Mapping) and _integer(request.get("valid_for_ms"), 1) is not None:
            valid_until = min(valid_until, observed + request["valid_for_ms"]) if observed is not None else None
        calibration = report.get("calibration") if isinstance(report.get("calibration"), Mapping) else {}
        folds = calibration.get("folds") if isinstance(calibration.get("folds"), list) else []
        for fold in folds:
            if isinstance(fold, Mapping):
                artifact = fold.get("artifact") if isinstance(fold.get("artifact"), Mapping) else {}
                generation = artifact.get("generation") if isinstance(artifact.get("generation"), Mapping) else {}
                limit = _integer(generation.get("valid_until_ms"), 1)
                if limit is not None and valid_until is not None:
                    valid_until = min(valid_until, limit)
    try:
        evidence_hash = _evidence_hash(report)
    except (ValueError, TypeError, OverflowError):
        evidence_hash, state = None, INVALID
        components["calibration"] = _component(INVALID, ["ACCEPTANCE_EVIDENCE_NONCANONICAL"])
    snapshot_payload = dict(prospective_evidence) if isinstance(prospective_evidence, Mapping) else None
    try:
        digest(snapshot_payload)
    except (TypeError, ValueError, OverflowError):
        snapshot_payload, state = None, INVALID
        components["economics"] = _component(INVALID, ["ACCEPTANCE_PROSPECTIVE_NONCANONICAL"])
    body = {"contract": ACCEPTANCE_CONTRACT, "source_producer": PRODUCER_CONTRACT,
        "policy_version": POLICY_VERSION, "policy_hash": policy["policy_hash"], "state": state,
        **components, "binding": _binding(report, context) if context is not None and evidence_hash is not None else None,
        "evidence_hash": evidence_hash, "generated_at_ms": generated, "valid_until_ms": valid_until,
        "prospective_evidence": snapshot_payload if evidence_hash is not None else None,
        "revoked": False, "real_study_allowed": bool(context is not None and report.get("real_study_allowed") is True),
        "is_test_evidence": not bool(context is not None and report.get("real_study_allowed") is True),
        "authorizes_live": False, "live_license": False}
    if prospective is not None and body["binding"] is not None:
        body["binding"]["prospective_identity"] = dict(prospective["identity"])
        body["binding"]["cohort_hash"] = prospective["cohort_hash"]
        body["binding"]["prospective_cutoff_ms"] = prospective["cutoff_ms"]
        body["binding"]["prospective_snapshot_hash"] = prospective["snapshot_hash"]
    return {**body, "record_hash": digest(body)}


def build_acceptance(report, now_ms=None, prospective_evidence=None):
    """Compute the exact policy offline. Does not mutate the report or V1.

    Records are deterministic for the study observation time; ``now_ms`` only
    checks availability. Caller appends this separate record to ``acceptance``.
    """
    now = _clock(now_ms)
    if now is None:
        report = {"invalid_clock": True}
        now = 1
    return _assemble(report, now, prospective_evidence=prospective_evidence)


def verify_acceptance(report, now_ms=None, purpose="PROMOTION", *, recompute=False):
    """Validate a receipt, cheaply by default, fully at official ingestion.

    Cheap verification does no replay, fitting or bootstrap. It recomputes
    identities, OOS probabilities, counts, Brier/reliability and all checks;
    the exact bootstrap interval is trusted ONLY after ``recompute=True`` at
    official persistence. ``STRUCTURAL`` permits intact rejected/test receipts
    to be stored/reported; it cannot be used as operational authority.
    """
    def refused(reason, **extra):
        return {"ok": False, "reason_code": reason, **extra}
    if purpose not in ("PROMOTION", "CANARY", "STRUCTURAL") or type(recompute) is not bool:
        return refused("ACCEPTANCE_PURPOSE_INVALID")
    now = _clock(now_ms)
    if now is None:
        return refused("ACCEPTANCE_CLOCK_INVALID")
    record = report.get("acceptance") if isinstance(report, Mapping) else None
    if not isinstance(record, Mapping):
        return refused("ACCEPTANCE_MISSING")
    try:
        if record.get("contract") != ACCEPTANCE_CONTRACT or record.get("source_producer") != PRODUCER_CONTRACT \
                or record.get("policy_version") != POLICY_VERSION or record.get("policy_hash") != policy_manifest()["policy_hash"] \
                or record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"}):
            return refused("ACCEPTANCE_RECORD_INVALID")
        if type(record.get("revoked")) is not bool or record.get("live_license") is not False \
                or type(record.get("is_test_evidence")) is not bool or record.get("authorizes_live") is not False:
            return refused("ACCEPTANCE_RECORD_INVALID")
        if record["revoked"]:
            return refused("ACCEPTANCE_REVOKED")
        generated, expiry = _integer(record.get("generated_at_ms"), 1), _integer(record.get("valid_until_ms"), 1)
        if generated is None or expiry is None or not generated < expiry <= generated + VALID_FOR_MS or now < generated:
            return refused("ACCEPTANCE_VALIDITY_INVALID")
        if now >= expiry:
            return refused("ACCEPTANCE_EXPIRED")
        if record.get("evidence_hash") != _evidence_hash(report):
            return refused("ACCEPTANCE_EVIDENCE_DRIFT")
        supplied = None if recompute else ((record.get("calibration") or {}).get("metrics") or {}).get("brier_gain_ci", _MISSING_CI)
        # Missing CI in an insufficient/invalid calibration never triggers a
        # bootstrap: it has no usable inputs. A forged missing CI cannot pass.
        if not recompute and supplied is _MISSING_CI and record.get("calibration", {}).get("metrics"):
            return refused("ACCEPTANCE_BOOTSTRAP_CONTRACT_INVALID")
        expected = _assemble(report, now, supplied, record.get("prospective_evidence"))
        # JSON persistence legitimately turns tuples into arrays; hash the
        # complete closed record instead of Python-specific tuple/list equality.
        if digest(record) != digest(expected):
            return refused("ACCEPTANCE_RECOMPUTE_MISMATCH" if recompute else "ACCEPTANCE_SEMANTICS_INVALID")
        result = {"record_hash": record["record_hash"], "state": record["state"],
            "calibration_state": record["calibration"]["state"], "economics_state": record["economics"]["state"],
            "protection": dict(record["protection"]), "binding": record["binding"],
            "valid_until_ms": expiry, "real_study_allowed": record["real_study_allowed"],
            "live_license": False, "verification": "FULL_OFFLINE" if recompute else "CONTROLLED_STORE_LINEAR"}
        if purpose == "STRUCTURAL":
            return {"ok": True, "reason_code": "OK", **result}
        if record["real_study_allowed"] is not True:
            return refused("ACCEPTANCE_TEST_ONLY_NOT_AUTHORITY", **result)
        if record["calibration"]["state"] != ACCEPTED:
            return refused("ACCEPTANCE_CALIBRATION_NOT_ACCEPTED", **result)
        if record["economics"]["state"] != ACCEPTED:
            return refused("ACCEPTANCE_ECONOMICS_NOT_ACCEPTED", **result)
        if purpose == "PROMOTION" and record["protection"]["state"] != ACCEPTED:
            return refused("ACCEPTANCE_PROTECTION_NOT_ACCEPTED", **result)
        return {"ok": True, "reason_code": "OK", **result}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return refused("ACCEPTANCE_RECORD_INVALID")
