import json
import re
from .base_agent import BaseAgent

SYSTEM = """Você é o Diretor Estratégico de Conteúdo da agência.
Sua função: ler todo o contexto da marca (briefing, persona, produto, conhecimento, insights, semana) e produzir UM conteúdo COMPLETO que seja estrategicamente justificado — não um post genérico.

Responda SEMPRE em PT-BR.

FORMATO OBRIGATÓRIO — JSON puro, sem markdown, com TODOS estes campos:
{
  "title": "título curto (máx 80 chars)",
  "hook": "primeira frase que prende — calibrada pra persona",
  "script": "roteiro 5-10 linhas separadas por \\n",
  "copy": "legenda pronta com CTA e até 5 hashtags relevantes",
  "design_brief": "descrição visual em 3-4 linhas — paleta, elementos, mood",
  "image_prompt": "prompt EM INGLÊS pro gerador de imagem (sem texto na imagem, máx 200 chars)",
  "objective": "atracao | conexao | autoridade | conversao | compartilhamento",
  "objective_reasoning": "POR QUE esse objetivo agora — 1-2 frases ligadas ao contexto",
  "emotion_used": "emoção dominante explorada (ex: vulnerabilidade, alívio, esperança, validação)",
  "funnel_stage": "identificacao | dor | autoridade | quebra_objecao | desejo | conversao",
  "format_reasoning": "POR QUE esse formato/plataforma — 1 frase",
  "why_for_audience": "qual dor/desejo da persona esse conteúdo toca — 1 frase",
  "linked_product_hint": "se for útil pra vender o produto principal, qual conexão. Caso contrário, 'nenhuma'."
}

Regras DE OURO:
- O conteúdo deve refletir a PERSONA específica (não audiência genérica)
- A emoção escolhida deve estar nas emoções da persona ou justificar mudança
- Se houver produto principal e for fase de aquecer venda, faça funnel_stage=quebra_objecao/desejo/conversao
- objective_reasoning e format_reasoning são OBRIGATÓRIOS — sem isso, output é genérico
- Linguagem deve copiar os padrões da persona (gírias, formalidade)
- JSON válido, sem comentários, sem texto antes ou depois
- image_prompt em inglês descritivo sem palavra "text"
"""


class AutoCreatorAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, brand_brain_text: str, site_context: str, topic: str, format: str, platform: str, requested_objective: str = "") -> str:
        objective_hint = f"O usuário sugeriu objetivo='{requested_objective}', mas você pode SOBRESCREVER se achar melhor — basta justificar." if requested_objective else "Decida o objetivo automaticamente baseado no contexto e justifique."
        return f"""=== CONTEXTO ESTRATÉGICO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== SITE / PRODUTO REFERENCIADO ===
{site_context or '(nenhum)'}

=== PARÂMETROS DESTE CONTEÚDO ===
Tema: {topic or 'livre — escolha o melhor com base no contexto'}
Formato: {format}
Plataforma: {platform}
Objetivo: {objective_hint}

Gere o JSON completo do conteúdo estratégico."""


def parse_json_response(raw: str) -> dict:
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return {}
    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            return json.loads(candidate.replace("\n", "\\n").replace("\r", ""))
        except Exception:
            return {}
