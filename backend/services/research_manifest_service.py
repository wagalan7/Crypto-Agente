"""R13 — manifesto AUTORIZADO do estudo: primeiro congelar, depois medir.

Contrato FECHADO e versionado que descreve, antes de qualquer resultado:

  • a decisão humana (autoridade, referência verificável, instante) — e o
    estado dela. `approved=true` não existe como campo: aprovação é a decisão
    registrada, não uma propriedade solta no payload;
  • baseline COMPLETA e candidata COMPLETA (núcleo, score, playbooks, política
    e a gestão congelada), lado a lado;
  • população/universo, a mudança ISOLADA, o escopo exato da comparação,
    cortes e divisão temporal;
  • custos: fonte, unidade, ativo, versões, disponibilidade e tratamento de
    fee/slippage/funding;
  • hashes SEPARADOS por seção + `bundle_hash`, calculados aqui — antes de
    existir qualquer métrica.

O que bloqueia: campo desconhecido, campo ausente, booleano no lugar de
número, NaN/inf, fonte de custo incompatível, mudança não isolada, escopo não
implementado e DRIFT (hash declarado que não bate com o recalculado).

Nada aqui abre sessão, lê ENV ou executa consulta: é contrato puro. O módulo
não escolhe baseline nem candidata — sem decisão registrada o estudo real
continua `BLOCKED_MISSING_DECISION`, e manifestos sintéticos existem apenas
como `APPROVED_TEST_ONLY` (engenharia e teste), nunca como estudo real.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MANIFEST_VERSION = "R13_RESEARCH_MANIFEST_V1"

# ── Escopos de comparação ───────────────────────────────────────────────────
SCOPE_MANAGEMENT = "MANAGEMENT_ONLY"
SCOPE_SELECTION = "SELECTION_ONLY"
IMPLEMENTED_SCOPES = (SCOPE_MANAGEMENT, SCOPE_SELECTION)

# ── Estados do manifesto (derivados da decisão registrada) ──────────────────
STATE_DRAFT = "DRAFT"
STATE_BLOCKED = "BLOCKED_MISSING_DECISION"
STATE_TEST_ONLY = "APPROVED_TEST_ONLY"
STATE_APPROVED = "APPROVED_RESEARCH"

DECISION_DRAFT = "DRAFT"
DECISION_TEST_ONLY = "TEST_ONLY"
DECISION_APPROVED = "APPROVED"
DECISION_STATES = (DECISION_DRAFT, DECISION_TEST_ONLY, DECISION_APPROVED)

# ── Motivos ─────────────────────────────────────────────────────────────────
OK = "OK"
UNKNOWN_FIELD = "MANIFEST_UNKNOWN_FIELD"
FIELD_MISSING = "MANIFEST_FIELD_MISSING"
NOT_MAPPING = "MANIFEST_NOT_MAPPING"
VERSION_MISMATCH = "MANIFEST_VERSION_MISMATCH"
BOOL_AS_NUMBER = "MANIFEST_BOOL_AS_NUMBER"
NON_FINITE = "MANIFEST_NON_FINITE_NUMBER"
TEXT_REQUIRED = "MANIFEST_TEXT_REQUIRED"
SCOPE_NOT_IMPLEMENTED = "COMPARISON_SCOPE_NOT_IMPLEMENTED"
COSTS_SOURCE_INCOMPATIBLE = "COSTS_SOURCE_INCOMPATIBLE"
COSTS_INCOMPLETE = "COSTS_INCOMPLETE"
CHANGE_NOT_ISOLATED = "CHANGE_NOT_ISOLATED"
CHANGE_SCOPE_MISMATCH = "CHANGE_SCOPE_MISMATCH"
DECISION_EVIDENCE_MISSING = "DECISION_EVIDENCE_MISSING"
DECISION_STATE_INVALID = "DECISION_STATE_INVALID"
MANIFEST_DRIFT = "MANIFEST_DRIFT"
SPLIT_INVALID = "MANIFEST_SPLIT_INVALID"
CONFIG_INVALID = "MANIFEST_CONFIG_INVALID"
SIDES_IDENTICAL_DECLARED_CHANGE = "SIDES_IDENTICAL_BUT_CHANGE_DECLARED"

#: Perguntas que SÓ o usuário responde. Enquanto faltarem, o estudo real fica
#: bloqueado — o implementador não escolhe o par comparado.
DECISION_REQUIRED = (
    "Qual baseline congelada: champion LIVE (CHAMPION_LEGACY/SCORE_V2) ou a "
    "configuração default do núcleo R07D?",
    "Qual candidata congelada e qual a ÚNICA mudança estrutural dela?",
    "Qual escopo/população e quais custos comparáveis valem para o par.",
)

# ── Schema fechado ──────────────────────────────────────────────────────────
MANIFEST_FIELDS = ("manifest_version", "study_id", "decision", "comparison_scope",
                   "population", "isolated_change", "baseline", "candidate",
                   "costs", "split", "hashes")
DECISION_FIELDS = ("state", "authority", "reference", "recorded_at_ms", "note")
SIDE_FIELDS = ("label", "core_version", "core_config_hash", "score_version",
               "score_config_hash", "playbooks", "policy_version",
               "management_config", "selection_rule")
SELECTION_RULE_FIELDS = ("kind", "min_score", "playbook", "source")
#: Regras de seleção IMPLEMENTADAS. A baseline de um estudo de seleção usa a
#: decisão que o champion REALMENTE tomou no instante observado — reconstruir a
#: baseline de hoje não representa a configuração histórica. A candidata roda o
#: motor dela. Regra fora desta lista é recusada, nunca substituída por default.
RULE_OBSERVED_CHAMPION = "OBSERVED_CHAMPION_DECISION"
RULE_SCORE_V3_MIN = "SCORE_V3_MIN_SCORE"
SELECTION_RULES = (RULE_OBSERVED_CHAMPION, RULE_SCORE_V3_MIN)
#: Qual regra cada componente alterado exige na candidata.
RULE_BY_COMPONENT = {"SCORE_MODEL": RULE_SCORE_V3_MIN}
RULE_NOT_IMPLEMENTED = "SELECTION_RULE_NOT_IMPLEMENTED"
RULE_SCOPE_MISMATCH = "SELECTION_RULE_SCOPE_MISMATCH"
POPULATION_FIELDS = ("universe_version", "cohort", "scope_id", "quote",
                     "symbols", "min_rows")
COSTS_FIELDS = ("source", "unit", "asset", "versions", "availability",
                "fee_treatment", "slippage_treatment", "funding_treatment",
                "config")
SPLIT_FIELDS = ("as_of_ms", "train_start_ms", "validation_start_ms",
                "holdout_start_ms", "purge_bars", "embargo_bars")
CHANGE_FIELDS = ("component", "from_value", "to_value")
HASH_FIELDS = ("baseline_hash", "candidate_hash", "population_hash",
               "costs_hash", "split_hash", "bundle_hash")

#: Componentes que uma mudança isolada pode tocar, por escopo. Mudança de
#: seleção não é validada como mudança de gestão, nem o contrário.
CHANGE_COMPONENTS = {
    SCOPE_SELECTION: ("STRATEGY_CORE", "SCORE_MODEL", "PLAYBOOK_SET"),
    SCOPE_MANAGEMENT: ("MANAGEMENT_CONFIG",),
}
#: Campos de cada lado comparados para provar o isolamento da mudança.
COMPONENT_FIELDS = {
    "STRATEGY_CORE": ("core_version", "core_config_hash"),
    "SCORE_MODEL": ("score_version", "score_config_hash"),
    "PLAYBOOK_SET": ("playbooks",),
    "MANAGEMENT_CONFIG": ("management_config",),
}

#: Fontes de custo ACEITAS, com a unidade/ativo e a disponibilidade que cada
#: uma pode declarar. Modelo declarado NUNCA se apresenta como custo observado
#: da conta — a distinção é do contrato, não de um comentário.
COSTS_DECLARED_MODEL = "DECLARED_MODEL"
COSTS_OBSERVED_ACCOUNT = "OBSERVED_ACCOUNT_COSTS"
COST_SOURCES = {
    "R10A_COST_CONFIG_BPS": {"unit": "BPS_PER_SIDE", "asset": "USDT",
                             "availability": (COSTS_DECLARED_MODEL,)},
    "BINANCE_USDM_ACCOUNT_LEDGER": {"unit": "SETTLEMENT_ASSET_ABSOLUTE",
                                    "asset": "USDT",
                                    "availability": (COSTS_OBSERVED_ACCOUNT,)},
}
COST_TREATMENTS = ("APPLIED_PER_SIDE", "APPLIED_PER_BAR", "NOT_MODELLED")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class ManifestError(Exception):
    """Recusa EXPLÍCITA do manifesto: motivo + campo, nunca default silencioso."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
        self.reason_code = reason_code
        self.detail = detail


