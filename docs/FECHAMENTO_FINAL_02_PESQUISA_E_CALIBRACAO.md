# Lote 02 — comparação real, coleta pré-seleção e calibração V3

Base: `main` em `d0bf52e7` (Lote 01 integrado). Itens 3+4+5 do índice.
**Nenhuma estratégia, coleta ou calibração foi ativada**; nenhuma quarentena foi
liberada; sem push/deploy. A candidata real **não** foi escolhida: o estudo real
continua `BLOCKED_MISSING_DECISION`.

## 1. O que existia e o que faltava

| Lacuna declarada no prompt | Situação agora |
|---|---|
| `authorized_comparison()` sempre bloqueado | Lê e VALIDA um manifesto fechado; sem manifesto, continua bloqueado com a decisão que falta |
| Comparação era `MANAGEMENT_ONLY` e não provava seleção | Despacho por escopo; `SELECTION_ONLY` com decisão real de cada lado |
| Coleta pré-seleção existia mas no ponto errado | Captura ANTES do `max(scored)`, com todos os TFs avaliados, veto macro e cobertura dos retornos antecipados |
| `V3Calibration` só validava metadados (`probability=None`) | Modelo R08E real: ajuste, artefato, verificação, previsão e validação OOS |

## 2. Manifesto autorizado (`research_manifest_service`)

Contrato fechado e versionado `R13_RESEARCH_MANIFEST_V1`, com hashes por seção
(`baseline`, `candidate`, `population`, `costs`, `split`) **+ `bundle_hash`**,
todos calculados **antes** de qualquer resultado, e `manifest_hash` sobre o corpo
inteiro.

Bloqueiam: campo desconhecido/ausente, booleano no lugar de número, NaN/inf,
divisão temporal fora de ordem ou sem purga, fonte/unidade/ativo de custo
incompatíveis, disponibilidade `OBSERVED_ACCOUNT_COSTS` declarada para um modelo,
tratamento de fee/slippage/funding desconhecido, escopo não implementado, mudança
que escapa do componente declarado, lados idênticos com mudança declarada, regra
de seleção não implementada e **drift** de hash.

Estados derivados da decisão registrada: `DRAFT` → `BLOCKED_MISSING_DECISION`,
`APPROVED_TEST_ONLY` (engenharia/teste; `real_study_allowed=false`) e
`APPROVED_RESEARCH` (exige autoridade + referência verificável + instante).
`approved: true` como propriedade solta é **campo desconhecido** — recusado.

**Decisão que falta (só o usuário responde):** baseline congelada (champion
`CHAMPION_LEGACY/SCORE_V2` ou default do núcleo R07D), candidata e a ÚNICA
mudança dela, e escopo/população/custos comparáveis.

## 3. Coleta oficial pré-seleção

- O **gate é lido uma vez, antes de qualquer trabalho novo**: desligado, o
  scanner não constrói linha, não calcula score de pesquisa, não pede a lista de
  avaliados e não grava nada — e a lista/ordem de recomendações é idêntica
  (provado pelo entrypoint real, nos dois modos).
- `_best_tf_for_symbol_server(..., evaluated_out=)` espelha os candidatos
  avaliados **antes** do `max(scored)`; o vencedor e o desempate não mudam. Os
  TFs que não venceram entram como `SELECTION: REJECTED /
  TIMEFRAME_NOT_SELECTED`, com as etapas posteriores `NOT_EVALUATED`.
- **Veto macro (`block_all`)**: nenhuma recomendação, mas os candidatos
  existentes são registrados como `VETOED` em `MTF_REGIME / REGIME_BLOCK_ALL` e
  contabilizados na cobertura.
- Retornos antecipados (blackout, sem símbolos, timeout, orçamento, sem
  candidato) registram **cobertura de ciclo** com motivo — sem oportunidade,
  preço ou outcome fabricados (`is_opportunity=false`).
- Payload versionado: `r09.pre.v1` (igual ao de antes, byte a byte) e
  `r09.pre.v2` com `features` ponto-no-tempo + `evaluation` (TFs avaliados ×
  escolhido). Produtor, exportador e manifesto aceitam as duas.
- **Não ativado**: `R09_PRESELECTION_MODE` permanece `inactive` no ambiente. Para
  observar (etapa posterior, após revisão): setar `R09_PRESELECTION_MODE=observe`
  no serviço desejado e reiniciar; a coleta é somente leitura do scan e não toca
  recomendações, ranking, gates ou executor.

## 4. Comparação integral por escopo

`MANAGEMENT_ONLY` continua intacto (mesmo corpo de pedido, mesmo `request_hash`,
mesmo contrato V1). `SELECTION_ONLY` é novo e fechado:

- pedido: `candidate.kind=SELECTION_ONLY` com `selection` (núcleo, score,
  playbooks, regra de corte) e **gestão idêntica** nos dois lados — gestão
  diferente é recusada;
