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
import time
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
LABEL_CHRONOLOGY_INVALID = "LABEL_CHRONOLOGY_INVALID"
LABEL_EVENT_MISMATCH = "LABEL_EVENT_MISMATCH"
TRAIN_OOS_OVERLAP = "TRAIN_OOS_OPPORTUNITY_OVERLAP"
DUPLICATED_OBSERVATION_CONFLICT = "DUPLICATED_OBSERVATION_CONFLICT"
CENSORING_RULES = ("EXPIRES_AFTER_HORIZON_WITHOUT_RESOLUTION",
                   "CENSOR_UNRESOLVED_AT_HORIZON")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        número = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return número if math.isfinite(número) else None


def _int(value: Any) -> Optional[int]:
    número = _finite(value)
    if número is None or número != int(número):
        return None
    return int(número)


def _clock(now_ms: Any = None) -> Optional[int]:
    """Omitir relógio usa UTC real; relógio inválido não desativa validade."""
    agora = int(time.time() * 1000) if now_ms is None else _int(now_ms)
    return agora if agora is not None and agora > 0 else None


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
def _usable(observation: Any, *, cutoff_ms: Optional[int], event: Optional[str] = None,
            horizon_ms: Optional[int] = None, bar_ms: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]],
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
    decisao = _int(observation.get("decision_ts_ms"))
    if disponivel is None or decisao is None or decisao <= 0 or disponivel < decisao:
        return None, LABEL_CHRONOLOGY_INVALID
    if event is not None and observation.get("event") != event:
        return None, LABEL_EVENT_MISMATCH
    if cutoff_ms is not None and disponivel > int(cutoff_ms):
        # Label que só ficou conhecível DEPOIS do corte não entra no treino.
        return None, LABEL_AFTER_CUTOFF
    first_bar = ((decisao + bar_ms - 1) // bar_ms) * bar_ms if bar_ms else decisao
    if horizon_ms is not None and disponivel > first_bar + horizon_ms:
        return None, LABEL_UNRESOLVED
    return {"opportunity_key": chave.strip(), "score": score, "label": label,
            "label_available_ts_ms": disponivel,
            "decision_ts_ms": decisao, "event": observation.get("event")}, None


def prepare_observations(observations: Sequence[Any], *,
                         cutoff_ms: Optional[int] = None, event: Optional[str] = None,
                         horizon_ms: Optional[int] = None, bar_ms: Optional[int] = None) -> Dict[str, Any]:
    """Deduplica por oportunidade e separa o que é utilizável do que não é."""
    usaveis: Dict[str, Dict[str, Any]] = {}
    excluidas: Dict[str, int] = {}
    duplicadas = 0
    conflitos = set()
    for bruta in observations or ():
        limpa, motivo = _usable(bruta, cutoff_ms=cutoff_ms, event=event,
                               horizon_ms=horizon_ms, bar_ms=bar_ms)
        if limpa is None:
            excluidas[motivo] = excluidas.get(motivo, 0) + 1
            continue
        key = limpa["opportunity_key"]
        if key in conflitos:
            excluidas[DUPLICATED_OBSERVATION_CONFLICT] = excluidas.get(DUPLICATED_OBSERVATION_CONFLICT, 0) + 1
            continue
        if key in usaveis:
            duplicadas += 1
            if usaveis[key] != limpa:
                conflitos.add(key)
                del usaveis[key]
                excluidas[DUPLICATED_OBSERVATION_CONFLICT] = excluidas.get(DUPLICATED_OBSERVATION_CONFLICT, 0) + 2
            else:
                excluidas[DUPLICATED_OBSERVATION] = excluidas.get(DUPLICATED_OBSERVATION, 0) + 1
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
    validade = _int(valid_until_ms)
    if corte is None or gerado is None or horizonte is None or barra is None \
            or corte <= 0 or gerado < corte or horizonte <= 0 or barra <= 0 \
            or validade is None or validade <= gerado or censoring not in CENSORING_RULES:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": "janela/horizonte"}
    if not isinstance(versions, Mapping) or not versions or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in versions.items()):
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": "versions"}

    preparadas = prepare_observations(observations, cutoff_ms=corte, event=event,
                                      horizon_ms=horizonte * barra, bar_ms=barra)
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
        "training": {"cutoff_ms": corte, "labels_after_cutoff_excluded": True,
                     "opportunity_keys": [r["opportunity_key"] for r in preparadas["rows"]],
                     "observations_hash": _hash(preparadas["rows"]),
                     "decision_min_ms": min((r["decision_ts_ms"] for r in preparadas["rows"]), default=None),
                     "decision_max_ms": max((r["decision_ts_ms"] for r in preparadas["rows"]), default=None),
                     "label_min_ms": min((r["label_available_ts_ms"] for r in preparadas["rows"]), default=None),
                     "label_max_ms": max((r["label_available_ts_ms"] for r in preparadas["rows"]), default=None)},
        "dataset_hash": dataset_hash,
        "generation": {"generated_at_ms": gerado,
                       "valid_until_ms": validade},
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
    try:
        return _hash({chave: valor for chave, valor in artifact.items()
                      if chave != "artifact_hash"})
    except (ValueError, TypeError, OverflowError):
        return None


