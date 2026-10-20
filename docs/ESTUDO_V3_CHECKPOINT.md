# Estudo V3 — checkpoint

Base: 2d6d87b1, worktree autorizado lote02-pesquisa. Critérios: ESTUDO_V3_CRITERIOS_APROVADOS.md.

## Autoridade

Usuário aprovou implementação analítica em 07/10/2026. Sem ativação LIVE,
sem aprovar manifesto incompleto, sem coleta/canário/cutover financeiro.

## Trabalho salvo em 08/10/2026

- Política versionada com aceites separados; produtor OOS e persistência oficial.
- Integração governada, preparação administrativa com CAS e snapshot V2.
- Provas de treino, identidade, calibração/economia, geração, expiração,
  resolução tardia de pendência e proteção de escopo.
- Quatro caracterizações antigas de governança foram adaptadas com controles
  positivos válidos; não se trocou toda falha por um bloqueio genérico.
- Fronteira de arquitetura preservada: aceite não importa o exportador.
- Harness PG governado final e regressão final concluídos.

Persistência verifica o recálculo completo antes do banco. GET e autoridade
consomem recibo verificado sem bootstrap/replay. Sem segredo efêmero ou tabela nova.

## Preservação

Cwd loving-maxwell e main não são usados para implementar. Dois prompts
untracked preexistentes de lote02-pesquisa permanecem fora do escopo.

## Pendências externas preservadas

Configuração efetiva, custos, fontes, datas e hashes do manifesto precisam
conferência e ratificação antes de estudo real. Nenhum resultado real foi avaliado.

## Testes e estado

PG do aceite: 18 verificações verdes 2× em socket Unix, zero TCP/DNS.
Primeira suíte completa: 2.906 executados, três falhas e dois skips históricos
R05C. Duas falhas eram guardas de arquitetura anteriores ao módulo novo;
a terceira era classificação de amostra pequena, corrigida mantendo NO_GO.
Esta primeira execução **não** é aprovação da versão final.

Versão de produção estabilizada: 63 focais verdes 2×; suíte completa final
2.911 executados, 2.909 aprovados e dois skips R05C. PG aceite18 verde e
regressão Lote02 26+19 verificações verdes. `py_compile` e diff-check limpos.

Integração PG governada final: **71 verificações, 2×, exit 0** após a última
edição. Dois clusters independentes; TCP zero; 36 tentativas DNS bloqueadas e
contadas por execução, todas `www.okx.com`. Cleanup confirmado. Exportação,
fitting, persistência, anotações, resolver, snapshot e autoridade oficiais;
mercado/ordens falsos. O caso 119/120 reconfirma no banco a resolução tardia.
Nenhum GO real nem aprovação humana foram fabricados.

Limite de QA: captura da fixture grande usa orçamento de 30s no harness
após timeout comprovado de 1,5s; orçamento de produção inalterado. O controle
de relógio desse harness expira recibo, artefato e CANARY conjuntamente; o
harness de aceite cobre expiração isolada do recibo. Não é prova de latência
de produção nem de proteção na exchange.

Estado: **LOCAL_VERIFIED**, implementação e verificação concluídas, entrega
em commit local deste worktree. Sem merge, push, deploy, aprovação operacional
ou candidata LIVE nesta etapa. Main permanece na baseline.

## Reprodução local

A partir deste worktree, com o Python 3.11 já instalado no projeto:

```sh
cd backend
"/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python" -B -m unittest tests.test_research_acceptance_policy tests.test_research_acceptance_study tests.test_research_acceptance_governance tests.test_lote03_governance
"/Users/alanmalta/Agente de IA Crypto/backend/.venv311/bin/python" -B -m unittest discover -s tests -p 'test_*.py'
bash tests/run_pg_research_acceptance.sh
bash tests/run_pg_lote02_correction.sh
bash tests/run_pg_lote03.sh
```

Os runners PG criam e encerram clusters locais descartáveis. Seus controles
de rede e fixtures sintéticas não são um procedimento de operação da conta.
