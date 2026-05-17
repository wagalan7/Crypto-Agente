"""Humanizer — turns LLM-flavored output into copy that sounds like the creator.

Two layers:
1. Deterministic cleanup (encoding, robotic transitions, em-dash overload, "IA-isms").
   Runs on every agent output via `clean()` — zero LLM cost, zero latency.
2. Optional rewrite (`humanize_with_voice`) — uses Groq + persona language patterns
   for a deeper rewrite. Use sparingly (only on hooks, CTAs, final scripts).

The goal: no reader should ever think "this looks like ChatGPT wrote it".
"""
from __future__ import annotations
import os
import re
import unicodedata
from typing import Optional

# Common encoding mojibake (UTF-8 read as Latin-1 etc) → correct char
_MOJIBAKE_FIX = {
    "Ã£": "ã", "Ã©": "é", "Ã¡": "á", "Ã³": "ó", "Ãº": "ú", "Ã­": "í",
    "Ã§": "ç", "Ãµ": "õ", "Ã¢": "â", "Ãª": "ê", "Ã´": "ô", "Ã ": "à",
    "Ã‰": "É", "Ã": "Á", "Ã“": "Ó", "Ãš": "Ú", "Ã‡": "Ç",
    "â€™": "'", "â€œ": '"', "â€": '"', "â€“": "–", "â€”": "—",
    "â€¦": "…", "Â´": "'", "Â¨": "", "Â°": "°", "Â": "",
    "ï»¿": "",  # BOM
}

# Robotic LLM transitions that scream "AI wrote this"
_ROBOTIC_PHRASES = [
    (r"\bem suma\b", "no fim"),
    (r"\bem resumo\b", "no fim"),
    (r"\bportanto\b", "então"),
    (r"\bdesta forma\b", "assim"),
    (r"\bdessa maneira\b", "assim"),
    (r"\bvale ressaltar que\b", ""),
    (r"\bé importante destacar que\b", ""),
    (r"\bno mundo de hoje\b", ""),
    (r"\bem um mundo cada vez mais\b", ""),
    (r"\bnos dias de hoje\b", "hoje"),
    (r"\bna era digital\b", ""),
    (r"\bem constante evolução\b", ""),
    (r"\bna palma da mão\b", ""),
    (r"\bcom certeza\b", ""),
    (r"\bsem dúvida alguma\b", ""),
    (r"\b(em última instância|em última análise)\b", "no final"),
    (r"\b(através de|por meio de)\b", "com"),
    (r"\bnão se trata apenas de\b", "não é só"),
    (r"\bnão se trata só de\b", "não é só"),
    (r"\bmais do que nunca\b", ""),
    (r"\buma jornada\b", "um caminho"),
    (r"\bDesvende\b", "Veja"),
    (r"\bDescubra como\b", "Veja como"),
    (r"\bMergulhe\b", "Entre"),
    (r"\b(?:absolutamente )?fundamental\b", "essencial"),
    (r"\bgame[- ]changer\b", "virada de chave"),
]

# Emoji/symbol clusters that LLMs over-use
_EMOJI_OVERLOAD = re.compile(r"([✨🚀💡🎯⭐🔥])\1{2,}")  # 3+ in a row
_DOUBLE_SPACES = re.compile(r"  +")
_MANY_EM_DASHES = re.compile(r"\s*[–—]\s*[–—]\s*")  # em-dash chain


def fix_encoding(text: str) -> str:
    """Fix common mojibake and normalize unicode (NFC)."""
    if not text:
        return text
    out = text
    for bad, good in _MOJIBAKE_FIX.items():
        if bad in out:
            out = out.replace(bad, good)
    try:
        out = unicodedata.normalize("NFC", out)
    except Exception:
        pass
    return out


def strip_robotic(text: str) -> str:
    """Remove or soften phrases that scream 'AI wrote this'."""
    if not text:
        return text
    out = text
    for pat, repl in _ROBOTIC_PHRASES:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    # Collapse leading commas/spaces left over
    out = re.sub(r"^[\s,]+", "", out, flags=re.MULTILINE)
    out = re.sub(r"[\s,]+([.!?])", r"\1", out)
    return out


def normalize_whitespace(text: str) -> str:
    if not text:
        return text
    out = _EMOJI_OVERLOAD.sub(r"\1", text)
    out = _MANY_EM_DASHES.sub(" — ", out)
    out = _DOUBLE_SPACES.sub(" ", out)
    # Trim trailing whitespace per line + collapse 3+ blank lines
    lines = [ln.rstrip() for ln in out.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def clean(text: Optional[str]) -> str:
    """One-shot deterministic cleanup. Run on every LLM output."""
    if not text:
        return ""
    out = fix_encoding(text)
    out = strip_robotic(out)
    out = normalize_whitespace(out)
    return out


async def humanize_with_voice(
    text: str,
    *,
    persona_language: Optional[str] = None,
    creator_tone: Optional[str] = None,
    voice_signals: Optional[list[str]] = None,
) -> str:
    """Deeper rewrite via Groq when deterministic cleanup isn't enough.

    Falls back to clean() if no GROQ_API_KEY (tests, local).
    """
    cleaned = clean(text)
    if not cleaned or not os.getenv("GROQ_API_KEY"):
        return cleaned

    from groq import AsyncGroq
    signals = ""
    if voice_signals:
        signals = "\nPALAVRAS/EXPRESSÕES DO CRIADOR PARA INCORPORAR:\n- " + "\n- ".join(voice_signals[:8])
    lang = (persona_language or "")[:400]
    tone = (creator_tone or "")[:200]
    system = (
        "Você reescreve textos para soarem humanos, reais e específicos.\n"
        "REGRAS:\n"
        "1. NUNCA use clichês de IA (\"no mundo de hoje\", \"jornada\", \"desvende\", \"através de\").\n"
        "2. Frases curtas. Ritmo de fala, não de redação escolar.\n"
        "3. Use contrações naturais (pra, tá, tô) quando o tom permitir.\n"
        "4. Zero emojis decorativos. Só se já estavam no original.\n"
        "5. Preserve TODA a informação. Não invente fatos.\n"
        "6. Mantenha o mesmo tamanho aproximado.\n"
        f"TOM DO CRIADOR: {tone or 'natural, direto'}\n"
        f"LINGUAGEM DA AUDIÊNCIA: {lang or '-'}{signals}"
    )
    try:
        client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
        resp = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1024,
            temperature=0.7,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Reescreva mantendo o sentido:\n\n{cleaned}"},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return clean(out) or cleaned
    except Exception:
        return cleaned
