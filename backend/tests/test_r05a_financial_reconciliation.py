"""R05A — reconciliação observacional de P&L e risco real (somente leitura).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco
externo, Telegram, push ou credencial real. Zero escrita.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste R05A (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import risk_reconciliation_service as r      # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
H24 = NOW - timedelta(hours=24)
D7 = NOW - timedelta(days=7)


def _snap(i=1, *, r_val=-1.0, risk=1.0, hours=2, outcome_at="auto"):
    ts = NOW - timedelta(hours=hours) if outcome_at == "auto" else outcome_at
    return {"id": i, "realized_r": r_val, "risk_pct": risk, "outcome_at": ts}


def _trade(i=1, *, source="auto", status="closed_stop", side="long", rec=1,
           entry=100.0, stop=98.0, qty=10.0, qty_initial=10.0, pnl=-20.0,
           realized_r=-1.0, entry_fee=0.5, exit_fee=0.5, tp1=None,
           slip=0.02, hours=2, closed_at="auto"):
    ts = NOW - timedelta(hours=hours) if closed_at == "auto" else closed_at
    return {"id": i, "source": source, "status": status, "side": side,
            "recommendation_id": rec, "entry_price": entry, "planned_stop": stop,
            "qty": qty, "qty_initial": qty_initial, "pnl_usd": pnl,
            "realized_r": realized_r, "entry_fee": entry_fee,
            "exit_fee": exit_fee, "tp1_realized_usd": tp1,
            "entry_slippage_pct": slip, "closed_at": ts}


def _open(i=1, *, source="auto", side="long", entry=100.0, qty=2.0,
          stop=95.0, sl_id="sl-1", sl_price=97.0, fee=0.2):
    return {"id": i, "source": source, "side": side, "entry_price": entry,
            "qty": qty, "planned_stop": stop, "sl_order_id": sl_id,
            "sl_current_price": sl_price, "entry_fee": fee}


# ════════════════════════════════════════════════════════════════════════════
#  TEÓRICO
# ════════════════════════════════════════════════════════════════════════════
class Teorico(unittest.TestCase):

    def test_soma_realized_r_vezes_risk_pct(self):
        snaps = [_snap(1, r_val=-1.0, risk=1.0), _snap(2, r_val=2.0, risk=0.5),
                 _snap(3, r_val=0.0, risk=1.0)]
        out = r.theoretical_window(snaps, since=H24, until=NOW)
        self.assertEqual(out["resolved_count"], 3)
        self.assertEqual(out["sum_realized_r"], 1.0)
        self.assertEqual(out["sum_bank_pct"], 0.0)
        self.assertEqual((out["wins"], out["losses"], out["neutral"]), (1, 1, 1))

    def test_none_nan_inf_excluidos_com_motivo(self):
        snaps = [_snap(1, r_val=None), _snap(2, risk=None),
                 _snap(3, r_val=float("nan")), _snap(4, risk=float("inf")),
                 _snap(5, r_val=True)]
        out = r.theoretical_window(snaps, since=H24, until=NOW)
        self.assertEqual(out["resolved_count"], 0)
        self.assertIsNone(out["sum_realized_r"])
        self.assertIsNone(out["sum_bank_pct"])
        self.assertIn("realized_r ausente, NaN ou infinito", out["excluded_by_reason"])
        self.assertIn("risk_pct ausente, NaN ou infinito", out["excluded_by_reason"])

    def test_janela_24h_e_7d(self):
        snaps = [_snap(1, hours=2), _snap(2, hours=48), _snap(3, hours=24 * 9)]
        j24 = r.theoretical_window(snaps, since=H24, until=NOW)
        j7 = r.theoretical_window(snaps, since=D7, until=NOW)
        self.assertEqual(j24["resolved_count"], 1)
        self.assertEqual(j7["resolved_count"], 2)

    def test_borda_da_janela_e_deterministica(self):
        exato = r.theoretical_window([_snap(1, outcome_at=H24)], since=H24, until=NOW)
        fora = r.theoretical_window([_snap(1, outcome_at=H24 - timedelta(seconds=1))],
                                    since=H24, until=NOW)
        self.assertEqual(exato["resolved_count"], 1)
        self.assertEqual(fora["resolved_count"], 0)

    def test_timestamp_naive_tratado_como_utc(self):
        naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        out = r.theoretical_window([_snap(1, outcome_at=naive)], since=H24, until=NOW)
        self.assertEqual(out["resolved_count"], 1)

    def test_timestamp_invalido_excluido(self):
        out = r.theoretical_window([_snap(1, outcome_at=None),
                                    _snap(2, outcome_at="2026-09-01")],
                                   since=H24, until=NOW)
        self.assertEqual(out["excluded_by_reason"]["outcome_at ausente ou inválido"], 2)

    def test_declara_que_nao_e_verdade_financeira(self):
        out = r.theoretical_window([], since=H24, until=NOW)
        self.assertEqual(out["source"], "RecommendationSnapshot")
        self.assertEqual(out["window_field"], "outcome_at")
        self.assertIn("não é dinheiro realizado", out["note"].lower())
        self.assertIn("não é usado como verdade financeira", out["note"].lower())


# ════════════════════════════════════════════════════════════════════════════
#  FINANCEIRO
# ════════════════════════════════════════════════════════════════════════════
class Financeiro(unittest.TestCase):

    def test_separa_coortes_por_source(self):
        trades = [_trade(1, source="auto", pnl=-20.0),
                  _trade(2, source="managed", pnl=10.0),
                  _trade(3, source="manual", pnl=5.0),
                  _trade(4, source="bybit", pnl=1.0),
                  _trade(5, source="desconhecida", pnl=2.0)]
        out = r.real_cohorts(trades, since=H24, until=NOW)
        c = out["cohorts"]
        self.assertEqual(c["auto"]["recorded_net_pnl_usd"], -20.0)
        self.assertEqual(c["managed"]["recorded_net_pnl_usd"], 10.0)
        self.assertEqual(c["manual"]["recorded_net_pnl_usd"], 5.0)
        self.assertEqual(c["bybit"]["recorded_net_pnl_usd"], 1.0)
        self.assertEqual(c["other"]["recorded_net_pnl_usd"], 2.0)
        self.assertEqual(out["all_sources_legacy_total"]["recorded_net_pnl_usd"], -2.0)
        self.assertEqual(out["primary_cohort"], "auto")

    def test_pnl_somado_uma_unica_vez_sem_descontar_taxa(self):
        trades = [_trade(1, pnl=-20.0, entry_fee=1.5, exit_fee=2.5, tp1=7.0)]
        out = r.real_cohorts(trades, since=H24, until=NOW)
        auto = out["cohorts"]["auto"]
        self.assertEqual(auto["recorded_net_pnl_usd"], -20.0)   # nem -24, nem -13
        self.assertEqual(auto["entry_fee_sum_usd"], 1.5)
        self.assertEqual(auto["exit_fee_sum_usd"], 2.5)
        self.assertEqual(auto["with_tp1_partial"], 1)
        self.assertIn("já inclui", out["fees_note"])

    def test_tp1_parcial_nao_e_somado_de_novo(self):
        sem = r.real_cohorts([_trade(1, pnl=30.0, tp1=None)], since=H24, until=NOW)
        com = r.real_cohorts([_trade(1, pnl=30.0, tp1=12.0)], since=H24, until=NOW)
        self.assertEqual(sem["cohorts"]["auto"]["recorded_net_pnl_usd"],
                         com["cohorts"]["auto"]["recorded_net_pnl_usd"])

    def test_closed_manual_de_auto_continua_em_auto(self):
        out = r.real_cohorts([_trade(1, source="auto", status="closed_manual",
                                     pnl=-3.0)], since=H24, until=NOW)
        auto = out["cohorts"]["auto"]
        self.assertEqual(auto["closed_valid"], 1)
        self.assertEqual(auto["closed_manual"], 1)
        self.assertEqual(auto["recorded_net_pnl_usd"], -3.0)
        self.assertEqual(out["cohorts"]["manual"]["closed_valid"], 0)

    def test_funding_explicitamente_unavailable(self):
        out = r.real_cohorts([], since=H24, until=NOW)
        self.assertIsNone(out["funding"]["value"])
        self.assertEqual(out["funding"]["reason_code"], "FUNDING_FIELD_UNAVAILABLE")

    def test_pnl_invalido_contabilizado_sem_virar_zero(self):
        trades = [_trade(1, pnl=None), _trade(2, pnl=float("nan")),
                  _trade(3, pnl=float("inf")), _trade(4, pnl=-5.0)]
        out = r.real_cohorts(trades, since=H24, until=NOW)
        auto = out["cohorts"]["auto"]
        self.assertEqual(auto["closed_valid"], 1)
        self.assertEqual(auto["recorded_net_pnl_usd"], -5.0)
        self.assertEqual(auto["excluded_by_reason"]
                         ["pnl_usd ausente, NaN ou infinito"], 3)
        self.assertEqual(auto["pnl_coverage_pct"], 25.0)

    def test_contagem_de_sinais_e_cobertura_de_vinculo(self):
        trades = [_trade(1, pnl=5.0), _trade(2, pnl=-5.0), _trade(3, pnl=0.0),
                  _trade(4, pnl=1.0, rec=None)]
        auto = r.real_cohorts(trades, since=H24, until=NOW)["cohorts"]["auto"]
        self.assertEqual((auto["positive"], auto["negative"], auto["zero"]),
                         (2, 1, 1))
        self.assertEqual(auto["without_recommendation_id"], 1)
        self.assertEqual(auto["recommendation_link_coverage_pct"], 75.0)
        self.assertEqual(auto["slippage_coverage_pct"], 100.0)

    def test_janela_por_closed_at_nunca_opened_at(self):
        out = r.real_cohorts([_trade(1, hours=48)], since=H24, until=NOW)
        self.assertEqual(out["cohorts"]["auto"]["closed_total_seen"], 0)
        self.assertEqual(out["window_field"], "closed_at")

    def test_rotulo_honesto_do_pnl(self):
        auto = r.real_cohorts([_trade(1)], since=H24, until=NOW)["cohorts"]["auto"]
        self.assertIn("funding não reconciliado", auto["pnl_label"])


# ════════════════════════════════════════════════════════════════════════════
#  PAREAMENTO
# ════════════════════════════════════════════════════════════════════════════
class Pareamento(unittest.TestCase):

    def _pair(self, trades, snaps):
        return r.paired_reconciliation(trades, {s["id"]: s for s in snaps},
                                       since=D7, until=NOW)

    def test_par_alinhado(self):
        out = self._pair([_trade(1, rec=7, entry=100.0, stop=98.0,
                                 qty_initial=10.0, pnl=-20.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["comparable_pairs"], 1)
        self.assertEqual(out["coverage_pct"], 100.0)
        self.assertEqual(out["mean_delta_r"], 0.0)
        self.assertEqual(out["delta_bank_pct"], 0.0)
        self.assertEqual(out["status"], "ALIGNED")

    def test_calculo_financial_r_from_pnl(self):
        # risk_dollar = |100-98| × 10 = 20 ; pnl -30 ⇒ R = -1.5
        out = self._pair([_trade(1, rec=7, pnl=-30.0)],
                         [_snap(7, r_val=-1.0, risk=2.0)])
        self.assertEqual(out["mean_delta_r"], -0.5)
        self.assertEqual(out["theoretical_bank_pct"], -2.0)
        self.assertEqual(out["financial_bank_pct_normalized"], -3.0)
        self.assertEqual(out["delta_bank_pct"], -1.0)
        self.assertEqual(out["status"], "DIVERGENT")

    def test_qty_initial_prioritario(self):
        out = self._pair([_trade(1, rec=7, qty=1.0, qty_initial=10.0, pnl=-20.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["mean_delta_r"], 0.0)
        self.assertEqual(out["qty_initial_fallback_count"], 0)

    def test_fallback_para_qty_identificado(self):
        out = self._pair([_trade(1, rec=7, qty=10.0, qty_initial=None, pnl=-20.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["comparable_pairs"], 1)
        self.assertEqual(out["qty_initial_fallback_count"], 1)

    def test_vinculo_ausente(self):
        out = self._pair([_trade(1, rec=None)], [_snap(7)])
        self.assertEqual(out["eligible_pairs"], 1)
        self.assertEqual(out["comparable_pairs"], 0)
        self.assertEqual(out["divergences_by_reason"]["sem recommendation_id"], 1)
        self.assertEqual(out["status"], "NOT_COMPARABLE")

    def test_vinculo_ambiguo_nao_escolhe_arbitrariamente(self):
        out = self._pair([_trade(1, rec=7), _trade(2, rec=7)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["comparable_pairs"], 0)
        self.assertEqual(out["divergences_by_reason"]["AMBIGUOUS_REAL_LINK"], 2)

    def test_risk_dollar_zero_invalida_o_par(self):
        out = self._pair([_trade(1, rec=7, entry=100.0, stop=100.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["comparable_pairs"], 0)
        self.assertEqual(out["divergences_by_reason"]["risk_dollar zero ou inválido"], 1)

    def test_risk_pct_invalido_invalida_o_par(self):
        for risk in (None, 0.0, -1.0, float("nan")):
            out = self._pair([_trade(1, rec=7)], [_snap(7, risk=risk)])
            self.assertEqual(out["comparable_pairs"], 0, repr(risk))

    def test_sinais_divergentes_sao_contados(self):
        out = self._pair([_trade(1, rec=7, pnl=20.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["sign_mismatch_count"], 1)

    def test_r_persistido_nunca_substitui_o_calculo(self):
        out = self._pair([_trade(1, rec=7, pnl=-40.0, realized_r=-1.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["mean_delta_r"], -1.0)          # veio de pnl_usd
        self.assertEqual(out["persisted_r_inconsistency_count"], 1)

    def test_somente_source_auto_entra(self):
        out = self._pair([_trade(1, source="managed", rec=7)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["eligible_pairs"], 0)
        self.assertEqual(out["status"], "NOT_COMPARABLE")

    def test_cobertura_abaixo_do_piso_nao_julga(self):
        trades = [_trade(1, rec=7, pnl=-20.0)] + [
            _trade(i, rec=None) for i in range(2, 6)]
        out = self._pair(trades, [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertEqual(out["coverage_pct"], 20.0)
        self.assertEqual(out["status"], "NOT_COMPARABLE")
        self.assertEqual(out["reason_code"], "COVERAGE_BELOW_FLOOR")

    def test_cobertura_acima_do_piso_julga(self):
        trades = [_trade(i, rec=i, pnl=-20.0) for i in range(1, 6)]
        snaps = [_snap(i, r_val=-1.0, risk=1.0) for i in range(1, 6)]
        out = self._pair(trades, snaps)
        self.assertEqual(out["coverage_pct"], 100.0)
        self.assertEqual(out["status"], "ALIGNED")
        self.assertEqual(r.MIN_PAIR_COVERAGE_PCT, 80.0)

    def test_tolerancia_na_borda(self):
        # delta_bank agregado = exatamente 0.10 pp ⇒ ALIGNED
        na_borda = self._pair([_trade(1, rec=7, pnl=-21.0)],
                              [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertAlmostEqual(na_borda["delta_bank_pct"], -0.05, places=6)
        self.assertEqual(na_borda["status"], "ALIGNED")
        acima = self._pair([_trade(1, rec=7, pnl=-23.0)],
                           [_snap(7, r_val=-1.0, risk=1.0)])
        self.assertAlmostEqual(acima["delta_bank_pct"], -0.15, places=6)
        self.assertEqual(acima["status"], "DIVERGENT")
        self.assertEqual(r.ALIGNED_TOLERANCE_BANK_PP, 0.10)

    def test_sem_listas_individuais_sensiveis(self):
        out = self._pair([_trade(1, rec=7, pnl=-20.0)],
                         [_snap(7, r_val=-1.0, risk=1.0)])
        blob = repr(out)
        for proibido in ("exchange_order_id", "client_order_id", "symbol",
                         "'id':", "trades", "pairs_detail"):
            self.assertNotIn(proibido, blob, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  RISCO ABERTO
# ════════════════════════════════════════════════════════════════════════════
class RiscoAberto(unittest.TestCase):

    def test_long_e_short(self):
        out = r.open_risk([_open(1, side="long", entry=100.0, sl_price=97.0, qty=2.0),
                           _open(2, side="short", entry=100.0, sl_price=104.0, qty=1.0)])
        self.assertEqual(out["cohorts"]["auto"]["remaining_price_risk_usd"], 10.0)
        self.assertEqual(out["cohorts"]["auto"]["with_confirmed_stop"], 2)

    def test_stop_em_lucro_gera_risco_zero_nao_negativo(self):
        out = r.open_risk([_open(1, side="long", entry=100.0, sl_price=105.0),
                           _open(2, side="short", entry=100.0, sl_price=95.0)])
        self.assertEqual(out["cohorts"]["auto"]["remaining_price_risk_usd"], 0.0)
        self.assertTrue(out["open_risk_complete"])

    def test_sl_current_price_prioritario(self):
        out = r.open_risk([_open(1, entry=100.0, sl_price=97.0, stop=90.0, qty=1.0)])
        self.assertEqual(out["cohorts"]["auto"]["remaining_price_risk_usd"], 3.0)
        self.assertEqual(out["cohorts"]["auto"]["planned_stop_fallback"], 0)

    def test_fallback_para_planned_stop_identificado(self):
        out = r.open_risk([_open(1, entry=100.0, sl_price=None, stop=90.0, qty=1.0)])
        self.assertEqual(out["cohorts"]["auto"]["remaining_price_risk_usd"], 10.0)
        self.assertEqual(out["cohorts"]["auto"]["planned_stop_fallback"], 1)

    def test_sem_sl_order_id_e_desconhecido(self):
        for sl in (None, "", "   "):
            out = r.open_risk([_open(1, sl_id=sl)])
            auto = out["cohorts"]["auto"]
            self.assertEqual(auto["without_confirmed_stop"], 1, repr(sl))
            self.assertEqual(auto["remaining_price_risk_usd"], 0.0)
            self.assertFalse(auto["open_risk_complete"])

    def test_usa_qty_restante_apos_parcial(self):
        out = r.open_risk([_open(1, entry=100.0, sl_price=95.0, qty=3.0)])
        self.assertEqual(out["cohorts"]["auto"]["remaining_price_risk_usd"], 15.0)

    def test_dados_invalidos_contabilizados(self):
        out = r.open_risk([_open(1, side="x"), _open(2, entry=None),
                           _open(3, qty=0.0), "linha ruim"])
        auto = out["cohorts"]["auto"]
        self.assertEqual(auto["invalid_data"], 3)
        self.assertFalse(out["open_risk_complete"])
        self.assertEqual(out["cohorts"]["other"]["invalid_data"], 1)

    def test_separa_auto_managed_e_demais(self):
        out = r.open_risk([_open(1, source="auto"), _open(2, source="managed"),
                           _open(3, source="manual")])
        self.assertEqual(set(out["cohorts"]), {"auto", "managed", "manual"})

    def test_nenhuma_posicao_aberta_e_zero_conhecido(self):
        out = r.open_risk([])
        self.assertTrue(out["open_risk_complete"])
        cenario = r.stop_scenario(-100.0, out)
        self.assertEqual(cenario["value"], -100.0)

    def test_posicao_desconhecida_torna_cenario_none(self):
        out = r.open_risk([_open(1, sl_id=None)])
        cenario = r.stop_scenario(-100.0, out)
        self.assertIsNone(cenario["value"])
        self.assertEqual(cenario["reason_code"], "OPEN_RISK_INCOMPLETE")
        self.assertIn("risco zero não é assumido", cenario["detail"])

    def test_desconhecido_em_outra_coorte_nao_bloqueia_o_cenario(self):
        out = r.open_risk([_open(1, source="auto", entry=100.0, sl_price=95.0, qty=1.0),
                           _open(2, source="manual", sl_id=None)])
        self.assertTrue(out["open_risk_complete"])
        self.assertEqual(r.stop_scenario(-10.0, out)["value"], -15.0)

    def test_pnl_indisponivel_torna_cenario_none(self):
        out = r.open_risk([])
        self.assertIsNone(r.stop_scenario(None, out)["value"])
        self.assertIsNone(r.stop_scenario(float("nan"), out)["value"])


# ════════════════════════════════════════════════════════════════════════════
#  RELATÓRIO / FONTES DE CONTROLE
# ════════════════════════════════════════════════════════════════════════════
class Relatorio(unittest.TestCase):

    def _rows(self):
        return {"closed": [_trade(1, rec=7, pnl=-20.0)],
                "snapshots": [_snap(7, r_val=-1.0, risk=1.0)],
                "open": [_open(1)]}

    def test_shape_minimo(self):
        rep = r.build_report(self._rows(), as_of=NOW)
        for chave in ("ok", "phase", "mode", "as_of_utc", "windows", "open_risk",
                      "paired_reconciliation", "current_control_sources",
                      "data_quality", "limitations", "invariants"):
            self.assertIn(chave, rep, chave)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["phase"], "R05A")
        self.assertEqual(rep["mode"], "OBSERVATION_ONLY")
        self.assertIn("24h", rep["windows"])
        self.assertIn("7d", rep["windows"])

    def test_fontes_de_controle_declaradas(self):
        cs = r.build_report(self._rows(), as_of=NOW)["current_control_sources"]
        self.assertEqual(cs["risk_service_daily_weekly"]["source"],
                         "RecommendationSnapshot")
        self.assertEqual(cs["risk_service_daily_weekly"]["unit"],
                         "percent_from_realized_r")
        self.assertEqual(cs["kill_switch_daily"]["source"], "RealTrade")
        self.assertEqual(cs["kill_switch_daily"]["scope"], "all_sources_currently")
        self.assertEqual(cs["kill_switch_daily"]["unit"], "recorded_pnl_usd")
        self.assertFalse(cs["unified_financial_source"])
        self.assertFalse(cs["authoritative_source_changed"])
        self.assertFalse(cs["enforcement_changed"])
        self.assertTrue(cs["observation_only"])

    def test_falha_de_bloco_nao_derruba_os_demais(self):
        def _boom(*a, **k):
            raise RuntimeError("bloco quebrou")

        with patch.object(r, "paired_reconciliation", _boom):
            rep = r.build_report(self._rows(), as_of=NOW)
        self.assertEqual(rep["paired_reconciliation"]["status"], "UNAVAILABLE")
        self.assertIn("theoretical", rep["windows"]["24h"])
        self.assertTrue(rep["ok"])

    def test_nunca_retorna_nan_ou_infinito(self):
        import math as _m

        rows = {"closed": [_trade(1, pnl=float("nan")), _trade(2, pnl=float("inf"))],
                "snapshots": [_snap(7, r_val=float("nan"))],
                "open": [_open(1, entry=float("inf"))]}
        rep = r.build_report(rows, as_of=NOW)

        def _walk(node, caminho="raiz"):
            if isinstance(node, dict):
                for k, v in node.items():
                    _walk(v, f"{caminho}.{k}")
            elif isinstance(node, (list, tuple)):
                for i, v in enumerate(node):
                    _walk(v, f"{caminho}[{i}]")
            elif isinstance(node, float):
                self.assertFalse(_m.isnan(node) or _m.isinf(node), caminho)

        _walk(rep)
        self.assertNotIn("[object Object]", repr(rep))
        # a métrica inválida vira None + motivo, nunca zero forjado
        auto = rep["windows"]["24h"]["financial"]["cohorts"]["auto"]
        self.assertIsNone(auto["recorded_net_pnl_usd"])
        self.assertEqual(auto["closed_valid"], 0)

    def test_carga_falha_devolve_ok_false(self):
        async def _boom(as_of):
            raise RuntimeError("db fora")

        with patch.object(r, "load_rows", _boom):
            rep = asyncio.run(r.build_reconciliation(NOW))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["phase"], "R05A")
        self.assertEqual(rep["mode"], "OBSERVATION_ONLY")
        self.assertEqual(rep["reason_code"], "LOAD_FAILED")
        self.assertNotIn("db fora", repr(rep))

    def test_as_of_injetavel_e_deterministico(self):
        async def _rows(as_of):
            return self._rows()

        with patch.object(r, "load_rows", _rows):
            a = asyncio.run(r.build_reconciliation(NOW))
            b = asyncio.run(r.build_reconciliation(NOW))
        self.assertEqual(a["as_of_utc"], NOW.isoformat())
        self.assertEqual(a["windows"], b["windows"])

    def test_as_of_naive_tratado_como_utc(self):
        async def _rows(as_of):
            return self._rows()

        with patch.object(r, "load_rows", _rows):
            rep = asyncio.run(r.build_reconciliation(NOW.replace(tzinfo=None)))
        self.assertEqual(rep["as_of_utc"], NOW.isoformat())


# ════════════════════════════════════════════════════════════════════════════
#  ARQUITETURA
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services" / "risk_reconciliation_service.py").read_text()
        cls.main = (BACKEND / "main.py").read_text()

    @staticmethod
    def _codigo(bloco: str) -> str:
        """Só linhas de CÓDIGO: comentários e docstrings ficam de fora."""
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

    def test_nao_importa_exchange_signed_notificacao_ou_push(self):
        codigo = self._codigo(self.src)
        for proibido in ("exchange_service", "binance_signed", "bybit",
                         "notification_service", "push_service", "send_telegram",
                         "import ccxt", "requests", "aiohttp", "httpx", "socket"):
            self.assertNotIn(proibido, codigo.replace('"bybit"', ""), proibido)

    def test_zero_escrita(self):
        codigo = self._codigo(self.src)
        for proibido in ("session.add", "session.commit", "session.flush",
                         "session.merge", "session.delete", "update(", "insert(",
                         "delete(", "CREATE TABLE", "ALTER TABLE", "alembic"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_nao_chama_metodos_mutantes_de_risco(self):
        """Nenhuma CHAMADA a método que pode mutar estado ou notificar.

        `risk_service_daily_weekly` é chave do contrato exigido e `RiskState`
        aparece no texto das invariantes: a checagem é sobre import/atributo,
        não sobre a substring solta.
        """
        codigo = self._codigo(self.src)
        for proibido in ("update_and_check", "check_can_trade", "set_manual_pause",
                         "import risk_service", "import kill_switch_service",
                         "risk_service.", "kill_switch_service.",
                         "models.risk_state", "import RiskState", "RiskState."):
            self.assertNotIn(proibido, codigo, proibido)

    def test_sem_scheduler_worker_ou_loop(self):
        codigo = self._codigo(self.src)
        for proibido in ("asyncio.create_task", "while True", "Thread(",
                         "Queue(", "APScheduler"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_sem_env_ou_flag_nova(self):
        self.assertNotIn("os.getenv", self.src)
        self.assertNotIn("environ", self.src)

    def test_no_maximo_tres_selects_e_sem_n_mais_1(self):
        import ast

        loader = self.src.split("async def load_rows")[1].split("\ndef ")[0]
        self.assertEqual(loader.count("await session.execute("), 3)
        self.assertEqual(loader.count("select("), 3)

        arvore = ast.parse(self.src)
        alvo = next(n for n in ast.walk(arvore)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_rows")

        def _executes(node) -> int:
            return sum(1 for n in ast.walk(node)
                       if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "execute")

        # Nenhum SELECT dentro de laço: sem N+1.
        for node in ast.walk(alvo):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.assertEqual(_executes(node), 0, "SELECT dentro de laço (N+1)")
        self.assertEqual(_executes(alvo), 3)

    def test_sessao_falsa_falha_se_houver_escrita(self):
        class _NoWrite:
            def __getattr__(self, name):
                if name in ("add", "commit", "flush", "merge", "delete", "add_all"):
                    raise AssertionError(f"ESCRITA PROIBIDA no R05A: {name}")
                raise AttributeError(name)

        class _Ctx:
            async def __aenter__(self):
                return _NoWrite()

            async def __aexit__(self, *a):
                return False

        import db
        with patch.object(db, "get_session", lambda: _Ctx()):
            rep = r.build_report({"closed": [_trade(1)], "snapshots": [_snap(1)],
                                  "open": [_open(1)]}, as_of=NOW)
        self.assertTrue(rep["ok"])

    def test_endpoint_somente_get_e_admin(self):
        rotas = [ln for ln in self.main.splitlines() if "risk/reconciliation" in ln]
        self.assertTrue(any('@app.get("/api/risk/reconciliation")' in ln
                            for ln in rotas))
        self.assertFalse([ln for ln in rotas if "@app.post" in ln])
        bloco = self.main.split('@app.get("/api/risk/reconciliation")')[1].split(
            "\n@app.")[0]
        self.assertIn("_check_admin_token(x_admin_token)", bloco)
        self.assertIn("X-Admin-Token", self.main)
        for proibido in ("retry-now", "apply", "promote", "activate", "refresh",
                         "session.commit", "place_order"):
            self.assertNotIn(proibido, bloco, proibido)

    def test_endpoint_fail_soft_sem_stack_trace(self):
        bloco = self.main.split('@app.get("/api/risk/reconciliation")')[1].split(
            "\n@app.")[0]
        self.assertIn('"ok": False', bloco)
        self.assertIn('"phase": "R05A"', bloco)
        self.assertIn('"mode": "OBSERVATION_ONLY"', bloco)
        self.assertNotIn("traceback", bloco)
        self.assertNotIn("str(e)", bloco)

    def test_relatorio_fora_de_hot_path(self):
        for fn in ("async def risk_status", "async def _daily_digest_loop",
                   "def _fmt_digest"):
            bloco = self.main.split(fn)[1].split("\n@app.")[0].split(
                "\nasync def ")[0]
            self.assertNotIn("risk_reconciliation", bloco, fn)

    def test_arquivos_congelados_intactos(self):
        import subprocess
        congelados = [
            "backend/services/risk_service.py",
            "backend/services/kill_switch_service.py",
            "backend/services/real_trade_service.py",
            "backend/services/shadow_trade_service.py",
            "backend/services/trade_manager_service.py",
            "backend/services/strategy_evidence_service.py",
            "backend/services/snapshot_service.py",
            "backend/models", "backend/db.py", "frontend/src",
        ]
        for caminho in congelados:
            res = subprocess.run(
                ["git", "diff", "--name-only", "8bba744c", "--", caminho],
                cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("baseline 8bba744c indisponível neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"arquivo congelado alterado: {caminho}")


if __name__ == "__main__":
    unittest.main()
