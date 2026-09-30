"""Correção integrada da convivência manual/bot — defeitos T1–T4 e T6.

Testes pelos CALLERS reais, com exchange/registro sintéticos nas bordas. Os
casos T5 (transferência de margem por geração) e T7 (reserva sob a lock
decisória) exigem PostgreSQL e vivem em
`tests/pg_integration_manual_margin.py`.

Cada teste afirma o COMPORTAMENTO correto e falhava na baseline `69fd090a`:

- T1 uma consulta filtrada de ALFA encerrava o reconhecimento de BETA;
- T2 flat encerrava o reconhecimento sem consultar ordens restantes;
- T3 sem incidente aberto, o ciclo oficial parava de revalidar;
- T4 carteira em cache de 600s aparecia como `live`, idade zero;
- T6 reconhecimento criado durante o throttle ainda deixava o POST sair.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import binance_signed_service as bss            # noqa: E402
from services import entry_intent_service as intents          # noqa: E402
from services import exchange_service as exs                  # noqa: E402
from services import execution_reconciliation_service as ers  # noqa: E402
from services import manual_position_service as mps           # noqa: E402
from services import shadow_trade_service as sts              # noqa: E402

ESCOPO = "c" * 64


def posicao(symbol, **mudancas):
    base = {"symbol": symbol, "side": "Buy", "size": 2.5, "entry_price": 100,
            "position_side": "BOTH", "update_time_ms": 1_770_000_000_000}
    base.update(mudancas)
    return mps.normalize_positions([base])[0][0]


def registro_ack(numero, symbol, **mudancas):
    p = posicao(symbol)
    descricao = mps.describe_candidate(p, account_scope=ESCOPO)
    linha = {**descricao, "id": numero, "account_scope": ESCOPO,
             "exchange": "binance", "market": "usdm_futures",
             "state": "ACTIVE", "symbol": mps.canonical_symbol(symbol),
             # Prova de validação FRESCA por padrão: é o estado normal com o
             # ciclo oficial rodando. Os testes de T3 sobrescrevem com uma
             # prova vencida de propósito.
             "validated_at_ms": mps._now_ms(), "revision": 1}
    linha.update(mudancas)
    return linha


class RegistroFalso:
    """Registro em memória com a MESMA semântica do persistido."""

    def __init__(self, linhas):
        self.linhas = [dict(linha) for linha in linhas]
        self.encerrados = []

    async def active(self):
        bloqueantes = set(getattr(mps, "BLOCKING_STATES", ("ACTIVE",)))
        return {"ok": True, "reason_code": "REGISTRY_READ",
                "acks": [dict(linha) for linha in self.linhas
                         if linha["state"] in bloqueantes]}

    async def end(self, ids, *, state_final, reason=None, reasons=None, **kwargs):
        return await self.transition([(i, None, state_final, reason) for i in ids])

    async def transition(self, transicoes):
        """Mesmo contrato de `_transition_acks`, com CAS de revisão."""
        alterados, por_estado, obsoletos = [], [], []
        for identificador, revisao, estado_final, _motivo in transicoes:
            for linha in self.linhas:
                if linha["id"] != identificador:
                    continue
                if revisao is not None and int(linha.get("revision") or 0) != int(revisao):
                    obsoletos.append(identificador)
                    break
                linha["state"] = estado_final
                linha["revision"] = int(linha.get("revision") or 0) + 1
                alterados.append(identificador)
                por_estado.append((identificador, estado_final))
                break
        self.encerrados.append((tuple(i for i, *_ in transicoes), "TRANSITION"))
        return {"ok": True, "changed": alterados, "by_state": por_estado,
                "stale": obsoletos}

    async def proof(self, ids, *, account_scope, scope, validated_at_ms):
        for linha in self.linhas:
            if linha["id"] in ids:
                linha["validated_at_ms"] = validated_at_ms
                linha["validation_scope"] = scope
        return {"ok": True, "updated": list(ids)}

    def estado(self, numero):
        for linha in self.linhas:
            if linha["id"] == numero:
                return linha["state"]
        return None


class T1ObservacaoParcial(unittest.IsolatedAsyncioTestCase):
    """Uma leitura de ALFA não pode encerrar o reconhecimento de BETA."""

    async def test_consulta_de_alfa_nao_encerra_beta(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT"),
                                  registro_ack(2, "BETAUSDT")])

        async def observar(symbol=None):
            # A resposta é FILTRADA: BETA nem foi consultada.
            return {"ok": True, "reason_code": "POSITIONS_FRESH",
                    "positions": [posicao("ALFAUSDT")],
                    "observed_at_ms": mps._now_ms(),
                    "scope": "SYMBOL", "symbol": mps.canonical_symbol(symbol),
                    "complete": True}

        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "_transition_acks", registro.transition), \
                patch.object(mps, "record_validation_proof", registro.proof), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "observe_positions", observar):
            desfecho = await ers._manual_ack_outcome({"symbol": "ALFA/USDT:USDT"})
            guarda_beta = await mps.ownership_guard("BETAUSDT", action="entry")

        self.assertEqual(desfecho["state"], ers.State.MANUAL_ACKNOWLEDGED,
                         "ALFA reconhecida continua reconhecida")
        self.assertEqual(registro.estado(2), "ACTIVE",
                         "BETA não participou da consulta e não pode ser encerrada")
        self.assertFalse(guarda_beta["allowed"], "BETA continua bloqueada")

    async def test_observacao_filtrada_so_revalida_o_proprio_simbolo(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT"),
                                  registro_ack(2, "BETAUSDT")])
        observacao = {"ok": True, "reason_code": "POSITIONS_FRESH",
                      "positions": [], "observed_at_ms": mps._now_ms(),
                      "scope": "SYMBOL", "symbol": "ALFA/USDT:USDT",
                      "complete": True}
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "_transition_acks", registro.transition), \
                patch.object(mps, "record_validation_proof", registro.proof), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "symbol_has_live_orders",
                             AsyncMock(return_value={"ok": True, "live": False,
                                                     "count": 0})):
            await mps.revalidate_active(observation=observacao)
        self.assertEqual(registro.estado(2), "ACTIVE",
                         "símbolo fora do escopo da observação não muda")

    async def test_observacao_incompleta_nao_prova_ausencia(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        observacao = {"ok": False, "reason_code": mps.ACK_POSITION_UNKNOWN,
                      "positions": [], "observed_at_ms": mps._now_ms(),
                      "scope": "ACCOUNT", "symbol": None, "complete": False}
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "_transition_acks", registro.transition), \
                patch.object(mps, "record_validation_proof", registro.proof), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO):
            veredito = await mps.revalidate_active(observation=observacao)
        self.assertFalse(veredito["ok"])
        self.assertEqual(registro.estado(1), "ACTIVE")


class T2FlatComOrdensRestantes(unittest.IsolatedAsyncioTestCase):
    """Flat sem prova de ausência de ordens não libera o símbolo."""

    def _observacao_flat(self):
        return {"ok": True, "reason_code": "POSITIONS_FRESH", "positions": [],
                "observed_at_ms": mps._now_ms(), "scope": "ACCOUNT",
                "symbol": None, "complete": True}

    async def _revalidar(self, registro, ordens):
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "_transition_acks", registro.transition), \
                patch.object(mps, "record_validation_proof", registro.proof), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "symbol_has_live_orders", ordens):
            return await mps.revalidate_active(observation=self._observacao_flat())

    async def test_ordem_viva_mantem_o_simbolo_bloqueado(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        ordens = AsyncMock(return_value={"ok": True, "live": True, "count": 1})
        await self._revalidar(registro, ordens)
        self.assertGreaterEqual(ordens.await_count, 1,
                                "ordens precisam ser consultadas antes de encerrar")
        self.assertNotEqual(registro.estado(1), "CLOSED")
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("ALFAUSDT", action="entry")
        self.assertFalse(guarda["allowed"])

    async def test_listagem_indisponivel_mantem_bloqueio(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        ordens = AsyncMock(return_value={"ok": False,
                                         "reason_code": mps.ACK_ORDERS_UNKNOWN})
        await self._revalidar(registro, ordens)
        self.assertNotEqual(registro.estado(1), "CLOSED")
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("ALFAUSDT", action="entry")
        self.assertFalse(guarda["allowed"], "prova incompleta não libera")

    async def test_flat_com_duas_listagens_vazias_encerra(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        ordens = AsyncMock(return_value={"ok": True, "live": False, "count": 0})
        await self._revalidar(registro, ordens)
        self.assertEqual(registro.estado(1), "CLOSED")
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("ALFAUSDT", action="entry")
        self.assertTrue(guarda["allowed"], "ausência comprovada libera o símbolo")

    async def test_estado_de_espera_continua_bloqueando(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               state="WAITING_ORDERS")])
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("ALFAUSDT", action="entry")
        self.assertFalse(guarda["allowed"])
        self.assertIn("WAITING_ORDERS", mps.BLOCKING_STATES)
        self.assertIn("INVALIDATED", mps.BLOCKING_STATES)

    async def test_ordens_cobrem_comuns_e_condicionais(self):
        """`symbol_has_live_orders` consulta as DUAS fontes; erro = incerteza."""
        async def condicionais(symbol=None, **kwargs):
            return {"ok": True, "orders": []}

        async def comuns(symbol=None, **kwargs):
            return {"ok": True, "orders": [{"symbol": "ALFAUSDT",
                                            "order_id": "1"}]}

        with patch.object(bss, "get_open_algo_orders", condicionais), \
                patch.object(bss, "get_open_orders", comuns):
            verdict = await mps.symbol_has_live_orders("ALFA/USDT:USDT")
        self.assertTrue(verdict["ok"])
        self.assertTrue(verdict["live"], "LIMIT manual aberta conta como ordem viva")

        async def comuns_quebradas(symbol=None, **kwargs):
            return {"ok": False, "error": "indisponível"}

        with patch.object(bss, "get_open_algo_orders", condicionais), \
                patch.object(bss, "get_open_orders", comuns_quebradas):
            verdict = await mps.symbol_has_live_orders("ALFA/USDT:USDT")
        self.assertFalse(verdict["ok"], "falha numa fonte não prova ausência")


class T3RevalidacaoRecorrente(unittest.IsolatedAsyncioTestCase):
    """O ciclo oficial revalida mesmo sem incidente aberto."""

    async def test_ciclo_revalida_com_zero_incidentes(self):
        repo = ers.InMemoryIncidentRepo()
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        leituras = []

        async def observar(symbol=None):
            leituras.append(symbol)
            return {"ok": True, "reason_code": "POSITIONS_FRESH",
                    "positions": [posicao("ALFAUSDT")],
                    "observed_at_ms": mps._now_ms(),
                    "scope": "SYMBOL" if symbol else "ACCOUNT",
                    "symbol": mps.canonical_symbol(symbol) if symbol else None,
                    "complete": True}

        with patch.object(ers, "_get_repo", return_value=repo), \
                patch.object(ers, "_boot_scan_safe", True), \
                patch.object(ers, "_p03_latch_armed", False), \
                patch.object(ers, "recover_entry_intents",
                             AsyncMock(return_value={})), \
                patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "_transition_acks", registro.transition), \
                patch.object(mps, "record_validation_proof", registro.proof), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "observe_positions", observar), \
                patch.object(mps, "symbol_has_live_orders",
                             AsyncMock(return_value={"ok": True, "live": False,
                                                     "count": 0})), \
                patch.object(mps, "record_validation_proof",
                             AsyncMock(return_value={"ok": True})):
            await ers.reconcile_due()

        self.assertGreaterEqual(len(leituras), 1,
                                "o ciclo precisa obter observação fresca")
        self.assertIn(None, leituras, "a varredura completa é de CONTA")

    async def test_prova_de_validacao_vencida_nao_autoriza_exposicao(self):
        antigo = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 60) * 1000)
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               validated_at_ms=antigo)])
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("BETA/USDT:USDT", action="entry",
                                               require_fresh_proof=True)
        self.assertFalse(guarda["allowed"],
                         "prova antiga não autoriza NOVA exposição")
        self.assertEqual(guarda["reason_code"], mps.GUARD_PROOF_STALE)

    async def test_prova_fresca_autoriza_outro_simbolo(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               validated_at_ms=mps._now_ms())])
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("BETA/USDT:USDT", action="entry",
                                               require_fresh_proof=True)
        self.assertTrue(guarda["allowed"])

    async def test_manutencao_protetiva_nao_exige_prova_nova(self):
        """SL/saída de posição BOT em outro símbolo não é NOVA exposição."""
        antigo = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 60) * 1000)
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               validated_at_ms=antigo)])
        with patch.object(mps, "active_acknowledgements", registro.active):
            guarda = await mps.ownership_guard("BETA/USDT:USDT",
                                               action="place_protection_orders")
        self.assertTrue(guarda["allowed"])


class T4CarteiraStale(unittest.IsolatedAsyncioTestCase):
    """Cache antigo não pode virar leitura `live` com idade zero."""

    async def test_cooldown_com_cache_de_dez_minutos_bloqueia_margem(self):
        cache = {"ok": True, "equity_usd": 1000.0, "available_usd": 100.0,
                 "wallet_balance_usd": 1000.0, "margin_used_usd": 900.0}
        with patch.object(bss, "_account_cache",
                          {"ts": time.time() - 600, "data": cache}), \
                patch.object(exs, "_equity_cache", {"ts": 0, "data": None}), \
                patch.object(bss, "_signed_request",
                             AsyncMock(return_value={"ok": False,
                                                     "_cooldown": True})):
            snapshot = await sts._free_margin_snapshot()

        if snapshot is not None:
            self.assertNotEqual(snapshot.get("source"), "live")
            idade_ms = mps._now_ms() - int(snapshot["as_of_ms"])
            self.assertGreater(idade_ms, 500_000,
                               "o instante ORIGINAL precisa ser preservado")
        motivo = intents._margin_reason(
            intents.MarginGate(
                available_usd=(snapshot or {}).get("available_usd"),
                required_usd=80.0,
                as_of_ms=(snapshot or {}).get("as_of_ms"),
                complete=bool(snapshot is not None
                              and snapshot.get("complete", False))), 0.0)
        self.assertIsNotNone(motivo, "margem não pode ser aprovada com prova stale")

    async def test_force_chega_na_origem(self):
        chamadas = []

        async def wallet(account_type="UNIFIED", *, force=False):
            chamadas.append(force)
            return {"ok": True, "equity_usd": 10.0, "available_usd": 10.0,
                    "wallet_balance_usd": 10.0, "margin_used_usd": 0.0,
                    "as_of_ms": mps._now_ms(), "source": "live"}

        with patch.object(exs, "_equity_cache",
                          {"ts": time.time(), "data": {"ok": True,
                                                       "total_usd": 1.0,
                                                       "available_usd": 1.0,
                                                       "wallet_usd": 1.0,
                                                       "margin_used_usd": 0.0,
                                                       "exchange": "binance"}}), \
                patch.object(exs._client, "get_wallet_balance", wallet):
            await exs.get_equity(force=True)
        self.assertEqual(chamadas, [True],
                         "force precisa atravessar os dois caches")

    async def test_zero_legitimo_bloqueia_proposta_positiva(self):
        motivo = intents._margin_reason(
            intents.MarginGate(available_usd=0.0, required_usd=10.0,
                               as_of_ms=mps._now_ms(), complete=True), 0.0)
        self.assertEqual(motivo, "INSUFFICIENT_FREE_MARGIN")

    async def test_horario_futuro_incoerente_nao_passa(self):
        futuro = mps._now_ms() + 120_000
        motivo = intents._margin_reason(
            intents.MarginGate(available_usd=1_000.0, required_usd=10.0,
                               as_of_ms=futuro, complete=True), 0.0)
        self.assertEqual(motivo, "FREE_MARGIN_STALE")


class T6OwnershipNoPontoFinal(unittest.IsolatedAsyncioTestCase):
    """Reconhecimento durante o throttle: ZERO mutação depois dele."""

    def _cliente(self, enviados):
        async def request(method, url):
            enviados.append((method, url.split("?")[0]))
            return SimpleNamespace(status_code=200, headers={},
                                   json=lambda: {"leverage": 5})
        return SimpleNamespace(request=request)

    async def _com_throttle(self, coro_factory, ativos, enviados):
        async def durante_throttle(delay):
            # O reconhecimento nasce ENQUANTO a requisição espera o throttle.
            ativos.append(registro_ack(1, "ALFAUSDT"))
            bss._throttle_until_ms = 0

        async def registro():
            return {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [dict(x) for x in ativos]}

        with patch.object(mps, "active_acknowledgements", registro), \
                patch.object(bss, "is_configured", return_value=True), \
                patch.object(bss, "_ban_until_ms", 0), \
                patch.object(bss, "_throttle_until_ms", time.time() * 1000 + 1000), \
                patch.object(bss.asyncio, "sleep", durante_throttle), \
                patch.object(bss, "_build_signed_url",
                             return_value="https://sintetico.invalido/x"), \
                patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)), \
                patch.object(bss, "_get_client", return_value=self._cliente(enviados)):
            return await coro_factory()

    async def test_set_leverage_nao_envia_depois_do_reconhecimento(self):
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.set_leverage("ALFAUSDT", 5), ativos, enviados)
        self.assertTrue(ativos, "o reconhecimento precisa ter nascido no throttle")
        self.assertEqual(enviados, [], "nenhuma requisição depois do guard final")
        self.assertFalse(res.get("ok"))
        self.assertIs(res.get("_request_sent"), False)

    async def test_cancel_order_nao_envia_depois_do_reconhecimento(self):
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.cancel_order("ALFA/USDT:USDT", order_id="1"),
            ativos, enviados)
        self.assertEqual(enviados, [])
        self.assertFalse(res.get("ok"))

    async def test_cancel_algo_nao_envia_depois_do_reconhecimento(self):
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.cancel_algo_order("A1", symbol="ALFA/USDT:USDT"),
            ativos, enviados)
        self.assertEqual(enviados, [])
        self.assertFalse(res.get("ok"))

    async def test_protecao_nao_envia_depois_do_reconhecimento(self):
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.place_protection_orders("ALFA/USDT:USDT", "Buy", 1.0,
                                                stop_loss=90.0),
            ativos, enviados)
        self.assertEqual(enviados, [])
        self.assertFalse(res.get("sl_ok"))

    async def test_outro_simbolo_continua_enviando(self):
        """O guard final não pode bloquear a manutenção legítima do bot."""
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.set_leverage("BETAUSDT", 5), ativos, enviados)
        self.assertTrue(ativos)
        self.assertEqual(enviados, [("POST", "https://sintetico.invalido/x")])
        self.assertTrue(res.get("ok"))


class TokenDeAdmissaoAntesDoEnvio(unittest.IsolatedAsyncioTestCase):
    """§4.2.7 — o dispatch confere a geração RESULTANTE da própria admissão."""

    async def _guard(self, token_admitido, token_no_banco):
        from services import entry_intent_service as eis
        intent = {"granted": True, "intent_key": "k", "dispatched": True,
                  "state": "SENDING", "margin_generation": token_admitido}
        linha = SimpleNamespace(margin_generation=token_no_banco)
        with patch.object(eis, "mark_sending", AsyncMock(return_value=True)), \
                patch.object(eis, "may_dispatch", AsyncMock(return_value=True)), \
                patch.object(eis, "get_intent", AsyncMock(return_value=linha)):
            return await sts._intent_dispatch_guard(intent)

    async def test_token_igual_libera_o_envio(self):
        self.assertTrue(await self._guard(7, 7))

    async def test_token_mudou_exige_nova_admissao(self):
        self.assertFalse(await self._guard(7, 8))

    async def test_token_ilegivel_nao_despacha(self):
        from services import entry_intent_service as eis
        intent = {"granted": True, "intent_key": "k", "dispatched": True,
                  "state": "SENDING", "margin_generation": 7}
        with patch.object(eis, "mark_sending", AsyncMock(return_value=True)), \
                patch.object(eis, "may_dispatch", AsyncMock(return_value=True)), \
                patch.object(eis, "get_intent",
                             AsyncMock(side_effect=RuntimeError("banco fora"))):
            self.assertFalse(await sts._intent_dispatch_guard(intent))


if __name__ == "__main__":
    unittest.main()
