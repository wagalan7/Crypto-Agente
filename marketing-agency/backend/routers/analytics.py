from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from models import MetricsSnapshot, User
from services import AuthorityScorer, detect_patterns
from auth import get_current_user, assert_client_access

router = APIRouter(prefix="/analytics", tags=["analytics"])


class MetricsCreate(BaseModel):
    client_id: int
    content_id: Optional[int] = None
    platform: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    reach: int = 0
    retention_rate: float = 0.0
    ctr: float = 0.0
    conversion_rate: float = 0.0


def _serialize(m: MetricsSnapshot) -> dict:
    return {
        "id": m.id,
        "client_id": m.client_id,
        "content_id": m.content_id,
        "platform": m.platform,
        "views": m.views,
        "likes": m.likes,
        "comments": m.comments,
        "shares": m.shares,
        "saves": m.saves,
        "reach": m.reach,
        "retention_rate": m.retention_rate,
        "ctr": m.ctr,
        "conversion_rate": m.conversion_rate,
        "recorded_at": m.recorded_at.isoformat() if m.recorded_at else None,
    }


@router.post("/metrics")
def add_metrics(data: MetricsCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    m = MetricsSnapshot(**data.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    scorer = AuthorityScorer(db)
    scorer.update(data.client_id)
    return _serialize(m)


@router.get("/client/{client_id}/summary")
def get_summary(client_id: int, days: int = 30, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=days)
    metrics = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.client_id == client_id, MetricsSnapshot.recorded_at >= since)
        .all()
    )
    if not metrics:
        return {"client_id": client_id, "period_days": days, "totals": {}, "averages": {}}

    n = len(metrics)
    return {
        "client_id": client_id,
        "period_days": days,
        "content_count": n,
        "totals": {
            "views": sum(m.views for m in metrics),
            "likes": sum(m.likes for m in metrics),
            "comments": sum(m.comments for m in metrics),
            "shares": sum(m.shares for m in metrics),
            "saves": sum(m.saves for m in metrics),
            "reach": sum(m.reach for m in metrics),
        },
        "averages": {
            "retention_rate": round(sum(m.retention_rate for m in metrics) / n, 1),
            "ctr": round(sum(m.ctr for m in metrics) / n, 2),
            "conversion_rate": round(sum(m.conversion_rate for m in metrics) / n, 2),
        },
    }


@router.get("/client/{client_id}/patterns")
def get_patterns(client_id: int, limit: int = 60,
                  current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Prescriptive learning: ranked winners/losers + day-of-week + concrete recs.
    Use this on the dashboard to tell the user WHAT to do next, not just what happened."""
    assert_client_access(client_id, current_user, db)
    return detect_patterns(db, client_id, limit=limit)


@router.get("/client/{client_id}/metrics")
def list_metrics(client_id: int, limit: int = 50, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    metrics = (
        db.query(MetricsSnapshot)
        .filter(MetricsSnapshot.client_id == client_id)
        .order_by(MetricsSnapshot.recorded_at.desc())
        .limit(limit)
        .all()
    )
    return [_serialize(m) for m in metrics]
