# P05.2A — diagnóstico longitudinal dos stop-losses (ANALYTICS ONLY)

## Objetivo

Descobrir, com evidência longitudinal, **onde** e **em quais contextos** as
recomendações acumulam stop — separando **volume** de **taxa** e verificando se o
efeito **persiste no tempo**.

> Esta fase **não otimiza parâmetro**. Não altera estratégia, score, tier, gate,
> stop, TP, sizing ou executor; não cria candidato executável, `StrategyExperiment`
> nem Shadow; não abre o holdout final.

## Antes

Havia muitos stops observados, mas sem separação robusta entre **volume**
(quantos stops um contexto produz), **taxa** (stops por exposição) e
**persistência temporal** (o mesmo contexto continua ruim depois?). Um segmento de
alto volume parecia "o pior" mesmo com taxa abaixo da média.

## Depois

Diagnóstico longitudinal · taxa por exposição com Wilson 95% e lift versus
baseline · confirmação treino → validação · **teste final selado** · REAL e SHADOW
separados · trajetória prospectiva quando disponível · hipóteses **somente
analíticas**.

## Definição de stop

### SHADOW (`RecommendationSnapshot`) — fonte principal

Janela por `outcome_at`, reutilizando `_calib._not_fast_void()`.

| Status | Classe | É stop? |
|---|---|---|
| `lost` (com `R < 0`) | `STOP` | **sim** — stop original antes do TP1 |
| `won_tp1_be` (com `R > 0`) | `PROTECTED_EXIT` | **não** — saída protetiva pós-TP1 |
| `won_tp1` / `won_tp2` (com `R > 0`) | `WIN` | não |
| `expired` (com `R = 0`) | `EXPIRED` | não |
| status × sinal de R divergente | `INCONSISTENT` | excluído e contabilizado |

`realized_r = None` **nunca** vira zero — é exclusão contabilizada.

### REAL (`RealTrade`) — confirmação secundária

Só `source="auto"`, janela por **`closed_at`** (nunca `opened_at`).
`status == "closed_stop"` é o stop real confirmado. `closed_manual` com R negativo
fica **separado** como `NEGATIVE_MANUAL_EXIT` — nunca se classifica todo
`realized_r < 0` como stop. Reporta cobertura do vínculo com snapshot.
Retry/import duplicado é removido pela mesma identidade econômica do loader REAL
do P05: ordem da exchange, `client_order_id`, `recommendation_id` e, apenas como
último fallback, id interno.

> **REAL e SHADOW nunca são somados.**

### P05 PATH — telemetria prospectiva

Lê **somente** `features["p05_path"]` já persistido. Sem backfill, sem recálculo
de LONG/SHORT. Ausência nunca vira zero; `FINAL_OBSERVED`, `FINAL_PARTIAL` e
`UNAVAILABLE` são preservados e a cobertura é reportada com os motivos de ausência.

## Qualidade de dado

Excluído **e contabilizado por motivo**: aberto · sem timestamp de resolução ·
`realized_r=None` · NaN/inf · duplicata · status/R incoerente · contexto ausente ·
`p05_path` ausente ou inválido · RealTrade sem vínculo quando a métrica exige
contexto. Nenhuma ausência é convertida em zero.

## Holdout selado

`load_stop_shadow_split` é o loader específico e somente-leitura desta fase:

1. conta as linhas elegíveis **sem selecionar outcome**;
2. ordena deterministicamente por **`(outcome_at, id)`**;
3. divide **50/25/25** cronologicamente;
4. materializa `realized_r`, `status` e `features` **apenas** para treino (50%) e
   validação (25%) — a segunda query é limitada pela borda temporal da validação
   e depois **aparada pelo conjunto exato de ids**, então empates de `outcome_at`
   não deixam linha de teste vazar;
5. do teste, devolve **somente** contagem e limites temporais.

