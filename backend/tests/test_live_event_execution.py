"""Negative boundaries first, followed by bound TEST_ONLY real-caller controls."""
import copy
import importlib.util
from pathlib import Path
import unittest

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("event_exec", BACKEND / "services/live_event_execution_service.py")
execution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(execution)


def config(maker=False, pyramid=False):
    return {"MAKER_ENTRY_ENABLED": maker, "MAKER_FALLBACK_MARKET": False,
            "P04A_ENTRY_REVALIDATION_ENABLED": True, "P04B_MARKET_REVALIDATION_ENABLED": True,
            "P04B_MAKER_FALLBACK_ENABLED": False, "PYRAMIDING_ENABLED": pyramid,
            "MAX_OPEN_POSITIONS": 5, "PYRAMIDING_MAX_LEVEL": 1,
            "BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS": 2,
            "MIN_NOTIONAL_USD": 50, "MAX_MARGIN_PCT_PER_TRADE": 15,
            "MAX_RISK_PCT_HARD": 2, "MAX_TOTAL_NOTIONAL_PCT": 150,
            "LIVE_SIZE_MULT": 1, "MAX_OPEN_RISK_PCT": 5,
            "DAILY_LOSS_LIMIT_USD": 40, "MARGIN_MAX_AGE_S": 30,
            "MAKER_ENTRY_TIMEOUT_S": 0, "MAKER_ENTRY_POLL_INTERVAL_S": .8,
            "BINANCE_MAKER_CANCEL_CONFIRM_DELAY": .25,
            "PYRAMIDING_SIZE_MULT": .5, "PYRAMIDING_MIN_PROFIT_R": 1,
            "TP1_QTY_PCT": .45,
            "filters": {"step": .001, "min_qty": .001, "tick": .01, "market_step": .001,
                        "market_min_qty": .001, "market_max_qty": 10000, "min_notional": 5},
            "limits": {"max_quote_age_ms": 1000, "max_depth_age_ms": 1000,
                       "max_fetch_latency_ms": 500, "max_spread_pct": .25,
                       "max_book_impact_pct": .5, "max_adverse_slippage_pct": 1,
                       "max_chase_atr": 1, "min_rr_tp1": .5, "min_rr_tp2": 1}}


def proposal():
    return {"opportunity_id": "opp-1", "trade_id": 1, "account_ref": "TEST_ONLY_ACCOUNT",
            "exchange": "binance", "market": "usdm_futures", "symbol": "BTC/USDT",
            "quote": "USDT", "side": "long", "position_side": "BOTH", "timeframe": "1h",
            "dispatch_id": "cw-test-entry-1", "decision_at_ms": 1000000,
            "entry": 100, "stop": 95, "tp1": 110, "tp2": 120, "leverage": 5,
            "risk_pct": 1, "atr": 2, "entry_zone_low": None, "entry_zone_high": None,
            "sizing_context": {"stage": "FINAL_LIVE_PRE_COMPUTE_QTY", "producer": "TEST_ONLY_FIXTURE",
                               "source_hash": "a" * 64, "observed_at_ms": 1000000,
                               "available_at_ms": 1000000}}


def portfolio():
    return {"equity_usd": 1000, "available_margin_usd": 800, "margin_as_of_ms": 1000000,
            "open_positions": 0, "open_risk_usd": 0, "open_notional_usd": 0,
            "pending_count": 0, "pending_risk_usd": 0, "pending_margin_usd": 0,
            "daily_base_usd": 0, "complete": True, "ownership": "BOT", "paused": False,
            "kill_allowed": True, "live_guards_allowed": True}


def quote():
    return {"ok": True, "exchange": "binance", "symbol": "BTCUSDT", "bid": 100, "ask": 100.1,
            "bid_qty": 100, "ask_qty": 100, "received_at_ms": 1000000,
            "exchange_time_ms": 1000000, "latency_ms": 0}


def depth():
    return {"ok": True, "exchange": "binance", "symbol": "BTCUSDT",
            "bids": [[99.9, 100]], "asks": [[100, 100]], "received_at_ms": 1000000,
            "exchange_time_ms": 1000000, "message_time_ms": 1000000,
            "latency_ms": 0, "last_update_id": 1}


def protection():
    return {"sl_ok": True, "sl_order_id": "sl-1", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": "tp1-1", "tp1_msg": None,
            "tp1_qty": .9, "tp1_skipped": False, "tp2_ok": True,
            "tp2_order_id": "tp2-1", "tp2_msg": None,
            "tp1_requested": True, "tp2_requested": True}


