"""
Heartbeat — last_alive_ts do backend (#6).

Singleton (id=1) atualizado a cada loop de server-scan. Gap entre
`last_alive_ts` e `now` > 5min indica que o backend parou de processar
(crash, restart, freeze). Endpoint /api/admin/health expõe pra alerta.

Reconciliação completa com exchange (cruzar posições do DB com
posições reais) fica pra quando #11 trouxer integração Bybit.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Integer, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class Heartbeat(Base):
    __tablename__ = "heartbeat"

    # Singleton: id=1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Último tick válido do server-scan loop
    last_alive_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    # Qual subsistema bateu por último (ajuda debug)
    last_source: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Contador monotônico de ticks (útil pra detectar freeze sem mudar ts)
    tick_count: Mapped[int] = mapped_column(Integer, default=0)
