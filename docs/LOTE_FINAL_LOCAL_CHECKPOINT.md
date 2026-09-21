# Lote final local — checkpoint de execução

Baseline: `e3878238` (branch `main`, checkout principal). Sem push/deploy.
Estados: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED`, `LOCAL_VERIFIED`,
`BLOCKED_EXTERNAL_EVIDENCE`.

Na retomada: ler este arquivo e `git log --oneline e3878238..HEAD`, continuar do
primeiro bloco que não estiver `LOCAL_VERIFIED`. Não reiniciar blocos prontos.

| Bloco | Status | Contrato | Arquivos | Testes | Commit | Próximo passo |
| --- | --- | --- | --- | --- | --- | --- |
| A · P03 entrada idempotente | `LOCAL_VERIFIED` | `SAFETY_FIX` | `models/entry_intent.py`, `services/entry_intent_service.py`, `services/shadow_trade_service.py`, `db.py`, `tests/test_lote_p03_entry_intent.py`, `tests/pg_integration_p03_intent.py`, `tests/test_p05_2l_execution_latency.py` (janela) | 20 herméticos + integração PostgreSQL real (14 cenários) + P02/P03/P04A/P05.2L | `73099fd7` | — |
| B · R05 contrato financeiro | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (fonte nova inativa) | `services/financial_total_service.py`, `services/shadow_trade_service.py` (gate R05D), `tests/test_lote_r05d_financial_total.py` | 24 herméticos + R05A/R05B/R05C (187, 2 skips históricos) | `<B>` | — |
| C · R11 política robusta | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (inativa) | `services/robust_policy_service.py`, `tests/test_lote_r11c_robust_policy.py`, `docs/R11A_LEARNING_ROTATION_AUDIT.md` | 32 herméticos + R11A/R11B2 preservados | `<C>` | Persistência da progressão e ligação com rotação ficam para o bloco G (simulação) |
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

## Bloco B — detalhe (concluído)

`financial_total_service` agrega o ledger R05C já persistido em um total
versionado COM funding (`R05D_TOTAL_WITH_FUNDING_V1`). `pnl_usd` continua
líquido de execuções EX-funding; nada é reescrito e nenhum fetch novo existe.
`COMPLETE` exige, para TODAS as linhas da janela: estado `CONFIRMED`, funding
`CONFIRMED`, taxas completas sem ativo não convertido, mesma conta e mesmo
ativo de liquidação, sem conflito de ledger, e coleta com paginação/overlap
provados. Qualquer falha mantém `PENDING`/`UNKNOWN` com subtotal separado.

Seleção pelo seletor NOVO `R05_FINANCIAL_TOTAL_SOURCE` (default `legacy`): a
flag do cutover R05B segue com o significado dela e não ativa o R05D. Com o
default, o gate de entrada não consulta nada e o comportamento legado é
idêntico (testado). Com a fonte nova, insuficiência essencial impede apenas
AUMENTAR exposição — nunca proteção, redução ou fechamento.

Pendências do bloco B: o total real depende de funding confirmado pelo
coletor R05C em produção (evidência externa); a conversão de comissão em outro
ativo continua indisponível por falta de prova de taxa/par/fonte/instante.

## Bloco C — detalhe (concluído)

Núcleo PURO `R11C_ROBUST_V1`, sem I/O e com instante injetado: histerese que
exige período elegível E evidência nova (chamada repetida, preview, restart e
concorrência não aceleram; evidência contrária reinicia; troca da fonte do
universo reinicia), decay com baseline e janela recente DISJUNTOS (o prejuízo
recente não derruba a própria referência: −1,0R recente agora corta até o piso),
cache vazio válido × erro, referência temporal causal única com cobertura
declarada, elegibilidade por geração de aprendizado (sem aplicar o melhor TF em
outro timeframe), liquidez indisponível que não promove, identidade por
oportunidade com dedupe e populações separadas, e mérito com EV líquido
descontado no tempo, estabilidade por janelas, incerteza e correção por número
de candidatos.

Pendência do bloco C: a progressão da histerese ainda não tem persistência
transacional própria (o núcleo é puro e recebe/devolve o estado); isso entra na
simulação do bloco G, junto do plano de rotação simulado. Nenhum serviço LIVE
importa o módulo, e o seletor `R11_POLICY_VERSION` nasce em `legacy`.
