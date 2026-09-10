"""R07A — playbooks por regime: contratos e comparação offline.

ANALYTICS_ONLY, non-promotable, sem ligação com o executor.

O que este módulo faz:
  1. **Contexto prospectivo** — adaptador puro que monta `features["r07_context"]`
     no momento em que um snapshot NOVO é criado, com allowlist estrita.
  2. **Classificador** — lê só campos pré-outcome e devolve dimensões
     (macro / tendência / estrutura), evidências, campos ausentes e motivos.
  3. **Catálogo descritivo** — o que cada cenário exigiria para ser reproduzido.
     Catálogo informativo NÃO é regra executável.
  4. **Três hipóteses congeladas** de abstenção, comparadas offline sobre
     treino e validação, reutilizando as primitivas do P05.

O que este módulo NÃO faz: gerar sinal, alterar seleção, tocar no executor,
abrir o holdout, criar experimento, promover nada ou escrever no banco.

Sem dependência de rede, exchange ou do serviço de métricas em runtime — as
únicas importações pesadas são locais às funções de avaliação, para manter o
adaptador de contexto barato no caminho de save.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

R07_PHASE = "R07A"
R07_MODE = "ANALYTICS_ONLY"
R07_CONTEXT_KEY = "r07_context"
R07_CONTEXT_SCHEMA_VERSION = 1
R07_CONTEXT_SOURCE = "recommendation_save"

# ── Estados do resultado (namespace R07) ────────────────────────────────────
R07_UNAVAILABLE = "UNAVAILABLE"
R07_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
R07_NO_INCREMENTAL_CHANGE = "NO_INCREMENTAL_CHANGE"
R07_NOT_SUPPORTED = "NOT_SUPPORTED"
R07_VALIDATION_SUPPORTED = "VALIDATION_SUPPORTED"
R07_STATUSES = frozenset({
    R07_UNAVAILABLE, R07_INSUFFICIENT, R07_NO_INCREMENTAL_CHANGE,
    R07_NOT_SUPPORTED, R07_VALIDATION_SUPPORTED,
})

# ── Vocabulário das dimensões ───────────────────────────────────────────────
DIM_UNKNOWN = "UNKNOWN"
DIM_UNCLASSIFIED = "UNCLASSIFIED"

MACRO_UNRESTRICTED = "MACRO_UNRESTRICTED"
MACRO_BLOCK_ALL = "MACRO_BLOCK_ALL"
MACRO_ALT_LONGS_BLOCKED = "MACRO_ALT_LONGS_BLOCKED"
MACRO_ALT_LONGS_DOWNGRADED = "MACRO_ALT_LONGS_DOWNGRADED"
MACRO_SHORTS_BLOCKED = "MACRO_SHORTS_BLOCKED"
MACRO_SHORTS_DOWNGRADED = "MACRO_SHORTS_DOWNGRADED"

TREND_BULLISH = "TREND_UNEQUIVOCAL_BULLISH"
TREND_BEARISH = "TREND_UNEQUIVOCAL_BEARISH"
TREND_MIXED = "TREND_MIXED"
TREND_UNCERTAIN = "TREND_UNCERTAIN"

STRUCT_HORIZONTAL_CHANNEL = "HORIZONTAL_CHANNEL_DETECTED"
STRUCT_BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
STRUCT_RETEST_ACTIVE = "RETEST_ACTIVE"

# ── Motivos controlados ─────────────────────────────────────────────────────
REASON_OK = "OK"
REASON_NO_CONTEXT = "NO_R07_CONTEXT"
REASON_LEGACY_CONTEXT = "LEGACY_SNAPSHOT"
REASON_MALFORMED = "CONTEXT_MALFORMED"
REASON_SCHEMA_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
REASON_MACRO_NOT_OBSERVED = "MACRO_NOT_OBSERVED"
REASON_MACRO_STALE_OBSERVATION = "MACRO_OBSERVED_AFTER_CAPTURE"
REASON_NO_HIGHER_TFS = "NO_HIGHER_TFS"
REASON_TF_CONFLICT = "TIMEFRAME_CONFLICT"
REASON_DIRECTION_UNKNOWN = "DIRECTION_UNKNOWN"
REASON_NO_PATTERNS = "NO_PATTERNS"
REASON_CT_CONFIG_MISSING = "CT_BRAKE_CONFIG_MISSING"
REASON_MAJOR_UNKNOWN = "MAJOR_POLICY_UNKNOWN"
REASON_TIME_INVALID = "CONTEXT_TIME_UNPROVEN"
REASON_TF_TIME_INVALID = "TIMEFRAME_EVIDENCE_UNPROVEN"
REASON_MACRO_FLAGS_UNKNOWN = "MACRO_FLAGS_UNPROVEN"
REASON_CONTEXT_MISMATCH = "CONTEXT_ROW_MISMATCH"
R07_REASON_CODES = frozenset({
    REASON_OK, REASON_NO_CONTEXT, REASON_LEGACY_CONTEXT, REASON_MALFORMED,
    REASON_SCHEMA_UNSUPPORTED, REASON_MACRO_NOT_OBSERVED,
    REASON_MACRO_STALE_OBSERVATION, REASON_NO_HIGHER_TFS, REASON_TF_CONFLICT,
    REASON_DIRECTION_UNKNOWN, REASON_NO_PATTERNS, REASON_CT_CONFIG_MISSING,
    REASON_MAJOR_UNKNOWN,
    REASON_TIME_INVALID, REASON_TF_TIME_INVALID, REASON_MACRO_FLAGS_UNKNOWN,
    REASON_CONTEXT_MISMATCH,
})

#: Qualidades do regime macro que NÃO sustentam uma leitura de contexto. Um
#: `regime == "NORMAL"` acompanhado de `DISABLED`/`UNKNOWN` significa "não sei",
#: não "mercado tranquilo" — e nunca pode virar contexto válido.
# Contrato positivo: DEGRADED pode esconder a falha do dado que governa o
# downgrade. Sem prova por componente, não há como transformá-lo em False.
MACRO_USABLE_QUALITY = frozenset({"FRESH"})

R07_LIMITATIONS = [
    "os snapshots são POSTERIORES a filtros e seleção: não representam todos os "
    "sinais vetados nem as alternativas que o bot poderia ter escolhido",
    "o contexto é gravado no SAVE — não é necessariamente o contexto que o "
    "scanner ou o executor usaram na decisão",
    "baseline é uma RECONSTRUÇÃO de seleção sobre setups SHADOW, com a config "
    "atual congelada; não reproduz integralmente as ordens históricas",
    "R de setup não é lucro líquido financeiro; REAL nunca é somado ao SHADOW",
    "abster-se de um contexto adverso também remove os wins daquele contexto",
    "hipóteses são exploratórias e múltiplas — não há declaração de vencedor, "
    "de causalidade nem de aprovação estatística final",
    "histórico anterior ao R07A não tem r07_context e não é reconstruído",
]


# ════════════════════════════════════════════════════════════════════════════
#  Utilidades puras
# ════════════════════════════════════════════════════════════════════════════
def _num(value) -> Optional[float]:
    """Número real e finito, ou None. `bool` nunca é número aqui."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, numbers.Real):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _flag(value) -> Optional[bool]:
    """Somente `True`/`False` reais. `1`, `"true"` e `None` não são flags."""
    return value if isinstance(value, bool) else None


