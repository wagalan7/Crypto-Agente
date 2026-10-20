# Réplica econômica por eventos — checkpoint

Base: `c12ea043`. Implementação local autorizada em 12/10/2026. LIVE,
conta, parâmetros, posição manual, holdout e histórico não serão alterados.

Retomado em 13/10 sem refazer os blocos. Execução isolada, ledger por evento,
estatística e integração/export/catalog/status implementados localmente.
Estado do pacote: **LOCAL_RESEARCH_PARTIAL**, não aprovação integral do lote.
Nenhum push/deploy nem estudo real autorizado.

Diagnóstico histórico na base `0e88f58f`: `_maybe_pyramid` pedia MARKET sem o preflight
exigido pelo transporte. O adapter deve reproduzir a recusa, não fornecer uma
autorização artificial nem mudar o LIVE. Portanto a réplica integral com
pyramiding ativo ainda não pode ser declarada concluída.

As fontes de execução são distintas (sintética, tabela modelada ou fatos
observados). Hashes conferem integridade e contexto; não certificam origem.
Fills/custos ausentes não são convertidos em zero; resultado desconhecido
mantém reserva e impede projeção causal de patrimônio conhecido.

Provas e correções: roundtrip JSONB/CAS, tipo oficial PRE_SELECTION_STRUCTURAL,
validador puro (sem importar executor), envelope copiado sem mutar relatório,
reservas UNKNOWN, seed/saídas vinculados ao caller e R total incluindo funding.
Não enfraquecemos testes antigos. Duas passagens iniciadas durante edits foram
descartadas; um teste novo também revelou alias mutável, corrigido no construtor.

Relatório/limites: `docs/LIVE_EVENT_STUDY_RESEARCH.md`.
Pendências técnicas do lote original: caller de pyramiding/preflight + cadeia de segmentos;
sizing upstream ainda fornecido; export bruto com IDs excluídos/ordem causal.
Não confundir com WAITING_DATA nem voltar aos blocos provados para começar de zero.

## Estado final verificado em 13/10

- 101 testes focais novos/integração: duas passagens finais OK, fontes congelados.
- PostgreSQL 16/asyncpg descartável: 38 verificações, duas passagens finais OK;
  duas conexões reais, barreira/CAS, restart, catálogo DRAFT, GET sem escrita;
  zero TCP/DNS/Unix externo, clusters encerrados/removidos pelo próprio runner.
- Backend completo: 3.059 executados, 3.057 aprovados, dois skips históricos
  R05C por fixture privada ausente. Nenhum skip novo ou fixture fabricada.
- Sintaxe dos 18 Python alterados/novos + runner bash e staged diff-check OK.
  Nenhum TS/TSX/dist editado: TSC não se aplica.
- Logs locais: `/tmp/cw_live_event_focus_frozen_1.log`,
  `/tmp/cw_live_event_focus_frozen_2.log`, `/tmp/cw_live_event_pg_frozen_1.log`,
  `/tmp/cw_live_event_pg_frozen_2.log`, `/tmp/cw_live_event_full_verified.log`.
- Git: commit local do lote, 22 arquivos explícitos. Pessoais e outros projetos
  fora do stage; sem stash/reset/clean/amend, push ou deploy.

Retomar pelas três pendências técnicas acima. Não repetir discovery, V1 de
gestão, ledger, estatística ou correções já demonstradas para começar do zero.
Um ajuste no caller LIVE requer escopo/autorização próprios; não foi feito
furtivamente para produzir um positivo de pyramiding.

## Retomada — preflight LIVE autorizado, 13/10/2026

O usuário autorizou explicitamente a correção do contrato de preflight do
reforço, preservando flags, limites e alavancagem. O caller foi corrigido no
checkout principal, sem alterar a conta real ou publicar. Detalhes e limites:
`LIVE_PYRAMID_PREFLIGHT_FIX.md`.

