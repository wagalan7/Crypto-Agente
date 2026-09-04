"""R05C — contabilização por EXECUÇÕES REAIS.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco de
produção, Railway, Telegram/push ou ordem real. A fixture versionada é
SINTÉTICA — nenhum identificador ou valor da conta é publicado. A verificação
contra a fixture auditada roda localmente e é PULADA quando ela não existe.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
SYNTHETIC = BACKEND / "tests" / "fixtures" / "r05c_synthetic_cases.json"
AUDITED = Path("/private/tmp/crypto-win-r05c-prompt.aIMpNK/r05c_audited_cases.json")
TOL = Decimal("0.00000001")

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste R05C (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import execution_accounting_service as ea      # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════
def _D(value):
    return Decimal(value) if value is not None else None


def _reconcile_case(case: dict) -> dict:
    """Roda o núcleo REAL sobre um caso da fixture."""
    db = case["db"]
    identity = ea.build_identity(
        exchange="binance", symbol=db["symbol"], side=db["side"],
        entry_order_id=db["exchange_order_id"],
        entry_client_order_id=db["client_order_id"])
    fills = [ea.normalize_fill(f, exchange="binance")[0] for f in case["fills"]]
    funding = [ea.normalize_funding(f)[0] for f in case["funding"]]
    orders = [ea.normalize_order(o) for o in case["closing_orders"]]
    acc = ea.merge_accounting(None, identity=identity,
                              fills=[f for f in fills if f],
                              funding=[f for f in funding if f],
                              orders=[o for o in orders if o], now=NOW)
    result = ea.finalize_accounting(
        acc, entry_order_ids=[identity["entry_order_id"]],
        exit_order_ids=[o["order_id"] for o in orders if o],
        fills_window_complete=case["fills_window_complete"],
        funding_window_complete=case["funding_window_complete"],
        planned_stop=db.get("planned_stop"), now=NOW)
    # Fixture audit is arithmetic only: it has no entry GET/exclusivity proof.
    # Never label its operational ledger CONFIRMED just to pass a totals test.
    attributed = ea.attribute_fills(fills, identity=identity,
        entry_order_ids=[identity["entry_order_id"]],
        exit_order_ids=[o["order_id"] for o in orders if o])
    result["fixture_arithmetic"] = ea.compute_totals(attributed["entry"], attributed["exit"],
        funding, funding_state="CONFIRMED" if case["funding_window_complete"] else "PENDING")
    return result


def _fill(exec_id="1", *, order_id="100", symbol="ALFAUSDT", side="BUY",
          price="10", qty="2", realized="0", commission="0.01",
          asset="USDT", ts="1700000000000", position_side="BOTH"):
    return {"id": exec_id, "orderId": order_id, "symbol": symbol, "side": side,
            "positionSide": position_side, "price": price, "qty": qty,
            "realizedPnl": realized, "commission": commission,
            "commissionAsset": asset, "time": ts}


def _norm(*raws):
    return [ea.normalize_fill(r, exchange="binance")[0] for r in raws]


def _ident(side="long", order_id="100", symbol="ALFA/USDT:USDT"):
    return ea.build_identity(exchange="binance", symbol=symbol, side=side,
                             entry_order_id=order_id)


# ════════════════════════════════════════════════════════════════════════════
#  CASOS DA FIXTURE (sintética versionada + auditada local)
# ════════════════════════════════════════════════════════════════════════════
class CasosSinteticos(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(SYNTHETIC.read_text())

    def test_sete_casos_e_totais(self):
        totals = {k: Decimal(0) for k in
                  ("binance_gross", "commission", "funding",
                   "net_without_funding", "net_with_funding")}
        for case in self.data["trades"]:
            acc = _reconcile_case(case)
            exp, t = case["expected"], acc["fixture_arithmetic"]
            self.assertEqual(acc["state"], "PARTIAL", case["case"])  # fixture lacks entry-order GET
            pares = (
                ("binance_gross", _D(t["gross_realized"])),
                ("commission", _D(t["fees_by_asset"].get("USDT"))),
                ("funding", _D(t["funding_net"])),
                ("net_without_funding", _D(t["net_trade"])),
                ("net_with_funding", _D(t["net_including_funding"])),
            )
            for nome, obtido in pares:
                self.assertIsNotNone(obtido, f'{case["case"]}.{nome}')
                self.assertLessEqual(abs(obtido - Decimal(exp[nome])), TOL,
                                     f'{case["case"]}.{nome}')
                totals[nome] += obtido
            self.assertLessEqual(abs(_D(t["entry_avg_price"]) - Decimal(exp["entry_avg"])),
                                 TOL, f'{case["case"]}.entry_avg')
            self.assertLessEqual(abs(_D(t["entry_qty_executed"]) - Decimal(exp["entry_qty"])),
                                 TOL, f'{case["case"]}.entry_qty')
        for nome, soma in totals.items():
            self.assertLessEqual(abs(soma - Decimal(self.data["totals_expected"][nome])),
                                 TOL, f"TOTAL.{nome}")

    def test_fixture_versionada_e_anonima(self):
        blob = SYNTHETIC.read_text()
        self.assertFalse(self.data["provenance"]["secrets_included"])
        self.assertEqual(self.data["provenance"]["kind"], "SYNTHETIC")
        for privado in ("JOE", "BROCCOLI714", "SKHY", "XAUUSDT", "TACUSDT",
                        "GdsMDcBm8EFJuArMWOzd"):
            self.assertNotIn(privado, blob, privado)

    def test_tres_saidas_sem_duplicar_tp1(self):
        """TP1 + TP2 + runner: as três contam, o TP1 não é somado de novo."""
        case = next(c for c in self.data["trades"] if c["case"] == "GAMA")
        acc = _reconcile_case(case)
        self.assertEqual(len(case["closing_orders"]), 3)
        self.assertGreaterEqual(acc["coverage"]["exit_fills"], 3)
        self.assertTrue(acc["coverage"]["quantity_balanced"])
        self.assertLessEqual(
            abs(_D(acc["totals"]["net_trade"])
                - Decimal(case["expected"]["net_without_funding"])), TOL)

    def test_saida_real_nao_repete_a_entrada(self):
        """Time-stop: o exit vem da ordem de fechamento, não do entry."""
        case = next(c for c in self.data["trades"] if c["case"] == "ALFA")
        acc = _reconcile_case(case)
        entry = _D(acc["totals"]["entry_avg_price"])
        exit_ = _D(acc["totals"]["exit_avg_price"])
        self.assertIsNotNone(exit_)
        self.assertNotEqual(entry, exit_)

    def test_resultado_nao_e_zero(self):
        case = next(c for c in self.data["trades"] if c["case"] == "EPSI")
        acc = _reconcile_case(case)
        self.assertNotEqual(_D(acc["totals"]["net_trade"]), Decimal("0"))
        self.assertNotEqual(_D(acc["totals"]["entry_avg_price"]),
                            _D(acc["totals"]["exit_avg_price"]))

    def test_origem_externa_nao_vira_sl(self):
        case = next(c for c in self.data["trades"] if c["case"] == "OMEGA")
        coid = case["closing_orders"][0]["clientOrderId"]
        self.assertTrue(coid.startswith("ios_"))
        self.assertEqual(ea.close_origin({"client_order_id": coid}),
                         "EXTERNAL_OR_UNKNOWN")
        acc = _reconcile_case(case)
        self.assertNotIn("stop", json.dumps(acc.get("close_origin") or ""))

    def test_funding_zero_confirmado_vs_ausente(self):
        case = next(c for c in self.data["trades"] if c["case"] == "DELTA")
        self.assertEqual(case["funding"], [])
        acc = _reconcile_case(case)
        self.assertEqual(_D(acc["fixture_arithmetic"]["funding_net"]), Decimal("0"))
        self.assertIsNone(acc["totals"]["funding_net"])
        self.assertEqual(acc["funding_state"], "PENDING")
        # janela NÃO consultada: zero deixa de ser provado
        parcial = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident()),
            fills_window_complete=True, funding_window_complete=False)
        self.assertIsNone(parcial["totals"]["funding_net"])


class CasosAuditadosLocais(unittest.TestCase):
    """Verificação local contra a fixture PRIVADA — nunca versionada."""

    def setUp(self):
        if not AUDITED.exists():
            self.skipTest("fixture auditada indisponível (esperado no repo)")
        self.data = json.loads(AUDITED.read_text())

    def test_sete_casos_auditados_e_totais(self):
        totals = {k: Decimal(0) for k in
                  ("binance_gross", "commission", "funding",
                   "net_without_funding", "net_with_funding")}
        for t in self.data["trades"]:
            db = t["historical_db"]
            case = {"db": {"symbol": db["symbol"], "side": db["side"],
                           "planned_stop": db["planned_stop"],
                           "exchange_order_id": db["exchange_order_id"],
                           "client_order_id": db["client_order_id"]},
                    "fills": t["fills"], "funding": t["funding"],
                    "closing_orders": t["closing_orders"],
                    "fills_window_complete": t["fills_window_complete"],
                    "funding_window_complete": t["funding_window_complete"],
                    "case": t["expected"]["symbol"]}
            acc = _reconcile_case(case)
            exp, tt = t["expected"], acc["fixture_arithmetic"]
            self.assertEqual(acc["state"], "PARTIAL", case["case"])  # fixture lacks entry-order GET
            for nome, obtido in (("binance_gross", _D(tt["gross_realized"])),
                                 ("commission", _D(tt["fees_by_asset"].get("USDT"))),
                                 ("funding", _D(tt["funding_net"])),
                                 ("net_without_funding", _D(tt["net_trade"])),
                                 ("net_with_funding", _D(tt["net_including_funding"]))):
                self.assertLessEqual(abs(obtido - Decimal(exp[nome])), TOL,
                                     f'{case["case"]}.{nome}')
                totals[nome] += obtido
        esperado = self.data["totals_expected"]
        for chave, nome in (("binance_gross", "binance_gross"),
                            ("commission", "commission"), ("funding", "funding"),
                            ("net_without_funding", "net_without_funding"),
                            ("net_with_funding", "net_with_funding")):
            self.assertLessEqual(abs(totals[nome] - Decimal(esperado[chave])), TOL,
                                 f"TOTAL.{chave}")

    def test_registro_antigo_nao_e_oracle(self):
        """+1.3896 registrado != resultado auditado; o R05C não usa o antigo."""
        self.assertNotEqual(Decimal(self.data["totals_expected"]["stored_pnl"]),
                            Decimal(self.data["totals_expected"]["net_without_funding"]))


# ════════════════════════════════════════════════════════════════════════════
#  NORMALIZAÇÃO E CONTRATO DE VALOR
# ════════════════════════════════════════════════════════════════════════════
class Normalizacao(unittest.TestCase):

    def test_decimal_das_strings_sem_erro_binario(self):
        self.assertEqual(ea.to_decimal("0.1") + ea.to_decimal("0.2"),
                         Decimal("0.3"))

    def test_rejeita_none_bool_nan_inf(self):
        for ruim in (None, True, False, float("nan"), float("inf"),
                     float("-inf"), "", "abc", []):
            self.assertIsNone(ea.to_decimal(ruim), repr(ruim))

    def test_ids_sao_strings_e_nunca_float(self):
        self.assertEqual(ea._id_str(1028043174000000001), "1028043174000000001")
        self.assertIsNone(ea._id_str(1.0))
        self.assertIsNone(ea._id_str(True))

    def test_symbol_com_quote_exata(self):
        self.assertEqual(ea.normalize_symbol("BTC/USDT:USDT"), "BTCUSDT")
        self.assertEqual(ea.normalize_symbol("BTC/USDC:USDC"), "BTCUSDC")
        self.assertNotEqual(ea.normalize_symbol("BTC/USDT:USDT"),
                            ea.normalize_symbol("BTC/USDC:USDC"))

    def test_fill_invalido_e_rejeitado_com_motivo(self):
        casos = [
            (_fill(price="0"), "price"), (_fill(qty="0"), "qty"),
            (_fill(side="X"), "side"),
        ]
        for raw, esperado in casos:
            norm, motivo = ea.normalize_fill(raw, exchange="binance")
            self.assertIsNone(norm)
            self.assertIn(esperado, motivo)
        semid = _fill(); semid.pop("id")
        self.assertIsNone(ea.normalize_fill(semid, exchange="binance")[0])

    def test_comissao_ausente_nao_vira_zero(self):
        raw = _fill(commission=None, asset=None)
        norm, _ = ea.normalize_fill(raw, exchange="binance")
        self.assertIsNotNone(norm)
        self.assertIsNone(norm["commission"])
        totals = ea.compute_totals([norm], [], [])
        self.assertFalse(totals["fees_complete"])
        self.assertIsNone(totals["net_trade"])
        self.assertEqual(totals["net_trade_reason_code"], "FEES_INCOMPLETE")

    def test_comissao_zero_verdadeiro_e_zero(self):
        norm, _ = ea.normalize_fill(_fill(commission="0"), exchange="binance")
        totals = ea.compute_totals([norm], [], [])
        self.assertTrue(totals["fees_complete"])
        self.assertEqual(totals["fees_by_asset"]["USDT"], "0")

    def test_comissao_em_outro_ativo_nao_e_subtraida(self):
        f1, f2 = _norm(_fill("1", commission="0.01", asset="USDT"),
                       _fill("2", commission="0.5", asset="BNB", side="SELL",
                             realized="1"))
        totals = ea.compute_totals([f1], [f2], [])
        self.assertIsNone(totals["net_trade"])
        self.assertEqual(totals["net_trade_reason_code"],
                         "FEE_ASSET_CONVERSION_UNAVAILABLE")
        self.assertEqual(totals["fee_assets_unconverted"], ["BNB"])
        self.assertIn("BNB", totals["fees_by_asset"])

    def test_comissao_contada_uma_vez_por_exec_id(self):
        f1 = _norm(_fill("1", commission="0.10"))[0]
        totals = ea.compute_totals([f1, dict(f1)], [], [])
        self.assertEqual(totals["fees_by_asset"]["USDT"], "0.10")

    def test_media_ponderada_de_multiplos_fills(self):
        fills = _norm(_fill("1", price="10", qty="1", commission="0"),
                      _fill("2", price="20", qty="3", commission="0"))
        totals = ea.compute_totals(fills, [], [])
        self.assertEqual(Decimal(totals["entry_avg_price"]), Decimal("17.5"))
        self.assertEqual(Decimal(totals["entry_qty_executed"]), Decimal("4"))

    def test_qty_executada_difere_da_planejada(self):
        fills = _norm(_fill("1", qty="0.002716", price="1", commission="0"))
        totals = ea.compute_totals(fills, [], [])
        self.assertEqual(Decimal(totals["entry_qty_executed"]), Decimal("0.002716"))
        self.assertNotEqual(Decimal(totals["entry_qty_executed"]),
                            Decimal("0.0027525"))

    def test_short_usa_sell_como_entrada(self):
        self.assertEqual(ea.entry_exit_sides("short"), ("SELL", "BUY"))
        self.assertEqual(ea.entry_exit_sides("long"), ("BUY", "SELL"))
        self.assertIsNone(ea.entry_exit_sides("lateral"))

    def test_ultimo_preco_de_perna_guardado_separado(self):
        exits = _norm(_fill("9", side="SELL", price="10", qty="1",
                            commission="0", ts="100"),
                      _fill("10", side="SELL", price="30", qty="1",
                            commission="0", ts="200"))
        totals = ea.compute_totals([], exits, [])
        self.assertEqual(Decimal(totals["exit_avg_price"]), Decimal("20"))
        self.assertEqual(Decimal(totals["last_exit_price"]), Decimal("30"))

    def test_realized_r_usa_qty_inicial(self):
        valor, motivo = ea.realized_r_from_net("-10", "100", "95", "2")
        self.assertIsNone(motivo)
        self.assertEqual(Decimal(valor), Decimal("-1"))
        for args in (("-10", "100", "95", "0"), ("-10", "100", "100", "2"),
                     (None, "100", "95", "2"), ("-10", None, "95", "2")):
            v, m = ea.realized_r_from_net(*args)
            self.assertIsNone(v)
            self.assertIsNotNone(m)


# ════════════════════════════════════════════════════════════════════════════
#  FUNDING
# ════════════════════════════════════════════════════════════════════════════
class Funding(unittest.TestCase):

    def _f(self, income, tran="1", itype="FUNDING_FEE", asset="USDT"):
        return {"incomeType": itype, "income": income, "asset": asset,
                "tranId": tran, "symbol": "ALFAUSDT", "time": "1700000000000"}

    def test_sinal_preservado(self):
        itens = [ea.normalize_funding(self._f("-0.5", "1"))[0],
                 ea.normalize_funding(self._f("0.2", "2"))[0]]
        totals = ea.compute_totals([], [], itens, funding_state="CONFIRMED")
        self.assertEqual(Decimal(totals["funding_net"]), Decimal("-0.3"))

    def test_income_type_diferente_nao_entra(self):
        for tipo in ("COMMISSION", "TRANSFER", "REFERRAL_KICKBACK",
                     "WELCOME_BONUS", "REALIZED_PNL"):
            norm, motivo = ea.normalize_funding(self._f("1", "9", itype=tipo))
            self.assertIsNone(norm, tipo)
            self.assertIn("não é funding", motivo)

    def test_dedupe_por_tran_id(self):
        item = ea.normalize_funding(self._f("0.5", "77"))[0]
        totals = ea.compute_totals([], [], [item, dict(item)],
                                   funding_state="CONFIRMED")
        self.assertEqual(Decimal(totals["funding_net"]), Decimal("0.5"))

    def test_pendente_nao_apaga_net_trade(self):
        fills = _norm(_fill("1", commission="0.01", realized="1"))
        totals = ea.compute_totals(fills, [], [], funding_state="PENDING")
        self.assertEqual(Decimal(totals["net_trade"]), Decimal("0.99"))
        self.assertIsNone(totals["funding_net"])
        self.assertIsNone(totals["net_including_funding"])

    def test_funding_em_outro_ativo_nao_soma(self):
        item = ea.normalize_funding(self._f("0.5", "1", asset="BNB"))[0]
        totals = ea.compute_totals([], [], [item], funding_state="CONFIRMED")
        self.assertIsNone(totals["funding_net"])

    def test_paginacao_incompleta_nao_prova_zero(self):
        acc = ea.finalize_accounting(ea.merge_accounting(None, identity=_ident()),
                                     funding_window_complete=False)
        self.assertIsNone(acc["totals"]["funding_net"])
        self.assertEqual(acc["funding_state"], "PENDING")

    def test_janela_vazia_sem_exposicao_provada_nao_prova_zero(self):
        acc = ea.finalize_accounting(ea.merge_accounting(None, identity=_ident()),
                                     funding_window_complete=True)
        self.assertIsNone(acc["totals"]["funding_net"])


# ════════════════════════════════════════════════════════════════════════════
#  ATRIBUIÇÃO, IDENTIDADE E ESTADOS
# ════════════════════════════════════════════════════════════════════════════
class Atribuicao(unittest.TestCase):

    def test_fill_de_outra_ordem_nao_e_atribuido(self):
        fills = _norm(_fill("1", order_id="100"), _fill("2", order_id="999"))
        res = ea.attribute_fills(fills, identity=_ident(),
                                 entry_order_ids=["100"], exit_order_ids=[])
        self.assertEqual(len(res["entry"]), 1)
        self.assertEqual(len(res["unattributed"]), 1)

    def test_simbolo_ou_position_side_divergente_e_estrangeiro(self):
        fills = _norm(_fill("1", symbol="BETAUSDT"),
                      _fill("2", position_side="LONG"))
        res = ea.attribute_fills(fills, identity=_ident(),
                                 entry_order_ids=["100"], exit_order_ids=[])
        self.assertEqual(res["foreign_symbol_or_position"], 2)
        self.assertEqual(res["entry"], [])

    def test_trades_sobrepostos_viram_ambiguo(self):
        fills = _norm(_fill("1", order_id="100"), _fill("2", order_id="777"))
        acc = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident(), fills=fills),
            entry_order_ids=["100"], exit_order_ids=[],
            fills_window_complete=True, funding_window_complete=True)
        self.assertEqual(acc["state"], "AMBIGUOUS")
        self.assertEqual(acc["reason_code"], "UNATTRIBUTED_FILLS")

    def test_identidade_insuficiente_fica_pendente(self):
        for kwargs in ({"symbol": None}, {"side": "x"}, {"entry_order_id": None}):
            ident = _ident()
            ident.update(kwargs)
            ok, motivo = ea.identity_is_sufficient(ident)
            self.assertFalse(ok)
            self.assertTrue(motivo.startswith("IDENTITY_"))

    def test_quantidade_nao_conservada_fica_parcial(self):
        fills = _norm(_fill("1", order_id="100", qty="2", commission="0"),
                      _fill("2", order_id="200", side="SELL", qty="1",
                            commission="0", realized="1"))
        acc = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident(), fills=fills),
            entry_order_ids=["100"], exit_order_ids=["200"],
            fills_window_complete=True, funding_window_complete=True)
        self.assertEqual(acc["state"], "PARTIAL")
        self.assertEqual(acc["reason_code"], "ENTRY_ORDER_NOT_PROVEN")

    def test_janela_incompleta_fica_parcial(self):
        fills = _norm(_fill("1", order_id="100", commission="0"))
        acc = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident(), fills=fills),
            entry_order_ids=["100"], fills_window_complete=False)
        self.assertEqual(acc["state"], "PARTIAL")
        self.assertEqual(acc["reason_code"], "ENTRY_ORDER_NOT_PROVEN")

    def test_sem_fill_de_entrada_fica_pendente(self):
        acc = ea.finalize_accounting(ea.merge_accounting(None, identity=_ident()),
                                     entry_order_ids=["100"],
                                     fills_window_complete=True)
        self.assertEqual(acc["state"], "PENDING")
        self.assertEqual(acc["reason_code"], "NO_ENTRY_FILL")

    def test_posicao_flat_com_contabilidade_pendente(self):
        acc = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident()),
            entry_order_ids=["100"], fills_window_complete=False,
            position_flat=True)
        self.assertTrue(acc["coverage"]["position_flat"])
        self.assertIn(acc["state"], ("PENDING", "PARTIAL"))
        self.assertIsNone(acc["totals"]["net_trade"])

    def test_saida_ausente_nunca_usa_a_entrada(self):
        fills = _norm(_fill("1", order_id="100", price="10", commission="0"))
        acc = ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident(), fills=fills),
            entry_order_ids=["100"], fills_window_complete=True)
        self.assertIsNone(acc["totals"]["exit_avg_price"])
        self.assertIsNone(acc["totals"]["last_exit_price"])

    def test_origem_do_fechamento(self):
        self.assertEqual(ea.close_origin({"client_order_id": "cw-123-sl"}),
                         "BOT_MANAGED")
        for coid in ("ios_abc", "", None, "x-outro"):
            self.assertEqual(ea.close_origin({"client_order_id": coid}),
                             "EXTERNAL_OR_UNKNOWN", repr(coid))


# ════════════════════════════════════════════════════════════════════════════
#  IDEMPOTÊNCIA, CONFLITO E CONCORRÊNCIA
# ════════════════════════════════════════════════════════════════════════════
class Idempotencia(unittest.TestCase):

    def _acc(self, fills, previous=None):
        return ea.merge_accounting(previous, identity=_ident(), fills=fills)

    def test_mesmo_exec_id_repetido_e_no_op(self):
        f = _norm(_fill("1", commission="0.01", realized="2"))
        a = self._acc(f)
        b = self._acc(f, a)
        self.assertEqual(len(b["fills"]), 1)
        self.assertEqual(b["conflicts"], [])

    def test_mesmo_exec_id_conflitante_vira_conflict(self):
        a = self._acc(_norm(_fill("1", price="10")))
        b = self._acc(_norm(_fill("1", price="99")), a)
        self.assertTrue(b["conflicts"])
        final = ea.finalize_accounting(b, entry_order_ids=["100"],
                                       fills_window_complete=True)
        self.assertEqual(final["state"], "CONFLICT")
        # o primeiro confirmado é preservado, não sobrescrito
        self.assertEqual(b["fills"][list(b["fills"])[0]]["price"], "10")

    def test_eventos_fora_de_ordem_nao_perdem_nada(self):
        f1 = _norm(_fill("1", order_id="100", qty="1", commission="0"))
        f2 = _norm(_fill("2", order_id="200", side="SELL", qty="1",
                         commission="0", realized="5"))
        direto = ea.finalize_accounting(self._acc(f1 + f2),
                                        entry_order_ids=["100"],
                                        exit_order_ids=["200"],
                                        fills_window_complete=True,
                                        funding_window_complete=True)
        invertido = ea.finalize_accounting(self._acc(f1, self._acc(f2)),
                                           entry_order_ids=["100"],
                                           exit_order_ids=["200"],
                                           fills_window_complete=True,
                                           funding_window_complete=True)
        self.assertEqual(direto["totals"]["net_trade"],
                         invertido["totals"]["net_trade"])
        self.assertEqual(direto["state"], invertido["state"])

    def test_duas_parciais_entre_polls(self):
        fills = _norm(
            _fill("1", order_id="100", qty="10", commission="0"),
            _fill("2", order_id="201", side="SELL", qty="4", commission="0",
                  realized="2"),
            _fill("3", order_id="202", side="SELL", qty="6", commission="0",
                  realized="3"))
        acc = ea.finalize_accounting(self._acc(fills), entry_order_ids=["100"],
                                     exit_order_ids=["201", "202"],
                                     fills_window_complete=True,
                                     funding_window_complete=True)
        self.assertEqual(acc["state"], "PARTIAL")  # quantities alone cannot prove finality
        self.assertEqual(Decimal(acc["totals"]["gross_realized"]), Decimal("5"))
        self.assertTrue(acc["coverage"]["quantity_balanced"])

    def test_acumulado_recalculado_nunca_incrementado(self):
        f = _norm(_fill("1", commission="0.01", realized="2"))
        acc = self._acc(f)
        for _ in range(5):
            acc = self._acc(f, acc)
        final = ea.finalize_accounting(acc, entry_order_ids=["100"],
                                       fills_window_complete=True)
        self.assertEqual(Decimal(final["totals"]["gross_realized"]), Decimal("2"))
        self.assertEqual(Decimal(final["totals"]["fees_by_asset"]["USDT"]),
                         Decimal("0.01"))

    def test_retry_com_backoff_finito(self):
        acc = ea.empty_accounting(identity=_ident())
        for i in range(ea.MAX_ATTEMPTS):
            acc = ea.schedule_retry(acc, error="timeout", now=NOW)
        self.assertEqual(acc["attempts"], ea.MAX_ATTEMPTS)
        self.assertIsNone(acc["next_retry_at"])
        self.assertEqual(acc["state"], "FAILED")
        self.assertFalse(ea.is_retry_due(acc, now=NOW + timedelta(days=30)))

    def test_legado_nunca_e_selecionado_pelo_retry(self):
        for legado in (None, {}, {"schema_version": 0}, ea.legacy_accounting()):
            self.assertFalse(ea.is_retry_due(legado), repr(legado))

    def test_confirmado_nao_e_reprocessado(self):
        acc = {"schema_version": 1, "state": "CONFIRMED", "funding_state": "CONFIRMED"}
        self.assertFalse(ea.is_retry_due(acc))
        self.assertTrue(ea.accounting_is_confirmed(acc))
        self.assertFalse(ea.accounting_is_confirmed({"schema_version": 0,
                                                     "state": "CONFIRMED"}))

    def test_legado_nao_e_confirmacao_retroativa(self):
        legacy = ea.legacy_accounting()
        self.assertEqual(legacy["state"], "LEGACY_UNVERIFIED")
        self.assertFalse(ea.accounting_is_confirmed(legacy))
        self.assertEqual(ea.project_to_trade_fields(legacy), {})
        self.assertEqual(ea.project_to_trade_fields(None), {})


# ════════════════════════════════════════════════════════════════════════════
#  COLETA — GET, janelas, orçamento (transporte mockado, código real)
# ════════════════════════════════════════════════════════════════════════════
class _FakeClient:
    """Transporte falso: registra o método HTTP implícito de cada chamada."""

    def __init__(self, *, fills=None, income=None, orders=None,
                 fills_error=None, income_error=None, page_limit=1000):
        self.fills = fills or []
        self.income = income or []
        self.orders = orders or {}
        self.fills_error = fills_error
        self.income_error = income_error
        self.page_limit = page_limit
        self.calls: list = []

    def accounting_scope(self):
        return "synthetic-account"

    async def get_executions(self, symbol, limit=1000, start_time=None, end_time=None):
        self.calls.append(("GET", "userTrades", symbol, start_time, end_time))
        if self.fills_error:
            return {"ok": False, "error": self.fills_error}
        rows = [f for f in self.fills
                if (start_time is None or int(f["time"]) >= start_time)
                and (end_time is None or int(f["time"]) <= end_time)]
        return {"ok": True, "raw": rows[: self.page_limit],
                "fills": [], "limit": self.page_limit}

    async def get_income(self, symbol, income_type="FUNDING_FEE",
                         start_time=None, end_time=None, limit=1000):
        self.calls.append(("GET", "income", symbol, start_time, end_time))
        if self.income_error:
            return {"ok": False, "error": self.income_error}
        rows = [f for f in self.income
                if (start_time is None or int(f["time"]) >= start_time)
                and (end_time is None or int(f["time"]) <= end_time)]
        return {"ok": True, "income": rows[: self.page_limit], "limit": self.page_limit}

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        self.calls.append(("GET", "order", symbol, order_id, None))
        raw = self.orders.get(str(order_id))
        if order_id is None:
            raw = next((o for o in self.orders.values() if o.get("clientOrderId") == client_order_id), None)
        return {"ok": True, "raw": raw} if raw else {"ok": False, "error": "not found"}


def _view(**kw):
    base = {"id": 1, "symbol": "ALFA/USDT:USDT", "side": "long",
            "exchange": "binance", "exchange_order_id": "100",
            "client_order_id": "cw-1", "planned_stop": "95",
            "opened_at": datetime(2023, 11, 14, tzinfo=timezone.utc),
            "closed_at": datetime(2023, 11, 14, 2, tzinfo=timezone.utc),
            "status": "closed_manual", "exclusive_exposure": True,
            "execution_accounting": ea.empty_accounting(identity={"account_scope": "synthetic-account"})}
    base.update(kw)
    return base


class Coleta(unittest.IsolatedAsyncioTestCase):

    def _fills(self):
        t0 = int(datetime(2023, 11, 14, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
        return [_fill("1", order_id="100", price="100", qty="1", commission="0.04",
                      realized="0", ts=str(t0)),
                _fill("2", order_id="200", side="SELL", price="110", qty="1",
                      commission="0.04", realized="10", ts=str(t0 + 1000))]

    def _order(self, order_id="200", coid="cw-1-sl"):
        return {"100": {"orderId": "100", "clientOrderId": "cw-1", "symbol": "ALFAUSDT",
                        "side": "BUY", "positionSide": "BOTH", "status": "FILLED", "executedQty": "1"},
                order_id: {"orderId": order_id, "clientOrderId": coid,
                           "status": "FILLED", "symbol": "ALFAUSDT",
                           "side": "SELL", "positionSide": "BOTH",
                           "executedQty": "1", "avgPrice": "110",
                           "reduceOnly": True, "type": "MARKET",
                           "updateTime": "1700000000000"}}

    async def test_coleta_confirma_com_ordem_de_saida(self):
        client = _FakeClient(fills=self._fills(), income=[],
                             orders=self._order())
        acc = await ea.collect_trade_accounting(
            _view(tp1_order_id=None), client=client, now=NOW)
        self.assertEqual(acc["state"], "CONFIRMED")
        self.assertEqual(Decimal(acc["totals"]["net_trade"]), Decimal("9.92"))
        self.assertEqual(acc["close_origin"], "BOT_MANAGED")
        self.assertTrue(all(c[0] == "GET" for c in client.calls))

    async def test_origem_externa_marcada(self):
        client = _FakeClient(fills=self._fills(), income=[],
                             orders=self._order(coid="ios_XYZ"))
        acc = await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        self.assertEqual(acc["close_origin"], "EXTERNAL_OR_UNKNOWN")

    async def test_falha_de_rede_vira_pendente_com_backoff(self):
        client = _FakeClient(fills_error="timeout")
        acc = await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        self.assertNotEqual(acc["state"], "CONFIRMED")
        self.assertIsNone(acc["totals"]["net_trade"])
        self.assertEqual(acc["attempts"], 1)
        self.assertIsNotNone(acc["next_retry_at"])

    async def test_funding_indisponivel_nao_vira_zero(self):
        client = _FakeClient(fills=self._fills(), income_error="500",
                             orders=self._order())
        acc = await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        self.assertIsNone(acc["totals"]["funding_net"])
        self.assertEqual(acc["funding_state"], "PENDING")

    async def test_identidade_insuficiente_nao_chama_a_exchange(self):
        client = _FakeClient(fills=self._fills())
        acc = await ea.collect_trade_accounting(
            _view(exchange_order_id=None, client_order_id=None),
            client=client, now=NOW)
        self.assertEqual(acc["state"], "PENDING")
        self.assertEqual(client.calls, [])

    async def test_orcamento_de_chamadas_e_finito(self):
        client = _FakeClient(fills=self._fills(), income=[], orders=self._order())
        await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        self.assertLessEqual(len(client.calls), ea.MAX_CALLS_PER_TRADE)

    async def test_nenhum_post_delete_put(self):
        client = _FakeClient(fills=self._fills(), income=[], orders=self._order())
        await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        for metodo, *_ in client.calls:
            self.assertEqual(metodo, "GET")

    def test_janelas_de_no_maximo_sete_dias(self):
        inicio = 0
        fim = ea.MAX_TRADE_WINDOW_MS * 3
        janelas = ea.plan_windows(inicio, fim)
        self.assertGreaterEqual(len(janelas), 3)
        for a, b in janelas:
            self.assertLessEqual(b - a, ea.MAX_TRADE_WINDOW_MS)
        self.assertEqual(janelas[0][0], inicio)
        self.assertEqual(janelas[-1][1], fim)
        # sem buraco entre janelas
        for anterior, atual in zip(janelas, janelas[1:]):
            self.assertEqual(atual[0], anterior[1] + 1)

    async def test_pagina_cheia_com_timestamps_iguais_nao_declara_completo(self):
        # Timestamp DENTRO da janela do trade; a página cheia com o mesmo ms
        # não avança o cursor e, por isso, não pode declarar completude.
        ts = str(int(datetime(2023, 11, 14, 1, tzinfo=timezone.utc).timestamp() * 1000))
        rows = [_fill(str(i), order_id="100", ts=ts, commission="0")
                for i in range(3)]
        client = _FakeClient(fills=rows, income=[], page_limit=3)
        acc = await ea.collect_trade_accounting(_view(), client=client, now=NOW)
        self.assertFalse(acc["coverage"]["fills_window_complete"])
        self.assertNotEqual(acc["state"], "CONFIRMED")


# ════════════════════════════════════════════════════════════════════════════
#  PROJEÇÃO PARA AS COLUNAS LEGADAS
# ════════════════════════════════════════════════════════════════════════════
class Projecao(unittest.TestCase):

    def _confirmed(self):
        fills = _norm(_fill("1", order_id="100", price="100", qty="1",
                            commission="0.04"),
                      _fill("2", order_id="200", side="SELL", price="110",
                            qty="1", commission="0.04", realized="10"))
        return ea.finalize_accounting(
            ea.merge_accounting(None, identity=_ident(), fills=fills, orders=[
                {**ea.normalize_order(o), "role": "entry" if oid == "100" else "exit"}
                for oid, o in Coleta()._order().items()]),
            entry_order_ids=["100"], exit_order_ids=["200"],
            fills_window_complete=True, funding_window_complete=True,
            planned_stop="95")

    def test_projeta_entrada_qty_saida_e_pnl(self):
        campos = ea.project_to_trade_fields(self._confirmed())
        self.assertEqual(campos["entry_price"], 100.0)
        self.assertEqual(campos["qty_initial"], 1.0)
        self.assertEqual(campos["exit_price"], 110.0)
        self.assertEqual(campos["pnl_usd"], 9.92)
        self.assertEqual(campos["entry_fee"], 0.04)
        self.assertEqual(campos["exit_fee"], 0.04)

    def test_pnl_exclui_funding(self):
        acc = self._confirmed()
        funding = ea.normalize_funding({"incomeType": "FUNDING_FEE",
                                        "income": "-1", "asset": "USDT",
                                        "tranId": "1", "symbol": "ALFAUSDT",
                                        "time": "1"})[0]
        acc = ea.finalize_accounting(
            ea.merge_accounting(acc, funding=[funding]),
            entry_order_ids=["100"], exit_order_ids=["200"],
            fills_window_complete=True, funding_window_complete=True)
        self.assertEqual(Decimal(acc["totals"]["net_trade"]), Decimal("9.92"))
        self.assertEqual(Decimal(acc["totals"]["net_including_funding"]),
                         Decimal("8.92"))
        self.assertEqual(ea.project_to_trade_fields(acc)["pnl_usd"], 9.92)

    def test_pnl_nao_e_projetado_quando_desconhecido(self):
        acc = ea.finalize_accounting(ea.merge_accounting(None, identity=_ident()),
                                     entry_order_ids=["100"])
        self.assertIsNone(ea.project_to_trade_fields(acc)["pnl_usd"])

    def test_precisao_preservada_no_json(self):
        acc = self._confirmed()
        self.assertEqual(acc["totals"]["net_trade"], "9.92")
        self.assertIsInstance(acc["totals"]["net_trade"], str)

    def test_realized_r_da_entrada_real(self):
        campos = ea.project_to_trade_fields(self._confirmed())
        # (110-100)*1 - 0.08 = 9.92 ; risco = |100-95| * 1 = 5
        self.assertAlmostEqual(campos["realized_r"], 1.984, places=3)


# ════════════════════════════════════════════════════════════════════════════
#  ARQUITETURA E INTEGRAÇÃO
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services" / "execution_accounting_service.py").read_text()
        cls.rts = (BACKEND / "services" / "real_trade_service.py").read_text()
        cls.tms = (BACKEND / "services" / "trade_manager_service.py").read_text()
        cls.main = (BACKEND / "main.py").read_text()

    @staticmethod
    def _codigo(bloco: str) -> str:
        linhas, dentro = [], False
        for ln in bloco.splitlines():
            aspas = ln.count('"""')
            if dentro:
                if aspas:
                    dentro = False
                continue
            if ln.lstrip().startswith("#"):
                continue
            if aspas == 1:
                dentro = True
                continue
            if aspas >= 2:
                ln = ln.split('"""')[0]
            linhas.append(ln)
        return "\n".join(linhas)

    def test_somente_get_no_servico_contabil(self):
        codigo = self._codigo(self.src)
        for proibido in ('"POST"', "'POST'", '"DELETE"', "'DELETE'", '"PUT"',
                         "place_order", "cancel_order", "cancel_algo_order",
                         "send_telegram", "notify_", "push_service"):
            self.assertNotIn(proibido, codigo, proibido)
        for permitido in ("get_executions", "get_income", "get_order"):
            self.assertIn(permitido, codigo, permitido)

    def test_nenhum_loop_worker_fila_ou_endpoint_novo(self):
        codigo = self._codigo(self.src)
        for proibido in ("asyncio.create_task", "while True", "Thread(",
                         "Queue(", "@app."):
            self.assertNotIn(proibido, codigo, proibido)
        rotas = [ln for ln in self.main.splitlines() if ln.lstrip().startswith("@app.")]
        for proibido in ("accounting", "r05c", "reconcile-fills"):
            self.assertFalse([r for r in rotas if proibido in r.lower()], proibido)

    def test_nenhuma_env_ou_flag_nova(self):
        self.assertNotIn("os.getenv", self._codigo(self.src))
        self.assertNotIn("R05C_", self.src.replace("R05C —", "").replace("[r05c]", ""))

    def test_uma_unica_coluna_aditiva(self):
        db = (BACKEND / "db.py").read_text()
        self.assertIn("ADD COLUMN IF NOT EXISTS execution_accounting JSONB", db)
        self.assertEqual(db.count("execution_accounting"), 1)
        self.assertNotIn("CREATE TABLE execution", db)
        modelo = (BACKEND / "models" / "real_trade.py").read_text()
        self.assertIn("execution_accounting", modelo)
        self.assertIn("nullable=True", modelo.split("execution_accounting")[1][:200])

    def test_sem_backfill_automatico_de_historico(self):
        codigo = self._codigo(self.src)
        for proibido in ("UPDATE real_trades", "backfill", "apply_history",
                         "recalc_all"):
            self.assertNotIn(proibido, codigo, proibido)
        # a seleção do retry exige schema novo e nunca varre NULL
        seletor = self.src.split("async def pending_trade_ids")[1].split("\nasync def ")[0]
        self.assertIn("execution_accounting.is_not(None)", seletor)

    def test_close_legado_nao_sobrescreve_confirmado(self):
        bloco = self.rts.split("async def close_trade")[1].split("\nasync def ")[0]
        self.assertIn("_r05c_locked", bloco)
        self.assertIn("accounting_is_confirmed", bloco)
        self.assertIn("if not _r05c_locked:", bloco)
        self.assertIn("if _r05c_locked:", bloco)

    def test_trade_novo_nasce_no_contrato(self):
        bloco = self.rts.split("async def open_trade")[1].split("\nasync def ")[0]
        self.assertIn("empty_accounting", bloco)
        self.assertIn('source == "auto"', bloco)

    def test_retry_no_ciclo_existente_com_lote_pequeno(self):
        self.assertIn("_reconcile_pending_accounting", self.tms)
        bloco = self.tms.split("async def _reconcile_pending_accounting")[1].split(
            "\nasync def ")[0]
        self.assertIn("limit=limit", bloco)
        self.assertIn("except Exception", bloco)
        self.assertEqual(self.tms.count("async def loop()"), 1)
        # roda DEPOIS do processamento das posições abertas
        tick = self.tms.split("async def _tick()")[1].split("\nasync def ")[0]
        self.assertLess(tick.index("_process_trade"),
                        tick.index("_reconcile_pending_accounting"))

    def test_lote_limitado_a_cinco(self):
        seletor = self.src.split("async def pending_trade_ids")[1].split("\nasync def ")[0]
        self.assertIn("min(int(limit or 5), 5)", seletor)

    def test_apply_usa_bloqueio_de_linha(self):
        bloco = self.src.split("async def apply_accounting")[1].split("\nasync def ")[0]
        self.assertIn("with_for_update()", bloco)
        self.assertIn("merge_observation", bloco)
        self.assertIn("finalize_accounting", self.src.split("def merge_observation")[1].split("async def apply_accounting")[0])
        for proibido in ("get_executions", "get_income", "get_order"):
            self.assertNotIn(proibido, bloco, f"I/O dentro da transação: {proibido}")

    def test_r05a_distingue_registrado_de_reconciliado(self):
        r05a = (BACKEND / "services" / "risk_reconciliation_service.py").read_text()
        for campo in ("execution_reconciled", "fees_reconciled",
                      "funding_reconciled_usd", "legacy_unverified",
                      "execution_states"):
            self.assertIn(campo, r05a, campo)
        bloco = r05a.split('bucket["fees_complete"] = bool(')[1][:300]
        self.assertIn("recon == total", bloco)

    def test_flags_de_producao_intactas(self):
        """O R05C não alterou os arquivos fora do escopo DA SUA FASE.

        Verificado sobre os commits da fase (`59a9448f..61265ed1`), não sobre o
        working tree: fases posteriores (R06A) alteram `frontend/src` com
        autorização explícita — no caso, apenas texto de tela.
        """
        import subprocess
        for caminho in ("backend/services/shadow_trade_service.py",
                        "backend/services/risk_service.py",
                        "backend/services/strategy_evidence_service.py",
                        "frontend/src"):
            res = subprocess.run(
                ["git", "diff", "--name-only", "59a9448f", "61265ed1", "--", caminho],
                cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("commits da fase R05C indisponíveis neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"R05C alterou arquivo fora do escopo: {caminho}")


class ReadersEIntegracao(unittest.IsolatedAsyncioTestCase):

    async def test_falha_contabil_nao_derruba_o_ciclo(self):
        from services import trade_manager_service as tms

        async def _boom(limit=5):
            raise RuntimeError("db fora")

        with patch.object(tms, "_reconcile_pending_accounting", _boom):
            with self.assertRaises(RuntimeError):
                await tms._reconcile_pending_accounting()
        # o helper real engole o erro internamente
        from services import execution_accounting_service as _ea
        with patch.object(_ea, "pending_trade_ids", side_effect=RuntimeError("x")):
            await tms._reconcile_pending_accounting()

    async def test_reconcile_trade_e_fail_soft(self):
        with patch.object(ea, "collect_trade_accounting",
                          side_effect=RuntimeError("erro")):
            out = await ea.reconcile_trade(1)
        self.assertFalse(out["ok"])
        self.assertIn("reason_code", out)

    async def test_apply_sem_db_e_no_op(self):
        out = await ea.apply_accounting(1, {"schema_version": 1})
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