def _artifact_semantics(a: Mapping[str, Any]) -> Optional[str]:
    """Hash protege integridade; este contrato valida o significado do conteúdo."""
    required = {"contract", "event", "event_definition", "population", "model_fingerprint",
                "score_config_hash", "source", "versions", "config", "bins", "coverage",
                "training", "dataset_hash", "generation", "metrics", "state", "reason_code",
                "approval", "artifact_hash"}
    if set(a) != required:
        return "schema"
    for field in ("population", "model_fingerprint", "score_config_hash", "source", "dataset_hash"):
        if not isinstance(a.get(field), str) or not a[field].strip():
            return field
    if a.get("event") not in EVENTS or not isinstance(a.get("versions"), dict) or not a["versions"] \
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in a["versions"].items()):
        return "event/versions"
    definition = a.get("event_definition")
    if not isinstance(definition, dict) or set(definition) != {
            "event", "horizon_bars", "bar_ms", "censoring", "payoff_ref", "population",
            "interchangeable_with_other_events"}:
        return "event_definition"
    if definition.get("event") != a["event"] or definition.get("population") != a["population"] \
            or definition.get("interchangeable_with_other_events") is not False \
            or definition.get("censoring") not in CENSORING_RULES \
            or not isinstance(definition.get("payoff_ref"), str) or not definition["payoff_ref"] \
            or _int(definition.get("horizon_bars")) is None or definition["horizon_bars"] <= 0 \
            or _int(definition.get("bar_ms")) is None or definition["bar_ms"] <= 0:
        return "event_definition"
    expected_config = {"bin_width": BIN_WIDTH, "bin_count": BIN_COUNT,
                       "min_global_observations": MIN_GLOBAL_OBSERVATIONS,
                       "min_labels_per_bin": MIN_PER_BIN_LABELS, "wilson_z": WILSON_Z,
                       "monotonicity_enforced": False, "smoothing": "NONE",
                       "probability_from_score": False}
    cfg = a.get("config")
    if not isinstance(cfg, dict) or cfg != expected_config \
            or cfg.get("monotonicity_enforced") is not False \
            or cfg.get("probability_from_score") is not False \
            or any(isinstance(cfg.get(k), bool) for k in ("bin_width", "bin_count", "min_global_observations", "min_labels_per_bin", "wilson_z")):
        return "config"
    bins = a.get("bins")
    if not isinstance(bins, list) or len(bins) != BIN_COUNT:
        return "bins"
    total, supported = 0, 0
    fields = {"bin", "lower", "upper", "upper_inclusive", "n", "successes", "p",
              "wilson_low", "wilson_high", "supported", "reason_code", "min_labels_required"}
    for idx, b in enumerate(bins):
        if not isinstance(b, dict) or set(b) != fields or _int(b.get("bin")) != idx:
            return "bin_schema"
        n, successes = _int(b.get("n")), _int(b.get("successes"))
        bounds = bin_bounds(idx)
        if n is None or successes is None or n < 0 or not 0 <= successes <= n \
                or _finite(b.get("lower")) != bounds[0] or _finite(b.get("upper")) != bounds[1] \
                or b.get("upper_inclusive") is not bounds[2] \
                or _int(b.get("min_labels_required")) != MIN_PER_BIN_LABELS:
            return "bin_counts/bounds"
        enough = n >= MIN_PER_BIN_LABELS
        if b.get("supported") is not enough or b.get("reason_code") != (OK if enough else SAMPLE_INSUFFICIENT):
            return "bin_support"
        if enough:
            interval = wilson_interval(successes, n)
            for key, expected in (("p", successes / n), ("wilson_low", interval[0]), ("wilson_high", interval[1])):
                value = _finite(b.get(key))
                if value is None or not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
                    return "bin_probability/interval"
        elif any(b.get(k) is not None for k in ("p", "wilson_low", "wilson_high")):
            return "unsupported_bin_probability"
        total += n
        supported += int(enough)
    coverage = a.get("coverage")
    training, generation = a.get("training"), a.get("generation")
    if not isinstance(coverage, dict) or set(coverage) != {
            "observations_seen", "unique_usable", "duplicated", "excluded", "bins_supported", "bins_total"} \
            or _int(coverage.get("unique_usable")) != total \
            or total < MIN_GLOBAL_OBSERVATIONS or _int(coverage.get("bins_supported")) != supported \
            or _int(coverage.get("bins_total")) != BIN_COUNT \
            or _int(coverage.get("observations_seen")) is None or coverage["observations_seen"] < total \
            or _int(coverage.get("duplicated")) is None or coverage["duplicated"] < 0 \
            or not isinstance(coverage.get("excluded"), dict) \
            or any(not isinstance(k, str) or _int(v) is None or v < 0 for k, v in coverage["excluded"].items()) \
            or coverage["observations_seen"] != total + sum(coverage["excluded"].values()):
        return "coverage"
    if not isinstance(training, dict) or set(training) != {
            "cutoff_ms", "labels_after_cutoff_excluded", "opportunity_keys", "observations_hash",
            "decision_min_ms", "decision_max_ms", "label_min_ms", "label_max_ms"} \
            or not isinstance(generation, dict) or set(generation) != {"generated_at_ms", "valid_until_ms"}:
        return "training/generation"
    keys = training.get("opportunity_keys")
    cutoff, generated, validity = (_int(training.get("cutoff_ms")),
                                   _int(generation.get("generated_at_ms")),
                                   _int(generation.get("valid_until_ms")))
    if not isinstance(keys, list) or len(keys) != total \
            or any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != total \
            or keys != sorted(keys) or training.get("labels_after_cutoff_excluded") is not True \
            or not isinstance(training.get("observations_hash"), str) \
            or cutoff is None or cutoff <= 0 or generated is None or generated < cutoff \
            or validity is None or validity <= generated:
        return "training/generation"
    dmin, dmax, lmin, lmax = (_int(training.get(k)) for k in
                             ("decision_min_ms", "decision_max_ms", "label_min_ms", "label_max_ms"))
    if any(v is None for v in (dmin, dmax, lmin, lmax)) \
            or not 0 < dmin <= dmax <= cutoff or not dmin <= lmin <= lmax <= cutoff:
        return "training_chronology"
    approval = a.get("approval")
    if not isinstance(approval, dict) or set(approval) != {"state", "reason_code", "economically_approved"} \
            or approval.get("economically_approved") is not False \
            or approval.get("state") != "NOT_APPROVED" \
            or approval.get("reason_code") != APPROVAL_DECISION_REQUIRED \
            or a.get("state") not in (STATE_FITTED, STATE_OOS_VALIDATED):
        return "approval/state"
    metrics = a.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != {"brier", "log_loss", "oos"} or metrics["log_loss"] is not None:
        return "metrics"
    predicted_count = sum(b["n"] for b in bins if b["supported"])
    expected_brier = (sum(b["successes"] * (b["p"] - 1) ** 2 + (b["n"] - b["successes"]) * b["p"] ** 2
                          for b in bins if b["supported"]) / predicted_count) if predicted_count else None
    if expected_brier is None:
        if metrics["brier"] is not None:
            return "in_sample_brier"
    elif _finite(metrics["brier"]) is None or not math.isclose(metrics["brier"], expected_brier, rel_tol=1e-12, abs_tol=1e-12):
        return "in_sample_brier"
    if a.get("state") == STATE_OOS_VALIDATED:
        oos = metrics.get("oos")
        if not isinstance(oos, dict) or _int(oos.get("predictions")) is None or oos["predictions"] <= 0 \
                or _finite(oos.get("brier")) is None or not 0 <= oos["brier"] <= 1 \
                or _int(oos.get("evaluated_at_ms")) is None \
                or not isinstance(oos.get("opportunity_keys"), list) \
                or len(set(oos["opportunity_keys"])) != oos["predictions"] \
                or set(oos["opportunity_keys"]) & set(keys):
            return "oos"
        evaluated = _int(oos.get("evaluated_at_ms"))
        decision_min = _int(oos.get("decision_min_ms"))
        label_max = _int(oos.get("label_max_ms"))
        if decision_min is None or label_max is None or not cutoff < decision_min <= label_max <= evaluated \
                or not generated <= evaluated <= validity:
            return "oos_chronology"
        reliability = oos.get("reliability")
        count, error, seen_bins = 0, 0.0, set()
        if not isinstance(reliability, list):
            return "oos_reliability"
        for item in reliability:
            if not isinstance(item, dict):
                return "oos_reliability"
            index, n, successes = (_int(item.get(k)) for k in ("bin", "n", "successes"))
            if index is None or not 0 <= index < BIN_COUNT or index in seen_bins \
                    or n is None or n <= 0 or successes is None or not 0 <= successes <= n \
                    or bins[index]["supported"] is not True:
                return "oos_reliability"
            seen_bins.add(index)
            p = bins[index]["p"]
            interval = wilson_interval(successes, n)
            for field, expected in (("predicted", p), ("observed", successes / n),
                                    ("wilson_low", interval[0]), ("wilson_high", interval[1])):
                value = _finite(item.get(field))
                if value is None or not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
                    return "oos_reliability"
            count += n
            error += successes * (p - 1) ** 2 + (n - successes) * p ** 2
        if count != oos["predictions"] or not math.isclose(oos["brier"], error / count, rel_tol=1e-12, abs_tol=1e-12):
            return "oos_counts/brier"
    elif metrics.get("oos") is not None:
        return "fitted_with_oos"
    return None


