# R10B — Dataset local das vetadas R09 para o laboratório R10A

Modo `LOCAL_RESEARCH_ONLY`, `promotable=false`. Base do lote: `51c992c2`.
Serviço: `backend/services/research_dataset_service.py`.
CLI: `backend/scripts/research_dataset.py`.

> É uma **ponte de leitura**, não um backtest do executor LIVE e não uma
> avaliação já realizada. Exportar não aprova estratégia.

## Coorte e unidade

Fonte: `rejected_setup_observations` (R09), com `decision_observations` só para
conferir identidade. A unidade é **uma oportunidade vetada**, nunca a tentativa.

As trajetórias só existem nas vetadas: `DecisionObservation` e
`DecisionObservationAttempt` não guardam velas das oportunidades aceitas. Por
isso o primeiro dataset econômico é da coorte `R09_REJECTED_POST_SELECTION` —
não é o mercado, não são as recomendações aceitas e não é desempenho REAL.

Cada linha usa `decision_at`, `frozen_setup` e `frozen_config` **da mesma
linha**. `first_seen_at`, o setup da primeira tentativa, o snapshot mais
recente e o relógio do exportador nunca substituem a decisão da rejeição.

## Os dois comandos

```sh
# 1) valida a requisição — nenhum acesso a banco
backend/.venv311/bin/python -B backend/scripts/research_dataset.py \
    --request req.json --validate-only

# 2) exporta (leitura explícita); DATABASE_URL vem do ambiente, nunca de argumento
backend/.venv311/bin/python -B backend/scripts/research_dataset.py \
    --request req.json --read-db --out-dir /fora/do/repo/export-2026-01-01

# 3) análise: comando SEPARADO, o CLI R10A de sempre
backend/.venv311/bin/python -B backend/scripts/research_replay.py \
    /fora/do/repo/export-2026-01-01/dataset.json
```

A exportação **não** roda a comparação. O CLI não aceita DSN em argumento, não
imprime credenciais nem texto de exceção (só `INVALID_USAGE`,
`INVALID_REQUEST`, `SOURCE_UNAVAILABLE`, `SOURCE_CONTRACT_OR_OUTPUT_ERROR`,
`DATASET_LIMIT`), não lê `.env`, não chama `init_db`/`create_all` e recusa
destino existente ou dentro do repositório.

### Requisição (schema fechado, sem defaults)

```json
{"as_of_utc": "2026-01-01T00:00:00Z",
 "split": {"train_start_ms": ..., "validation_start_ms": ..., "holdout_start_ms": ..., "purge_bars": 1},
 "baseline_config": {"bar_ms": 300000, "entry_window_bars": 3, "pre_tp1_time_stop_bars": 12,
                     "max_holding_bars": 24, "tp1_fraction": 0.45, "be_lock_fraction": 0.2,
                     "trail_atr_multiple": 2.2, "trail_activation_atr": 0.5, "max_bars": 96},
 "candidate": {"candidate_id": "...", "registered_at_ms": ..., "kind": "MANAGEMENT_ONLY",
               "replay_config": {"... igual ao baseline, com no máximo UM parâmetro de gestão diferente"}},
 "costs": {"fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0, "funding_bps_per_bar": 1.0},
 "bootstrap": {"seed": 7, "samples": 200, "block_size": 2}}
```

Validadores do R10A; chave desconhecida, campo ausente, bool/NaN/string no
lugar de número e `as_of_utc` fora de UTC ou com precisão abaixo de
milissegundo são recusados. `STRUCTURAL_CONF_ONLY` **não** é suportado aqui —
o exportador não fabrica features. Controle A/A (config idêntica) é permitido.
Nada é escolhido pelo resultado: sem grid search e sem gerar candidato.

O registro precisa anteceder o treino, como no contrato R10A. **Um timestamp
informado pelo operador não prova pré-registro independente**; o manifesto diz
isso em `configs.registration_evidence`, e o hash prova identidade, não
ausência de tuning externo.

## Artefatos

`dataset.json` é um payload `compare` aceito sem alterações pelo `run_payload`.
`manifest.json` traz schema/versão, origem, cutoff, configurações e hashes,
critérios de seleção, contagens e exclusões, cobertura, limitações e os
fingerprints. Nenhum dataset de conta é versionado no repositório.

Escrita: staging fora do destino, `mkdir` atômico e o **manifesto por último**.
Falha no meio não deixa par com aparência de exportação concluída, e nada é
sobrescrito.

Determinismo: mesma origem + mesma requisição ⇒ mesmos bytes e hashes. O
`request_hash` não contém relógio, segredo ou resultado. `source_sha256` cobre
`version`, setup, config e velas admitidas: se a origem mudar, ele muda —
o mesmo cutoff **não** garante snapshot histórico idêntico.

## Seleção, cortes e holdout

1. Índice apenas com `(opportunity_key, decision_at)`, ordenado por
   `(decision_at, opportunity_key)`, `LIMIT` do R10A + 1. Excedeu → falha
   clara (`DATASET_LIMIT`), sem truncar e sem escolher os casos mais completos.
2. Janelas semiabertas: treino `[train_start, validation_start)`, validação
   `[validation_start, holdout_start)`, cutoff `decision_at < min(holdout, as_of)`.
