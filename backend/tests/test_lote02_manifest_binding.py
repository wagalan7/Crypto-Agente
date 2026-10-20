"""Fronteiras finais L02: contrato congelado governa motor e catálogo.

Dados sintéticos explícitos; nenhum acesso a outcomes reais, DB ou rede.
"""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services import research_manifest_service as rm
from services import research_selection_service as selection
from services import preselection_experiment_service as r12
from services import strategy_evidence_service as ev
from services import score_v3_service as s3
from tests.test_lote02_research_manifest import manifesto, gestao, custos_config, T0, BAR5
from tests.test_lote02_selection_scope import linha, features


def frozen_manifest(*, score_config=None):
    body = manifesto()
    cfg = s3.ScoreConfig(**(score_config or s3.DEFAULT_CONFIG.as_dict()))
    body["baseline"].update(score_version="SCORE_V2", score_config=None)
    body["candidate"].update(
        score_version=s3.SCORE_VERSION, score_config=cfg.as_dict(),
        score_config_hash=s3.model_fingerprint(
            playbook=body["candidate"]["selection_rule"]["playbook"], config=cfg))
    body["population"].update(scope_id="R09_PRE_SELECTION_POPULATION",
                                cohort="R09_PRE_SELECTION_POPULATION")
    return rm.parse_manifest(body)


def contract_inputs(m):
    return dict(population="SHADOW", study_kind="PRE_SELECTION",
                policy_version=m["candidate"]["policy_version"],
                universe_version=m["population"]["universe_version"],
                comparison_scope=m["comparison_scope"],
                baseline_config=m["baseline"]["management_config"],
                candidate_config=m["candidate"]["management_config"],
                costs_config=m["costs"]["config"],
                selection_config=rm.selection_config_of(m["candidate"]),
                research_manifest=m, manifest_hash=m["manifest_hash"],
                dataset_scope=m["population"]["scope_id"],
                temporal_split=m["split"],
                bundle_hash=m["hashes"]["bundle_hash"],
                dataset_fingerprint="f" * 64, cutoff_ms=m["split"]["as_of_ms"])


def verifier(contract):
    envelope = r12.build_preselection_envelope(
        replay_config=contract["candidate_config"],
        contract_hash=contract["contract_hash"],
        selection_config=contract["selection_config"])
    study = dict(contract=contract, contract_hash=contract["contract_hash"],
                 population="SHADOW", dataset_fingerprint="f" * 64,
                 cutoff_ms=contract["cutoff_ms"], evidence={})
    return ev.verify_study_identity(
        study, candidate_config=envelope, fingerprint="f" * 64,
        cutoff=datetime.fromtimestamp(contract["cutoff_ms"] / 1000, timezone.utc))


