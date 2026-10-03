# Lote 01 — fechamento operacional e financeiro

Base publicada: `7d6dbe156700f1f3bedd6e8f2a9e60acf4349eaa`. Implementado no
worktree autorizado `blissful-sinoussi-0511a5` (branch
`claude/blissful-sinoussi-0511a5`); integração em main é etapa separada, após
revisão. Nenhum push, deploy, cutover, ordem, reconhecimento manual ou liberação
de pausa nesta entrega. `R05_FINANCIAL_TOTAL_SOURCE` e
`R05_FINANCIAL_BREAKER_ENABLED` ficaram INALTERADOS.

Estado: **LOCAL_VERIFIED** para o que foi executado localmente;
**WAITING_SOURCE** para a conversão de comissão sem registro da corretora e
**WAITING_OPERATIONAL_OBSERVATION** para a observação de uma entrada real. Isso
NÃO significa financeiro operacionalmente consolidado.

## 1. Mapa produtor → persistência → consumidor → prova

| Grandeza | Produtor | Persistência | Consumidor | Prova de completude |
|---|---|---|---|---|
| P&L líquido de execuções SEM funding (`net_trade`) | `execution_accounting_service.collect_trade_accounting` (userTrades + GET de ordens, paginado e com orçamento) → `compute_totals` | `real_trades.execution_accounting.totals` (JSONB, merge com bloqueio de linha em `apply_accounting`) | `project_to_trade_fields` → `RealTrade.pnl_usd`; `financial_risk_service.financial_window` | `gross_complete` + `fees_complete` + `state=CONFIRMED`; comissão em outro ativo exige conversão CONFIRMED por fill |
| Funding atribuído | `_collect_funding` (`/fapi/v1/income` `FUNDING_FEE`, paginação completa) → `normalize_funding` (sinal, `tranId`, ativo, símbolo) | `execution_accounting.funding` + `funding_proof` | `compute_totals.funding_net` | `funding_proof.complete` + exposição EXCLUSIVA + janela dos fills atribuídos; janela consultada e vazia prova zero |
| Total com funding | `compute_totals.net_including_funding` | mesma linha JSONB | `financial_total_service.aggregate` / `fresh_total` / `total_in_session` | `row_verdict` exige schema, conta, liquidação, `state=CONFIRMED`, `funding_state=CONFIRMED`, zero conflito e `fee_assets_unconverted` vazio; `collection_proven` |
| Risco aberto BOT, reservas e margem real | `financial_risk_service._ADMISSION_SNAPSHOT_SQL` (UMA instrução, `statement_timestamp()`, depois da advisory lock) + `entry_intent_service.reserve/admit_final_risk` | `real_trades` (abertas/fechadas) + `entry_intents.reserved_risk_usd/reserved_margin_usd` + `account_margin_epochs` | `admission_snapshot` → `MarginGate`/`admit_final_risk` → despacho | fechadas, abertas e reservas do MESMO snapshot; geração de margem (token) conferida na readmissão e no despacho |

Fronteiras preservadas: `Snapshot.realized_r` é pesquisa, não dinheiro; manual
externo não vira RealTrade/managed e não entra em P&L/slots do BOT, mas consome
margem real; `managed` preserva a política dele e `auto` encerrado manualmente
continua `auto`.

## 2. Comissão paga em OUTRO ativo — ANTES e DEPOIS

**ANTES.** `compute_totals` detectava qualquer comissão fora da moeda de
liquidação e devolvia `net_trade=None` com `FEE_ASSET_CONVERSION_UNAVAILABLE`
(bloqueio CORRETO: BNB não é USDT). Não existia caminho verificável para
resolver — a linha ficava fora de `accounting_total` indefinidamente.

**DEPOIS.** Contrato `R05E_FEE_CONVERSION_V1`, versionado **por fill**:

- `build_fee_conversion` valida com Decimal e devolve recusa explícita para
  bool/NaN/infinito/negativo, preço ≤ 0, ativo igual ao de liquidação, carimbo
  no futuro, janela incoerente e fonte desconhecida (`BLOCKED_SOURCE_UNAVAILABLE`).
  A qualidade é DERIVADA da fonte: `BROKER_REGISTERED_CONVERSION` ⇒ `CONFIRMED`;
  `HISTORICAL_MARKET_PRICE` ⇒ no máximo `ESTIMATED`. Preço atual não é histórico
  e não se assume USD=USDT=USDC nem 1:1.
- `fee_conversion_hash` cobre os campos canônicos (conta/exchange, `fill_key`,
  ativo/quantidade, liquidação, valor/preço, instante do fill, fonte, qualidade e
  janela observada) e é estável no ida-e-volta do JSONB.
- `fee_conversion_verdict` reconfere hash, vínculo com o fill, ativo e
  quantidade; `CONFIRMED` forjado sobre preço de mercado é recusado.
