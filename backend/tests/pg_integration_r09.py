"""R09: PostgreSQL real descartável, só socket Unix explicitamente autorizado.

R09_TEST_SOCKET deve apontar para /tmp/cw-r09-sock.* criado pelo teste.
Não lê DATABASE_URL externo. Sem rede, exchange ou efeitos operacionais.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch

test_socket = os.environ.get("R09_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r09-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r09@/r09db?host=" + test_socket
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R09")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R09")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R09")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

BAR = 300_000
R09_TABLES = {"decision_observations", "decision_observation_attempts",
              "rejected_setup_observations"}


def bars(first_ms, specs):
    return [{"timestamp": first_ms + i * BAR, "open": o, "high": h, "low": l,
             "close": c, "volume": 10.0} for i, (o, h, l, c) in enumerate(specs)]


async def run():
    from sqlalchemy import func, select, text, update
    import db
    from models.decision_observation import DecisionObservation as O, DecisionObservationAttempt as A
    from models.decision_observation import RejectedSetupObservation as R
    from services import decision_observation_service as obs

    async def operational_counts():
        counts = {}
        async with db.get_session() as session:
            for table in sorted(db.Base.metadata.tables.values(), key=lambda t: t.name):
                if table.name not in R09_TABLES:
                    counts[table.name] = await session.scalar(select(func.count()).select_from(table))
        return counts

    async def scalar(stmt):
        async with db.get_session() as session:
            return await session.scalar(stmt)

    async def rejected(key):
        async with db.get_session() as session:
            return (await session.execute(select(R).where(R.opportunity_key == key))).scalar_one()

    await db.init_db()
    await db.init_db()
    operational_before = await operational_counts()
    now = datetime.now(timezone.utc)
    rec = {"symbol": "SYNTH/USDT:USDT", "timeframe": "15m", "direction": "long",
           "tier": "A", "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0,
           "tp2": 110.0, "score": 65.0, "_snapshot_id": 1234}
    obs.begin_batch([rec], mode="LIVE")
    obs.stage_decision(rec, "REJECTED", "score-min")
    # Coleta concorrente NÃO drena tentativa ainda ativa.
    await obs.flush_pending()
    assert (await scalar(select(func.count()).select_from(A))) == 0
    obs.seal_batch([rec])
    await obs.flush_pending()
    report = await obs.get_status(7)
    assert report["state"] == "AVAILABLE", report
    assert report["unique_opportunities"] == 1, report
    assert report["attempts"] == 1, report
    assert report["first_blockers"] == {"score-min": 1}, report
    assert report["capacity"]["state"] == "OK", report

    # Repetir o flush é idempotente; reavaliar cria tentativa, não oportunidade.
    await obs.flush_pending()
    retry = deepcopy(rec)
    retry["entry"] = 101.0
    obs.begin_batch([retry], mode="LIVE")
    obs.stage_decision(retry, "REJECTED", "liquidity-gate")
    obs.seal_batch([retry])
    await obs.flush_pending()
    report = await obs.get_status(7)
    assert report["unique_opportunities"] == 1 and report["attempts"] == 2, report
    assert report["reevaluations"] == 1, report
    async with db.get_session() as session:
        original = (await session.execute(select(O))).scalar_one()
        assert original.frozen_setup["entry"] == 100.0
        assert original.first_blocker == "score-min"
        assert (await session.scalar(select(func.count()).select_from(R))) == 1

    # Duas transações da implementação real, mesma oportunidade: first-writer
    # preservado, tentativas idempotentes, nenhum erro de chave concorrente.
    replicas = [deepcopy(rec), deepcopy(rec)]
    replicas[0]["_snapshot_id"] = replicas[1]["_snapshot_id"] = 5678
    obs.begin_batch(replicas, mode="LIVE")
    for item in replicas:
        obs.stage_decision(item, "REJECTED", "risk-budget")
    obs.seal_batch(replicas)
    batches = list(obs._pending.items())
    obs._pending.clear()
    outcomes = await asyncio.gather(obs._flush_batch(batches[:1], {}), obs._flush_batch(batches[1:], {}))
    assert set(outcomes) <= {"ADMITTED", "CONTENTION"}, outcomes
    await obs._flush_batch(batches, {})
    report = await obs.get_status(7)
    assert report["unique_opportunities"] == 2 and report["attempts"] == 4, report
    assert report["rejected_shadow"]["economic_outcomes_exposed"] is False
    assert report["telemetry"]["flush_errors"] == 0, report

    # Ordem fora de sequência entre processos: a observação mais antiga chega
    # depois. first_seen_at = mais antiga; decisão = primeira PERSISTIDA.
    late, early = deepcopy(rec), deepcopy(rec)
    late["_snapshot_id"] = early["_snapshot_id"] = 9999
    obs.begin_batch([early], mode="LIVE")
    obs.stage_decision(early, "REJECTED", "gate-early")
    obs.seal_batch([early])
    early_batch = list(obs._pending.items())
    obs._pending.clear()
    obs.begin_batch([late], mode="LIVE")
    obs.stage_decision(late, "REJECTED", "gate-late")
    obs.seal_batch([late])
    late_batch = list(obs._pending.items())
    obs._pending.clear()
    assert early_batch[0][1]["observed_at"] < late_batch[0][1]["observed_at"]
    await obs._flush_batch(late_batch, {})
    await obs._flush_batch(early_batch, {})
    async with db.get_session() as session:
        row = (await session.execute(select(O).where(
            O.opportunity_key == late_batch[0][1]["opportunity_key"]))).scalar_one()
        assert row.first_blocker == "gate-late", row.first_blocker
        assert row.first_seen_at == early_batch[0][1]["observed_at"]
        assert row.first_decision_observed_at == late_batch[0][1]["observed_at"]
    report = await obs.get_status(7)
    assert report["out_of_order_first"] == 1, report
    assert report["order_semantics"]["first_decision"] == "FIRST_PERSISTED_ATTEMPT"

    # ── Replay REAL das vetadas: resolvido, terminal não reprocessado ──
    past = now - timedelta(hours=1)
    first_ms = ((int(past.timestamp() * 1000) + BAR - 1) // BAR) * BAR
    key_stop = batches[0][1]["opportunity_key"]
    async with db.get_session() as session:
        await session.execute(update(R).where(R.opportunity_key == key_stop).values(decision_at=past))
        await session.commit()
    stop_path = bars(first_ms, [(100, 101, 99, 100), (99, 99.5, 94, 95)])
    obs._wanted_symbols = None
    await obs.observe_candles(rec["symbol"], stop_path, as_of=now)
    await obs.flush_pending()
    resolved = await rejected(key_stop)
    assert resolved.coverage == "RESOLVED", (resolved.coverage, resolved.outcome)
    assert resolved.outcome["status"] == "CLOSED_STOP", resolved.outcome
    assert resolved.outcome["gross_r"] == -1.0 and resolved.outcome["net_r"] is None
    assert resolved.outcome["learning_eligible"] is False
    version = resolved.version
    obs._wanted_symbols = None
    await obs.observe_candles(rec["symbol"], stop_path + bars(first_ms + 2 * BAR, [(95, 96, 94, 95)]), as_of=now)
    await obs.flush_pending()
    assert (await rejected(key_stop)).version == version, "terminal reprocessado"

    # Setup congelado inválido: terminal INVALID, não fica na fila para sempre.
    broken = {**deepcopy(rec), "_snapshot_id": 4242, "stop_loss": 101.0}
    obs.begin_batch([broken], mode="SHADOW")
    obs.stage_decision(broken, "REJECTED", "rr-gate")
    obs.seal_batch([broken])
    key_broken = next(iter(obs._pending.values()))["opportunity_key"]
    await obs.flush_pending()
    async with db.get_session() as session:
        await session.execute(update(R).where(R.opportunity_key == key_broken).values(decision_at=past))
        await session.commit()
    obs._wanted_symbols = None
    await obs.observe_candles(rec["symbol"], stop_path, as_of=now)
    await obs.flush_pending()
    invalid = await rejected(key_broken)
    assert invalid.coverage == "INVALID", invalid.coverage
    assert "INVALID_FROZEN_SETUP" in invalid.outcome["reason_codes"]
    assert invalid.coverage in obs.TERMINAL_COVERAGE

    # Linha antiga ainda aberta (sem velas) para provar que o resolver segue
    # trabalhando com a admissão lotada.
    other = {**deepcopy(rec), "symbol": "OTHER/USDT:USDT", "_snapshot_id": 7777}
    obs.begin_batch([other], mode="LIVE")
    obs.stage_decision(other, "REJECTED", "score-min")
    obs.seal_batch([other])
    key_other = next(iter(obs._pending.values()))["opportunity_key"]
    await obs.flush_pending()
    assert (await rejected(key_other)).coverage == "UNAVAILABLE"
    async with db.get_session() as session:
        await session.execute(update(R).where(R.opportunity_key == key_other).values(decision_at=past))
        await session.commit()

    # ── Capacidade pequena (patch): mesma transação, checagem por lote ──
    used = {name: await scalar(select(func.count()).select_from(model))
            for name, model in (("opportunities", O), ("attempts", A), ("rejected", R))}
    small = {"opportunities": used["opportunities"] + 1, "attempts": used["attempts"] + 2,
             "rejected": used["rejected"]}
    obs._stats.clear()
    with patch.object(obs, "CAPACITY", small):
        fresh = [{**deepcopy(rec), "_snapshot_id": 8000 + i} for i in range(3)]
        obs.begin_batch(fresh, mode="LIVE")
        for item in fresh:
            obs.stage_decision(item, "REJECTED", "score-min")
        obs.seal_batch(fresh)
        obs._wanted_symbols = None
        await obs.observe_candles(other["symbol"], stop_path, as_of=now)
        await obs.flush_pending()
        assert obs._stats["capacity_dropped_opportunities"] == 2, dict(obs._stats)
        assert obs._stats["capacity_dropped_attempts"] == 2, dict(obs._stats)
        # 1 por falta de vaga + 2 cujas oportunidades não foram admitidas.
        assert obs._stats["capacity_dropped_rejected"] == 3, dict(obs._stats)
        assert (await scalar(select(func.count()).select_from(O))) == small["opportunities"]
        assert (await scalar(select(func.count()).select_from(R))) == small["rejected"]
        # O antigo foi resolvido apesar da admissão lotada.
        assert (await rejected(key_other)).coverage == "RESOLVED"
        # Reavaliação usa a vaga restante de tentativa; a seguinte é descartada.
        for expected_attempts in (small["attempts"], small["attempts"]):
            again = deepcopy(fresh[0])
            obs.begin_batch([again], mode="LIVE")
            obs.stage_decision(again, "REJECTED", "score-min")
            obs.seal_batch([again])
            await obs.flush_pending()
            assert (await scalar(select(func.count()).select_from(A))) == expected_attempts
        assert obs._stats["capacity_dropped_attempts"] == 3, dict(obs._stats)
        # Toda oportunidade tem ao menos uma tentativa (nenhuma órfã).
        async with db.get_session() as session:
            orphans = await session.scalar(select(func.count()).select_from(O).where(
                ~select(A.attempt_id).where(A.opportunity_key == O.opportunity_key).exists()))
        assert orphans == 0, orphans
        report = await obs.get_status(7)
        assert report["capacity"]["state"] == "AT_LIMIT", report["capacity"]
        assert report["capacity"]["admission_blocked"] is True
        assert report["telemetry"]["capacity_dropped_opportunities"] == 2

    # ── Contenção: lock R09 ocupado por outra conexão; risco não é afetado ──
    obs._stats.clear()
    attempts_before = await scalar(select(func.count()).select_from(A))
    async with db._engine.connect() as holder:
        await holder.begin()
        await holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": obs.R09_ADVISORY_LOCK_KEY})
        async with db._engine.connect() as risk:
            await risk.begin()
            assert (await risk.execute(text("SELECT pg_try_advisory_xact_lock(917283)"))).scalar() is True
            await risk.rollback()
        waiting = {**deepcopy(rec), "_snapshot_id": 6060}
        obs.begin_batch([waiting], mode="LIVE")
        obs.stage_decision(waiting, "INELIGIBLE", "TIER_NOT_EXECUTABLE")
        obs.seal_batch([waiting])
        await obs.flush_pending()
        assert obs._stats["capacity_lock_contention"] == 1, dict(obs._stats)
        assert obs._stats["contention_requeued"] == 1, dict(obs._stats)
        assert len(obs._pending) == 1
        assert (await scalar(select(func.count()).select_from(A))) == attempts_before
        await holder.rollback()
    await obs.flush_pending()
    assert len(obs._pending) == 0
    assert (await scalar(select(func.count()).select_from(A))) == attempts_before + 1
    assert obs._stats["persistence_dropped"] == 0, dict(obs._stats)

    # GET: coleta/cobertura, nunca métricas econômicas.
    report = await obs.get_status(7)
    serialized = json.dumps(report, default=str, allow_nan=False)
    for economic in ("gross_r", "net_r", "pnl", "expectancy", "win_rate"):
        assert economic not in serialized, economic
    # Nenhuma poluição de QUALQUER tabela operacional.
    assert await operational_counts() == operational_before, "tabela operacional alterada"
    await db._engine.dispose()
    print("R09_PG_INTEGRATION_OK: schema2x, sealed-batch, dedupe, frozen-setup, concurrency, "
          "out-of-order, replay-resolved, terminal-once, invalid-terminal, capacity, "
          "resolve-at-capacity, contention, lock-isolation, no-economics, isolation")


if __name__ == "__main__":
    asyncio.run(run())
