# P05 — otimização de estratégia governada por evidência

## Objetivo

Responder, com evidência auditável: onde o bot realmente ganha, onde concentra
perdas, quais filtros protegem, quais podem estar eliminando boas operações, e
se algum ajuste supera o comportamento atual **fora da amostra**.

O P05 não altera estratégia, score, tier, filtros, entry/stop/TP, sizing, nem
qualquer flag do LIVE. Ele produz **evidência e um plano** — a ativação é sempre
manual e humana.

## Antes

Métricas, calibração e learning já existiam (painel de assertividade, backtest,
walk-forward, contrafactual de gates), mas espalhados: não havia um ciclo de vida
único e governado que levasse um candidato do rascunho até a decisão, com
identidade versionada, validação temporal sem leakage e critério de aceite
explícito. Também não havia separação formal entre "o que o bot fez" e "o que um
ajuste teria feito".

## Depois

```
dados resolvidos
  → diagnóstico confiável        (P05A)
  → candidatos limitados         (P05B)
  → validação temporal offline   (P05B)
  → challenger em shadow         (P05C)
  → ELIGIBLE / REJECTED          (P05D)
  → recomendação de ativação MANUAL
```

## Três fontes, nunca misturadas

| Fonte | Contrato | Janela | Papel |
|---|---|---|---|
| **REAL** | `RealTrade(source="auto")` | `closed_at` | verdade financeira da execução |
| **SHADOW** | `RecommendationSnapshot` | `outcome_at` | amostra de setups (base da calibração) |
| **BACKTEST** | `BacktestTrade` | `bar_ts` | evidência histórica **secundária** |

BACKTEST nunca é somado ao REAL como se fossem equivalentes.

## Qualidade de dado (regras duras)

- `OPEN` não entra em métrica final.
- `realized_r = None` **não vira 0** — é excluído e contabilizado.
- NaN/infinito são excluídos e contabilizados.
- Snapshots `expired` sem avaliação justa saem pelo `_not_fast_void()` já
  existente na calibração (reuso, não reimplementação).
- Duplicatas do mesmo trade/snapshot são deduplicadas por identidade.
- Datas sempre UTC timezone-aware; naive é normalizado para UTC.
- Nunca se usa `opened_at`/`created_at` para atribuir resultado encerrado à janela.
- Todo descarte aparece em `data_quality.excluded_by_reason`.

### Breakeven, expired e o contrato de `pnl_usd`

- `breakeven` (R = 0) é reportado **separado de loss**.
- `expired` é reportado **separado de win/loss** (nunca vira "não-win" silencioso).
- **`RealTrade.pnl_usd` já é LÍQUIDO**: `real_trade_service.close_trade` desconta
  `entry_fee` e `exit_fee` (e `tp1_realized_usd` já vem líquido da fee de saída
  do TP1). O P05 reporta esse valor como líquido e mostra as fees **apenas como
  informação** — subtrair de novo seria double-count.

## Métricas

Núcleo puro e determinístico (`compute_evidence_metrics`), com **`None` + motivo**
sempre que a métrica não é estatisticamente calculável:

contagens (válidos, wins, losses, breakeven, expired) · win rate + **intervalo de
Wilson 95%** · expectancy média em R · mediana · soma · desvio-padrão · downside
deviation · profit factor · **Sharpe por trade (explicitamente NÃO anualizado)** ·
max drawdown em R · pior sequência de losses · P&L líquido · entry/exit fees ·
slippage médio + cobertura · TP1/TP2 hit rate · operações por dia · cobertura do
vínculo RealTrade→Snapshot · cobertura de features.

Intervalos de expectancy e de delta usam **bootstrap com seed fixa**
(`P05_RANDOM_SEED`) — mesma amostra ⇒ mesmo intervalo. Só biblioteca padrão
(`random`, `math`, `statistics` não são substituídos por dependência nova).

## Segmentos

SHADOW é segmentado por tier, timeframe, direção, tier×timeframe, símbolo/base,
padrão, sessão UTC, dia da semana, regime, funding sentiment, faixa de score,
faixa de ATR, MTF e entry-zone type. Cada segmento traz count, wins/losses/
neutros, win rate + intervalo, avg R, total R, profit factor, max drawdown e
**confiabilidade**: `INSUFFICIENT` (<10) · `EARLY` (<30) · `USABLE` (<100) ·
`STRONG` (≥100).

