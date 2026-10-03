"""
R05C — contabilização por EXECUÇÕES REAIS.

Origem única dos números financeiros de uma operação `auto`: os fills
confirmados pela exchange, a quantidade realmente executada, TODAS as parciais,
as comissões por ativo e a origem do fechamento. Funding fica SEPARADO e
verificável.

CONTRATO DE VALOR
    entry_price   = Σ(price × qty) / Σ(qty) dos fills de ENTRADA
    qty_initial   = quantidade EXECUTADA da ordem inicial (nunca a planejada)
    exit_price    = média ponderada das saídas ATRIBUÍDAS
    gross         = Σ(realizedPnl) dos fills atribuídos (fonte primária Binance)
    fees_by_asset = comissões de TODOS os fills, uma vez por `exec_id`
    net_trade     = gross − comissões confirmadas na moeda de liquidação
    funding_net   = Σ(FUNDING_FEE atribuíveis), preservando o sinal
    net_including_funding = net_trade + funding_net, só com AMBOS conhecidos

`pnl_usd` preserva o contrato legado: líquido de execuções/comissões e
**EXCLUINDO funding**. O nome é legado — o ativo é registrado como `USDT`, sem
afirmar conversão cambial para USD.

AUSÊNCIA ≠ ZERO. Comissão ausente, funding não consultado, paginação incompleta
ou atribuição ambígua produzem estado explicável (`PENDING`/`PARTIAL`/
`AMBIGUOUS`), nunca um zero fabricado. Registro antigo sem contabilidade é
`LEGACY_UNVERIFIED` — jamais confirmação retroativa.

ARQUITETURA: este módulo é PURO em relação a mutação de mercado. A coleta usa
SOMENTE `GET`. Nenhuma falha contábil pode atrasar colocação de SL, fechamento
de emergência, renovação de lease ou limpeza P02/P03.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SOURCE_BINANCE = "BINANCE_USDM_USER_TRADES"
SETTLEMENT_ASSET = "USDT"

# ── Estados CONTÁBEIS (separados da máquina de estados operacional) ─────────
STATE_CONFIRMED = "CONFIRMED"        # execuções completas e conservadas
STATE_PARTIAL = "PARTIAL"            # parte comprovada, parte pendente
STATE_PENDING = "PENDING"            # ainda sem evidência suficiente
STATE_AMBIGUOUS = "AMBIGUOUS"        # fills não exclusivos / origem incerta
STATE_CONFLICT = "CONFLICT"          # mesmo exec_id com conteúdo divergente
STATE_FAILED = "FAILED"              # tentativas esgotadas, visível p/ revisão
STATE_LEGACY = "LEGACY_UNVERIFIED"   # registro anterior ao R05C

ACCOUNTING_STATES = (STATE_CONFIRMED, STATE_PARTIAL, STATE_PENDING,
                     STATE_AMBIGUOUS, STATE_CONFLICT, STATE_FAILED, STATE_LEGACY)

# Completude do funding é SEPARADA da completude das execuções: funding pendente
# não apaga um `net_trade` já confirmado.
FUNDING_CONFIRMED = "CONFIRMED"
FUNDING_PENDING = "PENDING"
FUNDING_UNAVAILABLE = "UNAVAILABLE"

MAX_ATTEMPTS = 6
RETRY_BACKOFF_S = (60, 300, 900, 3600, 10800, 21600)

# Origem do fechamento — motivo operacional, origem e sinal do resultado são
# coisas distintas. Lucro não prova TP; prejuízo não prova execução de SL.
CLOSE_ORIGIN_BOT = "BOT_MANAGED"
CLOSE_ORIGIN_EXTERNAL = "EXTERNAL_OR_UNKNOWN"

# Prefixos de `clientOrderId` reconhecidos como do próprio bot.
BOT_COID_PREFIXES = ("cw-",)

_QTY_TOLERANCE = Decimal("0.000000005")


class AccountingError(Exception):
    """Erro estrutural de contabilidade — nunca vira zero silencioso."""


# ════════════════════════════════════════════════════════════════════════════
#  Normalização — Decimal a partir das STRINGS da exchange
# ════════════════════════════════════════════════════════════════════════════
def to_decimal(value: Any, *, allow_negative: bool = True) -> Optional[Decimal]:
    """`Decimal` exato a partir da string da exchange.

    Rejeita `None`, `bool`, NaN/infinito e texto inválido. `float` é aceito com
    conversão por `repr` (nunca binário cru) porque o banco devolve float — mas
    a fonte preferida é sempre a string original.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            dec = Decimal(repr(value))
        else:
            text = str(value).strip()
            if not text:
                return None
            dec = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not dec.is_finite():
        return None
    if not allow_negative and dec < 0:
        return None
    return dec


def _id_str(value: Any) -> Optional[str]:
    """IDs são STRINGS. Nunca converter id longo por float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):            # id nunca deveria chegar como float
        return None
    text = str(value).strip()
    return text or None


def _dstr(dec: Optional[Decimal]) -> Optional[str]:
    """Serializa preservando precisão (JSON guarda string, não float)."""
    return format(dec, "f") if isinstance(dec, Decimal) else None


def normalize_symbol(symbol: Any) -> Optional[str]:
    """Símbolo da exchange COM quote exata. `BTCUSDT` != `BTCUSDC`."""
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    # `BTC/USDT:USDT` → `BTCUSDT`; já normalizado passa direto.
    if "/" in text:
        base, _, rest = text.partition("/")
        quote = rest.split(":")[0]
        return f"{base}{quote}" if base and quote else None
    return text


def entry_exit_sides(trade_side: Any) -> Optional[Tuple[str, str]]:
    """(lado de ENTRADA, lado de SAÍDA) na convenção da exchange."""
    side = str(trade_side or "").strip().lower()
    if side == "long":
        return ("BUY", "SELL")
    if side == "short":
        return ("SELL", "BUY")
    return None


def fill_key(exchange: str, symbol: str, position_side: str, exec_id: str) -> str:
    """Chave ESTÁVEL do fill — identidade completa, não só o `exec_id`."""
    return f"{exchange}|{symbol}|{position_side}|{exec_id}"


def normalize_fill(raw: Any, *, exchange: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normaliza UM fill de `/fapi/v1/userTrades`. Devolve (fill, motivo_rejeição)."""
    if not isinstance(raw, dict):
        return None, "fill não é objeto"
    exec_id = _id_str(raw.get("id") if raw.get("id") is not None else raw.get("exec_id"))
    if not exec_id:
        return None, "exec_id ausente"
    order_id = _id_str(raw.get("orderId") if raw.get("orderId") is not None
                       else raw.get("order_id"))
    if not order_id:
        return None, "orderId ausente"
    symbol = normalize_symbol(raw.get("symbol"))
    if not symbol:
        return None, "symbol ausente"
    side = str(raw.get("side") or "").strip().upper()
    if side not in ("BUY", "SELL"):
        return None, "side inválido"
    position_side = str(raw.get("positionSide") or raw.get("position_side")
                        or "").strip().upper()
    if position_side not in ("BOTH", "LONG", "SHORT"):
        return None, "positionSide inválido"
    price = to_decimal(raw.get("price"), allow_negative=False)
    qty = to_decimal(raw.get("qty"), allow_negative=False)
    if price is None or price <= 0:
        return None, "price inválido"
    if qty is None or qty <= 0:
        return None, "qty inválida"
    realized = to_decimal(raw.get("realizedPnl") if "realizedPnl" in raw
                          else raw.get("realized_pnl"))
    # Comissão AUSENTE não é zero: fica `None` e derruba a completude de fees.
    commission = to_decimal(raw.get("commission"), allow_negative=False)
    commission_asset = (str(raw.get("commissionAsset") or raw.get("commission_asset")
                            or "").strip().upper() or None)
    if commission is not None and not commission_asset:
        return None, "commissionAsset ausente"
    ts = _id_str(raw.get("time"))
    if ts is None or not ts.isdigit() or int(ts) <= 0:
        return None, "time inválido"
    return {
        "exec_id": exec_id,
        "order_id": order_id,
        "symbol": symbol,
        "position_side": position_side,
        "side": side,
        "price": _dstr(price),
        "qty": _dstr(qty),
        "realized_pnl": _dstr(realized),
        "commission": _dstr(commission),
        "commission_asset": commission_asset,
        "time": ts,
        "key": fill_key(exchange, symbol, position_side, exec_id),
    }, None


