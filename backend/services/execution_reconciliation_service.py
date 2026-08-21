"""
Execution Reconciliation (P03 + P03.1) — reconciliação PERSISTENTE de execução.

Torna persistente a reconciliação de ordens/posições/condicionais que fiquem
`UNKNOWN`, inclusive após restart:

    submissão → safety state P02 → incidente persistido → restart/reconciler
    → mesma ordem reconciliada → posição/condicionais confirmadas
    → PROTECTED | FLAT | MANUAL_REQUIRED → liberação segura da quarentena própria.

Invariantes (P03.1):
  - UNKNOWN nunca é FLAT; nunca reenvia entry; nunca cria MARKET;
  - nenhum TP com qty incerta; nenhuma ordem alheia cancelada;
  - nenhum latch alheio (P02/operador) liberado pelo P03;
  - processo com lease vencido NÃO atualiza estado nem muta a exchange.

P03 só CONSULTA (get_order/positionRisk/open_algo), pode CANCELAR maker/
condicionais EXATAS, e pode criar SOMENTE SL sob invariantes estritas — nunca
entry, nunca MARKET, nunca TP com qty incerta. Consome o formato NORMALIZADO
snake_case real de `get_open_algo_orders` (algo_id/client_algo_id/close_position/
reduce_only/quantity/side/trigger_price/type).
"""
from __future__ import annotations

import os
import math
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

log = logging.getLogger(__name__)

EXCHANGE_BINANCE = "binance"
_LATCH_OWNER = "p03"


class Kind:
    ENTRY_SUBMISSION_UNKNOWN = "ENTRY_SUBMISSION_UNKNOWN"
    ENTRY_ORDER_UNKNOWN = "ENTRY_ORDER_UNKNOWN"
    FINAL_FILL_QTY_UNKNOWN = "FINAL_FILL_QTY_UNKNOWN"
    CONDITIONAL_SUBMISSION_UNKNOWN = "CONDITIONAL_SUBMISSION_UNKNOWN"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    UNTRACKED_POSITION = "UNTRACKED_POSITION"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class State:
    OPEN = "OPEN"
    RECONCILING = "RECONCILING"
    PROTECTED = "PROTECTED"          # terminal seguro
    FLAT = "FLAT"                    # terminal seguro
    RETRY_PENDING = "RETRY_PENDING"  # segurança (continua pausado)
    MANUAL_REQUIRED = "MANUAL_REQUIRED"  # segurança (continua pausado)


_TERMINAL_SAFE = {State.PROTECTED, State.FLAT}
_ENTRY_KINDS = {Kind.ENTRY_SUBMISSION_UNKNOWN, Kind.ENTRY_ORDER_UNKNOWN, Kind.FINAL_FILL_QTY_UNKNOWN}
_CLEANUP_KINDS = {Kind.CONDITIONAL_SUBMISSION_UNKNOWN, Kind.CLEANUP_PENDING}
_MANUAL_KINDS = {Kind.UNTRACKED_POSITION, Kind.PERSISTENCE_FAILURE}
_TERMINAL_ORDER_STATUS = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}


def kind_from_safety_state(safety_state: Optional[str], *, closed: bool = False) -> str:
    """Mapeia o safety_state real do P02 para o Kind correto (não colapsa tudo em
    ENTRY_ORDER_UNKNOWN). Posição fechada com condicional possivelmente órfã →
    CLEANUP_PENDING; submissão de entry desconhecida → ENTRY_SUBMISSION_UNKNOWN."""
    s = str(safety_state or "").upper()
    if closed or "ROLLBACK" in s or "CLEANUP" in s or "AFTER_CLOSE" in s:
        return Kind.CLEANUP_PENDING
    if "CONDITIONAL" in s:
        return Kind.CONDITIONAL_SUBMISSION_UNKNOWN
    if "FILL" in s and "QTY" in s:
        return Kind.FINAL_FILL_QTY_UNKNOWN
    if "SUBMISSION_UNKNOWN" in s:
        return Kind.ENTRY_SUBMISSION_UNKNOWN
    return Kind.ENTRY_ORDER_UNKNOWN


# ── Config (defaults conservadores / fail-closed) ───────────────────────────
def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


RECONCILE_INTERVAL_S = _f("RECONCILE_INTERVAL_S", 30.0)
RECONCILE_BACKOFF_BASE_S = _f("RECONCILE_BACKOFF_BASE_S", 15.0)
RECONCILE_BACKOFF_MAX_S = _f("RECONCILE_BACKOFF_MAX_S", 900.0)
RECONCILE_CLEAN_GRACE = _i("RECONCILE_CLEAN_GRACE", 2)        # ciclos separados
RECONCILE_LEASE_S = _f("RECONCILE_LEASE_S", 120.0)
RECONCILE_MAX_ATTEMPTS = _i("RECONCILE_MAX_ATTEMPTS", 8)
RECONCILE_MAX_PER_CYCLE = _i("RECONCILE_MAX_PER_CYCLE", 10)
RECONCILE_CREATE_SL = _b("RECONCILE_CREATE_SL", True)         # criar SL sob invariantes
RECONCILE_STOP_TOL_FRAC = _f("RECONCILE_STOP_TOL_FRAC", 0.005)  # trigger dentro de 0,5%

_PROCESS_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_PAUSE_MARKER = "P03-QUARANTINE:"

_last_reconciliation_at: Optional[str] = None
_reconciler_running = False
_prev_open_count = 0          # p/ liberar quarentena SÓ na transição >0 → 0
_p03_latch_armed = False      # P03 realmente armou o latch?


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mask(v: Any) -> Optional[str]:
    s = str(v) if v is not None else None
    if not s:
        return s
    return s if len(s) <= 6 else f"{s[:3]}…{s[-3:]}"


