"""R08E — calibração PRÓPRIA do Score V3: ajuste, artefato, verificação e previsão.

Método determinístico, CONGELADO antes de olhar outcome:

  • faixas fixas de 10 pontos em [0,100] (a última inclui 100). Sem busca de
    bordas, sem suavização, sem impor monotonicidade;
  • mínimo GLOBAL de 200 observações únicas e mínimo de 30 labels utilizáveis
    POR FAIXA. Faixa insuficiente não herda `p_global`, vizinho, V2 nem 0,5 —
    ela simplesmente não é servida;
  • `p = sucessos/n` da faixa, com intervalo de Wilson 95%, `n`, cobertura e
    proveniência. Nunca `score/100`;
  • o EVENTO é explícito no artefato (horizonte, censura/expiração e
    população). P(TP1), P(TP2) e P(resultado líquido positivo) NÃO são
    intercambiáveis;
  • label que só ficou conhecível DEPOIS do corte não entra no treino;
  • `FITTED` ≠ `OOS_VALIDATED` ≠ `ECONOMICALLY_APPROVED`: ajustar não aprova
    estratégia, e a aprovação econômica não é concedida aqui.

Artefato inválido, antigo, de outro fingerprint/população/evento ou revogado é
`UNAVAILABLE` — e `UNAVAILABLE` nunca vira número.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

CALIBRATION_CONTRACT = "R08E_V3_CALIBRATION_V1"

#: Grade FIXA: dez faixas de dez pontos; a última inclui o 100.
BIN_WIDTH = 10
BIN_COUNT = 10
MIN_GLOBAL_OBSERVATIONS = 200
MIN_PER_BIN_LABELS = 30
WILSON_Z = 1.959963984540054          # 95%

# ── Eventos (NÃO intercambiáveis) ───────────────────────────────────────────
EVENT_TP1 = "P_TP1_BEFORE_STOP"
EVENT_TP2 = "P_TP2_BEFORE_STOP"
EVENT_NET_POSITIVE = "P_NET_RESULT_POSITIVE"
EVENTS = (EVENT_TP1, EVENT_TP2, EVENT_NET_POSITIVE)
#: Eventos cujo payoff é BINÁRIO (ganha o alvo ou perde 1R). Gestão parcial com
#: runner NÃO é binária: ver `score_v3_service.net_ev`.
BINARY_PAYOFF_EVENTS = (EVENT_TP2,)

# ── Estados do artefato ─────────────────────────────────────────────────────
STATE_FITTED = "FITTED"
STATE_OOS_VALIDATED = "OOS_VALIDATED"
STATE_ECONOMICALLY_APPROVED = "ECONOMICALLY_APPROVED"
STATE_REVOKED = "REVOKED"
STATE_UNAVAILABLE = "UNAVAILABLE"
ARTIFACT_STATES = (STATE_FITTED, STATE_OOS_VALIDATED,
                   STATE_ECONOMICALLY_APPROVED, STATE_REVOKED)

# ── Motivos ─────────────────────────────────────────────────────────────────
OK = "OK"
ARTIFACT_MISSING = "CALIBRATION_ARTIFACT_MISSING"
ARTIFACT_INVALID = "CALIBRATION_ARTIFACT_INVALID"
ARTIFACT_EXPIRED = "CALIBRATION_ARTIFACT_EXPIRED"
ARTIFACT_REVOKED = "CALIBRATION_ARTIFACT_REVOKED"
CONTRACT_MISMATCH = "CALIBRATION_CONTRACT_MISMATCH"
FINGERPRINT_MISMATCH = "CALIBRATION_FINGERPRINT_MISMATCH"
POPULATION_MISMATCH = "CALIBRATION_POPULATION_MISMATCH"
EVENT_MISMATCH = "CALIBRATION_EVENT_MISMATCH"
DATASET_MISMATCH = "CALIBRATION_DATASET_MISMATCH"
SAMPLE_INSUFFICIENT = "CALIBRATION_SAMPLE_INSUFFICIENT"
BIN_UNSUPPORTED = "CALIBRATION_BIN_UNSUPPORTED"
SCORE_OUT_OF_RANGE = "SCORE_OUT_OF_RANGE"
LABEL_AFTER_CUTOFF = "LABEL_AVAILABLE_AFTER_CUTOFF"
LABEL_UNRESOLVED = "LABEL_UNRESOLVED_OR_CENSORED"
LABEL_INVALID = "LABEL_INVALID"
DUPLICATED_OBSERVATION = "DUPLICATED_OBSERVATION"
EVENT_UNKNOWN = "CALIBRATION_EVENT_UNKNOWN"
APPROVAL_DECISION_REQUIRED = "CALIBRATION_OOS_THRESHOLDS_DECISION_REQUIRED"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    número = float(value)
    return número if math.isfinite(número) else None


def _int(value: Any) -> Optional[int]:
    número = _finite(value)
    if número is None or número != int(número):
        return None
    return int(número)


# ── Grade de faixas ─────────────────────────────────────────────────────────
def bin_index(score: Any) -> Optional[int]:
    """Índice da faixa de 10 pontos; a última faixa INCLUI 100."""
    valor = _finite(score)
    if valor is None or not 0.0 <= valor <= 100.0:
        return None
    if valor == 100.0:
        return BIN_COUNT - 1
    return int(valor // BIN_WIDTH)


def bin_bounds(index: Any) -> Optional[Tuple[float, float, bool]]:
    """`(inferior, superior, superior_incluído)` da faixa."""
    idx = _int(index)
    if idx is None or not 0 <= idx < BIN_COUNT:
        return None
    inferior = float(idx * BIN_WIDTH)
    superior = float((idx + 1) * BIN_WIDTH)
    return inferior, superior, idx == BIN_COUNT - 1


def wilson_interval(successes: Any, total: Any, *, z: float = WILSON_Z
                    ) -> Optional[Tuple[float, float]]:
    """Intervalo de Wilson 95% — nada de aproximação normal simples."""
    sucessos, n = _int(successes), _int(total)
    if sucessos is None or n is None or n <= 0 or not 0 <= sucessos <= n:
        return None
    p = sucessos / n
    denominador = 1.0 + (z * z) / n
    centro = (p + (z * z) / (2 * n)) / denominador
    margem = (z * math.sqrt((p * (1 - p) / n) + (z * z) / (4 * n * n))) / denominador
    return max(0.0, centro - margem), min(1.0, centro + margem)


# ── Observações ─────────────────────────────────────────────────────────────
def _usable(observation: Any, *, cutoff_ms: Optional[int]) -> Tuple[Optional[Dict[str, Any]],
                                                                   Optional[str]]:
    """Observação utilizável para TREINO naquele corte, ou o motivo de não ser."""
    if not isinstance(observation, Mapping):
        return None, LABEL_INVALID
    chave = observation.get("opportunity_key")
    if not isinstance(chave, str) or not chave.strip():
        return None, LABEL_INVALID
    score = _finite(observation.get("score"))
    if score is None or not 0.0 <= score <= 100.0:
        return None, SCORE_OUT_OF_RANGE
    label = observation.get("label")
    if label is None:
        # Expirou/censurou sem resolver: NÃO é fracasso, é ausência.
        return None, LABEL_UNRESOLVED
    if not isinstance(label, bool):
        return None, LABEL_INVALID
    disponivel = _int(observation.get("label_available_ts_ms"))
    if disponivel is None:
        return None, LABEL_INVALID
    if cutoff_ms is not None and disponivel > int(cutoff_ms):
        # Label que só ficou conhecível DEPOIS do corte não entra no treino.
        return None, LABEL_AFTER_CUTOFF
    return {"opportunity_key": chave.strip(), "score": score, "label": label,
            "label_available_ts_ms": disponivel,
            "decision_ts_ms": _int(observation.get("decision_ts_ms"))}, None


def prepare_observations(observations: Sequence[Any], *,
                         cutoff_ms: Optional[int] = None) -> Dict[str, Any]:
    """Deduplica por oportunidade e separa o que é utilizável do que não é."""
    usaveis: Dict[str, Dict[str, Any]] = {}
    excluidas: Dict[str, int] = {}
    duplicadas = 0
    for bruta in observations or ():
        limpa, motivo = _usable(bruta, cutoff_ms=cutoff_ms)
        if limpa is None:
            excluidas[motivo] = excluidas.get(motivo, 0) + 1
            continue
        if limpa["opportunity_key"] in usaveis:
            duplicadas += 1
            excluidas[DUPLICATED_OBSERVATION] = \
                excluidas.get(DUPLICATED_OBSERVATION, 0) + 1
            continue
        usaveis[limpa["opportunity_key"]] = limpa
    return {"rows": sorted(usaveis.values(), key=lambda item: item["opportunity_key"]),
            "unique": len(usaveis), "duplicated": duplicadas,
            "excluded": excluidas,
            "total": len(list(observations or ()))}


# ── Ajuste ──────────────────────────────────────────────────────────────────
def fit_calibration(observations: Sequence[Any], *, event: str, population: str,
                    model_fingerprint: str, score_config_hash: str,
                    horizon_bars: int, bar_ms: int, censoring: str,
                    payoff_ref: str, source: str, dataset_hash: str,
                    cutoff_ms: int, generated_at_ms: int,
                    valid_until_ms: Optional[int] = None,
                    versions: Optional[Mapping[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Ajusta a calibração e devolve o ARTEFATO completo (ou a recusa dele).

    Nada é inventado: sem amostra global suficiente o artefato nasce
    `UNAVAILABLE` com o motivo, e faixa sem labels suficientes fica NÃO
    suportada — sem herdar probabilidade de ninguém.
    """
    if event not in EVENTS:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": EVENT_UNKNOWN, "detail": str(event)}
    for nome, valor in (("population", population),
                        ("model_fingerprint", model_fingerprint),
                        ("score_config_hash", score_config_hash),
                        ("censoring", censoring), ("payoff_ref", payoff_ref),
                        ("source", source), ("dataset_hash", dataset_hash)):
        if not isinstance(valor, str) or not valor.strip():
            return {"ok": False, "state": STATE_UNAVAILABLE,
                    "reason_code": ARTIFACT_INVALID, "detail": nome}
    corte = _int(cutoff_ms)
    gerado = _int(generated_at_ms)
    horizonte = _int(horizon_bars)
    barra = _int(bar_ms)
    if corte is None or gerado is None or horizonte is None or barra is None \
            or horizonte <= 0 or barra <= 0:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": "janela/horizonte"}

    preparadas = prepare_observations(observations, cutoff_ms=corte)
    faixas: List[Dict[str, Any]] = []
    por_faixa: Dict[int, List[Dict[str, Any]]] = {idx: [] for idx in range(BIN_COUNT)}
    for linha in preparadas["rows"]:
        por_faixa[bin_index(linha["score"])].append(linha)
    suportadas = 0
    for idx in range(BIN_COUNT):
        linhas = por_faixa[idx]
        inferior, superior, inclui = bin_bounds(idx)
        n = len(linhas)
        sucessos = sum(1 for item in linhas if item["label"] is True)
        suportada = n >= MIN_PER_BIN_LABELS
        intervalo = wilson_interval(sucessos, n) if n > 0 else None
        faixas.append({
            "bin": idx, "lower": inferior, "upper": superior,
            "upper_inclusive": inclui, "n": n, "successes": sucessos,
            # `p` existe só com amostra suficiente: faixa fraca NÃO publica
            # número (e não herda de vizinho, global, V2 ou 0,5).
            "p": (sucessos / n) if suportada and n > 0 else None,
            "wilson_low": (intervalo[0] if suportada and intervalo else None),
            "wilson_high": (intervalo[1] if suportada and intervalo else None),
            "supported": suportada,
            "reason_code": OK if suportada else SAMPLE_INSUFFICIENT,
            "min_labels_required": MIN_PER_BIN_LABELS,
        })
        suportadas += int(suportada)

    corpo = {
        "contract": CALIBRATION_CONTRACT,
        "event": event,
        "event_definition": {"event": event, "horizon_bars": horizonte,
                             "bar_ms": barra, "censoring": censoring,
                             "payoff_ref": payoff_ref,
                             "population": population,
                             "interchangeable_with_other_events": False},
        "population": population,
        "model_fingerprint": model_fingerprint,
        "score_config_hash": score_config_hash,
        "source": source,
        "versions": {str(chave): str(valor)
                     for chave, valor in sorted((versions or {}).items())},
        "config": {"bin_width": BIN_WIDTH, "bin_count": BIN_COUNT,
                   "min_global_observations": MIN_GLOBAL_OBSERVATIONS,
                   "min_labels_per_bin": MIN_PER_BIN_LABELS,
                   "wilson_z": WILSON_Z, "monotonicity_enforced": False,
                   "smoothing": "NONE", "probability_from_score": False},
        "bins": faixas,
        "coverage": {"observations_seen": preparadas["total"],
                     "unique_usable": preparadas["unique"],
                     "duplicated": preparadas["duplicated"],
                     "excluded": preparadas["excluded"],
                     "bins_supported": suportadas,
                     "bins_total": BIN_COUNT},
        "training": {"cutoff_ms": corte, "labels_after_cutoff_excluded": True},
        "dataset_hash": dataset_hash,
        "generation": {"generated_at_ms": gerado,
                       "valid_until_ms": _int(valid_until_ms)},
        "metrics": {"brier": None, "log_loss": None, "oos": None},
    }
    if preparadas["unique"] < MIN_GLOBAL_OBSERVATIONS:
        corpo["state"] = STATE_UNAVAILABLE
        corpo["reason_code"] = SAMPLE_INSUFFICIENT
        corpo["artifact_hash"] = _hash({k: v for k, v in corpo.items()
                                        if k != "artifact_hash"})
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": SAMPLE_INSUFFICIENT, "artifact": corpo}
    # Brier IN-SAMPLE é diagnóstico, NÃO validação: o OOS vem de outro passo.
    corpo["metrics"]["brier"] = _brier(corpo, preparadas["rows"])
    corpo["state"] = STATE_FITTED
    corpo["reason_code"] = OK
    corpo["approval"] = {"state": "NOT_APPROVED",
                         "reason_code": APPROVAL_DECISION_REQUIRED,
                         "economically_approved": False}
    corpo["artifact_hash"] = _hash({k: v for k, v in corpo.items()
                                    if k != "artifact_hash"})
    return {"ok": True, "state": STATE_FITTED, "reason_code": OK,
            "artifact": corpo}


