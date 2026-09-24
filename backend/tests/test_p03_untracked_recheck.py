"""P03 — saída para `UNTRACKED_POSITION` preso em `MANUAL_REQUIRED`.

Regressão do incidente real de 21–24/09/2026: a posição manual de BTC foi
fechada pelo operador, mas o incidente continuou aberto (o reconciliador não
reavalia `MANUAL_REQUIRED` e nenhum fluxo resolvia este kind), mantendo a
quarentena e bloqueando o resume manual por três dias.

Hermético: rede/DNS bloqueados e contabilizados, repo em memória, exchange
mockada. Nenhuma ordem é criada, cancelada ou fechada aqui.
"""
from __future__ import annotations

import asyncio
import socket as _socket
import sys
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import services.execution_reconciliation_service as ers  # noqa: E402
import services.binance_signed_service as bss  # noqa: E402
from services.execution_reconciliation_service import (  # noqa: E402
    InMemoryIncidentRepo, Kind, State,
)

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET: list = []


def _blocked(*a, **k):
    _NET.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P03 untracked: {a[:1]}")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


#: Funções de MUTAÇÃO da exchange: nenhuma pode ser chamada pela re-checagem.
_MUTATIONS = ("cancel_order", "cancel_algo_order", "place_protection_orders",
              "place_order", "close_position_market")

BTC = "BTC/USDT:USDT"
CHIP = "CHIP/USDT:USDT"


def flat():
    return {"quality": "FRESH", "size": 0.0, "side": None}


def aberta(side="buy", size=0.032):
    return {"quality": "FRESH", "size": size, "side": side}


def incerta():
    return {"quality": "UNKNOWN", "size": None, "side": None}


