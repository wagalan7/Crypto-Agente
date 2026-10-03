# Lote 01 — correção conjunta sobre a baseline `e995b63b`

Escopo: os **cinco** defeitos da auditoria `AUDITORIA_LOTE01_e995b63b.md`,
corrigidos em um único pacote. Arquivo de produção tocado: **apenas**
`backend/services/execution_accounting_service.py`.

## Três conceitos que a baseline misturava

| Conceito | Pergunta | Onde vive agora |
|---|---|---|
| **Integridade** | a prova persistida chegou intacta? | `integrity_hash` (registro inteiro, metadados de observação inclusive) |
| **Equivalência material** | duas provas descrevem a MESMA comissão e o MESMO dinheiro? | `material_id` (identidade + quantidade + ativo de liquidação + valor + referência do evento da fonte) |
| **Progresso útil** | uma comissão EXIGIDA saiu de não-confirmada para confirmada? | `fee_confirmed_keys(antes) × fee_confirmed_keys(depois)`, medido sob bloqueio de linha |

Hash íntegro não prova conta/origem; hash diferente por carimbo de consulta não
é conflito econômico; mais objetos no JSON não é progresso.

## Matriz R1–R5

### R1 — lançamento do ledger aceito sem normalização

* **RED (baseline, API da própria baseline):** `symbol` de outro mercado,
  crédito **positivo**, `tranId` ausente e linha **sem vínculo** com a comissão
  estrangeira **confirmavam** `0.42` (`fee_assets_unconverted: []`).
  `income_type` errado e `time` fora da janela já eram recusados.
* **Causa:** o coletor indexava `por_trade[tradeId] = linha` e só depois lia o
  valor; nenhum normalizador por linha, nenhuma identidade de evento.
* **Correção:** `normalize_commission_ledger_row` valida ANTES de indexar
  qualquer valor — conta consultada, exchange/mercado/símbolo normalizados (sem
  colapsar USDT/USDC), `incomeType` exatamente `COMMISSION`, `asset` igual ao de
  liquidação, `tradeId` ∈ `exec_id` atribuídos (recusando bool/float
  impreciso), `tranId` presente, `income` finito e **não positivo**
  (positivo ⇒ `LEDGER_CREDIT_NOT_SUPPORTED`, nunca `abs`), `time` dentro da
  janela **efetivamente consultada** e **vínculo explícito** com a comissão do
  fill (`commissionAsset` + `commission`) — sem ele,
  `LEDGER_FOREIGN_LINK_NOT_UNAMBIGUOUS` e o fill continua bloqueado.
  `index_commission_ledger` indexa por `(trade_id, tran_id)`: mesmo `tranId` e
  mesmo material ⇒ no-op; mesmo `tranId` com material diferente ⇒
  `LEDGER_SOURCE_CONFLICT`; `tranId` distintos para o mesmo fill ⇒
  `LEDGER_MULTIPLE_EVENTS_FOR_FILL` (bloqueia; não escolhe último/maior/menor
  nem soma). A referência normalizada é **persistida** (`source_ref`) e entra
  nos dois hashes.
* **GREEN:** `R1LedgerNormalizadoEValidado` (13 incompatibilidades, duplicata
  real, eventos distintos, conflito de fonte, referência persistida).

### R2 — prova íntegra com contexto divergente era aproveitada

* **RED:** prova com `account_scope` de outra conta, outra `exchange`,
  `fill_time_ms` divergente ou janela que **não contém** o fill devolvia
  `quality=CONFIRMED` e valor `0.42`.
* **Causa:** o veredito conferia só `fill_key`/ativo/quantidade — campos que a
  própria prova carrega. Não existia contexto esperado.
* **Correção:** `build_expected_context(identity=, fill=)` monta o contexto a
  partir da identidade contábil e do fill **realmente atribuído** (12 campos),
  passado explicitamente a construtor, veredito, resolvedor e finalizador e
  **reconferido no merge sob bloqueio**. Validação obrigatória de contrato,
  tipos, IDs, números finitos, zero vs ausência, hash recalculado, contexto
  exato, vínculo do `source_ref` com o fill, janela contendo o instante,
  carimbos não futuros, coerência fonte/qualidade, `valor = −income` da fonte e
  `valor ≈ preço × quantidade` (preço é representação, não prova nova).
  Sem contexto suficiente: `FEE_CONVERSION_CONTEXT_MISSING` — o argumento
  opcional **não** é fail-open.
* **GREEN:** `R2ContextoEsperadoEmTodasAsFronteiras` (9 divergências, janela sem
  o fill, ausência de contexto, contrato legado) e
  `MatrizFinalDeFechamento.test_adulterada_e_rehasheada_nao_passa_pelo_contexto`.

### R3 — carimbo de consulta virava divergência econômica

* **RED:** duas coletas do MESMO snapshot aberto (`now=T` e `T+1s`) produziam
  hashes diferentes com o mesmo dinheiro; não havia função de precedência (o
  merge decidia por existência da chave).