def _finite(x) -> Optional[float]:
    """float finito ou None (rejeita NaN/inf/inválido)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _backoff_seconds(attempts: int) -> float:
    # attempts=1 → BASE (casa com a doc: 15s inicial), depois dobra até o teto.
    exp = RECONCILE_BACKOFF_BASE_S * (2 ** max(0, attempts - 1))
    return float(min(exp, RECONCILE_BACKOFF_MAX_S))


def _norm_entry_side(side) -> Optional[str]:
    s = str(side or "").strip().lower()
    if s in ("buy", "long"):
        return "BUY"
    if s in ("sell", "short"):
        return "SELL"
    return None


def _close_side(entry_side: str) -> str:
    return "SELL" if entry_side == "BUY" else "BUY"


# ════════════════════════════════════════════════════════════════════════════
#  NÚCLEO DE DECISÃO — puro (sem DB/exchange), 100% testável
# ════════════════════════════════════════════════════════════════════════════
def terminal_qty(order_res: dict) -> tuple[Optional[float], str]:
    """Qty terminal de uma ordem de entrada:
      - FILLED → qty confirmada; REJECTED → 0;
      - CANCELED/EXPIRED/EXPIRED_IN_MATCH → exige executedQty terminal EXPLÍCITA;
      - ausente/NaN/inf → None (FINAL_FILL_QTY_UNKNOWN).
    Fill pré-terminal é apenas lower bound (nunca qty final)."""
    if not order_res or not order_res.get("ok"):
        return None, "consulta inconclusiva"
    status = (order_res.get("status") or "").upper()
    raw = order_res.get("raw") or {}
    if status == "FILLED":
        qty = _finite(order_res.get("orig_qty")) or _finite(order_res.get("executed_qty"))
        if qty is None:
            return None, "FILLED sem qty numérica"
        return (qty, "filled") if qty > 0 else (None, "FILLED com qty<=0")
    if status == "REJECTED":
        return 0.0, "rejected"
    if status in _TERMINAL_ORDER_STATUS:
        if "executedQty" not in raw and order_res.get("executed_qty") is None:
            return None, f"{status} sem executedQty explícita"
        eq = _finite(order_res.get("executed_qty"))
        if eq is None:
            return None, f"{status} com executedQty inválida"
        return eq, f"{status.lower()}_terminal"
    return None, f"não terminal ({status or 'sem status'})"


def classify_entry(order_res: dict) -> tuple[str, Optional[float], str]:
    """(verdict, qty_final|None, motivo), verdict ∈ {RETRY, FILL_UNKNOWN, FLAT, PROTECTED}."""
    if not order_res or not order_res.get("ok"):
        return "RETRY", None, "consulta indisponível"
    status = (order_res.get("status") or "").upper()
    if status in ("", "NEW", "PARTIALLY_FILLED"):
        return "RETRY", None, f"ainda {status or 'sem status'}"
    qty, why = terminal_qty(order_res)
    if qty is None:
        return "FILL_UNKNOWN", None, why
    if qty <= 0:
        return "FLAT", 0.0, why
    return "PROTECTED", qty, why


def position_verdict(size: Optional[float], status: str) -> str:
    """None (stale/rate-limited/erro/UNKNOWN) NUNCA prova flat."""
    if size is None:
        return "UNKNOWN"
    return "FLAT" if abs(size) <= 0 else "OPEN"


def _order_identities(o: dict) -> set:
    """Identidades EXATAS de uma ordem do listing (snake_case real + camel legado)."""
    ids = set()
    if not isinstance(o, dict):
        return ids
    for fld in ("algo_id", "client_algo_id", "algoId", "clientAlgoId",
                "order_id", "client_order_id", "orderId", "clientOrderId"):
        v = o.get(fld)
        if v:
            ids.add(str(v))
    return ids


def _adopt_live_stop(inc: dict, qty: Optional[float], listing: dict) -> tuple[bool, Optional[str], str]:
    """Adota um SL vivo VÁLIDO (contrato P02). Consome snake_case real.
    Válido = é STOP, `algo_id` não vazio, lado de fechamento correto, E
    (`close_position=True`) OU (`reduce_only=True` e `quantity >= qty`); trigger
    dentro da tolerância quando há `planned_stop`. Retorna (ok, algo_id, detalhe)."""
    entry_side = _norm_entry_side(inc.get("side"))
    want_close = _close_side(entry_side) if entry_side else None
    planned_stop = _finite(inc.get("planned_stop"))
    for o in (listing.get("orders") or []):
        if not isinstance(o, dict):
            continue
        otype = str(o.get("type") or o.get("origType") or "").upper()
        if "STOP" not in otype:
            continue
        algo_id = o.get("algo_id") or o.get("algoId")
        if not algo_id:
            continue
        oside = str(o.get("side") or "").upper()
        if want_close and oside and oside != want_close:
            continue  # lado de fechamento errado
        close_position = bool(o.get("close_position") if o.get("close_position") is not None
                              else o.get("closePosition"))
        reduce_only = bool(o.get("reduce_only") if o.get("reduce_only") is not None
                           else o.get("reduceOnly"))
        if close_position:
            covered = True
        elif reduce_only:
            oq = _finite(o.get("quantity") if o.get("quantity") is not None else o.get("origQty"))
            covered = (qty is None) or (oq is not None and oq + 1e-12 >= float(qty))
        else:
            covered = False
        if not covered:
            continue
        if planned_stop is not None and planned_stop > 0:
            trig = _finite(o.get("trigger_price") if o.get("trigger_price") is not None
                           else o.get("triggerPrice") or o.get("stopPrice"))
            if trig is None or abs(trig - planned_stop) / planned_stop > RECONCILE_STOP_TOL_FRAC:
                continue  # trigger fora da tolerância P02
        return True, str(algo_id), f"SL vivo adotado ({_mask(algo_id)})"
    return False, None, "sem SL vivo válido"


# ════════════════════════════════════════════════════════════════════════════
#  Repositório de incidentes (persistente) + fallback/injeção em memória
# ════════════════════════════════════════════════════════════════════════════
_EXTRA_COLS = None  # colunas reais do model (cache)


def _model_cols() -> set:
    global _EXTRA_COLS
    if _EXTRA_COLS is None:
        try:
            from models.execution_incident import ExecutionIncident
            _EXTRA_COLS = {c.name for c in ExecutionIncident.__table__.columns}
        except Exception:
            _EXTRA_COLS = set()
    return _EXTRA_COLS


class InMemoryIncidentRepo:
    """Repositório em memória com a MESMA semântica de claim/lease/upsert do SQL.
    Fallback fail-closed quando DB off; injetado nos testes (sem tocar banco)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        async with self._lock:
            row = self._rows.get(key)
            if row is not None:
                return dict(row), False
            self._seq += 1
            row = {
                "id": self._seq, "incident_key": key, "attempts": 0,
                "clean_observations": 0, "state": State.OPEN,
                "claimed_by": None, "claimed_at": None, "lease_expires_at": None,
                "resolved_at": None, "created_at": _now(), "updated_at": _now(),
            }
            row.update({k: v for k, v in defaults.items() if v is not None or k in row})
            self._rows[key] = row
            return dict(row), True

    async def get(self, key: str) -> Optional[dict]:
        row = self._rows.get(key)
        return dict(row) if row else None

    async def list_open(self) -> list[dict]:
        return [dict(r) for r in self._rows.values() if r.get("resolved_at") is None]

    async def list_all(self) -> list[dict]:
        return [dict(r) for r in self._rows.values()]

    async def update(self, key: str, **fields) -> Optional[dict]:
        async with self._lock:
            row = self._rows.get(key)
            if not row:
                return None
            row.update(fields)
            row["updated_at"] = _now()
            return dict(row)

    async def update_claimed(self, key: str, owner: str, **fields) -> Optional[dict]:
        """Fenced: só aplica se `owner` detém o claim COM lease válido e o
        incidente ainda não foi resolvido (elegível)."""
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return None
            if row.get("claimed_by") != owner:
                return None
            exp = row.get("lease_expires_at")
            if exp is None or exp < _now():
                return None  # lease vencido → não atualiza
            row.update(fields)
            row["updated_at"] = _now()
            return dict(row)

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return False
            cur = row.get("claimed_by")
            exp = row.get("lease_expires_at")
            free = cur is None or (exp is not None and exp < _now())
            if not free and cur != owner:
                return False
            row["claimed_by"] = owner
            row["claimed_at"] = _now()
            row["lease_expires_at"] = lease_until
            return True

    async def renew_claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return False
            if row.get("claimed_by") != owner:
                return False
            row["lease_expires_at"] = lease_until
            return True

    async def release_claim(self, key: str, owner: Optional[str] = None) -> None:
        async with self._lock:
            row = self._rows.get(key)
            if not row:
                return
            if owner is not None and row.get("claimed_by") != owner:
                return  # não libera claim alheio
            row["claimed_by"] = None
            row["claimed_at"] = None
            row["lease_expires_at"] = None

    async def recover_expired_claims(self) -> int:
        async with self._lock:
            n, now = 0, _now()
            for row in self._rows.values():
                exp = row.get("lease_expires_at")
                if row.get("claimed_by") and exp is not None and exp < now:
                    row["claimed_by"] = row["claimed_at"] = row["lease_expires_at"] = None
                    n += 1
            return n


