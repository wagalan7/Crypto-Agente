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
from typing import Any, Dict, Mapping, Optional

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
    #: Terminal ESPECÍFICO: posição aberta do operador, reconhecida
    #: explicitamente. NÃO é FLAT (a posição existe) e NÃO é PROTECTED
    #: (reconhecer não certifica proteção). Só o incidente
    #: `UNTRACKED_POSITION` daquela posição pode terminar assim.
    MANUAL_ACKNOWLEDGED = "MANUAL_ACKNOWLEDGED"


#: Estados que PROVAM segurança de uma observação de ENTRADA. Note que
#: `MANUAL_ACKNOWLEDGED` NÃO entra aqui: reconhecer uma posição do operador não
#: diz NADA sobre o desfecho de uma ordem despachada pelo bot — usá-lo como
#: prova liquidaria intenção sem evidência.
_TERMINAL_SAFE = {State.PROTECTED, State.FLAT}
#: Estados em que o incidente está ENCERRADO com desfecho conhecido (inclui o
#: reconhecimento manual). Só para contabilidade de ciclo/escalonamento.
_TERMINAL_CLOSED = _TERMINAL_SAFE | {State.MANUAL_ACKNOWLEDGED}
#: Desfecho COMPROVADO de um id despachado. `TERMINAL_ZERO` exige consulta
#: terminal da própria identidade com quantidade final zero — FLAT, ausência de
#: campo e lower-bound zero continuam sendo DESCONHECIDO.
PROOF_POSITIVE = "POSITIVE"
PROOF_TERMINAL_ZERO = "TERMINAL_ZERO"
PROOF_UNKNOWN = "UNKNOWN"
#: Duas respostas TERMINAIS incompatíveis para a MESMA ordem (fill positivo e
#: zero final). Nenhuma das duas é descartada e NADA é liquidado em cima disso.
PROOF_CONFLICT = "CONFLICT"
CONFLICT_REASON = "ENTRY_PROOF_CONFLICT"
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
# Lock LOCAL (mesmo processo) compartilhado por record_incident/_arm/_maybe_release
# — o advisory lock protege PROCESSOS diferentes; este protege arm/release
# concorrentes DENTRO do mesmo processo. Nenhum caminho P03 arma/limpa o latch fora
# deste protocolo.
_P03_LOCAL_LOCK = asyncio.Lock()


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
    Usa a `submitted_qty` confirmada pelo executor antes do fallback local;
    `local_client_order_id`/`local_planned_qty` cobrem callers legados. Reconhece
    o contrato maker real (incl. emissão GTX ambígua sem `status`) e evita
    falso-pending quando `entry_order_terminal=true`."""
    order_res = order_res or {}
    rec = rec or {}
    coid = (order_res.get("client_order_id") or local_client_order_id
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
    submitted_qty = _finite(order_res.get("submitted_qty"))
    if submitted_qty is not None and submitted_qty <= 0:
        submitted_qty = None
    planned_qty = (
        submitted_qty if submitted_qty is not None
        else (
            local_planned_qty if local_planned_qty is not None
            else (rec.get("qty") or rec.get("position_size"))
        )
    )
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
# ALLOWLIST EXPLÍCITA de estados VIVOS de uma conditional (algoStatus da corretora).
# Só adota/revalida SL cujo status esteja AQUI. Ausente/desconhecido/MYSTERY OU
# qualquer estado não reconhecido ⇒ REJEITA (nunca adota "no escuro").
_LIVE_ALGO_STATUS = {"NEW", "WORKING", "UNTRIGGERED", "ACTIVE", "PENDING", "PENDING_NEW"}


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
        if ostatus not in _LIVE_ALGO_STATUS:
            continue      # ausente/dead/MYSTERY/não-reconhecido → não adota (allowlist)
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

    async def list_by_client_ids(self, ids) -> list[dict]:
        """Incidentes de ids EFETIVAMENTE despachados, de QUALQUER kind — a prova
        do desfecho não depende de como o incidente foi classificado."""
        want = {str(i) for i in (ids or []) if i}
        if not want:
            return []
        return [dict(r) for r in self._rows.values()
                if str(r.get("client_order_id") or "") in want]

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

    @staticmethod
    def _build_upsert_stmt(key: str, defaults: dict):
        """Constrói o INSERT … ON CONFLICT DO UPDATE … RETURNING (puro, sem sessão).
        Contrato JSON tri-state: conditional_ids/payload SEMPRE objeto ou SQL NULL —
        NULLIF(...,'null') normaliza JSON-null, e só se opera sobre objetos jsonb."""
        from models.execution_incident import ExecutionIncident as EI
        from sqlalchemy import text, literal_column
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        # aceita SÓ dict ou None (rejeita array/escalar fail-closed); omite None
        cols = {}
        for k, v in defaults.items():
            if k not in _model_cols():
                continue
            if k in ("conditional_ids", "payload"):
                if v is None:
                    continue                       # omite → SQL NULL (none_as_null)
                if not isinstance(v, dict):
                    raise ValueError(f"{k} deve ser dict ou None, não {type(v).__name__}")
            cols[k] = v
        T = "execution_incidents"

        def _co(c):
            return text(f"COALESCE({T}.{c}, EXCLUDED.{c})")

        def _reopen(c, reopened):
            return text(f"CASE WHEN {T}.resolved_at IS NOT NULL THEN {reopened} ELSE {T}.{c} END")

        def _obj(colexpr):   # NULL/JSON-null/não-objeto → '{}'::jsonb
            n = f"COALESCE(NULLIF({colexpr}::jsonb,'null'::jsonb),'{{}}'::jsonb)"
            return f"(CASE WHEN jsonb_typeof({n})='object' THEN {n} ELSE '{{}}'::jsonb END)"

        def _arr(objexpr, k):  # elementos de ->k só quando é array
            return (f"jsonb_array_elements_text(CASE WHEN jsonb_typeof({objexpr}->'{k}')='array' "
                    f"THEN {objexpr}->'{k}' ELSE '[]'::jsonb END)")

        def _jm(c):   # merge de objetos (payload): resultado objeto → JSON
            return text(f"(({_obj(f'{T}.{c}')} || {_obj(f'EXCLUDED.{c}')}))::json")

        CJ = _obj(f"{T}.conditional_ids")
        EJ = _obj("EXCLUDED.conditional_ids")
        cond_merge = text(
            f"(({CJ} || {EJ}) "
            f"|| jsonb_build_object('all', (SELECT COALESCE(jsonb_agg(DISTINCT v),'[]'::jsonb) FROM ("
            f"  SELECT {_arr(CJ, 'all')} AS v"
            f"  UNION SELECT {_arr(EJ, 'all')}"
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
            "manual_reason": _reopen("manual_reason", "NULL"), "last_error": _reopen("last_error", "NULL"),
            "next_retry_at": _reopen("next_retry_at", "CURRENT_TIMESTAMP"),
            "claimed_by": _reopen("claimed_by", "NULL"), "claimed_at": _reopen("claimed_at", "NULL"),
            "lease_expires_at": _reopen("lease_expires_at", "NULL"),
            "updated_at": _now(),
        }
        ins = pg_insert(EI).values(incident_key=key, **cols)
        return ins.on_conflict_do_update(index_elements=["incident_key"], set_=set_) \
                  .returning(EI, literal_column("(xmax = 0)"))

    async def upsert_in_session(self, session, key: str, defaults: dict) -> tuple[dict, bool]:
        """Executa o upsert numa sessão JÁ ABERTA (NÃO abre sessão, NÃO faz commit).
        Usado pelo caminho transacional único pausa+incidente."""
        r = (await session.execute(self._build_upsert_stmt(key, defaults))).first()
        if r is None:
            return {"incident_key": key}, False
        return _row_to_dict(r[0]), bool(r[1])

    async def upsert(self, key: str, defaults: dict) -> tuple[dict, bool]:
        """Wrapper que abre sessão + commit (caminho legado/testes diretos)."""
        from db import get_session
        async with get_session() as session:
            res = await self.upsert_in_session(session, key, defaults)
            await session.commit()
            return res

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

    async def list_by_client_ids(self, ids) -> list[dict]:
        """Incidentes de ids EFETIVAMENTE despachados, de QUALQUER kind — a prova
        do desfecho não depende de como o incidente foi classificado."""
        from db import get_session
        from models.execution_incident import ExecutionIncident
        from sqlalchemy import select
        want = sorted({str(i) for i in (ids or []) if i})
        if not want:
            return []
        async with get_session() as session:
            rows = (await session.execute(
                select(ExecutionIncident)
                .where(ExecutionIncident.client_order_id.in_(want)))).scalars().all()
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
    uma pausa manual/não-P03 do operador. Compartilha o MESMO `_P03_LOCAL_LOCK` de
    record/release: nenhum interleaving arm↔release pode deixar pausa persistida com
    o owner local P03 desligado."""
    async with _P03_LOCAL_LOCK:
        _arm_local_latch(reason)
        try:
            from services import risk_service
            # arm ATÔMICO sob advisory lock — sem get_status→set_manual_pause (corrida).
            await risk_service.arm_p03_pause(reason)
        except Exception as exc:  # noqa: BLE001
            log.critical(f"[p03] falha persistindo pausa RiskState: {exc}")


async def _manual_cause_pending() -> bool:
    """Há causa MANUAL pendente (conta bloqueada ou falha local não persistida)?

    Zero incidentes NÃO remove essa causa: ela é estado durável próprio.
    """
    try:
        from services import manual_position_service as mps
        if mps.pending_validation_failure() is not None:
            return True
        estado = await mps.account_validation_state()
        return bool(estado.get("blocked"))
    except Exception as exc:  # noqa: BLE001 — dúvida mantém a contenção
        log.error(f"[p03][manual-ack] estado de validação ilegível: "
                  f"{type(exc).__name__}: {exc}")
        return True


