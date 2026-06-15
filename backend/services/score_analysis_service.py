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


def _quantile(sorted_vals: List[float], q: float) -> Optional[float]:
    """Quantil q∈[0,1] por interpolação linear. sorted_vals já ordenado asc."""
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    frac = idx - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac
    return sorted_vals[lo]


def _band_stats(rows: List[Tuple[float, int, float]], label: str) -> Dict[str, Any]:
    """rows = [(v2_score, win01, r)] de uma faixa de tier. Win-rate + avg_r + n."""
    n = len(rows)
    if n == 0:
        return {"tier": label, "n": 0, "win_rate": None, "avg_r": None,
                "score_lo": None, "score_hi": None}
    wins = sum(w for _, w, _ in rows)
    scores = [v for v, _, _ in rows]
    return {
        "tier": label, "n": n,
        "win_rate": round(100 * wins / n, 1),
        "avg_r": round(sum(r for _, _, r in rows) / n, 3),
        "score_lo": round(min(scores), 1), "score_hi": round(max(scores), 1),
    }


def _tierize(scored: List[Tuple[float, int, float]],
             c_aplus: float, c_a: float, c_b: float) -> Dict[str, List]:
    bands: Dict[str, List[Tuple[float, int, float]]] = {
        "A+": [], "A": [], "B": [], "rejeitado": []}
    for v, w, r in scored:
        if v >= c_aplus:
            bands["A+"].append((v, w, r))
        elif v >= c_a:
            bands["A"].append((v, w, r))
        elif v >= c_b:
            bands["B"].append((v, w, r))
        else:
            bands["rejeitado"].append((v, w, r))
    return bands


