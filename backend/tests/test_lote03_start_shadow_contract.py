"""L03/A — início GOVERNADO do SHADOW pré-seleção pelo caller REAL.

A rota existente `POST /api/strategy/p05/experiments/{exp_id}/start-shadow`
passa a aceitar um corpo FECHADO (confirm literal, `approval_id` não vazio,
`expected_generation` inteiro) e a encaminhá-lo ao serviço, que despacha POR
TIPO antes do loader legado. Nada aqui aprova, promove ou liga seletor: sem a
aprovação SHADOW exata o início é recusado.
"""
import ast
import copy
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from services import preselection_experiment_service as r12
from services import strategy_evidence_service as se
from services import operational_governance_service as governance
from services import prospective_shadow_service as prospective
from tests.test_lote03_governance import (CHAMPION, approval_payload,
                                         governance_fixture)

BACKEND = Path(__file__).resolve().parents[1]
ROTA = "/api/strategy/p05/experiments/{exp_id}/start-shadow"


class RotaGovernada(unittest.TestCase):
    """O caller real: a MESMA rota, com corpo fechado e autenticação."""

    def setUp(self):
        fonte = (BACKEND / "main.py").read_text()
        arvore = ast.parse(fonte)
        nomes = {"p05_start_shadow", "_check_admin_token", "_r13_admin_principal"}
        nos = [no for no in arvore.body
               if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef))
               and no.name in nomes]
        self.assertEqual({no.name for no in nos}, nomes)
        app = FastAPI()
        escopo = {"app": app, "Body": Body, "Header": Header, "Optional": Optional,
                  "Dict": Dict, "Any": Any, "os": os, "HTTPException": HTTPException,
                  "log": logging.getLogger("teste-l03a")}
        exec(compile(ast.Module(body=nos, type_ignores=[]), str(BACKEND / "main.py"),
                     "exec"), escopo)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.chamadas = []

        async def falso_start_shadow(exp_id, **extras):
            self.chamadas.append((exp_id, dict(extras)))
            return {"ok": True, "status": "SHADOW", "echo": dict(extras)}

        self.patch_servico = patch.object(se, "start_shadow", falso_start_shadow)
        self.patch_servico.start()
        self.addCleanup(self.patch_servico.stop)
        self.patch_token = patch.dict(os.environ, {"ADMIN_API_TOKEN": "token-local"})
        self.patch_token.start()
        self.addCleanup(self.patch_token.stop)

    def post(self, corpo=None, *, token="token-local", exp_id=7):
        caminho = f"/api/strategy/p05/experiments/{exp_id}/start-shadow"
        cabecalho = {"X-Admin-Token": token} if token is not None else {}
        if corpo is None:
            return self.client.post(caminho, headers=cabecalho)
        return self.client.post(caminho, json=corpo, headers=cabecalho)

    def corpo_valido(self, **mudar):
        base = {"confirm": True, "approval_id": "a" * 64, "expected_generation": 3}
        base.update(mudar)
        return base

    def test_chamada_sem_corpo_mantem_o_contrato_legado(self):
        resposta = self.post()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.chamadas, [(7, {})])

    def test_corpo_governado_viaja_inteiro_para_o_servico(self):
        resposta = self.post(self.corpo_valido())
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.chamadas,
                         [(7, {"approval_id": "a" * 64, "expected_generation": 3})])

    def test_corpo_invalido_nao_chega_ao_servico(self):
        casos = [
            self.corpo_valido(extra=1),                      # chave a mais
            self.corpo_valido(confirm="true"),               # confirm não literal
            self.corpo_valido(confirm=False),
            self.corpo_valido(expected_generation=True),     # bool não é inteiro
            self.corpo_valido(expected_generation="3"),      # string numérica
            self.corpo_valido(approval_id="   "),            # vazio após strip
            self.corpo_valido(approval_id=7),
            {"confirm": True, "approval_id": "a" * 64},      # campo ausente
            {},
        ]
        for corpo in casos:
            with self.subTest(corpo=sorted(corpo)):
                resposta = self.post(corpo)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.json(),
                                 {"ok": False, "blocked": True,
                                  "reason_code": "START_SHADOW_REQUEST_INVALID"})
        self.assertEqual(self.chamadas, [])

    def test_sem_token_nenhum_caminho_executa(self):
        for corpo in (None, self.corpo_valido()):
            with self.subTest(corpo=corpo):
                resposta = self.post(corpo, token=None)
                self.assertIn(resposta.status_code, (200, 401))
                if resposta.status_code == 200:
                    self.assertFalse(resposta.json().get("ok"))
        self.assertEqual(self.chamadas, [])

    def test_falha_do_servico_nao_vaza_stack_nem_segredo(self):
        async def explode(exp_id, **extras):
            raise RuntimeError("segredo-token-local no detalhe")

        with patch.object(se, "start_shadow", explode):
            resposta = self.post(self.corpo_valido())
        self.assertEqual(resposta.status_code, 500)
        detalhe = str(resposta.json())
        self.assertIn("start-shadow indisponível", detalhe)
        for proibido in ("segredo", "token-local", "Traceback", "RuntimeError"):
            self.assertNotIn(proibido, detalhe)