async def _maybe_release_quarantine() -> bool:
    """Release owner-aware do P03 sob LOCK LOCAL. Ordem OBRIGATÓRIA: valida
    boot_scan_safe → chama o release NO BANCO PRIMEIRO (transação + advisory lock,
    reconfirma zero incidentes lá dentro) → só com RELEASED/SAFE_OTHER_OWNER limpa o
    latch owner="p03". STILL_OPEN/ERROR/exceção → mantém/rearma o latch, retorna
    False, NÃO loga sucesso. Nunca limpa antes da resposta do banco; nunca clear
    genérico; nunca libera só com list_open() fora da transação."""
    global _p03_latch_armed
    async with _P03_LOCAL_LOCK:
        if not _p03_latch_armed:
            return False
        if not _boot_scan_safe:
            return False
        # Causa MANUAL pendente mantém a contenção mesmo com zero incidentes.
        if await _manual_cause_pending():
            _arm_local_latch("validação manual pendente — contenção mantida")
            return False
        try:
            from services import risk_service
            result = await risk_service.release_p03_pause(_PAUSE_MARKER)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03] release exceção — mantém latch: {exc}")
            _arm_local_latch("release exceção — re-armado")
            return False
        if result in (risk_service.RELEASE_RELEASED, risk_service.RELEASE_SAFE_OTHER_OWNER):
            # Banco CONFIRMOU (zero incidentes). Só AGORA limpa o latch owner p03.
            try:
                from services import shadow_trade_service
                shadow_trade_service.clear_execution_quarantine(owner=_LATCH_OWNER)
                _p03_latch_armed = False
            except Exception as exc:  # noqa: BLE001
                log.error(f"[p03] falha limpando latch p03 pós-release: {exc}")
                return False
            log.warning(f"[p03] quarentena própria liberada ({result})")
            return True
        # STILL_OPEN / ERROR → mantém latch, sem sucesso.
        _arm_local_latch(f"release não concluído ({result}) — latch mantido")
        return False


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


async def persist_incident_with_p03_pause(key: str, cols: dict, reason: str) -> tuple:
    """Caminho de PRODUÇÃO (P03.1E): UMA sessão, UM commit. Pausa P03 e incidente
    no MESMO commit sob `pg_advisory_xact_lock`. Garante o invariante observável por
    outra conexão: EXISTE incidente não resolvido ⇒ risk_state.trading_paused=true.
    Rollback desfaz pausa e incidente juntos. Retorna (row, created, persisted)."""
    repo = _get_repo()
    try:
        from db import DB_ENABLED
    except Exception:
        DB_ENABLED = False
    # DB off ou repo em memória (testes): sem transação real — latch já cobre; usa
    # upsert em memória + arm best-effort. persisted reflete a gravação.
    if not DB_ENABLED or not isinstance(repo, _SqlIncidentRepo):
        # Sem Postgres real não há transação; preserva a ORDEM (pausa ANTES do
        # incidente visível) armando a pausa primeiro, depois o upsert.
        try:
            from services import risk_service
            await risk_service.arm_p03_pause(reason)
        except Exception:  # noqa: BLE001
            pass
        try:
            row, created = await repo.upsert(key, cols)
        except Exception as exc:  # noqa: BLE001
            log.critical(f"[p03] persist (memória) falhou ({key}): {exc}")
            return None, False, False
        return row, created, (row is not None and row.get("id") is not None)
    # PostgreSQL real: transação única pausa+incidente.
    from db import get_session
    from sqlalchemy import text
    from services import risk_service
    try:
        async with get_session() as session:
            async with session.begin():                     # BEGIN … COMMIT único
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                      {"k": risk_service._P03_PAUSE_LOCK})
                await risk_service.ensure_p03_pause_in_session(session, reason)  # pausa P03
                row, created = await repo.upsert_in_session(session, key, cols)  # incidente
            # ao sair de session.begin() o COMMIT já ocorreu (pausa+incidente juntos)
        return row, created, (row is not None and row.get("id") is not None)
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03] transação pausa+incidente falhou (rollback, nada parcial): {exc}")
        return None, False, False


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
    cols = {
        "kind": kind, "symbol": symbol, "exchange": exchange, "side": side,
        "client_order_id": client_order_id, "entry_order_id": entry_order_id,
        "planned_qty": planned_qty, "min_known_fill": min_known_fill,
        "planned_stop": planned_stop, "conditional_prefix": conditional_prefix,
        "conditional_ids": (conditional_ids or None), "payload": (full_payload or None),
        "next_retry_at": _now(),
    }
    # Caminho ÚNICO sob lock LOCAL: latch local → 1 transação (advisory lock +
    # RiskState pausado + upsert do incidente) → 1 commit. Invariante:
    # incidente visível ⇒ pausa P03 já persistida (mesmo commit). Erro → rollback
    # (nada parcial) + persisted=False; latch local permanece armado.
    async with _P03_LOCAL_LOCK:
        _arm_local_latch(_reason)
        row, created, persisted = await persist_incident_with_p03_pause(key, cols, _reason)
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
    if attempts >= RECONCILE_MAX_ATTEMPTS and state not in _TERMINAL_CLOSED:
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


def _explicit_side(raw) -> Optional[str]:
    """Lado EXPLÍCITO de uma posição: buy/long→'buy', sell/short→'sell'. Ausente/
    inválido/desconhecido → None. NUNCA assume 'buy' por omissão (proíbe `else buy`)."""
    s = str(raw or "").strip().lower()
    if s in ("sell", "short"):
        return "sell"
    if s in ("buy", "long"):
        return "buy"
    return None


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
        # O ID precisa ficar rastreável ANTES de qualquer ação seguinte do caller
        # (cancel maker, cleanup de extras, retry ou crash). A gravação dentro de
        # `_resolve_protected` continua como defesa idempotente.
        if not await _persist_sl_in_incident(key, owner, inc, str(aid)):
            return False, str(aid), "SL adotado, mas sl_id não foi persistido no incidente"
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
    # Invariante #6: além do pré-check acima, o lease é REVALIDADO imediatamente antes
    # de CADA POST interno (SL principal + fallback quantity). Se o claim expirar/mudar
    # de dono no meio, nenhum POST é enviado (fail-closed).
    async def _guard() -> bool:
        return await _renew_or_abort(key, owner)
    try:
        res = await bss.place_protection_orders(
            symbol, entry_side, q, stop_loss=planned_stop, tp1=None, tp2=None,
            client_order_id_prefix=prefix, dedup_live=True, mutation_guard=_guard)
    except Exception as exc:  # noqa: BLE001
        return False, None, f"criação SL exceção: {exc}"
    if res and res.get("sl_ok") is True and res.get("sl_order_id"):
        sl_id = str(res.get("sl_order_id"))
        # Persistência IMEDIATA: nenhum retorno/cancelamento posterior pode deixar
        # um SL recém-criado sem identidade no incidente.
        if not await _persist_sl_in_incident(key, owner, inc, sl_id):
            return False, sl_id, "SL criado, mas sl_id não foi persistido no incidente"
        return True, sl_id, f"SL criado ({_mask(sl_id)})"
    return False, None, f"criação SL não confirmada (sl_ok={res.get('sl_ok') if res else None})"


