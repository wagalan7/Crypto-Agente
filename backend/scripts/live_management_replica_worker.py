#!/usr/bin/env python3
"""Explicit-trace, process-isolated adapter for the real LIVE manager caller.

This executable has no scheduler, account credentials, database connection,
HTTP client, API route, or economic eligibility. Its ports return only supplied
observations. An accepted order response never changes an exchange position.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import time
import types
from datetime import datetime as RealDatetime, timezone


CONFIG_ATTRIBUTES = {
    "TIME_STOP_ENABLED": "TIME_STOP_ENABLED",
    "TIME_STOP_SCALP_MIN": "TIME_STOP_SCALP_MIN",
    "TIME_STOP_DAY_MIN": "TIME_STOP_DAY_MIN",
    "TIME_STOP_SWING_MIN": "TIME_STOP_SWING_MIN",
    "PROTECTION_AUTOHEAL_ENABLED": "PROTECTION_AUTOHEAL_ENABLED",
    "PROTECTION_VERIFY_LIVE": "PROTECTION_VERIFY_LIVE",
    "PROTECTION_HEAL_GRACE_SECONDS": "PROTECTION_HEAL_GRACE_SECONDS",
    "PROTECTION_TP1_QTY_PCT": "_TP1_QTY_PCT",
    "TRADE_MANAGER_DUST_USD": "DUST_NOTIONAL_USD",
    "BE_STRUCTURAL_ENABLED": "BE_STRUCTURAL_ENABLED",
    "BE_STRUCT_ATR_BUFFER": "BE_STRUCT_ATR_BUFFER",
    "BE_MAX_GIVEBACK_R": "BE_MAX_GIVEBACK_R",
    "PRE_TP1_PROTECT_ENABLED": "PRE_TP1_PROTECT_ENABLED",
    "PRE_TP1_PROTECT_TRIGGER_FRAC": "PRE_TP1_PROTECT_TRIGGER_FRAC",
    "PRE_TP1_PROTECT_LOCK_R": "PRE_TP1_PROTECT_LOCK_R",
    "RUNNER_ENABLED": "RUNNER_ENABLED",
    "RUNNER_QTY_PCT": "RUNNER_QTY_PCT",
    "RUNNER_DETECT_FRAC": "RUNNER_DETECT_FRAC",
    "RUNNER_ATR_MULT": "RUNNER_ATR_MULT",
    "RUNNER_LOOKBACK": "RUNNER_LOOKBACK",
    "RUNNER_ATR_TF_FALLBACK": "RUNNER_ATR_TF_FALLBACK",
    "RUNNER_MIN_STEP_ATR": "RUNNER_MIN_STEP_ATR",
}
READ_PORTS = {
    "ownership_guard", "get_positions", "get_open_algo_orders",
    "get_executions", "fetch_ticker", "fetch_ohlcv",
}
ACTION_PORTS = {
    "place_protection_orders", "cancel_algo_order", "place_order",
    "cleanup_orphan_conditionals", "close_trade",
}
PORTS = READ_PORTS | ACTION_PORTS
ECONOMIC_FIELDS = {
    "pnl_usd", "pnl_pct", "realized_r", "entry_fee", "exit_fee",
    "tp1_realized_usd", "execution_accounting",
}
ACTIVE_RUNTIME = None


class TraceGap(RuntimeError):
    pass


def json_safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            if ACTIVE_RUNTIME is not None:
                ACTIVE_RUNTIME.gap("serialization", "NON_FINITE_GENERATED_OUTPUT")
            raise ValueError("non-finite generated output")
        return value
    if isinstance(value, RealDatetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return type(value).__name__


class OfflineRuntime:
    def __init__(self, payload):
        self.payload = payload
        self.at_ms = 0
        self.event_id = None
        self.ports = {}
        self.offsets = {}
        self.sequence = 0
        self.call_index = 0
        self.last_action_index = 0
        self.last_action_available_ms = -1
        self.preview = payload.get("binding_mode") == "SYNTHETIC_PREVIEW"
        self.actions = []
        self.reads = []
        self.sessions = []
        self.snapshots = []
        self.gaps = []
        self.unused = []
        self.blocked_network = []
        self.caller_calls = []
        self.persisted = None
        self.trade_model = None
        self.timeframe = payload["initial_state"].get("timeframe")
        self.manager_file = None

    def event(self, port, args=(), kwargs=None, **extra):
        self.sequence += 1
        return {
            "sequence": self.sequence, "event_id": self.event_id,
            "at_ms": self.at_ms, "port": port,
            "args": json_safe(args), "kwargs": json_safe(kwargs or {}),
            **json_safe(extra),
        }

    def gap(self, port, reason, **extra):
        record = self.event(port, reason=reason, **extra)
        self.gaps.append(record)
        return record

    async def consume(self, port, *args, **kwargs):
        self.call_index += 1
        record = self.event(port, args, kwargs)
        body = {"port": port, "args": record["args"], "kwargs": record["kwargs"]}
        rh = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        record.update(call_index=self.call_index, expected_request_hash=rh,
                      expected_after_action_index=self.last_action_index)
        target = self.actions if port in ACTION_PORTS else self.reads
        target.append(record)
        index = self.offsets.get(port, 0)
        responses = self.ports.get(port, [])
        if index >= len(responses):
            record["missing_response"] = True
            self.gap(port, "MISSING_RESPONSE", response_index=index)
            raise TraceGap(f"missing explicit response: {port}")
        self.offsets[port] = index + 1
        envelope = responses[index]
        record["response_index"] = index
        if not isinstance(envelope, dict) or set(envelope) != {
            "observed_at_ms", "available_at_ms", "value",
            "call_index", "request_hash", "after_action_index",
        }:
            self.gap(port, "INVALID_RESPONSE_ENVELOPE", response_index=index)
            raise TraceGap("invalid response envelope")
        if not self.preview and (envelope["call_index"] != self.call_index
                or envelope["request_hash"] != rh
                or envelope["after_action_index"] != self.last_action_index):
            self.gap(port, "RECEIPT_CALL_BINDING_MISMATCH", response_index=index)
            raise TraceGap("response does not bind the exact request and causal sequence")
        observed = envelope["observed_at_ms"]
        available = envelope["available_at_ms"]
        if (isinstance(observed, bool) or isinstance(available, bool)
                or not isinstance(observed, int) or not isinstance(available, int)
                or observed > available or available > self.at_ms):
            self.gap(port, "NON_CAUSAL_RESPONSE", response_index=index)
            raise TraceGap("response is not available at this tick")
        if (port == "get_positions" and kwargs.get("force") is True
                and observed < self.last_action_available_ms):
            self.gap(port, "POSITION_OBSERVED_BEFORE_ACTION", response_index=index)
            raise TraceGap("fresh position must be observed after the preceding action")
        record.update(json_safe(envelope))
        record["call_index"] = self.call_index
        if port in ACTION_PORTS:
            self.last_action_index = self.call_index
            self.last_action_available_ms = available
        value = copy.deepcopy(envelope["value"])
        if port == "fetch_ohlcv" and value is not None:
            if not isinstance(value, dict) or set(value) != {"columns"}:
                self.gap(port, "INVALID_OHLCV_VALUE", response_index=index)
                raise TraceGap("OHLCV requires explicit columns")
            import pandas as pd
            try:
                return pd.DataFrame(value["columns"])
            except Exception:
                self.gap(port, "INVALID_OHLCV_COLUMNS", response_index=index)
                raise TraceGap("invalid OHLCV columns") from None
        return value

    def logical_action(self, name, *args, **kwargs):
        self.actions.append(self.event(name, args, kwargs, internal_only=True))

    def model_values(self, trade):
        return {
            col.key: copy.deepcopy(getattr(trade, col.key, None))
            for col in self.trade_model.__table__.columns
        }

    def clone(self, trade):
        return self.trade_model(**self.model_values(trade))

    def state(self, trade):
        values = self.model_values(trade)
        state = {}
        estimates = {}
        for key, value in values.items():
            if key in ECONOMIC_FIELDS:
                if value is not None:
                    estimates[key] = json_safe(value)
                continue
            if key.endswith("_at"):
                state[key + "_ms"] = (
                    round(value.timestamp() * 1000) if value is not None else None
                )
            else:
                state[key] = json_safe(value)
        state["timeframe"] = self.timeframe
        return state, estimates

    def begin_tick(self, tick):
        self.event_id = tick["event_id"]
        self.at_ms = tick["at_ms"]
        self.ports = tick.get("ports", {})
        self.offsets = {}

    def end_tick(self, caller):
        state, estimates = self.state(self.persisted)
        caller_state, caller_estimates = self.state(caller)
        self.snapshots.append({
            "event_id": self.event_id, "at_ms": self.at_ms,
            "state": state, "caller_state": caller_state,
            "unverified_live_accounting_estimates": estimates,
            "unverified_caller_accounting_estimates": caller_estimates,
        })
        for port, responses in self.ports.items():
            for index in range(self.offsets.get(port, 0), len(responses)):
                self.unused.append({
                    "event_id": self.event_id, "port": port,
                    "response_index": index, "response": json_safe(responses[index]),
                })

    def profile(self, frame, event, arg):
        if event == "call" and frame.f_code.co_filename == self.manager_file:
            self.caller_calls.append({
                "event_id": self.event_id, "at_ms": self.at_ms,
                "function": frame.f_code.co_name,
                "source_file": self.manager_file,
                "first_line": frame.f_code.co_firstlineno,
            })


def install_network_guard(runtime):
    original_socket = socket.socket

    def deny(operation, *args, **kwargs):
        runtime.blocked_network.append(runtime.event(
            operation, args, kwargs, blocked=True,
        ))
        runtime.gap(operation, "NETWORK_ATTEMPT_BLOCKED")
        raise PermissionError("offline replica prohibits TCP, UDP and DNS")

    class OfflineSocket(original_socket):
        def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM,
                     proto=0, fileno=None):
            if family != socket.AF_UNIX:
                deny("socket", family, type, proto)
            super().__init__(family, type, proto, fileno)

        def connect(self, address):
            if self.family != socket.AF_UNIX:
                deny("connect", address)
            return super().connect(address)

        def connect_ex(self, address):
            if self.family != socket.AF_UNIX:
                deny("connect_ex", address)
            return super().connect_ex(address)

    socket.socket = OfflineSocket
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr", "create_connection"):
        setattr(socket, name, lambda *a, _name=name, **k: deny(_name, *a, **k))


class SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Compile the supplied source bytes; never read or write a bytecode cache."""

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)


