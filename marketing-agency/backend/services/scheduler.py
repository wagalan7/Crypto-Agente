"""Background scheduler: auto-publish + auto-fetch metrics.

Two jobs run inside the FastAPI process via APScheduler AsyncIOScheduler:

  - publish_due_slots (every 60s): finds CalendarSlot rows whose scheduled_at
    has passed and whose attached ContentPiece is approved/recorded with media,
    then calls meta_publisher.publish. Marks slot.status='published' on success
    or records the error on failure (no retry storm — re-checks on next tick).

  - fetch_metrics_for_published (every 30 min): for posts published in the last
    30 days that have an external_post_id, pulls Meta insights and upserts a
    daily MetricsSnapshot bucket (one row per content per UTC day).

Both jobs swallow errors per-item so one bad account doesn't block the others.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from database import SessionLocal
from models import CalendarSlot, ContentPiece, SocialAccount, MetricsSnapshot
from services import meta_publisher
from services.trend_discovery import discover_trends

logger = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# Job 1: auto-publish
# ---------------------------------------------------------------------------
MAX_PUBLISH_ATTEMPTS = 5
# Backoff schedule (minutes): 1, 5, 15, 60, 240 — gives up after ~5h
BACKOFF_MINUTES = [1, 5, 15, 60, 240]


def _next_retry_delay(attempts: int) -> timedelta:
    idx = min(attempts, len(BACKOFF_MINUTES) - 1)
    return timedelta(minutes=BACKOFF_MINUTES[idx])


async def publish_due_slots() -> None:
    """Publish slots whose scheduled_at is in the past and content is ready.

    On failure: increment publish_attempts and set next_retry_at via exponential
    backoff. Stops trying after MAX_PUBLISH_ATTEMPTS and marks the piece dead.
    """
    db: Session = SessionLocal()
    try:
        now = datetime.utcnow()
        due = db.query(CalendarSlot).filter(
            CalendarSlot.scheduled_at <= now,
            CalendarSlot.status.in_(("planned", "ready", "scheduled")),
            CalendarSlot.content_id.isnot(None),
        ).limit(20).all()

        for slot in due:
            content = db.query(ContentPiece).filter(ContentPiece.id == slot.content_id).first()
            if not content:
                continue
            if content.status not in ("approved", "recorded"):
                continue
            # Respect backoff window — skip if a retry is scheduled in the future
            if content.next_retry_at and content.next_retry_at > now:
                continue
            # Give up after max attempts
            if (content.publish_attempts or 0) >= MAX_PUBLISH_ATTEMPTS:
                continue
            platform = (content.platform or "").lower()
            if platform not in ("instagram", "facebook"):
                continue
            if not content.media_url:
                continue

            acc = db.query(SocialAccount).filter(
                SocialAccount.client_id == content.client_id,
                SocialAccount.platform == platform,
                SocialAccount.is_active == True,
            ).first()
            if not acc:
                continue

            try:
                external_id = await meta_publisher.publish(acc, content)
                content.external_post_id = external_id
                content.status = "published"
                content.published_at = datetime.utcnow()
                content.publish_error = None
                content.publish_attempts = 0
                content.next_retry_at = None
                slot.status = "published"
                acc.last_error = None
                db.commit()
                logger.info(f"auto-published content={content.id} → {external_id}")
            except meta_publisher.PublishError as e:
                content.publish_attempts = (content.publish_attempts or 0) + 1
                content.publish_error = str(e)[:500]
                if content.publish_attempts >= MAX_PUBLISH_ATTEMPTS:
                    content.next_retry_at = None
                    logger.error(f"auto-publish gave up content={content.id} after {content.publish_attempts} attempts: {e}")
                else:
                    content.next_retry_at = datetime.utcnow() + _next_retry_delay(content.publish_attempts - 1)
                    logger.warning(
                        f"auto-publish failed content={content.id} attempt={content.publish_attempts}; "
                        f"retry at {content.next_retry_at.isoformat()}: {e}"
                    )
                acc.last_error = str(e)[:500]
                db.commit()
            except Exception as e:
                logger.exception(f"auto-publish unexpected error content={content.id}: {e}")
                db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job 2: auto-fetch metrics
# ---------------------------------------------------------------------------
async def fetch_metrics_for_published() -> None:
    """For each recently-published piece, fetch Meta insights and upsert a daily snapshot."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        pieces = db.query(ContentPiece).filter(
            ContentPiece.status == "published",
            ContentPiece.external_post_id.isnot(None),
            ContentPiece.published_at >= cutoff,
        ).limit(50).all()

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        for c in pieces:
            platform = (c.platform or "").lower()
            acc = db.query(SocialAccount).filter(
                SocialAccount.client_id == c.client_id,
                SocialAccount.platform == platform,
                SocialAccount.is_active == True,
            ).first()
            if not acc:
                continue
            try:
                metrics = await meta_publisher.fetch_insights(acc, c.external_post_id)
            except meta_publisher.PublishError as e:
                logger.warning(f"insights failed content={c.id}: {e}")
                continue
            except Exception as e:
                logger.exception(f"insights unexpected content={c.id}: {e}")
                continue
            if not metrics:
                continue

            # Daily bucket: one row per content per UTC day. Upsert.
            snap = db.query(MetricsSnapshot).filter(
                MetricsSnapshot.content_id == c.id,
                MetricsSnapshot.recorded_at >= today_start,
            ).first()
            if not snap:
                snap = MetricsSnapshot(
                    client_id=c.client_id,
                    content_id=c.id,
                    platform=platform,
                )
                db.add(snap)
            for col, val in metrics.items():
                setattr(snap, col, val)
            db.commit()
            logger.info(f"metrics snapshot content={c.id}: {metrics}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(publish_due_slots, "interval", seconds=60, id="publish_due_slots",
                        max_instances=1, coalesce=True)
    _scheduler.add_job(fetch_metrics_for_published, "interval", minutes=30,
                        id="fetch_metrics", max_instances=1, coalesce=True)
    _scheduler.add_job(_trend_discovery_job, "interval", hours=6,
                        id="trend_discovery", max_instances=1, coalesce=True,
                        next_run_time=datetime.utcnow() + timedelta(minutes=2))
    _scheduler.add_job(refresh_meta_tokens, "interval", hours=24,
                        id="refresh_tokens", max_instances=1, coalesce=True,
                        next_run_time=datetime.utcnow() + timedelta(minutes=5))
    _scheduler.start()
    logger.info("scheduler started (publish=60s, metrics=30m, trends=6h, tokens=24h)")


