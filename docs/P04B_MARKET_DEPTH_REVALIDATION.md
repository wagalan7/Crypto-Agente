# P04B — Revalidação de profundidade antes de entrada MARKET

Data: 2026-08-26
Base: P04A (`b4027e41`)
Escopo: endurecimento do executor Binance; sem alteração de estratégia.

## Resultado

Toda abertura MARKET normal na Binance agora exige uma revalidação fail-closed
executada dentro do transporte, depois do throttle e imediatamente antes do
POST. A decisão usa um snapshot novo de profundidade para provar que 100% da
quantidade cabe no book dentro dos limites de spread, impacto, slippage, chase,
zona e R:R.

Nenhuma saída `reduceOnly` depende desse guard. Fechamentos de emergência e
reduções de posição continuam disponíveis mesmo se o depth estiver indisponível.

## Antes e depois

ANTES:

```text
plano aprovado → MARKET pela quantidade planejada → fill real → proteção
```

O top-of-book da P04A era suficiente para a LIMIT maker, mas não provava o custo
de consumir vários níveis em uma MARKET.

DEPOIS:

```text
plano aprovado
→ throttle/rate gate
→ filtros MARKET_LOT_SIZE + MIN_NOTIONAL
→ depth Binance novo, sem cache
→ runtime gates (RiskState/kill switch/quarentena)
→ VWAP + pior preço para 100% da qty
→ cap de risco/notional + floor no step
→ spread/impact/slippage/chase/zona/R:R
→ checagem final do rate gate
→ POST MARKET com a qty aprovada
→ proteção e P03 com qty/identidade reais
```

Qualquer dado ausente, inválido, não finito, stale, lento, divergente ou
insuficiente bloqueia a abertura com zero POST de entrada.

## Contrato do depth

A leitura usa `GET /fapi/v1/depth` na mesma BASE da Binance ativa, sem cache,
com `limit=50` por padrão. O snapshot precisa conter:

- `lastUpdateId`, `E` e `T` inteiros positivos;
- bids estritamente decrescentes e asks estritamente crescentes;
- preços e quantidades positivos e finitos;
- símbolo vinculado à requisição e venue Binance;
- timestamps dentro da idade máxima e latência dentro do teto;
- book não cruzado/travado;
- liquidez para preencher exatamente 100% da quantidade avaliada.

LONG consome asks; SHORT consome bids. O avaliador calcula com `Decimal`:

- melhor preço executável;
- VWAP da quantidade inteira;
- pior nível consumido;
- spread;
- impacto do VWAP contra o melhor preço;
- quantidade disponível/preenchida e níveis usados.

Não há extrapolação de liquidez além do snapshot.

## Quantidade e risco

A quantidade final nunca pode aumentar. Ela é limitada pelo menor valor entre:

- quantidade já planejada;
- risco monetário original, recalculado no pior preço;
- notional original, recalculado no pior preço.

Depois, a quantidade é truncada para baixo pelo `MARKET_LOT_SIZE.stepSize` e
validada contra `minQty`, `maxQty` e `MIN_NOTIONAL`. O executor repete essa
validação dentro do transporte e assina o POST com a quantidade aprovada.

A resposta sempre propaga `submitted_qty` e o `client_order_id` efetivo. Em
timeout/estado ambíguo, proteção e P03 usam a quantidade realmente submetida,
não a quantidade anterior ao cap.

## MARKET direto e fallback maker

Uma abertura MARKET Binance sem callback P04B retorna
`EXEC_MARKET_PREFLIGHT_REQUIRED` antes até do arredondamento e envia zero POST.
Isso fecha bypasses fora do fluxo principal.

O fallback maker → MARKET continua desligado por padrão e exige simultaneamente:

- `MAKER_FALLBACK_MARKET=true`;
- `P04B_MAKER_FALLBACK_ENABLED=true`;
- `P04B_MARKET_REVALIDATION_ENABLED=true`;
- capability de depth disponível.

Mesmo ligado, o fallback só é permitido após rejeição GTX explícita (`-5022`,
post-only rejeitada ou ordem que cruzaria imediatamente). Timeout, transporte
incerto, fill parcial ou cancelamento incerto nunca geram uma segunda MARKET.
O fallback recebe um `clientOrderId` próprio, diferente da LIMIT maker, e passa
pelo mesmo callback de depth.

