"""
Risk Event — histórico de mudanças de estado do circuit breaker.

Cada transição (pause/resume, automática ou manual) grava 1 linha.
Usado pelo painel "Status" pra mostrar histórico dos últimos 30 dias.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # event_type ∈ {auto_pause, auto_resume, manual_pause, manual_resume}
    event_type: Mapped[str] = mapped_column(String(20), index=True)

    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Snapshot das métricas no momento do evento
    daily_dd_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_dd_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
