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

### Fechamento cirúrgico dos três casos adversariais finais

- Maker com posição `UNKNOWN` agora retorna `RETRY_PENDING` antes de qualquer
  cancelamento ou criação de proteção.
- Todo SL criado/adotado é persistido imediatamente em `conditional_ids.sl/all`
  dentro de `_ensure_stop`, antes de cancelamentos, cleanup ou qualquer retorno.
- Se a segunda leitura pós-SL confirmar `FLAT`, o incidente segue para o cleanup
  exato da condicional e permanece aberto durante o grace; não resolve mais `FLAT`
  deixando SL vivo.
- Quatro regressões novas elevam a suíte crítica para **175 testes, verdes 2×**;
  os 12 cenários PostgreSQL reais também passaram **2×**.

---

## P04A — REVALIDAÇÃO FAIL-CLOSED PRÉ-MAKER (2026-08-25)

Base: `57938e87` (`origin/main`, P03 final). Implementação isolada em
`codex/hardening-p04a`, sem push/deploy. Detalhes em
`docs/P04_ENTRY_REVALIDATION.md`.

### Implementado

- Cotação Binance `bookTicker` sem cache, na mesma BASE/modo/proxy do executor,
  compartilhando rate gate e cooldown 418/429. O header de peso deste endpoint,
  documentado como inexato, não contamina o throttle global.
- Avaliador puro/Decimal para venue/símbolo, bid/ask/qty, freshness local e da
  exchange, latência, spread, chase/slippage LONG/SHORT, GTX não-cruzada, zona,
  níveis, R:R TP1/TP2 e qty que nunca aumenta.
- Callback executado no helper maker depois de arredondar preço/qty e confirmar
  leverage, imediatamente antes do primeiro POST de entrada. Depois da quote,
  revalida RiskState, kill switch e quarentena; o POST é o próximo `await` quando
  aprovado. UNKNOWN/exceção/veto retorna `entry_not_submitted` e produz zero POST
  de entrada.
- Reason codes registrados no `_record_skip` existente; nenhuma tabela/API/UI.
- `MAKER_ENTRY_ENABLED=false` preservado. `MAKER_FALLBACK_MARKET` passou a default
  false e o caller força zero fallback até a P04B.
- Maker ligado sem helper maker agora bloqueia, sem downgrade para MARKET. A qty
  realmente submetida acompanha respostas ambíguas e alimenta o incidente P03.

### Validação

- Testes P04A herméticos, com DNS/TCP bloqueados e contabilizados: LONG/SHORT,
  favorável/chase, freshness, NaN/infinito, venue/símbolo, spread/slippage/R:R
  nos limites, qty monotônica, runtime gates, rate-limit, exceção e zero POST de
  entrada/zero MARKET.
- **26 testes P04A** verdes. Suíte crítica P01+P02+P03+P04A: **201 testes,
  verdes 2×**, no Python 3.11 real.
- `py_compile` e `git diff --check` aprovados; revisão independente sem
  bloqueadores.

### Fora do escopo

- P04B (MARKET direto/fallback, depth/slippage por qty e freshness contextual);
  nenhuma estratégia/score/IA; nenhuma ativação de maker/live; nenhum push/deploy.

---

## P04B — DEPTH/VWAP FAIL-CLOSED PRÉ-MARKET (2026-08-26)

Base: P04A (`b4027e41`). Detalhes em
`docs/P04B_MARKET_DEPTH_REVALIDATION.md`.

### Implementado

- Toda abertura MARKET normal Binance exige callback P04B dentro do transporte,
  depois do throttle e imediatamente antes do POST. Sem callback, zero POST.
- `GET /fapi/v1/depth` sem cache, na mesma BASE e rate gate do executor. O núcleo
  puro/Decimal exige identidade, freshness, book válido e cobertura de 100% da
  qty; calcula melhor preço, VWAP, pior nível, spread e impacto LONG/SHORT.
- O pior preço revalida slippage, chase, zona, stop/alvos e R:R. A qty pode apenas
  diminuir por risco/notional, é truncada no MARKET step e validada novamente
  contra min/max qty e min notional dentro do transporte.
- `submitted_qty` e o client ID efetivo chegam à proteção, RealTrade/P03 e aos
  estados ambíguos. Timeout após cap nunca volta à qty original.
- Fallback maker→MARKET permanece duplamente OFF. Quando explicitamente habilitado,
  só rejeição GTX comprovada pode usá-lo; timeout/ambiguidade nunca duplica entry.
  O fallback possui COID distinto e usa o mesmo depth guard.
