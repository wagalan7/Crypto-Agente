# Correção integrada do Lote 02 — checkpoint

Base: `a39ab3580928a7ffd8d36070a07e715b6ef910de`, worktree autorizado
`lote02-pesquisa`. Iniciado em 05/10/2026 por autorização do usuário.

Estado: **LOCAL_VERIFIED — CORREÇÃO VALIDADA**, publicação ainda não executada.

- Coleta: batching, cobertura, veto/filtro final e features/proveniência.
- Manifesto: identidade real, configuração/custos executados e catálogo V2.
- Calibração: validação semântica/temporal, eventos e payoff OOS.
- Integração: export bruto, estudo offline, artefato persistido e status de leitura.

Nenhum default, flag, LIVE, limite, champion, posição manual, histórico ou
quarentena será alterado. Holdout final permanece selado. Nenhuma chamada à
produção ou corretora real está autorizada para estes testes.

Provas: 129 focais; suíte completa 2.695 (2 skips R05C históricos), verde 2×;
PG16 descartável 26 verificações novas + 19 regressões, verde 2×; compile e
diff-check limpos. Detalhes em `LOTE02_CORRECAO_INTEGRADA.md`.

Pendências externas: decisão humana de pesquisa, coleta/amostra prospectiva,
janelas históricas/quotes, limites OOS e aprovação/canário. Substituição de
núcleo/playbooks e adaptação LIVE seguem fora do escopo implementado. Lotes
03/04 não iniciados. Nenhuma aprovação operacional declarada.
