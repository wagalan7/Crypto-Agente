# Lote 03 — fechamento da integração governada (A, B, C, D)

Base: `581ad3d4fe843347992d245fb41983e9b2ce3a76` · branch `worktree-lote02-pesquisa`
· worktree `lote02-pesquisa` (autorizado). Nenhuma aprovação humana real, flag,
ENV, seletor, canário, ordem, mensagem ou pausa foi criada/alterada. Sem merge,
push ou deploy. O seletor operacional continua **LEGACY** (variável ausente) e a
candidata continua **desativada**.

---

## 1. Matriz dos quatro pontos

### A — início GOVERNADO do SHADOW pré-seleção

| | |
|---|---|
| **Defeito (RED)** | `main.p05_start_shadow` chamava `start_shadow(exp_id)` sem aprovação/geração. Para um experimento `PRE_SELECTION` isso era recusado pelo guard de tipo: **não existia caminho** para iniciar a coorte prospectiva pela API. |
| **Causa comprovada** | A rota era a legada (sem corpo) e o serviço público não despachava por tipo antes do loader pós-seleção. |
| **Correção** | `strategy_evidence_service.experiment_kind()` (leitura curta, sem lock de escrita) + despacho por tipo em `start_shadow()` ANTES do loader legado. PRE com `approval_id`+`expected_generation` → `start_preselection_shadow`; PRE sem eles → recusa mantendo o contrato conhecido (`EXPERIMENT_TYPE_MISMATCH` + `dispatch_to` + `requires`); POST_SELECTION preserva o contrato legado, inclusive a chamada SEM corpo; corpo de governança em tipo legado → `START_SHADOW_BODY_NOT_APPLICABLE`; tipo ilegível → `EXPERIMENT_TYPE_UNAVAILABLE`. A rota existente passou a aceitar corpo FECHADO (`confirm` literal, `approval_id` não vazio, `expected_generation` inteiro — extra, bool-como-número, string numérica e campo ausente são recusados) com `_r13_admin_principal`; erro interno devolve 500 com motivo seguro, sem stack nem segredo. |
| **Caller real** | `POST /api/strategy/p05/experiments/{exp_id}/start-shadow` (a mesma rota) → `strategy_evidence_service.start_shadow` → `start_preselection_shadow` → `operational_governance_service.assert_authority_in_session` (propósito SHADOW). |
| **GREEN** | `tests/test_lote03_start_shadow_contract.py` (11) — rota extraída do `main.py` real num app ASGI mínimo + despacho por tipo. No PG: `start_sem_aprovacao_e_bloqueado`, `start_com_aprovacao_inexistente_bloqueia`, `start_shadow_congela_a_autoridade`. |
| **Escopo** | Catálogo/API. Não liga seletor, não promove, não envia ordem. |
| **Migrações/flags** | Nenhuma. |
| **Dependência restante** | Aprovação SHADOW **real** (humana) para iniciar coorte real. |

### B — decisão candidata OBSERVACIONAL (quebra da circularidade)

