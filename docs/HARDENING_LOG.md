# Hardening Log

## P01 - Baseline e testes de caracterização

- Status: Concluído
- Início real: 19/08/2026
- Branch: `codex/hardening-p01`
- Base: `feat/decouple-exec-universe` em `58531c1f`

### Mapa do fluxo crítico

| Responsabilidade | Arquivo principal | Observação de baseline |
|---|---|---|
| Geração, score e tier | `backend/services/recommendation_service.py` | V1/V2, gates de RR/volume/MTF e CT downgrade |
| Tendência e regimes | `backend/services/regime_service.py` | CT brake falha aberto e exige tendência HTF inequívoca |
| Sizing e orquestração | `backend/services/shadow_trade_service.py` | Cap de margem, teto duro de risco e mínimo de notional |
| Entrada e proteção | `backend/services/binance_signed_service.py` | Entrada e SL/TP são sequenciais; dedup existe nas proteções |
| Gestão pós-entrada | `backend/services/trade_manager_service.py` | TP1, BE estrutural, time-stop, auto-heal e runner |
| Resultado de snapshots | `backend/services/snapshot_service.py` | Usa R fixo por status para pesquisa e agregação histórica |
| Risco por snapshots | `backend/services/risk_service.py` | DD soma `realized_r * risk_pct` dos snapshots |
| Kill switch live | `backend/services/kill_switch_service.py` | Deriva limites do histórico de `RealTrade` |
| Calibração | `backend/services/calibration_service.py` | Shrinkage e isotônica sobre outcomes resolvidos |
| Rotação | `backend/services/rotation_service.py` | Promoção/demissão com histerese e seed de backtest |
| Backtest | `backend/services/backtest_service.py` e `recommendation_backtest.py` | Caminho separado do scan live; equivalência ainda não demonstrada |

### Caracterização adicionada

- sizing: cap duro de risco, cap de margem e rejeição do mínimo de notional;
- proteção: contrato SL + TP1 + TP2 e dedup de SL vivo;
- CT brake: contra tendência inequívoca e fail-open em MTF misto;
- snapshot: mapeamento fixo de status real para R de pesquisa;
- MTF sintético: somente timeframes estritamente superiores.

### Validação

- Comando: `python -m unittest backend.tests.test_hardening_characterization -v`
- Resultado: **9 testes aprovados**.
- Banco e exchange: não acessados; dependências externas foram simuladas.

### Lacunas observadas para os próximos pacotes

- Não há suíte pytest/unittest do backend de trading; existia apenas script manual de circuit breaker.
- Proteções são criadas depois da entrada e o contrato atual não fecha emergencialmente a posição se o SL falhar.
- Dedup de proteção é fail-open quando a leitura da exchange é incerta.
- `risk_service` ainda usa R teórico de snapshots; o `kill_switch_service` usa `RealTrade`, criando duas fontes de risco.
- A documentação chama campos `ema9/ema21`, mas o cálculo histórico usa 12/26.
- O backtest e o live não têm teste de paridade de decisão/execução.

### Decisão

P01 somente caracteriza o baseline. Nenhuma regra de negócio, ENV ou caminho live foi alterado.

## P02 - Stop fail-safe e rollback da entrada

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Início real: 19/08/2026
- Dependência: P01 concluído em `8b655fda`

### Implementação

- Estado pós-fill explícito: entrada, proteção e safety state retornados ao caller.
- SL só é confirmado quando a Binance retorna `algoId` não vazio.
- Falha do SL interrompe a criação de TP1/TP2.
- Exceção durante a proteção também aciona rollback.
- Fechamento emergencial MARKET usa lado oposto, `reduceOnly=true` e a quantidade realmente preenchida.
- Fill parcial fecha apenas o saldo restante em novas tentativas.
- Sucesso exige `positionRisk` fresco e zerado; cache stale/rate-limited nunca comprova fechamento.
- Timeout-com-sucesso é reconciliado antes de repetir a ordem.
- Toda MARKET recebe `clientOrderId`; submissão ambígua é consultada pelo ID e,
  enquanto continuar UNKNOWN, recebe somente tentativa de SL total, sem novo
  MARKET ou TP.
