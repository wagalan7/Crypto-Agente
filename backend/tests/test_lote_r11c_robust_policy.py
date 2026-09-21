"""R11C — política robusta versionada (simulação). Hermético e determinístico.

Cada achado R11A pendente tem teste próprio; o default operacional continua na
política legada e isso é verificado.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket as _socket
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R11C")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import robust_policy_service as rp   # noqa: E402

DAY = 86_400_000
NOW = 1_760_000_000_000


def obs(r=0.5, days_ago=1, symbol="BTC", direction="long", trigger=None,
        population=rp.POPULATION_SHADOW, timeframe="4h"):
    resolved = NOW - int(days_ago * DAY)
    return rp.Observation(symbol=symbol, timeframe=timeframe, direction=direction,
                          trigger_ms=trigger if trigger is not None else resolved - 3_600_000,
                          outcome_r=r, resolved_at_ms=resolved, population=population)


class SampleIdentity(unittest.TestCase):
    def test_repeated_attempts_are_one_opportunity(self):
        same = obs(trigger=NOW - DAY)
        sample = rp.build_sample([same, same, obs(trigger=NOW - 2 * DAY)],
                                 population=rp.POPULATION_SHADOW)
        self.assertEqual(sample.n, 2)
        self.assertEqual(sample.duplicates_dropped, 1)

    def test_populations_never_mix(self):
        rows = [obs(population=rp.POPULATION_SHADOW), obs(population=rp.POPULATION_REAL, trigger=1),
                obs(population=rp.POPULATION_BACKTEST, trigger=2)]
        shadow = rp.build_sample(rows, population=rp.POPULATION_SHADOW)
        real = rp.build_sample(rows, population=rp.POPULATION_REAL)
        self.assertEqual((shadow.n, real.n), (1, 1))
        self.assertEqual(shadow.invalid_dropped, 2)

    def test_invalid_direction_and_result_never_complete_the_sample(self):
        rows = [obs(direction="LONG", trigger=1), obs(direction=None, trigger=2),
                obs(r=float("nan"), trigger=3), obs(r=None, trigger=4), obs(trigger=5)]
        sample = rp.build_sample(rows, population=rp.POPULATION_SHADOW)
        self.assertEqual(sample.n, 1)
        self.assertEqual(sample.invalid_dropped, 4)

    def test_evidence_key_changes_only_with_new_evidence(self):
        first = rp.build_sample([obs(trigger=1), obs(trigger=2)], population=rp.POPULATION_SHADOW)
        repeat = rp.build_sample([obs(trigger=2), obs(trigger=1)], population=rp.POPULATION_SHADOW)
        grown = rp.build_sample([obs(trigger=1), obs(trigger=2), obs(trigger=3)],
                                population=rp.POPULATION_SHADOW)
        self.assertEqual(first.evidence_key, repeat.evidence_key)
        self.assertNotEqual(first.evidence_key, grown.evidence_key)

    def test_labels_distinguish_neutral_from_dormant(self):
        self.assertEqual(rp.build_sample([], population=rp.POPULATION_SHADOW).label,
                         rp.LABEL_NO_EVIDENCE)
        self.assertEqual(rp.build_sample([obs(r=0.0)], population=rp.POPULATION_SHADOW).label,
                         rp.LABEL_NEUTRAL)


class CausalTimeReference(unittest.TestCase):
    def test_same_reference_for_learning_and_applying(self):
        noon = int(datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(rp.session_of(noon), "Europe")
        self.assertEqual(rp.session_of(noon + 5 * 3_600_000), "NY")
        for bad in (None, 0, -1, True, 1.5, "x"):
            self.assertIsNone(rp.session_of(bad))

    def test_coverage_is_reported_and_history_not_reinterpreted(self):
        coverage = rp.time_reference_coverage([obs(trigger=1_700_000_000_000), obs(trigger=0)])
        self.assertEqual(coverage["observations"], 2)
        self.assertEqual(coverage["covered"], 1)
        self.assertEqual(coverage["coverage_pct"], 50.0)
        self.assertFalse(coverage["legacy_reinterpreted"])
        self.assertEqual(coverage["reference"], rp.TIME_REFERENCE)


class DisjointDecay(unittest.TestCase):
    def sample(self, baseline_r, recent_r, baseline_n=25, recent_n=10):
        rows = [obs(r=baseline_r, days_ago=30, trigger=i) for i in range(baseline_n)]
        rows += [obs(r=recent_r, days_ago=2, trigger=1000 + i) for i in range(recent_n)]
        return rp.build_sample(rows, population=rp.POPULATION_SHADOW)

    def test_severe_recent_loss_still_reduces_exposure(self):
        moderate, moderate_details = rp.decay_multiplier(self.sample(0.3, -0.1), now_ms=NOW)
        severe, severe_details = rp.decay_multiplier(self.sample(0.3, -1.0), now_ms=NOW)
        self.assertLess(moderate, 1.0)
        self.assertTrue(severe_details["applied"])
        self.assertLessEqual(severe, moderate)        # pior recente ⇒ corte maior
        self.assertEqual(severe, rp.DecayConfig().mult_min)
        self.assertGreater(severe_details["baseline_mean_r"], 0.1)   # baseline intacto

    def test_windows_are_disjoint(self):
        sample = self.sample(0.3, -1.0)
        baseline, recent = rp.split_windows(sample, now_ms=NOW, config=rp.DecayConfig())
        self.assertEqual(baseline.n, 25)
        self.assertEqual(recent.n, 10)
        identities = {o.identity for o in baseline.observations} & {o.identity for o in recent.observations}
        self.assertEqual(identities, set())

    def test_thresholds_and_insufficient_samples(self):
        self.assertEqual(rp.decay_multiplier(self.sample(0.3, -0.5, recent_n=7), now_ms=NOW)[1]["reason_code"],
                         "RECENT_SAMPLE_INSUFFICIENT")
        self.assertEqual(rp.decay_multiplier(self.sample(0.3, -0.5, baseline_n=19), now_ms=NOW)[1]["reason_code"],
                         "BASELINE_SAMPLE_INSUFFICIENT")
        self.assertEqual(rp.decay_multiplier(self.sample(0.05, -0.5), now_ms=NOW)[1]["reason_code"],
                         "BASELINE_WITHOUT_EDGE")
        self.assertEqual(rp.decay_multiplier(self.sample(0.3, 0.1), now_ms=NOW)[1]["reason_code"],
                         "RECENT_OK")
        multiplier, _ = rp.decay_multiplier(self.sample(0.3, -5.0), now_ms=NOW)
        self.assertEqual(multiplier, rp.DecayConfig().mult_min)   # piso respeitado

    def test_decay_never_increases_exposure(self):
        for recent in (-0.05, -0.15, -0.3, -2.0):
            multiplier, _ = rp.decay_multiplier(self.sample(0.5, recent), now_ms=NOW)
            self.assertLessEqual(multiplier, 1.0)

    def test_cache_empty_is_not_an_error(self):
        config = rp.DecayConfig()
        self.assertEqual(rp.cache_verdict(cached_at_ms=NOW - 60_000, now_ms=NOW, config=config, empty=True),
                         "FRESH_EMPTY")
        self.assertEqual(rp.cache_verdict(cached_at_ms=NOW - 3_600_000, now_ms=NOW, config=config),
                         "REFRESH_REQUIRED")
        self.assertEqual(rp.cache_verdict(cached_at_ms=None, now_ms=NOW, config=config),
                         "REFRESH_REQUIRED")
        self.assertEqual(rp.cache_verdict(cached_at_ms=NOW, now_ms=NOW, config=config, last_error=True),
                         "ERROR_KEEP_PREVIOUS")


class Hysteresis(unittest.TestCase):
    def advance(self, progress, *, now_ms, evidence, contrary=False, required=3):
        return rp.advance_hysteresis(progress, symbol="AAA", action="promote", now_ms=now_ms,
                                     period_seconds=21600, evidence_key=evidence,
                                     required_periods=required, universe_source="db",
                                     contrary_evidence=contrary)

    def test_repeated_calls_in_the_same_period_do_not_advance(self):
        progress, report = self.advance(None, now_ms=NOW, evidence="e1")
        self.assertEqual((report["periods"], report["ready"]), (1, False))
        for _ in range(5):
            progress, report = self.advance(progress, now_ms=NOW + 60_000, evidence="e2")
            self.assertEqual(report["reason_code"], "SAME_PERIOD")
            self.assertEqual(report["periods"], 1)

    def test_new_period_without_new_evidence_does_not_advance(self):
        progress, _ = self.advance(None, now_ms=NOW, evidence="e1")
        progress, report = self.advance(progress, now_ms=NOW + 21600_000, evidence="e1")
        self.assertEqual(report["reason_code"], "NO_NEW_EVIDENCE")
        self.assertEqual(report["periods"], 1)

    def test_promotion_requires_periods_with_new_evidence(self):
        progress, report = self.advance(None, now_ms=NOW, evidence="e1")
        progress, report = self.advance(progress, now_ms=NOW + 21600_000, evidence="e2")
        self.assertEqual(report["periods"], 2)
        self.assertFalse(report["ready"])
        progress, report = self.advance(progress, now_ms=NOW + 43200_000, evidence="e3")
        self.assertTrue(report["ready"])
        self.assertEqual(report["periods"], 3)

    def test_contrary_evidence_and_source_change_reset(self):
        progress, _ = self.advance(None, now_ms=NOW, evidence="e1")
        progress, _ = self.advance(progress, now_ms=NOW + 21600_000, evidence="e2")
        reset, report = self.advance(progress, now_ms=NOW + 43200_000, evidence="e3", contrary=True)
        self.assertEqual((reset.periods, report["reason_code"]), (0, "CONTRARY_EVIDENCE_RESET"))
        moved, report = rp.advance_hysteresis(progress, symbol="AAA", action="promote",
                                              now_ms=NOW + 43200_000, period_seconds=21600,
                                              evidence_key="e4", required_periods=3,
                                              universe_source="env")
        self.assertEqual((moved.periods, report["reason_code"]), (1, "UNIVERSE_SOURCE_CHANGED"))

    def test_action_change_restarts_the_sequence(self):
        progress, _ = self.advance(None, now_ms=NOW, evidence="e1")
        demote, report = rp.advance_hysteresis(progress, symbol="AAA", action="demote",
                                               now_ms=NOW + 21600_000, period_seconds=21600,
                                               evidence_key="e2", required_periods=3,
                                               universe_source="db")
        self.assertEqual((demote.action, demote.periods), ("demote", 1))
        self.assertEqual(report["reason_code"], "SEQUENCE_STARTED")

    def test_period_key_is_clock_based_not_call_based(self):
        self.assertEqual(rp.period_key(NOW, period_seconds=21600),
                         rp.period_key(NOW + 1000, period_seconds=21600))
        self.assertNotEqual(rp.period_key(NOW, period_seconds=21600),
                            rp.period_key(NOW + 21600_000, period_seconds=21600))
        with self.assertRaises(ValueError):
            rp.period_key(NOW, period_seconds=0)


class LearnedGeneration(unittest.TestCase):
    def rows(self, **over):
        row = {"generation": "g2", "confidence": 0.9, "size_quality_mult": 1.10}
        row.update(over)
        return {"4h": row}

    def test_only_current_generation_and_exact_timeframe_apply(self):
        applied, details = rp.learned_multiplier(self.rows(), timeframe="4h", current_generation="g2",
                                                 min_confidence=0.25, clamp=(0.75, 1.15))
        self.assertEqual((applied, details["applied"]), (1.10, True))
        stale, details = rp.learned_multiplier(self.rows(generation="g1"), timeframe="4h",
                                               current_generation="g2", min_confidence=0.25,
                                               clamp=(0.75, 1.15))
        self.assertEqual((stale, details["reason_code"]), (1.0, "GENERATION_STALE"))
        other_tf, details = rp.learned_multiplier(self.rows(), timeframe="1h", current_generation="g2",
                                                  min_confidence=0.25, clamp=(0.75, 1.15))
        self.assertEqual((other_tf, details["reason_code"]), (1.0, "NO_ROW_FOR_TIMEFRAME"))

    def test_invalid_numbers_and_missing_generation_are_not_applied(self):
        for over, reason in ((dict(confidence=float("nan")), "ROW_NUMERICALLY_INVALID"),
                             (dict(size_quality_mult=0.0), "ROW_NUMERICALLY_INVALID"),
                             (dict(confidence=0.1), "CONFIDENCE_BELOW_MINIMUM"),
                             (dict(generation=None), "ROW_WITHOUT_GENERATION")):
            with self.subTest(over=over):
                value, details = rp.learned_multiplier(self.rows(**over), timeframe="4h",
                                                       current_generation="g2", min_confidence=0.25,
                                                       clamp=(0.75, 1.15))
                self.assertEqual((value, details["reason_code"]), (1.0, reason))
        self.assertEqual(rp.generation_eligible("g1", None), (False, "GENERATION_UNKNOWN"))

    def test_fallback_of_1_0_is_documented_as_layer_not_applied(self):
        verdict = rp.reduction_verdict(1.0, {"applied": False, "reason_code": "RECENT_OK"})
        self.assertEqual(verdict["multiplier"], 1.0)
        self.assertIn("NÃO aplicada", verdict["meaning"])
        self.assertFalse(verdict["applies_live"])
        self.assertFalse(verdict["risk_limits_changed"])


class Merit(unittest.TestCase):
    def windows(self, means, n=12):
        return [rp.build_sample([obs(r=mean, trigger=index * 1000 + i) for i in range(n)],
                                population=rp.POPULATION_SHADOW)
                for index, mean in enumerate(means)]

    def test_promotion_requires_stability_sample_costs_and_liquidity(self):
        verdict = rp.merit_verdict(windows=self.windows([0.4, 0.3, 0.35]), net_ev_r=0.3,
                                   uncertainty_r=0.1, costs_known=True, liquidity_ok=True,
                                   quarantine_done=True)
        self.assertEqual(verdict["verdict"], rp.VERDICT_PROMOTE)
        self.assertEqual(verdict["reason_codes"], [])
        self.assertFalse(verdict["applies_live"])

    def test_each_missing_requirement_blocks_promotion(self):
        base = dict(windows=self.windows([0.4, 0.3, 0.35]), net_ev_r=0.3, uncertainty_r=0.1,
                    costs_known=True, liquidity_ok=True, quarantine_done=True)
        cases = {
            "NET_EV_NOT_COMPARABLE": dict(costs_known=False),
            "LIQUIDITY_NOT_PROVEN": dict(liquidity_ok=False),
            "OBSERVATION_QUARANTINE_PENDING": dict(quarantine_done=False),
            "UNCERTAINTY_TOO_WIDE": dict(uncertainty_r=0.9),
            "EV_UNAVAILABLE": dict(net_ev_r=None),
            "WINDOW_SAMPLE_INSUFFICIENT": dict(windows=self.windows([0.4, 0.3, 0.35], n=5)),
            "SAMPLE_BELOW_MINIMUM": dict(windows=self.windows([0.4, 0.3, 0.35], n=9)),
        }
        for reason, over in cases.items():
            with self.subTest(reason=reason):
                verdict = rp.merit_verdict(**{**base, **over})
                self.assertIn(reason, verdict["reason_codes"])
                self.assertNotEqual(verdict["verdict"], rp.VERDICT_PROMOTE)

    def test_false_winner_in_one_window_does_not_promote(self):
        verdict = rp.merit_verdict(windows=self.windows([1.2, -0.3, -0.2]), net_ev_r=0.2,
                                   uncertainty_r=0.1, costs_known=True, liquidity_ok=True,
                                   quarantine_done=True)
        self.assertNotEqual(verdict["verdict"], rp.VERDICT_PROMOTE)
        self.assertFalse(verdict["stable_windows"])

    def test_multiple_candidates_raise_the_bar(self):
        single = rp.MeritConfig(candidates_considered=1)
        many = rp.MeritConfig(candidates_considered=16)
        self.assertLess(rp.bonferroni_threshold(single), rp.bonferroni_threshold(many))
        verdict = rp.merit_verdict(windows=self.windows([0.06, 0.07, 0.06]), net_ev_r=0.06,
                                   uncertainty_r=0.1, costs_known=True, liquidity_ok=True,
                                   quarantine_done=True, config=many)
        self.assertIn("EV_BELOW_CORRECTED_THRESHOLD", verdict["reason_codes"])

    def test_temporal_discount_weights_recent_windows_more(self):
        config = rp.MeritConfig()
        improving = rp.discounted_ev([0.0, 0.0, 0.6], config=config)
        worsening = rp.discounted_ev([0.6, 0.0, 0.0], config=config)
        self.assertGreater(improving, worsening)
        self.assertIsNone(rp.discounted_ev([0.1, None], config=config))

    def test_liquidity_unavailable_never_authorises_promotion(self):
        self.assertEqual(rp.liquidity_verdict("AAA", liquidity_universe=None, available=False),
                         (False, "LIQUIDITY_UNAVAILABLE"))
        self.assertEqual(rp.liquidity_verdict("AAA", liquidity_universe=[], available=True),
                         (False, "LIQUIDITY_UNIVERSE_EMPTY"))
        self.assertEqual(rp.liquidity_verdict("AAA", liquidity_universe=["AAA"], available=True),
                         (True, "LIQUIDITY_OK"))
        self.assertEqual(rp.liquidity_verdict("BBB", liquidity_universe=["AAA"], available=True),
                         (False, "BELOW_LIQUIDITY_FLOOR"))


class DefaultsAndIsolation(unittest.TestCase):
    def test_policy_is_inactive_by_default(self):
        os.environ.pop(rp.POLICY_SELECTOR_ENV, None)
        self.assertEqual(rp.selected_policy(), rp.POLICY_LEGACY)
        self.assertFalse(rp.robust_policy_enabled())
        for value in ("1", "true", "robust", "R11C"):
            with patch.dict(os.environ, {rp.POLICY_SELECTOR_ENV: value}):
                self.assertFalse(rp.robust_policy_enabled())
        with patch.dict(os.environ, {rp.POLICY_SELECTOR_ENV: rp.POLICY_VERSION}):
            self.assertTrue(rp.robust_policy_enabled())

    #: Leitor de manifesto permitido: compõe o resumo somente-leitura do lote
    #: (mesmo papel que já tem para o laboratório R10A). Só pode ler versão e
    #: seletor — nunca as funções de decisão.
    MANIFEST_READERS = {"research_batch_service.py"}
    DECISION_FUNCTIONS = ("merit_verdict", "reduction_verdict", "learned_multiplier",
                          "advance_hysteresis", "decay_multiplier", "liquidity_verdict",
                          "build_sample", "cache_verdict")

    def test_no_live_consumer_imports_the_new_policy(self):
        import ast

        offenders = []
        for path in (BACKEND / "services").glob("*.py"):
            if path.name == "robust_policy_service.py":
                continue
            source = path.read_text(encoding="utf-8")
            if "robust_policy_service" not in source:
                continue
            if path.name not in self.MANIFEST_READERS:
                offenders.append(path.name)
                continue
            # CÓDIGO, não prosa: o leitor só toca versão/seletor.
            tree = ast.parse(source)
            usados = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            for proibida in self.DECISION_FUNCTIONS:
                self.assertNotIn(proibida, usados, f"{path.name}: {proibida}")
        self.assertEqual(offenders, [])
        self.assertNotIn("robust_policy_service", (BACKEND / "main.py").read_text())

    def test_core_is_pure_and_has_no_io(self):
        source = (BACKEND / "services" / "robust_policy_service.py").read_text()
        for forbidden in ("get_session", "httpx", "select(", "create_task", "datetime.now("):
            self.assertNotIn(forbidden, source)
        self.assertIn("os.getenv(POLICY_SELECTOR_ENV", source)

    def test_verdicts_are_serialisable_and_never_live(self):
        verdict = rp.merit_verdict(windows=[], net_ev_r=None, uncertainty_r=None,
                                   costs_known=False, liquidity_ok=False, quarantine_done=False)
        json.dumps(verdict, allow_nan=False)
        self.assertFalse(verdict["applies_live"])
        self.assertFalse(verdict["risk_limits_changed"])

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
