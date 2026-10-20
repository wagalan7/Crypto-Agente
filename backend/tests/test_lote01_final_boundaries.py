"""Fechamento Lote 01: fonte, equivalência monetária e retry esgotado.

Só o transporte é falso. Construtor/veredito, coleta, merge da persistência,
finalização, projeção e consumidor do total são os serviços reais. Relógio
simulado; nenhuma rede, credencial, ordem ou banco é utilizado.
"""
from __future__ import annotations

import copy
import socket
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tests import test_lote01_correcao_conjunta as fx
from services import execution_accounting_service as ea
from services import financial_total_service as fts


_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido nas fronteiras finais do Lote 01"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


def rehash(proof):
    """Adulteração deliberada: hash íntegro não pode comprar origem válida."""
    proof["material_id"] = ea.fee_conversion_material_id(proof)
    proof["integrity_hash"] = ea.fee_conversion_hash(proof)
    return proof


def apply_observation(current, incoming):
    """Mesmo merge que apply_accounting executa sob bloqueio da linha."""
    result = ea.merge_observation(current, incoming,
                                  view=fx.visao(accounting=current))
    assert result is not None, "observação legítima deve chegar ao merge"
    return result


async def observe(*, income, accounting=None, now_ms=fx.T0):
    fills, orders = fx.conjunto_padrao()
    return await fx.coletar(fx.ClienteFalso(
        fills=fills, orders=orders, income=income),
        accounting=accounting, now_ms=now_ms)