- Saídas `reduceOnly` continuam independentes do depth. Caminhos não integrados
  (teste administrativo, pyramiding/hedge e exchange sem depth) falham fechado.
- Nenhuma estratégia, score, IA, indicador, funding/OI ou parâmetro de sinal foi
  alterado; nenhuma capability LIVE foi ligada.

### Validação

- **24 testes P04B herméticos**, incluindo depth multi-level LONG/SHORT, VWAP/worst,
  cobertura, malformed/NaN, freshness, limites, filtros, qty assinada, timeout,
  reduceOnly, fallback/COID e propagação P03; DNS/TCP bloqueados.
- Suíte crítica P01+P02+P03+P04A+P04B: **225 testes verdes 2×** no Python 3.11.
- `py_compile` e `git diff --check` aprovados. Sem exchange/banco real, push ou
  deploy.

---

## P04C — VALIDADE CENTRAL DOS DADOS (2026-08-26)

Detalhes em `docs/P04C_DATA_FRESHNESS.md`.

- Velas abertas são removidas antes de indicadores/padrões; schema, valores,
  ordem, duplicidade, lacunas, futuro e staleness falham fechado.
- A recomendação preserva prova de símbolo/timeframe/fonte/idade para candle,
  ticker, derivativos e cada contexto MTF realmente usado.
- Regime passa a distinguir `FRESH/DEGRADED/UNKNOWN/DISABLED`; ausência do dado
  essencial BTC 24h nunca é apresentada ao gate LIVE como contexto seguro.
- O gate central roda antes de toda entrada automática LIVE e antes de P04A/P04B.
  Flag OFF, exceção, identidade divergente ou contexto vencido = skip explicável,
  zero ordem. Shadow não é bloqueado.
- **18 testes P04C verdes**, `py_compile` aprovado e nenhuma rede/exchange/DB
  acessada. Estratégia, score, IA, sizing, SL/TP e capabilities permanecem
  inalterados/desligados conforme a baseline.

---

## P05 — OTIMIZAÇÃO GOVERNADA POR EVIDÊNCIA (2026-08-27)

Detalhes em `docs/P05_EVIDENCE_GOVERNED_STRATEGY.md`.

**ANTES:** métricas, calibração e learning já existiam, mas não havia um ciclo de
vida único e governado de candidato até a decisão — sem identidade versionada,
sem validação temporal formal e sem critério de aceite explícito.

**DEPOIS:** `dados → diagnóstico → candidato versionado → teste temporal →
shadow → ELIGIBLE/REJECTED → recomendação de ativação MANUAL`.

- **Três fontes separadas**: REAL (`RealTrade.source=auto`, janela por
  `closed_at`), SHADOW (`RecommendationSnapshot`, janela por `outcome_at`, reusa
  `_not_fast_void()`) e BACKTEST (`BacktestTrade`, evidência secundária). OPEN,
  `realized_r=None`, NaN/infinito e duplicatas são excluídos **e contabilizados**;
  breakeven e expired aparecem separados de win/loss.
- **`pnl_usd` auditado**: já é LÍQUIDO (`close_trade` desconta entry/exit fee) —
  reportado como líquido, fees só informativas, **sem double-count**.
- **Métricas com `None` + motivo** quando não calculáveis: Wilson 95%,
  expectancy/mediana/soma/desvio, downside deviation, profit factor, Sharpe por
  trade (**não anualizado**), max drawdown, pior streak, slippage e cobertura.
  IC de expectancy/delta por **bootstrap com seed fixa** (determinístico).
- **Segmentos** (tier, TF, direção, base, padrão, sessão, regime, score, ATR,
  MTF…) com rótulo de confiabilidade `INSUFFICIENT/EARLY/USABLE/STRONG`,
  ordenados por evidência — segmento pequeno não vira edge comprovado.
- **Funil corrigido**: `executados + skips` NÃO é "candidatos únicos".
  `skip_reason_stats` guarda EVENTOS por (gate, dia); o campo virou `gate_events`
  e `candidates_estimated` está marcado como estimativa. P04A/P04B abortam no
  POST e não têm contador — reportado como `null` + motivo, sem inventar número.
  Bloqueio **não** é chamado de lucro perdido.
- **`features["p05_context"]`**: namespace versionado dentro do JSONB existente
  (sem alterar schema), só com dado disponível no momento do snapshot; ausente
  permanece `None`. **MAE/MFE = `UNAVAILABLE`** (não é reconstruível com fidelidade).
