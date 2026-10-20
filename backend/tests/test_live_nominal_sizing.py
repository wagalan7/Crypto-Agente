"""Native nominal risk/leverage and whole pre-guard batch, TEST_ONLY facts.

No app module, environment, database, broker account or network is consulted.
Expected numbers are fixed controls; there is no duplicate leverage formula.
"""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_nominal_sizing_service as nominal

NOW = 1_800_000_000_000


def recommendation(**changes):
    result = {"symbol": "ALFA/USDT:USDT", "timeframe": "1h", "direction": "long", "tier": "A",
              "entry": 100., "stop_loss": 98., "tp1": 104., "tp2": 108., "score": 80.,
              "prob_tp1": .7, "prob_tp2": .4, "edge_tags": [], "edge_score": 0,
              "signal": {"entry": 100., "indicators": {"atr": 2.}}, "warnings": []}
    result.update(changes)
    return result


def batch(n=1, **changes):
    return [{"opportunity_id": "TEST_ONLY_NOMINAL_" + str(i),
             "recommendation": recommendation(symbol=f"ALT{i}/USDT:USDT", **changes)} for i in range(n)]


def regime(btc_change=0., *, quality="CAPTURED", **changes):
    body = {"producer": "regime_service.get_regime_status", "generation": 2,
            "observed_at_ms": NOW - 100, "available_at_ms": NOW, "quality": quality,
            "value": {"btc_24h_pct": btc_change} if quality == "CAPTURED" else None}
    body.update(changes)
    return {**body, "observation_hash": nominal.digest(body)}


def request(rows=None, obs=None, **changes):
    return nominal.make_request(recommendations=batch() if rows is None else rows,
        regime_observation=regime() if obs is None else obs, decision_at_ms=NOW, **changes)


def reseal(value, key="request_hash"):
    value = copy.deepcopy(value)
    value[key] = nominal.digest({k: v for k, v in value.items() if k != key})
    return value


@lru_cache(maxsize=64)
def _report(serialized):
    import json
    return nominal.replay(json.loads(serialized))


def replay(value):
    return copy.deepcopy(_report(nominal.canonical(value)))


