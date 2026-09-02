"""
R05A — reconciliação OBSERVACIONAL de P&L e risco real (somente leitura).

Confronta, de forma honesta, três visões que hoje NÃO são a mesma coisa:

  TEÓRICO    `RecommendationSnapshot.realized_r × risk_pct` — resultado do
             SETUP recomendado, usado hoje pelo `risk_service` para DD
             diário/semanal. NÃO é dinheiro realizado.
  FINANCEIRO `RealTrade.pnl_usd` — P&L líquido REGISTRADO (já inclui as taxas
             persistidas e o parcial de TP1). Usado hoje pelo kill switch.
  NORMALIZADO  o P&L financeiro convertido em R pela distância de risco REAL
             registrada, para poder ser comparado ao R do snapshot.

  ABERTO     risco de preço ainda em aberto até o stop CONFIRMADO.

INVARIANTE CENTRAL: este módulo OBSERVA. Não troca nenhuma fonte operacional,
não altera `RiskState`, circuit breaker, pausa, limite, executor ou estratégia.
A consolidação fica para o R05B.

ARQUITETURA: somente `SELECT`. Não importa SDK/provider de exchange, serviço
signed, notificação ou push; não emite ordem; não faz rede; não escreve no
banco; não cria scheduler/worker/loop. Nunca chama `risk_service.update_and_check`,
`kill_switch_service.check_can_trade` ou `kill_switch_service.status` — esses
podem mutar estado ou notificar.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

PHASE = "R05A"
MODE = "OBSERVATION_ONLY"

# ── Constantes FIXAS (sem ENV, sem flag) ────────────────────────────────────
WINDOW_DAYS = 7                      # carga única; 24h é particionada em memória
MIN_PAIR_COVERAGE_PCT = 80.0         # cobertura mínima para julgar o pareamento
ALIGNED_TOLERANCE_BANK_PP = 0.10     # tolerância absoluta, em ponto percentual

# Coortes por `RealTrade.source` — nunca somadas silenciosamente.
KNOWN_SOURCES = ("auto", "managed", "manual", "bybit")
PRIMARY_SOURCE = "auto"
OTHER_SOURCE = "other"

RECON_ALIGNED = "ALIGNED"
RECON_DIVERGENT = "DIVERGENT"
RECON_NOT_COMPARABLE = "NOT_COMPARABLE"
RECON_UNAVAILABLE = "UNAVAILABLE"

LIMITATIONS = [
    "RecommendationSnapshot é resultado de SETUP para pesquisa, não dinheiro "
    "realizado — não é usado como verdade financeira aqui",
    "P&L líquido REGISTRADO: inclui as taxas persistidas e o parcial de TP1; "
    "funding NÃO está reconciliado",
    "funding não possui campo financeiro confiável hoje — retorna UNAVAILABLE e "
    "nunca é estimado",
    "slippage já está refletido no fill/P&L; aqui só a cobertura é reportada, "
    "sem descontar de novo",
    "o cenário de risco aberto é projeção conservadora até o stop confirmado, "
    "não previsão, e não aciona nada",
    "a tolerância de alinhamento é diagnóstica e NÃO aciona trava",
]

INVARIANTS = [
    "observation_only: nenhuma fonte operacional foi trocada",
    "RiskState, circuit breakers, pausas e limites inalterados",
    "somente SELECT: zero escrita, zero rede, zero exchange, zero notificação",
    "nenhuma ordem criada, cancelada ou modificada",
    "nenhuma tabela, coluna, migration, ENV ou flag criada",
]


# ════════════════════════════════════════════════════════════════════════════
#  Helpers puros
# ════════════════════════════════════════════════════════════════════════════
def _finite(value: Any) -> Optional[float]:
    """Float finito ou `None`. Bool, texto, NaN e infinito NUNCA viram número."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return None if (math.isnan(f) or math.isinf(f)) else f


def _as_utc(value: Any) -> Optional[datetime]:
    """Timestamp UTC; naive é tratado explicitamente como UTC."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _valid_id(value: Any) -> bool:
    """Identidade inteira positiva. `bool` não é ID apesar de herdar de `int`."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unavailable(reason_code: str, detail: str) -> Dict[str, Any]:
    """Métrica indisponível: valor `None` + motivo. Nunca NaN nem zero forjado."""
    return {"value": None, "reason_code": reason_code, "detail": detail}


