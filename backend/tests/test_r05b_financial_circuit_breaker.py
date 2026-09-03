"""R05B — consolidação financeira dos circuit breakers.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, Railway,
banco externo, Telegram ou ordem real. Cutover default = FALSE.
"""
from __future__ import annotations

import asyncio
import os
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
    raise RuntimeError(f"REDE BLOQUEADA no teste R05B (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import financial_risk_service as f            # noqa: E402
from services import risk_reconciliation_service as r05a    # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
EQ_OK = {"quality": "OK", "reason_code": None, "detail": None,
         "total_usd": 1000.0, "source": "live", "age_sec": 3.0}


def _cutover(on: bool):
    return patch.dict(os.environ, {f.CUTOVER_ENV: "true" if on else "false"})


def _closed(i=1, *, source="auto", status="closed_stop", pnl=-20.0, hours=2,
            tp1=None, entry_fee=0.5, exit_fee=0.5, closed_at="auto", rec=1):
    ts = NOW - timedelta(hours=hours) if closed_at == "auto" else closed_at
    return {"id": i, "source": source, "status": status, "side": "long",
            "pnl_usd": pnl, "tp1_realized_usd": tp1, "entry_fee": entry_fee,
            "exit_fee": exit_fee, "entry_slippage_pct": 0.01,
            "recommendation_id": rec, "closed_at": ts}


def _openpos(i=1, *, source="auto", side="long", entry=100.0, qty=2.0,
             stop=95.0, sl_id="sl-1", sl_price=97.0, fee=0.2):
    return {"id": i, "source": source, "side": side, "entry_price": entry,
            "qty": qty, "planned_stop": stop, "sl_order_id": sl_id,
            "sl_current_price": sl_price, "entry_fee": fee}


def _codigo(bloco: str) -> str:
    """Só as linhas de CÓDIGO: comentários e docstrings ficam de fora."""
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


def _snap(closed=None, opens=None, equity=EQ_OK, as_of=NOW):
    return f.build_snapshot({"closed": closed if closed is not None else [],
                             "open": opens if opens is not None else []},
                            equity, as_of=as_of)


# ════════════════════════════════════════════════════════════════════════════
#  FONTE
# ════════════════════════════════════════════════════════════════════════════
class Fonte(unittest.TestCase):

    def test_somente_source_auto(self):
        rows = [_closed(1, source="auto", pnl=-10.0),
                _closed(2, source="managed", pnl=-100.0),
                _closed(3, source="manual", pnl=-100.0),
                _closed(4, source="bybit", pnl=-100.0),
                _closed(5, source="outra", pnl=-100.0)]
        w = f.financial_window(rows, since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertEqual(w["pnl_usd"], -10.0)
        self.assertEqual(w["valid_count"], 1)

    def test_closed_manual_de_auto_continua_incluido(self):
        w = f.financial_window([_closed(1, status="closed_manual", pnl=-7.0)],
                               since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertEqual(w["pnl_usd"], -7.0)
        self.assertEqual(w["valid_count"], 1)

    def test_janela_por_closed_at_nunca_opened_at(self):
        w = f.financial_window([_closed(1, hours=48)],
                               since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertEqual(w["valid_count"], 0)
        self.assertEqual(w["pnl_usd"], 0.0)     # nenhuma linha vista na janela

    def test_zero_financeiro_legitimo_continua_zero(self):
        w = f.financial_window([_closed(1, pnl=0.0)],
                               since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertEqual(w["pnl_usd"], 0.0)
        self.assertEqual(w["valid_count"], 1)
        self.assertEqual(w["quality"], "OK")

    def test_none_nan_inf_excluidos_e_qualidade_unknown(self):
        for ruim in (None, float("nan"), float("inf"), float("-inf")):
            w = f.financial_window([_closed(1, pnl=ruim)],
                                   since=NOW - timedelta(hours=24), until=NOW,
                                   kind=f.WINDOW_ROLLING, equity=EQ_OK)
            self.assertEqual(w["quality"], "UNKNOWN", repr(ruim))
            self.assertIsNone(w["pnl_usd"])
            self.assertEqual(w["excluded_count"], 1)
            self.assertFalse(w["data_complete"])

    def test_sem_fallback_para_snapshot_realized_r_ou_status(self):
        codigo = _codigo((BACKEND / "services" / "financial_risk_service.py").read_text())
        for proibido in ("RecommendationSnapshot", "realized_r",
                         '"closed_stop"', "'closed_stop'"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_contrato_da_janela(self):
        w = f.financial_window([_closed(1)], since=NOW - timedelta(hours=24),
                               until=NOW, kind=f.WINDOW_ROLLING, equity=EQ_OK)
        for chave in ("since_utc", "until_utc", "window_kind", "financial_source",
                      "valid_count", "excluded_count", "excluded_by_reason",
                      "data_complete"):
            self.assertIn(chave, w, chave)
        self.assertEqual(w["window_kind"], "rolling")
        self.assertEqual(w["pnl_label"], "RECORDED_NET_EX_FUNDING")


# ════════════════════════════════════════════════════════════════════════════
#  P&L — taxas, TP1, funding
# ════════════════════════════════════════════════════════════════════════════
class Pnl(unittest.TestCase):

    def _pnl(self, rows):
        return f.financial_window(rows, since=NOW - timedelta(hours=24),
                                  until=NOW, kind=f.WINDOW_ROLLING,
                                  equity=EQ_OK)["pnl_usd"]

    def test_tp1_mais_be(self):
        self.assertEqual(self._pnl([_closed(1, status="closed_be", pnl=2.4,
                                            tp1=2.55)]), 2.4)

    def test_tp1_mais_stop_estrutural(self):
        self.assertEqual(self._pnl([_closed(1, status="closed_stop", pnl=-0.17,
                                            tp1=2.55)]), -0.17)

    def test_tp2(self):
        self.assertEqual(self._pnl([_closed(1, status="closed_tp2", pnl=18.0)]), 18.0)

    def test_fechamento_parcial(self):
        self.assertEqual(self._pnl([_closed(1, status="closed_tp1", pnl=5.0,
                                            tp1=5.0)]), 5.0)

    def test_fechamento_manual_do_auto(self):
        self.assertEqual(self._pnl([_closed(1, status="closed_manual", pnl=-3.5)]), -3.5)

    def test_taxas_sem_double_count(self):
        self.assertEqual(self._pnl([_closed(1, pnl=-20.0, entry_fee=3.0,
                                            exit_fee=4.0)]), -20.0)

    def test_tp1_sem_double_count(self):
        sem = self._pnl([_closed(1, pnl=10.0, tp1=None)])
        com = self._pnl([_closed(1, pnl=10.0, tp1=6.0)])
        self.assertEqual(sem, com)

    def test_funding_null_e_unavailable(self):
        w = f.financial_window([_closed(1)], since=NOW - timedelta(hours=24),
                               until=NOW, kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertIsNone(w["funding"]["value"])
        self.assertEqual(w["funding"]["reason_code"], "FUNDING_FIELD_UNAVAILABLE")
        snap = _snap([_closed(1)])
        self.assertIsNone(snap["funding"]["value"])
        self.assertEqual(snap["pnl_label"], "RECORDED_NET_EX_FUNDING")

    def test_funding_nunca_estimado_nem_zero(self):
        src = (BACKEND / "services" / "financial_risk_service.py").read_text()
        self.assertNotIn("income_history", src)
        self.assertNotIn("funding_usd", src)
        self.assertIn("FUNDING_FIELD_UNAVAILABLE", src)


# ════════════════════════════════════════════════════════════════════════════
#  EQUITY
# ════════════════════════════════════════════════════════════════════════════
class Equity(unittest.TestCase):

    def test_live_e_cache_fresco_sao_validos(self):
        for src in ("live", "cache"):
            out = f.validate_equity({"ok": True, "total_usd": 500.0,
                                     "source": src, "age_sec": 10.0})
            self.assertEqual(out["quality"], "OK", src)
            self.assertEqual(out["total_usd"], 500.0)

    def test_fallback_bloqueado(self):
        out = f.validate_equity({"ok": True, "total_usd": 500.0,
                                 "source": "fallback", "age_sec": 1.0})
        self.assertEqual(out["quality"], "UNKNOWN")
        self.assertEqual(out["reason_code"], "EQUITY_FALLBACK")
        self.assertIsNone(out["total_usd"])

    def test_fonte_ou_idade_nao_confirmada_bloqueiam(self):
        casos = [
            {"ok": True, "total_usd": 500.0, "source": "", "age_sec": 1.0},
            {"ok": True, "total_usd": 500.0, "source": "inventada", "age_sec": 1.0},
            {"ok": True, "total_usd": 500.0, "source": "live", "age_sec": None},
            {"ok": True, "total_usd": 500.0, "source": "cache", "age_sec": -1.0},
        ]
        for payload in casos:
            out = f.validate_equity(payload)
            self.assertEqual(out["quality"], "UNKNOWN", repr(payload))
            self.assertIsNone(out["total_usd"])

    def test_stale_bloqueado(self):
        out = f.validate_equity({"ok": True, "total_usd": 500.0,
                                 "source": "cache", "age_sec": 99999.0})
        self.assertEqual(out["reason_code"], "EQUITY_STALE")
        self.assertIsNone(out["total_usd"])

    def test_zero_negativo_nan_inf_bloqueados(self):
        for ruim in (0.0, -10.0, float("nan"), float("inf"), None, "500"):
            out = f.validate_equity({"ok": True, "total_usd": ruim,
                                     "source": "live", "age_sec": 1.0})
            self.assertEqual(out["quality"], "UNKNOWN", repr(ruim))
            self.assertIsNone(out["total_usd"])

    def test_not_ok_e_payload_invalido_bloqueados(self):
        self.assertEqual(f.validate_equity({"ok": False})["reason_code"],
                         "EQUITY_NOT_OK")
        for ruim in (None, "x", [], 42):
            self.assertEqual(f.validate_equity(ruim)["reason_code"],
                             "EQUITY_INVALID_PAYLOAD")

    def test_excecao_bloqueia(self):
        class _Boom:
            async def get_equity(self):
                raise RuntimeError("exchange fora")

        with patch.dict("sys.modules", {"services.exchange_service": _Boom()}):
            out = asyncio.run(f.fetch_equity())
        self.assertEqual(out["quality"], "UNKNOWN")
        self.assertIsNone(out["total_usd"])

    def test_dd_percentual_numericamente_correto(self):
        w = f.financial_window([_closed(1, pnl=-30.0)],
                               since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=EQ_OK)
        self.assertEqual(w["dd_pct"], -3.0)      # -30 / 1000 × 100

    def test_equity_invalida_deixa_dd_none(self):
        bad = f.validate_equity({"ok": True, "total_usd": 0.0, "source": "live"})
        w = f.financial_window([_closed(1, pnl=-30.0)],
                               since=NOW - timedelta(hours=24), until=NOW,
                               kind=f.WINDOW_ROLLING, equity=bad)
        self.assertIsNone(w["dd_pct"])
        self.assertEqual(w["dd_reason_code"], "EQUITY_NOT_POSITIVE")
        self.assertEqual(w["pnl_usd"], -30.0)    # P&L continua conhecido


# ════════════════════════════════════════════════════════════════════════════
#  LOSS STREAK
# ════════════════════════════════════════════════════════════════════════════
class LossStreak(unittest.TestCase):

    def _streak(self, rows):
        return f.loss_streak(rows, since=NOW - timedelta(hours=24), until=NOW)

    def test_consecutivos_pelo_pnl_liquido(self):
        out = self._streak([_closed(1, pnl=-5.0, hours=1),
                            _closed(2, pnl=-3.0, hours=2),
                            _closed(3, pnl=4.0, hours=3),
                            _closed(4, pnl=-9.0, hours=4)])
        self.assertEqual(out["streak"], 2)
        self.assertEqual(out["quality"], "OK")

    def test_pnl_zero_quebra_o_streak(self):
        out = self._streak([_closed(1, pnl=-5.0, hours=1),
                            _closed(2, pnl=0.0, hours=2),
                            _closed(3, pnl=-5.0, hours=3)])
        self.assertEqual(out["streak"], 1)

    def test_tp1_positivo_com_pnl_final_negativo_continua_loss(self):
        out = self._streak([_closed(1, pnl=-0.17, tp1=2.55, hours=1)])
        self.assertEqual(out["streak"], 1)

    def test_somente_auto(self):
        out = self._streak([_closed(1, source="manual", pnl=-9.0, hours=1),
                            _closed(2, source="auto", pnl=5.0, hours=2)])
        self.assertEqual(out["streak"], 0)

    def test_pnl_ausente_e_fail_closed(self):
        out = self._streak([_closed(1, pnl=None, hours=1)])
        self.assertEqual(out["quality"], "UNKNOWN")
        self.assertIsNone(out["streak"])
        self.assertEqual(out["reason_code"], "STREAK_ROWS_INVALID")

    def test_sem_fallback_teorico(self):
        src = (BACKEND / "services" / "financial_risk_service.py").read_text()
        bloco = _codigo(src.split("def loss_streak")[1].split("\ndef ")[0])
        for proibido in ("realized_r", "closed_stop", "tp1_realized_usd"):
            self.assertNotIn(proibido, bloco, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  RISCO ABERTO
# ════════════════════════════════════════════════════════════════════════════
class RiscoAberto(unittest.TestCase):

    def test_long_e_short(self):
        out = f.open_exposure([
            _openpos(1, side="long", entry=100.0, sl_price=97.0, qty=2.0),
            _openpos(2, side="short", entry=100.0, sl_price=104.0, qty=1.0)])
        self.assertEqual(out["open_price_risk_usd"], 10.0)
        self.assertTrue(out["open_risk_complete"])

    def test_stop_em_lucro_gera_risco_zero(self):
        out = f.open_exposure([_openpos(1, side="long", entry=100.0, sl_price=105.0)])
        self.assertEqual(out["open_price_risk_usd"], 0.0)
        self.assertTrue(out["open_risk_complete"])

    def test_entry_fee_aberta_entra_no_cenario(self):
        out = f.open_exposure([_openpos(1, fee=0.7)])
        self.assertEqual(out["open_entry_fees_usd"], 0.7)
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1, entry=100.0,
                                                        sl_price=97.0, qty=2.0,
                                                        fee=0.7)])
        worst = f.worst_case_daily_usd(snap, 5.0)
        self.assertEqual(worst["value"], -10.0 - 6.0 - 0.7 - 5.0)

    def test_stop_ausente_ou_malformado_e_unknown(self):
        for pos in (_openpos(1, sl_id=None), _openpos(2, sl_id="  "),
                    _openpos(3, sl_id="sl", sl_price=None, stop=None)):
            out = f.open_exposure([pos])
            self.assertFalse(out["open_risk_complete"])
            self.assertIsNone(out["open_price_risk_usd"])
            self.assertEqual(out["reason_code"], "OPEN_RISK_INCOMPLETE")

    def test_planned_stop_fallback_contabilizado(self):
        out = f.open_exposure([_openpos(1, sl_price=None, stop=90.0, qty=1.0,
                                        entry=100.0)])
        self.assertEqual(out["open_price_risk_usd"], 10.0)
        self.assertEqual(out["planned_stop_fallback"], 1)

    def test_zero_posicoes_e_risco_conhecido_zero(self):
        out = f.open_exposure([])
        self.assertTrue(out["open_risk_complete"])
        self.assertEqual(out["open_price_risk_usd"], 0.0)
        self.assertEqual(out["quality"], "OK")

    def test_reutiliza_contrato_do_r05a(self):
        src = (BACKEND / "services" / "financial_risk_service.py").read_text()
        self.assertIn("from services.risk_reconciliation_service import", src)
        self.assertIn("open_risk", src)
        self.assertIn("real_cohorts", src)


# ════════════════════════════════════════════════════════════════════════════
#  PIOR CENÁRIO
# ════════════════════════════════════════════════════════════════════════════
class PiorCenario(unittest.TestCase):

    def test_market_usa_preco_adverso_por_lado(self):
        prices = [100.0, 101.0, 102.0]
        self.assertEqual(f.adverse_market_entry_price("long", prices)["value"], 102.0)
        self.assertEqual(f.adverse_market_entry_price("short", prices)["value"], 100.0)
        for side, values in (("x", prices), ("long", [100.0, None, 102.0]),
                             ("short", [100.0, float("nan"), 102.0])):
            out = f.adverse_market_entry_price(side, values)
            self.assertEqual(out["quality"], "UNKNOWN")
            self.assertIsNone(out["value"])

    def test_risco_proposto_long_e_short(self):
        self.assertEqual(f.proposed_trade_risk_usd("long", 100.0, 98.0, 5.0)["value"],
                         10.0)
        self.assertEqual(f.proposed_trade_risk_usd("short", 100.0, 102.0, 5.0)["value"],
                         10.0)

    def test_risco_proposto_rejeita_invalidos(self):
        casos = [("x", 100.0, 98.0, 1.0), ("long", None, 98.0, 1.0),
                 ("long", 100.0, None, 1.0), ("long", 100.0, 98.0, None),
                 ("long", float("nan"), 98.0, 1.0),
                 ("long", 100.0, 98.0, float("inf")),
                 ("long", 100.0, 105.0, 1.0),      # stop incompatível com LONG
                 ("short", 100.0, 95.0, 1.0),      # stop incompatível com SHORT
                 ("long", 100.0, 98.0, -1.0)]
        for caso in casos:
            out = f.proposed_trade_risk_usd(*caso)
            self.assertEqual(out["quality"], "UNKNOWN", repr(caso))
            self.assertIsNone(out["value"])

    def test_abaixo_do_limite_permite(self):
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1, entry=100.0,
                                                        sl_price=97.0, qty=2.0,
                                                        fee=0.0)])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "OK", "value": 100.0}):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=5.0))
        self.assertTrue(out["ok"])
        self.assertEqual(out["worst_case_daily_usd"], -26.0)

    def test_gate_forca_snapshot_fresco_e_reusa_o_mesmo_equity(self):
        snap = _snap([_closed(1, pnl=-10.0)], [])
        with _cutover(True), \
                patch.object(f, "financial_snapshot", return_value=snap) as get_snap, \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "OK", "value": 100.0}) as get_limit:
            out = asyncio.run(f.check_new_entry(
                side="long", final_entry=100.0, stop=98.0, final_qty=1.0))
        self.assertTrue(out["ok"])
        get_snap.assert_awaited_once_with(force=True)
        get_limit.assert_awaited_once_with(snap)

    def test_limite_percentual_usa_equity_do_snapshot(self):
        snap = _snap([], [], equity={**EQ_OK, "total_usd": 2000.0})
        out = f.daily_loss_limit_from_snapshot(
            {"max_daily_loss_pct": 3.0, "max_daily_loss_usd": 999.0}, snap)
        self.assertEqual(out["quality"], "OK")
        self.assertEqual(out["value"], 60.0)
        bad = f.daily_loss_limit_from_snapshot(
            {"max_daily_loss_pct": 3.0, "max_daily_loss_usd": 999.0},
            {"equity": {"quality": "UNKNOWN", "total_usd": None}})
        self.assertEqual(bad["quality"], "UNKNOWN")
        self.assertIsNone(bad["value"])

    def test_reset_futuro_nao_apaga_o_dia(self):
        import sys
        from types import SimpleNamespace
        future = NOW + timedelta(hours=1)
        day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        fake = SimpleNamespace(_daily_reset_at=lambda: future)
        with patch.dict(sys.modules, {"services.kill_switch_service": fake}):
            self.assertEqual(f.kill_daily_start(NOW), day_start)

    def test_exatamente_no_limite_bloqueia(self):
        snap = _snap([_closed(1, pnl=-10.0)], [])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "OK", "value": 20.0}):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=5.0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "FINANCIAL_WORST_CASE_LIMIT")
        self.assertEqual(out["worst_case_daily_usd"], -20.0)

    def test_acima_do_limite_bloqueia(self):
        snap = _snap([_closed(1, pnl=-30.0)], [])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "OK", "value": 20.0}):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=5.0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "FINANCIAL_WORST_CASE_LIMIT")

    def test_qty_reduzida_do_market_usa_qty_reduzida(self):
        snap = _snap([_closed(1, pnl=0.0)], [])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "OK", "value": 15.0}):
            cheia = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                  stop=98.0, final_qty=10.0))
            reduzida = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                     stop=98.0, final_qty=5.0))
        self.assertFalse(cheia["ok"])            # -20 <= -15
        self.assertTrue(reduzida["ok"])          # -10 > -15

    def test_divergencias_opostas_nao_se_cancelam(self):
        # risco aberto e risco proposto SOMAM ao prejuízo, nunca se compensam
        snap = _snap([_closed(1, pnl=50.0)],
                     [_openpos(1, entry=100.0, sl_price=90.0, qty=3.0, fee=0.0)])
        worst = f.worst_case_daily_usd(snap, 10.0)
        self.assertEqual(worst["value"], 50.0 - 30.0 - 0.0 - 10.0)

    def test_parcela_desconhecida_torna_cenario_none(self):
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1, sl_id=None)])
        worst = f.worst_case_daily_usd(snap, 5.0)
        self.assertIsNone(worst["value"])
        self.assertEqual(worst["reason_code"], "OPEN_RISK_INCOMPLETE")


