from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Inspiration, User
from auth import get_current_user, assert_client_access
from services import BrandBrain, fetch_site_context
from agents import InspirationAnalyzerAgent, parse_json_response

router = APIRouter(prefix="/inspirations", tags=["inspirations"])


class InspirationCreate(BaseModel):
    client_id: int
    source_type: str  # url / text / image
    source_value: str
    label: Optional[str] = None


def _serialize(i: Inspiration) -> dict:
    return {
        "id": i.id,
        "client_id": i.client_id,
        "source_type": i.source_type,
        "source_value": i.source_value,
        "label": i.label,
        "analysis": i.analysis or {},
        "adapted_brief": i.adapted_brief,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


@router.get("/client/{client_id}")
def list_inspirations(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(Inspiration).filter(Inspiration.client_id == client_id).order_by(Inspiration.created_at.desc()).all()
    return [_serialize(i) for i in items]


@router.post("/")
async def create_inspiration(data: InspirationCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    # Resolve source content
    source_content = data.source_value
    if data.source_type == "url":
        source_content = await fetch_site_context(data.source_value, max_chars=3000)

    brain = BrandBrain(db).build(data.client_id)
    agent = InspirationAnalyzerAgent()
    raw = await agent.run(agent.build_prompt(brain["text"], data.source_type, source_content))
    parsed = parse_json_response(raw)
    if not parsed:
        raise HTTPException(500, f"Falha ao analisar. Raw: {raw[:200]}")

    item = Inspiration(
        client_id=data.client_id,
        source_type=data.source_type,
        source_value=data.source_value,
        label=data.label or (parsed.get("hook", "")[:80] or "Referência"),
        analysis=parsed,
        adapted_brief=parsed.get("adapted_brief", ""),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _serialize(item)


@router.delete("/{inspiration_id}")
def delete_inspiration(inspiration_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(Inspiration).filter(Inspiration.id == inspiration_id).first()
    if not item:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(item.client_id, current_user, db)
    db.delete(item)
    db.commit()
    return {"detail": "removido"}
