"""Ledger de pesquisa por eventos; não consulta nem altera conta, SQL ou ENV.

O contrato financeiro é separado do relatório do gestor (cujo economics continua
nulo). As funções R05C/R05E reais normalizam, atribuem, deduplicam e totalizam
os fatos. SUPPLIED_EVENT_TABLE_V1 é uma tabela congelada de consequências, não
um simulador de fila/intravela: nenhum fill é produzido por este módulo.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from services import execution_accounting_service as accounting

INPUT_VERSION = "CW_LIVE_EXECUTION_ACCOUNTING_INPUT_V1"
LEDGER_VERSION = "CW_LIVE_EVENT_LEDGER_V1"
MODEL_VERSION = "SUPPLIED_EVENT_TABLE_V1"
SLIPPAGE_VERSION = "CW_EMBEDDED_SLIPPAGE_V1"
PROVENANCES = {"TEST_ONLY", "OBSERVED_ACCOUNT", "DECLARED_MODEL"}
ROLES = {"ENTRY", "PYRAMID", "TP1", "TP2", "RUNNER", "TIME_STOP", "STOP"}
ENTRY_ROLES = {"ENTRY", "PYRAMID"}
CONTEXT_FIELDS = {"account_scope", "exchange", "market", "symbol", "quote", "side",
                  "position_side", "trade_id", "opportunity_id", "dispatch_id", "planned_stop"}
BINDING_FIELDS = CONTEXT_FIELDS - {"planned_stop"}
INPUT_FIELDS = {"contract_version", "provenance", "test_only", "source_fingerprint", "model", "orders",
                "fills", "funding", "fee_conversions", "slippage", "coverage"}
EVENT_FIELDS = {"event_id", "causal_index", "observed_at_ms", "available_at_ms", "binding", "raw", "role"}
FILL_FIELDS = {"id", "orderId", "symbol", "positionSide", "side", "price", "qty",
               "realizedPnl", "commission", "commissionAsset", "time"}
ORDER_FIELDS = {"orderId", "clientOrderId", "status", "symbol", "positionSide", "side",
                "executedQty", "avgPrice", "reduceOnly", "type", "updateTime"}
FUNDING_FIELDS = {"incomeType", "tranId", "income", "asset", "symbol", "time"}
COVERAGE_FIELDS = {"fills_complete", "funding_complete", "position_flat", "window_start_ms",
                   "window_end_ms", "available_at_ms", "source_ref", "exclusive_exposure"}
SOURCE_REF_FIELDS = {"source", "source_fingerprint", "account_scope", "exchange", "market", "symbol",
                     "quote", "position_side", "trade_id", "window_start_ms", "window_end_ms", "exposure_intervals"}
EXPOSURE_FIELDS = {"trade_id", "side", "position_side", "opened_at_ms", "closed_at_ms"}
FEE_WRAPPER_FIELDS = {"event_id", "observed_at_ms", "available_at_ms", "evidence"}
SLIPPAGE_FIELDS = {"contract_version", "fill_key", "benchmark_price", "settlement_value", "source",
                   "observed_at_ms", "available_at_ms", "included_in_fill_price"}


class LedgerInputError(ValueError):
    """Recusa estrutural explicável, nunca um valor financeiro default."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def model_fingerprint() -> str:
    """Identidade local do modelo implementado, funções R05 e runtime exatos."""
    return digest({"version": MODEL_VERSION,
                   "ledger_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "accounting_source": hashlib.sha256(Path(accounting.__file__).read_bytes()).hexdigest(),
                   "python": list(sys.version_info[:3])})