class _SqlIncidentRepo:
    """Repositório Postgres: upsert atômico (ON CONFLICT) + claim/fencing por UPDATE…WHERE."""

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        cols = {k: v for k, v in defaults.items() if k in _model_cols()}
        async with get_session() as session:
            stmt = (pg_insert(ExecutionIncident)
                    .values(incident_key=key, **cols)
                    .on_conflict_do_nothing(index_elements=["incident_key"]))
            res = await session.execute(stmt)
            await session.commit()
            created = (res.rowcount or 0) == 1
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            return (_row_to_dict(row) if row else {"incident_key": key}), created

    async def get(self, key: str) -> Optional[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_open(self) -> list[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            rows = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.resolved_at.is_(None))
            )).scalars().all()
            return [_row_to_dict(r) for r in rows]

    async def list_all(self) -> list[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            rows = (await session.execute(select(ExecutionIncident))).scalars().all()
            return [_row_to_dict(r) for r in rows]

    async def _apply(self, key, where_extra, fields) -> Optional[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update, select
        cols = {k: v for k, v in fields.items() if k in _model_cols()}
        cols["updated_at"] = _now()
        async with get_session() as session:
            stmt = update(ExecutionIncident).where(
                ExecutionIncident.incident_key == key, *where_extra).values(**cols)
            res = await session.execute(stmt)
            await session.commit()
            if (res.rowcount or 0) != 1:
                return None
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def update(self, key: str, **fields) -> Optional[dict]:
        return await self._apply(key, (), fields)

    async def update_claimed(self, key: str, owner: str, **fields) -> Optional[dict]:
        from models.execution_incident import ExecutionIncident
        return await self._apply(key, (
            ExecutionIncident.claimed_by == owner,
            ExecutionIncident.lease_expires_at > _now(),
            ExecutionIncident.resolved_at.is_(None),
        ), fields)

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update, or_
        now = _now()
        async with get_session() as session:
            stmt = (update(ExecutionIncident).where(
                ExecutionIncident.incident_key == key,
                ExecutionIncident.resolved_at.is_(None),
                or_(ExecutionIncident.claimed_by.is_(None),
                    ExecutionIncident.lease_expires_at < now),
            ).values(claimed_by=owner, claimed_at=now, lease_expires_at=lease_until))
            res = await session.execute(stmt)
            await session.commit()
            return (res.rowcount or 0) == 1

    async def renew_claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        from models.execution_incident import ExecutionIncident
        row = await self._apply(key, (
            ExecutionIncident.claimed_by == owner,
            ExecutionIncident.resolved_at.is_(None),
        ), {"lease_expires_at": lease_until})
        return row is not None

    async def release_claim(self, key: str, owner: Optional[str] = None) -> None:
        from models.execution_incident import ExecutionIncident
        where = () if owner is None else (ExecutionIncident.claimed_by == owner,)
        await self._apply(key, where, {"claimed_by": None, "claimed_at": None,
                                       "lease_expires_at": None})

    async def recover_expired_claims(self) -> int:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update
        now = _now()
        async with get_session() as session:
            stmt = (update(ExecutionIncident).where(
                ExecutionIncident.claimed_by.is_not(None),
                ExecutionIncident.lease_expires_at < now,
            ).values(claimed_by=None, claimed_at=None, lease_expires_at=None))
            res = await session.execute(stmt)
            await session.commit()
            return res.rowcount or 0


def _row_to_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


_repo: Any = None


def _get_repo() -> Any:
    global _repo
    if _repo is None:
        try:
            from db import DB_ENABLED
        except Exception:
            DB_ENABLED = False
        _repo = _SqlIncidentRepo() if DB_ENABLED else InMemoryIncidentRepo()
    return _repo


def set_repo(repo: Any) -> None:
    global _repo
    _repo = repo


# ════════════════════════════════════════════════════════════════════════════
#  Quarentena com OWNERSHIP — P03 arma/limpa somente o próprio latch
# ════════════════════════════════════════════════════════════════════════════
async def _arm_quarantine(reason: str) -> None:
    """Arma o latch P03 (fail-closed imediato) e persiste a pausa SEM sobrescrever
    uma pausa manual/não-P03 do operador."""
    global _p03_latch_armed
    try:
        from services import shadow_trade_service
        shadow_trade_service._arm_execution_quarantine(reason, owner=_LATCH_OWNER)
        _p03_latch_armed = True
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha armando latch: {exc}")
    try:
        from services import risk_service
        status = await risk_service.get_status()
        r = (status or {}).get("pause_reason") or ""
        non_p03_pause = bool(status and status.get("trading_paused") and not r.startswith(_PAUSE_MARKER))
        if not non_p03_pause:
            await risk_service.set_manual_pause(True, f"{_PAUSE_MARKER} {reason}")
        # senão: preserva a pausa do operador; o latch P03 já bloqueia entradas.
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha persistindo pausa RiskState: {exc}")


async def _maybe_release_quarantine() -> bool:
    """Libera SOMENTE a quarentena PRÓPRIA do P03. Chamado apenas na transição
    de ≥1 incidente aberto para 0 (o caller garante). Exige: P03 armou o latch;
    zero incidentes abertos; pausa persistida é P03-owned. Nunca toca latch/pausa
    de P02 ou do operador."""
    global _p03_latch_armed
    if await _get_repo().list_open():
        return False
    if not _p03_latch_armed:
        return False
    try:
        from services import risk_service
        status = await risk_service.get_status()
    except Exception:
        return False
    reason = (status or {}).get("pause_reason") or ""
    paused = bool(status and status.get("trading_paused"))
    if paused and not reason.startswith(_PAUSE_MARKER):
        return False  # pausa trocada por humano/outro subsistema → não mexer
    try:
        from services import shadow_trade_service
        shadow_trade_service.clear_execution_quarantine(owner=_LATCH_OWNER)
        _p03_latch_armed = False
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] falha limpando latch próprio: {exc}")
    try:
        from services import risk_service
        if paused and reason.startswith(_PAUSE_MARKER):
            await risk_service.set_manual_pause(False, "P03: incidentes resolvidos")
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] falha resumindo RiskState: {exc}")
    log.warning("[p03] quarentena própria liberada — transição p/ zero incidentes")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Entrada oficial de incidentes (idempotente + reabre após resolução)
