"""
Sweep Progress — progresso do backtest massivo, persistido pra ser visível
ENTRE PROCESSOS.

Motivo: com a Opção B (worker separado), o sweep roda num processo diferente do
serviço web. O `_PROGRESS` em memória do worker é invisível pro web, então o
endpoint `/api/backtest/universe/status` (e o `sweep_milestone.py`) ficariam
cegos. Aqui o worker escreve o snapshot do progresso (JSON) numa linha singleton
(id=1) e o web lê de volta — mesma fonte da verdade pros dois processos.

Singleton: sempre id=1. `progress` é o dict completo do _PROGRESS.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import Integer, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class SweepProgress(Base):
    __tablename__ = "sweep_progress"

    # Singleton: sempre id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Snapshot completo do _PROGRESS (running, done, computed, errors, current...)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
