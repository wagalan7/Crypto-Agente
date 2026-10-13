"""Offline event-family statistics, using the existing numerical engines.

This producer consumes ONLY event/ledger-derived rows from the official event
study caller. It is not a replay, a collector, a parallel fitting/bootstrap
engine, or operational authority. The event-study caller must verify the
manifest/export/events/ledger before calling it; the consumed bodies are bound
again here for full recomputation at ingestion.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from services import research_acceptance_service as acceptance
from services import score_v3_calibration_service as calibration
from services import walk_forward_service as wf
from services import portfolio_replay_service as portfolio
from services import preselection_experiment_service as catalog

CONTRACT = "R13_LIVE_EVENT_EVIDENCE_V1"
SOURCE = "R13_LIVE_EVENT_FROM_OFFICIAL_EXPORT"
CALIBRATION_CONTRACT = "R13_LIVE_EVENT_CALIBRATION_V1"
ACCEPTANCE_CONTRACT = "R13_LIVE_EVENT_ACCEPTANCE_V1"
PROTOCOL_CONTRACT = "R13_LIVE_EVENT_EVALUATION_PROTOCOL_V1"
LABEL_SOURCES = frozenset({"ATTRIBUTED_EXECUTION_EVENT", "DECLARED_DETERMINISTIC_MODEL",
                           "TEST_ONLY_SYNTHETIC_EVENT"})
IDENTITY_FIELDS = frozenset({"manifest_hash", "dataset_hash", "events_hash", "ledger_hash",
    "config_hash", "model_fingerprint", "costs_hash", "bundle_hash",
    "engine_fingerprint", "runtime_fingerprint"})
TEMPORAL_FIELDS = frozenset({"train_start_ms", "validation_start_ms", "holdout_start_ms",
    "as_of_ms", "bar_ms", "horizon_ms", "purge_ms", "embargo_ms", "censoring"})
ECONOMIC_REQUIRED = frozenset({"opportunity_id", "decision_ts_ms", "result_available_ts_ms",
                             "net_r", "admitted"})
ECONOMIC_OPTIONAL = frozenset({"opportunity_key", "score", "playbook", "causal_index"})
POPULATION_REQUIRED = frozenset({"opportunity_key", "decision_ts_ms", "score", "included"})
LABEL_FIELDS = frozenset({"opportunity_key", "score", "label", "event", "decision_ts_ms",
    "label_available_ts_ms", "horizon_ms", "source", "proven"})
EVIDENCE_FIELDS = frozenset({"ok", "contract", "source", "identity", "input_hashes",
    "evaluation_protocol", "walk_forward", "economic_comparison", "calibration", "acceptance",
    "policy", "observed_at_ms", "test_only", "real_study_allowed", "holdout_status",
    "live_equivalent", "promotable", "authorizes_live", "evidence_hash"})
RECEIPT_FIELDS = frozenset({"contract", "source", "policy_version", "policy_hash", "state",
    "calibration", "economics", "protection", "test_only", "real_study_allowed",
    "authorizes_live", "authorizes_shadow", "authorizes_promotion", "authorizes_canary"})


class _Invalid(ValueError):
    pass


def digest(value):
    return acceptance.digest(value)


def _require(condition, reason):
    if not condition:
        raise _Invalid(reason)


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _number(value):
    return acceptance._number(value)


def _fields(value, required, optional=()):
    return isinstance(value, Mapping) and required <= set(value) <= required | set(optional)


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_inputs(baseline_rows, candidate_rows, population_rows, temporal,
                     event_labels, identity, now_ms, test_only):
    _require(type(test_only) is bool, "EVENT_EVIDENCE_ORIGIN_INVALID")
    _require(_integer(now_ms, minimum=1), "EVENT_EVIDENCE_CLOCK_INVALID")
    _require(_fields(identity, IDENTITY_FIELDS) and all(_hash(v) for v in identity.values()),
             "EVENT_EVIDENCE_IDENTITY_INVALID")
    _require(_fields(temporal, TEMPORAL_FIELDS, {"valid_for_ms"}), "EVENT_TEMPORAL_SCHEMA_INVALID")
    _require(all(_integer(temporal[k], minimum=1) for k in (
        "train_start_ms", "validation_start_ms", "holdout_start_ms", "as_of_ms", "bar_ms", "horizon_ms"))
        and all(_integer(temporal[k]) for k in ("purge_ms", "embargo_ms")),
        "EVENT_TEMPORAL_NUMERIC_INVALID")
    _require(temporal["train_start_ms"] < temporal["validation_start_ms"]
        < temporal["holdout_start_ms"] and temporal["validation_start_ms"] < temporal["as_of_ms"] <= now_ms,
        "EVENT_TEMPORAL_CHRONOLOGY_INVALID")
    _require(temporal["horizon_ms"] % temporal["bar_ms"] == 0
        and temporal["purge_ms"] % temporal["bar_ms"] == 0
        and temporal["embargo_ms"] % temporal["bar_ms"] == 0
        and temporal["censoring"] in calibration.CENSORING_RULES,
        "EVENT_TEMPORAL_UNITS_INVALID")
    validity = temporal.get("valid_for_ms", acceptance.VALID_FOR_MS)
    _require(_integer(validity, minimum=1) and validity <= acceptance.VALID_FOR_MS,
             "EVENT_CALIBRATION_VALIDITY_NOT_APPROVED")
    stop = min(temporal["holdout_start_ms"], temporal["as_of_ms"])
    _require(stop - temporal["validation_start_ms"] >= 6, "EVENT_FOLD_WINDOW_INVALID")
    _require(isinstance(population_rows, list), "EVENT_POPULATION_INVALID")
    population = {}
    for row in population_rows:
        _require(_fields(row, POPULATION_REQUIRED, {"causal_index"}), "EVENT_POPULATION_SCHEMA_INVALID")
        key, stamp = row["opportunity_key"], row["decision_ts_ms"]
        _require(isinstance(key, str) and key and key not in population
            and _integer(stamp, minimum=1) and temporal["train_start_ms"] <= stamp < stop,
            "EVENT_POPULATION_IDENTITY_OR_HOLDOUT_INVALID")
        _require(type(row["included"]) is bool and (row["score"] is None or
            _number(row["score"]) is not None and 0 <= row["score"] <= 100), "EVENT_POPULATION_SCORE_INVALID")
        _require("causal_index" not in row or _integer(row["causal_index"]), "EVENT_CAUSAL_INDEX_INVALID")
        population[key] = row
    for rows in (baseline_rows, candidate_rows):
        _require(isinstance(rows, list), "EVENT_ECONOMIC_ROWS_INVALID")
        seen = set()
        for row in rows:
            _require(_fields(row, ECONOMIC_REQUIRED, ECONOMIC_OPTIONAL), "EVENT_ECONOMIC_ROW_SCHEMA_INVALID")
            key, stamp, available = row["opportunity_id"], row["decision_ts_ms"], row["result_available_ts_ms"]
            _require(key in population and key not in seen and population[key]["decision_ts_ms"] == stamp
                and row.get("opportunity_key", key) == key, "EVENT_ECONOMIC_ROW_IDENTITY_INVALID")
            seen.add(key)
            _require(type(row["admitted"]) is bool and (row["net_r"] is None or _number(row["net_r"]) is not None)
                and (available is None or _integer(available, minimum=stamp)), "EVENT_ECONOMIC_ROW_NUMERIC_INVALID")
            _require("score" not in row or row["score"] is None or _number(row["score"]) is not None
                and 0 <= row["score"] <= 100, "EVENT_ECONOMIC_SCORE_INVALID")
            _require("playbook" not in row or row["playbook"] == "TREND_PULLBACK",
                     "EVENT_ECONOMIC_PLAYBOOK_NOT_APPROVED")
            _require(row["net_r"] is None or available is not None, "EVENT_RESULT_AVAILABILITY_MISSING")
            _require("causal_index" not in row or _integer(row["causal_index"]), "EVENT_CAUSAL_INDEX_INVALID")
    _require(isinstance(event_labels, list), "EVENT_LABELS_INVALID")
    seen = set()
    for row in event_labels:
        _require(_fields(row, LABEL_FIELDS), "EVENT_LABEL_SCHEMA_INVALID")
        key, available = row["opportunity_key"], row["label_available_ts_ms"]
        _require(key in population and key not in seen and row["decision_ts_ms"] == population[key]["decision_ts_ms"]
            and row["score"] == population[key]["score"], "EVENT_LABEL_IDENTITY_INVALID")
        seen.add(key)
        _require(row["event"] == calibration.EVENT_TP1 and row["horizon_ms"] == temporal["horizon_ms"]
            and row["source"] in LABEL_SOURCES and type(row["proven"]) is bool,
            "EVENT_LABEL_DEFINITION_OR_SOURCE_INVALID")
        _require(test_only or row["source"] != "TEST_ONLY_SYNTHETIC_EVENT",
                 "EVENT_SYNTHETIC_ORIGIN_CANNOT_BECOME_REAL")
        _require((row["label"] is None or type(row["label"]) is bool)
            and (available is None or _integer(available, minimum=row["decision_ts_ms"])),
            "EVENT_LABEL_CHRONOLOGY_INVALID")
        _require(row["label"] is None or available is not None, "EVENT_LABEL_AVAILABILITY_MISSING")
    return population, stop, validity


def _calibrate(population, event_labels, temporal, identity, now_ms, stop, validity, test_only):
    """Four real fits/predictions/evaluations; no substitute score probability."""
    width = (stop - temporal["validation_start_ms"]) // 4
    labels = [{k: r[k] for k in ("opportunity_key", "score", "label", "event",
                               "decision_ts_ms", "label_available_ts_ms")}
              for r in event_labels if r["proven"] is True and population[r["opportunity_key"]]["included"]]
    folds, all_predictions, metrics, eligible_counts, train_missing = [], [], [], [], []
    for idx in range(4):
        lo = temporal["validation_start_ms"] + idx * width
        hi = stop if idx == 3 else lo + width
        cut = lo - max(temporal["horizon_ms"], temporal["purge_ms"]) - temporal["embargo_ms"]
        eligible = sum(lo <= r["decision_ts_ms"] < hi for r in population.values())
        eligible_counts.append(eligible)
        train = [r for r in labels if r["decision_ts_ms"] < cut]
        oos = [r for r in labels if lo <= r["decision_ts_ms"] < hi]
        # Both train and OOS preparation enforce availability and real horizon.
        train_prepared = calibration.prepare_observations(train, cutoff_ms=cut,
            event=calibration.EVENT_TP1, horizon_ms=temporal["horizon_ms"], bar_ms=temporal["bar_ms"])
        oos_prepared = calibration.prepare_observations(oos, cutoff_ms=min(hi, now_ms),
            event=calibration.EVENT_TP1, horizon_ms=temporal["horizon_ms"], bar_ms=temporal["bar_ms"])
        fitted = calibration.fit_calibration(train, event=calibration.EVENT_TP1, population="RESEARCH_SHADOW",
            model_fingerprint=identity["model_fingerprint"], score_config_hash=identity["model_fingerprint"],
            horizon_bars=temporal["horizon_ms"] // temporal["bar_ms"], bar_ms=temporal["bar_ms"],
            censoring=temporal["censoring"], payoff_ref=identity["config_hash"], source=SOURCE,
            dataset_hash=identity["dataset_hash"], cutoff_ms=cut, generated_at_ms=now_ms,
            valid_until_ms=now_ms + validity,
            versions={"study": CONTRACT, "evidence_origin": "TEST_ONLY" if test_only
                      else "NON_TEST_EXECUTION_SOURCE", "operational_authority": "NONE", **identity})
        predictions, artifact = [], fitted.get("artifact")
        if fitted.get("ok"):
            constant = sum(r["label"] is True for r in train_prepared["rows"]) / train_prepared["unique"]
            for row in oos_prepared["rows"]:
                emitted = calibration.predict(artifact, score=row["score"], now_ms=now_ms)
                if emitted.get("available"):
                    predictions.append({"opportunity_key": row["opportunity_key"], "score": row["score"],
                        "label": row["label"], "prediction": emitted["probability"],
                        "constant_prediction": constant, "bin": emitted["bin"],
                        "decision_ts_ms": row["decision_ts_ms"],
                        "label_available_ts_ms": row["label_available_ts_ms"]})
            validated = calibration.validate_out_of_sample(artifact, oos_prepared["rows"], now_ms=now_ms)
            artifact = validated.get("artifact", artifact)
        else:
            validated = fitted
            train_missing.append("ACCEPTANCE_TRAIN_SAMPLE_INSUFFICIENT")
        bins, ece = acceptance._reliability(predictions)
        brier, reference = acceptance._brier(predictions, "prediction"), acceptance._brier(predictions, "constant_prediction")
        metrics.append({"fold": idx, "n": len(predictions), "eligible_count": eligible,
            "ece": ece, "brier": brier, "constant_brier": reference,
            "brier_gain": reference - brier if brier is not None else None,
            "artifact_hash": artifact.get("artifact_hash") if artifact else None})
        folds.append({"fold": idx, "train_cutoff_ms": cut, "oos_start_ms": lo, "oos_end_ms": hi,
            "state": validated.get("state"), "reason_code": validated.get("reason_code"), "artifact": artifact,
            "train_rows": train_prepared["unique"], "oos_rows": oos_prepared["unique"],
            "excluded_train": train_prepared["excluded"], "excluded_oos": oos_prepared["excluded"],
            "acceptance_inputs": {"eligible_count": eligible, "oos_rows": predictions}})
        all_predictions.extend(predictions)
    bins, ece = acceptance._reliability(all_predictions)
    recipe = acceptance.policy_manifest()["calibration"]
    authorized = [b for b in bins if b["bin"] in recipe["authorized_bins"]]
    last = folds[-1]["artifact"]
    coverage = len(all_predictions) / sum(eligible_counts) if sum(eligible_counts) else None
    ci = acceptance._bootstrap(all_predictions)
    checks = {"OOS_GLOBAL_100": len(all_predictions) >= recipe["min_global_oos"],
        "OOS_AUTHORIZED_BIN_SUPPORT_30": len(authorized) == len(recipe["authorized_bins"])
            and all(b["n"] >= recipe["min_per_authorized_bin"] for b in authorized),
        "SERVED_TRAIN_AUTHORIZED_BIN_SUPPORT_30": last is not None
            and all(last["bins"][idx]["supported"] is True for idx in recipe["authorized_bins"]),
        "COVERAGE_90": coverage is not None and coverage >= recipe["min_coverage"],
        "FOUR_FOLDS_MIN_20": all(f["n"] >= recipe["min_per_fold"] for f in metrics),
        "ECE_MAX_005": ece is not None and ece <= recipe["max_ece"] + 1e-12,
        "BIN_ERROR_MAX_010": all(b["error"] <= recipe["max_bin_error"] + 1e-12 for b in authorized),
        "BRIER_CI_LOW_POSITIVE": ci["available"] is True and ci["low"] > 0,
        "THREE_FOLD_GAINS_NONNEGATIVE": sum(f["brier_gain"] is not None and f["brier_gain"] >= -1e-12
            for f in metrics) >= recipe["min_nonnegative_fold_gains"],
        "FOLD_ECE_MAX_010": all(f["ece"] is not None and f["ece"] <= recipe["max_fold_ece"] + 1e-12 for f in metrics)}
    missing = train_missing + [k for k in ("OOS_GLOBAL_100", "OOS_AUTHORIZED_BIN_SUPPORT_30",
        "SERVED_TRAIN_AUTHORIZED_BIN_SUPPORT_30", "COVERAGE_90", "FOUR_FOLDS_MIN_20") if not checks[k]]
    if not ci["available"]:
        missing.append(ci["reason_code"])
    result = acceptance._outcome(checks, missing)
    result["metrics"] = {"oos_unique": len(all_predictions), "eligible_count": sum(eligible_counts),
        "coverage": coverage, "ece": ece, "bins": bins, "folds": metrics,
        "brier": acceptance._brier(all_predictions, "prediction"),
        "constant_brier": acceptance._brier(all_predictions, "constant_prediction"), "brier_gain_ci": ci,
        "dependence_sensitivity": {"utc_day_blocks": ci["blocks"], "cross_symbol_independence_assumed": False}}
    result.update({"source": SOURCE, "test_only": test_only, "real_study_allowed": False,
                   "authorizes_live": False})
    return {"contract": CALIBRATION_CONTRACT, "source": SOURCE, "folds": folds,
        "artifact": last, "acceptance": result, "event": calibration.EVENT_TP1,
        "event_not_net_profit": True, "holdout_status": "SEALED", "test_only": test_only,
        "real_study_allowed": False, "economically_approved": False,
        "acceptance_population": {"index": [{"opportunity_key": r["opportunity_key"],
            "decision_ts_ms": r["decision_ts_ms"]} for r in population.values()],
            "fold_eligible_counts": eligible_counts, "eligible_count": sum(eligible_counts)}}


def evaluate_evidence(*, baseline_rows, candidate_rows, population_rows, temporal,
                      event_labels, identity, now_ms, test_only=True):
    """Compute analytical evidence; never consume client-supplied metrics.

    ``horizon_ms`` is a label/censoring/purge horizon, NOT an instruction to
    close a LIVE position. UNKNOWN keys are removed symmetrically and remain
    in the raw coverage denominator. Uncertain portfolio periods must already
    be marked non-comparable by the event portfolio producer.
    """
    try:
        population, stop, validity = _validate_inputs(baseline_rows, candidate_rows,
            population_rows, temporal, event_labels, identity, now_ms, test_only)
        inputs = {"baseline_rows": baseline_rows, "candidate_rows": candidate_rows,
            "population_rows": population_rows, "temporal": temporal, "event_labels": event_labels,
            "identity": identity, "now_ms": now_ms, "test_only": test_only}
        hashes = {key: digest(value) for key, value in inputs.items()}
        unknown = {r["opportunity_id"] for rows in (baseline_rows, candidate_rows) for r in rows
            if r["admitted"] and (r["net_r"] is None or r["result_available_ts_ms"] > temporal["as_of_ms"])}
        unknown.update(key for key, row in population.items() if row["included"] is not True)
        def usable(rows):
            return sorted((r for r in rows if r["admitted"] and r["opportunity_id"] not in unknown),
                          key=lambda r: (r["decision_ts_ms"], r.get("causal_index", 0), r["opportunity_id"]))
        sides = {"baseline": usable(baseline_rows), "candidate": usable(candidate_rows)}
        coverage = 100.0 * (len(population) - len(unknown)) / len(population) if population else None
        guard = wf.coverage_guard({"considered": len(population), "resolved": len(population) - len(unknown)},
                                  {"considered": len(population), "resolved": len(population) - len(unknown)},
                                  min_coverage_pct=acceptance.policy_manifest()["economics"]["criteria"]["min_coverage_pct"])
        width = (stop - temporal["validation_start_ms"]) // 6
        folds = [wf.Fold(i, temporal["train_start_ms"], temporal["validation_start_ms"] + i * width,
            temporal["validation_start_ms"] + i * width, stop if i == 5
            else temporal["validation_start_ms"] + (i + 1) * width) for i in range(6)]
        protocol = {"contract": PROTOCOL_CONTRACT, "calibration_folds": 4, "economic_folds": 6,
            "economic_bootstrap": {"seed": 7, "samples": 500, "block_size": 5, "alpha": wf.DEFAULT_ALPHA,
                                   "comparisons": 1}, "economic_fold_plan": [f.as_dict() for f in folds],
            "temporal": dict(temporal), "source": SOURCE, "test_only": test_only,
            "real_study_allowed": False}
        observed = max((r["result_available_ts_ms"] - r["decision_ts_ms"]
                        for rows in sides.values() for r in rows), default=0)
        horizon_sufficient = observed <= temporal["horizon_ms"] and temporal["purge_ms"] >= temporal["horizon_ms"]
        walk = wf.run_walk_forward(baseline=sides["baseline"], candidate=sides["candidate"], folds=folds,
            bar_ms=temporal["bar_ms"], horizon_bars=max(temporal["horizon_ms"], temporal["purge_ms"]) // temporal["bar_ms"],
            embargo_bars=temporal["embargo_ms"] // temporal["bar_ms"], costs_complete=not unknown,
            horizon_sufficient=horizon_sufficient, coverage=guard, **protocol["economic_bootstrap"])
        walk.update({"source": SOURCE, "test_only": test_only, "real_study_allowed": False,
                     "authorizes_live": False})
        replays = {side: {"portfolio_version": CONTRACT, "trades": rows, "admitted": len(rows),
            "metrics": portfolio.portfolio_metrics([r["net_r"] for r in sorted(rows,
                key=lambda r: (r["result_available_ts_ms"], r.get("causal_index", 0), r["opportunity_id"]))])}
            for side, rows in sides.items()}
        evidence = catalog.gate_evidence_from_study(replay=replays["candidate"], study=walk,
            trades=sides["candidate"], enabled_playbooks=("TREND_PULLBACK",),
            window_start_ms=temporal["train_start_ms"], window_end_ms=stop, coverage_pct=coverage,
            essential_gaps=(["TEST_ONLY_NOT_REAL_EVIDENCE"] if test_only else [])
                + ["PROSPECTIVE_FIDELITY_NOT_OBSERVED", "REAL_PROTECTION_NOT_OBSERVED"])
        evidence["source"]["family"] = SOURCE
        gate = catalog.go_no_go(evidence)
        bm, cm = replays["baseline"]["metrics"], replays["candidate"]["metrics"]
        preserved = len(sides["candidate"]) / len(sides["baseline"]) if sides["baseline"] else None
        checks = {"GO_NO_GO_EXISTING": gate["verdict"] == "GO_CANDIDATE",
            "WF_CANDIDATE_FULL_POLICY": walk["verdict"].get("winner") == "CANDIDATE",
            "DRAWDOWN_NOT_WORSE": bm["max_drawdown_r"] is not None and cm["max_drawdown_r"] is not None
                and cm["max_drawdown_r"] <= bm["max_drawdown_r"],
            "BASELINE_TRADES_PRESERVED_70": preserved is not None
                and preserved >= acceptance.policy_manifest()["economics"]["min_baseline_trades_preserved"],
            "OPERATIONAL_OBSERVATION_90": False, "FIDELITY_OBSERVATION_90": False,
            "COSTS_COMPLETE": not unknown, "SHADOW_ONLY": True, "PROSPECTIVE_GO_NO_GO_EXISTING": False}
        economics = acceptance._outcome(checks, ("ACCEPTANCE_OPERATIONAL_OBSERVATION_MISSING",
            "ACCEPTANCE_FIDELITY_OBSERVATION_MISSING", "ACCEPTANCE_REAL_PROTECTION_MISSING"))
        economics.update({"source": SOURCE, "test_only": test_only, "real_study_allowed": False,
                          "authorizes_live": False})
        economics["metrics"] = {"gate": gate, "baseline": bm, "candidate": cm,
            "baseline_trades": len(sides["baseline"]), "candidate_trades": len(sides["candidate"]),
            "trades_preserved": preserved, "coverage_pct": coverage, "unknown_keys": sorted(unknown),
            "full_policy_delta_r": wf.policy_delta_r(wf.pair_opportunities(sides["baseline"], sides["candidate"])),
            "horizon_observed_ms": observed, "horizon_sufficient": horizon_sufficient}
        fitted = _calibrate(population, event_labels, temporal, identity, now_ms, stop, validity, test_only)
        protection = acceptance._component(acceptance.INSUFFICIENT, ("REAL_PROTECTION_NOT_OBSERVED",))
        protection.update({"source": SOURCE, "test_only": test_only, "real_study_allowed": False,
                           "authorizes_live": False})
        receipt = {"contract": ACCEPTANCE_CONTRACT, "source": SOURCE,
            "policy_version": acceptance.POLICY_VERSION, "policy_hash": acceptance.policy_manifest()["policy_hash"],
            "state": acceptance.INSUFFICIENT, "calibration": fitted["acceptance"], "economics": economics,
            "protection": protection,
            "test_only": test_only, "real_study_allowed": False, "authorizes_live": False,
            "authorizes_shadow": False, "authorizes_promotion": False, "authorizes_canary": False}
        result = {"ok": True, "contract": CONTRACT, "source": SOURCE, "identity": dict(identity),
            "input_hashes": hashes, "evaluation_protocol": protocol, "walk_forward": walk,
            "economic_comparison": economics, "calibration": fitted, "acceptance": receipt,
            "policy": acceptance.policy_manifest(), "observed_at_ms": now_ms, "test_only": test_only,
            "real_study_allowed": False, "holdout_status": "SEALED", "live_equivalent": False,
            "promotable": False, "authorizes_live": False}
        # JSONB restores JSON arrays as lists. Normalize ONLY this new-family
        # envelope, before hashing/returning it, so ingestion recomputation and
        # restart compare the same representation. Native engines are untouched
        # and their canonical hashes already serialize tuples as JSON arrays.
        result = json.loads(json.dumps(result, allow_nan=False))
        return {**result, "evidence_hash": digest(result)}
    except (_Invalid, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "contract": CONTRACT, "source": SOURCE, "state": "INVALID",
            "reason_code": str(exc) if isinstance(exc, _Invalid) else "EVENT_EVIDENCE_SCHEMA_INVALID",
            "test_only": test_only is True, "real_study_allowed": False, "promotable": False,
            "authorizes_live": False}


def verify_evidence(evidence, *, inputs=None):
    """Cheap controlled-store check, or full deterministic ingestion check.

    Without ``inputs`` this does no fitting, replay, bootstrap or writes and
    cannot authenticate arbitrary externally supplied numerical claims.
    """
    try:
        _require(_fields(evidence, EVIDENCE_FIELDS) and evidence.get("ok") is True
            and evidence.get("contract") == CONTRACT and evidence.get("source") == SOURCE,
            "EVENT_EVIDENCE_CONTRACT_INVALID")
        _require(evidence.get("evidence_hash") == digest({k: v for k, v in evidence.items() if k != "evidence_hash"}),
                 "EVENT_EVIDENCE_HASH_DRIFT")
        _require(evidence.get("policy") == acceptance.policy_manifest(), "EVENT_EVIDENCE_POLICY_DRIFT")
        _require(type(evidence.get("test_only")) is bool and evidence.get("holdout_status") == "SEALED"
            and all(evidence.get(k) is False for k in ("real_study_allowed", "promotable", "authorizes_live", "live_equivalent")),
            "EVENT_EVIDENCE_AUTHORITY_INVALID")
        receipt = evidence["acceptance"]
        _require(_fields(receipt, RECEIPT_FIELDS)
            and receipt.get("contract") == ACCEPTANCE_CONTRACT and receipt.get("source") == SOURCE
            and receipt.get("policy_hash") == evidence["policy"]["policy_hash"]
            and receipt.get("policy_version") == acceptance.POLICY_VERSION
            and receipt.get("state") == acceptance.INSUFFICIENT
            and receipt.get("test_only") is evidence["test_only"]
            and all(receipt.get(k) is False for k in ("real_study_allowed", "authorizes_live", "authorizes_shadow",
                                                    "authorizes_promotion", "authorizes_canary")),
            "EVENT_ACCEPTANCE_AUTHORITY_INVALID")
        fitted = evidence["calibration"]
        _require(fitted.get("contract") == CALIBRATION_CONTRACT and fitted.get("source") == SOURCE
            and fitted.get("event") == calibration.EVENT_TP1 and fitted.get("event_not_net_profit") is True
            and fitted.get("holdout_status") == "SEALED" and fitted.get("test_only") is evidence["test_only"]
            and fitted.get("real_study_allowed") is False and fitted.get("economically_approved") is False
            and len(fitted["folds"]) == 4, "EVENT_CALIBRATION_FAMILY_INVALID")
        for component in (evidence["walk_forward"], evidence["evaluation_protocol"],
                          evidence["economic_comparison"], fitted["acceptance"], receipt["protection"]):
            _require(component.get("source") == SOURCE and component.get("test_only") is evidence["test_only"]
                and component.get("real_study_allowed") is False, "EVENT_COMPONENT_ORIGIN_DRIFT")
        _require(_fields(evidence["identity"], IDENTITY_FIELDS)
            and all(_hash(v) for v in evidence["identity"].values()), "EVENT_EVIDENCE_IDENTITY_INVALID")
        for fold in fitted["folds"]:
            artifact = fold.get("artifact")
            if artifact is not None:
                _require(artifact.get("source") == SOURCE and artifact.get("contract") == calibration.CALIBRATION_CONTRACT
                    and artifact.get("artifact_hash") == calibration.recompute_hash(artifact)
                    and artifact.get("dataset_hash") == evidence["identity"]["dataset_hash"]
                    and artifact.get("model_fingerprint") == evidence["identity"]["model_fingerprint"],
                    "EVENT_CALIBRATION_ARTIFACT_DRIFT")
                _require(artifact["versions"].get("evidence_origin") == ("TEST_ONLY" if evidence["test_only"]
                         else "NON_TEST_EXECUTION_SOURCE") and artifact["versions"].get("operational_authority") == "NONE",
                         "EVENT_CALIBRATION_ORIGIN_DRIFT")
        if inputs is not None:
            _require(isinstance(inputs, Mapping), "EVENT_RECOMPUTATION_INPUTS_INVALID")
            recomputed = evaluate_evidence(**inputs)
            _require(recomputed.get("ok") and recomputed["evidence_hash"] == evidence["evidence_hash"],
                     "EVENT_EVIDENCE_RECOMPUTATION_DRIFT")
        return {"ok": True, "contract": CONTRACT, "source": SOURCE, "state": "LOCAL_VALIDATED",
                "recomputed": inputs is not None, "real_study_allowed": False, "authorizes_live": False}
    except (_Invalid, ValueError, TypeError, KeyError, OverflowError) as exc:
        return {"ok": False, "state": "INVALID", "reason_code": str(exc) if isinstance(exc, _Invalid)
                else "EVENT_EVIDENCE_SCHEMA_INVALID", "real_study_allowed": False, "authorizes_live": False}
