"""
P05 — Otimização de estratégia governada por evidência.

Fluxo único, do dado à decisão:

    dados resolvidos
      → diagnóstico confiável        (P05A)
      → candidatos limitados         (P05B)
      → validação temporal offline   (P05B)
      → challenger em shadow         (P05C)
      → decisão governada            (P05D)
      → recomendação de ativação MANUAL

INVARIANTE CENTRAL: o comportamento atual continua sendo o CHAMPION. Nada aqui
altera estratégia, score, tier, filtros, thresholds, entry/stop/TP, sizing ou
qualquer flag do LIVE. O challenger só registra veredito CONTRAFACTUAL. Não
existe caminho de promoção automática — `ELIGIBLE` significa apenas "pode ser
apresentado ao usuário para autorização".

ARQUITETURA: este módulo é PURO em relação à exchange — não importa SDK/provider,
não emite/cancela ordem, não faz rede. Só lê contratos já existentes no banco
(`RecommendationSnapshot`, `RealTrade`, `BacktestTrade`, `SkipReasonStat`).

TRÊS FONTES, NUNCA MISTURADAS:
  REAL     `RealTrade(source="auto")` — verdade financeira; janela por `closed_at`.
  SHADOW   `RecommendationSnapshot`  — amostra de setups; janela por `outcome_at`.
  BACKTEST `BacktestTrade`           — evidência histórica secundária.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
#  Configuração (defaults NÃO alteram o LIVE)
# ════════════════════════════════════════════════════════════════════════════
def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


P05_ANALYTICS_ENABLED = _env_bool("P05_ANALYTICS_ENABLED", "true")
P05_CHALLENGER_SHADOW_ENABLED = _env_bool("P05_CHALLENGER_SHADOW_ENABLED", "false")
P05_MAX_CANDIDATES = _env_int("P05_MAX_CANDIDATES", 12)
P05_MIN_OFFLINE_RESOLVED = _env_int("P05_MIN_OFFLINE_RESOLVED", 60)
P05_MIN_OOS_RESOLVED = _env_int("P05_MIN_OOS_RESOLVED", 30)
P05_MIN_SHADOW_RESOLVED = _env_int("P05_MIN_SHADOW_RESOLVED", 30)
P05_MIN_SHADOW_DAYS = _env_int("P05_MIN_SHADOW_DAYS", 14)
P05_MIN_FEATURE_COVERAGE_PCT = _env_float("P05_MIN_FEATURE_COVERAGE_PCT", 80.0)
P05_MIN_SHADOW_COVERAGE_PCT = _env_float("P05_MIN_SHADOW_COVERAGE_PCT", 90.0)
P05_BOOTSTRAP_SAMPLES = _env_int("P05_BOOTSTRAP_SAMPLES", 1000)
P05_RANDOM_SEED = _env_int("P05_RANDOM_SEED", 20260827)
P05_DIAG_CACHE_TTL_S = max(30, _env_int("P05_DIAG_CACHE_TTL_S", 300))

# Teto duro do bootstrap (protege CPU mesmo se a env vier absurda).
_BOOTSTRAP_HARD_MAX = 5000

# Locks no PostgreSQL: serializam avaliação idempotente e troca do único
# challenger. O índice parcial no model continua sendo a última barreira.
_P05_EVALUATE_LOCK_KEY = 505202608
_P05_SHADOW_LOCK_KEY = 505202609
# Mesmo lock transacional usado pelo P03 ao persistir incidente+pausa. Assim,
# start/decisão do P05 não conseguem "passar entre" a criação de um incidente
# e a quarentena correspondente.
_P03_SAFETY_LOCK_KEY = 917283

_DIAG_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}
_DIAG_CACHE_LOCK = asyncio.Lock()


def safety_config_snapshot() -> Dict[str, Any]:
    """Snapshot somente-leitura das proteções que condicionam o experimento.

    Guardamos os valores normalizados como texto porque são exatamente as envs
    consumidas pelo executor no boot. Qualquer mudança durante o SHADOW — mesmo
    uma mudança aparentemente mais conservadora — exige nova avaliação; o P05
    não tenta interpretar intenção nem escreve nessas variáveis.
    """
    defaults = {
        "LIVE_SIZE_MULT": "1.0",
        "MAKER_ENTRY_ENABLED": "false",
        "TF_UPGRADE_ENABLED": "false",
        "PYRAMIDING_ENABLED": "false",
        "P04A_ENTRY_REVALIDATION_ENABLED": "true",
        "P04A_QUOTE_TIMEOUT_S": "1.5",
        "P04A_MAX_QUOTE_AGE_MS": "1500",
        "P04A_MAX_FETCH_LATENCY_MS": "1500",
        "P04A_MAX_ADVERSE_SLIPPAGE_PCT": "0.10",
        "P04A_MAX_CHASE_ATR": "",
        "P04B_MARKET_REVALIDATION_ENABLED": "true",
        "P04B_MAKER_FALLBACK_ENABLED": "false",
        "P04B_DEPTH_TIMEOUT_S": "1.5",
        "P04B_MAX_DEPTH_AGE_MS": "1500",
        "P04B_MAX_FETCH_LATENCY_MS": "1500",
        "P04B_DEPTH_LIMIT": "50",
        "P04B_MAX_BOOK_IMPACT_PCT": "0.10",
        "P04B_MAX_MARKET_SLIPPAGE_PCT": os.getenv(
            "P04A_MAX_ADVERSE_SLIPPAGE_PCT", "0.10"
        ).strip(),
        "P04C_DATA_FRESHNESS_ENABLED": "true",
        "P04C_MAX_CANDLE_LAG_PERIODS": "1.25",
        "P04C_MAX_TICKER_AGE_MS": "300000",
        "P04C_MAX_DERIVATIVES_AGE_MS": "300000",
        "P04C_MAX_REGIME_AGE_MS": "900000",
    }
    boolean_names = {
        "MAKER_ENTRY_ENABLED", "TF_UPGRADE_ENABLED", "PYRAMIDING_ENABLED",
        "P04A_ENTRY_REVALIDATION_ENABLED", "P04B_MARKET_REVALIDATION_ENABLED",
        "P04B_MAKER_FALLBACK_ENABLED", "P04C_DATA_FRESHNESS_ENABLED",
    }
    out: Dict[str, Any] = {}
    for name, default in defaults.items():
        raw = os.getenv(name, default).strip()
        out[name] = raw.lower() in ("1", "true", "yes", "on") if name in boolean_names else raw
    return out


def safety_guard() -> Dict[str, Any]:
    config = safety_config_snapshot()
    return {"fingerprint": canonical_hash(config), "config": config}

FEATURES_SCHEMA_VERSION = 1

# Estados de outcome (espelham calibration_service/assertiveness_service).
SNAP_WIN = ("won_tp1", "won_tp1_be", "won_tp2")
SNAP_TP2 = ("won_tp2",)
SNAP_RESOLVED = SNAP_WIN + ("lost", "expired")
REAL_CLOSED = ("closed_tp1", "closed_tp2", "closed_be", "closed_stop", "closed_manual")
REAL_TP1_HIT = ("closed_tp1", "closed_tp2", "closed_be")
REAL_TP2_HIT = ("closed_tp2",)

# Rótulos de confiabilidade por tamanho de amostra.
RELIABILITY_INSUFFICIENT = "INSUFFICIENT"
RELIABILITY_EARLY = "EARLY"
RELIABILITY_USABLE = "USABLE"
RELIABILITY_STRONG = "STRONG"

_RELIABILITY_EARLY_MIN = 10
_RELIABILITY_USABLE_MIN = 30
_RELIABILITY_STRONG_MIN = 100

# Estados do ciclo de vida e transições VÁLIDAS (sem saltos).
STATUS_DRAFT = "DRAFT"
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"
STATUS_REJECTED = "REJECTED"
STATUS_OFFLINE_VALIDATED = "OFFLINE_VALIDATED"
STATUS_SHADOW = "SHADOW"
STATUS_ELIGIBLE = "ELIGIBLE"

VALID_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    STATUS_DRAFT: (STATUS_INSUFFICIENT, STATUS_REJECTED, STATUS_OFFLINE_VALIDATED),
    STATUS_OFFLINE_VALIDATED: (STATUS_SHADOW,),
    STATUS_SHADOW: (STATUS_REJECTED, STATUS_ELIGIBLE),
    STATUS_INSUFFICIENT: (),
    STATUS_REJECTED: (),
    STATUS_ELIGIBLE: (),
}

OBJECTIVE_LOSS_REDUCTION = "LOSS_REDUCTION"
OBJECTIVE_MORE_OPERATIONS = "MORE_OPERATIONS"
OBJECTIVES = (OBJECTIVE_LOSS_REDUCTION, OBJECTIVE_MORE_OPERATIONS)

CHALLENGER_ELIGIBLE = "ELIGIBLE"
CHALLENGER_BLOCKED = "BLOCKED"
CHALLENGER_UNKNOWN = "UNKNOWN"


def can_transition(current: str, target: str) -> bool:
    """Só permite as transições do ciclo oficial. Experimento decidido não reabre."""
    return target in VALID_TRANSITIONS.get(current, ())


# ════════════════════════════════════════════════════════════════════════════
#  Núcleo puro — determinístico, sem DB e sem rede
# ════════════════════════════════════════════════════════════════════════════
def _finite(v: Any) -> Optional[float]:
    """float finito ou None. NaN/infinito NUNCA passam."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _utc(dt: Any) -> Optional[datetime]:
    """Datetime timezone-aware em UTC, ou None. Naive é assumido UTC."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def classify_outcome(status: str, r: float) -> str:
    """win | loss | breakeven | expired. `expired` NUNCA é contado como win/loss;
    zero NUNCA é contado como loss."""
    st = (status or "").strip().lower()
    if st in ("expired", "expirado"):
        return "expired"
    if r > 0:
        return "win"
    if r < 0:
        return "loss"
    return "breakeven"


def normalize_outcomes(raw: Sequence[Dict[str, Any]], *, source: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Converte linhas cruas em outcomes VÁLIDOS + relatório de qualidade.

    Excluído (e CONTABILIZADO, nunca silencioso):
      • ainda aberto / não resolvido;
      • `realized_r` ausente — NUNCA vira 0;
      • NaN/infinito;
      • sem timestamp de resolução (não dá pra ordenar no tempo);
      • duplicata do mesmo trade/snapshot.
    """
    valid: List[Dict[str, Any]] = []
    excluded: Dict[str, int] = {}
    seen: set = set()

    def _drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in raw:
        if row.get("is_open"):
            _drop("aberto (não resolvido)")
            continue

        raw_r = row.get("realized_r")
        if raw_r is None:
            _drop("realized_r ausente")
            continue
        r = _finite(raw_r)
        if r is None:
            _drop("realized_r NaN/infinito")
            continue

        resolved_at = _utc(row.get("resolved_at"))
        if resolved_at is None:
            _drop("sem timestamp de resolução")
            continue

        # Só uma linha VÁLIDA reserva a identidade. Assim, uma cópia quebrada
        # lida primeiro não elimina silenciosamente a versão válida posterior.
        ident = row.get("dedupe_key")
        if ident is not None:
            if ident in seen:
                _drop("duplicado")
                continue
            seen.add(ident)

        item = dict(row)
        item["realized_r"] = r
        item["resolved_at"] = resolved_at
        item["outcome_class"] = classify_outcome(row.get("status") or "", r)
        valid.append(item)

    valid.sort(key=lambda x: x["resolved_at"])
    total_raw = len(raw)
    data_quality = {
        "source": source,
        "total_raw": total_raw,
        "total_valid": len(valid),
        "excluded_total": total_raw - len(valid),
        "excluded_by_reason": excluded,
        "valid_pct": round(len(valid) / total_raw * 100, 1) if total_raw else None,
    }
    return valid, data_quality


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> Optional[Dict[str, float]]:
    """Intervalo de Wilson 95% para proporção. `None` quando não há amostra."""
    if trials <= 0:
        return None
    p = successes / trials
    z2 = z * z
    denom = 1.0 + z2 / trials
    center = (p + z2 / (2 * trials)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * trials)) / trials)) / denom
    return {
        "low_pct": round(max(0.0, center - margin) * 100, 2),
        "high_pct": round(min(1.0, center + margin) * 100, 2),
    }


def _bootstrap_samples() -> int:
    return max(1, min(P05_BOOTSTRAP_SAMPLES, _BOOTSTRAP_HARD_MAX))


def bootstrap_mean_ci(values: Sequence[float], *, seed: int = None,
                      samples: int = None) -> Optional[Dict[str, float]]:
    """IC 95% da MÉDIA por bootstrap com seed FIXA → mesma amostra, mesmo IC."""
    vals = [v for v in (_finite(x) for x in values) if v is not None]
    if len(vals) < 2:
        return None
    rng = random.Random(P05_RANDOM_SEED if seed is None else seed)
    n = len(vals)
    b = _bootstrap_samples() if samples is None else max(1, min(samples, _BOOTSTRAP_HARD_MAX))
    means: List[float] = []
    for _ in range(b):
        means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return {"low": round(lo, 4), "high": round(hi, 4)}


