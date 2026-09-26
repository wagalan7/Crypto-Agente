"""R10 — execução simulada e capital: regressão dos defeitos confirmados.

Repros da revisão, todas com custos sintéticos zero:
  • ask 100 ou 100,4 produziam o MESMO `+2.325R` (o preço efetivo não chegava
    à trajetória nem à contabilidade);
  • latência de 300000ms gerava saída no instante T, antes da entrada efetiva
    em T+300000;
  • maker em 99,4, nunca tocado porque a mínima foi 99,5, aparecia preenchido
    e lucrativo;
  • capital 100 com risco 100% e dois stops sequenciais admitia os dois,
    perdendo 200 — o capital não recebia a primeira perda.

Limites conservadores usuais também são exercitados; o teste de estresse usa
fixture local, sem trocar limite real.
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
    raise RuntimeError("REDE BLOQUEADA no teste de execução simulada")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import offline_replay_service as r10a  # noqa: E402
from services import portfolio_replay_service as pf  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000
SEM_CUSTO = r10a.CostConfig(fee_bps_per_side=0.0, slippage_bps_per_side=0.0,
                            funding_bps_per_bar=0.0)


def candidato(key="a", *, entry=100.0, stop=98.0, tp1=103.0, tp2=106.0,
              decision_ms=T0 - 1, symbol="SYN"):
    return {"opportunity_id": key, "symbol": symbol, "direction": "long",
            "decision_ts_ms": decision_ms, "entry": entry, "stop_loss": stop,
            "tp1": tp1, "tp2": tp2, "atr": 1.0}


def barra(ts, o, h, l, c, volume=10.0):
    return {"timestamp_ms": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": volume}


def quote(key="a", *, bid=99.99, ask=100.0, ts_ms=T0 - 1):
    return {key: {"bid": bid, "ask": ask, "ts_ms": ts_ms, "source": "synthetic"}}


class PrecoEfetivoAlimentaAContabilidade(unittest.TestCase):
    """Trajetória resolvida: primeira barra neutra, depois alta até o alvo."""

    # A barra de entrada precisa NEGOCIAR o preço efetivo (o motor trata a
    # entrada como limite) sem tocar stop nem alvo: nada de ambiguidade.
    BARRAS = [barra(T0, 100.6, 100.8, 99.9, 100.2),
              barra(T0 + BAR, 100.2, 104.0, 100.1, 103.5),
              barra(T0 + 2 * BAR, 103.5, 107.0, 103.4, 106.5),
              barra(T0 + 3 * BAR, 106.5, 108.0, 106.0, 107.5)]

    def _rodar(self, ask):
        return pf.run_portfolio([candidato()], bars_by_id={"a": self.BARRAS},
                                quotes_by_id=quote(ask=ask, bid=ask - 0.01),
                                costs=SEM_CUSTO)

    def test_ask_diferente_muda_o_resultado(self):
        barato = self._rodar(100.0)["trades"][0]
        caro = self._rodar(100.4)["trades"][0]
        self.assertEqual(barato["admitted"], True)
        self.assertEqual(caro["admitted"], True)
        self.assertNotEqual(barato["net_r"], caro["net_r"])
        self.assertAlmostEqual(barato["entry_fill_price"], 100.0)
        self.assertAlmostEqual(caro["entry_fill_price"], 100.4)

    def test_entrada_mais_cara_reduz_o_r(self):
        barato = self._rodar(100.0)["trades"][0]
        caro = self._rodar(100.4)["trades"][0]
        self.assertIsNotNone(barato["net_r"])
        self.assertIsNotNone(caro["net_r"])
        self.assertLess(caro["net_r"], barato["net_r"])

    def test_quantidade_e_exposicao_usam_o_preco_simulado(self):
        caro = self._rodar(100.4)["trades"][0]
        risco_por_unidade = abs(caro["entry_fill_price"] - 98.0)
        self.assertAlmostEqual(caro["qty"], caro["risk_usd"] / risco_por_unidade)
        self.assertAlmostEqual(caro["exposure_usd"], caro["qty"] * caro["entry_fill_price"])


class NenhumaSaidaAntesDoFill(unittest.TestCase):
    def test_latencia_empurra_o_inicio_da_trajetoria(self):
        modelo = pf.ExecutionModel(send_latency_ms=300_000, max_quote_age_ms=600_000)
        # A janela de entrada só começa DEPOIS do instante efetivo (T0+300000).
        barras = [barra(T0, 100.6, 100.9, 100.5, 100.8),
                  barra(T0 + BAR, 100.6, 100.8, 99.9, 100.2),
                  barra(T0 + 2 * BAR, 100.2, 104.0, 100.1, 103.5),
                  barra(T0 + 3 * BAR, 103.5, 107.0, 103.4, 106.5),
                  barra(T0 + 4 * BAR, 106.5, 108.0, 106.0, 107.5)]
        resultado = pf.run_portfolio(
            [candidato(decision_ms=T0)], bars_by_id={"a": barras},
            quotes_by_id=quote(ts_ms=T0 + 300_000), model=modelo, costs=SEM_CUSTO)
        trade = resultado["trades"][0]
        self.assertTrue(trade["admitted"])
        self.assertEqual(trade["effective_ts_ms"], T0 + 300_000)
        self.assertIsNotNone(trade["exit_ts_ms"])
        self.assertGreaterEqual(trade["exit_ts_ms"], trade["effective_ts_ms"])

    def test_cotacao_anterior_ao_instante_efetivo_nao_abre(self):
        modelo = pf.ExecutionModel(send_latency_ms=300_000)
        resultado = pf.run_portfolio(
            [candidato(decision_ms=T0)], bars_by_id={"a": [barra(T0, 100.0, 107.0, 99.5, 106.0)]},
            quotes_by_id=quote(ts_ms=T0), model=modelo, costs=SEM_CUSTO)
        self.assertEqual(resultado["admitted"], 0)
        self.assertIn(pf.QUOTE_BEFORE_DECISION, resultado["rejected"])


class MakerSemToqueNaoAbre(unittest.TestCase):
    def test_limite_nunca_tocado_nao_vira_posicao(self):
        modelo = pf.ExecutionModel(maker_enabled=True, max_chase_atr=0.0)
        barras = [barra(T0, 100.0, 107.0, 99.5, 106.0)]
        # bid 99.4 = limite maker; a mínima da barra foi 99.5.
        resultado = pf.run_portfolio(
            [candidato(decision_ms=T0 - 1)], bars_by_id={"a": barras},
            quotes_by_id=quote(bid=99.4, ask=99.41), model=modelo, costs=SEM_CUSTO)
        self.assertEqual(resultado["admitted"], 0)
        self.assertIn(pf.MAKER_NOT_FILLED, resultado["rejected"])
        self.assertIsNone(resultado["metrics"]["net_total_r"])

    def test_limite_atravessado_abre_como_maker(self):
        modelo = pf.ExecutionModel(maker_enabled=True, max_chase_atr=0.0)
        barras = [barra(T0, 100.0, 107.0, 99.0, 106.0)]
        resultado = pf.run_portfolio(
            [candidato(decision_ms=T0 - 1)], bars_by_id={"a": barras},
            quotes_by_id=quote(bid=99.4, ask=99.41), model=modelo, costs=SEM_CUSTO)
        self.assertEqual(resultado["admitted"], 1)
        self.assertEqual(resultado["trades"][0]["entry_fill_type"], "MAKER")


class CapitalRecebeOResultado(unittest.TestCase):
    """Fixture local de estresse: risco 100% do capital, dois stops seguidos."""

    def _dois_stops(self, *, capital, risco_pct):
        queda = [barra(T0, 100.0, 100.5, 90.0, 91.0)]
        candidatos = [candidato("a", decision_ms=T0 - 2, symbol="AAA"),
                      candidato("b", decision_ms=T0 - 1, symbol="BBB")]
        return pf.run_portfolio(
            candidatos, bars_by_id={"a": queda, "b": queda},
            quotes_by_id={**quote("a"), **quote("b")},
            portfolio=pf.PortfolioConfig(capital_usd=capital, risk_per_trade_pct=risco_pct,
                                         max_concurrent=5, max_per_symbol=1,
                                         max_exposure_usd=10_000.0),
            costs=SEM_CUSTO)

    def test_primeira_perda_reduz_o_capital_da_segunda(self):
        resultado = self._dois_stops(capital=100.0, risco_pct=100.0)
        metricas = resultado["metrics"]
        self.assertEqual(metricas["capital_start_usd"], 100.0)
        self.assertGreaterEqual(metricas["capital_end_usd"], 0.0)
        self.assertGreater(metricas["capital_end_usd"], -100.0)
        # A carteira não perde mais do que tinha.
        self.assertGreaterEqual(metricas["realized_pnl_usd"], -100.0)

    def test_capital_zerado_nao_admite_nova_entrada(self):
        resultado = self._dois_stops(capital=100.0, risco_pct=100.0)
        admitidos = [linha for linha in resultado["trades"] if linha["admitted"]]
        self.assertEqual(len(admitidos), 1, str(resultado["rejected"]))
        self.assertIn(pf.NO_CAPITAL, resultado["rejected"])

    def test_limites_conservadores_usuais(self):
        resultado = self._dois_stops(capital=1000.0, risco_pct=1.0)
        admitidos = [linha for linha in resultado["trades"] if linha["admitted"]]
        self.assertEqual(len(admitidos), 2)
        self.assertLess(resultado["metrics"]["capital_end_usd"], 1000.0)
        self.assertGreater(resultado["metrics"]["capital_end_usd"], 970.0)

    def test_resultado_desconhecido_nao_libera_capital(self):
        """Sem custo completo o R é desconhecido: capital não é presumido."""
        queda = [barra(T0, 100.0, 100.5, 90.0, 91.0)]
        resultado = pf.run_portfolio(
            [candidato()], bars_by_id={"a": queda}, quotes_by_id=quote(),
            costs=r10a.CostConfig(fee_bps_per_side=4.0))
        self.assertIsNone(resultado["trades"][0]["net_r"])
        self.assertEqual(resultado["metrics"]["unknown_results"], 1)
        self.assertEqual(resultado["metrics"]["capital_end_usd"],
                         resultado["metrics"]["capital_start_usd"])
        self.assertEqual(resultado["metrics"]["realized_pnl_usd"], 0.0)


class CapitalSoLiquidaQuandoOResultadoOcorre(unittest.TestCase):
    """4.1 — o resultado só toca o capital no instante COMPROVADO da saída.

    Repro: capital 1000, risco 1%. `a` entra em T0-2 e perde 1R só em T0+600000;
    `b` entra em T0-1, antes da perda existir. Liquidando cedo, `b` recebia
    risco 9,90 e o final virava 980,10 — dinheiro do futuro financiando a
    entrada anterior.
    """

    PERDE = [barra(T0, 100.0, 100.2, 99.9, 100.0),
             barra(T0 + BAR, 100.0, 100.2, 99.9, 100.0),
             barra(T0 + 2 * BAR, 100.0, 100.2, 98.0, 98.5)]
    GANHA = [barra(T0, 100.0, 100.2, 99.9, 100.0),
             barra(T0 + BAR, 100.0, 100.2, 99.9, 100.0),
             barra(T0 + 2 * BAR, 100.0, 104.0, 99.9, 103.8),
             barra(T0 + 3 * BAR, 103.8, 107.0, 103.5, 106.5)]

    @staticmethod
    def cand(key, symbol, decision_ms):
        return candidato(key, stop=99.0, decision_ms=decision_ms, symbol=symbol)

    def rodar(self, candidatos, barras, *, capital=1000.0, risco=1.0, custos=SEM_CUSTO):
        cotacoes = {}
        for item in candidatos:
            cotacoes.update(quote(item["opportunity_id"], ts_ms=item["decision_ts_ms"]))
        return pf.run_portfolio(
            candidatos, bars_by_id=barras, quotes_by_id=cotacoes,
            portfolio=pf.PortfolioConfig(capital_usd=capital, risk_per_trade_pct=risco,
                                         max_concurrent=5, max_per_symbol=1,
                                         max_exposure_usd=100_000.0),
            replay_config=r10a.ReplayConfig(bar_ms=BAR), costs=custos)

    def linhas(self, resultado):
        return {row["opportunity_id"]: row for row in resultado["trades"]}

    def test_sobreposta_nao_financia_a_entrada_anterior(self):
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                               {"a": self.PERDE, "b": self.PERDE})
        linhas = self.linhas(resultado)
        self.assertEqual(linhas["b"]["capital_at_entry_usd"], 1000.0)
        self.assertEqual(linhas["b"]["risk_usd"], 10.0)      # não 9,90
        self.assertEqual(resultado["metrics"]["capital_end_usd"], 980.0)

    def test_ganho_futuro_tambem_nao_aumenta_a_proxima(self):
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                               {"a": self.GANHA, "b": self.PERDE})
        linhas = self.linhas(resultado)
        self.assertEqual(linhas["b"]["capital_at_entry_usd"], 1000.0)
        self.assertEqual(linhas["b"]["risk_usd"], 10.0)

    def test_sequenciais_usam_o_capital_ja_liquidado(self):
        tarde = [barra(T0 + 3 * BAR, 100.0, 100.2, 99.9, 100.0),
                 barra(T0 + 4 * BAR, 100.0, 100.2, 98.0, 98.5)]
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2),
                                self.cand("b", "BBB", T0 + 3 * BAR - 1)],
                               {"a": self.PERDE, "b": tarde})
        linhas = self.linhas(resultado)
        # `a` sai em T0+2BAR, antes da decisão de `b`: o capital dela já caiu.
        self.assertEqual(linhas["a"]["capital_after_usd"], 990.0)
        self.assertEqual(linhas["b"]["capital_at_entry_usd"], 990.0)
        self.assertAlmostEqual(linhas["b"]["risk_usd"], 9.9, places=9)
        self.assertAlmostEqual(resultado["metrics"]["capital_end_usd"], 980.1, places=9)

    def test_saida_empatada_com_entrada_ja_liquidou(self):
        # `b` decide EXATAMENTE no instante da saída de `a`: o resultado de `a`
        # já ocorreu e vale; o de `b` ainda não.
        saida_a = T0 + 2 * BAR
        tarde = [barra(saida_a, 100.0, 100.2, 99.9, 100.0),
                 barra(saida_a + BAR, 100.0, 100.2, 98.0, 98.5)]
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", saida_a)],
                               {"a": self.PERDE, "b": tarde})
        linhas = self.linhas(resultado)
        self.assertEqual(linhas["b"]["capital_at_entry_usd"], 990.0)
        self.assertAlmostEqual(linhas["b"]["risk_usd"], 9.9, places=9)

    def test_ordem_de_execucao_segue_o_instante_efetivo(self):
        # Decisões em ordem inversa à da lista: a ordenação é temporal, não de
        # inserção — o mesmo conjunto dá o mesmo resultado em qualquer ordem.
        direta = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                            {"a": self.PERDE, "b": self.PERDE})
        invertida = self.rodar([self.cand("b", "BBB", T0 - 1), self.cand("a", "AAA", T0 - 2)],
                               {"a": self.PERDE, "b": self.PERDE})
        self.assertEqual([row["opportunity_id"] for row in direta["trades"]],
                         [row["opportunity_id"] for row in invertida["trades"]])
        self.assertEqual(direta["metrics"]["capital_end_usd"],
                         invertida["metrics"]["capital_end_usd"])

    def test_barra_posterior_nao_muda_decisao_anterior(self):
        outro_final = self.PERDE[:2] + [barra(T0 + 2 * BAR, 100.0, 109.0, 99.95, 108.0)]
        base = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                          {"a": self.PERDE, "b": self.PERDE})
        mudado = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                            {"a": outro_final, "b": self.PERDE})
        for chave in ("risk_usd", "qty", "exposure_usd", "capital_at_entry_usd", "admitted"):
            self.assertEqual(self.linhas(base)["b"][chave], self.linhas(mudado)["b"][chave], chave)

    def test_resultado_desconhecido_nao_libera_capacidade(self):
        incompleto = r10a.CostConfig(fee_bps_per_side=4.0)
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2)], {"a": self.PERDE},
                               custos=incompleto)
        linha = self.linhas(resultado)["a"]
        self.assertIsNone(linha["net_r"])
        self.assertEqual(resultado["metrics"]["unknown_results"], 1)
        self.assertEqual(resultado["metrics"]["capital_end_usd"], 1000.0)
        self.assertEqual(resultado["metrics"]["realized_pnl_usd"], 0.0)

    def test_caixa_concilia(self):
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2), self.cand("b", "BBB", T0 - 1)],
                               {"a": self.GANHA, "b": self.PERDE})
        metricas = resultado["metrics"]
        realizados = [row for row in resultado["trades"]
                      if row["admitted"] and row["net_r"] is not None]
        soma = sum(row["net_r"] * row["risk_usd"] for row in realizados)
        self.assertAlmostEqual(metricas["realized_pnl_usd"], soma, places=9)
        self.assertAlmostEqual(metricas["capital_end_usd"],
                               metricas["capital_start_usd"] + metricas["realized_pnl_usd"],
                               places=9)
        # Cada trade declara o capital no instante da própria saída.
        self.assertEqual([row["capital_after_usd"] is not None for row in realizados],
                         [True] * len(realizados))

    def test_custos_entram_uma_vez_so(self):
        com_custo = r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                                    funding_bps_per_bar=1.0)
        resultado = self.rodar([self.cand("a", "AAA", T0 - 2)], {"a": self.PERDE},
                               custos=com_custo)
        linha = self.linhas(resultado)["a"]
        bruto = linha["gross_r"] - (linha["fee_r"] + linha["slippage_r"] + linha["funding_r"])
        self.assertAlmostEqual(linha["net_r"], bruto, places=9)
        self.assertAlmostEqual(resultado["metrics"]["realized_pnl_usd"],
                               linha["net_r"] * linha["risk_usd"], places=9)


class SemRede(unittest.TestCase):
    def test_nenhuma_tentativa_de_rede(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
