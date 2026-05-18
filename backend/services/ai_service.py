from __future__ import annotations
from typing import Optional
from models.trade_signal import TradeSignal, SignalDirection, TradeType
from config import GROQ_API_KEY, ANTHROPIC_API_KEY

# ─── Groq client (principal — gratuito) ───────────────────────────────────────
try:
    from groq import AsyncGroq
    _groq_client: Optional[AsyncGroq] = None

    def get_groq_client() -> AsyncGroq:
        global _groq_client
        if _groq_client is None:
            _groq_client = AsyncGroq(api_key=GROQ_API_KEY)
        return _groq_client
except ImportError:
    AsyncGroq = None  # type: ignore
    def get_groq_client():  # type: ignore
        raise RuntimeError("groq package not installed")

# ─── Anthropic client (fallback legado) ───────────────────────────────────────
try:
    import anthropic
    _anthropic_client: Optional[anthropic.AsyncAnthropic] = None

    def get_client() -> anthropic.AsyncAnthropic:
        global _anthropic_client
        if _anthropic_client is None:
            _anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        return _anthropic_client
except ImportError:
    def get_client():  # type: ignore
        raise RuntimeError("anthropic package not installed")


GROQ_MODEL = "llama-3.3-70b-versatile"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def has_ai() -> bool:
    return bool(GROQ_API_KEY) or bool(ANTHROPIC_API_KEY)


async def call_ai(system: str, user: str, max_tokens: int = 1000) -> str:
    """Chama Groq (preferencial) ou Anthropic (fallback)."""
    if GROQ_API_KEY:
        client = get_groq_client()
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    if ANTHROPIC_API_KEY:
        client = get_client()
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return message.content[0].text

    raise RuntimeError("Nenhuma chave de IA configurada (GROQ_API_KEY ou ANTHROPIC_API_KEY).")


# ─── Formatação de sinal ───────────────────────────────────────────────────────

def _format_signal_for_prompt(signal: TradeSignal, macro_context: str = "") -> str:
    direction_map = {
        SignalDirection.LONG: "COMPRA (Long)",
        SignalDirection.SHORT: "VENDA (Short)",
        SignalDirection.NEUTRAL: "NEUTRO",
    }
    trade_type_map = {
        TradeType.SCALP: "Scalp (minutos a 1h)",
        TradeType.DAY_TRADE: "Day Trade (1h–8h)",
        TradeType.SWING: "Swing Trade (dias a semanas)",
        TradeType.HODL: "HODL (longo prazo)",
    }
    ind = signal.indicators
    patterns_text = "\n".join(
        f"  - {p.type.value} (confiança {p.confidence:.0%}): {p.description}"
        for p in signal.patterns[:5]
    )
    is_actionable = signal.confidence >= 0.75
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100 if signal.entry else 0

    return f"""
{macro_context}

=== ATIVO ANALISADO ===
Par: {signal.symbol}
Timeframe: {signal.timeframe}
Tipo de Operação: {trade_type_map.get(signal.trade_type, '')}
Direção: {direction_map.get(signal.direction, '')}
Probabilidade: {signal.confidence:.0%} | Força: {signal.signal_strength}
Acionável (≥75%): {"SIM" if is_actionable else "NÃO — aguardar confluência"}

=== INDICADORES TÉCNICOS ===
  RSI(14): {ind.rsi}
  MACD: {ind.macd} | Sinal: {ind.macd_signal} | Histograma: {ind.macd_hist}
  EMA12: {ind.ema9} | EMA26: {ind.ema21} | EMA50: {ind.ema50} | EMA200: {ind.ema200}
  BB Superior: {ind.bb_upper} | Médio: {ind.bb_middle} | Inferior: {ind.bb_lower}
  ATR(14): {ind.atr}
  ADX(14): {ind.adx}
  Stoch K: {ind.stoch_k} | D: {ind.stoch_d}
  Supertrend: {ind.supertrend} ({"Alta" if ind.supertrend_direction == 1 else "Baixa"})
  Volume médio: {ind.volume_avg}
  Pivot High: {ind.pivot_high} | Pivot Low: {ind.pivot_low}

=== PADRÕES GRÁFICOS (TF {signal.timeframe}) ===
{patterns_text if patterns_text else '  Nenhum padrão significativo detectado'}

=== NÍVEIS DE OPERAÇÃO ===
  Entrada: {signal.entry}
  Stop Loss: {signal.stop_loss} (risco: {risk_pct:.2f}%)
  TP1: {signal.tp1}
  TP2: {signal.tp2}
  TP3: {signal.tp3}
  Risco/Retorno: 1:{signal.risk_reward}
"""