def _bin_of(artifact: Mapping[str, Any], score: Any) -> Optional[Mapping[str, Any]]:
    idx = bin_index(score)
    if idx is None:
        return None
    for faixa in artifact.get("bins") or ():
        if isinstance(faixa, Mapping) and _int(faixa.get("bin")) == idx:
            return faixa
    return None


def _brier(artifact: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
           ) -> Optional[float]:
    """Brier sobre as faixas SUPORTADAS (as outras não têm previsão)."""
    erros, n = 0.0, 0
    for linha in rows or ():
        faixa = _bin_of(artifact, linha.get("score"))
        if not faixa or not faixa.get("supported") or faixa.get("p") is None:
            continue
        p = float(faixa["p"])
        y = 1.0 if linha.get("label") is True else 0.0
        erros += (p - y) ** 2
        n += 1
    return (erros / n) if n else None


def recompute_hash(artifact: Any) -> Optional[str]:
    if not isinstance(artifact, Mapping):
        return None
    return _hash({chave: valor for chave, valor in artifact.items()
                  if chave != "artifact_hash"})


def verify_artifact(artifact: Any, *, model_fingerprint: Any = None,
                    population: Any = None, event: Any = None,
                    dataset_hash: Any = None, now_ms: Any = None
                    ) -> Dict[str, Any]:
    """O artefato é USÁVEL agora, para ESTE modelo/população/evento?

    Qualquer dúvida devolve `UNAVAILABLE` com motivo — nunca uma probabilidade
    de outro contrato, de outro evento ou de um artefato vencido/revogado.
    """
    if not isinstance(artifact, Mapping) or not artifact:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_MISSING}
    if artifact.get("contract") != CALIBRATION_CONTRACT:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": CONTRACT_MISMATCH}
    declarado = artifact.get("artifact_hash")
    if not isinstance(declarado, str) or recompute_hash(artifact) != declarado:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": "artifact_hash"}
    estado = str(artifact.get("state") or "")
    if estado == STATE_REVOKED:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_REVOKED}
    if estado not in (STATE_FITTED, STATE_OOS_VALIDATED,
                      STATE_ECONOMICALLY_APPROVED):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": f"state={estado}"}
    if model_fingerprint is not None \
            and str(artifact.get("model_fingerprint")) != str(model_fingerprint):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": FINGERPRINT_MISMATCH}
    if population is not None and str(artifact.get("population")) != str(population):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": POPULATION_MISMATCH}
    if event is not None and str(artifact.get("event")) != str(event):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": EVENT_MISMATCH}
    if dataset_hash is not None \
            and str(artifact.get("dataset_hash")) != str(dataset_hash):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": DATASET_MISMATCH}
    validade = _int((artifact.get("generation") or {}).get("valid_until_ms"))
    agora = _int(now_ms)
    if validade is not None and agora is not None and agora > validade:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_EXPIRED}
    return {"ok": True, "state": estado, "reason_code": OK,
            "event": artifact.get("event"),
            "bins_supported": (artifact.get("coverage") or {}).get("bins_supported")}


