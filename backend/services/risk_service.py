"""
Risk Service — circuit breaker de drawdown.

Computa daily/weekly DD a partir dos `recommendation_snapshots` resolvidos
(realized_r != null) e mantém o singleton `RiskState` atualizado.

Triggers (Fase 1):
  - daily_dd_pct  <= -3%   →  pausa "DD diário"
  - weekly_dd_pct <= -6%   →  pausa "DD semanal"

DD aqui é P&L total em % da banca, somando contribuição de cada trade
resolvido na janela. Cada trade contribui `realized_r * risk_pct`
(consistente com como `DailyPnLPanel` calcula `pct = r * risk_pct`).

Reset automático na virada do dia/semana UTC: se `current_day_utc`
no DB difere de hoje, daily_dd zera. Mesmo pra semana.

Pause manual (kill switch) NÃO é resetado automaticamente — fica
ligado até o usuário explicitamente desligar via endpoint.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, func

from db import DB_ENABLED, get_session
from models.risk_state import RiskState
from models.risk_event import RiskEvent

# Chave estável do advisory lock transacional que serializa arm/release/resume da
# pausa P03 (exclusão mútua real no Postgres — P03.1C/D).
_P03_PAUSE_LOCK = 917283
_P03_PAUSE_MARKER = "P03-QUARANTINE:"
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

# Thresholds — alinhados com ROADMAP Fase 1.1
DAILY_DD_LIMIT_PCT = -3.0
WEEKLY_DD_LIMIT_PCT = -6.0


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _this_week_utc() -> str:
    """ISO year-week, ex: '2026-W22'. Vira na segunda 00:00 UTC."""
    return datetime.now(timezone.utc).strftime("%G-W%V")


async def _get_or_create_state(session) -> RiskState:
    """Garante que existe a linha singleton id=1."""
    stmt = select(RiskState).where(RiskState.id == 1)
    state = (await session.execute(stmt)).scalar_one_or_none()
    if state is None:
        state = RiskState(
            id=1,
            trading_paused=False,
            current_day_utc=_today_utc(),
            current_week_utc=_this_week_utc(),
        )
        session.add(state)
        await session.flush()
    return state


def _log_event(session, state: RiskState, event_type: str, reason: str | None) -> None:
    """Grava evento de transição na tabela risk_events (snapshot das métricas)."""
    ev = RiskEvent(
        event_type=event_type,
        reason=reason,
        daily_dd_pct=state.daily_dd_pct,
        weekly_dd_pct=state.weekly_dd_pct,
        daily_trades=state.daily_trades,
        weekly_trades=state.weekly_trades,
    )
    session.add(ev)


async def _compute_window_dd(session, hours: int) -> tuple[float, int]:
    """
    Soma (realized_r * risk_pct) de trades resolvidos nas últimas `hours`.
    Retorna (dd_pct, trade_count).
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(
            func.coalesce(
                func.sum(RecommendationSnapshot.realized_r * RecommendationSnapshot.risk_pct),
                0.0,
            ),
            func.count(RecommendationSnapshot.id),
        )
        .where(RecommendationSnapshot.outcome_at >= since)
        .where(RecommendationSnapshot.realized_r.is_not(None))
    )
    row = (await session.execute(stmt)).one()
    return float(row[0]), int(row[1])


