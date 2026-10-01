"""P03 — fechamento das integrações: capacidade real e identidade da conta.

Defeitos reproduzidos aqui (RED antes da correção):
  • o chamador desativa o limite de slots passando `max_open_positions=None`,
    mesmo havendo limite configurado em produção;
  • `account_ref` é `exchange:ambiente` — não identifica a CONTA, então duas
    credenciais diferentes colidem na mesma identidade de decisão;
  • conta desconhecida não bloqueia a reserva;
  • a identidade recusa a referência opaca de 64 caracteres já usada pelo
    contrato contábil (R05C), o que forçaria truncar ou inventar outro formato.

A exclusividade transacional e o fencing de desfecho ficam no PostgreSQL real
(`tests/pg_integration_p03_intent.py`). Aqui: sem rede, sem banco, sem exchange.
"""
from __future__ import annotations

from datetime import datetime, timezone
import socket as _socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste de fechamento P03")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import entry_intent_service as intents  # noqa: E402

TRIGGER = 1_760_000_100_000
SCOPE_A = "a" * 64
SCOPE_B = "b" * 64


def rec(**over):
    values = {
        "symbol": "BTC/USDT:USDT", "timeframe": "4h", "leverage": 3,
        "data_freshness": {"candle": {"close_time_ms": TRIGGER}},
        "score_provenance": {"formula_effective": "SCORE_V2"},
    }
    values.update(over)
    return values


