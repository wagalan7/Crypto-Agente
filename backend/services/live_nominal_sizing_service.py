"""Source-bound nominal risk/leverage research, before portfolio filtering.

The input is the complete observed post-build batch, not proof that the scanner
or its eligibility/score/tier producers have been reconstructed. No LIVE caller
imports this module; no result grants operational authority.
"""
from __future__ import annotations

import ast
import __future__
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import types

from services import live_event_execution_service as execution

VERSION = "CW_LIVE_NOMINAL_SIZING_REQUEST_V1"
REPORT_VERSION = "CW_LIVE_NOMINAL_SIZING_REPORT_V1"
INPUT_SCOPE = "POST_BUILD_PRE_THROTTLE_PRE_PORTFOLIO"
DECLARED_MODEL = "CW_DECLARED_NOMINAL_SIZING_INPUTS_V1"
BACKEND = Path(__file__).resolve().parents[1]
WORKER = BACKEND / "scripts/live_nominal_sizing_worker.py"
SOURCE_FILES = ("services/recommendation_service.py", "services/regime_service.py",
                "services/live_nominal_sizing_service.py", "scripts/live_nominal_sizing_worker.py")
PRODUCER_PATHS = {"get_recommendations_via_vision", "get_recommendations_from_batch", "get_recommendations"}
THROTTLE_PATHS = PRODUCER_PATHS - {"get_recommendations"}
OUTPUT_FIELDS = {"risk_pct", "leverage", "margin_pct", "stop_distance_pct"}
FORBIDDEN_INPUT_FIELDS = OUTPUT_FIELDS | {"stop_dist_pct"}
OBSERVATION_FIELDS = {"producer", "generation", "observed_at_ms", "available_at_ms", "quality", "value", "observation_hash"}
FIELDS = {"version", "input_scope", "source", "test_only", "source_fingerprint", "decision_at_ms",
          "batch_id", "generation", "producer_path", "recommendations", "regime_observation", "source_provenance", "request_hash"}
REPORT_FIELDS = {"version", "input_scope", "status", "reason_code", "source", "test_only", "source_fingerprint",
                 "request_hash", "runtime_identity", "batch_id", "generation", "producer_path", "recommendations",
                 "native_proofs", "observation_used", "gaps", "declared_policy", "authorizes_live", "promotable",
                 "live_equivalent", "real_study_allowed", "blocked_network_attempts", "isolation", "report_hash"}
PROOF_FIELDS = {"function", "source_file", "source_line", "function_ast_hash", "calls"}
POLICY = {"stage": INPUT_SCOPE, "batch_scope": "OBSERVED_COMPLETE_POST_BUILD_BATCH",
          "scanner_replayed": False, "eligibility_replayed": False, "scores_replayed": False,
          "tiers_replayed": False, "portfolio_filter_replayed": False, "cache_writers_replayed": False,
          "cash_account_reproduced": False, "batch_completeness_independently_verified": False}


def canonical(value):
    return execution.canonical(value)


def digest(value):
    return execution.digest(value)


def _closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("NOMINAL_SCHEMA:" + name)


def _number(value, name, minimum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError("NOMINAL_NUMBER:" + name)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("NOMINAL_INTEGER:" + name)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 255:
        raise ValueError("NOMINAL_TEXT:" + name)


def native_functions():
    files = {"services/recommendation_service.py": {"_compute_leverage", "_apply_btc_correlation_throttle"},
             "services/regime_service.py": {"is_btc_symbol"}}
    result = {}
    for relative, names in files.items():
        tree = ast.parse((BACKEND / relative).read_text())
        found = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names}
        if set(found) != names:
            raise ValueError("NOMINAL_NATIVE_FUNCTION_DRIFT")
        for name, node in found.items():
            result[name] = {"function": name, "source_file": relative, "source_line": node.lineno,
                            "function_ast_hash": digest(ast.dump(node, include_attributes=False))}
        if relative == "services/recommendation_service.py":
            _validate_native_callers(tree)
    return result