SYSTEM_PROMPT = """Você é um analista técnico sênior de criptomoedas. Escreva em português claro e simples, como se explicasse para alguém que está aprendendo a operar. Sem jargão excessivo — se usar um termo técnico, explique em uma frase o que ele significa.

SIGA ESTA ORDEM OBRIGATÓRIA NA ANÁLISE:

1. DIREÇÃO DO ATIVO
   - Diga claramente: o ativo está em tendência de alta, baixa ou lateral?
   - Qual a probabilidade calculada e o que ela representa?

2. BTC — ESTRUTURA
   - O BTC está apoiando ou pressionando o mercado agora?
   - RSI, ADX e Supertrend do BTC.

3. DOMINÂNCIA DO BTC
   - O que o número indica para altcoins?

4. DXY (DÓLAR)
   - Dólar fortalecendo = pressão sobre cripto. Enfraquecendo = favorável.

5. S&P 500 E NASDAQ
   - Bolsas em alta = apetite por risco = favorável a cripto.
   - Bolsas em queda = aversão a risco = cuidado.

6. PAR BTC — FORÇA RELATIVA (TF 1D)
   - O ativo está mais forte ou mais fraco que o BTC no diário?
   - Há algum padrão gráfico detectado? Descreva em linguagem simples.

7. OPERAÇÃO RECOMENDADA
   - Tipo: Scalp / Day Trade / Swing — explicar qual é mais adequado para este setup
   - TF utilizado na decisão: mencionar explicitamente (ex: "Analisando no TF 4H")
   - Por que este TF? Justifique em uma frase simples
   - SE probabilidade ≥ 75%: mostrar entrada, stop, TP1/TP2/TP3 com JUSTIFICATIVA DETALHADA para cada nível:
       * Por que aquele stop? (estrutura, suporte/resistência, liquidez)
       * Por que aqueles alvos? (resistência, projeção, probabilidade de cada um)
       * Se < 75%: "AGUARDAR — [motivo exato, o que falta confirmar]"

8. ALERTAS SIMULADOS
   🎯 Alvo 1 | 🎯 Alvo 2 | 🎯 Alvo 3 | 🛑 Stop | ⚠️ Invalidação | 🚪 Saída a mercado

REGRAS:
- Linguagem simples e direta, como explicar para um iniciante
- Máximo 8 parágrafos
- Nunca coloque alvos negativos ou menores que a entrada em long / maiores em short
- Justifique SEMPRE o stop e os alvos com estrutura de mercado, suporte/resistência e liquidez"""


def _format_confluence_for_prompt(signal: TradeSignal) -> str:
    """Formata o score de confluência para o prompt da IA — dá contexto rico para análise."""
    if not signal.confluence:
        return ""
    c = signal.confluence

    pro = [f for f in c.factors if f.aligned and f.points > 0]
    contra = [f for f in c.factors if not f.aligned or f.points < 0]

    out = [
        f"\n=== SCORE DE CONFLUÊNCIA: {c.total:.0f}/{c.max_total:.0f} pontos ({c.pct:.0f}%) ===",
        f"\nFATORES A FAVOR ({len(pro)}):"
    ]
    for f in pro:
        out.append(f"  ✅ [{f.category}] +{f.points:.0f}pts — {f.name}: {f.description}")

    if contra:
        out.append(f"\nFATORES CONTRA / ALERTAS ({len(contra)}):")
        for f in contra:
            out.append(f"  ⚠️ [{f.category}] {f.points:.0f}pts — {f.name}: {f.description}")

    if c.warnings:
        out.append("\nWARNINGS DETECTADOS:")
        for w in c.warnings:
            out.append(f"  🚨 {w}")
    return "\n".join(out)


CRITIQUE_PROMPT = """Você é um GESTOR DE RISCO SÊNIOR muito cético. Acabou de ler esta análise de um analista júnior.

Seu trabalho é encontrar BURACOS na análise. Para cada um dos pontos abaixo, responda em 1-2 frases CURTAS:

1. **PRINCIPAL RISCO IGNORADO** — qual cenário negativo o analista não considerou?
2. **QUALIDADE DOS STOPS/ALVOS** — eles estão em estrutura real ou foram colocados arbitrariamente? O stop pode ser caçado?
3. **CONTEXTO MACRO** — algo no BTC, DXY, S&P, dominância foi subestimado?
4. **TIMING** — entrar agora é correto ou seria melhor aguardar pullback/confirmação?
5. **VEREDITO FINAL** — após sua crítica, qual sua recomendação real?
   - ✅ APROVADO: análise sólida, pode operar
   - ⏳ CONDICIONAL: operar somente se [condição específica]
   - ❌ REPROVADO: NÃO operar — [motivo claro]

Seja DIRETO. Sem floreio. Cada item máximo 2 frases. Total: máximo 200 palavras."""


