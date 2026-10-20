"""Causal chain research tests; fixtures never authorize or access LIVE.

Pure tests prove boundaries of the orchestrator. Native-call positive chains
below additionally run the isolated source-bound execution/management callers.
"""
import copy
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_event_study_service as study
from services import live_event_execution_service as execution
from tests.test_live_event_execution import portfolio, proposal
from tests.test_live_management_replica import state
from tests.test_live_management_replica import config as manager_config, tick, request as bound_manager
from tests.test_live_event_study import fixture as entry_fixture, ENTRY, EXIT, NOW
from tests.test_live_event_pyramid_ports import complete_pyramid_request

_NATIVE_CACHE = {}
_PYRAMID_CACHE = {}


def native_pyramid_chain_fixture(side="long"):
    """Source-bound entry/manager/pyramid/manager, never an account observation.

    The same frozen configuration governs both selection arms. All foreign
    key and broker facts are explicit TEST_ONLY values; the receipt-bound
    persistence port is not claimed to be a live PostgreSQL writer.
    """
    identity = (study.engine_fingerprint(), side)
    if identity in _PYRAMID_CACHE:
        return copy.deepcopy(_PYRAMID_CACHE[identity])
    from tests.test_live_event_execution import complete_entry_request, config as entry_config
    manifest, data = entry_fixture()
    record = copy.deepcopy(data["events"]["baseline"][0])
    # The LIVE futures identity includes settlement; a spot-shaped name does
    # not prove BASE-QUOTE-SETTLE at the synchronous signature boundary.
    symbol = "BTC/USDT:USDT"
    entry = complete_entry_request(side=side, symbol=symbol)
    cfg_exec = entry_config(pyramid=True)
    cfg_exec["pyramid_limits"] = {"max_total_margin_pct": 70, "depth_limit": 20,
                                 "depth_timeout_s": 2}
    entry = execution.make_execution_request(config=cfg_exec, proposal=entry["proposal"],
        portfolio=entry["portfolio"], receipts=entry["receipts"], fills=entry["fills"])
    cfg = manager_config(TIME_STOP_ENABLED=True, TIME_STOP_DAY_MIN=1,
                         BE_STRUCTURAL_ENABLED=False, PRE_TP1_PROTECT_ENABLED=False)
    descriptor = {"engine_version": study.manager.ENGINE_VERSION,
        "source_fingerprint": study.manager.source_fingerprint(), "config": cfg,
        "config_hash": study.digest(cfg)}
    manifest = {k: copy.deepcopy(v) for k, v in manifest.items() if k != "manifest_hash"}
    manifest["hashes"] = None
    manifest["shared"].update(management=descriptor, execution_config=cfg_exec)
    for arm in ("baseline", "candidate"):
        manifest[arm]["management_config"] = copy.deepcopy(descriptor)
    manifest = study.parse_manifest(manifest)
    prop = entry["proposal"]
    at, pyramid_at = ENTRY + 10000, ENTRY + 20000
    initial = state(id=1, symbol="BTCUSDT", side=side, qty=2., qty_initial=2.,
        entry_price=100., opened_at_ms=ENTRY, created_at_ms=ENTRY,
        planned_stop=prop["stop"], planned_tp1=prop["tp1"], planned_tp2=prop["tp2"],
        sl_current_price=prop["stop"], sl_order_id="sl-1", tp1_order_id="tp1-1",
        tp2_order_id="tp2-1", client_order_id="cw-test-entry-1", timeframe="1h")
    pos = {"ok": True, "positions": [{"symbol": "BTCUSDT", "side": "Buy" if side == "long" else "Sell",
        "size": 1.1, "entry_price": 100., "position_side": "BOTH"}]}
    first = bound_manager(cfg=cfg, initial=initial, ticks=[tick("pyramid-tp1", at_ms=at,
        ownership_guard=[{"allowed": True}], get_positions=[pos, pos],
        place_protection_orders=[{"sl_ok": True, "sl_order_id": "SL-BE"}],
        cancel_algo_order=[{"ok": True}], get_executions=[{"ok": True, "fills": []}])])
    first_report = study.manager.run_trace(first)
    if first_report["status"] != "COMPLETE_TRACE":
        raise AssertionError(first_report["gaps"])
    continued = study._manager_projection(first_report["snapshots"][-1]["state"])
    book = study._CausalPortfolio(manifest["shared"]["initial_portfolio"])
    entry_report = execution.replay_execution(entry)
    book.execution("opp-1", entry, entry_report)
    book.management("opp-1", first_report)
    balance = book.at(pyramid_at)
    current = {k: continued[k] for k in ("id", "symbol", "side", "qty", "qty_initial",
               "entry_price", "planned_stop", "phase", "notes")}
    # CCXT-normalized name at the execution port; persisted manager wire name
    # is compared by the exact base/quote contract, never by base alone.
    current["symbol"] = prop["symbol"]
    current.update(leverage=5, pyramiding_level=0, sl_close_position=True,
                   tp2_close_position=True, protection_confirmed=True)
    metadata = {k: continued[k] for k in ("source", "status", "exchange", "timeframe",
                "created_at_ms", "sl_current_price", "sl_order_id", "tp2_order_id")}
    metadata["recommendation_id"] = 1001
    pyr_prop = {**prop, "entry": 110 if side == "long" else 90, "stop": 100,
        "tp1": 115 if side == "long" else 85, "decision_at_ms": pyramid_at,
        "dispatch_id": "cw-pyr-1-1"}
    ports = execution.make_pyramid_port_state(proposal=pyr_prop, pyramid_state=current,
        trade_metadata=metadata, portfolio_rows=[{"id": 1, "entry_price": 100,
            "qty": continued["qty"], "leverage": 5}])
    pyramid = complete_pyramid_request(side=side, at_ms=pyramid_at, opportunity_id="opp-1",
        symbol=symbol, portfolio_state=balance, port_state=ports,
        source="TEST_ONLY_OBSERVED_EVENTS")
    pyramid_report = execution.replay_execution(pyramid)
    if pyramid_report["status"] != "COMPLETE_EXECUTION":
        raise AssertionError(pyramid_report["gaps"])
    writer = pyramid_report["manager_updates"][0]["writer_state"]
    final_initial = {**continued, "qty": writer["qty"], "notes": writer["notes"]}
    final = bound_manager(cfg=cfg, initial=final_initial, ticks=[tick("pyramid-final", at_ms=EXIT,
        ownership_guard=[{"allowed": True}], get_positions=[{"ok": True, "positions": []}],
        get_executions=[{"ok": True, "fills": [{"side": "SELL" if side == "long" else "BUY",
            "price": prop["tp2"], "qty": writer["qty"], "fee": .2, "time": EXIT}]}],
        cleanup_orphan_conditionals=[{"confirmed_absent": True}], close_trade=[{"applied": True}])])
    final_report = study.manager.run_trace(final)
    if final_report["status"] != "COMPLETE_TRACE":
        raise AssertionError(final_report["gaps"])
    record["steps"] = [{"kind": "EXECUTION", "at_ms": ENTRY, "request": entry},
        {"kind": "MANAGEMENT", "at_ms": at, "request": first},
        {"kind": "EXECUTION", "at_ms": pyramid_at, "request": pyramid},
        {"kind": "MANAGEMENT", "at_ms": EXIT, "request": final}]
    record["context"].update(side=side.upper(), planned_stop=str(prop["stop"]))
    root_binding = {k: v for k, v in record["context"].items() if k != "planned_stop"}
    record["accounting"]["orders"], record["accounting"]["fills"] = [], []
    executions = [("entry-1", "ENTRY", ENTRY, 100, 2, 0, 0, "cw-test-entry-1", "fill-1"),
        ("tp1-exec", "TP1", at, prop["tp1"], .9, 9, .1, "cw-test-entry-1-tp1", "tp1-fill"),
        ("pyr-1", "PYRAMID", pyramid_at, pyr_prop["entry"], 1, 0, 0, "cw-pyr-1-1", "pyr-fill-1"),
        ("tp2-exec", "TP2", EXIT, prop["tp2"], writer["qty"], 32, .2, "cw-test-entry-1-tp2", "tp2-fill")]
    for i, (oid, role, stamp, price, qty, pnl, fee, coid, fid) in enumerate(executions):
        binding = {**root_binding, "order_id": oid, "dispatch_id": coid}
        header = {"event_id": f"order-{oid}", "causal_index": 2*i,
            "observed_at_ms": stamp, "available_at_ms": stamp, "binding": binding, "role": role}
        entering = role in ("ENTRY", "PYRAMID")
        order_side = ("BUY" if side == "long" else "SELL") if entering else ("SELL" if side == "long" else "BUY")
        record["accounting"]["orders"].append({**copy.deepcopy(header), "raw": {"orderId": oid,
            "clientOrderId": coid, "status": "FILLED", "symbol": "BTCUSDT", "positionSide": "BOTH",
            "side": order_side, "executedQty": qty, "avgPrice": price, "reduceOnly": not entering,
            "type": "MARKET", "updateTime": stamp}})
        record["accounting"]["fills"].append({**copy.deepcopy(header), "event_id": fid, "causal_index": 2*i+1,
            "raw": {"id": fid, "orderId": oid, "symbol": "BTCUSDT", "positionSide": "BOTH",
                "side": order_side, "price": price, "qty": qty, "realizedPnl": pnl,
                "commission": fee, "commissionAsset": "USDT", "time": stamp}})
    record["accounting"]["coverage"]["source_ref"]["exposure_intervals"][0]["side"] = side.upper()
    action = next(a for a in entry_report["actions"] if a["port"] == "place_protection")
    record["order_links"] = []
    for oid, role, algo_id, dispatch in (("tp1-exec", "TP1", "tp1-1", "cw-test-entry-1-tp1"),
                                         ("tp2-exec", "TP2", "tp2-1", "cw-test-entry-1-tp2")):
        corr = {"contract": "CW_CONDITIONAL_EXECUTION_CORRELATION_V1", "source": "TEST_ONLY",
            **{k:record["context"][k] for k in ("account_scope", "symbol", "trade_id", "opportunity_id")},
            "order_id": oid, "algo_id": algo_id, "dispatch_id": dispatch, "role": role,
            "action_request_hash": action["request_hash"], "observed_at_ms": ENTRY, "available_at_ms": ENTRY}
        record["order_links"].append({"order_id": oid, "algo_id": algo_id, "dispatch_id": dispatch,
            "role": role, "step": 0, "call_index": action["call_index"], "correlation": corr})
    data["rows"][0].update(symbol=symbol, side=side, stop_loss=prop["stop"], tp1=prop["tp1"], tp2=prop["tp2"])
    data.update(manifest_hash=manifest["manifest_hash"], dataset_hash=None,
        events={"baseline": [record], "candidate": [copy.deepcopy(record)]})
    data = study.validate_dataset(data, manifest)
    _PYRAMID_CACHE[identity] = copy.deepcopy((manifest, data))
    return manifest, data