| | |
|---|---|
| **Defeito (RED)** | (i) circularidade: a fidelidade exigia `CANDIDATE_SCANNER_SELECTION`, que só existe **depois** de promover, e promover exigia fidelidade; (ii) perda de persistência: `frozen_decision` descartava todo escopo que não fosse `FINAL_SCANNER_SELECTION` e `build_preselection_annotation` lia escopo/outcome sem gravá-los em `frozen` — `fidelity_comparable` era estruturalmente 0. |
| **Causa comprovada** | RED medido reintroduzindo exatamente os dois pontos (script `red.sh`): **11 falhas + 5 erros** em `tests/test_lote03_shadow_decision.py`. |
| **Correção** | Escopo próprio `CANDIDATE_SHADOW` (`SHADOW_DECISION_VERSION=R13_CANDIDATE_SHADOW_DECISION_V1`): `shadow_group_decision` observa **ALL-TF antes da escolha** reusando a MESMA `candidate_decision` do caminho operacional (núcleo puro compartilhado, não cópia — provado por espião de chamadas), com autoridade de propósito **SHADOW**; estados `SELECTED` / `REJECTED` (recusa por score CONHECIDO) / `UNKNOWN` (feature/modelo/calibração ausente), vencedor por `MAX_CANDIDATE_SCORE`, `group_hash`. Fronteira de propósito: `freeze_context(require_purpose="CANARY")` e `assert_authority_in_session(require_purpose="CANARY")` → contexto SHADOW nunca autoriza reserva/alavancagem/POST (`CANDIDATE_PURPOSE_MISMATCH` / `AUTHORITY_PURPOSE_MISMATCH`). Allowlist versionada ampliada (`OBSERVED_DECISION_SCOPES`, `shadow_decision` com campos fechados e limite de TFs); a anotação passa a congelar `observed_decision_scope`, `shadow_decision`, `candidate_score_config_hash` e `candidate_min_score` **antes de qualquer preço futuro**, cobertos pelo `annotation_hash`. Fidelidade = decisão OBSERVADA × recomputação verificável do contrato na MESMA oportunidade/população/estágio, com hashes de manifesto/config/aprovação/denominador reconciliados; publica comparáveis, divergentes, UNKNOWN, cobertura, denominador e motivos. Ausência ⇒ `None` + lacuna + NO_GO, **nunca 0%**. |
| **Caller real** | `recommendation_service.get_recommendations_via_vision` (ciclo oficial): autoridade SHADOW carregada UMA vez por ciclo (só com coleta ON), grupo por símbolo sobre os MESMOS TFs avaliados, bloco por TF em cada linha observada → `decision_observation_service.observe_preselection` → `preselection_observation_service.frozen_decision` → `flush_pending`/`_admit` → `prospective_shadow_service.build_preselection_annotation`. |
| **GREEN** | `tests/test_lote03_shadow_decision.py` (32). No PG: `autoridade_shadow_vem_do_banco_sem_promocao`, `decisao_observacional_persiste_pela_cadeia_oficial` (linha gravada, commit e leitura por conexão NOVA), `autoridade_test_only_nao_cria_anotacao_prospectiva`. Paridade do champion provada com a MESMA lista/ordem/valores com e sem autoridade. |
| **Escopo** | Observação. Seletor permanece LEGACY/OFF; `rec.operational_selection` continua `None`. |
| **Migrações/flags** | Nenhuma (JSONB oficial). Nenhum loop/rede/DDL novo. |
| **Dependência restante** | Manifesto de pesquisa **APROVADO** (não TEST_ONLY) para o carregador automático; features ponto-no-tempo V3 exigem o produtor R13 (presente no scan quando `_capture_research_inputs` roda). |

### C — proteção SIMULADA explícita (fim da inferência)

| | |
|---|---|
| **Defeito (RED)** | `operational_measurements` inferia proteção pendente de "economia ausente" e o `elif` apagava a trilha de resolução quando as duas falhas ocorriam na mesma linha. Não existia fonte de proteção alguma. |
| **Causa comprovada** | RED medido reintroduzindo a ausência da trilha + o `elif` (`red_c.sh`): **3 falhas + 18 erros** em `tests/test_lote03_shadow_protection.py`. |
| **Correção** | Fonte explícita e versionada no motor OFICIAL (`offline_replay_service`): `PROTECTION_SOURCE="SHADOW_SIMULATED"`, `PROTECTION_VERSION="R13_SHADOW_SIMULATED_PROTECTION_V1"`, `PROTECTION_PRODUCER="OFFICIAL_R09_OFFLINE_REPLAY"`, `proves_real_sl=False`. Instrumentação MÍNIMA e ADITIVA (nenhum campo econômico/status alterado — 50 regressões R10A verdes e teste explícito de aditividade): observa do fill virtual até saída/fim de janela a obrigação, qtd/exposição remanescente, stop ativo finito, geometria **julgada pelo estágio** (BE/lucro pós-TP1 é válido, não reprovado pela geometria inicial), transições BE/trail percorridas e falhas pendentes/resolvidas. Consumo: `protection_measure` confere fonte/versão/produtor/escopo e reconcilia `config_hash`, `cost_config_hash`, oportunidade e identidade de resolução; conta **trilhas separadas** (`resolution_failures`, `economics_failures`, `economic_duplicates`, proteções pendentes) sem `elif`; zero só com observação APLICÁVEL e cobertura ≥ 90% sobre denominador auditável; sem fonte/trilha/obrigação ⇒ `None` + motivo. Saída lucrativa com runner aberto continua `PROTECTION_OBLIGATION_UNRESOLVED`. |
| **Caller real** | `prospective_shadow_service.resolve_annotation` (resolver R09 oficial, janelas já coletadas) → `protection_measure` → `summarize_prospective` → `preselection_experiment_service.go_no_go`. |
| **GREEN** | `tests/test_lote03_shadow_protection.py` (22). No PG: `protecao_simulada_sobrevive_ao_resolver_oficial_e_ao_restart` (resolver oficial em PG16 + leitura por conexão nova), `protecao_simulada_nunca_vira_sl_real_nem_promove`, `funding_indisponivel_nao_vira_zero_e_bloqueia`. |
| **Escopo** | **SHADOW_SIMULATED** — prova apenas proteção simulada no gate Shadow. **Nunca** SL/TP real, ramp ou `OPERATIONAL_ACCEPTED`. |
| **Migrações/flags** | Nenhuma. Nenhum segundo executor (fonte verificada sem `place_order`/`binance`/`set_leverage`). |
| **Dependência restante** | `BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED`: se a PRIMEIRA promoção exigir SL **REAL**, o Shadow não prova isso. `PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION=False` mantém a lacuna essencial e o gate em NO_GO até decisão humana específica (mudá-la é alteração de código revisada, nunca flag/ENV). |