def _validate_native_callers(tree):
    """The source functions must still occupy the stage represented by the API."""
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def calls(node, name):
        return [item for item in ast.walk(node) if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name) and item.func.id == name]

    builder = functions.get("_build_recommendation")
    if builder is None or len(calls(builder, "_compute_leverage")) != 1:
        raise ValueError("NOMINAL_NATIVE_BUILDER_DRIFT")
    returns = calls(builder, "Recommendation")
    if len(returns) != 1:
        raise ValueError("NOMINAL_NATIVE_FIELD_COPY_DRIFT")
    copied = {item.arg: item.value for item in returns[0].keywords}
    for output, inner in (("leverage", "leverage"), ("risk_pct", "risk_pct"),
                          ("margin_pct", "margin_pct"), ("stop_distance_pct", "stop_dist_pct")):
        value = copied.get(output)
        if not (isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name)
                and value.value.id == "lev" and isinstance(value.slice, ast.Constant) and value.slice.value == inner):
            raise ValueError("NOMINAL_NATIVE_FIELD_COPY_DRIFT")
    assignments = [item for item in ast.walk(builder) if isinstance(item, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "lev" for target in item.targets)]
    expected = ast.parse("_compute_leverage(sig.entry, sig.stop_loss, tier)", mode="eval").body
    if len(assignments) != 1 or ast.dump(assignments[0].value, include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise ValueError("NOMINAL_NATIVE_BUILDER_INPUT_DRIFT")
    for path in PRODUCER_PATHS:
        caller = functions.get(path)
        if caller is None or len(calls(caller, "_build_recommendation")) != 1:
            raise ValueError("NOMINAL_NATIVE_CALLER_DRIFT")
        throttles = calls(caller, "_apply_btc_correlation_throttle")
        if len(throttles) != int(path in THROTTLE_PATHS):
            raise ValueError("NOMINAL_NATIVE_THROTTLE_PATH_DRIFT")
        sort = [item for item in ast.walk(caller) if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute) and item.func.attr == "sort"
                and isinstance(item.func.value, ast.Name) and item.func.value.id == "recommendations"]
        guard = calls(caller, "_apply_portfolio_guard")
        if len(sort) != 1 or len(guard) != 1 or sort[0].lineno >= guard[0].lineno:
            raise ValueError("NOMINAL_NATIVE_BATCH_STAGE_DRIFT")
        if throttles and not calls(caller, "_build_recommendation")[0].lineno < throttles[0].lineno < sort[0].lineno:
            raise ValueError("NOMINAL_NATIVE_BATCH_STAGE_DRIFT")


def source_fingerprint():
    return digest({"files": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest() for name in SOURCE_FILES},
                   "runtime": execution.runtime_identity()})