def native_chain_fixture():
    """Two real manager segments; bound synthetic facts, not account history."""
    identity = study.engine_fingerprint()
    if identity in _NATIVE_CACHE:
        return copy.deepcopy(_NATIVE_CACHE[identity])
    manifest, data = entry_fixture()
    record = copy.deepcopy(data["events"]["baseline"][0])
    cfg = manager_config(TIME_STOP_ENABLED=True, TIME_STOP_DAY_MIN=1,
                         BE_STRUCTURAL_ENABLED=False, PRE_TP1_PROTECT_ENABLED=False)
    descriptor = {"engine_version": study.manager.ENGINE_VERSION,
        "source_fingerprint": study.manager.source_fingerprint(), "config": cfg, "config_hash": study.digest(cfg)}
    manifest = {k: copy.deepcopy(v) for k,v in manifest.items() if k != "manifest_hash"}
    manifest["hashes"] = None
    manifest["shared"]["management"] = descriptor
    for arm in ("baseline", "candidate"):
        manifest[arm]["management_config"] = copy.deepcopy(descriptor)
    manifest = study.parse_manifest(manifest)
    at = ENTRY + 10000
    initial = copy.deepcopy(record["steps"][1]["request"]["initial_state"])
    initial["created_at_ms"] = ENTRY
    pos = {"ok": True, "positions": [{"symbol": "BTCUSDT", "side": "Buy", "size": 1.1,
                                   "entry_price": 100., "position_side": "BOTH"}]}
    first = bound_manager(cfg=cfg, initial=initial, ticks=[tick("chain-tp1", at_ms=at,
        ownership_guard=[{"allowed": True}], get_positions=[pos, pos],
        place_protection_orders=[{"sl_ok": True, "sl_order_id": "SL-BE"}],
        cancel_algo_order=[{"ok": True}], get_executions=[{"ok": True, "fills": []}])])
    first_report = study.manager.run_trace(first)
    if first_report["status"] != "COMPLETE_TRACE":
        raise AssertionError(first_report["gaps"])
    continued = study._manager_projection(first_report["snapshots"][-1]["state"])
    if continued["qty"] != 1.1 or continued["phase"] != "post_tp1":
        raise AssertionError(continued)
    final = bound_manager(cfg=cfg, initial=continued, ticks=[tick("chain-final", at_ms=EXIT,
        ownership_guard=[{"allowed": True}], get_positions=[{"ok": True, "positions": []}],
        get_executions=[{"ok": True, "fills": [{"side": "SELL", "price": 120,
                        "qty": 1.1, "fee": .2, "time": EXIT}]}],
        cleanup_orphan_conditionals=[{"confirmed_absent": True}], close_trade=[{"applied": True}])])
    final_report = study.manager.run_trace(final)
    if final_report["status"] != "COMPLETE_TRACE":
        raise AssertionError(final_report["gaps"])
    record["steps"] = [record["steps"][0], {"kind": "MANAGEMENT", "at_ms": at, "request": first},
                       {"kind": "MANAGEMENT", "at_ms": EXIT, "request": final}]
    accounting = record["accounting"]
    root_binding = {k:v for k,v in record["context"].items() if k != "planned_stop"}
    accounting["orders"], accounting["fills"] = [], []
    executions = [("entry-1", "ENTRY", ENTRY, 100, 2, 0, 0, "cw-test-entry-1", "fill-1"),
                  ("tp1-exec", "TP1", at, 110, .9, 9, .1, "cw-test-entry-1-tp1", "tp1-fill"),
                  ("tp2-exec", "TP2", EXIT, 120, 1.1, 22, .2, "cw-test-entry-1-tp2", "tp2-fill")]
    for i, (oid, role, stamp, price, qty, pnl, fee, coid, fid) in enumerate(executions):
        binding = {**root_binding, "order_id": oid, "dispatch_id": coid}
        header = {"event_id": f"order-{oid}", "causal_index": 2*i,
            "observed_at_ms": stamp, "available_at_ms": stamp, "binding": binding, "role": role}
        side = "BUY" if role == "ENTRY" else "SELL"
        accounting["orders"].append({**copy.deepcopy(header), "raw": {"orderId": oid,
            "clientOrderId": coid, "status": "FILLED", "symbol": "BTCUSDT", "positionSide": "BOTH",
            "side": side, "executedQty": qty, "avgPrice": price, "reduceOnly": role != "ENTRY",
            "type": "MARKET", "updateTime": stamp}})
        accounting["fills"].append({**copy.deepcopy(header), "event_id": fid, "causal_index": 2*i+1,
            "raw": {"id": fid, "orderId": oid, "symbol": "BTCUSDT", "positionSide": "BOTH",
                "side": side, "price": price, "qty": qty, "realizedPnl": pnl,
                "commission": fee, "commissionAsset": "USDT", "time": stamp}})
    entry_report = execution.replay_execution(record["steps"][0]["request"])
    action = next(a for a in entry_report["actions"] if a["port"] == "place_protection")
    record["order_links"] = []
    for oid, role, algo_id, dispatch in (("tp1-exec", "TP1", "tp1-1", "cw-test-entry-1-tp1"),
                                         ("tp2-exec", "TP2", "tp2-1", "cw-test-entry-1-tp2")):
        correlation = {"contract": "CW_CONDITIONAL_EXECUTION_CORRELATION_V1", "source": "TEST_ONLY",
            **{k:record["context"][k] for k in ("account_scope", "symbol", "trade_id", "opportunity_id")},
            "order_id": oid, "algo_id": algo_id, "dispatch_id": dispatch, "role": role,
            "action_request_hash": action["request_hash"], "observed_at_ms": ENTRY, "available_at_ms": ENTRY}
        record["order_links"].append({"order_id": oid, "algo_id": algo_id, "dispatch_id": dispatch,
            "role": role, "step": 0, "call_index": action["call_index"], "correlation": correlation})
    data.update(manifest_hash=manifest["manifest_hash"], dataset_hash=None,
        events={"baseline": [record], "candidate": [copy.deepcopy(record)]})
    data = study.validate_dataset(data, manifest)
    _NATIVE_CACHE[identity] = copy.deepcopy((manifest, data))
    return manifest, data


