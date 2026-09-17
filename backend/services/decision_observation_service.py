"""R09: funil POST_SELECTION e replay de rejeições, isolados da operação.

Hooks são síncronos, limitados e sem IO. O ciclo existente chama flush_pending
depois da execução. Não há worker, create_task, fetch, notifier nem escrita em
snapshots/trades/risco/calibração. Erros observacionais nunca autorizam gates.

Semântica de "primeiro": `first_seen_at` é a observação MAIS ANTIGA já
persistida; decisão/bloqueio/setup/config são os da PRIMEIRA tentativa
PERSISTIDA e nunca são reescritos. Com vários processos, uma observação mais
antiga pode chegar depois — isso fica contado em `out_of_order_first`.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from db import DB_ENABLED, get_session
from models.decision_observation import (
    DecisionObservation as OpportunityRow,
    DecisionObservationAttempt as AttemptRow,
    RejectedSetupObservation as RejectedRow,
)

SCHEMA_VERSION = "r09.v1"
MAX_PENDING = 500
MAX_SYMBOLS = 64
MAX_CANDLES = 96
MAX_RESOLVE_ROWS = 100
MAX_ADMISSION_RETRIES = 3
# Defesa: tentativa não selada há mais que isto só é descartada quando o
# buffer lota (nunca persistida pela metade, nunca selada à força).
STALE_UNSEALED_S = 3600
FLUSH_TIMEOUT_S = 1.5
BAR_MS = 300_000
# snapshot_service busca `_resolver_fetch_ohlcv(symbol, "5m", 50)`: uma vela
# mais antiga que isso nunca mais chega por essa fonte compartilhada.
RESOLVER_LOOKBACK_BARS = 50
# Tetos FIXOS de admissão no banco (sem ENV). Nada é apagado ao atingi-los:
# novas linhas são descartadas e contadas; o resolver continua nos antigos.
CAPACITY = {"opportunities": 50_000, "attempts": 100_000, "rejected": 50_000}
CAPACITY_NEAR_RATIO = 0.9
# Lock transacional próprio do R09. Distinto do 917283 (P03/risco).
R09_ADVISORY_LOCK_KEY = 0x52303943  # "R09C"
POLICY = "ISOLATED_REPLAY_NOT_LIVE"
PRICE_SOURCE = "SNAPSHOT_RESOLVER_WINDOW_UNLABELED"
REPLAY_CONFIG_KEYS = ("bar_ms", "entry_window_bars", "pre_tp1_time_stop_bars",
                      "max_holding_bars", "tp1_fraction", "be_lock_fraction",
                      "trail_atr_multiple", "trail_activation_atr", "max_bars")
# Mapa EXPLÍCITO status do replay R10 -> cobertura R09. GAP/PENDING só
# viram terminais quando a vela faltante já não pode chegar pela fonte.
REPLAY_COVERAGE = {
    "CLOSED_STOP": "RESOLVED",
    "CLOSED_RUNNER_STOP": "RESOLVED",
    "CLOSED_TP2": "RESOLVED",
    "CLOSED_TIME_STOP": "RESOLVED",
    "CLOSED_MAX_HOLD": "RESOLVED",
    "NOT_FILLED": "NOT_FILLED",
    "AMBIGUOUS_ENTRY_BAR": "AMBIGUOUS",
    "MISSING_OR_UNORDERED_BARS": "GAP",
    "INSUFFICIENT_DATA": "PENDING",
}
TERMINAL_COVERAGE = ("RESOLVED", "NOT_FILLED", "AMBIGUOUS", "INVALID",
                     "DATA_GAP_FINAL", "EXPIRED_INCOMPLETE")
OPEN_COVERAGE = ("UNAVAILABLE", "PENDING", "GAP_PENDING")
ORDER_SEMANTICS = {
    "first_seen_at": "EARLIEST_PERSISTED_OBSERVATION",
    "first_decision": "FIRST_PERSISTED_ATTEMPT",
    "first_blocker": "FIRST_PERSISTED_BLOCKER",
    "frozen_setup": "FIRST_PERSISTED_ATTEMPT",
}
_pending: OrderedDict[str, dict] = OrderedDict()
_windows: OrderedDict[str, dict] = OrderedDict()
_stats: Counter = Counter()
_last_flush_at: str | None = None
_flushing = False
# None = ainda não consultado (boot): aceita até MAX_SYMBOLS. Depois, só
# símbolos com vetadas abertas, para o teto não descartar os que importam.
_wanted_symbols: set[str] | None = None
_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "policy": POLICY,
    "scope": "POST_SELECTION",
    "bar_ms": BAR_MS,
    "entry_window_bars": 3,
    "pre_tp1_time_stop_bars": 12,
    "max_holding_bars": 24,
    "tp1_fraction": 0.45,
    "be_lock_fraction": 0.2,
    "trail_atr_multiple": 2.2,
    "trail_activation_atr": 0.5,
    "max_bars": MAX_CANDLES,
    "horizon_semantics": "SHORT_RESEARCH_HORIZON_NOT_LIVE_TIME_STOP",
    "cost_status": "UNKNOWN",
    "learning_eligible": False,
}
_STATES = {
    "OBSERVED", "INELIGIBLE", "REJECTED", "ATTEMPTED", "NO_FILL",
    "FAILED", "OPENED", "PAPER_OPENED", "UNKNOWN", "INCIDENT",
    "PERSISTENCE_FAILED",
}
_TELEMETRY_KEYS = (
    "identity_missing", "buffer_dropped", "stage_errors", "flush_errors",
    "flush_timeouts", "flush_cancelled", "resolver_errors", "resolver_cas_conflicts",
    "db_disabled_dropped", "persistence_dropped", "candle_symbols_dropped",
    "invalid_or_unclosed_candles", "capacity_dropped_opportunities",
    "capacity_dropped_attempts", "capacity_dropped_rejected",
    "capacity_lock_contention", "contention_requeued", "contention_dropped",
    "stale_unsealed_evicted",
)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _label(value, max_len=64):
    if not isinstance(value, str) or len(value) > max_len:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_+:/.-]+", value) else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def frozen_setup(rec: dict) -> dict:
    """Allowlist explícita: nunca serializa a recomendação ou erro integral."""
    signal = rec.get("signal") if isinstance(rec.get("signal"), dict) else {}
    indicators = signal.get("indicators") or {}
    if not isinstance(indicators, dict):
        indicators = {}
    proof = rec.get("data_freshness") or signal.get("data_freshness") or {}
    candle = proof.get("candle") if isinstance(proof, dict) else {}
    candle = candle if isinstance(candle, dict) else {}
    result = {key: _label(rec.get(key), 50) for key in
              ("symbol", "timeframe", "direction", "tier")}
    for key in ("entry", "stop_loss", "tp2", "score", "prob_tp1", "prob_tp2",
                "risk_reward", "risk_pct"):
        result[key] = _number(rec.get(key))
    result.update(tp1=_number(signal.get("tp1", rec.get("tp1"))),
                  atr=_number(indicators.get("atr", rec.get("atr"))),
                  candle_open_ms=_number(candle.get("open_time_ms")),
                  candle_close_ms=_number(candle.get("close_time_ms")),
                  candle_source=_label(candle.get("source"), 40))
    return result


def opportunity_identity(rec: dict, setup: dict | None = None):
    """Sem busca 'latest snapshot'. Sem horário atual como identidade sintética."""
    setup = setup or frozen_setup(rec)
    snapshot_id = rec.get("_snapshot_id", rec.get("snapshot_id"))
    if isinstance(snapshot_id, (int, str)) and not isinstance(snapshot_id, bool):
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(snapshot_id)):
            return _digest(["snapshot", str(snapshot_id)]), "SNAPSHOT_ID"
    close_ms = setup.get("candle_close_ms")
    if (not close_ms or close_ms <= 0 or
            any(setup.get(key) is None for key in
                ("symbol", "timeframe", "direction", "entry", "stop_loss"))):
        return None, "MISSING_IDENTITY"
    identity = {key: setup.get(key) for key in
                ("symbol", "timeframe", "direction", "entry", "stop_loss", "tp1", "tp2")}
    identity["candle_close_ms"] = close_ms
    return _digest(identity), "SETUP_CANDLE"


def _trace(rec):
    try:
        from services.score_trace_service import freeze_trace
        source = rec.get("_r09_score_trace") or rec.get("r08_score_trace")
        return freeze_trace(source) if isinstance(source, dict) else None
    except Exception:
        return None


def _evict_stale_unsealed(now: datetime) -> None:
    cutoff = now - timedelta(seconds=STALE_UNSEALED_S)
    for key in [k for k, row in _pending.items()
                if not row["sealed"] and row["observed_at"] < cutoff]:
        _pending.pop(key, None)
        _stats["stale_unsealed_evicted"] += 1


def begin_batch(recs: list[dict], *, mode="UNKNOWN", config: dict | None = None) -> None:
    """Denominador é EXATAMENTE a lista entregue à execução, antes dos gates."""
    for rec in recs:
        try:
            if not isinstance(rec, dict):
                _stats["identity_missing"] += 1
                continue
            # Handles privados são sempre novos por invocação; oportunidade não.
            rec.pop("_r09_attempt_id", None)
            rec.pop("_r09_score_trace", None)
            setup = frozen_setup(rec)
            key, source = opportunity_identity(rec, setup)
            if key is None:
                _stats["identity_missing"] += 1
                continue
            now = datetime.now(timezone.utc)
            if len(_pending) >= MAX_PENDING:
                _evict_stale_unsealed(now)
            if len(_pending) >= MAX_PENDING:
                _stats["buffer_dropped"] += 1
                continue
            attempt = str(uuid4())
            cfg = dict(_CONFIG)
            # Valores realmente configurados no caller, só allowlist numérica.
            cfg["execution_gates"] = {
                name: _number((config or {}).get(name)) for name in
                ("score_min", "score_adjuster_cap", "min_prob_tp1", "min_rr_tp1", "min_rr_tp2")
            }
            cfg["version_hash"] = _digest(cfg)
            _pending[attempt] = {
                "opportunity_key": key, "identity_source": source,
                "symbol": setup.get("symbol") or "UNKNOWN", "observed_at": now,
                "mode": mode if mode in {"LIVE", "SHADOW"} else "UNKNOWN",
                "frozen_setup": setup, "frozen_config": cfg,
                "result": "OBSERVED", "first_blocker": None,
                "submit_evidence": "NOT_OBSERVED", "score_trace": _trace(rec),
                "rejected_at": None, "sealed": False, "admission_retries": 0,
            }
            rec["_r09_attempt_id"] = attempt
        except Exception:
            _stats["stage_errors"] += 1


def stage_decision(rec: dict, decision: str, reason_code: str | None = None) -> None:
    try:
        row = _pending.get(rec.get("_r09_attempt_id"))
        if row is None:
            return
        if decision not in _STATES:
            decision = "UNKNOWN"
        # Terminal state is not downgraded by generic exception/finally hooks.
        if row["result"] in {"OPENED", "PAPER_OPENED", "INCIDENT"} and decision == "UNKNOWN":
            return
        row["result"] = decision
        row["score_trace"] = _trace(rec) or row["score_trace"]
        if decision == "REJECTED" and row["first_blocker"] is None:
            row["first_blocker"] = _label(reason_code) or "UNKNOWN_GATE"
            row["rejected_at"] = datetime.now(timezone.utc)
            if _wanted_symbols is not None and row["symbol"] != "UNKNOWN":
                _wanted_symbols.add(row["symbol"])
    except Exception:
        _stats["stage_errors"] += 1


def _preflight_stage(result: dict) -> str:
    """P04A = revalidação da LIMIT maker; P04B = depth/VWAP da MARKET (inclui fallback)."""
    if result.get("was_maker") is True and result.get("fell_back_to_market") is not True:
        return "P04A_MAKER"
    return "P04B_MARKET"


def stage_result(rec: dict, result: dict) -> None:
    """Chamada ao transport não prova POST/fill. Guarda somente evidência explícita."""
    try:
        row = _pending.get(rec.get("_r09_attempt_id"))
        if row is None:
            return
        if not isinstance(result, dict):
            stage_decision(rec, "UNKNOWN")
            return
        raw = result.get("result") or {}
        raw = raw if isinstance(raw, dict) else {}
        if result.get("entry_not_submitted") is True:
            row["submit_evidence"] = "NOT_SUBMITTED"
        elif raw.get("orderId") or raw.get("orderID"):
            row["submit_evidence"] = "SUBMITTED"
        else:
            row["submit_evidence"] = "UNKNOWN"
        reason = None
        if (result.get("manual_intervention_required") or result.get("quarantine_required")
                or result.get("emergency_close_attempted")):
            state = "INCIDENT"
        elif result.get("preflight_failed"):
            state = "REJECTED"
            code = _label(result.get("reason_code"), 48) or "ENTRY_PREFLIGHT"
            reason = f"{_preflight_stage(result)}:{code}"
        elif result.get("no_fill"):
            state = "NO_FILL"
        elif result.get("ok"):
            state = "ATTEMPTED"  # RealTrade persistido é observado depois.
        else:
            state = "FAILED"
        stage_decision(rec, state, reason)
    except Exception:
        _stats["stage_errors"] += 1


def _valid_bar(item) -> bool:
    values = [item.get(k) for k in ("open", "high", "low", "close")]
    if any(v is None or v <= 0 for v in values) or item.get("volume") is None or item["volume"] < 0:
        return False
    open_, high, low, close = values
    return low <= min(open_, close) and max(open_, close) <= high


async def observe_candles(symbol: str, candles, *, as_of: datetime | None = None) -> None:
    """Reaproveita só candles JÁ buscados. Nenhum IO, nenhuma inferência de cauda."""
    try:
        symbol = _label(symbol, 50)
        if not symbol:
            return
        if _wanted_symbols is not None and symbol not in _wanted_symbols:
            return
        if symbol not in _windows and len(_windows) >= MAX_SYMBOLS:
            _stats["candle_symbols_dropped"] += 1
            return
        now = as_of or datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        rows = candles.tail(MAX_CANDLES).to_dict("records") if hasattr(candles, "tail") else list(candles)[-MAX_CANDLES:]
        existing = {x["timestamp"]: x for x in _windows.get(symbol, {}).get("candles", [])}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            item = {k: _number(raw.get(k)) for k in ("timestamp", "open", "high", "low", "close", "volume")}
            ts = item["timestamp"]
            if (ts is None or ts <= 0 or ts % BAR_MS or ts + BAR_MS > now_ms
                    or not _valid_bar(item)):
                _stats["invalid_or_unclosed_candles"] += 1
                continue
            item["timestamp"] = int(ts)
            existing[item["timestamp"]] = item
        if existing:
            _windows[symbol] = {"candles": [existing[k] for k in sorted(existing)[-MAX_CANDLES:]], "as_of": now}
    except Exception:
        _stats["resolver_errors"] += 1


def seal_batch(recs: list[dict]) -> None:
    """Finally do executor: libera persistência, inclusive após exception/abort.

    Remove os handles privados da rec: a mesma lista pode voltar à API/UI.
    """
    for rec in recs:
        try:
            row = _pending.get(rec.pop("_r09_attempt_id", None))
            rec.pop("_r09_score_trace", None)
            if row is not None:
                if row["result"] == "OBSERVED":
                    row["result"] = "UNKNOWN"
                row["sealed"] = True
        except Exception:
            _stats["stage_errors"] += 1


def _opportunity_values(row):
    return dict(
        opportunity_key=row["opportunity_key"], identity_source=row["identity_source"],
        scope="POST_SELECTION", symbol=row["symbol"], first_seen_at=row["observed_at"],
        last_seen_at=row["observed_at"], first_decision=row["result"],
        first_decision_observed_at=row["observed_at"],
        first_blocker=row["first_blocker"], frozen_setup=row["frozen_setup"],
        frozen_config=row["frozen_config"], score_trace=row["score_trace"],
    )


def _opportunity_upsert(row):
    values = [_opportunity_values(x) for x in row] if isinstance(row, list) else _opportunity_values(row)
    stmt = insert(OpportunityRow).values(values)
    return stmt.on_conflict_do_update(index_elements=["opportunity_key"], set_={
        "first_seen_at": func.least(OpportunityRow.first_seen_at, stmt.excluded.first_seen_at),
        "last_seen_at": func.greatest(OpportunityRow.last_seen_at, stmt.excluded.last_seen_at),
        # CAS/coalesce: retry nunca apaga setup, configuração, primeira decisão ou blocker.
        "first_decision": func.coalesce(OpportunityRow.first_decision, stmt.excluded.first_decision),
        "first_decision_observed_at": func.coalesce(
            OpportunityRow.first_decision_observed_at, stmt.excluded.first_decision_observed_at),
        "first_blocker": func.coalesce(OpportunityRow.first_blocker, stmt.excluded.first_blocker),
    })


def _safe_outcome(outcome, reason=None):
    """Somente campos declarados; gross_r só em trajetória fechada, net_r nunca."""
    from services.offline_replay_service import CLOSED_STATUSES, REPLAY_STATUSES, SCHEMA_VERSION as REPLAY_SCHEMA
    outcome = outcome if isinstance(outcome, dict) else {}
    status = outcome.get("status") if outcome.get("status") in REPLAY_STATUSES else None
    codes = [code for code in (_label(x) for x in list(outcome.get("reason_codes") or [])[:12]) if code]
    if reason:
        codes.append(reason)
    observed = outcome.get("bars_observed")
    return {"policy": POLICY, "replay_schema": REPLAY_SCHEMA, "status": status,
            "reason_codes": codes, "filled": outcome.get("filled") is True,
            "tp1_hit": outcome.get("tp1_hit") is True,
            "bars_observed": observed if isinstance(observed, int) and not isinstance(observed, bool) else None,
            "gross_r": _number(outcome.get("gross_r")) if status in CLOSED_STATUSES else None,
            "net_r": None, "cost_status": "UNKNOWN", "learning_eligible": False,
            "price_source": PRICE_SOURCE}


def _replay_rejected(row, shared):
    """Cópia isolada, parâmetros congelados; replay puro R10, sem globais live.

    Devolve (velas, cobertura, outcome). Nunca converte lacuna/ambiguidade em
    resolução, nem inventa R. Setup/config inválidos viram INVALID terminal.
    """
    from services import offline_replay_service as replay
    decision_ms = int(row.decision_at.timestamp() * 1000)
    first_ms = ((decision_ms + BAR_MS - 1) // BAR_MS) * BAR_MS
    stored = row.candles if isinstance(row.candles, list) else []
    combined = {c["timestamp"]: c for c in stored
                if isinstance(c, dict) and isinstance(c.get("timestamp"), int)}
    for candle in shared["candles"]:
        if candle["timestamp"] >= first_ms:
            combined[candle["timestamp"]] = candle
    ordered = [combined[k] for k in sorted(combined)][:MAX_CANDLES]
    if not ordered:
        return ordered, "UNAVAILABLE", None
    setup = row.frozen_setup if isinstance(row.frozen_setup, dict) else {}
    cfg = row.frozen_config if isinstance(row.frozen_config, dict) else {}
    try:
        config = replay.ReplayConfig(**{k: cfg[k] for k in REPLAY_CONFIG_KEYS})
        opportunity = replay.Opportunity(
            opportunity_id=row.opportunity_key, symbol=row.symbol,
            direction=setup.get("direction"), decision_ts_ms=decision_ms,
            entry=setup.get("entry"), stop_loss=setup.get("stop_loss"),
            tp1=setup.get("tp1"), tp2=setup.get("tp2"), atr=setup.get("atr"),
        )
    except (KeyError, TypeError, ValueError):
        return ordered, "INVALID", _safe_outcome(None, "INVALID_FROZEN_SETUP")
    try:
        bars = [replay.Candle(timestamp_ms=c["timestamp"], **{k: c[k] for k in
                ("open", "high", "low", "close", "volume")}) for c in ordered]
        outcome = replay.replay_opportunity(opportunity, bars, config, replay.CostConfig())
    except (KeyError, TypeError, ValueError):
        return ordered, "INVALID", _safe_outcome(None, "INVALID_REPLAY_INPUT")
    coverage = REPLAY_COVERAGE.get(outcome.get("status"))
    if coverage is None:
        return ordered, "INVALID", _safe_outcome(outcome, "UNMAPPED_REPLAY_STATUS")
    if coverage in ("GAP", "PENDING"):
        # Próxima vela necessária; só pode chegar se ainda estiver na janela.
        next_ms = first_ms + outcome["bars_observed"] * config.bar_ms
        as_of_ms = int(shared["as_of"].timestamp() * 1000)
        recoverable = next_ms >= as_of_ms - RESOLVER_LOOKBACK_BARS * BAR_MS
        if coverage == "GAP":
            coverage = "GAP_PENDING" if recoverable else "DATA_GAP_FINAL"
        else:
            coverage = "PENDING" if recoverable else "EXPIRED_INCOMPLETE"
    return ordered, coverage, _safe_outcome(outcome)


def _group(batch):
    """Ordem cronológica do processo; trace completo UMA vez por oportunidade."""
    opportunities, attempts, rejected = {}, [], {}
    for attempt_id, row in sorted(batch, key=lambda item: item[1]["observed_at"]):
        key = row["opportunity_key"]
        if key not in opportunities:
            opportunities[key] = dict(row)
        elif not opportunities[key]["first_blocker"]:
            opportunities[key]["first_blocker"] = row["first_blocker"]
        # Retry guarda só estágio de execução (~400 bytes), nunca a confluência.
        execution = ((row["score_trace"] or {}).get("stages") or {}).get("execution_score")
        attempts.append(dict(attempt_id=attempt_id, score_trace=(
            {"stages": {"execution_score": execution}} if execution else None), **{
            k: row[k] for k in ("opportunity_key", "observed_at", "mode", "result",
                               "first_blocker", "submit_evidence")}))
        if row["rejected_at"] and row["first_blocker"] and key not in rejected:
            rejected[key] = dict(
                opportunity_key=key, symbol=row["symbol"],
                decision_at=row["rejected_at"], first_blocker=row["first_blocker"],
                frozen_setup=row["frozen_setup"], frozen_config=row["frozen_config"],
                coverage="UNAVAILABLE", candles=[], outcome=None, version=0,
                updated_at=row["observed_at"],
            )
    return opportunities, attempts, rejected


async def _admit(session, batch) -> str:
    """Capacidade + insert na MESMA transação, sob lock próprio. Uma checagem por lote."""
    locked = (await session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                                    {"k": R09_ADVISORY_LOCK_KEY})).scalar()
    if not locked:
        _stats["capacity_lock_contention"] += 1
        return "CONTENTION"
    opportunities, attempts, rejected = _group(batch)
    existing = set((await session.execute(select(OpportunityRow.opportunity_key).where(
        OpportunityRow.opportunity_key.in_(list(opportunities))))).scalars().all())
    existing_rejected = set((await session.execute(select(RejectedRow.opportunity_key).where(
        RejectedRow.opportunity_key.in_(list(rejected))))).scalars().all()) if rejected else set()
    free = {}
    for name, model in (("opportunities", OpportunityRow), ("attempts", AttemptRow),
                        ("rejected", RejectedRow)):
        used = await session.scalar(select(func.count()).select_from(model))
        free[name] = max(0, CAPACITY[name] - int(used or 0))
    # Reavaliação não consome vaga. Oportunidade nova exige vaga para ela E
    # para a primeira tentativa: nunca fica oportunidade órfã por capacidade.
    admitted, reserved = set(existing), set()
    for key in opportunities:
        if key in admitted:
            continue
        if free["opportunities"] > 0 and free["attempts"] > 0:
            admitted.add(key)
            reserved.add(key)
            free["opportunities"] -= 1
            free["attempts"] -= 1
        else:
            _stats["capacity_dropped_opportunities"] += 1
    kept_attempts = []
    for attempt in attempts:
        key = attempt["opportunity_key"]
        if key in reserved:
            reserved.discard(key)
            kept_attempts.append(attempt)
        elif key in admitted and free["attempts"] > 0:
            kept_attempts.append(attempt)
            free["attempts"] -= 1
        else:
            _stats["capacity_dropped_attempts"] += 1
    kept_rejected = []
    for key, row in rejected.items():
        if key in existing_rejected:
            continue
        if key in admitted and free["rejected"] > 0:
            kept_rejected.append(row)
            free["rejected"] -= 1
        else:
            _stats["capacity_dropped_rejected"] += 1
    values = [row for key, row in opportunities.items() if key in admitted]
    if values:
        await session.execute(_opportunity_upsert(values))
    if kept_attempts:
        stmt = insert(AttemptRow).values(kept_attempts)
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["attempt_id"]))
    if kept_rejected:
        stmt = insert(RejectedRow).values(kept_rejected)
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["opportunity_key"]))
    return "ADMITTED"


async def _resolve(session, windows) -> None:
    """Antigos continuam sendo resolvidos mesmo com a admissão lotada."""
    global _wanted_symbols
    if windows:
        rows = (await session.execute(select(RejectedRow).where(
            RejectedRow.symbol.in_(list(windows)),
            RejectedRow.coverage.notin_(TERMINAL_COVERAGE),
        ).order_by(RejectedRow.updated_at).limit(MAX_RESOLVE_ROWS))).scalars().all()
        for row in rows:
            shared = windows[row.symbol]
            try:
                candles, coverage, outcome = _replay_rejected(row, shared)
            except Exception:
                # Função pura: exceção inesperada não é transitória. Terminal,
                # para não travar a fila ordenada por updated_at.
                _stats["resolver_errors"] += 1
                candles, coverage = row.candles or [], "INVALID"
                outcome = _safe_outcome(None, "REPLAY_ERROR")
            # Optimistic CAS: concurrent resolver cannot overwrite a newer result.
            result = await session.execute(update(RejectedRow).where(
                RejectedRow.opportunity_key == row.opportunity_key,
                RejectedRow.version == row.version,
            ).values(candles=candles, coverage=coverage, outcome=outcome,
                     version=row.version + 1, updated_at=shared["as_of"]))
            if result.rowcount != 1:
                _stats["resolver_cas_conflicts"] += 1
    # MAX_SYMBOLS limita janelas de velas, não a assinatura. Os símbolos
    # sem velas podem ficar antigos indefinidamente; selecionar só os 64
    # primeiros impediria observar os demais mesmo com memória disponível.
    # A lista distinta continua limitada pela capacidade da tabela R09.
    wanted = (await session.execute(select(RejectedRow.symbol).where(
        RejectedRow.coverage.notin_(TERMINAL_COVERAGE),
    ).distinct())).scalars().all()
    staged = {row["symbol"] for row in _pending.values() if row["rejected_at"]}
    _wanted_symbols = set(wanted) | staged


async def _flush_batch(batch, windows, progress: dict | None = None):
    """Admissão commitada antes do resolver: timeout no replay não perde o lote."""
    progress = progress if progress is not None else {}
    async with get_session() as session:
        if batch:
            progress["admission"] = await _admit(session, batch)
            await session.commit()
            progress["admission_committed"] = True
        await _resolve(session, windows)
        await session.commit()
    return progress.get("admission")


def _requeue(batch) -> None:
    for key, row in batch:
        row["admission_retries"] = row.get("admission_retries", 0) + 1
        if row["admission_retries"] <= MAX_ADMISSION_RETRIES and len(_pending) < MAX_PENDING:
            _pending[key] = row
            _stats["contention_requeued"] += 1
        else:
            _stats["contention_dropped"] += 1


def _account_failure(batch, progress) -> None:
    if not progress.get("admission_committed"):
        _stats["persistence_dropped"] += len(batch)
    elif progress.get("admission") == "CONTENTION":
        _requeue(batch)


async def flush_pending() -> None:
    """Um lote best-effort limitado no ciclo existente, depois de executar recs."""
    global _flushing, _last_flush_at
    if _flushing:
        return
    _flushing = True
    batch = [(key, row) for key, row in _pending.items() if row["sealed"]]
    windows = dict(_windows)
    for key, _ in batch:
        _pending.pop(key, None)
    _windows.clear()
    progress: dict = {}
    try:
        if not batch and not windows:
            return
        if not DB_ENABLED:
            _stats["db_disabled_dropped"] += len(batch)
            return
        await asyncio.wait_for(_flush_batch(batch, windows, progress=progress),
                               timeout=FLUSH_TIMEOUT_S)
        if progress.get("admission") == "CONTENTION":
            _requeue(batch)
        _last_flush_at = datetime.now(timezone.utc).isoformat()
    except asyncio.CancelledError:
        _stats["flush_cancelled"] += 1
        _account_failure(batch, progress)
        raise
    except TimeoutError:
        _stats["flush_timeouts"] += 1
        _account_failure(batch, progress)
    except Exception:
        _stats["flush_errors"] += 1
        _account_failure(batch, progress)
    finally:
        _flushing = False


def _telemetry():
    return {**{k: _stats[k] for k in _TELEMETRY_KEYS},
            "counter_scope": "PROCESS_SINCE_BOOT", "pending_attempts": len(_pending),
            "pending_symbols": len(_windows), "last_flush_at": _last_flush_at,
            "wanted_symbols": None if _wanted_symbols is None else len(_wanted_symbols)}


def _capacity_view(used: dict | None) -> dict:
    view = {"limits": dict(CAPACITY), "used": used, "state": "UNKNOWN",
            "admission_blocked": None, "policy": "DROP_NEW_KEEP_RESOLVING"}
    if not used:
        return view
    ratios = [used[name] / CAPACITY[name] for name in CAPACITY]
    view["admission_blocked"] = any(used[name] >= CAPACITY[name] for name in CAPACITY)
    view["state"] = ("AT_LIMIT" if view["admission_blocked"] else
                     "NEAR_LIMIT" if max(ratios) >= CAPACITY_NEAR_RATIO else "OK")
    return view


async def get_status(days=7) -> dict:
    """Read-only agregado. Não revela outcomes econômicos nem conjunto holdout."""
    try:
        days = min(30, max(1, int(days)))
    except (ValueError, TypeError, OverflowError):
        days = 7
    status = {"schema_version": SCHEMA_VERSION, "state": "UNAVAILABLE",
              "scope": "POST_SELECTION", "days": days, "unique_opportunities": None,
              "attempts": None, "reevaluations": None, "first_decisions": {},
              "first_blockers": {}, "attempt_results": {}, "submit_evidence": {},
              "order_semantics": dict(ORDER_SEMANTICS), "out_of_order_first": None,
              "rejected_shadow": {"total": None, "coverage": {}, "policy": POLICY,
                                  "terminal": list(TERMINAL_COVERAGE),
                                  "economic_outcomes_exposed": False},
              "capacity": _capacity_view(None),
              "trace_coverage": {"recommendation": 0, "execution": 0}, "telemetry": _telemetry()}
    if not DB_ENABLED:
        return status
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with get_session() as session:
            async def counts(column, date_column):
                rows = (await session.execute(select(column, func.count()).where(
                    date_column >= cutoff).group_by(column).limit(64))).all()
                return {str(key or "UNKNOWN"): count for key, count in rows}
            status["first_decisions"] = await counts(OpportunityRow.first_decision, OpportunityRow.first_seen_at)
            status["unique_opportunities"] = sum(status["first_decisions"].values())
            status["first_blockers"] = await counts(OpportunityRow.first_blocker, OpportunityRow.first_seen_at)
            status["first_blockers"].pop("UNKNOWN", None)
            status["attempt_results"] = await counts(AttemptRow.result, AttemptRow.observed_at)
            status["attempts"] = sum(status["attempt_results"].values())
            # Janela por data de cada tabela: diferença é aproximada nas bordas.
            status["reevaluations"] = max(0, status["attempts"] - status["unique_opportunities"])
            status["submit_evidence"] = await counts(AttemptRow.submit_evidence, AttemptRow.observed_at)
            status["rejected_shadow"]["coverage"] = await counts(RejectedRow.coverage, RejectedRow.decision_at)
            status["rejected_shadow"]["total"] = sum(status["rejected_shadow"]["coverage"].values())
            status["out_of_order_first"] = (await session.scalar(select(func.count()).select_from(
                OpportunityRow).where(OpportunityRow.first_seen_at >= cutoff,
                    OpportunityRow.first_seen_at < OpportunityRow.first_decision_observed_at))) or 0
            status["trace_coverage"]["recommendation"] = (await session.execute(select(func.count()).select_from(
                OpportunityRow).where(OpportunityRow.first_seen_at >= cutoff,
                    OpportunityRow.score_trace["stages"]["final_score"]["value"].as_float().is_not(None)))).scalar() or 0
            status["trace_coverage"]["execution"] = (await session.execute(select(func.count()).select_from(
                AttemptRow).where(AttemptRow.observed_at >= cutoff,
                    AttemptRow.score_trace["stages"]["execution_score"]["value"].as_float().is_not(None)))).scalar() or 0
            used = {}
            for name, model in (("opportunities", OpportunityRow), ("attempts", AttemptRow),
                                ("rejected", RejectedRow)):
                used[name] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
            status["capacity"] = _capacity_view(used)
        status["state"] = "AVAILABLE"
    except Exception:
        status["state"] = "ERROR"
    return status