async def _trend_discovery_job() -> None:
    try:
        n = await discover_trends()
        logger.info(f"trend discovery tick: inserted={n}")
    except Exception as e:
        logger.exception(f"trend discovery failed: {e}")


async def refresh_meta_tokens() -> None:
    """Refresh any SocialAccount token that expires within the next 7 days.

    Silently skips accounts when META_APP_ID/SECRET are missing.
    """
    import os
    if not (os.getenv("META_APP_ID") and os.getenv("META_APP_SECRET")):
        return
    db: Session = SessionLocal()
    try:
        soon = datetime.utcnow() + timedelta(days=7)
        accs = db.query(SocialAccount).filter(
            SocialAccount.is_active == True,
            SocialAccount.expires_at.isnot(None),
            SocialAccount.expires_at <= soon,
        ).all()
        for a in accs:
            try:
                new_token, expires_in = await meta_publisher.refresh_long_lived_token(a.access_token)
                a.access_token = new_token
                if expires_in:
                    a.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                a.last_error = None
                db.commit()
                logger.info(f"refreshed meta token social_account={a.id}")
            except Exception as e:
                a.last_error = f"refresh failed: {str(e)[:300]}"
                db.commit()
                logger.warning(f"meta token refresh failed for account={a.id}: {e}")
    finally:
        db.close()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
