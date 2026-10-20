"""Native filler context over a cold, ordered research batch.

Only the sort, allowlist/filler route and post-persistence counter statements
are executed. Initial DB-reader outputs and trade-return receipts are explicit
observations, not an assertion of real persistence or a replay of all gates.
No LIVE caller imports this module and no result grants entry authority.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

from services import live_event_execution_service as execution

VERSION = "CW_LIVE_FILLER_BATCH_REQUEST_V1"
REPORT_VERSION = "CW_LIVE_FILLER_BATCH_REPORT_V1"
INPUT_SCOPE = "FILLER_ROUTE_AND_POST_PERSISTENCE_COUNTERS_ONLY"
DECLARED_MODEL = "CW_DECLARED_FILLER_BATCH_INPUTS_V1"
BACKEND = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("services/shadow_trade_service.py", "services/live_filler_batch_service.py")
CONFIG_FIELDS = {"SHADOW_ENABLED", "FILLER_FORA_ENABLED", "FILLER_FORA_OFF_AT",
                 "FILLER_FORA_MAX", "FILLER_TOTAL_SLOTS", "FILLER_FORA_TEST_N"}
INITIAL_PRODUCERS = {"allowlist": "shadow_trade_service.get_exec_allowlist",
                     "counts": "shadow_trade_service._open_auto_counts_by_allowlist",
                     "brake": "shadow_trade_service._filler_fora_brake_state",
                     "test_done": "shadow_trade_service._filler_fora_test_opened_count"}
OBS_FIELDS = {"producer", "generation", "observed_at_ms", "available_at_ms", "quality", "value", "observation_hash"}
FIELDS = {"version", "input_scope", "source", "test_only", "source_fingerprint", "batch_id",
          "decision_at_ms", "config", "recommendations", "observations", "history", "source_provenance", "request_hash"}
REPORT_FIELDS = {"version", "input_scope", "source", "test_only", "source_fingerprint", "request_hash",
                 "runtime_identity", "batch_id", "batch_identity", "status", "reason_code", "opportunity_id",
                 "recommendation", "route", "initial_state", "state_before", "ordered_opportunities",
                 "transitions", "gaps", "native_regions", "declared_policy", "authorizes_live", "promotable",
                 "live_equivalent", "real_study_allowed", "report_hash"}
POLICY = {"stage": INPUT_SCOPE, "initial_readers_replayed": False, "trade_persistence_replayed": False,
          "eligibility_replayed": False, "scanner_replayed": False, "cash_account_reproduced": False,
          "completion_scope": "OBSERVED_RETURN_BOUND_TRANSITION", "ordered_batch_preserved": True}

canonical = execution.canonical
digest = execution.digest


def _closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("FILLER_SCHEMA:" + name)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("FILLER_INTEGER:" + name)


def _text(value, name):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 255:
        raise ValueError("FILLER_TEXT:" + name)


def source_fingerprint():
    return digest({"files": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest() for name in SOURCE_FILES},
                   "runtime": execution.runtime_identity()})


def _assigns(node, name):
    return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)


def native_regions():
    """Select unchanged native statements by structure, never by line number."""
    tree = ast.parse((BACKEND / SOURCE_FILES[0]).read_text())
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    caller = functions["open_shadow_for_recs"]
    loop = next(n for n in caller.body if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == "rec")
    body = loop.body[0].body
    gate_start = [i for i, n in enumerate(body) if _assigns(n, "_exec_allow")]
    if len(gate_start) != 1:
        raise ValueError("FILLER_NATIVE_GATE_BOUNDARY")
    gate = body[gate_start[0]:gate_start[0] + 2]
    if len(gate) != 2 or not isinstance(gate[1], ast.If):
        raise ValueError("FILLER_NATIVE_GATE_BOUNDARY")
    sort_blocks = []
    for outer in caller.body:
        if not isinstance(outer, ast.If):
            continue
        for i, child in enumerate(outer.body):
            if _assigns(child, "_allow_sort"):
                nested = outer.body[i:i + 2]
                if len(nested) != 2 or not isinstance(nested[1], ast.If):
                    raise ValueError("FILLER_NATIVE_SORT_BOUNDARY")
                sort_blocks.append(ast.If(test=copy.deepcopy(outer.test), body=copy.deepcopy(nested), orelse=[]))
    transitions = []
    for node in body:
        if not isinstance(node, ast.If) or ast.unparse(node.test) != "trade is not None":
            continue
        positions = [i for i, item in enumerate(node.body) if isinstance(item, ast.AugAssign)
                     and isinstance(item.target, ast.Name) and item.target.id == "opened"]
        if positions == [1] and len(node.body) > 2:
            transitions.append(ast.If(test=copy.deepcopy(node.test), body=copy.deepcopy(node.body[:3]), orelse=[]))
    if len(sort_blocks) != 1 or len(transitions) != 1:
        raise ValueError("FILLER_NATIVE_REGION_BOUNDARY")
    helpers = [copy.deepcopy(functions[n]) for n in ("get_exec_allowlist", "_symbol_base")]
    regions = {"sort": sort_blocks, "route": copy.deepcopy(gate), "transition": transitions}
    # No import/I/O is permitted in these deliberately pure native regions.
    for nodes in regions.values():
        if any(isinstance(n, (ast.Await, ast.Import, ast.ImportFrom)) for x in nodes for n in ast.walk(x)):
            raise ValueError("FILLER_NATIVE_IO_BOUNDARY_CHANGED")
    return regions, helpers


def native_config_names():
    regions, _ = native_regions()
    return {n.id for rows in regions.values() for row in rows for n in ast.walk(row)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id.isupper()}


def _observation(obs, name, cutoff):
    _closed(obs, OBS_FIELDS, name)
    _text(obs["producer"], name + ".producer")
    for key in ("generation", "observed_at_ms", "available_at_ms"):
        _integer(obs[key], name + "." + key, 1 if key != "generation" else 0)
    if not obs["observed_at_ms"] <= obs["available_at_ms"] <= cutoff:
        raise ValueError("FILLER_OBSERVATION_NOT_CAUSAL:" + name)
    if obs["quality"] not in {"CAPTURED", "UNAVAILABLE"} or (obs["quality"] == "UNAVAILABLE" and obs["value"] is not None):
        raise ValueError("FILLER_OBSERVATION_QUALITY:" + name)
    if obs["observation_hash"] != digest({k: v for k, v in obs.items() if k != "observation_hash"}):
        raise ValueError("FILLER_OBSERVATION_DRIFT:" + name)


def batch_identity(request):
    return digest({k: request[k] for k in ("version", "source", "test_only", "source_fingerprint", "batch_id",
                                          "config", "recommendations", "observations")})


def validate_request(request):
    _closed(request, FIELDS, "request")
    canonical(request)
    execution._credential_free(request)
    if request["version"] != VERSION or request["input_scope"] != INPUT_SCOPE or request["source"] not in execution.SOURCES:
        raise ValueError("FILLER_VERSION_OR_SCOPE")
    if request["source_fingerprint"] != source_fingerprint():
        raise ValueError("FILLER_SOURCE_DRIFT")
    if request["request_hash"] != digest({k: v for k, v in request.items() if k != "request_hash"}):
        raise ValueError("FILLER_REQUEST_DRIFT")
    _text(request["batch_id"], "batch_id")
    _integer(request["decision_at_ms"], "decision_at_ms", 1)
    config = request["config"]
    _closed(config, CONFIG_FIELDS, "config")
    if native_config_names() != CONFIG_FIELDS:
        raise ValueError("FILLER_NATIVE_CONFIG_CHANGED")
    if config["SHADOW_ENABLED"] is not False or type(config["FILLER_FORA_ENABLED"]) is not bool:
        raise ValueError("FILLER_REQUIRES_LITERAL_LIVE_POLICY")
    for name in CONFIG_FIELDS - {"SHADOW_ENABLED", "FILLER_FORA_ENABLED"}:
        _integer(config[name], name)
    rows = request["recommendations"]
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("FILLER_BATCH_ROWS")
    seen = set()
    for row in rows:
        _closed(row, {"opportunity_id", "recommendation"}, "row")
        _text(row["opportunity_id"], "opportunity_id")
        rec = row["recommendation"]
        if row["opportunity_id"] in seen or not isinstance(rec, dict) or "_is_filler" in rec:
            raise ValueError("FILLER_DUPLICATE_OR_PRECLASSIFIED")
        seen.add(row["opportunity_id"])
        _text(rec.get("symbol"), "symbol")
        if rec.get("direction") not in {"long", "short"}:
            raise ValueError("FILLER_SIDE")
    _closed(request["observations"], {"allowlist", "counts", "brake", "test_done"}, "observations")
    initial_clock = request["decision_at_ms"]
    for name, obs in request["observations"].items():
        _observation(obs, name, initial_clock)
        if obs["producer"] != INITIAL_PRODUCERS[name]:
            raise ValueError("FILLER_INITIAL_PRODUCER:" + name)
        if obs["quality"] != "CAPTURED":
            continue
        value = obs["value"]
        if name == "allowlist":
            if (not isinstance(value, list)
                    or any(not isinstance(v, str) or not v or v != v.strip().upper() for v in value)
                    or len(value) != len(set(value))):
                raise ValueError("FILLER_ALLOWLIST")
        elif name == "counts":
            _closed(value, {"inside", "outside"}, "counts")
            for key in value:
                _integer(value[key], "counts." + key)
        elif name == "brake":
            if (not isinstance(value, dict) or type(value.get("blocked")) is not bool
                    or value.get("block_reason") is not None and not isinstance(value["block_reason"], str)):
                raise ValueError("FILLER_BRAKE")
        else:
            _integer(value, "test_done")
    if not isinstance(request["history"], list) or len(request["history"]) > len(rows):
        raise ValueError("FILLER_HISTORY")
    trade_ids, dispatch_ids = set(), set()
    for event in request["history"]:
        _closed(event, {"opportunity_id", "observation"}, "history.row")
        _text(event["opportunity_id"], "history.opportunity_id")
        obs = event["observation"]
        _observation(obs, "completion", initial_clock)
        if obs["producer"] != "shadow_trade_service._open_trade_fail_closed":
            raise ValueError("FILLER_COMPLETION_PRODUCER")
        if obs["quality"] != "CAPTURED":
            continue
        v = obs["value"]
        _closed(v, {"trade_return", "unprotected_incident", "execution_status", "dispatch_id"}, "completion.value")
        if type(v["unprotected_incident"]) is not bool or v["execution_status"] not in {"NO_ENTRY", "OPENED", "INCIDENT", "UNKNOWN"}:
            raise ValueError("FILLER_COMPLETION_STATUS")
        trade = v["trade_return"]
        if v["execution_status"] == "NO_ENTRY" and (trade is not None or v["unprotected_incident"] or v["dispatch_id"] is not None):
            raise ValueError("FILLER_NO_ENTRY_CONTRADICTION")
        if v["execution_status"] in {"OPENED", "INCIDENT"}:
            if not isinstance(trade, dict):
                raise ValueError("FILLER_PERSISTENCE_MISSING")
            _integer(trade.get("id"), "trade.id", 1)
            if trade.get("source") != "auto" or trade.get("status") != "open" or trade.get("side") not in {"long", "short"}:
                raise ValueError("FILLER_TRADE_COHORT")
            _text(trade.get("symbol"), "trade.symbol")
            _text(v["dispatch_id"], "dispatch_id")
            if (trade.get("client_order_id") != v["dispatch_id"] or trade["id"] in trade_ids
                    or v["dispatch_id"] in dispatch_ids):
                raise ValueError("FILLER_PERSISTENCE_DISPATCH_OR_DUPLICATE")
            trade_ids.add(trade["id"])
            dispatch_ids.add(v["dispatch_id"])
            if v["unprotected_incident"] is not (v["execution_status"] == "INCIDENT"):
                raise ValueError("FILLER_INCIDENT_CONTRADICTION")
    p = request["source_provenance"]
    _closed(p, {"producer", "source_id", "source_hash", "quality", "model_version", "synthetic"}, "source_provenance")
    for key in ("producer", "source_id", "source_hash"):
        _text(p[key], key)
    if (type(p["synthetic"]) is not bool or type(request["test_only"]) is not bool
            or request["test_only"] is not (request["source"] == "TEST_ONLY_OBSERVED_EVENTS" or p["synthetic"])
            or p["quality"] != execution.SOURCES[request["source"]]
            or p["source_hash"] != digest({k: request[k] for k in ("recommendations", "observations", "history")})):
        raise ValueError("FILLER_PROVENANCE_DRIFT")
    expected_model = DECLARED_MODEL if request["source"] == "DECLARED_EVENT_TABLE" else None
    if p["model_version"] != expected_model:
        raise ValueError("FILLER_SOURCE_MODEL")
    return copy.deepcopy(request)


def make_request(*, batch_id, config, recommendations, observations, decision_at_ms, history=None,
                 source="TEST_ONLY_OBSERVED_EVENTS", source_provenance=None):
    history = [] if history is None else history
    if source_provenance is None and source == "TEST_ONLY_OBSERVED_EVENTS":
        source_provenance = {"producer": "TEST_ONLY_FIXTURE", "source_id": "TEST_ONLY", "synthetic": True,
            "quality": "TEST_ONLY", "model_version": None,
            "source_hash": digest({"recommendations": recommendations, "observations": observations, "history": history})}
    body = {"version": VERSION, "input_scope": INPUT_SCOPE, "source": source,
            "test_only": source == "TEST_ONLY_OBSERVED_EVENTS" or bool((source_provenance or {}).get("synthetic")),
            "source_fingerprint": source_fingerprint(), "batch_id": batch_id, "decision_at_ms": decision_at_ms,
            "config": config, "recommendations": recommendations, "observations": observations, "history": history,
            "source_provenance": source_provenance}
    return validate_request({**copy.deepcopy(body), "request_hash": digest(body)})


class _SilentLog:
    def info(self, *_args, **_kwargs):
        pass
    warning = critical = info


class _LogOnlyUnknown:
    """Satisfy native incident log formatting without inventing financial qty."""
    def __format__(self, _spec):
        return "NOT_EXPORTED"


def _namespace(request):
    regions, helpers = native_regions()
    allow = request["observations"]["allowlist"]
    namespace = {"__name__": "_research_filler_native", "_EFFECTIVE_ALLOWLIST": set(allow["value"] or []),
                 "log": _SilentLog(), **request["config"]}
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[])),
                 str(BACKEND / SOURCE_FILES[0]), "exec"), namespace)
    return namespace, regions


def _run_statements(nodes, namespace):
    # The one-item loop retains native continue behavior. No imports/awaits are
    # accepted by native_regions(), and only pure helpers/sinks are in globals.
    tree = ast.fix_missing_locations(ast.Module(body=[ast.For(target=ast.Name(id="_once", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant(True)], ctx=ast.Load()), body=copy.deepcopy(nodes), orelse=[])], type_ignores=[]))
    exec(compile(tree, str(BACKEND / SOURCE_FILES[0]), "exec"), namespace)


def _state(namespace):
    return {"inside": namespace["_flr_n_dentro"], "outside": namespace["_flr_n_fora"],
            "test_done": namespace["_flr_test_done"], "opened": namespace["opened"]}


def replay(request):
    request = validate_request(request)
    namespace, regions = _namespace(request)
    observations = request["observations"]
    filler_on = request["config"]["FILLER_FORA_ENABLED"]
    test_on = filler_on and request["config"]["FILLER_FORA_TEST_N"] > 0
    required = {"allowlist"} | ({"counts", "brake"} if filler_on else set()) | ({"test_done"} if test_on else set())
    gaps = [{"reason_code": "FILLER_OBSERVATION_UNAVAILABLE", "observation": n}
            for n in sorted(required) if observations[n]["quality"] != "CAPTURED"]
    counts = observations["counts"]["value"] if not gaps and filler_on else {"inside": 0, "outside": 0}
    test_done = observations["test_done"]["value"] if not gaps and test_on else 0
    namespace.update(_flr_n_dentro=counts["inside"], _flr_n_fora=counts["outside"], _flr_test_done=test_done,
                     _flr_brake=(observations["brake"]["value"] or {"blocked": True}), opened=0)
    initial = _state(namespace) if not gaps else None
    recs = [copy.deepcopy(row["recommendation"]) for row in request["recommendations"]]
    by_object = {id(rec): row["opportunity_id"] for rec, row in zip(recs, request["recommendations"])}
    namespace["recs"] = recs
    if not gaps:
        _run_statements(regions["sort"], namespace)
    ordered = [by_object[id(rec)] for rec in namespace["recs"]] if not gaps else []
    transitions, next_id, next_rec, next_route = [], None, None, None
    previous_at = max((observations[n]["available_at_ms"] for n in required), default=0)
    for ordinal, (opp, rec) in enumerate(zip(ordered, namespace["recs"])):
        skips = []
        namespace.update(rec=rec, _record_skip=lambda _r, code, reason: skips.append({"code": code, "reason": reason}))
        before = _state(namespace)
        _run_statements(regions["route"], namespace)
        route = {"allowed": not skips, "skip_reasons": skips}
        if ordinal >= len(request["history"]):
            next_id, next_rec, next_route = opp, copy.deepcopy(rec), route
            break
        event = request["history"][ordinal]
        if event["opportunity_id"] != opp:
            raise ValueError("FILLER_HISTORY_ORDER_OR_DUPLICATE")
        obs = event["observation"]
        if obs["observed_at_ms"] < previous_at:
            raise ValueError("FILLER_HISTORY_CLOCK_REVERSED")
        previous_at = obs["available_at_ms"]
        if obs["quality"] != "CAPTURED" or obs["value"]["execution_status"] == "UNKNOWN":
            gaps.append({"reason_code": "FILLER_COMPLETION_UNKNOWN", "opportunity_id": opp})
            break
        value, trade = obs["value"], obs["value"]["trade_return"]
        if not route["allowed"] and value["execution_status"] != "NO_ENTRY":
            raise ValueError("FILLER_BLOCKED_BUT_OPENED")
        if trade is not None and (trade["symbol"] != rec["symbol"] or trade["side"] != rec["direction"]):
            raise ValueError("FILLER_PERSISTENCE_IDENTITY_DRIFT")
        namespace.update(trade=trade, _track_unprotected_incident=value["unprotected_incident"],
                         qty=_LogOnlyUnknown(), _incident_safety_state="RESEARCH_INCIDENT")
        _run_statements(regions["transition"], namespace)
        transitions.append({"opportunity_id": opp, "generation_before": ordinal, "generation_after": ordinal + 1,
            "observation_hash": obs["observation_hash"], "before": before, "after": _state(namespace),
            "execution_status": value["execution_status"], "dispatch_id": value["dispatch_id"]})
    if len(request["history"]) > len(ordered) and not gaps:
        raise ValueError("FILLER_HISTORY_BEYOND_BATCH")
    status = "TECHNICAL_GAP" if gaps else "BATCH_EXHAUSTED" if next_id is None else "COMPLETE_CONTEXT"
    if gaps:
        next_id, next_rec, next_route = None, None, None
    report = {"version": REPORT_VERSION, "input_scope": INPUT_SCOPE, "source": request["source"],
        "test_only": request["test_only"], "source_fingerprint": request["source_fingerprint"],
        "request_hash": request["request_hash"], "runtime_identity": execution.runtime_identity(),
        "batch_id": request["batch_id"], "batch_identity": batch_identity(request), "status": status,
        "reason_code": gaps[0]["reason_code"] if gaps else status, "opportunity_id": next_id,
        "recommendation": next_rec, "route": next_route, "initial_state": initial,
        "state_before": _state(namespace) if not gaps else None, "ordered_opportunities": ordered,
        "transitions": transitions, "gaps": gaps,
        "native_regions": {name: digest(ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False))
                           for name, nodes in regions.items()}, "declared_policy": copy.deepcopy(POLICY),
        "authorizes_live": False, "promotable": False, "live_equivalent": False, "real_study_allowed": False}
    return {**report, "report_hash": digest(report)}


def validate_report(report, request, *, recompute=True):
    request = validate_request(request)
    _closed(report, REPORT_FIELDS, "report")
    if (report["version"] != REPORT_VERSION or report["input_scope"] != INPUT_SCOPE
            or report["request_hash"] != request["request_hash"] or report["source_fingerprint"] != request["source_fingerprint"]
            or report["runtime_identity"] != execution.runtime_identity() or report["batch_id"] != request["batch_id"]
            or report["batch_identity"] != batch_identity(request) or report["source"] != request["source"]
            or report["test_only"] is not request["test_only"] or report["declared_policy"] != POLICY
            or any(report[k] is not False for k in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"))
            or report["report_hash"] != digest({k: v for k, v in report.items() if k != "report_hash"})):
        raise ValueError("FILLER_REPORT_BOUNDARY_DRIFT")
    if recompute and report != replay(request):
        raise ValueError("FILLER_REPORT_RECOMPUTE_DRIFT")
    return copy.deepcopy(report)
