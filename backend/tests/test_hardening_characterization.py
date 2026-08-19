"""P01: testes de caracterização dos contratos críticos existentes.

Estes testes registram o comportamento atual; não propõem novas regras.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as binance  # noqa: E402
from services import recommendation_service as recommendation  # noqa: E402
from services import regime_service as regime  # noqa: E402
from services import shadow_trade_service as shadow  # noqa: E402
from services import snapshot_service as snapshot  # noqa: E402


class SizingCharacterizationTests(unittest.TestCase):
    def test_hard_risk_cap_applies_to_nominal_position(self):
        with patch.object(shadow, "MAX_RISK_PCT_HARD", 2.0), patch.object(
            shadow, "MAX_MARGIN_PCT_PER_TRADE", 100.0
        ), patch.object(shadow, "MIN_NOTIONAL_USD", 10.0):
            result = shadow._compute_qty(100.0, 90.0, 5.0, 1_000.0, leverage=1)

        self.assertEqual(result["status"], "risk_capped")
        self.assertAlmostEqual(result["risk_pct_real"], 2.0)
        self.assertAlmostEqual(result["qty"] * 10.0, 20.0)

    def test_margin_cap_reduces_notional(self):
        with patch.object(shadow, "MAX_RISK_PCT_HARD", 10.0), patch.object(
            shadow, "MAX_MARGIN_PCT_PER_TRADE", 15.0
        ), patch.object(shadow, "MIN_NOTIONAL_USD", 10.0):
            result = shadow._compute_qty(100.0, 99.0, 2.0, 1_000.0, leverage=2)

        self.assertEqual(result["status"], "capped")
        self.assertAlmostEqual(result["notional_usd"], 300.0)

    def test_min_notional_is_rejected_when_it_breaks_risk_cap(self):
        with patch.object(shadow, "MAX_RISK_PCT_HARD", 2.0), patch.object(
            shadow, "MAX_MARGIN_PCT_PER_TRADE", 100.0
        ), patch.object(shadow, "MIN_NOTIONAL_USD", 50.0):
            result = shadow._compute_qty(100.0, 50.0, 0.1, 100.0, leverage=1)

        self.assertEqual(result["status"], "skip")
        self.assertGreater(result["risk_pct_real"], 2.0)


class ProtectionCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_protection_creates_stop_and_two_take_profits(self):
        requests = []

        async def signed_request(method, path, params=None):
            requests.append((method, path, dict(params or {})))
            return {"ok": True, "result": {"algoId": str(len(requests))}}

        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)
        ), patch.object(binance, "_signed_request", side_effect=signed_request), patch.object(
            binance, "RUNNER_ENABLED", False
        ):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 10.0, stop_loss=90.0, tp1=110.0, tp2=120.0
            )

        self.assertTrue(result["sl_ok"] and result["tp1_ok"] and result["tp2_ok"])
        self.assertAlmostEqual(result["tp1_qty"], 4.5)
        self.assertEqual([r[2]["type"] for r in requests], [
            "STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET"
        ])
        self.assertEqual(requests[0][2]["closePosition"], "true")
        self.assertEqual(requests[1][2]["reduceOnly"], "true")
        self.assertEqual(requests[2][2]["closePosition"], "true")

    async def test_live_dedup_reuses_existing_stop(self):
        existing = {
            "ok": True,
            "orders": [{
                "type": "STOP_MARKET", "side": "SELL",
                "trigger_price": 90.0, "algo_id": "existing-sl",
            }],
        }
        post = AsyncMock(return_value={"ok": True, "result": {"algoId": "new"}})
        with patch.object(binance, "get_open_algo_orders", AsyncMock(return_value=existing)), patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)
        ), patch.object(binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)), patch.object(
            binance, "_signed_request", post
        ):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 1.0, stop_loss=90.0, dedup_live=True
            )

        self.assertEqual(result["sl_order_id"], "existing-sl")
        post.assert_not_awaited()


class SignalAndOutcomeCharacterizationTests(unittest.TestCase):
    def test_counter_trend_brake_detects_short_against_bullish_htf(self):
        mtf = {"higher_tfs": [{"timeframe": "4h", "ema_aligned": "bullish"}]}
        with patch.object(regime, "CT_BRAKE_ENABLED", True), patch.object(
            regime, "CT_BRAKE_MIN_TFS", 1
        ):
            reason = regime.symbol_counter_trend(mtf, "short")
            allowed = regime.symbol_counter_trend(mtf, "long")

        self.assertIn("contra-tendência", reason)
        self.assertIsNone(allowed)

    def test_mixed_higher_timeframes_fail_open(self):
        mtf = {"higher_tfs": [
            {"timeframe": "4h", "ema_aligned": "bullish"},
            {"timeframe": "1d", "ema_aligned": "bearish"},
        ]}
        with patch.object(regime, "CT_BRAKE_ENABLED", True), patch.object(
            regime, "CT_BRAKE_MIN_TFS", 1
        ):
            self.assertIsNone(regime.symbol_counter_trend(mtf, "short"))
            self.assertIsNone(regime.symbol_counter_trend(mtf, "long"))

    def test_live_trade_status_mapping_uses_fixed_snapshot_r(self):
        self.assertEqual(snapshot._REAL_TO_SNAP_OUTCOME["closed_stop"], ("lost", -1.0))
        self.assertEqual(snapshot._REAL_TO_SNAP_OUTCOME["closed_tp2"], ("won_tp2", 1.5))
        self.assertEqual(snapshot._REAL_TO_SNAP_OUTCOME["closed_tp1"], ("won_tp1", 0.6))

    def test_htf_attachment_uses_only_strictly_higher_timeframes(self):
        def signal(tf, e1, e2, e3):
            return SimpleNamespace(
                timeframe=tf,
                indicators=SimpleNamespace(ema9=e1, ema21=e2, ema50=e3),
                mtf=None,
            )

        s15 = signal("15m", 3, 2, 1)
        s1h = signal("1h", 3, 2, 1)
        s4h = signal("4h", 1, 2, 3)
        recommendation._attach_htf_ema_trend([(s15, 1), (s1h, 1), (s4h, 1)])

        self.assertEqual([x["timeframe"] for x in s15.mtf["higher_tfs"]], ["1h", "4h"])
        self.assertEqual([x["timeframe"] for x in s1h.mtf["higher_tfs"]], ["4h"])
        self.assertIsNone(s4h.mtf)


if __name__ == "__main__":
    unittest.main()
