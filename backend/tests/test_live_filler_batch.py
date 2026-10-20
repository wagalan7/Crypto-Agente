"""Research-only replay of the actual filler batch consumer.

Every account/count/brake/completion value below is an explicit synthetic
observation.  A hash makes the observation reproducible, not broker-authentic.
No live service, credential, environment setting or database is imported.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_filler_batch_service as filler
from services import live_upstream_sizing_service as upstream

T0 = 1_800_000_000_000


def config(**changes):
    result = {"SHADOW_ENABLED": False, "FILLER_FORA_ENABLED": True,
              "FILLER_FORA_OFF_AT": 350, "FILLER_FORA_MAX": 3,
              "FILLER_TOTAL_SLOTS": 5, "FILLER_FORA_TEST_N": 2}
    result.update(changes)
    return result


def recommendation(symbol="ALFA/USDT:USDT", **changes):
    result = {"symbol": symbol, "direction": "long", "timeframe": "1h",
              "tier": "A", "entry": 100., "stop_loss": 95., "tp1": 110.,
              "tp2": 120., "risk_pct": 1., "leverage": 5}
    result.update(changes)
    return result


def item(key, symbol="ALFA/USDT:USDT", **changes):
    return {"opportunity_id": key, "recommendation": recommendation(symbol, **changes)}


def observation(producer, value, **changes):
    values = {"producer": producer, "value": value, "observed_at_ms": T0 - 1000,
              "available_at_ms": T0, "generation": 1, "quality": "CAPTURED"}
    values.update(changes)
    return upstream.make_observation(**values)


def observations(*, allowlist=None, inside=0, outside=0, blocked=False, test_done=0):
    return {"allowlist": observation("shadow_trade_service.get_exec_allowlist",
                                     ["ALFA"] if allowlist is None else allowlist),
            "counts": observation("shadow_trade_service._open_auto_counts_by_allowlist",
                                  {"inside": inside, "outside": outside}),
            "brake": observation("shadow_trade_service._filler_fora_brake_state",
                                 {"blocked": blocked, "block_reason": "TEST_ONLY" if blocked else None}),
            "test_done": observation("shadow_trade_service._filler_fora_test_opened_count", test_done)}


def completion(key, symbol="ALFA/USDT:USDT", *, status="OPENED", incident=False,
               trade=True, **changes):
    value = {"trade_return": {"id": int(filler.digest(key)[:8], 16) + 1,
                              "symbol": symbol, "side": "long", "source": "auto", "status": "open",
                              "client_order_id": "TEST_ONLY_" + key} if trade else None,
             "unprotected_incident": incident, "execution_status": status,
             "dispatch_id": "TEST_ONLY_" + key if trade else None}
    value.update(changes)
    return {"opportunity_id": key,
            "observation": observation("shadow_trade_service._open_trade_fail_closed", value,
                                       observed_at_ms=T0)}


def make_request(*, cfg=None, recs=None, obs=None, history=None, **changes):
    return filler.make_request(batch_id="TEST_ONLY_FILLER_BATCH", config=config() if cfg is None else cfg,
        recommendations=[item("one")] if recs is None else recs,
        observations=observations() if obs is None else obs, decision_at_ms=T0,
        history=[] if history is None else history, **changes)


def reseal(request):
    result = copy.deepcopy(request)
    result["request_hash"] = filler.digest({k: v for k, v in result.items() if k != "request_hash"})
    return result


class NativeBatchPositiveTests(unittest.TestCase):
    def replay(self, **changes):
        report = filler.replay(make_request(**changes))
        self.assertNotEqual(report["status"], "TECHNICAL_GAP", report)
        return report

    def test_stable_inside_first_preserves_order_within_both_groups(self):
        recs = [item("outside-1", "GAMAUSDT"), item("inside-1", "ALFAUSDT"),
                item("outside-2", "DELTA/USDT:USDT"), item("inside-2", "ALFA/USDT")]
        report = self.replay(recs=recs)
        self.assertEqual(report["ordered_opportunities"],
                         ["inside-1", "inside-2", "outside-1", "outside-2"])
        self.assertEqual(report["opportunity_id"], "inside-1")
        self.assertTrue(report["route"]["allowed"])
        self.assertFalse(report["recommendation"].get("_is_filler", False))

    def test_plain_symbol_uses_native_base_extraction(self):
        for symbol in ("ALFAUSDT", "ALFA/USDT:USDT", "ALFAUSDC", "ALFAUSD"):
            with self.subTest(symbol=symbol):
                report = self.replay(recs=[item("one", symbol)])
                self.assertTrue(report["route"]["allowed"])
                self.assertFalse(report["recommendation"].get("_is_filler", False))

    def test_outside_allowed_is_marked_before_sizing(self):
        report = self.replay(recs=[item("one", "GAMAUSDT")])
        self.assertTrue(report["route"]["allowed"])
        self.assertTrue(report["recommendation"]["_is_filler"])
        self.assertEqual(report["state_before"]["test_done"], 0)

    def test_empty_allowlist_is_unrestricted_even_with_filler_disabled(self):
        report = self.replay(cfg=config(FILLER_FORA_ENABLED=False),
            obs=observations(allowlist=[]), recs=[item("one", "GAMAUSDT")])
        self.assertTrue(report["route"]["allowed"])
        self.assertFalse(report["recommendation"].get("_is_filler", False))

    def test_filler_disabled_preserves_order_and_blocks_outside(self):
        report = self.replay(cfg=config(FILLER_FORA_ENABLED=False),
            recs=[item("outside", "GAMAUSDT"), item("inside")])
        self.assertEqual(report["ordered_opportunities"], ["outside", "inside"])
        self.assertEqual(report["opportunity_id"], "outside")
        self.assertFalse(report["route"]["allowed"])
        self.assertTrue(report["route"]["skip_reasons"])

    def test_off_at_threshold_restricts_outside_but_not_inside(self):
        report = self.replay(cfg=config(FILLER_FORA_OFF_AT=1), recs=[item("one", "GAMAUSDT")])
        self.assertFalse(report["route"]["allowed"])
        inside = self.replay(cfg=config(FILLER_FORA_OFF_AT=1))
        self.assertTrue(inside["route"]["allowed"])

    def test_outside_cap_and_free_inside_capacity_are_both_enforced(self):
        for obs in (observations(outside=3), observations(inside=5),
                    observations(inside=4, outside=1)):
            with self.subTest(counts=obs["counts"]["value"]):
                report = self.replay(obs=obs, recs=[item("one", "GAMAUSDT")])
                self.assertFalse(report["route"]["allowed"])

    def test_brake_only_stops_outside_route(self):
        outside = self.replay(obs=observations(blocked=True), recs=[item("one", "GAMAUSDT")])
        self.assertFalse(outside["route"]["allowed"])
        self.assertTrue(self.replay(obs=observations(blocked=True))["route"]["allowed"])

    def test_normal_inside_completion_advances_native_state_once(self):
        recs = [item("one"), item("two", "GAMAUSDT")]
        report = self.replay(recs=recs, history=[completion("one")])
        self.assertEqual(report["opportunity_id"], "two")
        self.assertEqual(report["state_before"], {"inside": 1, "outside": 0, "test_done": 0, "opened": 1})
        self.assertEqual(len(report["transitions"]), 1)
        again = self.replay(recs=recs, history=[completion("one")])
        self.assertEqual(again["state_before"], report["state_before"])

    def test_outside_completion_advances_test_count_through_threshold(self):
        recs = [item("one", "GAMAUSDT"), item("two", "DELTAUSDT")]
        report = self.replay(recs=recs, obs=observations(test_done=1),
                             history=[completion("one", "GAMAUSDT")])
        self.assertEqual(report["state_before"], {"inside": 0, "outside": 1, "test_done": 2, "opened": 1})
        self.assertTrue(report["recommendation"]["_is_filler"])

    def test_disabled_test_counter_does_not_advance_on_outside_completion(self):
        recs = [item("one", "GAMAUSDT"), item("two", "DELTAUSDT")]
        report = self.replay(cfg=config(FILLER_FORA_TEST_N=0), recs=recs,
                             history=[completion("one", "GAMAUSDT")])
        self.assertEqual(report["state_before"]["outside"], 1)
        self.assertEqual(report["state_before"]["test_done"], 0)

    def test_disabled_test_counter_never_reads_unavailable_or_nonzero_observation(self):
        for captured in (False, True):
            with self.subTest(captured=captured):
                obs = observations(test_done=9)
                if not captured:
                    obs["test_done"] = observation(obs["test_done"]["producer"], None, quality="UNAVAILABLE")
                report = self.replay(cfg=config(FILLER_FORA_TEST_N=0), obs=obs)
                self.assertEqual(report["state_before"]["test_done"], 0)

    def test_none_and_incident_do_not_increment_normal_opened_or_slots(self):
        recs = [item("one"), item("two", "GAMAUSDT")]
        for event in (completion("one", status="NO_ENTRY", trade=False),
                      completion("one", status="INCIDENT", incident=True)):
            with self.subTest(event=event["observation"]["value"]):
                report = self.replay(recs=recs, history=[event])
                self.assertEqual(report["state_before"], {"inside": 0, "outside": 0, "test_done": 0, "opened": 0})

    def test_all_completions_exhaust_batch_without_inventing_next(self):
        report = self.replay(history=[completion("one")])
        self.assertEqual(report["status"], "BATCH_EXHAUSTED")
        self.assertIsNone(report["opportunity_id"])
        self.assertIsNone(report["recommendation"])
        self.assertEqual(report["state_before"]["opened"], 1)

    def test_replay_does_not_mutate_request_or_recommendation(self):
        request = make_request(recs=[item("one", "GAMAUSDT")])
        before = copy.deepcopy(request)
        filler.replay(request)
        self.assertEqual(request, before)
        self.assertNotIn("_is_filler", request["recommendations"][0]["recommendation"])


class NativeBatchBoundaryTests(unittest.TestCase):
    def test_unknown_completion_stops_before_next_opportunity(self):
        report = filler.replay(make_request(recs=[item("one"), item("two")],
            history=[completion("one", status="UNKNOWN", trade=False)]))
        self.assertEqual(report["status"], "TECHNICAL_GAP")
        self.assertIsNone(report["opportunity_id"])
        self.assertIsNone(report["route"])

    def test_unavailable_active_input_never_silently_defaults(self):
        for key in ("allowlist", "counts", "brake", "test_done"):
            with self.subTest(key=key):
                obs = observations()
                obs[key] = observation(obs[key]["producer"], None, quality="UNAVAILABLE")
                report = filler.replay(make_request(obs=obs))
                self.assertEqual(report["status"], "TECHNICAL_GAP")
                self.assertIsNone(report["route"])

    def test_causal_observation_and_payload_hash_are_enforced(self):
        obs = observations()
        obs["counts"] = observation(obs["counts"]["producer"], {"inside": 0, "outside": 0},
                                   available_at_ms=T0 + 1)
        with self.assertRaises(ValueError):
            make_request(obs=obs)
        request = make_request()
        request["observations"]["counts"]["value"]["inside"] = 1
        with self.assertRaises(ValueError):
            filler.validate_request(reseal(request))
        obs = observations()
        obs["counts"] = observation("unknown_reader", {"inside": 0, "outside": 0})
        with self.assertRaises(ValueError):
            make_request(obs=obs)

    def test_duplicate_opportunities_or_nonprefix_history_are_refused(self):
        for kwargs in ({"recs": [item("one"), item("one")]},
                       {"recs": [item("one"), item("two")], "history": [completion("two")]},
                       {"recs": [item("one"), item("two")],
                        "history": [completion("one"), completion("one")]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                filler.replay(make_request(**kwargs))
        for duplicate_field in ("trade_id", "dispatch_id"):
            first, second = completion("one"), completion("two")
            value = copy.deepcopy(second["observation"]["value"])
            if duplicate_field == "trade_id":
                value["trade_return"]["id"] = first["observation"]["value"]["trade_return"]["id"]
            else:
                value["dispatch_id"] = first["observation"]["value"]["dispatch_id"]
                value["trade_return"]["client_order_id"] = value["dispatch_id"]
            second["observation"] = observation("shadow_trade_service._open_trade_fail_closed", value,
                                                 observed_at_ms=T0)
            with self.subTest(duplicate_field=duplicate_field), self.assertRaises(ValueError):
                filler.replay(make_request(recs=[item("one"), item("two"), item("three")],
                                           history=[first, second]))

    def test_complete_claim_needs_trade_and_matching_identity(self):
        invalid_id = completion("one")
        invalid_id["observation"] = observation("shadow_trade_service._open_trade_fail_closed",
            {**invalid_id["observation"]["value"], "trade_return": {
                "id": True, "symbol": "ALFA/USDT:USDT", "side": "long", "source": "auto", "status": "open",
                "client_order_id": "TEST_ONLY_one"}},
            observed_at_ms=T0)
        mismatch = completion("one")
        value = copy.deepcopy(mismatch["observation"]["value"])
        value["trade_return"]["client_order_id"] = "TEST_ONLY_other_dispatch"
        mismatch["observation"] = observation("shadow_trade_service._open_trade_fail_closed", value,
                                              observed_at_ms=T0)
        for event in (completion("one", trade=False), invalid_id,
                      completion("one", symbol="OTHERUSDT"),
                      completion("one", status="NO_ENTRY"), mismatch):
            with self.subTest(event=event), self.assertRaises(ValueError):
                filler.replay(make_request(history=[event]))

    def test_config_is_closed_and_bool_is_not_a_number(self):
        bad = [config(FILLER_FORA_MAX=True), config(FILLER_TOTAL_SLOTS=-1),
               config(FILLER_FORA_TEST_N="2"), config(FILLER_FORA_ENABLED="false"),
               config(SHADOW_ENABLED=True), config(unexpected=1)]
        for cfg in bad:
            with self.subTest(config=cfg), self.assertRaises(ValueError):
                make_request(cfg=cfg)

    def test_count_and_completion_booleans_are_literal(self):
        for obs in (observations(inside=True), observations(outside=1.5),
                    observations(test_done=True), observations(blocked=1)):
            with self.subTest(observations=obs), self.assertRaises(ValueError):
                make_request(obs=obs)
        with self.assertRaises(ValueError):
            make_request(history=[completion("one", unprotected_incident=0)])

    def test_zero_counts_are_known_and_do_not_become_missing(self):
        report = filler.replay(make_request(obs=observations(inside=0, outside=0, test_done=0)))
        self.assertEqual(report["status"], "COMPLETE_CONTEXT")
        self.assertEqual(report["initial_state"], {"inside": 0, "outside": 0, "test_done": 0, "opened": 0})

    def test_report_never_authorizes_live_and_tampering_is_refused(self):
        request = make_request()
        report = filler.replay(request)
        for name in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"):
            self.assertIs(report[name], False)
        self.assertIs(report["test_only"], True)
        filler.validate_report(report, request)
        changed = copy.deepcopy(report)
        changed["authorizes_live"] = True
        changed["report_hash"] = filler.digest({k: v for k, v in changed.items() if k != "report_hash"})
        with self.assertRaises(ValueError):
            filler.validate_report(changed, request)

    def test_rehashed_route_or_state_needs_native_recomputation_not_hash_trust(self):
        request = make_request(obs=observations(outside=3), recs=[item("one", "GAMAUSDT")])
        report = filler.replay(request)
        self.assertFalse(report["route"]["allowed"])
        for field in ("route", "state_before"):
            with self.subTest(field=field):
                changed = copy.deepcopy(report)
                if field == "route":
                    changed["route"] = {"allowed": True, "skip_reasons": []}
                else:
                    changed["state_before"]["opened"] = 999
                changed["report_hash"] = filler.digest({k: v for k, v in changed.items() if k != "report_hash"})
                with self.assertRaises(ValueError):
                    filler.validate_report(changed, request, recompute=True)


if __name__ == "__main__":
    unittest.main()
