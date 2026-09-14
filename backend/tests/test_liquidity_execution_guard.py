"""Liquidez de execução: fonte Binance, USD correto e falha fechada só LIVE.

Testes herméticos: HTTP, exchange e persistência nunca são acessados.
"""
from __future__ import annotations

import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_futures_service as futures
from services import binance_service as legacy_okx
from services import edge_decay_service, exchange_service
from services import shadow_trade_service as shadow

SYMBOL = "BTC/USDT:USDT"


def _ticker(**changes):
    out = {
        "symbol": SYMBOL, "exchange": "binance", "source": "binance_futures",
        "last": 100.0, "volume": 20_000_000.0,
    }
    out.update(changes)
    return out


def _quote(**changes):
    out = {
        "ok": True, "symbol": "BTCUSDT", "exchange": "binance",
        "source": "binance_book_ticker", "bid": 99.9, "ask": 100.1,
    }
    out.update(changes)
    return out


class _HermeticTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("getaddrinfo", "create_connection"):
            self.stack.enter_context(patch.object(
                socket, target, side_effect=AssertionError("rede proibida no teste")
            ))


class FuturesTickerValidationTests(_HermeticTest):
    async def _fetch(self, payload):
        response = Mock()
        response.json.return_value = payload
        transport = AsyncMock(return_value=response)
        with patch.object(futures, "PROXY_ENABLED", True), patch.object(
            futures, "_ticker_cache", {}
        ), patch.object(futures, "_proxied_get", transport):
            result = await futures.fetch_ticker(SYMBOL)
            cached = await futures.fetch_ticker(SYMBOL)
        self.assertEqual(transport.await_count, 1)
        self.assertEqual(cached, result)
        return result

    async def test_valid_payload_preserves_quote_volume_and_cache(self):
        result = await self._fetch({
            "symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "12345678",
            "priceChangePercent": "1.2", "highPrice": "110", "lowPrice": "90",
        })
        self.assertEqual(result["volume"], 12_345_678)
        self.assertEqual(result["last"], 100)
        self.assertEqual(result["source"], "binance_futures")
        self.assertEqual(result["exchange"], "binance")
        self.assertEqual(result["change"], 1.2)

    async def test_empty_malformed_error_or_wrong_symbol_is_rejected(self):
        for payload in ([], {}, None, {"code": -1121}, {"data": []},
                        {"symbol": "ETHUSDT", "lastPrice": "100", "quoteVolume": "1e8"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self._fetch(payload)

    async def test_required_numbers_are_finite_and_valid(self):
        for field in ("lastPrice", "quoteVolume"):
            for value in (None, "", "bad", "NaN", "Infinity", "-Infinity", -1, True):
                payload = {"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "1e8"}
                payload[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    await self._fetch(payload)
        with self.assertRaises(ValueError):
            await self._fetch({"symbol": "BTCUSDT", "lastPrice": 0, "quoteVolume": 10})

    async def test_known_zero_volume_remains_zero(self):
        result = await self._fetch({"symbol": "BTCUSDT", "lastPrice": "100", "quoteVolume": "0"})
        self.assertEqual(result["volume"], 0)


class LiveLiquidityGuardTests(_HermeticTest):
    def setUp(self):
        super().setUp()
        for name, value in (("MIN_QUOTE_VOL_24H_USD", 10_000_000), ("MAX_SPREAD_PCT", 0.25)):
            self.stack.enter_context(patch.object(shadow, name, value))
        self.stack.enter_context(patch.object(exchange_service, "ACTIVE_EXCHANGE", "binance"))
        self.fetch = self.stack.enter_context(patch.object(futures, "fetch_ticker", AsyncMock(return_value=_ticker())))
        self.quote = self.stack.enter_context(patch.object(exchange_service, "get_execution_quote", AsyncMock(return_value=_quote())))
        self.legacy = self.stack.enter_context(patch.object(legacy_okx, "fetch_ticker", AsyncMock(side_effect=AssertionError("OKX proibida em LIVE"))))

    async def test_valid_ticker_and_quote_pass_without_using_okx(self):
        allowed, _ = await shadow._check_live_liquidity(SYMBOL)
        self.assertTrue(allowed)
        self.fetch.assert_awaited_once_with(SYMBOL)
        self.quote.assert_awaited_once()
        self.legacy.assert_not_awaited()

    async def test_low_and_zero_quote_volume_block_before_quote_fetch(self):
        for volume in (0, 100_000):
            self.fetch.return_value = _ticker(volume=volume, last=10_000)
            with self.subTest(volume=volume):
                allowed, reason = await shadow._check_live_liquidity(SYMBOL)
                self.assertFalse(allowed)
                self.assertIn("vol 24h", reason)
        self.quote.assert_not_awaited()

    async def test_volume_threshold_is_inclusive_and_already_usd(self):
        self.fetch.return_value = _ticker(volume=10_000_000, last=0.001)
        self.assertTrue((await shadow._check_live_liquidity(SYMBOL))[0])

    async def test_invalid_ticker_is_unknown_not_known_low_volume(self):
        payloads = [None, [], {}, _ticker(source="okx"), _ticker(source=None),
                    _ticker(exchange="bybit"), _ticker(symbol="ETH/USDT:USDT")]
        for field in ("last", "volume"):
            for value in (None, "", "bad", float("nan"), float("inf"), -1, True):
                payloads.append(_ticker(**{field: value}))
            missing = _ticker()
            missing.pop(field)
            payloads.append(missing)
        payloads.append(_ticker(last=0))
        for payload in payloads:
            self.fetch.return_value = payload
            with self.subTest(payload=payload):
                allowed, reason = await shadow._check_live_liquidity(SYMBOL)
                self.assertFalse(allowed)
                self.assertIn("indisponível", reason)
        self.quote.assert_not_awaited()

    async def test_fetch_exceptions_fail_closed(self):
        for error in (IndexError("list index out of range"), TimeoutError(), ValueError("JSON")):
            self.fetch.side_effect = error
            with self.subTest(error=error):
                allowed, reason = await shadow._check_live_liquidity(SYMBOL)
                self.assertFalse(allowed)
                self.assertIn("indisponível", reason)
        self.fetch.side_effect = None
        self.quote.side_effect = TimeoutError()
        self.assertFalse((await shadow._check_live_liquidity(SYMBOL))[0])

    async def test_missing_malformed_nonfinite_or_wrong_source_quote_blocks(self):
        payloads = [None, [], {}, _quote(ok=False), _quote(source="unknown"),
                    _quote(exchange="bybit"), _quote(symbol="ETHUSDT"), _quote(bid=101, ask=100)]
        for field in ("bid", "ask"):
            for value in (None, "", "bad", float("nan"), float("inf"), 0, -1, True):
                payloads.append(_quote(**{field: value}))
            missing = _quote()
            missing.pop(field)
            payloads.append(missing)
        for payload in payloads:
            self.quote.return_value = payload
            with self.subTest(payload=payload):
                allowed, reason = await shadow._check_live_liquidity(SYMBOL)
                self.assertFalse(allowed)
                self.assertIn("indisponível", reason)

    async def test_known_wide_spread_and_threshold_equality(self):
        self.quote.return_value = _quote(bid=99, ask=101)
        allowed, reason = await shadow._check_live_liquidity(SYMBOL)
        self.assertFalse(allowed)
        self.assertIn("spread", reason)
        self.quote.return_value = _quote(bid=99.875, ask=100.125)
        self.assertTrue((await shadow._check_live_liquidity(SYMBOL))[0])

    async def test_disabled_component_does_not_fetch_or_require_its_data(self):
        with patch.object(shadow, "MAX_SPREAD_PCT", 0):
            self.assertTrue((await shadow._check_live_liquidity(SYMBOL))[0])
        self.quote.assert_not_awaited()
        self.fetch.reset_mock()
        with patch.object(shadow, "MIN_QUOTE_VOL_24H_USD", 0):
            self.assertTrue((await shadow._check_live_liquidity(SYMBOL))[0])
        self.fetch.assert_not_awaited()

    async def test_unknown_exchange_blocks_without_any_fetch(self):
        for active in (None, "", "unknown", "bybit"):
            with self.subTest(active=active), patch.object(exchange_service, "ACTIVE_EXCHANGE", active):
                self.assertFalse((await shadow._check_live_liquidity(SYMBOL))[0])
        self.fetch.assert_not_awaited()
        self.quote.assert_not_awaited()

    async def test_entry_loop_blocks_unknown_before_sizing_but_valid_reaches_next_gate(self):
        flags = {
            "DB_ENABLED": True, "SHADOW_ENABLED": False, "REGIME_SIZING_ENABLED": False,
            "P04C_DATA_FRESHNESS_ENABLED": False, "FILLER_FORA_ENABLED": False,
            "NEWS_GATE_ENABLED": False, "DAILY_PROFIT_TP_ENABLED": False,
            "PROXIMITY_GATE_ENABLED": False, "STRUCT_CHASE_GATE_ENABLED": False,
            "ATR_GATE_ENABLED": False, "RR_GATE_ENABLED": False,
            "PROB_TP1_GATE_ENABLED": False, "SCORE_ADJUSTERS_ENABLED": False,
            "SCORE_MIN": 0, "QUALITY_EDGE_GATE_ENABLED": False,
            "LIQUIDITY_GATE_ENABLED": True, "SYMBOL_BLACKLIST": set(),
        }
        with ExitStack() as stack:
            for name, value in flags.items():
                stack.enter_context(patch.object(shadow, name, value))
            stack.enter_context(patch.object(edge_decay_service, "is_enabled", return_value=False))
            stack.enter_context(patch.object(shadow, "_p04c_live_data_verdict", return_value={"ok": True}))
            stack.enter_context(patch.object(shadow, "_calibration_contract_verdict", return_value={"ok": True}))
            stack.enter_context(patch.object(shadow, "get_exec_allowlist", return_value=set()))
            downstream = stack.enter_context(patch.object(shadow, "_is_blocked_time", return_value=(True, "sentinela")))
            skipped = stack.enter_context(patch.object(shadow, "_record_skip"))
            sizing = stack.enter_context(patch.object(shadow, "_compute_qty", side_effect=AssertionError("sizing proibido")))
            order = stack.enter_context(patch.object(exchange_service, "place_order", AsyncMock(side_effect=AssertionError("ordem proibida"))))
            persist = stack.enter_context(patch.object(shadow.real_trade_service, "open_trade", AsyncMock(side_effect=AssertionError("DB proibido"))))
            for payload, stage in (({}, "liquidity-gate"), (_ticker(volume=0), "liquidity-gate"),
                                   (_ticker(), "time-block")):
                self.fetch.return_value = payload
                skipped.reset_mock()
                downstream.reset_mock()
                self.assertEqual(await shadow.open_shadow_for_recs([
                    {"_just_saved": True, "tier": "A", "symbol": SYMBOL, "score": 90}
                ]), 0)
                self.assertEqual(skipped.call_args.args[1], stage)
                self.assertEqual(downstream.call_count, int(stage == "time-block"))
            sizing.assert_not_called()
            order.assert_not_awaited()
            persist.assert_not_awaited()

            # OFF explícito preserva o no-op, mesmo quando os dados falhariam.
            for disabled in ({"LIQUIDITY_GATE_ENABLED": False},
                             {"MIN_QUOTE_VOL_24H_USD": 0, "MAX_SPREAD_PCT": 0}):
                with patch.multiple(shadow, **disabled):
                    self.fetch.reset_mock()
                    self.quote.reset_mock()
                    self.legacy.reset_mock()
                    await shadow.open_shadow_for_recs([
                        {"_just_saved": True, "tier": "A", "symbol": SYMBOL, "score": 90}
                    ])
                    self.assertEqual(skipped.call_args.args[1], "time-block")
                    self.fetch.assert_not_awaited()
                    self.quote.assert_not_awaited()
                    self.legacy.assert_not_awaited()

            # Shadow continua usando OKX: baixo volume bloqueia; válido/erro
            # chegam ao sizing mockado (None encerra sem persistência/ordem).
            with patch.object(shadow, "SHADOW_ENABLED", True), patch.object(
                shadow, "_resolve_equity_usd", AsyncMock(return_value=(5_000, "shadow"))
            ), patch.object(shadow, "_conviction_mult", return_value=(1, "off")), patch.object(
                shadow, "_edge_mult", return_value=(1, "off")
            ):
                sizing.side_effect = None
                sizing.return_value = None
                for volume, error, reaches_sizing in ((1_000, None, False),
                                                      (200_000, None, True),
                                                      (None, IndexError("list index out of range"), True)):
                    self.legacy.side_effect = error
                    self.legacy.return_value = {"last": 100, "volume": volume, "bid": 99.9, "ask": 100.1}
                    sizing.reset_mock()
                    skipped.reset_mock()
                    await shadow.open_shadow_for_recs([
                        {"_just_saved": True, "tier": "A", "symbol": SYMBOL, "score": 90}
                    ])
                    self.assertEqual(sizing.call_count, int(reaches_sizing))
                    self.assertEqual(skipped.call_count, int(not reaches_sizing))
                self.fetch.assert_not_awaited()
                self.quote.assert_not_awaited()
                order.assert_not_awaited()
                persist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
