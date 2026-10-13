"""Reforço LIVE: corpos reais, somente portas falsas e nenhuma autoridade real."""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import importlib.util
import logging
import math
from pathlib import Path
import socket
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).resolve().parents[1]
SHADOW = BACKEND / "services/shadow_trade_service.py"


def source_functions(names, namespace):
    """Não importa boot/env/clientes: compila os mesmos corpos e linhas do LIVE."""
    parsed = ast.parse(SHADOW.read_text(), filename=str(SHADOW))
    nodes = [n for n in parsed.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    missing = set(names) - {n.name for n in nodes}
    if missing:
        raise AssertionError(f"LIVE_FUNCTION_MISSING:{sorted(missing)}")
    tree = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *nodes], type_ignores=[]))
    exec(compile(tree, str(SHADOW), "exec"), namespace)
    return namespace


def pure_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, BACKEND / relative)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REVALIDATION = pure_module("pyramid_actual_p04", "services/entry_revalidation_service.py")


@dataclass(frozen=True)
class FixtureIdentity:
    account_ref: str = "TEST_ONLY_ACCOUNT"
    exchange: str = "binance"
    symbol: str = "BTC-USDT"
    quote: str = "USDT"
    side: str = "long"
    position_side: str = "BOTH"
    timeframe: str = "1h"
    playbook: str = "TEST_ONLY"
    playbook_version: str = "1"
    purpose: str = "ENTRY"
    trigger_candle_ms: int = 0


class Column:
    def __init__(self, name=None):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def is_(self, other):
        return self


class Query:
    def __init__(self):
        self.locked = False
        self.conditions = []

    def where(self, *args):
        self.conditions.extend(args)
        return self

    def with_for_update(self, *args, **kwargs):
        self.locked = True
        return self


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Session:
    def __init__(self, owner):
        self.owner = owner
        self.row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, query):
        if self.owner.db_error:
            raise RuntimeError("TEST_ONLY_DATABASE_UNAVAILABLE")
        self.owner.queries.append(query)
        self.row = copy.deepcopy(self.owner.persisted)
        return Result(self.row)

    async def commit(self):
        if self.owner.commit_error:
            raise RuntimeError("TEST_ONLY_COMMIT_UNAVAILABLE")
        if self.row is not None:
            self.owner.persisted = copy.deepcopy(self.row)
        self.owner.commits += 1

    async def rollback(self):
        return None


def module(name, **attrs):
    item = ModuleType(name)
    item.__dict__.update(attrs)
    return item


