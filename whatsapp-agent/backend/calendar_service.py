from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import database as db

_TZ = ZoneInfo("America/Sao_Paulo")

_DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4]   # Seg–Sex
_DEFAULT_BLOCKED_HOURS = [12, 13, 14]      # Almoço


def _working_days(tenant: dict) -> list[int]:
    raw = tenant.get("working_days", "") or ""
    try:
        return [int(d) for d in raw.split(",") if d.strip().isdigit()]
    except Exception:
        return _DEFAULT_WORKING_DAYS


def _blocked_hours(tenant: dict) -> list[int]:
    raw = tenant.get("blocked_hours", "") or ""
    try:
        return [int(h) for h in raw.split(",") if h.strip().isdigit()]
    except Exception:
        return _DEFAULT_BLOCKED_HOURS


def _blocked_dates(tenant: dict) -> set[str]:
    """Datas bloqueadas (feriados/férias) — set de YYYY-MM-DD."""
    raw = tenant.get("blocked_dates", "") or ""
    return {d.strip() for d in raw.split(",") if d.strip()}


def get_available_slots(tenant: dict, days_ahead: int = 7, limit: int = 10) -> list[datetime]:
    tenant_id = tenant["id"]
    start_h = tenant["working_hours_start"]
    end_h = tenant["working_hours_end"]
    duration = tenant["session_minutes"]
    working_days = _working_days(tenant)
    blocked_hours = _blocked_hours(tenant)
    blocked_dates = _blocked_dates(tenant)

    now = datetime.now(_TZ).replace(tzinfo=None)  # naive, horário de Brasília
    check = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_date = now + timedelta(days=days_ahead)

    booked = db.get_appointments_in_range(tenant_id, now.isoformat(), end_date.isoformat())
    booked_dts = []
    for r in booked:
        try:
            booked_dts.append(datetime.fromisoformat(r["scheduled_at"]))
        except Exception:
            pass

    def _conflicts(slot: datetime) -> bool:
        """Slot conflita se está a menos de `duration` minutos de qualquer consulta existente."""
        for b in booked_dts:
            if abs((slot - b).total_seconds()) < duration * 60:
                return True
        return False

    slots = []
    while check <= end_date and len(slots) < limit:
        if (
            check.weekday() in working_days
            and start_h <= check.hour < end_h
            and check.hour not in blocked_hours
            and check.strftime("%Y-%m-%d") not in blocked_dates
            and not _conflicts(check)
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
