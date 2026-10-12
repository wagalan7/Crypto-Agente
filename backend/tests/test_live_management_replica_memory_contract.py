"""Pure memory/import contract tests: no LIVE manager, environment, DB or network."""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import marshal
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Integer, String, bindparam, or_, select, text, true, update
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column

WORKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_management_replica_worker.py"
SPEC = importlib.util.spec_from_file_location("_memory_contract_worker", WORKER_PATH)
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class Base(DeclarativeBase):
    pass


class RealTrade(Base):
    __tablename__ = "fixture_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recommendation_id: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[str | None] = mapped_column(String)


class RecommendationSnapshot(Base):
    __tablename__ = "fixture_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timeframe: Mapped[str] = mapped_column(String)


class OtherSnapshot(Base):
    __tablename__ = "other_fixture_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timeframe: Mapped[str] = mapped_column(String)


class MemorySelectContractTests(unittest.TestCase):
    def classify(self, statement, recommendation_id=31):
        return WORKER.memory_select_kind(
            statement, RealTrade, RecommendationSnapshot, 17, recommendation_id,
        )

    def test_only_two_canonical_queries_are_admitted(self):
        self.assertEqual(self.classify(
            select(RealTrade).where(RealTrade.id == 17),
        ), "trade")
        self.assertEqual(self.classify(
            select(RecommendationSnapshot.timeframe).where(RecommendationSnapshot.id == 31),
        ), "timeframe")

    def test_wrong_rows_entities_projections_and_shapes_are_rejected(self):
        trade = select(RealTrade).where(RealTrade.id == 17)
        timeframe = select(RecommendationSnapshot.timeframe).where(RecommendationSnapshot.id == 31)
        trade_alias = aliased(RealTrade)
        rejected = [
            select(RealTrade),
            select(RealTrade).where(RealTrade.id == 18),
            select(RealTrade).where(RealTrade.id != 17),
            select(RealTrade).where(RealTrade.id.in_([17])),
            select(RealTrade).where(or_(RealTrade.id == 17, RealTrade.id == 18)),
            trade.where(RealTrade.phase == "pre_tp1"),
            trade.where(true()),
            trade.join(RecommendationSnapshot, RecommendationSnapshot.id == RealTrade.recommendation_id),
            trade.select_from(RealTrade, RecommendationSnapshot),
            trade.limit(1), trade.offset(0), trade.order_by(RealTrade.id),
            trade.group_by(RealTrade.id), trade.distinct(), trade.with_for_update(),
            trade.execution_options(populate_existing=True),
            trade.union_all(trade), update(RealTrade).where(RealTrade.id == 17),
            text("SELECT * FROM fixture_trades WHERE id = 17"),
            select(RealTrade.id).where(RealTrade.id == 17),
            select(*RealTrade.__table__.columns).where(RealTrade.id == 17),
            select(trade_alias).where(trade_alias.id == 17),
            select(RealTrade, RecommendationSnapshot.timeframe).where(RealTrade.id == 17),
            select(RecommendationSnapshot).where(RecommendationSnapshot.id == 31),
            select(RecommendationSnapshot.timeframe.label("timeframe")).where(RecommendationSnapshot.id == 31),
            select(OtherSnapshot.timeframe).where(OtherSnapshot.id == 31),
            select(RecommendationSnapshot.timeframe).where(RecommendationSnapshot.id == 32),
            timeframe.where(RecommendationSnapshot.timeframe == "15m"),
            timeframe.join(RealTrade, RealTrade.recommendation_id == RecommendationSnapshot.id),
            select(RealTrade).where(RealTrade.id == bindparam("wanted", callable_=lambda: 17)),
            trade.params({trade.whereclause.right.key: 17.0}),
            trade.params({trade.whereclause.right.key: True}),
        ]
        for index, statement in enumerate(rejected):
            with self.subTest(index=index):
                with self.assertRaises(WORKER.TraceGap):
                    self.classify(statement)
        with self.assertRaises(WORKER.TraceGap):
            self.classify(timeframe, recommendation_id=None)

    def test_refused_import_records_gap_even_when_caller_swallows_it(self):
        runtime = WORKER.OfflineRuntime({"initial_state": {"id": 17}})
        guard = WORKER.OfflineImportGuard(runtime, Path("/unused-offline-fixture"))
        for module in ("services.unadmitted_service", "models.unadmitted_model",
                       "httpx", "requests.api", "asyncpg", "aiohttp"):
            try:
                guard.find_spec(module)
            except ImportError:
                pass
        self.assertEqual(len(runtime.gaps), 6)
        self.assertTrue(all(gap["reason"] == "OFFLINE_IMPORT_BLOCKED" for gap in runtime.gaps))
        spec = guard.find_spec("services.trade_manager_service")
        self.assertIsInstance(spec.loader, WORKER.SourceOnlyLoader)


