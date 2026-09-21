"""R10A — replay OHLCV offline e comparador pré-registrado.

Hermético: rede/DNS bloqueados e contabilizados; sem banco, exchange, ENV ou
relógio. Números esperados calculados à mão, não copiados do motor. Sentinelas
falham se qualquer barra do holdout (ou além do horizonte) for materializada.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
import math
from pathlib import Path
import socket as _socket
import subprocess
import sys
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = BACKEND / "tests" / "fixtures"
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET_ATTEMPTS: list = []


def _blocked(*args, **kwargs):
    _NET_ATTEMPTS.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R10A")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS}")


from services import offline_replay_service as r  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000
CFG = r.ReplayConfig(entry_window_bars=2, pre_tp1_time_stop_bars=4, max_holding_bars=6, max_bars=16)
NO_COST = r.CostConfig()
ZERO = r.CostConfig(fee_bps_per_side=0.0, slippage_bps_per_side=0.0, funding_bps_per_bar=0.0)
COSTS = r.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0, funding_bps_per_bar=1.0)


def opp(direction="long", slot=0, oid=None, **kw):
    levels = (100.0, 95.0, 105.0, 110.0) if direction == "long" else (100.0, 105.0, 95.0, 90.0)
    values = dict(opportunity_id=oid or f"{direction}-{slot}", symbol="SYNTH/USDT:USDT",
                  direction=direction, decision_ts_ms=T0 + slot * BAR + 1000,
                  entry=levels[0], stop_loss=levels[1], tp1=levels[2], tp2=levels[3], atr=2.0)
    values.update(kw)
    return r.Opportunity(**values)


def mirror(bar, direction):
    o, h, l, c = bar
    return bar if direction == "long" else (200 - o, 200 - l, 200 - h, 200 - c)


def candles(specs, direction="long", slot=0, start=1, skip=()):
    out, index = [], 0
    for i, spec in enumerate(specs):
        while index in skip:
            index += 1
        o, h, l, c = mirror(spec, direction)
        out.append(r.Candle(T0 + (slot + start + index) * BAR, float(o), float(h), float(l), float(c), 10.0))
        index += 1
    return out


STOP = [(100, 101, 99, 100), (99, 99.5, 94, 95)]
TP2 = [(100, 101, 99.5, 100.5), (101, 106, 100.5, 105.5), (106, 111, 105.5, 110)]
RUNNER = [(100, 101, 99, 100), (100, 106, 99.5, 105), (105, 105.5, 100.8, 101)]


class Contracts(unittest.TestCase):
    def test_candle_rejects_bool_str_nan_inf_and_bad_ohlc(self):
        good = dict(timestamp_ms=T0, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
        r.Candle(**good)
        for key, bad in [("timestamp_ms", True), ("timestamp_ms", 1.0), ("timestamp_ms", "1"),
                         ("timestamp_ms", -1), ("open", float("nan")), ("high", float("inf")),
                         ("low", 0.0), ("close", "100"), ("volume", -1.0), ("volume", None),
                         ("open", True), ("low", 100.5), ("high", 99.5)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                r.Candle(**{**good, key: bad})

    def test_opportunity_geometry_direction_and_point_in_time(self):
        opp("long")
        opp("short")
        for kw in [dict(direction="LONG"), dict(direction="neutral"), dict(stop_loss=100.0),
                   dict(tp1=99.0), dict(tp2=105.0), dict(entry=True), dict(entry=float("nan")),
                   dict(opportunity_id=""), dict(opportunity_id="x" * 129), dict(atr=0.0),
                   dict(confluence_pct=70.0), dict(confluence_pct=70.0, features_asof_ms=T0 + 10**9),
                   dict(decision_ts_ms=True)]:
            direction = kw.get("direction", "long")
            others = {k: v for k, v in kw.items() if k != "direction"}
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                opp(direction, **others)
        with self.assertRaises(ValueError):
            opp("short", stop_loss=95.0)
        opp("long", confluence_pct=70.0, features_asof_ms=T0)

    def test_replay_config_limits(self):
        for kw in [dict(bar_ms=True), dict(tp1_fraction=0.0), dict(tp1_fraction=1.0),
                   dict(be_lock_fraction=1.5), dict(pre_tp1_time_stop_bars=7),
                   dict(max_bars=6), dict(max_bars=r.MAX_BARS + 1), dict(schema_version="v0"),
                   dict(trail_atr_multiple=0.0), dict(tp1_fraction=float("nan"))]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                replace(CFG, **kw)

    def test_costs_none_is_not_zero_and_bounds(self):
        self.assertFalse(NO_COST.manifest()["complete"])
        self.assertTrue(ZERO.manifest()["complete"])
        r.CostConfig(funding_bps_per_bar=-3.0)
        for kw in [dict(fee_bps_per_side=-1.0), dict(slippage_bps_per_side=float("inf")),
                   dict(fee_bps_per_side=10_000.0), dict(funding_bps_per_bar=True),
                   dict(fee_bps_per_side="4")]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                r.CostConfig(**kw)

    def test_replay_rejects_untyped_and_oversized_inputs(self):
        with self.assertRaises(ValueError):
            r.replay_opportunity({"entry": 100}, candles(STOP), CFG, NO_COST)
        with self.assertRaises(ValueError):
            r.replay_opportunity(opp(), [{"timestamp_ms": T0 + BAR}], CFG, NO_COST)
        too_many = [r.Candle(T0 + (i + 1) * BAR, 102.0, 103.0, 101.0, 102.0, 1.0) for i in range(17)]
        with self.assertRaises(ValueError):
            r.replay_opportunity(opp(), too_many, CFG, NO_COST)

    def test_overflow_is_an_error_not_infinite_r(self):
        tiny = opp(entry=1.0, stop_loss=1.0 - 1e-12, tp1=1e300, tp2=1e308)
        bars = [r.Candle(T0 + BAR, 1.0, 1e308, 1.0, 1e300, 1.0)]
        with self.assertRaises(ValueError):
            r.replay_opportunity(tiny, bars, CFG, ZERO)


class Numbers(unittest.TestCase):
    def run_(self, specs, direction="long", costs=ZERO, config=CFG, **kw):
        return r.replay_opportunity(opp(direction), candles(specs, direction, **kw), config, costs)

    def test_long_and_short_stop_are_symmetric(self):
        for direction in ("long", "short"):
            out = self.run_(STOP, direction)
            self.assertEqual((out["status"], out["gross_r"], out["net_r"]), ("CLOSED_STOP", -1.0, -1.0))
            self.assertTrue(out["filled"])
            self.assertEqual(out["entry_ts_ms"], T0 + BAR)

    def test_partial_tp1_then_tp2(self):
        for direction in ("long", "short"):
            out = self.run_(TP2, direction)
            self.assertEqual(out["status"], "CLOSED_TP2")
            self.assertAlmostEqual(out["gross_r"], 0.45 * 1 + 0.55 * 2)
            self.assertEqual([x["reason"] for x in out["exits"]], ["TP1", "TP2"])

    def test_breakeven_lock_only_from_next_bar(self):
        # Barra do TP1 desce a 100.5 (< BE 101): ainda vale o stop original.
        specs = [(100, 101, 99, 100), (100.6, 105.2, 100.5, 104), (104, 104.5, 100.9, 101)]
        out = self.run_(specs)
        self.assertEqual(out["status"], "CLOSED_RUNNER_STOP")
        self.assertAlmostEqual(out["gross_r"], 0.45 * 1 + 0.55 * (101 - 100) / 5)
        self.assertEqual(out["exit_ts_ms"], T0 + 3 * BAR)

    def test_atr_trail_causal_long_and_short(self):
        # pico 106 >= TP1 + 0.5*ATR → trail = 106 - 2.2*2 = 101.6 (> BE 101).
        for direction in ("long", "short"):
            out = self.run_(RUNNER, direction)
            self.assertEqual(out["status"], "CLOSED_RUNNER_STOP")
            self.assertAlmostEqual(out["exits"][-1]["reference_price"],
                                   101.6 if direction == "long" else 98.4)
            self.assertAlmostEqual(out["gross_r"], 0.45 + 0.55 * 1.6 / 5)

    def test_entry_not_touched_is_not_filled_without_r(self):
        out = self.run_([(102, 103, 101, 102), (103, 104, 102, 103)], costs=COSTS)
        self.assertEqual(out["status"], "NOT_FILLED")
        self.assertFalse(out["filled"])
        self.assertIsNone(out["gross_r"])
        self.assertIsNone(out["net_r"])

    def test_late_intrabar_entry_is_flagged(self):
        out = self.run_([(102, 103, 101, 102), (101, 102, 99.5, 101), (101, 103, 100, 102),
                         (102, 103, 100, 102), (102, 103, 100, 103)])
        self.assertEqual(out["entry_ts_ms"], T0 + 2 * BAR)
        self.assertIn("ENTRY_INTRABAR_TIME_UNKNOWN", out["reason_codes"])
        self.assertEqual(out["status"], "CLOSED_TIME_STOP")
        self.assertAlmostEqual(out["gross_r"], (103 - 100) / 5)

    def test_ambiguous_entry_bar_has_no_r(self):
        out = self.run_([(102, 103, 94, 97)], costs=COSTS)
        self.assertEqual(out["status"], "AMBIGUOUS_ENTRY_BAR")
        self.assertIsNone(out["gross_r"])
        self.assertIsNone(out["net_r"])

    def test_same_bar_stop_and_target_is_stop_first_and_flagged(self):
        out = self.run_([(100, 101, 99, 100), (100, 106, 94, 100)])
        self.assertEqual((out["status"], out["gross_r"]), ("CLOSED_STOP", -1.0))
        self.assertIn("INTRABAR_STOP_TARGET_AMBIGUITY_STOP_FIRST", out["reason_codes"])

    def test_adverse_price_gap_exits_at_open(self):
        for direction in ("long", "short"):
            out = self.run_([(100, 101, 99, 100), (93, 94, 92, 93)], direction)
            self.assertEqual(out["status"], "CLOSED_STOP")
            self.assertAlmostEqual(out["gross_r"], -7 / 5)
            self.assertIn("ADVERSE_GAP_STOP_AT_OPEN", out["reason_codes"])

    def test_time_stop_and_max_hold_close_at_close(self):
        out = self.run_([(100, 101, 99, 100), (100, 102, 98, 101), (101, 103, 99, 102), (102, 104, 100, 103)])
        self.assertEqual(out["status"], "CLOSED_TIME_STOP")
        self.assertAlmostEqual(out["gross_r"], 0.6)
        specs = [(100, 101, 99, 100), (100, 105.5, 99.5, 104)] + [(104, 105, 102, 104)] * 3 + [(104, 105, 102, 103)]
        out = self.run_(specs, "short")
        self.assertEqual(out["status"], "CLOSED_MAX_HOLD")
        self.assertAlmostEqual(out["gross_r"], 0.45 + 0.55 * 3 / 5)

    def test_data_gap_duplicate_unordered_or_early_bars(self):
        cases = {
            "gap": candles(STOP, skip=(1,)),
            "early": candles(STOP, start=0),
            "duplicate": candles(STOP[:1]) * 2,
            "unordered": list(reversed(candles(STOP))),
        }
        for name, bars in cases.items():
            with self.subTest(name):
                out = r.replay_opportunity(opp(), bars, CFG, ZERO)
                self.assertEqual(out["status"], "MISSING_OR_UNORDERED_BARS")
                self.assertIsNone(out["gross_r"])

    def test_incomplete_horizon_never_becomes_zero_or_partial_r(self):
        out = self.run_(TP2[:2])
        self.assertTrue(out["tp1_hit"])
        self.assertEqual(out["status"], "INSUFFICIENT_DATA")
        self.assertIn("INCOMPLETE_FORWARD_HORIZON", out["reason_codes"])
        self.assertIsNone(out["gross_r"])
        self.assertIsNone(r.replay_opportunity(opp(), [], CFG, ZERO)["gross_r"])

    def test_costs_per_leg_and_signed_funding(self):
        out = self.run_(STOP, costs=COSTS)
        slip, fee = 2 / 10_000, 4 / 10_000
        self.assertAlmostEqual(out["entry_fill_price"], 100 * (1 + slip))
        self.assertAlmostEqual(out["exits"][0]["fill_price"], 95 * (1 - slip))
        self.assertAlmostEqual(out["slippage_r"], slip * (100 + 95) / 5)
        self.assertAlmostEqual(out["fee_r"], fee * (100 * (1 + slip) + 95 * (1 - slip)) / 5)
        self.assertAlmostEqual(out["funding_r"], 99 * 1 / 10_000 / 5)  # só na abertura da barra 2
        self.assertAlmostEqual(out["net_r"], -1 - out["slippage_r"] - out["fee_r"] - out["funding_r"])
        self.assertAlmostEqual(out["net_r"], -1.02538008)
        short = self.run_(STOP, "short", costs=COSTS)
        self.assertLess(short["funding_r"], 0)  # funding positivo credita short
        self.assertAlmostEqual(short["funding_r"], -101 / 10_000 / 5)

    def test_unknown_cost_component_means_no_net(self):
        for costs in (NO_COST, r.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0),
                      r.CostConfig(fee_bps_per_side=4.0, funding_bps_per_bar=0.0)):
            out = self.run_(STOP, costs=costs)
            self.assertEqual(out["gross_r"], -1.0)
            self.assertIsNone(out["net_r"])
            self.assertEqual(out["cost_status"], "UNKNOWN")
            self.assertIn("COST_COMPONENT_UNKNOWN", out["reason_codes"])

    def test_declared_zero_costs_are_legitimate(self):
        out = self.run_(STOP, costs=ZERO)
        self.assertEqual(out["cost_status"], "KNOWN_SCENARIO")
        self.assertEqual(out["net_r"], out["gross_r"])

    def test_output_is_strict_json_and_deterministic(self):
        first = self.run_(RUNNER, costs=COSTS)
        self.assertEqual(first, self.run_(RUNNER, costs=COSTS))
        json.dumps(first, allow_nan=False)
        self.assertFalse(first["live_equivalent"])
        self.assertFalse(first["promotable"])
        self.assertIn(first["status"], r.REPLAY_STATUSES)


class _Sentinel(Mapping):
    """Mapping que FALHA se um id proibido for lido."""

    def __init__(self, data, forbidden):
        self.data, self.forbidden, self.read = data, set(forbidden), []

    def __getitem__(self, key):
        if key in self.forbidden:
            raise AssertionError(f"barra proibida acessada: {key}")
        self.read.append(key)
        return self.data[key]

    def __iter__(self):
        raise AssertionError("iteração do mapping de barras não é permitida")

    def __len__(self):
        return len(self.data)


class _PrefixOnly(Sequence):
    """Sequência que falha se um elemento além do horizonte for materializado."""

    def __init__(self, items, allowed):
        self.items, self.allowed = items, allowed

    def __getitem__(self, index):
        if isinstance(index, slice) or index >= self.allowed:
            raise AssertionError("barra além do horizonte materializada")
        return self.items[index]

    def __iter__(self):
        for index in range(len(self.items)):
            yield self[index]

    def __len__(self):
        return len(self.items)


def split(**kw):
    values = dict(train_start_ms=T0, validation_start_ms=T0 + 70 * BAR,
                  holdout_start_ms=T0 + 120 * BAR, purge_bars=1)
    values.update(kw)
    return r.ChronologicalSplit(**values)


def management(**changes):
    return r.CandidateRegistration("R10A-TEST", T0 - 1, "MANAGEMENT_ONLY", replace(CFG, **changes))


class Comparator(unittest.TestCase):
    def dataset(self):
        plan = [("a", 0, "long", STOP), ("b", 10, "short", TP2), ("c", 20, "long", RUNNER),
                ("d", 30, "short", RUNNER), ("e", 70, "long", TP2), ("f", 80, "short", STOP)]
        opportunities = [opp(d, s, oid=o) for o, s, d, _ in plan]
        bars = {o: candles(p, d, slot=s) for o, s, d, p in plan}
        return opportunities, bars

    def test_management_guard_one_behavioral_parameter(self):
        self.assertEqual(r.management_diff(CFG, CFG), [])
        self.assertEqual(r.management_diff(CFG, replace(CFG, tp1_fraction=0.3)), ["tp1_fraction"])
        for changes in [dict(tp1_fraction=0.3, be_lock_fraction=0.1), dict(bar_ms=60_000),
                        dict(entry_window_bars=3), dict(max_bars=15)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                opportunities, bars = self.dataset()
                r.compare_registered_candidate(opportunities, bars, CFG, management(**changes), COSTS, split())

    def test_identical_config_is_a_valid_control_with_zero_delta(self):
        opportunities, bars = self.dataset()
        out = r.compare_registered_candidate(opportunities, bars, CFG, management(), COSTS, split(),
                                             r.BootstrapConfig(samples=100, block_size=1))
        self.assertEqual(out["experiment"]["management_changed_parameters"], [])
        training = out["splits"]["training"]
        self.assertEqual(training["paired_resolved_n"], 4)
        self.assertEqual(training["paired_delta_ci"]["point"], 0.0)
        self.assertEqual((training["paired_delta_ci"]["low"], training["paired_delta_ci"]["high"]), (0.0, 0.0))

    def test_paired_delta_matches_hand_computation(self):
        opportunities, bars = self.dataset()
        out = r.compare_registered_candidate(opportunities, bars, CFG, management(tp1_fraction=0.3),
                                             ZERO, split(), r.BootstrapConfig(samples=100, block_size=1))
        training = out["splits"]["training"]
        # stop: 0; TP2: 1.55 -> 1.70; runner (x2): 0.626 -> 0.524
        expected = (0.0 + (1.70 - 1.55) + 2 * (0.524 - 0.626)) / 4
        self.assertAlmostEqual(training["paired_delta_ci"]["point"], expected)
        self.assertAlmostEqual(training["baseline_paired"]["net_expectancy_r"], (-1 + 1.55 + 2 * 0.626) / 4)
        self.assertEqual(out["winner"], None)
        self.assertEqual(out["decision"], "NO_PROMOTION_RESEARCH_ONLY")
        self.assertFalse(out["promotable"])

    def test_better_candidate_is_still_not_promoted(self):
        # Só trajetórias TP2: fração menor no TP1 é estritamente melhor aqui.
        opportunities = [opp("long", s, oid=f"t{s}") for s in (0, 10, 20)]
        bars = {f"t{s}": candles(TP2, slot=s) for s in (0, 10, 20)}
        out = r.compare_registered_candidate(opportunities, bars, CFG, management(tp1_fraction=0.3),
                                             ZERO, split(), r.BootstrapConfig(samples=100, block_size=1))
        delta = out["splits"]["training"]["paired_delta_ci"]
        self.assertAlmostEqual(delta["point"], 1.70 - 1.55)
        self.assertGreater(delta["low"], 0)
        self.assertIsNone(out["winner"])
        self.assertEqual(out["decision"], "NO_PROMOTION_RESEARCH_ONLY")
        self.assertFalse(out["promotable"])

    def test_registration_must_precede_training_and_ids_unique(self):
        opportunities, bars = self.dataset()
        late = r.CandidateRegistration("late", T0 + 1, "MANAGEMENT_ONLY", CFG)
        with self.assertRaises(ValueError):
            r.compare_registered_candidate(opportunities, bars, CFG, late, COSTS, split())
        with self.assertRaises(ValueError):
            r.compare_registered_candidate(opportunities + opportunities[:1], bars, CFG,
                                           management(), COSTS, split())
        with self.assertRaises(ValueError):
            r.CandidateRegistration("x", T0, "STRUCTURAL_CONF_ONLY", CFG)
        with self.assertRaises(ValueError):
            r.CandidateRegistration("x", T0, "MANAGEMENT_ONLY")
        with self.assertRaises(ValueError):
            r.CandidateRegistration("x", T0, "SCORE_V3_LIVE")

    def test_purge_uses_maximum_horizon_of_both_configs(self):
        near = opp("long", 61, oid="near")   # 61 + 1 + 7 + 1 = 70: cabe com horizonte 7
        bars = {"near": candles(STOP, slot=61)}
        out = r.compare_registered_candidate([near], bars, CFG, management(), COSTS, split())
        self.assertEqual(out["counts"]["training"], 1)
        # candidato com horizonte maior (max_holding 7) empurra a purga
        longer = replace(CFG, max_holding_bars=7)
        out = r.compare_registered_candidate([near], _Sentinel(bars, {"near"}), CFG,
                                             r.CandidateRegistration("L", T0 - 1, "MANAGEMENT_ONLY", longer),
                                             COSTS, split())
        self.assertEqual(out["counts"]["purged"], 1)

    def test_holdout_sentinel_never_touched(self):
        opportunities, bars = self.dataset()
        sealed = [opp("long", 120, oid="ho-1"), opp("short", 130, oid="ho-2")]
        bars = dict(bars, **{"ho-1": candles(STOP, slot=120), "ho-2": candles(TP2, "short", slot=130)})
        guard = _Sentinel(bars, {"ho-1", "ho-2"})
        out = r.compare_registered_candidate(opportunities + sealed, guard, CFG, management(), COSTS, split())
        self.assertEqual(out["counts"]["holdout_sealed"], 2)
        self.assertNotIn("ho-1", guard.read)
        self.assertTrue(out["holdout_policy"] == "SEALED" and out["holdout_outcomes_loaded"] is False)
        self.assertNotIn("ho-", json.dumps(out["rows"]))

    def test_holdout_sentinel_is_live(self):
        """Controle negativo: se o split não selasse, a sentinela dispararia."""
        sealed = [opp("long", 120, oid="ho-1")]
        guard = _Sentinel({"ho-1": candles(STOP, slot=120)}, {"ho-1"})
        with self.assertRaises(AssertionError):
            r.compare_registered_candidate(sealed, guard, CFG, management(), COSTS,
                                           split(holdout_start_ms=T0 + 200 * BAR))

    def test_bars_beyond_horizon_are_not_materialized(self):
        bars = candles(STOP + [(95, 96, 94, 95)] * 10)
        guarded = _PrefixOnly(bars, allowed=CFG.entry_window_bars + CFG.max_holding_bars - 1)
        out = r.compare_registered_candidate([opp()], {"long-0": guarded}, CFG, management(), COSTS, split())
        self.assertEqual(out["rows"][0]["baseline"]["status"], "CLOSED_STOP")

    def test_bar_crossing_split_boundary_is_rejected(self):
        near = opp("long", 60, oid="near")
        crossing = candles(STOP[:1], slot=60) + [r.Candle(T0 + 75 * BAR, 100.0, 101.0, 99.0, 100.0, 1.0)]
        with self.assertRaises(ValueError):
            r.compare_registered_candidate([near], {"near": crossing}, CFG, management(), COSTS, split())

    def test_structural_candidate_compares_scores_only(self):
        opportunities = [opp("long", 0, oid="s1", confluence_pct=70.0, adx=5.0, funding_pct=0.0,
                             features_asof_ms=T0),
                         opp("long", 10, oid="s2")]
        bars = {"s1": candles(STOP), "s2": candles(TP2, slot=10)}
        registration = r.CandidateRegistration("R08A-ABLATION", T0 - 1)
        out = r.compare_registered_candidate(opportunities, bars, CFG, registration, COSTS, split())
        self.assertEqual(out["economic_comparison_status"], "UNAVAILABLE_STRUCTURAL_CANDIDATE")
        first = out["rows"][0]
        self.assertIsNone(first["candidate"])
        scores = first["structural_scores"]
        self.assertEqual(scores["baseline"]["score"], 50.0)   # 0.6*70 + 0.3*10 + 0.1*50
        self.assertEqual(scores["candidate"]["score"], 70.0)
        self.assertFalse(scores["economic_rule_available"])
        self.assertFalse(scores["calibrated"])
        self.assertEqual(out["rows"][1]["structural_scores"]["baseline"]["status"], "UNAVAILABLE")
        keys = set()
        def walk(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(out)
        for invented in ("tier", "prob_tp1", "p_tp1", "probability", "net_return"):
            self.assertNotIn(invented, keys)
        self.assertEqual(out["splits"]["training"]["paired_resolved_n"], 0)
        self.assertIsNone(out["splits"]["training"]["paired_delta_ci"])

    def test_costs_are_shared_and_hashed(self):
        opportunities, bars = self.dataset()
        a = r.compare_registered_candidate(opportunities, bars, CFG, management(), COSTS, split())
        b = r.compare_registered_candidate(opportunities, bars, CFG, management(), ZERO, split())
        self.assertNotEqual(a["experiment_hash"], b["experiment_hash"])
        self.assertEqual(a["rows"][0]["baseline"]["cost_config_hash"],
                         a["rows"][0]["candidate"]["cost_config_hash"])

    def test_unknown_costs_leave_no_paired_economics(self):
        opportunities, bars = self.dataset()
        out = r.compare_registered_candidate(opportunities, bars, CFG, management(tp1_fraction=0.3),
                                             NO_COST, split())
        training = out["splits"]["training"]
        self.assertEqual(training["paired_resolved_n"], 0)
        self.assertEqual(training["excluded_or_unpaired_n"], 4)
        self.assertIsNone(training["candidate_paired"]["net_expectancy_r"])

    def test_bootstrap_is_deterministic_and_bounded(self):
        opportunities, bars = self.dataset()
        run = lambda: r.compare_registered_candidate(opportunities, bars, CFG, management(tp1_fraction=0.3),
                                                     COSTS, split(), r.BootstrapConfig(seed=7, samples=150, block_size=2))
        self.assertEqual(run()["splits"], run()["splits"])
        for kw in [dict(samples=99), dict(samples=r.MAX_BOOTSTRAP_SAMPLES + 1), dict(block_size=0), dict(seed=True)]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                r.BootstrapConfig(**kw)
        with self.assertRaises(ValueError):
            r.compare_registered_candidate([opp(oid=str(i)) for i in range(r.MAX_OPPORTUNITIES + 1)],
                                           {}, CFG, management(), COSTS, split())

    def test_split_must_be_chronological(self):
        for kw in [dict(validation_start_ms=T0), dict(holdout_start_ms=T0 + 70 * BAR), dict(purge_bars=0)]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                split(**kw)


class PayloadAndCli(unittest.TestCase):
    def load(self, name):
        return json.loads((FIXTURES / name).read_text())

    def compare_with_full_horizon(self):
        payload = self.load("r10a_synthetic_compare.json")
        configs = (payload["baseline_config"], payload["candidate"]["replay_config"])
        horizon = max(c["entry_window_bars"] + c["max_holding_bars"] - 1 for c in configs)
        rows = next(iter(payload["bars_by_id"].values()))
        while len(rows) < horizon:
            rows.append(dict(rows[-1], timestamp_ms=rows[-1]["timestamp_ms"] + configs[0]["bar_ms"]))
        return payload, rows

    def test_fixture_replay_numbers(self):
        out = r.run_payload(self.load("r10a_synthetic_replay.json"))
        self.assertEqual(out["status"], "CLOSED_STOP")
        self.assertAlmostEqual(out["net_r"], -1.02538008)

    def test_fixture_compare_counts_and_no_winner(self):
        out = r.run_payload(self.load("r10a_synthetic_compare.json"))
        self.assertEqual(out["counts"], {"training": 6, "validation": 4, "purged": 1,
                                         "holdout_sealed": 2, "before_training": 0})
        self.assertEqual(out["splits"]["training"]["paired_resolved_n"], 5)
        self.assertEqual(out["splits"]["validation"]["paired_resolved_n"], 3)
        self.assertEqual(out["splits"]["training"]["ci_status"], "AVAILABLE")
        self.assertEqual(out["splits"]["validation"]["ci_status"], "INSUFFICIENT_BLOCKS")
        self.assertIsNone(out["winner"])
        statuses = {row["opportunity_id"]: row["baseline"]["status"] for row in out["rows"]}
        self.assertEqual(statuses["tr-nofill-L"], "NOT_FILLED")
        self.assertEqual(statuses["va-ambig-L"], "AMBIGUOUS_ENTRY_BAR")
        self.assertNotIn("tr-purged-L", statuses)

    def test_payload_rejects_holdout_bars_unknown_ids_and_keys(self):
        base = self.load("r10a_synthetic_compare.json")
        holdout_start = base["split"]["holdout_start_ms"]
        cases = []
        leak = json.loads(json.dumps(base))
        leak["bars_by_id"]["ho-stop-L"] = [dict(leak["bars_by_id"]["tr-stop-L"][0], timestamp_ms=holdout_start + BAR)]
        cases.append(leak)
        tail = json.loads(json.dumps(base))
        tail["bars_by_id"]["va-stop-L"].append(dict(tail["bars_by_id"]["va-stop-L"][0], timestamp_ms=holdout_start))
        cases.append(tail)
        unknown = json.loads(json.dumps(base))
        unknown["bars_by_id"]["ghost"] = []
        cases.append(unknown)
        extra = dict(base, outcomes={})
        cases.append(extra)
        bad_ts = json.loads(json.dumps(base))
        bad_ts["bars_by_id"]["tr-stop-L"][0]["timestamp_ms"] = float(bad_ts["bars_by_id"]["tr-stop-L"][0]["timestamp_ms"])
        cases.append(bad_ts)
        two_changes = json.loads(json.dumps(base))
        two_changes["candidate"]["replay_config"]["be_lock_fraction"] = 0.1
        cases.append(two_changes)
        for index, payload in enumerate(cases):
            with self.subTest(index), self.assertRaises(ValueError):
                r.run_payload(payload)
        with self.assertRaises(ValueError):
            r.run_payload(dict(self.load("r10a_synthetic_replay.json"), extra=1))
        with self.assertRaises(ValueError):
            r.run_payload({"mode": "sweep"})

    def test_payload_rejects_bar_ending_in_holdout_even_beyond_horizon(self):
        payload, rows = self.compare_with_full_horizon()
        old_boundary = payload["split"]["holdout_start_ms"]
        payload["split"]["holdout_start_ms"] = old_boundary + 1000
        rows.append(dict(timestamp_ms=old_boundary, open=100.0, high=101.0,
                         low=99.0, close=100.0, volume=1.0))
        with patch.object(r.Candle, "__post_init__", side_effect=AssertionError("Candle materializada")):
            with self.assertRaisesRegex(ValueError, "holdout"):
                r.run_payload(payload)

    def test_payload_allows_bar_ending_exactly_at_holdout_without_decoding_tail(self):
        payload, rows = self.compare_with_full_horizon()
        tail_stamp = payload["split"]["holdout_start_ms"] - payload["baseline_config"]["bar_ms"]
        rows.append(dict(timestamp_ms=tail_stamp, open=100.0, high=101.0,
                         low=99.0, close=100.0, volume=1.0))
        materialized = []
        validate = r.Candle.__post_init__

        def guard(candle):
            self.assertNotEqual(candle.timestamp_ms, tail_stamp, "barra além do horizonte materializada")
            materialized.append(candle.timestamp_ms)
            validate(candle)

        with patch.object(r.Candle, "__post_init__", new=guard):
            out = r.run_payload(payload)
        self.assertTrue(materialized)
        self.assertNotIn(tail_stamp, materialized)
        self.assertFalse(out["holdout_outcomes_loaded"])
        self.assertEqual(out["counts"]["holdout_sealed"], 2)

    def test_payload_validates_baseline_before_checking_bar_boundaries(self):
        payload = self.load("r10a_synthetic_compare.json")
        payload["baseline_config"]["bar_ms"] = True
        payload["bars_by_id"]["tr-stop-L"][0]["timestamp_ms"] = "invalid"
        with self.assertRaisesRegex(ValueError, "bar_ms"):
            r.run_payload(payload)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(BACKEND / "scripts" / "research_replay.py"), *args],
                              capture_output=True, text=True, cwd=BACKEND,
                              env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})

    def test_cli_manifest_and_fixtures(self):
        manifest = self.cli("--manifest")
        self.assertEqual(manifest.returncode, 0, manifest.stderr)
        data = json.loads(manifest.stdout)
        self.assertFalse(data["promotable"])
        self.assertFalse(data["live_equivalent"])
        self.assertEqual(data["statuses"], list(r.REPLAY_STATUSES))
        self.assertEqual(data["management_max_changed_parameters"], 1)
        for name in ("r10a_synthetic_replay.json", "r10a_synthetic_compare.json"):
            result = self.cli(str(FIXTURES / name))
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)

    def test_cli_invalid_input_is_generic_and_rejects_nan(self):
        scratch = BACKEND / "tests" / "fixtures" / "r10a_synthetic_replay.json"
        text = scratch.read_text().replace('"fee_bps_per_side": 4.0', '"fee_bps_per_side": NaN')
        result = subprocess.run([sys.executable, "-B", str(BACKEND / "scripts" / "research_replay.py"), "/dev/stdin"],
                                input=text, capture_output=True, text=True, cwd=BACKEND,
                                env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(result.stderr)["status"], "INVALID_INPUT")


class Isolation(unittest.TestCase):
    SOURCE = (BACKEND / "services" / "offline_replay_service.py").read_text()

    def test_module_level_imports_are_stdlib_only(self):
        tree = ast.parse(self.SOURCE)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                for name in names:
                    self.assertIn(name.split(".")[0], {"__future__", "dataclasses", "hashlib", "itertools",
                                                      "json", "math", "random", "typing"}, name)

    def test_no_env_clock_io_db_or_live_modules_in_code(self):
        tree = ast.parse(self.SOURCE)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for forbidden in ("os", "time", "datetime", "socket", "get_session", "requests", "httpx"):
            self.assertNotIn(forbidden, names)
        for forbidden in ("open", "exec", "eval", "__import__", "input"):
            self.assertNotIn(forbidden, called)
        for forbidden in ("getenv", "environ", "now", "utcnow", "read_text"):
            self.assertNotIn(forbidden, attrs)
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imports - {"__future__", "dataclasses", "itertools", "typing"},
                         {"services.score_research_service"})

    def test_structural_branch_loads_lab_lazily(self):
        code = ("import sys\n"
                "from services import offline_replay_service as r\n"
                "assert 'services.score_research_service' not in sys.modules\n"
                "o = r.Opportunity(opportunity_id='a', symbol='S', direction='long', decision_ts_ms=1000,"
                " entry=100.0, stop_loss=95.0, tp1=105.0, tp2=110.0)\n"
                "s = r.ChronologicalSplit(10, 10**12, 2 * 10**12)\n"
                "r.compare_registered_candidate([], {}, r.ReplayConfig(), r.CandidateRegistration('x', 0), r.CostConfig(), s)\n"
                "assert 'services.score_research_service' in sys.modules\n"
                "print('LAZY_OK')\n")
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=BACKEND, capture_output=True, text=True,
                                env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr[-600:])
        self.assertIn("LAZY_OK", result.stdout)

    def test_live_paths_do_not_use_the_comparator(self):
        """Quem importa o replay é nominal, e nenhum deles chama o comparador.

        `research_dataset_service` (R10B) é exportador OFFLINE: usa os contratos
        (Opportunity/Candle/ReplayConfig/management_diff), nunca a comparação —
        e nenhum serviço LIVE o importa (teste próprio do R10B).
        """
        users = []
        for path in (BACKEND / "services").glob("*.py"):
            if path.name == "offline_replay_service.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "offline_replay_service" not in text:
                continue
            users.append(path.name)
            # CÓDIGO, não prosa: nomes chamados, atributos e importados.
            tree = ast.parse(text)
            usados = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            usados |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    usados |= {a.asname or a.name for a in node.names}
            for proibido in ("compare_registered_candidate", "run_payload"):
                self.assertNotIn(proibido, usados, f"{path.name}: {proibido}")
        # `portfolio_replay_service` (R10D) reutiliza o MOTOR de trajetória em
        # vez de criar um segundo backtest; como os demais, não toca o comparador.
        self.assertEqual(sorted(users), ["decision_observation_service.py",
                                         "portfolio_replay_service.py",
                                         "research_batch_service.py",
                                         "research_dataset_service.py"])
        main = (BACKEND / "main.py").read_text()
        self.assertNotIn("offline_replay_service", main)
        self.assertNotIn("research_replay", main)
        self.assertNotIn("research_dataset", main)
        # A menção em prosa é permitida; a chamada é que não pode existir.
        exporter = (BACKEND / "services" / "research_dataset_service.py").read_text()
        self.assertIn("run_payload", exporter)

    def test_no_network_attempted(self):
        self.assertEqual(_NET_ATTEMPTS, [])


if __name__ == "__main__":
    unittest.main()
