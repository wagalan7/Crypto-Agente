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
    return {
        "enabled": True,
        "window_days": days,
        "real_money": real_money,
        "shadow": shadow,
        "gates": gates,
        "calibration": calibration,
        "opportunity_cost": opportunity_cost,
        "funnel": funnel,
        "scorecard": scorecard,
        "equity_curve": equity_curve,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
