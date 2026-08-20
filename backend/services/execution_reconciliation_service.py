"""
Execution Reconciliation (P03) — reconciliação PERSISTENTE de execução.

Torna persistente a reconciliação de ordens/posições/condicionais que fiquem
`UNKNOWN`, inclusive após restart:

    submissão → safety state P02 → incidente persistido → restart/reconciler
    → ordem + posição + condicionais reconciliadas → PROTECTED | FLAT
    | MANUAL_REQUIRED → liberação segura da quarentena própria.

Reutiliza integralmente o P02: NÃO cria motor de ordens, worker, scheduler,
queue ou retry engine paralelo. Roda como UMA task integrada ao lifespan
existente. Só CONSULTA a exchange (get_order/positionRisk/open_algo) e reusa as
primitivas P02 de leitura/cancelamento (`get_order`, `_fresh_position_size`,
`get_open_algo_orders`, `cancel_algo_order`). NÃO coloca ordem nova: adota um SL
vivo que cumpra o contrato P02 ou escala para MANUAL_REQUIRED. Nunca reenvia
entry, nunca emite MARKET cego, nunca declara no_fill sem confirmação.

Enquanto existir incidente aberto, a quarentena de execução (latch em memória do
`shadow_trade_service` + pausa no `RiskState`) bloqueia TODA nova entrada —
inclusive hedge/pyramiding/tf-upgrade — e sobrevive ao restart.
"""
from __future__ import annotations

import os
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

log = logging.getLogger(__name__)

EXCHANGE_BINANCE = "binance"


# ── Kinds (nomes do P02 quando equivalentes) ────────────────────────────────
class Kind:
    ENTRY_SUBMISSION_UNKNOWN = "ENTRY_SUBMISSION_UNKNOWN"
    ENTRY_ORDER_UNKNOWN = "ENTRY_ORDER_UNKNOWN"
    FINAL_FILL_QTY_UNKNOWN = "FINAL_FILL_QTY_UNKNOWN"
    CONDITIONAL_SUBMISSION_UNKNOWN = "CONDITIONAL_SUBMISSION_UNKNOWN"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    UNTRACKED_POSITION = "UNTRACKED_POSITION"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


# ── Lifecycle mínimo e explicável ───────────────────────────────────────────
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

# Status terminais de ordem que exigem executedQty explícita.
_TERMINAL_ORDER_STATUS = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}


# ── Config operacional (defaults conservadores / fail-closed) ────────────────
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


RECONCILE_INTERVAL_S = _f("RECONCILE_INTERVAL_S", 30.0)
RECONCILE_BACKOFF_BASE_S = _f("RECONCILE_BACKOFF_BASE_S", 15.0)
RECONCILE_BACKOFF_MAX_S = _f("RECONCILE_BACKOFF_MAX_S", 900.0)
RECONCILE_CLEAN_GRACE = _i("RECONCILE_CLEAN_GRACE", 2)        # ciclos separados
RECONCILE_LEASE_S = _f("RECONCILE_LEASE_S", 120.0)
RECONCILE_MAX_ATTEMPTS = _i("RECONCILE_MAX_ATTEMPTS", 8)
RECONCILE_MAX_PER_CYCLE = _i("RECONCILE_MAX_PER_CYCLE", 10)

_PROCESS_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_PAUSE_MARKER = "P03-QUARANTINE:"

_last_reconciliation_at: Optional[str] = None
_reconciler_running = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mask(v: Any) -> Optional[str]:
    s = str(v) if v is not None else None
    if not s:
        return s
    return s if len(s) <= 6 else f"{s[:3]}…{s[-3:]}"


def _backoff_seconds(attempts: int) -> float:
    exp = RECONCILE_BACKOFF_BASE_S * (2 ** max(0, attempts))
    return float(min(exp, RECONCILE_BACKOFF_MAX_S))


