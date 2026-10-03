"""Lote 01 — comissão paga em OUTRO ativo e o total que ela desbloqueia.

Testes herméticos (sem rede, banco ou credencial) do contrato de conversão
versionado por fill, da sua persistência idempotente no JSON existente e do
efeito no consumidor do total (R05D).

ANTES (baseline 7d6dbe15): uma comissão em BNB deixava `net_trade=None` com
`FEE_ASSET_CONVERSION_UNAVAILABLE` e NÃO havia caminho verificável para
resolvê-la — a linha ficava fora de `accounting_total` para sempre.
DEPOIS: evidência por fill com fonte/qualidade/hash; só conversão REGISTRADA
pela corretora confirma, estimativa não vira dinheiro, e a linha entra no total
quando todas as comissões exigidas estão confirmadas.
"""
from __future__ import annotations

import json
import socket
import sys
import unittest
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_REAL_GETADDRINFO = socket.getaddrinfo


def setUpModule():
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("DNS proibido no Lote 01"))


def tearDownModule():
    socket.getaddrinfo = _REAL_GETADDRINFO


from services import execution_accounting_service as ea      # noqa: E402
from services import financial_total_service as fts          # noqa: E402

ESCOPO = "a" * 64
AGORA_MS = 1_790_000_000_000
EXEC_ENTRADA = "7001"
EXEC_SAIDA = "7002"


def fill(exec_id, *, side, price, qty, realized, commission, asset, time_ms):
    """Fill JÁ normalizado (mesmo formato que `normalize_fill` devolve)."""
    bruto = {"id": exec_id, "orderId": f"o{exec_id}", "symbol": "BTCUSDT",
             "positionSide": "BOTH", "side": side, "price": price, "qty": qty,
             "realizedPnl": realized, "commission": commission,
             "commissionAsset": asset, "time": str(time_ms)}
    normalizado, motivo = ea.normalize_fill(bruto, exchange="binance")
    assert normalizado is not None, motivo
    return normalizado


def entrada(**extra):
    base = dict(side="BUY", price="100", qty="1", realized="0",
                commission="0.001", asset="BNB", time_ms=AGORA_MS - 60_000)
    base.update(extra)
    return fill(EXEC_ENTRADA, **base)


def saida(**extra):
    base = dict(side="SELL", price="110", qty="1", realized="10",
                commission="0.05", asset="USDT", time_ms=AGORA_MS - 30_000)
    base.update(extra)
    return fill(EXEC_SAIDA, **base)


IDENTIDADE = {"exchange": "binance", "symbol": "BTCUSDT", "side": "long",
              "position_side": "BOTH", "entry_order_id": f"o{EXEC_ENTRADA}",
              "account_scope": ESCOPO}

#: Campos que no contrato V2 vivem no CONTEXTO ESPERADO (e não mais como
#: argumentos soltos do builder). A fixture traduz os nomes antigos para o
#: contexto, preservando cada garantia dos testes do Lote 01.
_PARA_CONTEXTO = {"account_scope": "account_scope", "exchange": "exchange",
                  "fill_key": "fill_key", "fee_asset": "commission_asset",
                  "fee_qty": "commission_qty", "fill_time_ms": "fill_time_ms"}


def contexto(**mudancas):
    """Contexto esperado derivado do fill ATRIBUÍDO (nunca da prova)."""
    base = ea.build_expected_context(identity=IDENTIDADE, fill=entrada())
    base = dict(base or {})
    base.update(mudancas)
    return base


def evidencia(**mudancas):
    ctx = {}
    for antigo, novo in _PARA_CONTEXTO.items():
        if antigo in mudancas:
            ctx[novo] = mudancas.pop(antigo)
    campos = dict(settlement_value="0.42", price="420",
                  source=ea.FEE_SOURCE_BROKER,
                  source_ref=REFERENCIA_FONTE,
                  observed_start_ms=AGORA_MS - 120_000,
                  observed_end_ms=AGORA_MS - 1_000, now_ms=AGORA_MS)
    campos.update(mudancas)
    return ea.build_fee_conversion(expected=contexto(**ctx), **campos)


