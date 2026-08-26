"""P04B — depth/VWAP fail-closed antes de toda abertura MARKET Binance.

Testes herméticos: nenhuma rede, banco ou exchange real.
"""
from __future__ import annotations

import copy
import socket
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as bss  # noqa: E402
from services import exchange_service  # noqa: E402
from services.entry_revalidation_service import (  # noqa: E402
    estimate_market_depth_fill,
    evaluate_market_depth_revalidation,
)
from services.execution_reconciliation_service import assemble_entry_incident  # noqa: E402


NOW_MS = 2_000_000.0
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_NET_ATTEMPTS = []


def _blocked_network(*args, **kwargs):
    _NET_ATTEMPTS.append(args[:1])
    raise RuntimeError(f"rede bloqueada no teste P04B: {args[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    socket.getaddrinfo = _blocked_network
    socket.create_connection = _blocked_network


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO
    socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"P04B tentou acessar rede: {_NET_ATTEMPTS}")


class _Response:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _depth(**changes):
    out = {
        "ok": True,
        "exchange": "binance",
        "source": "binance_depth",
        "symbol": "BTCUSDT",
        "last_update_id": 42,
        "message_time_ms": NOW_MS - 50.0,
        "exchange_time_ms": NOW_MS - 50.0,
        "received_at_ms": NOW_MS - 40.0,
        "latency_ms": 10.0,
        "bids": [["99.9", "1"], ["99.8", "2"]],
        "asks": [["100.1", "1"], ["100.2", "2"]],
    }
    out.update(changes)
    return out


def _estimate(depth=None, **changes):
    params = {
        "depth": depth or _depth(),
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "qty": 1.5,
        "max_depth_age_ms": 1_000.0,
        "max_fetch_latency_ms": 1_000.0,
        "now_ms": NOW_MS,
    }
    params.update(changes)
    return estimate_market_depth_fill(**params)


def _verdict(depth=None, **changes):
    params = {
        "depth": depth or _depth(),
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "qty": 1.5,
        "planned_entry": 100.0,
        "stop_loss": 98.0,
        "tp1": 104.0,
        "tp2": 106.0,
        "atr": 5.0,
        "max_depth_age_ms": 1_000.0,
        "max_fetch_latency_ms": 1_000.0,
        "max_spread_pct": 0.5,
        "max_book_impact_pct": 0.5,
        "max_adverse_slippage_pct": 0.5,
        "max_chase_atr": 1.0,
        "min_rr_tp1": 1.0,
        "min_rr_tp2": 2.0,
        "now_ms": NOW_MS,
    }
    params.update(changes)
    return evaluate_market_depth_revalidation(**params)


def _market_rules(**changes):
    out = {
        "market_step": 0.1,
        "market_min_qty": 0.1,
        "market_max_qty": 100.0,
        "min_notional": 5.0,
    }
    out.update(changes)
    return out


def _approved(qty):
    return {
        "ok": True,
        "quality": "FRESH",
        "reason_code": "EXEC_MARKET_REVALIDATION_OK",
        "approved_qty": qty,
        "checks": {
            "best_executable_price": 100.0,
            "vwap_price": 100.1,
            "worst_price": 100.2,
        },
    }


def _protection_none():
    return {
        "sl_ok": True, "sl_order_id": None, "sl_msg": None,
        "tp1_ok": True, "tp1_order_id": None, "tp1_msg": None,
        "tp1_qty": 0.0, "tp1_skipped": False,
        "tp2_ok": True, "tp2_order_id": None, "tp2_msg": None,
    }


