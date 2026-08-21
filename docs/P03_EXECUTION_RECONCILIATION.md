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

Reutiliza as primitivas P02: `get_order` (por `clientOrderId`),
`_fresh_position_size`/`get_positions(force=True)`, `get_open_algo_orders`,
`cancel_order`, `cancel_algo_order`, `place_protection_orders`, o **latch**
owner-aware de `shadow_trade_service`, a pausa do `RiskState` e o `RealTrade`.

O que o P03 **faz** (P03.1): consulta ordens/posições; **cancela** maker viva e
condicionais **exatas** órfãs; **cria SOMENTE SL** sob invariantes estritas
(posição fresh aberta, lado conhecido, stop válido, qty confirmada, identidade)
reusando `place_protection_orders(symbol, entry_side, qty, stop_loss=…, tp1=None,
tp2=None, client_order_id_prefix=…, dedup_live=True)` — sucesso exige
`sl_ok is True` **e** `sl_order_id`. O que o P03 **nunca faz**: criar entry,
emitir MARKET (nem fallback), criar **TP** com qty incerta, tratar `UNKNOWN` como
`FLAT`, cancelar ordem de outro trade, ou liberar latch alheio (P02/operador).
Consome o formato **snake_case** real de `get_open_algo_orders`
(`algo_id`/`client_algo_id`/`reduce_only`/`close_position`/`quantity`/`side`/
`trigger_price`/`type`).

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

`RECONCILE_INTERVAL_S=30`, `RECONCILE_BACKOFF_BASE_S=15` (1º retry = 15s),
`RECONCILE_BACKOFF_MAX_S=900`, `RECONCILE_CLEAN_GRACE=2`, `RECONCILE_LEASE_S=120`,
`RECONCILE_MAX_ATTEMPTS=8`, `RECONCILE_MAX_PER_CYCLE=10`,
`RECONCILE_CREATE_SL=1`, `RECONCILE_STOP_TOL_FRAC=0.005` (trigger dentro de 0,5%).

## P03.1 — correções de segurança da reconciliação

Fecha as lacunas da auditoria P03:

- **Contrato SL real:** consome snake_case de `get_open_algo_orders`; adota SL só
  com lado de fechamento correto + (`close_position`) OU (`reduce_only` e
  cobertura) + trigger dentro da tolerância; senão **cria** SL sob invariantes.
- **Ownership de quarentena:** latch por owner — P03 arma/limpa só o próprio; não
  apaga latch de P02/legado nem pausa manual do operador; libera **só** na
  transição de ≥1 incidente para 0 (sem clear/log em ciclos comuns).
- **Boot fail-closed real:** falha de DB/leitura ou posições stale/rate-limited
  **arma** a quarentena P03 antes dos loops live (não só loga).
- **Cleanup por identidade:** cancela por IDs exatos (`algo_id`/`client_algo_id`/
  `<prefix>-sl/-tp1/-tp2`); exige `cancel.ok=True` + reconfirmação; sem identidade
  **não** resolve por "nenhum match"; reaparecimento reinicia o grace.
- **Maker viva:** NEW/PARTIALLY_FILLED é cancelada pelo ID, `cancel.ok` validado,
  re-consultada e exigida terminal; cancel incerto mantém quarentena; sem MARKET.
- **Fencing:** `claim` atômico + `update_claimed`/`renew_claim`/`release_claim`
  com owner+lease — processo com lease vencido não atualiza estado, não muta a
  exchange e não libera claim alheio.
- **Upsert atômico:** `INSERT … ON CONFLICT DO NOTHING` (Postgres); duas gravações
  → uma linha; lower-bound monotônico; falha de persistência mantém latch local.
- **Identidade/persistência:** kind correto por `safety_state`; persiste
  client/entry order IDs, prefixo, qty, lower-bound, side, stop, snapshot, flag
  maker; chave sem sufixo genérico `-`; incidente resolvido que reincide **reabre**.
- Correções menores: `_find_exact` reconhece `client_algo_id`; untracked usa
  `side` real; `cancel ok=False` não é sucesso; qty NaN/inf inválida; backoff
  inicial = 15s.

## API (somente leitura)

`GET /api/execution-incidents/status` — total aberto, retry pending, manual
required, protected, flat, quarantine active, itens resumidos, última
reconciliação e se o reconciliador está rodando. **Sem** execute/retry-now/
resolve/close/enable-live. Frontend inalterado neste P03.

## Testes

`backend/tests/test_p03_execution_reconciliation.py` — **46 testes** herméticos
(nenhuma rede/Binance/DB real; nenhuma entry criada). Suíte crítica completa
(P01+P02+P03.1) = **102 testes**, verde. `py_compile` e `git diff --check` verdes.

> Limitação declarada: sem Postgres descartável local, a atomicidade do upsert
> SQL (`ON CONFLICT`) e o fencing por `UPDATE…WHERE` são exercitados na
> implementação **em memória equivalente** (mesma semântica) — **não** é um teste
> Postgres real.
>
> Nota de ambiente: produção roda Python 3.11 (onde `Mapped[X | None]` resolve
> nativamente). O único interpretador com deps nesta máquina é o 3.9; as suítes
> rodaram com um shim de bootstrap **apenas de teste** (fora do repo) que resolve
> PEP 604 no 3.9. Nenhum arquivo do projeto foi alterado por isso.

## Escopo / invariantes

- **P04 NÃO foi implementado** (revalidação de preço e fallback MARKET maker).
- `MAKER_ENTRY_ENABLED=false`, `TF_UPGRADE_ENABLED=false`,
  `PYRAMIDING_ENABLED=false` — preservados.
- **Bybit live continua bloqueada.**
- Nenhuma entry MARKET adicional criada; nenhuma exchange real acessada.
- Nenhum push; nenhum deploy.