def verify_artifact(artifact: Any, *, model_fingerprint: Any = None,
                    population: Any = None, event: Any = None,
                    dataset_hash: Any = None, now_ms: Any = None,
                    score_config_hash: Any = None, expected_event_definition: Any = None
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
    try:
        problem = _artifact_semantics(artifact)
    except (TypeError, ValueError, KeyError, OverflowError):
        problem = "schema"
    if problem:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_INVALID, "detail": problem}
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
    if score_config_hash is not None and artifact.get("score_config_hash") != score_config_hash:
        return {"ok": False, "state": STATE_UNAVAILABLE, "reason_code": FINGERPRINT_MISMATCH}
    if expected_event_definition is not None and artifact.get("event_definition") != expected_event_definition:
        return {"ok": False, "state": STATE_UNAVAILABLE, "reason_code": EVENT_MISMATCH}
    validade = _int((artifact.get("generation") or {}).get("valid_until_ms"))
    agora = _clock(now_ms)
    if agora is None or agora < artifact["generation"]["generated_at_ms"]:
        return {"ok": False, "state": STATE_UNAVAILABLE, "reason_code": ARTIFACT_INVALID,
                "detail": "observation_clock"}
    if agora > validade:
        return {"ok": False, "state": STATE_UNAVAILABLE,
                "reason_code": ARTIFACT_EXPIRED}
    return {"ok": True, "state": estado, "reason_code": OK,
            "event": artifact.get("event"),
            "bins_supported": (artifact.get("coverage") or {}).get("bins_supported")}


