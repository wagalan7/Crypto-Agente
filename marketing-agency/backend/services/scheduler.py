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
async def publish_due_slots() -> None:
    """Publish slots whose scheduled_at is in the past and content is ready."""
    db: Session = SessionLocal()
    try:
        now = datetime.utcnow()
        # Slots due, not yet published, with content attached and approved+
        due = db.query(CalendarSlot).filter(
            CalendarSlot.scheduled_at <= now,
            CalendarSlot.status.in_(("planned", "ready", "scheduled")),
            CalendarSlot.content_id.isnot(None),
        ).limit(20).all()

        for slot in due:
            content = db.query(ContentPiece).filter(ContentPiece.id == slot.content_id).first()
            if not content:
                continue
            # Only attempt if content is approved/recorded and on a supported platform
            if content.status not in ("approved", "recorded"):
                continue
            platform = (content.platform or "").lower()
            if platform not in ("instagram", "facebook"):
                continue
            if not content.media_url:
                continue  # IG requires public media

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
                slot.status = "published"
                acc.last_error = None
                db.commit()
                logger.info(f"auto-published content={content.id} → {external_id}")
            except meta_publisher.PublishError as e:
                content.publish_error = str(e)[:500]
                acc.last_error = str(e)[:500]
                db.commit()
                logger.warning(f"auto-publish failed content={content.id}: {e}")
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
    _scheduler.start()
    logger.info("scheduler started (publish=60s, metrics=30m, trends=6h)")


async def _trend_discovery_job() -> None:
    try:
        n = await discover_trends()
        logger.info(f"trend discovery tick: inserted={n}")
    except Exception as e:
        logger.exception(f"trend discovery failed: {e}")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
