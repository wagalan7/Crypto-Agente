"""Lote 02 §6/§7 — status DERIVADO e sentinelas.

Status: estado + qualidade + `last_observed_at` vindos do que existe de fato;
`ERROR` nunca se disfarça de `NOT_STARTED`; o GET não dispara replay/fitting.

Sentinelas: outcome do holdout continua inacessível ao caminho de treino; dado
FUTURO não muda decisão anterior (artefato do mesmo corte é idêntico); coleta
desligada não muda o bot (provado na suíte de captura).
"""
from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido no Lote 02"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


from services import preselection_observation_service as pre       # noqa: E402
from services import research_batch_service as batch               # noqa: E402
from services import research_manifest_service as rm               # noqa: E402
from services import score_v3_calibration_service as calib         # noqa: E402
from tests.test_lote02_research_manifest import manifesto          # noqa: E402
from tests.test_lote02_v3_calibration import ajustar, observacao, T0, BAR  # noqa: E402


class StatusDerivado(unittest.TestCase):
    """Nada de string fixa: o estado vem da coleta, do artefato e do manifesto."""

    def setUp(self):
        pre.reset_coverage()

    def tearDown(self):
        pre.reset_coverage()

    def test_coleta_desligada_e_not_started(self):
        with patch.dict(os.environ, {pre.MODE_ENV: pre.MODE_INACTIVE}):
            evidencia = batch.evidence_status()
        prospectiva = evidencia["prospective_simulation"]
        self.assertEqual(prospectiva["state"], batch.EVIDENCE_NOT_STARTED)
        self.assertFalse(prospectiva["collection_enabled"])
        self.assertIsNone(prospectiva["last_observed_at"])

    def test_ligada_sem_amostra_e_observing_e_com_amostra_e_collected(self):
        with patch.dict(os.environ, {pre.MODE_ENV: pre.MODE_OBSERVE}):
            self.assertEqual(batch.evidence_status()["prospective_simulation"]["state"],
                             batch.EVIDENCE_OBSERVING)
            pre.record_cycle_coverage(pre.cycle_coverage(
                cycle_ts_ms=T0, symbols_requested=10, symbols_evaluated=10,
                candidates_observed=14, reasons={pre.COVERAGE_OBSERVED: 14}))
            prospectiva = batch.evidence_status()["prospective_simulation"]
        self.assertEqual(prospectiva["state"], batch.EVIDENCE_COLLECTED)
        self.assertEqual(prospectiva["candidates_observed"], 14)
        self.assertEqual(prospectiva["last_observed_at"], T0)
        self.assertEqual(prospectiva["cycles_observed"], 1)

    def test_erro_de_leitura_nao_e_not_started(self):
        falha = patch.object(pre, "coverage_snapshot",
                             side_effect=RuntimeError("banco fora"))
        with falha:
            evidencia = batch.evidence_status()
        prospectiva = evidencia["prospective_simulation"]
        self.assertEqual(prospectiva["state"], batch.EVIDENCE_ERROR)
        self.assertNotEqual(prospectiva["state"], batch.EVIDENCE_NOT_STARTED)
        self.assertEqual(prospectiva["reason_code"], "OBSERVATION_READ_UNAVAILABLE")

    def test_validacao_economica_segue_o_artefato(self):
        sem = batch.evidence_status()["economic_validation"]
        self.assertEqual(sem["state"], batch.EVIDENCE_NOT_STARTED)
        self.assertEqual(sem["reason_code"], calib.ARTIFACT_MISSING)
        artefato = ajustar()["artifact"]
        ajustado = batch.evidence_status(artifact=artefato)["economic_validation"]
        self.assertEqual(ajustado["state"], batch.EVIDENCE_FITTED)
        self.assertEqual(ajustado["quality"], "IN_SAMPLE")
        self.assertFalse(ajustado["economically_approved"])
        holdout = [observacao(4000 + i, score=75.0, label=i % 3 == 0,
                              disponivel_ms=T0 + 2000 * BAR, chave=f"s-{i}")
                   for i in range(45)]
        validado = calib.validate_out_of_sample(artefato, holdout,
                                                now_ms=T0 + 2100 * BAR)["artifact"]
        oos = batch.evidence_status(artifact=validado)["economic_validation"]
        self.assertEqual(oos["state"], batch.EVIDENCE_OOS_VALIDATED)
        self.assertEqual(oos["quality"], "OOS")
        self.assertFalse(oos["economically_approved"],
                         "validar fora da amostra não aprova economia")
        revogado = calib.revoke(validado, reason="substituído")["artifact"]
        self.assertEqual(batch.evidence_status(artifact=revogado)
                         ["economic_validation"]["state"], batch.EVIDENCE_ERROR)

    def test_aprovacao_humana_vem_do_manifesto(self):
        bloqueada = batch.evidence_status()["human_approval"]
        self.assertEqual(bloqueada["state"], batch.EVIDENCE_BLOCKED)
        self.assertFalse(bloqueada["real_study_allowed"])
        teste = batch.evidence_status(
            manifest=rm.parse_manifest(manifesto()))["human_approval"]
        self.assertEqual(teste["state"], rm.STATE_TEST_ONLY)
        self.assertEqual(teste["quality"], "TEST_ONLY")
        self.assertFalse(teste["real_study_allowed"])
        aprovada = batch.evidence_status(manifest=rm.parse_manifest(
            manifesto(decision_state=rm.DECISION_APPROVED)))["human_approval"]
        self.assertEqual(aprovada["state"], rm.STATE_APPROVED)
        self.assertTrue(aprovada["real_study_allowed"])
        self.assertIsNotNone(aprovada["last_observed_at"])

    def test_get_nao_dispara_trabalho_caro(self):
        """O status não chama replay, walk-forward nem fitting."""
        from services import portfolio_replay_service as pf
        from services import walk_forward_service as wf
        with patch.object(pf, "run_portfolio",
                          side_effect=AssertionError("replay no GET")), \
                patch.object(wf, "run_walk_forward",
                             side_effect=AssertionError("walk-forward no GET")), \
                patch.object(calib, "fit_calibration",
                             side_effect=AssertionError("fitting no GET")):
            evidencia = batch.evidence_status()
            resumo = batch.lote_final_summary()
        self.assertFalse(evidencia["expensive_work_in_get"])
        self.assertEqual(resumo["state"], "LOCAL_RESEARCH_ONLY")


