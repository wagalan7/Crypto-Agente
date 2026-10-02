"""Fechamento ÚNICO manual/BOT — sete defeitos levantados sobre `61920156`.

Casos PUROS e de transporte (sem banco). Os aceites que exigem persistência,
duas conexões, locks reais e o caller real `open_shadow_for_recs` vivem em
`tests/pg_integration_manual_single.py`.

Cobertura aqui:

- **F1** classificação ÚNICA da redução. `reduce_only` só vale como booleano
  LITERAL; valor inválido é recusado ANTES de `set_leverage`, arredondamento
  com I/O, POST ou cancelamento. Os dois caminhos positivos (abertura e
  redução) chegam ao transporte com o payload correto, e uma entrada sem SL
  instalado nunca reporta `sl_ok=True`.
- **F3** os carimbos ORIGINAIS da observação são validados como inteiros
  legítimos — ausência, bool ou futuro não viram `now`, zero nem idade
  espremida que autoriza.
- **F4** proposta financeira CONGELADA por despacho: o último exame antes de
  assinar é SÍNCRONO, compara os params finais e recusa sem enviar.
- **F7** TERMINAL idêntico é no-op econômico (predicado puro da transição).
"""
from __future__ import annotations

import json
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
from services import entry_intent_service as intents          # noqa: E402
from services import shadow_trade_service as sts              # noqa: E402
from services import manual_position_service as mps           # noqa: E402

ESCOPO = "f" * 64
DELTA = "DELTA/USDT:USDT"
DELTA_API = "DELTAUSDT"

ROTA_ORDEM = "/fapi/v1/order"
ROTA_ALGO = "/fapi/v1/algoOrder"
ROTA_LEVERAGE = "/fapi/v1/leverage"
ROTA_POSICOES = "/fapi/v2/positionRisk"


class ClienteHTTPFalso:
    """Borda HTTP falsa que registra método, rota e os params DESSERIALIZADOS.

    Assinatura e chaves nunca entram no registro: os params são capturados no
    momento em que `_build_signed_url` os recebe, antes de assinar.
    """

    def __init__(self, respostas: dict | None = None):
        self.chamadas: list[dict] = []
        self._pendentes: list[tuple[str, dict]] = []
        self.respostas = dict(respostas or {})

    # ── fronteira de assinatura (params crus, sem segredo) ───────────────
    def assinar(self, path: str, params: dict | None = None) -> str:
        self._pendentes.append((path, dict(params or {})))
        return "https://sintetico.invalido" + path

    async def request(self, method: str, url: str):
        path = url.split("?")[0].replace("https://sintetico.invalido", "")
        params: dict = {}
        for indice, (rota, capturados) in enumerate(self._pendentes):
            if rota == path:
                params = capturados
                self._pendentes.pop(indice)
                break
        self.chamadas.append({"method": method, "path": path, "params": params})
        corpo = self.respostas.get(path, self.respostas.get("*", {}))
        return SimpleNamespace(status_code=200, headers={},
                               json=lambda: corpo)

    # ── leituras do registro ─────────────────────────────────────────────
    def mutacoes(self) -> list[dict]:
        return [c for c in self.chamadas if c["method"] in ("POST", "DELETE")]

    def _ordens(self) -> list[dict]:
        return [c for c in self.chamadas
                if c["method"] == "POST" and c["path"] == ROTA_ORDEM]

    def posts_de_entrada(self) -> list[dict]:
        """POST de ordem que CRIA/ALTERA exposição (sem rótulo de redução)."""
        return [c for c in self._ordens()
                if not c["params"].get("reduceOnly")
                and not c["params"].get("closePosition")
                and not c["params"].get("stopPrice")]

    def posts_de_protecao(self) -> list[dict]:
        """Condicionais (SL/TP) e qualquer POST de ordem redutora."""
        condicionais = [c for c in self.chamadas
                        if c["method"] == "POST" and c["path"] == ROTA_ALGO]
        redutoras = [c for c in self._ordens()
                     if c["params"].get("reduceOnly")
                     or c["params"].get("closePosition")]
        return condicionais + redutoras

    def posts_de_alavancagem(self) -> list[dict]:
        return [c for c in self.chamadas
                if c["method"] == "POST" and c["path"] == ROTA_LEVERAGE]