class FontePersistidaERevalidada(unittest.IsolatedAsyncioTestCase):
    async def test_referencia_legitima_preserva_vinculo_e_confirma_dinheiro(self):
        observation = await observe(income=[fx.income_commission()])
        key, proof = next(iter(observation["fee_conversions"].items()))
        source = proof["source_ref"]
        self.assertEqual(source.get("commission_asset"), "BNB")
        self.assertEqual(ea.to_decimal(source.get("commission_qty")),
                         Decimal("0.001"))
        context = ea.fee_expected_contexts(observation)[key]
        self.assertEqual(ea.fee_conversion_verdict(proof, expected=context)
                         ["quality"], ea.FEE_QUALITY_CONFIRMED)
        result = apply_observation(fx.visao()["execution_accounting"],
                                   observation)
        self.assertEqual(result["state"], ea.STATE_CONFIRMED)
        self.assertEqual(ea.project_to_trade_fields(result)["pnl_usd"], 9.53)
        self.assertEqual(fts.row_verdict(result, account_scope=fx.ESCOPO),
                         (Decimal("9.53"), None))

    async def test_fonte_adulterada_e_rehasheada_nao_passa_nas_fronteiras(self):
        legitimate = await observe(income=[fx.income_commission()])
        key, original = next(iter(legitimate["fee_conversions"].items()))
        context = ea.fee_expected_contexts(legitimate)[key]
        cases = [
            ("asset", {"asset": "BUSD"}),
            ("symbol", {"symbol": "ETHUSDT"}),
            ("exchange", {"exchange": "bybit"}),
            ("account_scope", {"account_scope": "b" * 64}),
            ("tran_id_ausente", {"tran_id": None}),
            ("tran_id_bool", {"tran_id": True}),
            ("tran_id_float", {"tran_id": 9001.0}),
            ("time_ausente", {"time_ms": None}),
            ("time_fora", {"time_ms": fx.ABERTURA - 10_000_000}),
            ("time_futuro", {"time_ms": fx.T0 + 10_000_000}),
            ("query_start_ausente", {"window_start_ms": None}),
            ("query_end_ausente", {"window_end_ms": None}),
            ("query_start_depois", {"window_start_ms": fx.ENTRADA_MS + 1}),
            ("query_end_antes", {"window_end_ms": fx.ENTRADA_MS - 1}),
            ("query_end_futuro", {"window_end_ms": fx.T0 + 10_000_000}),
            ("fee_asset_ausente", {"commission_asset": None}),
            ("fee_asset_divergente", {"commission_asset": "BUSD"}),
            ("fee_qty_ausente", {"commission_qty": None}),
            ("fee_qty_divergente", {"commission_qty": "0.002"}),
        ]
        for label, changes in cases:
            with self.subTest(campo=label):
                proof = copy.deepcopy(original)
                proof["source_ref"].update(changes)
                rehash(proof)
                verdict = ea.fee_conversion_verdict(proof, expected=context)
                built = ea.build_fee_conversion(
                    expected=context, settlement_value=proof["settlement_value"],
                    price=proof["price"], source=proof["source"],
                    source_ref=proof["source_ref"],
                    observed_start_ms=proof["observed_start_ms"],
                    observed_end_ms=proof["observed_end_ms"], now_ms=fx.T0)
                altered = copy.deepcopy(legitimate)
                altered["fee_conversions"] = {key: proof}
                result = apply_observation(fx.visao()["execution_accounting"],
                                           altered)
                self.assertEqual(verdict["quality"], ea.FEE_QUALITY_UNAVAILABLE,
                                 "hash recalculado não valida a referência")
                self.assertFalse(built.get("ok"), "construtor deve recusar fonte")
                self.assertFalse(ea.accounting_is_confirmed(result))
                self.assertIsNone(result["totals"]["net_trade"])
                self.assertIsNone(ea.project_to_trade_fields(result)["pnl_usd"])
                self.assertIsNotNone(fts.row_verdict(
                    result, account_scope=fx.ESCOPO)[1])
                self.assertEqual(result.get("fee_merge_outcomes", {}).get(key),
                                 "REJECTED")

    async def _collect_pages(self, pages, *, start_ms, end_ms):
        pending = await observe(income=[])
        required = ea._fee_conversions_required(pending)
        requests = []

        class Transport:
            async def get_income(self, symbol, income_type=None,
                                 start_time=None, end_time=None, limit=None):
                requests.append((start_time, end_time))
                return {"ok": True, "income": pages.pop(0), "limit": limit}

        result = await ea.collect_broker_fee_conversions(
            Transport(), "BTCUSDT", required, identity=pending["identity"],
            start_ms=start_ms, end_ms=end_ms, budget=[10], now_ms=fx.T0)
        return result, requests

    async def test_pagina_nao_pode_confirmar_linha_anterior_ao_seu_cursor(self):
        cursor = fx.ENTRADA_MS + 10_000
        unrelated = fx.income_commission(tradeId="outro", time=cursor)
        result, requests = await self._collect_pages(
            [[unrelated] * ea.INCOME_PAGE_LIMIT, [fx.income_commission()]],
            start_ms=fx.ABERTURA, end_ms=fx.FECHAMENTO)
        self.assertEqual(requests[1][0], cursor)
        self.assertFalse(result["complete"])
        self.assertEqual(result["evidences"], [],
                         "estar na janela global não prova estar na página")

    async def test_chunk_nao_pode_confirmar_linha_de_outro_chunk(self):
        start = fx.ENTRADA_MS - ea.MAX_TRADE_WINDOW_MS - 1_000
        result, requests = await self._collect_pages(
            [[fx.income_commission()], []],
            start_ms=start, end_ms=fx.FECHAMENTO)
        self.assertEqual(len(requests), 2)
        self.assertLess(requests[0][1], fx.ENTRADA_MS)
        self.assertFalse(result["complete"])
        self.assertEqual(result["evidences"], [],
                         "uma resposta não atesta janela de outro request")

    async def test_pagina_valida_preserva_janela_efetivamente_consultada(self):
        cursor = fx.ENTRADA_MS - 500
        unrelated = fx.income_commission(tradeId="outro", time=cursor)
        result, requests = await self._collect_pages(
            [[unrelated] * ea.INCOME_PAGE_LIMIT, [fx.income_commission()]],
            start_ms=fx.ABERTURA, end_ms=fx.FECHAMENTO)
        self.assertTrue(result["complete"], result)
        proof = result["evidences"][0]
        source = proof["source_ref"]
        self.assertEqual((source["window_start_ms"], source["window_end_ms"]),
                         requests[1])


