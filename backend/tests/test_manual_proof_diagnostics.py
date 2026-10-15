"""Diagnóstico público não altera a autoridade do reconhecimento manual."""
import unittest
from unittest.mock import AsyncMock, patch

from services import manual_position_service as m
from services import shadow_trade_service as s
from services import execution_reconciliation_service as r


def healthy(**changes):
    row = dict(id=1, state="ACTIVE", symbol="SYN/USDT:USDT", revision=3,
               validated_revision=3, validation_account="test-account",
               validated_generation=7, contract_version="MANUAL_ACK_V1",
               exchange="binance", market="usdm_futures", validation_scope="ACCOUNT",
               validated_at_ms=1_000_000)
    row.update(changes)
    return row


class DiagnosticProof(unittest.TestCase):
    def verdict(self, **changes):
        return m.validation_proof_verdict(healthy(**changes), account_scope="test-account",
                                          manual_generation=7, now_ms=1_000_001)

    def test_valid(self):
        self.assertTrue(self.verdict()["valid"])

    def test_failure_is_specific_and_does_not_expose_identity(self):
        cases = [({"state": "INVALIDATED"}, "MANUAL_ACK_RECONFIRMATION_REQUIRED"),
                 ({"state": "WAITING_ORDERS"}, "MANUAL_ACK_WAITING_ORDERS"),
                 ({"revision": 4}, "MANUAL_ACK_REVISION_MISMATCH"),
                 ({"validation_account": "other"}, "MANUAL_ACK_ACCOUNT_MISMATCH"),
                 ({"validated_generation": 8}, "MANUAL_ACK_GENERATION_MISMATCH"),
                 ({"contract_version": "bad"}, "MANUAL_ACK_CONTRACT_MISMATCH"),
                 ({"exchange": "bybit"}, "MANUAL_ACK_MARKET_MISMATCH"),
                 ({"validation_scope": "bad"}, "MANUAL_ACK_SCOPE_MISMATCH"),
                 ({"validated_at_ms": None}, "MANUAL_ACK_TIMESTAMP_INVALID"),
                 ({"validated_at_ms": 2_000_000}, "MANUAL_ACK_TIMESTAMP_FUTURE"),
                 ({"validated_at_ms": 1}, "MANUAL_ACK_VALIDATION_EXPIRED")]
        for change, reason in cases:
            with self.subTest(change=change):
                verdict = self.verdict(**change)
                self.assertFalse(verdict["valid"])
                self.assertEqual(verdict["reason_code"], reason)
                self.assertNotIn("test-account", str(verdict))

    def test_boolean_wrapper_preserves_verdict(self):
        for state in ("ACTIVE", "INVALIDATED", "WAITING_ORDERS", "CLOSED"):
            row = healthy(state=state)
            args = dict(account_scope="test-account", manual_generation=7, now_ms=1_000_001)
            self.assertEqual(m.proof_is_valid(row, **args),
                             m.validation_proof_verdict(row, **args)["valid"])

    def test_skip_preserves_specific_reason_but_not_arbitrary_error_text(self):
        self.assertEqual(s._entry_intent_skip_reason({"decision": "BLOCKED_CAPACITY",
                         "reason": "MANUAL_ACK_PROOF_STALE"}), "MANUAL_ACK_PROOF_STALE")
        self.assertEqual(s._entry_intent_skip_reason({"decision": "BLOCKED_CAPACITY",
                         "reason": {"secret": "not public"}}), "BLOCKED_CAPACITY")
        self.assertEqual(s._entry_intent_skip_reason({"reason": "Bearer private value"}),
                         "ENTRY_INTENT_DENIED")


class ReadOnlySummary(unittest.IsolatedAsyncioTestCase):
    async def test_invalidated_is_blocked_even_with_zero_incidents(self):
        with patch.object(m, "active_acknowledgements", AsyncMock(return_value={
                "ok": True, "acks": [dict(healthy(state="INVALIDATED"), account_scope="test-account")]})), \
             patch.object(m, "account_validation_state", AsyncMock(return_value={"blocked": False, "generation": 7})), \
             patch.object(m, "current_account_scope", return_value="test-account"), \
             patch.object(m, "pending_validation_failure", return_value=None):
            summary = await m.manual_validation_summary()
        self.assertIs(summary["new_entries_blocked"], True)
        self.assertEqual(summary["reason_code"], "MANUAL_ACK_RECONFIRMATION_REQUIRED")
        self.assertNotIn("test-account", str(summary))

    async def test_unavailable_does_not_become_clear(self):
        with patch.object(m, "active_acknowledgements", AsyncMock(return_value={"ok": False, "acks": []})):
            summary = await m.manual_validation_summary()
        self.assertIsNone(summary["new_entries_blocked"])
        self.assertFalse(summary["ok"])

    async def test_clear_is_observational_and_foreign_account_is_not_exposed(self):
        rows = [dict(healthy(validated_at_ms=m._now_ms()), account_scope="test-account"),
                dict(healthy(state="INVALIDATED"), account_scope="foreign-account")]
        with patch.object(m, "active_acknowledgements", AsyncMock(return_value={"ok": True, "acks": rows})), \
             patch.object(m, "account_validation_state", AsyncMock(return_value={"blocked": False, "generation": 7})), \
             patch.object(m, "current_account_scope", return_value="test-account"), \
             patch.object(m, "pending_validation_failure", return_value=None):
            summary = await m.manual_validation_summary()
        self.assertIs(summary["new_entries_blocked"], False)
        self.assertEqual(summary["scope"], "MANUAL_VALIDATION_ONLY")
        self.assertNotIn("account", str(summary))

    async def test_public_status_carries_manual_block_without_open_incident(self):
        summary = {"ok": True, "new_entries_blocked": True,
                   "reason_code": "MANUAL_ACK_RECONFIRMATION_REQUIRED"}
        with patch.object(r, "_get_repo") as repo, \
             patch.object(m, "manual_validation_summary", AsyncMock(return_value=summary)):
            repo.return_value.list_all = AsyncMock(return_value=[])
            result = await r.get_status()
        self.assertEqual(result["open_total"], 0)
        self.assertEqual(result["manual_validation"], summary)

    async def test_read_failure_is_unknown_not_authority(self):
        with patch.object(m, "active_acknowledgements", AsyncMock(return_value={"ok": True, "acks": []})), \
             patch.object(m, "account_validation_state", AsyncMock(side_effect=RuntimeError("private"))):
            result = await m.manual_validation_summary()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["new_entries_blocked"])
        self.assertNotIn("private", str(result))

    async def test_summary_failure_does_not_hide_incidents(self):
        with patch.object(r, "_get_repo") as repo, \
             patch.object(m, "manual_validation_summary", AsyncMock(side_effect=RuntimeError("private"))):
            repo.return_value.list_all = AsyncMock(return_value=[])
            result = await r.get_status()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["manual_validation"]["new_entries_blocked"])
        self.assertNotIn("private", str(result))
