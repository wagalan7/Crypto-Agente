"""
Snapshot de uma recomendação no momento em que apareceu no painel.
Permite rastrear outcome (won/lost/open/expired) e calcular P&L diário.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class RecommendationSnapshot(Base):
    __tablename__ = "recommendation_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identificação do setup
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    tier: Mapped[str] = mapped_column(String(4), index=True)
    direction: Mapped[str] = mapped_column(String(8))           # long | short

    # Níveis no momento da recomendação
    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2: Mapped[float] = mapped_column(Float)

    # Métricas
    score: Mapped[float] = mapped_column(Float)
    risk_reward: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    risk_pct: Mapped[float] = mapped_column(Float)
    stop_distance_pct: Mapped[float] = mapped_column(Float)

    # Status / outcome
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)
    # "open" | "won_tp1" | "won_tp2" | "lost" | "expired"
    outcome_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    # múltiplo de R: +2 se TP2, +1 se TP1, -1 se stop, 0 se expirado.

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_snap_status_created", "status", "created_at"),
        Index("ix_snap_symbol_created", "symbol", "created_at"),
    )
