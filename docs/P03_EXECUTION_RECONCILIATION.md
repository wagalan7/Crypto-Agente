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

## P03.1B — final safety closure

Fecha os gaps não cobertos por testes na auditoria independente:

- **Boot fail-closed total:** o outer `except` do `init_db` no lifespan também
  arma o latch P03 e marca `boot_scan_safe=False`. Qualquer falha (init_db,
  boot_reconcile, leitura de incidentes/posições, ciclo inicial) bloqueia
  exposição. `_detect_untracked_positions` retorna status EXPLÍCITO
  (`FLAT`/`UNTRACKED`/`UNKNOWN`) — nunca o mesmo `0` para flat vs erro/stale — e a
  liberação exige `boot_scan_safe=True` (uma leitura fresh bem-sucedida); o loop
  periódico re-tenta o scan até ficar seguro.
- **Ownership sem clear genérico:** o P03 nunca chama `set_manual_pause(False)`
  (que apaga todos os owners). Novo `risk_service.release_p03_pause(marker)` faz
  CAS transacional (só remove se a pausa ATUAL ainda é P03) e o latch é limpo por
  owner `p03`; se restam owners P02/legacy, a pausa persistida permanece. Enquanto
  houver incidente aberto, cada ciclo re-arma o owner P03 (mesmo após resume
  indevido).
- **Contrato maker real:** reconhece `was_maker`/`pending_entry_order`/
  `final_fill_qty_unknown`. Maker NEW/PARTIALLY_FILLED: persiste lower-bound,
  consulta posição fresh, **protege a exposição observada ANTES** de cancelar,
  cancela pelo ID, valida `cancel.ok`, re-consulta e exige terminal; cancel
  incerto/não-terminal mantém quarentena com a exposição protegida; sem MARKET.
- **Qty terminal:** `CANCELED/EXPIRED/EXPIRED_IN_MATCH` exigem `executedQty`
  EXPLÍCITA no `raw`; o `0.0` da normalização não vira FLAT.
- **SL:** adota só com side do incidente conhecido, side da ordem presente e
  exatamente oposto, símbolo, status vivo, tipo STOP, `algo_id`, trigger na
  tolerância e cobertura por `close_position` OU `reduce_only`+`quantity ≥
  max(qty_terminal, posição_fresh)`. Booleanos normalizados (`"false"`≠True).
  Fallback de `clientAlgoId` só `[A-Za-z0-9-]` (sem `…`), curto.
- **Tracking:** só declara `PROTECTED` com `RealTrade` aberto correspondente;
  protegido sem RealTrade → `MANUAL_REQUIRED`/`UNTRACKED_POSITION` (não inventa
  fechamento, não abre nada). SL adotado/criado persiste `sl_order_id` idempotente
  no RealTrade. Entrada fresh-flat com identidade de condicional reconcilia o
  cleanup antes de ir a FLAT.
- **Fencing/SQL:** `renew_claim` exige `lease_expires_at > now` (lease vencido não
  ressuscita). `upsert` é uma única op `INSERT … ON CONFLICT DO UPDATE … RETURNING`
  (GREATEST do lower-bound, merge jsonb de conditional_ids/payload, COALESCE de
  IDs/stop/qty, reabertura atômica). Merge nunca reduz informação.

## P03.1C — final reconciliation closure

Fecha os bloqueadores de concorrência/contrato e valida o SQL em Postgres real:

- **Integração:** o hook passa as vars LOCAIS reais (`local_client_order_id`,
  `local_planned_qty`), não o retorno da exchange. Maker pendente reconhecida por
  `was_maker` + (`pending_entry_order`/status `NEW|PARTIALLY_FILLED`/
  `ENTRY_SUBMISSION_UNKNOWN`/`ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN`/GTX ambígua sem
  `status`); `entry_order_terminal=true` impede falso-pending.
- **Cleanup FLAT/OPEN:** REJECTED/terminal-zero com identidade condicional NÃO vai
  direto a FLAT (confirma fresh-flat + cancela IDs exatos + grace). OPEN garante um
  SL cardinal e cancela SÓ extras exatos do incidente (nunca o cardinal nem ordem
  alheia), com `cancel.ok` + reconfirmação antes de PROTECTED.
- **Tracking RealTrade:** match determinístico (`status=open`, `exchange=binance`,
  `source in auto/managed`, símbolo/quote EXATO — `BTCUSDC≠BTCUSDT`, mesmo lado,
  qty compatível). Sem match → `UNTRACKED_POSITION`/`MANUAL`. Persistência do
  `sl_order_id` idempotente e só no RealTrade certo; conflito/falha → não PROTECTED.
