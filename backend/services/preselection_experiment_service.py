"""R12 — experimento de PRÉ-SELEÇÃO sobre o catálogo P05 que já existe.

Não há segundo catálogo, bus ou painel de promoção: a linha continua sendo
`StrategyExperiment`, com o mesmo ciclo de vida, o mesmo lock e o mesmo índice
único que garante UM challenger em SHADOW. O que muda é o TIPO versionado do
experimento, gravado dentro de `candidate_config` — aditivo, sem DDL.

Os contratos NÃO são intercambiáveis: o P05 compara a mesma recomendação
pós-seleção; a estratégia estrutural precisa da coorte pré-seleção. Misturar as
duas populações seria comparar coisas diferentes, então o tipo viaja junto de
toda decisão e o comparador de um tipo recusa o outro.

Bloqueios antigos continuam valendo (`P051_ANALYTICS_ONLY` entre eles): nada
aqui libera experimento legado como efeito colateral.

Este módulo é PURO: decide e descreve, não escreve no banco, não emite ordem,
não promove, não abre canário e não limpa quarentena nem incidente.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

R12_VERSION = "R12_PRE_SELECTION_EXPERIMENT_V1"
CONTRACT = "CANDIDATE_POLICY"
MODE_ENV = "R12_PRE_SELECTION_MODE"
MODE_INACTIVE = "inactive"
MODE_SIMULATION = "simulation"

TYPE_KEY = "experiment_type"
TYPE_VERSION_KEY = "experiment_type_version"
TYPE_PRE_SELECTION = "PRE_SELECTION_STRUCTURAL"
TYPE_POST_SELECTION = "POST_SELECTION_KNOB"
TYPE_VERSION = "R12_PRE_SELECTION_V1"
#: Bloqueio herdado do P05.1 — permanece intocado.
P051_BLOCK_REASON = "P051_ANALYTICS_ONLY"

# ── Motivos ─────────────────────────────────────────────────────────────────
OK = "OK"
TYPE_MISMATCH = "EXPERIMENT_TYPE_MISMATCH"
CHALLENGER_ALREADY_ACTIVE = "CHALLENGER_ALREADY_ACTIVE"
LAB_SEQUENTIAL_ONLY = "LAB_SEQUENTIAL_ONLY"
CHAMPION_DRIFT = "CHAMPION_DRIFT"
CONFIG_CHANGED = "CONFIG_CHANGED"
P03_INCIDENT_OPEN = "P03_INCIDENT_OPEN"
COVERAGE_INSUFFICIENT = "COVERAGE_INSUFFICIENT"
FROZEN_BUNDLE_MISSING = "FROZEN_BUNDLE_MISSING"
INVALID_TRANSITION = "INVALID_TRANSITION"
AA_ARTIFACT = "AA_ARTIFACT"
SAMPLE_INSUFFICIENT = "SAMPLE_INSUFFICIENT"
PLAYBOOK_SAMPLE_INSUFFICIENT = "PLAYBOOK_SAMPLE_INSUFFICIENT"
DURATION_INSUFFICIENT = "DURATION_INSUFFICIENT"
BUSINESS_DAYS_INSUFFICIENT = "BUSINESS_DAYS_INSUFFICIENT"
EV_INSUFFICIENT = "EV_INSUFFICIENT"
UNCERTAINTY_TOO_HIGH = "UNCERTAINTY_TOO_HIGH"
DRAWDOWN_EXCEEDED = "DRAWDOWN_EXCEEDED"
STABILITY_INSUFFICIENT = "STABILITY_INSUFFICIENT"
OPERATIONAL_FAILURES = "OPERATIONAL_FAILURES"
ECONOMIC_DUPLICATE = "ECONOMIC_DUPLICATE"
PROTECTION_FAILURE_OPEN = "PROTECTION_FAILURE_OPEN"
ESSENTIAL_GAP = "ESSENTIAL_GAP"
FIDELITY_DISCREPANCY = "FIDELITY_DISCREPANCY"
LIMIT_INCREASE_FORBIDDEN = "LIMIT_INCREASE_FORBIDDEN"
DESTRUCTIVE_DDL_FORBIDDEN = "DESTRUCTIVE_DDL_FORBIDDEN"
INVALID_DATA_RESTORE_FORBIDDEN = "INVALID_DATA_RESTORE_FORBIDDEN"
SECURITY_REGRESSION_FORBIDDEN = "SECURITY_REGRESSION_FORBIDDEN"
SIMULATION_NOT_REAL_APPROVAL = "SIMULATION_NOT_REAL_APPROVAL"
REASON_CODES = frozenset({
    OK, TYPE_MISMATCH, CHALLENGER_ALREADY_ACTIVE, LAB_SEQUENTIAL_ONLY, CHAMPION_DRIFT,
    CONFIG_CHANGED, P03_INCIDENT_OPEN, COVERAGE_INSUFFICIENT, FROZEN_BUNDLE_MISSING,
    INVALID_TRANSITION, AA_ARTIFACT, SAMPLE_INSUFFICIENT, PLAYBOOK_SAMPLE_INSUFFICIENT,
    DURATION_INSUFFICIENT, BUSINESS_DAYS_INSUFFICIENT, EV_INSUFFICIENT,
    UNCERTAINTY_TOO_HIGH, DRAWDOWN_EXCEEDED, STABILITY_INSUFFICIENT,
    OPERATIONAL_FAILURES, ECONOMIC_DUPLICATE, PROTECTION_FAILURE_OPEN, ESSENTIAL_GAP,
    FIDELITY_DISCREPANCY, LIMIT_INCREASE_FORBIDDEN, DESTRUCTIVE_DDL_FORBIDDEN,
    INVALID_DATA_RESTORE_FORBIDDEN, SECURITY_REGRESSION_FORBIDDEN,
    P051_BLOCK_REASON, SIMULATION_NOT_REAL_APPROVAL,
})

#: Mesmo ciclo do P05 — sem saltos e sem reabertura silenciosa.
TRANSITIONS = {
    "DRAFT": ("INSUFFICIENT_DATA", "REJECTED", "OFFLINE_VALIDATED"),
    "OFFLINE_VALIDATED": ("SHADOW",),
    "SHADOW": ("REJECTED", "ELIGIBLE"),
    "INSUFFICIENT_DATA": (),
    "REJECTED": (),
    "ELIGIBLE": (),
}

LIMITATIONS = [
    "Simulação prospectiva não é aprovação de LIVE.",
    "Aprovar no teste não aprova na simulação real.",
    "Amostra mínima não substitui EV, incerteza, drawdown e estabilidade.",
    "Manifest de canário descreve; não aplica nada.",
    "Limites do projeto são tetos: o vigente mais restritivo prevalece.",
]


def selected_mode() -> str:
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_SIMULATION if value == MODE_SIMULATION else MODE_INACTIVE


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode("utf-8")).hexdigest()


# ── Tipo do experimento ─────────────────────────────────────────────────────
def experiment_type(candidate_config: Any) -> str:
    """Tipo do experimento. Config legada continua sendo pós-seleção."""
    if not isinstance(candidate_config, Mapping):
        return TYPE_POST_SELECTION
    value = candidate_config.get(TYPE_KEY)
    return TYPE_PRE_SELECTION if value == TYPE_PRE_SELECTION else TYPE_POST_SELECTION


def is_pre_selection(candidate_config: Any) -> bool:
    return experiment_type(candidate_config) == TYPE_PRE_SELECTION


def tag_config(candidate_config: Mapping[str, Any]) -> Dict[str, Any]:
    """Marca o tipo SEM apagar nada da configuração existente."""
    if not isinstance(candidate_config, Mapping):
        raise ValueError("candidate_config: objeto obrigatório")
    return {**dict(candidate_config), TYPE_KEY: TYPE_PRE_SELECTION,
            TYPE_VERSION_KEY: TYPE_VERSION}


def comparator_guard(*, expected_type: str, candidate_config: Any) -> Dict[str, Any]:
    """Comparador de um tipo RECUSA o outro: as populações são diferentes."""
    found = experiment_type(candidate_config)
    if found != expected_type:
        return {"ok": False, "reason_code": TYPE_MISMATCH,
                "expected": expected_type, "found": found,
                "interchangeable": False}
    return {"ok": True, "reason_code": OK, "expected": expected_type,
            "found": found, "interchangeable": False}


def legacy_blocks() -> Dict[str, Any]:
    """Bloqueios antigos preservados — nada é liberado em massa."""
    return {"p051_analytics_only": P051_BLOCK_REASON,
            "released_by_this_batch": [],
            "new_type_bypasses_legacy_blocks": False}


# ── Exclusividade do ciclo oficial ──────────────────────────────────────────
OFFICIAL_ACTIVE_STATUSES = ("SHADOW",)


def exclusivity_verdict(active_rows: Sequence[Mapping[str, Any]],
                        *, candidate_key: str) -> Dict[str, Any]:
    """UM challenger prospectivo no ciclo oficial; o resto é laboratório."""
    active = [row for row in (active_rows or ())
              if (row or {}).get("status") in OFFICIAL_ACTIVE_STATUSES]
    mine = [row for row in active if row.get("experiment_key") == candidate_key]
    if mine:
        # Repetir a chamada não duplica nem reabre nada.
        return {"allowed": True, "idempotent": True, "reason_code": OK,
                "active_keys": [row.get("experiment_key") for row in active]}
    if active:
        return {"allowed": False, "idempotent": False,
                "reason_code": CHALLENGER_ALREADY_ACTIVE,
                "lab_alternative": LAB_SEQUENTIAL_ONLY,
                "active_keys": [row.get("experiment_key") for row in active]}
    return {"allowed": True, "idempotent": False, "reason_code": OK, "active_keys": []}


def transition_verdict(current: str, target: str) -> Dict[str, Any]:
    """Sem saltos e sem reabertura silenciosa; repetir o estado é idempotente."""
    if current == target:
        return {"allowed": True, "idempotent": True, "reason_code": OK}
    if target in TRANSITIONS.get(current, ()):
        return {"allowed": True, "idempotent": False, "reason_code": OK}
    return {"allowed": False, "idempotent": False, "reason_code": INVALID_TRANSITION}


# ── Congelamento ────────────────────────────────────────────────────────────
FROZEN_PARTS = ("baseline", "candidate", "config", "costs", "protections")


def freeze_bundle(parts: Mapping[str, Any]) -> Dict[str, Any]:
    """Congela baseline, candidato, configuração, custos e proteções."""
    if not isinstance(parts, Mapping):
        raise ValueError("bundle: objeto obrigatório")
    missing = [name for name in FROZEN_PARTS if parts.get(name) is None]
    if missing:
        return {"frozen": False, "reason_code": FROZEN_BUNDLE_MISSING,
                "missing": missing, "bundle_hash": None}
    payload = {name: parts[name] for name in FROZEN_PARTS}
    return {"frozen": True, "reason_code": OK, "missing": [],
            "bundle_hash": _hash(payload), "parts": list(FROZEN_PARTS)}


def advance_verdict(*, frozen_bundle_hash: Optional[str],
                    current_bundle_hash: Optional[str],
                    champion_hash: Optional[str], evaluated_champion_hash: Optional[str],
                    open_p03_incidents: int, coverage_pct: Optional[float],
                    min_coverage_pct: float = 90.0) -> Dict[str, Any]:
    """Drift, incidente, cobertura ou config alterada impedem avançar.

    Incidente NÃO é limpo aqui: ele apenas bloqueia.
    """
    reasons: List[str] = []
    if not frozen_bundle_hash or not current_bundle_hash:
        reasons.append(FROZEN_BUNDLE_MISSING)
    elif frozen_bundle_hash != current_bundle_hash:
        reasons.append(CONFIG_CHANGED)
    if not champion_hash or not evaluated_champion_hash:
        reasons.append(CHAMPION_DRIFT)
    elif champion_hash != evaluated_champion_hash:
        reasons.append(CHAMPION_DRIFT)
    incidents = _int(open_p03_incidents)
    if incidents is None or incidents > 0:
        reasons.append(P03_INCIDENT_OPEN)
    coverage = _finite(coverage_pct)
    if coverage is None or coverage < min_coverage_pct:
        reasons.append(COVERAGE_INSUFFICIENT)
    codes = tuple(dict.fromkeys(reasons))
    return {"may_advance": not codes, "reason_codes": codes or (OK,),
            "incident_cleared": False, "quarantine_released": False}


# ── Teste A/A ───────────────────────────────────────────────────────────────
def aa_test(arm_a: Sequence[float], arm_b: Sequence[float], *,
            tolerance_r: float = 1e-9) -> Dict[str, Any]:
    """A/A: mesma configuração dos dois lados não pode gerar diferença."""
    a = [v for v in (_finite(x) for x in (arm_a or ())) if v is not None]
    b = [v for v in (_finite(x) for x in (arm_b or ())) if v is not None]
    if not a or not b or len(a) != len(b):
        return {"clean": False, "reason_code": SAMPLE_INSUFFICIENT, "delta": None}
    delta = sum(a) / len(a) - sum(b) / len(b)
    if abs(delta) > tolerance_r:
        return {"clean": False, "reason_code": AA_ARTIFACT, "delta": delta}
    return {"clean": True, "reason_code": OK, "delta": delta}


# ── Gate go/no-go ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GoNoGoCriteria:
    """Critérios CONGELADOS. Tetos do projeto convivem com pisos do P05:
    quando os dois falam do mesmo eixo, vale o mais conservador."""
    min_total_shadow_trades: int = 100
    min_trades_per_playbook: int = 30
    min_calendar_days: int = 14
    min_business_days: int = 10
    min_coverage_pct: float = 90.0
    min_net_ev_r: float = 0.05
    max_uncertainty_r: float = 0.05
    max_drawdown_r: float = 8.0
    min_stability_ratio: float = 0.5
    max_operational_failures: int = 0
    max_fidelity_discrepancy_pct: float = 5.0

    def __post_init__(self) -> None:
        for spec in fields(self):
            value = getattr(self, spec.name)
            if _finite(value) is None or value < 0:
                raise ValueError(f"{spec.name}: número não negativo obrigatório")

    def as_dict(self) -> Dict[str, Any]:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}

    def criteria_hash(self) -> str:
        return _hash({"version": R12_VERSION, "criteria": self.as_dict()})


def business_days(start: Any, end: Any) -> Optional[int]:
    """Dias ÚTEIS completos entre duas datas UTC; entrada inválida ⇒ None."""
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    if start.tzinfo is None or end.tzinfo is None or end < start:
        return None
    days = 0
    cursor = start.astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc).date()
    while cursor < last:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def go_no_go(evidence: Mapping[str, Any], *,
             criteria: Optional[GoNoGoCriteria] = None) -> Dict[str, Any]:
    """Veredicto do gate futuro. Nada aqui aprova LIVE — só diz se FALTA algo."""
    crit = criteria or GoNoGoCriteria()
    evidence = evidence if isinstance(evidence, Mapping) else {}
    reasons: List[str] = []

    total = _int(evidence.get("total_shadow_trades"))
    if total is None or total < crit.min_total_shadow_trades:
        reasons.append(SAMPLE_INSUFFICIENT)
    per_playbook = evidence.get("trades_per_playbook")
    enabled = evidence.get("enabled_playbooks") or ()
    if not isinstance(per_playbook, Mapping) or not enabled:
        reasons.append(PLAYBOOK_SAMPLE_INSUFFICIENT)
    else:
        for playbook in enabled:
            count = _int(per_playbook.get(playbook))
            if count is None or count < crit.min_trades_per_playbook:
                reasons.append(PLAYBOOK_SAMPLE_INSUFFICIENT)
                break
    days = _finite(evidence.get("calendar_days"))
    if days is None or days < crit.min_calendar_days:
        reasons.append(DURATION_INSUFFICIENT)
    working = _int(evidence.get("business_days"))
    if working is None or working < crit.min_business_days:
        reasons.append(BUSINESS_DAYS_INSUFFICIENT)
    coverage = _finite(evidence.get("coverage_pct"))
    if coverage is None or coverage < crit.min_coverage_pct:
        reasons.append(COVERAGE_INSUFFICIENT)
    net_ev = _finite(evidence.get("net_ev_r"))
    if net_ev is None or net_ev < crit.min_net_ev_r:
        reasons.append(EV_INSUFFICIENT)
    uncertainty = _finite(evidence.get("uncertainty_r"))
    if uncertainty is None or uncertainty > crit.max_uncertainty_r:
        reasons.append(UNCERTAINTY_TOO_HIGH)
    drawdown = _finite(evidence.get("drawdown_r"))
    if drawdown is None or drawdown > crit.max_drawdown_r:
        reasons.append(DRAWDOWN_EXCEEDED)
    stability = _finite(evidence.get("stability_ratio"))
    if stability is None or stability < crit.min_stability_ratio:
        reasons.append(STABILITY_INSUFFICIENT)
    failures = _int(evidence.get("operational_failures"))
    if failures is None or failures > crit.max_operational_failures:
        reasons.append(OPERATIONAL_FAILURES)
    duplicates = _int(evidence.get("economic_duplicates"))
    if duplicates is None or duplicates > 0:
        reasons.append(ECONOMIC_DUPLICATE)
    protection = _int(evidence.get("unresolved_protection_failures"))
    if protection is None or protection > 0:
        reasons.append(PROTECTION_FAILURE_OPEN)
    if evidence.get("essential_gaps"):
        reasons.append(ESSENTIAL_GAP)
    discrepancy = _finite(evidence.get("fidelity_discrepancy_pct"))
    if discrepancy is None or discrepancy > crit.max_fidelity_discrepancy_pct:
        reasons.append(FIDELITY_DISCREPANCY)

    codes = tuple(dict.fromkeys(reasons))
    passed = not codes
    return {
        "verdict": "GO_CANDIDATE" if passed else "NO_GO",
        "reason_codes": codes or (OK,),
        "criteria_hash": crit.criteria_hash(),
        "criteria": crit.as_dict(),
        # Passar no gate NÃO aprova LIVE nem dispensa simulação real.
        "live_approval": "UNAVAILABLE",
        "requires_human_authorization": True,
        "test_approval_is_not_simulation_approval": SIMULATION_NOT_REAL_APPROVAL,
    }


# ── Manifest de canário (sem aplicar) ───────────────────────────────────────
def canary_limits(current: Mapping[str, Any],
                  project_ceiling: Mapping[str, Any],
                  proposed: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Teto do projeto NÃO é permissão para subir o vigente: vale o menor."""
    current = current if isinstance(current, Mapping) else {}
    ceiling = project_ceiling if isinstance(project_ceiling, Mapping) else {}
    proposed = proposed if isinstance(proposed, Mapping) else {}
    resolved: Dict[str, Any] = {}
    violations: List[str] = []
    for key in sorted(set(current) | set(ceiling)):
        now = _finite(current.get(key))
        cap = _finite(ceiling.get(key))
        if now is None or cap is None:
            violations.append(key)
            continue
        limit = min(now, cap)
        want = _finite(proposed.get(key))
        if want is not None and want > limit:
            violations.append(key)
            continue
        resolved[key] = want if want is not None else limit
    if violations:
        return {"ok": False, "reason_code": LIMIT_INCREASE_FORBIDDEN,
                "violations": sorted(violations), "limits": resolved}
    return {"ok": True, "reason_code": OK, "violations": [], "limits": resolved}


