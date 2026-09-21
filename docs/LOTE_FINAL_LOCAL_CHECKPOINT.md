# Lote final local — checkpoint de execução

Baseline: `e3878238` (branch `main`, checkout principal). Sem push/deploy.
Estados: `NOT_STARTED`, `IN_PROGRESS`, `IMPLEMENTED`, `LOCAL_VERIFIED`,
`BLOCKED_EXTERNAL_EVIDENCE`.

Na retomada: ler este arquivo e `git log --oneline e3878238..HEAD`, continuar do
primeiro bloco que não estiver `LOCAL_VERIFIED`. Não reiniciar blocos prontos.

| Bloco | Status | Contrato | Arquivos | Testes | Commit | Próximo passo |
| --- | --- | --- | --- | --- | --- | --- |
| A · P03 entrada idempotente | `LOCAL_VERIFIED` | `SAFETY_FIX` | `models/entry_intent.py`, `services/entry_intent_service.py`, `services/shadow_trade_service.py`, `db.py`, `tests/test_lote_p03_entry_intent.py`, `tests/pg_integration_p03_intent.py`, `tests/test_p05_2l_execution_latency.py` (janela) | 20 herméticos + integração PostgreSQL real (14 cenários) + P02/P03/P04A/P05.2L | `73099fd7` | — |
| B · R05 contrato financeiro | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (fonte nova inativa) | `services/financial_total_service.py`, `services/shadow_trade_service.py` (gate R05D), `tests/test_lote_r05d_financial_total.py` | 24 herméticos + R05A/R05B/R05C (187, 2 skips históricos) | `aad86d41` | — |
| C · R11 política robusta | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (inativa) | `services/robust_policy_service.py`, `tests/test_lote_r11c_robust_policy.py`, `docs/R11A_LEARNING_ROTATION_AUDIT.md` | 32 herméticos + R11A/R11B2 preservados | `1e8eec4e` (+ `02c578b4`, escopo R11B2) | Persistência da progressão e ligação com rotação ficam para o bloco G (simulação) |
| D · R07+R08 estratégias | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (inativo) | `services/strategy_core_service.py`, `services/score_v3_service.py`, `tests/test_lote_d_strategy_core.py`, `tests/test_lote_d_score_v3.py` | 53 + 35 herméticos; paridade do laboratório V2 do R08A | `154411d9` | Ligar núcleo ao replay é o bloco F; evidência pré-seleção é o bloco E |
| E · R09 evidência | `LOCAL_VERIFIED` | `OBSERVATION_ONLY` (coleta desligada) | `services/preselection_observation_service.py`, `tests/test_lote_e_preselection.py` | 36 herméticos + R09/R10A/R10B preservados (137) | `da607ea0` | Exposição do resumo em endpoint existente fica no bloco H |
| F · R10 replay/walk-forward | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (inativo) | `services/research_dataset_scopes.py`, `services/research_dataset_service.py` (escopo), `services/portfolio_replay_service.py`, `services/walk_forward_service.py`, `tests/test_lote_f_dataset_scopes.py`, `tests/test_lote_f_portfolio_walk_forward.py`, `tests/test_r10a_offline_replay.py` (lista de importadores) | 17 + 47 herméticos; R09/R10A/R10B preservados (325 no conjunto) | `ee0f5ebd` + `113fe67c` | Carteira consome oportunidades já formadas; ligar o núcleo D ao replay ponta a ponta é trabalho do bloco G/H |
| G · R12 simulação/go-no-go | `LOCAL_VERIFIED` | `CANDIDATE_POLICY` (inativo) | `services/preselection_experiment_service.py`, `tests/test_lote_g_preselection_experiment.py` | 36 herméticos; P05/P05.1 preservados | `<G>` | Coleta prospectiva e canário dependem de autorização humana — fora deste lote |
| H · Integração/documentação | `NOT_STARTED` | — | — | — | — | Resumo em endpoint existente, docs e suíte completa |

## Bloco G — detalhe (concluído)

