"""One-shot hermetic adapter for real LIVE entry callers; never a scheduler."""
from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import socket
import sys
import time
import types


def load_source(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class Gap(Exception):
    pass


class Runtime:
    MUTATIONS = {"send_order", "cancel_order", "place_protection", "set_leverage",
                 "emergency_close", "cleanup"}

    def __init__(self, payload, contract):
        self.payload, self.contract = payload, contract
        self.at_ms = payload["proposal"]["decision_at_ms"]
        self.index, self.last_action = 0, 0
        self.calls, self.gaps, self.blocked_network, self.used = [], [], [], set()
        self.last_action_ms = 0
        self.admitted_orders, self.order_dispatch, self.dispatch_qty = [], {}, {}
        self.dispatch_proposals = []
        self.current_dispatch = payload["proposal"]["dispatch_id"]
        self.admissions = []
        self.transitions = []
        self.caller_calls = []

    def gap(self, reason, **details):
        self.gaps.append({"reason_code": reason, **details})

    async def call(self, port, args=(), kwargs=None):
        kwargs = kwargs or {}
        self.index += 1
        index, after = self.index, self.last_action
        request_hash = self.contract.call_hash(port, args, kwargs)
        event = {"port": port, "args": list(args), "kwargs": copy.deepcopy(kwargs),
                 "call_index": index, "after_action_index": after,
                 "at_ms": self.at_ms, "request_hash": request_hash,
                 "mutation": port in self.MUTATIONS}
        self.calls.append(event)
        rows = [r for r in self.payload["receipts"] if r["call_index"] == index]
        if not rows:
            self.gap("RECEIPT_MISSING", call_index=index, port=port)
            raise Gap("receipt missing")
        row = rows[0]
        if (row["port"] != port or row["request_hash"] != request_hash
                or row["after_action_index"] != after):
            self.gap("RECEIPT_ACTION_BINDING_MISMATCH", call_index=index, port=port)
            raise Gap("receipt not bound")
        if not self.last_action_ms <= row["observed_at_ms"] <= row["available_at_ms"] <= self.at_ms:
            self.gap("RECEIPT_STALE_OR_FUTURE", call_index=index, port=port)
            raise Gap("receipt causal boundary")
        self.used.add(index)
        event["receipt_hash"] = self.contract.digest(row)
        if port in self.MUTATIONS:
            self.last_action, self.last_action_ms = index, self.at_ms
        event["value"] = copy.deepcopy(row["value"])
        return copy.deepcopy(row["value"])

    async def sleep(self, seconds):
        self.at_ms += max(1, int(seconds * 1000))


def network_guard(runtime):
    original = socket.socket

    def deny(name, *args, **kwargs):
        runtime.blocked_network.append({"operation": name})
        runtime.gap("NETWORK_ATTEMPT_BLOCKED", operation=name)
        raise PermissionError("event replica has no external network")

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
    """Only exact in-memory dependencies used by the selected source bodies."""
    import importlib.abc

    class Guard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if (fullname.startswith("services.") or fullname.startswith("models.")
                    or fullname == "db" or fullname.split(".")[0] in
                    {"httpx", "requests", "aiohttp", "asyncpg"}):
                runtime.gap("OFFLINE_IMPORT_BLOCKED", module=fullname)
                raise ImportError(fullname)
            return None

    sys.meta_path.insert(0, Guard())


async def run(payload, backend):
    contract = load_source("_event_execution_contract", backend / "services/live_event_execution_service.py")
    payload = contract.validate_execution_request(payload,
                                                  expected_source_fingerprint=contract.execution_source_fingerprint())
    runtime = Runtime(payload, contract)
    network_guard(runtime)
    config, proposal, portfolio = (payload[key] for key in ("config", "proposal", "portfolio"))
    admission = contract.compute_admission(config, proposal, portfolio)
    runtime.admissions.append(admission)
    functions = contract.source_functions
    log = logging.getLogger("offline_event_execution")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    revalidation_path = backend / "services/entry_revalidation_service.py"
    revalidation = load_source("_event_entry_revalidation", revalidation_path)
    clock = types.SimpleNamespace(time=lambda: runtime.at_ms / 1000,
                                  time_ns=lambda: runtime.at_ms * 1000000)
    signed = functions("services/binance_signed_service.py",
                       {"_floor_to_step", "_round_qty", "_round_price", "to_binance",
                        "market_fallback_client_order_id", "_is_ambiguous_order_submission",
                        "_protection_exception_result", "_protection_not_requested_result",
                        "reduce_only_flag_is_valid", "classify_order_action", "_reduce_only_payload_conflict",
                        "_enforce_entry_safety", "place_order", "place_maker_entry_then_protect"},
                       {"log": log, "hashlib": hashlib, "time": clock,
                        "asyncio": types.SimpleNamespace(sleep=runtime.sleep),
                        "_AMBIGUOUS_ORDER_CODES": contract.source_literal("services/binance_signed_service.py",
                                                                        "_AMBIGUOUS_ORDER_CODES"),
                        "_MAKER_POLL_TIMEOUT_S": config["MAKER_ENTRY_TIMEOUT_S"],
                        "_MAKER_POLL_INTERVAL_S": config["MAKER_ENTRY_POLL_INTERVAL_S"],
                        "_MAKER_CANCEL_CONFIRM_ATTEMPTS": config["BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS"],
                        "_MAKER_CANCEL_CONFIRM_DELAY": config["BINANCE_MAKER_CANCEL_CONFIRM_DELAY"]})

    async def filters(symbol):
        return copy.deepcopy(config["filters"])

    async def ownership(*args):
        if portfolio["ownership"] != "BOT":
            return {"ok": False, "reason_code": "MANUAL_POSITION_SYMBOL_BLOCKED"}
        return None

    async def leverage(symbol, value, **kwargs):
        return await runtime.call("set_leverage", (symbol, value))

    async def get_order(symbol, **kwargs):
        return await runtime.call("get_order", (symbol,), kwargs)

    async def cancel_order(symbol, **kwargs):
        return await runtime.call("cancel_order", (symbol,), kwargs)

    async def protect(*args, **kwargs):
        return await runtime.call("place_protection", args, kwargs)

    async def emergency(*args, **kwargs):
        return await runtime.call("emergency_close", args, kwargs)

    async def cleanup(*args, **kwargs):
        return await runtime.call("cleanup", args, kwargs)

    async def final_authorization():
        return admission["decision"] == "ADMITTED" and portfolio["live_guards_allowed"]

    async def request(method, path, params, *, request_preflight=None, mutation=None):
        if request_preflight is not None:
            verdict = await request_preflight()
            if verdict.get("ok") is not True:
                return {"ok": False, "_preflight": True, "preflight": verdict,
                        "reason_code": verdict.get("reason_code"), "error": verdict.get("reason")}
        if mutation and mutation.get("final_check"):
            if await mutation["final_check"]() is not True:
                return {"ok": False, "_preflight": True, "reason_code": "FINAL_AUTHORITY_DENIED"}
        dispatch = params.get("newClientOrderId")
        if dispatch:
            runtime.current_dispatch = dispatch
            runtime.dispatch_qty[dispatch] = params["quantity"]
            frozen = {"dispatch_id": dispatch, "order_type": params["type"],
                      "time_in_force": params.get("timeInForce"), "qty": params["quantity"],
                      "price": params.get("price"), "stop": proposal["stop"],
                      "opportunity_id": proposal["opportunity_id"], "trade_id": proposal["trade_id"],
                      "admission_hash": runtime.admissions[-1]["admission_hash"],
                      "created_at_ms": runtime.at_ms}
            runtime.dispatch_proposals.append({**frozen, "proposal_hash": contract.digest(frozen)})
        res = await runtime.call("send_order", (method, path, dict(params)))
        if not isinstance(res, dict):
            runtime.gap("RAW_RECEIPT_INVALID")
            return {"ok": False, "error": "invalid raw receipt", "_request_sent": True}
        order = res.get("result")
        if res.get("ok") is True and isinstance(order, dict):
            oid = order.get("orderId")
            if oid is not None and str(oid):
                role = "PYRAMID" if payload["mode"] == "PYRAMID" else "ENTRY"
                binding = {"order_id": str(oid), "dispatch_id": dispatch, "role": role}
                if binding not in runtime.admitted_orders:
                    runtime.admitted_orders.append(binding)
                runtime.order_dispatch[str(oid)] = dispatch
            if str(order.get("status") or "").upper() == "FILLED":
                for key in ("executedQty", "avgPrice"):
                    try:
                        number = float(order[key])
                        if not 0 < number < float("inf") or type(order[key]) is bool:
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        runtime.gap("RAW_FILL_FIELDS_MISSING", field=key)
            raw_qty = order.get("executedQty")
            try:
                if raw_qty is not None and float(raw_qty) > params["quantity"] + 1e-12:
                    runtime.gap("FILL_QTY_EXCEEDS_DISPATCH", dispatch_id=dispatch)
            except (TypeError, ValueError):
                runtime.gap("RAW_FILL_QTY_INVALID")
        return res

    common = {"symbol": proposal["symbol"], "side": proposal["side"],
              "planned_entry": proposal["entry"], "stop_loss": proposal["stop"],
              "tp1": proposal["tp1"], "tp2": proposal["tp2"], "atr": proposal["atr"],
              "entry_zone_low": proposal["entry_zone_low"], "entry_zone_high": proposal["entry_zone_high"]}

    async def preflight_maker(price, qty):
        quote = await runtime.call("quote", (proposal["symbol"],))
        limits = {k: v for k, v in config["limits"].items()
                  if k not in ("max_depth_age_ms", "max_book_impact_pct")}
        verdict = revalidation.evaluate_entry_revalidation(
            **common, **limits, quote=quote, maker_limit_price=price,
            enforce_adverse_slippage=True, now_ms=runtime.at_ms)
        approved = revalidation.cap_qty_for_revalidated_entry(qty, proposal["entry"], proposal["stop"], price)
        if verdict.get("ok"):
            final = contract.compute_admission(config, proposal, portfolio,
                                               final_qty=approved, final_entry=price)
            runtime.admissions.append(final)
            verdict["ok"] = final["decision"] == "ADMITTED"
            verdict["approved_qty"] = approved
        return verdict

    async def preflight_market(qty, rules):
        depth = await runtime.call("depth", (proposal["symbol"], qty))
        limits = {k: v for k, v in config["limits"].items() if k != "max_quote_age_ms"}
        verdict = revalidation.evaluate_market_depth_revalidation(
            **common, **limits, depth=depth, qty=qty, now_ms=runtime.at_ms)
        if verdict.get("ok"):
            price = verdict["checks"]["worst_price"]
            approved = revalidation.cap_qty_for_revalidated_entry(qty, proposal["entry"], proposal["stop"], price)
            approved = signed["_floor_to_step"](approved, rules["step"])
            final = contract.compute_admission(config, proposal, portfolio,
                                               final_qty=approved, final_entry=price,
                                               consumes_slot=payload["mode"] != "PYRAMID")
            runtime.admissions.append(final)
            verdict["ok"] = final["decision"] == "ADMITTED"
            verdict["approved_qty"] = approved
        return verdict

    signed.update({"_get_symbol_filters": filters, "_manual_ownership_block": ownership,
                   "set_leverage": leverage, "_signed_request": request, "get_order": get_order,
                   "cancel_order": cancel_order, "_place_post_fill_protection": protect,
                   "_emergency_close_after_stop_failure": emergency, "_cleanup_entry_conditionals": cleanup,
                   "_fire_telegram": lambda *a: None})
    result, persisted = None, None
    install_import_guard(runtime)
    try:
        if payload["mode"] == "PYRAMID":
            result, persisted = await pyramid(runtime, contract, signed, preflight_market, log)
        elif admission["decision"] != "ADMITTED":
            result = {"ok": False, "entry_not_submitted": True, "no_fill": True}
        else:
            route = revalidation.select_entry_route(
                maker_enabled=config["MAKER_ENTRY_ENABLED"], maker_available=True,
                preflight_enabled=config["P04A_ENTRY_REVALIDATION_ENABLED"])
            if route.startswith("blocked") or not config["P04B_MARKET_REVALIDATION_ENABLED"]:
                runtime.gap("EXECUTION_REQUIRED_MODE_TECHNICALLY_UNSUPPORTED")
            else:
                args = {"symbol": proposal["symbol"], "side": "Buy" if proposal["side"] == "long" else "Sell",
                        "qty": admission["final_qty"], "stop_loss": proposal["stop"],
                        "take_profit": proposal["tp2"], "tp1": proposal["tp1"],
                        "tp1_qty_pct": config["TP1_QTY_PCT"], "leverage": proposal["leverage"],
                        "client_order_id": proposal["dispatch_id"], "final_authorization": final_authorization}
                if route == "maker":
                    runtime.caller_calls.append("binance_signed_service.place_maker_entry_then_protect")
                    result = await signed["place_maker_entry_then_protect"](
                        **args, limit_price=proposal["entry"], entry_preflight=preflight_maker,
                        market_preflight=preflight_market,
                        fallback_market=(config["MAKER_FALLBACK_MARKET"] and config["P04B_MAKER_FALLBACK_ENABLED"]))
                else:
                    runtime.caller_calls.append("binance_signed_service.place_order")
                    result = await signed["place_order"](**args, order_type="Market", entry_preflight=preflight_market)
    except Exception as exc:
        runtime.gap("CALLER_EXCEPTION", exception_type=type(exc).__name__)
    fills, fill_qty, fill_basis = validate_fills(runtime)
    outcome = result if isinstance(result, dict) else {}
    if payload["mode"] == "PYRAMID" and outcome.get("no_op"):
        status = "NO_OP" if not runtime.gaps else "UNKNOWN"
    elif admission["decision"] != "ADMITTED" and payload["mode"] == "ENTRY":
        status = "REJECTED"
    else:
        expected = outcome.get("executed_qty")
        if expected is None:
            expected = (outcome.get("result") or {}).get("executedQty")
        if outcome.get("entry_confirmed") is True or outcome.get("pyramid_applied") is True:
            if expected is None or abs(fill_qty - float(expected)) > 1e-9 or fill_qty <= 0:
                runtime.gap("ATTRIBUTED_FILLS_MISSING_OR_QUANTITY_DIVERGENT")
            receipt_basis = outcome.get("entry_fill_price") or (outcome.get("result") or {}).get("avgPrice")
            if (receipt_basis is not None and fill_basis is not None
                    and abs(float(receipt_basis) - fill_basis) > max(1e-9, abs(fill_basis) * 1e-9)):
                runtime.gap("FILL_PRICE_RECEIPT_DIVERGENT")
        elif not outcome.get("no_fill"):
            runtime.gap("ENTRY_FILL_OR_TERMINAL_UNKNOWN")
        if (outcome.get("entry_confirmed") is True and outcome.get("position_open") is not True
                or outcome.get("quarantine_required") is True
                or outcome.get("manual_intervention_required") is True):
            runtime.gap("PROTECTION_OR_POSITION_CONSEQUENCE_UNRESOLVED")
        status = "UNKNOWN" if runtime.gaps else "COMPLETE_EXECUTION"
        if any(g["reason_code"] in {"FILL_QTY_EXCEEDS_DISPATCH", "FILL_ID_CONFLICT",
                                   "FILL_ATTRIBUTION_CONFLICT", "PYRAMID_MANAGER_QTY_DIVERGENT"}
               for g in runtime.gaps):
            status = "CONFLICT"
    unknown = status in ("UNKNOWN", "CONFLICT")
    confirmed = status == "COMPLETE_EXECUTION" and fill_qty > 0
    final_admission = runtime.admissions[-1]
    reservations = {"state": "UNKNOWN_RETAINED" if unknown else "TRANSFERRED_TO_POSITION" if confirmed else "RELEASED",
                    "qty": final_admission["final_qty"] if unknown else 0,
                    "margin_usd": final_admission["margin_usd"] if unknown else 0,
                    "risk_usd": final_admission["risk_usd"] if unknown else 0,
                    "slot_retained": unknown and payload["mode"] == "ENTRY"}
    updates = []
    if confirmed:
        body = {"version": contract.UPDATE_VERSION, "producer": "live_event_execution_service",
                "request_hash": payload["request_hash"], "opportunity_id": proposal["opportunity_id"],
                "trade_id": proposal["trade_id"], "dispatch_ids": sorted(runtime.dispatch_qty),
                "kind": "PYRAMID_FILL" if payload["mode"] == "PYRAMID" else "ENTRY_FILL",
                "qty_added": fill_qty, "ledger_entry_basis": fill_basis,
                "manager_entry_price": persisted["entry_price"] if persisted else fill_basis,
                "manager_qty": persisted["qty"] if persisted else fill_qty,
                "pyramiding_level": persisted["pyramiding_level"] if persisted else 0,
                "observed_at_ms": max(f["observed_at_ms"] for f in fills),
                "available_at_ms": max(f["available_at_ms"] for f in fills),
                "test_only": payload["test_only"], "real_study_allowed": False}
        updates.append({**body, "update_hash": contract.digest(body)})
    report = {"version": contract.VERSION, "source": payload["source"], "test_only": payload["test_only"],
              "execution_quality": contract.SOURCES[payload["source"]],
              "source_provenance": payload["source_provenance"],
              "real_study_allowed": False, "authorizes_live": False, "promotable": False,
              "request_hash": payload["request_hash"], "source_fingerprint": payload["source_fingerprint"],
              "runtime_identity": contract.runtime_identity(), "status": status,
              "admission": final_admission, "admissions": runtime.admissions,
              "actions": [r for r in runtime.calls if r["mutation"]],
              "reads": [r for r in runtime.calls if not r["mutation"]],
              "caller_result": result, "admitted_orders": runtime.admitted_orders,
              "dispatch_proposals": runtime.dispatch_proposals,
              "fills": fills, "filled_qty": fill_qty, "entry_basis": fill_basis,
              "reservations": reservations, "manager_updates": updates,
              "memory_transitions": runtime.transitions, "gaps": runtime.gaps,
              "caller_calls": runtime.caller_calls,
              "unused_receipts": [r["call_index"] for r in payload["receipts"] if r["call_index"] not in runtime.used],
              "blocked_network_attempts": runtime.blocked_network,
              "caller_identities": {n: {"source_file": signed[n].__code__.co_filename,
                                         "first_line": signed[n].__code__.co_firstlineno}
                                    for n in ("place_order", "place_maker_entry_then_protect", "_enforce_entry_safety")},
              "isolation": {"input_credentials_inherited": False, "real_environment_inherited": False,
                            "actual_exchange_clients_imported": False, "database": "memory_only"}}
    if contract.execution_source_fingerprint() != payload["source_fingerprint"]:
        raise ValueError("source drift during replay")
    return {**report, "report_hash": contract.digest(report)}


def validate_fills(runtime):
    p = runtime.payload["proposal"]
    wrapper_fields = {"event_id", "causal_index", "observed_at_ms", "available_at_ms", "binding", "raw", "role"}
    bindings = {"account_scope": p["account_ref"], "exchange": p["exchange"], "market": p["market"],
                "symbol": p["symbol"], "quote": p["quote"], "side": p["side"].upper(),
                "position_side": p["position_side"], "trade_id": p["trade_id"],
                "opportunity_id": p["opportunity_id"]}
    expected_role = "PYRAMID" if runtime.payload["mode"] == "PYRAMID" else "ENTRY"
    raw_fields = {"id", "orderId", "symbol", "positionSide", "side", "price", "qty",
                  "realizedPnl", "commission", "commissionAsset", "time"}
    admitted = {(a["order_id"], a["dispatch_id"], a["role"]) for a in runtime.admitted_orders}
    seen, fills, total, cost, quantities = {}, [], 0.0, 0.0, {}
    for fill in runtime.payload["fills"]:
        try:
            runtime.contract._closed(fill, wrapper_fields, "fill")
            binding = fill["binding"]
            runtime.contract._closed(binding, set(bindings) | {"order_id", "dispatch_id"}, "fill.binding")
            if (any(binding[k] != v for k, v in bindings.items()) or fill["role"] != expected_role
                    or (str(binding["order_id"]), binding["dispatch_id"], fill["role"]) not in admitted):
                raise ValueError("attribution")
            raw = fill["raw"]
            runtime.contract._closed(raw, raw_fields, "fill.raw")
            runtime.contract._text(fill["event_id"], "fill.event_id")
            if raw["id"] is None or type(raw["id"]) not in (int, str):
                raise ValueError("exec id missing")
            runtime.contract._text(str(raw["id"]), "fill.exec_id")
            if (str(raw.get("orderId")) != str(binding["order_id"])
                    or raw.get("symbol") != p["symbol"].split(":")[0].replace("/", "")
                    or raw.get("positionSide") != p["position_side"]
                    or raw.get("side") != ("BUY" if p["side"] == "long" else "SELL")):
                raise ValueError("raw attribution")
            qty, price = float(raw["qty"]), float(raw["price"])
            if (type(raw["qty"]) is bool or type(raw["price"]) is bool
                    or not 0 < qty < float("inf") or not 0 < price < float("inf")
                    or not p["decision_at_ms"] <= raw["time"] <= fill["observed_at_ms"] <= fill["available_at_ms"] <= runtime.at_ms
                    or type(fill["causal_index"]) is not int or fill["causal_index"] <= 0):
                raise ValueError("quantity or causal")
            material = runtime.contract.digest({"binding": binding, "raw": raw, "role": fill["role"]})
            identity = (p["account_ref"], raw["symbol"], raw["positionSide"], str(raw["id"]))
            if identity in seen:
                if seen[identity] != material:
                    runtime.gap("FILL_ID_CONFLICT", exec_id=str(raw["id"]))
                continue
            seen[identity] = material
            fills.append(copy.deepcopy(fill))
            total += qty
            cost += qty * price
            d = binding["dispatch_id"]
            quantities[d] = quantities.get(d, 0) + qty
        except (ValueError, KeyError, TypeError):
            runtime.gap("FILL_ATTRIBUTION_CONFLICT", event_id=fill.get("event_id") if isinstance(fill, dict) else None)
    for dispatch, qty in quantities.items():
        if qty > runtime.dispatch_qty.get(dispatch, 0) + 1e-9:
            runtime.gap("FILL_QTY_EXCEEDS_DISPATCH", dispatch_id=dispatch)
    return fills, total, cost / total if total else None


async def pyramid(runtime, contract, signed, market_preflight, log):
    payload, p = runtime.payload, runtime.payload["proposal"]
    config, portfolio, initial = payload["config"], payload["portfolio"], payload["pyramid_state"]
    state = types.SimpleNamespace(**copy.deepcopy(initial))
    if config["PYRAMIDING_ENABLED"] and not all(initial[k] for k in
                                               ("sl_close_position", "tp2_close_position", "protection_confirmed")):
        runtime.gap("PYRAMID_PROTECTION_UNCONFIRMED")
        return {"ok": False}, None
    import sqlalchemy
    from sqlalchemy.orm import declarative_base
    db = types.ModuleType("db")
    db.Base = declarative_base()
    sys.modules["db"] = db
    models = types.ModuleType("models")
    models.__path__ = []
    sys.modules["models"] = models
    model = load_source("models.real_trade", contract.BACKEND / "models/real_trade.py")
    persisted = model.RealTrade(**{k: v for k, v in initial.items() if k in model.RealTrade.__table__.columns})

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            compiled = statement.compile()
            if (len(statement.get_final_froms()) != 1 or statement.get_final_froms()[0].name != "real_trades"
                    or list(compiled.params.values()) != [p["trade_id"]]):
                runtime.gap("PYRAMID_UNSUPPORTED_MEMORY_QUERY")
                raise Gap("query")
            return types.SimpleNamespace(scalar_one_or_none=lambda: persisted)

        async def commit(self):
            runtime.transitions.append({"producer": "shadow_trade_service._maybe_pyramid",
                                        "qty_before": initial["qty"], "qty_after": persisted.qty,
                                        "entry_before": initial["entry_price"], "entry_after": persisted.entry_price,
                                        "pyramiding_level": persisted.pyramiding_level})

    db.get_session = Session
    services = types.ModuleType("services")
    services.__path__ = []
    sys.modules["services"] = services

    def fake(name, **attrs):
        module = types.ModuleType("services." + name)
        module.__dict__.update(attrs)
        sys.modules[module.__name__] = module
        setattr(services, name, module)

    async def paused():
        return portfolio["paused"]

    async def kill():
        return {"allowed": portfolio["kill_allowed"]}

    async def mark(symbol):
        response = await runtime.call("quote", (symbol,))
        return response.get("last") if isinstance(response, dict) else None

    async def place(**kwargs):
        qty = kwargs["qty"]
        dispatch = kwargs["client_order_id"]
        expected = f"cw-pyr-{p['trade_id']}-{initial['pyramiding_level'] + 1}"
        if dispatch != expected:
            runtime.gap("PYRAMID_DISPATCH_CONFLICT")
            return {"ok": False}
        final = contract.compute_admission(config, p, portfolio, final_qty=qty,
                                           final_entry=p["entry"], consumes_slot=False)
        runtime.admissions.append(final)
        if final["decision"] != "ADMITTED":
            return {"ok": False, "reason": final["reason_codes"]}
        # The current LIVE reinforcement caller omits the required P04B
        # preflight. Preserve that exact call: ON returns a technical blocker,
        # never secretly add a different order policy to make it pass.
        response = await signed["place_order"](**kwargs)
        if response.get("reason_code") == "EXEC_MARKET_PREFLIGHT_REQUIRED":
            runtime.gap("TECHNICAL_LIVE_PYRAMID_MARKET_PREFLIGHT_MISSING")
        return response

    async def notify(**kwargs):
        return None

    fake("exchange_service", place_order=place)
    fake("risk_service", is_paused=paused)
    fake("kill_switch_service", check_can_trade=kill)
    fake("push_service", notify_alert=notify)
    namespace = contract.source_functions("services/shadow_trade_service.py", {"_maybe_pyramid"},
                                         {**config, "SHADOW_ENABLED": False, "DB_ENABLED": True,
                                          "_EXECUTION_QUARANTINE_REASON": None, "log": log,
                                          "_get_mark_price": mark,
                                          "_live_money_guard": lambda: (portfolio["live_guards_allowed"], "frozen"),
                                          "_live_execution_contract_guard": lambda: (portfolio["live_guards_allowed"], "frozen"),
                                          "_trade_active_exchange_guard": lambda trade: (True, "frozen"),
                                          "_arm_execution_quarantine": lambda *a: runtime.gap("PYRAMID_QUARANTINE")})
    runtime.caller_calls.append("shadow_trade_service._maybe_pyramid")
    applied = await namespace["_maybe_pyramid"](state, {"entry": p["entry"], "stop_loss": p["stop"]})
    if applied:
        # This branch is not reachable with the current LIVE preflight omission.
        # Keep the writer reconciliation explicit if a later source fixes it.
        return {"ok": True, "pyramid_applied": True,
                "executed_qty": persisted.qty - initial["qty"]}, {
                    "qty": persisted.qty, "entry_price": persisted.entry_price,
                    "pyramiding_level": persisted.pyramiding_level}
    return {"ok": False, "no_op": not runtime.gaps, "no_fill": not runtime.gaps}, None


def main():
    try:
        payload = json.load(sys.stdin)
        backend = Path(sys.argv[1]).resolve()
        # No backend path is added to import resolution; only source functions
        # and exact in-memory dependencies are admitted.
        report = asyncio.run(run(payload, backend))
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "reason_code": "EVENT_EXECUTION_WORKER_FAILED",
                          "exception_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
