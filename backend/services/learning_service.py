"""
Learning Service — aprendizado contínuo a partir das recomendações geradas.

NÍVEL 1 (implementado): estatísticas por bucket.
- Agrupa trades resolvidos por tier, timeframe, direção, hora, padrão, etc.
- Devolve win rate, R médio, sample size por bucket.
- Identifica "winning combos" (buckets com performance >> baseline) e
  "losing combos" (buckets com performance << baseline).
- Cacheado por 5 minutos pra não esquentar o banco.

NÍVEL 2 (preparado, não ativo): compute_score_adjustments() retorna
multiplicadores por bucket que podem ser aplicados ao score composto.
Ativar quando houver >= 50 trades por bucket.
"""
from __future__ import annotations
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from sqlalchemy import select, and_

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

# ── Configuração ─────────────────────────────────────────────────────────
CACHE_TTL = 300                  # 5 min
MIN_SAMPLE_BUCKET = 5            # mínimo pra reportar stat de um bucket
WINNING_THRESHOLD = 0.60         # win_rate >= 60% → "winning combo"
LOSING_THRESHOLD = 0.40          # win_rate <= 40% → "losing combo"
BASELINE_WIN_RATE = 0.50         # baseline pra comparar

# Janela padrão de aprendizado em dias. 0 = TODO o histórico (sem corte
# temporal) — o agente aprende com cada trade resolvido que já existiu.
# Defina LEARNING_LOOKBACK_DAYS > 0 pra voltar a uma janela móvel.
LEARNING_LOOKBACK_DAYS = int(os.getenv("LEARNING_LOOKBACK_DAYS", "0"))

# Status considerados "resolvidos" pra estatística de bucket.
_RESOLVED_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2", "lost")


def _resolved_conditions(days: int):
    """
    Monta as condições WHERE pra snapshots resolvidos. Se days <= 0,
    NÃO aplica corte temporal — usa todo o histórico disponível.
    """
    conds = [RecommendationSnapshot.status.in_(_RESOLVED_STATUSES)]
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conds.append(RecommendationSnapshot.outcome_at >= since)
    return conds


_cache: Dict[str, Any] = {"ts": 0, "data": None}


def invalidate_cache() -> None:
    """Limpa o cache de buckets — força recomputo na próxima chamada.
    Usado pela recalibração manual e por testes."""
    keys = [k for k in _cache.keys() if k.startswith("stats_")]
    for k in keys:
        _cache.pop(k, None)
    _cache["ts"] = 0
    _cache["data"] = None


def _empty_stat() -> Dict[str, Any]:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "avg_r": 0.0, "total_r": 0.0}


def _update_stat(stat: Dict[str, Any], r: float) -> None:
    stat["trades"] += 1
    stat["total_r"] += r
    if r > 0:
        stat["wins"] += 1
    elif r < 0:
        stat["losses"] += 1


def _finalize_stat(stat: Dict[str, Any]) -> Dict[str, Any]:
    n = stat["trades"]
    if n > 0:
        stat["win_rate"] = round(stat["wins"] / n * 100, 1)
        stat["avg_r"] = round(stat["total_r"] / n, 2)
        stat["total_r"] = round(stat["total_r"], 2)
    return stat


def _hour_bucket(hour: int) -> str:
    """Agrupa horas em sessões: Asia (0-7), Europe (7-14), NY (14-21), Off (21-24)"""
    if 0 <= hour < 7:
        return "Asia"
    if 7 <= hour < 14:
        return "Europe"
    if 14 <= hour < 21:
        return "NY"
    return "Off-hours"


def _dow_name(dow: int) -> str:
    return ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"][dow] if 0 <= dow <= 6 else "?"