A ordenação é por **evidência** (confiabilidade primeiro, depois R total) — não
por win rate cru, para que um segmento com n=3 e 100% de acerto não lidere.

> **Padrões sobrepostos são atribuição, não causalidade.** Um mesmo trade aparece
> em vários eixos e em vários padrões. Segmento pequeno **não** é edge comprovado.

## Eventos de bloqueio (P04) — e o que NÃO se pode afirmar

`SkipReasonStat` guarda **contadores de eventos por (gate, dia)**. Não existe
identidade única de oportunidade nessa tabela. Portanto:

- **Correção aplicada no funil**: `executados + skips` **não** é "candidatos
  únicos". O campo virou `gate_events`, e `candidates_estimated` está marcado
  como `is_estimate: true`.
- **P04A e P04B abortam dentro do POST** e não passam por `_record_skip` — logo
  não têm contador nessa tabela. O P05 reporta `events: null` + motivo, em vez de
  inventar número.
- **P04C** aparece pelo gate `data-freshness`.
- Um bloqueio **não** é "lucro perdido": sem outcome contrafactual ligado ao
  MESMO setup, não há como afirmar custo.

## Enriquecimento de features (sem alterar schema)

`features["p05_context"]` — namespace **versionado** (`schema_version`) gravado no
JSONB já existente de `recommendation_snapshots`. Nenhuma coluna nova.

Persiste, quando **realmente disponível** no instante do snapshot: score, tier,
P(TP1), P(TP2), risk_reward, quote volume, spread, edge_score, edge_tags,
bot_verdict (ok/reason/code), regime, MTF, funding, ATR%, chase_atr,
struct_chase_atr, entry_zone_type, resumo de data freshness, exchange/source e
`observed_at`.

Regras: **nunca inventa valor ausente**, nunca recomputa pós-fato como se fosse o
original, campo ausente permanece `None` (o P05 trata como UNKNOWN, não como
zero). A escrita é aditiva — não apaga chaves antigas de `features`.

### MAE/MFE

**`UNAVAILABLE`**. O histórico não permite reconstrução fiel, e aproximar pelo
preço final criaria afirmação falsa. Instrumentação futura só se puder reutilizar
dados que o resolver já coleta, sem nova chamada de rede.

## Candidatos

Máximo **12 por avaliação**, **um knob por candidato**, **sem grid search**.
Objetivos separados: `LOSS_REDUCTION` e `MORE_OPERATIONS`.

Allowlist (nada de safety/live): `SCORE_MIN`/`SCORE_MIN_V2` (conforme a fórmula
ativa) · `TF_MIN_TIER` · `QUALITY_EDGE_GATE_ENABLED` · `QUALITY_EDGE_MARGIN` ·
`MTF_ALIGNED_MODE` · `MTF_ALIGNED_MIN_COUNT` · `PROXIMITY_MAX_ATR` ·
`STRUCT_CHASE_GATE_ENABLED` · `STRUCT_CHASE_MAX_ATR`.

Um knob só entra quando: valor **champion real descoberto**, feature
correspondente **persistida**, **cobertura ≥ `P05_MIN_FEATURE_COVERAGE_PCT`**,
amostra suficiente e ajuste dentro dos limites conservadores (`max_delta`).

Rejeitado: chave fora da allowlist · boolean/string inválido · NaN/infinito ·
fora dos limites · configuração vazia · mais de um knob · qualquer tentativa de
tocar P04A/B/C, preço executável, depth, slippage, stop, TP, R mínimo, qty,
leverage, exposição, kill switch, portfolio guard, circuit breaker, maker/
fallback, LIVE ou `LEARNING_AUTO_*`.

> O **champion inclui o learning já ativo**. `LEARNING_AUTO_ADJUST` e
> `LEARNING_AUTO_BLOCK` não são knobs de candidato.

### Descoberta do champion

`discover_champion_config()` lê a **mesma env com os mesmos defaults** de
`shadow_trade_service`/`recommendation_service`. Aqueles módulos não são
importados de propósito (carregam SDK de exchange; o P05 é puro/hermético) — o
acoplamento é por **contrato de env**: se um default mudar lá, precisa mudar aqui.

### Identidade e idempotência

