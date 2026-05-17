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
- Se houver "PADRÕES VENCEDORES" no contexto, INCORPORE deliberadamente — replique emoção/formato/funil que já funcionaram. Cite a inspiração no objective_reasoning ("seguindo o padrão de 'X' que teve N shares")
- Se houver "FUNCIONOU PIOR", EVITE replicar exatamente aquele formato+emoção
- JSON válido, sem comentários, sem texto antes ou depois
- image_prompt em inglês descritivo sem palavra "text"
"""


class AutoCreatorAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, brand_brain_text: str, site_context: str, topic: str, format: str, platform: str,
                      requested_objective: str = "", image_refs: list = None) -> str:
        objective_hint = f"O usuário sugeriu objetivo='{requested_objective}', mas você pode SOBRESCREVER se achar melhor — basta justificar." if requested_objective else "Decida o objetivo automaticamente baseado no contexto e justifique."
        refs_block = ""
        if image_refs:
            refs_block = "\n=== REFERÊNCIAS VISUAIS ANEXADAS (replicar a estética + adaptar pro nicho) ===\n"
            for i, ref in enumerate(image_refs[:5], 1):
                analysis = ref.get("analysis") or {}
                visual = ref.get("visual_analysis") or {}
                refs_block += f"\nRef #{i} — {ref.get('label') or 'sem label'}\n"
                if visual.get("composition"):
                    refs_block += f"  Composição: {visual.get('composition')}\n"
                if visual.get("mood"):
                    refs_block += f"  Mood: {visual.get('mood')}\n"
                if visual.get("palette"):
                    refs_block += f"  Paleta: {', '.join(visual.get('palette') or [])}\n"
                if visual.get("identity"):
                    refs_block += f"  Identidade visual: {visual.get('identity')}\n"
                if analysis.get("hook"):
                    refs_block += f"  Hook: {analysis.get('hook')}\n"
                if analysis.get("structure"):
                    refs_block += f"  Estrutura: {analysis.get('structure')}\n"
                if ref.get("adapted_brief"):
                    refs_block += f"  Como adaptar pra essa marca: {ref.get('adapted_brief')}\n"
            refs_block += "\nUSE essas referências como direção criativa — não copie literalmente; traduza pro nicho/persona.\n"

        return f"""=== CONTEXTO ESTRATÉGICO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== SITE / PRODUTO REFERENCIADO ===
{site_context or '(nenhum)'}
{refs_block}
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