def receipt(port, index, after, args, value, kwargs=None, stamp=1000000):
    return {"port": port, "call_index": index, "after_action_index": after,
            "request_hash": execution.call_hash(port, args, kwargs), "observed_at_ms": stamp,
            "available_at_ms": stamp, "value": value}


def raw_fill(qty=2, dispatch="cw-test-entry-1", order="entry-1", exec_id="fill-1", role="ENTRY"):
    return {"event_id": exec_id, "causal_index": 3, "observed_at_ms": 1000000,
            "available_at_ms": 1000000, "role": role,
            "binding": {"account_scope": "TEST_ONLY_ACCOUNT", "exchange": "binance", "market": "usdm_futures",
                        "symbol": "BTC/USDT", "quote": "USDT", "side": "LONG", "position_side": "BOTH",
                        "trade_id": 1, "opportunity_id": "opp-1", "dispatch_id": dispatch, "order_id": order},
            "raw": {"id": exec_id, "orderId": order, "symbol": "BTCUSDT", "positionSide": "BOTH",
                    "side": "BUY", "price": 100, "qty": qty, "realizedPnl": 0,
                    "commission": 0, "commissionAsset": "USDT", "time": 1000000}}


def market_receipts(dispatch="cw-test-entry-1", start=1, after=0, with_leverage=True):
    rows = []
    if with_leverage:
        rows.append(receipt("set_leverage", start, after, ("BTCUSDT", 5), {"ok": True}))
        after, start = start, start + 1
    rows.append(receipt("depth", start, after, ("BTC/USDT", 2.0), depth()))
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 2.0,
              "newOrderRespType": "RESULT", "newClientOrderId": dispatch}
    rows.append(receipt("send_order", start + 1, after, ("POST", "/fapi/v1/order", params),
                        {"ok": True, "result": {"orderId": "entry-1", "status": "FILLED", "executedQty": 2, "avgPrice": 100}}))
    rows.append(receipt("place_protection", start + 2, start + 1, ("BTCUSDT", "BUY", 2.0), protection(),
                        {"stop_loss": 95, "tp1": 110, "tp2": 120, "tp1_qty_pct": .45,
                         "client_order_id_prefix": dispatch}))
    return rows


def maker_receipts(response=None):
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": 2.0,
              "price": 100.0, "timeInForce": "GTX", "newClientOrderId": "cw-test-entry-1"}
    return [receipt("set_leverage", 1, 0, ("BTCUSDT", 5), {"ok": True}),
            receipt("quote", 2, 1, ("BTC/USDT",), quote()),
            receipt("send_order", 3, 1, ("POST", "/fapi/v1/order", params), response or
                    {"ok": True, "result": {"orderId": "entry-1", "status": "FILLED", "executedQty": 2, "avgPrice": 100}})]


def request(rows=None, *, maker=False, fills=None, **changes):
    args = {"config": config(maker), "proposal": proposal(), "portfolio": portfolio(),
            "receipts": market_receipts() if rows is None else rows,
            "fills": [raw_fill()] if fills is None else fills}
    args.update(changes)
    return execution.make_execution_request(**args)


