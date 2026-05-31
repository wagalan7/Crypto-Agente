"""
Risk State — circuit breaker de drawdown.

Singleton (sempre 1 linha id=1) com flag global de pausa + métricas
rolando. Persistente pra sobreviver restarts; reset automático
acontece na virada do dia / semana (UTC).

Pause triggers (Fase 1):
  - daily_dd_pct <= -3%    OU
  - weekly_dd_pct <= -6%

Pausa NÃO fecha posições abertas — só impede emissão de novas recs
via push. Trades já em andamento continuam sob trail/stop normais.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class RiskState(Base):
    __tablename__ = "risk_state"

    # Singleton: sempre id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Flag principal: server-scan / push respeita
    trading_paused: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Por que pausou (legível em PT-BR)
    pause_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Foi pause manual (kill switch) vs automático (DD)
    pause_manual: Mapped[bool] = mapped_column(Boolean, default=False)

    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Métricas rolling (atualizadas a cada scan)
    daily_dd_pct: Mapped[float] = mapped_column(Float, default=0.0)
    weekly_dd_pct: Mapped[float] = mapped_column(Float, default=0.0)
    daily_trades: Mapped[int] = mapped_column(Integer, default=0)
    weekly_trades: Mapped[int] = mapped_column(Integer, default=0)

    # Marca o dia/semana atual pra detectar virada e resetar
    current_day_utc: Mapped[str | None] = mapped_column(String(10), nullable=True)
    current_week_utc: Mapped[str | None] = mapped_column(String(10), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
