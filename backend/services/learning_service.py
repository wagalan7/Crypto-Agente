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


_cache: Dict[str, Any] = {"ts": 0, "data": None}


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


async def compute_stats_by_bucket(days: int = 60) -> Dict[str, Any]:
    """
    Agrupa trades resolvidos dos últimos N dias em vários buckets.
    Cacheia por CACHE_TTL.
    """
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    now = time.time()
    cache_key = f"stats_{days}"
    cached = _cache.get(cache_key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
            )
        )
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
    tier: str, timeframe: str, direction: str, days: int = 60,
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

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.tier == tier,
                RecommendationSnapshot.timeframe == timeframe,
                RecommendationSnapshot.direction == direction,
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
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
    keys: List[Dict[str, str]], days: int = 60,
) -> Dict[str, Dict[str, Any]]:
    """
    Lookup em batch: recebe [{tier, timeframe, direction}, ...] e retorna
    {f'{tier}_{tf}_{dir}': stat}. Mais eficiente que N chamadas individuais.
    """
    if not DB_ENABLED or not keys:
        return {}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            and_(
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2", "lost")),
            )
        )
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


# ── NÍVEL 2 (preparado, não ativo) ──────────────────────────────────────
async def compute_score_adjustments() -> Dict[str, float]:
    """
    Multiplicadores por bucket (tier_tf) que poderiam ser aplicados ao score
    composto antes da classificação. Só usa buckets com sample >= 50.

    Não está sendo chamado em produção ainda — ativar quando dados forem
    suficientes pra evitar over-fitting em ruído.
    """
    stats = await compute_stats_by_bucket(days=90)
    if not stats.get("enabled") or stats.get("total_trades", 0) < 50:
        return {}

    adjustments = {}
    for bucket_key, stat in stats.get("by_tier_timeframe", {}).items():
        if stat["trades"] < 50:
            continue
        # Ajuste: relação win_rate / baseline. >1 = upweight, <1 = downweight.
        adj = (stat["win_rate"] / 100) / BASELINE_WIN_RATE
        adj = max(0.7, min(1.3, adj))  # cap em ±30%
        adjustments[bucket_key] = round(adj, 3)
    return adjustments
