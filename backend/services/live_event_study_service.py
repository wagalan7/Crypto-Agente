"""Versioned research event pipeline. No LIVE import, collector or authority.

The input is an official raw-population export plus explicitly attributed
event tables. It never manufactures an execution, rebinds baseline observations
to a counterfactual quantity, opens holdout details, or settles future money.
Unsupported paths are TECHNICAL_GAP, not a request for more historical data.
"""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from services.research_manifest_service import EVENT_MANIFEST_FIELDS, event_contract_hash

from services import live_management_replica_service as manager
from services import live_event_execution_service as execution
from services import live_event_accounting_service as ledger
from services import live_event_evidence_service as evidence

MANIFEST_VERSION = "R13_LIVE_EVENT_MANIFEST_V1"
STUDY_VERSION = "R13_LIVE_EVENT_STUDY_V1"
DATASET_VERSION = "CW_LIVE_EVENT_DATASET_V1"
PAYLOAD_KIND = "R13_REGISTERED_LIVE_EVENT_STUDY"
POPULATION_SOURCE = "R09_PRESELECTION_OFFICIAL_EXPORT_V2"
MANIFEST_FIELDS = set(EVENT_MANIFEST_FIELDS)
SHARED_FIELDS = {"management", "execution_config", "initial_portfolio"}
MANAGEMENT_FIELDS = {"engine_version", "source_fingerprint", "config", "config_hash"}
DATASET_FIELDS = {"version", "population_source", "manifest_hash", "index", "rows", "events", "dataset_hash"}
RECORD_FIELDS = {"opportunity_key", "steps", "context", "accounting", "order_links"}
STEP_FIELDS = {"kind", "at_ms", "request"}
LINK_FIELDS = {"order_id", "dispatch_id", "role", "step", "call_index", "algo_id", "correlation"}


class UncertainExecution(ValueError):
    def __init__(self, report):
        super().__init__("EVENT_EXECUTION_INCOMPLETE")
        self.report = report


def digest(value):
    return event_contract_hash(value)


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _closed(value, fields, name):
    _require(isinstance(value, dict) and set(value) == set(fields), f"EVENT_SCHEMA:{name}")
    return value


def _ms(value, name, minimum=1):
    _require(type(value) is int and value >= minimum, f"EVENT_CLOCK:{name}")
    return value


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def engine_fingerprint():
    dependencies = ("research_selection_service.py", "research_manifest_service.py", "score_v3_service.py",
        "score_v3_calibration_service.py", "walk_forward_service.py", "portfolio_replay_service.py",
        "preselection_experiment_service.py", "research_acceptance_service.py")
    return digest({"manager": manager.source_fingerprint(),
        "execution": execution.execution_source_fingerprint(), "ledger": ledger.model_fingerprint(),
        "study": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "evidence": hashlib.sha256(Path(evidence.__file__).read_bytes()).hexdigest(),
        "dependencies": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in dependencies},
        "runtime": manager.runtime_identity()})