`champion_hash`, `candidate_hash` e `dataset_fingerprint` são **SHA-256 de JSON
ordenado**. `experiment_key = champion_hash:candidate_hash:dataset_cutoff`.
Mesmo candidato + champion + cutoff ⇒ **mesma identidade lógica** — retry ou
restart não cria experimento duplicado. Config e hashes são **imutáveis** após o
`DRAFT`.

## Validação temporal (sem leakage)

Ordem cronológica obrigatória. Corte 50/25/25: **treino** gera hipótese,
**validação** seleciona, **teste final permanece intocado** (nunca é usado para
ajustar threshold). Walk-forward com folds cronológicos crescentes como evidência
adicional.

- < `P05_MIN_OFFLINE_RESOLVED` (60) outcomes válidos → `INSUFFICIENT_DATA`
- 60–119 → 4 folds · ≥120 → 6 folds
- teste final precisa de ≥ `P05_MIN_OOS_RESOLVED` (30) outcomes do candidato

Champion e candidato são comparados **sobre o mesmo dataset**. Linhas com dado
ausente para algum componente ativo são excluídas **dos dois lados** (simetria), e
`selects_subset` deixa explícito quando o candidato opera um subconjunto.

## Gates offline

**`LOSS_REDUCTION`** → `OFFLINE_VALIDATED` só com: amostra OOS mínima ·
expectancy candidate > 0 · **limite inferior do IC do delta > 0** · profit factor
≥ champion · max drawdown ≤ champion · operações ≥ 70% do champion · validação e
teste positivos.

**`MORE_OPERATIONS`** → `OFFLINE_VALIDATED` só com: operações ≥ 110% do champion ·
expectancy > 0 · **limite inferior do IC da expectancy > 0** · total R > champion ·
profit factor ≥ 1 · max drawdown ≤ 110% do champion · validação e teste positivos ·
**operações adicionais com expectancy positiva** · nenhuma trava P01–P04 relaxada.

Caso contrário: `REJECTED` ou `INSUFFICIENT_DATA`. **Nenhum candidato válido é um
resultado aceitável** — não se força vencedor.

## Champion × Challenger em shadow (P05C)

Sem divisão aleatória: os dois são avaliados **sobre a MESMA recomendação
observada**. O challenger **não** altera score exibido, **não** altera o tier
usado pelo LIVE, **não** bloqueia recomendação, **não** abre trade, **não** altera
sizing e **não** entra no executor — apenas registra o veredito contrafactual em
`features["p05_experiment"]` (idempotente por `experiment_key`), com
`challenger_status ∈ {ELIGIBLE, BLOCKED, UNKNOWN}`, `reason_code`,
`required_features` e `missing_features`.

**`UNKNOWN` nunca vira `BLOCKED` nem `ELIGIBLE` por fallback.**

Só **um** experimento em `SHADOW` por vez (garantido no serviço com
`with_for_update` + verificação de conflito).

## Decisão governada (P05D)

Estados: `DRAFT` · `INSUFFICIENT_DATA` · `REJECTED` · `OFFLINE_VALIDATED` ·
`SHADOW` · `ELIGIBLE`.

```
DRAFT             → INSUFFICIENT_DATA | REJECTED | OFFLINE_VALIDATED
OFFLINE_VALIDATED → SHADOW
SHADOW            → REJECTED | ELIGIBLE
```

Sem saltos. Experimento decidido **não reabre** — nova evidência gera nova versão
(cutoff diferente ⇒ `experiment_key` diferente). Mudança de status + métricas
ocorre em **transação única**.

Gate de shadow (default): ≥30 outcomes do challenger · ≥14 dias · cobertura ≥90% ·
expectancy > 0 · objetivo offline ainda atendido · nenhum incidente operacional ·
nenhum relaxamento de P01–P04 · métricas **não** podem depender de dados UNKNOWN.

Aprovado ⇒ `status = ELIGIBLE` + `promotion_plan` com diff exato, env atual, env
proposta, evidência, riscos, plano de canário, condição de rollback e valores de
rollback.

> **`ELIGIBLE` significa "pode ser apresentado ao usuário para autorização".**
> **NÃO significa "foi ativado".** Não existe endpoint de promoção.

## Persistência

Uma única tabela nova: **`strategy_experiments`** (registrada em `init_db()` para
`Base.metadata.create_all`). Nenhuma coluna foi adicionada a tabelas existentes.
Índices: `experiment_key` (único), `candidate_hash`, `status`, `created_at`.

## API

