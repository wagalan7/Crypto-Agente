import urllib.parse
import random


def generate_image_url(prompt: str, width: int = 1024, height: int = 1024, seed: int | None = None) -> str:
    """Generate a public image URL using Pollinations.ai (free, no API key).
    The URL is stable and fetchable by Meta's Graph API."""
    if not prompt:
        prompt = "minimal modern abstract background"
    if seed is None:
        seed = random.randint(1, 999_999_999)
    encoded = urllib.parse.quote(prompt[:500], safe="")
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={width}&height={height}&nologo=true&seed={seed}&model=flux"
    )


def aspect_for_format(fmt: str) -> tuple[int, int]:
    """Return (width, height) appropriate for the content format."""
    fmt = (fmt or "").lower()
    if fmt in ("reels", "shorts", "story", "tiktok"):
        return (1024, 1820)  # 9:16
    if fmt in ("carousel", "post"):
        return (1024, 1024)  # 1:1
    if fmt in ("youtube",):
        return (1280, 720)   # 16:9
    return (1024, 1024)
