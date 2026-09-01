"""P05.2A — diagnóstico longitudinal dos stop-losses (ANALYTICS ONLY).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco
externo, Railway ou credencial real.
"""
from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2A (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import strategy_evidence_service as p05      # noqa: E402


T0 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


class _SealedRow(dict):
    """Sentinela: EXPLODE se o outcome do holdout for acessado."""
    def __getitem__(self, key):
        if key in ("realized_r", "status"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in ("realized_r", "status"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().get(key, default)


def _row(i=0, *, status="lost", r=-1.0, regime="NORMAL", tier="A", timeframe="1h",
         direction="long", score=60.0, zone="limit_ob", atr=1.5, hour=10,
         path=None, stop_dist=2.0, minutes=45, symbol="BTC/USDT:USDT", **kw):
    created = T0 + timedelta(hours=i)
    feats = {"regime": regime, "entry_zone_type": zone, "atr_pct": atr,
             "hour_utc": hour, "day_of_week": 1, "patterns": [], "mtf_aligned": 2,
             "funding_sentiment": "neutral"}
    if path is not None:
        feats["p05_path"] = path
    base = {
        "id": i, "dedupe_key": f"snap:{i}", "symbol": symbol, "timeframe": timeframe,
        "tier": tier, "direction": direction, "score": score,
        "status": status, "realized_r": r, "features": feats,
        "stop_distance_pct": stop_dist,
        "created_at": created, "resolved_at": created + timedelta(minutes=minutes),
    }
    base.update(kw)
    return base


# ════════════════════════════════════════════════════════════════════════════
#  IDENTIDADE DO STOP
# ════════════════════════════════════════════════════════════════════════════
class StopIdentityTests(unittest.TestCase):
    def test_lost_e_stop(self):
        self.assertEqual(p05.classify_shadow_outcome("lost", -1.0), p05.OUTCOME_STOP)

    def test_won_tp1_be_nao_e_stop_original(self):
        self.assertEqual(p05.classify_shadow_outcome("won_tp1_be", 0.5),
                         p05.OUTCOME_PROTECTED_EXIT)
        self.assertNotEqual(p05.classify_shadow_outcome("won_tp1_be", 0.5), p05.OUTCOME_STOP)

    def test_expired_nao_e_stop(self):
        self.assertEqual(p05.classify_shadow_outcome("expired", 0.0), p05.OUTCOME_EXPIRED)

    def test_wins(self):
        self.assertEqual(p05.classify_shadow_outcome("won_tp2", 1.5), p05.OUTCOME_WIN)
        self.assertEqual(p05.classify_shadow_outcome("won_tp1", 0.5), p05.OUTCOME_WIN)

    def test_realized_r_none_nunca_vira_zero(self):
        self.assertEqual(p05.classify_shadow_outcome("lost", None), p05.OUTCOME_INCONSISTENT)

    def test_divergencia_status_r_e_inconsistente(self):
        self.assertEqual(p05.classify_shadow_outcome("lost", 1.0), p05.OUTCOME_INCONSISTENT)
        self.assertEqual(p05.classify_shadow_outcome("won_tp2", -1.0), p05.OUTCOME_INCONSISTENT)
        self.assertEqual(p05.classify_shadow_outcome("won_tp1_be", -0.5),
                         p05.OUTCOME_INCONSISTENT)
        self.assertEqual(p05.classify_shadow_outcome("won_tp1_be", 0.0),
                         p05.OUTCOME_INCONSISTENT)
        self.assertEqual(p05.classify_shadow_outcome("expired", -1.0),
                         p05.OUTCOME_INCONSISTENT)

    def test_nan_inf_inconsistente(self):
        for bad in (float("nan"), float("inf")):
            self.assertEqual(p05.classify_shadow_outcome("lost", bad), p05.OUTCOME_INCONSISTENT)


# ════════════════════════════════════════════════════════════════════════════
#  QUALIDADE DE DADO
# ════════════════════════════════════════════════════════════════════════════
class DataQualityTests(unittest.TestCase):
    def test_exclusoes_contabilizadas(self):
        rows = [
            _row(0), _row(1, status="won_tp2", r=1.5),
            _row(2, r=None), _row(3, r=float("nan")),
            _row(4, status="lost", r=1.0),               # incoerente
            _row(5, resolved_at=None),
        ]
        rows.append(dict(rows[0]))                        # duplicata
        valid, excluded = p05._partition_outcomes(rows)
        self.assertEqual(len(valid), 2)
        self.assertEqual(excluded["realized_r ausente"], 1)
        self.assertEqual(excluded["realized_r NaN/infinito"], 1)
        self.assertEqual(excluded["status/R incoerente"], 1)
        self.assertEqual(excluded["sem timestamp de resolução"], 1)
        self.assertEqual(excluded["duplicata"], 1)

    def test_ausencia_nunca_vira_zero(self):
        valid, _ = p05._partition_outcomes([_row(0, r=None)])
        self.assertEqual(valid, [])

    def test_metrics_reporta_exclusoes(self):
        m = p05.stop_stage_metrics([_row(0), _row(1, r=None)])
        self.assertEqual(m["total_resolved"], 1)
        self.assertIn("realized_r ausente", m["excluded_by_reason"])


# ════════════════════════════════════════════════════════════════════════════
#  HOLDOUT SELADO
# ════════════════════════════════════════════════════════════════════════════
class HoldoutSealedTests(unittest.IsolatedAsyncioTestCase):
    def test_sentinela_detecta_violacao(self):
        row = _SealedRow(_row(0))
        with self.assertRaises(AssertionError):
            _ = row["realized_r"]
        with self.assertRaises(AssertionError):
            _ = row.get("status")

    def test_analise_nunca_toca_linhas_seladas(self):
        """Só treino+validação entram; o teste selado nem é passado adiante."""
        train = [_row(i) for i in range(40)]
        sealed_test = [_SealedRow(_row(500 + i)) for i in range(20)]
        m = p05.stop_stage_metrics(train)               # não levanta
        self.assertEqual(m["total_resolved"], 40)
        seg = p05.stop_segments_for_axis(train, "regime")
        self.assertTrue(seg["items"])
        self.assertEqual(len(sealed_test), 20)          # nunca foi analisado

    async def test_loader_split_50_25_25_e_nao_materializa_teste(self):
        n = 100
        index = [SimpleNamespace(id=i, outcome_at=T0 + timedelta(hours=i)) for i in range(n)]
        captured = {"detail_calls": 0}

        class _Res:
            def __init__(self, rows): self._rows = rows
            def all(self): return self._rows

        class _Session:
            async def execute(self, stmt):
                text = str(stmt)
                if "realized_r" in text:
                    captured["detail_calls"] += 1
                    # devolve só o que a query permitiria (<= boundary)
                    return _Res([SimpleNamespace(
                        id=i, symbol="BTC/USDT:USDT", timeframe="1h", tier="A",
                        direction="long", score=60.0, status="lost", realized_r=-1.0,
                        features={"regime": "NORMAL"}, stop_distance_pct=2.0,
                        created_at=T0 + timedelta(hours=i),
                        outcome_at=T0 + timedelta(hours=i)) for i in range(75)])
                return _Res(index)

            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import db as _db
        with patch.object(_db, "get_session", lambda: _Session()):
            split = await p05.load_stop_shadow_split(days=120)

        self.assertEqual(split["train_count"], 50)
        self.assertEqual(split["validation_count"], 25)
        self.assertEqual(split["test_count"], 25)
        self.assertEqual(len(split["train"]), 50)
        self.assertEqual(len(split["validation"]), 25)
        self.assertEqual(split["holdout_status"], "SEALED")
        self.assertFalse(split["holdout_outcomes_read"])
        self.assertFalse(split["holdout_metrics_computed"])
        self.assertEqual(captured["detail_calls"], 1)

    async def test_ids_do_teste_nunca_entram_nos_estagios(self):
        n = 100
        index = [SimpleNamespace(id=i, outcome_at=T0 + timedelta(hours=i)) for i in range(n)]

        class _Res:
            def __init__(self, rows): self._rows = rows
            def all(self): return self._rows

        class _Session:
            async def execute(self, stmt):
                if "realized_r" in str(stmt):
                    # simula fonte devolvendo TAMBÉM ids do teste (>=75)
                    return _Res([SimpleNamespace(
                        id=i, symbol="B", timeframe="1h", tier="A", direction="long",
                        score=60.0, status="lost", realized_r=-1.0, features={},
                        stop_distance_pct=2.0, created_at=T0, outcome_at=T0)
                        for i in range(n)])
                return _Res(index)

            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import db as _db
        with patch.object(_db, "get_session", lambda: _Session()):
            split = await p05.load_stop_shadow_split(days=120)
        ids = {r["id"] for r in split["train"]} | {r["id"] for r in split["validation"]}
        self.assertTrue(max(ids) < 75, "id do holdout vazou para treino/validação")

    def test_loader_ordena_por_outcome_at_e_id(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def load_stop_shadow_split")[1].split(
            "def _partition_outcomes")[0]
        self.assertIn("order_by(RS.outcome_at.asc(), RS.id.asc())", block)
        self.assertIn("allowed_ids", block)

    def test_bloco_nao_chama_avaliacao_nem_gate(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("#  P05.2A — diagnóstico LONGITUDINAL")[1].split(
            "async def build_diagnosis")[0]
        for proibido in ("evaluate_contextual_gate", "evaluate_offline_gate",
                         "generate_contextual_candidates", "_upsert_experiment",
                         "compare_contextual", "start_shadow", "build_promotion_plan"):
            self.assertNotIn(proibido, block, f"proibido no P05.2A: {proibido}")


# ════════════════════════════════════════════════════════════════════════════
#  MÉTRICAS DE ESTÁGIO
# ════════════════════════════════════════════════════════════════════════════
class StageMetricsTests(unittest.TestCase):
    def test_taxa_e_intervalo(self):
        rows = [_row(i) for i in range(30)] + [_row(100 + i, status="won_tp2", r=1.5)
                                               for i in range(70)]
        m = p05.stop_stage_metrics(rows)
        self.assertEqual(m["total_resolved"], 100)
        self.assertEqual(m["stops"], 30)
        self.assertAlmostEqual(m["stop_rate_pct"], 30.0)
        self.assertIsNotNone(m["stop_rate_ci"])
        self.assertLess(m["stop_rate_ci"]["low_pct"], 30.0)

    def test_protetivo_e_expired_separados(self):
        rows = [_row(0), _row(1, status="won_tp1_be", r=0.5),
                _row(2, status="expired", r=0.0)]
        m = p05.stop_stage_metrics(rows)
        self.assertEqual(m["stops"], 1)
        self.assertEqual(m["protected_exits"], 1)
        self.assertEqual(m["expired"], 1)

    def test_pior_sequencia_de_stops(self):
        rows = [_row(0), _row(1), _row(2, status="won_tp2", r=1.5), _row(3)]
        self.assertEqual(p05.stop_stage_metrics(rows)["worst_stop_streak"], 2)

    def test_tempo_ate_stop(self):
        rows = [_row(i, minutes=60) for i in range(10)]
        m = p05.stop_stage_metrics(rows)
        self.assertAlmostEqual(m["time_to_stop_minutes"]["median"], 60.0)
        self.assertEqual(m["time_to_stop_minutes"]["count"], 10)

    def test_amostra_vazia(self):
        m = p05.stop_stage_metrics([])
        self.assertEqual(m["total_resolved"], 0)
        self.assertIsNone(m["stop_rate_pct"])
        self.assertIsNone(m["expectancy_r"])


# ════════════════════════════════════════════════════════════════════════════
#  SEGMENTOS
# ════════════════════════════════════════════════════════════════════════════
class SegmentTests(unittest.TestCase):
    def _mixed(self):
        bad = [_row(i, regime="ALT_DANGER") for i in range(40)]                 # todos stop
        good = [_row(100 + i, regime="NORMAL", status="won_tp2", r=1.5)
                for i in range(60)]
        return bad + good

    def test_taxa_por_exposicao_e_lift(self):
        seg = p05.stop_segments_for_axis(self._mixed(), "regime")
        alt = next(it for it in seg["items"] if it["key"] == "ALT_DANGER")
        self.assertEqual(alt["exposure"], 40)
        self.assertAlmostEqual(alt["stop_rate_pct"], 100.0)
        self.assertAlmostEqual(seg["stage_stop_rate_pct"], 40.0)
        self.assertAlmostEqual(alt["stop_rate_lift_pp"], 60.0)
        self.assertIsNotNone(alt["stop_rate_ci"])

    def test_alto_volume_com_taxa_baixa_nao_e_adverso(self):
        """Muitos stops absolutos, mas taxa ABAIXO do baseline."""
        big = [_row(i, base_key=None, regime="BIG") for i in range(20)]          # 20 stops
        big += [_row(200 + i, regime="BIG", status="won_tp2", r=1.5) for i in range(80)]
        small = [_row(400 + i, regime="SMALL") for i in range(30)]               # 30 stops
        seg = p05.stop_segments_for_axis(big + small, "regime")
        item_big = next(it for it in seg["items"] if it["key"] == "BIG")
        self.assertEqual(item_big["stop_count"], 20)
        self.assertLess(item_big["stop_rate_pct"], seg["stage_stop_rate_pct"])
        self.assertLess(item_big["stop_rate_lift_pp"], 0)

    def test_wins_removidos_se_bloqueado(self):
        seg = p05.stop_segments_for_axis(self._mixed(), "regime")
        normal = next(it for it in seg["items"] if it["key"] == "NORMAL")
        self.assertEqual(normal["wins_removed_if_blocked"], 60)

    def test_cobertura_e_missing(self):
        rows = [_row(i) for i in range(10)] + [_row(100 + i, regime=None) for i in range(10)]
        for r in rows[10:]:
            r["features"].pop("regime", None)
        seg = p05.stop_segments_for_axis(rows, "regime")
        self.assertEqual(seg["missing"], 10)
        self.assertAlmostEqual(seg["coverage_pct"], 50.0)

    def test_sobreposicao_marcada_e_nao_somada(self):
        rows = [_row(i) for i in range(10)]
        for r in rows:
            r["features"]["patterns"] = ["engulfing", "pinbar"]
        seg = p05.stop_segments_for_axis(rows, "patterns")
        self.assertTrue(seg["overlapping"])
        total = sum(it["exposure"] for it in seg["items"])
        self.assertGreater(total, 10)                # some > n é esperado (atribuição)
        self.assertIn("não somam", seg["note"])

    def test_todos_os_eixos_disponiveis(self):
        for axis in ("tier", "timeframe", "tier_timeframe", "direction", "base",
                     "patterns", "session_utc", "day_of_week", "regime",
                     "funding_sentiment", "score_bin", "atr_band", "mtf_aligned",
                     "entry_zone_type"):
            self.assertIn(axis, p05._STOP_AXES)


# ════════════════════════════════════════════════════════════════════════════
#  CONFIRMAÇÃO TEMPORAL
# ════════════════════════════════════════════════════════════════════════════
def _seg(exposure, stops, lift, expectancy):
    return {"exposure": exposure, "stop_count": stops, "stop_rate_lift_pp": lift,
            "expectancy_r": expectancy, "stop_rate_pct": 50.0, "stop_rate_ci": None,
            "stage_stop_rate_pct": 40.0, "wins_removed_if_blocked": 3,
            "reliability": "USABLE", "key": "X"}


class TemporalConfirmationTests(unittest.TestCase):
    def _cls(self, t, v, tc=95.0, vc=95.0):
        return p05.classify_stop_segment(t, v, train_coverage=tc, valid_coverage=vc)["classification"]

    def test_persistente(self):
        self.assertEqual(
            self._cls(_seg(40, 20, 15.0, -0.3), _seg(30, 12, 10.0, -0.2)),
            p05.STOP_PERSISTENT_ADVERSE)

    def test_ruim_no_treino_bom_na_validacao_e_mixed(self):
        self.assertEqual(
            self._cls(_seg(40, 20, 15.0, -0.3), _seg(30, 12, -5.0, 0.2)),
            p05.STOP_MIXED)

    def test_expectancy_positiva_nao_vira_adverso(self):
        self.assertEqual(
            self._cls(_seg(40, 20, 15.0, 0.3), _seg(30, 12, 10.0, 0.2)),
            p05.STOP_MIXED)

    def test_amostra_pequena_nao_promove(self):
        self.assertEqual(
            self._cls(_seg(20, 20, 15.0, -0.3), _seg(30, 12, 10.0, -0.2)),
            p05.STOP_SAMPLE_LIMITED)
        self.assertEqual(
            self._cls(_seg(40, 5, 15.0, -0.3), _seg(30, 12, 10.0, -0.2)),
            p05.STOP_SAMPLE_LIMITED)
        self.assertEqual(
            self._cls(_seg(40, 20, 15.0, -0.3), _seg(30, 3, 10.0, -0.2)),
            p05.STOP_SAMPLE_LIMITED)

    def test_cobertura_baixa_nao_promove(self):
        self.assertEqual(
            self._cls(_seg(40, 20, 15.0, -0.3), _seg(30, 12, 10.0, -0.2), tc=50.0),
            p05.STOP_LOW_COVERAGE)

    def test_nao_adverso(self):
        self.assertEqual(
            self._cls(_seg(40, 20, -5.0, -0.3), _seg(30, 12, -3.0, -0.2)),
            p05.STOP_NOT_ADVERSE)

    def test_contexto_ausente_na_validacao(self):
        self.assertEqual(self._cls(_seg(40, 20, 15.0, -0.3), None),
                         p05.STOP_SAMPLE_LIMITED)

    def test_hipoteses_sem_knob_nem_acao(self):
        train = {"regime": {"coverage_pct": 95.0, "items": [
            dict(_seg(40, 20, 15.0, -0.3), key="ALT_DANGER")]}}
        valid = {"regime": {"coverage_pct": 95.0, "items": [
            dict(_seg(30, 12, 10.0, -0.2), key="ALT_DANGER")]}}
        persistent, others = p05.build_stop_hypotheses(train, valid)
        self.assertEqual(len(persistent), 1)
        h = persistent[0]
        self.assertFalse(h["executable"])
        self.assertEqual(h["for_future_phase"], "P05.2B")
        for proibido in ("knob", "config", "threshold", "action", "score_delta",
                         "promotion_plan"):
            self.assertNotIn(proibido, h, f"campo executável: {proibido}")
        self.assertIn("blocking_would_remove_wins", h)

    def test_maximo_8_padroes(self):
        items_t = [dict(_seg(40, 20, 15.0, -0.3), key=f"k{i}") for i in range(20)]
        items_v = [dict(_seg(30, 12, 10.0, -0.2), key=f"k{i}") for i in range(20)]
        persistent, _ = p05.build_stop_hypotheses(
            {"regime": {"coverage_pct": 95.0, "items": items_t}},
            {"regime": {"coverage_pct": 95.0, "items": items_v}})
        self.assertLessEqual(len(persistent), 8)


# ════════════════════════════════════════════════════════════════════════════
#  TRAJETÓRIA
# ════════════════════════════════════════════════════════════════════════════
class TrajectoryTests(unittest.TestCase):
    def _path(self, mfe, status="FINAL_OBSERVED"):
        return {"status": status, "mfe_r": mfe, "mae_r": 1.0, "observed_candles": 5}

    def test_faixas_de_tempo(self):
        rows = [_row(0, minutes=10), _row(1, minutes=60), _row(2, minutes=200),
                _row(3, minutes=800)]
        t = p05.stop_trajectory(rows)["time_to_stop"]
        by = {b["band"]: b["count"] for b in t["bands"]}
        self.assertEqual(by["<30m"], 1)
        self.assertEqual(by["30m–2h"], 1)
        self.assertEqual(by["2h–6h"], 1)
        self.assertEqual(by[">6h"], 1)

    def test_faixas_de_mfe(self):
        rows = [_row(0, path=self._path(0.1)), _row(1, path=self._path(0.3)),
                _row(2, path=self._path(0.7)), _row(3, path=self._path(1.5))]
        m = p05.stop_trajectory(rows)["mfe_before_stop"]
        by = {b["band"]: b["count"] for b in m["bands"]}
        self.assertEqual(by["<0.25R"], 1)
        self.assertEqual(by["0.25–0.50R"], 1)
        self.assertEqual(by["0.50–1.00R"], 1)
        self.assertEqual(by[">=1.00R"], 1)
        self.assertEqual(m["observed"], 4)

    def test_path_ausente_nao_vira_zero(self):
        rows = [_row(0), _row(1, path=self._path(0.5))]
        m = p05.stop_trajectory(rows)["mfe_before_stop"]
        self.assertEqual(m["observed"], 1)
        self.assertAlmostEqual(m["coverage_pct"], 50.0)
        traj = p05.stop_trajectory(rows)
        self.assertIn("p05_path ausente (snapshot anterior ao P05.1T)",
                      traj["missing_by_reason"])

    def test_final_partial_preservado(self):
        rows = [_row(0, path=self._path(0.6, status="FINAL_PARTIAL"))]
        m = p05.stop_trajectory(rows)["mfe_before_stop"]
        self.assertEqual(m["path_status_counts"]["FINAL_PARTIAL"], 1)
        self.assertEqual(m["observed"], 1)

    def test_unavailable_sem_mfe_conta_como_missing(self):
        rows = [_row(0, path={"status": "UNAVAILABLE", "mfe_r": None})]
        traj = p05.stop_trajectory(rows)
        self.assertEqual(traj["mfe_before_stop"]["observed"], 0)
        self.assertTrue(any("UNAVAILABLE" in k for k in traj["missing_by_reason"]))

    def test_nao_recalcula_long_short(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("def stop_trajectory")[1].split("def build_stop_hypotheses")[0]
        for proibido in ("best_price", "worst_price", "risk_unit", "entry -", "- entry"):
            self.assertNotIn(proibido, block, f"recálculo proibido: {proibido}")

    def test_faixas_de_stop_distance(self):
        rows = [_row(0, stop_dist=0.5), _row(1, stop_dist=1.5),
                _row(2, stop_dist=3.0), _row(3, stop_dist=5.0)]
        d = p05.stop_trajectory(rows)["stop_distance"]
        by = {b["band"]: b["count"] for b in d["bands"]}
        self.assertEqual(by["<1%"], 1)
        self.assertEqual(by["1–2%"], 1)
        self.assertEqual(by["2–4%"], 1)
        self.assertEqual(by[">=4%"], 1)

    def test_descritivo_sem_sugestao_de_stop(self):
        traj = p05.stop_trajectory([_row(0, path=self._path(0.5))])
        self.assertTrue(traj["descriptive_only"])
        for proibido in ("suggested_stop", "stop_ideal", "recommended_stop",
                         "widen_stop", "optimal_stop"):
            self.assertNotIn(proibido, traj, f"campo proibido: {proibido}")

    def test_sem_stops_nao_quebra(self):
        traj = p05.stop_trajectory([_row(0, status="won_tp2", r=1.5)])
        self.assertEqual(traj["total_stops"], 0)
        self.assertIsNone(traj["time_to_stop"]["coverage_pct"])


# ════════════════════════════════════════════════════════════════════════════
#  REAL
# ════════════════════════════════════════════════════════════════════════════
class RealSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, trades):
        from services import assertiveness_service as a

        class _Res:
            def __init__(self, rows): self._rows = rows
            def scalars(self): return self
            def all(self): return self._rows

        class _Session:
            async def execute(self, stmt): return _Res(trades)
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import db as _db
        with patch.object(_db, "get_session", lambda: _Session()):
            return await a.real_stop_summary(120)

    def _t(self, i, status, r, rec_id=..., *, exchange_order_id=None,
           client_order_id=None, exchange="binance"):
        if rec_id is ...:
            rec_id = i + 1
        return SimpleNamespace(id=i, status=status, realized_r=r,
                               recommendation_id=rec_id, exchange=exchange,
                               exchange_order_id=exchange_order_id,
                               client_order_id=client_order_id)

    async def test_closed_stop_e_stop(self):
        out = await self._run([self._t(0, "closed_stop", -1.0),
                               self._t(1, "closed_tp2", 1.5)])
        self.assertEqual(out["stops"], 1)
        self.assertEqual(out["total_closed"], 2)
        self.assertAlmostEqual(out["stop_rate_pct"], 50.0)

    async def test_manual_negativo_fica_separado(self):
        out = await self._run([self._t(0, "closed_manual", -0.8),
                               self._t(1, "closed_stop", -1.0)])
        self.assertEqual(out["stops"], 1)
        self.assertEqual(out["negative_manual_exits"], 1)

    async def test_nem_todo_r_negativo_e_stop(self):
        out = await self._run([self._t(0, "closed_be", -0.05)])
        self.assertEqual(out["stops"], 0)
        self.assertEqual(out["closed_be"], 1)

    async def test_cobertura_de_vinculo(self):
        out = await self._run([self._t(0, "closed_stop", -1.0, rec_id=1),
                               self._t(1, "closed_stop", -1.0, rec_id=None)])
        self.assertAlmostEqual(out["linked_to_snapshot_pct"], 50.0)

    async def test_r_none_excluido_sem_virar_zero(self):
        out = await self._run([self._t(0, "closed_stop", None),
                               self._t(1, "closed_tp2", 1.5)])
        self.assertIn("realized_r ausente", out["excluded_by_reason"])
        self.assertAlmostEqual(out["expectancy_r"], 1.5)

    async def test_real_nunca_somado_ao_shadow(self):
        out = await self._run([self._t(0, "closed_stop", -1.0)])
        self.assertIn("nunca são somados", out["note"])
        self.assertEqual(out["source"], "RealTrade(source=auto)")

    async def test_dedupe_usa_identidade_economica(self):
        out = await self._run([
            self._t(1, "closed_stop", -1.0, rec_id=None, exchange_order_id="E-1"),
            self._t(2, "closed_stop", -1.0, rec_id=None, exchange_order_id="E-1"),
        ])
        self.assertEqual(out["total_closed"], 1)
        self.assertEqual(out["stops"], 1)
        self.assertEqual(out["excluded_by_reason"]["duplicata"], 1)

    def test_filtra_por_closed_at_e_source_auto(self):
        src = (BACKEND / "services/assertiveness_service.py").read_text()
        block = src.split("async def real_stop_summary")[1].split("async def _gate_counterfactual")[0]
        self.assertIn('RealTrade.source == "auto"', block)
        self.assertIn("RealTrade.closed_at >= since", block)
        self.assertNotIn("RealTrade.opened_at", block)   # nunca como filtro


# ════════════════════════════════════════════════════════════════════════════
#  API / CACHE / ARQUITETURA
# ════════════════════════════════════════════════════════════════════════════
class ApiCacheArchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        p05._DIAG_CACHE.clear()

    def tearDown(self):
        p05._DIAG_CACHE.clear()

    def test_status_inclui_stop_diagnosis(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def get_p05_status")[1]
        self.assertIn('out["stop_diagnosis"]', block)
        self.assertIn("get_cached_stop_diagnosis", block)
        self.assertIn("except Exception", block)          # fail-soft

    async def test_agregado_do_painel_repassa_stop_diagnosis_oficial(self):
        from services import assertiveness_service as a

        async def _diagnosis(_days):
            return {"segments": {}, "shadow": {}}

        async def _stop(days):
            return {
                "phase": "P05.2A",
                "requested_window_days": days,
                "holdout_status": p05.HOLDOUT_SEALED,
                "patterns_verdict": "NO_PERSISTENT_STOP_PATTERN",
            }

        class _FailingSession:
            async def __aenter__(self):
                raise RuntimeError("DB de experimentos indisponível no teste")

            async def __aexit__(self, *_args):
                return False

        with patch.object(p05, "P05_ANALYTICS_ENABLED", True), \
                patch.object(p05, "get_cached_diagnosis", side_effect=_diagnosis), \
                patch.object(p05, "get_cached_stop_diagnosis", side_effect=_stop), \
                patch("db.get_session", return_value=_FailingSession()):
            out = await a._p05_section(30)

        self.assertEqual(out["stop_diagnosis"]["phase"], "P05.2A")
        self.assertEqual(out["stop_diagnosis"]["requested_window_days"], 120)
        self.assertEqual(out["stop_diagnosis"]["holdout_status"], p05.HOLDOUT_SEALED)

    def test_painel_e_agregado_usam_o_mesmo_contrato_p052a(self):
        service = (BACKEND / "services/assertiveness_service.py").read_text()
        block = service.split("async def _p05_section")[1].split(
            "async def get_assertiveness")[0]
        self.assertIn('out["stop_diagnosis"]', block)
        self.assertIn("get_cached_stop_diagnosis", block)
        self.assertIn("P052A_WINDOW_DAYS", block)

    def test_nenhum_endpoint_novo(self):
        main = (BACKEND / "main.py").read_text()
        self.assertNotIn("stop-diagnosis", main)
        self.assertNotIn('@app.post("/api/strategy/p05/stop', main)

    async def test_cache_single_flight(self):
        calls = []

        async def _fake(days):
            calls.append(days)
            return {"phase": "P05.2A"}

        with patch.object(p05, "build_stop_diagnosis", _fake):
            await p05.get_cached_stop_diagnosis(120)
            await p05.get_cached_stop_diagnosis(120)
            await p05.get_cached_stop_diagnosis(90)
        self.assertEqual(calls, [120, 90])

    async def test_erro_nao_envenena_cache(self):
        calls = []

        async def _fake(days):
            calls.append(days)
            return {"phase": "P05.2A", "error": "boom"}

        with patch.object(p05, "build_stop_diagnosis", _fake):
            await p05.get_cached_stop_diagnosis(120)
            await p05.get_cached_stop_diagnosis(120)
        self.assertEqual(len(calls), 2)

    async def test_erro_parcial_nao_envenena_cache(self):
        calls = []

        async def _fake(days):
            calls.append(days)
            return {"phase": "P05.2A", "patterns_verdict": "UNAVAILABLE",
                    "patterns_error": "boom"}

        with patch.object(p05, "build_stop_diagnosis", _fake):
            await p05.get_cached_stop_diagnosis(120)
            await p05.get_cached_stop_diagnosis(120)
        self.assertEqual(len(calls), 2)

    def test_reutiliza_cache_do_p05_sem_cache_paralelo(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("async def get_cached_stop_diagnosis")[1].split(
            "async def build_diagnosis")[0]
        self.assertIn("_DIAG_CACHE", block)
        self.assertIn("_DIAG_CACHE_LOCK", block)
        self.assertNotIn("_STOP_DIAG_CACHE", src)

    async def test_falha_de_hipotese_nao_vira_ausencia_de_padrao(self):
        async def _split(_days):
            return {
                "train": [_row(1)], "validation": [_row(2)],
                "train_count": 1, "validation_count": 1, "test_count": 1,
                "eligible_total": 3, "observed_span_days": 1,
                "oldest_resolved_at": T0.isoformat(),
                "newest_resolved_at": (T0 + timedelta(hours=2)).isoformat(),
                "young_history": True, "test_bounds": {},
                "holdout_status": p05.HOLDOUT_SEALED,
            }

        async def _real(_days):
            return {"total_closed": 0}

        from services import assertiveness_service as a
        with patch.object(p05, "load_stop_shadow_split", _split), \
                patch.object(p05, "build_stop_hypotheses", side_effect=RuntimeError("boom")), \
                patch.object(a, "real_stop_summary", _real):
            out = await p05.build_stop_diagnosis(120)
        self.assertEqual(out["patterns_verdict"], "UNAVAILABLE")
        self.assertIn("patterns_error", out)

    def test_janela_fixa_120(self):
        self.assertEqual(p05.P052A_WINDOW_DAYS, 120)

    def test_arquitetura_sem_rede_sdk_executor(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("#  P05.2A — diagnóstico LONGITUDINAL")[1].split(
            "async def build_diagnosis")[0]
        for modulo in ("ccxt", "httpx", "aiohttp", "requests",
                       "binance_signed_service", "bybit_signed_service",
                       "shadow_trade_service", "trade_manager_service",
                       "snapshot_service"):
            self.assertNotIn(f"import {modulo}", block, f"import proibido: {modulo}")
            self.assertNotIn(f"from services.{modulo}", block, f"import proibido: {modulo}")
        for proibido in ("place_order", "cancel_order", "_signed_request"):
            self.assertNotIn(proibido, block, f"ordem proibida: {proibido}")

    def test_zero_escrita_no_db(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("#  P05.2A — diagnóstico LONGITUDINAL")[1].split(
            "async def build_diagnosis")[0]
        for proibido in ("session.add(", "session.commit(", "session.flush(",
                         "session.delete(", "StrategyExperiment"):
            self.assertNotIn(proibido, block, f"escrita/experimento proibido: {proibido}")

    def test_nenhuma_env_ou_flag_nova(self):
        src = (BACKEND / "services/strategy_evidence_service.py").read_text()
        block = src.split("#  P05.2A — diagnóstico LONGITUDINAL")[1].split(
            "async def build_diagnosis")[0]
        self.assertNotIn("os.getenv", block)
        env = p05.env_snapshot()
        self.assertFalse([k for k in env if "P052" in k or "STOP_DIAG" in k.upper()])

    def test_arquivos_proibidos_intactos(self):
        """O P05.2A não tocou nos arquivos proibidos DA SUA FASE.

        A verificação é feita sobre os commits da fase (3540a699 e 0aabf34b), e
        não sobre o working tree: fases posteriores (P05.2L) instrumentam
        `snapshot_service` e `shadow_trade_service` com autorização explícita.
        """
        import subprocess
        proibidos = ("snapshot_service.py", "shadow_trade_service.py",
                     "trade_manager_service.py", "backend/main.py",
                     "frontend/dist/assets")
        for commit in ("3540a699", "0aabf34b"):
            res = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", commit],
                cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest(f"commit {commit} indisponível neste checkout")
            for proibido in proibidos:
                self.assertNotIn(proibido, res.stdout,
                                 f"P05.2A alterou arquivo proibido: {proibido}")


# ════════════════════════════════════════════════════════════════════════════
#  FRONTEND
# ════════════════════════════════════════════════════════════════════════════
class FrontendStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND.parent / "frontend/src/components/AssertivenessPanel.tsx").read_text()
        cls.block = cls.src.split("Por que as recomendações tomam stop")[-1].split("</section>")[0]

    def test_secao_presente(self):
        self.assertIn("Por que as recomendações tomam stop", self.src)

    def test_holdout_protegido(self):
        self.assertIn("🔒 protegido", self.block)
        self.assertIn("reservadas", self.block)

    def test_real_e_shadow_separados(self):
        self.assertIn("Operações reais", self.block)
        self.assertIn("contadas à parte", self.block)

    def test_avisos_obrigatorios(self):
        self.assertIn("não causas", self.block)
        self.assertIn("Nenhuma alteração foi aplicada à estratégia", self.block)
        self.assertIn("temporariamente indisponível", self.block)

    def test_mostra_wins_removidos(self):
        self.assertIn("blocking_would_remove_wins", self.block)
        self.assertIn("removeria", self.block)

    def test_sem_botao(self):
        self.assertNotIn("<button", self.block)
        self.assertNotIn("onClick", self.block)

    def test_sem_object_object(self):
        for proibido in ("{pt.train}", "{pt.validation}", "{sd.segments}",
                         "{sd.shadow}", "String(pt)"):
            self.assertNotIn(proibido, self.block, f"render bruto: {proibido}")

    def test_sem_linguagem_de_promocao(self):
        low = self.block.lower()
        for proibido in ("ativar", "promover", "aplicar", "otimizar",
                         "stop ideal", "ampliar o stop"):
            self.assertNotIn(proibido, low, f"linguagem proibida: {proibido}")


if __name__ == "__main__":
    unittest.main()
