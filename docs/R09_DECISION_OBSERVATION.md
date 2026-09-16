# R09 — Funil de decisões pós-seleção e observação segregada das vetadas

Modo observacional. Base do lote: `b0d52a80`. Serviço:
`backend/services/decision_observation_service.py`; modelo:
`backend/models/decision_observation.py`. Nenhuma regra de entrada, score,
tier, probabilidade, risco, stop, TP, alavancagem ou sizing foi alterada.

## Escopo: `POST_SELECTION`

O denominador é **exatamente** a lista entregue a `open_shadow_for_recs`,
registrada antes dos gates do executor. Não é o scanner inteiro nem "todos os
sinais do mercado". Os contadores legados por gate (`_record_skip`) continuam
sendo eventos; não viraram oportunidades.

Três coisas distintas, três tabelas sem FK para as operacionais:

| Tabela | Unidade | Conteúdo |
| --- | --- | --- |
| `decision_observations` | oportunidade | identidade, setup/config/trace congelados, primeira decisão e primeiro bloqueio |
| `decision_observation_attempts` | tentativa (cada reavaliação) | resultado, bloqueio, evidência de envio, só o estágio de execução do trace |
| `rejected_setup_observations` | vetada | cópia isolada do setup/config, velas compartilhadas, cobertura e outcome de replay |

## APIs

`begin_batch`, `stage_decision`, `stage_result`, `seal_batch`,
`flush_pending`, `observe_candles`, `get_status`. Hooks são síncronos, sem IO,
com buffer limitado (`MAX_PENDING=500`) e nunca levantam exceção para o
executor. Não há worker, `create_task`, fila, fetch ou notificação novos.

- **Identidade:** `snapshot_id` explícito quando existir; senão hash de
  símbolo/TF/direção/entrada/stop/TP1/TP2 + fechamento da vela do sinal
  (`SETUP_CANDLE`). Sem vela/campos → `identity_missing`, **sem** ID inventado
  pelo relógio e sem consulta "último snapshot".
- **Reavaliação:** mesma identidade → nova tentativa, mesma oportunidade.
  Setup/config/trace/primeira decisão nunca são reescritos (`coalesce`).
- **"Primeiro":** `first_seen_at` = observação mais antiga persistida
  (`least`); `first_decision`, `first_blocker` e o setup congelado são os da
  **primeira tentativa persistida**. Entre processos, uma observação mais antiga
  pode chegar depois: fica em `first_decision_observed_at` e é contada em
  `out_of_order_first`. `get_status` expõe `order_semantics`.
- **Estados de tentativa:** `INELIGIBLE`, `REJECTED`, `ATTEMPTED`, `NO_FILL`,
  `FAILED`, `OPENED`, `PAPER_OPENED`, `INCIDENT`, `PERSISTENCE_FAILED`,
  `UNKNOWN`. Estado terminal não é rebaixado por hooks genéricos.
- **Envio ≠ fill:** `submit_evidence` só é `SUBMITTED` com `orderId`
  explícito; `entry_not_submitted` → `NOT_SUBMITTED`; quantidade planejada ou
  `submitted_qty` não contam como prova.
- **Abortos de pré-voo:** `P04A_MAKER:<código>` (revalidação da LIMIT maker) e
  `P04B_MARKET:<código>` (depth/VWAP da MARKET, inclusive fallback).

## Selagem e persistência

- Só lotes **selados** são persistidos. `seal_batch` roda no `finally` dos
  **dois** chamadores do executor em `main.py` (ciclo de scan e endpoint
  `/api/recommendations`) e remove os handles privados `_r09_*` da rec, que pode
  voltar à API. O endpoint só sela; o ciclo de snapshots persiste.
- Flush concorrente (ciclo de snapshots) **não drena** tentativa ativa.
- Tentativa não selada há mais de 1 h só é descartada quando o buffer lota
  (`stale_unsealed_evicted`); nunca é persistida pela metade.
- Timeout (`flush_timeouts`), cancelamento (`flush_cancelled`, re-propagado)
  e erro (`flush_errors`) são contados; `persistence_dropped` só quando a
  admissão não chegou a ser commitada. A admissão é commitada antes do
  resolver: falha no replay não perde o lote.

## Capacidade (tetos fixos, sem ENV)

`CAPACITY = {opportunities: 50.000, attempts: 100.000, rejected: 50.000}`.

- Checagem **por lote** (três `COUNT` + dois lookups por lote, não por linha),
  na **mesma transação** do insert, sob `pg_try_advisory_xact_lock` próprio
  (`R09_ADVISORY_LOCK_KEY = 0x52303943`), distinto do `917283` do P03/risco.
- Reavaliação não consome vaga de oportunidade. Oportunidade nova só entra com
  vaga para ela **e** para a primeira tentativa (nenhuma órfã).
- Cheio: novos registros são descartados e contados
  (`capacity_dropped_opportunities/attempts/rejected`). **Nada é apagado.**
  O resolver continua acompanhando vetadas antigas.
