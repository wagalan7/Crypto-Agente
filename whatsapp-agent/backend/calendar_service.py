from __future__ import annotations
from datetime import datetime, timedelta
import database as db

WORKING_DAYS = [0, 1, 2, 3, 4]  # Seg–Sex


def get_available_slots(tenant: dict, days_ahead: int = 7, limit: int = 10) -> list[datetime]:
    tenant_id = tenant["id"]
    start_h = tenant["working_hours_start"]
    end_h = tenant["working_hours_end"]
    duration = tenant["session_minutes"]

    now = datetime.now()
    check = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_date = now + timedelta(days=days_ahead)

    booked = db.get_appointments_in_range(tenant_id, now.isoformat(), end_date.isoformat())
    booked_times = {r["scheduled_at"][:16] for r in booked}

    slots = []
    while check <= end_date and len(slots) < limit:
        if (
            check.weekday() in WORKING_DAYS
            and start_h <= check.hour < end_h
            and check.isoformat()[:16] not in booked_times
        ):
            slots.append(check)
        check += timedelta(minutes=duration)

    return slots


def format_slots(slots: list[datetime]) -> list[str]:
    day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    return [
        f"{day_names[s.weekday()]}, {s.strftime('%d/%m')} às {s.strftime('%H:%M')}"
        for s in slots
    ]


def get_next_appointment(tenant_id: int, phone: str) -> dict | None:
    appts = db.get_appointments_by_phone(tenant_id, phone)
    return appts[0] if appts else None


def format_appointment(appt: dict) -> str:
    dt = datetime.fromisoformat(appt["scheduled_at"])
    day_names = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    return f"{day_names[dt.weekday()]}, {dt.strftime('%d/%m')} às {dt.strftime('%H:%M')}"
