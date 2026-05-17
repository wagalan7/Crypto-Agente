"""VoiceScorerAgent — grades how on-brand a generated piece sounds.

Why: AutoCreator produces decent content but drifts toward generic LLM voice
under pressure. A small scorer agent reads the brand tone/personality and the
generated copy, returning 0-100 + the worst offending sentence + a one-line fix.

Cheap call (small model context, small output) — used as a quality gate after
every generation. If score < threshold, caller can regenerate or just flag.
"""
import json
import re
from .base_agent import BaseAgent


SYSTEM = """Você é um editor de marca rigoroso. Sua função: ler o tom/personalidade
declarados da marca e julgar se um conteúdo gerado SOA como essa marca ou se
saiu como qualquer LLM genérico falaria.

Responda SEMPRE em PT-BR, JSON puro sem markdown:
{
  "score": 0,
  "verdict": "1 frase curta dizendo se está dentro do tom ou não",
  "weakest_part": "trecho exato (até 120 chars) que mais destoa do tom — ou null se está tudo ok",
  "fix_hint": "1 frase acionável dizendo o que ajustar pra ficar fiel"
}

Regras de pontuação:
- 0-39: voz genérica, soa LLM, qualquer marca falaria assim
- 40-69: aceitável mas com vícios (clichê, paragrafação muito limpa, falta de personalidade)
- 70-89: dentro do tom, soa autoral
- 90-100: praticamente indistinguível da marca falando
- Seja crítico — score alto deve ser raro

Sinais de drift pra detectar:
- frases-prontas de coaching ("você merece mais", "transforme sua vida")
- estrutura "primeiro/segundo/terceiro" robotizada
- ausência dos traços declarados (se tom='cru e direto', achou floreio? penalize)
- emoji em excesso quando o tom é sóbrio (e vice-versa)
- CTA genérico ("clique no link", "comente abaixo") quando a marca tem CTA próprio

JSON estritamente válido."""


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


class VoiceScorerAgent(BaseAgent):
    def __init__(self):
        super().__init__(SYSTEM)

    def build_prompt(self, *, tone: str, personality: str, positioning: str,
                     hook: str, script: str, copy: str) -> str:
        return f"""=== VOZ DECLARADA DA MARCA ===
Tom: {tone or '-'}
Personalidade: {personality or '-'}
Posicionamento: {positioning or '-'}

=== CONTEÚDO GERADO ===
HOOK:
{hook or '(vazio)'}

ROTEIRO:
{script or '(vazio)'}

COPY:
{copy or '(vazio)'}

=== TAREFA ===
Pontue 0-100 quanto esse conteúdo soa como a marca declarada. Retorne o JSON."""