def continuation_fixture():
    previous = state(id=1, symbol="BTC/USDT", qty=1.1, qty_initial=2., phase="post_tp1",
        entry_price=100., planned_stop=95., planned_tp1=110., planned_tp2=120.,
        sl_current_price=100., sl_order_id="SL-BE", tp2_order_id="tp2-1",
        created_at_ms=1000000, opened_at_ms=1000000, notes="original marker")
    original = proposal()
    p = {**original, "entry": 110, "stop": 100, "dispatch_id": "cw-pyr-1-1",
         "decision_at_ms": 1030000}
    current = {key: previous[key] for key in
        ("id", "symbol", "side", "qty", "qty_initial", "entry_price", "planned_stop", "phase", "notes")}
    current.update(leverage=5, pyramiding_level=0, sl_close_position=True,
                   tp2_close_position=True, protection_confirmed=True)
    metadata = {key: previous[key] for key in ("source", "status", "exchange", "timeframe",
               "created_at_ms", "sl_current_price", "sl_order_id", "tp2_order_id")}
    metadata["recommendation_id"] = 1001
    ports = execution.make_pyramid_port_state(proposal=p, pyramid_state=current,
        trade_metadata=metadata, portfolio_rows=[{"id": 1, "entry_price": 100, "qty": 1.1, "leverage": 5}])
    return {"proposal": p, "pyramid_state": current, "port_state": ports}, previous, original


