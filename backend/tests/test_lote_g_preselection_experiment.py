"""Bloco G — experimento de pré-seleção, gate go/no-go e ensaio de rollback.

Cobre tipo versionado sobre o catálogo existente, exclusividade, congelamento,
bloqueios de avanço, A/A, mínimos do gate, manifest de canário sem aplicação e
rollback local que preserva posição, proteção, intenção, ledger e histórico.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import preselection_experiment_service as r12  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 1, 5, tzinfo=UTC)   # segunda-feira


def evidence(**over):
    payload = {
        "total_shadow_trades": 120,
        "trades_per_playbook": {"TREND_PULLBACK": 40, "TREND_BREAKOUT": 40,
                                "RANGE_REVERSION": 40},
        "enabled_playbooks": ("TREND_PULLBACK", "TREND_BREAKOUT", "RANGE_REVERSION"),
        "calendar_days": 21,
        "business_days": 15,
        "coverage_pct": 95.0,
        "net_ev_r": 0.12,
        "uncertainty_r": 0.03,
        "drawdown_r": 4.0,
        "stability_ratio": 0.8,
        "operational_failures": 0,
        "economic_duplicates": 0,
        "unresolved_protection_failures": 0,
        "essential_gaps": [],
        "fidelity_discrepancy_pct": 1.0,
    }
    payload.update(over)
    return payload


class TipoDeExperimento(unittest.TestCase):
    def test_config_legada_continua_pos_selecao(self):
        self.assertEqual(r12.experiment_type({"score_min": 70}), r12.TYPE_POST_SELECTION)
        self.assertEqual(r12.experiment_type(None), r12.TYPE_POST_SELECTION)
        self.assertFalse(r12.is_pre_selection({"score_min": 70}))

    def test_tipo_novo_e_aditivo(self):
        marcada = r12.tag_config({"score_min": 70})
        self.assertEqual(marcada["score_min"], 70)
        self.assertEqual(marcada[r12.TYPE_KEY], r12.TYPE_PRE_SELECTION)
        self.assertEqual(marcada[r12.TYPE_VERSION_KEY], r12.TYPE_VERSION)
        self.assertTrue(r12.is_pre_selection(marcada))

    def test_contratos_nao_sao_intercambiaveis(self):
        pre = r12.tag_config({"playbook": "TREND_PULLBACK"})
        errado = r12.comparator_guard(expected_type=r12.TYPE_POST_SELECTION,
                                      candidate_config=pre)
        self.assertFalse(errado["ok"])
        self.assertEqual(errado["reason_code"], r12.TYPE_MISMATCH)
        self.assertFalse(errado["interchangeable"])
        certo = r12.comparator_guard(expected_type=r12.TYPE_PRE_SELECTION,
                                     candidate_config=pre)
        self.assertTrue(certo["ok"])

    def test_bloqueios_antigos_preservados(self):
        blocos = r12.legacy_blocks()
        self.assertEqual(blocos["p051_analytics_only"], "P051_ANALYTICS_ONLY")
        self.assertEqual(blocos["released_by_this_batch"], [])
        self.assertFalse(blocos["new_type_bypasses_legacy_blocks"])
        from services import strategy_evidence_service as p05
        self.assertEqual(p05.P051_BLOCK_REASON, r12.P051_BLOCK_REASON)

    def test_catalogo_unico(self):
        manifest = r12.r12_manifest()
        self.assertEqual(manifest["catalog"]["table"], "strategy_experiments")
        self.assertFalse(manifest["catalog"]["second_catalog"])
        self.assertFalse(manifest["catalog"]["second_bus"])
        self.assertFalse(manifest["catalog"]["second_promotion_panel"])


class Exclusividade(unittest.TestCase):
    def test_um_challenger_ativo(self):
        ativos = [{"experiment_key": "outro", "status": "SHADOW"}]
        verdict = r12.exclusivity_verdict(ativos, candidate_key="meu")
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason_code"], r12.CHALLENGER_ALREADY_ACTIVE)
        self.assertEqual(verdict["lab_alternative"], r12.LAB_SEQUENTIAL_ONLY)

    def test_repetir_chamada_e_idempotente(self):
        ativos = [{"experiment_key": "meu", "status": "SHADOW"}]
        verdict = r12.exclusivity_verdict(ativos, candidate_key="meu")
        self.assertTrue(verdict["allowed"])
        self.assertTrue(verdict["idempotent"])

    def test_experimentos_encerrados_nao_ocupam_o_ciclo(self):
        ativos = [{"experiment_key": "velho", "status": "REJECTED"},
                  {"experiment_key": "antigo", "status": "ELIGIBLE"}]
        self.assertTrue(r12.exclusivity_verdict(ativos, candidate_key="meu")["allowed"])

    def test_lifecycle_sem_salto_nem_reabertura(self):
        self.assertTrue(r12.transition_verdict("DRAFT", "OFFLINE_VALIDATED")["allowed"])
        self.assertFalse(r12.transition_verdict("DRAFT", "SHADOW")["allowed"])
        self.assertFalse(r12.transition_verdict("REJECTED", "SHADOW")["allowed"])
        self.assertFalse(r12.transition_verdict("ELIGIBLE", "DRAFT")["allowed"])
        repetida = r12.transition_verdict("SHADOW", "SHADOW")
        self.assertTrue(repetida["allowed"])
        self.assertTrue(repetida["idempotent"])


class CongelamentoEAvanco(unittest.TestCase):
    def test_congela_as_cinco_partes(self):
        verdict = r12.freeze_bundle({"baseline": {"a": 1}, "candidate": {"b": 2},
                                     "config": {"c": 3}, "costs": {"d": 4},
                                     "protections": {"e": 5}})
        self.assertTrue(verdict["frozen"])
        self.assertEqual(len(verdict["bundle_hash"]), 64)
        incompleto = r12.freeze_bundle({"baseline": {"a": 1}})
        self.assertFalse(incompleto["frozen"])
        self.assertEqual(incompleto["reason_code"], r12.FROZEN_BUNDLE_MISSING)
        self.assertIn("costs", incompleto["missing"])

    def _avanco(self, **over):
        payload = dict(frozen_bundle_hash="h", current_bundle_hash="h",
                       champion_hash="c", evaluated_champion_hash="c",
                       open_p03_incidents=0, coverage_pct=95.0)
        payload.update(over)
        return r12.advance_verdict(**payload)

    def test_avanco_limpo(self):
        verdict = self._avanco()
        self.assertTrue(verdict["may_advance"])
        self.assertEqual(verdict["reason_codes"], (r12.OK,))

    def test_drift_config_incidente_e_cobertura_bloqueiam(self):
        self.assertIn(r12.CHAMPION_DRIFT,
                      self._avanco(evaluated_champion_hash="outro")["reason_codes"])
        self.assertIn(r12.CONFIG_CHANGED,
                      self._avanco(current_bundle_hash="outro")["reason_codes"])
        self.assertIn(r12.P03_INCIDENT_OPEN,
                      self._avanco(open_p03_incidents=1)["reason_codes"])
        self.assertIn(r12.COVERAGE_INSUFFICIENT,
                      self._avanco(coverage_pct=50.0)["reason_codes"])

    def test_incidente_nunca_e_limpo_para_passar(self):
        verdict = self._avanco(open_p03_incidents=2)
        self.assertFalse(verdict["may_advance"])
        self.assertFalse(verdict["incident_cleared"])
        self.assertFalse(verdict["quarantine_released"])


class TesteAA(unittest.TestCase):
    def test_aa_limpo(self):
        serie = [0.5, -0.2, 1.0, 0.3]
        verdict = r12.aa_test(serie, list(serie))
        self.assertTrue(verdict["clean"])
        self.assertEqual(verdict["delta"], 0.0)

    def test_diferenca_artificial_reprova(self):
        verdict = r12.aa_test([0.5, 0.5], [0.4, 0.5])
        self.assertFalse(verdict["clean"])
        self.assertEqual(verdict["reason_code"], r12.AA_ARTIFACT)

    def test_amostra_desigual_nao_conclui(self):
        verdict = r12.aa_test([0.5], [0.5, 0.5])
        self.assertEqual(verdict["reason_code"], r12.SAMPLE_INSUFFICIENT)


class GateGoNoGo(unittest.TestCase):
    def test_evidencia_completa_passa_sem_aprovar_live(self):
        verdict = r12.go_no_go(evidence())
        self.assertEqual(verdict["verdict"], "GO_CANDIDATE")
        self.assertEqual(verdict["live_approval"], "UNAVAILABLE")
        self.assertTrue(verdict["requires_human_authorization"])
        self.assertEqual(verdict["test_approval_is_not_simulation_approval"],
                         r12.SIMULATION_NOT_REAL_APPROVAL)

    def test_minimos_do_projeto(self):
        self.assertIn(r12.SAMPLE_INSUFFICIENT,
                      r12.go_no_go(evidence(total_shadow_trades=99))["reason_codes"])
        poucos = evidence(trades_per_playbook={"TREND_PULLBACK": 29,
                                               "TREND_BREAKOUT": 40,
                                               "RANGE_REVERSION": 40})
        self.assertIn(r12.PLAYBOOK_SAMPLE_INSUFFICIENT,
                      r12.go_no_go(poucos)["reason_codes"])
        self.assertIn(r12.DURATION_INSUFFICIENT,
                      r12.go_no_go(evidence(calendar_days=13))["reason_codes"])
        self.assertIn(r12.BUSINESS_DAYS_INSUFFICIENT,
                      r12.go_no_go(evidence(business_days=9))["reason_codes"])
        self.assertIn(r12.COVERAGE_INSUFFICIENT,
                      r12.go_no_go(evidence(coverage_pct=89.0))["reason_codes"])

    def test_amostra_minima_nao_basta(self):
        for chave, valor, motivo in (("net_ev_r", 0.0, r12.EV_INSUFFICIENT),
                                     ("uncertainty_r", 0.5, r12.UNCERTAINTY_TOO_HIGH),
                                     ("drawdown_r", 20.0, r12.DRAWDOWN_EXCEEDED),
                                     ("stability_ratio", 0.1, r12.STABILITY_INSUFFICIENT),
                                     ("operational_failures", 1, r12.OPERATIONAL_FAILURES)):
            verdict = r12.go_no_go(evidence(**{chave: valor}))
            self.assertEqual(verdict["verdict"], "NO_GO", chave)
            self.assertIn(motivo, verdict["reason_codes"], chave)

    def test_duplicata_economica_e_protecao_bloqueiam(self):
        self.assertIn(r12.ECONOMIC_DUPLICATE,
                      r12.go_no_go(evidence(economic_duplicates=1))["reason_codes"])
        self.assertIn(r12.PROTECTION_FAILURE_OPEN,
                      r12.go_no_go(evidence(unresolved_protection_failures=1))["reason_codes"])

    def test_lacuna_essencial_e_discrepancia_de_fidelidade(self):
        self.assertIn(r12.ESSENTIAL_GAP,
                      r12.go_no_go(evidence(essential_gaps=["funding"]))["reason_codes"])
        self.assertIn(r12.FIDELITY_DISCREPANCY,
                      r12.go_no_go(evidence(fidelity_discrepancy_pct=30.0))["reason_codes"])

    def test_ausencia_nao_vira_zero_no_gate(self):
        vazio = r12.go_no_go({})
        self.assertEqual(vazio["verdict"], "NO_GO")
        for motivo in (r12.SAMPLE_INSUFFICIENT, r12.EV_INSUFFICIENT,
                       r12.COVERAGE_INSUFFICIENT, r12.ECONOMIC_DUPLICATE):
            self.assertIn(motivo, vazio["reason_codes"])

    def test_criterios_congelados_tem_hash(self):
        base = r12.GoNoGoCriteria()
        self.assertEqual(base.criteria_hash(), r12.GoNoGoCriteria().criteria_hash())
        self.assertNotEqual(base.criteria_hash(),
                            r12.GoNoGoCriteria(min_total_shadow_trades=200).criteria_hash())
        self.assertEqual(base.min_total_shadow_trades, 100)
        self.assertEqual(base.min_trades_per_playbook, 30)
        self.assertEqual(base.min_business_days, 10)
        self.assertEqual(base.min_calendar_days, 14)
        self.assertEqual(base.min_coverage_pct, 90.0)

    def test_dias_uteis(self):
        self.assertEqual(r12.business_days(START, START + timedelta(days=7)), 5)
        self.assertIsNone(r12.business_days(START, START - timedelta(days=1)))
        self.assertIsNone(r12.business_days("2026-01-05", START))


class CanarioSemAplicar(unittest.TestCase):
    def test_teto_do_projeto_nao_autoriza_subir(self):
        verdict = r12.canary_limits({"LIVE_SIZE_MULT": 0.5, "max_leverage": 3},
                                    {"LIVE_SIZE_MULT": 1.0, "max_leverage": 5})
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["limits"], {"LIVE_SIZE_MULT": 0.5, "max_leverage": 3})

    def test_proposta_acima_do_vigente_e_recusada(self):
        verdict = r12.canary_limits({"LIVE_SIZE_MULT": 0.5}, {"LIVE_SIZE_MULT": 1.0},
                                    {"LIVE_SIZE_MULT": 0.8})
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], r12.LIMIT_INCREASE_FORBIDDEN)
        self.assertEqual(verdict["violations"], ["LIVE_SIZE_MULT"])

    def test_limite_desconhecido_nao_vira_permissao(self):
        verdict = r12.canary_limits({"risk_pct": None}, {"risk_pct": 1.0})
        self.assertFalse(verdict["ok"])
        self.assertIn("risk_pct", verdict["violations"])

    def test_manifest_descreve_e_nao_aplica(self):
        gate = r12.go_no_go(evidence())
        manifest = r12.canary_manifest(
            version="canary-0001",
            diff=[{"file": "services/strategy_core_service.py", "change": "novo módulo"}],
            evidence={"experiment_key": "k"}, gate=gate,
            preconditions=["sem incidente P03 aberto", "cobertura >= 90%"],
            approvers=["operador"],
            limits=r12.canary_limits({"LIVE_SIZE_MULT": 0.5},
                                     {"LIVE_SIZE_MULT": 1.0})["limits"],
            rollback={"plan": "reverter config versionada"})
        self.assertFalse(manifest["applied"])
        self.assertIsNone(manifest["apply_endpoint"])
        self.assertEqual(manifest["live_approval"], "UNAVAILABLE")
        self.assertTrue(manifest["requires_human_authorization"])
        self.assertEqual(manifest["gate"]["criteria_hash"], gate["criteria_hash"])
        self.assertTrue(manifest["diff"])
        self.assertTrue(manifest["approvers"])


class RollbackLocal(unittest.TestCase):
    def _plano(self, **over):
        plano = {"preserves": {name: True for name in r12.PRESERVED_ON_ROLLBACK},
                 "destructive_ddl": False, "restores_invalid_data": False,
                 "reintroduces_security_fix_removal": False}
        plano.update(over)
        return plano

    def test_ensaio_preserva_tudo(self):
        verdict = r12.rollback_rehearsal(self._plano())
        self.assertTrue(verdict["ok"])
        self.assertTrue(verdict["rehearsed"])
        self.assertFalse(verdict["applied"])
        self.assertTrue(verdict["cancels_simulation_only"])

    def test_ddl_destrutivo_e_recusado(self):
        verdict = r12.rollback_rehearsal(self._plano(destructive_ddl=True))
        self.assertIn(r12.DESTRUCTIVE_DDL_FORBIDDEN, verdict["reason_codes"])

    def test_nao_restaura_dado_invalido_nem_falha_de_seguranca(self):
        self.assertIn(r12.INVALID_DATA_RESTORE_FORBIDDEN,
                      r12.rollback_rehearsal(
                          self._plano(restores_invalid_data=True))["reason_codes"])
        self.assertIn(r12.SECURITY_REGRESSION_FORBIDDEN,
                      r12.rollback_rehearsal(
                          self._plano(reintroduces_security_fix_removal=True))["reason_codes"])

    def test_preservacao_incompleta_reprova(self):
        plano = self._plano()
        plano["preserves"] = {**plano["preserves"], "entry_intents": False}
        verdict = r12.rollback_rehearsal(plano)
        self.assertFalse(verdict["ok"])
        self.assertIn("entry_intents", verdict["missing_preservation"])


class RelatorioEFronteiras(unittest.TestCase):
    def test_tres_eixos_separados(self):
        relatorio = r12.status_report(
            implementation={"blocks": "A-G", "state": "LOCAL_VERIFIED"},
            evidence={"shadow_trades": 0, "state": "NOT_COLLECTED"},
            gate=r12.go_no_go({}))
        self.assertIn("implementation_status", relatorio)
        self.assertIn("evidence_status", relatorio)
        self.assertEqual(relatorio["live_approval"]["state"], "UNAVAILABLE")
        self.assertFalse(relatorio["live_approval"]["canary_applied"])
        self.assertTrue(relatorio["live_approval"]["requires_human_authorization"])

    def test_sem_endpoint_de_promocao(self):
        """ROTAS de verdade, não prosa: `main.py` cita `enable-live` só em
        comentário de contrato, então a checagem olha os decoradores."""
        manifest = r12.r12_manifest()
        self.assertEqual(manifest["endpoints_added"], [])
        arvore = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
        rotas = []
        for node in ast.walk(arvore):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                alvo = decorator.func
                metodo = getattr(alvo, "attr", None)
                if metodo not in ("get", "post", "put", "patch", "delete"):
                    continue
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        rotas.append(arg.value)
        self.assertTrue(rotas, "nenhuma rota encontrada — parser quebrado")
        for proibido in ("execute-now", "enable-live", "promote", "clear-quarantine",
                         "activate-live"):
            ofensoras = [rota for rota in rotas if proibido in rota]
            self.assertEqual(ofensoras, [], f"{proibido}: {ofensoras}")
        self.assertNotIn("preselection_experiment_service",
                         (BACKEND / "main.py").read_text(encoding="utf-8"))

    def test_modulo_e_puro(self):
        arvore = ast.parse((BACKEND / "services" /
                            "preselection_experiment_service.py").read_text())
        importados = set()
        for node in ast.walk(arvore):
            if isinstance(node, ast.Import):
                importados.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                importados.add(node.module.split(".")[0])
        self.assertEqual(importados - {"__future__"},
                         {"dataclasses", "datetime", "hashlib", "json", "math", "os",
                          "typing"})

    def test_modo_inativo_por_padrao(self):
        anterior = os.environ.pop(r12.MODE_ENV, None)
        try:
            self.assertEqual(r12.selected_mode(), r12.MODE_INACTIVE)
        finally:
            if anterior is not None:
                os.environ[r12.MODE_ENV] = anterior


if __name__ == "__main__":
    unittest.main()