Agora há P04B compartilhado, posição/proteção frescas por IDs exatos, reserva
oficial por trade/nível, autoridade final pós-esperas, qty efetivamente
executada e risco confirmado conservador retido até o trade fechar. Dados
inválidos/UNKNOWN falham fechado. Sem migração, ENV, flag ou frontend novos.

Não retomar pelo defeito de callback já corrigido. Próxima pendência técnica:
**portas PYRAMID do replay + cadeia de segmentos**, ainda bloqueadas. Sizing
upstream e export bruto causal com excluídos também seguem pendentes.
UNKNOWN/recusa terminal do reforço não receberam nova política automática
de retry/recovery: os limites desse contrato estão documentados, não ocultos.

Verificação final desse ajuste: 50 focais verdes 2×, PG16/asyncpg descartável
21 checks verdes 2×, backend 3.109 executados / 3.107 aprovados / dois skips
históricos R05C. Logs e runner reproduzível no relatório do preflight. Não
confundir esta prova de correção local com aprovação operacional da conta ou
com conclusão da réplica econômica PYRAMID.

## Retomada de 14/10 — portas e cadeia PYRAMID

Base `31523017`. Portas de posição/proteção/autoridade/reserva/DB explícitas e
vinculadas, estado versionado e continuação exata do gestor implementados.
Positivos LONG/SHORT completos com fills próprios e ledger nativo verificados.
PYRAMID sem obrigação nova de proteção executa o no-op real, sem SL fabricado.
Reserva usa a intenção efetiva do reforço, não a admissão genérica ENTRY.
Carteira global causal conserva reservas durante a espera, usa fill efetivo,
preserva risco confirmado e não antecipa resultado de prova posterior.

Duas falhas reais da correção anterior foram demonstradas e corrigidas:
`RealTrade.timeframe` inexistente (consulta agora a FK original, fail-closed),
e preflight exigindo TP1 já realizado (estágio pós-TP1 explícito somente no
reforço, todos os requisitos de TP2 e segurança preservados).

O lote integral continua **LOCAL_RESEARCH_PARTIAL**: sizing upstream, export
bruto causal com excluídos e carteira/caixa integral ainda são técnicos.
Persistência das portas é simulada; PG prova o catálogo/relatório, não o SL real.
Não repetir V1/ledger/portas já provados. Não afirmar réplica exata aprovada.
Relatório: `LIVE_PYRAMID_EVENT_CHAIN.md`.

Provas finais com fontes congeladas:

- 170 focais na chamada consolidada correta: OK. Os mesmos 161 casos de
  execução/portas/cadeia/study/preflight também passaram nas duas chamadas
  iniciais; essas chamadas tiveram erro de loader por nome incorreto do módulo
  de risco (erro do comando, não contado como aprovação de suíte). O nome foi
  corrigido; os 9 testes de risco passaram separadamente 2× e na consolidação.
- Backend completo: 3.179 executados / 3.177 aprovados / somente 2 skips
  históricos R05C por fixture auditada privada ausente. Nenhum skip novo.
- PG16 descartável: 50 checks OK 2×, incluindo cadeia real isolada, ingestão,
  restart JSONB e GET sem motores/escrita; regressão de risco retido 21 checks
  OK. Clusters encerrados, zero TCP/DNS/Unix externo no harness de estudo.
- Sintaxe dos 10 Python e diff-check OK; sem TS/TSX/dist, TSC não se aplica.
- Logs: `/tmp/cw_pyramid_chain_focus_final.log`,
  `/tmp/cw_pyramid_chain_risk_{1,2}.log`, `/tmp/cw_pyramid_chain_full.log`,
  `/tmp/cw_pyramid_chain_pg_{1,2}.log`, `/tmp/cw_pyramid_chain_pgrisk.log`.

Commit somente local. Nenhum acesso externo, ordem, ativação, push ou deploy.

## Retomada de 14/10 — consumidor nativo de dimensionamento