# ════════════════════════════════════════════════════════════════════════════
#  SNAPSHOT / FAIL-CLOSED
# ════════════════════════════════════════════════════════════════════════════
class SnapshotFailClosed(unittest.TestCase):

    def test_flag_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(f.CUTOVER_ENV, None)
            self.assertFalse(f.cutover_enabled())
        for valor in ("false", "0", "no", "", "talvez", "TRUE_ISH"):
            with patch.dict(os.environ, {f.CUTOVER_ENV: valor}):
                self.assertFalse(f.cutover_enabled(), valor)
        for valor in ("true", "1", "yes", "on", "TRUE"):
            with patch.dict(os.environ, {f.CUTOVER_ENV: valor}):
                self.assertTrue(f.cutover_enabled())

    def test_snapshot_ok(self):
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1)])
        self.assertEqual(snap["quality"], "OK")
        self.assertEqual(snap["blockers"], [])
        self.assertEqual(snap["rolling_24h"]["pnl_usd"], -10.0)

    def test_equity_invalida_marca_snapshot_unknown(self):
        bad = f.validate_equity({"ok": False})
        snap = _snap([_closed(1, pnl=-10.0)], [], equity=bad)
        self.assertEqual(snap["quality"], "UNKNOWN")
        self.assertIn("equity", snap["blockers"])

    def test_risco_aberto_incompleto_marca_unknown(self):
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1, sl_id=None)])
        self.assertEqual(snap["quality"], "UNKNOWN")
        self.assertIn("open_exposure", snap["blockers"])

    def test_falha_de_banco_nao_vira_zero(self):
        async def _boom(as_of):
            raise RuntimeError("db fora")

        with _cutover(True), patch.object(f, "load_rows", _boom):
            snap = asyncio.run(f.financial_snapshot(NOW))
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["quality"], "UNKNOWN")
        self.assertEqual(snap["reason_code"], "FINANCIAL_DB_UNAVAILABLE")
        self.assertIsNone(snap["rolling_24h"]["pnl_usd"])
        self.assertIsNone(snap["open_exposure"]["open_price_risk_usd"])
        self.assertFalse(snap["open_exposure"]["open_risk_complete"])

    def test_gate_no_op_com_cutover_desligado(self):
        with _cutover(False):
            out = asyncio.run(f.check_new_entry(side="lixo", final_entry=None,
                                                stop=None, final_qty=None))
        self.assertTrue(out["ok"])
        self.assertFalse(out["cutover_enabled"])

    def test_gate_bloqueia_com_qualidade_unknown(self):
        snap = _snap([_closed(1, pnl=None)], [])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=1.0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["quality"], "UNKNOWN")

    def test_gate_bloqueia_em_excecao(self):
        def _boom(*a, **k):
            raise RuntimeError("erro interno")

        with _cutover(True), patch.object(f, "financial_snapshot", _boom):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=1.0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "FINANCIAL_CHECK_ERROR")

    def test_gate_bloqueia_com_limite_indisponivel(self):
        snap = _snap([_closed(1, pnl=-1.0)], [])
        with _cutover(True), patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(f, "daily_loss_limit_usd",
                             return_value={"quality": "UNKNOWN",
                                           "reason_code": "EQUITY_STALE",
                                           "value": None}):
            out = asyncio.run(f.check_new_entry(side="long", final_entry=100.0,
                                                stop=98.0, final_qty=1.0))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "EQUITY_STALE")

    def test_nunca_retorna_nan_ou_infinito(self):
        import math as _m

        snap = _snap([_closed(1, pnl=float("nan")), _closed(2, pnl=-5.0)],
                     [_openpos(1, entry=float("inf"))])

        def _walk(node, caminho="raiz"):
            if isinstance(node, dict):
                for k, v in node.items():
                    _walk(v, f"{caminho}.{k}")
            elif isinstance(node, (list, tuple)):
                for i, v in enumerate(node):
                    _walk(v, f"{caminho}[{i}]")
            elif isinstance(node, float):
                self.assertFalse(_m.isnan(node) or _m.isinf(node), caminho)

        _walk(snap)