- **Candidatos**: máx. 12, **um knob cada**, sem grid search, allowlist restrita,
  limites conservadores; exige valor champion descoberto + cobertura ≥80%.
  Qualquer tentativa de tocar P04A/B/C, stop/TP, qty/leverage/exposição, kill
  switch, portfolio guard, maker/fallback, LIVE ou `LEARNING_AUTO_*` é rejeitada.
- **Sem leakage**: candidatos/componentes nascem no treino, a validação escolhe
  no máximo um finalista por objetivo e só ele abre o **teste intocado**;
  4 ou 6 folds por tamanho, mínimo de 30 outcomes OOS. Champion e candidato
  comparados sobre o MESMO dataset, UNKNOWN excluído dos dois lados.
- **Champion × Challenger prospectivo**: o pipeline real anota somente snapshots
  criados após o start e com hashes exatos; challenger é puramente contrafactual
  (`features["p05_experiment"]`, idempotente) — não altera
  score/tier, não bloqueia rec, não abre trade. `UNKNOWN` nunca vira ELIGIBLE nem
  BLOCKED. Só **um** SHADOW ativo, garantido por advisory lock + índice parcial
  único. Champion, safety P01–P04 e ausência de incidente P03 são congelados e
  revalidados; UNKNOWN/drift/incidente falham fechado.
- **Decisão**: `DRAFT → INSUFFICIENT_DATA|REJECTED|OFFLINE_VALIDATED →
  SHADOW → REJECTED|ELIGIBLE`, sem saltos, sem reabrir decidido, status+métricas
  em transação única. `ELIGIBLE` = pode ser APRESENTADO ao usuário; **não** foi
  ativado. Não existe endpoint de promote/apply/activate-live.
- **1 tabela nova** (`strategy_experiments`, registrada em `init_db()`); nenhuma
  coluna adicionada a tabelas existentes. 6 endpoints (3 GET fail-soft, 3 POST com
  auth admin). Painel de assertividade ganhou 4 seções — sem botão de promoção.
- **134 testes P05 verdes 2×** e **suíte crítica P01–P05 (377) verde** no
  Python 3.11 real; `py_compile`, `tsc --noEmit` e `git diff --check` aprovados;
  rede/DNS bloqueados e contabilizados. Estratégia, score, IA, sizing, SL/TP,
  `LIVE_SIZE_MULT` e capabilities permanecem inalterados/desligados.

---

## P05.1 — CANDIDATOS CONTEXTUAIS DE EVIDÊNCIA (2026-08-27)

Detalhes em `docs/P05_1_CONTEXTUAL_CANDIDATES.md`.

**ANTES:** os candidatos do P05 eram GLOBAIS (essencialmente `SCORE_MIN`). Com 90
dias reais: 57/59 rejeitados em LOSS_REDUCTION, 53 não atingiu +10% de operações
e 51 passou na validação mas o **teste final levou o drawdown de 4R para 7R** —
os 4 experimentos terminaram `REJECTED`. Baixar o piso globalmente troca perdas
por volume ruim.

**DEPOIS:** candidatos CONDICIONAIS por contexto — bloquear contexto negativo e
afrouxar o piso em exatamente 2 pontos apenas no contexto positivo, com teste
temporal intocado e **nenhuma alteração LIVE**.

- **ANALYTICS_ONLY**: toda regra carrega `phase=P05.1`,
  `execution_mode=ANALYTICS_ONLY`, `promotable=false`, `shadow_supported=false`.
  O P05.1 **não executa a regra no bot real**.
- **Eixos permitidos nesta fase**: apenas `regime` e `entry_zone_type` (valores
  exatamente como persistidos). Símbolo, base, padrão, direção, hora, dia,
  score_bin e ATR band ficam fora — overfitting e múltiplas comparações.
  `ALT_DANGER`/`limit_pullback` são hipóteses observadas, não vencedores fixos.
- **`CONTEXT_RULE` com validador SEPARADO** (não entra na `KNOB_ALLOWLIST`):
  exatamente uma regra, ação `BLOCK` ou `SCORE_DELTA`, `score_delta` só −2.0,
  sem NaN/infinito, sem dois eixos/ações, sem campo de sizing/risco/stop/TP/
  maker/execução. O hash inclui o schema completo.