async def compute_tier_sim(
    c_aplus: float = 0.0, c_a: float = 0.0, c_b: float = 0.0, days: int = 0,
) -> Dict[str, Any]:
    """READ-ONLY. Re-deriva os cortes de tier (A+/A/B) sob o score V2.

    Por que: a execução real gate na TIER, e os cortes legados (75/65/52) estão
    calibrados pra distribuição do score LEGADO. A V2 muda essa distribuição →
    os cortes quebram. Esta sim re-pontua os trades resolvidos com `_compute_score_v2`
    (o MESMO helper de produção) e:
      • mostra a distribuição do score V2 (percentis);
      • se cortes não forem passados (0), DERIVA cortes que preservam o MIX
        A+/A/B atual (mesma proporção de hoje → não seca nem inunda execução);
      • compara win-rate/avg_r por tier LEGADA vs tier V2 proposta (a V2 é melhor
        se o gradiente A+→B for mais íngreme);
      • reporta a interação com os gates duros do A+ (mtf≥0.5, rr≥2.5): quantos
        dos A+ propostos passariam neles (pra decidir manter/derrubar os gates).
    NÃO altera execução, sizing, nem nada — é só medição."""
    if not DB_ENABLED:
        return {"enabled": False, "message": "Banco de dados não configurado."}

    from services.recommendation_service import _compute_score_v2

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

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # (v2_score, win, r) + metadados pra gate/legado
    scored: List[Tuple[float, int, float]] = []
    legacy_bands: Dict[str, List[Tuple[float, int, float]]] = {
        "A+": [], "A": [], "B": [], "outro": []}
    gate_meta: List[Tuple[float, Optional[float], Optional[float]]] = []  # (v2, mtf, rr)
    n_uncomputable = 0

    for s in snaps:
        feats = s.features or {}
        v2 = _compute_score_v2(
            conf_pct=_f(feats.get("confluence_pct")),
            adx_raw=_f(feats.get("adx")),
            funding_pct=_f(feats.get("funding_pct")),
        )
        if v2 is None:
            n_uncomputable += 1
            continue
        win = 1 if s.status in WIN_STATUSES else 0
        r = float(s.realized_r) if s.realized_r is not None else 0.0
        scored.append((v2, win, r))
        gate_meta.append((v2, _f(feats.get("mtf_score")), _f(s.risk_reward)))
        lt = s.tier if s.tier in ("A+", "A", "B") else "outro"
        legacy_bands[lt].append((v2, win, r))

    n = len(scored)
    if n == 0:
        return {"enabled": True, "total": len(snaps),
                "message": "Nenhum trade com score V2 computável."}

    sorted_v2 = sorted(v for v, _, _ in scored)

    # mix legado entre os que têm score V2 (proporção a preservar)
    n_aplus = len(legacy_bands["A+"])
    n_a = len(legacy_bands["A"])
    n_b = len(legacy_bands["B"])
    n_tiered = n_aplus + n_a + n_b
    auto = not (c_aplus or c_a or c_b)
    derived_from = "parâmetros manuais"
    if auto and n_tiered > 0:
        p_aplus = n_aplus / n_tiered
        p_a = n_a / n_tiered
        # corta nos percentis que reproduzem o mix atual
        c_aplus = round(_quantile(sorted_v2, 1.0 - p_aplus), 1)
        c_a = round(_quantile(sorted_v2, 1.0 - p_aplus - p_a), 1)
        c_b = round(sorted_v2[0], 1)   # preserva volume: tudo que pontua ≥ B
        derived_from = "mix A+/A/B atual (preserva volume de execução)"
    elif auto:
        # sem tier legada — usa tercis como fallback neutro
        c_aplus = round(_quantile(sorted_v2, 0.80), 1)
        c_a = round(_quantile(sorted_v2, 0.50), 1)
        c_b = round(sorted_v2[0], 1)
        derived_from = "percentis 80/50 (sem tier legada de referência)"

    v2_bands = _tierize(scored, c_aplus, c_a, c_b)

    # gate duro do A+ legado: quantos A+ propostos passam mtf≥0.5 E rr≥2.5
    aplus_meta = [(m, rr) for v, m, rr in gate_meta if v >= c_aplus]
    pass_gate = sum(1 for m, rr in aplus_meta
                    if (m is not None and m >= 0.5) and (rr is not None and rr >= 2.5))
    n_aplus_prop = len(aplus_meta)

    def spread(bands_map, order):
        return [_band_stats(bands_map[k], k) for k in order]

    return {
        "enabled": True,
        "total": n,
        "uncomputable": n_uncomputable,
        "days": days,
        "derived_from": derived_from,
        "proposed_cutoffs": {"A+": c_aplus, "A": c_a, "B": c_b},
        "v2_distribution": {
            "min": round(sorted_v2[0], 1),
            "p10": round(_quantile(sorted_v2, 0.10), 1),
            "p25": round(_quantile(sorted_v2, 0.25), 1),
            "p50": round(_quantile(sorted_v2, 0.50), 1),
            "p75": round(_quantile(sorted_v2, 0.75), 1),
            "p90": round(_quantile(sorted_v2, 0.90), 1),
            "max": round(sorted_v2[-1], 1),
        },
        "legacy_tier_mix": {"A+": n_aplus, "A": n_a, "B": n_b, "outro": len(legacy_bands["outro"])},
        # win-rate/avg_r por TIER LEGADA (cada trade pelo tier que recebeu na época)
        "by_legacy_tier": spread(legacy_bands, ["A+", "A", "B"]),
        # win-rate/avg_r por TIER V2 proposta (re-tierizado pelos cortes acima)
        "by_v2_tier": spread(v2_bands, ["A+", "A", "B", "rejeitado"]),
        "aplus_hard_gate": {
            "proposed_aplus": n_aplus_prop,
            "pass_mtf0.5_rr2.5": pass_gate,
            "pass_pct": round(100 * pass_gate / n_aplus_prop, 1) if n_aplus_prop else None,
            "note": ("Se poucos A+ passam os gates mtf≥0.5/rr≥2.5, manter os gates "
                     "esvazia o A+ sob a V2 → considerar afrouxar/derrubar (MTF/RR "
                     "não são preditivos no score V2)."),
        },
        "note": ("READ-ONLY. Compare by_v2_tier vs by_legacy_tier: a V2 é melhor se "
                 "o gradiente de win_rate A+→B for mais íngreme (separa ganhador). "
                 "Cortes derivados preservam o mix atual (mesmo volume de execução)."),
    }
