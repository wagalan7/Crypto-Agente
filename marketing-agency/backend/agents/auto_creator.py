import json
import re
from .base_agent import BaseAgent

SYSTEM = """Você é o Diretor de Conteúdo de uma agência de marketing digital.
Sua função: ler o briefing do cliente + conteúdo do site/produto + tema, e gerar UM conteúdo COMPLETO pronto pra publicar.

Responda SEMPRE em PT-BR.

FORMATO OBRIGATÓRIO — retorne APENAS um JSON válido (sem markdown, sem ```json), com estes campos:
{
  "title": "título curto e claro (máx 80 chars)",
  "hook": "primeira frase que prende (1 linha)",
  "script": "roteiro completo de 5-10 linhas, separado por \\n",
  "copy": "legenda pronta pra publicação com CTA e até 5 hashtags relevantes",
  "design_brief": "descrição visual do post em 3-4 linhas (cores, elementos, mood)",
  "image_prompt": "prompt em INGLÊS para gerador de imagem, fotorrealista ou design moderno, sem texto na imagem, máx 200 chars"
}

Regras:
- Linguagem direta, sem clichês de marketing
- Adaptado ao tom de voz e nicho do cliente
- Hook deve gerar curiosidade ou parar o scroll
- image_prompt em inglês, descritivo, sem palavras como "text" ou "logo"
- JSON válido, sem comentários, sem texto antes ou depois"""


class AutoCreatorAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, client_context: str, site_context: str, topic: str, format: str, platform: str, objective: str) -> str:
        return f"""BRIEFING DO CLIENTE:
{client_context or '(sem briefing)'}

CONTEÚDO DO SITE / PRODUTO:
{site_context or '(sem site informado)'}

TEMA: {topic or 'livre, escolha o melhor com base no briefing'}
FORMATO: {format}
PLATAFORMA: {platform}
OBJETIVO: {objective}

Gere o JSON com o conteúdo completo."""


def parse_json_response(raw: str) -> dict:
    """Robust JSON extraction — handles markdown fences and stray text."""
    if not raw:
        return {}
    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
    # Find first { and last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return {}
    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Try to fix common issues: unescaped newlines inside strings
        try:
            return json.loads(candidate.replace("\n", "\\n").replace("\r", ""))
        except Exception:
            return {}