FILTROS_PADRAO = {
    "step": 0.001, "min_qty": 0.001, "max_qty": 1000.0,
    "market_step": 0.001, "market_min_qty": 0.001, "market_max_qty": 1000.0,
    "min_notional": 5.0, "tick": 0.01,
}


async def preflight_aprovando(qty: float, rules: dict) -> dict:
    """Preflight P04B sintético que APROVA — a borda externa do teste."""
    return {"ok": True, "quality": "LIVE", "approved_qty": float(qty),
            "reason_code": "EXEC_PREFLIGHT_OK",
            "checks": {"best_executable_price": 100.0, "vwap_price": 100.0,
                       "worst_price": 100.0, "market_rules": dict(rules)}}


def respostas_padrao(*, posicao_amt: str = "1") -> dict:
    return {
        ROTA_ORDEM: {"orderId": 11, "status": "FILLED", "executedQty": "1",
                     "avgPrice": "100", "cumQuote": "100"},
        ROTA_ALGO: {"algoId": "A1", "status": "NEW"},
        ROTA_LEVERAGE: {"symbol": DELTA_API, "leverage": 5},
        ROTA_POSICOES: [{"symbol": DELTA_API, "positionAmt": posicao_amt,
                         "entryPrice": "100", "markPrice": "100",
                         "unRealizedProfit": "0", "leverage": "5",
                         "updateTime": 1}],
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": [],
        "*": {},
    }


