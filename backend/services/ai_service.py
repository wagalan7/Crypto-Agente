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
    is_actionable = signal.confidence >= 0.80
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100 if signal.entry else 0

    return f"""
{macro_context}

=== ANÁLISE DO ATIVO ===
Ativo: {signal.symbol}
Timeframe Principal: {signal.timeframe} (maior confluência encontrada)
Tipo de Operação: {trade_type_map.get(signal.trade_type, '')}
Direção: {direction_map.get(signal.direction, '')}
Probabilidade: {signal.confidence:.0%} | Força: {signal.signal_strength}
Operação Acionável (≥80%): {"SIM" if is_actionable else "NÃO — aguardar confluência"}

=== INDICADORES ===
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


SYSTEM_PROMPT = """Você é um analista técnico sênior especializado em criptomoedas. Siga EXATAMENTE estas regras:

REGRAS OBRIGATÓRIAS:
1. MACRO PRIMEIRO: Analise o contexto macro do BTC (viés, estrutura, tendência dominante).
2. ATIVO: Analise o par USDT e mencione a força relativa vs BTC.
3. INDICADORES: Use EMA 12 e EMA 26 como referência de tendência.
4. TIMEFRAME: Confirme se o TF fornecido é o de maior confluência. Se não for, indique qual seria.
5. OPERAÇÃO: Só recomende entrada se probabilidade ≥ 80%. Se menor, diga "AGUARDAR".
6. ALVOS: Sempre valores reais positivos. Nunca coloque alvos negativos ou menores que o stop em long.
7. JUSTIFICATIVA: Explique stop e alvos com base em: suporte/resistência, estrutura de mercado, liquidez e padrão identificado.
8. SAÍDA: Mostre APENAS a melhor operação ativa (uma única operação).
9. ALERTAS: Ao final, liste os alertas simulados: nível de alvo, stop, invalidação e saída a mercado.
10. GESTÃO: Classifique: Scalp (minutos/1h), Day Trade (1h-8h), Swing (1d-3d).

FORMATO DE RESPOSTA (em português, máximo 6 parágrafos):
§1 MACRO: Contexto BTC + impacto no mercado
§2 ATIVO: Análise técnica com indicadores chave (EMA 12/26, RSI, MACD, ADX, Supertrend)
§3 PADRÕES: Padrões detectados no TF e implicações
§4 OPERAÇÃO: Se ≥80% → Entrada, Stop, TP1/TP2/TP3 com justificativa baseada em S/R e estrutura. Se <80% → "AGUARDAR: [motivo específico]"
§5 ALERTAS SIMULADOS: 🎯 Alvo | 🛑 Stop | ⚠️ Invalidação | 🚪 Saída a mercado
§6 CLASSIFICAÇÃO: Tipo de operação recomendado + horizonte temporal"""


async def generate_ai_analysis(signal: TradeSignal, macro_context: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return _fallback_analysis(signal)

    client = get_client()
    prompt_data = _format_signal_for_prompt(signal, macro_context)

    try:
        message = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analise este setup e forneça análise completa seguindo as 10 regras:\n{prompt_data}",
                }
            ],
        )
        return message.content[0].text
    except Exception:
        return _fallback_analysis(signal)


def _fallback_analysis(signal: TradeSignal) -> str:
    ind = signal.indicators
    is_actionable = signal.confidence >= 0.80
    direction_text = "alta" if signal.direction == SignalDirection.LONG else "baixa" if signal.direction == SignalDirection.SHORT else "neutro"
    risk_pct = abs(signal.entry - signal.stop_loss) / signal.entry * 100 if signal.entry else 0

    parts = []
    parts.append(
        f"MACRO: Contexto de mercado requer atenção ao BTC como ativo líder. "
        f"Dominância e correlação com índices tradicionais (DXY, Nasdaq) podem influenciar a operação."
    )
    parts.append(
        f"ATIVO: {signal.symbol} apresenta viés de {direction_text} no timeframe {signal.timeframe} "
        f"com {signal.confidence:.0%} de probabilidade. "
        f"EMA12={ind.ema9} | EMA26={ind.ema21} | RSI={ind.rsi} | ADX={ind.adx}."
    )

    if signal.patterns:
        pattern_desc = "; ".join(p.description for p in signal.patterns[:2])
        parts.append(f"PADRÕES ({signal.timeframe}): {pattern_desc}.")

    if is_actionable:
        parts.append(
            f"OPERAÇÃO (≥80%): {signal.direction.value.upper()} em {signal.entry:.6g}. "
            f"Stop: {signal.stop_loss:.6g} (risco {risk_pct:.1f}% — abaixo/acima de suporte/resistência). "
            f"TP1: {signal.tp1:.6g} | TP2: {signal.tp2:.6g} | TP3: {signal.tp3:.6g} "
            f"(alvos em níveis de liquidez e estrutura). RR 1:{signal.risk_reward}."
        )
        parts.append(
            f"ALERTAS SIMULADOS: 🎯 Alvo TP1 em {signal.tp1:.6g} | "
            f"🛑 Stop em {signal.stop_loss:.6g} | "
            f"⚠️ Invalidação: fechamento além do stop | "
            f"🚪 Saída a mercado se estrutura romper antes dos alvos."
        )
    else:
        parts.append(
            f"AGUARDAR: Probabilidade {signal.confidence:.0%} abaixo do mínimo de 80%. "
            f"Aguardar confirmação adicional: ADX > 25, RSI saindo de zona extrema ou padrão com breakout confirmado."
        )

    type_map = {TradeType.SCALP: "Scalp (minutos a 1h)", TradeType.DAY_TRADE: "Day Trade (1h-8h)",
                TradeType.SWING: "Swing Trade (1d-3d)", TradeType.HODL: "HODL (semanas)"}
    parts.append(f"CLASSIFICAÇÃO: {type_map.get(signal.trade_type, signal.trade_type.value)}.")

    return "\n\n".join(parts)
