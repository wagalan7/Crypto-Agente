"""Lote 01 — correção conjunta dos cinco grupos da revisão de `e995b63b`.

Testes COMPORTAMENTAIS pelos callers reais (`collect_trade_accounting`,
`merge_observation`, `finalize_accounting`, `compute_totals`). Só transporte e
mercado são falsos; normalização, validação, merge, retry e projeção são os
oficiais. Sem rede, banco externo ou credencial.

REDs reproduzidos na baseline `e995b63b` (cada um aceitava dinheiro indevido ou
bloqueava um caso solucionável):

- R1 ledger com `symbol`/`incomeType`/sinal/`time`/`tranId` errados confirmava
  0.42 e `net_trade=9.53`;
- R2 prova íntegra com conta/exchange/`fill_time` divergentes, ou janela que não
  contém o fill, era RESOLVED;
- R3 duas coletas do MESMO snapshot aberto com `now=T` e `T+1s` viravam CONFLICT
  só pela janela; ESTIMATED→CONFIRMED também conflitava;
- R4 comissão exatamente 0 BNB exigia conversão e deixava `net_trade=None`;
- R5 49 conversões disponíveis em lotes de oito terminavam com 48 convertidas,
  `attempts=6`, FAILED e `retry_due=False`.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido na correção do Lote 01"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


from services import execution_accounting_service as ea      # noqa: E402

ESCOPO = "a" * 64
T0 = 1_790_000_000_000          # instante base (passado)
ABERTURA = T0 - 3_600_000
FECHAMENTO = T0 - 60_000
ENTRADA_MS = ABERTURA + 1_000
SAIDA_MS = FECHAMENTO - 1_000


# ════════════════════════════════════════════════════════════════════════════
#  Borda falsa da exchange (apenas transporte)
# ════════════════════════════════════════════════════════════════════════════
class ClienteFalso:
    """Transporte falso: userTrades, GET de ordem e ledger de income."""

    def __init__(self, *, fills, orders, income=None, escopo=ESCOPO):
        self.fills = list(fills)
        self.orders = {o["orderId"]: o for o in orders}
        self.income = list(income or [])
        self.escopo = escopo
        self.chamadas = {"executions": 0, "order": 0, "income": 0}
        self.income_requests: list = []

    def accounting_scope(self):
        return self.escopo

    async def get_executions(self, symbol, limit=None, start_time=None,
                             end_time=None):
        self.chamadas["executions"] += 1
        janela = [f for f in self.fills
                  if start_time <= int(f["time"]) <= end_time]
        return {"ok": True, "raw": janela, "limit": limit}

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        self.chamadas["order"] += 1
        if order_id is not None:
            bruto = self.orders.get(str(order_id))
        else:
            bruto = next((o for o in self.orders.values()
                          if o.get("clientOrderId") == client_order_id), None)
        if bruto is None:
            return {"ok": False, "error": "ORDER_NOT_FOUND"}
        return {"ok": True, "raw": bruto}

    async def get_income(self, symbol, income_type=None, start_time=None,
                         end_time=None, limit=None):
        self.chamadas["income"] += 1
        self.income_requests.append({"income_type": income_type,
                                     "start_time": start_time,
                                     "end_time": end_time})
        pagina = [i for i in self.income
                  if str(i.get("incomeType")) == str(income_type)
                  and start_time <= int(i["time"]) <= end_time]
        return {"ok": True, "income": pagina, "limit": limit or 1000}


def fill_bruto(exec_id, *, side, price, qty, realized, commission, asset,
               order_id, instante):
    return {"id": exec_id, "orderId": order_id, "symbol": "BTCUSDT",
            "positionSide": "BOTH", "side": side, "price": price, "qty": qty,
            "realizedPnl": realized, "commission": commission,
            "commissionAsset": asset, "time": str(instante)}


def ordem_bruta(order_id, *, side, reduce_only=False, qty="1"):
    return {"orderId": order_id, "symbol": "BTCUSDT", "side": side,
            "positionSide": "BOTH", "status": "FILLED", "type": "MARKET",
            "reduceOnly": reduce_only, "executedQty": qty,
            "clientOrderId": ("cw-entry-l01" if not reduce_only
                              else f"cw-exit-{order_id}"),
            "updateTime": FECHAMENTO}


def income_commission(**mudancas):
    """Lançamento de COMMISSION com vínculo EXPLÍCITO do ativo estrangeiro."""
    base = {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
            "income": "-0.42", "asset": "USDT", "tradeId": "7001",
            "tranId": "9001", "time": ENTRADA_MS,
            # Vínculo inequívoco com a comissão estrangeira daquele fill.
            "commissionAsset": "BNB", "commission": "0.001"}
    base.update(mudancas)
    return base


def _dt(instante_ms: int) -> datetime:
    """Mesmo formato do ORM: datetime UTC (o coletor converte via `_ms`)."""
    return datetime.fromtimestamp(instante_ms / 1000.0, tz=timezone.utc)


def visao(*, accounting=None, status="closed", exclusive=True, flat=True):
    return {"id": 1, "symbol": "BTC/USDT:USDT", "side": "long",
            "exchange": "binance", "exchange_order_id": "o7001",
            "client_order_id": "cw-entry-l01", "position_side": "BOTH",
            "planned_stop": 95.0, "opened_at": _dt(ABERTURA),
            "closed_at": _dt(FECHAMENTO), "status": status,
            "position_flat": flat, "exclusive_exposure": exclusive,
            "execution_accounting": accounting or ea.empty_accounting(
                identity={"exchange": "binance", "symbol": "BTCUSDT",
                          "side": "long", "position_side": "BOTH",
                          "entry_order_id": "o7001",
                          "entry_client_order_id": "cw-entry-l01",
                          "account_scope": ESCOPO})}


def conjunto_padrao(*, commission_entrada="0.001", asset_entrada="BNB"):
    """Entrada com comissão estrangeira + saída com comissão na liquidação."""
    fills = [
        fill_bruto("7001", side="BUY", price="100", qty="1", realized="0",
                   commission=commission_entrada, asset=asset_entrada,
                   order_id="o7001", instante=ENTRADA_MS),
        fill_bruto("7002", side="SELL", price="110", qty="1", realized="10",
                   commission="0.05", asset="USDT", order_id="o7002",
                   instante=SAIDA_MS),
    ]
    orders = [ordem_bruta("o7001", side="BUY"),
              ordem_bruta("o7002", side="SELL", reduce_only=True)]
    return fills, orders


async def coletar(cliente, *, accounting=None, now_ms=T0, **kwargs):
    """Chama o coletor REAL com relógio simulado."""
    from datetime import datetime, timezone
    agora = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    return await ea.collect_trade_accounting(
        visao(accounting=accounting, **kwargs), client=cliente,
        budget=ea.ReadBudget(seconds=30, calls=200), now=agora)


def totais(acc):
    return acc.get("totals") or {}


class R1LedgerNormalizadoEValidado(unittest.IsolatedAsyncioTestCase):
    """Cada incompatibilidade do lançamento tem recusa própria."""

    async def _net_trade(self, income):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders, income=income)
        acc = await coletar(cliente)
        return acc, totais(acc).get("net_trade")

    async def test_lancamento_valido_confirma_o_valor_correto(self):
        acc, net = await self._net_trade([income_commission()])
        self.assertEqual(net, "9.53", str(totais(acc))[:300])
        self.assertEqual(totais(acc)["fee_conversion_state"],
                         ea.FEE_CONVERSION_RESOLVED)

    async def test_incompatibilidades_nunca_confirmam(self):
        casos = [
            ("symbol_errado", {"symbol": "ETHUSDT"}),
            ("tipo_errado", {"incomeType": "REALIZED_PNL"}),
            ("credito_positivo", {"income": "0.42"}),
            ("time_fora_da_janela", {"time": ABERTURA - 10_000_000}),
            ("tran_id_ausente", {"tranId": None}),
            ("trade_id_ausente", {"tradeId": None}),
            ("trade_id_de_outro_fill", {"tradeId": "7002"}),
            ("ativo_errado", {"asset": "BUSD"}),
            ("income_nao_finito", {"income": "nan"}),
            ("trade_id_bool", {"tradeId": True}),
            ("sem_vinculo_estrangeiro", {"commissionAsset": None,
                                         "commission": None}),
            ("vinculo_de_outro_ativo", {"commissionAsset": "BUSD"}),
            ("vinculo_de_outra_quantidade", {"commission": "0.002"}),
        ]
        for rotulo, mudanca in casos:
            with self.subTest(caso=rotulo):
                acc, net = await self._net_trade([income_commission(**mudanca)])
                self.assertIsNone(net, f"{rotulo} confirmou {net}")
                self.assertEqual(totais(acc)["net_trade_reason_code"],
                                 "FEE_ASSET_CONVERSION_UNAVAILABLE", rotulo)
                self.assertEqual(totais(acc)["fee_assets_unconverted"], ["BNB"])

    async def test_duplicata_do_mesmo_tran_id_e_no_op(self):
        acc, net = await self._net_trade([income_commission(),
                                          income_commission()])
        self.assertEqual(net, "9.53", "duplicata real não duplica a taxa")

    async def test_lancamentos_distintos_para_o_mesmo_fill_bloqueiam(self):
        acc, net = await self._net_trade([
            income_commission(tranId="9001", income="-0.42"),
            income_commission(tranId="9002", income="-0.50")])
        self.assertIsNone(net, "vínculo ambíguo não escolhe um lançamento")

    async def test_mesmo_tran_id_com_conteudo_diferente_e_conflito_de_fonte(self):
        acc, net = await self._net_trade([
            income_commission(tranId="9001", income="-0.42"),
            income_commission(tranId="9001", income="-0.99")])
        self.assertIsNone(net)

    async def test_referencia_normalizada_do_lancamento_e_persistida(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        acc = await coletar(cliente)
        chave = ea.fill_key("binance", "BTCUSDT", "BOTH", "7001")
        prova = (acc.get("fee_conversions") or {})[chave]
        ref = prova.get("source_ref") or {}
        for campo in ("source", "account_scope", "exchange", "symbol",
                      "income_type", "trade_id", "tran_id", "asset", "income",
                      "time_ms", "window_start_ms", "window_end_ms"):
            self.assertIn(campo, ref, campo)
        self.assertEqual(ref["income_type"], "COMMISSION")
        self.assertEqual(ref["tran_id"], "9001")
        self.assertEqual(ref["account_scope"], ESCOPO)


class R2ContextoEsperadoEmTodasAsFronteiras(unittest.IsolatedAsyncioTestCase):
    """Hash íntegro não prova conta/origem/janela."""

    def _contexto(self, **mudancas):
        base = dict(account_scope=ESCOPO, exchange="binance",
                    market="usdm_futures", symbol="BTCUSDT", quote="USDT",
                    position_side="BOTH",
                    fill_key=ea.fill_key("binance", "BTCUSDT", "BOTH", "7001"),
                    exec_id="7001", fill_time_ms=ENTRADA_MS,
                    commission_asset="BNB", commission_qty=Decimal("0.001"),
                    settlement_asset="USDT")
        base.update(mudancas)
        return base

    def _prova(self, *, contexto=None, **mudancas):
        ctx = contexto or self._contexto()
        campos = dict(
            expected=ctx, settlement_value="0.42", price="420",
            source=ea.FEE_SOURCE_BROKER,
            source_ref={"source": ea.LEDGER_SOURCE, "account_scope": ESCOPO,
                        "exchange": "binance", "symbol": "BTCUSDT",
                        "income_type": "COMMISSION", "trade_id": "7001",
                        "tran_id": "9001", "asset": "USDT", "income": "-0.42",
                        "time_ms": ENTRADA_MS,
                        "window_start_ms": ABERTURA,
                        "window_end_ms": FECHAMENTO},
            observed_start_ms=ABERTURA, observed_end_ms=FECHAMENTO,
            now_ms=T0)
        campos.update(mudancas)
        return ea.build_fee_conversion(**campos)

    def test_prova_valida_confirma(self):
        prova = self._prova()
        self.assertTrue(prova.get("ok"), prova)
        veredito = ea.fee_conversion_verdict(
            {k: v for k, v in prova.items() if k != "ok"},
            expected=self._contexto())
        self.assertEqual(veredito["quality"], ea.FEE_QUALITY_CONFIRMED)

    def test_contexto_divergente_nao_confirma_mesmo_com_hash_integro(self):
        prova = {k: v for k, v in self._prova().items() if k != "ok"}
        divergentes = [
            ("conta", {"account_scope": "b" * 64}),
            ("exchange", {"exchange": "bybit"}),
            ("symbol", {"symbol": "ETHUSDT"}),
            ("fill_time", {"fill_time_ms": ENTRADA_MS + 5_000}),
            ("fill_key", {"fill_key": "outro"}),
            ("exec_id", {"exec_id": "9999"}),
            ("ativo", {"commission_asset": "BUSD"}),
            ("qty", {"commission_qty": Decimal("0.002")}),
            ("liquidacao", {"settlement_asset": "BUSD"}),
        ]
        for rotulo, mudanca in divergentes:
            with self.subTest(caso=rotulo):
                veredito = ea.fee_conversion_verdict(
                    prova, expected=self._contexto(**mudanca))
                self.assertEqual(veredito["quality"],
                                 ea.FEE_QUALITY_UNAVAILABLE, rotulo)

    def test_janela_que_nao_contem_o_fill_nao_nasce(self):
        prova = self._prova(observed_start_ms=FECHAMENTO - 5_000,
                            observed_end_ms=FECHAMENTO)
        self.assertFalse(prova.get("ok"), prova)
        self.assertEqual(prova["reason_code"], ea.FEE_REASON_INVALID)

    def test_sem_contexto_a_comissao_estrangeira_fica_indisponivel(self):
        veredito = ea.fee_conversion_verdict(
            {k: v for k, v in self._prova().items() if k != "ok"},
            expected=None)
        self.assertEqual(veredito["quality"], ea.FEE_QUALITY_UNAVAILABLE)
        self.assertEqual(veredito["reason_code"], ea.FEE_REASON_NO_CONTEXT)

    def test_prova_da_versao_antiga_fica_nao_verificada(self):
        """JSON antigo sem `source_ref`/contexto: preservado, nunca promovido."""
        antiga = {"contract_version": "R05E_FEE_CONVERSION_V1",
                  "fill_key": self._contexto()["fill_key"], "fee_asset": "BNB",
                  "fee_qty": "0.001", "settlement_asset": "USDT",
                  "settlement_value": "0.42", "price": "420",
                  "source": ea.FEE_SOURCE_BROKER,
                  "quality": ea.FEE_QUALITY_CONFIRMED,
                  "fill_time_ms": ENTRADA_MS, "observed_start_ms": ABERTURA,
                  "observed_end_ms": FECHAMENTO, "hash": "x" * 64}
        veredito = ea.fee_conversion_verdict(antiga, expected=self._contexto())
        self.assertEqual(veredito["quality"], ea.FEE_QUALITY_UNAVAILABLE)
        self.assertEqual(veredito["reason_code"], ea.FEE_REASON_UNVERIFIED)


class R3MaterialidadeEPrecedencia(unittest.IsolatedAsyncioTestCase):
    """Reobservação equivalente não é conflito; precedência é explícita."""

    async def test_duas_coletas_do_mesmo_snapshot_aberto_nao_conflitam(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        primeira = await coletar(cliente, status="open", flat=False, now_ms=T0)
        segunda = await coletar(cliente, status="open", flat=False,
                                now_ms=T0 + 1_000)
        # Aplicação na ordem natural e na ordem invertida: nenhuma delas pode
        # virar conflito econômico por mudança de janela/observed_at.
        fundido = ea.merge_observation(primeira, segunda, view=visao(
            accounting=primeira, status="open", flat=False))
        invertido = ea.merge_observation(segunda, primeira, view=visao(
            accounting=segunda, status="open", flat=False))
        for rotulo, resultado in (("direta", fundido), ("invertida", invertido)):
            self.assertIsNotNone(resultado, rotulo)
            self.assertFalse([c for c in (resultado.get("conflicts") or [])
                              if c.get("kind") == "FEE_CONVERSION"],
                             f"{rotulo}: {resultado.get('conflicts')}")

    async def test_promocao_de_estimada_para_confirmada(self):
        contexto = R2ContextoEsperadoEmTodasAsFronteiras()._contexto()
        estimada = ea.build_fee_conversion(
            expected=contexto, settlement_value="0.40", price="400",
            source=ea.FEE_SOURCE_HISTORICAL, price_basis="KLINE_1M_CLOSE",
            observed_start_ms=ABERTURA, observed_end_ms=FECHAMENTO, now_ms=T0)
        confirmada = R2ContextoEsperadoEmTodasAsFronteiras()._prova()
        acc = ea.empty_accounting(identity={"account_scope": ESCOPO})
        acc = ea.merge_accounting(acc, fee_conversions=[estimada],
                                  fee_context={contexto["fill_key"]: contexto})
        promovido = ea.merge_accounting(
            acc, fee_conversions=[confirmada],
            fee_context={contexto["fill_key"]: contexto})
        prova = promovido["fee_conversions"][contexto["fill_key"]]
        self.assertEqual(prova["quality"], ea.FEE_QUALITY_CONFIRMED)
        self.assertTrue(prova.get("superseded"), "histórico preservado")
        self.assertEqual([], [c for c in promovido["conflicts"]
                              if c.get("kind") == "FEE_CONVERSION"])

    async def test_estimada_atrasada_nao_rebaixa_confirmada(self):
        contexto = R2ContextoEsperadoEmTodasAsFronteiras()._contexto()
        confirmada = R2ContextoEsperadoEmTodasAsFronteiras()._prova()
        estimada = ea.build_fee_conversion(
            expected=contexto, settlement_value="0.40", price="400",
            source=ea.FEE_SOURCE_HISTORICAL, price_basis="KLINE_1M_CLOSE",
            observed_start_ms=ABERTURA, observed_end_ms=FECHAMENTO, now_ms=T0)
        acc = ea.merge_accounting(
            ea.empty_accounting(identity={"account_scope": ESCOPO}),
            fee_conversions=[confirmada],
            fee_context={contexto["fill_key"]: contexto})
        depois = ea.merge_accounting(
            acc, fee_conversions=[estimada],
            fee_context={contexto["fill_key"]: contexto})
        prova = depois["fee_conversions"][contexto["fill_key"]]
        self.assertEqual(prova["quality"], ea.FEE_QUALITY_CONFIRMED)
        self.assertEqual(prova["settlement_value"], "0.42")
        self.assertEqual([], [c for c in depois["conflicts"]
                              if c.get("kind") == "FEE_CONVERSION"])

    async def test_divergencia_material_entre_confirmadas_continua_conflito(self):
        contexto = R2ContextoEsperadoEmTodasAsFronteiras()._contexto()
        uma = R2ContextoEsperadoEmTodasAsFronteiras()._prova()
        outra = R2ContextoEsperadoEmTodasAsFronteiras()._prova(
            settlement_value="0.99", price="990",
            source_ref={"source": ea.LEDGER_SOURCE, "account_scope": ESCOPO,
                        "exchange": "binance", "symbol": "BTCUSDT",
                        "income_type": "COMMISSION", "trade_id": "7001",
                        "tran_id": "9002", "asset": "USDT", "income": "-0.99",
                        "time_ms": ENTRADA_MS, "window_start_ms": ABERTURA,
                        "window_end_ms": FECHAMENTO})
        acc = ea.merge_accounting(
            ea.empty_accounting(identity={"account_scope": ESCOPO}),
            fee_conversions=[uma],
            fee_context={contexto["fill_key"]: contexto})
        depois = ea.merge_accounting(
            acc, fee_conversions=[outra],
            fee_context={contexto["fill_key"]: contexto})
        self.assertEqual(
            depois["fee_conversions"][contexto["fill_key"]]["settlement_value"],
            "0.42", "original preservada")
        self.assertTrue([c for c in depois["conflicts"]
                         if c.get("kind") == "FEE_CONVERSION"])


class R4ZeroEstrangeiroComprovado(unittest.IsolatedAsyncioTestCase):
    """Zero comprovado é custo zero em QUALQUER ativo válido."""

    async def test_zero_bnb_nao_exige_conversao_nem_consulta(self):
        fills, orders = conjunto_padrao(commission_entrada="0")
        cliente = ClienteFalso(fills=fills, orders=orders, income=[])
        acc = await coletar(cliente)
        self.assertEqual(totais(acc)["net_trade"], "9.95", str(totais(acc))[:280])
        self.assertEqual(totais(acc)["fee_assets_unconverted"], [])
        self.assertEqual(totais(acc)["fee_conversion_required"], [])
        # A varredura de FUNDING continua (é exigência própria); o que NÃO pode
        # existir é consulta ao ledger de COMMISSION por conversão de taxa.
        self.assertEqual(
            [r for r in cliente.income_requests
             if r["income_type"] == "COMMISSION"], [],
            "zero comprovado não consulta ledger de conversão")
        self.assertEqual(totais(acc)["fees_by_asset"].get("BNB"), "0")

    async def test_ausente_e_invalida_nunca_viram_zero(self):
        for rotulo, valor in (("ausente", None), ("nan", "nan"),
                              ("inf", "inf"), ("negativa", "-0.001")):
            with self.subTest(caso=rotulo):
                fills, orders = conjunto_padrao(commission_entrada=valor)
                cliente = ClienteFalso(fills=fills, orders=orders, income=[])
                acc = await coletar(cliente)
                self.assertIsNone(totais(acc).get("net_trade"), rotulo)

    async def test_mistura_de_zero_e_positiva_exige_so_a_positiva(self):
        fills, orders = conjunto_padrao(commission_entrada="0")
        fills.append(fill_bruto("7003", side="SELL", price="110", qty="1",
                                realized="0", commission="0.002", asset="BNB",
                                order_id="o7002", instante=SAIDA_MS + 1))
        cliente = ClienteFalso(fills=fills, orders=orders, income=[])
        acc = await coletar(cliente)
        exigidas = totais(acc)["fee_conversion_required"]
        self.assertEqual(len(exigidas), 1, exigidas)
        self.assertIn("7003", exigidas[0])
        self.assertIsNone(totais(acc).get("net_trade"))


class R5ProgressoLoteEConcorrencia(unittest.IsolatedAsyncioTestCase):
    """Lote de oito avança até confirmar, sem gastar tentativas de falha."""

    def _cenario(self, quantas):
        fills = [fill_bruto("7000", side="BUY", price="100", qty=str(quantas),
                            realized="0", commission="0.05", asset="USDT",
                            order_id="o7001", instante=ENTRADA_MS)]
        orders = [ordem_bruta("o7001", side="BUY", qty=str(quantas)),
                  ordem_bruta("o7002", side="SELL", reduce_only=True,
                              qty=str(quantas))]
        income = []
        for i in range(quantas):
            exec_id = str(8000 + i)
            fills.append(fill_bruto(
                exec_id, side="SELL", price="110", qty="1",
                realized="10" if i == 0 else "0", commission="0.001",
                asset="BNB", order_id="o7002", instante=SAIDA_MS + i))
            income.append(income_commission(
                tradeId=exec_id, tranId=f"9{i:04d}", income="-0.01",
                time=SAIDA_MS + i, commission="0.001"))
        return fills, orders, income

    async def test_49_conversoes_completam_sem_failed(self):
        fills, orders, income = self._cenario(49)
        cliente = ClienteFalso(fills=fills, orders=orders, income=income)
        acc = None
        passes = 0
        relogio = T0                    # relógio SIMULADO (nunca `sleep`)
        while passes < 12:
            passes += 1
            if acc is not None:
                # O passe seguinte respeita o `next_retry_at` persistido: o
                # relógio avança ATÉ a hora marcada, nunca antes.
                marcado = acc.get("next_retry_at")
                self.assertIsInstance(marcado, str,
                                      "espera cadenciada tem hora")
                instante = datetime.fromisoformat(marcado)
                self.assertFalse(
                    ea.is_retry_due(acc, now=instante - timedelta(seconds=1)),
                    "não colhe antes da hora marcada")
                self.assertTrue(ea.is_retry_due(acc, now=instante),
                                "colhe quando a espera vence")
                proximo = int(instante.timestamp() * 1000)
                self.assertGreater(proximo, relogio, "sem retry imediato")
                relogio = proximo
            novo = await coletar(cliente, accounting=acc, now_ms=relogio)
            acc = novo if acc is None else ea.merge_observation(
                acc, novo, view=visao(accounting=acc))
            self.assertIsNotNone(acc, "merge sob contrato não pode descartar")
            if (acc.get("totals") or {}).get("net_trade") is not None:
                break
        confirmadas = len(acc.get("fee_conversions") or {})
        self.assertEqual(confirmadas, 49, f"passes={passes}")
        self.assertEqual(acc["state"], ea.STATE_CONFIRMED,
                         f"{acc['state']}/{acc.get('reason_code')}")
        self.assertLessEqual(passes, 7, "sete passes úteis bastam")
        self.assertEqual(int(acc.get("attempts") or 0), 0,
                         "incompletude saudável por lote não é falha")
        self.assertNotEqual(acc.get("reason_code"), "RETRY_BUDGET_EXHAUSTED")

    async def test_limite_de_oito_por_passe_e_respeitado(self):
        fills, orders, income = self._cenario(9)
        cliente = ClienteFalso(fills=fills, orders=orders, income=income)
        primeiro = await coletar(cliente)
        self.assertEqual(len(primeiro.get("fee_conversions") or {}), 8,
                         "oito por passe")
        self.assertEqual(int(primeiro.get("attempts") or 0), 0)
        segundo = await coletar(cliente, accounting=primeiro,
                                now_ms=T0 + 1_000)
        fundido = ea.merge_observation(primeiro, segundo,
                                       view=visao(accounting=primeiro))
        self.assertEqual(len(fundido.get("fee_conversions") or {}), 9)
        self.assertEqual(fundido["state"], ea.STATE_CONFIRMED)

    async def test_fonte_sem_registro_mantem_retry_finito_e_motivo_honesto(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders, income=[])
        acc = await coletar(cliente)
        self.assertIsNone(totais(acc).get("net_trade"))
        self.assertEqual(acc.get("fee_conversion_state"),
                         ea.FEE_COLLECT_SOURCE_UNAVAILABLE)
        self.assertGreaterEqual(int(acc.get("attempts") or 0), 1,
                                "fonte ausente é espera com retry finito")

    async def test_replay_da_mesma_observacao_nao_conta_duas_vezes(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders, income=[])
        primeiro = await coletar(cliente)
        tentativas = int(primeiro.get("attempts") or 0)
        replay = ea.merge_observation(primeiro, primeiro,
                                      view=visao(accounting=primeiro))
        self.assertEqual(int(replay.get("attempts") or 0), tentativas,
                         "mesma observação reaplicada não consome o contador")


class MatrizFinalDeFechamento(unittest.IsolatedAsyncioTestCase):
    """Casos CRUZADOS do §8: integridade × materialidade × progresso."""

    # ── 3. adulteração re-hasheada ainda falha se o contexto difere ─────────
    async def test_adulterada_e_rehasheada_nao_passa_pelo_contexto(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        acc = await coletar(cliente)
        chave, prova = next(iter((acc["fee_conversions"]).items()))
        forjada = {**prova, "account_scope": "b" * 64}
        forjada["material_id"] = ea.fee_conversion_material_id(forjada)
        forjada["integrity_hash"] = ea.fee_conversion_hash(forjada)
        contexto = ea.fee_expected_contexts(acc)[chave]
        v = ea.fee_conversion_verdict(forjada, expected=contexto)
        self.assertEqual(v["quality"], ea.FEE_QUALITY_UNAVAILABLE,
                         "hash recalculado não compra contexto")
        # A prova legítima guardada continua valendo e a forjada é recusada.
        depois = ea.merge_accounting(acc, fee_conversions=[forjada],
                                     fee_context={chave: contexto})
        self.assertEqual(depois["fee_conversions"][chave]["account_scope"],
                         ESCOPO)
        self.assertEqual((depois.get("fee_merge_outcomes") or {}).get(chave),
                         "REJECTED")

    # ── 5. reinício: ida e volta pelo JSON não muda nada ────────────────────
    async def test_reinicio_pelo_json_e_idempotente(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        acc = await coletar(cliente)
        persistida = json.loads(json.dumps(acc))
        refundida = ea.merge_observation(persistida, persistida,
                                         view=visao(accounting=persistida))
        self.assertEqual(refundida["state"], ea.STATE_CONFIRMED)
        self.assertEqual(Decimal(totais(refundida)["net_trade"]),
                         Decimal("9.53"))
        self.assertEqual(len(refundida["fee_conversions"]), 1)
        self.assertEqual(refundida["conflicts"], [])

    # ── 6. conjunto VAZIO de fills não tem P&L conhecido ───────────────────
    async def test_sem_fills_nao_ha_pnl_nem_exigencia(self):
        orders = [ordem_bruta("o7001", side="BUY")]
        cliente = ClienteFalso(fills=[], orders=orders, income=[])
        acc = await coletar(cliente)
        self.assertIsNone(totais(acc).get("net_trade"))
        self.assertEqual(totais(acc).get("fee_conversion_required"), [])
        self.assertFalse(ea.accounting_is_confirmed(acc))

    # ── 7. 1 e 9 conversões: cadência e completude ──────────────────────────
    async def test_uma_conversao_fecha_em_um_passe(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        acc = await coletar(cliente)
        self.assertEqual(acc.get("fee_conversion_state"),
                         ea.FEE_COLLECT_COMPLETE)
        self.assertEqual(acc["state"], ea.STATE_CONFIRMED)
        self.assertEqual(len([c for c in cliente.income_requests
                              if c["income_type"] == "COMMISSION"]), 1)

    # ── 9. falha ATRASADA não faz a estatística regredir ───────────────────
    async def test_falha_atrasada_nao_rebaixa_progresso(self):
        fills, orders = conjunto_padrao()
        sem_fonte = ClienteFalso(fills=fills, orders=orders, income=[])
        falha = await coletar(sem_fonte)             # observação da geração 0
        com_fonte = ClienteFalso(fills=fills, orders=orders,
                                 income=[income_commission()])
        progresso = await coletar(com_fonte)         # TAMBÉM da geração 0
        linha = visao()["execution_accounting"]
        atual = ea.merge_observation(linha, progresso, view=visao())
        self.assertEqual(atual["state"], ea.STATE_CONFIRMED)
        self.assertEqual(int(atual.get("generation") or 0), 1)
        tardia = ea.merge_observation(atual, falha, view=visao(accounting=atual))
        self.assertEqual(tardia["state"], ea.STATE_CONFIRMED,
                         "falha de geração anterior não rebaixa confirmação")
        self.assertEqual(int(tardia.get("attempts") or 0), 0,
                         "snapshot obsoleto não ressuscita tentativas")
        self.assertIsNone(tardia.get("last_error"))
        self.assertEqual(Decimal(totais(tardia)["net_trade"]), Decimal("9.53"))

    async def test_replay_de_falha_nao_consome_o_contador_duas_vezes(self):
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders, income=[])
        falha = await coletar(cliente)
        linha = visao()["execution_accounting"]
        primeira = ea.merge_observation(linha, falha, view=visao())
        tentativas = int(primeira.get("attempts") or 0)
        self.assertGreaterEqual(tentativas, 1)
        segunda = ea.merge_observation(primeira, falha,
                                       view=visao(accounting=primeira))
        self.assertEqual(int(segunda.get("attempts") or 0), tentativas)
        self.assertEqual(int(segunda.get("generation") or 0),
                         int(primeira.get("generation") or 0),
                         "replay não avança a geração")

    async def test_tentativas_antigas_nao_matam_um_passe_util(self):
        """Progresso ÚTIL zera o contador — não vira FAILED por herança."""
        fills, orders = conjunto_padrao()
        cliente = ClienteFalso(fills=fills, orders=orders,
                               income=[income_commission()])
        colhida = await coletar(cliente)
        linha = {**visao()["execution_accounting"],
                 "attempts": ea.MAX_ATTEMPTS, "last_error": "RATE_LIMIT"}
        fundida = ea.merge_observation(linha, colhida, view=visao())
        self.assertEqual(fundida["state"], ea.STATE_CONFIRMED)
        self.assertEqual(int(fundida.get("attempts") or 0), 0)
        self.assertNotEqual(fundida.get("reason_code"),
                            "RETRY_BUDGET_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