def _closed(value: Any, fields: set[str], name: str) -> Mapping:
    if not isinstance(value, dict) or set(value) != fields:
        raise LedgerInputError(f"CLOSED_SCHEMA:{name}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise LedgerInputError(f"INVALID_ID:{name}")
    return value


def _ms(value: Any, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise LedgerInputError(f"INVALID_TIME:{name}")
    return value


def _number(value: Any, name: str, *, positive: bool = False) -> Decimal:
    result = accounting.to_decimal(value, allow_negative=not positive)
    if result is None or (positive and result <= 0):
        raise LedgerInputError(f"INVALID_NUMBER:{name}")
    return result


def validate_context(context: Any) -> dict:
    """Contrato fechado: USDT é o único settlement suportado pelo R05C real."""
    _closed(context, CONTEXT_FIELDS, "context")
    for key in BINDING_FIELDS:
        _text(context[key], key)
    if context["exchange"] != "binance" or context["market"] != "usdm_futures":
        raise LedgerInputError("UNSUPPORTED_MARKET")
    if (context["quote"] != "USDT" or not context["symbol"].endswith("USDT")
            or context["symbol"] == "USDT" or context["symbol"] != context["symbol"].upper()):
        raise LedgerInputError("UNSUPPORTED_SETTLEMENT_ASSET")
    if context["side"] not in {"LONG", "SHORT"}:
        raise LedgerInputError("IDENTITY_NO_SIDE")
    if context["position_side"] not in {"BOTH", context["side"]}:
        raise LedgerInputError("POSITION_SIDE_MISMATCH")
    _number(context["planned_stop"], "planned_stop", positive=True)
    return copy.deepcopy(context)


def validate_input(record: Any, context: Any) -> dict:
    """Validação do envelope, sem abrir o conteúdo de eventos futuros."""
    validate_context(context)
    _closed(record, INPUT_FIELDS, "record")
    if record["contract_version"] != INPUT_VERSION:
        raise LedgerInputError("UNSUPPORTED_ACCOUNTING_CONTRACT")
    if record["provenance"] not in PROVENANCES:
        raise LedgerInputError("UNSUPPORTED_EXECUTION_SOURCE")
    if type(record["test_only"]) is not bool:
        raise LedgerInputError("TEST_ONLY_FLAG_REQUIRED")
    if record["provenance"] == "TEST_ONLY" and record["test_only"] is not True:
        raise LedgerInputError("TEST_ONLY_SOURCE_FLAG_MISMATCH")
    if not isinstance(record["source_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["source_fingerprint"]):
        raise LedgerInputError("SOURCE_FINGERPRINT_INVALID")
    if record["provenance"] == "DECLARED_MODEL":
        model = _closed(record["model"], {"kind", "version", "source_fingerprint"}, "model")
        if model["kind"] != "SUPPLIED_EVENT_TABLE" or model["version"] != MODEL_VERSION:
            raise LedgerInputError("UNSUPPORTED_EXECUTION_MODEL")
        if model["source_fingerprint"] != model_fingerprint():
            raise LedgerInputError("EXECUTION_MODEL_SOURCE_DRIFT")
    elif record["model"] is not None:
        raise LedgerInputError("MIXED_EXECUTION_SOURCES")
    for key in ("orders", "fills", "funding", "fee_conversions", "slippage"):
        if not isinstance(record[key], list):
            raise LedgerInputError(f"CLOSED_SCHEMA:{key}")
    _closed(record["coverage"], COVERAGE_FIELDS, "coverage")
    # Return only envelope metadata: copying raw outcomes here would violate the
    # as-of boundary. Event material is inspected later, after availability.
    return {"contract_version": record["contract_version"], "provenance": record["provenance"],
            "test_only": record["test_only"],
            "source_fingerprint": record["source_fingerprint"], "model": copy.deepcopy(record["model"])}


def _binding(value: Any, context: dict, *, order: bool) -> dict:
    _closed(value, BINDING_FIELDS | ({"order_id"} if order else set()), "binding")
    for key in value:
        _text(value[key], f"binding.{key}")
    for key in BINDING_FIELDS - {"dispatch_id"}:
        if value[key] != context[key]:
            raise LedgerInputError(f"EVENT_BINDING_MISMATCH:{key}")
    if not order and value["dispatch_id"] != context["dispatch_id"]:
        raise LedgerInputError("EVENT_BINDING_MISMATCH:dispatch_id")
    return dict(value)


def _header(event: Any, fields: set[str], name: str, as_of_ms: int) -> bool:
    _closed(event, fields, name)
    _text(event["event_id"], f"{name}.event_id")
    observed = _ms(event["observed_at_ms"], f"{name}.observed_at_ms")
    available = _ms(event["available_at_ms"], f"{name}.available_at_ms")
    if observed > available:
        raise LedgerInputError("AVAILABILITY_BEFORE_OBSERVATION")
    return available <= as_of_ms


def _event_material(event: dict, group: str) -> dict:
    raw = dict(event["raw"])
    for field in ("price", "qty", "realizedPnl", "commission", "income", "executedQty", "avgPrice"):
        if field in raw:
            value = accounting.to_decimal(raw[field])
            raw[field] = format(value.normalize(), "f") if value is not None else None
    return {"kind": group, "binding": event["binding"], "role": event["role"], "raw": raw}


def _source_proof(coverage: dict, context: dict, envelope: dict, fills: list[dict]) -> tuple[bool, list[str]]:
    source = _closed(coverage["source_ref"], SOURCE_REF_FIELDS, "coverage.source_ref")
    for key in SOURCE_REF_FIELDS - {"window_start_ms", "window_end_ms", "exposure_intervals"}:
        expected = (envelope["provenance"] if key == "source" else envelope["source_fingerprint"]
                    if key == "source_fingerprint" else context[key])
        if source[key] != expected:
            raise LedgerInputError(f"COVERAGE_SOURCE_MISMATCH:{key}")
    start = _ms(coverage["window_start_ms"], "coverage.window_start_ms")
    end = _ms(coverage["window_end_ms"], "coverage.window_end_ms")
    if start > end or source["window_start_ms"] != start or source["window_end_ms"] != end:
        raise LedgerInputError("COVERAGE_WINDOW_MISMATCH")
    if any(not start <= int(fill["time"]) <= end for fill in fills):
        raise LedgerInputError("FILL_OUTSIDE_COMPLETE_WINDOW")
    intervals = source["exposure_intervals"]
    if not isinstance(intervals, list):
        raise LedgerInputError("CLOSED_SCHEMA:exposure_intervals")
    own, overlap = [], False
    seen = set()
    for item in intervals:
        _closed(item, EXPOSURE_FIELDS, "exposure_interval")
        _text(item["trade_id"], "exposure.trade_id")
        opened = _ms(item["opened_at_ms"], "exposure.opened_at_ms")
        closed = None if item["closed_at_ms"] is None else _ms(item["closed_at_ms"], "exposure.closed_at_ms")
        if closed is not None and closed < opened:
            raise LedgerInputError("EXPOSURE_INTERVAL_INVALID")
        if item["side"] not in {"LONG", "SHORT"} or item["position_side"] not in {"BOTH", item["side"]}:
            raise LedgerInputError("EXPOSURE_IDENTITY_INVALID")
        material = digest(item)
        if material in seen:
            continue
        seen.add(material)
        if item["trade_id"] == context["trade_id"]:
            if item["side"] != context["side"] or item["position_side"] != context["position_side"]:
                raise LedgerInputError("EXPOSURE_TRADE_BINDING_MISMATCH")
            own.append(item)
        elif item["position_side"] == context["position_side"] and opened <= end and (closed is None or closed >= start):
            overlap = True
    own_complete = (len(own) == 1 and own[0]["opened_at_ms"] <= start
                    and own[0]["closed_at_ms"] is not None and own[0]["closed_at_ms"] >= end)
    reasons = []
    if not own_complete:
        reasons.append("FUNDING_EXPOSURE_NOT_FINAL_OR_NOT_PROVEN")
    if overlap or coverage["exclusive_exposure"] is not True:
        reasons.append("FUNDING_EXPOSURE_OVERLAP")
    return not reasons, reasons


def _report_base(envelope: dict | None, context: Any, as_of_ms: Any) -> dict:
    source = (envelope or {}).get("provenance")
    return {"contract_version": LEDGER_VERSION, "provenance": source,
            "source_fingerprint": (envelope or {}).get("source_fingerprint"),
            "status": "NON_COMPARABLE", "economic_value_known": False,
            "confirmed": False, "observed_money": False,
            "model_quality": MODEL_VERSION if source == "DECLARED_MODEL" else None,
            "test_only": (envelope or {}).get("test_only", True), "real_study_allowed": False,
            "source_assurance": "DECLARED_SOURCE_NOT_AUTHENTICATION",
            "authorizes_live": False, "promotable": False, "live_equivalent": False,
            "context": copy.deepcopy(context) if isinstance(context, dict) else None,
            "as_of_ms": as_of_ms, "net_trade": None, "net_including_funding": None,
            "realized_r_including_funding": None,
            "realized_r_from_net": None, "result_available_ts_ms": None,
            "physical_exposure_released": False, "financial_release_allowed": False,
            "order_ids": [], "dispatch_ids": [], "exec_ids": [], "role_events": [],
            "exposure_transitions": [], "tp1_before_stop": None,
            "slippage": {"settlement_asset": "USDT", "total": None, "deducted_again": False},
            "coverage": {}, "reasons": [], "conflicts": [], "accounting": None,
            "lineage_hash": None}


def _finish(report: dict, material: dict) -> dict:
    report["reasons"] = sorted(set(report["reasons"]))
    report["lineage_hash"] = digest({"input": material, "output": {k: v for k, v in report.items()
                                                              if k != "lineage_hash"}})
    return report


def replay_accounting(record: Any, context: Any, management_report: Any = None, *,
                      execution_report: Any = None, as_of_ms: int) -> dict:
    """Recomputação causal pura. Ordem aceita/gestor fechado não prova dinheiro.

    No estudo o caller fornece execution_report para confirmar admissões exatas.
    Unitários do ledger podem fornecer ordens declaradas sem report de entrada;
    isto não lhes confere autoridade de origem, observação ou execução real.
    Material cujo available_at_ms é futuro não é aberto nem entra no hash.
    """
    report = _report_base(None, context, as_of_ms)
    material: dict = {"contract_version": INPUT_VERSION, "context": None, "consumed": {}}
    try:
        _ms(as_of_ms, "as_of_ms")
        context = validate_context(context)
        envelope = validate_input(record, context)
        report = _report_base(envelope, context, as_of_ms)
        material.update({"context": context, "envelope": envelope})
        consumed = material["consumed"]
        admissions = None
        if execution_report is not None:
            if not isinstance(execution_report, dict) or not isinstance(execution_report.get("admitted_orders"), list):
                raise LedgerInputError("EXECUTION_ADMISSIONS_MISSING")
            admissions = {}
            for item in execution_report["admitted_orders"]:
                _closed(item, {"order_id", "dispatch_id", "role"}, "admitted_order")
                key = _text(item["order_id"], "admitted_order.order_id")
                if key in admissions and admissions[key] != item:
                    raise LedgerInputError("EXECUTION_ADMISSION_CONFLICT")
                admissions[key] = dict(item)
            consumed["execution_admissions"] = copy.deepcopy(execution_report["admitted_orders"])
        if management_report is not None:
            if not isinstance(management_report, dict) or management_report.get("economics") is not None:
                raise LedgerInputError("MANAGEMENT_ECONOMICS_BOUNDARY_INVALID")
            if any(management_report.get(key) is not False for key in ("authorizes_live", "promotable", "live_equivalent")):
                raise LedgerInputError("MANAGEMENT_AUTHORITY_BOUNDARY_INVALID")
            if management_report.get("status") != "COMPLETE_TRACE":
                report["reasons"].append("MANAGEMENT_TRACE_INCOMPLETE")
            for snapshot in management_report.get("snapshots", []):
                state = snapshot.get("state") or {}
                if (str(state.get("id")) != context["trade_id"] or state.get("symbol") != context["symbol"]
                        or str(state.get("side")).upper() != context["side"]):
                    raise LedgerInputError("MANAGEMENT_TRADE_BINDING_MISMATCH")
            consumed["management_trace_hash"] = management_report.get("trace_hash")

        now = datetime.fromtimestamp(as_of_ms / 1000, tz=timezone.utc)
        orders, fills, funding, fill_meta, order_meta = [], [], [], {}, {}
        late = 0
        event_ids: dict[str, str] = {}
        causal_keys: dict[int, str] = {}
        for group in ("orders", "fills", "funding"):
            consumed[group] = []
            for event in record[group]:
                if not _header(event, EVENT_FIELDS, group, as_of_ms):
                    late += 1
                    continue
                index = _ms(event["causal_index"], f"{group}.causal_index", zero=True)
                binding = _binding(event["binding"], context, order=group != "funding")
                role = event["role"]
                if role not in ({"FUNDING"} if group == "funding" else ROLES):
                    raise LedgerInputError("EVENT_ROLE_INVALID")
                raw = _closed(event["raw"], FUNDING_FIELDS if group == "funding" else
                              ORDER_FIELDS if group == "orders" else FILL_FIELDS, f"{group}.raw")
                ts = _ms(raw["updateTime" if group == "orders" else "time"], f"{group}.raw.time")
                if ts > event["observed_at_ms"]:
                    raise LedgerInputError("EVENT_BEFORE_EXECUTION_TIME")
                if group != "funding" and str(raw["orderId"]) != binding["order_id"]:
                    raise LedgerInputError("EVENT_ORDER_BINDING_MISMATCH")
                if (raw["symbol"] != context["symbol"] or
                        (group != "funding" and raw["positionSide"] != context["position_side"])):
                    raise LedgerInputError("EVENT_MARKET_BINDING_MISMATCH")
                signature = digest(_event_material(event, group))
                if event["event_id"] in event_ids and event_ids[event["event_id"]] != signature:
                    report["conflicts"].append({"kind": "EVENT_ID", "key": event["event_id"]})
                event_ids[event["event_id"]] = signature
                if index in causal_keys and causal_keys[index] != event["event_id"]:
                    raise LedgerInputError("CAUSAL_INDEX_CONFLICT")
                causal_keys[index] = event["event_id"]
                consumed[group].append(copy.deepcopy(event))
                if group == "funding":
                    item, reason = accounting.normalize_funding(dict(raw))
                    if item is None:
                        raise LedgerInputError(f"INVALID_FUNDING:{reason}")
                    if item["asset"] != context["quote"]:
                        raise LedgerInputError("FUNDING_ASSET_UNSUPPORTED")
                    funding.append(item)
                    continue
                expected_side = accounting.entry_exit_sides(context["side"])[0 if role in ENTRY_ROLES else 1]
                if raw["side"] != expected_side:
                    raise LedgerInputError("EVENT_TRADE_SIDE_MISMATCH")
                if admissions is not None and admissions.get(binding["order_id"]) != {
                        "order_id": binding["order_id"], "dispatch_id": binding["dispatch_id"], "role": role}:
                    raise LedgerInputError("EXECUTION_ORDER_NOT_ADMITTED")
                meta = {"role": role, "binding": binding, "causal_index": index,
                        "available_at_ms": event["available_at_ms"], "observed_at_ms": event["observed_at_ms"]}
                if group == "orders":
                    item = accounting.normalize_order(dict(raw))
                    if item is None:
                        raise LedgerInputError("INVALID_ORDER")
                    item["role"] = "entry" if role in ENTRY_ROLES else "exit"
                    old = order_meta.get(item["order_id"])
                    if old and (old["binding"] != binding or old["role"] != role):
                        report["conflicts"].append({"kind": "ORDER_LINK", "key": item["order_id"]})
                    order_meta[item["order_id"]] = meta
                    orders.append(item)
                else:
                    item, reason = accounting.normalize_fill(dict(raw), exchange=context["exchange"])
                    if item is None:
                        raise LedgerInputError(f"INVALID_FILL:{reason}")
                    old = fill_meta.get(item["key"])
                    if old and (old["binding"] != binding or old["role"] != role):
                        report["conflicts"].append({"kind": "FILL_LINK", "key": item["key"]})
                    elif not old:
                        fill_meta[item["key"]] = meta
                    fills.append(item)

        entry_ids = sorted({oid for oid, meta in order_meta.items() if meta["role"] in ENTRY_ROLES}
                           | {item["order_id"] for item in fills if fill_meta[item["key"]]["role"] in ENTRY_ROLES})
        exit_ids = sorted({oid for oid, meta in order_meta.items() if meta["role"] not in ENTRY_ROLES}
                          | {item["order_id"] for item in fills if fill_meta[item["key"]]["role"] not in ENTRY_ROLES})
        if set(entry_ids) & set(exit_ids):
            raise LedgerInputError("ORDER_BOTH_ENTRY_AND_EXIT")
        identity = accounting.build_identity(exchange=context["exchange"], symbol=context["symbol"],
                    side=context["side"], entry_order_id=entry_ids[0] if entry_ids else None,
                    position_side=context["position_side"])
        identity["account_scope"] = context["account_scope"]
        acc = accounting.merge_accounting(None, identity=identity, fills=fills, funding=funding, orders=orders, now=now)
        # Funding duplicates include time/symbol/binding, not only amount/asset.
        funding_seen = {}
        for event, item in zip(consumed["funding"], funding):
            key = item["key"]
            signature = digest({"item": item, "binding": event["binding"]})
            if key in funding_seen and funding_seen[key] != signature:
                report["conflicts"].append({"kind": "FUNDING_LINK", "key": key})
            funding_seen[key] = signature
        consumed["fee_conversions"] = []
        fee_proofs = []
        fee_fields = set(accounting.FEE_INTEGRITY_FIELDS) | {"integrity_hash"}
        for event in record["fee_conversions"]:
            if not _header(event, FEE_WRAPPER_FIELDS, "fee_conversion", as_of_ms):
                late += 1
                continue
            proof = _closed(event["evidence"], fee_fields, "fee_conversion.evidence")
            if proof["observed_at_ms"] != event["observed_at_ms"]:
                raise LedgerInputError("FEE_AVAILABILITY_BINDING_MISMATCH")
            if envelope["provenance"] == "DECLARED_MODEL" and proof["source"] == accounting.FEE_SOURCE_BROKER:
                raise LedgerInputError("MIXED_OBSERVED_AND_MODELED_COST")
            if proof["fill_key"] not in acc["fills"]:
                raise LedgerInputError("FEE_FILL_NOT_ATTRIBUTED")
            consumed["fee_conversions"].append(copy.deepcopy(event))
            fee_proofs.append(dict(proof))
        acc = accounting.merge_accounting(acc, fee_conversions=fee_proofs, now=now)

        coverage = record["coverage"]
        coverage_available = _ms(coverage["available_at_ms"], "coverage.available_at_ms") <= as_of_ms
        for key in ("fills_complete", "funding_complete", "exclusive_exposure"):
            if not isinstance(coverage[key], bool):
                raise LedgerInputError(f"INVALID_COVERAGE_FLAG:{key}")
        if coverage["position_flat"] is not None and not isinstance(coverage["position_flat"], bool):
            raise LedgerInputError("INVALID_COVERAGE_FLAG:position_flat")
        exposure_complete = False
        if coverage_available:
            exposure_complete, exposure_reasons = _source_proof(coverage, context, envelope, list(acc["fills"].values()))
            report["reasons"].extend(exposure_reasons)
            consumed["coverage"] = copy.deepcopy(coverage)
        else:
            report["reasons"].append("COVERAGE_NOT_AVAILABLE_AS_OF")
        if late:
            report["reasons"].append("EVENT_NOT_AVAILABLE_AS_OF")
        acc["conflicts"].extend(report["conflicts"])
        acc = accounting.finalize_accounting(acc, entry_order_ids=entry_ids, exit_order_ids=exit_ids,
                    fills_window_complete=coverage_available and coverage["fills_complete"] and not late,
                    funding_window_complete=coverage_available and coverage["funding_complete"] and exposure_complete and not late,
                    position_flat=coverage["position_flat"] if coverage_available else None,
                    planned_stop=context["planned_stop"], now=now)
        attribution = accounting.attribute_fills(list(acc["fills"].values()), identity=identity,
                                                  entry_order_ids=entry_ids, exit_order_ids=exit_ids)
        # Explicit second calculation is a parity assertion with the real finalizer.
        totals = accounting.compute_totals(attribution["entry"], attribution["exit"], list(acc["funding"].values()),
                    funding_state=acc["funding_state"], fee_conversions=acc["fee_conversions"],
                    fee_context=accounting.fee_expected_contexts(acc))
        if totals != acc["totals"]:
            raise LedgerInputError("ACCOUNTING_RECOMPUTATION_DRIFT")
        remaining, opened, closed_once = Decimal("0"), False, False
        previous_causal = None
        for fill in sorted(acc["fills"].values(), key=lambda f: (int(f["time"]), fill_meta[f["key"]]["causal_index"])):
            meta = fill_meta[fill["key"]]
            if previous_causal is not None and meta["causal_index"] <= previous_causal:
                raise LedgerInputError("EXECUTION_CAUSAL_ORDER_INVALID")
            previous_causal = meta["causal_index"]
            role, qty = meta["role"], Decimal(fill["qty"])
            before = remaining
            if role in ENTRY_ROLES:
                if closed_once or (role == "PYRAMID" and not opened):
                    raise LedgerInputError("PYRAMID_OUTSIDE_OPEN_POSITION")
                remaining += qty
                opened = True
                if accounting.to_decimal(fill["realized_pnl"]) not in (None, Decimal("0")):
                    raise LedgerInputError("ENTRY_REALIZED_PNL_NONZERO")
            else:
                remaining -= qty
                if not opened or remaining < 0:
                    raise LedgerInputError("QUANTITY_OVER_CLOSE")
                closed_once = remaining == 0
            role_event = {"role": role, "exec_id": fill["exec_id"], "order_id": fill["order_id"],
                          "dispatch_id": meta["binding"]["dispatch_id"], "fill_time_ms": int(fill["time"]),
                          "available_at_ms": meta["available_at_ms"], "causal_index": meta["causal_index"], "qty": fill["qty"]}
            report["role_events"].append(role_event)
            report["exposure_transitions"].append({**role_event, "qty_before": format(before, "f"),
                                                    "qty_after": format(remaining, "f")})
        if remaining != 0:
            # The existing R05 tolerance is preserved, but cannot turn a
            # research ledger's literal residual exposure into an exact flat.
            report["reasons"].append("QUANTITY_NOT_CONSERVED_EXACT")
        report["physical_exposure_released"] = (opened and remaining == 0 and coverage_available
                                                 and coverage["position_flat"] is True and not acc["conflicts"]
                                                 and acc["coverage"]["fills_window_complete"]
                                                 and acc["coverage"]["entry_order_proven"]
                                                 and acc["coverage"]["exit_orders_proven"])
        if coverage["position_flat"] is not True:
            report["reasons"].append("POSITION_FLAT_NOT_CONFIRMED")
        if acc["state"] != accounting.STATE_CONFIRMED:
            report["reasons"].append(acc["reason_code"] or "EXECUTION_ACCOUNTING_UNKNOWN")
        if totals["net_including_funding"] is None:
            report["reasons"].append("FUNDING_INCOMPLETE" if totals["funding_net"] is None else "NET_TRADE_UNKNOWN")
        if acc["realized_r"] is None:
            report["reasons"].append(acc["realized_r_reason_code"] or "REALIZED_R_UNKNOWN")

        consumed["slippage"] = []
        slippage_by_key = {}
        for item in record["slippage"]:
            _closed(item, SLIPPAGE_FIELDS, "slippage")
            available = _ms(item["available_at_ms"], "slippage.available_at_ms")
            observed = _ms(item["observed_at_ms"], "slippage.observed_at_ms")
            if observed > available:
                raise LedgerInputError("AVAILABILITY_BEFORE_OBSERVATION")
            if available > as_of_ms:
                report["reasons"].append("SLIPPAGE_NOT_AVAILABLE_AS_OF")
                continue
            if item["contract_version"] != SLIPPAGE_VERSION or item["included_in_fill_price"] is not True:
                raise LedgerInputError("SLIPPAGE_DOUBLE_DEDUCTION_OR_CONTRACT_INVALID")
            if item["source"] != envelope["provenance"] or item["fill_key"] not in acc["fills"]:
                raise LedgerInputError("SLIPPAGE_SOURCE_OR_FILL_MISMATCH")
            fill = acc["fills"][item["fill_key"]]
            benchmark = _number(item["benchmark_price"], "slippage.benchmark_price", positive=True)
            value = _number(item["settlement_value"], "slippage.settlement_value")
            expected = (Decimal(fill["price"]) - benchmark) * Decimal(fill["qty"])
            if fill["side"] == "SELL":
                expected = -expected
            if value != expected:
                raise LedgerInputError("SLIPPAGE_VALUE_INCOHERENT")
            old = slippage_by_key.get(item["fill_key"])
            if old is not None and old != value:
                raise LedgerInputError("SLIPPAGE_EXEC_ID_CONFLICT")
            slippage_by_key[item["fill_key"]] = value
            consumed["slippage"].append(copy.deepcopy(item))
        report["slippage"]["total"] = (format(sum(slippage_by_key.values(), Decimal("0")), "f")
                                          if slippage_by_key else None)
        report["coverage"] = {**acc["coverage"], "pending_available_events": late,
                              "slippage_fills": len(slippage_by_key), "attributed_fills": len(acc["fills"])}
        report["order_ids"] = sorted(set(entry_ids + exit_ids))
        report["dispatch_ids"] = sorted({m["binding"]["dispatch_id"] for m in fill_meta.values()})
        report["exec_ids"] = sorted({f["exec_id"] for f in acc["fills"].values()})
        report["conflicts"] = acc["conflicts"]
        # R05C's constant source identifies its schema, not evidence provenance.
        # The exposed ledger always carries the real provenance of supplied facts.
        acc["calculation_engine"] = "R05C_EXECUTION_ACCOUNTING/R05E_FEE_CONVERSION_V2"
        acc["source"] = envelope["provenance"]
        report["accounting"] = acc
        if acc["state"] == accounting.STATE_CONFIRMED:
            report["net_trade"] = totals["net_trade"]
            report["realized_r_from_net"] = acc["realized_r"]
        report["economic_value_known"] = not report["reasons"] and totals["net_including_funding"] is not None
        if report["economic_value_known"]:
            report["net_including_funding"] = totals["net_including_funding"]
            total_r, total_r_reason = accounting.realized_r_from_net(totals["net_including_funding"],
                totals["entry_avg_price"], context["planned_stop"], totals["entry_qty_executed"])
            if total_r_reason is not None:
                raise LedgerInputError(total_r_reason)
            report["realized_r_including_funding"] = total_r
            report["confirmed"] = envelope["provenance"] == "OBSERVED_ACCOUNT" and not envelope["test_only"]
            report["observed_money"] = report["confirmed"]
            report["status"] = {"TEST_ONLY": "KNOWN_TEST_ONLY", "DECLARED_MODEL": "KNOWN_MODELED",
                                  "OBSERVED_ACCOUNT": "ECONOMICALLY_COMPLETE"}[envelope["provenance"]]
            if envelope["provenance"] == "OBSERVED_ACCOUNT" and envelope["test_only"]:
                report["status"] = "KNOWN_TEST_ONLY"
            report["financial_release_allowed"] = True
            result_times = [coverage["available_at_ms"]]
            for group in ("orders", "fills", "funding", "fee_conversions", "slippage"):
                result_times.extend(item["available_at_ms"] for item in consumed[group])
            report["result_available_ts_ms"] = max(result_times)
            terminals = [e for e in report["role_events"] if e["role"] in {"TP1", "STOP"}]
            report["tp1_before_stop"] = (terminals[0]["role"] == "TP1" if terminals else None)
        else:
            report["status"] = "CONFLICT" if acc["conflicts"] else "UNKNOWN"
        return _finish(report, material)
    except (LedgerInputError, ValueError, TypeError, KeyError, OverflowError) as error:
        report["reasons"].append(str(error) if isinstance(error, LedgerInputError) else "INVALID_ACCOUNTING_INPUT")
        report.update({"status": "NON_COMPARABLE", "economic_value_known": False, "confirmed": False,
                       "observed_money": False, "net_trade": None, "net_including_funding": None,
                       "realized_r_from_net": None, "result_available_ts_ms": None,
                       "realized_r_including_funding": None,
                       "financial_release_allowed": False, "physical_exposure_released": False})
        # Invalid numeric objects never enter a canonical hash; preserve only
        # consumed validated material and the refusal, not arbitrary raw input.
        try:
            return _finish(report, material)
        except (ValueError, TypeError):
            report["lineage_hash"] = digest({"contract_version": LEDGER_VERSION,
                                               "reasons": sorted(set(report["reasons"]))})
            return report