3. Purga conservadora do R10A com o **maior** horizonte entre baseline e
   candidato, aplicada antes de ler qualquer JSON (paridade com o comparador
   verificada em teste).
4. Detalhes e velas só dos ids admitidos, filtrados na própria consulta.
5. Velas projetadas **no PostgreSQL**: `timestamp ≥ primeira barra` e
   `timestamp + bar_ms ≤ min(primeira barra + horizonte, fronteira do split,
   as_of)`. Fim exatamente na fronteira entra; atravessando, não. Cauda além do
   horizonte nunca chega ao Python. Timestamp fora de forma não é convertido
   às cegas: a linha inteira vira `MALFORMED_CANDLE_JSON`.
6. Do holdout só sai **contagem**: nenhuma barra, setup, trace ou outcome entra
   em payload, log, hash, métrica ou validação. Nenhuma `Opportunity` fictícia
   é criada para contá-lo.

Como `dataset.json` só contém treino e validação admitidos, o contador
`holdout_sealed` do comparador é 0 por construção; o total real está em
`counts.holdout_sealed` do manifesto. O R10A não foi ampliado para mudar isso.

SQL: uma transação `REPEATABLE READ READ ONLY` configurada antes da primeira
leitura e verificada por `current_setting`; colunas explícitas, consultas em
lote (sem N+1), nenhum DML/DDL, nenhum lock de admissão/risco, nenhum serviço
que escreva ou toque exchange, e `rollback` sempre no fim. As colunas
`outcome` e `coverage` **não são lidas**: a seleção nunca depende de desfecho,
retorno, win/loss ou completude.

## Conversão, exclusões e cobertura

`timestamp` (R09) vira `timestamp_ms` (R10A) preservando os valores. Zero é
válido (volume 0); bool, string, NaN e infinito não são. Oportunidade válida
sem velas permanece no conjunto com lista vazia — o R10A a reporta como
`INSUFFICIENT_DATA`.

Exclusões contadas e nomeadas: `SOURCE_CONTRACT_MISMATCH`, `CONFIG_MISMATCH`
(a config congelada precisa coincidir com o baseline; nada é mesclado nem
sobrescrito por defaults/ENV atuais), `OPPORTUNITY_ROW_MISSING`,
`IDENTITY_MISMATCH`, `INVALID_SETUP`, `TEMPORAL_INCONSISTENCY`,
`MALFORMED_CANDLE_JSON`, `INVALID_CANDLE_DATA`.

Cobertura relatada: com/sem velas, janelas completas, incompletas, truncadas
pelo cutoff e não contíguas. Rejeição terminal no R09 para de acumular velas,
então `RESOLVED` lá **não** garante horizonte para um candidato diferente — as
lacunas continuam explícitas nos resultados do R10A, com denominadores.

Custos são **cenários declarados**, não custos observados da conta: ausência
continua `None` e sem retorno líquido comparável (`costs.net_r_comparable`).
A matemática é a do R10A; não há outro motor de métricas.

Amostra vazia ou inutilizável gera manifesto com `state` em `EMPTY`,
`UNUSABLE_ALL_PURGED` ou `UNUSABLE_ALL_EXCLUDED` e motivo — nunca "estratégia
aprovada" ou "dataset economicamente suficiente".

## Validação

`backend/tests/test_r10b_research_dataset.py` (41 testes herméticos):
requisição e schema fechado, candidato de um parâmetro, janelas/empates/ordem,
paridade da purga com o comparador REAL, limites, conversão, exclusões,
violações de contrato, determinismo e sensibilidade dos fingerprints, estados
vazios, loader (sequência somente leitura, rollback, holdout só contado,
limite +1), escrita sem sobrescrita nem par parcial e CLI (uso, erros antes do
banco, indisponibilidade genérica, sem argumento de DSN).

`backend/tests/pg_integration_r10b.py` (PostgreSQL 16 descartável, só socket
Unix, TCP/DNS bloqueados, schema criado pelo harness):

```sh
R10B_TEST_SOCKET=/tmp/cw-r10b-sock.XXXX \
  backend/.venv311/bin/python -B backend/tests/pg_integration_r10b.py
```

Prova: transação sem XID atribuído; escrita injetada recusada
(`read-only transaction`); projeção JSONB no servidor com a cauda e as velas
do holdout nunca chegando ao Python (controle lê o JSONB bruto e vê a cauda);
purga e empates; holdout apenas contado; alterar `outcome`/`coverage` e os
detalhes selados não muda dataset nem fingerprints, enquanto `version`/velas
mudam; borda do cutoff; limite; exportação pelo CLI idêntica à leitura direta,
recusa de sobrescrita e o CLI R10A rodando sobre o arquivo; as três tabelas
permanecem idênticas.

## Limitações

Coorte enviesada (só vetadas, e só enquanto havia snapshot aberto do símbolo);
fonte de preço da janela do resolver sem rótulo por vela; horizonte curto de
pesquisa, não o time-stop LIVE; custos são cenários; `decision_ts_ms` é
truncado ao milissegundo; hashes provam identidade, não independência. Nenhum
dado real foi exportado durante a implementação.
