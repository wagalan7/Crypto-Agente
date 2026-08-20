"""
Execution Incident — reconciliação persistente de execução (P03).

Registra estados de execução inseguros/UNKNOWN que precisam sobreviver a
restart e ser reconciliados até um estado seguro (`PROTECTED`, `FLAT`) ou
escalados para intervenção humana (`MANUAL_REQUIRED`).

Reutiliza o hardening P02: a existência de QUALQUER incidente aberto mantém a
quarentena de execução (latch em memória + pausa no `RiskState`), bloqueando
novas entradas — inclusive após restart. Nenhum motor de ordens novo é criado;
o reconciliador só CONSULTA a exchange (get_order/positionRisk) e reutiliza as
primitivas P02 de proteção/cancelamento/fechamento emergencial.

Singleton por `incident_key` único → idempotência (mesmo evento 2x ou restart
2x geram 1 registro lógico). `claimed_by`/`lease_expires_at` dão lock/lease
persistente para que API e Worker nunca processem o mesmo incidente juntos.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class ExecutionIncident(Base):
    __tablename__ = "execution_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Idempotência: mesma submissão/posição gera SEMPRE a mesma chave.
    incident_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)

    exchange: Mapped[str] = mapped_column(String(20), default="binance", index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)

    # Um dos KINDS (ver execution_reconciliation_service.Kind).
    kind: Mapped[str] = mapped_column(String(40), index=True)

    # Lifecycle: OPEN → RECONCILING → PROTECTED|FLAT ; segurança:
    # RETRY_PENDING, MANUAL_REQUIRED (ver State).
    state: Mapped[str] = mapped_column(String(24), default="OPEN", index=True)

    # Identidade da ordem/condicionais (nunca reenviamos a entry — só consultamos).
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conditional_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # IDs EXATOS de SL/TP1/TP2 (algoId/clientAlgoId) — sem prefix match amplo.
    conditional_ids: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    side: Mapped[str | None] = mapped_column(String(8), nullable=True)   # buy|sell
    planned_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fill OBSERVADO antes do terminal = apenas lower bound (nunca qty final).
    min_known_fill: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_stop: Mapped[float | None] = mapped_column(Float, nullable=True)

    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Observações negativas em ciclos SEPARADOS antes de declarar cleanup.
    clean_observations: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manual_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Lock/lease persistente (não basta lock em memória).
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