def bootstrap_delta_ci(a: Sequence[float], b: Sequence[float], *, seed: int = None,
                       samples: int = None) -> Optional[Dict[str, float]]:
    """IC 95% do DELTA de médias (a − b), bootstrap independente e determinístico."""
    va = [v for v in (_finite(x) for x in a) if v is not None]
    vb = [v for v in (_finite(x) for x in b) if v is not None]
    if len(va) < 2 or len(vb) < 2:
        return None
    rng = random.Random(P05_RANDOM_SEED if seed is None else seed)
    na, nb = len(va), len(vb)
    nboot = _bootstrap_samples() if samples is None else max(1, min(samples, _BOOTSTRAP_HARD_MAX))
    deltas: List[float] = []
    for _ in range(nboot):
        ma = sum(va[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(vb[rng.randrange(nb)] for _ in range(nb)) / nb
        deltas.append(ma - mb)
    deltas.sort()
    lo = deltas[int(0.025 * (len(deltas) - 1))]
    hi = deltas[int(0.975 * (len(deltas) - 1))]
    return {"low": round(lo, 4), "high": round(hi, 4), "point": round(
        sum(va) / na - sum(vb) / nb, 4)}


def bootstrap_paired_selection_delta_ci(
    rows: Sequence[Dict[str, Any]],
    champion: Dict[str, Any],
    candidate: Dict[str, Any],
    active: Sequence[str],
    *,
    seed: int = None,
    samples: int = None,
) -> Optional[Dict[str, float]]:
    """IC do delta preservando a dependência entre champion e challenger.

    A unidade reamostrada é a oportunidade original; em cada réplica as duas
    regras são reaplicadas ao MESMO índice. Isso evita o IC independente (e
    incorreto) quando um lado é subconjunto do outro.
    """
    merged = dict(champion)
    merged.update(candidate)
    pairs: List[Tuple[Optional[float], Optional[float]]] = []
    for row in rows:
        cv = eligibility(row, champion, active)
        nv = eligibility(row, merged, active)
        if cv is None or nv is None:
            continue
        r = _finite(row.get("realized_r"))
        if r is None:
            continue
        pairs.append((r if nv else None, r if cv else None))
    if len(pairs) < 2:
        return None
    cand_all = [a for a, _ in pairs if a is not None]
    champ_all = [b for _, b in pairs if b is not None]
    if len(cand_all) < 2 or len(champ_all) < 2:
        return None
    rng = random.Random(P05_RANDOM_SEED if seed is None else seed)
    nboot = _bootstrap_samples() if samples is None else max(1, min(samples, _BOOTSTRAP_HARD_MAX))
    n = len(pairs)
    deltas: List[float] = []
    for _ in range(nboot):
        ca_sum = ch_sum = 0.0
        ca_n = ch_n = 0
        for _j in range(n):
            ca, ch = pairs[rng.randrange(n)]
            if ca is not None:
                ca_sum += ca
                ca_n += 1
            if ch is not None:
                ch_sum += ch
                ch_n += 1
        if ca_n and ch_n:
            deltas.append(ca_sum / ca_n - ch_sum / ch_n)
    if len(deltas) < max(20, nboot // 2):
        return None
    deltas.sort()
    lo = deltas[int(0.025 * (len(deltas) - 1))]
    hi = deltas[int(0.975 * (len(deltas) - 1))]
    return {
        "low": round(lo, 4),
        "high": round(hi, 4),
        "point": round(sum(cand_all) / len(cand_all) - sum(champ_all) / len(champ_all), 4),
        "method": "paired-opportunity-bootstrap",
    }


def bootstrap_paired_membership_delta_ci(
    rows: Sequence[Dict[str, Any]], champion_ids: set, candidate_ids: set,
    *, seed: int = None, samples: int = None,
) -> Optional[Dict[str, float]]:
    """Versão pareada para memberships já congelados (anotações P05C)."""
    pairs = [
        (r["realized_r"] if id(r) in candidate_ids else None,
         r["realized_r"] if id(r) in champion_ids else None)
        for r in rows
    ]
    cand_all = [a for a, _ in pairs if a is not None]
    champ_all = [b for _, b in pairs if b is not None]
    if len(pairs) < 2 or len(cand_all) < 2 or len(champ_all) < 2:
        return None
    rng = random.Random(P05_RANDOM_SEED if seed is None else seed)
    nboot = _bootstrap_samples() if samples is None else max(1, min(samples, _BOOTSTRAP_HARD_MAX))
    deltas: List[float] = []
    for _ in range(nboot):
        sample = [pairs[rng.randrange(len(pairs))] for _j in range(len(pairs))]
        ca = [a for a, _ in sample if a is not None]
        ch = [b for _, b in sample if b is not None]
        if ca and ch:
            deltas.append(sum(ca) / len(ca) - sum(ch) / len(ch))
    if len(deltas) < max(20, nboot // 2):
        return None
    deltas.sort()
    return {
        "low": round(deltas[int(0.025 * (len(deltas) - 1))], 4),
        "high": round(deltas[int(0.975 * (len(deltas) - 1))], 4),
        "point": round(sum(cand_all) / len(cand_all) - sum(champ_all) / len(champ_all), 4),
        "method": "paired-annotation-bootstrap",
    }


def reliability_label(n: int) -> str:
    """Confiabilidade da amostra. Segmento pequeno NUNCA é edge comprovado."""
    if n < _RELIABILITY_EARLY_MIN:
        return RELIABILITY_INSUFFICIENT
    if n < _RELIABILITY_USABLE_MIN:
        return RELIABILITY_EARLY
    if n < _RELIABILITY_STRONG_MIN:
        return RELIABILITY_USABLE
    return RELIABILITY_STRONG


def max_drawdown_r(rs: Sequence[float]) -> float:
    """Maior queda pico→vale da curva de R acumulado."""
    equity = peak = 0.0
    worst = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 4)


def worst_loss_streak(rows: Sequence[Dict[str, Any]]) -> int:
    """Maior sequência consecutiva de losses (breakeven/expired NÃO contam)."""
    worst = cur = 0
    for row in rows:
        if row.get("outcome_class") == "loss":
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def compute_evidence_metrics(rows: Sequence[Dict[str, Any]], *,
                             with_bootstrap: bool = True) -> Dict[str, Any]:
    """Métricas do núcleo. Toda métrica não calculável devolve `None` + motivo
    em `unavailable` — nunca um zero inventado."""
    unavailable: Dict[str, str] = {}
    n = len(rows)
    if n == 0:
        return {
            "count": 0, "wins": 0, "losses": 0, "breakeven": 0, "expired": 0,
            "win_rate_pct": None, "win_rate_ci": None,
            "expectancy_r": None, "median_r": None, "sum_r": None, "std_r": None,
            "downside_deviation_r": None, "profit_factor": None, "sharpe_per_trade": None,
            "max_drawdown_r": None, "worst_loss_streak": None,
            "expectancy_ci": None, "tp1_hit_rate_pct": None, "tp2_hit_rate_pct": None,
            "net_pnl_usd": None, "entry_fees_usd": None, "exit_fees_usd": None,
            "avg_slippage_pct": None, "slippage_coverage_pct": None,
            "trades_per_day": None, "reliability": RELIABILITY_INSUFFICIENT,
            "unavailable": {"all": "amostra vazia"},
            "sharpe_note": "Sharpe por trade — NÃO anualizado",
        }

    rs = [row["realized_r"] for row in rows]
    classes = [row.get("outcome_class") for row in rows]
    wins = classes.count("win")
    losses = classes.count("loss")
    breakeven = classes.count("breakeven")
    expired = classes.count("expired")

    total_r = sum(rs)
    mean_r = total_r / n
    ordered = sorted(rs)
    mid = n // 2
    median_r = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    if n >= 2:
        var = sum((r - mean_r) ** 2 for r in rs) / (n - 1)
        std_r = math.sqrt(var)
    else:
        std_r = None
        unavailable["std_r"] = "amostra < 2"

    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rs) / n)

    sum_wins = sum(r for r in rs if r > 0)
    sum_losses_abs = abs(sum(r for r in rs if r < 0))
    if sum_losses_abs > 0:
        profit_factor = round(sum_wins / sum_losses_abs, 3)
    else:
        profit_factor = None
        unavailable["profit_factor"] = "sem perdas na amostra (indefinido)"

    if std_r and std_r > 0:
        sharpe = round(mean_r / std_r, 3)
    else:
        sharpe = None
        unavailable["sharpe_per_trade"] = "desvio-padrão zero ou amostra < 2"

    expectancy_ci = bootstrap_mean_ci(rs) if with_bootstrap else None
    if expectancy_ci is None and with_bootstrap:
        unavailable["expectancy_ci"] = "amostra < 2 para bootstrap"

    # ── TP1/TP2 só quando a fonte informa o marcador ────────────────────────
    tp1_flags = [row.get("tp1_hit") for row in rows if row.get("tp1_hit") is not None]
    tp2_flags = [row.get("tp2_hit") for row in rows if row.get("tp2_hit") is not None]
    tp1_rate = round(sum(1 for f in tp1_flags if f) / len(tp1_flags) * 100, 1) if tp1_flags else None
    tp2_rate = round(sum(1 for f in tp2_flags if f) / len(tp2_flags) * 100, 1) if tp2_flags else None
    if tp1_rate is None:
        unavailable["tp1_hit_rate_pct"] = "fonte não informa marcador de TP1"
    if tp2_rate is None:
        unavailable["tp2_hit_rate_pct"] = "fonte não informa marcador de TP2"

    # ── Financeiro: `pnl_usd` do RealTrade JÁ É LÍQUIDO (entry_fee + exit_fee
    # descontados em real_trade_service.close_trade). Fees aparecem SÓ como
    # informação — subtrair de novo seria double-count. ─────────────────────
    pnls = [_finite(row.get("pnl_usd")) for row in rows]
    pnls = [p for p in pnls if p is not None]
    net_pnl = round(sum(pnls), 4) if pnls else None
    if net_pnl is None:
        unavailable["net_pnl_usd"] = "fonte sem P&L em USD (shadow/backtest)"

    entry_fees = [_finite(row.get("entry_fee")) for row in rows]
    entry_fees = [f for f in entry_fees if f is not None]
    exit_fees = [_finite(row.get("exit_fee")) for row in rows]
    exit_fees = [f for f in exit_fees if f is not None]

    slips = [_finite(row.get("entry_slippage_pct")) for row in rows]
    slips = [s for s in slips if s is not None]
    avg_slip = round(sum(slips) / len(slips), 4) if slips else None
    if avg_slip is None:
        unavailable["avg_slippage_pct"] = "sem slippage registrado na amostra"

    # ── Volume por dia (janela observada real, não a janela pedida) ─────────
    first, last = rows[0]["resolved_at"], rows[-1]["resolved_at"]
    span_days = max((last - first).total_seconds() / 86400.0, 0.0)
    if span_days >= 1.0:
        per_day = round(n / span_days, 2)
    else:
        per_day = None
        unavailable["trades_per_day"] = "janela observada < 1 dia"

    return {
        "count": n,
        "wins": wins, "losses": losses, "breakeven": breakeven, "expired": expired,
        "win_rate_pct": round(wins / n * 100, 2),
        "win_rate_ci": wilson_interval(wins, n),
        "expectancy_r": round(mean_r, 4),
        "median_r": round(median_r, 4),
        "sum_r": round(total_r, 4),
        "std_r": round(std_r, 4) if std_r is not None else None,
        "downside_deviation_r": round(downside, 4),
        "profit_factor": profit_factor,
        "sharpe_per_trade": sharpe,
        "sharpe_note": "Sharpe por trade — NÃO anualizado",
        "max_drawdown_r": max_drawdown_r(rs),
        "worst_loss_streak": worst_loss_streak(rows),
        "expectancy_ci": expectancy_ci,
        "tp1_hit_rate_pct": tp1_rate,
        "tp2_hit_rate_pct": tp2_rate,
        "net_pnl_usd": net_pnl,
        "net_pnl_note": "pnl_usd já é LÍQUIDO (fees descontadas na origem); fees abaixo são informativas",
        "entry_fees_usd": round(sum(entry_fees), 4) if entry_fees else None,
        "exit_fees_usd": round(sum(exit_fees), 4) if exit_fees else None,
        "avg_slippage_pct": avg_slip,
        "slippage_coverage_pct": round(len(slips) / n * 100, 1),
        "trades_per_day": per_day,
        "observed_span_days": round(span_days, 2),
        "reliability": reliability_label(n),
        "unavailable": unavailable,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Segmentação
# ════════════════════════════════════════════════════════════════════════════
def _score_bin(score: Optional[float]) -> Optional[str]:
    s = _finite(score)
    if s is None:
        return None
    edges = [(0, 30), (30, 40), (40, 50), (50, 57), (57, 63), (63, 70), (70, 200)]
    for lo, hi in edges:
        if lo <= s < hi:
            return f"{lo}-{hi}"
    return None


def _atr_band(atr_pct: Optional[float]) -> Optional[str]:
    a = _finite(atr_pct)
    if a is None:
        return None
    if a < 0.5:
        return "<0.5%"
    if a < 1.0:
        return "0.5-1%"
    if a < 2.0:
        return "1-2%"
    if a < 3.0:
        return "2-3%"
    return ">=3%"


def _session_utc(hour: Optional[int]) -> Optional[str]:
    if hour is None:
        return None
    try:
        h = int(hour)
    except (TypeError, ValueError):
        return None
    if 0 <= h < 7:
        return "asia"
    if 7 <= h < 14:
        return "europe"
    if 14 <= h < 22:
        return "us"
    return "late"


def _base_of(symbol: str) -> str:
    s = (symbol or "").upper()
    if "/" in s:
        return s.split("/")[0]
    for q in ("USDT", "USDC", "BUSD"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def segment_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Quebra a amostra pelos eixos do diagnóstico. Cada segmento vem com
    confiabilidade — segmento pequeno não é apresentado como edge comprovado.

    NOTA metodológica: um mesmo trade aparece em VÁRIOS eixos (e em vários
    padrões). Padrões sobrepostos são ATRIBUIÇÃO, não causalidade.
    """
    def _bucket(getter) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        missing = 0
        for row in rows:
            key = getter(row)
            if key is None or key == "":
                missing += 1
                continue
            if isinstance(key, (list, tuple, set)):
                for k in key:                       # multi-rótulo (padrões)
                    if k:
                        groups.setdefault(str(k), []).append(row)
            else:
                groups.setdefault(str(key), []).append(row)
        items = []
        for key, grp in groups.items():
            m = compute_evidence_metrics(grp, with_bootstrap=False)
            items.append({
                "key": key,
                "count": m["count"],
                "wins": m["wins"], "losses": m["losses"],
                "breakeven": m["breakeven"], "expired": m["expired"],
                "win_rate_pct": m["win_rate_pct"],
                "win_rate_ci": m["win_rate_ci"],
                "avg_r": m["expectancy_r"],
                "total_r": m["sum_r"],
                "profit_factor": m["profit_factor"],
                "max_drawdown_r": m["max_drawdown_r"],
                "reliability": m["reliability"],
            })
        # Ordena por EVIDÊNCIA (R total ponderado pela confiabilidade), não por
        # win-rate cru — win-rate alto com n=3 não pode liderar a lista.
        order = {RELIABILITY_STRONG: 3, RELIABILITY_USABLE: 2,
                 RELIABILITY_EARLY: 1, RELIABILITY_INSUFFICIENT: 0}
        items.sort(key=lambda it: (order[it["reliability"]], it["total_r"] or 0.0), reverse=True)
        return {"items": items, "missing_feature": missing}

    def _feat(row, name):
        return (row.get("features") or {}).get(name)

    return {
        "by_tier": _bucket(lambda r: r.get("tier")),
        "by_timeframe": _bucket(lambda r: r.get("timeframe")),
        "by_direction": _bucket(lambda r: r.get("direction")),
        "by_tier_timeframe": _bucket(
            lambda r: f"{r.get('tier')}·{r.get('timeframe')}" if r.get("tier") and r.get("timeframe") else None),
        "by_base": _bucket(lambda r: _base_of(r.get("symbol") or "")),
        "by_pattern": _bucket(lambda r: _feat(r, "patterns")),
        "by_session_utc": _bucket(lambda r: _session_utc(_feat(r, "hour_utc"))),
        "by_day_of_week": _bucket(lambda r: _feat(r, "day_of_week")),
        "by_regime": _bucket(lambda r: _feat(r, "regime")),
        "by_funding_sentiment": _bucket(lambda r: _feat(r, "funding_sentiment")),
        "by_score_bin": _bucket(lambda r: _score_bin(r.get("score"))),
        "by_atr_band": _bucket(lambda r: _atr_band(_feat(r, "atr_pct"))),
        "by_mtf_aligned": _bucket(lambda r: _feat(r, "mtf_aligned")),
        "by_entry_zone_type": _bucket(lambda r: _feat(r, "entry_zone_type")),
        "_note": ("padrões sobrepostos são atribuição, não causalidade; "
                  "um trade pode aparecer em mais de um segmento"),
    }


def feature_coverage(rows: Sequence[Dict[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
    """% de linhas com cada feature realmente presente (não-None)."""
    n = len(rows)
    out: Dict[str, Any] = {}
    for name in names:
        if n == 0:
            out[name] = {"present": 0, "total": 0, "coverage_pct": None}
            continue
        present = sum(1 for r in rows if (r.get("features") or {}).get(name) is not None)
        out[name] = {"present": present, "total": n,
                     "coverage_pct": round(present / n * 100, 1)}
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Champion — descoberta do comportamento ATUAL
# ════════════════════════════════════════════════════════════════════════════
def discover_champion_config() -> Dict[str, Any]:
    """Lê os knobs REAIS do champion da MESMA env, com os MESMOS defaults do
    `shadow_trade_service`/`recommendation_service`.

    Não importa aqueles módulos de propósito: eles carregam SDK de exchange e o
    P05 precisa ser puro/hermético. O acoplamento é por CONTRATO de env — se um
    default mudar lá, tem que mudar aqui (documentado no P05).
    """
    score_formula_v2 = _env_bool("SCORE_FORMULA_V2", "false")
    if score_formula_v2:
        score_min = _env_float("SCORE_MIN_V2", 57.0)
        score_knob = "SCORE_MIN_V2"
    else:
        score_min = _env_float("SCORE_MIN", 72.0)
        score_knob = "SCORE_MIN"
    return {
        "schema_version": FEATURES_SCHEMA_VERSION,
        "score_formula_v2": score_formula_v2,
        "score_min_knob": score_knob,
        "SCORE_MIN": score_min,
        "TF_MIN_TIER": os.getenv("TF_MIN_TIER", "15m:A+,1h:A,4h:B").strip(),
        "QUALITY_EDGE_GATE_ENABLED": _env_bool("QUALITY_EDGE_GATE_ENABLED", "false"),
        "QUALITY_EDGE_MARGIN": _env_float("QUALITY_EDGE_MARGIN", 6.0),
        "QUALITY_EDGE_MIN": _env_int("QUALITY_EDGE_MIN", 1),
        "MTF_ALIGNED_MODE": os.getenv("MTF_ALIGNED_MODE", "boost").strip().lower(),
        "MTF_ALIGNED_MIN_COUNT": _env_int("MTF_ALIGNED_MIN_COUNT", 2),
        "SCORE_ADJUSTERS_ENABLED": _env_bool("SCORE_ADJUSTERS_ENABLED", "true"),
        "SCORE_ADJUSTER_CAP": _env_float("SCORE_ADJUSTER_CAP", 20.0),
        "PROXIMITY_GATE_ENABLED": _env_bool("PROXIMITY_GATE_ENABLED", "true"),
        "PROXIMITY_MAX_ATR": _env_float("PROXIMITY_MAX_ATR", 1.0),
        "BREAKOUT_LANE_ENABLED": _env_bool("BREAKOUT_LANE_ENABLED", "false"),
        "BREAKOUT_LANE_MAX_ATR": _env_float("BREAKOUT_LANE_MAX_ATR", 2.0),
        "STRUCT_CHASE_GATE_ENABLED": _env_bool("STRUCT_CHASE_GATE_ENABLED", "false"),
        "STRUCT_CHASE_MAX_ATR": _env_float("STRUCT_CHASE_MAX_ATR", 5.0),
        "ATR_GATE_ENABLED": _env_bool("ATR_GATE_ENABLED", "true"),
        "ATR_BLOCK_THRESHOLD": _env_float("ATR_BLOCK_THRESHOLD", 3.0),
        "FUNDING_GATE_ENABLED": _env_bool("FUNDING_GATE_ENABLED", "true"),
        "FUNDING_BLOCK_THRESHOLD": _env_float("FUNDING_BLOCK_THRESHOLD", 0.05),
        "BLOCK_HOURS_UTC": os.getenv("BLOCK_HOURS_UTC", "").strip(),
        "BLOCK_DAYS_UTC": os.getenv("BLOCK_DAYS_UTC", "").strip().lower(),
    }


# Knobs permitidos + limites conservadores. NADA de safety/live entra aqui.
KNOB_ALLOWLIST: Dict[str, Dict[str, Any]] = {
    "SCORE_MIN": {"type": "float", "min": 15.0, "max": 95.0, "max_delta": 6.0,
                  "features": ["__score__"]},
    "TF_MIN_TIER": {"type": "str", "features": ["__tier__", "__timeframe__"]},
    "QUALITY_EDGE_GATE_ENABLED": {"type": "bool", "features": ["edge_score"]},
    "QUALITY_EDGE_MARGIN": {"type": "float", "min": 0.0, "max": 15.0, "max_delta": 3.0,
                            "features": ["edge_score"]},
    "MTF_ALIGNED_MODE": {"type": "str", "choices": ["off", "boost", "required"],
                         "features": ["mtf_aligned"]},
    "MTF_ALIGNED_MIN_COUNT": {"type": "int", "min": 1, "max": 4, "max_delta": 1,
                              "features": ["mtf_aligned"]},
    "PROXIMITY_MAX_ATR": {"type": "float", "min": 0.25, "max": 3.0, "max_delta": 0.5,
                          "features": ["chase_atr"]},
    "STRUCT_CHASE_GATE_ENABLED": {"type": "bool", "features": ["struct_chase_atr"]},
    "STRUCT_CHASE_MAX_ATR": {"type": "float", "min": 1.0, "max": 12.0, "max_delta": 2.0,
                             "features": ["struct_chase_atr"]},
}

# Qualquer tentativa de mexer nisso é REJEITADA — P05 nunca toca safety/live.
FORBIDDEN_KNOB_TOKENS = (
    "P04A", "P04B", "P04C", "LIVE", "SIZE", "QTY", "LEVERAGE", "STOP", "TP1", "TP2",
    "KILL", "PORTFOLIO", "CIRCUIT", "MAKER", "FALLBACK", "SLIPPAGE", "DEPTH",
    "EXPOSURE", "RISK", "PYRAMID", "HEDGE", "TF_UPGRADE", "LEARNING_AUTO",
)


class CandidateValidationError(ValueError):
    """Configuração de candidato inválida — rejeitada sem persistência."""


def validate_candidate_config(champion: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Valida a config canônica do candidato. Levanta `CandidateValidationError`.

    Regras duras: exatamente UM knob; knob na allowlist; tipo correto; sem
    NaN/infinito; dentro dos limites; delta conservador; nada de safety/live.
    """
    if not isinstance(config, dict) or not config:
        raise CandidateValidationError("configuração vazia")
    if len(config) != 1:
        raise CandidateValidationError(
            f"exatamente 1 knob por candidato (recebidos {len(config)}: {sorted(config)})")

    knob, value = next(iter(config.items()))
    if not isinstance(knob, str):
        raise CandidateValidationError("nome de knob inválido")
    upper = knob.upper()
    for token in FORBIDDEN_KNOB_TOKENS:
        if token in upper and upper not in KNOB_ALLOWLIST:
            raise CandidateValidationError(f"knob proibido (safety/live): {knob}")
    spec = KNOB_ALLOWLIST.get(knob)
    if spec is None:
        raise CandidateValidationError(f"knob fora da allowlist: {knob}")

    champ_value = champion.get(knob)
    kind = spec["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise CandidateValidationError(f"{knob} exige boolean real (recebido {type(value).__name__})")
    elif kind == "str":
        if not isinstance(value, str) or not value.strip():
            raise CandidateValidationError(f"{knob} exige string não-vazia")
        choices = spec.get("choices")
        if choices and value.strip().lower() not in choices:
            raise CandidateValidationError(f"{knob} fora das opções {choices}")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CandidateValidationError(f"{knob} exige número")
        num = _finite(value)
        if num is None:
            raise CandidateValidationError(f"{knob}: NaN/infinito rejeitado")
        if num < spec["min"] or num > spec["max"]:
            raise CandidateValidationError(
                f"{knob}={num} fora dos limites [{spec['min']}, {spec['max']}]")
        champ_num = _finite(champ_value)
        if champ_num is not None and abs(num - champ_num) > spec["max_delta"] + 1e-9:
            raise CandidateValidationError(
                f"{knob}: delta {abs(num - champ_num):.3f} excede o máximo conservador {spec['max_delta']}")
        if kind == "int" and float(num).is_integer() is False:
            raise CandidateValidationError(f"{knob} exige inteiro")
        value = int(num) if kind == "int" else float(num)

    if champ_value is not None and value == champ_value:
        raise CandidateValidationError(f"{knob} igual ao champion — não é candidato")
    return {knob: value}


def canonical_hash(payload: Any) -> str:
    """SHA-256 de JSON ORDENADO — mesma config ⇒ mesmo hash, sempre."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def dataset_fingerprint(rows: Sequence[Dict[str, Any]]) -> str:
    """Impressão digital determinística da amostra (identidade + R + resolução)."""
    items = [
        [str(r.get("dedupe_key")), round(float(r["realized_r"]), 6),
         r["resolved_at"].isoformat()]
        for r in rows
    ]
    items.sort()
    return canonical_hash(items)


def build_experiment_key(champion_hash: str, candidate_hash: str, cutoff: datetime) -> str:
    """Identidade lógica: mesmo candidato + champion + cutoff ⇒ MESMO experimento."""
    return f"{champion_hash}:{candidate_hash}:{_utc(cutoff).isoformat()}"


# ════════════════════════════════════════════════════════════════════════════
#  Contrafactual — quem o champion/candidato teria operado
# ════════════════════════════════════════════════════════════════════════════
_TIER_RANK = {"A+": 3, "A": 2, "B": 1, "C": 0}


def _parse_tf_min_tier(spec: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        tf, tier = part.split(":", 1)
        tf, tier = tf.strip(), tier.strip().upper()
        if tf and tier in _TIER_RANK:
            out[tf] = tier
    return out


def _execution_score(row: Dict[str, Any], config: Dict[str, Any]) -> Optional[float]:
    """Espelha o score efetivamente comparado com SCORE_MIN no executor.

    O executor aplica os adjusters antes do gate. Reproduzimos a fórmula pura
    usando apenas features persistidas no instante da recomendação; dado ausente
    conserva delta zero, exatamente como o caminho real.
    """
    score = _finite(row.get("score"))
    if score is None or not config.get("SCORE_ADJUSTERS_ENABLED"):
        return score
    f = row.get("features") or {}
    delta = 0.0
    atr = _finite(f.get("atr_pct"))
    if atr is not None:
        delta += -8 if atr > 3.0 else (6 if atr < 1.0 else 0)
    conf = _finite(f.get("confluence_pct"))
    if conf is not None:
        delta += -4 if conf < 50 else (12 if conf <= 70 else 0)
    rsi = _finite(f.get("rsi"))
    if rsi is not None and rsi < 30:
        delta -= 7
    adx = _finite(f.get("adx"))
    if adx is not None:
        delta += 6 if adx < 20 else (-2 if adx > 30 else 0)
    mtf = _finite(f.get("mtf_aligned"))
    if mtf is not None and mtf >= 2:
        delta += 8
    if f.get("funding_sentiment") == "neutral":
        delta += 6
    funding = _finite(f.get("funding_pct"))
    if funding is not None and -0.05 <= funding <= 0.05:
        delta += 6
    weights = {
        "descending_channel": 12,
        "descending_wedge": 10,
        "inverse_head_and_shoulders": 7,
        "double_bottom": 6,
    }
    patterns = f.get("patterns") or []
    if isinstance(patterns, list):
        delta += sum(weights.get(p, 0) for p in patterns)
    hour = f.get("hour_utc")
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        hour = None
    if hour is not None:
        delta += 6 if 0 <= hour <= 6 else (5 if 14 <= hour <= 21 else 0)
    cap = abs(_finite(config.get("SCORE_ADJUSTER_CAP")) or 20.0)
    return score + max(-cap, min(delta, cap))


def _csv_set(value: Any, *, ints: bool = False) -> set:
    out = set()
    for item in str(value or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        try:
            out.add(int(item) if ints else item)
        except (TypeError, ValueError):
            continue
    return out


def eligibility(row: Dict[str, Any], config: Dict[str, Any],
                active_components: Sequence[str]) -> Optional[bool]:
    """O setup passaria pelos gates desta configuração?

    Retorna True/False, ou **None quando algum dado exigido está ausente** —
    UNKNOWN nunca vira ELIGIBLE nem BLOCKED por fallback.

    Só avalia os componentes em `active_components` (aqueles cuja feature tem
    cobertura suficiente na amostra). Componentes desligados valem igual para
    champion e candidato, então não distorcem a comparação.
    """
    feats = row.get("features") or {}

    if "score_min" in active_components:
        score = _execution_score(row, config)
        if score is None:
            return None
        score_min = _finite(config.get("SCORE_MIN"))
        if score_min is None:
            return None
        if score < score_min:
            return False

    if "tf_min_tier" in active_components:
        tier = (row.get("tier") or "").strip().upper()
        tf = (row.get("timeframe") or "").strip()
        if not tier or not tf or tier not in _TIER_RANK:
            return None
        min_map = _parse_tf_min_tier(config.get("TF_MIN_TIER") or "")
        required = min_map.get(tf)
        if required is not None and _TIER_RANK[tier] < _TIER_RANK[required]:
            return False

    if "base_quality" in active_components:
        verdict_ok = feats.get("bot_verdict_ok")
        blocked_by = feats.get("bot_verdict_blocked_by") or feats.get("bot_verdict_code")
        if verdict_ok is None:
            return None
        if verdict_ok is False:
            # Quality-edge pode ser justamente o knob testado; os demais gates
            # do bot_verdict (RR/P(TP1)/liquidez) permanecem bloqueio simétrico.
            if blocked_by == "quality-edge-gate":
                pass
            elif blocked_by:
                return False
            else:
                return None

    if "quality_edge" in active_components and config.get("QUALITY_EDGE_GATE_ENABLED"):
        score = _execution_score(row, config)
        smin = _finite(config.get("SCORE_MIN"))
        margin = _finite(config.get("QUALITY_EDGE_MARGIN")) or 0.0
        if score is None or smin is None:
            return None
        if smin <= score < smin + margin:          # banda marginal exige edge
            edge = _finite(feats.get("edge_score"))
            if edge is None:
                return None
            edge_min = _finite(config.get("QUALITY_EDGE_MIN"))
            if edge_min is None:
                return None
            if edge < edge_min:
                return False

    if "mtf" in active_components and (config.get("MTF_ALIGNED_MODE") == "required"):
        aligned = feats.get("mtf_aligned")
        n_aligned = _finite(aligned)
        if n_aligned is None:
            return None
        if n_aligned < (_finite(config.get("MTF_ALIGNED_MIN_COUNT")) or 0):
            return False

    if "proximity" in active_components and config.get("PROXIMITY_GATE_ENABLED"):
        chase = _finite(feats.get("chase_atr"))
        if chase is None:
            return None
        ceiling = _finite(config.get("PROXIMITY_MAX_ATR"))
        if ceiling is None:
            return None
        if chase >= ceiling:
            if config.get("BREAKOUT_LANE_ENABLED"):
                breakout_ceiling = _finite(config.get("BREAKOUT_LANE_MAX_ATR"))
                # O bias macro usado pela lane não é reconstruível fielmente.
                # Acima do teto estendido o bloqueio é certo; na faixa entre os
                # tetos o veredito é UNKNOWN, nunca um passe inventado.
                if breakout_ceiling is None or chase < breakout_ceiling:
                    return None
            return False

    if "struct_chase" in active_components and config.get("STRUCT_CHASE_GATE_ENABLED"):
        if feats.get("retest_armed") is True:
            pass
        else:
            struct = _finite(feats.get("struct_chase_atr"))
            if struct is None:
                return None
            ceiling = _finite(config.get("STRUCT_CHASE_MAX_ATR"))
            if ceiling is None:
                return None
            if struct >= ceiling:
                return False

    if "atr_gate" in active_components and config.get("ATR_GATE_ENABLED"):
        atr = _finite(feats.get("atr_pct"))
        if atr is None:
            return None
        threshold = _finite(config.get("ATR_BLOCK_THRESHOLD"))
        if threshold is None:
            return None
        if atr > threshold:
            return False

    if "funding_gate" in active_components and config.get("FUNDING_GATE_ENABLED"):
        funding = _finite(feats.get("funding_pct"))
        direction = (row.get("direction") or "").strip().lower()
        if funding is None or direction not in ("long", "short"):
            return None
        threshold = _finite(config.get("FUNDING_BLOCK_THRESHOLD"))
        if threshold is None:
            return None
        if (direction == "long" and funding > threshold) or (
            direction == "short" and funding < -threshold
        ):
            return False

    if "time_gate" in active_components:
        hour = feats.get("hour_utc")
        dow = feats.get("day_of_week")
        try:
            hour = int(hour)
            dow = int(dow)
        except (TypeError, ValueError):
            return None
        if hour in _csv_set(config.get("BLOCK_HOURS_UTC"), ints=True):
            return False
        day_names = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
        if day_names.get(dow) in _csv_set(config.get("BLOCK_DAYS_UTC")):
            return False

    return True


_COMPONENT_FEATURES = {
    "score_min": "__score__",
    "tf_min_tier": "__tier__",
    "base_quality": "bot_verdict_ok",
    "quality_edge": "edge_score",
    "mtf": "mtf_aligned",
    "proximity": "chase_atr",
    "struct_chase": "struct_chase_atr",
    "atr_gate": "atr_pct",
    "funding_gate": "funding_pct",
    "time_gate": "hour_utc",
}
_KNOB_COMPONENT = {
    "SCORE_MIN": "score_min",
    "TF_MIN_TIER": "tf_min_tier",
    "QUALITY_EDGE_GATE_ENABLED": "quality_edge",
    "QUALITY_EDGE_MARGIN": "quality_edge",
    "MTF_ALIGNED_MODE": "mtf",
    "MTF_ALIGNED_MIN_COUNT": "mtf",
    "PROXIMITY_MAX_ATR": "proximity",
    "STRUCT_CHASE_GATE_ENABLED": "struct_chase",
    "STRUCT_CHASE_MAX_ATR": "struct_chase",
}


def component_coverage(rows: Sequence[Dict[str, Any]], component: str) -> Optional[float]:
    """Cobertura (%) do dado que o componente exige."""
    n = len(rows)
    if n == 0:
        return None
    feat = _COMPONENT_FEATURES[component]
    if feat == "__score__":
        present = sum(1 for r in rows if _finite(r.get("score")) is not None)
    elif feat == "__tier__":
        present = sum(1 for r in rows if (r.get("tier") or "") and (r.get("timeframe") or ""))
    elif component == "time_gate":
        present = sum(
            1 for r in rows
            if (r.get("features") or {}).get("hour_utc") is not None
            and (r.get("features") or {}).get("day_of_week") is not None
        )
    else:
        present = sum(1 for r in rows if (r.get("features") or {}).get(feat) is not None)
    return round(present / n * 100, 1)


def active_components_for(rows: Sequence[Dict[str, Any]], knob: str) -> Tuple[List[str], Dict[str, Any]]:
    """Componentes avaliáveis: o do knob (obrigatório) + os demais com cobertura
    suficiente. Cobertura insuficiente ⇒ componente desligado para AMBOS os
    lados (simetria) e registrado como limitação."""
    coverage = {c: component_coverage(rows, c) for c in _COMPONENT_FEATURES}
    active: List[str] = []
    skipped: Dict[str, Any] = {}
    target = _KNOB_COMPONENT[knob]
    for comp, cov in coverage.items():
        if cov is not None and cov >= P05_MIN_FEATURE_COVERAGE_PCT:
            active.append(comp)
        elif comp == target:
            active.append(comp)                 # decidido fora (gate de cobertura)
        else:
            skipped[comp] = cov
    return active, {"coverage_pct": coverage, "skipped_low_coverage": skipped}


def split_by_config(rows: Sequence[Dict[str, Any]], config: Dict[str, Any],
                    active: Sequence[str]) -> Tuple[List[Dict[str, Any]], int]:
    """(linhas que a config operaria, nº de UNKNOWN excluídos)."""
    taken: List[Dict[str, Any]] = []
    unknown = 0
    for row in rows:
        verdict = eligibility(row, config, active)
        if verdict is None:
            unknown += 1
        elif verdict:
            taken.append(row)
    return taken, unknown


def _material_segment_regressions(
    champion_rows: Sequence[Dict[str, Any]],
    candidate_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Detecta bolsões materiais com expectancy negativa no candidato.

    Um segmento só é material com pelo menos 10 casos e 10% da amostra do
    candidato. Eixos sobrepostos são diagnóstico conservador, não causalidade.
    """
    if not candidate_rows:
        return []
    min_n = max(10, math.ceil(len(candidate_rows) * 0.10))
    axes = {
        "direction": lambda r: r.get("direction"),
        "timeframe": lambda r: r.get("timeframe"),
        "tier": lambda r: r.get("tier"),
        "regime": lambda r: (r.get("features") or {}).get("regime"),
    }
    bad: List[Dict[str, Any]] = []
    for axis, getter in axes.items():
        cgroups: Dict[str, List[Dict[str, Any]]] = {}
        hgroups: Dict[str, List[Dict[str, Any]]] = {}
        for row in candidate_rows:
            key = getter(row)
            if key is not None:
                cgroups.setdefault(str(key), []).append(row)
        for row in champion_rows:
            key = getter(row)
            if key is not None:
                hgroups.setdefault(str(key), []).append(row)
        for key, group in cgroups.items():
            if len(group) < min_n:
                continue
            cm = compute_evidence_metrics(group, with_bootstrap=False)
            hm = compute_evidence_metrics(hgroups.get(key, []), with_bootstrap=False)
            cexp = cm.get("expectancy_r")
            hexp = hm.get("expectancy_r")
            if cexp is not None and cexp < 0 and (hexp is None or cexp < hexp):
                bad.append({"axis": axis, "segment": key, "count": len(group),
                            "candidate_expectancy_r": cexp,
                            "champion_expectancy_r": hexp})
    return bad


def compare_configs(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                    candidate_cfg: Dict[str, Any], active: Sequence[str]) -> Dict[str, Any]:
    """Compara champion × candidato sobre o MESMO dataset.

    Linhas com dado ausente para algum componente ativo são excluídas dos DOIS
    lados (comparação simétrica), e o subconjunto selecionado por cada lado fica
    EXPLÍCITO — correlação aqui não é causalidade.
    """
    merged = dict(champion)
    merged.update(candidate_cfg)
    evaluable = [r for r in rows if eligibility(r, champion, active) is not None
                 and eligibility(r, merged, active) is not None]
    champ_rows, _ = split_by_config(evaluable, champion, active)
    cand_rows, _ = split_by_config(evaluable, merged, active)

    champ_m = compute_evidence_metrics(champ_rows)
    cand_m = compute_evidence_metrics(cand_rows)
    champ_keys = {id(r) for r in champ_rows}
    cand_keys = {id(r) for r in cand_rows}
    added = [r for r in cand_rows if id(r) not in champ_keys]
    avoided = [r for r in champ_rows if id(r) not in cand_keys]

    delta_ci = bootstrap_paired_selection_delta_ci(rows, champion, candidate_cfg, active)
    return {
        "evaluable": len(evaluable),
        "unknown_excluded": len(rows) - len(evaluable),
        "champion": champ_m,
        "candidate": cand_m,
        "added_ops": len(added),
        "avoided_ops": len(avoided),
        "added_expectancy_r": (round(sum(r["realized_r"] for r in added) / len(added), 4)
                               if added else None),
        "avoided_expectancy_r": (round(sum(r["realized_r"] for r in avoided) / len(avoided), 4)
                                 if avoided else None),
        "delta_expectancy_ci": delta_ci,
        "material_segment_regressions": _material_segment_regressions(champ_rows, cand_rows),
        "selects_subset": len(cand_rows) < len(champ_rows),
        "note": "champion e candidato avaliados sobre o MESMO dataset; UNKNOWN excluído dos dois lados",
    }


# ════════════════════════════════════════════════════════════════════════════
#  Validação temporal (sem leakage)
# ════════════════════════════════════════════════════════════════════════════
def temporal_split(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Corte CRONOLÓGICO 50/25/25 (treino/validação/teste). O teste final fica
    INTOCADO — nunca é usado para escolher ou ajustar threshold."""
    ordered = sorted(rows, key=lambda r: r["resolved_at"])
    n = len(ordered)
    if n < P05_MIN_OFFLINE_RESOLVED:
        return {"ok": False, "reason": "INSUFFICIENT_DATA", "total": n,
                "required": P05_MIN_OFFLINE_RESOLVED}
    i_train = int(n * 0.5)
    i_valid = int(n * 0.75)
    folds = 6 if n >= 120 else 4
    return {
        "ok": True, "total": n, "folds": folds,
        "train": ordered[:i_train],
        "validation": ordered[i_train:i_valid],
        "test": ordered[i_valid:],
        "boundaries": {
            "train_end": ordered[i_train - 1]["resolved_at"].isoformat() if i_train else None,
            "validation_end": ordered[i_valid - 1]["resolved_at"].isoformat() if i_valid else None,
            "test_end": ordered[-1]["resolved_at"].isoformat(),
        },
    }


def walkforward_folds(rows: Sequence[Dict[str, Any]], folds: int) -> List[Dict[str, Any]]:
    """Folds CRONOLÓGICOS crescentes (treino sempre no passado do teste)."""
    ordered = sorted(rows, key=lambda r: r["resolved_at"])
    n = len(ordered)
    out: List[Dict[str, Any]] = []
    if n < folds * 2:
        return out
    step = n // (folds + 1)
    for k in range(1, folds + 1):
        cut = step * k
        end = min(step * (k + 1), n)
        if cut <= 0 or end <= cut:
            continue
        out.append({"fold": k, "train": ordered[:cut], "test": ordered[cut:end]})
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Gates offline
# ════════════════════════════════════════════════════════════════════════════
def _pf(m: Dict[str, Any]) -> float:
    """Profit factor comparável: None (sem perdas) conta como muito alto."""
    v = m.get("profit_factor")
    return float("inf") if v is None and m.get("count") else (v if v is not None else 0.0)


def evaluate_offline_gate(objective: str, validation: Dict[str, Any],
                          test: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica os critérios do objetivo. Nunca força vencedor: sem candidato
    válido é resultado ACEITÁVEL."""
    checks: List[Dict[str, Any]] = []

    def _chk(name: str, passed: Optional[bool], detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    cand_v, champ_v = validation["candidate"], validation["champion"]
    cand_t, champ_t = test["candidate"], test["champion"]

    if cand_t["count"] < P05_MIN_OOS_RESOLVED:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "OOS_SAMPLE_TOO_SMALL",
                "detail": f"teste final tem {cand_t['count']} outcomes do candidato "
                          f"(< {P05_MIN_OOS_RESOLVED})",
                "checks": checks}

    if objective not in OBJECTIVES:
        return {"verdict": STATUS_REJECTED, "reason_code": "OBJETIVO_INVALIDO",
                "detail": objective, "checks": checks}

    # O holdout confirma os MESMOS contratos da validação. Não basta terminar
    # levemente positivo se PF/drawdown/volume ou as operações adicionais
    # colapsaram fora da amostra.
    for stage, comparison in (("validacao", validation), ("teste", test)):
        cand = comparison["candidate"]
        champ = comparison["champion"]
        exp = cand.get("expectancy_r")
        _chk(f"{stage}_expectancy_candidate_positiva",
             exp is not None and exp > 0 and (cand.get("sum_r") or 0) > 0,
             f"expectancy={exp} total_r={cand.get('sum_r')}")

        if objective == OBJECTIVE_LOSS_REDUCTION:
            dci = comparison.get("delta_expectancy_ci")
            _chk(f"{stage}_ci_delta_expectancy_acima_de_zero",
                 bool(dci) and dci.get("low") is not None and dci["low"] > 0,
                 f"IC do delta={dci}")
            _chk(f"{stage}_profit_factor_nao_pior",
                 _pf(cand) >= _pf(champ),
                 f"cand={cand.get('profit_factor')} champ={champ.get('profit_factor')}")
            _chk(f"{stage}_drawdown_nao_pior",
                 (cand.get("max_drawdown_r") or 0) <= (champ.get("max_drawdown_r") or 0) + 1e-9,
                 f"cand={cand.get('max_drawdown_r')} champ={champ.get('max_drawdown_r')}")
            _chk(f"{stage}_operacoes_min_70pct",
                 champ.get("count", 0) > 0 and cand.get("count", 0) >= 0.7 * champ["count"],
                 f"cand={cand.get('count')} champ={champ.get('count')}")
        else:
            _chk(f"{stage}_operacoes_min_110pct",
                 champ.get("count", 0) > 0 and cand.get("count", 0) >= 1.1 * champ["count"],
                 f"cand={cand.get('count')} champ={champ.get('count')}")
            eci = cand.get("expectancy_ci")
            _chk(f"{stage}_ci_expectancy_acima_de_zero",
                 bool(eci) and eci.get("low") is not None and eci["low"] > 0, f"IC={eci}")
            _chk(f"{stage}_total_r_maior",
                 (cand.get("sum_r") or 0) > (champ.get("sum_r") or 0),
                 f"cand={cand.get('sum_r')} champ={champ.get('sum_r')}")
            _chk(f"{stage}_profit_factor_min_1",
                 _pf(cand) >= 1.0, f"cand={cand.get('profit_factor')}")
            _chk(f"{stage}_drawdown_max_110pct",
                 (cand.get("max_drawdown_r") or 0) <= 1.1 * (champ.get("max_drawdown_r") or 0) + 1e-9,
                 f"cand={cand.get('max_drawdown_r')} champ={champ.get('max_drawdown_r')}")
            add_exp = comparison.get("added_expectancy_r")
            _chk(f"{stage}_operacoes_adicionais_positivas",
                 add_exp is not None and add_exp > 0,
                 f"expectancy das adicionais={add_exp}")

        regressions = comparison.get("material_segment_regressions") or []
        _chk(f"{stage}_sem_segmento_material_negativo", not regressions,
             f"regressões={regressions[:5]}")

    failed = [c["check"] for c in checks if not c["passed"]]
    if failed:
        return {"verdict": STATUS_REJECTED, "reason_code": "GATE_NAO_ATENDIDO",
                "detail": f"falhou: {', '.join(failed)}", "checks": checks}
    return {"verdict": STATUS_OFFLINE_VALIDATED, "reason_code": "GATES_OK",
            "detail": "validação e teste positivos", "checks": checks}


def evaluate_shadow_gate(shadow_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Gate do P05C/P05D. Métricas que dependem de UNKNOWN não aprovam nada."""
    checks: List[Dict[str, Any]] = []

    def _chk(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    # Maturidade é medida pela coorte prospectiva total. Usar apenas as linhas
    # aceitas pelo challenger faria um candidato restritivo esperar para sempre;
    # a cobertura abaixo é quem reprova anotações/decisões ausentes.
    resolved = shadow_metrics.get("resolved") or 0
    days = shadow_metrics.get("observed_days") or 0
    coverage = shadow_metrics.get("coverage_pct")
    cand = shadow_metrics.get("candidate") or {}

    # Safety é pré-condição, não depende de maturidade estatística. Incidente,
    # drift ou falha ao provar integridade rejeitam antes do "aguardando amostra".
    safety_checks = [
        ("sem_incidente_operacional",
         shadow_metrics.get("operational_incident") is False,
         f"incidente={shadow_metrics.get('operational_incident')}"),
        ("sem_relaxamento_p01_p04",
         shadow_metrics.get("safety_relaxed") is False,
         f"safety_relaxed={shadow_metrics.get('safety_relaxed')}"),
    ]
    for name, passed, detail in safety_checks:
        _chk(name, passed, detail)
    if any(not passed for _name, passed, _detail in safety_checks):
        failed = [c["check"] for c in checks if not c["passed"]]
        return {"verdict": STATUS_REJECTED, "reason_code": "SHADOW_SAFETY_NOT_PROVEN",
                "detail": f"falhou: {', '.join(failed)}", "checks": checks}

    if resolved < P05_MIN_SHADOW_RESOLVED or days < P05_MIN_SHADOW_DAYS:
        return {"verdict": STATUS_INSUFFICIENT, "reason_code": "AGUARDANDO_AMOSTRA",
                "detail": (f"{resolved}/{P05_MIN_SHADOW_RESOLVED} outcomes · "
                           f"{days}/{P05_MIN_SHADOW_DAYS} dias"),
                "checks": checks}

    _chk("cobertura_minima", coverage is not None and coverage >= P05_MIN_SHADOW_COVERAGE_PCT,
         f"cobertura={coverage}% (mín {P05_MIN_SHADOW_COVERAGE_PCT}%)")
    _chk("expectancy_positiva", (cand.get("expectancy_r") or 0) > 0,
         f"expectancy={cand.get('expectancy_r')}")
    _chk("objetivo_offline_mantido", bool(shadow_metrics.get("objective_still_met")),
         "critério do objetivo continua atendido no shadow")
    failed = [c["check"] for c in checks if not c["passed"]]
    if failed:
        return {"verdict": STATUS_REJECTED, "reason_code": "SHADOW_GATE_NAO_ATENDIDO",
                "detail": f"falhou: {', '.join(failed)}", "checks": checks}
    return {"verdict": STATUS_ELIGIBLE, "reason_code": "SHADOW_GATE_OK",
            "detail": "pode ser apresentado ao usuário para autorização MANUAL",
            "checks": checks}


def build_promotion_plan(champion: Dict[str, Any], candidate_cfg: Dict[str, Any],
                         evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Plano de ativação MANUAL. Não existe endpoint que aplique isto."""
    knob, value = next(iter(candidate_cfg.items()))
    env_name = champion.get("score_min_knob", "SCORE_MIN") if knob == "SCORE_MIN" else knob
    return {
        "diff": {"knob": knob, "env": env_name,
                 "current_value": champion.get(knob), "proposed_value": value},
        "env_atual": {env_name: champion.get(knob)},
        "env_proposta": {env_name: value},
        "evidencia": evidence,
        "riscos": [
            "amostra shadow é de SETUPS, não de execução real (fill/slippage podem diferir)",
            "regime de mercado pode mudar após a decisão",
            "segmentos pequenos não são edge comprovado",
        ],
        "plano_canario": [
            "1. aplicar a env proposta apenas no ambiente de TESTES (Crypto-Agente-Dev)",
            "2. observar ≥ 1 semana com o painel de assertividade",
            "3. só então considerar PRD, mantendo LIVE_SIZE_MULT inalterado",
        ],
        "condicao_rollback": (
            "expectancy < 0 na janela de 7 dias, drawdown acima do champion, "
            "ou qualquer incidente operacional"
        ),
        "valores_rollback": {env_name: champion.get(knob)},
        "aviso": "ELIGIBLE = pode ser APRESENTADO para autorização. NÃO foi ativado.",
    }


# ════════════════════════════════════════════════════════════════════════════
#  Geração de candidatos (máx. 12, UM knob cada, sem grid search)
# ════════════════════════════════════════════════════════════════════════════
def generate_candidates(champion: Dict[str, Any],
                        rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Propostas conservadoras, uma por knob/direção. Sem combinatória.

    Um knob só entra quando: valor champion descoberto, feature persistida,
    cobertura ≥ P05_MIN_FEATURE_COVERAGE_PCT e amostra suficiente.
    """
    proposals: List[Tuple[str, Any, str]] = []
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    smin = _finite(champion.get("SCORE_MIN"))
    if smin is not None:
        proposals.append(("SCORE_MIN", round(smin + 2, 2), OBJECTIVE_LOSS_REDUCTION))
        proposals.append(("SCORE_MIN", round(smin + 4, 2), OBJECTIVE_LOSS_REDUCTION))
        proposals.append(("SCORE_MIN", round(smin - 2, 2), OBJECTIVE_MORE_OPERATIONS))
        proposals.append(("SCORE_MIN", round(smin - 4, 2), OBJECTIVE_MORE_OPERATIONS))
    if not champion.get("QUALITY_EDGE_GATE_ENABLED"):
        proposals.append(("QUALITY_EDGE_GATE_ENABLED", True, OBJECTIVE_LOSS_REDUCTION))
    else:
        proposals.append(("QUALITY_EDGE_GATE_ENABLED", False, OBJECTIVE_MORE_OPERATIONS))
        margin = _finite(champion.get("QUALITY_EDGE_MARGIN"))
        if margin is not None:
            proposals.append(("QUALITY_EDGE_MARGIN", round(margin + 2, 2), OBJECTIVE_LOSS_REDUCTION))
    mode = champion.get("MTF_ALIGNED_MODE")
    if mode != "required":
        proposals.append(("MTF_ALIGNED_MODE", "required", OBJECTIVE_LOSS_REDUCTION))
    else:
        proposals.append(("MTF_ALIGNED_MODE", "boost", OBJECTIVE_MORE_OPERATIONS))
        cnt = champion.get("MTF_ALIGNED_MIN_COUNT")
        if isinstance(cnt, int):
            proposals.append(("MTF_ALIGNED_MIN_COUNT", cnt + 1, OBJECTIVE_LOSS_REDUCTION))
            proposals.append(("MTF_ALIGNED_MIN_COUNT", cnt - 1, OBJECTIVE_MORE_OPERATIONS))
    prox = _finite(champion.get("PROXIMITY_MAX_ATR"))
    # A breakout lane consulta bias macro em tempo real; o histórico não permite
    # reconstruí-lo fielmente. Enquanto ela estiver ON, não se propõe mexer no
    # teto de proximity com um contrafactual incompleto.
    if prox is not None and not champion.get("BREAKOUT_LANE_ENABLED"):
        proposals.append(("PROXIMITY_MAX_ATR", round(prox - 0.25, 2), OBJECTIVE_LOSS_REDUCTION))
        proposals.append(("PROXIMITY_MAX_ATR", round(prox + 0.5, 2), OBJECTIVE_MORE_OPERATIONS))
    elif prox is not None:
        rejected.append({"knob": "PROXIMITY_MAX_ATR", "value": None,
                         "objective": None, "reason": "BREAKOUT_BIAS_NOT_RECONSTRUCTIBLE"})
    if not champion.get("STRUCT_CHASE_GATE_ENABLED"):
        proposals.append(("STRUCT_CHASE_GATE_ENABLED", True, OBJECTIVE_LOSS_REDUCTION))
    else:
        sc = _finite(champion.get("STRUCT_CHASE_MAX_ATR"))
        if sc is not None:
            proposals.append(("STRUCT_CHASE_MAX_ATR", round(sc + 2, 2), OBJECTIVE_MORE_OPERATIONS))

    for knob, value, objective in proposals:
        if len(accepted) >= max(1, min(P05_MAX_CANDIDATES, 12)):
            break
        comp = _KNOB_COMPONENT[knob]
        cov = component_coverage(rows, comp)
        if cov is None or cov < P05_MIN_FEATURE_COVERAGE_PCT:
            rejected.append({"knob": knob, "value": value, "objective": objective,
                             "reason": "MISSING_FEATURE_COVERAGE",
                             "coverage_pct": cov,
                             "required_pct": P05_MIN_FEATURE_COVERAGE_PCT})
            continue
        try:
            cfg = validate_candidate_config(champion, {knob: value})
        except CandidateValidationError as exc:
            rejected.append({"knob": knob, "value": value, "objective": objective,
                             "reason": "INVALID_CONFIG", "detail": str(exc)})
            continue
        accepted.append({"objective": objective, "knob": knob,
                         "champion_value": champion.get(knob),
                         "candidate_value": cfg[knob], "config": cfg,
                         "feature_coverage_pct": cov})
    return accepted, rejected


# ════════════════════════════════════════════════════════════════════════════
#  Leitura das três fontes (DB) — fail-soft por seção
# ════════════════════════════════════════════════════════════════════════════
async def _load_real(days: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """REAL: `RealTrade(source="auto")` FECHADO, janela por `closed_at`."""
    from db import get_session
    from models.real_trade import RealTrade
    from sqlalchemy import select
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(RealTrade)
            .where(RealTrade.source == "auto")
            .where(RealTrade.closed_at.is_not(None))
            .where(RealTrade.closed_at >= since)
        )).scalars().all()
        for t in rows:
            # Identidade econômica antes do id interno: retry/import duplicado
            # da mesma ordem ou recommendation não pode inflar o REAL.
            if t.exchange_order_id:
                dedupe = f"real-order:{t.exchange or 'unknown'}:{t.exchange_order_id}"
            elif t.client_order_id:
                dedupe = f"real-client:{t.exchange or 'unknown'}:{t.client_order_id}"
            elif t.recommendation_id:
                dedupe = f"real-rec:{t.recommendation_id}"
            else:
                dedupe = f"real:{t.id}"
            raw.append({
                "dedupe_key": dedupe,
                "is_open": (t.status or "") == "open" or t.closed_at is None,
                "realized_r": t.realized_r,
                "status": t.status,
                "resolved_at": t.closed_at,
                "symbol": t.symbol,
                "direction": t.side,
                "timeframe": None,
                "tier": None,
                "score": None,
                "pnl_usd": t.pnl_usd,
                "entry_fee": t.entry_fee,
                "exit_fee": t.exit_fee,
                "entry_slippage_pct": t.entry_slippage_pct,
                "tp1_hit": (t.status in REAL_TP1_HIT) if t.status else None,
                "tp2_hit": (t.status in REAL_TP2_HIT) if t.status else None,
                "recommendation_id": t.recommendation_id,
                "features": {},
            })
    valid, dq = normalize_outcomes(raw, source="REAL")
    linked = sum(1 for r in valid if r.get("recommendation_id"))
    dq["snapshot_link_coverage_pct"] = round(linked / len(valid) * 100, 1) if valid else None
    return valid, dq


async def _load_shadow(days: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """SHADOW: snapshots RESOLVIDOS, `_not_fast_void`, janela por `outcome_at`."""
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot as RS
    from services import calibration_service as _calib
    from sqlalchemy import select
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(RS)
            .where(RS.status.in_(SNAP_RESOLVED))
            .where(RS.outcome_at.is_not(None))
            .where(RS.outcome_at >= since)
            .where(_calib._not_fast_void())
        )).scalars().all()
        for s in rows:
            raw.append({
                "dedupe_key": f"snap:{s.id}",
                "is_open": (s.status or "") == "open",
                "realized_r": s.realized_r,
                "status": s.status,
                "resolved_at": s.outcome_at,
                "created_at": s.created_at,
                "symbol": s.symbol,
                "timeframe": s.timeframe,
                "tier": s.tier,
                "direction": s.direction,
                "score": s.score,
                "risk_reward": s.risk_reward,
                "tp1_hit": s.tp1_hit_at is not None or (s.status in SNAP_WIN),
                "tp2_hit": s.status in SNAP_TP2,
                "features": _merged_features(s.features),
            })
    valid, dq = normalize_outcomes(raw, source="SHADOW")
    return valid, dq


def _merged_features(features: Any) -> Dict[str, Any]:
    """Achata `features` + `features['p05_context']` numa view de leitura.
    O namespace versionado tem precedência quando traz o valor original."""
    base = dict(features or {}) if isinstance(features, dict) else {}
    ctx = base.get("p05_context")
    if isinstance(ctx, dict):
        for k, v in ctx.items():
            if v is not None:
                base[k] = v
    return base


async def _load_backtest(days: int, limit: int = 20000) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """BACKTEST: evidência histórica SECUNDÁRIA — nunca somada ao REAL."""
    from db import get_session
    from models.backtest_trade import BacktestTrade as BT
    from sqlalchemy import select
    since = datetime.now(timezone.utc) - timedelta(days=days)
    raw: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(BT).where(BT.bar_ts.is_not(None)).where(BT.bar_ts >= since)
            .order_by(BT.bar_ts.desc()).limit(limit)
        )).scalars().all()
        for b in rows:
            raw.append({
                "dedupe_key": f"bt:{b.id}",
                "is_open": False,
                "realized_r": b.realized_r,
                "status": b.status,
                "resolved_at": b.bar_ts,
                "symbol": b.symbol,
                "timeframe": b.timeframe,
                "tier": b.tier,
                "direction": b.direction,
                "score": b.score,
                "tp1_hit": b.status in SNAP_WIN,
                "tp2_hit": b.status in SNAP_TP2,
                "features": {"atr_pct": b.atr_pct, "hour_utc": b.hour_utc,
                             "day_of_week": b.dow, "patterns": b.patterns},
            })
    return normalize_outcomes(raw, source="BACKTEST")


async def _load_gate_events(days: int) -> Dict[str, Any]:
    """Eventos de bloqueio por gate (`SkipReasonStat`).

    HONESTIDADE: são EVENTOS agregados por (gate, dia), **não** oportunidades
    únicas. Não é possível somar com executados para obter "candidatos únicos"
    sem uma identidade única provável — e ela não existe nesta tabela.
    """
    from db import get_session
    from models.skip_reason_stat import SkipReasonStat as S
    from sqlalchemy import select, func
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out: Dict[str, Any] = {
        "window_days": days, "total_events": 0, "items": [], "by_phase": {},
        "note": ("contadores de EVENTOS por gate/dia — não são oportunidades únicas; "
                 "não somar com executados"),
    }
    async with get_session() as session:
        grouped = list((await session.execute(
            select(S.gate, func.sum(S.count), func.max(S.last_seen))
            .where(S.day >= since).group_by(S.gate)
            .order_by(func.sum(S.count).desc())
        )).all())
        recent = list((await session.execute(
            select(S).where(S.day >= since).order_by(S.last_seen.desc())
        )).scalars().all())
        by_day = list((await session.execute(
            select(S.day, func.sum(S.count)).where(S.day >= since)
            .group_by(S.day).order_by(S.day)
        )).all())

    last_by_gate: Dict[str, Any] = {}
    for row in recent:
        last_by_gate.setdefault(row.gate, row)

    total = 0
    items = []
    for gate, cnt, last_seen in grouped:
        c = int(cnt or 0)
        total += c
        ex = last_by_gate.get(gate)
        items.append({
            "gate": gate, "count": c,
            "last_reason": ex.last_reason if ex else None,
            "last_symbol": ex.last_symbol if ex else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })
    for it in items:
        it["share_pct"] = round(it["count"] / total * 100, 1) if total else None
    out["items"] = items
    out["total_events"] = total
    out["trend_by_day"] = [{"day": d.isoformat(), "count": int(c or 0)} for d, c in by_day]

    # P04A/P04B abortam DENTRO do POST (não passam por _record_skip) → não têm
    # contador nesta tabela. Reportar isso, não inventar número.
    p04c = sum(it["count"] for it in items if it["gate"] == "data-freshness")
    out["by_phase"] = {
        "P04A_entry_revalidation": {"events": None,
                                    "reason": "aborta no POST; não é contabilizado em skip_reason_stats"},
        "P04B_depth_vwap": {"events": None,
                            "reason": "aborta no POST; não é contabilizado em skip_reason_stats"},
        "P04C_data_freshness": {"events": p04c, "gate": "data-freshness"},
        "outros_gates": {"events": total - p04c},
    }
    out["disclaimer"] = ("bloqueio não é 'lucro perdido': sem outcome contrafactual "
                         "ligado ao MESMO setup não há como afirmar custo")
    return out


# ════════════════════════════════════════════════════════════════════════════
#  P05A — diagnóstico
# ════════════════════════════════════════════════════════════════════════════
_FEATURE_NAMES = ("edge_score", "mtf_aligned", "chase_atr", "struct_chase_atr",
                  "atr_pct", "regime", "funding_sentiment", "entry_zone_type",
                  "patterns", "hour_utc")


# ════════════════════════════════════════════════════════════════════════════
#  P05.1T — telemetria prospectiva (observação pura, nunca decide)
# ════════════════════════════════════════════════════════════════════════════
P051T_MIN_OBSERVED = 30
P051T_MIN_COVERAGE_PCT = 80.0
P051T_CONTEXT_WINDOW_DAYS = 120

TELEMETRY_UNAVAILABLE = "UNAVAILABLE"
TELEMETRY_COLLECTING = "COLLECTING"
TELEMETRY_USABLE = "USABLE"

_PATH_LIMITATIONS = [
    "MAE/MFE é do caminho do SETUP SHADOW — não é execução REAL",
    "fonte é OHLCV de 5 minutos; não é trajetória tick a tick",
    "ordem intravela é desconhecida (high/low sem sequência)",
    "não representa slippage nem a trajetória após o fill real",
    "não deve ser atribuído ao RealTrade como se fosse execução real",
    "não gera 'stop ideal' nem 'TP ideal' — é observação, não recomendação",
]


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Percentil por interpolação linear. `None` em amostra vazia."""
    vals = sorted(v for v in (_finite(x) for x in values) if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 4)
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(vals[lo], 4)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 4)


def _distribution(values: Sequence[float]) -> Dict[str, Any]:
    vals = [v for v in (_finite(x) for x in values) if v is not None]
    if not vals:
        return {"mean": None, "median": None, "p75": None, "p90": None, "count": 0}
    return {
        "mean": round(sum(vals) / len(vals), 4),
        "median": _percentile(vals, 0.50),
        "p75": _percentile(vals, 0.75),
        "p90": _percentile(vals, 0.90),
        "count": len(vals),
    }


def summarize_path_telemetry(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Diagnóstico MAE/MFE a partir SOMENTE de `features["p05_path"]`.

    Nunca reconstrói história por aproximação: snapshot sem telemetria conta como
    `missing`, com motivo. `USABLE` exige ≥30 observados E cobertura ≥80%.
    """
    eligible = len(rows)
    observed = 0
    missing = 0
    by_reason: Dict[str, int] = {}
    mae_values: List[float] = []
    mfe_values: List[float] = []

    for row in rows:
        feats = row.get("features") or {}
        path = feats.get("p05_path")
        if not isinstance(path, dict):
            missing += 1
            by_reason["sem_p05_path (snapshot anterior à telemetria)"] = (
                by_reason.get("sem_p05_path (snapshot anterior à telemetria)", 0) + 1)
            continue
        status = path.get("status")
        mae = _finite(path.get("mae_r"))
        mfe = _finite(path.get("mfe_r"))
        if mae is None or mfe is None:
            missing += 1
            reason = path.get("unavailable_reason") or f"sem MAE/MFE (status={status})"
            by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
            continue
        observed += 1
        mae_values.append(mae)
        mfe_values.append(mfe)

    coverage = round(observed / eligible * 100, 1) if eligible else None
    if observed == 0:
        status = TELEMETRY_UNAVAILABLE
    elif observed >= P051T_MIN_OBSERVED and (coverage or 0) >= P051T_MIN_COVERAGE_PCT:
        status = TELEMETRY_USABLE
    else:
        status = TELEMETRY_COLLECTING

    return {
        "source": "SHADOW_SETUP_PATH_5M",
        "status": status,
        "eligible_resolved": eligible,
        "observed": observed,
        "coverage_pct": coverage,
        "missing": missing,
        "unavailable_by_reason": by_reason,
        "min_observed": P051T_MIN_OBSERVED,
        "min_coverage_pct": P051T_MIN_COVERAGE_PCT,
        "mae_r": _distribution(mae_values),
        "mfe_r": _distribution(mfe_values),
        "limitations": _PATH_LIMITATIONS,
    }


def summarize_context_coverage(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Cobertura dos eixos do P05.1 no MESMO corte cronológico da avaliação.

    Reutiliza `temporal_split` e `axis_coverage` — não cria outro split. NÃO lê
    `realized_r` e NÃO abre holdout: só conta presença de feature.
    A cobertura GLOBAL nunca substitui a de TREINO (é ela que decide).
    """
    ordered = sorted(rows, key=lambda r: r["resolved_at"])
    split = temporal_split(ordered)
    if split.get("ok"):
        train, validation, test = split["train"], split["validation"], split["test"]
    else:                      # amostra insuficiente para split — reporta global
        train = validation = test = []

    out: Dict[str, Any] = {
        "min_required_pct": P05_MIN_FEATURE_COVERAGE_PCT,
        "split_available": bool(split.get("ok")),
        "axes": {},
        "note": ("a cobertura de TREINO é a que decide (mesmo corte da geração "
                 "P05.1); a global não a substitui"),
    }
    for axis in P051_CONTEXT_AXES:
        present = sum(1 for r in ordered if context_value(r, axis) != CONTEXT_UNKNOWN)
        global_cov = axis_coverage(ordered, axis)
        train_cov = axis_coverage(train, axis) if train else None
        usable = (train_cov is not None and train_cov >= P05_MIN_FEATURE_COVERAGE_PCT)
        out["axes"][axis] = {
            "coverage_global_pct": global_cov,
            "coverage_train_pct": train_cov,
            "coverage_validation_pct": axis_coverage(validation, axis) if validation else None,
            "coverage_test_pct": axis_coverage(test, axis) if test else None,
            "present": present,
            "missing": len(ordered) - present,
            "status": TELEMETRY_USABLE if usable else TELEMETRY_COLLECTING,
        }
    return out


def summarize_slippage(real_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Auditoria do slippage JÁ existente (`RealTrade.entry_slippage_pct`).

    Não recalcula, não altera `RealTrade` e nunca preenche ausência com zero —
    zero legítimo continua valendo como observação válida.
    """
    total = len(real_rows)
    values: List[float] = []
    missing = 0
    invalid_by_reason: Dict[str, int] = {}
    for row in real_rows:
        raw = row.get("entry_slippage_pct")
        if raw is None:
            missing += 1
            continue
        val = _finite(raw)
        if val is None:
            invalid_by_reason["NaN/infinito"] = invalid_by_reason.get("NaN/infinito", 0) + 1
            continue
        values.append(val)                     # zero legítimo É válido
    return {
        "total_real_closed": total,
        "slippage_valid": len(values),
        "slippage_missing": missing,
        "coverage_pct": round(len(values) / total * 100, 1) if total else None,
        "invalid_excluded_by_reason": invalid_by_reason,
        **_distribution(values),
        "note": "reutiliza RealTrade.entry_slippage_pct; ausência nunca vira zero",
    }


P052L_EXEC_KEY = "p05_execution_path"

P052L_LIMITATIONS = [
    "descreve o CAMINHO TÉCNICO da entrada; não prova causa de ganho ou stop",
    "duração da chamada inclui preflight e processamento do helper — não é "
    "'tempo de fill'",
    "sem backfill: só entradas posteriores ao P05.2L têm trace",
    "fill não auditável permanece UNKNOWN; ausência nunca vira zero",
]


def summarize_latency(rows: Sequence[Dict[str, Any]] = (),
                      real_rows: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """P05.2L — latência do caminho de entrada LIVE, a partir dos traces REAIS.

    Lê SOMENTE `features["p05_execution_path"]` já persistido. Sem backfill, sem
    reconstrução por aproximação e sem correlacionar latência com lucro ou stop.
    Slippage NÃO é recalculado aqui: a fonte única continua sendo
    `RealTrade.entry_slippage_pct` (bloco `slippage` da mesma resposta).
    """
    traces: List[Dict[str, Any]] = []
    missing_by_reason: Dict[str, int] = {}
    trace_snapshot_ids: set = set()

    def _miss(reason: str) -> None:
        missing_by_reason[reason] = missing_by_reason.get(reason, 0) + 1

    for row in rows or ():
        path = (row.get("features") or {}).get(P052L_EXEC_KEY)
        if not isinstance(path, dict):
            continue                      # sem tentativa LIVE registrada
        if path.get("schema_version") != 1 or path.get("status") not in (
                "NOT_SUBMITTED", "NO_FILL", "SUBMISSION_UNKNOWN", "FILL_CONFIRMED",
                "OPEN_PERSISTED", "PERSISTENCE_FAILED", "UNAVAILABLE"):
            _miss("trace inválido ou de schema desconhecido")
            continue
        traces.append(path)
        if row.get("id") is not None:
            trace_snapshot_ids.add(row["id"])

    observed = len(traces)
    if observed == 0:
        return {
            "status": TELEMETRY_UNAVAILABLE,
            "reason": ("nenhuma entrada LIVE com trace de latência na janela "
                       "(sem backfill: só entradas posteriores ao P05.2L)"),
            "attempts_observed": 0,
            "by_status": {}, "by_route": {}, "by_quality": {},
            "coverage_pct": None,
            "missing_by_reason": missing_by_reason,
            "decision_to_attempt_ms": _distribution([]),
            "attempt_roundtrip_ms": _distribution([]),
            "attempt_to_persist_ms": _distribution([]),
            "end_to_end_ms": _distribution([]),
            "fill_auditable": 0,
            "without_exchange_timestamp": 0,
            "linked_real_trades": 0,
            "min_observed": P051T_MIN_OBSERVED,
            "min_coverage_pct": P051T_MIN_COVERAGE_PCT,
            "slippage_source": ("RealTrade.entry_slippage_pct — ver o bloco "
                                "`slippage` desta mesma resposta"),
            "limitations": P052L_LIMITATIONS,
        }

    by_status: Dict[str, int] = {}
    by_route: Dict[str, int] = {}
    by_quality: Dict[str, int] = {}
    d2a: List[float] = []
    rt: List[float] = []
    a2p: List[float] = []
    e2e: List[float] = []
    fill_auditable = 0
    no_exchange_ts = 0
    complete_or_usable = 0

    for path in traces:
        st = str(path.get("status"))
        by_status[st] = by_status.get(st, 0) + 1
        route = str(path.get("route") or "unknown")
        by_route[route] = by_route.get(route, 0) + 1
        quality = str(path.get("quality") or "UNAVAILABLE")
        by_quality[quality] = by_quality.get(quality, 0) + 1
        if quality == "COMPLETE":
            complete_or_usable += 1
        for reason in (path.get("missing_fields") or []):
            _miss(f"campo ausente: {reason}")
        for bucket, key in ((d2a, "decision_to_attempt_ms"),
                            (rt, "attempt_roundtrip_ms"),
                            (a2p, "attempt_to_persist_ms"),
                            (e2e, "end_to_end_ms")):
            val = _finite(path.get(key))
            if val is not None and val >= 0:
                bucket.append(val)          # ausência NUNCA vira zero
        if path.get("fill_confirmed") is True:
            fill_auditable += 1
        if not path.get("exchange_event_at"):
            no_exchange_ts += 1

    coverage = round(complete_or_usable / observed * 100, 1)
    if observed < P051T_MIN_OBSERVED or coverage < P051T_MIN_COVERAGE_PCT:
        status = TELEMETRY_COLLECTING
    else:
        status = TELEMETRY_USABLE

    linked = sum(1 for r in (real_rows or ())
                 if r.get("recommendation_id") is not None
                 and r.get("recommendation_id") in trace_snapshot_ids)

    return {
        "status": status,
        "attempts_observed": observed,
        "by_status": by_status,
        "by_route": by_route,
        "by_quality": by_quality,
        "coverage_pct": coverage,
        "missing_by_reason": missing_by_reason,
        "decision_to_attempt_ms": _distribution(d2a),
        "attempt_roundtrip_ms": _distribution(rt),
        "attempt_to_persist_ms": _distribution(a2p),
        "end_to_end_ms": _distribution(e2e),
        "fill_auditable": fill_auditable,
        "without_exchange_timestamp": no_exchange_ts,
        "linked_real_trades": linked,
        "min_observed": P051T_MIN_OBSERVED,
        "min_coverage_pct": P051T_MIN_COVERAGE_PCT,
        "slippage_source": ("RealTrade.entry_slippage_pct — ver o bloco "
                            "`slippage` desta mesma resposta"),
        "note": ("mede o caminho TÉCNICO da entrada; nenhuma correlação com "
                 "lucro ou stop é calculada nesta fase"),
        "limitations": P052L_LIMITATIONS,
    }


def summarize_gate_availability(gate_events: Dict[str, Any]) -> Dict[str, Any]:
    """Mapa HONESTO de disponibilidade dos contadores de gate.

    `SkipReasonStat` guarda EVENTOS agregados por (gate, dia) — podem repetir o
    mesmo setup e NÃO são oportunidades únicas. Nunca somar com executados como
    verdade absoluta, nunca estimar "lucro perdido".
    """
    items = (gate_events or {}).get("items") or []
    found = sorted({str(it.get("gate")) for it in items if it.get("gate")})
    phases = (gate_events or {}).get("by_phase") or {}
    availability: Dict[str, Any] = {}
    for phase, info in phases.items():
        events = (info or {}).get("events")
        availability[phase] = {
            "status": "AVAILABLE" if events is not None else "UNAVAILABLE",
            "events": events,
            "reason": (info or {}).get("reason"),
        }
    return {
        "gates_with_counters": found,
        "gates_without_counters": [p for p, a in availability.items()
                                   if a["status"] == "UNAVAILABLE"],
        "by_phase": availability,
        "semantics": ("são EVENTOS agregados por gate/dia; podem conter repetição; "
                      "não são oportunidades únicas e não somam com executados"),
        "no_hooks_added": True,
    }


async def build_telemetry_section(days: int) -> Dict[str, Any]:
    """Seção compacta de telemetria. Fail-soft por subseção; somente observação."""
    out: Dict[str, Any] = {
        "phase": "P05.1T",
        "read_only": True,
        "affects_strategy": False,
        "note": "telemetria é subordinada: nunca decide, nunca altera execução",
    }
    # Cobertura/contexto e retenção usam o MESMO loader selado do P05.1R:
    # colunas explícitas, sem realized_r, e janela fixa de 120 dias. Usar os
    # outcomes normalizados do diagnóstico (ou os 30 dias selecionados na UI)
    # produziria cobertura condicionada ao resultado e divergente do readiness.
    readiness_rows: Optional[List[Dict[str, Any]]] = None
    try:
        readiness_rows = await _load_readiness_rows(P051T_CONTEXT_WINDOW_DAYS)
        coverage = summarize_context_coverage(readiness_rows)
        coverage["window_days"] = P051T_CONTEXT_WINDOW_DAYS
        coverage["source"] = "P05.1R_SEALED_ROWS_WITHOUT_OUTCOME"
        out["context_coverage"] = coverage
    except Exception as exc:
        out["context_coverage"] = {"error": str(exc),
                                   "window_days": P051T_CONTEXT_WINDOW_DAYS}
    real_rows: List[Dict[str, Any]] = []
    try:
        real_rows, _dq = await _load_real(days)
        out["slippage"] = summarize_slippage(real_rows)
    except Exception as exc:
        out["slippage"] = {"error": str(exc)}
    # P05.2L — latência do caminho de entrada LIVE, dos traces já persistidos.
    try:
        if readiness_rows is None:
            readiness_rows = await _load_readiness_rows(P051T_CONTEXT_WINDOW_DAYS)
        out["latency"] = summarize_latency(readiness_rows, real_rows)
    except Exception as exc:
        log.warning(f"[p05.2l] resumo de latência falhou: {exc}")
        out["latency"] = {"status": TELEMETRY_UNAVAILABLE, "error": str(exc)}
    try:
        out["gate_availability"] = summarize_gate_availability(
            await _load_gate_events(min(days, 30)))
    except Exception as exc:
        out["gate_availability"] = {"error": str(exc)}
    try:
        if readiness_rows is None:
            readiness_rows = await _load_readiness_rows(P051T_CONTEXT_WINDOW_DAYS)
        out["retention"] = await _retention_snapshot(readiness_rows)
    except Exception as exc:
        out["retention"] = {"error": str(exc)}
    return out


# ════════════════════════════════════════════════════════════════════════════
#  P05.2A — diagnóstico LONGITUDINAL dos stop-losses (ANALYTICS ONLY)
#
#  Responde ONDE e EM QUAIS CONTEXTOS as recomendações acumulam stop, separando
#  volume de TAXA e verificando PERSISTÊNCIA (treino → validação). O teste final
#  de 25% permanece SELADO: nunca é materializado outcome nem feature dele.
#
#  Não otimiza parâmetro, não gera knob/config/threshold, não sugere ampliar
#  stop e não cria candidato executável.
# ════════════════════════════════════════════════════════════════════════════
P052A_WINDOW_DAYS = 120

STOP_PERSISTENT_ADVERSE = "PERSISTENT_ADVERSE"
STOP_MIXED = "MIXED"
STOP_SAMPLE_LIMITED = "SAMPLE_LIMITED"
STOP_LOW_COVERAGE = "LOW_COVERAGE"
STOP_NOT_ADVERSE = "NOT_ADVERSE"

P052A_MIN_TRAIN_N = 30
P052A_MIN_VALID_N = 20
P052A_MIN_TRAIN_STOPS = 10
P052A_MIN_VALID_STOPS = 5
P052A_MAX_PATTERNS = 8

# Classes de desfecho SHADOW (stop original é só `lost`).
OUTCOME_STOP = "STOP"
OUTCOME_PROTECTED_EXIT = "PROTECTED_EXIT"     # won_tp1_be — saída pós-TP1, NÃO é stop
OUTCOME_WIN = "WIN"
OUTCOME_EXPIRED = "EXPIRED"
OUTCOME_INCONSISTENT = "INCONSISTENT"

P052A_LIMITATIONS = [
    "correlação não é causalidade — contexto descreve, não explica",
    "SHADOW é o caminho do SETUP, não o fill real",
    "REAL pode ter amostra pequena; REAL e SHADOW nunca são somados",
    "p05_path só existe para snapshots posteriores ao P05.1T (sem backfill)",
    "candle de 5m não revela a ordem intravela",
    "bloquear um contexto adverso também removeria os wins daquele contexto",
    "o diagnóstico NÃO recomenda ampliar, encurtar ou mover o stop",
]


def classify_shadow_outcome(status: Any, realized_r: Any) -> str:
    """Identidade do desfecho SHADOW.

    `lost` = stop ORIGINAL confirmado antes do TP1. `won_tp1_be` é saída
    PROTETIVA pós-TP1 e NUNCA conta como stop. `expired` não é stop.
    Divergência entre status e sinal de R é `INCONSISTENT` (excluída e contada).
    `realized_r=None` nunca vira zero.
    """
    st = str(status or "").strip()
    r = _finite(realized_r)
    if r is None:
        return OUTCOME_INCONSISTENT
    if st == "lost":
        return OUTCOME_STOP if r < 0 else OUTCOME_INCONSISTENT
    if st == "won_tp1_be":
        return OUTCOME_PROTECTED_EXIT if r > 0 else OUTCOME_INCONSISTENT
    if st in ("won_tp1", "won_tp2"):
        return OUTCOME_WIN if r > 0 else OUTCOME_INCONSISTENT
    if st == "expired":
        return OUTCOME_EXPIRED if r == 0 else OUTCOME_INCONSISTENT
    return OUTCOME_INCONSISTENT


async def load_stop_shadow_split(days: int = P052A_WINDOW_DAYS) -> Dict[str, Any]:
    """Loader SELADO do P05.2A.

    1) conta e ordena as linhas elegíveis por `(outcome_at, id)` SEM selecionar
       outcome; 2) divide 50/25/25 cronologicamente; 3) materializa outcome e
       features SOMENTE para treino e validação; 4) do teste, devolve apenas
       contagem e limites temporais.
    """
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot as RS
    from services import calibration_service as _calib
    from sqlalchemy import select

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        # (1) Índice cronológico — nenhuma coluna de outcome é selecionada.
        index_rows = (await session.execute(
            select(RS.id, RS.outcome_at)
            .where(RS.status.in_(SNAP_RESOLVED))
            .where(RS.outcome_at.is_not(None))
            .where(RS.outcome_at >= since)
            .where(_calib._not_fast_void())
            .order_by(RS.outcome_at.asc(), RS.id.asc())
        )).all()

        ordered = [(r.id, _utc(r.outcome_at)) for r in index_rows if _utc(r.outcome_at)]
        n = len(ordered)
        i_train, i_valid = int(n * 0.5), int(n * 0.75)
        train_idx = ordered[:i_train]
        valid_idx = ordered[i_train:i_valid]
        test_idx = ordered[i_valid:]

        allowed_ids = {rid for rid, _ in train_idx} | {rid for rid, _ in valid_idx}
        boundary = valid_idx[-1][1] if valid_idx else (train_idx[-1][1] if train_idx else None)

        rows_by_id: Dict[Any, Dict[str, Any]] = {}
        if boundary is not None and allowed_ids:
            # (3) Outcome só até a borda da VALIDAÇÃO; ties são aparados pelo
            # conjunto exato de ids, então nenhuma linha de teste entra.
            detail = (await session.execute(
                select(RS.id, RS.symbol, RS.timeframe, RS.tier, RS.direction,
                       RS.score, RS.status, RS.realized_r, RS.features,
                       RS.stop_distance_pct, RS.created_at, RS.outcome_at)
                .where(RS.status.in_(SNAP_RESOLVED))
                .where(RS.outcome_at.is_not(None))
                .where(RS.outcome_at >= since)
                .where(RS.outcome_at <= boundary)
                .where(_calib._not_fast_void())
            )).all()
            for row in detail:
                if row.id not in allowed_ids:
                    continue                        # aparo de empate temporal
                rows_by_id[row.id] = {
                    "id": row.id, "dedupe_key": f"snap:{row.id}",
                    "symbol": row.symbol, "timeframe": row.timeframe, "tier": row.tier,
                    "direction": row.direction, "score": row.score,
                    "status": row.status, "realized_r": row.realized_r,
                    "features": _merged_features(row.features),
                    "stop_distance_pct": row.stop_distance_pct,
                    "created_at": _utc(row.created_at),
                    "resolved_at": _utc(row.outcome_at),
                }

    def _stage(idx) -> List[Dict[str, Any]]:
        return [rows_by_id[rid] for rid, _ in idx if rid in rows_by_id]

    oldest = ordered[0][1].isoformat() if ordered else None
    newest = ordered[-1][1].isoformat() if ordered else None
    observed_days = round((ordered[-1][1] - ordered[0][1]).total_seconds() / 86400.0, 1) if n > 1 else None
    return {
        "train": _stage(train_idx),
        "validation": _stage(valid_idx),
        "train_count": len(train_idx),
        "validation_count": len(valid_idx),
        "test_count": len(test_idx),
        "eligible_total": n,
        "requested_window_days": days,
        "observed_span_days": observed_days,
        "oldest_resolved_at": oldest,
        "newest_resolved_at": newest,
        "young_history": bool(observed_days is not None and observed_days < days),
        "test_bounds": {
            "first_resolved_at": test_idx[0][1].isoformat() if test_idx else None,
            "last_resolved_at": test_idx[-1][1].isoformat() if test_idx else None,
        },
        "holdout_status": HOLDOUT_SEALED,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
    }


def _partition_outcomes(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Separa linhas válidas das excluídas, contabilizando o motivo."""
    valid: List[Dict[str, Any]] = []
    excluded: Dict[str, int] = {}
    seen: set = set()

    def _drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for row in rows:
        key = row.get("dedupe_key")
        if key is not None:
            if key in seen:
                _drop("duplicata")
                continue
            seen.add(key)
        if row.get("resolved_at") is None:
            _drop("sem timestamp de resolução")
            continue
        if row.get("realized_r") is None:
            _drop("realized_r ausente")
            continue
        if _finite(row.get("realized_r")) is None:
            _drop("realized_r NaN/infinito")
            continue
        klass = classify_shadow_outcome(row.get("status"), row.get("realized_r"))
        if klass == OUTCOME_INCONSISTENT:
            _drop("status/R incoerente")
            continue
        item = dict(row)
        item["outcome_class"] = klass
        valid.append(item)
    return valid, excluded


def stop_stage_metrics(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Métricas gerais de UM estágio (treino ou validação)."""
    valid, excluded = _partition_outcomes(rows)
    n = len(valid)
    if n == 0:
        return {"total_resolved": 0, "stops": 0, "stop_rate_pct": None,
                "stop_rate_ci": None, "wins": 0, "protected_exits": 0, "expired": 0,
                "expectancy_r": None, "sum_r": None, "median_r": None,
                "profit_factor": None, "worst_stop_streak": 0,
                "time_to_stop_minutes": {"median": None, "p75": None, "p90": None},
                "context_coverage_pct": None, "context_coverage_basis": "regime",
                "reliability": RELIABILITY_INSUFFICIENT,
                "excluded_by_reason": excluded, "excluded_total": len(rows)}

    classes = [r["outcome_class"] for r in valid]
    stops = classes.count(OUTCOME_STOP)
    rs = [_finite(r["realized_r"]) for r in valid]
    rs = [r for r in rs if r is not None]
    ordered_r = sorted(rs)
    mid = len(ordered_r) // 2
    median_r = ordered_r[mid] if len(ordered_r) % 2 else (ordered_r[mid - 1] + ordered_r[mid]) / 2

    gains = sum(r for r in rs if r > 0)
    losses_abs = abs(sum(r for r in rs if r < 0))
    pf = round(gains / losses_abs, 3) if losses_abs > 0 else None

    worst = cur = 0
    for r in valid:
        if r["outcome_class"] == OUTCOME_STOP:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0

    minutes = []
    for r in valid:
        if r["outcome_class"] != OUTCOME_STOP:
            continue
        created, resolved = r.get("created_at"), r.get("resolved_at")
        if created and resolved:
            delta = (resolved - created).total_seconds() / 60.0
            if delta >= 0:
                minutes.append(delta)

    with_ctx = sum(1 for r in valid
                   if (r.get("features") or {}).get("regime") is not None)
    return {
        "total_resolved": n,
        "stops": stops,
        "stop_rate_pct": round(stops / n * 100, 2),
        "stop_rate_ci": wilson_interval(stops, n),
        "wins": classes.count(OUTCOME_WIN),
        "protected_exits": classes.count(OUTCOME_PROTECTED_EXIT),
        "expired": classes.count(OUTCOME_EXPIRED),
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
        "sum_r": round(sum(rs), 4) if rs else None,
        "median_r": round(median_r, 4) if rs else None,
        "profit_factor": pf,
        "worst_stop_streak": worst,
        "time_to_stop_minutes": {
            "median": _percentile(minutes, 0.50),
            "p75": _percentile(minutes, 0.75),
            "p90": _percentile(minutes, 0.90),
            "count": len(minutes),
        },
        "context_coverage_pct": round(with_ctx / n * 100, 1),
        "context_coverage_basis": "regime",
        "reliability": reliability_label(n),
        "excluded_by_reason": excluded,
        "excluded_total": len(rows) - n,
    }


_STOP_AXES = {
    "tier": lambda r: r.get("tier"),
    "timeframe": lambda r: r.get("timeframe"),
    "tier_timeframe": lambda r: (f"{r.get('tier')}·{r.get('timeframe')}"
                                 if r.get("tier") and r.get("timeframe") else None),
    "direction": lambda r: r.get("direction"),
    "base": lambda r: _base_of(r.get("symbol") or "") or None,
    "patterns": lambda r: (r.get("features") or {}).get("patterns"),
    "session_utc": lambda r: _session_utc((r.get("features") or {}).get("hour_utc")),
    "day_of_week": lambda r: (r.get("features") or {}).get("day_of_week"),
    "regime": lambda r: (r.get("features") or {}).get("regime"),
    "funding_sentiment": lambda r: (r.get("features") or {}).get("funding_sentiment"),
    "score_bin": lambda r: _score_bin(r.get("score")),
    "atr_band": lambda r: _atr_band((r.get("features") or {}).get("atr_pct")),
    "mtf_aligned": lambda r: (r.get("features") or {}).get("mtf_aligned"),
    "entry_zone_type": lambda r: (r.get("features") or {}).get("entry_zone_type"),
}


def stop_segments_for_axis(rows: Sequence[Dict[str, Any]], axis: str) -> Dict[str, Any]:
    """Segmentos de UM eixo, com taxa por EXPOSIÇÃO (nunca contagem absoluta).

    Eixos multi-rótulo (patterns) fazem ATRIBUIÇÃO: o mesmo trade aparece em mais
    de um segmento, então os segmentos NÃO somam ao total.
    """
    getter = _STOP_AXES[axis]
    valid, _exc = _partition_outcomes(rows)
    total = len(valid)
    baseline_stops = sum(1 for r in valid if r["outcome_class"] == OUTCOME_STOP)
    baseline_rate = (baseline_stops / total * 100) if total else None

    groups: Dict[str, List[Dict[str, Any]]] = {}
    missing = 0
    for row in valid:
        key = getter(row)
        if key is None or key == "":
            missing += 1
            continue
        if isinstance(key, (list, tuple, set)):
            for k in key:
                if k:
                    groups.setdefault(str(k), []).append(row)
        else:
            groups.setdefault(str(key), []).append(row)

    items = []
    for key, grp in groups.items():
        exposure = len(grp)
        stops = sum(1 for r in grp if r["outcome_class"] == OUTCOME_STOP)
        wins = sum(1 for r in grp if r["outcome_class"] == OUTCOME_WIN)
        rate = round(stops / exposure * 100, 2) if exposure else None
        rs = [_finite(r["realized_r"]) for r in grp]
        rs = [r for r in rs if r is not None]
        items.append({
            "key": key,
            "exposure": exposure,
            "stop_count": stops,
            "stop_rate_pct": rate,
            "stop_rate_ci": wilson_interval(stops, exposure),
            "stage_stop_rate_pct": round(baseline_rate, 2) if baseline_rate is not None else None,
            "stop_rate_lift_pp": (round(rate - baseline_rate, 2)
                                  if rate is not None and baseline_rate is not None else None),
            "share_of_all_stops_pct": (round(stops / baseline_stops * 100, 1)
                                       if baseline_stops else None),
            "wins_removed_if_blocked": wins,
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
            "sum_r": round(sum(rs), 4) if rs else None,
            "reliability": reliability_label(exposure),
        })
    items.sort(key=lambda it: (
        -it["stop_rate_lift_pp"] if it["stop_rate_lift_pp"] is not None else float("inf"),
        -it["exposure"], it["key"]))
    return {
        "axis": axis,
        "stage_stop_rate_pct": round(baseline_rate, 2) if baseline_rate is not None else None,
        "coverage_pct": round((total - missing) / total * 100, 1) if total else None,
        "missing": missing,
        "overlapping": axis == "patterns",
        "items": items,
        "note": ("taxa por EXPOSIÇÃO, não contagem absoluta; eixos sobrepostos são "
                 "atribuição, não causalidade, e não somam"),
    }


def classify_stop_segment(train_item: Optional[Dict[str, Any]],
                          valid_item: Optional[Dict[str, Any]],
                          *, train_coverage: Optional[float],
                          valid_coverage: Optional[float]) -> Dict[str, Any]:
    """Classifica a PERSISTÊNCIA de um contexto adverso (treino → validação)."""
    if train_item is None or valid_item is None:
        return {"classification": STOP_SAMPLE_LIMITED,
                "reason": "contexto ausente em um dos estágios"}
    if (train_coverage is None or train_coverage < P05_MIN_FEATURE_COVERAGE_PCT
            or valid_coverage is None or valid_coverage < P05_MIN_FEATURE_COVERAGE_PCT):
        return {"classification": STOP_LOW_COVERAGE,
                "reason": (f"cobertura do eixo abaixo de {P05_MIN_FEATURE_COVERAGE_PCT}% "
                           f"(treino={train_coverage}% validação={valid_coverage}%)")}
    if (train_item["exposure"] < P052A_MIN_TRAIN_N
            or valid_item["exposure"] < P052A_MIN_VALID_N
            or train_item["stop_count"] < P052A_MIN_TRAIN_STOPS
            or valid_item["stop_count"] < P052A_MIN_VALID_STOPS):
        return {"classification": STOP_SAMPLE_LIMITED,
                "reason": (f"amostra insuficiente (treino n={train_item['exposure']}/"
                           f"stops={train_item['stop_count']}, validação "
                           f"n={valid_item['exposure']}/stops={valid_item['stop_count']})")}

    t_above = (train_item["stop_rate_lift_pp"] or 0) > 0
    v_above = (valid_item["stop_rate_lift_pp"] or 0) > 0
    t_neg = (train_item["expectancy_r"] is not None and train_item["expectancy_r"] < 0)
    v_neg = (valid_item["expectancy_r"] is not None and valid_item["expectancy_r"] < 0)

    if t_above and v_above and t_neg and v_neg:
        return {"classification": STOP_PERSISTENT_ADVERSE,
                "reason": ("taxa de stop acima do baseline E expectancy negativa nos "
                           "DOIS estágios")}
    if not t_above and not v_above:
        return {"classification": STOP_NOT_ADVERSE,
                "reason": "taxa de stop não supera o baseline em nenhum estágio"}
    return {"classification": STOP_MIXED,
            "reason": ("efeito não persistiu: adverso em um estágio e não no outro "
                       f"(treino_acima={t_above}/neg={t_neg}, "
                       f"validação_acima={v_above}/neg={v_neg})")}


def _band(value: Optional[float], edges: Sequence[Tuple[float, float, str]]) -> Optional[str]:
    v = _finite(value)
    if v is None:
        return None
    for lo, hi, label in edges:
        if lo <= v < hi:
            return label
    return None


_TIME_BANDS = [(0, 30, "<30m"), (30, 120, "30m–2h"), (120, 360, "2h–6h"),
               (360, float("inf"), ">6h")]
_MFE_BANDS = [(0, 0.25, "<0.25R"), (0.25, 0.50, "0.25–0.50R"),
              (0.50, 1.00, "0.50–1.00R"), (1.00, float("inf"), ">=1.00R")]
_STOPDIST_BANDS = [(0, 1.0, "<1%"), (1.0, 2.0, "1–2%"), (2.0, 4.0, "2–4%"),
                   (4.0, float("inf"), ">=4%")]


def stop_trajectory(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Trajetória dos stops: tempo até o stop, MFE antes do stop e faixa de
    distância do stop.

    MFE vem SOMENTE de `features["p05_path"]` já persistido — sem backfill e sem
    recálculo de LONG/SHORT. Ausência nunca vira zero.

    As faixas são DESCRITIVAS. É proibido concluir "o stop deveria ser ampliado",
    "o stop ideal seria X" ou "essa perda teria sido evitada".
    """
    valid, _exc = _partition_outcomes(rows)
    stops = [r for r in valid if r["outcome_class"] == OUTCOME_STOP]
    total = len(stops)

    time_counts: Dict[str, int] = {}
    minutes: List[float] = []
    mfe_counts: Dict[str, int] = {}
    mfe_values: List[float] = []
    dist_counts: Dict[str, int] = {}
    dist_values: List[float] = []
    missing: Dict[str, int] = {}
    path_status: Dict[str, int] = {}

    def _miss(reason: str) -> None:
        missing[reason] = missing.get(reason, 0) + 1

    for row in stops:
        created, resolved = row.get("created_at"), row.get("resolved_at")
        if created and resolved:
            mins = (resolved - created).total_seconds() / 60.0
            if mins >= 0:
                minutes.append(mins)
                band = _band(mins, _TIME_BANDS)
                if band:
                    time_counts[band] = time_counts.get(band, 0) + 1
        else:
            _miss("sem timestamps para tempo até o stop")

        path = (row.get("features") or {}).get("p05_path")
        if not isinstance(path, dict):
            _miss("p05_path ausente (snapshot anterior ao P05.1T)")
        else:
            status = str(path.get("status") or "UNKNOWN")
            path_status[status] = path_status.get(status, 0) + 1
            mfe = _finite(path.get("mfe_r"))
            if mfe is None:
                _miss(f"p05_path sem MFE válido (status={status})")
            else:
                mfe_values.append(mfe)
                band = _band(mfe, _MFE_BANDS)
                if band:
                    mfe_counts[band] = mfe_counts.get(band, 0) + 1

        dist = _finite(row.get("stop_distance_pct"))
        if dist is None:
            _miss("stop_distance_pct ausente")
        else:
            dist_values.append(dist)
            band = _band(dist, _STOPDIST_BANDS)
            if band:
                dist_counts[band] = dist_counts.get(band, 0) + 1

    def _bands(counts: Dict[str, int], order: Sequence[Tuple[float, float, str]],
               observed: int) -> List[Dict[str, Any]]:
        return [{"band": label, "count": counts.get(label, 0),
                 "pct": round(counts.get(label, 0) / observed * 100, 1) if observed else None}
                for _lo, _hi, label in order]

    return {
        "total_stops": total,
        "time_to_stop": {
            "observed": len(minutes),
            "coverage_pct": round(len(minutes) / total * 100, 1) if total else None,
            "bands": _bands(time_counts, _TIME_BANDS, len(minutes)),
            "median_minutes": _percentile(minutes, 0.50),
            "p75_minutes": _percentile(minutes, 0.75),
            "p90_minutes": _percentile(minutes, 0.90),
        },
        "mfe_before_stop": {
            "observed": len(mfe_values),
            "coverage_pct": round(len(mfe_values) / total * 100, 1) if total else None,
            "bands": _bands(mfe_counts, _MFE_BANDS, len(mfe_values)),
            "median_r": _percentile(mfe_values, 0.50),
            "p75_r": _percentile(mfe_values, 0.75),
            "p90_r": _percentile(mfe_values, 0.90),
            "path_status_counts": path_status,
            "source": "features.p05_path (prospectivo, sem backfill)",
        },
        "stop_distance": {
            "observed": len(dist_values),
            "coverage_pct": round(len(dist_values) / total * 100, 1) if total else None,
            "bands": _bands(dist_counts, _STOPDIST_BANDS, len(dist_values)),
            "median_pct": _percentile(dist_values, 0.50),
            "p75_pct": _percentile(dist_values, 0.75),
            "p90_pct": _percentile(dist_values, 0.90),
        },
        "missing_by_reason": missing,
        "descriptive_only": True,
        "note": ("faixas DESCRITIVAS: MAE/MFE de uma operação perdida não autoriza "
                 "otimizar o stop; nenhuma conclusão de stop ideal é derivada aqui"),
    }


def build_stop_hypotheses(train_axes: Dict[str, Any], valid_axes: Dict[str, Any]
                          ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Hipóteses ANALÍTICAS (máx. 8), ordem determinística. Sem knob, sem config,
    sem threshold, sem BLOCK/SCORE_DELTA, sem ação executável."""
    persistent: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []

    for axis in sorted(train_axes):
        t_axis, v_axis = train_axes[axis], valid_axes.get(axis) or {}
        t_items = {it["key"]: it for it in (t_axis.get("items") or [])}
        v_items = {it["key"]: it for it in (v_axis.get("items") or [])}
        for key in sorted(t_items):
            t_it, v_it = t_items[key], v_items.get(key)
            verdict = classify_stop_segment(
                t_it, v_it,
                train_coverage=t_axis.get("coverage_pct"),
                valid_coverage=v_axis.get("coverage_pct"))
            entry = {
                "axis": axis,
                "value": key,
                "classification": verdict["classification"],
                "reason": verdict["reason"],
                "train": {
                    "exposure": t_it["exposure"], "stops": t_it["stop_count"],
                    "stop_rate_pct": t_it["stop_rate_pct"],
                    "stop_rate_ci": t_it["stop_rate_ci"],
                    "stage_stop_rate_pct": t_it["stage_stop_rate_pct"],
                    "stop_rate_lift_pp": t_it["stop_rate_lift_pp"],
                    "expectancy_r": t_it["expectancy_r"],
                    "wins_in_context": t_it["wins_removed_if_blocked"],
                    "reliability": t_it["reliability"],
                },
                "validation": ({
                    "exposure": v_it["exposure"], "stops": v_it["stop_count"],
                    "stop_rate_pct": v_it["stop_rate_pct"],
                    "stop_rate_ci": v_it["stop_rate_ci"],
                    "stage_stop_rate_pct": v_it["stage_stop_rate_pct"],
                    "stop_rate_lift_pp": v_it["stop_rate_lift_pp"],
                    "expectancy_r": v_it["expectancy_r"],
                    "wins_in_context": v_it["wins_removed_if_blocked"],
                    "reliability": v_it["reliability"],
                } if v_it else None),
                "axis_coverage_pct": {"train": t_axis.get("coverage_pct"),
                                      "validation": v_axis.get("coverage_pct")},
                "blocking_would_remove_wins": (
                    (t_it["wins_removed_if_blocked"]
                     + (v_it["wins_removed_if_blocked"] if v_it else 0))),
                "temporal_status": ("CONFIRMED_IN_VALIDATION"
                                    if verdict["classification"] == STOP_PERSISTENT_ADVERSE
                                    else "NOT_CONFIRMED"),
                "limitations": P052A_LIMITATIONS,
                "for_future_phase": "P05.2B",
                "executable": False,
            }
            if verdict["classification"] == STOP_PERSISTENT_ADVERSE:
                persistent.append(entry)
            elif verdict["classification"] in (STOP_MIXED, STOP_SAMPLE_LIMITED):
                others.append(entry)

    # Ordem determinística: maior lift de validação, depois exposição, depois nome.
    persistent.sort(key=lambda h: (-((h["validation"] or {}).get("stop_rate_lift_pp") or 0),
                                   -((h["validation"] or {}).get("exposure") or 0),
                                   h["axis"], h["value"]))
    others.sort(key=lambda h: (-(h["train"]["stop_rate_lift_pp"] or 0),
                               -h["train"]["exposure"], h["axis"], h["value"]))
    return persistent[:P052A_MAX_PATTERNS], others[:P052A_MAX_PATTERNS]


# ════════════════════════════════════════════════════════════════════════════
#  P05.2B — STOP HYPOTHESIS OFFLINE LAB (somente validação, sem execução)
#
#  Responde UMA pergunta contrafactual: "como ficariam os resultados históricos
#  observados se as recomendações de um contexto PERSISTENTEMENTE adverso não
#  fossem selecionadas?".
#
#  O laboratório NÃO promove, NÃO ativa, NÃO cria experimento, NÃO entra em
#  Shadow e NÃO abre o holdout final. O melhor status possível é
#  `VALIDATION_SUPPORTED`, que significa APENAS "sobreviveu à validação" — nunca
#  "aprovado". A revisão no teste selado fica para uma fase futura.
# ════════════════════════════════════════════════════════════════════════════
P052B_HYPOTHESIS_TYPE = "STOP_CONTEXT_BLOCK"
P052B_MAX_HYPOTHESES = 4
P052B_MIN_PRESERVED_PCT = 70.0

# Eixos permitidos: baixa cardinalidade, exclusivos e sem sobreposição.
P052B_ALLOWED_AXES = (
    "tier", "timeframe", "direction", "session_utc", "regime",
    "funding_sentiment", "score_bin", "atr_band", "mtf_aligned", "entry_zone_type",
)
# Bloqueados nesta fase: alta cardinalidade (base), multi-rótulo sobreposto
# (patterns) ou combinação/estilhaçamento com risco de overfitting.
P052B_BLOCKED_AXES = ("base", "patterns", "tier_timeframe", "day_of_week")

LAB_NO_ELIGIBLE = "NO_ELIGIBLE_HYPOTHESIS"
LAB_INSUFFICIENT = "INSUFFICIENT"
LAB_REJECTED = "REJECTED"
LAB_VALIDATION_SUPPORTED = "VALIDATION_SUPPORTED"
LAB_UNAVAILABLE = "UNAVAILABLE"

P052B_LIMITATIONS = [
    "validação apoiada NÃO é aprovação — o teste final continua selado",
    "contrafactual assume que remover o contexto não muda o resto do mercado",
    "bloquear um contexto adverso também remove os wins daquele contexto",
    "correlação não é causalidade: o contexto pode apenas rotular o regime",
    "SHADOW é o caminho do SETUP, não o fill real",
    "nenhuma hipótese é executável, promovível ou elegível a Shadow nesta fase",
]

# Vocabulário do núcleo P05 (`compute_evidence_metrics`) a partir da identidade
# de desfecho do P05.2A. `won_tp1_be` tem R>0 por construção, então entra como
# ganho — nunca como stop.
_STOP_TO_EVIDENCE_CLASS = {
    OUTCOME_STOP: "loss",
    OUTCOME_WIN: "win",
    OUTCOME_PROTECTED_EXIT: "win",
    OUTCOME_EXPIRED: "expired",
}


def _lab_metric_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Traduz uma linha do P05.2A para o vocabulário do motor de métricas P05."""
    r = _finite(row.get("realized_r"))
    klass = _STOP_TO_EVIDENCE_CLASS.get(row.get("outcome_class"))
    if r is None or klass is None:
        return None
    out = dict(row)
    out["realized_r"] = float(r)
    out["outcome_class"] = klass
    out["stop_class"] = row.get("outcome_class")
    return out


def bootstrap_paired_stop_rate_delta_ci(
    rows: Sequence[Dict[str, Any]], kept_ids: set,
    *, seed: int = None, samples: int = None,
) -> Optional[Dict[str, float]]:
    """IC 95% do delta de TAXA DE STOP (candidato − champion), pareado.

    A unidade reamostrada é a oportunidade; em cada réplica as duas taxas saem
    do MESMO índice, preservando a dependência (o candidato é subconjunto do
    champion). Mesma seed fixa do P05.
    """
    pairs = [(1 if r.get("outcome_class") == "loss" else 0, id(r) in kept_ids)
             for r in rows]
    if len(pairs) < 2 or not any(k for _s, k in pairs):
        return None
    rng = random.Random(P05_RANDOM_SEED if seed is None else seed)
    nboot = _bootstrap_samples() if samples is None else max(1, min(samples, _BOOTSTRAP_HARD_MAX))
    n = len(pairs)
    deltas: List[float] = []
    for _ in range(nboot):
        ch_s = ch_n = ca_s = ca_n = 0
        for _j in range(n):
            stop, kept = pairs[rng.randrange(n)]
            ch_s += stop
            ch_n += 1
            if kept:
                ca_s += stop
                ca_n += 1
        if ch_n and ca_n:
            deltas.append(ca_s / ca_n * 100.0 - ch_s / ch_n * 100.0)
    if len(deltas) < max(20, nboot // 2):
        return None
    deltas.sort()
    ch_all = sum(s for s, _k in pairs) / n * 100.0
    kept_pairs = [s for s, k in pairs if k]
    ca_all = sum(kept_pairs) / len(kept_pairs) * 100.0
    return {
        "low_pp": round(deltas[int(0.025 * (len(deltas) - 1))], 3),
        "high_pp": round(deltas[int(0.975 * (len(deltas) - 1))], 3),
        "point_pp": round(ca_all - ch_all, 3),
        "method": "paired-opportunity-bootstrap",
    }


def stop_block_comparison(rows: Sequence[Dict[str, Any]], axis: str,
                          value: Any) -> Dict[str, Any]:
    """Champion observado × candidato `STOP_CONTEXT_BLOCK` sobre as MESMAS
    oportunidades.

    Contexto ausente/UNKNOWN é excluído dos DOIS lados (comparação simétrica):
    ausência nunca vira "não pertence ao contexto".
    """
    if axis not in _STOP_AXES:
        raise KeyError(axis)
    getter = _STOP_AXES[axis]
    valid, _exc = _partition_outcomes(rows)

    evaluable: List[Dict[str, Any]] = []
    unknown_excluded = 0
    for row in valid:
        key = getter(row)
        if key is None or key == "" or isinstance(key, (list, tuple, set)):
            unknown_excluded += 1          # UNKNOWN nunca vira zero nem ausência
            continue
        metric = _lab_metric_row(row)
        if metric is None:
            unknown_excluded += 1
            continue
        metric["_axis_value"] = str(key)
        evaluable.append(metric)

    target = str(value)
    removed = [r for r in evaluable if r["_axis_value"] == target]
    kept = [r for r in evaluable if r["_axis_value"] != target]

    def _rate(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(group)
        stops = sum(1 for r in group if r["outcome_class"] == "loss")
        return {
            "exposure": n,
            "stops": stops,
            "stop_rate_pct": round(stops / n * 100, 2) if n else None,
            "stop_rate_ci": wilson_interval(stops, n),
        }

    champ_rate, cand_rate, rem_rate = _rate(evaluable), _rate(kept), _rate(removed)
    champ_m = compute_evidence_metrics(evaluable)
    cand_m = compute_evidence_metrics(kept)
    rem_m = compute_evidence_metrics(removed)

    kept_ids = {id(r) for r in kept}
    paired = bootstrap_paired_membership_delta_ci(
        evaluable, {id(r) for r in evaluable}, kept_ids)
    stop_delta = bootstrap_paired_stop_rate_delta_ci(evaluable, kept_ids)

    total_valid = len(valid)
    coverage = round(len(evaluable) / total_valid * 100, 1) if total_valid else None
    preserved = (round(len(kept) / len(evaluable) * 100, 2) if evaluable else None)

    return {
        "axis": axis,
        "value": target,
        "evaluable": len(evaluable),
        "unknown_excluded": unknown_excluded,
        "axis_coverage_pct": coverage,
        "champion": {**champ_rate,
                     "expectancy_r": champ_m["expectancy_r"],
                     "sum_r": champ_m["sum_r"],
                     "median_r": champ_m["median_r"],
                     "profit_factor": champ_m["profit_factor"],
                     "max_drawdown_r": champ_m["max_drawdown_r"],
                     "worst_loss_streak": champ_m["worst_loss_streak"],
                     "expectancy_ci": champ_m["expectancy_ci"],
                     "reliability": champ_m["reliability"]},
        "candidate": {**cand_rate,
                      "expectancy_r": cand_m["expectancy_r"],
                      "sum_r": cand_m["sum_r"],
                      "median_r": cand_m["median_r"],
                      "profit_factor": cand_m["profit_factor"],
                      "max_drawdown_r": cand_m["max_drawdown_r"],
                      "worst_loss_streak": cand_m["worst_loss_streak"],
                      "expectancy_ci": cand_m["expectancy_ci"],
                      "reliability": cand_m["reliability"]},
        "removed": {**rem_rate,
                    "expectancy_r": rem_m["expectancy_r"],
                    "sum_r": rem_m["sum_r"],
                    "expectancy_ci": rem_m["expectancy_ci"],
                    "reliability": rem_m["reliability"]},
        "operations_kept": len(kept),
        "operations_removed": len(removed),
        "operations_preserved_pct": preserved,
        "stops_avoided": rem_rate["stops"],
        "wins_removed": sum(1 for r in removed if r["stop_class"] == OUTCOME_WIN),
        "protected_exits_removed": sum(1 for r in removed
                                       if r["stop_class"] == OUTCOME_PROTECTED_EXIT),
        "expired_removed": sum(1 for r in removed if r["stop_class"] == OUTCOME_EXPIRED),
        "paired_expectancy_delta_ci": paired,
        "stop_rate_delta_ci": stop_delta,
        "stop_rate_delta_pp": (
            round(cand_rate["stop_rate_pct"] - champ_rate["stop_rate_pct"], 2)
            if cand_rate["stop_rate_pct"] is not None
            and champ_rate["stop_rate_pct"] is not None else None),
        "material_segment_regressions": _material_segment_regressions(evaluable, kept),
        "note": ("comparação PAREADA sobre as mesmas oportunidades; contexto "
                 "ausente é excluído dos dois lados"),
    }


def _pf_not_worse(candidate: Optional[float], champion: Optional[float]) -> bool:
    """Profit factor do candidato não pode ser pior. `None` = indefinido (sem
    perdas na amostra) e só é aceito quando o champion também é indefinido ou
    quando o candidato deixou de ter perdas."""
    if candidate is None:
        return True                       # candidato sem perdas: não é pior
    if champion is None:
        return False                      # champion sem perdas, candidato com
    return candidate >= champion - 1e-9


def _hypothesis_hash(axis: str, value: str) -> str:
    payload = json.dumps({"type": P052B_HYPOTHESIS_TYPE, "axis": axis,
                          "value": value}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def evaluate_stop_hypothesis(pattern: Dict[str, Any],
                             train_rows: Sequence[Dict[str, Any]],
                             valid_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Avalia UMA hipótese `STOP_CONTEXT_BLOCK`. Gate SOMENTE de validação.

    Nunca devolve `OFFLINE_VALIDATED`, `ELIGIBLE`, `SHADOW`, `PROMOTED` ou
    `ACTIVE`: o melhor status possível é `VALIDATION_SUPPORTED`.
    """
    axis = str(pattern.get("axis") or "")
    value = str(pattern.get("value") or "")
    origin = str(pattern.get("classification") or "")

    checks: List[Dict[str, Any]] = []
    insufficient_checks: set[str] = set()

    def _check(name: str, passed: bool, detail: str, *, soft: bool = False) -> bool:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed and soft:
            insufficient_checks.add(name)
        return bool(passed)

    ok_origin = _check("origem_persistent_adverse",
                       origin == STOP_PERSISTENT_ADVERSE,
                       f"classificação de origem = {origin or 'ausente'}")
    ok_axis = _check("eixo_permitido", axis in P052B_ALLOWED_AXES,
                     f"eixo {axis or 'ausente'} "
                     f"{'permitido' if axis in P052B_ALLOWED_AXES else 'bloqueado nesta fase'}")

    base = {
        "hash": _hypothesis_hash(axis, value),
        "type": P052B_HYPOTHESIS_TYPE,
        "axis": axis,
        "value": value,
        "source_evidence": {
            "classification": origin,
            "reason": pattern.get("reason"),
            "train": pattern.get("train"),
            "validation": pattern.get("validation"),
            "axis_coverage_pct": pattern.get("axis_coverage_pct"),
            "blocking_would_remove_wins": pattern.get("blocking_would_remove_wins"),
        },
        "executable": False,
        "promotable": False,
        "shadow_supported": False,
        "requires_future_holdout_review": True,
        "holdout_status": HOLDOUT_SEALED,
        "limitations": P052B_LIMITATIONS,
    }

    if not (ok_origin and ok_axis):
        return {**base, "status": LAB_REJECTED,
                "reason_code": ("ORIGIN_NOT_PERSISTENT_ADVERSE" if not ok_origin
                                else "AXIS_NOT_ALLOWED"),
                "detail": ("só padrão PERSISTENT_ADVERSE em eixo permitido gera "
                           "hipótese nesta fase"),
                "checks": checks, "train": None, "validation": None,
                "risks": ["hipótese não elegível: origem ou eixo fora do contrato"]}

    train = stop_block_comparison(train_rows, axis, value)
    validation = stop_block_comparison(valid_rows, axis, value)

    _check("cobertura_treino",
           (train["axis_coverage_pct"] or 0) >= P05_MIN_FEATURE_COVERAGE_PCT,
           f"cobertura do eixo no treino = {train['axis_coverage_pct']}% "
           f"(mín {P05_MIN_FEATURE_COVERAGE_PCT}%)", soft=True)
    _check("cobertura_validacao",
           (validation["axis_coverage_pct"] or 0) >= P05_MIN_FEATURE_COVERAGE_PCT,
           f"cobertura do eixo na validação = {validation['axis_coverage_pct']}% "
           f"(mín {P05_MIN_FEATURE_COVERAGE_PCT}%)", soft=True)
    _check("amostra_afetada_validacao",
           validation["operations_removed"] >= P051_MIN_AFFECTED,
           f"{validation['operations_removed']} operações afetadas na validação "
           f"(mín {P051_MIN_AFFECTED})", soft=True)

    preserved = validation["operations_preserved_pct"]
    _check("operacoes_preservadas",
           preserved is not None and preserved >= P052B_MIN_PRESERVED_PCT,
           f"preserva {preserved}% das operações do champion "
           f"(mín {P052B_MIN_PRESERVED_PCT}%)")

    cand, champ, rem = validation["candidate"], validation["champion"], validation["removed"]
    _check("expectancy_candidato_positiva",
           cand["expectancy_r"] is not None and cand["expectancy_r"] > 0,
           f"expectancy do candidato na validação = {cand['expectancy_r']}")
    _check("soma_r_candidato_positiva",
           cand["sum_r"] is not None and cand["sum_r"] > 0,
           f"soma R do candidato na validação = {cand['sum_r']}")
    _check("profit_factor_nao_pior",
           _pf_not_worse(cand["profit_factor"], champ["profit_factor"]),
           f"PF candidato={cand['profit_factor']} vs champion={champ['profit_factor']}")
    _check("drawdown_nao_pior",
           (cand["max_drawdown_r"] is not None and champ["max_drawdown_r"] is not None
            and cand["max_drawdown_r"] <= champ["max_drawdown_r"] + 1e-9),
           f"drawdown candidato={cand['max_drawdown_r']} vs "
           f"champion={champ['max_drawdown_r']}")
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
           f"stop rate candidato={cand['stop_rate_pct']}% vs "
           f"champion={champ['stop_rate_pct']}%")

    sr_ci = validation.get("stop_rate_delta_ci")
    _check("ic_superior_delta_stop_negativo",
           bool(sr_ci) and sr_ci.get("high_pp") is not None and sr_ci["high_pp"] < 0,
           f"IC 95% do delta de stop rate = {sr_ci}", soft=True)

    regressions = validation.get("material_segment_regressions") or []
    _check("sem_regressao_material", not regressions,
           f"{len(regressions)} segmento(s) confiável(is) com regressão material")

    failed = [c["name"] for c in checks if not c["passed"]]
    substantive_failed = [name for name in failed if name not in insufficient_checks]
    coverage_missing = any(name in failed for name in (
        "cobertura_treino", "cobertura_validacao"))
    if not failed:
        status, reason_code = LAB_VALIDATION_SUPPORTED, "ALL_VALIDATION_CHECKS_PASSED"
        detail = ("sobreviveu a todos os checks de validação — NÃO é aprovação; "
                  "o teste final continua selado")
    elif coverage_missing or not substantive_failed:
        status, reason_code = LAB_INSUFFICIENT, "INSUFFICIENT_EVIDENCE"
        detail = f"evidência insuficiente para julgar: {', '.join(failed)}"
    else:
        status, reason_code = LAB_REJECTED, "VALIDATION_CHECK_FAILED"
        detail = f"reprovada na validação: {', '.join(failed)}"

    risks = [
        f"bloquear este contexto removeria {validation['wins_removed']} operações "
        f"vencedoras na validação",
        f"removeria {validation['operations_removed']} de {validation['evaluable']} "
        f"oportunidades avaliáveis da validação",
    ]
    if validation["protected_exits_removed"]:
        risks.append(f"{validation['protected_exits_removed']} saídas protetivas "
                     f"pós-TP1 também seriam removidas")
    if regressions:
        risks.append(f"{len(regressions)} segmento(s) pioraria(m) materialmente")

    return {**base, "status": status, "reason_code": reason_code, "detail": detail,
            "train": train, "validation": validation, "checks": checks,
            "wins_removed": validation["wins_removed"],
            "stops_avoided": validation["stops_avoided"],
            "operations_preserved_pct": validation["operations_preserved_pct"],
            "risks": risks}


def build_stop_offline_lab(train_rows: Sequence[Dict[str, Any]],
                           valid_rows: Sequence[Dict[str, Any]],
                           persistent_patterns: Sequence[Dict[str, Any]]
                           ) -> Dict[str, Any]:
    """P05.2B — laboratório offline.

    Recebe SOMENTE treino e validação. Nenhuma linha, id, status, `realized_r`
    ou métrica do teste chega até aqui; o holdout não é aberto nem para um
    finalista.
    """
    out: Dict[str, Any] = {
        "phase": "P05.2B",
        "execution_mode": "ANALYTICS_ONLY",
        "read_only": True,
        "executable": False,
        "promotable": False,
        "shadow_supported": False,
        "holdout_status": HOLDOUT_SEALED,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "requires_future_holdout_review": True,
        "hypothesis_type": P052B_HYPOTHESIS_TYPE,
        "max_hypotheses": P052B_MAX_HYPOTHESES,
        "allowed_axes": list(P052B_ALLOWED_AXES),
        "blocked_axes": list(P052B_BLOCKED_AXES),
        "limitations": P052B_LIMITATIONS,
    }

    eligible = [p for p in (persistent_patterns or [])
                if str(p.get("classification")) == STOP_PERSISTENT_ADVERSE
                and str(p.get("axis")) in P052B_ALLOWED_AXES]
    # Força determinística: maior lift de stop na VALIDAÇÃO, depois exposição.
    eligible.sort(key=lambda p: (-((p.get("validation") or {}).get("stop_rate_lift_pp") or 0),
                                 -((p.get("validation") or {}).get("exposure") or 0),
                                 str(p.get("axis")), str(p.get("value"))))
    selected = eligible[:P052B_MAX_HYPOTHESES]

    out["source_patterns"] = [
        {"axis": p.get("axis"), "value": p.get("value"),
         "classification": p.get("classification"),
         "validation_stop_rate_lift_pp": (p.get("validation") or {}).get("stop_rate_lift_pp")}
        for p in selected
    ]
    out["source_patterns_available"] = len(persistent_patterns or [])
    out["source_patterns_blocked_axis"] = [
        {"axis": p.get("axis"), "value": p.get("value"), "reason": "AXIS_NOT_ALLOWED"}
        for p in (persistent_patterns or [])
        if str(p.get("classification")) == STOP_PERSISTENT_ADVERSE
        and str(p.get("axis")) not in P052B_ALLOWED_AXES
    ]

    if not selected:
        out.update({
            "status": LAB_NO_ELIGIBLE,
            "reason_code": "NO_PERSISTENT_ADVERSE_PATTERN",
            "detail": ("ainda não há padrão de stop persistente elegível: nenhum "
                       "contexto de eixo permitido se manteve adverso em treino E "
                       "validação com amostra suficiente"),
            "candidates": [],
            "rejected": [],
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        return out

    evaluated = [evaluate_stop_hypothesis(p, train_rows, valid_rows) for p in selected]
    supported = [h for h in evaluated if h["status"] == LAB_VALIDATION_SUPPORTED]
    rejected = [h for h in evaluated if h["status"] != LAB_VALIDATION_SUPPORTED]

    if supported:
        status, reason_code = LAB_VALIDATION_SUPPORTED, "ALL_VALIDATION_CHECKS_PASSED"
        detail = (f"{len(supported)} hipótese(s) sobreviveu(ram) à validação — "
                  "validação apoiada NÃO significa aprovação e o teste final "
                  "continua selado")
    elif all(h["status"] == LAB_INSUFFICIENT for h in evaluated):
        status, reason_code = LAB_INSUFFICIENT, "INSUFFICIENT_EVIDENCE"
        detail = "amostra/cobertura ainda insuficiente para julgar as hipóteses"
    else:
        status, reason_code = LAB_REJECTED, "VALIDATION_CHECK_FAILED"
        detail = "nenhuma hipótese passou em todos os checks de validação"

    out.update({
        "status": status,
        "reason_code": reason_code,
        "detail": detail,
        "candidates": supported,
        "rejected": rejected,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })
    return out


async def build_stop_diagnosis(days: int = P052A_WINDOW_DAYS) -> Dict[str, Any]:
    """P05.2A — orquestra o diagnóstico. Fail-soft por seção; ZERO escrita."""
    out: Dict[str, Any] = {
        "phase": "P05.2A",
        "execution_mode": "ANALYTICS_ONLY",
        "read_only": True,
        "requested_window_days": days,
        "holdout_status": HOLDOUT_SEALED,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "limitations": P052A_LIMITATIONS,
        "note": ("diagnóstico observacional: nenhuma alteração foi aplicada à "
                 "estratégia e nenhuma hipótese foi ativada"),
    }
    try:
        split = await load_stop_shadow_split(days)
    except Exception as exc:
        log.warning(f"[p05.2a] loader selado falhou: {exc}")
        return {**out, "error": str(exc)}

    out["sample"] = {k: split[k] for k in (
        "train_count", "validation_count", "test_count", "eligible_total",
        "observed_span_days", "oldest_resolved_at", "newest_resolved_at",
        "young_history", "test_bounds")}
    out["holdout_status"] = split["holdout_status"]

    train, validation = split["train"], split["validation"]
    try:
        out["shadow"] = {
            "train": stop_stage_metrics(train),
            "validation": stop_stage_metrics(validation),
            "source": "RecommendationSnapshot (SHADOW)",
            "stop_definition": ("status=='lost' com R<0; won_tp1_be é saída "
                                "PROTETIVA pós-TP1 e NÃO é stop; expired não é stop"),
        }
    except Exception as exc:
        out["shadow"] = {"error": str(exc)}

    train_axes: Dict[str, Any] = {}
    valid_axes: Dict[str, Any] = {}
    try:
        for axis in _STOP_AXES:
            train_axes[axis] = stop_segments_for_axis(train, axis)
            valid_axes[axis] = stop_segments_for_axis(validation, axis)
        out["segments"] = {"train": train_axes, "validation": valid_axes}
    except Exception as exc:
        log.warning(f"[p05.2a] segmentos falharam: {exc}")
        out["segments"] = {"error": str(exc)}

    try:
        persistent, others = build_stop_hypotheses(train_axes, valid_axes)
        out["persistent_patterns"] = persistent
        out["non_persistent_patterns"] = others
        if not persistent:
            out["patterns_verdict"] = "NO_PERSISTENT_STOP_PATTERN"
            out["patterns_verdict_reason"] = (
                "nenhum contexto manteve taxa de stop acima do baseline E expectancy "
                "negativa em treino E validação com amostra suficiente")
        else:
            out["patterns_verdict"] = "PERSISTENT_PATTERNS_FOUND"
    except Exception as exc:
        log.warning(f"[p05.2a] hipóteses falharam: {exc}")
        out["persistent_patterns"] = []
        out["non_persistent_patterns"] = []
        out["patterns_verdict"] = "UNAVAILABLE"
        out["patterns_verdict_reason"] = (
            "não foi possível concluir se há padrão persistente")
        out["patterns_error"] = str(exc)

    try:
        out["trajectory"] = {
            "train": stop_trajectory(train),
            "validation": stop_trajectory(validation),
        }
    except Exception as exc:
        out["trajectory"] = {"error": str(exc)}

    # P05.2B — laboratório offline. Recebe SOMENTE treino e validação; nenhuma
    # linha, id ou métrica do teste selado chega até ele. Fail-soft: falha aqui
    # não derruba o P05.2A.
    try:
        out["offline_lab"] = build_stop_offline_lab(
            train, validation, out.get("persistent_patterns") or [])
    except Exception as exc:
        log.warning(f"[p05.2b] laboratório offline falhou: {exc}")
        out["offline_lab"] = {
            "phase": "P05.2B", "execution_mode": "ANALYTICS_ONLY", "read_only": True,
            "executable": False, "promotable": False, "shadow_supported": False,
            "holdout_status": HOLDOUT_SEALED, "holdout_outcomes_read": False,
            "holdout_metrics_computed": False, "requires_future_holdout_review": True,
            "status": LAB_UNAVAILABLE, "reason_code": "LAB_ERROR",
            "detail": "não foi possível avaliar as hipóteses nesta execução",
            "source_patterns": [], "candidates": [], "rejected": [],
            "limitations": P052B_LIMITATIONS, "error": str(exc),
        }

    try:
        from services import assertiveness_service as _assert
        out["real"] = await _assert.real_stop_summary(days)
    except Exception as exc:
        log.warning(f"[p05.2a] resumo REAL falhou: {exc}")
        out["real"] = {"error": str(exc)}

    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    return out


def _stop_diagnosis_cacheable(value: Dict[str, Any]) -> bool:
    """Falha total ou parcial nunca permanece no cache do diagnóstico."""
    if value.get("error") or value.get("patterns_error"):
        return False
    for section in ("shadow", "segments", "trajectory", "real", "offline_lab"):
        block = value.get(section)
        if isinstance(block, dict) and block.get("error"):
            return False
    return True


async def get_cached_stop_diagnosis(days: int = P052A_WINDOW_DAYS) -> Dict[str, Any]:
    """Cache single-flight no store/lock do P05. Erro não envenena o cache.

    Chaves negativas reservam o namespace P05.2A sem criar cache paralelo nem
    colidir com o diagnóstico P05A, cujas chaves são dias positivos.
    """
    days = max(30, min(int(days), 365))
    cache_key = -days
    now_mono = time.monotonic()
    cached = _DIAG_CACHE.get(cache_key)
    if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
        return cached[1]
    async with _DIAG_CACHE_LOCK:
        cached = _DIAG_CACHE.get(cache_key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
            return cached[1]
        value = await build_stop_diagnosis(days)
        if _stop_diagnosis_cacheable(value):
            _DIAG_CACHE[cache_key] = (now_mono, value)
            if len(_DIAG_CACHE) > 12:
                oldest = min(_DIAG_CACHE, key=lambda k: _DIAG_CACHE[k][0])
                _DIAG_CACHE.pop(oldest, None)
        return value


async def build_diagnosis(days: int = 30) -> Dict[str, Any]:
    """P05A — onde ganha, onde perde, com qualidade de evidência explícita."""
    out: Dict[str, Any] = {"window_days": days}
    shadow_rows: List[Dict[str, Any]] = []

    try:
        real_rows, real_dq = await _load_real(days)
        out["real"] = {"data_quality": real_dq,
                       "metrics": compute_evidence_metrics(real_rows)}
    except Exception as exc:                                  # fail-soft por seção
        log.warning(f"[p05] diagnóstico REAL falhou: {exc}")
        out["real"] = {"error": str(exc)}

    try:
        shadow_rows, shadow_dq = await _load_shadow(days)
        out["shadow"] = {
            "data_quality": shadow_dq,
            "metrics": compute_evidence_metrics(shadow_rows),
            "feature_coverage": feature_coverage(shadow_rows, _FEATURE_NAMES),
        }
        out["segments"] = segment_rows(shadow_rows)
    except Exception as exc:
        log.warning(f"[p05] diagnóstico SHADOW falhou: {exc}")
        out["shadow"] = {"error": str(exc)}
        out["segments"] = {"error": str(exc)}

    try:
        bt_rows, bt_dq = await _load_backtest(days)
        out["backtest"] = {"data_quality": bt_dq,
                           # Evidência secundária pode ter 20k linhas; bootstrap
                           # completo fica reservado à avaliação admin/offline.
                           "metrics": compute_evidence_metrics(bt_rows, with_bootstrap=False),
                           "note": "evidência histórica SECUNDÁRIA — não equivale a RealTrade"}
    except Exception as exc:
        log.warning(f"[p05] diagnóstico BACKTEST falhou: {exc}")
        out["backtest"] = {"error": str(exc)}

    try:
        out["gate_events"] = await _load_gate_events(min(days, 30))
    except Exception as exc:
        log.warning(f"[p05] gate events falhou: {exc}")
        out["gate_events"] = {"error": str(exc)}

    # P05.1T — MAE/MFE agora vem de telemetria PROSPECTIVA (`features.p05_path`),
    # não de reconstrução histórica. Fail-soft por seção.
    try:
        out["mae_mfe"] = summarize_path_telemetry(shadow_rows)
    except Exception as exc:
        log.warning(f"[p05.1t] telemetria MAE/MFE falhou: {exc}")
        out["mae_mfe"] = {"status": "UNAVAILABLE", "error": str(exc)}
    try:
        out["telemetry"] = await build_telemetry_section(days)
    except Exception as exc:
        log.warning(f"[p05.1t] seção de telemetria falhou: {exc}")
        out["telemetry"] = {"error": str(exc)}
    out["evidence_quality"] = _evidence_quality(out, shadow_rows)
    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def get_cached_diagnosis(days: int = 30) -> Dict[str, Any]:
    """Cache curto por processo para o dashboard não refazer bootstrap/20k
    backtests em cada refresh. A avaliação administrativa nunca usa este cache."""
    days = max(7, min(int(days), 365))
    now_mono = time.monotonic()
    cached = _DIAG_CACHE.get(days)
    if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
        return cached[1]
    async with _DIAG_CACHE_LOCK:
        cached = _DIAG_CACHE.get(days)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
            return cached[1]
        value = await build_diagnosis(days)
        _DIAG_CACHE[days] = (now_mono, value)
        # Limite simples contra cardinalidade acidental de parâmetros.
        if len(_DIAG_CACHE) > 12:
            oldest = min(_DIAG_CACHE, key=lambda k: _DIAG_CACHE[k][0])
            _DIAG_CACHE.pop(oldest, None)
        return value


def _evidence_quality(diag: Dict[str, Any], shadow_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    shadow = diag.get("shadow") or {}
    metrics = shadow.get("metrics") or {}
    n = metrics.get("count") or 0
    cov = shadow.get("feature_coverage") or {}
    usable = [k for k, v in cov.items()
              if (v.get("coverage_pct") or 0) >= P05_MIN_FEATURE_COVERAGE_PCT]
    return {
        "shadow_resolved": n,
        "min_offline_required": P05_MIN_OFFLINE_RESOLVED,
        "maturity": reliability_label(n),
        "ready_for_candidates": n >= P05_MIN_OFFLINE_RESOLVED,
        "features_usable": sorted(usable),
        "features_below_coverage": sorted(set(cov) - set(usable)),
        "excluded_total": (shadow.get("data_quality") or {}).get("excluded_total"),
        "excluded_by_reason": (shadow.get("data_quality") or {}).get("excluded_by_reason"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  P05B — avaliação offline e persistência idempotente
# ════════════════════════════════════════════════════════════════════════════
def evaluate_candidate_offline(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                               candidate: Dict[str, Any], *,
                               include_holdout: bool = True) -> Dict[str, Any]:
    """Roda UM candidato. Na seleção, `include_holdout=False` mantém o teste
    fisicamente fora do cálculo; só o finalista de cada objetivo o abre."""
    knob = candidate["knob"]
    split = temporal_split(rows)
    if not split.get("ok"):
        return {"verdict": STATUS_INSUFFICIENT, "reason_code": "INSUFFICIENT_DATA",
                "detail": f"{split['total']} outcomes válidos (< {split['required']})",
                "coverage": {}}

    # Cobertura e componentes são congelados usando SOMENTE treino. O holdout
    # não pode ligar/desligar um componente nem criar/remover uma hipótese.
    active, coverage_info = active_components_for(split["train"], knob)
    target = _KNOB_COMPONENT[knob]
    target_cov = (coverage_info.get("coverage_pct") or {}).get(target)
    if target_cov is None or target_cov < P05_MIN_FEATURE_COVERAGE_PCT:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "TRAIN_FEATURE_COVERAGE_TOO_LOW",
                "detail": f"cobertura de treino {target_cov}% (< {P05_MIN_FEATURE_COVERAGE_PCT}%)",
                "coverage": coverage_info, "active_components": active}

    cfg = candidate["config"]
    validation = compare_configs(split["validation"], champion, cfg, active)
    train = compare_configs(split["train"], champion, cfg, active)
    if include_holdout:
        test: Dict[str, Any] = compare_configs(split["test"], champion, cfg, active)
        gate = evaluate_offline_gate(candidate["objective"], validation, test)
    else:
        test = {"withheld": True, "reason": "finalista ainda não congelado"}
        gate = evaluate_offline_gate(candidate["objective"], validation, validation)

    folds_out = []
    # Walk-forward de seleção também termina na validação; nunca atravessa o
    # início do holdout antes de o finalista ser congelado.
    selection_rows = list(split["train"]) + list(split["validation"])
    for fold in walkforward_folds(selection_rows, split["folds"]):
        cmp_fold = compare_configs(fold["test"], champion, cfg, active)
        folds_out.append({
            "fold": fold["fold"],
            "test_count": cmp_fold["candidate"]["count"],
            "candidate_expectancy_r": cmp_fold["candidate"]["expectancy_r"],
            "champion_expectancy_r": cmp_fold["champion"]["expectancy_r"],
        })

    return {
        "verdict": gate["verdict"],
        "reason_code": gate["reason_code"],
        "detail": gate["detail"],
        "checks": gate["checks"],
        "objective": candidate["objective"],
        "knob": knob,
        "champion_value": candidate["champion_value"],
        "candidate_value": candidate["candidate_value"],
        "active_components": active,
        "coverage": coverage_info,
        "split": {"total": split["total"], "folds": split["folds"],
                  "train_n": len(split["train"]), "validation_n": len(split["validation"]),
                  "test_n": len(split["test"]), "boundaries": split["boundaries"],
                  "holdout_opened": include_holdout,
                  "note": "teste final só é aberto para o finalista congelado"},
        "train": {"champion": train["champion"], "candidate": train["candidate"]},
        "validation": validation,
        "test": test,
        "walkforward": folds_out,
    }


async def _acquire_p05_lock(session, key: int) -> None:
    """Advisory xact lock PostgreSQL; liberado automaticamente no commit/rollback."""
    from sqlalchemy import text
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def _open_incident_count(session) -> int:
    """Incidentes ainda inseguros. MANUAL_REQUIRED continua sendo aberto."""
    from models.execution_incident import ExecutionIncident
    from sqlalchemy import func, select
    count = (await session.execute(
        select(func.count(ExecutionIncident.id)).where(
            ExecutionIncident.state.notin_(("PROTECTED", "FLAT"))
        )
    )).scalar()
    return int(count or 0)


async def _incident_since_count(session, started_at: datetime) -> int:
    """Qualquer incidente iniciado durante o SHADOW invalida a promoção."""
    from models.execution_incident import ExecutionIncident
    from sqlalchemy import func, select
    count = (await session.execute(
        select(func.count(ExecutionIncident.id)).where(
            ExecutionIncident.created_at >= _utc(started_at)
        )
    )).scalar()
    return int(count or 0)


async def _upsert_experiment(session, *, experiment_key: str, champion_hash: str,
                             candidate_hash: str, objective: str, config: Dict[str, Any],
                             fingerprint: str, cutoff: datetime,
                             offline: Dict[str, Any]) -> Any:
    """Idempotente por `experiment_key`. Config/hash IMUTÁVEIS após o DRAFT —
    processo concorrente não sobrescreve identidade."""
    from models.strategy_experiment import StrategyExperiment
    from sqlalchemy import select
    existing = (await session.execute(
        select(StrategyExperiment).where(StrategyExperiment.experiment_key == experiment_key)
    )).scalar_one_or_none()

    verdict = offline.get("verdict", STATUS_DRAFT)
    now = datetime.now(timezone.utc)
    if existing is None:
        exp = StrategyExperiment(
            experiment_key=experiment_key, champion_hash=champion_hash,
            candidate_hash=candidate_hash, status=STATUS_DRAFT, objective=objective,
            candidate_config=config, dataset_fingerprint=fingerprint,
            dataset_cutoff=cutoff, offline_metrics=offline,
        )
        session.add(exp)
        if can_transition(STATUS_DRAFT, verdict):
            exp.status = verdict
            exp.decision = {"verdict": verdict, "reason_code": offline.get("reason_code"),
                            "detail": offline.get("detail"), "stage": "OFFLINE",
                            "decided_at": now.isoformat()}
            exp.decided_at = now if verdict in (STATUS_REJECTED, STATUS_INSUFFICIENT) else None
        return exp

    # Mesmo cutoff com conteúdo diferente não pode reaproveitar uma decisão
    # antiga. Falha explícita em vez de misturar duas versões do dataset.
    if existing.dataset_fingerprint != fingerprint:
        raise RuntimeError("DATASET_FINGERPRINT_MISMATCH para experiment_key existente")
    if (existing.champion_hash != champion_hash or existing.candidate_hash != candidate_hash
            or existing.candidate_config != config or existing.objective != objective):
        raise RuntimeError("IMMUTABLE_EXPERIMENT_IDENTITY_MISMATCH")

    # Já decidido não reabre; só reaproveita (nova evidência ⇒ nova versão/cutoff).
    if existing.status == STATUS_DRAFT and can_transition(STATUS_DRAFT, verdict):
        existing.offline_metrics = offline
        existing.status = verdict
        existing.decision = {"verdict": verdict, "reason_code": offline.get("reason_code"),
                             "detail": offline.get("detail"), "stage": "OFFLINE",
                             "decided_at": now.isoformat()}
        existing.decided_at = now if verdict in (STATUS_REJECTED, STATUS_INSUFFICIENT) else None
    return existing


async def evaluate_candidates(days: int = 90) -> Dict[str, Any]:
    """P05B — gera candidatos, valida no tempo e persiste (idempotente).
    NÃO acessa exchange. NÃO altera o LIVE."""
    if not P05_ANALYTICS_ENABLED:
        return {"ok": False, "reason": "P05_ANALYTICS_ENABLED=false"}
    champion = discover_champion_config()
    rows, dq = await _load_shadow(days)

    if len(rows) < P05_MIN_OFFLINE_RESOLVED:
        return {"ok": True, "status": STATUS_INSUFFICIENT,
                "reason_code": "INSUFFICIENT_DATA",
                "detail": f"{len(rows)} outcomes válidos (< {P05_MIN_OFFLINE_RESOLVED})",
                "champion": champion, "data_quality": dq,
                "candidates": [], "rejected": []}

    split = temporal_split(rows)
    if not split.get("ok"):
        return {"ok": True, "status": STATUS_INSUFFICIENT,
                "reason_code": split.get("reason"), "detail": "split temporal indisponível",
                "champion": champion, "data_quality": dq, "candidates": [], "rejected": []}

    # Hipóteses e cobertura nascem SOMENTE no treino. O holdout não participa
    # nem da existência do candidato nem da escolha de componentes.
    accepted, rejected = generate_candidates(champion, split["train"])
    cutoff = rows[-1]["resolved_at"]
    fingerprint = dataset_fingerprint(rows)
    champion_hash = canonical_hash(champion)

    previews: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for cand in accepted:
        previews.append((cand, evaluate_candidate_offline(
            rows, champion, cand, include_holdout=False)))

    def _rank(item: Tuple[Dict[str, Any], Dict[str, Any]]) -> Tuple[float, ...]:
        cand, offline = item
        val = offline.get("validation") or {}
        cm = val.get("candidate") or {}
        hm = val.get("champion") or {}
        if cand["objective"] == OBJECTIVE_LOSS_REDUCTION:
            dci = val.get("delta_expectancy_ci") or {}
            return (float(dci.get("low") or -1e9),
                    float(cm.get("expectancy_r") or -1e9),
                    -float(cm.get("max_drawdown_r") or 1e9))
        return (float((cm.get("sum_r") or 0) - (hm.get("sum_r") or 0)),
                float(val.get("added_ops") or 0),
                float(cm.get("expectancy_r") or -1e9))

    selected_hashes: set = set()
    for objective in OBJECTIVES:
        viable = [it for it in previews
                  if it[0]["objective"] == objective
                  and it[1].get("verdict") == STATUS_OFFLINE_VALIDATED]
        if viable:
            winner = max(viable, key=_rank)[0]
            selected_hashes.add(canonical_hash(winner["config"]))

    from db import get_session
    results: List[Dict[str, Any]] = []
    async with get_session() as session:
        # Torna SELECT→INSERT idempotente também entre processos/restarts.
        await _acquire_p05_lock(session, _P05_EVALUATE_LOCK_KEY)
        for cand, preview in previews:
            candidate_hash = canonical_hash(cand["config"])
            if candidate_hash in selected_hashes:
                offline = evaluate_candidate_offline(
                    rows, champion, cand, include_holdout=True)
                selection = "FINALIST_FROZEN_ON_VALIDATION"
            elif preview.get("verdict") == STATUS_OFFLINE_VALIDATED:
                offline = dict(preview)
                offline.update({
                    "verdict": STATUS_REJECTED,
                    "reason_code": "NOT_SELECTED_ON_VALIDATION",
                    "detail": "outro candidato do mesmo objetivo venceu na validação; holdout não foi aberto",
                })
                selection = "NOT_SELECTED_HOLDOUT_UNTOUCHED"
            else:
                offline = preview
                selection = "FAILED_BEFORE_HOLDOUT"
            # Snapshot imutável do champion que originou o hash. P05C/D nunca
            # redescobre uma baseline diferente e finge que é a mesma.
            offline = dict(offline)
            offline["champion_config"] = champion
            offline["selection"] = selection
            key = build_experiment_key(champion_hash, candidate_hash, cutoff)
            exp = await _upsert_experiment(
                session, experiment_key=key, champion_hash=champion_hash,
                candidate_hash=candidate_hash, objective=cand["objective"],
                config=cand["config"], fingerprint=fingerprint, cutoff=cutoff,
                offline=offline)
            results.append({
                "experiment_key": key, "candidate_hash": candidate_hash,
                "objective": cand["objective"], "knob": cand["knob"],
                "champion_value": cand["champion_value"],
                "candidate_value": cand["candidate_value"],
                "verdict": offline.get("verdict"),
                "reason_code": offline.get("reason_code"),
                "detail": offline.get("detail"),
                "selection": selection,
                "status": exp.status,
            })
        await session.commit()                       # transação única

    return {
        "ok": True, "champion": champion, "champion_hash": champion_hash,
        "dataset_fingerprint": fingerprint, "dataset_cutoff": cutoff.isoformat(),
        "data_quality": dq, "sample": len(rows),
        "candidates": results, "rejected": rejected,
        "finalists": len(selected_hashes),
        "max_candidates": P05_MAX_CANDIDATES,
        "note": "nenhum candidato válido é resultado ACEITÁVEL — não se força vencedor",
    }


# ════════════════════════════════════════════════════════════════════════════
#  P05.1 — candidatos CONTEXTUAIS (ANALYTICS_ONLY)
#
#  Motivação (medida no P05 com 90 dias): os candidatos GLOBAIS de SCORE_MIN
#  falharam — 57/59 rejeitados em LOSS_REDUCTION, 53 sem +10% de operações, e 51
#  passou na validação mas o holdout levou o drawdown de 4R para 7R. Baixar o
#  piso globalmente troca perdas por volume ruim.
#
#  O P05.1 pergunta outra coisa: existe CONTEXTO em que vale bloquear, e contexto
#  em que vale afrouxar 2 pontos? É SOMENTE análise offline — nunca entra no
#  executor, nunca vai para Shadow, nunca gera plano executável.
# ════════════════════════════════════════════════════════════════════════════
P051_PHASE = "P05.1"
P051_EXECUTION_MODE = "ANALYTICS_ONLY"
P051_BLOCK_REASON = "P051_ANALYTICS_ONLY"

# Eixos permitidos NESTA fase. Baixa cardinalidade e operáveis; símbolo, base,
# padrão, direção, hora, dia, score_bin e ATR band ficam de fora de propósito
# (overfitting, múltiplas comparações e regra difícil de operar).
P051_CONTEXT_AXES = ("regime", "entry_zone_type")
P051_ACTIONS = ("BLOCK", "SCORE_DELTA")
P051_SCORE_DELTA = -2.0                 # único valor aceito nesta fase
P051_SCHEMA_VERSION = 1

P051_MIN_TRAIN_SEGMENT = 30             # observações do contexto no treino
P051_MIN_STAGE_SEGMENT = 20             # observações na validação/teste
P051_MIN_AFFECTED = 20                  # operações realmente afetadas pela regra
P051_MAX_PER_OBJECTIVE = 4
P051_MAX_CANDIDATES = 8

CONTEXT_UNKNOWN = "__UNKNOWN__"


def context_value(row: Dict[str, Any], axis: str) -> str:
    """Valor do contexto EXATAMENTE como persistido no snapshot.

    Lê somente `features["regime"]` e `features["entry_zone_type"]`. Ausente,
    vazio ou eixo não permitido ⇒ `CONTEXT_UNKNOWN` (nunca um valor inventado).
    """
    if axis not in P051_CONTEXT_AXES:
        return CONTEXT_UNKNOWN
    raw = (row.get("features") or {}).get(axis)
    if raw is None:
        return CONTEXT_UNKNOWN
    value = str(raw).strip()
    return value or CONTEXT_UNKNOWN


def validate_context_rule(rule: Any) -> Dict[str, Any]:
    """Validador SEPARADO do P05 global — `CONTEXT_RULE` não entra na
    `KNOB_ALLOWLIST`. Levanta `CandidateValidationError`."""
    if not isinstance(rule, dict) or not rule:
        raise CandidateValidationError("CONTEXT_RULE vazio")
    allowed = {"schema_version", "axis", "value", "action", "score_delta"}
    extra = set(rule) - allowed
    if extra:
        raise CandidateValidationError(f"campos proibidos em CONTEXT_RULE: {sorted(extra)}")

    version = rule.get("schema_version")
    if version != P051_SCHEMA_VERSION or isinstance(version, bool):
        raise CandidateValidationError(f"schema_version deve ser {P051_SCHEMA_VERSION}")

    axis = rule.get("axis")
    if axis not in P051_CONTEXT_AXES:
        raise CandidateValidationError(
            f"axis deve ser um de {list(P051_CONTEXT_AXES)} (recebido {axis!r})")

    value = rule.get("value")
    if not isinstance(value, str) or not value.strip():
        raise CandidateValidationError("value deve ser string não vazia")
    if value.strip() == CONTEXT_UNKNOWN:
        raise CandidateValidationError("value não pode ser o marcador de UNKNOWN")

    action = rule.get("action")
    if action not in P051_ACTIONS:
        raise CandidateValidationError(
            f"action deve ser um de {list(P051_ACTIONS)} (recebido {action!r})")

    canonical: Dict[str, Any] = {
        "schema_version": P051_SCHEMA_VERSION,
        "axis": axis,
        "value": value.strip(),
        "action": action,
    }
    if action == "BLOCK":
        if "score_delta" in rule:
            raise CandidateValidationError("BLOCK não aceita score_delta")
    else:
        delta = rule.get("score_delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise CandidateValidationError("SCORE_DELTA exige score_delta numérico")
        num = _finite(delta)
        if num is None:
            raise CandidateValidationError("score_delta NaN/infinito rejeitado")
        if abs(num - P051_SCORE_DELTA) > 1e-9:
            raise CandidateValidationError(
                f"score_delta aceito nesta fase é apenas {P051_SCORE_DELTA}")
        canonical["score_delta"] = P051_SCORE_DELTA
    return canonical


def validate_contextual_candidate_config(config: Any) -> Dict[str, Any]:
    """Exatamente UMA chave lógica `CONTEXT_RULE`, com o schema completo."""
    if not isinstance(config, dict) or not config:
        raise CandidateValidationError("configuração vazia")
    if set(config) != {"CONTEXT_RULE"}:
        raise CandidateValidationError(
            f"configuração contextual aceita apenas CONTEXT_RULE (recebido {sorted(config)})")
    return {"CONTEXT_RULE": validate_context_rule(config["CONTEXT_RULE"])}


def contextual_eligibility(row: Dict[str, Any], champion: Dict[str, Any],
                           rule: Dict[str, Any],
                           active_components: Sequence[str]) -> Optional[bool]:
    """Champion + UMA regra contextual, reaproveitando `eligibility` sem alterá-la.

    * `None` (UNKNOWN) quando o champion não é decidível OU o contexto está
      ausente — nunca vira BLOCKED nem ELIGIBLE por fallback.
    * Fora do contexto: o veredito é IDÊNTICO ao do champion.
    * `BLOCK`: bloqueia somente o contexto correspondente.
    * `SCORE_DELTA`: baixa o piso de score em exatamente 2 pontos SOMENTE no
      contexto — e apenas para linhas cujo ÚNICO bloqueio era o score-min. Se
      outro gate barrou (R:R, P(TP1), liquidez, P04, ATR, funding, proximity,
      struct-chase, tempo, tier), a regra NÃO reativa a linha.
    """
    base = eligibility(row, champion, active_components)
    if base is None:
        return None

    ctx = context_value(row, rule.get("axis"))
    if ctx == CONTEXT_UNKNOWN:
        return None
    if ctx != rule.get("value"):
        return base                       # contexto diferente: intocado

    if rule.get("action") == "BLOCK":
        return False

    # ── SCORE_DELTA ──────────────────────────────────────────────────────────
    if "score_min" not in active_components:
        return None                       # sem o gate de score não há o que afrouxar
    if base is True:
        return True                       # já passava; afrouxar não retira nada

    others = [c for c in active_components if c != "score_min"]
    other_verdict = eligibility(row, champion, others)
    if other_verdict is None:
        return None                       # não dá pra provar que só o score barrou
    if other_verdict is False:
        return False                      # outro gate barrou → não reativa

    score = _execution_score(row, champion)
    score_min = _finite(champion.get("SCORE_MIN"))
    if score is None or score_min is None:
        return None
    return score >= score_min + P051_SCORE_DELTA


def contextual_active_components(rows: Sequence[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    """Componentes com cobertura suficiente. A regra contextual não substitui
    nenhum gate — ela atua POR CIMA do champion."""
    coverage = {c: component_coverage(rows, c) for c in _COMPONENT_FEATURES}
    active = [c for c, cov in coverage.items()
              if cov is not None and cov >= P05_MIN_FEATURE_COVERAGE_PCT]
    skipped = {c: cov for c, cov in coverage.items() if c not in active}
    return active, {"coverage_pct": coverage, "skipped_low_coverage": skipped}


def axis_coverage(rows: Sequence[Dict[str, Any]], axis: str) -> Optional[float]:
    """Cobertura (%) do eixo de contexto na amostra."""
    n = len(rows)
    if n == 0:
        return None
    present = sum(1 for r in rows if context_value(r, axis) != CONTEXT_UNKNOWN)
    return round(present / n * 100, 1)


def compare_contextual(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                       rule: Dict[str, Any], active: Sequence[str]) -> Dict[str, Any]:
    """Champion × champion+regra sobre o MESMO dataset, pareado pela oportunidade.

    UNKNOWN (champion indecidível ou contexto ausente) é excluído dos DOIS lados.
    """
    evaluable = [
        r for r in rows
        if eligibility(r, champion, active) is not None
        and contextual_eligibility(r, champion, rule, active) is not None
    ]
    champ_rows, _ = split_by_config(evaluable, champion, active)
    cand_rows = [r for r in evaluable
                 if contextual_eligibility(r, champion, rule, active)]

    champ_m = compute_evidence_metrics(champ_rows)
    cand_m = compute_evidence_metrics(cand_rows)
    champ_ids = {id(r) for r in champ_rows}
    cand_ids = {id(r) for r in cand_rows}
    added = [r for r in cand_rows if id(r) not in champ_ids]
    avoided = [r for r in champ_rows if id(r) not in cand_ids]

    added_r = [r["realized_r"] for r in added]
    avoided_r = [r["realized_r"] for r in avoided]
    in_context = [r for r in evaluable if context_value(r, rule["axis"]) == rule["value"]]

    return {
        "evaluable": len(evaluable),
        "unknown_excluded": len(rows) - len(evaluable),
        "in_context": len(in_context),
        "champion": champ_m,
        "candidate": cand_m,
        "added_ops": len(added),
        "avoided_ops": len(avoided),
        "affected_ops": len(added) + len(avoided),
        "added_expectancy_r": round(sum(added_r) / len(added_r), 4) if added_r else None,
        "avoided_expectancy_r": round(sum(avoided_r) / len(avoided_r), 4) if avoided_r else None,
        "added_expectancy_ci": bootstrap_mean_ci(added_r),
        "avoided_expectancy_ci": bootstrap_mean_ci(avoided_r),
        # Pareado pela MESMA oportunidade — champion e candidato compartilham a
        # amostra, então IC independente seria incorreto.
        "delta_expectancy_ci": bootstrap_paired_membership_delta_ci(
            evaluable, champ_ids, cand_ids),
        "material_segment_regressions": _material_segment_regressions(champ_rows, cand_rows),
        "selects_subset": len(cand_rows) < len(champ_rows),
        "note": ("regra contextual aplicada POR CIMA do champion; UNKNOWN excluído "
                 "dos dois lados; contexto não é causalidade"),
    }


def evaluate_contextual_gate(objective: str, action: str, validation: Dict[str, Any],
                             test: Dict[str, Any]) -> Dict[str, Any]:
    """Gates do P05.1 — aplicados em validação E teste. Nunca força vencedor."""
    checks: List[Dict[str, Any]] = []

    def _chk(name: str, passed: Optional[bool], detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    cand_t = test.get("candidate") or {}
    if (cand_t.get("count") or 0) < P05_MIN_OOS_RESOLVED:
        return {"verdict": STATUS_INSUFFICIENT, "reason_code": "OOS_SAMPLE_TOO_SMALL",
                "detail": (f"teste final tem {cand_t.get('count')} outcomes do candidato "
                           f"(< {P05_MIN_OOS_RESOLVED})"),
                "checks": checks}
    if objective not in OBJECTIVES:
        return {"verdict": STATUS_REJECTED, "reason_code": "OBJETIVO_INVALIDO",
                "detail": objective, "checks": checks}

    for stage, cmp_ in (("validacao", validation), ("teste", test)):
        cand = cmp_.get("candidate") or {}
        champ = cmp_.get("champion") or {}
        exp = cand.get("expectancy_r")

        # UNKNOWN nunca aprova: a comparação precisa de amostra avaliável real.
        _chk(f"{stage}_sem_aprovacao_por_unknown",
             (cmp_.get("evaluable") or 0) > 0 and (cand.get("count") or 0) > 0,
             f"evaluable={cmp_.get('evaluable')} unknown_excluded={cmp_.get('unknown_excluded')}")
        _chk(f"{stage}_amostra_afetada_minima",
             (cmp_.get("affected_ops") or 0) >= P051_MIN_AFFECTED,
             f"afetadas={cmp_.get('affected_ops')} (mín {P051_MIN_AFFECTED})")
        _chk(f"{stage}_expectancy_candidate_positiva",
             exp is not None and exp > 0 and (cand.get("sum_r") or 0) > 0,
             f"expectancy={exp} total_r={cand.get('sum_r')}")
        _chk(f"{stage}_sem_segmento_material_negativo",
             not (cmp_.get("material_segment_regressions") or []),
             f"regressões={(cmp_.get('material_segment_regressions') or [])[:5]}")

        dci = cmp_.get("delta_expectancy_ci") or {}
        if objective == OBJECTIVE_LOSS_REDUCTION:
            _chk(f"{stage}_ci_delta_pareado_acima_de_zero",
                 dci.get("low") is not None and dci["low"] > 0, f"IC pareado={dci}")
            _chk(f"{stage}_profit_factor_nao_pior",
                 _pf(cand) >= _pf(champ),
                 f"cand={cand.get('profit_factor')} champ={champ.get('profit_factor')}")
            _chk(f"{stage}_drawdown_nao_pior",
                 (cand.get("max_drawdown_r") or 0) <= (champ.get("max_drawdown_r") or 0) + 1e-9,
                 f"cand={cand.get('max_drawdown_r')} champ={champ.get('max_drawdown_r')}")
            _chk(f"{stage}_operacoes_min_70pct",
                 (champ.get("count") or 0) > 0 and (cand.get("count") or 0) >= 0.7 * champ["count"],
                 f"cand={cand.get('count')} champ={champ.get('count')}")
            # O contexto bloqueado precisa ser comprovadamente ruim — não basta
            # a média; o IC superior das evitadas tem que ficar abaixo de zero.
            avoided_exp = cmp_.get("avoided_expectancy_r")
            _chk(f"{stage}_contexto_bloqueado_negativo",
                 avoided_exp is not None and avoided_exp < 0,
                 f"expectancy das evitadas={avoided_exp}")
            aci = cmp_.get("avoided_expectancy_ci") or {}
            _chk(f"{stage}_ci_superior_evitadas_abaixo_de_zero",
                 aci.get("high") is not None and aci["high"] < 0,
                 f"IC das evitadas={aci}")
        else:
            _chk(f"{stage}_operacoes_min_110pct",
                 (champ.get("count") or 0) > 0 and (cand.get("count") or 0) >= 1.1 * champ["count"],
                 f"cand={cand.get('count')} champ={champ.get('count')}")
            eci = cand.get("expectancy_ci") or {}
            _chk(f"{stage}_ci_expectancy_acima_de_zero",
                 eci.get("low") is not None and eci["low"] > 0, f"IC={eci}")
            _chk(f"{stage}_total_r_maior",
                 (cand.get("sum_r") or 0) > (champ.get("sum_r") or 0),
                 f"cand={cand.get('sum_r')} champ={champ.get('sum_r')}")
            _chk(f"{stage}_profit_factor_min_1", _pf(cand) >= 1.0,
                 f"cand={cand.get('profit_factor')}")
            _chk(f"{stage}_drawdown_max_110pct",
                 (cand.get("max_drawdown_r") or 0) <= 1.1 * (champ.get("max_drawdown_r") or 0) + 1e-9,
                 f"cand={cand.get('max_drawdown_r')} champ={champ.get('max_drawdown_r')}")
            add_exp = cmp_.get("added_expectancy_r")
            _chk(f"{stage}_operacoes_adicionais_positivas",
                 add_exp is not None and add_exp > 0, f"expectancy das adicionais={add_exp}")
            adci = cmp_.get("added_expectancy_ci") or {}
            _chk(f"{stage}_ci_adicionais_acima_de_zero",
                 adci.get("low") is not None and adci["low"] > 0,
                 f"IC das adicionais={adci}")
            _chk(f"{stage}_ci_delta_pareado_nao_negativo",
                 dci.get("low") is not None and dci["low"] >= 0, f"IC pareado={dci}")

    # Estrutural: BLOCK só remove linhas; SCORE_DELTA só reativa linhas cujo
    # único bloqueio era o score-min (garantido em `contextual_eligibility`).
    _chk("nenhum_gate_p01_p04_relaxado", action in P051_ACTIONS,
         "BLOCK apenas remove; SCORE_DELTA não reativa linha barrada por outro gate")

    failed = [c["check"] for c in checks if not c["passed"]]
    if failed:
        return {"verdict": STATUS_REJECTED, "reason_code": "GATE_NAO_ATENDIDO",
                "detail": f"falhou: {', '.join(failed)}", "checks": checks}
    return {"verdict": STATUS_OFFLINE_VALIDATED, "reason_code": "GATES_OK",
            "detail": "validação e teste positivos (resultado ANALÍTICO, não promovível)",
            "checks": checks}


def generate_contextual_candidates(champion: Dict[str, Any],
                                   train_rows: Sequence[Dict[str, Any]]
                                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Hipóteses derivadas SOMENTE do treino. Ordem determinística, sem grid
    search, um contexto por candidato. Sem contexto elegível ⇒ sem candidato."""
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    total_cap = min(P051_MAX_CANDIDATES, P05_MAX_CANDIDATES)

    for objective, action in ((OBJECTIVE_LOSS_REDUCTION, "BLOCK"),
                              (OBJECTIVE_MORE_OPERATIONS, "SCORE_DELTA")):
        picked = 0
        for axis in P051_CONTEXT_AXES:
            cov = axis_coverage(train_rows, axis)
            if cov is None or cov < P05_MIN_FEATURE_COVERAGE_PCT:
                rejected.append({"axis": axis, "objective": objective,
                                 "reason": "AXIS_COVERAGE_TOO_LOW",
                                 "coverage_pct": cov,
                                 "required_pct": P05_MIN_FEATURE_COVERAGE_PCT})
                continue

            groups: Dict[str, List[Dict[str, Any]]] = {}
            for row in train_rows:
                value = context_value(row, axis)
                if value != CONTEXT_UNKNOWN:
                    groups.setdefault(value, []).append(row)

            scored: List[Tuple[str, Dict[str, Any]]] = []
            for value, grp in groups.items():
                metrics = compute_evidence_metrics(grp, with_bootstrap=False)
                n = metrics["count"]
                exp = metrics["expectancy_r"]
                if n < P051_MIN_TRAIN_SEGMENT:
                    rejected.append({"axis": axis, "value": value, "objective": objective,
                                     "reason": "TRAIN_SEGMENT_TOO_SMALL",
                                     "count": n, "required": P051_MIN_TRAIN_SEGMENT})
                    continue
                if metrics["reliability"] not in (RELIABILITY_USABLE, RELIABILITY_STRONG):
                    rejected.append({"axis": axis, "value": value, "objective": objective,
                                     "reason": "RELIABILITY_BELOW_USABLE",
                                     "reliability": metrics["reliability"], "count": n})
                    continue
                if exp is None:
                    rejected.append({"axis": axis, "value": value, "objective": objective,
                                     "reason": "EXPECTANCY_UNAVAILABLE", "count": n})
                    continue
                wanted = exp < 0 if objective == OBJECTIVE_LOSS_REDUCTION else exp > 0
                if not wanted:
                    rejected.append({"axis": axis, "value": value, "objective": objective,
                                     "reason": "CONTEXT_DIRECTION_MISMATCH",
                                     "expectancy_r": exp, "count": n})
                    continue
                scored.append((value, metrics))

            # Ordem determinística: pior primeiro (BLOCK) / melhor primeiro
            # (SCORE_DELTA); desempate por amostra e depois pelo nome do valor.
            reverse = objective == OBJECTIVE_MORE_OPERATIONS
            scored.sort(key=lambda it: (it[1]["expectancy_r"], it[1]["count"], it[0]),
                        reverse=reverse)

            for value, metrics in scored:
                if picked >= P051_MAX_PER_OBJECTIVE or len(accepted) >= total_cap:
                    break
                rule: Dict[str, Any] = {"schema_version": P051_SCHEMA_VERSION,
                                        "axis": axis, "value": value, "action": action}
                if action == "SCORE_DELTA":
                    rule["score_delta"] = P051_SCORE_DELTA
                try:
                    config = validate_contextual_candidate_config({"CONTEXT_RULE": rule})
                except CandidateValidationError as exc:
                    rejected.append({"axis": axis, "value": value, "objective": objective,
                                     "reason": "INVALID_CONTEXT_RULE", "detail": str(exc)})
                    continue
                accepted.append({
                    "objective": objective,
                    "config": config,
                    "rule": config["CONTEXT_RULE"],
                    "axis": axis,
                    "value": value,
                    "action": action,
                    "context_evidence": {
                        "train_count": metrics["count"],
                        "train_expectancy_r": metrics["expectancy_r"],
                        "train_win_rate_pct": metrics["win_rate_pct"],
                        "train_total_r": metrics["sum_r"],
                        "reliability": metrics["reliability"],
                        "axis_coverage_pct": cov,
                    },
                    "generation_reason": (
                        f"contexto {axis}={value} com expectancy "
                        f"{metrics['expectancy_r']:+.4f}R em {metrics['count']} casos de TREINO"),
                })
                picked += 1
    return accepted[:total_cap], rejected


def evaluate_contextual_candidate_offline(rows: Sequence[Dict[str, Any]],
                                          champion: Dict[str, Any],
                                          candidate: Dict[str, Any], *,
                                          include_holdout: bool = True) -> Dict[str, Any]:
    """Pipeline temporal de UMA regra contextual. `include_holdout=False` mantém
    o teste fisicamente fora do cálculo — só o finalista o abre."""
    rule = candidate["rule"]
    split = temporal_split(rows)
    if not split.get("ok"):
        return {"verdict": STATUS_INSUFFICIENT, "reason_code": "INSUFFICIENT_DATA",
                "detail": f"{split['total']} outcomes válidos (< {split['required']})",
                "coverage": {}}

    # Cobertura e componentes CONGELADOS no treino — o holdout não liga/desliga
    # componente nem cria/remove hipótese.
    active, coverage_info = contextual_active_components(split["train"])
    axis = rule["axis"]
    train_axis_cov = axis_coverage(split["train"], axis)
    coverage_info["axis"] = axis
    coverage_info["axis_coverage_pct"] = {
        "train": train_axis_cov,
        "validation": axis_coverage(split["validation"], axis),
        "test": axis_coverage(split["test"], axis) if include_holdout else None,
    }
    if train_axis_cov is None or train_axis_cov < P05_MIN_FEATURE_COVERAGE_PCT:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "TRAIN_AXIS_COVERAGE_TOO_LOW",
                "detail": f"cobertura de treino do eixo {axis}: {train_axis_cov}%",
                "coverage": coverage_info, "active_components": active}
    if rule["action"] == "SCORE_DELTA" and "score_min" not in active:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "SCORE_COMPONENT_UNAVAILABLE",
                "detail": "sem o gate de score na amostra não há piso para afrouxar",
                "coverage": coverage_info, "active_components": active}

    # Amostra mínima do contexto em cada estágio.
    def _stage_n(stage_rows) -> int:
        return sum(1 for r in stage_rows if context_value(r, axis) == rule["value"])

    stage_counts = {"train": _stage_n(split["train"]),
                    "validation": _stage_n(split["validation"]),
                    "test": _stage_n(split["test"]) if include_holdout else None}
    if stage_counts["validation"] < P051_MIN_STAGE_SEGMENT:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "VALIDATION_SEGMENT_TOO_SMALL",
                "detail": (f"contexto tem {stage_counts['validation']} casos na validação "
                           f"(< {P051_MIN_STAGE_SEGMENT})"),
                "coverage": coverage_info, "active_components": active,
                "context_stage_counts": stage_counts}
    if include_holdout and stage_counts["test"] < P051_MIN_STAGE_SEGMENT:
        return {"verdict": STATUS_INSUFFICIENT,
                "reason_code": "TEST_SEGMENT_TOO_SMALL",
                "detail": (f"contexto tem {stage_counts['test']} casos no teste "
                           f"(< {P051_MIN_STAGE_SEGMENT})"),
                "coverage": coverage_info, "active_components": active,
                "context_stage_counts": stage_counts}

    train_cmp = compare_contextual(split["train"], champion, rule, active)
    validation = compare_contextual(split["validation"], champion, rule, active)
    if include_holdout:
        test: Dict[str, Any] = compare_contextual(split["test"], champion, rule, active)
        gate = evaluate_contextual_gate(candidate["objective"], rule["action"],
                                        validation, test)
    else:
        test = {"withheld": True, "reason": "finalista ainda não congelado"}
        gate = evaluate_contextual_gate(candidate["objective"], rule["action"],
                                        validation, validation)

    folds_out = []
    selection_rows = list(split["train"]) + list(split["validation"])
    for fold in walkforward_folds(selection_rows, split["folds"]):
        cmp_fold = compare_contextual(fold["test"], champion, rule, active)
        folds_out.append({
            "fold": fold["fold"],
            "test_count": cmp_fold["candidate"]["count"],
            "candidate_expectancy_r": cmp_fold["candidate"]["expectancy_r"],
            "champion_expectancy_r": cmp_fold["champion"]["expectancy_r"],
        })

    return {
        "phase": P051_PHASE,
        "execution_mode": P051_EXECUTION_MODE,
        "promotable": False,
        "shadow_supported": False,
        "verdict": gate["verdict"],
        "reason_code": gate["reason_code"],
        "detail": gate["detail"],
        "checks": gate["checks"],
        "objective": candidate["objective"],
        "context_rule": rule,
        "context_evidence": candidate.get("context_evidence"),
        "generation_reason": candidate.get("generation_reason"),
        "context_stage_counts": stage_counts,
        "active_components": active,
        "coverage": coverage_info,
        "split": {"total": split["total"], "folds": split["folds"],
                  "train_n": len(split["train"]), "validation_n": len(split["validation"]),
                  "test_n": len(split["test"]), "boundaries": split["boundaries"],
                  "holdout_opened": include_holdout,
                  "note": "teste final só é aberto para o finalista congelado"},
        "train": {"champion": train_cmp["champion"], "candidate": train_cmp["candidate"]},
        "validation": validation,
        "test": test,
        "walkforward": folds_out,
        "applicability": ("Requer uma fase posterior de integração do contexto ao "
                          "executor. Não é aplicável ao LIVE."),
    }


def is_contextual_experiment(exp: Any) -> bool:
    """Experimento P05.1 (analítico) — reconhecido pela config OU pelos marcadores."""
    config = getattr(exp, "candidate_config", None)
    if isinstance(config, dict) and "CONTEXT_RULE" in config:
        return True
    offline = getattr(exp, "offline_metrics", None)
    if isinstance(offline, dict):
        if offline.get("phase") == P051_PHASE:
            return True
        if offline.get("execution_mode") == P051_EXECUTION_MODE:
            return True
        if offline.get("shadow_supported") is False:
            return True
    return False


async def evaluate_contextual_candidates(days: int = 90) -> Dict[str, Any]:
    """P05.1 — candidatos contextuais, validação temporal e persistência
    idempotente. ANALYTICS_ONLY: não vai para Shadow, não promove, não executa."""
    if not P05_ANALYTICS_ENABLED:
        return {"ok": False, "reason": "P05_ANALYTICS_ENABLED=false"}

    champion = discover_champion_config()
    rows, dq = await _load_shadow(days)
    base = {
        "ok": True, "phase": P051_PHASE, "execution_mode": P051_EXECUTION_MODE,
        "promotable": False, "shadow_supported": False,
        "champion": champion, "data_quality": dq, "sample": len(rows),
        "live_untouched": True,
        "allowed_axes": list(P051_CONTEXT_AXES),
        "limitations": [
            "contexto não é causalidade — o eixo pode estar apenas rotulando o regime",
            "segmento pequeno não é edge; a amostra mínima é exigida em treino, validação e teste",
            "SCORE_DELTA mantém a banda do quality-edge no piso do champion (gate desligado por padrão)",
            "resultado é ANALÍTICO: não existe Shadow nem promoção para P05.1",
        ],
    }

    if len(rows) < P05_MIN_OFFLINE_RESOLVED:
        return {**base, "status": STATUS_INSUFFICIENT, "reason_code": "INSUFFICIENT_DATA",
                "detail": f"{len(rows)} outcomes válidos (< {P05_MIN_OFFLINE_RESOLVED})",
                "candidates": [], "rejected": [], "finalists": 0}

    split = temporal_split(rows)
    if not split.get("ok"):
        return {**base, "status": STATUS_INSUFFICIENT, "reason_code": split.get("reason"),
                "detail": "split temporal indisponível",
                "candidates": [], "rejected": [], "finalists": 0}

    accepted, rejected = generate_contextual_candidates(champion, split["train"])
    if not accepted:
        return {**base, "status": STATUS_INSUFFICIENT,
                "reason_code": "NO_CONTEXT_WITH_SUFFICIENT_EVIDENCE",
                "detail": "nenhum contexto com evidência suficiente no treino",
                "candidates": [], "rejected": rejected, "finalists": 0}

    cutoff = rows[-1]["resolved_at"]
    fingerprint = dataset_fingerprint(rows)
    champion_hash = canonical_hash(champion)

    # Seleção acontece SOMENTE na validação; o holdout fica fechado.
    previews: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for cand in accepted:
        previews.append((cand, evaluate_contextual_candidate_offline(
            rows, champion, cand, include_holdout=False)))

    def _rank(item: Tuple[Dict[str, Any], Dict[str, Any]]) -> Tuple[float, ...]:
        cand, offline = item
        val = offline.get("validation") or {}
        cm = val.get("candidate") or {}
        hm = val.get("champion") or {}
        if cand["objective"] == OBJECTIVE_LOSS_REDUCTION:
            dci = val.get("delta_expectancy_ci") or {}
            return (float(dci.get("low") or -1e9),
                    float(cm.get("expectancy_r") or -1e9),
                    -float(cm.get("max_drawdown_r") or 1e9))
        return (float((cm.get("sum_r") or 0) - (hm.get("sum_r") or 0)),
                float(val.get("added_ops") or 0),
                float(cm.get("expectancy_r") or -1e9))

    selected_hashes: set = set()
    for objective in OBJECTIVES:
        viable = [it for it in previews
                  if it[0]["objective"] == objective
                  and it[1].get("verdict") == STATUS_OFFLINE_VALIDATED]
        if viable:
            selected_hashes.add(canonical_hash(max(viable, key=_rank)[0]["config"]))

    from db import get_session
    results: List[Dict[str, Any]] = []
    async with get_session() as session:
        await _acquire_p05_lock(session, _P05_EVALUATE_LOCK_KEY)
        for cand, preview in previews:
            candidate_hash = canonical_hash(cand["config"])
            if candidate_hash in selected_hashes:
                offline = evaluate_contextual_candidate_offline(
                    rows, champion, cand, include_holdout=True)
                selection = "FINALIST_FROZEN_ON_VALIDATION"
            elif preview.get("verdict") == STATUS_OFFLINE_VALIDATED:
                offline = dict(preview)
                offline.update({
                    "verdict": STATUS_REJECTED,
                    "reason_code": "NOT_SELECTED_ON_VALIDATION",
                    "detail": "outro contexto do mesmo objetivo venceu na validação; holdout não foi aberto",
                })
                selection = "NOT_SELECTED_HOLDOUT_UNTOUCHED"
            else:
                offline = preview
                selection = "FAILED_BEFORE_HOLDOUT"

            offline = dict(offline)
            offline.update({
                "champion_config": champion,
                "selection": selection,
                "phase": P051_PHASE,
                "execution_mode": P051_EXECUTION_MODE,
                "promotable": False,
                "shadow_supported": False,
            })
            key = build_experiment_key(champion_hash, candidate_hash, cutoff)
            exp = await _upsert_experiment(
                session, experiment_key=key, champion_hash=champion_hash,
                candidate_hash=candidate_hash, objective=cand["objective"],
                config=cand["config"], fingerprint=fingerprint, cutoff=cutoff,
                offline=offline)
            results.append({
                "experiment_key": key, "candidate_hash": candidate_hash,
                "objective": cand["objective"], "axis": cand["axis"],
                "value": cand["value"], "action": cand["action"],
                "context_evidence": cand.get("context_evidence"),
                "generation_reason": cand.get("generation_reason"),
                "verdict": offline.get("verdict"),
                "reason_code": offline.get("reason_code"),
                "detail": offline.get("detail"),
                "selection": selection,
                "status": exp.status,
                "promotable": False,
                "shadow_supported": False,
            })
        await session.commit()                       # transação única

    return {
        **base,
        "champion_hash": champion_hash,
        "dataset_fingerprint": fingerprint,
        "dataset_cutoff": cutoff.isoformat(),
        "candidates": results,
        "rejected": rejected,
        "finalists": len(selected_hashes),
        "max_candidates": min(P051_MAX_CANDIDATES, P05_MAX_CANDIDATES),
        "note": ("nenhum candidato válido é resultado ACEITÁVEL — não se força vencedor. "
                 "Requer uma fase posterior de integração do contexto ao executor. "
                 "Não é aplicável ao LIVE."),
    }


# ════════════════════════════════════════════════════════════════════════════
#  P05.1R — monitor de PRONTIDÃO de evidência (SOMENTE LEITURA)
#
#  Responde "já dá para reavaliar?" — nunca "o candidato é bom?". Conta
#  oportunidades que cada regra JÁ CONGELADA afetaria, projeta amostra e estima
#  quando uma nova avaliação passa a fazer sentido.
#
#  GARANTIAS DURAS:
#   • ZERO escrita: nenhum INSERT/UPDATE/DELETE, nenhum commit, nenhum status
#     alterado, nenhum experimento criado.
#   • HOLDOUT SELADO: `realized_r` NUNCA é lido; nenhuma métrica de outcome
#     (expectancy/PF/drawdown/win rate/IC) é calculada; nenhum gate é executado.
#     Contar linhas cronológicas NÃO é abrir o holdout.
# ════════════════════════════════════════════════════════════════════════════
P051R_PHASE = "P05.1R"
HOLDOUT_SEALED = "SEALED"

# Prontidão (valores permitidos)
READINESS_WAITING = "WAITING_FOR_DATA"
READINESS_READY = "READY_FOR_REEVALUATION"
READINESS_REFUTED = "REFUTED_LAST_RUN"
READINESS_MIXED = "MIXED_EVIDENCE"
READINESS_RETENTION_AT_RISK = "RETENTION_AT_RISK"
READINESS_NO_METADATA = "INSUFFICIENT_METADATA"
READINESS_UNKNOWN = "UNKNOWN"

# Classificação da última falha
FAILURE_SAMPLE_LIMITED = "SAMPLE_LIMITED"
FAILURE_REFUTED = "REFUTED_LAST_RUN"
FAILURE_MIXED = "MIXED"
FAILURE_NO_METADATA = "INSUFFICIENT_METADATA"
FAILURE_NONE = "NO_FAILURE"

# Veredito de "afetada" por linha
AFFECTED_YES = "AFFECTED"
AFFECTED_NO = "NOT_AFFECTED"
AFFECTED_UNKNOWN = "UNKNOWN"

P051R_MIN_OBSERVED_DAYS_FOR_RATE = 7
P051R_TARGET_RETENTION_DAYS = 120
P051R_MAX_UNKNOWN_PCT = 100.0 - P05_MIN_FEATURE_COVERAGE_PCT   # UNKNOWN não pode comer a cobertura

# Falhas que indicam DIREÇÃO ECONÔMICA CONTRARIADA (mais amostra não resolve).
_SUBSTANTIVE_CHECKS = (
    "contexto_bloqueado_negativo",        # evitadas tinham expectancy positiva
    "operacoes_adicionais_positivas",     # adicionadas tinham expectancy negativa
    "profit_factor_nao_pior",
    "profit_factor_min_1",
    "drawdown_nao_pior",
    "drawdown_max_110pct",
    "total_r_maior",
    "expectancy_candidate_positiva",
    "sem_segmento_material_negativo",
)
# Falhas compatíveis com "faltou amostra" (IC largo, poucas afetadas, volume).
_SAMPLE_CHECKS = (
    "amostra_afetada_minima",
    "sem_aprovacao_por_unknown",
    "operacoes_min_70pct",
    "operacoes_min_110pct",
    "ci_delta_pareado_acima_de_zero",
    "ci_delta_pareado_nao_negativo",
    "ci_superior_evitadas_abaixo_de_zero",
    "ci_expectancy_acima_de_zero",
    "ci_adicionais_acima_de_zero",
)
_STAGE_PREFIXES = ("validacao_", "teste_")


def _strip_stage(check: str) -> str:
    """Remove o prefixo de estágio — `teste_` no preview é a validação reavaliada,
    então tirar o prefixo também DEDUPLICA a classificação."""
    for prefix in _STAGE_PREFIXES:
        if check.startswith(prefix):
            return check[len(prefix):]
    return check


def classify_last_failure(offline_metrics: Any) -> Dict[str, Any]:
    """Classifica a ÚLTIMA decisão usando SOMENTE os checks já persistidos.

    Nunca reavalia nada. `REFUTED_LAST_RUN` e `MIXED` não recebem ETA: mais
    quantidade, sozinha, não desfaz uma contradição econômica.
    """
    if not isinstance(offline_metrics, dict):
        return {"classification": FAILURE_NO_METADATA,
                "reason": "offline_metrics ausente ou inválido",
                "failed_checks": [], "substantive": [], "sample": []}
    checks = offline_metrics.get("checks")
    if not isinstance(checks, list) or not checks:
        return {"classification": FAILURE_NO_METADATA,
                "reason": "sem checks persistidos para classificar",
                "failed_checks": [], "substantive": [], "sample": []}

    failed = sorted({_strip_stage(str(c.get("check") or ""))
                     for c in checks if isinstance(c, dict) and not c.get("passed")})
    if not failed:
        return {"classification": FAILURE_NONE,
                "reason": "nenhum check falhou na última decisão",
                "failed_checks": [], "substantive": [], "sample": []}

    substantive = [f for f in failed if f in _SUBSTANTIVE_CHECKS]
    sample = [f for f in failed if f in _SAMPLE_CHECKS]
    unclassified = [f for f in failed if f not in _SUBSTANTIVE_CHECKS and f not in _SAMPLE_CHECKS]

    # Fail-closed: um check desconhecido pode ser substantivo. Misturá-lo com
    # checks conhecidos e ainda assim concluir "faltou apenas amostra" criaria
    # uma prontidão falsa quando o contrato de gates evoluir.
    if unclassified:
        return {"classification": FAILURE_NO_METADATA,
                "reason": f"checks não reconhecidos: {unclassified}",
                "failed_checks": failed, "substantive": substantive,
                "sample": sample, "unclassified": unclassified}
    if substantive and sample:
        classification, reason = FAILURE_MIXED, (
            "falhas de amostra E de direção econômica ao mesmo tempo — mais "
            "quantidade, sozinha, não resolve a contradição")
    elif substantive:
        classification, reason = FAILURE_REFUTED, (
            "a direção econômica da hipótese foi contrariada na última avaliação")
    else:
        classification, reason = FAILURE_SAMPLE_LIMITED, (
            "falhou apenas por amostra afetada / IC largo")
    return {"classification": classification, "reason": reason,
            "failed_checks": failed, "substantive": substantive, "sample": sample,
            "unclassified": unclassified}


def affected_verdict(row: Dict[str, Any], champion: Dict[str, Any],
                     rule: Dict[str, Any], active: Sequence[str]) -> str:
    """A regra MUDARIA o veredito desta linha?

    BLOCK        → afetada só com champion=True  e contextual=False.
    SCORE_DELTA  → afetada só com champion=False e contextual=True (e
                   `contextual_eligibility` já garante que a diferença veio
                   EXCLUSIVAMENTE do score: linha barrada por R:R, P(TP1),
                   liquidez, P04, ATR, funding, proximity, struct-chase, tempo,
                   tier, risco ou cooldown nunca é reativada).
    Qualquer UNKNOWN dos dois lados ⇒ `UNKNOWN` (não conta como afetada).
    """
    axis = rule.get("axis")
    if context_value(row, axis) != rule.get("value"):
        return AFFECTED_NO
    champ = eligibility(row, champion, active)
    cand = contextual_eligibility(row, champion, rule, active)
    if champ is None or cand is None:
        return AFFECTED_UNKNOWN
    action = rule.get("action")
    if action == "BLOCK":
        return AFFECTED_YES if (champ is True and cand is False) else AFFECTED_NO
    if action == "SCORE_DELTA":
        return AFFECTED_YES if (champ is False and cand is True) else AFFECTED_NO
    return AFFECTED_UNKNOWN


def count_affected(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                   rule: Dict[str, Any], active: Sequence[str]) -> Dict[str, Any]:
    """Contagens puras. NÃO lê `realized_r` e NÃO calcula nenhuma métrica de
    outcome — só quantas oportunidades cada lado tocaria."""
    axis = rule.get("axis")
    total = len(rows)
    champ_ok = cand_ok = affected = unknown = context_present = context_matched = 0
    for row in rows:
        context = context_value(row, axis)
        if context != CONTEXT_UNKNOWN:
            context_present += 1
        if context == rule.get("value"):
            context_matched += 1
        if eligibility(row, champion, active) is True:
            champ_ok += 1
        if contextual_eligibility(row, champion, rule, active) is True:
            cand_ok += 1
        verdict = affected_verdict(row, champion, rule, active)
        if verdict == AFFECTED_YES:
            affected += 1
        elif verdict == AFFECTED_UNKNOWN:
            unknown += 1

    oldest = newest = None
    span = None
    if rows:
        stamps = [r["resolved_at"] for r in rows]
        oldest, newest = min(stamps), max(stamps)
        span = round((newest - oldest).total_seconds() / 86400.0, 2)
    return {
        "total_resolved": total,
        "champion_eligible": champ_ok,
        "candidate_eligible": cand_ok,
        "affected": affected,
        "unknown": unknown,
        "context_present": context_present,
        "context_matched": context_matched,
        "context_coverage_pct": round(context_present / total * 100, 1) if total else None,
        "observed_span_days": span,
        "oldest_resolved_at": oldest.isoformat() if oldest else None,
        "newest_resolved_at": newest.isoformat() if newest else None,
    }


def project_prospective(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                        rule: Dict[str, Any], active: Sequence[str]) -> Dict[str, Any]:
    """Projeção 50/25/25 CRONOLÓGICA — apenas contagem de linhas.

    Reproduz a mesma proporção de `temporal_split` para responder "se a avaliação
    rodasse hoje, quantas linhas cada estágio teria?". Nenhum outcome é lido.
    """
    ordered = sorted(rows, key=lambda r: r["resolved_at"])
    n = len(ordered)
    i_train, i_valid = int(n * 0.5), int(n * 0.75)
    train, validation, test = ordered[:i_train], ordered[i_train:i_valid], ordered[i_valid:]

    def _affected(chunk):
        return sum(1 for r in chunk
                   if affected_verdict(r, champion, rule, active) == AFFECTED_YES)

    oos = sum(1 for r in test if contextual_eligibility(r, champion, rule, active) is True)
    return {
        "prospective_train_affected": _affected(train),
        "prospective_validation_affected": _affected(validation),
        "prospective_test_affected": _affected(test),
        "prospective_candidate_oos_count": oos,
        # O avaliador P05.1 gera candidatos exclusivamente no treino. Medir a
        # cobertura na janela global pode anunciar READY para uma hipótese que
        # será imediatamente rejeitada por AXIS_COVERAGE_TOO_LOW.
        "prospective_train_context_coverage_pct": axis_coverage(
            train, rule.get("axis")),
        "split_counts": {"train": len(train), "validation": len(validation), "test": len(test)},
        "note": "somente contagem de linhas; nenhum outcome do holdout foi lido",
    }


def estimate_eta(*, affected_since_cutoff: int, observed_days: Optional[float],
                 missing_to_minimum: int, classification: str) -> Dict[str, Any]:
    """ETA conservadora. Nunca promete data exata; hipótese contrariada não
    recebe ETA (mais dado não desfaz contradição)."""
    if classification in (FAILURE_REFUTED, FAILURE_MIXED):
        return {"daily_rate": None, "eta_days": None,
                "eta_reason": ("hipótese contrariada na última avaliação — quantidade "
                               "sozinha não é critério suficiente")}
    if observed_days is None or observed_days < P051R_MIN_OBSERVED_DAYS_FOR_RATE:
        return {"daily_rate": None, "eta_days": None,
                "eta_reason": (f"menos de {P051R_MIN_OBSERVED_DAYS_FOR_RATE} dias observados "
                               f"desde o cutoff — taxa não é confiável")}
    rate = affected_since_cutoff / observed_days
    if rate <= 0:
        return {"daily_rate": 0.0, "eta_days": None,
                "eta_reason": "nenhuma nova oportunidade afetada desde o cutoff"}
    if missing_to_minimum <= 0:
        return {"daily_rate": round(rate, 4), "eta_days": 0,
                "eta_reason": "mínimo de amostra já atingido"}
    # Só ~25% das novas linhas caem na fatia de validação/teste — divisor
    # conservador para não prometer prazo curto demais.
    into_slice = rate * 0.25
    eta = math.ceil(missing_to_minimum / into_slice) if into_slice > 0 else None
    return {"daily_rate": round(rate, 4), "eta_days": eta,
            "eta_reason": ("estimativa conservadora: assume que ~25% das novas "
                           "oportunidades afetadas caem na fatia de validação/teste; "
                           "não é data garantida")}


async def _load_readiness_rows(days: int) -> List[Dict[str, Any]]:
    """Linhas resolvidas para CONTAGEM. Seleciona colunas EXPLÍCITAS e **nunca**
    inclui `realized_r` — o holdout permanece selado por construção."""
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot as RS
    from services import calibration_service as _calib
    from sqlalchemy import select
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    async with get_session() as session:
        result = await session.execute(
            select(RS.id, RS.symbol, RS.timeframe, RS.tier, RS.direction,
                   RS.score, RS.features, RS.created_at, RS.outcome_at)
            .where(RS.status.in_(SNAP_RESOLVED))
            .where(RS.outcome_at.is_not(None))
            .where(RS.outcome_at >= since)
            .where(_calib._not_fast_void())
        )
        for row in result.all():
            if row.id in seen:
                continue
            seen.add(row.id)
            resolved_at = _utc(row.outcome_at)
            if resolved_at is None:
                continue
            out.append({
                "id": row.id,                 # P05.2L: ligação com RealTrade
                "dedupe_key": f"snap:{row.id}",
                "symbol": row.symbol, "timeframe": row.timeframe, "tier": row.tier,
                "direction": row.direction, "score": row.score,
                "features": _merged_features(row.features),
                "created_at": _utc(row.created_at),
                "resolved_at": resolved_at,
                # NOTA: `realized_r` deliberadamente AUSENTE deste dicionário.
            })
    out.sort(key=lambda r: r["resolved_at"])
    return out


async def _retention_snapshot(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Retenção observada + política detectada.

    Histórico curto NÃO é prova de deleção. As rotinas de poda encontradas em
    `snapshot_service` apagam SOMENTE o namespace `wide` (`status == "wide"` e
    `wide_*`), disjunto dos status resolvidos que o P05 usa — logo não removem
    evidência do P05. As constantes são lidas por CONTRATO DE ENV (o módulo não é
    importado: ele carrega `push_service`, que faz rede).
    """
    now = datetime.now(timezone.utc)
    buckets = {}
    for d in (30, 60, 90, 120):
        cutoff = now - timedelta(days=d)
        buckets[f"rows_older_than_{d}d"] = sum(1 for r in rows if r["resolved_at"] < cutoff)

    oldest = min((r["resolved_at"] for r in rows), default=None)
    observed = round((now - oldest).total_seconds() / 86400.0, 1) if oldest else None

    wide_tracking = _env_bool("WIDE_TRACKING_ENABLED", "false")
    expiry_hours = _env_float("SNAPSHOT_EXPIRY_HOURS", 168.0)
    wide_ttl_hours = _env_float("WIDE_DISPLAY_TTL_HOURS", 6.0)

    policy = {
        "detected": "wide_namespace_prune_only",
        "applies_to_p05_statuses": False,
        "p05_statuses": list(SNAP_RESOLVED),
        "pruned_statuses": ["wide", "wide_*"],
        "wide_tracking_enabled": wide_tracking,
        "wide_prune_hours": expiry_hours if wide_tracking else wide_ttl_hours,
        "evidence": ("snapshot_service poda apenas status 'wide'/'wide_*'; "
                     "os status resolvidos do P05 são disjuntos e não são apagados"),
    }
    # Só há risco se existir política que efetivamente apague linhas do P05.
    if policy["applies_to_p05_statuses"]:
        policy_days = policy["wide_prune_hours"] / 24.0
        at_risk = policy_days < P051R_TARGET_RETENTION_DAYS
        warning = (f"política de retenção de {policy_days:.0f} dias < "
                   f"{P051R_TARGET_RETENTION_DAYS} dias alvo") if at_risk else None
    else:
        policy_days, at_risk, warning = None, False, None

    if observed is None:
        history = "UNKNOWN"
        history_note = "sem linhas resolvidas na janela consultada"
    elif observed < P051R_TARGET_RETENTION_DAYS:
        history = "YOUNG_HISTORY"
        history_note = ("histórico ainda jovem — NÃO é evidência de deleção; "
                        "nenhuma política apaga os status do P05")
    else:
        history = "MATURE"
        history_note = f"histórico cobre ≥ {P051R_TARGET_RETENTION_DAYS} dias"

    return {
        **buckets,
        "observed_retention_days": observed,
        "oldest_resolved_at": oldest.isoformat() if oldest else None,
        "retention_policy_detected": policy["detected"],
        "retention_policy_days": policy_days,
        "retention_policy_applies_to_p05": policy["applies_to_p05_statuses"],
        "retention_at_risk": at_risk,
        "retention_warning": warning,
        "history_status": history,
        "history_note": history_note,
        "policy_detail": policy,
    }


def _readiness_verdict(*, classification: str, coverage_pct: Optional[float],
                       prospective: Dict[str, Any], unknown_pct: Optional[float],
                       retention_at_risk: bool,
                       retention_known: bool = True) -> Dict[str, Any]:
    """`READY_FOR_REEVALUATION` significa APENAS "há amostra suficiente para
    rodar a avaliação de novo" — nunca aprovação, Shadow ou promoção."""
    blockers: List[str] = []
    if not retention_known:
        return {"readiness": READINESS_NO_METADATA,
                "blockers": ["auditoria de retenção indisponível"],
                "means": "não é seguro declarar prontidão sem validar a retenção"}
    if retention_at_risk:
        return {"readiness": READINESS_RETENTION_AT_RISK,
                "blockers": ["retenção insuficiente para a janela alvo"],
                "means": "dado pode sumir antes de completar a janela"}
    if classification == FAILURE_NO_METADATA:
        return {"readiness": READINESS_NO_METADATA,
                "blockers": ["metadados antigos não permitem classificar a última falha"],
                "means": "não é possível dizer se falta amostra"}
    if classification == FAILURE_REFUTED:
        return {"readiness": READINESS_REFUTED,
                "blockers": ["direção econômica contrariada na última avaliação"],
                "means": "mais amostra não desfaz a contradição"}
    if classification == FAILURE_MIXED:
        return {"readiness": READINESS_MIXED,
                "blockers": ["falhas de amostra E substantivas simultâneas"],
                "means": "quantidade sozinha não resolve a contradição"}
    if classification == FAILURE_NONE:
        return {"readiness": READINESS_UNKNOWN,
                "blockers": ["última avaliação não registrou falhas"],
                "means": "nada a reavaliar por falta de amostra"}

    # A partir daqui: SAMPLE_LIMITED
    if coverage_pct is None or coverage_pct < P05_MIN_FEATURE_COVERAGE_PCT:
        blockers.append(
            f"cobertura do contexto no treino {coverage_pct}% "
            f"< {P05_MIN_FEATURE_COVERAGE_PCT}%")
    if unknown_pct is not None and unknown_pct > P051R_MAX_UNKNOWN_PCT:
        blockers.append(f"UNKNOWN {unknown_pct}% compromete a cobertura")
    if (prospective.get("prospective_validation_affected") or 0) < P051_MIN_AFFECTED:
        blockers.append(f"validação afetada {prospective.get('prospective_validation_affected')} "
                        f"< {P051_MIN_AFFECTED}")
    if (prospective.get("prospective_test_affected") or 0) < P051_MIN_AFFECTED:
        blockers.append(f"teste afetado {prospective.get('prospective_test_affected')} "
                        f"< {P051_MIN_AFFECTED}")
    if (prospective.get("prospective_candidate_oos_count") or 0) < P05_MIN_OOS_RESOLVED:
        blockers.append(f"OOS do candidato {prospective.get('prospective_candidate_oos_count')} "
                        f"< {P05_MIN_OOS_RESOLVED}")
    if blockers:
        return {"readiness": READINESS_WAITING, "blockers": blockers,
                "means": "ainda não há amostra suficiente para reavaliar"}
    return {
        "readiness": READINESS_READY, "blockers": [],
        "means": "há amostra suficiente para EXECUTAR a avaliação novamente",
        "does_not_mean": ["candidato aprovado", "Shadow autorizado",
                          "estratégia pronta", "ganho esperado", "promoção"],
    }


async def _readiness_for_experiment(exp: Any, rows: Sequence[Dict[str, Any]],
                                    current_champion: Dict[str, Any],
                                    retention: Dict[str, Any],
                                    window_days: int = 120) -> Dict[str, Any]:
    """Prontidão de UM experimento congelado. Não altera nada."""
    config = exp.candidate_config if isinstance(exp.candidate_config, dict) else {}
    raw_rule = config.get("CONTEXT_RULE")
    try:
        rule = validate_context_rule(raw_rule)
    except CandidateValidationError as exc:
        return {"experiment_id": exp.id, "readiness": READINESS_NO_METADATA,
                "error": f"CONTEXT_RULE inválida: {exc}"}

    offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
    failure = classify_last_failure(offline)

    # Reproduz SOMENTE o contrato congelado. Usar o champion atual ou reconstruir
    # componentes ausentes mudaria a hipótese depois de olhar dados novos.
    champion = _frozen_champion(exp)
    active = offline.get("active_components")
    cutoff = _utc(exp.dataset_cutoff)
    metadata_errors: List[str] = []
    if champion is None:
        metadata_errors.append("champion congelado ausente ou hash divergente")
    if (not isinstance(active, list) or not active
            or any(not isinstance(c, str) for c in active)
            or len(active) != len(set(active))
            or any(c not in _COMPONENT_FEATURES for c in active)):
        metadata_errors.append("active_components congelados ausentes ou inválidos")
    if cutoff is None:
        metadata_errors.append("dataset_cutoff ausente ou inválido")

    champion_drift = (champion is not None
                      and canonical_hash(current_champion) != exp.champion_hash)
    if metadata_errors:
        return {
            "experiment_id": exp.id, "objective": exp.objective,
            "axis": rule["axis"], "value": rule["value"],
            "action": rule["action"], "score_delta": rule.get("score_delta"),
            "status": exp.status, "readiness": READINESS_NO_METADATA,
            "blockers": metadata_errors,
            "means": "o experimento não pode ser reproduzido com segurança",
            "failure_classification": failure["classification"],
            "failed_checks": failure["failed_checks"],
            "active_components": active if isinstance(active, list) else None,
            "active_components_source": "indisponível",
            "champion_drift": champion_drift,
            "holdout_outcomes_read": False, "holdout_metrics_computed": False,
            "holdout_status": HOLDOUT_SEALED,
        }

    active_source = "congelado na avaliação original"
    current = count_affected(rows, champion, rule, active)
    prospective = project_prospective(rows, champion, rule, active)

    since_rows = [r for r in rows if cutoff is None or r["resolved_at"] > cutoff]
    since = count_affected(since_rows, champion, rule, active)
    now = datetime.now(timezone.utc)
    # A taxa só pode usar o período efetivamente carregado. Se o cutoff for mais
    # antigo que a janela solicitada, não divide eventos de 120d por toda a idade
    # do experimento (o que inflaria artificialmente a ETA).
    window_start = now - timedelta(days=max(30, int(window_days)))
    observation_start = max(cutoff, window_start)
    observed_days = max(0.0, (now - observation_start).total_seconds() / 86400.0)

    worst_slice = min(prospective["prospective_validation_affected"],
                      prospective["prospective_test_affected"])
    missing = max(0, P051_MIN_AFFECTED - worst_slice)
    eta = estimate_eta(affected_since_cutoff=since["affected"],
                       observed_days=observed_days,
                       missing_to_minimum=missing,
                       classification=failure["classification"])

    # UNKNOWN só existe dentro do contexto-alvo; usar o total global diluiria a
    # falta de qualidade quando o segmento é pequeno.
    context_matched = current["context_matched"] or 0
    unknown_pct = (round(current["unknown"] / context_matched * 100, 1)
                   if context_matched else None)
    verdict = _readiness_verdict(
        classification=failure["classification"],
        # Paridade com o avaliador contextual: geração e seleção começam na
        # metade cronológica de TREINO, não na janela global.
        coverage_pct=prospective["prospective_train_context_coverage_pct"],
        prospective=prospective,
        unknown_pct=unknown_pct,
        retention_at_risk=bool(retention.get("retention_at_risk")),
        retention_known=not bool(retention.get("error")),
    )
    if champion_drift:
        verdict = {
            "readiness": READINESS_UNKNOWN,
            "blockers": ["champion atual diverge do champion congelado"],
            "means": "gere uma nova hipótese contra o champion atual antes de reavaliar",
        }

    baseline_validation_affected = None
    val = offline.get("validation")
    if isinstance(val, dict):
        baseline_validation_affected = val.get("affected_ops")

    decision = exp.decision if isinstance(exp.decision, dict) else {}
    return {
        "experiment_id": exp.id,
        "objective": exp.objective,
        "axis": rule["axis"],
        "value": rule["value"],
        "action": rule["action"],
        "score_delta": rule.get("score_delta"),
        "dataset_cutoff": cutoff.isoformat() if cutoff else None,
        "status": exp.status,
        "last_decision_reason": decision.get("reason_code") or offline.get("reason_code"),
        "failure_classification": failure["classification"],
        "failure_reason": failure["reason"],
        "failed_checks": failure["failed_checks"],
        "substantive_failures": failure.get("substantive"),
        "sample_failures": failure.get("sample"),
        "baseline_validation_affected": baseline_validation_affected,
        "new_since_cutoff": {
            "total_resolved": since["total_resolved"],
            "affected": since["affected"],
            "unknown": since["unknown"],
            "observed_days": round(observed_days, 1) if observed_days is not None else None,
        },
        "current_window": current,
        "prospective": prospective,
        "unknown_pct": unknown_pct,
        "missing_to_minimum": missing,
        "minimum_affected": P051_MIN_AFFECTED,
        "active_components": active,
        "active_components_source": active_source,
        "champion_drift": champion_drift,
        **eta,
        **verdict,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "holdout_status": HOLDOUT_SEALED,
    }


_READINESS_CACHE: Dict[Tuple[int, int, Optional[int]], Tuple[float, Dict[str, Any]]] = {}
_READINESS_CACHE_LOCK = asyncio.Lock()


async def _build_readiness(days: int, limit: int,
                           experiment_id: Optional[int]) -> Dict[str, Any]:
    """Monta o relatório. Fail-soft por seção; ZERO escrita."""
    out: Dict[str, Any] = {
        "phase": P051R_PHASE,
        "read_only": True,
        "holdout_status": HOLDOUT_SEALED,
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "window_days": days,
        "champion": discover_champion_config(),
        "live_untouched": True,
        "note": ("monitor SOMENTE LEITURA: informa se há amostra para reavaliar, "
                 "nunca se um candidato é bom"),
    }
    try:
        rows = await _load_readiness_rows(days)
    except Exception as exc:                                   # fail-soft
        log.warning(f"[p05.1r] leitura de snapshots falhou: {exc}")
        return {**out, "error": str(exc), "summary": {}, "retention": {}, "experiments": []}

    try:
        retention = await _retention_snapshot(rows)
    except Exception as exc:
        log.warning(f"[p05.1r] retenção falhou: {exc}")
        # Fail-closed: uma falha de auditoria de retenção nunca pode resultar em
        # READY_FOR_REEVALUATION.
        retention = {"error": str(exc), "retention_at_risk": False,
                     "history_status": "UNKNOWN"}

    experiments: List[Dict[str, Any]] = []
    try:
        from db import get_session
        from models.strategy_experiment import StrategyExperiment as E
        from sqlalchemy import select
        async with get_session() as session:
            stmt = (select(E)
                    .where(E.candidate_config.has_key("CONTEXT_RULE"))   # noqa: W601
                    .where(E.status.in_((STATUS_REJECTED, STATUS_INSUFFICIENT,
                                         STATUS_OFFLINE_VALIDATED)))
                    .order_by(E.created_at.desc()))
            if experiment_id is not None:
                stmt = stmt.where(E.id == experiment_id)
            found = (await session.execute(stmt.limit(limit))).scalars().all()
            champion = out["champion"]
            for exp in found:
                offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
                # Só monitora o que é declaradamente P05.1 ANALYTICS_ONLY.
                if not (offline.get("phase") == P051_PHASE
                        and offline.get("execution_mode") == P051_EXECUTION_MODE
                        and offline.get("promotable") is False
                        and offline.get("shadow_supported") is False
                        and is_contextual_experiment(exp)):
                    continue
                try:
                    experiments.append(
                        await _readiness_for_experiment(
                            exp, rows, champion, retention, window_days=days))
                except Exception as exc:                       # fail-soft por item
                    log.warning(f"[p05.1r] experimento {exp.id} falhou: {exc}")
                    experiments.append({"experiment_id": exp.id,
                                        "readiness": READINESS_UNKNOWN,
                                        "error": str(exc)})
    except Exception as exc:
        log.warning(f"[p05.1r] leitura de experimentos falhou: {exc}")
        out["experiments_error"] = str(exc)

    by_status: Dict[str, int] = {}
    for item in experiments:
        key = item.get("readiness") or READINESS_UNKNOWN
        by_status[key] = by_status.get(key, 0) + 1
    etas = [i["eta_days"] for i in experiments
            if isinstance(i.get("eta_days"), int) and i["eta_days"] > 0]

    out["summary"] = {
        "monitored": len(experiments),
        "readiness_by_status": by_status,
        "next_recommended_check_days": min(etas) if etas else None,
        "next_recommended_check_reason": (
            "menor ETA entre hipóteses limitadas por amostra" if etas else
            "nenhuma hipótese com ETA — refutadas/mistas não recebem prazo"),
        "total_resolved_in_window": len(rows),
        "minimum_affected": P051_MIN_AFFECTED,
        "min_oos_resolved": P05_MIN_OOS_RESOLVED,
        "min_feature_coverage_pct": P05_MIN_FEATURE_COVERAGE_PCT,
    }
    out["retention"] = retention
    out["experiments"] = experiments
    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    return out


async def get_readiness(days: int = 120, limit: int = 20,
                        experiment_id: Optional[int] = None) -> Dict[str, Any]:
    """Cache single-flight curto, chaveado por (days, limit, experiment_id).
    Falha NÃO envenena o cache."""
    days = max(30, min(int(days), 365))
    limit = max(1, min(int(limit), 100))
    key = (days, limit, experiment_id)
    now_mono = time.monotonic()
    cached = _READINESS_CACHE.get(key)
    if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
        return cached[1]
    async with _READINESS_CACHE_LOCK:
        cached = _READINESS_CACHE.get(key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < P05_DIAG_CACHE_TTL_S:
            return cached[1]
        value = await _build_readiness(days, limit, experiment_id)
        has_partial_error = (
            bool(value.get("experiments_error"))
            or bool((value.get("retention") or {}).get("error"))
            or any(bool(item.get("error")) for item in (value.get("experiments") or []))
        )
        if not value.get("error") and not has_partial_error:  # erro não entra no cache
            _READINESS_CACHE[key] = (now_mono, value)
            if len(_READINESS_CACHE) > 12:
                oldest = min(_READINESS_CACHE, key=lambda k: _READINESS_CACHE[k][0])
                _READINESS_CACHE.pop(oldest, None)
        return value


# ════════════════════════════════════════════════════════════════════════════
#  P05C — champion × challenger em shadow
# ════════════════════════════════════════════════════════════════════════════
def build_experiment_annotation(row: Dict[str, Any], champion: Dict[str, Any],
                                candidate_cfg: Dict[str, Any], *, experiment_key: str,
                                candidate_hash: str, active: Sequence[str],
                                champion_hash: Optional[str] = None) -> Dict[str, Any]:
    """Anotação contrafactual de UM snapshot. Não altera score/tier/execução."""
    merged = dict(champion)
    merged.update(candidate_cfg)
    champ_ok = eligibility(row, champion, active)
    chall_ok = eligibility(row, merged, active)
    if chall_ok is None:
        status, reason = CHALLENGER_UNKNOWN, "FEATURE_AUSENTE"
    elif chall_ok:
        status, reason = CHALLENGER_ELIGIBLE, "PASSOU_NOS_GATES"
    else:
        status, reason = CHALLENGER_BLOCKED, "BLOQUEADO_PELO_KNOB"
    feats = row.get("features") or {}
    required = sorted({_COMPONENT_FEATURES[c] for c in active} - {"__score__", "__tier__"})
    observed = _utc(row.get("created_at")) or datetime.now(timezone.utc)
    return {
        "experiment_key": experiment_key,
        "candidate_hash": candidate_hash,
        "champion_hash": champion_hash or canonical_hash(champion),
        "champion_eligible": champ_ok,
        "challenger_eligible": chall_ok,
        "challenger_status": status,
        "reason_code": reason,
        "evaluated_at": observed.isoformat(),
        "required_features": required,
        "missing_features": [f for f in required if feats.get(f) is None],
        "schema_version": FEATURES_SCHEMA_VERSION,
    }


def _frozen_champion(exp: Any) -> Optional[Dict[str, Any]]:
    offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
    frozen = offline.get("champion_config")
    if not isinstance(frozen, dict) or canonical_hash(frozen) != exp.champion_hash:
        return None
    return frozen


async def get_active_shadow_context(session) -> Optional[Dict[str, Any]]:
    """Carrega UMA vez por batch o experimento prospectivo ativo.

    Flag OFF, cardinalidade inválida, champion sem snapshot ou drift deixam o
    fluxo sem anotação (fail-closed). Nunca toca execução/ordens.
    """
    if not (P05_ANALYTICS_ENABLED and P05_CHALLENGER_SHADOW_ENABLED):
        return None
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select
    rows = (await session.execute(
        select(E).where(E.status == STATUS_SHADOW).order_by(E.id).limit(2)
    )).scalars().all()
    if len(rows) != 1:
        if len(rows) > 1:
            log.error("[p05] cardinalidade inválida: mais de um SHADOW ativo")
        return None
    exp = rows[0]
    champion = _frozen_champion(exp)
    if champion is None or canonical_hash(discover_champion_config()) != exp.champion_hash:
        log.error("[p05] champion ausente ou mudou; snapshot não recebe contrafactual")
        return None
    shadow_metrics = exp.shadow_metrics if isinstance(exp.shadow_metrics, dict) else {}
    start_guard = shadow_metrics.get("start_guard")
    start_safety = start_guard.get("safety") if isinstance(start_guard, dict) else None
    if (not isinstance(start_safety, dict)
            or start_safety.get("fingerprint") != safety_guard()["fingerprint"]):
        log.error("[p05] proteções mudaram ou não foram congeladas; snapshot sem contrafactual")
        return None
    offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
    active = offline.get("active_components")
    if not isinstance(active, list) or not active:
        log.error("[p05] experimento SHADOW sem active_components congelados")
        return None
    return {
        "experiment_key": exp.experiment_key,
        "candidate_hash": exp.candidate_hash,
        "champion_hash": exp.champion_hash,
        "champion": champion,
        "candidate_config": dict(exp.candidate_config or {}),
        "active_components": list(active),
        "shadow_started_at": _utc(exp.shadow_started_at),
    }


def annotate_snapshot_features(features: Dict[str, Any], row: Dict[str, Any],
                               context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mescla `p05_experiment` sem sobrescrever outra chave/histórico.

    Repetir o save lógico com a mesma experiment_key devolve exatamente a
    anotação original (inclusive timestamp), garantindo idempotência real.
    """
    out = dict(features or {})
    if not context:
        return out
    existing = out.get("p05_experiment")
    if isinstance(existing, dict):
        if existing.get("experiment_key") == context.get("experiment_key"):
            return out
        log.error("[p05] snapshot já pertence a outro experimento; não sobrescrito")
        return out
    created = _utc(row.get("created_at"))
    started = _utc(context.get("shadow_started_at"))
    if created is None or started is None or created < started:
        return out
    enriched = dict(row)
    enriched["features"] = out
    out["p05_experiment"] = build_experiment_annotation(
        enriched,
        context["champion"],
        context["candidate_config"],
        experiment_key=context["experiment_key"],
        candidate_hash=context["candidate_hash"],
        champion_hash=context["champion_hash"],
        active=context["active_components"],
    )
    return out


def compute_shadow_metrics(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                           candidate_cfg: Dict[str, Any], objective: str,
                           active: Sequence[str], *, started_at: datetime,
                           experiment_key: Optional[str] = None,
                           candidate_hash: Optional[str] = None,
                           champion_hash: Optional[str] = None,
                           require_annotations: bool = False,
                           operational_incident: Optional[bool] = None,
                           safety_relaxed: Optional[bool] = None) -> Dict[str, Any]:
    """Relatório do shadow sobre a MESMA recomendação.

    Em produção (`require_annotations=True`) usa exclusivamente os vereditos
    congelados na criação do snapshot. O modo recalculado fica apenas para
    funções puras/testes históricos e nunca decide P05D.
    """
    merged = dict(champion)
    merged.update(candidate_cfg)
    evaluable: List[Dict[str, Any]] = []
    champ_rows: List[Dict[str, Any]] = []
    cand_rows: List[Dict[str, Any]] = []
    annotation_matches = 0
    unknown = 0
    if require_annotations:
        for r in rows:
            ann = (r.get("features") or {}).get("p05_experiment")
            exact = (
                isinstance(ann, dict)
                and ann.get("experiment_key") == experiment_key
                and ann.get("candidate_hash") == candidate_hash
                and ann.get("champion_hash") == champion_hash
            )
            if not exact:
                unknown += 1
                continue
            annotation_matches += 1
            cv = ann.get("champion_eligible")
            nv = ann.get("challenger_eligible")
            if not isinstance(cv, bool) or not isinstance(nv, bool):
                unknown += 1
                continue
            evaluable.append(r)
            if cv:
                champ_rows.append(r)
            if nv:
                cand_rows.append(r)
    else:
        for r in rows:
            if eligibility(r, champion, active) is None or eligibility(r, merged, active) is None:
                unknown += 1
            else:
                evaluable.append(r)
        champ_rows, _ = split_by_config(evaluable, champion, active)
        cand_rows, _ = split_by_config(evaluable, merged, active)

    champ_m = compute_evidence_metrics(champ_rows)
    cand_m = compute_evidence_metrics(cand_rows)
    champ_ids = {id(r) for r in champ_rows}
    cand_ids = {id(r) for r in cand_rows}
    added = [r for r in cand_rows if id(r) not in champ_ids]
    avoided = [r for r in champ_rows if id(r) not in cand_ids]

    total = len(rows)
    coverage = round(len(evaluable) / total * 100, 1) if total else None
    days = 0
    if rows:
        last = max(r["resolved_at"] for r in rows)
        days = max(0, int((last - _utc(started_at)).total_seconds() // 86400))

    if objective == OBJECTIVE_LOSS_REDUCTION:
        still_met = ((cand_m["expectancy_r"] or 0) > (champ_m["expectancy_r"] or 0)
                     and champ_m["count"] > 0
                     and cand_m["count"] >= 0.7 * champ_m["count"])
    else:
        still_met = (champ_m["count"] > 0 and cand_m["count"] >= 1.1 * champ_m["count"]
                     and (cand_m["sum_r"] or 0) > (champ_m["sum_r"] or 0))

    return {
        "resolved": total,
        "challenger_resolved": cand_m["count"],
        "observed_days": days,
        "coverage_pct": coverage,
        "annotation_coverage_pct": (
            round(annotation_matches / total * 100, 1) if total and require_annotations else None
        ),
        "unknown_excluded": unknown,
        "champion": champ_m,
        "candidate": cand_m,
        "added_ops": len(added),
        "avoided_ops": len(avoided),
        "delta_expectancy_ci": bootstrap_paired_membership_delta_ci(
            evaluable, champ_ids, cand_ids),
        "objective": objective,
        "objective_still_met": bool(still_met),
        "operational_incident": operational_incident,
        "safety_relaxed": safety_relaxed,
        "evidence_mode": "prospective-annotation" if require_annotations else "recomputed-test-only",
        "note": "challenger é CONTRAFACTUAL: não altera score/tier, não bloqueia rec, não abre trade",
    }


# ════════════════════════════════════════════════════════════════════════════
#  Persistência / consultas de experimentos
# ════════════════════════════════════════════════════════════════════════════
async def list_experiments(limit: int = 20, offset: int = 0,
                           status: Optional[str] = None) -> Dict[str, Any]:
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select, func
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    async with get_session() as session:
        stmt = select(E)
        if status:
            stmt = stmt.where(E.status == status)
        total = (await session.execute(
            select(func.count(E.id)).where(E.status == status) if status
            else select(func.count(E.id))
        )).scalar() or 0
        rows = (await session.execute(
            stmt.order_by(E.created_at.desc()).limit(limit).offset(offset)
        )).scalars().all()
        return {"total": int(total), "limit": limit, "offset": offset,
                "items": [r.to_dict(full=False) for r in rows]}


async def get_experiment(exp_id: int) -> Optional[Dict[str, Any]]:
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select
    async with get_session() as session:
        exp = (await session.execute(select(E).where(E.id == exp_id))).scalar_one_or_none()
        return exp.to_dict(full=True) if exp else None


async def start_shadow(exp_id: int) -> Dict[str, Any]:
    """OFFLINE_VALIDATED → SHADOW com baseline/safety congelados."""
    if not (P05_ANALYTICS_ENABLED and P05_CHALLENGER_SHADOW_ENABLED):
        return {"ok": False, "blocked": True, "reason_code": "P05_SHADOW_DISABLED",
                "error": "P05_CHALLENGER_SHADOW_ENABLED=false"}
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    try:
        async with get_session() as session:
            # Ordem global: P05 → P03. O P03 não adquire lock P05, portanto não
            # há ciclo; a contagem e a transição ficam serializadas com incidentes.
            await _acquire_p05_lock(session, _P05_SHADOW_LOCK_KEY)
            await _acquire_p05_lock(session, _P03_SAFETY_LOCK_KEY)
            exp = (await session.execute(
                select(E).where(E.id == exp_id).with_for_update()
            )).scalar_one_or_none()
            if exp is None:
                return {"ok": False, "error": "experimento não encontrado"}
            # P05.1 é ANALYTICS_ONLY: um resultado analítico nunca vira challenger.
            if is_contextual_experiment(exp):
                return {"ok": False, "blocked": True,
                        "reason_code": P051_BLOCK_REASON,
                        "error": ("experimento P05.1 (regra contextual) é somente análise "
                                  "offline — requer uma fase posterior de integração do "
                                  "contexto ao executor. Não é aplicável ao LIVE."),
                        "status": exp.status}
            if exp.status == STATUS_SHADOW:
                return {"ok": True, "idempotent": True,
                        "experiment": exp.to_dict(full=False)}
            if not can_transition(exp.status, STATUS_SHADOW):
                return {"ok": False,
                        "error": f"transição inválida {exp.status} → {STATUS_SHADOW}",
                        "status": exp.status}

            champion = _frozen_champion(exp)
            if champion is None:
                return {"ok": False, "blocked": True,
                        "reason_code": "FROZEN_CHAMPION_MISSING",
                        "error": "baseline imutável do champion ausente ou inválida"}
            if canonical_hash(discover_champion_config()) != exp.champion_hash:
                return {"ok": False, "blocked": True,
                        "reason_code": "CHAMPION_DRIFT",
                        "error": "champion atual difere do avaliado offline"}
            offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
            if not isinstance(offline.get("active_components"), list):
                return {"ok": False, "blocked": True,
                        "reason_code": "ACTIVE_COMPONENTS_MISSING",
                        "error": "componentes avaliados não foram congelados"}

            open_incidents = await _open_incident_count(session)
            if open_incidents:
                return {"ok": False, "blocked": True,
                        "reason_code": "OPEN_EXECUTION_INCIDENT",
                        "error": f"há {open_incidents} incidente(s) de execução aberto(s)"}
            active = (await session.execute(
                select(E).where(E.status == STATUS_SHADOW).with_for_update()
            )).scalars().first()
            if active is not None and active.id != exp.id:
                return {"ok": False, "error": "já existe um experimento SHADOW ativo",
                        "conflict_experiment_id": active.id, "conflict": True}

            now = datetime.now(timezone.utc)
            exp.status = STATUS_SHADOW
            exp.shadow_started_at = now
            exp.shadow_metrics = {
                "evidence_mode": "prospective-annotation",
                "start_guard": {
                    "started_at": now.isoformat(),
                    "champion_hash": exp.champion_hash,
                    "safety": safety_guard(),
                    "open_incidents": 0,
                },
            }
            await session.commit()
            return {"ok": True, "idempotent": False,
                    "experiment": exp.to_dict(full=False)}
    except IntegrityError:
        return {"ok": False, "conflict": True,
                "error": "outro experimento SHADOW foi iniciado simultaneamente"}


async def evaluate_shadow(exp_id: int) -> Dict[str, Any]:
    """P05D — decide REJECTED/ELIGIBLE ou segue aguardando amostra."""
    if not (P05_ANALYTICS_ENABLED and P05_CHALLENGER_SHADOW_ENABLED):
        return {"ok": False, "blocked": True, "reason_code": "P05_SHADOW_DISABLED",
                "error": "P05_CHALLENGER_SHADOW_ENABLED=false"}
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select

    # 1) Congela os insumos imutáveis. Nenhuma classificação retrospectiva:
    # somente snapshots criados depois do start e anotados naquele momento.
    async with get_session() as session:
        await _acquire_p05_lock(session, _P05_SHADOW_LOCK_KEY)
        exp = (await session.execute(
            select(E).where(E.id == exp_id).with_for_update()
        )).scalar_one_or_none()
        if exp is None:
            return {"ok": False, "error": "experimento não encontrado"}
        # P05.1 é ANALYTICS_ONLY: nunca há shadow para avaliar.
        if is_contextual_experiment(exp):
            return {"ok": False, "blocked": True,
                    "reason_code": P051_BLOCK_REASON,
                    "error": ("experimento P05.1 (regra contextual) é somente análise "
                              "offline — requer uma fase posterior de integração do "
                              "contexto ao executor. Não é aplicável ao LIVE."),
                    "status": exp.status}
        if exp.status != STATUS_SHADOW:
            return {"ok": False, "error": f"experimento não está em SHADOW (status={exp.status})",
                    "status": exp.status}
        started = _utc(exp.shadow_started_at) or _utc(exp.created_at)
        cfg = dict(exp.candidate_config or {})
        champion = _frozen_champion(exp)
        offline = exp.offline_metrics if isinstance(exp.offline_metrics, dict) else {}
        active = offline.get("active_components")
        if not isinstance(active, list):
            active = []
        previous_shadow = exp.shadow_metrics if isinstance(exp.shadow_metrics, dict) else {}
        start_guard = previous_shadow.get("start_guard")
        experiment_key = exp.experiment_key
        candidate_hash = exp.candidate_hash
        champion_hash = exp.champion_hash
        objective = exp.objective

    days = max(1, min(365, int((datetime.now(timezone.utc) - started).days) + 1))
    rows, _dq = await _load_shadow(days)
    rows = [r for r in rows
            if _utc(r.get("created_at")) is not None and _utc(r.get("created_at")) >= started]
    integrity_ok = champion is not None and bool(active) and isinstance(start_guard, dict)
    metrics = compute_shadow_metrics(
        rows if integrity_ok else [], champion or {}, cfg, objective, active,
        started_at=started, experiment_key=experiment_key,
        candidate_hash=candidate_hash, champion_hash=champion_hash,
        require_annotations=True, operational_incident=None, safety_relaxed=None,
    )

    # 2) Decide sob os locks P05+P03. O count de incidentes, o fingerprint e a
    # atualização de estado pertencem à mesma transação; evita TOCTOU.
    async with get_session() as session:
        await _acquire_p05_lock(session, _P05_SHADOW_LOCK_KEY)
        await _acquire_p05_lock(session, _P03_SAFETY_LOCK_KEY)
        exp = (await session.execute(
            select(E).where(E.id == exp_id).with_for_update()
        )).scalar_one_or_none()
        if exp is None or exp.status != STATUS_SHADOW:
            return {"ok": False, "error": "experimento mudou de estado durante a avaliação"}
        if (exp.experiment_key != experiment_key or exp.candidate_hash != candidate_hash
                or exp.champion_hash != champion_hash):
            return {"ok": False, "error": "identidade imutável do experimento mudou"}

        incident_count = await _incident_since_count(session, started)
        current_safety = safety_guard()
        start_safety = start_guard.get("safety") if isinstance(start_guard, dict) else None
        start_fingerprint = (start_safety.get("fingerprint")
                             if isinstance(start_safety, dict) else None)
        champion_drift = canonical_hash(discover_champion_config()) != champion_hash
        safety_relaxed: Optional[bool]
        if not integrity_ok or not start_fingerprint:
            safety_relaxed = None
        else:
            safety_relaxed = bool(
                current_safety["fingerprint"] != start_fingerprint or champion_drift
            )
        metrics["operational_incident"] = incident_count > 0
        metrics["safety_relaxed"] = safety_relaxed
        metrics["start_guard"] = start_guard
        metrics["evaluation_guard"] = {
            "current_safety_fingerprint": current_safety["fingerprint"],
            "safety_fingerprint_matches": (
                current_safety["fingerprint"] == start_fingerprint
                if start_fingerprint else None
            ),
            "champion_drift": champion_drift,
            "incident_count_since_start": incident_count,
            "integrity_ok": integrity_ok,
        }
        gate = evaluate_shadow_gate(metrics)
        now = datetime.now(timezone.utc)
        exp.shadow_metrics = metrics
        verdict = gate["verdict"]
        decision = {"verdict": verdict, "reason_code": gate["reason_code"],
                    "detail": gate["detail"], "checks": gate["checks"],
                    "stage": "SHADOW", "decided_at": now.isoformat()}
        if verdict in (STATUS_ELIGIBLE, STATUS_REJECTED) and can_transition(exp.status, verdict):
            exp.status = verdict
            exp.decided_at = now
            if verdict == STATUS_ELIGIBLE:
                decision["promotion_plan"] = build_promotion_plan(
                    champion or {}, cfg,
                    {"shadow": {k: metrics.get(k) for k in
                                ("challenger_resolved", "observed_days", "coverage_pct")},
                     "candidate": metrics.get("candidate"),
                     "champion": metrics.get("champion")})
        exp.decision = decision
        await session.commit()                       # status+métricas: 1 transação
        return {"ok": True, "verdict": verdict, "reason_code": gate["reason_code"],
                "detail": gate["detail"], "experiment": exp.to_dict(full=True)}


def env_snapshot() -> Dict[str, Any]:
    """Flags do P05. Defaults NÃO alteram o LIVE; não existe auto-promotion."""
    return {
        "P05_ANALYTICS_ENABLED": P05_ANALYTICS_ENABLED,
        "P05_CHALLENGER_SHADOW_ENABLED": P05_CHALLENGER_SHADOW_ENABLED,
        "P05_MAX_CANDIDATES": P05_MAX_CANDIDATES,
        "P05_MIN_OFFLINE_RESOLVED": P05_MIN_OFFLINE_RESOLVED,
        "P05_MIN_OOS_RESOLVED": P05_MIN_OOS_RESOLVED,
        "P05_MIN_SHADOW_RESOLVED": P05_MIN_SHADOW_RESOLVED,
        "P05_MIN_SHADOW_DAYS": P05_MIN_SHADOW_DAYS,
        "P05_MIN_FEATURE_COVERAGE_PCT": P05_MIN_FEATURE_COVERAGE_PCT,
        "P05_MIN_SHADOW_COVERAGE_PCT": P05_MIN_SHADOW_COVERAGE_PCT,
        "P05_BOOTSTRAP_SAMPLES": P05_BOOTSTRAP_SAMPLES,
        "P05_RANDOM_SEED": P05_RANDOM_SEED,
        "P05_DIAG_CACHE_TTL_S": P05_DIAG_CACHE_TTL_S,
    }


async def get_p05_status(days: int = 30) -> Dict[str, Any]:
    """Visão consolidada: diagnóstico + champion + shadow ativo + gate + flags."""
    out: Dict[str, Any] = {
        "enabled": P05_ANALYTICS_ENABLED,
        "window_days": days,
        "champion": discover_champion_config(),
        "env": env_snapshot(),
        "live_untouched": True,
        "note": "P05 é somente evidência: não promove, não executa, não altera o LIVE",
    }
    if not P05_ANALYTICS_ENABLED:
        out["reason"] = "P05_ANALYTICS_ENABLED=false"
        return out
    try:
        out["diagnosis"] = await get_cached_diagnosis(days)
    except Exception as exc:
        log.warning(f"[p05] status/diagnóstico falhou: {exc}")
        out["diagnosis"] = {"error": str(exc)}
    try:
        from db import get_session
        from models.strategy_experiment import StrategyExperiment as E
        from sqlalchemy import select, func
        async with get_session() as session:
            shadow = (await session.execute(
                select(E).where(E.status == STATUS_SHADOW).order_by(E.shadow_started_at.desc())
            )).scalars().first()
            counts = list((await session.execute(
                select(E.status, func.count(E.id)).group_by(E.status)
            )).all())
            # Resumo P05.1: reconhecido pela config contextual, sem coluna nova.
            contextual = (await session.execute(
                select(E).where(E.candidate_config.has_key("CONTEXT_RULE"))  # noqa: W601
            )).scalars().all()
        out["shadow_experiment"] = shadow.to_dict(full=True) if shadow else None
        out["experiments_by_status"] = {s: int(c) for s, c in counts}
        out["contextual_experiments"] = len(contextual)
        out["contextual_offline_validated"] = sum(
            1 for e in contextual if e.status == STATUS_OFFLINE_VALIDATED)
        out["contextual_rejected"] = sum(
            1 for e in contextual if e.status in (STATUS_REJECTED, STATUS_INSUFFICIENT))
        out["contextual_analytics_only"] = len(contextual)
    except Exception as exc:
        log.warning(f"[p05] status/experimentos falhou: {exc}")
        out["shadow_experiment"] = None
        out["experiments_by_status"] = {"error": str(exc)}
        out["contextual_experiments"] = None
    # Resumo PEQUENO do monitor de prontidão (P05.1R) — somente leitura.
    try:
        # Diagnóstico pode usar 30d, mas prontidão/retenção precisa enxergar ao
        # menos a janela-alvo de 120d; do contrário todo status curto pareceria
        # artificialmente "histórico jovem".
        readiness = await get_readiness(
            days=max(P051R_TARGET_RETENTION_DAYS, min(days, 365)), limit=20)
        out["readiness_by_status"] = (readiness.get("summary") or {}).get("readiness_by_status")
        out["next_recommended_check"] = (readiness.get("summary") or {}).get(
            "next_recommended_check_days")
        out["retention_warning"] = (readiness.get("retention") or {}).get("retention_warning")
        out["holdout_status"] = readiness.get("holdout_status")
    except Exception as exc:
        log.warning(f"[p05] status/readiness falhou: {exc}")
        out["readiness_by_status"] = None
        out["holdout_status"] = HOLDOUT_SEALED
    # P05.2A — diagnóstico longitudinal dos stops (janela fixa de 120 dias,
    # independente do seletor do painel). Fail-soft: não derruba o resto.
    try:
        out["stop_diagnosis"] = await get_cached_stop_diagnosis(P052A_WINDOW_DAYS)
    except Exception as exc:
        log.warning(f"[p05.2a] stop_diagnosis falhou: {exc}")
        out["stop_diagnosis"] = {"phase": "P05.2A", "error": str(exc),
                                 "holdout_status": HOLDOUT_SEALED}
    out["p051"] = {
        "phase": P051_PHASE,
        "execution_mode": P051_EXECUTION_MODE,
        "promotable": False,
        "shadow_supported": False,
        "allowed_axes": list(P051_CONTEXT_AXES),
        "actions": list(P051_ACTIONS),
        "score_delta": P051_SCORE_DELTA,
        "note": ("regras contextuais são SOMENTE análise offline; integração ao "
                 "executor é uma fase posterior"),
    }
    out["decision_gate"] = {
        "states": [STATUS_DRAFT, STATUS_INSUFFICIENT, STATUS_REJECTED,
                   STATUS_OFFLINE_VALIDATED, STATUS_SHADOW, STATUS_ELIGIBLE],
        "transitions": {k: list(v) for k, v in VALID_TRANSITIONS.items()},
        "shadow_requirements": {
            "min_resolved": P05_MIN_SHADOW_RESOLVED,
            "min_days": P05_MIN_SHADOW_DAYS,
            "min_coverage_pct": P05_MIN_SHADOW_COVERAGE_PCT,
        },
        "eligible_means": "pode ser apresentado ao usuário para autorização MANUAL",
        "eligible_does_not_mean": "não significa que foi ativado",
    }
    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    return out
