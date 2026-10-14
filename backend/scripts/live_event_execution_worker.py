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
        self.initial_reservation = None
        self.port_state = copy.deepcopy(payload.get("port_state"))

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
    native_sizing = None
    if "sizing_stack" in payload["config"] or "upstream_sizing" in payload:
        package = types.ModuleType("services")
        package.__path__ = []
        package.live_event_execution_service = contract
        sys.modules["services"] = package
        sys.modules["services.live_event_execution_service"] = contract
        upstream = load_source("services.live_upstream_sizing_service", backend / "services/live_upstream_sizing_service.py")
        package.live_upstream_sizing_service = upstream
        if "upstream_sizing" in payload:
            native_sizing = {"request": payload["upstream_sizing"], "report": upstream.replay(payload["upstream_sizing"])}
    payload = contract.validate_execution_request(payload,
                                                  expected_source_fingerprint=contract.execution_source_fingerprint())
    runtime = Runtime(payload, contract)
    network_guard(runtime)
    config, proposal, portfolio = (payload[key] for key in ("config", "proposal", "portfolio"))
    admission = contract.compute_admission(config, proposal, portfolio, native_sizing=native_sizing)
    runtime.admissions.append(admission)
    functions = contract.source_functions
    log = logging.getLogger("offline_event_execution")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    revalidation_path = backend / "services/entry_revalidation_service.py"
    revalidation = load_source("_event_entry_revalidation", revalidation_path)
    clock = types.SimpleNamespace(time=lambda: runtime.at_ms / 1000,
                                  time_ns=lambda: runtime.at_ms * 1000000)
    revalidation.time = clock  # actual default-clock P04 helpers use the replay's causal clock
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
        if payload["mode"] == "PYRAMID" and hasattr(runtime, "pyramid_ownership"):
            return await runtime.pyramid_ownership(*args)
        if portfolio["ownership"] != "BOT":
            return {"ok": False, "reason_code": "MANUAL_POSITION_SYMBOL_BLOCKED"}
        return None

    async def leverage(symbol, value, **kwargs):
        if payload["mode"] == "PYRAMID" and value != proposal["leverage"]:
            runtime.gap("PYRAMID_LEVERAGE_CHANGED")
            return {"ok": False, "reason_code": "PYRAMID_LEVERAGE_CHANGED"}
        return await runtime.call("set_leverage", (symbol, value))

    async def get_order(symbol, **kwargs):
        return await runtime.call("get_order", (symbol,), kwargs)

    async def cancel_order(symbol, **kwargs):
        return await runtime.call("cancel_order", (symbol,), kwargs)

    no_obligation_protection = functions(
        "services/binance_signed_service.py", {"_place_post_fill_protection"}, signed
    )["_place_post_fill_protection"]

    async def protect(*args, **kwargs):
        if all(kwargs.get(key) is None for key in ("stop_loss", "tp1", "tp2")):
            # Native no-obligation branch does not call the broker. Its true
            # flags mean "not requested", never a confirmed SL for PYRAMID.
            runtime.caller_calls.append("binance_signed_service._place_post_fill_protection")
            return await no_obligation_protection(*args, **kwargs)
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
            final_verdict = await mutation["final_check"]()
            denied_reason = "FINAL_AUTHORITY_DENIED"
            denied_detail = {}
            if payload["mode"] == "PYRAMID":
                authorized = isinstance(final_verdict, dict) and final_verdict.get("ok") is True
                sync = final_verdict.get("sync_check") if authorized else None
                sync_verdict = sync(dict(params)) if authorized and callable(sync) else None
                authorized = isinstance(sync_verdict, dict) and sync_verdict.get("ok") is True
                if isinstance(sync_verdict, dict):
                    denied_reason = sync_verdict.get("reason_code") or denied_reason
                    denied_detail = sync_verdict
                elif isinstance(final_verdict, dict):
                    denied_reason = final_verdict.get("reason_code") or denied_reason
                    denied_detail = {key: final_verdict[key] for key in ("ok", "reason_code", "reason")
                                     if key in final_verdict}
            else:
                authorized = final_verdict is True
            if not authorized:
                return {"ok": False, "_preflight": True, "_request_sent": False,
                        "reason_code": denied_reason, "preflight": denied_detail,
                        "error": str(denied_detail.get("reason") or "autorização final negou o envio")}
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
                                               final_qty=approved, final_entry=price, native_sizing=native_sizing)
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
                                               consumes_slot=payload["mode"] != "PYRAMID", native_sizing=native_sizing)
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
        if payload["mode"] == "PYRAMID":
            writer = copy.deepcopy(runtime.port_state["trade"])
            body.update(notes=writer["notes"], writer_state=writer, writer_state_hash=contract.digest(writer),
                        port_state_hash=runtime.port_state["state_hash"],
                        writer_observed_at_ms=runtime.at_ms, writer_available_at_ms=runtime.at_ms,
                        retained_confirmed_risk_usd=runtime.port_state["retained_risk_usd"])
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
              "initial_reservation": copy.deepcopy(runtime.initial_reservation),
              "upstream_sizing_report": native_sizing["report"] if native_sizing is not None else None,
              "caller_calls": runtime.caller_calls,
              "port_state": runtime.port_state,
              "persistence_scope": "RECEIPT_BOUND_SIMULATED_PORTS" if payload["mode"] == "PYRAMID" else "ENTRY_MEMORY",
              "retained_confirmed_risk_usd": (runtime.port_state or {}).get("retained_risk_usd", 0),
              "unused_receipts": [r["call_index"] for r in payload["receipts"] if r["call_index"] not in runtime.used],
              "blocked_network_attempts": runtime.blocked_network,
              "caller_identities": {n: {"source_file": signed[n].__code__.co_filename,
                                         "first_line": signed[n].__code__.co_firstlineno}
                                    for n in ("place_order", "place_maker_entry_then_protect", "_enforce_entry_safety")},
              "isolation": {"input_credentials_inherited": False, "real_environment_inherited": False,
                            "actual_exchange_clients_imported": False, "database": "memory_only"}}
    report["caller_identities"].update(getattr(runtime, "pyramid_caller_identities", {}))
    if "binance_signed_service._place_post_fill_protection" in runtime.caller_calls:
        report["caller_identities"]["binance_signed_service._place_post_fill_protection"] = {
            "source_file": no_obligation_protection.__code__.co_filename,
            "first_line": no_obligation_protection.__code__.co_firstlineno,
        }
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
    if config["PYRAMIDING_ENABLED"]:
        return await pyramid_with_ports(runtime, contract, signed, log)
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
        response = await signed["place_order"](**kwargs)
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
        # A execução ON exige as novas portas oficiais acima, ainda bloqueadas.
        return {"ok": True, "pyramid_applied": True,
                "executed_qty": persisted.qty - initial["qty"]}, {
                    "qty": persisted.qty, "entry_price": persisted.entry_price,
                    "pyramiding_level": persisted.pyramiding_level}
    return {"ok": False, "no_op": not runtime.gaps, "no_fill": not runtime.gaps}, None