- `resolve_fee_conversions` só resolve com **todas** as comissões exigidas
  CONFIRMED; estimativa não vira dinheiro e ausência nunca vira zero.
- Persistência: `merge_accounting(..., fee_conversions=...)` grava por `fill_key`
  no JSON existente, é idempotente, e divergência vira `CONFLICT` **preservando a
  original**. `merge_observation` (merge com bloqueio de linha em
  `apply_accounting`) carrega a evidência — ela é prova, não apresentação.
- Coleta: `collect_broker_fee_conversions` faz UMA varredura paginada de
  `COMMISSION` no ledger de income para todos os fills (sem N+1, cliente de
  leitura existente, sem SDK novo), com orçamento de chamadas, e só aceita uma
  linha na moeda de liquidação amarrada ao MESMO `tradeId`. Integrada em
  `collect_trade_accounting` em lote limitado (`MAX_FEE_CONVERSIONS_PER_TRADE=8`),
  fora de qualquer transação. Nenhuma chamada real foi feita nesta implementação.
- `net_trade` = gross − taxa na liquidação − soma das conversões CONFIRMED.
  Com tudo confirmado, `fee_assets_unconverted` esvazia e a linha passa a entrar
  em `accounting_total`.

**Fontes.** CONFIRMADA: conversão registrada pela corretora no ledger de
`COMMISSION` (mesma `tradeId`, moeda de liquidação). ESTIMADA: preço histórico de
mercado — aceita como evidência, nunca como dinheiro. INDISPONÍVEL: sem registro
⇒ `BLOCKED_SOURCE_UNAVAILABLE` por fill e `net_trade` segue desconhecido.

## 3. Funding e total — auditado, sem reescrita

Já estavam corretos e foram PROVADOS, não reescritos: sinal preservado, dedupe
por `FUNDING_FEE:tranId`, paginação completa com motivos explícitos
(`INCOME_PAGE_SAME_TIMESTAMP`, `CALL_BUDGET_EXHAUSTED`, `INVALID_INCOME_PAGE`),
janela de exposição derivada dos fills atribuídos, exigência de exposição
EXCLUSIVA, descarte de lançamentos fora da janela e legado `NULL` em
`LEGACY_UNVERIFIED` sem backfill. Total = `net_trade` + funding confirmado UMA
vez; TP1/TP2/runner e taxas não entram de novo.

## 4. Admissão, cutover e manual/BOT — auditado

`_ADMISSION_SNAPSHOT_SQL` já usa UMA instrução com `statement_timestamp()` depois
da advisory lock, trazendo fechadas, abertas e reservas do mesmo instante;
`_apply_source_to_window` troca o P&L da janela pelo total COM funding quando a
fonte completa está selecionada, **sem fallback silencioso** (insuficiência vira
UNKNOWN). Nada foi alterado nessas rotas.

**Cutover/rollback preparados, NÃO ativados.** Para ligar o total com funding
basta `R05_FINANCIAL_TOTAL_SOURCE=accounting_total` (rollback: voltar a `legacy`);
o breaker continua em `R05_FINANCIAL_BREAKER_ENABLED`. Ambos permanecem como
estão — a ativação é decisão/etapa separada, com evidência própria.

## 5. Migrações

**Nenhuma.** A evidência entra como chave nova dentro do JSONB
`real_trades.execution_accounting`, que já existe; `init_db` não ganhou coluna,
tabela ou índice.

## 6. Observação operacional pendente (runbook posterior)

Observar UMA entrada legítima, **se** ocorrer, conferindo fill/qty/proteção,
ledger (`state`, `net_trade`, `funding_state`, `fee_conversions`) e reservas.
Não forçar trade. `GET /api/recommendations` e `/api/live/preflight` não são
leitura inofensiva: auditar efeitos antes de incluir rotas em qualquer verificação.

## 7. Correção conjunta sobre `e995b63b` (contrato V2 da conversão)

A auditoria `AUDITORIA_LOTE01_e995b63b.md` apontou cinco defeitos neste mesmo
lote. Todos foram corrigidos em um pacote único, ainda dentro de
`execution_accounting_service.py` e **sem DDL**: normalizador por linha do ledger
de COMMISSION (R1), contexto esperado derivado do fill atribuído em todas as
fronteiras (R2), separação entre hash de integridade e identidade material com
tabela de precedência (R3), zero estrangeiro comprovado como custo zero (R4) e
progresso útil em lote com geração de observação sob bloqueio (R5).

O contrato da evidência passou a `R05E_FEE_CONVERSION_V2`; o V1 fica **legado**
(`FEE_CONVERSION_UNVERIFIED`), preservado para diagnóstico e nunca promovido.
Detalhe por defeito, provas e limitações: `LOTE01_CORRECAO_CONJUNTA_e995b63b.md`.

`R05_FINANCIAL_TOTAL_SOURCE` e `R05_FINANCIAL_BREAKER_ENABLED` continuam
intocados — a correção não liga nem desliga nada.
