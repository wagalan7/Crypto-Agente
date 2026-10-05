"""Lote 02 §5 — calibração V3 funcional, sem probabilidade inventada.

Positivos: modelo com ≥200 observações únicas e ≥30 labels por faixa serve
previsão EXATA (`sucessos/n`), com Wilson, `n` e proveniência; extremos 0 e 100
caem nas faixas certas; probabilidade legítima 0 ou 1 é servida como tal;
validação fora da amostra publica Brier/curva e só então vira `OOS_VALIDATED`.

Negativos: amostra global curta, faixa insuficiente (sem herdar vizinho/global/
V2/0,5), label futuro no treino, artefato de outro fingerprint/população/evento,
artefato vencido/revogado/adulterado, score fora de [0,100], e a fórmula de EV
binário aplicada a evento/payoff que não correspondem.
"""
from __future__ import annotations

import copy
import socket
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido no Lote 02"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


from services import score_v3_calibration_service as calib        # noqa: E402
from services import score_v3_service as s3                      # noqa: E402
from services import strategy_core_service as core               # noqa: E402

T0 = 1_780_000_000_000
BAR = 300_000
FINGERPRINT = "fingerprint-de-teste"
POPULACAO = "RESEARCH_SHADOW"


def observacao(indice: int, *, score: float, label, disponivel_ms=None,
               chave=None) -> dict:
    return {"opportunity_key": chave or f"op-{indice}", "score": score,
            "label": label,
            "decision_ts_ms": T0 + indice * BAR,
            "label_available_ts_ms": (T0 + indice * BAR + 10 * BAR
                                      if disponivel_ms is None else disponivel_ms)}


def amostra(*, faixas=((55.0, 40, 24), (65.0, 40, 10), (75.0, 40, 32),
                       (85.0, 40, 36), (95.0, 40, 38))) -> list:
    """Observações por faixa: (score, n, sucessos) — todas resolvidas no corte."""
    linhas, indice = [], 0
    for score, n, sucessos in faixas:
        for posicao in range(n):
            indice += 1
            linhas.append(observacao(indice, score=score,
                                     label=posicao < sucessos))
    return linhas


def ajustar(observacoes=None, *, event=calib.EVENT_TP1, cutoff=None,
            valid_until=None, population=POPULACAO,
            fingerprint=FINGERPRINT) -> dict:
    return calib.fit_calibration(
        observacoes if observacoes is not None else amostra(),
        event=event, population=population, model_fingerprint=fingerprint,
        score_config_hash="h" * 64, horizon_bars=12, bar_ms=BAR,
        censoring="EXPIRES_AFTER_HORIZON_WITHOUT_RESOLUTION",
        payoff_ref="R10A_FROZEN_MANAGEMENT", source="R09_PRE_SELECTION_OUTCOME",
        dataset_hash="d" * 64,
        cutoff_ms=T0 + 1000 * BAR if cutoff is None else cutoff,
        generated_at_ms=T0 + 1001 * BAR, valid_until_ms=valid_until,
        versions={"score": s3.SCORE_VERSION, "core": core.CORE_VERSION})


