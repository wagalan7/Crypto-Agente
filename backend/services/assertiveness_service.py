"""
Assertiveness Service — agregação READ-ONLY pro "painel de assertividade".

Responde, num único lugar, "o quão confiável o bot está sendo?" cruzando:
  1. real_money  — trades reais source=auto resolvidos (dinheiro de verdade):
                   win-rate, TP1/TP2 hit, expectancy em R, P&L USD, por status.
  2. shadow      — recommendation_snapshots resolvidos (amostra maior, mesma
                   base que calibra P(TP1)): win-rate, TP1/TP2 hit, expectancy R.
  3. gates       — contadores PERSISTIDOS de skip por gate (skip_reason_stats),
                   sobrevivem redeploy → "qual gate mais barrou na janela?".
  4. calibration — maturidade da calibração (>= MIN_SAMPLE_TOTAL resolvidos).

Tudo fail-soft: qualquer erro de DB vira seção vazia, nunca derruba a API.
Não escreve nada, não toca no loop de execução — só lê e soma.
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select, func, and_, not_

from services import calibration_service as _calib

log = logging.getLogger(__name__)

# Status de trade real (real_trades) — espelha real_trade.py
_REAL_TP1_HIT = ("closed_tp1", "closed_tp2", "closed_be")  # tocou TP1 em algum momento
_REAL_TP2_HIT = ("closed_tp2",)

# Status de snapshot (recommendation_snapshots) — espelha calibration_service
_SNAP_WIN = ("won_tp1", "won_tp1_be", "won_tp2")
_SNAP_TP2 = ("won_tp2",)
_SNAP_RESOLVED = _SNAP_WIN + ("lost", "expired")


async def _real_money_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.real_trade import RealTrade
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = {
        "count": 0, "wins": 0, "losses": 0, "win_rate_pct": None,
        "avg_r": None, "expectancy_r": None, "sum_pnl_usd": 0.0,
        "tp1_hit_rate_pct": None, "tp2_hit_rate_pct": None,
        "by_status": {},
    }
    try:
        async with get_session() as session:
            stmt = (
                select(RealTrade)
                .where(RealTrade.source == "auto")
                .where(RealTrade.status != "open")
                .where(RealTrade.opened_at >= since)
            )
            rows = list((await session.execute(stmt)).scalars().all())
    except Exception as e:
        log.warning(f"[assertiveness] real_money read falhou: {e}")
        return out
    n = len(rows)
    if n == 0:
        return out
    by_status: dict[str, int] = defaultdict(int)
    rs = []
    pnl_sum = 0.0
    tp1_hit = 0
    tp2_hit = 0
    wins = 0
    for t in rows:
        by_status[t.status] += 1
        r = float(t.realized_r) if t.realized_r is not None else 0.0
        rs.append(r)
        pnl_sum += float(t.pnl_usd or 0)
        if r > 0:
            wins += 1
        if t.status in _REAL_TP1_HIT:
            tp1_hit += 1
        if t.status in _REAL_TP2_HIT:
            tp2_hit += 1
    avg_r = sum(rs) / n
    out.update({
        "count": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(avg_r, 3),
        "sum_pnl_usd": round(pnl_sum, 2),
        "tp1_hit_rate_pct": round(tp1_hit / n * 100, 1),
        "tp2_hit_rate_pct": round(tp2_hit / n * 100, 1),
        "by_status": dict(by_status),
    })
    return out


async def _shadow_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out = {
        "count": 0, "wins": 0, "win_rate_pct": None,
        "tp1_hit_rate_pct": None, "tp2_hit_rate_pct": None,
        "avg_r": None, "expectancy_r": None, "by_status": {},
    }
    try:
        async with get_session() as session:
            stmt = (
                select(
                    RecommendationSnapshot.status,
                    RecommendationSnapshot.realized_r,
                )
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                # DE-POLUIÇÃO (fonte única com calibration_service): exclui voids
                # — 'expired' que nunca teve avaliação justa (no-data / flip).
                # Mantém a win-rate do painel honesta.
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )
            rows = list((await session.execute(stmt)).all())
    except Exception as e:
        log.warning(f"[assertiveness] shadow read falhou: {e}")
        return out
    n = len(rows)
    if n == 0:
        return out
    by_status: dict[str, int] = defaultdict(int)
    wins = 0
    tp1_hit = 0
    tp2_hit = 0
    rs = []
    for status, r in rows:
        by_status[status] += 1
        rs.append(float(r) if r is not None else 0.0)
        if status in _SNAP_WIN:
            wins += 1
            tp1_hit += 1
        if status in _SNAP_TP2:
            tp2_hit += 1
    avg_r = sum(rs) / n
    out.update({
        "count": n,
        "wins": wins,
        "win_rate_pct": round(wins / n * 100, 1),
        "tp1_hit_rate_pct": round(tp1_hit / n * 100, 1),
        "tp2_hit_rate_pct": round(tp2_hit / n * 100, 1),
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(avg_r, 3),
        "by_status": dict(by_status),
    })
    return out


async def _gate_stats(days: int) -> Dict[str, Any]:
    from db import get_session
    from models.skip_reason_stat import SkipReasonStat
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    out = {"window_days": days, "total_skips": 0, "items": []}
    try:
        async with get_session() as session:
            stmt = (
                select(
                    SkipReasonStat.gate,
                    func.sum(SkipReasonStat.count).label("total"),
                    func.max(SkipReasonStat.last_seen).label("last_seen"),
                )
                .where(SkipReasonStat.day >= since)
                .group_by(SkipReasonStat.gate)
                .order_by(func.sum(SkipReasonStat.count).desc())
            )
            grouped = list((await session.execute(stmt)).all())
            # último motivo/símbolo por gate (linha mais recente)
            last_stmt = (
                select(SkipReasonStat)
                .where(SkipReasonStat.day >= since)
                .order_by(SkipReasonStat.last_seen.desc())
            )
            recent = list((await session.execute(last_stmt)).scalars().all())
    except Exception as e:
        log.warning(f"[assertiveness] gate read falhou: {e}")
        return out
    last_by_gate: dict[str, Any] = {}
    for row in recent:
        if row.gate not in last_by_gate:
            last_by_gate[row.gate] = row
    items = []
    total = 0
    for gate, cnt, last_seen in grouped:
        cnt_i = int(cnt or 0)
        total += cnt_i
        ex = last_by_gate.get(gate)
        items.append({
            "gate": gate,
            "count": cnt_i,
            "last_reason": ex.last_reason if ex else None,
            "last_symbol": ex.last_symbol if ex else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })
    out["items"] = items
    out["total_skips"] = total
    return out


def _norm_base(symbol: str, allow: set[str]) -> str:
    """Extrai a base de 'GAS/USDT:USDT' → 'GAS'. Normaliza prefixo 1000 quando
    a forma sem prefixo está na allowlist (igual ao gate de execução)."""
    b = (symbol or "").split("/")[0].strip().upper()
    if b.startswith("1000") and b[4:] in allow:
        return b[4:]
    return b


async def _opportunity_cost(days: int) -> Dict[str, Any]:
    """#3 — Custo de oportunidade da allowlist. Entre os snapshots RESOLVIDOS
    que eram candidatos de execução (tier A/A+ e score >= SCORE_MIN), compara o
    desempenho das bases DENTRO vs FORA da allowlist. Responde, com número, se a
    allowlist deixou dinheiro na mesa ou protegeu de trades ruins.

    Caveat: 'fora' inclui perps de ação tokenizada (EWZ/HOOD/…) e moedas magras
    que o bot nunca executaria — então o bucket 'fora' é teto otimista. Mesmo
    assim, win-rate/avg_R abaixo do 'dentro' já mata o argumento de expandir."""
    out = {
        "enabled": True, "window_days": days, "score_min": None,
        "in_allowlist": {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": None},
        "out_allowlist": {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": None},
        "verdict": None,
    }
    try:
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from services.shadow_trade_service import get_exec_allowlist, SCORE_MIN
    except Exception as e:
        log.warning(f"[assertiveness] opportunity_cost import falhou: {e}")
        return out
    allow = set(get_exec_allowlist() or set())
    out["score_min"] = SCORE_MIN
    if not allow:
        out["verdict"] = "allowlist vazia — sem restrição de execução"
        return out
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with get_session() as session:
            stmt = (
                select(
                    RecommendationSnapshot.symbol,
                    RecommendationSnapshot.status,
                    RecommendationSnapshot.realized_r,
                )
                .where(RecommendationSnapshot.tier.in_(("A", "A+")))
                .where(RecommendationSnapshot.score >= SCORE_MIN)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )
            rows = list((await session.execute(stmt)).all())
    except Exception as e:
        log.warning(f"[assertiveness] opportunity_cost read falhou: {e}")
        return out

    def _agg(items):
        n = len(items)
        if n == 0:
            return {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": None}
        rs = [float(r) if r is not None else 0.0 for _, _, r in items]
        wins = sum(1 for _, st, _ in items if st in _SNAP_WIN)
        return {
            "count": n,
            "win_rate_pct": round(wins / n * 100, 1),
            "avg_r": round(sum(rs) / n, 3),
            "sum_r": round(sum(rs), 2),
        }

    inside, outside = [], []
    for sym, st, r in rows:
        (inside if _norm_base(sym, allow) in allow else outside).append((sym, st, r))
    out["in_allowlist"] = _agg(inside)
    out["out_allowlist"] = _agg(outside)
    # Veredito legível
    ai = out["in_allowlist"]["avg_r"]
    ao = out["out_allowlist"]["avg_r"]
    if ao is None:
        out["verdict"] = "nenhum candidato fora da allowlist na janela — allowlist não está custando trades"
    elif ai is None:
        out["verdict"] = "nenhum candidato dentro da allowlist resolvido na janela"
    elif ao > ai:
        out["verdict"] = (
            f"FORA rendeu mais (avg_R {ao:+.2f} vs {ai:+.2f} dentro) em {out['out_allowlist']['count']} setups "
            f"— allowlist pode estar deixando edge na mesa (checar se são cripto líquida, não ação/magra)"
        )
    else:
        out["verdict"] = (
            f"DENTRO rendeu igual ou mais (avg_R {ai:+.2f} vs {ao:+.2f} fora) "
            f"— allowlist NÃO está custando; mantém a seletividade"
        )
    return out


async def _funnel(days: int, gates: Dict[str, Any]) -> Dict[str, Any]:
    """#1 — Funil de execução consolidado (janela = gate_days). Amarra os
    candidatos tier A/A+ que chegaram ao loop com os drop-offs por gate e os
    executados de verdade. candidates ≈ executed + total_skips."""
    out = {"window_days": days, "candidates": None, "executed": 0,
           "exec_rate_pct": None, "stages": []}
    executed = 0
    try:
        from db import get_session
        from models.real_trade import RealTrade
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with get_session() as session:
            executed = int((await session.execute(
                select(func.count(RealTrade.id))
                .where(RealTrade.source == "auto")
                .where(RealTrade.opened_at >= since)
            )).scalar() or 0)
    except Exception as e:
        log.warning(f"[assertiveness] funnel executed read falhou: {e}")
    total_skips = int(gates.get("total_skips") or 0)
    candidates = total_skips + executed
    out["executed"] = executed
    out["candidates"] = candidates
    out["exec_rate_pct"] = round(executed / candidates * 100, 1) if candidates else None
    # Etapas ordenadas: cada gate como um degrau de queda, executados no fim.
    stages = [{"stage": "candidatos_tierA", "count": candidates}]
    for it in gates.get("items", []):
        stages.append({"stage": f"barrado:{it['gate']}", "count": it["count"]})
    stages.append({"stage": "executados", "count": executed})
    out["stages"] = stages
    return out


async def _per_coin_scorecard(days: int, limit: int = 40) -> Dict[str, Any]:
    """#7 — Scorecard por moeda. Para cada base, cruza o desempenho de DINHEIRO
    REAL (real_trades source=auto, verdade-terreno mas amostra pequena) com o do
    SHADOW (recommendation_snapshots resolvidos, amostra grande). Serve pra
    responder, moeda a moeda, 'quem puxa lucro e quem dreno?' — insumo direto
    pra decidir allowlist/rotação. Ordena por R real somado (depois shadow).

    Fail-soft: qualquer erro vira lista vazia, nunca derruba a API."""
    out: Dict[str, Any] = {"window_days": days, "coins": [], "total_coins": 0}
    real_by: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "rs": [], "pnl": 0.0, "last": None})
    shad_by: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "rs": []})
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # base normalizada usa a allowlist como referência de des-prefixo 1000
    try:
        from services.shadow_trade_service import get_exec_allowlist
        allow = set(get_exec_allowlist() or set())
    except Exception:
        allow = set()
    # 1) dinheiro real
    try:
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            rows = list((await session.execute(
                select(RealTrade.symbol, RealTrade.status, RealTrade.realized_r,
                       RealTrade.pnl_usd, RealTrade.opened_at)
                .where(RealTrade.source == "auto")
                .where(RealTrade.status != "open")
                .where(RealTrade.opened_at >= since)
            )).all())
        for sym, st, r, pnl, opened in rows:
            b = _norm_base(sym, allow)
            d = real_by[b]
            d["n"] += 1
            rv = float(r) if r is not None else 0.0
            d["rs"].append(rv)
            if rv > 0:
                d["wins"] += 1
            d["pnl"] += float(pnl or 0)
            iso = opened.isoformat() if opened else None
            if iso and (d["last"] is None or iso > d["last"]):
                d["last"] = iso
    except Exception as e:
        log.warning(f"[assertiveness] scorecard real read falhou: {e}")
    # 2) shadow (amostra grande)
    try:
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            srows = list((await session.execute(
                select(RecommendationSnapshot.symbol,
                       RecommendationSnapshot.status,
                       RecommendationSnapshot.realized_r)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )).all())
        for sym, st, r in srows:
            b = _norm_base(sym, allow)
            d = shad_by[b]
            d["n"] += 1
            d["rs"].append(float(r) if r is not None else 0.0)
            if st in _SNAP_WIN:
                d["wins"] += 1
    except Exception as e:
        log.warning(f"[assertiveness] scorecard shadow read falhou: {e}")

    bases = set(real_by) | set(shad_by)
    coins = []
    for b in bases:
        rm = real_by.get(b)
        sh = shad_by.get(b)
        entry: Dict[str, Any] = {"base": b, "in_allowlist": b in allow}
        if rm and rm["n"]:
            n = rm["n"]
            entry["real"] = {
                "count": n, "win_rate_pct": round(rm["wins"] / n * 100, 1),
                "avg_r": round(sum(rm["rs"]) / n, 3), "sum_r": round(sum(rm["rs"]), 2),
                "sum_pnl_usd": round(rm["pnl"], 2), "last_opened_at": rm["last"],
            }
        else:
            entry["real"] = {"count": 0, "win_rate_pct": None, "avg_r": None,
                             "sum_r": 0.0, "sum_pnl_usd": 0.0, "last_opened_at": None}
        if sh and sh["n"]:
            n = sh["n"]
            entry["shadow"] = {
                "count": n, "win_rate_pct": round(sh["wins"] / n * 100, 1),
                "avg_r": round(sum(sh["rs"]) / n, 3), "sum_r": round(sum(sh["rs"]), 2),
            }
        else:
            entry["shadow"] = {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": 0.0}
        coins.append(entry)
    # ordena: maior R real somado primeiro; empate desempata por R shadow somado
    coins.sort(key=lambda c: (c["real"]["sum_r"], c["shadow"]["sum_r"]), reverse=True)
    out["total_coins"] = len(coins)
    out["coins"] = coins[:limit]
    return out


async def _calibration_audit(days: int) -> Dict[str, Any]:
    """#5 — Auditoria de calibração. A calibração (calibration_service) aprende
    com TODO o histórico um mapa score→P(TP1). Esta auditoria pega os snapshots
    RESOLVIDOS RECENTES (janela `days`) e, por bin de score, compara o P(TP1)
    PREVISTO pelo modelo (p_calibrated) com o win-rate REALIZADO recente. O
    descasamento é DRIFT: o modelo diz 70% mas a realidade recente é 50%.

    Sinaliza por bin: over-confident (previu mais do que entregou),
    under-confident (entregou mais), ou ok. Global: Brier score + gap médio.
    Fail-soft / read-only — só lê e compara."""
    out: Dict[str, Any] = {
        "enabled": False, "window_days": days, "reason": None,
        "n_recent": 0, "expected_wins": None, "actual_wins": None,
        "actual_rate_pct": None, "predicted_rate_pct": None,
        "calib_gap_pct": None, "brier": None, "verdict": None, "bins": [],
    }
    try:
        from services import calibration_service as cs
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
    except Exception as e:
        out["reason"] = f"import falhou: {e}"
        return out
    calib = await cs.get_calibration()
    if not calib or not calib.get("bins"):
        out["reason"] = "calibração imatura (sem mapa score→P(TP1) ainda)"
        return out
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with get_session() as session:
            rows = list((await session.execute(
                select(RecommendationSnapshot.score, RecommendationSnapshot.status)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )).all())
    except Exception as e:
        out["reason"] = f"read falhou: {e}"
        return out
    if not rows:
        out["reason"] = "sem snapshots resolvidos na janela"
        return out
    bins = calib["bins"]
    nb = len(bins)
    b_total = [0] * nb
    b_wins = [0] * nb
    exp_wins = 0.0
    brier_sum = 0.0
    n = 0
    for sc, st in rows:
        bi = cs._bin_index(float(sc))
        if bi < 0:
            continue
        p_pred = float(bins[bi].get("p_calibrated") or 0.0)
        y = 1.0 if st in cs.WIN_STATUSES else 0.0
        b_total[bi] += 1
        b_wins[bi] += int(y)
        exp_wins += p_pred
        brier_sum += (p_pred - y) ** 2
        n += 1
    if n == 0:
        out["reason"] = "scores recentes fora dos bins"
        return out
    actual_wins = sum(b_wins)
    actual_rate = actual_wins / n
    predicted_rate = exp_wins / n
    gap = actual_rate - predicted_rate  # >0 = modelo conservador; <0 = otimista
    bins_out = []
    worst = None  # (abs_gap, label) com amostra suficiente
    for i in range(nb):
        nt = b_total[i]
        if nt == 0:
            continue
        p_pred = round(float(bins[i].get("p_calibrated") or 0.0), 4)
        p_act = b_wins[i] / nt
        bgap = p_act - p_pred
        flag = "ok"
        # só rotula drift com amostra mínima e gap material
        if nt >= 8 and abs(bgap) >= 0.15:
            flag = "otimista" if bgap < 0 else "conservador"
            if worst is None or abs(bgap) > worst[0]:
                worst = (abs(bgap), bins[i].get("label"), flag, round(p_pred*100,1), round(p_act*100,1))
        bins_out.append({
            "label": bins[i].get("label"), "n_recent": nt,
            "predicted_pct": round(p_pred * 100, 1),
            "actual_pct": round(p_act * 100, 1),
            "gap_pct": round(bgap * 100, 1), "flag": flag,
        })
    if worst is not None:
        verdict = (f"DRIFT no bin {worst[1]}: modelo previu {worst[3]}% mas realizou "
                   f"{worst[4]}% ({worst[2]}) — vale revisar calibração se persistir")
    elif abs(gap) >= 0.10:
        verdict = (f"gap global {gap*100:+.1f}pp (modelo "
                   f"{'conservador' if gap>0 else 'otimista'}) mas nenhum bin com drift material")
    else:
        verdict = f"calibração fiel: gap global {gap*100:+.1f}pp, sem drift por bin"
    out.update({
        "enabled": True, "n_recent": n,
        "expected_wins": round(exp_wins, 1), "actual_wins": actual_wins,
        "actual_rate_pct": round(actual_rate * 100, 1),
        "predicted_rate_pct": round(predicted_rate * 100, 1),
        "calib_gap_pct": round(gap * 100, 1),
        "brier": round(brier_sum / n, 4),
        "verdict": verdict, "bins": bins_out,
    })
    return out


async def _equity_curve(days: int) -> Dict[str, Any]:
    """#8 — Curva de equity de DINHEIRO REAL. Série temporal acumulada (por
    closed_at) dos trades reais source=auto resolvidos: R acumulado, P&L USD
    acumulado, e o drawdown máximo (pico→vale) de cada um. Permite ver a
    trajetória — não só o número final — e quantificar o pior soluço.

    Fail-soft: erro de DB → curva vazia. Read-only."""
    out: Dict[str, Any] = {
        "window_days": days, "points": [],
        "final_cum_r": 0.0, "final_cum_pnl_usd": 0.0,
        "peak_cum_r": 0.0, "max_drawdown_r": 0.0,
        "peak_cum_pnl_usd": 0.0, "max_drawdown_usd": 0.0,
        "current_streak": 0,  # >0 = vitórias seguidas; <0 = derrotas seguidas
    }
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            rows = list((await session.execute(
                select(RealTrade.symbol, RealTrade.realized_r,
                       RealTrade.pnl_usd, RealTrade.closed_at)
                .where(RealTrade.source == "auto")
                .where(RealTrade.status != "open")
                .where(RealTrade.closed_at.is_not(None))
                .where(RealTrade.closed_at >= since)
                .order_by(RealTrade.closed_at.asc())
            )).all())
    except Exception as e:
        log.warning(f"[assertiveness] equity_curve read falhou: {e}")
        return out
    if not rows:
        return out
    cum_r = 0.0
    cum_pnl = 0.0
    peak_r = 0.0
    peak_pnl = 0.0
    max_dd_r = 0.0
    max_dd_pnl = 0.0
    streak = 0
    points = []
    for sym, r, pnl, closed in rows:
        rv = float(r) if r is not None else 0.0
        pv = float(pnl or 0)
        cum_r += rv
        cum_pnl += pv
        peak_r = max(peak_r, cum_r)
        peak_pnl = max(peak_pnl, cum_pnl)
        max_dd_r = max(max_dd_r, peak_r - cum_r)
        max_dd_pnl = max(max_dd_pnl, peak_pnl - cum_pnl)
        # streak: reinicia ao trocar de sinal
        if rv > 0:
            streak = streak + 1 if streak > 0 else 1
        elif rv < 0:
            streak = streak - 1 if streak < 0 else -1
        points.append({
            "t": closed.isoformat() if closed else None,
            "base": _norm_base(sym, set()),
            "r": round(rv, 3), "pnl_usd": round(pv, 2),
            "cum_r": round(cum_r, 3), "cum_pnl_usd": round(cum_pnl, 2),
        })
    out.update({
        "points": points,
        "final_cum_r": round(cum_r, 3),
        "final_cum_pnl_usd": round(cum_pnl, 2),
        "peak_cum_r": round(peak_r, 3),
        "max_drawdown_r": round(max_dd_r, 3),
        "peak_cum_pnl_usd": round(peak_pnl, 2),
        "max_drawdown_usd": round(max_dd_pnl, 2),
        "current_streak": streak,
    })
    return out


async def _directional_audit(days: int) -> Dict[str, Any]:
    """#A — Auditoria de edge por DIREÇÃO (long vs short). Cruza dinheiro REAL
    (real_trades.side) com o SHADOW (recommendation_snapshots.direction) e mede,
    de cada lado, win-rate / avg_R / sum_R. Responde com número a pergunta do
    momento: 'os longs estão sangrando mais que os shorts?'. Funciona em TODO o
    histórico (side/direction são persistidos). Read-only / fail-soft."""
    out: Dict[str, Any] = {
        "window_days": days,
        "real": {"long": None, "short": None},
        "shadow": {"long": None, "short": None},
        "verdict": None,
    }
    since = datetime.now(timezone.utc) - timedelta(days=days)

    def _agg_real(items):
        n = len(items)
        if n == 0:
            return {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": 0.0,
                    "sum_pnl_usd": 0.0, "tp1_hit_rate_pct": None}
        rs = [float(r) if r is not None else 0.0 for _, r, _, _ in items]
        wins = sum(1 for _, r, _, _ in items if (r or 0) > 0)
        tp1 = sum(1 for _, _, st, _ in items if st in _REAL_TP1_HIT)
        pnl = sum(float(p or 0) for _, _, _, p in items)
        return {"count": n, "win_rate_pct": round(wins / n * 100, 1),
                "avg_r": round(sum(rs) / n, 3), "sum_r": round(sum(rs), 2),
                "sum_pnl_usd": round(pnl, 2), "tp1_hit_rate_pct": round(tp1 / n * 100, 1)}

    def _agg_shadow(items):
        n = len(items)
        if n == 0:
            return {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": 0.0}
        rs = [float(r) if r is not None else 0.0 for st, r in items]
        wins = sum(1 for st, _ in items if st in _SNAP_WIN)
        return {"count": n, "win_rate_pct": round(wins / n * 100, 1),
                "avg_r": round(sum(rs) / n, 3), "sum_r": round(sum(rs), 2)}

    # 1) dinheiro real, por side
    try:
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            rows = list((await session.execute(
                select(RealTrade.side, RealTrade.realized_r,
                       RealTrade.status, RealTrade.pnl_usd)
                .where(RealTrade.source == "auto")
                .where(RealTrade.status != "open")
                .where(RealTrade.opened_at >= since)
            )).all())
        rl = {"long": [], "short": []}
        for side, r, st, pnl in rows:
            key = "long" if str(side).lower() == "long" else "short"
            rl[key].append((side, r, st, pnl))
        out["real"]["long"] = _agg_real(rl["long"])
        out["real"]["short"] = _agg_real(rl["short"])
    except Exception as e:
        log.warning(f"[assertiveness] directional real read falhou: {e}")

    # 2) shadow, por direction
    try:
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            srows = list((await session.execute(
                select(RecommendationSnapshot.direction,
                       RecommendationSnapshot.status,
                       RecommendationSnapshot.realized_r)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )).all())
        sl = {"long": [], "short": []}
        for direction, st, r in srows:
            key = "long" if str(direction).lower() == "long" else "short"
            sl[key].append((st, r))
        out["shadow"]["long"] = _agg_shadow(sl["long"])
        out["shadow"]["short"] = _agg_shadow(sl["short"])
    except Exception as e:
        log.warning(f"[assertiveness] directional shadow read falhou: {e}")

    # Veredito: prioriza o sinal de DINHEIRO REAL; usa shadow como reforço.
    rlong = (out["real"]["long"] or {})
    rshort = (out["real"]["short"] or {})
    al, ash = rlong.get("avg_r"), rshort.get("avg_r")
    nl, ns = rlong.get("count", 0), rshort.get("count", 0)
    if nl and ns and al is not None and ash is not None:
        if al < 0 <= ash:
            out["verdict"] = (f"LONGS sangrando: avg_R long {al:+.2f} ({nl} trades) vs "
                              f"short {ash:+.2f} ({ns}) — viés direcional desfavorável a long")
        elif ash < 0 <= al:
            out["verdict"] = (f"SHORTS sangrando: avg_R short {ash:+.2f} ({ns}) vs "
                              f"long {al:+.2f} ({nl})")
        else:
            diff = al - ash
            lado = "long" if diff > 0 else "short"
            out["verdict"] = (f"sem viés direcional forte: long {al:+.2f} ({nl}) vs "
                              f"short {ash:+.2f} ({ns}); leve vantagem {lado}")
    elif nl and al is not None and not ns:
        out["verdict"] = f"só longs reais na janela (avg_R {al:+.2f}, {nl}) — sem short pra comparar"
    elif ns and ash is not None and not nl:
        out["verdict"] = f"só shorts reais na janela (avg_R {ash:+.2f}, {ns}) — sem long pra comparar"
    else:
        out["verdict"] = "amostra real insuficiente por direção — ver shadow"
    return out


async def _regime_audit(days: int) -> Dict[str, Any]:
    """#A — Auditoria de edge por REGIME de mercado na ENTRADA. Lê o rótulo de
    regime persistido em snapshot.features['regime'] (NORMAL / RISK_OFF /
    ALT_DANGER / BTC_DOMINANT / ALT_RISK_OFF) e mede win-rate / avg_R por regime.

    IMPORTANTE: o regime só passou a ser gravado a partir do deploy desta feature
    — snapshots antigos saem como 'n/d' e a amostra acumula com o tempo. Por isso
    reporta `coverage` (quantos resolvidos já têm regime tagueado). Read-only."""
    out: Dict[str, Any] = {
        "window_days": days, "regimes": [], "coverage_pct": None,
        "n_tagged": 0, "n_total": 0, "note": None,
    }
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            rows = list((await session.execute(
                select(RecommendationSnapshot.status,
                       RecommendationSnapshot.realized_r,
                       RecommendationSnapshot.features)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )).all())
    except Exception as e:
        log.warning(f"[assertiveness] regime_audit read falhou: {e}")
        return out
    n_total = len(rows)
    by: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "rs": []})
    n_tagged = 0
    for st, r, feats in rows:
        label = None
        if isinstance(feats, dict):
            label = feats.get("regime")
        if not label:
            continue
        n_tagged += 1
        d = by[label]
        d["n"] += 1
        d["rs"].append(float(r) if r is not None else 0.0)
        if st in _SNAP_WIN:
            d["wins"] += 1
    regimes = []
    for label, d in by.items():
        n = d["n"]
        regimes.append({
            "regime": label, "count": n,
            "win_rate_pct": round(d["wins"] / n * 100, 1),
            "avg_r": round(sum(d["rs"]) / n, 3),
            "sum_r": round(sum(d["rs"]), 2),
        })
    regimes.sort(key=lambda x: x["sum_r"], reverse=True)
    out["regimes"] = regimes
    out["n_total"] = n_total
    out["n_tagged"] = n_tagged
    out["coverage_pct"] = round(n_tagged / n_total * 100, 1) if n_total else None
    if n_tagged == 0:
        out["note"] = ("regime ainda não tagueado em nenhum resolvido — começa a "
                       "acumular a partir deste deploy (snapshots antigos = n/d)")
    elif n_tagged < n_total:
        out["note"] = (f"amostra acumulando: {n_tagged}/{n_total} resolvidos já têm "
                       f"regime tagueado (resto é pré-deploy)")
    return out


async def _gate_counterfactual(days: int) -> Dict[str, Any]:
    """#B — Avaliador CONTRAFACTUAL dos gates protetivos que estão OFF. Para cada
    gate desligado, mede sobre o histórico resolvido 'o que ele teria feito',
    pra transformar a decisão de ligar em EVIDÊNCIA, não palpite.

    HONESTIDADE sobre o que é reconstruível com dado persistido:
      • quality_edge   — só a condição de SCORE (banda marginal) é reconstruível;
                         o filtro de edge_score NÃO é persistido → reportamos a
                         coorte da banda como COTA SUPERIOR do que o gate barraria.
      • regime_sizing  — precisa do regime-na-entrada, gravado só a partir do
                         deploy #A → amostra acumula (coverage informado).
      • pre_tp1_protect— precisa do caminho intra-trade pré-TP1 (não persistido)
                         → não reconstruível por outcome; só contexto de stops.
    Read-only / fail-soft."""
    out: Dict[str, Any] = {"window_days": days,
                           "quality_edge": {}, "regime_sizing": {}, "pre_tp1_protect": {}}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        from services.shadow_trade_service import (
            SCORE_MIN, QUALITY_EDGE_MARGIN, QUALITY_EDGE_MIN,
            QUALITY_EDGE_GATE_ENABLED, REGIME_SIZING_ENABLED, REGIME_SIZE_MULT_ALT_LONG,
        )
        from services.regime_service import is_btc_symbol
    except Exception as e:
        log.warning(f"[assertiveness] gate_counterfactual import falhou: {e}")
        return out

    # Snapshots resolvidos da janela (score/direction/symbol/features/status/R)
    rows = []
    try:
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            rows = list((await session.execute(
                select(RecommendationSnapshot.symbol, RecommendationSnapshot.direction,
                       RecommendationSnapshot.score, RecommendationSnapshot.status,
                       RecommendationSnapshot.realized_r, RecommendationSnapshot.features)
                .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                .where(_calib._not_fast_void())
                .where(RecommendationSnapshot.outcome_at >= since)
            )).all())
    except Exception as e:
        log.warning(f"[assertiveness] gate_counterfactual read falhou: {e}")

    def _agg(rs_list):
        n = len(rs_list)
        if n == 0:
            return {"count": 0, "win_rate_pct": None, "avg_r": None, "sum_r": 0.0}
        rs = [r for r, _ in rs_list]
        wins = sum(1 for _, w in rs_list if w)
        return {"count": n, "win_rate_pct": round(wins / n * 100, 1),
                "avg_r": round(sum(rs) / n, 3), "sum_r": round(sum(rs), 2)}

    # ── (1) quality_edge — coorte da banda marginal de score ───────────────────
    band_lo, band_hi = SCORE_MIN, SCORE_MIN + QUALITY_EDGE_MARGIN
    band = [(float(r or 0), st in _SNAP_WIN)
            for sym, d, sc, st, r, f in rows
            if sc is not None and band_lo <= float(sc) < band_hi]
    qe = _agg(band)
    qe_sum = qe["sum_r"]
    if qe["count"] < 8:
        qe_verdict = f"amostra pequena ({qe['count']}) na banda [{band_lo:.0f},{band_hi:.0f}) — inconclusivo"
    elif qe_sum < 0:
        qe_verdict = (f"banda marginal somou {qe_sum:+.1f}R (avg {qe['avg_r']:+.2f}, {qe['count']} setups) "
                      f"— LIGAR o gate provavelmente protege (cota superior; gate corta subconjunto sem edge)")
    else:
        qe_verdict = (f"banda marginal é lucrativa ({qe_sum:+.1f}R, avg {qe['avg_r']:+.2f}, {qe['count']}) "
                      f"— ligar o gate cortaria setups bons; NÃO recomendado agora")
    out["quality_edge"] = {
        "enabled_now": QUALITY_EDGE_GATE_ENABLED,
        "score_band": f"[{band_lo:.0f},{band_hi:.0f})",
        "edge_min_required": QUALITY_EDGE_MIN,
        "cohort_score_band": qe,
        "note": "edge_score não é persistido — coorte é só pela condição de score (cota superior do bloqueio)",
        "verdict": qe_verdict,
    }

    # ── (2) regime_sizing — alt longs sob regime de downgrade (tag #A) ──────────
    DOWNGRADE_REGIMES = {"BTC_DOMINANT", "ALT_RISK_OFF"}
    tagged = 0
    cohort = []
    for sym, d, sc, st, r, f in rows:
        label = f.get("regime") if isinstance(f, dict) else None
        if label:
            tagged += 1
        if (label in DOWNGRADE_REGIMES and str(d).lower() == "long"
                and not is_btc_symbol(sym or "")):
            cohort.append((float(r or 0), st in _SNAP_WIN))
    rs_agg = _agg(cohort)
    # Projeção: ao escalar o size por REGIME_SIZE_MULT_ALT_LONG, o R da coorte
    # escala linear → delta = (mult-1)*sum_r (negativo se coorte lucrativa).
    proj_delta = round((REGIME_SIZE_MULT_ALT_LONG - 1.0) * rs_agg["sum_r"], 2)
    if tagged == 0:
        rs_verdict = "nenhum resolvido com regime tagueado ainda — aguardando amostra (deploy #A)"
    elif rs_agg["count"] == 0:
        rs_verdict = f"{tagged} resolvidos tagueados, mas nenhum alt-long sob regime de downgrade na janela"
    elif rs_agg["sum_r"] < 0:
        rs_verdict = (f"alt-longs em regime de downgrade somaram {rs_agg['sum_r']:+.1f}R "
                      f"({rs_agg['count']} setups) — reduzir size (#6) teria poupado {-proj_delta:+.1f}R")
    else:
        rs_verdict = (f"alt-longs em regime de downgrade foram lucrativos ({rs_agg['sum_r']:+.1f}R) "
                      f"— reduzir size teria custado {proj_delta:+.1f}R")
    out["regime_sizing"] = {
        "enabled_now": REGIME_SIZING_ENABLED,
        "size_mult_alt_long": REGIME_SIZE_MULT_ALT_LONG,
        "downgrade_regimes": sorted(DOWNGRADE_REGIMES),
        "cohort_alt_long_downgrade": rs_agg,
        "projected_r_delta_if_on": proj_delta,
        "tagged_resolved": tagged, "total_resolved": len(rows),
        "verdict": rs_verdict,
    }

    # ── (3) pre_tp1_protect — não reconstruível por outcome ────────────────────
    try:
        from services.trade_manager_service import PRE_TP1_PROTECT_ENABLED as _ptp_on
    except Exception:
        _ptp_on = False
    stops_full = 0
    try:
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stops_full = int((await session.execute(
                select(func.count(RealTrade.id))
                .where(RealTrade.source == "auto")
                .where(RealTrade.status == "closed_stop")
                .where(RealTrade.opened_at >= since)
            )).scalar() or 0)
    except Exception as e:
        log.warning(f"[assertiveness] pre_tp1 stops read falhou: {e}")
    out["pre_tp1_protect"] = {
        "enabled_now": bool(_ptp_on),
        "real_full_stops_in_window": stops_full,
        "note": ("não reconstruível por outcome (precisa do caminho intra-trade pré-TP1, "
                 "não persistido). Avaliar só via shadow ao vivo, ou logar progress→TP1 nos stops."),
        "verdict": (f"{stops_full} stops cheios na janela são o alvo do gate; "
                    f"medir adoção exige instrumentar o caminho pré-TP1 antes"),
    }
    return out


async def _calibration_stats() -> Dict[str, Any]:
    out = {"mature": False, "total_resolved": 0, "min_sample": 30,
           "p_global": None, "win_rate_pct": None, "computed_at": None}
    try:
        from services import calibration_service as cs
        out["min_sample"] = cs.MIN_SAMPLE_TOTAL
        calib = await cs.get_calibration()
        if calib:
            out.update({
                "mature": True,
                "total_resolved": calib.get("total_resolved", 0),
                "p_global": calib.get("p_global"),
                "win_rate_pct": round((calib.get("p_global") or 0) * 100, 1),
                "computed_at": calib.get("computed_at"),
            })
        else:
            # imatura: ainda assim reporta quantos resolvidos existem
            try:
                from db import get_session
                from models.recommendation_snapshot import RecommendationSnapshot
                async with get_session() as session:
                    cnt = int((await session.execute(
                        select(func.count(RecommendationSnapshot.id))
                        .where(RecommendationSnapshot.status.in_(_SNAP_RESOLVED))
                    )).scalar() or 0)
                out["total_resolved"] = cnt
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[assertiveness] calibration read falhou: {e}")
    return out


async def get_assertiveness(days: int = 30, gate_days: int = 7) -> Dict[str, Any]:
    """Agrega tudo pro painel. days = janela de outcomes; gate_days = janela de
    skips (mais curta porque skips são muito mais frequentes)."""
    from db import DB_ENABLED
    if not DB_ENABLED:
        return {"enabled": False, "reason": "DB desabilitado"}
    real_money = await _real_money_stats(days)
    shadow = await _shadow_stats(days)
    gates = await _gate_stats(gate_days)
    calibration = await _calibration_stats()
    opportunity_cost = await _opportunity_cost(days)
    funnel = await _funnel(gate_days, gates)
    scorecard = await _per_coin_scorecard(days)
    equity_curve = await _equity_curve(days)
    calibration_audit = await _calibration_audit(days)
    directional = await _directional_audit(days)
    regime = await _regime_audit(days)
    gate_counterfactual = await _gate_counterfactual(days)
    return {
        "enabled": True,
        "window_days": days,
        "real_money": real_money,
        "shadow": shadow,
        "gates": gates,
        "calibration": calibration,
        "calibration_audit": calibration_audit,
        "opportunity_cost": opportunity_cost,
        "funnel": funnel,
        "scorecard": scorecard,
        "equity_curve": equity_curve,
        "directional": directional,
        "regime": regime,
        "gate_counterfactual": gate_counterfactual,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