def _native_outputs(request):
    """Independent verifier calls the SAME source bodies, never a copied formula.

    Only builtin arithmetic/object operations and the exact source-backed major
    helper are exposed. Imports cannot load app modules, clients or the network.
    """
    counts = {name: 0 for name in native_functions()}
    allowed = {name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
               for name in ("abs", "bool", "dict", "Exception", "float", "int", "len", "list", "max", "min", "round", "str")}
    logger = types.SimpleNamespace(info=lambda *a, **kw: None, warning=lambda *a, **kw: None)
    regime = types.SimpleNamespace()

    def limited_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not level and name == "services.regime_service" and tuple(fromlist) == ("is_btc_symbol",):
            return regime
        if not level and name == "logging":
            return logger
        raise ImportError("NOMINAL_VERIFIER_IMPORT_FORBIDDEN")

    allowed["__import__"] = limited_import

    def load(relative, names):
        parsed = ast.parse((BACKEND / relative).read_text())
        nodes = [n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name in names]
        if {n.name for n in nodes} != set(names):
            raise ValueError("NOMINAL_NATIVE_FUNCTION_DRIFT")
        namespace = {"__builtins__": allowed}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), relative, "exec",
                     flags=__future__.annotations.compiler_flag), namespace)
        return namespace

    def counted(name, actual):
        def call(*args, **kwargs):
            counts[name] += 1
            return actual(*args, **kwargs)
        return call

    major = load("services/regime_service.py", {"is_btc_symbol"})["is_btc_symbol"]
    regime.is_btc_symbol = counted("is_btc_symbol", major)
    native = load("services/recommendation_service.py", {"_compute_leverage", "_apply_btc_correlation_throttle"})
    values = []
    for row in request["recommendations"]:
        rec = copy.deepcopy(row["recommendation"])
        result = counted("_compute_leverage", native["_compute_leverage"])(rec["entry"], rec["stop_loss"], rec["tier"])
        rec.update(risk_pct=result["risk_pct"], leverage=result["leverage"], margin_pct=result["margin_pct"],
                   stop_distance_pct=result["stop_dist_pct"])
        rec.setdefault("warnings", [])
        values.append(types.SimpleNamespace(**rec))
    if request["producer_path"] in THROTTLE_PATHS:
        counted("_apply_btc_correlation_throttle", native["_apply_btc_correlation_throttle"])(values, copy.deepcopy(request["regime_observation"]["value"]))
    outputs = [{"opportunity_id": row["opportunity_id"], "recommendation": vars(rec)}
               for row, rec in zip(request["recommendations"], values)]
    return outputs, counts


def _validate_recommendations(rows):
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("NOMINAL_BATCH_ROWS")
    seen = set()
    for item in rows:
        _closed(item, {"opportunity_id", "recommendation"}, "batch.row")
        _text(item["opportunity_id"], "opportunity_id")
        if item["opportunity_id"] in seen:
            raise ValueError("NOMINAL_DUPLICATE_OPPORTUNITY")
        seen.add(item["opportunity_id"])
        rec = item["recommendation"]
        if not isinstance(rec, dict) or not {"symbol", "timeframe", "direction", "tier", "entry", "stop_loss"} <= set(rec):
            raise ValueError("NOMINAL_RECOMMENDATION_FIELDS")
        if FORBIDDEN_INPUT_FIELDS & set(rec):
            raise ValueError("NOMINAL_PRECOMPUTED_OUTPUT_FORBIDDEN")
        for key in ("symbol", "timeframe"):
            _text(rec[key], key)
        # Validation does not normalize the raw symbol used by the actual helper.
        symbol = rec["symbol"]
        if not re.fullmatch(r"[A-Za-z0-9]+(?:/(?:USDT|USDC)(?::(?:USDT|USDC))?|-(?:USDT|USDC)(?:-SWAP)?|(?:USDT|USDC))", symbol):
            raise ValueError("NOMINAL_SYMBOL_QUOTE")
        plain = symbol.split(":")[0]
        quote = "USDT" if plain.endswith("USDT") or "-USDT-" in plain else "USDC"
        if ":" in symbol and symbol.rsplit(":", 1)[1] != quote:
            raise ValueError("NOMINAL_SYMBOL_SETTLEMENT_MISMATCH")
        if "quote" in rec and rec["quote"] != quote:
            raise ValueError("NOMINAL_QUOTE_MISMATCH")
        if not re.fullmatch(r"[1-9][0-9]*(?:m|h|d|w)", rec["timeframe"]):
            raise ValueError("NOMINAL_TIMEFRAME")
        if rec["direction"] not in {"long", "short"} or rec["tier"] not in {"A+", "A", "B"}:
            raise ValueError("NOMINAL_SIDE_OR_TIER")
        for key in ("entry", "stop_loss"):
            _number(rec[key], key, 0)
            if rec[key] <= 0:
                raise ValueError("NOMINAL_POSITIVE_PRICE")
        long = rec["direction"] == "long"
        if not (rec["stop_loss"] < rec["entry"] if long else rec["stop_loss"] > rec["entry"]):
            raise ValueError("NOMINAL_STOP_GEOMETRY")
        for key in ("tp1", "tp2"):
            if key in rec and rec[key] is not None:
                _number(rec[key], key, 0)
                if not (rec[key] > rec["entry"] if long else rec[key] < rec["entry"]):
                    raise ValueError("NOMINAL_TARGET_GEOMETRY")
        for key in ("prob_tp1", "prob_tp2", "confidence"):
            if key in rec and rec[key] is not None:
                _number(rec[key], key, 0)
                if rec[key] > 1:
                    raise ValueError("NOMINAL_PROBABILITY_RANGE")
        if "score" in rec:
            _number(rec["score"], "score", 0)
            if rec["score"] > 100:
                raise ValueError("NOMINAL_SCORE_RANGE")
        if "warnings" in rec and (not isinstance(rec["warnings"], list) or not all(isinstance(w, str) for w in rec["warnings"])):
            raise ValueError("NOMINAL_WARNINGS")