class DespachoPorTipo(unittest.IsolatedAsyncioTestCase):
    """O serviço decide POR TIPO antes de qualquer loader legado."""

    async def asyncSetUp(self):
        self.flags = patch.multiple(se, P05_ANALYTICS_ENABLED=True,
                                    P05_CHALLENGER_SHADOW_ENABLED=True)
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def tipo(self, valor):
        return patch.object(se, "experiment_kind", AsyncMock(return_value=valor))

    async def test_pre_selecao_sem_aprovacao_recusa_e_aponta_o_caminho(self):
        with self.tipo(se.PRE_SELECTION_TYPE), \
                patch.object(se, "start_preselection_shadow",
                             AsyncMock(side_effect=AssertionError("sem aprovação"))):
            resultado = await se.start_shadow(7)
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["reason_code"], r12.TYPE_MISMATCH)
        self.assertEqual(resultado["dispatch_to"], "start_preselection_shadow")
        self.assertEqual(resultado["requires"],
                         ["approval_id", "expected_generation"])
        self.assertEqual(resultado["detail"],
                         se.PRE_SELECTION_START_REQUIRES_APPROVAL)
        self.assertEqual(resultado["live_approval"], "UNAVAILABLE")

    async def test_pre_selecao_com_aprovacao_delega_exatamente(self):
        delegado = AsyncMock(return_value={"ok": True, "status": "SHADOW"})
        with self.tipo(se.PRE_SELECTION_TYPE), \
                patch.object(se, "start_preselection_shadow", delegado):
            resultado = await se.start_shadow(7, approval_id="  " + "b" * 64 + "  ",
                                              expected_generation=2)
        self.assertTrue(resultado["ok"])
        delegado.assert_awaited_once_with(7, approval_id="b" * 64,
                                         expected_generation=2)

    async def test_corpo_de_governanca_nao_se_aplica_ao_legado(self):
        for tipo in ("POST_SELECTION", "P05_1_CONTEXTUAL"):
            with self.subTest(tipo=tipo), self.tipo(tipo), \
                    patch.object(se, "start_preselection_shadow",
                                 AsyncMock(side_effect=AssertionError("tipo errado"))):
                resultado = await se.start_shadow(7, approval_id="c" * 64,
                                                  expected_generation=1)
            self.assertEqual(resultado["reason_code"], se.START_BODY_NOT_APPLICABLE)
            self.assertEqual(resultado["experiment_kind"], tipo)

    async def test_tipo_ilegivel_bloqueia_em_vez_de_adivinhar(self):
        with self.tipo("UNAVAILABLE"), \
                patch.object(se, "start_preselection_shadow",
                             AsyncMock(side_effect=AssertionError("tipo ilegível"))):
            resultado = await se.start_shadow(7)
        self.assertEqual(resultado["reason_code"], "EXPERIMENT_TYPE_UNAVAILABLE")

    async def test_pre_selecao_nunca_alcanca_o_loader_pos_selecao(self):
        with self.tipo(se.PRE_SELECTION_TYPE), \
                patch.object(se, "_load_shadow",
                             AsyncMock(side_effect=AssertionError("loader legado")),
                             create=True):
            recusado = await se.start_shadow(7)
            delegado = AsyncMock(return_value={"ok": True})
            with patch.object(se, "start_preselection_shadow", delegado):
                await se.start_shadow(7, approval_id="d" * 64, expected_generation=0)
        self.assertFalse(recusado["ok"])
        delegado.assert_awaited_once()

    async def test_desligado_bloqueia_antes_de_ler_tipo(self):
        with patch.object(se, "P05_CHALLENGER_SHADOW_ENABLED", False), \
                patch.object(se, "experiment_kind",
                             AsyncMock(side_effect=AssertionError("leitura nova"))):
            resultado = await se.start_shadow(7, approval_id="e" * 64,
                                              expected_generation=1)
        self.assertEqual(resultado["reason_code"], "P05_SHADOW_DISABLED")


