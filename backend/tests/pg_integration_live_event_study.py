"""R13 event ingestion/catalogue: REAL PG16/asyncpg, disposable Unix only.

This is not an in-memory substitute. Two distinct backend connections read
generation zero before either publication, using a deterministic barrier.
Only existing official models/tables are created. No production environment,
credentials, external DB, collector, exchange or schema migration is used.
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import socket
import subprocess
import sys
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

TEST_SOCKET = os.environ.get("CW_LIVE_EVENT_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-live-event-sock\.[A-Za-z0-9]+", TEST_SOCKET) or not Path(TEST_SOCKET).is_dir():
    raise SystemExit("LIVE_EVENT_PG_BLOCKED: socket descartável do runner obrigatório")
if any(key in os.environ for key in ("DATABASE_URL", "BINANCE_API_KEY", "BINANCE_API_SECRET")):
    raise SystemExit("LIVE_EVENT_PG_BLOCKED: ambiente real não deve ser herdado")
DB_URL = "postgresql+asyncpg://event_research@/event_research_db?host=" + TEST_SOCKET
os.environ["DATABASE_URL"] = DB_URL  # Only the exact disposable socket above.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OriginalSocket = socket.socket
NETWORK = {"tcp_udp": 0, "dns": 0, "foreign_unix": 0}
CHECKS: list[str] = []


class UnixOnlySocket(OriginalSocket):
    def _allowed(self, address):
        if self.family != socket.AF_UNIX:
            NETWORK["tcp_udp"] += 1
            raise AssertionError("TCP/UDP prohibited in disposable event research")
        name = address.decode() if isinstance(address, bytes) else address
        if name != TEST_SOCKET + "/.s.PGSQL.5432":
            NETWORK["foreign_unix"] += 1
            raise AssertionError("Only THIS disposable PG socket is permitted")

    def connect(self, address):
        self._allowed(address)
        return super().connect(address)

    def connect_ex(self, address):
        self._allowed(address)
        return super().connect_ex(address)

    def sendto(self, data, *args):
        if self.family != socket.AF_UNIX:
            NETWORK["tcp_udp"] += 1
            raise AssertionError("UDP prohibited")
        return super().sendto(data, *args)


def no_dns(*args, **kwargs):
    NETWORK["dns"] += 1
    raise AssertionError("DNS prohibited in disposable event research")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    CHECKS.append(name)
    print("PASS", name, flush=True)


async def run():
    import db
    from sqlalchemy import event, func, select, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from models.policy_simulation_state import PolicySimulationState as State
    from models.strategy_experiment import StrategyExperiment as Experiment
    from services import live_event_study_service as study
    from services import policy_state_service as policy_state
    from services import research_acceptance_service as acceptance
    from services import research_study_service as official_study
    from services import strategy_evidence_service as catalog
    # Fresh process/source identity each run, not a persisted/previous fixture.
    from tests.test_live_event_study import report_fixture

    engines = []
    async def new_engine():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        engines.append(engine)
        return engine

    try:
        engine = await new_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all,
                                      tables=[State.__table__, Experiment.__table__])
            actual = (await connection.execute(text("SHOW listen_addresses"))).scalar_one()
            encoding = (await connection.execute(text("SHOW server_encoding"))).scalar_one()
        check("discardable_pg_unix_utf8", actual == "" and encoding == "UTF8", (actual, encoding))

        async def schema_signature():
            async with sessions() as session:
                return list((await session.execute(text(
                    "SELECT table_name,column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema='public' ORDER BY table_name,ordinal_position"))).all())

        schema_before = await schema_signature()
        check("only_existing_official_tables", {r[0] for r in schema_before} == {
            State.__tablename__, Experiment.__tablename__})
        report = report_fixture()
        check("official_real_callers_economic_report", report.get("ok") is True, report)
        check("test_only_without_operational_authority", report["evidence_inputs"]["test_only"] is True
              and all(report.get(key) is False for key in ("promotable", "authorizes_live", "real_study_allowed")))
        check("full_recomputation_before_ingestion", study.verify_report(report, recompute=True)["ok"])
        ref = study.study_reference(report)
        check("separate_event_namespace", ref["experiment_key"].startswith("r13evt:")
              and not ref["experiment_key"].startswith("r13cal:"))

        forged = copy.deepcopy(report)
        forged["arms"]["candidate"]["economics"][0]["net_r"] = 99.0
        forged["report_hash"] = study.digest({k: v for k, v in forged.items() if k != "report_hash"})
        refused = await study.persist_study(sessions, forged)
        check("resealed_money_rejected_before_write", not refused.get("published"), refused)
        async with sessions() as session:
            count = (await session.execute(select(func.count()).select_from(State))).scalar_one()
        check("forgery_has_zero_persisted_rows", count == 0, count)

        engine_a, engine_b = await new_engine(), await new_engine()
        async with engine_a.connect() as connection_a, engine_b.connect() as connection_b:
            pid_a = (await connection_a.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            pid_b = (await connection_b.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await connection_a.rollback()
            await connection_b.rollback()
            check("two_distinct_live_pg_connections", pid_a != pid_b, (pid_a, pid_b))

            @asynccontextmanager
            async def worker_a():
                async with AsyncSession(bind=connection_a, expire_on_commit=False) as session:
                    yield session

            @asynccontextmanager
            async def worker_b():
                async with AsyncSession(bind=connection_b, expire_on_commit=False) as session:
                    yield session

            barrier = asyncio.Event()
            arrivals, raw_cas = [], []
            real_read, real_publish = policy_state.read_state, policy_state.publish_generation

            async def barrier_read(factory, **identity):
                result = await real_read(factory, **identity)
                if identity == ref and result["available"] and result["found"] is False:
                    arrivals.append(factory)
                    if len(arrivals) == 2:
                        barrier.set()
                    await asyncio.wait_for(barrier.wait(), timeout=30)
                return result

            async def recorded_publish(factory, **kwargs):
                result = await real_publish(factory, **kwargs)
                raw_cas.append({"expected_generation": kwargs.get("expected_generation"), "result": result})
                return result

            with patch.object(policy_state, "read_state", side_effect=barrier_read), \
                 patch.object(policy_state, "publish_generation", side_effect=recorded_publish):
                results = await asyncio.wait_for(asyncio.gather(
                    study.persist_study(worker_a, report),
                    study.persist_study(worker_b, copy.deepcopy(report))), timeout=60)
            check("deterministic_barrier_both_read_generation_zero", len(arrivals) == 2 and barrier.is_set())
            check("cas_both_use_exact_generation_zero", len(raw_cas) == 2
                  and all(item["expected_generation"] == 0 for item in raw_cas), raw_cas)
            check("real_cas_stale_loser_not_last_writer_wins", any(item["result"].get("reason_code") == "GENERATION_STALE"
                  for item in raw_cas), raw_cas)
            check("two_workers_exactly_one_publication", sum(r.get("published") is True for r in results) == 1, results)
            check("loser_returns_immutable_unchanged", any(r.get("reason_code") == "EVENT_STUDY_UNCHANGED" for r in results), results)

        async with sessions() as session:
            persisted = list((await session.execute(select(State))).scalars())
            check("one_row_one_generation", len(persisted) == 1 and persisted[0].generation == 1)
            check("jsonb_roundtrip_report_exact", persisted[0].payload == {"event_report": report})
            check("committed_hash_matches_body", persisted[0].evidence_key == report["report_hash"])
            original_id = persisted[0].id

        # Different VALID recomputed observation/report, same frozen input
        # identity. Not an externally supplied metric or patched verifier.
        changed = study.run_study(manifest=report["manifest"], dataset=report["dataset"],
                                  now_ms=report["evidence_inputs"]["now_ms"] + 1)
        check("conflicting_report_itself_fully_valid", study.verify_report(changed, recompute=True)["ok"])
        check("conflict_preserves_same_identity", study.study_reference(changed) == ref
              and changed["report_hash"] != report["report_hash"])
        conflict = await study.persist_study(sessions, changed)
        check("identity_conflict_refused_no_overwrite", not conflict.get("published")
              and conflict.get("reason_code") == "EVENT_STUDY_IDENTITY_CONFLICT", conflict)

        await engine.dispose()
        engine = await new_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        policy_state._STATE_CACHE.clear()  # Restart has no process cache authority.
        repeated = await official_study.persist_study(sessions, report)
        check("official_dispatch_restart_idempotent", not repeated.get("published")
              and repeated.get("generation") == 1 and repeated.get("reason_code") == "EVENT_STUDY_UNCHANGED", repeated)

        config = study.build_event_envelope(report)
        cutoff = datetime.fromtimestamp(report["manifest"]["split"]["as_of_ms"] / 1000, tz=timezone.utc)
        catalog_args = {"champion": report["manifest"]["baseline"], "config": config,
                        "objective": catalog.OBJECTIVES[0], "study_ref": ref, "cutoff": cutoff,
                        "fingerprint": report["identity"]["dataset_hash"]}
        created = await catalog.create_preselection_experiment(**catalog_args)
        check("official_catalog_dispatch_creates_only_draft", created.get("ok") and created.get("status") == "DRAFT"
              and created.get("promotable") is False, created)
        recreated = await catalog.create_preselection_experiment(**catalog_args)
        check("catalog_retry_same_existing_id", recreated.get("experiment_id") == created["experiment_id"], recreated)
        await db._engine.dispose()
        restarted = await catalog.create_preselection_experiment(**catalog_args)
        check("catalog_restart_same_existing_id", restarted.get("experiment_id") == created["experiment_id"], restarted)
        async with db.get_session() as session:
            entries = list((await session.execute(select(Experiment))).scalars())
            check("one_draft_no_shadow_no_acceptance", len(entries) == 1 and entries[0].status == "DRAFT"
                  and entries[0].shadow_started_at is None and entries[0].offline_metrics["operational_authority"] == "NONE")
        wrong_args = {**catalog_args, "fingerprint": "0" * 64}
        refused_catalog = await catalog.create_preselection_experiment(**wrong_args)
        check("catalog_identity_mismatch_refused", refused_catalog.get("reason_code") == "EVENT_CATALOG_IDENTITY_MISMATCH",
              refused_catalog)
        legacy_read = await official_study.load_latest_study(sessions)
        check("event_report_not_a_legacy_calibration", legacy_read.get("available") is True
              and legacy_read.get("reason_code") == "NO_CALIBRATION_STUDY"
              and legacy_read.get("artifact") is None, legacy_read)
        check("synthetic_never_promotion_or_canary", all(not acceptance.verify_acceptance(report,
              now_ms=report["evidence_inputs"]["now_ms"], purpose=purpose)["ok"] for purpose in ("PROMOTION", "CANARY")))

        # Read paths: forbid engines, fitting and every non-SELECT SQL statement;
        # count ORM commits/flushes as well, not merely equivalent final rows.
        read_sql, write_attempts, commits, flushes = [], [], [], []
        def read_only_sql(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                read_sql.append(statement)
                return
            write_attempts.append(statement)
            raise AssertionError("Non-SELECT in event read endpoint")
        def committed(session):
            commits.append(True)
        def flushed(session, context):
            flushes.append(True)
        guarded_engines = (engine, db._engine)
        for item in guarded_engines:
            event.listen(item.sync_engine, "before_cursor_execute", read_only_sql)
        event.listen(AsyncSession.sync_session_class, "after_commit", committed)
        event.listen(AsyncSession.sync_session_class, "after_flush", flushed)
        try:
            with ExitStack() as guards:
                for module, name in ((study, "run_study"), (study.execution, "replay_execution"),
                                     (study.manager, "run_trace"), (study.ledger, "replay_accounting"),
                                     (study.evidence, "evaluate_evidence"), (subprocess, "run")):
                    guards.enter_context(patch.object(module, name, side_effect=AssertionError("engine/fitting on GET")))
                loaded = await study.load_study(sessions, ref)
                status = await study.load_latest_status(sessions)
                experiment = await catalog.get_experiment(created["experiment_id"])
            check("restart_read_verified_report_exact", loaded.get("available") and loaded.get("report") == report, loaded)
            check("status_read_is_observational", status.get("available") and status.get("promotable") is False
                  and status.get("authorizes_live") is False and status.get("holdout_status") == "SEALED", status)
            check("catalog_read_draft_without_replay", experiment is not None and experiment["status"] == "DRAFT", experiment)
            check("get_zero_writes_commits_flushes", not write_attempts and not commits and not flushes
                  and len(read_sql) >= 3, (write_attempts, commits, flushes, len(read_sql)))
        finally:
            for item in guarded_engines:
                event.remove(item.sync_engine, "before_cursor_execute", read_only_sql)
            event.remove(AsyncSession.sync_session_class, "after_commit", committed)
            event.remove(AsyncSession.sync_session_class, "after_flush", flushed)

        # Material tamper in a PG row is not granted validity by re-sealing a
        # public hash. The frozen persisted evidence key must bind its report.
        async with sessions() as session:
            row = (await session.execute(select(State).where(State.id == original_id))).scalar_one()
            row.payload = {"event_report": forged}
            await session.commit()
        tampered = await study.load_study(sessions, ref)
        tampered_status = await study.load_latest_status(sessions)
        check("db_resealed_money_tamper_read_refused", tampered.get("available") is False, tampered)
        check("db_resealed_money_tamper_status_refused", tampered_status.get("available") is False, tampered_status)
        async with sessions() as session:
            row = (await session.execute(select(State).where(State.id == original_id))).scalar_one()
            row.payload = {"event_report": report}
            await session.commit()
        check("restored_valid_row_reads_same_report", (await study.load_study(sessions, ref)).get("report") == report)
        async with sessions() as session:
            row = (await session.execute(select(State).where(State.id == original_id))).scalar_one()
            check("generation_and_original_hash_never_overwritten", row.generation == 1
                  and row.evidence_key == report["report_hash"])
        check("no_new_tables_or_columns", await schema_signature() == schema_before)
        check("zero_tcp_udp_dns_foreign_unix", NETWORK == {"tcp_udp": 0, "dns": 0, "foreign_unix": 0}, NETWORK)
        print("LIVE_EVENT_STUDY_PG_OK", len(CHECKS), flush=True)
    finally:
        for engine in engines:
            await engine.dispose()
        if db._engine is not None:
            await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