### D — matriz integrada pelo caller real

| | |
|---|---|
| **Defeito (RED)** | O harness governado parava sem causa comprovada; "fixture incompleta" era hipótese. |
| **Causa comprovada** | Instrumentação LOCAL de log no harness isolou, em ordem: (1) `MANUAL_ACCOUNT_VALIDATION_BLOCKED` — faltava a época de validação da conta; (2) `EXEC_DEPTH_TIMESTAMP_INVALID` — relógio congelado do fixture × `datetime.now` real das bordas; (3) `FREE_MARGIN_STALE` — `entry_intent_service._now` com relógio real; (4) `CANDIDATE_CALIBRATION_UNAVAILABLE` — faixa 70–80 é a única suportada pelo artefato real (contrato, não ajuste); (5) `CANARY_RISK_LIMIT` — teto aprovado 0,5% × risco 1,0% (controle negativo legítimo); (6/7) semântica de identidade/revogação de aprovação; (8) ponto REAL de injeção da revogação é o `depth` do preflight; (9) DNS da borda pública OKX vindo de thread de pool. |
| **Correção** | Fixture completada pelos contratos reais + matriz ampliada no MESMO harness. |
| **GREEN** | `tests/pg_integration_lote03_governed.py`: **53 verificações, EXIT_CODE=0, duas execuções após a última edição.** PG16 descartável UTF-8, Python 3.11/asyncpg reais, socket Unix, **TCP = 0** e DNS bloqueado e CONTADO (32 tentativas, todas `www.okx.com`, borda pública de preço de marca declarada). |
| **Cobertura** | paridade OFF×LEGACY pelo caller real (mesma entrada, mesma qty, mesmas chamadas, sem contexto candidato no payload legado); candidato completo scanner→reserva→proposta→assinatura falsa→proteção oficial com UM efeito lógico; filha `-mfb` com proposta e autorização FINAL próprias (GTX −5022 → fallback MARKET); último slot (`CANARY_ORDER_LIMIT`) com controle positivo; posição manual preservada; dois consumidores (controle positivo sequencial + concorrência real); restart (pool derrubado, autoridade e intenção relidas do banco); expiração e drift de bundle; revogação (antes e DURANTE a espera); aprovar×revogar concorrente; dois promotores com um efeito; rollback preservando posição/proteção e bloqueando nova candidata; status read-only; ordem GLOBAL do lock com **duas conexões REAIS e barreira no ponto real** (transação financeira `917283` segurando a LINHA do singleton; escritor de governança `505202609` espera e conclui — deadlock provaria ordem cruzada); concorrência repetida **2×** após a última mudança; cobertura de B e C pela cadeia prospectiva oficial. |
| **Escopo** | Local, hermético, aprovações TEST_ONLY por opt-in PRIVADO do teste (`_ALLOW_TEST_APPROVALS`), nunca ENV. |

