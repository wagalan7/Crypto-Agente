# Lote 03 — checkpoint da integração governada

**Estado mais recente — 06/10/2026:** correção dos resíduos sobre `14764cea`
aplicada e verificada localmente. 222 testes direcionados 2×; suíte completa
2.867 executados, 2.865 aprovados e 2 skips históricos; PG16 59 verificações 2×.
Detalhes: `docs/LOTE03_CORRECAO_RESIDUOS_14764cea.md`. Infraestrutura INATIVA;
sem merge/push/deploy/aprovação real. Decisões/dados prospectivos e integração
do índice no checkout principal continuam pendentes. Seções abaixo registram
a cronologia anterior, não o estado atual de implementação.

Base: `581ad3d4fe843347992d245fb41983e9b2ce3a76`.
Ambiente: worktree `lote02-pesquisa`, já autorizado pelo usuário para os próximos lotes. Checkout de outro aplicativo e arquivos pessoais não entram nesta entrega.

## Em execução

- Autoridade operacional versionada, aprovação por propósito, revogação e rollback persistentes.
- Adaptador de seleção no scanner/executor oficiais, LEGACY por padrão.
- Shadow PRE_SELECTION prospectivo, separado do estudo offline.
- API administrativa específica, provas herméticas e PostgreSQL descartável.

## Fronteiras

O manifesto implementado no Lote 02 suporta `SELECTION_ONLY` com `SCORE_V3_MIN_SCORE`, preservando gestão e geometria. Isso não constitui prova de uma substituição do núcleo/playbooks ou da gestão. Escopos não implementados devem ser recusados, nunca aproximados.

Não há par real autorizado, calibração economicamente aceita ou amostra prospectiva suficiente. Nenhuma aprovação humana, flag ou canário real será criado durante a implementação. Dados sintéticos demonstram engenharia, não autorização operacional.

Publicação somente depois de revisão e provas locais. A autorização anterior para publicar após cada lote permanece válida; publicação inativa não é ativação de estratégia.

## Provas pendentes

Matriz integrada scanner → intenção → autorização → transporte falso; aprovação/revogação/rollback concorrentes; restart; regressões manual/P03/financeiro; suíte completa após a última edição.

## Checkpoint desta execução

Estado: **HANDOFF_TO_CLAUDE — parcial, NÃO publicável**.

O ambiente recusou a edição de `strategy_evidence_service.py` que conectaria start/evaluate/promote PRE_SELECTION à autoridade nova. A edição recusada não foi aplicada nem repetida. Foi solicitada confirmação humana explícita para implementar essa infraestrutura inativa, sem aprovação real, seletor CANDIDATE em produção ou mudança de flags.

Partes locais já escritas:

- Serviços novos de autoridade governada, adaptador de seleção e observação prospectiva (ainda em integração/revisão).
- Rotas administrativas específicas com credencial configurada obrigatória; não registraram qualquer aprovação real.
- Callback composicional pré-assinatura para a alavancagem do candidato, incluindo retry e propagação no fallback. LEGACY permanece sem esse callback.
- Hooks do resolver/admissão R09 existentes, inativos por padrão. Resultados prospectivos não são colocados na tabela de vetadas nem copiados do estudo offline.
- Captura ALLTF e payload da intenção preparados; ligação completa ao executor ainda pendente.

Provas executadas pelo root até este checkpoint:

- 32 testes Lote 03 verdes: 8 de transporte, 8 de autenticação/rotas ASGI isoladas e 16 de observação prospectiva.
- 20 regressões da captura oficial do Lote 02 verdes.
- Compilação dos oito arquivos Python de produção alterados/novos e `git diff --check` aprovados.
- Nenhum harness PostgreSQL do Lote 03 nem suíte completa foi executado. Estes resultados NÃO substituem a matriz integrada exigida pelo lote.

Na última execução focal do root, a suíte Lote 03 passou a **51 testes verdes** com os 19 testes adicionais de governança. `test_lote03_candidate_adapter.py` foi criado depois dessa execução e ainda precisa ser verificado. Testes anteriores com nomes de módulos inexistentes foram tentativas inválidas, não provas; as contagens acima vêm das execuções finais com módulos existentes.

O usuário preferiu transferir a implementação restante ao Claude para poupar uso neste chat. A retomada está em `docs/PROMPT_RETOMADA_LOTE03_CLAUDE.md`; ler junto com o prompt completo original. Nenhuma edição de implementação foi feita pelo root após essa mudança de direção: somente registro e prompt de passagem.

Pendências para retomar, sem repetir descoberta:

1. Confirmar autorização da infraestrutura e aplicar somente então o patch recusado do catálogo PRE_SELECTION.
2. Concluir o consumidor do executor oficial; guardar a identidade estável (`bundle_hash`, `approval_id`, geração), não usar o hash de uma leitura temporal renovada como identidade imutável do despacho.
3. Provar ordem única dos row locks singleton → experimento, sem adquirir P05 dentro da transação de risco `917283`.
4. Revalidar aprovação PROMOTION e CANARY separadamente; preservar a autoridade inicial ao retomar a intenção, com reconferência da validade atual.
5. Conectar medições de falha/fidelidade/proteção à evidência prospectiva. Enquanto indisponíveis, permanecem `None` e `NO_GO`; quantidade de dados não supre essa ausência.
6. Executar scanner/executor/transporte reais com bordas falsas, concorrência/restart PostgreSQL 16 Unix-only duas vezes, regressões e suíte final após a última edição.
7. Só depois revisar, integrar e publicar inativo conforme autorização anterior de publicação por lote.

