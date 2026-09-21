"""R05D — total financeiro COM funding, versionado e inativo por padrão.

`pnl_usd` continua sendo o líquido das execuções **EX-funding** (R05C): este
módulo não soma funding lá, não desconta taxa duas vezes e não reescreve
histórico. Ele só AGREGA os pagamentos já provados pelo ledger do R05C.

Um total só existe quando execuções, comissões e funding atribuível são
conhecidos na MESMA conta, ativo de liquidação e janela. Qualquer sobreposição,
paginação inconclusiva, conflito de ledger, conta divergente ou comissão em
outro ativo mantém `PENDING`/`UNKNOWN`, com subtotal separado do total.

Seleção da fonte: `R05_FINANCIAL_TOTAL_SOURCE` (default `legacy`). A flag do
cutover R05B (`R05_FINANCIAL_BREAKER_ENABLED`) continua com o significado dela.
Enquanto a fonte for `legacy`, nada aqui altera decisão operacional.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import os
from typing import Any, Dict, Optional, Sequence

CONTRACT_VERSION = "R05D_TOTAL_WITH_FUNDING_V1"
SOURCE_ENV = "R05_FINANCIAL_TOTAL_SOURCE"
SOURCE_LEGACY = "legacy"
SOURCE_ACCOUNTING = "accounting_total"
SETTLEMENT_ASSET = "USDT"
#: R05C é a única origem aceita; nada de ledger paralelo ou fetch novo.
ACCOUNTING_SCHEMA_VERSION = 1
STATE_COMPLETE = "COMPLETE"
STATE_PENDING = "PENDING"
STATE_UNKNOWN = "UNKNOWN"
#: Motivos fechados de exclusão de uma linha do total.
NO_ROWS = "NO_ROWS"
ROW_INVALID = "ROW_INVALID"
NOT_CONFIRMED = "NOT_CONFIRMED"
FUNDING_NOT_CONFIRMED = "FUNDING_NOT_CONFIRMED"
FEES_INCOMPLETE = "FEES_INCOMPLETE"
FEE_ASSET_UNCONVERTED = "FEE_ASSET_UNCONVERTED"
SETTLEMENT_MISMATCH = "SETTLEMENT_MISMATCH"
ACCOUNT_DIVERGENT = "ACCOUNT_DIVERGENT"
LEDGER_CONFLICT = "LEDGER_CONFLICT"
COLLECTION_UNPROVEN = "COLLECTION_UNPROVEN"
REASONS = (NO_ROWS, ROW_INVALID, NOT_CONFIRMED, FUNDING_NOT_CONFIRMED, FEES_INCOMPLETE,
           FEE_ASSET_UNCONVERTED, SETTLEMENT_MISMATCH, ACCOUNT_DIVERGENT, LEDGER_CONFLICT,
           COLLECTION_UNPROVEN)
LIMITATIONS = [
    "Total em USDT liquidado; NÃO é dólar convertido comprovado.",
    "Transferência, depósito, bônus e saque não são resultado de trading e não entram.",
    "Comissão em outro ativo só entraria com taxa/par/fonte/instante comprovados: sem prova, indisponível.",
    "Janela consultada e vazia prova funding zero; janela não consultada, não.",
    "Operação externa não entra por coincidência de símbolo; atribuição vem do ledger R05C.",
    "Provisão de custo não é custo realizado.",
]


def selected_source() -> str:
    """Fonte do total. Desconhecida ⇒ `legacy` (a nova fica inativa)."""
    value = (os.getenv(SOURCE_ENV, SOURCE_LEGACY) or "").strip().lower()
    return SOURCE_ACCOUNTING if value == SOURCE_ACCOUNTING else SOURCE_LEGACY


def accounting_total_enabled() -> bool:
    return selected_source() == SOURCE_ACCOUNTING


def _decimal(value: Any) -> Optional[Decimal]:
    """Número monetário do ledger (string decimal ou número finito)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def row_verdict(row: Any, *, account_scope: Optional[str],
                settlement_asset: str = SETTLEMENT_ASSET):
    """(valor, None) quando a linha entra no total; (None, motivo) quando não."""
    if not isinstance(row, dict):
        return None, ROW_INVALID
    if row.get("schema_version") != ACCOUNTING_SCHEMA_VERSION:
        return None, ROW_INVALID
    if row.get("conflicts"):
        return None, LEDGER_CONFLICT
    identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
    scope = identity.get("account_scope")
    if account_scope is None or not scope or scope != account_scope:
        return None, ACCOUNT_DIVERGENT
    if row.get("settlement_asset") != settlement_asset:
        return None, SETTLEMENT_MISMATCH
    if row.get("state") != "CONFIRMED":
        return None, NOT_CONFIRMED
    if row.get("funding_state") != "CONFIRMED":
        return None, FUNDING_NOT_CONFIRMED
    totals = row.get("totals") if isinstance(row.get("totals"), dict) else {}
    if totals.get("settlement_asset") not in (None, settlement_asset):
        return None, SETTLEMENT_MISMATCH
    if totals.get("fee_assets_unconverted"):
        # BNB (ou qualquer outro ativo) NUNCA é tratado como USDT.
        return None, FEE_ASSET_UNCONVERTED
    if totals.get("fees_complete") is not True:
        return None, FEES_INCOMPLETE
    value = _decimal(totals.get("net_including_funding"))
    if value is None:
        return None, ROW_INVALID
    return value, None