class ExperimentoExpiravel(SimpleNamespace):
    """Detecta leitura de atributos ORM depois do rollback, sem banco real."""

    def __getattribute__(self, nome):
        if nome not in ("__dict__", "_expired") and vars(self).get("_expired"):
            raise AssertionError("atributo expirado lido depois do rollback")
        return super().__getattribute__(nome)


class SessoesDeStart:
    """Executa o serviço/governança reais; registra locks e escritas locais."""

    def __init__(self, exp, report):
        self.exp, self.report, self.state = exp, report, None
        self.calls = []
        self.expire_on_rollback = False

    def __call__(self):
        store = self

        class Resultado:
            def __init__(self, valor):
                self.valor = valor

            def scalar_one_or_none(self):
                return self.valor

            def scalars(self):
                return self

            def all(self):
                return self.valor

        class Sessao:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def execute(self, statement, params=None):
                sql = str(statement)
                if "pg_advisory" in sql:
                    store.calls.append(("advisory", next(iter(params.values()))))
                    return Resultado(None)
                wanted = next(iter(statement.compile().params.values()))
                bloqueia = statement._for_update_arg is not None
                if "strategy_experiments" in sql:
                    if "strategy_experiments.status" in sql.split("WHERE", 1)[-1]:
                        store.calls.append(("active", bloqueia))
                        return Resultado([store.exp] if store.exp.status == wanted else [])
                    store.calls.append(("experiment", bloqueia))
                    return Resultado(store.exp if wanted == store.exp.id else None)
                store.calls.append(("state", bloqueia))
                if wanted == governance.STATE_KEY:
                    return Resultado(store.state)
                return Resultado(SimpleNamespace(payload=copy.deepcopy(store.report)))

            def add(self, row):
                store.calls.append(("add",))
                store.state = row

            async def flush(self):
                store.calls.append(("flush",))

            async def commit(self):
                store.calls.append(("commit",))

            async def rollback(self):
                store.calls.append(("rollback",))
                if store.expire_on_rollback:
                    store.exp._expired = True

        return Sessao()