def predict(artifact: Any, *, score: Any, model_fingerprint: Any = None,
            population: Any = None, event: Any = None,
            dataset_hash: Any = None, now_ms: Any = None) -> Dict[str, Any]:
    """Probabilidade da FAIXA suportada — ou indisponibilidade explícita."""
    verdict = verify_artifact(artifact, model_fingerprint=model_fingerprint,
                              population=population, event=event,
                              dataset_hash=dataset_hash, now_ms=now_ms)
    if not verdict["ok"]:
        return {"available": False, "probability": None,
                "reason_code": verdict["reason_code"],
                "state": STATE_UNAVAILABLE}
    faixa = _bin_of(artifact, score)
    if faixa is None:
        return {"available": False, "probability": None,
                "reason_code": SCORE_OUT_OF_RANGE, "state": STATE_UNAVAILABLE}
    if not faixa.get("supported") or faixa.get("p") is None:
        # Faixa fraca NÃO é servida: sem herança de vizinho/global/V2/0,5.
        return {"available": False, "probability": None,
                "reason_code": BIN_UNSUPPORTED, "state": STATE_UNAVAILABLE,
                "bin": faixa.get("bin"), "n": faixa.get("n")}
    return {
        "available": True, "probability": float(faixa["p"]),
        "reason_code": OK, "state": verdict["state"],
        "bin": faixa.get("bin"), "bin_lower": faixa.get("lower"),
        "bin_upper": faixa.get("upper"), "n": faixa.get("n"),
        "wilson_low": faixa.get("wilson_low"), "wilson_high": faixa.get("wilson_high"),
        "event": artifact.get("event"),
        "event_definition": dict(artifact.get("event_definition") or {}),
        "provenance": {"source": artifact.get("source"),
                       "artifact_hash": artifact.get("artifact_hash"),
                       "dataset_hash": artifact.get("dataset_hash"),
                       "model_fingerprint": artifact.get("model_fingerprint"),
                       "cutoff_ms": (artifact.get("training") or {}).get("cutoff_ms")},
        "is_probability": True,
        "out_of_sample": verdict["state"] in (STATE_OOS_VALIDATED,
                                              STATE_ECONOMICALLY_APPROVED),
    }