async def generate_ai_analysis(
    signal: TradeSignal,
    macro_context: str = "",
    with_critique: bool = True,
) -> tuple[str, str | None]:
    """
    Gera análise + crítica (self-critique loop).
    Retorna (analise_principal, critica_ou_none).
    """
    if not has_ai():
        return _fallback_analysis(signal), None

    confluence_block = _format_confluence_for_prompt(signal)
    prompt_data = _format_signal_for_prompt(signal, macro_context) + confluence_block

    try:
        analysis = await call_ai(
            system=SYSTEM_PROMPT,
            user=(
                f"Analise este setup seguindo a ordem obrigatória. "
                f"Use o SCORE DE CONFLUÊNCIA fornecido para justificar a confiança no setup — "
                f"cite os fatores específicos que sustentam a recomendação.\n{prompt_data}"
            ),
            max_tokens=1000,
        )
    except Exception:
        return _fallback_analysis(signal), None

    if not with_critique:
        return analysis, None

    # Self-critique — segunda chamada
    try:
        critique = await call_ai(
            system=CRITIQUE_PROMPT,
            user=(
                f"=== DADOS DO SETUP ===\n{prompt_data}\n\n"
                f"=== ANÁLISE GERADA PELO JÚNIOR ===\n{analysis}\n\n"
                f"Sua crítica:"
            ),
            max_tokens=500,
        )
        return analysis, critique
    except Exception:
        return analysis, None


def _fallback_analysis(signal: TradeSignal) -> str:
    ind = signal.indicators
    is_actionable = signal.confidence >= 0.75
    direction_text = "alta" if signal.direction == SignalDirection.LONG else "baixa" if signal.direction == SignalDirection.SHORT else "lateral"
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100 if signal.entry else 0

    parts = []
    parts.append(
        f"1. DIREÇÃO: {signal.symbol} apresenta viés de {direction_text} no timeframe {signal.timeframe} "
        f"com {signal.confidence:.0%} de probabilidade. "
        f"{'Sinal acionável.' if is_actionable else 'Probabilidade abaixo do mínimo de 75% — aguardar.'}"
    )
    parts.append(
        "2. BTC: Contexto macro completo disponível apenas com análise IA ativada. "
        "Recomenda-se verificar manualmente o viés do BTC no TF diário antes de operar."
    )
    parts.append(
        "3–5. MACRO (DXY/SP500/Nasdaq): Ative a análise IA para contexto macro completo com dados em tempo real."
    )
    ind_text = f"EMA12={ind.ema9} | EMA26={ind.ema21} | RSI={ind.rsi} | ADX={ind.adx} | Supertrend={'Alta' if ind.supertrend_direction == 1 else 'Baixa' if ind.supertrend_direction == -1 else 'N/D'}"
    pat_text = "; ".join(p.description for p in signal.patterns[:2]) if signal.patterns else "Nenhum padrão detectado"
    parts.append(f"6. PAR BTC: {ind_text}. Padrões ({signal.timeframe}): {pat_text}.")
    type_map = {TradeType.SCALP: "Scalp", TradeType.DAY_TRADE: "Day Trade",
                TradeType.SWING: "Swing Trade", TradeType.HODL: "HODL"}
    if is_actionable:
        parts.append(
            f"7. OPERAÇÃO ({type_map.get(signal.trade_type, '')} — TF {signal.timeframe}): "
            f"{signal.direction.value.upper()} em {signal.entry:.6g}. "
            f"Stop: {signal.stop_loss:.6g} (risco {risk_pct:.1f}%). "
            f"TP1: {signal.tp1:.6g} | TP2: {signal.tp2:.6g} | TP3: {signal.tp3:.6g}. RR 1:{signal.risk_reward}."
        )
    else:
        parts.append(
            f"7. AGUARDAR: Probabilidade {signal.confidence:.0%} < 75%. Reanalisar após próximo fechamento de candle."
        )
    if is_actionable:
        parts.append(
            f"8. ALERTAS: 🎯 TP1 {signal.tp1:.6g} | 🎯 TP2 {signal.tp2:.6g} | "
            f"🎯 TP3 {signal.tp3:.6g} | 🛑 Stop {signal.stop_loss:.6g} | "
            f"⚠️ Invalidação: fechamento além do stop."
        )
    return "\n\n".join(parts)