class OfflineImportGuard(importlib.abc.MetaPathFinder):
    ALLOWED_SERVICES = {
        "services.trade_manager_service", "services.indicator_service",
        "services.entry_planner",
    }
    ALLOWED_MODELS = {
        "models.real_trade", "models.trade_signal", "models.recommendation_snapshot",
    }

    def __init__(self, runtime, backend):
        self.runtime = runtime
        self.backend = Path(backend).resolve()

    def find_spec(self, fullname, path=None, target=None):
        if (fullname.startswith("services.") and fullname not in self.ALLOWED_SERVICES
                or fullname.startswith("models.") and fullname not in self.ALLOWED_MODELS
                or fullname.split(".")[0] in {"httpx", "requests", "aiohttp", "asyncpg"}):
            self.runtime.gap("import", "OFFLINE_IMPORT_BLOCKED", module=fullname)
            raise ImportError(f"offline dependency not admitted: {fullname}")
        if fullname in self.ALLOWED_SERVICES | self.ALLOWED_MODELS:
            source_path = self.backend.joinpath(*fullname.split(".")).with_suffix(".py")
            return importlib.util.spec_from_file_location(
                fullname, source_path, loader=SourceOnlyLoader(fullname, str(source_path)),
            )
        return None


def memory_select_kind(statement, trade_model, snapshot_model, trade_id,
                       recommendation_id):
    """Admit only the two exact, row-bound SELECT shapes used by the caller."""
    from sqlalchemy import select
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter
    from sqlalchemy.sql.selectable import Select

    if type(statement) is not Select or statement.get_execution_options():
        raise TraceGap("unsupported memory query")
    descriptions = statement.column_descriptions
    if len(descriptions) != 1 or descriptions[0].get("aliased"):
        raise TraceGap("unsupported memory projection")
    description = descriptions[0]
    if (description.get("entity") is trade_model
            and description.get("expr") is trade_model):
        kind, row_id = "trade", trade_id
        expected = select(trade_model).where(trade_model.id == row_id)
    elif (description.get("entity") is snapshot_model
          and description.get("expr") is snapshot_model.timeframe):
        kind, row_id = "timeframe", recommendation_id
        expected = select(snapshot_model.timeframe).where(snapshot_model.id == row_id)
    else:
        raise TraceGap("unsupported memory projection")
    condition = statement.whereclause
    if (not isinstance(condition, BinaryExpression)
            or type(condition.right) is not BindParameter
            or type(condition.right.value) is not int
            or type(row_id) is not int or condition.right.value != row_id
            or not statement.compare(expected, compare_values=True,
                                     compare_annotations=True)):
        raise TraceGap("unsupported memory filter or query shape")
    return kind


