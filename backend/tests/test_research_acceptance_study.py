"""Aceite aditivo: produtor oficial, denominadores e registro imutável.

Exportadores, seleção, replay e calibração reais; apenas preços e sessões são
sintéticos. Nenhuma fixture ou manifesto TEST_ONLY vira evidência operacional.
"""
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import policy_state_service as state
from services import research_study_service as study
from services import research_acceptance_service as acceptance
from services import research_dataset_service as dataset_service
from services import research_manifest_service as manifest_service
from services import offline_replay_service as replay
from services import score_v3_calibration_service as calibration
from tests.test_lote02_official_study_cycle import official_fixture
from tests.test_lote02_selection_scope import BAR5

VALIDITY = 30 * 86400000
INPUT_FIELDS = {"opportunity_key", "score", "label", "prediction", "constant_prediction",
                "bin", "decision_ts_ms", "label_available_ts_ms"}


def hypothesis_fixture(*, training=240, validation=96):
    """Mesma fonte sintética com hipótese 70 pré-fixada no export oficial."""
    manifest, _, _, prices, request, source = official_fixture(
        training=training, validation=validation, return_source=True)
    body = {k: copy.deepcopy(v) for k, v in manifest.items() if k in manifest_service.MANIFEST_FIELDS}
    body["hashes"] = None
    body["candidate"]["selection_rule"]["min_score"] = 70.0
    manifest = manifest_service.parse_manifest(body)
    request = replace(request, selection=manifest_service.selection_config_of(manifest["candidate"]))
    plan = dataset_service.plan_selection(request, [(r["opportunity_key"], r["decision_at"]) for r in source])
    dataset, exported = dataset_service.build_feature_artifacts(request, plan, source, {"holdout_sealed": 17})
    prices = study.build_price_contract(dataset_hash=study.digest(dataset), source="SYNTHETIC_TEST_ONLY",
        bar_ms=BAR5, as_of_ms=manifest["split"]["as_of_ms"], windows=prices["windows"],
        quotes=prices["quotes"], test_only=True)
    return manifest, dataset, exported, prices


def run_fixture(*, training=240, validation=96, event=calibration.EVENT_TP1):
    manifest, dataset, exported, prices = hypothesis_fixture(training=training, validation=validation)
    return study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported, prices=prices,
        request=study.calibration_request(event=event, valid_for_ms=VALIDITY),
        now_ms=manifest["split"]["as_of_ms"])


def result_rows(rows, *, positive=True):
    return {r["opportunity_key"]: {"net_r": 1.0 if positive else -1.0, "filled": True,
        "result_available_ts_ms": r["decision_ts_ms"] + BAR5,
        "status": "CLOSED_TP2" if positive else "CLOSED_STOP",
        "tp1_hit": positive, "exits": [{"reason": "TP2"}] if positive else [{"reason": "STOP"}]}
        for r in rows}