def _closed(raw: Any, campos: Sequence[str], nome: str) -> Dict[str, Any]:
    """Seção com schema FECHADO: nem campo extra, nem campo ausente."""
    if not isinstance(raw, Mapping):
        raise ManifestError(NOT_MAPPING, f"{nome}: objeto obrigatório")
    extras = sorted(set(raw) - set(campos))
    if extras:
        raise ManifestError(UNKNOWN_FIELD, f"{nome}: {extras}")
    faltando = sorted(set(campos) - set(raw))
    if faltando:
        raise ManifestError(FIELD_MISSING, f"{nome}: {faltando}")
    return dict(raw)


def _text(valor: Any, nome: str, *, limite: int = 200) -> str:
    if not isinstance(valor, str) or not valor.strip() or len(valor) > limite:
        raise ManifestError(TEXT_REQUIRED, nome)
    return valor.strip()


def _int(valor: Any, nome: str, *, minimo: Optional[int] = None,
         maximo: Optional[int] = None) -> int:
    """Inteiro de verdade: `True` não é 1 e NaN/inf não entram."""
    if isinstance(valor, bool):
        raise ManifestError(BOOL_AS_NUMBER, nome)
    if not isinstance(valor, (int, float)):
        raise ManifestError(NON_FINITE, nome)
    numero = float(valor)
    if not math.isfinite(numero) or numero != int(numero):
        raise ManifestError(NON_FINITE, nome)
    inteiro = int(numero)
    if minimo is not None and inteiro < minimo:
        raise ManifestError(SPLIT_INVALID, f"{nome}: abaixo de {minimo}")
    if maximo is not None and inteiro > maximo:
        raise ManifestError(SPLIT_INVALID, f"{nome}: acima de {maximo}")
    return inteiro


def _tuple_of_text(valor: Any, nome: str) -> Tuple[str, ...]:
    if not isinstance(valor, (list, tuple)) or not valor:
        raise ManifestError(FIELD_MISSING, f"{nome}: lista não vazia obrigatória")
    saida = []
    for item in valor:
        saida.append(_text(item, f"{nome}[]", limite=64))
    if len(set(saida)) != len(saida):
        raise ManifestError(UNKNOWN_FIELD, f"{nome}: item repetido")
    return tuple(saida)


# ── Seções ──────────────────────────────────────────────────────────────────
def _parse_decision(raw: Any) -> Dict[str, Any]:
    """Decisão HUMANA registrada. `APPROVED` exige autoridade, referência
    verificável e instante — a ausência disso não é aprovação implícita."""
    corpo = _closed(raw, DECISION_FIELDS, "decision")
    estado = _text(corpo["state"], "decision.state", limite=32).upper()
    if estado not in DECISION_STATES:
        raise ManifestError(DECISION_STATE_INVALID, f"decision.state={estado}")
    nota = corpo.get("note")
    if nota is not None and not isinstance(nota, str):
        raise ManifestError(TEXT_REQUIRED, "decision.note")
    saida: Dict[str, Any] = {"state": estado, "note": (nota or None)}
    if estado == DECISION_DRAFT:
        for campo in ("authority", "reference", "recorded_at_ms"):
            if corpo.get(campo) not in (None, ""):
                raise ManifestError(DECISION_STATE_INVALID,
                                    f"decision.{campo} com estado DRAFT")
        saida.update(authority=None, reference=None, recorded_at_ms=None)
        return saida
    for campo in ("authority", "reference"):
        if corpo.get(campo) in (None, ""):
            raise ManifestError(DECISION_EVIDENCE_MISSING, f"decision.{campo}")
        saida[campo] = _text(corpo[campo], f"decision.{campo}", limite=300)
    if corpo.get("recorded_at_ms") in (None, ""):
        raise ManifestError(DECISION_EVIDENCE_MISSING, "decision.recorded_at_ms")
    saida["recorded_at_ms"] = _int(corpo["recorded_at_ms"],
                                   "decision.recorded_at_ms", minimo=1)
    return saida


