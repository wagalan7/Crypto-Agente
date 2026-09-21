"""Bloco H — integração sintética ponta a ponta e defaults preservados.

Fluxo com as funções REAIS (nada reimplementado no teste):
candidato → seleção → evidência → simulação de carteira → comparação →
go/no-go. E a checagem de que, com as versões novas desligadas, o legado
continua valendo — só a correção de segurança do P03 muda comportamento.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import offline_replay_service as r10a  # noqa: E402
from services import portfolio_replay_service as pf  # noqa: E402
from services import preselection_experiment_service as r12  # noqa: E402
from services import preselection_observation_service as pre  # noqa: E402
from services import research_batch_service as batch  # noqa: E402
from services import score_v3_service as s3  # noqa: E402
from services import strategy_core_service as core  # noqa: E402
from services import walk_forward_service as wf  # noqa: E402

HOUR = 3_600_000
BAR5 = 300_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % HOUR)


def market_state():
    """Mesmo cenário bull do bloco D, montado com os contratos reais."""
    rows = [core.Candle(open_time_ms=T0 + i * HOUR, open=100.3, high=100.6,
                        low=100.2, close=100.4, volume=100.0) for i in range(79)]
    trigger = core.Candle(open_time_ms=T0 + 79 * HOUR, open=100.3, high=101.2,
                          low=100.2, close=101.0, volume=90.0)
    return core.MarketState(
        symbol="SYN/USDT:USDT", timeframe="1h", bar_ms=HOUR,
        as_of_ms=T0 + 80 * HOUR, bars=tuple(rows + [trigger]), atr=1.0,
        ema_fast=100.0, ema_slow=98.0, adx=30.0, rsi=60.0, volume_ma=100.0,
        target_levels=(102.5, 103.5),
        higher_timeframes=(core.HigherTimeframeView(
            timeframe="4h", as_of_ms=T0 + 79 * HOUR, direction="long",
            confirmed=True, strength=0.8),))


def rising_bars(start_ms, count=40):
    """Velas de 5m subindo o bastante para alcançar os alvos do candidato."""
    return [{"timestamp_ms": start_ms + i * BAR5,
             "open": 101.0 + i * 0.1, "high": 101.2 + i * 0.1,
             "low": 100.9 + i * 0.1, "close": 101.1 + i * 0.1, "volume": 25.0}
            for i in range(count)]


class FluxoSintetico(unittest.TestCase):
    """candidato → seleção → evidência → simulação → comparação → go/no-go."""

    def setUp(self):
        self.decision = core.decide(market_state())

    def test_1_candidato_do_nucleo(self):
        self.assertEqual(self.decision["state"], core.STATE_ELIGIBLE)
        self.assertEqual(self.decision["playbook"], core.PLAYBOOK_TREND_PULLBACK)
        self.assertFalse(self.decision["executable"])
        self.assertIsNotNone(self.decision["opportunity_key"])

    def test_2_selecao_so_em_simulacao(self):
        envelope = core.selection_adapter(self.decision, mode="simulation")
        self.assertEqual(envelope["route"], "SIMULATION")
        self.assertFalse(envelope["executable"])
        self.assertEqual(envelope["live_route"], "UNAVAILABLE")
        self.assertEqual(envelope["candidate"]["opportunity_key"],
                         self.decision["opportunity_key"])

    def test_3_score_tecnico_sem_probabilidade(self):
        levels = self.decision["levels"]
        risco = levels["entry"] - levels["stop_loss"]
        payload = s3.score({
            "adx": 30.0, "htf_alignment_ratio": 1.0, "structure_quality": 0.8,
            "level_distance_atr": 0.5, "trigger_body_ratio": 0.7,
            "trigger_follow_through_atr": 0.4,
            "rr_tp2": (levels["tp2"] - levels["entry"]) / risco,
            "entry_distance_atr": 0.1, "volume_ratio": 1.1, "spread_pct": 0.03,
            "funding_pct": 0.0,
        }, playbook=self.decision["playbook"], side=self.decision["side"])
        self.assertEqual(payload["state"], s3.STATE_OK)
        self.assertIsNone(payload["probability"])
        self.assertEqual(s3.economic_verdict(payload)["live_eligibility"],
                         s3.STATE_UNAVAILABLE)

    def test_4_evidencia_pre_selecao_com_a_mesma_vela(self):
        chave, motivo = pre.pre_selection_identity(
            symbol=self.decision["symbol"], timeframe=self.decision["timeframe"],
            side=self.decision["side"],
            trigger_candle_ms=self.decision["trigger_candle_ms"],
            playbook=self.decision["playbook"],
            playbook_version=self.decision["playbook_version"])
        self.assertEqual(motivo, pre.OK)
        funil = pre.record_funnel([
            {"stage": pre.STAGE_CANDIDATE, "verdict": pre.VERDICT_PASSED},
            {"stage": pre.STAGE_PLAYBOOK, "verdict": pre.VERDICT_PASSED},
            {"stage": pre.STAGE_CANDLE, "verdict": pre.VERDICT_PASSED},
            {"stage": pre.STAGE_GEOMETRY_RR, "verdict": pre.VERDICT_PASSED},
            {"stage": pre.STAGE_LIQUIDITY, "verdict": pre.VERDICT_UNKNOWN,
             "reason_code": "DEPTH_UNAVAILABLE"}])
        congelada = pre.frozen_decision(
            identity=chave, outcome=pre.OUTCOME_ACCEPTED,
            decision_ts_ms=self.decision["decision_ts_ms"],
            setup={**self.decision["levels"], "symbol": self.decision["symbol"],
                   "timeframe": self.decision["timeframe"],
                   "side": self.decision["side"],
                   "playbook": self.decision["playbook"],
                   "playbook_version": self.decision["playbook_version"],
                   "trigger_candle_ms": self.decision["trigger_candle_ms"]},
            funnel=funil, availability={"depth": False},
            source=pre.window_source("binance", "binance",
                                     symbol=self.decision["symbol"], resolution="5m"))
        self.assertEqual(congelada["setup"]["trigger_candle_ms"],
                         self.decision["trigger_candle_ms"])
        self.assertFalse(congelada["learning_eligible"])
        # Etapas posteriores à liquidez não foram avaliadas — e não viram aprovação.
        self.assertIn(pre.STAGE_RISK, funil["stages_not_evaluated"])

    def _simular(self):
        levels = self.decision["levels"]
        decisao_ms = self.decision["decision_ts_ms"]
        primeiro = ((decisao_ms + BAR5 - 1) // BAR5) * BAR5
        candidato = {"opportunity_id": self.decision["opportunity_key"],
                     "symbol": self.decision["symbol"],
                     "direction": self.decision["side"],
                     "decision_ts_ms": decisao_ms,
                     "entry": levels["entry"], "stop_loss": levels["stop_loss"],
                     "tp1": levels["tp1"], "tp2": levels["tp2"], "atr": 1.0}
        return pf.run_portfolio(
            [candidato], bars_by_id={candidato["opportunity_id"]: rising_bars(primeiro)},
            quotes_by_id={candidato["opportunity_id"]: {
                "bid": 100.99, "ask": 101.01, "ts_ms": decisao_ms, "source": "binance"}},
            costs=r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                                  funding_bps_per_bar=1.0))

    def test_5_simulacao_de_carteira_usa_o_candidato_do_nucleo(self):
        resultado = self._simular()
        self.assertEqual(resultado["admitted"], 1)
        trade = resultado["trades"][0]
        self.assertEqual(trade["opportunity_id"], self.decision["opportunity_key"])
        self.assertIn(trade["status"], r10a.REPLAY_STATUSES)
        self.assertFalse(resultado["live_equivalent"])
        self.assertFalse(resultado["promotable"])

    def test_6_comparacao_nao_declara_vencedor_com_uma_amostra(self):
        resultado = self._simular()
        trade = resultado["trades"][0]
        pareado = wf.pair_opportunities(
            [{"opportunity_id": trade["opportunity_id"], "net_r": trade["net_r"]}],
            [{"opportunity_id": trade["opportunity_id"], "net_r": trade["net_r"]}])
        ci = wf.block_bootstrap_ci([trade["net_r"]], seed=1, block_size=5)
        disciplina = {stage: "train" for stage in wf.FITTED_STAGES}
        disciplina["candidate_selected_on"] = "validation"
        veredicto = wf.verdict(
            folds=wf.rolling_windows(start_ms=T0, end_ms=T0 + 200 * BAR5, bar_ms=BAR5,
                                     train_bars=40, test_bars=10)["folds"],
            discipline=wf.fold_discipline(disciplina),
            coverage=wf.coverage_guard({"considered": 1, "resolved": 1},
                                       {"considered": 1, "resolved": 1}),
            costs_complete=True, horizon_sufficient=True, paired=pareado, ci=ci)
        self.assertIsNone(veredicto["winner"])
        self.assertEqual(veredicto["state"], wf.INSUFFICIENT_EVIDENCE)
        self.assertIn(wf.SAMPLE_INSUFFICIENT, veredicto["reason_codes"])
        self.assertFalse(veredicto["promotable"])

    def test_7_go_no_go_recusa_a_amostra_do_fluxo(self):
        resultado = self._simular()
        gate = r12.go_no_go({
            "total_shadow_trades": resultado["admitted"],
            "trades_per_playbook": {self.decision["playbook"]: resultado["admitted"]},
            "enabled_playbooks": (self.decision["playbook"],),
            "calendar_days": 1, "business_days": 1, "coverage_pct": 100.0,
            "net_ev_r": resultado["metrics"]["net_expectancy_r"],
            "uncertainty_r": None, "drawdown_r": resultado["metrics"]["max_drawdown_r"],
            "stability_ratio": None, "operational_failures": 0,
            "economic_duplicates": 0, "unresolved_protection_failures": 0,
            "essential_gaps": ["prospective_sample"], "fidelity_discrepancy_pct": None,
        })
        self.assertEqual(gate["verdict"], "NO_GO")
        self.assertEqual(gate["live_approval"], "UNAVAILABLE")
        for motivo in (r12.SAMPLE_INSUFFICIENT, r12.DURATION_INSUFFICIENT,
                       r12.BUSINESS_DAYS_INSUFFICIENT, r12.ESSENTIAL_GAP):
            self.assertIn(motivo, gate["reason_codes"])

    def test_8_identidade_atravessa_o_fluxo_inteiro(self):
        envelope = core.selection_adapter(self.decision, mode="simulation")
        resultado = self._simular()
        self.assertEqual(envelope["candidate"]["opportunity_key"],
                         resultado["trades"][0]["opportunity_id"])
        repetida = core.decide(market_state())
        self.assertEqual(repetida["opportunity_key"], self.decision["opportunity_key"])


class DefaultsPreservados(unittest.TestCase):
    SELETORES = ("R05_FINANCIAL_TOTAL_SOURCE", "R11_POLICY_VERSION",
                 "R07_STRATEGY_CORE_MODE", "R08_SCORE_V3_MODE",
                 "R09_PRESELECTION_MODE", "R10_PORTFOLIO_REPLAY_MODE",
                 "R10_WALK_FORWARD_MODE", "R12_PRE_SELECTION_MODE")

    def setUp(self):
        self.anteriores = {name: os.environ.pop(name, None) for name in self.SELETORES}

    def tearDown(self):
        for name, valor in self.anteriores.items():
            os.environ.pop(name, None)
            if valor is not None:
                os.environ[name] = valor

    def test_versoes_novas_nascem_desligadas(self):
        from services import financial_total_service as r05d
        from services import robust_policy_service as r11c
        self.assertEqual(r05d.selected_source(), r05d.SOURCE_LEGACY)
        self.assertEqual(r11c.selected_policy(), r11c.POLICY_LEGACY)
        self.assertEqual(core.selected_mode(), core.MODE_INACTIVE)
        self.assertEqual(s3.selected_mode(), s3.MODE_INACTIVE)
        self.assertFalse(pre.collection_enabled())
        self.assertEqual(pf.selected_mode(), pf.MODE_INACTIVE)
        self.assertEqual(wf.selected_mode(), wf.MODE_INACTIVE)
        self.assertEqual(r12.selected_mode(), r12.MODE_INACTIVE)

    def test_resumo_mostra_apenas_a_correcao_ativa(self):
        resumo = batch.lote_final_summary()
        ativos = [row for row in resumo["blocks"]
                  if row.get("mode") not in ("inactive", "legacy")]
        self.assertEqual([row["block"] for row in ativos], ["A"])
        self.assertEqual(ativos[0]["contract"], "SAFETY_FIX")
        self.assertFalse(resumo["live_changed"])
        self.assertFalse(resumo["promotable"])
        self.assertEqual(resumo["live_approval"], "UNAVAILABLE")
        self.assertTrue(resumo["bot_operation_meaning_unchanged"])
        self.assertEqual(resumo["unavailable_blocks"], [])

    def test_resumo_e_fail_soft_por_item(self):
        """Um módulo quebrado degrada a linha dele, não o GET inteiro."""
        import services

        chave = "services.strategy_core_service"
        nome = "strategy_core_service"
        original_mod = sys.modules.get(chave)
        original_attr = getattr(services, nome, None)
        quebrado = types.ModuleType(chave)

        def explode(_name):
            raise RuntimeError("módulo indisponível no teste")

        quebrado.__getattr__ = explode
        # `from services import X` resolve pelo ATRIBUTO do pacote quando ele já
        # existe; trocar só `sys.modules` não simularia a falha.
        sys.modules[chave] = quebrado
        setattr(services, nome, quebrado)
        try:
            resumo = batch.lote_final_summary()
        finally:
            sys.modules.pop(chave, None)
            if original_mod is not None:
                sys.modules[chave] = original_mod
            if original_attr is not None:
                setattr(services, nome, original_attr)
            else:
                delattr(services, nome)
        linha = next(row for row in resumo["blocks"] if row["block"] == "D")
        self.assertEqual(linha["state"], "UNAVAILABLE")
        self.assertEqual(linha["reason_code"], "D_MANIFEST_UNAVAILABLE")
        self.assertEqual(resumo["unavailable_blocks"], ["D"])
        self.assertEqual(resumo["state"], "LOCAL_RESEARCH_ONLY")

    def test_resumo_nao_promete_operacao(self):
        resumo = batch.lote_final_summary()
        self.assertEqual(resumo["evidence"]["prospective_simulation"], "NOT_STARTED")
        self.assertEqual(resumo["evidence"]["economic_validation"], "NOT_STARTED")
        self.assertEqual(resumo["evidence"]["human_approval"], "NOT_REQUESTED")
        self.assertTrue(resumo["blockers"])
        self.assertTrue(resumo["next_step"])


if __name__ == "__main__":
    unittest.main()
