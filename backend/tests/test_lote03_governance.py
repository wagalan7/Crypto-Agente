"""Governance contracts and transactions; synthetic official study, no network.

Fake sessions exercise callers, not PostgreSQL isolation (root PG harness does
that separately). The private test opt-in never represents human LIVE approval.
"""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import operational_governance_service as g
from services import research_study_service as rs
from services import strategy_evidence_service as se
from services import preselection_experiment_service as pe
from services import score_v3_calibration_service as c
from tests.test_lote02_official_study_cycle import official_fixture


CHAMPION = {"schema_version": 1, "SCORE_MIN": 55, "test_only": True}
_FIXTURE = None


def governance_fixture():
    """Shared PG fixture: actual export/replay/fitting, visibly TEST_ONLY."""
    global _FIXTURE
    if _FIXTURE is None:
        manifest, data, exported, prices = official_fixture()
        now = manifest["split"]["as_of_ms"]
        report = rs.run_study(manifest=manifest, dataset=data, export_manifest=exported,
            prices=prices, request=rs.calibration_request(event=c.EVENT_NET_POSITIVE,
                valid_for_ms=86400000), now_ms=now)
        assert report["ok"] and rs.validate_study_report(report, now_ms=now)["ok"]
        contract = report["study"]["contract"]
        envelope = pe.build_preselection_envelope(replay_config=contract["candidate_config"],
            contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
        exp = SimpleNamespace(id=17, experiment_key="lote03-synthetic-candidate",
            candidate_config=envelope, candidate_hash=se.canonical_hash(envelope),
            champion_hash=se.canonical_hash(contract["baseline_config"]),
            dataset_fingerprint=contract["dataset_fingerprint"],
            dataset_cutoff=rs.ds.ms_datetime(contract["cutoff_ms"]) if hasattr(rs, "ds") else None,
            offline_metrics={"study": copy.deepcopy(report["study"])},
            shadow_metrics={}, decision={"previous_field": "preserved"}, status="OFFLINE_VALIDATED")
        from services.research_dataset_service import ms_datetime
        exp.dataset_cutoff = ms_datetime(contract["cutoff_ms"])
        with patch.object(se, "discover_champion_config", return_value=CHAMPION):
            bundle = g.build_bundle(exp, report, CHAMPION, now_ms=now)
        _FIXTURE = exp, report, bundle, now
    return copy.deepcopy(_FIXTURE)


def approval_payload(bundle, now, purpose="SHADOW", *, acceptance_record=None):
    return {"confirm": True, "purpose": purpose, "references": ["TEST_ONLY:engineering-fixture"],
        "limits": {"symbols": ["SYN/USDT:USDT"],
            "playbooks": [bundle["manifest"]["candidate"]["selection_rule"]["playbook"]],
            "max_orders": 2, "max_risk_pct": 0.5, "expires_at_ms": now + 3600000},
        "validity": {"id": "TEST_ONLY:approval-001", "hash": g.approval_validity_hash(
            bundle, purpose, (acceptance_record or {}).get("record_hash"))}}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.exp, self.report, self.bundle, self.now = governance_fixture()
        self.patches = [patch.object(se, "discover_champion_config", return_value=CHAMPION),
            patch.object(g, "_ALLOW_TEST_APPROVALS", True),
            patch.object(g.time, "time", return_value=self.now / 1000),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_shared_official_fixture_is_supported_but_test_only(self):
        self.assertFalse(self.report["real_study_allowed"])
        self.assertEqual(self.bundle["calibration_artifact"]["state"], "OOS_VALIDATED")
        # OOS execution alone no longer grants CANARY: preserve SHADOW positive
        # and independently assert the added acceptance boundary.
        self.assertTrue(g.validate_bundle(self.exp, self.bundle, now_ms=self.now, purpose="SHADOW")["ok"])
        self.assertFalse(g.validate_bundle(self.exp, self.bundle, now_ms=self.now)["ok"])
        self.assertEqual(self.bundle["operational_semantics"], g.OPERATIONAL_SEMANTICS)

    def test_test_only_bundle_never_has_operational_authority_by_default(self):
        with patch.object(g, "_ALLOW_TEST_APPROVALS", False):
            self.assertEqual(g.validate_bundle(self.exp, self.bundle, now_ms=self.now)["reason_code"],
                             "TEST_ONLY_BUNDLE_NOT_OPERATIONAL")

    def test_unknown_missing_and_forged_bundle_fields_fail(self):
        for bad in (None, [], {**self.bundle, "extra": True},
                    {k: v for k, v in self.bundle.items() if k != "policy_config"}):
            self.assertFalse(g.validate_bundle(self.exp, bad, now_ms=self.now)["ok"])
        for field, value in (("candidate_hash", "a" * 64), ("study_identity", {}),
                             ("policy_config", {}), ("operational_semantics", {"core": "new"})):
            bad = copy.deepcopy(self.bundle)
            bad[field] = value
            bad["bundle_hash"] = g.digest({k: v for k, v in bad.items() if k != "bundle_hash"})
            self.assertFalse(g.validate_bundle(self.exp, bad, now_ms=self.now)["ok"])

    def test_champion_drift_expiry_and_frozen_contract_drift_block(self):
        with patch.object(se, "discover_champion_config", return_value={"new": True}):
            self.assertFalse(g.validate_bundle(self.exp, self.bundle, now_ms=self.now)["ok"])
        self.assertFalse(g.validate_bundle(self.exp, self.bundle, now_ms=self.now + 86400001)["ok"])
        self.exp.offline_metrics["study"]["contract"]["cutoff_ms"] += 1
        self.assertFalse(g.validate_bundle(self.exp, self.bundle, now_ms=self.now)["ok"])

    def test_closed_approval_literal_confirm_and_typed_limits(self):
        good = approval_payload(self.bundle, self.now)
        self.assertTrue(g.validate_approval_payload(good, self.bundle, now_ms=self.now)["ok"])
        for key, bads in (("max_orders", (True, 1.5, "2", 0, 101)),
                          ("max_risk_pct", (True, "1", float("nan"), float("inf"), 0, 2.1)),
                          ("expires_at_ms", (True, self.now, self.now + 86400001))):
            for value in bads:
                bad = copy.deepcopy(good)
                bad["limits"][key] = value
                self.assertFalse(g.validate_approval_payload(bad, self.bundle, now_ms=self.now)["ok"])
        for value in (1, "true", False, None):
            bad = {**good, "confirm": value}
            self.assertFalse(g.validate_approval_payload(bad, self.bundle, now_ms=self.now)["ok"])
        self.assertFalse(g.validate_approval_payload({**good, "approved": True}, self.bundle, now_ms=self.now)["ok"])

    def test_purposes_are_not_interchangeable(self):
        from tests.test_research_acceptance_governance import accepted_governance_fixture
        exp, _, bundle, now = accepted_governance_fixture()
        receipt = exp.decision["operational"]["acceptance"]
        for purpose in g.PURPOSES:
            self.assertTrue(g.validate_approval_payload(approval_payload(bundle, now, purpose,
                acceptance_record=receipt), bundle, now_ms=now, acceptance_record=receipt)["ok"])
        stale = approval_payload(bundle, now, "CANARY", acceptance_record=receipt)
        stale["validity"]["hash"] = bundle["bundle_hash"]
        self.assertFalse(g.validate_approval_payload(stale, bundle, now_ms=now,
            acceptance_record=receipt)["ok"])
        self.assertFalse(g.validate_approval_payload(approval_payload(self.bundle, self.now, "LIVE"),
            self.bundle, now_ms=self.now)["ok"])

    def test_risk_cannot_exceed_actual_lower_ceiling(self):
        with patch.dict("os.environ", {"EXCHANGE_MAX_RISK_PCT": "0.25"}):
            self.assertFalse(g.validate_approval_payload(approval_payload(self.bundle, self.now),
                self.bundle, now_ms=self.now)["ok"])

    def test_selector_invalid_and_default_legacy(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(g.selected_mode(), "LEGACY")
        for raw in ("", "-1", "1.2", "True", "１２", "0"):
            with patch.dict("os.environ", {"R13_OPERATIONAL_SELECTOR": "CANDIDATE", "R13_OPERATIONAL_EXPERIMENT_ID": raw}):
                self.assertEqual(g.selected_mode(), "INVALID")

    def test_pending_two_mutations_do_not_clear_each_other(self):
        first, second = g._begin_mutation(), g._begin_mutation()
        try:
            g._mutation_finished(first)
            self.assertTrue(g._LOCAL_PENDING)
            g._mutation_finished(second)
            self.assertFalse(g._LOCAL_PENDING)
        finally:
            g._mutation_finished(first)
            g._mutation_finished(second, recover=True)


class Result:
    def __init__(self, row):
        self.row = row
    def scalar_one_or_none(self):
        return self.row


class MemorySessions:
    """No claims of SQL concurrency; records lock order and state changes."""
    def __init__(self, exp, report):
        self.exp, self.report, self.state = exp, report, None
        self.calls = []
        self.fail = False
        self.cleanup = None

    def __call__(self):
        store = self
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                if store.cleanup:
                    store.cleanup()
            async def execute(self, statement, params=None):
                if store.fail:
                    raise RuntimeError("TEST_ONLY persistence unavailable")
                sql = str(statement)
                if "pg_advisory" in sql:
                    store.calls.append(("advisory", params["k"]))
                    return Result(None)
                if "strategy_experiments" in sql:
                    store.calls.append(("experiment", statement._for_update_arg is not None))
                    wanted = next(iter(statement.compile().params.values()))
                    return Result(store.exp if wanted == store.exp.id else None)
                store.calls.append(("state", statement._for_update_arg is not None))
                key = next(iter(statement.compile().params.values()))
                if key == g.STATE_KEY:
                    return Result(store.state)
                return Result(SimpleNamespace(payload=copy.deepcopy(store.report)))
            def add(self, row):
                store.state = row
            async def flush(self):
                pass
            async def commit(self):
                store.calls.append(("commit",))
            async def rollback(self):
                store.calls.append(("rollback",))
        return Session()


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exp, self.report, self.bundle, self.now = governance_fixture()
        self.store = MemorySessions(self.exp, self.report)
        g._PENDING_FENCES.clear()
        g._FAILED_FENCES.clear()
        g._LOCAL_PENDING = False
        self.patches = [patch.object(se, "discover_champion_config", return_value=CHAMPION),
            patch.object(g, "_ALLOW_TEST_APPROVALS", True),
            patch.object(g.time, "time", return_value=self.now / 1000),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def register(self, purpose="SHADOW"):
        first = await g.register_bundle(self.store, self.exp.id,
            {"confirm": True, "calibration_study_key": self.report["study_key"], "champion_config": CHAMPION}, "TEST_ONLY_OPERATOR")
        self.assertTrue(first["ok"], first)
        self.bundle = self.exp.decision["operational"]["bundle"]
        result = await g.register_approval(self.store, self.exp.id,
            approval_payload(self.bundle, self.now, purpose,
                acceptance_record=self.exp.decision["operational"].get("acceptance")),
            "TEST_ONLY_OPERATOR", test_only=True)
        self.assertTrue(result["ok"], result)
        return result

    async def test_legacy_and_off_zero_query(self):
        def no_session():
            raise AssertionError("selector off must not query")
        for mode in ("LEGACY", "OFF"):
            with patch.dict("os.environ", {"R13_OPERATIONAL_SELECTOR": mode}):
                view = await g.load_view(no_session)
                self.assertTrue(view["ok"])
                self.assertIsNone(view["bundle"])

    async def test_register_bundle_and_approval_idempotent_preserve_existing_json(self):
        result = await self.register()
        generation = self.store.state.generation
        again = await self.register()
        self.assertEqual(again["approval_id"], result["approval_id"])
        self.assertEqual(self.store.state.generation, generation)
        self.assertEqual(self.exp.decision["previous_field"], "preserved")
        self.assertEqual(len(self.exp.decision["operational"]["approvals"]), 1)
        self.assertEqual(self.store.calls[0], ("advisory", g.LOCK_KEY))

    async def test_shadow_authority_fence_revoke_and_revoked_never_reactivates(self):
        result = await self.register()
        view = await g.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.assertTrue(view["ok"], view)
        self.assertTrue(g.sync_authority_valid(view))
        revoked = await g.revoke_approval(self.store, self.exp.id, result["approval_id"],
            result["generation"], "TEST_ONLY_OPERATOR")
        self.assertTrue(revoked["ok"], revoked)
        self.assertFalse(g.sync_authority_valid(view))
        self.assertFalse((await g.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now))["ok"])
        attempt = await g.register_approval(self.store, self.exp.id, approval_payload(self.bundle, self.now),
            "TEST_ONLY_OPERATOR", test_only=True)
        self.assertEqual(attempt["reason_code"], "APPROVAL_ALREADY_REVOKED")
        self.assertFalse(g._LOCAL_PENDING)  # proven rejection is not uncertain DB failure

    async def test_state_first_lock_order_and_no_nested_p05_lock(self):
        result = await self.register()
        self.store.calls.clear()
        async with self.store() as session:
            value = await g.assert_authority_in_session(session, exp_id=self.exp.id, purpose="SHADOW",
                approval_id=result["approval_id"], expected_generation=result["generation"], now_ms=self.now)
        self.assertTrue(value["ok"], value)
        self.assertEqual(self.store.calls[:2], [("state", True), ("experiment", True)])
        self.assertNotIn(("advisory", g.LOCK_KEY), self.store.calls)

    async def test_generation_stale_and_purpose_mismatch_fail(self):
        # Reach the old generation/purpose controls with valid acceptance;
        # an unrelated early OOS-only refusal would not prove these controls.
        from tests.test_research_acceptance_governance import accepted_governance_fixture
        self.exp, self.report, self.bundle, self.now = accepted_governance_fixture()
        self.store = MemorySessions(self.exp, self.report)
        with patch.object(g.time, "time", return_value=self.now / 1000):
            await self._generation_and_purpose_controls()

    async def _generation_and_purpose_controls(self):
        result = await self.register()
        async with self.store() as session:
            stale = await g.assert_authority_in_session(session, exp_id=self.exp.id, purpose="SHADOW",
                approval_id=result["approval_id"], expected_generation=0, now_ms=self.now)
            wrong = await g.assert_authority_in_session(session, exp_id=self.exp.id, purpose="CANARY",
                approval_id=result["approval_id"], expected_generation=result["generation"], now_ms=self.now)
        self.assertEqual(stale["reason_code"], "OPERATIONAL_GENERATION_STALE")
        self.assertEqual(wrong["reason_code"], "HUMAN_APPROVAL_MISSING_OR_REVOKED")

    async def test_payload_hash_tampering_fails(self):
        result = await self.register()
        self.exp.decision["operational"]["approvals"][0]["limits"]["max_orders"] = 99
        async with self.store() as session:
            value = await g.assert_authority_in_session(session, exp_id=self.exp.id, purpose="SHADOW",
                approval_id=result["approval_id"], expected_generation=result["generation"], now_ms=self.now)
        self.assertEqual(value["reason_code"], "HUMAN_APPROVAL_IDENTITY_INVALID")

    async def test_failed_persistence_blocks_local_authority_and_rollback_recovers(self):
        result = await self.register()
        old = await g.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.store.fail = True
        failure = await g.revoke_approval(self.store, self.exp.id, result["approval_id"],
            result["generation"], "TEST_ONLY_OPERATOR")
        self.assertFalse(failure["ok"])
        self.assertTrue(g._LOCAL_PENDING)
        self.assertFalse(g.sync_authority_valid(old))
        self.store.fail = False
        safe = await g.rollback(self.store, result["generation"], "TEST_ONLY_OPERATOR", "TEST_ONLY recovery")
        self.assertTrue(safe["ok"], safe)
        self.assertFalse(g._LOCAL_PENDING)
        self.assertEqual(self.store.state.payload["mode"], "BLOCKED")

    async def test_cleanup_failure_never_returns_revalidated_authority(self):
        await self.register()
        self.store.cleanup = lambda: g._begin_mutation()
        value = await g.load_view(self.store, self.exp.id, purpose="SHADOW", now_ms=self.now)
        self.assertEqual(value["reason_code"], "LOCAL_AUTHORITY_FENCE_ADVANCED")

    async def test_canary_needs_promoted_ref_and_distinct_purpose(self):
        from tests.test_research_acceptance_governance import accepted_governance_fixture
        self.exp, self.report, self.bundle, self.now = accepted_governance_fixture()
        self.store = MemorySessions(self.exp, self.report)
        with patch.object(g.time, "time", return_value=self.now / 1000):
            result = await self.register("CANARY")
            with patch.dict("os.environ", {"R13_OPERATIONAL_SELECTOR": "CANDIDATE", "R13_OPERATIONAL_EXPERIMENT_ID": "17"}):
                no_ref = await g.load_view(self.store, now_ms=self.now)
        self.assertEqual(no_ref["reason_code"], "CANDIDATE_NOT_PROMOTED")

    async def test_get_status_does_not_write(self):
        value = await g.get_status(self.store)
        self.assertTrue(value["ok"])
        self.assertIsNone(self.store.state)
        self.assertNotIn(("commit",), self.store.calls)


if __name__ == "__main__":
    unittest.main()