def _symbol_step(symbol: str) -> float:
    """stepSize do símbolo a partir do cache de exchangeInfo JÁ carregado — leitura
    PURA (sem rede: nunca dispara `_load_exchange_info`). Cache frio/indisponível →
    0.0 → tolerância de cobertura zero (exata, fail-closed)."""
    try:
        from services import binance_signed_service as bss
        bsym = bss.to_binance(symbol) if "/" in symbol else symbol
        cache = getattr(bss, "_filters_cache", None) or {}
        return float((cache.get(bsym) or {}).get("step") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


class Coverage:
    """Estados da cobertura RealTrade × posição fresh (invariante #4)."""
    COVERED = "COVERED"            # alvo determinístico + agregação ≥ posição (dentro do stepSize)
    INSUFFICIENT = "INSUFFICIENT"  # alvo determinístico mas soma < posição (exposição descoberta)
    NO_MATCH = "NO_MATCH"          # nenhum RealTrade determinístico casou (untracked)
    AMBIGUOUS = "AMBIGUOUS"        # múltiplos sem identidade única → alvo indeterminado
    UNKNOWN = "UNKNOWN"            # need/qty desconhecidos → cobertura não confirmável


def _coverage_verdict(pool: list, ident_rows: list, need_qty: Optional[float],
                      step: Optional[float]) -> tuple[str, Optional[object]]:
    """Puro/testável. Decide o alvo DETERMINÍSTICO e a cobertura por AGREGAÇÃO
    Decimal, com tolerância = stepSize do símbolo (SEM 0,1%/50% arbitrários).
    Retorna (verdict ∈ Coverage.*, target_id|None). AMBIGUOUS/NO_MATCH → target None.
    Falta de qty ou need desconhecido → UNKNOWN (nunca COVERED por omissão)."""
    from decimal import Decimal, InvalidOperation
    if not pool:
        return Coverage.NO_MATCH, None
    # alvo determinístico: 1 por identidade, ou pool unitário; senão ambíguo.
    if len(ident_rows) == 1:
        target = ident_rows[0]
    elif len(pool) == 1:
        target = pool[0]
    else:
        return Coverage.AMBIGUOUS, None
    target_id = _rt_get(target, "id")
    need = _finite(need_qty)
    if need is None or need <= 0:
        return Coverage.UNKNOWN, target_id       # sem posição fresh conhecida → não confirma
    try:
        agg = Decimal("0")
        for r in pool:
            q = _finite(_rt_get(r, "qty"))
            if q is None:
                q = _finite(_rt_get(r, "qty_initial"))
            if q is None:
                return Coverage.UNKNOWN, target_id   # qty ausente em alguma linha → não confirma
            agg += Decimal(str(q))
        tol = Decimal(str(step)) if (step and step > 0) else Decimal("0")
        need_d = Decimal(str(need))
    except (InvalidOperation, ValueError):
        return Coverage.UNKNOWN, target_id
    if agg + tol < need_d:                        # falta mais que um stepSize → descoberto
        return Coverage.INSUFFICIENT, target_id
    return Coverage.COVERED, target_id


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
        # Incidente COM identidade: exige match POR identidade. 0 matches ⇒ NO_MATCH
        # (nunca faz fallback pro único item do pool: identidade A + só trade B ⇒ B
        # não é o alvo — untracked/manual, jamais PROTECTED).
        if idvals and not ident_rows:
            return None
        # stepSize do símbolo → tolerância de LOTE p/ cobertura (sem 0,1%/50%).
        step = _symbol_step(symbol)
        verdict, target_id = _coverage_verdict(matched, ident_rows, fresh_size, step)
        if verdict in (Coverage.NO_MATCH, Coverage.INSUFFICIENT):
            return None                 # untracked/exposição descoberta → sem RealTrade determinístico
        if verdict == Coverage.AMBIGUOUS:
            return {"id": None, "verdict": verdict, "ambiguous": True}
        if verdict == Coverage.UNKNOWN:
            return {"id": target_id, "verdict": verdict, "unknown": True}
        return {"id": target_id, "verdict": Coverage.COVERED, "covered": True}


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
    # Lado EXPLÍCITO por posição (nunca `else buy`). Ausente/ambíguo (None presente
    # ou lados opostos) → side=None → o portão fresh trata como SIDE_UNKNOWN.
    sides = {_explicit_side(p.get("side")) for p in ps}
    side = next(iter(sides)) if len(sides) == 1 else None
    return {"quality": "FRESH", "size": size, "side": side}


class FreshGate:
    """Portão de leitura fresh ANTES de qualquer mutação de proteção."""
    OPEN_VALID = "OPEN_VALID"        # FRESH, size>0, lado presente e == incidente → pode mutar
    UNKNOWN = "UNKNOWN"              # leitura incerta (stale/erro) → RETRY, nunca muta
    FLAT = "FLAT"                    # size 0 → sem posição p/ proteger
    SIDE_UNKNOWN = "SIDE_UNKNOWN"    # posição aberta mas lado fresh/incidente ausente/ambíguo → MANUAL
    SIDE_MISMATCH = "SIDE_MISMATCH"  # lado fresh ≠ lado do incidente → MANUAL


async def _fresh_gate(inc: dict) -> tuple[str, dict]:
    """Lê a posição fresh (`_fresh_position`, com quality/size/side) e classifica se
    é SEGURO mutar proteção. UNKNOWN nunca vira FLAT/OPEN. Lado ausente/divergente
    nunca vira OPEN_VALID. Retorna (FreshGate.*, fresh_dict)."""
    fp = await _fresh_position(inc["symbol"])
    quality = fp.get("quality")
    size = _finite(fp.get("size"))
    if quality != "FRESH":
        return FreshGate.UNKNOWN, fp
    if size is None or size <= 0:
        return FreshGate.FLAT, fp
    inc_side = _norm_entry_side(inc.get("side"))
    fresh_side = _norm_entry_side(fp.get("side"))
    if inc_side is None or fresh_side is None:
        return FreshGate.SIDE_UNKNOWN, fp
    if fresh_side != inc_side:
        return FreshGate.SIDE_MISMATCH, fp
    return FreshGate.OPEN_VALID, fp


def _revalidate_stop_by_id(inc: dict, sl_id: str, need_qty: Optional[float], listing: dict) -> tuple[bool, str]:
    """Revalida DETERMINISTICAMENTE o SL pelo id EXATO na relistagem: presença;
    status VIVO (allowlist); tipo STOP_MARKET; lado de fechamento; símbolo/quote;
    cobertura (closePosition OU reduce_only qty≥need_qty); trigger na tolerância P02.
    Ausente ou qualquer validação falha → (False, motivo). Nunca confia só no POST."""
    entry_side = _norm_entry_side(inc.get("side"))
    if entry_side is None:
        return False, "side do incidente desconhecido"
    want_close = _close_side(entry_side)
    planned_stop = _finite(inc.get("planned_stop"))
    inc_key = _sym_key(inc.get("symbol"))
    want = {str(sl_id)}
    for o in (listing.get("orders") or []):
        if not isinstance(o, dict) or not (_order_identities(o) & want):
            continue
        # achou pelo id EXATO — agora precisa passar em TODAS as validações
        otype = str(o.get("type") or o.get("origType") or "").upper()
        if otype not in _ACCEPTED_STOP_TYPES:
            return False, f"tipo {otype or 'ausente'} ≠ STOP_MARKET"
        ostatus = str(o.get("status") or o.get("algoStatus") or "").upper()
        if ostatus not in _LIVE_ALGO_STATUS:
            return False, f"status {ostatus or 'ausente'} não-vivo (allowlist)"
        if str(o.get("side") or "").upper() != want_close:
            return False, "lado de fechamento incorreto"
        if not o.get("symbol") or _sym_key(o.get("symbol")) != inc_key:
            return False, "símbolo/quote divergente"
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
            return False, "cobertura insuficiente (nem closePosition nem reduceOnly≥need)"
        if planned_stop is not None and planned_stop > 0:
            trig = _finite(o.get("trigger_price") if o.get("trigger_price") is not None
                           else (o.get("triggerPrice") or o.get("stopPrice")))
            if trig is None or abs(trig - planned_stop) / planned_stop > RECONCILE_STOP_TOL_FRAC:
                return False, "trigger fora da tolerância P02"
        return True, f"SL {_mask(sl_id)} revalidado (vivo/lado/cobertura/trigger)"
    return False, f"SL {_mask(sl_id)} ausente na relistagem (nunca PROTECTED só pelo POST)"


async def _persist_sl_in_incident(key: str, owner: str, inc: dict, sl_id: str) -> bool:
    """Grava o sl_id no incidente (conditional_ids.sl + união histórica .all) FENCED
    por owner+lease, ANTES de resolver PROTECTED — mantém o SL rastreável mesmo que a
    revalidação/PROTECTED falhe. Retorna True se gravou (ou já constava)."""
    if not sl_id:
        return False
    cur = inc.get("conditional_ids") if isinstance(inc.get("conditional_ids"), dict) else {}
    merged = _merge_conditional_ids(cur, {"sl": str(sl_id)})
    if await _fenced(key, owner, conditional_ids=merged):
        inc["conditional_ids"] = merged     # reflete localmente p/ os passos seguintes
        return True
    return False


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
    """Resolve PROTECTED SÓ após, nesta ordem:
      (0) persistir o sl_id no incidente (fenced) — SL rastreável;
      (1) SEGUNDA leitura fresh independente: quality==FRESH, size>0, lado==incidente;
      (2) RELISTAR e revalidar o SL pelo id EXATO (status vivo/lado/cobertura/trigger);
      (3) RealTrade DETERMINÍSTICO cobrindo a qty da 2ª leitura;
      (4) CAS do sl_order_id no RealTrade certo.
    Qualquer falha → RETRY/MANUAL, SL segue rastreável, quarentena mantida. Segunda
    leitura UNKNOWN/FLAT/sem-lado/divergente → NUNCA PROTECTED. DB off/erro → RETRY."""
    from services import binance_signed_service as bss
    # (0) grava o sl_id no incidente ANTES de qualquer resolução.
    if sl_id and not await _persist_sl_in_incident(key, owner, inc, sl_id):
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "não gravou sl_id no incidente (lease?) — mantém quarentena")
        return
    # (1) SEGUNDA leitura fresh independente (a 1ª foi antes do SL).
    fp = await _fresh_position(inc["symbol"])
    quality = fp.get("quality")
    size = _finite(fp.get("size"))
    inc_side = _norm_entry_side(inc.get("side"))
    fresh_side = _norm_entry_side(fp.get("side"))
    if quality != "FRESH":
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "2ª leitura fresh UNKNOWN — nunca PROTECTED sem confirmar posição")
        return
    if size is None or size <= 0:
        # O SL já foi persistido no incidente. Não resolver FLAT enquanto essa
        # condicional exata puder continuar viva: encaminha diretamente ao cleanup
        # usando a leitura FLAT que acabamos de confirmar e mantém a quarentena.
        await _reconcile_cleanup(key, owner, inc, confirmed_flat=True)
        return
    if inc_side is None or fresh_side is None or fresh_side != inc_side:
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason=f"2ª leitura: lado ausente/divergente (fresh={fp.get('side')} inc={inc.get('side')})")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (2ª leitura lado)")
        return
    # (2) RELISTAR e revalidar o SL pelo id EXATO (nunca confia só na resposta do POST).
    if not sl_id:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "sem sl_id p/ revalidar — mantém quarentena")
        return
    try:
        listing = await bss.get_open_algo_orders(inc["symbol"])
    except Exception as exc:  # noqa: BLE001
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, f"relistagem SL exceção: {exc}")
        return
    if not listing or not listing.get("ok"):
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "relistagem SL stale/erro — mantém quarentena")
        return
    revalid_ok, revalid_det = _revalidate_stop_by_id(inc, sl_id, size, listing)
    if not revalid_ok:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              f"SL não revalidado ({revalid_det}) — nunca PROTECTED só pelo POST")
        return
    # (3) RealTrade DETERMINÍSTICO usando a qty da SEGUNDA leitura (não a 1ª).
    identity = [inc.get("client_order_id"), inc.get("entry_order_id")]
    rt = await _match_real_trade(inc["symbol"], inc.get("side"), size, identity=identity)
    if isinstance(rt, dict) and rt.get("skip"):
        # DB indisponível/erro → NUNCA PROTECTED (nunca "seguro por falta de banco").
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "RealTrade indisponível (DB off/erro) — nunca PROTECTED; mantém quarentena")
        return
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
    if rt.get("verdict") == Coverage.UNKNOWN:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "cobertura RealTrade não confirmável — mantém quarentena")
        return
    if not rt.get("id"):
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason="RealTrade ambíguo (múltiplos sem identidade única) — alvo indeterminado, sem PROTECTED")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (RealTrade ambíguo, target_id=None)")
        return
    # (4) CAS do sl_order_id no RealTrade determinístico.
    if not await _persist_sl_order_id(rt.get("id"), sl_id):
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "persistência do sl_order_id falhou/conflito — mantém quarentena")
        return
    await _resolve(key, owner, State.PROTECTED, f"{detail}; {revalid_det}")


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
            # Portão fresh ANTES de qualquer mutação (proteção OU cancel). Lado fresh
            # ausente/divergente do incidente maker → ZERO mutações → MANUAL.
            gate, fp = await _fresh_gate(inc)
            if gate in (FreshGate.SIDE_UNKNOWN, FreshGate.SIDE_MISMATCH):
                await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                              manual_reason=f"maker lado {gate} (fresh={fp.get('side')} inc={inc.get('side')}) — zero mutações")
                log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (maker {gate}, pré-mutação)")
                return
            # Classificação HONESTA (sem default "protegida"): só afirma proteção
            # após _ensure_stop confirmar SL válido em posição OPEN com lado correto.
            if gate == FreshGate.OPEN_VALID:
                _prot_ok, _psl, _prot = await _ensure_stop(key, owner, inc, max(_finite(fp.get("size")) or 0.0, obs or 0.0))
                _prot_lbl = f"exposição protegida ({_prot})" if _prot_ok else f"SL_NOT_CONFIRMED ({_prot})"
            elif gate == FreshGate.UNKNOWN:
                # UNKNOWN nunca autoriza mutação, inclusive cancelamento da maker.
                await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                                      "posição UNKNOWN com maker viva — zero mutações; mantém quarentena")
                return
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
        # Zero FINAL comprovado pela consulta da PRÓPRIA identidade: a prova é
        # gravada ANTES de resolver, porque é ela (não o FLAT da posição) que
        # autoriza encerrar a intenção como "não executou".
        registrada = await _persist_entry_proof(
            key, owner, inc, state=PROOF_TERMINAL_ZERO, client_order_id=coid,
            qty=0.0, status=(order_res or {}).get("status"))
        if registrada == PROOF_CONFLICT:
            # Esta MESMA ordem já provou fill: contradição não resolve nada.
            await _halt_on_proof_conflict(key, owner, inc)
            return
        # A consulta atual pode ter criado conflito com OUTRO incidente do
        # mesmo dispatch. Reconfere antes de limpar/cancelar qualquer proteção.
        if await _halt_if_conflicting(key, owner, inc):
            return
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

    # PROTECTED tentativo: fill terminal confirmado. Portão fresh ANTES de mutar:
    # exige quality FRESH, size>0 e LADO fresh == lado do incidente.
    # A quantidade EXECUTADA fica gravada ANTES de qualquer desfecho: sem ela,
    # resolver FLAT (posição já encerrada) seria indistinguível de "não houve
    # execução" e a intenção encerraria sem rastro contábil.
    _qty_terminal = _finite(qty)
    if _qty_terminal is not None and _qty_terminal > 0:
        _lower = max(_finite(inc.get("min_known_fill")) or 0.0, _qty_terminal)
        if await _fenced(key, owner, min_known_fill=_lower):
            inc["min_known_fill"] = _lower
        registrada = await _persist_entry_proof(
            key, owner, inc, state=PROOF_POSITIVE, client_order_id=coid,
            qty=_qty_terminal, status=(order_res or {}).get("status"))
        if registrada == PROOF_CONFLICT:
            # Esta MESMA ordem já provou zero final: contradição não resolve nada.
            await _halt_on_proof_conflict(key, owner, inc)
            return
        if await _halt_if_conflicting(key, owner, inc):
            return  # prova positiva nova também pode contradizer um irmão
    gate, fp = await _fresh_gate(inc)
    if gate == FreshGate.UNKNOWN:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição UNKNOWN pós-fill — sem mutação")
        return
    if gate == FreshGate.FLAT:
        # Terminal sem fill / fresh-flat: se houve tentativa de proteção (há
        # identidade de condicionais), reconcilia o cleanup ANTES de ir a FLAT —
        # não resolve FLAT deixando condicional exata possivelmente órfã.
        if _exact_conditional_ids(inc):
            await _reconcile_cleanup(key, owner, inc)
        else:
            await _resolve(key, owner, State.FLAT, "posição fresh-flat pós-terminal")
        return
    if gate in (FreshGate.SIDE_UNKNOWN, FreshGate.SIDE_MISMATCH):
        # Lado fresh ausente/divergente do incidente → NENHUMA mutação de proteção.
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason=f"lado fresh {gate} (fresh={fp.get('side')} inc={inc.get('side')}) — nenhuma proteção criada/adotada")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({gate}, pré-mutação)")
        return
    # OPEN_VALID: lado confere → cria/adota SL cobrindo a posição fresh total.
    size = _finite(fp.get("size"))
    need = max(_finite(qty) or 0.0, size or 0.0)
    ok, sl_id, detail = await _ensure_stop(key, owner, inc, need)
    if ok:
        await _resolve_protected(key, owner, inc, sl_id, f"fill {qty:g} protegido ({detail})", fresh_size=size)
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
            res = await bss.cancel_algo_order(str(aid), symbol=inc.get("symbol"))
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


