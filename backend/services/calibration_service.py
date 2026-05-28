"""
Calibration Service — mapeia score composto → P(TP1) calibrada.

Por que existe:
  O score do recommendation_service é uma combinação ponderada (confluence
  0.45 + MTF 0.30 + R:R 0.20 + win-rate ±5). Funciona como ranking, mas
  NÃO é uma probabilidade — score 72 não significa "72% de chance de TP1".

  Esta camada usa snapshots resolvidos pra produzir um mapeamento
  empírico score → P(TP1), que é mostrado no card e usado pra calibrar
  sizing (futuro).

Algoritmo:
  1. Bucketiza score em bins de 5 pontos: [55,60), [60,65) ... [95,100]
  2. Por bucket calcula P_observed = wins / total
     onde wins = won_tp1 + won_tp1_be + won_tp2
  3. Aplica shrinkage bayesiano: P_shrunk = (k*P_global + n*P_obs) / (k+n)
     com k=10 — pesa global quando amostra é pequena
  4. Aplica PAV (Pool Adjacent Violators) — isotonic regression manual
     pra forçar monotonicidade: score↑ ⇒ P↑
  5. Cache 10min

Fallback: enquanto não houver >= MIN_SAMPLE_TOTAL trades, retorna None
e o frontend não mostra prob calibrada.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

from sqlalchemy import select, and_

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

CACHE_TTL = 600                  # 10 min
LOOKBACK_DAYS = 90               # janela de aprendizado
MIN_SAMPLE_TOTAL = 30            # mínimo de trades resolvidos pra ativar calib
SHRINKAGE_K = 10                 # peso do P_global quando bin é pequeno
SCORE_BINS = [(55, 60), (60, 65), (65, 70), (70, 75),
              (75, 80), (80, 85), (85, 90), (90, 95), (95, 100.1)]

# Estados considerados vitória pra P(TP1)
WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
RESOLVED_STATUSES = WIN_STATUSES + ("lost", "expired")

_cache: Dict[str, Any] = {"ts": 0, "data": None}


def _bin_index(score: float) -> int:
    """Retorna índice do bin pro score. -1 se fora do range."""
    for i, (lo, hi) in enumerate(SCORE_BINS):
        if lo <= score < hi:
            return i
    return -1


def _pav_isotonic(values: List[float], weights: List[float]) -> List[float]:
    """
    Pool Adjacent Violators — isotonic regression monotônica (não-decrescente).
    Implementação iterativa O(n²) suficiente pra n=9 bins.

    Quando dois adjacentes violam (v[i] > v[i+1]), mescla em média ponderada
    pelos pesos e repete até estável.
    """
    n = len(values)
    if n <= 1:
        return list(values)
    v = list(values)
    w = list(weights)
    # Representa cada "bloco" como (sum, weight, start_idx, end_idx)
    blocks = [(v[i] * w[i], w[i], i, i) for i in range(n)]
    i = 0
    while i < len(blocks) - 1:
        sum_a, w_a, _, _ = blocks[i]
        sum_b, w_b, _, _ = blocks[i + 1]
        avg_a = sum_a / w_a if w_a > 0 else 0
        avg_b = sum_b / w_b if w_b > 0 else 0
        if avg_a > avg_b:
            # Mescla
            new_block = (sum_a + sum_b, w_a + w_b, blocks[i][2], blocks[i + 1][3])
            blocks = blocks[:i] + [new_block] + blocks[i + 2:]
            if i > 0:
                i -= 1  # volta pra revalidar
        else:
            i += 1
    # Expande blocos em valores
    result = [0.0] * n
    for sum_b, w_b, start, end in blocks:
        avg = sum_b / w_b if w_b > 0 else 0
        for j in range(start, end + 1):
            result[j] = avg
    return result


async def _compute_calibration() -> Optional[Dict[str, Any]]:
    """
    Calcula tabela score-bin → P(TP1) calibrada.
    Retorna None se não houver dados suficientes.
    """
    if not DB_ENABLED:
        return None

    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    async with get_session() as session:
        stmt = select(
            RecommendationSnapshot.score,
            RecommendationSnapshot.status,
        ).where(and_(
            RecommendationSnapshot.outcome_at >= since,
            RecommendationSnapshot.status.in_(RESOLVED_STATUSES),
        ))
        rows = (await session.execute(stmt)).all()

    total = len(rows)
    if total < MIN_SAMPLE_TOTAL:
        return None

    # P_global
    wins_global = sum(1 for _, st in rows if st in WIN_STATUSES)
    p_global = wins_global / total

    # Per-bin counts
    bin_total = [0] * len(SCORE_BINS)
    bin_wins = [0] * len(SCORE_BINS)
    for score, status in rows:
        bi = _bin_index(float(score))
        if bi < 0:
            continue
        bin_total[bi] += 1
        if status in WIN_STATUSES:
            bin_wins[bi] += 1

    # P_observed + shrinkage bayesiano
    bin_p_raw = []
    bin_p_shrunk = []
    for i in range(len(SCORE_BINS)):
        n = bin_total[i]
        if n == 0:
            bin_p_raw.append(p_global)
            bin_p_shrunk.append(p_global)
        else:
            p_obs = bin_wins[i] / n
            p_shr = (SHRINKAGE_K * p_global + n * p_obs) / (SHRINKAGE_K + n)
            bin_p_raw.append(p_obs)
            bin_p_shrunk.append(p_shr)

    # PAV: força monotonicidade não-decrescente
    # Peso = max(1, n) pra bins vazios não dominarem
    weights = [max(1.0, float(n)) for n in bin_total]
    bin_p_calibrated = _pav_isotonic(bin_p_shrunk, weights)

    # Constrói saída
    bins_out = []
    for i, (lo, hi) in enumerate(SCORE_BINS):
        bins_out.append({
            "score_lo": lo,
            "score_hi": int(hi) if hi == int(hi) else round(hi, 1),
            "label": f"[{lo}-{int(hi)})" if i < len(SCORE_BINS) - 1 else f"[{lo}-100]",
            "n_total": bin_total[i],
            "n_wins": bin_wins[i],
            "p_observed": round(bin_p_raw[i], 4),
            "p_shrunk": round(bin_p_shrunk[i], 4),
            "p_calibrated": round(bin_p_calibrated[i], 4),
        })

    return {
        "enabled": True,
        "total_resolved": total,
        "wins_global": wins_global,
        "p_global": round(p_global, 4),
        "lookback_days": LOOKBACK_DAYS,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "bins": bins_out,
    }


async def get_calibration() -> Optional[Dict[str, Any]]:
    """Wrapper cacheado. Retorna None se calib não está pronta ainda."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        data = await _compute_calibration()
    except Exception as e:
        log.warning(f"[calibration] compute falhou: {e}")
        data = None
    _cache["ts"] = now
    _cache["data"] = data
    return data


async def prob_tp1_for_score(score: float) -> Optional[float]:
    """
    Lookup rápido: dado um score, retorna P(TP1) calibrada [0..1] ou None
    se calibração não está pronta.
    """
    if score is None:
        return None
    calib = await get_calibration()
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        # Score fora do range — usa P_global como fallback
        return calib.get("p_global")
    return calib["bins"][bi]["p_calibrated"]


def prob_tp1_for_score_sync(score: float) -> Optional[float]:
    """
    Versão sync que só lê do cache. Usada em hot paths (_build_recommendation).
    Retorna None se cache vazio — primeira varredura após boot não tem prob
    ainda, mas próximas terão (get_calibration roda no startup).
    """
    if score is None:
        return None
    calib = _cache.get("data")
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        return calib.get("p_global")
    return calib["bins"][bi]["p_calibrated"]


def invalidate_cache() -> None:
    """Útil pra testes / forçar refresh."""
    _cache["ts"] = 0
    _cache["data"] = None
