"""Convivência manual/bot — identidade, guard de propriedade e margem real.

Unidades PURAS e caminhos reais sem banco: complementam (não substituem) o
harness `tests/pg_integration_manual_coexistence.py`, que exercita persistência,
constraints, ciclo oficial e concorrência em PostgreSQL real.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as bss            # noqa: E402
from services import entry_intent_service as intents          # noqa: E402
from services import manual_position_service as mps           # noqa: E402
from services import trade_manager_service as tms             # noqa: E402

ESCOPO = "c" * 64
ALFA = "ALFA/USDT:USDT"


def impressao(**mudancas):
    base = dict(account_scope=ESCOPO, exchange="binance", market="usdm_futures",
                symbol=ALFA, side="Buy", position_side="BOTH", qty="2.5",
                entry_price="100", update_time_ms=1_770_000_000_000,
                contract_version="MANUAL_ACK_V1")
    base.update(mudancas)
    return mps.position_fingerprint(**base)


class IdentidadeDaPosicao(unittest.TestCase):
    """Mark price/P&L fora; lado, perna, qty, entrada e versão temporal dentro."""

    def test_formas_de_simbolo_normalizam_para_a_mesma_chave(self):
        for bruto in ("BTC/USDT:USDT", "BTCUSDT", "BTC-USDT-USDT", "btc/usdt:usdt"):
            self.assertEqual(mps.symbol_key(bruto), "BTC/USDT", bruto)
        # Quote DIFERENTE não colapsa.
        self.assertNotEqual(mps.symbol_key("BTCUSDC"), mps.symbol_key("BTCUSDT"))

    def test_mark_price_e_pnl_nao_mudam_a_identidade(self):
        # Não existem parâmetros de mark/pnl na função: mudá-los na exchange
        # não altera o fingerprint calculado a partir dos campos de identidade.
        self.assertEqual(impressao(), impressao())

    def test_decimais_canonicos(self):
        self.assertEqual(impressao(qty="2.50", entry_price="100.000"), impressao())
        self.assertEqual(impressao(qty=Decimal("2.5")), impressao())

    def test_parcelas_invalidas_nao_geram_identidade(self):
        casos = {
            "qty_bool": {"qty": True},
            "qty_nan": {"qty": float("nan")},
            "entrada_infinita": {"entry_price": float("inf")},
            "qty_zero": {"qty": 0},
            "entrada_negativa": {"entry_price": -1},
            "lado_ausente": {"side": None},
            "lado_invalido": {"side": "talvez"},
            "perna_ausente": {"position_side": None},
            "perna_invalida": {"position_side": "QUALQUER"},
            "sem_update_time": {"update_time_ms": None},
            "update_time_zero": {"update_time_ms": 0},
            "update_time_bool": {"update_time_ms": True},
            "sem_conta": {"account_scope": ""},
        }
        for nome, mudanca in casos.items():
            self.assertIsNone(impressao(**mudanca), nome)

    def test_qualquer_parcela_diferente_muda_a_identidade(self):
        original = impressao()
        for nome, mudanca in (("qty", {"qty": "1.0"}),
                              ("lado", {"side": "Sell"}),
                              ("perna", {"position_side": "LONG"}),
                              ("entrada", {"entry_price": "101"}),
                              ("versao", {"update_time_ms": 1_770_000_000_001}),
                              ("conta", {"account_scope": "d" * 64}),
                              ("simbolo", {"symbol": "BETA/USDT:USDT"}),
                              ("contrato", {"contract_version": "MANUAL_ACK_V2"})):
            self.assertNotEqual(impressao(**mudanca), original, nome)

    def test_campo_ausente_da_exchange_nao_vira_padrao(self):
        self.assertIsNone(bss._explicit_position_side(None))
        self.assertIsNone(bss._explicit_position_side("qualquer"))
        self.assertEqual(bss._explicit_position_side("short"), "SHORT")
        self.assertIsNone(bss._exchange_update_time_ms(None))
        self.assertIsNone(bss._exchange_update_time_ms(0))
        self.assertIsNone(bss._exchange_update_time_ms(True))
        self.assertEqual(bss._exchange_update_time_ms("1770000000000"), 1_770_000_000_000)


class GuardDePropriedade(unittest.IsolatedAsyncioTestCase):
    """Fail-closed: dúvida nunca libera mutação."""

    async def _guard(self, registro, symbol=ALFA):
        async def fake():
            return registro
        with patch.object(mps, "active_acknowledgements", fake):
            return await mps.ownership_guard(symbol, action="entry")

    async def test_sem_reconhecimento_libera(self):
        v = await self._guard({"ok": True, "reason_code": "REGISTRY_READ", "acks": []})
        self.assertTrue(v["allowed"])

    async def test_simbolo_reconhecido_bloqueia_nos_dois_lados(self):
        registro = {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [{"id": 1, "symbol": ALFA, "side": "buy"}]}
        for alvo in (ALFA, "ALFAUSDT", "ALFA-USDT-USDT"):
            v = await self._guard(registro, alvo)
            self.assertFalse(v["allowed"], alvo)
            self.assertEqual(v["reason_code"], mps.GUARD_MANUAL_SYMBOL)

    async def test_outro_simbolo_continua_liberado(self):
        registro = {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [{"id": 1, "symbol": ALFA, "side": "buy"}]}
        v = await self._guard(registro, "BETA/USDT:USDT")
        self.assertTrue(v["allowed"])

    async def test_registro_ilegivel_bloqueia(self):
        v = await self._guard({"ok": False, "reason_code": mps.GUARD_REGISTRY_UNAVAILABLE,
                               "acks": []})
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason_code"], mps.GUARD_REGISTRY_UNAVAILABLE)

    async def test_simbolo_desconhecido_com_reconhecimento_ativo_bloqueia(self):
        registro = {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [{"id": 1, "symbol": ALFA, "side": "buy"}]}
        v = await self._guard(registro, None)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason_code"], mps.GUARD_SYMBOL_UNKNOWN)


class TransporteNaoMutaSimboloManual(unittest.IsolatedAsyncioTestCase):
    """O bloqueio acontece ANTES da primeira mutação — inclusive alavancagem."""

    async def asyncSetUp(self):
        self.enviados = []

        async def fake_signed(method, path, params=None, **kwargs):
            self.enviados.append((method, path))
            return {"ok": True, "result": {}}

        async def registro():
            return {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [{"id": 1, "symbol": ALFA, "side": "buy"}]}

        self._p = [patch.object(bss, "_signed_request", fake_signed),
                   patch.object(mps, "active_acknowledgements", registro),
                   patch.object(bss, "_round_qty", AsyncMock(return_value=1.0))]
        for item in self._p:
            item.start()

    async def asyncTearDown(self):
        for item in self._p:
            item.stop()

    async def test_nenhuma_mutacao_no_simbolo_manual(self):
        chamadas = {
            "set_leverage": await bss.set_leverage("ALFAUSDT", 10),
            "place_order": await bss.place_order(ALFA, "BUY", 1.0, order_type="Market",
                                                 leverage=10, entry_preflight=None),
            "protecao": await bss.place_protection_orders(ALFA, "Buy", 1.0,
                                                          stop_loss=90.0, tp1=110.0),
            "cancel_order": await bss.cancel_order(ALFA, order_id="1"),
            "cancel_algo": await bss.cancel_algo_order("A1", symbol=ALFA),
        }
        for nome, res in chamadas.items():
            self.assertFalse(res.get("ok"), nome)
            self.assertTrue(res.get("manual_ownership_blocked"), nome)
        self.assertEqual(self.enviados, [])

    async def test_cancel_sem_simbolo_com_reconhecimento_ativo_bloqueia(self):
        res = await bss.cancel_algo_order("A1")
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.enviados, [])

    async def test_outro_simbolo_continua_mutando(self):
        res = await bss.set_leverage("BETAUSDT", 5)
        self.assertTrue(res.get("ok"))
        self.assertEqual(self.enviados, [("POST", "/fapi/v1/leverage")])


class PosicaoDoTradeManager(unittest.IsolatedAsyncioTestCase):
    """O manager não pode escolher a PRIMEIRA posição do símbolo."""

    def _resposta(self, linhas):
        async def fake(symbol=None, force=False, **kwargs):
            return {"ok": True, "positions": linhas}
        return fake

    async def test_lado_divergente_nao_e_a_posicao_do_trade(self):
        from services import exchange_service as exs
        linhas = [{"symbol": "ALFAUSDT", "side": "Sell", "size": 3.0,
                   "entry_price": 100.0, "position_side": "SHORT"}]
        with patch.object(exs, "get_positions", self._resposta(linhas)):
            qty, _ = await tms._fetch_exchange_position(ALFA, side="long")
        self.assertEqual(qty, 0.0)

    async def test_pernas_ambiguas_viram_leitura_incerta(self):
        from services import exchange_service as exs
        linhas = [{"symbol": "ALFAUSDT", "side": "Buy", "size": 3.0,
                   "entry_price": 100.0, "position_side": "LONG"},
                  {"symbol": "ALFAUSDT", "side": "Buy", "size": 1.0,
                   "entry_price": 90.0, "position_side": "BOTH"}]
        with patch.object(exs, "get_positions", self._resposta(linhas)):
            qty, _ = await tms._fetch_exchange_position(ALFA, side="long")
        self.assertIsNone(qty)

    async def test_lado_ausente_nao_vira_compra(self):
        from services import exchange_service as exs
        linhas = [{"symbol": "ALFAUSDT", "side": None, "size": 3.0,
                   "entry_price": 100.0, "position_side": "BOTH"}]
        with patch.object(exs, "get_positions", self._resposta(linhas)):
            qty, _ = await tms._fetch_exchange_position(ALFA, side="long")
        self.assertIsNone(qty)

    async def test_perna_correta_e_encontrada(self):
        from services import exchange_service as exs
        linhas = [{"symbol": "ALFAUSDT", "side": "Buy", "size": 3.0,
                   "entry_price": 100.0, "position_side": "LONG"}]
        with patch.object(exs, "get_positions", self._resposta(linhas)):
            qty, entrada = await tms._fetch_exchange_position(ALFA, side="long")
        self.assertEqual((qty, entrada), (3.0, 100.0))


class MargemRealDaConta(unittest.TestCase):
    """Limite INDEPENDENTE do orçamento nominal do bot."""

    def _gate(self, **kwargs):
        base = dict(available_usd=1_000.0, required_usd=100.0,
                    as_of_ms=1_770_000_000_000, max_age_s=30.0, complete=True)
        base.update(kwargs)
        return intents.MarginGate(**base)

    def _motivo(self, gate, pendente=0.0, agora=1_770_000_000_000):
        return intents._margin_reason(gate, pendente, now_ms=agora)

    def test_sem_gate_nao_ha_veredicto(self):
        self.assertIsNone(self._motivo(None))

    def test_cabe_quando_ha_saldo(self):
        self.assertIsNone(self._motivo(self._gate()))

    def test_reservas_de_outras_intencoes_entram_na_conta(self):
        self.assertIsNone(self._motivo(self._gate(required_usd=400.0), pendente=600.0))
        self.assertEqual(self._motivo(self._gate(required_usd=401.0), pendente=600.0),
                         "INSUFFICIENT_FREE_MARGIN")

    def test_desconhecido_nunca_vira_zero(self):
        self.assertEqual(self._motivo(self._gate(available_usd=None)),
                         "FREE_MARGIN_UNKNOWN")
        self.assertEqual(self._motivo(self._gate(complete=False)),
                         "FREE_MARGIN_UNKNOWN")
        self.assertEqual(self._motivo(self._gate(required_usd=float("nan"))),
                         "FREE_MARGIN_UNKNOWN")
        self.assertEqual(self._motivo(self._gate(as_of_ms=None)),
                         "FREE_MARGIN_UNKNOWN")

    def test_carteira_velha_nao_vale_como_atual(self):
        self.assertEqual(
            self._motivo(self._gate(), agora=1_770_000_000_000 + 31_000),
            "FREE_MARGIN_STALE")

    def test_margem_proposta_usa_notional_sobre_alavancagem(self):
        from services import shadow_trade_service as sts
        valor = sts._proposed_margin_usd(entry=100.0, qty=2.0, leverage=5)
        # 200 de notional / 5 = 40, mais 2×5bps de custo conservador = 0.2
        self.assertAlmostEqual(valor, 40.2, places=6)
        self.assertIsNone(sts._proposed_margin_usd(entry=0.0, qty=2.0, leverage=5))
        self.assertIsNone(sts._proposed_margin_usd(entry=float("nan"), qty=2.0,
                                                   leverage=5))
        # Alavancagem ausente é tratada como 1× (mais margem exigida, nunca menos).
        self.assertGreater(sts._proposed_margin_usd(entry=100.0, qty=2.0, leverage=None),
                           valor)


class ContratoDaApiAdministrativa(unittest.TestCase):
    """Schema ESTRITO e auth — sem endpoint genérico e sem segredo no retorno."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.main = main

    def test_confirmacao_precisa_ser_booleano_literal(self):
        from pydantic import ValidationError
        Req = self.main.ManualPositionAckRequest
        ok = Req(symbol=ALFA, fingerprint="a" * 64, confirm=True)
        self.assertIs(ok.confirm, True)
        for valor in ("true", 1, "1", "on", 0):
            with self.assertRaises(ValidationError, msg=str(valor)):
                Req(symbol=ALFA, fingerprint="a" * 64, confirm=valor)

    def test_payload_malformado_e_recusado(self):
        from pydantic import ValidationError
        Req = self.main.ManualPositionAckRequest
        with self.assertRaises(ValidationError):
            Req(symbol=ALFA, fingerprint="a" * 64, confirm=True, extra="x")
        with self.assertRaises(ValidationError):
            Req(fingerprint="a" * 64, confirm=True)
        with self.assertRaises(ValidationError):
            Req(symbol=ALFA, confirm=True)

    def test_auth_administrativa_exigida(self):
        import os
        anterior = os.environ.get("ADMIN_API_TOKEN")
        os.environ["ADMIN_API_TOKEN"] = "segredo-de-teste"
        try:
            self.assertIsNotNone(self.main._check_admin_token(None))
            self.assertIsNotNone(self.main._check_admin_token("errado"))
            self.assertIsNone(self.main._check_admin_token("segredo-de-teste"))
            recusa = self.main._check_admin_token("errado")
            self.assertNotIn("segredo-de-teste", str(recusa))
        finally:
            if anterior is None:
                os.environ.pop("ADMIN_API_TOKEN", None)
            else:
                os.environ["ADMIN_API_TOKEN"] = anterior

    def test_nao_existe_endpoint_generico_novo(self):
        rotas = [getattr(r, "path", "") for r in self.main.app.routes]
        manuais = [r for r in rotas if "manual-positions" in r]
        self.assertEqual(sorted(manuais),
                         ["/api/admin/manual-positions/acknowledge",
                          "/api/admin/manual-positions/candidates"])
        for proibido in ("manual-positions/resolve", "manual-positions/clear-pause",
                         "manual-positions/enable-live", "manual-positions/execute"):
            self.assertNotIn(proibido, " ".join(rotas))