# ════════════════════════════════════════════════════════════════════════════
def build_incident_key(kind: str, symbol: str, *, exchange: str = EXCHANGE_BINANCE,
                       client_order_id: str = None, entry_order_id: str = None,
                       conditional_prefix: str = None, snapshot_id: str = None,
                       identity: str = None) -> str:
    ident = (client_order_id or entry_order_id or conditional_prefix
             or snapshot_id or identity or symbol)  # sem sufixo genérico "-"
    return f"{exchange}:{kind}:{symbol}:{ident}"


async def record_incident(*, kind: str, symbol: str, exchange: str = EXCHANGE_BINANCE,
                          client_order_id: str = None, entry_order_id: str = None,
                          side: str = None, planned_qty: float = None,
                          min_known_fill: float = None, planned_stop: float = None,
                          conditional_prefix: str = None, conditional_ids: dict = None,
                          safety_state: str = None, snapshot_id: str = None,
                          pending_maker: bool = None, payload: dict = None,
                          incident_key: str = None) -> dict:
    """Persiste (ou reabre/merge) um incidente e ARMA a quarentena. Fail-closed:
    o latch é armado mesmo se a persistência falhar. Idempotente por incident_key;
    incidente RESOLVIDO que reincide é REABERTO (não fica invisível pela unique key)."""
    key = incident_key or build_incident_key(
        kind, symbol, exchange=exchange, client_order_id=client_order_id,
        entry_order_id=entry_order_id, conditional_prefix=conditional_prefix,
        snapshot_id=snapshot_id)
    full_payload = dict(payload or {})
    for k, v in (("safety_state", safety_state), ("snapshot_id", snapshot_id),
                 ("pending_maker", pending_maker)):
        if v is not None:
            full_payload[k] = v
    repo = _get_repo()
    created = False
    row = None
    try:
        row, created = await repo.upsert(key, {
            "kind": kind, "symbol": symbol, "exchange": exchange, "side": side,
            "client_order_id": client_order_id, "entry_order_id": entry_order_id,
            "planned_qty": planned_qty, "min_known_fill": min_known_fill,
            "planned_stop": planned_stop, "conditional_prefix": conditional_prefix,
            "conditional_ids": conditional_ids, "payload": full_payload or None,
            "next_retry_at": _now(),
        })
        if not created and row:
            if row.get("resolved_at") is not None:
                # Reincidência do mesmo problema → REABRE (visível, reconciliável).
                await repo.update(key, state=State.OPEN, resolved_at=None, attempts=0,
                                  clean_observations=0, next_retry_at=_now(),
                                  last_error="reaberto: problema reincidiu")
                log.critical(f"[p03][incident] REABERTO key={key}")
            else:
                patch: dict = {}
                if min_known_fill is not None:
                    patch["min_known_fill"] = max(row.get("min_known_fill") or 0.0, min_known_fill)
                if conditional_ids and not row.get("conditional_ids"):
                    patch["conditional_ids"] = conditional_ids
                if conditional_prefix and not row.get("conditional_prefix"):
                    patch["conditional_prefix"] = conditional_prefix
                if planned_stop is not None and row.get("planned_stop") is None:
                    patch["planned_stop"] = planned_stop
                if planned_qty is not None and row.get("planned_qty") is None:
                    patch["planned_qty"] = planned_qty
                if patch:
                    await repo.update(key, **patch)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] persistência de incidente falhou ({key}): {exc}")
    await _arm_quarantine(f"incidente {kind} {symbol} ({_mask(client_order_id or entry_order_id)})")
    log.critical(f"[p03][incident] key={key} kind={kind} symbol={symbol} created={created} "
                 f"coid={_mask(client_order_id)} eoid={_mask(entry_order_id)} maker={pending_maker}")
    return {"incident_key": key, "created": created, "state": (row or {}).get("state", State.OPEN)}