def _parse_rule(raw: Any, nome: str) -> Dict[str, Any]:
    """Regra de seleção do lado: congelada, conhecida e com corte explícito."""
    corpo = _closed(raw, SELECTION_RULE_FIELDS, nome)
    kind = _text(corpo["kind"], f"{nome}.kind", limite=48).upper()
    if kind not in SELECTION_RULES:
        raise ManifestError(RULE_NOT_IMPLEMENTED, f"{nome}.kind={kind}")
    saida: Dict[str, Any] = {"kind": kind,
                             "source": _text(corpo["source"], f"{nome}.source")}
    if kind == RULE_OBSERVED_CHAMPION:
        for campo in ("min_score", "playbook"):
            if corpo.get(campo) not in (None, ""):
                raise ManifestError(RULE_SCOPE_MISMATCH,
                                    f"{nome}.{campo} não se aplica a {kind}")
        saida.update(min_score=None, playbook=None)
        return saida
    # Corte numérico É parte do congelamento: nunca escolhido depois do resultado.
    valor = corpo.get("min_score")
    if isinstance(valor, bool):
        raise ManifestError(BOOL_AS_NUMBER, f"{nome}.min_score")
    if not isinstance(valor, (int, float)) or not math.isfinite(float(valor)) \
            or not 0.0 <= float(valor) <= 100.0:
        raise ManifestError(CONFIG_INVALID, f"{nome}.min_score em [0,100]")
    saida.update(min_score=float(valor),
                 playbook=_text(corpo["playbook"], f"{nome}.playbook", limite=48))
    return saida


def _parse_side(raw: Any, nome: str) -> Dict[str, Any]:
    """Um lado COMPLETO: núcleo, score, playbooks, política e gestão congelada."""
    corpo = _closed(raw, SIDE_FIELDS, nome)
    saida = {
        "label": _text(corpo["label"], f"{nome}.label", limite=64),
        "core_version": _text(corpo["core_version"], f"{nome}.core_version"),
        "core_config_hash": _text(corpo["core_config_hash"],
                                  f"{nome}.core_config_hash"),
        "score_version": _text(corpo["score_version"], f"{nome}.score_version"),
        "score_config_hash": _text(corpo["score_config_hash"],
                                   f"{nome}.score_config_hash"),
        "policy_version": _text(corpo["policy_version"], f"{nome}.policy_version"),
        "playbooks": list(_tuple_of_text(corpo["playbooks"], f"{nome}.playbooks")),
    }
    saida["selection_rule"] = _parse_rule(corpo["selection_rule"], f"{nome}.selection_rule")
    gestao = corpo["management_config"]
    if not isinstance(gestao, Mapping) or not gestao:
        raise ManifestError(CONFIG_INVALID, f"{nome}.management_config")
    # A gestão congelada é o manifesto de um motor REAL: reconstruído aqui,
    # não aceito como dicionário qualquer.
    from services import preselection_experiment_service as r12
    schema = r12.validate_replay_manifest(gestao)
    if not schema["ok"]:
        raise ManifestError(CONFIG_INVALID,
                            f"{nome}.management_config: {schema.get('detail')}")
    saida["management_config"] = dict(schema["manifest"])
    return saida


def _parse_population(raw: Any) -> Dict[str, Any]:
    corpo = _closed(raw, POPULATION_FIELDS, "population")
    simbolos = corpo["symbols"]
    if simbolos is None:
        universo = None                      # população inteira do escopo
    else:
        universo = list(_tuple_of_text(simbolos, "population.symbols"))
    return {
        "universe_version": _text(corpo["universe_version"],
                                  "population.universe_version"),
        "cohort": _text(corpo["cohort"], "population.cohort"),
        "scope_id": _text(corpo["scope_id"], "population.scope_id"),
        "quote": _text(corpo["quote"], "population.quote", limite=16).upper(),
        "symbols": universo,
        "min_rows": _int(corpo["min_rows"], "population.min_rows", minimo=1),
    }