async def pyramid_with_ports(runtime, contract, signed, log):
    """Real caller/P04 bodies, explicit simulated persistence boundaries.

    Each database/authority operation needs a causally bound receipt. This
    adapter does not pretend its local CAS is a PostgreSQL concurrency proof.
    No positive broker response is manufactured from the admitted quantity.
    """
    from dataclasses import asdict, dataclass
    from datetime import datetime, timezone
    from collections.abc import Mapping
    from types import MappingProxyType
    import math
    import re
    import sqlalchemy
    from sqlalchemy.orm import declarative_base

    payload, p, config = runtime.payload, runtime.payload["proposal"], runtime.payload["config"]
    state = runtime.port_state
    if state is None or "pyramid_limits" not in config:
        runtime.gap("PYRAMID_PORT_STATE_OR_LIMITS_MISSING")
        return {"ok": False}, None
    initial = payload["pyramid_state"]
    portfolio = payload["portfolio"]
    pending_rows = [r for r in state["intents"] if r["state"] in ("RESERVED", "SENDING", "UNKNOWN")]
    if (portfolio["open_risk_usd"] + 1e-9 < state["retained_risk_usd"]
            or portfolio["pending_risk_usd"] + 1e-9 < sum(r["risk_usd"] for r in pending_rows)
            or portfolio["pending_margin_usd"] + 1e-9 < sum(r["margin_usd"] for r in pending_rows)
            or portfolio["open_positions"] < len(state["portfolio_rows"])
            or portfolio["open_notional_usd"] + 1e-9 < sum(r["entry_price"] * r["qty"] for r in state["portfolio_rows"])):
        runtime.gap("PYRAMID_PORTFOLIO_MEMORY_NOT_CONSUMED")
        return {"ok": False}, None
    db = types.ModuleType("db")
    db.Base = declarative_base()
    sys.modules["db"] = db
    models = types.ModuleType("models")
    models.__path__ = []
    sys.modules["models"] = models
    model = load_source("models.real_trade", contract.BACKEND / "models/real_trade.py")
    snapshot_model = load_source("models.recommendation_snapshot", contract.BACKEND / "models/recommendation_snapshot.py")
    services = types.ModuleType("services")
    services.__path__ = []
    sys.modules["services"] = services

    def fake(name, **attrs):
        module = types.ModuleType("services." + name)
        module.__dict__.update(attrs)
        sys.modules[module.__name__] = module
        setattr(services, name, module)
        return module

    def seal():
        state["state_hash"] = contract.digest({k: v for k, v in state.items() if k != "state_hash"})

    async def authority(operation, details=None, *, mutation=False):
        before = state["state_hash"]
        value = await runtime.call("pyramid_authority", (operation,),
                                   {"state_hash": before, "details": details or {}})
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            runtime.gap("PYRAMID_AUTHORITY_RECEIPT_INVALID", operation=operation)
            raise Gap("invalid authority receipt")
        if value["ok"] is not True:
            runtime.gap(str(value.get("reason_code") or "PYRAMID_AUTHORITY_DENIED"), operation=operation)
            return False
        if state["blocked"]:
            runtime.gap("PYRAMID_AUTHORITY_BLOCKED", operation=operation)
            return False
        # A generation supplied as evidence must equal the current memory;
        # no receipt can advance or erase the state by fiat.
        if "generation" in value and (type(value["generation"]) is not int
                or value["generation"] < 0 or value["generation"] != state["generation"]):
            runtime.gap("PYRAMID_AUTHORITY_GENERATION_MISMATCH", operation=operation)
            return False
        return True

    async def owned(symbol, action):
        if (portfolio["ownership"] != "BOT" or state["blocked"]
                or not await authority("OWNERSHIP", {"symbol": symbol, "action": action})):
            return {"ok": False, "reason_code": "PYRAMID_OWNERSHIP_DENIED"}
        return None

    runtime.pyramid_ownership = owned

    def trade_object():
        values = {k: v for k, v in state["trade"].items() if k in model.RealTrade.__table__.columns}
        values["created_at"] = datetime.fromtimestamp(state["trade"]["created_at_ms"] / 1000, timezone.utc)
        return model.RealTrade(**values)

    class Session:
        def __init__(self):
            self.row, self.before = None, None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, statement):
            compiled = statement.compile()
            entities = [d.get("entity") for d in statement.column_descriptions]
            if entities == [snapshot_model.RecommendationSnapshot]:
                if (len(statement.column_descriptions) != 1
                        or statement.column_descriptions[0].get("name") != "timeframe"
                        or list(compiled.params.values()) != [state["trade"]["recommendation_id"]]):
                    raise Gap("unsupported original recommendation query")
                details = {"kind": "ORIGINAL_SNAPSHOT_TIMEFRAME", "parameters": compiled.params,
                           "locked": False, "state_hash": state["state_hash"]}
                value = await runtime.call("pyramid_read", (details,))
                if not isinstance(value, dict) or value.get("ok") is not True:
                    raise Gap("original recommendation not observed")
                observed = value.get("timeframe")
                if observed is not None and observed != state["trade"]["timeframe"]:
                    runtime.gap("PYRAMID_ORIGINAL_TIMEFRAME_MISMATCH")
                    raise Gap("original timeframe differs from declared memory")
                return types.SimpleNamespace(scalar_one_or_none=lambda: observed)
            if any(e is not model.RealTrade for e in entities):
                raise Gap("unsupported memory query")
            kind = "PORTFOLIO" if len(statement.column_descriptions) > 1 else "TRADE"
            details = {"kind": kind, "parameters": compiled.params,
                       "locked": getattr(statement, "_for_update_arg", None) is not None,
                       "state_hash": state["state_hash"]}
            value = await runtime.call("pyramid_read", (details,))
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise Gap("memory read not observed")
            if kind == "PORTFOLIO":
                return types.SimpleNamespace(mappings=lambda: types.SimpleNamespace(
                    all=lambda: copy.deepcopy(state["portfolio_rows"])))
            if list(compiled.params.values()) != [p["trade_id"]]:
                raise Gap("unexpected trade identity")
            self.row, self.before = trade_object(), state["state_hash"]
            return types.SimpleNamespace(scalar_one_or_none=lambda: self.row)

        async def commit(self):
            if self.row is None or self.before != state["state_hash"]:
                raise Gap("stale writer memory")
            changed = {"qty": self.row.qty, "pyramiding_level": self.row.pyramiding_level,
                       "entry_price": self.row.entry_price, "notes": self.row.notes}
            value = await runtime.call("pyramid_write", ("TRADE_UPDATE",),
                                       {"expected_state_hash": self.before, "changes": changed})
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise Gap("writer commit not observed")
            if state["state_hash"] != self.before:
                raise Gap("writer generation changed")
            before = copy.deepcopy(state["trade"])
            state["trade"].update(changed)
            for row in state["portfolio_rows"]:
                if row["id"] == p["trade_id"]:
                    row["qty"] = self.row.qty
            state["generation"] += 1
            seal()
            runtime.transitions.append({"producer": "shadow_trade_service._maybe_pyramid",
                                        "qty_before": before["qty"], "qty_after": self.row.qty,
                                        "entry_before": before["entry_price"], "entry_after": self.row.entry_price,
                                        "pyramiding_level": self.row.pyramiding_level})

    db.get_session = Session
    clock = types.SimpleNamespace(time=lambda: runtime.at_ms / 1000,
                                  time_ns=lambda: runtime.at_ms * 1000000)
    intent_names = {"EntryIdentity", "_label", "_digest", "_canonical_value", "canonical_proposal_hash",
                    "freeze_dispatch_proposal", "proposal_matches_stored", "identity_symbol_to_market",
                    "local_authority_still_valid"}
    fake("entry_intent_service")  # dataclass resolves its declared module while compiling
    in_ns = contract.source_functions("services/entry_intent_service.py", intent_names,
        {"dataclass": dataclass, "Mapping": Mapping, "MappingProxyType": MappingProxyType,
         "Dict": dict, "Any": object, "Tuple": tuple, "hashlib": hashlib, "json": json,
         "copy": copy, "re": re, "SCHEMA_VERSION": contract.source_literal("services/entry_intent_service.py", "SCHEMA_VERSION"),
         "MAX_CLIENT_ORDER_ID": 36,
         **{name: contract.source_literal("services/entry_intent_service.py", name)
            for name in ("PROPOSAL_FIELDS", "PROPOSAL_INVALID", "PROPOSAL_MISSING", "PROPOSAL_TAMPERED",
                         "LOCAL_AUTHORITY_INVALID", "LOCAL_AUTHORITY_PENDING", "LOCAL_AUTHORITY_IDENTITY",
                         "LOCAL_AUTHORITY_FENCE", "LOCAL_AUTHORITY_CONTAINED")}})

    async def mark_state(_factory, key, *, owner, reason=None, real_trade_id=None):
        row = next((r for r in state["intents"] if r["intent_key"] == key), None)
        operation = "CONFIRM" if real_trade_id is not None else "UNKNOWN" if reason and "UNKNOWN" in reason else "TERMINAL"
        if row is None or not await authority(operation, {"intent_key": key, "reason": reason,
                                                        "real_trade_id": real_trade_id}):
            return False
        target = "CONFIRMED" if real_trade_id is not None else "UNKNOWN" if operation == "UNKNOWN" else "TERMINAL"
        if row["state"] != target:
            row["state"] = target
            row["real_trade_id"] = real_trade_id
            if target == "CONFIRMED":
                state["retained_risk_usd"] += row["risk_usd"]
            state["generation"] += 1
            row["generation"] = state["generation"]
            seal()
        return True

    async def unknown(factory, key, **kwargs):
        return await mark_state(factory, key, **{**kwargs, "reason": "UNKNOWN"})

    intents_module = fake("entry_intent_service", **in_ns, mark_confirmed=mark_state,
                          mark_unknown=unknown, mark_terminal=mark_state)
    fake("binance_signed_service", to_binance=signed["to_binance"])
    fake("entry_revalidation_service", **{k: v for k, v in sys.modules["_event_entry_revalidation"].__dict__.items()
                                         if not k.startswith("__")})
    fake("manual_position_service", local_validation_fence=lambda: state["generation"],
         pending_validation_failure=lambda: "blocked" if state["blocked"] else None,
         local_symbol_block=lambda _: "blocked" if state["blocked"] else None,
         current_account_scope=lambda: p["account_ref"])

    async def paused():
        if not await authority("RISK_STATE"):
            return True
        return portfolio["paused"] or state["blocked"]

    async def kill():
        if not await authority("KILL_SWITCH"):
            return {"allowed": False}
        return {"allowed": portfolio["kill_allowed"]}

    async def mark(symbol):
        response = await runtime.call("quote", (symbol,))
        return response.get("last") if isinstance(response, dict) else None

    async def position(symbol, **kwargs):
        return await runtime.call("positions", (symbol,), kwargs)

    async def orders(symbol):
        return await runtime.call("algo_orders", (symbol,))

    async def equity(**kwargs):
        return await runtime.call("equity", (), kwargs)

    async def depth(symbol, **kwargs):
        return await runtime.call("depth", (symbol,), kwargs)

    async def place(**kwargs):
        expected = f"cw-pyr-{p['trade_id']}-{initial['pyramiding_level'] + 1}"
        if kwargs.get("client_order_id") != expected or kwargs.get("leverage") != p["leverage"]:
            raise Gap("pyramid dispatch/leverage mismatch")
        response = await signed["place_order"](**kwargs)
        runtime.pyramid_order_result = copy.deepcopy(response)
        return response

    async def notify(**kwargs):
        return None

    fake("exchange_service", place_order=place, get_positions=position, get_open_algo_orders=orders,
         get_equity=equity, get_execution_depth=depth)
    fake("risk_service", is_paused=paused)
    fake("kill_switch_service", check_can_trade=kill)
    fake("push_service", notify_alert=notify)
    fake("financial_risk_service", adverse_market_entry_price=sys.modules["_event_entry_revalidation"].__dict__.get(
        "adverse_market_entry_price"))
    financial = contract.source_functions("services/financial_risk_service.py", {"adverse_market_entry_price", "_unknown"},
        {"_finite": in_ns.get("_finite"), "QUALITY_OK": "OK", "QUALITY_UNKNOWN": "UNKNOWN"})
    financial["_finite"] = contract.source_functions("services/risk_reconciliation_service.py", {"_finite"})["_finite"]
    sys.modules["services.financial_risk_service"].adverse_market_entry_price = financial["adverse_market_entry_price"]

    ns = contract.source_functions("services/shadow_trade_service.py",
        {"_maybe_pyramid", "_pyramid_original_timeframe", "_pyramid_protection", "_pyramid_legacy_caps", "_pyramid_number", "_proposed_margin_usd",
         "_market_entry_preflight_checks", "_p04a_runtime_recheck", "_entry_intent_identity",
         "_wire_symbol", "_num_close", "_entry_proposal_fields", "_p04_evidence_of", "_intent_guarded_preflight",
         "_settle_entry_intent", "_sync_final_check_for", "_identidade_do_contexto_bate",
         "_proposal_age_limit_ms", "_revalidate_frozen_p04", "_intent_candidate_guard"},
        {**config, "SHADOW_ENABLED": False, "DB_ENABLED": True, "time": clock, "log": log,
         "_EXECUTION_QUARANTINE_REASON": "blocked" if state["blocked"] else None,
         "_INTENT_OWNER": "RESEARCH_ONLY_PORT_OWNER", "_PROPOSAL_NUM_TOL": 1e-9,
         "MARGIN_COST_BPS_PER_SIDE": contract.source_literal("services/shadow_trade_service.py", "MARGIN_COST_BPS_PER_SIDE"),
         "_CAMPOS_PROIBIDOS_NA_ENTRADA": contract.source_literal("services/shadow_trade_service.py", "_CAMPOS_PROIBIDOS_NA_ENTRADA"),
         "P04B_MAX_DEPTH_AGE_MS": config["limits"]["max_depth_age_ms"],
         "P04B_MAX_FETCH_LATENCY_MS": config["limits"]["max_fetch_latency_ms"],
         "P04B_MAX_BOOK_IMPACT_PCT": config["limits"]["max_book_impact_pct"],
         "P04B_MAX_MARKET_SLIPPAGE_PCT": config["limits"]["max_adverse_slippage_pct"],
         "P04B_DEPTH_LIMIT": config["pyramid_limits"]["depth_limit"],
         "P04B_DEPTH_TIMEOUT_S": config["pyramid_limits"]["depth_timeout_s"],
         "MAX_SPREAD_PCT": config["limits"]["max_spread_pct"],
         "P04A_MAX_CHASE_ATR": config["limits"]["max_chase_atr"],
         "PROXIMITY_MAX_ATR": config["limits"]["max_chase_atr"],
         "MIN_RR_TP1_FILL": config["limits"]["min_rr_tp1"],
         "MIN_RR_TP1_EXEC": config["limits"]["min_rr_tp1"],
         "MIN_RR_TP2_EXEC": config["limits"]["min_rr_tp2"],
         "RR_GATE_ENABLED": True, "FILL_RR_GATE_ENABLED": True,
         "MAX_TOTAL_MARGIN_PCT": config["pyramid_limits"]["max_total_margin_pct"],
         "_get_mark_price": mark,
         "_live_money_guard": lambda: (portfolio["live_guards_allowed"] and not state["blocked"], "bound memory"),
         "_live_execution_contract_guard": lambda: (portfolio["live_guards_allowed"] and not state["blocked"], "bound memory"),
         "_trade_active_exchange_guard": lambda trade: (trade.exchange == "binance", "bound exchange"),
         "_active_exchange_name": lambda: "binance", "_entry_account_ref": lambda: p["account_ref"]})

    def row_of(intent):
        return next((r for r in state["intents"] if r["intent_key"] == intent.get("intent_key")), None)

    async def reserve(rec, *, side, entry, stop, tp1, tp2, qty, equity_usd, identity, consumes_slot):
        details = {"identity": asdict(identity), "entry": entry, "stop": stop, "qty": qty,
                   "tp1": tp1, "tp2": tp2, "consumes_slot": consumes_slot}
        if not await authority("RESERVE", details):
            return {"granted": False, "reason": "PYRAMID_RESERVE_DENIED"}
        existing = next((r for r in state["intents"] if r["intent_key"] == identity.intent_key), None)
        if existing is not None:
            return {"granted": False, "reason": "PYRAMID_IDENTITY_ALREADY_CONSUMED"}
        final = contract.compute_admission(config, p, portfolio, final_qty=qty, final_entry=entry,
                                           consumes_slot=False, stop_override=stop)
        runtime.admissions.append(final)
        if final["decision"] != "ADMITTED":
            return {"granted": False, "reason": "PYRAMID_CAPACITY_DENIED"}
        margin = ns["_proposed_margin_usd"](entry=entry, qty=qty, leverage=p["leverage"])
        if margin is None or margin + portfolio["pending_margin_usd"] > portfolio["available_margin_usd"]:
            return {"granted": False, "reason": "INSUFFICIENT_FREE_MARGIN"}
        state["generation"] += 1
        row = {"intent_key": identity.intent_key, "identity": asdict(identity), "state": "RESERVED",
               "dispatch_ids": [], "proposal": None, "qty": qty, "risk_usd": final["risk_usd"],
               "margin_usd": margin, "lease_expires_at_ms": runtime.at_ms + 90000,
               "generation": state["generation"], "real_trade_id": None}
        state["intents"].append(row)
        seal()
        initial_reservation = {"producer": "shadow_trade_service._reserve_entry_intent",
            "request_hash": payload["request_hash"], "intent_key": row["intent_key"],
            "reserved_at_ms": runtime.at_ms, "generation": state["generation"],
            "state_hash": state["state_hash"], "qty": row["qty"],
            "risk_usd": row["risk_usd"], "margin_usd": row["margin_usd"],
            "slot_retained": False}
        runtime.initial_reservation = {**initial_reservation,
            "reservation_hash": contract.digest(initial_reservation)}
        return {"granted": True, "intent_key": identity.intent_key, "identity": identity,
                "account_ref": identity.account_ref, "state": "RESERVED", "margin_generation": state["generation"],
                "leverage": p["leverage"], "dispatched": False, "operational_selection": None}

    async def prepare(intent):
        row = row_of(intent)
        if (row is None or row["state"] not in ("RESERVED", "SENDING")
                or row["lease_expires_at_ms"] <= runtime.at_ms
                or not await authority("PREPARE", {"intent_key": row["intent_key"]})):
            return False
        row["state"] = intent["state"] = "SENDING"
        intent["dispatched"] = True
        seal()
        return True

    async def register(intent, dispatch_id):
        row = row_of(intent)
        if row is None or not await authority("REGISTER_DISPATCH", {"intent_key": row["intent_key"], "dispatch_id": dispatch_id}):
            return False
        if dispatch_id not in row["dispatch_ids"]:
            row["dispatch_ids"].append(dispatch_id)
            seal()
        return True

    async def financial_gate(*, side, final_entry, stop, final_qty, checks, intent):
        row = row_of(intent)
        if row is None or not await authority("READMIT", {"intent_key": row["intent_key"],
                "entry": final_entry, "stop": stop, "qty": final_qty}):
            return {"ok": False, "reason_code": "PYRAMID_READMISSION_DENIED", "checks": checks}
        if not await authority("FINANCIAL_TOTAL", {"intent_key": row["intent_key"], "risk_usd": abs(final_entry-stop)*final_qty}):
            return {"ok": False, "reason_code": "PYRAMID_FINANCIAL_TOTAL_UNAVAILABLE", "checks": checks}
        final = contract.compute_admission(config, p, portfolio, final_qty=final_qty, final_entry=final_entry,
                                           consumes_slot=False, stop_override=stop)
        runtime.admissions.append(final)
        if final["decision"] != "ADMITTED":
            return {"ok": False, "reason_code": "PYRAMID_CAPACITY_DENIED", "checks": checks}
        margin = ns["_proposed_margin_usd"](entry=final_entry, qty=final_qty, leverage=p["leverage"])
        if margin is None or margin + portfolio["pending_margin_usd"] > portfolio["available_margin_usd"]:
            return {"ok": False, "reason_code": "INSUFFICIENT_FREE_MARGIN", "checks": checks}
        fields = ns["_entry_proposal_fields"](intent, checks, side=side, final_entry=final_entry,
                                              final_qty=final_qty, stop=stop)
        if fields is None:
            return {"ok": False, "reason_code": "PYRAMID_PROPOSAL_MISSING", "checks": checks}
        state["generation"] += 1
        frozen = in_ns["freeze_dispatch_proposal"](**fields, token=state["generation"],
                created_at_ms=runtime.at_ms, lease_expires_at_ms=row["lease_expires_at_ms"])
        if frozen.get("ok") is not True:
            raise Gap("proposal could not freeze")
        row.update(qty=final_qty, risk_usd=final["risk_usd"], margin_usd=margin,
                   proposal=frozen, generation=state["generation"])
        intent["proposal"], intent["margin_generation"] = frozen, state["generation"]
        seal()
        return None

    def authorization(intent, *, dispatch_id_fn=None, **kwargs):
        async def check():
            row = row_of(intent)
            dispatch = dispatch_id_fn() if dispatch_id_fn else intent.get("last_dispatch_id")
            if (row is None or row["state"] != "SENDING" or row["lease_expires_at_ms"] <= runtime.at_ms
                    or row["generation"] != state["generation"] or intent["margin_generation"] != state["generation"]
                    or dispatch not in row["dispatch_ids"] or row["proposal"] is None
                    or not await authority("AUTHORIZE", {"intent_key": row["intent_key"], "dispatch_id": dispatch})):
                return {"ok": False, "reason_code": "PYRAMID_FINAL_AUTHORITY_DENIED"}
            token = {"fence": state["generation"], "pending": False, "intent_key": row["intent_key"],
                     "account_ref": p["account_ref"], "exchange": "binance", "symbol": row["identity"]["symbol"]}
            verdict = {"ok": True, "proposal": copy.deepcopy(row["proposal"]), "authorized_at_ms": runtime.at_ms,
                       "identity": intent["identity"], "local_authority": token}
            verdict["sync_check"] = ns["_sync_final_check_for"](verdict, intent)
            return verdict
        return check

    async def uncertain(intent, same_dir, reason):
        # Known local failure arms containment before persistence is attempted;
        # missing/denied receipt retains the previously reserved exposure.
        state["blocked"] = True
        ns["_EXECUTION_QUARANTINE_REASON"] = reason
        row = row_of(intent or {})
        if row:
            row["state"] = "UNKNOWN"
            intent["state"] = "UNKNOWN"
        seal()
        runtime.gap(reason)
        try:
            await runtime.call("pyramid_write", ("QUARANTINE",), {"state_hash": state["state_hash"], "reason": reason})
        except Gap:
            pass

    async def close_intent(intent, reason):
        await mark_state(Session, intent["intent_key"], owner="RESEARCH_ONLY_PORT_OWNER", reason="ENTRY_NOT_SUBMITTED")

    def arm(reason):
        state["blocked"] = True
        ns["_EXECUTION_QUARANTINE_REASON"] = str(reason)
        seal()
        runtime.gap("PYRAMID_QUARANTINE", reason=str(reason))

    ns.update(_reserve_entry_intent=reserve, _intent_dispatch_guard=prepare, _register_intent_dispatch=register,
              _r05b_entry_gate=financial_gate, _intent_final_authorization=authorization,
              _pyramid_uncertain=uncertain, _close_entry_intent=close_intent,
              _arm_execution_quarantine=arm)
    tracked = ("_maybe_pyramid", "_pyramid_original_timeframe", "_pyramid_protection",
               "_pyramid_legacy_caps", "_market_entry_preflight_checks",
               "_intent_guarded_preflight", "_sync_final_check_for")
    runtime.pyramid_caller_identities = {
        "shadow_trade_service." + name: {"source_file": ns[name].__code__.co_filename,
                                         "first_line": ns[name].__code__.co_firstlineno}
        for name in tracked}
    for name in tracked:
        actual = ns[name]
        if asyncio.iscoroutinefunction(actual):
            async def tracked_async(*args, _actual=actual, _name=name, **kwargs):
                runtime.caller_calls.append("shadow_trade_service." + _name)
                return await _actual(*args, **kwargs)
            ns[name] = tracked_async
        else:
            def tracked_sync(*args, _actual=actual, _name=name, **kwargs):
                runtime.caller_calls.append("shadow_trade_service." + _name)
                return _actual(*args, **kwargs)
            ns[name] = tracked_sync
    same_dir = trade_object()
    applied = await ns["_maybe_pyramid"](same_dir, {"symbol": p["symbol"], "entry": p["entry"],
        "stop_loss": p["stop"], "timeframe": p["timeframe"], "signal": {"indicators": {"atr": p["atr"]}},
        "entry_zone_low": p["entry_zone_low"], "entry_zone_high": p["entry_zone_high"]})
    if applied:
        return {"ok": True, "pyramid_applied": True,
                "executed_qty": state["trade"]["qty"] - initial["qty"]}, {
                    "qty": state["trade"]["qty"], "entry_price": state["trade"]["entry_price"],
                    "pyramiding_level": state["trade"]["pyramiding_level"]}
    unresolved = any(r["state"] in ("RESERVED", "SENDING", "UNKNOWN") for r in state["intents"])
    if unresolved:
        runtime.gap("PYRAMID_INTENT_UNRESOLVED")
    return {**(getattr(runtime, "pyramid_order_result", None) or {}),
            "ok": False, "no_op": not runtime.gaps, "no_fill": not runtime.gaps}, None


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
