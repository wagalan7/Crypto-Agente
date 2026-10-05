"""REDs independentes: cronologia, artefato re-hasheado e payoff OOS tipado."""
import copy
import socket
import unittest
from unittest.mock import patch

from services import score_v3_calibration_service as c
from services import score_v3_service as s

T = 1_780_000_000_000
B = 300_000
CUT = T + 1000 * B
NOW = T + 1400 * B


def row(key, decision=None, available=None, *, label=True, event=c.EVENT_TP1):
    decision = T + B if decision is None else decision
    return {"opportunity_key": key, "score": 75.0, "label": label, "event": event,
            "decision_ts_ms": decision,
            "label_available_ts_ms": decision + 10 * B if available is None else available}


def fit(rows=None, **kwargs):
    data = [row(f"o{i}", label=i < 160) for i in range(200)] if rows is None else rows
    context = dict(event=c.EVENT_TP1, population="P", model_fingerprint="F",
                   score_config_hash="CFG", horizon_bars=12, bar_ms=B,
                   censoring=c.CENSORING_RULES[0], payoff_ref="M", source="REPLAY",
                   dataset_hash="D", cutoff_ms=CUT, generated_at_ms=CUT + B,
                   valid_until_ms=T + 10000 * B, versions={"score": "V3"})
    context.update(kwargs)
    return c.fit_calibration(data, **context)