class GradeEMinimos(unittest.TestCase):
    """Faixas fixas de 10 pontos; mínimos globais e por faixa."""

    def test_faixas_fixas_e_extremos(self):
        self.assertEqual(calib.bin_index(0.0), 0)
        self.assertEqual(calib.bin_index(9.999), 0)
        self.assertEqual(calib.bin_index(10.0), 1)
        self.assertEqual(calib.bin_index(99.999), 9)
        self.assertEqual(calib.bin_index(100.0), 9, "última faixa inclui 100")
        self.assertIsNone(calib.bin_index(100.1))
        self.assertIsNone(calib.bin_index(-0.1))
        self.assertIsNone(calib.bin_index(True))
        self.assertIsNone(calib.bin_index(float("nan")))
        self.assertEqual(calib.bin_bounds(9), (90.0, 100.0, True))
        self.assertEqual(calib.bin_bounds(0), (0.0, 10.0, False))

    def test_amostra_global_curta_nao_gera_modelo(self):
        resultado = ajustar(amostra(faixas=((55.0, 40, 20),)))
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["reason_code"], calib.SAMPLE_INSUFFICIENT)
        self.assertEqual(resultado["artifact"]["state"], calib.STATE_UNAVAILABLE)

    def test_faixa_insuficiente_nao_herda_de_ninguem(self):
        resultado = ajustar(amostra(faixas=((55.0, 40, 24), (65.0, 40, 10),
                                            (75.0, 40, 32), (85.0, 40, 36),
                                            (95.0, 40, 38), (45.0, 12, 6))))
        artefato = resultado["artifact"]
        fraca = next(f for f in artefato["bins"] if f["bin"] == 4)   # 40–50
        self.assertEqual(fraca["n"], 12)
        self.assertFalse(fraca["supported"])
        self.assertIsNone(fraca["p"], "faixa fraca não publica número")
        self.assertEqual(fraca["reason_code"], calib.SAMPLE_INSUFFICIENT)
        previsao = calib.predict(artefato, score=45.0)
        self.assertFalse(previsao["available"])
        self.assertEqual(previsao["reason_code"], calib.BIN_UNSUPPORTED)
        self.assertIsNone(previsao["probability"])
        # Faixa VAZIA também não vira 0,5 nem p_global.
        vazia = calib.predict(artefato, score=5.0)
        self.assertEqual(vazia["reason_code"], calib.BIN_UNSUPPORTED)

    def test_previsao_exata_com_wilson_e_proveniencia(self):
        artefato = ajustar()["artifact"]
        self.assertEqual(artefato["state"], calib.STATE_FITTED)
        previsao = calib.predict(artefato, score=75.0,
                                 model_fingerprint=FINGERPRINT,
                                 population=POPULACAO, event=calib.EVENT_TP1)
        self.assertTrue(previsao["available"])
        self.assertAlmostEqual(previsao["probability"], 32 / 40)
        self.assertEqual(previsao["n"], 40)
        self.assertEqual(previsao["bin"], 7)
        baixo, alto = previsao["wilson_low"], previsao["wilson_high"]
        self.assertTrue(0.0 < baixo < 32 / 40 < alto < 1.0)
        self.assertEqual(previsao["provenance"]["model_fingerprint"], FINGERPRINT)
        self.assertEqual(previsao["event"], calib.EVENT_TP1)
        self.assertTrue(previsao["is_probability"])
        self.assertFalse(previsao["out_of_sample"], "FITTED não é OOS")
        # `p` nunca é score/100.
        self.assertNotAlmostEqual(previsao["probability"], 75.0 / 100.0)

    def test_probabilidade_legitima_zero_e_um(self):
        faixas = ((55.0, 40, 24), (65.0, 40, 10), (75.0, 40, 32),
                  (5.0, 40, 0), (95.0, 40, 40), (85.0, 40, 36))
        artefato = ajustar(amostra(faixas=faixas))["artifact"]
        zero = calib.predict(artefato, score=5.0)
        um = calib.predict(artefato, score=95.0)
        self.assertEqual(zero["probability"], 0.0)
        self.assertEqual(um["probability"], 1.0)
        self.assertTrue(zero["available"] and um["available"])
        self.assertGreater(zero["wilson_high"], 0.0, "incerteza não desaparece")
        self.assertLess(um["wilson_low"], 1.0)

    def test_monotonicidade_nao_e_imposta(self):
        artefato = ajustar()["artifact"]
        ps = [f["p"] for f in artefato["bins"] if f["supported"]]
        self.assertEqual(ps, [24 / 40, 10 / 40, 32 / 40, 36 / 40, 38 / 40],
                         "a faixa 60–70 continua abaixo da anterior")
        self.assertFalse(artefato["config"]["monotonicity_enforced"])
        self.assertEqual(artefato["config"]["smoothing"], "NONE")


