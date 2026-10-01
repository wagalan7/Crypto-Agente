"""Fechamento manual/BOT — aceites que a baseline `20580139` não cumpria.

Casos PUROS e de transporte (sem banco). Os casos que exigem persistência,
locks e duas conexões vivem em `tests/pg_integration_manual_closure.py`.

Cobertura aqui:

- **T7** redução/proteção de OUTRO símbolo não depende de prova manual alheia
  nem do token financeiro de nova entrada; o payload contraditório é recusado
  ANTES de qualquer mutação; o símbolo manual bloqueia os dois lados.
- Predicado completo da prova de validação (estado, revisão, conta, época,
  contrato, escopo, carimbo inteiro positivo e não futuro).
- Classificação da redução derivada UMA vez e usada em todos os guards.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as bss            # noqa: E402
from services import manual_position_service as mps           # noqa: E402

ESCOPO = "d" * 64
GAMA = "GAMA/USDT:USDT"
DELTA = "DELTA/USDT:USDT"


def ack(**extra):
    """Reconhecimento BLOQUEANTE com prova completa e fresca por padrão."""
    base = {"id": 1, "symbol": GAMA, "side": "buy", "state": "ACTIVE",
            "account_scope": ESCOPO, "exchange": "binance",
            "market": "usdm_futures", "contract_version": "MANUAL_ACK_V1",
            "revision": 3, "validated_revision": 3,
            "validated_generation": 7, "validation_scope": "ACCOUNT",
            "validation_account": ESCOPO, "validated_at_ms": mps._now_ms()}
    base.update(extra)
    return base


class ProvaDeValidacaoCompleta(unittest.TestCase):
    """Idade sozinha não é prova: estado, revisão, conta e época contam."""

    def _fresca(self, linha, *, account_scope=ESCOPO, generation=7):
        return mps.proof_is_valid(linha, account_scope=account_scope,
                                  manual_generation=generation)

    def test_prova_completa_e_valida(self):
        self.assertTrue(self._fresca(ack()))

    def test_estado_nao_ativo_nunca_tem_prova_autorizadora(self):
        for estado in ("INVALIDATED", "WAITING_ORDERS", "CLOSED", "SUPERSEDED"):
            self.assertFalse(self._fresca(ack(state=estado)), estado)

    def test_revisao_divergente_invalida(self):
        self.assertFalse(self._fresca(ack(revision=4)))
        self.assertFalse(self._fresca(ack(validated_revision=None)))

    def test_conta_divergente_invalida(self):
        self.assertFalse(self._fresca(ack(validation_account="e" * 64)))
        self.assertFalse(self._fresca(ack(), account_scope="e" * 64))

    def test_epoca_manual_divergente_invalida(self):
        self.assertFalse(self._fresca(ack(validated_generation=6)))
        self.assertFalse(self._fresca(ack(validated_generation=None)))

    def test_contrato_divergente_invalida(self):
        self.assertFalse(self._fresca(ack(contract_version="MANUAL_ACK_V2")))

    def test_carimbo_invalido_ou_futuro_invalida(self):
        for valor in (None, 0, -1, True, "ontem",
                      mps._now_ms() + 120_000):
            self.assertFalse(self._fresca(ack(validated_at_ms=valor)), str(valor))

    def test_idade_alem_do_limite_invalida(self):
        velho = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 10) * 1000)
        self.assertFalse(self._fresca(ack(validated_at_ms=velho)))

    def test_escopo_symbol_vale_para_o_proprio_ack(self):
        self.assertTrue(self._fresca(ack(validation_scope="SYMBOL",
                                         validation_symbol=GAMA)))
        self.assertFalse(self._fresca(ack(validation_scope="SYMBOL",
                                          validation_symbol=DELTA)))

    def test_escopo_desconhecido_invalida(self):
        self.assertFalse(self._fresca(ack(validation_scope="QUALQUER")))


class ClassificacaoDaReducao(unittest.TestCase):
    """`reduce_only` é bool LITERAL e a classificação é derivada UMA vez."""

    def test_bool_literal_classifica_reducao(self):
        self.assertEqual(bss.classify_order_action(reduce_only=True), "reduce_only")
        self.assertEqual(bss.classify_order_action(reduce_only=False), "place_order")

    def test_string_ou_objeto_nao_ganha_isencao(self):
        for valor in ("true", 1, "1", [1], {"x": 1}, object()):
            self.assertEqual(bss.classify_order_action(reduce_only=valor),
                             "place_order", repr(valor))


class ReducaoDeOutroSimboloNaoDependeDeProvaAlheia(unittest.IsolatedAsyncioTestCase):
    """T7 — pelo caller real `place_order` até o cliente HTTP falso."""

    async def asyncSetUp(self):
        self.enviados = []
        velho = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 10) * 1000)
        self.registro = {"ok": True, "reason_code": "REGISTRY_READ",
                         "acks": [ack(validated_at_ms=velho)]}

        async def registro():
            return dict(self.registro)

        async def request(method, url):
            self.enviados.append((method, url.split("?")[0]))
            return SimpleNamespace(
                status_code=200, headers={},
                json=lambda: {"orderId": 1, "status": "FILLED",
                              "executedQty": "1", "avgPrice": "100"})

        self._p = [patch.object(mps, "active_acknowledgements", registro), \
                patch.object(mps, "current_account_scope",
                             return_value=ESCOPO),
                   patch.object(bss, "is_configured", return_value=True),
                   patch.object(bss, "_ban_until_ms", 0),
                   patch.object(bss, "_throttle_until_ms", 0),
                   patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)),
                   patch.object(bss, "_build_signed_url",
                                return_value="https://sintetico.invalido/fapi/v1/order"),
                   patch.object(bss, "_get_client",
                                return_value=SimpleNamespace(request=request))]
        for item in self._p:
            item.start()

    async def asyncTearDown(self):
        for item in self._p:
            item.stop()

    def _posts_de_ordem(self):
        """Só o POST da ORDEM conta (a rota de redução também relê posição)."""
        return [m for m, url in self.enviados
                if m == "POST" and url.endswith("/fapi/v1/order")]

    async def test_reducao_em_outro_simbolo_chega_ao_transporte(self):
        res = await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                    reduce_only=True, leverage=None,
                                    client_order_id="cw-reducao")
        self.assertNotEqual(res.get("reason_code"), mps.GUARD_PROOF_STALE)
        self.assertEqual(len(self._posts_de_ordem()), 1, str(res)[:200])

    async def test_entrada_nova_em_outro_simbolo_continua_bloqueada(self):
        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    entry_preflight=None, leverage=None,
                                    client_order_id="cw-entrada")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason_code"), mps.GUARD_PROOF_STALE)
        self.assertEqual(self.enviados, [])

    async def test_simbolo_manual_bloqueia_os_dois_lados(self):
        for lado, reduz in (("SELL", True), ("BUY", False)):
            self.enviados.clear()
            res = await bss.place_order(GAMA, lado, 1.0, order_type="Market",
                                        reduce_only=reduz, leverage=None,
                                        client_order_id="cw-manual")
            self.assertFalse(res.get("ok"), f"{lado}/{reduz}")
            self.assertEqual(res.get("reason_code"), mps.GUARD_MANUAL_SYMBOL)
            self.assertEqual(self.enviados, [])

    async def test_payload_contraditorio_e_recusado_antes_de_mutar(self):
        for extra in ({"leverage": 5}, {"stop_loss": 90.0}, {"tp1": 120.0},
                      {"take_profit": 130.0}):   # tp2 == take_profit na API
            self.enviados.clear()
            res = await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                        reduce_only=True,
                                        client_order_id="cw-contraditorio",
                                        **extra)
            self.assertFalse(res.get("ok"), str(extra))
            self.assertEqual(res.get("reason_code"),
                             "EXEC_REDUCE_ONLY_CONTRADICTORY", str(extra))
            self.assertEqual(self.enviados, [], str(extra))

    async def test_reducao_carrega_reduce_only_no_payload(self):
        capturado = []

        def captura(path, params):
            capturado.append({"path": path, "params": dict(params or {})})
            return "https://sintetico.invalido" + path

        with patch.object(bss, "_build_signed_url", captura):
            await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                  reduce_only=True, leverage=None,
                                  client_order_id="cw-payload")
        ordens = [c for c in capturado if c["path"].endswith("/fapi/v1/order")
                  and c["params"].get("type") == "MARKET"]
        self.assertTrue(ordens, str(capturado)[:200])
        self.assertEqual(str(ordens[0]["params"].get("reduceOnly")).lower(), "true")
        self.assertIsNone(ordens[0]["params"].get("leverage"),
                          "redução não altera alavancagem")

    async def test_registro_ilegivel_nega_a_reducao(self):
        async def quebrado():
            return {"ok": False, "reason_code": mps.GUARD_REGISTRY_UNAVAILABLE,
                    "acks": []}
        with patch.object(mps, "active_acknowledgements", quebrado), \
                patch.object(mps, "current_account_scope",
                             return_value=ESCOPO):
            res = await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                        reduce_only=True, leverage=None,
                                        client_order_id="cw-ilegivel")
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.enviados, [])

    async def test_conta_bloqueada_nao_impede_reducao_valida(self):
        """Falha de validação de conta bloqueia ENTRADA, não redução BOT."""
        async def bloqueada(*args, **kwargs):
            return {"blocked": True, "reason_code": mps.GUARD_ACCOUNT_BLOCKED}

        with patch.object(mps, "account_validation_state", bloqueada):
            reducao = await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                            reduce_only=True, leverage=None,
                                            client_order_id="cw-red-bloq")
            self.assertEqual(len(self._posts_de_ordem()), 1, str(reducao)[:200])
            self.enviados.clear()
            entrada = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                            entry_preflight=None, leverage=None,
                                            client_order_id="cw-ent-bloq")
        self.assertFalse(entrada.get("ok"))
        self.assertEqual(self.enviados, [])


class ProtecaoComMutationGuardNaFronteira(unittest.IsolatedAsyncioTestCase):
    """O `mutation_guard` de lease também roda DEPOIS do throttle."""

    async def test_guard_de_lease_nega_no_ponto_final(self):
        enviados = []

        async def request(method, url):
            enviados.append(method)
            return SimpleNamespace(status_code=200, headers={},
                                   json=lambda: {"algoId": "x"})

        async def registro():
            return {"ok": True, "reason_code": "REGISTRY_READ", "acks": []}

        chamadas = {"n": 0}

        async def guard_que_expira():
            chamadas["n"] += 1
            # Vivo no guard inicial; expirado na fronteira pós-throttle.
            return chamadas["n"] < 2

        with patch.object(mps, "active_acknowledgements", registro), \
                patch.object(mps, "current_account_scope",
                             return_value=ESCOPO), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", 0), \
                patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_round_price", AsyncMock(side_effect=lambda _s, p: p)), \
                patch.object(bss, "_build_signed_url",
                             return_value="https://sintetico.invalido/x"), \
                patch.object(bss, "_get_client",
                             return_value=SimpleNamespace(request=request)):
            res = await bss.place_protection_orders(
                DELTA, "Buy", 1.0, stop_loss=90.0,
                mutation_guard=guard_que_expira)
        self.assertFalse(res.get("sl_ok"))
        self.assertEqual(enviados, [], "nenhum POST com lease expirado")
        self.assertGreaterEqual(chamadas["n"], 2,
                                "o guard precisa rodar também na fronteira")


if __name__ == "__main__":
    unittest.main()
