# P04C — validade central dos dados de entrada

## Objetivo

Impedir que uma entrada automática LIVE seja criada a partir de vela aberta,
série incompleta, contexto vencido ou dados pertencentes a outro símbolo. O
P04C não altera estratégia, score, indicadores, IA, stops, alvos ou sizing.

## Antes

- Os provedores podiam entregar a vela corrente ainda aberta, que entrava no
  cálculo de indicadores, padrões e direção.
- A recomendação não carregava prova de origem/idade do candle, ticker,
  funding/OI e MTF.
- O regime macro classificava falha de leitura como `NORMAL`, sem distinguir
  dado válido de contexto desconhecido no último gate LIVE.

## Depois

1. Todo caminho de análise (server, legado e batch) normaliza OHLCV antes dos
   indicadores, remove apenas a vela ainda aberta e rejeita schema inválido,
   NaN/infinito, timestamps fora de ordem, duplicidade, futuro, lacunas e último
   candle fechado vencido.
2. O `TradeSignal` transporta `data_freshness` com símbolo, timeframe, fonte,
   abertura/fechamento e instante de observação.
3. Ticker e derivativos carregam identidade e horário da coleta. Eles só são
   obrigatórios no gate quando algum valor realmente participou do sinal.
4. Cada timeframe superior usado pelo MTF passa pelo mesmo contrato de candle
   fechado. O MTF sintético server-side preserva a prova dos sinais já calculados.
5. O regime informa `FRESH`, `DEGRADED`, `UNKNOWN` ou `DISABLED`; ausência do
   BTC 24h é `UNKNOWN` e não é cacheada como contexto seguro.
6. Antes de qualquer entrada LIVE, o gate central revalida identidade e idade.
   Falha, exceção ou flag desligada resulta em skip explicável e zero ordem.
7. Shadow permanece disponível para aprendizado. P04A/P04B continuam sendo os
   responsáveis pela cotação, depth, VWAP e slippage imediatamente antes do POST.

## Configuração

Defaults seguros, sem ativar maker, fallback ou outra capability:

- `P04C_DATA_FRESHNESS_ENABLED=true`
- `P04C_MAX_CANDLE_LAG_PERIODS=1.25`
- `P04C_MAX_TICKER_AGE_MS=300000`
- `P04C_MAX_DERIVATIVES_AGE_MS=300000`
- `P04C_MAX_REGIME_AGE_MS=900000`

No LIVE, definir `P04C_DATA_FRESHNESS_ENABLED=false` bloqueia novas entradas;
não restaura o caminho antigo. Os valores aparecem em `/api/shadow/env`.

## Reason codes principais

- `EXEC_CANDLE_INVALID|GAP|FUTURE|NOT_CLOSED|STALE`
- `EXEC_SIGNAL_IDENTITY_MISMATCH`
- `EXEC_TICKER_*`, `EXEC_DERIVATIVES_*`, `EXEC_MTF_*`, `EXEC_REGIME_*`
- `EXEC_DATA_FRESHNESS_DISABLED|UNKNOWN|OK`

## Validação local

- 18 testes P04C verdes: contrato puro, identidade, gaps, optional-context,
  regime, MTF e ordem arquitetural dos gates.
- `py_compile` aprovado nos arquivos P04C e `git diff --check` no fechamento.
- Nenhuma rede, exchange ou banco externo foi acessado.
- A suíte conjunta P04A/P04B não foi repetida nesta sessão porque o único venv
  completo disponível no host é Python 3.9 e o repositório já usa anotações de
  produção 3.11; a tentativa encerrou no import, antes de executar testes. As
  baselines P04A/P04B permanecem nos 50 testes e 225 testes críticos previamente
  aprovados; nenhuma lógica dessas fases foi modificada.

## Invariantes

- Vela aberta nunca forma sinal.
- Contexto `UNKNOWN` nunca autoriza entrada LIVE.
- Dados de outro símbolo/timeframe nunca autorizam entrada.
- P04C não envia ordens nem acessa rede.
- Nenhuma estratégia, score ou IA foi alterada.
- Maker, fallback MARKET, TF upgrade, pyramiding e Bybit LIVE não são ativados.