# ════════════════════════════════════════════════════════════════════════════
#  Reconciliação (fenced por owner+lease)
# ════════════════════════════════════════════════════════════════════════════
def _is_maker(inc: dict) -> bool:
    return bool((inc.get("payload") or {}).get("pending_maker"))


def _has_identity(inc: dict) -> bool:
    return bool(inc.get("client_order_id") or inc.get("entry_order_id")
                or inc.get("conditional_ids") or inc.get("conditional_prefix")
                or inc.get("kind") in _MANUAL_KINDS)


def _eligible(inc: dict) -> tuple[bool, str]:
    if inc.get("resolved_at") is not None:
        return False, "resolvido"
    if inc.get("state") == State.MANUAL_REQUIRED:
        return False, "manual_required"
    if (inc.get("exchange") or EXCHANGE_BINANCE) != EXCHANGE_BINANCE:
        return False, "exchange mismatch (bloqueado, sem mutação)"
    nra = inc.get("next_retry_at")
    if nra is not None and nra > _now():
        return False, "retry não elegível ainda"
    # NOTA: incidentes SEM identidade suficiente continuam elegíveis de propósito
    # — os caminhos de reconcile escalam para RETRY→MANUAL em vez de deixá-los
    # presos em OPEN/invisíveis. `_has_identity` orienta a decisão lá dentro.
    return True, "elegível"


async def _fenced(key: str, owner: str, **fields) -> bool:
    return (await _get_repo().update_claimed(key, owner, **fields)) is not None


async def _renew_or_abort(key: str, owner: str) -> bool:
    """Renova o lease ANTES de mutar a exchange. Se o claim foi perdido (lease
    vencido / outro dono), aborta a mutação — não cancela/cria nada."""
    return await _get_repo().renew_claim(key, owner, _now() + timedelta(seconds=RECONCILE_LEASE_S))


async def _schedule_retry(key: str, owner: str, inc: dict, state: str, reason: str) -> None:
    attempts = int(inc.get("attempts") or 0) + 1
    if attempts >= RECONCILE_MAX_ATTEMPTS and state not in _TERMINAL_SAFE:
        ok = await _fenced(key, owner, state=State.MANUAL_REQUIRED, attempts=attempts,
                           last_error=reason,
                           manual_reason=f"máx. tentativas ({attempts}) sem prova de segurança: {reason}")
        if ok:
            log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (attempts={attempts}) {reason}")
        return
    delay = _backoff_seconds(attempts)
    ok = await _fenced(key, owner, state=state, attempts=attempts, last_error=reason,
                       next_retry_at=_now() + timedelta(seconds=delay))
    if ok:
        log.warning(f"[p03][transition] {key} → {state} attempt={attempts} "
                    f"next_retry_in={delay:.0f}s reason={reason}")


async def _resolve(key: str, owner: str, state: str, reason: str) -> None:
    ok = await _fenced(key, owner, state=state, resolved_at=_now(), last_error=reason,
                       claimed_by=None, claimed_at=None, lease_expires_at=None)
    if ok:
        log.warning(f"[p03][transition] {key} → {state} (RESOLVIDO) {reason}")


