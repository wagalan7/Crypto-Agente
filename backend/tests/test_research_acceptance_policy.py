"""Synthetic, offline proof of the frozen acceptance rules; not evidence LIVE."""
import copy
import json
import unittest
from unittest.mock import patch

from services import research_acceptance_service as acceptance
from services import research_manifest_service as rm
from services import research_study_service as study
from services import score_v3_calibration_service as calibration
from services import score_v3_service as score
from services import preselection_experiment_service as catalog
from services import portfolio_replay_service as portfolio
from services import offline_replay_service as replay
from services import walk_forward_service as wf
from services import strategy_evidence_service as evidence_service
from tests.test_lote02_manifest_binding import frozen_manifest, contract_inputs
from tests.test_lote02_research_manifest import T0, BAR5

DAY = acceptance.DAY_MS


def policy_report(*, different_folds=False):
    """Build real V1 fit/OOS/WF contracts with explicit synthetic rows.

    The manifest is TEST_ONLY and never becomes operational authorization.
    This is deliberately not the official exporter or production data.
    """
    raw = {k: copy.deepcopy(v) for k, v in frozen_manifest().items() if k in rm.MANIFEST_FIELDS}
    raw["hashes"] = None
    raw["candidate"]["selection_rule"]["min_score"] = 70.0
    raw["baseline"]["playbooks"] = raw["candidate"]["playbooks"] = ["TREND_PULLBACK"]
    raw["split"].update(validation_start_ms=T0 + 21 * DAY,
                        holdout_start_ms=T0 + 49 * DAY, as_of_ms=T0 + 50 * DAY)
    manifest = rm.parse_manifest(raw)
    request = study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=30 * DAY)
    now, management = manifest["split"]["as_of_ms"], manifest["candidate"]["management_config"]
    identity = {"manifest_hash": manifest["manifest_hash"], "dataset_hash": "d" * 64,
        "price_hash": "e" * 64, "calibration_request": request,
        "baseline": manifest["baseline"]["management_config"], "candidate": management,
        "costs": manifest["costs"]["config"], "split": manifest["split"]}
    key = study.digest(identity)
    fingerprint = score.model_fingerprint(playbook="TREND_PULLBACK", config=score.DEFAULT_CONFIG)
    versions = {"study": study.STUDY_VERSION, "manifest": manifest["manifest_hash"],
                "prices": identity["price_hash"], "costs": manifest["costs"]["config"]["config_hash"]}
    training = []
    for index, successes in ((1, 40), (7, 20), (8, 40), (9, 60)):
        for number in range(80):
            stamp = T0 + DAY + (index * 80 + number) * BAR5
            training.append({"opportunity_key": f"train-{index}-{number}", "score": index * 10 + 5.,
                "label": number < successes, "decision_ts_ms": stamp,
                "label_available_ts_ms": stamp + BAR5, "event": calibration.EVENT_TP1})
    all_rows, folds = list(training), []
    for fold in range(4):
        lo, hi = T0 + (21 + fold * 7) * DAY, T0 + (28 + fold * 7) * DAY
        cutoff = lo - (manifest["split"]["purge_bars"] + manifest["split"]["embargo_bars"]) * BAR5
        fold_train = copy.deepcopy(training)
        if different_folds and fold == 3:
            for r in fold_train:
                if calibration.bin_index(r["score"]) in (7, 9):
                    number = int(r["opportunity_key"].rsplit("-", 1)[1])
                    r["label"] = number < (40 if calibration.bin_index(r["score"]) == 7 else 40)
        fit = calibration.fit_calibration(fold_train, event=calibration.EVENT_TP1,
            population="RESEARCH_SHADOW", model_fingerprint=fingerprint, score_config_hash=fingerprint,
            horizon_bars=management["entry_window_bars"] + management["max_holding_bars"] - 1,
            bar_ms=BAR5, censoring=request["censoring"], payoff_ref=management["config_hash"],
            source="R10A_REPLAY_FROM_OFFICIAL_EXPORT", dataset_hash=identity["dataset_hash"],
            cutoff_ms=cutoff, generated_at_ms=now, valid_until_ms=now + request["valid_for_ms"], versions=versions)
        oos = []
        for index, successes in ((7, 10), (8, 20), (9, 30)):
            if different_folds and fold == 3:
                successes = 20
            for number in range(40):
                stamp = lo + DAY + number * 3 * 3_600_000 + index * 1000
                oos.append({"opportunity_key": f"oos-{fold}-{index}-{number}", "score": index * 10 + 5.,
                    "label": number < successes, "decision_ts_ms": stamp,
                    "label_available_ts_ms": stamp + BAR5, "event": calibration.EVENT_TP1})
        validated = calibration.validate_out_of_sample(fit["artifact"], oos, now_ms=now)
        artifact = validated["artifact"]
        constant = sum(b["successes"] for b in artifact["bins"]) / artifact["coverage"]["unique_usable"]
        emitted = [{**{k: v for k, v in r.items() if k != "event"},
                    "prediction": artifact["bins"][calibration.bin_index(r["score"])]["p"],
                    "constant_prediction": constant, "bin": calibration.bin_index(r["score"])} for r in oos]
        folds.append({"fold": fold, "train_cutoff_ms": cutoff, "oos_start_ms": lo, "oos_end_ms": hi,
            "train_rows": len(fold_train), "oos_rows": len(oos), "state": validated["state"],
            "reason_code": validated["reason_code"], "artifact": artifact, "oos": validated["oos"],
            "acceptance_inputs": {"eligible_count": len(oos), "oos_rows": emitted}})
        all_rows.extend(oos)
    costs = replay.CostConfig(**{k: manifest["costs"]["config"][k] for k in
                               ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")})
    executions, labels = {}, {}
    for side, net in (("baseline", 0.1), ("candidate", 0.100001)):
        trades = [{"opportunity_id": r["opportunity_key"], "admitted": True,
                   "net_r": net, "result_available_ts_ms": r["decision_ts_ms"] + BAR5,
                   "playbook": "TREND_PULLBACK"} for r in all_rows]
        executions[side] = {"portfolio_version": portfolio.PORTFOLIO_VERSION,
            "trades": trades, "admitted": len(trades), "metrics": portfolio.portfolio_metrics([net] * len(trades)),
            "costs": portfolio.cost_status(costs),
            "config_hash": portfolio._hash({"model": portfolio.ExecutionModel().as_dict(),
                "portfolio": portfolio.PortfolioConfig().as_dict(), "replay": management["config_hash"],
                "costs": manifest["costs"]["config"]["config_hash"]})}
        labels[side] = [{"opportunity_id": r["opportunity_key"], "decision_ts_ms": r["decision_ts_ms"],
                        "net_r": net, "result_available_ts_ms": r["decision_ts_ms"] + BAR5} for r in all_rows]
    protocol = study._evaluation_protocol(manifest)
    windows = [wf.Fold(**definition) for definition in protocol["economic_fold_plan"]]
    walk = wf.run_walk_forward(baseline=labels["baseline"], candidate=labels["candidate"], folds=windows,
        bar_ms=BAR5, horizon_bars=27, embargo_bars=2, costs_complete=True, horizon_sufficient=True,
        **protocol["economic_bootstrap"])
    evidence = catalog.gate_evidence_from_study(replay=executions["candidate"], study=walk,
        trades=executions["candidate"]["trades"], enabled_playbooks=["TREND_PULLBACK"],
        window_start_ms=T0, window_end_ms=manifest["split"]["holdout_start_ms"],
        coverage_pct=100., operational_failures=0, economic_duplicates=0,
        unresolved_protection_failures=0, fidelity_discrepancy_pct=0.)
    evidence.update(operational_applicable=800, operational_observed=800, operational_coverage_pct=100.,
        fidelity_comparable=800, fidelity_denominator=800, fidelity_divergences=0, fidelity_coverage_pct=100.,
        protection_scope="SHADOW_SIMULATED", protection_proves_real_sl=False,
        protection_applicable=800, protection_observed=800, protection_coverage_pct=100., protection_pending=0)
    contract_kwargs = contract_inputs(manifest)
    contract_kwargs["dataset_fingerprint"] = identity["dataset_hash"]
    contract = catalog.preselection_contract(**contract_kwargs)
    gate = catalog.go_no_go(evidence)
    official = catalog.study_payload(contract=contract, evidence=evidence, gate=gate,
        study=walk, replay=executions["candidate"], evidence_key=key)
    artifact = folds[-1]["artifact"]
    report = {"ok": True, "version": study.STUDY_VERSION, "kind": study.PAYLOAD_KIND,
        "study_key": key, "manifest": manifest, "identity": identity,
        "selection": {"coverage": {"rows_total": len(all_rows)}},
        "baseline_replay": executions["baseline"], "candidate_replay": executions["candidate"],
        "walk_forward": walk, "gate": gate, "study": official, "evaluation_protocol": protocol,
        "calibration": {"artifact": artifact, "folds": folds,
            "request": request, "payoff_evidence": None, "net_ev": None,
            "acceptance_population": {"contract": "R13_ACCEPTANCE_POPULATION_V1", "eligible_count": 480,
                "export_denominator_complete": True,
                "excluded_by_selection": 0, "replay_rows": 480, "fold_eligible_counts": [120] * 4,
                "source_rows": len(all_rows), "index": [{"opportunity_key": r["opportunity_key"],
                    "decision_ts_ms": r["decision_ts_ms"]} for r in all_rows]}},
        "artifact": artifact, "observed_at_ms": now, "real_study_allowed": False,
        "promotable": False, "live_changed": False, "holdout_status": "SEALED", "source_assurance": "TEST_ONLY"}
    report["acceptance"] = acceptance.build_acceptance(report, now_ms=now)
    return report


def prospective_snapshot(report):
    """An explicit TEST_ONLY numerical snapshot for pure-contract tests.

    This helper never writes it to the official store; governance integration
    independently derives snapshots from actual synthetic annotation rows.
    """
    contract = report["study"]["contract"]
    envelope = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
        contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
    forward = copy.deepcopy(report["study"]["evidence"])
    forward["essential_gaps"] = ["BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED"]
    measured = {k: copy.deepcopy(v) for k, v in forward.items() if k.startswith("protection_") or k.startswith("fidelity_")}
    measured.update(operational_failures=0, economic_duplicates=0, unresolved_protection_failures=0,
        measured_until_ms=report["observed_at_ms"], source="PROSPECTIVE_ANNOTATIONS_OFFICIAL_RESOLVER")
    commitments = [{"opportunity_key": f"synthetic-anchor-{index:04d}",
        "annotation_hash": "a" * 64, "observation_hash": "b" * 64, "included": True} for index in range(800)]
    body = {"version": "R13_ACCEPTANCE_PROSPECTIVE_V2", "source": "REAL_PROSPECTIVE", "available": True,
        "identity": {"experiment_id": 1, "experiment_key": "SYNTHETIC_ACCEPTANCE_TEST_ONLY",
            "candidate_hash": evidence_service.canonical_hash(envelope),
            "champion_hash": evidence_service.canonical_hash(contract["baseline_config"]),
            "generation": 1, "approval_id": "a" * 64, "started_at_ms": report["observed_at_ms"] - 20 * DAY,
            "manifest_hash": report["manifest"]["manifest_hash"], "contract_hash": contract["contract_hash"],
            "study_key": report["study_key"], "bundle_hash": "b" * 64},
        "cutoff_ms": report["observed_at_ms"], "cohort_hash": acceptance.digest(commitments),
        "cohort_commitments": commitments,
        "evidence": forward, "gate": catalog.go_no_go(forward), "measurements": measured,
        "data_quality": {"raw": 800, "valid": 800, "excluded_by_reason": {}}}
    return {**body, "snapshot_hash": acceptance.digest(body)}


class AcceptancePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = policy_report()
        cls.now = cls.report["observed_at_ms"]

    def rebuilt(self, mutate):
        report = copy.deepcopy(self.report)
        mutate(report)
        report["acceptance"] = acceptance.build_acceptance(report, now_ms=self.now)
        return report

    def test_frozen_metadata_and_exact_analytic_positive(self):
        policy = acceptance.policy_manifest()
        self.assertEqual(policy["uncertainty"]["samples"], 2000)
        self.assertEqual(policy["hypothesis"]["min_score"], 70)
        self.assertEqual(policy["valid_for_ms"], 30 * DAY)
        self.assertFalse(policy["live_license"])
        record = self.report["acceptance"]
        self.assertEqual(record["calibration"]["state"], acceptance.ACCEPTED, record)
        self.assertEqual(record["economics"]["state"], acceptance.INSUFFICIENT)
        self.assertEqual(record["protection"]["state"], acceptance.INSUFFICIENT)
        self.assertTrue(acceptance.verify_acceptance(self.report, self.now, purpose="STRUCTURAL", recompute=True)["ok"])
        self.assertFalse(acceptance.verify_acceptance(self.report, self.now)["ok"])
        self.assertEqual(record, acceptance.build_acceptance(self.report, self.now))
        self.assertEqual(self.report["artifact"]["approval"]["state"], "NOT_APPROVED")
        restored = json.loads(json.dumps(self.report))
        self.assertTrue(acceptance.verify_acceptance(restored, self.now, purpose="STRUCTURAL", recompute=True)["ok"])

    def test_cheap_reads_do_not_run_bootstrap_even_with_missing_ci(self):
        with patch.object(acceptance, "_bootstrap", side_effect=AssertionError("GET bootstrap")), \
             patch.object(wf, "block_bootstrap_ci", side_effect=AssertionError("GET economic bootstrap")):
            self.assertTrue(acceptance.verify_acceptance(self.report, self.now, purpose="STRUCTURAL")["ok"])
            broken = copy.deepcopy(self.report)
            del broken["acceptance"]["calibration"]["metrics"]["brier_gain_ci"]
            broken["acceptance"]["record_hash"] = acceptance.digest({k: v for k, v in broken["acceptance"].items() if k != "record_hash"})
            self.assertFalse(acceptance.verify_acceptance(broken, self.now, purpose="STRUCTURAL")["ok"])

    def test_all_fold_predictions_not_last_artifact(self):
        report = policy_report(different_folds=True)
        record = report["acceptance"]["calibration"]
        self.assertEqual(record["state"], acceptance.ACCEPTED, record)
        bin7 = next(b for b in record["metrics"]["bins"] if b["bin"] == 7)
        self.assertEqual(bin7["predicted"], 0.3125)
        self.assertNotEqual(bin7["predicted"], report["artifact"]["bins"][7]["p"])
        self.assertEqual(record["metrics"]["oos_unique"], 480)
        self.assertGreater(record["metrics"]["brier_gain_ci"]["low"], 0)

    def test_invalid_numeric_bool_label_constant_and_probability(self):
        for field, value in (("score", float("nan")), ("prediction", True), ("constant_prediction", False),
                             ("label", 1), ("bin", True), ("prediction", 0.99), ("constant_prediction", 0.25)):
            with self.subTest(field=field, value=value):
                report = self.rebuilt(lambda r: r["calibration"]["folds"][0]["acceptance_inputs"]["oos_rows"][0].update({field: value}))
                self.assertEqual(report["acceptance"]["calibration"]["state"], acceptance.INVALID)
                self.assertFalse(acceptance.verify_acceptance(report, self.now)["ok"])

    def test_chronology_duplicate_overlap_and_population_denominator(self):
        changes = [lambda r: r["calibration"]["folds"][0]["acceptance_inputs"]["oos_rows"][0].update(label_available_ts_ms=self.now + 1),
            lambda r: r["calibration"]["folds"][0]["acceptance_inputs"]["oos_rows"].append(copy.deepcopy(r["calibration"]["folds"][0]["acceptance_inputs"]["oos_rows"][0])),
            lambda r: r["calibration"]["acceptance_population"].update(eligible_count=1),
            lambda r: r["calibration"]["folds"][0]["acceptance_inputs"].update(eligible_count=True),
            lambda r: r["calibration"]["folds"][0]["acceptance_inputs"]["oos_rows"][0].update(opportunity_key="train-7-0")]
        for change in changes:
            report = self.rebuilt(change)
            self.assertEqual(report["acceptance"]["calibration"]["state"], acceptance.INVALID)

    def test_missing_fold_or_inputs_never_becomes_zero(self):
        for change in (lambda r: r["calibration"]["folds"].pop(),
                       lambda r: r["calibration"]["folds"][0].pop("acceptance_inputs")):
            report = self.rebuilt(change)
            self.assertEqual(report["acceptance"]["calibration"]["state"], acceptance.INSUFFICIENT)
        report = self.rebuilt(lambda r: r["calibration"]["acceptance_population"].update(export_denominator_complete=False))
        self.assertEqual(report["acceptance"]["calibration"]["state"], acceptance.INSUFFICIENT)
        self.assertIn("ACCEPTANCE_EXPORT_DENOMINATOR_UNRECONCILED", report["acceptance"]["calibration"]["reason_codes"])

    def test_resealed_training_ids_and_times_must_match_official_index(self):
        for change in (lambda t: t["opportunity_keys"].__setitem__(0, "foreign-training-opportunity"),
                       lambda t: t["opportunity_keys"].__setitem__(0, "oos-3-7-0"),
                       lambda t: t.update(decision_min_ms=t["decision_min_ms"] + 1),
                       lambda t: t.update(decision_max_ms=t["decision_max_ms"] - 1)):
            with self.subTest(change=change):
                report = copy.deepcopy(self.report)
                artifact = report["calibration"]["folds"][0]["artifact"]
                change(artifact["training"])
                artifact["training"]["opportunity_keys"].sort()
                artifact["artifact_hash"] = calibration.recompute_hash(artifact)
                report["acceptance"] = acceptance.build_acceptance(report, self.now)
                result = report["acceptance"]["calibration"]
                self.assertEqual(result["state"], acceptance.INVALID)
                self.assertIn("ACCEPTANCE_TRAIN_IDENTITY_OR_LEAKAGE_INVALID", result["reason_codes"])

    def test_no_support_or_coverage_is_insufficient_not_accepted(self):
        report = copy.deepcopy(self.report)
        population = report["calibration"]["acceptance_population"]
        lo = report["calibration"]["folds"][0]["oos_start_ms"]
        for n in range(60):
            population["index"].append({"opportunity_key": f"censored-{n}", "decision_ts_ms": lo + 2 * DAY + n * 1000})
        population["eligible_count"] += 60
        population["source_rows"] += 60
        population["fold_eligible_counts"][0] += 60
        population["excluded_by_selection"] += 60
        report["selection"]["coverage"]["rows_total"] += 60
        report["calibration"]["folds"][0]["acceptance_inputs"]["eligible_count"] += 60
        report["acceptance"] = acceptance.build_acceptance(report, self.now)
        state = report["acceptance"]["calibration"]
        self.assertEqual(state["state"], acceptance.INSUFFICIENT)
        self.assertAlmostEqual(state["metrics"]["coverage"], 480 / 540)

    def test_identity_drift_expired_revoked_and_resealed_interval(self):
        for change in (lambda r: r["identity"].update(price_hash="a" * 64),
                       lambda r: r["acceptance"].update(policy_hash="a" * 64),
                       lambda r: r["acceptance"].update(revoked=True)):
            report = copy.deepcopy(self.report)
            change(report)
            record = report["acceptance"]
            record["record_hash"] = acceptance.digest({k: v for k, v in record.items() if k != "record_hash"})
            self.assertFalse(acceptance.verify_acceptance(report, self.now, purpose="STRUCTURAL")["ok"])
        self.assertFalse(acceptance.verify_acceptance(self.report, self.now + 30 * DAY + 1)["ok"])
        expiry = self.report["acceptance"]["valid_until_ms"]
        self.assertTrue(acceptance.verify_acceptance(self.report, expiry - 1, purpose="STRUCTURAL")["ok"])
        self.assertEqual(acceptance.verify_acceptance(self.report, expiry, purpose="STRUCTURAL")["reason_code"],
                         "ACCEPTANCE_EXPIRED")
        resealed = copy.deepcopy(self.report)
        resealed["acceptance"]["calibration"]["metrics"]["brier_gain_ci"]["low"] += 0.001
        resealed["acceptance"]["record_hash"] = acceptance.digest({k: v for k, v in resealed["acceptance"].items() if k != "record_hash"})
        self.assertFalse(acceptance.verify_acceptance(resealed, self.now, purpose="STRUCTURAL", recompute=True)["ok"])

    def test_malformed_reports_and_snapshots_fail_closed_without_exceptions(self):
        for bad in (None, [], {}, {"ok": True}, {**self.report, "artifact": []}):
            record = acceptance.build_acceptance(bad, self.now)
            self.assertNotEqual(record["calibration"]["state"], acceptance.ACCEPTED)
        snapshot = prospective_snapshot(self.report)
        snapshot["measurements"]["fidelity_discrepancy_pct"] = float("nan")
        record = acceptance.build_acceptance(self.report, self.now, prospective_evidence=snapshot)
        self.assertEqual(record["economics"]["state"], acceptance.INVALID)
        self.assertIsNone(record["prospective_evidence"])

    def test_economic_strings_and_zero_without_observation_are_not_proof(self):
        report = self.rebuilt(lambda r: r["study"]["evidence"].pop("operational_observed"))
        self.assertEqual(report["acceptance"]["economics"]["state"], acceptance.INSUFFICIENT)
        report = self.rebuilt(lambda r: r["candidate_replay"]["metrics"].update(net_expectancy_r=99))
        self.assertEqual(report["acceptance"]["economics"]["state"], acceptance.INVALID)
        report = self.rebuilt(lambda r: r["study"]["evidence"].update(operational_failures=False))
        self.assertEqual(report["acceptance"]["economics"]["state"], acceptance.INVALID)
        self.assertFalse(self.report["acceptance"]["protection"]["proves_real_sl"])
        self.assertFalse(self.report["acceptance"]["protection"]["authorizes_canary"])

    def test_full_policy_execution_and_economic_ci_cannot_be_resealed_at_ingestion(self):
        for change in (lambda r: r["walk_forward"]["folds"][0].update(candidate_selected_on_train=False),
                       lambda r: r["walk_forward"]["paired"]["paired"][0].update(candidate_net_r=99),
                       lambda r: r["walk_forward"]["ci"].update(low=0.01),
                       lambda r: r["candidate_replay"]["trades"][0].update(result_available_ts_ms=True)):
            report = self.rebuilt(change)
            self.assertEqual(report["acceptance"]["economics"]["state"], acceptance.INVALID)

    def test_forward_snapshot_binds_separate_observations_without_summing_cohorts(self):
        report = copy.deepcopy(self.report)
        forward = prospective_snapshot(report)
        report["acceptance"] = acceptance.build_acceptance(report, self.now, prospective_evidence=forward)
        record = report["acceptance"]
        self.assertEqual(record["economics"]["state"], acceptance.ACCEPTED, record["economics"])
        self.assertEqual(record["protection"]["state"], acceptance.ACCEPTED, record["protection"])
        self.assertEqual(record["economics"]["metrics"]["candidate_trades"], 800)
        self.assertEqual(record["binding"]["cohort_hash"], forward["cohort_hash"])
        self.assertTrue(acceptance.verify_acceptance(report, self.now, purpose="STRUCTURAL", recompute=True)["ok"])
        restored = json.loads(json.dumps(report))
        self.assertTrue(acceptance.verify_acceptance(restored, self.now, purpose="STRUCTURAL", recompute=True)["ok"])
        self.assertFalse(acceptance.verify_acceptance(report, self.now, purpose="CANARY")["ok"])
        for field, value in (("fidelity_discrepancy_pct", False), ("protection_pending", True)):
            damaged = copy.deepcopy(forward)
            damaged["measurements"][field] = value
            damaged["snapshot_hash"] = acceptance.digest({k: v for k, v in damaged.items() if k != "snapshot_hash"})
            broken = acceptance.build_acceptance(report, self.now, prospective_evidence=damaged)
            self.assertNotEqual(broken["economics"]["state"], acceptance.ACCEPTED)

    def test_prospective_identity_and_only_protection_scope_gap_can_be_removed(self):
        for change in (lambda s: s["identity"].update(manifest_hash="f" * 64),
                       lambda s: s["identity"].update(started_at_ms=self.now + 1),
                       lambda s: s["data_quality"].update(valid=1)):
            snapshot = prospective_snapshot(self.report)
            change(snapshot)
            snapshot["snapshot_hash"] = acceptance.digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"})
            record = acceptance.build_acceptance(self.report, self.now, prospective_evidence=snapshot)
            self.assertEqual(record["economics"]["state"], acceptance.INVALID)
        snapshot = prospective_snapshot(self.report)
        snapshot["evidence"]["essential_gaps"].append("UNRESOLVED_EXECUTION_FAILURE")
        snapshot["gate"] = catalog.go_no_go(snapshot["evidence"])
        snapshot["snapshot_hash"] = acceptance.digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"})
        record = acceptance.build_acceptance(self.report, self.now, prospective_evidence=snapshot)
        self.assertNotEqual(record["economics"]["state"], acceptance.ACCEPTED)

    def test_economic_support_floors_are_insufficient_but_bad_payoff_is_rejected(self):
        for updates, reason, expected in (
                ({"total_shadow_trades": 10, "trades_per_playbook": {"TREND_PULLBACK": 10}},
                 catalog.SAMPLE_INSUFFICIENT, acceptance.INSUFFICIENT),
                ({"calendar_days": 13}, catalog.DURATION_INSUFFICIENT, acceptance.INSUFFICIENT),
                ({"business_days": 9}, catalog.BUSINESS_DAYS_INSUFFICIENT, acceptance.INSUFFICIENT),
                ({"net_ev_r": -0.1}, catalog.EV_INSUFFICIENT, acceptance.REJECTED),
                ({"drawdown_r": 9.0}, catalog.DRAWDOWN_EXCEEDED, acceptance.REJECTED)):
            with self.subTest(updates=updates):
                snapshot = prospective_snapshot(self.report)
                snapshot["evidence"].update(updates)
                snapshot["gate"] = catalog.go_no_go(snapshot["evidence"])
                snapshot["snapshot_hash"] = acceptance.digest({
                    k: v for k, v in snapshot.items() if k != "snapshot_hash"})
                record = acceptance.build_acceptance(self.report, self.now, prospective_evidence=snapshot)
                self.assertEqual(record["economics"]["state"], expected, record["economics"])
                self.assertIn(reason, record["economics"]["reason_codes"])
                self.assertFalse(record["economics"]["checks"]["PROSPECTIVE_GO_NO_GO_EXISTING"])

    def test_closed_prospective_v2_commitments_reject_legacy_and_resealed_drift(self):
        for change in (
                lambda s: s.update(version="R13_ACCEPTANCE_PROSPECTIVE_V1"),
                lambda s: s.pop("cohort_commitments"),
                lambda s: s["cohort_commitments"].pop(),
                lambda s: s["cohort_commitments"][0].update(included=1),
                lambda s: s["cohort_commitments"][0].update(included=False),
                lambda s: s["cohort_commitments"][0].update(opportunity_key="synthetic-anchor-0001"),
                lambda s: s["cohort_commitments"][0].update(observation_hash="not-a-hash"),
                lambda s: s["cohort_commitments"][0].update(unknown=True),
                lambda s: s.update(cohort_hash="c" * 64)):
            with self.subTest(change=change):
                snapshot = prospective_snapshot(self.report)
                change(snapshot)
                snapshot["snapshot_hash"] = acceptance.digest({
                    k: v for k, v in snapshot.items() if k != "snapshot_hash"})
                record = acceptance.build_acceptance(self.report, self.now, prospective_evidence=snapshot)
                self.assertEqual(record["economics"]["state"], acceptance.INVALID)
                self.assertEqual(record["protection"]["state"], acceptance.INVALID)


if __name__ == "__main__":
    unittest.main()
