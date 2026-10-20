"""L03 — observação pura e forward replay; sem aprovação/ativação real.

Todos os contratos são TEST_ONLY, não habilitam runtime nem promovem hipótese.
"""
import copy
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import prospective_shadow_service as prospective
from services import strategy_evidence_service as ev
from services import preselection_experiment_service as catalog
from services import research_dataset_service as ds
from tests.test_lote02_official_study_cycle import official_fixture
from tests.test_lote02_manifest_binding import contract_inputs


def engineering_fixture():
    manifest, _, _, prices, _, rows = official_fixture(training=3, validation=2, return_source=True)
    contract = catalog.preselection_contract(**contract_inputs(manifest))
    candidate = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
        contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
    exp = SimpleNamespace(id=123, experiment_key="TEST_ONLY_L03", candidate_config=candidate,
        candidate_hash=ev.canonical_hash(candidate), champion_hash="b" * 64,
        offline_metrics={"study": {"contract": contract, "contract_hash": contract["contract_hash"]}})
    context = prospective.frozen_shadow_context(exp, generation=1,
        approval_id="TEST_ONLY_NOT_REAL_APPROVAL", started_at_ms=manifest["split"]["train_start_ms"])
    row = copy.deepcopy(rows[1])
    row["scope"] = "PRE_SELECTION"
    row["observed_at"] = row["decision_at"]
    payload = row["frozen_config"]["r09_pre_selection"]
    payload["feature_evidence"] = {"quality": "FRESH", "observed_at_ms": ds.utc_ms(row["decision_at"]),
                                    "candle_close_ms": ds.utc_ms(row["decision_at"])}
    return exp, context, row, prices