async def compute_stats_by_bucket(days: int = LEARNING_LOOKBACK_DAYS) -> Dict[str, Any]:
    """
    Agrupa trades resolvidos em vários buckets. Se days <= 0 usa TODO o
    histórico (sem corte temporal). Cacheia por CACHE_TTL.
    """
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    now = time.time()
    cache_key = f"stats_{days}"
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(and_(*_resolved_conditions(days)))
        snaps = (await session.execute(stmt)).scalars().all()

    total = len(snaps)
    if total == 0:
        result = {
            "enabled": True, "total_trades": 0, "days": days,
            "message": "Sem trades resolvidos ainda. Aguarde recomendações fecharem (~horas a dias)."
        }
        _cache[cache_key] = {"ts": now, "data": result}
        return result

    # Buckets
    by_tier = defaultdict(_empty_stat)
    by_tf = defaultdict(_empty_stat)
    by_direction = defaultdict(_empty_stat)
    by_tier_tf = defaultdict(_empty_stat)
    by_session = defaultdict(_empty_stat)
    by_dow = defaultdict(_empty_stat)
    by_pattern = defaultdict(_empty_stat)
    by_funding = defaultdict(_empty_stat)
    by_symbol = defaultdict(_empty_stat)

    for s in snaps:
        r = s.realized_r if s.realized_r is not None else 0
        feats = s.features or {}

        _update_stat(by_tier[s.tier], r)
        _update_stat(by_tf[s.timeframe], r)
        _update_stat(by_direction[s.direction], r)
        _update_stat(by_tier_tf[f"{s.tier}_{s.timeframe}"], r)
        _update_stat(by_symbol[s.symbol.split("/")[0]], r)

        # Hora UTC → sessão
        hour = feats.get("hour_utc")
        if hour is not None:
            _update_stat(by_session[_hour_bucket(int(hour))], r)

        # Dia da semana
        dow = feats.get("day_of_week")
        if dow is not None:
            _update_stat(by_dow[_dow_name(int(dow))], r)

        # Padrões (cada trade pode ter múltiplos)
        for pat in feats.get("patterns", []) or []:
            _update_stat(by_pattern[pat], r)

        # Funding regime
        fs = feats.get("funding_sentiment")
        if fs:
            _update_stat(by_funding[fs], r)

    # Finaliza (computa win_rate, avg_r)
    def _bucket_dict(d, sort_by="trades"):
        finalized = {k: _finalize_stat(v) for k, v in d.items()}
        return dict(sorted(finalized.items(), key=lambda x: -x[1][sort_by]))

    tiers = _bucket_dict(by_tier)
    tfs = _bucket_dict(by_tf)
    directions = _bucket_dict(by_direction)
    tier_tf = _bucket_dict(by_tier_tf)
    sessions = _bucket_dict(by_session)
    dows = _bucket_dict(by_dow)
    patterns = _bucket_dict(by_pattern)
    fundings = _bucket_dict(by_funding)
    symbols = _bucket_dict(by_symbol)

    # ── Combos vencedores / perdedores ───────────────────────────────────
    all_combos = []
    for label, bucket in [
        ("tier", tiers), ("tf", tfs), ("direction", directions),
        ("tier_tf", tier_tf), ("session", sessions), ("dow", dows),
        ("pattern", patterns), ("funding", fundings),
    ]:
        for key, stat in bucket.items():
            if stat["trades"] < MIN_SAMPLE_BUCKET:
                continue
            all_combos.append({
                "category": label,
                "name": key,
                "trades": stat["trades"],
                "win_rate": stat["win_rate"],
                "avg_r": stat["avg_r"],
                "total_r": stat["total_r"],
            })

    winners = sorted(
        [c for c in all_combos if c["win_rate"] >= WINNING_THRESHOLD * 100],
        key=lambda x: (-x["win_rate"], -x["trades"]),
    )[:8]
    losers = sorted(
        [c for c in all_combos if c["win_rate"] <= LOSING_THRESHOLD * 100],
        key=lambda x: (x["win_rate"], -x["trades"]),
    )[:8]

    # ── Edge total do sistema ────────────────────────────────────────────
    total_r = sum(s.realized_r or 0 for s in snaps)
    overall_win_rate = sum(1 for s in snaps if (s.realized_r or 0) > 0) / total * 100

    result = {
        "enabled": True,
        "days": days,
        "total_trades": total,
        "overall": {
            "win_rate_pct": round(overall_win_rate, 1),
            "total_r": round(total_r, 2),
            "avg_r": round(total_r / total, 2),
        },
        "by_tier": tiers,
        "by_timeframe": tfs,
        "by_direction": directions,
        "by_tier_timeframe": tier_tf,
        "by_session": sessions,
        "by_day_of_week": dows,
        "by_pattern": patterns,
        "by_funding": fundings,
        "by_symbol": symbols,
        "winning_combos": winners,
        "losing_combos": losers,
        "baseline_win_rate": int(BASELINE_WIN_RATE * 100),
        "min_sample": MIN_SAMPLE_BUCKET,
    }
    _cache[cache_key] = {"ts": now, "data": result}
    return result