async def _fresh_verdict(symbol: str) -> tuple[str, Optional[float]]:
    from services import binance_signed_service as bss
    try:
        size, _status = await bss._fresh_position_size(symbol)
    except Exception:  # noqa: BLE001
        return "UNKNOWN", None
    return position_verdict(size, size if size is None else 0.0), size


def _exact_conditional_ids(inc: dict) -> list[str]:
    """IDs EXATOS conhecidos: SL/TP1/TP2 do dict + derivados do prefixo
    (`<prefix>-sl/-tp1/-tp2`). SEM prefix match amplo."""
    ids: list[str] = []
    cids = inc.get("conditional_ids") or {}
    if isinstance(cids, dict):
        for k in ("sl", "tp1", "tp2"):
            if cids.get(k):
                ids.append(str(cids[k]))
        extra = cids.get("ids")
        if isinstance(extra, list):
            ids.extend(str(x) for x in extra if x)
    pref = inc.get("conditional_prefix")
    if pref:
        ids.extend([f"{pref}-sl", f"{pref}-tp1", f"{pref}-tp2"])
    # dedup preservando ordem
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _matching_orders(listing: dict, exact_ids: list[str]) -> list[dict]:
    if not exact_ids:
        return []
    want = {str(x) for x in exact_ids}
    return [o for o in (listing.get("orders") or [])
            if isinstance(o, dict) and (_order_identities(o) & want)]


async def _ensure_stop(key: str, owner: str, inc: dict, qty: Optional[float]) -> tuple[bool, str]:
    """Adota um SL vivo válido (snake_case, lado/cobertura/trigger). Se não houver
    e as invariantes permitirem, CRIA somente SL via P02 (assinatura real). Nunca
    TP, nunca MARKET, nunca fecha mais que a qty confirmada. Sucesso de criação
    exige `sl_ok is True` e `sl_order_id`."""
    from services import binance_signed_service as bss
    symbol = inc["symbol"]
    try:
        listing = await bss.get_open_algo_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        return False, f"listagem algo exceção: {exc}"
    if not listing or not listing.get("ok"):
        return False, "listagem algo stale/erro"
    adopted, _aid, detail = _adopt_live_stop(inc, qty, listing)
    if adopted:
        return True, detail
    if not RECONCILE_CREATE_SL:
        return False, "sem SL vivo válido (criação desabilitada)"
    entry_side = _norm_entry_side(inc.get("side"))
    planned_stop = _finite(inc.get("planned_stop"))
    q = _finite(qty)
    if entry_side is None or planned_stop is None or planned_stop <= 0 or q is None or q <= 0:
        return False, "sem lado/stop/qty válidos p/ criar SL"
    if not await _renew_or_abort(key, owner):
        return False, "claim perdido antes de criar SL — abortado"
    try:
        res = await bss.place_protection_orders(
            symbol, entry_side, q, stop_loss=planned_stop, tp1=None, tp2=None,
            client_order_id_prefix=(inc.get("conditional_prefix") or f"p03-{_mask(key)}"),
            dedup_live=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"criação SL exceção: {exc}"
    if res and res.get("sl_ok") is True and res.get("sl_order_id"):
        return True, f"SL criado ({_mask(res.get('sl_order_id'))})"
    return False, f"criação SL não confirmada (sl_ok={res.get('sl_ok') if res else None})"


async def _reconcile_entry(key: str, owner: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    coid = inc.get("client_order_id")
    oid = inc.get("entry_order_id")
    try:
        order_res = await bss.get_order(inc["symbol"], order_id=oid, client_order_id=coid)
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"get_order exceção: {exc}")
        return
    verdict, qty, why = classify_entry(order_res)

    # MAKER viva (NEW/PARTIALLY_FILLED): cancelar pelo ID correto, validar ok,
    # re-consultar e exigir terminal. NUNCA fallback MARKET. Cancel incerto mantém.
    if verdict == "RETRY" and _is_maker(inc):
        status = (order_res.get("status") or "").upper()
        if status in ("NEW", "PARTIALLY_FILLED"):
            if not await _renew_or_abort(key, owner):
                return
            try:
                cancel = await bss.cancel_order(inc["symbol"], order_id=oid, client_order_id=coid)
            except Exception as exc:  # noqa: BLE001
                cancel = {"ok": False, "error": str(exc)}
            if not (cancel and cancel.get("ok")):
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                      f"cancel maker incerto — mantém quarentena ({cancel.get('error') or cancel.get('msg')})")
                return
            try:
                order_res = await bss.get_order(inc["symbol"], order_id=oid, client_order_id=coid)
            except Exception as exc:  # noqa: BLE001
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"re-query pós-cancel: {exc}")
                return
            verdict, qty, why = classify_entry(order_res)
            if verdict == "RETRY":
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                      "maker cancelada mas ainda não-terminal")
                return

    if verdict == "FLAT":
        await _resolve(key, owner, State.FLAT, f"entrada sem fill ({why})")
        return
    if verdict == "RETRY":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, why)
        return
    if verdict == "FILL_UNKNOWN":
        if inc.get("kind") != Kind.FINAL_FILL_QTY_UNKNOWN:
            await _fenced(key, owner, kind=Kind.FINAL_FILL_QTY_UNKNOWN)
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"qty terminal desconhecida: {why}")
        return

    # PROTECTED tentativo: fill terminal confirmado. Exige posição fresh + SL.
    pv, _size = await _fresh_verdict(inc["symbol"])
    if pv == "UNKNOWN":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição UNKNOWN pós-fill")
        return
    if pv == "FLAT":
        await _resolve(key, owner, State.FLAT, "posição fresh-flat pós-terminal")
        return
    ok, detail = await _ensure_stop(key, owner, inc, qty)
    if ok:
        await _resolve(key, owner, State.PROTECTED, f"fill {qty:g} protegido ({detail})")
    else:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"sem SL confirmado: {detail}")


