from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import KnowledgeItem, User
from auth import get_current_user, assert_client_access

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeCreate(BaseModel):
    client_id: int
    title: str
    content: str
    source_type: str = "note"  # pdf/note/screenshot/idea/book/concept/reference
    tags: List[str] = []


def _serialize(k: KnowledgeItem) -> dict:
    return {
        "id": k.id,
        "client_id": k.client_id,
        "title": k.title,
        "content": k.content,
        "source_type": k.source_type,
        "tags": k.tags or [],
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/client/{client_id}")
def list_items(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).order_by(KnowledgeItem.created_at.desc()).all()
    return [_serialize(k) for k in items]


@router.post("/")
def create_item(data: KnowledgeCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    item = KnowledgeItem(**data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return _serialize(item)


@router.delete("/{item_id}")
def delete_item(item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    k = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if not k:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(k.client_id, current_user, db)
    db.delete(k)
    db.commit()
    return {"detail": "removido"}