async def lookup_historical_for(
    tier: str, timeframe: str, direction: str, days: int = LEARNING_LOOKBACK_DAYS,
) -> Optional[Dict[str, Any]]:
    """
    Devolve stat histórico do bucket (tier, tf, direction) — usado pelo
    frontend pra mostrar badge "histórico: X trades, Y% win" em cada
    recomendação.
    """
    stats = await compute_stats_by_bucket(days=days)
    if not stats.get("enabled"):
        return None

    # Bucket mais granular: tier_tf — depois cruza com direction manualmente
    # Como o cache já não separa por direction nesse bucket, vamos consultar
    # o banco direto pra precisão.
    if not DB_ENABLED:
        return None

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.tier == tier,
                RecommendationSnapshot.timeframe == timeframe,
                RecommendationSnapshot.direction == direction,
                *_resolved_conditions(days),
            )
        )
        snaps = (await session.execute(stmt)).scalars().all()

    if not snaps:
        return {"trades": 0, "win_rate": None, "avg_r": None,
                "sample_ok": False, "verdict": "sem_historico"}

    wins = sum(1 for s in snaps if (s.realized_r or 0) > 0)
    total_r = sum(s.realized_r or 0 for s in snaps)
    n = len(snaps)
    wr = wins / n * 100
    avg_r = total_r / n

    if n < MIN_SAMPLE_BUCKET:
        verdict = "amostra_pequena"
    elif wr >= WINNING_THRESHOLD * 100:
        verdict = "winning"
    elif wr <= LOSING_THRESHOLD * 100:
        verdict = "losing"
    else:
        verdict = "neutro"

    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wr, 1),
        "avg_r": round(avg_r, 2),
        "sample_ok": n >= MIN_SAMPLE_BUCKET,
        "verdict": verdict,
    }


async def lookup_historical_batch(
    keys: List[Dict[str, str]], days: int = LEARNING_LOOKBACK_DAYS,
) -> Dict[str, Dict[str, Any]]:
    """
    Lookup em batch: recebe [{tier, timeframe, direction}, ...] e retorna
    {f'{tier}_{tf}_{dir}': stat}. Mais eficiente que N chamadas individuais.
    """
    if not DB_ENABLED or not keys:
        return {}

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(and_(*_resolved_conditions(days)))
        snaps = (await session.execute(stmt)).scalars().all()

    # Indexa por (tier, tf, dir)
    by_key = defaultdict(list)
    for s in snaps:
        k = f"{s.tier}_{s.timeframe}_{s.direction}"
        by_key[k].append(s.realized_r or 0)

    result = {}
    for k_obj in keys:
        k = f"{k_obj['tier']}_{k_obj['timeframe']}_{k_obj['direction']}"
        rs = by_key.get(k, [])
        n = len(rs)
        if n == 0:
            result[k] = {"trades": 0, "win_rate": None, "avg_r": None,
                         "sample_ok": False, "verdict": "sem_historico"}
            continue
        wins = sum(1 for r in rs if r > 0)
        wr = wins / n * 100
        avg_r = sum(rs) / n
        if n < MIN_SAMPLE_BUCKET:
            verdict = "amostra_pequena"
        elif wr >= WINNING_THRESHOLD * 100:
            verdict = "winning"
        elif wr <= LOSING_THRESHOLD * 100:
            verdict = "losing"
        else:
            verdict = "neutro"
        result[k] = {
            "trades": n, "wins": wins, "losses": n - wins,
            "win_rate": round(wr, 1), "avg_r": round(avg_r, 2),
            "sample_ok": n >= MIN_SAMPLE_BUCKET, "verdict": verdict,
        }
    return result