Somente leitura e avaliação — **não existe** `promote`, `apply`, `activate-live`,
`execute`, `retry-now` ou `change-size`.

| Método | Rota | Auth | O que faz |
|---|---|---|---|
| GET | `/api/strategy/p05/status` | — | diagnóstico + champion + shadow + gate + flags |
| GET | `/api/strategy/p05/experiments` | — | lista paginada (sem payload gigante) |
| GET | `/api/strategy/p05/experiments/{id}` | — | detalhes e métricas |
| POST | `/api/strategy/p05/evaluate` | admin | gera/avalia candidatos |
| POST | `/api/strategy/p05/experiments/{id}/start-shadow` | admin | `OFFLINE_VALIDATED → SHADOW` (idempotente, 409 em conflito) |
| POST | `/api/strategy/p05/experiments/{id}/evaluate-shadow` | admin | `REJECTED`/`ELIGIBLE`/aguarda amostra |

Clamp: `days` 7–365 · `limit` 1–100 · bootstrap limitado · payload desconhecido
rejeitado. GET analítico é **fail-soft por seção**; POST falha explicitamente e
**sem persistência parcial**. Auth reutiliza `_check_admin_token` (`X-Admin-Token`).

`/api/shadow/assertiveness` ganhou a seção `p05` (reuso, sem painel duplicado).

## Frontend

`AssertivenessPanel.tsx` (expansão mínima, **sem página nova**): "Qualidade da
evidência", "Onde ganha / onde perde", "Champion × Challenger" e "Bloqueios de
segurança (P04)". Linguagem não técnica, responsivo, estados de loading/erro/
vazio — e **nenhum botão de promover ou aplicar**. `frontend/dist` não foi tocado.

## Flags

```
P05_ANALYTICS_ENABLED=true
P05_CHALLENGER_SHADOW_ENABLED=false
P05_MAX_CANDIDATES=12
P05_MIN_OFFLINE_RESOLVED=60
P05_MIN_OOS_RESOLVED=30
P05_MIN_SHADOW_RESOLVED=30
P05_MIN_SHADOW_DAYS=14
P05_MIN_FEATURE_COVERAGE_PCT=80
P05_MIN_SHADOW_COVERAGE_PCT=90
P05_BOOTSTRAP_SAMPLES=1000
P05_RANDOM_SEED=20260827
```

Expostas em `/api/strategy/p05/status`. **Defaults não alteram o LIVE.** Não
existe flag de auto-promotion.

## Rollout e rollback

**Rollout**: o P05 nasce inerte para a execução — `P05_ANALYTICS_ENABLED=true` só
liga leitura/diagnóstico, e `P05_CHALLENGER_SHADOW_ENABLED=false` mantém o
challenger desligado. Um experimento só entra em shadow por ação administrativa
explícita.

**Rollback**: desligar `P05_ANALYTICS_ENABLED` neutraliza o diagnóstico; o
`promotion_plan` de cada candidato `ELIGIBLE` carrega os **valores de rollback**
(a env atual do champion) para reverter uma ativação manual.

## Limitações honestas

1. **SHADOW é amostra de setups, não de execução real** — fill, slippage e
   disponibilidade de margem podem diferir do contrafactual.
2. **MAE/MFE indisponível** (ver acima).
3. **P04A/P04B não têm contador de eventos** em `skip_reason_stats`.
4. **Correlação não é causalidade**: padrões sobrepostos são atribuição; um
   candidato que seleciona subconjunto pode estar apenas escolhendo um regime.
5. **Componentes com cobertura baixa são desligados dos dois lados** — a
   comparação fica simétrica, mas não reproduz o gate completo do LIVE.
6. **O champion é descoberto por contrato de env** — mudar um default no
   `shadow_trade_service` sem refletir aqui desalinha a comparação.
7. **Nada disso é promessa de lucro.** Evidência fora da amostra reduz, mas não
   elimina, o risco de o ajuste não se sustentar em regime novo.

## Invariantes

- CHAMPION LIVE inalterado; nenhum challenger ativado no LIVE.
- Nenhuma estratégia promovida automaticamente.
- P01–P04 inalterados; `LIVE_SIZE_MULT` inalterado.
- MAKER, fallback MARKET, TF upgrade e pyramiding não foram ativados.
- Nenhuma ordem criada ou cancelada; nenhuma exchange/banco externo acessado.
- O serviço P05 não importa SDK de exchange e não faz rede (garantido por teste).
