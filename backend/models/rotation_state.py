"""
Rotation Universe State — estado persistido do motor de rotação (FASE 2).

Singleton (sempre 1 linha id=1) que guarda:
  • `universe`: a allowlist de execução gerida pela rotação (lista de bases).
    Semente = EXEC_UNIVERSE_ALLOWLIST do env no primeiro apply; depois a rotação
    muta ESTA lista (não o env). O env vira só seed/fallback.
  • `pending`: contadores de histerese por base — quantos ciclos consecutivos
    um candidato apareceu como promote/demote. Só vira mudança real quando
    sustenta ROTATION_HYSTERESIS_CYCLES ciclos. Formato:
        {"BASE": {"action": "promote"|"demote", "count": int}}

Sobrevive a redeploy (persistido). NUNCA é tocado quando ROTATION_AUTO_APPLY=off
(default) — nesse caso a rotação segue 100% dry-run, igual à FASE 1.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class RotationUniverseState(Base):
    __tablename__ = "rotation_universe_state"

    # Singleton: sempre id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Allowlist de execução gerida pela rotação (lista de bases, ex: ["BTC","ETH"]).
    universe: Mapped[list] = mapped_column(JSON, default=list)

    # Contadores de histerese: {"BASE": {"action": "promote"|"demote", "count": N}}
    pending: Mapped[dict] = mapped_column(JSON, default=dict)

    # Última vez que o preview semanal (seg 09h BRT) foi enviado ao Telegram.
    # Idempotência: evita reenviar a mesma ocorrência após redeploy.
    last_preview_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
