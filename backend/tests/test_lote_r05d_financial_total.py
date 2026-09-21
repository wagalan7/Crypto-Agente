"""R05D — total financeiro COM funding: aritmética, completude e default.

Hermético: sem rede, banco real ou exchange. Fixtures sintéticas no formato do
ledger R05C. O modo novo é inativo por default e isso é testado.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket as _socket
import unittest
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R05D")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import financial_total_service as fts   # noqa: E402

SCOPE = "binance-usdm:fp123"


def ledger_row(*, net="10.00", funding="1.50", state="CONFIRMED", funding_state="CONFIRMED",
               fees_complete=True, unconverted=(), settlement="USDT", scope=SCOPE,
               conflicts=(), schema=1, totals_extra=None):
    totals = {"net_trade": "8.50", "funding_net": funding, "net_including_funding": net,
              "fees_complete": fees_complete, "fee_assets_unconverted": list(unconverted),
              "settlement_asset": settlement, "gross_complete": True}
    totals.update(totals_extra or {})
    return {"schema_version": schema, "state": state, "settlement_asset": settlement,
            "identity": {"account_scope": scope}, "funding_state": funding_state,
            "conflicts": list(conflicts), "totals": totals}


class Arithmetic(unittest.TestCase):
    def proven(self, rows, **over):
        values = dict(account_scope=SCOPE,
                      collection={"pagination_complete": True, "overlap_resolved": True})
        values.update(over)
        return fts.aggregate(rows, **values)

    def test_total_sums_only_proven_rows(self):
        payload = self.proven([ledger_row(net="10.00"), ledger_row(net="-4.25"),
                               ledger_row(net="0.00")])
        self.assertEqual(payload["state"], "COMPLETE")
        self.assertAlmostEqual(payload["total_net_including_funding"], 5.75)
        self.assertEqual(payload["rows_confirmed"], 3)
        self.assertEqual(payload["exclusion_reasons"], {})
        json.dumps(payload, allow_nan=False)

    def test_long_and_short_signs_and_funding_sign(self):
        # funding positivo (recebido) e negativo (pago) entram com o sinal real
        received = self.proven([ledger_row(net="12.00", funding="2.00")])
        paid = self.proven([ledger_row(net="6.00", funding="-2.00")])
        self.assertAlmostEqual(received["total_net_including_funding"], 12.0)
        self.assertAlmostEqual(paid["total_net_including_funding"], 6.0)
        short_loss = self.proven([ledger_row(net="-9.99")])
        self.assertAlmostEqual(short_loss["total_net_including_funding"], -9.99)

    def test_zero_funding_only_counts_when_proven(self):
        proven_zero = self.proven([ledger_row(net="8.50", funding="0")])
        self.assertEqual(proven_zero["state"], "COMPLETE")
        unproven = self.proven([ledger_row(net="8.50", funding=None, funding_state="PENDING")])
        self.assertEqual(unproven["state"], "UNKNOWN")
        self.assertEqual(unproven["exclusion_reasons"]["FUNDING_NOT_CONFIRMED"], 1)
        unavailable = self.proven([ledger_row(funding_state="UNAVAILABLE")])
        self.assertEqual(unavailable["exclusion_reasons"]["FUNDING_NOT_CONFIRMED"], 1)

    def test_partial_runner_rows_keep_subtotal_separate(self):
        payload = self.proven([ledger_row(net="10.00"), ledger_row(state="PARTIAL"),
                               ledger_row(state="PENDING")])
        self.assertEqual(payload["state"], "PENDING")
        self.assertIsNone(payload["total_net_including_funding"])
        self.assertAlmostEqual(payload["subtotal_confirmed"], 10.0)
        self.assertEqual(payload["exclusion_reasons"]["NOT_CONFIRMED"], 2)

    def test_other_asset_commission_is_never_converted(self):
        payload = self.proven([ledger_row(unconverted=["BNB"])])
        self.assertEqual(payload["state"], "UNKNOWN")
        self.assertEqual(payload["exclusion_reasons"]["FEE_ASSET_UNCONVERTED"], 1)
        self.assertIsNone(payload["total_net_including_funding"])
        incomplete = self.proven([ledger_row(fees_complete=False)])
        self.assertEqual(incomplete["exclusion_reasons"]["FEES_INCOMPLETE"], 1)

    def test_conflict_divergent_account_and_asset_are_excluded(self):
        cases = {"LEDGER_CONFLICT": ledger_row(conflicts=[{"exec_id": "x"}]),
                 "ACCOUNT_DIVERGENT": ledger_row(scope="outra-conta"),
                 "SETTLEMENT_MISMATCH": ledger_row(settlement="BUSD"),
                 "ROW_INVALID": ledger_row(schema=99)}
        for reason, row in cases.items():
            with self.subTest(reason=reason):
                payload = self.proven([row])
                self.assertEqual(payload["exclusion_reasons"].get(reason), 1, payload)
                self.assertIsNone(payload["total_net_including_funding"])
        self.assertEqual(self.proven([ledger_row()], account_scope=None)
                         ["exclusion_reasons"]["ACCOUNT_DIVERGENT"], 1)

    def test_inconclusive_collection_never_completes(self):
        rows = [ledger_row()]
        for collection in (None, {}, {"pagination_complete": True},
                           {"pagination_complete": False, "overlap_resolved": True},
                           {"pagination_complete": True, "overlap_resolved": False}):
            with self.subTest(collection=collection):
                payload = fts.aggregate(rows, account_scope=SCOPE, collection=collection)
                self.assertEqual(payload["state"], "PENDING")
                self.assertEqual(payload["exclusion_reasons"]["COLLECTION_UNPROVEN"], 1)
                self.assertAlmostEqual(payload["subtotal_confirmed"], 10.0)

    def test_empty_window_is_unknown_not_zero(self):
        payload = self.proven([])
        self.assertEqual(payload["state"], "UNKNOWN")
        self.assertIsNone(payload["total_net_including_funding"])
        self.assertEqual(payload["exclusion_reasons"]["NO_ROWS"], 1)
        self.assertEqual(payload["subtotal_confirmed"], 0.0)

    def test_invalid_numbers_never_become_totals(self):
        for bad in (float("nan"), float("inf"), "abc", True, None, {"v": 1}):
            with self.subTest(bad=bad):
                payload = self.proven([ledger_row(net=bad)])
                self.assertEqual(payload["exclusion_reasons"].get("ROW_INVALID"), 1)

    def test_unit_is_usdt_and_not_declared_usd(self):
        payload = self.proven([ledger_row()])
        self.assertEqual(payload["unit"], "USDT")
        self.assertIs(payload["usd_conversion_proven"], False)
        text = " ".join(payload["limitations"])
        for required in ("USDT", "Transferência", "Comissão em outro ativo", "funding zero",
                         "Operação externa", "Provisão"):
            self.assertIn(required, text)


class WorstCase(unittest.TestCase):
    def test_no_double_count_and_reservations_included(self):
        verdict = fts.worst_case_with_reservations(
            window_pnl_usd=-10.0, open_risk_usd=20.0, reserved_risk_usd=5.0,
            known_fees_usd=1.0, proposed_risk_usd=4.0)
        self.assertTrue(verdict["available"])
        self.assertAlmostEqual(verdict["worst_case_usd"], -40.0)
        self.assertEqual(verdict["components"]["reserved_risk_usd"], 5.0)

    def test_missing_component_makes_it_unavailable(self):
        for missing in ("window_pnl_usd", "open_risk_usd", "reserved_risk_usd",
                        "known_fees_usd", "proposed_risk_usd"):
            values = {"window_pnl_usd": 1.0, "open_risk_usd": 1.0, "reserved_risk_usd": 1.0,
                      "known_fees_usd": 1.0, "proposed_risk_usd": 1.0, missing: None}
            with self.subTest(missing=missing):
                verdict = fts.worst_case_with_reservations(**values)
                self.assertFalse(verdict["available"])
                self.assertEqual(verdict["missing"], missing)
                self.assertIsNone(verdict["worst_case_usd"])


class SourceSelection(unittest.TestCase):
    def test_default_is_legacy_and_unknown_values_stay_legacy(self):
        for value in (None, "", "legacy", "LEGACY", "qualquer", "1", "true"):
            env = {} if value is None else {fts.SOURCE_ENV: value}
            with patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop(fts.SOURCE_ENV, None)
                with self.subTest(value=value):
                    self.assertEqual(fts.selected_source(), fts.SOURCE_LEGACY)
                    self.assertFalse(fts.accounting_total_enabled())

    def test_new_source_requires_the_exact_selector(self):
        with patch.dict(os.environ, {fts.SOURCE_ENV: "accounting_total"}):
            self.assertTrue(fts.accounting_total_enabled())
        with patch.dict(os.environ, {"R05_FINANCIAL_BREAKER_ENABLED": "true"}):
            os.environ.pop(fts.SOURCE_ENV, None)
            self.assertFalse(fts.accounting_total_enabled())   # flag do R05B não ativa o R05D


class ExposureGate(unittest.IsolatedAsyncioTestCase):
    def verdict(self, state, reasons=None):
        return fts.exposure_verdict({"state": state, "exclusion_reasons": reasons or {}})

    def test_only_complete_allows_increasing_exposure(self):
        self.assertTrue(self.verdict("COMPLETE")["allow_exposure_increase"])
        for state in ("PENDING", "UNKNOWN", None):
            verdict = self.verdict(state, {"FUNDING_NOT_CONFIRMED": 2})
            with self.subTest(state=state):
                self.assertFalse(verdict["allow_exposure_increase"])
                self.assertEqual(verdict["reason_code"], "R05D_FUNDING_NOT_CONFIRMED")
                self.assertFalse(verdict["blocks_protection"])
                self.assertFalse(verdict["blocks_close"])

    async def test_entry_gate_is_inert_while_source_is_legacy(self):
        from services import shadow_trade_service as sts
        checks = {}
        os.environ.pop(fts.SOURCE_ENV, None)
        with patch.object(fts, "fresh_total", AsyncMock(side_effect=AssertionError("não deve consultar"))):
            self.assertIsNone(await sts._r05d_total_gate(checks))
        self.assertEqual(checks, {})

    async def test_entry_gate_blocks_increase_when_total_is_insufficient(self):
        from services import shadow_trade_service as sts
        payload = fts.aggregate([ledger_row(state="PENDING")], account_scope=SCOPE,
                                collection={"pagination_complete": True, "overlap_resolved": True})
        checks = {}
        with patch.dict(os.environ, {fts.SOURCE_ENV: "accounting_total"}), \
                patch.object(fts, "fresh_total", AsyncMock(return_value=payload)):
            verdict = await sts._r05d_total_gate(checks)
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason_code"], "R05D_NOT_CONFIRMED")
        self.assertEqual(checks["r05d_total"]["state"], "UNKNOWN")

    async def test_entry_gate_allows_when_total_is_complete(self):
        from services import shadow_trade_service as sts
        payload = fts.aggregate([ledger_row()], account_scope=SCOPE,
                                collection={"pagination_complete": True, "overlap_resolved": True})
        checks = {}
        with patch.dict(os.environ, {fts.SOURCE_ENV: "accounting_total"}), \
                patch.object(fts, "fresh_total", AsyncMock(return_value=payload)):
            self.assertIsNone(await sts._r05d_total_gate(checks))
        self.assertEqual(checks["r05d_total"]["state"], "COMPLETE")

    async def test_error_is_fail_closed_for_exposure_only(self):
        from services import shadow_trade_service as sts
        checks = {}
        with patch.dict(os.environ, {fts.SOURCE_ENV: "accounting_total"}), \
                patch.object(fts, "fresh_total", AsyncMock(side_effect=RuntimeError("ledger"))):
            verdict = await sts._r05d_total_gate(checks)
        self.assertEqual(verdict["reason_code"], "R05D_TOTAL_ERROR")
        self.assertFalse(verdict["ok"])


class Freshness(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_total_reads_the_ledger_without_cache_or_fetch(self):
        rows = [ledger_row(), ledger_row(state="PENDING")]
        calls = []

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return rows

        class _Session:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def execute(self_inner, statement):
                calls.append(str(statement))
                return _Result()

        now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        first = await fts.fresh_total(lambda: _Session(), account_scope=SCOPE,
                                      since=now - timedelta(days=1), until=now)
        second = await fts.fresh_total(lambda: _Session(), account_scope=SCOPE,
                                       since=now - timedelta(days=1), until=now)
        self.assertEqual(len(calls), 2, "cada tentativa exige snapshot fresco")
        self.assertEqual(first["state"], "PENDING")
        self.assertEqual(first, second)
        sql = calls[0]
        self.assertIn("execution_accounting", sql)
        self.assertIn("real_trades", sql)
        for forbidden in ("INSERT", "UPDATE", "DELETE"):
            self.assertNotIn(forbidden, sql.upper())

    async def test_ledger_failure_is_unknown_not_zero(self):
        def broken():
            raise RuntimeError("db fora")
        now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        payload = await fts.fresh_total(broken, account_scope=SCOPE,
                                        since=now - timedelta(days=1), until=now)
        self.assertEqual(payload["state"], "UNKNOWN")
        self.assertIsNone(payload["total_net_including_funding"])
        self.assertEqual(payload["exclusion_reasons"]["LEDGER_UNAVAILABLE"], 1)


class LegacyInvariants(unittest.TestCase):
    def test_pnl_usd_stays_ex_funding_and_history_untouched(self):
        accounting = (BACKEND / "services" / "execution_accounting_service.py").read_text()
        self.assertIn("Não é somado a `pnl_usd`", (BACKEND.parent / "docs" / "R05C_EXECUTION_ACCOUNTING.md").read_text())
        total = (BACKEND / "services" / "financial_total_service.py").read_text()
        for forbidden in ("UPDATE real_trades", "backfill", "pnl_usd =", "getenv(\"BINANCE"):
            self.assertNotIn(forbidden, total)
        self.assertIn("EX-funding", total)
        # O contrato do R05C (pnl_usd líquido EX-funding) continua no serviço:
        # funding entra em `funding_net`/`net_including_funding`, nunca em pnl_usd.
        self.assertIn("**EXCLUINDO funding**", accounting)
        self.assertIn('"funding_net"', accounting)

    def test_no_new_fetch_or_scheduler(self):
        total = (BACKEND / "services" / "financial_total_service.py").read_text()
        for forbidden in ("httpx", "aiohttp", "fetch_", "create_task", "while True"):
            self.assertNotIn(forbidden, total)

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