def _text(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _side(value) -> Optional[str]:
    v = _text(value)
    v = v.lower() if v else None
    return v if v in ("long", "short") else None


def _iso_to_ms(value: Optional[str]) -> Optional[float]:
    """ISO-8601 → epoch em ms. Formato inválido vira None, nunca 0."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        result = dt.timestamp() * 1000.0
    except (ValueError, OverflowError, OSError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _utc_time(value: Any) -> Optional[datetime]:
    """Instante explícito; legado naive é UTC, nunca o fuso da máquina."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime):
        return None
    try:
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt if _iso_to_ms(dt.isoformat()) is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _candle_evidence(freshness: Any) -> Optional[Dict[str, Any]]:
    """Preserva só a prova temporal já fornecida pelo produtor do TF."""
    candle = freshness.get("candle") if isinstance(freshness, dict) else None
    if not isinstance(candle, dict):
        return None
    return {"candle": {
        "quality": _text(candle.get("quality")),
        "source": _text(candle.get("source")),
        "timeframe": _text(candle.get("timeframe")),
        "close_time_ms": _num(candle.get("close_time_ms")),
        "observed_at_ms": _num(candle.get("observed_at_ms")),
    }}


def _dedupe_reasons(reasons: Sequence[str]) -> List[str]:
    saida: List[str] = []
    for r in reasons:
        if r in R07_REASON_CODES and r not in saida:
            saida.append(r)
    return saida


# ════════════════════════════════════════════════════════════════════════════
#  1. CONTRATO DE CONTEXTO PROSPECTIVO
# ════════════════════════════════════════════════════════════════════════════
def build_r07_context(rec: Dict[str, Any], sig: Dict[str, Any],
                      regime_status: Optional[Dict[str, Any]],
                      *, captured_at: datetime,
                      is_major: Optional[bool] = None,
                      ct_brake: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Adaptador PURO do contexto R07. Allowlist estrita, sem I/O.

    Não guarda o objeto de sinal inteiro, não inventa timestamp nem qualidade,
    e nunca inclui `outcome`, `realized_r` ou `p05_path`. `regime_status` é o
    MESMO payload que o batch de save já consultou uma vez — nenhuma segunda
    chamada é feita aqui.

    Três instantes ficam separados de propósito: `captured_at` (quando a
    anotação foi montada), `macro.observed_at_ms` (quando a fonte macro foi
    observada) e o `created_at` da linha, que não é tocado.
    """
    rec = rec if isinstance(rec, dict) else {}
    sig = sig if isinstance(sig, dict) else {}
    rg = regime_status if isinstance(regime_status, dict) else {}
    ct = ct_brake if isinstance(ct_brake, dict) else {}

    macro = {
        "regime": _text(rg.get("regime")),
        "block_all": _flag(rg.get("block_all")),
        "block_alt_longs": _flag(rg.get("block_alt_longs")),
        "downgrade_alt_longs": _flag(rg.get("downgrade_alt_longs")),
        "block_shorts": _flag(rg.get("block_shorts")),
        "downgrade_shorts": _flag(rg.get("downgrade_shorts")),
        "filter_enabled": _flag(rg.get("filter_enabled")),
        "quality": _text(rg.get("quality")),
        "observed_at_ms": _num(rg.get("observed_at_ms")),
    }

    mtf = sig.get("mtf") if isinstance(sig.get("mtf"), dict) else {}
    higher: List[Dict[str, Any]] = []
    for h in (mtf.get("higher_tfs") or []):
        if not isinstance(h, dict):
            continue
        higher.append({
            "timeframe": _text(h.get("timeframe")),
            "ema_aligned": _text(h.get("ema_aligned")),
            "data_freshness": _candle_evidence(h.get("data_freshness")),
        })

    padroes: List[Dict[str, Any]] = []
    for p in (sig.get("patterns") or []):
        if not isinstance(p, dict):
            continue
        padroes.append({
            "type": _text(p.get("type")),
            "direction": _side(p.get("direction")),
            # ausência NÃO vira False: `None` significa "não registrado"
            "breakout_confirmed": _flag(p.get("breakout_confirmed")),
            "retest_active": _flag(p.get("retest_active")),
        })

    freshness = rec.get("data_freshness") or sig.get("data_freshness")
    fresh = None
    if isinstance(freshness, dict):
        candle = freshness.get("candle") if isinstance(freshness.get("candle"), dict) else {}
        fresh = {
            "quality": _text(candle.get("quality")) or _text(freshness.get("quality")),
            "source": _text(candle.get("source")) or _text(freshness.get("source")),
        }

    return {
        "schema_version": R07_CONTEXT_SCHEMA_VERSION,
        "source": R07_CONTEXT_SOURCE,
        "captured_at": (_utc_time(captured_at).isoformat()
                        if _utc_time(captured_at) is not None else None),
        "direction": _side(rec.get("direction")) or _side(sig.get("direction")),
        # Política de majors VIGENTE (`regime_service.is_btc_symbol`), resolvida
        # pelo chamador. Não recriamos uma lista paralela aqui.
        "is_major": _flag(is_major),
        "macro": macro,
        # Config necessária para INTERPRETAR o freio contratendência depois.
        # Chega pelo argumento (o adaptador continua puro); o chamador usa
        # `ct_brake_config()`, que lê o serviço de regime.
        "ct_brake": {
            "enabled": _flag((ct or {}).get("enabled")),
            "min_tfs": _num((ct or {}).get("min_tfs")),
            "block": _flag((ct or {}).get("block")),
            "select_penalty": _num((ct or {}).get("select_penalty")),
        },
        "higher_tfs": higher,
        "patterns": padroes,
        "entry_zone_type": _text(rec.get("entry_zone_type")),
        "retest_armed": _flag(rec.get("retest_armed")),
        "signal_timestamp_ms": _num(sig.get("timestamp")),
        "data_freshness": fresh,
    }


def ct_brake_config() -> Dict[str, Any]:
    """Config do freio contratendência, lida do serviço de regime.

    Vive aqui para que o adaptador receba tudo pelo argumento e continue puro.
    """
    from services import regime_service as rg
    return {
        "enabled": bool(rg.CT_BRAKE_ENABLED),
        "min_tfs": int(rg.CT_BRAKE_MIN_TFS),
        "block": bool(rg.CT_BRAKE_BLOCK),
        "select_penalty": float(rg.CT_BRAKE_SELECT_PENALTY),
    }


# ════════════════════════════════════════════════════════════════════════════
#  2. CLASSIFICADOR
# ════════════════════════════════════════════════════════════════════════════
def classify_context(context: Any, *, evaluated_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Dimensões do cenário a partir do contexto PRÉ-outcome.

    Cenários se sobrepõem: as três dimensões são independentes e nenhuma é
    forçada a um rótulo único. Dado ausente ou inválido produz `UNKNOWN` /
    `UNCLASSIFIED` com motivo — nunca "alinhamento favorável" por omissão.
    """
    faltantes: List[str] = []
    motivos: List[str] = []

    if context is None:
        return _classificacao_vazia(REASON_NO_CONTEXT, legacy=True)
    if not isinstance(context, dict):
        return _classificacao_vazia(REASON_MALFORMED)
    if (type(context.get("schema_version")) is not int
            or context.get("schema_version") != R07_CONTEXT_SCHEMA_VERSION):
        return _classificacao_vazia(REASON_SCHEMA_UNSUPPORTED)

    captured_ms = _iso_to_ms(context.get("captured_at"))
    evaluation = _utc_time(evaluated_at) if evaluated_at is not None else None
    evaluated_ms = (_iso_to_ms(evaluation.isoformat()) if evaluation is not None
                    else captured_ms if evaluated_at is None else None)
    if captured_ms is None or evaluated_ms is None or captured_ms > evaluated_ms:
        return _classificacao_vazia(REASON_TIME_INVALID)
    # A prova deve existir tanto no save quanto no instante avaliado. Jamais
    # aceitar uma observação futura porque o contexto foi capturado mais tarde.
    cutoff_ms = min(captured_ms, evaluated_ms)
    signal_ms = _num(context.get("signal_timestamp_ms"))
    if (context.get("signal_timestamp_ms") is not None
            and (signal_ms is None or not (0 < signal_ms <= cutoff_ms))):
        return _classificacao_vazia(REASON_TIME_INVALID)

    direction = _side(context.get("direction"))
    if direction is None:
        faltantes.append("direction")
        motivos.append(REASON_DIRECTION_UNKNOWN)

    macro_dims, macro_ev, macro_falta, macro_motivos = _classify_macro(
        context.get("macro"), direction, _flag(context.get("is_major")),
        captured_at=_text(context.get("captured_at")))
    trend_dim, trend_ev, trend_falta, trend_motivos = _classify_trend(
        context.get("higher_tfs"), context.get("ct_brake"), cutoff_ms=cutoff_ms)
    struct_dims, struct_ev, struct_falta, struct_motivos = _classify_structure(
        context.get("patterns"), context.get("entry_zone_type"), direction)

    faltantes += macro_falta + trend_falta + struct_falta
    motivos += macro_motivos + trend_motivos + struct_motivos

    return {
        "schema_version": R07_CONTEXT_SCHEMA_VERSION,
        "legacy": False,
        "direction": direction,
        "macro": macro_dims,
        "trend": trend_dim,
        "structure": struct_dims,
        "evidence": {"macro": macro_ev, "trend": trend_ev, "structure": struct_ev},
        "missing": sorted(set(faltantes)),
        "reasons": _dedupe_reasons(motivos) or [REASON_OK],
    }


def _classificacao_vazia(reason: str, *, legacy: bool = False) -> Dict[str, Any]:
    return {
        "schema_version": None,
        "legacy": bool(legacy),
        "direction": None,
        "macro": [DIM_UNKNOWN],
        "trend": DIM_UNKNOWN,
        "structure": [DIM_UNCLASSIFIED],
        "evidence": {"macro": {}, "trend": {}, "structure": {}},
        "missing": ["r07_context"],
        "reasons": [reason],
    }


def _classify_macro(macro: Any, direction: Optional[str],
                    is_major: Optional[bool], *,
                    captured_at: Optional[str] = None,
                    ) -> Tuple[List[str], Dict[str, Any], List[str], List[str]]:
    if not isinstance(macro, dict):
        return [DIM_UNKNOWN], {}, ["macro"], [REASON_MALFORMED]

    quality = _text(macro.get("quality"))
    filter_enabled = _flag(macro.get("filter_enabled"))
    observed_ms = _num(macro.get("observed_at_ms"))
    ev: Dict[str, Any] = {
        "regime": _text(macro.get("regime")),
        "quality": quality,
        "filter_enabled": filter_enabled,
        "observed_at_ms": observed_ms,
        "captured_at": captured_at,
    }

    # NORMAL com filtro desligado ou qualidade desconhecida é "não sei".
    if filter_enabled is not True or quality not in MACRO_USABLE_QUALITY:
        return [DIM_UNKNOWN], ev, ["macro.quality"], [REASON_MACRO_NOT_OBSERVED]

    # Observação POSTERIOR ao instante avaliado não podia ter informado aquela
    # decisão. Não se retrodata a observação nem se mexe no created_at: a
    # dimensão macro simplesmente não é utilizável nesta linha.
    capt_ms = _iso_to_ms(captured_at)
    if observed_ms is None or observed_ms <= 0 or capt_ms is None:
        return [DIM_UNKNOWN], ev, ["macro.observed_at_ms"], [REASON_MACRO_NOT_OBSERVED]
    if observed_ms > capt_ms:
        ev["observation_after_capture"] = True
        return [DIM_UNKNOWN], ev, ["macro.observed_at_ms"], [REASON_MACRO_STALE_OBSERVATION]

    flags = ("block_all", "block_alt_longs", "downgrade_alt_longs",
             "block_shorts", "downgrade_shorts")
    missing_flags = [name for name in flags if _flag(macro.get(name)) is None]
    if missing_flags:
        return [DIM_UNKNOWN], ev, [f"macro.{name}" for name in missing_flags], [REASON_MACRO_FLAGS_UNKNOWN]

    dims: List[str] = []
    if _flag(macro.get("block_all")) is True:
        dims.append(MACRO_BLOCK_ALL)
    if _flag(macro.get("block_alt_longs")) is True:
        dims.append(MACRO_ALT_LONGS_BLOCKED)
    elif _flag(macro.get("downgrade_alt_longs")) is True:
        dims.append(MACRO_ALT_LONGS_DOWNGRADED)
    if _flag(macro.get("block_shorts")) is True:
        dims.append(MACRO_SHORTS_BLOCKED)
    elif _flag(macro.get("downgrade_shorts")) is True:
        dims.append(MACRO_SHORTS_DOWNGRADED)

    falta: List[str] = []
    motivos: List[str] = []
    # A política de majors só importa para as dimensões de alt long.
    if is_major is None and any(d in dims for d in
                                (MACRO_ALT_LONGS_BLOCKED, MACRO_ALT_LONGS_DOWNGRADED)):
        falta.append("is_major")
        motivos.append(REASON_MAJOR_UNKNOWN)
    ev["is_major"] = is_major
    ev["direction"] = direction
    return (dims or [MACRO_UNRESTRICTED]), ev, falta, motivos


def _classify_trend(higher_tfs: Any, ct: Any, *, cutoff_ms: float) -> Tuple[str, Dict[str, Any],
                                                       List[str], List[str]]:
    """Tendência EMA dos TFs superiores, com a semântica REAL do freio.

    `mtf.aligned_count` mede concordância com a DIREÇÃO DO SINAL e não entra
    aqui: a tendência vem de `higher_tfs[].ema_aligned`. TF duplicado não conta
    duas vezes; conflito no mesmo TF torna a classificação incerta.
    """
    if not isinstance(ct, dict):
        return DIM_UNKNOWN, {}, ["ct_brake"], [REASON_CT_CONFIG_MISSING]
    min_tfs = _num(ct.get("min_tfs"))
    if min_tfs is None or min_tfs < 1 or not min_tfs.is_integer():
        return DIM_UNKNOWN, {"ct_brake": ct}, ["ct_brake.min_tfs"], [REASON_CT_CONFIG_MISSING]

    if not isinstance(higher_tfs, list) or not higher_tfs:
        return (DIM_UNKNOWN, {"higher_tfs": 0}, ["higher_tfs"], [REASON_NO_HIGHER_TFS])

    por_tf: Dict[str, set] = {}
    sem_tf = 0
    for h in higher_tfs:
        if not isinstance(h, dict):
            return DIM_UNKNOWN, {}, ["higher_tfs"], [REASON_TF_TIME_INVALID]
        tf = _text(h.get("timeframe"))
        alinhado = _text(h.get("ema_aligned"))
        if tf is None:
            return DIM_UNKNOWN, {}, ["higher_tfs.timeframe"], [REASON_NO_HIGHER_TFS]
        proof = _candle_evidence(h.get("data_freshness"))
        candle = (proof or {}).get("candle", {})
        closed = _num(candle.get("close_time_ms"))
        observed = _num(candle.get("observed_at_ms"))
        if (candle.get("quality") != "FRESH" or not candle.get("source")
                or candle.get("timeframe") not in (None, tf)
                or closed is None or observed is None
                or not (0 < closed <= observed <= cutoff_ms)):
            return DIM_UNKNOWN, {}, [f"higher_tfs.{tf}.data_freshness"], [REASON_TF_TIME_INVALID]
        if alinhado not in ("bullish", "bearish", "mixed"):
            return DIM_UNKNOWN, {}, [f"higher_tfs.{tf}.ema_aligned"], [REASON_NO_HIGHER_TFS]
        por_tf.setdefault(tf, set()).add(alinhado)

    conflito = [tf for tf, vals in por_tf.items() if len(vals) > 1]
    ev = {
        "timeframes": sorted(por_tf),
        "distinct_timeframes": len(por_tf),
        "conflicting_timeframes": sorted(conflito),
        "entries_without_timeframe": sem_tf,
        "ct_brake_min_tfs": int(min_tfs),
        "ct_brake_enabled": _flag(ct.get("enabled")),
        "ct_brake_block": _flag(ct.get("block")),
    }
    if conflito:
        return TREND_UNCERTAIN, ev, [], [REASON_TF_CONFLICT]
    if not por_tf:
        return DIM_UNKNOWN, ev, ["higher_tfs.timeframe"], [REASON_NO_HIGHER_TFS]

    bull = sum(1 for vals in por_tf.values() if "bullish" in vals)
    bear = sum(1 for vals in por_tf.values() if "bearish" in vals)
    ev["bullish_timeframes"] = bull
    ev["bearish_timeframes"] = bear

    # Inequívoca = N TFs distintos na mesma direção e NENHUM contra. É a mesma
    # regra de `regime_service.symbol_counter_trend`, com a config congelada.
    if bull >= min_tfs and bear == 0:
        return TREND_BULLISH, ev, [], []
    if bear >= min_tfs and bull == 0:
        return TREND_BEARISH, ev, [], []
    return TREND_MIXED, ev, [], []


def _classify_structure(patterns: Any, entry_zone_type: Any,
                        direction: Optional[str]) -> Tuple[List[str], Dict[str, Any],
                                                           List[str], List[str]]:
    """Estrutura do setup — só o que está EXPLICITAMENTE registrado.

    Nome de padrão não prova rompimento; `retest_active=None` não prova ausência
    de retest; limites de range, entrada na borda, volume e confirmação não são
    inventados.
    """
    if not isinstance(patterns, list):
        return [DIM_UNCLASSIFIED], {}, ["patterns"], [REASON_NO_PATTERNS]

    dims: List[str] = []
    ev: Dict[str, Any] = {
        "patterns": len(patterns),
        "entry_zone_type": _text(entry_zone_type),
        "breakout_flag_present": False,
        "retest_flag_present": False,
        "aligned_breakout": False,
    }
    motivos: List[str] = []
    falta: List[str] = []

    for p in patterns:
        if not isinstance(p, dict):
            continue
        tipo = _text(p.get("type"))
        lado = _side(p.get("direction"))
        confirmado = _flag(p.get("breakout_confirmed"))
        retest = _flag(p.get("retest_active"))

        if tipo == "horizontal_channel" and STRUCT_HORIZONTAL_CHANNEL not in dims:
            # Só isto: um canal horizontal DETECTADO. Não afirma range intacto,
            # nem entrada na borda, nem que o range vai respeitar.
            dims.append(STRUCT_HORIZONTAL_CHANNEL)
        if confirmado is not None:
            ev["breakout_flag_present"] = True
        if retest is not None:
            ev["retest_flag_present"] = True
        if confirmado is True:
            if STRUCT_BREAKOUT_CONFIRMED not in dims:
                dims.append(STRUCT_BREAKOUT_CONFIRMED)
            # A leitura direcional exige saber os dois lados.
            if lado is None or direction is None:
                falta.append("patterns.direction")
                motivos.append(REASON_DIRECTION_UNKNOWN)
            elif lado == direction:
                ev["aligned_breakout"] = True
        if retest is True and STRUCT_RETEST_ACTIVE not in dims:
            dims.append(STRUCT_RETEST_ACTIVE)

    if not patterns:
        motivos.append(REASON_NO_PATTERNS)
    return (dims or [DIM_UNCLASSIFIED]), ev, falta, motivos


# ════════════════════════════════════════════════════════════════════════════
#  3. CATÁLOGO DESCRITIVO
# ════════════════════════════════════════════════════════════════════════════
def scenario_catalog() -> List[Dict[str, Any]]:
    """Catálogo INFORMATIVO. Nenhum item aqui é regra executável."""
    return [
        {
            "id": "trend_alignment",
            "dimension": "trend",
            "label": "Tendência / alinhamento EMA dos TFs superiores",
            "requires": ["r07_context.higher_tfs[].ema_aligned",
                         "r07_context.ct_brake.min_tfs"],
            "missing_without_it": ("sem os TFs superiores gravados não há como "
                                   "afirmar tendência do próprio ativo"),
            "not_proven_by": ["mtf.aligned_count (mede direção do SINAL, não a EMA)"],
        },
        {
            "id": "horizontal_range",
            "dimension": "structure",
            "label": "Canal horizontal / range",
            "requires": ["patterns[].type == horizontal_channel"],
            "missing_without_it": ("limites do range, entrada na borda e volume "
                                   "não são persistidos — o rótulo só diz que um "
                                   "canal foi DETECTADO"),
            "not_proven_by": ["nome do padrão isolado"],
        },
        {
            "id": "breakout_retest",
            "dimension": "structure",
            "label": "Rompimento confirmado / retest",
            "requires": ["patterns[].breakout_confirmed == true",
                         "patterns[].retest_active == true",
                         "patterns[].direction"],
            "missing_without_it": ("retest_armed=None não comprova ausência de "
                                   "retest; ausência de flag é desconhecido"),
            "not_proven_by": ["entry_zone_type isolado"],
        },
        {
            "id": "macro_restriction",
            "dimension": "macro",
            "label": "Restrição macro vigente",
            "requires": ["r07_context.macro.quality utilizável",
                         "r07_context.macro.filter_enabled == true",
                         "flags block_/downgrade_"],
            "missing_without_it": ("NORMAL com filtro DISABLED ou qualidade "
                                   "UNKNOWN é ausência de leitura, não mercado calmo"),
            "not_proven_by": ["regime == NORMAL isolado"],
        },
    ]


# ════════════════════════════════════════════════════════════════════════════
#  4. TRÊS HIPÓTESES CONGELADAS
# ════════════════════════════════════════════════════════════════════════════
VETO = "VETO"
KEEP = "KEEP"
UNKNOWN = "UNKNOWN"

R07_HYPOTHESIS_TYPE = "REGIME_ABSTENTION"
R07_HYPOTHESIS_VERSION = 1


def _hypothesis_hash(hyp_id: str, config: Dict[str, Any]) -> str:
    payload = json.dumps(
        {"type": R07_HYPOTHESIS_TYPE, "version": R07_HYPOTHESIS_VERSION,
         "id": hyp_id, "config": config},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _h1(cls: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """H1 — abster-se de entrar contra a tendência EMA INEQUÍVOCA do ativo."""
    direction = cls.get("direction")
    trend = cls.get("trend")
    if direction is None:
        return UNKNOWN, REASON_DIRECTION_UNKNOWN
    if trend in (DIM_UNKNOWN, TREND_UNCERTAIN):
        return UNKNOWN, REASON_TF_CONFLICT if trend == TREND_UNCERTAIN else REASON_NO_HIGHER_TFS
    if trend == TREND_BEARISH and direction == "long":
        return VETO, None
    if trend == TREND_BULLISH and direction == "short":
        return VETO, None
    return KEEP, None


def _h2(cls: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """H2 — abster-se de SHORTS quando `downgrade_shorts=True`."""
    direction = cls.get("direction")
    if direction is None:
        return UNKNOWN, REASON_DIRECTION_UNKNOWN
    if DIM_UNKNOWN in (cls.get("macro") or []):
        return UNKNOWN, REASON_MACRO_NOT_OBSERVED
    if direction != "short":
        return KEEP, None
    if MACRO_SHORTS_DOWNGRADED in (cls.get("macro") or []):
        return VETO, None
    return KEEP, None


def _h3(cls: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """H3 — abster-se de LONGS não-majors quando `downgrade_alt_longs=True`."""
    direction = cls.get("direction")
    if direction is None:
        return UNKNOWN, REASON_DIRECTION_UNKNOWN
    if DIM_UNKNOWN in (cls.get("macro") or []):
        return UNKNOWN, REASON_MACRO_NOT_OBSERVED
    if direction != "long":
        return KEEP, None
    is_major = _flag((cls.get("evidence") or {}).get("macro", {}).get("is_major"))
    if is_major is None:
        return UNKNOWN, REASON_MAJOR_UNKNOWN
    if is_major:
        return KEEP, None
    if MACRO_ALT_LONGS_DOWNGRADED in (cls.get("macro") or []):
        return VETO, None
    return KEEP, None


#: Hipóteses FIXADAS antes de observar qualquer resultado. Nenhuma combina
#: regras, nenhuma é por símbolo/horário/resultado, e nenhuma altera entrada,
#: stop, TP, score ou tamanho. Regras de range/rompimento NÃO são candidatas
#: nesta entrega — o catálogo delas continua descritivo.
R07_HYPOTHESES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "H1_ABSTAIN_COUNTER_TREND",
        "label": "Abster-se de entradas contra a tendência EMA inequívoca do ativo",
        "config": {"source": "higher_tfs[].ema_aligned",
                   "requires_unequivocal": True,
                   "uses_ct_brake_min_tfs": True},
        "decide": _h1,
        "baseline_already_blocks": "ct_brake.block",
    },
    {
        "id": "H2_ABSTAIN_DOWNGRADED_SHORTS",
        "label": "Abster-se de shorts quando downgrade_shorts=True",
        "config": {"source": "macro.downgrade_shorts", "side": "short"},
        "decide": _h2,
        "baseline_already_blocks": "macro.block_shorts",
    },
    {
        "id": "H3_ABSTAIN_DOWNGRADED_ALT_LONGS",
        "label": "Abster-se de longs não-majors quando downgrade_alt_longs=True",
        "config": {"source": "macro.downgrade_alt_longs", "side": "long",
                   "majors_excluded": True},
        "decide": _h3,
        "baseline_already_blocks": "macro.block_alt_longs",
    },
)


def hypothesis_hashes() -> Dict[str, str]:
    """Hash determinístico por hipótese — muda só se a config mudar."""
    return {h["id"]: _hypothesis_hash(h["id"], h["config"]) for h in R07_HYPOTHESES}


def decide(hypothesis_id: str, context: Any, *,
           evaluated_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Decisão da regra para UM setup. Só campos pré-outcome entram aqui."""
    hyp = next((h for h in R07_HYPOTHESES if h["id"] == hypothesis_id), None)
    if hyp is None:
        return {"decision": UNKNOWN, "reason_code": REASON_MALFORMED}
    cls = classify_context(context, evaluated_at=evaluated_at)
    if cls.get("legacy") or cls.get("schema_version") is None:
        return {"decision": UNKNOWN,
                "reason_code": (cls.get("reasons") or [REASON_NO_CONTEXT])[0]}
    try:
        decision, reason = hyp["decide"](cls, context if isinstance(context, dict) else {})
    except Exception:
        return {"decision": UNKNOWN, "reason_code": REASON_MALFORMED}
    return {"decision": decision, "reason_code": reason or REASON_OK}


# ════════════════════════════════════════════════════════════════════════════
#  5. COMPARAÇÃO OFFLINE (reutiliza as primitivas do P05)
# ════════════════════════════════════════════════════════════════════════════
def _p05():
    """Import tardio: evita ciclo com `strategy_evidence_service`."""
    from services import strategy_evidence_service as p05
    return p05


def _baseline_components(rows: Sequence[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    """Componentes do baseline com cobertura suficiente NA AMOSTRA.

    Mesma primitiva e mesmo piso do P05 (`component_coverage`,
    `P05_MIN_FEATURE_COVERAGE_PCT`); só não há knob-alvo aqui, porque o R07 não
    testa knob nenhum. Componente sem cobertura fica desligado dos DOIS lados.
    """
    p05 = _p05()
    cobertura = {c: p05.component_coverage(rows, c) for c in p05._COMPONENT_FEATURES}
    ativos = [c for c, cov in cobertura.items()
              if cov is not None and cov >= p05.P05_MIN_FEATURE_COVERAGE_PCT]
    ignorados = {c: cov for c, cov in cobertura.items() if c not in ativos}
    return ativos, {"coverage_pct": cobertura, "skipped_low_coverage": ignorados}


def temporal_purge(train_rows: Sequence[Dict[str, Any]],
                   valid_rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]],
                                                                  Dict[str, Any]]:
    """Mantém na validação só setups CRIADOS depois do último resolvido do treino.

    Sem isso, uma regra "congelada no treino" seria aplicada a setups que já
    existiam enquanto o treino ainda estava se resolvendo. Ausência de timestamp
    impede confirmar a separação, então a linha é purgada — não presumida boa.
    As partições dos consumidores P05 NÃO são alteradas: a purga vive aqui.
    """
    resolvidos = [_utc_time(r.get("resolved_at")) for r in train_rows]
    if not resolvidos or any(t is None for t in resolvidos):
        return [], {
            "applied": False, "cutoff": None, "kept": 0,
            "purged": len(valid_rows), "purged_missing_created_at": 0,
            "reason": "treino sem resolved_at — separação temporal não verificável",
        }
    corte = max(resolvidos)
    mantidos: List[Dict[str, Any]] = []
    purgadas = sem_ts = 0
    for row in valid_rows:
        criado = _utc_time(row.get("created_at"))
        if criado is None:
            sem_ts += 1
            purgadas += 1
            continue
        if criado > corte:
            mantidos.append(row)
        else:
            purgadas += 1
    return mantidos, {
        "applied": True,
        "cutoff": corte.isoformat(),
        "kept": len(mantidos),
        "purged": purgadas,
        "purged_missing_created_at": sem_ts,
        "reason": ("validação restrita a setups criados após o último resolvido "
                   "do treino"),
    }


def r07_rule_comparison(rows: Sequence[Dict[str, Any]], hypothesis_id: str,
                        config: Dict[str, Any],
                        active_components: Sequence[str]) -> Dict[str, Any]:
    """Champion reconstruído × challenger, sobre as MESMAS oportunidades.

    Champion  = setups que o baseline reconstruído operaria (`eligibility` True).
    Challenger = champion AND regra != VETO. `baseline=False` nunca vira True.
    UNKNOWN — do baseline OU da regra — é excluído dos DOIS lados ANTES de
    qualquer consulta a resultado.
    """
    p05 = _p05()
    champion: List[Dict[str, Any]] = []
    excluidos = {"baseline_unknown": 0, "rule_unknown": 0,
                 "baseline_blocked": 0, "metric_unavailable": 0}
    motivos_unknown: Dict[str, int] = {}
    known_rules: List[Dict[str, Any]] = []
    total_rows = baseline_eligible = duplicates = 0
    seen: set = set()

    # Congela memberships sem consultar status/R. Até a qualidade dos outcomes
    # é avaliada só depois; cobertura de decisão não depende de lucro ou perda.
    for row in rows:
        key = row.get("dedupe_key")
        if key is not None and key in seen:
            duplicates += 1
            continue
        if key is not None:
            seen.add(key)
        total_rows += 1
        veredito = p05.eligibility(row, config, active_components)
        if type(veredito) is not bool:
            excluidos["baseline_unknown"] += 1
            continue
        if veredito is False:
            # Recusa conhecida não é falta de dados. False AND UNKNOWN também
            # é False; não precisamos do contexto da regra para abrir operação.
            excluidos["baseline_blocked"] += 1
            continue
        baseline_eligible += 1
        features = row.get("features")
        contexto = features.get(R07_CONTEXT_KEY) if isinstance(features, dict) else None
        created = _utc_time(row.get("created_at"))
        if created is None:
            regra = {"decision": UNKNOWN, "reason_code": REASON_TIME_INVALID}
        elif (isinstance(contexto, dict)
              and (_side(row.get("direction")) is None
                   or _side(contexto.get("direction")) != _side(row.get("direction")))):
            regra = {"decision": UNKNOWN, "reason_code": REASON_CONTEXT_MISMATCH}
        else:
            regra = decide(hypothesis_id, contexto, evaluated_at=created)
        if regra["decision"] == UNKNOWN:
            # Simetria: sai dos dois lados, e sai ANTES de olhar o desfecho.
            excluidos["rule_unknown"] += 1
            code = regra.get("reason_code") or REASON_MALFORMED
            motivos_unknown[code] = motivos_unknown.get(code, 0) + 1
            continue
        known_rules.append({**row, "_r07_decision": regra["decision"]})

    valid, outcome_exclusions = p05._partition_outcomes(known_rules)
    excluidos["metric_unavailable"] = len(known_rules) - len(valid)
    for row in valid:
        metric = p05._lab_metric_row(row)
        if metric is None:
            excluidos["metric_unavailable"] += 1
            continue
        champion.append(metric)

    kept = [r for r in champion if r["_r07_decision"] != VETO]
    removed = [r for r in champion if r["_r07_decision"] == VETO]

    def _rate(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(group)
        stops = sum(1 for r in group if r["outcome_class"] == "loss")
        return {"exposure": n, "stops": stops,
                "stop_rate_pct": round(stops / n * 100, 2) if n else None,
                "stop_rate_ci": p05.wilson_interval(stops, n)}

    champ_m = p05.compute_evidence_metrics(champion)
    cand_m = p05.compute_evidence_metrics(kept)
    rem_m = p05.compute_evidence_metrics(removed)
    kept_ids = {id(r) for r in kept}

    known_decisions = len(known_rules) + excluidos["baseline_blocked"]
    cobertura = round(known_decisions / total_rows * 100, 1) if total_rows else None
    rule_coverage = (round(len(known_rules) / baseline_eligible * 100, 1)
                     if baseline_eligible else None)
    metric_coverage = (round(len(champion) / len(known_rules) * 100, 1)
                       if known_rules else None)
    preservadas = round(len(kept) / len(champion) * 100, 2) if champion else None

    def _bloco(rate: Dict[str, Any], m: Dict[str, Any]) -> Dict[str, Any]:
        return {**rate, "expectancy_r": m["expectancy_r"], "sum_r": m["sum_r"],
                "median_r": m["median_r"], "profit_factor": m["profit_factor"],
                "max_drawdown_r": m["max_drawdown_r"],
                "worst_loss_streak": m["worst_loss_streak"],
                "expectancy_ci": m["expectancy_ci"], "reliability": m["reliability"]}

    return {
        "hypothesis_id": hypothesis_id,
        "universe_valid": total_rows,
        "universe_total": total_rows,
        "duplicates_excluded": duplicates,
        "known_decisions": known_decisions,
        "known_baseline_blocked": excluidos["baseline_blocked"],
        "baseline_eligible": baseline_eligible,
        "evaluable": len(champion),
        "coverage_pct": cobertura,
        "coverage_basis": "known_selection_decisions_before_outcomes",
        "eligible_rule_coverage_pct": rule_coverage,
        "metric_coverage_pct": metric_coverage,
        "outcome_exclusions": outcome_exclusions,
        "excluded": excluidos,
        "unknown_total": excluidos["baseline_unknown"] + excluidos["rule_unknown"],
        "excluded_total": (excluidos["baseline_unknown"] + excluidos["rule_unknown"]
                           + excluidos["metric_unavailable"]),
        "unknown_reasons": motivos_unknown,
        "champion": _bloco(_rate(champion), champ_m),
        "candidate": _bloco(_rate(kept), cand_m),
        "removed": _bloco(_rate(removed), rem_m),
        "operations_removed": len(removed),
        "operations_preserved_pct": preservadas,
        "stops_avoided": sum(1 for r in removed if r["stop_class"] == p05.OUTCOME_STOP),
        "wins_removed": sum(1 for r in removed if r["stop_class"] == p05.OUTCOME_WIN),
        "protected_exits_removed": sum(1 for r in removed
                                       if r["stop_class"] == p05.OUTCOME_PROTECTED_EXIT),
        "paired_expectancy_delta_ci":
            p05.bootstrap_paired_membership_delta_ci(
                champion, {id(r) for r in champion}, kept_ids),
        "stop_rate_delta_ci": p05.bootstrap_paired_stop_rate_delta_ci(champion, kept_ids),
        "material_segment_regressions": p05._material_segment_regressions(champion, kept),
    }


def evaluate_r07_hypothesis(hypothesis: Dict[str, Any],
                            train_rows: Sequence[Dict[str, Any]],
                            valid_rows: Sequence[Dict[str, Any]],
                            config: Dict[str, Any],
                            active_components: Sequence[str]) -> Dict[str, Any]:
    """Avalia UMA hipótese. Gate SOMENTE de validação, sem promoção possível.

    Reutiliza os CHECKS de redução de perdas do laboratório de stops, mas não o
    wrapper que exige origem `PERSISTENT_ADVERSE` e seleciona hipóteses pela
    validação — aquele marcador não se aplica aqui e não foi falsificado.
    """
    p05 = _p05()
    hyp_id = str(hypothesis.get("id") or "")
    base = {
        "id": hyp_id,
        "label": hypothesis.get("label"),
        "type": R07_HYPOTHESIS_TYPE,
        "version": R07_HYPOTHESIS_VERSION,
        "hash": _hypothesis_hash(hyp_id, hypothesis.get("config") or {}),
        "config": hypothesis.get("config"),
        "executable": False,
        "promotable": False,
        "opens_holdout": False,
    }

    try:
        train = r07_rule_comparison(train_rows, hyp_id, config, active_components)
        validation = r07_rule_comparison(valid_rows, hyp_id, config, active_components)
    except Exception as exc:
        return {**base, "status": R07_UNAVAILABLE, "reason_code": "COMPARISON_FAILED",
                "detail": "não foi possível comparar esta hipótese nesta execução",
                "train": None, "validation": None, "checks": [], "error": str(exc)}

    def _covered(stage: Dict[str, Any]) -> bool:
        return (stage["evaluable"] > 0 and all(
            (stage.get(key) or 0) >= p05.P05_MIN_FEATURE_COVERAGE_PCT
            for key in ("coverage_pct", "eligible_rule_coverage_pct", "metric_coverage_pct")))

    # Ausência de observações não prova ausência de efeito. O resultado só é
    # descritivo do universo conhecido com cobertura suficiente NOS DOIS lados.
    if (_covered(train) and _covered(validation)
            and validation["operations_removed"] == 0 and train["operations_removed"] == 0):
        return {**base, "status": R07_NO_INCREMENTAL_CHANGE,
                "reason_code": "NO_INCREMENTAL_CHANGE",
                "detail": ("no universo comparável de treino e validação, a regra "
                           "não remove operações adicionais — nenhum benefício "
                           "é demonstrado; desconhecidos permanecem fora da conclusão"),
                "train": train, "validation": validation, "checks": [],
                "risks": []}

    checks: List[Dict[str, Any]] = []
    insuficientes: set = set()

    def _check(name: str, passed: bool, detail: str, *, soft: bool = False) -> bool:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed and soft:
            insuficientes.add(name)
        return bool(passed)

    data_prerequisites: set = set()
    for name, stage in (("treino", train), ("validacao", validation)):
        for prefix, field in (("cobertura", "coverage_pct"),
                              ("cobertura_regra", "eligible_rule_coverage_pct"),
                              ("cobertura_metricas", "metric_coverage_pct")):
            check_name = f"{prefix}_{name}"
            data_prerequisites.add(check_name)
            _check(check_name, (stage[field] or 0) >= p05.P05_MIN_FEATURE_COVERAGE_PCT,
                   f"{field} em {name} = {stage[field]}% "
                   f"(mín {p05.P05_MIN_FEATURE_COVERAGE_PCT}%)", soft=True)
        check_name = f"amostra_afetada_{name}"
        data_prerequisites.add(check_name)
        _check(check_name, stage["operations_removed"] >= p05.P051_MIN_AFFECTED,
               f"{stage['operations_removed']} operações afetadas em {name} "
               f"(mín {p05.P051_MIN_AFFECTED})", soft=True)
        preserved = stage["operations_preserved_pct"]
        _check(f"operacoes_preservadas_{name}",
               preserved is not None and preserved >= p05.P052B_MIN_PRESERVED_PCT,
               f"preserva {preserved}% em {name} (mín {p05.P052B_MIN_PRESERVED_PCT}%)")

    cand, champ, rem = validation["candidate"], validation["champion"], validation["removed"]
    _check("expectancy_candidato_positiva",
           cand["expectancy_r"] is not None and cand["expectancy_r"] > 0,
           f"expectancy do candidato na validação = {cand['expectancy_r']}")
    _check("soma_r_candidato_positiva",
           cand["sum_r"] is not None and cand["sum_r"] > 0,
           f"soma R do candidato na validação = {cand['sum_r']}")
    _check("profit_factor_nao_pior",
           p05._pf_not_worse(cand["profit_factor"], champ["profit_factor"]),
           f"PF candidato={cand['profit_factor']} vs champion={champ['profit_factor']}")
    _check("drawdown_nao_pior",
           (cand["max_drawdown_r"] is not None and champ["max_drawdown_r"] is not None
            and cand["max_drawdown_r"] <= champ["max_drawdown_r"] + 1e-9),
           f"drawdown candidato={cand['max_drawdown_r']} vs champion={champ['max_drawdown_r']}")
    _check("removidas_negativas",
           rem["expectancy_r"] is not None and rem["expectancy_r"] < 0,
           f"expectancy das removidas = {rem['expectancy_r']}")

    rem_ci = rem.get("expectancy_ci")
    _check("ic_superior_removidas_negativo",
           bool(rem_ci) and rem_ci.get("high") is not None and rem_ci["high"] < 0,
           f"IC 95% das removidas = {rem_ci}", soft=True)
    paired = validation.get("paired_expectancy_delta_ci")
    _check("ic_inferior_delta_positivo",
           bool(paired) and paired.get("low") is not None and paired["low"] > 0,
           f"IC 95% do delta pareado de expectancy = {paired}", soft=True)
    _check("stop_rate_menor",
           (cand["stop_rate_pct"] is not None and champ["stop_rate_pct"] is not None
            and cand["stop_rate_pct"] < champ["stop_rate_pct"]),
           f"stop rate candidato={cand['stop_rate_pct']}% vs champion={champ['stop_rate_pct']}%")
    sr_ci = validation.get("stop_rate_delta_ci")
    _check("ic_superior_delta_stop_negativo",
           bool(sr_ci) and sr_ci.get("high_pp") is not None and sr_ci["high_pp"] < 0,
           f"IC 95% do delta de stop rate = {sr_ci}", soft=True)

    regressoes = validation.get("material_segment_regressions") or []
    _check("sem_regressao_material", not regressoes,
           f"{len(regressoes)} segmento(s) confiável(is) com regressão material")

    falhas = [c["name"] for c in checks if not c["passed"]]
    substantivas = [n for n in falhas if n not in insuficientes]
    dados_faltando = bool(data_prerequisites.intersection(falhas))
    if not falhas:
        status, code = R07_VALIDATION_SUPPORTED, "ALL_VALIDATION_CHECKS_PASSED"
        detalhe = ("sobreviveu a todos os checks de validação — NÃO é aprovação, "
                   "não abre o holdout e não autoriza execução")
    elif dados_faltando or not substantivas:
        status, code = R07_INSUFFICIENT, "INSUFFICIENT_EVIDENCE"
        detalhe = f"evidência insuficiente para julgar: {', '.join(falhas)}"
    else:
        status, code = R07_NOT_SUPPORTED, "VALIDATION_CHECK_FAILED"
        detalhe = f"não sustentada na validação: {', '.join(falhas)}"

    riscos = [
        f"abster-se removeria {validation['wins_removed']} operações vencedoras "
        f"na validação",
        f"removeria {validation['operations_removed']} de {validation['evaluable']} "
        f"oportunidades avaliáveis da validação",
        f"{validation['unknown_total']} linhas saíram do universo por baseline ou "
        f"regra desconhecidos — não são operações evitadas",
    ]
    if validation["protected_exits_removed"]:
        riscos.append(f"{validation['protected_exits_removed']} saídas protetivas "
                      f"pós-TP1 também seriam removidas")
    if regressoes:
        riscos.append(f"{len(regressoes)} segmento(s) pioraria(m) materialmente")

    return {**base, "status": status, "reason_code": code, "detail": detalhe,
            "train": train, "validation": validation, "checks": checks,
            "stops_avoided": validation["stops_avoided"],
            "wins_removed": validation["wins_removed"],
            "operations_preserved_pct": validation["operations_preserved_pct"],
            "risks": riscos}


def scenario_distribution(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Distribuição descritiva dos cenários — inclusive o histórico LEGACY."""
    macro: Dict[str, int] = {}
    trend: Dict[str, int] = {}
    structure: Dict[str, int] = {}
    legacy = com_contexto = 0
    for row in rows:
        ctx = (row.get("features") or {}).get(R07_CONTEXT_KEY)
        cls = classify_context(ctx)
        if cls.get("legacy") or cls.get("schema_version") is None:
            legacy += 1
        else:
            com_contexto += 1
        for d in cls.get("macro") or []:
            macro[d] = macro.get(d, 0) + 1
        t = cls.get("trend") or DIM_UNKNOWN
        trend[t] = trend.get(t, 0) + 1
        for d in cls.get("structure") or []:
            structure[d] = structure.get(d, 0) + 1
    total = len(rows)
    return {
        "total": total,
        "with_r07_context": com_contexto,
        "legacy_without_context": legacy,
        "context_coverage_pct": round(com_contexto / total * 100, 1) if total else None,
        "macro": macro, "trend": trend, "structure": structure,
        "legacy_note": ("linhas sem r07_context só exibem o que foi realmente "
                        "persistido; tendência EMA, qualidade macro e confirmação "
                        "de rompimento NÃO são reconstruídas por proxy"),
    }


def build_regime_playbooks(train_rows: Sequence[Dict[str, Any]],
                           valid_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Seção R07A do diagnóstico. Puro sobre as linhas já carregadas.

    Não abre o holdout, não escreve no banco, não cria experimento e não
    devolve `promotion_plan`. `VALIDATION_SUPPORTED` é o teto possível.
    """
    p05 = _p05()
    out: Dict[str, Any] = {
        "phase": R07_PHASE,
        "execution_mode": R07_MODE,
        "read_only": True,
        "executable": False,
        "promotable": False,
        "opens_holdout": False,
        "holdout_status": p05.HOLDOUT_SEALED,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "baseline_name": "baseline de seleção reconstruído sobre setups SHADOW",
        "hypothesis_hashes": hypothesis_hashes(),
        "catalog": scenario_catalog(),
        "limitations": list(R07_LIMITATIONS),
        "note": ("somente análise — nenhuma estratégia foi alterada, promovida "
                 "ou ativada"),
    }

    try:
        purged_valid, purge_info = temporal_purge(train_rows, valid_rows)
        out["temporal_purge"] = purge_info
        out["scenarios"] = {
            "train": scenario_distribution(train_rows),
            "validation": scenario_distribution(purged_valid),
        }
    except Exception as exc:
        return {**out, "status": R07_UNAVAILABLE, "reason_code": "SCENARIOS_FAILED",
                "hypotheses": [], "error": str(exc)}

    try:
        config = p05.discover_champion_config()
        ativos, cobertura = _baseline_components(train_rows)
        out["baseline"] = {
            "config_source": "config LIVE atual congelada para a análise",
            "active_components": ativos,
            "component_coverage": cobertura,
            "caveat": ("a config atual não equivale à config histórica de cada "
                       "trade; componentes sem cobertura ficam desligados dos "
                       "dois lados e aparecem em component_coverage"),
        }
    except Exception as exc:
        return {**out, "status": R07_UNAVAILABLE, "reason_code": "BASELINE_FAILED",
                "hypotheses": [], "error": str(exc)}

    avaliadas: List[Dict[str, Any]] = []
    for hyp in R07_HYPOTHESES:
        try:
            avaliadas.append(evaluate_r07_hypothesis(
                hyp, train_rows, purged_valid, config, ativos))
        except Exception as exc:
            avaliadas.append({
                "id": hyp["id"], "label": hyp.get("label"),
                "hash": _hypothesis_hash(hyp["id"], hyp.get("config") or {}),
                "status": R07_UNAVAILABLE, "reason_code": "EVALUATION_FAILED",
                "detail": "não foi possível avaliar esta hipótese",
                "executable": False, "promotable": False, "opens_holdout": False,
                "error": str(exc),
            })
    out["hypotheses"] = avaliadas
    if any(h.get("error") for h in avaliadas):
        # O cache externo precisa enxergar também falhas parciais internas.
        out["error"] = "HYPOTHESIS_EVALUATION_FAILED"
    contagem: Dict[str, int] = {}
    for h in avaliadas:
        contagem[h["status"]] = contagem.get(h["status"], 0) + 1
    out["status_counts"] = contagem
    out["status"] = (R07_VALIDATION_SUPPORTED
                     if contagem.get(R07_VALIDATION_SUPPORTED) else
                     (R07_NOT_SUPPORTED if contagem.get(R07_NOT_SUPPORTED) else
                      (R07_NO_INCREMENTAL_CHANGE
                       if contagem.get(R07_NO_INCREMENTAL_CHANGE) else
                       (R07_INSUFFICIENT if contagem.get(R07_INSUFFICIENT)
                        else R07_UNAVAILABLE))))
    return out