def _financial_fields(snapshot, state) -> dict:
    """R05B — campos financeiros expostos por `/api/risk/status`.

    Backward-compatible: apenas ACRESCENTA. `last_confirmed` carrega o último
    valor persistido, marcado como `stale` quando a métrica atual é UNKNOWN —
    nunca é zero fabricado.
    """
    try:
        from services import financial_risk_service as _frs
        last = {
            "daily_dd_pct": getattr(state, "daily_dd_pct", None),
            "weekly_dd_pct": getattr(state, "weekly_dd_pct", None),
            "daily_trades": getattr(state, "daily_trades", None),
            "weekly_trades": getattr(state, "weekly_trades", None),
            "updated_at": (getattr(state, "updated_at", None).isoformat()
                           if getattr(state, "updated_at", None) else None),
            "stale": bool(snapshot is not None
                          and (not isinstance(snapshot, dict)
                               or snapshot.get("quality") != "OK")),
        }
        if snapshot is None:
            # Cutover desligado: declara a fonte legada sem calcular nada novo.
            return {
                "metric_source": _frs.METRIC_SOURCE_LEGACY,
                "cutover_enabled": False,
                "financial_quality": None,
                "financial_reason_code": "CUTOVER_DISABLED",
                "financial_as_of_utc": None,
                "funding": dict(_frs.FUNDING_BLOCK),
                "last_confirmed": last,
            }
        return _frs.status_block(snapshot, last_confirmed=last)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[r05b] campos financeiros indisponíveis: {type(exc).__name__}")
        return {"metric_source": None, "cutover_enabled": None,
                "financial_quality": "UNKNOWN",
                "financial_reason_code": "FINANCIAL_FIELDS_ERROR"}


def _is_p03_pause(state) -> bool:
    """Pausa de owner P03 (quarentena de execução). Invariante #8: NUNCA é solta por
    auto-resume (virada de dia/semana). Só o release estruturado — zero incidentes
    abertos, mesma transação + advisory lock — pode removê-la."""
    return (getattr(state, "pause_reason", None) or "").startswith(_P03_PAUSE_MARKER)