- Condicionais conhecidas e ordens com o mesmo prefixo são removidas somente após FLAT confirmado.
- Falha não confirmada pausa novas entradas em `RiskState`, dispara alertas e mantém um `RealTrade` aberto para visibilidade/auto-heal.
- Latch local mantém o processo em quarentena mesmo se a persistência da pausa falhar.
- Maker parcial cancela no primeiro fill observado, preserva o lower-bound de qty
  e só dimensiona proteção/rollback quando a qty terminal é comprovada.
- Leitura stale/cache nunca fecha trade no DB; flip, time-stop, expiração e poeira
  só encerram após posição fresh-flat e cleanup confirmado.
- Gestão live, backfill e trades managed ficam restritos à Binance neste pacote;
  mismatch/typo de exchange falha fechado.
- Incidentes sem SL aparecem como `SEM SL CONFIRMADO` na interface e ignoram a
  carência normal do auto-heal.

### Configurações novas

- `BINANCE_EMERGENCY_CLOSE_ATTEMPTS` (default `3`).
- `BINANCE_EMERGENCY_CLOSE_RETRY_DELAY` (default `0.35` segundo, backoff linear).
- `BINANCE_POST_FILL_PROTECTION_TIMEOUT_S` (default `8` segundos por fase: SL e TPs).
- `BINANCE_MAKER_CANCEL_CONFIRM_ATTEMPTS` (default `2`).
- `BINANCE_MAKER_CANCEL_CONFIRM_DELAY` (default `0.25` segundo).

Defaults fail-closed mantidos no P02:

- `MAKER_ENTRY_ENABLED=false` até reconciliação persistente do P03;
- `TF_UPGRADE_ENABLED=false`;
- `PYRAMIDING_ENABLED=false`.

### Validação

- **56 testes aprovados** cobrindo proteção normal, `algoId` ausente, SL
  rejeitado, exceção, timeout-com-sucesso, fill parcial, maker parcial, qty
  terminal desconhecida, stale position, cleanup, quarentena e cross-exchange.
- `py_compile` aprovado para o backend alterado, incluindo `backend/main.py`.
- Build de produção da interface aprovado (`tsc && vite build`).
- `git diff --check` aprovado.
- Banco e exchange reais não foram acessados; integrações foram simuladas.

### Riscos restantes fora do P02

- Reconciliação persistente/assíncrona de entry maker UNKNOWN e de conditionals
  que materializem muito depois do timeout: P03. O maker permanece OFF por default.
- Revalidação de preço e política de fallback MARKET maker: P04.
- Boot reconcile ainda deve transformar posição `untracked` em pausa persistente
  após restart; até o P03, o latch cobre o processo atual e o relatório acusa drift.
- Conta Binance em hedge mode não está suportada explicitamente; o fluxo presume one-way mode.
- Bybit live permanece bloqueada até receber o mesmo contrato transacional.

## P03 - Reconciliação persistente de execução

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Branch: `feat/hardening-p03`
- Base: `d6f05d4c` (P02)
- Detalhe: `docs/P03_EXECUTION_RECONCILIATION.md`

### Implementação

- Model `ExecutionIncident` (`execution_incidents`) com `incident_key` único
  (idempotência), identidade de ordem/condicionais, qty planejada, lower-bound de
  fill, attempts, clean observations, next_retry, last_error, lease/claim e
  timestamps. Integrado ao `init_db()` (bootstrap `create_all`).
- Serviço `execution_reconciliation_service.py`: entrada oficial de incidentes
  idempotente, boot reconcile (arma quarentena ANTES do scan, detecta posição
  untracked fail-closed), loop periódico integrado ao `lifespan` (sem
  worker/scheduler/queue paralelo), claim/lease persistente, backoff e a máquina
  de estados `OPEN→RECONCILING→PROTECTED|FLAT` + `RETRY_PENDING`/`MANUAL_REQUIRED`.
- Só CONSULTA a exchange e reutiliza as primitivas P02 de leitura/cancelamento
  (get_order por clientOrderId, positionRisk fresh, get_open_algo_orders,
  cancel_algo_order). NÃO coloca ordem nova: adota SL vivo válido ou escala
  MANUAL. Nunca reenvia entry, nunca emite MARKET cego, nunca `no_fill` sem prova.
