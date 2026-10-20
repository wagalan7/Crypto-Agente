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
             "validated_at_ms": mps._now_ms(), "revision": 1,
             "validated_revision": 1,
             "validated_generation": 7,
             "validation_scope": "ACCOUNT",
             "validation_account": ESCOPO,
             "exchange": "binance",
             "market": "usdm_futures",
             "contract_version": "MANUAL_ACK_V1"}
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
    """Uma leitura de ALFA não pode encerrar o reconhecimento de BETA.

    O contrato ficou mais forte depois do fechamento: a identidade é CAPTURADA
    antes do GET e conferida com CAS no commit. Os cenários persistidos (duas
    posições, duas conexões, mudança durante o GET e durante cada consulta de
    ordens) estão em `tests/pg_integration_manual_closure.py` (T8) e em
    `tests/pg_integration_manual_coexistence.py`. Aqui ficam as invariantes
    PURAS do contrato, que não dependem de banco.
    """

    async def test_observacao_sem_contexto_nao_encerra_nada(self):
        observacao = {"ok": True, "complete": True, "positions": [],
                      "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                      "observed_end_ms": mps._now_ms(),
                      "reason_code": "POSITIONS_FRESH"}
        veredito = await mps.revalidate_active(observation=observacao)
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], mps.STALE_CONTEXT)
        self.assertEqual(veredito["closed"], [])

    async def test_escopo_divergente_do_contexto_e_recusado(self):
        contexto = {"ok": True, "scope": mps.SCOPE_SYMBOL,
                    "symbol": "ALFA/USDT:USDT", "symbol_key": "ALFA/USDT",
                    "account_scope": ESCOPO, "acks": [], "ack_ids": [],
                    "manual_generation": 0,
                    "local_fence": mps.local_validation_fence(),
                    "started_at_ms": mps._now_ms()}
        observacao = {"ok": True, "complete": True, "positions": [],
                      "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                      "account_scope": ESCOPO,
                      "observed_end_ms": mps._now_ms(),
                      "reason_code": "POSITIONS_FRESH"}
        with patch.object(mps, "current_account_scope", return_value=ESCOPO):
            veredito = await mps.revalidate_active(observation=observacao,
                                                   context=contexto)
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], mps.STALE_CONTEXT)

    async def test_observacao_incompleta_registra_falha_e_nao_encerra(self):
        contexto = {"ok": True, "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                    "symbol_key": None, "account_scope": ESCOPO, "acks": [],
                    "ack_ids": [], "manual_generation": 0,
                    "local_fence": mps.local_validation_fence(),
                    "started_at_ms": mps._now_ms()}
        observacao = {"ok": False, "complete": False, "positions": [],
                      "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                      "account_scope": ESCOPO,
                      "observed_end_ms": mps._now_ms(),
                      "reason_code": mps.ACK_POSITION_UNKNOWN}
        registrada = {}

        async def falha(*, reason, context=None):
            registrada.update({"reason": reason})
            return {"ok": True, "reason_code": mps.VALIDATION_FAILURE}

        with patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "register_validation_failure", falha):
            veredito = await mps.revalidate_active(observation=observacao,
                                                   context=contexto)
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["closed"], [])
        self.assertEqual(registrada.get("reason"), mps.ACK_POSITION_UNKNOWN)

    async def test_fence_local_posterior_a_captura_invalida_o_resultado(self):
        contexto = {"ok": True, "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                    "symbol_key": None, "account_scope": ESCOPO, "acks": [],
                    "ack_ids": [], "manual_generation": 0,
                    "local_fence": mps.local_validation_fence() - 1,
                    "started_at_ms": mps._now_ms()}
        observacao = {"ok": True, "complete": True, "positions": [],
                      "scope": mps.SCOPE_ACCOUNT, "symbol": None,
                      "account_scope": ESCOPO,
                      "observed_end_ms": mps._now_ms(),
                      "reason_code": "POSITIONS_FRESH"}
        with patch.object(mps, "current_account_scope", return_value=ESCOPO):
            veredito = await mps.revalidate_active(observation=observacao,
                                                   context=contexto)
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], mps.STALE_CONTEXT)

class T2FlatComOrdensRestantes(unittest.IsolatedAsyncioTestCase):
    """Flat sem prova de ausência de ordens não libera o símbolo.

    O ciclo persistido (flat + LIMIT manual, flat + condicional, falha numa
    listagem e duas listagens vazias) está em
    `tests/pg_integration_manual_coexistence.py`. Aqui ficam o predicado dos
    estados bloqueantes e o contrato das DUAS fontes de ordens.
    """

    async def test_estado_de_espera_continua_bloqueando(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               state="WAITING_ORDERS")])
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO):
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

    async def test_mudanca_de_estado_revoga_a_prova(self):
        """Transição SEMPRE limpa os cinco componentes da prova."""
        linha = SimpleNamespace(validated_at_ms=1, validation_scope="ACCOUNT",
                                validation_account=ESCOPO, validated_revision=3,
                                validated_generation=7, updated_at=None)
        mps._revoke_proof(linha, "agora")
        self.assertIsNone(linha.validated_at_ms)
        self.assertIsNone(linha.validation_scope)
        self.assertIsNone(linha.validation_account)
        self.assertIsNone(linha.validated_revision)
        self.assertIsNone(linha.validated_generation)

