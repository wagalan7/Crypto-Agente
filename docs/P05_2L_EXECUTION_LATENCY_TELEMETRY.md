# P05.2L — telemetria de latência da entrada real (OBSERVAÇÃO PURA)

## Objetivo

Registrar **prospectivamente** a duração do caminho de entrada LIVE, separando o
que antes era um único borrão:

- demora entre a **decisão** e a **tentativa**;
- duração da **chamada de entrada**;
- demora até **persistir o RealTrade**;
- **fill confirmado** versus estado desconhecido;
- rota **maker / market / fallback**;
- slippage (que continua vindo do `RealTrade`).

> Somente observação. Não altera decisão, execução, qty, entrada, SL, TP,
> sizing, rota ou fluxo de controle. Não faz chamada adicional à exchange.

## Antes

`summarize_latency()` devolvia um bloco **estático** `UNAVAILABLE`: não existia
timestamp durável de decisão nem de ACK, e latência de *fetch* de dados não é
latência de execução.

## Depois

Trace prospectivo por entrada LIVE, gravado em
`RecommendationSnapshot.features["p05_execution_path"]`, com resumo analítico
real (`UNAVAILABLE` → `COLLECTING` → `USABLE`) e painel próprio.

## Armazenamento

Namespace exclusivo `features["p05_execution_path"]` — **nenhuma tabela, coluna
ou migration nova**.

```
schema_version=1 · source="LIVE_ENTRY_CALLER" · status · route · quality
snapshot_created_at · decision_at · attempt_started_at · attempt_returned_at
real_trade_persisted_at · exchange_event_at
decision_to_attempt_ms · attempt_roundtrip_ms · attempt_to_persist_ms · end_to_end_ms
fill_confirmed · fill_price_source · reason_code · missing_fields
recorded_at · completeness_rank · limitations
```

Status: `NOT_SUBMITTED` · `NO_FILL` · `SUBMISSION_UNKNOWN` · `FILL_CONFIRMED` ·
`OPEN_PERSISTED` · `PERSISTENCE_FAILED` · `UNAVAILABLE`.

**Nunca persistido**: payload bruto da exchange, headers, assinatura,
credenciais, token, proxy, corpo de erro completo, IDs de ordens condicionais ou
qualquer dado secreto (verificado por teste).

`completeness_rank` é o único campo adicional ao contrato: ordena a completude do
trace e é a guarda do UPDATE atômico.

## Semântica dos tempos

Timestamps duráveis em **UTC**; durações medidas por relógio **monotônico**
dentro do mesmo processo.

| Marca | Momento exato |
|---|---|
| `decision_at` | o fluxo LIVE terminou os gates e decidiu tentar a entrada |
| `attempt_started_at` | imediatamente antes do `await` que chama maker ou market |
| `attempt_returned_at` | imediatamente após o retorno dessa chamada |
| `real_trade_persisted_at` | depois de `_open_trade_fail_closed` retornar sucesso |
| `exchange_event_at` | só se a resposta trouxer timestamp explícito e plausível |

`exchange_event_at` **nunca** é substituído por horário local: sem campo válido
fica `null`.

`attempt_roundtrip_ms` inclui preflight, chamada e processamento interno do
helper — **não é "tempo de fill"**.

Fill não auditável ⇒ `fill_confirmed=null`, `fill_price_source=null` e o campo
entra em `missing_fields`. NaN, infinito, valor negativo ou timestamp invertido
⇒ `null` e `quality` cai para `PARTIAL`/`UNAVAILABLE`. **Nunca zero.**

## Hot path

Instrumentado **somente** o caminho de entrada LIVE normal em
`shadow_trade_service.py`. Ficam de fora: shadow puro, hedge, pyramiding, TF
upgrade, fechamento, trade manager e autoheal (verificado por teste).

A telemetria é montada em memória, começa **depois dos gates**, não executa
leitura nem escrita no banco **antes** da tentativa de ordem, e é persistida
**depois** que o resultado crítico já está definido. Nunca altera `order_res`,
`qty`, entrada, SL, TP, nem um `continue`, retry ou retorno existente; nunca
captura exceção da execução como se fosse erro da telemetria.