class DinheiroEquivalenteNaoViraConflito(unittest.IsolatedAsyncioTestCase):
    async def test_duplicata_decimal_no_mesmo_lote_e_no_op(self):
        observed = await observe(income=[
            fx.income_commission(income="-0.42"),
            fx.income_commission(income="-0.42000000")])
        result = apply_observation(fx.visao()["execution_accounting"], observed)
        self.assertEqual(result["state"], ea.STATE_CONFIRMED)
        self.assertEqual(Decimal(result["totals"]["net_trade"]), Decimal("9.53"))
        self.assertEqual(result["conflicts"], [])

    async def test_escala_decimal_equivalente_nas_duas_ordens_de_aplicacao(self):
        a = await observe(income=[fx.income_commission(income="-0.42")])
        b = await observe(income=[fx.income_commission(income="-0.42000000")],
                          now_ms=fx.T0 + 1_000)
        key = next(iter(a["fee_conversions"]))
        self.assertEqual(a["fee_conversions"][key]["material_id"],
                         b["fee_conversions"][key]["material_id"])
        for first, second in ((a, b), (b, a)):
            with self.subTest(primeiro=first["fee_conversions"][key]
                              ["source_ref"]["income"]):
                current = apply_observation(fx.visao()["execution_accounting"],
                                            first)
                result = apply_observation(current, second)
                self.assertEqual(result["state"], ea.STATE_CONFIRMED)
                self.assertEqual(result["conflicts"], [])
                self.assertEqual(len(result["fee_conversions"]), 1)
                self.assertEqual(ea.project_to_trade_fields(result)["pnl_usd"],
                                 9.53)
                self.assertEqual(fts.row_verdict(result, account_scope=fx.ESCOPO),
                                 (Decimal("9.53"), None))

    async def test_diferenca_monetaria_real_preserva_conflito(self):
        a = await observe(income=[fx.income_commission(income="-0.42")])
        b = await observe(income=[fx.income_commission(income="-0.43")],
                          now_ms=fx.T0 + 1_000)
        current = apply_observation(fx.visao()["execution_accounting"], a)
        result = apply_observation(current, b)
        self.assertEqual(result["state"], ea.STATE_CONFLICT)
        self.assertIsNone(ea.project_to_trade_fields(result)["pnl_usd"])
        self.assertEqual(fts.row_verdict(result, account_scope=fx.ESCOPO),
                         (None, fts.LEDGER_CONFLICT))
        proof = next(iter(result["fee_conversions"].values()))
        self.assertEqual(Decimal(proof["settlement_value"]), Decimal("0.42"))


class ExaustaoEReplayNaoGanhamAutoridade(unittest.IsolatedAsyncioTestCase):
    async def test_oito_conversoes_atrasadas_nao_reabrem_retry_esgotado(self):
        fills, orders, income = fx.R5ProgressoLoteEConcorrencia()._cenario(49)
        late = await fx.coletar(fx.ClienteFalso(
            fills=fills, orders=orders, income=income))
        self.assertEqual(len(late["fee_conversions"]), 8)
        current = fx.visao()["execution_accounting"]
        clock = fx.T0
        for _ in range(ea.MAX_ATTEMPTS):
            if current.get("next_retry_at"):
                clock = int(datetime.fromisoformat(current["next_retry_at"])
                            .timestamp() * 1000)
            missing = await fx.coletar(fx.ClienteFalso(
                fills=fills, orders=orders, income=[]),
                accounting=current, now_ms=clock)
            current = apply_observation(current, missing)
        self.assertEqual(current["state"], ea.STATE_FAILED)
        self.assertEqual(current["attempts"], ea.MAX_ATTEMPTS)
        self.assertFalse(ea.is_retry_due(current))
        result = apply_observation(current, late)
        self.assertEqual(len(result["fee_conversions"]), 8,
                         "evidência útil atrasada continua sendo preservada")
        self.assertEqual(result["state"], ea.STATE_FAILED,
                         "evidência parcial não autoriza reabrir a recuperação")
        self.assertEqual(result["reason_code"], "RETRY_BUDGET_EXHAUSTED")
        self.assertEqual(result["attempts"], ea.MAX_ATTEMPTS)
        self.assertIsNone(result["next_retry_at"])
        self.assertFalse(ea.is_retry_due(result,
                                        now=datetime.now(timezone.utc)
                                        + timedelta(days=365)))
        self.assertIsNone(result["totals"]["net_trade"])
        self.assertIsNone(ea.project_to_trade_fields(result)["pnl_usd"])
        replay = apply_observation(result, late)
        self.assertEqual(replay["generation"], result["generation"])
        self.assertEqual(replay["attempts"], ea.MAX_ATTEMPTS)
        self.assertFalse(ea.is_retry_due(replay))

    async def test_replay_a_b_a_nao_avanca_geracao_nem_retry(self):
        a = await observe(income=[])
        b = await observe(income=[], now_ms=fx.T0 + 1_000)
        self.assertNotEqual(a["observation_id"], b["observation_id"])
        first = apply_observation(fx.visao()["execution_accounting"], a)
        current = apply_observation(first, b)
        replay = apply_observation(current, a)
        for field in ("generation", "last_observation_id", "attempts",
                      "next_retry_at", "last_error", "state", "conflicts",
                      "fills", "orders", "fee_conversions"):
            with self.subTest(campo=field):
                self.assertEqual(replay.get(field), current.get(field),
                                 "reaplicar A depois de B continua sendo replay")


if __name__ == "__main__":
    unittest.main()
