"""
Database setup (PostgreSQL via Railway).

- Usa SQLAlchemy async com asyncpg.
- DATABASE_URL injetada automaticamente pelo Railway quando o serviço Postgres
  está no mesmo projeto. Se ausente (dev local sem banco), tudo que depende
  de DB é desativado graciosamente.
- `init_db()` cria tabelas se não existirem (idempotente).
"""
from __future__ import annotations
import os
import logging
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _normalize_url(url: str) -> str:
    """Railway dá `postgres://...` — SQLAlchemy 2.0 quer `postgresql+asyncpg://`"""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


_engine = None
_SessionLocal: Optional[async_sessionmaker[AsyncSession]] = None

DATABASE_URL = os.getenv("DATABASE_URL")
DB_ENABLED = bool(DATABASE_URL)

if DB_ENABLED:
    _url = _normalize_url(DATABASE_URL)
    _engine = create_async_engine(_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    _SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    log.info("Database habilitado (Postgres conectado).")
else:
    log.warning("DATABASE_URL não definido — persistência desabilitada.")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    if not _SessionLocal:
        raise RuntimeError("DB desabilitado (DATABASE_URL ausente).")
    async with _SessionLocal() as session:
        yield session


async def init_db():
    """Cria todas as tabelas declaradas em Base. Idempotente.

    Também roda migrações incrementais simples (ADD COLUMN IF NOT EXISTS) pra
    tabelas que já existem mas ganharam colunas novas — evita ter que dropar.
    """
    if not DB_ENABLED or _engine is None:
        return
    # Importa modelos pra registrar metadata
    from models import recommendation_snapshot  # noqa: F401
    from models import push_subscription  # noqa: F401
    from models import risk_state  # noqa: F401
    from models import risk_event  # noqa: F401
    from models import heartbeat  # noqa: F401
    from models import calibration_version  # noqa: F401
    from models import real_trade  # noqa: F401
    from models import live_test_state  # noqa: F401
    from models import skip_reason_stat  # noqa: F401
    from models import rotation_state  # noqa: F401
    from models import symbol_backtest_stats  # noqa: F401
    from models import symbol_learned_params  # noqa: F401
    from models import backtest_trade  # noqa: F401
    from models import sweep_progress  # noqa: F401
    from sqlalchemy import text
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Rotação FASE 2 — coluna do preview semanal (tabela já podia existir)
        await conn.execute(text(
            "ALTER TABLE rotation_universe_state "
            "ADD COLUMN IF NOT EXISTS last_preview_at TIMESTAMP WITH TIME ZONE"
        ))
        # Via aditiva "backtest_seed" — rastreia ativos/bloqueados da via de backtest
        await conn.execute(text(
            "ALTER TABLE rotation_universe_state "
            "ADD COLUMN IF NOT EXISTS seeded JSON DEFAULT '{}'"
        ))
        # Migrações incrementais
        await conn.execute(text(
            "ALTER TABLE recommendation_snapshots "
            "ADD COLUMN IF NOT EXISTS features JSONB"
        ))
        # Step 2a: breakeven após TP1
        await conn.execute(text(
            "ALTER TABLE recommendation_snapshots "
            "ADD COLUMN IF NOT EXISTS tp1_hit_at TIMESTAMP WITH TIME ZONE"
        ))
        # Step 2b: trail por ATR no resto após TP1
        await conn.execute(text(
            "ALTER TABLE recommendation_snapshots "
            "ADD COLUMN IF NOT EXISTS peak_price_since_tp1 DOUBLE PRECISION"
        ))
        # #11.x: real_trades virou exchange-agnostic (era bybit-only)
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS exchange VARCHAR(20)"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS exchange_order_id VARCHAR(64)"
        ))
        # Fase 2 — trade manager (bracket TP1/TP2 + breakeven pós-TP1)
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS phase VARCHAR(16) DEFAULT 'pre_tp1'"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS qty_initial DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS sl_order_id VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS tp1_order_id VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS tp2_order_id VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS sl_current_price DOUBLE PRECISION"
        ))
        # Go-live Opção B — P&L parcial real embolsada no TP1 (somada no close)
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS tp1_realized_usd DOUBLE PRECISION"
        ))
        # Partials adaptativos (por-trade) — overrides decididos na abertura
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS adaptive_tp1_qty_pct DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS adaptive_runner_atr_mult DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS adaptive_runner_qty_pct DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS adaptive_test_idx INTEGER"
        ))
        # Feature 5 — pyramiding (reforço de winner pós-TP1) + hedge de regime
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS pyramiding_level INTEGER DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS hedge_for VARCHAR(64)"
        ))
        # Se a coluna antiga existir (deploy anterior), copia o valor pro novo nome.
        # Em Postgres o IF EXISTS no information_schema é mais seguro:
        await conn.execute(text("""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='real_trades' AND column_name='bybit_order_id'
              ) THEN
                UPDATE real_trades
                   SET exchange_order_id = COALESCE(exchange_order_id, bybit_order_id),
                       exchange = COALESCE(exchange, 'bybit')
                 WHERE bybit_order_id IS NOT NULL;
              END IF;
            END $$;
        """))
    log.info("Schema do banco verificado/criado (migrações aplicadas).")


async def close_db():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