class F1ClassificacaoUnicaDaReducao(unittest.IsolatedAsyncioTestCase):
    """O rótulo de redução é um booleano LITERAL do começo ao fim do percurso.

    Defeito medido na baseline: `place_order(reduce_only="true", stop_loss=95)`
    enviava MARKET SEM `reduceOnly`, não instalava SL nenhum e devolvia
    `ok=True, sl_ok=True, safety_state=NOT_APPLICABLE`.
    """

    def _ligar(self, *, posicao_amt: str = "1"):
        self.cliente = ClienteHTTPFalso(respostas_padrao(posicao_amt=posicao_amt))

        async def registro():
            return {"ok": True, "reason_code": "REGISTRY_READ", "acks": []}

        async def estado(account_scope=None):
            return {"blocked": False, "generation": 7, "pending_failure": False,
                    "reason_code": "ACCOUNT_VALIDATED"}

        patches = [
            patch.object(mps, "active_acknowledgements", registro),
            patch.object(mps, "current_account_scope", return_value=ESCOPO),
            patch.object(mps, "account_validation_state", estado),
            patch.object(bss, "is_configured", return_value=True),
            patch.object(bss, "_ban_until_ms", 0),
            patch.object(bss, "_throttle_until_ms", 0),
            patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)),
            patch.object(bss, "_round_price",
                         AsyncMock(side_effect=lambda _s, p: float(p))),
            patch.object(bss, "_get_symbol_filters",
                         AsyncMock(return_value=dict(FILTROS_PADRAO))),
            patch.object(bss, "_build_signed_url", self.cliente.assinar),
            patch.object(bss, "_get_client", return_value=self.cliente),
        ]
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patches])

    # ── valores inválidos: recusa explícita e ZERO mutação ───────────────
    async def test_valor_nao_booleano_nao_muta_nada(self):
        invalidos = ["true", "false", "True", 1, 0, None, float("nan"),
                     1.0, 0.0, object(), [], {}]
        for valor in invalidos:
            with self.subTest(valor=repr(valor)):
                self._ligar()
                res = await bss.place_order(
                    DELTA, "BUY", 1.0, order_type="Market",
                    reduce_only=valor, stop_loss=95.0,
                    entry_preflight=preflight_aprovando,
                    client_order_id="cw-f1-invalido")
                self.assertFalse(res.get("ok"), repr(valor))
                self.assertEqual(res.get("reason_code"),
                                 "EXEC_REDUCE_ONLY_INVALID", repr(valor))
                self.assertEqual(self.cliente.mutacoes(), [],
                                 f"{valor!r} mutou a corretora")
                self.assertEqual(self.cliente.chamadas, [],
                                 f"{valor!r} chegou ao transporte")
                self.assertTrue(res.get("entry_not_submitted"))
                self.assertEqual(res.get("submitted_qty"), 0.0)
                self.assertIsNot(res.get("sl_ok"), True,
                                 "SL ausente nunca é sl_ok=True")
                self.assertNotEqual(res.get("safety_state"), "NOT_APPLICABLE")

    async def test_string_true_nao_ganha_isencao_de_reducao(self):
        """O caso exato da revisão: `"true"` + `stop_loss` não abre nada."""
        self._ligar()
        res = await bss.place_order(DELTA, "BUY", 0.998, order_type="Market",
                                    reduce_only="true", stop_loss=95.0,
                                    entry_preflight=preflight_aprovando,
                                    client_order_id="cw-f1-string")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason_code"), "EXEC_REDUCE_ONLY_INVALID")
        self.assertEqual(self.cliente.posts_de_entrada(), [])
        self.assertEqual(self.cliente.posts_de_protecao(), [])
        self.assertEqual(self.cliente.posts_de_alavancagem(), [])

    async def test_valor_invalido_nem_tenta_alavancagem(self):
        """Recusa ANTES de `set_leverage` — mesmo com leverage pedido."""
        self._ligar()
        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    reduce_only=1, leverage=5,
                                    entry_preflight=preflight_aprovando,
                                    client_order_id="cw-f1-leverage")
        self.assertFalse(res.get("ok"))
        self.assertEqual(self.cliente.posts_de_alavancagem(), [])
        self.assertEqual(self.cliente.mutacoes(), [])

    # ── caminhos positivos reais ─────────────────────────────────────────
    async def test_abertura_com_false_instala_protecao_e_roda_safety(self):
        self._ligar()
        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    reduce_only=False, stop_loss=95.0,
                                    entry_preflight=preflight_aprovando,
                                    client_order_id="cw-f1-entrada")
        entradas = self.cliente.posts_de_entrada()
        protecoes = self.cliente.posts_de_protecao()
        self.assertEqual(len(entradas), 1, str(self.cliente.chamadas)[:400])
        self.assertEqual(entradas[0]["params"].get("type"), "MARKET")
        self.assertEqual(entradas[0]["params"].get("side"), "BUY")
        self.assertIsNone(entradas[0]["params"].get("reduceOnly"),
                          "abertura não carrega reduceOnly")
        self.assertEqual(entradas[0]["params"].get("newClientOrderId"),
                         "cw-f1-entrada")
        self.assertTrue(protecoes, "entrada com SL instala proteção")
        self.assertTrue(res.get("sl_ok"), str(res)[:300])
        self.assertNotEqual(res.get("safety_state"), "NOT_APPLICABLE")
        self.assertTrue(res.get("ok"), str(res)[:300])

    async def test_reducao_com_true_envia_reduce_only_de_verdade(self):
        self._ligar(posicao_amt="0")
        res = await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                                    reduce_only=True,
                                    client_order_id="cw-f1-reducao")
        ordens = [c for c in self.cliente.chamadas
                  if c["method"] == "POST" and c["path"] == ROTA_ORDEM]
        self.assertEqual(len(ordens), 1, str(self.cliente.chamadas)[:400])
        self.assertEqual(str(ordens[0]["params"].get("reduceOnly")).lower(),
                         "true")
        self.assertEqual(self.cliente.posts_de_entrada(), [],
                         "redução não conta como entrada")
        self.assertEqual(self.cliente.posts_de_alavancagem(), [],
                         "redução não altera alavancagem")
        self.assertEqual(res.get("entry_state"), "NOT_APPLICABLE")

    async def test_id_de_cliente_gerado_usa_a_classificacao_canonica(self):
        """Sem id fornecido: redução gera `close`, abertura gera `entry`."""
        self._ligar(posicao_amt="0")
        await bss.place_order(DELTA, "SELL", 1.0, order_type="Market",
                              reduce_only=True)
        reducao = [c for c in self.cliente.chamadas
                   if c["method"] == "POST" and c["path"] == ROTA_ORDEM]
        self.assertTrue(reducao[0]["params"]["newClientOrderId"]
                        .startswith("cw-close-"), reducao[0]["params"])

        self._ligar()
        await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                              reduce_only=False,
                              entry_preflight=preflight_aprovando)
        entrada = self.cliente.posts_de_entrada()
        self.assertTrue(entrada[0]["params"]["newClientOrderId"]
                        .startswith("cw-entry-"), entrada[0]["params"])

    def test_predicado_da_classificacao_rejeita_tudo_que_nao_e_bool(self):
        self.assertTrue(bss.reduce_only_flag_is_valid(True))
        self.assertTrue(bss.reduce_only_flag_is_valid(False))
        for valor in ("true", "false", 1, 0, 1.0, None, float("nan"),
                      object(), [], {}):
            self.assertFalse(bss.reduce_only_flag_is_valid(valor), repr(valor))
        self.assertEqual(bss.classify_order_action(reduce_only=True),
                         "reduce_only")
        self.assertEqual(bss.classify_order_action(reduce_only=False),
                         "place_order")