# ════════════════════════════════════════════════════════════════════════════
#  NÚCLEO DE DECISÃO — puro (sem DB/exchange), 100% testável
# ════════════════════════════════════════════════════════════════════════════
def terminal_qty(order_res: dict) -> tuple[Optional[float], str]:
    """Qty terminal de uma ordem de entrada, com as regras P03:

    - FILLED: qty original confirmada;
    - REJECTED: qty final zero;
    - CANCELED/EXPIRED/EXPIRED_IN_MATCH: exige executedQty terminal EXPLÍCITA;
    - qty ausente/inválida: None → FINAL_FILL_QTY_UNKNOWN.

    Fill observado antes do terminal é apenas lower bound (nunca qty final).
    """
    if not order_res or not order_res.get("ok"):
        return None, "consulta inconclusiva"
    status = (order_res.get("status") or "").upper()
    raw = order_res.get("raw") or {}
    if status == "FILLED":
        qty = order_res.get("orig_qty") or order_res.get("executed_qty")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return None, "FILLED sem qty numérica"
        return (qty, "filled") if qty > 0 else (None, "FILLED com qty<=0")
    if status == "REJECTED":
        return 0.0, "rejected"
    if status in _TERMINAL_ORDER_STATUS:
        # Exige executedQty terminal EXPLÍCITA (presente na resposta bruta).
        if "executedQty" not in raw and order_res.get("executed_qty") is None:
            return None, f"{status} sem executedQty explícita"
        try:
            eq = float(order_res.get("executed_qty"))
        except (TypeError, ValueError):
            return None, f"{status} com executedQty inválida"
        return eq, f"{status.lower()}_terminal"
    # NEW / PARTIALLY_FILLED / desconhecido → não terminal.
    return None, f"não terminal ({status or 'sem status'})"


def classify_entry(order_res: dict) -> tuple[str, Optional[float], str]:
    """Veredito de um incidente de entrada a partir da consulta (nunca reenviar
    entry). Retorna (verdict, qty_final|None, motivo), verdict ∈
    {"RETRY", "FILL_UNKNOWN", "FLAT", "PROTECTED"}."""
    if not order_res or not order_res.get("ok"):
        return "RETRY", None, "consulta indisponível"     # mantém pausa e reagenda
    status = (order_res.get("status") or "").upper()
    if status in ("", "NEW", "PARTIALLY_FILLED"):
        # Inconclusivo: mantém pausa, sem TP, reagenda. Lower bound só informa.
        return "RETRY", None, f"ainda {status or 'sem status'}"
    qty, why = terminal_qty(order_res)
    if qty is None:
        return "FILL_UNKNOWN", None, why                  # → kind FINAL_FILL_QTY_UNKNOWN
    if qty <= 0:
        return "FLAT", 0.0, why          # REJECTED / terminal sem fill → sem posição
    return "PROTECTED", qty, why          # fill terminal confirmado → precisa proteção


def position_verdict(size: Optional[float], status: str) -> str:
    """Traduz `_fresh_position_size` em veredito de segurança.

    None (stale/rate-limited/erro/UNKNOWN) NUNCA prova flat.
    """
    if size is None:
        return "UNKNOWN"          # mantém incidente, mantém pausa
    return "FLAT" if abs(size) <= 0 else "OPEN"


# ════════════════════════════════════════════════════════════════════════════
#  Repositório de incidentes (persistente) + fallback/injeção em memória
# ════════════════════════════════════════════════════════════════════════════
class InMemoryIncidentRepo:
    """Repositório em memória com a MESMA semântica de claim/lease do SQL.

    Serve de fallback fail-closed quando o DB está off e é injetado nos testes
    (persistência, restart lógico, claim expirado, concorrência) sem tocar banco.
    """

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

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return False
            cur_owner = row.get("claimed_by")
            exp = row.get("lease_expires_at")
            free = cur_owner is None or (exp is not None and exp < _now())
            if not free and cur_owner != owner:
                return False
            row["claimed_by"] = owner
            row["claimed_at"] = _now()
            row["lease_expires_at"] = lease_until
            return True

    async def release_claim(self, key: str) -> None:
        async with self._lock:
            row = self._rows.get(key)
            if row:
                row["claimed_by"] = None
                row["claimed_at"] = None
                row["lease_expires_at"] = None

    async def recover_expired_claims(self) -> int:
        async with self._lock:
            n = 0
            now = _now()
            for row in self._rows.values():
                exp = row.get("lease_expires_at")
                if row.get("claimed_by") and exp is not None and exp < now:
                    row["claimed_by"] = None
                    row["claimed_at"] = None
                    row["lease_expires_at"] = None
                    n += 1
            return n


