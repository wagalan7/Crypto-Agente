"""R11/R12 — catálogo oficial, corte temporal e gate alimentado por resultado.

Defeitos reproduzidos na revisão:
  • `validate_candidate_config(..., tag_config({'SCORE_MIN': 75}))` era recusado
    por TRÊS chaves — o envelope versionado contava como knob;
  • resultados com `resolved_at_ms > now_ms` entravam na janela recente e no
    mérito: perdas que só se resolvem amanhã derrubavam a exposição hoje;
  • o go/no-go recebia números livres montados pelo teste.

O estado persistente da histerese/geração é provado em PostgreSQL real
(`tests/pg_integration_r11_state.py`).
"""
from __future__ import annotations

import socket as _socket
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R11/R12")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import preselection_experiment_service as r12  # noqa: E402
from services import policy_state_service as ps  # noqa: E402
from services import robust_policy_service as rp  # noqa: E402
from services import strategy_evidence_service as p05  # noqa: E402

DIA = 86_400_000
AGORA = 1_760_000_000_000


def observacao(identity, outcome_r, *, dias_atras=1.0, direction="long",
               population=rp.POPULATION_SHADOW):
    return rp.Observation(symbol=f"SYM{identity}", timeframe="4h", direction=direction,
                          trigger_ms=int(AGORA - (dias_atras + 1) * DIA),
                          outcome_r=outcome_r, population=population,
                          resolved_at_ms=int(AGORA - dias_atras * DIA))


class CatalogoOficialAceitaOTipoNovo(unittest.TestCase):
    def test_envelope_versionado_nao_conta_como_knob(self):
        validado = p05.validate_candidate_config({"SCORE_MIN": 70.0},
                                                 r12.tag_config({"SCORE_MIN": 73.0}))
        self.assertEqual(validado["SCORE_MIN"], 73.0)
        self.assertEqual(validado["experiment_type"], r12.TYPE_PRE_SELECTION)
        self.assertEqual(validado["experiment_type_version"], r12.TYPE_VERSION)

    def test_regra_de_um_knob_continua_valendo_no_payload(self):
        with self.assertRaises(p05.CandidateValidationError) as erro:
            p05.validate_candidate_config(
                {"SCORE_MIN": 70.0},
                r12.tag_config({"SCORE_MIN": 73.0, "PROXIMITY_MAX_ATR": 1.0}))
        self.assertIn("1 knob", str(erro.exception))

    def test_knob_de_seguranca_continua_proibido_com_envelope(self):
        with self.assertRaises(p05.CandidateValidationError):
            p05.validate_candidate_config({"SCORE_MIN": 70.0},
                                          r12.tag_config({"LIVE_SIZE_MULT": 1.0}))

    def test_envelope_incompleto_ou_desconhecido_e_recusado(self):
        for config in ({"SCORE_MIN": 73.0, "experiment_type": r12.TYPE_PRE_SELECTION},
                       {"SCORE_MIN": 73.0, "experiment_type": "INVENTADO",
                        "experiment_type_version": "v1"}):
            with self.assertRaises(p05.CandidateValidationError):
                p05.validate_candidate_config({"SCORE_MIN": 70.0}, config)

    def test_config_legada_segue_pelo_caminho_antigo(self):
        self.assertEqual(p05.validate_candidate_config({"SCORE_MIN": 70.0},
                                                       {"SCORE_MIN": 73.0}),
                         {"SCORE_MIN": 73.0})

    def test_bloqueio_do_p051_preservado(self):
        self.assertEqual(p05.P051_BLOCK_REASON, r12.P051_BLOCK_REASON)
        self.assertFalse(r12.legacy_blocks()["new_type_bypasses_legacy_blocks"])


class CorteTemporalSuperior(unittest.TestCase):
    def _amostra(self):
        # Baseline com folga (min_baseline=20) e recente dentro de 14 dias; o
        # bloco do FUTURO resolve amanhã e não pode contar hoje.
        passado = [observacao(f"p{i}", 0.5, dias_atras=20 + i) for i in range(30)]
        recente = [observacao(f"r{i}", 0.4, dias_atras=1.0 + i * 0.01) for i in range(12)]
        futuro = [observacao(f"f{i}", -5.0, dias_atras=-1.0) for i in range(12)]
        return rp.build_sample(passado + recente + futuro,
                               population=rp.POPULATION_SHADOW)

    def test_resultado_do_futuro_fica_fora_das_janelas(self):
        baseline, recente = rp.split_windows(self._amostra(), now_ms=AGORA,
                                             config=rp.DecayConfig())
        self.assertEqual(recente.future_dropped, 12)
        for janela in (baseline, recente):
            for item in janela.observations:
                self.assertLessEqual(item.resolved_at_ms, AGORA)

    def test_perda_de_amanha_nao_derruba_a_exposicao_de_hoje(self):
        multiplicador, detalhe = rp.decay_multiplier(self._amostra(), now_ms=AGORA)
        self.assertEqual(multiplicador, 1.0, str(detalhe))

    def test_a_mesma_perda_conta_quando_o_tempo_chega(self):
        depois = AGORA + 2 * DIA
        multiplicador, detalhe = rp.decay_multiplier(self._amostra(), now_ms=depois)
        self.assertLess(multiplicador, 1.0, str(detalhe))

    def test_merito_nao_consome_o_futuro(self):
        passado = rp.Sample(rp.POPULATION_SHADOW,
                            tuple(observacao(f"a{i}", 0.4, dias_atras=10 + i)
                                  for i in range(40)))
        futuro = rp.Sample(rp.POPULATION_SHADOW,
                           tuple(observacao(f"b{i}", -9.0, dias_atras=-1.0)
                                 for i in range(40)))
        com_futuro = rp.merit_verdict(windows=[passado, futuro], net_ev_r=0.3,
                                      uncertainty_r=0.05, costs_known=True,
                                      liquidity_ok=True, quarantine_done=True,
                                      now_ms=AGORA)
        self.assertIn("FUTURE_RESULTS_EXCLUDED", com_futuro["reason_codes"])