def normalize_funding(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normaliza UM lançamento de `/fapi/v1/income`.

    Só `FUNDING_FEE` entra. Transferência, bônus, rebate e `COMMISSION` NÃO
    viram funding nem PnL de trade — `COMMISSION` já vem descontada dos fills e
    somá-la aqui seria desconto duplo.
    """
    if not isinstance(raw, dict):
        return None, "income não é objeto"
    income_type = str(raw.get("incomeType") or raw.get("income_type") or "").strip().upper()
    if income_type != "FUNDING_FEE":
        return None, f"incomeType {income_type or 'ausente'} não é funding"
    tran_id = _id_str(raw.get("tranId") if raw.get("tranId") is not None
                      else raw.get("tran_id"))
    if not tran_id:
        return None, "tranId ausente"
    income = to_decimal(raw.get("income"))
    if income is None:
        return None, "income inválido"
    asset = str(raw.get("asset") or "").strip().upper()
    if not asset:
        return None, "asset ausente"
    symbol = normalize_symbol(raw.get("symbol"))
    return {
        "income_type": income_type,
        "tran_id": tran_id,
        "income": _dstr(income),
        "asset": asset,
        "symbol": symbol,
        "time": _id_str(raw.get("time")),
        "key": f"{income_type}:{tran_id}",
    }, None


def normalize_order(raw: Any) -> Optional[Dict[str, Any]]:
    """Normaliza UMA ordem de `/fapi/v1/order`. `algoId` != `orderId`."""
    if not isinstance(raw, dict):
        return None
    order_id = _id_str(raw.get("orderId") if raw.get("orderId") is not None
                       else raw.get("order_id"))
    if not order_id:
        return None
    return {
        "order_id": order_id,
        "client_order_id": _id_str(raw.get("clientOrderId") or raw.get("client_order_id")),
        "status": str(raw.get("status") or "").strip().upper() or None,
        "symbol": normalize_symbol(raw.get("symbol")),
        "side": str(raw.get("side") or "").strip().upper() or None,
        "position_side": str(raw.get("positionSide") or "").strip().upper(),
        "executed_qty": _dstr(to_decimal(raw.get("executedQty"), allow_negative=False)),
        "avg_price": _dstr(to_decimal(raw.get("avgPrice"), allow_negative=False)),
        "reduce_only": raw.get("reduceOnly") is True or raw.get("reduceOnly") == "true",
        "type": str(raw.get("type") or raw.get("origType") or "").strip().upper() or None,
        "time": _id_str(raw.get("updateTime") or raw.get("time")),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Identidade e atribuição — exclusividade PROVADA, nunca arbitrária
# ════════════════════════════════════════════════════════════════════════════
def build_identity(*, exchange: str, symbol: Any, side: Any,
                   entry_order_id: Any, entry_client_order_id: Any = None,
                   position_side: str = "BOTH") -> Dict[str, Any]:
    """Identidade da operação. Sem símbolo/lado/ordem válidos não há atribuição."""
    return {
        "exchange": str(exchange or "").strip().lower() or None,
        "symbol": normalize_symbol(symbol),
        "position_side": str(position_side or "BOTH").strip().upper(),
        "side": str(side or "").strip().lower() or None,
        "entry_order_id": _id_str(entry_order_id),
        "entry_client_order_id": _id_str(entry_client_order_id),
    }


def identity_is_sufficient(identity: Any) -> Tuple[bool, Optional[str]]:
    """Identidade mínima para atribuir fills sem adivinhação."""
    ident = identity if isinstance(identity, dict) else {}
    if ident.get("exchange") != "binance":
        return False, "UNSUPPORTED_EXCHANGE"
    if not ident.get("symbol"):
        return False, "IDENTITY_NO_SYMBOL"
    if not str(ident["symbol"]).endswith(SETTLEMENT_ASSET):
        return False, "UNSUPPORTED_SETTLEMENT_ASSET"
    if entry_exit_sides(ident.get("side")) is None:
        return False, "IDENTITY_NO_SIDE"
    if not (ident.get("entry_order_id") or ident.get("entry_client_order_id")):
        return False, "IDENTITY_NO_ENTRY_ORDER"
    return True, None


def attribute_fills(fills: Sequence[Dict[str, Any]], *, identity: Dict[str, Any],
                    entry_order_ids: Sequence[str],
                    exit_order_ids: Sequence[str]) -> Dict[str, Any]:
    """Atribui fills por ORDEM confirmada — nunca por símbolo/lado/horário.

    Um fill do mesmo símbolo que não pertença a uma ordem conhecida da operação
    fica em `unattributed` e torna o resultado AMBÍGUO: operações sobrepostas,
    reversão ou origem incerta exigem estado explicável.
    """
    sides = entry_exit_sides(identity.get("side"))
    symbol = identity.get("symbol")
    position_side = str(identity.get("position_side") or "BOTH").upper()
    entry_ids = {str(o) for o in entry_order_ids if o}
    exit_ids = {str(o) for o in exit_order_ids if o}

    entry: List[Dict[str, Any]] = []
    exits: List[Dict[str, Any]] = []
    unattributed: List[Dict[str, Any]] = []
    foreign = 0

    for fill in fills or ():
        if not isinstance(fill, dict):
            continue
        if fill.get("symbol") != symbol:
            foreign += 1
            continue
        if str(fill.get("position_side") or "BOTH").upper() != position_side:
            foreign += 1
            continue
        order_id = str(fill.get("order_id") or "")
        if order_id in entry_ids:
            if sides and fill.get("side") != sides[0]:
                unattributed.append(fill)      # lado incompatível com a entrada
                continue
            entry.append(fill)
        elif order_id in exit_ids:
            if sides and fill.get("side") != sides[1]:
                unattributed.append(fill)
                continue
            exits.append(fill)
        else:
            unattributed.append(fill)

    return {"entry": entry, "exit": exits, "unattributed": unattributed,
            "foreign_symbol_or_position": foreign}


def close_origin(order: Any) -> str:
    """Origem do fechamento. Nunca conclui autoria humana específica.

    `clientOrderId` com prefixo do bot ⇒ `BOT_MANAGED`. Qualquer outro (ex.:
    app de terceiro) ⇒ `EXTERNAL_OR_UNKNOWN` — sem inventar BE/TP/SL.
    """
    coid = ""
    if isinstance(order, dict):
        coid = str(order.get("client_order_id") or order.get("clientOrderId") or "")
    return (CLOSE_ORIGIN_BOT
            if any(coid.startswith(p) for p in BOT_COID_PREFIXES)
            else CLOSE_ORIGIN_EXTERNAL)


# ════════════════════════════════════════════════════════════════════════════
#  Totais — Decimal exato, ausência nunca vira zero
# ════════════════════════════════════════════════════════════════════════════
def compute_totals(entry_fills: Sequence[Dict[str, Any]],
                   exit_fills: Sequence[Dict[str, Any]],
                   funding: Sequence[Dict[str, Any]], *,
                   funding_state: str = FUNDING_PENDING,
                   fee_conversions: Any = None,
                   fee_context: Any = None,
                   settlement_asset: str = SETTLEMENT_ASSET) -> Dict[str, Any]:
    """Agrega os totais financeiros a partir do conjunto DEDUPLICADO de eventos."""
    seen: set = set()
    gross = Decimal("0")
    # SEM execução atribuída não existe resultado conhecido: conjunto vazio
    # nunca prova P&L zero (ao contrário do funding, cuja janela consultada e
    # vazia É prova de zero).
    gross_known = bool(entry_fills or exit_fills)
    fees: Dict[str, Decimal] = {}
    #: Comissões em ativo DIFERENTE do de liquidação, por fill. A conversão é
    #: por fill (contrato versionado), nunca um rateio do total.
    fees_other_by_fill: List[Dict[str, Any]] = []
    fees_complete = True
    entry_qty = Decimal("0")
    entry_notional = Decimal("0")
    exit_qty = Decimal("0")
    exit_notional = Decimal("0")
    last_exit: Optional[Tuple[str, Decimal]] = None

    for role, group in (("entry", entry_fills), ("exit", exit_fills)):
        for fill in group or ():
            key = fill.get("key") or fill.get("exec_id")
            if key in seen:                     # comissão contada UMA vez por exec
                continue
            seen.add(key)
            price = to_decimal(fill.get("price"), allow_negative=False)
            qty = to_decimal(fill.get("qty"), allow_negative=False)
            if price is None or qty is None:
                gross_known = False
                fees_complete = False
                continue
            realized = to_decimal(fill.get("realized_pnl"))
            if realized is None:
                gross_known = False
            else:
                gross += realized
            commission = to_decimal(fill.get("commission"), allow_negative=False)
            asset = fill.get("commission_asset")
            if commission is None or not asset:
                fees_complete = False           # ausente ≠ zero
            else:
                fees[asset] = fees.get(asset, Decimal("0")) + commission
                # Só comissão estrangeira ESTRITAMENTE POSITIVA exige conversão:
                # zero comprovado é custo zero conhecido em qualquer ativo.
                if asset != settlement_asset and commission > 0:
                    fees_other_by_fill.append(
                        {"fill_key": key, "fee_asset": asset,
                         "fee_qty": commission, "role": role,
                         "time": fill.get("time"),
                         "exec_id": fill.get("exec_id")})
            if role == "entry":
                entry_qty += qty
                entry_notional += price * qty
            else:
                exit_qty += qty
                exit_notional += price * qty
                ts = str(fill.get("time") or "")
                if last_exit is None or ts >= last_exit[0]:
                    last_exit = (ts, price)

    # `net_trade` só existe com gross conhecido E comissões completas na moeda
    # de liquidação. Comissão em BNB/outro ativo NÃO é subtraída nominalmente.
    if not (entry_fills or exit_fills):
        fees_complete = False               # ausência de fill ≠ taxa zero
    # `fees_by_asset` continua informativo (inclusive o zero estrangeiro); a
    # pendência de conversão olha apenas as comissões positivas.
    other_assets = sorted({item["fee_asset"] for item in fees_other_by_fill})
    settlement_fee = fees.get(settlement_asset)
    # Conversão das comissões em OUTRO ativo: só evidência CONFIRMADA por fill
    # entra no dinheiro. ESTIMATED/ausente/conflito mantêm `net_trade` desconhecido.
    conversao = resolve_fee_conversions(
        fees_other_by_fill, fee_conversions, fee_context=fee_context,
        settlement_asset=settlement_asset)
    net_trade: Optional[Decimal] = None
    net_reason: Optional[str] = None
    if not gross_known:
        net_reason = ("NO_ATTRIBUTED_FILL" if not (entry_fills or exit_fills)
                      else "GROSS_INCOMPLETE")
    elif not fees_complete:
        net_reason = "FEES_INCOMPLETE"
    elif other_assets and not conversao["resolved"]:
        net_reason = conversao["reason_code"]
    else:
        net_trade = (gross - (settlement_fee or Decimal("0"))
                     - conversao["converted_total"])
    # Ativos ainda NÃO convertidos: o consumidor do total (R05D) exclui a linha
    # enquanto esta lista não estiver vazia.
    unconverted = [] if conversao["resolved"] else other_assets

    funding_net: Optional[Decimal] = None
    if funding_state == FUNDING_CONFIRMED:
        total = Decimal("0")
        seen_funding: set = set()
        for item in funding or ():
            key = item.get("key")
            if key in seen_funding:
                continue
            seen_funding.add(key)
            if item.get("asset") != settlement_asset:
                funding_net = None
                break
            value = to_decimal(item.get("income"))
            if value is None:
                funding_net = None
                break
            total += value
        else:
            funding_net = total

    net_including = (net_trade + funding_net
                     if (net_trade is not None and funding_net is not None) else None)

    return {
        "gross_realized": _dstr(gross) if gross_known else None,
        "gross_complete": gross_known,
        "fees_by_asset": {a: _dstr(v) for a, v in sorted(fees.items())},
        "fees_complete": fees_complete and not unconverted,
        "fee_assets_unconverted": unconverted,
        "fee_conversion_state": conversao["state"],
        "fee_conversion_reason_code": conversao["reason_code"],
        "fee_conversion_settlement_total": _dstr(conversao["converted_total"])
        if conversao["resolved"] else None,
        "fee_conversion_required": [item["fill_key"] for item in fees_other_by_fill],
        "fee_conversion_confirmed": conversao["confirmed_keys"],
        "settlement_asset": settlement_asset,
        "net_trade": _dstr(net_trade),
        "net_trade_reason_code": net_reason,
        "funding_net": _dstr(funding_net),
        "funding_state": funding_state,
        "net_including_funding": _dstr(net_including),
        "entry_qty_executed": _dstr(entry_qty) if entry_fills else None,
        "exit_qty_executed": _dstr(exit_qty) if exit_fills else None,
        "entry_avg_price": _dstr(entry_notional / entry_qty) if entry_qty > 0 else None,
        "exit_avg_price": _dstr(exit_notional / exit_qty) if exit_qty > 0 else None,
        "last_exit_price": _dstr(last_exit[1]) if last_exit else None,
        "entry_fee": _dstr(_fee_of(entry_fills, settlement_asset)),
        "exit_fee": _dstr(_fee_of(exit_fills, settlement_asset)),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Comissão paga em OUTRO ativo — contrato de conversão VERSIONADO por fill
# ════════════════════════════════════════════════════════════════════════════
#  `FEE_ASSET_CONVERSION_UNAVAILABLE` é um bloqueio CORRETO: BNB não é USDT e
#  não existe 1:1 entre USD/USDT/USDC. Resolvê-lo exige uma EVIDÊNCIA por fill
#  com três conceitos SEPARADOS:
#
#  1. INTEGRIDADE  — `integrity_hash` cobre o registro inteiro (inclusive os
#     metadados de observação). Prova que o objeto não foi alterado; NÃO prova
#     conta, origem nem janela.
#  2. EQUIVALÊNCIA MATERIAL — `material_id` cobre só o que é econômico:
#     identidade esperada, comissão (ativo/quantidade), ativo de liquidação,
#     valor confirmado e a referência MATERIAL do lançamento. Observação
#     (`observed_*`, `now`, tentativa) fica FORA: reobservar o mesmo fato não é
#     conflito econômico.
#  3. PROGRESSO ÚTIL — uma comissão EXIGIDA que passou de não confirmada a
#     confirmada. Mais objetos no JSON não é progresso.
#
#  Qualidade é DERIVADA da fonte: `BROKER_REGISTERED_CONVERSION` pode confirmar;
#  `HISTORICAL_MARKET_PRICE` é no máximo ESTIMATED (preço atual não é histórico).
#  Sem vínculo inequívoco da fonte primária ⇒ `BLOCKED_SOURCE_UNAVAILABLE`.
FEE_CONVERSION_CONTRACT = "R05E_FEE_CONVERSION_V2"
#: Contratos ANTERIORES: ficam preservados para diagnóstico e NUNCA são
#: promovidos (não se recalcula hash de prova antiga para validá-la).
FEE_CONVERSION_LEGACY_CONTRACTS = ("R05E_FEE_CONVERSION_V1",)
#: Versão da FONTE do lançamento (endpoint + contrato de leitura).
LEDGER_SOURCE = "BINANCE_USDM_INCOME_V1"
FEE_SOURCE_BROKER = "BROKER_REGISTERED_CONVERSION"
FEE_SOURCE_HISTORICAL = "HISTORICAL_MARKET_PRICE"
FEE_SOURCES = (FEE_SOURCE_BROKER, FEE_SOURCE_HISTORICAL)
FEE_QUALITY_CONFIRMED = "CONFIRMED"
FEE_QUALITY_ESTIMATED = "ESTIMATED"
FEE_QUALITY_UNAVAILABLE = "UNAVAILABLE"
FEE_CONVERSION_RESOLVED = "RESOLVED"
FEE_CONVERSION_NOT_REQUIRED = "NOT_REQUIRED"
FEE_BLOCKED_SOURCE = "BLOCKED_SOURCE_UNAVAILABLE"
FEE_REASON_MISSING = "FEE_ASSET_CONVERSION_UNAVAILABLE"
FEE_REASON_ESTIMATED = "FEE_CONVERSION_ESTIMATED_ONLY"
FEE_REASON_CONFLICT = "FEE_CONVERSION_CONFLICT"
FEE_REASON_INVALID = "FEE_CONVERSION_INVALID"
FEE_REASON_NO_CONTEXT = "FEE_CONVERSION_CONTEXT_MISSING"
FEE_REASON_UNVERIFIED = "FEE_CONVERSION_UNVERIFIED_LEGACY"
#: Desfechos do coletor: conclusão, progresso, incompletude SAUDÁVEL por lote,
#: ausência de fonte e erro real são coisas diferentes.
FEE_COLLECT_COMPLETE = "COMPLETE"
FEE_COLLECT_PROGRESS = "PROGRESS"
FEE_COLLECT_PARTIAL_LIMIT = "PARTIAL_LIMIT"
FEE_COLLECT_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
FEE_COLLECT_ERROR = "ERROR"
#: Recusas do lançamento do ledger (uma por incompatibilidade).
LEDGER_NOT_MAPPING = "LEDGER_ROW_NOT_MAPPING"
LEDGER_TYPE_MISMATCH = "LEDGER_INCOME_TYPE_MISMATCH"
LEDGER_ASSET_MISMATCH = "LEDGER_ASSET_MISMATCH"
LEDGER_SYMBOL_MISMATCH = "LEDGER_SYMBOL_MISMATCH"
LEDGER_TRADE_ID_INVALID = "LEDGER_TRADE_ID_INVALID"
LEDGER_TRADE_ID_UNKNOWN = "LEDGER_TRADE_ID_NOT_ATTRIBUTED"
LEDGER_TRAN_ID_INVALID = "LEDGER_TRAN_ID_INVALID"
LEDGER_INCOME_INVALID = "LEDGER_INCOME_INVALID"
LEDGER_CREDIT_UNSUPPORTED = "LEDGER_CREDIT_NOT_SUPPORTED"
LEDGER_TIME_INVALID = "LEDGER_TIME_INVALID"
LEDGER_TIME_OUT_OF_WINDOW = "LEDGER_TIME_OUT_OF_WINDOW"
LEDGER_LINK_AMBIGUOUS = "LEDGER_FOREIGN_LINK_NOT_UNAMBIGUOUS"
LEDGER_AMBIGUOUS_FOR_FILL = "LEDGER_MULTIPLE_EVENTS_FOR_FILL"
LEDGER_SOURCE_CONFLICT = "LEDGER_SOURCE_CONFLICT"
#: Campos do CONTEXTO ESPERADO — derivado da identidade contábil e do fill
#: REALMENTE ATRIBUÍDO. Nunca copiado da própria prova.
FEE_CONTEXT_FIELDS = (
    "account_scope", "exchange", "market", "symbol", "quote", "position_side",
    "fill_key", "exec_id", "fill_time_ms", "commission_asset",
    "commission_qty", "settlement_asset",
)
#: Campos de VALOR (canonicalizados como decimal venha string ou número).
FEE_DECIMAL_FIELDS = ("fee_qty", "settlement_value", "price")
#: Campos cobertos pelo hash de INTEGRIDADE.
FEE_INTEGRITY_FIELDS = (
    "contract_version", "account_scope", "exchange", "market", "symbol",
    "quote", "position_side", "fill_key", "exec_id", "fee_asset", "fee_qty",
    "settlement_asset", "settlement_value", "price", "price_basis",
    "fill_time_ms", "source", "quality", "observed_start_ms",
    "observed_end_ms", "source_ref", "material_id",
)
#: Campos ECONÔMICOS (equivalência material). Sem observação/now/tentativa.
FEE_MATERIAL_FIELDS = (
    "contract_version", "account_scope", "exchange", "market", "symbol",
    "position_side", "fill_key", "exec_id", "fee_asset", "fee_qty",
    "settlement_asset", "settlement_value",
)
#: `quality` NÃO entra na materialidade: promover estimativa a confirmação é
#: transição de QUALIDADE (tratada pela precedência), não dinheiro diferente.
#: `observed_*`/`observed_at_ms`/tentativa também não — são da OBSERVAÇÃO.
#: Referência material do lançamento (identidade do EVENTO, não da consulta).
LEDGER_MATERIAL_FIELDS = ("source", "account_scope", "exchange", "symbol",
                          "income_type", "trade_id", "tran_id", "asset",
                          "income", "time_ms")
#: Teto de histórico material preservado por comissão.
FEE_HISTORY_LIMIT = 4


def _fee_canonical(value: Any) -> Optional[str]:
    """Forma canônica e estável no ida-e-volta do JSONB."""
    if value is None:
        return None
    if isinstance(value, bool):
        return f"bool:{'true' if value else 'false'}"
    if isinstance(value, (int, float, Decimal)):
        numero = to_decimal(value)
        if numero is None:
            return None
        return f"num:{format(numero.normalize(), 'f')}"
    if isinstance(value, str):
        texto = value.strip()
        return f"str:{texto}" if texto else None
    if isinstance(value, Mapping):
        return {str(k): _fee_canonical(value[k]) for k in sorted(map(str, value))}
    if isinstance(value, (list, tuple)):
        return [_fee_canonical(item) for item in value]
    return None


def _fee_digest(evidence: Mapping, campos: Sequence[str]) -> Optional[str]:
    corpo = {}
    for campo in campos:
        valor = evidence.get(campo)
        if campo in FEE_DECIMAL_FIELDS and not isinstance(valor, bool):
            numero = to_decimal(valor)
            corpo[campo] = (f"num:{format(numero.normalize(), 'f')}"
                            if numero is not None else None)
        else:
            corpo[campo] = _fee_canonical(valor)
    try:
        texto = json.dumps(corpo, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def fee_conversion_hash(evidence: Any) -> Optional[str]:
    """Hash de INTEGRIDADE: o registro inteiro, metadados inclusive."""
    if not isinstance(evidence, Mapping):
        return None
    return _fee_digest(evidence, FEE_INTEGRITY_FIELDS)


def _fee_price_coherent(valor: Decimal, preco: Decimal,
                        quantidade: Decimal) -> bool:
    """`valor ≈ preço × quantidade` com Decimal (tolerância relativa)."""
    try:
        erro = abs(valor - (preco * quantidade))
    except (InvalidOperation, ArithmeticError):
        return False
    return erro <= (abs(valor) * Decimal("0.000001") + Decimal("0.00000001"))


def fee_conversion_material_id(evidence: Any) -> Optional[str]:
    """Identidade ECONÔMICA: muda só quando a comissão/valor/evento mudam."""
    if not isinstance(evidence, Mapping):
        return None
    base = dict(evidence)
    referencia = evidence.get("source_ref")
    base["source_ref_material"] = (
        {campo: referencia.get(campo) for campo in LEDGER_MATERIAL_FIELDS}
        if isinstance(referencia, Mapping) else None)
    return _fee_digest(base, FEE_MATERIAL_FIELDS + ("source_ref_material",))


def build_expected_context(*, identity: Any, fill: Any,
                           settlement_asset: str = SETTLEMENT_ASSET
                           ) -> Optional[Dict[str, Any]]:
    """Contexto ESPERADO de UMA comissão, da identidade + fill ATRIBUÍDO.

    Devolve None quando falta qualquer peça verificável: sem contexto não se
    confirma comissão estrangeira (e `None` nunca vira fail-open).
    """
    if not isinstance(identity, Mapping) or not isinstance(fill, Mapping):
        return None
    quantidade = to_decimal(fill.get("commission"), allow_negative=False)
    ativo = fill.get("commission_asset")
    simbolo = normalize_symbol(fill.get("symbol")) or normalize_symbol(
        identity.get("symbol"))
    contexto = {
        "account_scope": identity.get("account_scope"),
        "exchange": str(identity.get("exchange") or "").lower() or None,
        "market": "usdm_futures",
        "symbol": simbolo,
        "quote": (str(settlement_asset).upper() if simbolo else None),
        "position_side": fill.get("position_side") or identity.get("position_side"),
        "fill_key": fill.get("key"),
        "exec_id": _id_str(fill.get("exec_id")),
        "fill_time_ms": _ms(fill.get("time")),
        "commission_asset": (str(ativo).strip().upper() if ativo else None),
        "commission_qty": quantidade,
        "settlement_asset": str(settlement_asset).strip().upper(),
    }
    faltando = [campo for campo in FEE_CONTEXT_FIELDS
                if contexto.get(campo) in (None, "")]
    if faltando:
        return None
    if contexto["commission_asset"] == contexto["settlement_asset"]:
        return None                 # não é comissão estrangeira
    return contexto


def _fee_context_matches(evidence: Mapping, expected: Mapping) -> Optional[str]:
    """A prova descreve EXATAMENTE a comissão esperada? Motivo ou None."""
    textuais = ("account_scope", "exchange", "market", "symbol", "quote",
                "position_side", "fill_key", "exec_id", "settlement_asset")
    for campo in textuais:
        gravado = evidence.get(campo)
        esperado = expected.get(campo)
        if gravado is None or str(gravado) != str(esperado):
            return f"{campo}_divergente"
    if _ms(evidence.get("fill_time_ms")) != _ms(expected.get("fill_time_ms")):
        return "fill_time_divergente"
    if str(evidence.get("fee_asset") or "") != str(expected.get("commission_asset")):
        return "fee_asset_divergente"
    gravada = to_decimal(evidence.get("fee_qty"), allow_negative=False)
    esperada = to_decimal(expected.get("commission_qty"), allow_negative=False)
    if gravada is None or esperada is None or gravada != esperada:
        return "fee_qty_divergente"
    return None


def normalize_commission_ledger_row(raw: Any, *, account_scope: Any,
                                    exchange: Any, symbol: Any,
                                    settlement_asset: str,
                                    window_start_ms: Any, window_end_ms: Any,
                                    exec_ids: Any,
                                    expected_by_exec: Any = None
                                    ) -> Tuple[Optional[Dict[str, Any]],
                                               Optional[str]]:
    """Normaliza e VALIDA um lançamento do ledger ANTES de indexar valor.

    O filtro do request não substitui a validação do response: tipo, ativo,
    símbolo, identificadores, sinal e janela são conferidos aqui. `income`
    positivo é CRÉDITO e não vira custo por `abs`. `account_scope`/`exchange`
    vêm da consulta JÁ verificada pelo cliente, não da linha.

    O vínculo com a comissão ESTRANGEIRA precisa ser inequívoco: a linha tem de
    declarar o ativo e a quantidade da comissão daquele fill. A documentação de
    income da Binance não garante esse vínculo, então na prática a ausência dele
    mantém `BLOCKED_SOURCE_UNAVAILABLE` — e nenhuma estimativa entra como
    confirmação.
    """
    if not isinstance(raw, Mapping):
        return None, LEDGER_NOT_MAPPING
    tipo = str(raw.get("incomeType") or raw.get("income_type") or "").strip()
    if tipo != "COMMISSION":
        return None, LEDGER_TYPE_MISMATCH
    ativo = str(raw.get("asset") or "").strip().upper()
    if ativo != str(settlement_asset).strip().upper():
        return None, LEDGER_ASSET_MISMATCH
    simbolo_linha = normalize_symbol(raw.get("symbol"))
    if not simbolo_linha or simbolo_linha != normalize_symbol(symbol):
        return None, LEDGER_SYMBOL_MISMATCH
    bruto_trade = (raw.get("tradeId") if raw.get("tradeId") is not None
                   else raw.get("trade_id"))
    if isinstance(bruto_trade, bool) or isinstance(bruto_trade, float):
        return None, LEDGER_TRADE_ID_INVALID
    trade_id = _id_str(bruto_trade)
    if not trade_id:
        return None, LEDGER_TRADE_ID_INVALID
    if trade_id not in {str(e) for e in (exec_ids or ())}:
        return None, LEDGER_TRADE_ID_UNKNOWN
    bruto_tran = (raw.get("tranId") if raw.get("tranId") is not None
                  else raw.get("tran_id"))
    if isinstance(bruto_tran, bool) or isinstance(bruto_tran, float):
        return None, LEDGER_TRAN_ID_INVALID
    tran_id = _id_str(bruto_tran)
    if not tran_id:
        return None, LEDGER_TRAN_ID_INVALID
    valor = to_decimal(raw.get("income"))
    if valor is None:
        return None, LEDGER_INCOME_INVALID
    if valor > 0:
        # Crédito não é custo. Não é descontado nem invertido por `abs`.
        return None, LEDGER_CREDIT_UNSUPPORTED
    instante = _ms(raw.get("time"))
    inicio, fim = _ms(window_start_ms), _ms(window_end_ms)
    if instante is None or inicio is None or fim is None:
        return None, LEDGER_TIME_INVALID
    if not inicio <= instante <= fim:
        return None, LEDGER_TIME_OUT_OF_WINDOW
    # Vínculo EXPLÍCITO com a comissão estrangeira do fill atribuído.
    contexto = (expected_by_exec or {}).get(trade_id)
    if not isinstance(contexto, Mapping):
        return None, LEDGER_TRADE_ID_UNKNOWN
    ativo_vinculo = str(raw.get("commissionAsset")
                        or raw.get("commission_asset") or "").strip().upper()
    qty_vinculo = to_decimal(raw.get("commission"), allow_negative=False)
    if not ativo_vinculo or qty_vinculo is None:
        return None, LEDGER_LINK_AMBIGUOUS
    if ativo_vinculo != str(contexto.get("commission_asset")):
        return None, LEDGER_LINK_AMBIGUOUS
    if qty_vinculo != to_decimal(contexto.get("commission_qty"),
                                 allow_negative=False):
        return None, LEDGER_LINK_AMBIGUOUS
    return {
        "source": LEDGER_SOURCE,
        "account_scope": str(account_scope),
        "exchange": str(exchange).lower(),
        "symbol": simbolo_linha,
        "income_type": tipo,
        "trade_id": trade_id,
        "tran_id": tran_id,
        "asset": ativo,
        "income": _dstr(valor),
        "time_ms": instante,
        "window_start_ms": inicio,
        "window_end_ms": fim,
    }, None


def index_commission_ledger(rows: Sequence[Any], **kwargs
                            ) -> Tuple[Dict[str, Dict[str, Any]],
                                       Dict[str, str]]:
    """Indexa lançamentos VÁLIDOS por `trade_id`, com dedupe por identidade.

    - mesmo `tran_id` e conteúdo material idêntico ⇒ no-op;
    - mesmo `tran_id` com conteúdo material diferente ⇒ conflito de fonte;
    - `tran_id` distintos para o MESMO fill ⇒ ambíguo e BLOQUEADO (não se
      escolhe último/maior/menor nem se soma).
    """
    por_trade: Dict[str, Dict[str, Any]] = {}
    bloqueados: Dict[str, str] = {}
    vistos: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for bruta in rows or ():
        referencia, motivo = normalize_commission_ledger_row(bruta, **kwargs)
        if referencia is None:
            continue
        trade_id = referencia["trade_id"]
        chave = (trade_id, referencia["tran_id"])
        anterior = vistos.get(chave)
        if anterior is not None:
            material_a = {c: anterior.get(c) for c in LEDGER_MATERIAL_FIELDS}
            material_b = {c: referencia.get(c) for c in LEDGER_MATERIAL_FIELDS}
            if material_a != material_b:
                bloqueados[trade_id] = LEDGER_SOURCE_CONFLICT
                por_trade.pop(trade_id, None)
            continue                                   # duplicata real: no-op
        vistos[chave] = referencia
        if trade_id in bloqueados:
            continue
        if trade_id in por_trade:
            # Outro lançamento, mesmo fill: semântica não é inequívoca.
            bloqueados[trade_id] = LEDGER_AMBIGUOUS_FOR_FILL
            por_trade.pop(trade_id, None)
            continue
        por_trade[trade_id] = referencia
    return por_trade, bloqueados


def build_fee_conversion(*, expected: Any, settlement_value: Any, price: Any,
                         source: Any, observed_start_ms: Any,
                         observed_end_ms: Any, source_ref: Any = None,
                         price_basis: Any = None,
                         now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Evidência de conversão de UMA comissão, amarrada ao contexto esperado.

    Recusa explícita (sem número) quando: contexto insuficiente, fonte
    desconhecida, valores não finitos/negativos, preço ≤ 0, janela incoerente ou
    que NÃO contém o instante do fill, carimbo no futuro. Qualidade é derivada
    da fonte; `BROKER_REGISTERED_CONVERSION` exige `source_ref` normalizada.
    """
    agora_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if not isinstance(expected, Mapping):
        return {"ok": False, "reason_code": FEE_REASON_NO_CONTEXT,
                "detail": "contexto esperado ausente"}
    faltando = [c for c in FEE_CONTEXT_FIELDS if expected.get(c) in (None, "")]
    if faltando:
        return {"ok": False, "reason_code": FEE_REASON_NO_CONTEXT,
                "detail": f"contexto incompleto: {','.join(faltando)}"}
    fonte = str(source or "").strip().upper()
    if fonte not in FEE_SOURCES:
        return {"ok": False, "reason_code": FEE_BLOCKED_SOURCE,
                "detail": f"fonte desconhecida: {fonte or 'ausente'}"}
    quantidade = to_decimal(expected.get("commission_qty"), allow_negative=False)
    if quantidade is None or quantidade <= 0:
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "comissão estrangeira não é estritamente positiva"}
    if str(expected.get("commission_asset")).strip().upper() == \
            str(expected.get("settlement_asset")).strip().upper():
        # Comissão já liquidada no ativo de liquidação não tem conversão: uma
        # "prova de conversão" aqui seria um número sem fato econômico.
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "comissão já está no ativo de liquidação"}
    valor = to_decimal(settlement_value, allow_negative=False)
    preco = to_decimal(price, allow_negative=False)
    if valor is None or preco is None or preco <= 0:
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "valor/preço não finitos ou não positivos"}
    instante = _ms(expected.get("fill_time_ms"))
    inicio, fim = _ms(observed_start_ms), _ms(observed_end_ms)
    if instante is None or inicio is None or fim is None:
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "carimbos do fill/janela ausentes ou inválidos"}
    if inicio > fim or fim > agora_ms + 2_000 or instante > agora_ms + 2_000:
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "janela/instante incoerentes ou no futuro"}
    if not inicio <= instante <= fim:
        # Janela que não CONTÉM o fill não observou aquela comissão.
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "janela observada não contém o instante do fill"}
    qualidade = (FEE_QUALITY_CONFIRMED if fonte == FEE_SOURCE_BROKER
                 else FEE_QUALITY_ESTIMATED)
    referencia = None
    if isinstance(source_ref, Mapping):
        referencia = {c: source_ref.get(c) for c in
                      LEDGER_MATERIAL_FIELDS + ("window_start_ms",
                                                "window_end_ms")}
    if qualidade == FEE_QUALITY_CONFIRMED:
        if not referencia or referencia.get("source") != LEDGER_SOURCE:
            return {"ok": False, "reason_code": FEE_BLOCKED_SOURCE,
                    "detail": "confirmação exige referência normalizada da fonte"}
        if str(referencia.get("trade_id")) != str(expected.get("exec_id")):
            return {"ok": False, "reason_code": FEE_REASON_INVALID,
                    "detail": "referência da fonte não é do fill esperado"}
        if str(referencia.get("account_scope")) != str(expected.get("account_scope")) \
                or str(referencia.get("exchange")) != str(expected.get("exchange")) \
                or normalize_symbol(referencia.get("symbol")) != expected.get("symbol"):
            return {"ok": False, "reason_code": FEE_REASON_INVALID,
                    "detail": "referência da fonte de outra conta/símbolo"}
        registrado = to_decimal(referencia.get("income"))
        if registrado is None or -registrado != valor:
            # O número confirmado é o da FONTE. Nada de valor próprio apoiado
            # num lançamento que diz outra coisa.
            return {"ok": False, "reason_code": FEE_REASON_INVALID,
                    "detail": "valor não confere com o lançamento da fonte"}
    if not _fee_price_coherent(valor, preco, quantidade):
        # `price` é REPRESENTAÇÃO do valor liquidado por unidade de comissão,
        # não uma prova independente.
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "preço incoerente com valor/quantidade"}
    evidencia = {"contract_version": FEE_CONVERSION_CONTRACT}
    for campo in FEE_CONTEXT_FIELDS:
        if campo in ("commission_asset", "commission_qty"):
            continue
        evidencia[campo] = expected.get(campo)
    evidencia.update({
        "fee_asset": str(expected.get("commission_asset")),
        "fee_qty": _dstr(quantidade),
        "settlement_value": _dstr(valor), "price": _dstr(preco),
        "price_basis": (str(price_basis) if price_basis else None),
        "source": fonte, "quality": qualidade,
        "observed_start_ms": inicio, "observed_end_ms": fim,
        "observed_at_ms": agora_ms, "source_ref": referencia,
    })
    evidencia["material_id"] = fee_conversion_material_id(evidencia)
    evidencia["integrity_hash"] = fee_conversion_hash(evidencia)
    if not evidencia["material_id"] or not evidencia["integrity_hash"]:
        return {"ok": False, "reason_code": FEE_REASON_INVALID,
                "detail": "evidência não canonicalizável"}
    evidencia["ok"] = True
    return evidencia