class _SqlIncidentRepo:
    """Repositório persistente (Postgres) com claim atômico (UPDATE ... WHERE)."""

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            existing = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            if existing is not None:
                return _row_to_dict(existing), False
            row = ExecutionIncident(incident_key=key, **{
                k: v for k, v in defaults.items() if hasattr(ExecutionIncident, k)
            })
            session.add(row)
            await session.commit()
            return _row_to_dict(row), True

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

    async def update(self, key: str, **fields) -> Optional[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            if not row:
                return None
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            row.updated_at = _now()
            await session.commit()
            return _row_to_dict(row)

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update, or_
        now = _now()
        async with get_session() as session:
            stmt = (
                update(ExecutionIncident)
                .where(
                    ExecutionIncident.incident_key == key,
                    ExecutionIncident.resolved_at.is_(None),
                    or_(
                        ExecutionIncident.claimed_by.is_(None),
                        ExecutionIncident.lease_expires_at < now,
                    ),
                )
                .values(claimed_by=owner, claimed_at=now, lease_expires_at=lease_until)
            )
            res = await session.execute(stmt)
            await session.commit()
            return (res.rowcount or 0) == 1

    async def release_claim(self, key: str) -> None:
        await self.update(key, claimed_by=None, claimed_at=None, lease_expires_at=None)

    async def recover_expired_claims(self) -> int:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update
        now = _now()
        async with get_session() as session:
            stmt = (
                update(ExecutionIncident)
                .where(
                    ExecutionIncident.claimed_by.is_not(None),
                    ExecutionIncident.lease_expires_at < now,
                )
                .values(claimed_by=None, claimed_at=None, lease_expires_at=None)
            )
            res = await session.execute(stmt)
            await session.commit()
            return res.rowcount or 0


def _row_to_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


# Repositório ativo. Produção usa SQL quando o DB está on; senão fica em memória
# (fail-closed no processo atual). Testes injetam via set_repo().
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
#  Quarentena (reutiliza latch P02 + pausa RiskState) — só libera a PRÓPRIA
# ════════════════════════════════════════════════════════════════════════════
async def _arm_quarantine(reason: str) -> None:
    """Arma o latch em memória (fail-closed imediato) e persiste a pausa."""
    try:
        from services import shadow_trade_service
        shadow_trade_service._arm_execution_quarantine(reason)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha armando latch: {exc}")
    try:
        from services import risk_service
        await risk_service.set_manual_pause(True, f"{_PAUSE_MARKER} {reason}")
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha persistindo pausa RiskState: {exc}")


async def _maybe_release_quarantine() -> bool:
    """Libera SOMENTE a quarentena própria do P03 quando tudo estiver seguro.

    Nunca remove pausa manual do operador: só resume se a pausa ainda for
    P03-owned (reason com o marcador). Exige zero incidentes abertos, nenhuma
    posição untracked, nenhuma entry pendente e cleanup confirmado.
    """
    still_open = await _repo.list_open() if _repo else await _get_repo().list_open()
    if still_open:
        return False
    try:
        from services import risk_service
        status = await risk_service.get_status()
    except Exception:
        return False
    reason = (status or {}).get("pause_reason") or ""
    if status and status.get("trading_paused") and not reason.startswith(_PAUSE_MARKER):
        # Pausa foi trocada por humano/outro subsistema → não mexer.
        return False
    try:
        from services import shadow_trade_service
        shadow_trade_service.clear_execution_quarantine()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] falha limpando latch: {exc}")
    try:
        from services import risk_service
        if status and status.get("trading_paused"):
            await risk_service.set_manual_pause(False, "P03: incidentes resolvidos")
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] falha resumindo RiskState: {exc}")
    log.warning("[p03] quarentena própria liberada — nenhum incidente aberto")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Entrada oficial de incidentes (idempotente por incident_key)
# ════════════════════════════════════════════════════════════════════════════
def build_incident_key(kind: str, symbol: str, *, client_order_id: str = None,
                       entry_order_id: str = None, extra: str = None) -> str:
    ident = client_order_id or entry_order_id or extra or "-"
    return f"{EXCHANGE_BINANCE}:{kind}:{symbol}:{ident}"