class GateVemDoResultadoCalculado(unittest.TestCase):
    REPLAY = {
        "portfolio_version": "R10D_PORTFOLIO_REPLAY_V1",
        "metrics": {"net_expectancy_r": 0.22, "max_drawdown_r": 2.0, "resolved_n": 120},
        "fidelity": {"dimensions": {"decision_rule": "PROVEN", "price_path": "MODELED",
                                    "queue_position": "UNAVAILABLE"}},
        "trades": [],
    }
    STUDY = {
        "wf_version": "R10E_WALK_FORWARD_V1",
        "folds": [{"reason_code": "OK", "delta_net_r": 0.2},
                  {"reason_code": "OK", "delta_net_r": 0.1},
                  {"reason_code": "OK", "delta_net_r": -0.05}],
        "ci": {"available": True, "low": 0.02, "high": 0.06},
    }

    def _trades(self, n=120):
        return [{"admitted": True, "playbook": "TREND_PULLBACK" if i % 2 else "TREND_BREAKOUT",
                 "net_r": 0.2} for i in range(n)]

    def test_evidencia_deriva_dos_artefatos(self):
        evidencia = r12.gate_evidence_from_study(
            replay=self.REPLAY, study=self.STUDY, trades=self._trades(),
            enabled_playbooks=("TREND_PULLBACK", "TREND_BREAKOUT"),
            window_start_ms=AGORA - 20 * DIA, window_end_ms=AGORA,
            coverage_pct=95.0, operational_failures=0, economic_duplicates=0,
            unresolved_protection_failures=0)
        self.assertEqual(evidencia["total_shadow_trades"], 120)
        self.assertEqual(sum(evidencia["trades_per_playbook"].values()), 120)
        self.assertAlmostEqual(evidencia["net_ev_r"], 0.22)
        self.assertAlmostEqual(evidencia["uncertainty_r"], 0.02)
        self.assertAlmostEqual(evidencia["stability_ratio"], 2 / 3)
        self.assertEqual(evidencia["business_days"], 14)
        self.assertTrue(evidencia["source"]["derived_from_computed_results"])

    def test_gate_aprova_a_amostra_calculada_sem_liberar_live(self):
        evidencia = r12.gate_evidence_from_study(
            replay=self.REPLAY, study=self.STUDY, trades=self._trades(),
            enabled_playbooks=("TREND_PULLBACK", "TREND_BREAKOUT"),
            window_start_ms=AGORA - 30 * DIA, window_end_ms=AGORA,
            coverage_pct=95.0, operational_failures=0, economic_duplicates=0,
            unresolved_protection_failures=0, fidelity_discrepancy_pct=1.0)
        gate = r12.go_no_go(evidencia)
        self.assertEqual(gate["verdict"], "GO_CANDIDATE", str(gate["reason_codes"]))
        self.assertEqual(gate["live_approval"], "UNAVAILABLE")

    def test_metrica_ausente_no_artefato_vira_insuficiencia(self):
        replay = {**self.REPLAY, "metrics": {"net_expectancy_r": None,
                                             "max_drawdown_r": None}}
        evidencia = r12.gate_evidence_from_study(
            replay=replay, study={}, trades=self._trades(),
            enabled_playbooks=("TREND_PULLBACK",),
            window_start_ms=AGORA - 30 * DIA, window_end_ms=AGORA)
        gate = r12.go_no_go(evidencia)
        self.assertEqual(gate["verdict"], "NO_GO")
        for motivo in (r12.EV_INSUFFICIENT, r12.UNCERTAINTY_TOO_HIGH,
                       r12.STABILITY_INSUFFICIENT, r12.COVERAGE_INSUFFICIENT):
            self.assertIn(motivo, gate["reason_codes"])


class EstadoPersistenteTemContrato(unittest.TestCase):
    def test_identidade_inclui_experimento_versao_e_universo(self):
        chave = ps.state_key(experiment_key="exp-1", universe_version="u1")
        self.assertIn("exp-1", chave)
        self.assertIn(rp.POLICY_VERSION, chave)
        self.assertIn("u1", chave)
        self.assertNotEqual(chave, ps.state_key(experiment_key="exp-1",
                                                universe_version="u2"))

    def test_lock_da_politica_e_proprio(self):
        from services import entry_intent_service as intents
        from services import decision_observation_service as obs
        self.assertNotEqual(ps.POLICY_ADVISORY_LOCK_KEY, intents.RISK_LOCK_KEY)
        self.assertNotEqual(ps.POLICY_ADVISORY_LOCK_KEY, obs.R09_ADVISORY_LOCK_KEY)

    def test_cache_e_invalidado_por_identidade(self):
        chave = ps.state_key(experiment_key="exp-x", universe_version="u1")
        ps.cache_put(chave, {"generation": 1})
        self.assertIsNotNone(ps.cache_get(chave))
        ps.invalidate_cache(chave)
        self.assertIsNone(ps.cache_get(chave))


class SemRede(unittest.TestCase):
    def test_nenhuma_tentativa_de_rede(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