class NativeNominalControls(unittest.TestCase):
    def test_tiers_use_native_profiles_and_copy_all_four_fields(self):
        rows = batch(3)
        for row, tier in zip(rows, ("A+", "A", "B")):
            row["recommendation"]["tier"] = tier
        result = replay(request(rows, regime(2.)))
        outputs = [row["recommendation"] for row in result["recommendations"]]
        self.assertEqual([(r["risk_pct"], r["leverage"]) for r in outputs], [(1.5, 8), (1., 5), (.5, 2)])
        self.assertTrue(all((r["margin_pct"], r["stop_distance_pct"]) == (10., 2.) for r in outputs))

    def test_small_stop_caps_native_leverage_per_tier(self):
        rows = batch(3, stop_loss=99.99)
        for row, tier in zip(rows, ("A+", "A", "B")):
            row["recommendation"]["tier"] = tier
        result = replay(request(rows, regime(2.)))
        self.assertEqual([r["recommendation"]["leverage"] for r in result["recommendations"]], [15, 10, 5])

    def test_three_alt_longs_native_throttle(self):
        result = replay(request(batch(3)))
        self.assertEqual(result["status"], "COMPLETE_NOMINAL")
        self.assertEqual([(r["recommendation"]["risk_pct"], r["recommendation"]["leverage"]) for r in result["recommendations"]], [(.66, 3)] * 3)

    def test_five_alt_longs_native_half_and_bankers_round(self):
        result = replay(request(batch(5)))
        # round(5 * .5) = 2, not a new financial rounding policy.
        self.assertEqual([(r["recommendation"]["risk_pct"], r["recommendation"]["leverage"]) for r in result["recommendations"]], [(.5, 2)] * 5)

    def test_four_alt_longs_keep_point_66_not_half(self):
        result = replay(request(batch(4)))
        self.assertTrue(all(r["recommendation"]["risk_pct"] == .66 for r in result["recommendations"]))

    def test_two_alt_longs_are_not_throttled(self):
        result = replay(request(batch(2)))
        self.assertTrue(all(r["recommendation"]["risk_pct"] == 1. for r in result["recommendations"]))

    def test_btc_threshold_includes_both_endpoints(self):
        for value in (-1., 1.):
            with self.subTest(value=value):
                result = replay(request(batch(3), regime(value)))
                self.assertEqual(result["recommendations"][0]["recommendation"]["risk_pct"], .66)

    def test_outside_btc_threshold_does_not_throttle(self):
        for value in (-1.0001, 1.0001):
            with self.subTest(value=value):
                result = replay(request(batch(3), regime(value)))
                self.assertEqual(result["recommendations"][0]["recommendation"]["risk_pct"], 1.)

    def test_btc_and_eth_with_separator_are_majors_and_short_is_not_counted(self):
        rows = batch(4)
        rows[1]["recommendation"]["symbol"] = "BTC/USDT:USDT"
        rows[2]["recommendation"]["symbol"] = "ETH-USDT-SWAP"
        rows[3]["recommendation"].update(direction="short", stop_loss=102., tp1=96., tp2=92.)
        result = replay(request(rows))
        self.assertTrue(all(r["recommendation"]["risk_pct"] == 1. for r in result["recommendations"]))

    def test_raw_compact_btc_eth_preserve_native_alt_classification(self):
        rows = batch(3)
        rows[1]["recommendation"]["symbol"] = "BTCUSDT"
        rows[2]["recommendation"]["symbol"] = "ETHUSDT"
        result = replay(request(rows))
        self.assertTrue(all(r["recommendation"]["risk_pct"] == .66 for r in result["recommendations"]))

    def test_all_pre_guard_recommendations_count_even_if_only_one_later_kept(self):
        full = replay(request(batch(5)))
        kept_later = full["recommendations"][:1]
        self.assertEqual(kept_later[0]["recommendation"]["risk_pct"], .5)
        self.assertEqual(replay(request(batch(1)))["recommendations"][0]["recommendation"]["risk_pct"], 1.)

    def test_legacy_endpoint_never_calls_throttle(self):
        result = replay(request(batch(5), producer_path="get_recommendations"))
        self.assertTrue(all(r["recommendation"]["risk_pct"] == 1. for r in result["recommendations"]))
        counts = {r["function"]: r["calls"] for r in result["native_proofs"]}
        self.assertEqual(counts["_apply_btc_correlation_throttle"], 0)
        self.assertEqual(counts["is_btc_symbol"], 0)

    def test_both_actual_throttle_callers_have_same_sizing(self):
        a = replay(request(batch(3), producer_path="get_recommendations_from_batch"))
        b = replay(request(batch(3), producer_path="get_recommendations_via_vision"))
        self.assertEqual(a["recommendations"], b["recommendations"])
        for report in (a, b):
            self.assertEqual({r["function"]: r["calls"] for r in report["native_proofs"]}["_apply_btc_correlation_throttle"], 1)

    def test_native_warning_only_once_and_other_features_preserved(self):
        rows = batch(3)
        result = replay(request(rows))
        message = result["recommendations"][0]["recommendation"]["warnings"][0]
        rows[0]["recommendation"]["warnings"] = [message]
        result = replay(request(rows))
        self.assertEqual(result["recommendations"][0]["recommendation"]["warnings"], [message])
        for before, after in zip(rows, result["recommendations"]):
            self.assertEqual(before["recommendation"]["signal"], after["recommendation"]["signal"])

    def test_order_identity_input_not_mutated_and_repeat_is_deterministic(self):
        rows = list(reversed(batch(5)))
        body = request(rows)
        snapshot = copy.deepcopy(body)
        first, second = nominal.replay(body), nominal.replay(body)
        self.assertEqual(first, second)
        self.assertEqual(body, snapshot)
        self.assertEqual([r["opportunity_id"] for r in first["recommendations"]], [r["opportunity_id"] for r in rows])

    def test_empty_known_batch_remains_known_empty(self):
        result = replay(request([]))
        self.assertEqual(result["status"], "COMPLETE_NOMINAL")
        self.assertEqual(result["recommendations"], [])

    def test_unavailable_regime_is_gap_without_fabricated_final_values(self):
        result = replay(request(batch(3), regime(quality="UNAVAILABLE")))
        self.assertEqual(result["status"], "TECHNICAL_GAP")
        self.assertEqual(result["reason_code"], "NOMINAL_REGIME_UNAVAILABLE")
        self.assertTrue(all(r["recommendation"] is None for r in result["recommendations"]))

    def test_none_or_missing_btc_change_is_not_zero(self):
        for obs in (regime(None), regime(value={})):
            with self.subTest(obs=obs):
                result = replay(request(batch(3), obs))
                self.assertEqual(result["status"], "TECHNICAL_GAP")
                self.assertEqual(result["reason_code"], "NOMINAL_BTC_24H_UNKNOWN")

    def test_literal_zero_change_is_observed_and_throttles(self):
        result = replay(request(batch(3), regime(0.)))
        self.assertEqual(result["recommendations"][0]["recommendation"]["risk_pct"], .66)