async def record_incident(*, kind: str, symbol: str, exchange: str = EXCHANGE_BINANCE,
                          client_order_id: str = None, entry_order_id: str = None,
                          side: str = None, planned_qty: float = None,
                          min_known_fill: float = None, planned_stop: float = None,
                          conditional_prefix: str = None, conditional_ids: dict = None,
                          payload: dict = None, incident_key: str = None) -> dict:
    """Persiste (ou reusa) um incidente e ARMA a quarentena. Fail-closed: o latch
    é armado mesmo se a persistência falhar. Idempotente por incident_key."""
    key = incident_key or build_incident_key(
        kind, symbol, client_order_id=client_order_id, entry_order_id=entry_order_id
    )
    repo = _get_repo()
    created = False
    row = None
    try:
        row, created = await repo.upsert(key, {
            "kind": kind, "symbol": symbol, "exchange": exchange, "side": side,
            "client_order_id": client_order_id, "entry_order_id": entry_order_id,
            "planned_qty": planned_qty, "min_known_fill": min_known_fill,
            "planned_stop": planned_stop, "conditional_prefix": conditional_prefix,
            "conditional_ids": conditional_ids, "payload": payload,
            "next_retry_at": _now(),
        })
        if not created and row and row.get("resolved_at") is None:
            # Merge idempotente: eleva o lower bound conhecido, sem duplicar.
            patch: dict = {}
            if min_known_fill is not None:
                prev = row.get("min_known_fill") or 0.0
                patch["min_known_fill"] = max(prev, min_known_fill)
            if conditional_ids and not row.get("conditional_ids"):
                patch["conditional_ids"] = conditional_ids
            if patch:
                await repo.update(key, **patch)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] persistência de incidente falhou ({key}): {exc}")
    await _arm_quarantine(f"incidente {kind} {symbol} ({_mask(client_order_id or entry_order_id)})")
    log.critical(
        f"[p03][incident] key={key} kind={kind} symbol={symbol} "
        f"created={created} coid={_mask(client_order_id)} eoid={_mask(entry_order_id)}"
    )
    return {"incident_key": key, "created": created, "state": (row or {}).get("state", State.OPEN)}


# ════════════════════════════════════════════════════════════════════════════
#  Reconciliação
# ════════════════════════════════════════════════════════════════════════════
def _eligible(inc: dict) -> tuple[bool, str]:
    if inc.get("resolved_at") is not None:
        return False, "resolvido"
    if inc.get("state") == State.MANUAL_REQUIRED:
        return False, "manual_required"
    if (inc.get("exchange") or EXCHANGE_BINANCE) != EXCHANGE_BINANCE:
        return False, "exchange mismatch (bloqueado, sem mutação)"
    if not (inc.get("client_order_id") or inc.get("entry_order_id")
            or inc.get("conditional_ids") or inc.get("kind") in _MANUAL_KINDS):
        return False, "identidade insuficiente"
    nra = inc.get("next_retry_at")
    if nra is not None and nra > _now():
        return False, "retry não elegível ainda"
    return True, "elegível"


async def _schedule_retry(key: str, inc: dict, state: str, reason: str) -> None:
    attempts = int(inc.get("attempts") or 0) + 1
    if attempts >= RECONCILE_MAX_ATTEMPTS and state not in _TERMINAL_SAFE:
        await _repo.update(key, state=State.MANUAL_REQUIRED, attempts=attempts,
                           last_error=reason,
                           manual_reason=f"máx. tentativas ({attempts}) sem prova de segurança: {reason}")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (attempts={attempts}) {reason}")
        return
    delay = _backoff_seconds(attempts)
    await _repo.update(key, state=state, attempts=attempts, last_error=reason,
                       next_retry_at=_now() + timedelta(seconds=delay))
    log.warning(f"[p03][transition] {key} → {state} attempt={attempts} "
                f"next_retry_in={delay:.0f}s reason={reason}")


async def _resolve(key: str, state: str, reason: str) -> None:
    await _repo.update(key, state=state, resolved_at=_now(), last_error=reason,
                       claimed_by=None, claimed_at=None, lease_expires_at=None)
    log.warning(f"[p03][transition] {key} → {state} (RESOLVIDO) {reason}")


