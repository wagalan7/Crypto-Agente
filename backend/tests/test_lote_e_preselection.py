"""Bloco E — evidência pré-seleção do R09, aditiva e sem mexer no denominador.

Cobre ordem real do funil, etapa não avaliada, identidade compartilhada por
aceitas e vetadas, horizonte por timeframe, cobertura, rótulo de origem,
orçamento/capacidade, aditividade do contrato e segregação da pesquisa.
"""
import ast
import os
import sys
import time
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import preselection_observation_service as pre  # noqa: E402

IDENTIDADE = dict(symbol="BTCUSDT", timeframe="1h", side="long",
                  trigger_candle_ms=1_700_000_000_000,
                  playbook="TREND_PULLBACK", playbook_version="TREND_PULLBACK_V1")


def funil(*pares):
    return [{"stage": stage, "verdict": verdict, "reason_code": reason,
             "observed_at_ms": 1_700_000_000_000 + indice}
            for indice, (stage, verdict, reason) in enumerate(pares)]


class Funil(unittest.TestCase):
    def test_ordem_real_e_registrada(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
            (pre.STAGE_PLAYBOOK, pre.VERDICT_PASSED, None),
            (pre.STAGE_CANDLE, pre.VERDICT_PASSED, None),
            (pre.STAGE_GEOMETRY_RR, pre.VERDICT_REJECTED, "RR_BELOW_FLOOR")))
        self.assertEqual(registro["observed_order"][:4],
                         list(pre.STAGES[:4]))
        self.assertFalse(registro["out_of_order"])
        self.assertEqual(registro["first_blocker"], pre.STAGE_GEOMETRY_RR)
        self.assertEqual(registro["first_blocker_reason"], "RR_BELOW_FLOOR")

    def test_etapa_que_nao_rodou_nao_e_aprovada(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
            (pre.STAGE_PLAYBOOK, pre.VERDICT_REJECTED, "NO_PLAYBOOK")))
        for etapa in (pre.STAGE_LIQUIDITY, pre.STAGE_MTF_REGIME, pre.STAGE_RISK,
                      pre.STAGE_EXECUTION):
            self.assertEqual(registro["stages"][etapa]["verdict"],
                             pre.VERDICT_NOT_EVALUATED, etapa)
            self.assertNotEqual(registro["stages"][etapa]["verdict"], pre.VERDICT_PASSED)
        self.assertEqual(len(registro["stages_not_evaluated"]), 7)

    def test_primeira_rejeicao_nao_vira_causa_contrafactual(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
            (pre.STAGE_CANDLE, pre.VERDICT_REJECTED, "CANDLE_OPEN"),
            (pre.STAGE_LIQUIDITY, pre.VERDICT_REJECTED, "SPREAD_WIDE")))
        self.assertEqual(registro["causal_claim"], "NONE")
        self.assertEqual(registro["counterfactual_causes"], [])
        self.assertEqual(registro["blockers_observed"],
                         [pre.STAGE_CANDLE, pre.STAGE_LIQUIDITY])
        self.assertTrue(registro["stages"][pre.STAGE_LIQUIDITY]["after_first_rejection"])
        self.assertFalse(registro["stages"][pre.STAGE_CANDLE]["after_first_rejection"])

    def test_not_evaluated_nao_pode_ser_observado(self):
        registro = pre.record_funnel([
            {"stage": pre.STAGE_CANDIDATE, "verdict": pre.VERDICT_NOT_EVALUATED}])
        self.assertIn(pre.VERDICT_INVALID, registro["problems"])
        self.assertEqual(registro["stages"][pre.STAGE_CANDIDATE]["verdict"],
                         pre.VERDICT_NOT_EVALUATED)
        self.assertEqual(registro["observed_order"], [])

    def test_estagio_desconhecido_e_duplicado(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
            (pre.STAGE_CANDIDATE, pre.VERDICT_REJECTED, "DUPLICADO"),
            ("INVENTADA", pre.VERDICT_PASSED, None)))
        self.assertEqual(registro["problems"],
                         sorted({pre.STAGE_DUPLICATED, pre.STAGE_UNKNOWN}))
        self.assertEqual(registro["stages"][pre.STAGE_CANDIDATE]["verdict"],
                         pre.VERDICT_PASSED)

    def test_fora_de_ordem_e_sinalizado(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_RISK, pre.VERDICT_PASSED, None),
            (pre.STAGE_CANDLE, pre.VERDICT_PASSED, None)))
        self.assertTrue(registro["out_of_order"])

    def test_desconhecido_nao_e_rejeicao(self):
        registro = pre.record_funnel(funil(
            (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
            (pre.STAGE_LIQUIDITY, pre.VERDICT_UNKNOWN, "DEPTH_UNAVAILABLE")))
        self.assertIsNone(registro["first_blocker"])
        self.assertEqual(registro["blockers_observed"], [])


class Identidade(unittest.TestCase):
    def test_aceita_e_vetada_compartilham_identidade(self):
        chave_a, motivo = pre.pre_selection_identity(**IDENTIDADE)
        chave_b, _ = pre.pre_selection_identity(**dict(IDENTIDADE, symbol="btcusdt"))
        self.assertEqual(motivo, pre.OK)
        self.assertEqual(chave_a, chave_b)
        self.assertTrue(chave_a.startswith("pre-"))

    def test_identidade_muda_com_vela_lado_e_playbook(self):
        base, _ = pre.pre_selection_identity(**IDENTIDADE)
        for mudanca in ({"side": "short"}, {"trigger_candle_ms": 1_700_000_060_000},
                        {"playbook": "TREND_BREAKOUT"},
                        {"playbook_version": "TREND_PULLBACK_V2"},
                        {"timeframe": "4h"}):
            outra, _ = pre.pre_selection_identity(**dict(IDENTIDADE, **mudanca))
            self.assertNotEqual(base, outra, mudanca)

    def test_identidade_incompleta_nao_e_inventada(self):
        for mudanca in ({"symbol": None}, {"trigger_candle_ms": 0},
                        {"trigger_candle_ms": None}, {"side": "neutro"},
                        {"playbook_version": "  "}):
            chave, motivo = pre.pre_selection_identity(**dict(IDENTIDADE, **mudanca))
            self.assertIsNone(chave, mudanca)
            self.assertEqual(motivo, pre.MISSING_IDENTITY)

    def test_oportunidade_nao_e_tentativa(self):
        chave, _ = pre.pre_selection_identity(**IDENTIDADE)
        primeira = pre.attempt_key(chave, attempt_index=0)
        segunda = pre.attempt_key(chave, attempt_index=1)
        self.assertNotEqual(primeira, segunda)
        self.assertNotEqual(primeira, chave)
        self.assertTrue(primeira.startswith(chave))
        self.assertEqual(primeira, pre.attempt_key(chave, attempt_index=0))
        with self.assertRaises(ValueError):
            pre.attempt_key("sem-prefixo", attempt_index=0)


class Horizonte(unittest.TestCase):
    def test_horizonte_vem_do_timeframe(self):
        curto = pre.horizon_bars("5m")
        longo = pre.horizon_bars("1h")
        self.assertEqual(curto["bars"], 12)
        self.assertFalse(curto["truncated"])
        self.assertGreater(longo["bars"], curto["bars"])
        self.assertNotEqual(curto["bars"], 24)  # 2h fixo não existe aqui

    def test_truncamento_e_reportado(self):
        verdict = pre.horizon_bars("4h")
        self.assertTrue(verdict["truncated"])
        self.assertEqual(verdict["reason_code"], pre.HORIZON_TRUNCATED)
        self.assertEqual(verdict["bars"], pre.MAX_RESEARCH_BARS)

    def test_timeframe_desconhecido(self):
        verdict = pre.horizon_bars("7h")
        self.assertIsNone(verdict["bars"])
        self.assertEqual(verdict["reason_code"], pre.TIMEFRAME_UNKNOWN)

    def test_maior_horizonte_entre_candidatos(self):
        verdict = pre.required_horizon([{"candidate_id": "a", "timeframe": "5m"},
                                        {"candidate_id": "b", "timeframe": "30m"}])
        self.assertEqual(verdict["bars"], 72)
        desconhecido = pre.required_horizon([{"candidate_id": "c", "timeframe": "7h"}])
        self.assertEqual(desconhecido["unknown_timeframes"], ["c"])

    def test_baseline_resolvido_nao_encerra_a_janela(self):
        verdict = pre.collection_complete([
            {"candidate_id": "baseline", "timeframe": "5m", "terminal": True},
            {"candidate_id": "candidato", "timeframe": "30m", "terminal": False,
             "bars_collected": 12}])
        self.assertFalse(verdict["complete"])
        self.assertEqual(verdict["reason_code"], pre.KEEP_COLLECTING)
        self.assertEqual(verdict["pending_candidates"], ["candidato"])

    def test_horizonte_esgotado_encerra(self):
        verdict = pre.collection_complete([
            {"candidate_id": "baseline", "timeframe": "5m", "terminal": True},
            {"candidate_id": "candidato", "timeframe": "30m", "terminal": False,
             "bars_collected": 72}])
        self.assertTrue(verdict["complete"])


class Cobertura(unittest.TestCase):
    def test_falha_de_coleta_nao_e_prejuizo_zero(self):
        verdict = pre.coverage_verdict({"collection_error": "TIMEOUT"})
        self.assertEqual(verdict["coverage"], pre.COVERAGE_FAILED)
        self.assertIsNone(verdict["pnl_assumption"])
        self.assertFalse(verdict["outcome_available"])

    def test_incompleto_e_pendente(self):
        incompleto = pre.coverage_verdict({"candidates": [
            {"candidate_id": "a", "timeframe": "5m", "terminal": False,
             "bars_collected": 1}]})
        self.assertEqual(incompleto["coverage"], pre.COVERAGE_INCOMPLETE)
        pendente = pre.coverage_verdict({"candidates": [
            {"candidate_id": "a", "timeframe": "5m", "terminal": True}]})
        self.assertEqual(pendente["coverage"], pre.COVERAGE_PENDING)
        self.assertIsNone(pendente["pnl_assumption"])

    def test_completo_exige_outcome(self):
        verdict = pre.coverage_verdict({
            "candidates": [{"candidate_id": "a", "timeframe": "5m", "terminal": True}],
            "outcome": {"status": "CLOSED_TP2", "r": 2.0}})
        self.assertEqual(verdict["coverage"], pre.COVERAGE_COMPLETE)
        self.assertTrue(verdict["outcome_available"])


class OrigemDaJanela(unittest.TestCase):
    def test_okx_nao_vira_binance(self):
        verdict = pre.window_source("binance", "okx", symbol="BTCUSDT", resolution="5m")
        self.assertEqual(verdict["reason_code"], pre.SOURCE_MISMATCH)
        self.assertEqual(verdict["label"], "UNLABELED")
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["candle_source"], "okx")

    def test_sem_rotulo_fica_unlabeled(self):
        verdict = pre.window_source(None, "binance", symbol="BTCUSDT", resolution="5m")
        self.assertEqual(verdict["reason_code"], pre.SOURCE_UNLABELED)
        self.assertEqual(verdict["label"], "UNLABELED")

    def test_mesma_origem_e_utilizavel(self):
        verdict = pre.window_source("binance", "Binance", symbol="BTCUSDT",
                                    resolution="5m")
        self.assertTrue(verdict["usable"])
        self.assertEqual(verdict["resolution"], "5m")


