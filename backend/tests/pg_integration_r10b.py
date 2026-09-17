"""R10B: consultas REAIS do exportador em PostgreSQL descartável.

R10B_TEST_SOCKET deve apontar para /tmp/cw-r10b-sock.* criado para o teste.
Schema e fixtures são criados SOMENTE por este harness. Sem TCP/DNS, sem
DATABASE_URL externo, sem exchange. Prova: READ ONLY, projeção JSONB no
servidor, purga/empates, holdout só contado e integração com o R10A.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

test_socket = os.environ.get("R10B_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r10b-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r10b@/r10bdb?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R10B")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R10B")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R10B")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

BAR = 300_000
T0 = 1_760_000_100_000
VAL = T0 + 100 * BAR
HOLD = T0 + 200 * BAR
HORIZON = 26
REPLAY = {"bar_ms": BAR, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
          "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
          "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96}
SENTINELS = ("5555.5", "7777.5", "8888.5", "999.5", "6666.5")


def request(as_of="2026-01-01T00:00:00Z"):
    return {
        "as_of_utc": as_of,
        "split": {"train_start_ms": T0, "validation_start_ms": VAL,
                  "holdout_start_ms": HOLD, "purge_bars": 1},
        "baseline_config": dict(REPLAY),
        "candidate": {"candidate_id": "R10B-PG-AA", "registered_at_ms": T0 - 1,
                      "kind": "MANAGEMENT_ONLY", "replay_config": dict(REPLAY)},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0, "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 3, "samples": 100, "block_size": 1},
    }


def first_bar(ms):
    return ((ms + BAR - 1) // BAR) * BAR


def bars(first, n, price=100.0, start=0):
    out = []
    for i in range(start, start + n):
        if i == 1 and price == 100.0:
            out.append({"timestamp": first + i * BAR, "open": 99.0, "high": 99.5, "low": 94.0,
                        "close": 95.0, "volume": 10.0})
        else:
            out.append({"timestamp": first + i * BAR, "open": price, "high": price + 1,
                        "low": price - 1, "close": price, "volume": 10.0})
    return out


def config(**over):
    return {"schema_version": "r09.v1", "policy": "ISOLATED_REPLAY_NOT_LIVE",
            "scope": "POST_SELECTION", **REPLAY, "cost_status": "UNKNOWN",
            "learning_eligible": False, "version_hash": "h1", **over}


def setup(**over):
    return {"symbol": "SYN/USDT:USDT", "timeframe": "15m", "direction": "long", "tier": "A",
            "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0, "atr": 2.0,
            "score": 70.0, **over}


class Spy:
    """Sessão real; registra as LINHAS devolvidas pelo PostgreSQL ao Python."""

    def __init__(self, session, inject_write=False, check_xid=False):
        self.session, self.inject_write, self.check_xid = session, inject_write, check_xid
        self.sql, self.fetched, self.write_error, self.xid = [], [], None, "unset"

    def in_transaction(self):
        return self.session.in_transaction()

    async def rollback(self):
        from sqlalchemy import text
        if self.check_xid:
            self.xid = (await self.session.execute(text("SELECT txid_current_if_assigned()"))).scalar()
        await self.session.rollback()

    async def execute(self, statement, params=None):
        from sqlalchemy import text
        sql = " ".join(str(statement).split())
        self.sql.append(sql)
        result = await self.session.execute(statement, params or {})
        if self.inject_write and "current_setting" in sql:
            try:
                await self.session.execute(text(
                    "UPDATE rejected_setup_observations SET version = version + 1"))
            except Exception as exc:
                self.write_error = str(exc)
        if sql.startswith("WITH") or sql.startswith("SELECT opportunity_key"):
            rows = [dict(row) for row in result.mappings().all()]
            self.fetched.extend(rows)
            return _Rows(rows)
        return result


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


async def run():
    from sqlalchemy import select, text, update
    import db
    from models.decision_observation import (DecisionObservation as O,
                                             DecisionObservationAttempt as A,
                                             RejectedSetupObservation as R)
    from services import offline_replay_service as r10a
    from services import research_dataset_service as ds
    import research_dataset as cli

    async with db._engine.begin() as conn:   # schema criado só pelo harness
        await conn.run_sync(db.Base.metadata.create_all, tables=[O.__table__, A.__table__, R.__table__])

    now = ds.ms_datetime(T0 + 500 * BAR)

    def rejected(key, decision_ms, candles, **over):
        values = dict(opportunity_key=key, symbol="SYN/USDT:USDT", decision_at=ds.ms_datetime(decision_ms),
                      first_blocker="score-min", frozen_setup=setup(), frozen_config=config(),
                      coverage="RESOLVED", candles=candles,
                      outcome={"status": "CLOSED_TP2", "gross_r": 6666.5}, version=1, updated_at=now)
        values.update(over)
        return R(**values)

    def opportunity(key, first_ms, **over):
        values = dict(opportunity_key=key, identity_source="SETUP_CANDLE", scope="POST_SELECTION",
                      symbol="SYN/USDT:USDT", first_seen_at=ds.ms_datetime(first_ms),
                      last_seen_at=ds.ms_datetime(first_ms), first_decision="REJECTED",
                      first_decision_observed_at=ds.ms_datetime(first_ms), first_blocker="score-min",
                      frozen_setup=setup(), frozen_config=config(), score_trace=None)
        values.update(over)
        return O(**values)

    plan = {
        "tr-a": (T0 + 500, bars(first_bar(T0 + 500), HORIZON) + bars(first_bar(T0 + 500), 14, 7777.5, HORIZON)),
        "tr-b": (T0 + 500, bars(first_bar(T0 + 500), HORIZON)),
        "tr-late": (T0 + 20 * BAR + 123, bars(first_bar(T0 + 20 * BAR + 123), 5)),
        "tr-malformed-str": (T0 + 25 * BAR, [{"timestamp": "abc", "open": 1}]),
        "tr-malformed-float": (T0 + 26 * BAR, [{"timestamp": 1.5e12, "open": 1}]),
        "tr-malformed-obj": (T0 + 27 * BAR, {"timestamp": 1}),
        "tr-badohlc-in": (T0 + 28 * BAR, [dict(bars(T0 + 28 * BAR, 1)[0], open="x")]),
        "tr-badohlc-out": (T0 + 29 * BAR, bars(T0 + 29 * BAR, 3)
                           + [dict(bars(T0 + 29 * BAR, 1, start=40)[0], low=500.0)]),
        "tr-orphan": (T0 + 30 * BAR, bars(T0 + 30 * BAR, 2)),
        "tr-identity": (T0 + 31 * BAR, bars(T0 + 31 * BAR, 2)),
        "tr-config": (T0 + 32 * BAR, bars(T0 + 32 * BAR, 2)),
        "tr-purged": (VAL - BAR, bars(VAL - BAR, 3, 5555.5)),
        "tr-nocandles": (T0 + 33 * BAR, []),
        "before": (T0 - BAR, bars(T0 - BAR, 2, 5555.5)),
        "va-tie-2": (VAL, bars(VAL, 3)),
        "va-tie-1": (VAL, bars(VAL, 3)),
        "va-cut": (VAL + 30 * BAR + 7, bars(first_bar(VAL + 30 * BAR + 7), HORIZON)),
        "ho-1": (HOLD, bars(HOLD, 30, 8888.5)),
        "ho-2": (HOLD + 10 * BAR, bars(HOLD + 10 * BAR, 30, 8888.5)),
        "late": (ds.utc_ms(ds.ms_datetime(0) + timedelta(days=20500)), bars(0, 1, 8888.5)),
    }
    specials = {
        "tr-late": dict(frozen_setup=setup(entry=101.0, stop_loss=96.0, tp1=106.0, tp2=111.0)),
        "tr-config": dict(frozen_config=config(max_holding_bars=20)),
        "ho-1": dict(frozen_setup=setup(entry=8888.5, stop_loss=8000.0, tp1=9000.0, tp2=9500.0)),
    }
    async with db.get_session() as session:
        for key, (decision_ms, candles) in plan.items():
            session.add(rejected(key, decision_ms, candles, **specials.get(key, {})))
            if key == "tr-orphan":
                continue
            opp_over = {}
            if key == "tr-identity":
                opp_over["symbol"] = "OTHER/USDT:USDT"
            if key == "tr-late":   # primeira tentativa anterior, setup diferente
                opp_over["frozen_setup"] = setup(entry=999.5, stop_loss=990.0, tp1=1000.0, tp2=1010.0)
                opp_over["first_decision"] = "INELIGIBLE"
            session.add(opportunity(key, decision_ms - (10 * BAR if key == "tr-late" else 0), **opp_over))
            for n in range(3 if key in ("tr-a", "tr-late") else 1):
                session.add(A(attempt_id=f"{key}-{n}", opportunity_key=key,
                              observed_at=ds.ms_datetime(decision_ms - n), mode="LIVE",
                              result="REJECTED", first_blocker="score-min",
                              submit_evidence="NOT_OBSERVED", score_trace=None))
        await session.commit()

    async def dump():
        async with db.get_session() as session:
            out = []
            for model in (O, A, R):
                rows = (await session.execute(text(
                    f"SELECT to_jsonb(t) FROM {model.__tablename__} t"))).scalars().all()
                out.append(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows))
            return out

    async def export(req_body, **spy_kwargs):
        req = ds.parse_request(req_body)
        async with db.get_session() as session:
            spy = Spy(session, **spy_kwargs)
            dataset, manifest = await ds.load_dataset(spy, req)
        return dataset, manifest, spy

    before = await dump()
    dataset, manifest, spy = await export(request(), check_xid=True)
    counts = manifest["counts"]
    assert counts["before_training"] == 1 and counts["holdout_sealed"] == 2 and counts["after_cutoff"] == 1, counts
    assert counts["training_candidates"] == 13 and counts["validation_candidates"] == 3, counts
    assert counts["purged"] == 1, counts
    assert counts["excluded"] == {"SOURCE_CONTRACT_MISMATCH": 0, "CONFIG_MISMATCH": 1,
                                  "OPPORTUNITY_ROW_MISSING": 1, "IDENTITY_MISMATCH": 1,
                                  "INVALID_SETUP": 0, "TEMPORAL_INCONSISTENCY": 0,
                                  "MALFORMED_CANDLE_JSON": 3, "INVALID_CANDLE_DATA": 1}, counts
    assert counts["exported"] == {"training": 5, "validation": 3}, counts
    ids = [o["opportunity_id"] for o in dataset["opportunities"]]
    assert ids == ["tr-a", "tr-b", "tr-late", "tr-badohlc-out", "tr-nocandles",
                   "va-tie-1", "va-tie-2", "va-cut"], ids
    late = dataset["opportunities"][2]
    assert late["entry"] == 101.0 and late["decision_ts_ms"] == T0 + 20 * BAR + 123, late
    assert len(dataset["bars_by_id"]["tr-a"]) == HORIZON
    assert len(dataset["bars_by_id"]["tr-badohlc-out"]) == 3
    assert dataset["bars_by_id"]["tr-nocandles"] == []
    assert spy.xid is None, "transação do exportador recebeu XID (escrita)"
    fetched_ids = {row["opportunity_key"] for row in spy.fetched}
    assert not fetched_ids & {"ho-1", "ho-2", "late", "before"}, fetched_ids
    detail_rows = [row for row in spy.fetched if "candles" in row]
    assert {row["opportunity_key"] for row in detail_rows} == set(ids) | {
        "tr-malformed-str", "tr-malformed-float", "tr-malformed-obj", "tr-badohlc-in",
        "tr-orphan", "tr-identity", "tr-config"}
    assert all("outcome" not in row and "coverage" not in row for row in spy.fetched)
    blob = json.dumps(spy.fetched, default=str) + json.dumps(dataset) + json.dumps(manifest)
    for sentinel in SENTINELS:
        assert sentinel not in blob, sentinel
    async with db.get_session() as session:   # controle: o JSONB bruto TEM a cauda
        raw = (await session.execute(select(R.candles).where(R.opportunity_key == "tr-a"))).scalar_one()
    assert len(raw) == 40 and any(c["open"] == 7777.5 for c in raw)
    assert await dump() == before, "exportador alterou tabelas"

    # Prova direta de READ ONLY na transação configurada pelo exportador:
    # uma escrita injetada logo após o SET é recusada e aborta a exportação.
    aborted = False
    async with db.get_session() as session:
        spy_write = Spy(session, inject_write=True)
        try:
            await ds.load_dataset(spy_write, ds.parse_request(request()))
        except Exception:
            aborted = True
    assert aborted, "escrita injetada deveria abortar a exportação"
    assert spy_write.write_error and "read-only" in spy_write.write_error, spy_write.write_error
    assert await dump() == before

    # Integração: o payload é aceito tal como está pelo R10A (A/A → delta zero).
    result = r10a.run_payload(json.loads(ds.canonical_bytes(dataset)))
    assert result["counts"] == {"training": 5, "validation": 3, "purged": 0,
                                "holdout_sealed": 0, "before_training": 0}, result["counts"]
    for split_name in ("training", "validation"):
        ci = result["splits"][split_name]["paired_delta_ci"]
        assert ci is None or ci["point"] == 0.0, ci

    # Outcomes/cobertura e detalhes selados mudam: dataset e fingerprints NÃO.
    async with db.get_session() as session:
        await session.execute(update(R).values(outcome={"status": "CLOSED_STOP", "gross_r": -1},
                                               coverage="AMBIGUOUS"))
        await session.execute(update(R).where(R.opportunity_key.in_(["ho-1", "ho-2"])).values(
            candles=bars(HOLD, 5, 4321.5), frozen_setup=setup(entry=4321.5)))
        await session.commit()
    again, manifest2, _ = await export(request())
    assert ds.canonical_bytes(again) == ds.canonical_bytes(dataset)
    assert manifest2 == manifest
    async with db.get_session() as session:   # só metadado permitido altera a contagem selada
        session.add(rejected("ho-3", HOLD + 20 * BAR, bars(HOLD + 20 * BAR, 3, 8888.5)))
        await session.commit()
    _, manifest3, _ = await export(request())
    assert manifest3["counts"]["holdout_sealed"] == 3
    assert manifest3["fingerprints"] == manifest["fingerprints"]

    # Versão/vela de origem mudam: fingerprint muda.
    async with db.get_session() as session:
        await session.execute(update(R).where(R.opportunity_key == "tr-b").values(version=R.version + 1))
        await session.commit()
    _, manifest4, _ = await export(request())
    assert manifest4["fingerprints"]["dataset_sha256"] == manifest["fingerprints"]["dataset_sha256"]
    assert manifest4["fingerprints"]["source_sha256"] != manifest["fingerprints"]["source_sha256"]
    changed = bars(first_bar(T0 + 500), HORIZON)
    changed[0]["high"] = 101.5
    async with db.get_session() as session:
        await session.execute(update(R).where(R.opportunity_key == "tr-b").values(candles=changed))
        await session.commit()
    _, manifest5, _ = await export(request())
    assert manifest5["fingerprints"]["dataset_sha256"] != manifest["fingerprints"]["dataset_sha256"]

    # Cutoff dentro do horizonte: fim exatamente no cutoff entra; atravessando, não.
    cut_first = first_bar(VAL + 30 * BAR + 7)
    as_of = ds.ms_datetime(cut_first + 5 * BAR).isoformat().replace("+00:00", "Z")
    cut, cut_manifest, cut_spy = await export(request(as_of))
    stamps = [b["timestamp_ms"] for b in cut["bars_by_id"]["va-cut"]]
    assert stamps == [cut_first + i * BAR for i in range(5)], stamps
    assert cut_manifest["coverage"]["cutoff_truncated_windows"] == 1
    assert cut_manifest["counts"]["holdout_sealed"] == 0
    assert cut_manifest["counts"]["after_cutoff"] == 4, cut_manifest["counts"]
    returned = [c["timestamp"] for row in cut_spy.fetched if "candles" in row
                for c in (row["candles"] if isinstance(row["candles"], list) else json.loads(row["candles"]))]
    assert returned and max(returned) + BAR <= cut_first + 5 * BAR, max(returned)

    # Limite: excedeu → falha clara, sem truncar; transação encerrada.
    with patch.object(r10a, "MAX_OPPORTUNITIES", 5):
        try:
            await export(request())
            raise AssertionError("limite deveria falhar")
        except ds.DatasetLimitError:
            pass
    assert (await dump())[0] == before[0]

    # CLI ponta a ponta (thread própria) + CLI R10A separado sobre o arquivo.
    workdir = Path(tempfile.mkdtemp(prefix="r10b-pg-cli-"))
    try:
        req_file = workdir / "request.json"
        req_file.write_text(json.dumps(request()))
        await db._engine.dispose()
        code = await asyncio.to_thread(cli.main, ["--request", str(req_file), "--read-db",
                                                  "--out-dir", str(workdir / "export")])
        assert code == 0, code
        direct, direct_manifest, _ = await export(request())
        exported = (workdir / "export" / "dataset.json").read_bytes()
        assert exported == ds.canonical_bytes(direct)
        assert json.loads((workdir / "export" / "manifest.json").read_text()) == direct_manifest
        code = await asyncio.to_thread(cli.main, ["--request", str(req_file), "--read-db",
                                                  "--out-dir", str(workdir / "export")])
        assert code == 2, "sobrescrita deveria ser recusada"
        replay = subprocess.run([sys.executable, "-B", str(BACKEND / "scripts" / "research_replay.py"),
                                 str(workdir / "export" / "dataset.json")],
                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin",
                                                                     "PYTHONDONTWRITEBYTECODE": "1"})
        assert replay.returncode == 0, replay.stderr[-500:]
        assert json.loads(replay.stdout)["decision"] == "NO_PROMOTION_RESEARCH_ONLY"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    await db._engine.dispose()
    print("R10B_PG_INTEGRATION_OK: read-only+no-xid, write-rejected, server-side-jsonb, tail-filtered, "
          "purge, ties, holdout-count-only, outcome-invariant, fingerprint, cutoff-edge, limit, "
          "cli-export, r10a-cli, tables-unchanged")


if __name__ == "__main__":
    asyncio.run(run())
