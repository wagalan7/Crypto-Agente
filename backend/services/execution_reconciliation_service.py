"""
Execution Reconciliation (P03 + P03.1) — reconciliação PERSISTENTE de execução.

Torna persistente a reconciliação de ordens/posições/condicionais que fiquem
`UNKNOWN`, inclusive após restart:

    submissão → safety state P02 → incidente persistido → restart/reconciler
    → mesma ordem reconciliada → posição/condicionais confirmadas
    → PROTECTED | FLAT | MANUAL_REQUIRED → liberação segura da quarentena própria.

Invariantes (P03.1):
  - UNKNOWN nunca é FLAT; nunca reenvia entry; nunca cria MARKET;
  - nenhum TP com qty incerta; nenhuma ordem alheia cancelada;
  - nenhum latch alheio (P02/operador) liberado pelo P03;
  - processo com lease vencido NÃO atualiza estado nem muta a exchange.

P03 só CONSULTA (get_order/positionRisk/open_algo), pode CANCELAR maker/
condicionais EXATAS, e pode criar SOMENTE SL sob invariantes estritas — nunca
entry, nunca MARKET, nunca TP com qty incerta. Consome o formato NORMALIZADO
snake_case real de `get_open_algo_orders` (algo_id/client_algo_id/close_position/
reduce_only/quantity/side/trigger_price/type).
"""
from __future__ import annotations

import os
import math
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

log = logging.getLogger(__name__)

EXCHANGE_BINANCE = "binance"
_LATCH_OWNER = "p03"


class Kind:
    ENTRY_SUBMISSION_UNKNOWN = "ENTRY_SUBMISSION_UNKNOWN"
    ENTRY_ORDER_UNKNOWN = "ENTRY_ORDER_UNKNOWN"
    FINAL_FILL_QTY_UNKNOWN = "FINAL_FILL_QTY_UNKNOWN"
    CONDITIONAL_SUBMISSION_UNKNOWN = "CONDITIONAL_SUBMISSION_UNKNOWN"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    UNTRACKED_POSITION = "UNTRACKED_POSITION"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class State:
    OPEN = "OPEN"
    RECONCILING = "RECONCILING"
    PROTECTED = "PROTECTED"          # terminal seguro
    FLAT = "FLAT"                    # terminal seguro
    RETRY_PENDING = "RETRY_PENDING"  # segurança (continua pausado)
    MANUAL_REQUIRED = "MANUAL_REQUIRED"  # segurança (continua pausado)


_TERMINAL_SAFE = {State.PROTECTED, State.FLAT}
_ENTRY_KINDS = {Kind.ENTRY_SUBMISSION_UNKNOWN, Kind.ENTRY_ORDER_UNKNOWN, Kind.FINAL_FILL_QTY_UNKNOWN}
_CLEANUP_KINDS = {Kind.CONDITIONAL_SUBMISSION_UNKNOWN, Kind.CLEANUP_PENDING}
_MANUAL_KINDS = {Kind.UNTRACKED_POSITION, Kind.PERSISTENCE_FAILURE}
_TERMINAL_ORDER_STATUS = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}


def kind_from_safety_state(safety_state: Optional[str], *, closed: bool = False) -> str:
    """Mapeia o safety_state real do P02 para o Kind correto (não colapsa tudo em
    ENTRY_ORDER_UNKNOWN). Posição fechada com condicional possivelmente órfã →
    CLEANUP_PENDING; submissão de entry desconhecida → ENTRY_SUBMISSION_UNKNOWN."""
    s = str(safety_state or "").upper()
    if closed or "ROLLBACK" in s or "CLEANUP" in s or "AFTER_CLOSE" in s:
        return Kind.CLEANUP_PENDING
    if "CONDITIONAL" in s:
        return Kind.CONDITIONAL_SUBMISSION_UNKNOWN
    if "FILL" in s and "QTY" in s:
        return Kind.FINAL_FILL_QTY_UNKNOWN
    if "SUBMISSION_UNKNOWN" in s:
        return Kind.ENTRY_SUBMISSION_UNKNOWN
    return Kind.ENTRY_ORDER_UNKNOWN


# ── Config (defaults conservadores / fail-closed) ───────────────────────────
def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


RECONCILE_INTERVAL_S = _f("RECONCILE_INTERVAL_S", 30.0)
RECONCILE_BACKOFF_BASE_S = _f("RECONCILE_BACKOFF_BASE_S", 15.0)
RECONCILE_BACKOFF_MAX_S = _f("RECONCILE_BACKOFF_MAX_S", 900.0)
RECONCILE_CLEAN_GRACE = _i("RECONCILE_CLEAN_GRACE", 2)        # ciclos separados
RECONCILE_LEASE_S = _f("RECONCILE_LEASE_S", 120.0)
RECONCILE_MAX_ATTEMPTS = _i("RECONCILE_MAX_ATTEMPTS", 8)
RECONCILE_MAX_PER_CYCLE = _i("RECONCILE_MAX_PER_CYCLE", 10)
RECONCILE_CREATE_SL = _b("RECONCILE_CREATE_SL", True)         # criar SL sob invariantes
RECONCILE_STOP_TOL_FRAC = _f("RECONCILE_STOP_TOL_FRAC", 0.002)  # trigger dentro de 0,2% (P02)
RECONCILE_QTY_MIN_COVER = _f("RECONCILE_QTY_MIN_COVER", 0.5)    # RealTrade qty >= 50% da posição fresh

_PROCESS_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_PAUSE_MARKER = "P03-QUARANTINE:"

_last_reconciliation_at: Optional[str] = None
_reconciler_running = False
_prev_open_count = 0          # p/ liberar quarentena SÓ na transição >0 → 0
_p03_latch_armed = False      # P03 realmente armou o latch?
_boot_scan_safe = False       # leitura fresh de posições/incidentes já teve sucesso?
_prev_boot_safe = False       # p/ detectar recuperação unsafe→safe (release 0→0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mask(v: Any) -> Optional[str]:
    s = str(v) if v is not None else None
    if not s:
        return s
    return s if len(s) <= 6 else f"{s[:3]}…{s[-3:]}"


