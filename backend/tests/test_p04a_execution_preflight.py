"""P04A — revalidação fail-closed imediatamente antes da entrada maker.

Testes herméticos: nenhuma rede, banco ou exchange real.
"""
from __future__ import annotations

import copy
import inspect
import math
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.entry_revalidation_service import (  # noqa: E402
    cap_qty_for_revalidated_entry,
    evaluate_entry_revalidation,
    normalize_entry_side,
    select_entry_route,
)
from services import binance_signed_service as bss  # noqa: E402
from services import shadow_trade_service as sts  # noqa: E402
from services.execution_reconciliation_service import assemble_entry_incident  # noqa: E402


NOW_MS = 2_000_000.0
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_NET_ATTEMPTS = []


def _blocked_network(*args, **kwargs):
    _NET_ATTEMPTS.append(args[:1])
    raise RuntimeError(f"rede bloqueada no teste P04A: {args[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    socket.getaddrinfo = _blocked_network
    socket.create_connection = _blocked_network


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO
    socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"P04A tentou acessar rede: {_NET_ATTEMPTS}")


def _quote(**changes):
    out = {
        "ok": True,
        "exchange": "binance",
        "source": "binance_book_ticker",
        "symbol": "BTCUSDT",
        "bid": 100.0,
        "ask": 100.1,
        "bid_qty": 10.0,
        "ask_qty": 12.0,
        "received_at_ms": NOW_MS - 100.0,
        "exchange_time_ms": NOW_MS - 100.0,
        "latency_ms": 50.0,
    }
    out.update(changes)
    return out


def _verdict(quote=None, **changes):
    params = {
        "quote": quote or _quote(),
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "planned_entry": 100.0,
        "stop_loss": 98.0,
        "tp1": 102.0,
        "tp2": 104.0,
        "atr": 2.0,
        "max_quote_age_ms": 1_000.0,
        "max_fetch_latency_ms": 1_000.0,
        "max_spread_pct": 0.25,
        "max_chase_atr": 1.0,
        "min_rr_tp1": 0.7,
        "min_rr_tp2": 1.5,
        "maker_limit_price": 100.0,
        "now_ms": NOW_MS,
    }
    params.update(changes)
    return evaluate_entry_revalidation(**params)


