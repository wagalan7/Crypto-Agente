"""R10 — o veredito precisa do RESULTADO da política, não do IC pareado.

Regressão do defeito confirmado na revisão: dez oportunidades comuns com
baseline 0R e candidato +0,1R, mais uma exclusiva do baseline (+9R) e outra
exclusiva do candidato (−9R). O baseline soma +9R e o candidato −8R, mas o
código escolhia `CANDIDATE` pelo intervalo pareado positivo.

Cobre também o runner que EXECUTA cada dobra: seleção só no treino, avaliação
fora da amostra, pares vazios sem vencedor e futuro não consumido.
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
    raise RuntimeError("REDE BLOQUEADA no teste do runner walk-forward")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import walk_forward_service as wf  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000


def linha(key, net_r, passo, *, disponivel_em=None):
    """Linha de resultado como o replay corrigido a produz: além do instante da
    DECISÃO, o instante em que o resultado ficou DISPONÍVEL (uma vela depois,
    por default — o fechamento da vela que produziu a saída)."""
    decidido = T0 + passo * BAR
    return {"opportunity_id": key, "net_r": net_r, "decision_ts_ms": decidido,
            "result_available_ts_ms": (T0 + disponivel_em * BAR
                                       if disponivel_em is not None else decidido + BAR)}


class CandidatoGlobalmentePior(unittest.TestCase):
    def setUp(self):
        self.base = [linha(f"c{i}", 0.0, i) for i in range(10)]
        self.cand = [linha(f"c{i}", 0.1, i) for i in range(10)]
        self.base.append(linha("so-baseline", 9.0, 10))
        self.cand.append(linha("so-candidato", -9.0, 11))
        self.pareado = wf.pair_opportunities(self.base, self.cand)

    def test_delta_da_politica_inteira_conta_os_dois_lados(self):
        delta = wf.policy_delta_r(self.pareado)
        self.assertTrue(delta["available"])
        # 10 pares × +0,1 = +1,0 ; adicionada −9 ; removida +9 ⇒ −17
        self.assertAlmostEqual(delta["value"], -17.0)
        self.assertEqual(delta["only_baseline_n"], 1)
        self.assertEqual(delta["only_candidate_n"], 1)

    def test_ic_pareado_positivo_nao_elege_candidato_pior(self):
        disciplina = {stage: "train" for stage in wf.FITTED_STAGES}
        disciplina["candidate_selected_on"] = "validation"
        veredito = wf.verdict(
            folds=wf.rolling_windows(start_ms=T0, end_ms=T0 + 200 * BAR, bar_ms=BAR,
                                     train_bars=40, test_bars=10)["folds"],
            discipline=wf.fold_discipline(disciplina),
            coverage=wf.coverage_guard({"considered": 12, "resolved": 12},
                                       {"considered": 12, "resolved": 12}),
            costs_complete=True, horizon_sufficient=True, paired=self.pareado,
            ci={"available": True, "low": 0.05, "high": 0.15},
            policy_delta=wf.policy_delta_r(self.pareado), studies_executed=True)
        self.assertIsNone(veredito["winner"])
        self.assertEqual(veredito["reason_codes"], (wf.DELTA_DISAGREES_WITH_CI,))
        self.assertAlmostEqual(veredito["policy_delta_r"], -17.0)

    def test_desconhecido_no_delta_nao_vira_zero(self):
        cand = list(self.cand)
        cand[0] = {"opportunity_id": "c0", "net_r": None, "decision_ts_ms": T0}
        delta = wf.policy_delta_r(wf.pair_opportunities(self.base, cand))
        self.assertFalse(delta["available"])
        self.assertEqual(delta["reason_code"], wf.POLICY_DELTA_UNKNOWN)


class RunnerExecutaAsDobras(unittest.TestCase):
    def _serie(self, base_r, cand_r, n=60):
        base = [linha(f"k{i}", base_r, i) for i in range(n)]
        cand = [linha(f"k{i}", cand_r, i) for i in range(n)]
        return base, cand

    def _folds(self, n=60):
        return wf.rolling_windows(start_ms=T0, end_ms=T0 + (n + 40) * BAR, bar_ms=BAR,
                                  train_bars=20, test_bars=5, step_bars=5)["folds"]

    def test_dobras_sao_realmente_executadas(self):
        base, cand = self._serie(0.0, 0.2)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self._folds(),
                                     bar_ms=BAR, costs_complete=True,
                                     horizon_sufficient=True, seed=3)
        self.assertGreater(estudo["folds_executed"], 1)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertTrue(all("train_n" in item and "test_n" in item for item in executadas))
        self.assertTrue(any(item["test_n"] > 0 for item in executadas))
        # Seleção é decidida no TREINO da própria dobra.
        self.assertIn("candidate_selected_on_train", executadas[-1])

    def test_aa_integral_nao_produz_vencedor(self):
        base, cand = self._serie(0.3, 0.3)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self._folds(),
                                     bar_ms=BAR, costs_complete=True,
                                     horizon_sufficient=True, seed=3)
        self.assertAlmostEqual(estudo["policy_delta"]["value"], 0.0)
        self.assertIsNone(estudo["verdict"]["winner"])
        self.assertFalse(estudo["verdict"]["promotable"])

    def test_sem_amostra_nao_ha_vencedor(self):
        estudo = wf.run_walk_forward(baseline=[], candidate=[], folds=self._folds(),
                                     bar_ms=BAR, costs_complete=True,
                                     horizon_sufficient=True, seed=3)
        self.assertEqual(estudo["folds_executed"], 0)
        self.assertIsNone(estudo["verdict"]["winner"])
        self.assertIn(wf.STUDIES_NOT_EXECUTED, estudo["verdict"]["reason_codes"])

    def test_purga_e_embargo_removem_a_fronteira(self):
        base, cand = self._serie(0.0, 0.2)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self._folds(),
                                     bar_ms=BAR, horizon_bars=10, embargo_bars=2,
                                     costs_complete=True, horizon_sufficient=True, seed=3)
        janela = next(item["window"] for item in estudo["folds"]
                      if item["reason_code"] == wf.OK)
        self.assertEqual(janela["purged_bars"], 10)
        self.assertEqual(janela["embargo_bars"], 2)
        self.assertLess(janela["train_end_ms"], janela["test_start_ms"])

    def test_resultado_futuro_nao_entra_na_dobra(self):
        """Linha posterior ao fim do teste não pode ser avaliada nele."""
        base, cand = self._serie(0.0, 0.2, n=30)
        futuro = linha("futuro", 99.0, 5_000)
        estudo = wf.run_walk_forward(baseline=base + [futuro], candidate=cand,
                                     folds=self._folds(30), bar_ms=BAR,
                                     costs_complete=True, horizon_sufficient=True, seed=3)
        chaves = {item["opportunity_id"] for item in estudo["paired"]["paired"]}
        self.assertNotIn("futuro", chaves)
        self.assertNotIn("futuro", estudo["paired"]["only_baseline"])

    def test_custo_desconhecido_impede_vencedor(self):
        base, cand = self._serie(0.0, 0.5)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self._folds(),
                                     bar_ms=BAR, costs_complete=False,
                                     horizon_sufficient=True, seed=3)
        self.assertIsNone(estudo["verdict"]["winner"])
        self.assertIn(wf.COSTS_UNKNOWN, estudo["verdict"]["reason_codes"])

    def test_estudo_nao_declara_equivalencia_com_live(self):
        base, cand = self._serie(0.0, 0.2)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self._folds(),
                                     bar_ms=BAR, costs_complete=True,
                                     horizon_sufficient=True, seed=3)
        self.assertFalse(estudo["live_equivalent"])
        self.assertFalse(estudo["promotable"])


class SelecaoGovernaAPoliticaAvaliada(unittest.TestCase):
    """4.2 — a escolha do treino decide QUAL política é executada no teste.

    Repro: seis dobras independentes (20 barras de treino, 5 de teste, passo
    25), baseline 0R e candidato −1R por barra no treino e +1R no teste. As
    seis seleções são FALSE; ainda assim o agregado declarava winner CANDIDATE
    com delta +30R e IC [5,5] — porque `selected` era só um rótulo.
    """

    TREINO, TESTE, PASSO, DOBRAS = 20, 5, 25, 6

    def serie(self, treino_r, teste_r):
        base, cand = [], []
        for dobra in range(self.DOBRAS):
            inicio = dobra * self.PASSO
            for i in range(self.TREINO):
                base.append(linha(f"d{dobra}-tr{i}", 0.0, inicio + i))
                cand.append(linha(f"d{dobra}-tr{i}", treino_r, inicio + i))
            for i in range(self.TESTE):
                pos = inicio + self.TREINO + i
                base.append(linha(f"d{dobra}-te{i}", 0.0, pos))
                cand.append(linha(f"d{dobra}-te{i}", teste_r, pos))
        return base, cand

    def folds(self):
        return [wf.Fold(index=d,
                        train_start_ms=T0 + d * self.PASSO * BAR,
                        train_end_ms=T0 + (d * self.PASSO + self.TREINO) * BAR,
                        test_start_ms=T0 + (d * self.PASSO + self.TREINO) * BAR,
                        test_end_ms=T0 + (d + 1) * self.PASSO * BAR)
                for d in range(self.DOBRAS)]

    def estudo(self, treino_r, teste_r):
        base, cand = self.serie(treino_r, teste_r)
        return wf.run_walk_forward(baseline=base, candidate=cand, folds=self.folds(),
                                   bar_ms=BAR, costs_complete=True,
                                   horizon_sufficient=True, seed=3)

    def test_treino_ruim_nao_vira_vencedor_pelo_teste_bom(self):
        estudo = self.estudo(-1.0, +1.0)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual(len(executadas), self.DOBRAS)
        self.assertEqual([item["candidate_selected_on_train"] for item in executadas],
                         [False] * self.DOBRAS)
        self.assertEqual([item["evaluated_policy"] for item in executadas],
                         [wf.POLICY_BASELINE_FALLBACK] * self.DOBRAS)
        self.assertEqual([item["delta_net_r"] for item in executadas], [0.0] * self.DOBRAS)
        self.assertEqual(estudo["policy_delta"]["value"], 0.0)
        self.assertEqual(estudo["folds_running_candidate"], 0)
        self.assertIsNone(estudo["verdict"]["winner"])

    def test_dobra_sem_selecao_nao_e_descartada(self):
        estudo = self.estudo(-1.0, +1.0)
        # As dobras continuam na evidência com delta ZERO — descartá-las
        # deixaria só o que favorece o candidato.
        self.assertEqual(estudo["folds_executed"], self.DOBRAS)
        self.assertTrue(all(item["test_n"] > 0 for item in estudo["folds"]))

    def test_treino_bom_executa_o_candidato(self):
        estudo = self.estudo(+1.0, +1.0)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual([item["evaluated_policy"] for item in executadas],
                         [wf.POLICY_CANDIDATE] * self.DOBRAS)
        self.assertEqual(estudo["folds_running_candidate"], self.DOBRAS)
        self.assertGreater(estudo["policy_delta"]["value"], 0.0)

    def test_treino_bom_e_teste_ruim_mede_o_teste_ruim(self):
        estudo = self.estudo(+1.0, -1.0)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual([item["evaluated_policy"] for item in executadas],
                         [wf.POLICY_CANDIDATE] * self.DOBRAS)
        self.assertLess(estudo["policy_delta"]["value"], 0.0)
        # Candidato pior fora da amostra: o vencedor é o BASELINE, nunca ele.
        self.assertEqual(estudo["verdict"]["winner"], "BASELINE")

    def test_selecao_no_treino_nao_e_vazamento(self):
        estudo = self.estudo(+1.0, +1.0)
        self.assertTrue(estudo["selection_governs_evaluation"])
        self.assertEqual(estudo["verdict"]["reason_codes"] and True, True)
        disciplina = wf.fold_discipline({stage: "train" for stage in wf.FITTED_STAGES}
                                        | {"candidate_selected_on": "train"})
        self.assertTrue(disciplina["ok"], disciplina)
        vazando = wf.fold_discipline({stage: "train" for stage in wf.FITTED_STAGES}
                                     | {"candidate_selected_on": "test"})
        self.assertIn(wf.TEST_LEAKAGE, vazando["reason_codes"])


class TreinoSoUsaLabelDisponivel(unittest.TestCase):
    """E — resultado ainda não conhecido não pode selecionar candidato.

    Repro: seis dobras 20/5, baseline 0R e candidato +1R, com TODOS os
    `result_available_ts_ms` em T0+1000BAR — depois de TODOS os cortes de
    treino. A seleção olhava só `decision_ts_ms` e dava seis seleções TRUE,
    EVIDENCE_AVAILABLE, winner=CANDIDATE, delta +30R e IC [5,5].
    """

    TREINO, TESTE, PASSO, DOBRAS = 20, 5, 25, 6
    TARDE = 1000

    def serie(self, *, disponivel_em=None, cand_r=1.0):
        base, cand = [], []
        for dobra in range(self.DOBRAS):
            inicio = dobra * self.PASSO
            for rotulo, quantos in (("tr", self.TREINO), ("te", self.TESTE)):
                for i in range(quantos):
                    pos = inicio + (0 if rotulo == "tr" else self.TREINO) + i
                    disponivel = (T0 + disponivel_em * BAR if disponivel_em is not None
                                  else T0 + (pos + 1) * BAR)
                    base.append({**linha(f"d{dobra}-{rotulo}{i}", 0.0, pos),
                                 "result_available_ts_ms": disponivel})
                    cand.append({**linha(f"d{dobra}-{rotulo}{i}", cand_r, pos),
                                 "result_available_ts_ms": disponivel})
        return base, cand

    def folds(self):
        return [wf.Fold(index=d,
                        train_start_ms=T0 + d * self.PASSO * BAR,
                        train_end_ms=T0 + (d * self.PASSO + self.TREINO) * BAR,
                        test_start_ms=T0 + (d * self.PASSO + self.TREINO) * BAR,
                        test_end_ms=T0 + (d + 1) * self.PASSO * BAR)
                for d in range(self.DOBRAS)]

    def estudo(self, **kw):
        base, cand = self.serie(**kw)
        return wf.run_walk_forward(baseline=base, candidate=cand, folds=self.folds(),
                                   bar_ms=BAR, costs_complete=True,
                                   horizon_sufficient=True, seed=3)

    def test_label_posterior_ao_corte_nao_seleciona(self):
        estudo = self.estudo(disponivel_em=self.TARDE)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual([item["candidate_selected_on_train"] for item in executadas],
                         [False] * self.DOBRAS)
        self.assertEqual([item["evaluated_policy"] for item in executadas],
                         [wf.POLICY_BASELINE_FALLBACK] * self.DOBRAS)
        self.assertIsNone(estudo["verdict"]["winner"])
        self.assertEqual(estudo["policy_delta"]["value"], 0.0)

    def test_dobra_registra_quantos_labels_ficaram_de_fora(self):
        estudo = self.estudo(disponivel_em=self.TARDE)
        executada = next(item for item in estudo["folds"] if item["reason_code"] == wf.OK)
        self.assertEqual(executada["train_labels_available"], 0)
        self.assertGreater(executada["train_labels_withheld"], 0)
        self.assertIn(wf.LABEL_NOT_AVAILABLE, executada["train_reason_codes"])

    def test_label_disponivel_no_corte_seleciona_normalmente(self):
        estudo = self.estudo()
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual([item["candidate_selected_on_train"] for item in executadas],
                         [True] * self.DOBRAS)
        self.assertEqual([item["evaluated_policy"] for item in executadas],
                         [wf.POLICY_CANDIDATE] * self.DOBRAS)
        self.assertGreater(estudo["policy_delta"]["value"], 0.0)

    def test_sem_timestamp_confiavel_e_insuficiencia_nao_selecao(self):
        base, cand = self.serie()
        for linha_sem in base + cand:
            linha_sem.pop("result_available_ts_ms", None)
        estudo = wf.run_walk_forward(baseline=base, candidate=cand, folds=self.folds(),
                                     bar_ms=BAR, costs_complete=True,
                                     horizon_sufficient=True, seed=3)
        executadas = [item for item in estudo["folds"] if item["reason_code"] == wf.OK]
        self.assertEqual([item["candidate_selected_on_train"] for item in executadas],
                         [False] * self.DOBRAS)
        self.assertIn(wf.LABEL_TIMESTAMP_MISSING, executadas[0]["train_reason_codes"])
        self.assertIsNone(estudo["verdict"]["winner"])

    def test_disciplina_declara_as_etapas_realmente_executadas(self):
        estudo = self.estudo()
        etapas = estudo["stages"]
        self.assertEqual(etapas["candidate_selection"], "EXECUTED_ON_TRAIN")
        for nome in wf.FITTED_STAGES:
            self.assertIn(etapas[nome], ("EXECUTED_ON_TRAIN", "NOT_APPLICABLE"))
        # Nada é declarado "treinado" sem ter rodado.
        self.assertNotIn("train", set(etapas.values()))


class SemRede(unittest.TestCase):
    def test_nenhuma_tentativa_de_rede(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
