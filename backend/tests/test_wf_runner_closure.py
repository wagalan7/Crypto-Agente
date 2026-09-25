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


def linha(key, net_r, passo):
    return {"opportunity_id": key, "net_r": net_r, "decision_ts_ms": T0 + passo * BAR}


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


class SemRede(unittest.TestCase):
    def test_nenhuma_tentativa_de_rede(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