class T3RevalidacaoRecorrente(unittest.IsolatedAsyncioTestCase):
    """O ciclo oficial revalida mesmo sem incidente aberto.

    A versão persistida (`ciclo_revalida_sem_incidente_aberto`) está em
    `tests/pg_integration_manual_coexistence.py`. Aqui fica a exigência de
    prova COMPLETA antes de nova exposição e a isenção das ações protetivas.
    """

    def _estado(self, *, blocked=False, generation=7):
        async def _state(account_scope=None):
            return {"blocked": blocked, "generation": generation,
                    "pending_failure": False,
                    "reason_code": ("MANUAL_ACCOUNT_VALIDATION_BLOCKED"
                                    if blocked else "ACCOUNT_VALIDATED")}
        return _state

    async def _guard(self, registro, **kwargs):
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "account_validation_state", self._estado()):
            return await mps.ownership_guard("BETA/USDT:USDT", **kwargs)

    async def test_prova_de_validacao_vencida_nao_autoriza_exposicao(self):
        antigo = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 60) * 1000)
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               validated_at_ms=antigo)])
        guarda = await self._guard(registro, action="entry",
                                   require_fresh_proof=True)
        self.assertFalse(guarda["allowed"])
        self.assertEqual(guarda["reason_code"], mps.GUARD_PROOF_STALE)

    async def test_prova_fresca_autoriza_outro_simbolo(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        guarda = await self._guard(registro, action="entry",
                                   require_fresh_proof=True)
        self.assertTrue(guarda["allowed"], str(guarda))

    async def test_manutencao_protetiva_nao_exige_prova_nova(self):
        """SL/saída de posição BOT em outro símbolo não é NOVA exposição."""
        antigo = mps._now_ms() - int((mps.VALIDATION_MAX_AGE_S + 60) * 1000)
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT",
                                               validated_at_ms=antigo)])
        guarda = await self._guard(registro, action="place_protection_orders")
        self.assertTrue(guarda["allowed"])

    async def test_conta_bloqueada_nega_nova_exposicao(self):
        registro = RegistroFalso([registro_ack(1, "ALFAUSDT")])
        with patch.object(mps, "active_acknowledgements", registro.active), \
                patch.object(mps, "current_account_scope", return_value=ESCOPO), \
                patch.object(mps, "account_validation_state",
                             self._estado(blocked=True)):
            guarda = await mps.ownership_guard("BETA/USDT:USDT", action="entry",
                                               require_fresh_proof=True)
        self.assertFalse(guarda["allowed"])
        self.assertEqual(guarda["reason_code"], mps.GUARD_ACCOUNT_BLOCKED)

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
                patch.object(mps, "current_account_scope",
                             return_value=ESCOPO), \
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

    async def test_outro_simbolo_continua_com_manutencao_protetiva(self):
        """Manutenção protetiva/redutora de OUTRO símbolo continua passando.

        O reconhecimento nasce no throttle SEM prova publicada: nova exposição
        (alavancagem) é negada, mas cancelamento/proteção do bot não pode ser
        travado por reconhecimento alheio.
        """
        ativos, enviados = [], []
        cancelamento = await self._com_throttle(
            lambda: bss.cancel_order("BETA/USDT:USDT", order_id="9"),
            ativos, enviados)
        self.assertTrue(ativos)
        self.assertTrue(cancelamento.get("ok"), str(cancelamento)[:160])
        self.assertEqual(enviados, [("DELETE", "https://sintetico.invalido/x")])

    async def test_nova_exposicao_em_outro_simbolo_exige_prova(self):
        """Reconhecimento novo sem prova publicada barra NOVA exposição."""
        ativos, enviados = [], []
        res = await self._com_throttle(
            lambda: bss.set_leverage("BETAUSDT", 5), ativos, enviados)
        self.assertTrue(ativos)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason_code"), mps.GUARD_PROOF_STALE)
        self.assertEqual(enviados, [])


class TokenDeAdmissaoAntesDoEnvio(unittest.IsolatedAsyncioTestCase):
    """§4.2.7 — o dispatch confere a geração RESULTANTE da própria admissão."""

    async def _autorizacao(self, token_admitido, veredito):
        """A AUTORIZAÇÃO FINAL é quem compara o token (a preparação não)."""
        from services import entry_intent_service as eis
        intent = {"granted": True, "intent_key": "k", "dispatched": True,
                  "state": "SENDING", "margin_generation": token_admitido,
                  "last_dispatch_id": "cw-1"}
        with patch.object(eis, "authorize_dispatch",
                          AsyncMock(return_value=veredito)):
            return await sts._intent_final_authorization(intent)()

    async def test_token_igual_libera_o_envio(self):
        resultado = await self._autorizacao(7, {"ok": True, "token": 7})
        self.assertTrue(resultado["ok"])

    async def test_token_mudou_exige_nova_admissao(self):
        resultado = await self._autorizacao(
            7, {"ok": False, "reason_code": "MARGIN_OBSERVATION_SUPERSEDED"})
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["reason_code"], "MARGIN_OBSERVATION_SUPERSEDED")

    async def test_token_ilegivel_nao_despacha(self):
        resultado = await self._autorizacao(
            7, {"ok": False, "reason_code": "DISPATCH_CHECK_UNAVAILABLE"})
        self.assertFalse(resultado["ok"])

    async def test_preparacao_nao_exige_token_vigente(self):
        """A PREPARAÇÃO deixa renovar: ela não compara token financeiro."""
        from services import entry_intent_service as eis
        intent = {"granted": True, "intent_key": "k", "dispatched": True,
                  "state": "SENDING", "margin_generation": 7}
        with patch.object(eis, "mark_sending", AsyncMock(return_value=True)), \
                patch.object(eis, "may_dispatch", AsyncMock(return_value=True)):
            self.assertTrue(await sts._intent_dispatch_guard(intent))


if __name__ == "__main__":
    unittest.main()
