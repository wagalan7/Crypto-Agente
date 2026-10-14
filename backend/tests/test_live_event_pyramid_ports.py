"""Receipt-bound simulated PYRAMID ports: no real DB or LIVE authority.

Fixtures are explicit declared event tables. Source-selected LIVE callers run
inside the existing isolated worker; a receipt is not a permissive mock.
"""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "pyramid_event_contract", BACKEND / "services/live_event_execution_service.py")
execution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(execution)
fixture_spec = importlib.util.spec_from_file_location(
    "pyramid_base_fixtures", BACKEND / "tests/test_live_event_execution.py")
fixtures = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)
_COMPLETE_CACHE = {}


def pyramid_inputs(*, side="long", at_ms=1000000, trade_id=1,
                   opportunity_id="pyramid-1", symbol="BTC/USDT:USDT"):
    cfg, prop, balance = fixtures.config(pyramid=True), fixtures.proposal(), fixtures.portfolio()
    cfg["pyramid_limits"] = {"max_total_margin_pct": 70, "depth_limit": 20,
                             "depth_timeout_s": 2}
    entry = 110 if side == "long" else 90
    prop.update(opportunity_id=opportunity_id, trade_id=trade_id, side=side,
                symbol=symbol, dispatch_id=f"cw-pyr-{trade_id}-1", decision_at_ms=at_ms,
                entry=entry, stop=100, tp1=115 if side == "long" else 85,
                tp2=120 if side == "long" else 80)
    prop["sizing_context"].update(observed_at_ms=at_ms, available_at_ms=at_ms)
    balance.update(margin_as_of_ms=at_ms, open_positions=1, open_notional_usd=100)
    state = {"id": trade_id, "symbol": symbol, "side": side, "qty": 1,
             "qty_initial": 2, "entry_price": 100,
             "planned_stop": 95 if side == "long" else 105, "leverage": 5,
             "phase": "post_tp1", "pyramiding_level": 0, "notes": "nota original",
             "sl_close_position": True, "tp2_close_position": True,
             "protection_confirmed": True}
    metadata = {"source": "auto", "status": "open", "exchange": "binance",
                "timeframe": "1h", "created_at_ms": at_ms - 100000,
                "recommendation_id": trade_id + 1000,
                "sl_current_price": 100, "sl_order_id": f"sl-{trade_id}",
                "tp2_order_id": f"tp2-{trade_id}"}
    port_state = execution.make_pyramid_port_state(
        proposal=prop, pyramid_state=state, trade_metadata=metadata,
        portfolio_rows=[{"id": trade_id, "entry_price": 100, "qty": 1, "leverage": 5}])
    return cfg, prop, balance, state, port_state


def seal_port_state(state):
    state = copy.deepcopy(state)
    state["state_hash"] = execution.digest({k: v for k, v in state.items() if k != "state_hash"})
    return state


def make_pyramid_request(*, side="long", at_ms=1000000, trade_id=1,
                         opportunity_id="pyramid-1", symbol="BTC/USDT:USDT",
                         receipts=None, fills=None, portfolio_state=None,
                         port_state=None, source="DECLARED_EVENT_TABLE"):
    """Shared causal fixture builder; supplied receipts remain explicit facts."""
    cfg, prop, balance, state, initial_ports = pyramid_inputs(
        side=side, at_ms=at_ms, trade_id=trade_id,
        opportunity_id=opportunity_id, symbol=symbol)
    if portfolio_state is not None:
        balance = copy.deepcopy(portfolio_state)
    if port_state is not None:
        initial_ports = copy.deepcopy(port_state)
        state = {key: initial_ports["trade"][key] for key in execution.PYRAMID_FIELDS}
    rows, facts = copy.deepcopy(receipts or []), copy.deepcopy(fills or [])
    return execution.make_execution_request(
        config=cfg, proposal=prop, portfolio=balance, receipts=rows, fills=facts,
        mode="PYRAMID", pyramid_state=state, port_state=initial_ports,
        source=source,
        source_provenance={"producer": "TEST_ONLY_PYRAMID_PORT_FIXTURE",
                           "source_id": "TEST_ONLY_PYRAMID_EVENT_TABLE",
                           "source_hash": execution.digest({"receipts": rows, "fills": facts}),
                           "quality": execution.SOURCES[source], "synthetic": True,
                           "model_version": ("CW_DETERMINISTIC_EVENT_TABLE_V1"
                                             if source == "DECLARED_EVENT_TABLE" else None)})