def parse_manifest(raw):
    from services import research_manifest_service as rm
    declared_hash = raw.get("manifest_hash") if isinstance(raw, dict) else None
    if declared_hash is not None:
        raw = {key: value for key, value in raw.items() if key != "manifest_hash"}
    body = _closed(raw, MANIFEST_FIELDS, "manifest")
    _require(body["manifest_version"] == MANIFEST_VERSION, "EVENT_MANIFEST_VERSION")
    _require(body["comparison_scope"] == rm.SCOPE_SELECTION, "EVENT_SCOPE_NOT_IMPLEMENTED")
    shared = _closed(body["shared"], SHARED_FIELDS, "shared")
    management = _closed(shared["management"], MANAGEMENT_FIELDS, "management")
    _require(management["engine_version"] == manager.ENGINE_VERSION
        and management["source_fingerprint"] == manager.source_fingerprint(), "EVENT_MANAGER_DRIFT")
    cfg = _closed(management["config"], manager.CONFIG_FIELDS, "manager.config")
    for key in manager.BOOL_CONFIG:
        _require(type(cfg[key]) is bool, "EVENT_MANAGER_BOOL")
    for key in manager.INT_CONFIG:
        manager._integer(cfg[key], key, 1)
    for key in manager.NUMBER_CONFIG:
        manager._number(cfg[key], key)
    for key in ("PROTECTION_TP1_QTY_PCT", "RUNNER_QTY_PCT", "RUNNER_DETECT_FRAC"):
        _require(0 < cfg[key] < 1, "EVENT_MANAGER_FRACTION")
    _require(cfg["RUNNER_ATR_MULT"] > 0 and cfg["RUNNER_ATR_TF_FALLBACK"] in manager.TIMEFRAMES,
        "EVENT_MANAGER_RUNNER_CONFIG")
    _require(management["config_hash"] == digest(cfg), "EVENT_MANAGER_CONFIG_DRIFT")
    execution_cfg = execution.validate_execution_config(shared["execution_config"])
    initial = execution.validate_portfolio(shared["initial_portfolio"])
    _require(initial["complete"] is True, "EVENT_INITIAL_PORTFOLIO_UNKNOWN")
    sides = {}
    for name in ("baseline", "candidate"):
        side = _closed(body[name], rm.SIDE_FIELDS, name)
        parsed = {key: rm._text(side[key], f"{name}.{key}") for key in (
            "label", "core_version", "core_config_hash", "score_version", "score_config_hash", "policy_version")}
        parsed["playbooks"] = list(rm._tuple_of_text(side["playbooks"], name))
        parsed["selection_rule"] = rm._parse_rule(side["selection_rule"], name)
        parsed["score_config"] = rm._score_config(side["score_config"], parsed, name)
        _require(side["management_config"] == management, "EVENT_MANAGEMENT_MUST_BE_IDENTICAL")
        parsed["management_config"] = copy.deepcopy(management)
        sides[name] = parsed
    change = rm._parse_change(body["isolated_change"], escopo=rm.SCOPE_SELECTION)
    _require(change["component"] == "SCORE_MODEL", "EVENT_ISOLATED_COMPONENT_UNSUPPORTED")
    base, candidate = sides["baseline"], sides["candidate"]
    for key in ("core_version", "core_config_hash", "playbooks", "policy_version", "management_config"):
        _require(base[key] == candidate[key], f"EVENT_NON_ISOLATED_CHANGE:{key}")
    _require(base["selection_rule"]["kind"] == rm.RULE_OBSERVED_CHAMPION
        and candidate["selection_rule"]["kind"] == rm.RULE_SCORE_V3_MIN, "EVENT_SELECTION_RULE")
    _require(rm._side_component(base, "SCORE_MODEL") != rm._side_component(candidate, "SCORE_MODEL"),
        "EVENT_SIDES_IDENTICAL")
    split = _closed(body["split"], evidence.TEMPORAL_FIELDS, "split")
    for key in split.keys() - {"censoring"}:
        _ms(split[key], key, 0 if key in {"purge_ms", "embargo_ms"} else 1)
    _require(split["train_start_ms"] < split["validation_start_ms"] < split["holdout_start_ms"]
        and split["validation_start_ms"] < split["as_of_ms"], "EVENT_SPLIT_ORDER")
    _require(split["purge_ms"] >= split["horizon_ms"]
        and all(split[key] % split["bar_ms"] == 0 for key in ("horizon_ms", "purge_ms", "embargo_ms")),
        "EVENT_SPLIT_PURGE_OR_UNITS")
    from services import score_v3_calibration_service as calibration
    _require(split["censoring"] in calibration.CENSORING_RULES, "EVENT_CENSORING")
    costs = _closed(body["costs"], {"source", "unit", "asset", "model_fingerprint"}, "costs")
    _require(costs["source"] in ledger.PROVENANCES and costs["unit"] == "SETTLEMENT_ASSET_ABSOLUTE"
        and costs["asset"] == "USDT", "EVENT_COSTS_SOURCE_OR_UNIT")
    _require(costs["model_fingerprint"] == ledger.model_fingerprint(), "EVENT_COSTS_ENGINE_DRIFT")
    decision = rm._parse_decision(body["decision"])
    _require(decision["state"] == "TEST_ONLY" or costs["source"] != "TEST_ONLY",
        "EVENT_TEST_SOURCE_CANNOT_BECOME_REAL")
    population = rm._parse_population(body["population"])
    _require(population["quote"] == "USDT", "EVENT_SETTLEMENT_UNSUPPORTED")
    normalized = {"manifest_version": MANIFEST_VERSION, "study_id": rm._text(body["study_id"], "study_id"),
        "decision": decision, "comparison_scope": rm.SCOPE_SELECTION, "population": population,
        "isolated_change": change, **sides, "costs": copy.deepcopy(costs), "split": copy.deepcopy(split),
        "shared": {"management": copy.deepcopy(management), "execution_config": execution_cfg,
                   "initial_portfolio": initial}}
    hashes = {f"{key}_hash": digest(normalized[key]) for key in (
        "baseline", "candidate", "population", "costs", "split", "shared")}
    hashes["bundle_hash"] = digest({**hashes, "scope": rm.SCOPE_SELECTION, "isolated_change": change})
    _require(body["hashes"] is None or body["hashes"] == hashes, "EVENT_MANIFEST_DRIFT")
    normalized["hashes"] = hashes
    normalized["manifest_hash"] = digest(normalized)
    _require(declared_hash is None or declared_hash == normalized["manifest_hash"], "EVENT_MANIFEST_DRIFT")
    return normalized


def verify_manifest(raw, *, expected_hash=None):
    try:
        _require(isinstance(raw, dict) and set(raw) <= MANIFEST_FIELDS | {"manifest_hash"}, "EVENT_MANIFEST_SCHEMA")
        parsed = parse_manifest({key: raw[key] for key in MANIFEST_FIELDS})
        _require(raw.get("manifest_hash", parsed["manifest_hash"]) == parsed["manifest_hash"]
            and (expected_hash is None or expected_hash == parsed["manifest_hash"]), "EVENT_MANIFEST_DRIFT")
        from services import research_manifest_service as rm
        return {"ok": True, "reason_code": "OK", "state": rm.manifest_state(parsed),
                "manifest": parsed, "manifest_hash": parsed["manifest_hash"]}
    except (ValueError, TypeError, KeyError) as error:
        return {"ok": False, "state": "BLOCKED", "reason_code": str(error)}


def authorized_comparison(raw):
    verdict = verify_manifest(raw)
    parsed = verdict.get("manifest", {})
    approved = parsed.get("decision", {}).get("state") == "APPROVED"
    return {"available": verdict["ok"] and parsed.get("decision", {}).get("state") in {"TEST_ONLY", "APPROVED"},
        "reason_code": verdict["reason_code"], "state": verdict["state"], "real_study_allowed": approved,
        "manifest_hash": parsed.get("manifest_hash"), "study_id": parsed.get("study_id"),
        "comparison_scope": parsed.get("comparison_scope"), "decision": parsed.get("decision"),
        "bundle_hash": parsed.get("hashes", {}).get("bundle_hash"), "isolated_change": parsed.get("isolated_change")}


def _available(record, as_of):
    """Check clocks before accessing event raw/value bodies or hashing them."""
    for step in record["steps"]:
        _closed(step, STEP_FIELDS, "step")
        _require(_ms(step["at_ms"], "step") <= as_of, "EVENT_FUTURE_STEP_FORBIDDEN")
        request = step["request"]
        if step["kind"] == "EXECUTION":
            for receipt in request["receipts"]:
                _require(_ms(receipt["available_at_ms"], "receipt") <= as_of, "EVENT_FUTURE_DETAIL_FORBIDDEN")
            for fill in request["fills"]:
                _require(_ms(fill["available_at_ms"], "fill") <= as_of, "EVENT_FUTURE_DETAIL_FORBIDDEN")
        elif step["kind"] == "MANAGEMENT":
            for tick in request["ticks"]:
                _require(_ms(tick["at_ms"], "tick") <= as_of, "EVENT_FUTURE_DETAIL_FORBIDDEN")
                for receipts in tick["ports"].values():
                    for receipt in receipts:
                        _require(_ms(receipt["available_at_ms"], "receipt") <= as_of, "EVENT_FUTURE_DETAIL_FORBIDDEN")
        else:
            raise ValueError("EVENT_STEP_KIND_UNSUPPORTED")
    for group in ("orders", "fills", "funding", "fee_conversions", "slippage"):
        for event in record["accounting"][group]:
            _require(_ms(event["available_at_ms"], group) <= as_of, "EVENT_FUTURE_DETAIL_FORBIDDEN")
    _require(_ms(record["accounting"]["coverage"]["available_at_ms"], "coverage") <= as_of,
        "EVENT_FUTURE_DETAIL_FORBIDDEN")