def _validate_observation(obs, decision):
    _closed(obs, OBSERVATION_FIELDS, "regime_observation")
    _text(obs["producer"], "regime.producer")
    _integer(obs["generation"], "regime.generation")
    for key in ("observed_at_ms", "available_at_ms"):
        _integer(obs[key], "regime." + key, 1)
    if not obs["observed_at_ms"] <= obs["available_at_ms"] <= decision:
        raise ValueError("NOMINAL_REGIME_NOT_CAUSAL")
    if obs["quality"] not in {"CAPTURED", "UNAVAILABLE"}:
        raise ValueError("NOMINAL_REGIME_QUALITY")
    if obs["observation_hash"] != digest({k: v for k, v in obs.items() if k != "observation_hash"}):
        raise ValueError("NOMINAL_REGIME_OBSERVATION_DRIFT")
    if obs["quality"] == "UNAVAILABLE":
        if obs["value"] is not None:
            raise ValueError("NOMINAL_UNKNOWN_REGIME_HAS_VALUE")
        return
    if not isinstance(obs["value"], dict):
        raise ValueError("NOMINAL_REGIME_VALUE")
    if obs["value"].get("btc_24h_pct") is not None:
        _number(obs["value"]["btc_24h_pct"], "btc_24h_pct")


def validate_request(request):
    _closed(request, FIELDS, "request")
    canonical(request)
    execution._credential_free(request)
    if request["version"] != VERSION or request["input_scope"] != INPUT_SCOPE:
        raise ValueError("NOMINAL_VERSION_OR_SCOPE")
    if request["source"] not in execution.SOURCES or request["producer_path"] not in PRODUCER_PATHS:
        raise ValueError("NOMINAL_SOURCE_OR_CALLER")
    if request["source_fingerprint"] != source_fingerprint():
        raise ValueError("NOMINAL_SOURCE_DRIFT")
    if request["request_hash"] != digest({k: v for k, v in request.items() if k != "request_hash"}):
        raise ValueError("NOMINAL_REQUEST_DRIFT")
    _integer(request["decision_at_ms"], "decision_at_ms", 1)
    _integer(request["generation"], "generation")
    _text(request["batch_id"], "batch_id")
    _validate_recommendations(request["recommendations"])
    _validate_observation(request["regime_observation"], request["decision_at_ms"])
    provenance = request["source_provenance"]
    _closed(provenance, {"producer", "source_id", "source_hash", "quality", "model_version", "synthetic"}, "source_provenance")
    for key in ("producer", "source_id", "source_hash"):
        _text(provenance[key], "source_provenance." + key)
    if (type(request["test_only"]) is not bool or type(provenance["synthetic"]) is not bool
            or request["test_only"] is not (request["source"] == "TEST_ONLY_OBSERVED_EVENTS" or provenance["synthetic"])):
        raise ValueError("NOMINAL_TEST_ONLY_BOUNDARY")
    expected_source = digest({key: request[key] for key in ("recommendations", "regime_observation")})
    if provenance["source_hash"] != expected_source or provenance["quality"] != execution.SOURCES[request["source"]]:
        raise ValueError("NOMINAL_PROVENANCE_DRIFT")
    if request["source"] == "DECLARED_EVENT_TABLE":
        if provenance["model_version"] != DECLARED_MODEL:
            raise ValueError("NOMINAL_DECLARED_MODEL_UNSUPPORTED")
    elif provenance["model_version"] is not None:
        raise ValueError("NOMINAL_SOURCE_FAMILY_MISMATCH")
    return copy.deepcopy(request)


