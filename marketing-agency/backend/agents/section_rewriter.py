"""SectionRewriterAgent — regenerates ONE section of an existing piece.

Use case: user has a generated post but the hook is weak / copy too long /
script not vulnerable enough. Instead of regenerating the entire thing, ask
the IA to rewrite just that section with a custom instruction.

Output: plain text (the new section value). No JSON wrapping — caller knows
which field it'll go into.
"""
from .base_agent import BaseAgent


SECTION_RULES = {
    "hook": (
        "Reescreva apenas o HOOK (primeira frase de até 80 chars que prende a atenção). "
        "Não inclua o roteiro completo, apenas a frase. Sem aspas, sem emojis em excesso. "
        "Deve criar curiosidade ou tensão imediata."
    ),
    "script": (
        "Reescreva apenas o ROTEIRO (5-10 linhas separadas por \\n). "
        "Mantenha estrutura: abertura forte, desenvolvimento com tensão, fechamento com payoff/CTA. "
        "Linguagem da persona. Não inclua hook nem legenda — apenas o corpo do vídeo/post."
    ),
    "copy": (
        "Reescreva apenas a COPY (legenda do post). "
        "Até 4 parágrafos curtos, CTA explícito ao final, até 5 hashtags relevantes. "
        "Não inclua o roteiro do vídeo, só o que vai escrito embaixo do post."
    ),
    "design_brief": (
        "Reescreva apenas o BRIEFING VISUAL (3-4 linhas). "
        "Descreva paleta, elementos visuais, mood, e referência estilística. "
        "Foco no que o designer/criador precisa pra produzir a peça."
    ),
}


SYSTEM = """Você é um copywriter sênior. Sua função: reescrever UMA seção
específica de um conteúdo existente, respeitando o contexto da marca e da persona.

Responda em PT-BR, em texto puro (sem JSON, sem markdown, sem comentários).
Devolva APENAS o conteúdo da seção pedida — nada antes, nada depois.
Não inclua rótulos como 'Hook:' ou 'Roteiro:' — só o conteúdo bruto.
"""


class SectionRewriterAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, *, brand_brain_text: str, section: str, current_value: str,
                     title: str, format: str, platform: str, objective: str,
                     emotion: str = "", instruction: str = "") -> str:
        rules = SECTION_RULES.get(section, f"Reescreva a seção '{section}'.")
        instr = f"\nINSTRUÇÃO DO USUÁRIO: {instruction}" if instruction else ""
        return f"""=== CONTEXTO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== CONTEÚDO ATUAL ===
Título: {title}
Formato: {format} · Plataforma: {platform}
Objetivo: {objective}
Emoção dominante: {emotion or '-'}

VALOR ATUAL da seção '{section}':
\"\"\"
{current_value or '(vazio)'}
\"\"\"

=== TAREFA ===
{rules}{instr}

Devolva apenas o novo texto da seção, sem rótulos."""