Nenhum commit, merge, push, deploy, aprovação operacional, alteração de flag ou ordem real nesta execução. Main/produção permanecem no Lote 02 publicado (`581ad3d4`). Arquivos locais do Lote 03 devem ser preservados até a retomada, não publicados como lote concluído.

## Retomada pelo Claude — 05/10/2026 (parcial, limite de uso atingido)

Base confirmada: `581ad3d4`, branch `worktree-lote02-pesquisa`, alterações locais
preservadas (nenhum stash/reset/checkout). **Sem commit nesta execução**: a matriz
integrada do §5 não terminou, e código parcial não é Lote 03 concluído.

### Concluído e verde

**§2 — contratos fechados (RED medido antes: 19 de 21 falhavam).**
`tests/test_lote03_contract_closure.py` (21 testes) cobre as seis lacunas:
1. seletor com UMA interpretação — `live_candidate_adapter_service.selected_mode`
   passou a delegar para `operational_governance_service.selected_mode`; `OFF` é
   paridade legada documentada (`PARITY_MODES`), valor vazio lê como `LEGACY`
   (variável ausente não bloqueia o champion) e qualquer outro texto é `INVALID`
   e BLOQUEIA entradas novas. O scanner só consulta autoridade quando o modo não
   é de paridade, e usa `candidate_mode_active` — antes, `OFF` zerava as
   recomendações do champion (defeito corrigido);
2. `load_operational_view` não passa mais o ID textual da ENV: o
   identificador é lido e validado UMA vez em `governance._selected_id()`
   (antes, `"7" != 7` fazia CANDIDATE falhar sempre com
   `SELECTED_EXPERIMENT_MISMATCH`);
3. campo `operational_selection` declarado no modelo `Recommendation`, ausente
   na serialização legada (serializer já local, agora com teste);
4. identidade ESTÁVEL do despacho: `identity_of`/`identity_hash`/`same_identity`
   sobre experimento, bundle, aprovação, geração, modelo e decisão congelada —
   `observed_at_ms`, `local_fence` e `authority_hash` ficam fora;
5. retomada preserva a decisão original: `candidate_identity_matches` e
   `candidate_context_to_keep` em `entry_intent_service`, consumidos por
   `freeze_dispatch_proposal(stored_operational_selection=…)` e por
   `_bind_proposal_in_session` (perder ou acrescentar candidata na readmissão é
   recusa, não substituição);
6. validação FECHADA da seleção em `freeze_context`: lado, símbolo/quote,
   timeframe do catálogo oficial, preços finitos positivos, geometria coerente
   com o lado, score ≥ mínimo e probabilidade do evento EXATO do artefato —
   adulterar e re-selar o hash não passa.

**§3 — executor oficial ligado.** `shadow_trade_service`: contexto governado
entra na DECISÃO antes de qualquer mutação (`_reserve_entry_intent`, com
`context_for_rec` amarrando a geometria da recomendação e
`_candidate_transport_supported` exigindo Binance), viaja na proposta
(`_entry_proposal_fields`) e o `candidate_guard` (`_intent_candidate_guard`)
passa nos DOIS caminhos de POST (maker/fallback e MARKET direto), cobrindo
alavancagem e retry. Legado recebe `None` e segue idêntico.

**§4 — catálogo conectado (o patch antes recusado).** `strategy_evidence_service`:
`start_preselection_shadow(approval_id, expected_generation)` exige aprovação
SHADOW persistida/ativa/exata e congela a autoridade do start;
`evaluate_preselection_shadow` passou a consumir a coorte PROSPECTIVA
(`prospective_shadow_service.load_prospective_evidence`) em vez de reavaliar o
estudo offline, recusando `TEST_ONLY_NOT_PROSPECTIVE_AUTHORITY` e
`SYNTHETIC_SHADOW_NOT_PROSPECTIVE`; `promote_preselection` delega de verdade à
governança (aprovação PROMOTION + prova prospectiva + CAS), sem tocar ENV.
`summarize_prospective` deixou de mandar `None` fixo: `operational_measurements`
mede falhas de resolução, duplicatas deduplicadas e proteções sem economia
conhecível na própria coorte, e a fidelidade só existe com observação em runtime
candidato — caso contrário fica `None` com o bloqueio ESPECÍFICO
`FIDELITY_REQUIRES_CANDIDATE_RUNTIME_OBSERVATIONS`. Coorte vazia continua `None`
com `NO_PROSPECTIVE_OBSERVATIONS` (ausência não é zero).

