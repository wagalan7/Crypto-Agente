"""R10B: ponte SOMENTE LEITURA entre as vetadas R09 e o comparador R10A.

Núcleo puro (requisição, seleção temporal, conversão, manifesto) separado do
loader, que recebe uma sessão async injetada. Sem ENV, relógio, `.env`,
`init_db`, escrita, cache operacional, exchange ou import do processo LIVE.

Coorte: `R09_REJECTED_POST_SELECTION` — setups vetados depois da seleção.
Não é o mercado, nem as recomendações aceitas, nem o desempenho REAL. O
exportador não roda o comparador: o CLI R10A continua sendo a análise.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from services import offline_replay_service as r10a

EXPORTER_SCHEMA = "R10B_RESEARCH_DATASET_V1"
COHORT = "R09_REJECTED_POST_SELECTION"
SOURCE_SCHEMA = "r09.v1"
SOURCE_POLICY = "ISOLATED_REPLAY_NOT_LIVE"
SOURCE_PRICE = "SNAPSHOT_RESOLVER_WINDOW_UNLABELED"
REJECTED_TABLE = "rejected_setup_observations"
OPPORTUNITY_TABLE = "decision_observations"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
REQUEST_KEYS = ("as_of_utc", "split", "baseline_config", "candidate", "costs", "bootstrap")
CANDIDATE_KEYS = ("candidate_id", "registered_at_ms", "kind", "replay_config")
COST_KEYS = ("fee_bps_per_side", "slippage_bps_per_side", "funding_bps_per_bar")
SPLIT_KEYS = ("train_start_ms", "validation_start_ms", "holdout_start_ms", "purge_bars")
BOOTSTRAP_KEYS = ("seed", "samples", "block_size")
REPLAY_KEYS = tuple(f.name for f in fields(r10a.ReplayConfig) if f.name != "schema_version")
EXCLUSION_REASONS = (
    "SOURCE_CONTRACT_MISMATCH", "CONFIG_MISMATCH", "OPPORTUNITY_ROW_MISSING",
    "IDENTITY_MISMATCH", "INVALID_SETUP", "TEMPORAL_INCONSISTENCY",
    "MALFORMED_CANDLE_JSON", "INVALID_CANDLE_DATA",
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)
LIMITATIONS = [
    "Coorte R09_REJECTED_POST_SELECTION: só vetadas depois da seleção; não é mercado, aceitas nem desempenho REAL.",
    "R09 só acumula velas enquanto há snapshot aberto do símbolo; a cobertura é enviesada por isso.",
    "Fonte de preço da janela do resolver (OKX com fallback Binance) sem rótulo por vela.",
    "Rejeições terminais param de acumular velas: RESOLVED no R09 não garante horizonte para outro candidato.",
    "Horizonte R09 é de pesquisa (velas de 5m), não o time-stop LIVE.",
    "Custos informados são CENÁRIOS declarados, não custos observados da conta; ausência permanece null.",
    "registered_at_ms é fornecido pelo operador: não prova pré-registro independente nem ausência de tuning.",
    "O mesmo cutoff não garante snapshot histórico idêntico: linhas R09 mudam (velas, versão).",
    "O contador holdout_sealed do comparador reflete só o payload (0); o total real está neste manifesto.",
    "Replay OHLCV não é replay do executor LIVE; nenhum resultado aprova estratégia.",
    "decision_ts_ms é o instante da rejeição truncado ao milissegundo.",
]


class DatasetError(ValueError):
    """Requisição ou contrato de origem inválido. Mensagem sem dados sensíveis."""


class DatasetLimitError(DatasetError):
    """Mais linhas elegíveis que o limite do R10A: nunca trunca em silêncio."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_ms(value: datetime) -> int:
    """Piso exato em milissegundos (sem aritmética de float)."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DatasetError("datetime com fuso obrigatório")
    return (value - _EPOCH) // _MS


def ms_datetime(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


def _closed(raw: Any, keys: tuple, name: str) -> dict:
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{name}: objeto obrigatório")
    missing, unknown = set(keys) - set(raw), set(raw) - set(keys)
    if missing or unknown:
        raise DatasetError(f"{name}: chaves obrigatórias {sorted(missing)} / desconhecidas {sorted(unknown)}")
    return dict(raw)


def _replay_config(raw: Any, name: str) -> r10a.ReplayConfig:
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{name}: objeto obrigatório")
    extra = set(raw) - set(REPLAY_KEYS) - {"schema_version"}
    missing = set(REPLAY_KEYS) - set(raw)
    if extra or missing:
        raise DatasetError(f"{name}: todos os campos de replay explícitos, sem defaults")
    try:
        return r10a.ReplayConfig(**dict(raw))
    except (TypeError, ValueError) as exc:
        raise DatasetError(f"{name}: {exc}") from None


def _as_of(raw: Any) -> int:
    if not isinstance(raw, str) or not raw.endswith(("Z", "+00:00")):
        raise DatasetError("as_of_utc: ISO-8601 em UTC (Z ou +00:00) obrigatório")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise DatasetError("as_of_utc inválido") from None
    if value.utcoffset() != timedelta(0) or value.microsecond % 1000:
        raise DatasetError("as_of_utc: UTC com precisão máxima de milissegundo")
    return utc_ms(value)


@dataclass(frozen=True)
class ExportRequest:
    as_of_ms: int
    split: r10a.ChronologicalSplit
    baseline: r10a.ReplayConfig
    candidate: r10a.CandidateRegistration
    costs: r10a.CostConfig
    bootstrap: r10a.BootstrapConfig
    changed: tuple

    @property
    def bar_ms(self) -> int:
        return self.baseline.bar_ms

    @property
    def horizon_bars(self) -> int:
        """Mesmo horizonte máximo que o comparador usa para purgar."""
        return max(c.entry_window_bars + c.max_holding_bars - 1
                   for c in (self.baseline, self.candidate.replay_config))

    def payload_head(self) -> dict:
        """Campos do payload `compare`, exatamente no formato do `run_payload`."""
        return {
            "mode": "compare",
            "baseline_config": asdict(self.baseline),
            "costs": asdict(self.costs),
            "candidate": {"candidate_id": self.candidate.candidate_id,
                          "registered_at_ms": self.candidate.registered_at_ms,
                          "kind": self.candidate.kind,
                          "replay_config": asdict(self.candidate.replay_config)},
            "split": asdict(self.split),
            "bootstrap": asdict(self.bootstrap),
        }

    def normalized(self) -> dict:
        return {"exporter_schema": EXPORTER_SCHEMA, "cohort": COHORT,
                "as_of_ms": self.as_of_ms, **self.payload_head()}

    def request_hash(self) -> str:
        return _sha(self.normalized())


def parse_request(raw: Any) -> ExportRequest:
    """Schema fechado; validadores R10A; nada escolhido pelo resultado."""
    body = _closed(raw, REQUEST_KEYS, "requisição")
    as_of_ms = _as_of(body["as_of_utc"])
    try:
        split = r10a.ChronologicalSplit(**_closed(body["split"], SPLIT_KEYS, "split"))
        costs = r10a.CostConfig(**_closed(body["costs"], COST_KEYS, "costs"))
        bootstrap = r10a.BootstrapConfig(**_closed(body["bootstrap"], BOOTSTRAP_KEYS, "bootstrap"))
    except (TypeError, ValueError) as exc:
        raise DatasetError(str(exc)) from None
    baseline = _replay_config(body["baseline_config"], "baseline_config")
    cand = _closed(body["candidate"], CANDIDATE_KEYS, "candidate")
    if cand["kind"] != "MANAGEMENT_ONLY":
        raise DatasetError("exportador econômico suporta somente MANAGEMENT_ONLY")
    replay = _replay_config(cand["replay_config"], "candidate.replay_config")
    try:
        candidate = r10a.CandidateRegistration(
            candidate_id=cand["candidate_id"], registered_at_ms=cand["registered_at_ms"],
            kind="MANAGEMENT_ONLY", replay_config=replay)
        changed = tuple(r10a.management_diff(baseline, replay))
    except (TypeError, ValueError) as exc:
        raise DatasetError(str(exc)) from None
    if candidate.registered_at_ms > split.train_start_ms:
        raise DatasetError("candidato deve estar registrado antes do início do treino")
    if as_of_ms <= split.train_start_ms:
        raise DatasetError("as_of_utc deve ser posterior ao início do treino")
    return ExportRequest(as_of_ms, split, baseline, candidate, costs, bootstrap, changed)


@dataclass(frozen=True)
class PlannedRow:
    key: str
    decision_ms: int
    split: str
    first_ms: int
    window_end_ms: int
    cutoff_truncated: bool


@dataclass(frozen=True)
class SelectionPlan:
    rows: tuple
    counts: dict

    @property
    def keys(self) -> list:
        return [row.key for row in self.rows]


def plan_selection(request: ExportRequest, index_rows: Iterable) -> SelectionPlan:
    """Só metadados (id, decisão). Purga ANTES de qualquer detalhe/JSON.

    Treino = [train_start, validation_start); validação = [validation_start,
    holdout_start); decisão ≥ holdout ou ≥ as_of nunca chega aqui.
    """
    split, bar = request.split, request.bar_ms
    horizon = request.horizon_bars
    ordered, seen = [], set()
    for key, decided in index_rows:
        if not isinstance(key, str) or not key or len(key) > 128 or key in seen:
            raise DatasetError("índice com identificador inválido ou duplicado")
        seen.add(key)
        ordered.append((utc_ms(decided), key))
    ordered.sort()
    if len(ordered) > r10a.MAX_OPPORTUNITIES:
        raise DatasetLimitError("oportunidades elegíveis acima do limite R10A; estreite a janela")
    counts = {"training_candidates": 0, "validation_candidates": 0, "purged": 0}
    rows = []
    for decision_ms, key in ordered:
        if not split.train_start_ms <= decision_ms < min(split.holdout_start_ms, request.as_of_ms):
            raise DatasetError("índice fora da janela treino/validação ou após o cutoff")
        name = "training" if decision_ms < split.validation_start_ms else "validation"
        counts[f"{name}_candidates"] += 1
        boundary = split.validation_start_ms if name == "training" else split.holdout_start_ms
        first_ms = ((decision_ms + bar - 1) // bar) * bar
        if first_ms + (horizon + split.purge_bars) * bar > boundary:
            counts["purged"] += 1
            continue
        horizon_end = first_ms + horizon * bar
        end = min(horizon_end, boundary, request.as_of_ms)
        rows.append(PlannedRow(key, decision_ms, name, first_ms, end, end < horizon_end))
    return SelectionPlan(tuple(rows), counts)


def _plain_number(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _source_mismatch(config: Any, baseline: r10a.ReplayConfig) -> str | None:
    if not isinstance(config, Mapping):
        return "SOURCE_CONTRACT_MISMATCH"
    if config.get("schema_version") != SOURCE_SCHEMA or config.get("policy") != SOURCE_POLICY:
        return "SOURCE_CONTRACT_MISMATCH"
    for key in REPLAY_KEYS:
        value = config.get(key)
        expected = getattr(baseline, key)
        if not _plain_number(value) or value != expected:
            return "CONFIG_MISMATCH"
    return None


def _setup(row: Mapping, decision_ms: int):
    setup = row.get("frozen_setup")
    if not isinstance(setup, Mapping):
        return None, "INVALID_SETUP"
    if (not row.get("opportunity_found")):
        return None, "OPPORTUNITY_ROW_MISSING"
    symbol = row.get("symbol")
    if (setup.get("symbol") != symbol or row.get("opportunity_symbol") != symbol
            or row.get("opportunity_scope") != "POST_SELECTION"):
        return None, "IDENTITY_MISMATCH"
    close_ms = setup.get("candle_close_ms")
    if close_ms is not None and (not _plain_number(close_ms) or close_ms > decision_ms):
        return None, "TEMPORAL_INCONSISTENCY"
    try:
        opportunity = r10a.Opportunity(
            opportunity_id=row["opportunity_key"], symbol=symbol,
            direction=setup.get("direction"), decision_ts_ms=decision_ms,
            entry=setup.get("entry"), stop_loss=setup.get("stop_loss"),
            tp1=setup.get("tp1"), tp2=setup.get("tp2"), atr=setup.get("atr"))
    except (TypeError, ValueError):
        return None, "INVALID_SETUP"
    return opportunity, None


def _candles(raw: Any, planned: PlannedRow, bar: int):
    """Converte `timestamp` R09 → `timestamp_ms` R10A preservando os valores."""
    if not isinstance(raw, list):
        raise DatasetError("projeção de velas fora do contrato")
    out = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"timestamp", "open", "high", "low", "close", "volume"}:
            return None
        stamp = item.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            return None
        if not planned.first_ms <= stamp or stamp + bar > planned.window_end_ms:
            # A projeção é filtrada no PostgreSQL; aqui é violação de contrato.
            raise DatasetError("vela fora da janela permitida chegou ao cliente")
        bar_row = {"timestamp_ms": stamp, **{k: item.get(k) for k in ("open", "high", "low", "close", "volume")}}
        try:
            r10a.Candle(**bar_row)
        except (TypeError, ValueError):
            return None
        out.append(bar_row)
    return out


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        def reject(constant):
            raise DatasetError("constante JSON não finita")
        return json.loads(value, parse_constant=reject)
    return value


def build_artifacts(request: ExportRequest, plan: SelectionPlan, detail_rows: Iterable[Mapping],
                    sealed: Mapping[str, int]) -> tuple:
    """Dataset `compare` + manifesto. Detalhes vêm SÓ dos ids admitidos."""
    details = {}
    for row in detail_rows:
        key = row.get("opportunity_key")
        if key in details:
            raise DatasetError("detalhe duplicado para a mesma oportunidade")
        details[key] = row
    if set(details) != set(plan.keys):
        raise DatasetError("detalhes não correspondem exatamente aos ids admitidos")
    bar = request.bar_ms
    exclusions = {reason: 0 for reason in EXCLUSION_REASONS}
    opportunities, bars_by_id, source_rows = [], {}, []
    coverage = {"with_candles": 0, "without_candles": 0, "candles_exported": 0,
                "complete_windows": 0, "incomplete_windows": 0, "cutoff_truncated_windows": 0,
                "non_contiguous_windows": 0}
    exported = {"training": 0, "validation": 0}
    for planned in plan.rows:
        row = details[planned.key]
        if not isinstance(row.get("decision_at"), datetime) or utc_ms(row["decision_at"]) != planned.decision_ms:
            raise DatasetError("decisão mudou entre índice e detalhe")
        version = row.get("version")
        setup, config = _json(row.get("frozen_setup")), _json(row.get("frozen_config"))
        row = {**row, "frozen_setup": setup, "frozen_config": config}
        reason = _source_mismatch(config, request.baseline)
        opportunity = None
        if reason is None:
            opportunity, reason = _setup(row, planned.decision_ms)
        bars = None
        if reason is None:
            if row.get("candles_malformed"):
                reason = "MALFORMED_CANDLE_JSON"
            else:
                bars = _candles(_json(row.get("candles")), planned, bar)
                if bars is None:
                    reason = "INVALID_CANDLE_DATA"
        if reason is not None:
            exclusions[reason] += 1
            source_rows.append({"key": planned.key, "version": version, "excluded": reason})
            continue
        opp_row = {f.name: getattr(opportunity, f.name) for f in fields(r10a.Opportunity)
                   if getattr(opportunity, f.name) is not None}
        opportunities.append(opp_row)
        bars_by_id[planned.key] = bars
        exported[planned.split] += 1
        expected = (planned.window_end_ms - planned.first_ms) // bar
        stamps = [b["timestamp_ms"] for b in bars]
        coverage["with_candles" if bars else "without_candles"] += 1
        coverage["candles_exported"] += len(bars)
        coverage["complete_windows" if len(bars) == expected == request.horizon_bars else "incomplete_windows"] += 1
        coverage["cutoff_truncated_windows"] += planned.cutoff_truncated
        coverage["non_contiguous_windows"] += any(
            b - a != bar for a, b in zip([planned.first_ms - bar] + stamps, stamps))
        source_rows.append({"key": planned.key, "version": version, "setup": setup,
                            "config": config, "bars": bars})
    dataset = {**request.payload_head(), "opportunities": opportunities, "bars_by_id": bars_by_id}
    dataset_bytes = canonical_bytes(dataset)
    if len(dataset_bytes) > MAX_ARTIFACT_BYTES:
        raise DatasetLimitError("dataset acima de 16 MiB do CLI R10A; estreite a janela")
    total_exported = len(opportunities)
    candidates = plan.counts["training_candidates"] + plan.counts["validation_candidates"]
    state = ("EXPORTED" if total_exported else
             "EMPTY" if candidates == 0 else
             "UNUSABLE_ALL_PURGED" if candidates == plan.counts["purged"] else "UNUSABLE_ALL_EXCLUDED")
    cost_complete = r10a.CostConfig.manifest(request.costs)["complete"]
    manifest = {
        "schema_version": EXPORTER_SCHEMA,
        "mode": "LOCAL_RESEARCH_ONLY", "promotable": False, "live_equivalent": False,
        "approval": None, "economic_sufficiency": "NOT_ASSESSED",
        "state": state,
        "source": {"cohort": COHORT, "tables": [REJECTED_TABLE, OPPORTUNITY_TABLE],
                   "source_schema": SOURCE_SCHEMA, "source_policy": SOURCE_POLICY,
                   "price_source": SOURCE_PRICE, "unit": "ONE_REJECTED_OPPORTUNITY",
                   "decision_time": f"{REJECTED_TABLE}.decision_at",
                   "transaction": "REPEATABLE READ READ ONLY",
                   "outcome_or_coverage_read": False},
        "cutoff": {"as_of_ms": request.as_of_ms,
                   "as_of_utc": ms_datetime(request.as_of_ms).isoformat().replace("+00:00", "Z"),
                   "decision_rule": "decision_at < min(holdout_start, as_of)",
                   "candle_rule": "first_bar <= timestamp e timestamp + bar_ms <= min(first_bar + horizon, fronteira do split, as_of)"},
        "request_hash": request.request_hash(),
        "configs": {"baseline": request.baseline.manifest(),
                    "candidate": request.candidate.manifest(),
                    "costs": request.costs.manifest(), "split": asdict(request.split),
                    "bootstrap": asdict(request.bootstrap),
                    "management_changed_parameters": list(request.changed),
                    "horizon_bars": request.horizon_bars,
                    "registration_evidence": "OPERATOR_SUPPLIED_TIMESTAMP_NOT_INDEPENDENT_PROOF"},
        "costs": {"status": "DECLARED_SCENARIO" if cost_complete else "UNKNOWN",
                  "observed_account_costs": False,
                  "net_r_comparable": cost_complete},
        "selection": [
            "Ordem (decision_at, opportunity_key); janelas semiabertas [início, fim).",
            "Purga pelo maior horizonte baseline/candidato + purge_bars, antes de ler detalhes.",
            "Detalhes e velas só dos ids admitidos; velas filtradas no PostgreSQL.",
            "Sem filtro por cobertura, outcome, retorno, win/loss ou completude.",
            "Holdout e linhas após o cutoff: apenas contagem.",
        ],
        "counts": {**plan.counts, "before_training": int(sealed.get("before_training", 0)),
                   "holdout_sealed": int(sealed.get("holdout_sealed", 0)),
                   "after_cutoff": int(sealed.get("after_cutoff", 0)),
                   "excluded": exclusions, "exported": exported},
        "coverage": coverage,
        "fingerprints": {"dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
                         "source_sha256": _sha(source_rows)},
        "holdout": {"policy": "SEALED", "details_read": False,
                    "comparator_counter": "reflete apenas o payload; use counts.holdout_sealed"},
        "analysis_command": "backend/scripts/research_replay.py dataset.json",
        "limitations": LIMITATIONS,
    }
    # Mesma forma em memória e em disco (tuplas viram listas; NaN é recusado).
    return json.loads(dataset_bytes), json.loads(canonical_bytes(manifest))


# ── Loader somente leitura (sessão injetada) ────────────────────────────────

_SET_READ_ONLY = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
_MODE_SQL = ("SELECT current_setting('transaction_read_only') AS read_only, "
             "current_setting('transaction_isolation') AS isolation")
_COUNTS_SQL = f"""
SELECT count(*) FILTER (WHERE decision_at < :train_start) AS before_training,
       count(*) FILTER (WHERE decision_at >= :holdout_start AND decision_at < :as_of) AS holdout_sealed,
       count(*) FILTER (WHERE decision_at >= :as_of) AS after_cutoff
