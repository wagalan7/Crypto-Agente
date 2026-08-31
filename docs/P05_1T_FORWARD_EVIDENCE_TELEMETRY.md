# P05.1T — telemetria prospectiva de evidência (observação pura)

## Objetivo

Começar a coletar, daqui para frente, a evidência que hoje falta: MAE/MFE por
setup, cobertura dos contextos do P05.1, qualidade do slippage já existente,
disponibilidade dos eventos de gate e retenção.

> **Telemetria nunca decide nada.** Nada aqui altera recomendação, score, tier,
> gate, sizing, stop, TP, execução, candidato ou LIVE. Se a telemetria falhar, o
> outcome continua funcionando, o snapshot continua sendo resolvido e o bot mantém
> exatamente o comportamento anterior — nenhuma exceção de telemetria chega ao
> caminho principal.

## Antes

- MAE/MFE: bloco estático `UNAVAILABLE` (reconstruir do histórico seria falso).
- Cobertura contextual: observável só indiretamente, sem o corte de treino.
- Slippage: existia em `RealTrade.entry_slippage_pct`, sem painel consolidado.
- Gates: contadores com limitações conhecidas, sem mapa de disponibilidade.

## Depois

- MAE/MFE **prospectivo**, acumulado dos candles de 5m que o resolver **já busca**.
- Cobertura de coleta explícita, com global/treino/validação/teste.
- Slippage auditável (cobertura, média, mediana, p75, p90, inválidos por motivo).
- Disponibilidade honesta dos gates (`AVAILABLE` / `UNAVAILABLE` + motivo).
- **Zero efeito sobre a estratégia.**

## `features["p05_path"]` — schema

Namespace novo dentro do JSONB **já existente**. Nenhuma coluna, tabela,
migration, env ou flag foi criada.

| Campo | Significado |
|---|---|
| `schema_version` | 1 |
| `source` | `resolver_5m_ohlcv` |
| `resolution` | `5m` |
| `status` | `WAITING_FOR_FIRST_CANDLE` · `COLLECTING` · `FINAL_OBSERVED` · `FINAL_PARTIAL` · `UNAVAILABLE` |
| `mfe_r` / `mae_r` | excursão máxima a favor / contra, em múltiplos de R |
| `best_price` / `worst_price` | extremos observados (incluem o `entry`) |
| `risk_unit` | `abs(entry - stop_loss)` |
| `observed_candles` | candles únicos efetivamente contados |
| `first_candle_ts_ms` / `last_candle_ts_ms` | borda temporal observada |
| `last_updated_at` / `finalized_at` | carimbos |
| `unavailable_reason` | motivo explícito quando não há dado |
| `limitations` | limitações que acompanham o número |

Snapshots novos inicializam o namespace **sem** marcar MAE/MFE como zero
observado: sem candle, ambos permanecem `null`. **Não há backfill** — snapshots
antigos sem `p05_path` continuam antigos e entram como `missing`, com motivo.

A escrita **sempre reatribui um novo dict** a `snap.features` (garante o JSONB
dirty) e **nunca apaga outras chaves** — `p05_context` e o vetor de features do
learning ficam intactos.

## Fórmula

`risk_unit = abs(entry - stop_loss)`, calculada só quando `entry` e `stop` são
finitos e positivos, `risk_unit > 0`, `direction` é **exatamente** `long` ou
`short`, e o candle tem `high`/`low` finitos, positivos e `high >= low`.

**LONG**
```
best_price  = max(entry, maiores highs observados)
worst_price = min(entry, menores lows  observados)
mfe_r = max(0, (best_price - entry) / risk_unit)
mae_r = max(0, (entry - worst_price) / risk_unit)
```

**SHORT**
```
best_price  = min(entry, menores lows  observados)
worst_price = max(entry, maiores highs observados)
mfe_r = max(0, (entry - best_price) / risk_unit)
mae_r = max(0, (worst_price - entry) / risk_unit)
```

Garantias: MAE e MFE **nunca diminuem** (acumuladores só usam `max`/`min`);
timestamps nunca retrocedem; NaN/infinito nunca são persistidos; nenhum valor
negativo; dado inválido **não apaga** telemetria válida anterior; arredondamento
acontece **apenas na serialização**, nunca durante a acumulação.

> `direction` é comparado **exatamente** com `long`/`short` porque
> `_classify_outcome_candles` faz `snap.direction == "long"` sem normalizar.
> Normalizar aqui criaria divergência: `"LONG "` seria short para o classificador
> e long para a telemetria.