async def update_and_check() -> dict:
    """
    Atualiza métricas de DD + decide se aciona pausa automática.
    Chamada periodicamente (a cada scan loop) e exposta via endpoint.

    Retorna dict com estado atual pra UI/log.
    """
    if not DB_ENABLED:
        return {"enabled": False}

    # R05B — snapshot financeiro calculado ANTES de abrir a transação com o
    # advisory lock (evita segurar a lock enquanto se lê equity/fechamentos).
    # Com o cutover DESLIGADO fica apenas como diagnóstico; nada muda.
    _fin = None
    try:
        from services import financial_risk_service as _frs
        if _frs.cutover_enabled():
            _fin = await _frs.financial_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[r05b] snapshot financeiro indisponível "
                    f"(fail-closed, sem auto-resume): {type(exc).__name__}")
        _fin = {"quality": "UNKNOWN", "reason_code": "FINANCIAL_SNAPSHOT_ERROR"}

    _financial_mode = _fin is not None
    # Métrica financeira incerta ⇒ FAIL-CLOSED: nada de auto-resume e nada de
    # nova pausa a partir de número velho. A pausa existente é preservada.
    _financial_unknown = bool(
        _financial_mode and (
            not isinstance(_fin, dict)
            or _fin.get("quality") != "OK"
            or not isinstance((_fin.get("rolling_24h") or {}).get("dd_pct"), (int, float))
            or not isinstance((_fin.get("rolling_7d") or {}).get("dd_pct"), (int, float))
        )
    )

    from sqlalchemy import text, func, select as _sel
    async with get_session() as session:
        # Serializa com arm/release/record do P03 (MESMA advisory lock) ANTES da
        # primeira leitura de RiskState — fecha a corrida read-modify-write que
        # deixava `open_incidents=1, trading_paused=false`.
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _P03_PAUSE_LOCK})
        state = await _get_or_create_state(session)

        # Conta incidentes P03 abertos NA MESMA txn. Fail-closed: erro → desconhecido
        # (None) → NUNCA auto-resume e NÃO fabrica pausa (preserva estado).
        open_p03 = None
        try:
            from models.execution_incident import ExecutionIncident
            open_p03 = (await session.execute(_sel(func.count(ExecutionIncident.id))
                        .where(ExecutionIncident.resolved_at.is_(None)))).scalar() or 0
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[circuit-breaker] contagem de incidentes P03 falhou "
                        f"(fail-closed, sem auto-resume): {exc}")
            open_p03 = None
        _no_incident = (open_p03 == 0)   # True SÓ quando CONFIRMADO zero incidentes

        # Reset por virada de dia/semana
        today = _today_utc()
        week = _this_week_utc()
        if state.current_day_utc != today:
            state.current_day_utc = today
            # Não zera weekly aqui — semana tem seu próprio rollover
        if state.current_week_utc != week:
            state.current_week_utc = week
            # Reseta pausa AUTOMÁTICA só com ZERO incidentes P03 confirmado (nunca a
            # manual, nunca a P03, nunca enquanto houver incidente aberto).
            if (_no_incident and not _financial_unknown and state.trading_paused
                    and not state.pause_manual and not _is_p03_pause(state)):
                log.info("Virada de semana — pausa automática resetada.")
                state.trading_paused = False
                state.pause_reason = None
                state.paused_at = None
                _log_event(session, state, "auto_resume", "Virada de semana UTC")

        # Recalcula DD — R05B: com o cutover LIGADO a fonte é o P&L financeiro
        # registrado da coorte `auto`; com ele desligado, o legado por snapshot.
        if _financial_mode and not _financial_unknown:
            _d24 = _fin.get("rolling_24h") or {}
            _d7 = _fin.get("rolling_7d") or {}
            daily_dd = float(_d24["dd_pct"])
            weekly_dd = float(_d7["dd_pct"])
            daily_trades = int(_d24.get("valid_count") or 0)
            weekly_trades = int(_d7.get("valid_count") or 0)
            state.daily_dd_pct = daily_dd
            state.weekly_dd_pct = weekly_dd
            state.daily_trades = daily_trades
            state.weekly_trades = weekly_trades
        elif _financial_mode:
            # FAIL-CLOSED: não grava zero, não sobrescreve o último valor
            # confirmado e NUNCA libera pausa automática.
            daily_dd = float(state.daily_dd_pct or 0.0)
            weekly_dd = float(state.weekly_dd_pct or 0.0)
            daily_trades = int(state.daily_trades or 0)
            weekly_trades = int(state.weekly_trades or 0)
            log.warning("[r05b] métrica financeira UNKNOWN "
                        f"({(_fin or {}).get('reason_code')}) — valores "
                        "anteriores preservados como last_confirmed")
        else:
            daily_dd, daily_trades = await _compute_window_dd(session, hours=24)
            weekly_dd, weekly_trades = await _compute_window_dd(session, hours=24 * 7)
            state.daily_dd_pct = daily_dd
            state.weekly_dd_pct = weekly_dd
            state.daily_trades = daily_trades
            state.weekly_trades = weekly_trades

        # ── Auto-resume do circuit breaker AUTOMÁTICO ───────────────────────
        # BUGFIX: a virada de DIA atualizava a data mas NÃO despausava — só a
        # virada de SEMANA fazia. Uma pausa DIÁRIA (DD -3%) ficava presa até
        # segunda-feira (ou resume manual), deixando o bot parado por dias.
        # Regra: se a pausa é automática (não manual), disparou num dia UTC
        # ANTERIOR e AMBOS os DD já voltaram pra dentro do limite → retoma.
        # Exigir weekly saudável impede soltar uma pausa SEMANAL cedo demais.
        if (_no_incident and not _financial_unknown and state.trading_paused
                and not state.pause_manual and state.paused_at
                and not _is_p03_pause(state)):
            _pa = state.paused_at
            if _pa.tzinfo is None:
                _pa = _pa.replace(tzinfo=timezone.utc)
            _pa_day = _pa.astimezone(timezone.utc).strftime("%Y-%m-%d")
            if (_pa_day != today
                    and daily_dd > DAILY_DD_LIMIT_PCT
                    and weekly_dd > WEEKLY_DD_LIMIT_PCT):
                log.info(
                    f"[circuit-breaker] AUTO-RESUME: dia virou e DD recuperou "
                    f"(d={daily_dd:.2f}% w={weekly_dd:.2f}%)"
                )
                state.trading_paused = False
                state.pause_reason = None
                state.paused_at = None
                _log_event(session, state, "auto_resume", "Virada de dia UTC + DD recuperado")

        # Aciona pausa automática se cruzou limite (e não estava já pausado)
        _just_auto_paused = False
        # R05B: métrica financeira UNKNOWN não fabrica pausa a partir de
        # valor velho — o bloqueio de nova exposição fica com o kill switch.
        if not state.trading_paused and not _financial_unknown:
            _src = (" (P&L financeiro registrado)" if _financial_mode else "")
            if daily_dd <= DAILY_DD_LIMIT_PCT:
                state.trading_paused = True
                state.pause_manual = False
                state.pause_reason = (
                    f"DD diário {daily_dd:.2f}% atingiu limite "
                    f"{DAILY_DD_LIMIT_PCT}%{_src}"
                )
                state.paused_at = datetime.now(timezone.utc)
                log.warning(f"[circuit-breaker] AUTO-PAUSE: {state.pause_reason}")
                _log_event(session, state, "auto_pause", state.pause_reason)
                _just_auto_paused = True
            elif weekly_dd <= WEEKLY_DD_LIMIT_PCT:
                state.trading_paused = True
                state.pause_manual = False
                state.pause_reason = (
                    f"DD semanal {weekly_dd:.2f}% atingiu limite "
                    f"{WEEKLY_DD_LIMIT_PCT}%{_src}"
                )
                state.paused_at = datetime.now(timezone.utc)
                log.warning(f"[circuit-breaker] AUTO-PAUSE: {state.pause_reason}")
                _log_event(session, state, "auto_pause", state.pause_reason)
                _just_auto_paused = True

        # ENFORCEMENT do invariante (mesma txn+lock): incidente P03 aberto ⇒
        # trading_paused=true. `ensure_p03_pause_in_session` preserva pausa
        # manual/P02/DD existente (que já bloqueia) e carimba P03 só se estiver
        # despausado. Nunca libera; nunca sobrescreve owner do operador.
        if open_p03 and open_p03 > 0:
            await ensure_p03_pause_in_session(
                session, f"{open_p03} incidente(s) P03 aberto(s) — trading pausado")

        state.updated_at = datetime.now(timezone.utc)
        await session.commit()

        result = _to_dict(state)
        # Sinaliza a TRANSIÇÃO pra pausa (o scan loop dispara push de alerta).
        result["just_auto_paused"] = _just_auto_paused
        result.update(_financial_fields(_fin, state))
        return result


