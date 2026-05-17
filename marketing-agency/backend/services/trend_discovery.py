"""Auto-discover trending topics from Reddit public JSON.

Uses Reddit's public `.json` endpoints — no auth required for read-only access.
We dedupe by (source, source_id) and keep the rolling top results per subreddit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

import httpx
from sqlalchemy.orm import Session

from database import SessionLocal
from models import TrendTopic

logger = logging.getLogger(__name__)

# Default subreddits — Brazilian-Portuguese plus a few global creator/marketing
# communities. Override via env DISCOVERY_SUBREDDITS="brasil,empreendedorismo".
DEFAULT_SUBREDDITS = [
    "brasil",
    "empreendedorismo",
    "InvestimentosBrasil",
    "desabafos",
    "marketing",
    "socialmedia",
]

USER_AGENT = "ContentAI/1.0 (trend discovery)"
TRENDS_TTL_DAYS = 14


async def _fetch_subreddit_top(client: httpx.AsyncClient, subreddit: str, limit: int = 10) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
        if r.status_code != 200:
            logger.warning("Reddit %s returned %s", subreddit, r.status_code)
            return []
        data = r.json()
        return [c["data"] for c in data.get("data", {}).get("children", []) if c.get("kind") == "t3"]
    except Exception as e:
        logger.warning("Reddit fetch %s failed: %s", subreddit, e)
        return []


async def discover_trends(subreddits: Iterable[str] | None = None, limit_per_sub: int = 10) -> int:
    """Fetch top-of-day posts from each subreddit and upsert into TrendTopic.

    Returns number of new topics inserted.
    """
    subs = list(subreddits or DEFAULT_SUBREDDITS)
    inserted = 0
    async with httpx.AsyncClient() as client:
        all_posts: list[tuple[str, dict]] = []
        for sub in subs:
            posts = await _fetch_subreddit_top(client, sub, limit_per_sub)
            for p in posts:
                all_posts.append((sub, p))

    if not all_posts:
        return 0

    db: Session = SessionLocal()
    try:
        # Build a set of existing (source, source_id) to dedupe in one query
        ids = [p.get("id") for _, p in all_posts if p.get("id")]
        existing = set()
        if ids:
            rows = db.query(TrendTopic.source_id).filter(
                TrendTopic.source == "reddit",
                TrendTopic.source_id.in_(ids),
            ).all()
            existing = {r[0] for r in rows}

        for sub, p in all_posts:
            sid = p.get("id")
            if not sid or sid in existing:
                continue
            t = TrendTopic(
                source="reddit",
                source_id=sid,
                subreddit=sub,
                title=(p.get("title") or "")[:1000],
                url=f"https://reddit.com{p.get('permalink', '')}" if p.get("permalink") else p.get("url"),
                score=int(p.get("score") or 0),
                num_comments=int(p.get("num_comments") or 0),
                locale="pt-BR" if sub in {"brasil", "empreendedorismo", "InvestimentosBrasil", "desabafos"} else "en",
                tags=[],
            )
            db.add(t)
            inserted += 1
            existing.add(sid)
        if inserted:
            db.commit()

        # Prune anything older than TTL to keep table small
        cutoff = datetime.utcnow() - timedelta(days=TRENDS_TTL_DAYS)
        db.query(TrendTopic).filter(TrendTopic.discovered_at < cutoff).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    logger.info("trend discovery: inserted=%s subs=%s", inserted, len(subs))
    return inserted
