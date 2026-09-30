# Fechamento A / H — baseline `03862806`

Pacote dirigido de `docs/REVISAO_03862806_A_H.md`: os dois achados eram VÍNCULOS
entre componentes, não motores isolados. C (causalidade temporal na admissão),
B/D/E/G, fórmulas, champion, calibração, limites e defaults não foram tocados.

- HEAD conferido antes de editar: `03862806`.
- Commits deste pacote: `3d806b3b` (A) e o commit H desta entrega.
- Arquivos: `backend/services/execution_reconciliation_service.py`,
  `backend/services/preselection_experiment_service.py`,
  `backend/services/strategy_evidence_service.py`,
  `backend/tests/pg_integration_p03_conflict.py`,
  `backend/tests/pg_integration_r11_r12_pipeline.py`,
  `backend/tests/test_p03_execution_reconciliation.py`,
  `backend/tests/test_lote_g_preselection_experiment.py`,
  `backend/tests/test_r10a_offline_replay.py`, este documento e
  `docs/HARDENING_LOG.md`.

## A — contradição ENTRE incidentes do mesmo dispatch

### Causa

O detector criado no pacote anterior protege duas observações DENTRO do mesmo
incidente. `_dispatch_outcome` continuava agregando incidente por incidente: dois
produtores oficiais do MESMO `client_order_id` — `FINAL_FILL_QTY_UNKNOWN`
(montado por `assemble_entry_incident`) e `ENTRY_SUBMISSION_UNKNOWN` (criado por
`recover_entry_intents`) — podiam gravar provas OPOSTAS, uma `POSITIVE` e outra
`TERMINAL_ZERO`, sem marcador de conflito em nenhum dos dois. O positivo vencia e
a intenção era liquidada como confirmada.

### Contrato

Coletor ÚNICO por dispatch (`collect_dispatch_proofs`), varrendo TODOS os
incidentes daquele `client_order_id` exato (símbolo/exchange conferidos; `-mfb`
NÃO é unida por prefixo — ordem filha é outro ID e pode ser legítima).

Precedência do desfecho, nesta ordem:

1. marcador de conflito já persistido → `CONFLICT`;
2. prova positiva **e** zero terminal do MESMO dispatch → `CONFLICT`;
3. só positiva → `POSITIVE`;
4. só zero terminal → `TERMINAL_ZERO`;
5. nada → `UNKNOWN`.

Limite inferior > 0 (ou posição `PROTECTED`) é evidência POSITIVA incompatível
com zero terminal. Limite inferior zero/ausente, posição FLAT e ausência de SL
NUNCA demonstram zero terminal, e `executedQty=0` nunca é fabricado.

### Caller real

`_settle_intent_from_proof` trata conflito ANTES de qualquer retorno por
incidente aberto/estado inconclusivo, ANTES de consultar o vínculo RealTrade e
ANTES de `mark_confirmed`/`mark_terminal`: RealTrade não desempata contradição, e
o bloqueio vale com vínculo perfeito e com posição FLAT no momento.

Persistência pelo fluxo que já existe: payload determinístico
(`entry_proof_conflict` com dispatch, fontes `{incident_key, kind}` ordenadas e
cópias das duas provas), portador = incidente OFICIAL `ENTRY_SUBMISSION_UNKNOWN`
daquela identidade por `record_incident` e sua chave estável (registro existente
é reaproveitado; reabertura só pelo caminho oficial). Latch local antes da
persistência e a transação `persist_incident_with_p03_pause` já existente
(incidente visível e pausa P03 no MESMO commit). Depois, claim válido e
`_halt_on_proof_conflict`/`_fenced` para `MANUAL_REQUIRED` com
`resolved_at=None`. Nenhum kind, tabela ou reconciliador novo.

Falha de claim (lease de outro processo) ou de persistência mantém a liquidação
BLOQUEADA e o latch armado — o ciclo seguinte conclui o bloqueio. Lease nunca é
roubado e não há update sem fencing.

`_reconcile_one` para no caminho manual logo após claim/leitura (antes de
resolver, limpar ou mutar) e também depois de gravar prova nova;
`recover_entry_intents` conta o conflito e NÃO cai no caminho genérico de retry.
Nada de criar/cancelar ordem, apagar SL ou reenviar entrada.

