"""R08B: comportamento numérico preservado; captura prospectiva, hermética."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import random
import socket
import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from models.trade_signal import ConfluenceScore, Indicator, TradeSignal
from services import confluence_service as cs
from services import recommendation_service as rs
from services import score_trace_service as trace
from services import snapshot_service as snapshots


def signal(*, conf=60.0, adx=25.0, funding=0.01, timeframe="1h", **extra):
    values = dict(symbol="BTCUSDT", timeframe=timeframe, direction="long", trade_type="day_trade",
                  confidence=.65, entry=100.0, stop_loss=98.0, tp1=103.0, tp2=106.0,
                  tp3=109.0, risk_reward=3.0, patterns=[], indicators=Indicator(adx=adx, atr=2),
                  confluence=ConfluenceScore(total=conf or 0, max_total=100, pct=conf or 0, factors=[])
                  if conf is not None else None,
                  derivatives={"funding_rate_pct": funding} if funding is not None else None,
                  timestamp=1700000000000, signal_strength="strong")
    values.update(extra)
    return TradeSignal(**values)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.net = patch.object(socket, "create_connection", side_effect=AssertionError("IO proibido"))
        self.dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS proibido"))
        self.net_mock = self.net.start()
        self.dns_mock = self.dns.start()
        self.addCleanup(self.net.stop)
        self.addCleanup(self.dns.stop)

    def tearDown(self):
        self.net_mock.assert_not_called()
        self.dns_mock.assert_not_called()

    def test_v2_numerical_parity_independent_reference_and_exact_stages(self):
        rng = random.Random(808)
        with patch.object(rs, "SCORE_FORMULA_V2", True), patch.object(rs, "HIGH_TF_PATTERNS_ENABLED", True):
            for _ in range(120):
                conf, adx, funding = rng.uniform(0, 100), rng.uniform(-5, 65), rng.uniform(-.1, .1)
                sig = signal(conf=conf, adx=adx, funding=funding, timeframe="1d")
                weights = (rs.SCORE_V2_W_CONF, rs.SCORE_V2_W_ADX, rs.SCORE_V2_W_DER)
                comps = (conf, max(0, min(adx, 50)) / 50 * 100,
                         50 - max(-1, min(funding / .05, 1)) * 50)
                num = sum(w * x for w, x in zip(weights, comps) if w > 0)
                den = sum(w for w in weights if w > 0)
                expected_raw = round(max(0, min(100, num / den)), 1)
                expected_base = round(max(0, min(100, expected_raw * 1.10)), 1)
                self.assertEqual(rs._compute_score(sig), expected_base)
                self.assertEqual(sig.r08_score_trace["stages"]["raw_score"]["value"], expected_raw)
                self.assertEqual(sig.r08_score_trace["stages"]["base_score"]["value"], expected_base)
                self.assertEqual(sig.r08_score_trace["stages"]["base_score"]["relevance_multiplier"], 1.1)

    def test_legacy_and_v2_fallback_preserve_scores(self):
        with patch.object(rs, "SCORE_FORMULA_V2", False), patch.object(rs, "HIGH_TF_PATTERNS_ENABLED", False):
            for conf in (0, 40, 77, 100):
                sig = signal(conf=conf, mtf={"alignment_score": .4}, risk_reward=2.0)
                expected = round(conf * .35 + 70 * .25 + (2 / 3 * 100) * .25 + 50 * .10, 1)
                self.assertEqual(rs._compute_score(sig), expected)
                self.assertEqual(sig.r08_score_trace["formula_effective"], "LEGACY_V1")
                self.assertEqual(sig.r08_score_trace["stages"]["raw_score"]["win_bonus"], 0)
        sig = signal(conf=None, adx=None, funding=None)
        with patch.object(rs, "SCORE_FORMULA_V2", True):
            result = rs.compute_score_with_provenance(sig)
        self.assertEqual(result.score, rs._compute_score_legacy(sig))
        self.assertEqual(sig.r08_score_trace["fallback_reason"], "V2_NO_COMPONENTS")
        self.assertTrue(sig.r08_score_trace["fallback_used"])

    def test_confluence_actual_factors_and_calculation_config_frozen(self):
        df = pd.DataFrame({key: [100.0] * 30 for key in ("open", "high", "low", "close", "volume")})
        confluence = cs.calculate_confluence(Indicator(rsi=25, adx=36), [], df, "long", 100)
        capture = deepcopy(confluence.r08_capture)
        self.assertEqual(capture["pct"], confluence.pct)
        self.assertEqual(capture["factor_count"], len(confluence.factors))
        self.assertEqual([f["points"] for f in capture["factors"]], [f.points for f in confluence.factors])
        with patch.dict(cs.WEIGHTS, {"momentum": 999}), patch.dict(cs.PATTERN_EMPIRICAL_WEIGHT, {"double_bottom": 999}):
            self.assertEqual(confluence.r08_capture, capture)
        sig = signal(confluence=confluence)
        rs._compute_score(sig)
        confluence.factors[0].points = 999
        confluence.r08_capture["config"]["weights"]["momentum"] = 999
        self.assertEqual(sig.r08_score_trace["confluence"], capture)

    def test_htf_observed_bonus_and_cap(self):
        sig = signal()
        rs._compute_score(sig)
        with patch.object(rs, "HIGH_TF_CONFIRM_BONUS", 6.0):
            result = rs._score_with_htf_confirm(sig, 97.0, {sig.direction})
        self.assertEqual(result, 100.0)
        observed = sig.r08_score_trace["stages"]["htf_score"]
        self.assertEqual((observed["input_score"], observed["bonus"], observed["value"]), (97, 6, 100))
        self.assertEqual(rs._score_with_htf_confirm(sig, 80.0, set()), 80.0)
        self.assertEqual(sig.r08_score_trace["stages"]["htf_score"]["status"], "NOT_APPLIED")

    def test_selection_observed_actual_key_does_not_replace_candidate_score(self):
        from services import regime_service as regime
        async def run(server):
            candidates = {"15m": signal(conf=90, timeframe="15m", mtf={"counter": True}),
                          "1h": signal(conf=70, timeframe="1h", mtf={"counter": False})}
            async def analyze(*args):
                return candidates[args[-1]]
            target = "_analyze_symbol_tf_server" if server else "_analyze_symbol_tf"
            with patch.object(rs, "SCAN_TFS", ["15m", "1h"]), patch.object(rs, "HIGH_TF_PATTERNS_ENABLED", False), \
                    patch.object(rs, "SCORE_FORMULA_V2", False), patch.object(rs, target, side_effect=analyze), \
                    patch.object(rs, "_attach_htf_ema_trend"), patch.object(regime, "CT_BRAKE_SELECT_PENALTY", 20.0), \
                    patch.object(regime, "symbol_counter_trend", side_effect=lambda mtf, direction: mtf["counter"]):
                best = await (rs._best_tf_for_symbol_server(None, "BTCUSDT") if server else rs._best_tf_for_symbol("BTCUSDT"))
            self.assertEqual(best[0].timeframe, "1h")
            for sig in candidates.values():
                stages = sig.r08_score_trace["stages"]
                expected_pen = 20 if sig.timeframe == "15m" else 0
                self.assertEqual(stages["selection_score"]["value"], stages["htf_score"]["value"] - expected_pen)
            self.assertEqual(best[1], best[0].r08_score_trace["stages"]["htf_score"]["value"])
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_learning_aggregate_is_observed_and_does_not_apply_twice(self):
        from services import learning_service as learning
        sig = signal()
        score = rs._compute_score(sig)
        adjustment = {"enabled": True, "score_multipliers": {"tier_tf": {"A_1h": 1.1}}, "blocked_buckets": []}
        observed = learning.apply_score_adjustment(sig, score, adjustment, tier_provisional="A")
        expected = observed["score"]
        rs._record_learning_trace(sig, score, expected, adjustment, observed)
        frozen = sig.r08_score_trace["stages"]["learning_score"]
        self.assertEqual(frozen["value"], expected)
        self.assertEqual(frozen["input_score"], score)
        self.assertEqual(frozen["multiplier"], observed["multiplier"])
        self.assertEqual(frozen["matched_count"], 1)
        adjustment["score_multipliers"]["tier_tf"]["A_1h"] = 999
        self.assertEqual(sig.r08_score_trace["stages"]["learning_score"], frozen)

    def test_recommendation_preserves_original_trace_after_signal_mutation(self):
        sig = signal(mtf={"alignment_score": -.8})
        with patch.object(rs, "SCORE_FORMULA_V2", False):
            score = rs._compute_score(sig)
            original = deepcopy(sig.r08_score_trace)
            sig.mtf = {"alignment_score": .8}  # fluxo HTF anexa contexto depois do score
            rec = rs._build_recommendation(sig, score, "A")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.score, score)
        self.assertEqual(rec.r08_score_trace["stages"]["raw_score"], original["stages"]["raw_score"])
        self.assertEqual(rec.r08_score_trace["stages"]["final_score"]["value"], score)
        rec.r08_score_trace["config"]["v2_w_conf"] = 999
        self.assertNotEqual(sig.r08_score_trace["config"]["v2_w_conf"], 999)

    def test_snapshot_immutable_prospective_only_and_no_outcome_leakage(self):
        sig = signal()
        score = rs._compute_score(sig)
        payload = trace.recommendation_trace(sig, score, "A")
        payload.update({"realized_r": 5, "future_price": 999, "api_secret": "SECRET"})
        payload["stages"]["raw_score"]["future_price"] = 999
        rec = {"signal": sig.model_dump(), "score": score, trace.KEY: payload}
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        features = snapshots._extract_features(rec, now)
        saved = deepcopy(features[trace.KEY])
        payload["config"]["v2_w_conf"] = 999
        self.assertEqual(features[trace.KEY], saved)
        text = json.dumps(saved, allow_nan=False)
        for forbidden in ("realized_r", "future_price", "api_secret", "SECRET"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn(trace.KEY, snapshots._extract_features({"signal": {}}, now))

    def test_allowlist_nonfinite_unknown_and_size_bound(self):
        source = {"version": trace.VERSION, "formula_requested": "SECRET", "config": {"v2_w_conf": math.inf},
                  "confluence": {"factors": [{"category": "SECRET", "points": math.nan, "description": "SECRET"}] * 500},
                  "stages": {"raw_score": {"status": "OBSERVED", "value": math.nan, "conf_input": True},
                             "future_outcome": {"value": 100}}}
        frozen = trace.freeze_trace(source)
        stage = frozen["stages"]["raw_score"]
        self.assertIsNone(stage["value"])
        self.assertIsNone(stage["conf_input"])
        self.assertIn("value", stage["missing_fields"])
        self.assertTrue(frozen["confluence"]["factors_truncated"])
        self.assertEqual(len(frozen["confluence"]["factors"]), trace.MAX_FACTORS)
        data = json.dumps(frozen, allow_nan=False)
        self.assertLess(len(data.encode()), trace.MAX_BYTES)
        self.assertNotIn("SECRET", data)
        self.assertNotIn("future_outcome", data)
        self.assertEqual(frozen["stages"]["execution_score"]["status"], "NOT_OBSERVED")

    def test_execution_api_records_exact_observed_score_and_detaches(self):
        source = trace.freeze_trace(None)
        frozen = deepcopy(source)
        result = trace.append_execution_score(source, recommendation_score=67.3, execution_score=75.3,
                                              delta=8.0, enabled=True, cap=20, score_min=57)
        self.assertEqual(source, frozen)
        self.assertEqual(result["stages"]["execution_score"]["value"], 75.3)
        self.assertEqual(result["stages"]["execution_score"]["delta"], 8)

    def test_recalculation_failure_never_reuses_old_trace_and_serializes_once(self):
        sig = signal()
        score = rs._compute_score(sig)
        original = deepcopy(sig.r08_score_trace)
        self.assertNotIn(trace.KEY, sig.model_dump())
        self.assertNotIn("r08_capture", sig.model_dump()["confluence"])
        with patch.object(trace, "new_trace", side_effect=RuntimeError("failure")):
            self.assertEqual(rs._compute_score(sig), score)
        self.assertIsNone(sig.r08_score_trace)
        self.assertIsNotNone(original)

    def test_annotation_failure_cannot_change_score_tier_or_recommendation(self):
        sig = signal()
        reference = rs.compute_score_with_provenance(sig, capture_trace=False)
        tier = rs._classify_tier(sig, reference.score)
        for method in ("new_trace", "record_stage", "finish_trace", "freeze_trace"):
            with patch.object(trace, method, side_effect=RuntimeError("annotation failure")):
                actual = rs.compute_score_with_provenance(signal())
                self.assertEqual(actual, reference)
                self.assertEqual(rs._classify_tier(signal(), actual.score), tier)
        with patch.object(trace, "recommendation_trace", side_effect=RuntimeError("annotation failure")):
            self.assertEqual(rs._build_recommendation(sig, reference.score, "A").score, reference.score)
        with patch.object(trace, "snapshot_annotation", side_effect=RuntimeError("annotation failure")):
            self.assertNotIn(trace.KEY, snapshots._extract_features({"signal": {}}, datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