A resposta declara `train_count`, `validation_count`, `test_count`,
`holdout_status="SEALED"`, `holdout_outcomes_read=false` e
`holdout_metrics_computed=false`. Nenhuma função de avaliação, gate ou candidato é
chamada sobre o teste. Testes com **sentinela** falham se `realized_r`/`status` do
holdout forem acessados.

## Janela

`P052A_WINDOW_DAYS = 120`, fixa e independente do seletor do painel. A resposta
informa janela solicitada, período realmente observado, timestamp mais antigo e
mais recente, e marca `young_history` quando o histórico não alcança 120 dias —
sem inventar história ausente.

## Métricas gerais

**SHADOW**, calculado **separadamente** em treino e validação: total resolvido ·
stops · stop rate · **Wilson 95%** · wins · saídas protetivas pós-TP1 · expired ·
expectancy R · soma R · mediana R · profit factor · pior sequência de stops ·
duração mediana até o stop · p75/p90 do tempo até o stop · cobertura de contexto ·
confiabilidade.

A cobertura geral de contexto declara explicitamente `regime` como sua base; a
cobertura de cada um dos 14 eixos permanece no respectivo bloco segmentado.

**REAL**: total fechado (`source=auto`) · `closed_stop` · stop rate · Wilson 95% ·
saídas manuais negativas · TP1/TP2/BE · expectancy e soma R quando calculáveis ·
cobertura de vínculo com snapshot · confiabilidade · limitações de amostra pequena.

> Contagem absoluta de stops **nunca** é chamada de "pior" sem dividir pela
> exposição.

## Segmentos

Mesmos eixos do `segment_rows`: tier · timeframe · tier×timeframe · direção ·
base/símbolo · patterns · sessão UTC · dia da semana · regime · funding sentiment ·
score bin · ATR band · MTF aligned · entry zone type.

Cada segmento traz exposição · stop count · **stop rate** · Wilson 95% · stop rate
global do estágio · **lift em pontos percentuais** · participação em todos os
stops · **wins que seriam removidos se o segmento fosse bloqueado** · expectancy R ·
soma R · reliability · missing/coverage do eixo.

> **Padrões sobrepostos são atribuição, não causalidade.** Em `patterns` o mesmo
> trade aparece em mais de um segmento — os segmentos **não somam** ao total.

## Confirmação temporal

Treino **descobre**; validação apenas **verifica se persiste**.

| Classificação | Significado |
|---|---|
| `PERSISTENT_ADVERSE` | ruim nos dois estágios, com amostra e cobertura suficientes |
| `MIXED` | adverso em um estágio e não no outro |
| `SAMPLE_LIMITED` | amostra insuficiente em algum estágio |
| `LOW_COVERAGE` | cobertura do eixo < 80% em algum estágio |
| `NOT_ADVERSE` | taxa não supera o baseline |

`PERSISTENT_ADVERSE` exige **todos**: cobertura do eixo ≥80% em treino **e**
validação · ≥30 observações no treino · ≥20 na validação · ≥10 stops no treino ·
≥5 stops na validação · stop rate acima do baseline **nos dois** · expectancy
negativa **nos dois** · direção do efeito consistente.

Segmento de **alto volume** com muitos stops absolutos mas **taxa inferior ao
baseline** **não** pode ser marcado adverso (coberto por teste). Máximo de **8**
padrões persistentes, em ordem determinística. Nenhum achado é forçado.

## Trajetória MAE/MFE

Só onde há telemetria válida:

- **Tempo até o stop**: `<30m` · `30m–2h` · `2h–6h` · `>6h`
- **MFE antes do stop**: `<0.25R` · `0.25–0.50R` · `0.50–1.00R` · `>=1.00R`
- **Stop distance**: faixas descritivas de `stop_distance_pct`

Cada bloco reporta cobertura, quantidade, percentual, mediana, p75/p90 e missing
por motivo.

> **As faixas são DESCRITIVAS.** É proibido concluir "o stop deve ser ampliado",
> "o stop ideal seria X" ou "essa perda teria sido evitada". MAE/MFE de uma
> operação perdida **não autoriza** otimizar o stop.

