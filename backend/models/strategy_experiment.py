"""
StrategyExperiment — ciclo de vida governado de um candidato de estratégia (P05).

Uma linha = UM candidato versionado, do rascunho até a decisão. É a única tabela
nova do P05: guarda a configuração canônica, os hashes de identidade, as métricas
offline/shadow e a decisão final.

Contrato de identidade (idempotência):
  experiment_key = f"{champion_hash}:{candidate_hash}:{dataset_cutoff}"
Reavaliar o MESMO candidato sobre o MESMO champion e o MESMO corte de dataset
reaproveita a linha existente — retry/restart não cria experimento duplicado.

Ciclo de vida (transições válidas, sem saltos):
  DRAFT              → INSUFFICIENT_DATA | REJECTED | OFFLINE_VALIDATED
  OFFLINE_VALIDATED  → SHADOW
  SHADOW             → REJECTED | ELIGIBLE

`ELIGIBLE` significa "pode ser APRESENTADO ao usuário para autorização manual".
NÃO significa que foi ativado: o P05 nunca promove nada para o LIVE.

Imutabilidade: `candidate_config`, `champion_hash` e `candidate_hash` são
congelados após o DRAFT — só métricas, status e decisão evoluem.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identidade lógica (única) — ver contrato no docstring do módulo.
    experiment_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    champion_hash: Mapped[str] = mapped_column(String(64), index=True)
    candidate_hash: Mapped[str] = mapped_column(String(64), index=True)

    # DRAFT | INSUFFICIENT_DATA | REJECTED | OFFLINE_VALIDATED | SHADOW | ELIGIBLE
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    # LOSS_REDUCTION | MORE_OPERATIONS
    objective: Mapped[str] = mapped_column(String(24))

    # Configuração canônica do candidato (UM knob de diferença vs champion).
    candidate_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Amostra usada (fingerprint = SHA-256 do conteúdo; cutoff = borda temporal).
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    offline_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    shadow_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    decision: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    shadow_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_strategy_exp_status_created", "status", "created_at"),
    )

    def to_dict(self, *, full: bool = False) -> dict:
        """`full=False` omite payloads grandes (listagem paginada)."""
        base = {
            "id": self.id,
            "experiment_key": self.experiment_key,
            "champion_hash": self.champion_hash,
            "candidate_hash": self.candidate_hash,
            "status": self.status,
            "objective": self.objective,
            "candidate_config": self.candidate_config,
            "dataset_fingerprint": self.dataset_fingerprint,
            "dataset_cutoff": self.dataset_cutoff.isoformat() if self.dataset_cutoff else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "shadow_started_at": self.shadow_started_at.isoformat() if self.shadow_started_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
        if full:
            base["offline_metrics"] = self.offline_metrics
            base["shadow_metrics"] = self.shadow_metrics
            base["decision"] = self.decision
        else:
            # Resumo barato: só o veredito, sem as séries/folds.
            dec = self.decision or {}
            base["decision_summary"] = {
                "verdict": dec.get("verdict"),
                "reason_code": dec.get("reason_code"),
            }
        return base