class Sentinelas(unittest.TestCase):
    """Holdout selado, dado futuro inerte e cobertura honesta."""

    def test_dado_futuro_nao_muda_decisao_anterior(self):
        base = ajustar()["artifact"]
        futuras = [observacao(5000 + i, score=75.0, label=True,
                              disponivel_ms=T0 + 5000 * BAR, chave=f"f-{i}")
                   for i in range(60)]
        from tests.test_lote02_v3_calibration import amostra
        depois = ajustar(amostra() + futuras)["artifact"]
        self.assertEqual(base["bins"], depois["bins"],
                         "label posterior ao corte não altera o modelo do corte")
        for score in (55.0, 65.0, 75.0, 85.0, 95.0):
            self.assertEqual(calib.predict(base, score=score)["probability"],
                             calib.predict(depois, score=score)["probability"],
                             score)
        # O hash MUDA de propósito: a cobertura registra que 60 linhas futuras
        # foram vistas e excluídas. Honestidade de cobertura ≠ decisão alterada.
        self.assertNotEqual(base["artifact_hash"], depois["artifact_hash"])
        self.assertEqual(base["coverage"]["unique_usable"],
                         depois["coverage"]["unique_usable"])
        self.assertEqual(
            depois["coverage"]["excluded"][calib.LABEL_AFTER_CUTOFF], 60)

    def test_outcome_do_holdout_nao_entra_no_treino(self):
        """O artefato declara o corte e exclui o que só ficou conhecível depois."""
        artefato = ajustar()["artifact"]
        self.assertTrue(artefato["training"]["labels_after_cutoff_excluded"])
        self.assertEqual(artefato["training"]["cutoff_ms"], T0 + 1000 * BAR)
        # E a validação OOS recusa consumir o que era anterior ao corte.
        antigo = [observacao(6000 + i, score=75.0, label=True,
                             disponivel_ms=T0 + BAR, chave=f"p-{i}")
                  for i in range(40)]
        resultado = calib.validate_out_of_sample(artefato, antigo,
                                                 now_ms=T0 + 3000 * BAR)
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["oos"]["coverage"]["before_cutoff_discarded"], 40)

    def test_cobertura_recusa_motivo_desconhecido(self):
        with self.assertRaises(ValueError):
            pre.cycle_coverage(cycle_ts_ms=T0, symbols_requested=1,
                               symbols_evaluated=1, candidates_observed=0,
                               reasons={"MOTIVO_INVENTADO": 1})
        payload = pre.cycle_coverage(cycle_ts_ms=T0, symbols_requested=5,
                                     symbols_evaluated=2, candidates_observed=0,
                                     reasons={pre.COVERAGE_SYMBOL_TIMEOUT: 3})
        self.assertEqual(payload["not_evaluated"], 3)
        self.assertFalse(payload["is_opportunity"])
        self.assertFalse(payload["fabricates_prices_or_outcomes"])


if __name__ == "__main__":
    unittest.main()
