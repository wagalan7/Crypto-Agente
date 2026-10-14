"""Research sizing: actual source bodies, explicit TEST_ONLY causal cache facts.

The fixture reads literal declared defaults through AST, never production ENV,
database, account data or a live service import. Each numerical control exercises
the real selected caller region, not a second implementation of its formulas.
"""
from __future__ import annotations

import ast
import copy
from functools import lru_cache
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_upstream_sizing_service as upstream

T0 = 1_800_000_000_000


@lru_cache(maxsize=None)
def _fixture_source_tree(relative):
    return ast.parse((BACKEND / relative).read_text())


@lru_cache(maxsize=1)
def _fixture_shadow_names():
    return upstream.native_shadow_constant_names()


def _declared_default(relative, name):
    """TEST_ONLY declaration, not an observation of deployed configuration."""
    tree = _fixture_source_tree(relative)
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    calls = [n for n in ast.walk(assignment.value) if isinstance(n, ast.Call)]
    env = next(n for n in calls if isinstance(n.func, ast.Attribute)
               and n.func.attr == "getenv")
    value = ast.literal_eval(env.args[1])
    if name.endswith("_ENABLED"):
        return str(value).strip().lower() in ("1", "true", "yes")
    if any(isinstance(n.func, ast.Name) and n.func.id == "int" for n in calls):
        return int(value)
    return float(value)


def config(**shadow_changes):
    shadow = {name: _declared_default("services/shadow_trade_service.py", name)
              for name in _fixture_shadow_names()}
    # Closed frozen TEST_ONLY configuration: no reliance on the host's flags.
    for name in shadow:
        if name.endswith("_ENABLED"):
            shadow[name] = False
    shadow.update(SHADOW_ENABLED=False, MIN_NOTIONAL_USD=50., MAX_RISK_PCT_HARD=2.,
                  MAX_MARGIN_PCT_PER_TRADE=15., LIVE_SIZE_MULT=1.)
    shadow.update(shadow_changes)
    return {"shadow": shadow,
            "symbol_learning": {"SYMBOL_LEARNING_SIZE_ENABLED": False,
                "SIZE_MULT_MIN": .75, "SIZE_MULT_MAX": 1.15,
                "MIN_CONFIDENCE_APPLY": .25, "REL_DEADBAND": .10},
            "edge_decay": {"enabled": False, "window_days": 60, "recent_days": 14,
                "min_sample": 8, "r_floor": 0., "r_full": -.3,
                "base_min": .1, "mult_min": .5, "ttl_sec": 1800}}


def recommendation(**changes):
    values = {"symbol": "ALFA/USDT:USDT", "direction": "long", "timeframe": "1h",
              "tier": "A", "entry": 100., "stop_loss": 95., "tp1": 110., "tp2": 120.,
              "risk_pct": 1., "leverage": 5, "prob_tp1": .5, "prob_tp2": .3,
              "edge_score": 0, "edge_tags": [], "quote_vol_usd": 100_000_000.,
              "_is_filler": False,
              "signal": {"entry": 100., "indicators": {"atr": 2.},
                         "derivatives": {"funding_rate_pct": 0.}}}
    values.update(changes)
    return values


def observations(*, equity=None, symbol_rows=None, decay_rows=None):
    values = {"equity": {"ok": True, "total_usd": 1000., "source": "live"}
              if equity is None else equity,
              "symbol_learning": [] if symbol_rows is None else symbol_rows,
              "edge_decay": [] if decay_rows is None else decay_rows}
    producers = {"equity": "exchange_service.get_equity",
                 "symbol_learning": "symbol_learning_service.refresh_cache",
                 "edge_decay": "edge_decay_service.maybe_refresh"}
    return {name: upstream.make_observation(producer=producers[name], value=value,
            observed_at_ms=T0 - 1000, available_at_ms=T0, generation=1)
            for name, value in values.items()}


def make_request(*, cfg=None, rec=None, obs=None, context=None, **changes):
    return upstream.make_request(config=config() if cfg is None else cfg,
        recommendation=recommendation() if rec is None else rec,
        context={"regime": {}, "filler_test_done": 0} if context is None else context,
        observations=observations() if obs is None else obs,
        decision_at_ms=T0, opportunity_id="TEST_ONLY_UPSTREAM_OPPORTUNITY", **changes)


def reseal(request):
    result = copy.deepcopy(request)
    result["request_hash"] = upstream.digest({k: v for k, v in result.items()
                                            if k != "request_hash"})
    return result


