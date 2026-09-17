"""R10B — exportação somente leitura das vetadas R09 para o comparador R10A.

Hermético: rede/DNS bloqueados e contados; sem banco real (o PostgreSQL real
está em `pg_integration_r10b.py`). Datas sintéticas explícitas.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket as _socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
_REAL = (_socket.getaddrinfo, _socket.create_connection)
_NET: list = []


def _blocked(*args, **kwargs):
    _NET.append(args[:1])
    raise RuntimeError("REDE BLOQUEADA no teste R10B")


def setUpModule():
    _NET.clear()
    _socket.getaddrinfo = _blocked
    _socket.create_connection = _blocked


def tearDownModule():
    _socket.getaddrinfo, _socket.create_connection = _REAL
    if _NET:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET}")


from services import offline_replay_service as r10a  # noqa: E402
from services import research_dataset_service as ds  # noqa: E402

BAR = 300_000
T0 = 1_760_000_100_000          # sintético, alinhado a 5m
VAL = T0 + 100 * BAR
HOLD = T0 + 200 * BAR
R09_REPLAY = {"bar_ms": BAR, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
              "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
              "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96}
HORIZON = 3 + 24 - 1


def request(**over):
    body = {
        "as_of_utc": "2026-01-01T00:00:00Z",
        "split": {"train_start_ms": T0, "validation_start_ms": VAL,
                  "holdout_start_ms": HOLD, "purge_bars": 1},
        "baseline_config": dict(R09_REPLAY),
        "candidate": {"candidate_id": "R10B-SYNTH-AA", "registered_at_ms": T0 - 1,
                      "kind": "MANAGEMENT_ONLY", "replay_config": dict(R09_REPLAY)},
        "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0, "funding_bps_per_bar": 1.0},
        "bootstrap": {"seed": 7, "samples": 100, "block_size": 1},
    }
    body.update(over)
    return body


def dt(ms):
    return ds.ms_datetime(ms)


def frozen_config(**over):
    return {"schema_version": "r09.v1", "policy": "ISOLATED_REPLAY_NOT_LIVE",
            "scope": "POST_SELECTION", **R09_REPLAY, "cost_status": "UNKNOWN",
            "learning_eligible": False, "version_hash": "abc", **over}


def setup(**over):
    return {"symbol": "SYN/USDT:USDT", "timeframe": "15m", "direction": "long", "tier": "A",
            "entry": 100.0, "stop_loss": 95.0, "tp1": 105.0, "tp2": 110.0, "atr": 2.0,
            "score": 70.0, "candle_close_ms": None, **over}


def candles(first_ms, specs):
    return [{"timestamp": first_ms + i * BAR, "open": o, "high": h, "low": l, "close": c, "volume": 10}
            for i, (o, h, l, c) in enumerate(specs)]


STOP = [(100, 101, 99, 100), (99, 99.5, 94, 95)]


def first_bar(decision_ms):
    return ((decision_ms + BAR - 1) // BAR) * BAR


def detail(key, decision_ms, **over):
    row = {"opportunity_key": key, "symbol": "SYN/USDT:USDT", "decision_at": dt(decision_ms),
           "version": 3, "frozen_setup": setup(), "frozen_config": frozen_config(),
           "opportunity_found": True, "opportunity_symbol": "SYN/USDT:USDT",
           "opportunity_scope": "POST_SELECTION", "candles_malformed": False,
           "candles": candles(first_bar(decision_ms), STOP)}
    row.update(over)
    return row


def build(rows, req=None, sealed=None):
    req = req or ds.parse_request(request())
    plan = ds.plan_selection(req, [(r["opportunity_key"], r["decision_at"]) for r in rows])
    admitted = set(plan.keys)
    return ds.build_artifacts(req, plan, [r for r in rows if r["opportunity_key"] in admitted],
                              sealed or {})


class RequestContract(unittest.TestCase):
    def test_valid_request_hash_stable_and_time_free(self):
        a, b = ds.parse_request(request()), ds.parse_request(deepcopy(request()))
        self.assertEqual(a.request_hash(), b.request_hash())
        self.assertEqual(a.changed, ())
        self.assertEqual(a.horizon_bars, HORIZON)
        with patch.dict(os.environ, {"EDGE_DECAY_ENABLED": "true", "SCORE_MIN": "1"}):
            self.assertEqual(ds.parse_request(request()).request_hash(), a.request_hash())
        self.assertNotIn("now", json.dumps(a.normalized()))

    def test_candidate_single_change_only(self):
        one = request()
        one["candidate"]["replay_config"]["tp1_fraction"] = 0.3
        self.assertEqual(ds.parse_request(one).changed, ("tp1_fraction",))
        two = deepcopy(one)
        two["candidate"]["replay_config"]["be_lock_fraction"] = 0.1
        tf = request()
        tf["candidate"]["replay_config"]["bar_ms"] = 60_000
        for bad in (two, tf):
            with self.assertRaises(ds.DatasetError):
                ds.parse_request(bad)

    def test_closed_schema_and_no_defaults(self):
        cases = []
        cases.append(dict(request(), extra=1))
        missing = request()
        missing.pop("bootstrap")
        cases.append(missing)
        no_default = request()
        no_default["baseline_config"].pop("max_bars")
        cases.append(no_default)
        cost_key = request()
        cost_key["costs"].pop("funding_bps_per_bar")
        cases.append(cost_key)
        unknown_nested = request()
        unknown_nested["split"]["embargo"] = 3
        cases.append(unknown_nested)
        structural = request()
        structural["candidate"] = {"candidate_id": "x", "registered_at_ms": T0 - 1,
                                   "kind": "STRUCTURAL_CONF_ONLY", "replay_config": None}
        cases.append(structural)
        weights = request()
        weights["candidate"]["baseline_score_weights"] = [0.6, 0.3, 0.1]
        cases.append(weights)
        for index, body in enumerate(cases):
            with self.subTest(index), self.assertRaises(ds.DatasetError):
                ds.parse_request(body)

    def test_invalid_values(self):
        cases = [
            {"as_of_utc": "2026-01-01T00:00:00"}, {"as_of_utc": "2026-01-01T00:00:00-03:00"},
            {"as_of_utc": "2026-01-01T00:00:00.000500Z"}, {"as_of_utc": 1767225600000},
            {"as_of_utc": "2025-10-09T00:00:00Z"},   # antes do treino
        ]
        for over in cases:
            with self.subTest(over), self.assertRaises(ds.DatasetError):
                ds.parse_request(request(**over))
        for path, value in [(("split", "purge_bars"), True), (("costs", "fee_bps_per_side"), float("nan")),
                            (("costs", "fee_bps_per_side"), "4"), (("bootstrap", "samples"), 5),
                            (("baseline_config", "tp1_fraction"), True),
                            (("candidate", "registered_at_ms"), T0 + 1)]:
            body = request()
            body[path[0]][path[1]] = value
            with self.subTest(path), self.assertRaises(ds.DatasetError):
                ds.parse_request(body)

    def test_missing_costs_stay_none(self):
        body = request()
        body["costs"] = {"fee_bps_per_side": None, "slippage_bps_per_side": 2.0, "funding_bps_per_bar": None}
        req = ds.parse_request(body)
        _, manifest = build([detail("k1", T0 + 1000)], req)
        self.assertEqual(manifest["costs"], {"status": "UNKNOWN", "observed_account_costs": False,
                                             "net_r_comparable": False})
        dataset, _ = build([detail("k1", T0 + 1000)], req)
        self.assertIsNone(dataset["costs"]["fee_bps_per_side"])
        result = r10a.run_payload(dataset)
        self.assertIsNone(result["rows"][0]["baseline"]["net_r"])
        _, known = build([detail("k1", T0 + 1000)])
        self.assertEqual(known["costs"]["status"], "DECLARED_SCENARIO")
        self.assertFalse(known["costs"]["observed_account_costs"])


class Selection(unittest.TestCase):
    def test_semi_open_windows_ties_and_stable_order(self):
        req = ds.parse_request(request())
        rows = [("b", dt(VAL)), ("a", dt(VAL)), ("z", dt(T0)), ("c", dt(VAL - 1))]
        plan = ds.plan_selection(req, rows)
        self.assertEqual(plan.counts["training_candidates"], 2)
        self.assertEqual(plan.counts["validation_candidates"], 2)
        order = [(r.key, r.split) for r in plan.rows]
        self.assertEqual(order, [("z", "training"), ("a", "validation"), ("b", "validation")])
        self.assertEqual(plan.counts["purged"], 1)   # "c" encosta na fronteira

    def test_purge_parity_with_real_comparator(self):
        req = ds.parse_request(request())
        decisions = [T0 + i * 7 * BAR + 1234 for i in range(28)]
        index = [(f"k{i:02d}", dt(ms)) for i, ms in enumerate(decisions)]
        plan = ds.plan_selection(req, index)
        opportunities = [r10a.Opportunity(opportunity_id=k, symbol="S", direction="long",
                                          decision_ts_ms=ds.utc_ms(d), entry=100.0, stop_loss=95.0,
                                          tp1=105.0, tp2=110.0) for k, d in index]
        compared = r10a.compare_registered_candidate(
            opportunities, {}, req.baseline, req.candidate, req.costs, req.split, req.bootstrap)
        self.assertEqual(plan.counts["purged"], compared["counts"]["purged"])
        admitted = {r["opportunity_id"] for r in compared["rows"]}
        self.assertEqual(admitted, set(plan.keys))
        longer = request()
        longer["candidate"]["replay_config"]["max_holding_bars"] = 40
        req2 = ds.parse_request(longer)
        plan2 = ds.plan_selection(req2, index)
        compared2 = r10a.compare_registered_candidate(
            opportunities, {}, req2.baseline, req2.candidate, req2.costs, req2.split, req2.bootstrap)
        self.assertEqual(plan2.counts["purged"], compared2["counts"]["purged"])
        self.assertGreater(plan2.counts["purged"], plan.counts["purged"])

    def test_window_end_respects_horizon_boundary_and_cutoff(self):
        req = ds.parse_request(request())
        (row,) = ds.plan_selection(req, [("k", dt(T0 + 1))]).rows
        self.assertEqual(row.first_ms, T0 + BAR)
        self.assertEqual(row.window_end_ms, T0 + BAR + HORIZON * BAR)
        self.assertFalse(row.cutoff_truncated)
        early = ds.parse_request(request(as_of_utc=ds.ms_datetime(T0 + 10 * BAR).isoformat().replace("+00:00", "Z")))
        (cut,) = ds.plan_selection(early, [("k", dt(T0 + 1))]).rows
        self.assertEqual(cut.window_end_ms, T0 + 10 * BAR)
        self.assertTrue(cut.cutoff_truncated)

    def test_rows_outside_window_duplicates_and_limit_fail_loudly(self):
        req = ds.parse_request(request())
        for rows in ([("h", dt(HOLD))], [("x", dt(T0 - 1))], [("d", dt(T0)), ("d", dt(T0 + 1))],
                     [("", dt(T0))]):
            with self.subTest(rows), self.assertRaises(ds.DatasetError):
                ds.plan_selection(req, rows)
        too_many = [(f"k{i}", dt(T0)) for i in range(r10a.MAX_OPPORTUNITIES + 1)]
        with self.assertRaises(ds.DatasetLimitError):
            ds.plan_selection(req, too_many)

    def test_exact_millisecond_floor(self):
        value = datetime(2026, 1, 1, 0, 0, 0, 999_999, tzinfo=timezone.utc)
        self.assertEqual(ds.utc_ms(value), ds.utc_ms(value.replace(microsecond=0)) + 999)
        with self.assertRaises(ds.DatasetError):
            ds.utc_ms(datetime(2026, 1, 1))


class Artifacts(unittest.TestCase):
    def test_payload_accepted_by_r10a_and_aa_delta_zero(self):
        rows = [detail(f"k{i}", T0 + i * 30 * BAR + 500) for i in range(3)]
        rows += [detail(f"v{i}", VAL + i * 30 * BAR + 500) for i in range(2)]
        dataset, manifest = build(rows)
        self.assertEqual(manifest["state"], "EXPORTED")
        result = r10a.run_payload(json.loads(ds.canonical_bytes(dataset)))
        self.assertEqual(result["counts"]["training"], 3)
        self.assertEqual(result["counts"]["purged"], 0)
        self.assertEqual(result["counts"]["holdout_sealed"], 0)
        training = result["splits"]["training"]
        self.assertEqual(training["paired_resolved_n"], 3)
        self.assertEqual(training["paired_delta_ci"]["point"], 0.0)
        self.assertEqual(result["rows"][0]["baseline"]["status"], "CLOSED_STOP")

    def test_single_parameter_candidate_changes_only_that_field(self):
        body = request()
        body["candidate"]["replay_config"]["tp1_fraction"] = 0.3
        dataset, manifest = build([detail("k", T0 + 500)], ds.parse_request(body))
        diff = {k for k in dataset["baseline_config"]
                if dataset["baseline_config"][k] != dataset["candidate"]["replay_config"][k]}
        self.assertEqual(diff, {"tp1_fraction"})
        self.assertEqual(manifest["configs"]["management_changed_parameters"], ["tp1_fraction"])

    def test_timestamp_conversion_preserves_values_and_zero_volume(self):
        row = detail("k", T0 + 500)
        row["candles"][0]["volume"] = 0
        dataset, _ = build([row])
        bars = dataset["bars_by_id"]["k"]
        self.assertEqual(bars[0], {"timestamp_ms": T0 + BAR, "open": 100, "high": 101,
                                   "low": 99, "close": 100, "volume": 0})
        self.assertIsInstance(bars[0]["open"], int)

    def test_valid_opportunity_without_candles_is_kept(self):
        dataset, manifest = build([detail("k", T0 + 500, candles=[])])
        self.assertEqual(dataset["bars_by_id"], {"k": []})
        self.assertEqual(manifest["coverage"]["without_candles"], 1)
        result = r10a.run_payload(dataset)
        self.assertEqual(result["rows"][0]["baseline"]["status"], "INSUFFICIENT_DATA")

    def test_exclusions_are_explained_and_counted(self):
        bad = {
            "SOURCE_CONTRACT_MISMATCH": lambda ms: dict(frozen_config=frozen_config(schema_version="r09.v0")),
            "CONFIG_MISMATCH": lambda ms: dict(frozen_config=frozen_config(max_holding_bars=20)),
            "OPPORTUNITY_ROW_MISSING": lambda ms: dict(opportunity_found=False, opportunity_symbol=None,
                                                       opportunity_scope=None),
            "IDENTITY_MISMATCH": lambda ms: dict(opportunity_symbol="OTHER/USDT:USDT"),
            "INVALID_SETUP": lambda ms: dict(frozen_setup=setup(stop_loss=101.0)),
            "TEMPORAL_INCONSISTENCY": lambda ms: dict(frozen_setup=setup(candle_close_ms=float(ms + 1000))),
            "MALFORMED_CANDLE_JSON": lambda ms: dict(candles_malformed=True),
            "INVALID_CANDLE_DATA": lambda ms: dict(
                candles=[{**candles(first_bar(ms), STOP)[0], "open": "100"}]),
        }
        rows = []
        for i, (name, make) in enumerate(bad.items()):
            ms = T0 + i * 10 * BAR + 500
            rows.append(detail(name, ms, **make(ms)))
        dataset, manifest = build(rows)
        self.assertEqual(manifest["state"], "UNUSABLE_ALL_EXCLUDED")
        self.assertEqual(manifest["counts"]["excluded"], {name: 1 for name in bad})
        self.assertEqual(dataset["opportunities"], [])
        self.assertEqual(r10a.run_payload(dataset)["counts"]["training"], 0)

    def test_more_invalid_setups_and_config_types(self):
        variants = [setup(entry=True), setup(direction="LONG"), setup(tp1=None), setup(atr=0.0),
                    setup(entry="100"), setup(symbol="OTHER/USDT:USDT")]
        for index, frozen in enumerate(variants):
            _, manifest = build([detail("k", T0 + 500, frozen_setup=frozen)])
            self.assertEqual(sum(manifest["counts"]["excluded"].values()), 1, index)
        for value in ("300000", True, 300000.5):
            _, manifest = build([detail("k", T0 + 500, frozen_config=frozen_config(bar_ms=value))])
            self.assertEqual(manifest["counts"]["excluded"]["CONFIG_MISMATCH"], 1, value)

    def test_rejection_setup_and_time_are_used_not_first_seen(self):
        later = T0 + 40 * BAR + 777
        row = detail("k", later, frozen_setup=setup(entry=101.0, stop_loss=96.0, tp1=106.0, tp2=111.0))
        dataset, _ = build([row])
        opp = dataset["opportunities"][0]
        self.assertEqual((opp["decision_ts_ms"], opp["entry"]), (later, 101.0))

    def test_contract_violations_raise(self):
        req = ds.parse_request(request())
        plan = ds.plan_selection(req, [("k", dt(T0 + 500))])
        base = detail("k", T0 + 500)
        cases = [
            [base, dict(base)],                                   # detalhe duplicado
            [],                                                   # detalhe ausente
            [dict(base, decision_at=dt(T0 + 501))],               # decisão mudou
            [dict(base, candles=candles(T0, STOP))],              # vela antes da janela
            [dict(base, candles=candles(T0 + BAR + HORIZON * BAR, STOP))],   # após horizonte
            [dict(base, candles={"timestamp": 1})],               # projeção não é lista
        ]
        for index, rows in enumerate(cases):
            with self.subTest(index), self.assertRaises(ds.DatasetError):
                ds.build_artifacts(req, plan, rows, {})

    def test_deterministic_hashes_and_fingerprint_sensitivity(self):
        rows = [detail("k", T0 + 500), detail("v", VAL + 500)]
        first = build(deepcopy(rows), sealed={"holdout_sealed": 2})
        again = build(deepcopy(rows), sealed={"holdout_sealed": 2})
        self.assertEqual(ds.canonical_bytes(first[0]), ds.canonical_bytes(again[0]))
        self.assertEqual(first[1], again[1])
        fp = first[1]["fingerprints"]
        self.assertEqual(fp["dataset_sha256"], hashlib.sha256(ds.canonical_bytes(first[0])).hexdigest())
        sealed_only = build(deepcopy(rows), sealed={"holdout_sealed": 9})[1]
        self.assertEqual(sealed_only["fingerprints"], fp)
        self.assertEqual(sealed_only["counts"]["holdout_sealed"], 9)
        changed_candle = deepcopy(rows)
        changed_candle[0]["candles"][1]["low"] = 93
        self.assertNotEqual(build(changed_candle)[1]["fingerprints"]["dataset_sha256"], fp["dataset_sha256"])
        bumped = deepcopy(rows)
        bumped[0]["version"] = 4
        bumped_fp = build(bumped)[1]["fingerprints"]
        self.assertEqual(bumped_fp["dataset_sha256"], fp["dataset_sha256"])
        self.assertNotEqual(bumped_fp["source_sha256"], fp["source_sha256"])
        config_hash = deepcopy(rows)
        config_hash[0]["frozen_config"]["version_hash"] = "other"
        self.assertNotEqual(build(config_hash)[1]["fingerprints"]["source_sha256"], fp["source_sha256"])
        with patch.dict(os.environ, {"LEARNING_AUTO_ADJUST": "false"}):
            self.assertEqual(build(deepcopy(rows), sealed={"holdout_sealed": 2})[1], first[1])

    def test_in_memory_artifacts_equal_their_json(self):
        dataset, manifest = build([detail("k", T0 + 500)])
        self.assertEqual(json.loads(ds.canonical_bytes(manifest)), manifest)
        self.assertEqual(json.loads(ds.canonical_bytes(dataset)), dataset)
        self.assertIsInstance(manifest["configs"]["candidate"]["baseline_score_weights"], list)

    def test_manifest_contract_and_coverage(self):
        rows = [detail("full", T0 + 500, candles=candles(T0 + BAR, [(100, 101, 99, 100)] * HORIZON)),
                detail("gap", T0 + 40 * BAR + 500,
                       candles=[candles(T0 + 41 * BAR, STOP)[0], candles(T0 + 43 * BAR, STOP)[0]]),
                detail("none", T0 + 60 * BAR + 500, candles=[])]
        _, manifest = build(rows)
        cov = manifest["coverage"]
        self.assertEqual((cov["complete_windows"], cov["incomplete_windows"]), (1, 2))
        self.assertEqual((cov["with_candles"], cov["without_candles"]), (2, 1))
        self.assertEqual(cov["non_contiguous_windows"], 1)
        self.assertEqual(manifest["source"]["cohort"], "R09_REJECTED_POST_SELECTION")
        self.assertEqual(manifest["source"]["price_source"], "SNAPSHOT_RESOLVER_WINDOW_UNLABELED")
        self.assertFalse(manifest["source"]["outcome_or_coverage_read"])
        self.assertEqual(manifest["holdout"]["policy"], "SEALED")
        self.assertIsNone(manifest["approval"])
        self.assertEqual(manifest["economic_sufficiency"], "NOT_ASSESSED")
        text = " ".join(manifest["limitations"])
        for required in ("Coorte", "snapshot aberto", "sem rótulo", "terminais", "CENÁRIOS",
                         "operador", "mesmo cutoff", "holdout_sealed", "executor LIVE"):
            self.assertIn(required, text)
        json.dumps(manifest, allow_nan=False)

    def test_full_length_invalid_grid_is_incomplete_without_filtering(self):
        full = candles(T0 + BAR, [(100, 101, 99, 100)] * HORIZON)
        duplicate_missing = deepcopy(full)
        duplicate_missing[-1] = dict(duplicate_missing[-2])
        off_grid = deepcopy(full)
        off_grid[10]["timestamp"] += 1
        misaligned_start = deepcopy(full)
        misaligned_start[0]["timestamp"] += 1
        out_of_order = deepcopy(full)
        out_of_order[10], out_of_order[11] = out_of_order[11], out_of_order[10]
        for name, source in (("duplicate_missing", duplicate_missing), ("off_grid", off_grid),
                             ("misaligned_start", misaligned_start), ("out_of_order", out_of_order)):
            with self.subTest(name):
                dataset, manifest = build([detail("k", T0 + 500, candles=source)])
                cov = manifest["coverage"]
                self.assertEqual((cov["complete_windows"], cov["incomplete_windows"]), (0, 1))
                self.assertEqual(cov["non_contiguous_windows"], 1)
                self.assertEqual(cov["cutoff_truncated_windows"], 0)
                self.assertEqual(cov["candles_exported"], HORIZON)
                self.assertEqual(manifest["counts"]["exported"], {"training": 1, "validation": 0})
                self.assertEqual(sum(manifest["counts"]["excluded"].values()), 0)
                expected_bars = [{"timestamp_ms": bar["timestamp"],
                                  **{key: value for key, value in bar.items() if key != "timestamp"}}
                                 for bar in source]
                self.assertEqual(dataset["bars_by_id"], {"k": expected_bars})

    def test_exact_full_grid_is_complete(self):
        source = candles(T0 + BAR, [(100, 101, 99, 100)] * HORIZON)
        _, manifest = build([detail("k", T0 + 500, candles=source)])
        self.assertEqual(manifest["coverage"], {
            "with_candles": 1, "without_candles": 0, "candles_exported": HORIZON,
            "complete_windows": 1, "incomplete_windows": 0,
            "cutoff_truncated_windows": 0, "non_contiguous_windows": 0,
        })

    def test_empty_and_partial_grids_remain_incomplete(self):
        for count in (0, HORIZON - 1):
            with self.subTest(count):
                source = candles(T0 + BAR, [(100, 101, 99, 100)] * count)
                dataset, manifest = build([detail("k", T0 + 500, candles=source)])
                self.assertEqual(manifest["coverage"], {
                    "with_candles": int(count > 0), "without_candles": int(count == 0),
                    "candles_exported": count, "complete_windows": 0, "incomplete_windows": 1,
                    "cutoff_truncated_windows": 0, "non_contiguous_windows": 0,
                })
                self.assertEqual(len(dataset["opportunities"]), 1)

    def test_exact_cutoff_grid_remains_incomplete_and_truncated(self):
        cutoff = dt(T0 + 10 * BAR).isoformat().replace("+00:00", "Z")
        req = ds.parse_request(request(as_of_utc=cutoff))
        source = candles(T0 + BAR, [(100, 101, 99, 100)] * 9)
        dataset, manifest = build([detail("k", T0 + 500, candles=source)], req)
        self.assertEqual(manifest["coverage"], {
            "with_candles": 1, "without_candles": 0, "candles_exported": 9,
            "complete_windows": 0, "incomplete_windows": 1,
            "cutoff_truncated_windows": 1, "non_contiguous_windows": 0,
        })
        self.assertEqual(dataset["bars_by_id"]["k"][-1]["timestamp_ms"] + BAR, req.as_of_ms)

    def test_empty_and_all_purged_states(self):
        req = ds.parse_request(request())
        empty = ds.build_artifacts(req, ds.plan_selection(req, []), [], {"holdout_sealed": 4})
        self.assertEqual(empty[1]["state"], "EMPTY")
        self.assertEqual(r10a.run_payload(empty[0])["counts"]["training"], 0)
        plan = ds.plan_selection(req, [("c", dt(VAL - 1))])
        self.assertEqual(ds.build_artifacts(req, plan, [], {})[1]["state"], "UNUSABLE_ALL_PURGED")

    def test_json_strings_from_driver_are_decoded_strictly(self):
        row = detail("k", T0 + 500)
        row["frozen_setup"] = json.dumps(row["frozen_setup"])
        row["candles"] = json.dumps(row["candles"])
        dataset, _ = build([row])
        self.assertEqual(len(dataset["bars_by_id"]["k"]), 2)
        row = detail("k", T0 + 500, candles='[{"timestamp": NaN}]')
        with self.assertRaises(ds.DatasetError):
            build([row])


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self, replies, mode=("on", "repeatable read"), fail_at=None, active=False):
        self.replies, self.mode, self.fail_at, self.active = list(replies), mode, fail_at, active
        self.sql, self.params, self.rolled_back = [], [], 0

    def in_transaction(self):
        return self.active

    async def execute(self, statement, params=None):
        text_sql = " ".join(str(statement).split())
        self.sql.append(text_sql)
        self.params.append(params or {})
        if self.fail_at is not None and len(self.sql) == self.fail_at:
            raise RuntimeError("postgresql://user:SECRET@host/db falhou")
        if text_sql.startswith("SET TRANSACTION"):
            return _Result([])
        if "current_setting" in text_sql:
            return _Result([{"read_only": self.mode[0], "isolation": self.mode[1]}])
        return _Result(self.replies.pop(0))

    async def rollback(self):
        self.rolled_back += 1


class Loader(unittest.IsolatedAsyncioTestCase):
    def replies(self):
        rows = [detail("k", T0 + 500)]
        return [[{"before_training": 1, "holdout_sealed": 5, "after_cutoff": 2}],
                [{"opportunity_key": "k", "decision_at": dt(T0 + 500)}], rows]

    async def test_read_only_sequence_and_rollback(self):
        session = _Session(self.replies())
        dataset, manifest = await ds.load_dataset(session, ds.parse_request(request()))
        self.assertEqual(session.sql[0], ds._SET_READ_ONLY)
        self.assertEqual(session.rolled_back, 1)
        self.assertEqual(manifest["counts"]["holdout_sealed"], 5)
        self.assertEqual(len(dataset["opportunities"]), 1)
        for sql in session.sql:
            head = sql.split()[0].upper()
            self.assertIn(head, {"SET", "SELECT", "WITH"})
            for forbidden in ("INSERT", "UPDATE ", "DELETE", "ALTER", "CREATE", "LOCK",
                              "advisory", "outcome", "coverage", "FOR UPDATE"):
                self.assertNotIn(forbidden, sql)
        detail_sql, params = session.sql[-1], session.params[-1]
        self.assertIn("unnest(CAST(:keys AS text[])", detail_sql)
        self.assertIn("jsonb_array_elements", detail_sql)
        self.assertIn("w.ts + CAST(:bar_ms AS bigint) <= b.hi", detail_sql)
        self.assertEqual(params["keys"], ["k"])
        self.assertEqual(params["his"], [T0 + BAR + HORIZON * BAR])
        index_params = session.params[3]
        self.assertEqual(index_params["row_limit"], r10a.MAX_OPPORTUNITIES + 1)
        self.assertEqual(ds.utc_ms(index_params["index_end"]), HOLD)

    async def test_holdout_only_counted_never_detailed(self):
        session = _Session(self.replies())
        await ds.load_dataset(session, ds.parse_request(request()))
        holdout_sql = [s for s in session.sql if ":holdout_start" in s]
        self.assertEqual(len(holdout_sql), 1)
        self.assertTrue(holdout_sql[0].startswith("SELECT count(*)"))

    async def test_no_detail_query_when_nothing_admitted(self):
        replies = self.replies()[:2]
        replies[1] = []
        session = _Session(replies)
        _, manifest = await ds.load_dataset(session, ds.parse_request(request()))
        self.assertEqual(manifest["state"], "EMPTY")
        self.assertFalse(any("unnest" in s for s in session.sql))

    async def test_errors_roll_back_and_refuse_bad_sessions(self):
        req = ds.parse_request(request())
        failing = _Session(self.replies(), fail_at=4)
        with self.assertRaises(RuntimeError):
            await ds.load_dataset(failing, req)
        self.assertEqual(failing.rolled_back, 1)
        writable = _Session(self.replies(), mode=("off", "repeatable read"))
        with self.assertRaises(ds.DatasetError):
            await ds.load_dataset(writable, req)
        self.assertEqual(writable.rolled_back, 1)
        active = _Session(self.replies(), active=True)
        with self.assertRaises(ds.DatasetError):
            await ds.load_dataset(active, req)
        self.assertEqual(active.sql, [])
        with self.assertRaises(ds.DatasetError):
            await ds.load_dataset(_Session([]), request())

    async def test_limit_plus_one_fails_without_truncation(self):
        replies = self.replies()
        replies[1] = [{"opportunity_key": f"k{i}", "decision_at": dt(T0 + i)}
                      for i in range(r10a.MAX_OPPORTUNITIES + 1)]
        session = _Session(replies)
        with self.assertRaises(ds.DatasetLimitError):
            await ds.load_dataset(session, ds.parse_request(request()))
        self.assertEqual(session.rolled_back, 1)
        self.assertFalse(any("unnest" in s for s in session.sql))


class Writing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r10b-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dataset, self.manifest = build([detail("k", T0 + 500)])

    def test_pair_written_canonically(self):
        target = ds.write_artifacts(self.tmp / "out", self.dataset, self.manifest)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["dataset.json", "manifest.json"])
        raw = (target / "dataset.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.manifest["fingerprints"]["dataset_sha256"])
        self.assertEqual(r10a.run_payload(json.loads(raw))["counts"]["training"], 1)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["out"])

    def test_never_overwrites_and_rejects_bad_destinations(self):
        (self.tmp / "exists").mkdir()
        for target in (self.tmp / "exists", self.tmp / "missing" / "out",
                       BACKEND / "r10b-should-not-exist", BACKEND.parent / "docs" / "x"):
            with self.subTest(target), self.assertRaises(ds.DatasetError):
                ds.write_artifacts(target, self.dataset, self.manifest)
        self.assertFalse((BACKEND / "r10b-should-not-exist").exists())
        (self.tmp / "file").write_text("x")
        with self.assertRaises(ds.DatasetError):
            ds.write_artifacts(self.tmp / "file", self.dataset, self.manifest)

    def test_failure_leaves_no_artifact_pair(self):
        real = os.rename
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disco cheio")
            return real(src, dst)
        with patch.object(ds.os, "rename", flaky), self.assertRaises(OSError):
            ds.write_artifacts(self.tmp / "out", self.dataset, self.manifest)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_manifest_must_match_dataset(self):
        tampered = deepcopy(self.dataset)
        tampered["opportunities"][0]["entry"] = 100.5
        with self.assertRaises(ds.DatasetError):
            ds.write_artifacts(self.tmp / "out", tampered, self.manifest)
        self.assertEqual(list(self.tmp.iterdir()), [])


class Cli(unittest.TestCase):
    SECRET = "R10B-SENTINEL-PASSWORD"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r10b-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.req = self.tmp / "request.json"
        self.req.write_text(json.dumps(request()))

    def run_cli(self, *args, dsn=None):
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
        if dsn is not None:
            env["DATABASE_URL"] = dsn
        return subprocess.run([sys.executable, "-B", str(BACKEND / "scripts" / "research_dataset.py"), *args],
                              capture_output=True, text=True, cwd=self.tmp, env=env, timeout=60)

    def dsn(self):
        return f"postgresql+asyncpg://r10b:{self.SECRET}@/nodb?host={self.tmp}/no-socket-here"

    def test_validate_only_needs_no_database(self):
        res = self.run_cli("--request", str(self.req), "--validate-only", dsn=self.dsn())
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["request_hash"], ds.parse_request(request()).request_hash())
        self.assertNotIn(self.SECRET, res.stdout + res.stderr)
        self.assertNotIn("DATABASE_URL", res.stderr)

    def test_usage_and_request_errors_before_database(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(dict(request(), extra=1)))
        nan = self.tmp / "nan.json"
        nan.write_text(json.dumps(request()).replace('"fee_bps_per_side": 4.0', '"fee_bps_per_side": NaN'))
        (self.tmp / "exists").mkdir()
        cases = [
            (["--request", str(self.req)], 2),
            (["--request", str(self.req), "--read-db"], 2),
            (["--request", str(self.req), "--out-dir", str(self.tmp / "o")], 2),
            (["--request", str(self.req), "--validate-only", "--read-db", "--out-dir", str(self.tmp / "o")], 2),
            (["--request", str(bad), "--read-db", "--out-dir", str(self.tmp / "o")], 2),
            (["--request", str(nan), "--validate-only"], 2),
            (["--request", str(self.req), "--read-db", "--out-dir", str(self.tmp / "exists")], 2),
            (["--request", str(self.req), "--read-db", "--out-dir", str(BACKEND / "export-x")], 2),
        ]
        for args, code in cases:
            res = self.run_cli(*args, dsn=self.dsn())
            with self.subTest(args):
                self.assertEqual(res.returncode, code, res.stderr)
                self.assertNotIn(self.SECRET, res.stdout + res.stderr)
                self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["bad.json", "exists", "nan.json", "request.json"])
        self.assertFalse((BACKEND / "export-x").exists())

    def test_database_unavailable_is_generic_and_leaves_nothing(self):
        for dsn in (None, self.dsn()):
            res = self.run_cli("--request", str(self.req), "--read-db", "--out-dir", str(self.tmp / "out"), dsn=dsn)
            with self.subTest(dsn is None):
                self.assertEqual(res.returncode, 3, res.stderr)
                self.assertEqual(json.loads(res.stderr.strip().splitlines()[-1])["status"], "SOURCE_UNAVAILABLE")
                self.assertNotIn(self.SECRET, res.stdout + res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertFalse((self.tmp / "out").exists())
                self.assertEqual([p.name for p in self.tmp.iterdir()], ["request.json"])

    def test_no_dsn_argument_exists(self):
        source = (BACKEND / "scripts" / "research_dataset.py").read_text()
        tree = ast.parse(source)
        flags = [a.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant)]
        self.assertEqual(flags, ["--request", "--validate-only", "--read-db", "--out-dir"])


class Architecture(unittest.TestCase):
    SOURCE = (BACKEND / "services" / "research_dataset_service.py").read_text()

    def test_module_imports_are_limited(self):
        tree = ast.parse(self.SOURCE)
        top = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                top.add(node.module)
            elif isinstance(node, ast.Import):
                top.update(a.name for a in node.names)
        self.assertEqual(top - {"__future__", "dataclasses", "datetime", "hashlib", "json", "math",
                                "os", "pathlib", "tempfile", "typing"}, {"services"})
        lazy = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} - top
        self.assertEqual(lazy, {"sqlalchemy"})
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for forbidden in ("init_db", "create_all", "get_session", "load_dotenv", "main"):
            self.assertNotIn(forbidden, names | attrs)
        # `set.add`/`stream.flush` são legítimos; escrita de sessão é coberta pelo SQL.
        for forbidden in ("getenv", "environ", "now", "utcnow", "time", "commit", "merge", "execute_many"):
            self.assertNotIn(forbidden, attrs)

    def test_no_live_consumer_imports_exporter_or_r09_models(self):
        offenders = []
        for path in list((BACKEND / "services").glob("*.py")) + [BACKEND / "main.py"]:
            if path.name == "research_dataset_service.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "research_dataset" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])
        for name in ("learning_service", "symbol_learning_service", "edge_decay_service",
                     "rotation_service", "calibration_service", "risk_service"):
            text = (BACKEND / "services" / f"{name}.py").read_text()
            self.assertNotIn("decision_observation", text, name)
            self.assertNotIn("rejected_setup", text, name)

    def test_sql_tables_match_models(self):
        from models.decision_observation import DecisionObservation, RejectedSetupObservation
        self.assertEqual(ds.REJECTED_TABLE, RejectedSetupObservation.__tablename__)
        self.assertEqual(ds.OPPORTUNITY_TABLE, DecisionObservation.__tablename__)
        self.assertEqual(ds.REPLAY_KEYS, __import__("services.decision_observation_service",
                                                    fromlist=["x"]).REPLAY_CONFIG_KEYS)

    def test_frozen_r10a_and_r09_contracts_untouched(self):
        res = subprocess.run(["git", "diff", "--name-only", "51c992c2", "--",
                              "backend/services/offline_replay_service.py",
                              "backend/services/decision_observation_service.py",
                              "backend/models/decision_observation.py",
                              "backend/scripts/research_replay.py"],
                             cwd=BACKEND.parent, capture_output=True, text=True)
        if res.returncode != 0:
            self.skipTest("baseline 51c992c2 indisponível neste checkout")
        self.assertEqual(res.stdout.strip(), "")

    def test_no_network(self):
        self.assertEqual(_NET, [])


if __name__ == "__main__":
    unittest.main()
