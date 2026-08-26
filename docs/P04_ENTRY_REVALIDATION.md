# P04A — Revalidação fail-closed da entrada maker

## Objetivo

Impedir que uma entrada maker seja submetida com cotação velha, de outra
corretora ou com a geometria já degradada. A P04A não muda sinal, score, IA,
stop, alvos ou preço estrutural da ordem.

## Antes → depois

- **Antes:** o gate live chamava `services.binance_service.fetch_ticker`, que na
  prática lê a OKX, e o R:R final usava `current_price` derivado do último candle.
  Nenhum dos dois possuía prova de frescor na Binance no instante do POST maker.
- **Depois:** o helper maker lê `bookTicker` diretamente da mesma Binance e do
  mesmo modo/proxy usados pela ordem, sem cache. Um avaliador puro com `Decimal`
  valida a cotação depois do arredondamento de preço/qty e imediatamente antes
  do POST GTX. UNKNOWN ou veto produz zero submissões de entrada.

O gate antigo de liquidez continua existindo para volume/telemetria legada, mas
ele não é mais a barreira de segurança da entrada maker: a decisão final usa a
cotação Binance da P04A.

## Contrato da cotação

`get_execution_quote()` usa `/fapi/v1/ticker/bookTicker`, compartilha o rate gate
e o cooldown 418/429, e retorna:

- exchange, fonte e símbolo;
- bid/ask e quantidades do top-of-book;
- instante local de início/recebimento;
- timestamp obrigatório da exchange;
- latência da leitura.

Não há cache nem fallback para OHLCV, ticker de 60 segundos ou outra venue.
O header `X-MBX-USED-WEIGHT-1M` desse endpoint é documentado pela Binance como
inexato e, por isso, não é usado para recalibrar o throttle global.

## Validações

O veredito é fail-closed para:

- resposta ausente, erro, timeout ou rate-limit;
- símbolo/venue divergente;
- `NaN`, infinito, zero, book cruzado ou quantidades inválidas;
- timestamp local/exchange vencido e leitura lenta;
- spread acima do teto vigente;
- chase recalculado com ask para LONG e bid para SHORT;
- slippage adverso da LIMIT final contra a entrada planejada;
- LIMIT final que cruzaria o book e deixaria de ser maker;
- LIMIT fora da zona estrutural aprovada;
- stop/TPs do lado errado ou já atravessados;
- R:R de TP1/TP2 degradado no preço executável fresco;
- qty inválida ou tentativa de aumentar a exposição.

LONG usa ask e SHORT usa bid como preço executável conservador para revalidar o
setup. A ordem GTX preserva a LIMIT planejada/arredondada e a P04A não reprifica.
A LIMIT final também é comparada com o plano pelo teto de slippage maker. Se a
qty precisaria ser reduzida depois da cotação final, o ciclo é bloqueado; não há
novo arredondamento assíncrono entre o último veredito e o POST. O redimensionamento
controlado do caminho MARKET pertence à P04B.

Depois de ler a cotação, o callback consulta novamente RiskState, kill switch e
o latch de quarentena. A validação pura roda sem `await` e o POST da entrada é o
próximo ponto assíncrono. Uma pausa ou quarentena surgida durante a leitura do
book bloqueia a submissão.

## Reason codes

Principais códigos estáveis:

