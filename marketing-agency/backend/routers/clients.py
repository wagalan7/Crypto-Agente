from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from datetime import datetime, timedelta
from models import User, Client, ClientAccess, AuthorityScoreSnapshot
from auth import get_current_user, assert_client_access
from services import AuthorityScorer
from services.pdf_report import generate_monthly_report
from services.plans import assert_can_create_client, assert_feature
from fastapi.responses import StreamingResponse
import io

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
        "owner_id": c.owner_id,
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


def _accessible_client_ids(user: User, db: Session) -> list:
    """Returns list of client IDs this user can see."""
    owned = [c.id for c in db.query(Client.id).filter(Client.owner_id == user.id).all()]
    granted = [a.client_id for a in db.query(ClientAccess.client_id).filter(ClientAccess.user_id == user.id).all()]
    return list(set(owned + granted))


@router.get("/")
def list_clients(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ids = _accessible_client_ids(current_user, db)
    clients = db.query(Client).filter(Client.id.in_(ids)).order_by(Client.created_at.desc()).all()
    return [_serialize(c) for c in clients]


@router.post("/")
def create_client(data: ClientCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_can_create_client(current_user, db)
    client = Client(**data.model_dump(), owner_id=current_user.id)
    db.add(client)
    db.commit()
    db.refresh(client)
    return _serialize(client)


@router.get("/{client_id}")
def get_client(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    client = db.query(Client).filter(Client.id == client_id).first()
    return _serialize(client)


@router.patch("/{client_id}")
def update_client(client_id: int, data: ClientUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    client = db.query(Client).filter(Client.id == client_id).first()
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(client, field, value)
    db.commit()
    db.refresh(client)
    return _serialize(client)


@router.post("/{client_id}/refresh-score")
def refresh_score(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    scorer = AuthorityScorer(db)
    score = scorer.update(client_id)
    return {"authority_score": score}


@router.get("/{client_id}/score-history")
def score_history(client_id: int, days: int = 30,
                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Daily Authority Score snapshots for the last N days (max 180)."""
    assert_client_access(client_id, current_user, db)
    cutoff = datetime.utcnow() - timedelta(days=min(max(days, 7), 180))
    rows = (
        db.query(AuthorityScoreSnapshot)
        .filter(AuthorityScoreSnapshot.client_id == client_id,
                AuthorityScoreSnapshot.recorded_at >= cutoff)
        .order_by(AuthorityScoreSnapshot.recorded_at.asc())
        .all()
    )
    return [{"score": r.score, "recorded_at": r.recorded_at.isoformat()} for r in rows]


@router.get("/{client_id}/monthly-report.pdf")
def monthly_report(client_id: int, month: Optional[str] = None,
                   current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate a PDF monthly performance report. `month` is YYYY-MM (defaults to current month)."""
    assert_client_access(client_id, current_user, db)
    assert_feature(current_user, "pdf_report")
    now = datetime.utcnow()
    year, mon = now.year, now.month
    if month:
        try:
            year, mon = map(int, month.split("-"))
        except Exception:
            raise HTTPException(status_code=400, detail="month deve estar no formato YYYY-MM")
        if mon < 1 or mon > 12:
            raise HTTPException(status_code=400, detail="mês inválido")
    try:
        pdf_bytes = generate_monthly_report(db, client_id, year, mon)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    filename = f"relatorio-{client_id}-{year:04d}-{mon:02d}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