class SourceOnlyLoaderTests(unittest.TestCase):
    def test_allowed_specs_use_fixed_backend_source_paths(self):
        runtime = WORKER.OfflineRuntime({"initial_state": {"id": 17}})
        backend = Path("/unused-offline-fixture")
        guard = WORKER.OfflineImportGuard(runtime, backend)
        for module in guard.ALLOWED_SERVICES | guard.ALLOWED_MODELS:
            with self.subTest(module=module):
                spec = guard.find_spec(module, path=["/untrusted-search-path"])
                expected = backend.joinpath(*module.split(".")).with_suffix(".py")
                self.assertIsInstance(spec.loader, WORKER.SourceOnlyLoader)
                self.assertEqual(spec.origin, str(expected))
                self.assertEqual(spec.loader.path, str(expected))
        self.assertFalse(runtime.gaps)

    def test_equal_mtime_and_size_cache_old_source_new_loads_source_only(self):
        name = "_in_memory_source_fixture"
        source_path = "/unused-offline-fixture/manager.py"
        cache_path = importlib.util.cache_from_source(source_path)
        old_source = b"VALUE = 'OLD'\n"
        new_source = b"VALUE = 'NEW'\n"
        self.assertEqual(len(old_source), len(new_source))
        mtime = 1_700_000_000
        cached = (importlib.util.MAGIC_NUMBER
                  + struct.pack("<III", 0, mtime, len(new_source))
                  + marshal.dumps(compile(old_source, source_path, "exec")))
        files = {source_path: new_source, cache_path: cached}

        class InMemoryFiles:
            def __init__(self, fullname, path):
                super().__init__(fullname, path)
                self.reads = []

            def path_stats(self, path):
                return {"mtime": mtime, "size": len(new_source)}

            def get_data(self, path):
                self.reads.append(path)
                return files[path]

            def set_data(self, path, data, **kwargs):
                raise AssertionError("bytecode-cache writes are not admitted")

        class OrdinaryLoader(InMemoryFiles, importlib.machinery.SourceFileLoader):
            pass

        class SourceLoader(InMemoryFiles, WORKER.SourceOnlyLoader):
            pass

        ordinary = OrdinaryLoader(name, source_path)
        ordinary_namespace = {}
        exec(ordinary.get_code(name), ordinary_namespace)
        self.assertEqual(ordinary_namespace["VALUE"], "OLD")
        self.assertEqual(ordinary.reads, [cache_path])
        source_only = SourceLoader(name, source_path)
        source_namespace = {}
        exec(source_only.get_code(name), source_namespace)
        self.assertEqual(source_namespace["VALUE"], "NEW")
        self.assertEqual(source_only.reads, [source_path])
        self.assertEqual(files[cache_path], cached)


class MemorySessionAuditTests(unittest.TestCase):
    def setUp(self):
        self.modules = patch.dict(sys.modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.meta_path = patch.object(sys, "meta_path", list(sys.meta_path))
        self.meta_path.start()
        self.addCleanup(self.meta_path.stop)
        self.runtime = WORKER.OfflineRuntime({"initial_state": {"id": 17, "timeframe": "15m"}})
        self.runtime.trade_model = RealTrade
        self.runtime.persisted = RealTrade(id=17, recommendation_id=31, phase="pre_tp1")
        WORKER.install_memory_dependencies(self.runtime, Path("/unused-offline-fixture"))
        WORKER.fake_module("models.recommendation_snapshot",
                           RecommendationSnapshot=RecommendationSnapshot)
        self.session_factory = sys.modules["db"].get_session

    def test_successful_commit_is_audited_and_persists_only_memory(self):
        async def exercise():
            async with self.session_factory() as session:
                fresh = (await session.execute(
                    select(RealTrade).where(RealTrade.id == 17),
                )).scalar_one_or_none()
                fresh.phase = "post_tp1"
                await session.commit()
        asyncio.run(exercise())
        self.assertEqual(self.runtime.persisted.phase, "post_tp1")
        self.assertTrue(self.runtime.sessions[-1]["committed"])

    def test_read_only_exit_is_not_committed(self):
        async def exercise():
            async with self.session_factory() as session:
                row = (await session.execute(
                    select(RecommendationSnapshot.timeframe).where(RecommendationSnapshot.id == 31),
                )).first()
                self.assertEqual(row, ("15m",))
        asyncio.run(exercise())
        self.assertFalse(self.runtime.sessions[-1]["committed"])

    def test_invalid_query_records_gap_even_when_swallowed(self):
        async def exercise():
            async with self.session_factory() as session:
                try:
                    await session.execute(select(RealTrade).where(RealTrade.id == 18))
                except WORKER.TraceGap:
                    pass
        asyncio.run(exercise())
        self.assertEqual(self.runtime.gaps[-1]["reason"], "UNSUPPORTED_MEMORY_QUERY")
        self.assertFalse(self.runtime.sessions[-1]["committed"])

    def test_failed_commit_is_not_reported_as_committed(self):
        async def exercise():
            with self.assertRaises(RuntimeError):
                async with self.session_factory() as session:
                    await session.execute(select(RealTrade).where(RealTrade.id == 17))
                    with patch.object(self.runtime, "clone", side_effect=RuntimeError("fixture failure")):
                        await session.commit()
        asyncio.run(exercise())
        self.assertFalse(self.runtime.sessions[-1]["committed"])
        self.assertFalse(any(event["port"] == "session_commit" for event in self.runtime.sessions))

    def test_error_after_successful_commit_keeps_true_audit(self):
        async def exercise():
            with self.assertRaises(RuntimeError):
                async with self.session_factory() as session:
                    await session.execute(select(RealTrade).where(RealTrade.id == 17))
                    await session.commit()
                    raise RuntimeError("after commit")
        asyncio.run(exercise())
        self.assertTrue(self.runtime.sessions[-1]["committed"])
        self.assertEqual(self.runtime.sessions[-1]["exception"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
