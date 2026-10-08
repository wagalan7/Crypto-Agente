"""R08B/R09/R10A: composição somente leitura, sem decisão operacional.

Um relatório de coleta não aprova estratégia. Resultados do teste final não
são consultados. Reutiliza as APIs do lote, sem novo scheduler ou cache.
"""
from __future__ import annotations

import asyncio


async def get_research_status(days: int = 30) -> dict:
    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError, OverflowError):
        days = 30
    result = {
        "schema_version": 1,
        "mode": "OBSERVATION_AND_OFFLINE_RESEARCH",
        "live_changed": False,
        "promotable": False,
        "holdout_status": "SEALED",
        "days": days,
        "limitations": [
            "O funil começa nas recomendações entregues ao executor; não é todo o mercado.",
            "Contadores antigos são eventos; oportunidades únicas começam neste lote.",
            "Sinais vetados ficam fora da calibração e do aprendizado operacional.",
            "Trajetória das vetadas usa horizonte curto de pesquisa em velas de 5m, não o time-stop LIVE.",
            "Replay de OHLCV não reproduz fills reais nem prova melhora de lucro.",
        ],
    }
    try:
        from services.decision_observation_service import get_status
        result["decision_funnel"] = await asyncio.wait_for(
            get_status(days=days), timeout=3.0)
    except Exception:
        result["decision_funnel"] = {
            "state": "UNAVAILABLE", "scope": "POST_SELECTION",
            "reason_code": "OBSERVATION_READ_UNAVAILABLE",
        }
    try:
        from services.offline_replay_service import replay_manifest
        result["offline_lab"] = replay_manifest()
    except Exception:
        result["offline_lab"] = {
            "state": "UNAVAILABLE", "promotable": False,
            "reason_code": "LAB_MANIFEST_UNAVAILABLE",
        }
    try:
        from services.research_study_service import load_latest_study
        import db
        if db.DB_ENABLED:
            persisted = await asyncio.wait_for(load_latest_study(db.get_session), timeout=3.0)
        else:
            persisted = {"available": True, "artifact": None, "manifest": None,
                         "reason_code": "DB_DISABLED"}
        result["lote_final"] = lote_final_summary(
            artifact=persisted.get("artifact"), manifest=persisted.get("manifest"),
            report=persisted.get("report"),
            read_error=None if persisted.get("available") else persisted.get("reason_code"))
    except Exception:
        result["lote_final"] = {"state": "UNAVAILABLE", "promotable": False,
                                "reason_code": "LOTE_SUMMARY_UNAVAILABLE"}
    return result


# ── Resumo do lote final (somente leitura, fail-soft por item) ──────────────
LOTE_SCHEMA_VERSION = 1

#: Estados da evidência. `ERROR` NÃO é `NOT_STARTED`: falha de leitura é falha,
#: não ausência de trabalho. E nada aqui dispara replay/fitting dentro do GET —
#: só lê o que já foi calculado e persistido/registrado.
EVIDENCE_NOT_STARTED = "NOT_STARTED"
EVIDENCE_OBSERVING = "OBSERVING"
EVIDENCE_COLLECTED = "COLLECTED"
EVIDENCE_FITTED = "FITTED"
EVIDENCE_OOS_VALIDATED = "OOS_VALIDATED"
EVIDENCE_ERROR = "ERROR"
EVIDENCE_BLOCKED = "BLOCKED_MISSING_DECISION"


def acceptance_status(*, report=None, manifest=None, read_error=None, now_ms=None) -> dict:
    """Separate acceptance from ArtifactV1 state; cheap persisted-record reads only."""
    policy = {"available": False, "reason_code": "ACCEPTANCE_POLICY_UNAVAILABLE"}
    record = {"available": False,
              "state": "WAITING_PARAMETERS" if manifest is None else "NO_DATA",
              "reason_code": "ACCEPTANCE_RECORD_MISSING", "record_hash": None,
              "calibration_state": "UNAVAILABLE", "economics_state": "UNAVAILABLE"}
    approved = False
    try:
        from services import research_acceptance_service as acceptance
        policy = {"available": True, **acceptance.policy_manifest()}
        if report is not None:
            checked = acceptance.verify_acceptance(report, now_ms=now_ms, purpose="PROMOTION")
            raw = report.get("acceptance") if isinstance(report, dict) else None
            record = {"available": isinstance(raw, dict),
                      "state": checked.get("state", "UNACCEPTED"),
                      "reason_code": checked.get("reason_code"),
                      "record_hash": checked.get("record_hash"),
                      "calibration_state": checked.get("calibration_state", "UNAVAILABLE"),
                      "economics_state": checked.get("economics_state", "UNAVAILABLE"),
                      "valid_until_ms": checked.get("valid_until_ms"),
                      "verified": checked.get("ok") is True}
            approved = checked.get("ok") is True and checked.get("economics_state") == "ACCEPTED"
        if read_error:
            record.update(state="ERROR", reason_code=read_error, verified=False)
            approved = False
    except Exception:
        record.update(state="ERROR", reason_code="ACCEPTANCE_READ_UNAVAILABLE", verified=False)
    return {"policy": policy, "record": record, "economically_approved": approved,
            "live_approved": False, "computed_in_request": False,
            "expensive_work_in_get": False}


