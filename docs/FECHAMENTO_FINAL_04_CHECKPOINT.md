# Lote 04 — checkpoint (layout)

Base: `6fc9bf1a` (publicada, conferida como ancestral do HEAD) · branch
`worktree-lote02-pesquisa` · worktree `lote02-pesquisa`.
Estado: **LOCAL_VERIFIED** — commit local, sem push/deploy e sem integração em
`main`. Relatório: `docs/FECHAMENTO_FINAL_04_LAYOUT.md`.

## Blocos

| Bloco | Entrega | Estado |
|---|---|---|
| 1 | Descoberta dirigida (App, index.css, NavRail, Home, badge, Status, Dashboard, Recs, api, types) e mapa campo→endpoint→fonte→unidade→janela | concluído |
| 2 | Função PURA de estado operacional (`src/lib/operationalState.ts`) | concluído |
| 3 | Leitura delimitada do GET P03 com cache/single-flight compartilhado | concluído |
| 4 | Consumidores: Home, selo do header (escopo `RISK_ONLY`), Sistema | concluído |
| 5 | Cartões de apresentação puros (estado, P03, fonte/janela, posições) | concluído |
| 6 | Rótulos honestos: setups ≠ conta, força ≠ R/R ≠ probabilidade, fontes do Dashboard | concluído |
| 7 | Responsividade/overlays/a11y: `.app-overlay`, safe-area, foco, reduced-motion, Escape | concluído |
| 8 | Testes (42) + tsc + build em diretório temporário | concluído |
| 9 | QA visual isolada: 4 viewports × 4 cenários, externos bloqueados/contados | concluído |
| 10 | Documentação e commit | concluído |

## Decisões que valem registrar

- **Verde exige fato.** `AVAILABLE` só existe com autoridade de entrada
  explícita. Nenhum endpoint do contrato publica esse fato hoje, então a tela
  para em "Sem bloqueio de risco/P03 nesta leitura" e lista o que falta. Não foi
  inventado agregado de backend.
- **Escopo declarado em vez de decisão duplicada.** O selo do header não lê
  incidentes (nenhuma chamada por card): ele passa `scope: 'RISK_ONLY'` para a
  MESMA função, que então declara o P03 como lacuna.
- **Estado dito uma vez.** O banner de pausa virou apenas hospedeiro da ação
  existente de retomada e some quando a pausa é do P03 — nenhum caminho de
  interface contorna o reconciliador.
- **Chamada aditiva declarada**: +1 GET `/api/execution-incidents/status` por
  ciclo de 20s (cache de 12s compartilhado). A contagem de requests mudou.

## Não verificado (declarado)

Leitor de tela real, contraste medido por ferramenta, device físico, execução
das ações mutáveis (POSTs interceptados por contrato de QA) e painéis não
tocados além da reserva de navegação.

## Próximo passo

Revisão humana e, se aprovada, integração em `main` + publicação (Vercel) como
etapa separada. Layout não substitui as decisões/dados pendentes dos Lotes 02–03.