#: Referência normalizada do lançamento de COMMISSION que registra a conversão.
REFERENCIA_FONTE = {
    "source": ea.LEDGER_SOURCE, "account_scope": ESCOPO, "exchange": "binance",
    "symbol": "BTCUSDT", "income_type": "COMMISSION", "asset": "USDT",
    "trade_id": EXEC_ENTRADA, "tran_id": "9001", "income": "-0.42",
    "time_ms": AGORA_MS - 60_000,
    "window_start_ms": AGORA_MS - 120_000, "window_end_ms": AGORA_MS - 1_000,
}


def veredito(prova, *, fee_asset=None, fee_qty=None, fill_key=None, **extra):
    """Veredito V2: o vínculo é conferido contra o CONTEXTO ESPERADO."""
    ctx = {}
    if fee_asset is not None:
        ctx["commission_asset"] = fee_asset
    if fee_qty is not None:
        ctx["commission_qty"] = fee_qty
    if fill_key is not None:
        ctx["fill_key"] = fill_key
    return ea.fee_conversion_verdict(prova, expected=contexto(**ctx), **extra)


def guardadas(*evidencias):
    return {e["fill_key"]: {k: v for k, v in e.items() if k != "ok"}
            for e in evidencias}


#: Vínculo EXPLÍCITO do lançamento com a comissão estrangeira do fill. No
#: contrato V2 o rótulo COMMISSION + asset de liquidação não prova, só, a
#: conversão: a linha precisa declarar qual comissão ela liquidou.
VINCULO = {"commissionAsset": "BNB", "commission": "0.001"}


async def coletar_conversoes(cliente, simbolo, exigidas, **kwargs):
    """Tupla antiga a partir do desfecho EXPLÍCITO do coletor V2."""
    r = await ea.collect_broker_fee_conversions(cliente, simbolo, exigidas,
                                               **kwargs)
    return r["evidences"], r["complete"], r["error"]


def contextos(*fills):
    """Mapa `fill_key → contexto esperado` dos fills ATRIBUÍDOS.

    No fluxo real quem monta isto é `fee_expected_contexts`; aqui a fixture
    reproduz a MESMA derivação para exercitar `compute_totals` direto.
    """
    mapa = {}
    for f in fills:
        ctx = ea.build_expected_context(identity=IDENTIDADE, fill=f)
        if ctx:
            mapa[ctx["fill_key"]] = ctx
    return mapa