def evidence_status(*, artifact=None, manifest=None, report=None, read_error=None) -> dict:
    """Evidência DERIVADA do que existe de fato, com qualidade e último instante.

    A simulação prospectiva vem da cobertura/coleta REGISTRADA pelo coletor; a
    avaliação OOS, do estado do artefato de calibração (quando houver); o aceite
    econômico, do registro separado vinculado ao estudo, nunca de OOS_VALIDATED; a
    aprovação humana, do manifesto autorizado. Leitura indisponível vira
    `ERROR`, nunca `NOT_STARTED`.
    """
    prospectiva = {"state": EVIDENCE_ERROR, "quality": "UNKNOWN",
                   "last_observed_at": None,
                   "reason_code": "OBSERVATION_READ_UNAVAILABLE"}
    try:
        from services import preselection_observation_service as pre
        cobertura = pre.coverage_snapshot()
        ligada = pre.collection_enabled()
        observados = int(cobertura.get("candidates_observed") or 0)
        if observados > 0:
            estado = EVIDENCE_COLLECTED
        elif ligada:
            estado = EVIDENCE_OBSERVING
        else:
            estado = EVIDENCE_NOT_STARTED
        prospectiva = {
            "state": estado,
            "quality": ("PARTIAL" if cobertura.get("last_state") not in
                        (None, pre.COVERAGE_COMPLETE) else
                        ("OK" if observados else "EMPTY")),
            "last_observed_at": cobertura.get("last_cycle_ts_ms"),
            "collection_enabled": ligada,
            "cycles_observed": int(cobertura.get("cycles") or 0),
            "candidates_observed": observados,
            "coverage_reasons": dict(cobertura.get("reasons") or {}),
            "reason_code": None if observados else "NO_PROSPECTIVE_SAMPLE_YET",
        }
    except Exception:
        pass

    economica = {"state": EVIDENCE_NOT_STARTED, "quality": "UNKNOWN",
                 "last_observed_at": None,
                 "reason_code": "CALIBRATION_ARTIFACT_MISSING"}
    try:
        from services import score_v3_calibration_service as calib
        if artifact is None:
            economica["reason_code"] = calib.ARTIFACT_MISSING
        else:
            verdict = calib.verify_artifact(artifact)
            economica = {
                "state": (EVIDENCE_OOS_VALIDATED
                          if verdict.get("state") == calib.STATE_OOS_VALIDATED
                          else EVIDENCE_FITTED if verdict.get("ok")
                          else EVIDENCE_ERROR),
                "quality": "OOS" if verdict.get("state") == calib.STATE_OOS_VALIDATED
                else ("IN_SAMPLE" if verdict.get("ok") else "UNKNOWN"),
                "last_observed_at": ((artifact.get("generation") or {})
                                     .get("generated_at_ms")
                                     if isinstance(artifact, dict) else None),
                "reason_code": verdict.get("reason_code"),
                "economically_approved": False,
            }
    except Exception:
        economica = {"state": EVIDENCE_ERROR, "quality": "UNKNOWN",
                     "last_observed_at": None,
                     "reason_code": "CALIBRATION_READ_UNAVAILABLE"}
    if read_error:
        economica = {"state": EVIDENCE_ERROR, "quality": "UNKNOWN",
                     "last_observed_at": None, "reason_code": read_error,
                     "economically_approved": False}

    humana = {"state": EVIDENCE_BLOCKED, "quality": "UNKNOWN",
              "last_observed_at": None,
              "reason_code": "AUTHORIZED_CANDIDATE_NOT_DECLARED"}
    try:
        from services import research_manifest_service as rm
        autorizado = rm.authorized_comparison(manifest)
        humana = {"state": (autorizado.get("state") if autorizado.get("available")
                            else EVIDENCE_BLOCKED),
                  "quality": ("APPROVED" if autorizado.get("real_study_allowed")
                              else "TEST_ONLY" if autorizado.get("available")
                              else "UNKNOWN"),
                  "last_observed_at": ((autorizado.get("decision") or {})
                                       .get("recorded_at_ms")),
                  "reason_code": autorizado.get("reason_code"),
                  "real_study_allowed": bool(autorizado.get("real_study_allowed"))}
    except Exception:
        humana = {"state": EVIDENCE_ERROR, "quality": "UNKNOWN",
                  "last_observed_at": None,
                  "reason_code": "MANIFEST_READ_UNAVAILABLE"}

    acceptance = acceptance_status(report=report, manifest=manifest, read_error=read_error)
    economica["economically_approved"] = acceptance["economically_approved"]
    return {
        "derived": True, "computed_in_request": False,
        "expensive_work_in_get": False,
        "prospective_simulation": prospectiva,
        "economic_validation": economica,
        "research_acceptance": acceptance,
        "human_approval": humana,
        "canary": {"state": "NOT_PREPARED_FOR_APPLY", "quality": "UNKNOWN",
                   "last_observed_at": None,
                   "reason_code": "CANARY_REQUIRES_SPECIFIC_AUTHORIZATION"},
    }