async def _reconcile_entry(key: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    coid = inc.get("client_order_id")
    oid = inc.get("entry_order_id")
    try:
        order_res = await bss.get_order(inc["symbol"], order_id=oid, client_order_id=coid)
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, inc, State.RETRY_PENDING, f"get_order exceção: {exc}")
        return
    verdict, qty, why = classify_entry(order_res)
    if verdict == "FLAT":
        await _resolve(key, State.FLAT, f"entrada sem fill ({why})")
        return
    if verdict == "RETRY":
        await _schedule_retry(key, inc, State.RETRY_PENDING, why)
        return
    if verdict == "FILL_UNKNOWN":
        # Terminal sem qty auditável → marca o kind e mantém pausa (sem TP).
        if inc.get("kind") != Kind.FINAL_FILL_QTY_UNKNOWN:
            await _repo.update(key, kind=Kind.FINAL_FILL_QTY_UNKNOWN)
        await _schedule_retry(key, inc, State.RETRY_PENDING, f"qty terminal desconhecida: {why}")
        return
    # PROTECTED tentativo: fill terminal confirmado (qty). Exige posição fresh e
    # SL confirmado pelo contrato P02 antes de declarar seguro. Sem SL → sem TP.
    verdict, size = await _fresh_verdict(inc["symbol"])
    if verdict == "UNKNOWN":
        await _schedule_retry(key, inc, State.RETRY_PENDING, "posição UNKNOWN pós-fill")
        return
    if verdict == "FLAT":
        await _resolve(key, State.FLAT, "posição fresh-flat pós-terminal")
        return
    ok, detail = await _ensure_stop(inc, qty)
    if ok:
        await _resolve(key, State.PROTECTED, f"fill {qty:g} protegido ({detail})")
    else:
        await _schedule_retry(key, inc, State.RETRY_PENDING, f"sem SL confirmado: {detail}")


