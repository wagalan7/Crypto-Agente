"""Source-bound research sizing after existing eligibility, without LIVE I/O.

The recommendation/batch producer, cache writers and eligibility gates are
observed inputs, not reconstructed here. No report grants account authority.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

from services import live_event_execution_service as execution

VERSION = "CW_LIVE_UPSTREAM_SIZING_REQUEST_V1"
REPORT_VERSION = "CW_LIVE_UPSTREAM_SIZING_REPORT_V1"
INPUT_SCOPE = "AFTER_EXISTING_ELIGIBILITY_BEFORE_SIZING"
BACKEND = Path(__file__).resolve().parents[1]
WORKER = BACKEND / "scripts/live_upstream_sizing_worker.py"
SOURCE_FILES = ("services/shadow_trade_service.py", "services/symbol_learning_service.py",
    "services/edge_decay_service.py", "services/regime_service.py",
    "services/live_upstream_sizing_service.py", "scripts/live_upstream_sizing_worker.py",
    "services/live_event_execution_service.py", "services/live_nominal_sizing_service.py",
    "scripts/live_nominal_sizing_worker.py", "services/live_filler_batch_service.py")
HELPERS = {"_conviction_mult", "_edge_mult", "_funding_ev_r", "_get_rec_feature", "_compute_qty",
    "_exec_size_damp", "_short_slip_guard", "_liq_tier_mult", "_regime_size_mult",
    "_sizing_stack_report", "_resolve_equity_usd"}
SYMBOL_CONFIG = {"SYMBOL_LEARNING_SIZE_ENABLED", "SIZE_MULT_MIN", "SIZE_MULT_MAX",
    "REL_DEADBAND", "MIN_CONFIDENCE_APPLY"}
EDGE_CONFIG = {"enabled", "window_days", "recent_days", "min_sample", "r_floor",
    "r_full", "base_min", "mult_min", "ttl_sec"}
OBS_FIELDS = {"producer", "generation", "observed_at_ms", "available_at_ms", "quality",
    "value", "observation_hash"}
FIELDS = {"version", "source", "test_only", "source_fingerprint", "decision_at_ms",
    "opportunity_id", "recommendation", "config", "context", "observations", "source_provenance", "request_hash"}
REPORT_FIELDS = {"version", "status", "reason_code", "input_scope", "request_hash", "runtime_identity",
    "source_fingerprint", "source", "test_only", "opportunity_id", "raw_risk_pct", "final_risk_pct",
    "qty", "notional", "sizing", "steps", "observations_used", "decisions", "gaps", "declared_policy",
    "authorizes_live", "promotable", "live_equivalent", "real_study_allowed", "blocked_network_attempts",
    "isolation", "producer_refs", "report_hash"}


def canonical(value):
    return execution.canonical(value)


def digest(value):
    return execution.digest(value)


request_hash = digest


def helper_names():
    return set(HELPERS)


def native_slice():
    """Unchanged AST statements, bounded by caller assignments, not line numbers."""
    module = ast.parse((BACKEND / "services/shadow_trade_service.py").read_text())
    functions = {n.name: n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    caller = functions["open_shadow_for_recs"]
    matches = []
    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    for node in ast.walk(caller):
        if not isinstance(node, ast.Try):
            continue
        for start in range(len(node.body) - 2):
            if not all(assigns(node.body[start+i], name) for i, name in enumerate(("entry", "stop", "risk_pct"))):
                continue
            ends = [i for i in range(start+3, len(node.body)) if isinstance(node.body[i], ast.Try)
                    and any(assigns(n, "open_notional") for n in node.body[i].body)]
            if ends:
                matches.append(node.body[start:ends[0]])
    if len(matches) != 1 or not HELPERS <= functions.keys():
        raise ValueError("UPSTREAM_NATIVE_REGION_DRIFT")
    return copy.deepcopy(matches[0])


def native_shadow_constant_names():
    module = ast.parse((BACKEND / "services/shadow_trade_service.py").read_text())
    functions = {n.name: n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return {n.id for node in native_slice() + [functions[name] for name in HELPERS]
            for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id.isupper()}


def source_fingerprint():
    return digest({"files": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
                             for name in SOURCE_FILES}, "runtime": execution.runtime_identity()})


def _closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("UPSTREAM_SCHEMA:" + name)


def _number(value, name, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError("UPSTREAM_NUMBER:" + name)


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("UPSTREAM_INTEGER:" + name)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("UPSTREAM_TEXT:" + name)


def validate_config(config):
    _closed(config, {"shadow", "symbol_learning", "edge_decay"}, "config")
    _closed(config["shadow"], native_shadow_constant_names(), "shadow.config")
    for key, value in config["shadow"].items():
        if key.endswith("_ENABLED"):
            if type(value) is not bool:
                raise ValueError("UPSTREAM_LITERAL_BOOL:" + key)
        elif key == "FILLER_FORA_TEST_N":
            _int(value, key)
        else:
            _number(value, key, 0)
    if config["shadow"]["SHADOW_ENABLED"] is not False:
        raise ValueError("UPSTREAM_REQUIRES_LIVE_POLICY_ISOLATED")
    if not 0 < config["shadow"]["LIVE_SIZE_MULT"] <= 1:
        raise ValueError("UPSTREAM_CANARY_RANGE")
    _closed(config["symbol_learning"], SYMBOL_CONFIG, "symbol.config")
    s = config["symbol_learning"]
    if type(s["SYMBOL_LEARNING_SIZE_ENABLED"]) is not bool:
        raise ValueError("UPSTREAM_LITERAL_BOOL:SYMBOL_LEARNING_SIZE_ENABLED")
    for key in SYMBOL_CONFIG - {"SYMBOL_LEARNING_SIZE_ENABLED"}:
        _number(s[key], key, 0)
    if not (0 < s["SIZE_MULT_MIN"] <= s["SIZE_MULT_MAX"] and s["MIN_CONFIDENCE_APPLY"] <= 1):
        raise ValueError("UPSTREAM_SYMBOL_CONFIG_RANGE")
    _closed(config["edge_decay"], EDGE_CONFIG, "edge.config")
    e = config["edge_decay"]
    if type(e["enabled"]) is not bool:
        raise ValueError("UPSTREAM_LITERAL_BOOL:EDGE_DECAY_ENABLED")
    for key in ("window_days", "recent_days", "min_sample", "ttl_sec"):
        _int(e[key], key, 1)
    for key in ("r_floor", "r_full", "base_min", "mult_min"):
        _number(e[key], key)
    return copy.deepcopy(config)


def make_observation(*, producer, value, observed_at_ms, available_at_ms, generation=0, quality="CAPTURED"):
    body = {"producer": producer, "generation": generation, "observed_at_ms": observed_at_ms,
            "available_at_ms": available_at_ms, "quality": quality, "value": copy.deepcopy(value)}
    return {**body, "observation_hash": digest(body)}


def _observation(observation, name, decision):
    _closed(observation, OBS_FIELDS, name)
    _text(observation["producer"], name + ".producer")
    _int(observation["generation"], name + ".generation")
    for key in ("observed_at_ms", "available_at_ms"):
        _int(observation[key], name + "." + key, 1)
    if not observation["observed_at_ms"] <= observation["available_at_ms"] <= decision:
        raise ValueError("UPSTREAM_OBSERVATION_NOT_CAUSAL:" + name)
    if observation["quality"] not in {"CAPTURED", "UNAVAILABLE"}:
        raise ValueError("UPSTREAM_OBSERVATION_QUALITY:" + name)
    if observation["quality"] == "UNAVAILABLE" and observation["value"] is not None:
        raise ValueError("UPSTREAM_UNKNOWN_HAS_VALUE:" + name)
    if observation["observation_hash"] != digest({k:v for k,v in observation.items() if k != "observation_hash"}):
        raise ValueError("UPSTREAM_OBSERVATION_DRIFT:" + name)
    if observation["quality"] == "UNAVAILABLE":
        return
    value = observation["value"]
    if name == "equity":
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise ValueError("UPSTREAM_EQUITY_RESPONSE")
        if value["ok"]:
            _number(value.get("total_usd"), "equity.total", 0)
            if value.get("source") not in {"live", "cache", "fallback"}:
                raise ValueError("UPSTREAM_EQUITY_SOURCE")
        return
    if not isinstance(value, list) or len(value) > 2000:
        raise ValueError("UPSTREAM_CACHE_ROWS:" + name)
    seen = set()
    for row in value:
        if name == "symbol_learning":
            _closed(row, {"base", "timeframe", "row"}, name + ".row")
            key = (row["base"], row["timeframe"])
        else:
            _closed(row, {"symbol", "direction", "row"}, name + ".row")
            key = (row["symbol"], row["direction"])
            if row["direction"] not in {"long", "short", "any"}:
                raise ValueError("UPSTREAM_DECAY_DIRECTION")
        if key in seen or not all(isinstance(k, str) and k for k in key) or not isinstance(row["row"], dict):
            raise ValueError("UPSTREAM_CACHE_IDENTITY:" + name)
        seen.add(key)


def source_material(request):
    keys = ("recommendation", "context", "observations", "nominal_producer", "filler_batch")
    return {k: request[k] for k in keys if k in request}


def validate_producer_bindings(request):
    """Native producers are optional for the old observed-input contract only.

    Once supplied, they must be complete, recomputed and match the exact input.
    None of these component proofs substitutes for eligibility or DB truth.
    """
    rec = request["recommendation"]
    refs = {}
    for key in ("nominal_producer", "filler_batch"):
        if key not in request:
            continue
        pair = request[key]
        _closed(pair, {"request", "report"}, key)
        if key == "nominal_producer":
            from services import live_nominal_sizing_service as producer
        else:
            from services import live_filler_batch_service as producer
        original = producer.validate_request(pair["request"])
        report = (producer.validate_report(pair["report"], original, recompute=True)
                  if key == "filler_batch" else producer.validate_report(pair["report"], original))
        if (original["decision_at_ms"] > request["decision_at_ms"] or original["source"] != request["source"]
                or original["test_only"] is not request["test_only"]):
            raise ValueError("UPSTREAM_PRODUCER_SOURCE_OR_CLOCK_DRIFT:" + key)
        if key == "nominal_producer":
            if report["status"] != "COMPLETE_NOMINAL":
                raise ValueError("UPSTREAM_NOMINAL_UNAVAILABLE")
            matches = [r["recommendation"] for r in report["recommendations"]
                       if r["opportunity_id"] == request["opportunity_id"]]
            if len(matches) != 1:
                raise ValueError("UPSTREAM_NOMINAL_OPPORTUNITY_DRIFT")
            expected = matches[0]
        else:
            if (report["status"] != "COMPLETE_CONTEXT" or report["opportunity_id"] != request["opportunity_id"]
                    or report["route"]["allowed"] is not True):
                raise ValueError("UPSTREAM_FILLER_ROUTE_NOT_READY")
            expected = report["recommendation"]
            if request["context"]["filler_test_done"] != report["state_before"]["test_done"]:
                raise ValueError("UPSTREAM_FILLER_COUNTER_DRIFT")
            for field in set(original["config"]) & set(request["config"]["shadow"]):
                if original["config"][field] != request["config"]["shadow"][field]:
                    raise ValueError("UPSTREAM_FILLER_CONFIGURATION_DRIFT")
            if rec.get("_is_filler", False) is not expected.get("_is_filler", False):
                raise ValueError("UPSTREAM_FILLER_CLASSIFICATION_DRIFT")
        if any(k not in rec or canonical(rec[k]) != canonical(value) for k, value in expected.items()):
            raise ValueError("UPSTREAM_PRODUCER_RECOMMENDATION_DRIFT:" + key)
        refs[key] = {"request_hash": original["request_hash"], "report_hash": report["report_hash"],
                     "batch_id": original["batch_id"], "scope": report["input_scope"]}
    if len(refs) == 2:
        if refs["nominal_producer"]["batch_id"] != refs["filler_batch"]["batch_id"]:
            raise ValueError("UPSTREAM_PRODUCER_BATCH_ID_DRIFT")
        nominal_pair, filler_pair = request["nominal_producer"], request["filler_batch"]
        available = nominal_pair["request"]["decision_at_ms"]
        if (available > filler_pair["request"]["decision_at_ms"]
                or any(item["observation"]["observed_at_ms"] < available for item in filler_pair["request"]["history"])):
            raise ValueError("UPSTREAM_PRODUCER_STAGE_CLOCK_REVERSED")
        members = {r["opportunity_id"]: r["recommendation"] for r in nominal_pair["report"]["recommendations"]}
        for row in filler_pair["request"]["recommendations"]:
            expected = members.get(row["opportunity_id"])
            actual = row["recommendation"]
            if expected is None or any(k not in actual or canonical(actual[k]) != canonical(v) for k, v in expected.items()):
                raise ValueError("UPSTREAM_FILLER_BATCH_NOT_FROM_NOMINAL_PRODUCER")
    return refs


def validate_request(request):
    optional = set(request) & {"nominal_producer", "filler_batch"} if isinstance(request, dict) else set()
    _closed(request, FIELDS | optional, "request")
    canonical(request)
    execution._credential_free(request)
    if request["version"] != VERSION or request["source"] not in execution.SOURCES:
        raise ValueError("UPSTREAM_VERSION_OR_SOURCE")
    provenance = request["source_provenance"]
    _closed(provenance, {"producer", "source_id", "source_hash", "quality", "model_version", "synthetic"}, "source_provenance")
    if (type(request["test_only"]) is not bool or type(provenance["synthetic"]) is not bool
            or request["test_only"] is not (request["source"] == "TEST_ONLY_OBSERVED_EVENTS" or provenance["synthetic"])):
        raise ValueError("UPSTREAM_TEST_ONLY_BOUNDARY")
    for key in ("producer", "source_id", "source_hash"):
        _text(provenance[key], key)
    if (provenance["source_hash"] != digest(source_material(request))
            or provenance["quality"] != execution.SOURCES[request["source"]]):
        raise ValueError("UPSTREAM_PROVENANCE_DRIFT")
    if request["source"] == "DECLARED_EVENT_TABLE":
        if provenance["model_version"] != "CW_DECLARED_SIZING_INPUTS_V1":
            raise ValueError("UPSTREAM_DECLARED_MODEL_UNSUPPORTED")
    elif provenance["model_version"] is not None:
        raise ValueError("UPSTREAM_MIXED_SOURCE_FAMILIES")
    if request["source_fingerprint"] != source_fingerprint():
        raise ValueError("UPSTREAM_SOURCE_DRIFT")
    if request["request_hash"] != digest({k:v for k,v in request.items() if k != "request_hash"}):
        raise ValueError("UPSTREAM_REQUEST_DRIFT")
    _int(request["decision_at_ms"], "decision_at_ms", 1)
    _text(request["opportunity_id"], "opportunity_id")
    validate_config(request["config"])
    rec = request["recommendation"]
    if not isinstance(rec, dict) or rec.get("direction") not in {"long", "short"}:
        raise ValueError("UPSTREAM_RECOMMENDATION_SIDE")
    for key in ("symbol", "timeframe"):
        _text(rec.get(key), "recommendation." + key)
    for key in ("entry", "stop_loss", "risk_pct"):
        _number(rec.get(key), "recommendation." + key, 0)
    _int(rec.get("leverage"), "recommendation.leverage", 1)
    if rec["entry"] <= 0 or rec["stop_loss"] <= 0:
        raise ValueError("UPSTREAM_PRICE")
    if not (rec["stop_loss"] < rec["entry"] if rec["direction"] == "long" else rec["stop_loss"] > rec["entry"]):
        raise ValueError("UPSTREAM_STOP_GEOMETRY")
    for key in ("prob_tp1", "prob_tp2"):
        if rec.get(key) is not None:
            _number(rec[key], key, 0)
            if rec[key] > 1:
                raise ValueError("UPSTREAM_PROBABILITY_RANGE")
    _closed(request["context"], {"regime", "filler_test_done"}, "context")
    if request["context"]["regime"] is not None and not isinstance(request["context"]["regime"], dict):
        raise ValueError("UPSTREAM_REGIME_CONTEXT")
    _int(request["context"]["filler_test_done"], "filler_test_done")
    _closed(request["observations"], {"equity", "symbol_learning", "edge_decay"}, "observations")
    for name, obs in request["observations"].items():
        _observation(obs, name, request["decision_at_ms"])
    validate_producer_bindings(request)
    return copy.deepcopy(request)


def make_request(*, config, recommendation, context, observations, decision_at_ms, opportunity_id,
                 source="TEST_ONLY_OBSERVED_EVENTS", source_provenance=None, nominal_producer=None, filler_batch=None):
    material = {"recommendation": recommendation, "context": context, "observations": observations}
    for key, value in (("nominal_producer", nominal_producer), ("filler_batch", filler_batch)):
        if value is not None:
            material[key] = value
    if source_provenance is None and source == "TEST_ONLY_OBSERVED_EVENTS":
        source_provenance = {"producer": "TEST_ONLY_FIXTURE", "source_id": "TEST_ONLY",
            "source_hash": digest(material),
            "quality": "TEST_ONLY", "model_version": None, "synthetic": True}
    body = {"version": VERSION, "source": source,
        "test_only": source == "TEST_ONLY_OBSERVED_EVENTS" or bool((source_provenance or {}).get("synthetic")),
        "source_provenance": source_provenance,
        "source_fingerprint": source_fingerprint(), "decision_at_ms": decision_at_ms,
        "opportunity_id": opportunity_id, "recommendation": recommendation, "config": config,
        "context": context, "observations": observations,
        **{k: material[k] for k in ("nominal_producer", "filler_batch") if k in material}}
    return validate_request({**copy.deepcopy(body), "request_hash": digest(body)})


def validate_report(report, request):
    _closed(report, REPORT_FIELDS, "report")
    _number(report["raw_risk_pct"], "report.raw_risk_pct", 0)
    if (not isinstance(report, dict) or report.get("version") != REPORT_VERSION
            or report.get("request_hash") != request["request_hash"]
            or report.get("source_fingerprint") != request["source_fingerprint"]
            or report.get("input_scope") != INPUT_SCOPE
            or report.get("test_only") is not request["test_only"]
            or report.get("source") != request["source"]
            or report.get("opportunity_id") != request["opportunity_id"]
            or report.get("raw_risk_pct") != request["recommendation"]["risk_pct"]
            or report.get("runtime_identity") != execution.runtime_identity()
            or report.get("producer_refs") != validate_producer_bindings(request)
            or any(report.get(k) is not False for k in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"))
            or report.get("report_hash") != digest({k:v for k,v in report.items() if k != "report_hash"})):
        raise ValueError("UPSTREAM_REPORT_BOUNDARY_DRIFT")
    if report.get("status") not in {"COMPLETE_SIZING", "REJECTED", "TECHNICAL_GAP"}:
        raise ValueError("UPSTREAM_REPORT_STATUS")
    if report["status"] == "COMPLETE_SIZING":
        for key in ("final_risk_pct", "qty", "notional"):
            _number(report.get(key), key, 0)
        if report["qty"] <= 0 or report.get("gaps"):
            raise ValueError("UPSTREAM_INCOMPLETE_CANNOT_BE_COMPLETE")
        expected_notional = report["qty"] * request["recommendation"]["entry"]
        if abs(report["notional"] - expected_notional) > max(1e-9, abs(expected_notional) * 1e-10):
            raise ValueError("UPSTREAM_NOTIONAL_DRIFT")
    policy = report["declared_policy"]
    if (not isinstance(policy, dict) or policy.get("stage") != INPUT_SCOPE
            or any(policy.get(k) is not False for k in ("eligibility_replayed", "cache_writers_replayed", "cash_account_reproduced"))
            or policy.get("learned_cache_order_preserved") is not True
            or policy.get("native_region_ast_hash") != digest(ast.dump(ast.Module(body=native_slice(), type_ignores=[]),
                                                                       include_attributes=False))):
        raise ValueError("UPSTREAM_NATIVE_POLICY_DRIFT")
    return copy.deepcopy(report)


def replay(request):
    request = validate_request(request)
    process = subprocess.run([sys.executable, "-I", "-B", str(WORKER), str(BACKEND)],
        input=canonical(request), capture_output=True, text=True, timeout=30,
        env={"PYTHONHASHSEED": "0"}, cwd=str(BACKEND), check=False)
    if process.returncode:
        raise ValueError("UPSTREAM_ISOLATED_CALLER_FAILED")
    report = validate_report(json.loads(process.stdout), request)
    if source_fingerprint() != request["source_fingerprint"]:
        raise ValueError("UPSTREAM_SOURCE_CHANGED_DURING_REPLAY")
    return report
