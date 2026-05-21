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
    from sqlalchemy import text
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
    log.info("Schema do banco verificado/criado (migrações aplicadas).")


async def close_db():
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