- **Semântica**: `BLOCK` bloqueia só o contexto correspondente; `SCORE_DELTA` usa
  o **score efetivo** (`_execution_score`, com adjusters) e **não reativa linha
  barrada por outro gate** — R:R, P(TP1), liquidez, P04A/B/C, ATR, funding,
  proximity, struct-chase, tempo e tier continuam valendo. UNKNOWN nunca vira
  BLOCKED nem ELIGIBLE. A `eligibility` do P05 global ficou intacta.
- **Geração SOMENTE no treino**: máx. 4 por objetivo / 8 no total, um contexto por
  candidato, ordem determinística, sem grid search. Sem contexto elegível ⇒
  `NO_CONTEXT_WITH_SUFFICIENT_EVIDENCE`, sem candidato artificial.
- **Sem leakage**: treino gera e congela cobertura/componentes, validação escolhe
  no máximo um finalista por objetivo, e só o finalista abre o holdout. Alterar o
  teste não muda a lista nem os hashes gerados no treino (coberto por teste).
- **Gates pareados** (validação E teste), com IC pareado pela mesma oportunidade:
  além dos critérios do P05, exige contexto bloqueado com **IC superior das
  evitadas < 0** (LOSS_REDUCTION) e **IC inferior das adicionadas > 0**
  (MORE_OPERATIONS), mais amostra afetada mínima. `SCORE_MIN=51` permanece
  rejeitado — a regra de drawdown do holdout continua valendo.
- **Persistência reutilizada**: `StrategyExperiment`, `experiment_key`,
  `candidate_hash`, `dataset_fingerprint`, advisory lock e upsert idempotente —
  **nenhuma tabela nova, nenhuma coluna nova, nenhuma flag nova**.
- **Lifecycle**: P05.1 pode chegar a `OFFLINE_VALIDATED` como resultado analítico,
  mas `start-shadow` e `evaluate-shadow` rejeitam com **`P051_ANALYTICS_ONLY`**.
  Não existe `promotion_plan` executável.
- **API**: adicionado somente `POST /api/strategy/p05/contextual-evaluate` (auth
  admin, clamp 7–365, sem persistência parcial). `POST /api/strategy/p05/evaluate`
  inalterado; `GET /status` ganhou o resumo contextual. Painel reutilizado, com
  descrição legível da regra (nunca `[object Object]`) e aviso "somente análise";
  `frontend/dist` não editado.
- **239 testes P05 verdes 2×** e **suíte crítica P01–P05 verde 2×** no Python 3.11
  real; `py_compile`, `tsc --noEmit` e `git diff --check` aprovados; rede/DNS
  bloqueados e contabilizados. `P05_CHALLENGER_SHADOW_ENABLED` continua `false`,
  `LIVE_SIZE_MULT` e P01–P04 inalterados.

---

## P05.1R — MONITOR DE PRONTIDÃO DE EVIDÊNCIA (2026-08-28)

Detalhes em `docs/P05_1R_EVIDENCE_READINESS.md`.

**ANTES:** a decisão de reavaliar dependia de estimativa manual; candidatos
refutados e limitados por amostra apareciam juntos como `REJECTED`, sem ETA e sem
visibilidade de retenção.

**DEPOIS:** contagem real por hipótese, distinção `SAMPLE_LIMITED`/`REFUTED`/
`MIXED`, ETA conservadora (ausente quando não cabe), retenção observável, holdout
selado e zero alteração no LIVE.

- **Holdout SELADO por construção**: `_load_readiness_rows` seleciona colunas
  EXPLÍCITAS e nunca carrega `realized_r`; os dicionários de linha nem contêm a
  chave. Nenhuma métrica de outcome é calculada e nenhuma função de avaliação/gate
  é chamada. Provado por sentinela (`_SealedRow` levanta se `realized_r` for lido)
  + testes de código que proíbem acesso e chamadas de avaliação no bloco.
  Toda resposta carrega `holdout_outcomes_read=false`,
  `holdout_metrics_computed=false`, `holdout_status=SEALED`.
- **Zero escrita**: nenhum INSERT/UPDATE/DELETE, commit, flush, add ou delete;
  nenhum experimento criado/alterado/reavaliado. Provado por teste de código E por
  sessão falsa que levanta exceção em qualquer escrita.
- **"Afetada" com definição dura**: BLOCK só com champion=True e contextual=False;
  SCORE_DELTA só com champion=False e contextual=True — e a diferença tem que vir
  EXCLUSIVAMENTE do score (linha barrada por R:R, P(TP1), liquidez, P04, ATR,
  funding, proximity, struct-chase, tempo ou tier nunca vira "oportunidade").
  UNKNOWN é contado separado e nunca conta como afetada.
