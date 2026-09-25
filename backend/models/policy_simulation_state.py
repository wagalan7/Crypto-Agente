"""R11/R12 — estado PERSISTENTE da simulação (histerese e geração aprendida).

Estado que só existe dentro de uma dataclass não sobrevive a restart e não
prova concorrência. Esta tabela guarda, por experimento/versão/universo:

  • a geração publicada e a evidência que a justificou;
  • o período em que a última publicação aconteceu (histerese);
  • o instante da publicação, para auditoria.

Nada aqui é universo operacional, learned cache ou risco legado: é o estado da
SIMULAÇÃO. A exclusividade é transacional (chave única + compare-and-set sob
advisory lock), então duas simulações concorrentes não publicam duas gerações.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class PolicySimulationState(Base):
    __tablename__ = "policy_simulation_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Identidade lógica: experimento + versão da política + universo avaliado.
    state_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    experiment_key: Mapped[str] = mapped_column(String(200), index=True)
    policy_version: Mapped[str] = mapped_column(String(40))
    universe_version: Mapped[str] = mapped_column(String(64))
    population: Mapped[str] = mapped_column(String(16))

    generation: Mapped[int] = mapped_column(Integer, default=0)
    evidence_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("experiment_key", "policy_version", "universe_version",
                         "population", name="uq_policy_state_identity"),
    )