def learned_row(mult, confidence=.8, timeframe="1h", **extra):
    return {"base": "ALFA", "timeframe": timeframe, "row": {
        "timeframe": timeframe, "size_quality_mult": mult, "confidence": confidence,
        "n_trades": 40, "wf_n_trades": 30, "wf_avg_r": .2,
        "calibrated_edge": .2, "expiry_pct": 10., **extra}}


def enabled_symbol_config():
    cfg = config()
    cfg["symbol_learning"]["SYMBOL_LEARNING_SIZE_ENABLED"] = True
    return cfg


class NativeSizingPositiveTests(unittest.TestCase):
    def run_sizing(self, **changes):
        report = upstream.replay(make_request(**changes))
        self.assertEqual(report["status"], "COMPLETE_SIZING", report.get("gaps"))
        self.assertEqual(report["gaps"], [])
        return report

    def test_all_off_is_native_nominal_sizing(self):
        report = self.run_sizing()
        self.assertEqual(report["qty"], 2.)
        self.assertEqual(report["notional"], 200.)
        self.assertEqual(report["final_risk_pct"], 1.)
        self.assertEqual(report["sizing"]["status"], "ok")
        self.assertIn("_compute_qty", [s["caller"] for s in report["steps"]])

    def test_hard_cap_after_margin_cap_and_canary_is_applied_once(self):
        report = self.run_sizing(cfg=config(LIVE_SIZE_MULT=.5),
                                 rec=recommendation(risk_pct=10.))
        self.assertEqual(report["sizing"]["qty"], 4.)
        self.assertEqual(report["sizing"]["status"], "risk_capped")
        self.assertEqual(report["qty"], 2.)
        self.assertEqual(report["notional"], 200.)

    def test_rounding_and_post_qty_stack_preserve_native_order(self):
        cfg = config(CONVICTION_SIZING_ENABLED=True, EDGE_SIZING_ENABLED=True,
            FUNDING_EV_ENABLED=True, FUNDING_EV_SIZE_ENABLED=True,
            EXEC_SIZE_DAMP_ENABLED=True, LIQ_TIER_SIZING_ENABLED=True,
            FILLER_FORA_ENABLED=True, LIVE_SIZE_MULT=.5, MIN_NOTIONAL_USD=20.)
        cfg["symbol_learning"]["SYMBOL_LEARNING_SIZE_ENABLED"] = True
        cfg["edge_decay"]["enabled"] = True
        rec = recommendation(edge_score=2, edge_tags=["A+"], quote_vol_usd=15_000_000.,
            _is_filler=True, signal={"entry": 100., "indicators": {"atr": 2.},
                                    "derivatives": {"funding_rate_pct": -.01}})
        obs = observations(symbol_rows=[learned_row(1.15)], decay_rows=[
            {"symbol": rec["symbol"], "direction": "long", "row": {"mult": .6, "reason": "TEST_ONLY"}}])
        report = self.run_sizing(cfg=cfg, rec=rec, obs=obs)
        # Independent arithmetic at the native rounding boundaries, not an
        # alternate policy: the chosen fixture fixes each multiplier above.
        self.assertEqual(report["final_risk_pct"], 1.1753)
        qty = 2.3506
        for mult in (.8963, .75, .6, .75, .5):
            qty = round(qty * mult, 6)
        self.assertEqual(report["qty"], qty)
        names = [s["caller"] for s in report["steps"]]
        self.assertLess(names.index("_conviction_mult"), names.index("_compute_qty"))
        self.assertLess(names.index("_compute_qty"), names.index("_exec_size_damp"))
        self.assertLess(names.index("_exec_size_damp"), names.index("_liq_tier_mult"))

    def test_raw_signal_atr_not_flat_atr_pct_controls_short_guard(self):
        cfg = config(SHORT_SLIP_GUARD_ENABLED=True)
        rec = recommendation(direction="short", stop_loss=105., tp1=90., tp2=80.,
                             atr_pct=0., signal={"entry": 100., "indicators": {"atr": 3.}})
        self.assertEqual(self.run_sizing(cfg=cfg, rec=rec)["qty"], 1.2)

    def test_conviction_without_probability_is_native_noop(self):
        report = self.run_sizing(cfg=config(CONVICTION_SIZING_ENABLED=True),
                                rec=recommendation(prob_tp1=None))
        self.assertEqual(report["qty"], 2.)

    def test_legitimate_zero_risk_preserves_native_fallback_and_is_visible(self):
        report = self.run_sizing(rec=recommendation(risk_pct=0.))
        self.assertEqual(report["raw_risk_pct"], 0.)
        self.assertEqual(report["final_risk_pct"], 1.)
        self.assertEqual(report["qty"], 2.)

    def test_filler_test_count_changes_only_filler_size_choice(self):
        cfg = config(FILLER_FORA_ENABLED=True, FILLER_FORA_TEST_N=2,
                     FILLER_FORA_TEST_SIZE_MULT=.5, FILLER_FORA_SIZE_MULT=.75)
        rec = recommendation(_is_filler=True)
        test = self.run_sizing(cfg=cfg, rec=rec, context={"regime": {}, "filler_test_done": 1})
        after = self.run_sizing(cfg=cfg, rec=rec, context={"regime": {}, "filler_test_done": 2})
        self.assertEqual(test["qty"], 1.)
        self.assertEqual(after["qty"], 1.5)

    def test_regime_uses_native_major_symbol_rule(self):
        cfg = config(REGIME_SIZING_ENABLED=True)
        ctx = {"regime": {"regime": "ALT_RISK_OFF", "downgrade_alt_longs": True}, "filler_test_done": 0}
        alt = self.run_sizing(cfg=cfg, context=ctx)
        major = self.run_sizing(cfg=cfg, context=ctx, rec=recommendation(symbol="BTC/USDT:USDT"))
        self.assertEqual(alt["qty"], 1.)
        self.assertEqual(major["qty"], 2.)

    def test_liquidity_post_reduction_can_be_rejected_by_native_minimum(self):
        report = upstream.replay(make_request(cfg=config(LIQ_TIER_SIZING_ENABLED=True,
            LIVE_SIZE_MULT=.5), rec=recommendation(quote_vol_usd=1_000_000.)))
        self.assertEqual(report["status"], "REJECTED")
        self.assertEqual(report["gaps"], [])

    def test_funding_drag_uses_native_gate_before_sizing(self):
        rec = recommendation(signal={"entry": 100., "indicators": {"atr": 2.},
                                      "derivatives": {"funding_rate_pct": 1.}})
        report = upstream.replay(make_request(cfg=config(FUNDING_EV_ENABLED=True,
            FUNDING_EV_MAX_DRAG_R=.1), rec=rec))
        self.assertEqual(report["status"], "REJECTED")
        self.assertNotIn("_compute_qty", [s["caller"] for s in report["steps"]])


class NativeCacheBoundaryTests(unittest.TestCase):
    def test_exact_timeframe_wins_over_more_confident_fallback(self):
        obs = observations(symbol_rows=[learned_row(1.15, .9, "4h"), learned_row(.8, .5)])
        report = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=obs))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 1.6)

    def test_fallback_ties_keep_observed_insertion_order(self):
        rows = [learned_row(.8, .8, "4h"), learned_row(1.15, .8, "1d")]
        a = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=observations(symbol_rows=rows)))
        b = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=observations(symbol_rows=list(reversed(rows)))))
        self.assertEqual(a["qty"], 1.6)
        self.assertEqual(b["qty"], 2.3)

    def test_exact_invalid_cache_row_is_noop_not_other_timeframe(self):
        obs = observations(symbol_rows=[learned_row(1.15, .9, "4h"), learned_row(True)])
        report = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=obs))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 2.)

    def test_active_unavailable_symbol_cache_cannot_turn_native_failsoft_green(self):
        obs = observations()
        obs["symbol_learning"] = upstream.make_observation(producer="symbol_learning_service.refresh_cache",
            value=None, observed_at_ms=T0, available_at_ms=T0, quality="UNAVAILABLE")
        report = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=obs))
        self.assertEqual(report["status"], "TECHNICAL_GAP")
        self.assertTrue(report["gaps"])

    def test_disabled_unavailable_symbol_cache_does_not_invent_active_requirement(self):
        obs = observations()
        obs["symbol_learning"] = upstream.make_observation(producer="symbol_learning_service.refresh_cache",
            value=None, observed_at_ms=T0, available_at_ms=T0, quality="UNAVAILABLE")
        report = upstream.replay(make_request(obs=obs))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 2.)

    def test_edge_cache_exact_direction_precedes_any(self):
        cfg = config()
        cfg["edge_decay"]["enabled"] = True
        symbol = recommendation()["symbol"]
        obs = observations(decay_rows=[{"symbol": symbol, "direction": "any", "row": {"mult": .5, "reason": "TEST_ONLY"}},
            {"symbol": symbol, "direction": "long", "row": {"mult": .8, "reason": "TEST_ONLY"}}])
        self.assertEqual(upstream.replay(make_request(cfg=cfg, obs=obs))["qty"], 1.6)

    def test_active_unavailable_decay_cache_is_technical_gap(self):
        cfg = config()
        cfg["edge_decay"]["enabled"] = True
        obs = observations()
        obs["edge_decay"] = upstream.make_observation(producer="edge_decay_service.maybe_refresh",
            value=None, observed_at_ms=T0, available_at_ms=T0, quality="UNAVAILABLE")
        report = upstream.replay(make_request(cfg=cfg, obs=obs))
        self.assertEqual(report["status"], "TECHNICAL_GAP")

    def test_captured_empty_cache_is_known_native_noop(self):
        report = upstream.replay(make_request(cfg=enabled_symbol_config(), obs=observations(symbol_rows=[])))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 2.)