class CapacidadeReal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import shadow_trade_service as sts
        self.sts = sts
        self.capturado = {}

        async def fake_reserve(session_factory, identity, payload, **kwargs):
            self.capturado["identity"] = identity
            self.capturado["payload"] = payload
            self.capturado["capacity"] = kwargs.get("capacity")
            return intents.Reservation(intents.RESERVED_NEW, identity.intent_key,
                                       identity.client_order_id, "RESERVED")

        # O gate de MARGEM real exige banco (geração) e carteira fresca; aqui a
        # característica sob teste é a CAPACIDADE que chega à admissão, então a
        # observação de margem é fornecida pronta — sem afrouxar o gate real.
        async def fake_margin_gate(identity, *, entry, qty, leverage):
            self.capturado["margin_identity"] = identity
            return intents.MarginGate(
                available_usd=1_000_000.0, required_usd=0.0,
                as_of_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                complete=True, account_ref=identity.account_ref,
                exchange=identity.exchange, market="usdm_futures",
                generation=0, quality="live")

        self._patches = [
            patch.object(intents, "reserve", fake_reserve),
            patch.object(sts, "_margin_gate_for", fake_margin_gate),
            patch.object(sts, "_open_risk_usd", AsyncMock(return_value=0.0)),
            patch("services.binance_signed_service.accounting_scope", lambda: SCOPE_A),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()

    async def _reservar(self, **over):
        kwargs = dict(side="long", entry=100.0, stop=95.0, tp1=105.0, tp2=110.0,
                      qty=1.0, equity_usd=1000.0)
        kwargs.update(over)
        return await self.sts._reserve_entry_intent(rec(), **kwargs)

    async def test_limite_de_slots_configurado_e_respeitado(self):
        """Limite real existe (`PORTFOLIO_MAX_OPEN_POSITIONS`): não pode ir None."""
        await self._reservar()
        capacity = self.capturado["capacity"]
        self.assertIsNotNone(capacity)
        self.assertIsNotNone(capacity.max_open_positions,
                             "reserva desativou o limite de slots configurado")
        self.assertEqual(capacity.max_open_positions, self.sts.FILLER_TOTAL_SLOTS)

    async def test_risco_da_decisao_vai_para_a_admissao(self):
        await self._reservar(entry=100.0, stop=95.0, qty=2.0)
        self.assertAlmostEqual(self.capturado["capacity"].risk_usd, 10.0)


class IdentidadeDaConta(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import shadow_trade_service as sts
        self.sts = sts

    def test_referencia_opaca_distingue_contas(self):
        """Duas credenciais diferentes não podem gerar a MESMA conta."""
        with patch("services.binance_signed_service.accounting_scope", lambda: SCOPE_A):
            primeira = self.sts._entry_account_ref()
        with patch("services.binance_signed_service.accounting_scope", lambda: SCOPE_B):
            segunda = self.sts._entry_account_ref()
        self.assertNotEqual(primeira, segunda,
                            "account_ref não identifica a conta (exchange:ambiente)")
        self.assertNotIn("mainnet", str(primeira))

    def test_identidade_muda_com_a_conta(self):
        with patch("services.binance_signed_service.accounting_scope", lambda: SCOPE_A):
            a = self.sts._entry_intent_identity(rec(), "long")
        with patch("services.binance_signed_service.accounting_scope", lambda: SCOPE_B):
            b = self.sts._entry_intent_identity(rec(), "long")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a.intent_key, b.intent_key)
        self.assertNotEqual(a.client_order_id, b.client_order_id)

    def test_identidade_aceita_a_referencia_contabil_de_64(self):
        """O contrato R05C já usa sha256 hex; a identidade precisa comportá-lo."""
        identity = intents.EntryIdentity(
            account_ref=SCOPE_A, exchange="binance", symbol="BTC-USDT-USDT",
            quote="USDT", side="long", position_side="BOTH", timeframe="4h",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2", purpose="ENTRY",
            trigger_candle_ms=TRIGGER)
        self.assertEqual(identity.account_ref, SCOPE_A)
        self.assertEqual(len(identity.client_order_id), 23)

    async def test_conta_desconhecida_bloqueia_a_reserva(self):
        """Sem conta comprovada não se envia ordem — não se adota a atual."""
        with patch("services.binance_signed_service.accounting_scope", lambda: None):
            self.assertIsNone(self.sts._entry_intent_identity(rec(), "long"))
            with patch.object(intents, "reserve",
                              AsyncMock(side_effect=AssertionError("não pode reservar"))):
                verdict = await self.sts._reserve_entry_intent(
                    rec(), side="long", entry=100.0, stop=95.0, tp1=105.0, tp2=110.0,
                    qty=1.0, equity_usd=1000.0)
        self.assertFalse(verdict["granted"])
        self.assertEqual(verdict["reason"], "IDENTITY_UNAVAILABLE")


class RecuperacaoNoCicloP03(unittest.IsolatedAsyncioTestCase):
    """As intenções pendentes precisam entrar na recuperação OPERACIONAL."""

    def setUp(self):
        import services.execution_reconciliation_service as ers
        self.ers = ers
        self.incidentes = []

        async def fake_record(**kwargs):
            self.incidentes.append(kwargs)
            return {"persisted": True}

        self._patches = [
            patch("db.DB_ENABLED", True),
            patch.object(ers, "record_incident", fake_record),
            patch.object(intents, "recover_stale",
                         AsyncMock(return_value={"to_unknown": 1, "reserved_released": 2})),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()

    async def test_intencao_incerta_abre_incidente_pelo_client_id(self):
        pendente = {"intent_key": "k1", "client_order_id": "cw-abc", "account_ref": SCOPE_A,
                    "exchange": "binance", "symbol": "BTC-USDT-USDT", "side": "long",
                    "reason": "DISPATCH_OUTCOME_UNKNOWN", "updated_at": None}
        with patch.object(intents, "list_needing_reconciliation",
                          AsyncMock(return_value=[pendente])):
            resumo = await self.ers.recover_entry_intents()
        self.assertEqual(resumo["to_unknown"], 1)
        self.assertEqual(resumo["reserved_released"], 2)
        self.assertEqual(resumo["incidents"], 1)
        incidente = self.incidentes[0]
        self.assertEqual(incidente["kind"], self.ers.Kind.ENTRY_SUBMISSION_UNKNOWN)
        self.assertEqual(incidente["client_order_id"], "cw-abc")
        self.assertEqual(incidente["symbol"], "BTC/USDT:USDT")
        self.assertEqual(incidente["payload"]["intent_key"], "k1")

    async def test_identidade_incompleta_nao_inventa_incidente(self):
        pendente = {"intent_key": "k2", "client_order_id": None, "account_ref": SCOPE_A,
                    "exchange": "binance", "symbol": "BTC-USDT-USDT", "side": "long",
                    "reason": "SAFETY_STATE_UNKNOWN", "updated_at": None}
        with patch.object(intents, "list_needing_reconciliation",
                          AsyncMock(return_value=[pendente])):
            resumo = await self.ers.recover_entry_intents()
        self.assertEqual(resumo["incidents"], 0)
        self.assertEqual(resumo["skipped_identity"], 1)
        self.assertEqual(self.incidentes, [])

    async def test_ordem_maker_viva_nao_e_desfecho_incerto(self):
        """`PENDING_ENTRY_ORDER` é ordem conhecida: não abre incidente."""
        self.assertNotIn("PENDING_ENTRY_ORDER", intents.RECONCILE_REASONS)
        for motivo in ("DISPATCH_OUTCOME_UNKNOWN", "SAFETY_STATE_UNKNOWN",
                       "PERSISTENCE_FAILED", "LEASE_EXPIRED_AFTER_DISPATCH"):
            self.assertIn(motivo, intents.RECONCILE_REASONS)

    async def test_ciclo_existente_chama_a_recuperacao(self):
        """Sem worker novo: o ciclo que já roda é quem recupera."""
        with patch.object(self.ers, "recover_entry_intents",
                          AsyncMock(return_value={})) as chamada, \
                patch.object(self.ers, "recheck_untracked_manual", AsyncMock(return_value={})), \
                patch.object(self.ers, "_maybe_release_quarantine", AsyncMock(return_value=False)):
            self.ers.set_repo(self.ers.InMemoryIncidentRepo())
            try:
                await self.ers.reconcile_due()
            finally:
                self.ers.set_repo(None)
        self.assertEqual(chamada.await_count, 1)

    def test_simbolo_da_identidade_vira_simbolo_de_mercado(self):
        self.assertEqual(self.ers._intent_symbol("BTC-USDT-USDT"), "BTC/USDT:USDT")
        self.assertEqual(self.ers._intent_symbol("BTC/USDT:USDT"), "BTC/USDT:USDT")
        self.assertEqual(self.ers._intent_symbol("ESQUISITO"), "ESQUISITO")
        self.assertIsNone(self.ers._intent_symbol(None))


class GuardEmCadaPost(unittest.IsolatedAsyncioTestCase):
    """O guard precisa rodar antes de CADA POST — inclusive no fallback."""

    def setUp(self):
        from services import shadow_trade_service as sts
        self.sts = sts
        self.intent = {"granted": True, "intent_key": "k", "client_order_id": "cw-abc",
                       "dispatched": True, "state": "SENDING"}
        self.registrados = []

    async def _registrar(self, intent, dispatch_id):
        self.registrados.append(dispatch_id)
        return True

    async def test_post_so_acontece_com_guard_aprovado(self):
        chamadas = []

        async def inner(*args, **kwargs):
            chamadas.append(args)
            return {"ok": True, "approved_qty": 1.0}

        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=True)), \
                patch.object(self.sts, "_register_intent_dispatch", self._registrar):
            with patch.object(self.sts, "_intent_final_authorization",
                              self._autorizacao_ok):
                guarded = self.sts._intent_guarded_preflight(
                    self.intent, inner, dispatch_id_fn=lambda: "cw-abc")
                verdict = await guarded(100.0, 1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(chamadas, [(100.0, 1.0)])
        self.assertEqual(self.registrados, ["cw-abc"])

    async def test_guard_negado_impede_o_post(self):
        inner = AsyncMock(side_effect=AssertionError("não pode chegar ao POST"))
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=False)):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, inner, dispatch_id_fn=lambda: "cw-abc")
            verdict = await guarded(100.0, 1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "EXEC_INTENT_GUARD_DENIED")
        self.assertEqual(inner.await_count, 0)

    def _autorizacao_ok(self, intent, *, dispatch_id_fn=None):
        async def _aprova():
            return {"ok": True, "reason_code": "DISPATCH_AUTHORIZED"}
        return _aprova

    def _autorizacao_negada(self, intent, *, dispatch_id_fn=None):
        async def _nega():
            return {"ok": False, "reason_code": "MARGIN_OBSERVATION_SUPERSEDED"}
        return _nega

    async def test_sem_preflight_interno_o_guard_ainda_vale(self):
        """Revalidação desligada não pode deixar o POST sem guard."""
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=False)):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, None, dispatch_id_fn=lambda: "cw-abc")
            verdict = await guarded(100.0, 1.0)
        self.assertFalse(verdict["ok"])
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=True)), \
                patch.object(self.sts, "_register_intent_dispatch", self._registrar), \
                patch.object(self.sts, "_intent_final_authorization",
                             self._autorizacao_ok):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, None, dispatch_id_fn=lambda: "cw-abc")
            self.assertTrue((await guarded(100.0, 1.0))["ok"])

    async def test_autorizacao_final_negada_impede_o_post(self):
        """Depois do `inner`, só a AUTORIZAÇÃO FINAL libera o envio."""
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=True)), \
                patch.object(self.sts, "_register_intent_dispatch", self._registrar), \
                patch.object(self.sts, "_intent_final_authorization",
                             self._autorizacao_negada):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, None, dispatch_id_fn=lambda: "cw-abc")
            verdict = await guarded(100.0, 1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "MARGIN_OBSERVATION_SUPERSEDED")

    async def test_id_efetivo_desconhecido_nao_despacha(self):
        inner = AsyncMock(side_effect=AssertionError("não pode chegar ao POST"))
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=True)):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, inner, dispatch_id_fn=lambda: None)
            verdict = await guarded(100.0, 1.0)
        self.assertEqual(verdict["reason_code"], "EXEC_INTENT_DISPATCH_UNRECORDED")
        self.assertEqual(inner.await_count, 0)

    async def test_registro_do_id_falho_nao_despacha(self):
        inner = AsyncMock(side_effect=AssertionError("não pode chegar ao POST"))
        with patch.object(self.sts, "_intent_dispatch_guard", AsyncMock(return_value=True)), \
                patch.object(self.sts, "_register_intent_dispatch",
                             AsyncMock(return_value=False)):
            guarded = self.sts._intent_guarded_preflight(
                self.intent, inner, dispatch_id_fn=lambda: "cw-abc")
            verdict = await guarded(100.0, 1.0)
        self.assertEqual(verdict["reason_code"], "EXEC_INTENT_DISPATCH_UNRECORDED")
        self.assertEqual(inner.await_count, 0)

    def test_id_do_fallback_vem_do_transport_que_envia(self):
        from services.binance_signed_service import market_fallback_client_order_id
        coid = "cw-0123456789abcdef0123"
        self.assertEqual(self.sts._market_fallback_coid(coid),
                         market_fallback_client_order_id(coid))
        self.assertNotEqual(self.sts._market_fallback_coid(coid), coid)
        self.assertLessEqual(len(self.sts._market_fallback_coid(coid)), 36)

    def test_ambos_os_despachos_recebem_o_guard_composto(self):
        """Nenhum POST de entrada sai sem o preflight guardado."""
        import ast
        source = (BACKEND / "services" / "shadow_trade_service.py").read_text()
        tree = ast.parse(source)
        function = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "open_shadow_for_recs")
        body = ast.unparse(function)
        self.assertNotIn("entry_preflight=_entry_preflight", body)
        self.assertNotIn("entry_preflight=_market_entry_preflight", body)
        self.assertNotIn("market_preflight=_market_entry_preflight", body)
        self.assertEqual(body.count("_intent_guarded_preflight("), 3)


class SemRede(unittest.TestCase):
    def test_nenhuma_tentativa_de_rede(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