async def get_status() -> dict:
    """Lê estado atual sem recomputar (rápido, pra endpoint público).

    R05B: acrescenta o MESMO snapshot financeiro consumido pelo kill switch —
    sem fórmula independente e aproveitando o cache curto do núcleo.
    """
    if not DB_ENABLED:
        return {"enabled": False}
    _fin = None
    try:
        from services import financial_risk_service as _frs
        if _frs.cutover_enabled():
            _fin = await _frs.financial_snapshot()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[r05b] status financeiro indisponível: {type(exc).__name__}")
        _fin = {"quality": "UNKNOWN", "reason_code": "FINANCIAL_SNAPSHOT_ERROR"}
    async with get_session() as session:
        state = await _get_or_create_state(session)
        await session.commit()
        out = _to_dict(state)
        out.update(_financial_fields(_fin, state))
        return out


async def is_paused() -> bool:
    """Atalho pra gates de push/scan."""
    if not DB_ENABLED:
        return False
    async with get_session() as session:
        state = await _get_or_create_state(session)
        await session.commit()
        return bool(state.trading_paused)


async def set_manual_pause(paused: bool, reason: Optional[str] = None) -> dict:
    """Kill switch manual — usuário liga/desliga via UI."""
    if not DB_ENABLED:
        return {"enabled": False}
    from sqlalchemy import text, func, select as _sel
    async with get_session() as session:
        # Serializa com o arm/release do P03 (mesma advisory lock) — sem janela.
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _P03_PAUSE_LOCK})
        state = await _get_or_create_state(session)
        was_paused = bool(state.trading_paused)
        kept_p03 = False
        if paused:
            state.trading_paused = True
            state.pause_manual = True
            state.pause_reason = reason or "Pausa manual via kill switch"
            state.paused_at = datetime.now(timezone.utc)
        else:
            # Retomada owner-aware: se há incidente P03 aberto, NÃO libera — re-carimba
            # como P03-owned (fecha a janela transacionalmente). Invariante:
            # incidente aberto ⇒ trading_paused permanece true.
            open_p03 = 0
            try:
                from models.execution_incident import ExecutionIncident
                open_p03 = (await session.execute(_sel(func.count(ExecutionIncident.id))
                            .where(ExecutionIncident.resolved_at.is_(None)))).scalar() or 0
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[circuit-breaker] contagem de incidentes P03 falhou (fail-closed): {exc}")
                open_p03 = 1  # fail-closed: não libera sem confirmar zero
            if open_p03 > 0:
                kept_p03 = True
                state.trading_paused = True
                state.pause_manual = False
                state.pause_reason = f"{_P03_PAUSE_MARKER} {open_p03} incidente(s) P03 aberto(s) — resume manual bloqueado"
                state.paused_at = state.paused_at or datetime.now(timezone.utc)
            else:
                state.trading_paused = False
                state.pause_manual = False
                state.pause_reason = None
                state.paused_at = None
        state.updated_at = datetime.now(timezone.utc)
        if paused and not was_paused:
            _log_event(session, state, "manual_pause", state.pause_reason)
        elif (not paused) and was_paused and not kept_p03:
            _log_event(session, state, "manual_resume", reason or "Retomado manualmente")
        await session.commit()
        log.warning(f"[circuit-breaker] MANUAL pause={paused} kept_p03={kept_p03} reason={reason}")
        # Latch em memória: arma/limpa SOMENTE o owner "manual" — NUNCA o clear
        # genérico (que apagaria P03/P02/legacy).
        try:
            from services import shadow_trade_service
            if paused:
                shadow_trade_service._arm_execution_quarantine(
                    state.pause_reason or "manual", owner="manual")
            else:
                shadow_trade_service.clear_execution_quarantine(owner="manual")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[circuit-breaker] falha ajustando latch manual: {exc}")
        return _to_dict(state)


