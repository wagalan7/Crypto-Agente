"""Independent numerical and trust-boundary tests for the event family."""
import copy
import json
import unittest
from unittest import mock

from services import live_event_evidence_service as event
from services import research_acceptance_service as acceptance
from services import score_v3_calibration_service as calibration


DAY = 86_400_000


def evidence_inputs(*, complete=False):
    start = 100 * DAY
    temporal = {"train_start_ms": start, "validation_start_ms": start + 20 * DAY,
        "holdout_start_ms": start + 44 * DAY, "as_of_ms": start + 44 * DAY,
        "bar_ms": 1, "horizon_ms": 10, "purge_ms": 10, "embargo_ms": 0,
        "censoring": "EXPIRES_AFTER_HORIZON_WITHOUT_RESOLUTION"}
    population, labels, baseline, candidate = [], [], [], []
    training_n, validation_per_day = (360, 12) if complete else (6, 1)
    decisions = [start + 1 + i * (19 * DAY // training_n) for i in range(training_n)]
    decisions += [temporal["validation_start_ms"] + d * DAY + 1 + i * 100
                  for d in range(24) for i in range(validation_per_day)]
    for idx, stamp in enumerate(decisions):
        key, score = f"opp:{idx:04}", (75.0, 85.0, 95.0)[idx % 3]
        # Both fitting and OOS use the same predeclared event process. TP1 has
        # a label independently of net PnL (middle bin 50%, low 0%, high 100%).
        label = score == 95 or score == 85 and idx % 2 == 0
        population.append({"opportunity_key": key, "decision_ts_ms": stamp,
                           "score": score, "included": True})
        labels.append({"opportunity_key": key, "score": score, "label": label,
            "event": calibration.EVENT_TP1, "decision_ts_ms": stamp,
            "label_available_ts_ms": stamp + 2, "horizon_ms": 10,
            "source": "TEST_ONLY_SYNTHETIC_EVENT", "proven": True})
        base = {"opportunity_id": key, "decision_ts_ms": stamp,
                "result_available_ts_ms": stamp + 2, "net_r": 0.10,
                "admitted": True, "score": score, "playbook": "TREND_PULLBACK"}
        baseline.append(base)
        candidate.append({**base, "net_r": 0.12})
    return {"baseline_rows": baseline, "candidate_rows": candidate,
        "population_rows": population, "event_labels": labels, "temporal": temporal,
        "identity": {field: event.digest(field) for field in event.IDENTITY_FIELDS},
        "now_ms": temporal["as_of_ms"], "test_only": True}


class EventEvidenceTests(unittest.TestCase):
    def test_unknown_family_and_extra_fields_refused(self):
        inputs = evidence_inputs()
        inputs["population_rows"][0]["approved"] = True
        self.assertEqual(event.evaluate_evidence(**inputs)["reason_code"], "EVENT_POPULATION_SCHEMA_INVALID")
        inputs = evidence_inputs()
        inputs["identity"]["model_fingerprint"] = "not-a-hash"
        self.assertEqual(event.evaluate_evidence(**inputs)["reason_code"], "EVENT_EVIDENCE_IDENTITY_INVALID")
        self.assertEqual(event.verify_evidence({"ok": True, "contract": "R10A"})["reason_code"],
                         "EVENT_EVIDENCE_CONTRACT_INVALID")

    def test_holdout_sentinel_is_refused_before_fit(self):
        inputs = evidence_inputs()
        inputs["population_rows"][0]["decision_ts_ms"] = inputs["temporal"]["holdout_start_ms"]
        with mock.patch.object(calibration, "fit_calibration", side_effect=AssertionError("holdout opened")):
            result = event.evaluate_evidence(**inputs)
        self.assertEqual(result["reason_code"], "EVENT_POPULATION_IDENTITY_OR_HOLDOUT_INVALID")

    def test_net_positive_is_not_tp1_or_event_source_proof(self):
        inputs = evidence_inputs()
        inputs["event_labels"][0]["event"] = calibration.EVENT_NET_POSITIVE
        self.assertEqual(event.evaluate_evidence(**inputs)["reason_code"], "EVENT_LABEL_DEFINITION_OR_SOURCE_INVALID")
        inputs = evidence_inputs()
        inputs["event_labels"][0]["source"] = "SYNTHETIC_PREVIEW"
        self.assertEqual(event.evaluate_evidence(**inputs)["reason_code"], "EVENT_LABEL_DEFINITION_OR_SOURCE_INVALID")

    def test_unknown_exclusion_is_symmetric_and_denominator_is_raw(self):
        inputs = evidence_inputs()
        inputs["candidate_rows"][0]["net_r"] = None
        inputs["candidate_rows"][0]["result_available_ts_ms"] = None
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        metrics = result["economic_comparison"]["metrics"]
        self.assertEqual(metrics["baseline_trades"], len(inputs["baseline_rows"]) - 1)
        self.assertEqual(metrics["candidate_trades"], len(inputs["candidate_rows"]) - 1)
        self.assertAlmostEqual(metrics["coverage_pct"], 100 * (len(inputs["population_rows"]) - 1) / len(inputs["population_rows"]))
        self.assertEqual(metrics["unknown_keys"], [inputs["baseline_rows"][0]["opportunity_id"]])

    def test_weak_sample_is_not_wrapper_that_always_blocks_calculation(self):
        result = event.evaluate_evidence(**evidence_inputs())
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["calibration"]["folds"]), 4)
        self.assertEqual(result["walk_forward"]["folds_executed"], 6)
        self.assertTrue(result["walk_forward"]["ci"]["available"])
        self.assertEqual(result["calibration"]["acceptance"]["state"], acceptance.INSUFFICIENT)
        self.assertIn("ACCEPTANCE_TRAIN_SAMPLE_INSUFFICIENT", result["calibration"]["acceptance"]["reason_codes"])

    def test_complete_sample_executes_four_fits_and_approved_oos_policy(self):
        inputs = evidence_inputs(complete=True)
        with mock.patch.object(calibration, "fit_calibration", wraps=calibration.fit_calibration) as fit:
            result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(fit.call_count, 4)
        cal = result["calibration"]
        self.assertEqual(cal["acceptance"]["state"], acceptance.ACCEPTED, cal["acceptance"])
        self.assertTrue(all(f["state"] == calibration.STATE_OOS_VALIDATED for f in cal["folds"]))
        self.assertEqual(result["policy"], acceptance.policy_manifest())
        self.assertTrue(all(f["artifact"]["source"] == event.SOURCE for f in cal["folds"]))
        self.assertTrue(all(f["artifact"]["versions"]["evidence_origin"] == "TEST_ONLY" for f in cal["folds"]))
        self.assertEqual(cal["artifact"], cal["folds"][-1]["artifact"])

    def test_independent_brier_ece_and_economic_delta(self):
        inputs = evidence_inputs(complete=True)
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        predictions = [r for f in result["calibration"]["folds"] for r in f["acceptance_inputs"]["oos_rows"]]
        independent_brier = sum((r["prediction"] - int(r["label"])) ** 2 for r in predictions) / len(predictions)
        independent_reference = sum((r["constant_prediction"] - int(r["label"])) ** 2 for r in predictions) / len(predictions)
        # Exact constructed population has p=(0, .5, 1), four examples per bin
        # per day, so the middle bin contributes .25 for one third of rows.
        self.assertAlmostEqual(independent_brier, 1 / 12)
        self.assertAlmostEqual(independent_reference, 0.25)
        metrics = result["calibration"]["acceptance"]["metrics"]
        self.assertAlmostEqual(metrics["brier"], independent_brier)
        self.assertAlmostEqual(metrics["constant_brier"], independent_reference)
        self.assertAlmostEqual(metrics["ece"], 0)
        walk = result["walk_forward"]
        self.assertEqual(walk["verdict"]["winner"], "CANDIDATE")
        self.assertAlmostEqual(walk["policy_delta"]["value"], 24 * 12 * 0.02)
        self.assertEqual(walk["ci"]["block_size"], 5)
        self.assertEqual(walk["ci"]["samples"], 500)
        self.assertAlmostEqual(metrics["brier_gain_ci"]["point"], 1 / 6)
        self.assertEqual(metrics["brier_gain_ci"]["samples"], 2000)

    def test_both_arms_added_and_removed_use_full_policy(self):
        inputs = evidence_inputs(complete=True)
        # Remove a positive baseline-only trade and introduce a candidate-only
        # trade. Their exact effects enter policy delta, never paired-only delta.
        removed = inputs["candidate_rows"].pop(-1)
        added_base = inputs["baseline_rows"].pop(-2)
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        delta = result["economic_comparison"]["metrics"]["full_policy_delta_r"]
        expected = sum(r["net_r"] for r in inputs["candidate_rows"]) - sum(r["net_r"] for r in inputs["baseline_rows"])
        self.assertAlmostEqual(delta["value"], expected)
        self.assertEqual(delta["only_baseline_n"], 1)
        self.assertEqual(delta["only_candidate_n"], 1)
        self.assertNotEqual(removed["opportunity_id"], added_base["opportunity_id"])

    def test_aa_is_deterministic_and_never_forces_a_winner(self):
        inputs = evidence_inputs()
        inputs["candidate_rows"] = copy.deepcopy(inputs["baseline_rows"])
        first, restarted = event.evaluate_evidence(**inputs), event.evaluate_evidence(**inputs)
        self.assertEqual(first, restarted)
        self.assertIsNone(first["walk_forward"]["verdict"]["winner"])
        self.assertEqual(first["walk_forward"]["folds_running_candidate"], 0)
        self.assertEqual(first["walk_forward"]["policy_delta"]["value"], 0)

    def test_available_after_cut_and_censoring_do_not_become_labels(self):
        inputs = evidence_inputs(complete=True)
        key = inputs["event_labels"][0]["opportunity_key"]
        inputs["event_labels"][0]["label_available_ts_ms"] = inputs["temporal"]["validation_start_ms"] + 1
        inputs["event_labels"][1]["label"] = None
        inputs["event_labels"][1]["label_available_ts_ms"] = None
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertTrue(all(key not in f["artifact"]["training"]["opportunity_keys"] for f in result["calibration"]["folds"]))
        self.assertIn(calibration.LABEL_AFTER_CUTOFF, result["calibration"]["folds"][0]["excluded_train"])
        self.assertIn(calibration.LABEL_UNRESOLVED, result["calibration"]["folds"][0]["excluded_train"])

    def test_unsupported_bin_has_no_probability_inheritance(self):
        inputs = evidence_inputs(complete=True)
        for row in inputs["population_rows"]:
            if row["score"] == 95:
                row["score"] = 85.0
        for row in inputs["event_labels"]:
            if row["score"] == 95:
                row["score"] = 85.0
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["calibration"]["acceptance"]["checks"]["SERVED_TRAIN_AUTHORIZED_BIN_SUPPORT_30"])
        self.assertIsNone(result["calibration"]["artifact"]["bins"][9]["p"])

    def test_score_100_stays_in_last_bin(self):
        inputs = evidence_inputs(complete=True)
        for rows in (inputs["population_rows"], inputs["event_labels"]):
            for row in rows:
                if row["score"] == 95:
                    row["score"] = 100.0
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        artifact = result["calibration"]["artifact"]
        self.assertTrue(artifact["bins"][9]["upper_inclusive"])
        emitted = calibration.predict(artifact, score=100, now_ms=inputs["now_ms"])
        self.assertTrue(emitted["available"])
        self.assertEqual(emitted["bin"], 9)
        self.assertEqual(emitted["probability"], 1)

    def test_bad_oos_is_rejected_without_relaxing_thresholds(self):
        inputs = evidence_inputs(complete=True)
        for row in inputs["event_labels"]:
            if row["decision_ts_ms"] >= inputs["temporal"]["validation_start_ms"]:
                row["label"] = not row["label"]
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["calibration"]["acceptance"]["state"], acceptance.REJECTED)
        self.assertFalse(result["calibration"]["acceptance"]["checks"]["ECE_MAX_005"])
        self.assertFalse(result["calibration"]["acceptance"]["checks"]["BRIER_CI_LOW_POSITIVE"])

    def test_horizon_is_not_a_forced_close_instruction(self):
        inputs = evidence_inputs()
        for rows in (inputs["baseline_rows"], inputs["candidate_rows"]):
            rows[0]["result_available_ts_ms"] += 100
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["economic_comparison"]["metrics"]["horizon_sufficient"])
        self.assertIn(event.wf.HORIZON_INSUFFICIENT, result["walk_forward"]["verdict"]["reason_codes"])
        # The actual ledger-derived result is retained, not replaced by a
        # synthetic timeout exit at the calibration horizon.
        self.assertAlmostEqual(result["economic_comparison"]["metrics"]["baseline"]["net_total_r"],
                               len(inputs["baseline_rows"]) * 0.1)

    def test_synthetic_origin_and_accepted_statistics_never_authorize(self):
        inputs = evidence_inputs(complete=True)
        result = event.evaluate_evidence(**inputs)
        self.assertEqual(result["calibration"]["acceptance"]["state"], acceptance.ACCEPTED)
        self.assertEqual(result["acceptance"]["state"], acceptance.INSUFFICIENT)
        self.assertIsNone(result["economic_comparison"]["metrics"]["gate"]["criteria"].get("fidelity_observed"))
        for field in ("real_study_allowed", "promotable", "authorizes_live"):
            self.assertIs(result[field], False)
        self.assertFalse(result["acceptance"]["authorizes_shadow"])
        self.assertFalse(result["acceptance"]["authorizes_promotion"])
        self.assertFalse(result["acceptance"]["authorizes_canary"])
        for component in (result["walk_forward"], result["economic_comparison"], result["calibration"],
                          result["acceptance"]["protection"], result["evaluation_protocol"]):
            self.assertTrue(component["test_only"])
            self.assertFalse(component["real_study_allowed"])
        inputs["test_only"] = False
        self.assertEqual(event.evaluate_evidence(**inputs)["reason_code"], "EVENT_SYNTHETIC_ORIGIN_CANNOT_BECOME_REAL")
        for row in inputs["event_labels"]:
            row["source"] = "ATTRIBUTED_EXECUTION_EVENT"
        # Pure numerical computation is available to a future officially
        # verified observed caller, but this function grants no authority.
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["real_study_allowed"])
        self.assertFalse(result["authorizes_live"])

    def test_full_verification_rejects_rehashed_result_and_new_inputs(self):
        inputs = evidence_inputs()
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(event.verify_evidence(result, inputs=inputs)["ok"])
        forged = copy.deepcopy(result)
        forged["walk_forward"]["policy_delta"]["value"] += 1
        forged["evidence_hash"] = event.digest({k: v for k, v in forged.items() if k != "evidence_hash"})
        self.assertEqual(event.verify_evidence(forged, inputs=inputs)["reason_code"], "EVENT_EVIDENCE_RECOMPUTATION_DRIFT")
        changed = copy.deepcopy(inputs)
        changed["candidate_rows"][0]["net_r"] += 0.01
        self.assertEqual(event.verify_evidence(result, inputs=changed)["reason_code"], "EVENT_EVIDENCE_RECOMPUTATION_DRIFT")
        new = event.evaluate_evidence(**changed)
        self.assertNotEqual(new["input_hashes"]["candidate_rows"], result["input_hashes"]["candidate_rows"])
        self.assertNotEqual(new["evidence_hash"], result["evidence_hash"])

    def test_controlled_store_rejects_extra_fields_and_family_mixing(self):
        result = event.evaluate_evidence(**evidence_inputs())
        extra = {**result, "approved": True}
        extra["evidence_hash"] = event.digest({k: v for k, v in extra.items() if k != "evidence_hash"})
        self.assertEqual(event.verify_evidence(extra)["reason_code"], "EVENT_EVIDENCE_CONTRACT_INVALID")
        mixed = copy.deepcopy(result)
        mixed["calibration"]["source"] = "R10A_REPLAY_FROM_OFFICIAL_EXPORT"
        mixed["evidence_hash"] = event.digest({k: v for k, v in mixed.items() if k != "evidence_hash"})
        self.assertEqual(event.verify_evidence(mixed)["reason_code"], "EVENT_CALIBRATION_FAMILY_INVALID")

    def test_json_roundtrip_full_verification_and_rehashed_tamper(self):
        inputs = evidence_inputs(complete=True)
        result = event.evaluate_evidence(**inputs)
        self.assertTrue(result["ok"], result)
        loaded = json.loads(json.dumps(result, allow_nan=False))
        self.assertEqual(loaded, result)
        for codes in (result["walk_forward"]["verdict"]["reason_codes"],
                      result["economic_comparison"]["metrics"]["gate"]["reason_codes"],
                      result["acceptance"]["economics"]["metrics"]["gate"]["reason_codes"]):
            self.assertIsInstance(codes, list)
        # Canonical hashing is identical for the native tuple and its stored
        # JSON-array representation; no legacy artifact or hash is migrated.
        self.assertEqual(event.digest({"reason_codes": ("OK",)}),
                         event.digest({"reason_codes": ["OK"]}))
        self.assertTrue(event.verify_evidence(loaded, inputs=inputs)["ok"])
        loaded["walk_forward"]["policy_delta"]["value"] += 1
        loaded["evidence_hash"] = event.digest({k: v for k, v in loaded.items() if k != "evidence_hash"})
        self.assertEqual(event.verify_evidence(loaded, inputs=inputs)["reason_code"],
                         "EVENT_EVIDENCE_RECOMPUTATION_DRIFT")

    def test_read_only_verifier_never_replays_fits_or_bootstraps(self):
        result = event.evaluate_evidence(**evidence_inputs())
        with mock.patch.object(calibration, "fit_calibration", side_effect=AssertionError("fit in read")), \
             mock.patch.object(event.wf, "run_walk_forward", side_effect=AssertionError("WF in read")), \
             mock.patch.object(acceptance, "_bootstrap", side_effect=AssertionError("bootstrap in read")):
            checked = event.verify_evidence(result)
        self.assertTrue(checked["ok"], checked)
        self.assertFalse(checked["recomputed"])


if __name__ == "__main__":
    unittest.main()
