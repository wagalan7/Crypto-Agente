"""
Teste de aceite #3 — circuit breaker de drawdown.

Critério (issue #3): "5 perdedores em sequência devem disparar auto-pause
quando daily DD ≤ -3%".

Executa em SQLite in-memory pra não tocar o DB de produção. Mocka a
estrutura mínima de db.py + injeta snapshots fake → chama
risk_service.update_and_check() → valida que disparou pause.

Como rodar:
  cd backend && .venv311/bin/python scripts/test_circuit_breaker.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Evita o engine Postgres do db.py — deixamos DATABASE_URL vazio e
# configuramos manualmente engine SQLite logo abaixo.
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main():
    # Import depois de setar env (db.py vai inicializar com DB_ENABLED=False)
    import db
    from db import Base
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    # Reconfigura engine pra SQLite (asyncpg só serve pra Postgres)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Patch
    db._engine = engine
    db._SessionLocal = SessionLocal
    db.DB_ENABLED = True

    # Importa modelos depois do patch pra registrar metadata
    from models.recommendation_snapshot import RecommendationSnapshot
    from models.risk_state import RiskState
    from models.risk_event import RiskEvent
    # CalibrationVersion também precisa ser importado pra metadata, mas tem JSONB
    from models import calibration_version  # noqa: F401
    from models import heartbeat  # noqa: F401

    # Workaround: JSONB (Postgres-only) não compila pro SQLite.
    # Substitui por JSON genérico em todas as colunas JSONB antes do create_all.
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Insere 5 snapshots PERDEDORES recentes (1R cada, risk_pct=1% → DD = -5%)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        for i in range(5):
            session.add(RecommendationSnapshot(
                symbol=f"FAKE{i}/USDT:USDT",
                timeframe="1h",
                tier="A",
                direction="long",
                entry=100.0,
                stop_loss=99.0,
                tp1=101.0,
                tp2=102.0,
                score=70.0,
                risk_reward=2.0,
                leverage=5,
                risk_pct=1.0,
                stop_distance_pct=1.0,
                status="lost",
                realized_r=-1.0,
                outcome_at=now - timedelta(hours=i),
                created_at=now - timedelta(hours=i + 1),
            ))
        await session.commit()

    # Chama o circuit breaker
    from services import risk_service
    snap = await risk_service.update_and_check()

    # Asserts
    print("\n📊 Estado após 5 perdedores (-1R cada, risk_pct=1%):")
    print(f"  daily_dd_pct      = {snap['daily_dd_pct']}")
    print(f"  daily_trades      = {snap['daily_trades']}")
    print(f"  trading_paused    = {snap['trading_paused']}")
    print(f"  pause_reason      = {snap['pause_reason']}")
    print(f"  pause_manual      = {snap['pause_manual']}")

    expected_dd = -5.0
    assert abs(snap["daily_dd_pct"] - expected_dd) < 0.01, \
        f"DD esperado {expected_dd}%, veio {snap['daily_dd_pct']}%"
    assert snap["trading_paused"] is True, "trading_paused deveria ser True"
    assert snap["pause_manual"] is False, "pause_manual deveria ser False (foi auto)"
    assert "diário" in snap["pause_reason"].lower() or "limit" in snap["pause_reason"].lower(), \
        f"Motivo deveria mencionar limite diário: {snap['pause_reason']}"
    print("\n✅ AUTO-PAUSE disparou corretamente.")

    # Verifica evento gravado
    events = await risk_service.list_events(days=1)
    print(f"\n📜 Eventos gravados: {len(events)}")
    for ev in events:
        print(f"  - {ev['event_type']:14} · {ev['reason']}")
    auto_pause_events = [e for e in events if e["event_type"] == "auto_pause"]
    assert len(auto_pause_events) >= 1, "Deveria ter ≥1 evento auto_pause"
    print("✅ Evento auto_pause gravado em risk_events.")

    # Teste 2: kill-switch manual → unpause → repause via update_and_check
    # NÃO deve re-disparar auto_pause se já estava manual_paused
    await risk_service.set_manual_pause(False, "teste reset")
    snap2 = await risk_service.update_and_check()
    print(f"\n📊 Após reset manual + update_and_check:")
    print(f"  trading_paused    = {snap2['trading_paused']}")
    # DD ainda está em -5%, então deve repausar (auto)
    assert snap2["trading_paused"] is True, "DD ainda -5%, deveria repausar"
    print("✅ Auto-pause re-dispara se DD ainda está abaixo do limite.")

    # Teste 3: cenário VIÁVEL (3 perdas + 2 ganhos → DD ~ -1% > limite)
    async with SessionLocal() as session:
        # Limpa tabelas
        from sqlalchemy import delete
        await session.execute(delete(RecommendationSnapshot))
        await session.execute(delete(RiskState))
        await session.execute(delete(RiskEvent))
        await session.commit()
        # Insere 3 perdas + 2 ganhos pequenos (DD final = -1%)
        for i in range(3):
            session.add(RecommendationSnapshot(
                symbol=f"L{i}/USDT:USDT", timeframe="1h", tier="A", direction="long",
                entry=100.0, stop_loss=99.0, tp1=101.0, tp2=102.0, score=70.0,
                risk_reward=2.0, leverage=5, risk_pct=1.0, stop_distance_pct=1.0,
                status="lost", realized_r=-1.0,
                outcome_at=now - timedelta(hours=i), created_at=now - timedelta(hours=i+1),
            ))
        for i in range(2):
            session.add(RecommendationSnapshot(
                symbol=f"W{i}/USDT:USDT", timeframe="1h", tier="A", direction="long",
                entry=100.0, stop_loss=99.0, tp1=101.0, tp2=102.0, score=70.0,
                risk_reward=2.0, leverage=5, risk_pct=1.0, stop_distance_pct=1.0,
                status="won_tp2", realized_r=1.0,
                outcome_at=now - timedelta(hours=i+3), created_at=now - timedelta(hours=i+4),
            ))
        await session.commit()

    snap3 = await risk_service.update_and_check()
    print(f"\n📊 Cenário 'viável' (-1% DD):")
    print(f"  daily_dd_pct      = {snap3['daily_dd_pct']}")
    print(f"  trading_paused    = {snap3['trading_paused']}")
    assert abs(snap3["daily_dd_pct"] - (-1.0)) < 0.01
    assert snap3["trading_paused"] is False, "DD -1% > limite -3%, NÃO deveria pausar"
    print("✅ DD acima do limite mantém trading ativo.")

    print("\n🎉 TODOS OS TESTES PASSARAM — circuit breaker #3 validado.")


if __name__ == "__main__":
    asyncio.run(main())
