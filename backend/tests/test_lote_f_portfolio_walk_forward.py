"""Bloco F — replay de carteira compartilhada e validação walk-forward.

Hermético e sintético: adaptadores determinísticos, carteira compartilhada,
custos separados, matriz de fidelidade, janelas rolantes com purga/embargo e a
fronteira do holdout testada com holdout SINTÉTICO.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import offline_replay_service as r10a  # noqa: E402
from services import portfolio_replay_service as pf  # noqa: E402
from services import walk_forward_service as wf  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000
COSTS = r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                        funding_bps_per_bar=1.0)


def bars(count=30, *, base=100.0, drift=0.4, start_ms=T0):
    return [{"timestamp_ms": start_ms + i * BAR, "open": base + i * drift,
             "high": base + 1.0 + i * drift, "low": base - 0.5 + i * drift,
             "close": base + 0.5 + i * drift, "volume": 10.0} for i in range(count)]


def candidate(key="a", *, symbol="SYN", decision_ms=T0 - 1, side="long"):
    return {"opportunity_id": key, "symbol": symbol, "direction": side,
            "decision_ts_ms": decision_ms, "entry": 100.0, "stop_loss": 98.0,
            "tp1": 103.0, "tp2": 106.0, "atr": 1.0}


def quote(key="a", *, ts_ms=T0 - 1, bid=99.99, ask=100.02):
    return {key: {"bid": bid, "ask": ask, "ts_ms": ts_ms, "source": "binance"}}


class UniversoPontoNoTempo(unittest.TestCase):
    def test_sem_snapshot_anterior_fica_desconhecido(self):
        universo = pf.PointInTimeUniverse((pf.UniverseSnapshot(T0, ("SYN",)),))
        self.assertEqual(universo.at(T0 - 1)["reason_code"], pf.UNIVERSE_UNKNOWN)
        self.assertEqual(universo.at(T0 - 1)["symbols"], ())

    def test_universo_de_hoje_nao_substitui_o_de_ontem(self):
        universo = pf.PointInTimeUniverse((
            pf.UniverseSnapshot(T0, ("SYN",)),
            pf.UniverseSnapshot(T0 + 10 * BAR, ("SYN", "NOVA"))))
        self.assertFalse(universo.admits("NOVA", T0 + BAR)["admitted"])
        self.assertEqual(universo.admits("NOVA", T0 + BAR)["reason_code"],
                         pf.SYMBOL_OUT_OF_UNIVERSE)
        self.assertTrue(universo.admits("NOVA", T0 + 10 * BAR)["admitted"])


class PrecoExecutavel(unittest.TestCase):
    def test_cotacao_ausente_ou_anterior(self):
        modelo = pf.ExecutionModel(scan_latency_ms=1_000, send_latency_ms=500)
        self.assertEqual(pf.executable_price(None, side="long", decision_ts_ms=T0,
                                             planned_entry=100.0, atr=1.0,
                                             model=modelo)["reason_code"],
                         pf.QUOTE_UNAVAILABLE)
        antiga = {"bid": 100.0, "ask": 100.01, "ts_ms": T0 + 100}
        self.assertEqual(pf.executable_price(antiga, side="long", decision_ts_ms=T0,
                                             planned_entry=100.0, atr=1.0,
                                             model=modelo)["reason_code"],
                         pf.QUOTE_BEFORE_DECISION)

    def test_latencia_entra_no_instante_executavel(self):
        modelo = pf.ExecutionModel(scan_latency_ms=1_000, send_latency_ms=500)
        verdict = pf.executable_price({"bid": 100.0, "ask": 100.02, "ts_ms": T0 + 1_500},
                                      side="long", decision_ts_ms=T0, planned_entry=100.0,
                                      atr=1.0, model=modelo)
        self.assertEqual(verdict["reason_code"], pf.OK)
        self.assertEqual(verdict["effective_ts_ms"], T0 + 1_500)
        self.assertEqual(verdict["price"], 100.02)  # taker paga o ask

    def test_cotacao_velha_demais(self):
        modelo = pf.ExecutionModel(max_quote_age_ms=1_000)
        verdict = pf.executable_price({"bid": 100.0, "ask": 100.02, "ts_ms": T0 + 5_000},
                                      side="long", decision_ts_ms=T0, planned_entry=100.0,
                                      atr=1.0, model=modelo)
        self.assertEqual(verdict["reason_code"], pf.QUOTE_STALE)

    def test_gate_revalidado_bloqueia_spread_e_chase(self):
        modelo = pf.ExecutionModel()
        largo = pf.executable_price({"bid": 100.0, "ask": 101.0, "ts_ms": T0},
                                    side="long", decision_ts_ms=T0, planned_entry=100.0,
                                    atr=1.0, model=modelo)
        self.assertEqual(largo["reason_code"], pf.SPREAD_TOO_WIDE)
        longe = pf.executable_price({"bid": 100.99, "ask": 101.0, "ts_ms": T0},
                                    side="long", decision_ts_ms=T0, planned_entry=100.0,
                                    atr=1.0, model=modelo)
        self.assertEqual(longe["reason_code"], pf.CHASE_TOO_FAR)

    def test_atr_ausente_nao_vira_zero(self):
        verdict = pf.executable_price({"bid": 100.0, "ask": 100.02, "ts_ms": T0},
                                      side="long", decision_ts_ms=T0, planned_entry=100.0,
                                      atr=None, model=pf.ExecutionModel())
        self.assertEqual(verdict["reason_code"], pf.ATR_UNKNOWN)


class RecursosDesligados(unittest.TestCase):
    def test_default_nao_liga_maker_parcial_nem_fallback(self):
        modelo = pf.ExecutionModel()
        self.assertFalse(modelo.maker_enabled)
        self.assertFalse(modelo.allow_partial_fill)
        self.assertFalse(modelo.allow_cancel_fallback)
        self.assertEqual(pf.maker_outcome(limit_price=100.0, side="long",
                                          bar={"low": 101.0, "high": 102.0},
                                          model=modelo)["fill_type"], "TAKER")

    def test_maker_nao_preenchido(self):
        modelo = pf.ExecutionModel(maker_enabled=True)
        verdict = pf.maker_outcome(limit_price=99.0, side="long",
                                   bar={"low": 99.5, "high": 101.0}, model=modelo)
        self.assertFalse(verdict["filled"])
        self.assertEqual(verdict["reason_code"], pf.MAKER_NOT_FILLED)

    def test_toque_sem_parcial_habilitado_nao_enche(self):
        modelo = pf.ExecutionModel(maker_enabled=True)
        verdict = pf.maker_outcome(limit_price=99.5, side="long",
                                   bar={"low": 99.5, "high": 101.0}, model=modelo)
        self.assertEqual(verdict["reason_code"], pf.PARTIAL_NOT_ALLOWED)
        permitido = pf.maker_outcome(limit_price=99.5, side="long",
                                     bar={"low": 99.5, "high": 101.0},
                                     model=pf.ExecutionModel(maker_enabled=True,
                                                             allow_partial_fill=True))
        self.assertTrue(permitido["filled"])
        self.assertEqual(permitido["fraction"], 0.5)

    def test_fallback_so_quando_configurado(self):
        modelo = pf.ExecutionModel(maker_enabled=True, allow_cancel_fallback=True)
        verdict = pf.maker_outcome(limit_price=99.0, side="long",
                                   bar={"low": 99.5, "high": 101.0}, model=modelo)
        self.assertTrue(verdict["filled"])
        self.assertEqual(verdict["fill_type"], "TAKER_FALLBACK")


class RegraConservadora(unittest.TestCase):
    def test_stop_e_alvo_na_mesma_barra(self):
        verdict = pf.same_bar_rule(side="long", bar={"open": 100.0, "low": 97.0,
                                                     "high": 104.0}, stop=98.0,
                                   target=103.0)
        self.assertEqual(verdict["resolution"], "STOP")
        self.assertEqual(verdict["reason_code"], pf.SAME_BAR_CONSERVATIVE_STOP)

    def test_gap_preenche_na_abertura(self):
        verdict = pf.same_bar_rule(side="long", bar={"open": 96.0, "low": 95.0,
                                                     "high": 99.0}, stop=98.0,
                                   target=103.0)
        self.assertEqual(verdict["fill_price"], 96.0)
        self.assertEqual(verdict["reason_code"], pf.GAP_FILL_AT_OPEN)

    def test_barra_incompleta_nao_resolve(self):
        verdict = pf.same_bar_rule(side="long", bar={"open": 100.0, "low": 97.0,
                                                     "high": 104.0, "closed": False},
                                   stop=98.0, target=103.0)
        self.assertIsNone(verdict["resolution"])
        self.assertEqual(verdict["reason_code"], pf.BAR_INCOMPLETE)


class CarteiraCompartilhada(unittest.TestCase):
    def test_ordenacao_deterministica(self):
        entrada = [candidate("b", decision_ms=T0), candidate("a", decision_ms=T0),
                   candidate("c", decision_ms=T0 - BAR)]
        ordem = [row["opportunity_id"] for row in pf.order_candidates(entrada)]
        self.assertEqual(ordem, ["c", "a", "b"])
        self.assertEqual(ordem, [row["opportunity_id"] for row in
                                 pf.order_candidates(list(reversed(entrada)))])

    def test_slots_simultaneidade_e_simbolo(self):
        estado = pf.PortfolioState(config=pf.PortfolioConfig(max_concurrent=2,
                                                             max_per_symbol=1))
        primeiro = estado.admit(symbol="SYN", side="long", decision_ts_ms=T0,
                                exposure_usd=100.0, exit_ts_ms=T0 + 10 * BAR)
        self.assertTrue(primeiro["admitted"])
        mesmo = estado.admit(symbol="SYN", side="long", decision_ts_ms=T0 + BAR,
                             exposure_usd=100.0, exit_ts_ms=T0 + 10 * BAR)
        self.assertEqual(mesmo["reason_code"], pf.SYMBOL_SLOT_TAKEN)
        segundo = estado.admit(symbol="OUT", side="long", decision_ts_ms=T0 + BAR,
                               exposure_usd=100.0, exit_ts_ms=T0 + 10 * BAR)
        self.assertTrue(segundo["admitted"])
        terceiro = estado.admit(symbol="TER", side="long", decision_ts_ms=T0 + 2 * BAR,
                                exposure_usd=100.0, exit_ts_ms=T0 + 10 * BAR)
        self.assertEqual(terceiro["reason_code"], pf.NO_SLOT)

    def test_posicao_encerrada_libera_slot(self):
        estado = pf.PortfolioState(config=pf.PortfolioConfig(max_concurrent=1))
        estado.admit(symbol="SYN", side="long", decision_ts_ms=T0, exposure_usd=100.0,
                     exit_ts_ms=T0 + 2 * BAR)
        depois = estado.admit(symbol="OUT", side="long", decision_ts_ms=T0 + 3 * BAR,
                              exposure_usd=100.0, exit_ts_ms=None)
        self.assertTrue(depois["admitted"])

    def test_capital_reserva_e_exposicao(self):
        estado = pf.PortfolioState(config=pf.PortfolioConfig(
            capital_usd=100.0, risk_per_trade_pct=60.0, reserve_usd=50.0,
            max_concurrent=5, max_exposure_usd=10_000.0))
        self.assertEqual(estado.admit(symbol="SYN", side="long", decision_ts_ms=T0,
                                      exposure_usd=1.0, exit_ts_ms=None)["reason_code"],
                         pf.NO_CAPITAL)
        limitado = pf.PortfolioState(config=pf.PortfolioConfig(max_exposure_usd=50.0))
        self.assertEqual(limitado.admit(symbol="SYN", side="long", decision_ts_ms=T0,
                                        exposure_usd=100.0,
                                        exit_ts_ms=None)["reason_code"],
                         pf.EXPOSURE_LIMIT)

    def test_trades_impossiveis_nao_entram_na_soma(self):
        candidatos = [candidate("a", symbol="SYN", decision_ms=T0 - 1),
                      candidate("b", symbol="SYN", decision_ms=T0)]
        resultado = pf.run_portfolio(
            candidatos, bars_by_id={"a": bars(), "b": bars()},
            quotes_by_id={**quote("a"), **quote("b", ts_ms=T0)},
            portfolio=pf.PortfolioConfig(max_per_symbol=1), costs=COSTS)
        self.assertEqual(resultado["admitted"], 1)
        self.assertEqual(resultado["rejected"][pf.SYMBOL_SLOT_TAKEN], 1)
        self.assertEqual(resultado["metrics"]["resolved_n"], 1)


class CustosEFidelidade(unittest.TestCase):
    def test_custo_ausente_deixa_economia_indisponivel(self):
        resultado = pf.run_portfolio([candidate()], bars_by_id={"a": bars()},
                                     quotes_by_id=quote(),
                                     costs=r10a.CostConfig(fee_bps_per_side=4.0))
        self.assertEqual(resultado["costs"]["status"], "UNKNOWN")
        self.assertIn("slippage_bps_per_side", resultado["costs"]["missing"])
        self.assertEqual(resultado["metrics"]["economics"], pf.ECONOMICS_UNAVAILABLE)
        self.assertIsNone(resultado["trades"][0]["net_r"])
        self.assertEqual(resultado["economics_unavailable"], 1)

    def test_custos_separados_por_componente(self):
        resultado = pf.run_portfolio([candidate()], bars_by_id={"a": bars()},
                                     quotes_by_id=quote(), costs=COSTS)
        trade = resultado["trades"][0]
        for chave in ("fee_r", "slippage_r", "funding_r"):
            self.assertIsNotNone(trade[chave], chave)
        self.assertEqual(resultado["costs"]["observed_account_costs"], False)

    def test_matriz_de_fidelidade_por_dimensao(self):
        resultado = pf.run_portfolio([candidate()], bars_by_id={"a": bars()},
                                     quotes_by_id=quote(), costs=COSTS)
        matriz = resultado["fidelity"]["dimensions"]
        self.assertEqual(set(matriz), set(pf.FIDELITY_DIMENSIONS))
        self.assertEqual(matriz["queue_position"], pf.FIDELITY_UNAVAILABLE)
        self.assertEqual(matriz["price_path"], pf.FIDELITY_MODELED)
        self.assertEqual(matriz["point_in_time_universe"], pf.FIDELITY_UNAVAILABLE)
        self.assertFalse(resultado["fidelity"]["live_equivalent"])
        self.assertFalse(resultado["live_equivalent"])
        self.assertFalse(resultado["promotable"])

    def test_universo_declarado_vira_comprovado(self):
        universo = pf.PointInTimeUniverse((pf.UniverseSnapshot(T0 - 10 * BAR, ("SYN",)),))
        resultado = pf.run_portfolio([candidate()], bars_by_id={"a": bars()},
                                     quotes_by_id=quote(), universe=universo, costs=COSTS)
        self.assertEqual(resultado["fidelity"]["dimensions"]["point_in_time_universe"],
                         pf.FIDELITY_PROVEN)

    def test_motor_r10a_reaproveitado(self):
        manifest = pf.portfolio_manifest()
        self.assertFalse(manifest["reuses"]["second_backtest_system"])
        self.assertIn("R10A", manifest["reuses"]["trade_path_engine"])
        resultado = pf.run_portfolio([candidate()], bars_by_id={"a": bars()},
                                     quotes_by_id=quote(), costs=COSTS)
        self.assertEqual(resultado["engine"]["trade_path"], r10a.SCHEMA_VERSION)
        self.assertTrue(resultado["engine"]["reused_r10a"])

    def test_replay_nao_importa_servico_com_efeito_externo(self):
        arvore = ast.parse((BACKEND / "services" / "portfolio_replay_service.py").read_text())
        importados = set()
        for node in ast.walk(arvore):
            if isinstance(node, ast.Import):
                importados.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "services":
                    importados.update(f"services.{alias.name}" for alias in node.names)
                else:
                    importados.add(node.module.split(".")[0])
        self.assertEqual(importados - {"__future__"},
                         {"dataclasses", "hashlib", "json", "math", "os", "typing",
                          "services.offline_replay_service"})


class JanelasRolantes(unittest.TestCase):
    def test_walk_forward_exige_varias_dobras(self):
        poucas = wf.rolling_windows(start_ms=T0, end_ms=T0 + 60 * BAR, bar_ms=BAR,
                                    train_bars=40, test_bars=10)
        self.assertEqual(poucas["reason_code"], wf.INSUFFICIENT_WINDOWS)
        self.assertFalse(poucas["walk_forward"])
        muitas = wf.rolling_windows(start_ms=T0, end_ms=T0 + 200 * BAR, bar_ms=BAR,
                                    train_bars=40, test_bars=10)
        self.assertTrue(muitas["walk_forward"])
        self.assertGreaterEqual(len(muitas["folds"]), wf.MIN_FOLDS)

    def test_metade_recente_nao_e_walk_forward(self):
        unica = (wf.Fold(0, T0, T0 + 50 * BAR, T0 + 50 * BAR, T0 + 100 * BAR),)
        self.assertFalse(wf.is_walk_forward(unica)["walk_forward"])
        self.assertEqual(wf.is_walk_forward(unica)["reason_code"],
                         wf.INSUFFICIENT_WINDOWS)

    def test_dobras_avancam_e_nao_se_sobrepoem(self):
        folds = wf.rolling_windows(start_ms=T0, end_ms=T0 + 200 * BAR, bar_ms=BAR,
                                   train_bars=40, test_bars=10)["folds"]
        self.assertTrue(wf.is_walk_forward(folds)["walk_forward"])
        embaralhadas = (folds[1], folds[0], folds[2])
        self.assertFalse(wf.is_walk_forward(embaralhadas)["walk_forward"])

    def test_purga_e_embargo(self):
        fold = wf.Fold(0, T0, T0 + 40 * BAR, T0 + 40 * BAR, T0 + 50 * BAR)
        ajustado = wf.apply_purge_embargo(fold, horizon_bars=26, bar_ms=BAR,
                                          embargo_bars=2)
        self.assertEqual(ajustado["train_end_ms"], T0 + 14 * BAR)
        self.assertEqual(ajustado["test_start_ms"], T0 + 42 * BAR)
        self.assertTrue(ajustado["usable"])
        vazio = wf.apply_purge_embargo(fold, horizon_bars=40, bar_ms=BAR)
        self.assertFalse(vazio["usable"])
        self.assertEqual(vazio["reason_code"], wf.FOLD_EMPTY_AFTER_PURGE)


class DisciplinaDeDobra(unittest.TestCase):
    def test_ajuste_so_no_treino(self):
        bom = {stage: "train" for stage in wf.FITTED_STAGES}
        bom["candidate_selected_on"] = "validation"
        self.assertTrue(wf.fold_discipline(bom)["ok"])
        ruim = dict(bom, calibration="full_sample")
        self.assertEqual(wf.fold_discipline(ruim)["reason_codes"], (wf.FIT_OUTSIDE_TRAIN,))

    def test_teste_final_nao_escolhe_candidato(self):
        record = {stage: "train" for stage in wf.FITTED_STAGES}
        record["candidate_selected_on"] = "test"
        self.assertEqual(wf.fold_discipline(record)["reason_codes"], (wf.TEST_LEAKAGE,))
        record["candidate_selected_on"] = "validation"
        record["test_used_for_selection"] = True
        self.assertEqual(wf.fold_discipline(record)["reason_codes"], (wf.TEST_LEAKAGE,))
        self.assertEqual(wf.fold_discipline(record)["final_test_role"], "REPORT_ONLY")


class PareamentoECobertura(unittest.TestCase):
    def test_aceita_por_um_lado_entra_no_delta(self):
        base = [{"opportunity_id": "a", "net_r": 1.0}, {"opportunity_id": "b", "net_r": -1.0}]
        cand = [{"opportunity_id": "a", "net_r": 1.5}, {"opportunity_id": "c", "net_r": 2.0}]
        pareado = wf.pair_opportunities(base, cand)
        self.assertEqual([p["opportunity_id"] for p in pareado["paired"]], ["a"])
        self.assertEqual(pareado["only_baseline"], ["b"])
        self.assertEqual(pareado["only_candidate"], ["c"])
        self.assertEqual(pareado["policy_delta"]["removed_n"], 1)
        self.assertEqual(pareado["policy_delta"]["added_net_r"], 2.0)
        self.assertGreater(pareado["turnover_pct"], 0)
        self.assertTrue(pareado["paired_subsample_is_not_the_policy"])

    def test_desconhecido_no_delta_nao_vira_zero(self):
        pareado = wf.pair_opportunities([], [{"opportunity_id": "c", "net_r": None}])
        self.assertEqual(pareado["policy_delta"]["added_unknown"], 1)
        self.assertEqual(pareado["policy_delta"]["added_net_r"], 0.0)

    def test_exclusao_seletiva_reprova(self):
        igual = wf.coverage_guard({"considered": 100, "resolved": 90},
                                  {"considered": 100, "resolved": 88})
        self.assertTrue(igual["ok"])
        desigual = wf.coverage_guard({"considered": 100, "resolved": 95},
                                     {"considered": 100, "resolved": 75})
        self.assertEqual(desigual["reason_code"], wf.SELECTIVE_EXCLUSION)
        baixa = wf.coverage_guard({"considered": 100, "resolved": 50},
                                  {"considered": 100, "resolved": 50})
        self.assertEqual(baixa["reason_code"], wf.COVERAGE_INSUFFICIENT)


class Estatistica(unittest.TestCase):
    def test_multiplicidade_aperta_o_nivel(self):
        um = wf.multiplicity_adjusted_alpha(comparisons=1)
        cinco = wf.multiplicity_adjusted_alpha(comparisons=5)
        self.assertEqual(um["adjusted_alpha"], 0.05)
        self.assertAlmostEqual(cinco["adjusted_alpha"], 0.01)

    def test_bootstrap_por_blocos_e_deterministico(self):
        valores = [0.5, -0.2, 1.0, 0.3, -0.1, 0.8, 0.2, -0.4, 0.6, 0.1]
        a = wf.block_bootstrap_ci(valores, seed=7, samples=200, block_size=3)
        b = wf.block_bootstrap_ci(valores, seed=7, samples=200, block_size=3)
        self.assertEqual(a, b)
        self.assertTrue(a["available"])
        self.assertLessEqual(a["low"], a["mean"])
        self.assertGreaterEqual(a["high"], a["mean"])

    def test_amostra_pequena_nao_gera_intervalo(self):
        verdict = wf.block_bootstrap_ci([0.1], seed=1, block_size=5)
        self.assertFalse(verdict["available"])
        self.assertEqual(verdict["reason_code"], wf.SAMPLE_INSUFFICIENT)

    def test_metricas_por_corte(self):
        linhas = [{"window": "f0", "playbook": "TREND_PULLBACK", "net_r": 1.0,
                   "fee_r": 0.02, "slippage_r": 0.01, "funding_r": 0.0},
                  {"window": "f0", "playbook": "TREND_PULLBACK", "net_r": -1.0},
                  {"window": "f1", "playbook": "RANGE_REVERSION", "net_r": None}]
        por_janela = wf.slice_metrics(linhas, by="window")
        self.assertEqual(por_janela["f0"]["ops"], 2)
        self.assertEqual(por_janela["f0"]["profit_factor"], 1.0)
        self.assertEqual(por_janela["f1"]["unknown"], 1)
        self.assertIsNone(por_janela["f1"]["net_total_r"])
        self.assertAlmostEqual(por_janela["f0"]["costs_r"], 0.03)
        with self.assertRaises(ValueError):
            wf.slice_metrics(linhas, by="qualquer")


class VereditoEHoldout(unittest.TestCase):
    def _folds(self):
        return wf.rolling_windows(start_ms=T0, end_ms=T0 + 200 * BAR, bar_ms=BAR,
                                  train_bars=40, test_bars=10)["folds"]

    def _base(self, **over):
        disciplina = {stage: "train" for stage in wf.FITTED_STAGES}
        disciplina["candidate_selected_on"] = "validation"
        payload = dict(
            folds=self._folds(), discipline=wf.fold_discipline(disciplina),
            coverage=wf.coverage_guard({"considered": 100, "resolved": 90},
                                       {"considered": 100, "resolved": 89}),
            costs_complete=True, horizon_sufficient=True,
            paired=wf.pair_opportunities([], []),
            ci={"available": True, "low": 0.1, "high": 0.4})
        payload.update(over)
        return payload

    def test_custo_desconhecido_nao_tem_vencedor(self):
        verdict = wf.verdict(**self._base(costs_complete=False))
        self.assertIsNone(verdict["winner"])
        self.assertEqual(verdict["state"], wf.INSUFFICIENT_EVIDENCE)
        self.assertIn(wf.COSTS_UNKNOWN, verdict["reason_codes"])

    def test_horizonte_ou_cobertura_insuficiente(self):
        self.assertIn(wf.HORIZON_INSUFFICIENT,
                      wf.verdict(**self._base(horizon_sufficient=False))["reason_codes"])
        ruim = wf.coverage_guard({"considered": 100, "resolved": 95},
                                 {"considered": 100, "resolved": 70})
        self.assertIn(wf.SELECTIVE_EXCLUSION,
                      wf.verdict(**self._base(coverage=ruim))["reason_codes"])

    def test_intervalo_contendo_zero_nao_decide(self):
        verdict = wf.verdict(**self._base(ci={"available": True, "low": -0.2, "high": 0.4}))
        self.assertIsNone(verdict["winner"])
        self.assertEqual(verdict["reason_codes"], (wf.CI_INCLUDES_ZERO,))

    def test_evidencia_disponivel_nao_promove(self):
        verdict = wf.verdict(**self._base())
        self.assertEqual(verdict["winner"], "CANDIDATE")
        self.assertEqual(verdict["state"], "EVIDENCE_AVAILABLE")
        self.assertFalse(verdict["promotable"])

    def test_holdout_real_continua_selado(self):
        selo = wf.HoldoutSeal(synthetic=False, preregistration_ms=T0,
                              results_available_at_ms=T0 + BAR)
        verdict = wf.open_holdout(selo, now_ms=T0 + 10 * BAR)
        self.assertFalse(verdict["allowed"])
        self.assertFalse(verdict["loaded"])
        self.assertFalse(verdict["evaluated"])
        self.assertEqual(verdict["reason_code"], wf.REAL_HOLDOUT_SEALED)

    def test_holdout_sintetico_com_pre_registro_valido(self):
        selo = wf.HoldoutSeal(synthetic=True, preregistration_ms=T0,
                              results_available_at_ms=T0 + BAR,
                              preregistration_hash="abc")
        verdict = wf.open_holdout(selo, now_ms=T0 + 2 * BAR)
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["scope"], "SYNTHETIC_HOLDOUT_ONLY")
        self.assertFalse(verdict["hash_proves_preregistration"])
        self.assertFalse(verdict["results_usable_for_tuning"])

    def test_pre_registro_retrodatado_recusado(self):
        selo = wf.HoldoutSeal(synthetic=True, preregistration_ms=T0 + 2 * BAR,
                              results_available_at_ms=T0)
        self.assertEqual(wf.open_holdout(selo, now_ms=T0 + 3 * BAR)["reason_code"],
                         wf.BACKDATED_PREREGISTRATION)

    def test_fronteira_da_avaliacao_final(self):
        fronteira = wf.final_evaluation_boundary()
        self.assertEqual(fronteira["real_holdout"], "SEALED")
        self.assertFalse(fronteira["real_holdout_loaded"])
        self.assertFalse(fronteira["real_holdout_evaluated"])
        self.assertFalse(fronteira["hash_proves_preregistration"])
        self.assertFalse(fronteira["results_usable_for_parameter_choice"])

    def test_modos_inativos_por_padrao(self):
        for modulo in (pf, wf):
            anterior = os.environ.pop(modulo.MODE_ENV, None)
            try:
                self.assertEqual(modulo.selected_mode(), modulo.MODE_INACTIVE)
            finally:
                if anterior is not None:
                    os.environ[modulo.MODE_ENV] = anterior


if __name__ == "__main__":
    unittest.main()
