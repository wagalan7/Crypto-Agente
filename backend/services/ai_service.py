from __future__ import annotations
from typing import Optional
import anthropic
from models.trade_signal import TradeSignal, SignalDirection, TradeType
from config import ANTHROPIC_API_KEY

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


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
   - Tipo: Scalp / Day Trade / Swing
   - TF utilizado na decisão
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


async def generate_ai_analysis(signal: TradeSignal, macro_context: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return _fallback_analysis(signal)

    client = get_client()
    prompt_data = _format_signal_for_prompt(signal, macro_context)

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analise este setup seguindo a ordem obrigatória:\n{prompt_data}",
                }
            ],
        )
        return message.content[0].text
    except Exception:
        return _fallback_analysis(signal)


def _fallback_analysis(signal: TradeSignal) -> str:
    ind = signal.indicators
    is_actionable = signal.confidence >= 0.75
    direction_text = "alta" if signal.direction == SignalDirection.LONG else "baixa" if signal.direction == SignalDirection.SHORT else "lateral"
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100 if signal.entry else 0

    parts = []

    # 1. Direção
    parts.append(
        f"1. DIREÇÃO: {signal.symbol} apresenta viés de {direction_text} no timeframe {signal.timeframe} "
        f"com {signal.confidence:.0%} de probabilidade. "
        f"{'Sinal acionável.' if is_actionable else 'Probabilidade abaixo do mínimo de 75% — aguardar.'}"
    )

    # 2. BTC estrutura (contexto limitado sem macro)
    parts.append(
        "2. BTC: Contexto macro completo disponível apenas com análise IA ativada. "
        "Recomenda-se verificar manualmente o viés do BTC no TF diário antes de operar."
    )

    # 3–5. Macro
    parts.append(
        "3–5. MACRO (DXY/SP500/Nasdaq): Ative a análise IA para contexto macro completo com dados em tempo real."
    )

    # 6. Par + indicadores
    ind_text = f"EMA12={ind.ema9} | EMA26={ind.ema21} | RSI={ind.rsi} | ADX={ind.adx} | Supertrend={'Alta' if ind.supertrend_direction == 1 else 'Baixa' if ind.supertrend_direction == -1 else 'N/D'}"
    pat_text = "; ".join(p.description for p in signal.patterns[:2]) if signal.patterns else "Nenhum padrão detectado"
    parts.append(f"6. PAR BTC: {ind_text}. Padrões ({signal.timeframe}): {pat_text}.")

    # 7. Operação
    type_map = {TradeType.SCALP: "Scalp", TradeType.DAY_TRADE: "Day Trade",
                TradeType.SWING: "Swing Trade", TradeType.HODL: "HODL"}
    if is_actionable:
        parts.append(
            f"7. OPERAÇÃO ({type_map.get(signal.trade_type, '')} — TF {signal.timeframe}): "
            f"{signal.direction.value.upper()} em {signal.entry:.6g}. "
            f"Stop: {signal.stop_loss:.6g} (risco {risk_pct:.1f}% — posicionado abaixo/acima de estrutura de suporte/resistência). "
            f"TP1: {signal.tp1:.6g} (resistência imediata, probabilidade ~60%). "
            f"TP2: {signal.tp2:.6g} (próximo nível de liquidez, ~35%). "
            f"TP3: {signal.tp3:.6g} (extensão de movimento, ~15%). RR 1:{signal.risk_reward}."
        )
    else:
        parts.append(
            f"7. AGUARDAR: Probabilidade {signal.confidence:.0%} < 75%. "
            f"Falta confirmar: {'ADX > 25 para tendência mais forte' if (ind.adx or 0) < 25 else ''}"
            f"{', RSI saindo de zona extrema' if ind.rsi and 30 <= ind.rsi <= 70 else ''}"
            f". Reanalisar após próximo fechamento de candle."
        )

    # 8. Alertas
    if is_actionable:
        parts.append(
            f"8. ALERTAS SIMULADOS: 🎯 TP1 {signal.tp1:.6g} | 🎯 TP2 {signal.tp2:.6g} | "
            f"🎯 TP3 {signal.tp3:.6g} | 🛑 Stop {signal.stop_loss:.6g} | "
            f"⚠️ Invalidação: fechamento além do stop | 🚪 Saída a mercado se estrutura romper."
        )

    return "\n\n".join(parts)