Base `5d1647e5`. Pedido/relatório upstream versionados, trecho original AST de
`open_shadow_for_recs` e helpers/acessores reais integrados à execução isolada.
Rounding, caps, stack e ordem nativos preservados. A qty final nativa alimenta
a admissão; não há segundo cálculo do stack nem canário aplicado duas vezes.
ENTRY com `sizing_stack` exige pedido causal correspondente. Binding inclui
símbolo EXATO (cache/regime não equivalem à normalização wire), oportunidade,
lado/TF/preços/equity/config/clock/hash. Configuração não vem de ENV histórico.

Equity e caches ordenados têm qualidade, geração e relógios explícitos. Fonte
sintética continua TEST_ONLY; hash não autentica origem. UNKNOWN obrigatório
é TECHNICAL_GAP, nunca oportunidade evitada ou lucro zero; o estudo conserva
o braço não comparável. `final_risk_pct` é o valor antes de `_compute_qty`,
não risco financeiro final: a qty admitida e o ledger têm suas próprias provas.

Escopo fechado: AFTER_EXISTING_ELIGIBILITY_BEFORE_SIZING. Não foram reproduzidos
produtores de risco/alavancagem, throttle BTC, elegibilidade/calibração/filler,
escritores/refresh do aprendizado/decay ou incremento do contador filler no
batch. Seguem também export frio causal com todos os IDs excluídos/purgados e
carteira/caixa integral. O lote continua LOCAL_RESEARCH_PARTIAL; não declarar
réplica integral, estudo real aprovado ou autorização operacional.

Retomar por essas pendências, sem refazer consumidor, PYRAMID, gestor e ledger
já provados. Relatório: `LIVE_UPSTREAM_SIZING_RESEARCH.md`. Nenhum serviço LIVE,
flag/default/limite, frontend, DDL, posição manual ou histórico alterado nesta
etapa. Sem acesso externo, ordem, ativação, push ou deploy.

Provas finais desta camada, com fontes congeladas:

- 41 testes do consumidor e 21 de integração: 62/62 OK 2×, sem skip novo.
- PG16/asyncpg descartável: 58 checks OK 2×; os 50 anteriores preservados e
  oito novos para sizing → execução → estudo → catálogo DRAFT → restart.
  Duas conexões/CAS, JSONB e GET sem replay/escrita. Sem DDL; zero TCP/DNS/
  Unix externo; ambos os clusters encerrados pelo runner.
- Sintaxe dos oito Python OK; diff-check limpo. Serviços LIVE e frontend
  não alterados nesta camada; sem TS/TSX/dist, TSC não se aplica.
- Logs focais: `/tmp/cw_upstream_final_frozen_62_1.log` e
  `/tmp/cw_upstream_final_frozen_62_2.log`; PG:
  `/tmp/cw_upstream_pg_1.log` e `/tmp/cw_upstream_pg_2.log`.
- Backend completo: 3.241 executados, 3.239 aprovados e somente os dois
  skips históricos R05C por fixture auditada privada ausente. Nenhuma fixture
  fabricada ou skip novo. Fonte congelada; uma rodada final, exit 0:
  `/tmp/cw_upstream_full_final.log` (577,5 s).
- Commit somente local com os 12 arquivos explícitos desta camada. Pessoais
  e outros projetos preservados e fora do stage; sem stash/reset/clean/amend.

## Retomada de 15/10 — produtores nominais e contexto filler

Base `1a6a4ded`. Produtores nativos de risco/alavancagem e throttle, no ponto
pós-build/pré-sort/pré-portfolio, implementados com lista inteira e caller
explícito. AST valida cálculo/cópia/ordem real; caminho legado sem throttle.
Não reaplicar sobre recomendação já dimensionada. Tier/score/lista continuam
observados, não reconstruídos pelo scanner. Report nominal reexecuta funções
reais para recusar adulteração econômica mesmo re-hasheada.

Contexto filler executa trechos AST originais de sort estável, rota
allowlist/capacidade/freio e incremento pós-persistência. Teste desligado não
consome contador; incidente/None não incrementam, UNKNOWN não fornece próximo
contexto. Histórico é prefixo ordenado, causal e vinculado a oportunidade,
trade/client/dispatch; repetição/restart recompõem o mesmo estado. Não usamos
POST/fill/manager seed como prova de retorno de persistência. Os retornos e
contagens iniciais são fatos observados; não alegamos writer DB reproduzido.

