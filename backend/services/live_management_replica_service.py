"""Research-only, event-driven replica using isolated LIVE management callers.

No production caller imports this module. This is not R10A OHLCV, financial
evidence, a study approval, or an order licence. The child process receives
explicit observations and memory-only ports; it never receives credentials.
Unknown observations are recorded even when LIVE's fail-soft paths catch them.
"""
from __future__ import annotations

import copy
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import subprocess
import sys

ENGINE_VERSION = "CW_LIVE_MANAGEMENT_EVENT_REPLICA_V1"
SOURCES = {"SYNTHETIC_TEST_ONLY", "OPERATOR_EVENT_TRACE"}
MAX_TICKS = 2000
MAX_INPUT_BYTES = 16 * 1024 * 1024
BACKEND = Path(__file__).resolve().parents[1]
WORKER = BACKEND / "scripts" / "live_management_replica_worker.py"
SOURCE_FILES = (
    "services/trade_manager_service.py", "services/indicator_service.py",
    "services/entry_planner.py", "models/real_trade.py",
    "models/trade_signal.py", "models/recommendation_snapshot.py",
    "services/live_management_replica_service.py",
    "scripts/live_management_replica_worker.py",
)
BOOL_CONFIG = {
    "TIME_STOP_ENABLED", "PROTECTION_AUTOHEAL_ENABLED", "PROTECTION_VERIFY_LIVE",
    "BE_STRUCTURAL_ENABLED", "PRE_TP1_PROTECT_ENABLED", "RUNNER_ENABLED",
}
INT_CONFIG = {
    "TIME_STOP_SCALP_MIN", "TIME_STOP_DAY_MIN", "TIME_STOP_SWING_MIN", "RUNNER_LOOKBACK",
}
NUMBER_CONFIG = {
    "PROTECTION_HEAL_GRACE_SECONDS", "PROTECTION_TP1_QTY_PCT", "TRADE_MANAGER_DUST_USD",
    "BE_STRUCT_ATR_BUFFER", "BE_MAX_GIVEBACK_R", "PRE_TP1_PROTECT_TRIGGER_FRAC",
    "PRE_TP1_PROTECT_LOCK_R", "RUNNER_QTY_PCT", "RUNNER_DETECT_FRAC",
    "RUNNER_ATR_MULT", "RUNNER_MIN_STEP_ATR",
}
CONFIG_FIELDS = BOOL_CONFIG | INT_CONFIG | NUMBER_CONFIG | {"RUNNER_ATR_TF_FALLBACK"}
STATE_FIELDS = {
    "id", "symbol", "side", "source", "exchange", "status", "phase", "qty",
    "qty_initial", "entry_price", "opened_at_ms", "created_at_ms", "client_order_id",
    "planned_stop", "planned_tp1",
    "planned_tp2", "sl_order_id", "tp1_order_id", "tp2_order_id", "sl_current_price",
    "adaptive_tp1_qty_pct", "adaptive_runner_atr_mult", "adaptive_runner_qty_pct",
    "notes", "timeframe",
}
PORTS = {
    "ownership_guard", "get_positions", "get_open_algo_orders", "place_protection_orders",
    "cancel_algo_order", "place_order", "get_executions", "fetch_ticker", "fetch_ohlcv",
    "cleanup_orphan_conditionals", "close_trade",
}
REQUEST_FIELDS = {"engine_version", "source", "binding_mode", "source_fingerprint", "config",
                  "initial_state", "ticks", "trace_hash"}
TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h",
              "1d", "3d", "1w", "1M"}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def source_fingerprint():
    """Local code identity, not an approval or signature; no ENV/DB/network."""
    return digest({"files": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
                              for name in SOURCE_FILES}, "runtime": runtime_identity()})


def runtime_identity():
    return {"python": list(sys.version_info[:3]),
            "packages": {name: metadata.version(name)
                         for name in ("sqlalchemy", "pandas", "numpy", "ta",
                                      "pydantic", "pydantic_core")}}


def call_hash(port, args, kwargs):
    return digest({"port": port, "args": list(args), "kwargs": kwargs})