def fee_conversion_verdict(evidence: Any, *, expected: Any) -> Dict[str, Any]:
    """A prova persistida confirma ESTA comissão, e com que qualidade?

    Integridade, contexto exato, vínculo da referência com o fill e coerência de
    valor/preço. Hash íntegro não basta: contexto diferente não é dinheiro.
    """
    if not isinstance(expected, Mapping) or \
            [c for c in FEE_CONTEXT_FIELDS if expected.get(c) in (None, "")]:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_NO_CONTEXT}
    if not isinstance(evidence, Mapping):
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_MISSING}
    versao = evidence.get("contract_version")
    if versao in FEE_CONVERSION_LEGACY_CONTRACTS:
        # Preservada para diagnóstico; nunca promovida nem re-hasheada.
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_UNVERIFIED}
    if versao != FEE_CONVERSION_CONTRACT:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": f"contrato desconhecido: {versao}"}
    integridade = evidence.get("integrity_hash")
    if not isinstance(integridade, str) or \
            fee_conversion_hash(evidence) != integridade:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_CONFLICT}
    material = evidence.get("material_id")
    if not isinstance(material, str) or \
            fee_conversion_material_id(evidence) != material:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_CONFLICT}
    divergencia = _fee_context_matches(evidence, expected)
    if divergencia is not None:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID, "detail": divergencia}
    if str(evidence.get("fee_asset")).strip().upper() == \
            str(expected.get("settlement_asset")).strip().upper():
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": "comissão já está no ativo de liquidação"}
    valor = to_decimal(evidence.get("settlement_value"), allow_negative=False)
    preco = to_decimal(evidence.get("price"), allow_negative=False)
    quantidade = to_decimal(evidence.get("fee_qty"), allow_negative=False)
    if valor is None or preco is None or quantidade is None or quantidade <= 0 \
            or preco <= 0:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": "valor/preço/quantidade não utilizáveis"}
    inicio = _ms(evidence.get("observed_start_ms"))
    fim = _ms(evidence.get("observed_end_ms"))
    instante = _ms(evidence.get("fill_time_ms"))
    if inicio is None or fim is None or instante is None \
            or not inicio <= instante <= fim:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": "janela observada não contém o fill"}
    if not _fee_price_coherent(valor, preco, quantidade):
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": "preço incoerente com valor/quantidade"}
    qualidade = str(evidence.get("quality") or "")
    fonte = str(evidence.get("source") or "")
    if qualidade == FEE_QUALITY_CONFIRMED:
        referencia = evidence.get("source_ref")
        if fonte != FEE_SOURCE_BROKER or not isinstance(referencia, Mapping):
            return {"quality": FEE_QUALITY_UNAVAILABLE,
                    "reason_code": FEE_REASON_INVALID,
                    "detail": "CONFIRMED sem fonte de corretora/referência"}
        if referencia.get("source") != LEDGER_SOURCE \
                or str(referencia.get("trade_id")) != str(expected.get("exec_id")) \
                or str(referencia.get("account_scope")) != str(expected.get("account_scope")) \
                or str(referencia.get("income_type")) != "COMMISSION":
            return {"quality": FEE_QUALITY_UNAVAILABLE,
                    "reason_code": FEE_REASON_INVALID,
                    "detail": "referência da fonte incompatível"}
        registrado = to_decimal(referencia.get("income"))
        if registrado is None or -registrado != valor:
            return {"quality": FEE_QUALITY_UNAVAILABLE,
                    "reason_code": FEE_REASON_INVALID,
                    "detail": "valor não confere com o lançamento da fonte"}
    elif qualidade != FEE_QUALITY_ESTIMATED:
        return {"quality": FEE_QUALITY_UNAVAILABLE,
                "reason_code": FEE_REASON_INVALID,
                "detail": f"qualidade inesperada: {qualidade or 'ausente'}"}
    return {"quality": qualidade, "reason_code": None,
            "settlement_value": valor, "source": fonte,
            "material_id": material}