- 3 pontos de entrada: safety state P02 inseguro/UNKNOWN, boot com incidente
  aberto, e falha de persistência (posição real sem `RealTrade`).
- Endpoint read-only `GET /api/execution-incidents/status`. Sem mutação.
- `UNKNOWN` nunca vira `FLAT`; `MANUAL_REQUIRED` continua bloqueando entradas;
  quarentena própria liberada só com prova positiva; pausa manual preservada.

### Configurações novas

- `RECONCILE_INTERVAL_S` (30), `RECONCILE_BACKOFF_BASE_S` (15),
  `RECONCILE_BACKOFF_MAX_S` (900), `RECONCILE_CLEAN_GRACE` (2),
  `RECONCILE_LEASE_S` (120), `RECONCILE_MAX_ATTEMPTS` (8),
  `RECONCILE_MAX_PER_CYCLE` (10). Defaults conservadores, fail-closed.

Defaults fail-closed preservados: `MAKER_ENTRY_ENABLED=false`,
`TF_UPGRADE_ENABLED=false`, `PYRAMIDING_ENABLED=false`; Bybit live bloqueada.

### Validação

- **37 testes P03** herméticos; suíte crítica completa (P01+P02+P03) = **93
  testes**, executada **2×**, verde.
- `py_compile` e `git diff --check` aprovados.
- Banco e exchange reais não foram acessados; tudo mockado. Nenhum push/deploy.
- Nota: rodado no Python 3.9 local via shim de teste (fora do repo) para PEP 604;
  produção é 3.11.

### Fora do escopo (P04)

- Revalidação de preço e política de fallback MARKET maker permanecem para o P04,
  **não implementado** aqui.

## P03.1 - Correções de segurança da reconciliação

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Branch: `feat/hardening-p03-1` (novo commit sobre `62e4150e`)
- Detalhe: `docs/P03_EXECUTION_RECONCILIATION.md` (seção P03.1)

### Fechou as lacunas da auditoria P03

- **Contrato SL real (snake_case):** adota SL vivo válido (lado/cobertura/trigger)
  e, sob invariantes estritas, CRIA SL via `place_protection_orders` (assinatura
  real; sucesso exige `sl_ok` + `sl_order_id`). Nunca TP com qty incerta.
- **Ownership de quarentena:** latch por owner — P03 arma/limpa só o próprio,
  não apaga latch P02/legado nem pausa manual; libera só na transição >0→0.
- **Boot fail-closed real:** falha DB/leitura ou posições stale armam a quarentena
  antes dos loops live (main.py + boot_reconcile + _detect_untracked).
- **Cleanup por identidade exata** (`algo_id`/`client_algo_id`/`<prefix>-sl…`),
  `cancel.ok` exigido + reconfirmação; sem identidade não resolve por ausência.
- **Maker viva** NEW/PARTIAL cancelada pelo ID, validada e re-consultada; sem MARKET.
- **Fencing** owner+lease (`update_claimed`/`renew_claim`/`release_claim`); lease
  vencido não atualiza estado nem muta exchange nem libera claim alheio.
- **Upsert atômico** `INSERT … ON CONFLICT`; incidente resolvido que reincide reabre.
- Menores: `_find_exact`+`client_algo_id`; untracked usa `side` real; `cancel
  ok=False` ≠ sucesso; qty NaN/inf inválida; backoff inicial 15s.

### Validação

- **46 testes P03.1** herméticos; suíte crítica (P01+P02+P03.1) = **102**, verde.
- `py_compile` e `git diff --check` aprovados. Nenhuma rede/exchange/DB real.
- Limitação: atomicidade do upsert SQL/fencing exercitada em memória (sem Postgres
  descartável) — não é teste Postgres real. Sem push/deploy.

## P03.1B - Final safety closure

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Branch: `feat/hardening-p03-1b` (novo commit sobre `80d95621`)
- Detalhe: `docs/P03_EXECUTION_RECONCILIATION.md` (seção P03.1B)

### Fechou os gaps da auditoria independente