def aggregate(rows: Sequence[Any], *, account_scope: Optional[str] = None,
              window: Optional[dict] = None, collection: Optional[dict] = None,
              settlement_asset: str = SETTLEMENT_ASSET) -> Dict[str, Any]:
    """Total da janela. COMPLETE exige TODAS as linhas provadas e coleta provada."""
    reasons: Dict[str, int] = {}
    subtotal = Decimal("0")
    confirmed = 0
    for row in rows or ():
        value, reason = row_verdict(row, account_scope=account_scope,
                                    settlement_asset=settlement_asset)
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        subtotal += value
        confirmed += 1
    considered = len(rows or ())
    collection = collection if isinstance(collection, dict) else {}
    collection_proven = (collection.get("pagination_complete") is True
                         and collection.get("overlap_resolved") is True)
    if not collection_proven:
        reasons[COLLECTION_UNPROVEN] = reasons.get(COLLECTION_UNPROVEN, 0) + 1
    if considered == 0:
        state, total = STATE_UNKNOWN, None
        reasons[NO_ROWS] = reasons.get(NO_ROWS, 0) + 1
    elif confirmed == considered and collection_proven:
        state, total = STATE_COMPLETE, subtotal
    elif confirmed > 0:
        state, total = STATE_PENDING, None
    else:
        state, total = STATE_UNKNOWN, None
    return {
        "contract_version": CONTRACT_VERSION,
        "source": SOURCE_ACCOUNTING,
        "unit": settlement_asset,
        "usd_conversion_proven": False,
        "account_scope": account_scope,
        "window": dict(window) if isinstance(window, dict) else None,
        "state": state,
        "total_net_including_funding": float(total) if total is not None else None,
        "subtotal_confirmed": float(subtotal),
        "rows_considered": considered,
        "rows_confirmed": confirmed,
        "rows_excluded": considered - confirmed,
        "exclusion_reasons": reasons,
        "collection_proven": collection_proven,
        "limitations": list(LIMITATIONS),
    }


def worst_case_with_reservations(*, window_pnl_usd: Any, open_risk_usd: Any,
                                 reserved_risk_usd: Any, known_fees_usd: Any,
                                 proposed_risk_usd: Any) -> Dict[str, Any]:
    """Pior cenário da janela SEM double-count: P&L + risco aberto + reservas
    ainda não viradas trade + taxas já conhecidas + a entrada proposta."""
    parts = {"window_pnl_usd": window_pnl_usd, "open_risk_usd": open_risk_usd,
             "reserved_risk_usd": reserved_risk_usd, "known_fees_usd": known_fees_usd,
             "proposed_risk_usd": proposed_risk_usd}
    values: Dict[str, float] = {}
    for name, raw in parts.items():
        number = _decimal(raw)
        if number is None:
            return {"available": False, "reason_code": "COMPONENT_UNAVAILABLE",
                    "missing": name, "worst_case_usd": None}
        values[name] = float(number)
    worst = (values["window_pnl_usd"]
             - abs(values["open_risk_usd"]) - abs(values["reserved_risk_usd"])
             - abs(values["known_fees_usd"]) - abs(values["proposed_risk_usd"]))
    return {"available": True, "reason_code": None, "worst_case_usd": worst,
            "components": values}


def exposure_verdict(total_payload: Any) -> Dict[str, Any]:
    """Veredicto para AUMENTO de exposição no modo novo.

    Insuficiência essencial bloqueia abrir/aumentar. NUNCA impede proteção,
    redução ou fechamento, e não libera pausa de outro owner.
    """
    payload = total_payload if isinstance(total_payload, dict) else {}
    state = payload.get("state")
    if state == STATE_COMPLETE:
        return {"allow_exposure_increase": True, "reason_code": None,
                "blocks_protection": False, "blocks_close": False}
    reasons = payload.get("exclusion_reasons") if isinstance(payload.get("exclusion_reasons"), dict) else {}
    dominant = next(iter(sorted(reasons, key=lambda name: (-reasons[name], name))), "TOTAL_UNAVAILABLE")
    return {"allow_exposure_increase": False,
            "reason_code": f"R05D_{dominant}",
            "blocks_protection": False, "blocks_close": False}


async def fresh_total(session_factory, *, account_scope: Optional[str], since, until,
                      limit: int = 500) -> Dict[str, Any]:
    """Snapshot FRESCO da janela (sem cache de apresentação).

    Lê apenas o ledger R05C já persistido em `real_trades.execution_accounting`;
    não faz fetch novo, não pagina exchange e não abre transação longa.
    """
    from sqlalchemy import select
    from models.real_trade import RealTrade
    window = {"since": since.isoformat() if hasattr(since, "isoformat") else since,
              "until": until.isoformat() if hasattr(until, "isoformat") else until}
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(RealTrade.execution_accounting)
                .where(RealTrade.source == "auto", RealTrade.status != "open",
                       RealTrade.closed_at.is_not(None),
                       RealTrade.closed_at >= since, RealTrade.closed_at < until)
                .order_by(RealTrade.closed_at).limit(limit + 1)
            )).scalars().all()
    except Exception:
        payload = aggregate([], account_scope=account_scope, window=window)
        payload["exclusion_reasons"]["LEDGER_UNAVAILABLE"] = 1
        return payload
    truncated = len(rows) > limit
    collection = {"pagination_complete": not truncated, "overlap_resolved": True}
    return aggregate(rows[:limit], account_scope=account_scope, window=window,
                     collection=collection)