def fake_module(name, parent=None, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    if parent is not None:
        setattr(parent, name.rsplit(".", 1)[-1], module)
    return module


def install_memory_dependencies(runtime, backend):
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    class Result:
        def __init__(self, value, timeframe=False):
            self.value = value
            self.timeframe = timeframe

        def scalar_one_or_none(self):
            return self.value

        def first(self):
            return (self.value,) if self.value is not None else None

        def scalars(self):
            return self

        def all(self):
            return [] if self.value is None else [self.value]

    class MemorySession:
        def __init__(self):
            self.fresh = None
            self.committed = False

        async def __aenter__(self):
            self.committed = False
            runtime.sessions.append(runtime.event("session_enter", memory_only=True))
            return self

        async def __aexit__(self, exc_type, exc, tb):
            runtime.sessions.append(runtime.event(
                "session_exit", committed=self.committed,
                exception=exc_type.__name__ if exc_type else None,
            ))

        async def execute(self, statement):
            try:
                from models.recommendation_snapshot import RecommendationSnapshot
                query = memory_select_kind(
                    statement, runtime.trade_model, RecommendationSnapshot,
                    runtime.payload["initial_state"]["id"],
                    getattr(runtime.persisted, "recommendation_id", None),
                )
            except Exception:
                runtime.gap("session_select", "UNSUPPORTED_MEMORY_QUERY",
                            statement_type=type(statement).__name__)
                raise TraceGap("memory query is outside the explicit fixture") from None
            runtime.sessions.append(runtime.event(
                "session_select", query=query, memory_only=True,
            ))
            if query == "timeframe":
                return Result(runtime.timeframe, timeframe=True)
            if runtime.persisted is None:
                return Result(None)
            self.fresh = runtime.clone(runtime.persisted)
            return Result(self.fresh)

        async def commit(self):
            if self.fresh is not None:
                runtime.persisted = runtime.clone(self.fresh)
            runtime.sessions.append(runtime.event("session_commit", memory_only=True))
            self.committed = True

    fake_module("db", Base=Base, DB_ENABLED=True, get_session=MemorySession)
    services = fake_module("services", __path__=[str(backend / "services")])
    fake_module("models", __path__=[str(backend / "models")])

    def port(name):
        async def call(*args, **kwargs):
            return await runtime.consume(name, *args, **kwargs)
        return call

    exchange = fake_module("services.exchange_service", services,
                           ACTIVE_EXCHANGE="binance")
    for name in ("get_positions", "get_open_algo_orders", "place_protection_orders",
                 "cancel_algo_order", "place_order", "get_executions"):
        setattr(exchange, name, port(name))
    fake_module("services.binance_signed_service", services,
                place_protection_orders=port("place_protection_orders"),
                _cleanup_entry_conditionals=port("cleanup_orphan_conditionals"))
    fake_module("services.binance_service", services,
                fetch_ticker=port("fetch_ticker"), fetch_ohlcv=port("fetch_ohlcv"))
    fake_module("services.manual_position_service", services,
                ownership_guard=port("ownership_guard"))

    async def close_trade(trade_id, **kwargs):
        receipt = await runtime.consume("close_trade", trade_id, **kwargs)
        if not isinstance(receipt, dict) or receipt.get("applied") is not True:
            return None
        runtime.logical_action("memory_close_trade", trade_id, **kwargs)
        runtime.persisted.status = kwargs.get("status")
        runtime.persisted.exit_price = kwargs.get("exit_price")
        runtime.persisted.closed_at = RealDatetime.fromtimestamp(runtime.at_ms / 1000, timezone.utc)
        runtime.persisted.updated_at = runtime.persisted.closed_at
        # The caller's exit price is a LIVE heuristic, not a verified fill/PnL.
        if kwargs.get("notes") is not None:
            runtime.persisted.notes = kwargs["notes"]
        for field in ("pnl_usd", "pnl_pct", "realized_r", "execution_accounting"):
            setattr(runtime.persisted, field, None)
        return None

    async def get_trade(trade_id):
        runtime.sessions.append(runtime.event("memory_get_trade", (trade_id,), memory_only=True))
        state, _ = runtime.state(runtime.persisted)
        return {**state, "pnl_usd": None, "pnl_pct": None, "realized_r": None}

    async def recompute_entry_slippage(*args, **kwargs):
        runtime.logical_action("slippage_recompute_suppressed", *args, **kwargs)
        return None

    fake_module("services.real_trade_service", services,
                close_trade=close_trade, get_trade=get_trade,
                recompute_entry_slippage=recompute_entry_slippage)

    async def notification(*args, **kwargs):
        runtime.logical_action("notification_suppressed", *args, **kwargs)
        return None

    def formatter(*args, **kwargs):
        return "offline_notification"

    fake_module("services.notification_service", services, send_telegram=notification,
                fmt_time_stop=formatter, fmt_tp1_hit=formatter,
                fmt_trade_closed=formatter)

    async def pause(*args, **kwargs):
        runtime.logical_action("manual_pause_intent_only", *args, **kwargs)
        return None

    async def is_paused():
        runtime.gap("risk_is_paused", "UNSUPPORTED_EXTERNAL_READ")
        raise TraceGap("pause-state evidence not part of the explicit trace contract")

    fake_module("services.risk_service", services, set_manual_pause=pause,
                is_paused=is_paused)
    fake_module("services.shadow_trade_service", services,
                _arm_execution_quarantine=lambda *a, **k:
                    runtime.logical_action("execution_quarantine_intent_only", *a, **k))
    sys.meta_path.insert(0, OfflineImportGuard(runtime, backend))


def validate_minimum(payload):
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    config = payload.get("config")
    if not isinstance(config, dict) or set(config) != set(CONFIG_ATTRIBUTES):
        raise ValueError("config must contain exactly the canonical explicit manager settings")
    if not isinstance(payload.get("initial_state"), dict) or not isinstance(payload.get("ticks"), list):
        raise ValueError("initial_state and ticks are required")
    for tick in payload["ticks"]:
        if not isinstance(tick, dict) or "event_id" not in tick or "at_ms" not in tick:
            raise ValueError("each tick needs event_id and at_ms")
        if not isinstance(tick.get("ports", {}), dict) or set(tick.get("ports", {})) - PORTS:
            raise ValueError("unknown trace port")
        if any(not isinstance(responses, list) for responses in tick.get("ports", {}).values()):
            raise ValueError("port responses must be explicit lists")


async def run(payload, backend):
    global ACTIVE_RUNTIME
    validate_minimum(payload)
    runtime = OfflineRuntime(payload)
    ACTIVE_RUNTIME = runtime
    install_network_guard(runtime)
    # Independently bind the closed input contract to the current source files
    # in the child, before importing any LIVE caller or mapped model.
    contract_path = backend / "services" / "live_management_replica_service.py"
    contract_spec = importlib.util.spec_from_file_location(
        "_offline_replica_contract", contract_path,
        loader=SourceOnlyLoader("_offline_replica_contract", str(contract_path)),
    )
    if contract_spec is None or contract_spec.loader is None:
        raise ValueError("offline input contract unavailable")
    contract = importlib.util.module_from_spec(contract_spec)
    contract_spec.loader.exec_module(contract)
    payload = contract.validate_request(
        payload, expected_source_fingerprint=contract.source_fingerprint(),
    )
    runtime.payload = payload
    if runtime.preview:
        runtime.gap("trace", "UNBOUND_SYNTHETIC_PREVIEW_NOT_EVIDENCE")
    install_memory_dependencies(runtime, backend)
    # No process environment is consulted, including while the real module loads.
    original_getenv = os.getenv

    def explicit_getenv(name, default=None):
        if name in payload["config"]:
            value = payload["config"][name]
            return str(value).lower() if isinstance(value, bool) else str(value)
        return default

    os.getenv = explicit_getenv
    try:
        manager = importlib.import_module("services.trade_manager_service")
        model = importlib.import_module("models.real_trade").RealTrade
        # These are the actual production implementations, not copied formulas.
        importlib.import_module("services.indicator_service")
        importlib.import_module("services.entry_planner")
    finally:
        os.getenv = original_getenv
    if contract.source_fingerprint() != payload["source_fingerprint"]:
        raise ValueError("source changed while importing the LIVE caller")
    runtime.trade_model = model
    runtime.manager_file = manager.__file__
    for setting, attribute in CONFIG_ATTRIBUTES.items():
        setattr(manager, attribute, payload["config"][setting])

    initial = copy.deepcopy(payload["initial_state"])
    initial.pop("timeframe", None)
    for field in ("opened_at", "created_at", "updated_at", "closed_at"):
        millis = initial.pop(field + "_ms", None)
        initial[field] = RealDatetime.fromtimestamp(millis / 1000, timezone.utc) if millis is not None else None
    # A declared initial timeframe is the local recommendation-snapshot fixture.
    # This identifier is adapter-local; it never denotes a row in an actual DB.
    if runtime.timeframe is not None and not initial.get("recommendation_id"):
        initial["recommendation_id"] = initial["id"]
    mapped_keys = {col.key for col in model.__table__.columns}
    runtime.persisted = model(**{k: v for k, v in initial.items() if k in mapped_keys})
    for field in ECONOMIC_FIELDS:
        setattr(runtime.persisted, field, None)

    class FrozenDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            value = RealDatetime.fromtimestamp(runtime.at_ms / 1000, timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return cls.now(timezone.utc).replace(tzinfo=None)

    manager.datetime = FrozenDatetime
    original_time = time.time
    original_profile = sys.getprofile()
    time.time = lambda: runtime.at_ms / 1000
    try:
        for tick in payload["ticks"]:
            runtime.begin_tick(tick)
            caller = runtime.clone(runtime.persisted)
            sys.setprofile(runtime.profile)
            try:
                if caller.status == "open":
                    await manager._process_trade(caller)
                else:
                    runtime.sessions.append(runtime.event("closed_trade_not_dispatched", memory_only=True))
            except Exception as exc:
                runtime.gap("_process_trade", "CALLER_EXCEPTION", exception_type=type(exc).__name__)
            finally:
                sys.setprofile(original_profile)
                runtime.end_tick(caller)
    finally:
        sys.setprofile(original_profile)
        time.time = original_time

    if not payload["ticks"]:
        runtime.gap("trace", "EMPTY_TRACE")
    if contract.source_fingerprint() != payload["source_fingerprint"]:
        runtime.gap("trace", "SOURCE_DRIFT_DURING_EXECUTION")
    identities = {}
    for name in ("_process_trade", "_fetch_exchange_position", "_manual_symbol_blocked",
                 "_check_time_stop", "_ensure_protection", "_maybe_pre_tp1_protect",
                 "_transition_to_post_tp1", "_structural_be_stop", "_transition_to_runner",
                 "_maybe_trail_runner", "_chandelier_stop", "_runner_should_move", "_close_trade"):
        function = getattr(manager, name)
        identities[name] = {"module": function.__module__, "name": function.__qualname__,
                            "source_file": function.__code__.co_filename,
                            "first_line": function.__code__.co_firstlineno}
    return {
        "engine_version": payload["engine_version"],
        "trace_hash": payload["trace_hash"],
        "source_fingerprint": payload["source_fingerprint"],
        "binding_mode": payload["binding_mode"],
        "runtime_identity": contract.runtime_identity(),
        "status": "INCOMPLETE_TRACE" if runtime.gaps else "COMPLETE_TRACE",
        "actions": runtime.actions, "reads": runtime.reads,
        "sessions": runtime.sessions, "snapshots": runtime.snapshots,
        "gaps": runtime.gaps, "unused_responses": runtime.unused,
        "blocked_network_attempts": runtime.blocked_network,
        "caller_identities": identities, "caller_calls": runtime.caller_calls,
        "source_sha256": hashlib.sha256(Path(manager.__file__).read_bytes()).hexdigest(),
        "effective_config": payload["config"],
        "economics": None, "live_equivalent": False, "promotion": False,
        "authorizes_live": False, "promotable": False,
        "isolation": {
            "mode": "isolated_subprocess_explicit_trace", "database": "memory_only",
            "input_credentials_inherited": False, "real_environment_inherited": False,
            "actual_exchange_clients_imported": False,
        },
    }


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sys.dont_write_bytecode = True
    try:
        payload = json.load(sys.stdin)
        backend = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else Path(__file__).resolve().parents[1]
        if not (backend / "services" / "trade_manager_service.py").is_file():
            raise ValueError("backend source root is unavailable")
        report = asyncio.run(run(payload, backend))
    except BaseException as exc:
        runtime = ACTIVE_RUNTIME
        report = {
            "status": "WORKER_ERROR", "error_type": type(exc).__name__,
            "error": "offline worker failed; no external action was authorized",
            "actions": runtime.actions if runtime else [],
            "reads": runtime.reads if runtime else [],
            "sessions": runtime.sessions if runtime else [],
            "snapshots": runtime.snapshots if runtime else [],
            "gaps": (runtime.gaps if runtime else []) + [
                {"reason": "WORKER_ERROR", "exception_type": type(exc).__name__}],
            "unused_responses": runtime.unused if runtime else [],
            "blocked_network_attempts": runtime.blocked_network if runtime else [],
            "economics": None, "live_equivalent": False, "promotion": False,
            "authorizes_live": False, "promotable": False,
        }
    sys.stdout.write(json.dumps(json_safe(report), ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
