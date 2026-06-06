"""
RealTrade — execuções reais (paralelo ao paper-trade) — #11.2.

Distingue "paper-trade" (simulado a partir de snapshots) de "real-trade"
(ordem executada na exchange). Pode ser:
  - source='manual'  → user marcou fill via UI (modo shadow manual)
  - source='auto'    → bot disparou ordem automaticamente (modo shadow auto)
  - source='bybit'   → ordem rastreada via order_history da Bybit

Liga opcionalmente a um `recommendation_snapshot.id` quando o trade veio
de uma rec do sistema — permite comparar slippage paper vs real (rec
prometeu entry=X, fill real veio em Y, diff = slippage).

Status fluxo: open → closed_tp1 | closed_tp2 | closed_be | closed_stop | closed_manual
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Index, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class RealTrade(Base):
    __tablename__ = "real_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Identificação
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    side: Mapped[str] = mapped_column(String(8))  # long | short
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    # "manual" | "auto" | "bybit"

    # Liga ao paper-trade quando aplicável (pra cruzar slippage)
    recommendation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("recommendation_snapshots.id"), nullable=True, index=True
    )

    # Exchange order tracking (quando source != 'manual')
    # `exchange` = "bybit" | "binance" | "okx" | ... (qualquer corretora suportada)
    exchange: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Tamanho da posição
    qty: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notional_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Entry
    entry_price: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)

    # Níveis planejados (cópia da rec na hora da execução)
    planned_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_tp2: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Exit
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # Resultado
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # "open" | "closed_tp1" | "closed_tp2" | "closed_be" | "closed_stop" | "closed_manual"

    realized_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # P&L REAL embolsada na perna parcial do TP1 (líquido da fee de saída do TP1,
    # sem a entry_fee — essa é contada uma vez no restante). Gravada em
    # _transition_to_post_tp1 com o fill real da corretora; somada no close_trade
    # pra não subcontar trades que batem TP1 e depois fecham em breakeven/stop.
    tp1_realized_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Slippage vs rec (em pips/%, pode ser negativo se a favor)
    entry_slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Bracket management (Fase 2 — trade manager) ──────────────────────
    # phase: "pre_tp1" (SL inicial em planned_stop) → "post_tp1" (SL movido pra entry/BE)
    phase: Mapped[str] = mapped_column(String(16), default="pre_tp1", index=True)
    # qty_initial preserva o tamanho original; `qty` pode diminuir após parcial.
    qty_initial: Mapped[float | None] = mapped_column(Float, nullable=True)
    # IDs das 3 ordens condicionais na exchange (SL + TP1 parcial + TP2 restante)
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tp1_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tp2_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Preço atual do SL ativo na exchange (muda quando vai pra breakeven)
    sl_current_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Notas livres
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_real_trades_status_opened", "status", "opened_at"),
        Index("ix_real_trades_source_opened", "source", "opened_at"),
    )