Falha ao persistir: log estruturado, fluxo original preservado, sem pausa, sem
retry, sem segunda chamada à exchange.

O `snapshot_id` já era lido antes da tentativa; a consulta apenas passou a trazer
também `created_at`, para o ponta a ponta.

## Merge JSONB sem perda

Helper testável em `snapshot_service.py`:

- `merge_execution_path(features, payload)` — puro, preserva todas as demais
  chaves e nunca deixa um trace mais incompleto apagar um mais completo;
- `persist_execution_path(snapshot_id, payload)` — grava **só** a chave
  `p05_execution_path`, pelo id exato, com **UPDATE JSONB atômico**:

```sql
UPDATE recommendation_snapshots
   SET features = coalesce(features,'{}'::jsonb) || '{"p05_execution_path": …}'::jsonb
 WHERE id = :id
   AND coalesce(jsonb_extract_path_text(coalesce(features,'{}'::jsonb),
                'p05_execution_path','completeness_rank')::int, -1) <= :rank
```

Sem read-modify-write do JSON completo: o `||` é avaliado no PostgreSQL contra o
valor **atual** da linha, então `p05_path` e `p05_context` nunca são apagados. O
`WHERE` do rank torna o retry **idempotente** e impede downgrade.

No sentido inverso, os quatro pontos do P05.1T que reatribuíam `snap.features`
em snapshots existentes passam por `stage_feature_namespace_merge(snap)`, que
converte a reatribuição em um merge JSONB atômico **da chave `p05_path`**, sem
consulta extra — assim o UPDATE do lote não apaga uma latência gravada no
intervalo. Fail-soft: em qualquer erro a reatribuição original permanece. O
resolver não foi refatorado.

## Resumo analítico

`summarize_latency(rows, real_rows)` substitui o bloco estático e lê apenas
traces já persistidos, reutilizando `_distribution`, `_finite`, o loader de
readiness existente e a ligação `RealTrade.recommendation_id`.

Reporta: total de tentativas observadas · quantidade por status · por rota ·
`COMPLETE`/`PARTIAL`/`UNAVAILABLE` · cobertura · missing por motivo ·
decisão→tentativa · duração da chamada · tentativa→persistência ·
recomendação→persistência (mediana, p75, p90) · fills auditáveis · registros sem
timestamp da exchange · RealTrades ligados.

| Situação | Status |
|---|---|
| zero observações | `UNAVAILABLE` |
| alguma observação, mas <30 ou cobertura <80% | `COLLECTING` |
| ≥30 e cobertura ≥80% | `USABLE` |

Slippage **não** é recalculado: a fonte única continua sendo
`RealTrade.entry_slippage_pct`, no bloco `slippage` da mesma resposta.

**Latência não é correlacionada com lucro ou stop nesta fase.**

## Frontend

Dentro de "Qualidade da telemetria", a seção **"Latência da entrada real"**
mostra status, cobertura, tentativas observadas, mediana e p90 da chamada,
recomendação até persistência, fills auditáveis, registros incompletos e rotas
maker/market.

Texto fixo: *"Essa medição descreve o caminho técnico da entrada. Ela ainda não
prova que a latência causou ganho ou stop."*

Sem botão; `frontend/dist` não foi editado.

## Limitações

1. Descreve o **caminho técnico** da entrada; não prova causa de ganho ou stop.
2. `attempt_roundtrip_ms` inclui preflight e processamento do helper.
3. `exchange_event_at` só existe quando a resposta traz timestamp explícito.
4. Fill não auditável permanece UNKNOWN — nunca inferido de `ok=true`.
5. Sem backfill: só entradas posteriores ao P05.2L têm trace.

## Invariantes

- Champion LIVE idêntico; nenhuma ordem criada, cancelada ou modificada.
- Nenhuma chamada adicional à exchange, nenhum SDK ou provider novo.
- Nenhuma tabela, coluna, migration, env, flag ou endpoint novo.
- Telemetria nunca bloqueia nem atrasa uma entrada; falha é fail-soft.
- UNKNOWN nunca vira zero, sucesso, fill ou ausência comprovada.
