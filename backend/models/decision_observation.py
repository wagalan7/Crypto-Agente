"""R09: armazenamento exclusivamente observacional, sem FK para trades reais.

Oportunidade != tentativa != resultado do replay. Nenhuma destas tabelas é
fonte de calibração, aprendizagem, sizing, risco, PnL ou trade manager.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class DecisionObservation(Base):
    __tablename__ = "decision_observations"

    opportunity_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_source: Mapped[str] = mapped_column(String(24))
    scope: Mapped[str] = mapped_column(String(24), default="POST_SELECTION")
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Decisão/bloqueio/setup = primeira tentativa PERSISTIDA (imutáveis).
    # first_seen_at = observação mais antiga persistida (pode ser anterior).
    first_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_decision_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_blocker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frozen_setup: Mapped[dict] = mapped_column(JSONB)
    frozen_config: Mapped[dict] = mapped_column(JSONB)
    score_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DecisionObservationAttempt(Base):
    __tablename__ = "decision_observation_attempts"

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    opportunity_key: Mapped[str] = mapped_column(String(64), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    mode: Mapped[str] = mapped_column(String(12))
    result: Mapped[str] = mapped_column(String(32))
    first_blocker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submit_evidence: Mapped[str] = mapped_column(String(24), default="NOT_OBSERVED")
    score_trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class RejectedSetupObservation(Base):
    __tablename__ = "rejected_setup_observations"

    opportunity_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    first_blocker: Mapped[str] = mapped_column(String(64))
    frozen_setup: Mapped[dict] = mapped_column(JSONB)
    frozen_config: Mapped[dict] = mapped_column(JSONB)
    coverage: Mapped[str] = mapped_column(String(32), default="UNAVAILABLE", index=True)
    candles: Mapped[list] = mapped_column(JSONB, default=list)
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
