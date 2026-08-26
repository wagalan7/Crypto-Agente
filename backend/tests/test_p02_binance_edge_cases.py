"""P02: casos-limite do fail-safe de execução Binance."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as binance  # noqa: E402


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


def _confirmed_stop_only(stop_id: str = "sl-safe") -> dict:
    return {
        "sl_ok": True,
        "sl_order_id": stop_id,
        "sl_msg": None,
        "tp1_ok": True,
        "tp1_order_id": None,
        "tp1_msg": None,
        "tp1_qty": 0.0,
        "tp2_ok": True,
        "tp2_order_id": None,
        "tp2_msg": None,
        "tp1_skipped": False,
        "tp1_requested": False,
        "tp2_requested": False,
        "is_runner": False,
        "runner_qty": 0.0,
        "sl_submission_unknown": False,
    }


class SubmissionAmbiguityEdgeTests(unittest.TestCase):
    def test_local_cooldown_is_not_an_ambiguous_submission(self):
        self.assertFalse(binance._is_ambiguous_order_submission({
            "ok": False,
            "code": -1003,
            "msg": "Too many requests",
            "_cooldown": True,
        }))

    def test_timeout_after_request_was_sent_is_ambiguous(self):
        self.assertTrue(binance._is_ambiguous_order_submission({
            "ok": False,
            "error": "timeout",
            "_request_sent": True,
        }))


class MarketSubmissionAmbiguityTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_timeout_reconciled_as_filled_is_protected(self):
        protect = AsyncMock(return_value=_confirmed_stop_only())
        query = {
            "ok": True,
            "status": "FILLED",
            "executed_qty": 0.4,
            "avg_fill_price": 100.0,
            "order_id": "entry-1",
            "raw": {"status": "FILLED", "executedQty": "0.4"},
        }
        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_signed_request", AsyncMock(return_value={
                "ok": False, "error": "timeout", "_request_sent": True,
            })
        ), patch.object(binance, "get_order", AsyncMock(return_value=query)), patch.object(
            binance, "_place_post_fill_protection", protect
        ):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                client_order_id="cw-timeout-filled",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(protect.await_args.args[2], 0.4)

    async def test_market_timeout_unknown_gets_only_planned_stop_and_quarantine(self):
        protect = AsyncMock(return_value=_confirmed_stop_only())
        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_signed_request", AsyncMock(return_value={
                "ok": False, "error": "timeout", "_request_sent": True,
            })
        ), patch.object(
            binance, "get_order", AsyncMock(return_value={"ok": False, "error": "down"})
        ), patch.object(
            binance, "_place_post_fill_protection", protect
        ), patch.object(binance, "_fire_telegram"):
            result = await binance.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=90.0,
                client_order_id="cw-timeout-unknown",
                entry_preflight=_p04b_market_preflight,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["safety_state"], "ENTRY_SUBMISSION_UNKNOWN")
        self.assertTrue(result["quarantine_required"])
        self.assertTrue(result["sl_ok"])
        self.assertEqual(protect.await_args.args[2], 1.0)


class ProtectionDedupEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def _place_with_existing_tp1(self, existing_tp1: dict) -> tuple[dict, AsyncMock]:
        existing = {
            "ok": True,
            "orders": [
                existing_tp1,
                {
                    "type": "TAKE_PROFIT_MARKET",
                    "side": "SELL",
                    "trigger_price": 120.0,
                    "algo_id": "existing-tp2",
                    "close_position": True,
                },
            ],
        }
        post = AsyncMock(return_value={
            "ok": True,
            "result": {"algoId": "new-tp1"},
        })
        with patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value=existing)
        ), patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _symbol, price: price)
        ), patch.object(
            binance, "_signed_request", post
        ), patch.object(binance, "RUNNER_ENABLED", False):
            result = await binance.place_protection_orders(
                "BTCUSDT",
                "BUY",
                1.0,
                tp1=110.0,
                tp2=120.0,
                dedup_live=True,
            )
        return result, post

    async def test_tp_without_reduce_only_is_not_deduplicated(self):
        result, post = await self._place_with_existing_tp1({
            "type": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "trigger_price": 110.0,
            "algo_id": "unsafe-tp1",
            "reduce_only": False,
            "quantity": 0.45,
        })

        self.assertEqual(result["tp1_order_id"], "new-tp1")
        post.assert_awaited_once()
        self.assertEqual(post.await_args.args[2]["reduceOnly"], "true")

    async def test_reduce_only_tp_with_wrong_quantity_is_not_deduplicated(self):
        result, post = await self._place_with_existing_tp1({
            "type": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "trigger_price": 110.0,
            "algo_id": "undersized-tp1",
            "reduce_only": True,
            "quantity": 0.2,
        })

        self.assertEqual(result["tp1_order_id"], "new-tp1")
        post.assert_awaited_once()
        self.assertAlmostEqual(post.await_args.args[2]["quantity"], 0.45)

    async def test_requested_tp_without_algo_id_is_not_confirmed(self):
        post = AsyncMock(return_value={"ok": True, "result": {}})
        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _symbol, price: price)
        ), patch.object(binance, "_signed_request", post), patch.object(
            binance, "_ALGO_MAX_ATTEMPTS", 1
        ):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 1.0, tp2=120.0
            )

        self.assertTrue(result["tp2_requested"])
        self.assertFalse(result["tp2_ok"])
        self.assertIsNone(result["tp2_order_id"])


class ConditionalCleanupEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_stop_submission_stays_pending_after_three_empty_scans(self):
        live = AsyncMock(return_value={"ok": True, "orders": []})
        cancel = AsyncMock()
        with patch.object(binance, "get_open_algo_orders", live), patch.object(
            binance, "cancel_algo_order", cancel
        ), patch.object(binance.asyncio, "sleep", AsyncMock()):
            result = await binance._cleanup_entry_conditionals(
                "BTCUSDT",
                "cw-ambiguous",
                {
                    "sl_order_id": None,
                    "tp1_order_id": None,
                    "tp2_order_id": None,
                    "sl_submission_unknown": True,
                },
            )

        self.assertEqual(result["state"], "PENDING")
        self.assertFalse(result["confirmed_absent"])
        self.assertEqual(live.await_count, 3)
        cancel.assert_not_awaited()

    async def test_known_algo_id_is_reconciled_even_with_a_different_client_prefix(self):
        live = AsyncMock(side_effect=[
            {
                "ok": True,
                "orders": [{
                    "algo_id": "heal-1",
                    "client_algo_id": "cw-heal-sl",
                }],
            },
            {"ok": True, "orders": []},
        ])
        cancel = AsyncMock(side_effect=[
            {"ok": False, "error": "timeout"},
            {"ok": True, "result": {"algoId": "heal-1"}},
        ])
        with patch.object(binance, "get_open_algo_orders", live), patch.object(
            binance, "cancel_algo_order", cancel
        ), patch.object(binance.asyncio, "sleep", AsyncMock()):
            result = await binance._cleanup_entry_conditionals(
                "BTCUSDT",
                "cw-current",
                {
                    "sl_order_id": "heal-1",
                    "tp1_order_id": None,
                    "tp2_order_id": None,
                    "sl_submission_unknown": False,
                },
            )

        self.assertEqual(result["state"], "CONFIRMED")
        self.assertTrue(result["confirmed_absent"])
        self.assertEqual(cancel.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in cancel.await_args_list],
            ["heal-1", "heal-1"],
        )


class EmergencyCloseEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_canceled_close_without_executed_qty_blocks_retry(self):
        close = AsyncMock(return_value={
            "ok": True,
            "result": {"orderId": "close-1", "status": "CANCELED"},
        })
        fresh = AsyncMock(side_effect=[(1.0, "fresh"), (1.0, "fresh")])
        order_check = AsyncMock(return_value={
            "ok": True,
            "status": "CANCELED",
            "raw": {"orderId": "close-1", "status": "CANCELED"},
        })
        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(binance, "_signed_request", close), patch.object(
            binance, "_fresh_position_size", fresh
        ), patch.object(binance, "get_order", order_check), patch.object(
            binance, "_EMERGENCY_CLOSE_ATTEMPTS", 3
        ):
            result = await binance._emergency_close_after_stop_failure(
                "BTCUSDT", "BUY", 1.0, client_order_id_prefix="cw-edge"
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retry_blocked"])
        self.assertEqual(result["attempts"], 1)
        close.assert_awaited_once()
        order_check.assert_awaited_once()


class MakerFillMonotonicityEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def _run_partial_fill_case(
        self,
        *,
        order_check: dict,
        cancel_result: dict,
    ) -> tuple[dict, AsyncMock, AsyncMock]:
        entry = {
            "ok": True,
            "result": {
                "orderId": "maker-edge",
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.4",
                "avgPrice": "100",
            },
        }
        post = AsyncMock(return_value=entry)
        protect = AsyncMock(return_value=_confirmed_stop_only())
        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _symbol, price: price)
        ), patch.object(binance, "_signed_request", post), patch.object(
            binance, "cancel_order", AsyncMock(return_value=cancel_result)
        ), patch.object(
            binance, "get_order", AsyncMock(return_value=order_check)
        ), patch.object(
            binance, "_place_post_fill_protection", protect
        ), patch.object(binance, "_fire_telegram"):
            result = await binance.place_maker_entry_then_protect(
                "BTCUSDT",
                "Buy",
                1.0,
                limit_price=100.0,
                stop_loss=90.0,
                poll_timeout_s=0,
                fallback_market=True,
                client_order_id="cw-maker-edge",
            )
        return result, post, protect

    async def test_sparse_terminal_cancel_keeps_final_fill_quantity_unknown(self):
        result, post, protect = await self._run_partial_fill_case(
            order_check={"ok": False, "error": "query unavailable"},
            cancel_result={
                "ok": True,
                "result": {"orderId": "maker-edge", "status": "CANCELED"},
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(result["safety_state"], "FINAL_FILL_QTY_UNKNOWN")
        self.assertTrue(result["final_fill_qty_unknown"])
        self.assertTrue(result["entry_order_terminal"])
        self.assertFalse(result["pending_entry_order"])
        self.assertFalse(result["fell_back_to_market"])
        # A qty .4 é apenas lower-bound; o SL closePosition usa o teto planejado
        # e nenhum TP/rollback exato é criado.
        self.assertEqual(protect.await_args.args[2], 1.0)
        post.assert_awaited_once()

    async def test_regressive_terminal_quantity_is_not_treated_as_final(self):
        result, post, protect = await self._run_partial_fill_case(
            order_check={
                "ok": True,
                "status": "CANCELED",
                "executed_qty": 0.0,
                "avg_fill_price": 0.0,
                "raw": {
                    "orderId": "maker-edge",
                    "status": "CANCELED",
                    "executedQty": "0",
                },
            },
            cancel_result={
                "ok": True,
                "result": {"orderId": "maker-edge", "status": "CANCELED"},
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["executed_qty"], 0.4)
        self.assertEqual(result["safety_state"], "FINAL_FILL_QTY_UNKNOWN")
        self.assertTrue(result["executed_qty_is_lower_bound"])
        self.assertFalse(result["fell_back_to_market"])
        self.assertEqual(protect.await_args.args[2], 1.0)
        post.assert_awaited_once()


class ProtectionProgressEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_tp1_survives_tp2_timeout(self):
        async def signed_request(_method, _path, params=None):
            trigger = float((params or {}).get("triggerPrice") or 0.0)
            if trigger == 90.0:
                return {"ok": True, "result": {"algoId": "sl-progress"}}
            if trigger == 110.0:
                return {"ok": True, "result": {"algoId": "tp1-progress"}}
            if trigger == 120.0:
                await asyncio.sleep(60)
            raise AssertionError(f"gatilho inesperado: {trigger}")

        with patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _symbol, qty: qty)
        ), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _symbol, price: price)
        ), patch.object(
            binance, "_signed_request", side_effect=signed_request
        ), patch.object(binance, "RUNNER_ENABLED", False), patch.object(
            binance, "_ALGO_MAX_ATTEMPTS", 1
        ), patch.object(binance, "_POST_FILL_PROTECTION_TIMEOUT_S", 0.01):
            result = await binance._place_post_fill_protection(
                "BTCUSDT",
                "BUY",
                1.0,
                stop_loss=90.0,
                tp1=110.0,
                tp2=120.0,
                client_order_id_prefix="cw-progress",
            )

        self.assertEqual(result["sl_order_id"], "sl-progress")
        self.assertTrue(result["tp1_ok"])
        self.assertEqual(result["tp1_order_id"], "tp1-progress")
        self.assertAlmostEqual(result["tp1_qty"], 0.45)
        self.assertFalse(result["tp2_ok"])
        self.assertIsNone(result["tp2_order_id"])
        self.assertTrue(result["protection_timeout"])


class MutationGuardEdgeTests(unittest.IsolatedAsyncioTestCase):
    """P03.1E / invariante #6: cada POST interno de proteção exige um lease válido.
    `place_protection_orders` aceita `mutation_guard` e o invoca IMEDIATAMENTE antes
    de cada POST (tentativa/retry/fallback). Guard nega (ou levanta) ⇒ NENHUM POST."""

    async def _run(self, guard):
        post = AsyncMock(return_value={"ok": True, "result": {"algoId": "sl-guarded"}})
        with patch.object(
            binance, "get_open_algo_orders", AsyncMock(return_value={"ok": True, "orders": []})
        ), patch.object(
            binance, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)
        ), patch.object(
            binance, "_round_price", AsyncMock(side_effect=lambda _s, p: p)
        ), patch.object(binance, "_signed_request", post):
            result = await binance.place_protection_orders(
                "BTCUSDT", "BUY", 1.0, stop_loss=90.0, mutation_guard=guard,
            )
        return result, post

    async def test_guard_allows_lets_the_post_through_after_checking(self):
        calls = {"n": 0}

        async def guard():
            calls["n"] += 1
            return True

        result, post = await self._run(guard)
        self.assertTrue(result["sl_ok"])
        self.assertEqual(result["sl_order_id"], "sl-guarded")
        post.assert_awaited()                       # POST ocorreu
        self.assertGreaterEqual(calls["n"], post.await_count)   # guard precedeu cada POST

    async def test_guard_denies_blocks_every_post_and_fallback(self):
        async def guard():
            return False

        result, post = await self._run(guard)
        self.assertFalse(result["sl_ok"])
        self.assertIsNone(result["sl_order_id"])
        post.assert_not_awaited()                   # NENHUM POST (nem principal, nem fallback)

    async def test_guard_exception_is_fail_closed_no_post(self):
        async def guard():
            raise RuntimeError("lease store indisponível")

        result, post = await self._run(guard)
        self.assertFalse(result["sl_ok"])
        self.assertIsNone(result["sl_order_id"])
        post.assert_not_awaited()                   # exceção do guard ⇒ fail-closed

    async def test_no_guard_preserves_legacy_behavior(self):
        result, post = await self._run(None)        # sem guard: comportamento inalterado
        self.assertTrue(result["sl_ok"])
        self.assertEqual(result["sl_order_id"], "sl-guarded")
        post.assert_awaited()


if __name__ == "__main__":
    unittest.main()