Tipo de experimento PRÉ-SELEÇÃO versionado gravado dentro de `candidate_config`
do `StrategyExperiment` que já existe: sem segundo catálogo, bus ou painel de
promoção, sem DDL. Config legada continua sendo pós-seleção, e os contratos NÃO
são intercambiáveis — o comparador de um tipo recusa o outro, porque as
populações são diferentes. `P051_ANALYTICS_ONLY` e os demais bloqueios antigos
seguem valendo; nada foi liberado como efeito colateral.

Exclusividade: um challenger prospectivo no ciclo oficial (o índice único de
`SHADOW` continua sendo a garantia final), demais candidatos sequenciais no
laboratório; repetir a chamada é idempotente e o ciclo de vida não tem salto nem
reabertura. Baseline, candidato, config, custos e proteções são congelados com
hash. Drift de champion, mudança de config, incidente P03 aberto ou cobertura
insuficiente impedem avançar — e o incidente NUNCA é limpo para o teste passar.
A/A com a mesma configuração dos dois lados tem de dar diferença nula.

Gate go/no-go congelado com hash: 100 trades shadow no total, 30 por playbook
habilitado, 14 dias corridos, 10 dias úteis, 90% de cobertura — e, além da
amostra, EV líquido, incerteza, drawdown, estabilidade, falhas operacionais,
zero duplicata econômica, nenhuma falha de proteção em aberto, nenhuma lacuna
essencial e discrepância de fidelidade dentro do teto. Ausência não vira zero:
evidência vazia é NO_GO. Passar no gate mantém `live_approval=UNAVAILABLE`.

Canário: manifest com versão, diff, evidência, critérios, pré-condições,
responsáveis e rollback — `applied=False`, sem endpoint de aplicação. Teto do
projeto não autoriza subir o vigente: vale o menor dos dois, e proposta acima do
vigente é recusada. Ensaio local de rollback preserva posições, proteções,
intenções, ledgers, histórico e incidentes, recusando DDL destrutivo, restauração
de dado inválido e regressão de segurança. Relatório separa `implementation_status`,
`evidence_status` e `live_approval`.

Pendências do bloco G: a coleta prospectiva e o canário em si dependem de
autorização humana e de ambiente real — fora do escopo deste lote.

## Bloco F — detalhe (concluído)

Exportador (`ee0f5ebd`): escopos aceitas / vetadas / candidatos estruturais em
cima do R10B existente. O escopo legado gera SQL BYTE A BYTE igual e mantém o
mesmo `request_hash` quando nenhum escopo é informado. Aceitas não têm
trajetória coletada: o artefato delas sai sem outcome, com reason code por campo
ausente e declarando que NÃO é comparável no CLI R10A — nada de resultado
inventado. Tudo com fixtures sintéticas, sem banco, sem `.env`, sem rede.

Replay de carteira (`portfolio_replay_service`): não é um segundo backtest — a
trajetória continua no motor R10A, identificável. Em volta dele entram universo
ponto-no-tempo (o universo de hoje não substitui o de ontem), latência de
scan/envio, preço executável com gate revalidado nesse instante, maker sem fill,
parcial e fallback SOMENTE quando habilitados (default desligado), carteira
compartilhada com capital, reservas, exposição, slots e um por símbolo — trade
impossível não entra na soma —, custos separados por componente (ausência deixa
a economia indisponível, nunca zero), regra conservadora para stop e alvo na
mesma barra, gap e barra incompleta, e matriz de fidelidade por dimensão com
`live_equivalent` sempre falso.

Validação (`walk_forward_service`): janelas rolantes de verdade (mínimo de três
dobras que avançam e não se sobrepõem — metade recente é recusada), purga pelo
maior horizonte e embargo, disciplina de dobra (preprocessamento, seleção,
normalização e calibração só no treino; teste final não escolhe candidato),
pareamento que preserva as aceitas de um lado só no delta de política e no
turnover, guarda contra exclusão seletiva, IC por blocos determinístico com
correção de multiplicidade e métricas por janela/playbook/TF/lado/regime. Custo,
horizonte, cobertura ou intervalo insuficientes ⇒ evidência insuficiente e
NENHUM vencedor; vencer na validação não promove. Holdout real continua selado
(nem carregado, nem avaliado); a fronteira é testada com holdout SINTÉTICO, com
pré-registro anterior aos resultados e sem hash como prova de independência.

