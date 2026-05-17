from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from database import get_db
from models import CalendarSlot, User, WeeklyBrain
from services import CalendarService
from auth import get_current_user, assert_client_access

# Day-name → weekday index (PT/EN). 0=Monday following Python convention.
WEEKDAY_MAP = {
    "segunda": 0, "monday": 0, "seg": 0,
    "terca": 1, "terça": 1, "tuesday": 1, "ter": 1,
    "quarta": 2, "wednesday": 2, "qua": 2,
    "quinta": 3, "thursday": 3, "qui": 3,
    "sexta": 4, "friday": 4, "sex": 4,
    "sabado": 5, "sábado": 5, "saturday": 5, "sab": 5,
    "domingo": 6, "sunday": 6, "dom": 6,
}


def _emotion_to_objective(emotion: str) -> str:
    e = (emotion or "").lower()
    if any(x in e for x in ("vulnerab", "alívio", "alivio", "esperança", "esperanca")):
        return "conexao"
    if any(x in e for x in ("urgenc", "urg\u00eanc", "desejo")):
        return "conversao"
    if "autorid" in e or "credib" in e:
        return "autoridade"
    if "curios" in e or "surpres" in e:
        return "atracao"
    return "conexao"

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
        "narrative": s.narrative,
        "intent": s.intent,
        "hook_idea": s.hook_idea,
        "strategic_reasoning": s.strategic_reasoning,
    }


@router.post("/generate-week")
def generate_week(req: GenerateWeekRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
    svc = CalendarService(db)
    slots = svc.generate_week(req.client_id, req.start_date, req.frequency_per_week)
    return [_serialize(s) for s in slots]


@router.get("/client/{client_id}")
def get_calendar(client_id: int, days: int = 14, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    svc = CalendarService(db)
    slots = svc.get_upcoming(client_id, days)
    return [_serialize(s) for s in slots]


@router.patch("/{slot_id}/attach")
def attach_content(slot_id: int, req: AttachContentRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    slot = db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    assert_client_access(slot.client_id, current_user, db)
    svc = CalendarService(db)
    slot = svc.attach_content(slot_id, req.content_id)
    return _serialize(slot)


class PopulateFromWeeklyRequest(BaseModel):
    client_id: int
    start_date: Optional[datetime] = None  # defaults to next Monday
    platform: str = "instagram"
    default_hour: int = 18


@router.post("/populate-from-weekly")
def populate_from_weekly(req: PopulateFromWeeklyRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cria slots na próxima semana usando a sequência emocional do WeeklyBrain."""
    assert_client_access(req.client_id, current_user, db)
    wb = db.query(WeeklyBrain).filter(WeeklyBrain.client_id == req.client_id).order_by(WeeklyBrain.generated_at.desc()).first()
    if not wb or not wb.emotional_sequence:
        raise HTTPException(400, "Sem WeeklyBrain ou sequência emocional. Gere o cérebro semanal primeiro.")

    if req.start_date:
        start = req.start_date
    else:
        today = datetime.utcnow()
        days_to_monday = (7 - today.weekday()) % 7 or 7
        start = (today + timedelta(days=days_to_monday)).replace(hour=req.default_hour, minute=0, second=0, microsecond=0)

    created = []
    for entry in wb.emotional_sequence:
        if not isinstance(entry, dict):
            continue
        day_name = (entry.get("day") or "").lower().strip()
        weekday = WEEKDAY_MAP.get(day_name)
        if weekday is None:
            # try numeric "1"..."7"
            try:
                weekday = (int(day_name) - 1) % 7
            except Exception:
                continue
        offset = (weekday - start.weekday()) % 7
        scheduled = (start + timedelta(days=offset)).replace(hour=req.default_hour, minute=0)
        fmt = (entry.get("format_suggestion") or "post").lower().strip()
        # normalize
        if fmt not in ("reels", "carousel", "story", "post", "shorts", "youtube"):
            if "reel" in fmt: fmt = "reels"
            elif "carro" in fmt: fmt = "carousel"
            elif "story" in fmt or "stor" in fmt: fmt = "story"
            elif "short" in fmt: fmt = "shorts"
            else: fmt = "post"
        # Strategic narrative pulled from the weekly emotional sequence entry
        narrative = (entry.get("narrative") or entry.get("theme") or entry.get("idea") or "").strip() or None
        intent = (entry.get("intent") or entry.get("audience_action") or "").strip() or None
        hook_idea = (entry.get("hook") or entry.get("hook_idea") or "").strip() or None
        strategic = (entry.get("why") or entry.get("reasoning") or "").strip() or None
        slot = CalendarSlot(
            client_id=req.client_id,
            scheduled_at=scheduled,
            platform=req.platform,
            format=fmt,
            objective=_emotion_to_objective(entry.get("emotion", "")),
            status="planned",
            narrative=narrative,
            intent=intent,
            hook_idea=hook_idea,
            strategic_reasoning=strategic,
        )
        db.add(slot)
        created.append(slot)
    db.commit()
    return [_serialize(s) for s in created]


@router.patch("/{slot_id}/status")
def update_slot_status(slot_id: int, status: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    slot = db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    assert_client_access(slot.client_id, current_user, db)
    slot.status = status
    db.commit()
    return _serialize(slot)


class SlotUpdateRequest(BaseModel):
    narrative: Optional[str] = None
    intent: Optional[str] = None
    hook_idea: Optional[str] = None
    strategic_reasoning: Optional[str] = None
    format: Optional[str] = None
    objective: Optional[str] = None


@router.patch("/{slot_id}")
def update_slot(slot_id: int, req: SlotUpdateRequest,
                 current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Edit strategic fields of a slot (item 9: calendário estratégico)."""
    slot = db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    assert_client_access(slot.client_id, current_user, db)
    for f, v in req.model_dump(exclude_unset=True).items():
        setattr(slot, f, v)
    db.commit()
    db.refresh(slot)
    return _serialize(slot)


class RescheduleRequest(BaseModel):
    scheduled_at: datetime


@router.patch("/{slot_id}/reschedule")
def reschedule_slot(slot_id: int, req: RescheduleRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Move a slot to a new datetime (used by drag-and-drop on the calendar grid)."""
    slot = db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(404, "Slot not found")
    assert_client_access(slot.client_id, current_user, db)
    slot.scheduled_at = req.scheduled_at
    db.commit()
    return _serialize(slot)