# ── NÍVEL 2: AUTO-ADJUST + AUTO-BLOCK ───────────────────────────────────
# Dormente por bucket: só aplica quando o bucket atinge amostra mínima.
# Buckets independentes — pattern X pode despertar antes de pattern Y.
# Multi-categoria: combina multiplicadores de tier_tf × pattern × session × dow × funding.

import os as _os

AUTO_ADJUST_ENABLED = _os.getenv("LEARNING_AUTO_ADJUST", "true").strip().lower() in ("1", "true", "yes")
AUTO_BLOCK_ENABLED = _os.getenv("LEARNING_AUTO_BLOCK", "true").strip().lower() in ("1", "true", "yes")
MIN_SAMPLE_ADJUST = int(_os.getenv("LEARNING_MIN_SAMPLE_ADJUST", "20"))
MIN_SAMPLE_BLOCK = int(_os.getenv("LEARNING_MIN_SAMPLE_BLOCK", "30"))
BLOCK_WR_MAX = float(_os.getenv("LEARNING_BLOCK_WR_MAX", "30"))     # bloqueia bucket com wr ≤ 30%
BOOST_WR_MIN = float(_os.getenv("LEARNING_BOOST_WR_MIN", "65"))     # boost só se wr ≥ 65%
ADJUST_CAP = float(_os.getenv("LEARNING_ADJUST_CAP", "0.25"))       # ±25% no score

# Categorias e como derivar a chave do bucket a partir do sig
_BUCKET_CATEGORIES = ("tier_tf", "pattern", "session", "dow", "funding")


