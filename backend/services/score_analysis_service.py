"""
Score feature analysis — READ-ONLY diagnóstico do poder preditivo de cada
componente do score, medido contra o outcome real dos trades resolvidos.

NÃO altera execução, sizing, calibração nem nada. Só lê snapshots resolvidos
(status won_*/lost), extrai cada feature contínua e mede o quanto ela separa
ganhador de perdedor. Objetivo: provar/derrubar empiricamente a hipótese de que
mtf/der são "peso morto" no score (recommendation_service._compute_score) antes
de re-pesar a fórmula.

Métricas por feature:
  • coverage   — % dos trades em que a feature está presente (não-nula). Testa
                 direto o "ancora em 50 quando falta dado".
  • auc        — Mann-Whitney / ROC-AUC contra win binário. 0.5 = ruído puro;
                 >0.5 = feature alta → ganha; <0.5 = inversa. |auc-0.5| = força.
  • pbis_r     — correlação ponto-bisserial (Pearson feature × win 0/1).
  • r_corr     — Pearson da feature × realized_r (expectancy contínua).
  • quartis    — win_rate + avg_r por quartil da feature (mostra monotonicidade).

Win = status ∈ (won_tp1, won_tp1_be, won_tp2); Loss = lost. Pura estatística em
Python (sem numpy) pra evitar dependência.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, and_

from db import DB_ENABLED, get_session
from models.recommendation_snapshot import RecommendationSnapshot

log = logging.getLogger(__name__)

WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
RESOLVED_STATUSES = WIN_STATUSES + ("lost",)

# Features contínuas a medir. (label, fonte): fonte "col" = atributo do snapshot;
# "feat" = chave dentro do JSONB features.
_FEATURES: List[Tuple[str, str, str]] = [
    ("score", "col", "score"),                  # o composto atual — baseline a bater
    ("risk_reward", "col", "risk_reward"),
    ("confluence_pct", "feat", "confluence_pct"),
    ("mtf_score", "feat", "mtf_score"),         # alignment -1..+1 (None quando sem MTF)
    ("mtf_aligned", "feat", "mtf_aligned"),
    ("rsi", "feat", "rsi"),
    ("adx", "feat", "adx"),
    ("atr_pct", "feat", "atr_pct"),
    ("funding_pct", "feat", "funding_pct"),     # None quando sem derivativos
    ("oi_change_pct", "feat", "oi_change_pct"),
]


def _auc(pairs: List[Tuple[float, int]]) -> Optional[float]:
    """ROC-AUC via Mann-Whitney U com correção de empates (ranks médios).
    pairs = [(valor, win01)]. Retorna P(valor_win > valor_loss). None se faltar
    uma das classes."""
    n = len(pairs)
    n_win = sum(w for _, w in pairs)
    n_loss = n - n_win
    if n_win == 0 or n_loss == 0:
        return None
    # ranks médios sobre o valor
    order = sorted(range(n), key=lambda i: pairs[i][0])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[order[j + 1]][0] == pairs[order[i]][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # ranks 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    sum_ranks_win = sum(ranks[i] for i in range(n) if pairs[i][1] == 1)
    u = sum_ranks_win - n_win * (n_win + 1) / 2.0
    return u / (n_win * n_loss)


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return sxy / math.sqrt(sxx * syy)


def _quartiles(triples: List[Tuple[float, int, float]]) -> List[Dict[str, Any]]:
    """triples = [(valor, win01, realized_r)] ordenado por valor → 4 grupos."""
    s = sorted(triples, key=lambda t: t[0])
    n = len(s)
    if n < 4:
        return []
    out = []
    for q in range(4):
        lo = q * n // 4
        hi = (q + 1) * n // 4
        grp = s[lo:hi]
        if not grp:
            continue
        wins = sum(w for _, w, _ in grp)
        out.append({
            "q": q + 1,
            "n": len(grp),
            "val_lo": round(grp[0][0], 4),
            "val_hi": round(grp[-1][0], 4),
            "win_rate": round(100 * wins / len(grp), 1),
            "avg_r": round(sum(r for _, _, r in grp) / len(grp), 3),
        })
    return out


def _deciles(triples: List[Tuple[float, int, float]]) -> List[Dict[str, Any]]:
    """triples=[(score,win01,r)] → 10 grupos por score, win_rate+avg_r cada."""
    s = sorted(triples, key=lambda t: t[0])
    n = len(s)
    if n < 10:
        return []
    out = []
    for q in range(10):
        lo = q * n // 10
        hi = (q + 1) * n // 10
        grp = s[lo:hi]
        if not grp:
            continue
        wins = sum(w for _, w, _ in grp)
        out.append({
            "d": q + 1, "n": len(grp),
            "score_lo": round(grp[0][0], 2), "score_hi": round(grp[-1][0], 2),
            "win_rate": round(100 * wins / len(grp), 1),
            "avg_r": round(sum(r for _, _, r in grp) / len(grp), 3),
        })
    return out


def _norm_components(s) -> Dict[str, Optional[float]]:
    """Reconstrói os componentes 0–100 de um snapshot a partir das features
    armazenadas. None = ausente (pra renormalização). Espelha _compute_score."""
    feats = s.features or {}

    def g(k):
        v = feats.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    conf = g("confluence_pct")                      # já 0–100
    adx = g("adx")
    adx_n = (max(0.0, min(adx, 50.0)) / 50.0 * 100.0) if adx is not None else None
    mtf = g("mtf_score")                            # alignment -1..+1
    mtf_n = ((max(-1.0, min(mtf, 1.0)) + 1.0) * 50.0) if mtf is not None else None
    try:
        rr = float(s.risk_reward) if s.risk_reward is not None else None
    except (TypeError, ValueError):
        rr = None
    rr_n = (min(rr / 3.0, 1.0) * 100.0) if rr is not None else None
    fund = g("funding_pct")                          # inverso: maior → pior
    der_n = (50.0 - max(-1.0, min(fund / 0.05, 1.0)) * 50.0) if fund is not None else None
    return {"conf": conf, "adx": adx_n, "mtf": mtf_n, "rr": rr_n, "der": der_n}


async def compute_reweight_sim(
    w_conf: float = 0.55, w_adx: float = 0.20, w_der: float = 0.10,
    w_mtf: float = 0.05, w_rr: float = 0.0, days: int = 0,
) -> Dict[str, Any]:
    """READ-ONLY. Re-pontua os trades resolvidos com pesos novos (renormalizados
    sobre componentes presentes), e compara com o score atual: AUC, win-rate por
    decil e nº de platôs após calibração PAV. NÃO altera nada em produção."""
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    conds = [RecommendationSnapshot.status.in_(RESOLVED_STATUSES)]
    if days and days > 0:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conds.append(RecommendationSnapshot.outcome_at >= since)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(and_(*conds))
        snaps = (await session.execute(stmt)).scalars().all()
    if not snaps:
        return {"enabled": True, "total": 0, "message": "Sem trades resolvidos."}

    new_w = {"conf": w_conf, "adx": w_adx, "der": w_der, "mtf": w_mtf, "rr": w_rr}
    # pesos do score ATUAL (com mtf/der ANCORADOS em 50 quando ausentes)
    old_triples: List[Tuple[float, int, float]] = []   # (stored_score, win, r)
    new_triples: List[Tuple[float, int, float]] = []   # (new_score, win, r)
    old_pairs: List[Tuple[float, str]] = []
    new_pairs_raw: List[Tuple[float, str]] = []        # (new_score, status) p/ calib

    for s in snaps:
        win = 1 if s.status in WIN_STATUSES else 0
        r = float(s.realized_r) if s.realized_r is not None else 0.0
        comp = _norm_components(s)
        # ── score novo: renormaliza sobre presentes ──
        num = den = 0.0
        for k, w in new_w.items():
            if w > 0 and comp.get(k) is not None:
                num += w * comp[k]
                den += w
        if den == 0:
            continue
        new_score = num / den
        new_triples.append((new_score, win, r))
        new_pairs_raw.append((new_score, s.status))
        if s.score is not None:
            old_triples.append((float(s.score), win, r))
            old_pairs.append((float(s.score), s.status))

    if not new_triples or not old_triples:
        return {"enabled": True, "total": len(snaps), "message": "Dados insuficientes."}

    auc_old = _auc([(v, w) for v, w, _ in old_triples])
    auc_new = _auc([(v, w) for v, w, _ in new_triples])

    # nº de platôs após PAV: re-escala new_score pro range do old p/ binning justo
    from services.calibration_service import compute_calibration_from_pairs
    omin = min(v for v, _ in old_pairs); omax = max(v for v, _ in old_pairs)
    nmin = min(v for v, _ in new_pairs_raw); nmax = max(v for v, _ in new_pairs_raw)

    def rescale(v):
        if nmax == nmin:
            return (omin + omax) / 2
        return omin + (v - nmin) / (nmax - nmin) * (omax - omin)

    new_pairs = [(rescale(v), st) for v, st in new_pairs_raw]

    def plateaus(calib):
        if not calib:
            return None
        vals = sorted({b["p_calibrated"] for b in calib["bins"] if b["n_total"] > 0})
        return {"count": len(vals), "values": vals}

    cal_old = compute_calibration_from_pairs(old_pairs, source="sim-old")
    cal_new = compute_calibration_from_pairs(new_pairs, source="sim-new")

    return {
        "enabled": True,
        "total": len(new_triples),
        "weights_new": new_w,
        "note": ("READ-ONLY. AUC maior + mais platôs = score discrimina melhor. "
                 "new_score renormaliza sobre componentes presentes (dado faltante "
                 "NÃO ancora em 50). Re-escalado pro range do score atual só p/ binning."),
        "auc_old": round(auc_old, 4) if auc_old is not None else None,
        "auc_new": round(auc_new, 4) if auc_new is not None else None,
        "auc_delta": (round(auc_new - auc_old, 4)
                      if (auc_old is not None and auc_new is not None) else None),
        "plateaus_old": plateaus(cal_old),
        "plateaus_new": plateaus(cal_new),
        "deciles_old": _deciles(old_triples),
        "deciles_new": _deciles(new_triples),
    }


async def compute_feature_analysis(days: int = 0) -> Dict[str, Any]:
    """READ-ONLY. Mede o poder preditivo de cada componente do score nos trades
    resolvidos. days<=0 = todo o histórico."""
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    conds = [RecommendationSnapshot.status.in_(RESOLVED_STATUSES)]
    if days and days > 0:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conds.append(RecommendationSnapshot.outcome_at >= since)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(and_(*conds))
        snaps = (await session.execute(stmt)).scalars().all()

    total = len(snaps)
    if total == 0:
        return {"enabled": True, "total": 0,
                "message": "Sem trades resolvidos ainda."}

    n_win = sum(1 for s in snaps if s.status in WIN_STATUSES)
    base_wr = round(100 * n_win / total, 1)

    results: List[Dict[str, Any]] = []
    for label, src, key in _FEATURES:
        pairs: List[Tuple[float, int]] = []       # (valor, win01)
        triples: List[Tuple[float, int, float]] = []  # (valor, win01, r)
        for s in snaps:
            if src == "col":
                val = getattr(s, key, None)
            else:
                val = (s.features or {}).get(key)
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if math.isnan(val) or math.isinf(val):
                continue
            win = 1 if s.status in WIN_STATUSES else 0
            r = s.realized_r if s.realized_r is not None else 0.0
            pairs.append((val, win))
            triples.append((val, win, float(r)))

        cov = len(pairs)
        if cov == 0:
            results.append({"feature": label, "coverage": 0, "coverage_pct": 0.0,
                            "auc": None, "pbis_r": None, "r_corr": None,
                            "note": "sem dados"})
            continue

        auc = _auc(pairs)
        pbis = _pearson([v for v, _ in pairs], [float(w) for _, w in pairs])
        rcorr = _pearson([v for v, _, _ in triples], [r for _, _, r in triples])
        results.append({
            "feature": label,
            "coverage": cov,
            "coverage_pct": round(100 * cov / total, 1),
            "auc": round(auc, 4) if auc is not None else None,
            "auc_strength": round(abs(auc - 0.5), 4) if auc is not None else None,
            "pbis_r": round(pbis, 4) if pbis is not None else None,
            "r_corr": round(rcorr, 4) if rcorr is not None else None,
            "quartiles": _quartiles(triples),
        })

    # rank por força do AUC (poder discriminante), None por último
    results.sort(key=lambda d: (d.get("auc_strength") is None, -(d.get("auc_strength") or 0)))

    return {
        "enabled": True,
        "total": total,
        "wins": n_win,
        "losses": total - n_win,
        "base_win_rate": base_wr,
        "days": days,
        "note": ("READ-ONLY. auc≈0.5 → ruído; |auc-0.5| = força discriminante. "
                 "coverage baixo confirma feature que falta muito (ancora em 50 no score)."),
        "features": results,
    }
