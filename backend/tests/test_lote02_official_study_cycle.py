"""Export real → seleção/replay/folds/fitting/artefato; só mercado sintético.

Sem campo added observed_outcome no consumidor; nenhum dado/credencial real.
"""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from services import research_dataset_service as ds
from services import research_dataset_scopes as scopes
from services import research_manifest_service as rm
from services import research_study_service as study
from services import score_v3_calibration_service as calibration
from services import preselection_observation_service as pre
from services import preselection_experiment_service as catalog
from services import strategy_evidence_service as evidence
from services import score_trace_service as trace_service
from tests.test_lote02_manifest_binding import frozen_manifest
from tests.test_lote02_selection_scope import features, pedido_selecao, T0, BAR5


def official_fixture(*, training=240, validation=80, return_source=False):
    raw = {k: copy.deepcopy(v) for k, v in frozen_manifest().items() if k in rm.MANIFEST_FIELDS}
    raw["hashes"] = None
    trace = trace_service.freeze_trace({"version": trace_service.VERSION,
        "formula_requested": "SCORE_V2", "formula_effective": "SCORE_V2",
        "config": {**{k: 1.0 for k in trace_service.CONFIG_NUMBERS},
                   "high_tf_patterns_enabled": False, "high_tf_confirm_enabled": False}})
    raw["baseline"]["score_version"] = "SCORE_V2"
    raw["baseline"]["score_config_hash"] = rm.observed_baseline_hash(trace)
    raw["split"].update(validation_start_ms=T0 + 2000 * BAR5,
                        holdout_start_ms=T0 + 3000 * BAR5,
                        as_of_ms=T0 + 4000 * BAR5)
    manifest = rm.parse_manifest(raw)
    request_raw = pedido_selecao(scope=scopes.SCOPE_PRE_POPULATION)
    request_raw["split"] = {k: manifest["split"][k] for k in ds.SPLIT_KEYS} | {"embargo_bars": manifest["split"]["embargo_bars"]}
    request_raw["as_of_utc"] = ds.ms_datetime(manifest["split"]["as_of_ms"]).isoformat()
    request_raw["candidate"]["selection"] = rm.selection_config_of(manifest["candidate"])
    request = ds.parse_request(request_raw)
    rows = []
    for idx in range(training + validation):
        offset = 10 + idx * 3 if idx < training else 2020 + (idx - training) * 10
        stamp, key = T0 + offset * BAR5, "pre-" + study.digest("official-%04d" % idx)[:40]
        setup = dict(symbol="SYN/USDT:USDT", timeframe="1h", side="long",
                     playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
                     trigger_candle_ms=stamp - 3600000,
                     entry=100., stop_loss=99., tp1=103., tp2=106., atr=1.)
        payload = pre.frozen_decision(identity=key, outcome="ACCEPTED" if idx % 3 else "VETOED",
            decision_ts_ms=stamp, setup=setup, funnel=pre.record_funnel([]),
            availability={}, source={"decision_source": "TEST_ONLY_SCANNER"},
            config={"schema_version": "r09.pre.v2", "formula_effective": "SCORE_V2", "score": trace["config"]},
            score_trace=trace,
            features=features(), evaluation={"timeframes_evaluated": ["1h"]},
            observed_decision_scope="FINAL_SCANNER_SELECTION",
            feature_evidence={"version": "R13_POINT_IN_TIME_FEATURES_V1", "quality": "OBSERVED",
                              "observed_at_ms": stamp, "candle_close_ms": stamp})
        rows.append({"opportunity_key": key, "symbol": setup["symbol"],
                     "decision_at": ds.ms_datetime(stamp), "opportunity_scope": "PRE_SELECTION",
                     "frozen_setup": setup, "frozen_config": {"r09_pre_selection": payload}, "score_trace": trace,
                     "candles": None, "candles_malformed": False})
    plan = ds.plan_selection(request, [(r["opportunity_key"], r["decision_at"]) for r in rows])
    dataset, exported = ds.build_feature_artifacts(request, plan, rows, {"holdout_sealed": 17})
    horizon = request.horizon_bars
    windows, quotes = {}, {}
    for row in dataset["rows"]:
        key, stamp = row["opportunity_key"], row["decision_ts_ms"]
        first = ((stamp + BAR5 - 1) // BAR5) * BAR5
        windows[key] = [dict(timestamp_ms=first + j * BAR5,
                            open=100. if j == 0 else 106., high=100.5 if j == 0 else 107.,
                            low=99.8 if j == 0 else 100.0, close=100.2 if j == 0 else 106., volume=100.)
                        for j in range(horizon)]
        quotes[key] = dict(bid=99.99, ask=100.01, ts_ms=stamp, source="SYNTHETIC_TEST_ONLY")
    prices = study.build_price_contract(dataset_hash=study.digest(dataset), source="SYNTHETIC_TEST_ONLY",
        bar_ms=BAR5, as_of_ms=manifest["split"]["as_of_ms"], windows=windows, quotes=quotes, test_only=True)
    if return_source:
        return manifest, dataset, exported, prices, request, rows
    return manifest, dataset, exported, prices


class OfficialStudyCycle(unittest.TestCase):
    def setUp(self):
        self.net = patch("socket.getaddrinfo", side_effect=AssertionError("DNS prohibited"))
        self.net.start()
        self.addCleanup(self.net.stop)

    def test_official_export_keeps_baseline_and_unknown_price_is_not_zero(self):
        m, dataset, exported, prices = official_fixture(training=3, validation=2)
        self.assertEqual({r["observed_outcome"] for r in dataset["rows"]}, {"ACCEPTED", "VETOED"})
        self.assertTrue(all(r["outcome"] is None and r["candles"] is None for r in dataset["rows"]))
        self.assertTrue(study.verify_export(dataset, exported, m)["ok"])
        self.assertFalse(exported["holdout"]["details_read"])

    def test_export_tampering_scope_split_costs_and_cutoff_refused(self):
        m, data, exported, _ = official_fixture(training=2, validation=1)
        for field, value in (("observed_outcome", "BAD"), ("features", {"adx": 99.})):
            bad = copy.deepcopy(data)
            bad["rows"][0][field] = value
            self.assertFalse(study.verify_export(bad, exported, m)["ok"])
        for key, value in (("embargo_bars", 0), ("validation_start_ms", T0)):
            bad = copy.deepcopy(exported)
            bad["configs"]["split"][key] = value
            self.assertFalse(study.verify_export(data, bad, m)["ok"])

    def test_full_real_engines_fit_oos_and_catalog_binding_without_decoration(self):
        m, data, exported, prices = official_fixture()
        request = study.calibration_request(event=calibration.EVENT_NET_POSITIVE, valid_for_ms=86400000)
        report = study.run_study(manifest=m, dataset=data, export_manifest=exported, prices=prices,
                                 request=request, now_ms=m["split"]["as_of_ms"])
        self.assertTrue(report["ok"], report)
        self.assertFalse(report["real_study_allowed"])
        self.assertEqual(report["calibration"]["state"], calibration.STATE_OOS_VALIDATED)
        self.assertEqual(len(report["calibration"]["folds"]), 4)
        self.assertGreater(report["artifact"]["metrics"]["oos"]["predictions"], 0)
        self.assertGreater(report["candidate_replay"]["admitted"], 0)
        self.assertFalse(report["promotable"])
        contract = report["study"]["contract"]
        envelope = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
            contract_hash=contract["contract_hash"], selection_config=contract["selection_config"])
        verified = evidence.verify_study_identity(report["study"], candidate_config=envelope,
            fingerprint=study.digest(data), cutoff=ds.ms_datetime(m["split"]["as_of_ms"]))
        self.assertTrue(verified["ok"], verified)
        self.assertFalse(verified["real_study_allowed"])

    def test_missing_real_prices_never_falls_back_to_synthetic(self):
        m, data, exported, _ = official_fixture()
        with patch("scripts.research_pipeline.rising_bars", side_effect=AssertionError("synthetic fallback")):
            out = study.run_study(manifest=m, dataset=data, export_manifest=exported,
                                  now_ms=m["split"]["as_of_ms"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "PRICE_AND_QUOTE_WINDOWS_REQUIRED")

    def test_price_chronology_unknown_id_and_incomplete_horizon_block(self):
        m, data, exported, prices = official_fixture()
        for mutate in (lambda p: p["quotes"][data["rows"][0]["opportunity_key"]].update(ts_ms=m["split"]["as_of_ms"]),
                       lambda p: p["windows"].update(HOLDOUT=[]),
                       lambda p: p["windows"][data["rows"][0]["opportunity_key"]].pop()):
            bad = copy.deepcopy(prices)
            mutate(bad)
            bad["price_hash"] = study.digest({k: v for k, v in bad.items() if k != "price_hash"})
            result = study.run_study(manifest=m, dataset=data, export_manifest=exported, prices=bad,
                                     now_ms=m["split"]["as_of_ms"])
            self.assertFalse(result["ok"])

    def test_test_price_cannot_enter_real_manifest(self):
        m, data, exported, prices = official_fixture()
        body = {k: copy.deepcopy(v) for k, v in m.items() if k in rm.MANIFEST_FIELDS}
        body["hashes"] = None
        body["decision"]["state"] = rm.DECISION_APPROVED
        real = rm.parse_manifest(body)
        out = study.run_study(manifest=real, dataset=data, export_manifest=exported, prices=prices,
                             now_ms=real["split"]["as_of_ms"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason_code"], "TEST_PRICE_FOR_REAL_STUDY_FORBIDDEN")

    def test_request_hash_and_no_event_means_no_fitting(self):
        m, data, exported, prices = official_fixture()
        with patch.object(calibration, "fit_calibration", side_effect=AssertionError("unrequested fit")):
            out = study.run_study(manifest=m, dataset=data, export_manifest=exported, prices=prices,
                                 now_ms=m["split"]["as_of_ms"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["calibration"]["state"], "WAITING_DECISION")


class PersistedReadSafety(unittest.IsolatedAsyncioTestCase):
    async def test_read_error_is_not_no_artifact_or_fitting(self):
        class Broken:
            def __call__(self):
                raise RuntimeError("DB unavailable")
        result = await study.load_latest_study(Broken())
        self.assertFalse(result["available"])
        self.assertEqual(result["reason_code"], "CALIBRATION_STUDY_READ_ERROR")

    async def test_status_reads_persisted_artifact_and_never_calls_fit(self):
        from services import research_batch_service as batch
        import db
        with patch.object(db, "DB_ENABLED", True), \
             patch.object(study, "load_latest_study", new=AsyncMock(return_value={
                 "available": False, "artifact": None, "manifest": None,
                 "reason_code": "CALIBRATION_STUDY_READ_ERROR"})), \
             patch.object(calibration, "fit_calibration", side_effect=AssertionError("GET fit")):
            result = await batch.get_research_status()
        economic = result["lote_final"]["evidence"]["economic_validation"]
        self.assertEqual(economic["state"], "ERROR")
        self.assertEqual(economic["reason_code"], "CALIBRATION_STUDY_READ_ERROR")


class ReportContextSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m, cls.data, cls.exported, cls.prices = official_fixture()
        cls.now = cls.m["split"]["as_of_ms"]
        cls.report = study.run_study(manifest=cls.m, dataset=cls.data,
            export_manifest=cls.exported, prices=cls.prices,
            request=study.calibration_request(event=calibration.EVENT_NET_POSITIVE, valid_for_ms=86400000),
            now_ms=cls.now)

    def test_integrated_report_and_oos_net_payoff_verified(self):
        self.assertTrue(study.validate_study_report(self.report, now_ms=self.now)["ok"])
        ev = self.report["calibration"]["net_ev"]
        self.assertTrue(ev["available"])
        self.assertTrue(ev["costs_already_included"])
        self.assertEqual(ev["ev_r"], self.report["calibration"]["payoff_evidence"]["expected_net_payoff_r"])

    def test_valid_hash_does_not_authorize_other_event_source_or_prices(self):
        for mutate in (
            lambda a: a.update(event=calibration.EVENT_TP2),
            lambda a: a.update(source="OTHER_SOURCE"),
            lambda a: a["versions"].update(prices="a" * 64),
            lambda a: a["event_definition"].update(payoff_ref="b" * 64),
            lambda a: a["event_definition"].update(bar_ms=600000),
        ):
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(self.report)
                mutate(bad["artifact"])
                bad["artifact"]["artifact_hash"] = calibration.recompute_hash(bad["artifact"])
                bad["calibration"]["artifact"] = bad["artifact"]
                self.assertFalse(study.validate_study_report(bad, now_ms=self.now)["ok"])

    def test_expired_or_malformed_persisted_report_is_not_available(self):
        self.assertFalse(study.validate_study_report(self.report, now_ms=self.now + 86400001)["ok"])
        for bad in (None, [], {"ok": True}, {**self.report, "identity": None}):
            self.assertFalse(study.validate_study_report(bad, now_ms=self.now)["ok"])

    def test_target_event_never_uses_censored_cohort_as_full_management_ev(self):
        r = study.run_study(manifest=self.m, dataset=self.data, export_manifest=self.exported,
            prices=self.prices, request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=86400000),
            now_ms=self.now)
        self.assertTrue(study.validate_study_report(r, now_ms=self.now)["ok"])
        self.assertIsNone(r["calibration"]["net_ev"])
        self.assertIsNone(r["calibration"]["payoff_evidence"])
        self.assertEqual(r["calibration"]["net_ev_reason"], "TARGET_EVENT_COHORT_NOT_FULL_MANAGEMENT_PAYOFF")

    def test_horizon_without_observed_target_is_censored_not_false(self):
        rows = self.data["rows"]
        results = {r["opportunity_key"]: {"net_r": 0.1, "filled": True,
            "result_available_ts_ms": r["decision_ts_ms"] + 300000,
            "status": "CLOSED_TIME_STOP", "tp1_hit": False, "exits": []} for r in rows}
        r = study._calibrate(rows, results, self.m,
            request=study.calibration_request(event=calibration.EVENT_TP1, valid_for_ms=86400000),
            dataset_hash=study.digest(self.data), price_hash=self.prices["price_hash"], now_ms=self.now)
        self.assertEqual(r["excluded"]["TARGET_EVENT_CENSORED_AT_HORIZON"], len(rows))
        self.assertIsNone(r["artifact"])

    def test_export_baseline_management_must_match_declared_execution(self):
        bad = copy.deepcopy(self.data)
        bad["baseline_config"]["tp1_fraction"] = 0.1
        exported = copy.deepcopy(self.exported)
        exported["fingerprints"]["dataset_sha256"] = study.digest(bad)
        self.assertFalse(study.verify_export(bad, exported, self.m)["ok"])


if __name__ == "__main__":
    unittest.main()
