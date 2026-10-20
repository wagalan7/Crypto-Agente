"""R13 operational authority: explicit approvals, immutable bundles and CAS.

No order, exchange, risk mutation or implicit activation lives here. The existing
P05 SHADOW advisory lock is shared by approvals, promotion and their consumers.
The operational singleton is a namespace in the existing policy state table;
it is NOT published by the research generation publisher (different lock).
Local fences are additional containment, not inter-process/exchange atomicity.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

VERSION = "R13_OPERATIONAL_AUTHORITY_V1"
BUNDLE_VERSION = "R13_SELECTION_BUNDLE_V1"
LOCK_KEY = 505202609
STATE_KEY = "r13:operational:singleton:v1"
STATE_IDENTITY = ("r13:operational", VERSION, "SINGLETON", "OPERATIONAL")
PURPOSES = ("SHADOW", "PROMOTION", "CANARY")
OPERATIONAL_SEMANTICS = {"population": "R09_PRE_SELECTION_POPULATION",
    "scope": "SELECTION_ONLY", "tf_selection": "MAX_CANDIDATE_SCORE",
    "safety": "LEGACY_UNCHANGED", "management": "LEGACY_UNCHANGED"}
_ALLOW_TEST_APPROVALS = False  # private hermetic harness opt-in, NEVER an ENV
_LOCAL_FENCE = 0
_LOCAL_PENDING = False
_PENDING_FENCES = set()
_FAILED_FENCES = set()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _now(value=None):
    value = int(time.time() * 1000) if value is None else value
    if type(value) is not int or value <= 0:
        raise ValueError("CLOCK_INVALID")
    return value


def _text(value, maximum=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _closed(value, fields):
    return isinstance(value, Mapping) and set(value) == set(fields)


def _failure(reason, **extra):
    return {"ok": False, "blocked": True, "reason_code": reason, **extra}


def selected_mode():
    """LEGACY/OFF do no candidate work. Invalid explicit configuration blocks.

    Unset OR empty/whitespace reads as LEGACY: an empty variable is absence of
    configuration, and the champion must keep running exactly as today. Any
    OTHER string is an explicit mistake and blocks new candidate entries.
    """
    mode = os.getenv("R13_OPERATIONAL_SELECTOR", "LEGACY").strip().upper() or "LEGACY"
    if mode not in ("LEGACY", "OFF", "CANDIDATE"):
        return "INVALID"
    if mode == "CANDIDATE" and _selected_id() is None:
        return "INVALID"
    return mode


def _selected_id():
    raw = os.getenv("R13_OPERATIONAL_EXPERIMENT_ID", "")
    if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
        return None
    return int(raw)


def _begin_mutation():
    global _LOCAL_FENCE, _LOCAL_PENDING
    _LOCAL_FENCE += 1
    _PENDING_FENCES.add(_LOCAL_FENCE)
    _LOCAL_PENDING = True
    return _LOCAL_FENCE


def _mutation_finished(ticket, failed=False, recover=False):
    global _LOCAL_PENDING
    _PENDING_FENCES.discard(ticket)
    if failed:
        _FAILED_FENCES.add(ticket)
    if recover:
        _FAILED_FENCES.clear()
    _LOCAL_PENDING = bool(_PENDING_FENCES or _FAILED_FENCES)


async def _lock(session):
    from sqlalchemy import text
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})


async def _state(session, create=False, *, for_update=True):
    from sqlalchemy import select
    from models.policy_simulation_state import PolicySimulationState as S
    query = select(S).where(S.state_key == STATE_KEY)
    if for_update:
        query = query.with_for_update()
    row = (await session.execute(query)).scalar_one_or_none()
    if row is None and create:
        moment = datetime.now(timezone.utc)
        row = S(state_key=STATE_KEY, experiment_key=STATE_IDENTITY[0],
                policy_version=STATE_IDENTITY[1], universe_version=STATE_IDENTITY[2],
                population=STATE_IDENTITY[3], generation=0,
                payload={"version": VERSION, "mode": "LEGACY", "history": []},
                created_at=moment, updated_at=moment)
        session.add(row)
        await session.flush()
    if row is not None and ((row.experiment_key, row.policy_version, row.universe_version,
                             row.population) != STATE_IDENTITY or type(row.generation) is not int
                            or row.generation < 0 or not isinstance(row.payload, dict)
                            or row.payload.get("version") != VERSION):
        raise ValueError("OPERATIONAL_STATE_INVALID")
    return row


async def _experiment(session, exp_id):
    from sqlalchemy import select
    from models.strategy_experiment import StrategyExperiment as E
    if type(exp_id) is not int or exp_id <= 0:
        raise ValueError("EXPERIMENT_ID_INVALID")
    return (await session.execute(select(E).where(E.id == exp_id)
                                 .with_for_update())).scalar_one_or_none()


def _op(exp):
    raw = getattr(exp, "decision", None)
    decision = raw if isinstance(raw, dict) else {}
    return copy.deepcopy(decision.get("operational") or {})


def _write_op(exp, payload):
    exp.decision = {**(exp.decision if isinstance(exp.decision, dict) else {}),
                    "operational": copy.deepcopy(payload)}
    exp.updated_at = datetime.now(timezone.utc)


def _advance(row, action, *, exp_id=None, approval_id=None, operator=None,
             reason=None, mode=None, block_entries=None):
    data = copy.deepcopy(row.payload)
    if mode is not None:
        data["mode"] = mode
    if block_entries is not None:
        data["block_entries"] = block_entries
    if action == "PROMOTE":
        data.update(experiment_id=exp_id, approval_id=approval_id)
    if action == "ROLLBACK":
        data.update(experiment_id=None, approval_id=None)
    generation = row.generation + 1
    data.setdefault("history", []).append({"generation": generation, "action": action,
        "experiment_id": exp_id, "approval_id": approval_id, "operator": operator,
        "reason": reason, "recorded_at_ms": _now()})
    row.payload, row.generation = data, generation
    row.updated_at = datetime.now(timezone.utc)
    row.published_at_ms = _now()
    return generation


def _exp_identity(exp):
    from services import strategy_evidence_service as e
    from services import preselection_experiment_service as p
    if exp is None or not e.is_pre_selection_experiment(exp):
        raise ValueError("EXPERIMENT_TYPE_MISMATCH")
    if exp.status not in ("OFFLINE_VALIDATED", "SHADOW", "ELIGIBLE"):
        raise ValueError("EXPERIMENT_STATE_INCOMPATIBLE")
    frozen = e._frozen_study_of(exp)
    if not frozen["ok"]:
        raise ValueError(frozen["reason_code"])
    contract = frozen["contract"]
    if contract.get("comparison_scope") != "SELECTION_ONLY":
        raise ValueError("LIVE_SELECTION_SCOPE_NOT_IMPLEMENTED")
    from services import research_manifest_service as rm
    verified = rm.verify_manifest(contract.get("research_manifest"))
    if not verified.get("ok"):
        raise ValueError("MANIFEST_INVALID")
    manifest = verified["manifest"]
    if manifest["candidate"]["selection_rule"]["kind"] != rm.RULE_SCORE_V3_MIN:
        raise ValueError("LIVE_SELECTION_RULE_NOT_IMPLEMENTED")
    if e.canonical_hash(exp.candidate_config) != exp.candidate_hash \
            or e.canonical_hash(contract["baseline_config"]) != exp.champion_hash \
            or exp.dataset_fingerprint != contract["dataset_fingerprint"]:
        raise ValueError("EXPERIMENT_IDENTITY_DRIFT")
    return contract, manifest


def build_bundle(exp, report, champion_config, *, now_ms=None):
    """Derive identity from verified official experiment/report, never claims."""
    from services import research_study_service as rs
    from services import score_v3_calibration_service as c
    now = _now(now_ms)
    contract, manifest = _exp_identity(exp)
    check = rs.validate_study_report(report, now_ms=now)
    if not check.get("ok") or not isinstance(report.get("artifact"), dict):
        raise ValueError("CALIBRATION_STUDY_INVALID")
    if digest(report["manifest"]) != digest(manifest) \
            or report["study"]["contract_hash"] != contract["contract_hash"] \
            or report["identity"]["dataset_hash"] != exp.dataset_fingerprint:
        raise ValueError("CALIBRATION_STUDY_EXPERIMENT_MISMATCH")
    if not isinstance(champion_config, dict) or not champion_config:
        raise ValueError("CHAMPION_CONFIG_REQUIRED")
    # Snapshot current legacy selector contract. Changing it later invalidates
    # authority; it is not replaced by the research baseline replay config.
    from services.strategy_evidence_service import discover_champion_config
    if digest(champion_config) != digest(discover_champion_config()):
        raise ValueError("CHAMPION_CONFIG_DRIFT")
    artifact = report["artifact"]
    if artifact["state"] not in (c.STATE_FITTED, c.STATE_OOS_VALIDATED):
        raise ValueError("CALIBRATION_STATE_INVALID")
    body = {"bundle_version": BUNDLE_VERSION, "experiment_id": exp.id,
        "experiment_key": exp.experiment_key, "candidate_hash": exp.candidate_hash,
        "champion_hash": exp.champion_hash, "manifest": copy.deepcopy(manifest),
        "calibration_artifact": copy.deepcopy(artifact),
        "calibration_study_key": report["study_key"],
        "calibration_report": copy.deepcopy(report),
        "policy_config": copy.deepcopy(manifest["candidate"]),
        "operational_semantics": copy.deepcopy(OPERATIONAL_SEMANTICS),
        "champion_config": copy.deepcopy(champion_config),
        "champion_config_hash": digest(champion_config),
        "frozen_contract": copy.deepcopy(contract),
        "study_identity": {"contract_hash": contract["contract_hash"],
            "dataset_fingerprint": exp.dataset_fingerprint,
            "cutoff_ms": contract["cutoff_ms"], "bundle_hash": contract["bundle_hash"]}}
    return {**body, "bundle_hash": digest(body)}


def _verified_acceptance(report, *, now_ms, purpose):
    """Operational checks never confuse an evaluated V1 with accepted evidence.

    The private harness can bypass only TEST_ONLY human identity. It still
    invokes the real verifier and requires every metric/state for the purpose.
    Production never uses the STRUCTURAL storage verdict as authority.
    """
    from services import research_acceptance_service as acceptance
    checked = acceptance.verify_acceptance(report, now_ms=now_ms, purpose=purpose)
    if checked.get("reason_code") == "ACCEPTANCE_TEST_ONLY_NOT_AUTHORITY" and _ALLOW_TEST_APPROVALS:
        checked = acceptance.verify_acceptance(report, now_ms=now_ms, purpose="STRUCTURAL")
        if checked.get("ok"):
            if checked.get("calibration_state") != acceptance.ACCEPTED:
                return _failure("ACCEPTANCE_CALIBRATION_NOT_ACCEPTED")
            if checked.get("economics_state") != acceptance.ACCEPTED:
                return _failure("ACCEPTANCE_ECONOMICS_NOT_ACCEPTED")
            if purpose == "PROMOTION" and (checked.get("protection") or {}).get("state") != acceptance.ACCEPTED:
                return _failure("ACCEPTANCE_PROTECTION_NOT_ACCEPTED")
    return checked


def _acceptance_report(exp, bundle):
    """Overlay a controlled operational receipt without rewriting offline history."""
    report = copy.deepcopy(bundle["calibration_report"])
    receipt = _op(exp).get("acceptance") if exp else None
    if receipt is not None:
        report["acceptance"] = copy.deepcopy(receipt)
    snapshot = (report.get("acceptance") or {}).get("prospective_evidence")
    if snapshot is not None:
        from services import prospective_shadow_service as prospective
        start = (exp.shadow_metrics or {}).get(prospective.KEY)
        context = prospective.frozen_shadow_context(exp, generation=(start or {}).get("generation"),
            approval_id=(start or {}).get("approval_id"), started_at_ms=(start or {}).get("started_at_ms"))
        expected = ({key: context[key] for key in prospective._COHORT_FIELDS}
                    if context is not None else {})
        expected.update(study_key=bundle["calibration_study_key"], bundle_hash=bundle["bundle_hash"])
        if not isinstance(snapshot, Mapping) or snapshot.get("identity") != expected:
            raise ValueError("ACCEPTANCE_PROSPECTIVE_IDENTITY_DRIFT")
    return report


def approval_validity_hash(bundle, purpose, acceptance_record_hash=None):
    """Human ratification of the exact offline bundle AND operational receipt."""
    if purpose == "SHADOW":
        return bundle["bundle_hash"]
    if acceptance_record_hash is None:
        acceptance_record_hash = (bundle["calibration_report"].get("acceptance") or {}).get("record_hash")
    if purpose not in ("PROMOTION", "CANARY") or not _hash(acceptance_record_hash):
        return None
    return digest({"version": "R13_APPROVAL_VALIDITY_V2", "bundle_hash": bundle["bundle_hash"],
                   "acceptance_record_hash": acceptance_record_hash})


def validate_bundle(exp, bundle, *, now_ms=None, purpose="CANARY"):
    try:
        fields = ("bundle_version", "experiment_id", "experiment_key", "candidate_hash",
            "champion_hash", "manifest", "calibration_artifact", "calibration_study_key",
            "calibration_report", "policy_config", "operational_semantics", "champion_config", "champion_config_hash",
            "frozen_contract", "study_identity", "bundle_hash")
        if purpose not in PURPOSES or not _closed(bundle, fields):
            return _failure("OPERATIONAL_BUNDLE_SCHEMA_INVALID")
        expected = build_bundle(exp, bundle["calibration_report"], bundle["champion_config"], now_ms=now_ms)
        if digest(bundle) != digest(expected):
            return _failure("OPERATIONAL_BUNDLE_DRIFT")
        if purpose in ("CANARY", "PROMOTION") and bundle["calibration_artifact"]["state"] != "OOS_VALIDATED":
            return _failure("CALIBRATION_OOS_NOT_VALIDATED")
        from services import research_manifest_service as rm
        if rm.authorized_comparison(bundle["manifest"])["real_study_allowed"] is not True \
                and not _ALLOW_TEST_APPROVALS:
            return _failure("TEST_ONLY_BUNDLE_NOT_OPERATIONAL")
        record_hash = None
        if purpose in ("CANARY", "PROMOTION"):
            checked = _verified_acceptance(_acceptance_report(exp, bundle), now_ms=_now(now_ms), purpose=purpose)
            if checked.get("ok") is not True:
                return _failure(checked.get("reason_code") or "RESEARCH_ACCEPTANCE_NOT_VERIFIED")
            record_hash = checked["record_hash"]
        return {"ok": True, "bundle_hash": bundle["bundle_hash"], "acceptance_record_hash": record_hash}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return _failure("OPERATIONAL_BUNDLE_INVALID")


def validate_approval_payload(payload, bundle, *, now_ms=None, acceptance_record=None):
    try:
        now = _now(now_ms)
        if not _closed(payload, ("confirm", "purpose", "references", "limits", "validity")) \
                or payload["confirm"] is not True or payload["purpose"] not in PURPOSES:
            return _failure("APPROVAL_SCHEMA_INVALID")
        refs, limits, validity = payload["references"], payload["limits"], payload["validity"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 10 \
                or not all(_text(ref, 500) for ref in refs) \
                or len(set(refs)) != len(refs) \
                or not _closed(validity, ("id", "hash")) or not _text(validity["id"]) \
                or validity["hash"] != approval_validity_hash(bundle, payload["purpose"],
                    (acceptance_record or {}).get("record_hash")):
            return _failure("APPROVAL_REFERENCE_INVALID")
        if not _closed(limits, ("symbols", "playbooks", "max_orders", "max_risk_pct", "expires_at_ms")):
            return _failure("CANARY_LIMITS_INVALID")
        for key in ("symbols", "playbooks"):
            values = limits[key]
            if not isinstance(values, list) or not 1 <= len(values) <= 100 \
                    or not all(_text(v, 80) for v in values) or len(set(values)) != len(values):
                return _failure("CANARY_LIMITS_INVALID")
        allowed = bundle["manifest"]["population"]["symbols"]
        if allowed is not None and not set(limits["symbols"]).issubset(set(allowed)) \
                or not set(limits["playbooks"]).issubset(set(bundle["manifest"]["candidate"]["playbooks"])):
            return _failure("CANARY_UNIVERSE_INCOMPATIBLE")
        if type(limits["max_orders"]) is not int or not 1 <= limits["max_orders"] <= 100:
            return _failure("CANARY_LIMITS_INVALID")
        risk = limits["max_risk_pct"]
        ceiling = float(os.getenv("EXCHANGE_MAX_RISK_PCT", "2.0"))
        if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not math.isfinite(risk) \
                or not math.isfinite(ceiling) or not 0 < risk <= min(ceiling, 2.0):
            return _failure("CANARY_RISK_LIMIT_INVALID")
        until = limits["expires_at_ms"]
        if type(until) is not int or not now < until <= bundle["calibration_artifact"]["generation"]["valid_until_ms"]:
            return _failure("APPROVAL_EXPIRED_OR_OUTLIVES_ARTIFACT")
        record = acceptance_record if acceptance_record is not None else bundle["calibration_report"].get("acceptance")
        if payload["purpose"] in ("CANARY", "PROMOTION") and isinstance(record, Mapping) \
                and (type(record.get("valid_until_ms")) is not int or until > record["valid_until_ms"]):
            return _failure("APPROVAL_OUTLIVES_ACCEPTANCE")
        return {"ok": True, "payload": copy.deepcopy(payload)}
    except (ValueError, TypeError, KeyError, OverflowError):
        return _failure("APPROVAL_SCHEMA_INVALID")


async def _report_in_session(session, key, universe):
    from sqlalchemy import select
    from models.policy_simulation_state import PolicySimulationState as S
    from services.policy_state_service import state_key
    row = (await session.execute(select(S).where(S.state_key == state_key(
        experiment_key="r13cal:" + key[:32], universe_version=universe, population="SHADOW"))
        .with_for_update())).scalar_one_or_none()
    if row is None or not isinstance(row.payload, dict) or row.payload.get("study_key") != key:
        raise ValueError("CALIBRATION_STUDY_NOT_PERSISTED")
    return copy.deepcopy(row.payload)


async def _persisted_bundle_matches(session, bundle):
    """Observe the official record again, including revocation/drift after restart."""
    persisted = await _report_in_session(session, bundle["calibration_study_key"],
        bundle["manifest"]["population"]["universe_version"])
    if digest(persisted) != digest(bundle["calibration_report"]):
        raise ValueError("ACCEPTANCE_PERSISTED_RECORD_DRIFT")


async def register_bundle(session_factory, exp_id, payload, operator):
    base_fields = ("confirm", "calibration_study_key", "champion_config")
    prepare = _closed(payload, (*base_fields, "prospective_cutoff_ms", "expected_generation"))
    if not _text(operator) or not (_closed(payload, base_fields) or prepare) \
            or payload["confirm"] is not True or not _hash(payload["calibration_study_key"]):
        return _failure("BUNDLE_REGISTRATION_INVALID")
    if prepare:
        cutoff, generation = payload["prospective_cutoff_ms"], payload["expected_generation"]
        if type(cutoff) is not int or not 0 < cutoff <= _now() \
                or type(generation) is not int or generation < 0:
            return _failure("ACCEPTANCE_PREPARATION_INVALID")
        return await _register_prospective_acceptance(session_factory, exp_id, payload, operator)
    ticket = _begin_mutation()
    try:
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session, True), await _experiment(session, exp_id)
            _, manifest = _exp_identity(exp)
            report = await _report_in_session(session, payload["calibration_study_key"],
                                             manifest["population"]["universe_version"])
            bundle = build_bundle(exp, report, payload["champion_config"])
            op = _op(exp)
            if op.get("bundle"):
                if digest(op["bundle"]) != digest(bundle):
                    raise ValueError("OPERATIONAL_BUNDLE_IMMUTABLE")
                # Geração lida ANTES do rollback: depois dele a instância está
                # expirada e qualquer leitura exigiria IO fora do contexto.
                current = int(state.generation)
                await session.rollback()
                _mutation_finished(ticket)
                return {"ok": True, "idempotent": True, "generation": current,
                        "bundle_hash": bundle["bundle_hash"]}
            op.update(bundle=bundle, approvals=[], registered_by=operator)
            _write_op(exp, op)
            generation = _advance(state, "REGISTER_BUNDLE", exp_id=exp_id, operator=operator)
            await session.commit()
        _mutation_finished(ticket)
        return {"ok": True, "generation": generation, "bundle_hash": bundle["bundle_hash"]}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def _register_prospective_acceptance(session_factory, exp_id, payload, operator):
    """Prepare once at an explicit cutoff, then CAS the same official cohort.

    Replay/bootstrap are never done by reads or signed-order boundaries. The
    preparation runs outside both transactions; no offline record is rewritten.
    """
    from services import prospective_shadow_service as prospective
    from services import research_acceptance_service as acceptance
    ticket = _begin_mutation()
    try:
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session), await _experiment(session, exp_id)
            op = _op(exp)
            bundle = op.get("bundle")
            if state is None or not isinstance(bundle, Mapping):
                raise ValueError("OPERATIONAL_BUNDLE_NOT_REGISTERED")
            if bundle["calibration_study_key"] != payload["calibration_study_key"] \
                    or digest(bundle["champion_config"]) != digest(payload["champion_config"]):
                raise ValueError("OPERATIONAL_BUNDLE_IMMUTABLE")
            await _persisted_bundle_matches(session, bundle)
            if not validate_bundle(exp, bundle, now_ms=_now(), purpose="SHADOW").get("ok"):
                raise ValueError("OPERATIONAL_BUNDLE_INVALID")
            inputs = await prospective.load_prospective_inputs(session, exp,
                cutoff_ms=payload["prospective_cutoff_ms"])
            if not inputs.get("available"):
                raise ValueError(inputs.get("reason_code") or "PROSPECTIVE_INPUTS_UNAVAILABLE")
            existing = op.get("acceptance")
            previous = (existing or {}).get("prospective_evidence")
            if isinstance(previous, Mapping) and previous.get("cutoff_ms") == payload["prospective_cutoff_ms"]:
                if not prospective.cohort_matches_snapshot(inputs["annotations"], previous) \
                        or previous.get("identity", {}).get("bundle_hash") != bundle["bundle_hash"]:
                    raise ValueError("ACCEPTANCE_CUTOFF_IMMUTABLE")
                stored = acceptance.verify_acceptance(_acceptance_report(exp, bundle),
                    now_ms=_now(), purpose="STRUCTURAL")
                if not stored.get("ok"):
                    raise ValueError(stored.get("reason_code") or "ACCEPTANCE_RECORD_INVALID")
                current = int(state.generation)
                await session.rollback()
                _mutation_finished(ticket)
                return {"ok": True, "idempotent": True, "generation": current,
                        "bundle_hash": bundle["bundle_hash"], "acceptance_record_hash": existing["record_hash"],
                        "approval_validity_hash": approval_validity_hash(bundle, "PROMOTION", existing["record_hash"])}
            if state.generation != payload["expected_generation"]:
                raise ValueError("OPERATIONAL_GENERATION_STALE")
            if isinstance(existing, Mapping) and existing.get("state") == acceptance.ACCEPTED:
                raise ValueError("ACCEPTANCE_RECEIPT_FROZEN")
            start = (exp.shadow_metrics or {}).get(prospective.KEY) or {}
            shadow = await approval_for_in_session(session, exp, "SHADOW", _now(),
                approval_id=start.get("approval_id"), expected_generation=start.get("generation"),
                operational_state=state)
            if not shadow.get("ok"):
                raise ValueError(shadow.get("reason_code") or "SHADOW_AUTHORITY_NO_LONGER_VALID")
            guard = {"generation": state.generation, "bundle_hash": bundle["bundle_hash"],
                     "start_hash": digest((exp.shadow_metrics or {}).get(prospective.KEY)),
                     "previous_hash": digest(existing)}
            bundle = copy.deepcopy(bundle)
            await session.rollback()
        # Explicit computation, with no DB row locks held.
        snapshot = prospective.build_acceptance_snapshot(inputs, bundle)
        receipt = acceptance.build_acceptance(bundle["calibration_report"], now_ms=_now(),
                                             prospective_evidence=snapshot)
        report = {**copy.deepcopy(bundle["calibration_report"]), "acceptance": receipt}
        checked = acceptance.verify_acceptance(report, now_ms=_now(), purpose="STRUCTURAL", recompute=True)
        if not checked.get("ok"):
            raise ValueError(checked.get("reason_code") or "ACCEPTANCE_PREPARATION_INVALID")
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session), await _experiment(session, exp_id)
            op = _op(exp)
            if state is None or state.generation != guard["generation"]:
                raise ValueError("OPERATIONAL_GENERATION_STALE")
            if not isinstance(op.get("bundle"), Mapping) or op["bundle"]["bundle_hash"] != guard["bundle_hash"] \
                    or digest((exp.shadow_metrics or {}).get(prospective.KEY)) != guard["start_hash"] \
                    or digest(op.get("acceptance")) != guard["previous_hash"]:
                raise ValueError("ACCEPTANCE_PREPARATION_IDENTITY_DRIFT")
            await _persisted_bundle_matches(session, op["bundle"])
            if not validate_bundle(exp, op["bundle"], now_ms=_now(), purpose="SHADOW").get("ok"):
                raise ValueError("OPERATIONAL_BUNDLE_INVALID")
            final_inputs = await prospective.load_prospective_inputs(session, exp,
                cutoff_ms=payload["prospective_cutoff_ms"])
            if not final_inputs.get("available") or final_inputs.get("cohort") != inputs["cohort"] \
                    or digest(final_inputs.get("annotations")) != digest(inputs["annotations"]):
                raise ValueError("ACCEPTANCE_PREPARATION_COHORT_DRIFT")
            # Last awaited cohort read can outlive the original approval.
            # Check its authority AFTER that wait, before any receipt write.
            start = (exp.shadow_metrics or {}).get(prospective.KEY) or {}
            shadow = await approval_for_in_session(session, exp, "SHADOW", _now(),
                approval_id=start.get("approval_id"), expected_generation=start.get("generation"),
                operational_state=state)
            if not shadow.get("ok"):
                raise ValueError(shadow.get("reason_code") or "SHADOW_AUTHORITY_NO_LONGER_VALID")
            final = acceptance.verify_acceptance(report, now_ms=_now(), purpose="STRUCTURAL")
            if not final.get("ok"):
                raise ValueError(final.get("reason_code") or "ACCEPTANCE_RECORD_INVALID")
            if op.get("acceptance") is not None:
                previous = op["acceptance"]
                old_snapshot = previous.get("prospective_evidence") or {}
                metadata = {key: previous.get(key) for key in (
                    "record_hash", "state", "generated_at_ms", "valid_until_ms", "revoked")}
                metadata.update(prospective_cutoff_ms=old_snapshot.get("cutoff_ms"),
                                cohort_hash=old_snapshot.get("cohort_hash"), superseded_by=receipt["record_hash"])
                op["acceptance_history"] = [*(op.get("acceptance_history") or [])[-31:], metadata]
            op.update(acceptance=copy.deepcopy(receipt), acceptance_registered_by=operator)
            # Insufficient/rejected analytical cuts MUST NOT stop the SHADOW
            # approval generation while its cohort is still being collected.
            # Accepted evidence freezes the cut and fences the ready authority.
            generation = (_advance(state, "REGISTER_ACCEPTANCE", exp_id=exp_id, operator=operator)
                          if receipt["state"] == acceptance.ACCEPTED else int(state.generation))
            revision = op.get("acceptance_revision", 0)
            if type(revision) is not int or revision < 0:
                raise ValueError("ACCEPTANCE_REVISION_INVALID")
            op["acceptance_revision"] = revision + 1
            event = {"revision": revision + 1, "generation": generation, "operator": operator,
                     "cutoff_ms": snapshot["cutoff_ms"], "record_hash": receipt["record_hash"],
                     "state": receipt["state"], "recorded_at_ms": _now()}
            op["acceptance_events"] = [*(op.get("acceptance_events") or [])[-31:], event]
            op["acceptance_generation"] = generation
            _write_op(exp, op)
            await session.commit()
        _mutation_finished(ticket)
        return {"ok": True, "generation": generation, "bundle_hash": bundle["bundle_hash"],
                "acceptance_record_hash": receipt["record_hash"], "acceptance_state": receipt["state"],
                "approval_validity_hash": approval_validity_hash(bundle, "PROMOTION", receipt["record_hash"]),
                "promoted": False, "no_orders_executed": True}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def register_approval(session_factory, exp_id, payload, operator, test_only=False):
    if not _text(operator) or type(test_only) is not bool:
        return _failure("APPROVAL_OPERATOR_INVALID")
    ticket = _begin_mutation()
    try:
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session, True), await _experiment(session, exp_id)
            op = _op(exp) if exp else {}
            bundle = op.get("bundle")
            if isinstance(bundle, Mapping):
                await _persisted_bundle_matches(session, bundle)
            verdict = validate_bundle(exp, bundle, purpose=(payload or {}).get("purpose"), now_ms=_now())
            if not verdict["ok"]:
                raise ValueError(verdict["reason_code"])
            from services import research_manifest_service as rm
            effective_test = test_only or not rm.authorized_comparison(bundle["manifest"])["real_study_allowed"]
            if effective_test and not _ALLOW_TEST_APPROVALS:
                raise ValueError("TEST_ONLY_APPROVAL_NOT_OPERATIONAL")
            receipt = _acceptance_report(exp, bundle).get("acceptance")
            check = validate_approval_payload(payload, bundle, acceptance_record=receipt)
            if not check["ok"]:
                raise ValueError(check["reason_code"])
            body = {"version": VERSION, "experiment_id": exp.id, "candidate_hash": exp.candidate_hash,
                    "bundle_hash": bundle["bundle_hash"], "payload": check["payload"],
                    "operator": operator, "test_only": effective_test}
            if payload["purpose"] in ("PROMOTION", "CANARY"):
                body["acceptance_record_hash"] = verdict["acceptance_record_hash"]
            approval_id = digest(body)
            existing = next((a for a in op.get("approvals", []) if a.get("approval_id") == approval_id), None)
            if existing:
                if existing.get("status") != "ACTIVE":
                    raise ValueError("APPROVAL_ALREADY_REVOKED")
                current = int(state.generation)
                await session.rollback()
                _mutation_finished(ticket)
                return {"ok": True, "idempotent": True, "approval_id": approval_id,
                        "generation": current}
            approval = {**body, "approval_id": approval_id, "status": "ACTIVE", "recorded_at_ms": _now(),
                        "purpose": payload["purpose"], "limits": copy.deepcopy(payload["limits"]), "revocations": []}
            op.setdefault("approvals", []).append(approval)
            _write_op(exp, op)
            generation = _advance(state, "REGISTER_APPROVAL", exp_id=exp_id, approval_id=approval_id, operator=operator)
            await session.commit()
        _mutation_finished(ticket)
        return {"ok": True, "approval_id": approval_id, "generation": generation}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def approval_for_in_session(session, exp, purpose, now_ms=None, *, approval_id=None,
                                  expected_generation=None, operational_state=None):
    now = _now(now_ms)
    # Already-locking catalog callers hold505202609; do not take a new state
    # row lock after their experiment row. Transport passes its state-first lock.
    state = operational_state if operational_state is not None else await _state(session, for_update=False)
    generation = state.generation if state else 0
    if type(expected_generation) is not int or expected_generation != generation:
        return _failure("OPERATIONAL_GENERATION_STALE", generation=generation)
    bundle = _op(exp).get("bundle") if exp else None
    if isinstance(bundle, Mapping):
        try:
            await _persisted_bundle_matches(session, bundle)
        except (ValueError, KeyError, TypeError):
            return _failure("ACCEPTANCE_PERSISTED_RECORD_DRIFT")
    # Availability is checked AFTER the read await, not only at entry.
    now = max(now, _now())
    verdict = validate_bundle(exp, bundle, now_ms=now, purpose=purpose)
    if not verdict["ok"]:
        return verdict
    approvals = _op(exp).get("approvals", [])
    approval = next((a for a in approvals if a.get("approval_id") == approval_id), None)
    if not approval or approval.get("status") != "ACTIVE" or approval.get("purpose") != purpose:
        return _failure("HUMAN_APPROVAL_MISSING_OR_REVOKED")
    body = {k: approval.get(k) for k in ("version", "experiment_id", "candidate_hash", "bundle_hash", "payload", "operator", "test_only")}
    if "acceptance_record_hash" in approval:
        body["acceptance_record_hash"] = approval["acceptance_record_hash"]
    if digest(body) != approval_id or approval.get("experiment_id") != exp.id \
            or approval.get("candidate_hash") != exp.candidate_hash \
            or approval.get("bundle_hash") != bundle["bundle_hash"] \
            or approval.get("purpose") != approval.get("payload", {}).get("purpose") \
            or approval.get("limits") != approval.get("payload", {}).get("limits") \
            or (approval.get("test_only") is not False and not _ALLOW_TEST_APPROVALS):
        return _failure("HUMAN_APPROVAL_IDENTITY_INVALID")
    if purpose in ("PROMOTION", "CANARY") and approval.get("acceptance_record_hash") != verdict["acceptance_record_hash"]:
        return _failure("HUMAN_APPROVAL_ACCEPTANCE_MISMATCH")
    receipt = _acceptance_report(exp, bundle).get("acceptance")
    check = validate_approval_payload(approval.get("payload"), bundle, now_ms=now, acceptance_record=receipt)
    if not check["ok"]:
        return check
    authority = {"ok": True, "mode": "CANDIDATE" if purpose == "CANARY" else "SHADOW",
        "purpose": purpose, "experiment_id": exp.id, "generation": generation,
        "acceptance_record_hash": verdict["acceptance_record_hash"],
        "acceptance": copy.deepcopy(receipt),
        "bundle": copy.deepcopy(bundle), "approval": copy.deepcopy(approval),
        "local_fence": _LOCAL_FENCE, "observed_at_ms": now}
    authority["authority_hash"] = digest(authority)
    return authority


def sync_authority_valid(authority):
    """Last synchronous check. Does not claim to observe remote revocation."""
    try:
        if not isinstance(authority, Mapping) or authority.get("ok") is not True \
                or _LOCAL_PENDING or authority.get("local_fence") != _LOCAL_FENCE:
            return False
        body = {k: v for k, v in authority.items() if k != "authority_hash"}
        if digest(body) != authority.get("authority_hash"):
            return False
        if authority.get("purpose") == "CANARY" and (selected_mode() != "CANDIDATE" \
                or _selected_id() != authority.get("experiment_id")):
            return False
        now = _now()
        valid = authority["purpose"] in PURPOSES and authority["approval"].get("status") == "ACTIVE" \
            and authority["approval"]["limits"]["expires_at_ms"] > now \
            and authority["bundle"]["calibration_artifact"]["generation"]["valid_until_ms"] > now \
            and (authority["approval"].get("test_only") is False or _ALLOW_TEST_APPROVALS)
        if not valid:
            return False
        if authority["purpose"] in ("CANARY", "PROMOTION"):
            report = {**authority["bundle"]["calibration_report"], "acceptance": authority.get("acceptance")}
            checked = _verified_acceptance(report,
                now_ms=now, purpose=authority["purpose"])
            return checked.get("ok") is True and checked.get("valid_until_ms", 0) > now \
                and checked.get("record_hash") == authority.get("acceptance_record_hash")
        return True
    except (ValueError, KeyError, TypeError):
        return False


async def assert_authority_in_session(session, authority=None, now_ms=None, *, exp_id=None,
                                     purpose="CANARY", approval_id=None, expected_generation=None,
                                     require_purpose="CANARY"):
    """State→experiment rowlocks, retained through caller's protected write.

    Does NOT acquire505202609 inside a financial917283 transaction. Writers
    also lock singleton before experiment, so a revocation cannot interleave.
    """
    try:
        now = _now(now_ms)
        if _LOCAL_PENDING:
            return _failure("LOCAL_AUTHORITY_FAILURE_PENDING")
        if authority is not None:
            if not sync_authority_valid(authority):
                return _failure("LOCAL_AUTHORITY_INVALID")
            # Operação exige propósito CANARY: um contexto SHADOW (observação)
            # jamais autoriza reserva, alavancagem ou POST.
            if require_purpose is not None \
                    and authority.get("purpose") != require_purpose:
                return _failure("AUTHORITY_PURPOSE_MISMATCH")
            exp_id, purpose, approval_id, expected_generation = (authority["experiment_id"],
                authority["purpose"], authority["approval"]["approval_id"], authority["generation"])
        state = await _state(session)
        exp = await _experiment(session, exp_id)
        verdict = await approval_for_in_session(session, exp, purpose, now,
                            approval_id=approval_id, expected_generation=expected_generation,
                            operational_state=state)
        if not verdict["ok"]:
            return verdict
        if authority is not None and (authority["bundle"]["bundle_hash"] != verdict["bundle"]["bundle_hash"]
                or authority.get("acceptance_record_hash") != verdict.get("acceptance_record_hash")):
            return _failure("ACCEPTANCE_AUTHORITY_IDENTITY_DRIFT")
        if purpose == "CANARY":
            if exp.status != "ELIGIBLE" or not state or state.payload.get("mode") != "CANDIDATE" \
                    or state.payload.get("block_entries") is not False \
                    or state.payload.get("experiment_id") != exp_id \
                    or state.payload.get("bundle_hash") != verdict["bundle"]["bundle_hash"] \
                    or state.payload.get("acceptance_record_hash") != verdict.get("acceptance_record_hash") \
                    or not _hash(state.payload.get("promotion_approval_id")):
                return _failure("CANDIDATE_NOT_PROMOTED")
            promotion = await approval_for_in_session(session, exp, "PROMOTION", now,
                approval_id=state.payload["promotion_approval_id"],
                expected_generation=state.generation, operational_state=state)
            if not promotion["ok"] or promotion["bundle"]["bundle_hash"] != verdict["bundle"]["bundle_hash"] \
                    or promotion.get("acceptance_record_hash") != verdict.get("acceptance_record_hash") \
                    or not _hash(state.payload.get("prospective_fingerprint")):
                return _failure("PROMOTION_AUTHORITY_NO_LONGER_VALID")
        if not sync_authority_valid(verdict):
            return _failure("FINAL_AUTHORITY_NO_LONGER_VALID")
        if _LOCAL_PENDING or verdict["local_fence"] != _LOCAL_FENCE:
            return _failure("LOCAL_AUTHORITY_FENCE_ADVANCED")
        return verdict
    except Exception:
        return _failure("AUTHORITY_READ_ERROR")


async def load_view(session_factory, experiment_id=None, purpose="CANARY", now_ms=None):
    mode = selected_mode()
    if purpose == "CANARY" and mode in ("LEGACY", "OFF"):
        return {"ok": True, "mode": mode, "generation": None, "bundle": None, "approval": None, "authority_hash": None}
    if mode == "INVALID" or purpose not in PURPOSES:
        return _failure("OPERATIONAL_SELECTOR_INVALID", mode=mode)
    exp_id = _selected_id() if purpose == "CANARY" else experiment_id
    if purpose == "CANARY" and experiment_id is not None and experiment_id != exp_id:
        return _failure("SELECTED_EXPERIMENT_MISMATCH")
    try:
        async with session_factory() as session:
            await _lock(session)
            state = await _state(session)
            if state is None:
                return _failure("OPERATIONAL_STATE_MISSING")
            exp = await _experiment(session, exp_id)
            active = [a for a in _op(exp).get("approvals", []) if a.get("status") == "ACTIVE" and a.get("purpose") == purpose] if exp else []
            if len(active) != 1:
                return _failure("HUMAN_APPROVAL_NOT_UNIQUE")
            approval_id = active[0]["approval_id"]
            result = await assert_authority_in_session(session, now_ms=now_ms, exp_id=exp_id,
                purpose=purpose, approval_id=approval_id, expected_generation=state.generation)
            # Scope ends by rollback only; no POST/refit/cache/activation on GET.
        if result.get("ok") is True and result.get("local_fence") != _LOCAL_FENCE or _LOCAL_PENDING:
            return _failure("LOCAL_AUTHORITY_FENCE_ADVANCED")
        # Rollback/connection cleanup are awaits too. An intact fence does not
        # preserve an approval that expired while leaving the read transaction.
        if result.get("ok") is True and not sync_authority_valid(result):
            return _failure("FINAL_AUTHORITY_NO_LONGER_VALID")
        return result
    except Exception:
        return _failure("AUTHORITY_READ_ERROR")


async def revoke_approval(session_factory, exp_id, approval_id, expected_generation, operator, reason="HUMAN_REVOCATION"):
    if not _text(operator) or not _text(reason) or not _hash(approval_id) or type(expected_generation) is not int:
        return _failure("REVOCATION_INVALID")
    ticket = _begin_mutation()  # Invalidates local authority BEFORE any await, even error.
    try:
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session, True), await _experiment(session, exp_id)
            if state.generation != expected_generation:
                raise ValueError("OPERATIONAL_GENERATION_STALE")
            op = _op(exp) if exp else {}
            approval = next((a for a in op.get("approvals", []) if a.get("approval_id") == approval_id), None)
            if not approval:
                raise ValueError("HUMAN_APPROVAL_NOT_FOUND")
            if approval.get("status") == "REVOKED":
                current = int(state.generation)
                await session.rollback()
                _mutation_finished(ticket)
                return {"ok": True, "idempotent": True, "generation": current}
            approval["status"] = "REVOKED"
            approval.setdefault("revocations", []).append({"operator": operator, "reason": reason, "recorded_at_ms": _now()})
            _write_op(exp, op)
            selected = state.payload.get("experiment_id") == exp_id and approval.get("purpose") in ("PROMOTION", "CANARY")
            generation = _advance(state, "REVOKE", exp_id=exp_id, approval_id=approval_id,
                operator=operator, reason=reason, mode="BLOCKED" if selected else None,
                block_entries=True if selected else None)
            await session.commit()
        _mutation_finished(ticket)
        return {"ok": True, "generation": generation, "preserved_positions_and_protection": True}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def promote(session_factory, exp_id, approval_id, expected_generation, operator=None):
    if type(expected_generation) is not int or not _hash(approval_id):
        return _failure("PROMOTION_IDENTITY_INVALID")
    ticket = _begin_mutation()
    try:
        async with session_factory() as session:
            await _lock(session)
            state, exp = await _state(session, True), await _experiment(session, exp_id)
            if state.generation != expected_generation:
                raise ValueError("OPERATIONAL_GENERATION_STALE")
            authority = await approval_for_in_session(session, exp, "PROMOTION", _now(),
                approval_id=approval_id, expected_generation=expected_generation, operational_state=state)
            if not authority["ok"]:
                raise ValueError(authority["reason_code"])
            from services import prospective_shadow_service as prospective
            evidence = await prospective.load_prospective_evidence(session, exp, now_ms=_now())
            if evidence.get("available") is not True or evidence.get("source") != "REAL_PROSPECTIVE" \
                    or evidence.get("gate", {}).get("verdict") != "GO_CANDIDATE" \
                    or not _hash(evidence.get("fingerprint")):
                raise ValueError("PROSPECTIVE_GO_NO_GO_NOT_PASSED")
            final = await approval_for_in_session(session, exp, "PROMOTION", _now(),
                approval_id=approval_id, expected_generation=expected_generation, operational_state=state)
            if not final.get("ok") or final.get("acceptance_record_hash") != authority.get("acceptance_record_hash") \
                    or final.get("bundle", {}).get("bundle_hash") != authority["bundle"]["bundle_hash"]:
                raise ValueError("PROMOTION_AUTHORITY_NO_LONGER_VALID")
            authority = final
            if exp.status not in ("SHADOW", "ELIGIBLE"):
                raise ValueError("PROMOTION_STATE_INVALID")
            if state.payload.get("mode") == "CANDIDATE" and state.payload.get("experiment_id") == exp_id \
                    and state.payload.get("promotion_approval_id") == approval_id and exp.status == "ELIGIBLE":
                current = int(state.generation)
                await session.rollback()
                _mutation_finished(ticket)
                return {"ok": True, "idempotent": True, "generation": current}
            exp.status = "ELIGIBLE"
            exp.decided_at = datetime.now(timezone.utc)
            generation = _advance(state, "PROMOTE", exp_id=exp_id, approval_id=approval_id,
                operator=authority["approval"]["operator"], mode="CANDIDATE", block_entries=False)
            state.payload = {**state.payload, "promotion_approval_id": approval_id,
                "bundle_hash": authority["bundle"]["bundle_hash"],
                "acceptance_record_hash": authority["acceptance_record_hash"],
                "prospective_fingerprint": evidence.get("fingerprint")}
            await session.commit()
        _mutation_finished(ticket)
        return {"ok": True, "generation": generation, "status": "ELIGIBLE", "selector_env_unchanged": True}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def rollback(session_factory, expected_generation, operator, reason, block_entries=True):
    if type(expected_generation) is not int or not _text(operator) or not _text(reason) or type(block_entries) is not bool:
        return _failure("ROLLBACK_INVALID")
    ticket = _begin_mutation()
    try:
        async with session_factory() as session:
            await _lock(session)
            state = await _state(session, True)
            if state.generation != expected_generation:
                raise ValueError("OPERATIONAL_GENERATION_STALE")
            generation = _advance(state, "ROLLBACK", operator=operator, reason=reason,
                                   mode="BLOCKED" if block_entries else "LEGACY", block_entries=block_entries)
            await session.commit()
        _mutation_finished(ticket, recover=True)
        return {"ok": True, "generation": generation, "mode": "BLOCKED" if block_entries else "LEGACY",
                "selector_env_unchanged": True, "preserved_positions_and_protection": True}
    except Exception as exc:
        _mutation_finished(ticket, failed=not isinstance(exc, ValueError))
        return _failure(str(exc) if isinstance(exc, ValueError) else "GOVERNANCE_PERSISTENCE_ERROR",
                        detail=None if isinstance(exc, ValueError) else type(exc).__name__)


async def get_status(session_factory):
    try:
        async with session_factory() as session:
            await _lock(session)
            state = await _state(session)
            data = state.payload if state else {}
            report, manifest, read_error, validity_hash = None, None, None, None
            if type(data.get("experiment_id")) is int:
                exp = await _experiment(session, data["experiment_id"])
                bundle = _op(exp).get("bundle") if exp else None
                if isinstance(bundle, Mapping):
                    try:
                        await _persisted_bundle_matches(session, bundle)
                        report, manifest = _acceptance_report(exp, bundle), bundle.get("manifest")
                        validity_hash = approval_validity_hash(bundle, "PROMOTION",
                            (report.get("acceptance") or {}).get("record_hash"))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        read_error = "ACCEPTANCE_PERSISTED_RECORD_DRIFT"
            from services.research_batch_service import acceptance_status
            return {"ok": True, "selector": selected_mode(), "mode": data.get("mode", "LEGACY"),
                "generation": state.generation if state else 0, "experiment_id": data.get("experiment_id"),
                "approval_id": data.get("approval_id"), "block_entries": data.get("block_entries", False),
                "local_failure_pending": _LOCAL_PENDING, "no_orders_executed": True,
                "research_acceptance": acceptance_status(report=report, manifest=manifest, read_error=read_error),
                "approval_validity_hash": validity_hash}
    except Exception:
        return _failure("OPERATIONAL_STATUS_UNAVAILABLE")