def _bump(counter: Dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _pct(part: int, total: int) -> Optional[float]:
    return round(part / total * 100, 1) if total else None


def _median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(values)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(vals[mid], 6)
    return round((vals[mid - 1] + vals[mid]) / 2, 6)


def _cohort_of(source: Any) -> str:
    src = str(source or "").strip().lower()
    return src if src in KNOWN_SOURCES else OTHER_SOURCE


# ════════════════════════════════════════════════════════════════════════════
#  1) TEÓRICO — RecommendationSnapshot (fonte do risk_service)
# ════════════════════════════════════════════════════════════════════════════
def theoretical_window(snapshots: Sequence[Dict[str, Any]], *, since: datetime,
                       until: datetime) -> Dict[str, Any]:
    """Soma `realized_r` e `realized_r × risk_pct` dos snapshots RESOLVIDOS.

    Janela por `outcome_at`. `realized_r=None`, `risk_pct=None`, NaN/infinito e
    timestamp ausente/inválido são EXCLUÍDOS e contabilizados — nunca viram zero.
    """
    excluded: Dict[str, int] = {}
    count = 0
    sum_r = 0.0
    sum_bank_pct = 0.0
    wins = losses = neutral = 0

    for snap in snapshots or ():
        if not isinstance(snap, dict):
            _bump(excluded, "linha inválida")
            continue
        ts = _as_utc(snap.get("outcome_at"))
        if ts is None:
            _bump(excluded, "outcome_at ausente ou inválido")
            continue
        if not (since <= ts <= until):
            continue
        r = _finite(snap.get("realized_r"))
        if r is None:
            _bump(excluded, "realized_r ausente, NaN ou infinito")
            continue
        risk = _finite(snap.get("risk_pct"))
        if risk is None or risk <= 0:
            _bump(excluded, "risk_pct ausente, NaN, infinito ou não positivo")
            continue
        count += 1
        sum_r += r
        sum_bank_pct += r * risk
        if r > 0:
            wins += 1
        elif r < 0:
            losses += 1
        else:
            neutral += 1

    return {
        "source": "RecommendationSnapshot",
        "window_field": "outcome_at",
        "resolved_count": count,
        "sum_realized_r": round(sum_r, 6) if count else None,
        "sum_bank_pct": round(sum_bank_pct, 6) if count else None,
        "wins": wins, "losses": losses, "neutral": neutral,
        "excluded_by_reason": excluded,
        "note": ("resultado de SETUP para pesquisa — NÃO é dinheiro realizado e "
                 "não é usado como verdade financeira neste relatório"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  2) FINANCEIRO — RealTrade fechado, SEPARADO por source
# ════════════════════════════════════════════════════════════════════════════
def _empty_cohort(name: str) -> Dict[str, Any]:
    return {
        "source": name, "closed_valid": 0,
        "recorded_net_pnl_usd": None, "positive": 0, "negative": 0, "zero": 0,
        "entry_fee_sum_usd": None, "exit_fee_sum_usd": None,
        "entry_fee_coverage_pct": None, "exit_fee_coverage_pct": None,
        "fees_complete": False,
        "with_tp1_partial": 0, "closed_manual": 0, "without_recommendation_id": 0,
        "invalid_recommendation_id": 0,
        "pnl_coverage_pct": None, "recommendation_link_coverage_pct": None,
        "slippage_coverage_pct": None,
        "excluded_by_reason": {}, "closed_total_seen": 0,
    }


def real_cohorts(trades: Sequence[Dict[str, Any]], *, since: datetime,
                 until: datetime) -> Dict[str, Any]:
    """Coortes financeiras por `RealTrade.source`, janela por `closed_at`.

    `pnl_usd` já é LÍQUIDO (inclui `entry_fee`, `exit_fee` e `tp1_realized_usd`):
    é somado UMA única vez, sem descontar taxas de novo e sem re-somar o parcial
    de TP1. Coortes nunca são somadas silenciosamente — existe um total legado
    explícito.
    """
    cohorts: Dict[str, Dict[str, Any]] = {
        name: _empty_cohort(name) for name in KNOWN_SOURCES + (OTHER_SOURCE,)
    }
    legacy = _empty_cohort("ALL_SOURCES_LEGACY_TOTAL")
    fee_sums: Dict[str, List[float]] = {}
    pnl_sums: Dict[str, float] = {}
    seen_pnl: Dict[str, int] = {}
    seen_slip: Dict[str, int] = {}
    seen_entry_fee: Dict[str, int] = {}
    seen_exit_fee: Dict[str, int] = {}

    def _acc(bucket: Dict[str, Any], key: str, trade: Dict[str, Any],
             pnl: Optional[float]) -> None:
        bucket["closed_total_seen"] += 1
        if pnl is None:
            _bump(bucket["excluded_by_reason"], "pnl_usd ausente, NaN ou infinito")
        else:
            bucket["closed_valid"] += 1
            pnl_sums[key] = pnl_sums.get(key, 0.0) + pnl
            seen_pnl[key] = seen_pnl.get(key, 0) + 1
            if pnl > 0:
                bucket["positive"] += 1
            elif pnl < 0:
                bucket["negative"] += 1
            else:
                bucket["zero"] += 1
        entry_fee = _finite(trade.get("entry_fee"))
        exit_fee = _finite(trade.get("exit_fee"))
        fees = fee_sums.setdefault(key, [0.0, 0.0])
        if entry_fee is not None:
            fees[0] += entry_fee
            seen_entry_fee[key] = seen_entry_fee.get(key, 0) + 1
        if exit_fee is not None:
            fees[1] += exit_fee
            seen_exit_fee[key] = seen_exit_fee.get(key, 0) + 1
        if (_finite(trade.get("tp1_realized_usd")) or 0.0) != 0.0:
            bucket["with_tp1_partial"] += 1
        if str(trade.get("status") or "") == "closed_manual":
            bucket["closed_manual"] += 1
        if not _valid_id(trade.get("recommendation_id")):
            bucket["without_recommendation_id"] += 1
            if trade.get("recommendation_id") is not None:
                bucket["invalid_recommendation_id"] += 1
        if _finite(trade.get("entry_slippage_pct")) is not None:
            seen_slip[key] = seen_slip.get(key, 0) + 1

    for trade in trades or ():
        if not isinstance(trade, dict):
            _bump(legacy["excluded_by_reason"], "linha inválida")
            continue
        ts = _as_utc(trade.get("closed_at"))
        if ts is None:
            _bump(legacy["excluded_by_reason"], "closed_at ausente ou inválido")
            continue
        if not (since <= ts <= until):
            continue
        pnl = _finite(trade.get("pnl_usd"))
        cohort = _cohort_of(trade.get("source"))
        _acc(cohorts[cohort], cohort, trade, pnl)
        _acc(legacy, "__legacy__", trade, pnl)

    for key, bucket in list(cohorts.items()) + [("__legacy__", legacy)]:
        total = bucket["closed_total_seen"]
        valid = bucket["closed_valid"]
        bucket["recorded_net_pnl_usd"] = (round(pnl_sums.get(key, 0.0), 4)
                                          if valid else None)
        fees = fee_sums.get(key)
        entry_fee_n = seen_entry_fee.get(key, 0)
        exit_fee_n = seen_exit_fee.get(key, 0)
        bucket["entry_fee_sum_usd"] = (round(fees[0], 4)
                                        if fees and entry_fee_n else None)
        bucket["exit_fee_sum_usd"] = (round(fees[1], 4)
                                       if fees and exit_fee_n else None)
        bucket["entry_fee_coverage_pct"] = _pct(entry_fee_n, total)
        bucket["exit_fee_coverage_pct"] = _pct(exit_fee_n, total)
        bucket["fees_complete"] = bool(total and entry_fee_n == total
                                       and exit_fee_n == total)
        bucket["pnl_coverage_pct"] = _pct(seen_pnl.get(key, 0), total)
        bucket["recommendation_link_coverage_pct"] = _pct(
            total - bucket["without_recommendation_id"], total)
        bucket["slippage_coverage_pct"] = _pct(seen_slip.get(key, 0), total)
        bucket["pnl_label"] = ("P&L líquido registrado, incluindo taxas "
                               "persistidas; funding não reconciliado")

    return {
        "source": "RealTrade",
        "window_field": "closed_at",
        "primary_cohort": PRIMARY_SOURCE,
        "cohorts": cohorts,
        "all_sources_legacy_total": legacy,
        "funding": _unavailable(
            "FUNDING_FIELD_UNAVAILABLE",
            "não existe campo financeiro confiável de funding; não é estimado"),
        "fees_note": ("`pnl_usd` já inclui entry_fee, exit_fee e o parcial de "
                      "TP1 — taxas e TP1 NÃO são descontados/somados de novo; as "
                      "somas de taxa abaixo são apenas informativas"),
        "slippage_note": ("slippage já está refletido no fill/P&L; só a cobertura "
                          "é reportada"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  3) PAREAMENTO — RealTrade(auto) fechado × exatamente 1 RecommendationSnapshot
# ════════════════════════════════════════════════════════════════════════════
def paired_reconciliation(trades: Sequence[Dict[str, Any]],
                          snapshots_by_id: Dict[Any, Dict[str, Any]], *,
                          since: datetime, until: datetime) -> Dict[str, Any]:
    """Compara o R FINANCEIRO (derivado de `pnl_usd`) com o R do snapshot.

    O R persistido do `RealTrade` NUNCA substitui o cálculo por `pnl_usd`: ele
    só é confrontado para detectar inconsistência interna. Vínculo ambíguo é
    excluído da comparação, não escolhido arbitrariamente.
    """
    excluded: Dict[str, int] = {}
    eligible = 0
    by_rec: Dict[Any, List[Dict[str, Any]]] = {}

    for trade in trades or ():
        if not isinstance(trade, dict):
            _bump(excluded, "linha inválida")
            continue
        if _cohort_of(trade.get("source")) != PRIMARY_SOURCE:
            continue
        ts = _as_utc(trade.get("closed_at"))
        if ts is None:
            _bump(excluded, "closed_at ausente ou inválido")
            continue
        if not (since <= ts <= until):
            continue
        eligible += 1
        rec_id = trade.get("recommendation_id")
        if not _valid_id(rec_id):
            _bump(excluded, "sem recommendation_id")
            continue
        by_rec.setdefault(rec_id, []).append(trade)

    deltas_r: List[float] = []
    abs_deltas_r: List[float] = []
    abs_deltas_bank: List[float] = []
    delta_bank_sum = 0.0
    theoretical_bank_sum = 0.0
    financial_bank_sum = 0.0
    comparable = 0
    sign_mismatch = 0
    persisted_r_mismatch = 0
    qty_fallback = 0

    for rec_id, group in by_rec.items():
        if len(group) > 1:
            for _ in group:
                _bump(excluded, "AMBIGUOUS_REAL_LINK")
            continue
        trade = group[0]
        snap = snapshots_by_id.get(rec_id)
        if not isinstance(snap, dict):
            _bump(excluded, "recomendação vinculada não encontrada")
            continue

        pnl = _finite(trade.get("pnl_usd"))
        side = str(trade.get("side") or "").strip().lower()
        entry = _finite(trade.get("entry_price"))
        stop = _finite(trade.get("planned_stop"))
        qty = _finite(trade.get("qty_initial"))
        used_fallback = False
        if qty is None or qty <= 0:
            qty = _finite(trade.get("qty"))
            used_fallback = qty is not None and qty > 0
        snap_r = _finite(snap.get("realized_r"))
        risk_pct = _finite(snap.get("risk_pct"))

        if pnl is None:
            _bump(excluded, "pnl_usd ausente, NaN ou infinito")
            continue
        if entry is None or entry <= 0:
            _bump(excluded, "entry_price inválido")
            continue
        if side not in ("long", "short"):
            _bump(excluded, "side ausente ou inválido")
            continue
        if stop is None or stop <= 0:
            _bump(excluded, "planned_stop inválido")
            continue
        # Igual ao entry é tratado abaixo pelo contrato explícito risk_dollar=0.
        if (side == "long" and stop > entry) or (side == "short" and stop < entry):
            _bump(excluded, "planned_stop incompatível com o lado")
            continue
        if qty is None or qty <= 0:
            _bump(excluded, "qty inválida")
            continue
        if risk_pct is None or risk_pct <= 0:
            _bump(excluded, "risk_pct ausente ou não positivo")
            continue
        if snap_r is None:
            _bump(excluded, "realized_r do snapshot ausente, NaN ou infinito")
            continue

        risk_dollar = abs(entry - stop) * qty
        if not (risk_dollar > 0):
            _bump(excluded, "risk_dollar zero ou inválido")
            continue

        if used_fallback:
            qty_fallback += 1

        financial_r = pnl / risk_dollar
        theoretical_bank = snap_r * risk_pct
        financial_bank = financial_r * risk_pct
        delta_r = financial_r - snap_r
        delta_bank = financial_bank - theoretical_bank

        comparable += 1
        deltas_r.append(delta_r)
        abs_deltas_r.append(abs(delta_r))
        abs_deltas_bank.append(abs(delta_bank))
        delta_bank_sum += delta_bank
        theoretical_bank_sum += theoretical_bank
        financial_bank_sum += financial_bank
        if (financial_r > 0) != (snap_r > 0) and financial_r != 0 and snap_r != 0:
            sign_mismatch += 1
        persisted = _finite(trade.get("realized_r"))
        if persisted is not None and abs(persisted - financial_r) > 0.05:
            persisted_r_mismatch += 1

    coverage = _pct(comparable, eligible)
    material_pair_mismatches = sum(
        1 for value in abs_deltas_bank if value > ALIGNED_TOLERANCE_BANK_PP)
    cancellation_risk = (
        bool(abs_deltas_bank)
        and sum(abs_deltas_bank) > ALIGNED_TOLERANCE_BANK_PP
        and abs(delta_bank_sum) <= ALIGNED_TOLERANCE_BANK_PP
    )
    if eligible == 0 or comparable == 0:
        status, reason = RECON_NOT_COMPARABLE, "NO_COMPARABLE_PAIR"
        detail = "nenhum par elegível e comparável na janela"
    elif coverage is None or coverage < MIN_PAIR_COVERAGE_PCT:
        status, reason = RECON_NOT_COMPARABLE, "COVERAGE_BELOW_FLOOR"
        detail = (f"cobertura {coverage}% abaixo do piso "
                  f"{MIN_PAIR_COVERAGE_PCT}% — diferença não é julgada")
    elif material_pair_mismatches > 0 or sign_mismatch > 0:
        status, reason = RECON_DIVERGENT, "PAIR_LEVEL_DIVERGENCE"
        detail = ("há divergência material em um ou mais pares; diferenças "
                  "opostas não podem se cancelar para produzir ALIGNED")
    elif abs(delta_bank_sum) <= ALIGNED_TOLERANCE_BANK_PP:
        status, reason = RECON_ALIGNED, "WITHIN_TOLERANCE"
        detail = ("diferença agregada dentro da tolerância diagnóstica; "
                  "nenhuma trava é acionada por isso")
    else:
        status, reason = RECON_DIVERGENT, "ABOVE_TOLERANCE"
        detail = ("diferença agregada acima da tolerância diagnóstica; "
                  "nenhuma trava é acionada por isso")

    return {
        "status": status,
        "reason_code": reason,
        "detail": detail,
        "eligible_pairs": eligible,
        "comparable_pairs": comparable,
        "coverage_pct": coverage,
        "min_coverage_pct": MIN_PAIR_COVERAGE_PCT,
        "tolerance_bank_pp": ALIGNED_TOLERANCE_BANK_PP,
        "mean_delta_r": (round(sum(deltas_r) / len(deltas_r), 6)
                         if deltas_r else None),
        "median_delta_r": _median(deltas_r),
        "mean_abs_delta_r": (round(sum(abs_deltas_r) / len(abs_deltas_r), 6)
                              if abs_deltas_r else None),
        "median_abs_delta_r": _median(abs_deltas_r),
        "theoretical_bank_pct": (round(theoretical_bank_sum, 6)
                                 if comparable else None),
        "financial_bank_pct_normalized": (round(financial_bank_sum, 6)
                                          if comparable else None),
        "delta_bank_pct": round(delta_bank_sum, 6) if comparable else None,
        "sum_abs_delta_bank_pct": (round(sum(abs_deltas_bank), 6)
                                    if comparable else None),
        "max_abs_delta_bank_pct": (round(max(abs_deltas_bank), 6)
                                    if abs_deltas_bank else None),
        "material_pair_mismatch_count": material_pair_mismatches,
        "cancellation_risk_detected": cancellation_risk,
        "sign_mismatch_count": sign_mismatch,
        "persisted_r_inconsistency_count": persisted_r_mismatch,
        "qty_initial_fallback_count": qty_fallback,
        "divergences_by_reason": excluded,
        "note": ("apenas RealTrade(source=auto) fechado com exatamente uma "
                 "recomendação; o R persistido do RealTrade nunca substitui o "
                 "cálculo por pnl_usd"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  4) RISCO ABERTO até o stop CONFIRMADO
# ════════════════════════════════════════════════════════════════════════════
def open_risk(open_trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Risco de preço ainda aberto, por coorte, até o stop CONFIRMADO.

    Stop só conta com `sl_order_id` E preço válido (`sl_current_price`
    preferido, `planned_stop` como fallback identificado). Usa a quantidade
    RESTANTE (`qty`), nunca `qty_initial`. Stop já em lucro ⇒ risco de preço
    ZERO, nunca negativo. Posição desconhecida NUNCA vira risco zero.
    """
    cohorts: Dict[str, Dict[str, Any]] = {}

    def _bucket(name: str) -> Dict[str, Any]:
        return cohorts.setdefault(name, {
            "source": name, "positions": 0,
            "remaining_price_risk_usd": 0.0,
            "entry_fee_recorded_usd": 0.0,
            "with_confirmed_stop": 0, "without_confirmed_stop": 0,
            "invalid_data": 0, "planned_stop_fallback": 0,
            "invalid_by_reason": {},
        })

    for trade in open_trades or ():
        if not isinstance(trade, dict):
            b = _bucket(OTHER_SOURCE)
            b["positions"] += 1
            b["invalid_data"] += 1
            _bump(b["invalid_by_reason"], "linha inválida")
            continue
        name = _cohort_of(trade.get("source"))
        b = _bucket(name)
        b["positions"] += 1

        fee = _finite(trade.get("entry_fee"))
        if fee is not None:
            b["entry_fee_recorded_usd"] += fee

        side = str(trade.get("side") or "").strip().lower()
        entry = _finite(trade.get("entry_price"))
        qty = _finite(trade.get("qty"))
        if side not in ("long", "short") or entry is None or entry <= 0 \
                or qty is None or qty <= 0:
            b["invalid_data"] += 1
            _bump(b["invalid_by_reason"], "side, entry_price ou qty inválidos")
            continue

        sl_id = trade.get("sl_order_id")
        if not isinstance(sl_id, str) or not sl_id.strip():
            b["without_confirmed_stop"] += 1
            continue
        stop = _finite(trade.get("sl_current_price"))
        if stop is None or stop <= 0:
            stop = _finite(trade.get("planned_stop"))
            if stop is not None and stop > 0:
                b["planned_stop_fallback"] += 1
        if stop is None or stop <= 0:
            b["without_confirmed_stop"] += 1
            continue

        diff = (entry - stop) if side == "long" else (stop - entry)
        b["remaining_price_risk_usd"] += max(diff, 0.0) * qty
        b["with_confirmed_stop"] += 1

    for b in cohorts.values():
        total = b["positions"]
        unknown = b["without_confirmed_stop"] + b["invalid_data"]
        b["remaining_price_risk_usd"] = round(b["remaining_price_risk_usd"], 4)
        b["entry_fee_recorded_usd"] = round(b["entry_fee_recorded_usd"], 4)
        b["coverage_pct"] = _pct(b["with_confirmed_stop"], total)
        b["open_risk_complete"] = total == 0 or unknown == 0
        b["note"] = ("subtotal CONHECIDO até o stop confirmado; posição sem stop "
                     "confirmado não é risco zero")

    primary = cohorts.get(PRIMARY_SOURCE) or {
        "source": PRIMARY_SOURCE, "positions": 0, "remaining_price_risk_usd": 0.0,
        "entry_fee_recorded_usd": 0.0, "with_confirmed_stop": 0,
        "without_confirmed_stop": 0, "invalid_data": 0,
        "planned_stop_fallback": 0, "invalid_by_reason": {},
        "coverage_pct": None, "open_risk_complete": True,
        "note": "nenhuma posição aberta na coorte primária",
    }
    return {
        "source": "RealTrade(status=open)",
        "primary_cohort": PRIMARY_SOURCE,
        "cohorts": cohorts,
        "open_risk_complete": bool(primary.get("open_risk_complete")),
        "note": ("risco de PREÇO até o stop; stop já em lucro é risco zero, "
                 "nunca negativo"),
    }


def stop_scenario(recorded_net_pnl_usd_auto_24h: Any, open_risk_block: Any
                  ) -> Dict[str, Any]:
    """Cenário conservador: P&L 24h da coorte `auto` menos o risco ainda aberto.

    Projeção, NÃO previsão, e não aciona nada. Só é calculado quando TODA a
    exposição `auto` tem risco conhecido; caso contrário devolve `None`.
    """
    block = open_risk_block if isinstance(open_risk_block, dict) else {}
    if not block.get("open_risk_complete"):
        return _unavailable(
            "OPEN_RISK_INCOMPLETE",
            "há exposição auto com risco desconhecido; risco zero não é assumido")
    pnl = _finite(recorded_net_pnl_usd_auto_24h)
    if pnl is None:
        return _unavailable("PNL_UNAVAILABLE",
                            "P&L registrado de 24h da coorte auto indisponível")
    primary = (block.get("cohorts") or {}).get(PRIMARY_SOURCE) or {}
    risk = _finite(primary.get("remaining_price_risk_usd")) or 0.0
    return {
        "value": round(pnl - risk, 4),
        "reason_code": None,
        "detail": ("projeção conservadora: P&L registrado 24h (auto) menos o "
                   "risco de preço aberto até o stop; não é previsão e não "
                   "aciona nada"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  5) Fontes de controle HOJE (declaração honesta)
# ════════════════════════════════════════════════════════════════════════════
def current_control_sources() -> Dict[str, Any]:
    """O que ACIONA hoje — lido do código, não presumido.

    `risk_service._compute_window_dd` soma `realized_r * risk_pct` de
    `RecommendationSnapshot`; `kill_switch_service._daily_pnl_usd` soma
    `RealTrade.pnl_usd` de trades fechados hoje SEM filtrar `source`.
    """
    return {
        "risk_service_daily_weekly": {
            "source": "RecommendationSnapshot",
            "unit": "percent_from_realized_r",
            "detail": "soma realized_r × risk_pct na janela (24h / 7d)",
        },
        "kill_switch_daily": {
            "source": "RealTrade",
            "scope": "all_sources_currently",
            "unit": "recorded_pnl_usd",
            "detail": "soma pnl_usd de trades fechados no dia, sem filtro de source",
        },
        "unified_financial_source": False,
        "authoritative_source_changed": False,
        "enforcement_changed": False,
        "observation_only": True,
    }


# ════════════════════════════════════════════════════════════════════════════
#  6) Carga — no máximo TRÊS SELECTs, colunas explícitas, sem N+1
# ════════════════════════════════════════════════════════════════════════════
async def load_rows(as_of: datetime) -> Dict[str, Any]:
    """Carrega 7 dias de uma vez. Somente SELECT, colunas explícitas."""
    from db import get_session
    from models.real_trade import RealTrade as RT
    from models.recommendation_snapshot import RecommendationSnapshot as RS
    from sqlalchemy import or_, select

    since = as_of - timedelta(days=WINDOW_DAYS)

    async with get_session() as session:
        closed = (await session.execute(
            select(RT.id, RT.source, RT.status, RT.side, RT.recommendation_id,
                   RT.entry_price, RT.planned_stop, RT.qty, RT.qty_initial,
                   RT.pnl_usd, RT.realized_r, RT.entry_fee, RT.exit_fee,
                   RT.tp1_realized_usd, RT.entry_slippage_pct, RT.closed_at)
            .where(RT.status != "open")
            .where(RT.closed_at.is_not(None))
            .where(RT.closed_at >= since)
        )).all()
        closed_rows = [{
            "id": r.id, "source": r.source, "status": r.status, "side": r.side,
            "recommendation_id": r.recommendation_id,
            "entry_price": r.entry_price, "planned_stop": r.planned_stop,
            "qty": r.qty, "qty_initial": r.qty_initial, "pnl_usd": r.pnl_usd,
            "realized_r": r.realized_r, "entry_fee": r.entry_fee,
            "exit_fee": r.exit_fee, "tp1_realized_usd": r.tp1_realized_usd,
            "entry_slippage_pct": r.entry_slippage_pct, "closed_at": r.closed_at,
        } for r in closed]

        rec_ids = {r["recommendation_id"] for r in closed_rows
                   if r["recommendation_id"] is not None}
        cond = RS.outcome_at >= since
        snaps = (await session.execute(
            select(RS.id, RS.realized_r, RS.risk_pct, RS.outcome_at)
            .where(or_(cond, RS.id.in_(rec_ids)) if rec_ids else cond)
        )).all()
        snap_rows = [{"id": s.id, "realized_r": s.realized_r,
                      "risk_pct": s.risk_pct, "outcome_at": s.outcome_at}
                     for s in snaps]

        opens = (await session.execute(
            select(RT.id, RT.source, RT.side, RT.entry_price, RT.qty,
                   RT.planned_stop, RT.sl_order_id, RT.sl_current_price,
                   RT.entry_fee)
            .where(RT.status == "open")
        )).all()
        open_rows = [{"id": o.id, "source": o.source, "side": o.side,
                      "entry_price": o.entry_price, "qty": o.qty,
                      "planned_stop": o.planned_stop,
                      "sl_order_id": o.sl_order_id,
                      "sl_current_price": o.sl_current_price,
                      "entry_fee": o.entry_fee} for o in opens]

    return {"closed": closed_rows, "snapshots": snap_rows, "open": open_rows}


# ════════════════════════════════════════════════════════════════════════════
#  7) Orquestração — fail-soft por bloco
# ════════════════════════════════════════════════════════════════════════════
def build_report(rows: Dict[str, Any], *, as_of: datetime) -> Dict[str, Any]:
    """Monta o relatório a partir das linhas já carregadas. PURO e síncrono."""
    closed = rows.get("closed") or []
    snapshots = rows.get("snapshots") or []
    opens = rows.get("open") or []
    snapshots_by_id = {s.get("id"): s for s in snapshots if isinstance(s, dict)}

    windows: Dict[str, Any] = {}
    for label, delta in (("24h", timedelta(hours=24)),
                         ("7d", timedelta(days=WINDOW_DAYS))):
        since = as_of - delta
        block: Dict[str, Any] = {"since_utc": since.isoformat(),
                                 "until_utc": as_of.isoformat()}
        try:
            block["theoretical"] = theoretical_window(snapshots, since=since,
                                                      until=as_of)
        except Exception as exc:                       # uma coorte nunca derruba
            log.warning(f"[r05a] teórico {label} falhou: {exc}")
            block["theoretical"] = _unavailable("THEORETICAL_ERROR",
                                                "bloco teórico indisponível")
        try:
            block["financial"] = real_cohorts(closed, since=since, until=as_of)
        except Exception as exc:
            log.warning(f"[r05a] financeiro {label} falhou: {exc}")
            block["financial"] = _unavailable("FINANCIAL_ERROR",
                                              "bloco financeiro indisponível")
        windows[label] = block

    try:
        since7 = as_of - timedelta(days=WINDOW_DAYS)
        paired = paired_reconciliation(closed, snapshots_by_id, since=since7,
                                       until=as_of)
    except Exception as exc:
        log.warning(f"[r05a] pareamento falhou: {exc}")
        paired = {"status": RECON_UNAVAILABLE, "reason_code": "PAIRING_ERROR",
                  "detail": "reconciliação pareada indisponível"}

    try:
        risk_block = open_risk(opens)
    except Exception as exc:
        log.warning(f"[r05a] risco aberto falhou: {exc}")
        risk_block = {"open_risk_complete": False,
                      "reason_code": "OPEN_RISK_ERROR",
                      "detail": "risco aberto indisponível", "cohorts": {}}

    auto_24h = (((windows.get("24h") or {}).get("financial") or {})
                .get("cohorts") or {}).get(PRIMARY_SOURCE) or {}
    try:
        risk_block["realized_24h_plus_open_stop_scenario_usd"] = stop_scenario(
            auto_24h.get("recorded_net_pnl_usd"), risk_block)
    except Exception as exc:
        risk_block["realized_24h_plus_open_stop_scenario_usd"] = _unavailable(
            "SCENARIO_ERROR", "cenário indisponível")
        log.warning(f"[r05a] cenário falhou: {exc}")

    return {
        "ok": True,
        "phase": PHASE,
        "mode": MODE,
        "as_of_utc": as_of.isoformat(),
        "windows": windows,
        "open_risk": risk_block,
        "paired_reconciliation": paired,
        "current_control_sources": current_control_sources(),
        "data_quality": {
            "closed_rows_loaded": len(closed),
            "snapshot_rows_loaded": len(snapshots),
            "open_rows_loaded": len(opens),
            "window_days_loaded": WINDOW_DAYS,
            "naive_timestamp_policy": "tratado explicitamente como UTC",
        },
        "limitations": LIMITATIONS,
        "invariants": INVARIANTS,
    }


async def build_reconciliation(as_of_utc: Optional[datetime] = None
                               ) -> Dict[str, Any]:
    """R05A — relatório completo. Somente leitura; fail-soft na carga."""
    as_of = _as_utc(as_of_utc) or datetime.now(timezone.utc)
    try:
        rows = await load_rows(as_of)
    except Exception as exc:
        log.warning(f"[r05a] carga falhou: {exc}")
        return {
            "ok": False, "phase": PHASE, "mode": MODE,
            "as_of_utc": as_of.isoformat(),
            "reason_code": "LOAD_FAILED",
            "detail": "não foi possível carregar os dados da reconciliação",
            "current_control_sources": current_control_sources(),
            "limitations": LIMITATIONS, "invariants": INVARIANTS,
        }
    return build_report(rows, as_of=as_of)