FROM {REJECTED_TABLE}
"""
_INDEX_SQL = f"""
SELECT opportunity_key, decision_at
FROM {REJECTED_TABLE}
WHERE decision_at >= :train_start AND decision_at < :index_end
ORDER BY decision_at, opportunity_key
LIMIT :row_limit
"""
_VALID_TS = ("jsonb_typeof(e.value) = 'object' "
             "AND jsonb_typeof(e.value -> 'timestamp') = 'number' "
             "AND (e.value ->> 'timestamp') ~ '^[0-9]{1,15}$'")
_DETAIL_SQL = f"""
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
JOIN {REJECTED_TABLE} AS r ON r.opportunity_key = b.k
LEFT JOIN {OPPORTUNITY_TABLE} AS o ON o.opportunity_key = r.opportunity_key
ORDER BY r.decision_at, r.opportunity_key
"""


async def load_dataset(session, request: ExportRequest) -> tuple:
    """Uma transação REPEATABLE READ READ ONLY; sempre termina em rollback."""
    from sqlalchemy import text
    if not isinstance(request, ExportRequest):
        raise DatasetError("requisição validada obrigatória")
    if session.in_transaction():
        raise DatasetError("sessão precisa começar sem transação ativa")
    split = request.split
    try:
        await session.execute(text(_SET_READ_ONLY))
        mode = (await session.execute(text(_MODE_SQL))).mappings().one()
        if (mode["read_only"], mode["isolation"]) != ("on", "repeatable read"):
            raise DatasetError("transação não está em REPEATABLE READ READ ONLY")
        as_of = ms_datetime(request.as_of_ms)
        sealed = (await session.execute(text(_COUNTS_SQL), {
            "train_start": ms_datetime(split.train_start_ms),
            "holdout_start": ms_datetime(split.holdout_start_ms), "as_of": as_of,
        })).mappings().one()
        index = (await session.execute(text(_INDEX_SQL), {
            "train_start": ms_datetime(split.train_start_ms),
            "index_end": ms_datetime(min(split.holdout_start_ms, request.as_of_ms)),
            "row_limit": r10a.MAX_OPPORTUNITIES + 1,
        })).mappings().all()
        plan = plan_selection(request, [(row["opportunity_key"], row["decision_at"]) for row in index])
        details = []
        if plan.rows:
            details = (await session.execute(text(_DETAIL_SQL), {
                "keys": [row.key for row in plan.rows],
                "los": [row.first_ms for row in plan.rows],
                "his": [row.window_end_ms for row in plan.rows],
                "bar_ms": request.bar_ms,
            })).mappings().all()
        return build_artifacts(request, plan, details, dict(sealed))
    finally:
        await session.rollback()


# ── Escrita local, sem sobrescrita nem par parcial ──────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]


def check_output_dir(out_dir: Any) -> Path:
    path = Path(out_dir).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise DatasetError("destino já existe; exportação nunca sobrescreve")
    if not path.parent.is_dir():
        raise DatasetError("diretório pai do destino inexistente")
    resolved = path.parent.resolve() / path.name
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise DatasetError("dataset de conta não pode ser gravado dentro do repositório")
    return resolved


def write_artifacts(out_dir: Any, dataset: dict, manifest: dict) -> Path:
    """Staging fora do destino; o manifesto é o ÚLTIMO arquivo (marca de conclusão)."""
    target = check_output_dir(out_dir)
    blobs = [("dataset.json", canonical_bytes(dataset)), ("manifest.json", canonical_bytes(manifest))]
    if hashlib.sha256(blobs[0][1]).hexdigest() != manifest["fingerprints"]["dataset_sha256"]:
        raise DatasetError("manifesto não corresponde ao dataset")
    staging = Path(tempfile.mkdtemp(prefix=".r10b-staging-", dir=target.parent))
    moved = []
    try:
        for name, blob in blobs:
            with open(staging / name, "xb") as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
        os.mkdir(target)
        try:
            for name, _ in blobs:
                os.rename(staging / name, target / name)
                moved.append(target / name)
        except BaseException:
            for path in reversed(moved):
                path.unlink(missing_ok=True)
            target.rmdir()
            raise
    finally:
        for name, _ in blobs:
            (staging / name).unlink(missing_ok=True)
        staging.rmdir()
    return target
