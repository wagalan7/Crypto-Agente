"""Trending topics endpoints — auto-discovered from Reddit + on-demand refresh."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db
from models import TrendTopic, User
from auth import get_current_user
from services.trend_discovery import discover_trends

router = APIRouter(prefix="/trends", tags=["trends"])


def _serialize(t: TrendTopic) -> dict:
    return {
        "id": t.id,
        "source": t.source,
        "subreddit": t.subreddit,
        "title": t.title,
        "url": t.url,
        "score": t.score,
        "num_comments": t.num_comments,
        "locale": t.locale,
        "tags": t.tags or [],
        "discovered_at": t.discovered_at.isoformat() if t.discovered_at else None,
    }


@router.get("/")
def list_trends(limit: int = 30, locale: Optional[str] = None,
                current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(TrendTopic).order_by(TrendTopic.score.desc(), TrendTopic.discovered_at.desc())
    if locale:
        q = q.filter(TrendTopic.locale == locale)
    rows = q.limit(min(max(limit, 1), 100)).all()
    return [_serialize(t) for t in rows]


@router.post("/refresh")
async def refresh(current_user: User = Depends(get_current_user)):
    inserted = await discover_trends()
    return {"inserted": inserted}
