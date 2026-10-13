"""Provas sintéticas do ledger, sem conta/SQL/rede ou heurística do manager."""
from __future__ import annotations

import copy
import socket
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import execution_accounting_service as r05
from services import live_event_accounting_service as ledger

T0 = 1_800_000_000_000
SOURCE_FINGERPRINT = "e" * 64


def context(**changes):
    value = {"account_scope": "TEST_ONLY_SIMULATED_ACCOUNT", "exchange": "binance",
             "market": "usdm_futures", "symbol": "SYNTHETICUSDT", "quote": "USDT", "side": "LONG",
             "position_side": "BOTH", "trade_id": "17", "opportunity_id": "opportunity-17",
             "dispatch_id": "cw-entry-17", "planned_stop": "95"}
    value.update(changes)
    return value


def accounting_record(ctx=None, *, provenance="TEST_ONLY", test_only=True, time_stop=False):
    """Fixture explícita: qty/preço/fee/PnL todos fatos sintéticos fornecidos.

    Controle numérico independente: 10@100; venda 4@110; reforço 2@105;
    média da exposição remanescente 101.25; venda 4@115; venda 4@120.
    Gross=40+55+75=170; taxas=1+.44+.21+.46+.48=2.59;
    funding=-.50. Net=167.41; net com funding=166.91 USDT (não USD).
    """
    ctx = context() if ctx is None else copy.deepcopy(ctx)
    long = ctx["side"] == "LONG"
    record = {"contract_version": ledger.INPUT_VERSION, "provenance": provenance, "test_only": test_only,
              "source_fingerprint": SOURCE_FINGERPRINT, "model": None, "orders": [], "fills": [],
              "funding": [], "fee_conversions": [], "slippage": [], "coverage": {}}
    if provenance == "DECLARED_MODEL":
        record["model"] = {"kind": "SUPPLIED_EVENT_TABLE", "version": ledger.MODEL_VERSION,
                           "source_fingerprint": ledger.model_fingerprint()}
    prices = ("100", "110", "105", "115", "120") if long else ("100", "90", "95", "85", "80")
    fees = ("1", ".44", ".21", ".46", ".48") if long else ("1", ".36", ".19", ".34", ".32")
    steps = [("ENTRY", "10", prices[0], "0", fees[0]),
             ("TP1", "4", prices[1], "40", fees[1]),
             ("PYRAMID", "2", prices[2], "0", fees[2]),
             ("TP2", "4", prices[3], "55", fees[3]),
             ("RUNNER", "4", prices[4], "75", fees[4])]
    if time_stop:
        steps[-1] = ("RUNNER", "2", "120", "37.5", ".24")
        steps.append(("TIME_STOP", "2", "102", "1.5", ".204"))
    for index, (role, qty, price, pnl, fee) in enumerate(steps):
        at = T0 + index * 10
        order_id = f"order-{index}"
        dispatch = ctx["dispatch_id"] if role == "ENTRY" else f"dispatch-{role.lower()}-17"
        binding = {k: v for k, v in ctx.items() if k != "planned_stop"}
        binding.update({"order_id": order_id, "dispatch_id": dispatch})
        side = ("BUY" if long else "SELL") if role in ledger.ENTRY_ROLES else ("SELL" if long else "BUY")
        wrapper = {"event_id": f"order-event-{index}", "causal_index": index * 2,
                   "observed_at_ms": at, "available_at_ms": at, "binding": binding, "role": role}
        record["orders"].append({**copy.deepcopy(wrapper), "raw": {
            "orderId": order_id, "clientOrderId": f"cw-test-{index}", "status": "FILLED",
            "symbol": ctx["symbol"], "side": side, "positionSide": ctx["position_side"],
            "executedQty": qty, "avgPrice": price, "reduceOnly": role not in ledger.ENTRY_ROLES,
            "type": "LIMIT" if role == "ENTRY" else "MARKET", "updateTime": at}})
        wrapper.update({"event_id": f"fill-event-{index}", "causal_index": index * 2 + 1})
        record["fills"].append({**copy.deepcopy(wrapper), "raw": {
            "id": f"exec-{index}", "orderId": order_id, "symbol": ctx["symbol"], "positionSide": ctx["position_side"],
            "side": side, "price": price, "qty": qty, "realizedPnl": pnl, "commission": fee,
            "commissionAsset": ctx["quote"], "time": at}})
    end = T0 + (len(steps) - 1) * 10
    fund_binding = {k: v for k, v in ctx.items() if k != "planned_stop"}
    record["funding"] = [{"event_id": "funding-event", "causal_index": 100, "observed_at_ms": end,
        "available_at_ms": end, "binding": fund_binding, "role": "FUNDING", "raw": {
            "incomeType": "FUNDING_FEE", "tranId": "funding-1", "income": "-.5", "asset": "USDT",
            "symbol": ctx["symbol"], "time": end}}]
    source_ref = {"source": provenance, "source_fingerprint": SOURCE_FINGERPRINT,
                  **{k: ctx[k] for k in ("account_scope", "exchange", "market", "symbol", "quote", "position_side", "trade_id")},
                  "window_start_ms": T0, "window_end_ms": end,
                  "exposure_intervals": [{"trade_id": ctx["trade_id"], "side": ctx["side"],
                                          "position_side": ctx["position_side"], "opened_at_ms": T0, "closed_at_ms": end}]}
    record["coverage"] = {"fills_complete": True, "funding_complete": True, "position_flat": True,
                          "window_start_ms": T0, "window_end_ms": end, "available_at_ms": end,
                          "source_ref": source_ref, "exclusive_exposure": True}
    return record