def _closed(value, fields, context):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"REPLICA_CLOSED_SCHEMA:{context}")


def _number(value, name, minimum=0, nullable=False):
    if value is None and nullable:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"REPLICA_INVALID_NUMBER:{name}")


def _integer(value, name, minimum=0, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not int or value < minimum:
        raise ValueError(f"REPLICA_INVALID_INTEGER:{name}")


def _text(value, name, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError(f"REPLICA_INVALID_TEXT:{name}")


def _json_safe(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_safe(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            if any(term in key.lower() for term in ("api_key", "api_secret", "admin_token", "password")):
                raise ValueError("REPLICA_CREDENTIAL_FIELD_FORBIDDEN")
            _json_safe(item)
        return
    raise ValueError("REPLICA_NON_JSON_OR_NON_FINITE")


def _observed_value(port, value, stamp):
    """Validate relevant normalized inputs; do not synthesize missing readings."""
    if port == "get_positions" and isinstance(value, dict):
        if type(value.get("ok")) is not bool:
            raise ValueError("REPLICA_POSITION_QUALITY_MISSING")
        if value["ok"] is True and not isinstance(value.get("positions"), list):
            raise ValueError("REPLICA_POSITION_COMPLETENESS_MISSING")
        for position in value.get("positions") or []:
            if not isinstance(position, dict):
                raise ValueError("REPLICA_POSITION_FORMAT")
            _number(position.get("size"), "position.size")
            if position["size"] > 0:
                _text(position.get("symbol"), "position.symbol")
                if str(position.get("side", "")).lower() not in ("buy", "sell", "long", "short"):
                    raise ValueError("REPLICA_POSITION_SIDE_MISSING")
            if "entry_price" in position:
                _number(position["entry_price"], "position.entry_price", nullable=True)
    if port == "fetch_ticker" and isinstance(value, dict) and "last" in value:
        _number(value["last"], "ticker.last", nullable=True)
    if port == "fetch_ohlcv" and value is not None:
        _closed(value, {"columns"}, "ohlcv")
        columns = value["columns"]
        _closed(columns, {"timestamp", "open", "high", "low", "close", "volume"}, "ohlcv.columns")
        lengths = {len(v) for v in columns.values() if isinstance(v, list)}
        if any(not isinstance(v, list) for v in columns.values()) or len(lengths) != 1:
            raise ValueError("REPLICA_OHLCV_SHAPE")
        if len(columns["timestamp"]) > 4096:
            raise ValueError("REPLICA_OHLCV_LIMIT")
        previous = -1
        for i, ts in enumerate(columns["timestamp"]):
            _integer(ts, "ohlcv.timestamp")
            if ts <= previous or ts > stamp:
                raise ValueError("REPLICA_OHLCV_FUTURE_OR_ORDER")
            previous = ts
            for key in ("open", "high", "low", "close", "volume"):
                _number(columns[key][i], f"ohlcv.{key}")
            if (columns["low"][i] > min(columns["open"][i], columns["close"][i])
                    or columns["high"][i] < max(columns["open"][i], columns["close"][i])):
                raise ValueError("REPLICA_OHLCV_GEOMETRY")


def validate_request(request, *, expected_source_fingerprint=None):
    """Pure closed-contract validation, before any caller can run."""
    _closed(request, REQUEST_FIELDS, "request")
    _json_safe(request)
    if request["engine_version"] != ENGINE_VERSION or request["source"] not in SOURCES:
        raise ValueError("REPLICA_VERSION_OR_SOURCE")
    if request["binding_mode"] not in ("BOUND", "SYNTHETIC_PREVIEW"):
        raise ValueError("REPLICA_BINDING_MODE")
    preview = request["binding_mode"] == "SYNTHETIC_PREVIEW"
    if preview and request["source"] != "SYNTHETIC_TEST_ONLY":
        raise ValueError("REPLICA_PREVIEW_NOT_OPERATOR_EVIDENCE")
    fingerprint = request["source_fingerprint"]
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)):
        raise ValueError("REPLICA_SOURCE_IDENTITY")
    if expected_source_fingerprint is not None and fingerprint != expected_source_fingerprint:
        raise ValueError("REPLICA_SOURCE_DRIFT")
    if request["trace_hash"] != digest({k: v for k, v in request.items() if k != "trace_hash"}):
        raise ValueError("REPLICA_TRACE_DRIFT")
    config, state = request["config"], request["initial_state"]
    _closed(config, CONFIG_FIELDS, "config")
    for key in BOOL_CONFIG:
        if type(config[key]) is not bool:
            raise ValueError(f"REPLICA_LITERAL_BOOL:{key}")
    for key in INT_CONFIG:
        _integer(config[key], key, 1)
    for key in NUMBER_CONFIG:
        _number(config[key], key)
    for key in ("PROTECTION_TP1_QTY_PCT", "RUNNER_QTY_PCT", "RUNNER_DETECT_FRAC"):
        if not 0 < config[key] < 1:
            raise ValueError(f"REPLICA_FRACTION:{key}")
    if config["RUNNER_ATR_MULT"] <= 0 or config["RUNNER_ATR_TF_FALLBACK"] not in TIMEFRAMES:
        raise ValueError("REPLICA_RUNNER_CONFIG")
    _closed(state, STATE_FIELDS, "initial_state")
    _integer(state["id"], "trade.id", 1)
    _text(state["symbol"], "trade.symbol")
    if (state["side"] not in ("long", "short") or state["exchange"] != "binance"
            or state["source"] not in ("auto", "managed") or state["status"] != "open"
            or state["phase"] not in ("pre_tp1", "post_tp1", "runner")):
        raise ValueError("REPLICA_TRADE_SCOPE")
    for key in ("qty", "entry_price", "planned_stop", "planned_tp1", "planned_tp2"):
        _number(state[key], f"trade.{key}", minimum=1e-15)
    _number(state["qty_initial"], "trade.qty_initial", minimum=1e-15, nullable=True)
    _number(state["sl_current_price"], "trade.sl_current_price", minimum=1e-15, nullable=True)
    _integer(state["opened_at_ms"], "trade.opened_at_ms", nullable=True)
    _integer(state["created_at_ms"], "trade.created_at_ms", nullable=True)
    for key in ("sl_order_id", "tp1_order_id", "tp2_order_id", "client_order_id"):
        _text(state[key], key, nullable=True)
    if state["notes"] is not None and (not isinstance(state["notes"], str) or len(state["notes"]) > 255):
        raise ValueError("REPLICA_INVALID_NOTES")
    for key in ("adaptive_tp1_qty_pct", "adaptive_runner_atr_mult", "adaptive_runner_qty_pct"):
        _number(state[key], key, nullable=True)
    if state["timeframe"] is not None and state["timeframe"] not in TIMEFRAMES:
        raise ValueError("REPLICA_TIMEFRAME")
    e, sl, tp1, tp2 = (state[k] for k in ("entry_price", "planned_stop", "planned_tp1", "planned_tp2"))
    if not (sl < e < tp1 < tp2 if state["side"] == "long" else sl > e > tp1 > tp2):
        raise ValueError("REPLICA_INITIAL_GEOMETRY")
    ticks, previous, seen, call_ids = request["ticks"], -1, set(), set()
    if not isinstance(ticks, list) or not 0 < len(ticks) <= MAX_TICKS:
        raise ValueError("REPLICA_TICK_LIMIT")
    for tick in ticks:
        _closed(tick, {"event_id", "at_ms", "ports"}, "tick")
        _text(tick["event_id"], "event_id")
        _integer(tick["at_ms"], "at_ms", 1)
        if tick["event_id"] in seen or tick["at_ms"] <= previous:
            raise ValueError("REPLICA_EVENT_DUPLICATE_OR_ORDER")
        if state["opened_at_ms"] is not None and state["opened_at_ms"] > tick["at_ms"]:
            raise ValueError("REPLICA_OPENED_IN_FUTURE")
        if state["created_at_ms"] is not None and state["created_at_ms"] > tick["at_ms"]:
            raise ValueError("REPLICA_CREATED_IN_FUTURE")
        previous = tick["at_ms"]
        seen.add(tick["event_id"])
        if not isinstance(tick["ports"], dict) or set(tick["ports"]) - PORTS:
            raise ValueError("REPLICA_PORT_NOT_SUPPORTED")
        for port, responses in tick["ports"].items():
            if not isinstance(responses, list) or len(responses) > 50:
                raise ValueError("REPLICA_RESPONSE_LIMIT")
            last = -1
            for response in responses:
                _closed(response, {"observed_at_ms", "available_at_ms", "value",
                                   "call_index", "request_hash", "after_action_index"}, "response")
                if preview:
                    if any(response[k] is not None for k in
                           ("call_index", "request_hash", "after_action_index")):
                        raise ValueError("REPLICA_PREVIEW_RECEIPT_NOT_UNBOUND")
                else:
                    ci, ai, rh = (response[k] for k in
                                  ("call_index", "after_action_index", "request_hash"))
                    _integer(ci, "call_index", 1)
                    _integer(ai, "after_action_index")
                    if ai >= ci or ci in call_ids:
                        raise ValueError("REPLICA_RECEIPT_CAUSAL_ID")
                    if (not isinstance(rh, str) or len(rh) != 64
                            or any(c not in "0123456789abcdef" for c in rh)):
                        raise ValueError("REPLICA_RECEIPT_REQUEST_HASH")
                    call_ids.add(ci)
                lo, hi = response["observed_at_ms"], response["available_at_ms"]
                _integer(lo, "observed_at_ms")
                _integer(hi, "available_at_ms")
                if not lo <= hi <= tick["at_ms"] or hi < last:
                    raise ValueError("REPLICA_RESPONSE_FUTURE_OR_ORDER")
                last = hi
                _observed_value(port, response["value"], lo)
    if len(_canonical(request).encode()) > MAX_INPUT_BYTES:
        raise ValueError("REPLICA_INPUT_SIZE_LIMIT")
    return copy.deepcopy(request)


def make_request(*, config, initial_state, ticks, source="SYNTHETIC_TEST_ONLY",
                 binding_mode="BOUND"):
    body = {"engine_version": ENGINE_VERSION, "source": source,
            "binding_mode": binding_mode,
            "source_fingerprint": source_fingerprint(), "config": copy.deepcopy(config),
            "initial_state": copy.deepcopy(initial_state), "ticks": copy.deepcopy(ticks)}
    return validate_request({**body, "trace_hash": digest(body)})


def run_trace(request):
    """Manual offline invocation only; never called by API, scan or scheduler."""
    request = validate_request(request, expected_source_fingerprint=source_fingerprint())
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(WORKER), str(BACKEND)],
        input=_canonical(request), capture_output=True, text=True, timeout=30,
        env={"PYTHONHASHSEED": "0"}, cwd=str(BACKEND), check=False,
    )
    if result.returncode != 0:
        raise ValueError("REPLICA_ISOLATED_CALLER_FAILED")
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError("REPLICA_REPORT_INVALID") from None
    if (report.get("trace_hash") != request["trace_hash"]
            or report.get("source_fingerprint") != request["source_fingerprint"]
            or report.get("engine_version") != ENGINE_VERSION
            or report.get("authorizes_live") is not False
            or report.get("promotable") is not False
            or report.get("live_equivalent") is not False
            or report.get("economics") is not None):
        raise ValueError("REPLICA_REPORT_BOUNDARY_INVALID")
    if (report.get("binding_mode") != request["binding_mode"]
            or report.get("runtime_identity") != runtime_identity()
            or source_fingerprint() != request["source_fingerprint"]):
        raise ValueError("REPLICA_SOURCE_DRIFT_DURING_EXECUTION")
    _json_safe(report)
    return report


if __name__ == "__main__":
    # Explicit offline stdin -> stdout. No input/output file, no default config.
    try:
        print(_canonical(run_trace(json.load(sys.stdin))))
    except (ValueError, subprocess.TimeoutExpired):
        print(_canonical({"ok": False, "reason_code": "REPLICA_INPUT_OR_EXECUTION_BLOCKED",
                          "authorizes_live": False, "promotable": False}))
        raise SystemExit(1)
