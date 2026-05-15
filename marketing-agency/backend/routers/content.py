from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db
from models import ContentPiece, User
from auth import get_current_user, assert_client_access

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


class ContentUpdate(BaseModel):
    title: Optional[str] = None
    hook: Optional[str] = None
    script: Optional[str] = None
    copy: Optional[str] = None
    design_brief: Optional[str] = None
    media_url: Optional[str] = None
    status: Optional[str] = None
    strategic_note: Optional[str] = None


def _serialize(c: ContentPiece) -> dict:
    return {
        "id": c.id,
        "client_id": c.client_id,
        "title": c.title,
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
        "external_post_id": c.external_post_id,
        "publish_error": c.publish_error,
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
    return [_serialize(c) for c in q.order_by(ContentPiece.created_at.desc()).all()]


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
    return _serialize(c)


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
def approve_content(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    c.status = "approved"
    db.commit()
    return _serialize(c)
