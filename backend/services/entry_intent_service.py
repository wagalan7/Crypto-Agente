"""P03 — reserva transacional da entrada econômica (SAFETY_FIX).

Contrato: NENHUM POST de entrada antes do commit da intenção. A exclusividade
é do PostgreSQL (chave única + compare-and-set), não da memória do processo.

Quem envia precisa do lease em `SENDING`. Lease expirado NÃO prova que a
tentativa anterior não enviou: a intenção vai para `UNKNOWN` e só a consulta
pelo MESMO `client_order_id` (reconciliador P03) pode resolvê-la. Nada aqui
reenvia ordem, cancela, apaga intenção ou presume ausência por TTL.

Sem I/O de exchange e sem transação aberta durante I/O: cada operação abre,
decide e fecha sua própria transação curta.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from typing import Any, Optional

from sqlalchemy import func, select, text, update

from models.entry_intent import (
    EntryIntent, PENDING_STATES, STATE_CONFIRMED, STATE_RESERVED,
    STATE_SENDING, STATE_TERMINAL, STATE_UNKNOWN,
)

SCHEMA_VERSION = "p03.intent.v1"
#: Mesma advisory lock transacional do P03/risco: a admissão de capacidade de
#: decisões CONCORRENTES precisa ser serializada com arm/release da pausa.
RISK_LOCK_KEY = 917283
DEFAULT_LEASE_SECONDS = 90
MAX_CLIENT_ORDER_ID = 36
#: Vocabulário fechado das decisões de reserva.
RESERVED_NEW = "RESERVED_NEW"
RESERVED_RESUMED = "RESERVED_RESUMED"
BLOCKED_IN_FLIGHT = "BLOCKED_IN_FLIGHT"
BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"
BLOCKED_CONFIRMED = "BLOCKED_CONFIRMED"
BLOCKED_TERMINAL = "BLOCKED_TERMINAL"
BLOCKED_CAPACITY = "BLOCKED_CAPACITY"
CONFLICT_PAYLOAD = "CONFLICT_PAYLOAD"
UNAVAILABLE = "UNAVAILABLE"
GRANTED = (RESERVED_NEW, RESERVED_RESUMED)


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _label(value, max_len: int = 40) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_len:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_:+./-]+", value) else None


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntryIdentity:
    """Identidade da DECISÃO. Não inclui qty, score, preço mutável nem retry."""

    account_ref: str
    exchange: str
    symbol: str
    quote: str
    side: str
    position_side: str
    timeframe: str
    playbook: str
    playbook_version: str
    purpose: str
    trigger_candle_ms: int

    def __post_init__(self) -> None:
        for field in ("account_ref", "exchange", "symbol", "quote", "side",
                      "position_side", "timeframe", "playbook", "playbook_version", "purpose"):
            if _label(getattr(self, field), 50) is None:
                raise ValueError(f"identidade inválida: {field}")
        if self.side not in ("long", "short"):
            raise ValueError("side deve ser long ou short")
        if isinstance(self.trigger_candle_ms, bool) or not isinstance(self.trigger_candle_ms, int):
            raise ValueError("trigger_candle_ms inteiro obrigatório")
        if self.trigger_candle_ms <= 0:
            raise ValueError("trigger_candle_ms deve ser positivo")

    @property
    def setup_id(self) -> str:
        """Setup estável: símbolo, TF, lado e vela/gatilho da decisão."""
        return _digest([self.symbol, self.timeframe, self.side, self.trigger_candle_ms,
                        self.playbook, self.playbook_version])

    @property
    def intent_key(self) -> str:
        return _digest([SCHEMA_VERSION, self.account_ref, self.exchange, self.symbol,
                        self.quote, self.side, self.position_side, self.timeframe,
                        self.playbook, self.playbook_version, self.purpose,
                        self.trigger_candle_ms, self.setup_id])

    @property
    def client_order_id(self) -> str:
        """Determinístico, dentro do charset/limite do contrato da exchange."""
        return f"cw-{self.intent_key[:20]}"[:MAX_CLIENT_ORDER_ID]


def payload_fingerprint(payload: dict) -> str:
    """Conteúdo MATERIAL da decisão (preços/proteções), sem qty nem relógio."""
    fields = ("entry", "stop_loss", "tp1", "tp2", "leverage")
    values = {}
    for field in fields:
        value = payload.get(field)
        if value is None:
            values[field] = None
            continue
        number = _finite(value)
        if number is None:
            raise ValueError(f"payload inválido: {field}")
        values[field] = round(number, 10)
    return _digest(values)


@dataclass(frozen=True)
class Capacity:
    """Admissão atômica entre decisões distintas (slots e risco aberto)."""

    risk_usd: float = 0.0
    max_open_positions: Optional[int] = None
    max_open_risk_usd: Optional[float] = None
    open_positions: int = 0
    open_risk_usd: float = 0.0


@dataclass(frozen=True)
class Reservation:
    decision: str
    intent_key: Optional[str] = None
    client_order_id: Optional[str] = None
    state: Optional[str] = None
    reason: Optional[str] = None

    @property
    def granted(self) -> bool:
        return self.decision in GRANTED


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _pending_usage(session, account_ref: str, exchange: str):
    """Reservas ainda não resolvidas — capacidade não é liberada só porque o
    RealTrade ainda não nasceu."""
    row = (await session.execute(
        select(func.count(EntryIntent.intent_key), func.coalesce(func.sum(EntryIntent.reserved_risk_usd), 0.0))
        .where(EntryIntent.account_ref == account_ref, EntryIntent.exchange == exchange,
               EntryIntent.state.in_(PENDING_STATES), EntryIntent.real_trade_id.is_(None))
    )).one()
    return int(row[0] or 0), float(row[1] or 0.0)


async def _open_positions(session) -> int:
    """Posições abertas contadas DENTRO da mesma transação da admissão."""
    from models.real_trade import RealTrade
    return int((await session.execute(
        select(func.count(RealTrade.id)).where(RealTrade.status == "open"))).scalar() or 0)


def _capacity_reason(capacity: Capacity, pending_count: int, pending_risk: float) -> Optional[str]:
    if capacity.max_open_positions is not None:
        if capacity.open_positions + pending_count + 1 > capacity.max_open_positions:
            return "MAX_OPEN_POSITIONS"
    if capacity.max_open_risk_usd is not None:
        total = capacity.open_risk_usd + pending_risk + max(0.0, capacity.risk_usd)
        if total > capacity.max_open_risk_usd:
            return "MAX_OPEN_RISK"
    return None


async def reserve(session_factory, identity: EntryIdentity, payload: dict, *,
                  owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                  capacity: Optional[Capacity] = None, now: Optional[datetime] = None) -> Reservation:
    """Reserva (ou recupera) a intenção em UMA transação, antes de qualquer POST.

    Falha de banco ⇒ `UNAVAILABLE`: o chamador NÃO pode enviar ordem.
    """
    fingerprint = payload_fingerprint(payload)
    key, coid = identity.intent_key, identity.client_order_id
    moment = now or _now()
    deadline = moment + timedelta(seconds=max(1, int(lease_seconds)))
    try:
        async with session_factory() as session:
            # Serializa a admissão de capacidade entre decisões diferentes.
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": RISK_LOCK_KEY})
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == key).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                # Decisão nova: admissão de capacidade sob a mesma lock.
                if capacity is not None:
                    pending_count, pending_risk = await _pending_usage(
                        session, identity.account_ref, identity.exchange)
                    open_positions = max(int(capacity.open_positions or 0),
                                         await _open_positions(session))
                    capacity = Capacity(
                        risk_usd=capacity.risk_usd, max_open_positions=capacity.max_open_positions,
                        max_open_risk_usd=capacity.max_open_risk_usd,
                        open_positions=open_positions, open_risk_usd=capacity.open_risk_usd)
                    denial = _capacity_reason(capacity, pending_count, pending_risk)
                    if denial:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None, denial)
                session.add(EntryIntent(
                    intent_key=key, client_order_id=coid, account_ref=identity.account_ref,
                    exchange=identity.exchange, symbol=identity.symbol, quote=identity.quote,
                    side=identity.side, position_side=identity.position_side,
                    timeframe=identity.timeframe, playbook=identity.playbook,
                    playbook_version=identity.playbook_version, purpose=identity.purpose,
                    setup_id=identity.setup_id, trigger_candle_ms=identity.trigger_candle_ms,
                    payload_fingerprint=fingerprint, state=STATE_RESERVED, reason=None,
                    lease_owner=owner, lease_expires_at=deadline, attempts=1, dispatches=0,
                    reserved_risk_usd=max(0.0, float(capacity.risk_usd) if capacity else 0.0),
                    real_trade_id=None, created_at=moment, updated_at=moment, resolved_at=None))
                await session.commit()
                return Reservation(RESERVED_NEW, key, coid, STATE_RESERVED)

            # Lidos ANTES de qualquer rollback: após o rollback o ORM expira a
            # instância e reler atributo dispararia IO preguiçoso.
            current_state, current_reason = row.state, row.reason
            current_fingerprint, lease_owner = row.payload_fingerprint, row.lease_owner
            lease_expires_at = row.lease_expires_at
            if current_fingerprint != fingerprint:
                # Mesma decisão com conteúdo material diferente: conflito
                # explicável, nunca uma segunda entrada silenciosa.
                await session.rollback()
                return Reservation(CONFLICT_PAYLOAD, key, coid, current_state, "PAYLOAD_DIVERGENT")
            if current_state == STATE_CONFIRMED:
                await session.rollback()
                return Reservation(BLOCKED_CONFIRMED, key, coid, current_state, current_reason)
            if current_state == STATE_UNKNOWN:
                await session.rollback()
                return Reservation(BLOCKED_UNKNOWN, key, coid, current_state, current_reason)
            if current_state == STATE_TERMINAL:
                await session.rollback()
                return Reservation(BLOCKED_TERMINAL, key, coid, current_state, current_reason)
            if current_state == STATE_SENDING:
                if lease_expires_at is not None and lease_expires_at <= moment:
                    # Lease vencido em SENDING: a tentativa anterior PODE ter
                    # enviado. Vai para UNKNOWN e exige consulta pelo client id.
                    row.state, row.reason = STATE_UNKNOWN, "LEASE_EXPIRED_AFTER_DISPATCH"
                    row.lease_owner, row.lease_expires_at = None, None
                    row.updated_at = moment
                    await session.commit()
                    return Reservation(BLOCKED_UNKNOWN, key, coid, STATE_UNKNOWN,
                                       "LEASE_EXPIRED_AFTER_DISPATCH")
                await session.rollback()
                return Reservation(BLOCKED_IN_FLIGHT, key, coid, current_state, current_reason)
            # RESERVED: ninguém despachou ainda. Só o dono do lease (ou um lease
            # vencido) pode seguir; senão outro worker está preparando o envio.
            if (lease_owner not in (None, owner)
                    and lease_expires_at is not None and lease_expires_at > moment):
                await session.rollback()
                return Reservation(BLOCKED_IN_FLIGHT, key, coid, current_state, "LEASE_HELD")
            row.lease_owner, row.lease_expires_at = owner, deadline
            row.attempts = int(row.attempts or 0) + 1
            row.updated_at = moment
            await session.commit()
            return Reservation(RESERVED_RESUMED, key, coid, STATE_RESERVED)
    except Exception:
        return Reservation(UNAVAILABLE, key, coid, None, "DB_UNAVAILABLE")


async def mark_sending(session_factory, intent_key: str, *, owner: str,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS,
                       now: Optional[datetime] = None) -> bool:
    """CAS RESERVED→SENDING. Precisa estar COMMITADO antes do primeiro POST."""
    moment = now or _now()
    deadline = moment + timedelta(seconds=max(1, int(lease_seconds)))
    try:
        async with session_factory() as session:
            result = await session.execute(
                update(EntryIntent)
                .where(EntryIntent.intent_key == intent_key,
                       EntryIntent.state == STATE_RESERVED,
                       EntryIntent.lease_owner == owner)
                .values(state=STATE_SENDING, lease_expires_at=deadline, updated_at=moment,
                        dispatches=EntryIntent.dispatches + 1))
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def may_dispatch(session_factory, intent_key: str, *, owner: str,
                       now: Optional[datetime] = None) -> bool:
    """Guard imediatamente antes de CADA POST/retry: só o dono do lease vivo
    em SENDING pode despachar. Dúvida ⇒ False (nenhum segundo dispatcher)."""
    moment = now or _now()
    try:
        async with session_factory() as session:
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
            )).scalar_one_or_none()
            if row is None or row.state != STATE_SENDING or row.lease_owner != owner:
                return False
            return bool(row.lease_expires_at is not None and row.lease_expires_at > moment)
    except Exception:
        return False


async def _resolve(session_factory, intent_key: str, *, owner: Optional[str], state: str,
                   reason: Optional[str], real_trade_id: Optional[int] = None,
                   now: Optional[datetime] = None, require_owner: bool = True) -> bool:
    moment = now or _now()
    values = {"state": state, "reason": (_label(reason, 64) or None), "updated_at": moment,
              "lease_owner": None, "lease_expires_at": None,
              "resolved_at": moment if state in (STATE_CONFIRMED, STATE_TERMINAL) else None}
    if real_trade_id is not None:
        values["real_trade_id"] = int(real_trade_id)
    try:
        async with session_factory() as session:
            conditions = [EntryIntent.intent_key == intent_key]
            if require_owner and owner is not None:
                conditions.append(EntryIntent.lease_owner == owner)
            if state == STATE_CONFIRMED:
                # Vínculo idempotente: um fill confirmado não é sobrescrito.
                conditions.append(EntryIntent.state != STATE_CONFIRMED)
            result = await session.execute(update(EntryIntent).where(*conditions).values(**values))
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def mark_confirmed(session_factory, intent_key: str, *, owner: Optional[str] = None,
                         real_trade_id: Optional[int] = None, reason: str = "FILL_CONFIRMED",
                         now: Optional[datetime] = None) -> bool:
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_CONFIRMED,
                          reason=reason, real_trade_id=real_trade_id, now=now, require_owner=False)


async def mark_unknown(session_factory, intent_key: str, *, owner: Optional[str] = None,
                       reason: str = "DISPATCH_OUTCOME_UNKNOWN",
                       now: Optional[datetime] = None) -> bool:
    """Desfecho incerto: exige reconciliação P03 pelo mesmo client id."""
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_UNKNOWN,
                          reason=reason, now=now, require_owner=False)


async def mark_terminal(session_factory, intent_key: str, *, owner: Optional[str] = None,
                        reason: str = "NO_ECONOMIC_ENTRY",
                        now: Optional[datetime] = None) -> bool:
    """Encerra a decisão SEM entrada econômica (não preenchida, recusada...)."""
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_TERMINAL,
                          reason=reason, now=now, require_owner=False)


async def release_reserved(session_factory, intent_key: str, *, owner: str,
                           reason: str = "NOT_DISPATCHED", now: Optional[datetime] = None) -> bool:
    """Abandona uma reserva que NUNCA despachou (estado RESERVED apenas)."""
    moment = now or _now()
    try:
        async with session_factory() as session:
            result = await session.execute(
                update(EntryIntent)
                .where(EntryIntent.intent_key == intent_key, EntryIntent.state == STATE_RESERVED,
                       EntryIntent.lease_owner == owner, EntryIntent.dispatches == 0)
                .values(state=STATE_TERMINAL, reason=(_label(reason, 64) or None),
                        lease_owner=None, lease_expires_at=None,
                        updated_at=moment, resolved_at=moment))
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def recover_stale(session_factory, *, now: Optional[datetime] = None, limit: int = 100) -> dict:
    """Boot/manutenção: lease vencido em SENDING vira UNKNOWN (nunca reenvio).

    Intenções pendentes NÃO são apagadas e TTL não presume ordem inexistente.
    """
    moment = now or _now()
    summary = {"to_unknown": 0, "reserved_released": 0}
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(EntryIntent).where(EntryIntent.state.in_((STATE_SENDING, STATE_RESERVED)),
                                          EntryIntent.lease_expires_at.is_not(None),
                                          EntryIntent.lease_expires_at <= moment).limit(limit)
            )).scalars().all()
            for row in rows:
                if row.state == STATE_SENDING:
                    row.state, row.reason = STATE_UNKNOWN, "LEASE_EXPIRED_AFTER_DISPATCH"
                    summary["to_unknown"] += 1
                else:
                    # Reserva sem despacho: libera o lease, mantém a intenção.
                    summary["reserved_released"] += 1
                row.lease_owner, row.lease_expires_at = None, None
                row.updated_at = moment
            await session.commit()
    except Exception:
        summary["error"] = "DB_UNAVAILABLE"
    return summary


async def get_intent(session_factory, intent_key: str):
    try:
        async with session_factory() as session:
            return (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
            )).scalar_one_or_none()
    except Exception:
        return None