def validate_dataset(dataset, manifest):
    _closed(dataset, DATASET_FIELDS, "dataset")
    _require(dataset["version"] == DATASET_VERSION and dataset["population_source"] == POPULATION_SOURCE
        and dataset["manifest_hash"] == manifest["manifest_hash"], "EVENT_EXPORT_IDENTITY")
    split = manifest["split"]
    stop = min(split["holdout_start_ms"], split["as_of_ms"])
    index, causal_keys = {}, set()
    for row in dataset["index"]:
        _closed(row, {"opportunity_key", "decision_ts_ms", "causal_index"}, "cold.index")
        key = row["opportunity_key"]
        _require(isinstance(key, str) and key and key not in index, "EVENT_DUPLICATE_INDEX")
        _ms(row["decision_ts_ms"], "index")
        _ms(row["causal_index"], "index.causal", 0)
        causal = (row["decision_ts_ms"], row["causal_index"])
        _require(causal not in causal_keys, "EVENT_CAUSAL_INDEX_AMBIGUOUS")
        causal_keys.add(causal)
        index[key] = row
    permitted = {key for key, row in index.items() if split["train_start_ms"] <= row["decision_ts_ms"] < stop}
    rows = {}
    for row in dataset["rows"]:
        key = row["opportunity_key"]
        _require(key in permitted and key not in rows, "EVENT_HOLDOUT_OR_DUPLICATE_DETAIL")
        _require(row["decision_ts_ms"] == index[key]["decision_ts_ms"], "EVENT_DETAIL_CLOCK_DRIFT")
        _require(row.get("available_at_ms") is not None and row["available_at_ms"] <= row["decision_ts_ms"],
            "EVENT_FEATURES_NOT_POINT_IN_TIME")
        rows[key] = row
    _require(set(rows) == permitted, "EVENT_POPULATION_DETAIL_INCOMPLETE")
    _closed(dataset["events"], {"baseline", "candidate"}, "events")
    for records in dataset["events"].values():
        seen = set()
        for record in records:
            key = record["opportunity_key"]
            _require(key in permitted and key not in seen, "EVENT_HOLDOUT_OR_DUPLICATE_EVENT")
            seen.add(key)
            _closed(record, RECORD_FIELDS, "record")
            # A pre-holdout opportunity doesn't grant permission to read
            # executions/prices from the sealed period for its later exit.
            _available(record, stop - 1)
    calculated = digest({key: value for key, value in dataset.items() if key != "dataset_hash"})
    _require(dataset["dataset_hash"] in (None, calculated), "EVENT_DATASET_DRIFT")
    return {**copy.deepcopy(dataset), "dataset_hash": calculated}


def _wire(symbol):
    return symbol.split(":")[0].replace("/", "")


