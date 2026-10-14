"""Native sizing → execution → event-study integration, TEST_ONLY facts.

No LIVE helper is mocked. The public request builders freeze explicit broker
receipts and attributed fills; the real selected callers run in isolation.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
import unittest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_upstream_sizing_service as upstream
from services import live_event_execution_service as execution
from services import live_event_study_service as study
from tests import test_live_event_execution as events
from tests.test_live_event_study import fixture as study_fixture, NOW
from tests.test_live_upstream_sizing import config as sizing_config, recommendation, learned_row

_FIXTURES = {}


def _make_native(cfg, prop, portfolio, *, missing_symbol=False, learned=None):
    stamp = prop["decision_at_ms"]
    rec = recommendation(symbol=prop["symbol"], direction=prop["side"],
        timeframe=prop["timeframe"], entry=prop["entry"], stop_loss=prop["stop"],
        tp1=prop["tp1"], tp2=prop["tp2"], leverage=prop["leverage"],
        signal={"entry": prop["entry"], "indicators": {"atr": prop["atr"]}})
    rows = [] if learned is None else learned
    obs = {
        "equity": upstream.make_observation(producer="exchange_service.get_equity",
            value={"ok": True, "total_usd": portfolio["equity_usd"], "source": "live"},
            observed_at_ms=stamp - 1, available_at_ms=stamp),
        "symbol_learning": upstream.make_observation(producer="symbol_learning_service.refresh_cache",
            value=None if missing_symbol else rows, quality="UNAVAILABLE" if missing_symbol else "CAPTURED",
            observed_at_ms=stamp - 1, available_at_ms=stamp),
        "edge_decay": upstream.make_observation(producer="edge_decay_service.maybe_refresh",
            value=[], observed_at_ms=stamp - 1, available_at_ms=stamp)}
    return upstream.make_request(config=cfg, recommendation=rec,
        context={"regime": {}, "filler_test_done": 0}, observations=obs,
        decision_at_ms=stamp, opportunity_id=prop["opportunity_id"])


def bound_entry(*, canary=1., learned=None, missing_symbol=False, short=False):
    """Positive facts are built once; negative tests never repair their hashes."""
    key = execution.digest({"source": execution.execution_source_fingerprint(),
                            "canary": canary, "learned": learned,
                            "missing": missing_symbol, "short": short})
    if key in _FIXTURES:
        return copy.deepcopy(_FIXTURES[key])
    base = events.complete_entry_request(side="short" if short else "long")
    cfg, prop, portfolio = (copy.deepcopy(base[name]) for name in ("config", "proposal", "portfolio"))
    frozen = sizing_config(LIVE_SIZE_MULT=canary)
    if learned is not None or missing_symbol:
        frozen["symbol_learning"]["SYMBOL_LEARNING_SIZE_ENABLED"] = True
    cfg["LIVE_SIZE_MULT"] = canary
    cfg["sizing_stack"] = frozen
    native = _make_native(frozen, prop, portfolio, missing_symbol=missing_symbol, learned=learned)
    report = upstream.replay(native)
    prop["sizing_context"]["source_hash"] = native["request_hash"]
    if report["status"] == "COMPLETE_SIZING":
        prop["risk_pct"] = report["final_risk_pct"]
        admission = execution.compute_admission(cfg, prop, portfolio,
            native_sizing={"request": native, "report": report})
        qty = admission["final_qty"]
        wire = prop["symbol"].split(":")[0].replace("/", "")
        broker_side = "SELL" if short else "BUY"
        stamp = prop["decision_at_ms"]
        book = events.depth()
        if short:
            book.update(bids=[[100, 100]], asks=[[100.1, 100]])
        params = {"symbol": wire, "side": broker_side, "type": "MARKET", "quantity": qty,
                  "newOrderRespType": "RESULT", "newClientOrderId": prop["dispatch_id"]}
        protection = events.protection()
        protection["tp1_qty"] = qty * cfg["TP1_QTY_PCT"]
        rows = [events.receipt("set_leverage", 1, 0, (wire, prop["leverage"]), {"ok": True}, stamp=stamp),
            events.receipt("depth", 2, 1, (prop["symbol"], qty), book, stamp=stamp),
            events.receipt("send_order", 3, 1, ("POST", "/fapi/v1/order", params),
                {"ok": True, "result": {"orderId": "entry-1", "status": "FILLED",
                                          "executedQty": qty, "avgPrice": prop["entry"]}}, stamp=stamp),
            events.receipt("place_protection", 4, 3, (wire, broker_side, qty), protection,
                {"stop_loss": prop["stop"], "tp1": prop["tp1"], "tp2": prop["tp2"],
                 "tp1_qty_pct": cfg["TP1_QTY_PCT"], "client_order_id_prefix": prop["dispatch_id"]}, stamp=stamp)]
        fill = events.raw_fill(qty=qty)
        fill["binding"]["side"] = prop["side"].upper()
        fill["raw"]["side"] = broker_side
        fills = [fill]
    else:
        rows, fills = [], []
    request = execution.make_execution_request(config=cfg, proposal=prop, portfolio=portfolio,
        receipts=rows, fills=fills, upstream_sizing=native)
    result = (request, report)
    _FIXTURES[key] = copy.deepcopy(result)
    return copy.deepcopy(result)


def rebuild(request, **changes):
    values = {name: copy.deepcopy(request[name]) for name in
              ("config", "proposal", "portfolio", "receipts", "fills", "upstream_sizing")}
    values.update(changes)
    return execution.make_execution_request(**values)


class NativeAdmissionIntegration(unittest.TestCase):
    def test_native_post_qty_is_used_without_second_canary(self):
        request, report = bound_entry(canary=.5)
        admitted = execution.compute_admission(request["config"], request["proposal"], request["portfolio"],
            native_sizing={"request": request["upstream_sizing"], "report": report})
        self.assertEqual(report["qty"], 1.)
        self.assertEqual(admitted["final_qty"], 1.)
        self.assertEqual(admitted["risk_usd"], 5.)
        self.assertEqual(admitted["margin_usd"], 20.)

    def test_native_learner_multiplier_is_not_discarded_by_admission(self):
        rows = [learned_row(.8)]
        rows[0]["base"] = "BTC"
        request, report = bound_entry(learned=rows)
        admitted = execution.compute_admission(request["config"], request["proposal"], request["portfolio"],
            native_sizing={"request": request["upstream_sizing"], "report": report})
        self.assertEqual(report["qty"], 1.6)
        self.assertEqual(admitted["final_qty"], 1.6)

    def test_final_quantity_may_only_reduce_native_plan(self):
        request, report = bound_entry(canary=.5)
        native = {"request": request["upstream_sizing"], "report": report}
        reduced = execution.compute_admission(request["config"], request["proposal"], request["portfolio"],
                                             native_sizing=native, final_qty=.75)
        increased = execution.compute_admission(request["config"], request["proposal"], request["portfolio"],
                                               native_sizing=native, final_qty=1.001)
        self.assertEqual(reduced["decision"], "ADMITTED")
        self.assertEqual(reduced["final_qty"], .75)
        self.assertEqual(increased["decision"], "REJECTED")
        self.assertIn("FINAL_QTY_EXCEEDS_PLANNED", increased["reason_codes"])

    def test_effective_risk_mismatch_cannot_enter_admission(self):
        request, report = bound_entry()
        proposal = copy.deepcopy(request["proposal"])
        proposal["risk_pct"] = 1.5
        with self.assertRaisesRegex(ValueError, "EFFECTIVE_RISK_DRIFT"):
            execution.compute_admission(request["config"], proposal, request["portfolio"],
                native_sizing={"request": request["upstream_sizing"], "report": report})

    def test_shared_numeric_policy_cannot_disagree(self):
        request, _ = bound_entry()
        cfg = copy.deepcopy(request["config"])
        cfg["LIVE_SIZE_MULT"] = .5
        with self.assertRaisesRegex(ValueError, "POLICY_CONFIG_DRIFT"):
            rebuild(request, config=cfg)

    def test_context_source_hash_binds_full_native_request(self):
        request, _ = bound_entry()
        proposal = copy.deepcopy(request["proposal"])
        proposal["sizing_context"]["source_hash"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "UPSTREAM_IDENTITY_DRIFT"):
            rebuild(request, proposal=proposal)

    def test_timeframe_side_symbol_and_clock_cannot_drift(self):
        request, _ = bound_entry()
        for field, value in (("timeframe", "4h"), ("side", "short"),
                             ("symbol", "ALFA/USDT"), ("decision_at_ms", 1_000_001)):
            proposal = copy.deepcopy(request["proposal"])
            proposal[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "UPSTREAM_IDENTITY_DRIFT"):
                rebuild(request, proposal=proposal)

    def test_wire_equivalent_symbol_cannot_replace_raw_native_cache_identity(self):
        request, _ = bound_entry()
        for alias in ("BTCUSDT", "BTC/USDT:USDT"):
            proposal = copy.deepcopy(request["proposal"])
            proposal["symbol"] = alias
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "UPSTREAM_IDENTITY_DRIFT"):
                rebuild(request, proposal=proposal)

    def test_native_equity_cannot_be_replaced_by_portfolio_equity(self):
        request, _ = bound_entry()
        portfolio = copy.deepcopy(request["portfolio"])
        portfolio["equity_usd"] = 900.
        with self.assertRaisesRegex(ValueError, "PORTFOLIO_EQUITY_DRIFT"):
            rebuild(request, portfolio=portfolio)

    def test_quote_cannot_collapse_usdc_into_usdt(self):
        request, _ = bound_entry()
        proposal = copy.deepcopy(request["proposal"])
        proposal.update(symbol="BTC/USDC", quote="USDC")
        with self.assertRaisesRegex(ValueError, "UPSTREAM_IDENTITY_DRIFT|QUOTE_UNSUPPORTED"):
            rebuild(request, proposal=proposal)

    def test_frozen_stack_without_native_input_never_falls_back_to_legacy(self):
        request, _ = bound_entry()
        with self.assertRaisesRegex(ValueError, "UPSTREAM.*MISSING|UPSTREAM.*REQUIRED"):
            execution.make_execution_request(config=request["config"], proposal=request["proposal"],
                portfolio=request["portfolio"], receipts=request["receipts"], fills=request["fills"])


class NativeExecutionIntegration(unittest.TestCase):
    def test_actual_entry_caller_posts_once_with_native_all_off_quantity(self):
        request, _ = bound_entry()
        report = execution.replay_execution(request)
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        orders = [a for a in report["actions"] if a["port"] == "send_order"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["args"][2]["quantity"], 2.)
        self.assertEqual(report["upstream_sizing_report"]["status"], "COMPLETE_SIZING")
        self.assertEqual(report["filled_qty"], 2.)
        self.assertIs(report["authorizes_live"], False)

    def test_actual_post_consumes_canary_once(self):
        request, _ = bound_entry(canary=.5)
        report = execution.replay_execution(request)
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        order = next(a for a in report["actions"] if a["port"] == "send_order")
        self.assertEqual(order["args"][2]["quantity"], 1.)
        self.assertEqual(report["filled_qty"], 1.)

    def test_short_native_sizing_uses_actual_sell_entry_and_protection(self):
        request, _ = bound_entry(short=True)
        report = execution.replay_execution(request)
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        order = next(a for a in report["actions"] if a["port"] == "send_order")
        self.assertEqual(order["args"][2]["side"], "SELL")
        self.assertEqual(order["args"][2]["quantity"], 2.)

    def test_unknown_active_cache_is_never_a_zero_post_eligible_success(self):
        request, _ = bound_entry(missing_symbol=True)
        report = execution.replay_execution(request)
        self.assertEqual(report["upstream_sizing_report"]["status"], "TECHNICAL_GAP")
        self.assertFalse(any(a["port"] in ("send_order", "set_leverage", "place_protection")
                             for a in report["actions"]))
        self.assertNotEqual(report["status"], "COMPLETE_EXECUTION")

    def test_legacy_without_stack_preserves_existing_execution_contract(self):
        request = events.complete_entry_request()
        report = execution.replay_execution(request)
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(report["filled_qty"], 2.)
        self.assertIsNone(report["upstream_sizing_report"])

    def test_native_trace_roundtrip_preserves_hash_and_quantity(self):
        import json
        request, _ = bound_entry()
        restored = json.loads(json.dumps(request))
        report = execution.replay_execution(restored)
        self.assertEqual(restored["request_hash"], request["request_hash"])
        self.assertEqual(report["upstream_sizing_report"]["request_hash"], request["upstream_sizing"]["request_hash"])
        self.assertEqual(report["filled_qty"], 2.)

    def test_upstream_source_family_must_match_execution_source(self):
        request, _ = bound_entry()
        native = request["upstream_sizing"]
        provenance = {"producer": "TEST_ONLY_DECLARED_MODEL", "source_id": "TEST_ONLY",
            "source_hash": upstream.digest({k: native[k] for k in
                ("recommendation", "context", "observations")}),
            "quality": "DECLARED_MODEL", "model_version": "CW_DECLARED_SIZING_INPUTS_V1", "synthetic": True}
        other = upstream.make_request(config=native["config"], recommendation=native["recommendation"],
            context=native["context"], observations=native["observations"],
            decision_at_ms=native["decision_at_ms"], opportunity_id=native["opportunity_id"],
            source="DECLARED_EVENT_TABLE", source_provenance=provenance)
        prop = copy.deepcopy(request["proposal"])
        prop["sizing_context"]["source_hash"] = other["request_hash"]
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SOURCE_BOUNDARY"):
            rebuild(request, proposal=prop, upstream_sizing=other)

    def test_known_native_funding_rejection_sends_no_post_or_leverage(self):
        request, _ = bound_entry()
        native = request["upstream_sizing"]
        cfg = copy.deepcopy(native["config"])
        cfg["shadow"].update(FUNDING_EV_ENABLED=True, FUNDING_EV_MAX_DRAG_R=.1)
        rec = copy.deepcopy(native["recommendation"])
        rec["signal"]["derivatives"] = {"funding_rate_pct": 1.}
        other = upstream.make_request(config=cfg, recommendation=rec,
            context=native["context"], observations=native["observations"],
            decision_at_ms=native["decision_at_ms"], opportunity_id=native["opportunity_id"])
        prop, execution_cfg = copy.deepcopy(request["proposal"]), copy.deepcopy(request["config"])
        prop["sizing_context"]["source_hash"] = other["request_hash"]
        execution_cfg["sizing_stack"] = cfg
        denied = rebuild(request, config=execution_cfg, proposal=prop,
            upstream_sizing=other, receipts=[], fills=[])
        report = execution.replay_execution(denied)
        self.assertEqual(report["upstream_sizing_report"]["status"], "REJECTED")
        self.assertFalse(report["actions"])
        self.assertNotEqual(report["status"], "COMPLETE_EXECUTION")


class NativeStudyIntegration(unittest.TestCase):
    def test_missing_active_cache_keeps_study_noncomparable_not_not_executed(self):
        manifest, dataset = study_fixture()
        entry, _ = bound_entry(missing_symbol=True)
        body = {k: copy.deepcopy(v) for k, v in manifest.items() if k != "manifest_hash"}
        body["hashes"] = None
        body["shared"]["execution_config"] = copy.deepcopy(entry["config"])
        manifest = study.parse_manifest(body)
        dataset.update(manifest_hash=manifest["manifest_hash"], dataset_hash=None)
        for arm in ("baseline", "candidate"):
            dataset["events"][arm][0]["steps"][0]["request"] = copy.deepcopy(entry)
        result = study.run_study(manifest=manifest,
            dataset=study.validate_dataset(dataset, manifest), now_ms=NOW)
        self.assertEqual(result["state"], "TECHNICAL_GAP", result)
        for arm in result["arms"].values():
            self.assertEqual(arm["records"][0]["state"], "NON_COMPARABLE")
        self.assertIs(result["promotable"], False)

    def test_official_event_study_consumes_sizing_in_both_frozen_arms(self):
        manifest, dataset = study_fixture()
        entry, _ = bound_entry()
        body = {k: copy.deepcopy(v) for k, v in manifest.items() if k != "manifest_hash"}
        body["hashes"] = None
        body["shared"]["execution_config"] = copy.deepcopy(entry["config"])
        manifest = study.parse_manifest(body)
        dataset["manifest_hash"] = manifest["manifest_hash"]
        dataset["dataset_hash"] = None
        for arm in ("baseline", "candidate"):
            dataset["events"][arm][0]["steps"][0]["request"] = copy.deepcopy(entry)
        dataset = study.validate_dataset(dataset, manifest)
        result = study.run_study(manifest=manifest, dataset=dataset, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "LOCAL_EVENT_EVIDENCE", result.get("technical_gaps"))
        for arm in result["arms"].values():
            record = arm["records"][0]
            self.assertEqual(record["state"], "COMPLETE", record)
            self.assertEqual(record["execution"]["upstream_sizing_report"]["qty"], 2.)
            self.assertEqual(float(record["ledger"]["net_including_funding"]), 3.8)
            self.assertEqual(arm["portfolio_at_last_decision"]["available_margin_usd"], 760.)
        self.assertEqual(result["holdout_status"], "SEALED")
        self.assertIs(result["promotable"], False)
        checked = study.verify_report(result, recompute=True)
        self.assertTrue(checked["ok"], checked)


if __name__ == "__main__":
    unittest.main()