def _parse_costs(raw: Any) -> Dict[str, Any]:
    """Custos comparáveis: fonte conhecida, unidade/ativo coerentes com ela e
    tratamento declarado para cada componente."""
    corpo = _closed(raw, COSTS_FIELDS, "costs")
    fonte = _text(corpo["source"], "costs.source")
    if fonte not in COST_SOURCES:
        raise ManifestError(COSTS_SOURCE_INCOMPATIBLE,
                            f"costs.source={fonte}; use {sorted(COST_SOURCES)}")
    esperado = COST_SOURCES[fonte]
    unidade = _text(corpo["unit"], "costs.unit", limite=64)
    ativo = _text(corpo["asset"], "costs.asset", limite=16).upper()
    disponibilidade = _text(corpo["availability"], "costs.availability", limite=64)
    if unidade != esperado["unit"] or ativo != esperado["asset"]:
        raise ManifestError(COSTS_SOURCE_INCOMPATIBLE,
                            f"costs: {fonte} usa {esperado['unit']}/{esperado['asset']}")
    if disponibilidade not in esperado["availability"]:
        raise ManifestError(COSTS_SOURCE_INCOMPATIBLE,
                            f"costs.availability={disponibilidade} incompatível com {fonte}")
    tratamentos = {}
    for campo in ("fee_treatment", "slippage_treatment", "funding_treatment"):
        valor = _text(corpo[campo], f"costs.{campo}", limite=32).upper()
        if valor not in COST_TREATMENTS:
            raise ManifestError(COSTS_INCOMPLETE, f"costs.{campo}={valor}")
        tratamentos[campo] = valor
    versoes = corpo["versions"]
    if not isinstance(versoes, Mapping) or not versoes:
        raise ManifestError(COSTS_INCOMPLETE, "costs.versions")
    versoes_norm = {str(chave): _text(valor, f"costs.versions.{chave}")
                    for chave, valor in sorted(versoes.items())}
    config = corpo["config"]
    if not isinstance(config, Mapping) or not config:
        raise ManifestError(COSTS_INCOMPLETE, "costs.config")
    from services import preselection_experiment_service as r12
    schema = r12.validate_costs_manifest(config)
    if not schema["ok"]:
        raise ManifestError(COSTS_INCOMPLETE, f"costs.config: {schema.get('detail')}")
    return {"source": fonte, "unit": unidade, "asset": ativo,
            "availability": disponibilidade, "versions": versoes_norm,
            "config": dict(schema["manifest"]), **tratamentos}


def _parse_split(raw: Any) -> Dict[str, Any]:
    """Divisão temporal: ordem estrita, purga e embargo explícitos."""
    corpo = _closed(raw, SPLIT_FIELDS, "split")
    valores = {campo: _int(corpo[campo], f"split.{campo}", minimo=0)
               for campo in SPLIT_FIELDS}
    if not (valores["train_start_ms"] < valores["validation_start_ms"]
            < valores["holdout_start_ms"] <= valores["as_of_ms"]):
        raise ManifestError(SPLIT_INVALID,
                            "ordem exigida: train < validation < holdout <= as_of")
    if valores["purge_bars"] < 1:
        raise ManifestError(SPLIT_INVALID, "split.purge_bars: mínimo 1")
    return valores


def _parse_change(raw: Any, *, escopo: str) -> Dict[str, Any]:
    corpo = _closed(raw, CHANGE_FIELDS, "isolated_change")
    componente = _text(corpo["component"], "isolated_change.component",
                       limite=32).upper()
    permitidos = CHANGE_COMPONENTS[escopo]
    if componente not in permitidos:
        raise ManifestError(CHANGE_SCOPE_MISMATCH,
                            f"{componente} não pertence a {escopo} ({permitidos})")
    return {"component": componente,
            "from_value": corpo["from_value"], "to_value": corpo["to_value"]}


def _side_component(side: Mapping[str, Any], componente: str) -> Any:
    return [side.get(campo) for campo in COMPONENT_FIELDS[componente]]


