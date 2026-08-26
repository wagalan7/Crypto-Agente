"""P04C — validade central dos dados antes de qualquer entrada live."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

from services.data_freshness_service import (
    evaluate_entry_data_freshness,
    prepare_closed_candles,
    timeframe_ms,
)
from models.trade_signal import TradeSignal
from services import regime_service as rs


BACKEND = Path(__file__).resolve().parents[1]


def _function_source(relative_path: str, function_name: str) -> str:
    source = (BACKEND / relative_path).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"função não encontrada: {function_name}")


NOW_MS = 1_800_000
FIVE_MIN_MS = 300_000


def _candles(*opens: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": ts,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 10.0,
            }
            for ts in opens
        ]
    )


def _rec(
    *,
    candle_open_ms: int = 1_200_000,
    ticker_observed_at_ms: int | None = None,
    derivatives_observed_at_ms: int | None = None,
) -> dict:
    freshness = {
        "candle": {
            "quality": "FRESH",
            "source": "test",
            "symbol": "BTC/USDT:USDT",
            "timeframe": "5m",
            "open_time_ms": candle_open_ms,
            "close_time_ms": candle_open_ms + FIVE_MIN_MS,
            "observed_at_ms": NOW_MS,
        }
    }
    if ticker_observed_at_ms is not None:
        freshness["ticker"] = {
            "quality": "FRESH",
            "symbol": "BTC/USDT:USDT",
            "observed_at_ms": ticker_observed_at_ms,
        }
    derivatives = None
    if derivatives_observed_at_ms is not None:
        derivatives = {
            "symbol": "BTC/USDT:USDT",
            "funding_rate_pct": 0.01,
            "funding_sentiment": "neutral",
        }
        freshness["derivatives"] = {
            "quality": "FRESH",
            "symbol": "BTC/USDT:USDT",
            "observed_at_ms": derivatives_observed_at_ms,
        }
    return {
        "symbol": "BTC/USDT:USDT",
        "timeframe": "5m",
        "quote_vol_usd": 10_000_000.0 if ticker_observed_at_ms is not None else None,
        "signal": {
            "symbol": "BTC/USDT:USDT",
            "timeframe": "5m",
            "timestamp": candle_open_ms,
            "derivatives": derivatives,
            "data_freshness": freshness,
        },
    }


def _regime(*, quality: str = "FRESH", observed_at_ms: int = NOW_MS) -> dict:
    return {
        "filter_enabled": True,
        "quality": quality,
        "observed_at_ms": observed_at_ms,
        "regime": "NORMAL",
    }


class TimeframeTests(unittest.TestCase):
    def test_supported_timeframes(self):
        self.assertEqual(timeframe_ms("5m"), FIVE_MIN_MS)
        self.assertEqual(timeframe_ms("1h"), 3_600_000)
        self.assertEqual(timeframe_ms("3d"), 259_200_000)
        self.assertEqual(timeframe_ms("1w"), 604_800_000)

    def test_invalid_timeframe_is_unknown(self):
        self.assertIsNone(timeframe_ms("banana"))


class ClosedCandleTests(unittest.TestCase):
    def test_open_candle_is_removed_and_previous_closed_is_used(self):
        frame, verdict = prepare_closed_candles(
            _candles(900_000, 1_200_000, 1_500_000),
            "5m",
            now_ms=1_700_000,
            max_lag_periods=1.25,
        )
        self.assertTrue(verdict["ok"])
        self.assertEqual(frame["timestamp"].tolist(), [900_000, 1_200_000])
        self.assertEqual(verdict["checks"]["open_time_ms"], 1_200_000)
        self.assertEqual(verdict["checks"]["close_time_ms"], 1_500_000)

    def test_stale_last_closed_candle_is_rejected(self):
        frame, verdict = prepare_closed_candles(
            _candles(300_000, 600_000),
            "5m",
            now_ms=NOW_MS,
            max_lag_periods=1.25,
        )
        self.assertIsNone(frame)
        self.assertEqual(verdict["reason_code"], "EXEC_CANDLE_STALE")

    def test_unsorted_duplicate_future_and_non_finite_are_rejected(self):
        cases = [
            _candles(1_200_000, 900_000),
            _candles(1_200_000, 1_200_000),
            _candles(NOW_MS + FIVE_MIN_MS),
            _candles(1_200_000).assign(close=float("nan")),
        ]
        for frame in cases:
            with self.subTest(frame=frame.to_dict("records")):
                out, verdict = prepare_closed_candles(frame, "5m", now_ms=NOW_MS)
                self.assertIsNone(out)
                self.assertFalse(verdict["ok"])

    def test_missing_candle_in_sequence_is_rejected(self):
        out, verdict = prepare_closed_candles(
            _candles(600_000, 1_200_000),
            "5m",
            now_ms=NOW_MS,
        )
        self.assertIsNone(out)
        self.assertEqual(verdict["reason_code"], "EXEC_CANDLE_GAP")


class EntryContextTests(unittest.TestCase):
    def test_fresh_required_context_passes(self):
        verdict = evaluate_entry_data_freshness(
            _rec(ticker_observed_at_ms=NOW_MS, derivatives_observed_at_ms=NOW_MS),
            _regime(),
            now_ms=NOW_MS,
        )
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_DATA_FRESHNESS_OK")

    def test_open_or_stale_signal_candle_blocks(self):
        open_candle = _rec(candle_open_ms=1_700_000)
        stale_candle = _rec(candle_open_ms=600_000)
        self.assertEqual(
            evaluate_entry_data_freshness(open_candle, _regime(), now_ms=NOW_MS)["reason_code"],
            "EXEC_CANDLE_NOT_CLOSED",
        )
        self.assertEqual(
            evaluate_entry_data_freshness(stale_candle, _regime(), now_ms=NOW_MS)["reason_code"],
            "EXEC_CANDLE_STALE",
        )

    def test_unknown_or_stale_regime_blocks_but_explicitly_disabled_passes(self):
        self.assertEqual(
            evaluate_entry_data_freshness(_rec(), _regime(quality="UNKNOWN"), now_ms=NOW_MS)["reason_code"],
            "EXEC_REGIME_UNKNOWN",
        )
        self.assertEqual(
            evaluate_entry_data_freshness(
                _rec(), _regime(observed_at_ms=NOW_MS - 901_000), now_ms=NOW_MS
            )["reason_code"],
            "EXEC_REGIME_STALE",
        )
        disabled = {"filter_enabled": False, "quality": "DISABLED", "observed_at_ms": NOW_MS}
        self.assertTrue(
            evaluate_entry_data_freshness(_rec(), disabled, now_ms=NOW_MS)["ok"]
        )

    def test_context_is_required_only_when_it_influenced_the_signal(self):
        no_optional_context = evaluate_entry_data_freshness(
            _rec(), _regime(), now_ms=NOW_MS
        )
        self.assertTrue(no_optional_context["ok"])

        stale_ticker = evaluate_entry_data_freshness(
            _rec(ticker_observed_at_ms=NOW_MS - 301_000),
            _regime(),
            now_ms=NOW_MS,
        )
        self.assertEqual(stale_ticker["reason_code"], "EXEC_TICKER_STALE")

        stale_derivatives = evaluate_entry_data_freshness(
            _rec(derivatives_observed_at_ms=NOW_MS - 301_000),
            _regime(),
            now_ms=NOW_MS,
        )
        self.assertEqual(
            stale_derivatives["reason_code"], "EXEC_DERIVATIVES_STALE"
        )

    def test_metadata_mismatch_fails_closed(self):
        rec = _rec()
        rec["signal"]["data_freshness"]["candle"]["open_time_ms"] = 900_000
        verdict = evaluate_entry_data_freshness(rec, _regime(), now_ms=NOW_MS)
        self.assertEqual(verdict["reason_code"], "EXEC_CANDLE_IDENTITY_MISMATCH")

    def test_cross_symbol_context_fails_closed(self):
        rec = _rec(ticker_observed_at_ms=NOW_MS)
        rec["signal"]["data_freshness"]["ticker"]["symbol"] = "ETH/USDT:USDT"
        verdict = evaluate_entry_data_freshness(rec, _regime(), now_ms=NOW_MS)
        self.assertEqual(verdict["reason_code"], "EXEC_TICKER_IDENTITY_MISMATCH")

    def test_empty_derivatives_defaults_did_not_influence_signal(self):
        rec = _rec()
        rec["signal"]["derivatives"] = {
            "symbol": "BTC/USDT:USDT",
            "funding_rate": None,
            "open_interest": None,
            "oi_change_24h_pct": None,
            "funding_sentiment": "neutral",
        }
        self.assertTrue(
            evaluate_entry_data_freshness(rec, _regime(), now_ms=NOW_MS)["ok"]
        )

    def test_mtf_context_requires_its_own_closed_candle_proof(self):
        rec = _rec()
        rec["signal"]["mtf"] = {
            "higher_tfs": [{"timeframe": "15m", "direction": "bullish"}]
        }
        missing = evaluate_entry_data_freshness(rec, _regime(), now_ms=NOW_MS)
        self.assertEqual(missing["reason_code"], "EXEC_MTF_METADATA_MISSING")

        rec["signal"]["mtf"]["higher_tfs"][0]["data_freshness"] = {
            "candle": {
                "quality": "FRESH",
                "symbol": "BTC/USDT:USDT",
                "timeframe": "15m",
                "open_time_ms": 600_000,
                "close_time_ms": 1_500_000,
            }
        }
        self.assertTrue(
            evaluate_entry_data_freshness(rec, _regime(), now_ms=NOW_MS)["ok"]
        )


class ObservationMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_regime_marks_unknown_instead_of_claiming_fresh(self):
        old_cache = dict(rs._cache)
        rs._cache.update({"ts": 0, "data": None})
        try:
            with patch.object(rs, "REGIME_FILTER_ENABLED", True), \
                    patch.object(rs, "ALT_RISKOFF_ENABLED", False), \
                    patch.object(rs, "SHORT_BRAKE_ENABLED", True), \
                    patch.object(rs, "_fetch_btc_24h_pct", AsyncMock(return_value=None)), \
                    patch.object(rs, "_fetch_btc_dominance", AsyncMock(return_value=54.0)), \
                    patch.object(rs, "_fetch_btc_trend_pct", AsyncMock(return_value=2.0)):
                out = await rs.get_regime_status()
            self.assertTrue(out["filter_enabled"])
            self.assertEqual(out["quality"], "UNKNOWN")
            self.assertEqual(rs._cache["data"], None)
        finally:
            rs._cache.clear()
            rs._cache.update(old_cache)

    async def test_disabled_regime_is_explicit_not_unknown(self):
        with patch.object(rs, "REGIME_FILTER_ENABLED", False):
            out = await rs.get_regime_status()
        self.assertFalse(out["filter_enabled"])
        self.assertEqual(out["quality"], "DISABLED")


class LiveGateIntegrationTests(unittest.TestCase):
    def test_live_helper_is_fail_closed_and_uses_all_central_limits(self):
        source = _function_source(
            "services/shadow_trade_service.py", "_p04c_live_data_verdict"
        )
        for token in (
            "EXEC_DATA_FRESHNESS_DISABLED",
            "EXEC_DATA_FRESHNESS_UNKNOWN",
            "evaluate_entry_data_freshness",
            "P04C_MAX_CANDLE_LAG_PERIODS",
            "P04C_MAX_TICKER_AGE_MS",
            "P04C_MAX_DERIVATIVES_AGE_MS",
            "P04C_MAX_REGIME_AGE_MS",
        ):
            self.assertIn(token, source)

    def test_wiring_is_before_indicators_and_before_any_live_entry_post(self):
        for function_name in (
            "_analyze_symbol_tf",
            "_analyze_candles_for_tf",
            "_analyze_symbol_tf_server",
        ):
            source = _function_source("services/recommendation_service.py", function_name)
            self.assertLess(
                source.index("_prepare_signal_candles"),
                source.index("calculate_indicators"),
            )

        mtf_source = _function_source("services/mtf_service.py", "_analyze_tf")
        self.assertLess(
            mtf_source.index("prepare_closed_candles"),
            mtf_source.index("calculate_indicators"),
        )

        live_source = _function_source(
            "services/shadow_trade_service.py", "open_shadow_for_recs"
        )
        self.assertLess(
            live_source.index("_p04c_live_data_verdict(rec"),
            live_source.index("await exchange_service.place_order"),
        )
        shadow_source = (BACKEND / "services/shadow_trade_service.py").read_text()
        derivatives_source = (BACKEND / "services/derivatives_service.py").read_text()
        recommendation_source = (BACKEND / "services/recommendation_service.py").read_text()
        self.assertIn('"p04c_data_freshness_enabled"', shadow_source)
        self.assertIn('"P04C_DATA_FRESHNESS_ENABLED", "true"', shadow_source)
        self.assertIn("symbol=symbol", derivatives_source)
        self.assertIn('quality = "FRESH" if available == 3', derivatives_source)
        self.assertIn('"data_freshness": (', recommendation_source)
        self.assertIn("data_freshness", TradeSignal.model_fields)


if __name__ == "__main__":
    unittest.main()
