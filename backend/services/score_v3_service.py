"""R08D — Score V3 de PESQUISA: allowlist, categorias, caps e decomposição.

Não é a ablação conf-only do laboratório local do R08A, que continua existindo
e intocada (o teste de isolamento daquele laboratório exige que nenhum outro
módulo sequer o mencione). Aqui há uma fórmula própria, versionada e inativa por
padrão, organizada por categorias com cap explícito e decomposição verificável.

Regras que o contrato impõe, e que os testes cobram:
  • ADX mede FORÇA: nunca decide comprar ou vender, e seu significado depende
    do playbook (força ajuda continuação e atrapalha reversão de range);
  • funding é DIRECIONAL e entra uma única vez — não existe bônus somado por
    "neutro" e outro por "valor baixo";
  • confluência composta fica FORA por padrão (dupla contagem); quando ligada
    explicitamente, ela SUBSTITUI as categorias que já contém;
  • evidência ausente não vira zero: sai da escala disponível e é reportada;
  • valor inválido (não finito ou fora do domínio) não vira evidência;
  • pontuação técnica NÃO é probabilidade de lucro. Sem calibração V3 própria,
    aprovação econômica e elegibilidade LIVE ficam indisponíveis — e não há
    fallback para bins, p_global ou tier da V2.

Unidades: o score é em PONTOS 0–100 sobre a escala disponível. `adx` é o ADX
bruto; `funding_pct` está em pontos percentuais; distâncias estão em múltiplos
de ATR; percentuais estão em 0–100 quando o nome diz `_pct`.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

SCORE_VERSION = "R08D_SCORE_V3_RESEARCH_V1"
CONTRACT = "CANDIDATE_POLICY"
MODE_ENV = "R08_SCORE_V3_MODE"
MODE_INACTIVE = "inactive"
MODE_SIMULATION = "simulation"

STATE_OK = "OK"
STATE_UNAVAILABLE = "UNAVAILABLE"
STATE_INVALID_INPUT = "INVALID_INPUT"

# ── Motivos (vocabulário fechado) ───────────────────────────────────────────
OK = "OK"
FEATURE_MISSING = "FEATURE_MISSING"
INPUT_NOT_NUMERIC = "INPUT_NOT_NUMERIC"
INPUT_NOT_FINITE = "INPUT_NOT_FINITE"
INPUT_OUT_OF_DOMAIN = "INPUT_OUT_OF_DOMAIN"
SIDE_REQUIRED = "SIDE_REQUIRED"
PLAYBOOK_UNKNOWN = "PLAYBOOK_UNKNOWN"
EVIDENCE_BELOW_FLOOR = "EVIDENCE_BELOW_FLOOR"
SCORE_INACTIVE = "SCORE_INACTIVE"
CALIBRATION_ABSENT = "CALIBRATION_ABSENT"
CALIBRATION_FINGERPRINT_MISMATCH = "CALIBRATION_FINGERPRINT_MISMATCH"
CALIBRATION_SAMPLE_INSUFFICIENT = "CALIBRATION_SAMPLE_INSUFFICIENT"
V2_FALLBACK_FORBIDDEN = "V2_FALLBACK_FORBIDDEN"
PROBABILITY_UNAVAILABLE = "PROBABILITY_UNAVAILABLE"
COSTS_UNKNOWN = "COSTS_UNKNOWN"
OUT_OF_SAMPLE_REQUIRED = "OUT_OF_SAMPLE_REQUIRED"

REASON_CODES = frozenset({
    OK, FEATURE_MISSING, INPUT_NOT_NUMERIC, INPUT_NOT_FINITE, INPUT_OUT_OF_DOMAIN,
    SIDE_REQUIRED, PLAYBOOK_UNKNOWN, EVIDENCE_BELOW_FLOOR, SCORE_INACTIVE,
    CALIBRATION_ABSENT, CALIBRATION_FINGERPRINT_MISMATCH,
    CALIBRATION_SAMPLE_INSUFFICIENT, V2_FALLBACK_FORBIDDEN,
    PROBABILITY_UNAVAILABLE, COSTS_UNKNOWN, OUT_OF_SAMPLE_REQUIRED,
})

SIDE_LONG = "long"
SIDE_SHORT = "short"
SIDES = (SIDE_LONG, SIDE_SHORT)

PLAYBOOK_TREND_PULLBACK = "TREND_PULLBACK"
PLAYBOOK_TREND_BREAKOUT = "TREND_BREAKOUT"
PLAYBOOK_RANGE_REVERSION = "RANGE_REVERSION"
PLAYBOOKS = (PLAYBOOK_TREND_PULLBACK, PLAYBOOK_TREND_BREAKOUT, PLAYBOOK_RANGE_REVERSION)
CONTINUATION_PLAYBOOKS = (PLAYBOOK_TREND_PULLBACK, PLAYBOOK_TREND_BREAKOUT)

CATEGORY_REGIME_MTF = "regime_mtf"
CATEGORY_STRUCTURE = "structure"
CATEGORY_TRIGGER = "trigger"
CATEGORY_ENTRY_QUALITY = "entry_quality"
CATEGORY_VOLUME_LIQUIDITY = "volume_liquidity"
CATEGORY_DERIVATIVES = "derivatives"
CATEGORY_CONFLUENCE_COMPOSITE = "confluence_composite"
CATEGORY_CAPS = {
    CATEGORY_REGIME_MTF: 25.0,
    CATEGORY_STRUCTURE: 20.0,
    CATEGORY_TRIGGER: 20.0,
    CATEGORY_ENTRY_QUALITY: 15.0,
    CATEGORY_VOLUME_LIQUIDITY: 12.0,
    CATEGORY_DERIVATIVES: 8.0,
}
#: Quando o composto V2 é ligado, ele OCUPA as categorias que já contém —
#: nunca soma por cima delas.
COMPOSITE_SUPERSEDES = (CATEGORY_STRUCTURE, CATEGORY_TRIGGER)
COMPOSITE_CAP = CATEGORY_CAPS[CATEGORY_STRUCTURE] + CATEGORY_CAPS[CATEGORY_TRIGGER]

SENSE_MAGNITUDE = "magnitude"      # força; nunca define lado
SENSE_QUALITY = "quality"          # maior é melhor
SENSE_INVERSE = "inverse"          # menor é melhor
SENSE_DIRECTIONAL = "directional"  # sinal depende do lado da operação

LIMITATIONS = [
    "Pontuação técnica não é probabilidade de lucro.",
    "Score com coberturas diferentes não é comparável ponto a ponto.",
    "ADX é força de tendência: não indica direção nem aprova entrada.",
    "Sem calibração V3 própria não há probabilidade, tier ou aprovação econômica.",
    "EV líquido exige probabilidade fora da amostra e custos conhecidos.",
    "Ausência é ausência: não é zero e não reduz o score por omissão.",
]


def selected_mode() -> str:
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_SIMULATION if value == MODE_SIMULATION else MODE_INACTIVE


def score_active() -> bool:
    return selected_mode() == MODE_SIMULATION


# ── Allowlist de features ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Feature:
    name: str
    category: str
    unit: str
    domain: Tuple[float, float]
    sense: str
    weight: float
    evidence_key: str
    normalizer: Tuple[Tuple[str, Any], ...]
    playbooks: Tuple[str, ...] = PLAYBOOKS
    description: str = ""

    def normalizer_map(self) -> Dict[str, Any]:
        return dict(self.normalizer)

    def spec(self) -> Dict[str, Any]:
        return {"name": self.name, "category": self.category, "unit": self.unit,
                "domain": list(self.domain), "sense": self.sense, "weight": self.weight,
                "evidence_key": self.evidence_key,
                "normalizer": self.normalizer_map(),
                "playbooks": list(self.playbooks), "description": self.description}


def _ramp(spec: Dict[str, Any], value: float) -> float:
    lo, hi = float(spec["lo"]), float(spec["hi"])
    if hi == lo:
        raise ValueError("normalizador degenerado")
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


def _inverse_ramp(spec: Dict[str, Any], value: float) -> float:
    return 1.0 - _ramp(spec, value)


def _signed_ramp(spec: Dict[str, Any], value: float, *, sign: int) -> float:
    """Mapeamento ÚNICO e contínuo para feature direcional.

    0.5 é o ponto neutro; custo para o lado da operação puxa para baixo e
    prêmio puxa para cima. Uma única parcela — sem bônus extra por "neutro".
    """
    scale = float(spec["scale"])
    if scale <= 0:
        raise ValueError("escala direcional inválida")
    normalized = 0.5 - (sign * value) / (2.0 * scale)
    return min(1.0, max(0.0, normalized))


ALLOWLIST: Tuple[Feature, ...] = (
    Feature(name="adx", category=CATEGORY_REGIME_MTF, unit="adx_points",
            domain=(0.0, 100.0), sense=SENSE_MAGNITUDE, weight=12.0,
            evidence_key="trend_strength",
            normalizer=(("kind", "playbook_dependent_ramp"),
                        ("continuation", {"kind": "ramp", "lo": 15.0, "hi": 35.0}),
                        ("range", {"kind": "inverse_ramp", "lo": 10.0, "hi": 25.0})),
            description="Força da tendência. Ajuda continuação, atrapalha reversão de range."),
    Feature(name="htf_alignment_ratio", category=CATEGORY_REGIME_MTF, unit="ratio",
            domain=(0.0, 1.0), sense=SENSE_QUALITY, weight=18.0,
            evidence_key="htf_alignment",
            normalizer=(("kind", "ramp"), ("lo", 0.0), ("hi", 1.0)),
            description="Fração de timeframes superiores confirmados a favor."),
    Feature(name="structure_quality", category=CATEGORY_STRUCTURE, unit="ratio",
            domain=(0.0, 1.0), sense=SENSE_QUALITY, weight=12.0,
            evidence_key="structure_quality",
            normalizer=(("kind", "ramp"), ("lo", 0.0), ("hi", 1.0)),
            description="Qualidade da estrutura que sustenta stop e alvo."),
    Feature(name="level_distance_atr", category=CATEGORY_STRUCTURE, unit="atr_multiple",
            domain=(0.0, 10.0), sense=SENSE_INVERSE, weight=8.0,
            evidence_key="level_distance",
            normalizer=(("kind", "inverse_ramp"), ("lo", 0.2), ("hi", 3.0)),
            description="Distância da entrada ao nível estrutural de referência."),
    Feature(name="trigger_body_ratio", category=CATEGORY_TRIGGER, unit="ratio",
            domain=(0.0, 1.0), sense=SENSE_QUALITY, weight=12.0,
            evidence_key="trigger_bar_body",
            normalizer=(("kind", "ramp"), ("lo", 0.2), ("hi", 0.8)),
            description="Corpo da vela de gatilho sobre o range dela."),
    Feature(name="trigger_follow_through_atr", category=CATEGORY_TRIGGER, unit="atr_multiple",
            domain=(0.0, 10.0), sense=SENSE_QUALITY, weight=8.0,
            evidence_key="trigger_follow_through",
            normalizer=(("kind", "ramp"), ("lo", 0.0), ("hi", 1.0)),
            description="Avanço do fechamento além da referência do gatilho."),
    Feature(name="rr_tp2", category=CATEGORY_ENTRY_QUALITY, unit="ratio",
            domain=(0.0, 50.0), sense=SENSE_QUALITY, weight=10.0,
            evidence_key="geometry_rr",
            normalizer=(("kind", "ramp"), ("lo", 1.8), ("hi", 4.0)),
            description="R:R do TP2 já validado pela geometria do núcleo."),
    Feature(name="entry_distance_atr", category=CATEGORY_ENTRY_QUALITY, unit="atr_multiple",
            domain=(0.0, 10.0), sense=SENSE_INVERSE, weight=5.0,
            evidence_key="entry_distance",
            normalizer=(("kind", "inverse_ramp"), ("lo", 0.0), ("hi", 1.5)),
            description="Perseguição de preço: entrada longe do gatilho vale menos."),
    Feature(name="volume_ratio", category=CATEGORY_VOLUME_LIQUIDITY, unit="ratio",
            domain=(0.0, 50.0), sense=SENSE_QUALITY, weight=7.0,
            evidence_key="volume",
            normalizer=(("kind", "ramp"), ("lo", 0.8), ("hi", 2.0)),
            description="Volume da vela de gatilho sobre a média da janela."),
    Feature(name="spread_pct", category=CATEGORY_VOLUME_LIQUIDITY, unit="percent",
            domain=(0.0, 100.0), sense=SENSE_INVERSE, weight=5.0,
            evidence_key="spread",
            normalizer=(("kind", "inverse_ramp"), ("lo", 0.02), ("hi", 0.25)),
            description="Spread relativo no instante da decisão."),
    Feature(name="funding_pct", category=CATEGORY_DERIVATIVES, unit="percent_points",
            domain=(-100.0, 100.0), sense=SENSE_DIRECTIONAL, weight=8.0,
            evidence_key="funding",
            normalizer=(("kind", "signed_ramp"), ("scale", 0.05)),
            description="Funding é custo/prêmio direcional — uma única parcela."),
)
ALLOWLIST_BY_NAME = {feature.name: feature for feature in ALLOWLIST}

COMPOSITE_FEATURE = Feature(
    name="confluence_pct", category=CATEGORY_CONFLUENCE_COMPOSITE, unit="points_0_100",
    domain=(0.0, 100.0), sense=SENSE_QUALITY, weight=COMPOSITE_CAP,
    evidence_key="confluence_composite",
    normalizer=(("kind", "ramp"), ("lo", 40.0), ("hi", 90.0)),
    description=("Composto V2 com normalização CONTÍNUA — sem os degraus da V2. "
                 "Só entra quando ligado, e então substitui estrutura e gatilho."))

#: Por que uma feature conhecida ficou de fora — decisão registrada, não omissão.
EXCLUDED_FEATURES = {
    "confluence_pct": "Composto: por padrão duplicaria estrutura, gatilho e volume.",
    "probability_tp1": "Probabilidade não é insumo de score técnico.",
    "tier": "Tier é saída de política, não evidência de mercado.",
    "learning_multiplier": "Ajuste aprendido pertence à política versionada (R11C), não ao score.",
    "score_v2": "Reaproveitar a V2 dentro da V3 reintroduziria seus degraus e adjusters.",
}


# ── Configuração ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScoreConfig:
    include_composite_confluence: bool = False
    min_evidence_fraction: float = 0.6
    population: str = "RESEARCH_SHADOW"

    def __post_init__(self) -> None:
        if not isinstance(self.include_composite_confluence, bool):
            raise ValueError("include_composite_confluence: booleano obrigatório")
        if not isinstance(self.min_evidence_fraction, (int, float)) or isinstance(self.min_evidence_fraction, bool):
            raise ValueError("min_evidence_fraction: número obrigatório")
        if not 0.0 < float(self.min_evidence_fraction) <= 1.0:
            raise ValueError("min_evidence_fraction deve estar em (0, 1]")
        if not isinstance(self.population, str) or not self.population.strip():
            raise ValueError("population: identificador obrigatório")

    def as_dict(self) -> Dict[str, Any]:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


DEFAULT_CONFIG = ScoreConfig()


def active_features(config: Optional[ScoreConfig] = None) -> Tuple[Feature, ...]:
    """Features que contam nesta configuração — composto substitui, não soma."""
    cfg = config or DEFAULT_CONFIG
    if not cfg.include_composite_confluence:
        return ALLOWLIST
    kept = tuple(feature for feature in ALLOWLIST
                 if feature.category not in COMPOSITE_SUPERSEDES)
    return kept + (COMPOSITE_FEATURE,)


def active_caps(config: Optional[ScoreConfig] = None) -> Dict[str, float]:
    cfg = config or DEFAULT_CONFIG
    caps = {name: value for name, value in CATEGORY_CAPS.items()}
    if cfg.include_composite_confluence:
        for category in COMPOSITE_SUPERSEDES:
            caps.pop(category, None)
        caps[CATEGORY_CONFLUENCE_COMPOSITE] = COMPOSITE_CAP
    return caps


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def model_fingerprint(*, playbook: str, config: Optional[ScoreConfig] = None) -> str:
    """Fingerprint do MODELO: fórmula, features, pesos, caps, playbook e população.

    Trocar qualquer um deles troca o fingerprint — e uma calibração de outro
    fingerprint não serve.
    """
    cfg = config or DEFAULT_CONFIG
    if playbook not in PLAYBOOKS:
        raise ValueError("playbook fora do catálogo")
    return _hash({
        "formula": SCORE_VERSION,
        "features": [feature.spec() for feature in active_features(cfg)],
        "caps": active_caps(cfg),
        "playbook": playbook,
        "population": cfg.population,
        "config": cfg.as_dict(),
    })


# ── Normalização de um valor ────────────────────────────────────────────────
def _read_value(feature: Feature, raw: Any) -> Tuple[Optional[float], Optional[str]]:
    if raw is None:
        return None, FEATURE_MISSING
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, INPUT_NOT_NUMERIC
    value = float(raw)
    if not math.isfinite(value):
        return None, INPUT_NOT_FINITE
    low, high = feature.domain
    if value < low or value > high:
        return None, INPUT_OUT_OF_DOMAIN
    return value, None


def _normalize(feature: Feature, value: float, *, playbook: str, side: str) -> float:
    spec = feature.normalizer_map()
    kind = spec["kind"]
    if kind == "ramp":
        return _ramp(spec, value)
    if kind == "inverse_ramp":
        return _inverse_ramp(spec, value)
    if kind == "signed_ramp":
        return _signed_ramp(spec, value, sign=1 if side == SIDE_LONG else -1)
    if kind == "playbook_dependent_ramp":
        branch = spec["continuation"] if playbook in CONTINUATION_PLAYBOOKS else spec["range"]
        return _ramp(branch, value) if branch["kind"] == "ramp" else _inverse_ramp(branch, value)
    raise ValueError(f"normalizador desconhecido: {kind}")


# ── Score ───────────────────────────────────────────────────────────────────
def score(features: Mapping[str, Any], *, playbook: str, side: str,
          config: Optional[ScoreConfig] = None) -> Dict[str, Any]:
    """Score V3 com decomposição por categoria, caps e cobertura explícita."""
    cfg = config or DEFAULT_CONFIG
    base = {
        "score_version": SCORE_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "playbook": playbook,
        "side": side,
        "population": cfg.population,
        "probability": None,
        "tier": None,
        "is_probability": False,
        "limitations": list(LIMITATIONS),
    }
    if side not in SIDES:
        return {**base, "state": STATE_INVALID_INPUT, "reason_codes": (SIDE_REQUIRED,),
                "score": None, "model_fingerprint": None}
    if playbook not in PLAYBOOKS:
        return {**base, "state": STATE_INVALID_INPUT, "reason_codes": (PLAYBOOK_UNKNOWN,),
                "score": None, "model_fingerprint": None}
    if not isinstance(features, Mapping):
        return {**base, "state": STATE_INVALID_INPUT, "reason_codes": (INPUT_NOT_NUMERIC,),
                "score": None, "model_fingerprint": None}

    caps = active_caps(cfg)
    decomposition: Dict[str, Dict[str, Any]] = {
        category: {"cap": cap, "raw_points": 0.0, "points": 0.0,
                   "available_weight": 0.0, "declared_weight": 0.0,
                   "features": {}, "missing": [], "invalid": {}}
        for category, cap in caps.items()
    }
    reasons: List[str] = []
    for feature in active_features(cfg):
        if playbook not in feature.playbooks:
            continue
        bucket = decomposition[feature.category]
        bucket["declared_weight"] += feature.weight
        value, problem = _read_value(feature, features.get(feature.name))
        if problem is not None:
            # Ausente sai da escala; inválido NÃO vira evidência nem zero.
            if problem == FEATURE_MISSING:
                bucket["missing"].append(feature.name)
            else:
                bucket["invalid"][feature.name] = problem
            if problem not in reasons:
                reasons.append(problem)
            continue
        normalized = _normalize(feature, value, playbook=playbook, side=side)
        contribution = normalized * feature.weight
        bucket["available_weight"] += feature.weight
        bucket["raw_points"] += contribution
        bucket["features"][feature.name] = {
            "raw": value, "normalized": normalized, "weight": feature.weight,
            "contribution": contribution, "sense": feature.sense,
            "evidence_key": feature.evidence_key,
        }

    total_points = 0.0
    available_weight = 0.0
    declared_weight = 0.0
    for category, bucket in decomposition.items():
        capped = min(bucket["raw_points"], bucket["cap"])
        bucket["points"] = capped
        bucket["cap_applied"] = capped < bucket["raw_points"] - 1e-12
        bucket["state"] = STATE_OK if bucket["available_weight"] > 0 else STATE_UNAVAILABLE
        total_points += capped
        available_weight += bucket["available_weight"]
        declared_weight += bucket["declared_weight"]
    coverage = (available_weight / declared_weight) if declared_weight > 0 else 0.0
    fingerprint = model_fingerprint(playbook=playbook, config=cfg)
    payload = {
        **base,
        "model_fingerprint": fingerprint,
        "score": round(total_points, 10),
        "max_possible_points": round(sum(
            min(bucket["available_weight"], bucket["cap"]) for bucket in decomposition.values()), 10),
        "scale_points": round(sum(min(bucket["declared_weight"], bucket["cap"])
                                  for bucket in decomposition.values()), 10),
        "evidence_coverage": coverage,
        "decomposition": decomposition,
        "missing_by_category": {category: list(bucket["missing"])
                                for category, bucket in decomposition.items()},
        "invalid_by_category": {category: dict(bucket["invalid"])
                                for category, bucket in decomposition.items()},
        "reason_codes": tuple(reasons) if reasons else (OK,),
    }
    if coverage < cfg.min_evidence_fraction:
        payload["state"] = STATE_UNAVAILABLE
        payload["score"] = None
        payload["reason_codes"] = tuple(dict.fromkeys([EVIDENCE_BELOW_FLOOR, *reasons]))
        return payload
    payload["state"] = STATE_OK
    return payload


# ── Calibração e aprovação econômica ────────────────────────────────────────
@dataclass(frozen=True)
class V3Calibration:
    """Calibração PRÓPRIA da V3. Nenhum bin/probabilidade da V2 é aceito."""
    model_fingerprint: str
    population: str
    sample_size: int
    source: str = "R08D_V3_OUT_OF_SAMPLE"

    def __post_init__(self) -> None:
        for key in ("model_fingerprint", "population", "source"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key}: obrigatório")
        if isinstance(self.sample_size, bool) or not isinstance(self.sample_size, int) or self.sample_size < 0:
            raise ValueError("sample_size: inteiro não negativo")


MIN_CALIBRATION_SAMPLE = 200


def calibration_verdict(score_payload: Mapping[str, Any],
                        calibration: Optional[V3Calibration] = None) -> Dict[str, Any]:
    """Probabilidade só existe com calibração V3 do MESMO fingerprint.

    Sem ela: score técnico continua existindo, probabilidade não. Não há
    fallback para p_global, bins ou tier da V2.
    """
    unavailable = {"probability": None, "calibration_state": STATE_UNAVAILABLE,
                   "tier": None, "v2_fallback_used": False}
    if calibration is None:
        return {**unavailable, "reason_code": CALIBRATION_ABSENT}
    if not isinstance(calibration, V3Calibration):
        return {**unavailable, "reason_code": V2_FALLBACK_FORBIDDEN}
    expected = score_payload.get("model_fingerprint")
    if not expected or calibration.model_fingerprint != expected:
        return {**unavailable, "reason_code": CALIBRATION_FINGERPRINT_MISMATCH}
    if calibration.population != score_payload.get("population"):
        return {**unavailable, "reason_code": CALIBRATION_FINGERPRINT_MISMATCH}
    if calibration.sample_size < MIN_CALIBRATION_SAMPLE:
        return {**unavailable, "reason_code": CALIBRATION_SAMPLE_INSUFFICIENT}
    # Existe calibração válida: a probabilidade em si é produzida pelo estudo
    # que a construiu; este serviço apenas declara que ela está disponível.
    return {"probability": None, "calibration_state": "AVAILABLE",
            "tier": None, "v2_fallback_used": False, "reason_code": OK}


def net_ev(*, probability_out_of_sample: Any, rr_tp2: Any, cost_r: Any) -> Dict[str, Any]:
    """EV líquido — filtro POSTERIOR, com probabilidade fora da amostra e custo
    conhecido em múltiplos de R. Nunca deriva probabilidade do próprio score."""
    if probability_out_of_sample is None:
        return {"available": False, "reason_code": PROBABILITY_UNAVAILABLE, "ev_r": None}
    if isinstance(probability_out_of_sample, bool) or not isinstance(probability_out_of_sample, (int, float)):
        return {"available": False, "reason_code": OUT_OF_SAMPLE_REQUIRED, "ev_r": None}
    prob = float(probability_out_of_sample)
    if not math.isfinite(prob) or not 0.0 <= prob <= 1.0:
        return {"available": False, "reason_code": OUT_OF_SAMPLE_REQUIRED, "ev_r": None}
    numbers = {}
    for name, raw in (("rr_tp2", rr_tp2), ("cost_r", cost_r)):
        if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            return {"available": False, "reason_code": COSTS_UNKNOWN, "ev_r": None}
        numbers[name] = float(raw)
    ev = prob * numbers["rr_tp2"] - (1.0 - prob) * 1.0 - abs(numbers["cost_r"])
    return {"available": True, "reason_code": OK, "ev_r": ev,
            "components": {"probability": prob, **numbers}}


def economic_verdict(score_payload: Mapping[str, Any],
                     calibration: Optional[V3Calibration] = None,
                     ev_payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Aprovação econômica e elegibilidade LIVE — indisponíveis sem calibração
    V3 e EV líquido. Score técnico sozinho não aprova nada."""
    calib = calibration_verdict(score_payload, calibration)
    verdict = {"economic_approval": STATE_UNAVAILABLE,
               "live_eligibility": STATE_UNAVAILABLE,
               "calibration_state": calib["calibration_state"],
               "v2_fallback_used": False}
    if score_payload.get("state") != STATE_OK:
        return {**verdict, "reason_code": EVIDENCE_BELOW_FLOOR}
    if calib["calibration_state"] != "AVAILABLE":
        return {**verdict, "reason_code": calib["reason_code"]}
    if not ev_payload or not ev_payload.get("available"):
        return {**verdict,
                "reason_code": (ev_payload or {}).get("reason_code", PROBABILITY_UNAVAILABLE)}
    # Mesmo com tudo disponível, a elegibilidade LIVE não é concedida aqui:
    # ela depende de simulação prospectiva, aprovação humana e canário (R12).
    return {**verdict, "economic_approval": "PENDING_SIMULATION",
            "reason_code": OK, "ev_r": ev_payload.get("ev_r")}


def score_manifest(config: Optional[ScoreConfig] = None) -> Dict[str, Any]:
    """Manifest congelado da fórmula — emitido antes de qualquer outcome."""
    cfg = config or DEFAULT_CONFIG
    features = active_features(cfg)
    return {
        "score_version": SCORE_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "config": cfg.as_dict(),
        "caps": active_caps(cfg),
        "caps_total": sum(active_caps(cfg).values()),
        "features": [feature.spec() for feature in features],
        "evidence_keys": sorted({feature.evidence_key for feature in features}),
        "excluded_features": dict(EXCLUDED_FEATURES),
        "composite_supersedes": list(COMPOSITE_SUPERSEDES),
        "fingerprints": {playbook: model_fingerprint(playbook=playbook, config=cfg)
                         for playbook in PLAYBOOKS},
        "outcomes_consulted": False,
        "is_probability": False,
        "approved_for_production": False,
        "limitations": list(LIMITATIONS),
    }
