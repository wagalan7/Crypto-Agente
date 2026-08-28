# P05.1 — candidatos contextuais de evidência (ANALYTICS_ONLY)

## Objetivo

Corrigir uma limitação concreta observada no P05: os candidatos eram **globais**
(essencialmente `SCORE_MIN`), e mexer no piso vale para o mercado inteiro. O
P05.1 pergunta outra coisa — existe **contexto** em que vale bloquear, e contexto
em que vale afrouxar 2 pontos?

- reduzir perdas **somente** em contextos comprovadamente ruins;
- aumentar operações **somente** em contextos comprovadamente bons;
- **nunca** baixar `SCORE_MIN` globalmente;
- **nunca** relaxar P01–P04;
- **nunca** transformar evidência em alteração automática de estratégia.

## Antes (medido com 90 dias reais)

| Candidato | Resultado |
|---|---|
| `SCORE_MIN` 57 / 59 | `LOSS_REDUCTION` rejeitado |
| `SCORE_MIN` 53 | não atingiu +10% de operações |
| `SCORE_MIN` 51 | passou na validação, mas o **teste final levou o drawdown de 4R para 7R** |

Os 4 experimentos terminaram `REJECTED`. A leitura honesta: baixar o piso
globalmente troca perdas por volume ruim — o holdout pegou exatamente isso.

## Depois

```
dados históricos
  → contexto persistido
  → regra contrafactual pura
  → validação temporal
  → resultado offline
  → decisão manual futura
```

Candidatos passam a ser **condicionais**: bloquear contexto negativo, ou afrouxar
o piso em exatamente 2 pontos **apenas** no contexto positivo. O teste final
continua intocado até a seleção do finalista.

> **O P05.1 não executa a regra no bot real.** É extensão de análise offline —
> não é integração do contexto ao executor.

## Marcação obrigatória

Toda regra contextual carrega, na resposta e em `offline_metrics`:

```
phase           = "P05.1"
execution_mode  = "ANALYTICS_ONLY"
promotable      = false
shadow_supported = false
```

## Caminho de dados (o mesmo do P05)

Somente snapshots **SHADOW resolvidos**, com features persistidas no momento da
recomendação, `realized_r` válido, timestamps válidos, deduplicação e exclusões
já contabilizadas pelo núcleo existente. REAL, SHADOW e BACKTEST **não** se
misturam na mesma comparação. Nenhuma feature é recomputada pós-fato; ausência de
feature é **UNKNOWN** e é excluída **simetricamente dos dois lados**.

## Eixos permitidos nesta fase

Apenas **`regime`** e **`entry_zone_type`**, com os valores exatamente como
persistidos.

Fora nesta fase (de propósito): símbolo, base, padrão individual, direção,
horário, dia da semana, score_bin, ATR band e qualquer eixo de alta cardinalidade
ou amostra baixa — evitam overfitting, múltiplas comparações e regras difíceis de
operar.

`ALT_DANGER` e `limit_pullback` aparecem nos exemplos apenas como **hipóteses
observadas nos dados**, não como vencedores pré-definidos: nada é hardcodado.

Um eixo/segmento só é usado quando, **no treino**: cobertura do eixo ≥
`P05_MIN_FEATURE_COVERAGE_PCT`, ≥ 30 observações no segmento, confiabilidade ≥
`USABLE`, valores finitos e não nulos. Nos estágios seguintes exige-se ≥ 20
observações do contexto na validação e ≥ 20 no teste.

## Schema `CONTEXT_RULE`

`CONTEXT_RULE` **não** entra na `KNOB_ALLOWLIST` do P05 — tem validador separado
(`validate_context_rule`). `candidate_config` contém exatamente uma chave lógica:

```json
{ "CONTEXT_RULE": { "schema_version": 1, "axis": "regime",
                    "value": "ALT_DANGER", "action": "BLOCK" } }
```

```json
{ "CONTEXT_RULE": { "schema_version": 1, "axis": "entry_zone_type",
                    "value": "limit_pullback", "action": "SCORE_DELTA",
                    "score_delta": -2.0 } }
```

Regras: exatamente um `CONTEXT_RULE` · `axis` só `regime` ou `entry_zone_type` ·
`value` string não vazia · `action` só `BLOCK` ou `SCORE_DELTA` · `BLOCK` não
aceita `score_delta` · `SCORE_DELTA` aceita **somente −2.0** · sem NaN/infinito ·
sem dois eixos · sem duas ações · sem qualquer campo de sizing, risco, stop, TP,
maker ou execução. O **hash inclui o schema completo** da regra.

## Semântica das ações