class ContinuationBoundaryTests(unittest.TestCase):
    def test_projection_uses_persisted_contract_not_untrusted_caller_fields(self):
        original = state(qty=1.1, phase="post_tp1", notes="keep critical marker")
        projected = study._manager_projection({**original, "pnl_usd": 999, "updated_at_ms": 88,
                                                "pyramiding_level": 5, "leverage": 19})
        self.assertEqual(projected, original)
        projected["notes"] = "changed locally"
        self.assertEqual(original["notes"], "keep critical marker")

    def test_projection_preserves_actual_created_and_opened_clocks(self):
        snapshot = state(opened_at_ms=None, created_at_ms=None)
        snapshot.update(opened_at="2026-10-01T00:00:00+00:00", created_at="2026-10-01T00:01:00+00:00")
        projected = study._manager_projection(snapshot)
        self.assertEqual(projected["created_at_ms"] - projected["opened_at_ms"], 60000)
        snapshot["created_at"] = "not a clock"
        with self.assertRaisesRegex(ValueError, "CLOCK_FORMAT"):
            study._manager_projection(snapshot)

    def test_exact_continuation_preserves_original_entry_clocks_and_protection(self):
        request, previous, entry = continuation_fixture()
        before = copy.deepcopy(request)
        study._pyramid_continuation(request, previous, entry, 0)
        self.assertEqual(request, before)

    def test_state_drift_never_becomes_a_pyramid_even_with_fresh_config(self):
        for field, value in (("qty", 2), ("qty_initial", 3), ("entry_price", 101),
                             ("planned_stop", 96), ("phase", "runner"), ("notes", "lost marker")):
            with self.subTest(field=field):
                request, previous, entry = continuation_fixture()
                request["pyramid_state"][field] = value
                with self.assertRaisesRegex(ValueError, "MANAGER_STATE_DRIFT"):
                    study._pyramid_continuation(request, previous, entry, 0)

    def test_metadata_clocks_and_exact_protection_ids_cannot_be_reseeded(self):
        for field, value in (("created_at_ms", 1030000), ("sl_current_price", 99),
                             ("sl_order_id", "other"), ("tp2_order_id", "other"),
                             ("source", "managed"), ("timeframe", "4h")):
            with self.subTest(field=field):
                request, previous, entry = continuation_fixture()
                request["port_state"]["trade"][field] = value
                with self.assertRaisesRegex(ValueError, "PERSISTED_META_DRIFT"):
                    study._pyramid_continuation(request, previous, entry, 0)

    def test_closed_runner_or_absent_manager_cannot_authorize_reinforcement(self):
        for changes in ({"status": "closed_tp2"}, {"phase": "runner"}, {"phase": "pre_tp1"}):
            request, previous, entry = continuation_fixture()
            previous.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "NOT_OPEN_POST_TP1"):
                study._pyramid_continuation(request, previous, entry, 0)
        request, _, entry = continuation_fixture()
        with self.assertRaisesRegex(ValueError, "NOT_OPEN_POST_TP1"):
            study._pyramid_continuation(request, None, entry, 0)

    def test_level_leverage_account_quote_and_direction_are_not_exchangeable(self):
        for key, value in (("leverage", 10), ("account_ref", "other"), ("symbol", "BTC/USDC"),
                           ("side", "short"), ("trade_id", 99), ("opportunity_id", "other")):
            request, previous, entry = continuation_fixture()
            request["proposal"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "ENTRY_IDENTITY"):
                study._pyramid_continuation(request, previous, entry, 0)
        request, previous, entry = continuation_fixture()
        with self.assertRaisesRegex(ValueError, "LEVEL_OR_LEVERAGE"):
            study._pyramid_continuation(request, previous, entry, 1)

    def test_segment_end_is_last_observation_not_declared_step_start(self):
        step = {"kind": "MANAGEMENT", "at_ms": 10, "request": {"ticks": [
            {"at_ms": 20, "ports": {"get_positions": [{"available_at_ms": 19}]}},
            {"at_ms": 30, "ports": {}}]}}
        self.assertEqual(study._step_end(step, {}), 30)
        step = {"kind": "EXECUTION", "at_ms": 10}
        report = {"actions": [{"at_ms": 20}], "reads": [{"at_ms": 25}],
                  "fills": [{"available_at_ms": 31}], "manager_updates": [{"writer_available_at_ms": 35}]}
        self.assertEqual(study._step_end(step, report), 35)


class CausalPortfolioBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.book = study._CausalPortfolio(portfolio())
        self.entry = {"mode": "ENTRY", "proposal": proposal()}
        self.pyramid = {"mode": "PYRAMID", "proposal": {**proposal(), "entry": 110, "stop": 100}}
        self.initial_report = {"filled_qty": 2, "entry_basis": 100, "admission":
            {"margin_usd": 40, "risk_usd": 10, "notional_usd": 200}}

    def management(self, qty=1.1, status="open", sl=100):
        return {"snapshots": [{"state": state(id=1, symbol="BTCUSDT", qty=qty,
            qty_initial=2, entry_price=100, status=status, sl_current_price=sl, phase="post_tp1")} ]}

    def test_no_future_pyramid_or_profit_exists_before_its_execution(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        before = self.book.at(1010000)
        self.assertEqual(before["available_margin_usd"], 760)
        self.assertEqual(before["equity_usd"], 1000)
        self.assertEqual(before["open_risk_usd"], 10)
        self.book.management("opp-1", self.management())
        partial = self.book.at(1020000)
        self.assertEqual(partial["available_margin_usd"], 778)
        self.assertEqual(partial["equity_usd"], 1000)
        self.assertEqual(partial["open_risk_usd"], 0)
        pyramid = {"filled_qty": 1, "entry_basis": 110, "retained_confirmed_risk_usd": 10,
                   "admission": {"margin_usd": 22, "risk_usd": 10, "notional_usd": 110}}
        self.book.execution("opp-1", self.pyramid, pyramid)
        after = self.book.at(1030000)
        self.assertEqual(after["available_margin_usd"], 756)
        self.assertEqual(after["open_risk_usd"], 10)
        self.assertEqual(after["open_positions"], 1)
        self.assertEqual(before["available_margin_usd"], 760)

    def test_later_management_does_not_erase_confirmed_pyramid_risk(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        self.book.management("opp-1", self.management())
        self.book.execution("opp-1", self.pyramid, {"filled_qty": 1, "entry_basis": 110,
            "retained_confirmed_risk_usd": 10, "admission": {"margin_usd": 22, "risk_usd": 10, "notional_usd": 110}})
        self.book.management("opp-1", self.management(qty=2.1))
        self.assertEqual(self.book.at(1040000)["open_risk_usd"], 10)
        self.assertEqual(self.book.at(1040000)["available_margin_usd"], 758)

    def test_heuristic_closed_never_releases_money_or_slot_without_ledger(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        self.book.management("opp-1", self.management(status="closed_stop", qty=0))
        current = self.book.at(1050000)
        self.assertEqual(current["open_positions"], 1)
        self.assertEqual(current["available_margin_usd"], 760)
        self.assertEqual(current["equity_usd"], 1000)

    def test_unknown_reinforcement_retains_budget_without_a_new_slot(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        self.book.unknown({"reservations": {"slot_retained": False, "risk_usd": 10, "margin_usd": 22}})
        current = self.book.at(1050000)
        self.assertEqual(current["pending_count"], 0)
        self.assertEqual(current["pending_risk_usd"], 10)
        self.assertEqual(current["pending_margin_usd"], 22)
        self.assertEqual(current["open_positions"], 1)
        self.assertFalse(current["complete"])

    def test_settlement_releases_exact_applied_components_once(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        self.book.management("opp-1", self.management())
        self.book.execution("opp-1", self.pyramid, {"filled_qty": 1, "entry_basis": 110,
            "retained_confirmed_risk_usd": 10, "admission": {"margin_usd": 22, "risk_usd": 10, "notional_usd": 110}})
        money = {"net_including_funding": "30", "role_events": [{"fill_time_ms": 1060000}]}
        self.book.settle("opp-1", money, 1060001)
        after = self.book.at(1060001)
        self.book.settle("opp-1", money, 1060002)
        self.assertEqual(self.book.at(1060002)["equity_usd"], after["equity_usd"])
        self.assertEqual(after["equity_usd"], 1030)
        self.assertEqual(after["available_margin_usd"], 830)
        self.assertEqual(after["open_positions"], 0)
        self.assertEqual(after["open_notional_usd"], 0)
        self.assertEqual(after["open_risk_usd"], 0)

    def test_receipt_wait_reserves_slot_risk_and_margin_before_fill_is_known(self):
        request = {**self.entry, "request_hash": "explicit-entry-request"}
        report = {**self.initial_report, "status": "COMPLETE_EXECUTION",
            "admissions": [{**self.initial_report["admission"], "decision": "ADMITTED"}]}
        self.book.reserve("opp-1", request, report)
        waiting = self.book.at(1000001)
        self.assertEqual((waiting["pending_count"], waiting["pending_risk_usd"], waiting["pending_margin_usd"]),
                         (1, 10, 40))
        self.assertEqual(waiting["open_positions"], 0)
        self.assertEqual(waiting["equity_usd"], 1000)
        proposal_for_second = {**proposal(), "opportunity_id": "second", "trade_id": 2,
                              "dispatch_id": "cw-second-entry", "decision_at_ms": 1000001}
        proposal_for_second["sizing_context"] = {**proposal_for_second["sizing_context"],
            "observed_at_ms": 1000001, "available_at_ms": 1000001}
        cfg = self.fixtures_config(max_slots=1)
        admission = execution.compute_admission(cfg, proposal_for_second, waiting)
        self.assertNotEqual(admission["decision"], "ADMITTED")
        self.book.execution("opp-1", request, report)
        applied = self.book.at(1000002)
        self.assertEqual((applied["pending_count"], applied["pending_risk_usd"], applied["pending_margin_usd"]),
                         (0, 0, 0))
        self.assertEqual((applied["open_positions"], applied["open_risk_usd"], applied["available_margin_usd"]),
                         (1, 10, 760))
        self.book.release_pending("opp-1", request)
        self.assertEqual(self.book.at(1000003), {**applied, "margin_as_of_ms": 1000003})

    @staticmethod
    def fixtures_config(max_slots=5):
        from tests.test_live_event_execution import config
        out = config()
        out["MAX_OPEN_POSITIONS"] = max_slots
        return out

    def test_confirmed_zero_releases_only_its_reservation_once(self):
        request = {**self.entry, "request_hash": "terminal-zero"}
        admitted = {"decision": "ADMITTED", "risk_usd": 10, "margin_usd": 40, "notional_usd": 200}
        report = {"filled_qty": 0, "status": "COMPLETE_EXECUTION", "admission": admitted}
        self.book.reserve("opp-1", request, report)
        self.book.execution("opp-1", request, report)
        self.book.execution("opp-1", request, report)
        current = self.book.at(1000001)
        self.assertEqual((current["pending_count"], current["pending_risk_usd"], current["pending_margin_usd"]),
                         (0, 0, 0))
        self.assertEqual(current["open_positions"], 0)
        self.assertEqual(current["available_margin_usd"], 800)

    def test_pending_margin_and_risk_are_consumed_by_independent_admission(self):
        for constraint in ("margin", "risk"):
            with self.subTest(constraint=constraint):
                initial = portfolio()
                cfg = self.fixtures_config()
                if constraint == "margin":
                    initial["available_margin_usd"] = 60
                else:
                    cfg["MAX_OPEN_RISK_PCT"] = 1.5
                book = study._CausalPortfolio(initial)
                request = {**self.entry, "request_hash": "pending-" + constraint}
                admitted = {"decision": "ADMITTED", "risk_usd": 10, "margin_usd": 40,
                            "notional_usd": 200}
                book.reserve("opp-1", request, {"status": "COMPLETE_EXECUTION", "admission": admitted})
                proposal_for_second = {**proposal(), "opportunity_id": "second", "trade_id": 2,
                                      "dispatch_id": "cw-second-entry"}
                admission = execution.compute_admission(cfg, proposal_for_second, book.at(1000000))
                self.assertNotEqual(admission["decision"], "ADMITTED")
                self.assertEqual(book.at(1000000)["pending_risk_usd"], 10)
                self.assertEqual(book.at(1000000)["pending_margin_usd"], 40)

    def test_unknown_receipt_transfers_same_hold_once_without_free_capacity(self):
        request = {**self.entry, "request_hash": "uncertain-entry"}
        admitted = {"decision": "ADMITTED", "risk_usd": 10, "margin_usd": 40, "notional_usd": 200}
        report = {"admission": admitted, "status": "INCOMPLETE_EXECUTION",
                  "reservations": {"slot_retained": True, "risk_usd": 10, "margin_usd": 40}}
        self.book.reserve("opp-1", request, report)
        self.book.unknown(report, key="opp-1", request=request)
        self.book.unknown(report, key="opp-1", request=request)
        current = self.book.at(1000001)
        self.assertEqual((current["pending_count"], current["pending_risk_usd"], current["pending_margin_usd"]),
                         (1, 10, 40))
        self.assertFalse(current["complete"])
        self.assertEqual(current["equity_usd"], 1000)

    def test_nonfinite_manager_exposure_is_refused_without_changing_book(self):
        self.book.execution("opp-1", self.entry, self.initial_report)
        before = self.book.at(1000000)
        for field in ("qty", "entry_price", "sl_current_price"):
            for value in (float("inf"), float("nan"), True, "1"):
                report = self.management()
                report["snapshots"][0]["state"][field] = value
                with self.subTest(field=field, value=repr(value)), self.assertRaisesRegex(ValueError, "EXPOSURE_UNKNOWN"):
                    self.book.management("opp-1", report)
                self.assertEqual(self.book.at(1000000), before)

    def test_partial_fill_transfers_actual_quantity_price_not_planned_sizing(self):
        request = {**self.entry, "request_hash": "partial-fill"}
        report = {"status": "COMPLETE_EXECUTION", "filled_qty": 1, "entry_basis": 102,
                  "admission": {"decision": "ADMITTED", "risk_usd": 10,
                                "margin_usd": 40, "notional_usd": 200}}
        self.book.reserve("opp-1", request, report)
        self.assertEqual(self.book.at(1000000)["pending_margin_usd"], 40)
        self.book.execution("opp-1", request, report)
        after = self.book.at(1000001)
        self.assertAlmostEqual(after["available_margin_usd"], 779.6)
        self.assertEqual(after["open_notional_usd"], 102)
        self.assertEqual(after["open_risk_usd"], 7)
        self.assertEqual(after["open_positions"], 1)
        self.assertEqual((after["pending_risk_usd"], after["pending_margin_usd"]), (0, 0))


class NativeSegmentChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.data = native_chain_fixture()

    def run_record(self, record):
        return study._run_record(record, self.manifest, self.data["rows"][0],
                                 self.manifest["shared"]["initial_portfolio"])

    def test_two_actual_manager_segments_keep_persisted_state_and_known_money(self):
        out = self.run_record(copy.deepcopy(self.data["events"]["baseline"][0]))
        self.assertTrue(out["complete"], out["ledger"]["reasons"])
        self.assertEqual(len(out["step_reports"]), 3)
        self.assertEqual(out["step_reports"][1]["snapshots"][-1]["state"]["phase"], "post_tp1")
        self.assertEqual(float(out["ledger"]["net_including_funding"]), 30.7)
        self.assertEqual(float(out["ledger"]["accounting"]["totals"]["entry_qty_executed"]), 2)
        self.assertFalse(out["ledger"]["observed_money"])
        self.assertFalse(out["management"]["authorizes_live"])

    def test_next_segment_cannot_change_original_clocks_or_initial_quantity(self):
        for field, value in (("created_at_ms", ENTRY + 1), ("opened_at_ms", ENTRY + 1),
                             ("qty_initial", 3), ("entry_price", 101), ("sl_order_id", "unrelated")):
            with self.subTest(field=field):
                record = copy.deepcopy(self.data["events"]["baseline"][0])
                record["steps"][2]["request"]["initial_state"][field] = value
                with self.assertRaisesRegex(ValueError, "CONTINUATION_DRIFT"):
                    self.run_record(record)

    def test_next_segment_before_last_observed_tick_is_rejected(self):
        record = copy.deepcopy(self.data["events"]["baseline"][0])
        record["steps"][2]["at_ms"] = ENTRY + 9999
        with self.assertRaisesRegex(ValueError, "STEP_CHRONOLOGY"):
            self.run_record(record)

    def test_root_scheduler_runs_management_reports_not_last_execution_again(self):
        result = study.run_study(manifest=self.manifest, dataset=self.data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        for arm in result["arms"].values():
            self.assertEqual(arm["records"][0]["state"], "COMPLETE", arm)
            self.assertEqual(arm["portfolio_at_last_decision"]["equity_usd"], 1000)
            self.assertEqual(arm["economics"][0]["net_r"], 3.07)


class NativePyramidChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = {side: native_pyramid_chain_fixture(side) for side in ("long", "short")}
        cls.results = {side: study.run_study(manifest=m, dataset=d, now_ms=NOW)
                       for side, (m, d) in cls.fixtures.items()}

    def test_native_long_and_short_full_chain_derive_money_only_from_exact_fills(self):
        for side, result in self.results.items():
            with self.subTest(side=side):
                self.assertTrue(result["ok"], result)
                for arm in result["arms"].values():
                    record = arm["records"][0]
                    self.assertEqual(record["state"], "COMPLETE", record)
                    self.assertEqual(len(record["step_reports"]), 4)
                    self.assertEqual(len(record["execution_reports"]), 2)
                    money = record["ledger"]
                    self.assertAlmostEqual(float(money["net_including_funding"]), 40.7)
                    self.assertEqual(float(money["accounting"]["totals"]["entry_qty_executed"]), 3)
                    self.assertFalse(money["observed_money"])
                self.assertEqual(result["arms"]["baseline"]["economics"], result["arms"]["candidate"]["economics"])
                for flag in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"):
                    self.assertIs(result[flag], False)
                self.assertEqual(result["holdout_status"], "SEALED")

    def test_manager_after_pyramid_consumes_writer_qty_notes_and_original_basis(self):
        for side, result in self.results.items():
            with self.subTest(side=side):
                record = result["arms"]["baseline"]["records"][0]
                self.assertEqual(record["state"], "COMPLETE", record)
                pyramid = record["step_reports"][2]
                persisted = pyramid["manager_updates"][0]["writer_state"]
                after = result["dataset"]["events"]["baseline"][0]["steps"][3]["request"]["initial_state"]
                self.assertEqual(after["qty"], persisted["qty"])
                self.assertEqual(after["qty"], 2.1)
                self.assertEqual(after["notes"], persisted["notes"])
                self.assertIn("pyramid L1", after["notes"])
                self.assertEqual(after["entry_price"], 100)
                self.assertEqual(after["qty_initial"], 2)
                self.assertEqual(after["created_at_ms"], ENTRY)
                self.assertEqual(after["opened_at_ms"], ENTRY)
                self.assertEqual(after["sl_order_id"], "SL-BE")
                self.assertEqual(after["tp2_order_id"], "tp2-1")
                self.assertEqual(pyramid["persistence_scope"], "RECEIPT_BOUND_SIMULATED_PORTS")

    def test_pyramid_costs_start_at_pyramid_clock_not_at_original_entry(self):
        manifest, original = self.fixtures["long"]
        for at, margin, risk, notional, slots, equity in (
                (ENTRY + 19999, 778, 0, 110, 1, 1000),
                (ENTRY + 20000, 778, 0, 110, 1, 1000),
                (ENTRY + 20001, 756, 10, 220, 1, 1000),
                (EXIT + 1, 840.7, 0, 0, 0, 1040.7)):
            with self.subTest(at=at):
                data = copy.deepcopy(original)
                data["dataset_hash"] = None
                row = {**copy.deepcopy(data["rows"][0]), "opportunity_key": "vetoed-observer",
                    "decision_ts_ms": at, "available_at_ms": at, "observed_outcome": "VETOED"}
                data["rows"].append(row)
                data["index"].append({"opportunity_key": "vetoed-observer", "decision_ts_ms": at,
                                       "causal_index": 2})
                result = study.run_study(manifest=manifest, dataset=data, now_ms=NOW)
                self.assertTrue(result["ok"], result)
                state = result["arms"]["baseline"]["portfolio_at_last_decision"]
                self.assertAlmostEqual(state["available_margin_usd"], margin)
                self.assertAlmostEqual(state["open_risk_usd"], risk)
                self.assertAlmostEqual(state["open_notional_usd"], notional)
                self.assertEqual(state["open_positions"], slots)
                self.assertAlmostEqual(state["equity_usd"], equity)

    def test_pyramid_fill_not_present_in_actual_execution_cannot_create_profit(self):
        manifest, data = copy.deepcopy(self.fixtures["long"])
        data["dataset_hash"] = None
        fill = next(f for f in data["events"]["candidate"][0]["accounting"]["fills"]
                    if f["role"] == "PYRAMID")
        fill["raw"]["price"] = 111
        result = study.run_study(manifest=manifest, dataset=data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        self.assertIn("EVENT_EXECUTION_LEDGER_FILL_MISMATCH", result["technical_gaps"])
        self.assertIsNone(result["arms"]["candidate"]["economics"][0]["net_r"])
        self.assertEqual(result["arms"]["baseline"]["records"][0]["state"], "COMPLETE")

    def test_final_management_cannot_lose_reinforcement_or_replace_original_entry(self):
        manifest, original = self.fixtures["long"]
        for field, value in (("qty", 1.1), ("qty_initial", 3), ("entry_price", 110),
                             ("notes", None), ("created_at_ms", ENTRY + 20000)):
            with self.subTest(field=field):
                data = copy.deepcopy(original)
                data["dataset_hash"] = None
                req = data["events"]["candidate"][0]["steps"][3]["request"]
                req["initial_state"][field] = value
                result = study.run_study(manifest=manifest, dataset=data, now_ms=NOW)
                self.assertTrue(result["ok"], result)
                self.assertIn("EVENT_MANAGER_CONTINUATION_DRIFT", result["technical_gaps"])
                self.assertIsNone(result["arms"]["candidate"]["economics"][0]["net_r"])

    def test_financial_completion_never_releases_before_last_manager_proof(self):
        manifest, data = copy.deepcopy(self.fixtures["long"])
        data["dataset_hash"] = None
        for arm in ("baseline", "candidate"):
            step = data["events"][arm][0]["steps"][3]
            old = step["request"]
            events = copy.deepcopy(old["ticks"])
            events[0]["at_ms"] = EXIT + 100
            for values in events[0]["ports"].values():
                for value in values:
                    value["observed_at_ms"] = EXIT + 100
                    value["available_at_ms"] = EXIT + 100
            step["request"] = bound_manager(cfg=old["config"], initial=old["initial_state"], ticks=events)
            step["at_ms"] = EXIT + 100
        row = {**copy.deepcopy(data["rows"][0]), "opportunity_key": "before-confirmation",
            "decision_ts_ms": EXIT + 50, "available_at_ms": EXIT + 50, "observed_outcome": "VETOED"}
        data["rows"].append(row)
        data["index"].append({"opportunity_key": "before-confirmation", "decision_ts_ms": EXIT + 50,
                              "causal_index": 2})
        result = study.run_study(manifest=manifest, dataset=data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        state = result["arms"]["baseline"]["portfolio_at_last_decision"]
        self.assertEqual(state["equity_usd"], 1000)
        self.assertEqual(state["open_positions"], 1)
        self.assertAlmostEqual(state["available_margin_usd"], 756)


if __name__ == "__main__":
    unittest.main()