class InicioERepeticaoGovernados(unittest.IsolatedAsyncioTestCase):
    """A repetição real não dispensa autoridade nem troca a identidade original."""

    async def asyncSetUp(self):
        exp, self.report, self.bundle, self.now = governance_fixture()
        self.exp = ExperimentoExpiravel(**vars(exp), shadow_started_at=None)
        self.store = SessoesDeStart(self.exp, self.report)
        self.patches = [
            patch.multiple(se, P05_ANALYTICS_ENABLED=True,
                           P05_CHALLENGER_SHADOW_ENABLED=True),
            patch.object(se, "discover_champion_config", return_value=CHAMPION),
            patch.object(governance, "_ALLOW_TEST_APPROVALS", True),
            patch.object(governance, "_LOCAL_PENDING", False),
            patch.object(governance, "_LOCAL_FENCE", 0),
            patch.object(governance, "_PENDING_FENCES", set()),
            patch.object(governance, "_FAILED_FENCES", set()),
            patch.object(governance.time, "time", return_value=self.now / 1000),
            patch("db.get_session", self.store),
            patch("socket.getaddrinfo", side_effect=AssertionError("rede proibida")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        registrado = await governance.register_bundle(self.store, self.exp.id,
            {"confirm": True, "calibration_study_key": self.report["study_key"],
             "champion_config": CHAMPION}, "TEST_ONLY_OPERATOR")
        self.assertTrue(registrado["ok"], registrado)
        self.bundle = self.exp.decision["operational"]["bundle"]
        self.approval = await self.aprovar()

    async def aprovar(self, *, outra=False):
        payload = approval_payload(self.bundle, self.now)
        if outra:
            payload["validity"]["id"] = "TEST_ONLY:approval-002"
        resultado = await governance.register_approval(self.store, self.exp.id,
            payload, "TEST_ONLY_OPERATOR", test_only=True)
        self.assertTrue(resultado["ok"], resultado)
        return resultado

    async def iniciar(self):
        resultado = await se.start_preselection_shadow(self.exp.id,
            approval_id=self.approval["approval_id"],
            expected_generation=self.approval["generation"])
        self.assertTrue(resultado["ok"], resultado)
        self.assertFalse(resultado.get("idempotent", False))
        self.store.calls.clear()
        return resultado

    def congelado(self):
        return copy.deepcopy({k: v for k, v in vars(self.exp).items()
                              if k != "_expired"})

    def sem_escritas(self, antes):
        self.assertEqual(self.congelado(), antes)
        for proibido in ("add", "flush", "commit"):
            self.assertFalse(any(c[0] == proibido for c in self.store.calls), self.store.calls)

    async def recusar_repeticao(self, approval_id, generation, *, reason=None):
        antes = self.congelado()
        self.store.calls.clear()
        self.store.expire_on_rollback = True
        with patch.object(governance, "assert_authority_in_session",
                          wraps=governance.assert_authority_in_session) as autoridade:
            resultado = await se.start_preselection_shadow(self.exp.id,
                approval_id=approval_id, expected_generation=generation)
        self.assertFalse(resultado["ok"], resultado)
        self.assertTrue(resultado["blocked"])
        self.assertEqual(resultado["status"], se.STATUS_SHADOW)
        if reason is not None:
            self.assertEqual(resultado["reason_code"], reason)
        autoridade.assert_awaited_once()
        self.sem_escritas(antes)
        self.assertIn(("rollback",), self.store.calls)

    async def test_start_e_repeticao_exata_preservam_tudo_sem_escrita(self):
        self.store.calls.clear()
        start = await self.iniciar()
        self.assertTrue(start["prospective_cohort"])
        self.assertEqual(self.exp.shadow_metrics["shadow_approval_id"],
                         self.approval["approval_id"])
        antes = self.congelado()
        with patch.object(governance, "assert_authority_in_session",
                          wraps=governance.assert_authority_in_session) as autoridade:
            resultado = await se.start_preselection_shadow(self.exp.id,
                approval_id=self.approval["approval_id"],
                expected_generation=self.approval["generation"])
        self.assertTrue(resultado["ok"], resultado)
        self.assertTrue(resultado["idempotent"])
        autoridade.assert_awaited_once()
        self.sem_escritas(antes)
        self.assertEqual(self.store.calls[:3],
                         [("advisory", se._P05_SHADOW_LOCK_KEY),
                          ("state", True), ("experiment", True)])

    async def test_aprovacao_ausente_e_inexistente_recusam_repeticao(self):
        await self.iniciar()
        for approval_id in (None, "nao-e-aprovacao-persistida"):
            with self.subTest(approval_id=approval_id):
                await self.recusar_repeticao(approval_id, self.approval["generation"],
                    reason="HUMAN_APPROVAL_MISSING_OR_REVOKED")
                self.exp._expired = False

    async def test_aprovacao_revogada_recusa_repeticao(self):
        await self.iniciar()
        revogada = await governance.revoke_approval(self.store, self.exp.id,
            self.approval["approval_id"], self.approval["generation"],
            "TEST_ONLY_OPERATOR")
        self.assertTrue(revogada["ok"], revogada)
        await self.recusar_repeticao(self.approval["approval_id"], revogada["generation"],
            reason="HUMAN_APPROVAL_MISSING_OR_REVOKED")

    async def test_aprovacao_expirada_recusa_repeticao(self):
        await self.iniciar()
        with patch.object(governance.time, "time", return_value=(self.now + 3600001) / 1000):
            await self.recusar_repeticao(self.approval["approval_id"],
                self.approval["generation"], reason="APPROVAL_EXPIRED_OR_OUTLIVES_ARTIFACT")

    async def test_aprovacao_de_outro_experimento_recusa_repeticao(self):
        await self.iniciar()
        estrangeira = copy.deepcopy(self.exp.decision["operational"]["approvals"][0])
        estrangeira["experiment_id"] += 1
        corpo = {k: estrangeira[k] for k in ("version", "experiment_id", "candidate_hash",
                 "bundle_hash", "payload", "operator", "test_only")}
        estrangeira["approval_id"] = governance.digest(corpo)
        self.exp.decision["operational"]["approvals"].append(estrangeira)
        await self.recusar_repeticao(estrangeira["approval_id"],
            self.approval["generation"], reason="HUMAN_APPROVAL_IDENTITY_INVALID")

    async def test_geracao_obsoleta_recusa_repeticao(self):
        await self.iniciar()
        await self.recusar_repeticao(self.approval["approval_id"],
            self.approval["generation"] - 1, reason="OPERATIONAL_GENERATION_STALE")

    async def test_aprovacao_nova_valida_nao_reassocia_o_start_original(self):
        await self.iniciar()
        nova = await self.aprovar(outra=True)
        await self.recusar_repeticao(nova["approval_id"], nova["generation"],
            reason="SHADOW_START_IDENTITY_MISMATCH")

    async def test_geracao_nova_valida_nao_reassocia_o_start_original(self):
        await self.iniciar()
        nova = await self.aprovar(outra=True)
        await self.recusar_repeticao(self.approval["approval_id"], nova["generation"],
            reason="SHADOW_START_IDENTITY_MISMATCH")

    async def test_legado_sem_aprovacao_original_nao_inventa_metadados(self):
        await self.iniciar()
        del self.exp.shadow_metrics["shadow_approval_id"]
        del self.exp.shadow_metrics["shadow_approval_generation"]
        await self.recusar_repeticao(self.approval["approval_id"],
            self.approval["generation"], reason="SHADOW_START_IDENTITY_MISSING")

    async def test_coorte_de_selecao_ausente_ou_divergente_nao_e_refeita(self):
        await self.iniciar()
        start = copy.deepcopy(self.exp.shadow_metrics[prospective.KEY])
        casos = (None, {**start, "approval_id": "outro-id"},
                 {**start, "generation": start["generation"] + 1})
        for coorte in casos:
            with self.subTest(coorte=coorte):
                self.exp._expired = False
                self.exp.shadow_metrics[prospective.KEY] = coorte
                await self.recusar_repeticao(self.approval["approval_id"],
                    self.approval["generation"])

    async def test_management_only_repete_sem_fabricar_coorte_de_selecao(self):
        # A fixture oficial cobre seleção; o escopo de gestão é isolado aqui
        # para preservar o branch sem ampliar a governança de seleção.
        vinculo = se._frozen_study_of(self.exp)
        gestao = {**vinculo, "contract": {**vinculo["contract"],
                                        "comparison_scope": "MANAGEMENT_ONLY"}}
        autoridade = AsyncMock(return_value={"ok": True,
                                            "generation": self.approval["generation"]})
        with patch.object(se, "_frozen_study_of", return_value=gestao), \
                patch.object(governance, "assert_authority_in_session", autoridade):
            start = await self.iniciar()
            self.assertFalse(start["prospective_cohort"])
            self.assertNotIn(prospective.KEY, self.exp.shadow_metrics)
            antes = self.congelado()
            autoridade.reset_mock()
            resultado = await se.start_preselection_shadow(self.exp.id,
                approval_id=self.approval["approval_id"],
                expected_generation=self.approval["generation"])
            self.assertTrue(resultado["ok"], resultado)
            self.assertTrue(resultado["idempotent"])
            autoridade.assert_awaited_once()
            self.sem_escritas(antes)


if __name__ == "__main__":
    unittest.main()
