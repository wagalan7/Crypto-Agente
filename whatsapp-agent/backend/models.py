from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class Intent(str, Enum):
    confirm = "confirm"
    schedule = "schedule"
    reschedule = "reschedule"
    new_patient = "new_patient"
    other = "other"


class Action(str, Enum):
    none = "none"
    list_slots = "list_slots"
    create = "create"
    update = "update"
    confirm = "confirm"


class AgentResponse(BaseModel):
    intent: Intent
    action: Action
    data: dict[str, Any]
    response_text: str


class Appointment(BaseModel):
    id: Optional[int] = None
    patient_name: str
    phone: str
    scheduled_at: datetime
    confirmed: bool = False
    notes: Optional[str] = None


class WhatsAppMessage(BaseModel):
    phone: str
    text: str
    timestamp: Optional[datetime] = None


class WebhookPayload(BaseModel):
    data: Optional[dict] = None