# ════════════════════════════════════════════════════════════════════════════
#  RISK SERVICE / KILL SWITCH — integração
# ════════════════════════════════════════════════════════════════════════════
class Integracao(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.risk = (BACKEND / "services" / "risk_service.py").read_text()
        cls.kill = (BACKEND / "services" / "kill_switch_service.py").read_text()
        cls.shadow = (BACKEND / "services" / "shadow_trade_service.py").read_text()

    def test_risk_service_ramifica_pela_flag(self):
        self.assertIn("_frs.cutover_enabled()", self.risk)
        self.assertIn("_compute_window_dd(session, hours=24)", self.risk)   # legado
        self.assertIn('_fin.get("rolling_24h")', self.risk)

    def test_unknown_nao_grava_zero_e_preserva_last_confirmed(self):
        bloco = self.risk.split("# Recalcula DD")[1].split("# ── Auto-resume")[0]
        self.assertIn("state.daily_dd_pct or 0.0", bloco)
        self.assertIn("last_confirmed", self.risk)

    def test_unknown_nao_auto_resume_e_nao_cria_pausa(self):
        self.assertIn("_no_incident and not _financial_unknown", self.risk)
        self.assertEqual(self.risk.count("_no_incident and not _financial_unknown"), 2)
        self.assertIn("if not state.trading_paused and not _financial_unknown:",
                      self.risk)

    def test_p03_manual_e_ownership_preservados(self):
        for contrato in ("pg_advisory_xact_lock", "_is_p03_pause",
                         "ensure_p03_pause_in_session", "state.pause_manual",
                         "RELEASE_SAFE_OTHER_OWNER", "RiskEvent"):
            self.assertIn(contrato, self.risk, contrato)

    def test_r05b_nao_cria_state_machine_nem_tabela(self):
        src = (BACKEND / "services" / "financial_risk_service.py").read_text()
        for proibido in ("class RiskState", "CREATE TABLE", "ALTER TABLE",
                         "alembic", "session.add", "session.commit",
                         "session.flush", "session.merge", "session.delete"):
            self.assertNotIn(proibido, src, proibido)

    def test_pausa_identifica_a_fonte_financeira(self):
        self.assertIn("P&L financeiro registrado", self.risk)

    def test_kill_switch_usa_o_mesmo_nucleo(self):
        self.assertIn("financial_risk_service", self.kill)
        self.assertIn('_fin.get("kill_daily")', self.kill)
        self.assertIn('_fin.get("loss_streak")', self.kill)
        # legado preservado para quando a flag está desligada
        self.assertIn("await _daily_pnl_usd()", self.kill)
        self.assertIn("await _recent_losses_streak(", self.kill)

    def test_kill_switch_bloqueia_com_qualidade_unknown(self):
        bloco = self.kill.split("R05B: núcleo financeiro")[1].split("# 1. Manual")[0]
        self.assertIn('if _fin.get("quality") != "OK":', bloco)
        self.assertIn("blocked_reasons.append", bloco)

    def test_kill_switch_usa_limite_do_mesmo_snapshot_sem_segunda_equity(self):
        import importlib
        kill = importlib.import_module("services.kill_switch_service")
        snap = _snap([_closed(1, pnl=-1.0)], [])
        th = {"kill_switch": False, "max_open_positions": 5,
              "max_daily_loss_usd": 999.0, "max_daily_loss_pct": 3.0,
              "max_consec_losses": 3, "cooldown_hours": 12,
              "max_daily_trades": 20}
        with _cutover(True), patch.object(kill, "thresholds", return_value=th), \
                patch.object(kill, "_count_open", return_value=0), \
                patch.object(kill, "_daily_opens", return_value=0), \
                patch.object(f, "financial_snapshot", return_value=snap), \
                patch.object(kill, "_resolve_daily_loss_limit_usd",
                             side_effect=AssertionError("legado não pode rodar")) as legacy:
            out = asyncio.run(kill.check_can_trade())
        self.assertTrue(out["allowed"])
        self.assertEqual(out["checks"]["daily_loss_limit_usd"], 30.0)
        legacy.assert_not_awaited()

    def test_kill_switch_payload_financeiro_malformado_bloqueia_sem_excecao(self):
        import importlib
        kill = importlib.import_module("services.kill_switch_service")
        th = {"kill_switch": False, "max_open_positions": 5,
              "max_daily_loss_usd": 100.0, "max_daily_loss_pct": 0.0,
              "max_consec_losses": 3, "cooldown_hours": 12,
              "max_daily_trades": 20}
        with _cutover(True), patch.object(kill, "thresholds", return_value=th), \
                patch.object(kill, "_count_open", return_value=0), \
                patch.object(kill, "_daily_opens", return_value=0), \
                patch.object(f, "financial_snapshot", return_value=["inválido"]):
            out = asyncio.run(kill.check_can_trade())
        self.assertFalse(out["allowed"])
        self.assertIn("FINANCIAL_PAYLOAD_INVALID", out["reason"])

    def test_caps_nao_financeiros_preservados(self):
        for contrato in ("max_open_positions", "max_daily_trades",
                         "cooldown_hours", "kill_switch_manual",
                         "fmt_kill_switch", "_KILL_NOTIFIED_DAY"):
            self.assertIn(contrato, self.kill, contrato)

    def test_preflight_chama_o_gate_antes_do_post(self):
        maker = self.shadow.split("async def _entry_preflight")[1].split(
            "_exec_mark(_exec_trace")[0]
        self.assertIn("_r05b_entry_gate(", maker)
        self.assertIn("final_entry=final_limit_price", maker)
        market = self.shadow.split("async def _market_entry_preflight")[1].split(
            "select_entry_route")[0]
        self.assertIn("_r05b_entry_gate(", market)
        self.assertIn("adverse_market_entry_price", market)
        self.assertIn('final_entry=_risk_entry["value"]', market)
        # o gate vem ANTES do approved_qty/return de cada preflight
        for bloco in (maker, market):
            self.assertLess(bloco.index("_r05b_entry_gate("),
                            bloco.rindex('verdict["approved_qty"]'))

    def test_gate_do_preflight_e_fail_closed(self):
        helper = self.shadow.split("async def _r05b_entry_gate")[1].split(
            "\ndef _exec_mark")[0]
        self.assertIn("except Exception", helper)
        self.assertIn('"ok": False', helper)
        for proibido in ("place_order", "await _maker_fn", "exchange_service."):
            self.assertNotIn(proibido, helper, proibido)

    def test_maker_e_fallback_continuam_desligados(self):
        self.assertIn('MAKER_ENTRY_ENABLED', self.shadow)
        self.assertIn('os.getenv("MAKER_FALLBACK_MARKET", "false")', self.shadow)


# ════════════════════════════════════════════════════════════════════════════
#  API / ARQUITETURA
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (BACKEND / "services" / "financial_risk_service.py").read_text()
        cls.main = (BACKEND / "main.py").read_text()

    def test_uma_unica_env_nova(self):
        import re
        envs = set(re.findall(r'os\.getenv\(\s*["\']([A-Z0-9_]+)', self.src))
        envs |= {m for m in re.findall(r'CUTOVER_ENV = "([A-Z0-9_]+)"', self.src)}
        self.assertEqual(envs - {"R05_FINANCIAL_BREAKER_ENABLED"}, set())
        self.assertEqual(f.CUTOVER_ENV, "R05_FINANCIAL_BREAKER_ENABLED")

    def test_reutiliza_thresholds_existentes(self):
        self.assertIn("max_daily_loss_pct", self.src)
        self.assertIn("max_daily_loss_usd", self.src)
        for proibido in ("R05_DAILY_LIMIT", "R05_WEEKLY_LIMIT", "R05_MAX_"):
            self.assertNotIn(proibido, self.src, proibido)

    def test_no_maximo_dois_selects_sem_n_mais_1(self):
        import ast

        loader = self.src.split("async def load_rows")[1].split("\ndef ")[0]
        self.assertEqual(loader.count("await session.execute("), 2)
        arvore = ast.parse(self.src)
        alvo = next(n for n in ast.walk(arvore)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_rows")
        for node in ast.walk(alvo):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.assertEqual(
                    sum(1 for n in ast.walk(node)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "execute"), 0, "N+1")

    def test_uma_unica_chamada_de_equity(self):
        codigo = _codigo(self.src)
        self.assertEqual(codigo.count("exchange_service.get_equity()"), 1)
        self.assertNotIn("get_equity(force=True)", codigo)

    def test_sem_sdk_provider_ou_ordem(self):
        for proibido in ("import ccxt", "place_order", "cancel_order",
                         "binance_signed", "notification_service", "send_telegram",
                         "requests.", "aiohttp", "httpx"):
            self.assertNotIn(proibido, self.src, proibido)

    def test_nenhuma_rota_nova(self):
        rotas = [ln for ln in self.main.splitlines() if ln.lstrip().startswith("@app.")]
        for proibido in ("financial", "r05b", "cutover"):
            self.assertFalse([r for r in rotas if proibido in r.lower()], proibido)

    def test_endpoints_existentes_expandidos(self):
        self.assertIn('@app.get("/api/risk/status")', self.main)
        self.assertIn('@app.get("/api/kill-switch/status")', self.main)
        risk_src = (BACKEND / "services" / "risk_service.py").read_text()
        kill_src = (BACKEND / "services" / "kill_switch_service.py").read_text()
        for campo in ("metric_source", "cutover_enabled", "financial_quality",
                      "financial_reason_code", "financial_as_of_utc",
                      "last_confirmed"):
            self.assertIn(campo, risk_src, campo)
        self.assertIn("status_block", kill_src)

    def test_status_block_backward_compatible(self):
        snap = _snap([_closed(1, pnl=-10.0)], [_openpos(1)])
        block = f.status_block(snap, last_confirmed={"daily_dd_pct": -1.0})
        for campo in ("metric_source", "cutover_enabled", "financial_quality",
                      "financial_reason_code", "financial_as_of_utc",
                      "daily_pnl_usd", "weekly_pnl_usd", "equity_usd",
                      "equity_source", "open_risk_usd", "open_risk_complete",
                      "funding", "last_confirmed"):
            self.assertIn(campo, block, campo)
        self.assertIsNone(block["funding"]["value"])

    def test_r05a_declara_o_cutover(self):
        with _cutover(False):
            self.assertFalse(r05a.current_control_sources()["r05b_cutover_enabled"])
            self.assertFalse(r05a.current_control_sources()["unified_financial_source"])
        with _cutover(True):
            cs = r05a.current_control_sources()
            self.assertTrue(cs["r05b_cutover_enabled"])
            self.assertTrue(cs["unified_financial_source"])
            self.assertEqual(cs["kill_switch_daily"]["scope"], "auto_only")

    def test_rollback_da_flag_restaura_o_legado(self):
        with _cutover(True):
            self.assertTrue(f.cutover_enabled())
        with _cutover(False):
            self.assertFalse(f.cutover_enabled())
            self.assertFalse(r05a.current_control_sources()["enforcement_changed"])
        # rollback não exige migration nem edição manual no banco
        self.assertNotIn("ALTER TABLE", self.src)
        self.assertNotIn("UPDATE risk_state", self.src)

    def test_estrategia_intacta(self):
        import subprocess
        # Audita o pacote R05B fechado, não o working tree de fases futuras.
        # Isso mantém a prova de escopo sem proibir migrações legítimas depois
        # do commit final auditado do R05B.
        for caminho in ("backend/services/strategy_evidence_service.py",
                        "backend/services/snapshot_service.py",
                        "backend/models", "backend/db.py", "frontend/src"):
            res = subprocess.run([
                "git", "diff", "--name-only", "d0952be4..572652fa", "--", caminho
            ],
                                 cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("range R05B d0952be4..572652fa indisponível neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"arquivo fora do escopo alterado: {caminho}")


if __name__ == "__main__":
    unittest.main()
