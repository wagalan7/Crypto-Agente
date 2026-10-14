"""One-shot, receipt-bound sizing replay; never an exchange client or scheduler."""
from __future__ import annotations

import asyncio
import ast
import copy
import json
import logging
import math
from pathlib import Path
import socket
import sys
import types


REPORT_VERSION = "CW_LIVE_UPSTREAM_SIZING_REPORT_V1"
INPUT_SCOPE = "AFTER_EXISTING_ELIGIBILITY_BEFORE_SIZING"
SHADOW_HELPERS = {
    "_conviction_mult", "_edge_mult", "_funding_ev_r", "_get_rec_feature",
    "_compute_qty", "_exec_size_damp", "_short_slip_guard", "_liq_tier_mult",
    "_regime_size_mult", "_sizing_stack_report", "_resolve_equity_usd",
}
SYMBOL_HELPERS = {
    "_base_of", "_finite", "_count", "_bounded", "_rank_limits",
    "_size_limits", "_checked_field", "_row_numbers", "get_size_mult",
}
EDGE_HELPERS = {"_b", "_f", "_i", "_cfg", "is_enabled", "get_mult"}
EDGE_ENV = {
    "EDGE_DECAY_ENABLED": "enabled", "EDGE_DECAY_WINDOW_DAYS": "window_days",
    "EDGE_DECAY_RECENT_DAYS": "recent_days", "EDGE_DECAY_MIN_SAMPLE": "min_sample",
    "EDGE_DECAY_R_FLOOR": "r_floor", "EDGE_DECAY_R_FULL": "r_full",
    "EDGE_DECAY_BASE_MIN": "base_min", "EDGE_DECAY_MULT_MIN": "mult_min",
    "EDGE_DECAY_TTL_SEC": "ttl_sec",
}


class BoundaryGap(Exception):
    pass


class Runtime:
    def __init__(self, payload):
        self.payload = payload
        self.gaps, self.steps, self.decisions, self.calls = [], [], [], []
        self.blocked_network = []
        self._faults = set()

    def gap(self, code, **details):
        row = {"reason_code": code, **details}
        if row not in self.gaps:
            self.gaps.append(row)

    def observation(self, name, *, required=True):
        envelope = self.payload["observations"][name]
        if envelope["quality"] != "CAPTURED":
            if required:
                self.gap("SIZING_OBSERVATION_UNAVAILABLE", observation=name)
            return None
        self.calls.append({"port": name, "observation_hash": envelope["observation_hash"],
                           "generation": envelope["generation"],
                           "observed_at_ms": envelope["observed_at_ms"],
                           "available_at_ms": envelope["available_at_ms"]})
        return copy.deepcopy(envelope["value"])

    def source_fault_trace(self, filenames):
        def trace(frame, event, arg):
            if event == "exception" and frame.f_code.co_filename in filenames:
                kind, _value, _tb = arg
                if kind not in (StopIteration, StopAsyncIteration, GeneratorExit):
                    identity = (frame.f_code.co_name, frame.f_lineno, kind.__name__)
                    if identity not in self._faults:
                        self._faults.add(identity)
                        self.gap("SIZING_NATIVE_SOURCE_FAULT", function=identity[0],
                                 source_line=identity[1], exception_type=identity[2])
            return trace
        return trace


def network_guard(runtime):
    original = socket.socket

    def deny(operation, *args, **kwargs):
        runtime.blocked_network.append({"operation": operation})
        runtime.gap("NETWORK_ATTEMPT_BLOCKED", operation=operation)
        raise PermissionError("sizing replica has no external network")

    class OfflineSocket(original):
        def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
            if family != socket.AF_UNIX:
                deny("socket")
            super().__init__(family, type, proto, fileno)

        def connect(self, address):
            deny("connect")

        def connect_ex(self, address):
            deny("connect_ex")

    socket.socket = OfflineSocket
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr"):
        setattr(socket, name, lambda *a, _name=name, **k: deny(_name, *a, **k))