async def _reconcile_cleanup(key: str, owner: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    pv, size = await _fresh_verdict(inc["symbol"])
    if pv == "UNKNOWN":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição/listagem incerta")
        return
    if pv == "OPEN":
        ok, detail = await _ensure_stop(key, owner, inc, size)
        if ok:
            await _resolve(key, owner, State.PROTECTED, f"posição aberta re-protegida ({detail})")
        else:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"posição aberta sem SL: {detail}")
        return

    # FLAT. Sem identidade confiável de condicionais → NÃO resolve por "nenhum
    # match"; mantém retry até MANUAL. FLAT exige identidade suficiente.
    exact_ids = _exact_conditional_ids(inc)
    if not exact_ids:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "cleanup sem identidade de condicionais — não resolve por ausência")
        return
    try:
        listing = await bss.get_open_algo_orders(inc["symbol"])
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"listagem algo exceção: {exc}")
        return
    if not listing or not listing.get("ok"):
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "listagem algo stale/erro")
        return
    matches = _matching_orders(listing, exact_ids)
    if matches:
        if not await _renew_or_abort(key, owner):
            return
        all_ok = True
        for o in matches:
            algo_id = o.get("algo_id") or o.get("algoId")
            if not algo_id:
                all_ok = False
                continue
            try:
                res = await bss.cancel_algo_order(str(algo_id))
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            if not (res and res.get("ok")):     # ok=False NÃO é sucesso
                all_ok = False
        # Re-consulta pra confirmar ausência; zera clean (reaparecimento).
        try:
            listing2 = await bss.get_open_algo_orders(inc["symbol"])
        except Exception:  # noqa: BLE001
            listing2 = {"ok": False}
        still = _matching_orders(listing2, exact_ids) if listing2.get("ok") else ["?"]
        await _fenced(key, owner, clean_observations=0)
        if all_ok and not still:
            await _schedule_retry(key, owner, inc, State.OPEN,
                                  "condicional cancelada — reconfirmar ausência no próximo ciclo")
        else:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                  "cancel incerto/condicional persiste — mantém quarentena")
        return

    # Nenhuma condicional exata + listagem limpa → observação separada.
    clean = int(inc.get("clean_observations") or 0) + 1
    await _fenced(key, owner, clean_observations=clean)
    if clean >= RECONCILE_CLEAN_GRACE:
        await _resolve(key, owner, State.FLAT, f"fresh-flat + cleanup confirmado ({clean} ciclos)")
    else:
        await _schedule_retry(key, owner, inc, State.OPEN,
                              f"grace {clean}/{RECONCILE_CLEAN_GRACE} (aguardando novo ciclo)")


async def _reconcile_manual_kind(key: str, owner: str, inc: dict) -> None:
    await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                  manual_reason=inc.get("manual_reason")
                  or f"{inc.get('kind')} exige intervenção humana (não pode ser fechada automaticamente)")
    log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({inc.get('kind')})")


async def _reconcile_one(key: str, owner: str) -> None:
    repo = _get_repo()
    inc = await repo.get(key)
    if not inc:
        return
    ok, why = _eligible(inc)
    if not ok:
        if "mismatch" in why:
            await repo.update(key, last_error=why)  # registra motivo, sem mutação
        return
    if not await _fenced(key, owner, state=State.RECONCILING):
        return  # claim perdido (lease vencido / outro dono) → não processa
    inc = await repo.get(key) or inc
    kind = inc.get("kind")
    try:
        if kind in _ENTRY_KINDS:
            await _reconcile_entry(key, owner, inc)
        elif kind in _CLEANUP_KINDS:
            await _reconcile_cleanup(key, owner, inc)
        elif kind in _MANUAL_KINDS:
            await _reconcile_manual_kind(key, owner, inc)
        else:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"kind desconhecido {kind}")
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"reconcile exceção: {exc}")


async def reconcile_due() -> dict:
    """Um ciclo: recupera claims expirados, processa incidentes elegíveis (claim
    atômico + fencing por owner/lease). Libera a quarentena própria SÓ na transição
    de ≥1 incidente para 0 — nada de clear/log nos ciclos comuns."""
    global _last_reconciliation_at, _prev_open_count
    repo = _get_repo()
    await repo.recover_expired_claims()
    processed = 0
    for inc in await repo.list_open():
        if processed >= RECONCILE_MAX_PER_CYCLE:
            break
        if not _eligible(inc)[0]:
            continue
        key = inc["incident_key"]
        if not await repo.claim(key, _PROCESS_ID, _now() + timedelta(seconds=RECONCILE_LEASE_S)):
            continue
        try:
            await _reconcile_one(key, _PROCESS_ID)
            processed += 1
        finally:
            cur = await repo.get(key)
            if cur and cur.get("resolved_at") is None:
                await repo.release_claim(key, owner=_PROCESS_ID)  # só o próprio claim
    open_now = len(await repo.list_open())
    released = False
    if _prev_open_count > 0 and open_now == 0:
        released = await _maybe_release_quarantine()
    _prev_open_count = open_now
    _last_reconciliation_at = _now().isoformat()
    return {"processed": processed, "open_now": open_now, "quarantine_released": released}


