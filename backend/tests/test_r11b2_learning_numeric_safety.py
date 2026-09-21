"""R11B2 — integridade numérica do aprendizado e do multiplicador por moeda.

Corrige A4 (NaN virava amplificação de size), A5 (R desconhecido bloqueava
bucket) e M2 (None/NaN distorciam a rotação). Dado inválido deixa de
contribuir; dado válido preserva fórmula, limite e arredondamento.

Serviços REAIS com sessões/linhas sintéticas; sem rede, banco, exchange ou
notificação. Caches, ENV e chaves de `sys.modules` restaurados por teste.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket as _socket
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R11B2")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import learning_service as ls            # noqa: E402
from services import rotation_service as rot           # noqa: E402
from services import symbol_learning_service as sls    # noqa: E402

INVALID_R = [None, float("nan"), float("inf"), float("-inf"), True, False, "1.0", "", [1], {"r": 1}]
_MISSING = object()


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, handler):
        self.handler, self.statements, self.added, self.commits = handler, [], [], 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self.handler(stmt))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def session_factory(handler):
    session = _Session(handler if callable(handler) else (lambda stmt: handler))
    return (lambda: session), session


def snap(r=1.0, symbol="BTC/USDT:USDT", tier="A", tf="4h", direction="long", features=None):
    return NS(symbol=symbol, realized_r=r, tier=tier, timeframe=tf, direction=direction,
              features=features or {}, status="won_tp1")


def learned(tf="4h", mult=1.10, conf=0.9, **over):
    row = {"base": "AAA", "timeframe": tf, "size_quality_mult": mult, "confidence": conf,
           "source": "backtest_history", "n_trades": 40, "wf_avg_r": 0.4, "wf_n_trades": 20,
           "expiry_pct": 10.0, "calibrated_edge": 0.28, "params": {}, "learned_at": None}
    row.update(over)
    return row


def backtest_row(symbol="AAA/USDT:USDT", tf="4h", **over):
    data = {"n_trades": 50, "wf_avg_r": 0.6, "wf_n_trades": 20, "expiry_pct": 10.0}
    data.update(over)
    return NS(error=None, symbol=symbol, timeframe=tf, to_dict=lambda d=data: dict(d))


class SafetyCase(unittest.IsolatedAsyncioTestCase):
    def install_module(self, name, module):
        previous = sys.modules.get(name, _MISSING)
        sys.modules[name] = module

        def restore():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        self.addCleanup(restore)

    def setUp(self):
        saved = (dict(ls._cache), dict(sls._CACHE), sls._CACHE_LOADED)
        notifier = types.ModuleType("services.notification_service")
        notifier.send_telegram = AsyncMock(side_effect=AssertionError("notificação real proibida"))
        notifier.fmt_symbol_rerank = Mock(return_value="x")
        self.notifier = notifier
        self.install_module("services.notification_service", notifier)

        def restore():
            cache, scache, loaded = saved
            ls._cache.clear()
            ls._cache.update(cache)
            sls._CACHE, sls._CACHE_LOADED = scache, loaded
        self.addCleanup(restore)
        for p in (patch.dict(os.environ, {}, clear=False),
                  patch("db.get_session", Mock(side_effect=AssertionError("banco real proibido"))),
                  patch.object(ls, "get_session", Mock(side_effect=AssertionError("banco real proibido"))),
                  patch.object(sls, "get_session", Mock(side_effect=AssertionError("banco real proibido")))):
            p.start()
            self.addCleanup(p.stop)
        ls.invalidate_cache()

    async def symbol_stats(self, rows, days=0):
        factory, session = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.compute_symbol_stats(days=days), session

    async def bucket_stats(self, rows, days=0):
        factory, session = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.compute_stats_by_bucket(days=days), session

    async def lookup_one(self, rows, tier="A", tf="4h", direction="long"):
        factory, _ = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.lookup_historical_for(tier, tf, direction, days=0)

    async def lookup_many(self, rows, keys):
        factory, _ = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.lookup_historical_batch(keys, days=0)


# ── A4: acessor do multiplicador por moeda ─────────────────────────────────
class LearnedMultiplierAccessor(SafetyCase):
    def mult(self, cache, symbol="AAA/USDT:USDT", tf="4h", **flags):
        values = dict(SYMBOL_LEARNING_SIZE_ENABLED=True, MIN_CONFIDENCE_APPLY=0.25,
                      SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15)
        values.update(flags)
        with patch.object(sls, "_CACHE", cache), patch.multiple(sls, **values):
            return sls.get_size_mult(symbol, tf)

    def test_invalid_multiplier_never_amplifies(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, False, "1.15", None, 0.0, -0.5, [1.1]):
            with self.subTest(bad=bad):
                value, reason = self.mult({"AAA": {"4h": learned(mult=bad)}})
                self.assertEqual(value, 1.0)
                self.assertIsInstance(reason, str)
                self.assertNotIn("nan", reason.lower())
                self.assertNotIn("inf", reason.lower())

    def test_invalid_confidence_never_applies(self):
        for bad in (float("nan"), float("inf"), True, "0.9", None, -0.1, 1.5, {"c": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.mult({"AAA": {"4h": learned(conf=bad)}})[0], 1.0)

    def test_exact_timeframe_invalid_does_not_fall_back(self):
        cache = {"AAA": {"4h": learned(mult=float("nan")), "1h": learned(tf="1h", mult=1.15, conf=0.99)}}
        self.assertEqual(self.mult(cache, tf="4h")[0], 1.0)
        self.assertEqual(self.mult(cache, tf="1h")[0], 1.15)

    def test_fallback_skips_invalid_and_keeps_tie_order(self):
        cache = {"AAA": {"1h": learned(tf="1h", mult=float("inf"), conf=0.99),
                         "4h": learned(tf="4h", mult=1.12, conf=0.80),
                         "12h": learned(tf="12h", mult=0.80, conf=0.80)}}
        self.assertEqual(self.mult(cache, tf="15m")[0], 1.12)   # empate: primeira válida
        only_invalid = {"AAA": {"1h": learned(tf="1h", mult="x"), "4h": learned(tf="4h", conf=float("nan"))}}
        self.assertEqual(self.mult(only_invalid, tf="15m")[0], 1.0)
        self.assertEqual(self.mult(only_invalid, tf=None)[0], 1.0)

    def test_valid_rows_keep_existing_behaviour(self):
        self.assertEqual(self.mult({"AAA": {"4h": learned(mult=1.10, conf=0.25)}})[0], 1.10)
        self.assertEqual(self.mult({"AAA": {"4h": learned(mult=1.10, conf=0.2499)}})[0], 1.0)
        self.assertEqual(self.mult({"AAA": {"4h": learned(mult=2.0)}})[0], 1.15)      # clamp antigo
        self.assertEqual(self.mult({"AAA": {"4h": learned(mult=0.10)}})[0], 0.75)     # clamp antigo
        self.assertEqual(self.mult({"AAA": {"4h": learned(mult=1.0)}}), (1.0, "neutro"))
        self.assertEqual(self.mult({"AAA": {"4h": learned()}}, SYMBOL_LEARNING_SIZE_ENABLED=False),
                         (1.0, "off"))
        self.assertEqual(self.mult({}, tf="4h")[0], 1.0)

    def test_legacy_row_without_optional_metadata_still_applies(self):
        legacy = {"timeframe": "4h", "size_quality_mult": 1.10, "confidence": 0.9}
        self.assertEqual(self.mult({"AAA": {"4h": legacy}})[0], 1.10)
        absent = learned(n_trades=None, wf_avg_r=None, wf_n_trades=None,
                         expiry_pct=None, calibrated_edge=None)
        self.assertEqual(self.mult({"AAA": {"4h": absent}})[0], 1.10)

    def test_invalid_optional_metadata_disqualifies_the_row(self):
        for field, bad in (("calibrated_edge", float("nan")), ("wf_avg_r", "x"),
                           ("n_trades", -3), ("expiry_pct", 150.0), ("wf_n_trades", 2.5)):
            with self.subTest(field=field):
                self.assertEqual(self.mult({"AAA": {"4h": learned(**{field: bad})}})[0], 1.0)

    def test_broken_numeric_limits_are_a_noop(self):
        for limits in ({"SIZE_MULT_MIN": float("nan")}, {"SIZE_MULT_MAX": float("inf")},
                       {"SIZE_MULT_MIN": 1.2, "SIZE_MULT_MAX": 1.0}, {"MIN_CONFIDENCE_APPLY": float("nan")},
                       {"SIZE_MULT_MIN": 0.0}):
            with self.subTest(limits=limits):
                value, reason = self.mult({"AAA": {"4h": learned()}}, **limits)
                self.assertEqual(value, 1.0)
                self.assertNotIn("nan", reason.lower())

    async def test_refresh_keeps_invalid_row_and_accessor_protects(self):
        rows = [NS(base="AAA", timeframe="4h",
                   to_dict=lambda: learned(mult=float("nan"))),
                NS(base="AAA", timeframe="1h",
                   to_dict=lambda: learned(tf="1h", mult=1.15, conf=0.99))]
        factory, _ = session_factory(rows)
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", factory):
            self.assertEqual(await sls.refresh_cache(), 2)
        self.assertIn("4h", sls._CACHE["AAA"])      # chave exata continua existindo
        with patch.multiple(sls, SYMBOL_LEARNING_SIZE_ENABLED=True, MIN_CONFIDENCE_APPLY=0.25,
                            SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15):
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "4h")[0], 1.0)
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "1h")[0], 1.15)

    async def test_status_serialises_corrupted_row_safely(self):
        row = NS(to_dict=lambda: learned(mult=float("nan"), calibrated_edge=float("inf")))
        factory, _ = session_factory([row])
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", factory):
            report = await sls.status()
        learned_rows = report["learned"]
        self.assertEqual(len(learned_rows), 1)
        self.assertIsNone(learned_rows[0]["size_quality_mult"])
        self.assertIsNone(learned_rows[0]["calibrated_edge"])
        self.assertEqual(sorted(learned_rows[0]["invalid_fields"]), ["calibrated_edge", "size_quality_mult"])
        json.dumps(report, allow_nan=False)
        self.assertTrue(math.isnan(row.to_dict()["size_quality_mult"]))   # original intocado


# ── A4 (origem): derivação e relearn ───────────────────────────────────────
class DerivationAndRelearn(SafetyCase):
    def test_eligibility_rejects_invalid_and_keeps_legacy_defaults(self):
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": "nan"}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": True}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": float("inf")}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": "40", "wf_avg_r": 0.5}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 29.5, "wf_avg_r": 0.5}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": -40, "wf_avg_r": 0.5}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": 0.5, "expiry_pct": 150}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": 0.5, "wf_n_trades": "12"}))
        self.assertIsNone(sls._eligible_metrics(None))
        legacy = sls._eligible_metrics({"n_trades": 40.0, "wf_avg_r": 0.5})
        self.assertEqual((legacy["n"], legacy["wf_n"], legacy["expiry"]), (40, 0, 0.0))
        negative = sls._eligible_metrics({"n_trades": 40, "wf_avg_r": -0.3})
        self.assertEqual(negative["wf"], -0.3)          # sem piso de edge positiva
        zero = sls._eligible_metrics({"n_trades": 40, "wf_avg_r": 0.0})
        self.assertEqual((zero["wf"], zero["calib"]), (0.0, 0.0))

    def test_derive_params_validates_percentile_and_outputs(self):
        for bad in (float("nan"), 1.5, -0.01, "0.5", True, None):
            with self.subTest(bad=bad):
                self.assertIsNone(sls.derive_params({"n_trades": 40, "wf_avg_r": 0.5}, bad))
        self.assertIsNone(sls.derive_params({"n_trades": 40, "wf_avg_r": "nan"}, 0.5))
        good = sls.derive_params({"n_trades": 60, "wf_avg_r": 0.4, "wf_n_trades": 20}, 0.5)
        self.assertEqual(good["confidence"], round(0.5 * (0.5 + 0.5 * 0.5), 3))
        self.assertEqual(good["calibrated_edge"], round(0.4 * sls.CALIB_FACTOR, 4))
        self.assertEqual(good["size_quality_mult"], 1.0)
        json.dumps(good, allow_nan=False)

    async def test_relearn_ignores_invalid_rows_and_ranks_only_valid(self):
        from models.symbol_learned_params import SymbolLearnedParams
        rows = [
            backtest_row("AAA/USDT:USDT", "4h", wf_avg_r=0.6),
            backtest_row("AAA/USDT:USDT", "1h", wf_avg_r="nan"),      # inválida
            backtest_row("BBB/USDT:USDT", "4h", wf_avg_r=0.2),
            backtest_row("CCC/USDT:USDT", "4h", n_trades=5, wf_avg_r=0.9),   # amostra pequena
            backtest_row("DDD/USDT:USDT", "4h", wf_avg_r=float("inf")),      # inválida
        ]
        stale = SymbolLearnedParams(base="AAA", timeframe="4h",
                                    size_quality_mult=float("nan"), confidence=0.9)
        existing = {("AAA", "4h"): stale}

        def handler(stmt):
            if "symbol_backtest_stats" in str(stmt):
                return rows
            params = stmt.compile().params
            key = (params["base_1"], params["timeframe_1"])
            return [existing[key]] if key in existing else []
        factory, session = session_factory(handler)
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", factory), \
                patch.object(sls, "refresh_cache", AsyncMock(return_value=0)), \
                patch.object(sls, "SYMBOL_LEARNING_NOTIFY", False):
            summary = await sls.relearn_all_from_history()
        self.assertEqual(summary["scanned"], 5)
        self.assertEqual(summary["learned"], 2)                 # AAA e BBB
        self.assertEqual(summary["skipped_small"], 1)           # CCC
        self.assertEqual(summary["skipped_invalid"], 2)         # 1h da AAA e DDD
        self.assertEqual(summary["bases"], 2)
        self.assertNotIn("error", summary)
        # percentis só com as válidas: AAA (melhor) no topo, BBB no fundo
        # Universo de 2 bases válidas: percentis 0.75 (AAA) e 0.25 (BBB) —
        # mesma fórmula de antes, calculada sem as linhas inválidas.
        self.assertEqual(stale.size_quality_mult, 1.0562)
        self.assertEqual(session.added[0].base, "BBB")
        self.assertEqual(session.added[0].size_quality_mult, 0.9062)
        self.assertEqual(session.commits, 1)

    async def test_relearn_invalid_previous_value_is_not_a_delta(self):
        from models.symbol_learned_params import SymbolLearnedParams
        captured = {}

        def fake_fmt(summary, ups, downs, news):
            captured.update(ups=ups, downs=downs, news=news)
            return "resumo"
        stale = SymbolLearnedParams(base="AAA", timeframe="4h",
                                    size_quality_mult=float("nan"), confidence=0.5)

        def handler(stmt):
            if "symbol_backtest_stats" in str(stmt):
                return [backtest_row("AAA/USDT:USDT", "4h")]
            return [stale]
        factory, _ = session_factory(handler)
        self.notifier.send_telegram = AsyncMock(return_value=True)
        self.notifier.fmt_symbol_rerank = fake_fmt
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", factory), \
                patch.object(sls, "refresh_cache", AsyncMock(return_value=0)), \
                patch.object(sls, "SYMBOL_LEARNING_NOTIFY", True):
            await sls.relearn_all_from_history()
        self.assertEqual(captured["ups"], [])
        self.assertEqual(captured["downs"], [])
        self.assertEqual([c["base"] for c in captured["news"]], ["AAA"])
        self.assertIsNone(captured["news"][0]["old"])


# ── A5/M2: estatísticas que alimentam ajuste, bloqueio e rotação ───────────
class StatisticsExcludeInvalidEvidence(SafetyCase):
    async def test_missing_results_never_block_a_bucket(self):
        stats, _ = await self.bucket_stats([snap(r=None) for _ in range(30)])
        self.assertEqual(stats["total_trades"], 0)
        quality = stats["data_quality"]
        self.assertEqual((quality["total_raw"], quality["total_valid"]), (30, 0))
        self.assertEqual(quality["excluded_by_reason"]["ausente"], 30)
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)), \
                patch.multiple(ls, AUTO_BLOCK_ENABLED=True, AUTO_ADJUST_ENABLED=True):
            adjustments = await ls.compute_auto_adjustments()
        self.assertEqual(adjustments["blocked_buckets"], [])
        sig = NS(timeframe="4h", timestamp=None, patterns=[], derivatives=None)
        self.assertFalse(ls.apply_score_adjustment(sig, 80.0, adjustments, "A")["blocked"])

    async def test_sample_counts_only_valid_results(self):
        stats, _ = await self.symbol_stats([snap(r=1.0)] * 6 + [snap(r=None)] * 6)
        self.assertEqual(stats["BTC"]["trades"], 6)
        self.assertEqual(stats["BTC"]["avg_r"], 1.0)
        self.assertEqual(stats["BTC"]["verdict"], "amostra_pequena")
        self.assertEqual(stats["BTC"]["data_quality"]["excluded_total"], 6)

        eleven, _ = await self.symbol_stats([snap(r=-1.0)] * 11 + [snap(r=float("nan"))])
        self.assertEqual(eleven["BTC"]["trades"], 11)
        self.assertEqual(eleven["BTC"]["verdict"], "amostra_pequena")

        twelve, _ = await self.symbol_stats([snap(r=-1.0)] * 12 + [snap(r=float("nan"))])
        self.assertEqual(twelve["BTC"]["trades"], 12)
        self.assertEqual(twelve["BTC"]["avg_r"], -1.0)
        self.assertEqual(twelve["BTC"]["verdict"], "demote")

    async def test_group_without_valid_rows_is_absence_not_zero(self):
        stats, _ = await self.symbol_stats([snap(r=float("nan"))] * 20)
        btc = stats["BTC"]
        self.assertEqual(btc["trades"], 0)
        self.assertIsNone(btc["avg_r"])
        self.assertIsNone(btc["win_rate"])
        self.assertIsNone(btc["total_r"])
        self.assertFalse(btc["sample_ok"])
        self.assertEqual(btc["verdict"], "amostra_pequena")
        self.assertEqual(btc["data_quality"]["total_raw"], 20)
        json.dumps(stats, allow_nan=False)

    async def test_true_zero_stays_in_the_sample(self):
        stats, _ = await self.symbol_stats([snap(r=0.0)] * 12)
        self.assertEqual(stats["BTC"]["trades"], 12)
        self.assertEqual(stats["BTC"]["avg_r"], 0.0)
        self.assertEqual(stats["BTC"]["wins"], 0)
        self.assertEqual(stats["BTC"]["losses"], 12)     # convenção preexistente
        self.assertEqual(stats["BTC"]["verdict"], "neutro")
        self.assertEqual(stats["BTC"]["data_quality"]["excluded_total"], 0)
        buckets, _ = await self.bucket_stats([snap(r=0.0)] * 12)
        self.assertEqual(buckets["by_tier"]["A"]["trades"], 12)
        self.assertEqual(buckets["by_tier"]["A"]["losses"], 0)   # convenção preexistente

    async def test_every_read_path_partitions_the_same_way(self):
        rows = ([snap(r=1.0)] * 3 + [snap(r=-1.0)] * 2
                + [snap(r=v) for v in (None, float("nan"), float("inf"), True, "1.0")])
        buckets, _ = await self.bucket_stats(list(rows))
        symbols, _ = await self.symbol_stats(list(rows))
        single = await self.lookup_one(list(rows))
        batch = await self.lookup_many(list(rows), [{"tier": "A", "timeframe": "4h", "direction": "long"}])
        key = "A_4h_long"
        self.assertEqual(buckets["total_trades"], 5)
        self.assertEqual(symbols["BTC"]["trades"], 5)
        self.assertEqual(single["trades"], 5)
        self.assertEqual(batch[key]["trades"], 5)
        self.assertEqual(single["avg_r"], batch[key]["avg_r"])
        self.assertEqual(single["win_rate"], batch[key]["win_rate"])
        for payload in (buckets["data_quality"], symbols["BTC"]["data_quality"],
                        single["data_quality"], batch[key]["data_quality"]):
            self.assertEqual(payload["total_raw"], 10)
            self.assertEqual(payload["total_valid"], 5)
            self.assertEqual(payload["excluded_total"], 5)
            self.assertEqual(payload["excluded_by_reason"],
                             {"ausente": 1, "tipo_invalido": 2, "nao_finito": 2})
            self.assertEqual(payload["total_valid"] + payload["excluded_total"], payload["total_raw"])

    async def test_empty_input_differs_from_all_invalid(self):
        empty, _ = await self.bucket_stats([])
        self.assertEqual(empty["total_trades"], 0)
        self.assertEqual(empty["data_quality"]["total_raw"], 0)
        ls.invalidate_cache()          # mesma janela ⇒ mesma chave de cache
        invalid, _ = await self.bucket_stats([snap(r=None)] * 4)
        self.assertEqual(invalid["total_trades"], 0)
        self.assertEqual(invalid["data_quality"]["total_raw"], 4)
        self.assertEqual(await self.lookup_one([]), await self.lookup_one([]))
        missing_group = await self.lookup_many([], [{"tier": "A", "timeframe": "1h", "direction": "short"}])
        self.assertEqual(missing_group["A_1h_short"]["trades"], 0)
        self.assertEqual(missing_group["A_1h_short"]["data_quality"]["total_raw"], 0)

    async def test_valid_evidence_keeps_existing_decisions(self):
        blocked_stats, _ = await self.bucket_stats([snap(r=-1.0) for _ in range(30)])
        self.assertEqual(blocked_stats["by_tier_timeframe"]["A_4h"]["win_rate"], 0.0)
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=blocked_stats)), \
                patch.multiple(ls, AUTO_BLOCK_ENABLED=True, AUTO_ADJUST_ENABLED=True,
                               MIN_SAMPLE_BLOCK=30, BLOCK_WR_MAX=30.0):
            adjustments = await ls.compute_auto_adjustments()
        self.assertEqual([b["key"] for b in adjustments["blocked_buckets"]], ["A_4h"])
        sig = NS(timeframe="4h", timestamp=None, patterns=[], derivatives=None)
        self.assertTrue(ls.apply_score_adjustment(sig, 80.0, adjustments, "A")["blocked"])

    async def test_adjustment_bands_unchanged_for_valid_stats(self):
        def bucket(n, wr):
            return {"trades": n, "win_rate": wr}
        stats = {"enabled": True, "total_trades": 200,
                 "by_tier_timeframe": {"blk": bucket(30, 30.0), "boost": bucket(20, 82.5),
                                       "pen": bucket(20, 40.0), "mid": bucket(40, 55.0)},
                 "by_pattern": {}, "by_session": {}, "by_day_of_week": {}, "by_funding": {}}
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)), \
                patch.multiple(ls, AUTO_BLOCK_ENABLED=True, AUTO_ADJUST_ENABLED=True, MIN_SAMPLE_BLOCK=30,
                               MIN_SAMPLE_ADJUST=20, BLOCK_WR_MAX=30.0, BOOST_WR_MIN=65.0, ADJUST_CAP=0.25):
            adjustments = await ls.compute_auto_adjustments()
        self.assertEqual(adjustments["score_multipliers"]["tier_tf"], {"boost": 1.125, "pen": 0.875})
        self.assertEqual([b["key"] for b in adjustments["blocked_buckets"]], ["blk"])

    async def test_injected_invalid_bucket_never_becomes_boost_or_block(self):
        def bucket(n, wr):
            return {"trades": n, "win_rate": wr}
        stats = {"enabled": True, "total_trades": 100,
                 "by_tier_timeframe": {"nan_wr": bucket(50, float("nan")), "inf_n": bucket(float("inf"), 90.0),
                                       "bool_n": bucket(True, 90.0), "str_wr": bucket(50, "90"),
                                       "neg": bucket(-5, 10.0), "none": bucket(None, None),
                                       "frac": bucket(20.5, 90.0), "ok": bucket(30, 20.0)},
                 "by_pattern": {}, "by_session": {}, "by_day_of_week": {}, "by_funding": {}}
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)), \
                patch.multiple(ls, AUTO_BLOCK_ENABLED=True, AUTO_ADJUST_ENABLED=True, MIN_SAMPLE_BLOCK=30,
                               MIN_SAMPLE_ADJUST=20, BLOCK_WR_MAX=30.0, BOOST_WR_MIN=65.0):
            adjustments = await ls.compute_auto_adjustments()
        self.assertEqual(adjustments["score_multipliers"]["tier_tf"], {})
        self.assertEqual([b["key"] for b in adjustments["blocked_buckets"]], ["ok"])
        self.assertEqual(adjustments["invalid_buckets"], 7)
        json.dumps(adjustments, allow_nan=False)

    async def test_cache_query_count_and_window_preserved(self):
        rows = [snap(r=1.0)] * 3
        factory, session = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            first = await ls.compute_stats_by_bucket(days=0)
            cached = await ls.compute_stats_by_bucket(days=0)
            self.assertEqual(len(session.statements), 1)
            self.assertIs(first, cached)
            where = str(session.statements[0].whereclause)
            self.assertNotIn("outcome_at", where)
            ls.invalidate_cache()
            await ls.compute_stats_by_bucket(days=0)
            self.assertEqual(len(session.statements), 2)
            windowed = await ls.compute_stats_by_bucket(days=7)
            self.assertEqual(len(session.statements), 3)
            self.assertIn("outcome_at", str(session.statements[2].whereclause))
        self.assertEqual(windowed["days"], 7)

    async def test_database_failure_is_not_cached_as_empty(self):
        def boom():
            raise RuntimeError("db fora")
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", boom):
            with self.assertRaises(RuntimeError):
                await ls.compute_stats_by_bucket(days=0)
        self.assertEqual([k for k in ls._cache if k.startswith("stats_")], [])
        stats, _ = await self.bucket_stats([snap(r=1.0)] * 2)
        self.assertEqual(stats["total_trades"], 2)


# ── Paridade com dados válidos ─────────────────────────────────────────────
class ValidDataParity(SafetyCase):
    async def test_rotation_plan_uses_sanitised_stats_without_touching_the_engine(self):
        rows = ([snap(symbol="AAA/USDT:USDT", r=0.5)] * 12
                + [snap(symbol="BBB/USDT:USDT", r=-1.0)] * 12
                + [snap(symbol="BBB/USDT:USDT", r=float("nan"))] * 5
                + [snap(symbol="CCC/USDT:USDT", r=None)] * 30)
        factory, _ = session_factory(rows)
        shadow = types.ModuleType("services.shadow_trade_service")
        shadow.get_exec_allowlist = Mock(return_value={"BBB"})
        shadow.set_exec_allowlist = Mock(side_effect=AssertionError("apply real proibido"))
        shadow.EXEC_UNIVERSE_ALLOWLIST = {"BBB"}
        self.install_module("services.shadow_trade_service", shadow)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            plan = await rot.compute_rotation_plan(days=0)
        self.assertTrue(plan["dry_run"])
        self.assertEqual([p["symbol"] for p in plan["promote"]], ["AAA"])
        self.assertEqual([d["symbol"] for d in plan["demote"]], ["BBB"])
        self.assertEqual(plan["new_universe"], ["AAA"])
        shadow.set_exec_allowlist.assert_not_called()
        json.dumps(plan, allow_nan=False)

    def test_derivation_parity_on_valid_fixtures(self):
        cases = [
            ({"n_trades": 40, "wf_avg_r": 0.5, "wf_n_trades": 40, "expiry_pct": 0.0}, 0.5, 1.0, 0.333),
            ({"n_trades": 120, "wf_avg_r": 0.5, "wf_n_trades": 40, "expiry_pct": 0.0}, 1.0, 1.15, 1.0),
            ({"n_trades": 120, "wf_avg_r": 0.5, "wf_n_trades": 40, "expiry_pct": 0.0}, 0.0, 0.75, 1.0),
            ({"n_trades": 120, "wf_avg_r": 0.5, "wf_n_trades": 40, "expiry_pct": 45.0}, 1.0,
             round(1.15 * 0.85, 4), 1.0),
            ({"n_trades": 120, "wf_avg_r": 0.5, "wf_n_trades": 40, "expiry_pct": 30.0}, 0.8,
             round(1.075 * 0.92, 4), 1.0),
        ]
        for stats, percentile, mult, conf in cases:
            with self.subTest(percentile=percentile):
                derived = sls.derive_params(stats, percentile)
                self.assertEqual(derived["size_quality_mult"], mult)
                self.assertEqual(derived["confidence"], conf)

    def test_accessor_reason_strings_are_short_and_safe(self):
        cache = {"AAA": {"4h": learned(mult=float("nan"))}}
        with patch.object(sls, "_CACHE", cache), patch.multiple(
                sls, SYMBOL_LEARNING_SIZE_ENABLED=True, MIN_CONFIDENCE_APPLY=0.25,
                SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15):
            _, reason = sls.get_size_mult("AAA/USDT:USDT", "4h")
        self.assertLessEqual(len(reason), 60)
        json.dumps({"reason": reason}, allow_nan=False)


class Architecture(unittest.TestCase):
    def test_only_the_two_services_changed(self):
        """Escopo do PACOTE R11B2: audita o range do próprio commit, não o
        worktree — lotes posteriores acrescentam serviços legitimamente."""
        import subprocess
        res = subprocess.run(["git", "diff", "--name-only", "ec5c2fd9..da472b9c",
                              "--", "backend/services"],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("range ec5c2fd9..da472b9c indisponível neste checkout")
        self.assertEqual(sorted(res.stdout.split()),
                         ["backend/services/learning_service.py",
                          "backend/services/symbol_learning_service.py"])

    def test_no_new_query_source_or_status_filter(self):
        source = (BACKEND / "services" / "learning_service.py").read_text()
        self.assertEqual(source.count("_RESOLVED_STATUSES = "), 1)
        self.assertIn('_RESOLVED_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2", "lost")', source)
        self.assertEqual(source.count("await session.execute"), 4)
        self.assertNotIn("getenv(\"R11B2", source)

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