- **Classificação só com checks persistidos** (nada é reavaliado), com prefixo de
  estágio removido (o que também deduplica): 3 dos 5 experimentos reais são MIXED
  (market, NORMAL, limit_ob — direção econômica contrariada) e 2 são
  SAMPLE_LIMITED (ALT_RISK_OFF, limit_fvg_fill). **Refutada/mista não recebe ETA.**
- **Readiness** exige TODOS os mínimos (falha=SAMPLE_LIMITED, cobertura ≥80%,
  validação ≥20, teste ≥20, OOS ≥30, UNKNOWN não comprometendo, retenção ok) e
  significa apenas "há amostra para reavaliar" — a própria resposta lista o
  `does_not_mean` (não é aprovação, Shadow, promoção nem ganho esperado).
- **ETA conservadora**: exige ≥7 dias observados, arredonda para cima, divide por
  0,25 (só ~25% das novas linhas caem na fatia de validação/teste), nunca promete
  data exata e sempre traz `eta_reason`.
- **Retenção auditada**: as duas rotinas de poda em `snapshot_service` apagam
  SOMENTE o namespace `wide`/`wide_*`, disjunto dos status do P05 — nenhuma
  política apaga evidência do P05. Histórico curto é reportado como
  `YOUNG_HISTORY`, explicitamente **não** como deleção. Retenção não foi alterada.
- **API**: apenas `GET /api/strategy/p05/readiness` (clamps 30–365 / 1–100,
  `experiment_id` opcional, sem auth admin, fail-soft, cache single-flight
  chaveado por parâmetros que não cacheia erro). Nenhum POST criado;
  `/p05/status` ganhou só um resumo pequeno. Painel reutilizado com a seção
  "Prontidão para nova avaliação" + selo "holdout protegido", sem botão de ação;
  `frontend/dist` não editado.
- **308 testes P05 verdes 2×** e **suíte crítica P01–P05 verde 2×** no Python 3.11
  real; `py_compile`, `tsc --noEmit` e `git diff --check` aprovados. P05.2 NÃO foi
  implementado; `P05_CHALLENGER_SHADOW_ENABLED` continua `false`; champion LIVE,
  `SCORE_MIN`, `LIVE_SIZE_MULT` e P04A/B/C inalterados.

### Auditoria de fechamento P05.1R (2026-08-29)

- O monitor passou a reproduzir cada hipótese exclusivamente com o **champion** e
  os `active_components` congelados no experimento. Metadado ausente, hash
  divergente ou componente inválido agora resulta em `INSUFFICIENT_METADATA`; o
  contrato não é reconstruído com dados posteriores.
- Drift entre o champion atual e o congelado bloqueia prontidão e orienta gerar
  uma hipótese nova. A contagem ainda usa o contrato congelado para manter a
  auditoria explicável.
- Check persistido desconhecido falha fechado, mesmo misturado a checks de
  amostra; não pode mais ser reduzido indevidamente a `SAMPLE_LIMITED`.
- `UNKNOWN` é medido dentro do contexto-alvo (não diluído pelo dataset global),
  a ETA usa somente o período realmente carregado e o status consolidado consulta
  ao menos a janela de retenção de 120 dias.
- Falha de retenção, erro parcial de leitura ou configuração P05.1 incompleta
  nunca produz `READY_FOR_REEVALUATION` nem contamina o cache. Somente registros
  declarados simultaneamente como `P05.1` + `ANALYTICS_ONLY` + não promovíveis +
  sem Shadow entram no monitor.
- **318 testes P05 verdes 2×**; **561 testes críticos P01–P05 verdes**;
  `py_compile` e `git diff --check` aprovados. Holdout permaneceu selado, zero
  escrita no banco, zero rede/exchange e nenhuma alteração de estratégia LIVE.

### Paridade de cobertura treino/readiness (2026-08-31)

- Uma reavaliação real com 5.638 snapshots expôs que a cobertura global podia
  superar 80% enquanto a metade cronológica de treino ainda tinha apenas 69,3%
  (`regime`) / 70,7% (`entry_zone_type`). O monitor anunciava READY, mas o
  gerador corretamente recusava com `AXIS_COVERAGE_TOO_LOW`.
- `project_prospective` agora publica
  `prospective_train_context_coverage_pct`, e o readiness usa exatamente esse
  valor para o gate de 80%, em paridade com `temporal_split` e
  `generate_contextual_candidates`. Cobertura global continua visível apenas
  como diagnóstico e nunca libera reavaliação.