class EntryVerdictTests(unittest.TestCase):
    def test_fresh_valid_passes_long_and_short(self):
        long_v = _verdict()
        self.assertTrue(long_v["ok"])
        self.assertEqual(long_v["quality"], "FRESH")
        self.assertEqual(long_v["checks"]["executable_price"], 100.1)

        short_v = _verdict(
            quote=_quote(bid=99.9, ask=100.0),
            side="short", planned_entry=100.0, maker_limit_price=100.0,
            stop_loss=102.0, tp1=98.0, tp2=96.0,
        )
        self.assertTrue(short_v["ok"])
        self.assertEqual(short_v["checks"]["executable_price"], 99.9)

    def test_favorable_price_is_not_chase(self):
        long_v = _verdict(
            quote=_quote(bid=98.9, ask=99.0), maker_limit_price=98.8,
        )
        self.assertTrue(long_v["ok"])
        self.assertLess(long_v["checks"]["chase_atr"], 0)

        short_v = _verdict(
            quote=_quote(bid=101.0, ask=101.1), side="short",
            maker_limit_price=101.2, stop_loss=103.0, tp1=98.0, tp2=96.0,
        )
        self.assertTrue(short_v["ok"])
        self.assertLess(short_v["checks"]["chase_atr"], 0)

    def test_price_chase_blocks_symmetrically_at_limit(self):
        long_v = _verdict(
            quote=_quote(bid=101.9, ask=102.0), max_chase_atr=1.0,
        )
        self.assertFalse(long_v["ok"])
        self.assertEqual(long_v["reason_code"], "EXEC_PRICE_CHASE")

        short_v = _verdict(
            quote=_quote(bid=98.0, ask=98.1), side="short",
            maker_limit_price=100.0, stop_loss=102.0, tp1=96.0, tp2=94.0,
            max_chase_atr=1.0,
        )
        self.assertFalse(short_v["ok"])
        self.assertEqual(short_v["reason_code"], "EXEC_PRICE_CHASE")

    def test_wide_spread_and_boundary(self):
        at_limit = _verdict(
            quote=_quote(bid=99.875, ask=100.125),
            max_spread_pct=0.25,
        )
        self.assertTrue(at_limit["ok"])

        too_wide = _verdict(
            quote=_quote(bid=99.87, ask=100.13),
            max_spread_pct=0.25,
        )
        self.assertFalse(too_wide["ok"])
        self.assertEqual(too_wide["reason_code"], "EXEC_SPREAD_TOO_WIDE")

    def test_age_and_rr_exact_boundaries_pass(self):
        verdict = _verdict(
            quote=_quote(
                bid=99.9, ask=100.0, received_at_ms=NOW_MS - 1_000,
                exchange_time_ms=NOW_MS - 1_000,
            ),
            planned_entry=100.0, maker_limit_price=99.9,
            stop_loss=99.0, tp1=101.0, tp2=102.0,
            min_rr_tp1=1.0, min_rr_tp2=2.0,
        )
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["checks"]["age_ms"], 1_000.0)
        self.assertEqual(verdict["checks"]["rr_tp1"], 1.0)
        self.assertEqual(verdict["checks"]["rr_tp2"], 2.0)

    def test_rr_is_recomputed_from_fresh_executable_price(self):
        verdict = _verdict(
            quote=_quote(bid=100.9, ask=101.0),
            max_chase_atr=2.0,
            min_rr_tp1=0.7,
        )
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_RR_TP1_TOO_LOW")
        self.assertAlmostEqual(verdict["checks"]["rr_tp1"], 1.0 / 3.0)

    def test_required_tp_missing_or_non_finite_blocks(self):
        for field, value in (("tp1", None), ("tp1", float("nan")),
                             ("tp2", 0), ("tp2", float("inf"))):
            with self.subTest(field=field, value=value):
                verdict = _verdict(**{field: value})
                self.assertFalse(verdict["ok"])
                self.assertEqual(verdict["reason_code"], "EXEC_LEVELS_INVALID")

    def test_maker_slippage_long_short_and_boundary(self):
        long_at = _verdict(
            quote=_quote(bid=100.4, ask=100.5),
            maker_limit_price=100.1,
            tp1=103.0, tp2=105.0,
            max_chase_atr=1.0,
            max_adverse_slippage_pct=0.1,
            enforce_adverse_slippage=True,
        )
        self.assertTrue(long_at["ok"])
        self.assertAlmostEqual(long_at["checks"]["adverse_slippage_pct"], 0.1)
        long_over = _verdict(
            quote=_quote(bid=100.4, ask=100.5),
            maker_limit_price=100.11,
            tp1=103.0, tp2=105.0,
            max_chase_atr=1.0,
            max_adverse_slippage_pct=0.1,
            enforce_adverse_slippage=True,
        )
        self.assertEqual(long_over["reason_code"], "EXEC_SLIPPAGE_TOO_HIGH")

        short_at = _verdict(
            quote=_quote(bid=99.5, ask=99.6), side="short",
            maker_limit_price=99.9, stop_loss=102.0, tp1=97.0, tp2=95.0,
            max_adverse_slippage_pct=0.1, enforce_adverse_slippage=True,
        )
        self.assertTrue(short_at["ok"])
        short_over = _verdict(
            quote=_quote(bid=99.5, ask=99.6), side="short",
            maker_limit_price=99.89, stop_loss=102.0, tp1=97.0, tp2=95.0,
            max_adverse_slippage_pct=0.1, enforce_adverse_slippage=True,
        )
        self.assertEqual(short_over["reason_code"], "EXEC_SLIPPAGE_TOO_HIGH")

    def test_invalid_unknown_and_stale_fail_closed(self):
        cases = [
            (_quote(ok=False, reason_code="RATE_LIMITED"), "EXEC_QUOTE_UNAVAILABLE"),
            (_quote(ok=False, reason_code="EXEC_QUOTE_RATE_LIMITED"), "EXEC_QUOTE_RATE_LIMITED"),
            (_quote(received_at_ms=None), "EXEC_QUOTE_TIMESTAMP_INVALID"),
            (_quote(exchange_time_ms=None), "EXEC_QUOTE_TIMESTAMP_INVALID"),
            (_quote(received_at_ms=NOW_MS - 1_001), "EXEC_QUOTE_STALE"),
            (_quote(exchange_time_ms=NOW_MS - 1_001), "EXEC_QUOTE_STALE"),
            (_quote(symbol="ETHUSDT"), "EXEC_SYMBOL_MISMATCH"),
            (_quote(bid=float("nan")), "EXEC_QUOTE_INVALID"),
            (_quote(ask=float("inf")), "EXEC_QUOTE_INVALID"),
            (_quote(bid=0), "EXEC_QUOTE_INVALID"),
            (_quote(bid_qty=0), "EXEC_BOOK_LIQUIDITY_INVALID"),
            (_quote(bid=101, ask=100), "EXEC_BOOK_CROSSED"),
            (_quote(latency_ms=1_001), "EXEC_QUOTE_SLOW"),
        ]
        for quote, code in cases:
            with self.subTest(code=code):
                verdict = _verdict(quote=quote)
                self.assertFalse(verdict["ok"])
                self.assertEqual(verdict["quality"], "UNKNOWN")
                self.assertEqual(verdict["reason_code"], code)

    def test_maker_crossing_and_invalid_levels_block(self):
        crossing = _verdict(maker_limit_price=100.1)
        self.assertFalse(crossing["ok"])
        self.assertEqual(crossing["reason_code"], "EXEC_MAKER_WOULD_TAKE")

        bad_stop = _verdict(stop_loss=101.0)
        self.assertFalse(bad_stop["ok"])
        self.assertEqual(bad_stop["reason_code"], "EXEC_LEVELS_INVALID")

    def test_final_limit_geometry_and_target_order_are_symmetric(self):
        cases = [
            {"side": "long", "maker_limit_price": 97.9},
            {
                "side": "short", "quote": _quote(bid=99.9, ask=100.0),
                "maker_limit_price": 102.1, "stop_loss": 102.0,
                "tp1": 98.0, "tp2": 96.0,
            },
            {"side": "long", "tp1": 104.0, "tp2": 102.0},
            {
                "side": "short", "quote": _quote(bid=99.9, ask=100.0),
                "maker_limit_price": 100.0, "stop_loss": 102.0,
                "tp1": 96.0, "tp2": 98.0,
            },
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                verdict = _verdict(**changes)
                self.assertFalse(verdict["ok"])
                self.assertEqual(verdict["reason_code"], "EXEC_LEVELS_INVALID")

    def test_is_pure_and_qty_cap_never_increases(self):
        quote = _quote()
        before = copy.deepcopy(quote)
        one = _verdict(quote=quote)
        two = _verdict(quote=quote)
        self.assertEqual(one, two)
        self.assertEqual(quote, before)

        self.assertEqual(
            cap_qty_for_revalidated_entry(2.0, 100.0, 98.0, 100.0), 2.0
        )
        reduced = cap_qty_for_revalidated_entry(2.0, 100.0, 98.0, 101.0)
        self.assertGreater(reduced, 0)
        self.assertLessEqual(reduced, 2.0)
        for invalid in (0, -1, math.nan, math.inf):
            self.assertEqual(
                cap_qty_for_revalidated_entry(2.0, 100.0, 98.0, invalid), 0.0
            )


class _Response:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class ExecutionQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_book_ticker_uses_active_binance_base_without_cache(self):
        client = type("Client", (), {})()
        client.get = AsyncMock(return_value=_Response({
            "symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.1",
            "bidQty": "10", "askQty": "12", "time": 1_999_950,
        }, headers={"x-mbx-used-weight-1m": "7"}))

        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=True)), \
                patch.object(bss, "record_external_weight") as weight, \
                patch.object(bss.time, "time", return_value=2_000.0):
            result = await bss.get_execution_quote("BTC/USDT:USDT", timeout_s=1.0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["exchange"], "binance")
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["bid"], 100.0)
        self.assertEqual(result["ask"], 100.1)
        url = client.get.await_args.args[0]
        self.assertEqual(url, f"{bss.BASE}/fapi/v1/ticker/bookTicker")
        self.assertEqual(client.get.await_args.kwargs["params"], {"symbol": "BTCUSDT"})
        weight.assert_not_called()

    async def test_rate_limit_and_invalid_payload_fail_closed(self):
        client = type("Client", (), {})()
        client.get = AsyncMock()
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=False)):
            blocked = await bss.get_execution_quote("BTCUSDT")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason_code"], "EXEC_QUOTE_RATE_LIMITED")
        client.get.assert_not_called()

        client.get = AsyncMock(return_value=_Response({
            "symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.1",
            "bidQty": "10", "askQty": "12", "time": "invalid",
        }))
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=True)):
            malformed = await bss.get_execution_quote("BTCUSDT")
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["reason_code"], "EXEC_QUOTE_INVALID_TIMESTAMP")


class MakerPreflightWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_guard_runs_immediately_before_post_and_sends_nothing(self):
        guard = AsyncMock(return_value={
            "ok": False, "quality": "UNKNOWN",
            "reason_code": "EXEC_QUOTE_STALE", "reason": "cotação vencida",
        })
        client = type("Client", (), {})()
        client.request = AsyncMock()
        market = AsyncMock(return_value={"ok": True})
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "set_leverage", AsyncMock(return_value={"ok": True})), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "place_order", market):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                stop_loss=98.0, tp1=102.0, take_profit=104.0,
                leverage=2, client_order_id="cw-p04a", fallback_market=False,
                entry_preflight=guard,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["entry_not_submitted"])
        self.assertEqual(result["reason_code"], "EXEC_QUOTE_STALE")
        guard.assert_awaited_once_with(100.0, 1.0)
        client.request.assert_not_awaited()
        market.assert_not_awaited()

    async def test_guard_exception_is_fail_closed(self):
        guard = AsyncMock(side_effect=RuntimeError("boom"))
        client = type("Client", (), {})()
        client.request = AsyncMock()
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "set_leverage", AsyncMock(return_value={"ok": True})), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "EXEC_PREFLIGHT_ERROR")
        client.request.assert_not_awaited()

    async def test_qty_change_after_final_quote_blocks_before_post(self):
        guard = AsyncMock(return_value={
            "ok": True, "quality": "FRESH", "reason_code": "EXEC_REVALIDATION_OK",
            "approved_qty": 0.8, "checks": {},
        })
        client = type("Client", (), {})()
        client.request = AsyncMock()
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "set_leverage", AsyncMock(return_value={"ok": True})), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "place_order", AsyncMock()) as market:
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "EXEC_QTY_REVALIDATION_FAILED")
        client.request.assert_not_awaited()
        market.assert_not_awaited()

    async def test_successful_guard_posts_next_and_reports_submitted_qty(self):
        events = []

        async def guard(price, qty):
            events.append("guard")
            return {
                "ok": True, "quality": "FRESH",
                "reason_code": "EXEC_REVALIDATION_OK",
                "approved_qty": qty, "checks": {},
            }

        async def post(*args):
            events.append("post")
            return _Response({"code": -1007, "msg": "timeout"}, status_code=400)

        client = type("Client", (), {})()
        client.request = AsyncMock(side_effect=post)

        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "set_leverage", AsyncMock(return_value={"ok": True})), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "_fire_telegram"):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
                client_order_id="cw-submitted",
            )
        self.assertEqual(events, ["guard", "post"])
        self.assertEqual(result["safety_state"], "ENTRY_SUBMISSION_UNKNOWN")
        self.assertEqual(result["submitted_qty"], 1.0)

    async def test_throttle_then_new_quarantine_blocks_before_transport(self):
        events = []
        client = type("Client", (), {})()
        client.request = AsyncMock()
        risk = type("Risk", (), {})()
        kill = type("Kill", (), {})()
        risk.is_paused = AsyncMock(return_value=False)
        kill.check_can_trade = AsyncMock(return_value={"allowed": True})

        async def wait_then_quarantine(_delay):
            events.append("throttle")
            sts._EXECUTION_QUARANTINE_REASON = "P03 incidente durante throttle"

        async def guard(_price, _qty):
            events.append("guard")
            return await sts._p04a_runtime_recheck(risk, kill)

        throttle_until = bss.time.time() * 1000.0 + 1_000.0
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", throttle_until), \
                patch.object(bss.asyncio, "sleep", AsyncMock(side_effect=wait_then_quarantine)), \
                patch.object(sts, "_EXECUTION_QUARANTINE_REASON", None):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
            )

        self.assertEqual(events, ["throttle", "guard"])
        self.assertEqual(result["reason_code"], "EXEC_QUARANTINE_ACTIVE")
        client.request.assert_not_awaited()

    async def test_success_after_throttle_orders_sleep_guard_transport(self):
        events = []
        clock = {"now": 1_000.0}

        async def wait(_delay):
            events.append("throttle")
            clock["now"] = 1_002.0

        async def guard(_price, qty):
            events.append("guard")
            return {"ok": True, "quality": "FRESH", "approved_qty": qty}

        async def post(*_args):
            events.append("post")
            return _Response({"code": -1007, "msg": "timeout"}, status_code=400)

        client = type("Client", (), {})()
        client.request = AsyncMock(side_effect=post)
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 1_001_000.0), \
                patch.object(bss.asyncio, "sleep", AsyncMock(side_effect=wait)), \
                patch.object(bss.time, "time", side_effect=lambda: clock["now"]), \
                patch.object(bss, "_fire_telegram"):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
                client_order_id="cw-throttle-order",
            )

        self.assertEqual(events, ["throttle", "guard", "post"])
        self.assertEqual(result["safety_state"], "ENTRY_SUBMISSION_UNKNOWN")

    async def test_rate_gate_armed_by_preflight_blocks_same_cycle(self):
        client = type("Client", (), {})()
        client.request = AsyncMock()

        async def guard(_price, _qty):
            bss._throttle_until_ms = bss.time.time() * 1000.0 + 1_000.0
            return {"ok": True, "quality": "FRESH", "approved_qty": 1.0}

        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            result = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=False, entry_preflight=guard,
            )

        self.assertEqual(result["reason_code"], "EXEC_RATE_GATE_CHANGED")
        client.request.assert_not_awaited()


class RuntimeRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_or_new_quarantine_blocks_final_gate(self):
        risk = type("Risk", (), {})()
        kill = type("Kill", (), {})()
        risk.is_paused = AsyncMock(return_value=True)
        kill.check_can_trade = AsyncMock(return_value={"allowed": True})
        with patch.object(sts, "_EXECUTION_QUARANTINE_REASON", None):
            paused = await sts._p04a_runtime_recheck(risk, kill)
        self.assertEqual(paused["reason_code"], "EXEC_RISK_PAUSED")
        kill.check_can_trade.assert_not_awaited()

        async def arm_during_read():
            sts._EXECUTION_QUARANTINE_REASON = "P03 novo incidente"
            return False

        risk.is_paused = AsyncMock(side_effect=arm_during_read)
        kill.check_can_trade = AsyncMock(return_value={"allowed": True})
        with patch.object(sts, "_EXECUTION_QUARANTINE_REASON", None):
            quarantined = await sts._p04a_runtime_recheck(risk, kill)
        self.assertEqual(quarantined["reason_code"], "EXEC_QUARANTINE_ACTIVE")

    async def test_non_boolean_kill_switch_permission_fails_closed(self):
        risk = type("Risk", (), {})()
        kill = type("Kill", (), {})()
        risk.is_paused = AsyncMock(return_value=False)
        kill.check_can_trade = AsyncMock(return_value={"allowed": "true"})
        with patch.object(sts, "_EXECUTION_QUARANTINE_REASON", None):
            result = await sts._p04a_runtime_recheck(risk, kill)
        self.assertEqual(result["reason_code"], "EXEC_KILL_SWITCH_BLOCKED")