* **Correção:** dois papéis separados (ver tabela acima) — `observed_*`,
  `observed_at_ms`, duração/janela de consulta e tentativa **não** entram na
  materialidade; `quality` também não (promoção é transição de qualidade).
  `merge_fee_proof` implementa a precedência pura, reusada no merge
  transacional: ausente+válida ⇒ `STORED`; equivalente com janela nova ⇒
  `ENRICHED`/`IDEMPOTENT` (sem conflito); `ESTIMATED`+`CONFIRMED` ⇒ `PROMOTED`
  com histórico (`superseded`, teto `FEE_HISTORY_LIMIT=4`); `CONFIRMED` + estimativa
  atrasada ⇒ `KEPT_CONFIRMED`; confirmadas materialmente divergentes ⇒
  `CONFLICT` preservando a original e registrando a contraprova; inválida ⇒
  `REJECTED` (sem progresso).
* **GREEN:** `R3MaterialidadeEPrecedencia` (duas ordens de aplicação, promoção,
  estimativa atrasada, divergência material) + provas PG `l01c_*`.

### R4 — zero estrangeiro comprovado exigia conversão

* **RED:** comissão **exatamente 0 BNB** entrava em
  `fee_conversion_required`/`fee_assets_unconverted` e o `net_trade` ficava
  `None` — zero conhecido tratado como desconhecido.
* **Correção:** `_fee_conversions_required` e `compute_totals` exigem conversão
  só de comissão estrangeira **estritamente positiva**. Zero finito com ativo
  válido é custo zero conhecido (sem consulta ao ledger);
  ausente/`None`/bool/NaN/inf/negativa continua **desconhecida** e derruba a
  completude. `fees_by_asset` segue informativo (`BNB: 0`).
* **GREEN:** `R4ZeroEstrangeiroComprovado` (zero BNB ⇒ `9.95` e zero consultas
  de COMMISSION; ausente/NaN/inf/negativa ⇒ desconhecido; mistura zero+positiva
  exige só a positiva).

### R5 — 49 conversões terminavam em FAILED

* **RED:** 49 comissões em lotes de oito ⇒ `state=FAILED`, 48 convertidas,
  `attempts=6`: a incompletude saudável do lote era contada como falha.
* **Correção:** o desfecho do coletor é **explícito** (`COMPLETE`, `PROGRESS`,
  `PARTIAL_LIMIT`, `SOURCE_UNAVAILABLE`, `ERROR`) e `complete` considera
  **todas** as exigências, não os oito primeiros. A seleção parte dos fills
  atribuídos **ainda não confirmados após validação** (existência da chave não
  basta). `PARTIAL_LIMIT`/`PROGRESS` são espera cadenciada (60 s), não
  tentativa falhada; erro real da fonte mantém backoff finito com motivo
  honesto. Progresso **útil** é medido por confirmações novas (promoção conta;
  duplicata, janela atualizada ou objeto estrangeiro não) e reinicia as
  tentativas consecutivas.
  Metadado aditivo no JSON: `observation_id` (determinístico — replay
  reconhecível) e `base_generation` (geração lida ANTES da coleta); a geração da
  linha avança **sob bloqueio**. Resposta de geração anterior ainda contribui
  **eventos**, mas não tem autoridade sobre estatística: falha atrasada não
  ressuscita `attempts` nem rebaixa confirmação/progresso concorrente; o mesmo
  `observation_id` reaplicado não incrementa nem reinicia de novo; resposta
  antiga sem metadado não manda na estatística atual; falha real da geração
  corrente conta **uma vez**.
* **GREEN:** `R5ProgressoLoteEConcorrencia` — 49 conversões completam em **≤7
  passes úteis**, sem `FAILED`, com relógio **simulado** respeitando
  `next_retry_at` (sem `sleep`); limite de oito por passe; fonte sem registro
  mantém retry finito; replay não conta duas vezes.

## Provas executadas

| Prova | Resultado |
|---|---|
| RED medido na baseline `e995b63b` (API da própria baseline) | 5/5 defeitos reproduzidos (`R1`×4 subcasos, `R2`×4, `R3`×2, `R4`, `R5`) |
| `tests/test_lote01_correcao_conjunta.py` (novo) | 29 testes OK (2× no estado final) |
| `tests/test_lote01_fee_conversion.py` (os 27 do Lote 01, adaptados ao V2) | 27 OK |
| `tests/pg_integration_lote01_financeiro.py` (PG16 descartável) | 21 verificações OK (13 do lote + 8 novas), 2× no estado final |
| `tests/pg_integration_r05c.py` · `pg_integration_r05d_gate.py` · `pg_integration_r05_clock.py` | OK (16 e 10 verificações; R05C sem regressão) |
| Suíte completa | 2556 testes, OK, 2 skips declarados (R05C privados) |
| `py_compile` + `git diff --check` | limpos |