- Lock ocupado: `capacity_lock_contention`; o lote volta ao buffer até
  `MAX_ADMISSION_RETRIES=3` (`contention_requeued`), depois
  `contention_dropped`.
- `get_status.capacity`: `OK`, `NEAR_LIMIT` (≥90%), `AT_LIMIT`
  (`admission_blocked=true`) ou `UNKNOWN`. O painel mostra o aviso.

## Trajetória das vetadas (replay isolado)

- Só velas **já buscadas** pelo resolver de snapshots
  (`_resolver_fetch_ohlcv(symbol, "5m", 50)`), fechadas, válidas (OHLC
  coerente, volume presente) e posteriores à decisão. **Zero fetch
  adicional.** Sem janela compartilhada para o símbolo → `UNAVAILABLE`.
- A fonte é OKX com fallback Binance e **não é rotulada por vela**
  (`price_source=SNAPSHOT_RESOLVER_WINDOW_UNLABELED`).
- Janelas só para símbolos com vetadas abertas (após o primeiro refresh),
  para o teto de 64 símbolos não descartar justamente os que importam.
- Replay R10 real com config congelada. Mapa **explícito**:

| Status R10 | Cobertura R09 | Terminal |
| --- | --- | --- |
| `CLOSED_STOP`, `CLOSED_RUNNER_STOP`, `CLOSED_TP2`, `CLOSED_TIME_STOP`, `CLOSED_MAX_HOLD` | `RESOLVED` | sim |
| `NOT_FILLED` | `NOT_FILLED` | sim |
| `AMBIGUOUS_ENTRY_BAR` | `AMBIGUOUS` | sim |
| `MISSING_OR_UNORDERED_BARS` | `GAP_PENDING` → `DATA_GAP_FINAL` | quando a vela faltante sai da janela de 50 velas |
| `INSUFFICIENT_DATA` | `PENDING` → `EXPIRED_INCOMPLETE` | idem |
| setup/config congelados inválidos, status não mapeado, erro do motor | `INVALID` | sim |

  Terminais não são reprocessados (versão CAS inalterada). Ambiguidade e
  lacuna nunca viram resolução. `gross_r` só em trajetória fechada;
  `net_r=None` e `cost_status=UNKNOWN` sempre; `learning_eligible=false`.
- **Horizonte:** a config congelada usa 3 velas de janela, time-stop pré-TP1 de
  12 e máximo de 24 velas de 5 m (`SHORT_RESEARCH_HORIZON_NOT_LIVE_TIME_STOP`).
  Não é o time-stop LIVE por timeframe (horas/dias). O resultado responde "o que
  aconteceu nas ~2 h seguintes sob este adaptador", não "o que o bot teria feito".

## Isolamento e leitura

Nenhuma tabela R09 alimenta calibração, learning, risco, rotação, PnL ou
`RealTrade`. `GET /api/strategy/p05/status` e o agregado do painel recebem
`research_batch.decision_funnel` com coleta/cobertura: oportunidades,
reavaliações, primeiros bloqueios, evidência de envio, cobertura de trace e das
vetadas, capacidade e telemetria do processo. Nenhuma métrica econômica e
nenhum dado do holdout. Sem rota mutante nova e sem botão de ativação.

## Validação

- `tests/test_r09_decision_observation.py` (42 testes, hermético): identidade,
  congelamento/allowlist, reavaliação, P04A×P04B, envio≠fill, selagem e
  remoção de handles, despejo de não selados antigos, buffer/símbolos,
  filtro de símbolos desejados, velas inválidas, timeout/cancelamento/
  contenção/falha pós-admissão, capacidade, upsert, status sem economia, e o
  **mapa de status chamando o replay R10 real** (stop, TP2, não preenchida,
  ambígua, lacuna pendente/final, primeira vela ausente, horizonte
  pendente/expirado, setup inválido, sem fonte, status não mapeado).
- `tests/pg_integration_r09.py`: PostgreSQL 16 real descartável, só socket
  Unix, TCP/DNS bloqueados. Schema 2×, lote ativo não drenado, dedupe,
  setup congelado, concorrência, ordem fora de sequência, replay resolvido,
  terminal processado uma vez, inválido terminal, capacidade pequena (patch),
  resolução com admissão lotada, nenhuma órfã, contenção com lock ocupado,
  lock do risco livre, GET sem economia e **todas** as tabelas operacionais
  com contagem inalterada.

```sh
R09_TEST_SOCKET=/tmp/cw-r09-sock.XXXX backend/.venv311/bin/python -B backend/tests/pg_integration_r09.py
```

## Limitações

Cobertura só enquanto houver snapshot aberto do símbolo; fonte de velas sem
rótulo por vela; horizonte curto de pesquisa; contadores de telemetria são
por processo desde o boot; janelas de data de oportunidades e tentativas são
independentes, então `reevaluations` é aproximado nas bordas; o trace R08B da
recomendação (≈3,7–8,8 KB) também segue na resposta da API. A coleta só começa
após deploy; nada foi medido em produção nesta etapa.