def _linked_exits(record, reports):
    """Admit exits only from exact manager/protection call evidence.

    orderId ↔ algoId correlation is an explicit broker/model observation. It is
    not a proof of exchange authenticity, and is never inferred from price.
    """
    linked = []
    for link in record["order_links"]:
        _closed(link, LINK_FIELDS, "order_link")
        _require(link["role"] in ledger.ROLES - ledger.ENTRY_ROLES, "EVENT_EXIT_ROLE")
        idx = _ms(link["step"], "link.step", 0)
        _require(idx < len(reports), "EVENT_EXIT_STEP")
        actions = reports[idx].get("actions", [])
        matching = [a for a in actions if a.get("call_index") == link["call_index"]]
        _require(len(matching) == 1, "EVENT_EXIT_CALL_MISSING")
        action = matching[0]
        action_at = _ms(action["at_ms"], "exit.action")
        response = action.get("response", action.get("value", {}))
        name = action.get("port", action.get("name"))
        if link["algo_id"] is not None:
            expected = {"STOP": "sl_order_id", "TP1": "tp1_order_id", "TP2": "tp2_order_id", "RUNNER": "sl_order_id"}[link["role"]]
            _require(name in {"place_protection", "place_protection_orders"}
                and str(response.get(expected)) == link["algo_id"]
                and response.get(expected.replace("_order_id", "_ok")) is True, "EVENT_CONDITIONAL_LINK_UNPROVEN")
            corr = _closed(link["correlation"], {"contract", "source", "account_scope", "symbol", "trade_id",
                "opportunity_id", "order_id", "algo_id", "dispatch_id", "role", "action_request_hash",
                "observed_at_ms", "available_at_ms"}, "conditional.correlation")
            _require(corr["contract"] == "CW_CONDITIONAL_EXECUTION_CORRELATION_V1"
                and corr["source"] == record["accounting"]["provenance"]
                and all(corr[key] == link[key] for key in ("order_id", "algo_id", "dispatch_id", "role"))
                and all(corr[key] == record["context"][key] for key in ("account_scope", "symbol", "trade_id", "opportunity_id"))
                and corr["action_request_hash"] == action.get("request_hash", action.get("expected_request_hash"))
                and _ms(corr["observed_at_ms"], "correlation") <= _ms(corr["available_at_ms"], "correlation"),
                "EVENT_CONDITIONAL_CORRELATION_MISMATCH")
            _require(corr["observed_at_ms"] >= action_at, "EVENT_CORRELATION_PRECEDES_ACTION")
            fills = [f for f in record["accounting"]["fills"] if f["binding"]["order_id"] == link["order_id"]]
            _require(bool(fills) and all(corr["available_at_ms"] <= f["available_at_ms"] for f in fills),
                "EVENT_CONDITIONAL_CORRELATION_NOT_AVAILABLE")
        else:
            _require(link["correlation"] is None, "EVENT_NONCONDITIONAL_CORRELATION")
            result = response.get("result", response)
            _require(name in {"place_order", "emergency_close"} and response.get("ok") is True
                and str(result.get("orderId", result.get("order_id"))) == link["order_id"], "EVENT_EXIT_ORDER_UNPROVEN")
            _require(action.get("kwargs", {}).get("client_order_id") == link["dispatch_id"],
                "EVENT_EXIT_DISPATCH_UNPROVEN")
            args, kwargs = action.get("args", []), action.get("kwargs", {})
            symbol = kwargs.get("symbol", args[0] if args else None)
            side = kwargs.get("side", args[1] if len(args) > 1 else None)
            qty = ledger.accounting.to_decimal(kwargs.get("qty", args[2] if len(args) > 2 else None))
            _require(isinstance(symbol, str) and _wire(symbol) == record["context"]["symbol"]
                and str(side).upper() == ("SELL" if record["context"]["side"] == "LONG" else "BUY")
                and kwargs.get("reduce_only") is True and str(kwargs.get("order_type")).upper() == "MARKET"
                and qty is not None and qty > 0, "EVENT_EXIT_REQUEST_GEOMETRY_UNPROVEN")
            distinct = {}
            for event in record["accounting"]["fills"]:
                if event["binding"]["order_id"] != link["order_id"]:
                    continue
                fill, reason = ledger.accounting.normalize_fill(event["raw"], exchange="binance")
                _require(fill is not None and reason is None, "EVENT_EXIT_FILL_INVALID")
                old = distinct.get(fill["exec_id"])
                _require(old is None or old == fill, "EVENT_EXIT_FILL_CONFLICT")
                distinct[fill["exec_id"]] = fill
            total = sum((Decimal(f["qty"]) for f in distinct.values()), Decimal("0"))
            orders = [e["raw"] for e in record["accounting"]["orders"] if e["binding"]["order_id"] == link["order_id"]]
            _require(0 < total <= qty and bool(orders) and all(
                ledger.accounting.to_decimal(o["executedQty"]) is not None
                and ledger.accounting.to_decimal(o["executedQty"]) <= qty
                and o["side"] == str(side).upper() and _wire(o["symbol"]) == _wire(symbol)
                and (o["reduceOnly"] is True or isinstance(o["reduceOnly"], str) and o["reduceOnly"].lower() == "true")
                and o["type"] == "MARKET" for o in orders), "EVENT_EXIT_QUANTITY_EXCEEDS_REQUEST")
        for group, clock in (("fills", "time"), ("orders", "updateTime")):
            matches = [e for e in record["accounting"][group] if e["binding"]["order_id"] == link["order_id"]]
            _require(bool(matches) and all(_ms(e["raw"][clock], "exit.execution") >= action_at for e in matches),
                "EVENT_EXIT_EXECUTED_BEFORE_ACTION")
        linked.append({key: link[key] for key in ("order_id", "dispatch_id", "role")})
    return linked


