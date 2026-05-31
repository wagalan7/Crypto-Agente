"""
CalibrationVersion — versionamento do modelo PAV (#9).

Cada snapshot da calibração é salvo aqui pra auditabilidade e A/B.
Permite comparar P(TP1) calibrada hoje vs há 30 dias e detectar drift.

Schema:
  - version: string semântica (ex: "2026-05-31T12:00Z")
  - total_resolved, p_global: métricas de amostra
  - bins_json: tabela completa de bins (snapshot do output PAV)
  - sharpe, expectancy, win_rate: métricas de performance retro
  - active: flag indicando qual é a versão "live"
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class CalibrationVersion(Base):
    __tablename__ = "calibration_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identificador semântico (ts ISO 8601 truncado)
    version: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # Metadata da amostra
    total_resolved: Mapped[int] = mapped_column(Integer, default=0)
    p_global: Mapped[float | None] = mapped_column(Float, nullable=True)
    lookback_days: Mapped[int] = mapped_column(Integer, default=90)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Tabela de bins completa (mesmo formato que get_calibration().bins retorna)
    bins_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Métricas retroativas (computadas no momento do snapshot)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Qual versão está em uso pelo scan
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # Notas livres (ex: "primeiro snapshot pós #4")
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