def complete_pyramid_request(*, side="long", at_ms=1000000, trade_id=1,
                             opportunity_id="pyramid-1", symbol="BTC/USDT:USDT",
                             portfolio_state=None, port_state=None,
                             source="DECLARED_EVENT_TABLE"):
    """Explicit synthetic port table, frozen before negative tests alter it.

    The fixture records the REAL caller's requested hashes/ordering, then
    supplies predeclared synthetic observations for each supported port.
    No negative test asks this builder to repair a denied or missing receipt.
    This is not observed account data or a PostgreSQL authority proof.
    """
    cache_key = execution.digest({"source": execution.execution_source_fingerprint(),
        "side": side, "at": at_ms, "trade": trade_id, "opportunity": opportunity_id,
        "symbol": symbol, "portfolio": portfolio_state, "ports": port_state,
        "event_source": source})
    if cache_key in _COMPLETE_CACHE:
        return copy.deepcopy(_COMPLETE_CACHE[cache_key])
    cfg, prop, balance, state, ports = pyramid_inputs(
        side=side, at_ms=at_ms, trade_id=trade_id,
        opportunity_id=opportunity_id, symbol=symbol)
    if port_state is not None:
        ports = copy.deepcopy(port_state)
    wire = symbol.split(":")[0].replace("/", "")
    broker_side = "buy" if side == "long" else "sell"
    close_side = "sell" if side == "long" else "buy"
    rows = []
    for _ in range(80):
        request = make_pyramid_request(
            side=side, at_ms=at_ms, trade_id=trade_id, opportunity_id=opportunity_id,
            symbol=symbol, portfolio_state=portfolio_state, port_state=port_state,
            receipts=rows, fills=[], source=source)
        report = execution.replay_execution(request)
        missing = next((g for g in report["gaps"]
                        if g["reason_code"] == "RECEIPT_MISSING"), None)
        if missing is None:
            break
        event = next(e for e in report["reads"] + report["actions"]
                     if e["call_index"] == missing["call_index"])
        port = event["port"]
        if port in ("pyramid_authority", "pyramid_read", "pyramid_write", "set_leverage"):
            value = {"ok": True}
            if port == "pyramid_read" and event["args"][0]["kind"] == "ORIGINAL_SNAPSHOT_TIMEFRAME":
                value["timeframe"] = ports["trade"]["timeframe"]
        elif port == "quote":
            value = {"last": prop["entry"]}
        elif port == "positions":
            value = {"ok": True, "quality": "live", "positions": [
                {"symbol": wire, "side": broker_side, "size": ports["trade"]["qty"],
                 "leverage": 5, "position_side": "BOTH"}]}
        elif port == "algo_orders":
            value = {"ok": True, "orders": [
                {"symbol": wire, "algo_id": ports["trade"]["sl_order_id"],
                 "side": close_side, "type": "STOP_MARKET", "status": "NEW",
                 "close_position": True, "trigger_price": 100},
                {"symbol": wire, "algo_id": ports["trade"]["tp2_order_id"],
                 "side": close_side, "type": "TAKE_PROFIT_MARKET", "status": "NEW",
                 "close_position": True, "trigger_price": prop["tp2"]}]}
        elif port == "equity":
            value = {"ok": True, "quality": "live", "total_usd": 1000,
                     "available_usd": 800, "as_of_ms": at_ms}
        elif port == "depth":
            value = fixtures.depth()
            entry = prop["entry"]
            bid = entry - .1 if side == "long" else entry
            ask = entry if side == "long" else entry + .1
            value.update(symbol=wire, received_at_ms=at_ms, exchange_time_ms=at_ms,
                         message_time_ms=at_ms, bids=[[bid, 100]], asks=[[ask, 100]])
        elif port == "send_order":
            params = event["args"][2]
            value = {"ok": True, "result": {"orderId": f"pyr-{trade_id}",
                "clientOrderId": params["newClientOrderId"], "status": "FILLED",
                "executedQty": params["quantity"], "avgPrice": prop["entry"]}}
        else:
            raise AssertionError(f"Fixture has no declared observation for {port}")
        receipt = {key: event[key] for key in
                   ("port", "call_index", "after_action_index", "request_hash")}
        receipt.update(observed_at_ms=at_ms, available_at_ms=at_ms, value=value)
        rows.append(receipt)
    else:
        raise AssertionError("Synthetic port fixture exceeded the closed call budget")
    orders = [e for e in report["actions"] if e["port"] == "send_order"]
    if len(orders) != 1:
        raise AssertionError(f"Expected exactly one real-caller order: {report['gaps']}; "
                             f"last_reads={report['reads'][-3:]}; result={report['caller_result']}")
    params = orders[0]["args"][2]
    fill = fixtures.raw_fill(qty=params["quantity"], dispatch=params["newClientOrderId"],
                             order=f"pyr-{trade_id}", exec_id=f"pyr-fill-{trade_id}", role="PYRAMID")
    fill.update(observed_at_ms=at_ms, available_at_ms=at_ms,
                causal_index=orders[0]["call_index"])
    fill["binding"].update(symbol=symbol, side=side.upper(), opportunity_id=opportunity_id,
                           trade_id=trade_id)
    fill["raw"].update(symbol=wire, side=broker_side.upper(), price=prop["entry"], time=at_ms)
    request = make_pyramid_request(side=side, at_ms=at_ms, trade_id=trade_id,
        opportunity_id=opportunity_id, symbol=symbol, portfolio_state=portfolio_state,
        port_state=port_state, receipts=rows, fills=[fill], source=source)
    _COMPLETE_CACHE[cache_key] = copy.deepcopy(request)
    return request