# ── Parse + congelamento ────────────────────────────────────────────────────
def parse_manifest(raw: Any) -> Dict[str, Any]:
    """Valida o manifesto inteiro e devolve o corpo NORMALIZADO com hashes.

    Os hashes são recalculados aqui: se o payload declarar hashes diferentes,
    isso é DRIFT e bloqueia — ninguém "congela" um corpo e mede outro.
    """
    corpo = _closed(raw, MANIFEST_FIELDS, "manifest")
    if corpo["manifest_version"] != MANIFEST_VERSION:
        raise ManifestError(VERSION_MISMATCH, str(corpo["manifest_version"]))
    escopo = _text(corpo["comparison_scope"], "comparison_scope", limite=32).upper()
    if escopo not in IMPLEMENTED_SCOPES:
        raise ManifestError(SCOPE_NOT_IMPLEMENTED, escopo)
    decisao = _parse_decision(corpo["decision"])
    baseline = _parse_side(corpo["baseline"], "baseline")
    candidata = _parse_side(corpo["candidate"], "candidate")
    populacao = _parse_population(corpo["population"])
    custos = _parse_costs(corpo["costs"])
    divisao = _parse_split(corpo["split"])
    mudanca = _parse_change(corpo["isolated_change"], escopo=escopo)

    # A mudança declarada tem de aparecer no componente certo E ser a ÚNICA.
    componente = mudanca["component"]
    if _side_component(baseline, componente) == _side_component(candidata, componente):
        raise ManifestError(SIDES_IDENTICAL_DECLARED_CHANGE, componente)
    outros = [nome for nome in COMPONENT_FIELDS if nome != componente]
    divergentes = [nome for nome in outros
                   if _side_component(baseline, nome) != _side_component(candidata, nome)]
    if divergentes:
        raise ManifestError(CHANGE_NOT_ISOLATED,
                            f"mudança declarada em {componente} mas também "
                            f"diverge em {sorted(divergentes)}")
    if baseline["policy_version"] != candidata["policy_version"]:
        raise ManifestError(CHANGE_NOT_ISOLATED, "policy_version difere dos dois lados")

    # Coerência REGRA × ESCOPO × COMPONENTE: num estudo de seleção a baseline é
    # a decisão observada do champion e a candidata roda o motor do componente
    # declarado; num estudo de gestão, os DOIS lados usam a mesma lista de
    # entradas (nenhum dos lados re-seleciona).
    regra_base = baseline["selection_rule"]["kind"]
    regra_cand = candidata["selection_rule"]["kind"]
    if regra_base != RULE_OBSERVED_CHAMPION:
        raise ManifestError(RULE_SCOPE_MISMATCH,
                            f"baseline.selection_rule deve ser {RULE_OBSERVED_CHAMPION}")
    if escopo == SCOPE_MANAGEMENT:
        if regra_cand != RULE_OBSERVED_CHAMPION:
            raise ManifestError(RULE_SCOPE_MISMATCH,
                                "MANAGEMENT_ONLY não re-seleciona oportunidade")
    else:
        esperada = RULE_BY_COMPONENT.get(componente)
        if esperada is None:
            raise ManifestError(RULE_NOT_IMPLEMENTED,
                                f"sem regra implementada para {componente}")
        if regra_cand != esperada:
            raise ManifestError(RULE_SCOPE_MISMATCH,
                                f"{componente} exige candidate.selection_rule={esperada}")

    hashes = {
        "baseline_hash": _hash(baseline),
        "candidate_hash": _hash(candidata),
        "population_hash": _hash(populacao),
        "costs_hash": _hash(custos),
        "split_hash": _hash(divisao),
    }
    hashes["bundle_hash"] = _hash({**hashes, "comparison_scope": escopo,
                                   "isolated_change": mudanca})
    declarados = corpo["hashes"]
    if declarados is not None:
        conferir = _closed(declarados, HASH_FIELDS, "hashes")
        divergiu = sorted(campo for campo in HASH_FIELDS
                          if str(conferir.get(campo) or "") != hashes[campo])
        if divergiu:
            raise ManifestError(MANIFEST_DRIFT, f"hashes divergentes: {divergiu}")
    normalizado = {
        "manifest_version": MANIFEST_VERSION,
        "study_id": _text(corpo["study_id"], "study_id", limite=120),
        "decision": decisao, "comparison_scope": escopo,
        "population": populacao, "isolated_change": mudanca,
        "baseline": baseline, "candidate": candidata,
        "costs": custos, "split": divisao, "hashes": hashes,
    }
    normalizado["manifest_hash"] = _hash(
        {campo: normalizado[campo] for campo in MANIFEST_FIELDS})
    return normalizado