class ProspectiveCore(unittest.TestCase):
    def setUp(self):
        self.no_dns = patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        self.no_dns.start()
        self.addCleanup(self.no_dns.stop)
        self.exp, self.context, self.row, self.prices = engineering_fixture()

    def annotation(self):
        return prospective.build_preselection_annotation(self.row, self.context)

    def resolved(self):
        ann = self.annotation()
        row = copy.deepcopy(self.row)
        row["frozen_config"]["r09_pre_selection"][prospective.KEY] = ann
        key = row["opportunity_key"]
        candles = [{"timestamp": c["timestamp_ms"], **{k: c[k] for k in
                   ("open", "high", "low", "close", "volume")}} for c in self.prices["windows"][key]]
        clock = max(c["timestamp"] for c in candles) + 300_000
        return prospective.resolve_annotation(SimpleNamespace(**row),
            {"candles": candles, "as_of": ds.ms_datetime(clock)})

    def test_freezes_actual_decisions_before_any_economic_outcome(self):
        before = copy.deepcopy(self.row)
        ann = self.annotation()
        self.assertTrue(prospective.verify_annotation(ann))
        self.assertEqual(ann["frozen"]["decisions"]["baseline"]["state"], "SELECTED")
        self.assertIn(ann["frozen"]["decisions"]["candidate"]["state"], ("SELECTED", "REJECTED"))
        self.assertIsNone(ann["resolution"]["net_r"])
        self.assertEqual(before, self.row)

    def test_test_only_manifest_cannot_become_real_by_declaring_server_scan(self):
        self.row["frozen_config"]["r09_pre_selection"]["source"]["decision_source"] = "server_scan"
        self.assertEqual(self.annotation()["frozen"]["source_mode"], prospective.TEST)

    def test_capture_before_start_is_not_prospective(self):
        changed = {**self.context, "started_at_ms": ds.utc_ms(self.row["observed_at"]) + 1}
        self.assertIsNone(prospective.build_preselection_annotation(self.row, changed))

    def test_future_decision_is_not_backdated_into_capture(self):
        self.row["frozen_config"]["r09_pre_selection"]["decision_ts_ms"] += 1
        self.assertIsNone(self.annotation())

    def test_first_annotation_is_immutable_on_retry(self):
        original = self.annotation()
        self.row["frozen_config"]["r09_pre_selection"][prospective.KEY] = copy.deepcopy(original)
        changed = {**self.context, "generation": 999}
        self.assertEqual(prospective.build_preselection_annotation(self.row, changed), original)

    def test_absent_feature_evidence_does_not_turn_features_into_point_in_time_proof(self):
        self.row["frozen_config"]["r09_pre_selection"].pop("feature_evidence")
        self.assertEqual(self.annotation()["frozen"]["decisions"]["candidate"]["state"], "UNKNOWN")

    def test_future_feature_evidence_does_not_enter_candidate_decision(self):
        self.row["frozen_config"]["r09_pre_selection"]["feature_evidence"]["observed_at_ms"] += 1
        self.assertEqual(self.annotation()["frozen"]["decisions"]["candidate"]["state"], "UNKNOWN")

    def test_real_replay_uses_frozen_costs_and_reports_nonzero_economics(self):
        changed = self.resolved()
        ann = changed["r09_pre_selection"][prospective.KEY]
        self.assertTrue(prospective.verify_annotation(ann))
        self.assertIn(ann["resolution"]["status"], prospective.TERMINAL)
        self.assertEqual(ann["resolution"]["cost_source"], "DECLARED_BPS_SCENARIO")
        self.assertNotEqual(ann["resolution"]["net_r"], 0.0)
        self.assertEqual(ann["resolution"]["cost_config_hash"], ann["frozen"]["costs_config"]["config_hash"])

    def test_completed_annotation_does_not_replay_or_increment_generation(self):
        changed = self.resolved()
        self.row["frozen_config"] = changed
        self.assertIsNone(prospective.resolve_annotation(SimpleNamespace(**self.row),
            {"candles": [], "as_of": self.row["observed_at"]}))

    def test_tampered_frozen_annotation_is_not_replayed(self):
        ann = self.annotation()
        ann["frozen"]["generation"] += 1
        self.row["frozen_config"]["r09_pre_selection"][prospective.KEY] = ann
        self.assertFalse(prospective.verify_annotation(ann))
        self.assertIsNone(prospective.resolve_annotation(SimpleNamespace(**self.row),
            {"candles": [], "as_of": self.row["observed_at"]}))

    def test_unclosed_future_bar_cannot_make_current_outcome(self):
        ann = self.annotation()
        self.row["frozen_config"]["r09_pre_selection"][prospective.KEY] = ann
        stamp = ds.utc_ms(self.row["observed_at"])
        changed = prospective.resolve_annotation(SimpleNamespace(**self.row), {"as_of": self.row["observed_at"],
            "candles": [{"timestamp": stamp, "open": 100., "high": 100000., "low": 0.1, "close": 100000., "volume": 10.}]})
        result = changed["r09_pre_selection"][prospective.KEY]
        self.assertEqual(result["candles"], [])
        self.assertIsNone(result["resolution"]["net_r"])

    def test_test_only_result_is_excluded_not_reused_as_prospective(self):
        ann = self.resolved()["r09_pre_selection"][prospective.KEY]
        summary = prospective.summarize_prospective([ann], started_at_ms=self.context["started_at_ms"],
            now_ms=self.context["manifest"]["split"]["as_of_ms"], enabled_playbooks=["TREND_PULLBACK"])
        self.assertEqual(summary["data_quality"]["valid"], 0)
        self.assertEqual(summary["data_quality"]["excluded_by_reason"], {"NOT_REAL_PROSPECTIVE": 1})
        self.assertIsNone(summary["evidence"]["net_ev_r"])
        self.assertEqual(summary["gate"]["verdict"], "NO_GO")

    def test_no_sample_never_fabricates_safety_zero_or_economic_approval(self):
        summary = prospective.summarize_prospective([], started_at_ms=1, now_ms=2,
            enabled_playbooks=["TREND_PULLBACK"])
        for field in ("operational_failures", "economic_duplicates", "unresolved_protection_failures", "fidelity_discrepancy_pct"):
            self.assertIsNone(summary["evidence"][field])
        self.assertFalse(summary["promotable"])
        self.assertFalse(summary["offline_used"])

    def test_service_has_no_network_executor_commit_or_new_loop(self):
        source = inspect.getsource(prospective)
        for forbidden in ("send_telegram(", "place_order(", "asyncio.create_task(", "await session.commit("):
            self.assertNotIn(forbidden, source)


class ProspectiveReadGuards(unittest.IsolatedAsyncioTestCase):
    async def test_off_default_queries_nothing(self):
        session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("QUERY_OFF")))
        with patch.object(ev, "P05_CHALLENGER_SHADOW_ENABLED", False):
            self.assertIsNone(await prospective.get_active_preselection_context(session))
            self.assertEqual(await prospective.resolve_pending(session, {}), set())
        session.execute.assert_not_awaited()

    async def test_old_offline_metrics_without_prospective_start_do_not_load_outcomes(self):
        exp = SimpleNamespace(shadow_metrics={"study_generation": 1},
            offline_metrics={"evidence": {"net_ev_r": 100.0}})
        session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("OUTCOME_QUERY")))
        result = await prospective.load_prospective_evidence(session, exp, now_ms=1)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason_code"], "PROSPECTIVE_SHADOW_NOT_STARTED")
        session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
