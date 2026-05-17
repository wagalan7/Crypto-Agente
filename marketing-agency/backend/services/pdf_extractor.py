"""PDF extractor — turns uploaded PDFs into KnowledgeItem.content + summary.

Uses pypdf (pure Python, no system deps). Returns plain text capped at
~40k chars to keep DB rows reasonable; the AI summarizer condenses further.
"""
from __future__ import annotations
import io
from typing import Optional

MAX_CHARS = 40_000


def extract_text_from_bytes(data: bytes) -> str:
    """Extract text from a PDF byte buffer. Returns "" on any failure."""
    if not data:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        budget = MAX_CHARS
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if not t:
                continue
            if len(t) > budget:
                parts.append(t[:budget])
                break
            parts.append(t)
            budget -= len(t)
            if budget <= 0:
                break
        return "\n\n".join(parts).strip()
    except Exception:
        return ""


async def summarize(text: str, *, title: Optional[str] = None) -> dict:
    """Use Groq to produce summary + key insights + voice signals.

    Returns {summary, key_insights[], voice_signals[]} — empty lists on failure
    so callers can treat the result uniformly.
    """
    import os
    fallback = {"summary": "", "key_insights": [], "voice_signals": []}
    if not text or not os.getenv("GROQ_API_KEY"):
        return fallback
    text_snippet = text[:8000]
    from groq import AsyncGroq
    import json as _json
    system = (
        "Você extrai inteligência utilizável de textos para um criador de conteúdo.\n"
        "Devolva APENAS JSON neste formato exato:\n"
        '{"summary": "2-3 frases", "key_insights": ["ideia 1", ...], "voice_signals": ["expressão/jargão", ...]}\n'
        "- summary: digesto denso, sem clichê\n"
        "- key_insights: até 6 ideias acionáveis que servem de matéria-prima pra posts\n"
        "- voice_signals: até 8 palavras/expressões/frases-marca que mostram a voz do autor"
    )
    user = f"TÍTULO: {title or '-'}\n\nTEXTO:\n{text_snippet}"
    try:
        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        resp = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=900,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        data = _json.loads(raw)
        return {
            "summary": (data.get("summary") or "").strip(),
            "key_insights": [s.strip() for s in (data.get("key_insights") or []) if s],
            "voice_signals": [s.strip() for s in (data.get("voice_signals") or []) if s],
        }
    except Exception:
        return fallback