class DepthEstimateTests(unittest.TestCase):
    def test_long_uses_full_asks_and_is_pure(self):
        depth = _depth()
        before = copy.deepcopy(depth)
        out = _estimate(depth)
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["checks"]["vwap_price"], 100.1333333333)
        self.assertEqual(out["checks"]["worst_price"], 100.2)
        self.assertEqual(out["checks"]["levels_used"], 2)
        self.assertEqual(depth, before)

    def test_short_uses_full_bids(self):
        out = _estimate(side="short")
        self.assertTrue(out["ok"])
        self.assertAlmostEqual(out["checks"]["vwap_price"], 99.8666666667)
        self.assertEqual(out["checks"]["worst_price"], 99.8)

    def test_exact_coverage_passes_and_partial_depth_blocks(self):
        self.assertTrue(_estimate(qty=3.0)["ok"])
        blocked = _estimate(qty=3.0001)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason_code"], "EXEC_DEPTH_INSUFFICIENT")
        self.assertEqual(blocked["quality"], "FRESH")

    def test_malformed_or_ambiguous_book_blocks(self):
        cases = (
            _depth(asks=[]),
            _depth(asks=[["100.1", "1"], ["100.1", "2"]]),
            _depth(asks=[["100.2", "1"], ["100.1", "2"]]),
            _depth(asks=[["nan", "1"]]),
            _depth(last_update_id=1.5),
            _depth(bids=[["100.1", "1"]], asks=[["100.1", "1"]]),
        )
        for depth in cases:
            with self.subTest(depth=depth):
                self.assertFalse(_estimate(depth)["ok"])

    def test_identity_freshness_and_latency_are_fail_closed(self):
        cases = (
            (_depth(symbol="ETHUSDT"), {}, "EXEC_SYMBOL_MISMATCH"),
            (_depth(exchange="bybit"), {}, "EXEC_DEPTH_VENUE_MISMATCH"),
            (_depth(received_at_ms=NOW_MS - 2_000), {}, "EXEC_DEPTH_STALE"),
            (_depth(latency_ms=2_000), {}, "EXEC_DEPTH_SLOW"),
            (_depth(), {"side": "unknown"}, "EXEC_SIDE_INVALID"),
        )
        for depth, kwargs, code in cases:
            with self.subTest(code=code):
                out = _estimate(depth, **kwargs)
                self.assertFalse(out["ok"])
                self.assertEqual(out["reason_code"], code)
                self.assertEqual(out["quality"], "UNKNOWN")


class MarketVerdictTests(unittest.TestCase):
    def test_valid_long_and_short_pass(self):
        self.assertTrue(_verdict()["ok"])
        short = _verdict(
            side="short", stop_loss=102.0, tp1=96.0, tp2=94.0,
        )
        self.assertTrue(short["ok"])

    def test_spread_and_impact_exact_boundaries_pass(self):
        base = _estimate()
        spread = base["checks"]["spread_pct"]
        impact = base["checks"]["book_impact_pct"]
        self.assertTrue(_verdict(max_spread_pct=spread)["ok"])
        self.assertTrue(_verdict(max_book_impact_pct=impact)["ok"])
        self.assertEqual(
            _verdict(max_spread_pct=spread / 2)["reason_code"],
            "EXEC_SPREAD_TOO_WIDE",
        )
        self.assertEqual(
            _verdict(max_book_impact_pct=impact / 2)["reason_code"],
            "EXEC_BOOK_IMPACT_TOO_HIGH",
        )

    def test_worst_price_slippage_is_conservative(self):
        self.assertTrue(_verdict(max_adverse_slippage_pct=0.2)["ok"])
        self.assertEqual(
            _verdict(max_adverse_slippage_pct=0.199)["reason_code"],
            "EXEC_SLIPPAGE_TOO_HIGH",
        )

    def test_chase_boundary_blocks_symmetrically(self):
        self.assertEqual(_verdict(max_chase_atr=0.04)["reason_code"], "EXEC_PRICE_CHASE")
        short = _verdict(
            side="short", stop_loss=102.0, tp1=96.0, tp2=94.0,
            max_chase_atr=0.04,
        )
        self.assertEqual(short["reason_code"], "EXEC_PRICE_CHASE")

    def test_zone_levels_and_rr_use_worst_price(self):
        self.assertTrue(_verdict(entry_zone_low=99.0, entry_zone_high=100.2)["ok"])
        self.assertEqual(
            _verdict(entry_zone_low=99.0, entry_zone_high=100.19)["reason_code"],
            "EXEC_ENTRY_OUTSIDE_ZONE",
        )
        self.assertEqual(_verdict(tp1=100.1)["reason_code"], "EXEC_LEVELS_INVALID")
        self.assertEqual(_verdict(min_rr_tp1=2.0)["reason_code"], "EXEC_RR_TP1_TOO_LOW")


class DepthFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_depth_endpoint_shape_and_weight(self):
        client = type("Client", (), {})()
        client.get = AsyncMock(return_value=_Response({
            "lastUpdateId": 42, "E": 1_999_950, "T": 1_999_951,
            "bids": [["99.9", "1"]], "asks": [["100.1", "1"]],
        }, headers={"x-mbx-used-weight-1m": "9"}))
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=True)), \
                patch.object(bss, "record_external_weight") as weight, \
                patch.object(bss.time, "time", return_value=2_000.0):
            out = await bss.get_execution_depth("BTC/USDT:USDT", limit=50)
        self.assertTrue(out["ok"])
        self.assertEqual(out["symbol"], "BTCUSDT")
        self.assertEqual(client.get.await_args.args[0], f"{bss.BASE}/fapi/v1/depth")
        self.assertEqual(client.get.await_args.kwargs["params"], {"symbol": "BTCUSDT", "limit": 50})
        weight.assert_called_once_with("9")

    async def test_rate_gate_and_bad_limit_send_nothing(self):
        client = type("Client", (), {})()
        client.get = AsyncMock()
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=False)):
            rate = await bss.get_execution_depth("BTCUSDT")
            limit = await bss.get_execution_depth("BTCUSDT", limit=7)
        self.assertEqual(rate["reason_code"], "EXEC_DEPTH_RATE_LIMITED")
        self.assertEqual(limit["reason_code"], "EXEC_DEPTH_LIMIT_INVALID")
        client.get.assert_not_awaited()

    async def test_http_ban_and_invalid_payload_fail_closed(self):
        client = type("Client", (), {})()
        client.get = AsyncMock(return_value=_Response({}, status_code=429, headers={"retry-after": "2"}))
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=True)), \
                patch.object(bss, "arm_ban_external") as arm:
            limited = await bss.get_execution_depth("BTCUSDT")
        self.assertEqual(limited["reason_code"], "EXEC_DEPTH_HTTP_ERROR")
        arm.assert_called_once()

        client.get = AsyncMock(return_value=_Response({"lastUpdateId": 42, "E": 1, "T": 1, "bids": [], "asks": []}))
        with patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "await_rate_gate", AsyncMock(return_value=True)):
            malformed = await bss.get_execution_depth("BTCUSDT")
        self.assertEqual(malformed["reason_code"], "EXEC_DEPTH_INVALID_PAYLOAD")


class MarketOrderWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_market_without_guard_is_blocked_before_rounding_or_post(self):
        with patch.object(bss, "_round_qty", AsyncMock()) as rounding, \
                patch.object(bss, "_signed_request", AsyncMock()) as signed:
            out = await bss.place_order("BTCUSDT", "Buy", 1.0)
        self.assertEqual(out["reason_code"], "EXEC_MARKET_PREFLIGHT_REQUIRED")
        self.assertTrue(out["entry_not_submitted"])
        rounding.assert_not_awaited()
        signed.assert_not_awaited()

    async def test_blocked_guard_sends_zero_posts(self):
        guard = AsyncMock(return_value={
            "ok": False, "quality": "UNKNOWN",
            "reason_code": "EXEC_DEPTH_STALE", "reason": "depth vencido",
        })
        client = type("Client", (), {})()
        client.request = AsyncMock()
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(return_value=_market_rules())), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            out = await bss.place_order(
                "BTCUSDT", "Buy", 1.0, entry_preflight=guard,
            )
        self.assertEqual(out["reason_code"], "EXEC_DEPTH_STALE")
        self.assertEqual(out["submitted_qty"], 0.0)
        client.request.assert_not_awaited()

    async def test_approved_qty_is_floored_signed_and_reported(self):
        events = []

        async def filters(_symbol):
            events.append("filters")
            return _market_rules()

        async def guard(qty, rules):
            events.append("guard")
            self.assertEqual(qty, 1.0)
            self.assertEqual(rules["step"], 0.1)
            return _approved(0.876)

        async def post(_method, url):
            events.append("post")
            qty = parse_qs(urlparse(url).query)["quantity"][0]
            self.assertEqual(qty, "0.8")
            return _Response({
                "orderId": 7, "clientOrderId": "cw-p04b",
                "status": "FILLED", "executedQty": "0.8", "avgPrice": "100.1",
            })

        client = type("Client", (), {})()
        client.request = AsyncMock(side_effect=post)
        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(side_effect=filters)), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "_place_post_fill_protection", AsyncMock(return_value=_protection_none())), \
                patch.object(bss, "_enforce_entry_safety", AsyncMock(return_value={
                    "safety_state": "ENTRY_CONFIRMED_NO_SL_REQUESTED",
                })):
            out = await bss.place_order(
                "BTCUSDT", "Buy", 1.0, client_order_id="cw-p04b",
                entry_preflight=guard,
            )
        self.assertEqual(events, ["filters", "guard", "post"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["submitted_qty"], 0.8)
        self.assertEqual(out["client_order_id"], "cw-p04b")

    async def test_qty_increase_and_min_notional_fail_before_post(self):
        client = type("Client", (), {})()
        client.request = AsyncMock()
        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(return_value=_market_rules())), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            increase = await bss.place_order(
                "BTCUSDT", "Buy", 1.0,
                entry_preflight=AsyncMock(return_value=_approved(1.1)),
            )
        self.assertEqual(increase["reason_code"], "EXEC_QTY_REVALIDATION_FAILED")
        client.request.assert_not_awaited()

        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(return_value=_market_rules(min_notional=100.0))), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            notional = await bss.place_order(
                "BTCUSDT", "Buy", 0.1,
                entry_preflight=AsyncMock(return_value=_approved(0.1)),
            )
        self.assertEqual(notional["reason_code"], "EXEC_MIN_NOTIONAL_FAILED")
        client.request.assert_not_awaited()

        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(return_value=_market_rules(min_notional=float("nan")))), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0):
            nonfinite = await bss.place_order(
                "BTCUSDT", "Buy", 1.0,
                entry_preflight=AsyncMock(return_value=_approved(1.0)),
            )
        self.assertEqual(nonfinite["reason_code"], "EXEC_MARKET_FILTERS_UNKNOWN")
        client.request.assert_not_awaited()

    async def test_ambiguous_submission_protects_only_submitted_qty(self):
        client = type("Client", (), {})()
        client.request = AsyncMock(return_value=_Response(
            {"code": -1007, "msg": "timeout"}, status_code=400,
        ))
        protect = AsyncMock(return_value=_protection_none())
        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_get_symbol_filters", AsyncMock(return_value=_market_rules())), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_get_client", return_value=client), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "get_order", AsyncMock(return_value={
                    "ok": False, "error": "consulta incerta",
                })), patch.object(bss, "_place_post_fill_protection", protect), \
                patch.object(bss, "_fire_telegram"):
            out = await bss.place_order(
                "BTCUSDT", "Buy", 1.0, stop_loss=98.0,
                client_order_id="cw-p04b-unknown",
                entry_preflight=AsyncMock(return_value=_approved(0.8)),
            )
        self.assertEqual(out["submitted_qty"], 0.8)
        self.assertEqual(protect.await_args.args[2], 0.8)

    async def test_reduce_only_close_does_not_require_entry_guard(self):
        signed = AsyncMock(return_value={
            "ok": True,
            "result": {"status": "FILLED", "executedQty": "0.5", "orderId": 9},
            "raw": {},
        })
        with patch.object(bss, "_round_qty", AsyncMock(side_effect=lambda _s, q: q)), \
                patch.object(bss, "_signed_request", signed), \
                patch.object(bss, "_enforce_entry_safety", AsyncMock(return_value={
                    "safety_state": "NOT_APPLICABLE",
                })), \
                patch.object(bss, "_fresh_position_size", AsyncMock(return_value=(0.0, "fresh"))):
            out = await bss.place_order(
                "BTCUSDT", "Sell", 0.5, reduce_only=True,
            )
        self.assertTrue(out["ok"])
        self.assertIsNone(signed.await_args.kwargs["request_preflight"])


class MakerFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_gtx_rejection_uses_guarded_market_with_distinct_id(self):
        market = AsyncMock(return_value={"ok": True})
        guard = AsyncMock()
        original = "x" * 36
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "_signed_request", AsyncMock(return_value={
                    "ok": False, "code": -5022, "msg": "Post Only order will be rejected",
                    "_request_sent": True,
                })), patch.object(bss, "place_order", market):
            out = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                client_order_id=original, fallback_market=True,
                market_preflight=guard,
            )
        kwargs = market.await_args.kwargs
        self.assertIs(kwargs["entry_preflight"], guard)
        self.assertNotEqual(kwargs["client_order_id"], original)
        self.assertLessEqual(len(kwargs["client_order_id"]), 36)
        self.assertEqual(out["client_order_id"], kwargs["client_order_id"])
        self.assertTrue(out["fell_back_to_market"])

    async def test_ambiguous_gtx_never_falls_back_to_market(self):
        market = AsyncMock()
        with patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(return_value=100.0)), \
                patch.object(bss, "_signed_request", AsyncMock(return_value={
                    "ok": False, "error": "timeout", "_request_sent": True,
                })), patch.object(bss, "place_order", market), \
                patch.object(bss, "_fire_telegram"):
            out = await bss.place_maker_entry_then_protect(
                "BTCUSDT", "Buy", 1.0, limit_price=100.0,
                fallback_market=True, market_preflight=AsyncMock(),
            )
        market.assert_not_awaited()
        self.assertFalse(out["fell_back_to_market"])
        self.assertTrue(out["manual_intervention_required"])


class IntegrationContractTests(unittest.TestCase):
    def test_p03_prefers_actual_market_identity_and_submitted_qty(self):
        kw = assemble_entry_incident(
            {
                "client_order_id": "cw-maker-mfb",
                "submitted_qty": 0.8,
                "safety_state": "ENTRY_SUBMISSION_UNKNOWN",
            },
            {"symbol": "BTC/USDT:USDT", "direction": "long", "qty": 1.0},
            local_client_order_id="cw-maker",
            local_planned_qty=1.0,
        )
        self.assertEqual(kw["client_order_id"], "cw-maker-mfb")
        self.assertEqual(kw["planned_qty"], 0.8)

    def test_defaults_keep_fallback_off_and_depth_guard_on(self):
        source = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertIn('"P04B_MARKET_REVALIDATION_ENABLED", "true"', source)
        self.assertIn('"P04B_MAKER_FALLBACK_ENABLED", "false"', source)
        self.assertIn("fallback_market=_p04b_fallback_effective,", source)
        self.assertTrue(callable(getattr(exchange_service, "get_execution_depth", None)))

    def test_pure_depth_module_has_no_transport_or_order_side_effects(self):
        source = (BACKEND / "services" / "entry_revalidation_service.py").read_text()
        self.assertNotIn("place_order(", source)
        self.assertNotIn("httpx", source)


if __name__ == "__main__":
    unittest.main()