class IncidentQtyTests(unittest.TestCase):
    def test_submitted_qty_overrides_legacy_local_qty(self):
        kwargs = assemble_entry_incident(
            {
                "was_maker": True,
                "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
                "submitted_qty": 0.8,
                "client_order_id": "cw-q",
            },
            {"symbol": "BTC/USDT:USDT", "direction": "long", "stop_loss": 98},
            local_client_order_id="cw-q",
            local_planned_qty=1.0,
        )
        self.assertEqual(kwargs["planned_qty"], 0.8)

    def test_invalid_submitted_qty_falls_back_to_local_plan(self):
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                kwargs = assemble_entry_incident(
                    {
                        "was_maker": True,
                        "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
                        "submitted_qty": invalid,
                        "client_order_id": "cw-q-invalid",
                    },
                    {"symbol": "BTC/USDT:USDT", "direction": "long"},
                    local_client_order_id="cw-q-invalid",
                    local_planned_qty=1.0,
                )
                self.assertEqual(kwargs["planned_qty"], 1.0)


class ScopeGuardTests(unittest.TestCase):
    def test_p04a_keeps_maker_off_and_market_fallback_out_of_wiring(self):
        signature = inspect.signature(bss.place_maker_entry_then_protect)
        self.assertIs(signature.parameters["fallback_market"].default, False)

        shadow_source = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertIn(
            'MAKER_ENTRY_ENABLED = os.getenv("MAKER_ENTRY_ENABLED", "false")',
            shadow_source,
        )
        self.assertIn(
            'P04B_MAKER_FALLBACK_ENABLED = os.getenv(',
            shadow_source,
        )
        self.assertIn(
            '"P04B_MAKER_FALLBACK_ENABLED", "false"',
            shadow_source,
        )
        self.assertIn("fallback_market=_p04b_fallback_effective,", shadow_source)
        self.assertIn("MIN_RR_TP1_EXEC if RR_GATE_ENABLED", shadow_source)
        self.assertEqual(
            select_entry_route(maker_enabled=True, maker_available=False),
            "blocked",
        )
        self.assertEqual(
            select_entry_route(
                maker_enabled=True,
                maker_available=True,
                preflight_enabled=False,
            ),
            "blocked-preflight",
        )
        self.assertEqual(
            select_entry_route(maker_enabled=False, maker_available=False),
            "market",
        )
        self.assertEqual(normalize_entry_side("LONG"), "long")
        self.assertEqual(normalize_entry_side("sell"), "short")
        for invalid in (None, "", "garbage", 0):
            self.assertIsNone(normalize_entry_side(invalid))

        pure_source = (BACKEND / "services" / "entry_revalidation_service.py").read_text()
        self.assertNotIn("place_order(", pure_source)
        self.assertNotIn("httpx", pure_source)
        self.assertNotIn(
            'side = "long" if rec.get("direction") == "long" else "short"',
            shadow_source,
        )
        self.assertIn('EXEC_SIDE_INVALID: {_reason}', shadow_source)
        self.assertIn('EXEC_PREFLIGHT_DISABLED: {_reason}', shadow_source)


if __name__ == "__main__":
    unittest.main()
