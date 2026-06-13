"""
Live Test State — estado do "teste do canário a 0.50".

Singleton (sempre 1 linha id=1) que guarda apenas os flags de idempotência
das notificações de marco (pra não reenviar a cada scan / após redeploy).

A CONTAGEM em si (quantos auto-trades já saíram desde o marco) é derivada ao
vivo do banco (RealTrade source='auto' com opened_at >= start) — não é
persistida aqui, então sobrevive a redeploy sem risco de dessincronizar.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Integer, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class LiveTestState(Base):
    __tablename__ = "live_test_state"

    # Singleton: sempre id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Já avisei que bateu o alvo de N auto-trades?
    count_milestone_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Já avisei que fechou a janela de X dias?
    time_milestone_notified: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