- **Validação SL:** só `STOP_MARKET` EXATO (rejeita `TRAILING_STOP_MARKET`),
  símbolo/quote presente e exato, status vivo, `algo_id`, side oposto, cobertura
  por `close_position` OU `reduce_only`+qty ≥ max(qty_terminal, posição_fresh),
  trigger dentro de **0,2%**; booleanos normalizados. Falha real → `SL_NOT_CONFIRMED`
  (nunca afirma "protegida").
- **Persistência fail-closed:** latch local armado ANTES da persistência;
  `record_incident` retorna `persisted`. `UNTRACKED` não persistido → boot inseguro,
  scan `UNKNOWN`, sem release, tenta de novo.
- **CAS/ownership:** `release_p03_pause` é `UPDATE … WHERE … LIKE 'P03-QUARANTINE:%'`
  sob `pg_advisory_xact_lock` (sem SELECT+commit, sem clear genérico). Preserva
  pausa manual/P02/legacy; owner P03 re-armado a cada ciclo com incidente aberto.
  Recuperação `0→0`: boot inseguro→seguro com zero incidentes libera a pausa P03.
- **Upsert/reabertura:** `conditional_ids` mantém união HISTÓRICA (perna atual +
  lista `all` deduplicada — `{sl:S1}`+`{sl:S2}` preserva ambos). Reabertura limpa
  claim/lease/manual_reason/backoff (elegível imediatamente).

## P03.1D — final transactional reconciliation closure

Fecha os bloqueadores transacionais confirmados na P03.1C:

- **JSON/JSONB real:** a coluna `conditional_ids` é `JSON`. O merge castava mal
  (`json ? / json || jsonb`). Agora casta os operandos para `JSONB` ANTES de
  `->`/`->>`/`||` e converte o resultado de volta para `JSON` — validado em
  Postgres 16 real com as EXPRESSÕES VERBATIM do serviço sobre a coluna `JSON`.
- **Invariante "incidente visível ⇒ pausa P03 persistida":** `record_incident`
  arma o latch local, **persiste a pausa P03** (`arm_p03_pause`, `UPDATE` condicional
  sob `pg_advisory_xact_lock`, sem `get_status→set_manual_pause`) e SÓ ENTÃO faz o
  upsert do incidente. `release_p03_pause` confirma **zero incidentes não resolvidos
  dentro da mesma transação/lock** antes de liberar.
- **Retomada manual owner-aware:** `set_manual_pause(False)` não usa mais o
  `clear_execution_quarantine()` genérico — limpa só o owner `manual` e, se houver
  incidente P03 aberto, re-carimba a pausa como P03 (sem janela; trading segue
  pausado). Preserva P02/legacy.
- **Posição fresh com lado:** `_fresh_position` retorna símbolo/quote, LADO real,
  qty absoluta e qualidade `FRESH|UNKNOWN`. PROTECTED exige lado fresh == lado do
  incidente; ausente/divergente → `MANUAL_REQUIRED`.
- **Boot tracking = matcher do reconciliador:** removido `_open_real_trade_symbols`;
  o boot usa `_real_trade_match` (exchange/source/símbolo/quote/lado) sobre as linhas
  RealTrade — símbolo não é mais prova de tracking.
- **RealTrade determinístico:** `_match_real_trade` prefere IDENTIDADE (client/
  exchange order id), agrega qty de múltiplos trades para cobrir a posição fresh
  (tolerância só de lote ~0,1%, sem 50% fixo), lado ausente → MANUAL, e persiste o
  `sl_order_id` só no trade determinístico (conflito/falha → não PROTECTED).
- **SL cardinal:** sem `planned_stop` numérico > 0 não adota NEM cria stop.
- **Lease por mutação:** renova/valida o lease imediatamente antes de CADA
  `cancel_order`/`cancel_algo_order`/criação de SL; se expirar após A1, A2 não roda.
- **Maker honesto:** sem `_prot_ok=True` default — só afirma "exposição protegida"
  após `_ensure_stop` confirmar SL; senão `SL_NOT_CONFIRMED`/`UNKNOWN_NO_SL`/
  `PENDING_ORDER_UNPROTECTED`. Nunca fallback MARKET.

## API (somente leitura)

`GET /api/execution-incidents/status` — total aberto, retry pending, manual
required, protected, flat, quarantine active, itens resumidos, última
reconciliação e se o reconciliador está rodando. **Sem** execute/retry-now/
resolve/close/enable-live. Frontend inalterado neste P03.

## Testes

`backend/tests/test_p03_execution_reconciliation.py` — **82 testes** herméticos,
com **rede/DNS bloqueados durante toda a suíte** (qualquer tentativa de socket
falha o teste; zero chamadas a `demo-fapi.binance.com`). Suíte crítica completa
(P01+P02+P03.1C) = **138 testes**, verde (executada 2×). `py_compile` e
`git diff --check` verdes.

**PostgreSQL runtime (P03.1D):**

