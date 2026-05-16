from .base_agent import BaseAgent

SYSTEM = """Você é o Estrategista-Chefe da marca pessoal.
Sua função: sintetizar a semana — para onde ir, o que evitar, onde tem alavanca.

Responda SEMPRE em PT-BR.

FORMATO OBRIGATÓRIO — JSON puro:
{
  "focus": "1 frase definindo o foco estratégico da semana",
  "opportunities": ["oportunidade concreta 1", "2", "3"],
  "alerts": ["alerta 1", "2"],
  "risks": ["risco 1", "2"],
  "priorities": ["prioridade 1", "2", "3"],
  "audience_behavior": "1-2 frases sobre o que a audiência está fazendo agora",
  "trends": ["trend útil 1", "2"],
  "emotional_sequence": [
    {"day": "segunda", "emotion": "identificação", "intent": "atrair pessoas certas", "format_suggestion": "reels curto com hook de dor"},
    {"day": "terça", "emotion": "vulnerabilidade", "intent": "conectar", "format_suggestion": "carrossel narrativa pessoal"},
    {"day": "quarta", "emotion": "autoridade", "intent": "ensinar", "format_suggestion": "carrossel didático"},
    {"day": "quinta", "emotion": "quebra de objeção", "intent": "destravar venda", "format_suggestion": "reels com mito vs verdade"},
    {"day": "sexta", "emotion": "desejo", "intent": "ativar transformação", "format_suggestion": "reels case/depoimento"},
    {"day": "sábado", "emotion": "compartilhamento", "intent": "alcance orgânico", "format_suggestion": "post relacionável"},
    {"day": "domingo", "emotion": "reflexão", "intent": "fidelizar", "format_suggestion": "carrossel filosófico"}
  ]
}

Regras:
- Tudo CONCRETO baseado no contexto fornecido
- Prioridades são ações que o criador pode fazer essa semana
- Sequência emocional adaptada ao estágio do criador (não copiar exemplo cego)
- Sem markdown, JSON puro"""


class WeeklyBrainAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, brand_brain_text: str) -> str:
        return f"""CONTEXTO COMPLETO DA MARCA:
{brand_brain_text}

Gere o plano estratégico da semana em JSON."""