As provas PG usam **duas conexões reais** com bloqueio de linha
(`asyncio.gather` de dois `apply_accounting`) e verificam efeito em **P&L** e
inclusão/exclusão no `accounting_total` — não só no JSON.

## Autoauditoria das fronteiras (§9)

| Fronteira | Quem escreve/lê | Prova |
|---|---|---|
| `fee_conversions` (JSON) | só `merge_accounting` escreve (via `merge_fee_proof`); leem `compute_totals`, `resolve_fee_conversions`, `fee_confirmed_keys`, `_fee_conversions_required`. Fora do módulo, apenas `financial_total_service` lê `totals["fee_assets_unconverted"]` | `l01c_*` (PG) + `PersistenciaDaEvidencia` |
| contexto/conta | única origem `build_expected_context`; `fee_expected_contexts` deriva dos fills atribuídos; `merge_accounting` **recalcula** quando o chamador não passa (é a reconferência sob bloqueio) | `R2*`, `l01c_reobservacao_equivalente_*` |
| hash × materialidade | `fee_conversion_hash` e `fee_conversion_material_id`; ambos recalculados no veredito | `test_adulterada_e_rehasheada_*`, `test_hash_sobrevive_ao_ida_e_volta_do_json` |
| qualidade | derivada da fonte no construtor; `CONFIRMED` exige `source_ref` do ledger e `valor = −income` | `test_confirmed_forjado_*`, `l01c_estimativa_nao_vira_dinheiro_*` |
| `attempts`/`next_retry_at` | só `schedule_retry`, `_retry_after_observation` e `merge_observation` (este último somente com autoridade de geração) | `R5*`, `test_falha_atrasada_*`, `l01c_falha_atrasada_*` |
| `generation`/`observation_id` | escritos apenas em `merge_observation` (sob `FOR UPDATE`) e carimbados por `stamp_observation` no coletor | `test_replay_de_falha_*`, `l01c_replay_nao_avanca_geracao_*` |
| `finalize_accounting` | **as duas** chamadas de `compute_totals` recebem `fee_context` — inclusive a que descarta funding quando o estado não é `CONFIRMED` | `l01c_estimativa_nao_vira_dinheiro_*` (estado não-confirmado) e `l01_conversao_confirmada_*` |
| fonte→construtor→veredito→precedência→merge→retry→projeção→consumidor | nenhum chamador restou na assinatura permissiva antiga (varredura em `services/`, `models/`, `main.py` e `tests/`) | suíte completa + harnesses PG |

Nenhuma fixture, gate ou validação foi relaxada para passar. Estados
distinguidos com honestidade: **fonte ausente** (`BLOCKED_SOURCE_UNAVAILABLE` /
`SOURCE_UNAVAILABLE`, espera de fonte), **erro de código/contrato** (recusa com
motivo próprio, não concluído) e **observação operacional ainda não executada**
(declarada como pendência, não como prova).

## Versão e compatibilidade

* Contrato novo: `R05E_FEE_CONVERSION_V2`. O anterior
  (`R05E_FEE_CONVERSION_V1`) é **legado**: o veredito devolve
  `FEE_CONVERSION_UNVERIFIED`, a prova é **preservada para diagnóstico**, nunca
  re-hasheada para promoção e nunca vira dinheiro.
* `NULL` legado continua `LEGACY_UNVERIFIED`; nenhuma linha financeira real foi
  corrigida, reclassificada ou migrada. **Sem DDL novo** — o contrato V2 cabe no
  JSONB existente (`fee_conversions`, mais `generation`/`last_observation_id`
  aditivos).
* Mudança de código de recusa (mesma garantia): prova que descreve **outro**
  contexto é `FEE_CONVERSION_INVALID` (antes `..._CONFLICT`) — conflito ficou
  reservado a adulteração/divergência material. Ambígua do ledger virou
  `LEDGER_MULTIPLE_EVENTS_FOR_FILL`.

## Limitações reais e dependências externas

1. **Fonte**: o endpoint `/fapi/v1/income` (COMMISSION) é a única origem aceita
   para `CONFIRMED`. A ausência do lançamento é `BLOCKED_SOURCE_UNAVAILABLE` —
   espera de fonte, não resultado: o `net_trade` daquele trade continua
   desconhecido e a linha fica fora do total. Nenhum `USD=USDT=USDC`, cotação
   atual ou registro sintético.
2. **Vínculo do lançamento**: a Binance não garante, no `income`, o par
   `commissionAsset`/`commission` da linha. Sem esse vínculo inequívoco o fill
   permanece bloqueado — decisão conservadora, não defeito corrigido aqui.
3. **Observação operacional**: nenhuma entrada real foi executada neste pacote;
   nenhuma chamada à conta real, credencial, ordem, flag ou pausa foi tocada.
4. Promoção Dev→PRD e cutover seguem fora deste escopo.
