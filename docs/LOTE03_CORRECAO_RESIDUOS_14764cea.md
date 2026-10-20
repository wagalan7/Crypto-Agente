# Lote 03 — correção dos quatro resíduos verificados

Data: 06/10/2026. Base exata: `14764ceac30c2b7f5cc0a437932cba11634b8b73`.
Ambiente: worktree autorizado `lote02-pesquisa`, branch `worktree-lote02-pesquisa`.
Estado: **CORRIGIDO / LOCAL_VERIFIED**, infraestrutura inativa.

## Escopo e prova do defeito

1. **Repetição do start ignorava a aprovação.** Na base, um experimento já em
   SHADOW retornava sucesso idempotente sem consultar a autoridade — inclusive
   com aprovação inexistente/revogada e geração divergente. Corrigido em
   `start_preselection_shadow`: lock P05 → singleton → experimento; autoridade
   SHADOW atual/exata e identidade do primeiro start antes da idempotência.
   Uma aprovação nova não reassocia a coorte. Identidade ausente recusa, sem
   backfill. Repetição válida preserva início/identidade/atributos sem escrita;
   ramos negativos salvam primitivos antes do rollback, sem ler ORM expirado.
   Os 11 testes novos reproduziram 14 falhas na base, contando subcasos.

2. **Recusa legítima por score perdia o corte.** O motor produzia REJECTED com
   score conhecido, mas descartava `min_score`; o grupo e a anotação ficavam
   incomparáveis por `DENOMINATOR_NOT_RECONCILED`. Agora `candidate_decision` e
   `shadow_group_decision` transportam o corte validado em REJECTED. A cadeia
   real V3 → congelamento → anotação → fidelidade admite o par legítimo; corte
   ausente/divergente e UNKNOWN continuam fora do denominador. Não há nova
   probabilidade, fallback ou decisão de entrada.

3. **Saída do runner ficava pendente.** `CLOSED_RUNNER_STOP` era um estado
   terminal do replay oficial, mas não da coorte prospectiva. `TERMINAL` agora
   deriva de `offline_replay_service.CLOSED_STATUSES`, acrescido de NOT_FILLED.
   Um replay real sintético que realiza TP1 e fecha o runner entra na amostra
   terminal, mantém R/estado oficiais e não é reaberto na repetição do resolver.

4. **O contador zero escondia uma trilha inválida.** Uma trilha com stop
   ausente, flags contraditórias e falha não resolvida ainda passava pela
   conferência de rótulos/hash e certificava zero. O consumidor agora concilia
   identidade da resolução, lado/entrada/stop inicial, timestamps/estágios,
   quantidade/exposição/obrigação, finitude/geometria conforme TP1, resumo final
   e falhas declaradas. Contradição retorna `PROTECTION_TRACE_INVALID`, perde
   cobertura e deixa a métrica desconhecida — nunca certifica zero.
   Não há recálculo de economia ou consulta de preços nesse validador.

## Verificação final

- **222 testes direcionados**, duas execuções verdes, cobrindo Lote 03 completo
  e regressão do replay oficial.
- **Suíte completa: 2.867 executados, 2.865 aprovados, 2 skips** históricos R05C
  (fixture auditada privada indisponível; não fabricada). Uma execução final.
- **59 verificações PostgreSQL 16, duas execuções no estado final**, via
  `bash backend/tests/run_pg_lote03.sh`. Cluster UTF-8 descartável, driver real,
  socket Unix; zero TCP. DNS público do harness é bloqueado e contabilizado
  (32 tentativas por execução, nenhuma consulta externa concluída).
- O harness mantém os 53 aceites anteriores e acrescenta repetição válida,
  três recusas do fast-path, identidade/início preservados e trilha
  contraditória persistida/relida após descarte do pool sem certificar zero.
- Revisão independente somente leitura: 4.000 trilhas sintéticas produzidas
  pelo replay oficial, LONG/SHORT, STOP/RUNNER_STOP/TP2/TIME_STOP/MAX_HOLD e
  janela incompleta; nenhuma rejeição indevida ou mutação pelo validador.
  Oito contradições adicionais foram recusadas. Isso é prova local sintética,
  não validação econômica nem prova de SL real.
- `py_compile`, sintaxe do runner e `git diff --check` aprovados. Nenhum TS/TSX
  alterado; TSC não se aplica. Não foram repetidos todos os 24 harnesses do
  relatório anterior — não os declaro reexecutados nesta correção.

O teste antigo de obrigação aberta foi ajustado: agora usa uma janela realmente
incompleta do resolver oficial, em vez de adulterar apenas o resumo de uma
posição já encerrada. A garantia permanece: obrigação aberta genuína conta
como pendente; resumo incoerente é recusado por um teste novo. Na elaboração
do teste de runner, uma vela sintética inicial tinha OHLC incoerente; corrigida
a fixture, o defeito terminal foi reproduzido antes da correção de produção.

## Invariantes e limites

- Nenhum score/corte de decisão, tier, risco, limite, sizing, stop ou TP do
  champion foi alterado. LEGACY/OFF e os propósitos de aprovação preservados.
- `PROTECTION_SCOPE_ACCEPTED_FOR_PROMOTION=False`: a trilha SIMULADA não vira
  SL real e não libera promoção. Nenhuma aprovação real foi criada.
- Sem DDL, flag/ENV, endpoint, worker/loop ou escrita histórica nova. Proteção
  foi corrigida no consumidor; o produtor e a economia do replay não mudaram.
- Testes usam dados/autoridades TEST_ONLY e bordas de exchange falsas.
  Nenhuma conta, ordem, banco externo, mensagem ou produção foi acessada.
- Sem merge, push ou deploy nesta correção. Os dois prompts untracked e os
  arquivos de outros aplicativos ficam fora do commit.

Continuam pendentes **decisões/dados**, não são aprovados por estes testes:
manifesto de pesquisa aprovado; decisão sobre o escopo de proteção da primeira
promoção; amostra prospectiva real; aprovação CANARY e seleção explícitas.
`OPERATIONAL_ACCEPTED` não foi alcançado. Integração em main e atualização do
índice existente apenas no checkout principal são etapas separadas.