def quote_fresca(*, agora_ms: float, idade_ms: float = 100.0, **mudancas) -> dict:
    """Cotação sintética no formato REAL que o avaliador P04A aceita."""
    base = {"ok": True, "exchange": "binance", "source": "binance_book_ticker",
            "symbol": "DELTAUSDT", "bid": 99.9, "ask": 100.0,
            "bid_qty": 50.0, "ask_qty": 50.0,
            "received_at_ms": agora_ms - idade_ms,
            "exchange_time_ms": agora_ms - idade_ms,
            "latency_ms": 20.0}
    base.update(mudancas)
    return base


def evidencia_quote(*, agora_ms: float, idade_ms: float = 100.0,
                    ttl_ms: float = 1500.0, preco: float = 99.9) -> dict:
    return {
        "evaluator": "quote", "order_type": "LIMIT", "time_in_force": "GTX",
        "args": {"quote": quote_fresca(agora_ms=agora_ms, idade_ms=idade_ms),
                 "symbol": "DELTA/USDT:USDT", "side": "long",
                 "planned_entry": 100.0, "stop_loss": 99.0,
                 "tp1": 101.0, "tp2": 102.0, "atr": 1.0,
                 "maker_limit_price": preco,
                 "entry_zone_low": None, "entry_zone_high": None},
        "limits": {"max_quote_age_ms": ttl_ms, "max_fetch_latency_ms": 400.0,
                   "max_spread_pct": 0.5, "max_chase_atr": 1.0,
                   "min_rr_tp1": 1.0, "min_rr_tp2": 2.0,
                   "max_adverse_slippage_pct": 0.0,
                   "enforce_adverse_slippage": False},
        "margin": {"observed_start_ms": agora_ms - idade_ms,
                   "observed_end_ms": agora_ms - idade_ms,
                   "quality": "live", "required_usd": 10.0,
                   "free_usd": 1000.0, "generation": 7},
    }


