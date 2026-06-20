"""
Resultado do backtest histórico por (símbolo, timeframe) — a "memória" do estudo
massivo que roda no DEV sobre TODO o universo desde a listagem de cada moeda.

Propósito: quebrar o "pega-22" da allowlist. Uma moeda fora do universo de
execução nunca executa → nunca acumula amostra → a rotação nunca a promove. O
backtest gera essa amostra OFFLINE (sem risco), persiste a edge por moeda aqui, e
o ranking vira candidata à allowlist do PRD (revisão humana antes de subir).

Uma linha por (symbol, timeframe). Reexecutar o backtest faz UPSERT (atualiza
métricas + computed_at). Acumula entre redeploys do DEV.

NÃO é consumido por nenhum agregador de PnL/risco/learner do PRD — é uma tabela
de PESQUISA, lida só pelo ranking e pela decisão de allowlist.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, DateTime, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class SymbolBacktestStats(Base):
    __tablename__ = "symbol_backtest_stats"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", name="uq_backtest_symbol_tf"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), index=True)

    # Cobertura histórica avaliada
    candles: Mapped[int] = mapped_column(Integer, default=0)          # candles carregados
    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    full_history: Mapped[bool] = mapped_column(Boolean, default=True)  # backtest desde a listagem

    # Métricas de outcome (mesma simulação de produção: TP1/BE/trail/time-stop)
    n_trades: Mapped[int] = mapped_column(Integer, default=0, index=True)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    expired: Mapped[int] = mapped_column(Integer, default=0)
    wr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)          # wins/n (bruta)
    wr_clean_pct: Mapped[float | None] = mapped_column(Float, nullable=True)    # wins/(wins+losses)
    expiry_pct: Mapped[float | None] = mapped_column(Float, nullable=True)      # expired/n
    avg_r: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    total_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Out-of-sample (walk-forward) — o número em que confiar pra promover.
    wf_avg_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    wf_n_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "candles": self.candles,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "full_history": self.full_history,
            "n_trades": self.n_trades,
            "wins": self.wins,
            "losses": self.losses,
            "expired": self.expired,
            "wr_pct": self.wr_pct,
            "wr_clean_pct": self.wr_clean_pct,
            "expiry_pct": self.expiry_pct,
            "avg_r": self.avg_r,
            "total_r": self.total_r,
            "profit_factor": self.profit_factor,
            "wf_avg_r": self.wf_avg_r,
            "wf_n_trades": self.wf_n_trades,
            "error": self.error,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }
