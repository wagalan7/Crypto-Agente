"""R10B — escopos do exportador somente leitura (aceitas, vetadas, estruturais).

Descritores PUROS: cada escopo diz de qual acervo vem, qual filtro SQL o
delimita, quais campos existem de verdade e quais faltam — com reason code — e
se ele tem trajetória. O escopo legado (`R09_REJECTED_POST_SELECTION`) continua
gerando exatamente o mesmo SQL de antes: export antigo segue válido.

Nada aqui abre sessão, lê ENV ou executa consulta: são textos e contratos.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

REJECTED_TABLE = "rejected_setup_observations"
OPPORTUNITY_TABLE = "decision_observations"

SCOPE_REJECTED_POST = "R09_REJECTED_POST_SELECTION"
SCOPE_PRE_VETOED = "R09_PRE_SELECTION_VETOED"
SCOPE_PRE_ACCEPTED = "R09_PRE_SELECTION_ACCEPTED"
SCOPE_STRUCTURAL = "R10_STRUCTURAL_CANDIDATES"
DEFAULT_SCOPE = SCOPE_REJECTED_POST

# Motivos de campo ausente — lacuna histórica declarada, nunca preenchida com 0.
TRAJECTORY_NOT_COLLECTED = "TRAJECTORY_NOT_COLLECTED_FOR_ACCEPTED"
OUTCOME_NOT_COLLECTED = "OUTCOME_NOT_COLLECTED_FOR_ACCEPTED"
COST_NOT_OBSERVED = "COST_NOT_OBSERVED_IN_SOURCE"
UNIVERSE_NOT_POINT_IN_TIME = "UNIVERSE_SNAPSHOT_NOT_POINT_IN_TIME"
FUNNEL_ONLY_IN_PRE_SELECTION = "FUNNEL_ONLY_IN_PRE_SELECTION"

_PRE_MARK = "'r09_pre_selection'"


def _pre_filter(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"({prefix}frozen_config -> {_PRE_MARK} ->> 'scope') = 'PRE_SELECTION'"


def _structural_filter(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (f"{_pre_filter(alias)} AND "
            f"({prefix}frozen_config -> {_PRE_MARK} -> 'setup' ->> 'playbook') IS NOT NULL")


@dataclass(frozen=True)
class Scope:
    scope_id: str
    cohort: str
    source_table: str
    decision_column: str
    opportunity_scope: str
    unit: str
    exports_trajectory: bool
    comparable_with_r10a: bool
    index_filter: str = ""
    detail_filter: str = ""
    absent_fields: Tuple[Tuple[str, str], ...] = ()
    tables: Tuple[str, ...] = (REJECTED_TABLE, OPPORTUNITY_TABLE)

    def absent_map(self) -> Dict[str, str]:
        return dict(self.absent_fields)

    def manifest(self) -> Dict[str, Any]:
        return {"scope": self.scope_id, "cohort": self.cohort,
                "tables": list(self.tables), "unit": self.unit,
                "decision_time": f"{self.source_table}.{self.decision_column}",
                "exports_trajectory": self.exports_trajectory,
                "comparable_with_r10a": self.comparable_with_r10a,
                "absent_fields": self.absent_map()}


LEGACY = Scope(
    scope_id=SCOPE_REJECTED_POST,
    cohort="R09_REJECTED_POST_SELECTION",
    source_table=REJECTED_TABLE,
    decision_column="decision_at",
    opportunity_scope="POST_SELECTION",
    unit="ONE_REJECTED_OPPORTUNITY",
    exports_trajectory=True,
    comparable_with_r10a=True,
    absent_fields=(("funnel", FUNNEL_ONLY_IN_PRE_SELECTION),
                   ("observed_costs", COST_NOT_OBSERVED)),
)

PRE_VETOED = Scope(
    scope_id=SCOPE_PRE_VETOED,
    cohort="R09_PRE_SELECTION_VETOED",
    source_table=REJECTED_TABLE,
    decision_column="decision_at",
    opportunity_scope="PRE_SELECTION",
    unit="ONE_PRE_SELECTION_VETOED_OPPORTUNITY",
    exports_trajectory=True,
    comparable_with_r10a=True,
    index_filter=_pre_filter(),
    detail_filter=_pre_filter("r"),
    absent_fields=(("observed_costs", COST_NOT_OBSERVED),
                   ("universe_snapshot", UNIVERSE_NOT_POINT_IN_TIME)),
)

STRUCTURAL = Scope(
    scope_id=SCOPE_STRUCTURAL,
    cohort="R10_STRUCTURAL_CANDIDATES",
    source_table=REJECTED_TABLE,
    decision_column="decision_at",
    opportunity_scope="PRE_SELECTION",
    unit="ONE_STRUCTURAL_CANDIDATE",
    exports_trajectory=True,
    comparable_with_r10a=True,
    index_filter=_structural_filter(),
    detail_filter=_structural_filter("r"),
    absent_fields=(("observed_costs", COST_NOT_OBSERVED),
                   ("universe_snapshot", UNIVERSE_NOT_POINT_IN_TIME)),
)

PRE_ACCEPTED = Scope(
    scope_id=SCOPE_PRE_ACCEPTED,
    cohort="R09_PRE_SELECTION_ACCEPTED",
    source_table=OPPORTUNITY_TABLE,
    decision_column="first_decision_observed_at",
    opportunity_scope="PRE_SELECTION",
    unit="ONE_PRE_SELECTION_ACCEPTED_OPPORTUNITY",
    # Aceitas não têm trajetória coletada: o acervo guarda a decisão, não o
    # caminho do preço. Exportar mesmo assim seria inventar resultado.
    exports_trajectory=False,
    comparable_with_r10a=False,
    index_filter="scope = 'PRE_SELECTION'",
    detail_filter="o.scope = 'PRE_SELECTION'",
    absent_fields=(("candles", TRAJECTORY_NOT_COLLECTED),
                   ("outcome", OUTCOME_NOT_COLLECTED),
                   ("observed_costs", COST_NOT_OBSERVED)),
    tables=(OPPORTUNITY_TABLE,),
)

SCOPES: Dict[str, Scope] = {scope.scope_id: scope for scope in
                            (LEGACY, PRE_VETOED, STRUCTURAL, PRE_ACCEPTED)}


def resolve(scope_id: Any) -> Scope:
    if scope_id is None:
        return SCOPES[DEFAULT_SCOPE]
    if not isinstance(scope_id, str) or scope_id not in SCOPES:
        raise ValueError(f"scope desconhecido; use um de {sorted(SCOPES)}")
    return SCOPES[scope_id]


# ── SQL por escopo ──────────────────────────────────────────────────────────
# O escopo legado gera o MESMO texto de antes (sem WHERE extra, sem alias novo).
def counts_sql(scope: Scope) -> str:
    column = scope.decision_column
    where = f"\nWHERE {scope.index_filter}" if scope.index_filter else ""
    return f"""
