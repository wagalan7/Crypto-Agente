"""R08B: telemetria prospectiva pura; nunca decide, estima ou consulta IO.

Somente campos enumerados são aceitos. Valores ausentes/não finitos viram
None, estágios não visitados ficam NOT_OBSERVED. Toda saída é cópia JSON.
"""
from __future__ import annotations

import json
import math
from numbers import Real

KEY = "r08_score_trace"
VERSION = "r08b.1"
MAX_BYTES = 24576
MAX_FACTORS = 64
CATEGORIES = ("momentum", "trend", "macd", "volume", "bollinger", "pattern",
              "structure", "volatility", "smc", "derivatives", "divergence",
              "vp_vwap", "mtf")
PATTERNS = ("ascending_wedge", "descending_wedge", "symmetric_triangle",
            "ascending_triangle", "descending_triangle", "ascending_channel",
            "descending_channel", "horizontal_channel", "lta", "ltb",
            "head_and_shoulders", "inverse_head_and_shoulders", "double_top",
            "double_bottom", "cup_and_handle", "bull_flag", "bear_flag")
FORMULAS = ("SCORE_V2", "LEGACY_V1")
STATUSES = ("OBSERVED", "NOT_OBSERVED", "NOT_APPLIED", "UNAVAILABLE", "BLOCKED")
STAGES = {
    "raw_score": ("value", "conf_input", "adx_input", "funding_input", "conf_component",
                  "adx_component", "der_component", "mtf_component", "rr_component",
                  "win_bonus", "breakout_bonus", "numerator", "denominator"),
    "base_score": ("value", "input_score", "relevance_multiplier"),
    "htf_score": ("value", "input_score", "bonus", "configured_bonus"),
    "selection_score": ("value", "input_score", "penalty", "configured_penalty"),
    "learning_score": ("value", "input_score", "multiplier", "matched_count", "cap",
                       "min_sample_adjust", "min_sample_block", "block_wr_max",
                       "boost_wr_min", "adjust_cap_pct"),
    "final_score": ("value",),
    "execution_score": ("value", "input_score", "delta", "cap", "score_min"),
}
STAGE_FLAGS = ("enabled", "blocked", "auto_adjust", "auto_block")
CONFIG_NUMBERS = ("v2_w_conf", "v2_w_adx", "v2_w_der", "legacy_w_conf", "legacy_w_mtf",
                  "legacy_w_rr", "legacy_w_der", "legacy_w_win", "tier_aplus", "tier_a",
                  "tier_b", "high_tf_confirm_bonus")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _enum(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _map(source, keys):
    source = source if isinstance(source, dict) else {}
    return {k: _number(source.get(k)) for k in keys}


def _confluence(source):
    source = source if isinstance(source, dict) else {}
    config = source.get("config") or {}
    config = config if isinstance(config, dict) else {}
    factors = source.get("factors")
    factors = factors if isinstance(factors, list) else []
    result = {
        "status": _enum(source.get("status"), STATUSES) or "NOT_OBSERVED",
        **_map(source, ("total", "max_total", "pct", "factor_count")),
        "factors_truncated": len(factors) > MAX_FACTORS or source.get("factors_truncated") is True,
        "factors": [],
        "config": {"weights": _map(config.get("weights"), CATEGORIES),
                   "pattern_empirical": _map(config.get("pattern_empirical"), PATTERNS),
                   "pattern_calibration": _map(config.get("pattern_calibration"), PATTERNS),
                   "pattern_override": _map(config.get("pattern_override"), PATTERNS)},
    }
    for i, factor in enumerate(factors[:MAX_FACTORS]):
        if not isinstance(factor, dict):
            continue
        result["factors"].append({"index": i,
            "category": _enum(factor.get("category"), CATEGORIES),
            "points": _number(factor.get("points")),
            "max_points": _number(factor.get("max_points")),
            "aligned": factor.get("aligned") if isinstance(factor.get("aligned"), bool) else None})
    return result


def capture_confluence(result, *, weights, pattern_empirical, pattern_calibration, pattern_override):
    """Captura no retorno do cálculo, sem textos livres, dados futuros ou IO."""
    try:
        return _confluence({"status": "OBSERVED", "total": result.total,
            "max_total": result.max_total, "pct": result.pct,
            "factor_count": len(result.factors),
            "factors": [{"category": f.category, "points": f.points,
                         "max_points": f.max_points, "aligned": f.aligned}
                        for f in result.factors[:MAX_FACTORS]],
            "factors_truncated": len(result.factors) > MAX_FACTORS,
            "config": {"weights": weights, "pattern_empirical": pattern_empirical,
                       "pattern_calibration": pattern_calibration, "pattern_override": pattern_override}})
    except Exception:
        return _confluence({"status": "UNAVAILABLE"})


def freeze_trace(source):
    """Allowlist recursiva e limite duro; não copia payloads/outcomes arbitrários."""
    try:
        source = source if isinstance(source, dict) and source.get("version") == VERSION else {}
        config = source.get("config")
        config = config if isinstance(config, dict) else {}
        result = {"schema_version": 1, "version": VERSION, "mode": "ANALYTICS_ONLY",
                  "formula_requested": _enum(source.get("formula_requested"), FORMULAS),
                  "formula_effective": _enum(source.get("formula_effective"), FORMULAS),
                  "fallback_used": source.get("fallback_used") if isinstance(source.get("fallback_used"), bool) else None,
                  "fallback_reason": _enum(source.get("fallback_reason"), ("V2_NO_COMPONENTS",)),
                  "config": {**_map(config, CONFIG_NUMBERS),
                     "high_tf_patterns_enabled": config.get("high_tf_patterns_enabled") if isinstance(config.get("high_tf_patterns_enabled"), bool) else None,
                     "high_tf_confirm_enabled": config.get("high_tf_confirm_enabled") if isinstance(config.get("high_tf_confirm_enabled"), bool) else None},
                  "confluence": _confluence(source.get("confluence")), "stages": {}}
        stages = source.get("stages")
        stages = stages if isinstance(stages, dict) else {}
        for name, fields in STAGES.items():
            stage = stages.get(name)
            stage = stage if isinstance(stage, dict) else {}
            values = _map(stage, fields)
            result["stages"][name] = {
                "status": _enum(stage.get("status"), STATUSES) or "NOT_OBSERVED",
                **values, "missing_fields": [key for key, value in values.items() if value is None],
                **{key: stage.get(key) if isinstance(stage.get(key), bool) else None for key in STAGE_FLAGS},
                "tier": _enum(stage.get("tier"), ("A+", "A", "B")),
            }
        if len(json.dumps(result, allow_nan=False, separators=(",", ":")).encode()) > MAX_BYTES:
            return {"schema_version": 1, "version": VERSION, "mode": "ANALYTICS_ONLY",
                    "status": "UNAVAILABLE", "reason": "SIZE_LIMIT"}
        return result
    except Exception:
        return {"schema_version": 1, "version": VERSION, "mode": "ANALYTICS_ONLY",
                "status": "UNAVAILABLE", "reason": "ANNOTATION_FAILED"}


def new_trace(sig, config):
    try:
        return freeze_trace({"version": VERSION, "config": config,
                             "confluence": getattr(getattr(sig, "confluence", None), "r08_capture", None)})
    except Exception:
        return {"schema_version": 1, "version": VERSION, "mode": "ANALYTICS_ONLY",
                "status": "UNAVAILABLE", "reason": "ANNOTATION_FAILED"}


def clear_signal_trace(sig):
    """Um cálculo novo nunca herda trace antigo se a nova anotação falhar."""
    try:
        setattr(sig, KEY, None)
    except Exception:
        pass


def record_stage(trace, stage, **observed):
    """Muta somente o buffer de telemetria privado; falha nunca chega ao scorer."""
    try:
        if isinstance(trace, dict) and stage in STAGES:
            trace.setdefault("stages", {})[stage] = {"status": "OBSERVED", **observed}
    except Exception:
        pass


def record_signal_stage(sig, stage, **observed):
    try:
        trace = freeze_trace(getattr(sig, KEY, None))
        record_stage(trace, stage, **observed)
        setattr(sig, KEY, freeze_trace(trace))
    except Exception:
        pass


def finish_trace(sig, trace, provenance):
    try:
        trace.update({key: provenance.get(key) for key in
                      ("formula_requested", "formula_effective", "fallback_used", "fallback_reason")})
        setattr(sig, KEY, freeze_trace(trace))
    except Exception:
        pass


def recommendation_trace(sig, score, tier):
    try:
        trace = freeze_trace(getattr(sig, KEY, None))
        record_stage(trace, "final_score", value=score, tier=tier)
        return freeze_trace(trace)
    except Exception:
        return {"schema_version": 1, "version": VERSION, "mode": "ANALYTICS_ONLY",
                "status": "UNAVAILABLE", "reason": "ANNOTATION_FAILED"}


def append_execution_score(trace, *, recommendation_score, execution_score,
                           delta=None, enabled=None, cap=None, score_min=None):
    """Integração executor/R09: recebe SOMENTE valores já calculados, sem IO."""
    result = freeze_trace(trace)
    record_stage(result, "execution_score", value=execution_score,
                 input_score=recommendation_score, delta=delta, enabled=enabled,
                 cap=cap, score_min=score_min)
    return freeze_trace(result)


def snapshot_annotation(rec):
    """Somente registros novos; não tenta reconstruir trace ausente."""
    try:
        trace = rec.get(KEY)
        return {KEY: freeze_trace(trace)} if isinstance(trace, dict) else {}
    except Exception:
        return {}
