"""R09 hermético: sem rede, sem exchange, sem notifier, sem DB externo."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.dialects import postgresql
from services import decision_observation_service as obs

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp() * 1000)


def rec(**kw):
    return {"symbol": "BTC/USDT:USDT", "timeframe": "15m", "direction": "long",
            "tier": "A", "score": 80, "entry": 100, "stop_loss": 95, "tp2": 110,
            "signal": {"tp1": 105, "indicators": {"atr": 2},
                       "data_freshness": {"candle": {"close_time_ms": TS}}}, **kw}


def candle(ts=TS, **kw):
    return {"timestamp": ts, "open": 100, "high": 102, "low": 99,
            "close": 101, "volume": 1000, **kw}


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        obs._pending.clear()
        obs._windows.clear()
        obs._stats.clear()
        obs._flushing = False
        obs._wanted_symbols = None
        self.net = patch.object(socket, "getaddrinfo", side_effect=AssertionError("rede proibida"))
        self.net.start()

    def tearDown(self):
        self.net.stop()

    def staged(self, item):
        return obs._pending[item["_r09_attempt_id"]]

    def test_identity_retry_stable_attempt_is_new(self):
        item = rec()
        obs.begin_batch([item])
        key, first = self.staged(item)["opportunity_key"], item["_r09_attempt_id"]
        obs.begin_batch([item])
        self.assertEqual(key, self.staged(item)["opportunity_key"])
        self.assertNotEqual(first, item["_r09_attempt_id"])

    def test_actual_snapshot_id_preferred_not_latest_query(self):
        a, b = rec(snapshot_id=123), rec(snapshot_id=123, entry=101)
        self.assertEqual(obs.opportunity_identity(a), obs.opportunity_identity(b))
        self.assertEqual(obs.opportunity_identity(a)[1], "SNAPSHOT_ID")

    def test_no_identity_does_not_invent_timestamp(self):
        item = rec(signal={})
        obs.begin_batch([item])
        self.assertNotIn("_r09_attempt_id", item)
        self.assertEqual(obs._stats["identity_missing"], 1)

    def test_new_candle_or_new_setup_is_new_opportunity(self):
        a, b = rec(), rec(entry=101)
        self.assertNotEqual(obs.opportunity_identity(a), obs.opportunity_identity(b))
        b = rec()
        b["signal"]["data_freshness"]["candle"]["close_time_ms"] += 900000
        self.assertNotEqual(obs.opportunity_identity(a), obs.opportunity_identity(b))

    def test_freeze_allowlist_finite_and_immutable(self):
        item = rec(api_key="SECRET", score=float("nan"), signal={"tp1": 105, "secret": "SECRET"}, snapshot_id=1)
        before = deepcopy(item)
        obs.begin_batch([item], config={"score_min": 60, "secret": "SECRET"})
        frozen = self.staged(item)
        item["entry"] = 200
        self.assertEqual(frozen["frozen_setup"]["entry"], 100)
        self.assertIsNone(frozen["frozen_setup"]["score"])
        payload = json.dumps({"setup": frozen["frozen_setup"], "config": frozen["frozen_config"]}, allow_nan=False)
        self.assertNotIn("SECRET", payload)
        self.assertEqual(before["signal"], item["signal"])

    def test_first_blocker_preserved_outcomes_separate(self):
        item = rec()
        obs.begin_batch([item])
        obs.stage_decision(item, "REJECTED", "score-min")
        first = self.staged(item)["rejected_at"]
        obs.stage_decision(item, "REJECTED", "risk-budget")
        self.assertEqual(self.staged(item)["first_blocker"], "score-min")
        self.assertEqual(self.staged(item)["rejected_at"], first)
        self.assertNotIn("outcome", self.staged(item))

    def test_preflight_not_submitted_no_fill_and_unknown_distinct(self):
        item = rec()
        obs.begin_batch([item])
        obs.stage_result(item, {"preflight_failed": True, "entry_not_submitted": True,
                                "submitted_qty": 10, "reason_code": "EXEC_PRICE_STALE"})
        self.assertEqual(self.staged(item)["result"], "REJECTED")
        self.assertEqual(self.staged(item)["submit_evidence"], "NOT_SUBMITTED")
        obs.stage_result(item, {"no_fill": True, "submitted_qty": 10})
        self.assertEqual(self.staged(item)["result"], "NO_FILL")
        self.assertEqual(self.staged(item)["submit_evidence"], "UNKNOWN")
        obs.stage_result(item, {"ok": True, "result": {"orderId": 42}})
        self.assertEqual(self.staged(item)["submit_evidence"], "SUBMITTED")
        self.assertEqual(self.staged(item)["result"], "ATTEMPTED")

    def test_pending_hard_cap_and_telemetry(self):
        with patch.object(obs, "MAX_PENDING", 2):
            obs.begin_batch([rec(), rec(), rec()])
        self.assertEqual(len(obs._pending), 2)
        self.assertEqual(obs._stats["buffer_dropped"], 1)

    async def test_flush_never_drains_active_attempt(self):
        a, b = rec(), rec(snapshot_id=2)
        obs.begin_batch([a, b])
        obs.seal_batch([a])
        saved = AsyncMock()
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "_flush_batch", saved):
            await obs.flush_pending()
        self.assertEqual(len(saved.call_args.args[0]), 1)
        self.assertIn(b["_r09_attempt_id"], obs._pending)
        obs.stage_decision(b, "REJECTED", "late-gate")
        self.assertEqual(self.staged(b)["first_blocker"], "late-gate")

    async def test_flush_failure_is_fail_soft_and_counted(self):
        item = rec()
        obs.begin_batch([item])
        obs.seal_batch([item])
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "_flush_batch", AsyncMock(side_effect=RuntimeError("db"))):
            await obs.flush_pending()
        self.assertEqual(obs._stats["flush_errors"], 1)
        self.assertEqual(obs._stats["persistence_dropped"], 1)

    async def test_db_unavailable_is_not_false_zero(self):
        with patch.object(obs, "DB_ENABLED", False):
            status = await obs.get_status(999)
        self.assertEqual(status["state"], "UNAVAILABLE")
        self.assertIsNone(status["unique_opportunities"])
        self.assertEqual(status["days"], 30)
        self.assertFalse(status["rejected_shadow"]["economic_outcomes_exposed"])

    async def test_only_closed_original_bars_are_buffered(self):
        bars = [candle(TS - 300000), candle(TS), candle(TS + 300000)]
        await obs.observe_candles("BTC/USDT:USDT", bars, as_of=NOW)
        self.assertEqual([x["timestamp"] for x in obs._windows["BTC/USDT:USDT"]["candles"]], [TS - 300000])
        self.assertEqual(obs._stats["invalid_or_unclosed_candles"], 2)

    async def test_shared_windows_symbol_cap(self):
        bars = [candle(TS - 300000)]
        with patch.object(obs, "MAX_SYMBOLS", 1):
            await obs.observe_candles("BTC/USDT:USDT", bars, as_of=NOW)
            await obs.observe_candles("ETH/USDT:USDT", bars, as_of=NOW)
        self.assertEqual(len(obs._windows), 1)
        self.assertEqual(obs._stats["candle_symbols_dropped"], 1)

    async def test_empty_fetch_does_not_create_window(self):
        await obs.observe_candles("BTC/USDT:USDT", [], as_of=NOW)
        self.assertEqual(len(obs._windows), 0)

    async def test_wanted_symbols_filter_prevents_cap_bias(self):
        obs._wanted_symbols = {"ETH/USDT:USDT"}
        bars = [candle(TS - 300000)]
        with patch.object(obs, "MAX_SYMBOLS", 1):
            await obs.observe_candles("BTC/USDT:USDT", bars, as_of=NOW)
            await obs.observe_candles("ETH/USDT:USDT", bars, as_of=NOW)
        self.assertEqual(list(obs._windows), ["ETH/USDT:USDT"])
        self.assertEqual(obs._stats["candle_symbols_dropped"], 0)
        # veto novo passa a ser desejado antes mesmo do próximo refresh
        item = rec(symbol="SOL/USDT:USDT")
        obs.begin_batch([item])
        obs.stage_decision(item, "REJECTED", "score-min")
        self.assertIn("SOL/USDT:USDT", obs._wanted_symbols)

    async def test_inconsistent_ohlc_or_missing_volume_rejected(self):
        bars = [candle(TS - 900000, low=101.5), candle(TS - 600000, volume=None),
                candle(TS - 300000, high=float("inf")), candle(TS - 1200000, open=0)]
        await obs.observe_candles("BTC/USDT:USDT", bars, as_of=NOW)
        self.assertNotIn("BTC/USDT:USDT", obs._windows)
        self.assertEqual(obs._stats["invalid_or_unclosed_candles"], 4)

    def test_preflight_abort_stage_p04a_vs_p04b(self):
        maker, market, fallback = rec(), rec(snapshot_id=7), rec(snapshot_id=8)
        obs.begin_batch([maker, market, fallback])
        base = {"preflight_failed": True, "entry_not_submitted": True, "no_fill": True}
        obs.stage_result(maker, {**base, "was_maker": True, "fell_back_to_market": False,
                                 "reason_code": "EXEC_PRICE_STALE"})
        obs.stage_result(market, {**base, "reason_code": "EXEC_DEPTH_UNSUPPORTED"})
        obs.stage_result(fallback, {**base, "was_maker": True, "fell_back_to_market": True,
                                    "reason_code": "bad code with spaces"})
        self.assertEqual(self.staged(maker)["first_blocker"], "P04A_MAKER:EXEC_PRICE_STALE")
        self.assertEqual(self.staged(market)["first_blocker"], "P04B_MARKET:EXEC_DEPTH_UNSUPPORTED")
        self.assertEqual(self.staged(fallback)["first_blocker"], "P04B_MARKET:ENTRY_PREFLIGHT")
        for item in (maker, market, fallback):
            self.assertEqual(self.staged(item)["result"], "REJECTED")
            self.assertEqual(self.staged(item)["submit_evidence"], "NOT_SUBMITTED")

    def sealed(self, n=1):
        items = [rec(snapshot_id=100 + i) for i in range(n)]
        obs.begin_batch(items)
        ids = [item["_r09_attempt_id"] for item in items]
        obs.seal_batch(items)
        return ids

    def test_seal_removes_private_handles_from_rec(self):
        item = rec(snapshot_id=5)
        obs.begin_batch([item])
        item["_r09_score_trace"] = {"version": "r08b.1"}
        attempt = item["_r09_attempt_id"]
        obs.seal_batch([item])
        self.assertNotIn("_r09_attempt_id", item)
        self.assertNotIn("_r09_score_trace", item)
        self.assertTrue(obs._pending[attempt]["sealed"])
        obs.stage_decision(item, "REJECTED", "late")   # sem handle: no-op
        self.assertIsNone(obs._pending[attempt]["first_blocker"])

    def test_full_buffer_evicts_only_stale_unsealed(self):
        with patch.object(obs, "MAX_PENDING", 2):
            old, fresh = rec(snapshot_id=1), rec(snapshot_id=2)
            obs.begin_batch([old, fresh])
            obs._pending[old["_r09_attempt_id"]]["observed_at"] -= timedelta(seconds=obs.STALE_UNSEALED_S + 1)
            newer = rec(snapshot_id=3)
            obs.begin_batch([newer])
            self.assertNotIn(old["_r09_attempt_id"], obs._pending)
            self.assertIn(fresh["_r09_attempt_id"], obs._pending)
            self.assertIn(newer["_r09_attempt_id"], obs._pending)
            self.assertEqual(obs._stats["stale_unsealed_evicted"], 1)
            # nada velho: descarta a nova, nunca uma tentativa ativa recente
            obs.begin_batch([rec(snapshot_id=4)])
            self.assertEqual(obs._stats["buffer_dropped"], 1)
            self.assertIn(fresh["_r09_attempt_id"], obs._pending)

    async def test_flush_timeout_is_counted_not_hidden(self):
        self.sealed()
        async def slow(batch, windows, progress=None):
            await asyncio.sleep(5)
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "FLUSH_TIMEOUT_S", 0.01), \
                patch.object(obs, "_flush_batch", slow):
            await obs.flush_pending()
        self.assertEqual(obs._stats["flush_timeouts"], 1)
        self.assertEqual(obs._stats["persistence_dropped"], 1)
        self.assertFalse(obs._flushing)

    async def test_flush_cancellation_is_counted_and_propagated(self):
        self.sealed(2)
        started = asyncio.Event()
        async def hang(batch, windows, progress=None):
            started.set()
            await asyncio.sleep(60)
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "_flush_batch", hang):
            task = asyncio.create_task(obs.flush_pending())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(obs._stats["flush_cancelled"], 1)
        self.assertEqual(obs._stats["persistence_dropped"], 2)
        self.assertFalse(obs._flushing)

    async def test_contention_requeues_bounded_then_drops(self):
        ids = self.sealed()
        async def busy(batch, windows, progress=None):
            progress.update(admission="CONTENTION", admission_committed=True)
            return "CONTENTION"
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "_flush_batch", busy):
            for _ in range(obs.MAX_ADMISSION_RETRIES):
                await obs.flush_pending()
                self.assertIn(ids[0], obs._pending)
            await obs.flush_pending()
        self.assertNotIn(ids[0], obs._pending)
        self.assertEqual(obs._stats["contention_requeued"], obs.MAX_ADMISSION_RETRIES)
        self.assertEqual(obs._stats["contention_dropped"], 1)
        self.assertEqual(obs._stats["persistence_dropped"], 0)

    async def test_resolver_failure_after_committed_admission_loses_nothing(self):
        self.sealed()
        async def partial(batch, windows, progress=None):
            progress.update(admission="ADMITTED", admission_committed=True)
            raise RuntimeError("resolver")
        with patch.object(obs, "DB_ENABLED", True), patch.object(obs, "_flush_batch", partial):
            await obs.flush_pending()
        self.assertEqual(obs._stats["flush_errors"], 1)
        self.assertEqual(obs._stats["persistence_dropped"], 0)
        self.assertEqual(len(obs._pending), 0)

    def test_capacity_view_states(self):
        limits = obs.CAPACITY
        self.assertEqual(obs._capacity_view(None)["state"], "UNKNOWN")
        ok = obs._capacity_view({k: 0 for k in limits})
        self.assertEqual((ok["state"], ok["admission_blocked"]), ("OK", False))
        near = obs._capacity_view({**{k: 0 for k in limits}, "attempts": int(limits["attempts"] * 0.95)})
        self.assertEqual(near["state"], "NEAR_LIMIT")
        full = obs._capacity_view({**{k: 0 for k in limits}, "rejected": limits["rejected"]})
        self.assertEqual((full["state"], full["admission_blocked"]), ("AT_LIMIT", True))
        self.assertEqual(full["policy"], "DROP_NEW_KEEP_RESOLVING")

    def test_capacity_constants_fixed_and_lock_distinct_from_risk(self):
        self.assertEqual(obs.CAPACITY, {"opportunities": 50_000, "attempts": 100_000,
                                        "rejected": 50_000})
        risk_source = (Path(obs.__file__).parent / "risk_service.py").read_text()
        self.assertIn("_P03_PAUSE_LOCK = 917283", risk_source)
        self.assertNotEqual(obs.R09_ADVISORY_LOCK_KEY, 917283)
        self.assertNotIn("getenv", Path(obs.__file__).read_text())

    def test_upsert_does_not_replace_frozen_or_first_fields(self):
        item = rec()
        obs.begin_batch([item])
        obs.stage_decision(item, "REJECTED", "score-min")
        sql = str(obs._opportunity_upsert(self.staged(item)).compile(dialect=postgresql.dialect()))
        update_clause = sql.split("DO UPDATE SET")[1]
        self.assertIn("coalesce(decision_observations.first_blocker", update_clause)
        self.assertIn("coalesce(decision_observations.first_decision_observed_at", update_clause)
        self.assertIn("least(decision_observations.first_seen_at", update_clause)
        self.assertNotIn("frozen_setup =", update_clause)
        self.assertNotIn("frozen_config =", update_clause)
        self.assertNotIn("score_trace =", update_clause)

    def test_no_learner_risk_trade_or_network_dependencies(self):
        source = Path(obs.__file__).read_text()
        for forbidden in ("exchange_service", "risk_service", "real_trade_service",
                          "notify_outcome", "create_task(", "calibration_service"):
            self.assertNotIn(forbidden, source)

    async def test_hooks_do_not_repurpose_legacy_skip_event_counter(self):
        from services import shadow_trade_service as shadow
        item = rec()
        obs.begin_batch([item])
        with patch.object(shadow, "_schedule_skip_persist") as persist:
            shadow._record_skip(item, "score-min", "score baixo")
            shadow._record_skip(item, "score-min", "retry")
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(len(obs._pending), 1)

    async def test_status_exposes_capacity_and_semantics_not_economics(self):
        with patch.object(obs, "DB_ENABLED", False):
            status = await obs.get_status(7)
        self.assertEqual(status["order_semantics"]["first_decision"], "FIRST_PERSISTED_ATTEMPT")
        self.assertEqual(status["capacity"]["limits"], obs.CAPACITY)
        text = json.dumps(status, allow_nan=False)
        for economic in ("gross_r", "net_r", "pnl", "profit", "win_rate", "expectancy"):
            self.assertNotIn(economic, text)

    def test_model_is_strictly_segregated(self):
        for model in (obs.OpportunityRow, obs.AttemptRow, obs.RejectedRow):
            self.assertFalse(model.__table__.foreign_keys)
            self.assertNotEqual(model.__tablename__, "recommendation_snapshots")
            self.assertNotEqual(model.__tablename__, "real_trades")


class RejectedReplayMapTests(unittest.TestCase):
    """Chama o replay R10 REAL. Nenhum status fica sem mapa explícito."""

    DECISION = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    FIRST = int(DECISION.timestamp() * 1000)

    def row(self, **setup):
        frozen = {"direction": "long", "entry": 100.0, "stop_loss": 95.0,
                  "tp1": 105.0, "tp2": 110.0, "atr": 2.0, **setup}
        return SimpleNamespace(opportunity_key="k" * 64, symbol="BTC/USDT:USDT",
                               decision_at=self.DECISION, frozen_setup=frozen,
                               frozen_config=dict(obs._CONFIG), candles=[])

    def bars(self, specs, start=0):
        out = []
        for i, (o, h, l, c) in enumerate(specs):
            out.append({"timestamp": self.FIRST + (start + i) * obs.BAR_MS, "open": o,
                        "high": h, "low": l, "close": c, "volume": 10.0})
        return out

    def run_replay(self, candles, row=None, as_of_bars=None):
        last = candles[-1]["timestamp"] if candles else self.FIRST
        as_of_ms = last + obs.BAR_MS if as_of_bars is None else self.FIRST + as_of_bars * obs.BAR_MS
        shared = {"candles": candles, "as_of": datetime.fromtimestamp(as_of_ms / 1000, timezone.utc)}
        return obs._replay_rejected(row or self.row(), shared)

    def test_map_covers_exact_engine_vocabulary(self):
        from services import offline_replay_service as replay
        self.assertEqual(set(obs.REPLAY_COVERAGE), set(replay.REPLAY_STATUSES))
        mapped = set(obs.REPLAY_COVERAGE.values()) - {"GAP", "PENDING"}
        self.assertTrue(mapped <= set(obs.TERMINAL_COVERAGE))
        self.assertFalse(set(obs.TERMINAL_COVERAGE) & set(obs.OPEN_COVERAGE))
        for status, coverage in obs.REPLAY_COVERAGE.items():
            if status in replay.CLOSED_STATUSES:
                self.assertEqual(coverage, "RESOLVED")
            else:
                self.assertNotEqual(coverage, "RESOLVED", status)

    def test_stop_resolves_with_gross_but_no_net(self):
        _, coverage, outcome = self.run_replay(self.bars([(100, 101, 99, 100), (99, 99.5, 94, 95)]))
        self.assertEqual((coverage, outcome["status"]), ("RESOLVED", "CLOSED_STOP"))
        self.assertEqual(outcome["gross_r"], -1.0)
        self.assertIsNone(outcome["net_r"])
        self.assertEqual(outcome["cost_status"], "UNKNOWN")
        self.assertFalse(outcome["learning_eligible"])

    def test_tp1_then_tp2_resolves(self):
        _, coverage, outcome = self.run_replay(self.bars([(100, 101, 99.5, 100.5), (101, 106, 100.5, 105.5),
                                                         (106, 111, 105.5, 110)]))
        self.assertEqual((coverage, outcome["status"]), ("RESOLVED", "CLOSED_TP2"))
        self.assertAlmostEqual(outcome["gross_r"], 0.45 * 1 + 0.55 * 2)

    def test_not_filled_is_terminal_without_r(self):
        _, coverage, outcome = self.run_replay(self.bars([(102, 103, 101, 102)] * 3))
        self.assertEqual((coverage, outcome["status"]), ("NOT_FILLED", "NOT_FILLED"))
        self.assertIsNone(outcome["gross_r"])

    def test_ambiguous_entry_never_resolved(self):
        _, coverage, outcome = self.run_replay(self.bars([(102, 103, 94, 97)]))
        self.assertEqual((coverage, outcome["status"]), ("AMBIGUOUS", "AMBIGUOUS_ENTRY_BAR"))
        self.assertIsNone(outcome["gross_r"])

    def test_gap_pending_then_final_never_resolved(self):
        candles = self.bars([(100, 101, 99, 100)]) + self.bars([(100, 101, 99, 100)], start=2)
        _, coverage, outcome = self.run_replay(candles)
        self.assertEqual((coverage, outcome["status"]), ("GAP_PENDING", "MISSING_OR_UNORDERED_BARS"))
        self.assertIsNone(outcome["gross_r"])
        _, coverage, _ = self.run_replay(candles, as_of_bars=1 + obs.RESOLVER_LOOKBACK_BARS + 1)
        self.assertEqual(coverage, "DATA_GAP_FINAL")

    def test_missing_first_bar_is_gap(self):
        _, coverage, outcome = self.run_replay(self.bars([(100, 101, 99, 100)], start=1))
        self.assertEqual(coverage, "GAP_PENDING")
        self.assertEqual(outcome["bars_observed"], 0)

    def test_incomplete_horizon_pending_then_expired(self):
        candles = self.bars([(100, 101, 99, 100)] * 2)
        _, coverage, outcome = self.run_replay(candles)
        self.assertEqual((coverage, outcome["status"]), ("PENDING", "INSUFFICIENT_DATA"))
        self.assertIsNone(outcome["gross_r"])
        _, coverage, _ = self.run_replay(candles, as_of_bars=2 + obs.RESOLVER_LOOKBACK_BARS + 1)
        self.assertEqual(coverage, "EXPIRED_INCOMPLETE")

    def test_invalid_setup_is_terminal_not_retried(self):
        for bad in ({"tp1": None}, {"direction": "LONG"}, {"stop_loss": 101.0}, {"entry": True}):
            _, coverage, outcome = self.run_replay(self.bars([(100, 101, 99, 100)]), row=self.row(**bad))
            self.assertEqual(coverage, "INVALID", bad)
            self.assertIn("INVALID_FROZEN_SETUP", outcome["reason_codes"])

    def test_no_shared_source_is_unavailable(self):
        candles, coverage, outcome = self.run_replay([])
        self.assertEqual((candles, coverage, outcome), ([], "UNAVAILABLE", None))

    def test_pre_decision_bars_are_ignored(self):
        candles, _, _ = self.run_replay(self.bars([(100, 101, 99, 100)], start=-2)
                                        + self.bars([(100, 101, 99, 100)]))
        self.assertEqual([c["timestamp"] for c in candles], [self.FIRST])

    def test_unmapped_engine_status_is_invalid_terminal(self):
        from services import offline_replay_service as replay
        with patch.object(replay, "replay_opportunity", return_value={"status": "NEW_STATUS"}):
            _, coverage, outcome = self.run_replay(self.bars([(100, 101, 99, 100)]))
        self.assertEqual(coverage, "INVALID")
        self.assertIn("UNMAPPED_REPLAY_STATUS", outcome["reason_codes"])
        self.assertIsNone(outcome["status"])


if __name__ == "__main__":
    unittest.main()
