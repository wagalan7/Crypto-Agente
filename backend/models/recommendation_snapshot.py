"""
Snapshot de uma recomendação no momento em que apareceu no painel.
Permite rastrear outcome (won/lost/open/expired) e calcular P&L diário.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
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
    # "open" | "won_tp1" | "won_tp1_be" | "won_tp2" | "lost" | "expired"
    # won_tp1_be = breakeven após TP1 (Step 2a): TP1 tocou, stop subiu pra entry, depois voltou.
    outcome_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    # múltiplo de R (Step 2b — parcial 50% no TP1 + trail no resto):
    #   +1.5 se TP2 (50% saiu em TP1 = +0.5R, 50% em TP2 = +1.0R)
    #   +0.5 se won_tp1_be (50% TP1 + 50% trail/BE)
    #   +0.5 se won_tp1 (expirou após TP1, conservador na metade restante)
    #   -1.0 se stop original bateu antes de TP1
    #    0   se expired sem nem tocar TP1

    # ── Features do setup pra learning loop ──────────────────────────────
    # JSONB com vetor de features: rsi, mtf_score, confluence_pct, patterns,
    # funding_pct, oi_change_pct, hour_utc, day_of_week, atr_pct, etc.
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Step 2a: quando TP1 é tocado, stop sobe pra entry (breakeven). Esse campo
    # marca o momento do hit — se None, posição ainda não tocou TP1.
    tp1_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Step 2b: trail por ATR após TP1. Guarda o pico do preço a favor desde o
    # TP1 hit (high pra long, low pra short). Usado pra calcular stop trailing
    # = peak ± K × ATR, com piso em entry. None até TP1 hit.
    peak_price_since_tp1: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_snap_status_created", "status", "created_at"),
        Index("ix_snap_symbol_created", "symbol", "created_at"),
    )
