"""Vision analyzer — extract aesthetic + narrative signals from image references.

Uses Groq's vision model (llama-3.2-90b-vision-preview). Accepts a public
image URL (preferred — fast) or raw bytes (base64-encoded inline).

Output is a structured dict the InspirationAnalyzer agent merges with its
text analysis. Failure is silent (returns empty dict) so the rest of the
inspiration flow keeps working without vision.
"""
from __future__ import annotations
import os
import base64
import json
from typing import Optional, Dict, Any


VISION_MODEL = "llama-3.2-90b-vision-preview"

SYSTEM = (
    "Você é um diretor criativo analisando referências visuais. "
    "Olhe a imagem e devolva APENAS JSON neste formato exato:\n"
    '{"composition": "...", "palette": ["cor1", "cor2"], "mood": "...", '
    '"layout": "...", "identity": "...", "hook_visual": "...", '
    '"emotion": "...", "what_works": ["...", "..."]}\n'
    "- composition: enquadramento, regra dos terços, peso visual\n"
    "- palette: 2-4 cores dominantes (nomes curtos: 'bege quente', 'preto')\n"
    "- mood: atmosfera em 1-3 palavras (ex: 'intimista melancólico')\n"
    "- layout: arranjo de elementos (texto/imagem/CTA)\n"
    "- identity: identidade visual percebida (ex: 'editorial minimalista', 'pop saturado')\n"
    "- hook_visual: o que prende o olhar primeiro\n"
    "- emotion: emoção que a imagem evoca\n"
    "- what_works: 2-3 coisas que o criador deve replicar"
)


async def analyze_image(
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> Dict[str, Any]:
    """Return structured visual analysis or {} on any failure."""
    if not os.getenv("GROQ_API_KEY"):
        return {}
    if not image_url and not image_bytes:
        return {}

    if image_url:
        image_block = {"type": "image_url", "image_url": {"url": image_url}}
    else:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"
        image_block = {"type": "image_url", "image_url": {"url": data_url}}

    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        resp = await client.chat.completions.create(
            model=VISION_MODEL,
            max_tokens=600,
            temperature=0.4,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": SYSTEM},
                        image_block,
                    ],
                }
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Strip markdown fences if model wraps the JSON
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        # Find first { ... last }
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
