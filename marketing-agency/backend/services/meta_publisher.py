"""
Meta (Instagram + Facebook) publisher using stored Page Access Tokens.

User flow:
  1. User manually creates SocialAccount via the UI with:
     - Instagram Business Account ID + Page Access Token
     - Facebook Page ID + Page Access Token
  2. Content must have a publicly-accessible media_url (image or video).
  3. publish() picks the right account for the content's platform and posts.

Graph API docs:
  - IG: https://developers.facebook.com/docs/instagram-api/guides/content-publishing
  - FB: https://developers.facebook.com/docs/pages-api/posts
"""
import asyncio
import httpx
from typing import Optional, Tuple
from models import SocialAccount, ContentPiece

GRAPH_API = "https://graph.facebook.com/v21.0"


class PublishError(Exception):
    pass


def _extract_meta_error(resp: httpx.Response) -> str:
    """Pull the most informative message from a Meta error response."""
    try:
        body = resp.json()
        err = body.get("error", {})
        parts = [
            err.get("message"),
            f"(code {err.get('code')}, subcode {err.get('error_subcode')})" if err.get("code") else None,
            f"user_msg: {err.get('error_user_msg')}" if err.get("error_user_msg") else None,
            f"trace: {err.get('fbtrace_id')}" if err.get("fbtrace_id") else None,
        ]
        return " | ".join(p for p in parts if p)
    except Exception:
        return resp.text[:500]


async def publish_facebook(account: SocialAccount, content: ContentPiece) -> str:
    """Post to a Facebook Page. Returns the post ID."""
    caption = content.copy or content.hook or content.title or ""
    media_url = content.media_url

    async with httpx.AsyncClient(timeout=60.0) as client:
        if media_url:
            # Photo post
            r = await client.post(
                f"{GRAPH_API}/{account.account_id}/photos",
                data={"url": media_url, "caption": caption, "access_token": account.access_token},
            )
        else:
            # Text-only post
            r = await client.post(
                f"{GRAPH_API}/{account.account_id}/feed",
                data={"message": caption, "access_token": account.access_token},
            )
        if r.status_code >= 400:
            raise PublishError(f"FB publish failed: {_extract_meta_error(r)}")
        data = r.json()
        return data.get("post_id") or data.get("id", "")


async def publish_instagram(account: SocialAccount, content: ContentPiece) -> str:
    """Publish to Instagram. Returns the published media ID.

    IG requires:
      1. Create a media container (image_url or video_url).
      2. For videos, poll status until FINISHED.
      3. Publish the container.
    """
    caption = content.copy or content.hook or content.title or ""
    media_url = content.media_url
    if not media_url:
        raise PublishError("Instagram requires a public media_url (image or video URL)")

    is_video = any(media_url.lower().endswith(ext) for ext in [".mp4", ".mov", ".m4v"])

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Step 1: create container
        params = {"caption": caption, "access_token": account.access_token}
        if is_video:
            params["media_type"] = "REELS"
            params["video_url"] = media_url
        else:
            params["image_url"] = media_url

        r = await client.post(f"{GRAPH_API}/{account.account_id}/media", data=params)
        if r.status_code >= 400:
            raise PublishError(f"IG container creation failed: {_extract_meta_error(r)}")
        container_id = r.json()["id"]

        # Step 2: poll status (videos take time to process)
        if is_video:
            for _ in range(30):  # up to ~5 minutes
                await asyncio.sleep(10)
                s = await client.get(
                    f"{GRAPH_API}/{container_id}",
                    params={"fields": "status_code", "access_token": account.access_token},
                )
                status = s.json().get("status_code")
                if status == "FINISHED":
                    break
                if status == "ERROR":
                    raise PublishError(f"IG container processing failed: {s.text}")
            else:
                raise PublishError("IG container timed out waiting for FINISHED status")

        # Step 3: publish
        p = await client.post(
            f"{GRAPH_API}/{account.account_id}/media_publish",
            data={"creation_id": container_id, "access_token": account.access_token},
        )
        if p.status_code >= 400:
            raise PublishError(f"IG publish failed: {_extract_meta_error(p)}")
        return p.json()["id"]


async def test_account(account: SocialAccount) -> Tuple[bool, str]:
    """Verify the stored token works. Returns (ok, message)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{GRAPH_API}/{account.account_id}",
            params={"fields": "id,name,username", "access_token": account.access_token},
        )
        if r.status_code >= 400:
            return False, _extract_meta_error(r)
        data = r.json()
        return True, data.get("name") or data.get("username") or str(data.get("id"))


async def publish(account: SocialAccount, content: ContentPiece) -> str:
    if account.platform == "instagram":
        return await publish_instagram(account, content)
    if account.platform == "facebook":
        return await publish_facebook(account, content)
    raise PublishError(f"Unsupported platform: {account.platform}")