def make_request(*, recommendations, regime_observation, decision_at_ms,
                 source="TEST_ONLY_OBSERVED_EVENTS", source_provenance=None,
                 producer_path="get_recommendations_via_vision", batch_id=None, generation=0):
    if batch_id is None:
        if source != "TEST_ONLY_OBSERVED_EVENTS":
            raise ValueError("NOMINAL_BATCH_ID_REQUIRED")
        batch_id = "TEST_ONLY_BATCH"
    if source_provenance is None and source == "TEST_ONLY_OBSERVED_EVENTS":
        source_provenance = {"producer": "TEST_ONLY_FIXTURE", "source_id": "TEST_ONLY",
            "source_hash": digest({"recommendations": recommendations, "regime_observation": regime_observation}),
            "quality": "TEST_ONLY", "model_version": None, "synthetic": True}
    body = {"version": VERSION, "input_scope": INPUT_SCOPE, "source": source,
            "test_only": source == "TEST_ONLY_OBSERVED_EVENTS" or bool((source_provenance or {}).get("synthetic")),
            "source_fingerprint": source_fingerprint(), "source_provenance": source_provenance,
            "decision_at_ms": decision_at_ms, "batch_id": batch_id, "generation": generation,
            "producer_path": producer_path, "recommendations": recommendations, "regime_observation": regime_observation}
    return validate_request({**copy.deepcopy(body), "request_hash": digest(body)})


