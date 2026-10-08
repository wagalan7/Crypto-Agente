"""Real-driver receipt persistence/restart/CAS, disposable Unix PG only."""
import asyncio
import copy
import os
from pathlib import Path
import re
import socket
import sys
from unittest.mock import patch

test_socket = os.environ.get("ACCEPTANCE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-acceptance-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Disposable test socket required")
DB_URL = "postgresql+asyncpg://acceptance@/acceptance_db?host=" + test_socket
os.environ["DATABASE_URL"] = DB_URL
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OriginalSocket = socket.socket
NETWORK = {"tcp": 0, "dns": 0}


class UnixOnly(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            NETWORK["tcp"] += 1
            raise AssertionError("TCP prohibited")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            NETWORK["tcp"] += 1
            raise AssertionError("TCP prohibited")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    NETWORK["dns"] += 1
    raise AssertionError("DNS prohibited")


socket.socket, socket.getaddrinfo = UnixOnly, no_dns
CHECKS = []


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    CHECKS.append(name)
    print("PASS", name)


async def run():
    import db
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from models.policy_simulation_state import PolicySimulationState as State
    from services import research_acceptance_service as acceptance
    from services import research_study_service as study
    from services import score_v3_calibration_service as calibration
    from services import portfolio_replay_service as portfolio
    from services import offline_replay_service as replay
    from services import walk_forward_service as walk_forward
    from tests.research_acceptance_fixture import accepted_calibration_report
    engine = create_async_engine(DB_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all, tables=[State.__table__])
    report = accepted_calibration_report()
    now = report["observed_at_ms"]
    check("official_export_replay_fitting_calibration_accepted",
        report["ok"] is True and report["acceptance"]["calibration"]["state"] == "ACCEPTED")
    check("four_calibration_six_economic_folds_executed",
        len(report["calibration"]["folds"]) == 4 and report["walk_forward"]["folds_executed"] == 6)
    check("economic_ci_uses_original_five_fold_blocks",
        report["walk_forward"]["ci"].get("available") is True
        and report["walk_forward"]["ci"].get("block_size") == 5)
    check("no_runtime_evidence_no_economic_approval",
        report["acceptance"]["economics"]["state"] != "ACCEPTED")
    check("test_data_never_real_authority",
        not acceptance.verify_acceptance(report, now_ms=now, purpose="CANARY")["ok"])
    check("full_recomputation_verifies_real_report",
        acceptance.verify_acceptance(report, now_ms=now, purpose="STRUCTURAL", recompute=True)["ok"])

    forged = copy.deepcopy(report)
    forged["acceptance"]["calibration"]["metrics"]["brier_gain_ci"]["low"] += .001
    forged["acceptance"]["record_hash"] = acceptance.digest({
        k: v for k, v in forged["acceptance"].items() if k != "record_hash"})
    refused = await study.persist_study(sessions, forged)
    check("resealed_derived_ci_refused_before_db", not refused["published"], refused)
    async with sessions() as session:
        check("forgery_did_not_create_row", (await session.execute(
            select(func.count()).select_from(State))).scalar_one() == 0)

    # Both calls use independent AsyncSessions and the real advisory/CAS path.
    results = await asyncio.gather(study.persist_study(sessions, report),
                                   study.persist_study(sessions, copy.deepcopy(report)))
    check("two_connections_one_publication", sum(r.get("published") is True for r in results) == 1, results)
    check("concurrent_repeat_is_exact_identity", any(
        r.get("reason_code") == "STUDY_UNCHANGED" for r in results), results)
    async with sessions() as session:
        rows = list((await session.execute(select(State))).scalars())
        check("one_row_one_generation", len(rows) == 1 and rows[0].generation == 1)
        check("json_roundtrip_preserves_receipt_hash", rows[0].payload["acceptance"]["record_hash"] == report["acceptance"]["record_hash"])
    await engine.dispose()
    engine = create_async_engine(DB_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with patch.object(acceptance, "build_acceptance", side_effect=AssertionError("GET evaluation")), \
         patch.object(acceptance, "_bootstrap", side_effect=AssertionError("GET calibration bootstrap")), \
         patch.object(walk_forward, "block_bootstrap_ci", side_effect=AssertionError("GET economic bootstrap")), \
         patch.object(calibration, "fit_calibration", side_effect=AssertionError("GET fitting")), \
         patch.object(portfolio, "run_portfolio", side_effect=AssertionError("GET portfolio replay")), \
         patch.object(replay, "replay_opportunity", side_effect=AssertionError("GET setup replay")):
        loaded = await study.load_latest_study(sessions, now_ms=now)
        check("restart_read_without_fit_or_bootstrap", loaded["available"], loaded)
        check("restart_same_calibration_receipt", loaded["acceptance"]["calibration"]["state"] == "ACCEPTED")
    repeated = await study.persist_study(sessions, report)
    check("restart_repeat_no_generation_increment", not repeated["published"] and repeated.get("generation") == 1, repeated)
    check("expiry_fails_closed", not acceptance.verify_acceptance(report,
        now_ms=report["acceptance"]["valid_until_ms"], purpose="STRUCTURAL")["ok"])
    revoked = copy.deepcopy(report)
    revoked["acceptance"]["revoked"] = True
    revoked["acceptance"]["record_hash"] = acceptance.digest({
        k: v for k, v in revoked["acceptance"].items() if k != "record_hash"})
    check("revocation_fails_closed", not acceptance.verify_acceptance(revoked, now_ms=now, purpose="CANARY")["ok"])
    check("zero_tcp_dns", NETWORK == {"tcp": 0, "dns": 0}, NETWORK)
    await engine.dispose()
    print("RESEARCH_ACCEPTANCE_PG_OK", len(CHECKS))


if __name__ == "__main__":
    asyncio.run(run())