def complete_entry_request(*, opportunity_id="opp-1", trade_id=1, symbol="BTC/USDT", side="long",
                           at_ms=1000000, dispatch_id="cw-test-entry-1", portfolio_state=None):
    """Reusable synthetic bound MARKET fixture; no mocks, preview, network or DB.

    Prices/equity are explicitly declared TEST_ONLY facts, not production
    estimates. Root integration may replace these inputs before hash freezing.
    """
    cfg, prop, balance = config(), proposal(), portfolio()
    prop.update(opportunity_id=opportunity_id, trade_id=trade_id, symbol=symbol,
                side=side, decision_at_ms=at_ms, dispatch_id=dispatch_id)
    if side == "short":
        prop.update(stop=105, tp1=90, tp2=80)
    prop["sizing_context"].update(observed_at_ms=at_ms, available_at_ms=at_ms)
    balance.update(margin_as_of_ms=at_ms)
    if portfolio_state is not None:
        balance = copy.deepcopy(portfolio_state)
    qty = execution.compute_admission(cfg, prop, balance)["final_qty"]
    wire = symbol.split(":")[0].replace("/", "")
    book = depth()
    book.update(symbol=wire, received_at_ms=at_ms, exchange_time_ms=at_ms, message_time_ms=at_ms)
    if side == "short":
        book.update(bids=[[100, 100]], asks=[[100.1, 100]])
    binance_side = "BUY" if side == "long" else "SELL"
    params = {"symbol": wire, "side": binance_side, "type": "MARKET", "quantity": qty,
              "newOrderRespType": "RESULT", "newClientOrderId": dispatch_id}
    oid = f"entry-{trade_id}"
    prot = protection()
    prot.update(sl_order_id=f"sl-{trade_id}", tp1_order_id=f"tp1-{trade_id}",
                tp2_order_id=f"tp2-{trade_id}", tp1_qty=qty * cfg["TP1_QTY_PCT"])
    rows = [receipt("set_leverage", 1, 0, (wire, 5), {"ok": True}, stamp=at_ms),
            receipt("depth", 2, 1, (symbol, qty), book, stamp=at_ms),
            receipt("send_order", 3, 1, ("POST", "/fapi/v1/order", params),
                    {"ok": True, "result": {"orderId": oid, "status": "FILLED", "executedQty": qty, "avgPrice": 100}}, stamp=at_ms),
            receipt("place_protection", 4, 3, (wire, binance_side, qty), prot,
                    {"stop_loss": prop["stop"], "tp1": prop["tp1"], "tp2": prop["tp2"],
                     "tp1_qty_pct": .45, "client_order_id_prefix": dispatch_id}, stamp=at_ms)]
    fill = raw_fill(qty=qty, dispatch=dispatch_id, order=oid, exec_id=f"fill-{trade_id}")
    fill.update(observed_at_ms=at_ms, available_at_ms=at_ms)
    fill["binding"].update(symbol=symbol, side=side.upper(), opportunity_id=opportunity_id, trade_id=trade_id)
    fill["raw"].update(symbol=wire, side=binance_side, time=at_ms)
    return execution.make_execution_request(config=cfg, proposal=prop, portfolio=balance, receipts=rows, fills=[fill])


