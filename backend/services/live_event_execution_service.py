"""Closed research economic entry adapter; no LIVE caller imports this module.

The adapter executes source-selected production functions in a credential-free
child. Broker responses are observations, never invented executions. Financial
fills remain separately attributed facts for the event ledger.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

VERSION = "CW_LIVE_EVENT_EXECUTION_V1"
UPDATE_VERSION = "CW_LIVE_EXTERNAL_EXPOSURE_UPDATE_V1"
SOURCES = {"TEST_ONLY_OBSERVED_EVENTS": "TEST_ONLY", "OBSERVED_ACCOUNT_EVENTS": "OBSERVED_ACCOUNT",
           "DECLARED_EVENT_TABLE": "DECLARED_MODEL"}
BACKEND = Path(__file__).resolve().parents[1]
WORKER = BACKEND / "scripts/live_event_execution_worker.py"
SOURCE_FILES = (
    "services/shadow_trade_service.py", "services/entry_intent_service.py",
    "services/entry_revalidation_service.py", "services/binance_signed_service.py", "services/exchange_service.py",
    "models/real_trade.py", "services/live_event_execution_service.py",
    "scripts/live_event_execution_worker.py",
)
CONFIG_BOOL = {"MAKER_ENTRY_ENABLED", "MAKER_FALLBACK_MARKET",
               "P04A_ENTRY_REVALIDATION_ENABLED", "P04B_MARKET_REVALIDATION_ENABLED",
               "P04B_MAKER_FALLBACK_ENABLED", "PYRAMIDING_ENABLED"}
CONFIG_INT = {"MAX_OPEN_POSITIONS", "PYRAMIDING_MAX_LEVEL",
              "BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS"}
CONFIG_NUMBER = {"MIN_NOTIONAL_USD", "MAX_MARGIN_PCT_PER_TRADE", "MAX_RISK_PCT_HARD",
                 "MAX_TOTAL_NOTIONAL_PCT", "LIVE_SIZE_MULT", "MAX_OPEN_RISK_PCT",
                 "DAILY_LOSS_LIMIT_USD", "MARGIN_MAX_AGE_S", "MAKER_ENTRY_TIMEOUT_S",
                 "MAKER_ENTRY_POLL_INTERVAL_S", "BINANCE_MAKER_CANCEL_CONFIRM_DELAY",
                 "PYRAMIDING_SIZE_MULT", "PYRAMIDING_MIN_PROFIT_R", "TP1_QTY_PCT"}
FILTER_FIELDS = {"step", "min_qty", "tick", "market_step", "market_min_qty",
                 "market_max_qty", "min_notional"}
LIMIT_FIELDS = {"max_quote_age_ms", "max_depth_age_ms", "max_fetch_latency_ms",
                "max_spread_pct", "max_book_impact_pct", "max_adverse_slippage_pct",
                "max_chase_atr", "min_rr_tp1", "min_rr_tp2"}
CONFIG_FIELDS = CONFIG_BOOL | CONFIG_INT | CONFIG_NUMBER | {"filters", "limits"}
PROPOSAL_FIELDS = {"opportunity_id", "trade_id", "account_ref", "exchange", "market",
                   "symbol", "quote", "side", "position_side", "timeframe", "dispatch_id",
                   "decision_at_ms", "entry", "stop", "tp1", "tp2", "leverage", "risk_pct",
                   "atr", "entry_zone_low", "entry_zone_high", "sizing_context"}
PORTFOLIO_FIELDS = {"equity_usd", "available_margin_usd", "margin_as_of_ms", "open_positions",
                    "open_risk_usd", "open_notional_usd", "pending_count", "pending_risk_usd",
                    "pending_margin_usd", "daily_base_usd", "complete", "ownership", "paused",
                    "kill_allowed", "live_guards_allowed"}
PYRAMID_FIELDS = {"id", "symbol", "side", "qty", "qty_initial", "entry_price", "planned_stop",
                  "leverage", "phase", "pyramiding_level", "notes", "sl_close_position",
                  "tp2_close_position", "protection_confirmed"}
PORTS = {"quote", "depth", "set_leverage", "send_order", "get_order", "cancel_order",
         "place_protection", "emergency_close", "cleanup"}
RECEIPT_FIELDS = {"port", "call_index", "after_action_index", "request_hash",
                  "observed_at_ms", "available_at_ms", "value"}
REQUEST_FIELDS = {"version", "source", "test_only", "real_study_allowed", "source_fingerprint",
                  "config", "proposal", "portfolio", "receipts", "fills", "mode",
                  "pyramid_state", "request_hash", "source_provenance"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def runtime_identity():
    return {"python": list(sys.version_info[:3]), "sqlalchemy": metadata.version("sqlalchemy")}


def execution_source_fingerprint():
    return digest({"files": {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest()
                              for name in SOURCE_FILES}, "runtime": runtime_identity()})


def call_hash(port, args=(), kwargs=None):
    return digest({"port": port, "args": list(args), "kwargs": kwargs or {}})


def _closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"EVENT_EXECUTION_CLOSED_SCHEMA:{name}")


def _number(value, name, minimum=0, nullable=False):
    if value is None and nullable:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"EVENT_EXECUTION_INVALID_NUMBER:{name}")


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"EVENT_EXECUTION_INVALID_INTEGER:{name}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError(f"EVENT_EXECUTION_INVALID_TEXT:{name}")


def _credential_free(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if any(term in key.lower() for term in ("api_key", "api_secret", "admin_token", "password")):
                raise ValueError("EVENT_EXECUTION_CREDENTIAL_FIELD_FORBIDDEN")
            _credential_free(item)
    elif isinstance(value, list):
        for item in value:
            _credential_free(item)


def validate_execution_config(config):
    _closed(config, CONFIG_FIELDS, "config")
    for key in CONFIG_BOOL:
        if type(config[key]) is not bool:
            raise ValueError(f"EVENT_EXECUTION_LITERAL_BOOL:{key}")
    for key in CONFIG_INT:
        _integer(config[key], key, 1)
    for key in CONFIG_NUMBER:
        _number(config[key], key)
    for key in ("MIN_NOTIONAL_USD", "MAX_MARGIN_PCT_PER_TRADE", "MAX_RISK_PCT_HARD",
                "MAX_TOTAL_NOTIONAL_PCT", "MAX_OPEN_RISK_PCT", "DAILY_LOSS_LIMIT_USD",
                "MARGIN_MAX_AGE_S", "MAKER_ENTRY_POLL_INTERVAL_S", "PYRAMIDING_SIZE_MULT"):
        if config[key] <= 0:
            raise ValueError(f"EVENT_EXECUTION_POSITIVE_CONFIG:{key}")
    if not 0 < config["LIVE_SIZE_MULT"] <= 1 or not 0 < config["TP1_QTY_PCT"] < 1:
        raise ValueError("EVENT_EXECUTION_SIZE_FRACTION")
    _closed(config["filters"], FILTER_FIELDS, "filters")
    for key in FILTER_FIELDS:
        _number(config["filters"][key], key, 1e-15)
    _closed(config["limits"], LIMIT_FIELDS, "limits")
    for key in LIMIT_FIELDS:
        _number(config["limits"][key], key)
    return copy.deepcopy(config)


def validate_proposal(proposal):
    _closed(proposal, PROPOSAL_FIELDS, "proposal")
    for key in ("opportunity_id", "account_ref", "symbol", "quote", "timeframe", "dispatch_id"):
        _text(proposal[key], key)
    _integer(proposal["trade_id"], "trade_id", 1)
    _integer(proposal["decision_at_ms"], "decision_at_ms", 1)
    _integer(proposal["leverage"], "leverage", 1)
    if (proposal["exchange"] != "binance" or proposal["market"] != "usdm_futures"
            or proposal["side"] not in ("long", "short") or proposal["position_side"] != "BOTH"):
        raise ValueError("EVENT_EXECUTION_SCOPE_UNSUPPORTED")
    # The active caller is the one-way USDT-M transport, not USDC or hedge mode.
    if proposal["quote"] != "USDT" or not proposal["symbol"].split(":")[0].endswith("/USDT"):
        raise ValueError("EVENT_EXECUTION_QUOTE_UNSUPPORTED")
    for key in ("entry", "stop", "tp1", "tp2", "risk_pct", "atr"):
        _number(proposal[key], key, 1e-15)
    for key in ("entry_zone_low", "entry_zone_high"):
        _number(proposal[key], key, 1e-15, nullable=True)
    e, s, t1, t2 = (proposal[key] for key in ("entry", "stop", "tp1", "tp2"))
    if not (s < e < t1 < t2 if proposal["side"] == "long" else s > e > t1 > t2):
        raise ValueError("EVENT_EXECUTION_GEOMETRY")
    context = proposal["sizing_context"]
    _closed(context, {"stage", "producer", "source_hash", "observed_at_ms", "available_at_ms"},
            "sizing_context")
    if context["stage"] != "FINAL_LIVE_PRE_COMPUTE_QTY":
        raise ValueError("EVENT_EXECUTION_SIZING_STAGE")
    for key in ("producer", "source_hash"):
        _text(context[key], key)
    for key in ("observed_at_ms", "available_at_ms"):
        _integer(context[key], key)
    if not context["observed_at_ms"] <= context["available_at_ms"] <= proposal["decision_at_ms"]:
        raise ValueError("EVENT_EXECUTION_SIZING_CAUSALITY")
    return copy.deepcopy(proposal)


def validate_portfolio(portfolio):
    _closed(portfolio, PORTFOLIO_FIELDS, "portfolio")
    for key in ("complete", "paused", "kill_allowed", "live_guards_allowed"):
        if type(portfolio[key]) is not bool:
            raise ValueError(f"EVENT_EXECUTION_LITERAL_BOOL:{key}")
    for key in ("open_positions", "pending_count", "margin_as_of_ms"):
        _integer(portfolio[key], key)
    for key in ("equity_usd", "available_margin_usd", "open_risk_usd", "open_notional_usd",
                "pending_risk_usd", "pending_margin_usd"):
        _number(portfolio[key], key)
    _number(portfolio["daily_base_usd"], "daily_base_usd", -1e100)
    if portfolio["equity_usd"] <= 0 or portfolio["ownership"] not in ("BOT", "MANUAL", "UNKNOWN"):
        raise ValueError("EVENT_EXECUTION_PORTFOLIO_SCOPE")
    return copy.deepcopy(portfolio)


def source_functions(relative, names, namespace=None):
    """Compile only declared real source bodies, preserving file/line identities.

    No module top-level ENV/import/cache code is executed. This is source-only
    function loading, not a rewrite of production formulas or transition rules.
    """
    path = BACKEND / relative
    parsed = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise ValueError("EVENT_EXECUTION_SOURCE_FUNCTION_MISSING")
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes]
    tree = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    ns = {"__name__": relative[:-3].replace("/", "."), "math": math, "Optional": object,
          "Decimal": Decimal, "InvalidOperation": InvalidOperation, "ROUND_FLOOR": ROUND_FLOOR}
    ns.update(namespace or {})
    exec(compile(tree, str(path), "exec"), ns)
    return ns


def source_literal(relative, name):
    """Only literal constants from the same bound production source."""
    path = BACKEND / relative
    for node in ast.parse(path.read_text(), filename=str(path)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("EVENT_EXECUTION_SOURCE_LITERAL_MISSING")


def compute_admission(config, proposal, portfolio, *, final_qty=None, final_entry=None,
                      consumes_slot=True):
    """Use the actual LIVE caps and P03 pure gates with explicit causal inputs.

    risk_pct is the frozen final input from the upstream sizing stage. This
    function does not claim to reproduce upstream learned sizing/cache writers.
    """
    config, proposal, portfolio = (validate_execution_config(config), validate_proposal(proposal),
                                   validate_portfolio(portfolio))
    sizing = source_functions("services/shadow_trade_service.py", {"_compute_qty"}, config)
    result = sizing["_compute_qty"](proposal["entry"], proposal["stop"], proposal["risk_pct"],
                                    portfolio["equity_usd"], leverage=proposal["leverage"])
    qty = 0.0 if result is None else result["qty"]
    if config["LIVE_SIZE_MULT"] < 1:
        qty = round(qty * config["LIVE_SIZE_MULT"], 6)
    precision = source_functions("services/binance_signed_service.py", {"_floor_to_step"})
    qty = precision["_floor_to_step"](qty, config["filters"]["step"])
    reasons = []
    if result is None or result["status"] == "skip":
        reasons.append("SIZING_INVALID_OR_SKIP")
    if final_qty is not None:
        _number(final_qty, "final_qty", 1e-15)
        if consumes_slot and final_qty > qty + 1e-12:
            reasons.append("FINAL_QTY_EXCEEDS_PLANNED")
        qty = final_qty
    entry = proposal["entry"] if final_entry is None else final_entry
    _number(entry, "final_entry", 1e-15)
    notional = qty * entry
    risk = qty * abs(entry - proposal["stop"])
    margin = notional / proposal["leverage"]
    if qty < config["filters"]["min_qty"] or notional < max(config["MIN_NOTIONAL_USD"],
                                                            config["filters"]["min_notional"]):
        reasons.append("MIN_NOTIONAL_OR_QTY")
    if not portfolio["complete"]:
        reasons.append("PORTFOLIO_UNKNOWN")
    if portfolio["ownership"] != "BOT":
        reasons.append("MANUAL_POSITION_SYMBOL_BLOCKED")
    if portfolio["paused"] or not portfolio["kill_allowed"] or not portfolio["live_guards_allowed"]:
        reasons.append("RUNTIME_GUARD_BLOCKED")
    gates = source_functions("services/entry_intent_service.py",
                             {"_finite", "_budget_reason", "_margin_reason", "_capacity_reason"},
                             {"_MARGIN_CLOCK_SKEW_MS": source_literal("services/entry_intent_service.py",
                                                                     "_MARGIN_CLOCK_SKEW_MS")})
    cap = SimpleNamespace(risk_usd=risk, max_open_positions=config["MAX_OPEN_POSITIONS"]
                          if consumes_slot else None,
                          max_open_risk_usd=portfolio["equity_usd"] * config["MAX_OPEN_RISK_PCT"] / 100,
                          open_positions=portfolio["open_positions"], open_risk_usd=portfolio["open_risk_usd"])
    mg = SimpleNamespace(available_usd=portfolio["available_margin_usd"], required_usd=margin,
                         complete=portfolio["complete"], as_of_ms=portfolio["margin_as_of_ms"],
                         quality="live", max_age_s=config["MARGIN_MAX_AGE_S"])
    budget = SimpleNamespace(base_usd=portfolio["daily_base_usd"], limit_usd=config["DAILY_LOSS_LIMIT_USD"],
                             complete=portfolio["complete"])
    for reason in (gates["_capacity_reason"](cap, portfolio["pending_count"], portfolio["pending_risk_usd"]),
                   gates["_margin_reason"](mg, portfolio["pending_margin_usd"], now_ms=proposal["decision_at_ms"]),
                   gates["_budget_reason"](budget, portfolio["pending_risk_usd"], risk)):
        if reason:
            reasons.append(reason)
    if portfolio["open_notional_usd"] + notional > portfolio["equity_usd"] * config["MAX_TOTAL_NOTIONAL_PCT"] / 100:
        reasons.append("MAX_TOTAL_NOTIONAL")
    body = {"decision": "REJECTED" if reasons else "ADMITTED", "reason_codes": reasons,
            "final_qty": qty, "margin_usd": margin, "risk_usd": risk, "notional_usd": notional,
            "sizing": result, "consumes_slot": consumes_slot,
            "inputs_hash": digest({"config": config, "proposal": proposal, "portfolio": portfolio}),
            "caller_identities": ["shadow_trade_service._compute_qty", "binance_signed_service._floor_to_step",
                                  "entry_intent_service._capacity_reason", "entry_intent_service._margin_reason",
                                  "entry_intent_service._budget_reason"]}
    return {**body, "admission_hash": digest(body)}


def validate_execution_request(request, *, expected_source_fingerprint=None):
    _closed(request, REQUEST_FIELDS, "request")
    canonical(request)
    _credential_free(request)
    provenance = request["source_provenance"]
    _closed(provenance, {"producer", "source_id", "source_hash", "quality", "model_version", "synthetic"},
            "source_provenance")
    if (request["version"] != VERSION or request["source"] not in SOURCES
            or type(provenance["synthetic"]) is not bool
            or request["test_only"] is not (request["source"] == "TEST_ONLY_OBSERVED_EVENTS" or provenance["synthetic"])
            or request["real_study_allowed"] is not False):
        raise ValueError("EVENT_EXECUTION_SOURCE_OR_TEST_MARKER_INVALID")
    for key in ("producer", "source_id", "source_hash"):
        _text(provenance[key], key)
    if provenance["source_hash"] != digest({"receipts": request["receipts"], "fills": request["fills"]}):
        raise ValueError("EVENT_EXECUTION_EVENT_SOURCE_CONTENT_DRIFT")
    if provenance["quality"] != SOURCES[request["source"]]:
        raise ValueError("EVENT_EXECUTION_SOURCE_QUALITY_MISMATCH")
    if request["source"] == "DECLARED_EVENT_TABLE":
        if provenance["model_version"] != "CW_DETERMINISTIC_EVENT_TABLE_V1":
            raise ValueError("EVENT_EXECUTION_MODEL_TECHNICALLY_UNSUPPORTED")
    elif provenance["model_version"] is not None:
        raise ValueError("EVENT_EXECUTION_MIXED_SOURCE_FAMILIES")
    if expected_source_fingerprint is not None and request["source_fingerprint"] != expected_source_fingerprint:
        raise ValueError("EVENT_EXECUTION_SOURCE_DRIFT")
    if request["request_hash"] != digest({k: v for k, v in request.items() if k != "request_hash"}):
        raise ValueError("EVENT_EXECUTION_REQUEST_DRIFT")
    validate_execution_config(request["config"])
    proposal = validate_proposal(request["proposal"])
    validate_portfolio(request["portfolio"])
    if request["mode"] not in ("ENTRY", "PYRAMID"):
        raise ValueError("EVENT_EXECUTION_MODE_UNSUPPORTED")
    state = request["pyramid_state"]
    if request["mode"] == "ENTRY" and state is not None:
        raise ValueError("EVENT_EXECUTION_UNEXPECTED_PYRAMID_STATE")
    if request["mode"] == "PYRAMID":
        _closed(state, PYRAMID_FIELDS, "pyramid_state")
        if (state["id"] != proposal["trade_id"] or state["symbol"] != proposal["symbol"]
                or state["side"] != proposal["side"]):
            raise ValueError("EVENT_EXECUTION_PYRAMID_IDENTITY")
        for key in ("qty", "qty_initial", "entry_price", "planned_stop"):
            _number(state[key], key, 1e-15)
        for key in ("leverage", "pyramiding_level"):
            _integer(state[key], key, 1 if key == "leverage" else 0)
        for key in ("sl_close_position", "tp2_close_position", "protection_confirmed"):
            if type(state[key]) is not bool:
                raise ValueError("EVENT_EXECUTION_PYRAMID_PROTECTION")
        if state["phase"] not in ("pre_tp1", "post_tp1", "runner"):
            raise ValueError("EVENT_EXECUTION_PYRAMID_PHASE")
    seen = set()
    if not isinstance(request["receipts"], list) or len(request["receipts"]) > 1000:
        raise ValueError("EVENT_EXECUTION_RECEIPT_LIMIT")
    for row in request["receipts"]:
        _closed(row, RECEIPT_FIELDS, "receipt")
        if row["port"] not in PORTS:
            raise ValueError("EVENT_EXECUTION_PORT_UNSUPPORTED")
        for key in ("call_index", "after_action_index", "observed_at_ms", "available_at_ms"):
            _integer(row[key], key, 1 if key == "call_index" else 0)
        if (row["call_index"] in seen or row["after_action_index"] >= row["call_index"]
                or row["observed_at_ms"] > row["available_at_ms"]):
            raise ValueError("EVENT_EXECUTION_RECEIPT_CAUSALITY")
        seen.add(row["call_index"])
        _text(row["request_hash"], "request_hash")
    if not isinstance(request["fills"], list):
        raise ValueError("EVENT_EXECUTION_FILLS_REQUIRED")
    return copy.deepcopy(request)


def make_execution_request(*, config, proposal, portfolio, receipts, fills, mode="ENTRY", pyramid_state=None,
                           source="TEST_ONLY_OBSERVED_EVENTS", source_provenance=None):
    if source_provenance is None and source == "TEST_ONLY_OBSERVED_EVENTS":
        source_provenance = {"producer": "TEST_ONLY_FIXTURE", "source_id": "TEST_ONLY",
                             "source_hash": digest({"receipts": receipts, "fills": fills}),
                             "quality": "TEST_ONLY", "model_version": None, "synthetic": True}
    body = {"version": VERSION, "source": source,
            "test_only": source == "TEST_ONLY_OBSERVED_EVENTS" or bool((source_provenance or {}).get("synthetic")),
            "real_study_allowed": False, "source_fingerprint": execution_source_fingerprint(),
            "config": config, "proposal": proposal, "portfolio": portfolio, "receipts": receipts,
            "fills": fills, "mode": mode, "pyramid_state": pyramid_state, "source_provenance": source_provenance}
    return validate_execution_request({**copy.deepcopy(body), "request_hash": digest(body)})


def replay_execution(request):
    request = validate_execution_request(request, expected_source_fingerprint=execution_source_fingerprint())
    proc = subprocess.run([sys.executable, "-I", "-B", str(WORKER), str(BACKEND)],
                          input=canonical(request), capture_output=True, text=True, timeout=30,
                          env={"PYTHONHASHSEED": "0"}, cwd=str(BACKEND), check=False)
    if proc.returncode:
        raise ValueError("EVENT_EXECUTION_ISOLATED_CALLER_FAILED")
    report = json.loads(proc.stdout)
    if (report.get("version") != VERSION or report.get("request_hash") != request["request_hash"]
            or report.get("source_fingerprint") != request["source_fingerprint"]
            or report.get("runtime_identity") != runtime_identity()
            or report.get("test_only") is not request["test_only"] or report.get("real_study_allowed") is not False
            or report.get("authorizes_live") is not False
            or execution_source_fingerprint() != request["source_fingerprint"]):
        raise ValueError("EVENT_EXECUTION_REPORT_BOUNDARY_DRIFT")
    if report.get("report_hash") != digest({k: v for k, v in report.items() if k != "report_hash"}):
        raise ValueError("EVENT_EXECUTION_REPORT_DRIFT")
    return report


# Explicit name retained for callers that use the manager's run_trace convention.
run_execution = replay_execution