def admissions(record):
    return {"admitted_orders": [{"order_id": e["binding"]["order_id"], "dispatch_id": e["binding"]["dispatch_id"],
                                  "role": e["role"]} for e in record["orders"]]}


def replay(record, ctx=None, **kwargs):
    return ledger.replay_accounting(record, context() if ctx is None else ctx,
                                   as_of_ms=kwargs.pop("as_of_ms", T0 + 1_000), **kwargs)


def conversion(record, *, broker=False, settlement_value="2"):
    fill = r05.normalize_fill(record["fills"][0]["raw"], exchange="binance")[0]
    identity = {**r05.build_identity(exchange="binance", symbol=fill["symbol"], side="long",
                                     entry_order_id=fill["order_id"]), "account_scope": context()["account_scope"]}
    expected = r05.build_expected_context(identity=identity, fill=fill)
    at = T0 + 100
    source_ref = None
    if broker:
        source_ref = {"source": r05.LEDGER_SOURCE, "account_scope": expected["account_scope"], "exchange": "binance",
                      "symbol": expected["symbol"], "income_type": "COMMISSION", "trade_id": fill["exec_id"],
                      "tran_id": "fee-conversion-1", "asset": "USDT", "income": str(-Decimal(settlement_value)),
                      "time_ms": T0, "commission_asset": "BNB", "commission_qty": ".01",
                      "window_start_ms": T0, "window_end_ms": at}
    proof = r05.build_fee_conversion(expected=expected, settlement_value=settlement_value,
                price=str(Decimal(settlement_value) / Decimal(".01")),
                source=r05.FEE_SOURCE_BROKER if broker else r05.FEE_SOURCE_HISTORICAL,
                observed_start_ms=T0, observed_end_ms=at, now_ms=at, source_ref=source_ref)
    if not proof.pop("ok", False):
        raise AssertionError(proof)
    return {"event_id": "conversion-event", "observed_at_ms": at, "available_at_ms": at, "evidence": proof}


class LedgerNegativeTests(unittest.TestCase):
    def assert_unknown(self, record, reason, **kwargs):
        result = replay(record, **kwargs)
        self.assertFalse(result["economic_value_known"], result)
        self.assertFalse(result["financial_release_allowed"])
        self.assertIsNone(result["net_including_funding"])
        self.assertIn(reason, result["reasons"])
        return result

    def test_wrong_version_extra_field_and_missing_field_refuse(self):
        for mutation, reason in ((lambda r: r.update(contract_version="R10A"), "UNSUPPORTED_ACCOUNTING_CONTRACT"),
                                 (lambda r: r.update(extra=True), "CLOSED_SCHEMA:record"),
                                 (lambda r: r.pop("funding"), "CLOSED_SCHEMA:record")):
            record = accounting_record()
            mutation(record)
            self.assert_unknown(record, reason)

    def test_test_only_is_independent_mandatory_and_never_source_promotion(self):
        record = accounting_record()
        record.pop("test_only")
        self.assert_unknown(record, "CLOSED_SCHEMA:record")
        record = accounting_record()
        record["test_only"] = False
        self.assert_unknown(record, "TEST_ONLY_SOURCE_FLAG_MISMATCH")
        record = accounting_record(provenance="DECLARED_MODEL")
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertTrue(result["test_only"])
        self.assertFalse(result["confirmed"])
        record = accounting_record(provenance="OBSERVED_ACCOUNT")
        result = replay(record)
        self.assertTrue(result["test_only"])
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["observed_money"])
        self.assertFalse(result["real_study_allowed"])

    def test_binding_mismatch_each_exact_identity(self):
        for key in ledger.BINDING_FIELDS - {"dispatch_id"}:
            with self.subTest(key=key):
                record = accounting_record()
                record["fills"][0]["binding"][key] = "OTHER"
                self.assert_unknown(record, f"EVENT_BINDING_MISMATCH:{key}")

    def test_quote_is_not_usd_or_usdc_and_side_not_inferred(self):
        for change, reason in (({"quote": "USDC"}, "UNSUPPORTED_SETTLEMENT_ASSET"),
                               ({"quote": "USD"}, "UNSUPPORTED_SETTLEMENT_ASSET"),
                               ({"side": ""}, "INVALID_ID:side")):
            ctx = context(**change)
            self.assert_unknown(accounting_record(ctx), reason, ctx=ctx)

    def test_ack_terminal_missing_and_flat_unknown_are_not_fills(self):
        record = accounting_record()
        record["orders"][0]["raw"]["status"] = "NEW"
        self.assert_unknown(record, "ENTRY_ORDER_NOT_PROVEN")
        record = accounting_record()
        record["fills"] = []
        self.assert_unknown(record, "NO_ENTRY_FILL")
        record = accounting_record()
        record["coverage"]["position_flat"] = None
        result = self.assert_unknown(record, "POSITION_FLAT_NOT_CONFIRMED")
        self.assertFalse(result["physical_exposure_released"])

    def test_no_fee_no_pnl_nonfinite_and_bool_never_zero(self):
        for field in ("commission", "realizedPnl"):
            for value in (None, "NaN", "Infinity", True):
                with self.subTest(field=field, value=value):
                    record = accounting_record()
                    record["fills"][0]["raw"][field] = value
                    result = replay(record)
                    self.assertFalse(result["economic_value_known"])
                    self.assertIsNone(result["net_trade"])
                    self.assertIsNone(result["net_including_funding"])

    def test_risk_denominator_and_order_reduce_only_are_not_defaults(self):
        record = accounting_record()
        average = Decimal("1210") / Decimal("12")
        self.assert_unknown(record, "RISK_DENOMINATOR_ZERO", ctx=context(planned_stop=str(average)))
        record = accounting_record()
        record["orders"][-1]["raw"]["reduceOnly"] = False
        self.assert_unknown(record, "EXIT_ORDERS_NOT_PROVEN")
        record = accounting_record()
        record["coverage"]["fills_complete"] = False
        result = self.assert_unknown(record, "FILLS_WINDOW_INCOMPLETE")
        self.assertFalse(result["physical_exposure_released"])

    def test_duplicate_fill_and_funding_material_conflict_preserve_original(self):
        record = accounting_record()
        duplicate = copy.deepcopy(record["fills"][0])
        duplicate["event_id"] = "different-observation"
        duplicate["causal_index"] = 200
        duplicate["raw"]["price"] = "101"
        record["fills"].append(duplicate)
        result = self.assert_unknown(record, "EXEC_ID_CONTENT_CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["accounting"]["fills"]["binance|SYNTHETICUSDT|BOTH|exec-0"]["price"], "100")
        record = accounting_record()
        duplicate = copy.deepcopy(record["funding"][0])
        duplicate["event_id"] = "funding-other"
        duplicate["causal_index"] = 201
        duplicate["raw"]["time"] = T0 + 20
        record["funding"].append(duplicate)
        self.assert_unknown(record, "EXEC_ID_CONTENT_CONFLICT")

    def test_funding_absent_incomplete_and_overlapped_never_zero(self):
        record = accounting_record()
        record["funding"] = []
        record["coverage"]["funding_complete"] = False
        result = self.assert_unknown(record, "FUNDING_INCOMPLETE")
        self.assertIsNone(result["accounting"]["totals"]["funding_net"])
        self.assertTrue(result["physical_exposure_released"])
        record = accounting_record()
        record["coverage"]["source_ref"]["exposure_intervals"].append({
            "trade_id": "OTHER", "side": "LONG", "position_side": "BOTH", "opened_at_ms": T0, "closed_at_ms": None})
        self.assert_unknown(record, "FUNDING_EXPOSURE_OVERLAP")

    def test_funding_asset_source_and_window_are_exact(self):
        record = accounting_record()
        record["funding"][0]["raw"]["asset"] = "USDC"
        self.assert_unknown(record, "FUNDING_ASSET_UNSUPPORTED")
        record = accounting_record()
        record["coverage"]["source_ref"]["source_fingerprint"] = "0" * 64
        self.assert_unknown(record, "COVERAGE_SOURCE_MISMATCH:source_fingerprint")
        record = accounting_record()
        record["coverage"]["window_start_ms"] += 1
        self.assert_unknown(record, "COVERAGE_WINDOW_MISMATCH")

    def test_qty_overclose_and_pyramid_without_open_position(self):
        record = accounting_record()
        record["fills"][-1]["raw"]["qty"] = "5"
        record["orders"][-1]["raw"]["executedQty"] = "5"
        self.assert_unknown(record, "QUANTITY_OVER_CLOSE")
        record = accounting_record()
        record["fills"][0]["role"] = "PYRAMID"
        record["orders"][0]["role"] = "PYRAMID"
        self.assert_unknown(record, "PYRAMID_OUTSIDE_OPEN_POSITION")

    def test_r05_tolerance_does_not_erase_literal_residual_exposure(self):
        record = accounting_record()
        record["fills"][-1]["raw"]["qty"] = "3.999999999"
        record["orders"][-1]["raw"]["executedQty"] = "3.999999999"
        result = self.assert_unknown(record, "QUANTITY_NOT_CONSERVED_EXACT")
        self.assertFalse(result["physical_exposure_released"])

    def test_execution_report_requires_exact_child_dispatch_and_order(self):
        record = accounting_record()
        report = admissions(record)
        report["admitted_orders"][2]["dispatch_id"] = "OTHER-PYRAMID"
        self.assert_unknown(record, "EXECUTION_ORDER_NOT_ADMITTED", execution_report=report)

    def test_dispatch_suffix_or_prefix_is_not_order_admission(self):
        record = accounting_record()
        report = admissions(record)
        record["fills"][0]["binding"]["dispatch_id"] += "-mfb"
        self.assert_unknown(record, "EXECUTION_ORDER_NOT_ADMITTED", execution_report=report)

    def test_equal_millisecond_ordering_needs_unique_causal_index(self):
        record = accounting_record()
        record["fills"][1]["causal_index"] = record["fills"][0]["causal_index"]
        self.assert_unknown(record, "CAUSAL_INDEX_CONFLICT")
        record = accounting_record()
        record["fills"][1]["causal_index"], record["fills"][2]["causal_index"] = (
            record["fills"][2]["causal_index"], record["fills"][1]["causal_index"])
        self.assert_unknown(record, "EXECUTION_CAUSAL_ORDER_INVALID")
        report = admissions(record)
        report["admitted_orders"].pop()
        self.assert_unknown(record, "EXECUTION_ORDER_NOT_ADMITTED", execution_report=report)

    def test_manager_economics_and_authority_are_separate(self):
        good = {"economics": None, "authorizes_live": False, "promotable": False,
                "live_equivalent": False, "status": "COMPLETE_TRACE", "snapshots": [], "trace_hash": "t" * 64}
        bad = {**good, "economics": {"net_trade": 1_000_000}}
        self.assert_unknown(accounting_record(), "MANAGEMENT_ECONOMICS_BOUNDARY_INVALID", management_report=bad)
        bad = {**good, "authorizes_live": True}
        self.assert_unknown(accounting_record(), "MANAGEMENT_AUTHORITY_BOUNDARY_INVALID", management_report=bad)
        bad = {**good, "status": "INCOMPLETE_TRACE"}
        self.assert_unknown(accounting_record(), "MANAGEMENT_TRACE_INCOMPLETE", management_report=bad)

    def test_future_raw_not_opened_and_does_not_enter_lineage(self):
        record = accounting_record()
        future = copy.deepcopy(record["fills"][-1])
        future["event_id"] = "holdout-sentinel"
        future["available_at_ms"] = T0 + 2_000
        future["raw"] = object()  # Would fail schema/hash if outcomes were opened.
        record["fills"].append(future)
        result = self.assert_unknown(record, "EVENT_NOT_AVAILABLE_AS_OF")
        future["raw"] = {"DIFFERENT_HOLDOUT_OUTCOME": object()}
        second = replay(record)
        self.assertEqual(result["lineage_hash"], second["lineage_hash"])

    def test_foreign_fee_missing_estimated_tampered_and_future_unknown(self):
        record = accounting_record()
        record["fills"][0]["raw"].update(commission=".01", commissionAsset="BNB")
        self.assert_unknown(record, r05.FEE_REASON_MISSING)
        record["fee_conversions"] = [conversion(record)]
        self.assert_unknown(record, r05.FEE_REASON_ESTIMATED)
        record["fee_conversions"] = [conversion(record, broker=True)]
        record["fee_conversions"][0]["evidence"]["settlement_value"] = "3"
        result = replay(record)
        self.assertFalse(result["economic_value_known"])
        record["fee_conversions"] = [conversion(record, broker=True)]
        record["fee_conversions"][0]["available_at_ms"] = T0 + 2_000
        self.assert_unknown(record, "EVENT_NOT_AVAILABLE_AS_OF")

    def test_slippage_origin_invalid_value_and_double_discount_refuse(self):
        record = accounting_record()
        diagnostic = {"contract_version": ledger.SLIPPAGE_VERSION,
            "fill_key": "binance|SYNTHETICUSDT|BOTH|exec-0", "benchmark_price": "99", "settlement_value": "10",
            "source": "TEST_ONLY", "observed_at_ms": T0, "available_at_ms": T0, "included_in_fill_price": True}
        for changes, reason in (({"source": "OBSERVED_ACCOUNT"}, "SLIPPAGE_SOURCE_OR_FILL_MISMATCH"),
                                ({"settlement_value": "20"}, "SLIPPAGE_VALUE_INCOHERENT"),
                                ({"included_in_fill_price": False}, "SLIPPAGE_DOUBLE_DEDUCTION_OR_CONTRACT_INVALID")):
            record["slippage"] = [{**diagnostic, **changes}]
            self.assert_unknown(record, reason)

    def test_model_wrong_fingerprint_kind_and_mixed_broker_cost(self):
        record = accounting_record(provenance="DECLARED_MODEL")
        record["model"]["source_fingerprint"] = "a" * 64
        self.assert_unknown(record, "EXECUTION_MODEL_SOURCE_DRIFT")
        record = accounting_record(provenance="DECLARED_MODEL")
        record["model"]["kind"] = "INTRABAR_TOUCH"
        self.assert_unknown(record, "UNSUPPORTED_EXECUTION_MODEL")
        record = accounting_record(provenance="DECLARED_MODEL")
        record["fills"][0]["raw"].update(commission=".01", commissionAsset="BNB")
        record["fee_conversions"] = [conversion(record, broker=True)]
        self.assert_unknown(record, "MIXED_OBSERVED_AND_MODELED_COST")


class LedgerPositiveTests(unittest.TestCase):
    def test_exact_long_partial_pyramid_tp2_runner_independent_arithmetic(self):
        record = accounting_record()
        result = replay(record, execution_report=admissions(record))
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(result["status"], "KNOWN_TEST_ONLY")
        totals = result["accounting"]["totals"]
        self.assertEqual(Decimal(totals["gross_realized"]), Decimal("170"))
        self.assertEqual(Decimal(totals["fees_by_asset"]["USDT"]), Decimal("2.59"))
        self.assertEqual(Decimal(result["net_trade"]), Decimal("167.41"))
        self.assertEqual(Decimal(result["net_including_funding"]), Decimal("166.91"))
        self.assertEqual(Decimal(totals["entry_qty_executed"]), Decimal("12"))
        self.assertEqual(Decimal(totals["exit_qty_executed"]), Decimal("12"))
        self.assertEqual([x["qty_after"] for x in result["exposure_transitions"]], ["10", "6", "8", "4", "0"])
        # R05's published average-entry basis serializes a repeating Decimal;
        # reproduce that arithmetic independently (money above is exact cents).
        average_entry = Decimal("1210") / Decimal("12")
        self.assertEqual(Decimal(result["realized_r_from_net"]), Decimal("167.41") / ((average_entry - Decimal("95")) * Decimal("12")))
        self.assertTrue(result["tp1_before_stop"])
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["observed_money"])
        self.assertFalse(result["real_study_allowed"])

    def test_short_symmetric_basis_without_usd_conversion(self):
        ctx = context(side="SHORT", planned_stop="105")
        result = replay(accounting_record(ctx), ctx=ctx)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(Decimal(result["net_trade"]), Decimal("167.79"))
        self.assertEqual(Decimal(result["net_including_funding"]), Decimal("167.29"))
        self.assertEqual(result["accounting"]["totals"]["settlement_asset"], "USDT")

    def test_time_stop_additional_exit_once_with_runner_and_reinforcement(self):
        result = replay(accounting_record(time_stop=True))
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(Decimal(result["accounting"]["totals"]["gross_realized"]), Decimal("134"))
        self.assertEqual(Decimal(result["net_trade"]), Decimal("131.446"))
        self.assertEqual(Decimal(result["net_including_funding"]), Decimal("130.946"))
        self.assertEqual([e["role"] for e in result["role_events"]], ["ENTRY", "TP1", "PYRAMID", "TP2", "RUNNER", "TIME_STOP"])

    def test_r05_real_functions_are_called_and_recompute_same_output(self):
        record = accounting_record()
        names = ["normalize_fill", "attribute_fills", "merge_accounting", "finalize_accounting", "compute_totals"]
        mocks = []
        try:
            for name in names:
                wrapper = patch.object(r05, name, wraps=getattr(r05, name))
                mocks.append((wrapper, wrapper.start()))
            result = replay(record)
            self.assertTrue(result["economic_value_known"], result["reasons"])
            for name, (_, mock) in zip(names, mocks):
                self.assertGreater(mock.call_count, 0, name)
        finally:
            for wrapper, _ in reversed(mocks):
                wrapper.stop()

    def test_exact_duplicate_and_reobservation_are_economically_idempotent(self):
        record = accounting_record()
        baseline = replay(record)
        record["fills"].append(copy.deepcopy(record["fills"][0]))
        record["funding"].append(copy.deepcopy(record["funding"][0]))
        duplicate = replay(record)
        self.assertTrue(duplicate["economic_value_known"], duplicate["reasons"])
        self.assertEqual(duplicate["net_trade"], baseline["net_trade"])
        self.assertEqual(len(duplicate["exec_ids"]), 5)
        reobserved = copy.deepcopy(record["fills"][0])
        reobserved.update(event_id="observation-after-restart", causal_index=300,
                          observed_at_ms=T0 + 100, available_at_ms=T0 + 100)
        record["fills"].append(reobserved)
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(result["net_trade"], baseline["net_trade"])
        self.assertEqual(replay(record)["lineage_hash"], result["lineage_hash"])

    def test_decimal_representation_is_not_material_duplicate_conflict(self):
        record = accounting_record()
        duplicate = copy.deepcopy(record["fills"][0])
        duplicate["raw"].update(price="100.00", qty="10.000", commission="1.00")
        record["fills"].append(duplicate)
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(Decimal(result["net_trade"]), Decimal("167.41"))

    def test_equal_timestamp_fills_use_declared_causal_index(self):
        record = accounting_record()
        record["fills"][2]["raw"]["time"] = record["fills"][1]["raw"]["time"]
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual([e["role"] for e in result["role_events"]], ["ENTRY", "TP1", "PYRAMID", "TP2", "RUNNER"])

    def test_no_collector_client_http_sql_or_network_call(self):
        with patch.object(r05, "collect_trade_accounting", side_effect=AssertionError("collector forbidden")), \
             patch.object(r05, "reconcile_trade", side_effect=AssertionError("account forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            result = replay(accounting_record())
        self.assertTrue(result["economic_value_known"], result["reasons"])

    def test_literal_zero_fee_and_complete_empty_funding_valid(self):
        record = accounting_record()
        for event in record["fills"]:
            event["raw"].update(commission="0", commissionAsset="BNB")
        record["funding"] = []
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(result["accounting"]["totals"]["funding_net"], "0")
        self.assertEqual(result["accounting"]["totals"]["fee_conversion_state"], "NOT_REQUIRED")
        self.assertEqual(Decimal(result["net_including_funding"]), Decimal("170"))

    def test_foreign_fee_confirmed_only_r05e_v2_and_money_exact(self):
        record = accounting_record()
        record["fills"][0]["raw"].update(commission=".01", commissionAsset="BNB")
        record["fee_conversions"] = [conversion(record, broker=True)]
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(Decimal(result["net_trade"]), Decimal("166.41"))
        self.assertEqual(result["accounting"]["totals"]["fee_conversion_state"], "RESOLVED")
        self.assertEqual(result["result_available_ts_ms"], T0 + 100)

    def test_slippage_is_diagnostic_embedded_not_double_charged(self):
        record = accounting_record()
        record["slippage"] = [{"contract_version": ledger.SLIPPAGE_VERSION,
            "fill_key": "binance|SYNTHETICUSDT|BOTH|exec-0", "benchmark_price": "99", "settlement_value": "10",
            "source": "TEST_ONLY", "observed_at_ms": T0, "available_at_ms": T0, "included_in_fill_price": True}]
        result = replay(record)
        self.assertTrue(result["economic_value_known"], result["reasons"])
        self.assertEqual(result["slippage"]["total"], "10")
        self.assertFalse(result["slippage"]["deducted_again"])
        self.assertEqual(Decimal(result["net_trade"]), Decimal("167.41"))

    def test_provenance_is_distinct_and_never_real_study_authority(self):
        for provenance, status in (("TEST_ONLY", "KNOWN_TEST_ONLY"), ("DECLARED_MODEL", "KNOWN_MODELED"),
                                   ("OBSERVED_ACCOUNT", "ECONOMICALLY_COMPLETE")):
            record = accounting_record(provenance=provenance, test_only=provenance != "OBSERVED_ACCOUNT")
            result = replay(record, execution_report=admissions(record))
            self.assertTrue(result["economic_value_known"], result["reasons"])
            self.assertEqual(result["status"], status)
            self.assertEqual(result["confirmed"], provenance == "OBSERVED_ACCOUNT")
            self.assertEqual(result["observed_money"], provenance == "OBSERVED_ACCOUNT")
            self.assertFalse(result["real_study_allowed"])
            self.assertFalse(result["authorizes_live"])
            self.assertFalse(result["promotable"])


if __name__ == "__main__":
    unittest.main()
