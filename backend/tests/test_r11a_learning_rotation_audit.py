"""R11A — caracterização do aprendizado, edge decay e rotação (SEM corrigir).

Cada teste fixa o comportamento ATUAL com dados sintéticos e mocks integrais.
Passar aqui não aprova a política nem prova rentabilidade; os achados e as
propostas R11B estão em docs/R11A_LEARNING_ROTATION_AUDIT.md (ids L*/S*/E*/R*).
Sem rede, banco, exchange ou notificação reais; caches/ENV restaurados.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import socket as _socket
import subprocess
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.dialects import postgresql

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R11A")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import edge_decay_service as ed          # noqa: E402
from services import learning_service as ls            # noqa: E402
from services import rotation_service as rot           # noqa: E402
from services import symbol_learning_service as sls    # noqa: E402


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


def _forbidden_session(*args, **kwargs):
    raise AssertionError("banco real proibido no R11A")


_MISSING = object()


class AuditCase(unittest.IsolatedAsyncioTestCase):
    """Isola caches, ENV, notificador e banco; tudo restaurado após cada teste."""

    def install_module(self, name, module):
        """Substitui SÓ esta chave de sys.modules (patch.dict removeria imports novos)."""
        previous = sys.modules.get(name, _MISSING)
        sys.modules[name] = module

        def restore():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        self.addCleanup(restore)

    def setUp(self):
        self._saved = (dict(ls._cache), dict(sls._CACHE), sls._CACHE_LOADED,
                       dict(ed._CACHE), ed._CACHE_AT)
        notifier = types.ModuleType("services.notification_service")
        notifier.send_telegram = AsyncMock(side_effect=AssertionError("notificação real proibida"))
        notifier.fmt_symbol_rerank = Mock(return_value="x")
        self.notifier = notifier
        self.install_module("services.notification_service", notifier)
        for p in (patch.dict(os.environ, {}, clear=False),
                  patch("db.get_session", _forbidden_session),
                  patch.object(ls, "get_session", _forbidden_session),
                  patch.object(sls, "get_session", _forbidden_session)):
            p.start()
            self.addCleanup(p.stop)
        ls.invalidate_cache()

    def tearDown(self):
        cache, scache, loaded, ecache, eat = self._saved
        ls._cache.clear()
        ls._cache.update(cache)
        sls._CACHE, sls._CACHE_LOADED = scache, loaded
        ed._CACHE, ed._CACHE_AT = ecache, eat


def snap(symbol="BTC/USDT:USDT", r=1.0, tier="A", tf="4h", direction="long", features=None,
         status="won_tp1"):
    return NS(symbol=symbol, realized_r=r, tier=tier, timeframe=tf, direction=direction,
              features=features or {}, status=status)


# ── L: learning_service ─────────────────────────────────────────────────────
class LearningService(AuditCase):
    async def symbol_stats(self, rows, days=0):
        factory, session = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.compute_symbol_stats(days=days), session

    async def test_L1_unknown_r_is_excluded_after_r11b2(self):
        """CORRIGIDO no R11B2 (A5/M2). ANTES: 12 trades, 6 "derrotas" de R
        ausente, média 0,5R e amostra mínima completada → veredicto promote.
        AGORA: só a evidência utilizável entra, e a qualidade é reportada."""
        rows = [snap(r=1.0)] * 6 + [snap(r=None)] * 6
        stats, _ = await self.symbol_stats(rows)
        btc = stats["BTC"]
        self.assertEqual((btc["trades"], btc["wins"], btc["losses"]), (6, 6, 0))
        self.assertEqual(btc["avg_r"], 1.0)
        self.assertFalse(btc["sample_ok"])
        self.assertEqual(btc["verdict"], "amostra_pequena")
        self.assertEqual(btc["data_quality"]["excluded_by_reason"]["ausente"], 6)

    async def test_L2_non_finite_r_no_longer_neutralises_verdict(self):
        """CORRIGIDO no R11B2 (M2). ANTES: um NaN tornava a média não finita,
        o veredicto virava "neutro" (um símbolo a −1R não era rebaixado) e o
        JSON não serializava. AGORA: o NaN sai da amostra e o resto é julgado."""
        rows = [snap(r=-1.0)] * 12 + [snap(r=float("nan"))]
        stats, _ = await self.symbol_stats(rows)
        self.assertEqual(stats["BTC"]["trades"], 12)
        self.assertEqual(stats["BTC"]["avg_r"], -1.0)
        self.assertEqual(stats["BTC"]["verdict"], "demote")
        self.assertEqual(stats["BTC"]["data_quality"]["excluded_by_reason"]["nao_finito"], 1)
        json.dumps(stats, allow_nan=False)

    async def test_L3_status_and_time_window(self):
        conds = ls._resolved_conditions(0)
        self.assertEqual(len(conds), 1)
        sql = str(conds[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn("('won_tp1', 'won_tp1_be', 'won_tp2', 'lost')", sql)
        self.assertNotIn("expired", sql)
        self.assertEqual(len(ls._resolved_conditions(-5)), 1)          # negativo = histórico todo
        windowed = ls._resolved_conditions(7)
        self.assertEqual(len(windowed), 2)
        self.assertIn("outcome_at", str(windowed[1]))
        _, session = await self.symbol_stats([], days=0)
        where = str(session.statements[0].whereclause)
        self.assertNotIn("outcome_at", where)
        self.assertNotIn("wide_", where)

    async def test_L4_rotation_verdict_edges(self):
        cases = [([0.5] * 11, "amostra_pequena"), ([0.0] * 12, "neutro"),
                 ([-0.2] * 12, "neutro"), ([-0.21] * 12, "demote"), ([0.01] * 12, "promote")]
        for values, verdict in cases:
            stats, _ = await self.symbol_stats([snap(r=v) for v in values])
            self.assertEqual(stats["BTC"]["verdict"], verdict, values[:1])

    async def test_L5_symbol_formats_merge_and_each_snapshot_is_one_sample(self):
        rows = [snap(symbol="BTC/USDT:USDT", tf="1h"), snap(symbol="BTCUSDT", tf="4h"),
                snap(symbol="btc/usdt", direction="short")]
        stats, _ = await self.symbol_stats(rows)
        self.assertEqual(list(stats), ["BTC"])
        self.assertEqual(stats["BTC"]["trades"], 3)   # TF/direção/formato somados

    async def bucket_stats(self, rows):
        factory, _ = session_factory(rows)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            return await ls.compute_stats_by_bucket(days=0)

    async def test_L6_unknown_r_no_longer_blocks_a_live_bucket(self):
        """CORRIGIDO no R11B2 (A5). ANTES: 30 resolvidos sem R davam WR 0% em
        A_4h, o bucket era bloqueado e toda rec dele era descartada (score 0).
        AGORA: sem evidência utilizável não há bucket, bloqueio nem descarte."""
        stats = await self.bucket_stats([snap(r=None) for _ in range(30)])
        self.assertEqual(stats["total_trades"], 0)
        self.assertEqual(stats.get("by_tier_timeframe", {}), {})
        self.assertEqual(stats["data_quality"]["excluded_by_reason"]["ausente"], 30)
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)), \
                patch.object(ls, "AUTO_BLOCK_ENABLED", True), patch.object(ls, "AUTO_ADJUST_ENABLED", True), \
                patch.object(ls, "MIN_SAMPLE_BLOCK", 30), patch.object(ls, "BLOCK_WR_MAX", 30.0):
            adj = await ls.compute_auto_adjustments()
        self.assertEqual(adj["blocked_buckets"], [])
        sig = NS(timeframe="4h", timestamp=None, patterns=[], derivatives=None)
        res = ls.apply_score_adjustment(sig, 80.0, adj, tier_provisional="A")
        self.assertFalse(res["blocked"])
        self.assertEqual(res["score"], 80.0)

    async def test_L7_adjustment_bands_and_dormant_label(self):
        def bucket(n, wr):
            return {"trades": n, "win_rate": wr}
        stats = {"enabled": True, "total_trades": 200,
                 "by_tier_timeframe": {"blk": bucket(30, 30.0), "boost": bucket(20, 82.5),
                                       "pen": bucket(20, 40.0), "mid": bucket(40, 55.0),
                                       "small": bucket(19, 90.0)},
                 "by_pattern": {}, "by_session": {}, "by_day_of_week": {}, "by_funding": {}}
        with patch.object(ls, "compute_stats_by_bucket", AsyncMock(return_value=stats)), \
                patch.multiple(ls, AUTO_BLOCK_ENABLED=True, AUTO_ADJUST_ENABLED=True, MIN_SAMPLE_BLOCK=30,
                               MIN_SAMPLE_ADJUST=20, BLOCK_WR_MAX=30.0, BOOST_WR_MIN=65.0, ADJUST_CAP=0.25):
            adj = await ls.compute_auto_adjustments()
        mults = adj["score_multipliers"]["tier_tf"]
        self.assertEqual([b["key"] for b in adj["blocked_buckets"]], ["blk"])
        self.assertEqual(mults, {"boost": 1.125, "pen": 0.875})
        self.assertEqual(adj["dormant_buckets"], 2)    # "mid" tem n=40 e entra como dormente
        with patch.multiple(ls, AUTO_BLOCK_ENABLED=False, AUTO_ADJUST_ENABLED=False):
            self.assertFalse((await ls.compute_auto_adjustments())["enabled"])

    def test_L8_product_cap_block_and_disabled(self):
        adj = {"enabled": True, "blocked_buckets": [],
               "score_multipliers": {"tier_tf": {"A_1h": 1.25}, "pattern": {"p1": 1.2, "p2": 1.2},
                                     "session": {}, "dow": {}, "funding": {}}}
        sig = NS(timeframe="1h", timestamp=None, derivatives=None,
                 patterns=[NS(type=NS(value="p1")), {"type": "p2"}])
        with patch.object(ls, "ADJUST_CAP", 0.25):
            res = ls.apply_score_adjustment(sig, 60.0, adj, tier_provisional="A")
        self.assertEqual((res["multiplier"], res["score"]), (1.25, 75.0))   # 1.8 capado em 1.25
        blocked = dict(adj, blocked_buckets=[{"category": "pattern", "key": "p2", "reason": "x"}])
        self.assertTrue(ls.apply_score_adjustment(sig, 60.0, blocked, "A")["blocked"])
        self.assertEqual(ls.apply_score_adjustment(sig, 60.0, {}, "A")["score"], 60.0)

    async def test_L9_session_learned_from_creation_hour_applied_from_candle_time(self):
        stats = await self.bucket_stats([snap(features={"hour_utc": 16}) for _ in range(25)])
        self.assertIn("NY", stats["by_session"])
        candle_open = int(datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        sig = NS(timeframe="4h", timestamp=candle_open, patterns=[], derivatives=None)
        self.assertEqual(ls._sig_to_bucket_keys(sig)["session"], "Europe")
        adj = {"enabled": True, "blocked_buckets": [], "score_multipliers": {"session": {"NY": 1.2}}}
        self.assertEqual(ls.apply_score_adjustment(sig, 70.0, adj, "A")["matched_buckets"], [])
        snapshot_src = (BACKEND / "services" / "snapshot_service.py").read_text()
        self.assertIn('"hour_utc": created_at.hour', snapshot_src)
        signal_src = (BACKEND / "services" / "signal_service.py").read_text()
        self.assertIn('timestamp=int(df["timestamp"].iloc[-1])', signal_src)

    async def test_L10_cache_and_failure_semantics(self):
        stats = await self.bucket_stats([snap()] * 3)
        self.assertEqual(stats["total_trades"], 3)
        factory, session = session_factory([snap()] * 9)
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", factory):
            cached = await ls.compute_stats_by_bucket(days=0)
        self.assertEqual(cached["total_trades"], 3)      # TTL: nova linha invisível
        self.assertEqual(session.statements, [])
        ls.invalidate_cache()

        def boom():
            raise RuntimeError("db fora")
        with patch.object(ls, "DB_ENABLED", True), patch.object(ls, "get_session", boom):
            with self.assertRaises(RuntimeError):
                await ls.compute_auto_adjustments()
        caller = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertIn('_log.warning(f"[learning] auto-adjust falhou (fail-open): {e}")', caller)


# ── S: symbol_learning_service ──────────────────────────────────────────────
class SymbolLearning(AuditCase):
    def test_S2_eligibility_rejects_nan_and_bool_after_r11b2(self):
        """CORRIGIDO no R11B2 (origem de A4). ANTES: `"nan"` virava float NaN e
        `True` virava 1.0, entrando no ranking e na edge calibrada. AGORA: os
        dois são inelegíveis e a derivação devolve None, não dicionário sujo."""
        self.assertIsNone(sls._eligible_metrics({"n_trades": 29, "wf_avg_r": 0.5}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": None}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": "nan"}))
        self.assertIsNone(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": True}))
        self.assertIsNone(sls.derive_params({"n_trades": 40, "wf_avg_r": "nan"}, 0.5))
        self.assertEqual(sls._eligible_metrics({"n_trades": 40, "wf_avg_r": 0.5})["wf"], 0.5)

    def test_S3_rank_mapping_and_expiry_penalty(self):
        with patch.multiple(sls, SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15, REL_DEADBAND=0.10):
            self.assertEqual(sls._mult_from_rank(0.5, 0), 1.0)
            self.assertEqual(sls._mult_from_rank(0.6, 0), 1.0)
            self.assertEqual(sls._mult_from_rank(1.0, 0), 1.15)
            self.assertEqual(sls._mult_from_rank(0.0, 0), 0.75)
            self.assertEqual(sls._mult_from_rank(0.8, 0), 1.075)
            self.assertEqual(sls._mult_from_rank(1.0, 45), round(1.15 * 0.85, 4))
            self.assertEqual(sls._mult_from_rank(0.0, 30), 0.75)     # piso vence a penalidade

    def test_S4_confidence_formula(self):
        d = sls.derive_params({"n_trades": 60, "wf_avg_r": 0.4, "wf_n_trades": 20}, 0.5)
        self.assertEqual(d["confidence"], round(0.5 * (0.5 + 0.5 * 0.5), 3))
        self.assertEqual(d["calibrated_edge"], round(0.4 * sls.CALIB_FACTOR, 4))

    def test_S5_get_size_mult_resolution(self):
        row = lambda tf, mult, conf: {"timeframe": tf, "size_quality_mult": mult, "confidence": conf,
                                      "calibrated_edge": 0.1}
        cache = {"AAA": {"1h": row("1h", 0.8, 0.9), "4h": row("4h", 1.1, 0.3)},
                 "LOW": {"4h": row("4h", 1.1, 0.2)}, "ZERO": {"4h": row("4h", 0.0, 0.9)},
                 "NANM": {"4h": row("4h", float("nan"), 0.9)},
                 "NANC": {"4h": row("4h", 0.8, float("nan"))}}
        with patch.object(sls, "_CACHE", cache), patch.multiple(sls, SYMBOL_LEARNING_SIZE_ENABLED=True,
                                                                MIN_CONFIDENCE_APPLY=0.25,
                                                                SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15):
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "4h")[0], 1.1)
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "15m")[0], 0.8)   # outro TF (maior confiança)
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", None)[0], 0.8)
            self.assertEqual(sls.get_size_mult("LOW/USDT:USDT", "4h")[0], 1.0)
            # CORRIGIDO no R11B2 (A4). ANTES: mult 0.0 era tratado como 1.0
            # "neutro", NaN no multiplicador virava o TETO 1.15 (amplificava a
            # mão) e confiança NaN passava no corte mínimo. AGORA: no-op.
            self.assertEqual(sls.get_size_mult("ZERO/USDT:USDT", "4h"),
                             (1.0, "linha aprendida inválida"))
            self.assertEqual(sls.get_size_mult("NANM/USDT:USDT", "4h"),
                             (1.0, "linha aprendida inválida"))
            self.assertEqual(sls.get_size_mult("NANC/USDT:USDT", "4h"),
                             (1.0, "linha aprendida inválida"))
            self.assertEqual(sls.get_size_mult("AAAUSDT", "4h")[0], 1.0)           # formato sem "/"
            self.assertEqual(sls.get_size_mult("AAA", "4h")[0], 1.1)
        with patch.object(sls, "_CACHE", cache), patch.object(sls, "SYMBOL_LEARNING_SIZE_ENABLED", False):
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "4h"), (1.0, "off"))

    async def test_S6_refresh_failure_keeps_stale_cache(self):
        sls._CACHE = {"OLD": {"4h": {"size_quality_mult": 1.15, "confidence": 1.0}}}

        def boom():
            raise RuntimeError("db fora")
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", boom):
            self.assertEqual(await sls.refresh_cache(), 0)
        self.assertIn("OLD", sls._CACHE)
        self.assertTrue(sls._CACHE_LOADED)

    async def test_S7_relearn_updates_only_best_tf_and_leaves_other_rows_stale(self):
        from models.symbol_learned_params import SymbolLearnedParams
        stats = [
            NS(error=None, symbol="AAA/USDT:USDT", timeframe="1h",
               to_dict=lambda: {"n_trades": 50, "wf_avg_r": 0.2, "wf_n_trades": 10}),
            NS(error=None, symbol="AAA/USDT:USDT", timeframe="4h",
               to_dict=lambda: {"n_trades": 50, "wf_avg_r": 0.6, "wf_n_trades": 10}),
            NS(error=None, symbol="BBB/USDT:USDT", timeframe="4h",
               to_dict=lambda: {"n_trades": 10, "wf_avg_r": 0.9, "wf_n_trades": 5}),   # inelegível
        ]
        stale_1h = SymbolLearnedParams(base="AAA", timeframe="1h", size_quality_mult=1.15, confidence=0.9)
        stale_bbb = SymbolLearnedParams(base="BBB", timeframe="4h", size_quality_mult=1.15, confidence=0.9)
        existing = {("AAA", "1h"): stale_1h, ("BBB", "4h"): stale_bbb}
        lookups = []

        def handler(stmt):
            if "symbol_backtest_stats" in str(stmt):
                return stats
            params = stmt.compile().params
            key = (params["base_1"], params["timeframe_1"])
            lookups.append(key)
            return [existing[key]] if key in existing else []
        factory, session = session_factory(handler)
        with patch.object(sls, "DB_ENABLED", True), patch.object(sls, "get_session", factory), \
                patch.object(sls, "refresh_cache", AsyncMock(return_value=0)), \
                patch.object(sls, "SYMBOL_LEARNING_NOTIFY", False):
            summary = await sls.relearn_all_from_history()
        self.assertEqual(summary["learned"], 1)
        self.assertEqual(lookups, [("AAA", "4h")])
        self.assertEqual(stale_1h.size_quality_mult, 1.15)    # linha antiga intocada
        self.assertEqual(stale_bbb.size_quality_mult, 1.15)   # base inelegível mantém o antigo
        self.assertEqual(len(session.added), 1)
        cache = {"AAA": {"1h": stale_1h.to_dict(), "4h": session.added[0].to_dict()},
                 "BBB": {"4h": stale_bbb.to_dict()}}
        with patch.object(sls, "_CACHE", cache), patch.object(sls, "SYMBOL_LEARNING_SIZE_ENABLED", True):
            self.assertEqual(sls.get_size_mult("AAA/USDT:USDT", "1h")[0], 1.15)
            self.assertEqual(sls.get_size_mult("BBB/USDT:USDT", "4h")[0], 1.15)
        self.notifier.send_telegram.assert_not_called()


# ── E: edge_decay_service ───────────────────────────────────────────────────
def _env(**over):
    base = {"EDGE_DECAY_ENABLED": "true", "EDGE_DECAY_WINDOW_DAYS": "60", "EDGE_DECAY_RECENT_DAYS": "14",
            "EDGE_DECAY_MIN_SAMPLE": "8", "EDGE_DECAY_R_FLOOR": "0.0", "EDGE_DECAY_R_FULL": "-0.3",
            "EDGE_DECAY_BASE_MIN": "0.1", "EDGE_DECAY_MULT_MIN": "0.5", "EDGE_DECAY_TTL_SEC": "1800"}
    base.update(over)
    return base


class EdgeDecay(AuditCase):
    def rows(self, spec):
        now = datetime.now(timezone.utc)
        out = []
        for count, r, days, direction in spec:
            out += [("SYN/USDT:USDT", direction, r, now - timedelta(days=days))] * count
        return out

    async def refresh(self, rows, force=True, **env):
        factory, session = session_factory(rows)
        with patch.dict(os.environ, _env(**env)), patch("db.get_session", factory):
            await ed.maybe_refresh(force=force)
        return session

    def test_E1_pure_rule_edges(self):
        c = ed._cfg.__wrapped__() if hasattr(ed._cfg, "__wrapped__") else None
        with patch.dict(os.environ, _env()):
            c = ed._cfg()
        self.assertEqual(ed._mult_from(-1.0, 0.5, 7, c)[0], 1.0)
        self.assertEqual(ed._mult_from(-1.0, 0.09, 8, c)[0], 1.0)
        self.assertEqual(ed._mult_from(0.0, 0.5, 8, c)[0], 1.0)
        self.assertEqual(ed._mult_from(-0.15, 0.5, 8, c)[0], 0.75)
        self.assertEqual(ed._mult_from(-5.0, 0.5, 8, c)[0], 0.5)

    async def test_E2_baseline_contains_recent_so_severe_decay_is_not_cut(self):
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        await self.refresh(self.rows([(20, 0.3, 30, "long"), (10, -0.1, 2, "long")]))
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "long")[0], round(1 - (0.1 / 0.3) * 0.5, 4))
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        await self.refresh(self.rows([(20, 0.3, 30, "long"), (10, -1.0, 2, "long")]))
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "long"), (1.0, "sem decay em cache"))

    async def test_E3_query_population_differs_from_learning(self):
        session = await self.refresh([])
        stmt = session.statements[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        statuses = [v for v in compiled.params.values() if isinstance(v, (list, tuple))][0]
        self.assertIn("expired", statuses)
        self.assertIn("realized_r IS NOT NULL", str(compiled))
        self.assertIn("outcome_at >=", str(compiled))
        self.assertNotIn("expired", ls._RESOLVED_STATUSES)

    async def test_E4_single_nan_forces_maximum_cut(self):
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        await self.refresh(self.rows([(9, 0.5, 2, "long"), (1, float("nan"), 2, "long")]))
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "long")[0], 0.5)

    async def test_E5_invalid_direction_is_counted_twice(self):
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        await self.refresh(self.rows([(20, 0.5, 30, "long"), (4, -0.5, 2, "long")]))
        self.assertEqual(ed._CACHE, {})                          # 4 recentes < mínimo 8
        await self.refresh(self.rows([(20, 0.5, 30, None), (4, -0.5, 2, None)]))
        hit = ed._CACHE[("SYN/USDT:USDT", "any")]
        self.assertEqual((hit["recent_n"], hit["mult"]), (8, 0.5))   # mesmas 4 linhas contam 8

    async def test_E6_empty_cache_requeries_every_call(self):
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        factory, session = session_factory(self.rows([(20, 0.5, 30, "long")]))
        with patch.dict(os.environ, _env()), patch("db.get_session", factory):
            for _ in range(3):
                await ed.maybe_refresh()
        self.assertEqual(len(session.statements), 3)
        factory, session = session_factory(self.rows([(20, 0.5, 30, "long"), (10, -0.5, 2, "long")]))
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        with patch.dict(os.environ, _env()), patch("db.get_session", factory):
            for _ in range(3):
                await ed.maybe_refresh()
        self.assertEqual(len(session.statements), 1)

    async def test_E7_failure_and_disabled_flag_keep_old_cuts(self):
        ed._CACHE, ed._CACHE_AT = {}, 0.0
        await self.refresh(self.rows([(20, 0.5, 30, "long"), (10, -0.5, 2, "long")]))
        stamp = ed._CACHE_AT

        def boom():
            raise RuntimeError("db fora")
        with patch.dict(os.environ, _env()), patch("db.get_session", boom):
            await ed.maybe_refresh(force=True)
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "long")[0], 0.5)
        self.assertEqual(ed._CACHE_AT, stamp)
        with patch.dict(os.environ, _env(EDGE_DECAY_ENABLED="false")):
            await ed.maybe_refresh(force=True)
            self.assertFalse(ed.is_enabled())
            self.assertEqual(ed.get_mult("SYN/USDT:USDT", "long")[0], 0.5)   # só o chamador filtra
        caller = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        self.assertIn("if edge_decay_service.is_enabled():\n                        _ed, _ed_reason", caller)

    async def test_E8_key_is_full_symbol_string(self):
        ed._CACHE = {("SYN/USDT:USDT", "long"): {"mult": 0.6, "reason": "x"}}
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "LONG")[0], 0.6)
        self.assertEqual(ed.get_mult("SYNUSDT", "long")[0], 1.0)
        self.assertEqual(ed.get_mult("SYN/USDT:USDT", "short")[0], 1.0)


# ── R: rotation_service ─────────────────────────────────────────────────────
class _Store:
    def __init__(self, universe, pending=None):
        self.state = {"universe": sorted(universe), "pending": pending or {}, "seeded": {}}
        self.saves = []

    async def load(self):
        return json.loads(json.dumps(self.state))

    async def save(self, universe, pending, seeded=None):
        self.saves.append((sorted(universe), json.loads(json.dumps(pending))))
        self.state = {"universe": sorted(universe), "pending": pending, "seeded": seeded or {}}


class Rotation(AuditCase):
    def setUp(self):
        super().setUp()
        self.shadow = types.ModuleType("services.shadow_trade_service")
        self.shadow.set_exec_allowlist = Mock()
        self.shadow.get_exec_allowlist = Mock(return_value=set())
        self.shadow.EXEC_UNIVERSE_ALLOWLIST = set()
        self.install_module("services.shadow_trade_service", self.shadow)
        self.notifier.send_telegram = AsyncMock(return_value=True)

    def stats(self, **verdicts):
        return {base: {"trades": 20, "win_rate": 60.0, "avg_r": avg, "total_r": avg * 20,
                       "verdict": verdict} for base, (verdict, avg) in verdicts.items()}

    async def plan(self, stats, universe):
        with patch("services.learning_service.compute_symbol_stats", AsyncMock(return_value=stats)), \
                patch.object(rot, "_resolve_current_universe", AsyncMock(return_value=(set(universe), "mem"))):
            return await rot.compute_rotation_plan(days=0)

    async def apply(self, store, plan, liq=frozenset(), **flags):
        values = dict(ROTATION_AUTO_APPLY=True, ROTATION_HYSTERESIS_CYCLES=3, ROTATION_MAX_UNIVERSE=350,
                      BT_SEED_ENABLED=False)
        values.update(flags)
        with patch.multiple(rot, **values), patch.object(rot, "_load_state", store.load), \
                patch.object(rot, "_save_state", store.save), \
                patch.object(rot, "_liquidity_floor", AsyncMock(return_value=set(liq))), \
                patch.object(rot, "_backtest_seed_candidates", AsyncMock(return_value={})):
            return await rot.apply_rotation_plan(plan)

    async def test_R1_plan_is_dry_run(self):
        plan = await self.plan(self.stats(NEW=("promote", 0.4), IN=("promote", 0.9), BAD=("demote", -0.5),
                                          OUT=("demote", -0.9), MID=("neutro", 0.0)), {"IN", "BAD"})
        self.assertEqual([p["symbol"] for p in plan["promote"]], ["NEW"])
        self.assertEqual([d["symbol"] for d in plan["demote"]], ["BAD"])
        self.assertEqual(plan["new_universe"], ["IN", "NEW"])
        self.shadow.set_exec_allowlist.assert_not_called()
        self.notifier.send_telegram.assert_not_called()

    async def test_R2_hysteresis_counts_calls_not_new_evidence(self):
        store = _Store({"IN"})
        plan = await self.plan(self.stats(NEW=("promote", 0.4)), {"IN"})
        results = [await self.apply(store, plan) for _ in range(3)]
        self.assertEqual([r["applied"] for r in results], [False, False, True])
        self.assertEqual(results[-1]["promoted"], ["NEW"])
        self.assertEqual(len(store.saves), 3)                 # escreve a cada chamada
        self.shadow.set_exec_allowlist.assert_called_once()
        self.notifier.send_telegram.assert_awaited_once()     # mock; nenhuma mensagem real

    async def test_R3_absence_resets_pending(self):
        store = _Store({"IN"})
        plan = await self.plan(self.stats(NEW=("promote", 0.4)), {"IN"})
        empty = await self.plan({}, {"IN"})
        for current in (plan, plan, empty, plan, plan):
            result = await self.apply(store, current)
        self.assertFalse(result["applied"])
        self.assertEqual(store.state["pending"]["NEW"]["count"], 2)

    async def test_R4_liquidity_floor_is_fail_open(self):
        plan = await self.plan(self.stats(NEW=("promote", 0.4)), {"IN"})
        store = _Store({"IN"}, {"NEW": {"action": "promote", "count": 2}})
        self.assertEqual((await self.apply(store, plan, liq=set()))["promoted"], ["NEW"])
        store = _Store({"IN"}, {"NEW": {"action": "promote", "count": 2}})
        blocked = await self.apply(store, plan, liq={"IN"})
        self.assertEqual(blocked["promoted"], [])
        self.assertNotIn("NEW", store.state["pending"])       # piso reprovado zera a contagem

    async def test_R5_cap_demote_first_and_waiting_promotes(self):
        plan = await self.plan(self.stats(A=("promote", 0.9), B=("promote", 0.5), BAD=("demote", -0.5)),
                               {"X", "BAD"})
        pending = {k: {"action": a, "count": 2} for k, a in (("A", "promote"), ("B", "promote"), ("BAD", "demote"))}
        store = _Store({"X", "BAD"}, pending)
        result = await self.apply(store, plan, ROTATION_MAX_UNIVERSE=2)
        self.assertEqual((result["demoted"], result["promoted"]), (["BAD"], ["A"]))
        self.assertEqual(store.state["pending"], {"B": {"action": "promote", "count": 3}})

    async def test_R6_memory_universe_divergence_blinds_demotion(self):
        store = _Store({"OK", "BAD"})
        plan = await self.plan(self.stats(BAD=("demote", -0.9)), {"OK"})   # memória sem BAD
        for _ in range(5):
            result = await self.apply(store, plan)
        self.assertEqual(result["demoted"], [])
        self.assertIn("BAD", store.state["universe"])

    async def test_R7_ties_follow_input_order(self):
        pending = {"A": {"action": "promote", "count": 2}, "B": {"action": "promote", "count": 2}}
        winners = []
        for order in (("A", "B"), ("B", "A")):
            plan = {"promote": [{"symbol": s, "avg_r": 0.5} for s in order], "demote": []}
            store = _Store({"X"}, dict(pending))
            winners.append((await self.apply(store, plan, ROTATION_MAX_UNIVERSE=2))["promoted"])
        self.assertEqual(winners, [["A"], ["B"]])

    async def test_R8_auto_apply_off_is_preview_without_writes(self):
        store = _Store({"IN"})
        plan = await self.plan(self.stats(NEW=("promote", 0.4)), {"IN"})
        result = await self.apply(store, plan, ROTATION_AUTO_APPLY=False)
        self.assertFalse(result["applied"])
        self.assertEqual(result["would_promote"], ["NEW"])
        self.assertEqual(store.saves, [])

    async def test_R11_seed_lane_admits_without_hysteresis_and_blocks_demoted_seed(self):
        store = _Store({"X"})
        with patch.object(rot, "BT_SEED_MAX", 2):
            with patch.object(rot, "_backtest_seed_candidates", AsyncMock(return_value={"S1": 0.9, "S2": 0.7})):
                values = dict(ROTATION_AUTO_APPLY=True, ROTATION_HYSTERESIS_CYCLES=3,
                              ROTATION_MAX_UNIVERSE=350, BT_SEED_ENABLED=True)
                with patch.multiple(rot, **values), patch.object(rot, "_load_state", store.load), \
                        patch.object(rot, "_save_state", store.save), \
                        patch.object(rot, "_liquidity_floor", AsyncMock(return_value=set())):
                    first = await rot.apply_rotation_plan({"promote": [], "demote": []})
                    store.state["pending"] = {"S1": {"action": "demote", "count": 2}}
                    second = await rot.apply_rotation_plan({"promote": [], "demote": [{"symbol": "S1", "avg_r": -1}]})
        self.assertEqual(first["seed_promoted"], ["S1", "S2"])
        self.assertEqual(second["demoted"], ["S1"])
        self.assertEqual(second["seed_blocked"], ["S1"])


def _function(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _route(tree, path):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call) and getattr(deco.func, "attr", "") == "post"
                        and deco.args and getattr(deco.args[0], "value", None) == path):
                    return node
    raise AssertionError(path)


def _calls(node):
    return {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in ast.walk(node) if isinstance(n, ast.Call)}


class Wiring(unittest.TestCase):
    MAIN = ast.parse((BACKEND / "main.py").read_text())

    def test_R9_mutating_routes_require_admin_check_after_r11b1(self):
        # R11A caracterizou a ausência; R11B1 corrige somente esse contrato.
        # Testes ASGI comportamentais estão em test_r11b1_admin_routes.py.
        for path in ("/api/rotation/apply", "/api/symbol-params/relearn"):
            node = _route(self.MAIN, path)
            self.assertIn("_check_admin_token", _calls(node), path)
            self.assertEqual([a.arg for a in node.args.args], ["x_admin_token"], path)
        self.assertIn("_check_admin_token", _calls(_route(self.MAIN, "/api/admin/force-test-trade")))
        cors = [n for n in ast.walk(self.MAIN) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "add_middleware"][0]
        origins = [k.value for k in cors.keywords if k.arg == "allow_origins"][0]
        self.assertEqual(ast.literal_eval(origins), ["*"])

    def test_R10_automatic_callers_map(self):
        loop = _calls(_function(self.MAIN, "_rotation_loop"))
        self.assertTrue({"prime_effective_allowlist", "maybe_send_weekly_preview",
                         "apply_rotation_plan"} <= loop)
        sweep = ast.parse((BACKEND / "services" / "backtest_universe_service.py").read_text())
        self.assertIn("relearn_all_from_history", {getattr(n.func, "attr", "") for n in ast.walk(sweep)
                                                   if isinstance(n, ast.Call)})
        shadow = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        for needle in ("_sls.get_size_mult(", "edge_decay_service.get_mult(", "edge_decay_service.maybe_refresh()"):
            self.assertIn(needle, shadow)
        rec = (BACKEND / "services" / "recommendation_service.py").read_text()
        self.assertEqual(rec.count("auto_adj = await compute_auto_adjustments()"), 2)
        self.assertEqual(rec.count("apply_score_adjustment(sig, score, auto_adj, tier_provisional=tier_prov)"), 2)

    def test_S1_R12_code_defaults_versus_comments(self):
        code = ("import json\n"
                "from services import learning_service as ls, symbol_learning_service as sls\n"
                "from services import rotation_service as rot, edge_decay_service as ed\n"
                "print(json.dumps({'symbol_size': sls.SYMBOL_LEARNING_SIZE_ENABLED,"
                " 'learn_on_sweep': sls.SYMBOL_LEARNING_LEARN_ON_SWEEP, 'notify': sls.SYMBOL_LEARNING_NOTIFY,"
                " 'auto_apply': rot.ROTATION_AUTO_APPLY, 'hysteresis': rot.ROTATION_HYSTERESIS_CYCLES,"
                " 'max_universe': rot.ROTATION_MAX_UNIVERSE, 'min_sample': ls.ROTATION_MIN_SAMPLE,"
                " 'auto_adjust': ls.AUTO_ADJUST_ENABLED, 'auto_block': ls.AUTO_BLOCK_ENABLED,"
                " 'lookback': ls.LEARNING_LOOKBACK_DAYS, 'edge_decay': ed.is_enabled(),"
                " 'seed': rot.BT_SEED_ENABLED}))\n")
        res = subprocess.run([sys.executable, "-B", "-c", code], cwd=BACKEND, capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr[-500:])
        defaults = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(defaults, {"symbol_size": True, "learn_on_sweep": True, "notify": True,
                                    "auto_apply": True, "hysteresis": 3, "max_universe": 350,
                                    "min_sample": 12, "auto_adjust": True, "auto_block": True,
                                    "lookback": 0, "edge_decay": False, "seed": False})
        sls_doc = (BACKEND / "services" / "symbol_learning_service.py").read_text()
        self.assertIn("SYMBOL_LEARNING_SIZE_ENABLED (default OFF)", sls_doc)
        self.assertIn("Default OFF (SYMBOL_LEARNING_SIZE_ENABLED)",
                      (BACKEND / "services" / "shadow_trade_service.py").read_text())
        self.assertIn("Com ROTATION_AUTO_APPLY=off (default) NADA é mutado",
                      (BACKEND / "main.py").read_text())
        self.assertIn("NÍVEL 2 (preparado, não ativo)",
                      (BACKEND / "services" / "learning_service.py").read_text())
        agents = BACKEND.parent / "AGENTS.md"
        if agents.exists():   # arquivo pessoal não versionado; só leitura
            self.assertIn("≥15 trades", agents.read_text())

    def test_audited_services_untouched(self):
        """R11B2 corrigiu A4/A5/M2 nos dois serviços de aprendizado. Edge decay
        e rotação seguem intactos: A2, A3, M3 e demais achados continuam abertos."""
        res = subprocess.run(["git", "diff", "--name-only", "51c992c2", "--",
                              "backend/services/learning_service.py",
                              "backend/services/symbol_learning_service.py",
                              "backend/services/edge_decay_service.py",
                              "backend/services/rotation_service.py"],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 51c992c2 indisponível neste checkout")
        self.assertEqual(sorted(res.stdout.split()),
                         ["backend/services/learning_service.py",
                          "backend/services/symbol_learning_service.py"])

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
