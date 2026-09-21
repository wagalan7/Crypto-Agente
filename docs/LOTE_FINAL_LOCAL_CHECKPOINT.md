# Lote final local — checkpoint de execução

Baseline: `e3878238` (branch `main`, checkout principal). Sem push/deploy.
Estados: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED`, `LOCAL_VERIFIED`,
`BLOCKED_EXTERNAL_EVIDENCE`.

Na retomada: ler este arquivo e `git log --oneline e3878238..HEAD`, continuar do
primeiro bloco que não estiver `LOCAL_VERIFIED`. Não reiniciar blocos prontos.

| Bloco | Status | Contrato | Arquivos | Testes | Commit | Próximo passo |
| --- | --- | --- | --- | --- | --- | --- |
| A · P03 entrada idempotente | `LOCAL_VERIFIED` | `SAFETY_FIX` | `models/entry_intent.py`, `services/entry_intent_service.py`, `services/shadow_trade_service.py`, `db.py`, `tests/test_lote_p03_entry_intent.py`, `tests/pg_integration_p03_intent.py`, `tests/test_p05_2l_execution_latency.py` (janela) | 20 herméticos + integração PostgreSQL real (14 cenários) + P02/P03/P04A/P05.2L | `<A>` | — |
| B · R05 contrato financeiro | `NOT_STARTED` | `CANDIDATE_POLICY` + `OBSERVATION_ONLY` | — | — | — | Ler `R05B`/`R05C` e serviços contábeis; criar contrato versionado inativo por padrão |
| C · R11 política robusta | `NOT_STARTED` | `CANDIDATE_POLICY` | — | — | — | Política versionada com histerese persistente, decay disjunto, geração atômica |
| D · R07+R08 estratégias | `NOT_STARTED` | `CANDIDATE_POLICY` | — | — | — | Núcleo puro + 3 playbooks + Score V3 de pesquisa |
| E · R09 evidência | `NOT_STARTED` | `OBSERVATION_ONLY` | — | — | — | Escopo pré-seleção versionado, aceitas + vetadas |
| F · R10 replay/walk-forward | `NOT_STARTED` | `CANDIDATE_POLICY` | — | — | — | Replay usando o núcleo D, carteira compartilhada e validação por janelas |
| G · R12 simulação/go-no-go | `NOT_STARTED` | `CANDIDATE_POLICY` | — | — | — | Tipo de experimento pré-seleção sobre `StrategyExperiment` |
| H · Integração/documentação | `NOT_STARTED` | — | — | — | — | Resumo em endpoint existente, docs e suíte completa |

## Bloco A — detalhe (concluído)

Intenção de entrada econômica persistente (`entry_intents`), reservada e
commitada ANTES da primeira mutação de ordem, com `client_order_id` derivado da
intenção. Estados `RESERVED → SENDING → CONFIRMED | UNKNOWN | TERMINAL`,
compare-and-set e unicidade no PostgreSQL, lease por dono e admissão de
capacidade sob a advisory lock oficial de risco (`917283`).

Provado em PostgreSQL descartável: mesma decisão em snapshots distintos gera um
único envio; duas conexões concorrentes idem; payload divergente é conflito;
crash antes do envio permite retomada (um envio); crash depois do envio vira
`UNKNOWN` e nunca reenvia; lease vencido em `SENDING` idem; callback tardio
vincula o RealTrade de forma idempotente; `no_fill` é terminal sem travar o
símbolo (novo gatilho é aceito); duas decisões diferentes disputando o último
risco/slot — só uma entra; nenhuma intenção apagada; zero chamada de exchange.

Pendências do bloco A: rotas manuais do app ainda não passam pela intenção
(apenas o caminho automático do executor); ordens abertas fora do app seguem
externas por contrato.
