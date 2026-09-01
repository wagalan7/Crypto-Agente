"""P05.2B — Stop Hypothesis Offline Lab (somente validação, sem execução).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco
externo, Railway ou credencial real. O holdout final permanece SELADO.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2B (hermético): {a[:1]}")


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
    """Sentinela: EXPLODE se qualquer outcome do holdout for acessado."""

    def __getitem__(self, key):
        if key in ("realized_r", "status", "outcome_class", "features"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in ("realized_r", "status", "outcome_class", "features"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().get(key, default)


def _row(i, *, status, r, regime="TREND", tier="A", timeframe="1h",
         direction="long", score=60.0, symbol="BTC/USDT:USDT", **kw):
    created = T0 + timedelta(hours=i)
    feats = {"regime": regime, "entry_zone_type": "limit_ob", "atr_pct": 1.5,
             "hour_utc": 10, "day_of_week": 1, "patterns": [], "mtf_aligned": 2,
             "funding_sentiment": "neutral"}
    feats.update(kw.pop("features", {}) or {})
    base = {
        "id": i, "dedupe_key": f"snap:{i}", "symbol": symbol, "timeframe": timeframe,
        "tier": tier, "direction": direction, "score": score,
        "status": status, "realized_r": r, "features": feats,
        "stop_distance_pct": 2.0,
        "created_at": created, "resolved_at": created + timedelta(minutes=45),
    }
    base.update(kw)
    return base


def _stage(*, adverse_n, adverse_regime="CHOP", win_n, loss_n):
    """Estágio sintético: contexto adverso 100% stop + resto lucrativo."""
    rows = []
    idx = 0
    for _ in range(adverse_n):
        rows.append(_row(idx, status="lost", r=-1.0, regime=adverse_regime))
        idx += 1
    for _ in range(win_n):
        rows.append(_row(idx, status="won_tp2", r=0.75, regime="TREND"))
        idx += 1
    for _ in range(loss_n):
        rows.append(_row(idx, status="lost", r=-1.0, regime="TREND"))
        idx += 1
    return rows


TRAIN = _stage(adverse_n=30, win_n=55, loss_n=15)
VALID = _stage(adverse_n=25, win_n=60, loss_n=15)


def _patterns(train=TRAIN, valid=VALID):
    t_axes = {a: p05.stop_segments_for_axis(train, a) for a in p05._STOP_AXES}
    v_axes = {a: p05.stop_segments_for_axis(valid, a) for a in p05._STOP_AXES}
    persistent, _others = p05.build_stop_hypotheses(t_axes, v_axes)
    return persistent


def _pattern(axis="regime", value="CHOP", classification=None, **kw):
    base = {
        "axis": axis, "value": value,
        "classification": classification or p05.STOP_PERSISTENT_ADVERSE,
        "reason": "sintético",
        "train": {"exposure": 30, "stops": 30, "stop_rate_lift_pp": 55.0,
                  "expectancy_r": -1.0, "wins_in_context": 0},
        "validation": {"exposure": 25, "stops": 25, "stop_rate_lift_pp": 60.0,
                       "expectancy_r": -1.0, "wins_in_context": 0},
        "axis_coverage_pct": {"train": 100.0, "validation": 100.0},
        "blocking_would_remove_wins": 0,
    }
    base.update(kw)
    return base


# ════════════════════════════════════════════════════════════════════════════
#  Origem das hipóteses
# ════════════════════════════════════════════════════════════════════════════
class OrigemDasHipoteses(unittest.TestCase):

    def test_sem_padrao_persistente_zero_candidatos(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, [])
        self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE)
        self.assertEqual(lab["candidates"], [])
        self.assertEqual(lab["rejected"], [])
        self.assertEqual(lab["reason_code"], "NO_PERSISTENT_ADVERSE_PATTERN")
        self.assertIn("persistente", lab["detail"])

    def test_mixed_nao_gera_hipotese(self):
        lab = p05.build_stop_offline_lab(
            TRAIN, VALID, [_pattern(classification=p05.STOP_MIXED)])
        self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE)
        self.assertEqual(lab["candidates"], [])

    def test_sample_limited_nao_gera_hipotese(self):
        lab = p05.build_stop_offline_lab(
            TRAIN, VALID, [_pattern(classification=p05.STOP_SAMPLE_LIMITED)])
        self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE)

    def test_low_coverage_nao_gera_hipotese(self):
        lab = p05.build_stop_offline_lab(
            TRAIN, VALID, [_pattern(classification=p05.STOP_LOW_COVERAGE)])
        self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE)

    def test_not_adverse_nao_gera_hipotese(self):
        lab = p05.build_stop_offline_lab(
            TRAIN, VALID, [_pattern(classification=p05.STOP_NOT_ADVERSE)])
        self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE)

    def test_origem_invalida_e_rejeitada_no_avaliador(self):
        out = p05.evaluate_stop_hypothesis(
            _pattern(classification=p05.STOP_MIXED), TRAIN, VALID)
        self.assertEqual(out["status"], p05.LAB_REJECTED)
        self.assertEqual(out["reason_code"], "ORIGIN_NOT_PERSISTENT_ADVERSE")


# ════════════════════════════════════════════════════════════════════════════
#  Eixos permitidos / bloqueados
# ════════════════════════════════════════════════════════════════════════════
class EixosPermitidos(unittest.TestCase):

    def test_allowlist_exata(self):
        self.assertEqual(set(p05.P052B_ALLOWED_AXES), {
            "tier", "timeframe", "direction", "session_utc", "regime",
            "funding_sentiment", "score_bin", "atr_band", "mtf_aligned",
            "entry_zone_type"})

    def test_eixos_bloqueados_nao_estao_na_allowlist(self):
        for axis in ("base", "patterns", "tier_timeframe", "day_of_week"):
            self.assertIn(axis, p05.P052B_BLOCKED_AXES)
            self.assertNotIn(axis, p05.P052B_ALLOWED_AXES)

    def test_eixo_proibido_nao_gera_hipotese(self):
        for axis in p05.P052B_BLOCKED_AXES:
            lab = p05.build_stop_offline_lab(
                TRAIN, VALID, [_pattern(axis=axis, value="X")])
            self.assertEqual(lab["status"], p05.LAB_NO_ELIGIBLE, axis)
            self.assertEqual(lab["candidates"], [])
            self.assertTrue(any(b["axis"] == axis
                                for b in lab["source_patterns_blocked_axis"]))

    def test_avaliador_rejeita_eixo_bloqueado(self):
        out = p05.evaluate_stop_hypothesis(_pattern(axis="base", value="BTC"),
                                           TRAIN, VALID)
        self.assertEqual(out["status"], p05.LAB_REJECTED)
        self.assertEqual(out["reason_code"], "AXIS_NOT_ALLOWED")


# ════════════════════════════════════════════════════════════════════════════
#  Limite, ordem e tipo único
# ════════════════════════════════════════════════════════════════════════════
class LimiteEOrdem(unittest.TestCase):

    def _many(self):
        return [
            _pattern(axis="regime", value="CHOP",
                     validation={"exposure": 25, "stops": 25,
                                 "stop_rate_lift_pp": 10.0, "expectancy_r": -1.0}),
            _pattern(axis="tier", value="C",
                     validation={"exposure": 20, "stops": 20,
                                 "stop_rate_lift_pp": 60.0, "expectancy_r": -1.0}),
            _pattern(axis="timeframe", value="15m",
                     validation={"exposure": 30, "stops": 30,
                                 "stop_rate_lift_pp": 40.0, "expectancy_r": -1.0}),
            _pattern(axis="direction", value="short",
                     validation={"exposure": 22, "stops": 22,
                                 "stop_rate_lift_pp": 30.0, "expectancy_r": -1.0}),
            _pattern(axis="atr_band", value="alta",
                     validation={"exposure": 21, "stops": 21,
                                 "stop_rate_lift_pp": 20.0, "expectancy_r": -1.0}),
            _pattern(axis="score_bin", value="55-60",
                     validation={"exposure": 40, "stops": 40,
                                 "stop_rate_lift_pp": 15.0, "expectancy_r": -1.0}),
        ]

    def test_maximo_quatro_hipoteses(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, self._many())
        self.assertEqual(p05.P052B_MAX_HYPOTHESES, 4)
        self.assertEqual(len(lab["source_patterns"]), 4)
        self.assertLessEqual(len(lab["candidates"]) + len(lab["rejected"]), 4)

    def test_ordem_deterministica_pela_forca_na_validacao(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, self._many())
        axes = [p["axis"] for p in lab["source_patterns"]]
        self.assertEqual(axes, ["tier", "timeframe", "direction", "atr_band"])
        again = p05.build_stop_offline_lab(TRAIN, VALID, self._many())
        self.assertEqual([p["axis"] for p in again["source_patterns"]], axes)

    def test_uma_hipotese_por_padrao(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, self._many())
        pares = [(h["axis"], h["value"])
                 for h in lab["candidates"] + lab["rejected"]]
        self.assertEqual(len(pares), len(set(pares)))

    def test_somente_stop_context_block(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, _patterns())
        for h in lab["candidates"] + lab["rejected"]:
            self.assertEqual(h["type"], "STOP_CONTEXT_BLOCK")
        self.assertEqual(p05.P052B_HYPOTHESIS_TYPE, "STOP_CONTEXT_BLOCK")

    def test_hash_canonico_estavel(self):
        a = p05._hypothesis_hash("regime", "CHOP")
        b = p05._hypothesis_hash("regime", "CHOP")
        self.assertEqual(a, b)
        self.assertNotEqual(a, p05._hypothesis_hash("regime", "TREND"))
        self.assertEqual(len(a), 16)


# ════════════════════════════════════════════════════════════════════════════
#  Comparação pareada
# ════════════════════════════════════════════════════════════════════════════
class ComparacaoPareada(unittest.TestCase):

    def test_unknown_excluido_dos_dois_lados(self):
        rows = list(VALID)
        for i in range(5):
            r = _row(900 + i, status="lost", r=-1.0)
            r["features"] = dict(r["features"])
            r["features"]["regime"] = None
            rows.append(r)
        cmp = p05.stop_block_comparison(rows, "regime", "CHOP")
        self.assertEqual(cmp["unknown_excluded"], 5)
        self.assertEqual(cmp["evaluable"], len(VALID))
        self.assertEqual(cmp["champion"]["exposure"], len(VALID))
        self.assertEqual(cmp["operations_kept"] + cmp["operations_removed"],
                         cmp["evaluable"])

    def test_stops_evitados_e_wins_removidos(self):
        cmp = p05.stop_block_comparison(VALID, "regime", "CHOP")
        self.assertEqual(cmp["operations_removed"], 25)
        self.assertEqual(cmp["stops_avoided"], 25)
        self.assertEqual(cmp["wins_removed"], 0)
        cmp2 = p05.stop_block_comparison(VALID, "regime", "TREND")
        self.assertEqual(cmp2["operations_removed"], 75)
        self.assertEqual(cmp2["wins_removed"], 60)
        self.assertEqual(cmp2["stops_avoided"], 15)

    def test_taxa_por_exposicao_nao_contagem(self):
        cmp = p05.stop_block_comparison(VALID, "regime", "CHOP")
        self.assertEqual(cmp["champion"]["stop_rate_pct"], 40.0)
        self.assertEqual(cmp["candidate"]["stop_rate_pct"], 20.0)
        self.assertEqual(cmp["candidate"]["exposure"], 75)
        self.assertEqual(cmp["stop_rate_delta_pp"], -20.0)

    def test_operacoes_preservadas_pct(self):
        cmp = p05.stop_block_comparison(VALID, "regime", "CHOP")
        self.assertEqual(cmp["operations_preserved_pct"], 75.0)

    def test_bootstrap_deterministico(self):
        a = p05.stop_block_comparison(VALID, "regime", "CHOP")
        b = p05.stop_block_comparison(VALID, "regime", "CHOP")
        self.assertEqual(a["paired_expectancy_delta_ci"], b["paired_expectancy_delta_ci"])
        self.assertEqual(a["stop_rate_delta_ci"], b["stop_rate_delta_ci"])

    def test_delta_pareado_usa_as_mesmas_oportunidades(self):
        cmp = p05.stop_block_comparison(VALID, "regime", "CHOP")
        paired = cmp["paired_expectancy_delta_ci"]
        self.assertEqual(paired["method"], "paired-annotation-bootstrap")
        self.assertGreater(paired["low"], 0)
        sr = cmp["stop_rate_delta_ci"]
        self.assertEqual(sr["method"], "paired-opportunity-bootstrap")
        self.assertLess(sr["high_pp"], 0)

    def test_saidas_protetivas_e_expired_removidos_sao_contados(self):
        rows = list(VALID)
        rows.append(_row(800, status="won_tp1_be", r=0.1, regime="CHOP"))
        rows.append(_row(801, status="expired", r=0.0, regime="CHOP"))
        cmp = p05.stop_block_comparison(rows, "regime", "CHOP")
        self.assertEqual(cmp["protected_exits_removed"], 1)
        self.assertEqual(cmp["expired_removed"], 1)

    def test_eixo_desconhecido_explode(self):
        with self.assertRaises(KeyError):
            p05.stop_block_comparison(VALID, "eixo_inexistente", "x")


# ════════════════════════════════════════════════════════════════════════════
#  Gate somente de validação
# ════════════════════════════════════════════════════════════════════════════
class GateDeValidacao(unittest.TestCase):

    def test_validacao_apoiada_com_todos_os_checks(self):
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, VALID)
        self.assertEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        self.assertTrue(all(c["passed"] for c in out["checks"]))
        self.assertEqual(out["reason_code"], "ALL_VALIDATION_CHECKS_PASSED")

    def test_status_proibidos_nunca_aparecem(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, _patterns())
        blob = repr(lab)
        for proibido in ("OFFLINE_VALIDATED", '"SHADOW"', "ELIGIBLE",
                         "PROMOTED", '"ACTIVE"'):
            self.assertNotIn(proibido, blob)
        self.assertIn(lab["status"], (p05.LAB_NO_ELIGIBLE, p05.LAB_INSUFFICIENT,
                                      p05.LAB_REJECTED, p05.LAB_VALIDATION_SUPPORTED))

    def test_amostra_afetada_minima(self):
        pequeno = _stage(adverse_n=5, win_n=60, loss_n=15)
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, pequeno)
        self.assertEqual(out["status"], p05.LAB_INSUFFICIENT)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("amostra_afetada_validacao", nomes)
        self.assertEqual(p05.P051_MIN_AFFECTED, 20)

    def test_preservacao_minima_de_70pct(self):
        # 40 removidas em 100 → preserva 60% < 70%
        agressivo = _stage(adverse_n=40, win_n=45, loss_n=15)
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, agressivo)
        self.assertNotEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("operacoes_preservadas", nomes)
        self.assertEqual(p05.P052B_MIN_PRESERVED_PCT, 70.0)

    def test_cobertura_do_eixo_insuficiente(self):
        rows = []
        for i, r in enumerate(VALID):
            row = dict(r)
            row["features"] = dict(r["features"])
            if i % 2 == 0 and row["features"]["regime"] != "CHOP":
                row["features"]["regime"] = None
            rows.append(row)
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertEqual(out["status"], p05.LAB_INSUFFICIENT)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("cobertura_validacao", nomes)

    def test_contexto_removido_precisa_ser_negativo(self):
        # contexto "adverso" que na verdade é lucrativo → reprovado
        rows = _stage(adverse_n=25, win_n=60, loss_n=15)
        for r in rows:
            if r["features"]["regime"] == "CHOP":
                r["status"], r["realized_r"] = "won_tp2", 0.9
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertEqual(out["status"], p05.LAB_REJECTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("removidas_negativas", nomes)
        self.assertIn("ic_superior_removidas_negativo", nomes)

    def test_ic_superior_das_removidas_precisa_ser_negativo(self):
        rows = _stage(adverse_n=25, win_n=60, loss_n=15)
        chop = [r for r in rows if r["features"]["regime"] == "CHOP"]
        for i, r in enumerate(chop):          # média ~0, IC cruza zero
            if i % 2 == 0:
                r["status"], r["realized_r"] = "won_tp2", 1.0
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertNotEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("ic_superior_removidas_negativo", nomes)

    def test_stop_rate_precisa_cair(self):
        # contexto com taxa de stop IGUAL ao baseline: candidato não melhora
        rows = []
        for i in range(50):
            rows.append(_row(i, status="lost", r=-1.0, regime="CHOP"))
        for i in range(50):
            rows.append(_row(100 + i, status="lost", r=-1.0, regime="TREND"))
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertNotEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("stop_rate_menor", nomes)

    def test_expectancy_do_candidato_precisa_ser_positiva(self):
        rows = _stage(adverse_n=25, win_n=20, loss_n=55)
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertNotEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("expectancy_candidato_positiva", nomes)
        self.assertIn("soma_r_candidato_positiva", nomes)

    def test_profit_factor_e_drawdown_sao_avaliados(self):
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, VALID)
        nomes = {c["name"] for c in out["checks"]}
        self.assertIn("profit_factor_nao_pior", nomes)
        self.assertIn("drawdown_nao_pior", nomes)
        val = out["validation"]
        self.assertLessEqual(val["candidate"]["max_drawdown_r"],
                             val["champion"]["max_drawdown_r"])
        self.assertGreaterEqual(val["candidate"]["profit_factor"],
                                val["champion"]["profit_factor"])

    def test_pf_indefinido_do_champion_reprova_candidato_com_perdas(self):
        self.assertFalse(p05._pf_not_worse(2.0, None))
        self.assertTrue(p05._pf_not_worse(None, 1.5))
        self.assertTrue(p05._pf_not_worse(2.0, 1.5))
        self.assertFalse(p05._pf_not_worse(1.0, 1.5))

    def test_regressao_material_reprova(self):
        rows = _stage(adverse_n=25, win_n=60, loss_n=15)
        # O bloqueio remove os ganhadores de 15m e deixa só os perdedores:
        # o segmento sobrevivente fica materialmente PIOR que no champion.
        for i in range(12):
            rows.append(_row(700 + i, status="won_tp2", r=0.3,
                             regime="CHOP", timeframe="15m"))
        for i in range(12):
            rows.append(_row(750 + i, status="lost", r=-1.0,
                             regime="TREND", timeframe="15m"))
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, rows)
        self.assertNotEqual(out["status"], p05.LAB_VALIDATION_SUPPORTED)
        nomes = {c["name"] for c in out["checks"] if not c["passed"]}
        self.assertIn("sem_regressao_material", nomes)

    def test_nenhum_vencedor_forcado(self):
        neutro = _stage(adverse_n=25, win_n=60, loss_n=15)
        for r in neutro:
            if r["features"]["regime"] == "CHOP":
                r["status"], r["realized_r"] = "won_tp2", 0.75
                r["features"] = dict(r["features"])
                r["features"]["regime"] = "CHOP"
        lab = p05.build_stop_offline_lab(TRAIN, neutro, [_pattern()])
        self.assertEqual(lab["candidates"], [])
        self.assertEqual(lab["status"], p05.LAB_REJECTED)


# ════════════════════════════════════════════════════════════════════════════
#  Contrato de saída
# ════════════════════════════════════════════════════════════════════════════
class ContratoDeSaida(unittest.TestCase):

    def test_shape_minimo(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, _patterns())
        for key in ("phase", "execution_mode", "read_only", "executable",
                    "promotable", "shadow_supported", "holdout_status", "status",
                    "reason_code", "detail", "source_patterns", "candidates",
                    "rejected", "limitations", "computed_at"):
            self.assertIn(key, lab)
        self.assertEqual(lab["phase"], "P05.2B")
        self.assertEqual(lab["execution_mode"], "ANALYTICS_ONLY")
        self.assertTrue(lab["read_only"])
        self.assertFalse(lab["executable"])
        self.assertFalse(lab["promotable"])
        self.assertFalse(lab["shadow_supported"])
        self.assertEqual(lab["holdout_status"], "SEALED")
        self.assertFalse(lab["holdout_outcomes_read"])
        self.assertFalse(lab["holdout_metrics_computed"])
        self.assertTrue(lab["requires_future_holdout_review"])

    def test_shape_do_candidato(self):
        out = p05.evaluate_stop_hypothesis(_pattern(), TRAIN, VALID)
        for key in ("hash", "axis", "value", "type", "source_evidence", "train",
                    "validation", "checks", "wins_removed", "stops_avoided",
                    "risks", "executable", "requires_future_holdout_review"):
            self.assertIn(key, out)
        self.assertFalse(out["executable"])
        self.assertTrue(out["requires_future_holdout_review"])

    def test_sem_config_executavel_nem_promotion_plan(self):
        lab = p05.build_stop_offline_lab(TRAIN, VALID, _patterns())
        blob = repr(lab)
        for proibido in ("candidate_config", "promotion_plan", "endpoint",
                         "feature_flag", "apply_to", "activate"):
            self.assertNotIn(proibido, blob)


# ════════════════════════════════════════════════════════════════════════════
#  Holdout selado
# ════════════════════════════════════════════════════════════════════════════
class HoldoutSelado(unittest.TestCase):

    def test_sentinela_do_holdout_nunca_e_acessada(self):
        sealed = [_SealedRow(_row(i, status="lost", r=-1.0)) for i in range(40)]
        lab = p05.build_stop_offline_lab(TRAIN, VALID, _patterns())
        self.assertEqual(lab["holdout_status"], "SEALED")
        # o laboratório não recebe e não toca no holdout
        for row in sealed:
            with self.assertRaises(AssertionError):
                _ = row["realized_r"]

    def test_diagnostico_nao_passa_o_teste_para_o_laboratorio(self):
        split = {
            "train": TRAIN, "validation": VALID,
            "test": [_SealedRow(_row(i, status="lost", r=-1.0)) for i in range(30)],
            "train_count": len(TRAIN), "validation_count": len(VALID),
            "test_count": 30, "eligible_total": len(TRAIN) + len(VALID) + 30,
            "observed_span_days": 120.0, "oldest_resolved_at": None,
            "newest_resolved_at": None, "young_history": False,
            "test_bounds": {"first_resolved_at": None, "last_resolved_at": None},
            "holdout_status": "SEALED", "holdout_outcomes_read": False,
            "holdout_metrics_computed": False,
        }

        async def _fake_split(days):
            return split

        async def _fake_real(days):
            return {"total_closed": 0}

        with patch.object(p05, "load_stop_shadow_split", _fake_split):
            from services import assertiveness_service as a
            with patch.object(a, "real_stop_summary", _fake_real):
                out = asyncio.run(p05.build_stop_diagnosis(120))
        lab = out["offline_lab"]
        self.assertEqual(lab["holdout_status"], "SEALED")
        self.assertFalse(lab["holdout_outcomes_read"])
        self.assertFalse(lab["holdout_metrics_computed"])
        self.assertEqual(out["sample"]["test_count"], 30)

    def test_laboratorio_falha_soft_sem_derrubar_o_p052a(self):
        def _boom(*a, **k):
            raise RuntimeError("lab quebrou")

        split = {
            "train": TRAIN, "validation": VALID, "train_count": len(TRAIN),
            "validation_count": len(VALID), "test_count": 10,
            "eligible_total": 210, "observed_span_days": 120.0,
            "oldest_resolved_at": None, "newest_resolved_at": None,
            "young_history": False,
            "test_bounds": {"first_resolved_at": None, "last_resolved_at": None},
            "holdout_status": "SEALED", "holdout_outcomes_read": False,
            "holdout_metrics_computed": False,
        }

        async def _fake_split(days):
            return split

        async def _fake_real(days):
            return {"total_closed": 0}

        with patch.object(p05, "load_stop_shadow_split", _fake_split), \
                patch.object(p05, "build_stop_offline_lab", _boom):
            from services import assertiveness_service as a
            with patch.object(a, "real_stop_summary", _fake_real):
                out = asyncio.run(p05.build_stop_diagnosis(120))
        self.assertEqual(out["offline_lab"]["status"], "UNAVAILABLE")
        self.assertIn("shadow", out)
        self.assertIn("segments", out)
        self.assertFalse(p05._stop_diagnosis_cacheable(out))


# ════════════════════════════════════════════════════════════════════════════
#  Arquitetura: zero escrita, zero executor, zero rede
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        src = (BACKEND / "services" / "strategy_evidence_service.py").read_text()
        cls.block = src.split("P05.2B — STOP HYPOTHESIS OFFLINE LAB")[-1].split(
            "async def build_stop_diagnosis")[0]

    def test_sem_escrita_no_banco(self):
        for proibido in ("session.add(", "session.commit(", "session.flush(",
                         "session.merge(", "session.delete(", "await session.execute(update"):
            self.assertNotIn(proibido, self.block)

    def test_sem_strategy_experiment(self):
        self.assertNotIn("StrategyExperiment", self.block)
        self.assertNotIn("strategy_experiment", self.block)

    def test_sem_executor_rede_ou_sdk(self):
        for proibido in ("import ccxt", "exchange_service", "shadow_trade_service",
                         "place_order", "requests.", "aiohttp", "httpx", "urllib"):
            self.assertNotIn(proibido, self.block)

    def test_sem_env_ou_flag_nova(self):
        self.assertNotIn('getenv("P052B', self.block)
        self.assertNotIn("_env_int(\"P052B", self.block)
        self.assertNotIn("_env_float(\"P052B", self.block)

    def test_reutiliza_contratos_existentes(self):
        for contrato in ("_partition_outcomes", "compute_evidence_metrics",
                         "bootstrap_paired_membership_delta_ci", "wilson_interval",
                         "_material_segment_regressions", "P051_MIN_AFFECTED",
                         "P05_MIN_FEATURE_COVERAGE_PCT", "_STOP_AXES",
                         "P05_RANDOM_SEED"):
            self.assertIn(contrato, self.block)

    def test_sem_endpoint_novo_no_main(self):
        main = (BACKEND / "main.py").read_text()
        self.assertNotIn("p05/offline-lab", main)
        self.assertNotIn("p05.2b", main.lower())

    def test_lab_entra_no_cache_existente(self):
        src = (BACKEND / "services" / "strategy_evidence_service.py").read_text()
        diag = src.split("async def build_stop_diagnosis")[-1].split(
            "def _stop_diagnosis_cacheable")[0]
        self.assertIn("build_stop_offline_lab", diag)
        cacheable = src.split("def _stop_diagnosis_cacheable")[-1].split(
            "async def get_cached_stop_diagnosis")[0]
        self.assertIn("offline_lab", cacheable)


class Frontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        panel = (BACKEND.parent / "frontend" / "src" / "components"
                 / "AssertivenessPanel.tsx")
        cls.src = panel.read_text()
        cls.block = cls.src.split("P05.2B Laboratório offline")[-1].split(
            "padrões observados, não causas")[0]

    def test_secao_existe(self):
        self.assertIn("Laboratório offline de redução de stops", self.src)

    def test_sem_botao_ou_acao(self):
        for proibido in ("<button", "onClick", "Aplicar", "Promover", "Ativar",
                         "Testar agora", "Executar"):
            self.assertNotIn(proibido, self.block)

    def test_avisos_obrigatorios(self):
        self.assertIn("Validação apoiada não significa aprovação", self.block)
        self.assertIn("O teste final ainda não foi aberto", self.block)
        self.assertIn("Nenhuma alteração foi aplicada à estratégia", self.block)
        self.assertIn("🔒 holdout ainda fechado", self.block)

    def test_estado_vazio(self):
        self.assertIn("Nenhuma hipótese elegível por enquanto", self.block)
        self.assertIn("Os dados continuam sendo coletados", self.block)

    def test_mostra_stops_evitados_e_wins_removidos(self):
        self.assertIn("stops evitados", self.block)
        self.assertIn("operações vencedoras que sairiam junto", self.block)
        self.assertIn("preserva", self.block)

    def test_dist_nao_foi_editado(self):
        dist = BACKEND.parent / "frontend" / "dist" / "assets"
        if dist.exists():
            for f in dist.glob("*.js"):
                self.assertNotIn("Laboratório offline de redução de stops",
                                 f.read_text(errors="ignore"))


if __name__ == "__main__":
    unittest.main()