async def _reconcile_cleanup(key: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    verdict, size = await _fresh_verdict(inc["symbol"])
    if verdict == "UNKNOWN":
        # Leitura incerta NUNCA conta como observação limpa.
        await _schedule_retry(key, inc, State.RETRY_PENDING, "posição/listagem incerta")
        return
    if verdict == "OPEN":
        # Posição aberta: só adota SL que cumpra cobertura; TP inválido não vira
        # confirmado. Posição anterior indistinguível → MANUAL.
        ok, detail = await _ensure_stop(inc, size)
        if ok:
            await _resolve(key, State.PROTECTED, f"posição aberta re-protegida ({detail})")
        else:
            await _schedule_retry(key, inc, State.RETRY_PENDING, f"posição aberta sem SL: {detail}")
        return
    # FLAT: procurar condicionais EXATAS órfãs.
    try:
        listing = await bss.get_open_algo_orders(inc["symbol"])
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, inc, State.RETRY_PENDING, f"listagem algo exceção: {exc}")
        return
    if not listing or not listing.get("ok"):
        await _schedule_retry(key, inc, State.RETRY_PENDING, "listagem algo stale/erro")
        return
    exact_ids = _exact_conditional_ids(inc)
    present = _find_exact(listing, exact_ids)
    if present:
        # Reapareceu: cancelar idempotente, zerar clean e manter aberto.
        for algo_id in present:
            try:
                await bss.cancel_algo_order(algo_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[p03] cancel algo {_mask(algo_id)} falhou: {exc}")
        await _repo.update(key, clean_observations=0)
        await _schedule_retry(key, inc, State.OPEN, f"condicional reapareceu ({len(present)}) — cancelada")
        return
    # Nenhuma condicional exata + listagem limpa → conta observação separada.
    clean = int(inc.get("clean_observations") or 0) + 1
    await _repo.update(key, clean_observations=clean)
    if clean >= RECONCILE_CLEAN_GRACE:
        await _resolve(key, State.FLAT, f"fresh-flat + cleanup confirmado ({clean} ciclos)")
    else:
        await _schedule_retry(key, inc, State.OPEN,
                              f"grace {clean}/{RECONCILE_CLEAN_GRACE} (aguardando novo ciclo)")


async def _reconcile_manual_kind(key: str, inc: dict) -> None:
    # UNTRACKED_POSITION / PERSISTENCE_FAILURE: nunca fechar automaticamente.
    await _repo.update(key, state=State.MANUAL_REQUIRED,
                       manual_reason=inc.get("manual_reason")
                       or f"{inc.get('kind')} exige intervenção humana (posição não pode ser fechada automaticamente)")
    log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({inc.get('kind')})")


async def _fresh_verdict(symbol: str) -> tuple[str, Optional[float]]:
    from services import binance_signed_service as bss
    try:
        size, status = await bss._fresh_position_size(symbol)
    except Exception as exc:  # noqa: BLE001
        return "UNKNOWN", None
    return position_verdict(size, status), size


def _exact_conditional_ids(inc: dict) -> list[str]:
    cids = inc.get("conditional_ids") or {}
    ids: list[str] = []
    for k in ("sl", "tp1", "tp2"):
        v = cids.get(k) if isinstance(cids, dict) else None
        if v:
            ids.append(str(v))
    extra = cids.get("ids") if isinstance(cids, dict) else None
    if isinstance(extra, list):
        ids.extend(str(x) for x in extra if x)
    return ids


def _find_exact(listing: dict, exact_ids: list[str]) -> list[str]:
    if not exact_ids:
        return []
    orders = listing.get("orders") or listing.get("algos") or listing.get("result") or []
    live = set()
    for o in orders:
        for field in ("algoId", "algo_id", "orderId", "clientAlgoId", "clientOrderId"):
            val = o.get(field) if isinstance(o, dict) else None
            if val:
                live.add(str(val))
    return [i for i in exact_ids if i in live]


async def _ensure_stop(inc: dict, qty: Optional[float]) -> tuple[bool, str]:
    """ADOTA (read-only) um SL vivo que cumpra o contrato P02: reduceOnly/
    closePosition, é STOP, tem `algoId` real e cobre a qty confirmada. NÃO coloca
    ordem nova — reconciliação não cria implementação paralela de proteção. Sem SL
    confirmável, o caller escala para RETRY→MANUAL_REQUIRED (posição sem stop exige
    intervenção humana). Nunca considera mais que a qty confirmada do incidente."""
    from services import binance_signed_service as bss
    symbol = inc["symbol"]
    try:
        listing = await bss.get_open_algo_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        return False, f"listagem algo exceção: {exc}"
    if not listing or not listing.get("ok"):
        return False, "listagem algo stale/erro"
    for o in (listing.get("orders") or listing.get("result") or []):
        if not isinstance(o, dict):
            continue
        is_stop = "STOP" in str(o.get("type") or o.get("origType") or "").upper()
        reduce_only = bool(o.get("reduceOnly") or o.get("closePosition"))
        algo_id = o.get("algoId") or o.get("algo_id") or o.get("orderId")
        if is_stop and reduce_only and algo_id:
            cover = o.get("closePosition") or (qty is None) or (
                float(o.get("origQty") or o.get("quantity") or 0) + 1e-12 >= float(qty))
            if cover:
                return True, f"SL vivo adotado ({_mask(algo_id)})"
    return False, "sem SL vivo confirmável (não coloca ordem — escala manual)"


async def _reconcile_one(key: str) -> None:
    repo = _get_repo()
    inc = await repo.get(key)
    if not inc:
        return
    ok, why = _eligible(inc)
    if not ok:
        if "mismatch" in why:
            await repo.update(key, last_error=why)   # registra motivo, sem mutação
        return
    await repo.update(key, state=State.RECONCILING)
    inc = await repo.get(key) or inc
    kind = inc.get("kind")
    try:
        if kind in _ENTRY_KINDS:
            await _reconcile_entry(key, inc)
        elif kind in _CLEANUP_KINDS:
            await _reconcile_cleanup(key, inc)
        elif kind in _MANUAL_KINDS:
            await _reconcile_manual_kind(key, inc)
        else:
            await _schedule_retry(key, inc, State.RETRY_PENDING, f"kind desconhecido {kind}")
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, inc, State.RETRY_PENDING, f"reconcile exceção: {exc}")


