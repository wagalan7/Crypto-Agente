from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import Client
from services import AuthorityScorer

router = APIRouter(prefix="/clients", tags=["clients"])


class ClientCreate(BaseModel):
    name: str
    niche: Optional[str] = None
    target_audience: Optional[str] = None
    tone: Optional[str] = None
    personality: Optional[str] = None
    positioning: Optional[str] = None
    goals: Optional[List[str]] = []
    platforms: Optional[List[str]] = []


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    niche: Optional[str] = None
    target_audience: Optional[str] = None
    tone: Optional[str] = None
    personality: Optional[str] = None
    positioning: Optional[str] = None
    goals: Optional[List[str]] = None
    platforms: Optional[List[str]] = None


def _serialize(c: Client) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "niche": c.niche,
        "target_audience": c.target_audience,
        "tone": c.tone,
        "personality": c.personality,
        "positioning": c.positioning,
        "goals": c.goals or [],
        "platforms": c.platforms or [],
        "authority_score": c.authority_score,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/")
def list_clients(db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.created_at.desc()).all()
    return [_serialize(c) for c in clients]


@router.post("/")
def create_client(data: ClientCreate, db: Session = Depends(get_db)):
    client = Client(**data.model_dump())
    db.add(client)
    db.commit()
    db.refresh(client)
    return _serialize(client)


@router.get("/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    return _serialize(client)


@router.patch("/{client_id}")
def update_client(client_id: int, data: ClientUpdate, db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(client, field, value)
    db.commit()
    db.refresh(client)
    return _serialize(client)


@router.post("/{client_id}/refresh-score")
def refresh_score(client_id: int, db: Session = Depends(get_db)):
    scorer = AuthorityScorer(db)
    score = scorer.update(client_id)
    return {"authority_score": score}
