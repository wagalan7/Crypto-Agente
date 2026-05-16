"""Central Estratégica — Weekly Brain + Insights + Sales Sequence + Profile Audit."""
from datetime import datetime, timedelta
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import Insight, WeeklyBrain, User, Product, ContentPiece, CalendarSlot
from auth import get_current_user, assert_client_access
from services import BrandBrain, generate_image_url, aspect_for_format, compute_heuristic_insights
from agents import (
    WeeklyBrainAgent, InsightGeneratorAgent, parse_json_response,
    SalesSequenceAgent, ProfileAnalyzerAgent,
)

router = APIRouter(prefix="/strategy", tags=["strategy"])


def _serialize_wb(wb: WeeklyBrain) -> dict:
    return {
        "id": wb.id,
        "client_id": wb.client_id,
        "focus": wb.focus,
        "opportunities": wb.opportunities or [],
        "alerts": wb.alerts or [],
        "risks": wb.risks or [],
        "priorities": wb.priorities or [],
        "audience_behavior": wb.audience_behavior,
        "trends": wb.trends or [],
        "emotional_sequence": wb.emotional_sequence or [],
        "generated_at": wb.generated_at.isoformat() if wb.generated_at else None,
    }


def _serialize_insight(i: Insight) -> dict:
    return {
        "id": i.id,
        "client_id": i.client_id,
        "kind": i.kind,
        "title": i.title,
        "message": i.message,
        "evidence": i.evidence,
        "severity": i.severity,
        "is_dismissed": i.is_dismissed,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


@router.get("/weekly/{client_id}")
def get_weekly(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    wb = db.query(WeeklyBrain).filter(WeeklyBrain.client_id == client_id).order_by(WeeklyBrain.generated_at.desc()).first()
    if not wb:
        return {"exists": False}
    return {"exists": True, **_serialize_wb(wb)}


@router.post("/weekly/{client_id}/generate")
async def regenerate_weekly(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    agent = WeeklyBrainAgent()
    raw = await agent.run(agent.build_prompt(brain["text"]))
    data = parse_json_response(raw)
    if not data:
        raise HTTPException(500, f"Falha. Raw: {raw[:200]}")

    wb = WeeklyBrain(
        client_id=client_id,
        focus=data.get("focus", ""),
        opportunities=data.get("opportunities", []),
        alerts=data.get("alerts", []),
        risks=data.get("risks", []),
        priorities=data.get("priorities", []),
        audience_behavior=data.get("audience_behavior", ""),
        trends=data.get("trends", []),
        emotional_sequence=data.get("emotional_sequence", []),
    )
    db.add(wb)
    db.commit()
    db.refresh(wb)
    return _serialize_wb(wb)


def _refresh_heuristics(db: Session, client_id: int) -> None:
    """Re-run deterministic checks and persist as Insight rows.

    Heuristic insights have kinds prefixed with saturation_/calendar_gap_/velocity_.
    We dismiss the old ones with those prefixes and create fresh ones — keeps the
    set current without polluting LLM-generated insights.
    """
    HEURISTIC_KINDS = ("saturation_emotion", "saturation_funnel", "saturation_format",
                       "calendar_gap_urgent", "calendar_gap_soon", "velocity_drop",
                       "persona_stale")
    db.query(Insight).filter(
        Insight.client_id == client_id,
        Insight.is_dismissed == False,
        Insight.kind.in_(HEURISTIC_KINDS),
    ).update({"is_dismissed": True}, synchronize_session=False)
    for h in compute_heuristic_insights(db, client_id):
        db.add(Insight(
            client_id=client_id,
            kind=h["kind"][:50],
            title=h["title"][:300],
            message=h["message"],
            evidence=h.get("evidence", ""),
            severity=h.get("severity", "info"),
        ))
    db.commit()


@router.get("/insights/{client_id}")
def list_insights(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    # Refresh heuristic insights on every read (cheap, deterministic) so calendar
    # gaps and saturation alerts are always up-to-date without manual regen.
    try:
        _refresh_heuristics(db, client_id)
    except Exception:
        pass  # never break the read because of heuristic side effect
    items = db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == False).order_by(Insight.created_at.desc()).limit(20).all()
    return [_serialize_insight(i) for i in items]


@router.post("/insights/{client_id}/generate")
async def regenerate_insights(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    agent = InsightGeneratorAgent()
    raw = await agent.run(agent.build_prompt(brain["text"]))
    data = parse_json_response(raw)
    if not data or "insights" not in data:
        raise HTTPException(500, f"Falha. Raw: {raw[:200]}")

    # Dismiss old insights (refresh the set)
    db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == False).update({"is_dismissed": True})
    created = []
    for ins in data["insights"]:
        i = Insight(
            client_id=client_id,
            kind=ins.get("kind", "info"),
            title=ins.get("title", "")[:300],
            message=ins.get("message", ""),
            evidence=ins.get("evidence", ""),
            severity=ins.get("severity", "info"),
        )
        db.add(i)
        created.append(i)
    db.commit()
    # Append deterministic heuristics alongside LLM insights
    try:
        _refresh_heuristics(db, client_id)
    except Exception:
        pass
    # Return ALL active insights (LLM + heuristic) so frontend sees everything
    items = db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == False).order_by(Insight.created_at.desc()).limit(20).all()
    return [_serialize_insight(i) for i in items]


class SalesSequenceRequest(BaseModel):
    product_id: int
    launch_date: str  # ISO date "YYYY-MM-DD"
    total_days: int = 7
    platform: str = "instagram"
    generate_images: bool = True


def _product_block(p: Product) -> str:
    return "\n".join([
        f"PRODUTO: {p.name} ({p.type})",
        f"  Preço: {p.price or '-'}",
        f"  Transformação: {p.transformation or '-'}",
        f"  Dores que resolve: {', '.join(p.pains_solved or [])}",
        f"  Desejos: {', '.join(p.desires or [])}",
        f"  Objeções comuns: {', '.join(p.objections or [])}",
        f"  Estágio de consciência alvo: {p.awareness_stage or '-'}",
    ])


@router.post("/sales-sequence/{client_id}")
async def generate_sales_sequence(
    client_id: int,
    req: SalesSequenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assert_client_access(client_id, current_user, db)
    product = db.query(Product).filter(Product.id == req.product_id, Product.client_id == client_id).first()
    if not product:
        raise HTTPException(404, "Produto não encontrado")
    try:
        launch_dt = datetime.strptime(req.launch_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "launch_date inválida (use YYYY-MM-DD)")

    brain = BrandBrain(db).build(client_id)
    agent = SalesSequenceAgent()
    raw = await agent.run(agent.build_prompt(brain["text"], _product_block(product), req.total_days, req.platform))
    data = parse_json_response(raw)
    if not data or "posts" not in data:
        raise HTTPException(500, f"Falha ao gerar sequência. Raw: {raw[:200]}")

    sequence_start = launch_dt - timedelta(days=req.total_days - 1)
    created = []
    for idx, post in enumerate(data["posts"]):
        try:
            offset = int(post.get("day_offset", idx))
        except Exception:
            offset = idx
        offset = max(0, min(offset, req.total_days - 1))
        scheduled = sequence_start + timedelta(days=offset)

        # Generate image
        media_url = None
        if req.generate_images and post.get("image_prompt"):
            w, h = aspect_for_format(post.get("format", "post"))
            media_url = generate_image_url(post["image_prompt"], width=w, height=h, seed=client_id * 1000 + idx)

        content = ContentPiece(
            client_id=client_id,
            title=(post.get("title") or f"Sequência #{idx + 1}")[:200],
            format=post.get("format", "post"),
            platform=post.get("platform", req.platform),
            objective=(post.get("objective") or "conversao")[:50],
            hook=post.get("hook"),
            script=post.get("script"),
            copy=post.get("copy"),
            design_brief=post.get("design_brief"),
            media_url=media_url,
            strategic_note=f"Sequência de venda para '{product.name}' — peça {idx + 1}/{len(data['posts'])}",
            objective_reasoning=post.get("reasoning"),
            emotion_used=(post.get("emotion") or "")[:100] or None,
            funnel_stage=(post.get("funnel_stage") or "")[:50] or None,
            scheduled_at=scheduled,
            linked_product_id=product.id,
            status="pending",
        )
        db.add(content)
        db.flush()  # so we get content.id

        slot = CalendarSlot(
            client_id=client_id,
            content_id=content.id,
            scheduled_at=scheduled,
            platform=content.platform,
            format=content.format,
            objective=content.objective,
            status="ready",
        )
        db.add(slot)
        created.append({
            "id": content.id,
            "title": content.title,
            "scheduled_at": scheduled.isoformat(),
            "funnel_stage": content.funnel_stage,
            "objective": content.objective,
            "emotion_used": content.emotion_used,
            "media_url": media_url,
            "reasoning": post.get("reasoning"),
        })

    db.commit()
    return {
        "strategy_summary": data.get("strategy_summary", ""),
        "product": {"id": product.id, "name": product.name},
        "launch_date": launch_dt.isoformat(),
        "sequence_start": sequence_start.isoformat(),
        "posts": created,
    }


@router.post("/profile-audit/{client_id}")
async def generate_profile_audit(
    client_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assert_client_access(client_id, current_user, db)

    contents = db.query(ContentPiece).filter(ContentPiece.client_id == client_id).order_by(ContentPiece.created_at.desc()).limit(30).all()

    if not contents:
        raise HTTPException(400, "Sem histórico de conteúdo para auditar")

    # Distribution stats
    funnel_dist = Counter(c.funnel_stage or "—" for c in contents)
    objective_dist = Counter(c.objective or "—" for c in contents)
    emotion_dist = Counter(c.emotion_used or "—" for c in contents)
    format_dist = Counter(c.format or "—" for c in contents)
    days_since_first = (datetime.utcnow() - min(c.created_at for c in contents)).days if contents else 0

    distribution = "\n".join([
        f"Total de posts analisados: {len(contents)}",
        f"Período: últimos {days_since_first} dias",
        f"Distribuição por etapa de funil: {dict(funnel_dist)}",
        f"Distribuição por objetivo: {dict(objective_dist)}",
        f"Distribuição por emoção: {dict(emotion_dist)}",
        f"Distribuição por formato: {dict(format_dist)}",
    ])

    history = "\n---\n".join(
        f"[{c.created_at.strftime('%Y-%m-%d') if c.created_at else '?'}] {c.format}/{c.objective} · funnel={c.funnel_stage or '?'} · emoção={c.emotion_used or '?'}\n"
        f"Title: {c.title}\nHook: {c.hook or ''}"
        for c in contents[:15]
    )

    brain = BrandBrain(db).build(client_id)
    agent = ProfileAnalyzerAgent()
    raw = await agent.run(agent.build_prompt(brain["text"], history, distribution))
    data = parse_json_response(raw)
    if not data:
        raise HTTPException(500, f"Falha ao auditar. Raw: {raw[:200]}")

    # Persist audit insights (don't dismiss other insights — audit is separate signal)
    created = []
    for ins in (data.get("insights") or []):
        kind = (ins.get("kind") or "audit")[:50]
        if not kind.startswith("audit"):
            kind = f"audit_{kind}"
        i = Insight(
            client_id=client_id,
            kind=kind[:50],
            title=(ins.get("title") or "")[:300],
            message=ins.get("message", ""),
            evidence=ins.get("evidence", ""),
            severity=ins.get("severity", "info"),
        )
        db.add(i)
        created.append(i)
    db.commit()

    return {
        "audit_summary": data.get("audit_summary", ""),
        "strengths": data.get("strengths", []),
        "gaps": data.get("gaps", []),
        "inconsistencies": data.get("inconsistencies", []),
        "funnel_distribution_observation": data.get("funnel_distribution_observation", ""),
        "emotion_observation": data.get("emotion_observation", ""),
        "frequency_observation": data.get("frequency_observation", ""),
        "insights": [_serialize_insight(i) for i in created],
        "stats": {
            "total_posts": len(contents),
            "funnel_distribution": dict(funnel_dist),
            "objective_distribution": dict(objective_dist),
            "emotion_distribution": dict(emotion_dist),
            "format_distribution": dict(format_dist),
        },
    }


@router.post("/insights/{insight_id}/dismiss")
def dismiss_insight(insight_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    i = db.query(Insight).filter(Insight.id == insight_id).first()
    if not i:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(i.client_id, current_user, db)
    i.is_dismissed = True
    db.commit()
    return _serialize_insight(i)