def reseal_request(request):
    request = copy.deepcopy(request)
    request["source_fingerprint"] = execution.execution_source_fingerprint()
    request["source_provenance"]["source_hash"] = execution.digest(
        {"receipts": request["receipts"], "fills": request["fills"]})
    request["request_hash"] = execution.digest(
        {k: v for k, v in request.items() if k != "request_hash"})
    return request


class PortStateBoundaryTests(unittest.TestCase):
    def validate(self, state):
        _, prop, _, current, _ = pyramid_inputs()
        return execution.validate_pyramid_port_state(state, prop, current)

    def test_restart_memory_roundtrip_is_hash_stable_and_detached(self):
        _, _, _, _, state = pyramid_inputs()
        restored = self.validate(json.loads(json.dumps(state)))
        self.assertEqual(restored, state)
        restored["trade"]["notes"] = "mutated"
        self.assertEqual(state["trade"]["notes"], "nota original")

    def test_tampered_state_cannot_bypass_hash(self):
        _, _, _, _, state = pyramid_inputs()
        state["generation"] = 1
        with self.assertRaisesRegex(ValueError, "PORT_STATE_DRIFT"):
            self.validate(state)

    def test_literal_generation_boolean_is_rejected_even_resealed(self):
        _, _, _, _, state = pyramid_inputs()
        state["generation"] = True
        with self.assertRaisesRegex(ValueError, "INVALID_INTEGER"):
            self.validate(seal_port_state(state))

    def test_manual_source_is_not_silently_normalized_into_auto(self):
        _, _, _, _, state = pyramid_inputs()
        state["trade"]["source"] = "manual"
        self.assertEqual(self.validate(seal_port_state(state))["trade"]["source"], "manual")

    def test_known_zero_retained_risk_is_valid_but_missing_is_not(self):
        _, _, _, _, state = pyramid_inputs()
        self.assertEqual(self.validate(state)["retained_risk_usd"], 0)
        for value in (None, True, "0", float("nan"), float("inf"), -1):
            with self.subTest(value=repr(value)):
                invalid = copy.deepcopy(state)
                invalid["retained_risk_usd"] = value
                # JSON NaN is refused before a caller can consume it.
                with self.assertRaises(ValueError):
                    self.validate(seal_port_state(invalid))

    def test_quote_and_side_cannot_diverge_between_trade_memories(self):
        _, _, _, _, state = pyramid_inputs()
        for key, value in (("symbol", "BTC/USDC"), ("side", "short"), ("qty", 2)):
            invalid = copy.deepcopy(state)
            invalid["trade"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "TRADE_MEMORY_DIVERGENT"):
                self.validate(seal_port_state(invalid))

    def test_unknown_field_refused_without_silent_default(self):
        _, _, _, _, state = pyramid_inputs()
        state["approved"] = True
        with self.assertRaisesRegex(ValueError, "CLOSED_SCHEMA"):
            self.validate(seal_port_state(state))

    def test_duplicate_or_missing_target_portfolio_rows_refused(self):
        _, _, _, _, state = pyramid_inputs()
        duplicate = copy.deepcopy(state)
        duplicate["portfolio_rows"] *= 2
        with self.assertRaisesRegex(ValueError, "DUPLICATE_PORTFOLIO_ROW"):
            self.validate(seal_port_state(duplicate))
        state["portfolio_rows"] = []
        with self.assertRaisesRegex(ValueError, "TARGET_PORTFOLIO_MISSING"):
            self.validate(seal_port_state(state))

    def test_configuration_does_not_inherit_missing_limits_from_environment(self):
        cfg, _, _, _, _ = pyramid_inputs()
        cfg["pyramid_limits"].pop("depth_limit")
        with self.assertRaisesRegex(ValueError, "CLOSED_SCHEMA:pyramid_limits"):
            execution.validate_execution_config(cfg)

    def test_leverage_changed_in_restart_metadata_is_refused(self):
        _, prop, _, state, initial = pyramid_inputs()
        initial["trade"]["leverage"] = 6
        state["leverage"] = 6
        with self.assertRaisesRegex(ValueError, "LEVERAGE_CHANGED"):
            execution.validate_pyramid_port_state(seal_port_state(initial), prop, state)

    def test_receipt_table_is_not_operational_approval(self):
        req = make_pyramid_request()
        self.assertEqual(req["source"], "DECLARED_EVENT_TABLE")
        self.assertTrue(req["test_only"])
        self.assertFalse(req["real_study_allowed"])
        self.assertTrue(req["source_provenance"]["synthetic"])


class RealCallerPortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.long = complete_pyramid_request()
        cls.report = execution.replay_execution(cls.long)

    def run_changed(self, mutate, *, keep_fills=False):
        request = copy.deepcopy(self.long)
        if not keep_fills:
            request["fills"] = []
        mutate(request)
        return execution.replay_execution(reseal_request(request))

    def index_for(self, port, operation=None):
        return next(row["call_index"] for row in self.report["reads"] + self.report["actions"]
                    if row["port"] == port and
                    (operation is None or row["args"][0] == operation))

    @staticmethod
    def receipt_at(request, index):
        return next(row for row in request["receipts"] if row["call_index"] == index)

    def assert_no_post(self, report):
        self.assertEqual([a for a in report["actions"] if a["port"] == "send_order"], [])
        self.assertEqual(report["manager_updates"], [])
        self.assertEqual(report["memory_transitions"], [])

    def test_long_real_caller_and_guarded_writer_have_exact_effect_once(self):
        report = self.report
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["filled_qty"], 1)
        self.assertEqual(report["entry_basis"], 110)
        self.assertEqual(report["port_state"]["trade"]["qty"], 2)
        self.assertEqual(report["port_state"]["trade"]["entry_price"], 100)
        self.assertEqual(report["port_state"]["trade"]["pyramiding_level"], 1)
        self.assertTrue(report["port_state"]["trade"]["notes"].startswith("nota original | pyramid L1"))
        self.assertEqual(len(report["memory_transitions"]), 1)
        self.assertEqual(report["port_state"]["intents"][0]["state"], "CONFIRMED")
        self.assertEqual(report["retained_confirmed_risk_usd"], 10)
        self.assertEqual(report["reservations"]["margin_usd"], 0)
        self.assertFalse(report["reservations"]["slot_retained"])
        frozen = report["port_state"]["intents"][0]["proposal"]
        self.assertIsNone(frozen["evidence"]["args"]["tp1"])
        self.assertEqual(frozen["evidence"]["limits"]["min_rr_tp1"], 0)
        self.assertEqual(frozen["evidence"]["limits"]["min_rr_tp2"],
                         self.long["config"]["limits"]["min_rr_tp2"])
        self.assertFalse(any(a["port"] == "place_protection" for a in report["actions"]))

    def test_short_real_caller_is_mirrored_without_rebracketing(self):
        report = execution.replay_execution(complete_pyramid_request(side="short"))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        order = next(a for a in report["actions"] if a["port"] == "send_order")
        self.assertEqual(order["args"][2]["side"], "SELL")
        self.assertEqual(order["args"][2]["type"], "MARKET")
        self.assertEqual(order["args"][2]["newClientOrderId"], "cw-pyr-1-1")
        self.assertFalse(any(a["port"] in ("place_protection", "cleanup", "cancel_order")
                             for a in report["actions"]))
        self.assertEqual(report["port_state"]["trade"]["entry_price"], 100)
        self.assertEqual(report["retained_confirmed_risk_usd"], 10)

    def test_initial_reservation_is_real_pyramid_size_not_generic_entry_admission(self):
        request = copy.deepcopy(self.long)
        # Upstream risk_pct controls the ordinary ENTRY sizing. A reinforcement
        # instead comes from original qty_initial × frozen PYRAMIDING_SIZE_MULT.
        request["proposal"]["risk_pct"] = .05
        report = execution.replay_execution(reseal_request(request))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        initial = report["initial_reservation"]
        self.assertEqual(initial["qty"], 1)
        self.assertEqual(initial["risk_usd"], 10)
        self.assertFalse(initial["slot_retained"])
        self.assertLess(report["admissions"][0]["final_qty"], initial["qty"])
        self.assertEqual(initial["reservation_hash"], execution.digest(
            {key: value for key, value in initial.items() if key != "reservation_hash"}))

    def test_receipts_do_not_import_exchange_database_or_authority(self):
        report = self.report
        self.assertEqual(report["execution_quality"], "DECLARED_MODEL")
        self.assertEqual(report["persistence_scope"], "RECEIPT_BOUND_SIMULATED_PORTS")
        self.assertEqual(report["isolation"]["database"], "memory_only")
        self.assertFalse(report["authorizes_live"])
        self.assertFalse(report["promotable"])
        self.assertFalse(report["real_study_allowed"])
        self.assertEqual(report["blocked_network_attempts"], [])
        self.assertIn("shadow_trade_service._maybe_pyramid", report["caller_calls"])
        self.assertIn("shadow_trade_service._market_entry_preflight_checks", report["caller_calls"])
        self.assertIn("shadow_trade_service._sync_final_check_for", report["caller_calls"])
        self.assertIn("binance_signed_service._place_post_fill_protection", report["caller_calls"])
        no_obligation = report["caller_identities"]["binance_signed_service._place_post_fill_protection"]
        self.assertEqual(no_obligation["source_file"],
                         str(BACKEND / "services/binance_signed_service.py"))
        self.assertEqual(report["source_fingerprint"], execution.execution_source_fingerprint())
        for name in ("_maybe_pyramid", "_pyramid_protection", "_pyramid_original_timeframe",
                     "_market_entry_preflight_checks", "_sync_final_check_for"):
            identity = report["caller_identities"][f"shadow_trade_service.{name}"]
            self.assertEqual(identity["source_file"], str(BACKEND / "services/shadow_trade_service.py"))
            self.assertGreater(identity["first_line"], 0)

    def test_every_authority_receipt_denial_blocks_before_any_post(self):
        for operation, reason in (("RISK_STATE", "RISK_UNKNOWN"),
                                  ("KILL_SWITCH", "KILL_DENIED"),
                                  ("OWNERSHIP", "MANUAL_POSITION_SYMBOL_BLOCKED"),
                                  ("RESERVE", "RESERVE_DENIED"),
                                  ("PREPARE", "LEASE_EXPIRED"),
                                  ("REGISTER_DISPATCH", "GENERATION_CHANGED"),
                                  ("READMIT", "MARGIN_UNKNOWN"),
                                  ("FINANCIAL_TOTAL", "FUNDING_UNAVAILABLE"),
                                  ("AUTHORIZE", "AUTHORITY_REVOKED")):
            with self.subTest(operation=operation):
                index = self.index_for("pyramid_authority", operation)
                report = self.run_changed(lambda req: self.receipt_at(req, index).update(
                    value={"ok": False, "reason_code": reason}))
                self.assert_no_post(report)
                self.assertIn(reason, [g["reason_code"] for g in report["gaps"]])

    def test_authority_generation_cannot_be_advanced_by_receipt(self):
        index = self.index_for("pyramid_authority", "AUTHORIZE")
        report = self.run_changed(lambda req: self.receipt_at(req, index).update(
            value={"ok": True, "generation": 999}))
        self.assert_no_post(report)
        self.assertIn("PYRAMID_AUTHORITY_GENERATION_MISMATCH", [g["reason_code"] for g in report["gaps"]])

    def test_boolean_generation_cannot_equal_current_integer_epoch(self):
        index = self.index_for("pyramid_authority", "PREPARE")
        report = self.run_changed(lambda req: self.receipt_at(req, index).update(
            value={"ok": True, "generation": True}))
        self.assert_no_post(report)
        self.assertIn("PYRAMID_AUTHORITY_GENERATION_MISMATCH", [g["reason_code"] for g in report["gaps"]])

    def test_missing_and_unbound_receipts_never_become_implicit_success(self):
        index = self.index_for("pyramid_authority", "AUTHORIZE")
        for missing in (True, False):
            with self.subTest(missing=missing):
                def mutate(req):
                    if missing:
                        req["receipts"] = [row for row in req["receipts"] if row["call_index"] != index]
                    else:
                        self.receipt_at(req, index)["request_hash"] = "f" * 64
                report = self.run_changed(mutate)
                self.assert_no_post(report)
                expected = "RECEIPT_MISSING" if missing else "RECEIPT_ACTION_BINDING_MISMATCH"
                self.assertIn(expected, [g["reason_code"] for g in report["gaps"]])

    def test_stale_market_book_and_unknown_position_block(self):
        for port in ("depth", "positions"):
            index = self.index_for(port)
            def mutate(req):
                value = self.receipt_at(req, index)["value"]
                if port == "depth":
                    value.update(received_at_ms=997000, exchange_time_ms=997000, message_time_ms=997000)
                else:
                    value.update(ok=False, quality="UNKNOWN")
            with self.subTest(port=port):
                report = self.run_changed(mutate)
                self.assert_no_post(report)

    def test_missing_existing_sl_or_wrong_side_never_rebrackets(self):
        index = self.index_for("algo_orders")
        for wrong_side in (True, False):
            def mutate(req):
                orders = self.receipt_at(req, index)["value"]["orders"]
                if wrong_side:
                    orders[0]["side"] = "buy"
                else:
                    orders.pop(0)
            with self.subTest(wrong_side=wrong_side):
                report = self.run_changed(mutate)
                self.assert_no_post(report)
                self.assertFalse(any(a["port"] == "place_protection" for a in report["actions"]))

    def test_original_snapshot_timeframe_missing_or_divergent_blocks(self):
        index = next(row["call_index"] for row in self.report["reads"]
                     if row["port"] == "pyramid_read" and
                     row["args"][0]["kind"] == "ORIGINAL_SNAPSHOT_TIMEFRAME")
        for timeframe in (None, "15m"):
            with self.subTest(timeframe=timeframe):
                report = self.run_changed(lambda req: self.receipt_at(req, index).update(
                    value={"ok": True, "timeframe": timeframe}))
                self.assert_no_post(report)

    def test_original_recommendation_missing_never_uses_new_signal_timeframe(self):
        # Rebind only the already-declared clean prefix, before the original
        # snapshot read. Reusing old state hashes would fail at the first
        # authority receipt and would NOT prove the missing-FK branch.
        request = copy.deepcopy(self.long)
        before = request["port_state"]["state_hash"]
        request["port_state"]["trade"]["recommendation_id"] = None
        request["port_state"] = seal_port_state(request["port_state"])
        after = request["port_state"]["state_hash"]
        original_read = next(row["call_index"] for row in self.report["reads"]
                             if row["port"] == "pyramid_read" and
                             row["args"][0]["kind"] == "ORIGINAL_SNAPSHOT_TIMEFRAME")
        events = {row["call_index"]: row for row in self.report["reads"] + self.report["actions"]}
        prefix = []
        for receipt in request["receipts"]:
            if receipt["call_index"] >= original_read:
                continue
            event = events[receipt["call_index"]]
            args = json.loads(json.dumps(event["args"]).replace(before, after))
            kwargs = json.loads(json.dumps(event["kwargs"]).replace(before, after))
            receipt["request_hash"] = execution.call_hash(event["port"], args, kwargs)
            prefix.append(receipt)
        request["receipts"], request["fills"] = prefix, []
        report = execution.replay_execution(reseal_request(request))
        self.assert_no_post(report)
        self.assertEqual(report["status"], "NO_OP", report["gaps"])
        self.assertEqual(report["gaps"], [])
        self.assertFalse(any(row["port"] == "pyramid_authority" and
                             row["args"][0] == "RESERVE" for row in report["reads"]))
        self.assertFalse(any(row["port"] == "pyramid_read" and
                             row["args"][0]["kind"] == "ORIGINAL_SNAPSHOT_TIMEFRAME"
                             for row in report["reads"]))

    def test_original_timeframe_read_is_bound_to_fk_not_runtime_trade_attribute(self):
        read = next(row for row in self.report["reads"]
                    if row["port"] == "pyramid_read" and
                    row["args"][0]["kind"] == "ORIGINAL_SNAPSHOT_TIMEFRAME")
        self.assertEqual(list(read["args"][0]["parameters"].values()),
                         [self.long["port_state"]["trade"]["recommendation_id"]])
        self.assertNotEqual(self.long["port_state"]["trade"]["recommendation_id"],
                            self.long["proposal"]["trade_id"])
        self.assertEqual(self.report["port_state"]["intents"][0]["identity"]["timeframe"], "1h")

    def test_tp2_reward_gate_is_still_required_after_tp1_was_realized(self):
        report = self.run_changed(lambda req: req["config"]["limits"].update(min_rr_tp2=100))
        self.assert_no_post(report)

    def test_manual_or_blocked_memory_is_not_disguised_as_bot(self):
        for manual in (True, False):
            def mutate(req):
                state = req["port_state"]
                if manual:
                    state["trade"]["source"] = "manual"
                else:
                    state["blocked"] = True
                req["port_state"] = seal_port_state(state)
            with self.subTest(manual=manual):
                self.assert_no_post(self.run_changed(mutate))

    def test_low_free_margin_and_daily_risk_gate_are_not_bypassed(self):
        for field, value in (("available_margin_usd", 1), ("daily_base_usd", -39),
                             ("open_risk_usd", 49)):
            with self.subTest(field=field):
                report = self.run_changed(lambda req: req["portfolio"].update({field: value}))
                self.assert_no_post(report)

    def test_response_missing_fill_does_not_write_planned_quantity(self):
        index = self.index_for("send_order")
        def mutate(req):
            self.receipt_at(req, index)["value"]["result"].pop("executedQty")
        report = self.run_changed(mutate)
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(report["memory_transitions"], [])
        self.assertEqual(report["manager_updates"], [])
        self.assertEqual(report["port_state"]["trade"]["qty"], 1)
        self.assertTrue(report["port_state"]["blocked"])

    def test_writer_failure_retains_unknown_exposure_and_no_added_trade_qty(self):
        index = self.index_for("pyramid_write", "TRADE_UPDATE")
        report = self.run_changed(lambda req: self.receipt_at(req, index).update(value={"ok": False}),
                                  keep_fills=True)
        self.assertEqual(report["status"], "UNKNOWN", report["gaps"])
        self.assertEqual(report["port_state"]["trade"]["qty"], 1)
        self.assertEqual(report["port_state"]["trade"]["pyramiding_level"], 0)
        self.assertEqual(report["memory_transitions"], [])
        self.assertEqual(report["port_state"]["intents"][0]["state"], "UNKNOWN")
        self.assertTrue(report["port_state"]["blocked"])
        self.assertGreater(report["reservations"]["risk_usd"], 0)
        self.assertGreater(report["reservations"]["margin_usd"], 0)

    def test_restart_confirmed_level_cannot_repeat_post_or_economic_generation(self):
        state = json.loads(json.dumps(self.report["port_state"]))
        _, _, balance, _, _ = pyramid_inputs()
        balance.update(open_notional_usd=200, open_risk_usd=10)
        req = make_pyramid_request(port_state=state, portfolio_state=balance)
        report = execution.replay_execution(req)
        self.assert_no_post(report)
        self.assertEqual(report["port_state"]["generation"], state["generation"])
        self.assertEqual(report["port_state"]["trade"]["qty"], 2)
        self.assertEqual(report["retained_confirmed_risk_usd"], 10)

    def test_same_cold_request_has_identical_state_and_report_after_restart(self):
        again = execution.replay_execution(copy.deepcopy(self.long))
        self.assertEqual(again["report_hash"], self.report["report_hash"])
        self.assertEqual(again["port_state"], self.report["port_state"])
        self.assertEqual(len(again["actions"]), len(self.report["actions"]))


if __name__ == "__main__":
    unittest.main()