async def reconcile_due() -> dict:
    """Um ciclo: recupera claims expirados, processa incidentes elegíveis (um
    claim ativo por incidente), tenta liberar a quarentena própria."""
    global _last_reconciliation_at
    repo = _get_repo()
    await repo.recover_expired_claims()
    processed = 0
    for inc in await repo.list_open():
        if processed >= RECONCILE_MAX_PER_CYCLE:
            break
        ok, _ = _eligible(inc)
        if not ok:
            continue
        key = inc["incident_key"]
        claimed = await repo.claim(key, _PROCESS_ID, _now() + timedelta(seconds=RECONCILE_LEASE_S))
        if not claimed:
            continue  # outro processo detém o claim
        try:
            await _reconcile_one(key)
            processed += 1
        finally:
            cur = await repo.get(key)
            if cur and cur.get("resolved_at") is None:
                await repo.release_claim(key)
    released = await _maybe_release_quarantine()
    _last_reconciliation_at = _now().isoformat()
    return {"processed": processed, "quarantine_released": released}


async def boot_reconcile() -> dict:
    """No boot: recupera claims, ARMA quarentena ANTES de qualquer scan se houver
    incidente aberto, detecta posição untracked (fail-closed) e faz um 1º ciclo."""
    repo = _get_repo()
    await repo.recover_expired_claims()
    open_incs = await repo.list_open()
    if open_incs:
        await _arm_quarantine(f"boot: {len(open_incs)} incidente(s) aberto(s)")
        log.critical(f"[p03][boot] {len(open_incs)} incidente(s) aberto(s) — quarentena armada")
    untracked = await _detect_untracked_positions()
    try:
        await reconcile_due()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][boot] ciclo inicial falhou: {exc}")
    return {"open_incidents": len(open_incs), "untracked": untracked}


async def _detect_untracked_positions() -> int:
    """Compara posições Binance fresh × RealTrade aberto. Posição sem trade →
    incidente UNTRACKED_POSITION + pausa. Leitura indisponível → falha fechado
    (não assume conta flat)."""
    try:
        from services import binance_signed_service as bss
        if not bss.is_configured():
            return 0
        res = await bss.get_positions(force=True)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03][boot] leitura de posições indisponível — fail-closed: {exc}")
        return 0
    if not res or not res.get("ok") or res.get("stale") or res.get("rate_limited"):
        log.critical("[p03][boot] posições stale/incertas — não assumo flat")
        return 0
    positions = [p for p in (res.get("positions") or []) if abs(float(p.get("size") or 0)) > 0]
    if not positions:
        return 0
    try:
        open_symbols = await _open_real_trade_symbols()
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03][boot] leitura RealTrade falhou — fail-closed: {exc}")
        open_symbols = set()
    n = 0
    for p in positions:
        sym = p.get("symbol") or ""
        norm = sym.replace("USDT", "/USDT:USDT") if "/" not in sym else sym
        if norm in open_symbols or sym in open_symbols:
            continue
        await record_incident(kind=Kind.UNTRACKED_POSITION, symbol=norm,
                              side=("sell" if float(p.get("size") or 0) < 0 else "buy"),
                              min_known_fill=abs(float(p.get("size") or 0)),
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


# ── Task integrada ao lifespan existente (NÃO é worker/scheduler separado) ───
async def loop() -> None:
    global _reconciler_running
    _reconciler_running = True
    log.info(f"[p03] reconciliador iniciado (intervalo {RECONCILE_INTERVAL_S:.0f}s, "
             f"proc={_PROCESS_ID}).")
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
def _summ(inc: dict) -> dict:
    return {
        "incident_id": inc.get("id"),
        "incident_key": inc.get("incident_key"),
        "symbol": inc.get("symbol"),
        "side": inc.get("side"),
        "kind": inc.get("kind"),
        "state": inc.get("state"),
        "qty_known": inc.get("min_known_fill") if inc.get("planned_qty") is None else inc.get("planned_qty"),
        "attempts": inc.get("attempts"),
        "last_error": inc.get("last_error"),
        "manual_reason": inc.get("manual_reason"),
        "next_retry_at": _iso(inc.get("next_retry_at")),
        "updated_at": _iso(inc.get("updated_at")),
    }


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


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
        "ok": True,
        "reconciler_running": _reconciler_running,
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