- Auditoria agregada e somente leitura no PostgreSQL confirmou causa histórica:
  ambos os campos estão em **100% dos snapshots semanais desde 2026-06-29**.
  Nenhum backfill foi feito, nenhum dado antigo foi inventado e a persistência
  atual não precisou de alteração.

---

## P05.1T — TELEMETRIA PROSPECTIVA DE EVIDÊNCIA (2026-08-28)

Detalhes em `docs/P05_1T_FORWARD_EVIDENCE_TELEMETRY.md`.

**ANTES:** MAE/MFE indisponível (bloco estático); cobertura contextual só
parcialmente observável, sem o corte de treino; slippage existia mas sem painel
consolidado; eventos de gate com limitações não mapeadas.

**DEPOIS:** MAE/MFE prospectivo por candle de 5m, cobertura de coleta explícita
(global/treino/validação/teste), slippage auditável, disponibilidade honesta dos
gates — e **zero efeito sobre a estratégia**.

- **`features["p05_path"]`** — namespace novo dentro do JSONB JÁ existente.
  Nenhuma coluna, tabela, migration, env ou flag. Snapshot novo inicializa sem
  marcar MAE/MFE como zero observado (sem candle ⇒ `null`). **Sem backfill.**
  Escrita sempre reatribui novo dict e nunca apaga `p05_context`.
- **Fórmula** `risk_unit=abs(entry-stop)`, com LONG/SHORT espelhados e
  `max(0, …)`. MAE/MFE nunca diminuem, timestamps nunca retrocedem, NaN/inf nunca
  persistidos, dado inválido não apaga telemetria válida, arredondamento só na
  serialização. `direction` comparado **exatamente** (`long`/`short`) porque o
  classificador usa `== "long"` sem normalizar — normalizar criaria divergência.
- **Limite terminal**: a telemetria observa no INÍCIO de cada vela, antes da
  decisão; como o classificador retorna no terminal, o candle terminal ENTRA e os
  posteriores NÃO.
- **Integração opcional e fail-soft**: `_classify_outcome_candles(..., path_trace=None)`.
  Sem trace = comportamento anterior EXATO; com trace = MESMO outcome. Stop/TP na
  mesma vela, TP1/BE+/trail e `peak_price_since_tp1` idênticos. Erro no trace é
  engolido e não altera decisão. Instrumentado só em `check_open_snapshots` —
  `check_wide_snapshots` (namespace podado) não recebe `p05_path`.
- **Dedupe** por `last_candle_ts_ms` do próprio `p05_path`: só timestamp
  estritamente maior, nada anterior ao `created_at`, nada repetido pela
  sobreposição. O classificador continua recebendo a janela ORIGINAL. Restart
  continua do último timestamp; retry é idempotente.
- **Finalização**: `FINAL_OBSERVED` (fechou por candle), `FINAL_PARTIAL`
  (time-stop/teto/símbolo sem dados — mantém o observado), `UNAVAILABLE` (nunca
  houve candle). Resolução do snapshot nunca é atrasada por telemetria.
- **Diagnóstico MAE/MFE** substitui o bloco estático: `SHADOW_SETUP_PATH_5M`,
  cobertura, missing por motivo, mean/median/p75/p90. `USABLE` só com ≥30
  observados E ≥80% de cobertura. **Não gera "stop ideal" nem "TP ideal".**
- **Cobertura contextual** (`regime`, `entry_zone_type`) reutiliza o MESMO
  `temporal_split`/`axis_coverage` — sem split novo. O status é decidido pela
  cobertura de **TREINO**; a global nunca a substitui. Não lê `realized_r`, não
  abre holdout (sentinela em teste).
- **Slippage** reutiliza `RealTrade.entry_slippage_pct` sem recalcular: cobertura,
  média, mediana, p75, p90, inválidos por motivo. Ausência nunca vira zero; zero
  legítimo continua válido. **Latência = `UNAVAILABLE`**: não há timestamp durável
  decisão→ACK, e fetch latency NÃO é execution latency.
- **Gates**: mapa de disponibilidade honesto — P04A/P04B `UNAVAILABLE` (abortam no
  POST), P04C `AVAILABLE`. Eventos seguem sendo eventos, não oportunidades únicas.
  Nenhum hook no executor; nenhum "lucro perdido" estimado.
