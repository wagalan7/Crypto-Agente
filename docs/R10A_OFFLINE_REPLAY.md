# R10A — Replay OHLCV offline e comparador pré-registrado

Modo `LOCAL_RESEARCH_ONLY`, `promotable=false`, `live_equivalent=false`.
Módulo: `backend/services/offline_replay_service.py`. CLI:
`backend/scripts/research_replay.py`. Base do lote: `b0d52a80`.

> **Não é replay do executor LIVE.** É um adaptador independente de barras
> OHLCV. Não reproduz scanner, seleção, gates, portfólio, latência, fila,
> liquidez, fills parciais nem o gerenciador de posição real. Um resultado
> aqui não prova, por si, redução de stops nem aumento de lucro.

## O que ele faz

- `replay_opportunity(opportunity, bars, config, costs)` — uma oportunidade
  ponto-no-tempo contra velas fechadas e contíguas a partir de
  `ceil(decision_ts_ms / bar_ms)`. Sem ENV, relógio, IO, banco ou provider.
- `compare_registered_candidate(...)` — **um** candidato pré-registrado,
  nas **mesmas** oportunidades e com os **mesmos** custos do baseline, em
  treino e validação cronológicos purgados. Holdout nunca lido.
- `replay_manifest()` — contrato, vocabulário de status e limitações.
- `run_payload(payload)` — adaptador JSON puro usado pelo CLI.

## Regras do replay (todas cobertas por teste numérico)

| Tema | Regra |
| --- | --- |
| Entrada | Toque-mercado hipotético no preço planejado, dentro de `entry_window_bars`. Sem toque → `NOT_FILLED`, sem R. |
| Vela de entrada | Abertura ≠ entrada e a mesma vela alcança stop/TP1 → `AMBIGUOUS_ENTRY_BAR`, sem R. Entrada intrabar sem saída é marcada `ENTRY_INTRABAR_TIME_UNKNOWN`. |
| Stop | Baseado em pavio. Stop e alvo na mesma vela → stop primeiro + `INTRABAR_STOP_TARGET_AMBIGUITY_STOP_FIRST`. Gap adverso → saída na abertura + `ADVERSE_GAP_STOP_AT_OPEN`. |
| TP1/runner | Fração `tp1_fraction` no TP1; resto no TP2 ou no stop do runner. Gap favorável é preenchido no preço do alvo (conservador). |
| BE/trail | Calculados no fechamento, valem só a partir da **próxima** vela. BE = entrada + `be_lock_fraction`·(TP1−entrada); trail ATR após `trail_activation_atr`. |
| Tempo | Time-stop antes do TP1 e horizonte máximo fecham no `close`. |
| Dados | Lacuna, duplicata, desordem ou vela anterior ao início → `MISSING_OR_UNORDERED_BARS`. Horizonte incompleto → `INSUFFICIENT_DATA`. Nenhum dos dois produz R (nem parcial do TP1). |
| Custos | Bps por perna: taxa sobre o notional preenchido; slippage adverso em entrada e em cada saída; funding como **cenário** assinado por abertura após a entrada (positivo debita long e credita short). |
| Custos ausentes | Qualquer componente `None` → `cost_status=UNKNOWN`, `net_r=None`. Zero só vale quando declarado (`KNOWN_SCENARIO`). |
| Validação | Tipos estritos (bool/string/NaN/inf recusados), geometria stop<entrada<TP1<TP2 (invertida no short), `features_asof_ms ≤ decisão`, limites de barras/oportunidades/bootstrap, overflow → erro, nunca R infinito. |

R é medido pela distância **planejada** entrada−stop. Vocabulário fechado:
`CLOSED_STOP`, `CLOSED_RUNNER_STOP`, `CLOSED_TP2`, `CLOSED_TIME_STOP`,
`CLOSED_MAX_HOLD`, `NOT_FILLED`, `AMBIGUOUS_ENTRY_BAR`,
`MISSING_OR_UNORDERED_BARS`, `INSUFFICIENT_DATA` (`REPLAY_STATUSES`).

## Comparador

- **Registro:** `CandidateRegistration` com hash; precisa ser anterior ao
  início do treino. O hash prova identidade da configuração, **não** prova
  registro independente nem ausência de tuning externo.
- **`MANAGEMENT_ONLY`:** difere do baseline em **no máximo um** parâmetro
  comportamental (`pre_tp1_time_stop_bars`, `max_holding_bars`,
  `tp1_fraction`, `be_lock_fraction`, `trail_atr_multiple`,
  `trail_activation_atr`). Timeframe, janela de entrada, `max_bars` e schema
  ficam idênticos. Config idêntica é aceita como controle A/A (delta 0).
  Guarda: `management_diff`.
- **`STRUCTURAL_CONF_ONLY`:** só compara **scores** (V2 bruta sob pesos
  explícitos × ablação conf-only do R08A). Sem regra econômica, sem retorno,
  tier ou P(TP1) inventados; `economic_comparison_status=UNAVAILABLE_STRUCTURAL_CANDIDATE`.