async def boot_reconcile() -> dict:
    """Boot: recupera claims, ARMA quarentena ANTES de qualquer scan se houver
    incidente aberto, detecta untracked (fail-closed) e faz 1 ciclo. QUALQUER
    falha (DB/leitura) ARMA o latch P03 e mantém o bloqueio (fail-closed real)."""
    global _prev_open_count
    repo = _get_repo()
    try:
        await repo.recover_expired_claims()
        open_incs = await repo.list_open()
    except Exception as exc:  # noqa: BLE001
        await _arm_quarantine(f"boot: falha lendo incidentes ({exc})")
        log.critical(f"[p03][boot] falha lendo incidentes — quarentena armada (fail-closed): {exc}")
        return {"open_incidents": None, "untracked": None, "boot_error": str(exc)}
    if open_incs:
        await _arm_quarantine(f"boot: {len(open_incs)} incidente(s) aberto(s)")
        log.critical(f"[p03][boot] {len(open_incs)} incidente(s) aberto(s) — quarentena armada")
    _prev_open_count = len(open_incs)
    untracked = await _detect_untracked_positions()
    try:
        await reconcile_due()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][boot] ciclo inicial falhou: {exc}")
    return {"open_incidents": len(open_incs), "untracked": untracked}


async def _detect_untracked_positions() -> int:
    """Posições Binance fresh × RealTrade aberto. Posição sem trade →
    UNTRACKED_POSITION + pausa. Leitura indisponível/stale/rate-limited → NÃO
    assume flat: ARMA quarentena P03 (fail-closed) e mantém bloqueio."""
    try:
        from services import binance_signed_service as bss
        if not bss.is_configured():
            return 0
        res = await bss.get_positions(force=True)
    except Exception as exc:  # noqa: BLE001
        await _arm_quarantine(f"boot: leitura de posições indisponível ({exc})")
        log.critical(f"[p03][boot] leitura de posições indisponível — quarentena armada: {exc}")
        return 0
    if not res or not res.get("ok") or res.get("stale") or res.get("rate_limited"):
        await _arm_quarantine("boot: posições stale/rate-limited (não assumo flat)")
        log.critical("[p03][boot] posições stale/incertas — quarentena armada (não assumo flat)")
        return 0
    positions = [p for p in (res.get("positions") or []) if abs(_finite(p.get("size")) or 0) > 0]
    if not positions:
        return 0
    try:
        open_symbols = await _open_real_trade_symbols()
    except Exception as exc:  # noqa: BLE001
        await _arm_quarantine(f"boot: leitura RealTrade falhou ({exc})")
        log.critical(f"[p03][boot] leitura RealTrade falhou — quarentena armada: {exc}")
        open_symbols = set()
    n = 0
    for p in positions:
        sym = p.get("symbol") or ""
        norm = sym.replace("USDT", "/USDT:USDT") if "/" not in sym else sym
        if norm in open_symbols or sym in open_symbols:
            continue
        side = str(p.get("side") or "").strip().lower()   # usa o side REAL (não o abs)
        await record_incident(kind=Kind.UNTRACKED_POSITION, symbol=norm,
                              side=("sell" if side == "sell" else "buy"),
                              min_known_fill=abs(_finite(p.get("size")) or 0),
                              payload={"detected_at_boot": True})
        n += 1
    if n:
        log.critical(f"[p03][boot] {n} posição(ões) UNTRACKED → pausa persistente")
    return n


async def _open_real_trade_symbols() -> set:
    from db import get_session, DB_ENABLED
    if not DB_ENABLED:
        return set()
    from models.real_trade import RealTrade
    from sqlalchemy import select
    async with get_session() as session:
        rows = (await session.execute(
            select(RealTrade.symbol).where(RealTrade.status == "open")
        )).scalars().all()
        return set(rows)


# ── Task integrada ao lifespan (NÃO é worker/scheduler separado) ─────────────
async def loop() -> None:
    global _reconciler_running
    _reconciler_running = True
    log.info(f"[p03] reconciliador iniciado (intervalo {RECONCILE_INTERVAL_S:.0f}s, proc={_PROCESS_ID}).")
    try:
        while True:
            try:
                await reconcile_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(f"[p03] ciclo falhou: {exc}")
            await asyncio.sleep(RECONCILE_INTERVAL_S)
    finally:
        _reconciler_running = False


# ── API read-only ────────────────────────────────────────────────────────────
def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _summ(inc: dict) -> dict:
    return {
        "incident_id": inc.get("id"), "incident_key": inc.get("incident_key"),
        "symbol": inc.get("symbol"), "side": inc.get("side"), "kind": inc.get("kind"),
        "state": inc.get("state"),
        "qty_known": inc.get("planned_qty") if inc.get("planned_qty") is not None else inc.get("min_known_fill"),
        "attempts": inc.get("attempts"), "last_error": inc.get("last_error"),
        "manual_reason": inc.get("manual_reason"),
        "next_retry_at": _iso(inc.get("next_retry_at")), "updated_at": _iso(inc.get("updated_at")),
    }


async def get_status() -> dict:
    repo = _get_repo()
    try:
        rows = await repo.list_all()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "reconciler_running": _reconciler_running}
    open_rows = [r for r in rows if r.get("resolved_at") is None]
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r.get("state")] = by_state.get(r.get("state"), 0) + 1
    quarantine_active = False
    try:
        from services import shadow_trade_service
        quarantine_active = bool(shadow_trade_service.execution_quarantine_reason())
    except Exception:
        pass
    return {
        "ok": True, "reconciler_running": _reconciler_running,
        "last_reconciliation_at": _last_reconciliation_at,
        "quarantine_active": quarantine_active,
        "open_total": len(open_rows),
        "retry_pending": sum(1 for r in open_rows if r.get("state") == State.RETRY_PENDING),
        "manual_required": sum(1 for r in open_rows if r.get("state") == State.MANUAL_REQUIRED),
        "protected": by_state.get(State.PROTECTED, 0),
        "flat": by_state.get(State.FLAT, 0),
        "items": [_summ(r) for r in open_rows],
        "manual_items": [_summ(r) for r in open_rows if r.get("state") == State.MANUAL_REQUIRED],
    }
