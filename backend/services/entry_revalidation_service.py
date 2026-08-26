"""P04A — núcleo puro de revalidação da cotação antes da entrada.

Não gera sinais, não envia ordens e não consulta rede. O caller fornece uma
cotação recém-lida da mesma exchange que executará a ordem. Qualquer dado
essencial ausente ou não finito produz UNKNOWN e bloqueia a submissão.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Optional


def _decimal(value) -> Optional[Decimal]:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not out.is_finite():
        return None
    return out


def _positive(value) -> Optional[Decimal]:
    out = _decimal(value)
    return out if out is not None and out > 0 else None


def _symbol_key(symbol: object) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    return raw.split(":", 1)[0].replace("/", "").replace("-", "")


def _side_key(side: object) -> Optional[str]:
    raw = str(side or "").strip().lower()
    if raw in {"long", "buy"}:
        return "long"
    if raw in {"short", "sell"}:
        return "short"
    return None


def normalize_entry_side(side: object) -> Optional[str]:
    """Normaliza apenas lados explicitamente reconhecidos; nunca infere SELL."""
    return _side_key(side)


def select_entry_route(
    *,
    maker_enabled: bool,
    maker_available: bool,
    preflight_enabled: bool = True,
) -> str:
    """Escolhe o único caminho de submissão permitido.

    Ligar maker sem a capability maker nunca pode degradar silenciosamente para
    MARKET. O retorno é puro e fechado em quatro estados estáveis.
    """
    if maker_enabled:
        if not maker_available:
            return "blocked"
        if not preflight_enabled:
            return "blocked-preflight"
        return "maker"
    return "market"


def _blocked(
    code: str,
    reason: str,
    checks: dict,
    *,
    quality: str = "FRESH",
    blocked_by: Optional[str] = None,
) -> dict:
    return {
        "ok": False,
        "quality": quality,
        "blocked_by": blocked_by or code.lower().replace("exec_", "").replace("_", "-"),
        "reason_code": code,
        "reason": reason,
        "checks": checks,
    }


def cap_qty_for_revalidated_entry(
    planned_qty: float,
    planned_entry: float,
    stop_loss: float,
    execution_price: float,
) -> float:
    """Reduz qty para não aumentar risco USD nem notional; jamais aumenta.

    A P04A usa o preço LIMIT maker já planejado, então a qty normalmente fica
    igual. A função fica como contrato puro para o fallback MARKET da P04B.
    """
    qty = _positive(planned_qty)
    planned = _positive(planned_entry)
    stop = _positive(stop_loss)
    execution = _positive(execution_price)
    if None in {qty, planned, stop, execution}:
        return 0.0
    planned_risk = abs(planned - stop)
    execution_risk = abs(execution - stop)
    if planned_risk <= 0 or execution_risk <= 0:
        return 0.0
    risk_capped = qty * planned_risk / execution_risk
    notional_capped = qty * planned / execution
    capped = min(qty, risk_capped, notional_capped)
    if capped <= 0 or not capped.is_finite():
        return 0.0
    return float(capped)


def evaluate_entry_revalidation(
    *,
    quote: dict,
    symbol: str,
    side: str,
    planned_entry: float,
    stop_loss: float,
    tp1: Optional[float],
    tp2: Optional[float],
    atr: Optional[float],
    max_quote_age_ms: float,
    max_fetch_latency_ms: float,
    max_spread_pct: float,
    max_chase_atr: float,
    min_rr_tp1: float,
    min_rr_tp2: float,
    maker_limit_price: Optional[float] = None,
    entry_zone_low: Optional[float] = None,
    entry_zone_high: Optional[float] = None,
    max_adverse_slippage_pct: float = 0.0,
    enforce_adverse_slippage: bool = False,
    now_ms: Optional[float] = None,
) -> dict:
    """Avalia quote Binance fresca sem alterar o plano ou produzir efeitos.

    `executable_price` é ask para LONG e bid para SHORT. Ele revalida setup,
    spread, chase e R:R no instante da decisão. A LIMIT maker continua no preço
    planejado (ou no preço final arredondado fornecido pelo helper) e também é
    checada para garantir que GTX não cruzaria o book.
    """
    checks: dict = {}
    unknown = {"quality": "UNKNOWN"}
    if not isinstance(quote, dict) or quote.get("ok") is not True:
        raw_code = str(quote.get("reason_code") or "") if isinstance(quote, dict) else ""
        code = raw_code if raw_code.startswith("EXEC_QUOTE_") else "EXEC_QUOTE_UNAVAILABLE"
        return _blocked(
            code,
            f"cotação de execução indisponível: {raw_code or 'resposta inválida'}",
            checks,
            **unknown,
            blocked_by="quote-unknown",
        )

    side_key = _side_key(side)
    if side_key is None:
        return _blocked(
            "EXEC_SIDE_INVALID", "lado da entrada inválido", checks,
            **unknown, blocked_by="quote-unknown",
        )

    expected_symbol = _symbol_key(symbol)
    actual_symbol = _symbol_key(quote.get("symbol"))
    if not expected_symbol or actual_symbol != expected_symbol:
        checks.update({"expected_symbol": expected_symbol, "quote_symbol": actual_symbol})
        return _blocked(
            "EXEC_SYMBOL_MISMATCH", "símbolo da cotação difere da ordem", checks,
            **unknown, blocked_by="quote-unknown",
        )
    if str(quote.get("exchange") or "").strip().lower() != "binance":
        checks["exchange"] = quote.get("exchange")
        return _blocked(
            "EXEC_QUOTE_VENUE_MISMATCH", "cotação não veio da Binance ativa", checks,
            **unknown, blocked_by="quote-unknown",
        )

    bid = _positive(quote.get("bid"))
    ask = _positive(quote.get("ask"))
    if bid is None or ask is None:
        return _blocked(
            "EXEC_QUOTE_INVALID", "bid/ask ausente, não positivo ou não finito", checks,
            **unknown, blocked_by="quote-unknown",
        )
    checks.update({"bid": float(bid), "ask": float(ask)})
    bid_qty = _positive(quote.get("bid_qty"))
    ask_qty = _positive(quote.get("ask_qty"))
    if bid_qty is None or ask_qty is None:
        return _blocked(
            "EXEC_BOOK_LIQUIDITY_INVALID", "quantidade do top-of-book ausente ou inválida", checks,
            **unknown, blocked_by="quote-unknown",
        )
    checks.update({"bid_qty": float(bid_qty), "ask_qty": float(ask_qty)})
    if ask < bid:
        return _blocked(
            "EXEC_BOOK_CROSSED", "book inválido: ask menor que bid", checks,
            **unknown, blocked_by="quote-unknown",
        )

    received_at = _decimal(quote.get("received_at_ms"))
    max_age = _decimal(max_quote_age_ms)
    current_ms = _decimal(now_ms if now_ms is not None else time.time() * 1000.0)
    if received_at is None or current_ms is None or max_age is None or max_age < 0:
        return _blocked(
            "EXEC_QUOTE_TIMESTAMP_INVALID", "timestamp da cotação inválido", checks,
            **unknown, blocked_by="quote-unknown",
        )
    age_ms = current_ms - received_at
    if age_ms < 0:
        return _blocked(
            "EXEC_QUOTE_TIMESTAMP_INVALID", "cotação está no futuro", checks,
            **unknown, blocked_by="quote-unknown",
        )
    checks["age_ms"] = float(age_ms)
    if age_ms > max_age:
        return _blocked(
            "EXEC_QUOTE_STALE",
            f"cotação com {float(age_ms):.0f}ms excede {float(max_age):.0f}ms",
            checks,
            **unknown,
            blocked_by="quote-unknown",
        )

    exchange_time = _decimal(quote.get("exchange_time_ms"))
    if exchange_time is None or exchange_time <= 0:
        return _blocked(
            "EXEC_QUOTE_TIMESTAMP_INVALID", "timestamp da exchange ausente ou inválido", checks,
            **unknown, blocked_by="quote-unknown",
        )
    exchange_age_ms = current_ms - exchange_time
    if exchange_age_ms < -max_age:
        return _blocked(
            "EXEC_QUOTE_TIMESTAMP_INVALID", "timestamp da exchange está no futuro", checks,
            **unknown, blocked_by="quote-unknown",
        )
    exchange_age_ms = max(Decimal("0"), exchange_age_ms)
    checks["exchange_age_ms"] = float(exchange_age_ms)
    if exchange_age_ms > max_age:
        return _blocked(
            "EXEC_QUOTE_STALE",
            f"book da exchange com {float(exchange_age_ms):.0f}ms excede {float(max_age):.0f}ms",
            checks,
            **unknown,
            blocked_by="quote-unknown",
        )

    latency = _decimal(quote.get("latency_ms"))
    max_latency = _decimal(max_fetch_latency_ms)
    if latency is None or latency < 0 or max_latency is None or max_latency < 0:
        return _blocked(
            "EXEC_QUOTE_TIMESTAMP_INVALID", "latência da cotação inválida", checks,
            **unknown, blocked_by="quote-unknown",
        )
    checks["latency_ms"] = float(latency)
    if latency > max_latency:
        return _blocked(
            "EXEC_QUOTE_SLOW",
            f"leitura levou {float(latency):.0f}ms e excede {float(max_latency):.0f}ms",
            checks,
            **unknown,
            blocked_by="quote-unknown",
        )

    mid = (bid + ask) / Decimal("2")
    spread_pct = ((ask - bid) / mid) * Decimal("100") if mid > 0 else Decimal("Infinity")
    checks["spread_pct"] = float(spread_pct)
    spread_limit = _decimal(max_spread_pct)
    if spread_limit is None or spread_limit < 0:
        return _blocked(
            "EXEC_CONFIG_INVALID", "limite de spread inválido", checks,
            **unknown, blocked_by="quote-unknown",
        )
    if spread_limit > 0 and spread_pct > spread_limit:
        return _blocked(
            "EXEC_SPREAD_TOO_WIDE",
            f"spread {float(spread_pct):.4f}% excede {float(spread_limit):.4f}%",
            checks,
            blocked_by="spread",
        )

    planned = _positive(planned_entry)
    stop = _positive(stop_loss)
    first_tp = _positive(tp1) if tp1 is not None else None
    second_tp = _positive(tp2) if tp2 is not None else None
    limit = _positive(maker_limit_price if maker_limit_price is not None else planned_entry)
    atr_d = _positive(atr)
    if planned is None or stop is None or limit is None:
        return _blocked(
            "EXEC_LEVELS_INVALID", "entry, LIMIT ou stop inválido", checks,
            **unknown, blocked_by="levels",
        )

    min_rr1 = _decimal(min_rr_tp1)
    min_rr2 = _decimal(min_rr_tp2)
    if min_rr1 is None or min_rr2 is None or min_rr1 < 0 or min_rr2 < 0:
        return _blocked(
            "EXEC_CONFIG_INVALID", "pisos de R:R inválidos", checks,
            **unknown, blocked_by="quote-unknown",
        )
    if min_rr1 > 0 and first_tp is None:
        return _blocked(
            "EXEC_LEVELS_INVALID", "TP1 obrigatório está ausente ou inválido", checks,
            **unknown, blocked_by="levels",
        )
    if min_rr2 > 0 and second_tp is None:
        return _blocked(
            "EXEC_LEVELS_INVALID", "TP2 obrigatório está ausente ou inválido", checks,
            **unknown, blocked_by="levels",
        )

    executable = ask if side_key == "long" else bid
    checks["executable_price"] = float(executable)
    checks["maker_limit_price"] = float(limit)

    chase_limit = _decimal(max_chase_atr)
    if chase_limit is None or chase_limit < 0:
        return _blocked(
            "EXEC_CONFIG_INVALID", "limite anti-chase inválido", checks,
            **unknown, blocked_by="quote-unknown",
        )
    if chase_limit > 0:
        if atr_d is None:
            return _blocked(
                "EXEC_ATR_INVALID", "ATR ausente ou inválido para revalidar chase", checks,
                **unknown, blocked_by="quote-unknown",
            )
        directional_move = executable - planned if side_key == "long" else planned - executable
        chase_atr = directional_move / atr_d
        checks["chase_atr"] = float(chase_atr)
        if chase_atr >= chase_limit:
            return _blocked(
                "EXEC_PRICE_CHASE",
                f"preço fresco está {float(chase_atr):.3f} ATR a favor; teto {float(chase_limit):.3f}",
                checks,
                blocked_by="price-chase",
            )
    else:
        checks["chase_atr"] = None

    market_adverse = (
        max(Decimal("0"), executable - planned)
        if side_key == "long"
        else max(Decimal("0"), planned - executable)
    )
    checks["market_adverse_drift_pct"] = float(
        market_adverse / planned * Decimal("100")
    )
    maker_adverse = (
        max(Decimal("0"), limit - planned)
        if side_key == "long"
        else max(Decimal("0"), planned - limit)
    )
    adverse_pct = maker_adverse / planned * Decimal("100")
    checks["adverse_slippage_pct"] = float(adverse_pct)
    slippage_limit = _decimal(max_adverse_slippage_pct)
    if enforce_adverse_slippage:
        if slippage_limit is None or slippage_limit < 0:
            return _blocked(
                "EXEC_CONFIG_INVALID", "limite de slippage inválido", checks,
                **unknown, blocked_by="quote-unknown",
            )
        if adverse_pct > slippage_limit:
            return _blocked(
                "EXEC_SLIPPAGE_TOO_HIGH",
                f"slippage adverso {float(adverse_pct):.4f}% excede {float(slippage_limit):.4f}%",
                checks,
                blocked_by="slippage",
            )

    # A ordem GTX deve permanecer maker. P04A não reprifica automaticamente.
    if (side_key == "long" and limit >= ask) or (side_key == "short" and limit <= bid):
        return _blocked(
            "EXEC_MAKER_WOULD_TAKE",
            "LIMIT planejada cruzaria o book e deixaria de ser maker",
            checks,
            blocked_by="maker-cross",
        )

    zone_low = _positive(entry_zone_low) if entry_zone_low is not None else None
    zone_high = _positive(entry_zone_high) if entry_zone_high is not None else None
    if (entry_zone_low is not None or entry_zone_high is not None) and (
        zone_low is None or zone_high is None or zone_low > zone_high
    ):
        return _blocked(
            "EXEC_ENTRY_ZONE_INVALID", "zona de entrada inválida", checks,
            **unknown, blocked_by="levels",
        )
    if zone_low is not None and zone_high is not None and not (zone_low <= limit <= zone_high):
        return _blocked(
            "EXEC_ENTRY_OUTSIDE_ZONE", "LIMIT final saiu da zona estrutural aprovada", checks,
            blocked_by="levels",
        )

    if side_key == "long":
        levels_ok = stop < limit < executable
        levels_ok = levels_ok and (first_tp is None or executable < first_tp)
        levels_ok = levels_ok and (second_tp is None or executable < second_tp)
        levels_ok = levels_ok and (
            first_tp is None or second_tp is None or first_tp <= second_tp
        )
        reward1 = first_tp - executable if first_tp is not None else None
        reward2 = second_tp - executable if second_tp is not None else None
    else:
        levels_ok = stop > limit > executable
        levels_ok = levels_ok and (first_tp is None or executable > first_tp)
        levels_ok = levels_ok and (second_tp is None or executable > second_tp)
        levels_ok = levels_ok and (
            first_tp is None or second_tp is None or first_tp >= second_tp
        )
        reward1 = executable - first_tp if first_tp is not None else None
        reward2 = executable - second_tp if second_tp is not None else None
    risk = abs(executable - stop)
    if not levels_ok or risk <= 0:
        return _blocked(
            "EXEC_LEVELS_INVALID", "preço fresco já invalidou stop ou alvos", checks,
            blocked_by="levels",
        )

    rr1 = reward1 / risk if reward1 is not None else None
    rr2 = reward2 / risk if reward2 is not None else None
    checks["rr_tp1"] = float(rr1) if rr1 is not None else None
    checks["rr_tp2"] = float(rr2) if rr2 is not None else None
    if rr1 is not None and min_rr1 > 0 and rr1 < min_rr1:
        return _blocked(
            "EXEC_RR_TP1_TOO_LOW",
            f"R:R TP1 fresco {float(rr1):.3f} abaixo de {float(min_rr1):.3f}",
            checks,
            blocked_by="fill-rr",
        )
    if rr2 is not None and min_rr2 > 0 and rr2 < min_rr2:
        return _blocked(
            "EXEC_RR_TP2_TOO_LOW",
            f"R:R TP2 fresco {float(rr2):.3f} abaixo de {float(min_rr2):.3f}",
            checks,
            blocked_by="fill-rr",
        )

    return {
        "ok": True,
        "quality": "FRESH",
        "blocked_by": None,
        "reason_code": "EXEC_REVALIDATION_OK",
        "reason": None,
        "checks": checks,
    }


def _depth_levels(raw_levels, *, descending: bool) -> tuple[Optional[list], str]:
    """Normaliza um lado do snapshot sem tolerar book ambíguo."""
    if not isinstance(raw_levels, (list, tuple)) or not raw_levels:
        return None, "lado do book ausente ou vazio"
    levels = []
    previous = None
    seen = set()
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None, "nível do book não possui [preço, quantidade]"
        price = _positive(raw[0])
        quantity = _positive(raw[1])
        if price is None or quantity is None:
            return None, "nível do book contém valor inválido"
        if price in seen:
            return None, "book contém preço duplicado"
        if previous is not None:
            ordered = price < previous if descending else price > previous
            if not ordered:
                return None, "book não está estritamente ordenado"
        seen.add(price)
        previous = price
        levels.append((price, quantity))
    return levels, ""


def estimate_market_depth_fill(
    *,
    depth: dict,
    symbol: str,
    side: str,
    qty: float,
    max_depth_age_ms: float,
    max_fetch_latency_ms: float,
    now_ms: Optional[float] = None,
) -> dict:
    """Estima VWAP/pior preço para 100% da qty usando um snapshot fresco.

    É puro: não consulta rede, não altera o snapshot e não extrapola liquidez
    além dos níveis recebidos. LONG consome asks; SHORT consome bids.
    """
    checks: dict = {}
    unknown = {"quality": "UNKNOWN", "blocked_by": "depth-unknown"}
    if not isinstance(depth, dict) or depth.get("ok") is not True:
        raw_code = str(depth.get("reason_code") or "") if isinstance(depth, dict) else ""
        code = raw_code if raw_code.startswith("EXEC_DEPTH_") else "EXEC_DEPTH_UNAVAILABLE"
        return _blocked(
            code,
            f"profundidade de execução indisponível: {raw_code or 'resposta inválida'}",
            checks,
            **unknown,
        )

    side_key = _side_key(side)
    if side_key is None:
        return _blocked("EXEC_SIDE_INVALID", "lado da entrada inválido", checks, **unknown)
    expected_symbol = _symbol_key(symbol)
    actual_symbol = _symbol_key(depth.get("symbol"))
    if not expected_symbol or actual_symbol != expected_symbol:
        checks.update({"expected_symbol": expected_symbol, "depth_symbol": actual_symbol})
        return _blocked(
            "EXEC_SYMBOL_MISMATCH", "símbolo do depth difere da ordem", checks, **unknown
        )
    if str(depth.get("exchange") or "").strip().lower() != "binance":
        checks["exchange"] = depth.get("exchange")
        return _blocked(
            "EXEC_DEPTH_VENUE_MISMATCH", "depth não veio da Binance ativa", checks, **unknown
        )

    requested = _positive(qty)
    current_ms = _decimal(now_ms if now_ms is not None else time.time() * 1000.0)
    max_age = _decimal(max_depth_age_ms)
    max_latency = _decimal(max_fetch_latency_ms)
    received_at = _decimal(depth.get("received_at_ms"))
    exchange_time = _decimal(depth.get("exchange_time_ms"))
    message_time = _decimal(depth.get("message_time_ms"))
    latency = _decimal(depth.get("latency_ms"))
    update_id = _positive(depth.get("last_update_id"))
    if requested is None:
        return _blocked("EXEC_QTY_INVALID", "qty MARKET inválida", checks, **unknown)
    if (
        current_ms is None or max_age is None or max_age < 0
        or max_latency is None or max_latency < 0
        or received_at is None or exchange_time is None or exchange_time <= 0
        or message_time is None or message_time <= 0
        or latency is None or latency < 0 or update_id is None
        or update_id != update_id.to_integral_value()
    ):
        return _blocked(
            "EXEC_DEPTH_TIMESTAMP_INVALID",
            "timestamps, latência ou update id do depth são inválidos",
            checks,
            **unknown,
        )
    local_age = current_ms - received_at
    if local_age < 0:
        return _blocked(
            "EXEC_DEPTH_TIMESTAMP_INVALID", "depth local está no futuro", checks, **unknown
        )
    exchange_age = current_ms - exchange_time
    message_age = current_ms - message_time
    if exchange_age < -max_age or message_age < -max_age:
        return _blocked(
            "EXEC_DEPTH_TIMESTAMP_INVALID", "timestamp da exchange está no futuro", checks, **unknown
        )
    exchange_age = max(Decimal("0"), exchange_age)
    message_age = max(Decimal("0"), message_age)
    checks.update({
        "requested_qty": float(requested),
        "age_ms": float(local_age),
        "exchange_age_ms": float(exchange_age),
        "message_age_ms": float(message_age),
        "latency_ms": float(latency),
        "last_update_id": int(update_id),
    })
    if max(local_age, exchange_age, message_age) > max_age:
        return _blocked(
            "EXEC_DEPTH_STALE",
            f"depth excede idade máxima de {float(max_age):.0f}ms",
            checks,
            **unknown,
        )
    if latency > max_latency:
        return _blocked(
            "EXEC_DEPTH_SLOW",
            f"leitura levou {float(latency):.0f}ms e excede {float(max_latency):.0f}ms",
            checks,
            **unknown,
        )

    bids, bids_error = _depth_levels(depth.get("bids"), descending=True)
    asks, asks_error = _depth_levels(depth.get("asks"), descending=False)
    if bids is None or asks is None:
        return _blocked(
            "EXEC_DEPTH_INVALID", bids_error or asks_error or "book inválido",
            checks, **unknown,
        )
    best_bid, best_bid_qty = bids[0]
    best_ask, best_ask_qty = asks[0]
    if best_ask <= best_bid:
        return _blocked(
            "EXEC_BOOK_CROSSED", "book cruzado ou travado", checks, **unknown
        )
    mid = (best_bid + best_ask) / Decimal("2")
    spread_pct = (best_ask - best_bid) / mid * Decimal("100")
    selected = asks if side_key == "long" else bids
    remaining = requested
    notional = Decimal("0")
    filled = Decimal("0")
    worst = selected[0][0]
    levels_used = 0
    available = sum((level_qty for _, level_qty in selected), Decimal("0"))
    for price, level_qty in selected:
        if remaining <= 0:
            break
        take = min(remaining, level_qty)
        notional += take * price
        filled += take
        remaining -= take
        worst = price
        levels_used += 1
    checks.update({
        "best_bid": float(best_bid),
        "best_ask": float(best_ask),
        "best_bid_qty": float(best_bid_qty),
        "best_ask_qty": float(best_ask_qty),
        "spread_pct": float(spread_pct),
        "available_qty": float(available),
        "filled_qty": float(filled),
        "levels_used": levels_used,
    })
    if remaining > 0 or filled != requested:
        return _blocked(
            "EXEC_DEPTH_INSUFFICIENT",
            f"depth cobre {float(filled):g} de {float(requested):g}",
            checks,
            quality="FRESH",
            blocked_by="depth-liquidity",
        )
    vwap = notional / requested
    best = best_ask if side_key == "long" else best_bid
    impact = (
        (vwap - best) / best if side_key == "long" else (best - vwap) / best
    ) * Decimal("100")
    checks.update({
        "vwap_price": float(vwap),
        "worst_price": float(worst),
        "best_executable_price": float(best),
        "book_impact_pct": float(max(Decimal("0"), impact)),
    })
    return {
        "ok": True,
        "quality": "FRESH",
        "blocked_by": None,
        "reason_code": "EXEC_DEPTH_ESTIMATE_OK",
        "reason": None,
        "checks": checks,
    }


def evaluate_market_depth_revalidation(
    *,
    depth: dict,
    symbol: str,
    side: str,
    qty: float,
    planned_entry: float,
    stop_loss: float,
    tp1: Optional[float],
    tp2: Optional[float],
    atr: Optional[float],
    max_depth_age_ms: float,
    max_fetch_latency_ms: float,
    max_spread_pct: float,
    max_book_impact_pct: float,
    max_adverse_slippage_pct: float,
    max_chase_atr: float,
    min_rr_tp1: float,
    min_rr_tp2: float,
    entry_zone_low: Optional[float] = None,
    entry_zone_high: Optional[float] = None,
    now_ms: Optional[float] = None,
) -> dict:
    """Revalida uma abertura MARKET pelo custo conservador do depth inteiro."""
    estimate = estimate_market_depth_fill(
        depth=depth,
        symbol=symbol,
        side=side,
        qty=qty,
        max_depth_age_ms=max_depth_age_ms,
        max_fetch_latency_ms=max_fetch_latency_ms,
        now_ms=now_ms,
    )
    if estimate.get("ok") is not True:
        return estimate
    checks = dict(estimate.get("checks") or {})
    side_key = _side_key(side)
    planned = _positive(planned_entry)
    stop = _positive(stop_loss)
    first_tp = _positive(tp1) if tp1 is not None else None
    second_tp = _positive(tp2) if tp2 is not None else None
    atr_d = _positive(atr)
    vwap = _positive(checks.get("vwap_price"))
    worst = _positive(checks.get("worst_price"))
    spread = _decimal(checks.get("spread_pct"))
    impact = _decimal(checks.get("book_impact_pct"))
    limits = {
        "spread": _decimal(max_spread_pct),
        "impact": _decimal(max_book_impact_pct),
        "slippage": _decimal(max_adverse_slippage_pct),
        "chase": _decimal(max_chase_atr),
        "rr1": _decimal(min_rr_tp1),
        "rr2": _decimal(min_rr_tp2),
    }
    if (
        side_key is None or planned is None or stop is None or vwap is None or worst is None
        or spread is None or impact is None
        or any(value is None or value < 0 for value in limits.values())
    ):
        return _blocked(
            "EXEC_CONFIG_INVALID", "níveis ou limites MARKET inválidos", checks,
            quality="UNKNOWN", blocked_by="depth-unknown",
        )
    if limits["rr1"] > 0 and first_tp is None:
        return _blocked(
            "EXEC_LEVELS_INVALID", "TP1 obrigatório ausente", checks,
            quality="UNKNOWN", blocked_by="levels",
        )
    if limits["rr2"] > 0 and second_tp is None:
        return _blocked(
            "EXEC_LEVELS_INVALID", "TP2 obrigatório ausente", checks,
            quality="UNKNOWN", blocked_by="levels",
        )
    if limits["spread"] > 0 and spread > limits["spread"]:
        return _blocked(
            "EXEC_SPREAD_TOO_WIDE", "spread do depth excede o teto", checks,
            blocked_by="spread",
        )
    if limits["impact"] > 0 and impact > limits["impact"]:
        return _blocked(
            "EXEC_BOOK_IMPACT_TOO_HIGH", "impacto VWAP excede o teto", checks,
            blocked_by="slippage",
        )

    adverse_vwap = (
        max(Decimal("0"), vwap - planned)
        if side_key == "long" else max(Decimal("0"), planned - vwap)
    ) / planned * Decimal("100")
    adverse_worst = (
        max(Decimal("0"), worst - planned)
        if side_key == "long" else max(Decimal("0"), planned - worst)
    ) / planned * Decimal("100")
    checks.update({
        "adverse_vwap_pct": float(adverse_vwap),
        "adverse_worst_pct": float(adverse_worst),
        "execution_price": float(worst),
    })
    if limits["slippage"] > 0 and adverse_worst > limits["slippage"]:
        return _blocked(
            "EXEC_SLIPPAGE_TOO_HIGH", "pior nível consumido excede o teto", checks,
            blocked_by="slippage",
        )

    if limits["chase"] > 0:
        if atr_d is None:
            return _blocked(
                "EXEC_ATR_INVALID", "ATR ausente para revalidar chase", checks,
                quality="UNKNOWN", blocked_by="depth-unknown",
            )
        directional_move = worst - planned if side_key == "long" else planned - worst
        chase_atr = directional_move / atr_d
        checks["chase_atr"] = float(chase_atr)
        if chase_atr >= limits["chase"]:
            return _blocked(
                "EXEC_PRICE_CHASE", "pior nível atingiu o teto anti-chase", checks,
                blocked_by="price-chase",
            )
    else:
        checks["chase_atr"] = None

    zone_low = _positive(entry_zone_low) if entry_zone_low is not None else None
    zone_high = _positive(entry_zone_high) if entry_zone_high is not None else None
    if (entry_zone_low is not None or entry_zone_high is not None) and (
        zone_low is None or zone_high is None or zone_low > zone_high
    ):
        return _blocked(
            "EXEC_ENTRY_ZONE_INVALID", "zona de entrada inválida", checks,
            quality="UNKNOWN", blocked_by="levels",
        )
    if zone_low is not None and zone_high is not None and not (zone_low <= worst <= zone_high):
        return _blocked(
            "EXEC_ENTRY_OUTSIDE_ZONE", "pior preço saiu da zona aprovada", checks,
            blocked_by="levels",
        )

    if side_key == "long":
        levels_ok = stop < planned and stop < vwap <= worst
        levels_ok = levels_ok and (first_tp is None or worst < first_tp)
        levels_ok = levels_ok and (second_tp is None or worst < second_tp)
        levels_ok = levels_ok and (
            first_tp is None or second_tp is None or first_tp <= second_tp
        )
        risk = worst - stop
        reward1 = first_tp - worst if first_tp is not None else None
        reward2 = second_tp - worst if second_tp is not None else None
    else:
        levels_ok = stop > planned and stop > vwap >= worst
        levels_ok = levels_ok and (first_tp is None or worst > first_tp)
        levels_ok = levels_ok and (second_tp is None or worst > second_tp)
        levels_ok = levels_ok and (
            first_tp is None or second_tp is None or first_tp >= second_tp
        )
        risk = stop - worst
        reward1 = worst - first_tp if first_tp is not None else None
        reward2 = worst - second_tp if second_tp is not None else None
    if not levels_ok or risk <= 0:
        return _blocked(
            "EXEC_LEVELS_INVALID", "depth invalidou stop ou alvos", checks,
            blocked_by="levels",
        )
    rr1 = reward1 / risk if reward1 is not None else None
    rr2 = reward2 / risk if reward2 is not None else None
    checks["rr_tp1"] = float(rr1) if rr1 is not None else None
    checks["rr_tp2"] = float(rr2) if rr2 is not None else None
    if rr1 is not None and limits["rr1"] > 0 and rr1 < limits["rr1"]:
        return _blocked(
            "EXEC_RR_TP1_TOO_LOW", "R:R TP1 no pior preço ficou abaixo do piso", checks,
            blocked_by="fill-rr",
        )
    if rr2 is not None and limits["rr2"] > 0 and rr2 < limits["rr2"]:
        return _blocked(
            "EXEC_RR_TP2_TOO_LOW", "R:R TP2 no pior preço ficou abaixo do piso", checks,
            blocked_by="fill-rr",
        )
    return {
        "ok": True,
        "quality": "FRESH",
        "blocked_by": None,
        "reason_code": "EXEC_MARKET_REVALIDATION_OK",
        "reason": None,
        "checks": checks,
    }