#: Bloco de SELEÇÃO compartilhado por produtor (pedido/export), contrato do
#: estudo e catálogo. UMA função valida os três — não há cópia de regra.
SELECTION_CONFIG_FIELDS = ("core_version", "core_config_hash", "score_version",
                           "score_config_hash", "playbooks", "selection_rule")
SELECTION_CONFIG_INVALID = "SELECTION_CONFIG_INVALID"


def validate_selection_config(raw: Any) -> Dict[str, Any]:
    """Valida o bloco de motores de seleção da candidata (schema fechado)."""
    try:
        corpo = _closed(raw, SELECTION_CONFIG_FIELDS, "selection_config")
        saida = {campo: _text(corpo[campo], f"selection_config.{campo}")
                 for campo in ("core_version", "core_config_hash",
                               "score_version", "score_config_hash")}
        saida["playbooks"] = list(_tuple_of_text(corpo["playbooks"],
                                                "selection_config.playbooks"))
        saida["selection_rule"] = _parse_rule(corpo["selection_rule"],
                                             "selection_config.selection_rule")
    except ManifestError as exc:
        return {"ok": False, "reason_code": SELECTION_CONFIG_INVALID,
                "detail": f"{exc.reason_code}: {exc.detail}"}
    return {"ok": True, "reason_code": OK, "detail": None, "config": saida}


def selection_config_of(side: Any) -> Optional[Dict[str, Any]]:
    """Extrai, de um LADO do manifesto, o bloco de seleção canônico."""
    if not isinstance(side, Mapping):
        return None
    corpo = {campo: side.get(campo) for campo in SELECTION_CONFIG_FIELDS}
    verdict = validate_selection_config(corpo)
    return verdict["config"] if verdict["ok"] else None


def manifest_state(manifest: Any) -> str:
    """Estado DERIVADO da decisão registrada — nunca de um booleano no payload."""
    if not isinstance(manifest, Mapping):
        return STATE_BLOCKED
    estado = str(((manifest.get("decision") or {}) if isinstance(
        manifest.get("decision"), Mapping) else {}).get("state") or "")
    if estado == DECISION_APPROVED:
        return STATE_APPROVED
    if estado == DECISION_TEST_ONLY:
        return STATE_TEST_ONLY
    if estado == DECISION_DRAFT:
        return STATE_DRAFT
    return STATE_BLOCKED


def verify_manifest(manifest: Any, *, expected_hash: Optional[str] = None
                    ) -> Dict[str, Any]:
    """Reconfere um manifesto JÁ normalizado: corpo, hashes e drift.

    Reutiliza o MESMO parser do produtor — adulterar qualquer campo muda o
    `manifest_hash` e a conferência recusa antes de qualquer métrica.
    """
    if not isinstance(manifest, Mapping):
        return {"ok": False, "reason_code": NOT_MAPPING, "state": STATE_BLOCKED}
    corpo = {campo: manifest.get(campo) for campo in MANIFEST_FIELDS}
    try:
        refeito = parse_manifest(corpo)
    except ManifestError as exc:
        return {"ok": False, "reason_code": exc.reason_code,
                "detail": exc.detail, "state": STATE_BLOCKED}
    declarado = str(manifest.get("manifest_hash") or "")
    if declarado and declarado != refeito["manifest_hash"]:
        return {"ok": False, "reason_code": MANIFEST_DRIFT,
                "detail": "manifest_hash", "state": STATE_BLOCKED}
    if expected_hash is not None and str(expected_hash) != refeito["manifest_hash"]:
        return {"ok": False, "reason_code": MANIFEST_DRIFT,
                "detail": "expected_hash", "state": STATE_BLOCKED}
    return {"ok": True, "reason_code": OK, "state": manifest_state(refeito),
            "manifest": refeito, "manifest_hash": refeito["manifest_hash"]}


