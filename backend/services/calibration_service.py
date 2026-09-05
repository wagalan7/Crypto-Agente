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
import hashlib
import json
import logging
import math
import numbers
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, NamedTuple, Optional, Tuple

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

# ── Aprendizado por histórico (blended): mistura trades simulados do sweep ──────
# (tabela backtest_trades) na calibração score→P(TP1). DESLIGADO no scoring de
# dinheiro real por default — só entra ao vivo com CALIBRATION_INCLUDE_BACKTEST=on
# (revisar o A/B do preview antes). O backtest é CAPADO a cap_mult × nº de trades
# reais pra não afogar o sinal vivo recente.
def _include_backtest_live() -> bool:
    return os.getenv("CALIBRATION_INCLUDE_BACKTEST", "false").strip().lower() in ("1", "true", "yes", "on")
BACKTEST_CAP_MULT = float(os.getenv("CALIBRATION_BACKTEST_CAP_MULT", "1.0"))

# Gavetas (bins) score→P(TP1). A escala do score V2 é OUTRA (≈18–71, p50≈46) vs
# o legado (55–100). Se a calibração usasse os bins legados sob a V2, a maioria
# dos scores cairia ABAIXO de 55 (fora de range → p_global pra tudo), matando a
# diferenciação da convicção e do gate de P(TP1). Por isso os bins seguem a flag
# SCORE_FORMULA_V2 — lidos do env aqui (sem import de recommendation_service pra
# evitar ciclo). Com a flag OFF, mantém-se os bins legados → NO-OP.
_SCORE_FORMULA_V2 = os.getenv("SCORE_FORMULA_V2", "false").strip().lower() in ("1", "true", "yes", "on")
SCORE_BINS_LEGACY = [(55, 60), (60, 65), (65, 70), (70, 75),
                     (75, 80), (80, 85), (85, 90), (90, 95), (95, 100.1)]
# Derivados da distribuição V2 medida (/api/score/tier-sim): min 18, p25 36,
# p50 46, p75 53, p90 59, max 71. Bins ~equipopulados cobrindo a faixa + margem.
SCORE_BINS_V2 = [(15, 31), (31, 36), (36, 40), (40, 44), (44, 48),
                 (48, 52), (52, 57), (57, 63), (63, 75)]
SCORE_BINS = SCORE_BINS_V2 if _SCORE_FORMULA_V2 else SCORE_BINS_LEGACY

# ── R06B2: identidade da calibração ─────────────────────────────────────────
# Os IDs de fórmula são os MESMOS de `recommendation_service.ScoreProvenance`
# (SCORE_V2 / LEGACY_V1), repetidos aqui como literais para não importar aquele
# módulo (evita ciclo). Um teste do R06B2 trava a igualdade entre os dois lados.
#
# `calibration_formula` diz qual fórmula os BINS ACEITAM — não é uma afirmação
# retroativa de que cada par histórico tenha proveniência individual conhecida.
# Pares resolvidos antes do R06B1 não carregam a fórmula que os gerou; isso é
# registrado honestamente em `pairs_formula_provenance` e NÃO é inferido pelo
# valor do score.
CALIBRATION_FORMULA_V2 = "SCORE_V2"
CALIBRATION_FORMULA_LEGACY = "LEGACY_V1"
KNOWN_FORMULAS = frozenset({CALIBRATION_FORMULA_V2, CALIBRATION_FORMULA_LEGACY})
PAIRS_PROVENANCE_UNVERSIONED = "UNVERSIONED_PRE_R06B1"
CALIBRATION_CONTRACT_VERSION = "r06b2.1"
SUPPORTED_CONTRACT_VERSIONS = frozenset({"r06b2.1"})


def active_calibration_formula() -> str:
    """Fórmula que os bins ATUAIS aceitam (segue a mesma flag dos bins)."""
    return CALIBRATION_FORMULA_V2 if _SCORE_FORMULA_V2 else CALIBRATION_FORMULA_LEGACY


def _bin_pairs(bins) -> List[Tuple[float, float]]:
    """Normaliza `[(lo, hi), ...]` ou `[{score_lo, score_hi}, ...]` em pares."""
    out: List[Tuple[float, float]] = []
    for b in bins:
        if isinstance(b, dict):
            lo, hi = b.get("score_lo"), b.get("score_hi")
        else:
            lo, hi = b[0], b[1]
        out.append((float(lo), float(hi)))
    return out