class NominalContractBoundaries(unittest.TestCase):
    def test_native_caller_removing_throttle_is_not_silently_accepted(self):
        path = BACKEND / "services/recommendation_service.py"
        original_read = Path.read_text
        text = original_read(path)
        start = text.index("async def get_recommendations_via_vision(")
        changed = text[:start] + text[start:].replace("_apply_btc_correlation_throttle(recommendations, regime)", "pass", 1)
        with patch.object(Path, "read_text", lambda p, *a, **kw: changed if p == path else original_read(p, *a, **kw)):
            with self.assertRaisesRegex(ValueError, "THROTTLE_PATH_DRIFT"):
                nominal.native_functions()

    def test_native_builder_changing_output_mapping_is_not_silently_accepted(self):
        path = BACKEND / "services/recommendation_service.py"
        original_read = Path.read_text
        changed = original_read(path).replace('leverage=lev["leverage"]', 'leverage=1', 1)
        with patch.object(Path, "read_text", lambda p, *a, **kw: changed if p == path else original_read(p, *a, **kw)):
            with self.assertRaisesRegex(ValueError, "FIELD_COPY_DRIFT"):
                nominal.native_functions()

    def test_outputs_as_inputs_are_forbidden_preventing_double_throttle(self):
        for field in nominal.FORBIDDEN_INPUT_FIELDS:
            rows = batch()
            rows[0]["recommendation"][field] = 1.
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "PRECOMPUTED_OUTPUT"):
                request(rows)

    def test_numeric_bool_strings_nan_inf_are_not_sizing_evidence(self):
        for field in ("entry", "stop_loss", "prob_tp1", "score"):
            for value in (True, False, "1", float("nan"), float("inf"), -float("inf")):
                rows = batch()
                rows[0]["recommendation"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                    request(rows)

    def test_wrong_side_tier_stop_target_quote_timeframe_rejected(self):
        for changes in ({"direction": "BUY"}, {"tier": "C"}, {"stop_loss": 100.},
                        {"stop_loss": 102.}, {"tp1": 90.}, {"symbol": "ALFA/USDT:USDC"},
                        {"symbol": "ALFA/USD"}, {"quote": "USDC"}, {"timeframe": "now"}):
            rows = batch()
            rows[0]["recommendation"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                request(rows)

    def test_btc_change_invalid_numbers_are_refused_before_helper_fail_open(self):
        for value in (True, "0", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                request(obs=regime(value))

    def test_future_or_reversed_observation_refused(self):
        for changes in ({"available_at_ms": NOW + 1}, {"observed_at_ms": NOW + 1}, {"generation": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                request(obs=regime(**changes))

    def test_hashes_detect_request_and_observation_tamper(self):
        body = request()
        body["recommendations"][0]["recommendation"]["tier"] = "B"
        with self.assertRaisesRegex(ValueError, "REQUEST_DRIFT"):
            nominal.validate_request(body)
        body = request()
        body["regime_observation"]["value"]["btc_24h_pct"] = 2.
        with self.assertRaisesRegex(ValueError, "OBSERVATION_DRIFT"):
            nominal.validate_request(reseal(body))

    def test_duplicate_opportunity_and_unknown_schema_refused(self):
        rows = batch(2)
        rows[1]["opportunity_id"] = rows[0]["opportunity_id"]
        with self.assertRaisesRegex(ValueError, "DUPLICATE_OPPORTUNITY"):
            request(rows)
        body = request()
        body["apply_throttle"] = False
        with self.assertRaisesRegex(ValueError, "SCHEMA"):
            nominal.validate_request(reseal(body))

    def test_caller_enum_cannot_be_replaced_with_free_bool(self):
        for path in ("other", False, "", "server"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                request(producer_path=path)

    def test_generation_and_batch_identity_are_bound(self):
        body = request(batch(3), batch_id="BATCH_TEST", generation=2)
        report = replay(body)
        for field, value in (("batch_id", "OTHER_BATCH"), ("generation", 3), ("generation", True)):
            bad = copy.deepcopy(report)
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "REPORT_BINDING"):
                nominal.validate_report(reseal(bad, "report_hash"), body)

    def test_rehashed_economic_output_cannot_pass(self):
        body = request(batch(3))
        report = replay(body)
        for field, value in (("risk_pct", 1.), ("leverage", 5), ("margin_pct", 12.), ("stop_distance_pct", 3.), ("risk_pct", True)):
            bad = copy.deepcopy(report)
            bad["recommendations"][0]["recommendation"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "NATIVE_OUTPUT_DRIFT|NOMINAL_NUMBER"):
                nominal.validate_report(reseal(bad, "report_hash"), body)

    def test_rehashed_symbol_order_proofs_or_features_cannot_pass(self):
        body = request(batch(3))
        report = replay(body)
        for mutation in ("symbol", "order", "calls", "feature", "warning"):
            bad = copy.deepcopy(report)
            if mutation == "symbol":
                bad["recommendations"][0]["recommendation"]["symbol"] = "OTHERUSDT"
            elif mutation == "order":
                bad["recommendations"].reverse()
            elif mutation == "calls":
                bad["native_proofs"][0]["calls"] += 1
            elif mutation == "feature":
                bad["recommendations"][0]["recommendation"]["signal"]["entry"] = 101.
            else:
                bad["recommendations"][0]["recommendation"]["warnings"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                nominal.validate_report(reseal(bad, "report_hash"), body)

    def test_source_drift_and_report_unknown_fields_refused(self):
        body = request()
        report = replay(body)
        with patch.object(nominal, "source_fingerprint", return_value="CHANGED"):
            with self.assertRaisesRegex(ValueError, "SOURCE_DRIFT"):
                nominal.validate_request(body)
        report["unknown"] = 1
        with self.assertRaisesRegex(ValueError, "SCHEMA"):
            nominal.validate_report(reseal(report, "report_hash"), body)

    def test_report_authority_is_never_enabled_even_rehashed(self):
        body = request()
        report = replay(body)
        for key in ("authorizes_live", "promotable", "live_equivalent", "real_study_allowed"):
            self.assertIs(report[key], False)
            for value in (True, 0):
                bad = copy.deepcopy(report)
                bad[key] = value
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "AUTHORITY"):
                    nominal.validate_report(reseal(bad, "report_hash"), body)

    def test_synthetic_source_cannot_be_laundered_by_changing_source_label(self):
        body = request()
        provenance = copy.deepcopy(body["source_provenance"])
        provenance["quality"] = "OBSERVED_ACCOUNT"
        body = request(source="OBSERVED_ACCOUNT_EVENTS", source_provenance=provenance, batch_id="SYNTHETIC_LABEL")
        report = replay(body)
        self.assertIs(report["test_only"], True)
        self.assertIs(report["real_study_allowed"], False)

    def test_declared_source_requires_own_explicit_model_and_batch_id(self):
        body = request()
        provenance = copy.deepcopy(body["source_provenance"])
        provenance.update(quality="DECLARED_MODEL", model_version=nominal.DECLARED_MODEL)
        with self.assertRaisesRegex(ValueError, "BATCH_ID_REQUIRED"):
            request(source="DECLARED_EVENT_TABLE", source_provenance=provenance)
        result = request(source="DECLARED_EVENT_TABLE", source_provenance=provenance, batch_id="DECLARED_BATCH")
        self.assertEqual(replay(result)["source"], "DECLARED_EVENT_TABLE")
        provenance["model_version"] = "OTHER_MODEL"
        with self.assertRaisesRegex(ValueError, "DECLARED_MODEL"):
            request(source="DECLARED_EVENT_TABLE", source_provenance=provenance, batch_id="DECLARED_BATCH")

    def test_credential_fields_are_refused(self):
        rows = batch()
        rows[0]["recommendation"]["api_secret"] = "TEST_ONLY_NOT_A_SECRET"
        with self.assertRaisesRegex(ValueError, "CREDENTIAL"):
            request(rows)

    def test_no_operational_claim_is_sneaked_into_declared_policy(self):
        report = replay(request())
        for key, value in report["declared_policy"].items():
            if key.endswith("replayed") or key.endswith("verified") or key.endswith("reproduced"):
                self.assertIs(value, False)
        self.assertEqual(report["isolation"], {"network": "BLOCKED", "app_imports": "BLOCKED", "environment": "EMPTY"})
        self.assertEqual(report["blocked_network_attempts"], [])


if __name__ == "__main__":
    unittest.main()