## Limite temporal terminal

A telemetria observa o candle **no início do processamento de cada vela**, antes
de qualquer decisão do classificador. Como o classificador **retorna** ao detectar
o outcome terminal, o loop encerra:

- o candle **terminal ENTRA** no MAE/MFE;
- os candles **POSTERIORES ao terminal NÃO entram**.

## Integração com o classificador

`_classify_outcome_candles(snap, df_window, path_trace=None)` — parâmetro
**opcional**, default `None`.

- Chamada **sem** trace produz exatamente o comportamento anterior.
- Chamada **com** trace produz exatamente o **mesmo outcome**.
- A regra conservadora de stop/TP na mesma vela permanece idêntica.
- `peak_price_since_tp1`, trailing e BE+ permanecem idênticos.
- Qualquer erro na atualização do trace é **capturado e ignorado** pelo
  classificador — telemetria nunca altera decisão.

Instrumentado **apenas** em `check_open_snapshots`. `check_wide_snapshots` (o
namespace `wide`, único sujeito a poda) **não** recebe `p05_path`.

## Deduplicação

Usa `last_candle_ts_ms` do próprio `p05_path`. Só aceita timestamp
**estritamente maior** que o último persistido; ignora candle anterior ao
`created_at` do snapshot e o candle repetido pela sobreposição da janela.

O **classificador continua recebendo a janela original** — a deduplicação é só da
telemetria e não pode mudar o outcome. `last_check_at` **não** é usado como
identidade de candle.

Após restart, o trace é semeado do que foi persistido e continua do último
timestamp — retry é idempotente.

## Finalização e qualidade

| Situação | Status |
|---|---|
| Fechou por candle observado | `FINAL_OBSERVED` + `finalized_at` |
| Time-stop, teto de expiração, símbolo sem dados | `FINAL_PARTIAL` + motivo (mantém o observado) |
| Nunca houve candle válido | `UNAVAILABLE` + motivo, MAE/MFE `null` |

A resolução do snapshot **nunca é atrasada ou impedida** para obter telemetria.

## O que MAE/MFE é — e o que não é

- É o caminho do **SETUP SHADOW**, não da execução real.
- A fonte é **OHLCV de 5 minutos** — não é trajetória tick a tick.
- A **ordem intravela é desconhecida** (high/low sem sequência).
- **Não representa slippage.**
- Não representa necessariamente a trajetória após o fill real.
- **Não deve ser atribuído ao `RealTrade` como se fosse execução real** — um
  `RealTrade` ligado a snapshot pode no máximo expor
  `linked_setup_path_available=true|false`; isso **não** é "MAE/MFE real".
- **Não gera "stop ideal" nem "TP ideal".** É observação, não recomendação.

## Diagnóstico MAE/MFE

Substitui o bloco estático. Baseado **somente** em `p05_path`:

`source="SHADOW_SETUP_PATH_5M"` · `status` · `eligible_resolved` · `observed` ·
`coverage_pct` · `missing` · `unavailable_by_reason` · `mae_r` e `mfe_r`
(mean/median/p75/p90) · `limitations`.

**`USABLE`** exige ≥30 observados **e** cobertura ≥80%. Abaixo disso,
`COLLECTING`. Sem nenhum observado, `UNAVAILABLE`.

## Cobertura contextual

`telemetry.context_coverage`, só nos eixos permitidos (`regime`,
`entry_zone_type`). Para cada eixo: cobertura **global**, **treino**,
**validação**, **teste**, presente, ausente, mínimo 80% e status
`COLLECTING`/`USABLE`.

Reutiliza **exatamente** a ordenação cronológica, o `temporal_split` e o
`axis_coverage` do P05.1/P05.1R — **não cria outro split**. O status é decidido
pela cobertura de **TREINO**: a global nunca a substitui (é exatamente por isso
que a janela de 90d não gerava candidatos mesmo com 86% global).

Usa sempre a janela operacional de **120 dias** do P05.1R e o loader selado de
colunas explícitas. Assim o seletor de 30/60 dias do painel não contradiz o
readiness e a cobertura não fica condicionada ao outcome.

**Não lê `realized_r` e não abre holdout** — só conta presença de feature.

## Slippage e latência

Slippage reutiliza `RealTrade.entry_slippage_pct` (não recalcula, não altera
`RealTrade`): total de trades REAL fechados, válidos, ausentes, cobertura, média,
mediana, p75, p90 e inválidos excluídos por motivo. **Ausência nunca vira zero**;
**zero legítimo continua válido**.