- **Split:** `ChronologicalSplit` treino < validação < holdout. Purga pelo
  horizonte **máximo** das duas configs + `purge_bars`. Oportunidades do
  holdout são só contadas (`holdout_sealed`); suas barras não são lidas.
  Só as barras do horizonte são materializadas (`islice`); barra dentro do
  horizonte que cruze a fronteira do split é erro.
- **Métricas:** apenas pares com `net_r` conhecido nos dois lados; relata
  amostra, cobertura (`excluded_or_unpaired_n`), expectativa, profit factor,
  drawdown sequencial e IC 95% por bootstrap circular em blocos (semente
  fixa). Poucos blocos → `INSUFFICIENT_BLOCKS`, sem IC.
- **Decisão:** sempre `NO_PROMOTION_RESEARCH_ONLY`, `winner=None`. Um delta
  positivo não é promovido nem sugerido.

## Fronteira com o R08A e com o processo live

`offline_replay_service` importa só a stdlib no topo. O laboratório R08A é
importado **tardiamente e apenas** dentro de `compare_registered_candidate`,
no ramo estrutural. O resolver R09 roda o replay de preços no processo live
sem carregar a ablação. O teste de isolamento do R08A foi ajustado somente para
essa fronteira: verifica por AST que toda menção em código é esse import tardio,
rejeita import no topo ou em outra função e confirma, em subprocesso, que
importar o replay e executá-lo não carrega o laboratório. Nenhum caminho live
chama `compare_registered_candidate` ou `run_payload`.

## Uso local

```sh
backend/.venv311/bin/python -B backend/scripts/research_replay.py --manifest
backend/.venv311/bin/python -B backend/scripts/research_replay.py backend/tests/fixtures/r10a_synthetic_replay.json
backend/.venv311/bin/python -B backend/scripts/research_replay.py backend/tests/fixtures/r10a_synthetic_compare.json
```

O CLI lê um JSON explícito (limite 16 MiB, constantes `NaN/Infinity`
recusadas), não carrega `.env`, não acessa rede nem banco. Entrada inválida →
código 2 e mensagem genérica, sem traceback. `compare` recusa chaves
desconhecidas, barras de ids não declarados, barras para oportunidades do
holdout e **qualquer** barra com `timestamp_ms ≥ holdout_start_ms`.
Também recusa barras cuja abertura anteceda o holdout, mas cujo fim
(`timestamp_ms + baseline_config.bar_ms`) ultrapasse essa fronteira, inclusive
fora do horizonte de replay. Fim exatamente na fronteira é permitido. Essa
checagem usa a configuração já validada; a construção de `Candle` é preguiçosa
e limitada pelo `islice` do comparador, após a purga.

### Fixtures sintéticas (dados inventados, não mercado)

- `r10a_synthetic_replay.json`: long com stop e custos 4/2/1 bps →
  `gross_r=-1`, `net_r=-1,02538008` (−1 − 0,0078 slippage − 0,01560008 taxa −
  0,00198 funding).
- `r10a_synthetic_compare.json`: `MANAGEMENT_ONLY` com `tp1_fraction`
  0,45→0,30; 6 treino, 4 validação, 1 purgada, 2 holdout seladas. Pares
  resolvidos: 5 (treino, IC disponível) e 3 (validação, `INSUFFICIENT_BLOCKS`).
  Não há vencedor: é demonstração de mecânica, não evidência.

## Validação

`backend/tests/test_r10a_offline_replay.py` (50 testes): contratos e tipos;
LONG/SHORT simétricos; stop, TP1+TP2, BE só na vela seguinte, trail ATR
causal nos dois lados; não-preenchimento; entrada intrabar tardia; ambiguidade
de entrada e de stop/alvo; gap adverso; time-stop e horizonte; lacuna,
duplicata, desordem e barra antecipada; horizonte incompleto sem R; custos por
perna e funding assinado; custo ausente ≠ zero; overflow; guarda de um
parâmetro e controle A/A; delta pareado calculado à mão; candidato melhor não
promovido; purga pelo maior horizonte; **sentinela do holdout** (Mapping que
falha se lido) com controle negativo provando que ela dispara; barras além do
horizonte não materializadas; barra que cruza fronteira; candidato estrutural
sem economia; custos compartilhados e hash; bootstrap determinístico; payload
e CLI (`--manifest`, fixtures, NaN, holdout); fronteira pelo fim da barra,
inclusive além do horizonte, igualdade permitida e sentinela de construção
preguiçosa no adaptador JSON; isolamento de imports/IO.

## Limitações

Adaptador ≠ executor; OHLCV não prova fila nem fill; stop por pavio difere do
classificador de snapshots (pré-TP1 por fechamento); funding é cenário
constante, não histórico; IC por blocos é diagnóstico e amostras pequenas ou
dependência longa exigem revisão; hash não prova ausência de tuning externo;
nenhum dado real foi carregado nesta etapa; holdout não acessado.
