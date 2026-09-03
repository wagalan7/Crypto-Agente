"""Regressões dos reparos operacionais de snapshot e Web Push."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from models.recommendation_snapshot import RecommendationSnapshot
from models.push_subscription import PushSubscription
from services import push_service


ROOT = Path(__file__).resolve().parents[2]


class SnapshotStatusSchemaTests(unittest.TestCase):
    def test_model_accepts_all_wide_lifecycle_statuses(self):
        status_type = RecommendationSnapshot.__table__.c.status.type
        self.assertGreaterEqual(status_type.length, len("wide_superseded"))
        self.assertGreaterEqual(status_type.length, len("wide_won_tp1_be"))

    def test_existing_database_is_widened_explicitly(self):
        source = (ROOT / "backend" / "db.py").read_text()
        self.assertIn("ALTER COLUMN status TYPE VARCHAR(32)", source)


class PushFailureClassificationTests(unittest.TestCase):
    def test_gone_subscription_is_permanent(self):
        self.assertEqual(
            push_service._permanent_push_failure_reason(Exception("410 Gone")),
            "SUBSCRIPTION_GONE",
        )

    def test_vapid_key_mismatch_is_permanent(self):
        self.assertEqual(
            push_service._permanent_push_failure_reason(
                Exception('400 Bad Request: {"reason":"VapidPkHashMismatch"}')
            ),
            "VAPID_KEY_MISMATCH",
        )

    def test_transient_failure_is_not_deactivated(self):
        self.assertIsNone(
            push_service._permanent_push_failure_reason(Exception("503 timeout"))
        )


class PushSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_vapid_mismatch_returns_false_for_fanout_deactivation(self):
        sub = PushSubscription(
            id=7,
            endpoint="https://web.push.apple.com/example",
            p256dh="key",
            auth="auth",
        )
        with (
            patch.object(push_service, "PUSH_ENABLED", True),
            patch.object(
                push_service,
                "_sync_push",
                side_effect=Exception('400 {"reason":"VapidPkHashMismatch"}'),
            ),
        ):
            self.assertFalse(await push_service._send_one(sub, {"tag": "test"}))


class FrontendRenewalContractTests(unittest.TestCase):
    def test_frontend_compares_rotates_and_persists_vapid_subscription(self):
        source = (
            ROOT / "frontend" / "src" / "components" / "PushSubscribeButton.tsx"
        ).read_text()
        self.assertIn("subscriptionUsesVapidKey", source)
        self.assertIn("ensureCurrentVapidSubscription", source)
        self.assertIn("await sub.unsubscribe()", source)
        self.assertIn("await persistSubscription(sub)", source)


if __name__ == "__main__":
    unittest.main()