class ContratoDaEvidencia(unittest.TestCase):
    """Construção, qualidade derivada e recusa de valor ilegítimo."""

    def test_conversao_registrada_pela_corretora_confirma(self):
        prova = evidencia()
        self.assertTrue(prova["ok"], prova)
        self.assertEqual(prova["quality"], ea.FEE_QUALITY_CONFIRMED)
        self.assertEqual(prova["contract_version"], ea.FEE_CONVERSION_CONTRACT)
        self.assertTrue(prova["integrity_hash"])

    def test_preco_de_mercado_e_no_maximo_estimado(self):
        prova = evidencia(source=ea.FEE_SOURCE_HISTORICAL,
                          price_basis="KLINE_1M_CLOSE")
        self.assertTrue(prova["ok"])
        self.assertEqual(prova["quality"], ea.FEE_QUALITY_ESTIMATED)

    def test_fonte_desconhecida_bloqueia_sem_numero(self):
        for fonte in ("", None, "PRECO_ATUAL", "CHUTE"):
            prova = evidencia(source=fonte)
            self.assertFalse(prova.get("ok"), fonte)
            self.assertEqual(prova["reason_code"], ea.FEE_BLOCKED_SOURCE)

    def test_valores_ilegitimos_nao_viram_evidencia(self):
        casos = [
            ("qty_bool", {"fee_qty": True}),
            ("qty_nan", {"fee_qty": float("nan")}),
            ("qty_inf", {"fee_qty": float("inf")}),
            ("qty_negativa", {"fee_qty": "-0.001"}),
            ("valor_negativo", {"settlement_value": "-0.42"}),
            ("valor_nan", {"settlement_value": float("nan")}),
            ("preco_zero", {"price": "0"}),
            ("preco_bool", {"price": True}),
            ("mesmo_ativo", {"fee_asset": "USDT"}),
            ("sem_ativo", {"fee_asset": ""}),
            ("sem_conta", {"account_scope": None}),
            ("sem_fill", {"fill_key": ""}),
            ("instante_futuro", {"fill_time_ms": AGORA_MS + 600_000}),
            ("janela_invertida", {"observed_start_ms": AGORA_MS,
                                  "observed_end_ms": AGORA_MS - 600_000}),
            ("janela_futura", {"observed_end_ms": AGORA_MS + 600_000}),
            ("sem_janela", {"observed_start_ms": None}),
        ]
        for rotulo, mudanca in casos:
            prova = evidencia(**mudanca)
            self.assertFalse(prova.get("ok"), rotulo)
            self.assertIn(prova["reason_code"],
                          (ea.FEE_REASON_INVALID, ea.FEE_BLOCKED_SOURCE,
                           ea.FEE_REASON_NO_CONTEXT), rotulo)

    def test_hash_sobrevive_ao_ida_e_volta_do_json(self):
        prova = {k: v for k, v in evidencia().items() if k != "ok"}
        ida_e_volta = json.loads(json.dumps(prova))
        ida_e_volta["fee_qty"] = 0.001          # numeric → float, como no JSONB
        v = veredito(ida_e_volta, fee_asset="BNB", fee_qty="0.001",
                     fill_key=entrada()["key"])
        self.assertEqual(v["quality"], ea.FEE_QUALITY_CONFIRMED,
                         "representação não pode invalidar prova legítima")

    def test_veredito_recusa_adulteracao_e_vinculo_errado(self):
        prova = {k: v for k, v in evidencia().items() if k != "ok"}
        adulterada = {**prova, "settlement_value": "999"}
        self.assertEqual(
            veredito(adulterada, fee_asset="BNB",
                                      fee_qty="0.001",
                                      fill_key=entrada()["key"])["reason_code"],
            ea.FEE_REASON_CONFLICT)
        # V2: descrever OUTRO contexto (fill, ativo ou quantidade) é prova
        # INVÁLIDA para esta comissão — não um conflito sobre o mesmo dinheiro.
        # A garantia preservada é a mesma: nunca confirma.
        for rotulo, kw in (("fill", {"fill_key": "outro"}),
                           ("ativo", {"fee_asset": "BUSD"}),
                           ("quantidade", {"fee_qty": "0.002"})):
            campos = {"fee_asset": "BNB", "fee_qty": "0.001",
                      "fill_key": entrada()["key"], **kw}
            v = veredito(prova, **campos)
            self.assertEqual(v["quality"], ea.FEE_QUALITY_UNAVAILABLE, rotulo)
            self.assertEqual(v["reason_code"], ea.FEE_REASON_INVALID, rotulo)

    def test_confirmed_forjado_com_fonte_de_mercado_e_recusado(self):
        """Ninguém declara CONFIRMED para preço de mercado."""
        forjada = {k: v for k, v in
                   evidencia(source=ea.FEE_SOURCE_HISTORICAL).items()
                   if k != "ok"}
        forjada["quality"] = ea.FEE_QUALITY_CONFIRMED
        forjada["integrity_hash"] = ea.fee_conversion_hash(forjada)
        v = veredito(forjada, fee_asset="BNB", fee_qty="0.001",
                     fill_key=entrada()["key"])
        self.assertEqual(v["quality"], ea.FEE_QUALITY_UNAVAILABLE)
        self.assertEqual(v["reason_code"], ea.FEE_REASON_INVALID)