def _run_record(record, manifest, row, portfolio, *, execution_sink=None):
    steps = record["steps"]
    _require(bool(steps) and steps[0]["kind"] == "EXECUTION" and steps[0]["request"]["mode"] == "ENTRY",
        "EVENT_ENTRY_FIRST_REQUIRED")
    proposal = steps[0]["request"]["proposal"]
    _require(proposal["opportunity_id"] == row["opportunity_key"]
        and proposal["decision_at_ms"] == row["decision_ts_ms"] and proposal["symbol"] == row["symbol"]
        and proposal["side"] == row["side"], "EVENT_ENTRY_POPULATION_MISMATCH")
    for pkey, rkey in (("entry", "entry"), ("stop", "stop_loss"), ("tp1", "tp1"), ("tp2", "tp2")):
        _require(proposal[pkey] == row[rkey], "EVENT_ENTRY_GEOMETRY_MISMATCH")
    _require(steps[0]["request"]["portfolio"] == portfolio, "EVENT_COUNTERFACTUAL_PORTFOLIO_MISMATCH")
    reports, admitted, management_report = [], [], None
    previous_at = row["decision_ts_ms"]
    for step in steps:
        _require(step["at_ms"] >= previous_at, "EVENT_STEP_CHRONOLOGY")
        previous_at = step["at_ms"]
        request = step["request"]
        if step["kind"] == "EXECUTION":
            _require(request["config"] == manifest["shared"]["execution_config"], "EVENT_EXECUTION_CONFIG_DRIFT")
            expected_source = {"TEST_ONLY": "TEST_ONLY_OBSERVED_EVENTS", "OBSERVED_ACCOUNT": "OBSERVED_ACCOUNT_EVENTS",
                "DECLARED_MODEL": "DECLARED_EVENT_TABLE"}[manifest["costs"]["source"]]
            _require(request["source"] == expected_source
                and request["test_only"] is (manifest["decision"]["state"] == "TEST_ONLY"), "EVENT_EXECUTION_SOURCE_MISMATCH")
            if request["mode"] == "PYRAMID":
                # The current real caller cannot supply its mandatory preflight.
                # Never bypass this limitation to make a positive fixture pass.
                raise ValueError("TECHNICAL_LIVE_PYRAMID_MARKET_PREFLIGHT_MISSING")
            report = execution.replay_execution(request)
            if execution_sink is not None:
                execution_sink.append(report)
            admitted.extend(report.get("admitted_orders", []))
            if report.get("status") == "REJECTED" or (report.get("status") == "COMPLETE_EXECUTION"
                    and report["filled_qty"] == 0 and report["reservations"]["state"] == "RELEASED"):
                return {"execution": report, "management": None, "ledger": None, "step_reports": [report],
                        "complete": True, "not_executed": True}
            if report.get("status") != "COMPLETE_EXECUTION":
                raise UncertainExecution(report)
        else:
            _require(manifest["decision"]["state"] == "TEST_ONLY" or request["source"] == "OPERATOR_EVENT_TRACE",
                "EVENT_SYNTHETIC_MANAGER_CANNOT_BECOME_OBSERVED")
            _require(request["binding_mode"] == "BOUND", "EVENT_MANAGER_PREVIEW_FORBIDDEN")
            _require(request["config"] == manifest["shared"]["management"]["config"], "EVENT_MANAGER_CONFIG_DRIFT")
            state = request["initial_state"]
            _require(state["id"] == proposal["trade_id"] and _wire(state["symbol"]) == _wire(proposal["symbol"])
                and state["side"] == proposal["side"] and state["planned_stop"] == proposal["stop"],
                "EVENT_MANAGER_ENTRY_IDENTITY")
            _require(state["source"] == "auto" and state["exchange"] == "binance" and state["status"] == "open"
                and state["phase"] == "pre_tp1" and state["planned_tp1"] == proposal["tp1"]
                and state["planned_tp2"] == proposal["tp2"], "EVENT_MANAGER_INITIAL_STATE_UNPROVEN")
            entry_fills = steps[0]["request"]["fills"]
            _require(bool(entry_fills), "EVENT_ENTRY_FILLS_MISSING")
            qty = sum(float(f["raw"]["qty"]) for f in entry_fills)
            avg = sum(float(f["raw"]["qty"]) * float(f["raw"]["price"]) for f in entry_fills) / qty
            _require(abs(state["qty_initial"] - qty) < 1e-10 and abs(state["qty"] - qty) < 1e-10
                and abs(state["entry_price"] - avg) < 1e-10 and state["sl_current_price"] == proposal["stop"],
                "EVENT_MANAGER_PHANTOM_FILL")
            known_at = max(f["available_at_ms"] for f in entry_fills)
            _require(_ms(state["opened_at_ms"], "manager.opened") >= known_at
                and (state["created_at_ms"] is None or _ms(state["created_at_ms"], "manager.created") >= known_at),
                "EVENT_MANAGER_BACKDATED_ENTRY")
            initial_protection = next((a["value"] for a in reports[0]["actions"] if a["port"] == "place_protection"), None)
            _require(initial_protection is not None and all(state[k] == initial_protection.get(k)
                for k in ("sl_order_id", "tp1_order_id", "tp2_order_id")), "EVENT_MANAGER_INITIAL_PROTECTION_UNPROVEN")
            _require(management_report is None, "TECHNICAL_MULTI_SEGMENT_MANAGER_NOT_IMPLEMENTED")
            report = manager.run_trace(request)
            management_report = report
            _require(report.get("status") == "COMPLETE_TRACE", "EVENT_MANAGEMENT_INCOMPLETE")
        reports.append(report)
    _require(management_report is not None, "EVENT_MANAGEMENT_REQUIRED")
    ctx = ledger.validate_context(record["context"])
    _require(ctx["opportunity_id"] == row["opportunity_key"] and ctx["trade_id"] == str(proposal["trade_id"])
        and ctx["dispatch_id"] == proposal["dispatch_id"] and ctx["symbol"] == _wire(proposal["symbol"])
        and ctx["side"] == proposal["side"].upper() and float(ctx["planned_stop"]) == proposal["stop"],
        "EVENT_LEDGER_CONTEXT_MISMATCH")
    _require(record["accounting"]["provenance"] == manifest["costs"]["source"], "EVENT_COSTS_PROVENANCE_MISMATCH")
    _require(record["accounting"]["test_only"] is (manifest["decision"]["state"] == "TEST_ONLY"),
        "EVENT_TEST_ONLY_PROVENANCE_MISMATCH")
    def fill_material(f):
        raw = f["raw"]
        numbers = {}
        for key in ("price", "qty", "realizedPnl", "commission"):
            number = ledger.accounting.to_decimal(raw[key], allow_negative=key == "realizedPnl")
            _require(number is not None, "EVENT_EXECUTION_LEDGER_NUMBER_INVALID")
            numbers[key] = str(number.normalize())
        return {**{key: str(raw[key]) for key in ("id", "orderId", "side", "positionSide", "commissionAsset", "time")},
            "symbol": _wire(raw["symbol"]), **numbers,
            "dispatch_id": f["binding"]["dispatch_id"], "role": f["role"]}
    real_fills = {str(f["raw"]["id"]): fill_material(f) for f in reports[0]["fills"]}
    financial_fills = {str(f["raw"]["id"]): fill_material(f) for f in record["accounting"]["fills"] if f["role"] in ledger.ENTRY_ROLES}
    _require(real_fills == financial_fills, "EVENT_EXECUTION_LEDGER_FILL_MISMATCH")
    for fill in reports[0]["fills"]:
        emissions = [a for a in reports[0]["actions"] if a["port"] == "send_order"
            and a["args"][2].get("newClientOrderId") == fill["binding"]["dispatch_id"]]
        _require(bool(emissions) and _ms(fill["raw"]["time"], "entry.fill") >= min(a["at_ms"] for a in emissions),
            "EVENT_ENTRY_EXECUTED_BEFORE_ACTION")
    admitted.extend(_linked_exits(record, reports))
    money = ledger.replay_accounting(record["accounting"], ctx, management_report,
        execution_report={"admitted_orders": admitted}, as_of_ms=manifest["split"]["as_of_ms"])
    return {"execution": reports[0], "management": management_report, "ledger": money,
            "step_reports": reports, "complete": money["economic_value_known"]}


