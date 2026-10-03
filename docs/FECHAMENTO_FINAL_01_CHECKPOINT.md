# Lote 01 — checkpoint de execução

Worktree autorizado: `.claude/worktrees/blissful-sinoussi-0511a5` (branch
`claude/blissful-sinoussi-0511a5`), base
`7d6dbe156700f1f3bedd6e8f2a9e60acf4349eaa` (confirmada como ancestral do HEAD).
Integração em main é etapa separada, após revisão. Sem push/deploy/cutover.

## Estado atual

ETAPA: **LOCAL_VERIFIED** (provas locais executadas) +
**WAITING_SOURCE** / **WAITING_OPERATIONAL_OBSERVATION** declarados.
Lote 01 CONCLUÍDO nesta execução; Lotes 02/03/04 NÃO iniciados.

| # | Etapa | Estado |
|---|---|---|
| 1 | Mapa produtor→persistência→consumidor (§1) | feito (doc de entrega) |
| 2 | Conversão de comissão em outro ativo (§2) | implementado, 27 + 13 provas |
| 3 | Funding e total (§3) | auditado e provado em PG (sem reescrita) |
| 4 | Admissão/cutover/manual-BOT (§4) | auditado; cutover preparado, não ativo |
| 5 | Testes PG16 + direcionados 2× + suíte completa (§5) | feito |
| 6 | Docs de entrega + HARDENING_LOG + commit (§6) | feito |
| 7 | Correção conjunta da auditoria `e995b63b` (R1–R5) | feito, 29 + 27 + 21 provas |

### Correção conjunta sobre `e995b63b`

Cinco defeitos (ledger sem normalização, prova íntegra com contexto divergente,
carimbo de consulta virando divergência econômica, zero estrangeiro exigindo
conversão e 49 conversões terminando em `FAILED`) corrigidos em um pacote, com
RED medido na baseline pela API dela mesma. Contrato da evidência:
`R05E_FEE_CONVERSION_V2` (V1 = legado, preservado, nunca promovido). Sem DDL.
Detalhe: `LOTE01_CORRECAO_CONJUNTA_e995b63b.md`.

## Arquivos do lote

- `backend/services/execution_accounting_service.py` — contrato
  `R05E_FEE_CONVERSION_V2` (normalizador do ledger, contexto esperado, hash de
  integridade × identidade material, precedência, exigência por fill), merge
  idempotente no JSON existente (inclusive no merge com bloqueio de linha de
  `merge_observation`, agora com geração de observação), coletor da conversão
  registrada pela corretora e integração no ciclo contábil.
  **Único serviço alterado.**
- `backend/tests/test_lote01_fee_conversion.py` — 27 testes herméticos
  (adaptados ao contrato V2, garantias preservadas).
- `backend/tests/test_lote01_correcao_conjunta.py` — novo (29 testes: R1–R5 +
  matriz final de fechamento).
- `backend/tests/pg_integration_lote01_financeiro.py` — 21 verificações PG
  (13 do lote + 8 da correção conjunta).
- `docs/FECHAMENTO_FINAL_01_OPERACIONAL_FINANCEIRO.md`, `docs/HARDENING_LOG.md`,
  este checkpoint.

## Provas executadas (estado final)

- RED da correção conjunta medido na baseline `e995b63b`, pela API da própria
  baseline: **5/5 defeitos reproduzidos** antes de corrigir.
- `tests.test_lote01_correcao_conjunta` + `tests.test_lote01_fee_conversion` →
  **56/56 OK, 2×** no estado final.
- `pg_integration_lote01_financeiro.py` → **21/21 OK, 2×** (PG16 descartável,
  socket Unix, TCP/DNS bloqueados; duas conexões reais com bloqueio de linha).
- Reexecutados: `pg_integration_r05c.py` → `R05C_PG_INTEGRATION_OK`;
  `pg_integration_r05d_gate.py` → 16; `pg_integration_r05_clock.py` → 10
  (espera real pela lock em `pg_locks`).
- Suíte completa: **2.556 executados, 2.554 aprovados, 2 skips R05C** declarados.
- `py_compile` dos arquivos próprios e `git diff --check` aprovados.

## Bloqueios reais (não mascarados)

1. `BLOCKED_SOURCE_UNAVAILABLE` por fill quando a corretora não registrou a
   conversão da comissão na moeda de liquidação: interface funcional, número não
   inventado, `net_trade` segue desconhecido para aquele trade.
2. Observação operacional de uma entrada real ainda não aconteceu — nenhum trade
   foi forçado e nenhuma chamada real à Binance foi feita.
3. Vínculo do lançamento: o `income` da Binance não garante o par
   `commissionAsset`/`commission` da linha. Sem esse vínculo inequívoco o fill
   continua bloqueado (`LEDGER_FOREIGN_LINK_NOT_UNAMBIGUOUS`) — decisão
   conservadora declarada, não defeito pendente.

## Próximo comando para retomar/reverificar

```
cd "/Users/alanmalta/Agente de IA Crypto/.claude/worktrees/blissful-sinoussi-0511a5/backend" && ../../../../backend/.venv311/bin/python -B -m unittest tests.test_lote01_correcao_conjunta tests.test_lote01_fee_conversion
```

O harness PG do lote precisa de um cluster descartável (socket
`/tmp/cw-lote01-sock.*`) e da variável `LOTE01_TEST_SOCKET`.