## Flags

- `P04B_MARKET_REVALIDATION_ENABLED=true`: guard ativo por padrão. Se false,
  aberturas MARKET são bloqueadas; o caminho antigo não é restaurado.
- `P04B_DEPTH_TIMEOUT_S=1.5`
- `P04B_MAX_DEPTH_AGE_MS=1500`
- `P04B_MAX_FETCH_LATENCY_MS=1500`
- `P04B_DEPTH_LIMIT=50`
- `P04B_MAX_BOOK_IMPACT_PCT=0.10`
- `P04B_MAX_MARKET_SLIPPAGE_PCT`: herda o teto adverso da P04A por padrão.
- `P04B_MAKER_FALLBACK_ENABLED=false`: segunda trava do fallback.

Nenhuma flag de estratégia ou LIVE foi ligada. Maker, TF upgrade e pyramiding
permanecem com seus defaults anteriores.

## Impacto operacional intencional

- O fluxo automático normal do Crypto Win está integrado à P04B.
- O endpoint administrativo de abertura de teste não possui contexto completo
  de estratégia/stop/alvos e, portanto, agora falha fechado em vez de abrir uma
  MARKET sem revalidação.
- Pyramiding e regime hedge continuam OFF e também falham fechado se alguém os
  ligar isoladamente antes de uma integração própria com o mesmo contrato.
- Saídas `reduceOnly`, inclusive emergência, não foram bloqueadas.
- Bybit não ganhou implementação parcial: sem capability de depth, abertura
  automática é bloqueada em vez de degradar silenciosamente.

## Observabilidade

Os bloqueios reutilizam o `_record_skip` e o contrato de preflight existentes.
Principais reason codes:

- `EXEC_MARKET_PREFLIGHT_REQUIRED`
- `EXEC_DEPTH_UNAVAILABLE|FETCH_ERROR|TIMEOUT|RATE_LIMITED`
- `EXEC_DEPTH_TIMESTAMP_INVALID|STALE|SLOW|INVALID`
- `EXEC_DEPTH_INSUFFICIENT`
- `EXEC_SPREAD_TOO_WIDE`
- `EXEC_BOOK_IMPACT_TOO_HIGH`
- `EXEC_SLIPPAGE_TOO_HIGH`
- `EXEC_PRICE_CHASE`
- `EXEC_ENTRY_OUTSIDE_ZONE`
- `EXEC_RR_TP1_TOO_LOW|EXEC_RR_TP2_TOO_LOW`
- `EXEC_QTY_REVALIDATION_FAILED`
- `EXEC_MIN_NOTIONAL_FAILED`

Nenhuma tabela, API de execução imediata ou painel novo foi criado.

## Validação

A suíte P04B cobre, sem rede/banco/exchange real:

- LONG e SHORT em vários níveis, VWAP e pior preço;
- cobertura exata e depth insuficiente;
- book vazio, duplicado, desordenado, cruzado, NaN/infinito;
- venue/símbolo/lado, timestamps, latência e update id;
- limites exatos e violações de spread, impacto, slippage, chase, zona e R:R;
- endpoint/params/peso HTTP e 418/429;
- ordem `throttle/filter/depth guard → POST` e zero POST em veto;
- floor, quantidade monotônica, min notional e números não finitos;
- timeout após quantidade reduzida e proteção pela `submitted_qty`;
- `reduceOnly` sem dependência do guard de entrada;
- fallback apenas em rejeição GTX explícita, COID distinto e zero fallback em
  estado ambíguo;
- identidade/quantidade efetivas preservadas no P03;
- hermeticidade com DNS/TCP bloqueados e contabilizados.

Suíte crítica P01+P02+P03+P04A+P04B: 225 testes, executada duas vezes no Python
3.11. `py_compile` e `git diff --check` também fazem parte do gate final.

## Fora do escopo

- nenhuma alteração de sinal, score, IA, indicadores, funding, OI ou regime;
- nenhuma ativação de maker, fallback, TF upgrade, pyramiding ou LIVE;
- nenhuma nova entry/fallback durante testes;
- nenhum acesso a exchange ou banco externo;
- nenhum push ou deploy nesta etapa.
