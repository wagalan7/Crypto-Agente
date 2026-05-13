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


# ── Google Ads Campaign Reports ───────────────────────────────

@dataclass
class CampaignReport:
    campaign_id:  str
    campaign_name: str
    status:       str
    impressions:  int   = 0
    clicks:       int   = 0
    ctr:          float = 0.0
    avg_cpc:      float = 0.0
    cost:         float = 0.0
    conversions:  float = 0.0
    error:        Optional[str] = None


async def fetch_google_ads_campaigns(
    developer_token: str,
    customer_id: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    mcc_id: str = "",
    date_range: str = "LAST_30_DAYS",
) -> list[CampaignReport]:
    """Fetch campaign performance metrics from Google Ads API using GAQL."""
    import os
    cid = customer_id.replace("-", "").replace(" ", "")
    try:
        # Get access token
        async with httpx.AsyncClient(timeout=15) as c:
            tr = await c.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            tokens = tr.json()
        access_token = tokens.get("access_token", "")
        if not access_token:
            return [CampaignReport("", "", "", error=f"Token inválido: {tokens.get('error_description', tokens.get('error', '?'))}")]

        headers = {
            "Authorization":   f"Bearer {access_token}",
            "developer-token": developer_token,
            "Content-Type":    "application/json",
        }
        clean_mcc = mcc_id.replace("-", "").replace(" ", "")
        if clean_mcc:
            headers["login-customer-id"] = clean_mcc

        # GAQL query — campaign metrics last N days
        query = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.status,
              metrics.impressions,
              metrics.clicks,
              metrics.ctr,
              metrics.average_cpc,
              metrics.cost_micros,
              metrics.conversions
            FROM campaign
            WHERE segments.date DURING {date_range}
            ORDER BY metrics.cost_micros DESC
            LIMIT 50
        """
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://googleads.googleapis.com/v19/customers/{cid}/googleAds:search",
                headers=headers,
                json={"query": query},
            )
            try:
                data = r.json()
            except Exception:
                return [CampaignReport("", "", "", error=f"HTTP {r.status_code}: {r.text[:200]}")]

        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            return [CampaignReport("", "", "", error=msg)]

        results = []
        for row in data.get("results", []):
            camp = row.get("campaign", {})
            m    = row.get("metrics", {})
            results.append(CampaignReport(
                campaign_id=camp.get("id", ""),
                campaign_name=camp.get("name", ""),
                status=camp.get("status", ""),
                impressions=int(m.get("impressions", 0)),
                clicks=int(m.get("clicks", 0)),
                ctr=round(float(m.get("ctr", 0)) * 100, 2),
                avg_cpc=round(int(m.get("averageCpc", 0)) / 1_000_000, 2),
                cost=round(int(m.get("costMicros", 0)) / 1_000_000, 2),
                conversions=round(float(m.get("conversions", 0)), 1),
            ))
        return results if results else [CampaignReport("", "", "", error="Nenhuma campanha encontrada no período.")]

    except Exception as e:
        return [CampaignReport("", "", "", error=str(e))]


async def fetch_facebook_ad_insights(page_id: str, token: str, date_preset: str = "last_30d") -> list[dict]:
    """Fetch Facebook Page post insights summary."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://graph.facebook.com/v19.0/{page_id}/insights",
                params={
                    "metric": "page_impressions,page_reach,page_post_engagements,page_fan_adds",
                    "period": "day",
                    "date_preset": date_preset,
                    "access_token": token,
                },
            )
            data = r.json()
        if "error" in data:
            return [{"error": data["error"].get("message", str(data["error"]))}]
        summary = {}
        for item in data.get("data", []):
            name = item.get("name", "")
            values = item.get("values", [])
            total = sum(v.get("value", 0) for v in values if isinstance(v.get("value"), (int, float)))
            summary[name] = total
        return [summary]
    except Exception as e:
        return [{"error": str(e)}]
