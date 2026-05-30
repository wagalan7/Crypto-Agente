"""
Push Subscription — armazena assinaturas Web Push (browser-side endpoint
+ chaves). Cada device que ativar notificações vira um registro.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Endpoint URL único do browser/device (FCM, APNs, Mozilla — varia)
    endpoint: Mapped[str] = mapped_column(String(500), unique=True, index=True)

    # Chaves criptográficas da subscription (geradas pelo browser)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(64))

    # Metadados pra debug
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Filtros: usuário pode escolher quais tiers receber
    notify_a_plus: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_a: Mapped[bool] = mapped_column(Boolean, default=True)
    # Default True: B é objetivamente o melhor tier do sistema atual
    # (WR 84% PF 6.7), maior produção de sinais. Subscribers antigos com
    # notify_b=False permanecem opt-out até trocarem na UI.
    notify_b: Mapped[bool] = mapped_column(Boolean, default=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