async def _reconcile_cleanup(key: str, owner: str, inc: dict, *, confirmed_flat: bool = False) -> None:
    """Reconcilia condicionais do incidente.

    `confirmed_flat=True` é usado somente quando `_resolve_protected` acabou de
    obter a segunda leitura FRESH/FLAT. Isso evita uma nova leitura e, sobretudo,
    impede resolver FLAT antes de cancelar/reconfirmar as condicionais exatas.
    """
    from services import binance_signed_service as bss
    if confirmed_flat:
        gate, fp = FreshGate.FLAT, {"quality": "FRESH", "size": 0.0, "side": None}
    else:
        gate, fp = await _fresh_gate(inc)
    if gate == FreshGate.UNKNOWN:
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING, "posição/listagem incerta")
        return
    if gate in (FreshGate.SIDE_UNKNOWN, FreshGate.SIDE_MISMATCH):
        # Lado fresh ausente/divergente → NENHUMA proteção mutada; escala MANUAL.
        await _fenced(key, owner, state=State.MANUAL_REQUIRED,
                      manual_reason=f"cleanup lado {gate} (fresh={fp.get('side')} inc={inc.get('side')}) — zero mutações")
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED (cleanup {gate}, pré-mutação)")
        return
    if gate == FreshGate.OPEN_VALID:
        size = _finite(fp.get("size"))
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
                res = await bss.cancel_algo_order(str(algo_id), symbol=inc.get("symbol"))
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


# ── Re-checagem de UNTRACKED_POSITION preso em MANUAL_REQUIRED ──────────────
#: Intervalo mínimo entre duas leituras fresh do MESMO incidente (não martela a
#: exchange: o operador leva minutos/horas para fechar a posição dele).
UNTRACKED_RECHECK_S = _f("P03_UNTRACKED_RECHECK_S", 900.0)
_untracked_recheck_at: dict = {}


async def capture_manual_context(*, scope=None, symbol=None):
    """Captura a identidade ANTES de qualquer leitura externa do ciclo.

    Chamada SEMPRE antes de `get_positions(force=True)`/listagens: o resultado
    de um GET não pode fechar/validar o que mudou depois dele.
    """
    try:
        from services import manual_position_service as mps
        return await mps.capture_validation_context(
            scope=scope or mps.SCOPE_ACCOUNT, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][manual-ack] captura de contexto falhou: "
                  f"{type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": "MANUAL_ACK_CONTEXT_ERROR"}


async def _revalidate_manual_acks(raw_positions, *, source_ok: bool = True,
                                  context=None) -> dict:
    """Revalida reconhecimentos com a leitura de CONTA já feita no ciclo.

    `context` é a captura ANTERIOR à leitura; sem ela nada é publicado ou
    encerrado (novo ciclo é agendado). Nunca gasta uma segunda chamada à
    exchange e nunca converte erro em "sem reconhecimento".
    """
    try:
        from services import manual_position_service as mps
        observacao = mps.observation_from_rows(raw_positions, source_ok=source_ok)
        return await mps.revalidate_active(observation=observacao, context=context)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][manual-ack] revalidação falhou: {type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": "MANUAL_ACK_REVALIDATION_ERROR"}


async def _revalidate_manual_acks_fresh() -> dict:
    """Observação de CONTA própria, para o ciclo que não fez leitura de posições.

    Integrada ao `reconcile_due` SEM depender de `_boot_scan_safe` nem da
    existência de incidentes abertos. Antes de recuperar qualquer autorização,
    uma falha PENDENTE local é persistida (§7.1.5).
    """
    try:
        from services import manual_position_service as mps
        if mps.pending_validation_failure() is not None:
            await mps.flush_pending_validation_failure()
        registro = await mps.active_acknowledgements()
        if not registro["ok"]:
            return {"ok": False, "reason_code": registro["reason_code"]}
        estado = await mps.account_validation_state()
        if not registro["acks"] and not estado.get("blocked"):
            return {"ok": True, "reason_code": "NO_BLOCKING_ACKS", "valid": []}
        # Captura ANTES do GET; a transação fecha antes da rede.
        contexto = await mps.capture_validation_context(scope=mps.SCOPE_ACCOUNT)
        observacao = await mps.observe_positions()
        return await mps.revalidate_active(observation=observacao, context=contexto)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][manual-ack] revalidação periódica falhou: "
                  f"{type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": "MANUAL_ACK_REVALIDATION_ERROR"}