def proposta_de_teste(*, agora_ms: float, idade_ms: float = 100.0,
                      ttl_ms: float = 1500.0, dispatch_id: str = "cw-f4-maker",
                      qty: float = 1.0, preco: float = 99.9) -> dict:
    return intents.freeze_dispatch_proposal(
        account_ref="c" * 64, exchange="binance", market="usdm_futures",
        intent_key="chave-f4", symbol="DELTA/USDT:USDT", side="BUY",
        position_side="LONG", order_type="LIMIT", time_in_force="GTX",
        reduce_only=False, dispatch_id=dispatch_id, qty=qty, price=preco,
        stop=99.0, reference_price=None, leverage=5, token=7,
        created_at_ms=int(agora_ms), lease_expires_at_ms=int(agora_ms) + 90_000,
        evidence=evidencia_quote(agora_ms=agora_ms, idade_ms=idade_ms,
                                 ttl_ms=ttl_ms, preco=preco),
        limits=evidencia_quote(agora_ms=agora_ms)["limits"])


class F4PropostaCongeladaEExameSincrono(unittest.TestCase):
    """A última palavra antes de assinar é SÍNCRONA e compara os params reais.

    Defeito medido na baseline: P04 aprovava quote/depth, a carteira levava
    1,7 s e maker/MARKET saíam com evidência de ~1725 ms contra TTL de 1500 ms,
    porque a autorização final reconferia o TOKEN, não a cotação.
    """

    def setUp(self):
        self.agora_ms = time.time() * 1000.0

    def _exame(self, proposta, *, autorizado_ms=None):
        autorizacao = {"ok": True, "proposal": proposta, "token": 7,
                       "authorized_at_ms": (autorizado_ms
                                            if autorizado_ms is not None
                                            else self.agora_ms)}
        return sts._sync_final_check_for(autorizacao)

    def _payload(self, **mudancas) -> dict:
        base = {"symbol": "DELTAUSDT", "side": "BUY", "type": "LIMIT",
                "quantity": 1.0, "price": 99.9, "timeInForce": "GTX",
                "newClientOrderId": "cw-f4-maker"}
        base.update(mudancas)
        return base

    # ── contrato da proposta ─────────────────────────────────────────────
    def test_hash_sobrevive_ao_ida_e_volta_do_jsonb(self):
        """`100.0` pode voltar do JSONB como `100`: o hash não pode mudar."""
        proposta = proposta_de_teste(agora_ms=self.agora_ms, qty=100.0,
                                     preco=100.0)
        self.assertTrue(proposta.get("ok"), proposta)
        ida_e_volta = json.loads(json.dumps(proposta))
        ida_e_volta["qty"] = 100           # numeric → int, como o JSONB faria
        ida_e_volta["price"] = 100
        self.assertTrue(
            intents.proposal_matches_stored(ida_e_volta)["ok"],
            "proposta legítima não pode ser recusada por representação")

    def test_campo_essencial_invalido_nao_congela(self):
        for mudanca in ({"dispatch_id": None}, {"qty": float("nan")},
                        {"qty": None}, {"symbol": None},
                        {"account_ref": None}, {"order_type": None}):
            base = dict(account_ref="c" * 64, exchange="binance",
                        intent_key="k", symbol="DELTA/USDT:USDT", side="BUY",
                        order_type="LIMIT", dispatch_id="d1", qty=1.0,
                        created_at_ms=int(self.agora_ms))
            base.update(mudanca)
            saida = intents.freeze_dispatch_proposal(**base)
            self.assertFalse(saida.get("ok"), repr(mudanca))
            self.assertEqual(saida.get("reason_code"),
                             intents.PROPOSAL_INVALID, repr(mudanca))

    def test_adulteracao_e_detectada(self):
        proposta = dict(proposta_de_teste(agora_ms=self.agora_ms))
        proposta["qty"] = 999.0            # aumenta exposição no JSON
        veredito = intents.proposal_matches_stored(proposta)
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], intents.PROPOSAL_TAMPERED)

    def test_proposta_ausente_recusa(self):
        exame = self._exame(None)
        veredito = exame(self._payload())
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], "EXEC_PROPOSAL_MISSING")

    # ── TTL: 1499 passa, 1501 recusa ─────────────────────────────────────
    def test_ttl_1500_com_1499_e_1501(self):
        passa = self._exame(proposta_de_teste(agora_ms=self.agora_ms,
                                              idade_ms=1499.0))
        self.assertTrue(passa(self._payload())["ok"], "1499ms ainda é fresco")

        recusa = self._exame(proposta_de_teste(agora_ms=self.agora_ms,
                                               idade_ms=1501.0))
        veredito = recusa(self._payload())
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], "EXEC_QUOTE_STALE",
                         str(veredito))

    def test_autorizacao_envelhecida_alem_da_ttl_recusa(self):
        exame = self._exame(proposta_de_teste(agora_ms=self.agora_ms),
                            autorizado_ms=self.agora_ms - 1600.0)
        veredito = exame(self._payload())
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], "EXEC_PROPOSAL_EXPIRED")

    def test_lease_vencido_antes_de_assinar_recusa(self):
        proposta = dict(proposta_de_teste(agora_ms=self.agora_ms))
        proposta["lease_expires_at_ms"] = int(self.agora_ms) - 1
        proposta["hash"] = intents.canonical_proposal_hash(proposta)
        exame = self._exame(proposta)
        veredito = exame(self._payload())
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], "EXEC_LEASE_EXPIRED")

    # ── params alterados depois da aprovação ─────────────────────────────
    def test_payload_diferente_do_admitido_recusa(self):
        exame = self._exame(proposta_de_teste(agora_ms=self.agora_ms))
        casos = [{"quantity": 1.5}, {"quantity": 0.5}, {"price": 100.5},
                 {"side": "SELL"}, {"type": "MARKET"},
                 {"newClientOrderId": "cw-f4-maker-mfb"},
                 {"timeInForce": "GTC"}, {"reduceOnly": "true"}]
        for mudanca in casos:
            veredito = exame(self._payload(**mudanca))
            self.assertFalse(veredito["ok"], repr(mudanca))
            self.assertEqual(veredito["reason_code"],
                             "EXEC_PROPOSAL_TAMPERED", repr(mudanca))

    def test_proposta_maker_nao_autoriza_o_filho_mfb(self):
        """A filha `-mfb` precisa de proposta PRÓPRIA; nunca herda a maker."""
        exame = self._exame(proposta_de_teste(agora_ms=self.agora_ms,
                                              dispatch_id="cw-f4-maker"))
        veredito = exame(self._payload(
            newClientOrderId=bss.market_fallback_client_order_id("cw-f4-maker"),
            type="MARKET", price=None, timeInForce=None))
        self.assertFalse(veredito["ok"])
        self.assertEqual(veredito["reason_code"], "EXEC_PROPOSAL_TAMPERED")

    def test_market_nao_aceita_price_nem_stop_artificiais(self):
        proposta = dict(proposta_de_teste(agora_ms=self.agora_ms,
                                          dispatch_id="cw-f4-market"))
        proposta["order_type"] = "MARKET"
        proposta["price"] = None
        proposta["reference_price"] = 100.0
        proposta["time_in_force"] = None
        proposta["hash"] = intents.canonical_proposal_hash(proposta)
        exame = self._exame(proposta)
        limpo = {"symbol": "DELTAUSDT", "side": "BUY", "type": "MARKET",
                 "quantity": 1.0, "newOrderRespType": "RESULT",
                 "newClientOrderId": "cw-f4-market"}
        self.assertTrue(exame(limpo)["ok"], str(exame(limpo)))
        for sujo in ({"price": 100.0}, {"stopPrice": 99.0}):
            veredito = exame({**limpo, **sujo})
            self.assertFalse(veredito["ok"], repr(sujo))
            self.assertEqual(veredito["reason_code"], "EXEC_PROPOSAL_TAMPERED")