class BindingBoundaries(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_current_engine_identity_and_test_only_roundtrip(self):
        m = frozen_manifest()
        c = r12.preselection_contract(**contract_inputs(m))
        actual = verifier(c)
        self.assertTrue(actual["ok"], actual)
        self.assertFalse(actual["real_study_allowed"])
        self.assertEqual(actual["decision_state"], rm.STATE_TEST_ONLY)
        self.assertIn("research_manifest", actual["verified_fields"])
        self.assertIn("temporal_split", actual["verified_fields"])

    def test_nonexistent_engine_or_unrelated_fingerprint_is_not_authorized(self):
        m = frozen_manifest()
        for field, wrong in (("score_version", "NONEXISTENT_SCORE_999"),
                             ("score_config_hash", "completely-unrelated-config"),
                             ("core_config_hash", "unrelated-core")):
            with self.subTest(field=field):
                raw = {k: copy.deepcopy(m[k]) for k in rm.MANIFEST_FIELDS}
                raw["hashes"] = None
                raw["candidate"][field] = wrong
                with self.assertRaises(rm.ManifestError):
                    rm.parse_manifest(raw)

    def test_explicit_score_config_governs_actual_score(self):
        default = frozen_manifest()
        changed = frozen_manifest(score_config={**s3.DEFAULT_CONFIG.as_dict(),
                                                "include_composite_confluence": True})
        row = linha("test-only", outcome="ACCEPTED",
                    feats=features(confluence_pct=40.0))
        scores = []
        for manifest in (default, changed):
            result = selection.compare_population([row], manifest=manifest)
            decision = result["decisions"][0]["candidate"]
            expected = s3.score(row["features"], playbook=manifest["candidate"][
                "selection_rule"]["playbook"], side="long",
                config=s3.ScoreConfig(**manifest["candidate"]["score_config"]))
            self.assertEqual(decision["score"], expected["score"])
            self.assertEqual(decision["model_fingerprint"],
                             manifest["candidate"]["score_config_hash"])
            scores.append(decision["score"])
        self.assertNotEqual(*scores)

    def test_bps_model_never_declares_observed_absolute_account_costs(self):
        raw = {k: copy.deepcopy(frozen_manifest()[k]) for k in rm.MANIFEST_FIELDS}
        raw["hashes"] = None
        raw["costs"].update(source="BINANCE_USDM_ACCOUNT_LEDGER",
                            unit="SETTLEMENT_ASSET_ABSOLUTE",
                            availability="OBSERVED_ACCOUNT_COSTS")
        with self.assertRaises(rm.ManifestError) as err:
            rm.parse_manifest(raw)
        self.assertEqual(err.exception.reason_code, "COSTS_SOURCE_NOT_IMPLEMENTED")

    def test_actual_context_must_match_all_frozen_sections(self):
        m = frozen_manifest()
        variations = dict(dataset_scope="OTHER", universe_version="OTHER",
                          policy_version="OTHER",
                          cutoff_ms=m["split"]["as_of_ms"] - 1,
                          bundle_hash="0" * 64,
                          costs_config={**custos_config(), "fee_bps_per_side": 0})
        variations["temporal_split"] = {**m["split"], "embargo_bars": 0}
        variations["baseline_config"] = gestao(tp1_fraction=0.8)
        for field, value in variations.items():
            with self.subTest(field=field):
                inputs = contract_inputs(m)
                inputs[field] = value
                with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
                    r12.preselection_contract(**inputs)

    def test_hash_without_manifest_body_never_verifies(self):
        c = r12.preselection_contract(**contract_inputs(frozen_manifest()))
        c["research_manifest"] = None
        c["manifest_hash"] = "NOT_A_MANIFEST"
        c["contract_hash"] = r12.contract_hash_of(c)
        self.assertFalse(verifier(c)["ok"])

    def test_invalid_manifest_is_rejected_before_evidence_is_touched(self):
        class SealedStudy(dict):
            def get(self, field, *args):
                if field == "evidence":
                    raise AssertionError("OUTCOMES_READ_BEFORE_IDENTITY")
                return super().get(field, *args)
        c = r12.preselection_contract(**contract_inputs(frozen_manifest()))
        c["research_manifest"] = None
        c["contract_hash"] = r12.contract_hash_of(c)
        envelope = r12.build_preselection_envelope(
            replay_config=c["candidate_config"], contract_hash=c["contract_hash"],
            selection_config=c["selection_config"])
        study = SealedStudy(contract=c, contract_hash=c["contract_hash"],
                            population="SHADOW", dataset_fingerprint="f" * 64,
                            cutoff_ms=c["cutoff_ms"])
        self.assertFalse(ev.verify_study_identity(
            study, candidate_config=envelope, fingerprint="f" * 64,
            cutoff=datetime.fromtimestamp(c["cutoff_ms"] / 1000, timezone.utc))["ok"])

    def test_rehashing_context_drift_does_not_hide_it_from_catalog(self):
        original = r12.preselection_contract(**contract_inputs(frozen_manifest()))
        changes = {"dataset_scope": "OTHER", "universe_version": "OTHER",
                   "cutoff_ms": original["cutoff_ms"] - 1,
                   "costs_config": gestao(), "bundle_hash": "x" * 64,
                   "temporal_split": {**original["temporal_split"], "purge_bars": 0}}
        for key, value in changes.items():
            with self.subTest(field=key):
                changed = copy.deepcopy(original)
                changed[key] = value
                changed["contract_hash"] = r12.contract_hash_of(changed)
                self.assertFalse(verifier(changed)["ok"])

    def test_unrecognized_contract_fields_are_not_dropped_from_hash(self):
        c = r12.preselection_contract(**contract_inputs(frozen_manifest()))
        c["unknown"] = True
        self.assertIsNone(r12.contract_hash_of(c))

    def test_population_identity_time_and_duplicate_guard(self):
        m = frozen_manifest()
        row = linha("p", outcome="ACCEPTED")
        for invalid in ({**row, "symbol": "SYN/USDC:USDC"},
                        {**row, "decision_ts_ms": m["split"]["holdout_start_ms"]}):
            result = selection.compare_population([invalid], manifest=m)
            self.assertFalse(result["ok"])
        self.assertFalse(selection.compare_population([row, row], manifest=m)["ok"])

    def test_observed_baseline_mismatch_or_absence_is_unknown_not_selected(self):
        m = frozen_manifest()
        row = linha("p", outcome="ACCEPTED")
        for altered in ({**row, "score_trace": None},
                        {**row, "score_trace": {**row["score_trace"],
                                                 "formula_effective": "LEGACY_V1"}},
                        {**row, "score_trace": {**row["score_trace"], "config": {
                            **row["score_trace"]["config"], "v2_w_conf": 0.7}}}):
            result = selection.compare_population([altered], manifest=m)
            self.assertEqual(result["counts"]["baseline"][selection.STATE_UNKNOWN], 1)
            self.assertEqual(result["selected"]["baseline"], [])
            self.assertEqual(result["selected"]["candidate"], [])
            self.assertEqual(result["coverage"]["excluded_symmetric"], 1)

    def test_earlier_or_missing_champion_decision_scope_is_symmetric_unknown(self):
        m = frozen_manifest()
        row = linha("p", outcome="ACCEPTED")
        for scope in (None, "PRE_PORTFOLIO", "POST_SELECTION", ""):
            altered = {**row, "observed_decision_scope": scope}
            result = selection.compare_population([altered], manifest=m)
            self.assertEqual(result["counts"]["baseline"][selection.STATE_UNKNOWN], 1)
            self.assertEqual(result["selected"]["baseline"], [])
            self.assertEqual(result["selected"]["candidate"], [])
            self.assertEqual(result["coverage"]["excluded_symmetric"], 1)

    def test_management_v1_contract_has_identical_historical_body_hash(self):
        fields = dict(population="SHADOW", study_kind="PRE_SELECTION",
                      policy_version="P", universe_version="U",
                      comparison_scope="MANAGEMENT_ONLY", baseline_config=gestao(),
                      candidate_config=gestao(tp1_fraction=0.6), costs_config=custos_config(),
                      bundle_hash="b" * 64, dataset_fingerprint="f" * 64, cutoff_ms=T0)
        c = r12.preselection_contract(**fields)
        body = {"contract_version": r12.PRE_SELECTION_CONTRACT_VERSION, **fields}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True, default=str).encode()).hexdigest()
        self.assertEqual(c, {**body, "contract_hash": expected})


if __name__ == "__main__":
    unittest.main()
