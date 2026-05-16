"""InspirationAlignmentAgent — compares a generated content piece against the
client's saved inspirations and tells the creator how close it lands.

Why: inspirations exist to teach the IA what "good" looks like for this brand.
After generation, the user wants to know — does this post actually capture the
references I curated? Where does it diverge? What should I tweak?

Input: brand brain summary, the content piece (hook/script/copy/design_brief),
and a list of analyzed inspirations (label + analysis JSON + adapted_brief).
Output: JSON with best-matched inspiration, alignment score 0-100, strengths,
divergences, and a concrete adjustment suggestion.
"""
import json
import re
from .base_agent import BaseAgent


SYSTEM = """Você é um analista crítico de conteúdo digital. Sua função: comparar
um post recém-gerado contra as inspirações curadas da marca e dizer, sem
suavizar, o quanto ele captura ou se distancia das referências.

Responda SEMPRE em PT-BR, JSON puro sem markdown:
{
  "best_match": "label da inspiração mais próxima (ou null se nenhuma encaixa)",
  "alignment_score": 0,
  "strengths": ["o que o post acertou em relação às inspirações (até 4 itens curtos)"],
  "divergences": ["o que o post deixou na mesa ou divergiu (até 4 itens curtos e específicos)"],
  "adjustment_suggestion": "uma sugestão CONCRETA de ajuste — 1-3 frases acionáveis, dizendo o que mexer (no hook, no ritmo, na narrativa ou no CTA) pra aproximar das referências sem copiar"
}

Regras:
- alignment_score 0-100: 0=ignorou completamente as referências, 50=captura tom mas perde estrutura, 80+=adapta com fidelidade sem plagiar
- Compare hook contra hook, ritmo contra ritmo, narrativa contra narrativa
- Não invente — se as inspirações são vagas, diga isso em divergences
- NUNCA sugira copiar literalmente — sugestão deve respeitar a voz da marca
- JSON estritamente válido"""


def parse_json_response(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    # strip code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


class InspirationAlignmentAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, *, brand_brain_text: str, title: str, format: str,
                     platform: str, hook: str, script: str, copy: str,
                     design_brief: str, inspirations: list[dict]) -> str:
        if not inspirations:
            insp_block = "(nenhuma inspiração cadastrada — retorne best_match=null e alignment_score=0)"
        else:
            parts = []
            for i, ins in enumerate(inspirations[:8], 1):
                label = ins.get("label") or f"Inspiração #{i}"
                analysis = ins.get("analysis") or {}
                adapted = ins.get("adapted_brief") or ""
                parts.append(
                    f"--- {label} ---\n"
                    f"Análise: {json.dumps(analysis, ensure_ascii=False)}\n"
                    f"Adaptação pra marca: {adapted or '-'}"
                )
            insp_block = "\n\n".join(parts)

        return f"""=== CONTEXTO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== INSPIRAÇÕES CURADAS ===
{insp_block}

=== POST GERADO ===
Título: {title}
Formato: {format} · Plataforma: {platform}

HOOK:
{hook or '(vazio)'}

ROTEIRO:
{script or '(vazio)'}

COPY:
{copy or '(vazio)'}

BRIEFING VISUAL:
{design_brief or '(vazio)'}

=== TAREFA ===
Compare o post acima contra as inspirações. Retorne o JSON do formato definido."""
