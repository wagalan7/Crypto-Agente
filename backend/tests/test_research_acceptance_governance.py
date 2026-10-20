"""Acceptance consumer guards: old OOS evidence is not promotion authority."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from services import operational_governance_service as governance
from services import prospective_shadow_service as prospective
from services import research_batch_service as batch
from services import strategy_evidence_service as evidence
from services import research_acceptance_service as acceptance
from services import preselection_experiment_service as catalog
from services import research_dataset_service as dataset
from tests.test_lote03_governance import CHAMPION, MemorySessions, governance_fixture, approval_payload
from tests.test_research_acceptance_policy import policy_report, prospective_snapshot, DAY

_POLICY_REPORT = None


def policy_governance_fixture(*, prepared=False):
    """Pure synthetic fit/OOS/WF proof; no official-export or LIVE-evidence claim."""
    global _POLICY_REPORT
    if _POLICY_REPORT is None:
        _POLICY_REPORT = policy_report()
    report = copy.deepcopy(_POLICY_REPORT)
    now, contract = report["observed_at_ms"], report["study"]["contract"]
    candidate = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
        contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
    exp = SimpleNamespace(id=17, experiment_key="SYNTHETIC_ACCEPTANCE_TEST_ONLY",
        candidate_config=candidate, candidate_hash=evidence.canonical_hash(candidate),
        champion_hash=evidence.canonical_hash(contract["baseline_config"]),
        dataset_fingerprint=contract["dataset_fingerprint"], dataset_cutoff=dataset.ms_datetime(contract["cutoff_ms"]),
        offline_metrics={"study": copy.deepcopy(report["study"])}, shadow_metrics={},
        status="OFFLINE_VALIDATED", decision={"previous_field": "preserved"})
    with patch.object(evidence, "discover_champion_config", return_value=CHAMPION):
        bundle = governance.build_bundle(exp, report, CHAMPION, now_ms=now)
    if prepared:
        snapshot = prospective_snapshot(report)
        snapshot["identity"].update(experiment_id=exp.id, experiment_key=exp.experiment_key,
                                    bundle_hash=bundle["bundle_hash"])
        snapshot["snapshot_hash"] = acceptance.digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"})
        exp.shadow_metrics[prospective.KEY] = {k: snapshot["identity"][k] for k in
                                              ("generation", "approval_id", "started_at_ms")}
        exp.decision["operational"] = {"acceptance": acceptance.build_acceptance(report,
            now_ms=now, prospective_evidence=snapshot)}
    return exp, report, bundle, now


def accepted_governance_fixture():
    return policy_governance_fixture(prepared=True)


class CohortSessions(MemorySessions):
    """Same fake transactions; annotation rows still run through real producers."""
    def __init__(self, exp, report):
        super().__init__(exp, report)
        self.annotations = []
        self.read_hook = None

    def __call__(self):
        session = super().__call__()
        original = session.execute
        store = self
        async def execute(statement, params=None):
            if "decision_observations" in str(statement):
                rows = copy.deepcopy(store.annotations)
                if store.read_hook:
                    store.read_hook()
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
            return await original(statement, params)
        session.execute = execute
        return session


def forward_annotations(exp, bundle, authority, count=120):
    """Actual V3 shadow decisions and forward replay; synthetic market only."""
    from services import live_candidate_adapter_service as adapter
    from services import preselection_observation_service as pre
    from tests.test_lote03_prospective_shadow import engineering_fixture
    from tests.test_lote02_research_manifest import champion_trace
    from tests.test_lote03_candidate_adapter import FEATURES
    _, _, source, _ = engineering_fixture()
    start = exp.shadow_metrics[prospective.KEY]
    context = prospective.frozen_shadow_context(exp, **start)
    trace = champion_trace()
    management = bundle["manifest"]["candidate"]["management_config"]
    bar = management["bar_ms"]
    result = []
    for index in range(count):
        row = copy.deepcopy(source)
        stamp = start["started_at_ms"] + DAY + index * (18 * DAY // count)
        row.update(opportunity_key=f"synthetic-forward-{index:04d}", observed_at=dataset.ms_datetime(stamp),
                   score_trace=copy.deepcopy(trace))
        payload = row["frozen_config"]["r09_pre_selection"]
        payload.update(identity=row["opportunity_key"], decision_ts_ms=stamp,
            outcome="VETOED" if index % 8 == 0 else "ACCEPTED", features=copy.deepcopy(FEATURES),
            config={"formula_effective": "SCORE_V2", "score": copy.deepcopy(trace["config"])},
            feature_evidence={"quality": "FRESH", "observed_at_ms": stamp, "candle_close_ms": stamp})
        setup = payload["setup"]
        group = adapter.shadow_group_decision(authority, [{**setup, "features": FEATURES}], now_ms=stamp)
        payload["shadow_decision"] = pre._shadow_decision_view(
            adapter.shadow_decision_for_timeframe(group["group"], setup["timeframe"]))
        ann = prospective.build_preselection_annotation(row, context)
        # Explicit private-harness source label: this does not make these
        # synthetic rows real evidence, and the manifest remains TEST_ONLY.
        ann["frozen"]["source_mode"] = prospective.REAL
        ann["annotation_hash"] = prospective.digest(ann["frozen"])
        payload[prospective.KEY] = ann
        first = ((stamp + bar - 1) // bar) * bar
        candles = [{"timestamp": first + i * bar, "open": 100., "high": 100.8,
                    "low": 99.8, "close": 100.7, "volume": 100.}
                   for i in range(management["max_bars"])]
        resolved = prospective.resolve_annotation(SimpleNamespace(**row),
            {"candles": candles, "as_of": dataset.ms_datetime(first + management["max_bars"] * bar)})
        final = resolved["r09_pre_selection"][prospective.KEY]
        assert final["frozen"]["decisions"]["candidate"]["state"] == "SELECTED", final
        assert final["frozen"]["decisions"]["baseline"]["state"] != "UNKNOWN", final
        assert final["resolution"]["filled"] and final["resolution"]["net_r"] > .05, final
        result.append(final)
    return result


class AcceptanceConsumerGuards(unittest.TestCase):
    def setUp(self):
        self.exp, self.report, self.bundle, self.now = governance_fixture()
        self.patches = [patch.object(evidence, "discover_champion_config", return_value=CHAMPION),
                        patch.object(governance, "_ALLOW_TEST_APPROVALS", True),
                        patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK_PROHIBITED"))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_oos_only_and_private_test_identity_never_bypass_acceptance(self):
        self.assertEqual(self.report["artifact"]["state"], "OOS_VALIDATED")
        for purpose in ("PROMOTION", "CANARY"):
            verdict = governance.validate_bundle(self.exp, self.bundle, now_ms=self.now, purpose=purpose)
            self.assertFalse(verdict["ok"], (purpose, verdict))

    def test_shadow_can_collect_before_economic_acceptance(self):
        self.assertTrue(governance.validate_bundle(self.exp, self.bundle,
                        now_ms=self.now, purpose="SHADOW")["ok"])


class ProtectionAndStatusGuards(unittest.TestCase):
    def test_protection_global_switch_cannot_authorize_studies(self):
        with patch.object(prospective, "PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION", True):
            measured = prospective.protection_measure([])
        self.assertFalse(measured["protection_scope_accepted_for_promotion"])
        self.assertEqual(measured["protection_decision_required"], prospective.PROTECTION_DECISION_REQUIRED)
        self.assertIsNone(measured["unresolved_protection_failures"])
        self.assertFalse(measured["protection_proves_real_sl"])

    def test_status_no_record_is_honest_and_does_not_compute_acceptance(self):
        status = batch.acceptance_status()
        self.assertTrue(status["policy"]["available"])
        self.assertFalse(status["record"]["available"])
        self.assertEqual(status["record"]["state"], "WAITING_PARAMETERS")
        self.assertFalse(status["economically_approved"])


class AcceptancePreparationTransactions(unittest.IsolatedAsyncioTestCase):
    """Real evidence producers on fake transactions; PG proves SQL separately."""
    async def asyncSetUp(self):
        self.exp, self.report, self.bundle, self.initial_now = policy_governance_fixture()
        self.now = self.initial_now
        self.store = CohortSessions(self.exp, self.report)
        governance._PENDING_FENCES.clear()
        governance._FAILED_FENCES.clear()
        governance._LOCAL_PENDING = False
        self.patches = [patch.object(evidence, "discover_champion_config", return_value=CHAMPION),
            patch.object(governance, "_ALLOW_TEST_APPROVALS", True),
            patch.object(governance.time, "time", side_effect=lambda: self.now / 1000),
            patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK_PROHIBITED"))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        registered = await governance.register_bundle(self.store, self.exp.id,
            {"confirm": True, "calibration_study_key": self.report["study_key"],
             "champion_config": CHAMPION}, "TEST_ONLY_OPERATOR")
        self.assertTrue(registered["ok"], registered)
        self.bundle = self.exp.decision["operational"]["bundle"]
        payload = approval_payload(self.bundle, self.now)
        payload["limits"]["expires_at_ms"] = self.now + 25 * DAY
        approved = await governance.register_approval(self.store, self.exp.id, payload,
            "TEST_ONLY_OPERATOR", test_only=True)
        self.assertTrue(approved["ok"], approved)
        self.exp.status = "SHADOW"
        self.exp.shadow_metrics[prospective.KEY] = {"generation": approved["generation"],
            "approval_id": approved["approval_id"], "started_at_ms": self.initial_now}
        self.authority = await governance.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.assertTrue(self.authority["ok"], self.authority)
        self.annotations = forward_annotations(self.exp, self.bundle, self.authority)

    async def prepare(self, cutoff=None):
        return await governance.register_bundle(self.store, self.exp.id,
            {"confirm": True, "calibration_study_key": self.report["study_key"],
             "champion_config": CHAMPION, "prospective_cutoff_ms": self.now if cutoff is None else cutoff,
             "expected_generation": self.store.state.generation}, "TEST_ONLY_OPERATOR")

    async def test_insufficient_cuts_idempotency_preserve_shadow_generation_and_original_report(self):
        original_report = governance.digest(self.store.report)
        generation = self.store.state.generation
        self.now += 20 * DAY
        self.store.annotations = self.annotations[:10]
        first = await self.prepare()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["acceptance_state"], acceptance.INSUFFICIENT)
        self.assertEqual(self.store.state.generation, generation)
        repeated = await self.prepare()
        self.assertTrue(repeated["idempotent"], repeated)
        self.assertEqual(self.exp.decision["operational"]["acceptance_revision"], 1)
        self.now += DAY
        self.store.annotations = self.annotations[:20]
        second = await self.prepare()
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.store.state.generation, generation)
        self.assertEqual(self.exp.decision["operational"]["acceptance_revision"], 2)
        view = await governance.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.assertTrue(view["ok"], view)
        self.assertEqual(self.exp.shadow_metrics[prospective.KEY]["generation"], generation)
        self.assertEqual(governance.digest(self.store.report), original_report)

    async def test_accepted_cut_scoped_protection_read_only_promotion_and_canary(self):
        self.now += 20 * DAY
        self.store.annotations = self.annotations
        result = await self.prepare()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["acceptance_state"], acceptance.ACCEPTED, result)
        receipt = self.exp.decision["operational"]["acceptance"]
        generation = self.store.state.generation
        self.assertTrue((await self.prepare())["idempotent"])
        self.assertEqual(self.store.state.generation, generation)
        self.assertEqual((await self.prepare(self.now - 1))["reason_code"], "ACCEPTANCE_RECEIPT_FROZEN")
        self.assertFalse(receipt["authorizes_live"])
        # Prepared reads cannot replay, fit, bootstrap or rebuild receipts.
        from services import walk_forward_service as wf
        with patch.object(acceptance, "build_acceptance", side_effect=AssertionError("GET building receipt")), \
             patch.object(acceptance, "_bootstrap", side_effect=AssertionError("GET bootstrap")), \
             patch.object(wf, "block_bootstrap_ci", side_effect=AssertionError("GET economic bootstrap")):
            async with self.store() as session:
                measured = await prospective.load_prospective_evidence(session, self.exp, now_ms=self.now)
            self.assertEqual(measured["gate"]["verdict"], "GO_CANDIDATE", measured)
            commits = self.store.calls.count(("commit",))
            status = await governance.get_status(self.store)
            self.assertTrue(status["ok"], status)
            self.assertEqual(self.store.calls.count(("commit",)), commits)
        promotion_payload = approval_payload(self.bundle, self.now, "PROMOTION", acceptance_record=receipt)
        promotion = await governance.register_approval(self.store, self.exp.id, promotion_payload,
            "TEST_ONLY_OPERATOR", test_only=True)
        self.assertTrue(promotion["ok"], promotion)
        promoted = await governance.promote(self.store, self.exp.id, promotion["approval_id"], promotion["generation"])
        self.assertTrue(promoted["ok"], promoted)
        self.assertEqual(self.store.state.payload["acceptance_record_hash"], receipt["record_hash"])
        canary_payload = approval_payload(self.bundle, self.now, "CANARY", acceptance_record=receipt)
        canary = await governance.register_approval(self.store, self.exp.id, canary_payload,
            "TEST_ONLY_OPERATOR", test_only=True)
        self.assertTrue(canary["ok"], canary)
        with patch.dict("os.environ", {"R13_OPERATIONAL_SELECTOR": "CANDIDATE",
                                       "R13_OPERATIONAL_EXPERIMENT_ID": str(self.exp.id)}):
            live = await governance.load_view(self.store, now_ms=self.now)
            self.assertTrue(live["ok"], live)
            self.store.report["acceptance"]["revoked"] = True
            self.assertFalse((await governance.load_view(self.store, now_ms=self.now))["ok"])

    async def test_cohort_drift_between_reads_refused_without_receipt(self):
        self.now += 20 * DAY
        self.store.annotations = self.annotations
        reads = 0
        def mutate_after_first_read():
            nonlocal reads
            reads += 1
            if reads == 1:
                self.store.annotations = self.store.annotations[:-1]
        self.store.read_hook = mutate_after_first_read
        result = await self.prepare()
        self.assertEqual(result["reason_code"], "ACCEPTANCE_PREPARATION_COHORT_DRIFT", result)
        self.assertNotIn("acceptance", self.exp.decision["operational"])

    async def test_expiry_during_cleanup_never_returns_authority(self):
        expiry = self.authority["approval"]["limits"]["expires_at_ms"]
        self.store.cleanup = lambda: setattr(self, "now", expiry)
        result = await governance.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.assertEqual(result["reason_code"], "FINAL_AUTHORITY_NO_LONGER_VALID", result)

    async def test_shadow_authority_expiry_after_rollback_refused(self):
        context = prospective.frozen_shadow_context(self.exp, **self.exp.shadow_metrics[prospective.KEY])
        expiry = self.authority["approval"]["limits"]["expires_at_ms"]
        self.store.cleanup = lambda: setattr(self, "now", expiry)
        with patch.object(prospective, "get_active_preselection_context", new=AsyncMock(return_value=context)):
            self.assertIsNone(await prospective.shadow_authority(self.store))

    async def test_shadow_expiry_during_preparation_or_last_cohort_read_refuses_write(self):
        self.now += 20 * DAY
        self.store.annotations = self.annotations
        original_builder = prospective.build_acceptance_snapshot
        expiry = self.authority["approval"]["limits"]["expires_at_ms"]
        def expensive_preparation(inputs, bundle):
            result = original_builder(inputs, bundle)
            self.now = expiry
            return result
        with patch.object(prospective, "build_acceptance_snapshot", side_effect=expensive_preparation):
            result = await self.prepare()
        self.assertEqual(result["reason_code"], "APPROVAL_EXPIRED_OR_OUTLIVES_ARTIFACT", result)
        self.assertNotIn("acceptance", self.exp.decision["operational"])
        self.now = self.initial_now + 20 * DAY
        reads = 0
        def expire_after_second_read():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.now = expiry
        self.store.read_hook = expire_after_second_read
        result = await self.prepare()
        self.assertEqual(result["reason_code"], "APPROVAL_EXPIRED_OR_OUTLIVES_ARTIFACT", result)
        self.assertNotIn("acceptance", self.exp.decision["operational"])

    async def test_excluded_pending_progress_after_cut_does_not_rewrite_accepted_evidence(self):
        self.now += 20 * DAY
        pending = copy.deepcopy(self.annotations[-1])
        pending.update(generation=0, candles=[], resolution={"status": "PENDING", "net_r": None})
        self.store.annotations = [*self.annotations[:-1], pending]
        result = await self.prepare()
        self.assertEqual(result.get("acceptance_state"), acceptance.ACCEPTED, result)
        receipt = copy.deepcopy(self.exp.decision["operational"]["acceptance"])
        self.assertEqual(receipt["prospective_evidence"]["data_quality"]["valid"], 119)
        self.now += DAY
        # Real resolver progresses the excluded row; no metrics are fabricated.
        resumed = prospective.resolve_annotation(SimpleNamespace(frozen_config={
            "r09_pre_selection": {prospective.KEY: pending}}), {
            "candles": self.annotations[-1]["candles"], "as_of": dataset.ms_datetime(self.now)})
        final = resumed["r09_pre_selection"][prospective.KEY]
        self.assertIn(final["resolution"]["status"], prospective.TERMINAL)
        self.store.annotations[-1] = final
        async with self.store() as session:
            measured = await prospective.load_prospective_evidence(session, self.exp, now_ms=self.now)
        self.assertEqual(measured["state"], "GATE_PASSED", measured)
        repeated = await self.prepare(receipt["prospective_evidence"]["cutoff_ms"])
        self.assertTrue(repeated["idempotent"], repeated)
        self.assertEqual(receipt, self.exp.decision["operational"]["acceptance"])
        # Changing a USED outcome remains a genuine drift and fails closed.
        self.store.annotations[0]["resolution"]["net_r"] += .1
        async with self.store() as session:
            bad = await prospective.load_prospective_evidence(session, self.exp, now_ms=self.now)
        self.assertEqual(bad["reason_code"], "PROSPECTIVE_ACCEPTANCE_COHORT_DRIFT", bad)

    async def test_receipt_expiry_during_evidence_query_is_not_reported_as_current(self):
        self.now += 20 * DAY
        self.store.annotations = self.annotations
        prepared = await self.prepare()
        self.assertEqual(prepared.get("acceptance_state"), acceptance.ACCEPTED, prepared)
        expiry = self.exp.decision["operational"]["acceptance"]["valid_until_ms"]
        self.store.read_hook = lambda: setattr(self, "now", expiry)
        async with self.store() as session:
            result = await prospective.load_prospective_evidence(session, self.exp, now_ms=self.now)
        self.assertNotEqual(result["state"], "GATE_PASSED", result)


if __name__ == "__main__":
    unittest.main()