def _finite(x) -> Optional[float]:
    """float finito ou None (rejeita NaN/inf/inválido)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_bool(v) -> bool:
    """Normaliza booleano: a string "false"/"0"/"" NUNCA vira True."""
    if isinstance(v, bool):
        return v
    return str(v if v is not None else "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_prefix(seed: str) -> str:
    """clientAlgoId de fallback aceito pela Binance: só [A-Za-z0-9-], sem `…`
    Unicode e dentro de um limite curto."""
    import re
    base = re.sub(r"[^A-Za-z0-9]", "", str(seed or ""))[-10:] or uuid.uuid4().hex[:8]
    return f"p03-{base}"[:20]


def _backoff_seconds(attempts: int) -> float:
    # attempts=1 → BASE (casa com a doc: 15s inicial), depois dobra até o teto.
    exp = RECONCILE_BACKOFF_BASE_S * (2 ** max(0, attempts - 1))
    return float(min(exp, RECONCILE_BACKOFF_MAX_S))


def _norm_entry_side(side) -> Optional[str]:
    s = str(side or "").strip().lower()
    if s in ("buy", "long"):
        return "BUY"
    if s in ("sell", "short"):
        return "SELL"
    return None


def _close_side(entry_side: str) -> str:
    return "SELL" if entry_side == "BUY" else "BUY"


def _mark_boot_unsafe() -> None:
    global _boot_scan_safe
    _boot_scan_safe = False


_MAKER_PENDING_STATES = {"ENTRY_SUBMISSION_UNKNOWN", "ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN"}


def assemble_entry_incident(order_res: dict, rec: dict, *, closed: bool = False,
                            snapshot_id=None, local_client_order_id=None,
                            local_planned_qty=None) -> dict:
    """Monta os kwargs de `record_incident` a partir dos dados REAIS do caller.
    Usa `local_client_order_id`/`local_planned_qty` (vars locais do caller) como
    fonte primária. Reconhece o contrato maker real (incl. emissão GTX ambígua sem
    `status`) e evita falso-pending quando `entry_order_terminal=true`."""
    order_res = order_res or {}
    rec = rec or {}
    coid = (local_client_order_id or order_res.get("client_order_id")
            or rec.get("client_order_id"))
    was_maker = _as_bool(order_res.get("was_maker") or order_res.get("maker")
                         or order_res.get("is_maker"))
    status = str(order_res.get("status") or order_res.get("entry_order_status") or "").upper()
    ss = str(order_res.get("safety_state") or "").upper()
    terminal = _as_bool(order_res.get("entry_order_terminal"))
    pending_signals = (
        _as_bool(order_res.get("pending_entry_order"))
        or status in ("NEW", "PARTIALLY_FILLED")
        or ss in _MAKER_PENDING_STATES
        or (was_maker and not status)   # GTX ambígua: was_maker sem status = pendente
    )
    pending_maker = bool(was_maker and pending_signals and not terminal)
    ffqu = _as_bool(order_res.get("final_fill_qty_unknown"))
    kind = (Kind.FINAL_FILL_QTY_UNKNOWN if ffqu
            else kind_from_safety_state(order_res.get("safety_state"), closed=closed))
    result = order_res.get("result") or {}
    cond_ids = {}
    for k, src in (("sl", "sl_order_id"), ("tp1", "tp1_order_id"), ("tp2", "tp2_order_id")):
        v = order_res.get(src) or rec.get(src)
        if v:
            cond_ids[k] = str(v)
    eoid = result.get("orderId") or order_res.get("entry_order_id")
    planned_qty = (local_planned_qty if local_planned_qty is not None
                   else (rec.get("qty") or rec.get("position_size")))
    return dict(
        kind=kind, symbol=rec.get("symbol"),
        side=rec.get("direction") or rec.get("side"),
        client_order_id=coid,
        entry_order_id=(str(eoid) if eoid else None),
        conditional_prefix=coid,                       # P02 nomeia <coid>-sl/-tp1/-tp2
        conditional_ids=(cond_ids or None),
        planned_qty=planned_qty,
        planned_stop=rec.get("stop_loss") or rec.get("stop"),
        min_known_fill=(_finite(order_res.get("executed_qty")) or None),
        safety_state=order_res.get("safety_state"),
        snapshot_id=(str(snapshot_id) if snapshot_id else None),
        pending_maker=pending_maker,
        payload={"closed": bool(closed)},
    )


# ════════════════════════════════════════════════════════════════════════════
#  NÚCLEO DE DECISÃO — puro (sem DB/exchange), 100% testável
# ════════════════════════════════════════════════════════════════════════════
def terminal_qty(order_res: dict) -> tuple[Optional[float], str]:
    """Qty terminal de uma ordem de entrada:
      - FILLED → qty confirmada; REJECTED → 0;
      - CANCELED/EXPIRED/EXPIRED_IN_MATCH → exige executedQty terminal EXPLÍCITA;
      - ausente/NaN/inf → None (FINAL_FILL_QTY_UNKNOWN).
    Fill pré-terminal é apenas lower bound (nunca qty final)."""
    if not order_res or not order_res.get("ok"):
        return None, "consulta inconclusiva"
    status = (order_res.get("status") or "").upper()
    raw = order_res.get("raw") or {}
    if status == "FILLED":
        qty = _finite(order_res.get("orig_qty")) or _finite(order_res.get("executed_qty"))
        if qty is None:
            return None, "FILLED sem qty numérica"
        return (qty, "filled") if qty > 0 else (None, "FILLED com qty<=0")
    if status == "REJECTED":
        return 0.0, "rejected"
    if status in _TERMINAL_ORDER_STATUS:
        # Exige executedQty EXPLÍCITA no raw. O `executed_qty` normalizado vira
        # 0.0 por ausência — tratá-lo como fill zero viraria FLAT indevidamente.
        if "executedQty" not in raw:
            return None, f"{status} sem executedQty no raw (UNKNOWN, não FLAT)"
        eq = _finite(raw.get("executedQty"))
        if eq is None:
            return None, f"{status} com executedQty inválida"
        return eq, f"{status.lower()}_terminal"
    return None, f"não terminal ({status or 'sem status'})"


def classify_entry(order_res: dict) -> tuple[str, Optional[float], str]:
    """(verdict, qty_final|None, motivo), verdict ∈ {RETRY, FILL_UNKNOWN, FLAT, PROTECTED}."""
    if not order_res or not order_res.get("ok"):
        return "RETRY", None, "consulta indisponível"
    status = (order_res.get("status") or "").upper()
    if status in ("", "NEW", "PARTIALLY_FILLED"):
        return "RETRY", None, f"ainda {status or 'sem status'}"
    qty, why = terminal_qty(order_res)
    if qty is None:
        return "FILL_UNKNOWN", None, why
    if qty <= 0:
        return "FLAT", 0.0, why
    return "PROTECTED", qty, why


def position_verdict(size: Optional[float], status: str) -> str:
    """None (stale/rate-limited/erro/UNKNOWN) NUNCA prova flat."""
    if size is None:
        return "UNKNOWN"
    return "FLAT" if abs(size) <= 0 else "OPEN"


def _order_identities(o: dict) -> set:
    """Identidades EXATAS de uma ordem do listing (snake_case real + camel legado)."""
    ids = set()
    if not isinstance(o, dict):
        return ids
    for fld in ("algo_id", "client_algo_id", "algoId", "clientAlgoId",
                "order_id", "client_order_id", "orderId", "clientOrderId"):
        v = o.get(fld)
        if v:
            ids.add(str(v))
    return ids


_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD")


def _sym_key(s: str) -> str:
    """Chave normalizada base/quote — DISTINGUE quote (BTCUSDC ≠ BTCUSDT).
    "BTC/USDT:USDT"→"BTC/USDT"; "BTCUSDT"→"BTC/USDT"; "BTCUSDC"→"BTC/USDC"."""
    import re
    s = re.sub(r"[^A-Z0-9/:]", "", str(s or "").upper())
    if "/" in s:
        base = s.split("/")[0]
        quote = s.split("/")[1].split(":")[0]
        return f"{base}/{quote}" if base and quote else s
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return s


def _sym_base(s: str) -> str:
    k = _sym_key(s)
    return k.split("/")[0] if "/" in k else k


# Tipos de STOP aceitos: EXATO STOP_MARKET. TRAILING_STOP_MARKET é REJEITADO.
_ACCEPTED_STOP_TYPES = {"STOP_MARKET"}
_DEAD_ALGO_STATUS = {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "FILLED", "REJECTED"}


def _adopt_live_stop(inc: dict, need_qty: Optional[float], listing: dict) -> tuple[bool, Optional[str], str]:
    """Adota um SL vivo VÁLIDO (contrato P02), consumindo snake_case real. Exige:
    side do incidente conhecido; side da ordem PRESENTE e exatamente oposto;
    símbolo correspondente; status vivo (quando presente); tipo STOP; `algo_id`
    válido; trigger dentro da tolerância P02; e cobertura por `close_position=True`
    OU (`reduce_only=True` e `quantity >= need_qty`). `need_qty` já é
    max(qty_terminal_confirmada, posição_fresh_total). Retorna (ok, algo_id, det)."""
    entry_side = _norm_entry_side(inc.get("side"))
    if entry_side is None:
        return False, None, "side do incidente desconhecido — não adota"
    want_close = _close_side(entry_side)
    planned_stop = _finite(inc.get("planned_stop"))
    if planned_stop is None or planned_stop <= 0:
        # Sem planned_stop válido não há como validar o trigger → NÃO adota stop
        # existente (poderia ser de outro nível/estratégia). Escala retry/manual.
        return False, None, "sem planned_stop válido — não adota stop existente"
    inc_key = _sym_key(inc.get("symbol"))
    for o in (listing.get("orders") or []):
        if not isinstance(o, dict):
            continue
        otype = str(o.get("type") or o.get("origType") or "").upper()
        if otype not in _ACCEPTED_STOP_TYPES:        # EXATO STOP_MARKET; trailing rejeitado
            continue
        algo_id = o.get("algo_id") or o.get("algoId")
        if not algo_id:
            continue
        oside = str(o.get("side") or "").upper()
        if oside != want_close:                      # side ausente OU não-oposto → rejeita
            continue
        osym = o.get("symbol")
        if not osym or _sym_key(osym) != inc_key:     # símbolo/contrato/quote exato obrigatório
            continue
        ostatus = str(o.get("status") or o.get("algoStatus") or "").upper()
        if ostatus and ostatus in _DEAD_ALGO_STATUS:
            continue                                 # não está vivo
        close_position = _as_bool(o.get("close_position") if o.get("close_position") is not None
                                  else o.get("closePosition"))
        reduce_only = _as_bool(o.get("reduce_only") if o.get("reduce_only") is not None
                               else o.get("reduceOnly"))
        if close_position:
            covered = True
        elif reduce_only:
            oq = _finite(o.get("quantity") if o.get("quantity") is not None else o.get("origQty"))
            covered = (need_qty is None) or (oq is not None and oq + 1e-12 >= float(need_qty))
        else:
            covered = False
        if not covered:
            continue
        if planned_stop is not None and planned_stop > 0:
            trig = _finite(o.get("trigger_price") if o.get("trigger_price") is not None
                           else o.get("triggerPrice") or o.get("stopPrice"))
            if trig is None or abs(trig - planned_stop) / planned_stop > RECONCILE_STOP_TOL_FRAC:
                continue  # trigger fora da tolerância P02
        return True, str(algo_id), f"SL vivo adotado ({_mask(algo_id)})"
    return False, None, "sem SL vivo válido (lado/cobertura/trigger)"


# ════════════════════════════════════════════════════════════════════════════
#  Repositório de incidentes (persistente) + fallback/injeção em memória
# ════════════════════════════════════════════════════════════════════════════
_EXTRA_COLS = None  # colunas reais do model (cache)


def _model_cols() -> set:
    global _EXTRA_COLS
    if _EXTRA_COLS is None:
        try:
            from models.execution_incident import ExecutionIncident
            _EXTRA_COLS = {c.name for c in ExecutionIncident.__table__.columns}
        except Exception:
            _EXTRA_COLS = set()
    return _EXTRA_COLS


def _merge_conditional_ids(cur: dict, new: dict) -> dict:
    """União HISTÓRICA: perna atual (sl/tp1/tp2) atualizada + lista `all`
    deduplicada com TODOS os IDs já vistos. `{sl:S1}` + `{sl:S2}` preserva S1 e S2."""
    out = dict(cur or {})
    all_ids = [str(x) for x in (out.get("all") or []) if x]
    def _add(v):
        if v and str(v) not in all_ids:
            all_ids.append(str(v))
    for leg in ("sl", "tp1", "tp2"):
        _add(out.get(leg))
    for leg in ("sl", "tp1", "tp2"):
        v = (new or {}).get(leg)
        if v:
            out[leg] = str(v)
            _add(v)
    for v in ((new or {}).get("all") or []):
        _add(v)
    for v in ((new or {}).get("ids") or []):
        _add(v)
    out["all"] = all_ids
    return out


def _merge_incident_row(row: dict, incoming: dict) -> None:
    """Merge monotônico in-place (mesma semântica do ON CONFLICT DO UPDATE):
    nunca reduz informação conhecida. Reabre reincidência resolvida (limpa
    manual_reason, claim/lease e backoff antigo → elegível imediatamente)."""
    if row.get("resolved_at") is not None:
        row.update({"state": State.OPEN, "resolved_at": None, "attempts": 0,
                    "clean_observations": 0, "next_retry_at": _now(),
                    "manual_reason": None, "claimed_by": None, "claimed_at": None,
                    "lease_expires_at": None, "last_error": "reaberto: problema reincidiu"})
    inc_lb = incoming.get("min_known_fill")
    if inc_lb is not None:
        row["min_known_fill"] = max(row.get("min_known_fill") or 0.0, inc_lb)
    new_cids = incoming.get("conditional_ids")
    if isinstance(new_cids, dict) and new_cids:
        row["conditional_ids"] = _merge_conditional_ids(row.get("conditional_ids") or {}, new_cids)
    new_pl = incoming.get("payload")
    if isinstance(new_pl, dict) and new_pl:
        merged = dict(row.get("payload") or {})
        merged.update(new_pl)
        row["payload"] = merged
    for scol in ("side", "planned_qty", "planned_stop", "client_order_id",
                 "entry_order_id", "conditional_prefix"):
        if row.get(scol) is None and incoming.get(scol) is not None:
            row[scol] = incoming[scol]


class InMemoryIncidentRepo:
    """Repositório em memória com a MESMA semântica de claim/lease/upsert do SQL.
    Fallback fail-closed quando DB off; injetado nos testes (sem tocar banco)."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        """Atômico: no conflito faz merge (GREATEST do lower-bound, união de
        conditional_ids/payload, preenche IDs/stop/qty/side/prefixo ausentes) e
        REABRE reincidência — sem reduzir informação conhecida nem duplicar."""
        async with self._lock:
            row = self._rows.get(key)
            if row is None:
                self._seq += 1
                row = {
                    "id": self._seq, "incident_key": key, "attempts": 0,
                    "clean_observations": 0, "state": State.OPEN,
                    "claimed_by": None, "claimed_at": None, "lease_expires_at": None,
                    "resolved_at": None, "created_at": _now(), "updated_at": _now(),
                }
                row.update({k: v for k, v in defaults.items() if v is not None or k in row})
                self._rows[key] = row
                return dict(row), True
            _merge_incident_row(row, defaults)   # in-place, monotônico
            row["updated_at"] = _now()
            return dict(row), False

    async def get(self, key: str) -> Optional[dict]:
        row = self._rows.get(key)
        return dict(row) if row else None

    async def list_open(self) -> list[dict]:
        return [dict(r) for r in self._rows.values() if r.get("resolved_at") is None]

    async def list_all(self) -> list[dict]:
        return [dict(r) for r in self._rows.values()]

    async def update(self, key: str, **fields) -> Optional[dict]:
        async with self._lock:
            row = self._rows.get(key)
            if not row:
                return None
            row.update(fields)
            row["updated_at"] = _now()
            return dict(row)

    async def update_claimed(self, key: str, owner: str, **fields) -> Optional[dict]:
        """Fenced: só aplica se `owner` detém o claim COM lease válido e o
        incidente ainda não foi resolvido (elegível)."""
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return None
            if row.get("claimed_by") != owner:
                return None
            exp = row.get("lease_expires_at")
            if exp is None or exp < _now():
                return None  # lease vencido → não atualiza
            row.update(fields)
            row["updated_at"] = _now()
            return dict(row)

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return False
            cur = row.get("claimed_by")
            exp = row.get("lease_expires_at")
            free = cur is None or (exp is not None and exp < _now())
            if not free and cur != owner:
                return False
            row["claimed_by"] = owner
            row["claimed_at"] = _now()
            row["lease_expires_at"] = lease_until
            return True

    async def renew_claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        async with self._lock:
            row = self._rows.get(key)
            if not row or row.get("resolved_at") is not None:
                return False
            if row.get("claimed_by") != owner:
                return False
            exp = row.get("lease_expires_at")
            if exp is None or exp < _now():
                return False  # lease vencido NÃO ressuscita o próprio claim
            row["lease_expires_at"] = lease_until
            return True

    async def release_claim(self, key: str, owner: Optional[str] = None) -> None:
        async with self._lock:
            row = self._rows.get(key)
            if not row:
                return
            if owner is not None and row.get("claimed_by") != owner:
                return  # não libera claim alheio
            row["claimed_by"] = None
            row["claimed_at"] = None
            row["lease_expires_at"] = None

    async def recover_expired_claims(self) -> int:
        async with self._lock:
            n, now = 0, _now()
            for row in self._rows.values():
                exp = row.get("lease_expires_at")
                if row.get("claimed_by") and exp is not None and exp < now:
                    row["claimed_by"] = row["claimed_at"] = row["lease_expires_at"] = None
                    n += 1
            return n


class _SqlIncidentRepo:
    """Repositório Postgres: upsert atômico (ON CONFLICT) + claim/fencing por UPDATE…WHERE."""

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        """Uma única operação atômica: INSERT … ON CONFLICT DO UPDATE … RETURNING.
        No conflito: GREATEST do lower-bound, merge de conditional_ids/payload
        (jsonb ||), COALESCE dos IDs/stop/qty/side/prefixo ausentes e reabertura
        atômica da reincidência. Nunca reduz informação. (xmax=0 ⇒ inserido.)"""
        from db import get_session
        from models.execution_incident import ExecutionIncident as EI
        from sqlalchemy import text, literal_column
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        cols = {k: v for k, v in defaults.items() if k in _model_cols()}
        T = "execution_incidents"

        def _co(c):   # COALESCE(existente, novo)
            return text(f"COALESCE({T}.{c}, EXCLUDED.{c})")

        def _jm(c):   # merge jsonb sem apagar anteriores, de volta pro tipo JSON
            return text(f"(COALESCE({T}.{c}::jsonb,'{{}}'::jsonb) "
                        f"|| COALESCE(EXCLUDED.{c}::jsonb,'{{}}'::jsonb))::json")

        def _reopen(c, reopened):  # CASE de reabertura
            return text(f"CASE WHEN {T}.resolved_at IS NOT NULL THEN {reopened} ELSE {T}.{c} END")

        # conditional_ids: a COLUNA É `JSON`. Casta os operandos para JSONB ANTES
        # de qualquer `->`/`->>`/`||` (senão o Postgres não tem operador json ? /
        # json || jsonb) e converte o resultado final de volta para JSON.
        CJ = f"(COALESCE({T}.conditional_ids,'{{}}')::jsonb)"
        EJ = "(COALESCE(EXCLUDED.conditional_ids,'{}')::jsonb)"
        cond_merge = text(
            f"(({CJ} || {EJ}) "
            f"|| jsonb_build_object('all', (SELECT COALESCE(jsonb_agg(DISTINCT v),'[]'::jsonb) FROM ("
            f"  SELECT jsonb_array_elements_text(COALESCE({CJ}->'all','[]'::jsonb)) AS v"
            f"  UNION SELECT jsonb_array_elements_text(COALESCE({EJ}->'all','[]'::jsonb))"
            f"  UNION SELECT {CJ}->>'sl' UNION SELECT {CJ}->>'tp1' UNION SELECT {CJ}->>'tp2'"
            f"  UNION SELECT {EJ}->>'sl' UNION SELECT {EJ}->>'tp1' UNION SELECT {EJ}->>'tp2'"
            f") s WHERE v IS NOT NULL)))::json")

        set_ = {
            "min_known_fill": text(f"GREATEST(COALESCE({T}.min_known_fill,0), "
                                   f"COALESCE(EXCLUDED.min_known_fill,0))"),
            "side": _co("side"), "planned_qty": _co("planned_qty"),
            "planned_stop": _co("planned_stop"), "client_order_id": _co("client_order_id"),
            "entry_order_id": _co("entry_order_id"), "conditional_prefix": _co("conditional_prefix"),
            "conditional_ids": cond_merge, "payload": _jm("payload"),
            "state": _reopen("state", "'OPEN'"), "resolved_at": _reopen("resolved_at", "NULL"),
            "attempts": _reopen("attempts", "0"), "clean_observations": _reopen("clean_observations", "0"),
            "manual_reason": _reopen("manual_reason", "NULL"),
            "claimed_by": _reopen("claimed_by", "NULL"), "claimed_at": _reopen("claimed_at", "NULL"),
            "lease_expires_at": _reopen("lease_expires_at", "NULL"),
            "updated_at": _now(),
        }
        async with get_session() as session:
            ins = pg_insert(EI).values(incident_key=key, **cols)
            stmt = ins.on_conflict_do_update(index_elements=["incident_key"], set_=set_) \
                      .returning(EI, literal_column("(xmax = 0)"))
            r = (await session.execute(stmt)).first()
            await session.commit()
            if r is None:
                return {"incident_key": key}, False
            return _row_to_dict(r[0]), bool(r[1])

    async def get(self, key: str) -> Optional[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_open(self) -> list[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            rows = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.resolved_at.is_(None))
            )).scalars().all()
            return [_row_to_dict(r) for r in rows]

    async def list_all(self) -> list[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        async with get_session() as session:
            rows = (await session.execute(select(ExecutionIncident))).scalars().all()
            return [_row_to_dict(r) for r in rows]

    async def _apply(self, key, where_extra, fields) -> Optional[dict]:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update, select
        cols = {k: v for k, v in fields.items() if k in _model_cols()}
        cols["updated_at"] = _now()
        async with get_session() as session:
            stmt = update(ExecutionIncident).where(
                ExecutionIncident.incident_key == key, *where_extra).values(**cols)
            res = await session.execute(stmt)
            await session.commit()
            if (res.rowcount or 0) != 1:
                return None
            row = (await session.execute(
                select(ExecutionIncident).where(ExecutionIncident.incident_key == key)
            )).scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def update(self, key: str, **fields) -> Optional[dict]:
        return await self._apply(key, (), fields)

    async def update_claimed(self, key: str, owner: str, **fields) -> Optional[dict]:
        from models.execution_incident import ExecutionIncident
        return await self._apply(key, (
            ExecutionIncident.claimed_by == owner,
            ExecutionIncident.lease_expires_at > _now(),
            ExecutionIncident.resolved_at.is_(None),
        ), fields)

    async def claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update, or_
        now = _now()
        async with get_session() as session:
            stmt = (update(ExecutionIncident).where(
                ExecutionIncident.incident_key == key,
                ExecutionIncident.resolved_at.is_(None),
                or_(ExecutionIncident.claimed_by.is_(None),
                    ExecutionIncident.lease_expires_at < now),
            ).values(claimed_by=owner, claimed_at=now, lease_expires_at=lease_until))
            res = await session.execute(stmt)
            await session.commit()
            return (res.rowcount or 0) == 1

    async def renew_claim(self, key: str, owner: str, lease_until: datetime) -> bool:
        from models.execution_incident import ExecutionIncident
        row = await self._apply(key, (
            ExecutionIncident.claimed_by == owner,
            ExecutionIncident.resolved_at.is_(None),
            ExecutionIncident.lease_expires_at > _now(),   # lease vencido não ressuscita
        ), {"lease_expires_at": lease_until})
        return row is not None

    async def release_claim(self, key: str, owner: Optional[str] = None) -> None:
        from models.execution_incident import ExecutionIncident
        where = () if owner is None else (ExecutionIncident.claimed_by == owner,)
        await self._apply(key, where, {"claimed_by": None, "claimed_at": None,
                                       "lease_expires_at": None})

    async def recover_expired_claims(self) -> int:
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import update
        now = _now()
        async with get_session() as session:
            stmt = (update(ExecutionIncident).where(
                ExecutionIncident.claimed_by.is_not(None),
                ExecutionIncident.lease_expires_at < now,
            ).values(claimed_by=None, claimed_at=None, lease_expires_at=None))
            res = await session.execute(stmt)
            await session.commit()
            return res.rowcount or 0


def _row_to_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


_repo: Any = None


def _get_repo() -> Any:
    global _repo
    if _repo is None:
        try:
            from db import DB_ENABLED
        except Exception:
            DB_ENABLED = False
        _repo = _SqlIncidentRepo() if DB_ENABLED else InMemoryIncidentRepo()
    return _repo


def set_repo(repo: Any) -> None:
    global _repo
    _repo = repo


# ════════════════════════════════════════════════════════════════════════════
#  Quarentena com OWNERSHIP — P03 arma/limpa somente o próprio latch
# ════════════════════════════════════════════════════════════════════════════
def _arm_local_latch(reason: str) -> None:
    """Arma SÓ o latch local P03 em memória (fail-closed IMEDIATO, síncrono).
    Chamado ANTES de qualquer persistência — uma falha de DB não pode liberar a
    próxima ordem do processo."""
    global _p03_latch_armed
    try:
        from services import shadow_trade_service
        shadow_trade_service._arm_execution_quarantine(reason, owner=_LATCH_OWNER)
        _p03_latch_armed = True
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha armando latch local: {exc}")


async def _arm_quarantine(reason: str) -> None:
    """Arma o latch P03 (fail-closed imediato) e persiste a pausa SEM sobrescrever
    uma pausa manual/não-P03 do operador."""
    _arm_local_latch(reason)
    try:
        from services import risk_service
        # arm ATÔMICO sob advisory lock — sem get_status→set_manual_pause (corrida).
        await risk_service.arm_p03_pause(reason)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] falha persistindo pausa RiskState: {exc}")


async def _maybe_release_quarantine() -> bool:
    """Libera SOMENTE a quarentena PRÓPRIA do P03. Chamado apenas na transição
    de ≥1 incidente para 0 (o caller garante). Exige: P03 armou o latch; zero
    incidentes abertos; **boot/scan fresh já teve sucesso**; pausa persistida é
    P03-owned. NUNCA usa clear genérico nem toca latch/pausa de P02 ou operador."""
    global _p03_latch_armed
    if await _get_repo().list_open():
        return False
    if not _p03_latch_armed:
        return False
    if not _boot_scan_safe:
        return False  # sem leitura fresh confirmada, mantém quarentena
    # Limpa APENAS o owner p03 do latch em memória.
    other_owners = True
    try:
        from services import shadow_trade_service
        shadow_trade_service.clear_execution_quarantine(owner=_LATCH_OWNER)
        _p03_latch_armed = False
        remaining = shadow_trade_service.execution_quarantine_owners()
        other_owners = bool(remaining)  # P02/legacy/manual ainda segurando?
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] falha limpando latch próprio: {exc}")
        return False
    # Só remove a pausa persistida se NÃO restam outros owners locais e a pausa
    # atual ainda é P03-owned (CAS transacional no RiskState — sem clear genérico).
    if not other_owners:
        try:
            from services import risk_service
            await risk_service.release_p03_pause(_PAUSE_MARKER)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03] falha resumindo RiskState (owner-aware): {exc}")
    log.warning("[p03] quarentena própria liberada — transição p/ zero incidentes")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Entrada oficial de incidentes (idempotente + reabre após resolução)
# ════════════════════════════════════════════════════════════════════════════
def build_incident_key(kind: str, symbol: str, *, exchange: str = EXCHANGE_BINANCE,
                       client_order_id: str = None, entry_order_id: str = None,
                       conditional_prefix: str = None, snapshot_id: str = None,
                       identity: str = None) -> str:
    ident = (client_order_id or entry_order_id or conditional_prefix
             or snapshot_id or identity or symbol)  # sem sufixo genérico "-"
    return f"{exchange}:{kind}:{symbol}:{ident}"


async def record_incident(*, kind: str, symbol: str, exchange: str = EXCHANGE_BINANCE,
                          client_order_id: str = None, entry_order_id: str = None,
                          side: str = None, planned_qty: float = None,
                          min_known_fill: float = None, planned_stop: float = None,
                          conditional_prefix: str = None, conditional_ids: dict = None,
                          safety_state: str = None, snapshot_id: str = None,
                          pending_maker: bool = None, payload: dict = None,
                          incident_key: str = None) -> dict:
    """Persiste (ou reabre/merge) um incidente e ARMA a quarentena. Fail-closed:
    o latch é armado mesmo se a persistência falhar. Idempotente por incident_key;
    incidente RESOLVIDO que reincide é REABERTO (não fica invisível pela unique key)."""
    key = incident_key or build_incident_key(
        kind, symbol, exchange=exchange, client_order_id=client_order_id,
        entry_order_id=entry_order_id, conditional_prefix=conditional_prefix,
        snapshot_id=snapshot_id)
    full_payload = dict(payload or {})
    for k, v in (("safety_state", safety_state), ("snapshot_id", snapshot_id),
                 ("pending_maker", pending_maker)):
        if v is not None:
            full_payload[k] = v
    _reason = f"incidente {kind} {symbol} ({_mask(client_order_id or entry_order_id)})"
    # Ordem CRÍTICA (invariante "incidente visível ⇒ pausa P03 persistida"):
    # (1) latch local; (2) pausa P03 PERSISTIDA; só então (3) upsert do incidente.
    # Assim o incidente nunca fica observável no banco sem a pausa já persistida.
    _arm_local_latch(_reason)
    await _arm_quarantine(_reason)      # persiste a pausa P03 ANTES de o incidente existir
    repo = _get_repo()
    created = False
    persisted = False
    row = None
    try:
        # upsert é ATÔMICO: no conflito já faz merge monotônico (GREATEST/união)
        # e reabre reincidência. Não há read-modify-write separado (evita corrida).
        row, created = await repo.upsert(key, {
            "kind": kind, "symbol": symbol, "exchange": exchange, "side": side,
            "client_order_id": client_order_id, "entry_order_id": entry_order_id,
            "planned_qty": planned_qty, "min_known_fill": min_known_fill,
            "planned_stop": planned_stop, "conditional_prefix": conditional_prefix,
            "conditional_ids": conditional_ids, "payload": full_payload or None,
            "next_retry_at": _now(),
        })
        persisted = row is not None and row.get("id") is not None
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] persistência de incidente falhou ({key}): {exc}")
    log.critical(f"[p03][incident] key={key} kind={kind} symbol={symbol} created={created} "
                 f"persisted={persisted} coid={_mask(client_order_id)} eoid={_mask(entry_order_id)} maker={pending_maker}")
    return {"incident_key": key, "created": created, "persisted": persisted,
            "state": (row or {}).get("state", State.OPEN)}


# ════════════════════════════════════════════════════════════════════════════
#  Reconciliação (fenced por owner+lease)
# ════════════════════════════════════════════════════════════════════════════
def _is_maker(inc: dict) -> bool:
    return bool((inc.get("payload") or {}).get("pending_maker"))


def _has_identity(inc: dict) -> bool:
    return bool(inc.get("client_order_id") or inc.get("entry_order_id")
                or inc.get("conditional_ids") or inc.get("conditional_prefix")
                or inc.get("kind") in _MANUAL_KINDS)


def _eligible(inc: dict) -> tuple[bool, str]:
    if inc.get("resolved_at") is not None:
        return False, "resolvido"
    if inc.get("state") == State.MANUAL_REQUIRED:
        return False, "manual_required"
    if (inc.get("exchange") or EXCHANGE_BINANCE) != EXCHANGE_BINANCE:
        return False, "exchange mismatch (bloqueado, sem mutação)"
    nra = inc.get("next_retry_at")
    if nra is not None and nra > _now():
        return False, "retry não elegível ainda"
    # NOTA: incidentes SEM identidade suficiente continuam elegíveis de propósito
    # — os caminhos de reconcile escalam para RETRY→MANUAL em vez de deixá-los
    # presos em OPEN/invisíveis. `_has_identity` orienta a decisão lá dentro.
    return True, "elegível"


async def _fenced(key: str, owner: str, **fields) -> bool:
    return (await _get_repo().update_claimed(key, owner, **fields)) is not None


async def _renew_or_abort(key: str, owner: str) -> bool:
    """Renova o lease ANTES de mutar a exchange. Se o claim foi perdido (lease
    vencido / outro dono), aborta a mutação — não cancela/cria nada."""
    return await _get_repo().renew_claim(key, owner, _now() + timedelta(seconds=RECONCILE_LEASE_S))


async def _schedule_retry(key: str, owner: str, inc: dict, state: str, reason: str) -> None:
    attempts = int(inc.get("attempts") or 0) + 1
    if attempts >= RECONCILE_MAX_ATTEMPTS and state not in _TERMINAL_SAFE:
        ok = await _fenced(key, owner, state=State.MANUAL_REQUIRED, attempts=attempts,
                           last_error=reason,
                           manual_reason=f"máx. tentativas ({attempts}) sem prova de segurança: {reason}")
        if ok:
            log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (attempts={attempts}) {reason}")
        return
    delay = _backoff_seconds(attempts)
    ok = await _fenced(key, owner, state=state, attempts=attempts, last_error=reason,
                       next_retry_at=_now() + timedelta(seconds=delay))
    if ok:
        log.warning(f"[p03][transition] {key} → {state} attempt={attempts} "
                    f"next_retry_in={delay:.0f}s reason={reason}")


async def _resolve(key: str, owner: str, state: str, reason: str) -> None:
    ok = await _fenced(key, owner, state=state, resolved_at=_now(), last_error=reason,
                       claimed_by=None, claimed_at=None, lease_expires_at=None)
    if ok:
        log.warning(f"[p03][transition] {key} → {state} (RESOLVIDO) {reason}")


async def _fresh_verdict(symbol: str) -> tuple[str, Optional[float]]:
    from services import binance_signed_service as bss
    try:
        size, _status = await bss._fresh_position_size(symbol)
    except Exception:  # noqa: BLE001
        return "UNKNOWN", None
    return position_verdict(size, size if size is None else 0.0), size


def _exact_conditional_ids(inc: dict) -> list[str]:
    """IDs EXATOS conhecidos: SL/TP1/TP2 do dict + derivados do prefixo
    (`<prefix>-sl/-tp1/-tp2`). SEM prefix match amplo."""
    ids: list[str] = []
    cids = inc.get("conditional_ids") or {}
    if isinstance(cids, dict):
        for k in ("sl", "tp1", "tp2"):
            if cids.get(k):
                ids.append(str(cids[k]))
        for k in ("all", "ids"):                 # união histórica de todos os IDs vistos
            extra = cids.get(k)
            if isinstance(extra, list):
                ids.extend(str(x) for x in extra if x)
    pref = inc.get("conditional_prefix")
    if pref:
        ids.extend([f"{pref}-sl", f"{pref}-tp1", f"{pref}-tp2"])
    # dedup preservando ordem
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _matching_orders(listing: dict, exact_ids: list[str]) -> list[dict]:
    if not exact_ids:
        return []
    want = {str(x) for x in exact_ids}
    return [o for o in (listing.get("orders") or [])
            if isinstance(o, dict) and (_order_identities(o) & want)]


async def _ensure_stop(key: str, owner: str, inc: dict, need_qty: Optional[float]) -> tuple[bool, Optional[str], str]:
    """Adota um SL vivo válido (snake_case, lado/cobertura/trigger) cobrindo
    `need_qty` = max(qty_terminal_confirmada, posição_fresh_total). Se não houver e
    as invariantes permitirem, CRIA somente SL via P02 (assinatura real) cobrindo a
    posição fresh total — não só a qty da entry. Nunca TP, nunca MARKET. Sucesso de
    criação exige `sl_ok is True` e `sl_order_id`. Retorna (ok, sl_order_id, det)."""
    from services import binance_signed_service as bss
    symbol = inc["symbol"]
    try:
        listing = await bss.get_open_algo_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"listagem algo exceção: {exc}"
    if not listing or not listing.get("ok"):
        return False, None, "listagem algo stale/erro"
    adopted, aid, detail = _adopt_live_stop(inc, need_qty, listing)
    if adopted:
        return True, aid, detail
    if not RECONCILE_CREATE_SL:
        return False, None, "sem SL vivo válido (criação desabilitada)"
    entry_side = _norm_entry_side(inc.get("side"))
    planned_stop = _finite(inc.get("planned_stop"))
    q = _finite(need_qty)
    if entry_side is None or planned_stop is None or planned_stop <= 0 or q is None or q <= 0:
        return False, None, "sem lado/stop/qty válidos p/ criar SL"
    if not await _renew_or_abort(key, owner):
        return False, None, "claim perdido antes de criar SL — abortado"
    prefix = inc.get("conditional_prefix") or _safe_prefix(key)   # sem `…` Unicode
    try:
        res = await bss.place_protection_orders(
            symbol, entry_side, q, stop_loss=planned_stop, tp1=None, tp2=None,
            client_order_id_prefix=prefix, dedup_live=True)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"criação SL exceção: {exc}"
    if res and res.get("sl_ok") is True and res.get("sl_order_id"):
        return True, str(res.get("sl_order_id")), f"SL criado ({_mask(res.get('sl_order_id'))})"
    return False, None, f"criação SL não confirmada (sl_ok={res.get('sl_ok') if res else None})"


async def _match_real_trade(symbol: str, side: Optional[str], fresh_size: Optional[float] = None,
                            identity: Optional[list] = None) -> Optional[dict]:
    """Match DETERMINÍSTICO de RealTrade(s) × posição Binance. `status=open`,
    `exchange=binance`, `source in (auto,managed)`, símbolo/quote EXATO, lado
    presente e igual. Prefere linhas por IDENTIDADE (client/exchange order id).
    Cobertura por AGREGAÇÃO de qty (sem tolerância fixa de 50%; só precisão de lote
    ~0,1%). Lado do incidente ausente → {"manual": True}. DB off → {"skip": True}.
    Sem match/cobertura → None (untracked). Retorna {"id": <trade determinístico|None>}."""
    try:
        from db import get_session, DB_ENABLED
    except Exception:
        return {"skip": True}
    if not DB_ENABLED:
        return {"skip": True}
    if _norm_entry_side(side) is None:
        return {"manual": True}       # não infere lado silenciosamente
    from models.real_trade import RealTrade
    from sqlalchemy import select
    async with get_session() as session:
        rows = (await session.execute(select(RealTrade).where(RealTrade.status == "open"))).scalars().all()
        matched = [r for r in rows if _real_trade_match(r, symbol, side)]
        if not matched:
            return None
        idvals = {str(x) for x in (identity or []) if x}
        ident_rows = [r for r in matched if idvals & {
            str(_rt_get(r, "client_order_id") or ""), str(_rt_get(r, "exchange_order_id") or "")}
        ] if idvals else []
        pool = ident_rows or matched
        need = _finite(fresh_size)
        if need is not None and need > 0:
            agg = sum((_finite(_rt_get(r, "qty")) or _finite(_rt_get(r, "qty_initial")) or 0.0) for r in pool)
            if agg + need * 0.001 + 1e-9 < need:
                return None           # cobertura agregada insuficiente → untracked
        target = ident_rows[0] if len(ident_rows) == 1 else (pool[0] if len(pool) == 1 else None)
        return {"id": (_rt_get(target, "id") if target is not None else None), "covered": True}


def _rt_get(r, name):
    return r.get(name) if isinstance(r, dict) else getattr(r, name, None)


def _real_trade_match(r, symbol: str, side: Optional[str]) -> bool:
    """Preditor PURO (testável): exchange=binance, source in (auto,managed),
    símbolo/quote EXATO, e LADO presente e IGUAL (lado ausente NÃO casa)."""
    if (_rt_get(r, "status") or "") != "open":
        return False
    if (str(_rt_get(r, "exchange") or "").strip().lower()) != EXCHANGE_BINANCE:
        return False                                      # Bybit/shadow/ausente
    if (_rt_get(r, "source") or "") not in ("auto", "managed"):
        return False                                      # manual/shadow
    if _sym_key(_rt_get(r, "symbol") or "") != _sym_key(symbol):
        return False                                      # símbolo/quote/contrato exato
    want = _norm_entry_side(side)
    rside = _norm_entry_side(_rt_get(r, "side") or _rt_get(r, "direction"))
    if want is None or rside is None or rside != want:    # lado presente e igual (obrigatório)
        return False
    return True


async def _fresh_position(symbol: str) -> dict:
    """Leitura fresh preservando símbolo/quote, LADO real e qty ABSOLUTA, com
    qualidade FRESH|UNKNOWN. stale/rate-limited/erro → UNKNOWN (nunca assume flat).
    Lado ambíguo (posições opostas no mesmo símbolo) → side=None."""
    from services import binance_signed_service as bss
    try:
        res = await bss.get_positions(symbol, force=True)
    except Exception:  # noqa: BLE001
        return {"quality": "UNKNOWN", "size": None, "side": None}
    if not res or not res.get("ok") or res.get("stale") or res.get("rate_limited"):
        return {"quality": "UNKNOWN", "size": None, "side": None}
    key = _sym_key(symbol)
    ps = [p for p in (res.get("positions") or [])
          if abs(_finite(p.get("size")) or 0) > 0 and _sym_key(p.get("symbol") or "") == key]
    if not ps:
        return {"quality": "FRESH", "size": 0.0, "side": None}
    size = sum(abs(_finite(p.get("size")) or 0.0) for p in ps)
    sides = {("sell" if str(p.get("side") or "").strip().lower() == "sell" else "buy") for p in ps}
    return {"quality": "FRESH", "size": size, "side": (next(iter(sides)) if len(sides) == 1 else None)}


async def _persist_sl_order_id(rt_id, sl_id: str) -> bool:
    """Persiste o sl_order_id no RealTrade CERTO, idempotente. Retorna True se
    gravou ou já era o mesmo; False em CONFLITO (outro ID) ou falha — nesse caso o
    caller NÃO resolve PROTECTED (preserva IDs, RETRY/MANUAL, quarentena)."""
    if not rt_id or not sl_id:
        return False
    try:
        from db import get_session, DB_ENABLED
        if not DB_ENABLED:
            return True
        from models.real_trade import RealTrade
        from sqlalchemy import update, or_
        async with get_session() as session:
            # grava só se ausente OU já igual (não sobrescreve ID de outro).
            res = await session.execute(update(RealTrade).where(
                RealTrade.id == rt_id,
                or_(RealTrade.sl_order_id.is_(None), RealTrade.sl_order_id == str(sl_id)),
            ).values(sl_order_id=str(sl_id)))
            await session.commit()
            return (res.rowcount or 0) == 1     # 0 → conflito (ID diferente já lá)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[p03] persistir sl_order_id falhou: {exc}")
        return False


async def _resolve_protected(key: str, owner: str, inc: dict, sl_id: Optional[str],
                             detail: str, fresh_size: Optional[float] = None) -> None:
    """PROTECTED só com RealTrade DETERMINÍSTICO (exchange/símbolo/lado/qty
    agregada/identidade) E lado fresh == lado do incidente. Lado ausente/mismatch →
    MANUAL. Sem match → UNTRACKED_POSITION/MANUAL. Falha/conflito ao persistir o
    sl_order_id → NÃO PROTECTED (mantém quarentena)."""
    # Lado fresh deve bater com o incidente (nunca PROTECTED em mismatch).
    fp = await _fresh_position(inc["symbol"])
    inc_side = _norm_entry_side(inc.get("side"))
    fresh_side = _norm_entry_side(fp.get("side"))
    if inc_side is None or (fresh_side is not None and inc_side is not None and fresh_side != inc_side):
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason=f"lado fresh/incidente ausente ou divergente (fresh={fp.get('side')} inc={inc.get('side')})")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (lado fresh/incidente)")
        return
    identity = [inc.get("client_order_id"), inc.get("entry_order_id")]
    rt = await _match_real_trade(inc["symbol"], inc.get("side"), fresh_size, identity=identity)
    if isinstance(rt, dict) and rt.get("manual"):
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason="lado do incidente ausente — RealTrade não pode ser inferido")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (lado ausente)")
        return
    if rt is None:
        await record_incident(kind=Kind.UNTRACKED_POSITION, symbol=inc["symbol"],
                              side=inc.get("side"), planned_qty=inc.get("planned_qty"),
                              payload={"protected_without_real_trade": True})
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason="posição protegida SEM RealTrade correspondente — untracked/manual")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (protegida sem RealTrade)")
        return
    # Persiste o sl_order_id SÓ no RealTrade determinístico (rt['id']); se ambíguo
    # (múltiplos sem identidade) rt['id'] é None → não grava em trade errado.
    if sl_id and rt.get("id"):
        if not await _persist_sl_order_id(rt.get("id"), sl_id):
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                  "persistência do sl_order_id falhou/conflito — mantém quarentena")
            return
    await _resolve(key, owner, State.PROTECTED, detail)


async def _reconcile_entry(key: str, owner: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    coid = inc.get("client_order_id")
    oid = inc.get("entry_order_id")
    try:
        order_res = await bss.get_order(inc["symbol"], order_id=oid, client_order_id=coid)
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"get_order exceção: {exc}")
        return
    verdict, qty, why = classify_entry(order_res)

    # MAKER viva (NEW/PARTIALLY_FILLED): (1) lower-bound monotônico do fill;
    # (2) posição fresh; (3) proteção PROVISÓRIA da exposição já observada;
    # (4-5) cancelar pelo ID e validar ok; (6-7) re-consultar e exigir terminal.
    # NUNCA fallback MARKET. Cancel incerto/ainda-não-terminal mantém quarentena
    # COM a exposição observada protegida.
    if verdict == "RETRY" and _is_maker(inc):
        status = (order_res.get("status") or "").upper()
        if status in ("NEW", "PARTIALLY_FILLED"):
            obs = _finite(order_res.get("executed_qty"))
            if obs and obs > 0:
                await _fenced(key, owner, min_known_fill=max(_finite(inc.get("min_known_fill")) or 0.0, obs))
            pv0, size0 = await _fresh_verdict(inc["symbol"])
            # Classificação HONESTA (sem default "protegida"): só afirma proteção
            # após _ensure_stop confirmar SL válido em posição OPEN.
            if pv0 == "OPEN":
                _prot_ok, _psl, _prot = await _ensure_stop(key, owner, inc, max(_finite(size0) or 0.0, obs or 0.0))
                _prot_lbl = f"exposição protegida ({_prot})" if _prot_ok else f"SL_NOT_CONFIRMED ({_prot})"
            elif pv0 == "UNKNOWN":
                _prot_lbl = "UNKNOWN_NO_SL (posição pós-fill incerta)"
            else:  # FLAT com entry ainda pendente/incerta
                _prot_lbl = "PENDING_ORDER_UNPROTECTED (entry pendente, sem posição)"
            if not await _renew_or_abort(key, owner):
                return
            try:
                cancel = await bss.cancel_order(inc["symbol"], order_id=oid, client_order_id=coid)
            except Exception as exc:  # noqa: BLE001
                cancel = {"ok": False, "error": str(exc)}
            if not (cancel and cancel.get("ok")):
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                      f"cancel maker incerto — quarentena mantida ({_prot_lbl}; {cancel.get('error') or cancel.get('msg')})")
                return
            try:
                order_res = await bss.get_order(inc["symbol"], order_id=oid, client_order_id=coid)
            except Exception as exc:  # noqa: BLE001
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"re-query pós-cancel: {exc}")
                return
            verdict, qty, why = classify_entry(order_res)
            if verdict == "RETRY":
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                      f"maker cancelada mas ainda não-terminal — {_prot_lbl}")
                return

    if verdict == "FLAT":
        # REJECTED/terminal-zero: NÃO vai direto a FLAT se há identidade condicional
        # — confirma fresh-flat, cancela IDs exatos e aplica grace via cleanup.
        if _exact_conditional_ids(inc):
            await _reconcile_cleanup(key, owner, inc)
        else:
            await _resolve(key, owner, State.FLAT, f"entrada sem fill ({why})")
        return
    if verdict == "RETRY":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, why)
        return
    if verdict == "FILL_UNKNOWN":
        if inc.get("kind") != Kind.FINAL_FILL_QTY_UNKNOWN:
            await _fenced(key, owner, kind=Kind.FINAL_FILL_QTY_UNKNOWN)
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"qty terminal desconhecida: {why}")
        return

    # PROTECTED tentativo: fill terminal confirmado. Exige posição fresh + SL.
    pv, _size = await _fresh_verdict(inc["symbol"])
    if pv == "UNKNOWN":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição UNKNOWN pós-fill")
        return
    if pv == "FLAT":
        # Terminal sem fill / fresh-flat: se houve tentativa de proteção (há
        # identidade de condicionais), reconcilia o cleanup ANTES de ir a FLAT —
        # não resolve FLAT deixando condicional exata possivelmente órfã.
        if _exact_conditional_ids(inc):
            await _reconcile_cleanup(key, owner, inc)
        else:
            await _resolve(key, owner, State.FLAT, "posição fresh-flat pós-terminal")
        return
    need = max(_finite(qty) or 0.0, _finite(_size) or 0.0)   # cobre a posição fresh total
    ok, sl_id, detail = await _ensure_stop(key, owner, inc, need)
    if ok:
        await _resolve_protected(key, owner, inc, sl_id, f"fill {qty:g} protegido ({detail})", fresh_size=_size)
    else:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"SL_NOT_CONFIRMED: {detail}")


async def _cancel_extra_conditionals(key: str, owner: str, inc: dict, keep_id: Optional[str]) -> tuple[bool, str]:
    """Cancela SÓ as condicionais EXATAS do incidente que NÃO são o SL cardinal
    (`keep_id`) — duplicatas/TP antigos. Nunca toca o SL cardinal, ordem sem
    identidade exata, ou ordem de outro trade. Exige `cancel.ok` + reconfirmação."""
    from services import binance_signed_service as bss
    exact_ids = _exact_conditional_ids(inc)
    if not exact_ids:
        return True, "sem condicionais conhecidas"
    try:
        listing = await bss.get_open_algo_orders(inc["symbol"])
    except Exception as exc:  # noqa: BLE001
        return False, f"listagem exceção: {exc}"
    if not listing or not listing.get("ok"):
        return False, "listagem stale/erro"
    extras = [o for o in _matching_orders(listing, exact_ids)
              if str(o.get("algo_id") or o.get("algoId") or "") != str(keep_id or "")]
    if not extras:
        return True, "nenhum extra exato vivo"
    all_ok = True
    for o in extras:
        aid = o.get("algo_id") or o.get("algoId")
        if not aid:
            all_ok = False
            continue
        # Renova/valida o lease IMEDIATAMENTE antes de CADA mutação (A1, A2, …).
        # Se expirou após A1, NÃO executa A2 e aborta sem tocar mais nada.
        if not await _renew_or_abort(key, owner):
            return False, "lease perdido no meio do cancelamento — abortado"
        try:
            res = await bss.cancel_algo_order(str(aid))
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        if not (res and res.get("ok")):   # ok=False NÃO é sucesso
            all_ok = False
    try:
        l2 = await bss.get_open_algo_orders(inc["symbol"])
    except Exception:  # noqa: BLE001
        return False, "recheck exceção"
    if not l2 or not l2.get("ok"):
        return False, "recheck stale/erro"
    still = [o for o in _matching_orders(l2, exact_ids)
             if str(o.get("algo_id") or o.get("algoId") or "") != str(keep_id or "")]
    if all_ok and not still:
        return True, "extras cancelados e ausentes"
    return False, "cancel incerto/extra persiste"


async def _reconcile_cleanup(key: str, owner: str, inc: dict) -> None:
    from services import binance_signed_service as bss
    pv, size = await _fresh_verdict(inc["symbol"])
    if pv == "UNKNOWN":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição/listagem incerta")
        return
    if pv == "OPEN":
        # (1) garante 1 SL cardinal válido cobrindo a posição fresh TOTAL (nunca
        # cancela o SL cardinal); (2) cancela SÓ extras exatos do incidente
        # (duplicatas/TP antigos), reconfirma ausência; (3) só então PROTECTED.
        need = max(_finite(size) or 0.0, _finite(inc.get("planned_qty")) or 0.0,
                   _finite(inc.get("min_known_fill")) or 0.0)
        ok, sl_id, detail = await _ensure_stop(key, owner, inc, need)
        if not ok:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"posição aberta SL_NOT_CONFIRMED: {detail}")
            return
        cleaned, why2 = await _cancel_extra_conditionals(key, owner, inc, keep_id=sl_id)
        if not cleaned:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"extras exatos não confirmados: {why2}")
            return
        await _resolve_protected(key, owner, inc, sl_id, f"posição aberta re-protegida ({detail})", fresh_size=size)
        return

    # FLAT. Sem identidade confiável de condicionais → NÃO resolve por "nenhum
    # match"; mantém retry até MANUAL. FLAT exige identidade suficiente.
    exact_ids = _exact_conditional_ids(inc)
    if not exact_ids:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "cleanup sem identidade de condicionais — não resolve por ausência")
        return
    try:
        listing = await bss.get_open_algo_orders(inc["symbol"])
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"listagem algo exceção: {exc}")
        return
    if not listing or not listing.get("ok"):
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "listagem algo stale/erro")
        return
    matches = _matching_orders(listing, exact_ids)
    if matches:
        all_ok = True
        for o in matches:
            algo_id = o.get("algo_id") or o.get("algoId")
            if not algo_id:
                all_ok = False
                continue
            if not await _renew_or_abort(key, owner):   # lease por CADA mutação
                return
            try:
                res = await bss.cancel_algo_order(str(algo_id))
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            if not (res and res.get("ok")):     # ok=False NÃO é sucesso
                all_ok = False
        # Re-consulta pra confirmar ausência; zera clean (reaparecimento).
        try:
            listing2 = await bss.get_open_algo_orders(inc["symbol"])
        except Exception:  # noqa: BLE001
            listing2 = {"ok": False}
        still = _matching_orders(listing2, exact_ids) if listing2.get("ok") else ["?"]
        await _fenced(key, owner, clean_observations=0)
        if all_ok and not still:
            await _schedule_retry(key, owner, inc, State.OPEN,
                                  "condicional cancelada — reconfirmar ausência no próximo ciclo")
        else:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                  "cancel incerto/condicional persiste — mantém quarentena")
        return

    # Nenhuma condicional exata + listagem limpa → observação separada.
    clean = int(inc.get("clean_observations") or 0) + 1
    await _fenced(key, owner, clean_observations=clean)
    if clean >= RECONCILE_CLEAN_GRACE:
        await _resolve(key, owner, State.FLAT, f"fresh-flat + cleanup confirmado ({clean} ciclos)")
    else:
        await _schedule_retry(key, owner, inc, State.OPEN,
                              f"grace {clean}/{RECONCILE_CLEAN_GRACE} (aguardando novo ciclo)")


async def _reconcile_manual_kind(key: str, owner: str, inc: dict) -> None:
    await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                  manual_reason=inc.get("manual_reason")
                  or f"{inc.get('kind')} exige intervenção humana (não pode ser fechada automaticamente)")
    log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({inc.get('kind')})")


async def _reconcile_one(key: str, owner: str) -> None:
    repo = _get_repo()
    inc = await repo.get(key)
    if not inc:
        return
    ok, why = _eligible(inc)
    if not ok:
        if "mismatch" in why:
            await repo.update(key, last_error=why)  # registra motivo, sem mutação
        return
    if not await _fenced(key, owner, state=State.RECONCILING):
        return  # claim perdido (lease vencido / outro dono) → não processa
    inc = await repo.get(key) or inc
    kind = inc.get("kind")
    try:
        if kind in _ENTRY_KINDS:
            await _reconcile_entry(key, owner, inc)
        elif kind in _CLEANUP_KINDS:
            await _reconcile_cleanup(key, owner, inc)
        elif kind in _MANUAL_KINDS:
            await _reconcile_manual_kind(key, owner, inc)
        else:
            await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"kind desconhecido {kind}")
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"reconcile exceção: {exc}")


async def reconcile_due() -> dict:
    """Um ciclo: recupera claims expirados, processa incidentes elegíveis (claim
    atômico + fencing por owner/lease). Libera a quarentena própria SÓ na transição
    de ≥1 incidente para 0 — nada de clear/log nos ciclos comuns."""
    global _last_reconciliation_at, _prev_open_count, _prev_boot_safe
    repo = _get_repo()
    await repo.recover_expired_claims()
    # Boot inseguro (leitura stale/erro): re-tenta o scan fresh de posições a cada
    # ciclo — só libera quando `_boot_scan_safe` virar True numa leitura bem-sucedida.
    if not _boot_scan_safe:
        try:
            await _detect_untracked_positions()
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03] re-scan de boot falhou: {exc}")
    processed = 0
    for inc in await repo.list_open():
        if processed >= RECONCILE_MAX_PER_CYCLE:
            break
        if not _eligible(inc)[0]:
            continue
        key = inc["incident_key"]
        if not await repo.claim(key, _PROCESS_ID, _now() + timedelta(seconds=RECONCILE_LEASE_S)):
            continue
        try:
            await _reconcile_one(key, _PROCESS_ID)
            processed += 1
        finally:
            cur = await repo.get(key)
            if cur and cur.get("resolved_at") is None:
                await repo.release_claim(key, owner=_PROCESS_ID)  # só o próprio claim
    open_now = len(await repo.list_open())
    # Enquanto houver incidente aberto, garante o owner P03 armado (mesmo após uma
    # tentativa manual de resume que possa ter limpado o latch).
    if open_now > 0:
        try:
            from services import shadow_trade_service
            if _LATCH_OWNER not in shadow_trade_service.execution_quarantine_owners():
                await _arm_quarantine(f"re-arm: {open_now} incidente(s) aberto(s)")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03] re-arm do latch falhou: {exc}")
    released = False
    # Libera na transição >0→0 OU na recuperação de boot inseguro→seguro com zero
    # incidentes (0→0) — sem clear/log nos ciclos comuns já-estáveis.
    transition_to_zero = _prev_open_count > 0 and open_now == 0
    boot_recovery = (open_now == 0 and _boot_scan_safe and not _prev_boot_safe and _p03_latch_armed)
    if transition_to_zero or boot_recovery:
        released = await _maybe_release_quarantine()
    _prev_open_count = open_now
    _prev_boot_safe = _boot_scan_safe
    _last_reconciliation_at = _now().isoformat()
    return {"processed": processed, "open_now": open_now, "quarantine_released": released}


async def boot_reconcile() -> dict:
    """Boot: recupera claims, ARMA quarentena ANTES de qualquer scan se houver
    incidente aberto, detecta untracked (fail-closed) e faz 1 ciclo. QUALQUER
    falha (DB/leitura) ARMA o latch P03 e mantém o bloqueio (fail-closed real)."""
    global _prev_open_count
    repo = _get_repo()
    try:
        await repo.recover_expired_claims()
        open_incs = await repo.list_open()
    except Exception as exc:  # noqa: BLE001
        await _arm_quarantine(f"boot: falha lendo incidentes ({exc})")
        log.critical(f"[p03][boot] falha lendo incidentes — quarentena armada (fail-closed): {exc}")
        return {"open_incidents": None, "untracked": None, "boot_error": str(exc)}
    if open_incs:
        await _arm_quarantine(f"boot: {len(open_incs)} incidente(s) aberto(s)")
        log.critical(f"[p03][boot] {len(open_incs)} incidente(s) aberto(s) — quarentena armada")
    _prev_open_count = len(open_incs)
    scan = await _detect_untracked_positions()
    try:
        await reconcile_due()
    except Exception as exc:  # noqa: BLE001
        # Ciclo inicial falhou → boot NÃO é seguro; arma latch (não loga como ok).
        _mark_boot_unsafe()
        await _arm_quarantine(f"boot: ciclo inicial falhou ({exc})")
        log.critical(f"[p03][boot] ciclo inicial falhou — quarentena armada (boot inseguro): {exc}")
    return {"open_incidents": len(open_incs), "untracked": scan.get("count"),
            "scan_status": scan.get("status"), "boot_scan_safe": _boot_scan_safe}


async def _detect_untracked_positions() -> dict:
    """Posições Binance fresh × RealTrade aberto. Retorna status EXPLÍCITO
    (NÃO o mesmo `0` para tudo): {"status": FLAT|UNTRACKED|UNKNOWN, "count": n}.
    Seta `_boot_scan_safe` só quando a leitura fresh tem SUCESSO. Leitura
    indisponível/stale/rate-limited → UNKNOWN + ARMA quarentena (não assume flat)."""
    global _boot_scan_safe
    try:
        from services import binance_signed_service as bss
        if not bss.is_configured():
            _boot_scan_safe = True   # sem exchange real → nada a escanear
            return {"status": "FLAT", "count": 0}
        res = await bss.get_positions(force=True)
    except Exception as exc:  # noqa: BLE001
        _boot_scan_safe = False
        await _arm_quarantine(f"boot: leitura de posições indisponível ({exc})")
        log.critical(f"[p03][boot] leitura de posições indisponível — quarentena armada: {exc}")
        return {"status": "UNKNOWN", "count": 0}
    if not res or not res.get("ok") or res.get("stale") or res.get("rate_limited"):
        _boot_scan_safe = False
        await _arm_quarantine("boot: posições stale/rate-limited (não assumo flat)")
        log.critical("[p03][boot] posições stale/incertas — quarentena armada (não assumo flat)")
        return {"status": "UNKNOWN", "count": 0}
    positions = [p for p in (res.get("positions") or []) if abs(_finite(p.get("size")) or 0) > 0]
    if not positions:
        _boot_scan_safe = True          # leitura fresh confirmou conta flat
        return {"status": "FLAT", "count": 0}
    try:
        open_trades = await _open_real_trades()   # linhas completas p/ o MESMO matcher
    except Exception as exc:  # noqa: BLE001
        _boot_scan_safe = False
        await _arm_quarantine(f"boot: leitura RealTrade falhou ({exc})")
        log.critical(f"[p03][boot] leitura RealTrade falhou — quarentena armada: {exc}")
        return {"status": "UNKNOWN", "count": 0}
    n = 0
    all_persisted = True
    for p in positions:
        sym = p.get("symbol") or ""
        norm = sym.replace("USDT", "/USDT:USDT") if "/" not in sym else sym
        side = ("sell" if str(p.get("side") or "").strip().lower() == "sell" else "buy")
        # MESMO matcher rigoroso do reconciliador (exchange/source/símbolo/quote/lado).
        if any(_real_trade_match(t, norm, side) for t in open_trades):
            continue
        res_inc = await record_incident(kind=Kind.UNTRACKED_POSITION, symbol=norm, side=side,
                                        min_known_fill=abs(_finite(p.get("size")) or 0),
                                        payload={"detected_at_boot": True})
        if res_inc.get("persisted"):
            n += 1
        else:
            all_persisted = False   # latch armado, mas UNTRACKED não persistiu
    if not all_persisted:
        # Não incrementar como persistido, não permitir release, continuar tentando.
        _boot_scan_safe = False
        log.critical("[p03][boot] UNTRACKED não persistido — boot inseguro (UNKNOWN)")
        return {"status": "UNKNOWN", "count": n}
    _boot_scan_safe = True              # leitura fresh + persistência OK
    if n:
        log.critical(f"[p03][boot] {n} posição(ões) UNTRACKED → pausa persistente")
    return {"status": ("UNTRACKED" if n else "FLAT"), "count": n}


async def _open_real_trades() -> list:
    """Linhas RealTrade abertas (campos p/ o matcher rigoroso). NÃO usa símbolo
    como prova de tracking — o boot aplica `_real_trade_match` igual ao reconciliador."""
    from db import get_session, DB_ENABLED
    if not DB_ENABLED:
        return []
    from models.real_trade import RealTrade
    from sqlalchemy import select
    async with get_session() as session:
        rows = (await session.execute(select(RealTrade).where(RealTrade.status == "open"))).scalars().all()
        return [{"status": r.status, "exchange": getattr(r, "exchange", None),
                 "source": getattr(r, "source", None), "symbol": r.symbol,
                 "side": getattr(r, "side", None), "qty": getattr(r, "qty", None),
                 "qty_initial": getattr(r, "qty_initial", None),
                 "client_order_id": getattr(r, "client_order_id", None),
                 "exchange_order_id": getattr(r, "exchange_order_id", None), "id": r.id}
                for r in rows]


# ── Task integrada ao lifespan (NÃO é worker/scheduler separado) ─────────────
async def loop() -> None:
    global _reconciler_running
    _reconciler_running = True
    log.info(f"[p03] reconciliador iniciado (intervalo {RECONCILE_INTERVAL_S:.0f}s, proc={_PROCESS_ID}).")
    try:
        while True:
            try:
                await reconcile_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(f"[p03] ciclo falhou: {exc}")
            await asyncio.sleep(RECONCILE_INTERVAL_S)
    finally:
        _reconciler_running = False


# ── API read-only ────────────────────────────────────────────────────────────
def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _summ(inc: dict) -> dict:
    return {
        "incident_id": inc.get("id"), "incident_key": inc.get("incident_key"),
        "symbol": inc.get("symbol"), "side": inc.get("side"), "kind": inc.get("kind"),
        "state": inc.get("state"),
        "qty_known": inc.get("planned_qty") if inc.get("planned_qty") is not None else inc.get("min_known_fill"),
        "attempts": inc.get("attempts"), "last_error": inc.get("last_error"),
        "manual_reason": inc.get("manual_reason"),
        "next_retry_at": _iso(inc.get("next_retry_at")), "updated_at": _iso(inc.get("updated_at")),
    }


async def get_status() -> dict:
    repo = _get_repo()
    try:
        rows = await repo.list_all()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "reconciler_running": _reconciler_running}
    open_rows = [r for r in rows if r.get("resolved_at") is None]
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r.get("state")] = by_state.get(r.get("state"), 0) + 1
    quarantine_active = False
    try:
        from services import shadow_trade_service
        quarantine_active = bool(shadow_trade_service.execution_quarantine_reason())
    except Exception:
        pass
    return {
        "ok": True, "reconciler_running": _reconciler_running,
        "last_reconciliation_at": _last_reconciliation_at,
        "quarantine_active": quarantine_active,
        "open_total": len(open_rows),
        "retry_pending": sum(1 for r in open_rows if r.get("state") == State.RETRY_PENDING),
        "manual_required": sum(1 for r in open_rows if r.get("state") == State.MANUAL_REQUIRED),
        "protected": by_state.get(State.PROTECTED, 0),
        "flat": by_state.get(State.FLAT, 0),
        "items": [_summ(r) for r in open_rows],
        "manual_items": [_summ(r) for r in open_rows if r.get("state") == State.MANUAL_REQUIRED],
    }