class LabelsECensura(unittest.TestCase):
    """Label futuro não treina; censurado não é fracasso."""

    def test_label_disponivel_depois_do_corte_fica_fora(self):
        linhas = amostra()
        for linha in linhas[:50]:
            linha["label_available_ts_ms"] = T0 + 5000 * BAR     # depois do corte
        resultado = ajustar(linhas)
        self.assertFalse(resultado["ok"])
        self.assertEqual(
            resultado["artifact"]["coverage"]["excluded"][calib.LABEL_AFTER_CUTOFF],
            50)

    def test_censurado_e_duplicado_nao_contam(self):
        linhas = amostra()
        linhas.append(observacao(999, score=75.0, label=None))      # expirou
        linhas.append({**linhas[0]})                                # duplicata
        resultado = ajustar(linhas)
        cobertura = resultado["artifact"]["coverage"]
        self.assertEqual(cobertura["excluded"][calib.LABEL_UNRESOLVED], 1)
        self.assertEqual(cobertura["duplicated"], 1)
        self.assertEqual(cobertura["unique_usable"], 200)
        # Censura NÃO entra como fracasso em nenhuma faixa.
        faixa = next(f for f in resultado["artifact"]["bins"] if f["bin"] == 7)
        self.assertEqual(faixa["n"], 40)

    def test_label_invalido_e_score_fora_de_faixa_sao_excluidos(self):
        linhas = amostra()
        linhas.append(observacao(900, score=75.0, label="sim"))
        linhas.append(observacao(901, score=120.0, label=True))
        resultado = ajustar(linhas)
        cobertura = resultado["artifact"]["coverage"]["excluded"]
        self.assertEqual(cobertura[calib.LABEL_INVALID], 1)
        self.assertEqual(cobertura[calib.SCORE_OUT_OF_RANGE], 1)


class IdentidadeEValidade(unittest.TestCase):
    """Artefato de outro contrato/modelo/evento ou vencido é UNAVAILABLE."""

    def setUp(self):
        self.artefato = ajustar()["artifact"]

    def test_fingerprint_populacao_e_evento_precisam_bater(self):
        for kwargs, motivo in (
                ({"model_fingerprint": "outro"}, calib.FINGERPRINT_MISMATCH),
                ({"population": "OUTRA"}, calib.POPULATION_MISMATCH),
                ({"event": calib.EVENT_TP2}, calib.EVENT_MISMATCH),
                ({"dataset_hash": "x" * 64}, calib.DATASET_MISMATCH)):
            verdict = calib.verify_artifact(self.artefato, **kwargs)
            self.assertFalse(verdict["ok"], kwargs)
            self.assertEqual(verdict["reason_code"], motivo)
            previsao = calib.predict(self.artefato, score=75.0, **kwargs)
            self.assertIsNone(previsao["probability"])

    def test_artefato_adulterado_revogado_ou_vencido_nao_serve(self):
        adulterado = copy.deepcopy(self.artefato)
        adulterado["bins"][7]["p"] = 0.99
        self.assertEqual(calib.verify_artifact(adulterado)["reason_code"],
                         calib.ARTIFACT_INVALID)
        revogado = calib.revoke(self.artefato, reason="modelo substituído")["artifact"]
        self.assertEqual(calib.verify_artifact(revogado)["reason_code"],
                         calib.ARTIFACT_REVOKED)
        self.assertFalse(calib.predict(revogado, score=75.0)["available"])
        vencido = ajustar(valid_until=T0 + 1002 * BAR)["artifact"]
        self.assertEqual(
            calib.verify_artifact(vencido, now_ms=T0 + 2000 * BAR)["reason_code"],
            calib.ARTIFACT_EXPIRED)
        self.assertTrue(calib.verify_artifact(vencido, now_ms=T0 + 1002 * BAR)["ok"])

    def test_contrato_e_evento_desconhecidos_recusam(self):
        velho = {**self.artefato, "contract": "R08D_OLD_METADATA"}
        self.assertEqual(calib.verify_artifact(velho)["reason_code"],
                         calib.CONTRACT_MISMATCH)
        self.assertEqual(ajustar(event="P_QUALQUER_COISA")["reason_code"],
                         calib.EVENT_UNKNOWN)
        self.assertEqual(calib.verify_artifact(None)["reason_code"],
                         calib.ARTIFACT_MISSING)

    def test_score_fora_de_faixa_nao_tem_previsao(self):
        for score in (-1.0, 100.5, None, True, "80"):
            previsao = calib.predict(self.artefato, score=score)
            self.assertFalse(previsao["available"], score)
            self.assertEqual(previsao["reason_code"], calib.SCORE_OUT_OF_RANGE)


