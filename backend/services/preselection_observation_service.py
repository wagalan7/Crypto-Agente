"""R09 pré-seleção — evidência do funil ANTES da seleção, sem mexer no que já existe.

`OBSERVATION_ONLY`, inativo por padrão (`R09_PRESELECTION_MODE`). O escopo
`POST_SELECTION` do R09 e todas as linhas antigas mantêm o significado original:
este módulo só acrescenta um escopo versionado (`PRE_SELECTION`) e devolve
PAYLOADS para o armazenamento, o flush e o resolver que já existem. Não cria
tabela nova, scheduler, worker, fila nem cliente de exchange.

O que ele garante:
  • a sequência observada é a REAL; etapa que não rodou fica `NOT_EVALUATED` —
    nunca `PASSED`. A primeira rejeição é um fato, não uma lista de causas
    contrafactuais;
  • aceitas e vetadas compartilham identidade e fronteira temporal;
  • oportunidade ≠ tentativa ≠ fill, com dedupe determinístico;
  • horizonte vem do timeframe e do MAIOR horizonte entre os candidatos
    registrados — não existe 2h fixo como se fosse LIVE;
  • origem/mercado/símbolo/resolução viajam com a janela: fonte desconhecida
    fica `UNLABELED` e nunca é rotulada como Binance;
  • falha de coleta e completude são COBERTURA; ausência de outcome não é
    prejuízo zero;
  • promoção futura exige contrato explícito: vetada não vira insumo sozinha.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCOPE = "PRE_SELECTION"
LEGACY_SCOPE = "POST_SELECTION"
PRE_SCHEMA_VERSION = "r09.pre.v1"
#: v2 ACRESCENTA, à v1, os blocos `features` (ponto-no-tempo) e `evaluation`
#: (lista de TFs avaliados × TF escolhido). Linha sem esses blocos continua v1,
#: byte a byte — produtor, exportador e verificador aceitam as DUAS versões.
PRE_SCHEMA_VERSION_V2 = "r09.pre.v2"
PRE_SCHEMA_VERSIONS = (PRE_SCHEMA_VERSION, PRE_SCHEMA_VERSION_V2)
POLICY = "OBSERVATION_ONLY"
MODE_ENV = "R09_PRESELECTION_MODE"
MODE_INACTIVE = "inactive"
MODE_OBSERVE = "observe"

# ── Funil pré-seleção, na ordem canônica ────────────────────────────────────
STAGE_CANDIDATE = "CANDIDATE"
STAGE_PLAYBOOK = "PLAYBOOK"
STAGE_CANDLE = "CANDLE"
STAGE_GEOMETRY_RR = "GEOMETRY_RR"
STAGE_LIQUIDITY = "LIQUIDITY"
STAGE_MTF_REGIME = "MTF_REGIME"
STAGE_SELECTION = "SELECTION"
STAGE_RISK = "RISK"
STAGE_EXECUTION = "EXECUTION"
STAGES = (STAGE_CANDIDATE, STAGE_PLAYBOOK, STAGE_CANDLE, STAGE_GEOMETRY_RR,
          STAGE_LIQUIDITY, STAGE_MTF_REGIME, STAGE_SELECTION, STAGE_RISK,
          STAGE_EXECUTION)

VERDICT_PASSED = "PASSED"
VERDICT_REJECTED = "REJECTED"
VERDICT_UNKNOWN = "UNKNOWN"
VERDICT_NOT_EVALUATED = "NOT_EVALUATED"
OBSERVABLE_VERDICTS = (VERDICT_PASSED, VERDICT_REJECTED, VERDICT_UNKNOWN)
VERDICTS = OBSERVABLE_VERDICTS + (VERDICT_NOT_EVALUATED,)

OUTCOME_ACCEPTED = "ACCEPTED"
OUTCOME_VETOED = "VETOED"
OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_VETOED)

# ── Motivos ─────────────────────────────────────────────────────────────────
OK = "OK"
MISSING_IDENTITY = "MISSING_IDENTITY"
STAGE_UNKNOWN = "STAGE_UNKNOWN"
STAGE_DUPLICATED = "STAGE_DUPLICATED"
VERDICT_INVALID = "VERDICT_INVALID"
NOT_EVALUATED = "NOT_EVALUATED"
COLLECTION_DISABLED = "COLLECTION_DISABLED"
CAPACITY_REACHED = "CAPACITY_REACHED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
SOURCE_MISMATCH = "SOURCE_MISMATCH"
SOURCE_UNLABELED = "SOURCE_UNLABELED"
HORIZON_TRUNCATED = "HORIZON_TRUNCATED"
TIMEFRAME_UNKNOWN = "TIMEFRAME_UNKNOWN"
COLLECTION_FAILED = "COLLECTION_FAILED"
COLLECTION_INCOMPLETE = "COLLECTION_INCOMPLETE"
KEEP_COLLECTING = "KEEP_COLLECTING"
PROMOTION_REQUIRES_EXPLICIT_CONTRACT = "PROMOTION_REQUIRES_EXPLICIT_CONTRACT"
DEPENDENCY_INACTIVE = "DEPENDENCY_INACTIVE"
REASON_CODES = frozenset({
    OK, MISSING_IDENTITY, STAGE_UNKNOWN, STAGE_DUPLICATED, VERDICT_INVALID,
    NOT_EVALUATED, COLLECTION_DISABLED, CAPACITY_REACHED, BUDGET_EXCEEDED,
    SOURCE_MISMATCH, SOURCE_UNLABELED, HORIZON_TRUNCATED, TIMEFRAME_UNKNOWN,
    COLLECTION_FAILED, COLLECTION_INCOMPLETE, KEEP_COLLECTING,
    PROMOTION_REQUIRES_EXPLICIT_CONTRACT, DEPENDENCY_INACTIVE,
})

# ── Cobertura ───────────────────────────────────────────────────────────────
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_INCOMPLETE = "INCOMPLETE"
COVERAGE_FAILED = "COLLECTION_FAILED"
COVERAGE_PENDING = "PENDING"

#: Horizonte de PESQUISA: 12 velas do timeframe do setup, resolvidas na grade
#: de 5m que o resolver compartilhado já coleta. Não é o time-stop do LIVE.
RESOLVER_BAR_MINUTES = 5
RESEARCH_HORIZON_CANDLES = 12
MAX_RESEARCH_BARS = 96  # teto do buffer existente (MAX_CANDLES do R09)
TIMEFRAME_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                     "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720,
                     "1d": 1440}

#: Sub-orçamento da coleta pré-seleção. Nada é apagado ao atingir o teto:
#: linhas novas são recusadas e contadas.
PRE_CAPACITY = {"opportunities": 20_000, "attempts": 40_000}
CAPACITY_NEAR_RATIO = 0.9
MAX_BATCH_RECORDS = 50
MAX_BUFFERED_RECORDS = 200
MAX_PAYLOAD_BYTES = 4096
MAX_PAYLOAD_BYTES_V2 = 32768  # trace allowlisted (<=24 KiB) + evidência/config v2

#: Fonte adicional que só entraria com dependência EXPLÍCITA — declarada
#: inativa, sem fetch, sem histórico inventado.
EXTRA_SOURCE = {
    "name": "ORDERBOOK_DEPTH_AT_DECISION",
    "active": False,
    "reason_code": DEPENDENCY_INACTIVE,
    "note": "Profundidade no instante da decisão não existe no acervo atual; "
            "sem ela, liquidez pré-seleção fica UNKNOWN, nunca estimada.",
}

LIMITATIONS = [
    "Escopo PRE_SELECTION é aditivo: POST_SELECTION e linhas antigas não mudam.",
    "Etapa não avaliada não é etapa aprovada.",
    "Primeira rejeição é um fato observado, não uma causa contrafactual.",
    "Ausência de outcome é cobertura incompleta, nunca prejuízo zero.",
    "Horizonte de pesquisa é curto e por timeframe; não é o time-stop do LIVE.",
    "Vetada não alimenta learner, calibração, P&L ou RealTrade.",
]


def selected_mode() -> str:
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_OBSERVE if value == MODE_OBSERVE else MODE_INACTIVE


def collection_enabled() -> bool:
    """Coleta nova é OPCIONAL e desligada por padrão: nada do scan depende dela."""
    return selected_mode() == MODE_OBSERVE


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _label(value: Any, max_len: int = 64) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:max_len] if text else None


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# ── Identidade compartilhada por aceitas e vetadas ──────────────────────────
def pre_selection_identity(*, symbol: Any, timeframe: Any, side: Any,
                           trigger_candle_ms: Any, playbook: Any,
                           playbook_version: Any) -> Tuple[Optional[str], str]:
    """Mesma identidade para aceita e vetada. Sem 'agora' como identidade."""
    fields = {
        "symbol": _label(symbol, 50),
        "timeframe": _label(timeframe, 12),
        "side": _label(side, 8),
        "playbook": _label(playbook, 40),
        "playbook_version": _label(playbook_version, 40),
    }
    candle = _positive_int(trigger_candle_ms)
    if candle is None or any(value is None for value in fields.values()):
        return None, MISSING_IDENTITY
    if fields["side"] not in ("long", "short"):
        return None, MISSING_IDENTITY
    fields["symbol"] = fields["symbol"].upper()
    payload = {**fields, "trigger_candle_ms": candle, "scope": SCOPE,
               "schema_version": PRE_SCHEMA_VERSION}
    return f"pre-{_digest(payload)[:28]}", OK


def attempt_key(identity: str, *, attempt_index: int) -> str:
    """Oportunidade ≠ tentativa: a tentativa carrega a oportunidade e o índice."""
    if not isinstance(identity, str) or not identity.startswith("pre-"):
        raise ValueError("identidade pré-seleção inválida")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 0:
        raise ValueError("attempt_index inválido")
    return f"{identity}#{attempt_index:04d}"


# ── Funil observado ─────────────────────────────────────────────────────────
def record_funnel(observed: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Congela o funil REAL. Etapa não reportada fica NOT_EVALUATED."""
    stages: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    problems: List[str] = []
    rejected_seen = False
    for item in observed or ():
        if not isinstance(item, Mapping):
            problems.append(VERDICT_INVALID)
            continue
        stage = _label(item.get("stage"), 32)
        verdict = _label(item.get("verdict"), 24)
        if stage not in STAGES:
            problems.append(STAGE_UNKNOWN)
            continue
        if stage in stages:
            problems.append(STAGE_DUPLICATED)
            continue
        if verdict not in OBSERVABLE_VERDICTS:
            # NOT_EVALUATED é DERIVADO; ninguém o "observa".
            problems.append(VERDICT_INVALID)
            continue
        entry = {
            "verdict": verdict,
            "reason_code": _label(item.get("reason_code"), 64),
            "observed_at_ms": _positive_int(item.get("observed_at_ms")),
            "observed_position": len(order),
            "after_first_rejection": rejected_seen,
        }
        if verdict == VERDICT_REJECTED:
            rejected_seen = True
        stages[stage] = entry
        order.append(stage)
    for stage in STAGES:
        if stage not in stages:
            stages[stage] = {"verdict": VERDICT_NOT_EVALUATED, "reason_code": NOT_EVALUATED,
                             "observed_at_ms": None, "observed_position": None,
                             "after_first_rejection": None}
    canonical_positions = {stage: index for index, stage in enumerate(STAGES)}
    out_of_order = any(canonical_positions[a] >= canonical_positions[b]
                       for a, b in zip(order, order[1:]))
    blockers = tuple(stage for stage in order
                     if stages[stage]["verdict"] == VERDICT_REJECTED)
    return {
        "schema_version": PRE_SCHEMA_VERSION,
        "scope": SCOPE,
        "canonical_order": list(STAGES),
        "observed_order": list(order),
        "out_of_order": out_of_order,
        "stages": stages,
        "first_blocker": blockers[0] if blockers else None,
        "first_blocker_reason": stages[blockers[0]]["reason_code"] if blockers else None,
        "blockers_observed": list(blockers),
        "stages_not_evaluated": [stage for stage in STAGES
                                 if stages[stage]["verdict"] == VERDICT_NOT_EVALUATED],
        # A primeira rejeição NÃO autoriza afirmar o que teria acontecido depois.
        "causal_claim": "NONE",
        "counterfactual_causes": [],
        "problems": sorted(set(problems)),
    }