def merge_fee_proof(current: Any, incoming: Any, *, expected: Any
                    ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Precedência PURA entre duas provas da MESMA comissão.

    Devolve `(prova_escolhida, desfecho)`. Desfechos: `STORED`, `IDEMPOTENT`,
    `ENRICHED` (reobservação equivalente), `PROMOTED` (estimativa → confirmação),
    `KEPT_CONFIRMED` (estimativa atrasada), `CONFLICT` (confirmadas
    materialmente divergentes) e `REJECTED` (incoming inválido/estrangeiro).
    """
    atual = current if isinstance(current, Mapping) else None
    novo_veredito = fee_conversion_verdict(incoming, expected=expected)
    if novo_veredito["quality"] == FEE_QUALITY_UNAVAILABLE:
        return (dict(atual) if atual else None), "REJECTED"
    novo = dict(incoming)
    if atual is None:
        return novo, "STORED"
    atual_veredito = fee_conversion_verdict(atual, expected=expected)
    if atual_veredito["quality"] == FEE_QUALITY_UNAVAILABLE:
        # A guardada não é utilizável (legado/ inválida): a nova válida assume,
        # preservando a anterior como histórico de diagnóstico.
        return _fee_with_history(novo, atual), "STORED"
    mesmo_material = (atual.get("material_id") == novo.get("material_id"))
    if atual_veredito["quality"] == FEE_QUALITY_CONFIRMED:
        if novo_veredito["quality"] == FEE_QUALITY_ESTIMATED:
            return dict(atual), "KEPT_CONFIRMED"
        if mesmo_material:
            return dict(atual), "IDEMPOTENT"
        return dict(atual), "CONFLICT"
    # Atual é ESTIMATED.
    if novo_veredito["quality"] == FEE_QUALITY_CONFIRMED:
        return _fee_with_history(novo, atual), "PROMOTED"
    if mesmo_material:
        return dict(atual), "IDEMPOTENT"
    return _fee_with_history(novo, atual), "ENRICHED"


def _fee_with_history(escolhida: Mapping, anterior: Mapping) -> Dict[str, Any]:
    """Preserva o material anterior, sem anexar objetos indefinidamente."""
    saida = dict(escolhida)
    historico = list(anterior.get("superseded") or [])
    historico.append({campo: anterior.get(campo) for campo in
                      ("quality", "source", "settlement_value", "price",
                       "material_id", "observed_at_ms")})
    saida["superseded"] = historico[-FEE_HISTORY_LIMIT:]
    return saida


def _fee_conversions_required(acc: Any, *,
                              settlement_asset: Optional[str] = None
                              ) -> List[Dict[str, Any]]:
    """Comissões ESTRANGEIRAS ESTRITAMENTE POSITIVAS dos fills atribuídos.

    Zero comprovado não exige conversão; ausente/inválida é desconhecida (e
    derruba a completude em `compute_totals`, não aqui).
    """
    if not isinstance(acc, Mapping):
        return []
    liquidacao = str(settlement_asset or acc.get("settlement_asset")
                     or SETTLEMENT_ASSET).upper()
    identidade = acc.get("identity") or {}
    saida: List[Dict[str, Any]] = []
    for chave, fill in sorted((acc.get("fills") or {}).items()):
        if not isinstance(fill, Mapping):
            continue
        ativo = fill.get("commission_asset")
        comissao = to_decimal(fill.get("commission"), allow_negative=False)
        if not ativo or comissao is None or comissao <= 0 \
                or str(ativo).upper() == liquidacao:
            continue
        contexto = build_expected_context(identity=identidade, fill=fill,
                                          settlement_asset=liquidacao)
        saida.append({"fill_key": chave, "fee_asset": str(ativo).upper(),
                      "fee_qty": comissao, "time": fill.get("time"),
                      "exec_id": fill.get("exec_id"), "expected": contexto})
    return saida


def fee_expected_contexts(acc: Any, *, settlement_asset: Optional[str] = None
                          ) -> Dict[str, Dict[str, Any]]:
    """`fill_key → contexto esperado` das comissões estrangeiras exigidas."""
    return {item["fill_key"]: item["expected"]
            for item in _fee_conversions_required(
                acc, settlement_asset=settlement_asset)
            if item.get("expected")}


def fee_confirmed_keys(acc: Any, *, fee_context: Any = None,
                       settlement_asset: Optional[str] = None) -> set:
    """Comissões exigidas com prova CONFIRMADA e VÁLIDA agora."""
    contextos = (fee_context if isinstance(fee_context, Mapping)
                 else fee_expected_contexts(acc, settlement_asset=settlement_asset))
    guardadas = (acc or {}).get("fee_conversions") or {}
    confirmadas = set()
    for chave, contexto in contextos.items():
        veredito = fee_conversion_verdict(guardadas.get(chave),
                                          expected=contexto)
        if veredito["quality"] == FEE_QUALITY_CONFIRMED:
            confirmadas.add(chave)
    return confirmadas


def resolve_fee_conversions(required: Sequence[Dict[str, Any]], stored: Any, *,
                            fee_context: Any = None,
                            settlement_asset: str = SETTLEMENT_ASSET
                            ) -> Dict[str, Any]:
    """Todas as comissões estrangeiras positivas estão CONFIRMADAS por fill?"""
    if not required:
        return {"resolved": True, "state": FEE_CONVERSION_NOT_REQUIRED,
                "reason_code": None, "converted_total": Decimal("0"),
                "confirmed_keys": []}
    guardadas = stored if isinstance(stored, Mapping) else {}
    contextos = fee_context if isinstance(fee_context, Mapping) else {}
    total = Decimal("0")
    confirmados: List[str] = []
    motivos: List[str] = []
    for item in required:
        chave = str(item.get("fill_key"))
        contexto = contextos.get(chave) or item.get("expected")
        veredito = fee_conversion_verdict(guardadas.get(chave),
                                          expected=contexto)
        if veredito["quality"] == FEE_QUALITY_CONFIRMED:
            total += veredito["settlement_value"]
            confirmados.append(chave)
        elif veredito["quality"] == FEE_QUALITY_ESTIMATED:
            motivos.append(FEE_REASON_ESTIMATED)
        else:
            motivos.append(str(veredito.get("reason_code") or FEE_REASON_MISSING))
    if len(confirmados) == len(required):
        return {"resolved": True, "state": FEE_CONVERSION_RESOLVED,
                "reason_code": None, "converted_total": total,
                "confirmed_keys": sorted(confirmados)}
    prioridade = (FEE_REASON_CONFLICT, FEE_REASON_INVALID,
                  FEE_REASON_NO_CONTEXT, FEE_REASON_UNVERIFIED,
                  FEE_REASON_ESTIMATED, FEE_BLOCKED_SOURCE, FEE_REASON_MISSING)
    motivo = next((m for m in prioridade if m in motivos), FEE_REASON_MISSING)
    # O consumidor do total precisa de um motivo ESTÁVEL de bloqueio.
    if motivo in (FEE_REASON_NO_CONTEXT, FEE_REASON_UNVERIFIED,
                  FEE_BLOCKED_SOURCE):
        motivo = FEE_REASON_MISSING
    return {"resolved": False, "state": FEE_QUALITY_UNAVAILABLE,
            "reason_code": motivo, "converted_total": Decimal("0"),
            "confirmed_keys": sorted(confirmados)}


def _fee_of(fills: Sequence[Dict[str, Any]], asset: str) -> Optional[Decimal]:
    """Comissão informativa de um lado, na moeda de liquidação. Ausente ⇒ None."""
    total = Decimal("0")
    seen: set = set()
    found = False
    for fill in fills or ():
        key = fill.get("key") or fill.get("exec_id")
        if key in seen:
            continue
        seen.add(key)
        commission = to_decimal(fill.get("commission"), allow_negative=False)
        if commission is None or fill.get("commission_asset") != asset:
            return None
        total += commission
        found = True
    return total if found else None


def realized_r_from_net(net_trade: Any, entry_fill_price: Any, planned_stop: Any,
                        qty_initial: Any) -> Tuple[Optional[str], Optional[str]]:
    """`net_trade / (|entry_fill − planned_stop| × qty_initial)`.

    Usa a quantidade INICIAL executada, nunca a restante. Denominador inválido
    devolve `None` + motivo — jamais um R inventado.
    """
    net = to_decimal(net_trade)
    entry = to_decimal(entry_fill_price, allow_negative=False)
    stop = to_decimal(planned_stop, allow_negative=False)
    qty = to_decimal(qty_initial, allow_negative=False)
    if net is None:
        return None, "NET_TRADE_UNKNOWN"
    if entry is None or entry <= 0:
        return None, "ENTRY_FILL_UNKNOWN"
    if stop is None or stop <= 0:
        return None, "PLANNED_STOP_UNKNOWN"
    if qty is None or qty <= 0:
        return None, "QTY_INITIAL_UNKNOWN"
    risk = abs(entry - stop) * qty
    if risk <= 0:
        return None, "RISK_DENOMINATOR_ZERO"
    return _dstr(net / risk), None


# ════════════════════════════════════════════════════════════════════════════
#  Merge idempotente — dedupe, CONFLICT e monotonicidade
# ════════════════════════════════════════════════════════════════════════════
_MATERIAL_FILL_FIELDS = ("order_id", "side", "symbol", "position_side", "time", "price", "qty", "realized_pnl",
                         "commission", "commission_asset")


def _conflicts(old: Dict[str, Any], new: Dict[str, Any],
               fields: Sequence[str]) -> List[str]:
    """Campos materialmente divergentes. `None` → valor não é conflito."""
    out = []
    for field in fields:
        prev, cur = old.get(field), new.get(field)
        if prev is None or cur is None:
            continue
        if field in ("price", "qty", "realized_pnl", "commission", "income") and to_decimal(prev) == to_decimal(cur):
            continue
        if str(prev) != str(cur):
            out.append(field)
    return out


def empty_accounting(*, identity: Optional[Dict[str, Any]] = None,
                     state: str = STATE_PENDING,
                     reason_code: Optional[str] = None) -> Dict[str, Any]:
    """Contabilidade inicial de um trade NOVO — nasce no contrato R05C."""
    return {
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "reason_code": reason_code,
        "source": SOURCE_BINANCE,
        "settlement_asset": SETTLEMENT_ASSET,
        "identity": identity or {},
        "orders": {},
        "fills": {},
        "funding": {},
        #: Evidência de conversão de comissão em outro ativo, por `fill_key`.
        "fee_conversions": {},
        "funding_state": FUNDING_PENDING,
        "coverage": {},
        "totals": {},
        "close_origin": None,
        "provisional": True,
        "attempts": 0,
        "next_retry_at": None,
        "last_error": None,
        "conflicts": [],
        "updated_at": None,
        #: Geração da linha, avançada SOB BLOQUEIO a cada observação aplicada.
        #: Uma resposta coletada sobre geração anterior ainda contribui eventos,
        #: mas não tem autoridade sobre estatística de retry.
        "generation": 0,
        "last_observation_id": None,
    }


def legacy_accounting(reason: str = "registro anterior ao R05C") -> Dict[str, Any]:
    """`NULL` em registro antigo é LEGACY_UNVERIFIED — nunca confirmação."""
    acc = empty_accounting(state=STATE_LEGACY, reason_code="LEGACY_UNVERIFIED")
    acc["provisional"] = False
    acc["last_error"] = None
    acc["detail"] = reason
    return acc


def merge_accounting(previous: Any, *, identity: Optional[Dict[str, Any]] = None,
                     fills: Sequence[Dict[str, Any]] = (),
                     funding: Sequence[Dict[str, Any]] = (),
                     orders: Sequence[Dict[str, Any]] = (),
                     fee_conversions: Sequence[Dict[str, Any]] = (),
                     fee_context: Any = None,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """Funde eventos novos no acumulado. IDEMPOTENTE e sem regressão.

    O mesmo `exec_id` reaparecendo com conteúdo idêntico é no-op; com conteúdo
    MATERIALMENTE divergente vira `CONFLICT` — nunca sobrescrita silenciosa.
    Respostas fora de ordem e reinício não perdem evento nem duplicam valor:
    os acumulados são sempre RECALCULADOS do conjunto deduplicado.
    """
    acc = copy.deepcopy(previous) if isinstance(previous, dict) else empty_accounting()
    if acc.get("schema_version") != SCHEMA_VERSION:
        acc = empty_accounting(identity=acc.get("identity") if isinstance(acc, dict) else None)
    acc.setdefault("fills", {})
    acc.setdefault("funding", {})
    acc.setdefault("orders", {})
    acc.setdefault("fee_conversions", {})
    acc.setdefault("conflicts", [])
    conflicts: List[Dict[str, Any]] = list(acc.get("conflicts") or [])
    if identity:
        old_identity = acc.get("identity") or {}
        different = [k for k, v in identity.items()
                     if v is not None and old_identity.get(k) is not None
                     and old_identity[k] != v]
        if different:
            conflicts.append({"kind": "IDENTITY", "fields": sorted(different)})
        else:
            acc["identity"] = {**old_identity, **{k: v for k, v in identity.items() if v is not None}}
    stored_fills: Dict[str, Any] = dict(acc["fills"])
    for fill in fills or ():
        if not isinstance(fill, dict):
            continue
        key = fill.get("key") or fill.get("exec_id")
        if not key:
            continue
        prev = stored_fills.get(key)
        if prev is None:
            stored_fills[key] = fill
            continue
        diverging = _conflicts(prev, fill, _MATERIAL_FILL_FIELDS)
        if diverging:
            conflicts.append({"kind": "FILL", "key": key, "fields": diverging})
            continue                            # preserva o primeiro confirmado
        stored_fills[key] = {**prev, **{k: v for k, v in fill.items() if v is not None}}
    acc["fills"] = stored_fills

    stored_funding: Dict[str, Any] = dict(acc["funding"])
    for item in funding or ():
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        prev = stored_funding.get(key)
        if prev is not None:
            diverging = _conflicts(prev, item, ("income", "asset"))
            if diverging:
                conflicts.append({"kind": "FUNDING", "key": key, "fields": diverging})
                continue
        stored_funding[key] = item
    acc["funding"] = stored_funding

    stored_orders: Dict[str, Any] = dict(acc["orders"])
    for order in orders or ():
        if not isinstance(order, dict):
            continue
        oid = order.get("order_id")
        if not oid:
            continue
        old = stored_orders.get(oid) or {}
        # A delayed NEW/partial response cannot erase terminal proof. Identity
        # changes are conflicts, never a new attribution for the same order.
        different = _conflicts(old, order, ("symbol", "side", "position_side", "client_order_id"))
        if different:
            conflicts.append({"kind": "ORDER", "key": oid, "fields": different})
            continue
        old_qty = to_decimal(old.get("executed_qty")) or Decimal(0)
        new_qty = to_decimal(order.get("executed_qty")) or Decimal(0)
        if old.get("status") in TERMINAL_ORDER_STATES and (order.get("status") not in TERMINAL_ORDER_STATES or new_qty < old_qty):
            continue
        stored_orders[oid] = {**old, **{k: v for k, v in order.items() if v is not None}}
    acc["orders"] = stored_orders

    stored_fees: Dict[str, Any] = dict(acc["fee_conversions"])
    contextos = (dict(fee_context) if isinstance(fee_context, Mapping)
                 else fee_expected_contexts(acc))
    desfechos: Dict[str, str] = {}
    for evidencia in fee_conversions or ():
        if not isinstance(evidencia, Mapping):
            continue
        chave = evidencia.get("fill_key")
        if not chave:
            continue
        limpa = {k: v for k, v in evidencia.items() if k != "ok"}
        escolhida, desfecho = merge_fee_proof(
            stored_fees.get(str(chave)), limpa,
            expected=contextos.get(str(chave)))
        desfechos[str(chave)] = desfecho
        if desfecho == "CONFLICT":
            # Confirmadas materialmente divergentes: original preservada e a
            # contraprova registrada no diagnóstico.
            conflicts.append({"kind": "FEE_CONVERSION", "key": str(chave),
                              "fields": ["material_id"],
                              "rejected_material_id": limpa.get("material_id")})
        if escolhida is not None:
            stored_fees[str(chave)] = escolhida
    acc["fee_conversions"] = stored_fees
    if desfechos:
        acc["fee_merge_outcomes"] = desfechos

    acc["conflicts"] = [c for i, c in enumerate(conflicts) if c not in conflicts[:i]]
    acc["updated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return acc


TERMINAL_ORDER_STATES = frozenset({"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"})


def _order_proof(order, identity, side, fills):
    """Exact order, market, side and cumulative quantity; ACK is not a fill."""
    if not isinstance(order, dict) or order.get("status") not in TERMINAL_ORDER_STATES:
        return False
    qty = to_decimal(order.get("executed_qty"))
    observed = sum((to_decimal(f.get("qty")) or Decimal(0) for f in fills), Decimal(0))
    return (order.get("symbol") == identity.get("symbol")
            and order.get("position_side") == identity.get("position_side")
            and order.get("side") == side and qty is not None and qty > 0
            and abs(qty - observed) <= _QTY_TOLERANCE)


def finalize_accounting(acc: Dict[str, Any], *,
                        entry_order_ids: Sequence[str] = (),
                        exit_order_ids: Sequence[str] = (),
                        fills_window_complete: bool = False,
                        funding_window_complete: bool = False,
                        position_flat: Optional[bool] = None,
                        planned_stop: Any = None,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    """Recalcula cobertura, totais e ESTADO a partir do conjunto deduplicado."""
    out = dict(acc)
    identity = out.get("identity") or {}
    ok_identity, identity_reason = identity_is_sufficient(identity)

    all_fills = list((out.get("fills") or {}).values())
    attribution = attribute_fills(
        all_fills, identity=identity,
        entry_order_ids=entry_order_ids, exit_order_ids=exit_order_ids)

    funding_items = list((out.get("funding") or {}).values())
    funding_window_complete = bool(funding_window_complete and not attribution["unattributed"]
                                   and not out.get("conflicts"))
    funding_state = (FUNDING_CONFIRMED if funding_window_complete
                     else FUNDING_PENDING)
    contexto_taxas = fee_expected_contexts(out)
    totals = compute_totals(attribution["entry"], attribution["exit"],
                            funding_items, funding_state=funding_state,
                            fee_conversions=out.get("fee_conversions"),
                            fee_context=contexto_taxas)

    entry_qty = to_decimal(totals.get("entry_qty_executed")) or Decimal("0")
    exit_qty = to_decimal(totals.get("exit_qty_executed")) or Decimal("0")
    balanced = bool(entry_qty > 0 and abs(entry_qty - exit_qty) <= _QTY_TOLERANCE)
    orders = out.get("orders") or {}
    sides = entry_exit_sides(identity.get("side")) or (None, None)
    entry_proven = bool(attribution["entry"]) and all(
        _order_proof(orders.get(oid), identity, sides[0],
                     [f for f in attribution["entry"] if f["order_id"] == oid])
        for oid in {f["order_id"] for f in attribution["entry"]})
    exit_proven = bool(attribution["exit"]) and all(
        orders.get(oid, {}).get("reduce_only") is True
        and orders.get(oid, {}).get("role") == "exit"
        and _order_proof(orders.get(oid), identity, sides[1],
                         [f for f in attribution["exit"] if f["order_id"] == oid])
        for oid in {f["order_id"] for f in attribution["exit"]})

    out["coverage"] = {
        "entry_fills": len(attribution["entry"]),
        "exit_fills": len(attribution["exit"]),
        "unattributed_fills": len(attribution["unattributed"]),
        "foreign_fills": attribution["foreign_symbol_or_position"],
        "entry_qty_executed": totals.get("entry_qty_executed"),
        "exit_qty_executed": totals.get("exit_qty_executed"),
        "quantity_balanced": balanced,
        "entry_order_proven": entry_proven,
        "exit_orders_proven": exit_proven,
        "fills_window_complete": bool(fills_window_complete),
        "funding_window_complete": bool(funding_window_complete),
        "position_flat": position_flat,
    }
    out["totals"] = totals
    out["funding_state"] = (funding_state if funding_items or funding_window_complete
                            else FUNDING_PENDING)

    r_value, r_reason = realized_r_from_net(
        totals.get("net_trade"), totals.get("entry_avg_price"), planned_stop,
        totals.get("entry_qty_executed"))
    out["realized_r"] = r_value
    out["realized_r_reason_code"] = r_reason

    # ── Estado contábil: precedência explícita, sem otimismo ────────────────
    if out.get("conflicts"):
        state, reason = STATE_CONFLICT, "EXEC_ID_CONTENT_CONFLICT"
    elif not ok_identity:
        state, reason = STATE_PENDING, identity_reason
    elif attribution["unattributed"]:
        state, reason = STATE_AMBIGUOUS, "UNATTRIBUTED_FILLS"
    elif not attribution["entry"]:
        state, reason = STATE_PENDING, "NO_ENTRY_FILL"
    elif not entry_proven:
        state, reason = STATE_PARTIAL, "ENTRY_ORDER_NOT_PROVEN"
    elif not fills_window_complete:
        state, reason = STATE_PARTIAL, "FILLS_WINDOW_INCOMPLETE"
    elif not balanced:
        state, reason = STATE_PARTIAL, "QUANTITY_NOT_CONSERVED"
    elif not exit_proven:
        state, reason = STATE_PARTIAL, "EXIT_ORDERS_NOT_PROVEN"
    elif position_flat is False:
        state, reason = STATE_PARTIAL, "POSITION_STILL_OPEN"
    elif totals.get("net_trade") is None:
        state, reason = STATE_PARTIAL, totals.get("net_trade_reason_code")
    else:
        state, reason = STATE_CONFIRMED, None
    out["state"] = state
    out["reason_code"] = reason
    if state != STATE_CONFIRMED:
        out["funding_state"] = FUNDING_PENDING
        out["coverage"]["funding_window_complete"] = False
        out["totals"] = compute_totals(attribution["entry"], attribution["exit"],
                                       funding_items, funding_state=FUNDING_PENDING,
                                       fee_conversions=out.get("fee_conversions"),
                                       fee_context=contexto_taxas)
    out["provisional"] = state not in (STATE_CONFIRMED,)
    out["updated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return out


def schedule_retry(acc: Dict[str, Any], *, error: Optional[str] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Backoff PERSISTIDO e finito. Exaustão fica visível, sem retry infinito."""
    out = dict(acc)
    attempts = int(out.get("attempts") or 0) + 1
    out["attempts"] = attempts
    out["last_error"] = (str(error)[:200] if error else None)
    ts = now or datetime.now(timezone.utc)
    if attempts >= MAX_ATTEMPTS:
        out["next_retry_at"] = None
        if out.get("state") not in (STATE_CONFIRMED, STATE_CONFLICT):
            out["state"] = STATE_FAILED
            out["reason_code"] = "RETRY_BUDGET_EXHAUSTED"
    else:
        delay = RETRY_BACKOFF_S[min(attempts - 1, len(RETRY_BACKOFF_S) - 1)]
        out["next_retry_at"] = (ts + timedelta(seconds=delay)).isoformat()
    return out


def is_retry_due(acc: Any, *, now: Optional[datetime] = None) -> bool:
    """Só registros ADERENTES ao schema novo e ainda pendentes são elegíveis."""
    if not isinstance(acc, dict) or acc.get("schema_version") != SCHEMA_VERSION:
        return False                             # legado nunca é varrido
    if acc.get("state") in (STATE_FAILED, STATE_CONFLICT,
                            STATE_LEGACY):
        return False
    if acc.get("state") == STATE_CONFIRMED:
        if acc.get("funding_state") == FUNDING_CONFIRMED:
            return False
        if int(acc.get("funding_attempts") or 0) >= MAX_ATTEMPTS:
            return False
    if acc.get("state") != STATE_CONFIRMED and int(acc.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False
    nxt = acc.get("next_retry_at")
    if not nxt:
        return True
    try:
        due = datetime.fromisoformat(str(nxt))
    except (TypeError, ValueError):
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= due


# ════════════════════════════════════════════════════════════════════════════
#  Projeção para as colunas legadas do `RealTrade`
# ════════════════════════════════════════════════════════════════════════════
def project_to_trade_fields(acc: Any) -> Dict[str, Any]:
    """PnL só CONFIRMED; metadados de entrada exigem ordem terminal comprovada.

    `pnl_usd` mantém a semântica legada: líquido de execuções e comissões,
    EXCLUINDO funding. Só é projetado quando `net_trade` é conhecido — nunca
    zero para "encerrar". Precisão total fica no JSON; aqui vai o arredondamento
    legado de 4 casas.
    """
    if not isinstance(acc, dict) or acc.get("schema_version") != SCHEMA_VERSION:
        return {}
    if acc.get("state") == STATE_LEGACY:
        return {}
    if not accounting_is_confirmed(acc):
        # Partial cumulative amounts remain in the ledger, not in the public
        # closed-trade result. A conflict invalidates a previously published P&L.
        fields = {"pnl_usd": None, "pnl_pct": None, "realized_r": None}
        # Entry proof may be complete while the position is still OPEN. These
        # are actual-entry metadata, not a finalized/partial profit claim.
        if (acc.get("coverage") or {}).get("entry_order_proven") is True and not acc.get("conflicts"):
            totals = acc.get("totals") or {}
            for field, key in (("entry_price", "entry_avg_price"), ("qty_initial", "entry_qty_executed"), ("entry_fee", "entry_fee")):
                value = to_decimal(totals.get(key), allow_negative=False)
                if value is not None:
                    fields[field] = float(value)
        return fields
    totals = acc.get("totals") or {}
    out: Dict[str, Any] = {}

    entry_avg = to_decimal(totals.get("entry_avg_price"), allow_negative=False)
    if entry_avg is not None and entry_avg > 0:
        out["entry_price"] = float(entry_avg)
    qty_initial = to_decimal(totals.get("entry_qty_executed"), allow_negative=False)
    if qty_initial is not None and qty_initial > 0:
        out["qty_initial"] = float(qty_initial)
    exit_avg = to_decimal(totals.get("exit_avg_price"), allow_negative=False)
    if exit_avg is not None and exit_avg > 0:
        out["exit_price"] = float(exit_avg)

    net = to_decimal(totals.get("net_trade"))
    if net is not None:
        out["pnl_usd"] = float(round(net, 4))
    entry_fee = to_decimal(totals.get("entry_fee"), allow_negative=False)
    if entry_fee is not None:
        out["entry_fee"] = float(entry_fee)
    exit_fee = to_decimal(totals.get("exit_fee"), allow_negative=False)
    if exit_fee is not None:
        out["exit_fee"] = float(exit_fee)

    r_value = to_decimal(acc.get("realized_r"))
    if r_value is not None:
        out["realized_r"] = float(round(r_value, 3))
    if net is not None and entry_avg and qty_initial and entry_avg * qty_initial > 0:
        out["pnl_pct"] = float(round(net / (entry_avg * qty_initial) * 100, 4))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Coleta — SOMENTE GET, janelas explícitas, orçamento por ciclo
# ════════════════════════════════════════════════════════════════════════════
MAX_TRADE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000     # userTrades: janelas <= 7 dias
USER_TRADES_PAGE_LIMIT = 1000
INCOME_PAGE_LIMIT = 1000
MAX_CALLS_PER_TRADE = 12                          # orçamento por operação
_WINDOW_PAD_MS = 60_000                           # folga p/ fills na borda


def _ms(value: Any) -> Optional[int]:
    """Epoch em ms a partir de datetime/str/int. Inválido ⇒ `None`."""
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(ts.timestamp() * 1000)
    dec = to_decimal(value, allow_negative=False)
    return int(dec) if dec is not None else None


def plan_windows(start_ms: int, end_ms: int) -> List[Tuple[int, int]]:
    """Fatia [start, end] em janelas de no máximo 7 dias, sem buraco."""
    if end_ms < start_ms:
        return []
    out: List[Tuple[int, int]] = []
    cursor = start_ms
    while cursor <= end_ms:
        stop = min(cursor + MAX_TRADE_WINDOW_MS - 1, end_ms)
        out.append((cursor, stop))
        cursor = stop + 1
    return out


async def _collect_fills(client: Any, symbol: str, start_ms: int, end_ms: int,
                         budget: List[int]) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Fills do intervalo. Página CHEIA não prova completude — devolve parcial.

    A janela seguinte começa no mesmo milissegundo do último fill para não
    perder execuções com timestamp idêntico.
    """
    rows: List[Dict[str, Any]] = []
    complete = True
    for win_start, win_end in plan_windows(start_ms, end_ms):
        cursor = win_start
        while cursor <= win_end:
            if budget[0] <= 0:
                return rows, False, "CALL_BUDGET_EXHAUSTED"
            budget[0] -= 1
            res = await client.get_executions(symbol, limit=USER_TRADES_PAGE_LIMIT,
                                              start_time=cursor, end_time=win_end)
            if not res.get("ok"):
                return rows, False, str(res.get("error") or res.get("msg") or "USER_TRADES_ERROR")
            page = res.get("raw")
            if page is None:
                page = [f.get("raw") for f in (res.get("fills") or [])]
            if not isinstance(page, list) or any(not isinstance(p, dict) for p in page):
                return rows, False, "INVALID_FILL_PAGE"
            rows.extend(page)
            if len(page) < int(res.get("limit") or USER_TRADES_PAGE_LIMIT):
                break                              # página curta encerra a janela
            times = [_ms(p.get("time")) for p in page]
            times = [t for t in times if t is not None]
            if not times:
                complete = False
                break
            nxt = max(times)
            if nxt <= cursor:                      # todos no mesmo ms: não avança
                complete = False
                break
            cursor = nxt                           # inclusivo: preserva o mesmo ms
    return rows, complete, None


async def _collect_funding(client: Any, symbol: str, start_ms: int, end_ms: int,
                           budget: List[int]) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Lançamentos `FUNDING_FEE` do intervalo. Sem evidência ⇒ incompleto."""
    getter = getattr(client, "get_income", None)
    if getter is None:
        return [], False, "INCOME_ENDPOINT_UNAVAILABLE"
    rows: List[Dict[str, Any]] = []
    for win_start, win_end in plan_windows(start_ms, end_ms):
        cursor = win_start
        while cursor <= win_end:
            if budget[0] <= 0:
                return rows, False, "CALL_BUDGET_EXHAUSTED"
            budget[0] -= 1
            res = await getter(symbol, income_type="FUNDING_FEE",
                               start_time=cursor, end_time=win_end,
                               limit=INCOME_PAGE_LIMIT)
            if not res.get("ok"):
                return rows, False, str(res.get("error") or res.get("msg") or "INCOME_ERROR")
            page = res.get("income")
            if not isinstance(page, list) or any(not isinstance(p, dict) for p in page):
                return rows, False, "INVALID_INCOME_PAGE"
            rows.extend(page)
            if len(page) < int(res.get("limit") or INCOME_PAGE_LIMIT):
                break
            times = [_ms(p.get("time")) for p in page]
            times = [t for t in times if t is not None]
            if not times:
                return rows, False, "INCOME_PAGE_WITHOUT_TIME"
            nxt = max(times)
            if nxt <= cursor:
                return rows, False, "INCOME_PAGE_SAME_TIMESTAMP"
            cursor = nxt
    return rows, True, None


#: Teto de comissões em outro ativo resolvidas por PASSE (lote limitado). A
#: incompletude saudável por este limite NÃO é falha da fonte.
MAX_FEE_CONVERSIONS_PER_TRADE = 8


async def collect_broker_fee_conversions(
        client: Any, symbol: str, required: Sequence[Dict[str, Any]],
        *, identity: Dict[str, Any], start_ms: int, end_ms: int,
        budget: List[int], now_ms: Optional[int] = None,
        settlement_asset: str = SETTLEMENT_ASSET) -> Dict[str, Any]:
    """Conversão REGISTRADA pela corretora, por fill, via ledger de COMMISSION.

    UMA varredura paginada da janela cobre o lote inteiro (sem N+1) e reaproveita
    o cliente de leitura, a paginação e o orçamento existentes. Cada linha passa
    pelo normalizador ANTES de qualquer indexação de valor; página cheia, cursor
    parado, erro ou fonte incompleta NUNCA viram completude.

    Devolve um desfecho explícito: `COMPLETE` (todas as exigências resolvidas),
    `PROGRESS` (avançou, falta lote), `PARTIAL_LIMIT` (limite do passe),
    `SOURCE_UNAVAILABLE` (fonte sem vínculo inequívoco) e `ERROR` (erro real).
    `complete` considera TODAS as exigências, não só os primeiros oito itens.
    """
    exigidas = [item for item in (required or ())]
    if not exigidas:
        return {"state": FEE_COLLECT_COMPLETE, "evidences": [],
                "pending": [], "complete": True, "error": None,
                "attempted": 0}
    lote = exigidas[:MAX_FEE_CONVERSIONS_PER_TRADE]
    restantes = [str(i.get("fill_key")) for i in
                 exigidas[MAX_FEE_CONVERSIONS_PER_TRADE:]]
    contextos = {str(i.get("exec_id")): i.get("expected") for i in lote
                 if i.get("expected")}
    if not contextos:
        return {"state": FEE_COLLECT_SOURCE_UNAVAILABLE, "evidences": [],
                "pending": [str(i.get("fill_key")) for i in exigidas],
                "complete": False, "error": FEE_REASON_NO_CONTEXT,
                "attempted": len(lote)}
    getter = getattr(client, "get_income", None)
    if getter is None:
        return {"state": FEE_COLLECT_ERROR, "evidences": [],
                "pending": [str(i.get("fill_key")) for i in exigidas],
                "complete": False, "error": "INCOME_ENDPOINT_UNAVAILABLE",
                "attempted": len(lote)}
    linhas: List[Any] = []
    for win_start, win_end in plan_windows(start_ms, end_ms):
        cursor = win_start
        while cursor <= win_end:
            if budget[0] <= 0:
                return {"state": FEE_COLLECT_ERROR, "evidences": [],
                        "pending": [str(i.get("fill_key")) for i in exigidas],
                        "complete": False, "error": "CALL_BUDGET_EXHAUSTED",
                        "attempted": len(lote)}
            budget[0] -= 1
            res = await getter(symbol, income_type="COMMISSION",
                               start_time=cursor, end_time=win_end,
                               limit=INCOME_PAGE_LIMIT)
            if not res.get("ok"):
                return {"state": FEE_COLLECT_ERROR, "evidences": [],
                        "pending": [str(i.get("fill_key")) for i in exigidas],
                        "complete": False,
                        "error": str(res.get("error") or res.get("msg")
                                     or "INCOME_ERROR"),
                        "attempted": len(lote)}
            pagina = res.get("income")
            if not isinstance(pagina, list) or \
                    any(not isinstance(p, dict) for p in pagina):
                return {"state": FEE_COLLECT_ERROR, "evidences": [],
                        "pending": [str(i.get("fill_key")) for i in exigidas],
                        "complete": False, "error": "INVALID_INCOME_PAGE",
                        "attempted": len(lote)}
            linhas.extend(pagina)
            if len(pagina) < int(res.get("limit") or INCOME_PAGE_LIMIT):
                break
            tempos = [t for t in (_ms(p.get("time")) for p in pagina)
                      if t is not None]
            if not tempos:
                return {"state": FEE_COLLECT_ERROR, "evidences": [],
                        "pending": [str(i.get("fill_key")) for i in exigidas],
                        "complete": False, "error": "INCOME_PAGE_WITHOUT_TIME",
                        "attempted": len(lote)}
            proximo = max(tempos)
            if proximo <= cursor:
                return {"state": FEE_COLLECT_ERROR, "evidences": [],
                        "pending": [str(i.get("fill_key")) for i in exigidas],
                        "complete": False,
                        "error": "INCOME_PAGE_SAME_TIMESTAMP",
                        "attempted": len(lote)}
            cursor = proximo
    por_trade, bloqueados = index_commission_ledger(
        linhas, account_scope=identity.get("account_scope"),
        exchange=identity.get("exchange") or "binance", symbol=symbol,
        settlement_asset=settlement_asset, window_start_ms=start_ms,
        window_end_ms=end_ms, exec_ids=set(contextos),
        expected_by_exec=contextos)
    evidencias: List[Dict[str, Any]] = []
    pendentes: List[str] = list(restantes)
    motivos: List[str] = []
    for item in lote:
        chave = str(item.get("fill_key"))
        exec_id = _id_str(item.get("exec_id"))
        contexto = item.get("expected")
        referencia = por_trade.get(exec_id) if exec_id else None
        if exec_id in bloqueados:
            pendentes.append(chave)
            motivos.append(bloqueados[exec_id])
            continue
        quantidade = to_decimal((contexto or {}).get("commission_qty"),
                               allow_negative=False)
        if referencia is None or contexto is None or quantidade is None \
                or quantidade <= 0:
            pendentes.append(chave)
            # Ausência de lançamento compatível (ou linha recusada pelo
            # normalizador) é FONTE INDISPONÍVEL — nunca estimativa. O motivo
            # específico de cada recusa fica em `ledger_reasons`.
            motivos.append(FEE_BLOCKED_SOURCE if referencia is None
                           else FEE_REASON_INVALID)
            continue
        valor = to_decimal(referencia.get("income"))
        if valor is None or valor > 0:
            pendentes.append(chave)
            motivos.append(LEDGER_INCOME_INVALID)
            continue
        liquidado = -valor                      # custo registrado pela fonte
        evidencia = build_fee_conversion(
            expected=contexto, settlement_value=liquidado,
            price=(liquidado / quantidade),
            price_basis="COMMISSION_LEDGER_SETTLEMENT_VALUE",
            source=FEE_SOURCE_BROKER, source_ref=referencia,
            observed_start_ms=start_ms, observed_end_ms=end_ms, now_ms=now_ms)
        if evidencia.get("ok") is not True:
            pendentes.append(chave)
            motivos.append(str(evidencia.get("reason_code")))
            continue
        evidencias.append(evidencia)
    if not pendentes:
        estado = FEE_COLLECT_COMPLETE
    elif evidencias:
        estado = (FEE_COLLECT_PARTIAL_LIMIT
                  if set(pendentes) <= set(restantes) else FEE_COLLECT_PROGRESS)
    elif restantes and not motivos:
        estado = FEE_COLLECT_PARTIAL_LIMIT
    else:
        estado = FEE_COLLECT_SOURCE_UNAVAILABLE
    erro = None
    if estado == FEE_COLLECT_SOURCE_UNAVAILABLE:
        erro = motivos[0] if motivos else FEE_BLOCKED_SOURCE
    return {"state": estado, "evidences": evidencias,
            "pending": sorted(set(pendentes)), "complete": not pendentes,
            "error": erro, "attempted": len(lote),
            "ledger_reasons": sorted(set(motivos))}


class ReadBudget:
    """One shared deadline/request budget for the whole existing manager batch."""
    def __init__(self, seconds=2.0, calls=8):
        self.deadline = time.monotonic() + seconds
        self.calls = calls
        self.stopped = False

    async def call(self, getter, *args, **kwargs):
        remaining = self.deadline - time.monotonic()
        if self.stopped or self.calls <= 0 or remaining <= 0:
            raise TimeoutError("ACCOUNTING_BUDGET_EXHAUSTED")
        self.calls -= 1
        result = await asyncio.wait_for(getter(*args, **kwargs), timeout=remaining)
        if isinstance(result, dict) and (result.get("status_code") in (418, 429)
                or result.get("code") in (-1003, -1015)
                or any(x in str(result.get("error") or "").lower()
                       for x in ("rate", "banned", "429", "418"))):
            self.stopped = True
        return result


class _BudgetClient:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    def __getattr__(self, name):
        getter = getattr(self.client, name)
        async def bounded(*args, **kwargs):
            return await self.budget.call(getter, *args, **kwargs)
        return bounded


def _identity_for_view(view):
    identity = build_identity(exchange=view.get("exchange"), symbol=view.get("symbol"),
                              side=view.get("side"), entry_order_id=view.get("exchange_order_id"),
                              entry_client_order_id=view.get("client_order_id"),
                              position_side=view.get("position_side") or "BOTH")
    stored = (view.get("execution_accounting") or {}).get("identity") or {}
    for key in ("account_scope",):
        if stored.get(key):
            identity[key] = stored[key]
    return identity


def _retry_after_observation(acc, previous, *, error=None, funding_error=None,
                            fee_progress=False, fee_state=None, now):
    out = dict(acc)
    # PROGRESSO ÚTIL: fill/ordem novos OU comissão exigida que passou a
    # confirmada. Mais objetos no JSON, janela atualizada ou prova duplicada não
    # contam.
    progress = (len(out.get("fills") or {}) > len((previous or {}).get("fills") or {})
                or len(out.get("orders") or {}) > len((previous or {}).get("orders") or {})
                or bool(fee_progress))
    if progress:
        out["attempts"] = 0
    if out.get("state") == STATE_CONFIRMED:
        out["attempts"] = 0
        out["last_error"] = None
        if out.get("funding_state") == FUNDING_CONFIRMED:
            out["next_retry_at"] = None
            out["funding_attempts"] = 0
        else:
            n = int(out.get("funding_attempts") or 0) + (1 if funding_error else 0)
            out["funding_attempts"] = n
            out["funding_last_error"] = funding_error
            out["next_retry_at"] = (now + timedelta(seconds=RETRY_BACKOFF_S[min(n, 5)])).isoformat()
        return out
    if out.get("state") == STATE_CONFLICT:
        out["next_retry_at"] = None
        return out
    # Incompletude SAUDÁVEL pelo limite do lote (ou progresso útil no passe) é
    # espera cadenciada, nunca uma tentativa falhada.
    if fee_state in (FEE_COLLECT_PARTIAL_LIMIT, FEE_COLLECT_PROGRESS) \
            or (progress and fee_state == FEE_COLLECT_COMPLETE):
        out["next_retry_at"] = (now + timedelta(seconds=60)).isoformat()
        out["last_error"] = None
        return out
    if error and error not in ("TimeoutError", "CALL_BUDGET_EXHAUSTED"):
        return schedule_retry(out, error=error, now=now)
    # A healthy open position, slow endpoint or exhausted per-tick budget is
    # waiting, not six failed trades. Retry is paced without exhausting recovery.
    out["next_retry_at"] = (now + timedelta(seconds=60)).isoformat()
    out["last_error"] = error
    return out


#: Campos de identidade que participam do `observation_id`.
_OBSERVATION_IDENTITY = ("account_scope", "exchange", "symbol", "position_side",
                         "entry_order_id")


def stamp_observation(acc: Dict[str, Any], *, base_generation: int,
                      observed_at: Any) -> Dict[str, Any]:
    """Metadado ADITIVO da observação: identidade estável + geração-base.

    `observation_id` é DETERMINÍSTICO sobre o conteúdo observado: reaplicar a
    MESMA resposta devolve o mesmo id (replay reconhecível, sem contar falha
    nem reiniciar contador duas vezes); duas coletas distintas diferem.
    `base_generation` é a geração lida ANTES da coleta — é ela, não o
    `updated_at`, que decide autoridade sobre estatística de retry.
    """
    identidade = acc.get("identity") or {}
    corpo = {
        "base": int(base_generation),
        "observed_at": str(observed_at),
        "identity": {k: identidade.get(k) for k in _OBSERVATION_IDENTITY},
        "state": acc.get("state"), "reason_code": acc.get("reason_code"),
        "last_error": acc.get("last_error"),
        "fills": sorted(acc.get("fills") or {}),
        "orders": sorted(acc.get("orders") or {}),
        "funding": sorted(acc.get("funding") or {}),
        "fees": sorted(str((e or {}).get("material_id") or chave)
                       for chave, e in (acc.get("fee_conversions") or {}).items()),
    }
    out = dict(acc)
    out["base_generation"] = int(base_generation)
    out["observation_id"] = hashlib.sha256(
        json.dumps(corpo, sort_keys=True, default=str,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    return out


async def collect_trade_accounting(trade_view: Dict[str, Any], *,
                                   client: Any = None, budget=None,
                                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Only bounded GETs; persist partial observations, never partial public P&L."""
    ts_now = now or datetime.now(timezone.utc)
    previous = trade_view.get("execution_accounting") or {}
    identity = _identity_for_view(trade_view)
    # Geração da linha ANTES de coletar: o merge transacional usa isto para
    # saber se esta resposta ainda fala da linha que ela observou.
    base_generation = int(previous.get("generation") or 0)

    def _selar(resultado: Dict[str, Any]) -> Dict[str, Any]:
        return stamp_observation(resultado, base_generation=base_generation,
                                 observed_at=ts_now.isoformat())

    acc = merge_accounting(previous, identity=identity, now=ts_now)
    ok, reason = identity_is_sufficient(identity)
    if not ok:
        acc.update(state=STATE_PENDING, reason_code=reason)
        return _selar(schedule_retry(acc, error=reason, now=ts_now))
    if client is None:
        from services import exchange_service
        from services import binance_signed_service
        if exchange_service.ACTIVE_EXCHANGE != "binance":
            acc.update(state=STATE_PENDING, reason_code="ACTIVE_EXCHANGE_MISMATCH")
            return _selar(schedule_retry(acc, error="ACTIVE_EXCHANGE_MISMATCH", now=ts_now))
        client = binance_signed_service
    scope = getattr(client, "accounting_scope", lambda: None)()
    if not identity.get("account_scope") or scope != identity["account_scope"]:
        acc.update(state=STATE_PENDING, reason_code="ACCOUNT_SCOPE_UNVERIFIED")
        return _selar(schedule_retry(acc, error="ACCOUNT_SCOPE_UNVERIFIED", now=ts_now))
    client = _BudgetClient(client, budget or ReadBudget())
    opened_ms, closed_ms = _ms(trade_view.get("opened_at")), _ms(trade_view.get("closed_at"))
    if opened_ms is None:
        acc.update(state=STATE_PENDING, reason_code="OPENED_AT_UNKNOWN")
        return _selar(schedule_retry(acc, error="OPENED_AT_UNKNOWN", now=ts_now))
    if opened_ms > _ms(ts_now) or (closed_ms is not None and (closed_ms < opened_ms or closed_ms > _ms(ts_now))):
        acc.update(state=STATE_PENDING, reason_code="TRADE_WINDOW_INVALID")
        return _selar(schedule_retry(acc, error="TRADE_WINDOW_INVALID", now=ts_now))
    end_ms = min(closed_ms + _WINDOW_PAD_MS, _ms(ts_now)) if closed_ms else _ms(ts_now)
    start_ms = opened_ms - _WINDOW_PAD_MS
    local_budget = [MAX_CALLS_PER_TRADE]
    errors = []
    # Window proofs are reusable only for this exact operational close. An open
    # observation never proves completeness of a later closure.
    window_key = str(closed_ms) if closed_ms else None
    proof = acc.get("execution_proof") or {}
    fills_complete = bool(accounting_is_confirmed(previous) and window_key and proof.get("window_key") == window_key
                          and proof.get("complete") is True)
    if not fills_complete:
        try:
            raws, fills_complete, err = await _collect_fills(client, identity["symbol"],
                                                           start_ms, end_ms, local_budget)
        except Exception as exc:
            raws, fills_complete, err = [], False, type(exc).__name__
        if err:
            errors.append(err)
        fills = []
        for raw in raws:
            fill, why = normalize_fill(raw, exchange="binance")
            if fill is None or fill["symbol"] != identity["symbol"] or not start_ms <= int(fill["time"]) <= end_ms:
                fills_complete = False
                errors.append("INVALID_FILL_COVERAGE")
            else:
                fills.append(fill)
        acc = merge_accounting(acc, fills=fills, now=ts_now)
        # Persist each completed page/window observation even if the subsequent
        # order/funding request times out.
        acc["execution_proof"] = {"window_key": window_key, "complete": fills_complete,
                                  "observed_at": ts_now.isoformat()}
    orders = acc.get("orders") or {}
    entry_id = identity.get("entry_order_id")
    existing = orders.get(entry_id) or {}
    if not entry_id or existing.get("status") not in TERMINAL_ORDER_STATES:
        try:
            kwargs = {"order_id": entry_id} if entry_id else {"client_order_id": identity["entry_client_order_id"]}
            res = await client.get_order(identity["symbol"], **kwargs)
            order = normalize_order(res.get("raw") or {}) if res.get("ok") is True else None
            if (order and order["symbol"] == identity["symbol"]
                    and order["side"] == entry_exit_sides(identity["side"])[0]
                    and (not entry_id or order["order_id"] == entry_id)
                    and (not identity.get("entry_client_order_id")
                         or order["client_order_id"] == identity["entry_client_order_id"])):
                entry_id = order["order_id"]
                identity = {**identity, "entry_order_id": entry_id}
                acc = merge_accounting(acc, identity=identity, orders=[{**order, "role": "entry"}], now=ts_now)
            else:
                errors.append("ENTRY_ORDER_LOOKUP_UNVERIFIED")
        except Exception as exc:
            errors.append(type(exc).__name__)
    fills = list((acc.get("fills") or {}).values())
    # Conditional algo IDs are NOT normal order IDs. Never attribute by numeric
    # coincidence. Each actual closing order requires GET and exclusivity proof.
    exclusive = trade_view.get("exclusive_exposure") is True
    candidate_ids = sorted({f["order_id"] for f in fills
                            if f["side"] == entry_exit_sides(identity["side"])[1]})
    for oid in candidate_ids:
        old_order = (acc.get("orders") or {}).get(oid) or {}
        if old_order.get("status") in TERMINAL_ORDER_STATES:
            continue
        try:
            res = await client.get_order(identity["symbol"], order_id=oid)
            order = normalize_order(res.get("raw") or {}) if res.get("ok") is True else None
            if order and order["order_id"] == oid:
                valid = (exclusive and order["reduce_only"] is True
                         and order["symbol"] == identity["symbol"]
                         and order["position_side"] == identity["position_side"]
                         and order["side"] == entry_exit_sides(identity["side"])[1])
                acc = merge_accounting(acc, orders=[{**order, "role": "exit" if valid else "unattributed"}], now=ts_now)
            else:
                errors.append("EXIT_ORDER_LOOKUP_UNVERIFIED")
        except Exception as exc:
            errors.append(type(exc).__name__)
            break
    exit_ids = [o["order_id"] for o in (acc.get("orders") or {}).values()
                if o.get("role") == "exit" and o.get("reduce_only") is True and exclusive]
    # Operational OPEN is never converted to a finalized trade by a matching sum.
    flat = trade_view.get("position_flat")
    if trade_view.get("status") == "open":
        flat = False
    acc = finalize_accounting(acc, entry_order_ids=[entry_id] if entry_id else [],
                              exit_order_ids=exit_ids, fills_window_complete=fills_complete,
                              position_flat=flat, planned_stop=trade_view.get("planned_stop"), now=ts_now)
    # ── Comissão paga em OUTRO ativo: resolve a conversão REGISTRADA pela
    #    corretora, em lote limitado, com o MESMO orçamento de chamadas e fora
    #    de qualquer transação. Sem registro ⇒ o fill continua bloqueando o
    #    `net_trade` (nada é estimado aqui).
    # ── Conversão da comissão estrangeira: seleção pelos fills ATRIBUÍDOS e
    #    ainda não CONFIRMADOS *após validação* (a existência da chave não
    #    basta: estimativa/invalidez não impedem nova consulta).
    fee_error = None
    fee_state = None
    contexto_taxas = fee_expected_contexts(acc)
    confirmadas_antes = fee_confirmed_keys(acc, fee_context=contexto_taxas)
    pendentes_conv = [item for item in _fee_conversions_required(acc)
                      if str(item["fill_key"]) not in confirmadas_antes]
    if pendentes_conv:
        try:
            resultado_taxas = await collect_broker_fee_conversions(
                client, identity["symbol"], pendentes_conv, identity=identity,
                start_ms=start_ms, end_ms=end_ms, budget=local_budget,
                now_ms=_ms(ts_now))
        except Exception as exc:  # noqa: BLE001 — dúvida mantém o bloqueio
            resultado_taxas = {"state": FEE_COLLECT_ERROR, "evidences": [],
                               "error": type(exc).__name__}
        fee_state = resultado_taxas.get("state")
        fee_error = resultado_taxas.get("error")
        acc["fee_conversion_state"] = fee_state
        acc["fee_conversion_pending"] = resultado_taxas.get("pending") or []
        if resultado_taxas.get("evidences"):
            acc = merge_accounting(acc, fee_conversions=resultado_taxas["evidences"],
                                   fee_context=contexto_taxas, now=ts_now)
            acc = finalize_accounting(
                acc, entry_order_ids=[entry_id] if entry_id else [],
                exit_order_ids=exit_ids, fills_window_complete=fills_complete,
                position_flat=flat, planned_stop=trade_view.get("planned_stop"),
                now=ts_now)
        # Erro REAL da fonte entra no fluxo de retry; incompletude por LOTE não.
        if fee_error and fee_state in (FEE_COLLECT_ERROR,
                                       FEE_COLLECT_SOURCE_UNAVAILABLE):
            errors.append(fee_error)
    fee_progresso = bool(
        fee_confirmed_keys(acc, fee_context=contexto_taxas) - confirmadas_antes)
    funding_error = None
    funding_proof = previous.get("funding_proof") or {}
    exposure_fills = [f for f in fills if f["order_id"] == entry_id or f["order_id"] in exit_ids]
    if acc["state"] == STATE_CONFIRMED and exclusive and exposure_fills:
        exposure_start = min(int(f["time"]) for f in exposure_fills)
        exposure_end = max(int(f["time"]) for f in exposure_fills)
        funding_key = f"{exposure_start}:{exposure_end}"
        funding_complete = funding_proof.get("key") == funding_key and funding_proof.get("complete") is True
        if not funding_complete:
            try:
                raws, funding_complete, funding_error = await _collect_funding(
                    client, identity["symbol"], exposure_start, exposure_end, local_budget)
            except Exception as exc:
                raws, funding_complete, funding_error = [], False, type(exc).__name__
            funding = []
            for raw in raws:
                item, why = normalize_funding(raw)
                if (not item or item.get("symbol") != identity["symbol"]
                        or item.get("asset") != SETTLEMENT_ASSET or _ms(item.get("time")) is None):
                    funding_complete = False
                    funding_error = "FUNDING_ATTRIBUTION_UNVERIFIED"
                elif exposure_start <= _ms(item["time"]) <= exposure_end:
                    funding.append(item)
            acc = merge_accounting(acc, funding=funding, now=ts_now)
        acc["funding_proof"] = {"key": funding_key, "complete": funding_complete}
        # Stored entries outside the observed exposure can never enter its sum.
        acc["funding"] = {k: f for k, f in (acc.get("funding") or {}).items()
                          if f.get("symbol") == identity["symbol"] and f.get("asset") == SETTLEMENT_ASSET
                          and _ms(f.get("time")) is not None
                          and exposure_start <= _ms(f["time"]) <= exposure_end}
        acc = finalize_accounting(acc, entry_order_ids=[entry_id], exit_order_ids=exit_ids,
                                  fills_window_complete=fills_complete, funding_window_complete=funding_complete,
                                  position_flat=flat, planned_stop=trade_view.get("planned_stop"), now=ts_now)
    else:
        acc["funding_reason_code"] = "EXPOSURE_NOT_EXCLUSIVE_OR_NOT_FINAL"
    acc["exclusive_exposure"] = exclusive
    origins = {close_origin(o) for o in (acc.get("orders") or {}).values() if o.get("role") == "exit"}
    if origins:
        acc["close_origin"] = CLOSE_ORIGIN_BOT if origins == {CLOSE_ORIGIN_BOT} else CLOSE_ORIGIN_EXTERNAL
    error = errors[0] if errors else (acc.get("reason_code") if closed_ms else None)
    return _selar(_retry_after_observation(
        acc, previous, error=error, funding_error=funding_error,
        fee_progress=fee_progresso, fee_state=fee_state, now=ts_now))

# ════════════════════════════════════════════════════════════════════════════
#  Persistência — merge transacional com bloqueio de linha
# ════════════════════════════════════════════════════════════════════════════
def trade_view(trade: Any) -> Dict[str, Any]:
    """Projeção simples do ORM para o coletor (nenhum I/O dentro da transação)."""
    get = (lambda k: getattr(trade, k, None))
    return {
        "id": get("id"), "symbol": get("symbol"), "side": get("side"),
        "exchange": (get("exchange") or "binance"),
        "exchange_order_id": get("exchange_order_id"),
        "client_order_id": get("client_order_id"),
        "sl_order_id": get("sl_order_id"), "tp1_order_id": get("tp1_order_id"),
        "tp2_order_id": get("tp2_order_id"), "planned_stop": get("planned_stop"),
        "opened_at": get("opened_at"), "closed_at": get("closed_at"),
        "execution_accounting": get("execution_accounting"),
        "status": get("status"),
    }


def accounting_is_confirmed(acc: Any) -> bool:
    """Contabilidade confirmada NUNCA pode ser regredida para estimativa."""
    return (isinstance(acc, dict)
            and acc.get("schema_version") == SCHEMA_VERSION
            and acc.get("state") == STATE_CONFIRMED
            and not acc.get("conflicts"))


def financial_value_usable(acc: Any) -> bool:
    # Legacy rows are not retroactively rewritten or silently reclassified.
    return acc is None or accounting_is_confirmed(acc)


def public_summary(acc: Any) -> Dict[str, Any]:
    if not isinstance(acc, dict):
        return {"state": STATE_LEGACY, "financial_confirmed": False}
    return {"state": acc.get("state"), "reason_code": acc.get("reason_code"),
            "financial_confirmed": accounting_is_confirmed(acc),
            "funding_state": acc.get("funding_state"),
            "close_origin": acc.get("close_origin"),
            "attempts": acc.get("attempts", 0), "next_retry_at": acc.get("next_retry_at")}


def invalidate_financial_cache():
    # No network and no import side effects after commit.
    import sys
    module = sys.modules.get("services.financial_risk_service")
    if module is not None:
        module.reset_cache()


def merge_observation(current, incoming, *, view):
    """Row-locked evidence merge: conflicts/proofs survive stale deliveries."""
    expected = _identity_for_view(view)
    identity = incoming.get("identity") or {}
    for key in ("exchange", "symbol", "side", "position_side", "entry_order_id", "entry_client_order_id", "account_scope"):
        if expected.get(key) is not None and identity.get(key) != expected[key]:
            return None
    if not current or current.get("schema_version") != SCHEMA_VERSION:
        return None                       # never enroll legacy through apply
    # ── Geração da linha × geração observada (decisão SOB BLOQUEIO) ─────────
    #   `replay`   : a MESMA observação chegando outra vez — não conta falha
    #                nem reinicia contador de novo.
    #   `obsoleta` : coletada sobre geração anterior — ainda contribui EVENTOS,
    #                mas não tem autoridade sobre estatística de retry (uma
    #                falha atrasada não ressuscita tentativas nem rebaixa a
    #                confirmação/progresso de quem veio depois).
    #   `sem_meta` : resposta antiga, sem metadado — sem autoridade sobre a
    #                estatística atual (compatibilidade: linhas que nunca
    #                tiveram geração mantêm a regra anterior por `updated_at`).
    geracao_atual = int(current.get("generation") or 0)
    observacao = incoming.get("observation_id")
    base_geracao = incoming.get("base_generation")
    replay = bool(observacao) and observacao == current.get("last_observation_id")
    sem_meta = base_geracao is None
    obsoleta = (not sem_meta) and int(base_geracao) < geracao_atual
    confirmadas_antes = fee_confirmed_keys(current)
    fills_antes = len(current.get("fills") or {})
    ordens_antes = len(current.get("orders") or {})
    merged = merge_accounting(current, identity=identity,
        fills=list((incoming.get("fills") or {}).values()),
        funding=list((incoming.get("funding") or {}).values()),
        orders=list((incoming.get("orders") or {}).values()),
        # A evidência de conversão de comissão viaja pelo MESMO merge com
        # bloqueio de linha: ela é prova, não um campo de apresentação.
        fee_conversions=[{**e, "ok": True} for e in
                         (incoming.get("fee_conversions") or {}).values()
                         if isinstance(e, dict)])
    for conflict in incoming.get("conflicts") or []:
        if conflict not in merged["conflicts"]:
            merged["conflicts"].append(conflict)
    latest = str(incoming.get("updated_at") or "") >= str(current.get("updated_at") or "")
    for key in ("execution_proof", "funding_proof"):
        old, new = current.get(key) or {}, incoming.get(key) or {}
        if new.get("complete") is True or not old.get("complete") and latest:
            merged[key] = new
    if latest:
        for field in ("close_origin", "exclusive_exposure"):
            if field in incoming:
                merged[field] = incoming[field]
    # Estatística de retry só é adotada de quem tem autoridade sobre a geração
    # atual. `updated_at` mais novo NÃO basta: um snapshot obsoleto não copia
    # `attempts`/`next_retry_at` sobre a linha.
    autoridade = (not replay) and (not obsoleta) and (
        (latest and current.get("generation") is None) if sem_meta else True)
    if autoridade:
        for field in ("attempts", "funding_attempts", "next_retry_at",
                      "last_error", "funding_last_error"):
            if field in incoming:
                merged[field] = incoming[field]
    # Completeness of a closed window can survive an older incomplete response;
    # it cannot survive a new conflicting event or different close boundary.
    proof = merged.get("execution_proof") or {}
    window = str(_ms(view.get("closed_at")))
    complete = proof.get("complete") is True and proof.get("window_key") == window
    if not proof:
        complete = False
    exit_ids = [o["order_id"] for o in (merged.get("orders") or {}).values()
                if o.get("role") == "exit" and o.get("reduce_only") is True
                and merged.get("exclusive_exposure") is True]
    result = finalize_accounting(merged, entry_order_ids=[identity.get("entry_order_id")],
        exit_order_ids=exit_ids, fills_window_complete=complete,
        funding_window_complete=(merged.get("funding_proof") or {}).get("complete") is True,
        position_flat=False if view.get("status") == "open" else None,
        planned_stop=view.get("planned_stop"))
    # ── Progresso ÚTIL medido SOB BLOQUEIO: conjunto confirmado válido antes ×
    #    depois (promoção de estimativa conta; duplicata, janela nova, objeto
    #    estrangeiro ou invalidez não). Progresso real reinicia as tentativas
    #    consecutivas — e uma falha ATRASADA não faz a estatística regredir.
    progresso = (not replay) and (
        bool(fee_confirmed_keys(result) - confirmadas_antes)
        or len(result.get("fills") or {}) > fills_antes
        or len(result.get("orders") or {}) > ordens_antes)
    # Quem TEM autoridade já aplicou a própria regra de progresso no coletor
    # (com o snapshot que observou) — e uma falha real daquela geração conta
    # uma vez. O reinício aqui serve ao caso SEM autoridade: a resposta
    # obsoleta/replay não manda na estatística, mas o evento novo que ela
    # trouxe é progresso e não pode ser cobrado como tentativa perdida.
    if progresso and not autoridade:
        result["attempts"] = 0
        if result.get("state") != STATE_FAILED:
            result["last_error"] = None
    if int(result.get("attempts") or 0) >= MAX_ATTEMPTS and not progresso \
            and result["state"] not in (STATE_CONFIRMED, STATE_CONFLICT):
        result.update(state=STATE_FAILED, reason_code="RETRY_BUDGET_EXHAUSTED")
    # Geração avança por observação EFETIVAMENTE aplicada; o replay da mesma
    # resposta não invalida as coletas em voo nem é contado outra vez.
    if not replay:
        result["generation"] = geracao_atual + 1
        if observacao:
            result["last_observation_id"] = observacao
    else:
        result["generation"] = geracao_atual
        result["last_observation_id"] = current.get("last_observation_id")
    return result


async def apply_accounting(trade_id: Any, accounting: Dict[str, Any], *,
                           project_fields: bool = True) -> Dict[str, Any]:
    """Grava a contabilidade com BLOQUEIO DE LINHA, sem perder eventos.

    O merge é refeito DENTRO da transação contra o valor atual da linha: dois
    workers com respostas diferentes não duplicam valor nem perdem fill, e uma
    fonte confirmada nunca regride para estimativa. Nenhuma chamada à exchange
    acontece aqui.
    """
    from db import DB_ENABLED, get_session
    from models.real_trade import RealTrade
    from sqlalchemy import select

    if not DB_ENABLED or trade_id is None or not isinstance(accounting, dict):
        return {"ok": False, "reason_code": "APPLY_SKIPPED"}
    async with get_session() as session:
        trade = (await session.execute(
            select(RealTrade).where(RealTrade.id == trade_id).with_for_update()
        )).scalar_one_or_none()
        if trade is None:
            return {"ok": False, "reason_code": "TRADE_NOT_FOUND"}

        current = trade.execution_accounting
        merged = merge_observation(current, accounting, view=trade_view(trade))
        if merged is None:
            return {"ok": False, "reason_code": "IDENTITY_OR_SCHEMA_MISMATCH"}
        trade.execution_accounting = merged
        projected = project_to_trade_fields(merged) if project_fields else {}
        # Even a diagnostic-only apply must retract a newly invalidated result.
        if not accounting_is_confirmed(merged):
            projected.update(pnl_usd=None, pnl_pct=None, realized_r=None)
        for field, value in projected.items():
            setattr(trade, field, value)
        if "entry_price" in projected and "qty_initial" in projected:
            trade.notional_usd = trade.entry_price * trade.qty_initial
            trade.entry_slippage_pct = None
            from services.real_trade_service import recompute_entry_slippage
            await recompute_entry_slippage(session, trade, fill_price=trade.entry_price)
        await session.commit()
        invalidate_financial_cache()
        return {"ok": True, "state": merged.get("state"),
                "reason_code": merged.get("reason_code"),
                "projected": sorted(projected), "trade_id": trade_id}


async def reconcile_trade(trade_id: Any, *, client: Any = None, budget=None,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Ciclo completo de UMA operação: lê (fora da txn), coleta e aplica.

    FAIL-SOFT total: qualquer erro devolve `ok=False` e nunca levanta para o
    caller — proteção, fechamento de emergência e limpeza P02/P03 nunca esperam
    contabilidade.
    """
    try:
        from db import DB_ENABLED, get_session
        from models.real_trade import RealTrade
        from sqlalchemy import select, or_

        if not DB_ENABLED or trade_id is None:
            return {"ok": False, "reason_code": "DB_DISABLED"}
        async with get_session() as session:
            trade = (await session.execute(
                select(RealTrade).where(RealTrade.id == trade_id)
            )).scalar_one_or_none()
            if trade is None:
                return {"ok": False, "reason_code": "TRADE_NOT_FOUND"}
            view = trade_view(trade)
            # All sources matter for exclusivity, including manual/managed. A
            # DB failure leaves it unknown; symbol comparison keeps quote exact.
            others = (await session.execute(select(RealTrade.id, RealTrade.symbol)
                .where(RealTrade.id != trade.id, or_(RealTrade.exchange == "binance", RealTrade.exchange.is_(None)))
                .where(RealTrade.opened_at <= (trade.closed_at or now or datetime.now(timezone.utc)))
                .where(or_(RealTrade.closed_at.is_(None), RealTrade.closed_at >= trade.opened_at)))).all()
            view["exclusive_exposure"] = not any(normalize_symbol(r.symbol) == normalize_symbol(trade.symbol) for r in others)
        if accounting_is_confirmed(view.get("execution_accounting")) and (view["execution_accounting"].get("funding_state") == FUNDING_CONFIRMED):
            return {"ok": True, "state": STATE_CONFIRMED, "reason_code": "ALREADY_CONFIRMED"}
        accounting = await collect_trade_accounting(view, client=client, budget=budget, now=now)
        return await apply_accounting(trade_id, accounting)
    except Exception as exc:                      # nunca propaga para o executor
        log.warning(f"[r05c] reconciliação #{trade_id} falhou: {type(exc).__name__}")
        return {"ok": False, "reason_code": "RECONCILE_ERROR"}


async def pending_trade_ids(limit: int = 5, *, now: Optional[datetime] = None
                            ) -> List[int]:
    """IDs ADERENTES ao schema novo com contabilidade pendente.

    Nunca varre nem seleciona registro legado (`execution_accounting IS NULL`
    ou de outro `schema_version`). Lote pequeno: a proteção das posições abertas
    tem prioridade.
    """
    try:
        from db import DB_ENABLED, get_session
        from models.real_trade import RealTrade
        from sqlalchemy import select, and_, or_, func

        if not DB_ENABLED:
            return []
        ledger = RealTrade.execution_accounting
        state = ledger["state"].as_string()
        due = ledger["next_retry_at"].as_string()
        attempts = func.coalesce(ledger["attempts"].as_string(), "0")
        funding_attempts = func.coalesce(ledger["funding_attempts"].as_string(), "0")
        allowed = [str(i) for i in range(MAX_ATTEMPTS)]
        eligible = or_(
            and_(state.in_([STATE_PENDING, STATE_PARTIAL, STATE_AMBIGUOUS]), attempts.in_(allowed)),
            and_(state == STATE_CONFIRMED,
                 ledger["funding_state"].as_string().is_distinct_from(FUNDING_CONFIRMED),
                 funding_attempts.in_(allowed)))
        async with get_session() as session:
            rows = (await session.execute(
                select(RealTrade.id, RealTrade.execution_accounting)
                .where(RealTrade.source == "auto")
                .where(RealTrade.exchange == "binance")
                .where(RealTrade.execution_accounting.is_not(None))
                .where(ledger["schema_version"].as_string() == str(SCHEMA_VERSION))
                .where(eligible, or_(due.is_(None), due <= (now or datetime.now(timezone.utc)).isoformat()))
                .order_by(due.asc().nullsfirst(), RealTrade.id.asc())
                .limit(max(1, min(int(limit or 5), 5)))
            )).all()
        out: List[int] = []
        for row in rows:
            if is_retry_due(row[1], now=now):
                out.append(row[0])
            if len(out) >= max(1, min(int(limit or 5), 5)):
                break
        return out
    except Exception as exc:
        log.warning(f"[r05c] seleção de pendentes falhou: {type(exc).__name__}")
        return []