class ForaDaAmostraEAprovacao(unittest.TestCase):
    """FITTED ≠ OOS_VALIDATED ≠ ECONOMICALLY_APPROVED."""

    def test_validacao_oos_publica_brier_curva_e_cobertura(self):
        artefato = ajustar()["artifact"]
        holdout = [observacao(2000 + i, score=75.0, label=i % 2 == 0,
                              disponivel_ms=T0 + 2000 * BAR, chave=f"h-{i}")
                   for i in range(40)]
        resultado = calib.validate_out_of_sample(artefato, holdout,
                                                 now_ms=T0 + 2100 * BAR)
        self.assertTrue(resultado["ok"], resultado)
        novo = resultado["artifact"]
        self.assertEqual(novo["state"], calib.STATE_OOS_VALIDATED)
        oos = novo["metrics"]["oos"]
        self.assertEqual(oos["predictions"], 40)
        self.assertIsNotNone(oos["brier"])
        self.assertEqual(oos["reliability"][0]["bin"], 7)
        self.assertAlmostEqual(oos["reliability"][0]["observed"], 0.5)
        self.assertAlmostEqual(oos["reliability"][0]["predicted"], 32 / 40)
        self.assertEqual(oos["uncertainty"], "WILSON_95_PER_BIN")
        # O artefato OOS continua íntegro e agora é out-of-sample na previsão.
        self.assertTrue(calib.verify_artifact(novo)["ok"])
        self.assertTrue(calib.predict(novo, score=75.0)["out_of_sample"])
        # Ajuste/validação NÃO aprovam economia.
        self.assertFalse(novo["approval"]["economically_approved"])
        self.assertEqual(novo["approval"]["reason_code"],
                         calib.APPROVAL_DECISION_REQUIRED)

    def test_label_anterior_ao_corte_nao_e_out_of_sample(self):
        artefato = ajustar()["artifact"]
        antigo = [observacao(3000 + i, score=75.0, label=True,
                             disponivel_ms=T0 + 10 * BAR, chave=f"a-{i}")
                  for i in range(40)]
        resultado = calib.validate_out_of_sample(artefato, antigo,
                                                 now_ms=T0 + 2100 * BAR)
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["reason_code"], calib.SAMPLE_INSUFFICIENT)
        self.assertEqual(resultado["oos"]["coverage"]["before_cutoff_discarded"], 40)

    def test_manifesto_declara_o_contrato_sem_rodar_ajuste(self):
        manifesto = calib.calibration_manifest()
        self.assertEqual(manifesto["bins"]["width"], 10)
        self.assertTrue(manifesto["bins"]["last_bin_includes_100"])
        self.assertFalse(manifesto["bins"]["edge_search"])
        self.assertEqual(manifesto["minimums"]["global_unique_observations"], 200)
        self.assertEqual(manifesto["minimums"]["labels_per_bin"], 30)
        self.assertFalse(manifesto["insufficient_bin_inherits"])
        self.assertFalse(manifesto["economic_approval_granted_here"])
        self.assertEqual(manifesto["state"], calib.STATE_UNAVAILABLE)