async def _manual_ack_outcome(inc: dict) -> dict:
    """Desfecho do incidente UNTRACKED à luz do reconhecimento manual.

    Só devolve `MANUAL_ACKNOWLEDGED` quando: existe reconhecimento ACTIVE para
    AQUELE símbolo, a leitura fresca mostra UMA perna e a identidade observada
    é IDÊNTICA à reconhecida. Qualquer dúvida mantém o incidente aberto — e
    posição aberta jamais vira FLAT ou PROTECTED por este caminho.
    """
    simbolo = inc.get("symbol")
    try:
        from services import manual_position_service as mps
        vinculo = await mps.ack_for_symbol(simbolo)
        if not vinculo["ok"]:
            return {"state": None, "reason": f"registro manual ilegível ({vinculo['reason_code']})"}
        ack = vinculo["ack"]
        if ack is None:
            return {"state": None, "reason": "sem reconhecimento manual para o símbolo"}
        if str(ack.get("state")) != "ACTIVE":
            # INVALIDATED/WAITING_ORDERS continuam bloqueando o símbolo, mas
            # NÃO encerram o incidente: falta confirmação nova.
            return {"state": None,
                    "reason": f"reconhecimento em {ack.get('state')} — não encerra a causa"}
        # Captura ANTES do GET: um desfecho não pode encerrar a causa de um
        # reconhecimento criado/alterado depois da leitura.
        contexto = await mps.capture_validation_context(
            scope=mps.SCOPE_SYMBOL, symbol=mps.canonical_symbol(simbolo))
        if not contexto.get("ok"):
            return {"state": None,
                    "reason": f"contexto indisponível ({contexto.get('reason_code')})"}
        observacao = await mps.observe_positions(mps.canonical_symbol(simbolo))
        if not observacao["ok"]:
            return {"state": None,
                    "reason": f"leitura fresca indisponível ({observacao['reason_code']})"}
        confere = await mps.revalidate_active(observation=observacao,
                                              context=contexto)
        if confere.get("reason_code") == mps.STALE_CONTEXT:
            return {"state": None, "reason": "contexto vencido — novo ciclo"}
        if not confere["ok"]:
            return {"state": None, "reason": f"revalidação indisponível ({confere['reason_code']})"}
        if ack.get("id") not in (confere.get("valid") or []):
            return {"state": None,
                    "reason": "identidade manual divergente — autorização invalidada"}
        return {"state": State.MANUAL_ACKNOWLEDGED,
                "reason": (f"posição manual reconhecida (ack #{ack.get('id')}, "
                           f"fingerprint {str(ack.get('fingerprint'))[:12]}…); "
                           "o bot não administra esta posição")}
    except Exception as exc:  # noqa: BLE001
        return {"state": None, "reason": f"verificação manual falhou ({type(exc).__name__})"}


async def _manual_ack_for_symbol(symbol) -> dict:
    """Reconhecimento ACTIVE daquele símbolo. Erro ⇒ `ok=False` (mantém bloqueio)."""
    try:
        from services import manual_position_service as mps
        return await mps.ack_for_symbol(symbol)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": "MANUAL_ACK_REGISTRY_ERROR",
                "detail": type(exc).__name__, "ack": None}


async def _manual_symbol_free_of_orders(symbol) -> dict:
    """Ausência FRESCA de ordens/condicionais do operador naquele símbolo."""
    try:
        from services import manual_position_service as mps
        return await mps.symbol_has_live_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": "MANUAL_ORDERS_UNKNOWN",
                "detail": type(exc).__name__}


async def recheck_untracked_manual() -> dict:
    """Reavalia a CAUSA de incidentes `UNTRACKED_POSITION` em `MANUAL_REQUIRED`.

    Motivo: `_eligible` exclui `MANUAL_REQUIRED` e nenhum fluxo resolvia este
    kind, então o incidente ficava aberto para sempre — mesmo depois de o
    operador fechar a posição. Como qualquer incidente aberto mantém a
    quarentena e bloqueia o resume manual, o bot ficava parado por dias com a
    causa já extinta (observado em produção em 21–24/09/2026).

    Regra: leitura FRESH do PRÓPRIO símbolo do incidente; só `FLAT` comprovado
    resolve, como `FLAT` e com motivo auditável. Stale, erro, lado ambíguo ou
    posição ainda aberta MANTÊM o incidente (fail-closed). Nada é fechado ou
    cancelado na exchange, e posições não rastreadas de OUTROS símbolos (as
    manuais do operador) não interferem neste veredicto.
    """
    repo = _get_repo()
    try:
        rows = await repo.list_open()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][untracked-recheck] leitura de incidentes falhou: {exc}")
        return {"checked": 0, "resolved": 0, "kept": {}, "error": str(exc)}
    now = _now()
    checked = resolved = 0
    kept: dict = {}
    for inc in rows:
        if (inc.get("kind") != Kind.UNTRACKED_POSITION
                or inc.get("state") != State.MANUAL_REQUIRED):
            continue
        key = inc.get("incident_key")
        if not key:
            continue
        last = _untracked_recheck_at.get(key)
        if last is not None and (now - last).total_seconds() < UNTRACKED_RECHECK_S:
            continue
        _untracked_recheck_at[key] = now
        checked += 1
        # Contexto capturado ANTES da leitura de flat e das duas consultas de
        # ordens: nenhuma delas pode encerrar um reconhecimento mais novo.
        contexto_ciclo = await capture_manual_context(
            scope=None, symbol=None)
        gate, _fp = await _fresh_gate(inc)
        if gate != FreshGate.FLAT:
            # Posição ABERTA (ou incerta). Único desfecho possível aqui é o
            # reconhecimento manual explícito — nunca FLAT, nunca PROTECTED.
            desfecho = await _manual_ack_outcome(inc)
            if desfecho["state"] is None:
                kept[key] = gate
                await repo.update(key, last_error=f"re-check untracked: {gate}")
                continue
            if not await repo.claim(key, _PROCESS_ID,
                                    _now() + timedelta(seconds=RECONCILE_LEASE_S)):
                kept[key] = "CLAIM_PERDIDO"
                continue
            await _resolve(key, _PROCESS_ID, desfecho["state"], desfecho["reason"])
            resolved += 1
            continue
        # FLAT comprovado. Havendo reconhecimento manual ATIVO, o registro só é
        # encerrado depois de provar que NÃO restam ordens/condicionais do
        # operador — e nenhuma delas é cancelada pelo bot. Sem reconhecimento,
        # o caminho legado segue igual.
        vinculo = await _manual_ack_for_symbol(inc.get("symbol"))
        if not vinculo.get("ok"):
            kept[key] = "MANUAL_ACK_UNVERIFIED"
            await repo.update(key, last_error="re-check untracked: registro manual ilegível")
            continue
        if not contexto_ciclo.get("ok"):
            kept[key] = "MANUAL_ACK_CONTEXT_UNAVAILABLE"
            await repo.update(key, last_error="re-check untracked: contexto indisponível")
            continue
        if vinculo.get("ack") is not None:
            ordens = await _manual_symbol_free_of_orders(inc.get("symbol"))
            if not ordens.get("ok"):
                kept[key] = "MANUAL_ORDERS_UNKNOWN"
                await repo.update(key, last_error="re-check untracked: ordens do símbolo incertas")
                continue
            if ordens.get("live"):
                kept[key] = "MANUAL_ORDERS_LIVE"
                await repo.update(key, last_error="re-check untracked: ordens manuais ainda vivas")
                continue
        if not await repo.claim(key, _PROCESS_ID,
                                _now() + timedelta(seconds=RECONCILE_LEASE_S)):
            kept[key] = "CLAIM_PERDIDO"
            continue
        await _resolve(key, _PROCESS_ID, State.FLAT,
                       "untracked: símbolo confirmado fresh-flat na re-checagem")
        resolved += 1
    if resolved:
        log.warning(f"[p03][untracked-recheck] {resolved} incidente(s) resolvido(s) "
                    f"por posição inexistente; {len(kept)} mantido(s)")
    return {"checked": checked, "resolved": resolved, "kept": kept}


# ── Intenções de entrada (P03) dentro do ciclo de reconciliação ─────────────
def _intent_symbol(stored: Optional[str]) -> Optional[str]:
    """`BTC-USDT-USDT` (forma da identidade) → `BTC/USDT:USDT` (forma do mercado).

    Forma inesperada volta como está: um símbolo irreconhecível faz a leitura
    falhar e o incidente permanece — nunca vira "flat" presumido.
    """
    if not isinstance(stored, str) or not stored:
        return None
    if "/" in stored or ":" in stored:
        return stored
    parts = stored.rsplit("-", 1)
    if len(parts) != 2 or "-" not in parts[0]:
        return stored
    return parts[0].replace("-", "/", 1) + ":" + parts[1]


async def _real_trade_for_dispatch(dispatch_ids, *, symbol: str,
                                   exchange: Optional[str] = None,
                                   side: Optional[str] = None) -> Optional[int]:
    """RealTrade vinculado a QUALQUER id efetivamente despachado desta decisão.

    O `client_order_id` é a identidade da conta que despachou, mas ele sozinho
    não basta: símbolo, exchange e LADO precisam bater, e o alvo precisa ser
    ÚNICO. Trade ABERTO ou já FECHADO vale igual — o que importa é existir
    rastro contábil do que foi executado.
    """
    try:
        from db import DB_ENABLED, get_session
        from models.real_trade import RealTrade
        from sqlalchemy import select
    except Exception:  # noqa: BLE001
        return None
    if not DB_ENABLED or not dispatch_ids:
        return None
    try:
        async with get_session() as session:
            rows = (await session.execute(
                select(RealTrade.id, RealTrade.symbol, RealTrade.exchange, RealTrade.side)
                .where(RealTrade.client_order_id.in_(list(dispatch_ids)))
                .order_by(RealTrade.id))).all()
    except Exception:  # noqa: BLE001
        return None
    alvo_sym = _sym_key(symbol)
    alvo_exc = str(exchange or EXCHANGE_BINANCE).strip().lower()
    alvo_lado = _norm_entry_side(side)
    ids = set()
    for rid, rsym, rexc, rside in rows:
        if _sym_key(rsym or "") != alvo_sym:
            continue                                   # símbolo/quote/contrato
        if str(rexc or "").strip().lower() != alvo_exc:
            continue                                   # outra corretora
        if alvo_lado is not None and _norm_entry_side(rside) != alvo_lado:
            continue                                   # lado divergente
        ids.add(int(rid))
    if len(ids) != 1:
        return None            # 0 → sem vínculo; >1 → ambíguo, nunca escolhe
    return ids.pop()


