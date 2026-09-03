"""
R05B — núcleo financeiro ÚNICO dos circuit breakers.

Substitui, de forma controlada e REVERSÍVEL, a fonte teórica
(`RecommendationSnapshot.realized_r × risk_pct`) por uma fonte financeira comum:

    RealTrade(source="auto") · pnl_usd finito · janela por closed_at
    equity real validada pelo contrato existente de exchange_service.get_equity()
    risco das posições auto abertas até o stop CONFIRMADO

O MESMO snapshot alimenta `risk_service`, `kill_switch_service`,
`/api/risk/status` e `/api/kill-switch/status` — não existe fórmula duplicada
entre consumidores.

REUSO: as validações e fórmulas puras aprovadas no R05A são IMPORTADAS de
`risk_reconciliation_service` (`_finite`, `_as_utc`, `_bump`, `_pct`,
`real_cohorts`, `open_risk`). Nada é copiado com pequenas diferenças, e a
resposta pública do R05A permanece exatamente a mesma.

CUTOVER: `R05_FINANCIAL_BREAKER_ENABLED` nasce `false`. Com `false`, o
comportamento operacional legado permanece e este núcleo só aparece como
DIAGNÓSTICO. Com `true`, toda incerteza é FAIL-CLOSED. O rollback para `false`
restaura o legado sem migration nem edição manual no banco.

FUNDING: R05A provou que não existe campo financeiro confiável de funding.
Aqui ele NUNCA é estimado nem tratado como zero — permanece
`value=null` / `reason_code="FUNDING_FIELD_UNAVAILABLE"`. Por isso o rótulo do
P&L é `RECORDED_NET_EX_FUNDING`: líquido das taxas persistidas, sem funding
reconciliado. Nunca chamar isso de "P&L completo da exchange".
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

# Helpers e fórmulas PUROS já aprovados no R05A — reutilizados, não copiados.
from services.risk_reconciliation_service import (  # noqa: F401
    PRIMARY_SOURCE,
    _as_utc,
    _bump,
    _finite,
    _pct,
    open_risk,
    real_cohorts,
)

log = logging.getLogger(__name__)

PHASE = "R05B"

# ── Flag ÚNICA do cutover (default obrigatório: false) ──────────────────────
CUTOVER_ENV = "R05_FINANCIAL_BREAKER_ENABLED"

# Rótulo obrigatório da fonte financeira.
PNL_LABEL = "RECORDED_NET_EX_FUNDING"
FINANCIAL_SOURCE = 'RealTrade(source="auto").pnl_usd'

METRIC_SOURCE_FINANCIAL = "REAL_TRADE_AUTO_PNL"
METRIC_SOURCE_LEGACY = "RECOMMENDATION_SNAPSHOT"

QUALITY_OK = "OK"
QUALITY_UNKNOWN = "UNKNOWN"

WINDOW_ROLLING = "rolling"
WINDOW_CALENDAR = "calendar"

LOAD_DAYS = 7                 # maior janela necessária; 24h vem em memória
_SNAPSHOT_TTL_S = 15.0        # protege /api/risk/status; não é fórmula paralela
_snapshot_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_snapshot_lock = asyncio.Lock()

FUNDING_BLOCK = {
    "value": None,
    "reason_code": "FUNDING_FIELD_UNAVAILABLE",
    "detail": ("não existe campo financeiro confiável de funding; nunca é "
               "estimado nem tratado como zero"),
}


def cutover_enabled() -> bool:
    """Flag do cutover. Default FALSE — qualquer valor não reconhecido é false."""
    return os.getenv(CUTOVER_ENV, "false").strip().lower() in ("1", "true", "yes", "on")


def _unknown(reason_code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    """Qualidade desconhecida. NUNCA zero, NUNCA valor inventado."""
    return {"quality": QUALITY_UNKNOWN, "reason_code": reason_code,
            "detail": detail, **extra}


# ════════════════════════════════════════════════════════════════════════════
#  Equity — contrato existente, validado
# ════════════════════════════════════════════════════════════════════════════
def _equity_ttl_s() -> float:
    """TTL OFICIAL do cache de equity do `exchange_service`."""
    try:
        from services import exchange_service
        ttl = _finite(getattr(exchange_service, "_EQUITY_CACHE_TTL", None))
        if ttl is not None and ttl > 0:
            return float(ttl)
    except Exception:
        pass
    return 60.0


def validate_equity(payload: Any) -> Dict[str, Any]:
    """Equity só é válida com `ok`, total finito > 0, fonte não-fallback e idade
    dentro do TTL oficial. Caso contrário ⇒ `UNKNOWN`, nunca zero válido."""
    if not isinstance(payload, dict):
        return _unknown("EQUITY_INVALID_PAYLOAD",
                        "resposta de equity não é um contrato válido",
                        total_usd=None, source=None)
    if payload.get("ok") is not True:
        return _unknown("EQUITY_NOT_OK", "exchange não confirmou o equity",
                        total_usd=None, source=payload.get("source"))
    source = str(payload.get("source") or "")
    if source == "fallback":
        return _unknown("EQUITY_FALLBACK", "equity veio do fallback estático",
                        total_usd=None, source=source)
    total = _finite(payload.get("total_usd"))
    if total is None or total <= 0:
        return _unknown("EQUITY_NOT_POSITIVE",
                        "equity ausente, não finita ou não positiva",
                        total_usd=None, source=source)
    age = _finite(payload.get("age_sec"))
    if age is not None and age > _equity_ttl_s():
        return _unknown("EQUITY_STALE",
                        f"equity com {age:.0f}s excede o TTL oficial",
                        total_usd=None, source=source)
    return {"quality": QUALITY_OK, "reason_code": None, "detail": None,
            "total_usd": round(total, 4), "source": source,
            "age_sec": age}


async def fetch_equity() -> Dict[str, Any]:
    """Uma única chamada ao contrato existente, aproveitando o cache oficial."""
    try:
        from services import exchange_service
        return validate_equity(await exchange_service.get_equity())
    except Exception as exc:
        log.warning(f"[r05b] equity indisponível: {type(exc).__name__}")
        return _unknown("EQUITY_ERROR", "falha ao consultar o equity",
                        total_usd=None, source=None)


# ════════════════════════════════════════════════════════════════════════════
#  Janelas financeiras (auto-only) — reutilizam `real_cohorts` do R05A
# ════════════════════════════════════════════════════════════════════════════
def financial_window(closed: Sequence[Dict[str, Any]], *, since: datetime,
                     until: datetime, kind: str,
                     equity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """P&L financeiro da coorte `auto` na janela, com DD percentual sobre equity.

    Usa `real_cohorts` (R05A) e lê SOMENTE a coorte `auto`: `managed`, `manual`,
    `bybit` e `other` nunca são misturados. `pnl_usd` é somado uma única vez —
    taxas e TP1 não são re-aplicados. Zero financeiro legítimo continua zero.
    """
    block = real_cohorts(closed, since=since, until=until)
    auto = (block.get("cohorts") or {}).get(PRIMARY_SOURCE) or {}
    valid = int(auto.get("closed_valid") or 0)
    total_seen = int(auto.get("closed_total_seen") or 0)
    excluded = dict(auto.get("excluded_by_reason") or {})
    pnl = _finite(auto.get("recorded_net_pnl_usd"))
    # `data_complete` só quando nenhuma linha da janela foi excluída.
    data_complete = (total_seen == valid)

    out: Dict[str, Any] = {
        "since_utc": since.isoformat(),
        "until_utc": until.isoformat(),
        "window_kind": kind,
        "financial_source": FINANCIAL_SOURCE,
        "pnl_label": PNL_LABEL,
        "valid_count": valid,
        "excluded_count": max(total_seen - valid, 0),
        "excluded_by_reason": excluded,
        "data_complete": data_complete,
        "pnl_usd": pnl if valid else (0.0 if total_seen == 0 else None),
        "funding": dict(FUNDING_BLOCK),
    }
    if not data_complete:
        out.update(_unknown("PNL_ROWS_EXCLUDED",
                            "há linhas financeiras inválidas na janela"))
        out["pnl_usd"] = None
        out["dd_pct"] = None
        return out
    out["quality"] = QUALITY_OK
    out["reason_code"] = None
    out["detail"] = None

    eq = equity if isinstance(equity, dict) else {}
    total_equity = _finite(eq.get("total_usd")) if eq.get("quality") == QUALITY_OK else None
    if total_equity is None or total_equity <= 0:
        out["dd_pct"] = None
        out["dd_reason_code"] = eq.get("reason_code") or "EQUITY_UNAVAILABLE"
    else:
        out["dd_pct"] = round(out["pnl_usd"] / total_equity * 100.0, 4)
        out["dd_reason_code"] = None
        out["equity_usd"] = round(total_equity, 4)
    return out


def loss_streak(closed: Sequence[Dict[str, Any]], *, since: datetime,
                until: datetime) -> Dict[str, Any]:
    """Losses CONSECUTIVOS da coorte `auto` pelo P&L líquido registrado.

    `pnl_usd < 0` é loss; `>= 0` quebra o streak. TP1 positivo NÃO transforma um
    P&L final negativo em win. Sem fallback para `realized_r` nem para o status.
    `pnl_usd` ausente ou inválido ⇒ qualidade UNKNOWN (fail-closed).
    """
    rows: List[Dict[str, Any]] = []
    invalid = 0
    for trade in closed or ():
        if not isinstance(trade, dict):
            invalid += 1
            continue
        if str(trade.get("source") or "").strip().lower() != PRIMARY_SOURCE:
            continue
        ts = _as_utc(trade.get("closed_at"))
        if ts is None:
            invalid += 1
            continue
        if not (since <= ts <= until):
            continue
        if _finite(trade.get("pnl_usd")) is None:
            invalid += 1
            continue
        rows.append({"ts": ts, "pnl": _finite(trade.get("pnl_usd"))})

    if invalid:
        return _unknown("STREAK_ROWS_INVALID",
                        "há fechamentos auto sem P&L financeiro utilizável",
                        streak=None, last_loss_close=None, invalid_rows=invalid)

    rows.sort(key=lambda r: r["ts"], reverse=True)
    streak = 0
    last_close: Optional[datetime] = None
    for row in rows:
        if row["pnl"] < 0:
            streak += 1
            if last_close is None:
                last_close = row["ts"]
        else:
            break
    return {"quality": QUALITY_OK, "reason_code": None, "detail": None,
            "streak": streak,
            "last_loss_close": last_close.isoformat() if last_close else None,
            "invalid_rows": 0}


def open_exposure(open_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Risco aberto da coorte `auto`, pelo contrato aprovado no R05A.

    A `entry_fee` já registrada da posição aberta entra no pior cenário: ela
    ainda NÃO está dentro do `pnl_usd` de um fechamento.
    """
    block = open_risk(open_rows)
    auto = (block.get("cohorts") or {}).get(PRIMARY_SOURCE) or {}
    complete = bool(block.get("open_risk_complete"))
    # ZERO posições auto é risco CONHECIDO zero — nunca desconhecido. Já uma
    # posição sem stop confirmado mantém `complete=False` e o valor `None`.
    risk = _finite(auto.get("remaining_price_risk_usd"))
    fees = _finite(auto.get("entry_fee_recorded_usd"))
    if complete:
        risk = 0.0 if risk is None else risk
        fees = 0.0 if fees is None else fees
    out = {
        "positions": int(auto.get("positions") or 0),
        "open_price_risk_usd": risk if complete else None,
        "open_entry_fees_usd": fees if complete else None,
        "with_confirmed_stop": int(auto.get("with_confirmed_stop") or 0),
        "without_confirmed_stop": int(auto.get("without_confirmed_stop") or 0),
        "invalid_data": int(auto.get("invalid_data") or 0),
        "planned_stop_fallback": int(auto.get("planned_stop_fallback") or 0),
        "open_risk_complete": complete,
        "cohorts": block.get("cohorts") or {},
    }
    if not complete:
        out.update(_unknown("OPEN_RISK_INCOMPLETE",
                            "há exposição auto com risco desconhecido"))
    else:
        out.update({"quality": QUALITY_OK, "reason_code": None, "detail": None})
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Risco da entrada PROPOSTA
# ════════════════════════════════════════════════════════════════════════════
def proposed_trade_risk_usd(side: Any, final_entry: Any, stop: Any,
                            final_qty: Any) -> Dict[str, Any]:
    """Risco em USD da entrada proposta, com preço e qty FINAIS do preflight.

    LONG  `max(final_entry - stop, 0) × final_qty`
    SHORT `max(stop - final_entry, 0) × final_qty`

    Rejeita como UNKNOWN: side inválido, entry/stop/qty ausentes, NaN/infinito,
    stop estruturalmente incompatível com o lado e risco negativo.
    """
    s = str(side or "").strip().lower()
    if s not in ("long", "short"):
        return _unknown("PROPOSED_SIDE_INVALID", "side inválido", value=None)
    entry = _finite(final_entry)
    stop_price = _finite(stop)
    qty = _finite(final_qty)
    if entry is None or entry <= 0:
        return _unknown("PROPOSED_ENTRY_INVALID", "entry inválido", value=None)
    if stop_price is None or stop_price <= 0:
        return _unknown("PROPOSED_STOP_INVALID", "stop inválido", value=None)
    if qty is None or qty <= 0:
        return _unknown("PROPOSED_QTY_INVALID", "qty inválida", value=None)
    # Stop estruturalmente incompatível com o lado NÃO é risco zero: é UNKNOWN.
    if s == "long" and stop_price >= entry:
        return _unknown("PROPOSED_STOP_SIDE_MISMATCH",
                        "stop acima do entry em LONG", value=None)
    if s == "short" and stop_price <= entry:
        return _unknown("PROPOSED_STOP_SIDE_MISMATCH",
                        "stop abaixo do entry em SHORT", value=None)
    diff = (entry - stop_price) if s == "long" else (stop_price - entry)
    risk = max(diff, 0.0) * qty
    if risk < 0 or _finite(risk) is None:
        return _unknown("PROPOSED_RISK_INVALID", "risco negativo ou inválido",
                        value=None)
    return {"quality": QUALITY_OK, "reason_code": None, "detail": None,
            "value": round(risk, 6)}


