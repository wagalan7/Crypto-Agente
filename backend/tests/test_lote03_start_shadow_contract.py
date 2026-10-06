"""L03/A — início GOVERNADO do SHADOW pré-seleção pelo caller REAL.

A rota existente `POST /api/strategy/p05/experiments/{exp_id}/start-shadow`
passa a aceitar um corpo FECHADO (confirm literal, `approval_id` não vazio,
`expected_generation` inteiro) e a encaminhá-lo ao serviço, que despacha POR
TIPO antes do loader legado. Nada aqui aprova, promove ou liga seletor: sem a
aprovação SHADOW exata o início é recusado.
"""
import ast
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from services import preselection_experiment_service as r12
from services import strategy_evidence_service as se

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


if __name__ == "__main__":
    unittest.main()