def bins_fingerprint(bins=None, formula: Optional[str] = None) -> str:
    """SHA-256 do JSON canônico da configuração INTEIRA dos bins.

    R06B2.1: a versão anterior resumia fórmula + quantidade + extremos, então
    mover uma divisão INTERNA mantinha a mesma string — um score podia ser lido
    por uma partição diferente daquela em que a tabela foi treinada. O hash
    cobre todas as bordas.

    Entram no hash apenas `calibration_formula` e a lista ordenada completa de
    `[score_lo, score_hi]`. Nada de data, probabilidade ou dado de amostra —
    caso contrário o fingerprint mudaria a cada recalibração.
    """
    pares = _bin_pairs(SCORE_BINS if bins is None else bins)
    formula = active_calibration_formula() if formula is None else formula
    canonico = json.dumps(
        {"calibration_formula": formula,
         "bins": [[lo, hi] for lo, hi in pares]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def bins_version(bins=None, formula: Optional[str] = None) -> str:
    """Identificador estável da configuração de bins: `<FÓRMULA>:sha256:<hex>`."""
    formula = active_calibration_formula() if formula is None else formula
    return f"{formula}:sha256:{bins_fingerprint(bins, formula)}"


def is_valid_bins_version(value) -> bool:
    """Forma sintática de uma `bins_version` — não verifica o conteúdo."""
    if not isinstance(value, str):
        return False
    partes = value.split(":")
    if len(partes) != 3:
        return False
    formula, algoritmo, digest = partes
    return (formula in KNOWN_FORMULAS and algoritmo == "sha256"
            and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest))


def bins_score_range(bins=None) -> List[float]:
    pares = _bin_pairs(SCORE_BINS if bins is None else bins)
    return [pares[0][0], pares[-1][1]]


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
            # Label reflete o hi REAL do bin (não hardcode "100"). Legado: último bin
            # (95,100.1)→"[95-100)"; V2: último bin (63,75)→"[63-75)". Antes o último
            # bin era sempre "[lo-100]", o que sob a V2 exibia "[63-100]" (errado).
            "label": f"[{lo}-{min(int(hi), 100)})",
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
        # ── R06B2: identidade declarada da calibração ────────────────────
        # Sem isto, um score de uma fórmula podia ser lido pelos bins de outra.
        "contract_version": CALIBRATION_CONTRACT_VERSION,
        "calibration_formula": active_calibration_formula(),
        "bins_version": bins_version(),
        "score_range": bins_score_range(),
        # Honestidade sobre a AMOSTRA: os pares que alimentaram esta tabela não
        # carregam, individualmente, a fórmula que gerou cada score — o registro
        # de proveniência só existe a partir do R06B1. Não é inferido do valor.
        "pairs_formula_provenance": PAIRS_PROVENANCE_UNVERSIONED,
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


async def _load_shadow_pairs() -> List[Tuple[float, str]]:
    """Pares (score, status) dos snapshots SHADOW resolvidos.

    R06B1 (nomenclatura): a fonte é `RecommendationSnapshot` — o registro de
    RECOMENDAÇÕES do bot, não o financeiro. Trade real com dinheiro vive em
    `RealTrade` e NÃO é lido aqui. O nome antigo (`_load_real_pairs`) dizia o
    contrário e induzia a ler a calibração como se fosse P&L executado.
    Amostra, filtros e resultado são exatamente os de antes.
    """
    pairs: List[Tuple[float, str]] = []
    if not DB_ENABLED:
        return pairs
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
    except Exception as e:
        log.warning(f"[calibration] DB read (shadow) falhou: {e}")
    return pairs


async def _load_real_pairs() -> List[Tuple[float, str]]:
    """Alias LEGADO de `_load_shadow_pairs` — delega, não duplica a query.

    Mantido só pra não quebrar consumidores/testes que ainda chamam o nome
    antigo. O nome é enganoso (a fonte é SHADOW, não financeira); use o novo.
    """
    return await _load_shadow_pairs()


async def _load_backtest_pairs(cap: Optional[int]) -> List[Tuple[float, str]]:
    """Pares (score, status) dos trades SIMULADOS do sweep (backtest_trades).
    `cap`: nº máximo de pares (amostra aleatória) pra não afogar o sinal real.
    Fail-soft: tabela pode não existir ainda → retorna []."""
    if cap is not None and cap <= 0:
        return []
    pairs: List[Tuple[float, str]] = []
    if not DB_ENABLED:
        return pairs
    try:
        from models.backtest_trade import BacktestTrade
        from sqlalchemy import func as _func
        async with get_session() as session:
            stmt = select(BacktestTrade.score, BacktestTrade.status).where(
                BacktestTrade.status.in_(RESOLVED_STATUSES)
            )
            if cap is not None:
                stmt = stmt.order_by(_func.random()).limit(cap)
            rows = (await session.execute(stmt)).all()
            for sc, st in rows:
                pairs.append((float(sc), st))
    except Exception as e:
        log.warning(f"[calibration] DB read (backtest) falhou: {e}")
    return pairs


async def _compute_calibration(include_backtest: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    """
    Calcula tabela score-bin → P(TP1) calibrada combinando:
      1. Trades reais resolvidos do DB (últimos LOOKBACK_DAYS)
      2. Trades simulados do sweep (backtest_trades) — só se include_backtest
      3. Seed externo legado (se CALIBRATION_SEED_PATH setado)
    `include_backtest=None` ⇒ usa a flag CALIBRATION_INCLUDE_BACKTEST (caminho live).
    Passe True/False explícito pra computar variantes (preview/compare A/B).
    Retorna None se total < MIN_SAMPLE_TOTAL.
    """
    if include_backtest is None:
        include_backtest = _include_backtest_live()

    real_pairs = await _load_shadow_pairs()
    real_count = len(real_pairs)
    pairs: List[Tuple[float, str]] = list(real_pairs)

    bt_count = 0
    if include_backtest:
        cap = int(BACKTEST_CAP_MULT * real_count) if real_count > 0 else None
        bt_pairs = await _load_backtest_pairs(cap)
        bt_count = len(bt_pairs)
        pairs.extend(bt_pairs)

    seed_pairs = _load_seed_pairs()
    pairs.extend(seed_pairs)

    if len(pairs) < MIN_SAMPLE_TOTAL:
        return None

    parts = [f"db({real_count})"]
    if bt_count:
        parts.append(f"backtest({bt_count})")
    if seed_pairs:
        parts.append(f"seed({len(seed_pairs)})")
    source = "+".join(parts)
    out = compute_calibration_from_pairs(pairs, source=source)
    if out is not None:
        out["real_count"] = real_count
        out["backtest_count"] = bt_count
        out["include_backtest"] = bool(include_backtest)
    return out


async def get_calibration() -> Optional[Dict[str, Any]]:
    """Wrapper cacheado. Retorna None se calib não está pronta ainda.
    Usa a flag CALIBRATION_INCLUDE_BACKTEST pro caminho de dinheiro real."""
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


async def compute_calibration_preview() -> Dict[str, Any]:
    """A/B pra revisão humana: calibração SÓ real vs real+backtest (blended),
    SEM tocar o cache live nem o scoring de dinheiro real. Mostra o delta de
    P(TP1) por bin e o p_global, pra decidir se vale ligar CALIBRATION_INCLUDE_BACKTEST."""
    live = await _compute_calibration(include_backtest=False)
    blended = await _compute_calibration(include_backtest=True)

    bins_delta = []
    if live and blended:
        bl_by_label = {b["label"]: b for b in blended["bins"]}
        for b in live["bins"]:
            bb = bl_by_label.get(b["label"]) or {}
            bins_delta.append({
                "label": b["label"],
                "n_real": b["n_total"],
                "n_blended": bb.get("n_total"),
                "p_live": b["p_calibrated"],
                "p_blended": bb.get("p_calibrated"),
                "delta": round((bb.get("p_calibrated", 0) - b["p_calibrated"]), 4),
            })
    return {
        "active_live": "blended" if _include_backtest_live() else "real_only",
        "cap_mult": BACKTEST_CAP_MULT,
        "real_count": (live or {}).get("real_count") if live else None,
        "backtest_count": (blended or {}).get("backtest_count") if blended else None,
        "p_global_live": (live or {}).get("p_global") if live else None,
        "p_global_blended": (blended or {}).get("p_global") if blended else None,
        "bins": bins_delta,
        "note": ("Ligue CALIBRATION_INCLUDE_BACKTEST=on (1 deploy) pra usar o blended "
                 "no scoring real. Default OFF — só real."),
    }


# ════════════════════════════════════════════════════════════════════════════
#  R06B2 — CERCA ENTRE SCORE, FÓRMULA, BINS E PROBABILIDADE
# ════════════════════════════════════════════════════════════════════════════
# Defeito corrigido: SCORE_V2 solicitada → V2 indisponível → fallback LEGACY_V1
# → bins continuam V2 → o lookup devolvia `p_global` (ou uma probabilidade de
# outro contrato) e esse número alimentava gates e sizing como se fosse
# calibrado. Agora fórmula e bins têm identidade explícita, e incompatibilidade
# vira AUSÊNCIA de probabilidade + bloqueio da autoexecução afetada.
#
# `p_global` continua válido como ESTATÍSTICA AGREGADA da calibração; o que
# deixou de existir é seu uso como probabilidade INDIVIDUAL de um score que não
# tem bin compatível.

PROB_STATUS_READY = "READY"
PROB_STATUS_CALIBRATION_UNAVAILABLE = "CALIBRATION_UNAVAILABLE"
PROB_STATUS_FORMULA_MISMATCH = "FORMULA_MISMATCH"
PROB_STATUS_SCORE_OUT_OF_RANGE = "SCORE_OUT_OF_RANGE"
PROB_STATUS_INVALID_SCORE = "INVALID_SCORE"
PROB_STATUS_INVALID_CALIBRATION_CONTRACT = "INVALID_CALIBRATION_CONTRACT"

PROB_STATUSES = frozenset({
    PROB_STATUS_READY,
    PROB_STATUS_CALIBRATION_UNAVAILABLE,
    PROB_STATUS_FORMULA_MISMATCH,
    PROB_STATUS_SCORE_OUT_OF_RANGE,
    PROB_STATUS_INVALID_SCORE,
    PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
})

# Estados que BLOQUEIAM a autoexecução. `CALIBRATION_UNAVAILABLE` fica de fora
# de propósito: calibração imatura é o estado normal de quem ainda não tem
# amostra, e o comportamento operacional dela não muda neste pacote.
BLOCKING_PROB_STATUSES = frozenset({
    PROB_STATUS_FORMULA_MISMATCH,
    PROB_STATUS_SCORE_OUT_OF_RANGE,
    PROB_STATUS_INVALID_SCORE,
    PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
})

# Vocabulário FECHADO de motivos. Nunca mensagem crua de exceção, caminho de
# arquivo, credencial ou dado pessoal.
PROB_REASON_OK = "OK"
PROB_REASON_NO_CALIBRATION = "NO_CALIBRATION_CACHE"
PROB_REASON_NO_BINS = "NO_BINS"
PROB_REASON_SAMPLE_BELOW_MINIMUM = "SAMPLE_BELOW_MINIMUM"
PROB_REASON_SCORE_MISSING = "SCORE_MISSING"
PROB_REASON_SCORE_NOT_NUMERIC = "SCORE_NOT_NUMERIC"
PROB_REASON_SCORE_NOT_FINITE = "SCORE_NOT_FINITE"
PROB_REASON_SCORE_OUTSIDE_BINS = "SCORE_OUTSIDE_BINS"
PROB_REASON_FORMULA_NOT_ACCEPTED = "SCORE_FORMULA_NOT_ACCEPTED_BY_BINS"
PROB_REASON_SCORE_FORMULA_UNKNOWN = "SCORE_FORMULA_UNKNOWN"
PROB_REASON_CALIB_FORMULA_UNKNOWN = "CALIBRATION_FORMULA_UNKNOWN"
PROB_REASON_BINS_VERSION_MISSING = "BINS_VERSION_MISSING"
PROB_REASON_BINS_MALFORMED = "BINS_MALFORMED"
PROB_REASON_BINS_AMBIGUOUS = "BINS_AMBIGUOUS"
PROB_REASON_PROB_OUT_OF_UNIT = "PROBABILITY_OUT_OF_UNIT_RANGE"
PROB_REASON_TP2_ABOVE_TP1 = "TP2_ABOVE_TP1"
PROB_REASON_PROVENANCE_MISSING = "PROBABILITY_PROVENANCE_MISSING"
# ── R06B2.1 ────────────────────────────────────────────────────────────────
PROB_REASON_LOOKUP_FAILED = "PROBABILITY_LOOKUP_FAILED"
PROB_REASON_CONTRACT_VERSION_UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"
PROB_REASON_TOTAL_RESOLVED_INVALID = "TOTAL_RESOLVED_INVALID"
PROB_REASON_BINS_VERSION_MISMATCH = "BINS_VERSION_MISMATCH"
PROB_REASON_SCORE_RANGE_MISMATCH = "SCORE_RANGE_MISMATCH"
PROB_REASON_BINS_UNSORTED = "BINS_UNSORTED"
PROB_REASON_PROVENANCE_MALFORMED = "PROBABILITY_PROVENANCE_MALFORMED"
PROB_REASON_READY_INCOMPLETE = "READY_CONTRACT_INCOMPLETE"
PROB_REASON_UNAVAILABLE_WITH_PROB = "UNAVAILABLE_WITH_PROBABILITY"
PROB_REASON_STATUS_UNKNOWN = "STATUS_UNKNOWN"

PROB_REASON_CODES = frozenset({
    PROB_REASON_OK, PROB_REASON_NO_CALIBRATION, PROB_REASON_NO_BINS,
    PROB_REASON_SAMPLE_BELOW_MINIMUM, PROB_REASON_SCORE_MISSING,
    PROB_REASON_SCORE_NOT_NUMERIC, PROB_REASON_SCORE_NOT_FINITE,
    PROB_REASON_SCORE_OUTSIDE_BINS, PROB_REASON_FORMULA_NOT_ACCEPTED,
    PROB_REASON_SCORE_FORMULA_UNKNOWN, PROB_REASON_CALIB_FORMULA_UNKNOWN,
    PROB_REASON_BINS_VERSION_MISSING, PROB_REASON_BINS_MALFORMED,
    PROB_REASON_BINS_AMBIGUOUS, PROB_REASON_PROB_OUT_OF_UNIT,
    PROB_REASON_TP2_ABOVE_TP1, PROB_REASON_PROVENANCE_MISSING,
    PROB_REASON_LOOKUP_FAILED, PROB_REASON_CONTRACT_VERSION_UNSUPPORTED,
    PROB_REASON_TOTAL_RESOLVED_INVALID, PROB_REASON_BINS_VERSION_MISMATCH,
    PROB_REASON_SCORE_RANGE_MISMATCH, PROB_REASON_BINS_UNSORTED,
    PROB_REASON_PROVENANCE_MALFORMED, PROB_REASON_READY_INCOMPLETE,
    PROB_REASON_UNAVAILABLE_WITH_PROB, PROB_REASON_STATUS_UNKNOWN,
})

# Folga numérica para a comparação P(TP2) <= P(TP1). As duas tabelas saem de
# execuções independentes de PAV sobre o MESMO conjunto de pares (won_tp2 ⊂
# win), então a ordem é estrutural; a folga cobre só arredondamento de 4 casas.
_TP2_TOLERANCE = 1e-9


class CalibrationProbabilityResult(NamedTuple):
    """Resultado ÚNICO do lookup de probabilidade calibrada.

    `prob_tp1`/`prob_tp2` só existem quando `status == READY`. Em qualquer
    outro estado são `None` — ausência, nunca zero e nunca `p_global`.
    """
    prob_tp1: Optional[float]
    prob_tp2: Optional[float]
    status: str
    reason_code: str
    score: Optional[float]
    score_formula_effective: Optional[str]
    calibration_formula: Optional[str]
    bins_version: Optional[str]
    bin_index: Optional[int]
    fallback_used: bool

    @property
    def ok(self) -> bool:
        return self.status == PROB_STATUS_READY

    @property
    def blocking(self) -> bool:
        return self.status in BLOCKING_PROB_STATUSES

    def as_provenance(self) -> Dict[str, Any]:
        """Payload anexado à Recommendation (JSON já serializado, sem coluna)."""
        return {
            "contract_version": CALIBRATION_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "score_formula_effective": self.score_formula_effective,
            "calibration_formula": self.calibration_formula,
            "bins_version": self.bins_version,
            "bin_index": self.bin_index,
            "fallback_used": self.fallback_used,
        }


def _finite_number(value) -> Optional[float]:
    """Número real e finito, ou None. `bool` não é score."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, numbers.Real):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _unit_prob(value) -> Optional[float]:
    f = _finite_number(value)
    if f is None or f < 0.0 or f > 1.0:
        return None
    return f


def _result(status, reason_code, **kw) -> CalibrationProbabilityResult:
    base = {
        "prob_tp1": None, "prob_tp2": None, "score": None,
        "score_formula_effective": None, "calibration_formula": None,
        "bins_version": None, "bin_index": None, "fallback_used": False,
    }
    base.update(kw)
    return CalibrationProbabilityResult(status=status, reason_code=reason_code, **base)


def probability_for_score(
    score,
    score_formula_effective: Optional[str],
    calibration: Optional[Dict[str, Any]],
    *,
    fallback_used: bool = False,
) -> CalibrationProbabilityResult:
    """Função PURA: score + fórmula efetiva + calibração já carregada → veredito.

    Não consulta banco, rede, ENV nem exchange — a calibração chega pronta pelo
    argumento. Ordem de avaliação fixa (determinística):
      score inválido → sem calibração → contrato inválido → mismatch →
      fora do range → READY.
    """
    formula = score_formula_effective if isinstance(score_formula_effective, str) else None

    # 1. Score precisa ser número real e finito.
    if score is None:
        return _result(PROB_STATUS_INVALID_SCORE, PROB_REASON_SCORE_MISSING,
                       score_formula_effective=formula, fallback_used=fallback_used)
    if isinstance(score, bool) or not isinstance(score, numbers.Real):
        return _result(PROB_STATUS_INVALID_SCORE, PROB_REASON_SCORE_NOT_NUMERIC,
                       score_formula_effective=formula, fallback_used=fallback_used)
    score_f = _finite_number(score)
    if score_f is None:
        return _result(PROB_STATUS_INVALID_SCORE, PROB_REASON_SCORE_NOT_FINITE,
                       score_formula_effective=formula, fallback_used=fallback_used)

    common = {"score": score_f, "score_formula_effective": formula,
              "fallback_used": fallback_used}

    # 2. Calibração ausente/imatura — NÃO é incompatibilidade.
    if not isinstance(calibration, dict):
        return _result(PROB_STATUS_CALIBRATION_UNAVAILABLE,
                       PROB_REASON_NO_CALIBRATION, **common)
    bins = calibration.get("bins")
    if not bins:
        return _result(PROB_STATUS_CALIBRATION_UNAVAILABLE,
                       PROB_REASON_NO_BINS, **common)

    calib_formula = calibration.get("calibration_formula")
    calib_bins_version = calibration.get("bins_version")
    common = dict(common, calibration_formula=(
        calib_formula if isinstance(calib_formula, str) else None),
        bins_version=(calib_bins_version if isinstance(calib_bins_version, str) else None))

    # 3. Versão do contrato: uma tabela que não se identifica não é confiável
    #    nem para reportar o próprio tamanho de amostra.
    if calibration.get("contract_version") not in SUPPORTED_CONTRACT_VERSIONS:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_CONTRACT_VERSION_UNSUPPORTED, **common)

    # 4. Amostra: ausente/malformada invalida; abaixo do mínimo é IMATURA.
    total = calibration.get("total_resolved")
    total_f = _finite_number(total)
    if total_f is None:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_TOTAL_RESOLVED_INVALID, **common)
    if total_f < MIN_SAMPLE_TOTAL:
        return _result(PROB_STATUS_CALIBRATION_UNAVAILABLE,
                       PROB_REASON_SAMPLE_BELOW_MINIMUM, **common)

    # 5. Contrato da calibração precisa estar bem formado e identificado.
    if not isinstance(calib_formula, str) or calib_formula not in KNOWN_FORMULAS:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_CALIB_FORMULA_UNKNOWN, **common)
    if not isinstance(calib_bins_version, str) or not calib_bins_version:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_BINS_VERSION_MISSING, **common)
    if formula is None or formula not in KNOWN_FORMULAS:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_SCORE_FORMULA_UNKNOWN, **common)
    if not isinstance(bins, (list, tuple)):
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_BINS_MALFORMED, **common)
    faixas = []
    for b in bins:
        if not isinstance(b, dict):
            return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                           PROB_REASON_BINS_MALFORMED, **common)
        lo = _finite_number(b.get("score_lo"))
        hi = _finite_number(b.get("score_hi"))
        if lo is None or hi is None or lo >= hi:
            return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                           PROB_REASON_BINS_MALFORMED, **common)
        faixas.append((lo, hi))

    # 6. Partição: ordenada, sem sobreposição, com a faixa que ela declara.
    for anterior, atual in zip(faixas, faixas[1:]):
        if atual[0] < anterior[0]:
            return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                           PROB_REASON_BINS_UNSORTED, **common)
        if atual[0] < anterior[1]:
            return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                           PROB_REASON_BINS_AMBIGUOUS, **common)
    faixa_declarada = calibration.get("score_range")
    if (not isinstance(faixa_declarada, (list, tuple))
            or len(faixa_declarada) != 2
            or _finite_number(faixa_declarada[0]) != faixas[0][0]
            or _finite_number(faixa_declarada[1]) != faixas[-1][1]):
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_SCORE_RANGE_MISMATCH, **common)

    # 7. Fingerprint recomputado sobre TODAS as bordas — pega repartição
    #    interna que a versão anterior (fórmula + n + extremos) não via.
    if calib_bins_version != bins_version(faixas, calib_formula):
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_BINS_VERSION_MISMATCH, **common)

    # 8. A fórmula que gerou o score precisa ser a que os bins aceitam.
    if formula != calib_formula:
        return _result(PROB_STATUS_FORMULA_MISMATCH,
                       PROB_REASON_FORMULA_NOT_ACCEPTED, **common)

    # 9. O score precisa cair em EXATAMENTE um bin.
    achados = [i for i, (lo, hi) in enumerate(faixas) if lo <= score_f < hi]
    if len(achados) > 1:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_BINS_AMBIGUOUS, **common)
    if not achados:
        # Sem bin compatível NÃO existe probabilidade individual. `p_global` é
        # estatística agregada e não pode ser usada aqui.
        return _result(PROB_STATUS_SCORE_OUT_OF_RANGE,
                       PROB_REASON_SCORE_OUTSIDE_BINS, **common)
    idx = achados[0]
    common = dict(common, bin_index=idx)

    # 10. As probabilidades do bin precisam ser finitas, em [0,1] e ordenadas.
    p1 = _unit_prob(bins[idx].get("p_calibrated"))
    p2 = _unit_prob(bins[idx].get("p_tp2_calibrated"))
    if p1 is None or p2 is None:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_PROB_OUT_OF_UNIT, **common)
    if p2 > p1 + _TP2_TOLERANCE:
        return _result(PROB_STATUS_INVALID_CALIBRATION_CONTRACT,
                       PROB_REASON_TP2_ABOVE_TP1, **common)

    return _result(PROB_STATUS_READY, PROB_REASON_OK,
                   prob_tp1=p1, prob_tp2=min(p2, p1), **common)


def cached_calibration() -> Optional[Dict[str, Any]]:
    """Calibração já carregada no cache do processo. Não faz I/O nem recalcula."""
    return _cache.get("data")


# ── Veredito único do contrato (consumido por app, verdict e executor) ───────
CALIBRATION_CONTRACT_GATE = "calibration-contract"

_CONTRACT_REASON_PT = {
    PROB_STATUS_FORMULA_MISMATCH:
        "calibração incompatível com a fórmula que gerou este score",
    PROB_STATUS_SCORE_OUT_OF_RANGE:
        "score fora da faixa coberta pelos bins da calibração",
    PROB_STATUS_INVALID_SCORE:
        "score inválido para lookup de probabilidade",
    PROB_STATUS_INVALID_CALIBRATION_CONTRACT:
        "contrato de calibração inválido ou incompleto",
}


def _rec_field(rec, name):
    if isinstance(rec, dict):
        return rec.get(name)
    return getattr(rec, name, None)


def _bloqueia(reason: str, code: str,
              status: str = PROB_STATUS_INVALID_CALIBRATION_CONTRACT) -> Dict[str, Any]:
    return {"ok": False, "blocked_by": CALIBRATION_CONTRACT_GATE,
            "reason": reason, "status": status, "reason_code": code}


def _envelope_ok(prov: Dict[str, Any]) -> bool:
    """Casca comum a qualquer estado: versão e reason_code reconhecidos."""
    return (prov.get("contract_version") in SUPPORTED_CONTRACT_VERSIONS
            and isinstance(prov.get("reason_code"), str)
            and prov.get("reason_code") in PROB_REASON_CODES)


def _ready_consistente(prov: Dict[str, Any], p1, p2) -> bool:
    """READY só passa com o contrato INTEIRO coerente.

    R06B2.1: antes bastava `status == "READY"`. Um dicionário incompleto —
    `{"status": "READY"}` — era aceito como se tivesse sido produzido pelo
    lookup, e a rec passava direto pelos gates.
    """
    if prov.get("score_formula_effective") not in KNOWN_FORMULAS:
        return False
    if prov.get("calibration_formula") not in KNOWN_FORMULAS:
        return False
    if prov.get("score_formula_effective") != prov.get("calibration_formula"):
        return False
    if not is_valid_bins_version(prov.get("bins_version")):
        return False
    idx = prov.get("bin_index")
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
        return False
    if prov.get("fallback_used") is not False:
        return False
    v1, v2 = _unit_prob(p1), _unit_prob(p2)
    if v1 is None or v2 is None:
        return False
    return v2 <= v1 + _TP2_TOLERANCE


def _unavailable_consistente(prov: Dict[str, Any], p1, p2) -> bool:
    """Imaturo tem que ser imaturo: sem bin e sem probabilidade vazada."""
    if p1 is not None or p2 is not None:
        return False
    return prov.get("bin_index") is None


def calibration_contract_verdict(
    recommendation, *, require_current_contract: bool = False,
) -> Dict[str, Any]:
    """Fonte ÚNICA da decisão "esta rec pode virar ordem?" quanto ao contrato
    score↔calibração. Pura, sem I/O; aceita dict ou objeto.

    `require_current_contract=True` (caminho oficial de execução) exige um
    contrato ATUAL: payload sem proveniência nenhuma deixa de passar. Com
    `False` (leitura/exibição de histórico) o payload realmente legado segue
    legível — o que nunca passa, nos dois modos, é contrato presente e
    malformado.

    Retorna {ok, blocked_by, reason, status, reason_code}. `ok=True` significa
    apenas que ESTE contrato não bloqueia — os demais gates continuam valendo.
    """
    prov = _rec_field(recommendation, "probability_provenance")
    score_prov = _rec_field(recommendation, "score_provenance")
    p1 = _rec_field(recommendation, "prob_tp1")
    p2 = _rec_field(recommendation, "prob_tp2")

    if prov is None:
        # Sem proveniência de probabilidade nenhuma.
        if isinstance(score_prov, dict) and score_prov.get("fallback_used") is True:
            return _bloqueia("score veio de fallback de fórmula e o contrato de "
                             "probabilidade está ausente",
                             PROB_REASON_PROVENANCE_MISSING)
        if require_current_contract:
            return _bloqueia("recomendação sem contrato de probabilidade — o "
                             "caminho de execução exige contrato atual",
                             PROB_REASON_PROVENANCE_MISSING)
        return {"ok": True, "blocked_by": None, "reason": None,
                "status": None, "reason_code": None}

    if not isinstance(prov, dict):
        return _bloqueia("contrato de probabilidade malformado",
                         PROB_REASON_PROVENANCE_MALFORMED)

    status = prov.get("status")
    if not isinstance(status, str) or status not in PROB_STATUSES:
        return _bloqueia("estado do contrato de probabilidade desconhecido",
                         PROB_REASON_STATUS_UNKNOWN)

    code = prov.get("reason_code")
    code = code if isinstance(code, str) and code in PROB_REASON_CODES else None

    if status in BLOCKING_PROB_STATUSES:
        return {"ok": False, "blocked_by": CALIBRATION_CONTRACT_GATE,
                "reason": _CONTRACT_REASON_PT[status],
                "status": status, "reason_code": code}

    if not _envelope_ok(prov):
        return _bloqueia("contrato de probabilidade sem versão ou motivo "
                         "reconhecidos", PROB_REASON_PROVENANCE_MALFORMED)

    if status == PROB_STATUS_CALIBRATION_UNAVAILABLE:
        if not _unavailable_consistente(prov, p1, p2):
            return _bloqueia("contrato declara calibração indisponível mas traz "
                             "probabilidade preenchida",
                             PROB_REASON_UNAVAILABLE_WITH_PROB)
        return {"ok": True, "blocked_by": None, "reason": None,
                "status": status, "reason_code": code}

    # status == READY
    if not _ready_consistente(prov, p1, p2):
        return _bloqueia("contrato declara READY mas está incompleto ou "
                         "inconsistente", PROB_REASON_READY_INCOMPLETE)
    return {"ok": True, "blocked_by": None, "reason": None,
            "status": status, "reason_code": code}


def contract_is_blocking(recommendation, *, require_current_contract: bool = False) -> bool:
    """Atalho booleano do MESMO helper — para quem só precisa do sim/não."""
    return not calibration_contract_verdict(
        recommendation, require_current_contract=require_current_contract)["ok"]


def probabilities_from_contract(payload) -> Tuple[Optional[float], Optional[float]]:
    """Probabilidades PERSISTIDAS, devolvidas só se o contrato gravado era READY.

    R06B2.1: histórico não é recalculado com o cache de hoje. Sem proveniência
    (registro legado) ou com contrato não-READY ⇒ ausência nos dois valores.
    Zero legítimo continua zero — a checagem é por `None`, não por falsidade.
    """
    if not isinstance(payload, dict):
        return None, None
    prov = payload.get("probability_provenance")
    if not isinstance(prov, dict) or prov.get("status") != PROB_STATUS_READY:
        return None, None
    p1 = payload.get("prob_tp1")
    p2 = payload.get("prob_tp2")
    veredito = calibration_contract_verdict(
        {"probability_provenance": prov,
         "score_provenance": payload.get("score_provenance"),
         "prob_tp1": p1, "prob_tp2": p2},
        require_current_contract=True,
    )
    if not veredito["ok"]:
        return None, None
    return _unit_prob(p1), _unit_prob(p2)


# ── Lookups LEGADOS ─────────────────────────────────────────────────────────
# Mantidos para compatibilidade de leitura/diagnóstico. R06B2 removeu deles o
# fallback para `p_global`: score sem bin compatível devolve AUSÊNCIA. Eles não
# conhecem a fórmula efetiva do score, então nenhum caminho operacional NOVO
# deve depender deles — use `probability_for_score`.

async def prob_tp1_for_score(score: float) -> Optional[float]:
    """P(TP1) calibrada [0..1] ou None. Sem fórmula efetiva ⇒ sem cerca."""
    if score is None:
        return None
    calib = await get_calibration()
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        return None
    return calib["bins"][bi]["p_calibrated"]


def prob_tp1_for_score_sync(score: float) -> Optional[float]:
    """Versão sync que só lê do cache. Retorna None se cache vazio."""
    if score is None:
        return None
    calib = _cache.get("data")
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        return None
    return calib["bins"][bi]["p_calibrated"]


def prob_tp2_for_score_sync(score: float) -> Optional[float]:
    """Igual a prob_tp1_for_score_sync, mas pra P(TP2)."""
    if score is None:
        return None
    calib = _cache.get("data")
    if not calib or not calib.get("bins"):
        return None
    bi = _bin_index(float(score))
    if bi < 0:
        return None
    return calib["bins"][bi].get("p_tp2_calibrated")


def invalidate_cache() -> None:
    """Útil pra testes / forçar refresh."""
    _cache["ts"] = 0
    _cache["data"] = None
