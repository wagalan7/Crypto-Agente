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

# Teto duro do bootstrap (protege CPU mesmo se a env vier absurda).
_BOOTSTRAP_HARD_MAX = 5000

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
        ident = row.get("dedupe_key")
        if ident is not None:
            if ident in seen:
                _drop("duplicado")
                continue
            seen.add(ident)

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
        "MTF_ALIGNED_MODE": os.getenv("MTF_ALIGNED_MODE", "boost").strip().lower(),
        "MTF_ALIGNED_MIN_COUNT": _env_int("MTF_ALIGNED_MIN_COUNT", 2),
        "PROXIMITY_MAX_ATR": _env_float("PROXIMITY_MAX_ATR", 1.0),
        "STRUCT_CHASE_GATE_ENABLED": _env_bool("STRUCT_CHASE_GATE_ENABLED", "false"),
        "STRUCT_CHASE_MAX_ATR": _env_float("STRUCT_CHASE_MAX_ATR", 5.0),
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
    return f"{champion_hash[:16]}:{candidate_hash[:16]}:{_utc(cutoff).isoformat()}"


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
        score = _finite(row.get("score"))
        if score is None:
            return None
        if score < _finite(config.get("SCORE_MIN")):
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

    if "quality_edge" in active_components and config.get("QUALITY_EDGE_GATE_ENABLED"):
        score = _finite(row.get("score"))
        smin = _finite(config.get("SCORE_MIN"))
        margin = _finite(config.get("QUALITY_EDGE_MARGIN")) or 0.0
        if score is None or smin is None:
            return None
        if smin <= score < smin + margin:          # banda marginal exige edge
            edge = _finite(feats.get("edge_score"))
            tags = feats.get("edge_tags")
            if edge is None and tags is None:
                return None
            has_edge = (edge is not None and edge > 0) or bool(tags)
            if not has_edge:
                return False

    if "mtf" in active_components and (config.get("MTF_ALIGNED_MODE") == "required"):
        aligned = feats.get("mtf_aligned")
        n_aligned = _finite(aligned)
        if n_aligned is None:
            return None
        if n_aligned < (_finite(config.get("MTF_ALIGNED_MIN_COUNT")) or 0):
            return False

    if "proximity" in active_components:
        chase = _finite(feats.get("chase_atr"))
        if chase is None:
            return None
        if chase > (_finite(config.get("PROXIMITY_MAX_ATR")) or 0.0):
            return False

    if "struct_chase" in active_components and config.get("STRUCT_CHASE_GATE_ENABLED"):
        struct = _finite(feats.get("struct_chase_atr"))
        if struct is None:
            return None
        if struct > (_finite(config.get("STRUCT_CHASE_MAX_ATR")) or 0.0):
            return False

    return True