Envelopes opcionais nominal_producer/filler_batch integram source_hash,
validação e producer_refs do sizing. Presença exige ambos próprios vínculos
íntegros/completos; componentes ausentes mantêm modalidade observacional antiga.
Dados nominais devem estar disponíveis antes do filler; histórico não pode
antecipá-los. Todos os membros filler precisam existir no batch nominal.
Nada disso substitui elegibilidade completa ou autoriza LIVE/estudo real.

Ainda técnicos: origem integral de scores/tiers/seleção/elegibilidade, leitores
e writers de histórico/learning/decay, export frio com excluídos/purga/ordem
prospectiva e caixa/Binance integral. Não retomar pelo produtor nominal ou
contador já demonstrados, nem chamar essas pendências somente WAITING_DATA.
Lote permanece LOCAL_RESEARCH_PARTIAL. Relatório de escopo/retomada:
`LIVE_SIZING_PRODUCERS_AND_BATCH.md`.

Nenhum serviço LIVE/flag/limite/estratégia/posição manual/histórico alterado;
sem DDL/ENV/endpoint/frontend novo, conta/DB externo, ordem, mensagem, ativação,
push ou deploy. Somente commit local após as validações finais abaixo.

Mapa somente leitura para a próxima pendência (não implementado nesta etapa):

- SLS: `relearn_all_from_history` lê SymbolBacktestStats atuais; reusar as duas
  passadas/derive_params/rank nativos e estado anterior. Upsert só do melhor
  TF preserva linhas antigas; não reconstruir cache apenas com vencedores.
  `refresh_cache` não possui TTL e preserva cache antigo em falha.
- Decay: `maybe_refresh` lê RecommendationSnapshot por outcome_at, com duas
  leituras nativas de relógio. TTL só suprime refresh com cache não vazio;
  falha conserva cache/timestamp. Reusar agregação e getters sem traduzir.
- Computed_at/learned_at atuais e outcome_at não provam que o dado estava
  publicado antes da decisão. Congelar versões prospectivas, estado anterior,
  parâmetros e geração; sem história as-of: HISTORY_AS_OF_UNAVAILABLE. Não
  retrodatar agregado sobrescrito ou outcome ainda não disponível.

Provas finais já concluídas: 134 focais OK 2×, sem skip novo; sintaxe dos 11
Python e diff-check aprovados. Logs `/tmp/cw_sizing_producers_focus_1.log` e
`/tmp/cw_sizing_producers_focus_2.log`. Incluem os 62 da camada anterior mais
38 nominais, 25 filler e nove integrações novas, com fontes congeladas.

PG16/asyncpg descartável: 66 verificações OK 2×; as 58 anteriores preservadas
e oito novas ligando os produtores a estudo/JSONB/restart/DRAFT/GET read-only.
Mesmas qty 2 e líquido sintético 3,80 nos dois braços; nenhuma aprovação
econômica é inferida desse exemplo. Duas conexões/CAS anteriores preservadas,
zero TCP/DNS/Unix externo, sem DDL novo. Runners encerraram os dois clusters.
Logs `/tmp/cw_sizing_producers_pg_1.log` e `/tmp/cw_sizing_producers_pg_2.log`.

Backend completo: **3.313 executados / 3.311 aprovados / dois skips históricos
R05C** por fixture auditada privada ausente. Nenhum skip novo ou fixture
fabricada. Uma rodada final com fontes congeladas, 598,5 s, exit 0;
`/tmp/cw_sizing_producers_full.log`. Os erros/warnings anteriores no log são
cenários negativos exercitados, não falhas finais da suíte.

Commit local do bloco: 16 arquivos explícitos, índice sem pessoais/outros
projetos. `.DS_Store`/settings pessoais e untracked preexistentes preservados;
sem stash/reset/clean/amend, envio ao remoto ou publicação.
