"""P05.1T — telemetria prospectiva de evidência (observação pura).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Nenhuma exchange, banco
externo, Railway ou credencial real. Só fronteiras externas são mockadas — as
funções puras sob teste rodam de verdade.
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.1T (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import snapshot_service as ss                    # noqa: E402
from services import strategy_evidence_service as p05          # noqa: E402


T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
T0_MS = int(T0.timestamp() * 1000)
FIVE_MIN = 300_000


def _snap(*, entry=100.0, stop=95.0, direction="long", tp1=110.0, tp2=120.0,
          features=None, created_at=T0, **kw):
    base = dict(
        id=1, symbol="BTC/USDT:USDT", timeframe="1h", tier="A", direction=direction,
        entry=entry, stop_loss=stop, tp1=tp1, tp2=tp2, status="open",
        created_at=created_at, last_check_at=None, tp1_hit_at=None,
        peak_price_since_tp1=None, outcome_at=None, outcome_price=None,
        realized_r=None, features=features if features is not None else {},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _candles(*rows) -> pd.DataFrame:
    """rows = (ts_ms, high, low, close)"""
    return pd.DataFrame(
        [{"timestamp": ts, "open": (h + l) / 2, "high": h, "low": l,
          "close": c, "volume": 1.0} for ts, h, l, c in rows]
    )


# ════════════════════════════════════════════════════════════════════════════
#  MAE/MFE — LONG
# ════════════════════════════════════════════════════════════════════════════
class MaeMfeLongTests(unittest.TestCase):
    def _trace(self, **kw):
        return ss.new_path_trace(_snap(entry=100.0, stop=95.0, direction="long", **kw))

    def test_formula_long(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 110.0, 90.0)     # risk_unit = 5
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertAlmostEqual(feats["mfe_r"], (110.0 - 100.0) / 5.0)   # +2.0
        self.assertAlmostEqual(feats["mae_r"], (100.0 - 90.0) / 5.0)    # +2.0
        self.assertAlmostEqual(feats["best_price"], 110.0)
        self.assertAlmostEqual(feats["worst_price"], 90.0)

    def test_melhor_e_pior_incluem_o_entry(self):
        """Candle inteiramente acima do entry não cria MAE negativa."""
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 108.0, 104.0)
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertAlmostEqual(feats["worst_price"], 100.0)   # entry é o piso
        self.assertAlmostEqual(feats["mae_r"], 0.0)
        self.assertGreaterEqual(feats["mfe_r"], 0.0)

    def test_monotonicidade_long(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 110.0, 90.0)
        first = ss.merge_p05_path({}, t)["p05_path"]
        ss.path_observe_candle(t, T0_MS + FIVE_MIN, 102.0, 99.0)   # candle "menor"
        second = ss.merge_p05_path({}, t)["p05_path"]
        self.assertGreaterEqual(second["mfe_r"], first["mfe_r"])
        self.assertGreaterEqual(second["mae_r"], first["mae_r"])

    def test_nunca_negativo(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 100.0, 100.0)
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertGreaterEqual(feats["mfe_r"], 0.0)
        self.assertGreaterEqual(feats["mae_r"], 0.0)


# ════════════════════════════════════════════════════════════════════════════
#  MAE/MFE — SHORT
# ════════════════════════════════════════════════════════════════════════════
class MaeMfeShortTests(unittest.TestCase):
    def _trace(self):
        return ss.new_path_trace(_snap(entry=100.0, stop=105.0, direction="short",
                                       tp1=90.0, tp2=80.0))

    def test_formula_short(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 110.0, 90.0)     # risk_unit = 5
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertAlmostEqual(feats["mfe_r"], (100.0 - 90.0) / 5.0)    # a favor = queda
        self.assertAlmostEqual(feats["mae_r"], (110.0 - 100.0) / 5.0)   # contra = alta
        self.assertAlmostEqual(feats["best_price"], 90.0)
        self.assertAlmostEqual(feats["worst_price"], 110.0)

    def test_melhor_e_pior_incluem_o_entry_short(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 96.0, 92.0)      # tudo abaixo do entry
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertAlmostEqual(feats["worst_price"], 100.0)
        self.assertAlmostEqual(feats["mae_r"], 0.0)

    def test_monotonicidade_short(self):
        t = self._trace()
        ss.path_observe_candle(t, T0_MS, 110.0, 90.0)
        first = ss.merge_p05_path({}, t)["p05_path"]
        ss.path_observe_candle(t, T0_MS + FIVE_MIN, 101.0, 99.0)
        second = ss.merge_p05_path({}, t)["p05_path"]
        self.assertGreaterEqual(second["mfe_r"], first["mfe_r"])
        self.assertGreaterEqual(second["mae_r"], first["mae_r"])


# ════════════════════════════════════════════════════════════════════════════
#  VALIDAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class ValidationTests(unittest.TestCase):
    def test_entry_invalida(self):
        for entry in (None, 0.0, -5.0, float("nan"), float("inf")):
            self.assertIsNone(ss.new_path_trace(_snap(entry=entry)), f"entry={entry}")

    def test_stop_invalido(self):
        for stop in (None, 0.0, -5.0, float("nan"), float("inf")):
            self.assertIsNone(ss.new_path_trace(_snap(stop=stop)), f"stop={stop}")

    def test_risk_unit_zero(self):
        self.assertIsNone(ss.new_path_trace(_snap(entry=100.0, stop=100.0)))

    def test_direction_invalida(self):
        for d in ("", None, "buy", "LONG ", "sell", "flat"):
            self.assertIsNone(ss.new_path_trace(_snap(direction=d)), f"dir={d}")

    def test_high_low_invalidos(self):
        t = ss.new_path_trace(_snap())
        self.assertFalse(ss.path_observe_candle(t, T0_MS, 90.0, 110.0))   # high < low
        self.assertFalse(ss.path_observe_candle(t, T0_MS, 0.0, 0.0))      # não positivo
        self.assertFalse(ss.path_observe_candle(t, T0_MS, -1.0, -2.0))
        self.assertEqual(t["observed_candles"], 0)

    def test_nan_inf_nunca_persistidos(self):
        t = ss.new_path_trace(_snap())
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(ss.path_observe_candle(t, T0_MS, bad, 90.0))
            self.assertFalse(ss.path_observe_candle(t, T0_MS, 110.0, bad))
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertIsNone(feats["mae_r"])
        self.assertIsNone(feats["mfe_r"])

    def test_ausencia_nao_vira_zero(self):
        """Snapshot novo sem candle: MAE/MFE NULL, não 0.0."""
        t = ss.new_path_trace(_snap())
        feats = ss.merge_p05_path({}, t)["p05_path"]
        self.assertEqual(feats["observed_candles"], 0)
        self.assertIsNone(feats["mae_r"])
        self.assertIsNone(feats["mfe_r"])
        self.assertIsNone(feats["best_price"])
        self.assertEqual(feats["status"], ss.PATH_WAITING)


# ════════════════════════════════════════════════════════════════════════════
#  CANDLES — dedupe e limite temporal
# ════════════════════════════════════════════════════════════════════════════
class CandleDedupeTests(unittest.TestCase):
    def setUp(self):
        self.t = ss.new_path_trace(_snap())

    def test_timestamp_duplicado_nao_conta(self):
        self.assertTrue(ss.path_observe_candle(self.t, T0_MS, 110.0, 95.0))
        self.assertFalse(ss.path_observe_candle(self.t, T0_MS, 120.0, 90.0))
        self.assertEqual(self.t["observed_candles"], 1)
        self.assertAlmostEqual(self.t["best_price"], 110.0)   # não absorveu o dup

    def test_timestamp_antigo_nao_conta(self):
        ss.path_observe_candle(self.t, T0_MS + FIVE_MIN, 110.0, 95.0)
        self.assertFalse(ss.path_observe_candle(self.t, T0_MS, 130.0, 80.0))
        self.assertEqual(self.t["observed_candles"], 1)

    def test_candle_anterior_ao_snapshot_nao_conta(self):
        self.assertFalse(ss.path_observe_candle(self.t, T0_MS - FIVE_MIN, 150.0, 50.0))
        self.assertEqual(self.t["observed_candles"], 0)

    def test_contagem_unica_e_progressiva(self):
        for i in range(5):
            ss.path_observe_candle(self.t, T0_MS + i * FIVE_MIN, 101.0 + i, 99.0)
        self.assertEqual(self.t["observed_candles"], 5)
        self.assertEqual(self.t["first_candle_ts_ms"], T0_MS)
        self.assertEqual(self.t["last_candle_ts_ms"], T0_MS + 4 * FIVE_MIN)

    def test_timestamp_ausente_nao_conta(self):
        self.assertFalse(ss.path_observe_candle(self.t, None, 110.0, 95.0))
        self.assertEqual(self.t["observed_candles"], 0)


# ════════════════════════════════════════════════════════════════════════════
#  OUTCOME — integração não altera decisão
# ════════════════════════════════════════════════════════════════════════════
class OutcomeParityTests(unittest.TestCase):
    def _windows(self):
        return {
            "sem_evento": _candles((T0_MS, 101.0, 99.0, 100.0),
                                   (T0_MS + FIVE_MIN, 102.0, 98.0, 100.0)),
            "tp2": _candles((T0_MS, 111.0, 99.0, 110.0),
                            (T0_MS + FIVE_MIN, 121.0, 110.0, 120.0)),
            "stop": _candles((T0_MS, 101.0, 90.0, 94.0)),
            "stop_e_tp1_mesma_vela": _candles((T0_MS, 111.0, 90.0, 94.0)),
            "tp1_depois_tp2": _candles((T0_MS, 111.0, 100.0, 110.0),
                                       (T0_MS + FIVE_MIN, 121.0, 110.0, 120.0)),
        }

    def test_saida_identica_com_e_sem_trace(self):
        for name, df in self._windows().items():
            snap_a, snap_b = _snap(), _snap()
            out_a = ss._classify_outcome_candles(snap_a, df)
            trace = ss.new_path_trace(snap_b)
            out_b = ss._classify_outcome_candles(snap_b, df, path_trace=trace)
            self.assertEqual(out_a, out_b, f"divergiu em {name}")

    def test_stop_tp_mesma_vela_preservado(self):
        """Regra conservadora: stop tem prioridade se TP1 não foi marcado antes."""
        df = self._windows()["stop_e_tp1_mesma_vela"]
        snap = _snap()
        trace = ss.new_path_trace(snap)
        out = ss._classify_outcome_candles(snap, df, path_trace=trace)
        self.assertEqual(out[0], "lost")

    def test_tp1_be_trail_preservados(self):
        df = _candles((T0_MS, 111.0, 100.0, 110.0))
        snap_a, snap_b = _snap(), _snap()
        out_a = ss._classify_outcome_candles(snap_a, df)
        out_b = ss._classify_outcome_candles(snap_b, df, path_trace=ss.new_path_trace(snap_b))
        self.assertEqual(out_a, out_b)
        self.assertEqual(out_a[0], "open_after_tp1")
        self.assertEqual(out_a[4], out_b[4])          # peak idêntico

    def test_candle_terminal_incluido_posterior_excluido(self):
        """O candle que fecha o snapshot ENTRA; os depois dele NÃO."""
        df = _candles(
            (T0_MS, 101.0, 90.0, 94.0),                       # terminal (stop)
            (T0_MS + FIVE_MIN, 500.0, 400.0, 450.0),          # posterior — não pode entrar
        )
        snap = _snap()
        trace = ss.new_path_trace(snap)
        out = ss._classify_outcome_candles(snap, df, path_trace=trace)
        self.assertEqual(out[0], "lost")
        self.assertEqual(trace["observed_candles"], 1)
        self.assertAlmostEqual(trace["best_price"], 101.0)    # não viu o 500
        self.assertAlmostEqual(trace["worst_price"], 90.0)

    def test_erro_no_trace_nao_altera_outcome(self):
        df = self._windows()["tp2"]
        snap_a, snap_b = _snap(), _snap()
        expected = ss._classify_outcome_candles(snap_a, df)

        def _boom(*a, **k):
            raise RuntimeError("telemetria quebrada")

        with patch.object(ss, "path_observe_candle", _boom):
            got = ss._classify_outcome_candles(snap_b, df, path_trace={"x": 1})
        self.assertEqual(got, expected)

    def test_trace_none_reproduz_comportamento_anterior(self):
        for df in self._windows().values():
            snap = _snap()
            self.assertEqual(ss._classify_outcome_candles(snap, df),
                             ss._classify_outcome_candles(_snap(), df, path_trace=None))


# ════════════════════════════════════════════════════════════════════════════
#  JSONB — merge, idempotência, restart
# ════════════════════════════════════════════════════════════════════════════
class JsonbMergeTests(unittest.TestCase):
    def test_preserva_p05_context_e_outras_chaves(self):
        feats = {"p05_context": {"schema_version": 1, "regime": "NORMAL"},
                 "rsi": 55, "patterns": ["engulfing"]}
        t = ss.new_path_trace(_snap(features=feats))
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        out = ss.merge_p05_path(feats, t)
        self.assertEqual(out["p05_context"]["regime"], "NORMAL")
        self.assertEqual(out["rsi"], 55)
        self.assertEqual(out["patterns"], ["engulfing"])
        self.assertIn("p05_path", out)

    def test_reatribuicao_de_features(self):
        """Retorna dict NOVO (marca o JSONB dirty), sem mutar o original."""
        feats = {"rsi": 55}
        t = ss.new_path_trace(_snap(features=feats))
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        out = ss.merge_p05_path(feats, t)
        self.assertIsNot(out, feats)
        self.assertNotIn("p05_path", feats)

    def test_restart_continua_do_ultimo_timestamp(self):
        snap = _snap()
        t1 = ss.new_path_trace(snap)
        ss.path_observe_candle(t1, T0_MS, 110.0, 95.0)
        snap.features = ss.merge_p05_path(snap.features, t1)
        # "restart": novo trace semeado do que foi persistido
        t2 = ss.new_path_trace(snap)
        self.assertEqual(t2["observed_candles"], 1)
        self.assertEqual(t2["last_candle_ts_ms"], T0_MS)
        self.assertFalse(ss.path_observe_candle(t2, T0_MS, 999.0, 1.0))   # dedupe pós-restart
        self.assertTrue(ss.path_observe_candle(t2, T0_MS + FIVE_MIN, 115.0, 94.0))
        final = ss.merge_p05_path(snap.features, t2)["p05_path"]
        self.assertEqual(final["observed_candles"], 2)
        self.assertAlmostEqual(final["best_price"], 115.0)

    def test_retry_idempotente(self):
        snap = _snap()
        t = ss.new_path_trace(snap)
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        a = ss.merge_p05_path(snap.features, t)["p05_path"]
        b = ss.merge_p05_path(snap.features, t)["p05_path"]
        for k in ("mae_r", "mfe_r", "best_price", "worst_price", "observed_candles"):
            self.assertEqual(a[k], b[k], k)

    def test_dado_invalido_nao_apaga_telemetria_valida(self):
        snap = _snap()
        t = ss.new_path_trace(snap)
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        snap.features = ss.merge_p05_path(snap.features, t)
        before = dict(snap.features["p05_path"])
        # trace "vazio" (nenhum candle novo) não pode zerar o que já existia
        empty = ss.new_path_trace(_snap(features={}))
        after = ss.merge_p05_path(snap.features, empty)
        self.assertEqual(after["p05_path"]["mae_r"], before["mae_r"])
        self.assertEqual(after["p05_path"]["observed_candles"], before["observed_candles"])

    def test_schema_minimo(self):
        t = ss.new_path_trace(_snap())
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        path = ss.merge_p05_path({}, t)["p05_path"]
        for field in ("schema_version", "source", "resolution", "status", "mfe_r",
                      "mae_r", "best_price", "worst_price", "risk_unit",
                      "observed_candles", "first_candle_ts_ms", "last_candle_ts_ms",
                      "last_updated_at", "finalized_at", "unavailable_reason",
                      "limitations"):
            self.assertIn(field, path, f"campo ausente: {field}")
        self.assertEqual(path["source"], "resolver_5m_ohlcv")
        self.assertEqual(path["resolution"], "5m")

    def test_privados_nao_serializados(self):
        t = ss.new_path_trace(_snap())
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        path = ss.merge_p05_path({}, t)["p05_path"]
        for k in ("_entry", "_is_long", "_created_ts_ms"):
            self.assertNotIn(k, path)


# ════════════════════════════════════════════════════════════════════════════
#  FINALIZAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class FinalizationTests(unittest.TestCase):
    def test_final_observado(self):
        snap = _snap()
        t = ss.new_path_trace(snap)
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        path = ss.merge_p05_path(snap.features, t, status=ss.PATH_FINAL_OBSERVED,
                                 finalize=True)["p05_path"]
        self.assertEqual(path["status"], "FINAL_OBSERVED")
        self.assertIsNotNone(path["finalized_at"])

    def test_final_parcial_preserva_o_observado(self):
        snap = _snap()
        t = ss.new_path_trace(snap)
        ss.path_observe_candle(t, T0_MS, 110.0, 95.0)
        snap.features = ss.merge_p05_path(snap.features, t)
        ss._finalize_path(snap, status=ss.PATH_FINAL_PARTIAL,
                          reason="time-stop sem janela final", now=T0)
        path = snap.features["p05_path"]
        self.assertEqual(path["status"], "FINAL_PARTIAL")
        self.assertIsNotNone(path["mae_r"])                # mantém o observado
        self.assertIn("time-stop", path["unavailable_reason"])

    def test_indisponivel_quando_nunca_houve_candle(self):
        snap = _snap()
        ss._finalize_path(snap, status=ss.PATH_FINAL_PARTIAL,
                          reason="símbolo sem dados", now=T0)
        path = snap.features["p05_path"]
        self.assertEqual(path["status"], "UNAVAILABLE")
        self.assertIsNone(path["mae_r"])
        self.assertIsNone(path["mfe_r"])
        self.assertIn("sem dados", path["unavailable_reason"])

    def test_time_stop_nao_inventa_candle(self):
        snap = _snap()
        ss._finalize_path(snap, status=ss.PATH_FINAL_PARTIAL, reason="time-stop", now=T0)
        self.assertEqual(snap.features["p05_path"]["observed_candles"], 0)

    def test_finalize_nunca_levanta(self):
        """Fail-soft: setup inválido não pode quebrar a resolução."""
        snap = _snap(entry=None)
        ss._finalize_path(snap, status=ss.PATH_FINAL_PARTIAL, reason="x", now=T0)
        self.assertEqual(snap.features, {})          # nada escrito, nada explodiu


# ════════════════════════════════════════════════════════════════════════════
#  DIAGNÓSTICO MAE/MFE
# ════════════════════════════════════════════════════════════════════════════
def _row_with_path(i=0, *, mae=1.0, mfe=2.0, path=True, reason=None):
    feats = {}
    if path:
        feats["p05_path"] = {"status": "FINAL_OBSERVED", "mae_r": mae, "mfe_r": mfe,
                             "observed_candles": 3, "unavailable_reason": reason}
    elif reason is not None:
        feats["p05_path"] = {"status": "UNAVAILABLE", "mae_r": None, "mfe_r": None,
                             "observed_candles": 0, "unavailable_reason": reason}
    return {"dedupe_key": f"r{i}", "features": feats,
            "resolved_at": T0 + timedelta(hours=i)}


class TelemetryDiagnosticTests(unittest.TestCase):
    def test_zero_observados_e_unavailable(self):
        out = p05.summarize_path_telemetry([_row_with_path(i, path=False) for i in range(10)])
        self.assertEqual(out["status"], "UNAVAILABLE")
        self.assertEqual(out["observed"], 0)
        self.assertEqual(out["missing"], 10)

    def test_amostra_vazia(self):
        out = p05.summarize_path_telemetry([])
        self.assertEqual(out["status"], "UNAVAILABLE")
        self.assertIsNone(out["coverage_pct"])

    def test_abaixo_de_30_e_collecting(self):
        rows = [_row_with_path(i) for i in range(20)]
        out = p05.summarize_path_telemetry(rows)
        self.assertEqual(out["status"], "COLLECTING")
        self.assertEqual(out["coverage_pct"], 100.0)

    def test_cobertura_abaixo_de_80_e_collecting(self):
        rows = [_row_with_path(i) for i in range(40)]
        rows += [_row_with_path(100 + i, path=False) for i in range(20)]   # 66.7%
        out = p05.summarize_path_telemetry(rows)
        self.assertEqual(out["observed"], 40)
        self.assertLess(out["coverage_pct"], 80.0)
        self.assertEqual(out["status"], "COLLECTING")

    def test_minimo_completo_e_usable(self):
        rows = [_row_with_path(i) for i in range(40)]
        out = p05.summarize_path_telemetry(rows)
        self.assertEqual(out["status"], "USABLE")

    def test_percentis_corretos(self):
        rows = [_row_with_path(i, mae=float(i), mfe=float(i)) for i in range(101)]
        out = p05.summarize_path_telemetry(rows)
        self.assertAlmostEqual(out["mae_r"]["median"], 50.0)
        self.assertAlmostEqual(out["mae_r"]["p75"], 75.0)
        self.assertAlmostEqual(out["mae_r"]["p90"], 90.0)
        self.assertAlmostEqual(out["mae_r"]["mean"], 50.0)

    def test_missing_reasons(self):
        rows = [_row_with_path(0), _row_with_path(1, path=False),
                _row_with_path(2, path=False, reason="símbolo sem dados")]
        out = p05.summarize_path_telemetry(rows)
        self.assertEqual(out["missing"], 2)
        self.assertIn("símbolo sem dados", out["unavailable_by_reason"])

    def test_limitacoes_explicitas(self):
        out = p05.summarize_path_telemetry([_row_with_path(0)])
        blob = " ".join(out["limitations"]).lower()
        self.assertIn("5 minutos", blob)
        self.assertIn("não é execução real", blob)
        self.assertIn("tick", blob)
        self.assertIn("slippage", blob)

    def test_nao_gera_stop_ou_tp_ideal(self):
        """Só distribuição observada — nenhum CAMPO de recomendação.
        (a frase 'stop ideal' aparece apenas na lista de limitações, negada)"""
        out = p05.summarize_path_telemetry([_row_with_path(i) for i in range(40)])
        for proibido in ("suggested_stop", "stop_ideal", "recommended_stop",
                         "suggested_tp", "tp_ideal", "recommended_tp",
                         "suggestion", "recommendation"):
            self.assertNotIn(proibido, out, f"campo de recomendação: {proibido}")
        blob = " ".join(out["limitations"]).lower()
        self.assertIn("não gera 'stop ideal'", blob)      # está negado, não gerado


# ════════════════════════════════════════════════════════════════════════════
#  COBERTURA CONTEXTUAL
# ════════════════════════════════════════════════════════════════════════════
def _ctx_row(i, *, regime="NORMAL", zone="limit_ob"):
    feats = {}
    if regime is not None:
        feats["regime"] = regime
    if zone is not None:
        feats["entry_zone_type"] = zone
    return {"dedupe_key": f"c{i}", "features": feats,
            "resolved_at": T0 + timedelta(hours=i), "score": 60.0,
            "tier": "A", "timeframe": "1h", "direction": "long"}


class ContextCoverageTests(unittest.TestCase):
    def test_global_alto_com_treino_baixo_continua_collecting(self):
        """Metade ANTIGA sem feature: global passa de 80%, treino não."""
        rows = [_ctx_row(i, regime=None, zone=None) for i in range(30)]      # treino velho
        rows += [_ctx_row(100 + i) for i in range(70)]                        # recente ok
        out = p05.summarize_context_coverage(rows)
        axis = out["axes"]["regime"]
        self.assertGreaterEqual(axis["coverage_global_pct"], 60.0)
        self.assertLess(axis["coverage_train_pct"], 80.0)
        self.assertEqual(axis["status"], "COLLECTING")

    def test_treino_completo_fica_usable(self):
        rows = [_ctx_row(i) for i in range(100)]
        out = p05.summarize_context_coverage(rows)
        for axis in ("regime", "entry_zone_type"):
            self.assertEqual(out["axes"][axis]["coverage_train_pct"], 100.0)
            self.assertEqual(out["axes"][axis]["status"], "USABLE")

    def test_split_identico_ao_p05_1(self):
        rows = [_ctx_row(i) for i in range(100)]
        split = p05.temporal_split(sorted(rows, key=lambda r: r["resolved_at"]))
        out = p05.summarize_context_coverage(rows)
        self.assertTrue(out["split_available"])
        self.assertTrue(split["ok"])
        # mesma proporção 50/25/25 usada pela geração
        self.assertEqual(len(split["train"]), 50)

    def test_somente_eixos_permitidos(self):
        out = p05.summarize_context_coverage([_ctx_row(i) for i in range(100)])
        self.assertEqual(set(out["axes"]), {"regime", "entry_zone_type"})

    def test_nao_le_realized_r(self):
        """Sentinela: acesso a realized_r explode."""
        class _Sealed(dict):
            def __getitem__(self, k):
                if k == "realized_r":
                    raise AssertionError("HOLDOUT VIOLADO: realized_r lido")
                return super().__getitem__(k)

            def get(self, k, d=None):
                if k == "realized_r":
                    raise AssertionError("HOLDOUT VIOLADO: realized_r lido")
                return super().get(k, d)

        rows = [_Sealed(_ctx_row(i)) for i in range(100)]
        out = p05.summarize_context_coverage(rows)     # não pode levantar
        self.assertIn("regime", out["axes"])

    def test_global_nao_substitui_treino(self):
        out = p05.summarize_context_coverage([_ctx_row(i) for i in range(100)])
        self.assertIn("não a substitui", out["note"])


# ════════════════════════════════════════════════════════════════════════════
#  SLIPPAGE / LATÊNCIA
# ════════════════════════════════════════════════════════════════════════════
class SlippageTests(unittest.TestCase):
    def test_zero_legitimo_continua_valido(self):
        out = p05.summarize_slippage([{"entry_slippage_pct": 0.0},
                                      {"entry_slippage_pct": 0.2}])
        self.assertEqual(out["slippage_valid"], 2)
        self.assertAlmostEqual(out["mean"], 0.1)

    def test_null_continua_ausente(self):
        out = p05.summarize_slippage([{"entry_slippage_pct": None},
                                      {"entry_slippage_pct": 0.2}])
        self.assertEqual(out["slippage_missing"], 1)
        self.assertEqual(out["slippage_valid"], 1)
        self.assertAlmostEqual(out["mean"], 0.2)      # ausente NÃO virou zero

    def test_nan_inf_excluidos_com_motivo(self):
        out = p05.summarize_slippage([{"entry_slippage_pct": float("nan")},
                                      {"entry_slippage_pct": float("inf")},
                                      {"entry_slippage_pct": 0.1}])
        self.assertEqual(out["slippage_valid"], 1)
        self.assertEqual(out["invalid_excluded_by_reason"]["NaN/infinito"], 2)

    def test_cobertura_e_percentis(self):
        rows = [{"entry_slippage_pct": float(i)} for i in range(101)]
        out = p05.summarize_slippage(rows)
        self.assertEqual(out["coverage_pct"], 100.0)
        self.assertAlmostEqual(out["median"], 50.0)
        self.assertAlmostEqual(out["p90"], 90.0)

    def test_sem_double_count(self):
        rows = [{"entry_slippage_pct": 1.0}] * 5
        out = p05.summarize_slippage(rows)
        self.assertEqual(out["slippage_valid"], 5)
        self.assertEqual(out["total_real_closed"], 5)

    def test_amostra_vazia(self):
        out = p05.summarize_slippage([])
        self.assertIsNone(out["coverage_pct"])
        self.assertIsNone(out["mean"])

    def test_latencia_nao_e_inventada(self):
        out = p05.summarize_latency()
        self.assertEqual(out["status"], "UNAVAILABLE")
        self.assertIn("fetch latency não é", out["reason"])
        self.assertIn("decision_at", out["missing_fields"])


# ════════════════════════════════════════════════════════════════════════════
#  GATES
# ════════════════════════════════════════════════════════════════════════════
class GateAvailabilityTests(unittest.TestCase):
    def _events(self):
        return {
            "items": [{"gate": "data-freshness", "count": 10},
                      {"gate": "score-min", "count": 5}],
            "by_phase": {
                "P04A_entry_revalidation": {"events": None, "reason": "aborta no POST"},
                "P04B_depth_vwap": {"events": None, "reason": "aborta no POST"},
                "P04C_data_freshness": {"events": 10},
            },
        }

    def test_p04_ausente_aparece_unavailable(self):
        out = p05.summarize_gate_availability(self._events())
        self.assertEqual(out["by_phase"]["P04A_entry_revalidation"]["status"], "UNAVAILABLE")
        self.assertEqual(out["by_phase"]["P04B_depth_vwap"]["status"], "UNAVAILABLE")
        self.assertEqual(out["by_phase"]["P04C_data_freshness"]["status"], "AVAILABLE")

    def test_gates_com_contador_listados(self):
        out = p05.summarize_gate_availability(self._events())
        self.assertIn("data-freshness", out["gates_with_counters"])
        self.assertIn("P04A_entry_revalidation", out["gates_without_counters"])

    def test_eventos_nao_viram_oportunidades(self):
        out = p05.summarize_gate_availability(self._events())
        self.assertIn("não são oportunidades únicas", out["semantics"])
        self.assertIn("não somam com executados", out["semantics"])

    def test_sem_hooks_no_executor(self):
        out = p05.summarize_gate_availability(self._events())
        self.assertTrue(out["no_hooks_added"])

    def test_nao_estima_lucro_perdido(self):
        blob = str(p05.summarize_gate_availability(self._events())).lower()
        self.assertNotIn("lucro perdido", blob)


# ════════════════════════════════════════════════════════════════════════════
#  RETENÇÃO
# ════════════════════════════════════════════════════════════════════════════
class RetentionIsolationTests(unittest.TestCase):
    def test_p05_path_so_em_snapshots_normais(self):
        """A telemetria é instrumentada apenas em `check_open_snapshots`; o
        namespace `wide` (o único podado) nunca recebe `p05_path`."""
        src = (BACKEND / "services/snapshot_service.py").read_text()
        open_block = src.split("async def check_open_snapshots")[1].split(
            "def _record_wide_outcome")[0]
        wide_block = src.split("async def check_wide_snapshots")[1]
        self.assertIn("path_trace=_path", open_block)
        self.assertIn("merge_p05_path", open_block)
        self.assertNotIn("path_trace", wide_block)
        self.assertNotIn("merge_p05_path", wide_block)

    def test_poda_continua_exclusiva_de_wide(self):
        src = (BACKEND / "services/snapshot_service.py").read_text()
        self.assertIn("RecommendationSnapshot.status == WIDE_DISPLAY_STATUS", src)
        self.assertIn("RecommendationSnapshot.status.in_(WIDE_RESOLVED_STATUSES)", src)
        # nenhuma poda nova por status resolvido do P05
        self.assertNotIn('status.in_(SNAP_RESOLVED)', src)


# ════════════════════════════════════════════════════════════════════════════
#  API / ARQUITETURA / FRONTEND
# ════════════════════════════════════════════════════════════════════════════
class ApiArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = (BACKEND / "main.py").read_text()
        cls.svc = (BACKEND / "services/strategy_evidence_service.py").read_text()
        cls.snap = (BACKEND / "services/snapshot_service.py").read_text()

    def test_nenhum_endpoint_novo(self):
        for proibido in ("collect-now", "recompute", "backfill", "telemetry-run",
                         "/apply", "/promote", "activate", "retry-now"):
            self.assertNotIn(f'"{proibido}"', self.main, f"endpoint proibido: {proibido}")

    def test_nenhum_post_de_telemetria(self):
        self.assertNotIn('@app.post("/api/strategy/p05/telemetry', self.main)
        self.assertNotIn('@app.post("/api/strategy/p05/readiness', self.main)

    def test_nenhuma_chamada_nova_a_exchange(self):
        """A telemetria reaproveita os candles que o resolver JÁ busca."""
        block = self.snap.split("#  P05.1T — telemetria PROSPECTIVA")[1].split(
            "async def _current_regime_label")[0]
        for proibido in ("fetch_ohlcv", "_resolver_fetch_ohlcv", "httpx", "aiohttp",
                         "ccxt", "binance_signed_service", "place_order", "cancel_order"):
            self.assertNotIn(proibido, block, f"proibido na telemetria: {proibido}")

    def test_telemetria_nao_importa_sdk(self):
        block = self.svc.split("#  P05.1T — telemetria prospectiva")[1].split(
            "async def build_diagnosis")[0]
        for proibido in ("import ccxt", "binance_signed_service", "httpx", "aiohttp",
                         "shadow_trade_service", "trade_manager"):
            self.assertNotIn(proibido, block, f"import proibido: {proibido}")

    def test_nenhuma_coluna_ou_tabela_nova(self):
        model = (BACKEND / "models/recommendation_snapshot.py").read_text()
        self.assertNotIn("p05_path", model)          # vive dentro do JSONB existente
        self.assertEqual(model.count("__tablename__"), 1)

    def test_nenhuma_env_ou_flag_nova(self):
        block = self.snap.split("#  P05.1T — telemetria PROSPECTIVA")[1].split(
            "async def _current_regime_label")[0]
        self.assertNotIn("os.getenv", block)
        env = p05.env_snapshot()
        self.assertFalse([k for k in env if "P051T" in k or "TELEMETRY" in k.upper()])

    def test_arquivos_proibidos_intactos(self):
        import subprocess
        out = subprocess.run(["git", "status", "--short"], cwd=BACKEND.parent,
                             capture_output=True, text=True).stdout
        for proibido in ("shadow_trade_service.py", "trade_manager_service.py",
                         "binance_signed_service.py", "frontend/dist/assets"):
            self.assertNotIn(proibido, out, f"arquivo proibido alterado: {proibido}")

    def test_telemetria_nao_alimenta_avaliacao(self):
        """`p05_path` não pode entrar em nenhum caminho de decisão do P05.1."""
        for fn in ("def eligibility", "def contextual_eligibility",
                   "def evaluate_contextual_gate", "def generate_contextual_candidates"):
            block = self.svc.split(fn)[1].split("\ndef ")[0]
            self.assertNotIn("p05_path", block, f"{fn} não pode ler p05_path")


class FrontendTelemetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()

    def test_secao_existe(self):
        self.assertIn("Qualidade da telemetria", self.src)

    def test_linguagem_correta(self):
        self.assertIn("velas de 5 minutos", self.src)
        low = self.src.lower()
        for proibido in ("stop ideal", "tp ideal", "lucro esperado",
                         "oportunidade perdida", "recomendamos alterar"):
            self.assertNotIn(proibido, low, f"linguagem proibida: {proibido}")

    def test_sem_botao_na_secao(self):
        block = self.src.split("Qualidade da telemetria")[1].split("</section>")[0]
        self.assertNotIn("<button", block)
        self.assertNotIn("onClick", block)

    def test_sem_object_object(self):
        block = self.src.split("Qualidade da telemetria")[1].split("</section>")[0]
        for proibido in ("{t.mae_r}", "{t.limitations}", "{tel.axes}", "String(t)"):
            self.assertNotIn(proibido, block, f"render bruto de objeto: {proibido}")


if __name__ == "__main__":
    unittest.main()
