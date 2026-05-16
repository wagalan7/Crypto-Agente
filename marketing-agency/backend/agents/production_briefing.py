"""ProductionBriefingAgent — turns an approved content piece into a concrete
shooting/recording checklist so the creator stops re-thinking each post.

Input: title, format, platform, hook, script, design_brief.
Output: JSON with location, wardrobe, props, shots, audio, captions, lighting,
total_duration_estimate, equipment, tips.
"""
import json
import re
from .base_agent import BaseAgent

SYSTEM = """Você é um Diretor de Produção de conteúdo digital.
Sua função: ler um conteúdo aprovado (roteiro/copy/briefing visual) e produzir
um CHECKLIST CONCRETO de gravação, pronto pra executar hoje.

Responda SEMPRE em PT-BR, JSON puro sem markdown:
{
  "location": "local sugerido (ex: cozinha clara, parque arborizado, mesa de trabalho)",
  "wardrobe": "figurino: 1-2 opções calibradas com o tom",
  "props": ["item 1", "item 2", "item 3"],
  "shots": [
    {"order": 1, "type": "close/wide/over-shoulder/B-roll", "description": "o que filmar e por quê"}
  ],
  "audio": "áudio: silêncio limpo, música ambiente sugerida (estilo), ou tendência viral aplicável",
  "lighting": "iluminação prática (natural lateral, anel, 3 pontos básico)",
  "captions_overlay": ["legendas/textos sobrepostos curtos que devem aparecer no vídeo"],
  "duration_estimate_seconds": 30,
  "equipment_minimum": ["celular", "tripé", "microfone lapela (recomendado)"],
  "production_tips": ["dica 1 curta e prática", "dica 2"],
  "edit_notes": "diretriz de edição: cortes rápidos, jump cuts, legendas grudadas, transição etc."
}

Regras:
- Shots devem ser ACIONÁVEIS — descreva ângulo + ação + duração aproximada
- Adapte ao formato: Reels/Shorts = 4-7 shots curtos com hook visual nos primeiros 2s; Carousel = não tem shots, retorna apenas 1 entry com type='static' e a sequência de slides
- Considere a plataforma (Instagram vertical 9:16, YouTube horizontal 16:9)
- Equipment_minimum: liste APENAS o essencial, sem firulas
- Nada de jargão de cinema obscuro — linguagem de criador
- JSON estritamente válido, sem comentários
"""


class ProductionBriefingAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    async def run(self, user_message: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content

    def build_prompt(self, *, title: str, format: str, platform: str, hook: str, script: str, design_brief: str, copy: str = "", emotion: str = "", tone: str = "") -> str:
        return f"""=== CONTEÚDO APROVADO ===
Título: {title}
Formato: {format} · Plataforma: {platform}
Tom da marca: {tone or '-'}
Emoção dominante: {emotion or '-'}

HOOK:
{hook or '-'}

ROTEIRO:
{script or '-'}

COPY (legenda):
{copy or '-'}

BRIEFING VISUAL:
{design_brief or '-'}

Gere o JSON completo do briefing de produção."""


def parse_json_response(raw: str) -> dict:
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        try:
            return json.loads(raw[start:end + 1].replace("\n", "\\n").replace("\r", ""))
        except Exception:
            return {}