Funções puras e testáveis: `validate_context_rule`, `context_value`,
`contextual_eligibility`.

`context_value` lê **somente** `features["regime"]` e
`features["entry_zone_type"]`; ausente ou vazio ⇒ `UNKNOWN`.

**`BLOCK`** — bloqueia somente quando o contexto corresponde; não altera os demais
contextos; **UNKNOWN nunca vira BLOCKED**.

**`SCORE_DELTA`** — baixa o piso em exatamente 2 pontos **somente no contexto**,
comparando com o **score efetivo** (`_execution_score`, com os adjusters), não com
o score bruto. Todos os demais gates permanecem iguais. Crucialmente: **se a linha
estava bloqueada por qualquer motivo que não o score-min, a regra não a reativa** —
R:R, P(TP1) e liquidez (via `bot_verdict`), P04A/P04B/P04C, ATR, funding,
proximity, struct-chase, tempo, tier e risco/exposição/cooldown/circuit breaker
continuam valendo. Se não for possível provar que só o score barrou, o veredito é
`UNKNOWN` — nunca um passe inventado.

A `eligibility` do P05 global **não foi alterada**: a regra contextual atua por
cima dela, reutilizando o núcleo sem duplicá-lo.

## Geração (somente treino)

`generate_contextual_candidates(champion, train_rows)`. A validação e o teste
**nunca** são usados para descobrir contexto, escolher valor, escolher ação,
escolher quantidade de candidatos ou escolher vencedor.

- `LOSS_REDUCTION`: segmentos com expectancy **negativa** → `BLOCK`, máx. **4**.
- `MORE_OPERATIONS`: segmentos com expectancy **positiva** → `SCORE_DELTA` fixo em
  −2.0, máx. **4**.
- Total máx. **8**, nunca acima de `P05_MAX_CANDIDATES`. Ordenação determinística
  (expectancy, depois amostra, depois nome). Sem grid search, sem combinar dois
  contextos, sem candidato aleatório.

Cada candidato registra eixo, valor, ação, amostra no treino, expectancy,
confiabilidade, cobertura e o motivo da geração; rejeições registram o motivo
(`AXIS_COVERAGE_TOO_LOW`, `TRAIN_SEGMENT_TOO_SMALL`, `RELIABILITY_BELOW_USABLE`,
`CONTEXT_DIRECTION_MISMATCH`, `INVALID_CONTEXT_RULE`).

Sem contexto elegível: **não se inventa candidato** — retorna
`NO_CONTEXT_WITH_SUFFICIENT_EVIDENCE` e nada é persistido.

## Validação temporal

Reutiliza `temporal_split` e `walkforward_folds`. Fluxo obrigatório:

1. treino gera hipóteses;
2. treino **congela** cobertura e componentes;
3. validação compara candidatos;
4. validação escolhe no máximo **um finalista por objetivo**;
5. somente o finalista abre o teste final;
6. o teste permanece intocado até a seleção.

O teste nunca cria candidato, escolhe contexto/ação/componente, altera threshold
ou altera a regra. Alterar o holdout muda apenas o resultado final do finalista —
**nunca** a lista ou os hashes gerados no treino (coberto por teste).

Comparação **pareada pela mesma oportunidade**
(`bootstrap_paired_membership_delta_ci`): champion e candidato compartilham a
amostra, então IC independente seria incorreto.

## Gates (aplicados em validação **e** teste)

**Comuns**: amostra OOS mínima · nenhum dado UNKNOWN usado como aprovação ·
amostra afetada mínima (≥ 20) · expectancy do candidato > 0 e `sum_r` > 0 ·
nenhuma regressão material negativa · nenhum gate P01–P04 relaxado.

**`LOSS_REDUCTION`**: IC pareado do delta com limite inferior > 0 · profit factor
≥ champion · drawdown ≤ champion · operações ≥ 70% do champion · contexto
bloqueado com expectancy negativa · **IC superior das evitadas < 0**.

**`MORE_OPERATIONS`**: operações ≥ 110% do champion · IC da expectancy com limite
inferior > 0 · total R > champion · profit factor ≥ 1 · drawdown ≤ 110% do
champion · operações adicionadas com expectancy > 0 · **IC das adicionadas com
limite inferior > 0** · IC pareado do delta ≥ 0.

Qualquer critério que falhe ⇒ `REJECTED` ou `INSUFFICIENT_DATA`. **Nunca se força
vencedor.** O caso `SCORE_MIN=51` permanece rejeitado: a regra de drawdown do
holdout continua valendo.

## Persistência