def predict(artifact: Any, *, score: Any, model_fingerprint: Any = None,
            population: Any = None, event: Any = None,
            dataset_hash: Any = None, now_ms: Any = None,
            score_config_hash: Any = None, expected_event_definition: Any = None) -> Dict[str, Any]:
    """Probabilidade da FAIXA suportada — ou indisponibilidade explícita."""
    verdict = verify_artifact(artifact, model_fingerprint=model_fingerprint,
                              population=population, event=event,
                              dataset_hash=dataset_hash, now_ms=now_ms,
                              score_config_hash=score_config_hash,
                              expected_event_definition=expected_event_definition)
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
    agora = _clock(now_ms)
    definition = artifact["event_definition"]
    preparadas = prepare_observations(holdout, cutoff_ms=agora,
                                      event=artifact["event"],
                                      horizon_ms=definition["horizon_bars"] * definition["bar_ms"],
                                      bar_ms=definition["bar_ms"])
    posteriores, antecipadas = [], 0
    train_keys = set(artifact["training"]["opportunity_keys"])
    overlap = 0
    for linha in preparadas["rows"]:
        if linha["opportunity_key"] in train_keys:
            overlap += 1
            continue
        if linha["decision_ts_ms"] <= corte:
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
                             "train_opportunity_overlap_discarded": overlap,
                             "excluded": preparadas["excluded"],
                             "predicted": n_total},
                "uncertainty": "WILSON_95_PER_BIN",
                "evaluated_at_ms": agora,
                "opportunity_keys": sorted(r["opportunity_key"] for r in posteriores
                                            if _bin_of(artifact, r["score"])["supported"]),
                "decision_min_ms": min((r["decision_ts_ms"] for r in posteriores), default=None),
                "label_max_ms": max((r["label_available_ts_ms"] for r in posteriores), default=None)}
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


