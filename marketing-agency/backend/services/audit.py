"""Append-only audit log helper.

Cheap to call from anywhere. Swallows DB errors so audit failures never
break the underlying action.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from models import AuditLog, User

logger = logging.getLogger(__name__)


def log_action(
    db: Session,
    *,
    user: Optional[User],
    action: str,
    client_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    meta: Optional[dict] = None,
    request: Optional[Request] = None,
) -> None:
    try:
        ip = None
        if request is not None:
            xff = request.headers.get("x-forwarded-for")
            ip = (xff.split(",")[0].strip() if xff else (request.client.host if request.client else None))
        entry = AuditLog(
            user_id=user.id if user else None,
            client_id=client_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            meta=meta or {},
            ip=ip,
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.warning(f"audit log failed (action={action}): {e}")
        try:
            db.rollback()
        except Exception:
            pass
