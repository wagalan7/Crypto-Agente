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
        result["lote_final"] = lote_final_summary()
    except Exception:
        result["lote_final"] = {"state": "UNAVAILABLE", "promotable": False,
                                "reason_code": "LOTE_SUMMARY_UNAVAILABLE"}
    return result


# ── Resumo do lote final (somente leitura, fail-soft por item) ──────────────
LOTE_SCHEMA_VERSION = 1


def _safe(label: str, loader) -> dict:
    """Cada linha do resumo falha sozinha: um import quebrado não derruba o GET."""
    try:
        return {"state": "OK", **loader()}
    except Exception:
        return {"state": "UNAVAILABLE", "reason_code": f"{label}_MANIFEST_UNAVAILABLE"}


def lote_final_summary() -> dict:
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
        "evidence": {"prospective_simulation": "NOT_STARTED",
                     "economic_validation": "NOT_STARTED",
                     "human_approval": "NOT_REQUESTED",
                     "canary": "NOT_PREPARED_FOR_APPLY"},
        "blockers": ["Sem coleta prospectiva, não há evidência econômica.",
                     "Aprovação humana e canário continuam pendentes."],
        "next_step": ("Ligar a coleta pré-seleção em simulação e acumular a amostra "
                      "do gate (100 trades/30 por playbook) antes de qualquer canário."),
    }
