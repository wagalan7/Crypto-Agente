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
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from sqlalchemy import select, and_, or_, not_

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

CACHE_TTL = 600                  # 10 min
# Janela de aprendizado em dias. 0 (default) = TODO o histórico, sem corte
# temporal — a calibração aprende com cada trade resolvido que existe.
# Defina CALIBRATION_LOOKBACK_DAYS > 0 pra voltar a uma janela móvel.
LOOKBACK_DAYS = int(os.getenv("CALIBRATION_LOOKBACK_DAYS", "0"))
MIN_SAMPLE_TOTAL = 30            # mínimo de trades resolvidos pra ativar calib
SHRINKAGE_K = 10                 # peso do P_global quando bin é pequeno
SCORE_BINS = [(55, 60), (60, 65), (65, 70), (70, 75),
              (75, 80), (80, 85), (85, 90), (90, 95), (95, 100.1)]

# Estados considerados vitória pra P(TP1)
WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
# Vitória pra P(TP2): SÓ quem correu até o TP2 (won_tp2). Subconjunto de WIN.
TP2_WIN_STATUSES = ("won_tp2",)
RESOLVED_STATUSES = WIN_STATUSES + ("lost", "expired")

# Janela mínima pra um 'expired' ser um time-stop LEGÍTIMO. O menor time-stop
# por TF é 1h (TIME_STOP_HOURS_BY_TF), então qualquer 'expired' que resolveu em
# menos que isto NÃO pode ser time-stop real — é um "void" (descarte que nunca
# foi avaliado contra TP1/stop). Dois produtores conhecidos:
#   1. no-data / fora do universo (snapshot_service: fetch_ohlcv vazio + símbolo
#      não-rastreável) → expira no PRIMEIRO check, segundos após criar. DOMINANTE.
#   2. flip_advisory (expire_open_snapshot) → expira na hora, last_check_at NULL.
# Ambos contavam como não-win e diluíam a P(TP1) pra baixo. 30min dá folga ampla
# (nada legítimo resolve entre ~2min e 1h).
FAST_VOID_MAX = timedelta(minutes=30)


def _not_fast_void():
    """Condição SQLAlchemy: exclui 'expired' que nunca teve avaliação justa.
    Agnóstico de causa — pega no-data E flip_advisory."""
    # Usa a forma timestamp+intervalo (outcome_at < created_at + 30min) — não a
    # subtração (outcome_at - created_at < 30min): SQLAlchemy tipa a subtração de
    # dois DateTime como DateTime (não Interval), e comparar com um timedelta
    # Python gera bind inválido → asyncpg estoura. Já 'DateTime + timedelta' é
    # tratado nativamente (bind como interval) e vira comparação limpa de timestamp.
    return not_(and_(
        RecommendationSnapshot.status == "expired",
        or_(
            RecommendationSnapshot.last_check_at.is_(None),
            RecommendationSnapshot.outcome_at
            < (RecommendationSnapshot.created_at + FAST_VOID_MAX),
        ),
    ))

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


def _calibrate_for_win_set(
    pairs: List[Tuple[float, str]],
    win_set: Tuple[str, ...],
) -> Dict[str, Any]:
    """Roda shrinkage bayesiano + PAV pra um conjunto de "vitória" qualquer.
    Reusável: TP1 (win = chegou no TP1) e TP2 (win = só won_tp2). Mesmos pares,
    mesma matemática, só muda o que conta como vitória."""
    total = len(pairs)
    wins_global = sum(1 for _, st in pairs if st in win_set)
    p_global = wins_global / total if total else 0.0

    bin_total = [0] * len(SCORE_BINS)
    bin_wins = [0] * len(SCORE_BINS)
    for score, status in pairs:
        bi = _bin_index(float(score))
        if bi < 0:
            continue
        bin_total[bi] += 1
        if status in win_set:
            bin_wins[bi] += 1

    bin_p_raw, bin_p_shrunk = [], []
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

    weights = [max(1.0, float(n)) for n in bin_total]
    bin_p_calibrated = _pav_isotonic(bin_p_shrunk, weights)
    return {
        "wins_global": wins_global,
        "p_global": p_global,
        "bin_total": bin_total,
        "bin_wins": bin_wins,
        "bin_p_raw": bin_p_raw,
        "bin_p_shrunk": bin_p_shrunk,
        "bin_p_calibrated": bin_p_calibrated,
    }