**Latência: `UNAVAILABLE`.** Não existe timestamp durável de decisão→ACK
(`RealTrade` tem `opened_at`, mas não `decision_at` nem `order_ack_at`). Latência
de *fetch* **não é** latência de *execução* — usar uma no lugar da outra seria
afirmação falsa. Medir exigiria instrumentar o hot path, o que está fora desta
fase.

## Gates

Reutiliza `SkipReasonStat` com a linguagem correta: são **eventos agregados** por
(gate, dia), podem conter repetição, **não são oportunidades únicas** e não somam
com executados como verdade absoluta.

O mapa de disponibilidade lista gates com contador, gates sem contador e marca
P04A/P04B explicitamente como `UNAVAILABLE` (abortam dentro do POST, não passam
por `_record_skip`) e P04C como `AVAILABLE`. **Nenhum hook foi adicionado ao
executor** nesta fase; nenhuma identidade de oportunidade foi inventada; nenhum
"lucro perdido" é estimado.

## Retenção

Reutiliza `_retention_snapshot`: dias observados, contagens 30/60/90/120, risco de
retenção, distinção entre **dados jovens** e **dados apagados**, e os namespaces
realmente sujeitos a poda.

Garantido por teste: a telemetria vive **apenas** em snapshots normais
(`check_open_snapshots`); a poda continua **exclusiva** de `wide`/`wide_*`. Nenhuma
evidência do P05 é apagada por esta implementação. **A política de retenção não
foi alterada.**

## API

**Nenhum endpoint novo.** As respostas existentes foram expandidas:

- `GET /api/shadow/assertiveness` → `p05.mae_mfe` e `p05.telemetry`
- `GET /api/strategy/p05/status` → `diagnosis.mae_mfe` e `diagnosis.telemetry`

GET, read-only, fail-soft por seção, clamps existentes preservados, cache
existente reutilizado, erro não envenena cache. **Não** foram criados
`collect-now`, `recompute`, `backfill`, `evaluate`, `apply`, `promote`,
`activate` ou `retry-now`.

## Frontend

Seção **"Qualidade da telemetria"** no `AssertivenessPanel` existente — sem página
nova, sem botão, sem sugestão de alteração de estratégia, sem `[object Object]`,
responsivo, `frontend/dist` intacto.

Mostra, em linguagem simples: cobertura de regime e de zona de entrada **no
treino** (com barra de progresso até 80%), estado do MAE/MFE (coletando ou
utilizável), quantidade observada, cobertura, deslize de preço e sua cobertura,
aviso explícito de que a latência **não é medida**, retenção, e o aviso de que
tudo vem de **velas de 5 minutos**.

## Invariantes

- `holdout_status=SEALED`, `holdout_outcomes_read=false`,
  `holdout_metrics_computed=false`.
- Readiness não lê outcome; cobertura contextual não lê outcome.
- MAE/MFE **não alimenta** a avaliação do P05.1 (garantido por teste: nenhuma
  função de decisão lê `p05_path`).
- Nenhuma reavaliação automática; nenhum experimento criado ou alterado.
- Nenhuma chamada adicional à exchange ou de candle — a telemetria só reaproveita
  a janela que o resolver já buscou.
- Nenhuma tabela, coluna, migration, env ou flag.
- `shadow_trade_service`, `trade_manager_service` e signed services **intactos**.

## Limitações honestas

1. **Só vale daqui para frente.** Snapshots anteriores não têm `p05_path` e nunca
   terão — não há backfill possível sem inventar história.
2. **Granularidade de 5 minutos.** Um movimento que vá ao stop e volte dentro da
   mesma vela aparece no MAE, mas a **sequência** é desconhecida: não dá para
   afirmar o que veio primeiro.
3. **É o caminho do setup, não do trade.** Fill real, slippage, parciais e
   trailing da execução podem divergir do caminho teórico.
4. **A janela do resolver limita a resolução temporal.** Se o resolver rodar com
   intervalo maior que a janela de candles buscada, algumas velas intermediárias
   podem não ser observadas — daí `FINAL_PARTIAL` existir como estado honesto.
5. **Latência continua sem medição** — e continuará até uma fase que instrumente
   o hot path.
6. **Cobertura contextual de treino sobe devagar**, porque o treino é a metade
   cronológica mais **antiga**: só o tempo move esse número.