class CausalContractTests(unittest.TestCase):
    def test_native_constant_config_closed_and_missing_value_rejected(self):
        cfg = config()
        del cfg["shadow"]["LIVE_SIZE_MULT"]
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SCHEMA"):
            make_request(cfg=cfg)

    def test_unknown_config_field_rejected(self):
        cfg = config()
        cfg["shadow"]["ALLOW_REAL_ORDER"] = True
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SCHEMA"):
            make_request(cfg=cfg)

    def test_nonliteral_flag_rejected(self):
        with self.assertRaisesRegex(ValueError, "UPSTREAM_LITERAL_BOOL"):
            make_request(cfg=config(CONVICTION_SIZING_ENABLED=1))

    def test_future_cache_receipt_cannot_enter_decision(self):
        obs = observations()
        obs["symbol_learning"] = upstream.make_observation(producer="symbol_learning_service.refresh_cache",
            value=[], observed_at_ms=T0, available_at_ms=T0 + 1)
        with self.assertRaisesRegex(ValueError, "UPSTREAM_OBSERVATION_NOT_CAUSAL"):
            make_request(obs=obs)

    def test_observation_hash_drift_rejected_even_with_new_request_hash(self):
        request = make_request()
        request["observations"]["equity"]["value"]["total_usd"] = 999.
        request["source_provenance"]["source_hash"] = upstream.digest(
            {k: request[k] for k in ("recommendation", "context", "observations")})
        with self.assertRaisesRegex(ValueError, "UPSTREAM_OBSERVATION_DRIFT"):
            upstream.replay(reseal(request))

    def test_request_hash_drift_rejected(self):
        request = make_request()
        request["config"]["shadow"]["LIVE_SIZE_MULT"] = .5
        with self.assertRaisesRegex(ValueError, "UPSTREAM_REQUEST_DRIFT"):
            upstream.replay(request)

    def test_raw_recommendation_drift_does_not_preserve_source_provenance(self):
        request = make_request()
        request["recommendation"]["risk_pct"] = 1.5
        with self.assertRaisesRegex(ValueError, "UPSTREAM_PROVENANCE_DRIFT"):
            upstream.replay(reseal(request))

    def test_unavailable_equity_is_technical_gap_not_native_fallback_authority(self):
        obs = observations()
        obs["equity"] = upstream.make_observation(producer="exchange_service.get_equity",
            value=None, observed_at_ms=T0, available_at_ms=T0, quality="UNAVAILABLE")
        report = upstream.replay(make_request(obs=obs))
        self.assertEqual(report["status"], "TECHNICAL_GAP")
        self.assertTrue(report["gaps"])

    def test_active_regime_without_context_cannot_be_complete(self):
        report = upstream.replay(make_request(cfg=config(REGIME_SIZING_ENABLED=True),
            context={"regime": None, "filler_test_done": 0}))
        self.assertEqual(report["status"], "TECHNICAL_GAP")

    def test_disabled_regime_does_not_require_context(self):
        report = upstream.replay(make_request(context={"regime": None, "filler_test_done": 0}))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 2.)

    def test_source_fingerprint_drift_rejected(self):
        request = make_request()
        request["source_fingerprint"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SOURCE_DRIFT"):
            upstream.replay(reseal(request))

    def test_cache_duplicate_identity_rejected_not_last_write_wins(self):
        rows = [learned_row(.8), learned_row(1.15)]
        with self.assertRaisesRegex(ValueError, "UPSTREAM_CACHE_IDENTITY"):
            make_request(obs=observations(symbol_rows=rows))

    def test_bool_as_risk_and_probability_never_converts_to_number(self):
        for field in ("risk_pct", "prob_tp1"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "UPSTREAM_NUMBER"):
                make_request(rec=recommendation(**{field: True}))

    def test_nonfinite_input_rejected_before_native_caller(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                make_request(rec=recommendation(risk_pct=value))

    def test_equity_fallback_is_native_rejection_not_fabricated_live_equity(self):
        obs = observations(equity={"ok": True, "total_usd": 1000., "source": "fallback"})
        report = upstream.replay(make_request(obs=obs))
        self.assertEqual(report["status"], "REJECTED")
        self.assertNotIn("_compute_qty", [s["caller"] for s in report["steps"]])

    def test_equity_source_cache_remains_explicit_and_native_accepted(self):
        obs = observations(equity={"ok": True, "total_usd": 1000., "source": "cache"})
        report = upstream.replay(make_request(obs=obs))
        self.assertEqual(report["status"], "COMPLETE_SIZING")
        self.assertEqual(report["qty"], 2.)

    def test_raw_arguments_and_request_not_mutated_by_funding_stash(self):
        cfg, rec, obs = config(FUNDING_EV_ENABLED=True), recommendation(), observations()
        before = copy.deepcopy((cfg, rec, obs))
        request = make_request(cfg=cfg, rec=rec, obs=obs)
        saved = copy.deepcopy(request)
        upstream.replay(request)
        self.assertEqual((cfg, rec, obs), before)
        self.assertEqual(request, saved)
        self.assertNotIn("_funding_ev_r", request["recommendation"])

    def test_isolated_replay_ignores_host_env_and_has_no_live_authority(self):
        request = make_request()
        with patch.dict(os.environ, {"LIVE_SIZE_MULT": ".01", "EXCHANGE_SHADOW": "false",
                "BINANCE_API_KEY": "TEST_ONLY_NEVER_PASSED", "DATABASE_URL": "TEST_ONLY_NO_DB"}):
            report = upstream.replay(request)
        self.assertEqual(report["qty"], 2.)
        for flag in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"):
            self.assertIs(report[flag], False)
        self.assertEqual(report["input_scope"], upstream.INPUT_SCOPE)
        self.assertEqual(report["blocked_network_attempts"], [])

    def test_repeated_request_has_identical_economic_output(self):
        request = make_request()
        first, second = upstream.replay(request), upstream.replay(request)
        for key in ("qty", "notional", "final_risk_pct", "sizing", "request_hash"):
            self.assertEqual(first[key], second[key])


class ReportBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.request = make_request()
        cls.report = upstream.replay(cls.request)

    def altered(self, field, value):
        result = copy.deepcopy(self.report)
        result[field] = value
        result["report_hash"] = upstream.digest({k: v for k, v in result.items()
                                               if k != "report_hash"})
        return result

    def test_resealed_report_keeps_source_opportunity_and_raw_risk_bound(self):
        for field, value in (("source", "OBSERVED_ACCOUNT_EVENTS"),
                             ("opportunity_id", "OTHER_TEST_ONLY"),
                             ("raw_risk_pct", 1.5), ("raw_risk_pct", True)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, "REPORT_BOUNDARY_DRIFT|UPSTREAM_NUMBER:report.raw_risk_pct"):
                upstream.validate_report(self.altered(field, value), self.request)

    def test_report_fields_are_closed_even_when_resealed(self):
        report = self.altered("apply_live", False)
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SCHEMA:report"):
            upstream.validate_report(report, self.request)
        report = copy.deepcopy(self.report)
        del report["notional"]
        report["report_hash"] = upstream.digest({k: v for k, v in report.items() if k != "report_hash"})
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SCHEMA:report"):
            upstream.validate_report(report, self.request)

    def test_notional_must_match_native_quantity_and_bound_entry(self):
        with self.assertRaisesRegex(ValueError, "UPSTREAM_NOTIONAL_DRIFT"):
            upstream.validate_report(self.altered("notional", 201.), self.request)

    def test_report_cannot_claim_unexecuted_producer_cash_or_cache_writers(self):
        for field, value in (("eligibility_replayed", True), ("cache_writers_replayed", True),
                             ("cash_account_reproduced", True), ("native_region_ast_hash", "0" * 64)):
            policy = copy.deepcopy(self.report["declared_policy"])
            policy[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "UPSTREAM_NATIVE_POLICY_DRIFT"):
                upstream.validate_report(self.altered("declared_policy", policy), self.request)


if __name__ == "__main__":
    unittest.main()
