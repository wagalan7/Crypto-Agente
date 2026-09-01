# P05.2B — laboratório offline de hipóteses de stop (SOMENTE VALIDAÇÃO)

## Objetivo

Preparar — e deixar funcionando desde já — o laboratório que avaliará hipóteses
destinadas a **reduzir stops** quando o P05.2A encontrar padrões adversos
persistentes.

> Esta fase **não promove, não ativa, não executa**. Não cria candidato
> executável, `StrategyExperiment`, Shadow challenger ou LIVE challenger; não
> altera score, tier, gate, stop, TP ou sizing; e **não abre o holdout final**.

## Antes

O P05.2A já classificava contextos como `PERSISTENT_ADVERSE`, mas não havia
nenhuma avaliação **stop-specific**: a pergunta "e se as recomendações desse
contexto não fossem selecionadas?" não tinha resposta quantificada, com IC e
custo em operações vencedoras.

## Depois

Contrafactual pareado sobre as mesmas oportunidades · gate exclusivamente de
**validação** · teto de 4 hipóteses · uma única forma de hipótese · **teste final
selado** · nenhum caminho de execução, promoção ou Shadow.

## Estado com os dados atuais

O diagnóstico P05.2A em produção fecha hoje em
`patterns_verdict="NO_PERSISTENT_STOP_PATTERN"`. Nesse estado o laboratório
devolve, por construção:

```
status      = NO_ELIGIBLE_HYPOTHESIS
reason_code = NO_PERSISTENT_ADVERSE_PATTERN
candidates  = []      rejected = []
```

Nenhum candidato, nenhum vencedor, nenhum experimento. O comportamento com
padrões persistentes é validado com **dados sintéticos herméticos**.

## Origem das hipóteses

Só entra padrão classificado pelo P05.2A como **`PERSISTENT_ADVERSE`**. São
recusados `MIXED`, `SAMPLE_LIMITED`, `LOW_COVERAGE`, `NOT_ADVERSE`, eixo ausente
e contexto com cobertura insuficiente. **Não existe hipótese manual arbitrária.**

## Eixos

| Permitidos | Bloqueados nesta fase |
|---|---|
| `tier` · `timeframe` · `direction` · `session_utc` · `regime` · `funding_sentiment` · `score_bin` · `atr_band` · `mtf_aligned` · `entry_zone_type` | `base` (alta cardinalidade) · `patterns` (multi-rótulo sobreposto) · `tier_timeframe` (combinação) · `day_of_week` · qualquer eixo fora da lista |

## A única hipótese

`STOP_CONTEXT_BLOCK` — *"como ficariam os resultados históricos observados se as
recomendações deste contexto persistente não fossem selecionadas?"*.

Sem grid search. Não gera alteração de stop, de TP, `SCORE_DELTA`, delta de tier,
tamanho de posição, horário novo, parâmetro contínuo, combinação de dois
contextos nem duas regras na mesma hipótese.

**Máximo 1 hipótese por padrão, 4 no total**, em ordem determinística pela força
da evidência na **validação** (lift de stop → exposição → eixo → valor).

## Comparação pareada

Champion observado × hipótese sobre as **mesmas oportunidades**:

- contexto **igual** ao valor ⇒ removido pelo candidato;
- contexto **diferente** ⇒ permanece;
- contexto **ausente/UNKNOWN** ⇒ excluído dos **dois** lados.

Calculado separadamente em treino e validação: exposições · stops · stop rate ·
Wilson 95% · expectancy · soma R · mediana R · profit factor · max drawdown ·
pior sequência · operações mantidas/removidas · stops evitados · wins removidos ·
saídas protetivas removidas · expired removidos · expectancy das removidas com IC
bootstrap · delta pareado de expectancy com IC · delta da taxa de stop com IC
bootstrap · regressões materiais de segmento · percentual de operações
preservadas.

Bootstrap com a **seed fixa já usada pelo P05** (`P05_RANDOM_SEED`): mesma
amostra, mesmo IC.

## Gate — somente validação

Status possíveis: `NO_ELIGIBLE_HYPOTHESIS` · `INSUFFICIENT` · `REJECTED` ·
`VALIDATION_SUPPORTED`.

`OFFLINE_VALIDATED`, `SHADOW`, `ELIGIBLE`, `PROMOTED` e `ACTIVE` **não existem**
nesta fase (verificado por teste).

`VALIDATION_SUPPORTED` exige **todos**:

