# P05.1R — monitor de prontidão de evidência (SOMENTE LEITURA)

## Objetivo

Responder **"já dá para reavaliar?"** — nunca **"o candidato é bom?"**.

O monitor conta quanto dado existe, quantas oportunidades cada regra já congelada
realmente afetaria, quanto falta para os mínimos estatísticos, a velocidade de
acumulação, quando uma nova avaliação passa a fazer sentido, e se há risco de os
dados sumirem antes disso.

```
experimentos P05.1 existentes
  → regras congeladas
  → contagem de novas oportunidades
  → cobertura
  → projeção de amostra
  → readiness (somente leitura)
```

## Antes

A decisão de reavaliar dependia de estimativa manual. Candidatos **refutados** e
candidatos **limitados por amostra** apareciam juntos como `REJECTED`, sem
distinção — o que convida a "esperar mais dados" para uma hipótese que os dados já
contrariaram. Não havia ETA nem visibilidade de retenção.

## Depois

Contagem real por hipótese · distinção `SAMPLE_LIMITED` / `REFUTED_LAST_RUN` /
`MIXED` · ETA conservadora (e ausente quando não cabe) · retenção observável ·
**holdout selado** · zero alteração no LIVE.

## Garantias duras

### Holdout SELADO

O monitor **nunca** lê `realized_r` e **nunca** calcula métrica de outcome
(expectancy, profit factor, drawdown, win rate, IC). Nenhuma função de avaliação
ou gate é chamada. Nenhum `offline_metrics.test` é preenchido, nenhum `withheld`
muda, nenhum finalista é aberto.

> **Contar linhas cronológicas não é abrir o holdout.** O monitor só precisa saber
> *quantas* oportunidades existem, não *quanto renderam*.

Como isso é garantido, não apenas prometido:

- `_load_readiness_rows` seleciona **colunas explícitas** (`id`, `symbol`,
  `timeframe`, `tier`, `direction`, `score`, `features`, `created_at`,
  `outcome_at`) — `realized_r` não é sequer carregado do banco.
- Os dicionários de linha **não contêm** a chave `realized_r`.
- Teste com **sentinela** (`_SealedRow`) que levanta `AssertionError` se
  `realized_r` for acessado por qualquer caminho do monitor.
- Teste de código que proíbe acesso (`["realized_r"]`, `.realized_r`,
  `get("realized_r")`, `RS.realized_r`) dentro do bloco P05.1R.
- Teste que proíbe o bloco de chamar `compare_contextual`,
  `evaluate_contextual_gate`, `evaluate_contextual_candidate_offline`,
  `evaluate_offline_gate`, `compute_evidence_metrics`, `bootstrap_*`,
  `temporal_split`, `walkforward_folds` ou `_upsert_experiment`.

Toda resposta carrega `holdout_outcomes_read=false`,
`holdout_metrics_computed=false` e `holdout_status="SEALED"`.

### Zero escrita

Nenhum `INSERT`/`UPDATE`/`DELETE`, nenhum `commit`/`flush`/`add`/`delete`, nenhuma
alteração de `StrategyExperiment` ou `RecommendationSnapshot`, nenhuma tabela,
coluna ou migration. Verificado por teste de código **e** por teste funcional com
uma sessão falsa que levanta exceção em qualquer tentativa de escrita.

## Fonte de dados

Apenas `RecommendationSnapshot` **resolvido** (mesmos filtros do P05:
`status in SNAP_RESOLVED`, `outcome_at` não nulo, `_not_fast_void()`), usado
somente para identidade, timestamps, contexto persistido, score/features
necessários à elegibilidade, contagem e cobertura.

Não usa `RealTrade`, `BacktestTrade`, `SkipReasonStat` como oportunidade,
"executados + skips", dados de exchange ou rede. `UNKNOWN` continua `UNKNOWN` e é
contado separadamente.

## Definição de "afetada"

Uma linha é **afetada** apenas quando a regra realmente mudaria o veredito:

| Ação | Afetada quando |
|---|---|
| `BLOCK` | contexto corresponde **e** champion = `True` **e** contextual = `False` |
| `SCORE_DELTA` | contexto corresponde **e** champion = `False` **e** contextual = `True` |

Para `SCORE_DELTA`, `contextual_eligibility` (reutilizada, não duplicada) já
garante que a diferença veio **exclusivamente do score**: linha barrada por R:R,
P(TP1), liquidez, P04A/B/C, ATR, funding, proximity, struct-chase, tempo, tier,
risco, exposição ou cooldown **nunca** é reativada nem contada como oportunidade.

Se champion ou contextual retornar `UNKNOWN`, a linha **não** conta como afetada —
incrementa `unknown` separadamente.

