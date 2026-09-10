"""R08A — laboratório LOCAL de pesquisa do score. Preparação do P08.

`LOCAL_RESEARCH_ONLY`: este módulo **não é importado por nenhum caminho de
produção** — nem scanner, nem snapshot, nem API, nem frontend, nem executor.
Ele existe para (a) espelhar a fórmula BRUTA V2 sob configuração explícita e
(b) medir uma única ablação estrutural contra ela.

O que ele NÃO faz, por contrato:
  • não lê ENV, relógio, banco, arquivos, cache ou rede dentro da matemática;
  • não produz probabilidade, tier, sizing, Kelly ou bins;
  • não aplica bônus HTF, auto-learning, ajustes do executor ou penalidade de
    seleção — nada disso pertence à fórmula bruta;
  • não promove candidato nem afirma melhora de resultado.

Unidades: `confluence_pct` e o score são **pontos 0–100**, não probabilidade.
`adx` é o ADX bruto. `funding_pct` é o funding em PONTOS PERCENTUAIS (o mesmo
número que `derivatives.funding_rate_pct`).
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
from typing import Any, Dict, Optional, Tuple

R08A_PHASE = "R08A"
R08A_SCHEMA_VERSION = 1
LAB_MODE = "LOCAL_RESEARCH_ONLY"

FORMULA_V2_RAW = "SCORE_V2_RAW"
FORMULA_V3_ABLATION = "SCORE_V3_CONF_ONLY_ABLATION"
KNOWN_FORMULAS = frozenset({FORMULA_V2_RAW, FORMULA_V3_ABLATION})

# ── Estados e motivos (vocabulário fechado) ─────────────────────────────────
STATUS_OK = "OK"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_INVALID_INPUT = "INVALID_INPUT"
STATUS_INVALID_CONFIG = "INVALID_CONFIG"
LAB_STATUSES = frozenset({STATUS_OK, STATUS_UNAVAILABLE,
                          STATUS_INVALID_INPUT, STATUS_INVALID_CONFIG})

REASON_OK = "OK"
REASON_NO_COMPONENT = "NO_COMPUTABLE_COMPONENT"
REASON_NO_CONFLUENCE = "CONFLUENCE_REQUIRED"
REASON_NOT_NUMERIC = "INPUT_NOT_NUMERIC"
REASON_NOT_FINITE = "INPUT_NOT_FINITE"
REASON_OUT_OF_DOMAIN = "INPUT_OUT_OF_DOMAIN"
REASON_WEIGHTS_NOT_NUMERIC = "WEIGHTS_NOT_NUMERIC"
REASON_WEIGHTS_NEGATIVE = "WEIGHTS_NEGATIVE"
REASON_WEIGHTS_SUM_ZERO = "WEIGHTS_SUM_NOT_POSITIVE"
REASON_WEIGHTS_MISSING_KEY = "WEIGHTS_MISSING_KEY"
REASON_WEIGHTS_OVERFLOW = "WEIGHTS_ARITHMETIC_OVERFLOW"
REASON_COMPARISON_UNAVAILABLE = "COMPARISON_UNAVAILABLE"
LAB_REASON_CODES = frozenset({
    REASON_OK, REASON_NO_COMPONENT, REASON_NO_CONFLUENCE, REASON_NOT_NUMERIC,
    REASON_NOT_FINITE, REASON_OUT_OF_DOMAIN, REASON_WEIGHTS_NOT_NUMERIC,
    REASON_WEIGHTS_NEGATIVE, REASON_WEIGHTS_SUM_ZERO,
    REASON_WEIGHTS_MISSING_KEY, REASON_WEIGHTS_OVERFLOW, REASON_COMPARISON_UNAVAILABLE,
})

#: Domínios DOCUMENTADOS das entradas. Fora deles o laboratório RECUSA em vez de
#: saturar em silêncio — a V2 de produção satura, e essa diferença é o ponto:
#: aqui queremos enxergar a entrada inválida, não escondê-la num clamp.
DOMAIN_CONFLUENCE_PCT = (0.0, 100.0)
DOMAIN_ADX = (0.0, 100.0)
DOMAIN_FUNDING_PCT = (-100.0, 100.0)

WEIGHT_KEYS = ("conf", "adx", "der")

LAB_LIMITATIONS = [
    "espelha a fórmula BRUTA V2 sob configuração explícita — não é replay fiel "
    "do bot nem o score histórico final",
    "não inclui relevância por timeframe, bônus HTF, auto-learning, tier nem os "
    "ajustes do executor",
    "a ablação é estrutural e provisória: NÃO é o Score V3 definitivo, não prova "
    "que a confluência isolada opera melhor e não elimina toda dupla contagem",
    "score é pontuação, não probabilidade de lucro — nenhum bin, Kelly, tier ou "
    "tamanho é produzido",
    "configuração local não é configuração confirmada de produção",
    "nenhum dado histórico é carregado: diferença de pontuação não estima lucro, "
    "stops evitados ou volume real",
]


# ════════════════════════════════════════════════════════════════════════════
#  Validação pura
# ════════════════════════════════════════════════════════════════════════════
def _numero(value) -> Tuple[Optional[float], Optional[str]]:
    """(float finito, None) ou (None, motivo). `bool` e string NÃO são número."""
    if value is None:
        return None, None                      # ausência legítima, sem motivo
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None, REASON_NOT_NUMERIC
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, REASON_NOT_NUMERIC
    if not math.isfinite(f):
        return None, REASON_NOT_FINITE
    return f, None


def _no_dominio(value: Optional[float], dominio: Tuple[float, float]) -> bool:
    return value is None or dominio[0] <= value <= dominio[1]


def validate_weights(weights: Any) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """Pesos finitos, não negativos e com soma positiva. Nada é corrigido."""
    if not isinstance(weights, dict):
        return None, REASON_WEIGHTS_NOT_NUMERIC
    limpos: Dict[str, float] = {}
    for chave in WEIGHT_KEYS:
        if chave not in weights:
            return None, REASON_WEIGHTS_MISSING_KEY
        valor, motivo = _numero(weights[chave])
        if valor is None:
            return None, motivo or REASON_WEIGHTS_NOT_NUMERIC
        if valor < 0:
            return None, REASON_WEIGHTS_NEGATIVE
        limpos[chave] = valor
    total = sum(limpos.values())
    if not math.isfinite(total):
        return None, REASON_WEIGHTS_OVERFLOW
    if total <= 0:
        return None, REASON_WEIGHTS_SUM_ZERO
    return limpos, None


def local_v2_weights() -> Dict[str, float]:
    """Pesos V2 **do módulo local**, lidos por conveniência de teste/documento.

    Isto é configuração LOCAL. Não é, e não deve ser apresentada como,
    configuração confirmada de produção — a produção lê ENV no boot dela.
    """
    from services import recommendation_service as rs
    return {"conf": float(rs.SCORE_V2_W_CONF),
            "adx": float(rs.SCORE_V2_W_ADX),
            "der": float(rs.SCORE_V2_W_DER)}


def config_hash(config: Dict[str, Any]) -> str:
    """SHA-256 do JSON canônico da configuração. Sem resultado, sem relógio."""
    canonico = json.dumps(config, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def _config_safe(value: Any) -> Any:
    """Config inválida ainda precisa ser serializável para entrar no hash."""
    if isinstance(value, dict):
        return {str(k): _config_safe(v) for k, v in value.items()}
    num, erro = _numero(value)
    if erro or num is None:
        return repr(value) if value is not None else None
    return num


def _resultado(formula: str, status: str, reason: str, *, score=None,
               components=None, effective_weights=None, contributions=None,
               missing=None, config=None) -> Dict[str, Any]:
    cfg = dict(config or {})
    return {
        "schema_version": R08A_SCHEMA_VERSION,
        "formula_id": formula,
        "execution_mode": LAB_MODE,
        "promotable": False,
        "calibrated": False,
        "status": status,
        "reason_code": reason,
        "score": score,
        "components": dict(components or {}),
        "effective_weights": dict(effective_weights or {}),
        "contributions": dict(contributions or {}),
        "missing_components": sorted(missing or []),
        "config": cfg,
        "config_hash": config_hash(cfg),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Baseline: V2 BRUTA sob configuração explícita
# ════════════════════════════════════════════════════════════════════════════
def score_v2_raw(*, confluence_pct=None, adx=None, funding_pct=None,
                 weights: Any) -> Dict[str, Any]:
    """Espelha `recommendation_service._compute_score_v2`, e só ela.

        conf_n = confluence_pct
        adx_n  = clamp(adx, 0, 50) / 50 * 100
        der_n  = 50 − clamp(funding_pct / 0.05, −1, +1) × 50
        score  = round(clamp(Σ wᵢ·xᵢ / Σ wᵢ, 0, 100), 1)   sobre os PRESENTES

    Componente ausente sai da soma E do denominador (renormalização). Nenhum
    componente presente ⇒ `UNAVAILABLE` — nunca um score neutro fabricado.
    """
    pesos, motivo = validate_weights(weights)
    if pesos is None:
        return _resultado(FORMULA_V2_RAW, STATUS_INVALID_CONFIG, motivo,
                          config={"weights": _config_safe(weights)})
    cfg = {"weights": pesos,
           "domains": {"confluence_pct": list(DOMAIN_CONFLUENCE_PCT),
                       "adx": list(DOMAIN_ADX),
                       "funding_pct": list(DOMAIN_FUNDING_PCT)}}

    brutos = {"confluence_pct": confluence_pct, "adx": adx, "funding_pct": funding_pct}
    dominios = {"confluence_pct": DOMAIN_CONFLUENCE_PCT, "adx": DOMAIN_ADX,
                "funding_pct": DOMAIN_FUNDING_PCT}
    limpos: Dict[str, Optional[float]] = {}
    for nome, valor in brutos.items():
        num, erro = _numero(valor)
        if erro:
            return _resultado(FORMULA_V2_RAW, STATUS_INVALID_INPUT, erro, config=cfg)
        if not _no_dominio(num, dominios[nome]):
            return _resultado(FORMULA_V2_RAW, STATUS_INVALID_INPUT,
                              REASON_OUT_OF_DOMAIN, config=cfg)
        limpos[nome] = num

    conf_n = limpos["confluence_pct"]
    adx_n = (max(0.0, min(limpos["adx"], 50.0)) / 50.0 * 100.0
             if limpos["adx"] is not None else None)
    der_n = (50.0 - max(-1.0, min(limpos["funding_pct"] / 0.05, 1.0)) * 50.0
             if limpos["funding_pct"] is not None else None)

    normalizados = {"conf": conf_n, "adx": adx_n, "der": der_n}
    ausentes = [k for k, v in normalizados.items() if v is None]

    num = den = 0.0
    for chave in WEIGHT_KEYS:
        val, w = normalizados[chave], pesos[chave]
        if w > 0 and val is not None:
            num += w * val
            den += w
    if den == 0:
        return _resultado(FORMULA_V2_RAW, STATUS_UNAVAILABLE, REASON_NO_COMPONENT,
                          components=normalizados, missing=ausentes, config=cfg)

    # Pesos individuais finitos não garantem soma/produto finito. Não deixar
    # o clamp converter overflow/NaN em score 100; preservar a fórmula normal.
    if not math.isfinite(num) or not math.isfinite(den):
        return _resultado(FORMULA_V2_RAW, STATUS_INVALID_CONFIG, REASON_WEIGHTS_OVERFLOW,
                          components=normalizados, missing=ausentes, config=cfg)
    raw_score = num / den
    if not math.isfinite(raw_score):
        return _resultado(FORMULA_V2_RAW, STATUS_INVALID_CONFIG, REASON_WEIGHTS_OVERFLOW,
                          components=normalizados, missing=ausentes, config=cfg)
    score = round(max(0.0, min(100.0, raw_score)), 1)
    ativo = {k: (pesos[k] > 0 and normalizados[k] is not None) for k in WEIGHT_KEYS}
    efetivos = {k: (pesos[k] / den if ativo[k] else 0.0) for k in WEIGHT_KEYS}
    contrib = {k: (round(pesos[k] * normalizados[k] / den, 6) if ativo[k] else 0.0)
               for k in WEIGHT_KEYS}
    return _resultado(FORMULA_V2_RAW, STATUS_OK, REASON_OK, score=score,
                      components=normalizados, effective_weights=efetivos,
                      contributions=contrib, missing=ausentes, config=cfg)


# ════════════════════════════════════════════════════════════════════════════
#  Ablação: SCORE_V3_CONF_ONLY_ABLATION
# ════════════════════════════════════════════════════════════════════════════
def score_v3_conf_only(*, confluence_pct=None, adx=None, funding_pct=None,
                       **_ignorado) -> Dict[str, Any]:
    """Ablação ESTRUTURAL: score bruto = `confluence_pct`, e nada mais.

    Não adiciona ADX nem funding POR FORA da confluência — eles continuam
    dentro do agregado de confluência, que é justamente o ponto da medida.
    Sem confluência válida ⇒ `UNAVAILABLE`; nenhum outro componente a
    substitui e não há renormalização.

    Finalidade única: medir quanto as camadas externas de ADX/funding movem a
    pontuação em relação ao agregado de confluência. Isto **não** prova que a
    dupla contagem foi eliminada, que a confluência isolada é melhor, nem que
    este candidato deva substituir a V2.
    """
    cfg = {"source": "confluence_pct", "external_components": [],
           "domains": {"confluence_pct": list(DOMAIN_CONFLUENCE_PCT)}}
    valor, erro = _numero(confluence_pct)
    if erro:
        return _resultado(FORMULA_V3_ABLATION, STATUS_INVALID_INPUT, erro, config=cfg)
    if valor is None:
        return _resultado(FORMULA_V3_ABLATION, STATUS_UNAVAILABLE,
                          REASON_NO_CONFLUENCE, missing=["conf"], config=cfg)
    if not _no_dominio(valor, DOMAIN_CONFLUENCE_PCT):
        return _resultado(FORMULA_V3_ABLATION, STATUS_INVALID_INPUT,
                          REASON_OUT_OF_DOMAIN, config=cfg)
    score = round(max(0.0, min(100.0, valor)), 1)
    return _resultado(FORMULA_V3_ABLATION, STATUS_OK, REASON_OK, score=score,
                      components={"conf": valor}, effective_weights={"conf": 1.0},
                      contributions={"conf": round(valor, 6)}, config=cfg)


# ════════════════════════════════════════════════════════════════════════════
#  Comparação lado a lado
# ════════════════════════════════════════════════════════════════════════════
def compare_formulas(*, confluence_pct=None, adx=None, funding_pct=None,
                     weights: Any, direction: Any = None) -> Dict[str, Any]:
    """Baseline, ablação, delta e explicação matemática — sem preencher zero.

    Se qualquer um dos lados não for calculável, o delta é `None` e o motivo é
    explícito. `direction` entra apenas na descrição: nenhuma das duas fórmulas
    usa o lado da operação.
    """
    base = score_v2_raw(confluence_pct=confluence_pct, adx=adx,
                        funding_pct=funding_pct, weights=weights)
    abl = score_v3_conf_only(confluence_pct=confluence_pct, adx=adx,
                             funding_pct=funding_pct)

    comparavel = base["status"] == STATUS_OK and abl["status"] == STATUS_OK
    delta = round(abl["score"] - base["score"], 6) if comparavel else None
    return {
        "schema_version": R08A_SCHEMA_VERSION,
        "execution_mode": LAB_MODE,
        "promotable": False,
        "calibrated": False,
        "comparable": comparavel,
        "reason_code": REASON_OK if comparavel else REASON_COMPARISON_UNAVAILABLE,
        "direction": direction if isinstance(direction, str) else None,
        "baseline": base,
        "ablation": abl,
        "delta": delta,
        "delta_note": ("ablação − baseline, em PONTOS de score; não é lucro, "
                       "stop evitado nem probabilidade"),
        "explanation": _explicacao(base, abl, comparavel),
        "limitations": list(LAB_LIMITATIONS),
    }


def _explicacao(base: Dict[str, Any], abl: Dict[str, Any], comparavel: bool) -> str:
    if not comparavel:
        return (f"comparação indisponível: baseline={base['status']}"
                f"/{base['reason_code']}, ablação={abl['status']}"
                f"/{abl['reason_code']}")
    partes = []
    for chave, rotulo in (("conf", "confluência"), ("adx", "ADX"), ("der", "funding")):
        peso = base["effective_weights"].get(chave, 0.0)
        valor = base["components"].get(chave)
        if peso and valor is not None:
            partes.append(f"{rotulo} {valor:.4g}x{peso:.4f}")
    soma = " + ".join(partes) if partes else "nenhum componente"
    return (f"baseline V2 bruta = {soma} = {base['score']}; "
            f"ablacao = confluencia {abl['components'].get('conf'):.4g} = {abl['score']}; "
            f"a diferenca mede quanto as camadas EXTERNAS de ADX/funding "
            f"deslocam a pontuacao em relacao ao agregado de confluencia")


def lab_manifest() -> Dict[str, Any]:
    """Identidade do laboratório — para o documento e para os testes."""
    return {
        "phase": R08A_PHASE,
        "schema_version": R08A_SCHEMA_VERSION,
        "execution_mode": LAB_MODE,
        "promotable": False,
        "calibrated": False,
        "formulas": sorted(KNOWN_FORMULAS),
        "statuses": sorted(LAB_STATUSES),
        "reason_codes": sorted(LAB_REASON_CODES),
        "domains": {"confluence_pct": list(DOMAIN_CONFLUENCE_PCT),
                    "adx": list(DOMAIN_ADX),
                    "funding_pct": list(DOMAIN_FUNDING_PCT)},
        "limitations": list(LAB_LIMITATIONS),
        "note": ("pesquisa local: nenhum caminho de produção importa este "
                 "módulo e nenhum candidato foi ativado"),
    }