class RegistroIndisponivel(unittest.IsolatedAsyncioTestCase):
    """Registro inacessível não vira lista vazia nem exceção solta."""

    async def test_erro_de_leitura_devolve_ok_false(self):
        import db

        def sessao_quebrada():
            raise RuntimeError("banco fora")

        with patch.object(db, "DB_ENABLED", True), \
                patch.object(db, "get_session", sessao_quebrada):
            registro = await mps.active_acknowledgements()
        self.assertFalse(registro["ok"])
        self.assertEqual(registro["reason_code"], mps.GUARD_REGISTRY_UNAVAILABLE)
        self.assertEqual(registro["acks"], [])

    async def test_banco_desligado_nao_isenta_ninguem(self):
        import db
        with patch.object(db, "DB_ENABLED", False):
            registro = await mps.active_acknowledgements()
        self.assertTrue(registro["ok"])
        self.assertEqual(registro["acks"], [])

    async def test_guard_traduz_erro_em_bloqueio(self):
        async def quebrado():
            return {"ok": False, "reason_code": mps.GUARD_REGISTRY_UNAVAILABLE,
                    "acks": []}
        with patch.object(mps, "active_acknowledgements", quebrado):
            v = await mps.ownership_guard("QUALQUER/USDT:USDT", action="entry")
        self.assertFalse(v["allowed"])


if __name__ == "__main__":
    unittest.main()