- decisão: baseline = decisão **observada** do champion (reconstruir a baseline
  de hoje não representa a configuração histórica); candidata = motor dela
  (`score_v3_service.score` com o corte congelado). Feature ausente é `UNKNOWN`,
  nunca `REJECTED`; `UNKNOWN` de um lado exclui a linha dos **dois** e entra na
  cobertura;
- contrato: `R12_PRE_SELECTION_CONTRACT_V2` inclui `manifest_hash` e
  `selection_config` no corpo hasheado; o verificador exige V2 no escopo de
  seleção e o **envelope do catálogo do mesmo tipo** — candidato de seleção não
  passa como de gestão, nem o contrário;
- circuito provado: manifesto → `parse_request`/export → despacho do pipeline →
  decisão por lado → dois replays (gestão congelada) → contrato persistido →
  `verify_study_identity` → catálogo, com restart.

## 5. Calibração V3 (`score_v3_calibration_service`)

Contrato `R08E_V3_CALIBRATION_V1`: faixas fixas de 10 pontos (a última inclui
100), mínimo global de **200 observações únicas** e **30 labels por faixa**,
`p = sucessos/n`, Wilson 95%, cobertura e proveniência. Faixa insuficiente **não
é servida** (sem herdar vizinho, global, V2 ou 0,5); nada de monotonicidade
imposta ou suavização; `p` nunca é `score/100`.

Evento explícito por artefato (horizonte, censura/expiração, população):
`P_TP1_BEFORE_STOP`, `P_TP2_BEFORE_STOP` e `P_NET_RESULT_POSITIVE` **não** são
intercambiáveis. Label conhecível depois do corte não treina. Artefato
inválido/vencido/revogado/de outro fingerprint, população, evento ou dataset é
`UNAVAILABLE`. Estados separados: `FITTED` ≠ `OOS_VALIDATED` ≠
`ECONOMICALLY_APPROVED` — este último **não é concedido aqui**.

`net_ev` só aplica a fórmula binária quando evento e payoff correspondem; para
gestão parcial com runner existe `net_ev_from_payoff`, que exige o payoff líquido
**OOS** da gestão congelada, a fonte e o tamanho da amostra. Nenhuma
probabilidade de evento diferente alimenta sizing/Kelly.

**`DECISION_REQUIRED`**: os limites de aceitação da calibração OOS não existem;
o artefato declara `CALIBRATION_OOS_THRESHOLDS_DECISION_REQUIRED` e não se
aprova sozinho.

## 6. Status honesto

`research_batch_service.evidence_status()` deriva cada linha do que existe:
coleta (cobertura registrada, `last_observed_at`, qualidade), calibração (estado
do artefato) e aprovação humana (manifesto). **Erro de leitura é `ERROR`, nunca
`NOT_STARTED`**, e o GET não dispara replay, walk-forward ou fitting.

## 7. Provas executadas

| Prova | Resultado |
|---|---|
| `test_lote02_research_manifest` | 16 OK |
| `test_lote02_preselection_capture` (entrypoint real do scanner) | 8 OK |
| `test_lote02_selection_scope` | 19 OK |
| `test_lote02_v3_calibration` | 21 OK |
| `test_lote02_status_e_sentinelas` | 9 OK |
| `pg_integration_lote02_pesquisa.py` (PG16 descartável) | 18 verificações, **2×** |
| Regressões PG: `r11_r12_pipeline` (138) · `r09_preselection` (29) · `r10b` | OK |
| Suíte completa | 2.639 testes, OK, **2 skips R05C declarados** |
| `py_compile` + `git diff --check` | limpos (nenhum arquivo de frontend alterado) |

## 8. Limitações reais e dependências externas

1. **Candidata real não decidida** → estudo real `BLOCKED_MISSING_DECISION`.
   Os manifestos usados nos testes são `APPROVED_TEST_ONLY`.
2. **Features de estrutura/gatilho do Score V3 não são calculadas pelo scan
   champion** (`structure_quality`, `level_distance_atr`, `trigger_body_ratio`,
   `trigger_follow_through_atr`). Elas ficam ausentes na captura, o modelo real
   devolve `UNAVAILABLE` e um estudo `SELECTION_ONLY` sobre a captura do champion
   fica **WAITING_DATA** — não produz decisão inventada.
3. **Amostra prospectiva não existe**: a coleta está desligada. Sem ela não há
   calibração com 200/30 nem evidência econômica.
4. **Cobertura de ciclo é telemetria em memória** (últimos 20 ciclos, perdida em
   restart), não evidência de estudo. Nenhuma DDL foi criada.
5. **Custos**: só `DECLARED_MODEL` (bps do R10A). `OBSERVED_ACCOUNT_COSTS`
   depende da fonte do Lote 01 e não foi usado aqui.
6. Holdout permanece selado; limites de aceitação OOS e canário continuam
   decisões externas.