async def _settle_intent_from_proof(row: dict) -> dict:
    """Resolve a INTENÇÃO quando a reconciliação já provou o desfecho econômico.

    A prova é o incidente RESOLVIDO de cada id efetivamente despachado — de
    QUALQUER kind, porque quem classificou o incidente (executor ou ciclo) não
    muda o desfecho econômico:
      • todos `FLAT`  ⇒ não houve execução ⇒ intenção TERMINAL;
      • algum `PROTECTED` ⇒ houve execução ⇒ intenção CONFIRMED vinculada ao
        RealTrade correspondente (reserva vira exposição real).
    Faltando prova de QUALQUER id (inclusive a filha `-mfb`), nada é resolvido:
    primária rejeitada não prova ausência de fill na filha. Consulta incerta,
    ordem viva, ACK e MANUAL_REQUIRED também não provam desfecho.
    """
    from services import entry_intent_service as intents
    from db import get_session
    repo = _get_repo()
    symbol = _intent_symbol(row.get("symbol"))
    ids = [i for i in (row.get("dispatch_ids") or []) if i]
    if not ids and row.get("client_order_id"):
        ids = [row["client_order_id"]]
    if not symbol or not ids:
        return {"resolved": False, "unproven_ids": [], "reason": "IDENTITY_INCOMPLETE"}
    try:
        incidents = await repo.list_by_client_ids(ids)
    except Exception as exc:  # noqa: BLE001
        # Sem leitura da prova não se afirma nada (nunca "resolvido por falta de banco").
        log.warning(f"[p03][intents] prova indisponível ({exc})")
        return {"resolved": False, "unproven_ids": ids, "reason": "PROOF_UNAVAILABLE"}
    por_id: dict[str, list] = {str(i): [] for i in ids}
    sym_key = _sym_key(symbol)
    exchange = row.get("exchange") or EXCHANGE_BINANCE
    for incident in incidents:
        coid = str(incident.get("client_order_id") or "")
        if coid not in por_id:
            continue
        if _sym_key(incident.get("symbol") or "") != sym_key:
            continue                      # id de outro símbolo não prova este
        por_id[coid].append(incident)
    # CONFLITO PRIMEIRO: contradição terminal do MESMO dispatch bloqueia ANTES
    # de qualquer retorno por incidente aberto/estado inconclusivo e ANTES de
    # consultar vínculo. RealTrade não desempata contradição, e posição FLAT
    # agora também não.
    coletas = {dispatch_id: collect_dispatch_proofs(dispatch_id, found,
                                                   symbol=symbol, exchange=exchange)
               for dispatch_id, found in por_id.items()}
    desfechos = {dispatch_id: _dispatch_outcome(dispatch_id, found, symbol=symbol,
                                                exchange=exchange)
                 for dispatch_id, found in por_id.items()}
    conflitantes = [i for i, desfecho in desfechos.items() if desfecho == PROOF_CONFLICT]
    if conflitantes:
        bloqueado = True
        for dispatch_id in conflitantes:
            if not await _persist_cross_dispatch_conflict(
                    coletas[dispatch_id], symbol=symbol, exchange=exchange,
                    side=row.get("side"), row=row):
                bloqueado = False
        return {"resolved": False, "unproven_ids": conflitantes,
                "reason": CONFLICT_REASON, "conflict_persisted": bloqueado,
                "conflict_handled": True}
    unproven = [i for i, found in por_id.items()
                if not found or any(x.get("resolved_at") is None for x in found)]
    if unproven:
        return {"resolved": False, "unproven_ids": unproven, "reason": "PROOF_MISSING"}
    provas = [x for found in por_id.values() for x in found]
    states = {str(x.get("state")) for x in provas}
    if not states <= _TERMINAL_SAFE:
        return {"resolved": False, "unproven_ids": ids, "reason": "PROOF_INCONCLUSIVE"}
    sem_prova = [i for i, desfecho in desfechos.items() if desfecho == PROOF_UNKNOWN]
    positivos = [i for i, desfecho in desfechos.items() if desfecho == PROOF_POSITIVE]
    if positivos and sem_prova:
        # Uma perna executou e outra é desconhecida: nada encerra, nada libera.
        return {"resolved": False, "unproven_ids": sem_prova,
                "reason": "EXECUTION_WITH_UNPROVEN_LEG"}
    if positivos:
        # Houve execução (posição protegida AGORA ou fill já encerrado antes da
        # recuperação). Sem vínculo contábil, nada encerra.
        trade_id = await _real_trade_for_dispatch(
            ids, symbol=symbol, exchange=row.get("exchange"), side=row.get("side"))
        if trade_id is None:
            await _escalate_untracked_execution(provas, executed_qty=0.0)
            return {"resolved": False, "unproven_ids": ids, "reason": "TRADE_LINK_MISSING"}
        ok = await intents.mark_confirmed(get_session, row["intent_key"],
                                          real_trade_id=trade_id,
                                          reason="RECONCILED_EXECUTION")
        return {"resolved": bool(ok), "unproven_ids": [], "reason": "EXECUTION_LINKED"}
    if sem_prova:
        return {"resolved": False, "unproven_ids": sem_prova,
                "reason": "ENTRY_PROOF_MISSING"}
    # TODOS os ids (inclusive a filha `-mfb`) com zero FINAL comprovado.
    ok = await intents.mark_terminal(get_session, row["intent_key"],
                                     reason="RECONCILED_NO_EXECUTION")
    return {"resolved": bool(ok), "unproven_ids": [], "reason": "NO_EXECUTION"}


def _entry_proof_of(incident) -> Optional[dict]:
    """Prova TERMINAL da entry gravada por este incidente, se houver."""
    payload = incident.get("payload") if isinstance(incident.get("payload"), dict) else {}
    proof = payload.get("entry_proof")
    return proof if isinstance(proof, dict) else None


def collect_dispatch_proofs(dispatch_id: str, incidents, *, symbol: Optional[str] = None,
                           exchange: Optional[str] = None) -> Dict[str, Any]:
    """Coletor ÚNICO de provas de UM id despachado, em TODOS os incidentes.

    Agrega independentemente do kind, da ordem da lista e de estarem resolvidos.
    Filtra pela identidade EFETIVA: client id exato e, quando informados,
    símbolo/exchange da intenção — prova de outro símbolo/conta não conta aqui.
    `-mfb` não é unida por prefixo: é outro dispatch.
    """
    alvo = str(dispatch_id)
    sym = _sym_key(symbol) if symbol else None
    exc = str(exchange).strip().lower() if exchange else None
    positivas, zeros, fontes = [], [], []
    conflito_persistido = None
    for incident in incidents or ():
        if str(incident.get("client_order_id") or "") != alvo:
            continue
        if sym is not None and _sym_key(incident.get("symbol") or "") != sym:
            continue
        if exc is not None and str(incident.get("exchange") or "").strip().lower() != exc:
            continue
        chave = incident.get("incident_key")
        fontes.append({"incident_key": chave, "kind": incident.get("kind"),
                       "state": incident.get("state"),
                       "resolved": incident.get("resolved_at") is not None})
        payload = incident.get("payload") if isinstance(incident.get("payload"), dict) else {}
        marcador = payload.get("entry_proof_conflict")
        if isinstance(marcador, dict) \
                and str(marcador.get("client_order_id") or "") == alvo:
            conflito_persistido = conflito_persistido or {**marcador,
                                                          "incident_key": chave}
        proof = _entry_proof_of(incident)
        if proof and str(proof.get("client_order_id") or "") == alvo:
            estado = str(proof.get("state") or "")
            if estado == PROOF_POSITIVE:
                positivas.append({**proof, "incident_key": chave,
                                  "kind": incident.get("kind")})
            elif estado == PROOF_TERMINAL_ZERO:
                zeros.append({**proof, "incident_key": chave,
                              "kind": incident.get("kind")})
        # Lower-bound positivo e posição PROTEGIDA são evidência POSITIVA;
        # lower-bound zero/ausente e FLAT nunca são zero TERMINAL.
        lower = _finite(incident.get("min_known_fill")) or 0.0
        if str(incident.get("state")) == State.PROTECTED or lower > 0:
            positivas.append({"state": PROOF_POSITIVE, "source": "incident_state",
                              "client_order_id": alvo, "executed_qty": lower or None,
                              "incident_key": chave, "kind": incident.get("kind")})
    return {"dispatch_id": alvo, "positive": positivas, "zero": zeros,
            "conflict": conflito_persistido, "sources": fontes}


def _dispatch_outcome(dispatch_id: str, incidents, *, symbol: Optional[str] = None,
                      exchange: Optional[str] = None) -> str:
    """Desfecho COMPROVADO de UM id despachado, pela precedência do contrato:

        marcador de conflito persistido                  → CONFLICT
        prova positiva + zero terminal do MESMO dispatch → CONFLICT
        somente prova positiva                           → POSITIVE
        somente prova de zero terminal                   → TERMINAL_ZERO
        nenhuma prova suficiente                         → UNKNOWN

    Duas respostas terminais incompatíveis para a MESMA ordem valem conflito
    mesmo vindo de incidentes (e kinds) DIFERENTES. Ausência de campo,
    lower-bound zero, posição FLAT e ausência de SL NÃO provam zero final.
    """
    provas = collect_dispatch_proofs(dispatch_id, incidents, symbol=symbol,
                                     exchange=exchange)
    if provas["conflict"]:
        return PROOF_CONFLICT
    if provas["positive"] and provas["zero"]:
        return PROOF_CONFLICT
    if provas["positive"]:
        return PROOF_POSITIVE
    return PROOF_TERMINAL_ZERO if provas["zero"] else PROOF_UNKNOWN