## Hipóteses para o P05.2B

Máximo 8, cada uma com: eixo · valor/contexto · evidência no treino · confirmação
ou refutação na validação · cobertura · tamanho da amostra · stop-rate lift ·
expectancy · **wins existentes no mesmo contexto** · risco de remover operações
boas · limitações · status temporal.

**Não** contêm knob, config, threshold novo, decisão `BLOCK`, `SCORE_DELTA`,
alteração de stop, ação executável ou `promotion_plan` (verificado por teste).
Sem padrão persistente, o veredito é **`NO_PERSISTENT_STOP_PATTERN`**.

## Integração e cache

`stop_diagnosis` foi acrescentado ao retorno **existente** de
`/api/strategy/p05/status`, preservando todos os campos atuais. Usa cache
single-flight com o **mesmo TTL** do P05 (erro não envenena o cache) e é
**fail-soft**: falha do diagnóstico não derruba o restante do status.
O diagnóstico de stop reutiliza também o **mesmo store e lock** do cache P05,
com namespace próprio de chave; não existe cache paralelo. Falha total ou
parcial não entra no cache. Falha ao construir hipóteses devolve
`patterns_verdict="UNAVAILABLE"` e nunca é mascarada como ausência comprovada de
padrão.

**Nenhum endpoint novo, nenhum POST, `main.py` não foi modificado.**

## Frontend

Seção **"Por que as recomendações tomam stop"** no `AssertivenessPanel` existente:
janela de 120 dias · SHADOW e REAL separados · taxa de stop e amostra · treino
versus validação · holdout com selo **🔒 protegido** · padrões persistentes ·
padrões mistos/insuficientes · trajetória quando disponível · aviso de que são
**padrões observados, não causas** · aviso de que **nenhuma alteração foi aplicada
à estratégia**.

Sem botão, sem promoção, sem ativação, sem configuração técnica, sem
`[object Object]`, responsivo. `frontend/dist` intacto.

## Limitações

1. **Correlação ≠ causalidade.** Um contexto aparecer como adverso não prova que
   ele causa o prejuízo — pode estar apenas rotulando o regime de mercado.
2. **SHADOW é o caminho do setup, não o fill real.**
3. **REAL pode ter amostra pequena** — o intervalo de Wilson reflete isso e a
   confiabilidade é reportada.
4. **`p05_path` só existe após o P05.1T** (sem backfill): a cobertura de MFE antes
   do stop começa parcial e sobe com o tempo.
5. **Candle de 5m não revela a ordem intravela** — não se sabe o que veio primeiro
   dentro da vela.
6. **Bloquear um contexto adverso também removeria os wins daquele contexto** — o
   número de wins removidos aparece em toda hipótese, exatamente por isso.
7. **O diagnóstico não recomenda ampliar, encurtar ou mover o stop.**

## Invariantes

- Holdout final **SELADO**; nenhuma métrica do teste é computada.
- Zero escrita no banco; nenhum `StrategyExperiment` criado ou alterado.
- Zero rede, SDK, exchange, executor ou resolver de snapshot.
- Nenhuma tabela, coluna, migration, env, flag ou endpoint novo.
- P05.1R e P05.1T intactos; champion, score, tier, gates, stop, TP e sizing
  inalterados.

## Fechamento da auditoria

- Divergência status/R também é rejeitada para `won_tp1_be` não positivo e
  `expired` diferente de zero.
- O resumo REAL deduplica por identidade econômica, em paridade com `_load_real`.
- Cache P05.2A reutiliza `_DIAG_CACHE` e `_DIAG_CACHE_LOCK`; erro total ou
  parcial não envenena.
- Erro interno de hipótese fica `UNAVAILABLE`, tanto no contrato quanto na tela.
- A tela nomeia corretamente treino (50%) e validação (25%) e não chama amostra
  insuficiente de hipótese refutada.
