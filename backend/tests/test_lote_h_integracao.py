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


class OrquestracaoReal(unittest.TestCase):
    """O fluxo é exercitado pelo ENTRYPOINT oficial, não montado no teste.

    O teste não fornece decisões nem métricas: ele executa
    `backend/scripts/research_pipeline.py` — o mesmo caminho que outra pessoa
    roda — e verifica o relatório que a aplicação produz.
    """

    @classmethod
    def setUpClass(cls):
        import json
        import subprocess
        script = BACKEND / "scripts" / "research_pipeline.py"
        cls.proc = subprocess.run(
            [sys.executable, "-B", str(script), "--symbols", "8", "--seed", "7"],
            capture_output=True, text=True, cwd=str(BACKEND),
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTHONPATH": str(BACKEND)})
        saida = cls.proc.stdout
        cls.report = json.loads(saida[saida.index("{"):]) if "{" in saida else None

    def test_entrypoint_executa_e_devolve_relatorio(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr[-800:])
        self.assertIsNotNone(self.report)
        self.assertEqual(self.report["entrypoint"], "research_pipeline")
        self.assertEqual(self.report["mode"], "LOCAL_RESEARCH_ONLY")

    def test_nucleo_decide_e_nada_e_executavel(self):
        candidatos = self.report["candidates"]
        self.assertGreater(candidatos["evaluated"], 0)
        self.assertGreaterEqual(candidatos["evaluated"], candidatos["eligible"])
        self.assertFalse(self.report["live_equivalent"])
        self.assertFalse(self.report["promotable"])

    def test_score_v3_nao_entrega_probabilidade(self):
        self.assertFalse(self.report["score_v3"]["probability_available"])
        self.assertEqual(self.report["score_v3"]["economic_approval"], "UNAVAILABLE")

    def test_observacao_desligada_por_padrao(self):
        self.assertFalse(self.report["observation"]["enabled"])
        self.assertEqual(self.report["observation"]["accepted"], 0)
        self.assertEqual(self.report["observation"]["vetoed"], 0)

    def test_carteira_compartilhada_limita_a_simulacao(self):
        replay = self.report["replay"]
        self.assertGreaterEqual(replay["admitted"], 1)
        self.assertLessEqual(replay["admitted"], self.report["candidates"]["eligible"])
        self.assertFalse(replay["live_equivalent"])
        self.assertIn("queue_position", replay["fidelity_unavailable"])
        self.assertIsNotNone(replay["metrics"]["capital_end_usd"])

    def test_walk_forward_executa_dobras_e_nao_promove(self):
        wf_report = self.report["walk_forward"]
        self.assertGreaterEqual(wf_report["folds_executed"], 1)
        self.assertFalse(wf_report["promotable"])
        if wf_report["winner"] is not None:
            self.assertEqual(wf_report["state"], "EVIDENCE_AVAILABLE")

    def test_comparacao_declara_escopo_e_hipotese_bloqueada(self):
        """F — o contraste executado é de GESTÃO; a hipótese autorizada não
        existe nos contratos, então ela aparece BLOQUEADA, não substituída."""
        comparacao = self.report["comparison"]
        self.assertEqual(comparacao["scope"], "MANAGEMENT_ONLY")
        self.assertIn("Score V3", comparacao["does_not_prove"])
        self.assertIn("núcleo R07D", comparacao["does_not_prove"])
        self.assertTrue(comparacao["identity_registered_before_results"])
        self.assertTrue(comparacao["baseline_config_hash"])
        self.assertTrue(comparacao["candidate_config_hash"])
        self.assertNotEqual(comparacao["baseline_config_hash"],
                            comparacao["candidate_config_hash"])
        self.assertTrue(comparacao["bundle_hash"])
        autorizada = comparacao["authorized_hypothesis"]
        self.assertFalse(autorizada["available"])
        self.assertEqual(autorizada["state"], "BLOCKED_MISSING_DECISION")
        self.assertEqual(autorizada["reason_code"], "AUTHORIZED_CANDIDATE_NOT_DECLARED")
        self.assertGreaterEqual(len(autorizada["decision_required"]), 2)
        # O lado candidato declara o mesmo escopo — nada de rótulo trocado.
        self.assertEqual(self.report["candidate_replay"]["comparison_scope"],
                         "MANAGEMENT_ONLY")

    def test_mesma_configuracao_dos_dois_lados_da_o_mesmo_resultado(self):
        """Igual decisão/execução sob igual configuração ⇒ igual resultado."""
        from services import offline_replay_service as r10a
        from services import portfolio_replay_service as pf
        import importlib, sys as _sys
        pipeline = importlib.import_module("scripts.research_pipeline")
        self.assertTrue(hasattr(pipeline, "comparacao_declarada"))
        config = r10a.ReplayConfig()
        candidatos = [{"opportunity_id": "a", "symbol": "SYN/USDT:USDT",
                       "direction": "long", "decision_ts_ms": 1_000_000,
                       "entry": 100.0, "stop_loss": 99.0, "tp1": 103.0,
                       "tp2": 106.0, "atr": 1.0}]
        barras = [{"timestamp_ms": 1_200_000 + i * 300_000, "open": 100.0 + i,
                   "high": 100.5 + i, "low": 99.5 + i, "close": 100.2 + i,
                   "volume": 10.0} for i in range(6)]
        cotacao = {"a": {"bid": 99.99, "ask": 100.0, "ts_ms": 1_000_000,
                         "source": "synthetic"}}
        custos = r10a.CostConfig(fee_bps_per_side=4.0, slippage_bps_per_side=2.0,
                                 funding_bps_per_bar=1.0)
        um = pf.run_portfolio(candidatos, bars_by_id={"a": barras},
                              quotes_by_id=cotacao, replay_config=config, costs=custos)
        outro = pf.run_portfolio(candidatos, bars_by_id={"a": barras},
                                 quotes_by_id=cotacao, replay_config=config, costs=custos)
        self.assertEqual(um["metrics"]["net_total_r"], outro["metrics"]["net_total_r"])
        self.assertEqual(um["config_hash"], outro["config_hash"])

    def test_candidato_e_executado_e_nao_fabricado(self):
        """O lado candidato sai do MOTOR, não de baseline mais um bônus."""
        candidato = self.report["candidate_replay"]
        self.assertTrue(candidato["executed"])
        self.assertEqual(candidato["kind"], "MANAGEMENT_ONLY")
        # A diferença entre as políticas é declarada em configuração.
        self.assertTrue(candidato["config_diff"], str(candidato))
        for campo, (antes, depois) in candidato["config_diff"].items():
            self.assertNotEqual(antes, depois, campo)
        base_total = self.report["replay"]["metrics"]["net_total_r"]
        cand_total = candidato["metrics"]["net_total_r"]
        if base_total is not None and cand_total is not None:
            # Nada garante vantagem ao candidato: a diferença é o que o motor
            # produziu, nunca um acréscimo constante por oportunidade.
            admitidos = max(1, int(self.report["replay"]["admitted"]))
            self.assertNotAlmostEqual(cand_total - base_total, 0.05 * admitidos, places=9)

    def test_gate_vem_do_resultado_e_nao_libera_live(self):
        gate = self.report["gate"]
        self.assertTrue(gate["evidence_from_computed_results"])
        self.assertEqual(gate["live_approval"], "UNAVAILABLE")
        self.assertIn(gate["verdict"], ("NO_GO", "GO_CANDIDATE"))
        self.assertTrue(gate["criteria_hash"])

    def test_capital_liquida_no_instante_da_saida(self):
        """O relatório mostra o capital de ENTRADA e o da SAÍDA de cada trade."""
        linha = self.report["replay"]["capital_timeline"]
        self.assertTrue(linha, "nenhum trade admitido para inspecionar")
        for trade in linha:
            self.assertIsNotNone(trade["capital_at_entry_usd"], str(trade))
            self.assertLessEqual(trade["effective_ts_ms"], trade["exit_ts_ms"], str(trade))
        # Nenhuma entrada foi dimensionada com capital de trade que saiu DEPOIS.
        for trade in linha:
            posteriores = [outro for outro in linha
                           if outro["exit_ts_ms"] > trade["effective_ts_ms"]
                           and outro["opportunity_id"] != trade["opportunity_id"]]
            for outro in posteriores:
                self.assertNotEqual(trade["capital_at_entry_usd"],
                                    outro["capital_after_usd"],
                                    f"{trade['opportunity_id']} usou saída futura de "
                                    f"{outro['opportunity_id']}")

    def test_selecao_governa_a_politica_no_walk_forward(self):
        wf_report = self.report["walk_forward"]
        self.assertTrue(wf_report["selection_governs_evaluation"])
        self.assertLessEqual(wf_report["folds_running_candidate"],
                             wf_report["folds_executed"])

    def test_simulacao_r11_declara_o_que_fez(self):
        """Sem banco declarado a etapa diz que foi PULADA — não finge rodar."""
        simulacao = self.report["policy_simulation"]
        self.assertEqual(simulacao["state"], "SKIPPED")
        self.assertEqual(simulacao["reason_code"], "PERSISTENCE_NOT_REQUESTED")
        self.assertFalse(simulacao["persisted"])
        self.assertEqual(simulacao["identity"]["population"], "SHADOW")
        self.assertTrue(simulacao["evidence_key"])

    def test_adaptador_operacional_declara_o_que_falta(self):
        """Falta de código não é renomeada para pendência externa."""
        self.assertEqual(self.report["live_adapter"], "LIVE_ADAPTER_NOT_IMPLEMENTED")

    def test_nenhuma_ordem_ou_chamada_externa_no_caminho(self):
        proibidos = ("place_order", "kill-switch", "telegram", "binance.com")
        blob = (self.proc.stdout + self.proc.stderr).lower()
        for termo in proibidos:
            self.assertNotIn(termo, blob, termo)


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
        """A evidência agora é DERIVADA (estado + qualidade + último instante),
        mas continua sem prometer operação: coleta desligada é NOT_STARTED,
        calibração sem artefato é NOT_STARTED e aprovação humana fica
        BLOQUEADA enquanto não houver manifesto autorizado."""
        resumo = batch.lote_final_summary()
        evidencia = resumo["evidence"]
        self.assertTrue(evidencia["derived"])
        self.assertFalse(evidencia["expensive_work_in_get"])
        self.assertEqual(evidencia["prospective_simulation"]["state"],
                         batch.EVIDENCE_NOT_STARTED)
        self.assertEqual(evidencia["economic_validation"]["state"],
                         batch.EVIDENCE_NOT_STARTED)
        self.assertEqual(evidencia["human_approval"]["state"],
                         batch.EVIDENCE_BLOCKED)
        self.assertFalse(evidencia["human_approval"]["real_study_allowed"])
        self.assertEqual(evidencia["canary"]["state"], "NOT_PREPARED_FOR_APPLY")
        self.assertTrue(resumo["blockers"])
        self.assertTrue(resumo["next_step"])


if __name__ == "__main__":
    unittest.main()