def compute_calibration_from_pairs(
    pairs: List[Tuple[float, str]],
    source: str = "db",
) -> Optional[Dict[str, Any]]:
    """
    Núcleo puro: dado lista de (score, status) → tabela de bins calibrada.
    `status` precisa estar em RESOLVED_STATUSES.

    Calcula DUAS calibrações sobre os MESMOS pares:
      - P(TP1): vitória = chegou no TP1 (WIN_STATUSES). Campos `p_*`.
      - P(TP2): vitória = correu até TP2 (TP2_WIN_STATUSES). Campos `p_tp2_*`.
    P(TP2) <= P(TP1) por construção (won_tp2 ⊂ win). Usada no sizing por
    convicção (#2a) como sinal aditivo — setup que tende a correr até TP2 vale
    mais. Não checa MIN_SAMPLE_TOTAL — chamador decide. None se vazio.
    """
    total = len(pairs)
    if total == 0:
        return None

    c1 = _calibrate_for_win_set(pairs, WIN_STATUSES)       # P(TP1)
    c2 = _calibrate_for_win_set(pairs, TP2_WIN_STATUSES)   # P(TP2)

    bins_out = []
    for i, (lo, hi) in enumerate(SCORE_BINS):
        bins_out.append({
            "score_lo": lo,
            "score_hi": int(hi) if hi == int(hi) else round(hi, 1),
            "label": f"[{lo}-{int(hi)})" if i < len(SCORE_BINS) - 1 else f"[{lo}-100]",
            "n_total": c1["bin_total"][i],
            "n_wins": c1["bin_wins"][i],
            "p_observed": round(c1["bin_p_raw"][i], 4),
            "p_shrunk": round(c1["bin_p_shrunk"][i], 4),
            "p_calibrated": round(c1["bin_p_calibrated"][i], 4),
            # P(TP2) — mesma estrutura, win = só won_tp2
            "n_wins_tp2": c2["bin_wins"][i],
            "p_tp2_observed": round(c2["bin_p_raw"][i], 4),
            "p_tp2_shrunk": round(c2["bin_p_shrunk"][i], 4),
            "p_tp2_calibrated": round(c2["bin_p_calibrated"][i], 4),
        })
    return {
        "enabled": True,
        "source": source,
        "total_resolved": total,
        "wins_global": c1["wins_global"],
        "p_global": round(c1["p_global"], 4),
        "wins_tp2_global": c2["wins_global"],
        "p_tp2_global": round(c2["p_global"], 4),
        "lookback_days": LOOKBACK_DAYS,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "bins": bins_out,
    }


def _load_seed_pairs() -> List[Tuple[float, str]]:
    """
    Lê seed externo (ex: gerado pelo scripts/seed_calibration.py a partir
    de backtest). Caminho via env CALIBRATION_SEED_PATH. Formato JSON:
      {"pairs": [{"score": 78.3, "status": "won_tp2"}, ...]}
    Trades virtuais — não substituem dados reais, são complementares.
    """
    path = os.getenv("CALIBRATION_SEED_PATH")
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        log.warning(f"[calibration] CALIBRATION_SEED_PATH={path} não existe")
        return []
    try:
        data = json.loads(p.read_text())
        pairs_raw = data.get("pairs", [])
        out = []
        for r in pairs_raw:
            sc = r.get("score")
            st = r.get("status")
            if sc is None or st not in RESOLVED_STATUSES:
                continue
            out.append((float(sc), st))
        log.info(f"[calibration] seed carregado: {len(out)} pares de {path}")
        return out
    except Exception as e:
        log.warning(f"[calibration] falha lendo seed {path}: {e}")
        return []


async def _compute_calibration() -> Optional[Dict[str, Any]]:
    """
    Calcula tabela score-bin → P(TP1) calibrada combinando:
      1. Trades reais resolvidos do DB (últimos LOOKBACK_DAYS)
      2. Trades sintéticos do backtest (se CALIBRATION_SEED_PATH setado)
    Retorna None se total < MIN_SAMPLE_TOTAL.
    """
    pairs: List[Tuple[float, str]] = []
    real_count = 0
    if DB_ENABLED:
        try:
            async with get_session() as session:
                conds = [RecommendationSnapshot.status.in_(RESOLVED_STATUSES)]
                conds.append(_not_fast_void())
                # LOOKBACK_DAYS <= 0 ⇒ TODO o histórico (sem corte temporal)
                if LOOKBACK_DAYS > 0:
                    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
                    conds.append(RecommendationSnapshot.outcome_at >= since)
                stmt = select(
                    RecommendationSnapshot.score,
                    RecommendationSnapshot.status,
                ).where(and_(*conds))
                rows = (await session.execute(stmt)).all()
                for sc, st in rows:
                    pairs.append((float(sc), st))
                real_count = len(rows)
        except Exception as e:
            log.warning(f"[calibration] DB read falhou: {e}")

    seed_pairs = _load_seed_pairs()
    pairs.extend(seed_pairs)

    if len(pairs) < MIN_SAMPLE_TOTAL:
        return None

    source = "db" if not seed_pairs else (
        f"db({real_count})+seed({len(seed_pairs)})" if real_count > 0 else "seed"
    )
    return compute_calibration_from_pairs(pairs, source=source)


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


def prob_tp2_for_score_sync(score: float) -> Optional[float]:
    """
    Igual a prob_tp1_for_score_sync mas pra P(TP2) (correr até o TP2). Lê só do
    cache. None se calib imatura → conviction trata como NO-OP. Usada no sizing
    por convicção (#2a) como sinal aditivo.
    """
    if score is None:
        return None
    calib = _cache.get("data")
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        return calib.get("p_tp2_global")
    return calib["bins"][bi].get("p_tp2_calibrated")


def invalidate_cache() -> None:
    """Útil pra testes / forçar refresh."""
    _cache["ts"] = 0
    _cache["data"] = None