async def compute_auto_adjustments(days: int = LEARNING_LOOKBACK_DAYS) -> Dict[str, Any]:
    """
    Calcula multiplicadores de score (por bucket) + lista de buckets bloqueados.
    Cada categoria é independente e dormente: só ativa quando bucket atinge
    amostra mínima. Resultado cacheado via compute_stats_by_bucket (5 min).

    Shape:
      {
        "enabled": bool,
        "score_multipliers": {
            "tier_tf": {"A_4h": 1.12, ...},
            "pattern": {"engulfing_bull": 1.18, ...},
            "session": {"NY": 0.92, ...},
            "dow": {"Qua": 0.88, ...},
            "funding": {"contango_alto": 1.05, ...},
        },
        "blocked_buckets": [
            {"category":"pattern", "key":"hammer_bear", "wr":22, "n":34},
        ],
        "active_buckets": int,
        "dormant_buckets": int,
        "thresholds": {...},
        "total_trades": int,
      }

    Não-destrutivo: se uma rec NÃO bate em nenhum bucket despertado, score
    fica inalterado (multiplicador = 1.0).
    """
    if not AUTO_ADJUST_ENABLED and not AUTO_BLOCK_ENABLED:
        return {"enabled": False, "reason": "ambos LEARNING_AUTO_* desativados"}

    stats = await compute_stats_by_bucket(days=days)
    if not stats.get("enabled"):
        return {"enabled": False, "reason": "stats indisponíveis"}

    total_trades = stats.get("total_trades", 0)

    multipliers: Dict[str, Dict[str, float]] = {c: {} for c in _BUCKET_CATEGORIES}
    blocked: List[Dict[str, Any]] = []
    active = 0
    dormant = 0

    # Mapa categoria → bucket no stats
    category_to_field = {
        "tier_tf": "by_tier_timeframe",
        "pattern": "by_pattern",
        "session": "by_session",
        "dow": "by_day_of_week",
        "funding": "by_funding",
    }

    for category, field in category_to_field.items():
        for key, stat in stats.get(field, {}).items():
            n = stat["trades"]
            wr = stat["win_rate"]

            # Auto-block tem prioridade — bucket catastrófico nunca contribui pro multiplicador
            if AUTO_BLOCK_ENABLED and n >= MIN_SAMPLE_BLOCK and wr <= BLOCK_WR_MAX:
                blocked.append({
                    "category": category, "key": key, "wr": wr, "n": n,
                    "reason": f"win_rate {wr}% ≤ {BLOCK_WR_MAX}% em {n} amostras",
                })
                active += 1
                continue

            # Boost só vale quando bucket é claramente vencedor
            if AUTO_ADJUST_ENABLED and n >= MIN_SAMPLE_ADJUST and wr >= BOOST_WR_MIN:
                # Mapeia wr [BOOST_WR_MIN..100] → multiplier [1.0..1+CAP]
                excess = (wr - BOOST_WR_MIN) / (100.0 - BOOST_WR_MIN)
                mult = 1.0 + ADJUST_CAP * max(0.0, min(1.0, excess))
                multipliers[category][key] = round(mult, 3)
                active += 1
                continue

            # Penalidade leve pra buckets ruins (>= MIN_SAMPLE_ADJUST mas < blocking)
            # win_rate entre BLOCK_WR_MAX e 50% → multiplier 1-CAP até 1.0
            if AUTO_ADJUST_ENABLED and n >= MIN_SAMPLE_ADJUST and wr < 50.0:
                deficit = (50.0 - wr) / (50.0 - BLOCK_WR_MAX)
                mult = 1.0 - ADJUST_CAP * max(0.0, min(1.0, deficit))
                multipliers[category][key] = round(mult, 3)
                active += 1
                continue

            # Bucket existe mas sem amostra → dormente
            if n > 0:
                dormant += 1

    return {
        "enabled": True,
        "score_multipliers": multipliers,
        "blocked_buckets": blocked,
        "active_buckets": active,
        "dormant_buckets": dormant,
        "total_trades": total_trades,
        "thresholds": {
            "min_sample_adjust": MIN_SAMPLE_ADJUST,
            "min_sample_block": MIN_SAMPLE_BLOCK,
            "block_wr_max": BLOCK_WR_MAX,
            "boost_wr_min": BOOST_WR_MIN,
            "adjust_cap_pct": int(ADJUST_CAP * 100),
        },
        "feature_flags": {
            "auto_adjust": AUTO_ADJUST_ENABLED,
            "auto_block": AUTO_BLOCK_ENABLED,
        },
    }


def _sig_to_bucket_keys(sig: Any) -> Dict[str, Any]:
    """Mapeia um TradeSignal pros keys de bucket. Robusto a campos faltando."""
    keys: Dict[str, Any] = {}
    try:
        # timeframe
        tf = getattr(sig, "timeframe", None)

        # Sessão/dow a partir do timestamp (int ms ou s)
        ts = getattr(sig, "timestamp", None)
        if ts is not None:
            try:
                ts_int = int(ts)
                if ts_int > 1e12:  # ms
                    ts_int = ts_int // 1000
                from datetime import datetime as _dt, timezone as _tz
                dt = _dt.fromtimestamp(ts_int, tz=_tz.utc)
                keys["session"] = _hour_bucket(dt.hour)
                keys["dow"] = _dow_name(dt.weekday())
            except Exception:
                pass

        # Padrões — extrai .type.value de cada DetectedPattern
        patterns = getattr(sig, "patterns", None)
        if patterns and isinstance(patterns, list):
            pat_names: List[str] = []
            for p in patterns:
                t = getattr(p, "type", None)
                if t is None and isinstance(p, dict):
                    t = p.get("type")
                if t is not None:
                    name = t.value if hasattr(t, "value") else str(t)
                    pat_names.append(name)
            if pat_names:
                keys["__patterns_list"] = pat_names

        # Funding sentiment (derivatives pode ser dict ou obj)
        deriv = getattr(sig, "derivatives", None)
        if deriv:
            fs = None
            if isinstance(deriv, dict):
                fs = deriv.get("funding_sentiment")
            else:
                fs = getattr(deriv, "funding_sentiment", None)
            if fs:
                keys["funding"] = fs

        # tier_tf é setado externamente via tier_provisional
        if tf:
            keys["__tf"] = tf
    except Exception:
        pass
    return keys