Reutiliza `StrategyExperiment` — **nenhuma tabela nova, nenhuma coluna nova**.
Dentro do JSON existente `offline_metrics`: `phase`, `execution_mode`,
`promotable`, `shadow_supported`, `context_evidence`, `selection`, `checks`,
`split`, `validation`, `test`.

Reutiliza `experiment_key`, `candidate_hash`, `dataset_fingerprint`, o advisory
lock, o upsert e a idempotência já existentes. Repetir a mesma avaliação com o
mesmo cutoff não duplica experimento, não muda a configuração congelada, não muda
o hash e não cria segunda linha.

## Ciclo de vida

Uma regra contextual pode chegar a `OFFLINE_VALIDATED` **apenas como resultado
analítico**. Ela **não** pode ir para Shadow:

- `start-shadow` rejeita com `P051_ANALYTICS_ONLY`;
- `evaluate-shadow` rejeita com `P051_ANALYTICS_ONLY`.

O bloqueio reconhece o experimento por `candidate_config.CONTEXT_RULE` **ou** por
`phase = P05.1` / `execution_mode = ANALYTICS_ONLY` / `shadow_supported = false`.

**Não existe `promotion_plan` executável para P05.1.** Havendo resultado
promissor, a resposta informa: *"Requer uma fase posterior de integração do
contexto ao executor. Não é aplicável ao LIVE."*

## API

Adicionado **somente**:

```
POST /api/strategy/p05/contextual-evaluate
```

Auth `X-Admin-Token` (reutiliza `_check_admin_token`) · `days` com clamp 7–365 ·
sem acesso à exchange · sem alteração LIVE · resposta compacta · erros explícitos
· sem persistência parcial.

A resposta informa `phase`, `execution_mode`, `champion`, `dataset_fingerprint`,
`sample`, `data_quality`, candidatos, rejeitados, finalistas, motivos, limitações
e `live_untouched = true`.

`POST /api/strategy/p05/evaluate` **não teve comportamento alterado**.
`GET /api/strategy/p05/status` ganhou o resumo: `contextual_experiments`,
`contextual_offline_validated`, `contextual_rejected`,
`contextual_analytics_only`.

**Não** foram criados: `execute`, `apply`, `promote`, `activate-live`,
`change-size`, `enable-context`, `retry-now`.

## Frontend

Reutiliza o `AssertivenessPanel` — nenhuma página nova, nenhuma ação de promoção
ou ativação. A configuração passa a ser descrita em texto legível (eixo, valor e
ação), **nunca `[object Object]`**, e uma regra contextual exibe o aviso
**"somente análise"**. `frontend/dist` não foi editado.

## Flags

**Nenhuma variável nova.** Preservados: `P05_ANALYTICS_ENABLED=true`,
`P05_CHALLENGER_SHADOW_ENABLED=false`, `MAKER_ENTRY_ENABLED=false`,
`TF_UPGRADE_ENABLED=false`, `PYRAMIDING_ENABLED=false`, P04A/P04B/P04C sem
alteração, `LIVE_SIZE_MULT` sem alteração.

## Limitações honestas

1. **Contexto não é causalidade.** `regime` e `entry_zone_type` podem estar apenas
   rotulando condições de mercado correlacionadas — a regra descreve, não explica.
2. **Segmento pequeno não é edge.** Por isso a amostra mínima é exigida nos três
   estágios, e a confiabilidade precisa ser ao menos `USABLE`.
3. **`SCORE_DELTA` mantém a banda do quality-edge no piso do champion.** Num
   executor real com piso menor, a banda marginal desceria junto. O gate está
   desligado por padrão, então na prática não há divergência hoje — mas é uma
   diferença conhecida entre a simulação e uma futura integração.
4. **Componentes com cobertura baixa ficam desligados dos dois lados**: a
   comparação é simétrica, mas não reproduz o gate completo do LIVE.
5. **O P05.1 é `ANALYTICS_ONLY`**: não existe Shadow, não existe promoção
   automática, e a integração ao executor será uma fase posterior.
6. **A amostra é de setups (SHADOW)**, não de execução real — fill, slippage e
   disponibilidade de margem podem diferir.

## Invariantes

- Nenhum candidato contextual entra no executor.
- Nenhum candidato contextual altera score real, tier real ou sizing.
- Nenhuma ordem criada ou cancelada; nenhuma exchange acessada; nenhum SDK ou
  rede utilizados (garantido por teste de arquitetura).
- P05 global inalterado; `P05_CHALLENGER_SHADOW_ENABLED` continua `false`.
- Nenhuma estratégia LIVE alterada; P01–P04 inalterados.