- **Boot fail-closed total:** outer `except` do init_db arma latch P03 +
  `boot_scan_safe=False`; `_detect_untracked_positions` retorna status explícito
  (FLAT/UNTRACKED/UNKNOWN) e a liberação exige `boot_scan_safe=True` (leitura fresh
  bem-sucedida); loop periódico re-tenta o scan.
- **Ownership sem clear genérico:** `risk_service.release_p03_pause` (CAS, sem
  `clear_execution_quarantine()` genérico); latch limpo por owner `p03`; preserva
  owners P02/legacy e pausa manual; re-arma P03 a cada ciclo com incidente aberto.
- **Maker real:** reconhece `was_maker`/`pending_entry_order`/
  `final_fill_qty_unknown`; protege a exposição observada ANTES de cancelar; sem MARKET.
- **Qty terminal:** exige `executedQty` no `raw` (0.0 normalizado não vira FLAT).
- **SL:** side oposto presente, símbolo, status vivo, cobertura por
  max(qty_terminal, posição_fresh), booleanos normalizados, fallback clientAlgoId válido.
- **Tracking:** PROTECTED só com RealTrade correspondente (senão UNTRACKED/MANUAL);
  persiste `sl_order_id` idempotente; fresh-flat com condicional reconcilia cleanup.
- **Fencing/SQL:** `renew_claim` exige lease>now; upsert = `INSERT … ON CONFLICT
  DO UPDATE … RETURNING` (GREATEST, merge jsonb, COALESCE, reabertura atômica).

### Validação

- **67 testes P03.1B** herméticos; suíte crítica (P01+P02+P03.1B) = **123**, verde.
- `py_compile` e `git diff --check` aprovados. Nenhuma rede/exchange/DB real.
- Limitação: upsert SQL/fencing verificados em memória equivalente (sem Postgres
  descartável) — não é teste Postgres real. Sem push/deploy.

## P03.1C - Final reconciliation closure

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Branch: `feat/hardening-p03-1c` (novo commit sobre `67efe7d2`)
- Detalhe: `docs/P03_EXECUTION_RECONCILIATION.md` (seção P03.1C)

### Fechou os bloqueadores da auditoria do 67efe7d2

- Integração: hook passa `local_client_order_id`/`local_planned_qty` reais; maker
  ambígua (incl. GTX sem status / `ENTRY_ORDER_STILL_ACTIVE_OR_UNKNOWN`) reconhecida;
  `entry_order_terminal` impede falso-pending.
- Cleanup: REJECTED/terminal-zero com identidade → cleanup (não FLAT direto); OPEN
  preserva SL cardinal e cancela só extras exatos com reconfirmação.
- Tracking: match RealTrade determinístico (binance/auto|managed/símbolo+quote
  exato/lado/qty); BTCUSDC≠BTCUSDT; persistência de `sl_order_id` idempotente, sem
  conflito → sem PROTECTED.
- SL: só `STOP_MARKET` (rejeita trailing), símbolo exato, status vivo, cobertura
  por max(qty_terminal, posição_fresh), trigger 0,2%; `SL_NOT_CONFIRMED` honesto.
- Persistência: latch armado ANTES da persistência; `record_incident.persisted`;
  UNTRACKED não persistido → boot inseguro/UNKNOWN, sem release.
- CAS/ownership: `release_p03_pause` = `UPDATE…WHERE LIKE` sob `pg_advisory_xact_lock`
  (sem clear genérico); recuperação `0→0`; re-arm por ciclo.
- Upsert: união histórica `conditional_ids.all`; reabertura limpa claim/lease/manual.

### Validação

- **82 testes P03.1C** herméticos (rede/DNS bloqueados); suíte crítica
  (P01+P02+P03.1C) = **138**, verde, executada **2×**.
- **PostgreSQL runtime test = PASS**: `backend/tests/pg_runtime_check.sql` via
  `run_pg_runtime.sh` num cluster PG16 descartável (socket unix, sem rede) —
  ON CONFLICT/união/reabertura/claim/lease/renew/pausa-CAS. Nunca Railway/produção.
- `py_compile` e `git diff --check` aprovados. Sem push/deploy.

## P03.1D - Final transactional reconciliation closure

- Status: Concluído localmente (não publicado/não ativado em conta real)
- Branch: `feat/hardening-p03-1d` (novo commit sobre `3878ce59`)
- Detalhe: `docs/P03_EXECUTION_RECONCILIATION.md` (seção P03.1D)