class NegativeBoundaries(unittest.TestCase):
    def test_explicit_credentials_never_reach_child(self):
        rows = market_receipts()
        rows[0]["value"]["api_key"] = "TEST_ONLY_SECRET_SENTINEL"
        with self.assertRaisesRegex(ValueError, "CREDENTIAL_FIELD_FORBIDDEN"):
            request(rows)

    def test_missing_configuration_never_fills_defaults(self):
        cfg = config()
        cfg.pop("LIVE_SIZE_MULT")
        with self.assertRaisesRegex(ValueError, "CLOSED_SCHEMA:config"):
            request(config=cfg)

    def test_unknown_source_and_extra_field_refused(self):
        req = request()
        req["source"] = "OBSERVED_ACCOUNT"
        req["request_hash"] = execution.digest({k: v for k, v in req.items() if k != "request_hash"})
        with self.assertRaisesRegex(ValueError, "SOURCE_OR_TEST_MARKER_INVALID"):
            execution.replay_execution(req)
        req = request()
        req["approved"] = True
        with self.assertRaisesRegex(ValueError, "CLOSED_SCHEMA:request"):
            execution.replay_execution(req)

    def test_source_fingerprint_cannot_be_rehashed_to_pass(self):
        req = request()
        req["source_fingerprint"] = "f" * 64
        req["request_hash"] = execution.digest({k: v for k, v in req.items() if k != "request_hash"})
        with self.assertRaisesRegex(ValueError, "SOURCE_DRIFT"):
            execution.replay_execution(req)

    def test_event_source_hash_is_recomputed_from_consumed_content(self):
        req = request()
        req["source_provenance"]["source_hash"] = "f" * 64
        req["request_hash"] = execution.digest({k: v for k, v in req.items() if k != "request_hash"})
        with self.assertRaisesRegex(ValueError, "EVENT_SOURCE_CONTENT_DRIFT"):
            execution.replay_execution(req)

    def test_filled_ack_without_attributed_fill_stays_unknown_reserved(self):
        report = execution.replay_execution(request(fills=[]))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(report["reservations"]["margin_usd"], 40)
        self.assertTrue(report["reservations"]["slot_retained"])
        self.assertEqual(report["manager_updates"], [])

    def test_wrong_action_receipt_is_not_consumed(self):
        rows = market_receipts()
        rows[2]["request_hash"] = "f" * 64
        report = execution.replay_execution(request(rows, fills=[]))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("RECEIPT_ACTION_BINDING_MISMATCH", [g["reason_code"] for g in report["gaps"]])

    def test_stale_after_action_receipt_retains_reservation(self):
        rows = market_receipts()
        rows[1].update(observed_at_ms=999999, available_at_ms=999999)
        report = execution.replay_execution(request(rows, fills=[]))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("RECEIPT_STALE_OR_FUTURE", [g["reason_code"] for g in report["gaps"]])
        self.assertEqual(report["reservations"]["margin_usd"], 40)

    def test_same_exec_id_material_conflict_is_not_double_counted(self):
        other = raw_fill()
        other["raw"]["commission"] = .2
        report = execution.replay_execution(request(fills=[raw_fill(), other]))
        self.assertEqual(report["status"], "CONFLICT")
        self.assertEqual(report["filled_qty"], 2)
        self.assertIn("FILL_ID_CONFLICT", [g["reason_code"] for g in report["gaps"]])

    def test_future_fill_does_not_materialize_capital_earlier(self):
        fill = raw_fill()
        fill.update(observed_at_ms=1000001, available_at_ms=1000001)
        report = execution.replay_execution(request(fills=[fill]))
        self.assertEqual(report["status"], "CONFLICT")
        self.assertEqual(report["manager_updates"], [])

    def test_ack_average_cannot_replace_different_fill_price(self):
        fill = raw_fill()
        fill["raw"]["price"] = 99
        report = execution.replay_execution(request(fills=[fill]))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("FILL_PRICE_RECEIPT_DIVERGENT", [g["reason_code"] for g in report["gaps"]])
        self.assertEqual(report["manager_updates"], [])

    def test_raw_filled_missing_qty_never_uses_planned_as_economic_truth(self):
        rows = maker_receipts({"ok": True, "result": {"orderId": "entry-1", "status": "FILLED", "avgPrice": 100}})
        rows.append(receipt("place_protection", 4, 3, ("BTCUSDT", "BUY", 2.0), protection(),
                            {"stop_loss": 95, "tp1": 110, "tp2": 120, "tp1_qty_pct": .45,
                             "client_order_id_prefix": "cw-test-entry-1", "dedup_live": True}))
        report = execution.replay_execution(request(rows, maker=True))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("RAW_FILL_FIELDS_MISSING", [g["reason_code"] for g in report["gaps"]])
        self.assertEqual(report["manager_updates"], [])

    def test_literal_bool_fee_and_qty_are_not_zero_or_size(self):
        fill = raw_fill()
        fill["raw"]["qty"] = True
        report = execution.replay_execution(request(fills=[fill]))
        self.assertEqual(report["status"], "CONFLICT")
        self.assertEqual(report["filled_qty"], 0)

    def test_final_quantity_cannot_exceed_planned_admission(self):
        report = execution.compute_admission(config(), proposal(), portfolio(), final_qty=3)
        self.assertEqual(report["decision"], "REJECTED")
        self.assertIn("FINAL_QTY_EXCEEDS_PLANNED", report["reason_codes"])

    def test_another_opportunity_cannot_supply_fill(self):
        fill = raw_fill()
        fill["binding"]["opportunity_id"] = "opp-other"
        report = execution.replay_execution(request(fills=[fill]))
        self.assertEqual(report["status"], "CONFLICT")
        self.assertEqual(report["filled_qty"], 0)

    def test_quantity_above_dispatch_conflicts(self):
        report = execution.replay_execution(request(fills=[raw_fill(qty=3)]))
        self.assertEqual(report["status"], "CONFLICT")
        self.assertIn("FILL_QTY_EXCEEDS_DISPATCH", [g["reason_code"] for g in report["gaps"]])

    def test_maker_unknown_does_not_make_duplicate_market(self):
        cfg = config(maker=True)
        cfg.update(MAKER_FALLBACK_MARKET=True, P04B_MAKER_FALLBACK_ENABLED=True)
        rows = maker_receipts({"ok": False, "error": "timeout", "_request_sent": True})
        report = execution.replay_execution(request(rows, maker=True, config=cfg, fills=[]))
        self.assertEqual(report["status"], "UNKNOWN")
        orders = [a for a in report["actions"] if a["port"] == "send_order"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["args"][2]["type"], "LIMIT")
        self.assertEqual(report["reservations"]["margin_usd"], 40)

    def test_live_pyramid_on_requires_official_event_ports(self):
        state = {"id": 1, "symbol": "BTC/USDT", "side": "long", "qty": 1, "qty_initial": 2,
                 "entry_price": 100, "planned_stop": 95, "leverage": 5, "phase": "post_tp1",
                 "pyramiding_level": 0, "notes": None, "sl_close_position": True,
                 "tp2_close_position": True, "protection_confirmed": True}
        rows = [receipt("quote", 1, 0, ("BTC/USDT",), {"last": 110})]
        report = execution.replay_execution(request(rows, config=config(pyramid=True), fills=[],
                                                    mode="PYRAMID", pyramid_state=state))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("TECHNICAL_PYRAMID_EVENT_PORTS_NOT_IMPLEMENTED", [g["reason_code"] for g in report["gaps"]])
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["memory_transitions"], [])