## Contagens e projeção

Por janela (30/60/90/120 dias): `total_resolved`, `champion_eligible`,
`candidate_eligible`, `affected`, `unknown`, `context_present`,
`context_coverage_pct`, `observed_span_days`, `oldest_resolved_at`,
`newest_resolved_at`.

A projeção usa a **mesma proporção cronológica do `temporal_split`** (50/25/25) e
**apenas conta linhas**: `prospective_train_affected`,
`prospective_validation_affected`, `prospective_test_affected`,
`prospective_candidate_oos_count`.

## Classificação da última falha

Usa **somente os checks já persistidos** — nada é reavaliado. O prefixo de
estágio (`validacao_`/`teste_`) é removido, o que também deduplica (no preview, os
checks `teste_` são a validação reavaliada).

| Classificação | Quando |
|---|---|
| `SAMPLE_LIMITED` | só falhas de amostra/IC largo |
| `REFUTED_LAST_RUN` | só falhas substantivas (direção econômica contrariada) |
| `MIXED` | falhas de amostra **e** substantivas ao mesmo tempo |
| `INSUFFICIENT_METADATA` | checks ausentes ou não reconhecidos |

Falhas **substantivas**: `contexto_bloqueado_negativo` (as evitadas tinham
expectancy positiva), `operacoes_adicionais_positivas` (as adicionadas tinham
expectancy negativa), `profit_factor_*`, `drawdown_*`, `total_r_maior`,
`expectancy_candidate_positiva`, `sem_segmento_material_negativo`.

Falhas de **amostra**: `amostra_afetada_minima`, `sem_aprovacao_por_unknown`,
`operacoes_min_70pct`, `operacoes_min_110pct` e os `ci_*`.

Regras: `REFUTED_LAST_RUN` **nunca** recebe "execute assim que atingir 20".
`MIXED` informa explicitamente que mais quantidade, sozinha, não resolve a
contradição. Só `SAMPLE_LIMITED` recebe ETA. **O status persistido do experimento
nunca é alterado.**

### Aplicado aos 5 experimentos reais

| Contexto | Ação | Classificação |
|---|---|---|
| `entry_zone_type=market` | BLOCK | **MIXED** (evitadas deram +0,075R — direção contrariada) |
| `regime=NORMAL` | SCORE_DELTA | **MIXED** (adicionadas −0,47R) |
| `entry_zone_type=limit_ob` | SCORE_DELTA | **MIXED** (adicionadas −1,0R) |
| `regime=ALT_RISK_OFF` | SCORE_DELTA | **SAMPLE_LIMITED** |
| `entry_zone_type=limit_fvg_fill` | SCORE_DELTA | **SAMPLE_LIMITED** |

Ou seja: 3 das 5 hipóteses **não** ganham ETA — mais dado não as salva.

## Readiness

Valores: `WAITING_FOR_DATA` · `READY_FOR_REEVALUATION` · `REFUTED_LAST_RUN` ·
`MIXED_EVIDENCE` · `RETENTION_AT_RISK` · `INSUFFICIENT_METADATA` · `UNKNOWN`.

`READY_FOR_REEVALUATION` exige **todos**: última falha = `SAMPLE_LIMITED` ·
cobertura do contexto ≥ `P05_MIN_FEATURE_COVERAGE_PCT` ·
`prospective_validation_affected` ≥ 20 · `prospective_test_affected` ≥ 20 ·
`prospective_candidate_oos_count` ≥ `P05_MIN_OOS_RESOLVED` · `UNKNOWN` não
compromete a cobertura · retenção suficiente.

> **Significa apenas: "há amostra suficiente para executar a avaliação de novo".**
> **Não significa** candidato aprovado, Shadow autorizado, estratégia pronta,
> ganho esperado ou promoção — a própria resposta carrega esse `does_not_mean`.

## ETA

Calculada só sobre novas oportunidades **após o `dataset_cutoff`**. Exige ao menos
7 dias observados; `daily_rate = afetadas / dias`. Taxa zero ou janela curta ⇒
`eta_days = null`. `missing_to_minimum` nunca é negativo e o arredondamento é
**para cima**.

O divisor é conservador: apenas ~25% das novas linhas caem na fatia de
validação/teste, então `eta_days = ceil(missing / (daily_rate × 0.25))`. Todo
retorno traz `eta_reason`, e **nenhuma data exata é prometida**.

Para `REFUTED_LAST_RUN` e `MIXED`: `eta_days = null` e `daily_rate = null`, com a
razão explícita de que quantidade sozinha não é critério suficiente.

## Retenção