class ReCheckUntracked(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ers.set_repo(InMemoryIncidentRepo())
        ers._prev_open_count = 0
        ers._p03_latch_armed = False
        ers._boot_scan_safe = True
        ers._untracked_recheck_at.clear()
        self._stack = ExitStack()
        self._arm = self._stack.enter_context(
            patch.object(ers, "_arm_quarantine", AsyncMock()))
        self._release = self._stack.enter_context(
            patch.object(ers, "_maybe_release_quarantine", AsyncMock(return_value=True)))
        self._stack.enter_context(
            patch.object(ers, "_match_real_trade", AsyncMock(return_value={"skip": True})))
        # Guarda de hermeticidade + prova de que NADA é mutado na exchange.
        self.mutacoes = {}
        for nome in _MUTATIONS:
            if hasattr(bss, nome):
                mock = AsyncMock(side_effect=AssertionError(f"{nome} não pode ser chamada"))
                self._stack.enter_context(patch.object(bss, nome, mock))
                self.mutacoes[nome] = mock
        self._stack.enter_context(patch.object(
            bss, "get_positions", AsyncMock(return_value={"ok": True, "positions": []})))
        # Posição do incidente: começa ABERTA (é o que o torna untracked).
        self.fresh = self._stack.enter_context(
            patch.object(ers, "_fresh_position", AsyncMock(return_value=aberta())))

    def tearDown(self):
        self._stack.close()
        ers.set_repo(None)

    async def _seed_manual(self, symbol=BTC, kind=Kind.UNTRACKED_POSITION, side="buy"):
        """Cria o incidente e o leva ao estado real de produção: MANUAL_REQUIRED."""
        await ers.record_incident(kind=kind, symbol=symbol, side=side,
                                  min_known_fill=0.032,
                                  payload={"detected_at_boot": True})
        key = [row["incident_key"] for row in await ers._get_repo().list_open()
               if row["symbol"] == symbol][0]
        # Mesma sequência do ciclo real: claim antes de reconciliar (o fencing
        # por owner recusa mutação sem claim).
        await ers._get_repo().claim(key, "teste", ers._now() + timedelta(seconds=60))
        await ers._reconcile_one(key, "teste")
        # O ciclo real solta o claim de incidente não resolvido no `finally`.
        await ers._get_repo().release_claim(key, owner="teste")
        ers._untracked_recheck_at.clear()
        linha = await ers._get_repo().get(key)
        self.assertEqual(linha["state"], State.MANUAL_REQUIRED)
        return key

    async def test_posicao_sumiu_resolve_o_incidente(self):
        key = await self._seed_manual()
        self.fresh.return_value = flat()
        out = await ers.recheck_untracked_manual()
        self.assertEqual((out["checked"], out["resolved"]), (1, 1))
        linha = await ers._get_repo().get(key)
        self.assertEqual(linha["state"], State.FLAT)
        self.assertIsNotNone(linha["resolved_at"])
        self.assertIn("fresh-flat", linha["last_error"])
        self.assertEqual(await ers._get_repo().list_open(), [])

    async def test_posicao_ainda_aberta_mantem_o_incidente(self):
        key = await self._seed_manual()
        out = await ers.recheck_untracked_manual()
        self.assertEqual(out["resolved"], 0)
        self.assertEqual(out["kept"][key], ers.FreshGate.OPEN_VALID)
        linha = await ers._get_repo().get(key)
        self.assertEqual(linha["state"], State.MANUAL_REQUIRED)
        self.assertIsNone(linha["resolved_at"])

    async def test_leitura_incerta_nao_assume_flat(self):
        key = await self._seed_manual()
        self.fresh.return_value = incerta()
        out = await ers.recheck_untracked_manual()
        self.assertEqual(out["resolved"], 0)
        self.assertEqual(out["kept"][key], ers.FreshGate.UNKNOWN)
        self.assertIsNone((await ers._get_repo().get(key))["resolved_at"])

    async def test_lado_ambiguo_mantem_o_incidente(self):
        key = await self._seed_manual()
        self.fresh.return_value = {"quality": "FRESH", "size": 0.032, "side": None}
        out = await ers.recheck_untracked_manual()
        self.assertEqual(out["resolved"], 0)
        self.assertEqual(out["kept"][key], ers.FreshGate.SIDE_UNKNOWN)

    async def test_outro_kind_manual_nao_e_tocado(self):
        key = await self._seed_manual(kind=Kind.PERSISTENCE_FAILURE)
        self.fresh.return_value = flat()
        out = await ers.recheck_untracked_manual()
        self.assertEqual((out["checked"], out["resolved"]), (0, 0))
        linha = await ers._get_repo().get(key)
        self.assertEqual(linha["state"], State.MANUAL_REQUIRED)
        self.assertIsNone(linha["resolved_at"])

    async def test_posicao_manual_de_outro_simbolo_nao_impede(self):
        """CHIP aberta (posição manual do operador) não segura o incidente do BTC."""
        btc = await self._seed_manual(symbol=BTC)

        async def por_simbolo(symbol):
            return flat() if symbol == BTC else aberta(size=17409.0)

        self.fresh.side_effect = por_simbolo
        out = await ers.recheck_untracked_manual()
        self.assertEqual(out["resolved"], 1)
        self.assertIsNotNone((await ers._get_repo().get(btc))["resolved_at"])

    async def test_janela_de_re_checagem_evita_martelar_a_exchange(self):
        await self._seed_manual()
        primeira = await ers.recheck_untracked_manual()
        segunda = await ers.recheck_untracked_manual()
        self.assertEqual(primeira["checked"], 1)
        self.assertEqual(segunda["checked"], 0)
        self.assertEqual(self.fresh.await_count, 1)

    async def test_nenhuma_mutacao_na_exchange(self):
        await self._seed_manual()
        self.fresh.return_value = flat()
        await ers.recheck_untracked_manual()
        for nome, mock in self.mutacoes.items():
            self.assertEqual(mock.await_count, 0, nome)

    async def test_ciclo_libera_a_quarentena_no_mesmo_ciclo(self):
        """O ciclo que constata a posição ausente já solta a quarentena."""
        await self._seed_manual()
        ers._p03_latch_armed = True
        self.fresh.return_value = flat()
        resultado = await ers.reconcile_due()
        self.assertEqual(resultado["open_now"], 0)
        self.assertTrue(resultado["quarantine_released"])
        self.assertEqual(self._release.await_count, 1)

    async def test_ciclo_com_posicao_presente_mantem_a_quarentena(self):
        await self._seed_manual()
        ers._p03_latch_armed = True
        resultado = await ers.reconcile_due()
        self.assertEqual(resultado["open_now"], 1)
        self.assertFalse(resultado["quarantine_released"])
        self.assertEqual(self._release.await_count, 0)

    async def test_falha_lendo_incidentes_nao_resolve_nada(self):
        await self._seed_manual()
        with patch.object(ers._get_repo(), "list_open",
                          AsyncMock(side_effect=RuntimeError("banco fora"))):
            out = await ers.recheck_untracked_manual()
        self.assertEqual((out["checked"], out["resolved"]), (0, 0))
        self.assertIn("banco fora", out["error"])

    async def test_falha_na_re_checagem_nao_derruba_o_ciclo(self):
        await self._seed_manual()
        with patch.object(ers, "recheck_untracked_manual",
                          AsyncMock(side_effect=RuntimeError("leitura falhou"))):
            resultado = await ers.reconcile_due()
        self.assertEqual(resultado["open_now"], 1)
        self.assertFalse(resultado["quarantine_released"])


if __name__ == "__main__":
    unittest.main()
