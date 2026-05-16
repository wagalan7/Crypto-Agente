import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db
from models import ContentPiece, User, Product, Client
from auth import get_current_user, assert_client_access
from agents import ProductionBriefingAgent
from agents.production_briefing import parse_json_response as parse_brief_json

router = APIRouter(prefix="/content", tags=["content"])


class ContentCreate(BaseModel):
    client_id: int
    title: str
    format: str
    platform: str
    objective: str
    hook: Optional[str] = None
    script: Optional[str] = None
    copy: Optional[str] = None
    design_brief: Optional[str] = None
    media_url: Optional[str] = None
    trend_context: Optional[str] = None
    strategic_note: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    objective_reasoning: Optional[str] = None
    emotion_used: Optional[str] = None
    funnel_stage: Optional[str] = None
    format_reasoning: Optional[str] = None
    linked_product_id: Optional[int] = None


class ContentUpdate(BaseModel):
    title: Optional[str] = None
    hook: Optional[str] = None
    script: Optional[str] = None
    copy: Optional[str] = None
    design_brief: Optional[str] = None
    media_url: Optional[str] = None
    status: Optional[str] = None
    strategic_note: Optional[str] = None
    objective_reasoning: Optional[str] = None
    emotion_used: Optional[str] = None
    funnel_stage: Optional[str] = None
    format_reasoning: Optional[str] = None
    linked_product_id: Optional[int] = None


def _serialize(c: ContentPiece, product_name: Optional[str] = None) -> dict:
    return {
        "id": c.id,
        "client_id": c.client_id,
        "title": c.title,
        "linked_product_name": product_name,
        "format": c.format,
        "platform": c.platform,
        "objective": c.objective,
        "hook": c.hook,
        "script": c.script,
        "copy": c.copy,
        "design_brief": c.design_brief,
        "media_url": c.media_url,
        "status": c.status,
        "trend_context": c.trend_context,
        "strategic_note": c.strategic_note,
        "objective_reasoning": c.objective_reasoning,
        "emotion_used": c.emotion_used,
        "funnel_stage": c.funnel_stage,
        "format_reasoning": c.format_reasoning,
        "linked_product_id": c.linked_product_id,
        "external_post_id": c.external_post_id,
        "publish_error": c.publish_error,
        "production_brief": json.loads(c.production_brief) if c.production_brief else None,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/client/{client_id}")
def list_content(
    client_id: int,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assert_client_access(client_id, current_user, db)
    q = db.query(ContentPiece).filter(ContentPiece.client_id == client_id)
    if status:
        q = q.filter(ContentPiece.status == status)
    contents = q.order_by(ContentPiece.created_at.desc()).all()
    # Resolve product names for linked content
    product_ids = {c.linked_product_id for c in contents if c.linked_product_id}
    name_by_id: dict[int, str] = {}
    if product_ids:
        for p in db.query(Product).filter(Product.id.in_(product_ids)).all():
            name_by_id[p.id] = p.name
    return [_serialize(c, name_by_id.get(c.linked_product_id) if c.linked_product_id else None) for c in contents]


@router.post("/")
def create_content(data: ContentCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    content = ContentPiece(**data.model_dump())
    db.add(content)
    db.commit()
    db.refresh(content)
    return _serialize(content)


@router.get("/{content_id}")
def get_content(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    pname = None
    if c.linked_product_id:
        p = db.query(Product).filter(Product.id == c.linked_product_id).first()
        pname = p.name if p else None
    return _serialize(c, pname)


@router.patch("/{content_id}")
def update_content(content_id: int, data: ContentUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(c, field, value)
    if data.status == "published" and not c.published_at:
        c.published_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _serialize(c)


@router.post("/{content_id}/approve")
async def approve_content(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    c.status = "approved"
    # Auto-generate production briefing on approve if missing — turns "approved"
    # from a status flag into an actionable shooting plan.
    if not c.production_brief:
        try:
            client = db.query(Client).filter(Client.id == c.client_id).first()
            agent = ProductionBriefingAgent()
            prompt = agent.build_prompt(
                title=c.title or "",
                format=c.format or "post",
                platform=c.platform or "instagram",
                hook=c.hook or "",
                script=c.script or "",
                design_brief=c.design_brief or "",
                copy=c.copy or "",
                emotion=c.emotion_used or "",
                tone=(client.tone if client else "") or "",
            )
            raw = await agent.run(prompt)
            brief = parse_brief_json(raw)
            if brief:
                c.production_brief = json.dumps(brief, ensure_ascii=False)
        except Exception:
            # Never fail approve because of briefing — silent fallback
            pass
    db.commit()
    return _serialize(c)


@router.post("/{content_id}/regenerate-brief")
async def regenerate_brief(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    client = db.query(Client).filter(Client.id == c.client_id).first()
    agent = ProductionBriefingAgent()
    prompt = agent.build_prompt(
        title=c.title or "",
        format=c.format or "post",
        platform=c.platform or "instagram",
        hook=c.hook or "",
        script=c.script or "",
        design_brief=c.design_brief or "",
        copy=c.copy or "",
        emotion=c.emotion_used or "",
        tone=(client.tone if client else "") or "",
    )
    raw = await agent.run(prompt)
    brief = parse_brief_json(raw)
    if not brief:
        raise HTTPException(500, "Falha ao gerar briefing de produção")
    c.production_brief = json.dumps(brief, ensure_ascii=False)
    db.commit()
    return _serialize(c)