def validate_out_of_sample(artifact: Any, holdout: Sequence[Any], *,
                           now_ms: Any = None) -> Dict[str, Any]:
    """Avalia o artefato FORA DA AMOSTRA e, só então, marca `OOS_VALIDATED`.

    As previsões vêm do artefato ajustado no treino; as labels, do conjunto
    posterior. Publica Brier, curva de confiabilidade por faixa, cobertura e
    incerteza OOS. Aprovação econômica continua sendo OUTRA decisão.
    """
    verdict = verify_artifact(artifact, now_ms=now_ms)
    if not verdict["ok"]:
        return {"ok": False, "reason_code": verdict["reason_code"],
                "state": STATE_UNAVAILABLE}
    # O corte do artefato separa treino de teste: label conhecível ANTES do
    # corte não é out-of-sample.
    corte = _int((artifact.get("training") or {}).get("cutoff_ms"))
    preparadas = prepare_observations(holdout, cutoff_ms=None)
    posteriores, antecipadas = [], 0
    for linha in preparadas["rows"]:
        if corte is not None and linha["label_available_ts_ms"] <= corte:
            antecipadas += 1
            continue
        posteriores.append(linha)
    por_faixa: Dict[int, Dict[str, Any]] = {}
    erros, n_total = 0.0, 0
    for linha in posteriores:
        previsao = predict(artifact, score=linha["score"], now_ms=now_ms)
        if not previsao["available"]:
            continue
        idx = int(previsao["bin"])
        bucket = por_faixa.setdefault(idx, {"bin": idx, "n": 0, "successes": 0,
                                            "predicted": previsao["probability"]})
        bucket["n"] += 1
        bucket["successes"] += int(linha["label"] is True)
        erros += (previsao["probability"] - (1.0 if linha["label"] else 0.0)) ** 2
        n_total += 1
    curva = []
    for idx in sorted(por_faixa):
        bucket = por_faixa[idx]
        intervalo = wilson_interval(bucket["successes"], bucket["n"])
        curva.append({**bucket,
                      "observed": (bucket["successes"] / bucket["n"]) if bucket["n"] else None,
                      "wilson_low": intervalo[0] if intervalo else None,
                      "wilson_high": intervalo[1] if intervalo else None})
    metricas = {"brier": (erros / n_total) if n_total else None,
                "predictions": n_total,
                "reliability": curva,
                "coverage": {"holdout_seen": preparadas["total"],
                             "unique_usable": preparadas["unique"],
                             "before_cutoff_discarded": antecipadas,
                             "excluded": preparadas["excluded"],
                             "predicted": n_total},
                "uncertainty": "WILSON_95_PER_BIN"}
    if not n_total:
        return {"ok": False, "reason_code": SAMPLE_INSUFFICIENT,
                "state": artifact.get("state"), "oos": metricas}
    novo = {chave: valor for chave, valor in artifact.items()
            if chave != "artifact_hash"}
    novo["metrics"] = {**(artifact.get("metrics") or {}), "oos": metricas}
    novo["state"] = STATE_OOS_VALIDATED
    # Validar fora da amostra NÃO aprova economia: o limite de aceitação é
    # decisão humana e ainda não existe.
    novo["approval"] = {"state": "NOT_APPROVED",
                        "reason_code": APPROVAL_DECISION_REQUIRED,
                        "economically_approved": False}
    novo["artifact_hash"] = _hash(novo)
    return {"ok": True, "reason_code": OK, "state": STATE_OOS_VALIDATED,
            "artifact": novo, "oos": metricas}


