"""
Parâmetros APRENDIDOS por moeda a partir do histórico COMPLETO (pós-sweep).

Ponte que faltava entre `symbol_backtest_stats` (tabela de PESQUISA, não consumida
ao vivo) e a operação em tempo real. Depois que o sweep cobre todo o universo, o
`symbol_learning_service` percorre cada (base, timeframe), lê a edge de todo o
histórico e destila um punhado de tunáveis por-moeda — hoje um multiplicador de
size defensivo/amplificador limitado — persistidos aqui.

Uma linha por (base, timeframe). Reexecutar o aprendizado faz UPSERT. Sobrevive a
redeploy. NÃO altera nada ao vivo sozinho: a stack de sizing só lê estes valores
quando SYMBOL_LEARNING_SIZE_ENABLED=true (default OFF) — pra revisão humana antes.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class SymbolLearnedParams(Base):
    __tablename__ = "symbol_learned_params"
    __table_args__ = (
        UniqueConstraint("base", "timeframe", name="uq_learned_base_tf"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(50), index=True)       # ex.: "TAG"
    timeframe: Mapped[str] = mapped_column(String(10), index=True)  # ex.: "4h"

    # Tunável principal derivado do histórico completo: multiplicador de size.
    # Defensivo (<1.0) p/ edge fraca; amplificador (>1.0) limitado p/ edge forte.
    # Clampado no service; os caps duros de _compute_qty mandam depois.
    size_quality_mult: Mapped[float] = mapped_column(Float, default=1.0)

    # Confiança [0,1] derivada do tamanho de amostra (n_trades / wf_n_trades).
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Origem do aprendizado ("backtest_history" | "manual").
    source: Mapped[str] = mapped_column(String(30), default="backtest_history")

    # Metadados que originaram a decisão (auditoria / inspeção no painel).
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    wf_avg_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    wf_n_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expiry_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibrated_edge: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Bolsa extensível pra novos tunáveis sem migração de schema.
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    learned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "timeframe": self.timeframe,
            "size_quality_mult": self.size_quality_mult,
            "confidence": self.confidence,
            "source": self.source,
            "n_trades": self.n_trades,
            "wf_avg_r": self.wf_avg_r,
            "wf_n_trades": self.wf_n_trades,
            "expiry_pct": self.expiry_pct,
            "calibrated_edge": self.calibrated_edge,
            "params": self.params or {},
            "learned_at": self.learned_at.isoformat() if self.learned_at else None,
        }