class CalibrationBoundaries(unittest.TestCase):
    def setUp(self):
        self.no_dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("no DNS"))
        self.no_dns.start()
        self.addCleanup(self.no_dns.stop)
        self.a = fit()["artifact"]

    def test_positive_fit_and_p0_p1(self):
        prediction = c.predict(self.a, score=75.0, now_ms=NOW)
        self.assertTrue(prediction["available"])
        self.assertEqual(prediction["probability"], 0.8)
        for label, expected in ((False, 0.0), (True, 1.0)):
            a = fit([row(f"p{i}", label=label) for i in range(200)])["artifact"]
            self.assertEqual(c.predict(a, score=75, now_ms=NOW)["probability"], expected)

    def test_fitting_rejects_incoherent_or_unavailable_context(self):
        for change in ({"generated_at_ms": CUT - 1}, {"valid_until_ms": None},
                       {"valid_until_ms": CUT}, {"horizon_bars": True},
                       {"cutoff_ms": -1}, {"censoring": "MADE_UP"},
                       {"versions": {"score": True}}):
            with self.subTest(change=change):
                self.assertFalse(fit(**change)["ok"])

    def test_fitting_rejects_bad_decision_label_event_horizon(self):
        bad = [row("x", decision=CUT + B, available=T + 2 * B),
               row("x", decision=None, available=T),
               row("x", decision=T + B, available=T + 14 * B),
               row("x", event=c.EVENT_TP2),
               {**row("x"), "decision_ts_ms": None},
               {**row("x"), "decision_ts_ms": True}]
        for invalid in bad:
            rows = [row(f"o{i}") for i in range(199)] + [invalid]
            with self.subTest(invalid=invalid):
                self.assertFalse(fit(rows)["ok"])

    def test_horizon_uses_first_full_replay_candle(self):
        decision = T + B + 123
        first = ((decision + B - 1) // B) * B
        rows = [row(f"a{i}", decision=decision, available=first + 12 * B) for i in range(200)]
        self.assertTrue(fit(rows)["ok"])
        rows[0]["label_available_ts_ms"] += 1
        self.assertFalse(fit(rows)["ok"])

    def test_conflicting_duplicate_is_not_first_winner(self):
        rows = [row(f"o{i}") for i in range(200)]
        other = {**rows[0], "label": False}
        for data in (rows + [other], [other] + rows):
            result = fit(data)
            self.assertFalse(result["ok"])
            self.assertEqual(result["artifact"]["coverage"]["unique_usable"], 199)

    def test_rehashed_semantically_invalid_artifact_never_predicts(self):
        mutations = [
            lambda a: a["bins"][7].update(n=1, successes=1, p=1.0),
            lambda a: a["bins"][7].update(p=2.0),
            lambda a: a["bins"][7].update(p=True),
            lambda a: a["bins"][7].update(supported="false"),
            lambda a: a["bins"][7].update(wilson_low=0.0),
            lambda a: a["bins"][7].update(upper_inclusive=1),
            lambda a: a["coverage"].update(unique_usable=1),
            lambda a: a["coverage"].update(bins_supported=9),
            lambda a: a["event_definition"].update(population="OTHER"),
            lambda a: a["event_definition"].update(event=c.EVENT_TP2),
            lambda a: a["event_definition"].update(interchangeable_with_other_events=0),
            lambda a: a["config"].update(min_labels_per_bin=1),
            lambda a: a["training"].update(opportunity_keys=["same"] * 200),
            lambda a: a["training"].update(label_max_ms=CUT + B),
            lambda a: a["generation"].update(valid_until_ms=None),
            lambda a: a.update(extra="unknown"),
        ]
        for index, mutate in enumerate(mutations):
            a = copy.deepcopy(self.a)
            mutate(a)
            a["artifact_hash"] = c.recompute_hash(a)
            with self.subTest(case=index):
                self.assertFalse(c.predict(a, score=75, now_ms=NOW)["available"])

    def test_malformed_nonfinite_artifact_fails_closed(self):
        for value in (float("nan"), float("inf"), "0.8", object()):
            a = copy.deepcopy(self.a)
            a["bins"][7]["p"] = value
            a["artifact_hash"] = c.recompute_hash(a)
            self.assertFalse(c.predict(a, score=75, now_ms=NOW)["available"])

    def test_expiry_cannot_be_skipped_by_missing_or_bad_clock(self):
        expires = self.a["generation"]["valid_until_ms"]
        with patch.object(c.time, "time", return_value=(expires + 1) / 1000):
            self.assertFalse(c.predict(self.a, score=75)["available"])
        for now in (True, "1400", float("nan"), CUT):
            self.assertFalse(c.predict(self.a, score=75, now_ms=now)["available"])

    def test_complete_expected_context_checks(self):
        for kwargs in ({"event": c.EVENT_TP2}, {"population": "OTHER"},
                       {"model_fingerprint": "OTHER"}, {"dataset_hash": "OTHER"},
                       {"score_config_hash": "OTHER"},
                       {"expected_event_definition": {**self.a["event_definition"], "horizon_bars": 6}}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(c.predict(self.a, score=75, now_ms=NOW, **kwargs)["available"])

    def test_oos_rejects_four_original_reds(self):
        invalids = [row("new", decision=CUT - B, available=CUT + B),
                    row("o0", decision=CUT + B, available=CUT + 2 * B),
                    row("future", decision=NOW - B, available=NOW + B),
                    row("reverse", decision=NOW, available=NOW - B)]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                self.assertFalse(c.validate_out_of_sample(self.a, [invalid], now_ms=NOW)["ok"])

    def test_positive_oos_validity_and_rehashed_metrics(self):
        data = [row(f"v{i}", decision=CUT + B, label=i % 2 == 0) for i in range(40)]
        result = c.validate_out_of_sample(self.a, data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        a = result["artifact"]
        self.assertTrue(c.verify_artifact(a, now_ms=NOW)["ok"])
        self.assertEqual(a["metrics"]["oos"]["predictions"], 40)
        self.assertFalse(a["approval"]["economically_approved"])
        for field, value in (("predictions", 1), ("decision_min_ms", CUT),
                             ("label_max_ms", NOW + 1), ("brier", 0.0)):
            bad = copy.deepcopy(a)
            bad["metrics"]["oos"][field] = value
            bad["artifact_hash"] = c.recompute_hash(bad)
            self.assertFalse(c.verify_artifact(bad, now_ms=NOW)["ok"])

    def test_score_verdict_requires_context(self):
        payload = {"score": 75, "model_fingerprint": "F", "population": "P"}
        self.assertIsNone(s.calibration_verdict(payload, artifact=self.a, now_ms=NOW)["probability"])
        prediction = s.calibration_verdict(payload, artifact=self.a, now_ms=NOW,
            event=c.EVENT_TP1, dataset_hash="D", score_config_hash="CFG",
            expected_event_definition=self.a["event_definition"])
        self.assertEqual(prediction["probability"], 0.8)


class PayoffBoundaries(unittest.TestCase):
    def setUp(self):
        self.rows = [{**row(f"pay{i}", decision=CUT + B), "net_r": 0.42,
                      "costs_included": True, "management_hash": "M",
                      "dataset_hash": "D", "costs_hash": "C"} for i in range(40)]
        self.context = dict(management_hash="M", dataset_hash="D", costs_hash="C",
                            event=c.EVENT_TP1, now_ms=NOW)

    def test_ev_requires_event_and_real_oos_proof(self):
        self.assertFalse(s.net_ev(probability_out_of_sample=.8, rr_tp2=2.5, cost_r=.1)["available"])
        self.assertFalse(s.net_ev_from_payoff(expected_payoff_r=.42, cost_r=.08,
            source="IN_SAMPLE_OR_INVENTED", sample_size=1, payoff_contract="WRONG")["available"])
        self.assertTrue(s.net_ev(probability_out_of_sample=.8, rr_tp2=2.5,
                                  cost_r=.1, event=c.EVENT_TP2)["available"])

    def test_net_payoff_is_not_charged_again(self):
        proof = c.build_oos_payoff_evidence(self.rows, cutoff_ms=CUT, **self.context)
        self.assertTrue(proof["ok"])
        ev = s.net_ev_from_payoff(evidence=proof["evidence"], **self.context)
        self.assertTrue(ev["available"])
        self.assertAlmostEqual(ev["ev_r"], .42)
        self.assertTrue(ev["costs_already_included"])

    def test_context_future_cost_identity_and_rehash_tamper(self):
        for field, value in (("event", c.EVENT_TP2), ("management_hash", "OTHER"),
                             ("dataset_hash", "OTHER"), ("costs_hash", "OTHER"),
                             ("costs_included", 1), ("net_r", float("nan")),
                             ("label_available_ts_ms", NOW + B), ("decision_ts_ms", CUT)):
            rows = copy.deepcopy(self.rows)
            rows[0][field] = value
            self.assertFalse(c.build_oos_payoff_evidence(rows, cutoff_ms=CUT, **self.context)["ok"])
        proof = c.build_oos_payoff_evidence(self.rows, cutoff_ms=CUT, **self.context)["evidence"]
        for key in ("management_hash", "dataset_hash", "costs_hash"):
            context = {**self.context, key: "OTHER"}
            self.assertFalse(s.net_ev_from_payoff(evidence=proof, **context)["available"])
        proof["expected_net_payoff_r"] = 99
        proof["evidence_hash"] = c._hash({k: v for k, v in proof.items() if k != "evidence_hash"})
        self.assertFalse(s.net_ev_from_payoff(evidence=proof, **self.context)["available"])

    def test_economic_consumer_revalidates_management_costs_and_proof(self):
        artifact = fit()["artifact"]
        oos_rows = [row(f"v{i}", decision=CUT + B) for i in range(40)]
        artifact = c.validate_out_of_sample(artifact, oos_rows, now_ms=NOW)["artifact"]
        proof = c.build_oos_payoff_evidence(self.rows, cutoff_ms=CUT, **self.context)["evidence"]
        ev = s.net_ev_from_payoff(evidence=proof, **self.context)
        payload = {"state": s.STATE_OK, "score": 75, "model_fingerprint": "F", "population": "P"}
        expected = dict(artifact=artifact, now_ms=NOW, event=c.EVENT_TP1,
                        dataset_hash="D", score_config_hash="CFG", costs_hash="C",
                        expected_event_definition=artifact["event_definition"])
        verdict = s.economic_verdict(payload, ev_payload=ev, **expected)
        self.assertEqual(verdict["economic_approval"], "PENDING_SIMULATION")
        self.assertEqual(verdict["live_eligibility"], s.STATE_UNAVAILABLE)
        for bad in ({"available": True, "ev_r": 99}, {**ev, "available": 1},
                    {**ev, "ev_r": 99}, {**ev, "costs_already_included": 1}):
            self.assertEqual(s.economic_verdict(payload, ev_payload=bad, **expected)["economic_approval"], s.STATE_UNAVAILABLE)
        expected["costs_hash"] = "OTHER"
        self.assertEqual(s.economic_verdict(payload, ev_payload=ev, **expected)["economic_approval"], s.STATE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