### Fechou os bloqueadores transacionais

- **JSON/JSONB**: coluna `conditional_ids` é `JSON`; casts para `JSONB` antes de
  `->`/`||` e de volta para `JSON`. Validado em Postgres real com as expressões
  VERBATIM sobre a coluna `JSON`.
- **Acquire/release atômico**: `arm_p03_pause`/`release_p03_pause` por
  `pg_advisory_xact_lock`; release confirma zero incidentes não resolvidos NA
  transação. `record_incident` persiste a pausa ANTES de o incidente existir
  (invariante "incidente visível ⇒ pausa persistida").
- **Retomada manual owner-aware**: `set_manual_pause(False)` limpa só o owner
  `manual`; com incidente P03 aberto re-carimba a pausa como P03 (sem janela);
  preserva P02/legacy.
- **Fresh com lado**: `_fresh_position` (símbolo/lado/qty/qualidade); PROTECTED só
  com lado fresh == incidente; senão MANUAL.
- **Boot tracking**: `_real_trade_match` (matcher do reconciliador) em vez de símbolo.
- **RealTrade determinístico**: identidade + agregação de qty (sem 50% fixo); lado
  ausente → MANUAL; `sl_order_id` só no trade certo.
- **SL cardinal**: exige `planned_stop` válido; **lease por CADA mutação**.
- **Maker honesto**: sem default "protegida"; SL_NOT_CONFIRMED/UNKNOWN_NO_SL/
  PENDING_ORDER_UNPROTECTED; nunca MARKET.

### Validação

- **89 testes P03.1D** herméticos (rede/DNS bloqueados e CONTABILIZADOS — a suíte
  falha se houver qualquer tentativa). Rodados em **Python 3.9 PURO (sem shim)**,
  **2×**, verdes; + P02 edge (13) no 3.9 puro.
- **PostgreSQL SQL-expression runtime = PASS** (coluna `JSON` real, expressões
  verbatim). **Real-repo via driver async = `POSTGRES_RUNTIME_TEST=BLOCKED`** —
  ambiente sem `asyncpg`/`psycopg`; não instalado pela rede; NÃO afirmado como passou.
- Execução completa P01+P02+P03 (145) feita como AUXILIAR sob shim de teste externo
  (P01/P02 exigem Python ≥3.10; no 3.9 puro ficam BLOCKED). `py_compile` e
  `git diff --check` aprovados. Sem push/deploy.

---

## P03.1E — FINAL INVARIANT CLOSURE (2026-08-24)

Runtime REAL disponível pela primeira vez: **Python 3.11.15** (sem shim),
**SQLAlchemy 2.0.50 + asyncpg 0.31.0**, **PostgreSQL 16** local. O que em P03.1D
ficou `POSTGRES_RUNTIME_TEST=BLOCKED` (driver async ausente) agora é **PASS** com
código real, conexões distintas e concorrência real.

### Os 8 invariantes — fechamento

1. **Incidente aberto ⇒ pausa persistida (transação única).**
   `record_incident` → `persist_incident_with_p03_pause`: 1 `AsyncSession`,
   `BEGIN` → `pg_advisory_xact_lock(917283)` → `ensure_p03_pause_in_session` →
   upsert do incidente → **COMMIT ÚNICO**. `asyncio.Lock` local serializa o mesmo
   processo; advisory lock serializa entre processos. Provado em PG real com
   **trigger `BEFORE INSERT` + `pg_sleep(0.6)`** e um `release_p03_pause`
   concorrente em OUTRA conexão → resultado `STILL_OPEN` (nunca `RELEASED`) e
   `trading_paused=true`.
2. **UNKNOWN nunca vira FLAT/PROTECTED.** `_fresh_position` retorna
   `quality=UNKNOWN` em stale/rate-limit/erro; `_resolve_protected` com cobertura
   `UNKNOWN` agenda RETRY (não protege). Boot com posições stale → `UNKNOWN` +
   quarentena.