def run_study(*, manifest, dataset, now_ms):
    """Run both selections and causal portfolios; no database or network."""
    from services import research_selection_service as selection
    result = {"ok": False, "version": STUDY_VERSION, "kind": PAYLOAD_KIND,
        "authorizes_live": False, "promotable": False, "live_equivalent": False,
        "live_changed": False, "real_study_allowed": False, "holdout_status": "SEALED"}
    try:
        checked = verify_manifest(manifest)
        _require(checked["ok"], checked["reason_code"])
        manifest = checked["manifest"]
        _require(authorized_comparison(manifest)["available"], "EVENT_MANIFEST_NOT_AUTHORIZED")
        _require(_ms(now_ms, "now") >= manifest["split"]["as_of_ms"], "EVENT_AS_OF_FUTURE")
        data = validate_dataset(dataset, manifest)
        comparison = selection.compare_population(data["rows"], manifest=manifest)
        _require(comparison["ok"], comparison.get("reason_code", "EVENT_SELECTION_UNKNOWN"))
        rows = {r["opportunity_key"]: r for r in data["rows"]}
        idx = {r["opportunity_key"]: r for r in data["index"]}
        decisions = sorted(comparison["decisions"], key=lambda r: (r["decision_ts_ms"], idx[r["opportunity_key"]]["causal_index"]))
        population, labels, arms, ledgers = [], [], {}, []
        for d in decisions:
            score = d["candidate"].get("score")
            population.append({"opportunity_key": d["opportunity_key"], "decision_ts_ms": d["decision_ts_ms"],
                               "score": score, "included": d["included"]})
        for arm in ("baseline", "candidate"):
            state = copy.deepcopy(manifest["shared"]["initial_portfolio"])
            initial = copy.deepcopy(state)
            records = {r["opportunity_key"]: r for r in data["events"][arm]}
            pending, outputs, economics = [], [], []
            day = None
            day_net = initial["daily_base_usd"]
            for decision in decisions:
                key, at = decision["opportunity_key"], decision["decision_ts_ms"]
                this_day = at // 86_400_000
                if day is not None and day != this_day:
                    day_net = 0.0
                day = this_day
                for open_item in list(pending):
                    money, admission = open_item["ledger"], open_item["execution"]["admission"]
                    # There is no global receipt sequence across independent
                    # trade traces. A same-ms tie cannot prove prior knowledge.
                    if money["financial_release_allowed"] and money["result_available_ts_ms"] < at:
                        net = float(money["net_including_funding"])
                        state["equity_usd"] += net
                        state["available_margin_usd"] += admission["margin_usd"] + net
                        state["open_positions"] -= 1
                        state["open_risk_usd"] -= admission["risk_usd"]
                        state["open_notional_usd"] -= admission["notional_usd"]
                        closed_day = max(e["fill_time_ms"] for e in money["role_events"]) // 86_400_000
                        if closed_day == this_day:
                            day_net += net
                        pending.remove(open_item)
                state["margin_as_of_ms"] = at
                state["daily_base_usd"] = day_net - state["open_risk_usd"]
                selected = decision["included"] and decision[arm]["state"] == "SELECTED"
                if not selected:
                    continue
                raw = records.get(key)
                if raw is None:
                    outputs.append({"opportunity_key": key, "state": "NON_COMPARABLE", "reason_code": "EVENT_COUNTERFACTUAL_OBSERVATIONS_MISSING"})
                    economics.append({"opportunity_id": key, "decision_ts_ms": at, "admitted": True,
                                      "result_available_ts_ms": None, "net_r": None})
                    state["complete"] = False
                    continue
                attempts = []
                try:
                    outcome = _run_record(raw, manifest, rows[key], state, execution_sink=attempts)
                    outputs.append({"opportunity_key": key, "state": "COMPLETE" if outcome["complete"] else "UNKNOWN", **outcome})
                    if outcome.get("not_executed"):
                        outputs[-1]["state"] = "NOT_EXECUTED"
                        economics.append({"opportunity_id": key, "decision_ts_ms": at, "admitted": False,
                                          "result_available_ts_ms": None, "net_r": None})
                        continue
                    admission = outcome["execution"]["admission"]
                    state["available_margin_usd"] -= admission["margin_usd"]
                    state["open_positions"] += 1
                    state["open_risk_usd"] += admission["risk_usd"]
                    state["open_notional_usd"] += admission["notional_usd"]
                    pending.append(outcome)
                    money = outcome["ledger"]
                    ledgers.append(money["lineage_hash"])
                    economics.append({"opportunity_id": key, "decision_ts_ms": at, "admitted": True,
                        "result_available_ts_ms": money["result_available_ts_ms"],
                        "net_r": float(money["realized_r_including_funding"]) if outcome["complete"] else None})
                    if not outcome["complete"]:
                        state["complete"] = False
                    if arm == "candidate":
                        terminal = next((e for e in money["role_events"] if e["role"] in {"TP1", "STOP"}), None)
                        label = terminal is not None and terminal["fill_time_ms"] <= at + manifest["split"]["horizon_ms"]
                        labels.append({"opportunity_key": key, "score": decision[arm].get("score"),
                            "label": terminal["role"] == "TP1" if label else None, "event": "P_TP1_BEFORE_STOP",
                            "decision_ts_ms": at, "label_available_ts_ms": terminal["available_at_ms"] if label else None,
                            "horizon_ms": manifest["split"]["horizon_ms"],
                            "source": {"TEST_ONLY": "TEST_ONLY_SYNTHETIC_EVENT", "OBSERVED_ACCOUNT": "ATTRIBUTED_EXECUTION_EVENT",
                                "DECLARED_MODEL": "DECLARED_DETERMINISTIC_MODEL"}[manifest["costs"]["source"]], "proven": label})
                except ValueError as error:
                    refused = {"opportunity_key": key, "state": "NON_COMPARABLE", "reason_code": str(error)}
                    if isinstance(error, UncertainExecution):
                        refused["execution"] = error.report
                        reserved = error.report["reservations"]
                        state["pending_count"] += int(reserved["slot_retained"])
                        state["pending_risk_usd"] += reserved["risk_usd"]
                        state["pending_margin_usd"] += reserved["margin_usd"]
                    elif attempts and attempts[0]["status"] == "COMPLETE_EXECUTION" and attempts[0]["filled_qty"] > 0:
                        # A downstream trace/ledger failure does not erase an
                        # already proven entry or its economic exposure.
                        refused["execution"] = attempts[0]
                        admission = attempts[0]["admission"]
                        state["available_margin_usd"] -= admission["margin_usd"]
                        state["open_positions"] += 1
                        state["open_risk_usd"] += admission["risk_usd"]
                        state["open_notional_usd"] += admission["notional_usd"]
                    outputs.append(refused)
                    economics.append({"opportunity_id": key, "decision_ts_ms": at, "admitted": True,
                                      "result_available_ts_ms": None, "net_r": None})
                    state["complete"] = False
            arms[arm] = {"records": outputs, "economics": economics, "portfolio_at_last_decision": state,
                         "unsettled": len(pending), "future_settlement_not_applied": True}
        identity = {"manifest_hash": manifest["manifest_hash"], "dataset_hash": data["dataset_hash"],
            "events_hash": digest(data["events"]), "ledger_hash": digest(ledgers),
            "config_hash": digest(manifest["shared"]), "model_fingerprint": manifest["candidate"]["score_config_hash"],
            "costs_hash": manifest["hashes"]["costs_hash"], "bundle_hash": manifest["hashes"]["bundle_hash"],
            "engine_fingerprint": engine_fingerprint(), "runtime_fingerprint": digest(manager.runtime_identity())}
        evidence_inputs = {"baseline_rows": arms["baseline"]["economics"], "candidate_rows": arms["candidate"]["economics"],
            "population_rows": population, "temporal": manifest["split"], "event_labels": labels,
            "identity": identity, "now_ms": now_ms, "test_only": manifest["decision"]["state"] == "TEST_ONLY"}
        statistical = evidence.evaluate_evidence(**evidence_inputs)
        _require(statistical.get("ok") is True, statistical.get("reason_code", "EVENT_EVIDENCE_INVALID"))
        gaps = sorted({r["reason_code"] for a in arms.values() for r in a["records"] if r["state"] == "NON_COMPARABLE"})
        if manifest["shared"]["execution_config"]["PYRAMIDING_ENABLED"]:
            gaps.append("TECHNICAL_LIVE_PYRAMID_MARKET_PREFLIGHT_MISSING")
        result.update(ok=True, manifest=manifest, identity=identity, dataset=data, selection=comparison,
            arms=arms, evidence=statistical, evidence_inputs=evidence_inputs, technical_gaps=sorted(set(gaps)),
            state="TECHNICAL_GAP" if gaps else "LOCAL_EVENT_EVIDENCE", source_assurance="DECLARED_SOURCE_NOT_AUTHENTICATION")
        # The controlled store is JSONB. Canonical transport before sealing
        # prevents native algorithm tuples becoming unequal after a restart.
        result = json.loads(json.dumps(result, allow_nan=False))
        result["report_hash"] = digest(result)
        return result
    except (ValueError, TypeError, KeyError) as error:
        return {**result, "state": "BLOCKED", "reason_code": str(error)}