class TotaisComComissaoEmOutroAtivo(unittest.TestCase):
    """`net_trade` só com TODAS as comissões exigidas confirmadas."""

    def _totais(self, *, fee_conversions=None, entrada_fill=None):
        entrada_usada = entrada_fill or entrada()
        return ea.compute_totals([entrada_usada], [saida()], [],
                                 funding_state=ea.FUNDING_PENDING,
                                 fee_conversions=fee_conversions,
                                 fee_context=contextos(entrada_usada, saida()))

    def test_sem_evidencia_continua_bloqueado(self):
        totais = self._totais()
        self.assertIsNone(totais["net_trade"])
        self.assertEqual(totais["net_trade_reason_code"],
                         "FEE_ASSET_CONVERSION_UNAVAILABLE")
        self.assertEqual(totais["fee_assets_unconverted"], ["BNB"])
        self.assertFalse(totais["fees_complete"])

    def test_estimativa_nao_vira_dinheiro(self):
        prova = evidencia(source=ea.FEE_SOURCE_HISTORICAL)
        totais = self._totais(fee_conversions=guardadas(prova))
        self.assertIsNone(totais["net_trade"])
        self.assertEqual(totais["net_trade_reason_code"],
                         ea.FEE_REASON_ESTIMATED)
        self.assertEqual(totais["fee_assets_unconverted"], ["BNB"])

    def test_conversao_confirmada_desbloqueia_o_net_trade(self):
        totais = self._totais(fee_conversions=guardadas(evidencia()))
        # gross 10 − taxa USDT 0.05 − conversão 0.42 = 9.53
        self.assertEqual(totais["net_trade"], "9.53")
        self.assertEqual(totais["fee_assets_unconverted"], [])
        self.assertTrue(totais["fees_complete"])
        self.assertEqual(totais["fee_conversion_state"],
                         ea.FEE_CONVERSION_RESOLVED)
        self.assertEqual(totais["fee_conversion_settlement_total"], "0.42")
        self.assertEqual(totais["fee_conversion_confirmed"], [entrada()["key"]])

    def test_uma_de_duas_comissoes_nao_resolve(self):
        outra = fill("7003", side="SELL", price="110", qty="1", realized="0",
                     commission="0.002", asset="BNB", time_ms=AGORA_MS - 20_000)
        totais = ea.compute_totals([entrada()], [saida(), outra], [],
                                   fee_conversions=guardadas(evidencia()),
                                   fee_context=contextos(entrada(), saida(),
                                                         outra))
        self.assertIsNone(totais["net_trade"])
        self.assertEqual(totais["net_trade_reason_code"],
                         "FEE_ASSET_CONVERSION_UNAVAILABLE")
        self.assertEqual(totais["fee_conversion_confirmed"], [entrada()["key"]])

    def test_conflito_de_evidencia_bloqueia_com_motivo_proprio(self):
        prova = {k: v for k, v in evidencia().items() if k != "ok"}
        prova["settlement_value"] = "999"       # hash não bate mais
        totais = self._totais(fee_conversions={prova["fill_key"]: prova})
        self.assertIsNone(totais["net_trade"])
        self.assertEqual(totais["net_trade_reason_code"], ea.FEE_REASON_CONFLICT)

    def test_comissao_zero_registrada_na_liquidacao_continua_valida(self):
        """Zero PROVADO é válido; ausência nunca vira zero."""
        zero = entrada(commission="0", asset="USDT")
        totais = ea.compute_totals([zero], [saida()], [])
        self.assertEqual(totais["net_trade"], "9.95")
        self.assertEqual(totais["fee_assets_unconverted"], [])
        self.assertEqual(totais["fee_conversion_state"],
                         ea.FEE_CONVERSION_NOT_REQUIRED)

    def test_comissao_ausente_nunca_vira_zero(self):
        sem_taxa = {**entrada(), "commission": None, "commission_asset": None}
        totais = ea.compute_totals([sem_taxa], [saida()], [])
        self.assertIsNone(totais["net_trade"])
        self.assertEqual(totais["net_trade_reason_code"], "FEES_INCOMPLETE")

    def test_duplicata_de_fill_nao_duplica_comissao(self):
        prova = evidencia()
        totais = ea.compute_totals([entrada(), dict(entrada())], [saida()], [],
                                   fee_conversions=guardadas(prova),
                                   fee_context=contextos(entrada(), saida()))
        self.assertEqual(totais["net_trade"], "9.53")