Auditoria (`rg`) encontrou exatamente duas rotinas de poda, ambas em
`snapshot_service`:

- `save_wide_display_snapshots` → `DELETE ... WHERE status == "wide"`
- `check_wide_snapshots` → `DELETE ... WHERE status IN (wide_*)`

**Ambas são exclusivas do namespace `wide`.** O P05 usa
`won_tp1 | won_tp1_be | won_tp2 | lost | expired` — conjuntos **disjuntos**. Logo
**nenhuma política apaga evidência do P05**, e `retention_at_risk = false`.

O relatório expõe `observed_retention_days`, `rows_older_than_{30,60,90,120}d`,
`retention_policy_detected`, `retention_policy_days`,
`retention_policy_applies_to_p05`, `retention_warning`, `history_status` e
`history_note`, distinguindo explicitamente:

- **`YOUNG_HISTORY`** — histórico ainda jovem; **não é evidência de deleção**;
- **`MATURE`** — cobre ≥ 120 dias;
- **`UNKNOWN`** — sem linhas na janela consultada.

Se algum dia existir política < 120 dias aplicável ao P05, o readiness vira
`RETENTION_AT_RISK`. **A retenção não foi alterada nesta fase** — o ponto exato
está documentado acima para uma fase separada, se for necessário.

As constantes (`WIDE_TRACKING_ENABLED`, `SNAPSHOT_EXPIRY_HOURS`,
`WIDE_DISPLAY_TTL_HOURS`) são lidas por **contrato de env**: `snapshot_service`
não é importado porque carrega `push_service`, que faz rede — e o monitor é
hermético.

## API

Somente um endpoint, **GET**:

```
GET /api/strategy/p05/readiness?days=120&limit=20&experiment_id=<opcional>
```

`days` clamp 30–365 · `limit` clamp 1–100 · `experiment_id` opcional · **sem auth
administrativa** (segue o padrão do status analítico) · **zero escrita** · fail-soft
por seção. Resposta compacta com `ok`, `phase`, `read_only`, `holdout_status`,
`summary`, `retention`, `experiments`, `computed_at`.

Nenhum endpoint POST foi criado. `/api/strategy/p05/status` ganhou apenas um
resumo pequeno: `readiness_by_status`, `next_recommended_check`,
`retention_warning` e `holdout_status`.

**Cache** single-flight curto reutilizando o TTL do P05, com chave
`(days, limit, experiment_id)`. Erro **não** envenena o cache.

## Frontend

Seção **"Prontidão para nova avaliação"** dentro do `AssertivenessPanel` existente
— sem página nova, sem editar `frontend/dist`. Mostra contexto, última conclusão,
afetadas atuais, mínimo, quanto falta, velocidade/dia, ETA, cobertura, retenção e
o selo **🔒 holdout protegido**.

Linguagem: "amostra suficiente para reavaliar". **Nunca** "candidato pronto para
ativar", "lucro esperado" ou "oportunidade perdida". **Sem botão** de avaliar,
executar, ativar, promover ou iniciar Shadow.

## Limitações honestas

1. **O monitor não julga qualidade.** `READY_FOR_REEVALUATION` diz que há amostra
   para rodar a avaliação — o resultado pode continuar sendo `REJECTED`.
2. **A projeção 50/25/25 é aproximação.** A janela real é recalculada na
   avaliação; a contagem prospectiva indica ordem de grandeza, não garantia.
3. **A ETA assume ritmo estável.** Regime de mercado, volume de scan e mudanças no
   champion alteram a taxa; por isso o divisor é conservador e nenhuma data exata
   é prometida.
4. **Componentes congelados vêm do experimento original.** Se um experimento antigo
   não os tiver, o monitor recomputa e sinaliza em `active_components_source` —
   a comparação fica aproximada.
5. **Mudança no champion invalida a contagem.** As regras são congeladas, mas o
   champion é lido do env atual; se um knob mudar, a contagem de afetadas muda
   junto e a reavaliação gerará novo `experiment_key`.
6. **Retenção é observação, não garantia.** Ausência de política detectada não
   impede deleção manual ou perda de dados fora do código.

## Invariantes

- Holdout selado: `realized_r` nunca lido, nenhuma métrica de outcome calculada.
- Zero escrita: nenhum experimento criado, alterado ou reavaliado.
- Nenhuma tabela, coluna, migration ou env nova.
- Sem rede, sem SDK de exchange, sem provider, sem executor.
- `P05_CHALLENGER_SHADOW_ENABLED` continua `false`; P05.2 não foi implementado.
- Champion LIVE, `SCORE_MIN`, `LIVE_SIZE_MULT` e P04A/B/C inalterados.