class CallerFixture:
    def __init__(self):
        self.trade = SimpleNamespace(
            id=11, status="open", source="auto", exchange="binance", symbol="BTC/USDT",
            side="long", phase="post_tp1", entry_price=100.0, planned_stop=95.0,
            qty_initial=2.0, qty=1.0, leverage=5, pyramiding_level=0, notes=None,
            created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            opened_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            sl_order_id="TEST_SL", tp2_order_id="TEST_TP2", sl_current_price=100.0,
            timeframe="1h", recommendation_id=7)
        self.persisted = copy.deepcopy(self.trade)
        self.rec = {"symbol": "BTC/USDT", "direction": "long", "timeframe": "1h",
                    "entry": 110.0, "stop_loss": 105.0, "tp1": 115.0, "tp2": 120.0,
                    "leverage": 19, "signal": {"indicators": {"atr": 5.0}}}
        self.order = {"ok": True, "entry_confirmed": True, "result": {"orderId": "TEST_PYRAMID_ORDER", "status": "FILLED",
                      "executedQty": "0.4", "avgPrice": "110"}, "executed_qty": .4,
                      "submitted_qty": 1.0, "final_qty": 1.0, "client_order_id": "TEST_PYRAMID_COID"}
        self.db_error = self.commit_error = False
        self.commits = 0
        self.queries = []
        self.calls = []
        self.posts = []
        self.after_post = None
        self.after_preflight = None
        self.final_authorizations = 0
        self.final_verdict = {"ok": True}
        self.reservations = []
        self.settlements = []
        self.quarantines = []
        self.preflight_verdict = {"ok": True, "quality": "FRESH", "approved_qty": 1.0,
                                  "checks": {"capped_qty": 1.0, "best_executable_price": 110.0,
                                             "vwap_price": 110.0, "worst_price": 110.0}}
        self.caps = AsyncMock(return_value=True)
        self.intent = {"granted": True, "dispatched": False, "intent_key": "TEST_INTENT",
                       "client_order_id": "TEST_PYRAMID_COID", "state": "RESERVED",
                       "identity": SimpleNamespace(symbol="BTC-USDT"), "margin_generation": 2,
                       "proposal": {"reference_price": 110.0, "qty": 1.0}}
        self.exchange = module("services.exchange_service", ACTIVE_EXCHANGE="binance",
            place_order=self.place_order, get_execution_depth=AsyncMock(),
            get_equity=AsyncMock(return_value={"ok": True, "source": "live", "total_usd": 1000}),
            get_position=AsyncMock(return_value={"ok": True, "size": 1, "side": "long"}))
        self.exchange.get_positions = AsyncMock(return_value={"ok": True, "positions": [
            {"symbol": "BTC/USDT", "side": "buy", "position_side": "BOTH", "size": 1, "leverage": 5}]})
        self.exchange.get_open_algo_orders = AsyncMock(return_value={"ok": True, "orders": [
            {"algo_id": "TEST_SL", "symbol": "BTCUSDT", "side": "sell", "type": "STOP_MARKET",
             "status": "NEW", "close_position": True, "trigger_price": 100},
            {"algo_id": "TEST_TP2", "symbol": "BTCUSDT", "side": "sell", "type": "TAKE_PROFIT_MARKET",
             "status": "NEW", "close_position": True, "trigger_price": 130}]})
        self.risk = module("services.risk_service", is_paused=AsyncMock(return_value=False),
                           set_manual_pause=AsyncMock())
        self.kill = module("services.kill_switch_service",
                           check_can_trade=AsyncMock(return_value={"allowed": True}))
        self.reconciliation = module("services.execution_reconciliation_service",
            record_incident=AsyncMock(return_value={"persisted": True}),
            assemble_entry_incident=lambda *args, **kw: kw)
        self.db = module("db", get_session=lambda: Session(self), DB_ENABLED=True)
        self.sql = module("sqlalchemy", select=lambda *args: Query(), or_=lambda *args: args)
        self.model = module("models.real_trade", RealTrade=type("RealTrade", (), {"id": Column()}))
        self.push = module("services.push_service", notify_alert=AsyncMock())
        self.entry_intents = module("services.entry_intent_service",
            mark_unknown=AsyncMock(), mark_confirmed=AsyncMock(), release_reserved=AsyncMock(),
            EntryIdentity=FixtureIdentity)
        self.ns = {"__name__": "TEST_ONLY_PYRAMID_REAL_SOURCE", "math": math, "time": time,
            "Decimal": Decimal, "InvalidOperation": InvalidOperation, "ROUND_FLOOR": ROUND_FLOOR,
            "datetime": datetime, "timezone": timezone, "log": logging.getLogger(__name__),
            "DB_ENABLED": True, "PYRAMIDING_ENABLED": True, "SHADOW_ENABLED": False,
            "P04B_MARKET_REVALIDATION_ENABLED": True,
            "PYRAMIDING_MAX_LEVEL": 1, "PYRAMIDING_MIN_PROFIT_R": 1,
            "PYRAMIDING_SIZE_MULT": .5, "_EXECUTION_QUARANTINE_REASON": None,
            "P04B_MAX_DEPTH_AGE_MS": 1500, "P04A_MAX_CHASE_ATR": 1.0,
            "_PROPOSAL_NUM_TOL": 1e-9,
            "_INTENT_OWNER": "TEST_ONLY_OWNER", "_live_money_guard": lambda: (True, None),
            "_live_execution_contract_guard": lambda: (True, None),
            "_trade_active_exchange_guard": lambda trade: (True, None),
            "_arm_execution_quarantine": self.arm_quarantine,
            "_persist_execution_quarantine": self.persist_quarantine,
            "_get_mark_price": AsyncMock(return_value=110.0),
            "_resolve_equity_usd": AsyncMock(return_value=(1000.0, "live")),
            "_reserve_entry_intent": self.reserve, "_settle_entry_intent": self.settle,
            "_close_entry_intent": self.close, "_record_skip": lambda *args: None,
            "_active_exchange_name": lambda: "binance",
            "_entry_account_ref": lambda: "TEST_ONLY_ACCOUNT",
            "_wire_symbol": lambda value: str(value).split(":")[0].replace("/", "").replace("-", ""),
            "_entry_intent_identity": lambda rec, side, purpose="ENTRY": FixtureIdentity(purpose=purpose),
            "_p04a_runtime_recheck": AsyncMock(return_value={"ok": True}),
            "_p04a_effective_chase_ceiling": lambda *args, **kw: 1.0,
            "_market_entry_preflight_checks": self.preflight,
            "_pyramid_legacy_caps": self.caps,
            "_candidate_guard_for_intent": lambda intent, **kw: self.candidate_guard,
            "_intent_candidate_guard": lambda intent, **kw: self.candidate_guard,
            "_candidate_live_guard": lambda *args, **kw: self.candidate_guard,
            "_intent_guarded_preflight": lambda intent, inner, **kw: inner,
            "_intent_final_authorization": lambda *args, **kw: self.final_authorization}

    def arm_quarantine(self, reason, **kw):
        self.quarantines.append(reason)

    async def persist_quarantine(self, reason, **kw):
        self.quarantines.append(reason)

    async def reserve(self, rec, **kwargs):
        self.reservations.append((copy.deepcopy(rec), kwargs))
        return self.intent

    async def settle(self, intent, order):
        self.settlements.append(copy.deepcopy(order))
        if order.get("ok") is not True:
            intent["state"] = "UNKNOWN"

    async def close(self, intent, trade, **kwargs):
        intent["state"] = "CONFIRMED" if trade is not None else "UNKNOWN"

    async def candidate_guard(self, *args, **kwargs):
        return {"ok": True}

    async def final_authorization(self):
        self.final_authorizations += 1
        return copy.deepcopy(self.final_verdict)

    async def preflight(self, *args, **kwargs):
        if self.after_preflight is not None:
            self.after_preflight()
        return copy.deepcopy(self.preflight_verdict)

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        callback = kwargs.get("entry_preflight")
        if callback is not None:
            verdict = await callback(kwargs["qty"], {"step": .001, "min_qty": .001,
                                   "max_qty": 1000, "min_notional": 5})
            if verdict.get("ok") is not True:
                return {**verdict, "entry_not_submitted": True, "ok": False, "no_fill": True}
        self.intent["dispatched"] = True
        final = kwargs.get("final_authorization")
        if final is not None:
            authorized = await final()
            if not isinstance(authorized, dict) or authorized.get("ok") is not True:
                return {"ok": False, "entry_not_submitted": True, "no_fill": True,
                        "reason_code": "TEST_FINAL_AUTHORITY_DENIED"}
        self.posts.append({key: value for key, value in kwargs.items() if not callable(value)})
        if self.after_post is not None:
            self.after_post()
        return copy.deepcopy(self.order)

    async def run(self):
        # A baseline não tinha estes helpers; os corpos novos são carregados
        # quando existem, sem alterar o callable LIVE que se está provando.
        names = {n.name for n in ast.parse(SHADOW.read_text()).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        optional = [n for n in ("_pyramid_number", "_pyramid_protection", "_pyramid_uncertain", "_num_close")
                    if n in names]
        source_functions(["_maybe_pyramid", "_entry_intent_identity", *optional], self.ns)
        services = module("services", exchange_service=self.exchange, risk_service=self.risk,
            kill_switch_service=self.kill, entry_intent_service=self.entry_intents,
            execution_reconciliation_service=self.reconciliation, push_service=self.push)
        patches = {"services": services, "services.exchange_service": self.exchange, "services.risk_service": self.risk,
                   "services.kill_switch_service": self.kill,
                   "services.execution_reconciliation_service": self.reconciliation,
                   "services.entry_intent_service": self.entry_intents,
                   "services.push_service": self.push, "models.real_trade": self.model,
                   "sqlalchemy": self.sql, "db": self.db}
        # Os mocks substituem apenas portas; nenhuma conexão ou mensagem real.
        with patch.dict(sys.modules, patches), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            return await self.ns["_maybe_pyramid"](self.trade, self.rec)


class ActualPyramidCallerTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_remains_noop(self):
        fixture = CallerFixture()
        fixture.ns["PYRAMIDING_ENABLED"] = False
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.reservations, [])

    async def test_market_preflight_disabled_blocks_before_any_submission(self):
        fixture = CallerFixture()
        fixture.ns["P04B_MARKET_REVALIDATION_ENABLED"] = False
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])
        self.assertEqual(fixture.reservations, [])

    async def test_database_off_does_not_send_or_fake_success(self):
        fixture = CallerFixture()
        fixture.ns["DB_ENABLED"] = False
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])

    async def test_actual_market_caller_has_preflight_and_final_authority(self):
        fixture = CallerFixture()
        self.assertTrue(await fixture.run())
        self.assertEqual(len(fixture.calls), 1)
        order = fixture.calls[0]
        self.assertTrue(callable(order.get("entry_preflight")))
        self.assertTrue(callable(order.get("final_authorization")))
        self.assertTrue(callable(order.get("candidate_guard")))
        self.assertEqual(order["leverage"], fixture.trade.leverage)
        self.assertEqual(order["order_type"], "Market")
        self.assertNotIn("stop_loss", order)
        self.assertNotIn("reduce_only", order)
        self.assertEqual(fixture.persisted.qty, 1.4)
        self.assertEqual(fixture.persisted.pyramiding_level, 1)
        self.assertEqual(fixture.persisted.pyramiding_level, 1)
        self.assertTrue(any(query.locked for query in fixture.queries))
        self.assertEqual(fixture.persisted.entry_price, 100.0)
        rec, reserve_args = fixture.reservations[0]
        self.assertFalse(reserve_args["consumes_slot"])
        self.assertEqual(reserve_args["identity"].purpose, "PYRAMID")
        self.assertEqual(reserve_args["identity"].trigger_candle_ms,
                         int(fixture.trade.created_at.timestamp() * 1000))
        self.assertEqual(reserve_args["identity"].playbook, "PYRAMID-11-1")
        self.assertEqual(rec["leverage"], 5)
        self.assertEqual(fixture.final_authorizations, 1)
        self.assertIsNone(reserve_args["tp1"])
        self.assertEqual(reserve_args["stop"], 100.0)
        self.assertEqual(reserve_args["tp2"], 130.0)

    async def test_protection_disappearing_after_preflight_prevents_post(self):
        fixture = CallerFixture()
        fixture.after_preflight = lambda: setattr(fixture.persisted, "sl_order_id", None)
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.final_authorizations, 0)

    async def test_pause_at_final_boundary_prevents_post(self):
        fixture = CallerFixture()
        fixture.ns["_p04a_runtime_recheck"].return_value = {"ok": False}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.final_authorizations, 0)

    async def test_final_intent_authority_denied_prevents_post(self):
        fixture = CallerFixture()
        fixture.final_verdict = {"ok": False}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.final_authorizations, 1)

    async def test_pyramid_identity_does_not_rebirth_from_recommendation_candle(self):
        first, second = CallerFixture(), CallerFixture()
        first.rec.update(timeframe="15m", playbook="FIRST", score_provenance={"formula_effective": "LEGACY"},
                         data_freshness={"candle": {"close_time_ms": 1000}})
        second.rec.update(timeframe="4h", playbook="NEW", score_provenance={"formula_effective": "SCORE_V3"},
                          data_freshness={"candle": {"close_time_ms": 9999999999999}})
        self.assertTrue(await first.run())
        self.assertTrue(await second.run())
        identity_a = first.reservations[0][1]["identity"]
        identity_b = second.reservations[0][1]["identity"]
        self.assertEqual(identity_a, identity_b)
        self.assertEqual(identity_a.timeframe, "1h")
        self.assertEqual(identity_a.playbook_version, "1")
        self.assertEqual(first.posts[0]["client_order_id"], second.posts[0]["client_order_id"])

    async def test_missing_executed_quantity_never_adds_planned_size(self):
        fixture = CallerFixture()
        fixture.order = {"ok": True, "result": {"orderId": "TEST_ORDER", "status": "FILLED", "avgPrice": 110}}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertTrue(fixture.quarantines)

    async def test_unknown_result_keeps_level_and_quarantine(self):
        fixture = CallerFixture()
        fixture.order = {"ok": False, "error": "timeout", "_request_sent": True,
                         "safety_state": "ENTRY_SUBMISSION_UNKNOWN", "quarantine_required": True}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertTrue(fixture.quarantines)
        self.assertNotEqual(fixture.intent["state"], "CONFIRMED")

    async def test_unknown_persistence_failure_still_arms_local_quarantine(self):
        fixture = CallerFixture()
        fixture.order = {"ok": False, "error": "timeout", "_request_sent": True,
                         "safety_state": "ENTRY_SUBMISSION_UNKNOWN", "quarantine_required": True}
        fixture.entry_intents.mark_unknown.side_effect = RuntimeError("TEST_ONLY_UNKNOWN_WRITE_FAILED")
        self.assertFalse(await fixture.run())
        self.assertTrue(fixture.quarantines)
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertNotEqual(fixture.intent["state"], "CONFIRMED")

    async def test_database_read_failure_never_sends(self):
        fixture = CallerFixture()
        fixture.db_error = True
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])

    async def test_manual_source_never_reinforced(self):
        fixture = CallerFixture()
        fixture.trade.source = "manual"
        fixture.persisted.source = "manual"
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])

    async def test_unknown_side_never_defaults_to_sell(self):
        fixture = CallerFixture()
        fixture.trade.side = "mystery"
        fixture.persisted.side = "mystery"
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.calls, [])

    async def test_repeated_same_level_does_not_double_economic_qty(self):
        fixture = CallerFixture()
        self.assertTrue(await fixture.run())
        self.assertFalse(await fixture.run())
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(fixture.persisted.qty, 1.4)

    async def test_portfolio_caps_block_before_reservation(self):
        fixture = CallerFixture()
        fixture.caps.return_value = False
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.reservations, [])

    async def test_portfolio_caps_rechecked_on_approved_depth_before_post(self):
        fixture = CallerFixture()
        fixture.caps.side_effect = [True, False]
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.caps.await_count, 2)
        self.assertEqual(fixture.caps.await_args.kwargs["entry"], 110.0)
        self.assertEqual(fixture.caps.await_args.kwargs["qty"], 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)

    async def test_preflight_rejection_never_updates_trade(self):
        fixture = CallerFixture()
        fixture.preflight_verdict = {"ok": False, "quality": "UNKNOWN",
                                     "reason_code": "EXEC_DEPTH_STALE", "checks": {}}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertNotEqual(fixture.intent["state"], "CONFIRMED")
        self.assertEqual(fixture.posts, [])

    async def test_missing_source_or_non_finite_fill_never_promotes_qty(self):
        for value in (None, True, float("nan"), float("inf"), -1, 0):
            with self.subTest(value=value):
                fixture = CallerFixture()
                fixture.order["result"]["executedQty"] = value
                fixture.order["executed_qty"] = value
                self.assertFalse(await fixture.run())
                self.assertEqual(fixture.persisted.qty, 1.0)
                self.assertEqual(fixture.persisted.pyramiding_level, 0)
                self.assertTrue(fixture.quarantines)

    async def test_commit_failure_retains_original_qty_and_unknown_authority(self):
        fixture = CallerFixture()
        fixture.commit_error = True
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertTrue(fixture.quarantines)
        self.assertNotEqual(fixture.intent["state"], "CONFIRMED")

    async def test_position_or_protection_unknown_blocks_before_post(self):
        cases = (
            ("position_stale", lambda f: f.exchange.get_positions.return_value.update(stale=True)),
            ("position_other_side", lambda f: f.exchange.get_positions.return_value["positions"][0].update(side="sell")),
            ("position_bool_qty", lambda f: f.exchange.get_positions.return_value["positions"][0].update(size=True)),
            ("position_unknown_qty", lambda f: f.exchange.get_positions.return_value["positions"][0].update(size=float("nan"))),
            ("sl_wrong_id", lambda f: f.exchange.get_open_algo_orders.return_value["orders"][0].update(algo_id="OTHER_SL")),
            ("sl_not_close_position", lambda f: f.exchange.get_open_algo_orders.return_value["orders"][0].update(close_position=False)),
            ("sl_unknown_status", lambda f: f.exchange.get_open_algo_orders.return_value["orders"][0].update(status="MYSTERY")),
            ("sl_below_be", lambda f: (setattr(f.persisted, "sl_current_price", 95),
                                     f.exchange.get_open_algo_orders.return_value["orders"][0].update(trigger_price=95))),
            ("tp2_wrong_side", lambda f: f.exchange.get_open_algo_orders.return_value["orders"][1].update(side="buy")),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                fixture = CallerFixture()
                mutate(fixture)
                self.assertFalse(await fixture.run())
                self.assertEqual(fixture.posts, [])
                self.assertEqual(fixture.reservations, [])

    async def test_protection_changed_during_wait_is_rechecked(self):
        fixture = CallerFixture()
        first = copy.deepcopy(fixture.exchange.get_open_algo_orders.return_value)
        second = copy.deepcopy(first)
        second["orders"][0]["algo_id"] = "REPLACED_SL"
        fixture.exchange.get_open_algo_orders.side_effect = [first, second]
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.persisted.qty, 1.0)

    async def test_trade_level_changed_after_fill_is_not_overwritten(self):
        fixture = CallerFixture()
        fixture.after_post = lambda: setattr(fixture.persisted, "pyramiding_level", 1)
        self.assertFalse(await fixture.run())
        self.assertEqual(len(fixture.posts), 1)
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 1)
        self.assertTrue(fixture.quarantines)
        self.assertNotEqual(fixture.intent["state"], "CONFIRMED")

    async def test_proved_fill_above_submitted_never_commits_qty(self):
        fixture = CallerFixture()
        fixture.order["result"]["executedQty"] = 2
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.persisted.qty, 1.0)
        self.assertEqual(fixture.persisted.pyramiding_level, 0)
        self.assertTrue(fixture.quarantines)

    async def test_fill_identity_and_terminal_evidence_are_required(self):
        cases = (
            ("missing_order_id", lambda f: f.order["result"].pop("orderId")),
            ("wrong_client_id", lambda f: f.order["result"].update(clientOrderId="OTHER_DISPATCH")),
            ("non_terminal", lambda f: f.order["result"].update(status="PARTIALLY_FILLED")),
            ("missing_average", lambda f: f.order["result"].pop("avgPrice")),
            ("unknown_average", lambda f: f.order["result"].update(avgPrice=float("nan"))),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                fixture = CallerFixture()
                mutate(fixture)
                self.assertFalse(await fixture.run())
                self.assertEqual(fixture.persisted.qty, 1.0)
                self.assertEqual(fixture.persisted.pyramiding_level, 0)
                self.assertTrue(fixture.quarantines)

    async def test_fill_beyond_admitted_price_or_missing_proposal_does_not_commit_qty(self):
        for label in ("beyond_price", "missing_proposal"):
            with self.subTest(label=label):
                fixture = CallerFixture()
                if label == "beyond_price":
                    fixture.order["result"]["avgPrice"] = 112
                else:
                    fixture.intent["proposal"] = None
                self.assertFalse(await fixture.run())
                self.assertEqual(fixture.persisted.qty, 1.0)
                self.assertEqual(fixture.persisted.pyramiding_level, 0)
                self.assertTrue(fixture.quarantines)

    async def test_candidate_shadow_context_does_not_authorize_reinforcement(self):
        fixture = CallerFixture()
        fixture.rec["operational_selection"] = {"scope": "CANDIDATE_SHADOW"}
        self.assertFalse(await fixture.run())
        self.assertEqual(fixture.posts, [])
        self.assertEqual(fixture.reservations, [])

    async def test_manual_on_other_symbol_is_not_managed_or_counted_as_bot_qty(self):
        fixture = CallerFixture()
        fixture.exchange.get_positions.return_value["positions"].append(
            {"symbol": "APE/USDT", "side": "buy", "position_side": "BOTH", "size": 500, "leverage": 2})
        self.assertTrue(await fixture.run())
        self.assertEqual(len(fixture.posts), 1)
        self.assertEqual(fixture.posts[0]["symbol"], "BTC/USDT")
        self.assertEqual(fixture.persisted.qty, 1.4)


class SharedMarketPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = int(time.time() * 1000)
        self.book = {"ok": True, "exchange": "binance", "symbol": "BTCUSDT",
                     "bids": [[109.99, 20]], "asks": [[110, 20]],
                     "received_at_ms": self.clock, "exchange_time_ms": self.clock,
                     "message_time_ms": self.clock, "latency_ms": 0, "last_update_id": 1}
        self.depth = AsyncMock(side_effect=lambda *args, **kw: copy.deepcopy(self.book))
        self.runtime = AsyncMock(return_value={"ok": True})
        self.financial = AsyncMock(return_value=None)
        self.ns = {"__name__": "TEST_ONLY_PYRAMID_SHARED_SOURCE", "math": math,
            "Decimal": Decimal, "InvalidOperation": InvalidOperation, "ROUND_FLOOR": ROUND_FLOOR,
            "P04B_MARKET_REVALIDATION_ENABLED": True, "P04B_DEPTH_LIMIT": 50,
            "P04B_DEPTH_TIMEOUT_S": 1.5, "P04B_MAX_DEPTH_AGE_MS": 1500,
            "P04B_MAX_FETCH_LATENCY_MS": 1500, "MAX_SPREAD_PCT": .25,
            "P04B_MAX_BOOK_IMPACT_PCT": .1, "P04B_MAX_MARKET_SLIPPAGE_PCT": .1,
            "MIN_RR_TP1_FILL": .7, "MIN_RR_TP1_EXEC": .7,
            "MIN_RR_TP2_EXEC": 1.5, "FILL_RR_GATE_ENABLED": True, "RR_GATE_ENABLED": True,
            "_p04a_runtime_recheck": self.runtime, "_r05b_entry_gate": self.financial}
        source_functions(["_market_entry_preflight_checks"], self.ns)
        self.rules = {"step": .001, "min_qty": .001, "max_qty": 1000, "min_notional": 5}
        self.args = {"rec": {"symbol": "BTC/USDT", "signal": {"indicators": {"atr": 5}}},
                     "side": "long", "entry": 110.0, "stop": 105.0,
                     "tp1": 115.0, "tp2": 120.0, "intent": {"intent_key": "TEST_ONLY"},
                     "final_qty": 1.0, "market_rules": self.rules,
                     "depth_fn": self.depth, "chase_ceiling": 1.0}
        self.frs = module("services.financial_risk_service", adverse_market_entry_price=
            lambda side, values: {"quality": "OK", "value": max(values) if side == "long" else min(values)})

    async def run_helper(self, **changes):
        args = {**self.args, **changes}
        services = module("services", risk_service=module("services.risk_service"),
            kill_switch_service=module("services.kill_switch_service"), financial_risk_service=self.frs)
        with patch.dict(sys.modules, {"services": services,
                "services.entry_revalidation_service": REVALIDATION,
                "services.financial_risk_service": self.frs}), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            return await self.ns["_market_entry_preflight_checks"](**args)

    async def test_market_disabled_blocks_instead_of_legacy_bypass(self):
        self.ns["P04B_MARKET_REVALIDATION_ENABLED"] = False
        verdict = await self.run_helper()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_MARKET_REVALIDATION_DISABLED")
        self.depth.assert_not_awaited()
        self.financial.assert_not_awaited()

    async def test_missing_depth_capability_blocks(self):
        verdict = await self.run_helper(depth_fn=None)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_DEPTH_UNSUPPORTED")
        self.financial.assert_not_awaited()

    async def test_depth_failure_never_treated_as_empty_healthy_book(self):
        self.depth.side_effect = RuntimeError("TEST_ONLY_DEPTH_UNAVAILABLE")
        verdict = await self.run_helper()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_DEPTH_FETCH_ERROR")
        self.financial.assert_not_awaited()

    async def test_runtime_pause_still_applies_after_depth_wait(self):
        self.runtime.return_value = {"ok": False, "reason_code": "EXEC_RISK_PAUSED"}
        verdict = await self.run_helper()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_RISK_PAUSED")
        self.financial.assert_not_awaited()

    async def test_stale_depth_is_rejected_by_actual_p04_evaluator(self):
        self.book["received_at_ms"] = self.clock - 2000
        self.book["exchange_time_ms"] = self.clock - 2000
        self.book["message_time_ms"] = self.clock - 2000
        verdict = await self.run_helper()
        self.assertFalse(verdict["ok"])
        self.assertIn("STALE", verdict["reason_code"])
        self.financial.assert_not_awaited()

    async def test_invalid_market_filters_never_make_qty(self):
        rules = {**self.rules, "step": float("nan")}
        verdict = await self.run_helper(market_rules=rules)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_MARKET_FILTERS_UNKNOWN")
        self.financial.assert_not_awaited()

    async def test_success_freezes_same_depth_and_reads_financial_gate(self):
        verdict = await self.run_helper()
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["approved_qty"], 1.0)
        checks = verdict["checks"]
        self.assertEqual(checks["financial_risk_entry_price"], 110.0)
        evidence = checks["p04_evidence"]
        self.assertEqual(evidence["args"]["depth"], self.book)
        self.assertEqual(evidence["args"]["qty"], verdict["approved_qty"])
        self.assertEqual(evidence["limits"]["max_depth_age_ms"], 1500)
        self.financial.assert_awaited_once()
        self.assertEqual(self.financial.await_args.kwargs["intent"], self.args["intent"])

    async def test_funding_or_financial_uncertainty_blocks_valid_book(self):
        for reason in ("R05D_TOTAL_FUNDING_UNKNOWN", "DAILY_LOSS_LIMIT", "FREE_MARGIN_UNKNOWN"):
            with self.subTest(reason=reason):
                self.financial.return_value = {"ok": False, "quality": "UNKNOWN", "reason_code": reason}
                verdict = await self.run_helper()
                self.assertFalse(verdict["ok"])
                self.assertEqual(verdict["reason_code"], reason)
                self.assertNotIn("approved_qty", verdict)


class PyramidLegacyCapsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = [{"id": 11, "entry_price": 100.0, "qty": 1.0, "leverage": 5}]
        self.queries = []
        self.database_error = False
        self.fixture = CallerFixture()
        self.ns = {"__name__": "TEST_ONLY_LEGACY_CAPS_SOURCE", "math": math,
                   "MARGIN_COST_BPS_PER_SIDE": 5.0, "MAX_MARGIN_PCT_PER_TRADE": 15,
                   "MAX_TOTAL_MARGIN_PCT": 0, "MAX_TOTAL_NOTIONAL_PCT": 150}
        source_functions(["_pyramid_number", "_proposed_margin_usd", "_pyramid_legacy_caps"], self.ns)

    async def run_caps(self, *, entry=110.0, qty=1.0, equity=1000.0):
        owner = self

        class MappingResult:
            def mappings(self):
                return self

            def all(self):
                return copy.deepcopy(owner.rows)

        class MappingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def execute(self, query):
                owner.queries.append(query)
                if owner.database_error:
                    raise RuntimeError("TEST_ONLY_CAPS_READ_FAILED")
                return MappingResult()

        model = type("RealTrade", (), {key: Column(key) for key in
                    ("id", "entry_price", "qty", "leverage", "status", "source")})
        with patch.dict(sys.modules, {"db": module("db", get_session=MappingSession),
                "models.real_trade": module("models.real_trade", RealTrade=model),
                "sqlalchemy": module("sqlalchemy", select=lambda *args: Query())}), \
                patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS_FORBIDDEN")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK_FORBIDDEN")):
            return await self.ns["_pyramid_legacy_caps"](
                self.fixture.trade, entry=entry, qty=qty, equity=equity)

    async def test_valid_registered_auto_exposure_is_selected_once(self):
        self.assertTrue(await self.run_caps())
        self.assertEqual(len(self.queries), 1)
        self.assertIn(("status", "eq", "open"), self.queries[0].conditions)
        self.assertIn(("source", "eq", "auto"), self.queries[0].conditions)

    async def test_combined_margin_notional_and_aggregate_margin_keep_existing_caps(self):
        for key, limit in (("MAX_MARGIN_PCT_PER_TRADE", 4.2),
                           ("MAX_TOTAL_NOTIONAL_PCT", 20), ("MAX_TOTAL_MARGIN_PCT", 4.2)):
            with self.subTest(cap=key):
                self.ns.update(MAX_MARGIN_PCT_PER_TRADE=15, MAX_TOTAL_NOTIONAL_PCT=150,
                               MAX_TOTAL_MARGIN_PCT=0)
                self.ns[key] = limit
                self.assertFalse(await self.run_caps())

    async def test_missing_target_or_unknown_other_exposure_is_not_zero(self):
        self.rows = []
        self.assertFalse(await self.run_caps())
        for value in (None, True, float("nan"), float("inf"), 0, -1):
            with self.subTest(value=value):
                self.rows = [{"id": 11, "entry_price": 100, "qty": 1, "leverage": 5},
                             {"id": 12, "entry_price": value, "qty": 1, "leverage": 5}]
                self.assertFalse(await self.run_caps())

    async def test_invalid_configured_caps_are_not_silently_disabled(self):
        for value in (None, True, float("nan"), float("inf"), -1, "150"):
            with self.subTest(value=value):
                self.ns["MAX_TOTAL_NOTIONAL_PCT"] = value
                self.assertFalse(await self.run_caps())

    async def test_database_error_never_returns_authorization(self):
        self.database_error = True
        with self.assertRaisesRegex(RuntimeError, "TEST_ONLY_CAPS_READ_FAILED"):
            await self.run_caps()


if __name__ == "__main__":
    unittest.main()
