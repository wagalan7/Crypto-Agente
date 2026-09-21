"""P03 — intenção de entrada econômica, reservada ANTES da primeira ordem.

Uma decisão = uma intenção. A chave é estável (não depende de relógio da
tentativa, qty, score, preço mutável ou retry) e o conteúdo material fica em
`payload_fingerprint`: mesma chave com payload divergente é CONFLITO, nunca uma
segunda entrada silenciosa.

Esta tabela é aditiva e nunca é apagada por rollback de código: uma intenção
pendente permanece pendente até ser reconciliada pelo MESMO `client_order_id`.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

#: Estados. RESERVED e SENDING são exclusivos por decisão; UNKNOWN exige
#: reconciliação P03 (consulta pelo mesmo client id) e NUNCA reenvio automático.
STATE_RESERVED = "RESERVED"
STATE_SENDING = "SENDING"
STATE_CONFIRMED = "CONFIRMED"
STATE_UNKNOWN = "UNKNOWN"
STATE_TERMINAL = "TERMINAL"
INTENT_STATES = (STATE_RESERVED, STATE_SENDING, STATE_CONFIRMED,
                 STATE_UNKNOWN, STATE_TERMINAL)
#: Estados que ainda ocupam capacidade/slot (a entrada pode existir na exchange).
PENDING_STATES = (STATE_RESERVED, STATE_SENDING, STATE_UNKNOWN)


class EntryIntent(Base):
    __tablename__ = "entry_intents"

    intent_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    account_ref: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    quote: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(8))
    position_side: Mapped[str] = mapped_column(String(10))
    timeframe: Mapped[str] = mapped_column(String(8))
    playbook: Mapped[str] = mapped_column(String(40))
    playbook_version: Mapped[str] = mapped_column(String(24))
    purpose: Mapped[str] = mapped_column(String(16))
    setup_id: Mapped[str] = mapped_column(String(64))
    trigger_candle_ms: Mapped[int] = mapped_column(BigInteger)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dispatches: Mapped[int] = mapped_column(Integer, default=0)
    reserved_risk_usd: Mapped[float] = mapped_column(Float, default=0.0)
    real_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