def cross_conflict_payload(provas: Mapping[str, Any]) -> Optional[dict]:
    """Payload DETERMINÍSTICO do conflito entre incidentes do mesmo dispatch.

    Guarda as duas provas (positiva preservada e zero observada), as fontes
    (chave + kind) e a identidade. Repetir NÃO faz o payload crescer: as listas
    são as mesmas provas, deduplicadas por incidente.
    """
    if not isinstance(provas, Mapping):
        return None
    positivas, zeros = provas.get("positive") or [], provas.get("zero") or []
    if not positivas or not zeros:
        return None

    def _menor(itens):
        return sorted(itens, key=lambda p: (str(p.get("incident_key") or ""),
                                            str(p.get("state") or "")))[0]

    mantida, observada = _menor(positivas), _menor(zeros)
    fontes = sorted({(str(p.get("incident_key") or ""), str(p.get("kind") or ""))
                     for p in list(positivas) + list(zeros)})
    return {"client_order_id": str(provas.get("dispatch_id") or ""),
            "kept_state": PROOF_POSITIVE, "observed_state": PROOF_TERMINAL_ZERO,
            "kept": dict(mantida), "observed": dict(observada),
            "scope": "CROSS_INCIDENT",
            "sources": [{"incident_key": chave, "kind": kind} for chave, kind in fontes]}


async def _persist_cross_dispatch_conflict(provas: Mapping[str, Any], *, symbol: str,
                                          exchange: str, side: Optional[str],
                                          row: Mapping[str, Any]) -> bool:
    """Persiste o conflito ENTRE incidentes no portador OFICIAL e bloqueia.

    Portador: o incidente `ENTRY_SUBMISSION_UNKNOWN` daquela identidade, pela
    chave estável — `record_incident` arma o latch local ANTES da gravação e usa
    a transação `persist_incident_with_p03_pause` (incidente visível e pausa no
    MESMO commit). Depois, com claim válido, `_halt_on_proof_conflict` deixa o
    portador em MANUAL_REQUIRED com `resolved_at=None`.

    Devolve True só quando o bloqueio ficou persistido. Falha de claim ou de
    gravação NÃO libera nada: a liquidação continua bloqueada, o latch armado e
    o ciclo seguinte conclui.
    """
    dispatch_id = str(provas.get("dispatch_id") or "")
    payload_conflito = cross_conflict_payload(provas)
    if not dispatch_id or not payload_conflito:
        return False
    if provas.get("conflict") and str(provas["conflict"].get("scope") or "") \
            == "CROSS_INCIDENT":
        payload_conflito = {**payload_conflito,
                            "first_seen_at_ms": provas["conflict"].get("first_seen_at_ms")}
    payload_conflito.setdefault("first_seen_at_ms", int(_now().timestamp() * 1000))
    payload_conflito["last_seen_at_ms"] = int(_now().timestamp() * 1000)
    chave = build_incident_key(Kind.ENTRY_SUBMISSION_UNKNOWN, symbol,
                              exchange=exchange, client_order_id=dispatch_id)
    decisao = row.get("decision_payload") if isinstance(row.get("decision_payload"), dict) else {}
    try:
        # Reaproveita o registro existente pela chave estável (reabertura oficial
        # quando já estiver resolvido); nenhum kind, tabela ou reconciliador novo.
        resultado = await record_incident(
            kind=Kind.ENTRY_SUBMISSION_UNKNOWN, symbol=symbol, exchange=exchange,
            client_order_id=dispatch_id, side=_norm_entry_side(side),
            planned_stop=_finite(decisao.get("stop_loss")),
            planned_qty=_finite(decisao.get("qty")),
            payload={"source": "entry_intent", "intent_key": row.get("intent_key"),
                     "account_ref": row.get("account_ref"),
                     "settle_reason": CONFLICT_REASON,
                     "entry_proof_conflict": payload_conflito})
    except Exception as exc:  # noqa: BLE001
        log.critical(f"[p03][conflict] persistência do conflito falhou ({dispatch_id}): {exc}")
        return False
    if not resultado.get("persisted"):
        return False
    repo = _get_repo()
    portador = await repo.get(chave)
    if not portador:
        return False
    if str(portador.get("state")) == State.MANUAL_REQUIRED \
            and portador.get("resolved_at") is None:
        return True                       # já bloqueado: repetir é idempotente
    if not await repo.claim(chave, _PROCESS_ID,
                            _now() + timedelta(seconds=RECONCILE_LEASE_S)):
        # Claim de outro dono/lease vivo: NÃO rouba lease. O ciclo seguinte
        # conclui o bloqueio; a liquidação segue barrada e o latch armado.
        log.warning(f"[p03][conflict] claim indisponível para {chave}; bloqueio no próximo ciclo")
        return False
    portador = await repo.get(chave) or portador
    await _halt_on_proof_conflict(chave, _PROCESS_ID, portador)
    final = await repo.get(chave) or {}
    return (str(final.get("state")) == State.MANUAL_REQUIRED
            and final.get("resolved_at") is None)


async def _persist_entry_proof(key: str, owner: str, inc: dict, *, state: str,
                               client_order_id: Optional[str], qty: Optional[float],
                               status: Optional[str]) -> str:
    """Grava no incidente a prova TERMINAL da entry, com identidade e origem.

    Roda FENCED (owner+lease) e ANTES de qualquer resolução: quem liquida a
    intenção depois precisa saber se aquele id teve zero COMPROVADO ou se
    simplesmente nunca foi consultado.

    Duas respostas TERMINAIS incompatíveis para a MESMA ordem (positiva e zero)
    NÃO se sobrescrevem: o fill positivo é preservado, a observação nova fica
    registrada em `entry_proof_conflict` e o retorno é `PROOF_CONFLICT` — quem
    chamou precisa PARAR, não resolver.
    """
    if not client_order_id:
        return state
    atual = dict(inc.get("payload") or {}) if isinstance(inc.get("payload"), dict) else {}
    proof = {"state": state, "source": "get_order",
             "client_order_id": str(client_order_id),
             "status": str(status or "").upper() or None,
             "executed_qty": _finite(qty),
             "observed_at_ms": int(_now().timestamp() * 1000)}
    anterior = atual.get("entry_proof")
    mesmo_id = (isinstance(anterior, dict)
                and str(anterior.get("client_order_id") or "") == str(client_order_id))
    estados = {str((anterior or {}).get("state")), state} if mesmo_id else set()
    if mesmo_id and estados == {PROOF_POSITIVE, PROOF_TERMINAL_ZERO}:
        # CONTRADIÇÃO na mesma ordem: preserva a positiva, registra a outra.
        mantida = anterior if anterior.get("state") == PROOF_POSITIVE else proof
        observada = proof if anterior.get("state") == PROOF_POSITIVE else anterior
        conflito = {"client_order_id": str(client_order_id),
                    "kept_state": PROOF_POSITIVE,
                    "observed_state": PROOF_TERMINAL_ZERO,
                    "kept": dict(mantida), "observed": dict(observada),
                    "first_seen_at_ms": (atual.get("entry_proof_conflict") or {})
                    .get("first_seen_at_ms") or int(_now().timestamp() * 1000),
                    "last_seen_at_ms": int(_now().timestamp() * 1000)}
        merged = {**atual, "entry_proof": dict(mantida),
                  "entry_proof_conflict": conflito}
        if await _fenced(key, owner, payload=merged):
            inc["payload"] = merged
        return PROOF_CONFLICT
    if isinstance(atual.get("entry_proof_conflict"), dict):
        return PROOF_CONFLICT      # conflito já registrado continua valendo
    if isinstance(anterior, dict) and anterior.get("state") == PROOF_POSITIVE \
            and state != PROOF_POSITIVE:
        return PROOF_POSITIVE      # positiva anterior NÃO é apagada
    merged = {**atual, "entry_proof": proof}
    if await _fenced(key, owner, payload=merged):
        inc["payload"] = merged
    return state


async def _halt_if_conflicting(key: str, owner: str, inc: dict) -> bool:
    """Para no caminho manual quando há contradição terminal desta identidade.

    Confere o marcador do próprio incidente E as provas IRMÃS (outros incidentes
    do mesmo dispatch, de qualquer kind). Devolve True quando parou.
    """
    coid = inc.get("client_order_id")
    if not coid or inc.get("kind") not in _ENTRY_KINDS:
        return False
    repo = _get_repo()
    try:
        irmaos = await repo.list_by_client_ids([coid])
    except Exception:  # noqa: BLE001
        # Sem leitura dos irmãos não há prova de ausência de conflito. Não
        # prossegue só com a visão local, principalmente após uma prova nova.
        await _schedule_retry(key, owner, inc, State.RETRY_PENDING,
                              "provas do dispatch indisponíveis — sem mutação")
        return True
    if not any(str(x.get("incident_key")) == str(key) for x in irmaos):
        irmaos = list(irmaos) + [inc]
    provas = collect_dispatch_proofs(coid, irmaos, symbol=inc.get("symbol"),
                                     exchange=inc.get("exchange"))
    if not provas["conflict"] and not (provas["positive"] and provas["zero"]):
        return False
    payload = cross_conflict_payload(provas) or provas["conflict"]
    if payload:
        atual = dict(inc.get("payload") or {}) if isinstance(inc.get("payload"), dict) else {}
        anterior = atual.get("entry_proof_conflict")
        if isinstance(anterior, dict):
            payload.setdefault("first_seen_at_ms", anterior.get("first_seen_at_ms"))
        payload.setdefault("first_seen_at_ms", int(_now().timestamp() * 1000))
        payload["last_seen_at_ms"] = int(_now().timestamp() * 1000)
        if anterior != payload:
            merged = {**atual, "entry_proof_conflict": payload}
            if await _fenced(key, owner, payload=merged):
                inc["payload"] = merged
    await _halt_on_proof_conflict(key, owner, inc)
    return True


