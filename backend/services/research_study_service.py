"""Lote 02: circuito offline sobre o EXPORT oficial, não sobre demo sintética.

Sem rede, exchange, scheduler ou promoção. Dados/preços são explícitos e
hasheados antes dos resultados; ausência nunca chama rising_bars. Persistência
reutiliza policy_simulation_state e seu CAS, em namespace de pesquisa isolado.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import math
import time
from typing import Mapping

STUDY_VERSION = "R13_OFFLINE_STUDY_V1"
PRICE_CONTRACT = "R13_OFFLINE_PRICE_WINDOW_V1"
PAYLOAD_KIND = "LOTE02_REGISTERED_STUDY"
CALIBRATION_REQUEST = "R13_CALIBRATION_REQUEST_V1"
FOLDS = 4


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _integer(value):
    return value if type(value) is int and value > 0 else None


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _blocked(reason, *, detail=None, state="WAITING_DATA"):
    return {"ok": False, "version": STUDY_VERSION, "state": state,
            "reason_code": reason, "detail": detail, "promotable": False,
            "live_changed": False, "holdout_status": "SEALED"}


def verify_export(dataset, export_manifest, manifest):
    """Confere o artefato R10B e o contrato que realmente delimitou a consulta."""
    from services import research_dataset_service as ds
    from services import research_dataset_scopes as scopes
    from services import research_manifest_service as rm
    if not isinstance(dataset, Mapping) or not isinstance(export_manifest, Mapping):
        return _blocked("OFFICIAL_DATASET_REQUIRED")
    if (dataset.get("exporter_schema") != ds.EXPORTER_SCHEMA
            or export_manifest.get("schema_version") != ds.EXPORTER_SCHEMA
            or (export_manifest.get("fingerprints") or {}).get("dataset_sha256") != digest(dataset)
            or export_manifest.get("holdout", {}).get("details_read") is not False
            or export_manifest.get("holdout", {}).get("policy") != "SEALED"):
        return _blocked("EXPORT_IDENTITY_MISMATCH", state="INVALID")
    split = manifest["split"]
    if export_manifest.get("cutoff", {}).get("as_of_ms") != split["as_of_ms"]:
        return _blocked("EXPORT_CUTOFF_MISMATCH", state="INVALID")
    if export_manifest.get("source", {}).get("scope") != manifest["population"]["scope_id"]:
        return _blocked("EXPORT_POPULATION_MISMATCH", state="INVALID")
    actual_split = (export_manifest.get("configs") or {}).get("split") or {}
    if any(actual_split.get(k) != split[k] for k in
           ("train_start_ms", "validation_start_ms", "holdout_start_ms", "purge_bars")):
        return _blocked("EXPORT_SPLIT_MISMATCH", state="INVALID")
    if manifest["comparison_scope"] == rm.SCOPE_SELECTION:
        if dataset.get("scope") != scopes.SCOPE_PRE_POPULATION:
            return _blocked("RAW_POPULATION_REQUIRED", state="INVALID")
        if (actual_split.get("embargo_bars") != split["embargo_bars"]
                or dataset.get("split") != {k: split[k] for k in
                    ("train_start_ms", "validation_start_ms", "holdout_start_ms", "purge_bars", "embargo_bars")}
                or dataset.get("candidate", {}).get("selection") != rm.selection_config_of(manifest["candidate"])
                or dataset.get("baseline_config") != {k: v for k, v in
                    manifest["baseline"]["management_config"].items() if k != "config_hash"}
                or dataset.get("as_of_ms") != split["as_of_ms"]
                or dataset.get("bar_ms") != manifest["baseline"]["management_config"]["bar_ms"]
                or dataset.get("costs") != {k: manifest["costs"]["config"][k] for k in
                    ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")}):
            return _blocked("EXPORT_EXECUTION_CONFIG_MISMATCH", state="INVALID")
        rows = dataset.get("rows")
    else:
        rows = [{**r, "opportunity_key": r.get("opportunity_id"),
                 "side": r.get("direction")}
                for r in dataset.get("opportunities", [])]
        expected_management = {k: v for k, v in manifest["baseline"]["management_config"].items()
                               if k != "config_hash"}
        if dataset.get("baseline_config") != expected_management:
            return _blocked("EXPORT_MANAGEMENT_MISMATCH", state="INVALID")
    if not isinstance(rows, list):
        return _blocked("EXPORT_ROWS_INVALID", state="INVALID")
    seen = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return _blocked("EXPORT_ROWS_INVALID", state="INVALID")
        key, stamp = row.get("opportunity_key"), _integer(row.get("decision_ts_ms"))
        if (not isinstance(key, str) or not key or key in seen or stamp is None
                or not split["train_start_ms"] <= stamp < min(split["holdout_start_ms"], split["as_of_ms"])):
            return _blocked("EXPORT_TEMPORAL_OR_IDENTITY_MISMATCH", state="INVALID")
        seen.add(key)
    return {"ok": True, "rows": rows, "dataset_hash": digest(dataset)}


def build_price_contract(*, dataset_hash, source, bar_ms, as_of_ms, windows, quotes,
                         test_only=False):
    """Construtor de arquivo explícito; não busca preço nem inventa quote.

    Fonte externa é DECLARADA pelo operador, não certificada pela corretora.
    SYNTHETIC_TEST_ONLY é aceito apenas com manifesto de engenharia TEST_ONLY.
    """
    body = {"contract": PRICE_CONTRACT, "dataset_hash": dataset_hash,
            "source": source, "bar_ms": bar_ms, "as_of_ms": as_of_ms,
            "test_only": test_only, "windows": windows, "quotes": quotes}
    return {**body, "price_hash": digest(body)}


def verify_prices(prices, *, rows, manifest, dataset_hash, now_ms, exported_keys=None):
    from services import offline_replay_service as replay
    from services import research_manifest_service as rm
    if not isinstance(prices, Mapping):
        return _blocked("PRICE_AND_QUOTE_WINDOWS_REQUIRED")
    keys = {"contract", "dataset_hash", "source", "bar_ms", "as_of_ms",
            "test_only", "windows", "quotes", "price_hash"}
    body = {k: v for k, v in prices.items() if k != "price_hash"}
    try:
        matches = digest(body) == prices.get("price_hash")
    except (ValueError, TypeError):
        matches = False
    bar = manifest["baseline"]["management_config"]["bar_ms"]
    split = manifest["split"]
    if (set(prices) != keys or not matches or prices.get("contract") != PRICE_CONTRACT
            or prices.get("dataset_hash") != dataset_hash or prices.get("bar_ms") != bar
            or prices.get("as_of_ms") != split["as_of_ms"] or type(prices.get("test_only")) is not bool):
        return _blocked("PRICE_CONTRACT_MISMATCH", state="INVALID")
    if prices["source"] not in ("SYNTHETIC_TEST_ONLY", "OPERATOR_HISTORICAL_OHLCV"):
        return _blocked("PRICE_SOURCE_NOT_IMPLEMENTED", state="INVALID")
    test = rm.manifest_state(manifest) == rm.STATE_TEST_ONLY
    if prices["source"] == "SYNTHETIC_TEST_ONLY" and not (test and prices["test_only"] is True):
        return _blocked("TEST_PRICE_FOR_REAL_STUDY_FORBIDDEN", state="INVALID")
    if prices["test_only"] is True and not test:
        return _blocked("TEST_PRICE_FOR_REAL_STUDY_FORBIDDEN", state="INVALID")
    windows, quotes = prices["windows"], prices["quotes"]
    if not isinstance(windows, Mapping) or not isinstance(quotes, Mapping):
        return _blocked("PRICE_WINDOWS_INVALID", state="INVALID")
    allowed = set(exported_keys) if exported_keys is not None else {r["opportunity_key"] for r in rows}
    if set(windows) - allowed or set(quotes) - allowed:
        return _blocked("PRICE_WINDOW_OUTSIDE_EXPORTED_POPULATION", state="INVALID")
    missing = []
    horizon = max(manifest[s]["management_config"]["entry_window_bars"]
                  + manifest[s]["management_config"]["max_holding_bars"] - 1
                  for s in ("baseline", "candidate"))
    for row in rows:
        key, stamp = row["opportunity_key"], row["decision_ts_ms"]
        bars, quote = windows.get(key), quotes.get(key)
        first = ((stamp + bar - 1) // bar) * bar
        boundary = split["validation_start_ms"] if stamp < split["validation_start_ms"] else split["holdout_start_ms"]
        end = first + horizon * bar
        if end + (split["purge_bars"] + split["embargo_bars"]) * bar > boundary:
            return _blocked("PRICE_WINDOW_CROSSES_PURGE_BOUNDARY", state="INVALID")
        if end > min(split["as_of_ms"], now_ms):
            missing.append(key)
            continue
        if not isinstance(bars, list) or len(bars) != horizon or not isinstance(quote, Mapping):
            missing.append(key)
            continue
        try:
            parsed = [replay.Candle(**dict(b)) for b in bars]
        except (TypeError, ValueError):
            return _blocked("PRICE_WINDOW_INVALID_CANDLE", state="INVALID")
        if [b.timestamp_ms for b in parsed] != list(range(first, end, bar)):
            return _blocked("PRICE_WINDOW_NOT_CONTIGUOUS", state="INVALID")
        bid, ask, ts = _number(quote.get("bid")), _number(quote.get("ask")), _integer(quote.get("ts_ms"))
        if bid is None or ask is None or bid <= 0 or ask < bid or ts is None or ts > stamp:
            return _blocked("POINT_IN_TIME_QUOTE_INVALID", state="INVALID")
    if missing:
        return _blocked("PRICE_WINDOW_INCOMPLETE", detail={"missing_rows": len(missing)})
    return {"ok": True, "windows": windows, "quotes": quotes,
            "price_hash": prices["price_hash"],
            "source_assurance": "TEST_ONLY" if test else "OPERATOR_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED"}


def calibration_request(*, event, valid_for_ms):
    from services import score_v3_calibration_service as calib
    if event not in calib.EVENTS or _integer(valid_for_ms) is None:
        raise ValueError("evento e duração explícitos obrigatórios")
    body = {"contract": CALIBRATION_REQUEST, "event": event,
            "valid_for_ms": valid_for_ms, "folds": FOLDS,
            "censoring": "CENSOR_UNRESOLVED_AT_HORIZON"}
    return {**body, "request_hash": digest(body)}


def _calibrate(rows, results, manifest, *, request, dataset_hash, price_hash, now_ms):
    from services import score_v3_calibration_service as calib
    from services import score_v3_service as score
    if request is None:
        return {"state": "WAITING_DECISION", "reason_code": "CALIBRATION_EVENT_AND_VALIDITY_REQUIRED", "artifact": None}
    try:
        expected = calibration_request(event=request["event"], valid_for_ms=request["valid_for_ms"])
    except (KeyError, TypeError, ValueError):
        return {"state": "INVALID", "reason_code": "CALIBRATION_REQUEST_INVALID", "artifact": None}
    if request != expected:
        return {"state": "INVALID", "reason_code": "CALIBRATION_REQUEST_INVALID", "artifact": None}
    cfg = score.ScoreConfig(**manifest["candidate"]["score_config"])
    playbook = manifest["candidate"]["selection_rule"]["playbook"]
    fingerprint = score.model_fingerprint(playbook=playbook, config=cfg)
    event = request["event"]
    labels, excluded = [], {}
    for row in rows:
        result = results.get(row["opportunity_key"], {})
        payload = score.score(row.get("features") or {}, playbook=playbook, side=row["side"], config=cfg)
        net, available = _number(result.get("net_r")), _integer(result.get("result_available_ts_ms"))
        if payload["state"] != score.STATE_OK or available is None or net is None or not result.get("filled"):
            reason = "SCORE_OR_LABEL_UNAVAILABLE"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        tp2 = any(x["reason"] == "TP2" for x in result["exits"])
        label = result["tp1_hit"] if event == calib.EVENT_TP1 else tp2 if event == calib.EVENT_TP2 else net > 0
        # Horizonte sem alvo/stop não é uma perda provada de TP1/TP2. O
        # evento líquido tem resultado terminal; os eventos de toque censuram.
        if event != calib.EVENT_NET_POSITIVE and not label and result.get("status") in (
                "CLOSED_TIME_STOP", "CLOSED_MAX_HOLD"):
            excluded["TARGET_EVENT_CENSORED_AT_HORIZON"] = excluded.get("TARGET_EVENT_CENSORED_AT_HORIZON", 0) + 1
            continue
        labels.append({"opportunity_key": row["opportunity_key"], "score": payload["score"],
                       "decision_ts_ms": row["decision_ts_ms"], "label_available_ts_ms": available,
                       "event": event, "label": bool(label), "net_r": net})
    split, config = manifest["split"], manifest["candidate"]["management_config"]
    start, stop = split["validation_start_ms"], min(split["holdout_start_ms"], split["as_of_ms"])
    width = (stop - start) // FOLDS
    if width <= 0:
        return {"state": "INVALID", "reason_code": "CALIBRATION_FOLDS_INVALID", "artifact": None}
    folds, last, payoff_evidence, payoff_ev = [], None, None, None
    management_hash = config["config_hash"]
    costs_hash = manifest["costs"]["config"]["config_hash"]
    for idx in range(FOLDS):
        lo, hi = start + idx * width, stop if idx == FOLDS - 1 else start + (idx + 1) * width
        cutoff = lo - (split["purge_bars"] + split["embargo_bars"]) * config["bar_ms"]
        train = [r for r in labels if r["decision_ts_ms"] < cutoff and r["label_available_ts_ms"] <= cutoff]
        oos = [r for r in labels if lo <= r["decision_ts_ms"] < hi and r["label_available_ts_ms"] <= min(hi, now_ms)]
        fitted = calib.fit_calibration(train, event=event, population=cfg.population,
            model_fingerprint=fingerprint, score_config_hash=fingerprint,
            horizon_bars=config["entry_window_bars"] + config["max_holding_bars"] - 1,
            bar_ms=config["bar_ms"], censoring=request["censoring"],
            payoff_ref=manifest["candidate"]["management_config"]["config_hash"],
            source="R10A_REPLAY_FROM_OFFICIAL_EXPORT", dataset_hash=dataset_hash,
            cutoff_ms=cutoff, generated_at_ms=now_ms, valid_until_ms=now_ms + request["valid_for_ms"],
            versions={"study": STUDY_VERSION, "manifest": manifest["manifest_hash"],
                      "prices": price_hash, "costs": costs_hash})
        validation = calib.validate_out_of_sample(fitted["artifact"], oos, now_ms=now_ms) if fitted.get("ok") else fitted
        artifact = validation.get("artifact") or fitted.get("artifact")
        folds.append({"fold": idx, "train_cutoff_ms": cutoff, "oos_start_ms": lo, "oos_end_ms": hi,
                      "train_rows": len(train), "oos_rows": len(oos),
                      "state": validation.get("state"), "reason_code": validation.get("reason_code"),
                      "artifact": artifact, "oos": validation.get("oos")})
        if validation.get("ok"):
            last = artifact
            # A amostra TP1/TP2 censura expirações; não representa o payoff
            # inteiro da gestão. EV líquido só usa a coorte NET_POSITIVE.
            if event == calib.EVENT_NET_POSITIVE:
                proof = calib.build_oos_payoff_evidence(
                    [{**r, "management_hash": management_hash, "dataset_hash": dataset_hash,
                      "costs_hash": costs_hash, "costs_included": True} for r in oos],
                    management_hash=management_hash, dataset_hash=dataset_hash, costs_hash=costs_hash,
                    event=event, cutoff_ms=cutoff, now_ms=min(hi, now_ms))
                payoff_evidence = proof.get("evidence")
                payoff_ev = score.net_ev_from_payoff(evidence=payoff_evidence,
                    management_hash=management_hash, dataset_hash=dataset_hash, costs_hash=costs_hash,
                    event=event, now_ms=now_ms)
    return {"state": last["state"] if last else "WAITING_DATA",
            "reason_code": calib.APPROVAL_DECISION_REQUIRED if last else calib.SAMPLE_INSUFFICIENT,
            "artifact": last, "folds": folds, "excluded": excluded,
            "payoff_evidence": payoff_evidence, "net_ev": payoff_ev,
            "net_ev_reason": "NET_POSITIVE_OOS_PAYOFF" if event == calib.EVENT_NET_POSITIVE else
                             "TARGET_EVENT_COHORT_NOT_FULL_MANAGEMENT_PAYOFF",
            "request": request, "holdout_status": "SEALED", "economically_approved": False}


def run_study(*, manifest, dataset, export_manifest, prices=None, request=None, now_ms=None):
    """Comparação registrada inteira: nunca substitui export por fixture/demonstração."""
    from services import research_manifest_service as rm
    from services import research_selection_service as selection
    from services import offline_replay_service as replay
    from services import portfolio_replay_service as portfolio
    from services import preselection_experiment_service as catalog
    from services import walk_forward_service as wf
    now_ms = int(time.time() * 1000) if now_ms is None else _integer(now_ms)
    if now_ms is None:
        return _blocked("STUDY_CLOCK_INVALID", state="INVALID")
    verified = rm.verify_manifest(manifest)
    authorized = rm.authorized_comparison(manifest)
    if not verified.get("ok") or not authorized.get("available"):
        return _blocked(authorized.get("reason_code"), state="WAITING_DECISION")
    manifest = verified["manifest"]
    if manifest["split"]["as_of_ms"] > now_ms:
        return _blocked("STUDY_CUTOFF_NOT_OBSERVED", state="WAITING_DATA")
    source = verify_export(dataset, export_manifest, manifest)
    if not source["ok"]:
        return source
    rows = source["rows"]
    if len(rows) < manifest["population"]["min_rows"]:
        return _blocked("PROSPECTIVE_SAMPLE_INSUFFICIENT", detail={"rows": len(rows), "required": manifest["population"]["min_rows"]})
    if manifest["comparison_scope"] == rm.SCOPE_SELECTION:
        compared = selection.compare_population(rows, manifest=manifest)
        if not compared.get("ok") or not compared.get("coverage", {}).get("sample_sufficient"):
            return _blocked(compared.get("reason_code") or "POINT_IN_TIME_FEATURE_SAMPLE_INSUFFICIENT", detail=compared.get("coverage"))
        allowed = {r["opportunity_key"] for r in compared["decisions"] if r.get("included")}
        rows = [r for r in rows if r["opportunity_key"] in allowed]
        sides = compared["selected"]
    else:
        sides = {"baseline": rows, "candidate": rows}
        compared = None
    price = verify_prices(prices, rows=rows, manifest=manifest, dataset_hash=source["dataset_hash"], now_ms=now_ms,
                          exported_keys={r["opportunity_key"] for r in source["rows"]})
    if not price["ok"]:
        return price
    configs = {s: replay.ReplayConfig(**{k: v for k, v in manifest[s]["management_config"].items() if k != "config_hash"})
               for s in ("baseline", "candidate")}
    costs = replay.CostConfig(**{k: manifest["costs"]["config"][k] for k in
                                ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")})
    # Congela a identidade dos inputs efetivamente executados ANTES do replay.
    identity = {"manifest_hash": manifest["manifest_hash"], "dataset_hash": source["dataset_hash"],
                "price_hash": price["price_hash"], "calibration_request": request,
                "baseline": configs["baseline"].manifest(), "candidate": configs["candidate"].manifest(),
                "costs": costs.manifest(), "split": manifest["split"]}
    study_key = digest(identity)
    executions = {s: portfolio.run_portfolio(selection.replay_candidates(sides[s]),
                    bars_by_id=price["windows"], quotes_by_id=price["quotes"],
                    replay_config=configs[s], costs=costs) for s in sides}
    def labels_of(side):
        ts = {r["opportunity_key"]: r["decision_ts_ms"] for r in sides[side]}
        return [{"opportunity_id": t["opportunity_id"], "decision_ts_ms": ts[t["opportunity_id"]],
                 "net_r": t["net_r"], "result_available_ts_ms": t["result_available_ts_ms"]}
                for t in executions[side]["trades"] if t["admitted"]]
    split, bar = manifest["split"], configs["baseline"].bar_ms
    stop = min(split["holdout_start_ms"], split["as_of_ms"])
    width = max(1, (stop - split["validation_start_ms"]) // FOLDS)
    folds = [wf.Fold(i, split["train_start_ms"],
              split["validation_start_ms"] + i * width,
              split["validation_start_ms"] + i * width,
              stop if i == FOLDS - 1 else split["validation_start_ms"] + (i + 1) * width)
             for i in range(FOLDS)]
    study = wf.run_walk_forward(baseline=labels_of("baseline"), candidate=labels_of("candidate"),
                               folds=folds, bar_ms=bar, costs_complete=costs.manifest()["complete"],
                               horizon_bars=max(c.entry_window_bars + c.max_holding_bars - 1
                                                for c in configs.values()) + split["purge_bars"],
                               embargo_bars=split["embargo_bars"],
                               horizon_sufficient=True, seed=7)
    evidence = catalog.gate_evidence_from_study(replay=executions["candidate"], study=study,
        trades=[{**t, "playbook": manifest["candidate"]["selection_rule"].get("playbook")}
                for t in executions["candidate"]["trades"]],
        enabled_playbooks=manifest["candidate"]["playbooks"], window_start_ms=split["train_start_ms"],
        window_end_ms=stop, essential_gaps=[] if authorized["real_study_allowed"] else ["TEST_ONLY_NOT_REAL_EVIDENCE"])
    gate = catalog.go_no_go(evidence)
    # Calibração é de setups: replay individual, não confunde exclusão da
    # carteira com um stop, e não usa ledger financeiro da conta.
    results = {}
    for row in rows:
        try:
            opp = replay.Opportunity(opportunity_id=row["opportunity_key"], symbol=row["symbol"], direction=row["side"],
                decision_ts_ms=row["decision_ts_ms"], entry=row["entry"], stop_loss=row["stop_loss"],
                tp1=row["tp1"], tp2=row["tp2"], atr=row.get("atr"))
            results[row["opportunity_key"]] = replay.replay_opportunity(opp,
                tuple(replay.Candle(**b) for b in price["windows"][row["opportunity_key"]]), configs["candidate"], costs)
        except (KeyError, TypeError, ValueError):
            results[row["opportunity_key"]] = {}
    calibration = (_calibrate(rows, results, manifest, request=request,
                              dataset_hash=source["dataset_hash"], price_hash=price["price_hash"], now_ms=now_ms)
                   if manifest["comparison_scope"] == rm.SCOPE_SELECTION else
                   {"state": "NOT_REQUESTED", "artifact": None, "reason_code": "MANAGEMENT_SCOPE_NO_V3_FITTING"})
    extra = ({"research_manifest": manifest, "manifest_hash": manifest["manifest_hash"],
              "dataset_scope": dataset["scope"], "temporal_split": split,
              "selection_config": rm.selection_config_of(manifest["candidate"])}
             if manifest["comparison_scope"] == rm.SCOPE_SELECTION else {})
    contract = catalog.preselection_contract(population="SHADOW", study_kind="PRE_SELECTION",
        policy_version=manifest["candidate"]["policy_version"], universe_version=manifest["population"]["universe_version"],
        comparison_scope=manifest["comparison_scope"], baseline_config=configs["baseline"].manifest(),
        candidate_config=configs["candidate"].manifest(), costs_config=costs.manifest(),
        bundle_hash=manifest["hashes"]["bundle_hash"], dataset_fingerprint=source["dataset_hash"], cutoff_ms=split["as_of_ms"], **extra)
    official_study = catalog.study_payload(contract=contract, evidence=evidence, gate=gate,
        study=study, replay=executions["candidate"], evidence_key=study_key)
    return {"ok": True, "version": STUDY_VERSION, "kind": PAYLOAD_KIND, "study_key": study_key,
            "state": "LOCAL_OFFLINE_EXECUTED", "manifest": manifest, "identity": identity,
            "selection": compared, "baseline_replay": executions["baseline"], "candidate_replay": executions["candidate"],
            "walk_forward": study, "gate": gate, "study": official_study, "calibration": calibration,
            "artifact": calibration.get("artifact"), "observed_at_ms": now_ms,
            "real_study_allowed": authorized["real_study_allowed"], "source_assurance": price["source_assurance"],
            "promotable": False, "live_changed": False, "holdout_status": "SEALED"}


def validate_study_report(report, *, now_ms=None):
    """Hash íntegro não autoriza trocar evento, gestão ou janela do artefato.

    Só reconfere o registro; não roda replay/fitting nem concede aprovação.
    O contexto esperado vem do manifesto/pedido, nunca do artefato recebido.
    """
    from services import research_manifest_service as rm
    from services import score_v3_calibration_service as calib
    from services import preselection_experiment_service as catalog
    from services import strategy_evidence_service as evidence
    from services import research_dataset_service as ds
    from services import score_v3_service as score
    refusal = {"ok": False, "reason_code": "CALIBRATION_STUDY_IDENTITY_INVALID"}
    try:
        if not isinstance(report, Mapping) or report.get("ok") is not True \
                or report.get("kind") != PAYLOAD_KIND or report.get("version") != STUDY_VERSION \
                or report.get("promotable") is not False or report.get("live_changed") is not False \
                or report.get("holdout_status") != "SEALED":
            return refusal
        checked = rm.verify_manifest(report.get("manifest"))
        if not checked.get("ok"):
            return refusal
        manifest, identity = checked["manifest"], report["identity"]
        expected = {"manifest_hash": manifest["manifest_hash"],
                    "dataset_hash": identity["dataset_hash"], "price_hash": identity["price_hash"],
                    "calibration_request": identity["calibration_request"],
                    "baseline": manifest["baseline"]["management_config"],
                    "candidate": manifest["candidate"]["management_config"],
                    "costs": manifest["costs"]["config"], "split": manifest["split"]}
        if identity != expected or report.get("study_key") != digest(expected) \
                or any(not isinstance(identity[k], str) or len(identity[k]) != 64
                       or any(c not in "0123456789abcdef" for c in identity[k])
                       for k in ("dataset_hash", "price_hash")) \
                or report.get("real_study_allowed") is not rm.authorized_comparison(manifest)["real_study_allowed"]:
            return refusal
        contract = report["study"]["contract"]
        envelope = catalog.build_preselection_envelope(replay_config=contract["candidate_config"],
            contract_hash=contract["contract_hash"], selection_config=contract.get("selection_config"))
        study_verdict = evidence.verify_study_identity(report["study"], candidate_config=envelope,
            fingerprint=identity["dataset_hash"], cutoff=ds.ms_datetime(manifest["split"]["as_of_ms"]))
        if not study_verdict.get("ok"):
            return refusal
        request, calibration, artifact = identity["calibration_request"], report["calibration"], report.get("artifact")
        if artifact != calibration.get("artifact"):
            return refusal
        if request is None:
            return refusal if artifact is not None else {"ok": True, "reason_code": "CALIBRATION_NOT_REQUESTED"}
        if request != calibration_request(event=request["event"], valid_for_ms=request["valid_for_ms"]):
            return refusal
        if artifact is None:
            return {"ok": True, "reason_code": calibration.get("reason_code")}
        management = manifest["candidate"]["management_config"]
        cfg = score.ScoreConfig(**manifest["candidate"]["score_config"])
        fingerprint = score.model_fingerprint(playbook=manifest["candidate"]["selection_rule"]["playbook"], config=cfg)
        definition = {"event": request["event"], "population": cfg.population,
            "horizon_bars": management["entry_window_bars"] + management["max_holding_bars"] - 1,
            "bar_ms": management["bar_ms"], "censoring": request["censoring"],
            "payoff_ref": management["config_hash"], "interchangeable_with_other_events": False}
        verdict = calib.verify_artifact(artifact, now_ms=now_ms, model_fingerprint=fingerprint,
            score_config_hash=fingerprint, dataset_hash=identity["dataset_hash"],
            event=request["event"], population=cfg.population, expected_event_definition=definition)
        if not verdict["ok"]:
            return verdict
        expected_versions = {"study": STUDY_VERSION, "manifest": manifest["manifest_hash"],
                             "prices": identity["price_hash"], "costs": manifest["costs"]["config"]["config_hash"]}
        split = manifest["split"]
        width = (min(split["holdout_start_ms"], split["as_of_ms"]) - split["validation_start_ms"]) // FOLDS
        cuts = {split["validation_start_ms"] + i * width
                - (split["purge_bars"] + split["embargo_bars"]) * management["bar_ms"] for i in range(FOLDS)}
        generated = artifact["generation"]["generated_at_ms"]
        if artifact["versions"] != expected_versions or artifact["training"]["cutoff_ms"] not in cuts \
                or artifact["source"] != "R10A_REPLAY_FROM_OFFICIAL_EXPORT" \
                or generated != report["observed_at_ms"] \
                or artifact["generation"]["valid_until_ms"] != generated + request["valid_for_ms"]:
            return refusal
        if request["event"] != calib.EVENT_NET_POSITIVE:
            return refusal if calibration.get("payoff_evidence") is not None or calibration.get("net_ev") is not None \
                else {"ok": True, "reason_code": calibration.get("reason_code")}
        payoff = calibration.get("payoff_evidence")
        payoff_verdict = calib.verify_oos_payoff_evidence(payoff,
            management_hash=management["config_hash"], dataset_hash=identity["dataset_hash"],
            costs_hash=manifest["costs"]["config"]["config_hash"], event=request["event"], now_ms=now_ms)
        if not payoff_verdict.get("ok") or payoff["cutoff_ms"] != artifact["training"]["cutoff_ms"]:
            return refusal
        return {"ok": True, "reason_code": calibration.get("reason_code")}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return refusal


async def persist_study(session_factory, report):
    """Reusa CAS oficial; primeira evidência não é sobrescrita por reprocessamento."""
    from services import policy_state_service as state
    if not isinstance(report, Mapping) or report.get("ok") is not True or report.get("kind") != PAYLOAD_KIND:
        return {"published": False, "reason_code": "STUDY_NOT_EXECUTED"}
    verified = validate_study_report(report, now_ms=report.get("observed_at_ms"))
    if not verified["ok"]:
        return {"published": False, "reason_code": verified["reason_code"]}
    identity = {"experiment_key": "r13cal:" + report["study_key"][:32],
                "universe_version": report["manifest"]["population"]["universe_version"], "population": "SHADOW"}
    read = await state.read_state(session_factory, **identity)
    if not read["available"]:
        return {"published": False, "reason_code": read["reason_code"]}
    if read.get("found"):
        stored = (read["state"].get("payload") or {})
        if stored.get("study_key") == report["study_key"]:
            return {"published": False, "generation": read["state"]["generation"], "reason_code": "STUDY_UNCHANGED"}
        return {"published": False, "reason_code": "STUDY_IDENTITY_CONFLICT"}
    return await state.publish_generation(session_factory, **identity,
        expected_generation=0, period_key=str(report["identity"]["split"]["as_of_ms"]),
        evidence_key=report["study_key"], now_ms=report["observed_at_ms"], payload=dict(report))


async def load_latest_study(session_factory, *, now_ms=None):
    """GET só lê registro concluído; nunca exporta, ajusta, simula ou grava."""
    from sqlalchemy import select
    from models.policy_simulation_state import PolicySimulationState
    try:
        async with session_factory() as session:
            row = (await session.execute(select(PolicySimulationState.payload)
                .where(PolicySimulationState.experiment_key.like("r13cal:%"),
                       PolicySimulationState.population == "SHADOW")
                .order_by(PolicySimulationState.published_at_ms.desc(), PolicySimulationState.id.desc())
                .limit(1))).scalar_one_or_none()
    except Exception:
        return {"available": False, "reason_code": "CALIBRATION_STUDY_READ_ERROR", "artifact": None, "manifest": None}
    if row is None:
        return {"available": True, "reason_code": "NO_CALIBRATION_STUDY", "artifact": None, "manifest": None}
    verified = validate_study_report(row, now_ms=now_ms)
    if not verified["ok"]:
        return {"available": False, "reason_code": verified["reason_code"], "artifact": None, "manifest": None}
    manifest = row["manifest"]
    artifact = row.get("artifact")
    return {"available": True, "reason_code": row["calibration"].get("reason_code"),
            "artifact": artifact, "manifest": manifest, "study_key": row["study_key"],
            "calibration_state": row["calibration"]["state"]}
