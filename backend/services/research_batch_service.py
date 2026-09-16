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
    return result
