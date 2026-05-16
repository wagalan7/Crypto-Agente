import re
import httpx
from typing import Optional


async def fetch_site_context(url: str, max_chars: int = 4000) -> str:
    """Fetch URL and extract readable text: title, meta description, headings, body text.
    Returns a compact summary suitable for feeding to an LLM."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; ContentAI/1.0)"
        }) as client:
            r = await client.get(url)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return f"[ERRO ao acessar {url}: {e}]"

    # Extract title
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = _clean(m.group(1))

    # Extract meta description
    desc = ""
    m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        desc = _clean(m.group(1))

    # Extract og:title / og:description as fallback
    og_title = _extract_meta_prop(html, "og:title")
    og_desc = _extract_meta_prop(html, "og:description")

    # Extract h1/h2 headings
    headings = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", html, re.IGNORECASE | re.DOTALL)
    headings = [_clean(h) for h in headings if _clean(h)][:10]

    # Strip scripts, styles, tags → plain text
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    body = _clean(body)
    body = body[:max_chars]

    parts = [f"URL: {url}"]
    if title: parts.append(f"TÍTULO: {title}")
    if desc: parts.append(f"META DESCRIÇÃO: {desc}")
    if og_title and og_title != title: parts.append(f"OG TÍTULO: {og_title}")
    if og_desc and og_desc != desc: parts.append(f"OG DESCRIÇÃO: {og_desc}")
    if headings: parts.append("CABEÇALHOS:\n  - " + "\n  - ".join(headings))
    if body: parts.append(f"CONTEÚDO:\n{body}")
    return "\n\n".join(parts)


def _extract_meta_prop(html: str, prop: str) -> Optional[str]:
    m = re.search(
        rf'<meta\s+property=["\']({re.escape(prop)})["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    return _clean(m.group(2)) if m else None


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return s.strip()
