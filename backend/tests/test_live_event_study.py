"""Official event-family integration; inputs are synthetic, not a study approval."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from services import live_event_study_service as study
from services import live_management_replica_service as manager
from services import research_manifest_service as manifests
from services import research_study_service as official
from tests.test_live_event_execution import complete_entry_request
from tests.test_live_management_replica import config as manager_config, state, tick, request as bound_manager
from tests.test_lote02_research_manifest import manifesto, champion_trace
from tests.test_lote02_selection_scope import features

NOW = 2_000_000
ENTRY = 1_000_000
EXIT = 1_060_001


def fixture():
    entry = complete_entry_request()
    cfg = manager_config(TIME_STOP_ENABLED=True, TIME_STOP_DAY_MIN=1, PRE_TP1_PROTECT_ENABLED=False)
    descriptor = {"engine_version": manager.ENGINE_VERSION, "source_fingerprint": manager.source_fingerprint(),
                  "config": cfg, "config_hash": study.digest(cfg)}
    manifest = manifesto()
    manifest.update(manifest_version=study.MANIFEST_VERSION,
        costs={"source": "TEST_ONLY", "unit": "SETTLEMENT_ASSET_ABSOLUTE", "asset": "USDT",
               "model_fingerprint": study.ledger.model_fingerprint()},
        split={"train_start_ms": 10, "validation_start_ms": 900_000, "holdout_start_ms": 3_000_000,
               "as_of_ms": NOW, "bar_ms": 1000, "horizon_ms": 120_000, "purge_ms": 120_000,
               "embargo_ms": 0, "censoring": "EXPIRES_AFTER_HORIZON_WITHOUT_RESOLUTION"},
        shared={"management": descriptor, "execution_config": entry["config"], "initial_portfolio": entry["portfolio"]})
    for side in ("baseline", "candidate"):
        manifest[side]["management_config"] = copy.deepcopy(descriptor)
    manifest["population"]["min_rows"] = 1
    manifest = study.parse_manifest(manifest)
    context = {"account_scope": "TEST_ONLY_ACCOUNT", "exchange": "binance", "market": "usdm_futures",
        "symbol": "BTCUSDT", "quote": "USDT", "side": "LONG", "position_side": "BOTH", "trade_id": "1",
        "opportunity_id": "opp-1", "dispatch_id": "cw-test-entry-1", "planned_stop": "95"}
    initial = state(id=1, symbol="BTCUSDT", qty=2., qty_initial=2., entry_price=100.,
        opened_at_ms=ENTRY, planned_stop=95., planned_tp1=110., planned_tp2=120.,
        sl_current_price=95., sl_order_id="sl-1", tp1_order_id="tp1-1", tp2_order_id="tp2-1",
        client_order_id="cw-test-entry-1", timeframe="1h")
    pos = {"ok": True, "positions": [{"symbol": "BTCUSDT", "side": "Buy", "size": 2.,
                                   "entry_price": 100., "position_side": "BOTH"}]}
    empty = {"ok": True, "positions": []}
    trace = bound_manager(cfg=cfg, initial=initial, ticks=[tick(at_ms=EXIT,
        ownership_guard=[{"allowed": True}], get_positions=[pos, empty, empty],
        place_order=[{"ok": True, "result": {"orderId": "exit-1"}}],
        cleanup_orphan_conditionals=[{"confirmed_absent": True}], close_trade=[{"applied": True}])])
    binding = {k: v for k, v in context.items() if k != "planned_stop"}
    accounting = {"contract_version": study.ledger.INPUT_VERSION, "test_only": True, "provenance": "TEST_ONLY",
        "source_fingerprint": "e" * 64, "model": None, "orders": [], "fills": [], "funding": [],
        "fee_conversions": [], "slippage": [], "coverage": {}}
    for i, (oid, role, stamp, price, pnl, fee, coid, side) in enumerate([
        ("entry-1", "ENTRY", ENTRY, 100, 0, 0, "cw-test-entry-1", "BUY"),
        ("exit-1", "TIME_STOP", EXIT, 102, 4, .2, "cw-ts-1", "SELL")]):
        bind = {**binding, "order_id": oid, "dispatch_id": coid}
        header = {"event_id": f"order-event-{i}", "causal_index": 2*i, "observed_at_ms": stamp,
                  "available_at_ms": stamp, "binding": bind, "role": role}
        accounting["orders"].append({**copy.deepcopy(header), "raw": {"orderId": oid, "clientOrderId": coid,
            "status": "FILLED", "symbol": "BTCUSDT", "positionSide": "BOTH", "side": side,
            "executedQty": 2, "avgPrice": price, "reduceOnly": role != "ENTRY", "type": "MARKET", "updateTime": stamp}})
        fill = {**copy.deepcopy(header), "event_id": f"fill-{i+1}", "causal_index": 2*i+1,
            "raw": {"id": f"fill-{i+1}", "orderId": oid, "symbol": "BTCUSDT", "positionSide": "BOTH",
                "side": side, "price": price, "qty": 2, "realizedPnl": pnl, "commission": fee,
                "commissionAsset": "USDT", "time": stamp}}
        accounting["fills"].append(fill)
    source = {"source": "TEST_ONLY", "source_fingerprint": "e"*64,
        **{k: context[k] for k in ("account_scope", "exchange", "market", "symbol", "quote", "position_side", "trade_id")},
        "window_start_ms": ENTRY, "window_end_ms": EXIT,
        "exposure_intervals": [{"trade_id": "1", "side": "LONG", "position_side": "BOTH",
                               "opened_at_ms": ENTRY, "closed_at_ms": EXIT}]}
    accounting["coverage"] = {"fills_complete": True, "funding_complete": True, "position_flat": True,
        "window_start_ms": ENTRY, "window_end_ms": EXIT, "available_at_ms": EXIT,
        "source_ref": source, "exclusive_exposure": True}
    # Bound exact LIVE caller request. No order↔algo correlation is needed for
    # this time-stop normal order; conditional mapping has its own negatives.
    management_report = manager.run_trace(trace)
    action = next(a for a in management_report["actions"] if a["port"] == "place_order")
    record = {"opportunity_key": "opp-1", "steps": [{"kind": "EXECUTION", "at_ms": ENTRY, "request": entry},
        {"kind": "MANAGEMENT", "at_ms": EXIT, "request": trace}], "context": context, "accounting": accounting,
        "order_links": [{"order_id": "exit-1", "dispatch_id": "cw-ts-1", "role": "TIME_STOP", "step": 1,
                         "call_index": action["call_index"], "algo_id": None, "correlation": None}]}
    row = {"opportunity_key": "opp-1", "decision_ts_ms": ENTRY, "available_at_ms": ENTRY, "symbol": "BTC/USDT",
        "side": "long", "observed_outcome": "ACCEPTED", "observed_decision_scope": "FINAL_SCANNER_SELECTION",
        "score_trace": champion_trace(), "features": features(), "atr": 2, "entry": 100, "stop_loss": 95,
        "tp1": 110, "tp2": 120, "funnel": None}
    dataset = {"version": study.DATASET_VERSION, "population_source": study.POPULATION_SOURCE,
        "manifest_hash": manifest["manifest_hash"], "index": [{"opportunity_key": "opp-1", "decision_ts_ms": ENTRY, "causal_index": 1}],
        "rows": [row], "events": {"baseline": [record], "candidate": [copy.deepcopy(record)]}, "dataset_hash": None}
    return manifest, study.validate_dataset(dataset, manifest)


def report_fixture():
    manifest, dataset = fixture()
    return study.run_study(manifest=manifest, dataset=dataset, now_ms=NOW)


class EventIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.dataset = fixture()
        cls.result = study.run_study(manifest=cls.manifest, dataset=cls.dataset, now_ms=NOW)

    def test_complete_real_callers_produce_net_from_fills_not_intentions(self):
        r = self.result
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["evidence"]["ok"], r)
        self.assertEqual(r["state"], "LOCAL_EVENT_EVIDENCE", r["arms"])
        for arm in r["arms"].values():
            record = arm["records"][0]
            self.assertEqual(record["state"], "COMPLETE", record)
            self.assertEqual(float(record["ledger"]["net_including_funding"]), 3.8)
            self.assertEqual(record["management"]["economics"], None)
            self.assertFalse(record["ledger"]["observed_money"])
            self.assertEqual(arm["economics"][0]["net_r"], .38)
            self.assertEqual(arm["portfolio_at_last_decision"]["equity_usd"], 1000)
            self.assertEqual(arm["portfolio_at_last_decision"]["available_margin_usd"], 760)
        self.assertFalse(r["promotable"])
        self.assertEqual(r["holdout_status"], "SEALED")

    def test_shared_config_change_and_bad_schema_refuse(self):
        body = {k: copy.deepcopy(v) for k,v in self.manifest.items() if k != "manifest_hash"}
        body["hashes"] = None
        body["candidate"]["management_config"]["config"]["RUNNER_ENABLED"] = True
        with self.assertRaisesRegex(ValueError, "MANAGEMENT_MUST_BE_IDENTICAL"):
            study.parse_manifest(body)
        body = copy.deepcopy(self.manifest)
        body["approved"] = True
        self.assertFalse(study.verify_manifest(body)["ok"])

    def test_temporal_purge_and_test_source_never_promote(self):
        body = {k: copy.deepcopy(v) for k,v in self.manifest.items() if k != "manifest_hash"}
        body["hashes"] = None
        body["split"]["purge_ms"] = 1
        with self.assertRaisesRegex(ValueError, "PURGE_OR_UNITS"):
            study.parse_manifest(body)
        body["split"] = copy.deepcopy(self.manifest["split"])
        body["decision"]["state"] = "APPROVED"
        with self.assertRaisesRegex(ValueError, "TEST_SOURCE_CANNOT_BECOME_REAL"):
            study.parse_manifest(body)

    def test_holdout_details_and_future_receipts_not_opened(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        data["index"].append({"opportunity_key": "sealed", "decision_ts_ms": 3_000_000, "causal_index": 2})
        data["events"]["baseline"].append({"opportunity_key": "sealed"})
        with self.assertRaisesRegex(ValueError, "HOLDOUT"):
            study.validate_dataset(data, self.manifest)
        data = copy.deepcopy(self.dataset)
        data["events"]["baseline"][0]["steps"][0]["request"]["receipts"][0]["available_at_ms"] = NOW + 1
        with self.assertRaisesRegex(ValueError, "FUTURE_DETAIL"):
            study.validate_dataset(data, self.manifest)

    def test_preholdout_trade_does_not_grant_postcut_outcome_access(self):
        manifest = {k: copy.deepcopy(v) for k,v in self.manifest.items() if k != "manifest_hash"}
        manifest["hashes"] = None
        manifest["split"]["holdout_start_ms"] = ENTRY + 30_000
        manifest = study.parse_manifest(manifest)
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        data["manifest_hash"] = manifest["manifest_hash"]
        with self.assertRaisesRegex(ValueError, "FUTURE_STEP"):
            study.validate_dataset(data, manifest)

    def test_duplicate_causal_indices_refuse_before_ordering(self):
        data = copy.deepcopy(self.dataset)
        data["index"].append({"opportunity_key": "another", "decision_ts_ms": ENTRY, "causal_index": 1})
        with self.assertRaisesRegex(ValueError, "CAUSAL_INDEX_AMBIGUOUS"):
            study.validate_dataset(data, self.manifest)

    def test_read_verification_never_runs_engines(self):
        with patch.object(study.execution, "replay_execution", side_effect=AssertionError("executor on GET")), \
             patch.object(study.evidence, "evaluate_evidence", side_effect=AssertionError("fit on GET")):
            self.assertTrue(study.verify_report(self.result)["ok"])

    def test_recompute_refuses_rehashed_money(self):
        r = copy.deepcopy(self.result)
        r["arms"]["candidate"]["economics"][0]["net_r"] = 8
        r["report_hash"] = study.digest({k:v for k,v in r.items() if k != "report_hash"})
        self.assertFalse(study.verify_report(r, recompute=True)["ok"])

    def test_ledger_entry_price_cannot_diverge_from_execution(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        data["events"]["candidate"][0]["accounting"]["fills"][0]["raw"]["price"] = 101
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        self.assertIn("EVENT_EXECUTION_LEDGER_FILL_MISMATCH", result["technical_gaps"])
        self.assertIsNone(result["arms"]["candidate"]["economics"][0]["net_r"])

    def test_official_dispatch_and_legacy_family_not_aliased(self):
        result = official.run_study(manifest=self.manifest, dataset=self.dataset, export_manifest=None, now_ms=NOW)
        self.assertEqual(result, self.result)
        self.assertTrue(manifests.verify_manifest(self.manifest)["ok"])
        self.assertFalse(official.run_study(manifest=self.manifest, dataset=self.dataset,
            export_manifest=None, now_ms=NOW, prices={})["ok"])

    def test_catalog_type_separate_and_no_operational_acceptance(self):
        from services import preselection_experiment_service as catalog, research_acceptance_service as acceptance
        cfg = study.build_event_envelope(self.result)
        self.assertTrue(catalog.validate_preselection_envelope(cfg)["ok"])
        cfg["experiment_type_version"] = catalog.TYPE_VERSION
        self.assertFalse(catalog.validate_preselection_envelope(cfg)["ok"])
        self.assertFalse(acceptance.verify_acceptance(self.result, now_ms=NOW, purpose="PROMOTION")["ok"])

    def test_event_catalogue_never_enters_legacy_or_operational_loader(self):
        from types import SimpleNamespace
        from services import strategy_evidence_service as catalog
        cfg = study.build_event_envelope(self.result)
        verdict = catalog.verify_study_identity({}, candidate_config=cfg, fingerprint=self.result["identity"]["dataset_hash"], cutoff=None)
        self.assertEqual(verdict["reason_code"], "EVENT_RESEARCH_REQUIRES_VERSIONED_STUDY_VERIFIER")
        verdict = catalog._frozen_study_of(SimpleNamespace(candidate_config=cfg, status="ELIGIBLE", offline_metrics={}))
        self.assertEqual(verdict["reason_code"], "EVENT_RESEARCH_LIFECYCLE_NOT_RATIFIED")

    def test_catalog_type_structure_check_has_no_runtime_io(self):
        from services import preselection_experiment_service as catalog
        cfg = study.build_event_envelope(self.result)
        with patch.object(study, "verify_manifest", side_effect=AssertionError("runtime on type check")), \
             patch.object(manager, "source_fingerprint", side_effect=AssertionError("file read on type check")):
            verdict = catalog.validate_preselection_envelope(cfg)
        self.assertTrue(verdict["ok"])
        self.assertTrue(verdict["structural_only"])
        self.assertFalse(verdict["promotable"])
        cfg["event_manifest"]["shared"]["initial_portfolio"]["equity_usd"] = 999999
        self.assertFalse(catalog.validate_preselection_envelope(cfg)["ok"])
        self.assertEqual(self.result["manifest"]["shared"]["initial_portfolio"]["equity_usd"], 1000)

    def test_json_roundtrip_preserves_full_recomputation(self):
        body = json.loads(json.dumps(self.result, allow_nan=False))
        self.assertEqual(body, self.result)
        self.assertTrue(study.verify_report(body, recompute=True)["ok"])

    def test_unknown_entry_keeps_reservation_and_no_money(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        req = data["events"]["candidate"][0]["steps"][0]["request"]
        req = study.execution.make_execution_request(config=req["config"], proposal=req["proposal"],
            portfolio=req["portfolio"], receipts=req["receipts"], fills=[])
        data["events"]["candidate"][0]["steps"][0]["request"] = req
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        self.assertTrue(result["ok"], result)
        state = result["arms"]["candidate"]["portfolio_at_last_decision"]
        self.assertEqual((state["pending_count"], state["pending_risk_usd"], state["pending_margin_usd"]), (1, 10, 40))
        self.assertFalse(state["complete"])
        self.assertIsNone(result["arms"]["candidate"]["economics"][0]["net_r"])

    def test_downstream_failure_does_not_erase_proven_exposure(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        data["events"]["candidate"][0]["accounting"]["fills"][0]["raw"]["price"] = 101
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        state = result["arms"]["candidate"]["portfolio_at_last_decision"]
        self.assertEqual((state["open_positions"], state["open_risk_usd"], state["available_margin_usd"]), (1, 10, 760))
        self.assertFalse(state["complete"])

    def test_economic_r_uses_total_with_funding_exactly_once(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        for arm in ("baseline", "candidate"):
            record = data["events"][arm][0]
            binding = {k:v for k,v in record["context"].items() if k != "planned_stop"}
            record["accounting"]["funding"] = [{"event_id": "fund-1", "causal_index": 20,
                "observed_at_ms": EXIT, "available_at_ms": EXIT, "binding": binding, "role": "FUNDING",
                "raw": {"incomeType": "FUNDING_FEE", "tranId": "fund-1", "income": "-.5",
                    "asset": "USDT", "symbol": "BTCUSDT", "time": EXIT}}]
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        for arm in result["arms"].values():
            self.assertEqual(float(arm["records"][0]["ledger"]["net_trade"]), 3.8)
            self.assertEqual(float(arm["records"][0]["ledger"]["net_including_funding"]), 3.3)
            self.assertEqual(arm["economics"][0]["net_r"], .33)

    def test_exit_fill_cannot_precede_its_actual_order_action(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        for group, clock in (("fills", "time"), ("orders", "updateTime")):
            data["events"]["candidate"][0]["accounting"][group][1]["raw"][clock] = ENTRY + 1000
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        self.assertIn("EVENT_EXIT_EXECUTED_BEFORE_ACTION", result["technical_gaps"])

    def test_exit_financial_qty_cannot_exceed_actual_manager_request(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        record = data["events"]["candidate"][0]
        old = record["steps"][1]["request"]
        ticks = copy.deepcopy(old["ticks"])
        ticks[0]["ports"]["get_positions"][0]["value"]["positions"][0]["size"] = 1.
        record["steps"][1]["request"] = bound_manager(cfg=old["config"], initial=old["initial_state"], ticks=ticks)
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        self.assertIn("EVENT_EXIT_QUANTITY_EXCEEDS_REQUEST", result["technical_gaps"])
        self.assertIsNone(result["arms"]["candidate"]["economics"][0]["net_r"])
        self.assertEqual(result["arms"]["candidate"]["portfolio_at_last_decision"]["open_risk_usd"], 10)

    def test_backdated_manager_seed_refused_without_running_it(self):
        data = copy.deepcopy(self.dataset)
        data["dataset_hash"] = None
        request = data["events"]["candidate"][0]["steps"][1]["request"]
        request["initial_state"]["opened_at_ms"] = ENTRY - 1
        request["trace_hash"] = manager.digest({k:v for k,v in request.items() if k != "trace_hash"})
        result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
        self.assertIn("EVENT_MANAGER_BACKDATED_ENTRY", result["technical_gaps"])

    def test_same_millisecond_does_not_unlock_future_money(self):
        for decision_at, expected_equity, expected_margin, expected_slots in (
                (EXIT, 1000, 760, 1), (EXIT + 1, 1003.8, 803.8, 0)):
            data = copy.deepcopy(self.dataset)
            data["dataset_hash"] = None
            row = {**copy.deepcopy(data["rows"][0]), "opportunity_key": "opp-2", "decision_ts_ms": decision_at,
                "available_at_ms": decision_at, "observed_outcome": "VETOED"}
            data["rows"].append(row)
            data["index"].append({"opportunity_key": "opp-2", "decision_ts_ms": decision_at, "causal_index": 2})
            result = study.run_study(manifest=self.manifest, dataset=data, now_ms=NOW)
            self.assertTrue(result["ok"], result)
            state = result["arms"]["baseline"]["portfolio_at_last_decision"]
            self.assertAlmostEqual(state["equity_usd"], expected_equity)
            self.assertAlmostEqual(state["available_margin_usd"], expected_margin)
            self.assertEqual(state["open_positions"], expected_slots)

    def test_official_export_attach_is_distinct_and_never_backfills(self):
        from services import research_dataset_service as dataset_service
        raw = {"scope": "R09_PRE_SELECTION_POPULATION", "rows": [{**self.dataset["rows"][0],
            "source_schema": "r09.pre.v2", "feature_evidence": {"quality": "FRESH", "observed_at_ms": ENTRY}}]}
        exported = {"source": {"scope": "R09_PRE_SELECTION_POPULATION"},
            "holdout": {"policy": "SEALED", "details_read": False}, "cutoff": {"as_of_ms": NOW},
            "counts": {"excluded": {}}, "fingerprints": {"dataset_sha256": hashlib.sha256(dataset_service.canonical_bytes(raw)).hexdigest()}}
        attached = dataset_service.attach_event_dataset(dataset=raw, export_manifest=exported, research_manifest=self.manifest,
            events=self.dataset["events"], index=self.dataset["index"])
        self.assertEqual(attached["version"], study.DATASET_VERSION)
        self.assertNotIn("outcome", attached["rows"][0])
        exported["counts"]["excluded"]["INVALID_SETUP"] = 1
        with self.assertRaisesRegex(ValueError, "RAW_EXCLUDED_IDENTITIES_NOT_EXPORTED"):
            dataset_service.attach_event_dataset(dataset=raw, export_manifest=exported, research_manifest=self.manifest,
                events=self.dataset["events"], index=self.dataset["index"])


if __name__ == "__main__":
    unittest.main()
