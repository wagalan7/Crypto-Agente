"""RepurposeAgent — adapts an existing approved post into another format/platform.

Why: a winning reel is also a great carousel and a great LinkedIn post — but
each medium needs its own pacing, length and structure. Instead of asking the
user to re-prompt from scratch, this agent takes the source piece and re-shapes
it for the target medium, preserving the strategic core (objective, funnel
stage, emotion, linked product) while rebuilding hook/script/copy/design.

Output: same JSON shape as AutoCreator so the router can store a new
ContentPiece with the adapted fields.
"""
import json
import re
from .base_agent import BaseAgent


SYSTEM = """Você é um especialista em reaproveitamento de conteúdo digital.
Sua função: pegar um post APROVADO e re-formatá-lo para um novo formato/plataforma,
sem perder o ponto estratégico, mas respeitando a linguagem do destino.

Responda SEMPRE em PT-BR, JSON puro sem markdown:
{
  "title": "novo título adaptado ao destino",
  "hook": "novo hook calibrado pro formato/plataforma alvo",
  "script": "novo roteiro/estrutura adaptada (5-10 linhas separadas por \\n)",
  "copy": "nova legenda — adaptada ao tom da plataforma alvo, com CTA",
  "design_brief": "novo briefing visual pro formato alvo",
  "adaptation_notes": "1-2 frases explicando como foi adaptado e o que mudou estrategicamente"
}

Regras de adaptação:
- Reels/Shorts → Carrossel: transforme o ritmo em 6-10 slides com 1 ideia por slide; hook vira slide 1; payoff vira slide final com CTA
- Reels/Shorts → LinkedIn: aumente a profundidade, troque tom emocional por análise + dados; primeiro parágrafo carrega o hook, último é CTA explícito
- Carrossel → Reel: condense em 1 ideia central + 3 beats visuais; ritmo rápido, hook visual nos primeiros 2s
- Instagram → TikTok: tom mais cru, menos polido, jump cuts, menos firulas
- Instagram → YouTube long: expanda em capítulos, abertura sustentada, retenção em 3min
- Mantenha SEMPRE: objetivo, estágio de funil, emoção dominante, produto linkado, ângulo psicológico
- NÃO copie literalmente — re-escreva tudo na linguagem do destino
- JSON estritamente válido, sem comentários"""


def parse_json_response(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
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


class RepurposeAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, *, brand_brain_text: str, source_title: str,
                     source_format: str, source_platform: str, source_hook: str,
                     source_script: str, source_copy: str, source_design_brief: str,
                     source_emotion: str, source_funnel: str, source_objective: str,
                     target_format: str, target_platform: str, instruction: str = "") -> str:
        instr_block = f"\nINSTRUÇÃO EXTRA: {instruction}" if instruction else ""
        return f"""=== CONTEXTO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== POST DE ORIGEM ===
Título: {source_title}
Formato atual: {source_format} · Plataforma atual: {source_platform}
Objetivo: {source_objective} · Funil: {source_funnel} · Emoção: {source_emotion}

HOOK:
{source_hook or '(vazio)'}

ROTEIRO:
{source_script or '(vazio)'}

COPY:
{source_copy or '(vazio)'}

BRIEFING VISUAL:
{source_design_brief or '(vazio)'}

=== ALVO DA ADAPTAÇÃO ===
Novo formato: {target_format}
Nova plataforma: {target_platform}{instr_block}

=== TAREFA ===
Re-formate o post acima respeitando as regras de adaptação. Devolva o JSON pedido."""