- **Retenção** reutiliza `_retention_snapshot`; teste garante que a poda continua
  exclusiva de `wide`/`wide_*` e que nenhuma evidência P05 é apagada.
- **API**: nenhum endpoint novo — só expansão de `/api/shadow/assertiveness` e
  `/api/strategy/p05/status` (GET, read-only, fail-soft, cache reutilizado).
  Painel ganhou "Qualidade da telemetria", sem botão, sem `[object Object]`,
  `frontend/dist` intacto.
- **397 testes P05 verdes 2×** (78 novos em `test_p05_1t_evidence_telemetry.py`) e
  **suíte crítica P01–P05 (640) verde 2×** no Python 3.11 real; `py_compile`,
  `tsc --noEmit` e `git diff --check` aprovados; rede/DNS bloqueados e
  contabilizados. `shadow_trade_service`, `trade_manager_service` e signed services
  INTACTOS. P05.2 não implementado; `P05_CHALLENGER_SHADOW_ENABLED` continua
  `false`; champion LIVE, score, tier, gates, stop, TP e sizing inalterados.

### Fechamento da auditoria P05.1T

- `p05_path` passa a nascer junto do snapshot normal, antes do primeiro candle;
  setup incompatível recebe schema completo `UNAVAILABLE` com motivo, sem ser
  confundido com histórico anterior à telemetria.
- A cobertura contextual deixou de usar outcomes normalizados e a janela móvel
  do painel. Agora usa exclusivamente `_load_readiness_rows(120)`, sem
  `realized_r`, em paridade com o P05.1R; retenção reutiliza a mesma leitura.
- A regressão reproduz a condição real: cobertura global 85% e treino 70%
  continuam `COLLECTING`; a UI explicita a janela operacional de 120 dias.
- Validação final no Python 3.11 real: **81 testes específicos P05.1T**, **400
  testes P05 verdes 2×** e **suíte crítica P01–P05 (643) verde 2×**;
  `py_compile`, `tsc --noEmit` e `git diff --check` aprovados.

---

## P05.2A — DIAGNÓSTICO LONGITUDINAL DOS STOP-LOSSES (2026-08-28)

Detalhes em `docs/P05_2A_LONGITUDINAL_STOP_DIAGNOSIS.md`.

**ANTES:** muitos stops observados, mas sem separação robusta entre volume, taxa
e persistência temporal — um segmento de alto volume parecia "o pior" mesmo com
taxa abaixo da média.

**DEPOIS:** diagnóstico longitudinal com taxa por exposição, confirmação
treino→validação, teste final selado, REAL e SHADOW separados, trajetória
prospectiva quando disponível e hipóteses SOMENTE analíticas.

- **Identidade do stop**: SHADOW `lost` (com R<0) é stop original; `won_tp1_be` é
  saída PROTETIVA pós-TP1 e NÃO é stop; `expired` não é stop; divergência
  status/R é `INCONSISTENT` (excluída e contada); `realized_r=None` nunca vira
  zero. REAL: só `source=auto`, janela por `closed_at`, `closed_stop` é stop e
  `closed_manual` negativo fica SEPARADO como `NEGATIVE_MANUAL_EXIT`.
  **REAL e SHADOW nunca são somados.**
- **Holdout SELADO**: `load_stop_shadow_split` conta e ordena por
  `(outcome_at, id)` SEM selecionar outcome, divide 50/25/25 e materializa
  `realized_r`/`status`/`features` APENAS para treino+validação — a 2ª query é
  limitada pela borda da validação e aparada pelo conjunto exato de ids, então
  empate de `outcome_at` não deixa linha de teste vazar. Do teste só saem
  contagem e limites. `holdout_status=SEALED`, `holdout_outcomes_read=false`,
  `holdout_metrics_computed=false`, com sentinela que explode se o outcome do
  holdout for lido.
- **Janela fixa** `P052A_WINDOW_DAYS = 120`, com período realmente observado,
  timestamps e marcação `young_history` — sem inventar histórico.
- **Métricas por estágio** (treino e validação separados): stop rate + Wilson
  95%, wins, saídas protetivas, expired, expectancy/soma/mediana R, profit
  factor, pior sequência de stops, tempo até o stop (mediana/p75/p90), cobertura
  e confiabilidade. Contagem absoluta NUNCA é chamada de "pior" sem dividir pela
  exposição.
- **Segmentos** nos mesmos 14 eixos do `segment_rows`, com taxa por EXPOSIÇÃO,
  Wilson, lift em p.p. versus baseline do estágio, participação nos stops,
  **wins que seriam removidos se o segmento fosse bloqueado**, expectancy e
  cobertura. Eixos sobrepostos (patterns) são ATRIBUIÇÃO e não somam.