- `EXEC_QUOTE_UNAVAILABLE`, `EXEC_QUOTE_RATE_LIMITED`, `EXEC_QUOTE_STALE`;
- `EXEC_QUOTE_INVALID`, `EXEC_SYMBOL_MISMATCH`, `EXEC_QUOTE_VENUE_MISMATCH`;
- `EXEC_BOOK_CROSSED`, `EXEC_BOOK_LIQUIDITY_INVALID`;
- `EXEC_SPREAD_TOO_WIDE`, `EXEC_PRICE_CHASE`;
- `EXEC_SLIPPAGE_TOO_HIGH`, `EXEC_RISK_PAUSED`, `EXEC_KILL_SWITCH_BLOCKED`;
- `EXEC_RATE_GATE_CHANGED`, `EXEC_SIDE_INVALID`, `EXEC_PREFLIGHT_DISABLED`;
- `EXEC_MAKER_WOULD_TAKE`, `EXEC_ENTRY_OUTSIDE_ZONE`;
- `EXEC_LEVELS_INVALID`, `EXEC_RR_TP1_TOO_LOW`, `EXEC_RR_TP2_TOO_LOW`;
- `EXEC_QTY_REVALIDATION_FAILED`, `EXEC_PREFLIGHT_ERROR`.

Veto no helper retorna `entry_not_submitted=true`, `preflight_failed=true`,
`no_fill=true`, sem quarentena (não houve ordem). O caller grava o código pelo
mesmo `_record_skip`/`SkipReasonStat` já existente.

## Flags e limites

- `MAKER_ENTRY_ENABLED=false` permanece o default.
- `P04A_ENTRY_REVALIDATION_ENABLED=true`: ao ligar maker, o guard não pode ser
  esquecido por acidente. Se a flag for desligada com maker ligado, a entrada é
  bloqueada; desligar P04A nunca restaura o caminho inseguro.
- `MAKER_FALLBACK_MARKET=false` por default e o caller força `false` durante a
  P04A, mesmo que uma configuração antiga solicite o contrário.
- `P04A_QUOTE_TIMEOUT_S=1.5`;
- `P04A_MAX_QUOTE_AGE_MS=1500`;
- `P04A_MAX_FETCH_LATENCY_MS=1500`;
- `P04A_MAX_ADVERSE_SLIPPAGE_PCT=0.10`;
- `P04A_MAX_CHASE_ATR`: vazio herda o teto do proximity gate e respeita a lane
  de breakout já aprovada; valor explícito atua como override.
- spread e pisos de R:R reutilizam os contratos existentes e respeitam seus
  gates; nenhuma constante de estratégia foi recalibrada.

## Invariantes preservadas

- maker, TF upgrade e pyramiding continuam OFF por default;
- nenhuma estratégia, score ou IA foi alterada;
- maker ligado sem capability maker bloqueia; nunca cai para MARKET direto;
- clientOrderId e lifecycle P02/P03 permanecem iguais; a qty realmente submetida
  é propagada ao incidente P03 quando a resposta fica ambígua;
- UNKNOWN nunca vira FLAT/PROTECTED;
- nenhuma ordem é reenviada;
- nenhum fallback MARKET ocorre na P04A;
- nenhuma tabela, migration, endpoint ou frontend novo;
- nenhuma rede real, push ou deploy nos testes/implementação local.

## Testes

`tests/test_p04a_execution_preflight.py` cobre núcleo puro, Binance bookTicker,
rate-limit, LONG/SHORT, limites exatos, freshness local/exchange, spread, chase,
slippage, R:R, zona/níveis, qty monotônica, runtime pause/quarentena, propagação
da qty ao P03, exceção no guard e prova de zero POST de entrada/zero MARKET quando
bloqueado. DNS/conexão TCP são bloqueados e contabilizados.

A suíte P04A possui 26 testes. A suíte crítica P04A + P01/P02/P03 totalizou 201
testes e passou 2× no Python 3.11 real. `py_compile` e `git diff --check` também
passaram. A revisão independente final não encontrou bloqueadores.

## Fora do escopo / P04B

- revalidação dentro do fallback MARKET e do caminho MARKET direto;
- depth agregado e slippage por toda a quantidade (top-of-book não prova isso);
- frescor central de candle, funding, OI, regime e macro;
- ativação do maker ou de dinheiro real;
- push e deploy.

Até a P04B fechar esses itens, fallback MARKET permanece impossível no caller
maker da P04A.
