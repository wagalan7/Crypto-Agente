from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from models import CalendarSlot
from services import CalendarService

router = APIRouter(prefix="/calendar", tags=["calendar"])


class GenerateWeekRequest(BaseModel):
    client_id: int
    start_date: datetime
    frequency_per_week: int = 7


class AttachContentRequest(BaseModel):
    content_id: int


def _serialize(s: CalendarSlot) -> dict:
    return {
        "id": s.id,
        "client_id": s.client_id,
        "content_id": s.content_id,
        "scheduled_at": s.scheduled_at.isoformat() if s.scheduled_at else None,
        "platform": s.platform,
        "format": s.format,
        "objective": s.objective,
        "status": s.status,
    }


@router.post("/generate-week")
def generate_week(req: GenerateWeekRequest, db: Session = Depends(get_db)):
    svc = CalendarService(db)
    slots = svc.generate_week(req.client_id, req.start_date, req.frequency_per_week)
    return [_serialize(s) for s in slots]


@router.get("/client/{client_id}")
def get_calendar(client_id: int, days: int = 14, db: Session = Depends(get_db)):
    svc = CalendarService(db)
    slots = svc.get_upcoming(client_id, days)
    return [_serialize(s) for s in slots]


@router.patch("/{slot_id}/attach")
def attach_content(slot_id: int, req: AttachContentRequest, db: Session = Depends(get_db)):
    svc = CalendarService(db)
    slot = svc.attach_content(slot_id, req.content_id)
    if not slot:
        raise HTTPException(404, "Slot not found")
    return _serialize(slot)


@router.patch("/{slot_id}/status")
def update_slot_status(slot_id: int, status: str, db: Session = Depends(get_db)):
    slot = db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    slot.status = status
    db.commit()
    return _serialize(slot)