---

## 2. Revisão final pelos quatro callers (awaits que envelhecem autoridade)

**1. Scanner / coleta** — `get_recommendations_via_vision`
`await load_shadow_authority` (leitura curta, rollback interno) → awaits longos de
símbolos/OHLCV/regime/learning. **Reconferência:** cada `candidate_decision` do
grupo revalida `expires_at_ms` com o relógio do momento (aprovação vencida vira
`UNKNOWN`, nunca `SELECTED`) e a população aprovada. A decisão observacional
**não autoriza nada**, e a anotação guarda `approval_id`+geração usados, de modo
que revogação posterior é detectada na medição por reconciliação. O campo
operacional que autoriza intenção (`rec.operational_selection`) nunca recebe
contexto Shadow.

**2. Reserva / readmissão** — `entry_intent_service.reserve`
`await pg_advisory_xact_lock(917283)` → relógio capturado **depois** da espera →
`assert_authority_in_session(..., require_purpose="CANARY")` DENTRO da transação,
com row locks singleton→experimento → teto de ordens contado na MESMA transação →
ownership → geração de margem → gravação. Readmissão (`_bind_proposal_in_session`)
recusa perder ou acrescentar candidata.

**3. Transporte / assinatura** — `shadow_trade_service` → `binance_signed_service`
awaits de quote, depth, alavancagem, POST e polling. **Reconferência:**
`_intent_guarded_preflight` (fronteira do depth, antes do POST),
`_intent_final_authorization` (imediatamente antes de assinar) e
`_intent_candidate_guard` em alavancagem/retry/fallback. A filha `-mfb` repete a
autorização final com dispatch id e proposta próprios. Legado recebe `None`.

**4. Catálogo / anotação / resolver** — `strategy_evidence_service`,
`prospective_shadow_service`, `decision_observation_service._admit`
start: lock P05 → `with_for_update` → autoridade na MESMA transação → congelamento
(puro) → commit. `_admit`: autoridade e insert na mesma transação. `evaluate`:
reconfere identidade/contrato no segundo lock antes de gravar (`FROZEN_MISMATCH`).
`promote`: CAS por geração.

**Verificações exigidas:** nenhum contexto Shadow vira autoridade CANARY (todos os
`freeze_context` operacionais usam o padrão CANARY); nenhuma leitura de ORM após
`rollback()` (primitivos capturados antes em todos os ramos); nenhum dado ausente
vira aprovação ou zero.

---

## 3. Estados (separados de propósito)

**IMPLEMENTADO / LOCAL_VERIFIED** — A, B, C, D acima, com provas locais.
**PUBLISHED_INACTIVE** — nada publicado nesta execução (sem push/deploy); o commit
local é infraestrutura INATIVA.
**WAITING_DATA** — amostra prospectiva real (100 trades / 30 por playbook / 14
dias / 10 úteis / cobertura ≥ 90%); fidelidade comparável exige observação
candidata com features ponto-no-tempo V3.
**WAITING_DECISION** — `BLOCKED_PROTECTION_SCOPE_DECISION_REQUIRED` (proteção
simulada satisfaz o requisito da primeira promoção, ou ela exige SL real?);
manifesto de pesquisa aprovado (hoje TEST_ONLY).
**WAITING_CANARY_APPROVAL** — aprovação CANARY própria + seletor explícito;
`ELIGIBLE` por si só nunca autoriza POST.
**OPERATIONAL_ACCEPTED** — **não alcançado** e não alcançável por este lote.

Critérios de `GoNoGoCriteria` **inalterados**: 100 trades, 30/playbook, 14 dias,
10 dias úteis, cobertura 90%, EV ≥ 0,05R, incerteza ≤ 0,05R, DD ≤ 8R,
estabilidade ≥ 0,5, zero falhas/duplicatas/proteções pendentes, fidelidade ≤ 5%.

---

## 4. Provas executadas