def validate_report(report, request):
    request = validate_request(request)
    _closed(report, REPORT_FIELDS, "report")
    canonical(report)
    if report["version"] != REPORT_VERSION or report["input_scope"] != INPUT_SCOPE:
        raise ValueError("NOMINAL_REPORT_VERSION_OR_SCOPE")
    for key in ("source", "test_only", "source_fingerprint", "request_hash", "batch_id", "generation", "producer_path"):
        if type(report[key]) is not type(request[key]) or report[key] != request[key]:
            raise ValueError("NOMINAL_REPORT_BINDING:" + key)
    if report["runtime_identity"] != execution.runtime_identity():
        raise ValueError("NOMINAL_RUNTIME_DRIFT")
    if any(report[key] is not False for key in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed")):
        raise ValueError("NOMINAL_AUTHORITY_FORBIDDEN")
    if report["report_hash"] != digest({k: v for k, v in report.items() if k != "report_hash"}):
        raise ValueError("NOMINAL_REPORT_HASH_DRIFT")
    if report["status"] not in {"COMPLETE_NOMINAL", "TECHNICAL_GAP"}:
        raise ValueError("NOMINAL_REPORT_STATUS")
    if report["declared_policy"] != POLICY:
        raise ValueError("NOMINAL_POLICY_DRIFT")
    if report["blocked_network_attempts"] != [] or report["isolation"] != {"network": "BLOCKED", "app_imports": "BLOCKED", "environment": "EMPTY"}:
        raise ValueError("NOMINAL_ISOLATION_DRIFT")
    expected_proofs = native_functions()
    if not isinstance(report["native_proofs"], list) or len(report["native_proofs"]) != len(expected_proofs):
        raise ValueError("NOMINAL_NATIVE_PROOFS")
    seen = set()
    for proof in report["native_proofs"]:
        _closed(proof, PROOF_FIELDS, "native_proof")
        name = proof["function"]
        if name in seen or name not in expected_proofs or {k: v for k, v in proof.items() if k != "calls"} != expected_proofs[name]:
            raise ValueError("NOMINAL_NATIVE_PROOF_DRIFT")
        seen.add(name)
        _integer(proof["calls"], "native.calls")
    outputs = report["recommendations"]
    if not isinstance(outputs, list) or len(outputs) != len(request["recommendations"]):
        raise ValueError("NOMINAL_OUTPUT_COUNT")
    for item, original in zip(outputs, request["recommendations"]):
        _closed(item, {"opportunity_id", "recommendation"}, "output.row")
        if item["opportunity_id"] != original["opportunity_id"]:
            raise ValueError("NOMINAL_OUTPUT_ORDER")
        rec = item["recommendation"]
        if report["status"] == "TECHNICAL_GAP":
            if rec is not None:
                raise ValueError("NOMINAL_UNKNOWN_CANNOT_PUBLISH_OUTPUT")
            continue
        if not isinstance(rec, dict):
            raise ValueError("NOMINAL_OUTPUT_RECOMMENDATION")
        # The only non-sizing field allowed to change is the native warning list.
        original_rec = original["recommendation"]
        if set(rec) != set(original_rec) | OUTPUT_FIELDS | {"warnings"}:
            raise ValueError("NOMINAL_OUTPUT_FIELDS")
        for key, value in original_rec.items():
            if key != "warnings" and (type(rec[key]) is not type(value) or rec[key] != value):
                raise ValueError("NOMINAL_OUTPUT_INPUT_DRIFT:" + key)
        for key in OUTPUT_FIELDS - {"leverage"}:
            _number(rec[key], "output." + key, 0)
        _integer(rec["leverage"], "output.leverage", 1)
        if rec["risk_pct"] <= 0 or rec["margin_pct"] <= 0:
            raise ValueError("NOMINAL_OUTPUT_POSITIVE")
        if not isinstance(rec["warnings"], list) or not all(isinstance(w, str) for w in rec["warnings"]):
            raise ValueError("NOMINAL_OUTPUT_WARNINGS")
    if report["status"] == "COMPLETE_NOMINAL":
        if report["gaps"] or report["reason_code"] != "NOMINAL_NATIVE_COMPLETED":
            raise ValueError("NOMINAL_COMPLETE_WITH_GAP")
        counts = {p["function"]: p["calls"] for p in report["native_proofs"]}
        if counts["_compute_leverage"] != len(outputs) or counts["_apply_btc_correlation_throttle"] != int(request["producer_path"] in THROTTLE_PATHS):
            raise ValueError("NOMINAL_NATIVE_CALL_COUNT")
        obs = request["regime_observation"]
        if obs["quality"] != "CAPTURED" or obs["value"].get("btc_24h_pct") is None:
            raise ValueError("NOMINAL_COMPLETE_WITH_UNKNOWN_REGIME")
        expected_outputs, expected_counts = _native_outputs(request)
        if canonical(outputs) != canonical(expected_outputs) or counts != expected_counts:
            raise ValueError("NOMINAL_NATIVE_OUTPUT_DRIFT")
    elif not isinstance(report["gaps"], list) or not report["gaps"]:
        raise ValueError("NOMINAL_TECHNICAL_GAP_REQUIRED")
    expected_obs = {key: request["regime_observation"][key] for key in ("producer", "generation", "observed_at_ms", "available_at_ms", "quality", "observation_hash")}
    if report["observation_used"] != expected_obs:
        raise ValueError("NOMINAL_OBSERVATION_BINDING")
    return copy.deepcopy(report)


def replay(request):
    request = validate_request(request)
    process = subprocess.run([sys.executable, "-I", "-B", str(WORKER), str(BACKEND)],
        input=canonical(request), text=True, capture_output=True, check=False, timeout=30,
        env={"PYTHONHASHSEED": "0"}, cwd=str(BACKEND))
    if process.returncode:
        raise ValueError("NOMINAL_ISOLATED_CALLER_FAILED")
    report = validate_report(json.loads(process.stdout), request)
    if source_fingerprint() != request["source_fingerprint"]:
        raise ValueError("NOMINAL_SOURCE_CHANGED_DURING_REPLAY")
    return report