class F4ExameSincronoNoTransporteReal(unittest.IsolatedAsyncioTestCase):
    """O exame entregue pela autorização roda DENTRO do transporte real."""

    async def asyncSetUp(self):
        self.cliente = ClienteHTTPFalso(respostas_padrao())

        async def registro():
            return {"ok": True, "reason_code": "REGISTRY_READ", "acks": []}

        async def estado(account_scope=None):
            return {"blocked": False, "generation": 7, "pending_failure": False,
                    "reason_code": "ACCOUNT_VALIDATED"}

        patches = [
            patch.object(mps, "active_acknowledgements", registro),
            patch.object(mps, "current_account_scope", return_value=ESCOPO),
            patch.object(mps, "account_validation_state", estado),
            patch.object(bss, "is_configured", return_value=True),
            patch.object(bss, "_ban_until_ms", 0),
            patch.object(bss, "_throttle_until_ms", 0),
            patch.object(bss, "_round_qty", AsyncMock(return_value=1.0)),
            patch.object(bss, "_round_price",
                         AsyncMock(side_effect=lambda _s, p: float(p))),
            patch.object(bss, "_get_symbol_filters",
                         AsyncMock(return_value=dict(FILTROS_PADRAO))),
            patch.object(bss, "_build_signed_url", self.cliente.assinar),
            patch.object(bss, "_get_client", return_value=self.cliente),
        ]
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patches])

    async def test_exame_sincrono_recusando_nao_envia_nada(self):
        chamadas = {"sincrono": 0}

        def _sincrono(params):
            chamadas["sincrono"] += 1
            chamadas["params"] = dict(params)
            return {"ok": False, "reason_code": "EXEC_PROPOSAL_EXPIRED",
                    "reason": "proposta vencida"}

        async def autoriza():
            return {"ok": True, "reason_code": "DISPATCH_AUTHORIZED",
                    "sync_check": _sincrono}

        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    reduce_only=False, stop_loss=95.0,
                                    entry_preflight=preflight_aprovando,
                                    final_authorization=autoriza,
                                    client_order_id="cw-f4-transporte")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason_code"), "EXEC_PROPOSAL_EXPIRED")
        self.assertTrue(res.get("entry_not_submitted"))
        self.assertEqual(self.cliente.posts_de_entrada(), [],
                         "zero POST quando o exame síncrono recusa")
        self.assertEqual(self.cliente.posts_de_protecao(), [])
        self.assertEqual(chamadas["sincrono"], 1)
        self.assertEqual(chamadas["params"].get("quantity"), 1.0,
                         "o exame recebe os params FINAIS")

    async def test_autorizacao_negativa_nao_produz_exame_sincrono(self):
        """Veredito assíncrono negativo nunca é contornado por exame síncrono."""
        chamadas = {"sincrono": 0}

        async def autoriza():
            return {"ok": False, "reason_code": "MARGIN_OBSERVATION_SUPERSEDED",
                    "sync_check": lambda params: chamadas.__setitem__(
                        "sincrono", chamadas["sincrono"] + 1) or {"ok": True}}

        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    reduce_only=False,
                                    entry_preflight=preflight_aprovando,
                                    final_authorization=autoriza,
                                    client_order_id="cw-f4-negativa")
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason_code"),
                         "MARGIN_OBSERVATION_SUPERSEDED")
        self.assertEqual(chamadas["sincrono"], 0)
        self.assertEqual(self.cliente.mutacoes(), [])

    async def test_exame_sincrono_aprovando_deixa_a_entrada_seguir(self):
        vistos = []

        def _sincrono(params):
            vistos.append(dict(params))
            return True

        async def autoriza():
            return {"ok": True, "reason_code": "DISPATCH_AUTHORIZED",
                    "sync_check": _sincrono}

        res = await bss.place_order(DELTA, "BUY", 1.0, order_type="Market",
                                    reduce_only=False, stop_loss=95.0,
                                    entry_preflight=preflight_aprovando,
                                    final_authorization=autoriza,
                                    client_order_id="cw-f4-ok")
        self.assertTrue(res.get("ok"), str(res)[:300])
        self.assertEqual(len(self.cliente.posts_de_entrada()), 1)
        self.assertTrue(self.cliente.posts_de_protecao())
        self.assertEqual(len(vistos), 1)
        self.assertEqual(vistos[0].get("newClientOrderId"), "cw-f4-ok")