### RED → GREEN (PG, fluxos persistidos)

RED na baseline `03862806` (serviço revertido só para a medição, harness novo):

```
✓ dois_kinds_oficiais_para_o_mesmo_dispatch
✓ provas_opostas_em_incidentes_distintos
AssertionError: conflito_cruzado_persistido: None
```

Os dois produtores oficiais gravam provas opostas e nenhum conflito é
persistido — a liquidação segue.

GREEN com o código corrigido: `P03_CONFLICT_PG_OK: 46 verificações`, incluindo
`conflito_cruzado_persistido`, `portador_do_conflito_e_o_incidente_oficial`,
`payload_do_conflito_cita_as_duas_fontes`, `portador_nao_resolvido_e_manual`,
`incidente_irmao_tambem_para_no_manual`, `cruzado_nao_confirma_a_intencao`,
`cruzado_mantem_slot_e_reserva`, `cruzado_mantem_pausa`,
`ordem_inversa_tambem_conflita`, `restart_preserva_conflito_cruzado`,
`repeticao_nao_cria_incidente_infinito`, `claim_alheio_nao_libera_liquidacao`,
`ciclo_seguinte_conclui_o_bloqueio`, `falha_de_persistencia_nao_libera` e
`zero_mutacao_na_exchange`. Primária zero + filha `-mfb` positiva continua
reconciliando (IDs distintos).

Unitário de permutação (`AgregacaoPorDispatch`, 7 testes) cobre todas as ordens
de chegada e a exclusão de outro dispatch/símbolo, sem substituir a integração.

## H — um contrato só, do runner ao catálogo

### Causa

Dois sintomas do mesmo vínculo incompleto:

1. `candidate_config` só participava do guard de TIPO. O MESMO estudo de replay
   era `STUDY_VERIFIED` com `SCORE_MIN=71`, `73` ou `74`, e o manifesto
   `ReplayConfig` verdadeiro era recusado na criação pela regra legada de um
   knob — que é do POST_SELECTION.
2. A reavaliação não comparava o contrato recuperado com o contrato ORIGINAL
   congelado na criação. Integridade interna (hash refeito sobre o próprio
   corpo) prova ausência de corrupção, não vínculo: outro contrato BEM FORMADO,
   com candidata/custos diferentes e TODOS os hashes recalculados, passava.

### Contrato

Envelope FECHADO, construído e conferido por um único par de funções no serviço
do tipo (`build_preselection_envelope` / `validate_preselection_envelope`):

```
{experiment_type, experiment_type_version, replay_config, contract_hash}
```

`replay_config` é o manifesto COMPLETO da candidata efetivamente executada pelo
replay. Recusa `SCORE_MIN` e knobs-placeholder, campo extra, campo ausente,
versão/tipo errados e contrato ausente, sem preencher default nenhum.

Manifestos são validados RECONSTRUINDO o motor: `validate_replay_manifest`
reinjeta os campos de entrada em `ReplayConfig` e exige `manifest()` idêntico;
`validate_costs_manifest` faz o mesmo com `CostConfig`, separando entradas
(`fee_bps_per_side`, `slippage_bps_per_side`, `funding_bps_per_bar`) dos
derivados (`complete`, `funding_model`, `config_hash`). As validações numéricas
do próprio motor continuam valendo.

Hashes distintos e não confundidos: `manifest.config_hash` (motor),
`candidate_hash = canonical_hash(envelope)`,
`champion_hash = canonical_hash(manifesto baseline)` e `contract_hash`
(função canônica sobre o contrato inteiro). Como `contract_hash` viaja DENTRO do
envelope, o `build_experiment_key` que já existia passa a distinguir mudanças
só de custos ou só de escopo. Chave e comportamento dos tipos legados intactos.

### Caller real

- `create_preselection_experiment` usa o validador DO TIPO (não
  `validate_candidate_config`), trata `champion` como o MANIFESTO BASELINE
  executado pelo replay (`discover_champion_config()` não serve), confere
  identidades ANTES de `evaluate_preselection_candidate` e congela uma cópia
  COMPLETA e independente (`deepcopy`) do contrato validado em
  `offline_metrics.study.contract`.