async def ensure_p03_pause_in_session(session, reason: str) -> None:
    """Garante RiskState pausado como P03 DENTRO da sessão/transação do CALLER (que
    já detém `pg_advisory_xact_lock(_P03_PAUSE_LOCK)`). NÃO faz commit. Preserva
    pausa manual/não-P03 do operador. Usado no caminho transacional único
    pausa+incidente (P03.1E)."""
    state = await _get_or_create_state(session)
    now = datetime.now(timezone.utc)
    cur = state.pause_reason or ""
    if state.trading_paused and not cur.startswith(_P03_PAUSE_MARKER):
        return   # pausa manual/não-P03 → preserva; o latch/pausa já bloqueia entradas
    state.trading_paused = True
    state.pause_manual = False
    state.pause_reason = f"{_P03_PAUSE_MARKER} {reason}"
    state.paused_at = state.paused_at or now
    state.updated_at = now


async def arm_p03_pause(reason: str) -> bool:
    """Arma a pausa P03 ATOMICAMENTE sob `pg_advisory_xact_lock` (sessão própria)."""
    if not DB_ENABLED:
        return False
    from sqlalchemy import text
    async with get_session() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _P03_PAUSE_LOCK})
        before = ((await _get_or_create_state(session)).pause_reason or "")
        await ensure_p03_pause_in_session(session, reason)
        await session.commit()
        return True if not (before and not before.startswith(_P03_PAUSE_MARKER)) else False


# Resultados estruturados do release (P03.1E)
RELEASE_RELEASED = "RELEASED"
RELEASE_SAFE_OTHER_OWNER = "SAFE_OTHER_OWNER"
RELEASE_STILL_OPEN = "STILL_OPEN"
RELEASE_ERROR = "ERROR"