class F7TerminalIdenticoEhNoOp(unittest.TestCase):
    """Repetir o MESMO desfecho não é evento econômico.

    Defeito medido na baseline: geração financeira 11 → `mark_terminal` 12 →
    repetição IDÊNTICA 13. O `_resolve` excluía apenas `CONFIRMED`, então
    TERMINAL→TERMINAL casava, reescrevia carimbos/razão e incrementava a época.
    """

    def _classificar(self, atual, alvo, **extra):
        return intents.classify_resolution(current_state=atual,
                                           target_state=alvo, **extra)

    def test_mesmo_estado_e_no_op_idempotente(self):
        for estado in (intents.STATE_TERMINAL, intents.STATE_UNKNOWN,
                       intents.STATE_CONFIRMED):
            self.assertEqual(self._classificar(estado, estado),
                             intents.RESOLUTION_ALREADY_APPLIED, estado)

    def test_transicao_real_continua_efetiva(self):
        efetivas = [
            (intents.STATE_RESERVED, intents.STATE_TERMINAL),
            (intents.STATE_SENDING, intents.STATE_TERMINAL),
            (intents.STATE_SENDING, intents.STATE_UNKNOWN),
            (intents.STATE_UNKNOWN, intents.STATE_TERMINAL),
            (intents.STATE_UNKNOWN, intents.STATE_CONFIRMED),
            # Fill comprovado tarde: a exposição existe e PRECISA ser vinculada.
            (intents.STATE_TERMINAL, intents.STATE_CONFIRMED),
            (None, intents.STATE_TERMINAL),
        ]
        for atual, alvo in efetivas:
            self.assertEqual(self._classificar(atual, alvo),
                             intents.RESOLUTION_EFFECTIVE, f"{atual}->{alvo}")

    def test_confirmado_nunca_e_rebaixado(self):
        for alvo in (intents.STATE_UNKNOWN, intents.STATE_TERMINAL):
            self.assertEqual(
                self._classificar(intents.STATE_CONFIRMED, alvo),
                intents.RESOLUTION_FORBIDDEN, alvo)

    def test_terminal_nao_e_reaberto_como_unknown(self):
        self.assertEqual(
            self._classificar(intents.STATE_TERMINAL, intents.STATE_UNKNOWN),
            intents.RESOLUTION_FORBIDDEN)

    def test_vinculo_diferente_no_mesmo_estado_exige_reconciliacao(self):
        self.assertEqual(
            self._classificar(intents.STATE_CONFIRMED, intents.STATE_CONFIRMED,
                              current_trade_id=7, real_trade_id=9),
            intents.RESOLUTION_CONTRADICTORY)
        self.assertEqual(
            self._classificar(intents.STATE_CONFIRMED, intents.STATE_CONFIRMED,
                              current_trade_id=None, real_trade_id=9),
            intents.RESOLUTION_CONTRADICTORY)
        self.assertEqual(
            self._classificar(intents.STATE_CONFIRMED, intents.STATE_CONFIRMED,
                              current_trade_id=9, real_trade_id=9),
            intents.RESOLUTION_ALREADY_APPLIED)


if __name__ == "__main__":
    unittest.main()
