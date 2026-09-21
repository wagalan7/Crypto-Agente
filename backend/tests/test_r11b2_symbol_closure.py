"""Fechamento R11B2: configuração e inteiros extremos no serviço REAL de símbolos.

Sem DB, rede ou Telegram; cada patch de configuração/cache é restaurado.
"""
from contextlib import ExitStack
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import symbol_learning_service as sls


HUGE = 10 ** 400
VALID = {"n_trades": 60, "wf_avg_r": 0.4, "wf_n_trades": 20, "expiry_pct": 10.0}


class SymbolClosure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            sls, DB_ENABLED=False, SYMBOL_LEARNING_SIZE_ENABLED=True,
            SYMBOL_LEARNING_NOTIFY=False, MIN_TRADES=30, CALIB_FACTOR=0.70,
            SIZE_MULT_MIN=0.75, SIZE_MULT_MAX=1.15, MIN_CONFIDENCE_APPLY=0.25,
            REL_DEADBAND=0.10, _CACHE={}, _CACHE_LOADED=False,
        ))
        self.blocked = []

        def blocked(*args, **kwargs):
            self.blocked.append(True)
            raise AssertionError("DB/rede real proibido neste teste")

        for target in ("db.get_session", "services.symbol_learning_service.get_session",
                       "socket.getaddrinfo", "socket.create_connection", "socket.socket.connect"):
            self.stack.enter_context(patch(target, side_effect=blocked))
        self.addCleanup(lambda: self.assertEqual(self.blocked, []))

    def lookup(self, **fields):
        row = {"timeframe": "4h", "size_quality_mult": 1.10, "confidence": 0.9}
        row.update(fields)
        sls._CACHE = {"AAA": {"4h": row}}
        return sls.get_size_mult("AAA", "4h")

    def test_invalid_deadband_is_noop_for_cached_and_derived_values(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -0.1, 0.6,
                    True, "0.1", None, HUGE):
            with patch.object(sls, "REL_DEADBAND", bad):
                with self.subTest(kind=type(bad).__name__, path="cache"):
                    self.assertEqual(self.lookup()[0], 1.0)
                with self.subTest(kind=type(bad).__name__, path="rank"):
                    self.assertIsNone(sls._mult_from_rank(0.9, 0.0))
                with self.subTest(kind=type(bad).__name__, path="derive"):
                    self.assertIsNone(sls.derive_params(VALID, 0.9))

    def test_derivation_validates_size_config_before_clamp(self):
        for config in ({"SIZE_MULT_MIN": float("nan")}, {"SIZE_MULT_MAX": float("inf")},
                       {"SIZE_MULT_MIN": 0.0}, {"SIZE_MULT_MIN": 1.2, "SIZE_MULT_MAX": 1.1},
                       {"SIZE_MULT_MAX": HUGE}, {"SIZE_MULT_MIN": True}):
            with self.subTest(config=list(config)), patch.multiple(sls, **config):
                self.assertIsNone(sls._mult_from_rank(0.9, 0.0))
                self.assertIsNone(sls.derive_params(VALID, 0.9))

    def test_calibration_config_is_checked_before_arithmetic(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, "0.7", None, HUGE):
            with self.subTest(kind=type(bad).__name__), patch.object(sls, "CALIB_FACTOR", bad):
                self.assertIsNone(sls.derive_params(VALID, 0.9))
        # Ambos operandos finitos podem gerar infinito: não converter em clamp.
        with patch.object(sls, "CALIB_FACTOR", 2.0):
            self.assertIsNone(sls.derive_params(dict(VALID, wf_avg_r=1e308), 0.9))

    def test_oversized_integer_conversion_and_counts_are_rejected(self):
        self.assertIsNone(sls._finite(HUGE))
        self.assertIsNone(sls._finite(-HUGE))
        self.assertIsNone(sls._count(HUGE))
        self.assertIsNone(sls._count(-HUGE))
        for field in VALID:
            with self.subTest(field=field):
                self.assertIsNone(sls.derive_params(dict(VALID, **{field: HUGE}), 0.9))
        self.assertIsNone(sls.derive_params(VALID, HUGE))

    def test_oversized_cached_fields_are_noop_and_status_remains_serializable(self):
        for field in ("size_quality_mult", "confidence", "n_trades", "wf_n_trades",
                      "wf_avg_r", "calibrated_edge", "expiry_pct"):
            with self.subTest(field=field):
                self.assertEqual(self.lookup(**{field: HUGE})[0], 1.0)
                safe = sls._safe_status_row({field: HUGE})
                self.assertIsNone(safe[field])
                self.assertIn(field, safe["invalid_fields"])
                json.dumps(safe, allow_nan=False)

    def test_valid_formulas_and_optional_defaults_are_unchanged(self):
        for deadband in (0.0, 0.1, 0.49, 0.5):
            with patch.object(sls, "REL_DEADBAND", deadband):
                self.assertEqual(self.lookup()[0], 1.10)
                for rank in (0.0, 0.25, 0.5, 0.75, 1.0):
                    for expiry in (0.0, 29.9, 30.0, 45.0, 100.0):
                        d, span = rank - 0.5, max(1e-6, 0.5 - deadband)
                        if abs(d) <= deadband:
                            expected = 1.0
                        elif d > 0:
                            expected = 1.0 + min(1.0, (d - deadband) / span) * (1.15 - 1.0)
                        else:
                            expected = 1.0 - min(1.0, (-d - deadband) / span) * (1.0 - 0.75)
                        expected *= 0.85 if expiry >= 45 else 0.92 if expiry >= 30 else 1.0
                        expected = round(max(0.75, min(1.15, expected)), 4)
                        actual = sls.derive_params(dict(VALID, expiry_pct=expiry), rank)
                        self.assertEqual(actual["size_quality_mult"], expected)
                        self.assertEqual(actual["confidence"], 0.375)
                        self.assertEqual(actual["calibrated_edge"], 0.28)
        for extra in ({}, {"wf_n_trades": None, "expiry_pct": None}):
            actual = sls.derive_params({"n_trades": 60.0, "wf_avg_r": 0.4, **extra}, 0.9)
            self.assertEqual((actual["wf_n_trades"], actual["expiry_pct"], actual["confidence"]),
                             (0, 0.0, 0.25))
        representable = sls.derive_params(dict(VALID, n_trades=10 ** 300, wf_n_trades=10 ** 300), 0.9)
        self.assertEqual(representable["confidence"], 1.0)
        json.dumps(representable, allow_nan=False)

    async def test_oversized_row_does_not_poison_relearn(self):
        rows = []
        for base, overrides in (("BAD_N", {"n_trades": HUGE}), ("GOOD_A", {}),
                                ("BAD_WF_N", {"wf_n_trades": HUGE}),
                                ("GOOD_B", {"wf_avg_r": 0.2})):
            data = dict(VALID, **overrides)
            rows.append(NS(error=None, symbol=base, timeframe="4h", to_dict=lambda d=data: dict(d)))

        class Result:
            def scalars(self):
                return self

            def all(self):
                return rows

            def scalar_one_or_none(self):
                return None

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.execute.return_value = Result()
        session.add = Mock()
        refresh = AsyncMock(return_value=0)
        with patch.object(sls, "DB_ENABLED", True), \
                patch.object(sls, "get_session", return_value=session), \
                patch.object(sls, "refresh_cache", refresh):
            summary = await sls.relearn_all_from_history()
        self.assertNotIn("error", summary)
        self.assertEqual(summary, {"scanned": 4, "learned": 2, "skipped_small": 0,
                                   "skipped_invalid": 2, "bases": 2})
        self.assertEqual([call.args[0].base for call in session.add.call_args_list], ["GOOD_A", "GOOD_B"])
        self.assertEqual([call.args[0].size_quality_mult for call in session.add.call_args_list],
                         [1.0562, 0.9062])
        session.commit.assert_awaited_once()
        refresh.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