3. **Nenhuma mutação antes de validar posição e lado.** As 4 chamadas mutáveis
   (`place_protection_orders`, `cancel_order`, 2× `cancel_algo_order`) são
   precedidas por `_renew_or_abort` (lease). Lado fresh vs incidente conferido.
4. **PROTECTED exige SL cardinal + RealTrade DETERMINÍSTICO.** Novo
   `_coverage_verdict` (5 estados: COVERED/INSUFFICIENT/NO_MATCH/AMBIGUOUS/UNKNOWN)
   por **agregação `Decimal` + tolerância = `stepSize`** (removidos 0,1%/50%).
   **BUG corrigido:** RealTrade ambíguo (`target_id=None`) resolvia PROTECTED
   silenciosamente — agora → MANUAL_REQUIRED. `stepSize` lido do cache de
   `exchangeInfo` (leitura pura, sem rede).
5. **Boot exige cobertura INTEGRAL.** `any(_real_trade_match)` (match parcial
   contava como rastreado) → `_boot_coverage_ok` (agregação `Decimal` ≥ posição
   fresh, tolerância `stepSize`). Cobertura parcial → UNTRACKED_POSITION.
6. **Cada POST interno exige lease válido.** Novo argumento
   `mutation_guard: Callable[[],Awaitable[bool]]` em `place_protection_orders`,
   invocado em `_place_algo` **antes de CADA POST** (tentativa, retry e fallback).
   Negar/lançar ⇒ **nenhum POST** (fail-closed, `submission_unknown=False`). P03
   passa `mutation_guard=_renew_or_abort`.
7. **JSON permanece objeto, nunca array/null.** `JSON(none_as_null=True)` em
   `conditional_ids`/`payload` (Python `None` → SQL `NULL`, nunca JSON `'null'`);
   guardas `jsonb_typeof`/`NULLIF` no upsert; união de IDs preservada
   (`jsonb_agg(DISTINCT ...)`), `GREATEST` monotônico. Provado em PG real.
8. **Owner P03 nunca sofre auto-resume.** `update_and_check` tinha DOIS pontos de
   auto-resume (virada de semana e de dia) gated só por `not pause_manual` — como
   a pausa P03 tem `pause_manual=false`, **ambos soltavam a quarentena** no
   rollover. `_is_p03_pause(state)` agora bloqueia os dois. Provado em PG real
   (RED→GREEN): pausa P03 sobrevive à virada de dia+semana com DD saudável.

### Testes (RED→GREEN, real 3.11, sem shim)

- **Integração PostgreSQL REAL** (`tests/pg_integration_p03.py` via
  `tests/run_pg_runtime.sh`): cluster PG16 **descartável, socket unix**
  (`listen_addresses=''`), **código real** (`record_incident`/`_SqlIncidentRepo`/
  `risk_service`). **9 cenários** incl. concorrência 2-conexões com trigger+sleep,
  reabertura, união JSON tri-state, rollback sem parcial, record-vs-resume,
  sobrevivência ao auto-resume. **Hermeticidade provada**: guarda de socket
  permite só **AF_UNIX** e bloqueia/conta **AF_INET/AF_INET6/DNS** → zero TCP.
  `PG_INTEGRATION_OK`, rodado **2×**.
- **Unittest** (venv311, herméticos): **161** testes (P03=101, P02 edge=16,
  P02 trade-manager, hardening) verdes **2×**. Novos: `MutationGuardEdgeTests`
  (guard antes de cada POST/fallback), `CoverageVerdictTests` (5 estados +
  Decimal + stepSize), boot cobertura parcial→untracked / agregada→tracked,
  ambíguo→MANUAL.
- `py_compile` e `git diff --check` aprovados. Sem push/deploy.

---

## P03.1E-FIX — VERIFIED CLOSURE OF SAFETY RACES (2026-08-24)

Base: commit `5904b963`. Reproduzido em runtime real (Python 3.11 + asyncpg + PG16)
o bug de PRODUÇÃO `open_incidents=1 / trading_paused=false` e fechadas as corridas
e brechas remanescentes do caminho PROTECTED.