def verify_report(report, *, recompute=False):
    try:
        _require(isinstance(report, dict) and report.get("version") == STUDY_VERSION
            and report.get("kind") == PAYLOAD_KIND and report.get("ok") is True, "EVENT_REPORT_FAMILY")
        _require(all(report.get(k) is False for k in ("authorizes_live", "promotable", "live_equivalent", "live_changed", "real_study_allowed"))
            and report.get("holdout_status") == "SEALED", "EVENT_REPORT_AUTHORITY")
        _require(report["report_hash"] == digest({k: v for k, v in report.items() if k != "report_hash"}), "EVENT_REPORT_DRIFT")
        checked = verify_manifest(report["manifest"])
        _require(checked["ok"], "EVENT_REPORT_MANIFEST")
        data = validate_dataset(report["dataset"], checked["manifest"])
        _require(report["identity"]["dataset_hash"] == data["dataset_hash"]
            and report["identity"]["manifest_hash"] == checked["manifest_hash"]
            and report["identity"]["engine_fingerprint"] == engine_fingerprint(), "EVENT_REPORT_BINDING")
        _require(evidence.verify_evidence(report["evidence"], inputs=report["evidence_inputs"] if recompute else None)["ok"],
            "EVENT_REPORT_EVIDENCE")
        _require(report["evidence"]["input_hashes"] == {k: evidence.digest(v) for k, v in report["evidence_inputs"].items()},
            "EVENT_EVIDENCE_INPUT_DRIFT")
        _require(report["evidence_inputs"]["identity"] == report["identity"]
            and report["evidence_inputs"]["baseline_rows"] == report["arms"]["baseline"]["economics"]
            and report["evidence_inputs"]["candidate_rows"] == report["arms"]["candidate"]["economics"],
            "EVENT_REPORTED_ECONOMICS_DRIFT")
        if recompute:
            actual = run_study(manifest=report["manifest"], dataset=data, now_ms=report["evidence_inputs"]["now_ms"])
            _require(actual == report, "EVENT_REPORT_RECOMPUTATION_MISMATCH")
        return {"ok": True, "reason_code": "EVENT_REPORT_VERIFIED", "recomputed": recompute}
    except (ValueError, TypeError, KeyError) as error:
        return {"ok": False, "reason_code": str(error)}


def study_reference(report):
    return {"experiment_key": "r13evt:" + digest(report["identity"])[:32],
        "universe_version": report["manifest"]["population"]["universe_version"], "population": "SHADOW"}


async def persist_study(session_factory, report):
    """Full recomputation OUTSIDE the transaction; official state CAS inside.

    Stores insufficient/rejected/local-test research as such. It grants no
    approval. A conflicting result at the same frozen identity is preserved,
    not overwritten by an UPDATE or last writer wins.
    """
    from services import policy_state_service as state
    checked = verify_report(report, recompute=True)
    if not checked["ok"]:
        return {"published": False, "reason_code": checked["reason_code"]}
    ref = study_reference(report)
    expected = {"event_report": report}
    def existing(read):
        return {"published": False, "generation": read["state"]["generation"],
            "reason_code": "EVENT_STUDY_UNCHANGED" if read["state"]["payload"] == expected else "EVENT_STUDY_IDENTITY_CONFLICT",
            "study_ref": ref}
    read = await state.read_state(session_factory, **ref)
    if not read["available"]:
        return {"published": False, "reason_code": read["reason_code"]}
    if read["found"]:
        return existing(read)
    result = await state.publish_generation(session_factory, **ref, expected_generation=0,
        period_key=str(report["manifest"]["split"]["as_of_ms"]),
        evidence_key=report["report_hash"], now_ms=report["evidence_inputs"]["now_ms"], payload=expected)
    if not result.get("published"):
        reread = await state.read_state(session_factory, **ref)
        if reread["available"] and reread["found"]:
            return existing(reread)
    return {**result, "study_ref": ref}