class PersistenciaDaEvidencia(unittest.TestCase):
    """Merge idempotente no JSON existente; divergência vira CONFLICT."""

    def test_merge_idempotente_e_conflito_preserva_original(self):
        acc = ea.empty_accounting(identity=IDENTIDADE)
        ctx = contextos(entrada())
        prova = evidencia()
        acc = ea.merge_accounting(acc, fee_conversions=[prova], fee_context=ctx)
        acc = ea.merge_accounting(acc, fee_conversions=[prova], fee_context=ctx)
        self.assertEqual(len(acc["fee_conversions"]), 1)
        self.assertEqual(acc["conflicts"], [])
        # Confirmada DIVERGENTE: outro lançamento da fonte (outro `tranId`,
        # outro valor) — é assim que duas confirmadas podem divergir de fato.
        divergente = evidencia(
            settlement_value="0.99", price="990",
            source_ref={**REFERENCIA_FONTE, "tran_id": "9002",
                        "income": "-0.99"})
        acc = ea.merge_accounting(acc, fee_conversions=[divergente],
                                  fee_context=ctx)
        self.assertEqual(acc["fee_conversions"][prova["fill_key"]]
                         ["settlement_value"], "0.42", "original preservado")
        # V2: o conflito é de MATERIALIDADE (duas confirmadas descrevendo
        # dinheiro diferente), não de representação do hash.
        self.assertIn({"kind": "FEE_CONVERSION", "key": prova["fill_key"],
                       "fields": ["material_id"],
                       "rejected_material_id": divergente["material_id"]},
                      acc["conflicts"])

    def test_evidencia_invalida_nao_entra_no_json(self):
        acc = ea.empty_accounting(identity=IDENTIDADE)
        acc = ea.merge_accounting(acc, fee_conversions=[evidencia(price="0")],
                                  fee_context=contextos(entrada()))
        self.assertEqual(acc["fee_conversions"], {})

    def test_exigencia_vem_dos_fills_atribuidos(self):
        acc = ea.empty_accounting(identity=IDENTIDADE)
        acc = ea.merge_accounting(acc, fills=[entrada(), saida()])
        exigidas = ea._fee_conversions_required(acc)
        self.assertEqual([i["fill_key"] for i in exigidas], [entrada()["key"]])
        self.assertEqual(exigidas[0]["fee_asset"], "BNB")
        self.assertEqual(exigidas[0]["fee_qty"], Decimal("0.001"))