### Corrida de RiskState (bug de produção)
- **`update_and_check` agora adquire `pg_advisory_xact_lock(917283)` ANTES da 1ª
  leitura** de RiskState (os outros writers — `set_manual_pause`, `arm_p03_pause`,
  `release_p03_pause`, `persist_incident_with_p03_pause` — já adquiriam). Conta os
  incidentes abertos NA MESMA txn: `>0` ⇒ força `trading_paused=true` (via
  `ensure_p03_pause_in_session`, que preserva pausa manual/P02/DD) e **bloqueia
  auto-resume diário e semanal** (`_no_incident = open==0`). Prova em PG real, os
  dois sentidos (`update||record` e `record||update`) ⇒ `open=1, paused=true`; e o
  caso determinístico "pausa DD pré-existente + incidente + rollover ⇒ permanece
  pausado".
- **`_arm_quarantine` passou a compartilhar o MESMO `_P03_LOCAL_LOCK`** de
  `record_incident`/`_maybe_release_quarantine` — nenhum interleaving arm↔release
  deixa pausa persistida com o owner local P03 desligado.

### Caminho PROTECTED (nunca "protegido no escuro")
- **Portão fresh ANTES de qualquer mutação** (`_fresh_gate`, sobre `_fresh_position`
  com quality/size/**side**): só muta com FRESH + size>0 + lado fresh == lado do
  incidente. UNKNOWN → RETRY; FLAT → cleanup/FLAT; lado ausente/divergente → MANUAL,
  **zero mutações** (nem SL, nem cancel). Substitui o `_fresh_verdict` (removido) nos
  fluxos maker, entry e cleanup. `LONG × fresh SHORT` e `maker BUY × SHORT` ⇒ zero
  mutações.
- **Parsing de lado explícito** (`_explicit_side`): buy/long | sell/short; ausente/
  inválido/ambíguo → None. **Nunca `else "buy"`** (removido em `_fresh_position` e no
  boot).
- **`_resolve_protected` reescrito**: (0) grava o `sl_id` no incidente
  (`conditional_ids.sl` + união `.all`, fenced) ANTES de resolver; (1) **SEGUNDA
  leitura fresh** independente (FRESH/size>0/lado==incidente; senão nunca PROTECTED);
  (2) **RELISTA e revalida o SL pelo id EXATO** (`_revalidate_stop_by_id`: status vivo
  por allowlist, STOP_MARKET, lado de fechamento, símbolo/quote, cobertura, trigger) —
  SL ausente pós-POST ⇒ nunca PROTECTED; (3) RealTrade DETERMINÍSTICO com a qty da 2ª
  leitura; (4) CAS do `sl_order_id`. **Removido o atalho `skip`→PROTECTED**: DB off/
  erro ⇒ RETRY, jamais PROTECTED.
- **Allowlist de status vivo do SL** (`_LIVE_ALGO_STATUS`): ausente/`MYSTERY`/
  não-reconhecido ⇒ **rejeita** (era denylist, que adotava o desconhecido).
- **Identidade**: incidente com identidade A e só RealTrade B ⇒ **NO_MATCH** (nunca
  fallback pro único item do pool).
- **Boot**: posição com lado ausente/ambíguo ⇒ UNKNOWN + `_boot_scan_safe=false` +
  quarentena (não infere BUY, não retorna FLAT). Cobertura agregada íntegra preservada.
- **mutation_guard preservado** (guard antes de cada POST/retry/fallback; lease negado/
  exceção ⇒ zero POST; cada cancel renova lease) — sem regressão.

### Validação (RED→GREEN, Python 3.11 real, sem shim)
- **10 testes novos** que FALHAM em `5904b963` (provado por stash do serviço, 9/10
  RED) e passam agora: 2ª leitura UNKNOWN, LONG×SHORT/maker×SHORT zero-mutação, DB
  off nunca PROTECTED, SL persistido no incidente, SL ausente pós-POST, status
  MYSTERY rejeitado, lado ausente → None, identidade A+B → NO_MATCH, boot lado
  ausente → quarentena.
- **Integração PostgreSQL REAL: 12 cenários** (incl. produção + `update||record` /
  `record||update` / rollover), `PG_INTEGRATION_OK` **2×**, socket unix, hermeticidade
  AF_UNIX-only (zero TCP/DNS). **Unittest herméticos 171 verdes 2×**. `py_compile` e
  `git diff --check` OK. Sem push/deploy.
