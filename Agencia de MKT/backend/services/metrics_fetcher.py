"""
Real-time metrics fetcher for published posts.
Fetches live data from Facebook, Instagram and Twitter APIs.
"""
import httpx
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlatformMetrics:
    platform: str
    post_id: str
    impressions: Optional[int]  = None
    reach:       Optional[int]  = None
    likes:       Optional[int]  = None
    comments:    Optional[int]  = None
    shares:      Optional[int]  = None
    clicks:      Optional[int]  = None
    saves:       Optional[int]  = None
    engagements: Optional[int]  = None
    url:         Optional[str]  = None
    error:       Optional[str]  = None
    raw:         dict           = field(default_factory=dict)


async def fetch_facebook_metrics(post_id: str, token: str) -> PlatformMetrics:
    metrics = ["post_impressions", "post_reach", "post_reactions_by_type_total",
               "post_clicks", "post_shares"]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://graph.facebook.com/v19.0/{post_id}/insights",
                params={"metric": ",".join(metrics), "access_token": token},
            )
            data = r.json()

        if "error" in data:
            return PlatformMetrics(platform="facebook", post_id=post_id,
                                   error=data["error"].get("message", str(data["error"])))

        vals: dict = {}
        for item in data.get("data", []):
            vals[item["name"]] = item.get("values", [{}])[-1].get("value", 0)

        reactions = vals.get("post_reactions_by_type_total", {})
        likes = sum(reactions.values()) if isinstance(reactions, dict) else reactions

        return PlatformMetrics(
            platform="facebook", post_id=post_id,
            impressions=vals.get("post_impressions"),
            reach=vals.get("post_reach"),
            likes=likes,
            clicks=vals.get("post_clicks"),
            shares=vals.get("post_shares"),
            url=f"https://www.facebook.com/{post_id.replace('_', '/posts/')}",
            raw=vals,
        )
    except Exception as e:
        return PlatformMetrics(platform="facebook", post_id=post_id, error=str(e))


async def fetch_instagram_metrics(media_id: str, token: str) -> PlatformMetrics:
    metrics = ["impressions", "reach", "likes", "comments", "shares", "saved", "total_interactions"]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://graph.facebook.com/v19.0/{media_id}/insights",
                params={"metric": ",".join(metrics), "access_token": token},
            )
            data = r.json()

        if "error" in data:
            return PlatformMetrics(platform="instagram", post_id=media_id,
                                   error=data["error"].get("message", str(data["error"])))

        vals: dict = {item["name"]: item.get("values", [{}])[-1].get("value", 0)
                      for item in data.get("data", [])}

        return PlatformMetrics(
            platform="instagram", post_id=media_id,
            impressions=vals.get("impressions"),
            reach=vals.get("reach"),
            likes=vals.get("likes"),
            comments=vals.get("comments"),
            shares=vals.get("shares"),
            saves=vals.get("saved"),
            engagements=vals.get("total_interactions"),
            url=f"https://www.instagram.com/p/{media_id}/",
            raw=vals,
        )
    except Exception as e:
        return PlatformMetrics(platform="instagram", post_id=media_id, error=str(e))


async def fetch_twitter_metrics(tweet_id: str, bearer_token: str) -> PlatformMetrics:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://api.twitter.com/2/tweets/{tweet_id}",
                params={"tweet.fields": "public_metrics"},
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
            data = r.json()

        if "errors" in data:
            return PlatformMetrics(platform="twitter", post_id=tweet_id,
                                   error=data["errors"][0].get("detail", str(data["errors"])))

        pm = data.get("data", {}).get("public_metrics", {})
        return PlatformMetrics(
            platform="twitter", post_id=tweet_id,
            likes=pm.get("like_count"),
            comments=pm.get("reply_count"),
            shares=pm.get("retweet_count"),
            impressions=pm.get("impression_count"),
            clicks=pm.get("url_link_clicks"),
            url=f"https://twitter.com/i/web/status/{tweet_id}",
            raw=pm,
        )
    except Exception as e:
        return PlatformMetrics(platform="twitter", post_id=tweet_id, error=str(e))