- **Confirmação temporal**: `PERSISTENT_ADVERSE` exige cobertura ≥80% nos dois
  estágios, ≥30/≥20 observações, ≥10/≥5 stops, taxa acima do baseline E
  expectancy negativa NOS DOIS. Caso contrário `MIXED`/`SAMPLE_LIMITED`/
  `LOW_COVERAGE`/`NOT_ADVERSE`. Alto volume com taxa abaixo do baseline NÃO é
  adverso (coberto por teste). Máx. 8 padrões, ordem determinística; sem padrão
  persistente ⇒ `NO_PERSISTENT_STOP_PATTERN`.
- **Trajetória** (só onde há telemetria válida): tempo até o stop, MFE antes do
  stop (lido SOMENTE de `features["p05_path"]`, sem backfill e sem recálculo de
  LONG/SHORT) e faixas de `stop_distance_pct`, todas DESCRITIVAS. É proibido
  concluir "ampliar o stop", "stop ideal" ou "perda teria sido evitada".
- **Hipóteses P05.2B**: no máx. 8, com evidência de treino, confirmação/refutação
  na validação, cobertura, lift, expectancy, wins no contexto e risco de remover
  operações boas — **sem** knob, config, threshold, BLOCK, SCORE_DELTA, ação
  executável ou `promotion_plan` (verificado por teste).
- **Integração**: `stop_diagnosis` acrescentado ao retorno EXISTENTE de
  `/api/strategy/p05/status`, preservando todos os campos, com cache
  single-flight do P05 (erro não envenena) e fail-soft. **Nenhum endpoint novo,
  nenhum POST, `main.py` não foi modificado.** Painel ganhou a seção "Por que as
  recomendações tomam stop" com selo 🔒 protegido, REAL/SHADOW separados e os
  avisos de correlação≠causalidade e "nenhuma alteração foi aplicada à
  estratégia"; sem botão, sem `[object Object]`, `frontend/dist` intacto.
- **69 testes P05.2A verdes 2×**, suíte P05 completa **verde 2×** e suíte crítica
  completa (`unittest discover`) **verde 2×** no Python 3.11 real; `py_compile`,
  `tsc --noEmit` e `git diff --check` aprovados; rede/DNS bloqueados e
  contabilizados. Zero escrita no banco, zero `StrategyExperiment`, zero
  tabela/coluna/env/flag. `snapshot_service`, `shadow_trade_service`,
  `trade_manager_service` e signed services INTACTOS. P05.2B não implementado;
  champion, score, tier, gates, stop, TP e sizing inalterados.

### Fechamento da auditoria P05.2A

- Integração de produção fechada: o agregado `/api/shadow/assertiveness`, que é
  a fonte real do `AssertivenessPanel`, agora repassa o mesmo `stop_diagnosis`
  cacheado exposto por `/api/strategy/p05/status`; antes o backend calculava o
  P05.2A corretamente, mas o painel não recebia esse campo. Validação após a
  correção: **75 testes P05.2A**, **475 testes P05 2×** e **718 testes críticos
  2×**, além de `py_compile`, `tsc --noEmit` e `git diff --check`.
- `won_tp1_be` não positivo e `expired` diferente de zero agora são tratados
  como `INCONSISTENT`, em coerência com o contrato status/R.
- O resumo REAL passou a reutilizar a identidade econômica do loader P05
  (ordem da exchange → client order → recommendation → id), impedindo retry ou
  import duplicado de inflar stop rate e expectancy.
- O diagnóstico reutiliza `_DIAG_CACHE` e `_DIAG_CACHE_LOCK` com namespace de
  chave próprio; o cache paralelo foi removido e erros totais ou parciais
  continuam sem cache.
- Falha na geração das hipóteses agora retorna `UNAVAILABLE` e aparece assim na
  tela, sem afirmar falsamente que não existe padrão persistente.
- Ordenação de lift zero e a base `regime` da cobertura geral ficaram explícitas;
  a tela distingue treino (50%) de validação (25%) e não chama amostra limitada
  de hipótese refutada.
- Validação final no Python 3.11 real: **73 testes específicos P05.2A**, **473
  testes P05 verdes 2×** e **suíte crítica P01–P05 (716) verde 2×**;
  `py_compile`, `tsc --noEmit` e `git diff --check` aprovados.
