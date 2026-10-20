"""One-shot offline nominal-sizing consumer of original source functions."""
from __future__ import annotations

import ast
import copy
import json
import logging
from pathlib import Path
import socket
import sys
import types


def network_guard(attempts):
    original = socket.socket

    def deny(operation, *args, **kwargs):
        attempts.append({"operation": operation})
        raise PermissionError("nominal research has no external network")

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
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, lambda *a, _name=name, **kw: deny(_name, *a, **kw))


def install_import_guard():
    import importlib.abc

    class Guard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if (fullname.startswith("services.") or fullname.startswith("models.") or fullname == "db"
                    or fullname.split(".")[0] in {"httpx", "requests", "aiohttp", "asyncpg", "psycopg", "ccxt", "dotenv"}):
                raise ImportError("offline nominal import blocked: " + fullname)
            return None

    sys.meta_path.insert(0, Guard())


def compile_functions(path, names):
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {node.name for node in nodes} != set(names):
        raise ValueError("NOMINAL_NATIVE_FUNCTION_DRIFT")
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes]
    namespace = {"__name__": "_nominal_source", "__file__": str(path)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)
    return namespace


def run(payload, backend, contract):
    payload = contract.validate_request(payload)
    attempts, gaps, faults = [], [], set()
    network_guard(attempts)
    native = contract.native_functions()
    proofs = {name: {**value, "calls": 0} for name, value in native.items()}
    package = types.ModuleType("services")
    package.__path__ = []
    sys.modules["services"] = package
    regime = types.ModuleType("services.regime_service")
    regime_path = backend / "services/regime_service.py"
    recommendation_path = backend / "services/recommendation_service.py"
    regime.__dict__.update(compile_functions(regime_path, {"is_btc_symbol"}))
    regime.__name__ = "services.regime_service"
    sys.modules[regime.__name__] = regime
    package.regime_service = regime
    namespace = compile_functions(recommendation_path, {"_compute_leverage", "_apply_btc_correlation_throttle"})

    def traced(name, actual):
        def call(*args, **kwargs):
            proofs[name]["calls"] += 1
            return actual(*args, **kwargs)
        return call

    regime.is_btc_symbol = traced("is_btc_symbol", regime.is_btc_symbol)
    for name in ("_compute_leverage", "_apply_btc_correlation_throttle"):
        namespace[name] = traced(name, namespace[name])
    install_import_guard()
    paths = {str(regime_path), str(recommendation_path)}

    def trace(frame, event, arg):
        if event == "exception" and frame.f_code.co_filename in paths:
            exc, _value, _tb = arg
            if exc not in (StopIteration, StopAsyncIteration, GeneratorExit):
                identity = (frame.f_code.co_name, frame.f_lineno, exc.__name__)
                if identity not in faults:
                    faults.add(identity)
                    gaps.append({"reason_code": "NOMINAL_NATIVE_SOURCE_FAULT", "function": identity[0],
                                 "source_line": identity[1], "exception_type": identity[2]})
        return trace

    obs = payload["regime_observation"]
    outputs = [{"opportunity_id": row["opportunity_id"], "recommendation": None} for row in payload["recommendations"]]
    if obs["quality"] != "CAPTURED":
        gaps.append({"reason_code": "NOMINAL_REGIME_UNAVAILABLE"})
    elif obs["value"].get("btc_24h_pct") is None:
        gaps.append({"reason_code": "NOMINAL_BTC_24H_UNKNOWN"})
    if not gaps:
        objects = []
        sys.settrace(trace)
        try:
            for row in payload["recommendations"]:
                rec = copy.deepcopy(row["recommendation"])
                result = namespace["_compute_leverage"](rec["entry"], rec["stop_loss"], rec["tier"])
                rec.update(risk_pct=result["risk_pct"], leverage=result["leverage"], margin_pct=result["margin_pct"],
                           stop_distance_pct=result["stop_dist_pct"])
                rec.setdefault("warnings", [])
                objects.append(types.SimpleNamespace(**rec))
            if payload["producer_path"] in contract.THROTTLE_PATHS:
                namespace["_apply_btc_correlation_throttle"](objects, copy.deepcopy(obs["value"]))
            if not gaps:
                outputs = [{"opportunity_id": original["opportunity_id"], "recommendation": vars(rec)}
                           for original, rec in zip(payload["recommendations"], objects)]
        except Exception as exc:
            gaps.append({"reason_code": "NOMINAL_NATIVE_CALL_FAILED", "exception_type": type(exc).__name__})
        finally:
            sys.settrace(None)
    if attempts:
        gaps.append({"reason_code": "NOMINAL_NETWORK_ATTEMPT_BLOCKED"})
    if gaps:
        outputs = [{"opportunity_id": row["opportunity_id"], "recommendation": None} for row in payload["recommendations"]]
    body = {"version": contract.REPORT_VERSION, "input_scope": contract.INPUT_SCOPE,
            "status": "TECHNICAL_GAP" if gaps else "COMPLETE_NOMINAL",
            "reason_code": gaps[0]["reason_code"] if gaps else "NOMINAL_NATIVE_COMPLETED",
            **{key: payload[key] for key in ("source", "test_only", "source_fingerprint", "request_hash", "batch_id", "generation", "producer_path")},
            "runtime_identity": contract.execution.runtime_identity(), "recommendations": outputs,
            "native_proofs": [proofs[name] for name in sorted(proofs)],
            "observation_used": {key: obs[key] for key in ("producer", "generation", "observed_at_ms", "available_at_ms", "quality", "observation_hash")},
            "gaps": gaps, "declared_policy": contract.POLICY, "authorizes_live": False, "promotable": False,
            "live_equivalent": False, "real_study_allowed": False, "blocked_network_attempts": attempts,
            "isolation": {"network": "BLOCKED", "app_imports": "BLOCKED", "environment": "EMPTY"}}
    return {**body, "report_hash": contract.digest(body)}


def main():
    try:
        backend = Path(sys.argv[1]).resolve()
        # Bootstrap only the pure research contracts. Never add the app to sys.path.
        package = types.ModuleType("services")
        package.__path__ = []
        sys.modules["services"] = package
        execution_path = backend / "services/live_event_execution_service.py"
        execution = types.ModuleType("services.live_event_execution_service")
        execution.__file__ = str(execution_path)
        sys.modules[execution.__name__] = execution
        exec(compile(execution_path.read_bytes(), str(execution_path), "exec"), execution.__dict__)
        package.live_event_execution_service = execution
        path = backend / "services/live_nominal_sizing_service.py"
        contract = types.ModuleType("_nominal_contract")
        contract.__file__ = str(path)
        sys.modules[contract.__name__] = contract
        exec(compile(path.read_bytes(), str(path), "exec"), contract.__dict__)
        logging.disable(logging.CRITICAL)
        result = run(json.loads(sys.stdin.read()), backend, contract)
        print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "reason_code": "NOMINAL_ISOLATED_WORKER_FAILED", "exception_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
