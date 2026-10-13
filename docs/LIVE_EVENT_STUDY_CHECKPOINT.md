# Réplica econômica por eventos — checkpoint

Base: `c12ea043`. Implementação local autorizada em 12/10/2026. LIVE,
conta, parâmetros, posição manual, holdout e histórico não serão alterados.

Retomado em 13/10 sem refazer os blocos. Execução isolada, ledger por evento,
estatística e integração/export/catalog/status implementados localmente.
Estado do pacote: **LOCAL_RESEARCH_PARTIAL**, não aprovação integral do lote.
Nenhum push/deploy nem estudo real autorizado.

Bloqueio técnico comprovado: `_maybe_pyramid` pede MARKET sem o preflight
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
Pendências técnicas: caller de pyramiding/preflight + cadeia de segmentos;
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
