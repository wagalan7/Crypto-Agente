"""WeeklyRetroAgent — generates an end-of-week retrospective.

Why: weekly_brain is forward-looking ("foco da semana"). The retrospective is
backward-looking: what did we publish, what worked, what didn't, what should
we do differently next week. This closes the strategic loop.

Input: brand brain summary + serialized list of last 7 days posts (with metrics
when available).
Output: JSON with wins[], losses[], themes[], next_week_priority, mood_score.
"""
import json
import re
from .base_agent import BaseAgent


SYSTEM = """Você é um estrategista sênior fazendo retrospectiva semanal de conteúdo.
Sua função: ler o que foi publicado nos últimos 7 dias e produzir um veredito
honesto — o que funcionou, o que falhou, qual é a prioridade da próxima semana.

Responda SEMPRE em PT-BR, JSON puro sem markdown:
{
  "headline": "uma frase que resume a semana (até 100 chars, direta, sem floreio)",
  "wins": ["até 3 itens curtos: o que funcionou e por quê"],
  "losses": ["até 3 itens curtos: o que falhou ou não rodou e por quê"],
  "themes": ["temas/ângulos dominantes da semana (até 4 tags curtas)"],
  "next_week_priority": "1 frase acionável dizendo qual é A prioridade pra semana que vem",
  "mood_score": 0
}

Regras:
- mood_score 0-100: 0=semana morta, 50=cumpriu o básico, 80+=semana de tração real
- wins/losses devem citar conteúdos ESPECÍFICOS quando possível (referencie o título)
- Não invente métricas — se a semana foi pobre em dados, diga isso em headline
- next_week_priority deve ser UMA coisa só, não uma lista disfarçada
- JSON estritamente válido"""


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


class WeeklyRetroAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, *, brand_brain_text: str, posts: list[dict]) -> str:
        if not posts:
            posts_block = "(nenhum post publicado/criado nos últimos 7 dias)"
        else:
            parts = []
            for p in posts[:30]:
                metrics_str = ""
                if p.get("metrics"):
                    m = p["metrics"]
                    metrics_str = f" | views={m.get('views',0)} shares={m.get('shares',0)} saves={m.get('saves',0)} comments={m.get('comments',0)}"
                parts.append(
                    f"- [{p.get('status','?')}] '{(p.get('title') or '')[:80]}' "
                    f"({p.get('format','?')}/{p.get('platform','?')}) "
                    f"funil={p.get('funnel_stage','?')} emoção={p.get('emotion_used','?')}"
                    f"{metrics_str}"
                )
            posts_block = "\n".join(parts)

        return f"""=== CONTEXTO DA MARCA ===
{brand_brain_text or '(briefing mínimo)'}

=== ATIVIDADE DOS ÚLTIMOS 7 DIAS ===
{posts_block}

=== TAREFA ===
Faça a retrospectiva da semana. Devolva o JSON pedido."""