def canary_manifest(*, version: str, diff: Sequence[Mapping[str, Any]],
                    evidence: Mapping[str, Any], gate: Mapping[str, Any],
                    preconditions: Sequence[str], approvers: Sequence[str],
                    limits: Mapping[str, Any], rollback: Mapping[str, Any]) -> Dict[str, Any]:
    """Descreve o canário — e NÃO aplica nada."""
    return {
        "r12_version": R12_VERSION,
        "manifest_version": version,
        "applied": False,
        "apply_endpoint": None,
        "diff": [dict(item) for item in (diff or ())],
        "evidence": dict(evidence or {}),
        "gate": {"verdict": (gate or {}).get("verdict"),
                 "criteria_hash": (gate or {}).get("criteria_hash"),
                 "reason_codes": list((gate or {}).get("reason_codes") or ())},
        "preconditions": list(preconditions or ()),
        "approvers": list(approvers or ()),
        "limits": dict(limits or {}),
        "rollback": dict(rollback or {}),
        "live_approval": "UNAVAILABLE",
        "requires_human_authorization": True,
        "limitations": list(LIMITATIONS),
    }


# ── Ensaio local de rollback ────────────────────────────────────────────────
PRESERVED_ON_ROLLBACK = ("open_positions", "protections", "entry_intents",
                         "execution_ledgers", "history", "incidents")