class VereditoDoScoreEEv(unittest.TestCase):
    """O serviço de score reconhece o modelo real — e o EV respeita o evento."""

    def setUp(self):
        self.payload = s3.score(
            {"adx": 30.0, "htf_alignment_ratio": 1.0, "structure_quality": 0.8,
             "level_distance_atr": 0.5, "trigger_body_ratio": 0.7,
             "trigger_follow_through_atr": 0.4, "rr_tp2": 2.5,
             "entry_distance_atr": 0.1, "volume_ratio": 1.1,
             "spread_pct": 0.03, "funding_pct": 0.0},
            playbook=core.PLAYBOOK_TREND_PULLBACK, side="long")
        self.artefato = ajustar(fingerprint=self.payload["model_fingerprint"],
                                population=self.payload["population"])["artifact"]

    def test_artefato_real_devolve_probabilidade_da_faixa(self):
        veredito = s3.calibration_verdict(self.payload, artifact=self.artefato)
        self.assertEqual(veredito["calibration_state"], "AVAILABLE")
        self.assertEqual(veredito["model_state"], calib.STATE_FITTED)
        # score ≈ 73.3 ⇒ faixa 70–80 ⇒ 32/40.
        self.assertAlmostEqual(veredito["probability"], 32 / 40)
        self.assertEqual(veredito["bin"], 7)
        self.assertEqual(veredito["event"], calib.EVENT_TP1)
        self.assertFalse(veredito["v2_fallback_used"])

    def test_metadado_antigo_continua_sem_probabilidade(self):
        metadado = s3.V3Calibration(
            model_fingerprint=self.payload["model_fingerprint"],
            population=self.payload["population"], sample_size=250)
        veredito = s3.calibration_verdict(self.payload, metadado)
        self.assertEqual(veredito["calibration_state"], "AVAILABLE")
        self.assertIsNone(veredito["probability"])
        self.assertEqual(veredito["model_state"], "METADATA_ONLY")

    def test_artefato_de_outro_modelo_nao_vira_probabilidade(self):
        outro = ajustar(fingerprint="modelo-alheio")["artifact"]
        veredito = s3.calibration_verdict(self.payload, artifact=outro)
        self.assertIsNone(veredito["probability"])
        self.assertEqual(veredito["calibration_state"], s3.STATE_UNAVAILABLE)
        self.assertEqual(veredito["reason_code"], calib.FINGERPRINT_MISMATCH)

    def test_ev_binario_recusa_evento_que_nao_corresponde(self):
        # P(TP1) com payoff do TP2 não é expectativa de nada.
        recusa = s3.net_ev(probability_out_of_sample=0.8, rr_tp2=2.5, cost_r=0.1,
                           event=calib.EVENT_TP1)
        self.assertFalse(recusa["available"])
        self.assertEqual(recusa["reason_code"], s3.EVENT_PAYOFF_MISMATCH)
        # Gestão parcial/runner também não usa a fórmula binária.
        parcial = s3.net_ev(probability_out_of_sample=0.8, rr_tp2=2.5, cost_r=0.1,
                            payoff_contract=s3.PAYOFF_PARTIAL_RUNNER)
        self.assertEqual(parcial["reason_code"], s3.EVENT_PAYOFF_MISMATCH)
        # Com o evento correspondente, a fórmula binária vale.
        ok = s3.net_ev(probability_out_of_sample=0.8, rr_tp2=2.5, cost_r=0.1,
                       event=calib.EVENT_TP2)
        self.assertTrue(ok["available"])
        self.assertAlmostEqual(ok["ev_r"], 0.8 * 2.5 - 0.2 - 0.1)

    def test_ev_de_gestao_parcial_vem_do_payoff_oos(self):
        ev = s3.net_ev_from_payoff(expected_payoff_r=0.42, cost_r=0.08,
                                   source="R10A_PORTFOLIO_REPLAY_OOS",
                                   sample_size=120)
        self.assertTrue(ev["available"])
        self.assertAlmostEqual(ev["ev_r"], 0.34)
        self.assertFalse(ev["derived_from_probability"])
        # Sem payoff medido/fonte/amostra, não existe EV.
        self.assertFalse(s3.net_ev_from_payoff(expected_payoff_r=None, cost_r=0.0,
                                               source="x", sample_size=1)["available"])
        self.assertFalse(s3.net_ev_from_payoff(expected_payoff_r=0.4, cost_r=0.0,
                                               source=None, sample_size=1)["available"])
        self.assertFalse(s3.net_ev_from_payoff(expected_payoff_r=0.4, cost_r=0.0,
                                               source="x", sample_size=0)["available"])


if __name__ == "__main__":
    unittest.main()