SELECT count(*) FILTER (WHERE {column} < :train_start) AS before_training,
       count(*) FILTER (WHERE {column} >= :holdout_start AND {column} < :as_of) AS holdout_sealed,
       count(*) FILTER (WHERE {column} >= :as_of) AS after_cutoff
FROM {scope.source_table}{where}
"""


def index_sql(scope: Scope) -> str:
    column = scope.decision_column
    select = ("opportunity_key, decision_at" if column == "decision_at"
              else f"opportunity_key, {column} AS decision_at")
    extra = f"\n  AND {scope.index_filter}" if scope.index_filter else ""
    return f"""
SELECT {select}
FROM {scope.source_table}
WHERE {column} >= :train_start AND {column} < :index_end{extra}
ORDER BY {column}, opportunity_key
LIMIT :row_limit
"""


_VALID_TS = ("jsonb_typeof(e.value) = 'object' "
             "AND jsonb_typeof(e.value -> 'timestamp') = 'number' "
             "AND (e.value ->> 'timestamp') ~ '^[0-9]{1,15}$'")


def _trajectory_detail_sql(scope: Scope) -> str:
    extra = f"\nWHERE {scope.detail_filter}" if scope.detail_filter else ""
    return f"""
WITH bounds AS (
    SELECT b.k, b.lo, b.hi
    FROM unnest(CAST(:keys AS text[]), CAST(:los AS bigint[]), CAST(:his AS bigint[])) AS b(k, lo, hi)
)
SELECT r.opportunity_key, r.symbol, r.decision_at, r.version,
       r.frozen_setup, r.frozen_config,
       (o.opportunity_key IS NOT NULL) AS opportunity_found,
       o.symbol AS opportunity_symbol, o.scope AS opportunity_scope,
       CASE WHEN jsonb_typeof(r.candles) = 'array'
            THEN COALESCE((SELECT bool_or(NOT ({_VALID_TS}))
                           FROM jsonb_array_elements(r.candles) AS e(value)), false)
            ELSE true END AS candles_malformed,
       CASE WHEN jsonb_typeof(r.candles) = 'array' THEN (
            SELECT COALESCE(jsonb_agg(w.value ORDER BY w.ts, w.ord), '[]'::jsonb)
            FROM (SELECT e.value, e.ord,
                         CASE WHEN {_VALID_TS} THEN (e.value ->> 'timestamp')::bigint END AS ts
                  FROM jsonb_array_elements(r.candles) WITH ORDINALITY AS e(value, ord)) AS w
            WHERE w.ts IS NOT NULL AND w.ts >= b.lo
              AND w.ts + CAST(:bar_ms AS bigint) <= b.hi)
       ELSE '[]'::jsonb END AS candles
FROM bounds AS b
JOIN {scope.source_table} AS r ON r.opportunity_key = b.k
LEFT JOIN {OPPORTUNITY_TABLE} AS o ON o.opportunity_key = r.opportunity_key{extra}
ORDER BY r.decision_at, r.opportunity_key
"""


def _feature_detail_sql(scope: Scope) -> str:
    column = scope.decision_column
    extra = f"\n  AND {scope.detail_filter}" if scope.detail_filter else ""
    return f"""
SELECT o.opportunity_key, o.symbol, o.{column} AS decision_at,
       o.scope AS opportunity_scope, o.frozen_setup, o.frozen_config, o.score_trace
FROM unnest(CAST(:keys AS text[])) AS b(k)
JOIN {scope.source_table} AS o ON o.opportunity_key = b.k
WHERE true{extra}
ORDER BY o.{column}, o.opportunity_key
"""


def detail_sql(scope: Scope) -> str:
    return (_trajectory_detail_sql(scope) if scope.exports_trajectory
            else _feature_detail_sql(scope))


def detail_needs_window_params(scope: Scope) -> bool:
    """Só a consulta com trajetória recebe janelas (`los`, `his`, `bar_ms`)."""
    return scope.exports_trajectory