def rollback_rehearsal(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Ensaio LOCAL: reverter política/config sem destruir nada."""
    plan = plan if isinstance(plan, Mapping) else {}
    reasons: List[str] = []
    if plan.get("destructive_ddl"):
        reasons.append(DESTRUCTIVE_DDL_FORBIDDEN)
    if plan.get("restores_invalid_data"):
        reasons.append(INVALID_DATA_RESTORE_FORBIDDEN)
    if plan.get("reintroduces_security_fix_removal"):
        reasons.append(SECURITY_REGRESSION_FORBIDDEN)
    preserved = plan.get("preserves") if isinstance(plan.get("preserves"), Mapping) else {}
    missing = [name for name in PRESERVED_ON_ROLLBACK if preserved.get(name) is not True]
    if missing:
        reasons.append(FROZEN_BUNDLE_MISSING)
    codes = tuple(dict.fromkeys(reasons))
    return {"rehearsed": True, "applied": False, "ok": not codes,
            "reason_codes": codes or (OK,), "missing_preservation": missing,
            "preserved": list(PRESERVED_ON_ROLLBACK),
            "cancels_simulation_only": True}


# ── Relatório ───────────────────────────────────────────────────────────────
def status_report(*, implementation: Mapping[str, Any], evidence: Mapping[str, Any],
                  gate: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Três eixos SEPARADOS: implementação, evidência e aprovação LIVE."""
    gate = gate or {}
    return {
        "r12_version": R12_VERSION,
        "implementation_status": dict(implementation or {}),
        "evidence_status": dict(evidence or {}),
        "live_approval": {"state": "UNAVAILABLE",
                          "reason_code": (gate.get("reason_codes") or (OK,))[0]
                          if gate else SIMULATION_NOT_REAL_APPROVAL,
                          "requires_human_authorization": True,
                          "canary_applied": False},
        "limitations": list(LIMITATIONS),
    }


def r12_manifest() -> Dict[str, Any]:
    return {
        "r12_version": R12_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "experiment_types": {"pre_selection": TYPE_PRE_SELECTION,
                             "post_selection": TYPE_POST_SELECTION,
                             "type_version": TYPE_VERSION,
                             "interchangeable": False},
        "catalog": {"table": "strategy_experiments", "second_catalog": False,
                    "second_bus": False, "second_promotion_panel": False},
        "exclusivity": {"official_active_statuses": list(OFFICIAL_ACTIVE_STATUSES),
                        "one_active_challenger": True,
                        "others": LAB_SEQUENTIAL_ONLY},
        "legacy_blocks": legacy_blocks(),
        "criteria": GoNoGoCriteria().as_dict(),
        "criteria_hash": GoNoGoCriteria().criteria_hash(),
        "endpoints_added": [],
        "forbidden_endpoints": ["execute-now", "promote", "enable-live",
                                "clear-quarantine"],
        "reason_codes": sorted(REASON_CODES),
        "limitations": list(LIMITATIONS),
    }
