"""PatternDetector — turns metrics history into prescriptive learning.

Where BrandBrain shows raw top/bottom posts, this service ranks across
multiple dimensions (format, emotion, funnel, theme, hook style, day of week)
and outputs *recommendations* a strategist can apply: "use carousel + dor on
Tuesday, avoid generic reels on Sunday".

Pure SQL/Python — no LLM calls. Cheap enough to run on every dashboard load.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func
from models import ContentPiece, MetricsSnapshot


def _impact_score(m: MetricsSnapshot) -> float:
    """Weighted impact: shares + saves = strong intent; views = soft."""
    return (
        (m.shares or 0) * 3.0
        + (m.saves or 0) * 2.0
        + (m.comments or 0) * 1.5
        + (m.likes or 0) * 0.3
        + (m.views or 0) * 0.01
    )


def detect_patterns(db: Session, client_id: int, limit: int = 60) -> Dict[str, Any]:
    """Return ranked patterns + recommendations."""
    snaps: List[MetricsSnapshot] = db.query(MetricsSnapshot).filter(
        MetricsSnapshot.client_id == client_id,
        MetricsSnapshot.content_id.isnot(None),
    ).order_by(MetricsSnapshot.recorded_at.desc()).limit(limit).all()

    if not snaps:
        return {"sample_size": 0, "winners": {}, "losers": {}, "recommendations": []}

    # Index content to avoid n+1
    content_ids = {s.content_id for s in snaps if s.content_id}
    contents = {c.id: c for c in db.query(ContentPiece).filter(ContentPiece.id.in_(content_ids)).all()}

    scored = []
    for s in snaps:
        c = contents.get(s.content_id)
        if not c:
            continue
        scored.append((c, s, _impact_score(s)))

    if not scored:
        return {"sample_size": 0, "winners": {}, "losers": {}, "recommendations": []}

    scored.sort(key=lambda t: t[2], reverse=True)
    n = len(scored)
    top = scored[: max(3, n // 4)]   # top 25% (min 3)
    bot = scored[-max(3, n // 4):]   # bottom 25%

    def _agg(items, attr):
        c = Counter()
        for c_obj, _, _ in items:
            v = getattr(c_obj, attr, None)
            if v:
                c[v] += 1
        return c

    winners = {
        "format": _agg(top, "format").most_common(3),
        "emotion": _agg(top, "emotion_used").most_common(3),
        "funnel": _agg(top, "funnel_stage").most_common(3),
        "platform": _agg(top, "platform").most_common(3),
    }
    losers = {
        "format": _agg(bot, "format").most_common(3),
        "emotion": _agg(bot, "emotion_used").most_common(3),
    }

    # Day-of-week winner
    dow_score: Dict[int, float] = defaultdict(float)
    dow_count: Dict[int, int] = defaultdict(int)
    for c, s, sc in scored:
        if c.scheduled_at:
            dow_score[c.scheduled_at.weekday()] += sc
            dow_count[c.scheduled_at.weekday()] += 1
    dow_avg = {d: dow_score[d] / dow_count[d] for d in dow_score if dow_count[d]}
    dow_winner = max(dow_avg.items(), key=lambda t: t[1])[0] if dow_avg else None
    dow_names = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

    # Build human recommendations
    recs: List[Dict[str, str]] = []
    if winners["format"]:
        f, count = winners["format"][0]
        recs.append({
            "kind": "format",
            "title": f"Priorize formato {f}",
            "rationale": f"Apareceu em {count}/{len(top)} dos posts vencedores.",
        })
    if winners["emotion"]:
        e, count = winners["emotion"][0]
        recs.append({
            "kind": "emotion",
            "title": f"Emoção dominante: {e}",
            "rationale": f"Dispara engajamento em {count}/{len(top)} casos. Use como gatilho narrativo.",
        })
    if winners["funnel"]:
        fn, count = winners["funnel"][0]
        recs.append({
            "kind": "funnel",
            "title": f"Estágio que converte: {fn}",
            "rationale": f"{count} de {len(top)} top posts. Foque o calendário aqui.",
        })
    if losers["format"]:
        f, count = losers["format"][0]
        # Only warn if the same format isn't ALSO a winner
        if not any(f == wf for wf, _ in winners["format"]):
            recs.append({
                "kind": "avoid",
                "title": f"Evite repetir formato {f}",
                "rationale": f"Concentrado em {count}/{len(bot)} posts de menor performance.",
            })
    if dow_winner is not None:
        recs.append({
            "kind": "schedule",
            "title": f"Melhor dia: {dow_names[dow_winner]}",
            "rationale": "Maior impacto médio por post nessa janela.",
        })

    return {
        "sample_size": n,
        "winners": {k: [list(t) for t in v] for k, v in winners.items()},
        "losers": {k: [list(t) for t in v] for k, v in losers.items()},
        "recommendations": recs,
        "best_day": dow_names[dow_winner] if dow_winner is not None else None,
    }
