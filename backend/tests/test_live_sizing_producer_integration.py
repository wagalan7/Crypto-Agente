"""Native nominal + filler context → native sizing → synthetic transport.

Cold-batch membership and persistence receipts are declared TEST_ONLY facts,
not a reconstruction of the scanner or an assertion about a real database.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_nominal_sizing_service as nominal
from services import live_filler_batch_service as filler
from services import live_upstream_sizing_service as upstream
from services import live_event_execution_service as execution
from tests import test_live_event_execution as broker
from tests import test_live_upstream_execution as native_tests
from tests import test_live_filler_batch as filler_tests

_CACHE = {}


def seal_input(request):
    request = copy.deepcopy(request)
    request["source_provenance"]["source_hash"] = upstream.digest(upstream.source_material(request))
    request["request_hash"] = upstream.digest({k: v for k, v in request.items() if k != "request_hash"})
    return request


def producer_bound_entry(*, canary=1., filler_on=False, test_done=0, short=False):
    key = execution.digest({"source": execution.execution_source_fingerprint(), "canary": canary,
                            "filler": filler_on, "done": test_done, "short": short})
    if key in _CACHE:
        return copy.deepcopy(_CACHE[key])
    base, _ = native_tests.bound_entry(canary=canary, short=short)
    original = base["upstream_sizing"]
    stamp, opp = original["decision_at_ms"], original["opportunity_id"]
    raw = copy.deepcopy(original["recommendation"])
    for name in nominal.FORBIDDEN_INPUT_FIELDS | {"_is_filler"}:
        raw.pop(name, None)
    regime = upstream.make_observation(producer="regime_service.get_regime_status",
        value={"btc_24h_pct": 2.}, observed_at_ms=stamp - 1, available_at_ms=stamp)
    nr = nominal.make_request(recommendations=[{"opportunity_id": opp, "recommendation": raw}],
        regime_observation=regime, decision_at_ms=stamp, batch_id="TEST_ONLY_PRODUCER_BATCH")
    np = nominal.replay(nr)
    rec = copy.deepcopy(np["recommendations"][0]["recommendation"])
    sizing = copy.deepcopy(original["config"])
    sizing["shadow"].update(FILLER_FORA_ENABLED=filler_on, FILLER_FORA_TEST_N=1,
                            FILLER_FORA_TEST_SIZE_MULT=.5, FILLER_FORA_SIZE_MULT=1.)
    cfg = filler_tests.config(FILLER_FORA_ENABLED=filler_on, FILLER_FORA_TEST_N=1)
    obs = filler_tests.observations(allowlist=["OTHER"], test_done=test_done)
    for item in obs.values():
        item["observed_at_ms"], item["available_at_ms"] = stamp - 1, stamp
        item["observation_hash"] = filler.digest({k: v for k, v in item.items() if k != "observation_hash"})
    # With filler disabled, empty allowlist is the known unrestricted policy.
    if not filler_on:
        obs["allowlist"] = upstream.make_observation(producer=filler.INITIAL_PRODUCERS["allowlist"], value=[],
            observed_at_ms=stamp - 1, available_at_ms=stamp)
    fr = filler.make_request(batch_id=nr["batch_id"], config=cfg,
        recommendations=[{"opportunity_id": opp, "recommendation": rec}], observations=obs, decision_at_ms=stamp)
    fp = filler.replay(fr)
    request = upstream.make_request(config=sizing, recommendation=fp["recommendation"],
        context={"regime": {}, "filler_test_done": fp["state_before"]["test_done"]},
        observations=original["observations"], decision_at_ms=stamp, opportunity_id=opp,
        nominal_producer={"request": nr, "report": np}, filler_batch={"request": fr, "report": fp})
    result = upstream.replay(request)
    prop, portfolio, execution_config = (copy.deepcopy(base[n]) for n in ("proposal", "portfolio", "config"))
    prop.update(leverage=rec["leverage"], risk_pct=result["final_risk_pct"])
    prop["sizing_context"]["source_hash"] = request["request_hash"]
    execution_config["sizing_stack"] = sizing
    admission = execution.compute_admission(execution_config, prop, portfolio,
        native_sizing={"request": request, "report": result})
    qty = admission["final_qty"]
    wire, side = prop["symbol"].split(":")[0].replace("/", ""), "SELL" if short else "BUY"
    params = {"symbol": wire, "side": side, "type": "MARKET", "quantity": qty,
              "newOrderRespType": "RESULT", "newClientOrderId": prop["dispatch_id"]}
    protection = broker.protection()
    protection["tp1_qty"] = qty * execution_config["TP1_QTY_PCT"]
    depth = broker.depth()
    if short:
        depth.update(bids=[[100, 100]], asks=[[100.1, 100]])
    receipts = [broker.receipt("set_leverage", 1, 0, (wire, prop["leverage"]), {"ok": True}, stamp=stamp),
        broker.receipt("depth", 2, 1, (prop["symbol"], qty), depth, stamp=stamp),
        broker.receipt("send_order", 3, 1, ("POST", "/fapi/v1/order", params),
            {"ok": True, "result": {"orderId": "entry-1", "status": "FILLED", "executedQty": qty,
                                     "avgPrice": prop["entry"]}}, stamp=stamp),
        broker.receipt("place_protection", 4, 3, (wire, side, qty), protection,
            {"stop_loss": prop["stop"], "tp1": prop["tp1"], "tp2": prop["tp2"],
             "tp1_qty_pct": execution_config["TP1_QTY_PCT"], "client_order_id_prefix": prop["dispatch_id"]}, stamp=stamp)]
    fill = broker.raw_fill(qty=qty)
    fill["binding"]["side"], fill["raw"]["side"] = prop["side"].upper(), side
    executed = execution.make_execution_request(config=execution_config, proposal=prop, portfolio=portfolio,
        receipts=receipts, fills=[fill], upstream_sizing=request)
    value = executed, result
    _CACHE[key] = copy.deepcopy(value)
    return copy.deepcopy(value)


class ProducerExecutionTests(unittest.TestCase):
    def test_nominal_then_native_quantity_and_transport_long_short(self):
        for short in (False, True):
            with self.subTest(short=short):
                request, sized = producer_bound_entry(short=short)
                self.assertEqual(request["proposal"]["leverage"], 2)
                self.assertEqual(sized["qty"], 2.)
                report = execution.replay_execution(request)
                self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
                self.assertEqual(report["filled_qty"], 2.)
                self.assertEqual(len([a for a in report["actions"] if a["port"] == "send_order"]), 1)
                self.assertEqual(set(sized["producer_refs"]), {"nominal_producer", "filler_batch"})
                self.assertEqual(report["upstream_sizing_report"]["producer_refs"], sized["producer_refs"])

    def test_canary_not_reapplied_after_producer(self):
        request, sized = producer_bound_entry(canary=.5)
        report = execution.replay_execution(request)
        self.assertEqual(report["status"], "COMPLETE_EXECUTION", report["gaps"])
        self.assertEqual(sized["qty"], 1.)
        self.assertEqual(report["filled_qty"], 1.)

    def test_filler_test_uses_counter_before_open(self):
        _, first = producer_bound_entry(filler_on=True, test_done=0)
        _, later = producer_bound_entry(filler_on=True, test_done=1)
        self.assertEqual(first["qty"], 1.)
        self.assertEqual(later["qty"], 2.)

    def test_batch_generation_replay_and_next_size_at_test_boundary(self):
        entry, _ = producer_bound_entry(filler_on=True)
        native = entry["upstream_sizing"]
        cold = copy.deepcopy(native["filler_batch"]["request"])
        first = cold["recommendations"][0]
        second = copy.deepcopy(first)
        second["opportunity_id"] = "TEST_ONLY_SECOND_OPPORTUNITY"
        second["recommendation"]["symbol"] = "OTHERALT/USDT:USDT"
        v = {"trade_return": {"id": 42, "symbol": first["recommendation"]["symbol"], "side": "long",
                              "source": "auto", "status": "open", "client_order_id": entry["proposal"]["dispatch_id"]},
             "unprotected_incident": False, "execution_status": "OPENED", "dispatch_id": entry["proposal"]["dispatch_id"]}
        obs = upstream.make_observation(producer="shadow_trade_service._open_trade_fail_closed", value=v,
            observed_at_ms=cold["decision_at_ms"], available_at_ms=cold["decision_at_ms"])
        future = filler.make_request(batch_id=cold["batch_id"], config=cold["config"],
            recommendations=[first, second], observations=cold["observations"], decision_at_ms=cold["decision_at_ms"],
            history=[{"opportunity_id": first["opportunity_id"], "observation": obs}])
        result = filler.replay(future)
        self.assertEqual(result["opportunity_id"], second["opportunity_id"])
        self.assertEqual(result["state_before"]["test_done"], 1)
        self.assertEqual(result["state_before"]["outside"], 1)
        self.assertEqual(filler.replay(copy.deepcopy(future)), result)

    def test_binding_mutations_resealed_are_refused(self):
        executed, _ = producer_bound_entry(filler_on=True)
        original = executed["upstream_sizing"]
        mutations = [lambda r: r["recommendation"].update(risk_pct=.9),
            lambda r: r["recommendation"].update(leverage=3),
            lambda r: r["context"].update(filler_test_done=100),
            lambda r: r["recommendation"].update(_is_filler=False),
            lambda r: r.update(opportunity_id="OTHER_OPPORTUNITY"),
            lambda r: r.update(decision_at_ms=r["decision_at_ms"] - 1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                request = copy.deepcopy(original)
                mutate(request)
                with self.assertRaises(ValueError):
                    upstream.validate_request(seal_input(request))

    def test_rehashed_component_reports_cannot_change_risk_or_counter(self):
        executed, _ = producer_bound_entry(filler_on=True)
        for component, mutate in (("nominal_producer", lambda p: p["recommendations"][0]["recommendation"].update(risk_pct=.8)),
                                  ("filler_batch", lambda p: p["state_before"].update(test_done=100))):
            with self.subTest(component=component):
                request = copy.deepcopy(executed["upstream_sizing"])
                report = request[component]["report"]
                mutate(report)
                report["report_hash"] = upstream.digest({k: v for k, v in report.items() if k != "report_hash"})
                with self.assertRaises(ValueError):
                    upstream.validate_request(seal_input(request))

    def test_no_live_authority_or_hidden_producer_claim(self):
        request, report = producer_bound_entry()
        self.assertFalse(report["declared_policy"]["eligibility_replayed"])
        for pair in (request["upstream_sizing"]["nominal_producer"], request["upstream_sizing"]["filler_batch"]):
            self.assertTrue(pair["report"]["test_only"])
            for name in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"):
                self.assertIs(pair["report"][name], False)

    def test_component_stage_clock_cannot_run_backwards(self):
        executed, _ = producer_bound_entry()
        request = copy.deepcopy(executed["upstream_sizing"])
        pair = request["filler_batch"]
        original = pair["request"]
        observations = copy.deepcopy(original["observations"])
        for obs in observations.values():
            obs["observed_at_ms"] -= 100
            obs["available_at_ms"] -= 100
            obs["observation_hash"] = filler.digest({k: v for k, v in obs.items() if k != "observation_hash"})
        earlier = filler.make_request(batch_id=original["batch_id"], config=original["config"],
            recommendations=original["recommendations"], observations=observations,
            decision_at_ms=original["decision_at_ms"] - 100)
        request["filler_batch"] = {"request": earlier, "report": filler.replay(earlier)}
        with self.assertRaisesRegex(ValueError, "STAGE_CLOCK_REVERSED"):
            upstream.validate_request(seal_input(request))

    def test_filler_members_must_come_from_nominal_batch_even_if_not_target(self):
        executed, _ = producer_bound_entry()
        request = copy.deepcopy(executed["upstream_sizing"])
        original = request["filler_batch"]["request"]
        second = copy.deepcopy(original["recommendations"][0])
        second["opportunity_id"] = "NOT_IN_NOMINAL_BATCH"
        expanded = filler.make_request(batch_id=original["batch_id"], config=original["config"],
            recommendations=original["recommendations"] + [second], observations=original["observations"],
            decision_at_ms=original["decision_at_ms"])
        request["filler_batch"] = {"request": expanded, "report": filler.replay(expanded)}
        with self.assertRaisesRegex(ValueError, "NOT_FROM_NOMINAL_PRODUCER"):
            upstream.validate_request(seal_input(request))
