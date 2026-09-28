"""R11/R12 — estado PERSISTENTE da simulação (histerese e geração publicada).

O núcleo da política (`robust_policy_service`) é PURO por contrato: sem I/O,
sem relógio implícito. A persistência mora aqui, separada, porque estado que só
existe em dataclass não sobrevive a restart e não prova concorrência.

Escreve APENAS `policy_simulation_state`: nada de universo operacional, learned
cache ou risco legado. A exclusividade é transacional — chave única mais
compare-and-set sob advisory lock próprio.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services.robust_policy_service import POLICY_VERSION, POPULATION_SHADOW


#: Lock transacional próprio: distinto do 917283 (P03/risco) e do R09.
POLICY_ADVISORY_LOCK_KEY = 0x52313143   # "R11C"


def state_key(*, experiment_key: str, universe_version: str,
              population: str = POPULATION_SHADOW) -> str:
    return f"{experiment_key}|{POLICY_VERSION}|{universe_version}|{population}"


async def read_state(session_factory, *, experiment_key: str, universe_version: str,
                     population: str = POPULATION_SHADOW) -> Dict[str, Any]:
    """Leitura EXPLÍCITA do estado publicado.

    `available=False` é FALHA DE LEITURA — não é "primeiro estado". Confundir os
    dois reiniciaria a histerese em silêncio a cada erro de banco. Ausência de
    linha é `available=True, found=False` (nunca zero implícito). A identidade
    gravada é conferida: linha de outra política/universo/população não vale.
    """
    from sqlalchemy import select
    from models.policy_simulation_state import PolicySimulationState
    key = state_key(experiment_key=experiment_key, universe_version=universe_version,
                    population=population)
    try:
        async with session_factory() as session:
            row = (await session.execute(
                select(PolicySimulationState)
                .where(PolicySimulationState.state_key == key))).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "found": None, "state": None,
                "reason_code": "STATE_READ_ERROR", "error": type(exc).__name__}
    if row is None:
        return {"available": True, "found": False, "state": None,
                "reason_code": "NO_STATE"}
    if (row.policy_version != POLICY_VERSION or row.universe_version != universe_version
            or row.population != population):
        return {"available": True, "found": False, "state": None,
                "reason_code": "STATE_IDENTITY_MISMATCH"}
    return {"available": True, "found": True, "reason_code": "STATE_LOADED",
            "state": {"state_key": row.state_key, "generation": int(row.generation or 0),
                      "evidence_key": row.evidence_key, "period_key": row.period_key,
                      "published_at_ms": row.published_at_ms, "payload": row.payload,
                      "policy_version": row.policy_version,
                      "universe_version": row.universe_version,
                      "population": row.population}}


async def load_state(session_factory, *, experiment_key: str, universe_version: str,
                     population: str = POPULATION_SHADOW) -> Optional[Dict[str, Any]]:
    """Estado publicado, como está no banco. Sem linha ⇒ None (nunca zero).

    Conveniência de leitura: quem precisa DISTINGUIR ausência de falha usa
    `read_state` — aqui as duas devolvem None de propósito.
    """
    verdict = await read_state(session_factory, experiment_key=experiment_key,
                              universe_version=universe_version, population=population)
    return verdict.get("state")


async def publish_generation(session_factory, *, experiment_key: str,
                             universe_version: str, period_key: str, evidence_key: str,
                             now_ms: int, population: str = POPULATION_SHADOW,
                             payload: Optional[Dict[str, Any]] = None,
                             expected_generation: Optional[int] = None) -> Dict[str, Any]:
    """Publica uma geração NOVA de forma atômica e com histerese persistente.

    `expected_generation` é a geração sobre a qual o CÁLCULO foi feito: se o
    estado avançou desde a leitura, a escrita é RECUSADA (`GENERATION_STALE`) —
    período diferente não autoriza publicar um cálculo de versão antiga por
    cima do avanço de outro processo. Quem for recusado relê e recalcula.

    Exige período NOVO **e** evidência NOVA — repetir a chamada no mesmo período
    ou com a mesma evidência é no-op idempotente, não uma geração a mais. A
    publicação e a invalidação do cache acontecem na MESMA transação, sob
    advisory lock: duas simulações concorrentes não publicam duas gerações.

    Não escreve universo operacional, learned cache nem risco legado.
    """
    from sqlalchemy import select, text
    from models.policy_simulation_state import PolicySimulationState
    key = state_key(experiment_key=experiment_key, universe_version=universe_version,
                    population=population)
    moment = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                  {"k": POLICY_ADVISORY_LOCK_KEY})
            row = (await session.execute(
                select(PolicySimulationState)
                .where(PolicySimulationState.state_key == key)
                .with_for_update())).scalar_one_or_none()
            if row is None:
                if expected_generation not in (None, 0):
                    # O cálculo dizia partir de uma geração que não existe aqui.
                    await session.rollback()
                    return {"published": False, "generation": 0,
                            "reason_code": "GENERATION_STALE",
                            "expected_generation": expected_generation,
                            "current_generation": 0}
                row = PolicySimulationState(
                    state_key=key, experiment_key=experiment_key,
                    policy_version=POLICY_VERSION, universe_version=universe_version,
                    population=population, generation=1, evidence_key=evidence_key,
                    period_key=period_key, published_at_ms=now_ms,
                    payload=dict(payload or {}), created_at=moment, updated_at=moment)
                session.add(row)
                await session.commit()
                invalidate_cache(key)
                return {"published": True, "generation": 1, "reason_code": "FIRST_GENERATION"}
            current_period, current_evidence = row.period_key, row.evidence_key
            generation = int(row.generation or 0)
            if expected_generation is not None and int(expected_generation) != generation:
                # CAS pela geração LIDA: o cálculo é de uma versão superada.
                await session.rollback()
                return {"published": False, "generation": generation,
                        "reason_code": "GENERATION_STALE",
                        "expected_generation": int(expected_generation),
                        "current_generation": generation}
            if current_period == period_key:
                await session.rollback()
                return {"published": False, "generation": generation,
                        "reason_code": "PERIOD_UNCHANGED"}
            if current_evidence == evidence_key:
                await session.rollback()
                return {"published": False, "generation": generation,
                        "reason_code": "EVIDENCE_UNCHANGED"}
            row.generation = generation + 1
            row.evidence_key = evidence_key
            row.period_key = period_key
            row.published_at_ms = now_ms
            row.payload = dict(payload or {})
            row.updated_at = moment
            await session.commit()
            # Cache só cai DEPOIS do commit: ninguém lê geração nova sem estado.
            invalidate_cache(key)
            return {"published": True, "generation": generation + 1, "reason_code": OK_PUBLISHED}
    except Exception as exc:  # noqa: BLE001
        return {"published": False, "generation": None,
                "reason_code": "STATE_UNAVAILABLE", "error": type(exc).__name__}


OK_PUBLISHED = "GENERATION_PUBLISHED"
_STATE_CACHE: Dict[str, Any] = {}


def invalidate_cache(key: str) -> None:
    """Invalida o cache local da política para aquela identidade."""
    _STATE_CACHE.pop(key, None)


def cache_put(key: str, value: Any) -> None:
    _STATE_CACHE[key] = value


def cache_get(key: str) -> Any:
    return _STATE_CACHE.get(key)