async def load_study(session_factory, ref):
    from services import policy_state_service as state
    _closed(ref, {"experiment_key", "universe_version", "population"}, "study_ref")
    _require(ref["experiment_key"].startswith("r13evt:") and ref["population"] == "SHADOW", "EVENT_STUDY_REF")
    read = await state.read_state(session_factory, **ref)
    if not read["available"] or not read["found"]:
        return {"available": False, "reason_code": read["reason_code"]}
    report = read["state"]["payload"].get("event_report")
    checked = verify_report(report)
    if checked["ok"] and (read["state"]["evidence_key"] != report["report_hash"] or study_reference(report) != ref):
        checked = {"ok": False, "reason_code": "EVENT_PERSISTED_IDENTITY_DRIFT"}
    return {"available": checked["ok"], "reason_code": checked["reason_code"],
            "report": report if checked["ok"] else None}


def build_event_envelope(report):
    from services import preselection_experiment_service as catalog
    return {"experiment_type": catalog.TYPE_PRE_SELECTION, "experiment_type_version": "R12_PRE_SELECTION_EVENT_V1",
        "contract_hash": digest(report["identity"]), "manifest_hash": report["manifest"]["manifest_hash"],
        "event_manifest": copy.deepcopy(report["manifest"])}


def validate_event_envelope(config):
    from services import preselection_experiment_service as catalog
    try:
        _closed(config, {"experiment_type", "experiment_type_version", "contract_hash", "manifest_hash", "event_manifest"}, "event_envelope")
        _require(config["experiment_type"] == catalog.TYPE_PRE_SELECTION
            and config["experiment_type_version"] == "R12_PRE_SELECTION_EVENT_V1"
            and _hash(config["contract_hash"]), "EVENT_ENVELOPE_TYPE")
        checked = verify_manifest(config["event_manifest"], expected_hash=config["manifest_hash"])
        _require(checked["ok"], "EVENT_ENVELOPE_MANIFEST")
        return {"ok": True, "reason_code": "OK", "manifest": checked["manifest"], "config": copy.deepcopy(config)}
    except (ValueError, TypeError, KeyError) as error:
        return {"ok": False, "reason_code": str(error)}


async def create_event_experiment(*, champion, config, objective, study_ref, cutoff, fingerprint):
    """Only a DRAFT in the existing catalogue, from a persisted verified study."""
    from services import strategy_evidence_service as official
    if not official.P05_ANALYTICS_ENABLED or objective not in official.OBJECTIVES:
        return {"ok": False, "reason_code": "EVENT_ANALYTICS_DISABLED_OR_OBJECTIVE_INVALID"}
    from db import get_session, DB_ENABLED
    if not DB_ENABLED:
        return {"ok": False, "reason_code": "DB_DISABLED"}
    loaded = await load_study(get_session, study_ref)
    if not loaded["available"]:
        return {"ok": False, "reason_code": loaded["reason_code"]}
    report = loaded["report"]
    if (config != build_event_envelope(report) or champion != report["manifest"]["baseline"]
            or fingerprint != report["identity"]["dataset_hash"]
            or int(cutoff.timestamp() * 1000) != report["manifest"]["split"]["as_of_ms"]):
        return {"ok": False, "reason_code": "EVENT_CATALOG_IDENTITY_MISMATCH"}
    # The expensive reproduction was made at ingestion, not in a read endpoint.
    # Lock/upsert and the immutable official DRAFT contract remain unchanged.
    async with get_session() as session:
        await official._acquire_p05_lock(session, official._P05_EVALUATE_LOCK_KEY)
        exp = await official._upsert_experiment(session, experiment_key="event:" + digest({"config": config, "objective": objective}),
            champion_hash=digest(champion), candidate_hash=digest(config), objective=objective, config=config,
            fingerprint=fingerprint, cutoff=cutoff, offline={"verdict": "DRAFT", "study_ref": study_ref,
                "phase": STUDY_VERSION, "state": report["state"], "acceptance": report["evidence"]["acceptance"],
                "promotable": False, "operational_authority": "NONE"}, expected_type=official.PRE_SELECTION_TYPE)
        await session.flush()
        answer = {"ok": True, "experiment_id": exp.id, "status": exp.status, "promotable": False,
                  "reason_code": "EVENT_RESEARCH_CATALOGUED_NOT_APPROVED"}
        await session.commit()
        return answer


async def load_latest_status(session_factory):
    """One SELECT; no execution, fitting, bootstrap or writes in a GET."""
    from sqlalchemy import select
    from models.policy_simulation_state import PolicySimulationState as State
    try:
        async with session_factory() as session:
            row = (await session.execute(select(State.payload, State.evidence_key, State.experiment_key, State.universe_version)
                .where(State.experiment_key.like("r13evt:%"), State.population == "SHADOW")
                .order_by(State.published_at_ms.desc(), State.id.desc()).limit(1))).first()
        if row is None:
            return {"available": True, "state": "NOT_STARTED", "promotable": False}
        payload, evidence_key, experiment_key, universe_version = row
        report = payload.get("event_report")
        checked = verify_report(report)
        if checked["ok"] and (evidence_key != report["report_hash"] or study_reference(report) != {
                "experiment_key": experiment_key, "universe_version": universe_version, "population": "SHADOW"}):
            checked = {"ok": False, "reason_code": "EVENT_PERSISTED_IDENTITY_DRIFT"}
        if not checked["ok"]:
            return {"available": False, "state": "ERROR", "reason_code": checked["reason_code"], "promotable": False}
        return {"available": True, "state": report["state"], "version": STUDY_VERSION,
            "manifest_hash": report["manifest"]["manifest_hash"], "observed_at_ms": report["evidence_inputs"]["now_ms"],
            "technical_gaps": report["technical_gaps"], "acceptance": report["evidence"]["acceptance"],
            "holdout_status": "SEALED", "promotable": False, "authorizes_live": False}
    except Exception:
        return {"available": False, "state": "ERROR", "reason_code": "EVENT_STUDY_READ_ERROR", "promotable": False}
