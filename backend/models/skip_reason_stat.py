"""
Persistência dos motivos de skip do loop de execução (assertividade).

Diferente do dict in-memory `_LAST_SKIP_REASONS` (que reseta a cada redeploy do
Railway), esta tabela acumula **contadores por gate por dia** (UTC). Vantagens:
- sobrevive redeploy (responde "qual gate mais barrou trade na semana?");
- limitada por construção (~20 gates × N dias) — nada de inflar linha por skip;
- permite janela temporal (somar counts dos últimos N dias).

Guarda também o último motivo/símbolo observado naquele bucket pra dar exemplo
no painel sem ter que persistir cada evento.
"""
from __future__ import annotations
from datetime import date as _date, datetime, timezone

from sqlalchemy import String, Integer, Date, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class SkipReasonStat(Base):
    __tablename__ = "skip_reason_stats"
    __table_args__ = (
        UniqueConstraint("gate", "day", name="uq_skip_gate_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    gate: Mapped[str] = mapped_column(String(40), index=True)
    day: Mapped[_date] = mapped_column(Date, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_symbol: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "day": self.day.isoformat() if self.day else None,
            "count": self.count,
            "last_reason": self.last_reason,
            "last_symbol": self.last_symbol,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }
