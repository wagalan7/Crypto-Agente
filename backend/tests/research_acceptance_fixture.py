"""TEST_ONLY technical evidence from the official engines, never a real study.

The price generator is fixed by bin before any engine runs. No accepted state,
probability, CI or economic metric is supplied to the production evaluator.
"""
import copy
from functools import lru_cache

from services import preselection_observation_service as pre
from services import research_dataset_service as ds
from services import research_dataset_scopes as scopes
from services import research_manifest_service as rm
from services import research_study_service as study
from services import score_trace_service as trace_service
from services import score_v3_calibration_service as calibration
from services import score_v3_service as score
from tests.test_lote02_manifest_binding import frozen_manifest
from tests.test_lote02_selection_scope import T0, BAR5, pedido_selecao


def graded_features(value):
    return dict(adx=15 + 20 * value, htf_alignment_ratio=value,
        structure_quality=value, level_distance_atr=3 - 2.8 * value,
        trigger_body_ratio=.2 + .6 * value, trigger_follow_through_atr=value,
        rr_tp2=1.8 + 2.2 * value, entry_distance_atr=1.5 * (1 - value),
        volume_ratio=.8 + 1.2 * value, spread_pct=.25 - .23 * value,
        funding_pct=.05 * (1 - 2 * value))


def acceptance_export(*, training=400, validation=600):
    body = {k: copy.deepcopy(v) for k, v in frozen_manifest().items()
            if k in rm.MANIFEST_FIELDS}
    body["hashes"] = None
    trace = trace_service.freeze_trace({"version": trace_service.VERSION,
        "formula_requested": "SCORE_V2", "formula_effective": "SCORE_V2",
        "config": {**{k: 1.0 for k in trace_service.CONFIG_NUMBERS},
            "high_tf_patterns_enabled": False, "high_tf_confirm_enabled": False}})
    body["baseline"]["score_config_hash"] = rm.observed_baseline_hash(trace)
    for side in ("baseline", "candidate"):
        body[side]["playbooks"] = ["TREND_PULLBACK"]
    body["candidate"]["selection_rule"]["min_score"] = 70.0
    body["split"].update(validation_start_ms=T0 + 5000 * BAR5,
        holdout_start_ms=T0 + 11000 * BAR5, as_of_ms=T0 + 12000 * BAR5)
    manifest = rm.parse_manifest(body)
    raw_request = pedido_selecao(scope=scopes.SCOPE_PRE_POPULATION)
    raw_request["split"] = {k: manifest["split"][k] for k in ds.SPLIT_KEYS} | {
        "embargo_bars": manifest["split"]["embargo_bars"]}
    raw_request["as_of_utc"] = ds.ms_datetime(manifest["split"]["as_of_ms"]).isoformat()
    raw_request["candidate"]["selection"] = rm.selection_config_of(manifest["candidate"])
    request = ds.parse_request(raw_request)
    source, outcomes = [], {}
    for idx in range(training + validation):
        group, occurrence = idx % 4, idx // 4
        value = (.65, .75, .85, .95)[group]
        val_idx = idx - training
        offset = (10 + idx * 5 if idx < training else
                  5010 + (val_idx // 100) * 1000 + (val_idx % 100) * 9)
        stamp, key = T0 + offset * BAR5, "pre-" + study.digest("acceptance-%04d" % idx)[:40]
        feats = graded_features(value)
        assert score.score(feats, playbook="TREND_PULLBACK", side="long")["score"] // 10 == (6, 7, 8, 9)[group]
        # Known synthetic law, not an inferred or fitted probability.
        outcomes[key] = occurrence % 10 < (0, 2, 8, 9)[group]
        setup = dict(symbol="SYN/USDT:USDT", timeframe="1h", side="long",
            playbook="CHAMPION_LEGACY", playbook_version="SCORE_V2",
            trigger_candle_ms=stamp - 3600000, entry=100., stop_loss=90.,
            tp1=108., tp2=110., atr=1.)
        frozen = pre.frozen_decision(identity=key, outcome="ACCEPTED", decision_ts_ms=stamp,
            setup=setup, funnel=pre.record_funnel([]), availability={},
            source={"decision_source": "TEST_ONLY_SCANNER"},
            config={"schema_version": "r09.pre.v2", "formula_effective": "SCORE_V2", "score": trace["config"]},
            score_trace=trace, features=feats, evaluation={"timeframes_evaluated": ["1h"]},
            observed_decision_scope="FINAL_SCANNER_SELECTION",
            feature_evidence={"version": "R13_POINT_IN_TIME_FEATURES_V1", "quality": "OBSERVED",
                "observed_at_ms": stamp, "candle_close_ms": stamp})
        source.append(dict(opportunity_key=key, symbol=setup["symbol"], decision_at=ds.ms_datetime(stamp),
            opportunity_scope="PRE_SELECTION", frozen_setup=setup,
            frozen_config={"r09_pre_selection": frozen}, score_trace=trace,
            candles=None, candles_malformed=False))
    plan = ds.plan_selection(request, [(r["opportunity_key"], r["decision_at"]) for r in source])
    dataset, exported = ds.build_feature_artifacts(request, plan, source, {"holdout_sealed": 17})
    windows, quotes = {}, {}
    for row in dataset["rows"]:
        key, stamp = row["opportunity_key"], row["decision_ts_ms"]
        first = ((stamp + BAR5 - 1) // BAR5) * BAR5
        win = outcomes[key]
        windows[key] = [dict(timestamp_ms=first + j * BAR5,
            open=100. if j == 0 else (110. if win else 90.),
            high=100.5 if j == 0 else (111. if win else 100.),
            low=99.8 if j == 0 else (100. if win else 89.),
            close=100.2 if j == 0 else (110. if win else 90.), volume=100.)
            for j in range(request.horizon_bars)]
        quotes[key] = dict(bid=99.99, ask=100.01, ts_ms=stamp, source="SYNTHETIC_TEST_ONLY")
    prices = study.build_price_contract(dataset_hash=study.digest(dataset),
        source="SYNTHETIC_TEST_ONLY", bar_ms=BAR5, as_of_ms=manifest["split"]["as_of_ms"],
        windows=windows, quotes=quotes, test_only=True)
    return manifest, dataset, exported, prices


@lru_cache(maxsize=1)
def _report():
    manifest, dataset, exported, prices = acceptance_export()
    return study.run_study(manifest=manifest, dataset=dataset, export_manifest=exported,
        prices=prices, request=study.calibration_request(event=calibration.EVENT_TP1,
            valid_for_ms=30 * 86400000), now_ms=manifest["split"]["as_of_ms"])


def accepted_calibration_report():
    """Fresh copy: fitting/replay actually run; economic runtime may be missing."""
    return copy.deepcopy(_report())