def _safe(label: str, loader) -> dict:
    """Cada linha do resumo falha sozinha: um import quebrado não derruba o GET."""
    try:
        return {"state": "OK", **loader()}
    except Exception:
        return {"state": "UNAVAILABLE", "reason_code": f"{label}_MANIFEST_UNAVAILABLE"}


def lote_final_summary(*, artifact=None, manifest=None, report=None, read_error=None) -> dict:
    """Versões, modo, cobertura, bloqueios, evidência e próximo passo.

    Nada aqui muda o significado de "bot opera": candidato em simulação não é
    operação. Sem botão, sem rota nova, sem escrita.
    """
    blocks = []

    def bloco(letra: str, nome: str, contrato: str, loader) -> None:
        blocks.append({"block": letra, "name": nome, "contract": contrato,
                       **_safe(letra, loader)})

    def bloco_a():
        from services import entry_intent_service as p03
        return {"version": p03.SCHEMA_VERSION, "mode": "ACTIVE",
                "active_by_default": True,
                "note": "correção de segurança: uma entrada econômica por decisão"}

    def bloco_b():
        from services import financial_total_service as r05d
        return {"version": r05d.CONTRACT_VERSION, "mode": r05d.selected_source(),
                "active_by_default": not r05d.accounting_total_enabled()}

    def bloco_c():
        from services import robust_policy_service as r11c
        return {"version": r11c.POLICY_VERSION, "mode": r11c.selected_policy(),
                "active_by_default": r11c.selected_policy() == r11c.POLICY_LEGACY}

    def bloco_d():
        from services import strategy_core_service as core
        from services import score_v3_service as s3
        return {"version": core.CORE_VERSION, "mode": core.selected_mode(),
                "score_version": s3.SCORE_VERSION, "score_mode": s3.selected_mode(),
                "config_hash": core.DEFAULT_CONFIG.config_hash()[:12],
                "playbooks": list(core.PLAYBOOKS), "executable": False}

    def bloco_e():
        from services import preselection_observation_service as pre
        manifest = pre.preselection_manifest()
        return {"version": manifest["schema_version"], "mode": manifest["mode"],
                "collection_enabled": manifest["enabled"],
                "legacy_scope_preserved": manifest["legacy_scope_preserved"]}

    def bloco_f():
        from services import portfolio_replay_service as pf
        from services import walk_forward_service as wf
        return {"version": pf.PORTFOLIO_VERSION, "mode": pf.selected_mode(),
                "walk_forward_version": wf.WF_VERSION,
                "live_equivalent": False,
                "holdout": wf.final_evaluation_boundary()["real_holdout"]}

    def bloco_g():
        from services import preselection_experiment_service as r12
        manifest = r12.r12_manifest()
        return {"version": manifest["r12_version"], "mode": manifest["mode"],
                "criteria_hash": manifest["criteria_hash"][:12],
                "endpoints_added": manifest["endpoints_added"]}

    bloco("A", "P03 entrada idempotente", "SAFETY_FIX", bloco_a)
    bloco("B", "R05 total com funding", "CANDIDATE_POLICY", bloco_b)
    bloco("C", "R11 política robusta", "CANDIDATE_POLICY", bloco_c)
    bloco("D", "R07/R08 núcleo e Score V3", "CANDIDATE_POLICY", bloco_d)
    bloco("E", "R09 evidência pré-seleção", "OBSERVATION_ONLY", bloco_e)
    bloco("F", "R10 replay e walk-forward", "CANDIDATE_POLICY", bloco_f)
    bloco("G", "R12 simulação e go/no-go", "CANDIDATE_POLICY", bloco_g)

    inativos = [row["block"] for row in blocks
                if row.get("mode") in ("inactive", "legacy")]
    indisponiveis = [row["block"] for row in blocks if row["state"] != "OK"]
    return {
        "schema_version": LOTE_SCHEMA_VERSION,
        "state": "LOCAL_RESEARCH_ONLY",
        "blocks": blocks,
        "candidate_versions_inactive": inativos,
        "unavailable_blocks": indisponiveis,
        "live_changed": False,
        "promotable": False,
        "live_approval": "UNAVAILABLE",
        "bot_operation_meaning_unchanged": True,
        "evidence": evidence_status(artifact=artifact, manifest=manifest, report=report, read_error=read_error),
        "blockers": ["Sem coleta prospectiva, não há evidência econômica.",
                     "Aprovação humana e canário continuam pendentes."],
        "next_step": ("Ligar a coleta pré-seleção em simulação e acumular a amostra "
                      "do gate (100 trades/30 por playbook) antes de qualquer canário."),
    }
