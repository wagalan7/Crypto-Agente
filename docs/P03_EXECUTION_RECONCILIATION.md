# P03 — Reconciliação persistente de execução

## Objetivo

Tornar **persistente** a reconciliação de ordens, posições e condicionais que
permaneçam `UNKNOWN`, inclusive após restart:

```
submissão → safety state P02 → incidente persistido → restart/reconciler
→ ordem + posição + condicionais reconciliadas
→ PROTECTED | FLAT | MANUAL_REQUIRED → liberação segura da quarentena própria
```

Nenhuma nova entrada pode ocorrer enquanto existir incidente não resolvido.

## ANTES → DEPOIS

- **ANTES:** o P02 bloqueava localmente estados `UNKNOWN` (latch em memória +
  pausa no `RiskState`), mas alguns incidentes dependiam do processo atual e
  podiam perder continuidade após restart (o boot só acusava drift).
- **DEPOIS:** o incidente `UNKNOWN` é **persistido** (`execution_incidents`),
  **recuperado no boot** (armando a quarentena ANTES do primeiro scan) e
  **reconciliado** por consulta idempotente até `PROTECTED`, `FLAT` ou
  `MANUAL_REQUIRED`.

## Reuso do P02 (sem novo motor de ordens)

Reutiliza as primitivas P02 de **leitura/cancelamento**: `get_order`
(reconciliação por `clientOrderId`), `_fresh_position_size`/
`get_positions(force=True)`, `get_open_algo_orders`, `cancel_algo_order`, o
contrato de SL por `algoId`, o **latch** de `shadow_trade_service`, a pausa do
`RiskState` e o `RealTrade`. **P03 não coloca ordem nova**: adota um SL vivo que
cumpra o contrato de cobertura ou escala para `MANUAL_REQUIRED` (a criação de
proteção na entrada segue sendo do fail-safe P02).

Criados apenas: **1 model** (`ExecutionIncident`), **1 serviço**
(`execution_reconciliation_service.py`), **1 task** integrada ao `lifespan`
existente e **1 endpoint read-only**. Nenhum worker/scheduler/queue/retry engine
paralelo. O reconciliador só **consulta** a exchange — nunca reenvia entry,
nunca emite MARKET cego, nunca declara `no_fill` sem confirmação.

## Kinds

`ENTRY_SUBMISSION_UNKNOWN`, `ENTRY_ORDER_UNKNOWN`, `FINAL_FILL_QTY_UNKNOWN`,
`CONDITIONAL_SUBMISSION_UNKNOWN`, `CLEANUP_PENDING`, `UNTRACKED_POSITION`,
`PERSISTENCE_FAILURE`.

## Lifecycle

`OPEN → RECONCILING → PROTECTED | FLAT`. Estados de segurança (mantêm pausa):
`RETRY_PENDING`, `MANUAL_REQUIRED`. `UNKNOWN` **nunca** vira `FLAT`. Resolução
só com **prova positiva** de segurança; nunca por tempo decorrido.

## Regras principais

- **Qty terminal:** `FILLED` → qty confirmada; `REJECTED` → zero;
  `CANCELED/EXPIRED/EXPIRED_IN_MATCH` → exigem `executedQty` terminal explícita;
  ausente/inválida → `FINAL_FILL_QTY_UNKNOWN`. Fill antes do terminal é apenas
  **lower bound** (nunca qty final).
- **Posição:** decisões usam `positionRisk` fresh (`force=True`); leitura
  stale/rate-limited/erro é `UNKNOWN` (mantém incidente e pausa; não cancela
  proteção nem fecha `RealTrade`). Posição anterior indistinguível →
  `MANUAL_REQUIRED`.
- **Condicionais:** cancelamento **idempotente** por IDs **exatos** (SL/TP1/TP2),
  sem prefix match amplo; nunca toca ordem de outro trade. Cleanup exige **grace**
  com observações negativas em **ciclos separados**; condicional que reaparece
  cancela e **zera** o contador.
- **Maker:** continua **OFF** por default; incidentes históricos/simulados são
  reconciliados. Ordem maker viva → pedido de cancelamento; incerto → mantém
  quarentena; **sem fallback MARKET**.
- **Untracked / persistence failure:** nunca fecha automaticamente →
  `MANUAL_REQUIRED` (pausa persiste). Leitura da exchange indisponível → **falha
  fechado** (não assume conta flat).
- **Exchange mismatch:** mantido bloqueado, motivo registrado, **sem mutação**.
- **Idempotência:** `incident_key` único (mesmo evento 2× / restart 2× → 1
  registro); claim/lease persistente (`claimed_by`/`lease_expires_at`) → 1 claim
  ativo, sem processamento concorrente; consulta repetida nunca cria entry;
  retry após timeout reconcilia a MESMA ordem.
- **Backoff:** tentativas limitadas por ciclo, backoff persistido, retry após
  restart; falha repetida/estado inconclusivo prolongado → `MANUAL_REQUIRED`.

## Pausa / quarentena

Incidente aberto bloqueia entrada normal, hedge, pyramiding, TF Upgrade e
qualquer abertura live (reutiliza os gates P02). A pausa **sobrevive ao restart**
(armada no boot antes do primeiro scan). Libera **somente a quarentena própria**
do P03 quando todos os incidentes estiverem seguros, sem posição untracked, sem
entry pendente e cleanup confirmado — **nunca** remove a pausa manual do operador.

## Configuração operacional (defaults conservadores, fail-closed)

`RECONCILE_INTERVAL_S=30`, `RECONCILE_BACKOFF_BASE_S=15`,
`RECONCILE_BACKOFF_MAX_S=900`, `RECONCILE_CLEAN_GRACE=2`,
`RECONCILE_LEASE_S=120`, `RECONCILE_MAX_ATTEMPTS=8`, `RECONCILE_MAX_PER_CYCLE=10`.

## API (somente leitura)

`GET /api/execution-incidents/status` — total aberto, retry pending, manual
required, protected, flat, quarantine active, itens resumidos, última
reconciliação e se o reconciliador está rodando. **Sem** execute/retry-now/
resolve/close/enable-live. Frontend inalterado neste P03.

## Testes

`backend/tests/test_p03_execution_reconciliation.py` — 37 testes herméticos
(nenhuma rede/Binance/DB real; nenhuma entry criada). Suíte crítica completa
(P01+P02+P03) = **93 testes**, executada **2×**, verde. `py_compile` e
`git diff --check` verdes.

> Nota de ambiente: produção roda Python 3.11 (onde `Mapped[X | None]` dos models
> resolve nativamente). O único interpretador com deps nesta máquina é o 3.9; as
> suítes foram executadas com um shim de bootstrap **apenas de teste** (fora do
> repo) que resolve PEP 604 no 3.9. Nenhum arquivo do projeto foi alterado por isso.

## Escopo / invariantes

- **P04 NÃO foi implementado** (revalidação de preço e fallback MARKET maker).
- `MAKER_ENTRY_ENABLED=false`, `TF_UPGRADE_ENABLED=false`,
  `PYRAMIDING_ENABLED=false` — preservados.
- **Bybit live continua bloqueada.**
- Nenhuma entry MARKET adicional criada; nenhuma exchange real acessada.
- Nenhum push; nenhum deploy.