class PositiveControls(unittest.TestCase):
    def test_reusable_short_fixture_actual_caller(self):
        report = execution.replay_execution(complete_entry_request(side="short", opportunity_id="short-1", trade_id=2,
                                                                  dispatch_id="cw-test-short", at_ms=2000000))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["fills"][0]["raw"]["side"], "SELL")
        self.assertEqual(report["admission"]["risk_usd"], 10)

    def test_terminal_maker_zero_fill_is_known_release(self):
        rows = maker_receipts({"ok": True, "result": {"orderId": "entry-1", "status": "NEW", "executedQty": 0, "avgPrice": 0}})
        rows += [receipt("cancel_order", 4, 3, ("BTCUSDT",),
                         {"ok": True, "result": {"status": "CANCELED", "executedQty": 0, "avgPrice": 0}},
                         {"order_id": "entry-1"}),
                 receipt("get_order", 5, 4, ("BTCUSDT",),
                         {"ok": True, "status": "CANCELED", "executed_qty": 0, "avg_fill_price": 0,
                          "raw": {"executedQty": 0}}, {"order_id": "entry-1"})]
        report = execution.replay_execution(request(rows, maker=True, fills=[]))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertTrue(report["caller_result"]["no_fill"])
        self.assertEqual(report["reservations"]["state"], "RELEASED")
        self.assertEqual(report["manager_updates"], [])

    def test_declared_event_table_has_distinct_quality_no_authority(self):
        args = request()
        args.pop("request_hash")
        args["source"] = "DECLARED_EVENT_TABLE"
        args["test_only"] = True
        args["source_provenance"] = {"producer": "TEST_ONLY_MODEL_FIXTURE", "source_id": "TEST_ONLY_TABLE",
                                     "source_hash": execution.digest({"receipts": args["receipts"], "fills": args["fills"]}),
                                     "quality": "DECLARED_MODEL", "model_version": "CW_DETERMINISTIC_EVENT_TABLE_V1",
                                     "synthetic": True}
        report = execution.replay_execution({**args, "request_hash": execution.digest(args)})
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["execution_quality"], "DECLARED_MODEL")
        self.assertFalse(report["authorizes_live"])
        self.assertFalse(report["real_study_allowed"])

    def test_market_exact_caller_final_admission_and_economics_facts(self):
        report = execution.replay_execution(request())
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["admission"]["final_qty"], 2)
        self.assertEqual(report["admission"]["margin_usd"], 40)
        self.assertEqual(report["admission"]["risk_usd"], 10)
        self.assertEqual(report["admitted_orders"], [{"order_id": "entry-1", "dispatch_id": "cw-test-entry-1", "role": "ENTRY"}])
        self.assertEqual(report["filled_qty"], 2)
        self.assertEqual(report["entry_basis"], 100)
        self.assertEqual(report["reservations"]["state"], "TRANSFERRED_TO_POSITION")
        self.assertEqual(report["manager_updates"][0]["manager_qty"], 2)
        self.assertFalse(report["authorizes_live"])
        self.assertFalse(report["real_study_allowed"])
        self.assertEqual(report["blocked_network_attempts"], [])

    def test_maker_filled_is_limit_gtx_not_market(self):
        rows = maker_receipts()
        rows.append(receipt("place_protection", 4, 3, ("BTCUSDT", "BUY", 2.0), protection(),
                            {"stop_loss": 95, "tp1": 110, "tp2": 120, "tp1_qty_pct": .45,
                             "client_order_id_prefix": "cw-test-entry-1", "dedup_live": True}))
        report = execution.replay_execution(request(rows, maker=True))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        order = [a for a in report["actions"] if a["port"] == "send_order"][0]
        self.assertEqual(order["args"][2]["timeInForce"], "GTX")
        self.assertTrue(report["caller_result"]["was_maker"])

    def test_maker_partial_cancel_uses_only_fill_no_fallback(self):
        rows = maker_receipts({"ok": True, "result": {"orderId": "entry-1", "status": "NEW", "executedQty": 1, "avgPrice": 100}})
        rows += [receipt("cancel_order", 4, 3, ("BTCUSDT",),
                         {"ok": True, "result": {"status": "CANCELED", "executedQty": 1, "avgPrice": 100}},
                         {"order_id": "entry-1"}),
                 receipt("get_order", 5, 4, ("BTCUSDT",),
                         {"ok": True, "status": "CANCELED", "executed_qty": 1, "avg_fill_price": 100,
                          "raw": {"executedQty": 1}}, {"order_id": "entry-1"}),
                 receipt("place_protection", 6, 4, ("BTCUSDT", "BUY", 1.0), protection(),
                         {"stop_loss": 95, "tp1": 110, "tp2": 120, "tp1_qty_pct": .45,
                          "client_order_id_prefix": "cw-test-entry-1", "dedup_live": True})]
        report = execution.replay_execution(request(rows, maker=True, fills=[raw_fill(qty=1)]))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["filled_qty"], 1)
        self.assertEqual(report["caller_result"]["executed_qty"], 1)
        self.assertEqual(len([a for a in report["actions"] if a["port"] == "send_order"]), 1)

    def test_fallback_owns_mfb_proposal_and_dispatch(self):
        cfg = config(maker=True)
        cfg.update(MAKER_FALLBACK_MARKET=True, P04B_MAKER_FALLBACK_ENABLED=True)
        rows = maker_receipts({"ok": False, "code": -5022, "error": "post only order will be rejected"})
        child = "cw-test-entry-1-mfb"
        rows += market_receipts(child, start=4, after=3, with_leverage=False)
        report = execution.replay_execution(request(rows, maker=True, config=cfg,
                                                    fills=[raw_fill(dispatch=child)]))
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["admitted_orders"][0]["dispatch_id"], child)
        self.assertTrue(report["caller_result"]["fell_back_to_market"])
        self.assertNotEqual(report["dispatch_proposals"][0]["proposal_hash"], report["dispatch_proposals"][-1]["proposal_hash"])

    def test_aa_reapply_is_deterministic_and_dedupe_is_exact(self):
        req = request(fills=[raw_fill(), raw_fill()])
        first = execution.replay_execution(req)
        second = execution.replay_execution(req)
        self.assertEqual(first, second)
        self.assertEqual(first["filled_qty"], 2)
        self.assertEqual(len(first["fills"]), 1)

    def test_pyramid_off_actual_function_is_noop(self):
        state = {"id": 1, "symbol": "BTC/USDT", "side": "long", "qty": 1, "qty_initial": 2,
                 "entry_price": 100, "planned_stop": 95, "leverage": 5, "phase": "post_tp1",
                 "pyramiding_level": 0, "notes": None, "sl_close_position": True,
                 "tp2_close_position": True, "protection_confirmed": True}
        report = execution.replay_execution(request([], fills=[], mode="PYRAMID", pyramid_state=state))
        self.assertEqual(report["status"], "NO_OP", report["gaps"])
        self.assertEqual(report["actions"], [])
        self.assertEqual(report["reads"], [])
        self.assertEqual(report["memory_transitions"], [])

    def test_live_hard_caps_and_margin_not_double_subtracted(self):
        cfg, p, balance = config(), proposal(), portfolio()
        p["risk_pct"] = 20
        answer = execution.compute_admission(cfg, p, balance)
        self.assertEqual(answer["sizing"]["status"], "risk_capped")
        self.assertEqual(answer["final_qty"], 4)
        self.assertEqual(answer["risk_usd"], 20)
        balance["open_positions"] = 2
        balance["available_margin_usd"] = 40
        answer = execution.compute_admission(cfg, proposal(), balance)
        self.assertEqual(answer["decision"], "ADMITTED")
        balance["pending_margin_usd"] = .01
        self.assertIn("INSUFFICIENT_FREE_MARGIN", execution.compute_admission(cfg, proposal(), balance)["reason_codes"])


if __name__ == "__main__":
    unittest.main()
