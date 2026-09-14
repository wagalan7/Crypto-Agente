"""Cobertura V2 75–100 autorizada: >=30 por faixa, sem banco/rede/ordem."""
import asyncio
import copy
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from services import calibration_service as c

V2 = c.CALIBRATION_FORMULA_V2
OLD_BINS = [(15, 31), (31, 36), (36, 40), (40, 44), (44, 48),
            (48, 52), (52, 57), (57, 63), (63, 75)]
TAIL = [(75, 80), (80, 85), (85, 90), (90, 95), (95, 100.1)]


class HighScoreCoverage(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(c, "_SCORE_FORMULA_V2", True),
                        patch.object(c, "SCORE_BINS", c.SCORE_BINS_V2)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        for name in ("getaddrinfo", "create_connection"):
            guard = patch.object(socket, name, side_effect=AssertionError("rede proibida"))
            guard.start()
            self.addCleanup(guard.stop)

    def table(self, score=77., n=30):
        pairs = [(50., "won_tp1")] * 20 + [(50., "lost")] * 20
        pairs += [(score, "won_tp2" if i % 3 else "lost") for i in range(n)]
        return c.compute_calibration_from_pairs(pairs)

    def test_partition_preserves_existing_bins_and_covers_high_scores(self):
        self.assertEqual(c.SCORE_BINS_V2, OLD_BINS + TAIL)
        for score in (75., 78.38, 80., 85., 90., 95., 100.):
            with self.subTest(score=score):
                r = c.probability_for_score(score, V2, self.table(score))
                self.assertEqual(r.status, c.PROB_STATUS_READY)
                self.assertIsNotNone(r.prob_tp1)
                self.assertLessEqual(r.prob_tp2, r.prob_tp1)
        for score in (14.99, 100.1, 999.):
            self.assertEqual(c.probability_for_score(score, V2, self.table()).status,
                             c.PROB_STATUS_SCORE_OUT_OF_RANGE)

    def test_insufficient_high_bin_never_uses_global_prior_or_unavailable(self):
        for n in (0, 1, 29):
            with self.subTest(n=n):
                r = c.probability_for_score(77., V2, self.table(n=n))
                self.assertEqual(r.status, "INSUFFICIENT_BIN_EVIDENCE")
                self.assertIsNone(r.prob_tp1)
                self.assertIsNone(r.prob_tp2)
                self.assertIn(r.status, c.BLOCKING_PROB_STATUSES)
                rec = {"probability_provenance": r.as_provenance(),
                       "prob_tp1": None, "prob_tp2": None}
                self.assertFalse(c.calibration_contract_verdict(
                    rec, require_current_contract=True)["ok"])
                self.assertEqual(c.probabilities_from_contract(rec), (None, None))
                json.dumps(r.as_provenance(), allow_nan=False)

    def test_high_score_without_table_or_global_sample_still_blocks(self):
        for table in (None, {}, {"bins": []}, self.table(n=0),
                      c.compute_calibration_from_pairs([(77., "won_tp1")] * 29)):
            with self.subTest(table=bool(table)):
                r = c.probability_for_score(77., V2, table)
                self.assertEqual(r.status, "INSUFFICIENT_BIN_EVIDENCE")
                self.assertTrue(r.blocking)
                self.assertIsNone(r.prob_tp1)
        self.assertEqual(c.probability_for_score(50., V2, None).status,
                         c.PROB_STATUS_CALIBRATION_UNAVAILABLE)

    def test_ready_high_provenance_requires_own_sample_count(self):
        r = c.probability_for_score(77., V2, self.table())
        self.assertEqual(r.status, c.PROB_STATUS_READY)
        rec = {"score": 77., "prob_tp1": r.prob_tp1, "prob_tp2": r.prob_tp2,
               "probability_provenance": r.as_provenance()}
        self.assertEqual(rec["probability_provenance"]["bin_sample_count"], 30)
        self.assertTrue(c.calibration_contract_verdict(rec, require_current_contract=True)["ok"])
        for count in (None, 29, True, "30", float("nan")):
            bad = copy.deepcopy(rec)
            bad["probability_provenance"]["bin_sample_count"] = count
            with self.subTest(count=count):
                self.assertFalse(c.calibration_contract_verdict(bad, require_current_contract=True)["ok"])
                self.assertEqual(c.probabilities_from_contract(bad), (None, None))

    def test_old_unavailable_high_payload_cannot_bypass_execution_guard(self):
        r = c.probability_for_score(50., V2, None)
        rec = {"score": 77., "prob_tp1": None, "prob_tp2": None,
               "probability_provenance": r.as_provenance()}
        self.assertFalse(c.calibration_contract_verdict(rec, require_current_contract=True)["ok"])

    def test_old_ready_or_wrong_bin_cannot_authorize_high_score(self):
        r = c.probability_for_score(77., V2, self.table())
        base = {"score": 77., "prob_tp1": r.prob_tp1, "prob_tp2": r.prob_tp2,
                "probability_provenance": r.as_provenance()}
        for version, idx in ((c.bins_version(OLD_BINS, V2), 8),
                             (r.bins_version, 1), (r.bins_version, 10)):
            rec = copy.deepcopy(base)
            rec["probability_provenance"].update(bins_version=version, bin_index=idx)
            with self.subTest(version=version, idx=idx):
                self.assertFalse(c.calibration_contract_verdict(rec, require_current_contract=True)["ok"])

    def test_above_100_is_not_counted_in_last_bin(self):
        table = self.table(100.01, 30)
        self.assertEqual(table["bins"][-1]["n_total"], 0)
        self.assertEqual(c.probability_for_score(100.01, V2, table).status,
                         c.PROB_STATUS_SCORE_OUT_OF_RANGE)

    def test_legacy_partition_and_immature_policy_are_unchanged(self):
        with patch.object(c, "_SCORE_FORMULA_V2", False), patch.object(c, "SCORE_BINS", c.SCORE_BINS_LEGACY):
            table = c.compute_calibration_from_pairs([(67., "won_tp1")] * 40)
            self.assertEqual([(b["score_lo"], b["score_hi"]) for b in table["bins"]],
                             c.SCORE_BINS_LEGACY)
            self.assertEqual(c.probability_for_score(77., c.CALIBRATION_FORMULA_LEGACY, table).status,
                             c.PROB_STATUS_READY)
            self.assertEqual(c.probability_for_score(77., c.CALIBRATION_FORMULA_LEGACY, None).status,
                             c.PROB_STATUS_CALIBRATION_UNAVAILABLE)

    def test_live_verdict_observes_new_block_without_order_or_database(self):
        from services import shadow_trade_service as shadow
        for table in (None, self.table(n=29), self.table(n=30)):
            r = c.probability_for_score(77., V2, table)
            rec = {"score": 77., "symbol": "BTC/USDT:USDT", "direction": "LONG",
                   "prob_tp1": r.prob_tp1, "prob_tp2": r.prob_tp2,
                   "probability_provenance": r.as_provenance()}
            verdict = shadow._calibration_contract_verdict(rec, require_current_contract=True)
            self.assertEqual(verdict["ok"], r.ok)
            if not r.ok:
                self.assertEqual(shadow.exec_verdict(rec)["blocked_by"], c.CALIBRATION_CONTRACT_GATE)

    def test_ui_names_insufficient_sample_instead_of_formula_mismatch(self):
        panel = (Path(__file__).resolve().parents[2] / "frontend/src/components/RecommendationsPanel.tsx").read_text()
        self.assertIn("st === 'INSUFFICIENT_BIN_EVIDENCE'", panel)
        self.assertIn("mínimo de 30 observações; entrada automática bloqueada", panel)

    def test_each_high_bin_requires_its_own_30_observations(self):
        for score in (75., 80., 85., 90., 95., 100.):
            table = self.table(score, n=29)
            with self.subTest(score=score):
                self.assertEqual(c.probability_for_score(score, V2, table).status,
                                 "INSUFFICIENT_BIN_EVIDENCE")
        self.assertEqual(c.probability_for_score(80., V2, self.table(77., n=300)).status,
                         "INSUFFICIENT_BIN_EVIDENCE")

    def test_count_is_validated_not_trusted(self):
        for n in (None, True, "30", -1, 30.5, float("nan"), float("inf"), 100000):
            with self.subTest(n=n):
                table = self.table()
                table["bins"][-5]["n_total"] = n
                r = c.probability_for_score(77., V2, table)
                self.assertEqual(r.status, c.PROB_STATUS_INVALID_CALIBRATION_CONTRACT)
                self.assertIsNone(r.prob_tp1)

    def test_adding_empty_tail_does_not_move_lower_probabilities(self):
        pairs = [(30., "lost")] * 70 + [(70., "won_tp2")] * 30
        with patch.object(c, "SCORE_BINS", OLD_BINS):
            before = c.compute_calibration_from_pairs(pairs)
        after = c.compute_calibration_from_pairs(pairs)
        for old, new in zip(before["bins"], after["bins"]):
            for key in ("n_total", "p_calibrated", "p_tp2_calibrated"):
                self.assertEqual(old[key], new[key], key)
        self.assertNotEqual(before["bins_version"], after["bins_version"])
        self.assertEqual(c.probability_for_score(75., V2, before).status,
                         c.PROB_STATUS_SCORE_OUT_OF_RANGE)
        self.assertEqual(c.probability_for_score(74.999, V2, after).status,
                         c.PROB_STATUS_READY)

    def test_old_fingerprint_is_not_reinterpreted_as_extended(self):
        table = self.table()
        table["bins_version"] = c.bins_version(OLD_BINS, V2)
        r = c.probability_for_score(77., V2, table)
        self.assertEqual(r.reason_code, c.PROB_REASON_BINS_VERSION_MISMATCH)
        self.assertIsNone(r.prob_tp1)

    def test_formula_mismatch_still_blocks_high_score(self):
        r = c.probability_for_score(80., c.CALIBRATION_FORMULA_LEGACY, self.table(80.))
        self.assertEqual(r.status, c.PROB_STATUS_FORMULA_MISMATCH)
        self.assertIsNone(r.prob_tp1)

    def test_legacy_helpers_share_count_and_partition_guard(self):
        table = self.table(n=0)
        with patch.dict(c._cache, {"data": table}), \
                patch.object(c, "get_calibration", return_value=table):
            self.assertIsNone(c.prob_tp1_for_score_sync(77.))
            self.assertIsNone(c.prob_tp2_for_score_sync(77.))
            self.assertIsNone(asyncio.run(c.prob_tp1_for_score(77.)))
        mature = self.table()
        with patch.dict(c._cache, {"data": mature}):
            self.assertEqual(c.prob_tp1_for_score_sync(77.),
                             c.probability_for_score(77., V2, mature).prob_tp1)


if __name__ == "__main__":
    unittest.main()
