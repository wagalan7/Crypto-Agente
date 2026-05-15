from datetime import datetime, timedelta
from typing import List
from sqlalchemy.orm import Session
from models import CalendarSlot, Client, ContentPiece


PLATFORM_BEST_TIMES = {
    "instagram": [{"hour": 7, "minute": 0}, {"hour": 12, "minute": 0}, {"hour": 20, "minute": 0}],
    "tiktok": [{"hour": 6, "minute": 0}, {"hour": 10, "minute": 0}, {"hour": 19, "minute": 0}],
    "youtube": [{"hour": 15, "minute": 0}, {"hour": 20, "minute": 0}],
    "twitter": [{"hour": 8, "minute": 0}, {"hour": 18, "minute": 0}],
    "linkedin": [{"hour": 8, "minute": 0}, {"hour": 12, "minute": 0}],
}

OBJECTIVE_SEQUENCE = ["attract", "connect", "authority", "sell", "connect", "attract", "break_objection"]

FORMAT_BY_OBJECTIVE = {
    "attract": ["reels", "shorts"],
    "connect": ["story", "post"],
    "authority": ["carousel", "youtube"],
    "sell": ["reels", "story"],
    "break_objection": ["carousel", "post"],
}


class CalendarService:
    def __init__(self, db: Session):
        self.db = db

    def generate_week(self, client_id: int, start_date: datetime, frequency_per_week: int = 7) -> List[CalendarSlot]:
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return []

        platforms = client.platforms or ["instagram"]
        primary_platform = platforms[0].lower() if platforms else "instagram"
        best_times = PLATFORM_BEST_TIMES.get(primary_platform, PLATFORM_BEST_TIMES["instagram"])

        slots = []
        days_with_content = sorted(set(range(0, min(frequency_per_week, 7))))

        for i, day_offset in enumerate(days_with_content):
            slot_date = start_date + timedelta(days=day_offset)
            time_info = best_times[i % len(best_times)]
            scheduled = slot_date.replace(hour=time_info["hour"], minute=time_info["minute"], second=0)

            objective = OBJECTIVE_SEQUENCE[i % len(OBJECTIVE_SEQUENCE)]
            format_options = FORMAT_BY_OBJECTIVE.get(objective, ["post"])
            fmt = format_options[i % len(format_options)]

            slot = CalendarSlot(
                client_id=client_id,
                scheduled_at=scheduled,
                platform=primary_platform,
                format=fmt,
                objective=objective,
                status="planned",
            )
            self.db.add(slot)
            slots.append(slot)

        self.db.commit()
        return slots

    def get_upcoming(self, client_id: int, days: int = 14) -> List[CalendarSlot]:
        now = datetime.utcnow()
        until = now + timedelta(days=days)
        return (
            self.db.query(CalendarSlot)
            .filter(
                CalendarSlot.client_id == client_id,
                CalendarSlot.scheduled_at >= now,
                CalendarSlot.scheduled_at <= until,
            )
            .order_by(CalendarSlot.scheduled_at)
            .all()
        )

    def attach_content(self, slot_id: int, content_id: int) -> CalendarSlot:
        slot = self.db.query(CalendarSlot).filter(CalendarSlot.id == slot_id).first()
        content = self.db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
        if slot and content:
            slot.content_id = content_id
            content.scheduled_at = slot.scheduled_at
            slot.status = "ready"
            self.db.commit()
        return slot