1. origem `PERSISTENT_ADVERSE`;
2. eixo permitido;
3. cobertura do eixo ≥80% em treino **e** validação;
4. amostra afetada na validação ≥ `P051_MIN_AFFECTED` (20);
5. preserva ≥70% das operações do champion;
6. expectancy do candidato na validação > 0;
7. soma R do candidato > 0;
8. profit factor não pior que o champion;
9. max drawdown não pior que o champion;
10. expectancy das removidas < 0;
11. IC superior da expectancy das removidas < 0;
12. IC inferior do delta pareado de expectancy > 0;
13. taxa de stop do candidato menor;
14. IC superior do delta da taxa de stop < 0;
15. nenhuma regressão material em segmento confiável.

Qualquer falha ⇒ `REJECTED` (ou `INSUFFICIENT` quando faltam cobertura,
amostra ou significância estatística e não existe reprovação substantiva).
Uma reprovação econômica real, como expectancy do candidato negativa,
prevalece sobre uma insuficiência simultânea de amostra. Cobertura global
abaixo do piso impede o julgamento dos checks derivados. **Nenhum critério é
afrouxado para produzir candidato.**

## Holdout selado

O laboratório recebe **apenas** treino e validação, mais os padrões persistentes
derivados desses dois estágios. Nunca recebe `split["test"]`, ids do teste,
status, `realized_r` ou métrica do teste — nem para um finalista.

A resposta declara `holdout_status="SEALED"`, `holdout_outcomes_read=false`,
`holdout_metrics_computed=false` e `requires_future_holdout_review=true`. Testes
com **sentinela** explodem se qualquer outcome selado for tocado.

## Contrato de saída

`offline_lab` foi acrescentado ao `stop_diagnosis` existente:

```
phase="P05.2B" · execution_mode="ANALYTICS_ONLY" · read_only=true
executable=false · promotable=false · shadow_supported=false
holdout_status="SEALED" · status · reason_code · detail
source_patterns · candidates · rejected · limitations · computed_at
```

Cada candidato traz hash canônico, eixo, valor, tipo `STOP_CONTEXT_BLOCK`,
evidência de origem, treino, validação, checks, wins removidos, stops evitados,
riscos, `executable=false` e `requires_future_holdout_review=true`.

**Não** existe `candidate_config` executável, `promotion_plan`, endpoint, flag ou
ação de aplicação.

## Persistência e cache

Read-only: zero `session.add`, `commit`, `flush`, `merge`, `delete` e `update`;
nenhum `StrategyExperiment`, snapshot ou `RealTrade` criado ou alterado.

O laboratório roda **dentro** de `build_stop_diagnosis`, então usa o cache
single-flight já existente do P05.2A — não há cache paralelo. Falha do
laboratório não derruba o P05.2A: devolve `offline_lab.status="UNAVAILABLE"` e o
resultado parcial **não entra no cache**.

**Nenhum endpoint novo, nenhum POST, `main.py` não foi modificado.**

## Frontend

Dentro do bloco existente "Por que as recomendações tomam stop", a seção
**"Laboratório offline de redução de stops"** mostra status, quantidade de
hipóteses, contexto, stops evitados, wins que seriam removidos, operações
preservadas, resultado da validação, motivo da rejeição e o selo
**🔒 holdout ainda fechado**.

Sem padrão: *"Nenhuma hipótese elegível por enquanto. Os dados continuam sendo
coletados."*

Avisos fixos: *"Validação apoiada não significa aprovação."*, *"O teste final
ainda não foi aberto."*, *"Nenhuma alteração foi aplicada à estratégia."*
Sem botão; `frontend/dist` intacto.

## Limitações

1. **Validação apoiada não é aprovação** — o teste final continua selado.
2. O contrafactual assume que remover o contexto **não muda o resto do mercado**.
3. Bloquear um contexto adverso **também remove os wins** daquele contexto — o
   número aparece em toda hipótese.
4. **Correlação não é causalidade**: o contexto pode apenas rotular o regime.
5. SHADOW é o caminho do **setup**, não o fill real.
6. Nenhuma hipótese é executável, promovível ou elegível a Shadow nesta fase.

## Invariantes

- Champion LIVE idêntico; holdout final **SELADO**.
- Zero escrita no banco, zero rede, SDK, exchange ou executor.
- Nenhuma tabela, coluna, migration, env, flag ou endpoint novo.
- P05.1R, P05.1T e P05.2A intactos.