- `_upsert_experiment` reaproveita lock e caminho existentes; para
  PRE_SELECTION, reencontrar registro (inclusive em DRAFT, cujo
  `offline_metrics` seria reescrito) com contrato congelado divergente falha
  explicitamente com `PRESELECTION_FROZEN_CONTRACT_MISMATCH`. Idempotência é
  repetir o MESMO contrato.
- `verify_study_identity` preserva a integridade interna e passa a conferir:
  schema/versão do envelope, manifestos reais, igualdade canônica do
  `replay_config` aceito com `contract.candidate_config`, hash recalculado igual
  ao declarado no envelope e — quando informado — igual ao contrato ORIGINAL
  congelado, além de população, tipo, versões, escopo, custos, bundle, dataset e
  corte.
- `verify_study_against_frozen` é o vínculo: exige contrato congelado que fecha
  consigo mesmo e compara campo a campo contra o recuperado ANTES da conferência
  interna, devolvendo `diverged`.
- `evaluate_preselection_shadow` lê envelope, hashes, referência, dataset/corte e
  o contrato congelado; sem congelamento verificável bloqueia com
  `PRESELECTION_FROZEN_CONTRACT_MISSING` (sem backfill de identidade); compara o
  estudo recuperado contra o original antes de avaliar evidência; no segundo
  lock reconfere identidade e contrato além de status/chave.
- `start_preselection_shadow` recusa vínculo ausente/inválido inclusive no
  retorno idempotente de quem já está em SHADOW, preservando a exclusividade
  oficial.
- PRE_SELECTION continua despachado antes de qualquer loader POST_SELECTION;
  divergência não chama avaliador nem gate e não consulta outcomes. Ler o JSON do
  estudo persistido NÃO é nova avaliação econômica: a evidência continua sendo a
  produzida pelo estudo recuperado.

### RED → GREEN (PG, fluxos persistidos)

RED na baseline `03862806` (serviços H revertidos só para a medição; harness da
baseline com bloco de medição no fim, 93 verificações verdes antes do bloco):

```
RED1_KNOBS_ACEITOS_PELO_MESMO_ESTUDO: [71, 73, 74]
RED1_MANIFESTO_REAL_NA_CRIACAO: False INVALID_CANDIDATE_CONFIG
RED2_B_INTEGRO_EM_SI: True | B_PUBLICADO_NA_REFERENCIA_DE_A: True
RED2_CONTRATO_CONGELADO_NA_CRIACAO: 152079b97004a942 | CONTRATO_AVALIADO_AGORA: 8f990170af02b782 | DIFERENTES: True
RED2_REAVALIACAO_ACEITOU_OUTRO_CONTRATO: True OFFLINE_VALIDATED None
```

GREEN com o código corrigido: `R11_R12_PIPELINE_PG_OK: 138 verificações` (era
93). O caso positivo passou a usar os manifestos REAIS (fixture sintética
congelada → replay de carteira → walk-forward → gate → produtor persistido →
criação oficial → SHADOW → avaliação oficial → repetição/restart), sem o
placeholder `SCORE_MIN=73` e sem métricas fabricadas. Verificações novas:

- `envelope_fechado_leva_o_manifesto_executado`,
  `knob_placeholder_nao_vale_por_candidata`,
  `envelope_recusado_{manifesto_incompleto, versao_invalida, hash_interno_errado,
  campo_extra, sem_contrato}`, `champion_do_ambiente_nao_e_baseline_do_replay`;
- `mesmo_contrato_repetido_preserva_identidade`,
  `contrato_congelado_no_experimento`, `envelope_congelado_e_o_do_manifesto`;
- por variação ISOLADA de candidata, custos e baseline:
  `contrato_b_*_recalcula_todos_os_hashes`, `contrato_b_*_valido_isoladamente`,
  `contrato_b_*_publicado_na_referencia_de_a`,
  `reavaliacao_bloqueia_contrato_b_*` (com `diverged` nomeando o campo),
  `bloqueio_b_*_nao_avalia_evidencia` (sentinela do avaliador e do loader
  pós-seleção intocados), `bloqueio_b_*_preserva_status_config_e_congelado`,
  `contrato_b_*_tem_identidade_propria`,
  `custos_isolados_mudam_a_identidade_do_experimento`;
- `contrato_{universo,politica,escopo}_e_integro_em_si` +
  `divergencia_de_*_bloqueia_antes_do_avaliador`;