async def release_p03_pause(marker: str) -> str:
    """Release do P03 com RESULTADO ESTRUTURADO, tudo na MESMA transação + advisory
    lock: (1) conta incidentes não resolvidos; falha na contagem → ERROR; (2) >0 →
    STILL_OPEN; (3) pausa de outro owner (não-P03) → SAFE_OTHER_OWNER (preserva);
    (4) zero incidentes + pausa P03 → remove SÓ a pausa P03 → RELEASED."""
    if not DB_ENABLED:
        return RELEASE_ERROR
    from sqlalchemy import update, text, func, select as _sel
    from models.risk_state import RiskState
    like = marker.replace("%", r"\%") + "%"
    async with get_session() as session:
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _P03_PAUSE_LOCK})
            from models.execution_incident import ExecutionIncident
            open_p03 = (await session.execute(_sel(func.count(ExecutionIncident.id))
                        .where(ExecutionIncident.resolved_at.is_(None)))).scalar()
            if open_p03 is None:
                await session.rollback()
                return RELEASE_ERROR
            if open_p03 > 0:
                await session.commit()
                return RELEASE_STILL_OPEN
            state = await _get_or_create_state(session)
            if not state.trading_paused:
                await session.commit()
                return RELEASE_RELEASED    # já não pausado
            if not (state.pause_reason or "").startswith(marker):
                await session.commit()
                return RELEASE_SAFE_OTHER_OWNER   # pausa de outro owner → preserva
            res = await session.execute(
                update(RiskState).where(
                    RiskState.id == 1, RiskState.trading_paused.is_(True),
                    RiskState.pause_reason.like(like),
                ).values(trading_paused=False, pause_manual=False, pause_reason=None,
                         paused_at=None, updated_at=datetime.now(timezone.utc)))
            await session.commit()
            if (res.rowcount or 0) == 1:
                log.warning("[circuit-breaker] P03 resume (zero incidentes, mesma txn) — pausa removida")
                return RELEASE_RELEASED
            return RELEASE_SAFE_OTHER_OWNER
        except Exception as exc:  # noqa: BLE001
            try:
                await session.rollback()
            except Exception:
                pass
            log.warning(f"[circuit-breaker] release P03 ERROR (fail-closed): {exc}")
            return RELEASE_ERROR


async def list_events(days: int = 30, limit: int = 200) -> list[dict]:
    """Lista eventos do circuit breaker dos últimos N dias (mais recentes primeiro)."""
    if not DB_ENABLED:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    async with get_session() as session:
        stmt = (
            select(RiskEvent)
            .where(RiskEvent.ts >= since)
            .order_by(RiskEvent.ts.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "event_type": r.event_type,
                "reason": r.reason,
                "daily_dd_pct": round(r.daily_dd_pct, 3) if r.daily_dd_pct is not None else None,
                "weekly_dd_pct": round(r.weekly_dd_pct, 3) if r.weekly_dd_pct is not None else None,
                "daily_trades": r.daily_trades,
                "weekly_trades": r.weekly_trades,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ]


def _to_dict(state: RiskState) -> dict:
    return {
        "enabled": True,
        "trading_paused": state.trading_paused,
        "pause_reason": state.pause_reason,
        "pause_manual": state.pause_manual,
        "paused_at": state.paused_at.isoformat() if state.paused_at else None,
        "daily_dd_pct": round(state.daily_dd_pct, 3),
        "weekly_dd_pct": round(state.weekly_dd_pct, 3),
        "daily_trades": state.daily_trades,
        "weekly_trades": state.weekly_trades,
        "daily_limit_pct": DAILY_DD_LIMIT_PCT,
        "weekly_limit_pct": WEEKLY_DD_LIMIT_PCT,
        "current_day_utc": state.current_day_utc,
        "current_week_utc": state.current_week_utc,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }
