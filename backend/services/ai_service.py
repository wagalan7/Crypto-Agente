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


def _format_signal_for_prompt(signal: TradeSignal) -> str:
    direction_map = {
        SignalDirection.LONG: "COMPRA (Long)",
        SignalDirection.SHORT: "VENDA (Short)",
        SignalDirection.NEUTRAL: "NEUTRO",
    }
    trade_type_map = {
        TradeType.SCALP: "Scalp",
        TradeType.DAY_TRADE: "Day Trade",
        TradeType.SWING: "Swing Trade",
        TradeType.HODL: "HODL",
    }
    ind = signal.indicators
    patterns_text = "\n".join(
        f"  - {p.type.value} (confiança {p.confidence:.0%}): {p.description}"
        for p in signal.patterns[:5]
    )

    return f"""
Ativo: {signal.symbol}
Timeframe: {signal.timeframe}
Tipo de Operação: {trade_type_map.get(signal.trade_type, '')}
Direção: {direction_map.get(signal.direction, '')}
Força do Sinal: {signal.signal_strength} ({signal.confidence:.0%})

NÍVEIS:
  Entrada: {signal.entry}
  Stop Loss: {signal.stop_loss} (risco: {abs(signal.entry - signal.stop_loss) / signal.entry * 100:.2f}%)
  TP1: {signal.tp1}
  TP2: {signal.tp2}
  TP3: {signal.tp3}
  Risco/Retorno: 1:{signal.risk_reward}

INDICADORES:
  RSI(14): {ind.rsi}
  MACD: {ind.macd} | Sinal: {ind.macd_signal} | Hist: {ind.macd_hist}
  BB Superior: {ind.bb_upper} | Meio: {ind.bb_middle} | Inferior: {ind.bb_lower}
  EMA9: {ind.ema9} | EMA21: {ind.ema21} | EMA50: {ind.ema50} | EMA200: {ind.ema200}
  ATR: {ind.atr}
  ADX: {ind.adx}
  Stoch RSI K: {ind.stoch_k} | D: {ind.stoch_d}
  Supertrend: {ind.supertrend} (dir: {'Alta' if ind.supertrend_direction == 1 else 'Baixa'})
  Volume médio: {ind.volume_avg}

PADRÕES DETECTADOS:
{patterns_text if patterns_text else '  Nenhum padrão significativo detectado'}
"""


async def generate_ai_analysis(signal: TradeSignal) -> str:
    if not ANTHROPIC_API_KEY:
        return _fallback_analysis(signal)

    client = get_client()
    prompt_data = _format_signal_for_prompt(signal)

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=(
                "Você é um analista técnico experiente de criptomoedas. "
                "Analise os dados fornecidos e forneça uma análise técnica concisa em português. "
                "Seja direto, objetivo e profissional. Máximo 4 parágrafos curtos. "
                "Foque nos pontos mais importantes: contexto de mercado, qualidade do setup, "
                "riscos principais e o que invalidaria o setup."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Analise este setup técnico e forneça sua avaliação:\n{prompt_data}",
                }
            ],
        )
        return message.content[0].text
    except Exception as e:
        return _fallback_analysis(signal)


def _fallback_analysis(signal: TradeSignal) -> str:
    ind = signal.indicators
    parts = []

    direction_text = "bullish" if signal.direction == SignalDirection.LONG else "bearish" if signal.direction == SignalDirection.SHORT else "neutro"

    parts.append(
        f"O ativo {signal.symbol} apresenta viés {direction_text} no timeframe {signal.timeframe} "
        f"com sinal de força {signal.signal_strength} ({signal.confidence:.0%} de confiança)."
    )

    indicator_notes = []
    if ind.rsi:
        if ind.rsi < 30:
            indicator_notes.append(f"RSI em zona de sobrevenda ({ind.rsi:.1f})")
        elif ind.rsi > 70:
            indicator_notes.append(f"RSI em zona de sobrecompra ({ind.rsi:.1f})")
        else:
            indicator_notes.append(f"RSI neutro ({ind.rsi:.1f})")

    if ind.macd and ind.macd_signal:
        if ind.macd > ind.macd_signal:
            indicator_notes.append("MACD cruzando acima da linha de sinal (bullish)")
        else:
            indicator_notes.append("MACD abaixo da linha de sinal (bearish)")

    if ind.adx:
        if ind.adx > 25:
            indicator_notes.append(f"ADX={ind.adx:.1f} indica tendência forte")
        else:
            indicator_notes.append(f"ADX={ind.adx:.1f} indica mercado sem tendência clara")

    if indicator_notes:
        parts.append("Indicadores: " + ". ".join(indicator_notes) + ".")

    if signal.patterns:
        top_patterns = signal.patterns[:2]
        pattern_desc = "; ".join(p.description for p in top_patterns)
        parts.append(f"Padrões identificados: {pattern_desc}.")

    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100
    parts.append(
        f"Setup oferece RR 1:{signal.risk_reward} com risco de {risk_pct:.1f}% até o stop. "
        f"Invalidação acima/abaixo do stop em {signal.stop_loss:.6g}."
    )

    return "\n\n".join(parts)