# ── Horizonte por timeframe ─────────────────────────────────────────────────
def horizon_bars(timeframe: Any) -> Dict[str, Any]:
    """Horizonte em barras de 5m, derivado do TF — nunca 2h fixo."""
    label = _label(timeframe, 12)
    minutes = TIMEFRAME_MINUTES.get(label) if label else None
    if minutes is None:
        return {"bars": None, "reason_code": TIMEFRAME_UNKNOWN, "truncated": False,
                "timeframe": label}
    wanted = math.ceil(minutes * RESEARCH_HORIZON_CANDLES / RESOLVER_BAR_MINUTES)
    bars = min(wanted, MAX_RESEARCH_BARS)
    return {"bars": bars, "requested_bars": wanted,
            "reason_code": HORIZON_TRUNCATED if bars < wanted else OK,
            "truncated": bars < wanted, "timeframe": label,
            "semantics": "SHORT_RESEARCH_HORIZON_NOT_LIVE_TIME_STOP"}


def required_horizon(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """MAIOR horizonte entre os candidatos REGISTRADOS."""
    bars = 0
    unknown: List[str] = []
    truncated = False
    for candidate in candidates or ():
        verdict = horizon_bars((candidate or {}).get("timeframe"))
        if verdict["bars"] is None:
            unknown.append(_label((candidate or {}).get("candidate_id"), 64) or "?")
            continue
        truncated = truncated or verdict["truncated"]
        bars = max(bars, verdict["bars"])
    return {"bars": bars or None, "unknown_timeframes": unknown, "truncated": truncated,
            "reason_code": TIMEFRAME_UNKNOWN if unknown else OK}


def collection_complete(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Coleta só encerra quando TODOS os candidatos registrados encerram.

    O baseline resolver antes não encerra a janela: outro candidato registrado
    pode exigir horizonte maior.
    """
    pending: List[str] = []
    for candidate in candidates or ():
        candidate = candidate if isinstance(candidate, Mapping) else {}
        name = _label(candidate.get("candidate_id"), 64) or "?"
        horizon = horizon_bars(candidate.get("timeframe"))
        collected = _positive_int(candidate.get("bars_collected"))
        collected = 0 if collected is None else collected
        if candidate.get("terminal") is True:
            continue
        if horizon["bars"] is not None and collected >= horizon["bars"]:
            continue
        pending.append(name)
    if pending:
        return {"complete": False, "reason_code": KEEP_COLLECTING,
                "pending_candidates": pending}
    return {"complete": True, "reason_code": OK, "pending_candidates": []}


# ── Cobertura ───────────────────────────────────────────────────────────────
def coverage_verdict(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Cobertura é métrica de COLETA. Sem outcome não existe resultado zero."""
    row = row if isinstance(row, Mapping) else {}
    if row.get("collection_error"):
        return {"coverage": COVERAGE_FAILED, "reason_code": COLLECTION_FAILED,
                "outcome_available": False, "pnl_assumption": None}
    completion = collection_complete(row.get("candidates") or ())
    outcome = row.get("outcome")
    if not completion["complete"]:
        return {"coverage": COVERAGE_INCOMPLETE, "reason_code": COLLECTION_INCOMPLETE,
                "outcome_available": isinstance(outcome, Mapping),
                "pending_candidates": completion["pending_candidates"],
                "pnl_assumption": None}
    if not isinstance(outcome, Mapping):
        return {"coverage": COVERAGE_PENDING, "reason_code": COLLECTION_INCOMPLETE,
                "outcome_available": False, "pnl_assumption": None}
    return {"coverage": COVERAGE_COMPLETE, "reason_code": OK,
            "outcome_available": True, "pnl_assumption": None}


# ── Origem da janela ────────────────────────────────────────────────────────
def window_source(decision_source: Any, candle_source: Any, *, symbol: Any,
                  resolution: Any) -> Dict[str, Any]:
    """Origem/mercado/símbolo/resolução viajam juntos. Sem rótulo, UNLABELED."""
    decision = _label(decision_source, 40)
    candle = _label(candle_source, 40)
    payload = {"decision_source": decision, "candle_source": candle,
               "symbol": _label(symbol, 50), "resolution": _label(resolution, 12)}
    if decision is None or candle is None:
        return {**payload, "label": "UNLABELED", "reason_code": SOURCE_UNLABELED,
                "usable": False}
    if decision.strip().lower() != candle.strip().lower():
        return {**payload, "label": "UNLABELED", "reason_code": SOURCE_MISMATCH,
                "usable": False}
    return {**payload, "label": decision, "reason_code": OK, "usable": True}


# ── Admissão, orçamento e payload ───────────────────────────────────────────
def admission_verdict(used: Optional[Mapping[str, Any]] = None, *,
                      capacity: Optional[Mapping[str, int]] = None) -> Dict[str, Any]:
    """Teto de admissão: linha nova é recusada e contada. Nada é apagado."""
    caps = dict(capacity or PRE_CAPACITY)
    used = used if isinstance(used, Mapping) else {}
    view: Dict[str, Any] = {}
    accept = True
    near = False
    for name, cap in caps.items():
        current = _positive_int(used.get(name))
        current = 0 if current is None else current
        ratio = current / cap if cap else 1.0
        view[name] = {"used": current, "capacity": cap, "ratio": ratio}
        if current >= cap:
            accept = False
        elif ratio >= CAPACITY_NEAR_RATIO:
            near = True
    return {"accept": accept, "near_capacity": near, "deletes_history": False,
            "reason_code": OK if accept else CAPACITY_REACHED, "usage": view}


def budget_verdict(*, batch_records: Any, buffered_records: Any) -> Dict[str, Any]:
    """Orçamento por lote e por buffer — a coleta não cresce sem limite."""
    batch = _positive_int(batch_records) or 0
    buffered = _positive_int(buffered_records) or 0
    if batch > MAX_BATCH_RECORDS or buffered > MAX_BUFFERED_RECORDS:
        return {"within_budget": False, "reason_code": BUDGET_EXCEEDED,
                "max_batch": MAX_BATCH_RECORDS, "max_buffered": MAX_BUFFERED_RECORDS}
    return {"within_budget": True, "reason_code": OK,
            "max_batch": MAX_BATCH_RECORDS, "max_buffered": MAX_BUFFERED_RECORDS}


#: Features PONTO-NO-TEMPO aceitas na captura (allowlist estrita). Só o que o
#: scanner realmente tinha naquele instante; ausência fica `None` e nunca 0.
FEATURE_NUMERIC_FIELDS = (
    "atr", "adx", "rsi", "ema_fast", "ema_slow", "volume_ratio", "spread_pct",
    "funding_pct", "rr_tp1", "rr_tp2", "entry_distance_atr", "score",
    "score_before_learning", "selection_score",
    # Features do Score V3, pelo MESMO nome/semântica. O scan champion calcula
    # apenas `htf_alignment_ratio` (alinhamento MTF); estrutura e gatilho ele
    # NÃO calcula, então ficam ausentes e derrubam a cobertura do modelo —
    # jamais viram zero. A allowlist permite que um produtor que REALMENTE as
    # tenha (pipeline de pesquisa) as registre; ela não as inventa.
    "htf_alignment_ratio", "structure_quality", "level_distance_atr",
    "trigger_body_ratio", "trigger_follow_through_atr",
)
FEATURE_LABEL_FIELDS = ("regime", "regime_source", "structure", "mtf_alignment",
                        "tier", "tier_provisional", "quality", "data_source")
#: Bloco de AVALIAÇÃO: quantos TFs do símbolo foram avaliados e qual venceu.
#: Reavaliação é tentativa, não oportunidade nova — por isso o TF entra na
#: identidade e o vencedor é um campo, não outra linha.
EVALUATION_FIELDS = ("evaluated_timeframes", "selected_timeframe",
                     "is_selected_timeframe", "evaluated_count")
#: Escopos de decisão OBSERVADA aceitos no payload (allowlist versionada). A
#: decisão do champion é `FINAL_SCANNER_SELECTION`; com o seletor operacional em
#: CANDIDATE o próprio runtime decidiu e o escopo é `CANDIDATE_SCANNER_SELECTION`.
#: Nenhum dos dois descreve a decisão OBSERVACIONAL do Shadow — ela tem escopo
#: próprio (`SHADOW_DECISION_SCOPE`), em campo separado.
OBSERVED_DECISION_SCOPES = ("FINAL_SCANNER_SELECTION", "CANDIDATE_SCANNER_SELECTION")
#: Decisão candidata OBSERVACIONAL (seletor operacional em LEGACY/OFF): escopo
#: explícito, separado da decisão LIVE e da seleção bruta/final do champion.
SHADOW_DECISION_SCOPE = "CANDIDATE_SHADOW"
SHADOW_DECISION_STATES = ("SELECTED", "REJECTED", "UNKNOWN")
#: Campos da decisão observacional, allowlist estrita (nada de objeto inteiro).
SHADOW_DECISION_FIELDS = ("scope", "version", "rule", "timeframe", "state",
                          "score", "min_score", "probability", "probability_event",
                          "model_fingerprint", "reason_code",
                          "is_selected_timeframe", "selected", "decided_at_ms",
                          "group", "authority")
SHADOW_AUTHORITY_FIELDS = ("experiment_id", "generation", "approval_id",
                           "bundle_hash", "manifest_hash", "score_config_hash",
                           "playbook", "purpose")
MAX_SHADOW_TIMEFRAMES = 12


def _features_view(features: Any) -> Optional[Dict[str, Any]]:
    """Normaliza o bloco de features: número finito, rótulo curto ou `None`."""
    if not isinstance(features, Mapping) or not features:
        return None
    saida: Dict[str, Any] = {}
    for campo in FEATURE_NUMERIC_FIELDS:
        saida[campo] = _number(features.get(campo))
    for campo in FEATURE_LABEL_FIELDS:
        saida[campo] = _label(features.get(campo), 40)
    return saida


def _evaluation_view(evaluation: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(evaluation, Mapping) or not evaluation:
        return None
    tfs = evaluation.get("evaluated_timeframes")
    lista = sorted({_label(item, 8) for item in tfs
                    if _label(item, 8)}) if isinstance(tfs, (list, tuple)) else []
    return {
        "evaluated_timeframes": lista,
        "selected_timeframe": _label(evaluation.get("selected_timeframe"), 8),
        "is_selected_timeframe": bool(evaluation.get("is_selected_timeframe")),
        "evaluated_count": len(lista),
    }


def _shadow_decision_view(shadow: Any) -> Optional[Dict[str, Any]]:
    """Normaliza a decisão candidata OBSERVACIONAL — allowlist fechada.

    Exige escopo/propósito SHADOW, estado declarado e a identidade de regra
    (manifesto/config/aprovação) que a recomputação offline vai reconciliar
    depois. Falta essencial devolve `None`: a ausência fica ausente, nunca é
    completada com zero nem com um estado inventado.
    """
    if not isinstance(shadow, Mapping) or shadow.get("scope") != SHADOW_DECISION_SCOPE:
        return None
    estado = shadow.get("state")
    if estado not in SHADOW_DECISION_STATES:
        return None
    autoridade_bruta = shadow.get("authority")
    if not isinstance(autoridade_bruta, Mapping) or autoridade_bruta.get("purpose") != "SHADOW":
        return None
    experimento = _positive_int(autoridade_bruta.get("experiment_id"))
    geracao = autoridade_bruta.get("generation")
    aprovacao = _label(autoridade_bruta.get("approval_id"), 64)
    if experimento is None or type(geracao) is not int or geracao < 0 or not aprovacao:
        return None
    grupo_bruto = shadow.get("group") if isinstance(shadow.get("group"), Mapping) else {}
    tfs = grupo_bruto.get("evaluated_timeframes")
    lista = sorted({_label(item, 8) for item in tfs if _label(item, 8)}) \
        if isinstance(tfs, (list, tuple)) else []
    timeframe = _label(shadow.get("timeframe"), 8)
    if timeframe is None or len(lista) > MAX_SHADOW_TIMEFRAMES:
        return None
    return {
        "scope": SHADOW_DECISION_SCOPE,
        "version": _label(shadow.get("version"), 64),
        "rule": _label(shadow.get("rule"), 40),
        "timeframe": timeframe,
        "state": estado,
        "score": _number(shadow.get("score")),
        "min_score": _number(shadow.get("min_score")),
        "probability": _number(shadow.get("probability")),
        "probability_event": _label(shadow.get("probability_event"), 40),
        "model_fingerprint": _label(shadow.get("model_fingerprint"), 64),
        "reason_code": _label(shadow.get("reason_code"), 64),
        "is_selected_timeframe": bool(shadow.get("is_selected_timeframe")),
        "selected": bool(shadow.get("selected")),
        "decided_at_ms": _positive_int(shadow.get("decided_at_ms")),
        "group": {"evaluated_timeframes": lista,
                  "evaluated_count": len(lista),
                  "selected_timeframe": _label(grupo_bruto.get("selected_timeframe"), 8),
                  "group_hash": _label(grupo_bruto.get("group_hash"), 64)},
        "authority": {"experiment_id": experimento, "generation": geracao,
                      "approval_id": aprovacao,
                      "bundle_hash": _label(autoridade_bruta.get("bundle_hash"), 64),
                      "manifest_hash": _label(autoridade_bruta.get("manifest_hash"), 64),
                      "score_config_hash": _label(autoridade_bruta.get("score_config_hash"), 64),
                      "playbook": _label(autoridade_bruta.get("playbook"), 40),
                      "purpose": "SHADOW"},
    }


def frozen_decision(*, identity: str, outcome: str, decision_ts_ms: Any,
                    setup: Mapping[str, Any], funnel: Mapping[str, Any],
                    availability: Mapping[str, Any],
                    source: Mapping[str, Any],
                    config: Optional[Mapping[str, Any]] = None,
                    score_trace_digest: Optional[str] = None,
                    features: Optional[Mapping[str, Any]] = None,
                    evaluation: Optional[Mapping[str, Any]] = None,
                    score_trace: Optional[Mapping[str, Any]] = None,
                    feature_evidence: Optional[Mapping[str, Any]] = None,
                    observed_decision_scope: Optional[str] = None,
                    shadow_decision: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Congela contexto, trace, configuração e disponibilidade NA decisão.

    Allowlist estrita: nada de objeto inteiro, exceção ou recomendação crua.
    """
    if outcome not in OUTCOMES:
        raise ValueError("outcome deve ser ACCEPTED ou VETOED")
    decision_ms = _positive_int(decision_ts_ms)
    if not isinstance(identity, str) or not identity.startswith("pre-") or decision_ms is None:
        raise ValueError("identidade ou instante da decisão inválidos")
    allowed_setup = ("symbol", "timeframe", "side", "playbook", "playbook_version",
                     "trigger_candle_ms", "entry", "stop_loss", "tp1", "tp2", "atr")
    frozen_setup = {}
    for key in allowed_setup:
        value = (setup or {}).get(key)
        frozen_setup[key] = (_number(value) if key not in
                             ("symbol", "timeframe", "side", "playbook", "playbook_version")
                             else _label(value, 50))
    frozen_setup["trigger_candle_ms"] = _positive_int((setup or {}).get("trigger_candle_ms"))
    availability_view = {}
    for key, value in (availability or {}).items():
        name = _label(key, 40)
        if name is None:
            continue
        availability_view[name] = value if isinstance(value, bool) else VERDICT_UNKNOWN
    features_view = _features_view(features)
    evaluation_view = _evaluation_view(evaluation)
    shadow_view = _shadow_decision_view(shadow_decision)
    payload = {
        # v2 só quando há bloco novo de verdade: sem eles o payload continua
        # idêntico ao v1 (mesmos campos, mesma ordem canônica, mesmo hash).
        "schema_version": (PRE_SCHEMA_VERSION_V2 if (features_view or evaluation_view
                           or score_trace is not None or feature_evidence is not None
                           or observed_decision_scope is not None
                           or shadow_view is not None)
                           else PRE_SCHEMA_VERSION),
        "scope": SCOPE,
        "policy": POLICY,
        "identity": identity,
        "outcome": outcome,
        "decision_ts_ms": decision_ms,
        "frozen_at_ms": decision_ms,
        "setup": frozen_setup,
        "funnel": dict(funnel or {}),
        "availability": availability_view,
        "source": dict(source or {}),
        "config": dict(config or {}),
        "score_trace_digest": _label(score_trace_digest, 64),
        "learning_eligible": False,
        "segregated_from": ["RealTrade", "pnl", "learner", "operational_calibration"],
    }
    if features_view is not None:
        payload["features"] = features_view
    if evaluation_view is not None:
        payload["evaluation"] = evaluation_view
    if score_trace is not None:
        from services.score_trace_service import freeze_trace
        trace = freeze_trace(dict(score_trace))
        payload['score_trace'] = trace
        payload['config'] = {'score_trace_version': trace.get('version'),
            'formula_requested': trace.get('formula_requested'),
            'formula_effective': trace.get('formula_effective'),
            'score': dict(trace.get('config') or {})}
    if observed_decision_scope in OBSERVED_DECISION_SCOPES:
        payload['observed_decision_scope'] = observed_decision_scope
    if shadow_view is not None:
        # Campo SEPARADO: a decisão observacional da candidata nunca sobrescreve
        # `observed_decision_scope` (que descreve a decisão final desta linha) e
        # nunca entra na configuração que autoriza intenção/ordem.
        payload['shadow_decision'] = shadow_view
    if isinstance(feature_evidence, Mapping):
        missing = feature_evidence.get('missing_reasons') or {}
        payload['feature_evidence'] = {
            'version': 'R13_POINT_IN_TIME_FEATURES_V1',
            'observed_at_ms': _positive_int(feature_evidence.get('observed_at_ms')),
            'candle_close_ms': _positive_int(feature_evidence.get('candle_close_ms')),
            'quality': 'FRESH' if feature_evidence.get('quality') == 'FRESH' else 'UNKNOWN',
            'missing_reasons': {key: 'POINT_IN_TIME_INPUT_UNAVAILABLE' for key in FEATURE_NUMERIC_FIELDS
                                if isinstance(missing, Mapping) and key in missing}}
        # Definição semântica congelada no payload, portanto coberta pelo hash
        # do dataset; não é aprovação/calibração nem qualidade preditiva.
        payload['feature_evidence']['definitions'] = {
            'structure_quality': 'PIVOT_STOP_AND_TARGET_GEOMETRY_BINARY',
            'level_distance_atr': 'ABS_ENTRY_MINUS_OBSERVED_PIVOT_OVER_ATR',
            'trigger_body_ratio': 'CLOSED_BODY_OVER_CLOSED_RANGE',
            'trigger_follow_through_atr': 'SIGNED_CLOSE_ADVANCE_FROM_PRIOR_CONFIRMED_TRIGGER_LEVEL_OVER_ATR',
            'htf_alignment_ratio': 'ALIGNED_FRESH_HIGHER_TFS_OVER_VALID_OBSERVED_HIGHER_TFS',
            'entry_distance_atr': 'ABS_CURRENT_PRICE_MINUS_ENTRY_OVER_ATR'}
    payload["payload_bytes"] = len(_canonical(payload).encode("utf-8"))
    limit = MAX_PAYLOAD_BYTES_V2 if payload['schema_version'] == PRE_SCHEMA_VERSION_V2 else MAX_PAYLOAD_BYTES
    if payload['payload_bytes'] > limit:
        raise ValueError('PRE_SELECTION_PAYLOAD_BUDGET_EXCEEDED')
    return payload


def merge_into_config(existing: Optional[Mapping[str, Any]],
                      pre_payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Compatibilidade ADITIVA: nenhuma chave existente é sobrescrita."""
    merged = dict(existing or {})
    merged.setdefault("scope", LEGACY_SCOPE)
    merged["r09_pre_selection"] = dict(pre_payload or {})
    return merged


def storage_plan() -> Dict[str, Any]:
    """Onde a evidência pré-seleção mora — sempre no acervo que já existe."""
    return {
        "tables": {"accepted": "decision_observations",
                   "vetoed": "rejected_setup_observations",
                   "attempts": "decision_observation_attempts"},
        "creates_new_table": False,
        "creates_scheduler_or_worker": False,
        "creates_exchange_client": False,
        "reuses_existing_flush_and_resolver": True,
        "retention_changed": False,
        "deletes_history": False,
        "experiment_evidence_preserved": True,
    }


def promotion_contract() -> Dict[str, Any]:
    """Promoção futura exige contrato explícito — vetada não vira insumo só por existir."""
    return {"auto_promotion": False,
            "reason_code": PROMOTION_REQUIRES_EXPLICIT_CONTRACT,
            "vetoed_readable_by_learner": False,
            "vetoed_readable_by_calibration": False,
            "requires_human_authorization": True}


def extra_source_dependency() -> Dict[str, Any]:
    return dict(EXTRA_SOURCE)


# ── Cobertura de CICLO: por que um ciclo não produziu observação ────────────
#: Não é oportunidade: é a razão de NÃO ter avaliado. Guardada em memória (teto
#: pequeno) e exposta no status — nunca gravada como linha de oportunidade, que
#: seria fabricar preço/decisão que não existiram.
COVERAGE_BLACKOUT = "BLACKOUT_WINDOW"
COVERAGE_NO_SYMBOLS = "NO_SYMBOLS_AVAILABLE"
COVERAGE_SOURCE_FAILURE = "SOURCE_FAILURE"
COVERAGE_SYMBOL_TIMEOUT = "SYMBOL_TIMEOUT"
COVERAGE_BUDGET = "CYCLE_BUDGET_EXHAUSTED"
COVERAGE_NO_CANDIDATE = "NO_CANDIDATE_IN_SYMBOL"
COVERAGE_MACRO_BLOCK = "MACRO_BLOCK_ALL"
COVERAGE_OBSERVED = "OBSERVED"
CYCLE_COVERAGE_REASONS = (COVERAGE_BLACKOUT, COVERAGE_NO_SYMBOLS,
                          COVERAGE_SOURCE_FAILURE, COVERAGE_SYMBOL_TIMEOUT,
                          COVERAGE_BUDGET, COVERAGE_NO_CANDIDATE,
                          COVERAGE_MACRO_BLOCK, COVERAGE_OBSERVED)
MAX_COVERAGE_CYCLES = 20
_COVERAGE_CYCLES: List[Dict[str, Any]] = []


def cycle_coverage(*, cycle_ts_ms: Any, symbols_requested: Any,
                   symbols_evaluated: Any, candidates_observed: Any,
                   candidates_seen: Any = None, candidates_refused: Any = 0,
                   reasons: Optional[Mapping[str, Any]] = None,
                   state: str = COVERAGE_COMPLETE) -> Dict[str, Any]:
    """Payload PURO de cobertura do ciclo. Motivo desconhecido é recusado."""
    contagem: Dict[str, int] = {}
    for chave, valor in (reasons or {}).items():
        nome = str(chave)
        if nome not in CYCLE_COVERAGE_REASONS:
            raise ValueError(f"motivo de cobertura desconhecido: {nome}")
        numero = _positive_int(valor)
        if numero:
            contagem[nome] = numero
    pedidos = _positive_int(symbols_requested) or 0
    avaliados = _positive_int(symbols_evaluated) or 0
    return {
        "schema_version": PRE_SCHEMA_VERSION_V2,
        "scope": SCOPE,
        "cycle_ts_ms": _positive_int(cycle_ts_ms),
        "symbols_requested": pedidos,
        "symbols_evaluated": avaliados,
        "candidates_observed": _positive_int(candidates_observed) or 0,
        "candidates_seen": _positive_int(candidates_seen if candidates_seen is not None else candidates_observed) or 0,
        "candidates_refused": _positive_int(candidates_refused) or 0,
        "not_evaluated": max(0, pedidos - avaliados),
        "reasons": contagem,
        "state": state if state in (COVERAGE_COMPLETE, COVERAGE_INCOMPLETE,
                                    COVERAGE_FAILED, COVERAGE_PENDING)
        else COVERAGE_INCOMPLETE,
        # Cobertura NÃO é oportunidade: nada aqui vira entrada de replay.
        "is_opportunity": False,
        "fabricates_prices_or_outcomes": False,
    }


def record_cycle_coverage(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Guarda a cobertura do último ciclo (memória, teto pequeno).

    Desligado não chega aqui: quem chama confere o gate antes. Reinício perde
    este histórico — é telemetria de cobertura, não evidência de estudo.
    """
    if not isinstance(payload, Mapping):
        return {"recorded": False, "reason_code": MISSING_IDENTITY}
    _COVERAGE_CYCLES.append(dict(payload))
    del _COVERAGE_CYCLES[:-MAX_COVERAGE_CYCLES]
    return {"recorded": True, "cycles_kept": len(_COVERAGE_CYCLES)}


def coverage_snapshot() -> Dict[str, Any]:
    """Resumo consultável da cobertura — alimenta o status derivado (sem I/O)."""
    if not _COVERAGE_CYCLES:
        return {"cycles": 0, "last_cycle_ts_ms": None, "last_state": None,
                "reasons": {}, "candidates_observed": 0}
    agregado: Dict[str, int] = {}
    observados = 0
    for ciclo in _COVERAGE_CYCLES:
        observados += int(ciclo.get("candidates_observed") or 0)
        for motivo, valor in (ciclo.get("reasons") or {}).items():
            agregado[motivo] = agregado.get(motivo, 0) + int(valor or 0)
    ultimo = _COVERAGE_CYCLES[-1]
    return {"cycles": len(_COVERAGE_CYCLES),
            "last_cycle_ts_ms": ultimo.get("cycle_ts_ms"),
            "last_state": ultimo.get("state"),
            "reasons": agregado, "candidates_observed": observados,
            'last_candidates_seen': ultimo.get('candidates_seen'),
            'last_candidates_refused': ultimo.get('candidates_refused')}


def reset_coverage() -> None:
    """Só para teste: limpa o anel de cobertura em memória."""
    _COVERAGE_CYCLES.clear()


def preselection_manifest() -> Dict[str, Any]:
    return {
        "schema_version": PRE_SCHEMA_VERSION,
        "schema_versions_accepted": list(PRE_SCHEMA_VERSIONS),
        "coverage": coverage_snapshot(),
        "scope": SCOPE,
        "legacy_scope_preserved": LEGACY_SCOPE,
        "policy": POLICY,
        "mode": selected_mode(),
        "enabled": collection_enabled(),
        "stages": list(STAGES),
        "verdicts": list(VERDICTS),
        "capacity": dict(PRE_CAPACITY),
        "budget": {"max_batch": MAX_BATCH_RECORDS, "max_buffered": MAX_BUFFERED_RECORDS,
                   "max_payload_bytes": MAX_PAYLOAD_BYTES},
        "horizon": {"candles": RESEARCH_HORIZON_CANDLES,
                    "resolver_bar_minutes": RESOLVER_BAR_MINUTES,
                    "max_bars": MAX_RESEARCH_BARS,
                    "semantics": "SHORT_RESEARCH_HORIZON_NOT_LIVE_TIME_STOP"},
        "storage": storage_plan(),
        "extra_source": extra_source_dependency(),
        "promotion": promotion_contract(),
        "limitations": list(LIMITATIONS),
    }