def install_import_guard(runtime):
    import importlib.abc

    class Guard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if (fullname.startswith("services.") or fullname.startswith("models.")
                    or fullname == "db" or fullname.split(".")[0] in
                    {"httpx", "requests", "aiohttp", "asyncpg", "psycopg", "ccxt"}):
                runtime.gap("OFFLINE_IMPORT_BLOCKED", module=fullname)
                raise ImportError(fullname)
            return None

    sys.meta_path.insert(0, Guard())


def compile_functions(path, names, namespace):
    parsed = ast.parse(path.read_text(), filename=str(path))
    nodes = [n for n in parsed.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    if {n.name for n in nodes} != set(names):
        raise BoundaryGap("SIZING_NATIVE_FUNCTION_MISSING")
    tree = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *nodes], type_ignores=[]))
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


def sizing_slice(path):
    """Select native statements structurally, preserving their exact AST bodies."""
    parsed = ast.parse(path.read_text(), filename=str(path))
    fn = next((n for n in parsed.body if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "open_shadow_for_recs"), None)
    if fn is None:
        raise BoundaryGap("SIZING_NATIVE_ENTRYPOINT_MISSING")
    loops = [n for n in fn.body if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == "rec"]
    if len(loops) != 1 or len(loops[0].body) != 1 or not isinstance(loops[0].body[0], ast.Try):
        raise BoundaryGap("SIZING_NATIVE_LOOP_BOUNDARY_CHANGED")
    body = loops[0].body[0].body
    starts = [i for i, n in enumerate(body) if isinstance(n, ast.Assign)
              and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
              and n.targets[0].id == "entry"]
    if len(starts) != 1:
        raise BoundaryGap("SIZING_NATIVE_START_BOUNDARY_CHANGED")
    start = starts[0]
    ends = [i for i, n in enumerate(body) if i > start and isinstance(n, ast.Try)
            and any(isinstance(q, ast.Assign) and any(isinstance(a, ast.Name)
                    and a.id == "open_notional" for a in q.targets) for q in n.body)]
    if len(ends) != 1:
        raise BoundaryGap("SIZING_NATIVE_END_BOUNDARY_CHANGED")
    selected = body[start:ends[0]]
    assignments = {target.id for n in selected for q in ast.walk(n)
                   if isinstance(q, ast.Assign) for target in q.targets if isinstance(target, ast.Name)}
    if not {"entry", "stop", "risk_pct", "qty", "sizing", "notional_effective", "lev"} <= assignments:
        raise BoundaryGap("SIZING_NATIVE_OUTPUT_BOUNDARY_CHANGED")
    return selected


def compile_slice(path, namespace):
    selected = sizing_slice(path)
    outputs = ast.parse("return {'entry': entry, 'stop': stop, 'final_risk_pct': risk_pct, "
                        "'qty': qty, 'notional': notional_effective, 'sizing': sizing, "
                        "'leverage': lev, 'equity_usd': equity_usd, 'equity_source': equity_src}").body
    loop = ast.For(target=ast.Name(id="rec", ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Name(id="recommendation", ctx=ast.Load())], ctx=ast.Load()),
                   body=[*copy.deepcopy(selected), *outputs], orelse=[])
    wrapper = ast.AsyncFunctionDef(
        name="_run_native_sizing_region",
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="recommendation")],
                           kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[loop, ast.Return(value=ast.Constant(value=None))], decorator_list=[])
    tree = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        wrapper], type_ignores=[]))
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace[wrapper.name], selected


def install_module(name, namespace):
    mod = types.ModuleType(name)
    mod.__dict__.update(namespace)
    sys.modules[name] = mod
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, mod)
    return mod


def observe_helper(runtime, namespace, name):
    actual = namespace[name]

    def call(*args, **kwargs):
        value = actual(*args, **kwargs)
        runtime.steps.append({"caller": name, "source_file": actual.__code__.co_filename,
                              "source_line": actual.__code__.co_firstlineno,
                              "result": copy.deepcopy(value)})
        return value

    namespace[name] = call