_COMPONENT_FEATURES = {
    "score_min": "__score__",
    "tf_min_tier": "__tier__",
    "quality_edge": "edge_score",
    "mtf": "mtf_aligned",
    "proximity": "chase_atr",
    "struct_chase": "struct_chase_atr",
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

    delta_ci = bootstrap_delta_ci([r["realized_r"] for r in cand_rows],
                                  [r["realized_r"] for r in champ_rows])
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

    exp_v, exp_t = cand_v["expectancy_r"], cand_t["expectancy_r"]
    _chk("expectancy_candidate_positiva",
         exp_v is not None and exp_v > 0 and exp_t is not None and exp_t > 0,
         f"validação={exp_v} teste={exp_t}")

    if objective == OBJECTIVE_LOSS_REDUCTION:
        dci = validation.get("delta_expectancy_ci")
        _chk("ci_delta_expectancy_acima_de_zero",
             bool(dci) and dci["low"] > 0,
             f"IC do delta={dci}")
        _chk("profit_factor_nao_pior",
             _pf(cand_v) >= _pf(champ_v),
             f"cand={cand_v['profit_factor']} champ={champ_v['profit_factor']}")
        _chk("drawdown_nao_pior",
             (cand_v["max_drawdown_r"] or 0) <= (champ_v["max_drawdown_r"] or 0) + 1e-9,
             f"cand={cand_v['max_drawdown_r']} champ={champ_v['max_drawdown_r']}")
        _chk("operacoes_min_70pct",
             champ_v["count"] > 0 and cand_v["count"] >= 0.7 * champ_v["count"],
             f"cand={cand_v['count']} champ={champ_v['count']}")
    elif objective == OBJECTIVE_MORE_OPERATIONS:
        _chk("operacoes_min_110pct",
             champ_v["count"] > 0 and cand_v["count"] >= 1.1 * champ_v["count"],
             f"cand={cand_v['count']} champ={champ_v['count']}")
        eci = cand_v.get("expectancy_ci")
        _chk("ci_expectancy_acima_de_zero",
             bool(eci) and eci["low"] > 0, f"IC={eci}")
        _chk("total_r_maior",
             (cand_v["sum_r"] or 0) > (champ_v["sum_r"] or 0),
             f"cand={cand_v['sum_r']} champ={champ_v['sum_r']}")
        _chk("profit_factor_min_1",
             _pf(cand_v) >= 1.0, f"cand={cand_v['profit_factor']}")
        _chk("drawdown_max_110pct",
             (cand_v["max_drawdown_r"] or 0) <= 1.1 * (champ_v["max_drawdown_r"] or 0) + 1e-9,
             f"cand={cand_v['max_drawdown_r']} champ={champ_v['max_drawdown_r']}")
        add_exp = validation.get("added_expectancy_r")
        _chk("operacoes_adicionais_positivas",
             add_exp is not None and add_exp > 0,
             f"expectancy das adicionais={add_exp}")
    else:
        return {"verdict": STATUS_REJECTED, "reason_code": "OBJETIVO_INVALIDO",
                "detail": objective, "checks": checks}

    _chk("teste_final_positivo",
         exp_t is not None and exp_t > 0 and (cand_t["sum_r"] or 0) > 0,
         f"expectancy={exp_t} total_r={cand_t['sum_r']}")

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

    resolved = shadow_metrics.get("challenger_resolved") or 0
    days = shadow_metrics.get("observed_days") or 0
    coverage = shadow_metrics.get("coverage_pct")
    cand = shadow_metrics.get("candidate") or {}

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
    _chk("sem_incidente_operacional", not shadow_metrics.get("operational_incident"),
         "nenhum incidente operacional registrado")
    _chk("sem_relaxamento_p01_p04", not shadow_metrics.get("safety_relaxed"),
         "nenhuma trava P01–P04 relaxada")

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
    if prox is not None:
        proposals.append(("PROXIMITY_MAX_ATR", round(prox - 0.25, 2), OBJECTIVE_LOSS_REDUCTION))
        proposals.append(("PROXIMITY_MAX_ATR", round(prox + 0.5, 2), OBJECTIVE_MORE_OPERATIONS))
    if not champion.get("STRUCT_CHASE_GATE_ENABLED"):
        proposals.append(("STRUCT_CHASE_GATE_ENABLED", True, OBJECTIVE_LOSS_REDUCTION))
    else:
        sc = _finite(champion.get("STRUCT_CHASE_MAX_ATR"))
        if sc is not None:
            proposals.append(("STRUCT_CHASE_MAX_ATR", round(sc + 2, 2), OBJECTIVE_MORE_OPERATIONS))

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
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
            raw.append({
                "dedupe_key": f"real:{t.id}",
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
                           "metrics": compute_evidence_metrics(bt_rows),
                           "note": "evidência histórica SECUNDÁRIA — não equivale a RealTrade"}
    except Exception as exc:
        log.warning(f"[p05] diagnóstico BACKTEST falhou: {exc}")
        out["backtest"] = {"error": str(exc)}

    try:
        out["gate_events"] = await _load_gate_events(min(days, 30))
    except Exception as exc:
        log.warning(f"[p05] gate events falhou: {exc}")
        out["gate_events"] = {"error": str(exc)}

    out["mae_mfe"] = {
        "status": "UNAVAILABLE",
        "reason": ("o histórico não permite reconstrução fiel de MAE/MFE; aproximar "
                   "pelo preço final criaria afirmação falsa"),
    }
    out["evidence_quality"] = _evidence_quality(out, shadow_rows)
    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    return out


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
                               candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Roda o pipeline temporal completo de UM candidato."""
    knob = candidate["knob"]
    active, coverage_info = active_components_for(rows, knob)
    split = temporal_split(rows)
    if not split.get("ok"):
        return {"verdict": STATUS_INSUFFICIENT, "reason_code": "INSUFFICIENT_DATA",
                "detail": f"{split['total']} outcomes válidos (< {split['required']})",
                "coverage": coverage_info}

    cfg = candidate["config"]
    validation = compare_configs(split["validation"], champion, cfg, active)
    test = compare_configs(split["test"], champion, cfg, active)
    train = compare_configs(split["train"], champion, cfg, active)

    gate = evaluate_offline_gate(candidate["objective"], validation, test)

    folds_out = []
    for fold in walkforward_folds(rows, split["folds"]):
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
                  "note": "teste final NUNCA usado para ajustar threshold"},
        "train": {"champion": train["champion"], "candidate": train["candidate"]},
        "validation": validation,
        "test": test,
        "walkforward": folds_out,
    }


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

    accepted, rejected = generate_candidates(champion, rows)
    cutoff = rows[-1]["resolved_at"]
    fingerprint = dataset_fingerprint(rows)
    champion_hash = canonical_hash(champion)

    from db import get_session
    results: List[Dict[str, Any]] = []
    async with get_session() as session:
        for cand in accepted:
            offline = evaluate_candidate_offline(rows, champion, cand)
            candidate_hash = canonical_hash(cand["config"])
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
                "status": exp.status,
            })
        await session.commit()                       # transação única

    return {
        "ok": True, "champion": champion, "champion_hash": champion_hash,
        "dataset_fingerprint": fingerprint, "dataset_cutoff": cutoff.isoformat(),
        "data_quality": dq, "sample": len(rows),
        "candidates": results, "rejected": rejected,
        "max_candidates": P05_MAX_CANDIDATES,
        "note": "nenhum candidato válido é resultado ACEITÁVEL — não se força vencedor",
    }


# ════════════════════════════════════════════════════════════════════════════
#  P05C — champion × challenger em shadow
# ════════════════════════════════════════════════════════════════════════════
def build_experiment_annotation(row: Dict[str, Any], champion: Dict[str, Any],
                                candidate_cfg: Dict[str, Any], *, experiment_key: str,
                                candidate_hash: str, active: Sequence[str]) -> Dict[str, Any]:
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
    return {
        "experiment_key": experiment_key,
        "candidate_hash": candidate_hash,
        "champion_eligible": champ_ok,
        "challenger_eligible": chall_ok,
        "challenger_status": status,
        "reason_code": reason,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "required_features": required,
        "missing_features": [f for f in required if feats.get(f) is None],
        "schema_version": FEATURES_SCHEMA_VERSION,
    }


def compute_shadow_metrics(rows: Sequence[Dict[str, Any]], champion: Dict[str, Any],
                           candidate_cfg: Dict[str, Any], objective: str,
                           active: Sequence[str], *, started_at: datetime) -> Dict[str, Any]:
    """Relatório do shadow: os DOIS avaliados sobre a MESMA recomendação."""
    merged = dict(champion)
    merged.update(candidate_cfg)
    evaluable, unknown = [], 0
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
        "unknown_excluded": unknown,
        "champion": champ_m,
        "candidate": cand_m,
        "added_ops": len(added),
        "avoided_ops": len(avoided),
        "delta_expectancy_ci": bootstrap_delta_ci(
            [r["realized_r"] for r in cand_rows], [r["realized_r"] for r in champ_rows]),
        "objective": objective,
        "objective_still_met": bool(still_met),
        "operational_incident": False,
        "safety_relaxed": False,
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
    """OFFLINE_VALIDATED → SHADOW. Idempotente; só UM shadow ativo."""
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select
    async with get_session() as session:
        exp = (await session.execute(
            select(E).where(E.id == exp_id).with_for_update()
        )).scalar_one_or_none()
        if exp is None:
            return {"ok": False, "error": "experimento não encontrado"}
        if exp.status == STATUS_SHADOW:
            return {"ok": True, "idempotent": True, "experiment": exp.to_dict(full=False)}
        if not can_transition(exp.status, STATUS_SHADOW):
            return {"ok": False, "error": f"transição inválida {exp.status} → {STATUS_SHADOW}",
                    "status": exp.status}
        active = (await session.execute(
            select(E).where(E.status == STATUS_SHADOW)
        )).scalars().first()
        if active is not None and active.id != exp.id:
            return {"ok": False, "error": "já existe um experimento SHADOW ativo",
                    "conflict_experiment_id": active.id, "conflict": True}
        exp.status = STATUS_SHADOW
        exp.shadow_started_at = datetime.now(timezone.utc)
        await session.commit()                       # status+métricas: 1 transação
        return {"ok": True, "idempotent": False, "experiment": exp.to_dict(full=False)}


async def evaluate_shadow(exp_id: int) -> Dict[str, Any]:
    """P05D — decide REJECTED/ELIGIBLE ou segue aguardando amostra."""
    from db import get_session
    from models.strategy_experiment import StrategyExperiment as E
    from sqlalchemy import select
    champion = discover_champion_config()
    async with get_session() as session:
        exp = (await session.execute(
            select(E).where(E.id == exp_id).with_for_update()
        )).scalar_one_or_none()
        if exp is None:
            return {"ok": False, "error": "experimento não encontrado"}
        if exp.status != STATUS_SHADOW:
            return {"ok": False, "error": f"experimento não está em SHADOW (status={exp.status})",
                    "status": exp.status}
        started = _utc(exp.shadow_started_at) or _utc(exp.created_at)
        cfg = exp.candidate_config or {}
        knob = next(iter(cfg), None)

    days = max(1, min(365, int((datetime.now(timezone.utc) - started).days) + 1))
    rows, _dq = await _load_shadow(days)
    rows = [r for r in rows if r["resolved_at"] >= started]
    active, _cov = active_components_for(rows, knob) if knob else ([], {})
    metrics = compute_shadow_metrics(rows, champion, cfg, exp.objective, active,
                                     started_at=started)
    gate = evaluate_shadow_gate(metrics)
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        exp = (await session.execute(
            select(E).where(E.id == exp_id).with_for_update()
        )).scalar_one_or_none()
        if exp is None or exp.status != STATUS_SHADOW:
            return {"ok": False, "error": "experimento mudou de estado durante a avaliação"}
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
                    champion, cfg,
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
        out["diagnosis"] = await build_diagnosis(days)
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
        out["shadow_experiment"] = shadow.to_dict(full=True) if shadow else None
        out["experiments_by_status"] = {s: int(c) for s, c in counts}
    except Exception as exc:
        log.warning(f"[p05] status/experimentos falhou: {exc}")
        out["shadow_experiment"] = None
        out["experiments_by_status"] = {"error": str(exc)}
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