class ColetorDaConversaoRegistrada(unittest.IsolatedAsyncioTestCase):
    """Uma varredura paginada do ledger de COMMISSION, sem N+1 e sem estimar."""

    def _cliente(self, paginas, *, limite=1000):
        chamadas = []

        class Falso:
            async def get_income(self, symbol, income_type=None, start_time=None,
                                 end_time=None, limit=None):
                chamadas.append({"income_type": income_type,
                                 "start_time": start_time})
                if not paginas:
                    return {"ok": True, "income": [], "limit": limite}
                return paginas.pop(0)

        return Falso(), chamadas

    def _exigidas(self):
        acc = ea.merge_accounting(ea.empty_accounting(identity=IDENTIDADE),
                                  fills=[entrada()])
        return ea._fee_conversions_required(acc)

    async def test_linha_do_ledger_vinculada_ao_fill_confirma(self):
        cliente, chamadas = self._cliente([{
            "ok": True, "limit": 1000, "income": [
                {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
                 "income": "-0.42", "asset": "USDT", "tradeId": EXEC_ENTRADA, **VINCULO,
                 "tranId": "9001", "time": AGORA_MS - 60_000}]}])
        evidencias, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertTrue(completo)
        self.assertIsNone(erro)
        self.assertEqual(len(evidencias), 1)
        self.assertEqual(evidencias[0]["quality"], ea.FEE_QUALITY_CONFIRMED)
        self.assertEqual(evidencias[0]["settlement_value"], "0.42")
        self.assertEqual(evidencias[0]["source"], ea.FEE_SOURCE_BROKER)
        self.assertEqual([c["income_type"] for c in chamadas], ["COMMISSION"],
                         "uma varredura, não uma chamada por fill")

    async def test_sem_registro_bloqueia_sem_estimar(self):
        cliente, _ = self._cliente([{"ok": True, "limit": 1000, "income": []}])
        evidencias, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertEqual(evidencias, [])
        self.assertFalse(completo)
        self.assertEqual(erro, ea.FEE_BLOCKED_SOURCE)

    async def test_ativo_errado_no_ledger_nao_confirma(self):
        cliente, _ = self._cliente([{
            "ok": True, "limit": 1000, "income": [
                {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
                 "income": "-0.001", "asset": "BNB", "tradeId": EXEC_ENTRADA, **VINCULO,
                 "tranId": "9002", "time": AGORA_MS - 60_000}]}])
        evidencias, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertEqual(evidencias, [])
        self.assertEqual(erro, ea.FEE_BLOCKED_SOURCE)

    async def test_ledger_divergente_para_o_mesmo_fill_e_erro(self):
        cliente, _ = self._cliente([{
            "ok": True, "limit": 1000, "income": [
                {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
                 "income": "-0.42", "asset": "USDT", "tradeId": EXEC_ENTRADA, **VINCULO,
                 "tranId": "9003", "time": AGORA_MS - 60_000},
                {"symbol": "BTCUSDT", "incomeType": "COMMISSION",
                 "income": "-0.99", "asset": "USDT", "tradeId": EXEC_ENTRADA, **VINCULO,
                 "tranId": "9004", "time": AGORA_MS - 59_000}]}])
        evidencias, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertEqual(evidencias, [])
        self.assertFalse(completo)
        # V2: dois eventos DISTINTOS para o mesmo fill bloqueiam com motivo
        # próprio (nunca escolher o último/maior/menor nem somar).
        self.assertEqual(erro, ea.LEDGER_AMBIGUOUS_FOR_FILL)

    async def test_pagina_truncada_e_erro_nao_conversao_parcial(self):
        # Página CHEIA cujo maior carimbo não avança o cursor: truncamento real.
        cheia = [{"symbol": "BTCUSDT", "incomeType": "COMMISSION",
                  "income": "-0.42", "asset": "USDT", "tradeId": EXEC_ENTRADA, **VINCULO,
                  "tranId": str(9100 + i), "time": AGORA_MS - 120_000}
                 for i in range(2)]
        cliente, _ = self._cliente([{"ok": True, "limit": 2, "income": cheia}])
        evidencias, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertEqual(evidencias, [])
        self.assertEqual(erro, "INCOME_PAGE_SAME_TIMESTAMP")

    async def test_erro_do_ledger_e_orcamento_bloqueiam(self):
        cliente, _ = self._cliente([{"ok": False, "error": "RATE_LIMIT"}])
        _, completo, erro = await coletar_conversoes(
            cliente, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertFalse(completo)
        self.assertEqual(erro, "RATE_LIMIT")

        cliente2, _ = self._cliente([])
        _, completo2, erro2 = await coletar_conversoes(
            cliente2, "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[0], now_ms=AGORA_MS)
        self.assertFalse(completo2)
        self.assertEqual(erro2, "CALL_BUDGET_EXHAUSTED")

    async def test_sem_endpoint_de_ledger_nao_inventa_fonte(self):
        class SemIncome:
            pass
        _, completo, erro = await coletar_conversoes(
            SemIncome(), "BTCUSDT", self._exigidas(),
            identity={"account_scope": ESCOPO, "exchange": "binance"},
            start_ms=AGORA_MS - 120_000, end_ms=AGORA_MS - 1_000,
            budget=[6], now_ms=AGORA_MS)
        self.assertFalse(completo)
        self.assertEqual(erro, "INCOME_ENDPOINT_UNAVAILABLE")


class TotalR05DDesbloqueadoPelaConversao(unittest.TestCase):
    """O consumidor do total só aceita a linha quando a conversão resolve."""

    def _linha(self, *, fee_conversions=None):
        ctx = contextos(entrada(), saida())
        acc = ea.merge_accounting(
            ea.empty_accounting(identity=IDENTIDADE),
            fills=[entrada(), saida()], fee_context=ctx,
            fee_conversions=([fee_conversions] if fee_conversions else ()))
        totais = ea.compute_totals(
            [entrada()], [saida()],
            [{"key": "FUNDING_FEE:1", "income": "-0.10", "asset": "USDT"}],
            funding_state=ea.FUNDING_CONFIRMED, fee_context=ctx,
            fee_conversions=acc.get("fee_conversions"))
        return {**acc, "state": "CONFIRMED", "funding_state": "CONFIRMED",
                "totals": totais}

    def test_linha_com_comissao_nao_convertida_fica_fora(self):
        valor, motivo = fts.row_verdict(self._linha(), account_scope=ESCOPO)
        self.assertIsNone(valor)
        self.assertEqual(motivo, fts.FEE_ASSET_UNCONVERTED)

    def test_linha_com_conversao_confirmada_entra_no_total(self):
        linha = self._linha(fee_conversions=evidencia())
        valor, motivo = fts.row_verdict(linha, account_scope=ESCOPO)
        self.assertIsNone(motivo, linha["totals"])
        # net_trade 9.53 + funding −0.10 = 9.43
        self.assertEqual(valor, Decimal("9.43"))
        agregado = fts.aggregate([linha], account_scope=ESCOPO,
                                 collection={"pagination_complete": True,
                                             "overlap_resolved": True})
        self.assertEqual(agregado["state"], fts.STATE_COMPLETE)
        self.assertEqual(agregado["rows_confirmed"], 1)
        self.assertAlmostEqual(agregado["total_net_including_funding"], 9.43,
                               places=9)


if __name__ == "__main__":
    unittest.main()
