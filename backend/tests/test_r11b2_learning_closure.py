"""R11B2 closure: real functions, synthetic rows and local ASGI only."""
import ast
import json
import logging
from pathlib import Path
import socket
import traceback
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from services import learning_service as ls


def snap(r, symbol="AAA/USDT", tier="A"):
    return NS(realized_r=r, symbol=symbol, tier=tier, timeframe="4h",
              direction="long", features={})


class Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.rows


class NumericClosure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        saved = dict(ls._cache)
        self.addCleanup(lambda: (ls._cache.clear(), ls._cache.update(saved)))
        ls.invalidate_cache()
        for name in ("getaddrinfo", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("network forbidden"))
            p.start()
            self.addCleanup(p.stop)

    async def call(self, name, rows, *args):
        with patch.object(ls, "DB_ENABLED", True), \
                patch.object(ls, "get_session", lambda: Session(rows)):
            return await getattr(ls, name)(*args)

    async def test_conversion_overflow_excluded_not_raised(self):
        stats = await self.call("compute_symbol_stats", [snap(10 ** 400), snap(1.0)])
        self.assertEqual(stats["AAA"]["trades"], 1)
        self.assertEqual(stats["AAA"]["data_quality"]["excluded_by_reason"]["nao_finito"], 1)
        self.assertIsNone(ls._valid_count(10 ** 400))
        self.assertIsNone(ls._valid_rate(10 ** 400))

    async def test_symbol_aggregate_overflow_never_judges_merit(self):
        for r in (1e308, -1e308):
            with self.subTest(r=r):
                stats = await self.call("compute_symbol_stats", [snap(r)] * 30)
                row = stats["AAA"]
                self.assertEqual(row["trades"], 30)  # finite inputs, unusable aggregate
                self.assertFalse(row["sample_ok"])
                self.assertNotIn(row["verdict"], ("promote", "demote", "neutro"))
                self.assertIsNone(row["avg_r"])
                self.assertIsNone(row["total_r"])
                self.assertEqual(row["numeric_error"], "aggregate_not_finite")
                json.dumps(stats, allow_nan=False)

    async def test_bucket_overflow_cannot_boost_or_block(self):
        for r in (1e308, -1e308):
            with self.subTest(r=r):
                ls.invalidate_cache()
                stats = await self.call("compute_stats_by_bucket", [snap(r)] * 30)
                self.assertIsNone(stats["overall"]["avg_r"])
                self.assertIsNone(stats["by_tier_timeframe"]["A_4h"]["win_rate"])
                self.assertEqual(stats["winning_combos"], [])
                self.assertEqual(stats["losing_combos"], [])
                with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)):
                    adjusted = await ls.compute_auto_adjustments()
                self.assertEqual(adjusted["blocked_buckets"], [])
                self.assertTrue(all(not items for items in adjusted["score_multipliers"].values()))
                json.dumps([stats, adjusted], allow_nan=False)

    async def test_overflow_latches_even_when_later_values_cancel(self):
        stats = await self.call("compute_stats_by_bucket",
                                [snap(1e308), snap(1e308), snap(-1e308), snap(-1e308)])
        self.assertIsNone(stats["overall"]["total_r"])
        self.assertIsNone(stats["by_tier"]["A"]["total_r"])
        json.dumps(stats, allow_nan=False)

    async def test_bad_bucket_does_not_disable_other_valid_buckets(self):
        rows = [snap(1e308, tier="A"), snap(-1e308, tier="B")] * 30
        rows += [snap(1.0, tier="C")] * 30
        stats = await self.call("compute_stats_by_bucket", rows)
        self.assertEqual(stats["overall"]["total_r"], 30.0)
        self.assertIsNone(stats["by_tier"]["A"]["avg_r"])
        self.assertEqual(stats["by_tier"]["C"]["avg_r"], 1.0)
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)):
            adjusted = await ls.compute_auto_adjustments()
        self.assertEqual(adjusted["score_multipliers"]["tier_tf"], {"C_4h": 1.25})
        json.dumps([stats, adjusted], allow_nan=False)

    async def test_single_and_batch_overflow_report_unavailable(self):
        rows = [snap(1e308)] * 12
        one = await self.call("lookup_historical_for", rows, "A", "4h", "long")
        batch = await self.call("lookup_historical_batch", rows,
                                [{"tier": "A", "timeframe": "4h", "direction": "long"}])
        for row in (one, batch["A_4h_long"]):
            self.assertFalse(row["sample_ok"])
            self.assertIsNone(row["avg_r"])
            self.assertIsNone(row["win_rate"])
            self.assertEqual(row["numeric_error"], "aggregate_not_finite")
            json.dumps(row, allow_nan=False)

    async def test_valid_arithmetic_and_zero_unchanged(self):
        rows = [snap(r) for r in (0.1, 0.2, 0.0, -0.5)] * 4
        stats = await self.call("compute_symbol_stats", rows)
        self.assertEqual(stats["AAA"]["total_r"], round(sum(r.realized_r for r in rows), 2))
        self.assertEqual(stats["AAA"]["avg_r"], round(sum(r.realized_r for r in rows) / len(rows), 3))
        self.assertNotIn("numeric_error", stats["AAA"])

    async def test_real_route_orders_unavailable_rows_last_without_fabricating_zero(self):
        rows = [snap(None, "EMPTY/USDT"), snap(0.0, "ZERO/USDT"),
                snap(-1.0, "SMALL/USDT"), snap(None, "EMPTY2/USDT")]
        rows += [snap(0.5, "READY/USDT")] * 12
        stats = await self.call("compute_symbol_stats", rows)
        path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(path.read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "rotation_symbol_stats"]
        self.assertEqual(len(nodes), 1)
        app = FastAPI()
        scope = {"app": app, "HTTPException": HTTPException,
                 "logging": logging, "traceback": traceback}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
        with patch.object(ls, "compute_symbol_stats", AsyncMock(return_value=stats)), TestClient(app) as client:
            response = client.get("/api/rotation/symbol-stats")
        self.assertEqual(response.status_code, 200, response.text)
        ordered = response.json()["symbols"]
        self.assertEqual([r["symbol"] for r in ordered], ["READY", "ZERO", "SMALL", "EMPTY", "EMPTY2"])
        self.assertIsNone(ordered[-1]["avg_r"])
        self.assertEqual(ordered[1]["avg_r"], 0.0)


if __name__ == "__main__":
    unittest.main()