**Defeitos reais encontrados pelo harness PG e corrigidos:** leitura de
`state.generation`/`exp.status` DEPOIS de `rollback()` (instância expirada →
`MissingGreenlet`) nos quatro ramos idempotentes da governança e nos dois novos
ramos do catálogo; as falhas de persistência agora declaram o tipo da exceção.

**Provas:** 86 testes Lote 03 verdes (21 fechamento + 14 adapter + 19 governança
+ 16 prospectivos + 8 API + 8 transporte); suíte completa **2.781 testes, OK, 2
skips R05C declarados**; `py_compile` dos arquivos tocados.
Caracterizações antigas adaptadas com a garantia preservada e o motivo escrito:
rota `promote` governada única (antes: nenhuma), `_r13_admin_principal` aceito
como porta administrativa (o teste agora confere o corpo do helper) e
`prospective_shadow_service` na lista de quem reusa o motor de replay.

### Pendente (não feito nesta execução)

1. **Matriz integrada §5 em PG**: `tests/pg_integration_lote03_governed.py`
   existe e passa **15 verificações** (persistência do experimento/estudo,
   bundle idempotente, start/aprovações, evaluate prospectivo, promoção
   bloqueada sem prova e governada com opt-in de engenharia, autoridade
   candidata lida do banco). Ele PARA na primeira verificação do executor:
   `open_shadow_for_recs` devolve 0 com a fixture atual (nenhum
   `_observe_decision`/`_record_skip` registrado), ou seja **fixture do harness
   incompleta**, não defeito conhecido de produção. Faltam, nesse arquivo:
   paridade OFF×LEGACY, caminho candidato com um POST falso, revogação durante a
   espera, duas conexões em aprovação×revogação e dois promotores, restart/filha
   `-mfb`, rollback com posição aberta e status read-only. Concorrência 2× e as
   regressões PG (P03/manual/financeiro/R09/R10B/R11-R12) **não** foram
   executadas.
2. Revisão final única pelos quatro callers (scanner, reserva/readmissão,
   transporte, catálogo/resolver) listando cada `await` de autoridade e onde se
   reconfere depois.
3. `docs/FECHAMENTO_FINAL_03_INTEGRACAO_GOVERNADA.md`, registro no
   HARDENING_LOG, índice operacional e commit — todos pendentes, por decisão de
   não publicar prova incompleta.

Nada foi ativado: `R13_OPERATIONAL_SELECTOR` permanece ausente (LEGACY),
`P05_CHALLENGER_SHADOW_ENABLED` e a coleta R09 seguem OFF no ambiente, nenhuma
aprovação real foi registrada, nenhuma ordem/mensagem/pausa foi tocada e não
houve merge/push/deploy.

## Fechamento dos quatro pontos — 06/10/2026

Estado: **IMPLEMENTADO / LOCAL_VERIFIED**, commit local de infraestrutura
INATIVA. Relatório: `docs/FECHAMENTO_FINAL_03_INTEGRACAO_GOVERNADA.md`.

As pendências 1–3 da retomada anterior estão fechadas:

- **A** — a rota existente passou a aceitar corpo governado FECHADO e o serviço
  despacha por TIPO antes do loader legado (POST_SELECTION e P05.1 intocados).
- **B** — decisão candidata OBSERVACIONAL (`CANDIDATE_SHADOW`) no ciclo oficial
  do scanner com aprovação SHADOW e seletor em LEGACY: quebra a circularidade
  (fidelidade sem promover), persiste escopo/decisão/identidade de regra na
  anotação e no hash, e mede fidelidade com hashes reconciliados — ausência fica
  `None` + lacuna + NO_GO, nunca 0%. Fronteira de propósito imposta: contexto
  SHADOW não autoriza reserva, alavancagem ou POST.
- **C** — fonte de proteção **SHADOW_SIMULATED** versionada no motor oficial
  (instrumentação aditiva; resultados econômicos inalterados), trilhas de
  resolução/economia/duplicata/proteção contadas SEPARADAMENTE (sem `elif`),
  zero só com observação aplicável e cobertura auditável, e
  `BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED` mantendo NO_GO até decisão humana.
- **D** — matriz integrada no harness governado: **53 verificações, exit 0, 2×**
  após a última edição (PG16 descartável, socket Unix, TCP zero, DNS contado).

Provas: suíte backend completa **2.846 testes, OK, 2 skips históricos R05C**;
24 harnesses PG verdes (lista no relatório); `py_compile` e verificação de
espaços em branco do diff aprovados.

Falhas PRÉ-EXISTENTES corrigidas com prova de anterioridade (ownership com prova
fresca posterior aos harnesses P03/R05; payload v2 posterior ao harness R09;
`contract_hash_of` exigindo o corpo inteiro). Correção de escopo própria: a
coorte prospectiva passou a ser exigida só em contratos `SELECTION_ONLY` — o
challenger de GESTÃO voltou a ter o ciclo dele, com o escopo DECLARADO.

Nada ativado: seletor ausente (LEGACY), coleta R09 e challenger shadow OFF,
nenhuma aprovação real, nenhuma ordem/mensagem/pausa. Sem publicação remota.
O índice dos quatro lotes existe só no checkout principal → inclusão PENDENTE.
