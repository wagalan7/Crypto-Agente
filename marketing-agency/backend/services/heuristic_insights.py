"""Heuristic insights — deterministic, no LLM cost.

Detects patterns that are noise-free signals: saturation of emotion/funnel/format,
empty calendar slots in the near term, content velocity drops. These complement
the LLM-generated insights — they're fast, free, and never hallucinate.
"""
from collections import Counter
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from models import ContentPiece, CalendarSlot


def detect_saturation(db: Session, client_id: int, window_days: int = 14) -> list[dict]:
    """Flag when the same dimension dominates content production.

    Thresholds:
      - emotion repeated in >60% of posts
      - funnel_stage repeated in >50% of posts
      - format repeated in >70% of posts
    Minimum 6 posts in window to evaluate (avoids cold-start false positives).
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    posts = db.query(ContentPiece).filter(
        ContentPiece.client_id == client_id,
        ContentPiece.created_at >= cutoff,
    ).all()
    if len(posts) < 6:
        return []

    n = len(posts)
    insights: list[dict] = []

    emotions = Counter(p.emotion_used for p in posts if p.emotion_used)
    if emotions:
        top_emo, count = emotions.most_common(1)[0]
        ratio = count / n
        if ratio > 0.6:
            insights.append({
                "kind": "saturation_emotion",
                "title": f"Saturação emocional: '{top_emo}'",
                "message": f"{int(ratio*100)}% dos seus últimos {n} posts usam a emoção '{top_emo}'. A audiência satura — alterne pra outra emoção da persona (validação, urgência, alívio etc).",
                "evidence": f"{count}/{n} posts em {window_days}d",
                "severity": "warning",
            })

    funnels = Counter(p.funnel_stage for p in posts if p.funnel_stage)
    if funnels:
        top_fn, count = funnels.most_common(1)[0]
        ratio = count / n
        if ratio > 0.5:
            insights.append({
                "kind": "saturation_funnel",
                "title": f"Funil desbalanceado: '{top_fn}' dominando",
                "message": f"{int(ratio*100)}% dos posts estão em '{top_fn}'. Você está deixando outras etapas do funil descobertas — varie entre identificação, dor, autoridade, quebra de objeção, desejo e conversão.",
                "evidence": f"{count}/{n} posts em {window_days}d",
                "severity": "warning",
            })

    formats = Counter(p.format for p in posts if p.format)
    if formats:
        top_fmt, count = formats.most_common(1)[0]
        ratio = count / n
        if ratio > 0.7:
            insights.append({
                "kind": "saturation_format",
                "title": f"Formato monótono: {int(ratio*100)}% '{top_fmt}'",
                "message": f"Quase tudo está em {top_fmt}. A audiência se acostuma e o alcance cai. Misture formatos pra reativar o algoritmo (reels + carrossel + story).",
                "evidence": f"{count}/{n} posts em {window_days}d",
                "severity": "info",
            })

    return insights


def detect_calendar_gaps(db: Session, client_id: int, lookahead_days: int = 3) -> list[dict]:
    """Flag scheduled slots in the next N days that have no content attached.

    Each slot becomes 1 critical insight if it's within 24h, warning if 1-3d.
    Aggregated into a single insight when there are multiple.
    """
    now = datetime.utcnow()
    horizon = now + timedelta(days=lookahead_days)
    slots = db.query(CalendarSlot).filter(
        CalendarSlot.client_id == client_id,
        CalendarSlot.scheduled_at >= now,
        CalendarSlot.scheduled_at <= horizon,
        CalendarSlot.content_id.is_(None),
    ).order_by(CalendarSlot.scheduled_at).all()
    if not slots:
        return []

    # Bucket by urgency
    tomorrow = now + timedelta(hours=24)
    urgent = [s for s in slots if s.scheduled_at <= tomorrow]
    soon = [s for s in slots if s.scheduled_at > tomorrow]

    insights: list[dict] = []
    if urgent:
        when_list = ", ".join(s.scheduled_at.strftime("%d/%m %Hh") for s in urgent[:5])
        insights.append({
            "kind": "calendar_gap_urgent",
            "title": f"⚠ {len(urgent)} slot(s) sem conteúdo nas próximas 24h",
            "message": f"Slots agendados sem post anexado: {when_list}. Use Auto-Criar ou marque como cancelado.",
            "evidence": f"{len(urgent)} slot(s) vazio(s) urgente(s)",
            "severity": "critical",
        })
    if soon:
        insights.append({
            "kind": "calendar_gap_soon",
            "title": f"{len(soon)} slot(s) vazio(s) nos próximos {lookahead_days} dias",
            "message": f"Você tem {len(soon)} slot(s) agendado(s) ainda sem conteúdo. Antecipe a criação pra não perder o ritmo.",
            "evidence": f"{len(soon)} slot(s) vazio(s) em {lookahead_days}d",
            "severity": "warning",
        })
    return insights


def detect_velocity_drop(db: Session, client_id: int) -> list[dict]:
    """Flag when posting cadence dropped vs prior period."""
    now = datetime.utcnow()
    last_7 = db.query(ContentPiece).filter(
        ContentPiece.client_id == client_id,
        ContentPiece.created_at >= now - timedelta(days=7),
    ).count()
    prev_7 = db.query(ContentPiece).filter(
        ContentPiece.client_id == client_id,
        ContentPiece.created_at >= now - timedelta(days=14),
        ContentPiece.created_at < now - timedelta(days=7),
    ).count()
    if prev_7 >= 3 and last_7 < prev_7 * 0.5:
        return [{
            "kind": "velocity_drop",
            "title": f"Queda de cadência: {last_7} posts essa semana vs {prev_7} na anterior",
            "message": "Você caiu o ritmo pela metade. Consistência é o sinal mais forte de autoridade pro algoritmo — retome.",
            "evidence": f"7d atual: {last_7} · 7d anterior: {prev_7}",
            "severity": "warning",
        }]
    return []


def all_heuristics(db: Session, client_id: int) -> list[dict]:
    """Run all deterministic checks. Returns insight dicts ready to persist."""
    return [
        *detect_saturation(db, client_id),
        *detect_calendar_gaps(db, client_id),
        *detect_velocity_drop(db, client_id),
    ]