- Suítes Lote 03 (A/B/C/D + fechamento/adapter/governança/prospectivo/API/transporte): **verde**.
- Suíte backend completa: **2.846 testes, OK, 2 skips históricos R05C declarados** (fixture auditada privada e commits da fase R05C; nenhum skip novo).
- `tests/pg_integration_lote03_governed.py`: **53 verificações, exit 0, 2×** após a última edição.
- Regressões PG verdes: manual boot (46) · dispatch (32) · coexistence (131) · margin (46) · single (73) · closure (75) · P03 intent · P03 proof (20) · P03 cycle (21) · P03 conflict (66) · P03 settlement (22) · R05 clock (10) · R05 reservas (31) · R05 transferência (16) · R05D gate (16) · Lote 01 financeiro (31) · R09 (17) · R09 pré-seleção (29) · R10B (14) · R11 state (11) · R11/R12 pipeline (140) · Lote 02 pesquisa (19) · Lote 02 closure (26+19) · H persistido (31).
- `py_compile` dos 12 arquivos de produção e dos harnesses tocados; `git diff --check` aprovado. Nenhum `.ts/.tsx` alterado → TSC não se aplica.

### Falhas PRÉ-EXISTENTES encontradas e corrigidas (com prova de anterioridade)

1. **Ownership com prova fresca** (`require_fresh_proof=True`, commit `20580139`) é posterior à última atualização dos harnesses `r05_reservations`, `p03_intent`, `p03_proof`, `p03_cycle`, `p03_settlement`, `r05_transferencia` (`46c7d7f0`) — provado por `git merge-base --is-ancestor`. Sem a época da conta e sem a tabela de reconhecimento manual, toda reserva era negada com `MANUAL_POSITION_SYMBOL_BLOCKED`. Fixtures completadas (criam as tabelas e declaram a conta liberada; **não** desligam o guard).
2. **Payload v2** (`feature_evidence` em `frozen_decision`) entrou em `581ad3d4`, posterior a `a39ab358` do harness `r09_preselection`: a expectativa de `schema_version` v1 ficou obsoleta. Passou a aceitar as versões DECLARADAS do contrato.
3. **`contract_hash_of` exige o corpo inteiro** (inclui a chave de hash): o contraexemplo de escopo em `r11_r12_pipeline` nascia sem hash. Fixture corrigida.

### Chamadores reais atualizados pela mudança de contrato (Lote 03)

- `r11_r12_pipeline`: start do SHADOW pré-seleção passou a exigir aprovação (novo check de recusa + opt-in de engenharia DECLARADO para o ciclo do catálogo — a autoridade real é provada no harness governado); promoção do tipo agora é governada (`PROMOTION_IDENTITY_INVALID` sem identidade de aprovação), sem seletor, ENV ou ordem.
- **Correção de escopo própria, encontrada por essa regressão:** exigir coorte prospectiva no start/avaliação de **todo** experimento PRE_SELECTION bloqueava o challenger de **GESTÃO** (`MANAGEMENT_ONLY`), que não tem coorte de seleção. Agora a coorte é exigida só para `SELECTION_ONLY`; gestão segue avaliada pelo estudo congelado, com `comparison_scope`, `prospective_cohort` e `offline_used` DECLARADOS no retorno.

---

## 5. Runbook — preparação de publicação INATIVA

1. Commit local apenas dos arquivos do Lote 03 (feito nesta execução). Sem amend, merge, push ou deploy.
2. Publicar depois, se o usuário quiser, como infraestrutura INATIVA: `R13_OPERATIONAL_SELECTOR` **ausente** (LEGACY), `P05_CHALLENGER_SHADOW_ENABLED` e coleta R09 OFF no ambiente.
3. Nada a migrar: nenhuma DDL, endpoint novo, worker ou loop.
4. Para observar (quando houver manifesto aprovado): ligar a coleta R09 e registrar aprovação SHADOW real. Isso **não** liga execução.
5. Ativação da candidata exige, em ordem: decisão humana do escopo de proteção → coorte prospectiva suficiente → gate GO_CANDIDATE → aprovação PROMOTION → promoção governada → aprovação CANARY própria → seletor explícito. Cada passo é do usuário.

## 6. Integração documental pendente

`docs/FECHAMENTO_FINAL_4_LOTES_INDICE.md` existe **apenas no checkout principal**
e, por regra desta execução, **não foi editado**. A inclusão do item 03 no índice
fica explicitamente **PENDENTE**.