async def _halt_on_proof_conflict(key: str, owner: str, inc: dict) -> None:
    """Conflito terminal ⇒ pendência HUMANA pelo fluxo oficial.

    Não resolve, não devolve reserva, não libera slot nem quarentena — e não
    toca proteção: nenhum SL é cancelado e nenhuma entrada é reenviada por
    causa do conflito. Repetir é idempotente (o estado já é MANUAL_REQUIRED).
    """
    conflito = (inc.get("payload") or {}).get("entry_proof_conflict") or {}
    motivo = (f"{CONFLICT_REASON}: respostas terminais incompatíveis para "
              f"{_mask(conflito.get('client_order_id'))} "
              f"(mantida {conflito.get('kept_state')}, observada "
              f"{conflito.get('observed_state')}) — exige conferência humana")
    if str(inc.get("state")) == State.MANUAL_REQUIRED:
        return
    if await _fenced(key, owner, state=State.MANUAL_REQUIRED, manual_reason=motivo,
                     last_error=motivo):
        log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({CONFLICT_REASON})")


async def _escalate_untracked_execution(proofs, *, executed_qty: float) -> None:
    """Execução comprovada SEM RealTrade: vira pendência humana explícita.

    Não fabrica preço, P&L nem vínculo, não devolve reserva e não libera a
    quarentena — a intenção continua ocupando o slot até alguém reconciliar.
    """
    repo = _get_repo()
    motivo = (f"execução comprovada (qty≥{executed_qty:g}) sem RealTrade "
              f"correspondente — vínculo contábil ausente")
    for incident in proofs or ():
        key = incident.get("incident_key")
        if not key or str(incident.get("state")) == State.MANUAL_REQUIRED:
            continue
        try:
            await repo.update(key, state=State.MANUAL_REQUIRED, resolved_at=None,
                              manual_reason=motivo, last_error=motivo)
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03][intents] escalonamento manual falhou ({key}): {exc}")
        else:
            log.critical(f"[p03][transition] {key} → MANUAL_REQUIRED ({motivo})")
    try:
        from services import risk_service
        await risk_service.arm_p03_pause(motivo)
    except Exception:  # noqa: BLE001
        pass


async def recover_entry_intents() -> dict:
    """Liga as INTENÇÕES pendentes (P03) à recuperação/reconciliação operacional.

    Sem worker novo: roda dentro do boot e do ciclo que já existem.
      1. lease vencido em SENDING vira UNKNOWN (nunca reenvio automático);
      2. cada intenção com desfecho de ENVIO incerto abre/mantém incidente pelo
         MESMO `client_order_id`, então a quarentena só cai quando a
         reconciliação provar o desfecho — não por TTL.
    """
    try:
        from db import DB_ENABLED, get_session
        from services import entry_intent_service as intents
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import: {exc}"}
    if not DB_ENABLED:
        return {"skipped": "DB_DISABLED"}
    summary = {"to_unknown": 0, "reserved_released": 0, "incidents": 0, "skipped_identity": 0}
    try:
        recovered = await intents.recover_stale(get_session)
        summary["to_unknown"] = int(recovered.get("to_unknown") or 0)
        summary["reserved_released"] = int(recovered.get("reserved_released") or 0)
        pending = await intents.list_needing_reconciliation(get_session)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][intents] recuperação falhou: {exc}")
        return {**summary, "error": str(exc)}
    summary["resolved"] = 0
    for row in pending:
        symbol = _intent_symbol(row.get("symbol"))
        client_order_id = row.get("client_order_id")
        if not symbol or not client_order_id:
            summary["skipped_identity"] += 1
            continue
        try:
            # 1. A reconciliação já provou o desfecho? Então a intenção FECHA —
            # reapresentá-la reabriria o incidente pela MESMA prova, em ciclo.
            verdict = await _settle_intent_from_proof(row)
            if verdict["resolved"]:
                summary["resolved"] += 1
                continue
            if verdict.get("conflict_handled"):
                # Conflito já tratado no portador oficial: NÃO cai no caminho
                # genérico que o transformaria em retry sem motivo.
                summary["conflicts"] = int(summary.get("conflicts") or 0) + 1
                continue
            # 2. Sem prova: garante incidente para CADA id ainda não provado,
            # COM os dados point-in-time da decisão — sem planned_stop nem
            # planned_qty o incidente não consegue adotar o SL vivo que já
            # existe, e a pendência nunca fecharia.
            decisao = row.get("decision_payload") if isinstance(
                row.get("decision_payload"), dict) else {}
            for dispatch_id in (verdict["unproven_ids"] or [client_order_id]):
                result = await record_incident(
                    kind=Kind.ENTRY_SUBMISSION_UNKNOWN, symbol=symbol,
                    exchange=row.get("exchange") or EXCHANGE_BINANCE,
                    client_order_id=dispatch_id,
                    side=_norm_entry_side(row.get("side")),
                    planned_stop=_finite(decisao.get("stop_loss")),
                    planned_qty=_finite(decisao.get("qty")),
                    payload={"source": "entry_intent", "intent_key": row.get("intent_key"),
                             "account_ref": row.get("account_ref"),
                             "reason": row.get("reason"),
                             "dispatch_ids": list(row.get("dispatch_ids") or []),
                             "decision_payload": dict(decisao) or None,
                             "settle_reason": verdict.get("reason")})
                if result.get("persisted"):
                    summary["incidents"] += 1
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03][intents] incidente de {client_order_id} falhou: {exc}")
            summary["skipped_identity"] += 1
    if summary["to_unknown"] or summary["incidents"] or summary["resolved"]:
        log.warning(f"[p03][intents] recuperação: {summary}")
    return summary


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
    # ANTES de qualquer resolução/cleanup/mutação: marcador de conflito já
    # persistido (neste incidente ou em irmão do MESMO dispatch) para no
    # caminho manual. A recuperação não reabre para depois limpar sozinha.
    if await _halt_if_conflicting(key, owner, inc):
        return
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
    else:
        # Reconhecimentos manuais são revalidados em TODO ciclo, mesmo com boot
        # seguro e ZERO incidentes abertos: é essa prova recorrente que o guard
        # exige antes de nova exposição. Quando o bloco acima já rodou, a
        # revalidação aconteceu lá com a MESMA leitura (sem chamada dupla).
        try:
            veredito = await _revalidate_manual_acks_fresh()
            if not veredito.get("ok"):
                log.warning("[p03][manual-ack] revalidação periódica incompleta: "
                            f"{veredito.get('reason_code')}")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[p03][manual-ack] revalidação periódica falhou: {exc}")
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
    # ANTES de contar: reavalia untracked preso em MANUAL_REQUIRED, para que a
    # liberação aconteça no MESMO ciclo em que a causa deixa de existir.
    try:
        await recheck_untracked_manual()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] re-check de untracked falhou (incidentes mantidos): {exc}")
    # Intenções de entrada pendentes entram no MESMO ciclo (sem worker novo).
    try:
        await recover_entry_intents()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03] recuperação de intenções falhou: {exc}")
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
    # Enquanto houver ZERO incidentes e o latch P03 armado, tenta o release SEGURO
    # em TODO ciclo elegível (não só na transição) — o próprio _maybe_release_quarantine
    # é idempotente (retorna cedo se o latch já não está armado) e só age com a
    # confirmação do banco.
    if open_now == 0 and _p03_latch_armed and _boot_scan_safe:
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
    try:
        await recover_entry_intents()   # intenções pendentes ANTES do scan
    except Exception as exc:  # noqa: BLE001
        log.error(f"[p03][boot] recuperação de intenções falhou: {exc}")
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


def _boot_coverage_ok(matched: list, size: Optional[float], step: Optional[float]) -> bool:
    """Invariante #5: no boot, uma posição só é 'tracked' se o AGREGADO (Decimal) das
    qty dos RealTrade que casam cobre a posição fresh INTEGRALMENTE (tolerância =
    stepSize). Fail-closed: sem match, tamanho desconhecido ou qty ausente → False
    (→ registra UNTRACKED). Cobertura parcial NÃO conta como rastreada."""
    from decimal import Decimal, InvalidOperation
    need = _finite(size)
    if need is None or need <= 0 or not matched:
        return False
    try:
        agg = Decimal("0")
        for r in matched:
            q = _finite(_rt_get(r, "qty"))
            if q is None:
                q = _finite(_rt_get(r, "qty_initial"))
            if q is None:
                return False                 # qty ausente → cobertura não confirmável
            agg += Decimal(str(q))
        tol = Decimal(str(step)) if (step and step > 0) else Decimal("0")
        return agg + tol >= Decimal(str(need))
    except (InvalidOperation, ValueError):
        return False


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
        contexto_manual = await capture_manual_context()
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
    # Reconhecimentos manuais são revalidados contra ESTA leitura fresca antes
    # de o scan ser considerado seguro: identidade divergente invalida a
    # autorização e reabre a contenção; registro ilegível mantém bloqueio.
    revalidacao = await _revalidate_manual_acks(positions, context=contexto_manual)
    if not revalidacao["ok"]:
        _boot_scan_safe = False
        await _arm_quarantine(
            f"boot: reconhecimento manual não revalidado ({revalidacao['reason_code']})")
        log.critical("[p03][boot] reconhecimento manual não revalidado "
                     f"({revalidacao['reason_code']}) — quarentena armada")
        return {"status": "UNKNOWN", "count": 0}
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
        side = _explicit_side(p.get("side"))
        if side is None:
            # Lado ausente/inválido/ambíguo: NÃO infiro BUY, NÃO retorno FLAT.
            # Boot inseguro + quarentena (boot exige cobertura de posição com lado).
            _boot_scan_safe = False
            await _arm_quarantine(f"boot: posição {sym} com lado ausente/ambíguo — não infiro BUY")
            log.critical(f"[p03][boot] posição {sym} lado ausente/ambíguo — UNKNOWN + quarentena")
            return {"status": "UNKNOWN", "count": n}
        # MESMO matcher rigoroso do reconciliador (exchange/source/símbolo/quote/lado)
        # E cobertura AGREGADA integral (invariante #5): match parcial NÃO é tracked.
        matched = [t for t in open_trades if _real_trade_match(t, norm, side)]
        psize = abs(_finite(p.get("size")) or 0)
        if _boot_coverage_ok(matched, psize, _symbol_step(norm)):
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