Pendências do bloco F: a carteira consome oportunidades já formadas — ligar o
núcleo D ponta a ponta no replay e expor o resumo ficam para G/H.

## Bloco E — detalhe (concluído)

Escopo `PRE_SELECTION` versionado (`r09.pre.v1`) ADITIVO: `POST_SELECTION` e as
linhas antigas mantêm significado, e a evidência nova mora nas tabelas, no flush
e no resolver que já existem — sem tabela, scheduler, worker, fila ou cliente de
exchange novos, e sem alterar retenção. A coleta é opcional e desligada por
padrão (`R09_PRESELECTION_MODE=inactive`), então nada do scan depende dela.

Funil observado na ordem REAL (candidato → playbook → candle → geometria/RR →
liquidez → MTF/regime → seleção → risco → execução): etapa que não rodou fica
`NOT_EVALUATED` e nunca `PASSED`; `UNKNOWN` não é rejeição; a primeira rejeição é
registrada como fato, com `causal_claim=NONE` e sem lista contrafactual. Aceitas
e vetadas usam a MESMA identidade (símbolo, TF, lado, vela de gatilho, playbook e
versão), com tentativa derivada da oportunidade. Horizonte vem do timeframe e do
maior horizonte entre os candidatos registrados — o baseline resolver não encerra
a janela — com truncamento reportado quando excede o buffer. Origem, mercado,
símbolo e resolução viajam com a janela: divergência vira `SOURCE_MISMATCH` e
ausência vira `UNLABELED`, nunca Binance por omissão. Falha e completude são
cobertura, com `pnl_assumption=None` em todos os casos.

Pendências do bloco E: nenhuma coleta é ligada aqui; a exposição somente-leitura
do resumo e a ligação com o endpoint existente ficam no bloco H.

## Bloco D — detalhe (concluído)

Núcleo PURO (`strategy_core_service`): estado ponto-no-tempo validado, config
explícita com hash e procedência por parâmetro, relógio injetado. Decisão sempre
com motivos, features ausentes, playbook/versão e trace. Vela aberta, barra
fora de ordem, buraco na série, estado velho e evidência HTF do futuro não
decidem. Três playbooks espelhados long/short: `TREND_PULLBACK` (oscilador é
filtro e não inverte o lado), `TREND_BREAKOUT` (fechamento além da referência +
volume; nome de padrão não é evidência; reteste vira obrigatório quando a config
o exige) e `RANGE_REVERSION` (range provado por bordas, toques, largura e ADX —
`NORMAL` não prova lateralidade). Stop sempre estrutural: R:R abaixo do piso
reprova o setup e o stop NÃO é alargado. Conflito HTF confirmado bloqueia,
inclusive a reversão de range; força HTF desconhecida é tratada como conflito.
Arbitragem determinística: lados opostos bloqueiam, mesmo lado resolve por
prioridade declarada. `opportunity_key` casa com a identidade do P03 — a mesma
vela de gatilho é uma única entrada econômica.

Score V3 (`score_v3_service`): allowlist com unidade, domínio, sentido, peso,
cap e chave de evidência única por feature; seis categorias com cap somando 100.
ADX é força e muda de sentido conforme o playbook, sem nunca definir lado;
funding é direcional em parcela única; o composto de confluência fica fora por
padrão e, quando ligado, SUBSTITUI estrutura e gatilho com normalização
contínua (sem os degraus da V2). Ausência sai da escala, valor inválido não vira
evidência, cobertura abaixo do piso indisponibiliza o score. Sem calibração V3
do mesmo fingerprint não há probabilidade, tier, aprovação econômica nem
elegibilidade LIVE — e bins/p_global da V2 são recusados explicitamente.

Pendências do bloco D: a integração com replay/carteira é do bloco F; o
adaptador de seleção existe, é testado e devolve sempre `executable=False`,
com `R07_STRATEGY_CORE_MODE`/`R08_SCORE_V3_MODE` em `inactive` por padrão.

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