def worst_case_daily_usd(snapshot: Any, proposed_risk_usd: Any) -> Dict[str, Any]:
    """`realizado − risco aberto − taxas abertas − risco da nova entrada`.

    Qualquer parcela desconhecida torna o cenário `None` — nunca zero.
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    daily = snap.get("kill_daily") if isinstance(snap.get("kill_daily"), dict) else {}
    exposure = snap.get("open_exposure") if isinstance(snap.get("open_exposure"), dict) else {}

    if daily.get("quality") != QUALITY_OK:
        return _unknown(daily.get("reason_code") or "PNL_UNAVAILABLE",
                        "P&L financeiro diário indisponível", value=None)
    if exposure.get("quality") != QUALITY_OK:
        return _unknown(exposure.get("reason_code") or "OPEN_RISK_INCOMPLETE",
                        "risco aberto incompleto", value=None)
    realized = _finite(daily.get("pnl_usd"))
    open_risk_usd = _finite(exposure.get("open_price_risk_usd"))
    open_fees = _finite(exposure.get("open_entry_fees_usd"))
    proposed = _finite(proposed_risk_usd)
    if realized is None or open_risk_usd is None or open_fees is None \
            or proposed is None or proposed < 0:
        return _unknown("WORST_CASE_NOT_COMPUTABLE",
                        "alguma parcela do pior cenário é desconhecida", value=None)
    return {"quality": QUALITY_OK, "reason_code": None, "detail": None,
            "value": round(realized - open_risk_usd - open_fees - proposed, 6),
            "realized_daily_pnl_usd": realized,
            "open_price_risk_usd": open_risk_usd,
            "open_entry_fees_usd": open_fees,
            "proposed_trade_risk_usd": proposed}


# ════════════════════════════════════════════════════════════════════════════
#  Carga — 2 SELECTs, colunas explícitas, sem N+1
# ════════════════════════════════════════════════════════════════════════════
async def load_rows(as_of: datetime) -> Dict[str, Any]:
    """Somente `SELECT`, apenas a coorte `auto`. Uma leitura, um `as_of`."""
    from db import get_session
    from models.real_trade import RealTrade as RT
    from sqlalchemy import select

    since = as_of - timedelta(days=LOAD_DAYS)
    async with get_session() as session:
        closed = (await session.execute(
            select(RT.id, RT.source, RT.status, RT.side, RT.pnl_usd,
                   RT.tp1_realized_usd, RT.entry_fee, RT.exit_fee,
                   RT.entry_slippage_pct, RT.recommendation_id, RT.closed_at)
            .where(RT.source == PRIMARY_SOURCE)
            .where(RT.status != "open")
            .where(RT.closed_at.is_not(None))
            .where(RT.closed_at >= since)
        )).all()
        opens = (await session.execute(
            select(RT.id, RT.source, RT.side, RT.entry_price, RT.qty,
                   RT.planned_stop, RT.sl_order_id, RT.sl_current_price,
                   RT.entry_fee)
            .where(RT.source == PRIMARY_SOURCE)
            .where(RT.status == "open")
        )).all()

    return {
        "closed": [{"id": c.id, "source": c.source, "status": c.status,
                    "side": c.side, "pnl_usd": c.pnl_usd,
                    "tp1_realized_usd": c.tp1_realized_usd,
                    "entry_fee": c.entry_fee, "exit_fee": c.exit_fee,
                    "entry_slippage_pct": c.entry_slippage_pct,
                    "recommendation_id": c.recommendation_id,
                    "closed_at": c.closed_at} for c in closed],
        "open": [{"id": o.id, "source": o.source, "side": o.side,
                  "entry_price": o.entry_price, "qty": o.qty,
                  "planned_stop": o.planned_stop, "sl_order_id": o.sl_order_id,
                  "sl_current_price": o.sl_current_price,
                  "entry_fee": o.entry_fee} for o in opens],
    }


def kill_daily_start(as_of: datetime) -> datetime:
    """Início do dia UTC do kill switch, respeitando `KILL_DAILY_RESET_AT`."""
    start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        from services.kill_switch_service import _daily_reset_at
        anchor = _as_utc(_daily_reset_at())
        if anchor and anchor > start:
            return anchor
    except Exception:
        pass
    return start


def cooldown_hours() -> float:
    """`KILL_COOLDOWN_HOURS` do contrato existente do kill switch."""
    try:
        from services.kill_switch_service import thresholds
        hours = _finite((thresholds() or {}).get("cooldown_hours"))
        if hours is not None and hours > 0:
            return float(hours)
    except Exception:
        pass
    return 24.0


# ════════════════════════════════════════════════════════════════════════════
#  Snapshot financeiro — a ÚNICA leitura consumida por todos
# ════════════════════════════════════════════════════════════════════════════
def build_snapshot(rows: Dict[str, Any], equity: Dict[str, Any], *,
                   as_of: datetime) -> Dict[str, Any]:
    """Monta o snapshot a partir das linhas já carregadas. PURO e síncrono."""
    closed = (rows or {}).get("closed") or []
    opens = (rows or {}).get("open") or []

    daily_since = as_of - timedelta(hours=24)
    weekly_since = as_of - timedelta(days=7)
    kill_since = kill_daily_start(as_of)
    streak_since = as_of - timedelta(hours=cooldown_hours())
    if kill_since > streak_since:
        streak_since = kill_since          # mesma âncora do kill switch legado

    exposure = open_exposure(opens)
    snap: Dict[str, Any] = {
        "phase": PHASE,
        "ok": True,
        "as_of_utc": as_of.isoformat(),
        "cutover_enabled": cutover_enabled(),
        "metric_source": (METRIC_SOURCE_FINANCIAL if cutover_enabled()
                          else METRIC_SOURCE_LEGACY),
        "financial_source": FINANCIAL_SOURCE,
        "pnl_label": PNL_LABEL,
        "equity": equity,
        "rolling_24h": financial_window(closed, since=daily_since, until=as_of,
                                        kind=WINDOW_ROLLING, equity=equity),
        "rolling_7d": financial_window(closed, since=weekly_since, until=as_of,
                                       kind=WINDOW_ROLLING, equity=equity),
        "kill_daily": financial_window(closed, since=kill_since, until=as_of,
                                       kind=WINDOW_CALENDAR, equity=equity),
        "loss_streak": loss_streak(closed, since=streak_since, until=as_of),
        "open_exposure": exposure,
        "funding": dict(FUNDING_BLOCK),
    }
    blockers: List[str] = []
    for name in ("rolling_24h", "rolling_7d", "kill_daily", "loss_streak"):
        if snap[name].get("quality") != QUALITY_OK:
            blockers.append(name)
    if equity.get("quality") != QUALITY_OK:
        blockers.append("equity")
    if exposure.get("quality") != QUALITY_OK:
        blockers.append("open_exposure")
    snap["blockers"] = blockers
    if blockers:
        snap["quality"] = QUALITY_UNKNOWN
        first = snap.get(blockers[0]) if isinstance(snap.get(blockers[0]), dict) else {}
        snap["reason_code"] = first.get("reason_code") or "FINANCIAL_QUALITY_UNKNOWN"
        snap["detail"] = ("dados financeiros incompletos — nova exposição é "
                          "bloqueada quando o cutover está ligado")
    else:
        snap["quality"] = QUALITY_OK
        snap["reason_code"] = None
        snap["detail"] = None
    return snap


def unavailable_snapshot(reason_code: str, detail: str, *,
                         as_of: Optional[datetime] = None) -> Dict[str, Any]:
    """Snapshot fail-closed: nunca P&L zero, risco zero ou equity zero válida."""
    ts = _as_utc(as_of) or datetime.now(timezone.utc)
    return {
        "phase": PHASE, "ok": False, "as_of_utc": ts.isoformat(),
        "cutover_enabled": cutover_enabled(),
        "metric_source": (METRIC_SOURCE_FINANCIAL if cutover_enabled()
                          else METRIC_SOURCE_LEGACY),
        "financial_source": FINANCIAL_SOURCE, "pnl_label": PNL_LABEL,
        "quality": QUALITY_UNKNOWN, "reason_code": reason_code, "detail": detail,
        "equity": _unknown(reason_code, detail, total_usd=None, source=None),
        "rolling_24h": _unknown(reason_code, detail, pnl_usd=None, dd_pct=None),
        "rolling_7d": _unknown(reason_code, detail, pnl_usd=None, dd_pct=None),
        "kill_daily": _unknown(reason_code, detail, pnl_usd=None, dd_pct=None),
        "loss_streak": _unknown(reason_code, detail, streak=None),
        "open_exposure": _unknown(reason_code, detail,
                                  open_price_risk_usd=None,
                                  open_entry_fees_usd=None,
                                  open_risk_complete=False),
        "funding": dict(FUNDING_BLOCK),
        "blockers": ["load"],
    }


async def financial_snapshot(as_of_utc: Optional[datetime] = None, *,
                             force: bool = False) -> Dict[str, Any]:
    """Leitura financeira completa: 2 SELECTs + 1 equity, com cache curto.

    O cache existe apenas para proteger `/api/risk/status` de alta frequência —
    não é uma segunda fórmula. Falha de banco ou payload inválido devolve
    snapshot `UNKNOWN`, nunca zero.
    """
    if as_of_utc is None and not force:
        cached = _snapshot_cache.get("data")
        if cached is not None and (_time.monotonic() - _snapshot_cache["ts"]) < _SNAPSHOT_TTL_S:
            return cached

    async with _snapshot_lock:
        if as_of_utc is None and not force:
            cached = _snapshot_cache.get("data")
            if cached is not None and (_time.monotonic() - _snapshot_cache["ts"]) < _SNAPSHOT_TTL_S:
                return cached
        as_of = _as_utc(as_of_utc) or datetime.now(timezone.utc)
        try:
            rows = await load_rows(as_of)
        except Exception as exc:
            log.warning(f"[r05b] carga financeira falhou: {type(exc).__name__}")
            return unavailable_snapshot(
                "FINANCIAL_DB_UNAVAILABLE",
                "não foi possível ler os fechamentos financeiros", as_of=as_of)
        equity = await fetch_equity()
        try:
            snap = build_snapshot(rows, equity, as_of=as_of)
        except Exception as exc:
            log.warning(f"[r05b] snapshot financeiro falhou: {type(exc).__name__}")
            return unavailable_snapshot(
                "FINANCIAL_PAYLOAD_INVALID",
                "payload financeiro inválido", as_of=as_of)
        if as_of_utc is None:
            _snapshot_cache["ts"] = _time.monotonic()
            _snapshot_cache["data"] = snap
        return snap


def reset_cache() -> None:
    """Invalida o cache curto (usado em teste e no rollback da flag)."""
    _snapshot_cache["ts"] = 0.0
    _snapshot_cache["data"] = None


# ════════════════════════════════════════════════════════════════════════════
#  Gate financeiro do preflight — chamado ANTES de cada POST de entrada
# ════════════════════════════════════════════════════════════════════════════
BLOCK_REASON = "FINANCIAL_WORST_CASE_LIMIT"


async def check_new_entry(*, side: Any, final_entry: Any, stop: Any,
                          final_qty: Any) -> Dict[str, Any]:
    """Gate financeiro de UMA nova entrada normal. FAIL-CLOSED.

    Com o cutover desligado é um no-op permissivo (diagnóstico). Ligado, nega
    quando o pior cenário diário atinge OU ultrapassa o limite, e também quando
    qualquer parcela é desconhecida. Nenhuma ordem é enviada em caso de negação.
    """
    if not cutover_enabled():
        return {"ok": True, "quality": QUALITY_OK, "reason_code": None,
                "cutover_enabled": False,
                "reason": "cutover financeiro desligado (comportamento legado)"}
    try:
        snap = await financial_snapshot()
        if snap.get("quality") != QUALITY_OK:
            return {"ok": False, "quality": QUALITY_UNKNOWN,
                    "reason_code": snap.get("reason_code") or "FINANCIAL_QUALITY_UNKNOWN",
                    "cutover_enabled": True,
                    "reason": "dados financeiros incompletos — entrada bloqueada",
                    "blockers": snap.get("blockers") or []}

        proposed = proposed_trade_risk_usd(side, final_entry, stop, final_qty)
        if proposed.get("quality") != QUALITY_OK:
            return {"ok": False, "quality": QUALITY_UNKNOWN,
                    "reason_code": proposed.get("reason_code") or "PROPOSED_RISK_INVALID",
                    "cutover_enabled": True,
                    "reason": "risco da entrada proposta não pôde ser calculado"}

        worst = worst_case_daily_usd(snap, proposed.get("value"))
        if worst.get("quality") != QUALITY_OK:
            return {"ok": False, "quality": QUALITY_UNKNOWN,
                    "reason_code": worst.get("reason_code") or "WORST_CASE_NOT_COMPUTABLE",
                    "cutover_enabled": True,
                    "reason": "pior cenário diário não pôde ser calculado"}

        limit = await daily_loss_limit_usd()
        if limit.get("quality") != QUALITY_OK:
            return {"ok": False, "quality": QUALITY_UNKNOWN,
                    "reason_code": limit.get("reason_code") or "DAILY_LIMIT_UNAVAILABLE",
                    "cutover_enabled": True,
                    "reason": "limite diário financeiro indisponível"}

        worst_value = _finite(worst.get("value"))
        limit_usd = _finite(limit.get("value"))
        if worst_value is None or limit_usd is None:
            return {"ok": False, "quality": QUALITY_UNKNOWN,
                    "reason_code": "WORST_CASE_NOT_COMPUTABLE",
                    "cutover_enabled": True,
                    "reason": "pior cenário ou limite não numérico"}
        # Atingir o limite JÁ bloqueia (>=, não >).
        if worst_value <= -limit_usd:
            return {"ok": False, "quality": QUALITY_OK,
                    "reason_code": BLOCK_REASON, "cutover_enabled": True,
                    "reason": (f"pior cenário diário ${worst_value:.2f} atinge o "
                               f"limite ${limit_usd:.2f}"),
                    "worst_case_daily_usd": worst_value,
                    "daily_loss_limit_usd": limit_usd}
        return {"ok": True, "quality": QUALITY_OK, "reason_code": None,
                "cutover_enabled": True,
                "worst_case_daily_usd": worst_value,
                "daily_loss_limit_usd": limit_usd}
    except Exception as exc:                       # exceção NUNCA libera entrada
        log.warning(f"[r05b] gate financeiro falhou: {type(exc).__name__}: {exc}")
        return {"ok": False, "quality": QUALITY_UNKNOWN,
                "reason_code": "FINANCIAL_CHECK_ERROR", "cutover_enabled": True,
                "reason": "erro no gate financeiro — entrada bloqueada"}


async def daily_loss_limit_usd() -> Dict[str, Any]:
    """Limite diário em USD pelo contrato existente do kill switch.

    Reutiliza `KILL_MAX_DAILY_LOSS_PCT` / `KILL_MAX_DAILY_LOSS_USD` — nenhum
    threshold novo. Com pct > 0 e equity inválida, é UNKNOWN (fail-closed).
    """
    try:
        from services.kill_switch_service import thresholds
        th = thresholds() or {}
        floor = _finite(th.get("max_daily_loss_usd"))
        pct = _finite(th.get("max_daily_loss_pct")) or 0.0
        if pct > 0:
            eq = await fetch_equity()
            if eq.get("quality") != QUALITY_OK:
                return _unknown(eq.get("reason_code") or "EQUITY_UNAVAILABLE",
                                "limite percentual exige equity válida", value=None)
            total = _finite(eq.get("total_usd")) or 0.0
            return {"quality": QUALITY_OK, "reason_code": None,
                    "value": round(total * (pct / 100.0), 4),
                    "source": f"{pct:.1f}% × equity ${total:.0f}"}
        if floor is None or floor <= 0:
            return _unknown("DAILY_LIMIT_INVALID",
                            "limite diário absoluto ausente ou inválido", value=None)
        return {"quality": QUALITY_OK, "reason_code": None,
                "value": round(floor, 4), "source": "USD absoluto"}
    except Exception as exc:
        log.warning(f"[r05b] limite diário indisponível: {type(exc).__name__}")
        return _unknown("DAILY_LIMIT_ERROR", "falha ao resolver o limite diário",
                        value=None)


# ════════════════════════════════════════════════════════════════════════════
#  Bloco para `/api/risk/status` e `/api/kill-switch/status`
# ════════════════════════════════════════════════════════════════════════════
def status_block(snapshot: Any, *, last_confirmed: Any = None) -> Dict[str, Any]:
    """Campos backward-compatible expostos pelos dois endpoints. PURO."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    eq = snap.get("equity") if isinstance(snap.get("equity"), dict) else {}
    d24 = snap.get("rolling_24h") if isinstance(snap.get("rolling_24h"), dict) else {}
    d7 = snap.get("rolling_7d") if isinstance(snap.get("rolling_7d"), dict) else {}
    exp = snap.get("open_exposure") if isinstance(snap.get("open_exposure"), dict) else {}
    return {
        "metric_source": snap.get("metric_source") or METRIC_SOURCE_LEGACY,
        "cutover_enabled": bool(snap.get("cutover_enabled")),
        "financial_quality": snap.get("quality") or QUALITY_UNKNOWN,
        "financial_reason_code": snap.get("reason_code"),
        "financial_as_of_utc": snap.get("as_of_utc"),
        "financial_source": snap.get("financial_source") or FINANCIAL_SOURCE,
        "pnl_label": snap.get("pnl_label") or PNL_LABEL,
        "daily_pnl_usd": _finite(d24.get("pnl_usd")),
        "weekly_pnl_usd": _finite(d7.get("pnl_usd")),
        "equity_usd": _finite(eq.get("total_usd")),
        "equity_source": eq.get("source"),
        "open_risk_usd": _finite(exp.get("open_price_risk_usd")),
        "open_risk_complete": bool(exp.get("open_risk_complete")),
        "funding": dict(FUNDING_BLOCK),
        "last_confirmed": last_confirmed,
    }