OOS_PAYOFF_CONTRACT = "R08E_OOS_NET_PAYOFF_V1"
OOS_PAYOFF_SOURCE = "R10A_FROZEN_MANAGEMENT_REPLAY_OOS"
PARTIAL_PAYOFF = "PARTIAL_TP1_PLUS_RUNNER"


def build_oos_payoff_evidence(rows: Sequence[Any], *, management_hash: str,
                              dataset_hash: str, costs_hash: str, event: str,
                              payoff_contract: str = PARTIAL_PAYOFF,
                              cutoff_ms: Any, now_ms: Any) -> Dict[str, Any]:
    """Agrega R JÁ líquido de replay OOS com identidades congeladas por linha.

    O produtor oficial decide se custos/resultado são observados ou modelados;
    esta fronteira não converte um scalar avulso em prova do replay.
    """
    cutoff, now = _int(cutoff_ms), _clock(now_ms)
    invalid = {"ok": False, "reason_code": "OOS_PAYOFF_EVIDENCE_INVALID", "evidence": None}
    if event not in EVENTS or payoff_contract != PARTIAL_PAYOFF or cutoff is None or cutoff <= 0 \
            or now is None or now <= cutoff or not isinstance(rows, (list, tuple)) or not rows \
            or any(not isinstance(h, str) or not h.strip() for h in (management_hash, dataset_hash, costs_hash)):
        return invalid
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, Mapping):
            return invalid
        key = row.get("opportunity_key")
        decision, available = _int(row.get("decision_ts_ms")), _int(row.get("label_available_ts_ms"))
        net = _finite(row.get("net_r"))
        if not isinstance(key, str) or not key or key in seen or decision is None or available is None \
                or not cutoff < decision <= available <= now or net is None \
                or row.get("event") != event or not isinstance(row.get("label"), bool) \
                or row.get("costs_included") is not True \
                or row.get("management_hash") != management_hash \
                or row.get("dataset_hash") != dataset_hash or row.get("costs_hash") != costs_hash \
                or (event == EVENT_NET_POSITIVE and row["label"] is not (net > 0)):
            return invalid
        seen.add(key)
        clean.append({"opportunity_key": key, "decision_ts_ms": decision,
                      "label_available_ts_ms": available, "net_r": net,
                      "event": event, "label": row["label"], "costs_included": True,
                      "management_hash": management_hash, "dataset_hash": dataset_hash,
                      "costs_hash": costs_hash})
    clean.sort(key=lambda r: r["opportunity_key"])
    try:
        expected = math.fsum(r["net_r"] for r in clean) / len(clean)
    except (ValueError, OverflowError):
        return invalid
    if not math.isfinite(expected):
        return invalid
    evidence = {"contract": OOS_PAYOFF_CONTRACT, "source": OOS_PAYOFF_SOURCE,
                "stage": "OOS", "management_hash": management_hash,
                "dataset_hash": dataset_hash, "costs_hash": costs_hash,
                "event": event, "payoff_contract": payoff_contract,
                "cutoff_ms": cutoff, "observed_at_ms": now,
                "costs_already_included": True, "rows": clean,
                "sample_size": len(clean), "expected_net_payoff_r": expected}
    evidence["evidence_hash"] = _hash(evidence)
    return {"ok": True, "reason_code": OK, "evidence": evidence}


def verify_oos_payoff_evidence(evidence: Any, *, management_hash: Any,
                               dataset_hash: Any, costs_hash: Any, event: Any,
                               payoff_contract: str = PARTIAL_PAYOFF,
                               now_ms: Any = None) -> Dict[str, Any]:
    refusal = {"ok": False, "reason_code": "OOS_PAYOFF_EVIDENCE_INVALID"}
    if not isinstance(evidence, Mapping) or any(not isinstance(h, str) or not h for h in
                                               (management_hash, dataset_hash, costs_hash)):
        return refusal
    now = _clock(now_ms)
    observed = _int(evidence.get("observed_at_ms"))
    if now is None or observed is None or observed > now:
        return refusal
    rebuilt = build_oos_payoff_evidence(evidence.get("rows"), management_hash=management_hash,
                                        dataset_hash=dataset_hash, costs_hash=costs_hash, event=event,
                                        payoff_contract=payoff_contract,
                                        cutoff_ms=evidence.get("cutoff_ms"), now_ms=observed)
    if not rebuilt.get("ok") or rebuilt["evidence"] != evidence:
        return refusal
    return {"ok": True, "reason_code": OK,
            "expected_net_payoff_r": evidence["expected_net_payoff_r"],
            "sample_size": evidence["sample_size"], "evidence_hash": evidence["evidence_hash"]}


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