def revoke(artifact: Any, *, reason: str) -> Dict[str, Any]:
    """Revoga o artefato (e revogado nunca volta a servir probabilidade)."""
    if not isinstance(artifact, Mapping):
        return {"ok": False, "reason_code": ARTIFACT_MISSING}
    novo = {chave: valor for chave, valor in artifact.items()
            if chave != "artifact_hash"}
    novo["state"] = STATE_REVOKED
    novo["revocation"] = {"reason": str(reason)}
    novo["artifact_hash"] = _hash(novo)
    return {"ok": True, "reason_code": OK, "artifact": novo}


def calibration_manifest(artifact: Any = None) -> Dict[str, Any]:
    """Manifesto do contrato de calibração (sem rodar ajuste nenhum)."""
    base = {
        "contract": CALIBRATION_CONTRACT,
        "bins": {"width": BIN_WIDTH, "count": BIN_COUNT,
                 "last_bin_includes_100": True, "edge_search": False},
        "minimums": {"global_unique_observations": MIN_GLOBAL_OBSERVATIONS,
                     "labels_per_bin": MIN_PER_BIN_LABELS},
        "events": list(EVENTS),
        "binary_payoff_events": list(BINARY_PAYOFF_EVENTS),
        "states": list(ARTIFACT_STATES),
        "insufficient_bin_inherits": False,
        "probability_from_score": False,
        "economic_approval_granted_here": False,
        "approval_reason_code": APPROVAL_DECISION_REQUIRED,
    }
    if artifact is None:
        return {**base, "artifact": None, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_MISSING}
    verdict = verify_artifact(artifact)
    return {**base, "state": verdict.get("state"),
            "reason_code": verdict.get("reason_code"),
            "artifact": {"event": artifact.get("event"),
                         "population": artifact.get("population"),
                         "model_fingerprint": artifact.get("model_fingerprint"),
                         "artifact_hash": artifact.get("artifact_hash"),
                         "bins_supported": (artifact.get("coverage") or {})
                         .get("bins_supported"),
                         "brier": (artifact.get("metrics") or {}).get("brier")}}
