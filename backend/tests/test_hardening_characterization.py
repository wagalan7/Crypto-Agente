"""P01/P02: caracterização e invariantes do fail-safe de execução.

Estes testes registram o comportamento atual; não propõem novas regras.
"""
from __future__ import annotations

import asyncio
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


async def _p04b_market_preflight(qty, _rules):
    """Contrato explícito para testes legados que mockam o transporte."""
    return {
        "ok": True,
        "approved_qty": qty,
        "checks": {
            "best_executable_price": 100.0,
            "vwap_price": 100.0,
            "worst_price": 100.0,
        },
    }


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
                "close_position": True,
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

    async def test_live_dedup_rejects_undersized_quantity_stop(self):
        existing = {
            "ok": True,
            "orders": [{
                "type": "STOP_MARKET", "side": "SELL",
                "trigger_price": 90.0, "algo_id": "old-small-sl",
                "close_position": False, "quantity": 0.2,
            }],
        }
        post = AsyncMock(return_value={"ok": True, "result": {"algoId": "new-full-sl"}})
        with patch.object(binance, "get_open_algo_orders", AsyncMock(return_value=existing)), patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)
        ), patch.object(binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)), patch.object(
            binance, "_signed_request", post
        ):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 1.0, stop_loss=90.0, dedup_live=True
            )

        self.assertEqual(result["sl_order_id"], "new-full-sl")
        post.assert_awaited_once()

    async def test_stop_without_algo_id_is_not_confirmed_and_tps_are_not_sent(self):
        post = AsyncMock(return_value={"ok": True, "result": {}})
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)
        ), patch.object(binance, "_signed_request", post), patch.object(
            binance, "_ALGO_MAX_ATTEMPTS", 1
        ):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 1.0, stop_loss=90.0, tp1=110.0, tp2=120.0
            )

        self.assertFalse(result["sl_ok"])
        self.assertFalse(result["tp1_ok"])
        self.assertFalse(result["tp2_ok"])
        self.assertEqual(post.await_count, 2)  # SL closePosition + fallback qty; nenhum TP


class EntryFailSafeTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_entry_with_failed_stop_is_flattened_using_executed_qty(self):
        entry = {"ok": True, "result": {
            "orderId": "entry-1", "status": "FILLED", "executedQty": "0.4",
        }, "raw": {}}
        failed_protection = {
            "sl_ok": False, "sl_order_id": None, "sl_msg": "rejected",
            "tp1_ok": False, "tp1_order_id": None, "tp1_msg": "not sent", "tp1_qty": 0,
            "tp2_ok": False, "tp2_order_id": None, "tp2_msg": "not sent",
            "tp1_skipped": False,
        }
        close = AsyncMock(return_value={
            "ok": True, "confirmed_flat": True, "attempts": 1,
            "requested_qty": 0.4, "closed_qty": 0.4, "remaining_qty": 0,
        })
        with patch.object(binance, "_round_qty", AsyncMock(return_value=0.4)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "place_protection_orders", AsyncMock(return_value=failed_protection)), patch.object(
            binance, "_emergency_close_after_stop_failure", close
        ), patch.object(binance, "get_open_algo_orders", AsyncMock(return_value={
            "ok": True, "orders": [],
        })), patch.object(binance, "_fire_telegram"):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0, take_profit=120.0,
                client_order_id="cw-market-fail",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["safety_state"], "FLATTENED_AFTER_SL_FAILURE")
        self.assertFalse(result["position_open"])
        self.assertEqual(close.await_args.args[2], 0.4)

    async def test_market_protection_is_sized_from_actual_fill(self):
        entry = {"ok": True, "result": {
            "orderId": "entry-fill", "status": "FILLED", "executedQty": "0.4",
        }, "raw": {}}
        protected = {
            "sl_ok": True, "sl_order_id": "sl-1", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": "tp1-1", "tp1_msg": None, "tp1_qty": 0.18,
            "tp2_ok": True, "tp2_order_id": "tp2-1", "tp2_msg": None,
            "tp1_skipped": False,
        }
        protect = AsyncMock(return_value=protected)
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "place_protection_orders", protect):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                tp1=110.0, take_profit=120.0,
                client_order_id="cw-actual-fill",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(protect.await_args.args[2], 0.4)

    async def test_protection_exception_after_fill_also_triggers_flatten(self):
        entry = {"ok": True, "result": {
            "orderId": "entry-2", "status": "FILLED", "executedQty": "1",
        }, "raw": {}}
        close = AsyncMock(return_value={
            "ok": True, "confirmed_flat": True, "attempts": 1,
            "requested_qty": 1.0, "closed_qty": 1.0, "remaining_qty": 0,
        })
        with patch.object(binance, "_round_qty", AsyncMock(return_value=1.0)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(
            binance, "place_protection_orders", AsyncMock(side_effect=RuntimeError("boom"))
        ), patch.object(binance, "_emergency_close_after_stop_failure", close), patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value={"ok": True, "orders": []})
        ), patch.object(
            binance, "_fire_telegram"
        ):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                client_order_id="cw-exception",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["emergency_close_ok"])
        close.assert_awaited_once()

    async def test_tp_timeout_preserves_confirmed_stop_and_never_flattens(self):
        entry = {"ok": True, "result": {
            "orderId": "entry-tp-timeout", "status": "FILLED", "executedQty": "0.4",
        }, "raw": {}}
        stop_only = {
            "sl_ok": True, "sl_order_id": "sl-safe", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0.0,
            "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
            "tp1_skipped": False, "is_runner": False, "runner_qty": 0.0,
        }
        protect = AsyncMock(side_effect=[stop_only, asyncio.TimeoutError()])
        emergency = AsyncMock()
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "place_protection_orders", protect), patch.object(
            binance, "_emergency_close_after_stop_failure", emergency
        ):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                tp1=110.0, take_profit=120.0,
                client_order_id="cw-tp-timeout",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["safety_state"], "STOP_CONFIRMED_TP_DEGRADED")
        self.assertEqual(result["sl_order_id"], "sl-safe")
        self.assertFalse(result["tp1_ok"])
        self.assertFalse(result["tp2_ok"])
        self.assertEqual(protect.await_count, 2)
        self.assertIsNone(protect.await_args_list[0].kwargs["tp1"])
        self.assertIsNone(protect.await_args_list[0].kwargs["tp2"])
        self.assertIsNone(protect.await_args_list[1].kwargs["stop_loss"])
        emergency.assert_not_awaited()

    async def test_sl_ok_without_order_id_is_treated_as_failure(self):
        close = AsyncMock(return_value={
            "ok": True, "confirmed_flat": True, "attempts": 1,
            "requested_qty": 1.0, "closed_qty": 1.0, "remaining_qty": 0,
        })
        protection = {"sl_ok": True, "sl_order_id": None, "tp1_ok": True, "tp2_ok": True}
        with patch.object(binance, "_emergency_close_after_stop_failure", close), patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value={"ok": True, "orders": []})
        ), patch.object(
            binance, "_fire_telegram"
        ):
            state = await binance._enforce_entry_safety(
                "BTCUSDT", "BUY", 1.0, 90.0, protection,
                client_order_id_prefix="cw-no-id",
            )

        self.assertEqual(state["safety_state"], "FLATTENED_AFTER_SL_FAILURE")

    async def test_partial_emergency_close_retries_only_remaining_qty(self):
        posts = []

        async def post(_method, _path, params):
            posts.append(dict(params))
            if len(posts) == 1:
                return {"ok": True, "result": {
                    "orderId": "c1", "status": "PARTIALLY_FILLED", "executedQty": "0.4",
                }}
            return {"ok": True, "result": {
                "orderId": "c2", "status": "FILLED", "executedQty": "0.6",
            }}

        fresh = AsyncMock(side_effect=[(1.0, "fresh"), (0.6, "fresh"), (0.0, "fresh")])
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", side_effect=post
        ), patch.object(binance, "_fresh_position_size", fresh), patch.object(
            binance, "get_order", AsyncMock(return_value={
                "ok": True, "status": "CANCELED", "executed_qty": 0.4,
            })
        ), patch.object(
            binance.asyncio, "sleep", AsyncMock()
        ):
            result = await binance._emergency_close_after_stop_failure("BTCUSDT", "BUY", 1.0)

        self.assertTrue(result["confirmed_flat"])
        self.assertEqual([p["quantity"] for p in posts], [1.0, 0.6])
        self.assertTrue(all(p["reduceOnly"] == "true" for p in posts))

    async def test_decimal_remaining_never_drops_a_step_after_partial_fill(self):
        posts = []

        async def post(_method, _path, params):
            posts.append(dict(params))
            if len(posts) == 1:
                return {"ok": True, "result": {
                    "orderId": "c-dec-1", "status": "PARTIALLY_FILLED", "executedQty": "0.1",
                }}
            return {"ok": True, "result": {
                "orderId": "c-dec-2", "status": "FILLED", "executedQty": "0.2",
            }}

        fresh = AsyncMock(side_effect=[(0.3, "fresh"), (0.2, "fresh"), (0.0, "fresh")])
        round_qty = AsyncMock(side_effect=lambda _s, q: binance._floor_to_step(q, 0.1))
        with patch.object(binance, "_round_qty", round_qty), patch.object(
            binance, "_signed_request", side_effect=post
        ), patch.object(binance, "_fresh_position_size", fresh), patch.object(
            binance, "get_order", AsyncMock(return_value={
                "ok": True, "status": "CANCELED", "executed_qty": 0.1,
            })
        ), patch.object(binance.asyncio, "sleep", AsyncMock()):
            result = await binance._emergency_close_after_stop_failure(
                "BTCUSDT", "BUY", 0.3
            )

        self.assertTrue(result["confirmed_flat"])
        self.assertEqual([p["quantity"] for p in posts], [0.3, 0.2])

    async def test_initial_flat_snapshot_does_not_skip_emergency_close(self):
        post = AsyncMock(return_value={"ok": True, "result": {
            "orderId": "close-visible-late", "status": "FILLED", "executedQty": "0.4",
        }})
        fresh = AsyncMock(side_effect=[(0.0, "fresh"), (0.0, "fresh")])
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", post
        ), patch.object(binance, "_fresh_position_size", fresh):
            result = await binance._emergency_close_after_stop_failure(
                "BTCUSDT", "BUY", 0.4
            )

        self.assertTrue(result["confirmed_flat"])
        post.assert_awaited_once()

    async def test_close_timeout_but_fresh_flat_is_success_without_retry(self):
        post = AsyncMock(return_value={"ok": False, "error": "timeout"})
        fresh = AsyncMock(side_effect=[(1.0, "fresh"), (0.0, "fresh")])
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", post
        ), patch.object(binance, "_fresh_position_size", fresh):
            result = await binance._emergency_close_after_stop_failure("BTCUSDT", "BUY", 1.0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(post.await_count, 1)

    async def test_unknown_position_after_ambiguous_close_blocks_blind_retry(self):
        post = AsyncMock(return_value={"ok": False, "error": "timeout"})
        fresh = AsyncMock(side_effect=[(1.0, "fresh"), (None, "stale")])
        order_check = AsyncMock(return_value={"ok": False, "error": "query timeout"})
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", post
        ), patch.object(binance, "_fresh_position_size", fresh), patch.object(
            binance, "get_order", order_check
        ), patch.object(binance, "_EMERGENCY_CLOSE_ATTEMPTS", 3):
            result = await binance._emergency_close_after_stop_failure(
                "BTCUSDT", "BUY", 1.0, client_order_id_prefix="cw-safe"
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_blocked"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(post.await_count, 1)
        order_check.assert_awaited_once()

    async def test_stale_empty_position_never_confirms_flat(self):
        with patch.object(binance, "get_positions", AsyncMock(return_value={
            "ok": True, "positions": [], "stale": True, "rate_limited": True,
        })):
            size, reason = await binance._fresh_position_size("BTCUSDT")

        self.assertIsNone(size)
        self.assertIn("stale", reason)

    async def test_unconfirmed_close_requires_manual_intervention(self):
        post = AsyncMock(return_value={"ok": False, "error": "network down"})
        fresh = AsyncMock(return_value=(1.0, "fresh"))
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", post
        ), patch.object(binance, "_fresh_position_size", fresh), patch.object(
            binance.asyncio, "sleep", AsyncMock()
        ), patch.object(binance, "_fire_telegram"):
            closed = await binance._emergency_close_after_stop_failure("BTCUSDT", "BUY", 1.0)
            state = await binance._enforce_entry_safety(
                "BTCUSDT", "BUY", 1.0, 90.0,
                {"sl_ok": False, "sl_order_id": None},
            )

        self.assertFalse(closed["ok"])
        self.assertEqual(state["safety_state"], "MANUAL_INTERVENTION_REQUIRED")
        self.assertTrue(state["manual_intervention_required"])

    async def test_maker_partial_fill_rolls_back_only_filled_quantity(self):
        failed_protection = {
            "sl_ok": False, "sl_order_id": None, "sl_msg": "rejected",
            "tp1_ok": False, "tp1_order_id": None, "tp1_msg": "not sent", "tp1_qty": 0,
            "tp2_ok": False, "tp2_order_id": None, "tp2_msg": "not sent",
            "tp1_skipped": False,
        }
        close = AsyncMock(return_value={
            "ok": True, "confirmed_flat": True, "attempts": 1,
            "requested_qty": 0.4, "closed_qty": 0.4, "remaining_qty": 0,
        })
        entry = {"ok": True, "result": {"orderId": "maker-1", "status": "NEW"}}
        partial = {
            "ok": True, "status": "CANCELED", "executed_qty": 0.4,
            "avg_fill_price": 100.0,
        }
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)
        ), patch.object(binance, "_signed_request", AsyncMock(return_value=entry)), patch.object(
            binance, "cancel_order", AsyncMock(return_value={"ok": True})
        ), patch.object(binance, "get_order", AsyncMock(return_value=partial)), patch.object(
            binance, "place_protection_orders", AsyncMock(return_value=failed_protection)
        ), patch.object(binance, "_emergency_close_after_stop_failure", close), patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value={"ok": True, "orders": []})
        ), patch.object(
            binance, "_fire_telegram"
        ):
            result = await binance.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0, stop_loss=90.0,
                poll_timeout_s=0, fallback_market=False,
                client_order_id="cw-maker-partial",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(result["entry_state"], "PARTIALLY_FILLED")
        self.assertEqual(close.await_args.args[2], 0.4)

    async def test_maker_cancel_unknown_never_flattens_or_falls_back_to_market(self):
        entry = {"ok": True, "result": {"orderId": "maker-live", "status": "NEW"}}
        active_partial = {
            "ok": True, "status": "PARTIALLY_FILLED", "executed_qty": 0.4,
            "avg_fill_price": 100.0,
        }
        stop_only = {
            "sl_ok": True, "sl_order_id": "sl-live", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0,
            "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
            "tp1_skipped": False,
        }
        emergency = AsyncMock()
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)
        ), patch.object(binance, "_signed_request", AsyncMock(return_value=entry)) as post, patch.object(
            binance, "cancel_order", AsyncMock(return_value={"ok": False, "error": "timeout"})
        ) as cancel, patch.object(
            binance, "get_order", AsyncMock(return_value=active_partial)
        ), patch.object(
            binance, "place_protection_orders", AsyncMock(return_value=stop_only)
        ), patch.object(
            binance, "_emergency_close_after_stop_failure", emergency
        ), patch.object(binance.asyncio, "sleep", AsyncMock()), patch.object(
            binance, "_fire_telegram"
        ):
            result = await binance.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0, stop_loss=90.0,
                poll_timeout_s=0, fallback_market=True,
                client_order_id="cw-maker-live",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["safety_state"], "ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN")
        self.assertTrue(result["manual_intervention_required"])
        self.assertTrue(result["pending_entry_order"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(cancel.await_count, binance._MAKER_CANCEL_CONFIRM_ATTEMPTS)
        self.assertEqual(post.await_count, 1)
        emergency.assert_not_awaited()

    async def test_market_non_terminal_result_enters_quarantine(self):
        entry = {"ok": True, "result": {
            "orderId": "market-new", "status": "NEW", "executedQty": "0.2",
        }, "raw": {}}
        stop_only = {
            "sl_ok": True, "sl_order_id": "sl-unknown", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0,
            "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
            "tp1_skipped": False,
        }
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "get_order", AsyncMock(return_value={
            "ok": False, "error": "timeout",
        })), patch.object(
            binance, "place_protection_orders", AsyncMock(return_value=stop_only)
        ), patch.object(binance, "_fire_telegram"):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                client_order_id="cw-market-unknown",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["safety_state"], "ENTRY_CONFIRMATION_UNKNOWN")
        self.assertEqual(result["executed_qty"], 0.2)
        self.assertTrue(result["manual_intervention_required"])
        self.assertEqual(result["protection_state"], "STOP_CONFIRMED")

    async def test_filled_without_executed_qty_is_unknown_not_no_fill(self):
        entry = {"ok": True, "result": {
            "orderId": "filled-no-qty", "status": "FILLED",
        }, "raw": {}}
        stop_only = {
            "sl_ok": True, "sl_order_id": "sl-max-cover", "sl_msg": None,
            "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None, "tp1_qty": 0.0,
            "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
            "tp1_skipped": False, "is_runner": False, "runner_qty": 0.0,
        }
        protect = AsyncMock(return_value=stop_only)
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "get_order", AsyncMock(return_value={
            "ok": False, "error": "query timeout",
        })), patch.object(binance, "place_protection_orders", protect), patch.object(
            binance, "_fire_telegram"
        ):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                client_order_id="cw-filled-no-qty",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result.get("no_fill", False))
        self.assertEqual(result["safety_state"], "ENTRY_CONFIRMATION_UNKNOWN")
        self.assertEqual(result["protection_state"], "STOP_CONFIRMED")
        self.assertTrue(result["manual_intervention_required"])
        self.assertEqual(protect.await_args.args[2], 1.0)

    async def test_cleanup_uses_exact_client_ids_not_prefix_collision(self):
        live = {"ok": True, "orders": [{
            "algo_id": "other-sl", "client_algo_id": "cw-10-sl",
        }]}
        cancel = AsyncMock(return_value={"ok": True})
        with patch.object(binance, "get_open_algo_orders", AsyncMock(return_value=live)), patch.object(
            binance, "cancel_algo_order", cancel
        ):
            result = await binance._cleanup_entry_conditionals(
                "BTCUSDT", "cw-1", {"sl_order_id": None}
            )

        self.assertEqual(result["state"], "CONFIRMED")
        cancel.assert_not_awaited()

    async def test_cleanup_catches_conditional_that_appears_after_first_empty_scan(self):
        live = AsyncMock(side_effect=[
            {"ok": True, "orders": []},
            {"ok": True, "orders": [{
                "algo_id": "late-sl", "client_algo_id": "cw-late-sl",
            }]},
            {"ok": True, "orders": []},
        ])
        cancel = AsyncMock(return_value={"ok": True})
        with patch.object(binance, "get_open_algo_orders", live), patch.object(
            binance, "cancel_algo_order", cancel
        ), patch.object(binance.asyncio, "sleep", AsyncMock()):
            result = await binance._cleanup_entry_conditionals(
                "BTCUSDT", "cw-late", {"sl_order_id": None}
            )

        self.assertEqual(result["state"], "CONFIRMED")
        cancel.assert_awaited_once_with("late-sl")

    async def test_reduce_only_partial_fill_is_not_reported_as_closed(self):
        entry = {"ok": True, "result": {
            "orderId": "close-partial", "status": "EXPIRED", "executedQty": "0.4",
        }, "raw": {}}
        with patch.object(binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), patch.object(
            binance, "_signed_request", AsyncMock(return_value=entry)
        ), patch.object(binance, "_fresh_position_size", AsyncMock(return_value=(0.6, "fresh"))):
            result = await binance.place_order(
                "BTCUSDT", "Sell", 1.0, reduce_only=True,
                client_order_id="cw-close-partial",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["safety_state"], "CLOSE_PARTIAL_OR_UNKNOWN")
        self.assertAlmostEqual(result["remaining_qty"], 0.6)

    async def test_duplicate_client_order_id_is_an_ambiguous_submission(self):
        self.assertTrue(binance._is_ambiguous_order_submission({
            "ok": False, "code": -4116, "msg": "clientOrderId is duplicated",
        }))

    async def test_flat_position_with_uncertain_cleanup_stays_quarantined(self):
        close = AsyncMock(return_value={
            "ok": True, "confirmed_flat": True, "attempts": 1,
            "requested_qty": 1.0, "closed_qty": 1.0, "remaining_qty": 0,
        })
        with patch.object(binance, "_emergency_close_after_stop_failure", close), patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value={
                "ok": False, "error": "rate limited",
            })
        ), patch.object(binance, "_fire_telegram"):
            state = await binance._enforce_entry_safety(
                "BTCUSDT", "BUY", 1.0, 90.0,
                {"sl_ok": False, "sl_order_id": None},
                client_order_id_prefix="cw-cleanup",
            )

        self.assertEqual(state["safety_state"], "POSITION_FLAT_CONDITIONALS_UNKNOWN")
        self.assertFalse(state["position_open"])
        self.assertTrue(state["emergency_close_ok"])
        self.assertTrue(state["manual_intervention_required"])


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
                indicators=SimpleNamespace(ema12=e1, ema26=e2, ema50=e3),
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