class CustoELimites(unittest.TestCase):
    def test_teto_recusa_sem_apagar(self):
        verdict = pre.admission_verdict({"opportunities": pre.PRE_CAPACITY["opportunities"],
                                         "attempts": 10})
        self.assertFalse(verdict["accept"])
        self.assertEqual(verdict["reason_code"], pre.CAPACITY_REACHED)
        self.assertFalse(verdict["deletes_history"])

    def test_proximo_do_teto_e_sinalizado(self):
        quase = int(pre.PRE_CAPACITY["opportunities"] * 0.95)
        verdict = pre.admission_verdict({"opportunities": quase})
        self.assertTrue(verdict["accept"])
        self.assertTrue(verdict["near_capacity"])

    def test_orcamento_por_lote_e_buffer(self):
        self.assertTrue(pre.budget_verdict(batch_records=10, buffered_records=10)["within_budget"])
        estourado = pre.budget_verdict(batch_records=pre.MAX_BATCH_RECORDS + 1,
                                       buffered_records=1)
        self.assertFalse(estourado["within_budget"])
        self.assertEqual(estourado["reason_code"], pre.BUDGET_EXCEEDED)

    def test_payload_cabe_no_orcamento(self):
        payload = pre.frozen_decision(
            identity=pre.pre_selection_identity(**IDENTIDADE)[0],
            outcome=pre.OUTCOME_VETOED, decision_ts_ms=1_700_000_003_600_000,
            setup=dict(IDENTIDADE, entry=101.0, stop_loss=99.9, tp1=102.5, tp2=103.5,
                       atr=1.0, lixo="não deve viajar"),
            funnel=pre.record_funnel(funil(
                (pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
                (pre.STAGE_LIQUIDITY, pre.VERDICT_REJECTED, "SPREAD_WIDE"))),
            availability={"depth": False, "funding": True, "htf": "desconhecido"},
            source=pre.window_source("binance", "binance", symbol="BTCUSDT",
                                     resolution="5m"),
            config={"schema_version": pre.PRE_SCHEMA_VERSION})
        self.assertLessEqual(payload["payload_bytes"], pre.MAX_PAYLOAD_BYTES)
        self.assertNotIn("lixo", payload["setup"])
        self.assertEqual(payload["availability"]["htf"], pre.VERDICT_UNKNOWN)
        self.assertFalse(payload["learning_eligible"])
        self.assertIn("RealTrade", payload["segregated_from"])

    def test_custo_de_registro_e_limitado(self):
        entrada = funil((pre.STAGE_CANDIDATE, pre.VERDICT_PASSED, None),
                        (pre.STAGE_PLAYBOOK, pre.VERDICT_PASSED, None),
                        (pre.STAGE_CANDLE, pre.VERDICT_REJECTED, "CANDLE_OPEN"))
        inicio = time.perf_counter()
        for _ in range(500):
            pre.record_funnel(entrada)
        self.assertLess(time.perf_counter() - inicio, 2.0)

    def test_decisao_congelada_recusa_entrada_invalida(self):
        chave = pre.pre_selection_identity(**IDENTIDADE)[0]
        with self.assertRaises(ValueError):
            pre.frozen_decision(identity=chave, outcome="TALVEZ", decision_ts_ms=1,
                                setup={}, funnel={}, availability={}, source={})
        with self.assertRaises(ValueError):
            pre.frozen_decision(identity="sem-prefixo", outcome=pre.OUTCOME_ACCEPTED,
                                decision_ts_ms=1, setup={}, funnel={}, availability={},
                                source={})


class AditividadeESegregacao(unittest.TestCase):
    def test_merge_nao_sobrescreve_contrato_antigo(self):
        antigo = {"schema_version": "r09.v1", "scope": "POST_SELECTION",
                  "bar_ms": 300_000, "learning_eligible": False}
        merged = pre.merge_into_config(antigo, {"schema_version": pre.PRE_SCHEMA_VERSION})
        self.assertEqual(merged["schema_version"], "r09.v1")
        self.assertEqual(merged["scope"], "POST_SELECTION")
        self.assertEqual(merged["bar_ms"], 300_000)
        self.assertEqual(merged["r09_pre_selection"]["schema_version"],
                         pre.PRE_SCHEMA_VERSION)

    def test_armazenamento_reaproveitado(self):
        plano = pre.storage_plan()
        self.assertFalse(plano["creates_new_table"])
        self.assertFalse(plano["creates_scheduler_or_worker"])
        self.assertFalse(plano["creates_exchange_client"])
        self.assertTrue(plano["reuses_existing_flush_and_resolver"])
        self.assertFalse(plano["retention_changed"])
        self.assertFalse(plano["deletes_history"])
        self.assertTrue(plano["experiment_evidence_preserved"])
        self.assertEqual(plano["tables"]["vetoed"], "rejected_setup_observations")

    def test_fonte_extra_fica_inativa(self):
        extra = pre.extra_source_dependency()
        self.assertFalse(extra["active"])
        self.assertEqual(extra["reason_code"], pre.DEPENDENCY_INACTIVE)

    def test_promocao_exige_contrato_explicito(self):
        contrato = pre.promotion_contract()
        self.assertFalse(contrato["auto_promotion"])
        self.assertFalse(contrato["vetoed_readable_by_learner"])
        self.assertFalse(contrato["vetoed_readable_by_calibration"])
        self.assertEqual(contrato["reason_code"],
                         pre.PROMOTION_REQUIRES_EXPLICIT_CONTRACT)

    def test_coleta_desligada_por_padrao(self):
        anterior = os.environ.pop(pre.MODE_ENV, None)
        try:
            self.assertFalse(pre.collection_enabled())
            os.environ[pre.MODE_ENV] = "live"
            self.assertFalse(pre.collection_enabled())
            os.environ[pre.MODE_ENV] = "observe"
            self.assertTrue(pre.collection_enabled())
        finally:
            os.environ.pop(pre.MODE_ENV, None)
            if anterior is not None:
                os.environ[pre.MODE_ENV] = anterior

    def test_modulo_nao_toca_banco_nem_exchange(self):
        tree = ast.parse((BACKEND / "services" /
                          "preselection_observation_service.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"hashlib", "json", "math", "os", "typing"})

    def test_escopo_legado_preservado(self):
        from services import decision_observation_service as r09
        self.assertEqual(r09._CONFIG["scope"], pre.LEGACY_SCOPE)
        self.assertEqual(r09.SCHEMA_VERSION, "r09.v1")
        self.assertNotEqual(pre.PRE_SCHEMA_VERSION, r09.SCHEMA_VERSION)
        manifest = pre.preselection_manifest()
        self.assertEqual(manifest["legacy_scope_preserved"], pre.LEGACY_SCOPE)
        self.assertEqual(manifest["scope"], pre.SCOPE)


if __name__ == "__main__":
    unittest.main()
