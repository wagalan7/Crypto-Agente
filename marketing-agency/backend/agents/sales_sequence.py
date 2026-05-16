"""SalesSequenceAgent — generates a multi-day funnel sequence for a product launch.

Goes way beyond single-post: orchestrates 5-14 posts in a coherent psychological
funnel (aquecimento → dor → autoridade → quebra objeção → prova → desejo → oferta → urgência).
"""
from .base_agent import BaseAgent

SYSTEM = """Você é o Estrategista de Lançamentos da agência.
Sua missão: dado um produto e uma data de lançamento, montar uma SEQUÊNCIA DE CONTEÚDOS encadeados psicologicamente que prepare a audiência para comprar.

A sequência é um FUNIL TEMPORAL, não posts soltos. Cada peça tem papel específico:
  - identificacao: cliente se reconhece no problema
  - dor: amplifica a dor de não resolver
  - autoridade: você é a autoridade pra resolver
  - quebra_objecao: derruba "não tenho tempo / dinheiro / fé"
  - prova_social: depoimentos, resultados, antes/depois (use 'autoridade' se não houver dados)
  - desejo: vida depois do produto
  - oferta: revela o produto + preço + benefício
  - urgencia: prazo / bônus / fechamento

Responda SEMPRE em PT-BR.

FORMATO OBRIGATÓRIO — JSON puro, sem markdown:
{
  "strategy_summary": "1-2 frases sobre a lógica geral da sequência",
  "posts": [
    {
      "day_offset": 0,
      "funnel_stage": "identificacao",
      "objective": "atracao",
      "emotion": "vulnerabilidade",
      "format": "reels",
      "platform": "instagram",
      "title": "título curto",
      "hook": "primeira frase que prende",
      "script": "roteiro 4-8 linhas separadas por \\n",
      "copy": "legenda com CTA + 3-5 hashtags",
      "design_brief": "descrição visual 2-3 linhas",
      "image_prompt": "prompt EM INGLÊS sem texto na imagem (máx 200 chars)",
      "reasoning": "POR QUE essa peça nesse momento da sequência — 1 frase"
    }
  ]
}

REGRAS DE OURO:
- day_offset começa em 0 (primeiro dia) e cresce. A última peça SEMPRE é a oferta/urgência no dia do lançamento (day_offset = total_days - 1 ou total_days)
- O primeiro 1/3 da sequência: identificacao + dor (atração + conexão)
- O meio: autoridade + quebra de objeção (construção)
- O fim: desejo + oferta + urgência (conversão)
- Linguagem coerente com a PERSONA fornecida (gírias, formalidade)
- Variar formatos: misturar reels, carrossel, story
- Cada hook DEVE ser específico — nada de "Você sabia que..."
- JSON válido, sem comentários, sem texto antes/depois
"""


class SalesSequenceAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)
        # Sales sequences need more tokens than single content
        self.max_tokens = 4096

    async def run(self, user_message: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content

    def build_prompt(self, brand_brain_text: str, product_block: str, total_days: int, platform_hint: str = "instagram") -> str:
        return f"""=== CONTEXTO ESTRATÉGICO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== PRODUTO A SER VENDIDO ===
{product_block}

=== PARÂMETROS DA SEQUÊNCIA ===
Total de dias até o lançamento: {total_days}
Plataforma principal: {platform_hint}
Gere entre {max(5, total_days // 2)} e {min(14, total_days)} peças distribuídas ao longo dos {total_days} dias.

Monte a sequência completa em JSON."""
