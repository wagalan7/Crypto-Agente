# Lote 02 — checkpoint de execução

Worktree autorizado pelo usuário em 04/10/2026 para este e os próximos lotes:
`.claude/worktrees/lote02-pesquisa` (branch `worktree-lote02-pesquisa`), base
`d0bf52e7eb95333b31f2647e761983681f34957b` (`main`, Lote 01 integrado;
`7d6dbe15` confirmado como ancestral). A sessão é isolada em worktree e o hook
recusa git/`change_directory` no checkout compartilhado — isso foi informado e a
exceção veio do usuário, sem contornar o hook. Integração em `main` é etapa
separada, após revisão. Sem push/deploy/ativação.

## Estado atual

ETAPA: **LOCAL_VERIFIED** (provas locais executadas) + **WAITING_DECISION**
(candidata/baseline/escopo/custos) + **WAITING_DATA** (amostra prospectiva).
Lote 02 concluído nesta execução; Lotes 03/04 NÃO iniciados.

| # | Etapa do prompt | Estado |
|---|---|---|
| §1 | Mapa de contratos/callers por fronteira | feito |
| §2 | Manifesto autorizado + `authorized_comparison` | implementado, 16 provas |
| §3 | Coleta oficial pré-seleção no ponto certo | implementado, 8 provas pelo entrypoint real |
| §4 | Comparação integral por escopo (`SELECTION_ONLY`) | implementado, 19 provas + PG |
| §5 | Calibração V3 (ajuste/artefato/previsão/OOS) | implementado, 21 provas |
| §6 | Status derivado e honesto | implementado, 9 provas |
| §7 | PG descartável, concorrência 2×, suíte, docs | feito |

## Arquivos do lote

Novos: `backend/services/research_manifest_service.py`,
`backend/services/research_selection_service.py`,
`backend/services/score_v3_calibration_service.py`, cinco suítes
`backend/tests/test_lote02_*.py` e `backend/tests/pg_integration_lote02_pesquisa.py`.

Alterados: `recommendation_service` (captura/gate/cobertura),
`preselection_observation_service` (payload v2 + cobertura de ciclo),
`decision_observation_service` (repasse dos blocos v2),
`research_dataset_service` (pedido `SELECTION_ONLY`, export das features),
`preselection_experiment_service` (contrato V2 + envelope por escopo),
`strategy_evidence_service` (verificação por escopo),
`offline_replay_service` (tipo de candidato `SELECTION_ONLY`),
`score_v3_service` (veredito com artefato real, `net_ev` por payoff),
`research_batch_service` (status derivado), `scripts/research_pipeline.py`
(`--manifest` e despacho por escopo).

Fixtures adaptadas (garantias preservadas): `test_lote_d_score_v3`,
`test_lote_g_preselection_experiment`, `test_lote_h_integracao`,
`pg_integration_r09_preselection`, `pg_integration_r11_r12_pipeline`.

## Provas executadas (estado final)

- Direcionadas: 16 + 8 + 19 + 21 + 9 = **73 testes novos**, todos OK.
- `pg_integration_lote02_pesquisa.py` → **18 verificações, 2×** (PG16
  descartável, socket Unix, TCP/DNS bloqueados, duas conexões reais com CAS).
- Regressões PG: `pg_integration_r11_r12_pipeline.py` (138),
  `pg_integration_r09_preselection.py` (29), `pg_integration_r10b.py` (OK).
- Suíte completa: **2.639 testes, OK, 2 skips R05C declarados** (fixture privada
  ausente, não fabricada).
- `py_compile` dos arquivos próprios e `git diff --check` aprovados; nenhum
  arquivo de frontend alterado (TSC não se aplica).

## Bloqueios reais (não mascarados)

1. **WAITING_DECISION** — baseline/candidata/escopo/custos do estudo real. Sem
   isso, `authorized_comparison` devolve `BLOCKED_MISSING_DECISION` e os
   manifestos de teste ficam `APPROVED_TEST_ONLY` (`real_study_allowed=false`).
2. **WAITING_DATA** — coleta desligada: não existe amostra prospectiva, logo não
   há calibração 200/30 nem evidência econômica.
3. **Lacuna de features** — o scan champion não calcula as features de
   estrutura/gatilho do Score V3; estudo `SELECTION_ONLY` sobre a captura do
   champion fica `WAITING_DATA` em vez de decidir sem base.
4. **Limites de aceitação OOS** — `CALIBRATION_OOS_THRESHOLDS_DECISION_REQUIRED`.
5. Quarentena por posição não reconhecida (apontada na última verificação da
   publicação) **não** foi tocada neste lote, conforme instrução.

## Próximo comando para retomar/reverificar

```bash
cd "/Users/alanmalta/Agente de IA Crypto/.claude/worktrees/lote02-pesquisa/backend" && ../../../../backend/.venv311/bin/python -B -m unittest tests.test_lote02_research_manifest tests.test_lote02_preselection_capture tests.test_lote02_selection_scope tests.test_lote02_v3_calibration tests.test_lote02_status_e_sentinelas
```

O harness PG precisa de um cluster descartável (socket `/tmp/cw-lote02-sock.*`)
e da variável `LOTE02_TEST_SOCKET`.