- **SQL-expression runtime = PASS.** `backend/tests/pg_runtime_check.sql` (via
  `run_pg_runtime.sh`) roda as **expressões VERBATIM do serviço** (incl. o merge
  `conditional_ids` JSON/JSONB corrigido) sobre a coluna **`JSON` real** num cluster
  PostgreSQL 16 descartável (socket unix, sem rede, nunca Railway): `ON CONFLICT DO
  UPDATE` (união `all`/GREATEST), reabertura atômica (limpa claim/lease/manual),
  claim/lease/renew e pausa CAS sob advisory lock.
- **Real-repo runtime (`_SqlIncidentRepo.upsert` via driver async) = `POSTGRES_RUNTIME_TEST=BLOCKED`.**
  O ambiente NÃO tem `asyncpg`/`psycopg` e, por política, **não** se instala pela
  rede — então o caminho pelo engine async real do projeto **não foi executado** e
  **não** é afirmado como passando. A validação acima cobre o CONTRATO SQL exato.

**Hermeticidade:** a suíte P03 bloqueia socket/DNS e **contabiliza** qualquer
tentativa — `tearDownModule` FALHA se o código sob teste tocar a rede (mesmo
capturada). Zero chamadas a `demo-fapi.binance.com`.

**Reprodutibilidade (sem shim externo):** os **89 testes P03** rodam em **Python
3.9 puro** (interpretador do sistema), executados **2×**, verdes; + P02 edge (13)
no 3.9 puro. As suítes P01/P02 que importam models (`Mapped[X | None]`, PEP 604)
exigem **Python ≥3.10** (produção é 3.11) — no 3.9 puro ficam **BLOCKED**; a
execução completa (145) foi feita como AUXILIAR sob um shim de teste externo (não
contado como reprodução sem shim).
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

---

## Atualização P03.1E (2026-08-24) — runtime PostgreSQL real

Com **Python 3.11 real + asyncpg + PostgreSQL 16**, o item antes BLOCKED
(“real-repo via driver async”) passou a **PASS**. Mudanças de invariante:

- **Transação única pausa+incidente** (`persist_incident_with_p03_pause`): um só
  `BEGIN` com `pg_advisory_xact_lock(917283)`, pausa P03 e upsert do incidente no
  **mesmo COMMIT**; `asyncio.Lock` local + advisory lock cobrem intra e
  inter-processo. Provado com trigger `BEFORE INSERT`+`pg_sleep` e `release`
  concorrente (→ `STILL_OPEN`).
- **`release_p03_pause` estruturado**: `RELEASED` / `SAFE_OTHER_OWNER` /
  `STILL_OPEN` / `ERROR`, contando incidentes não resolvidos NA transação;
  `_maybe_release_quarantine` chama o release NO BANCO primeiro e só então limpa o
  latch local do owner p03.
- **Cobertura RealTrade tipada** (`_coverage_verdict`): COVERED / INSUFFICIENT /
  NO_MATCH / AMBIGUOUS / UNKNOWN, por **agregação `Decimal` + tolerância
  `stepSize`** (sem 0,1%/50%). **Ambíguo (`target_id=None`) nunca vira PROTECTED**
  → MANUAL_REQUIRED. UNKNOWN → RETRY (nunca “seguro por omissão”).
- **Boot com cobertura integral** (`_boot_coverage_ok`): substitui
  `any(_real_trade_match)`; posição só é rastreada se o agregado das qty cobre o
  tamanho fresh (tolerância `stepSize`). Cobertura parcial → UNTRACKED_POSITION.
- **`mutation_guard` por POST**: `place_protection_orders(..., mutation_guard=…)`
  revalida o lease em `_place_algo` **antes de cada POST/retry/fallback**; negar/
  lançar ⇒ nenhum POST (fail-closed). P03 injeta `_renew_or_abort`.
- **Auto-resume nunca solta P03**: `_is_p03_pause` bloqueia os dois pontos de
  auto-resume (virada de dia/semana) para pausas de owner P03.
- **`JSON(none_as_null=True)`** em `conditional_ids`/`payload`: `None` → SQL
  `NULL` (nunca JSON `'null'`); normalização `jsonb_typeof`/`NULLIF` no upsert.

**Validação:** integração PostgreSQL REAL (9 cenários, `PG_INTEGRATION_OK`, 2×) em
cluster PG16 descartável por **socket unix**, com guarda de hermeticidade que só
permite **AF_UNIX** e bloqueia/conta **AF_INET/AF_INET6/DNS** (zero TCP). Unittest
herméticos **161** verdes 2× no venv311. `py_compile` e `git diff --check` OK.
Sem push/deploy. **P04 continua NÃO implementado**; flags MAKER/TF/PYRAMIDING e
Bybit live preservados.