def apply_score_adjustment(
    sig: Any, base_score: float, adjustments: Dict[str, Any], tier_provisional: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Aplica multiplicadores de bucket ao score. Combina por produto, capeia.
    Retorna {score, multiplier, matched_buckets, blocked, block_reason}.

    Se algum bucket está na block list → blocked=True.
    """
    if not adjustments or not adjustments.get("enabled"):
        return {"score": base_score, "multiplier": 1.0, "matched_buckets": [], "blocked": False, "block_reason": None}

    # Constrói chaves do sig
    sig_keys = _sig_to_bucket_keys(sig)
    tf = sig_keys.get("__tf") or getattr(sig, "timeframe", None)
    if tier_provisional and tf:
        sig_keys["tier_tf"] = f"{tier_provisional}_{tf}"

    # 1) Block check
    blocked_buckets = adjustments.get("blocked_buckets", [])
    for b in blocked_buckets:
        cat = b["category"]
        key = b["key"]
        if cat == "pattern":
            pats = sig_keys.get("__patterns_list") or []
            if key in pats:
                return {
                    "score": 0.0, "multiplier": 0.0, "matched_buckets": [],
                    "blocked": True,
                    "block_reason": f"bucket {cat}={key} bloqueado ({b['reason']})",
                }
        else:
            if sig_keys.get(cat) == key:
                return {
                    "score": 0.0, "multiplier": 0.0, "matched_buckets": [],
                    "blocked": True,
                    "block_reason": f"bucket {cat}={key} bloqueado ({b['reason']})",
                }

    # 2) Apply multipliers — produto combinado, capeado em [1-CAP, 1+CAP] no agregado
    multipliers = adjustments.get("score_multipliers", {})
    combined = 1.0
    matched: List[str] = []

    for cat in _BUCKET_CATEGORIES:
        cat_mults = multipliers.get(cat, {})
        if not cat_mults:
            continue
        if cat == "pattern":
            for pat in (sig_keys.get("__patterns_list") or []):
                m = cat_mults.get(pat)
                if m is not None:
                    combined *= m
                    matched.append(f"pattern={pat}({m:.2f})")
        else:
            key = sig_keys.get(cat)
            if key and key in cat_mults:
                m = cat_mults[key]
                combined *= m
                matched.append(f"{cat}={key}({m:.2f})")

    # Cap agregado pra evitar drift extremo quando vários buckets se acumulam
    combined = max(1.0 - ADJUST_CAP, min(1.0 + ADJUST_CAP, combined))

    adjusted = base_score * combined
    return {
        "score": round(adjusted, 2),
        "multiplier": round(combined, 3),
        "matched_buckets": matched,
        "blocked": False,
        "block_reason": None,
    }


# ── Backward-compat: stub antigo ────────────────────────────────────────
async def compute_score_adjustments() -> Dict[str, float]:
    """Mantido pra compatibilidade — agora delega pra compute_auto_adjustments
    e retorna só o bucket tier_tf como dict plano."""
    data = await compute_auto_adjustments(days=LEARNING_LOOKBACK_DAYS)
    if not data.get("enabled"):
        return {}
    return data.get("score_multipliers", {}).get("tier_tf", {})
