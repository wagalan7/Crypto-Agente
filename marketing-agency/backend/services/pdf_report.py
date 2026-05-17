"""Monthly PDF performance report.

Renders an executive recap for a client+month: authority score, totals,
averages, top performing posts, and an alerts/wins section.
"""
from __future__ import annotations

import io
from calendar import monthrange
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from sqlalchemy.orm import Session

from models import Client, ContentPiece, MetricsSnapshot


PT_MONTHS = [
    "",
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year, month, monthrange(year, month)[1], 23, 59, 59)
    return start, end


def generate_monthly_report(db: Session, client_id: int, year: int, month: int) -> bytes:
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise ValueError("Client not found")

    start, end = _month_range(year, month)

    metrics = (
        db.query(MetricsSnapshot)
        .filter(
            MetricsSnapshot.client_id == client_id,
            MetricsSnapshot.recorded_at >= start,
            MetricsSnapshot.recorded_at <= end,
        )
        .all()
    )
    posts = (
        db.query(ContentPiece)
        .filter(
            ContentPiece.client_id == client_id,
            ContentPiece.created_at >= start,
            ContentPiece.created_at <= end,
        )
        .all()
    )

    n = len(metrics) or 1
    totals = {
        "views": sum(m.views for m in metrics),
        "likes": sum(m.likes for m in metrics),
        "comments": sum(m.comments for m in metrics),
        "shares": sum(m.shares for m in metrics),
        "saves": sum(m.saves for m in metrics),
        "reach": sum(m.reach for m in metrics),
    }
    averages = {
        "retention": round(sum(m.retention_rate for m in metrics) / n, 1) if metrics else 0,
        "ctr": round(sum(m.ctr for m in metrics) / n, 2) if metrics else 0,
        "conversion": round(sum(m.conversion_rate for m in metrics) / n, 2) if metrics else 0,
    }

    # Top performing by views
    metrics_by_content: dict[int, dict] = {}
    for m in metrics:
        if not m.content_id:
            continue
        bucket = metrics_by_content.setdefault(m.content_id, {"views": 0, "likes": 0, "comments": 0, "shares": 0, "saves": 0})
        bucket["views"] += m.views
        bucket["likes"] += m.likes
        bucket["comments"] += m.comments
        bucket["shares"] += m.shares
        bucket["saves"] += m.saves

    top_posts: list[tuple[ContentPiece, dict]] = []
    for p in posts:
        agg = metrics_by_content.get(p.id)
        if agg and agg["views"] > 0:
            top_posts.append((p, agg))
    top_posts.sort(key=lambda x: x[1]["views"], reverse=True)
    top_posts = top_posts[:5]

    # ---- PDF build ----
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"Relatório {client.name} - {PT_MONTHS[month]}/{year}",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=20, textColor=colors.HexColor("#111827"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=14, textColor=colors.HexColor("#1f2937"))
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, textColor=colors.HexColor("#374151"))
    muted = ParagraphStyle("muted", parent=styles["BodyText"], fontSize=9, textColor=colors.HexColor("#6b7280"))

    story = []
    story.append(Paragraph(f"Relatório Mensal — {client.name}", h1))
    story.append(Paragraph(f"{PT_MONTHS[month]} de {year}", muted))
    story.append(Spacer(1, 0.5 * cm))

    # Headline metrics
    story.append(Paragraph("Visão Geral", h2))
    overview_data = [
        ["Authority Score", f"{client.authority_score}/100"],
        ["Posts criados", str(len(posts))],
        ["Posts com métricas", str(len(metrics_by_content))],
        ["Plataformas", ", ".join(client.platforms) if client.platforms else "—"],
    ]
    t = Table(overview_data, colWidths=[5 * cm, 11 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#111827")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5 * cm))

    # Totals
    story.append(Paragraph("Totais do mês", h2))
    totals_rows = [
        ["Views", f"{totals['views']:,}"],
        ["Likes", f"{totals['likes']:,}"],
        ["Comentários", f"{totals['comments']:,}"],
        ["Compartilhamentos", f"{totals['shares']:,}"],
        ["Saves", f"{totals['saves']:,}"],
        ["Alcance", f"{totals['reach']:,}"],
    ]
    t = Table(totals_rows, colWidths=[5 * cm, 11 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbeafe")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5 * cm))

    # Averages
    story.append(Paragraph("Médias", h2))
    avg_rows = [
        ["Retenção média", f"{averages['retention']}%"],
        ["CTR médio", f"{averages['ctr']}%"],
        ["Conversão média", f"{averages['conversion']}%"],
    ]
    t = Table(avg_rows, colWidths=[5 * cm, 11 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#ecfdf5")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1fae5")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.6 * cm))

    # Top posts
    story.append(Paragraph("Top 5 posts do mês", h2))
    if not top_posts:
        story.append(Paragraph("Nenhum post com métricas registradas ainda.", muted))
    else:
        rows = [["#", "Título", "Plataforma", "Views", "Likes", "Coments"]]
        for i, (p, agg) in enumerate(top_posts, 1):
            title = (p.title or "(sem título)")[:50]
            rows.append([str(i), title, p.platform, f"{agg['views']:,}", f"{agg['likes']:,}", f"{agg['comments']:,}"])
        t = Table(rows, colWidths=[0.8 * cm, 7 * cm, 2.5 * cm, 2 * cm, 1.7 * cm, 2 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        f"Gerado por ContentAI em {datetime.utcnow().strftime('%d/%m/%Y %H:%M UTC')}.",
        muted,
    ))

    doc.build(story)
    pdf = buf.getvalue()
    buf.close()
    return pdf