class AcceptanceProducer(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_official_run_emits_four_frozen_folds_without_holdout(self):
        report = run_fixture()
        self.assertTrue(report["ok"], report)
        folds = report["calibration"]["folds"]
        self.assertEqual(len(folds), 4)
        self.assertEqual(report["evaluation_protocol"]["calibration_folds"], 4)
        self.assertEqual(report["evaluation_protocol"]["economic_folds"], 6)
        self.assertEqual(len(report["walk_forward"]["folds"]), 6)
        self.assertLessEqual(max(f["test_end_ms"] for f in report["evaluation_protocol"]["economic_fold_plan"]),
                             report["manifest"]["split"]["holdout_start_ms"])
        self.assertIn("acceptance", report)
        self.assertNotEqual(report["acceptance"]["calibration"]["state"], "INVALID")
        self.assertTrue(report["acceptance"]["calibration"]["metrics"])
        self.assertTrue(study.validate_study_report(report, now_ms=report["observed_at_ms"])["ok"])
        self.assertFalse(acceptance.verify_acceptance(report, now_ms=report["observed_at_ms"])["ok"])
        seen = set()
        population = report["calibration"]["acceptance_population"]
        self.assertIs(population["export_denominator_complete"], True)
        self.assertEqual(population["fold_eligible_counts"], [f["acceptance_inputs"]["eligible_count"] for f in folds])
        self.assertEqual(population["eligible_count"], sum(population["fold_eligible_counts"]))
        self.assertEqual(population["source_rows"], report["selection"]["coverage"]["rows_total"])
        for fold in folds:
            inputs, artifact = fold["acceptance_inputs"], fold["artifact"]
            self.assertEqual(set(inputs), {"eligible_count", "oos_rows"})
            self.assertGreaterEqual(inputs["eligible_count"], len(inputs["oos_rows"]))
            self.assertGreaterEqual(len(inputs["oos_rows"]), 20)
            constant = sum(b["successes"] for b in artifact["bins"]) / artifact["coverage"]["unique_usable"]
            self.assertEqual(artifact["approval"]["state"], "NOT_APPROVED")
            self.assertFalse(artifact["approval"]["economically_approved"])
            definition = artifact["event_definition"]
            management = report["manifest"]["candidate"]["management_config"]
            self.assertEqual(definition["event"], calibration.EVENT_TP1)
            self.assertEqual(definition["horizon_bars"], management["entry_window_bars"] + management["max_holding_bars"] - 1)
            for row in inputs["oos_rows"]:
                self.assertEqual(set(row), INPUT_FIELDS)
                self.assertIs(type(row["label"]), bool)
                self.assertNotIn(row["opportunity_key"], seen)
                self.assertNotIn(row["opportunity_key"], artifact["training"]["opportunity_keys"])
                seen.add(row["opportunity_key"])
                self.assertLessEqual(fold["oos_start_ms"], row["decision_ts_ms"])
                self.assertLess(row["decision_ts_ms"], fold["oos_end_ms"])
                self.assertLessEqual(row["label_available_ts_ms"], fold["oos_end_ms"])
                self.assertLess(row["decision_ts_ms"], report["manifest"]["split"]["holdout_start_ms"])
                self.assertEqual(row["constant_prediction"], constant)
                self.assertEqual(row["prediction"], artifact["bins"][row["bin"]]["p"])
        self.assertFalse(report["promotable"])
        self.assertEqual(report["holdout_status"], "SEALED")
        self.assertIsNone(report["calibration"]["net_ev"])

    def test_missing_selection_evidence_stays_in_raw_denominator(self):
        manifest, dataset, exported, prices = hypothesis_fixture(training=240, validation=96)
        row = next(r for r in dataset["rows"] if r["decision_ts_ms"] >= manifest["split"]["validation_start_ms"])
        missing_key = row["opportunity_key"]
        row["features"] = {}
        dataset_hash = study.digest(dataset)
        exported["fingerprints"]["dataset_sha256"] = dataset_hash
        prices["dataset_hash"] = dataset_hash
        prices["price_hash"] = study.digest({k: v for k, v in prices.items() if k != "price_hash"})
        report = study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported, prices=prices,
            request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
            now_ms=manifest["split"]["as_of_ms"])
        self.assertTrue(report["ok"], report)
        self.assertNotEqual(report["acceptance"]["calibration"]["state"], "INVALID")
        population = report["calibration"]["acceptance_population"]
        self.assertEqual(population["excluded_by_selection"], 1)
        self.assertEqual(population["eligible_count"], population["replay_rows"] + 1)
        first = report["calibration"]["folds"][0]["acceptance_inputs"]
        self.assertEqual(first["eligible_count"], len(first["oos_rows"]) + 1)
        self.assertNotIn(missing_key, {r["opportunity_key"] for r in first["oos_rows"]})
        self.assertIn(missing_key, {r["opportunity_key"] for r in population["index"]})

    def test_export_exclusions_without_timestamps_block_acceptance_not_study_record(self):
        manifest, dataset, exported, prices = hypothesis_fixture()
        exported["counts"]["excluded"]["INVALID_SETUP"] = 2
        exported["counts"]["validation_candidates"] += 2
        report = study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported, prices=prices,
            request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
            now_ms=manifest["split"]["as_of_ms"])
        self.assertTrue(report["ok"], report)
        self.assertIs(report["calibration"]["acceptance_population"]["export_denominator_complete"], False)
        self.assertEqual(report["acceptance"]["calibration"]["state"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("ACCEPTANCE_EXPORT_DENOMINATOR_UNRECONCILED", report["acceptance"]["calibration"]["reason_codes"])
        self.assertTrue(study.validate_study_report(report, now_ms=report["observed_at_ms"])["ok"])

    def test_economic_ci_preserves_five_fold_blocks_separate_from_four_calibration_folds(self):
        from tests.research_acceptance_fixture import accepted_calibration_report
        report = accepted_calibration_report()
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["calibration"]["folds"]), 4)
        self.assertEqual(report["evaluation_protocol"]["economic_folds"], 6)
        self.assertEqual(report["walk_forward"]["folds_executed"], 6)
        self.assertTrue(report["walk_forward"]["ci"]["available"])
        self.assertEqual(report["walk_forward"]["ci"]["block_size"], 5)
        self.assertEqual(report["walk_forward"]["ci"]["samples"], 500)

    def test_reference_constant_never_uses_future_labels_or_last_artifact(self):
        manifest, dataset, _, prices = official_fixture(training=240, validation=96)
        rows = dataset["rows"]
        initial = result_rows(rows)
        # Um label conhecido no corte, mas além do horizonte, tampouco pode
        # contaminar a referência constante exclusiva do treino utilizável.
        first = rows[0]
        horizon = manifest["candidate"]["management_config"]["entry_window_bars"] + manifest["candidate"]["management_config"]["max_holding_bars"] - 1
        initial[first["opportunity_key"]].update(tp1_hit=False, net_r=-1.0,
            result_available_ts_ms=((first["decision_ts_ms"] + BAR5 - 1) // BAR5) * BAR5 + horizon * BAR5 + 1)
        changed = copy.deepcopy(initial)
        boundary = manifest["split"]["validation_start_ms"]
        for row in rows:
            if row["decision_ts_ms"] >= boundary:
                changed[row["opportunity_key"]] = result_rows([row], positive=False)[row["opportunity_key"]]
        def fit(results):
            return study._calibrate(rows, results, manifest,
                request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
                dataset_hash=study.digest(dataset), price_hash=prices["price_hash"], now_ms=manifest["split"]["as_of_ms"])
        before, after = fit(initial), fit(changed)
        old_rows = before["folds"][0]["acceptance_inputs"]["oos_rows"]
        new_rows = after["folds"][0]["acceptance_inputs"]["oos_rows"]
        self.assertEqual([r["prediction"] for r in old_rows], [r["prediction"] for r in new_rows])
        self.assertEqual([r["constant_prediction"] for r in old_rows], [r["constant_prediction"] for r in new_rows])
        self.assertTrue(all(r["label"] is False for r in new_rows))
        first_p = new_rows[0]["prediction"]
        last_p = after["folds"][-1]["acceptance_inputs"]["oos_rows"][0]["prediction"]
        self.assertNotEqual(first_p, last_p)
        self.assertEqual(first_p, 1.0)

    def test_oos_outcomes_are_opened_only_after_frozen_predictions(self):
        manifest, dataset, _, prices = hypothesis_fixture()
        emitted = []
        class SealedOOS(dict):
            def get(self, key, *args):
                if key in ("net_r", "tp1_hit", "exits", "status", "filled") and not emitted:
                    raise AssertionError("OOS outcome read before frozen prediction")
                return super().get(key, *args)
        results = result_rows(dataset["rows"])
        for row in dataset["rows"]:
            if row["decision_ts_ms"] >= manifest["split"]["validation_start_ms"]:
                key = row["opportunity_key"]
                results[key] = SealedOOS(results[key])
        original = calibration.predict
        def predict(*args, **kwargs):
            value = original(*args, **kwargs)
            emitted.append(value)
            return value
        with patch.object(calibration, "predict", side_effect=predict):
            out = study._calibrate(dataset["rows"], results, manifest,
                request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
                dataset_hash=study.digest(dataset), price_hash=prices["price_hash"], now_ms=manifest["split"]["as_of_ms"])
        self.assertEqual(len(out["folds"]), 4)
        self.assertTrue(emitted)

    def test_censor_missing_and_label_after_fold_end_do_not_clean_denominator(self):
        manifest, dataset, _, prices = official_fixture(training=240, validation=96)
        rows, results = dataset["rows"], result_rows(dataset["rows"])
        validation_rows = [r for r in rows if r["decision_ts_ms"] >= manifest["split"]["validation_start_ms"]]
        a, b, c = validation_rows[:3]
        results[a["opportunity_key"]].update(tp1_hit=False, status="CLOSED_MAX_HOLD")
        results[b["opportunity_key"]] = {}
        end = manifest["split"]["validation_start_ms"] + (manifest["split"]["holdout_start_ms"] - manifest["split"]["validation_start_ms"]) // 4
        results[c["opportunity_key"]]["result_available_ts_ms"] = end + 1
        out = study._calibrate(rows, results, manifest,
            request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
            dataset_hash=study.digest(dataset), price_hash=prices["price_hash"], now_ms=manifest["split"]["as_of_ms"])
        first = out["folds"][0]["acceptance_inputs"]
        self.assertEqual(first["eligible_count"], len(first["oos_rows"]) + 3)
        self.assertEqual(out["excluded"]["TARGET_EVENT_CENSORED_AT_HORIZON"], 1)
        self.assertEqual(out["excluded"]["SCORE_OR_LABEL_UNAVAILABLE"], 1)
        self.assertFalse(any(r["opportunity_key"] in {a["opportunity_key"], b["opportunity_key"], c["opportunity_key"]} for r in first["oos_rows"]))

    def test_literal_bool_required_and_insufficient_fold_has_no_fabricated_probability(self):
        manifest, dataset, _, prices = official_fixture(training=100, validation=80)
        results = result_rows(dataset["rows"])
        results[dataset["rows"][0]["opportunity_key"]]["tp1_hit"] = "true"
        out = study._calibrate(dataset["rows"], results, manifest,
            request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=VALIDITY),
            dataset_hash=study.digest(dataset), price_hash=prices["price_hash"], now_ms=manifest["split"]["as_of_ms"])
        self.assertEqual(out["excluded"]["EVENT_LABEL_INVALID"], 1)
        self.assertIsNone(out["artifact"])
        self.assertTrue(all(f["acceptance_inputs"]["oos_rows"] == [] for f in out["folds"]))

    def test_simulated_protection_reconciles_effective_fill_not_planned_entry(self):
        report = run_fixture()
        proof, evidence = report["offline_observation"], report["study"]["evidence"]
        self.assertEqual(proof["scope"], "OFFLINE_REPLAY_SIMULATED")
        self.assertFalse(proof["live_equivalent"])
        self.assertFalse(proof["proves_real_sl"])
        self.assertEqual(evidence["source"]["observation_scope"], proof["scope"])
        self.assertEqual(evidence["operational_applicable"], len(report["selection"]["selected"]["candidate"]))
        self.assertEqual(evidence["operational_coverage_pct"], 100.0)
        self.assertEqual(evidence["operational_failures"], 0)
        self.assertEqual(evidence["economic_duplicates"], 0)
        self.assertEqual(evidence["protection_scope"], "SHADOW_SIMULATED")
        self.assertEqual(evidence["protection_observed"], evidence["protection_applicable"])
        self.assertIsNone(evidence["fidelity_discrepancy_pct"])
        self.assertIsNone(evidence["fidelity_comparable"])
        for row in proof["rows"]:
            if row["trade"]["admitted"]:
                self.assertTrue(row["resolution_reconciled"])
                self.assertIsNone(row["protection_reason"])
                self.assertEqual(row["frozen"]["setup"]["entry"], row["trade"]["entry_fill_price"])
                self.assertNotEqual(row["frozen"]["setup"]["entry"], 100.0)
        changed = copy.deepcopy(report)
        changed["offline_observation"]["measured"]["operational_failures"] = 12
        changed["study"]["evidence"]["operational_failures"] = 12
        changed["acceptance"] = acceptance.build_acceptance(changed, now_ms=report["observed_at_ms"])
        verdict = study.validate_study_report(changed, now_ms=report["observed_at_ms"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "STUDY_OFFLINE_OBSERVATION_INVALID")

    def test_missing_protection_trace_and_incomplete_operation_logs_never_claim_zero(self):
        manifest, _, _, prices = hypothesis_fixture()
        report = run_fixture()
        rows = report["selection"]["selected"]["candidate"]
        execution = report["candidate_replay"]
        config = replay.ReplayConfig(**{k: v for k, v in manifest["candidate"]["management_config"].items() if k != "config_hash"})
        costs = replay.CostConfig(**{k: manifest["costs"]["config"][k] for k in
                                   ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")})
        original = replay.replay_opportunity
        def no_trace(*args, **kwargs):
            result = original(*args, **kwargs)
            result.pop("protection")
            return result
        with patch.object(replay, "replay_opportunity", side_effect=no_trace):
            measured = study._offline_observation(rows, execution, prices, config=config, costs=costs)["measured"]
        self.assertEqual(measured["protection_observed"], 0)
        self.assertEqual(measured["protection_coverage_pct"], 0.0)
        self.assertIsNone(measured["protection_pending"])
        self.assertIsNone(measured["unresolved_protection_failures"])
        missing = {**execution, "trades": execution["trades"][:len(execution["trades"]) // 2]}
        measured = study._offline_observation(rows, missing, prices, config=config, costs=costs)["measured"]
        self.assertLess(measured["operational_coverage_pct"], 90.0)
        self.assertIsNone(measured["operational_failures"])
        self.assertIsNone(measured["economic_duplicates"])

    def test_fallback_last_good_does_not_stand_in_for_unexecuted_last_fold(self):
        original, calls = calibration.validate_out_of_sample, []
        def validate(*args, **kwargs):
            calls.append(True)
            if len(calls) == 4:
                return {"ok": False, "state": "FITTED", "reason_code": calibration.SAMPLE_INSUFFICIENT}
            return original(*args, **kwargs)
        with patch.object(calibration, "validate_out_of_sample", side_effect=validate):
            report = run_fixture()
        self.assertEqual(len(calls), 4)
        self.assertEqual(report["artifact"], report["calibration"]["folds"][2]["artifact"])
        self.assertNotEqual(report["artifact"], report["calibration"]["folds"][3]["artifact"])
        self.assertNotEqual(report["acceptance"]["calibration"]["state"], "ACCEPTED")
        self.assertFalse(acceptance.verify_acceptance(report, now_ms=report["observed_at_ms"])["ok"])


class MemoryFactory:
    """Sessão mínima: exercita serviço CAS real, sem criar outro catálogo."""
    def __init__(self, memory=None):
        self.memory = memory or SimpleNamespace(row=None, commits=0, reads=0, rollbacks=0)

    def __call__(self):
        return MemorySession(self.memory)


class MemorySession:
    def __init__(self, memory):
        self.memory, self.pending = memory, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, query, *args):
        self.memory.reads += 1
        names = getattr(query, "column_descriptions", [])
        row = self.memory.row
        if names and names[0]["name"] == "payload":
            row = row.payload if row is not None else None
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    def add(self, row):
        row.id = 1
        self.pending = row

    async def commit(self):
        self.memory.row = self.pending
        self.memory.commits += 1

    async def rollback(self):
        self.memory.rollbacks += 1


class AcceptancePersistence(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_fixture(training=100, validation=80)

    async def test_insufficient_report_roundtrips_restart_and_is_idempotent(self):
        factory = MemoryFactory()
        self.assertEqual(self.report["acceptance"]["calibration"]["state"], "INSUFFICIENT_EVIDENCE")
        with patch.object(acceptance, "verify_acceptance", wraps=acceptance.verify_acceptance) as verify:
            first = await study.persist_study(factory, self.report)
        self.assertTrue(first["published"], first)
        self.assertTrue(any(c.kwargs.get("recompute") is True for c in verify.call_args_list))
        restart = MemoryFactory(factory.memory)
        repeated = await study.persist_study(restart, copy.deepcopy(self.report))
        self.assertEqual(repeated["reason_code"], "STUDY_UNCHANGED")
        self.assertEqual(factory.memory.commits, 1)
        loaded = await study.load_latest_study(restart, now_ms=self.report["observed_at_ms"])
        self.assertTrue(loaded["available"], loaded)
        self.assertEqual(loaded["report"], self.report)
        self.assertEqual(loaded["acceptance"]["state"], self.report["acceptance"]["state"])
        self.assertFalse(loaded["acceptance"]["promotable"])

    async def test_same_study_key_with_new_proof_conflicts(self):
        factory = MemoryFactory()
        self.assertTrue((await study.persist_study(factory, self.report))["published"])
        changed = copy.deepcopy(self.report)
        changed["source_assurance"] = "REPROCESSED_OPERATOR_REPORT"
        changed["acceptance"] = acceptance.build_acceptance(changed, now_ms=changed["observed_at_ms"])
        self.assertTrue(study.validate_study_report(changed, now_ms=changed["observed_at_ms"])["ok"])
        conflict = await study.persist_study(factory, changed)
        self.assertEqual(conflict["reason_code"], "STUDY_IDENTITY_CONFLICT")
        self.assertEqual(factory.memory.commits, 1)
        self.assertEqual(factory.memory.row.payload, self.report)

    async def test_concurrent_cas_reread_distinguishes_drift(self):
        factory = MemoryFactory()
        concurrent = copy.deepcopy(self.report)
        concurrent["source_assurance"] = "ANOTHER_REPROCESSING"
        concurrent["acceptance"] = acceptance.build_acceptance(concurrent, now_ms=concurrent["observed_at_ms"])
        original = state.publish_generation
        async def publish_raced(session_factory, **kwargs):
            rival = {**kwargs, "payload": concurrent}
            self.assertTrue((await original(session_factory, **rival))["published"])
            return await original(session_factory, **kwargs)
        with patch.object(state, "publish_generation", side_effect=publish_raced):
            out = await study.persist_study(factory, self.report)
        self.assertEqual(out["reason_code"], "STUDY_IDENTITY_CONFLICT")
        self.assertEqual(factory.memory.commits, 1)
        self.assertEqual(factory.memory.row.payload, concurrent)

    async def test_recompute_failure_prevents_even_database_read(self):
        def verify(report, **kwargs):
            return {"ok": not kwargs.get("recompute"), "reason_code": "ACCEPTANCE_RECOMPUTE_MISMATCH"}
        with patch.object(acceptance, "verify_acceptance", side_effect=verify), \
             patch.object(state, "read_state", new=AsyncMock()) as read:
            result = await study.persist_study(MemoryFactory(), self.report)
        self.assertEqual(result["reason_code"], "ACCEPTANCE_RECOMPUTE_MISMATCH")
        read.assert_not_awaited()

    async def test_get_is_read_only_and_legacy_never_gains_acceptance(self):
        factory = MemoryFactory()
        self.assertTrue((await study.persist_study(factory, self.report))["published"])
        reads, commits = factory.memory.reads, factory.memory.commits
        with patch.object(acceptance, "build_acceptance", side_effect=AssertionError("GET bootstrap/build")), \
             patch.object(calibration, "fit_calibration", side_effect=AssertionError("GET fit")), \
             patch.object(calibration, "validate_out_of_sample", side_effect=AssertionError("GET evaluation")), \
             patch.object(replay, "replay_opportunity", side_effect=AssertionError("GET replay")), \
             patch.object(state, "publish_generation", side_effect=AssertionError("GET write")):
            out = await study.load_latest_study(factory, now_ms=self.report["observed_at_ms"])
        self.assertTrue(out["available"], out)
        self.assertEqual(factory.memory.reads, reads + 1)
        self.assertEqual(factory.memory.commits, commits)
        legacy = copy.deepcopy(self.report)
        legacy.pop("acceptance")
        factory.memory.row.payload = legacy
        out = await study.load_latest_study(factory, now_ms=self.report["observed_at_ms"])
        self.assertTrue(out["available"], out)
        self.assertEqual(out["acceptance"]["state"], "LEGACY_UNACCEPTED")
        self.assertNotIn("acceptance", out["report"])


if __name__ == "__main__":
    unittest.main()