async def run(payload, backend, contract):
    payload = contract.validate_request(payload)
    if payload["source_fingerprint"] != contract.source_fingerprint():
        raise BoundaryGap("SIZING_NATIVE_SOURCE_DRIFT")
    runtime = Runtime(payload)
    network_guard(runtime)
    log = logging.getLogger("offline_upstream_sizing")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    package = types.ModuleType("services")
    package.__path__ = []
    sys.modules["services"] = package
    shadow_path = backend / "services/shadow_trade_service.py"
    sls_path = backend / "services/symbol_learning_service.py"
    edge_path = backend / "services/edge_decay_service.py"
    regime_path = backend / "services/regime_service.py"

    shadow_config = copy.deepcopy(payload["config"]["shadow"])
    live_region = not shadow_config["SHADOW_ENABLED"]
    symbol_config = copy.deepcopy(payload["config"]["symbol_learning"])
    symbol_rows = runtime.observation("symbol_learning", required=live_region and symbol_config["SYMBOL_LEARNING_SIZE_ENABLED"])
    symbol_cache = {}
    for item in symbol_rows or []:
        symbol_cache.setdefault(item["base"], {})[item["timeframe"]] = copy.deepcopy(item["row"])
    literal = next((n.value for n in ast.parse(sls_path.read_text()).body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_OPTIONAL_METADATA" for t in n.targets)), None)
    if literal is None:
        raise BoundaryGap("SIZING_SYMBOL_METADATA_CONTRACT_CHANGED")
    sls = compile_functions(sls_path, SYMBOL_HELPERS, {
        "__name__": "services.symbol_learning_service", "__file__": str(sls_path),
        "math": math, "log": log, "_CACHE": symbol_cache,
        "_OPTIONAL_METADATA": ast.literal_eval(literal), **symbol_config})
    install_module("services.symbol_learning_service", sls)

    decay_config = copy.deepcopy(payload["config"]["edge_decay"])
    decay_rows = runtime.observation("edge_decay", required=live_region and decay_config["enabled"])
    decay_cache = {(r["symbol"], r["direction"]): copy.deepcopy(r["row"]) for r in decay_rows or []}

    def frozen_getenv(name, default=None):
        if name not in EDGE_ENV:
            runtime.gap("SIZING_UNBOUND_CONFIGURATION_READ", configuration=name)
            raise BoundaryGap("unbound configuration")
        value = decay_config[EDGE_ENV[name]]
        return ("true" if value else "false") if type(value) is bool else str(value)

    decay = compile_functions(edge_path, EDGE_HELPERS, {
        "__name__": "services.edge_decay_service", "__file__": str(edge_path),
        "os": types.SimpleNamespace(getenv=frozen_getenv), "_CACHE": decay_cache, "log": log})
    install_module("services.edge_decay_service", decay)
    regime = compile_functions(regime_path, {"is_btc_symbol"}, {
        "__name__": "services.regime_service", "__file__": str(regime_path)})
    install_module("services.regime_service", regime)

    async def get_equity():
        value = runtime.observation("equity")
        if value is None:
            raise BoundaryGap("equity unavailable")
        return value

    install_module("services.exchange_service", {"get_equity": get_equity})

    def decision(rec, kind, code=None):
        runtime.decisions.append({"decision": kind, "reason_code": code})

    def skip(rec, gate, reason):
        # Source-controlled text is metadata, not an invented gate decision.
        runtime.decisions.append({"decision": "REJECTED", "gate": gate, "reason": str(reason)})

    if live_region and shadow_config["REGIME_SIZING_ENABLED"] and payload["context"]["regime"] is None:
        runtime.gap("SIZING_REGIME_CONTEXT_UNAVAILABLE")
    namespace = {
        "__name__": "services.shadow_trade_service", "log": log, "math": math,
        "_regime_cached": copy.deepcopy(payload["context"]["regime"]),
        "_flr_test_done": payload["context"]["filler_test_done"],
        "_record_skip": skip, "_observe_decision": decision, **shadow_config}
    compile_functions(shadow_path, SHADOW_HELPERS, namespace)
    for name in SHADOW_HELPERS - {"_get_rec_feature", "_resolve_equity_usd"}:
        observe_helper(runtime, namespace, name)
    native, selected = compile_slice(shadow_path, namespace)
    install_import_guard(runtime)
    filenames = {str(p) for p in (shadow_path, sls_path, edge_path, regime_path)}
    sys.settrace(runtime.source_fault_trace(filenames))
    result = None
    try:
        result = await native(copy.deepcopy(payload["recommendation"]))
    except Exception as exc:
        runtime.gap("SIZING_NATIVE_CALLER_EXCEPTION", exception_type=type(exc).__name__)
    finally:
        sys.settrace(None)
    if result is not None:
        for field in ("entry", "stop", "final_risk_pct", "qty", "notional", "equity_usd"):
            value = result[field]
            if type(value) not in (int, float) or not math.isfinite(value):
                runtime.gap("SIZING_NATIVE_OUTPUT_NOT_FINITE", field=field)
        if result["equity_usd"] != payload["observations"]["equity"]["value"].get("total_usd"):
            runtime.gap("SIZING_EQUITY_OBSERVATION_DIVERGENT")
    if contract.source_fingerprint() != payload["source_fingerprint"]:
        runtime.gap("SIZING_NATIVE_SOURCE_DRIFT")
    status = "TECHNICAL_GAP" if runtime.gaps else "COMPLETE_SIZING" if result is not None else "REJECTED"
    rejection = runtime.decisions[-1] if runtime.decisions else {}
    reason = (runtime.gaps[0]["reason_code"] if runtime.gaps else
              rejection.get("reason_code") or rejection.get("gate") or "SIZING_REJECTED")
    report = {
        "version": REPORT_VERSION, "status": status,
        "reason_code": None if status == "COMPLETE_SIZING" else reason,
        "input_scope": INPUT_SCOPE, "request_hash": payload["request_hash"],
        "runtime_identity": contract.execution.runtime_identity(),
        "source_fingerprint": payload["source_fingerprint"], "source": payload["source"],
        "test_only": payload["test_only"], "opportunity_id": payload["opportunity_id"],
        "raw_risk_pct": payload["recommendation"].get("risk_pct"),
        "final_risk_pct": result["final_risk_pct"] if result else None,
        "qty": result["qty"] if result else None,
        "notional": result["notional"] if result else None,
        "sizing": result["sizing"] if result else None,
        "steps": runtime.steps, "observations_used": runtime.calls,
        "decisions": runtime.decisions, "gaps": runtime.gaps,
        "declared_policy": {
            "stage": INPUT_SCOPE, "source_file": str(shadow_path),
            "source_first_line": selected[0].lineno,
            "source_last_line": selected[-1].end_lineno,
            "native_region_ast_hash": contract.digest(ast.dump(ast.Module(body=selected, type_ignores=[]),
                                                               include_attributes=False)),
            "eligibility_replayed": False, "cache_writers_replayed": False,
            "learned_cache_order_preserved": True,
            "recommendation_risk_default_observed": payload["recommendation"].get("risk_pct") == 0,
            "cash_account_reproduced": False},
        "authorizes_live": False, "promotable": False, "live_equivalent": False,
        "real_study_allowed": False, "blocked_network_attempts": runtime.blocked_network,
        "isolation": {"input_credentials_inherited": False, "real_environment_inherited": False,
                      "actual_exchange_clients_imported": False, "database": "none"},
    }
    # Never put NaN/Infinity on stdout, even when native behavior produced it.
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value
    report = safe(report)
    return {**report, "report_hash": contract.digest(report)}


def main():
    try:
        backend = Path(sys.argv[1]).resolve()
        package = types.ModuleType("services")
        package.__path__ = []
        sys.modules["services"] = package
        execution_path = backend / "services/live_event_execution_service.py"
        execution = types.ModuleType("services.live_event_execution_service")
        execution.__file__ = str(execution_path)
        sys.modules[execution.__name__] = execution
        exec(compile(execution_path.read_bytes(), str(execution_path), "exec"), execution.__dict__)
        package.live_event_execution_service = execution
        path = backend / "services/live_upstream_sizing_service.py"
        contract = types.ModuleType("_upstream_sizing_contract")
        contract.__file__ = str(path)
        sys.modules[contract.__name__] = contract
        exec(compile(path.read_bytes(), str(path), "exec"), contract.__dict__)
        payload = json.loads(sys.stdin.read())
        result = asyncio.run(run(payload, backend, contract))
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "reason_code": "SIZING_ISOLATED_WORKER_FAILED",
                          "exception_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
