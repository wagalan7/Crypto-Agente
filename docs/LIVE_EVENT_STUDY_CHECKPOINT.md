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