- `contrato_original_de_volta_avalia`,
  `congelado_divergente_bloqueia_a_idempotencia`;
- `shadow_recusa_vinculo_ausente`, `idempotencia_nao_dispensa_o_vinculo`,
  `legado_sem_contrato_congelado_bloqueia_com_motivo`,
  `bloqueio_nao_faz_backfill_de_identidade`;
- preservados: adulteração interna campo a campo,
  `regra_de_um_knob_preservada_no_legado`, `comparador_legado_preservado`,
  bloqueios cruzados de comparadores, `exclusividade_do_ciclo_preservada`,
  `transicao_para_elegivel_bloqueada`,
  `promocao_do_tipo_declarada_nao_implementada`.

Unitário novo (`EnvelopeFechadoDoTipo`, 8 testes) cobre envelope, manifestos sem
default silencioso, separação entrada/derivado dos custos e as três respostas do
vínculo (igual, divergente, sem congelamento).

## Comandos reproduzíveis

Cluster PostgreSQL 16 descartável, UTF-8, socket Unix, TCP/DNS bloqueados pelos
próprios harnesses. Nenhum `DATABASE_URL` de produção é usado.

```bash
PGBIN=/opt/homebrew/opt/postgresql@16/bin
SOCK=/tmp/cw-r11pipe-sock.$(python3 -c 'import secrets;print(secrets.token_hex(4))')
DATA=$(mktemp -d /tmp/cw-pgdata.XXXXXX); mkdir -p "$SOCK"
LC_ALL=C $PGBIN/initdb -D "$DATA" -U r11pipe --auth=trust \
  --encoding=UTF8 --lc-collate=C --lc-ctype=C
$PGBIN/pg_ctl -D "$DATA" -o "-k $SOCK -c listen_addresses=''" -w start
$PGBIN/createdb -h "$SOCK" -U r11pipe r11pipedb
cd backend && R11PIPE_TEST_SOCKET="$SOCK" PYTHONDONTWRITEBYTECODE=1 \
  .venv311/bin/python -B tests/pg_integration_r11_r12_pipeline.py
$PGBIN/pg_ctl -D "$DATA" -m immediate stop; rm -rf "$DATA" "$SOCK"
```

Os outros dois harnesses seguem o mesmo molde, trocando socket/usuário/banco:
`P03_CONFLICT_TEST_SOCKET=/tmp/cw-p03cfl-sock.*` com
`tests/pg_integration_p03_conflict.py` e
`R05_CLOCK_TEST_SOCKET=/tmp/cw-r05clk-sock.*` com
`tests/pg_integration_r05_clock.py`.

Suíte unitária:

```bash
cd backend && PYTHONDONTWRITEBYTECODE=1 \
  .venv311/bin/python -B -m unittest discover -s tests -t . -q
```

## Limitações declaradas

- A: bordas de exchange, persistência da intenção e lookup RealTrade são
  simulados; repositório de incidentes, transação incidente+pausa, claim/fencing
  e ciclo são reais em PostgreSQL. Não foi demonstrada liberação financeira em
  PG por este contraexemplo.
- H: mercado é fixture sintética congelada; replay de carteira, walk-forward,
  gate, contrato canônico, catálogo, locks e persistência são reais. O RED foi
  medido revertendo temporariamente os dois serviços H para `03862806` no
  checkout local; nada foi commitado nesse estado.
- Dois skips históricos em `tests/test_r05c_execution_accounting` por fixture
  auditada privada indisponível no repositório. Fixture não foi fabricada,
  nenhum teste foi apagado e nenhuma falha foi convertida em skip.
- Funding permanece limitado ao alcance declarado na entrega anterior; não é
  validação de contabilidade total.
- Fora deste pacote, sem alteração: C (SQL/locks da admissão), F
  (`BLOCKED_MISSING_DECISION`), adaptador LIVE, promoção pré-seleção e
  `ELIGIBLE` — que continuam fechados. Sem migração, tabela, coluna, ENV, flag,
  endpoint, scheduler, fila ou frontend novos; sem acesso a produção/exchange,
  segredo, Telegram, push ou deploy. Cluster descartável encerrado.
- Não se declara ausência de bugs: o que este documento afirma é o que os
  harnesses acima executaram.
