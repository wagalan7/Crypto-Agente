"""
Trade simulado individual do sweep histórico — a matéria-prima por-trade que o
backtest produz (score, features, outcome) e que ANTES era só agregada em
`symbol_backtest_stats` e descartada.

Propósito (pedido do usuário 2026-06-25): deixar o cérebro do bot **aprender com
o histórico INTEIRO do universo**, não só com os trades reais resolvidos. Cada
linha é 1 entrada simulada com o MESMO pipeline de produção (build_trade_signal
→ score/tier → _classify_outcome_candles), então é diretamente "ingestível" pela
calibração (score→P(TP1)) e pelo learning por bucket.

SEGURANÇA: esta tabela é PESQUISA. O scoring de dinheiro real só a consome se
`CALIBRATION_INCLUDE_BACKTEST` estiver ON (default OFF). Sem a flag, serve apenas
pra calibração "blended" comparável (A/B) antes de qualquer ativação.

Idempotência: reexecutar o backtest de um (symbol, tf) faz DELETE+INSERT das suas
linhas — resumível e sem duplicar.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), index=True)
    tier: Mapped[str] = mapped_column(String(4), index=True)
    direction: Mapped[str] = mapped_column(String(8))            # long | short

    # Sinal no momento da entrada (mesmo score/tier de produção)
    score: Mapped[float] = mapped_column(Float, index=True)
    rr: Mapped[float | None] = mapped_column(Float, nullable=True)
    atr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Outcome resolvido (paridade 1:1 com RecommendationSnapshot.status)
    status: Mapped[str] = mapped_column(String(12), index=True)
    # won_tp2 | won_tp1_be | won_tp1 | lost | expired
    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Features de bucket (espelham learning_service)
    hour_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dow: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0=segunda
    patterns: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Momento histórico da entrada simulada
    bar_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Quando o backtest computou esta linha (pra prune/refresh)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        Index("ix_bttrade_symbol_tf", "symbol", "timeframe"),
        Index("ix_bttrade_tf_status", "timeframe", "status"),
        Index("ix_bttrade_bucket", "tier", "timeframe", "direction"),
    )