def authorized_comparison(manifest: Any = None) -> Dict[str, Any]:
    """Par AUTORIZADO — ou a ausência dele, explícita e com a decisão que falta.

    Sem manifesto (o caso de hoje) devolve `BLOCKED_MISSING_DECISION`. Com
    manifesto `TEST_ONLY`, a engenharia roda mas o estudo REAL continua
    bloqueado: `real_study_allowed=False`.
    """
    if manifest is None:
        return {"available": False, "state": STATE_BLOCKED,
                "reason_code": "AUTHORIZED_CANDIDATE_NOT_DECLARED",
                "real_study_allowed": False,
                "decision_required": list(DECISION_REQUIRED)}
    verdict = verify_manifest(manifest)
    if not verdict["ok"]:
        return {"available": False, "state": STATE_BLOCKED,
                "reason_code": verdict["reason_code"],
                "detail": verdict.get("detail"), "real_study_allowed": False,
                "decision_required": list(DECISION_REQUIRED)}
    estado = verdict["state"]
    if estado in (STATE_DRAFT, STATE_BLOCKED):
        return {"available": False, "state": STATE_BLOCKED,
                "reason_code": "AUTHORIZED_CANDIDATE_NOT_DECLARED",
                "real_study_allowed": False,
                "decision_required": list(DECISION_REQUIRED)}
    aprovado = estado == STATE_APPROVED
    manifesto = verdict["manifest"]
    return {
        "available": True, "state": estado,
        "reason_code": OK if aprovado else "TEST_ONLY_MANIFEST",
        "real_study_allowed": aprovado,
        "study_id": manifesto["study_id"],
        "comparison_scope": manifesto["comparison_scope"],
        "isolated_change": dict(manifesto["isolated_change"]),
        "manifest_hash": manifesto["manifest_hash"],
        "bundle_hash": manifesto["hashes"]["bundle_hash"],
        "decision": dict(manifesto["decision"]),
        "decision_required": [] if aprovado else list(DECISION_REQUIRED),
    }


def load_manifest_file(path: Any) -> Dict[str, Any]:
    """Lê o manifesto de um arquivo EXPLÍCITO (nunca de ENV/descoberta).

    Arquivo é insumo aprovado pelo usuário; o caminho vem do comando. Erro de
    leitura/JSON devolve recusa com motivo — não um manifesto parcial.
    """
    from pathlib import Path
    try:
        texto = Path(str(path)).read_text(encoding="utf-8")
        bruto = json.loads(texto)
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason_code": "MANIFEST_FILE_UNREADABLE",
                "detail": type(exc).__name__, "state": STATE_BLOCKED}
    try:
        manifesto = parse_manifest(bruto)
    except ManifestError as exc:
        return {"ok": False, "reason_code": exc.reason_code,
                "detail": exc.detail, "state": STATE_BLOCKED}
    return {"ok": True, "reason_code": OK, "state": manifest_state(manifesto),
            "manifest": manifesto, "manifest_hash": manifesto["manifest_hash"]}


def manifest_summary(manifest: Any) -> Dict[str, Any]:
    """Resumo para catálogo/relatório: identidade e hashes, sem resultados."""
    verdict = verify_manifest(manifest)
    if not verdict["ok"]:
        return {"state": STATE_BLOCKED, "reason_code": verdict["reason_code"]}
    manifesto = verdict["manifest"]
    return {
        "state": verdict["state"], "reason_code": OK,
        "study_id": manifesto["study_id"],
        "comparison_scope": manifesto["comparison_scope"],
        "component_changed": manifesto["isolated_change"]["component"],
        "population": {"universe_version": manifesto["population"]["universe_version"],
                       "cohort": manifesto["population"]["cohort"],
                       "scope_id": manifesto["population"]["scope_id"]},
        "costs": {"source": manifesto["costs"]["source"],
                  "availability": manifesto["costs"]["availability"]},
        "hashes": dict(manifesto["hashes"]),
        "manifest_hash": manifesto["manifest_hash"],
        "outcomes_consulted": False,
        "hashes_generated_before_results": True,
    }
