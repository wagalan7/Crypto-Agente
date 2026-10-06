"""A autorização do candidato chega à borda REAL de assinatura da alavancagem.

HTTP/ownership externos são falsos; set_leverage, composição dos guards e
place_order/maker são código real. Não há chaves, conta ou mensagem real.
"""
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from services import binance_signed_service as signed
from tests.test_manual_bot_single_closure import (
    ClienteHTTPFalso, FILTROS_PADRAO, DELTA, respostas_padrao, preflight_aprovando,
)


class CandidateTransportBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.client = ClienteHTTPFalso(respostas_padrao())
        for item in (
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited")),
            patch.object(signed, "is_configured", return_value=True),
            patch.object(signed, "_ban_until_ms", 0),
            patch.object(signed, "_throttle_until_ms", 0),
            patch.object(signed, "_manual_ownership_block", AsyncMock(return_value=None)),
            patch.object(signed, "_round_qty", AsyncMock(return_value=1.0)),
            patch.object(signed, "_get_symbol_filters", AsyncMock(return_value=FILTROS_PADRAO)),
            patch.object(signed, "_build_signed_url", self.client.assinar),
            patch.object(signed, "_get_client", return_value=self.client),
        ):
            self.stack.enter_context(item)

    async def test_legacy_leverage_unchanged(self):
        result = await signed.set_leverage("DELTAUSDT", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.client.mutacoes()), 1)
        self.assertEqual(self.client.mutacoes()[0]["params"], {"symbol": "DELTAUSDT", "leverage": 5})

    async def test_denied_candidate_zero_leverage_posts(self):
        guard = AsyncMock(return_value={"ok": False, "reason_code": "APPROVAL_REVOKED"})
        result = await signed.set_leverage("DELTAUSDT", 5, candidate_guard=guard)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "APPROVAL_REVOKED")
        self.assertEqual(self.client.mutacoes(), [])

    async def test_missing_boolean_approval_never_authorizes(self):
        for verdict in ({"ok": 1}, {"ok": "true"}, None, {}, False):
            result = await signed.set_leverage("DELTAUSDT", 5,
                candidate_guard=AsyncMock(return_value=verdict))
            self.assertFalse(result["ok"])
        self.assertEqual(self.client.mutacoes(), [])

    async def test_exception_guard_zero_posts(self):
        result = await signed.set_leverage("DELTAUSDT", 5,
            candidate_guard=AsyncMock(side_effect=RuntimeError("source unavailable")))
        self.assertFalse(result["ok"])
        self.assertEqual(self.client.mutacoes(), [])

    async def test_sync_revocation_after_async_verdict_zero_posts(self):
        result = await signed.set_leverage("DELTAUSDT", 5,
            candidate_guard=AsyncMock(return_value={"ok": True,
                "sync_check": lambda _params: {"ok": False, "reason_code": "GENERATION_STALE"}}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "GENERATION_STALE")
        self.assertEqual(self.client.mutacoes(), [])

    async def test_guard_is_after_await_of_ownership(self):
        state = {"revoked": False}
        async def ownership(*_args, **_kwargs):
            state["revoked"] = True
            return None
        async def guard():
            return {"ok": not state["revoked"], "reason_code": "APPROVAL_REVOKED"}
        with patch.object(signed, "_manual_ownership_block", ownership):
            result = await signed.set_leverage("DELTAUSDT", 5, candidate_guard=guard)
        self.assertFalse(result["ok"])
        self.assertEqual(self.client.mutacoes(), [])

    async def test_market_leverage_retry_is_guarded_and_never_enters(self):
        guard = AsyncMock(return_value={"ok": False, "reason_code": "APPROVAL_EXPIRED"})
        result = await signed.place_order(DELTA, "Buy", 1.0, leverage=5,
            candidate_guard=guard, entry_preflight=preflight_aprovando)
        self.assertFalse(result["ok"])
        self.assertTrue(result["entry_not_submitted"])
        self.assertEqual(guard.await_count, 2)
        self.assertEqual(self.client.mutacoes(), [])

    async def test_maker_leverage_retry_is_guarded_and_never_enters(self):
        guard = AsyncMock(return_value={"ok": False, "reason_code": "APPROVAL_EXPIRED"})
        result = await signed.place_maker_entry_then_protect(DELTA, "Buy", 1.0,
            limit_price=100.0, leverage=5, candidate_guard=guard)
        self.assertFalse(result["ok"])
        self.assertTrue(result["entry_not_submitted"])
        self.assertEqual(guard.await_count, 2)
        self.assertEqual(self.client.mutacoes(), [])
