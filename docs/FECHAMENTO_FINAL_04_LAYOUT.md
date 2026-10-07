# Lote 04 — layout claro, responsivo e honesto

Base: `6fc9bf1aa9a46cc4431c2a68f2e520fe41cad6dd` (publicada) · branch
`worktree-lote02-pesquisa` · worktree `lote02-pesquisa` (autorizado).
**FRONTEND APENAS.** Nenhuma decisão, estratégia, risco, limite, flag, ENV ou
ordem foi alterada; nenhum endpoint novo; nenhum push/deploy. `frontend/dist`
e arquivos pessoais preservados.

---

## 1. Problema → correção (ANTES / DEPOIS)

| # | ANTES | DEPOIS |
|---|---|---|
| 1 | `risk` ausente/erro ⇒ **"Operando"** com bolinha verde pulsando. | Função PURA única (`src/lib/operationalState.ts`): `LOADING / UNAVAILABLE / STALE / BLOCKED / NO_BLOCK_CONFIRMED / AVAILABLE`. Verde (`AVAILABLE`) só com **fato explícito de autoridade de entrada** — que o contrato atual **não publica**. Hoje o melhor estado possível é "Sem bloqueio de risco/P03 nesta leitura", nomeando o que não foi confirmado. |
| 2 | `daily_dd_pct ?? 0`, `wins ?? 0`, `win_rate ?? '—'`, `trades_total ?? 0`: ausência virava zero. | `finiteNumber`/`literalBool`/`countOf`: só valor literal passa. Ausente aparece como **"não confirmado"** ou "—", nunca 0. Zero legítimo continua 0. |
| 3 | Falha de `/api/real-trades` virava **lista vazia** ("Nenhuma posição aberta agora"). | Leitura com qualidade: erro ⇒ "Lista de posições não disponível nesta leitura. Isso não é lista vazia." Vazio confirmado continua vazio confirmado. |
| 4 | Incidentes P03 **não existiam** na interface: a Home prometia retomada automática. | Leitura delimitada do GET público existente `/api/execution-incidents/status`, em Home e Sistema, por método tipado + cache/single-flight compartilhado. Incidente/quarentena **prevalecem** sobre risco sem pausa. |
| 5 | Hero "💰 Resultado de hoje" (fonte `/api/daily-pnl`) lido como dinheiro da conta. | "Resultado dos setups · hoje (UTC)", unidade explícita (`% da banca (setups)`), nota de que o contrato não demonstra líquido confirmado. |
| 6 | Badge do header: verde "OK" sempre que `trading_paused === false`. | Mesma função pura com **escopo declarado** `RISK_ONLY`: "SEM PAUSA" (azul) / "BLOQUEADO" / "SEM CONFIRMAÇÃO"; o tooltip lista motivo, lacunas e idade da leitura. Erro deixou de ser silencioso. |
| 7 | Scanner: `R/R↓` ordenando por `confidence`. | Rótulo "Força do sinal↓". **Chave `rr` e algoritmo intactos** (`b.confidence - a.confidence`). Confiança não virou probabilidade. |
| 8 | Dashboard: selo global **PAPER** sobre blocos de fontes diferentes. | Selo "FONTES SEPARADAS"; cada bloco declara fonte e janela ("Curva dos setups (paper) · janela 30d", "Registros do app · real/shadow"). |
| 9 | Rail/barra (z-60) podia cobrir painéis e ações. | Classe `.app-overlay` em **10 overlays**: reserva do rail no desktop, da barra no mobile e `env(safe-area-inset-bottom)`. |
| 10 | Pausa aparecia duas vezes (cartão + banner) e o banner oferecia "Retomar agora" mesmo com P03. | O ESTADO é dito **uma vez**. A faixa de ação só aparece quando a retomada manual é válida — com pausa do P03 ela some e o único caminho é o diagnóstico. |

Também: `bot_verdict.ok ?? true` (ausência virava "OK") → `elegível / em espera / sem verdito`; `P(TP1)` rotulado como modelo, com nota de que força/confluência não é probabilidade; heartbeat com aviso de que não é autorização; foco visível, `prefers-reduced-motion`, Escape fechando overlay com retorno de foco.

## 2. Fontes, estados e janelas

| Campo (tela) | Endpoint | Fonte/coorte | Unidade | Janela | Qualidade |
|---|---|---|---|---|---|
| Estado operacional | `/api/risk/status` + `/api/execution-incidents/status` | risco + reconciliador P03 | — | leitura atual | nível + lacunas + instante do último OK |
| Resultado dos setups | `/api/daily-pnl` | SETUPS recomendados | % da banca ou R | hoje (UTC) | `None` ≠ 0 |
| Travas de perda | `/api/risk/status` | risco | % | dia / semana | "não confirmado" quando ausente |
| Posições por origem | `/api/real-trades` | **registros do app** | contagem | abertos | erro ≠ vazio |
| Curva paper | `/api/paper/summary` | setups (paper) | % | 30 dias | ausência declarada |
| Incidentes | `/api/execution-incidents/status` | P03 | contagem | leitura atual | `ok=false`/erro ⇒ não confirmado |

Regras aplicadas: `/api/real-trades` **não** é inventário da conta (dito na tela);
posições externas/manuais da conta **não** são exibidas (não há fonte legítima);
`ready=true` do preflight **não** entrou no frontend; saúde/heartbeat não autoriza
entrada; motivos usam vocabulário controlado e desconhecido vira
"Motivo não disponível nesta leitura", com detalhe técnico saneado (sem stack,
sem credencial, sem truncar causa) em `<details>`.

## 3. Paridade preservada

- Ordenação, filtros, ações, confirmações, rotas e corpos **inalterados**
  (`/api/risk/kill-switch?paused=…` POST + `window.confirm` / confirmação 2-step).
- Nenhum botão novo de enable-live/promote/execute/clear/retry-now; CTA de causa
  P03 leva ao diagnóstico existente.
- Nenhum recálculo de score, risco, probabilidade, tier ou elegibilidade no
  cliente: os cartões exibem o que o chamador formatou.
- Polling, WebSocket, caches e navegação existentes preservados; a única chamada
  **aditiva** é o GET P03 (ver §5).

## 4. Testes

`cd frontend && node qa/run-tests.mjs` → **42 testes, 42 passaram, 0 falhas**
(runner local: esbuild já instalado + `node --test`; nenhuma suíte nova instalada).

- `qa/tests/operationalState.test.ts` (24): primitivos, saneamento, tradução de
  motivos, risco ausente/false/erro/stale/zero legítimo, pausa manual, pausa P03,
  quarentena sem pausa de risco, `ok=false`, payload incoerente, health+pausa,
  preflight+incidente, escopo `RISK_ONLY`, frescor que não se renova.
- `qa/tests/markup.test.tsx` (9): markup real via `react-dom/server` — sem verde
  por ausência, bloqueio com causa, leitura antiga rotulada, P03 sem zero
  fabricado, setups ≠ lucro da conta, fonte indisponível, erro ≠ lista vazia,
  paridade numérica, ausência de botão perigoso.
- `qa/tests/p03Read.test.ts` (3): **duas chamadas simultâneas = 1 GET**, cache na
  janela, `maxAgeMs: 0` força, erro não contamina o cache.
- `qa/tests/contracts.test.ts` (6): rotas/método/confirmação das ações
  preservados, sem segredo/admin token, endpoint P03 num único ponto, preflight
  fora do polling, chave de ordenação preservada, estado decidido num só módulo,
  overlays com reserva de navegação.

`npx tsc --noEmit` (binário local): **limpo**. Build: `vite build --outDir /tmp/cw_l04_qa/build` **OK** (`frontend/dist` intocado).

## 5. Chamada aditiva declarada

O frontend passou a fazer **um GET a mais por ciclo**:
`/api/execution-incidents/status`. Medido no preview isolado: **1 chamada por
ciclo de 20s** com a Home aberta (cache/single-flight de 12s compartilhado entre
Home e Sistema na mesma página). A contagem de requests **não** permaneceu
idêntica. Abas separadas são contextos JS distintos e fazem uma leitura cada.

## 6. QA visual com APIs mockadas

Preview isolado: `cd frontend && node_modules/.bin/vite --config qa/preview/vite.config.ts`
(`http://127.0.0.1:5199/?cenario=…`). O mock é instalado **antes** de montar a
app; se falhar, a app não monta. Fixtures sintéticas rotuladas `SINTÉTICO-QA`.

Medido na sessão: **8 requisições externas bloqueadas e contadas**
(`fapi.binance.com`), **0 mutações enviadas** (POST/PATCH interceptados),
**2 WebSockets** substituídos por stub, `VITE_API_URL` apontando para sentinela
local. Sem scroll horizontal em 390 / 768 / 1024.

Evidências (JPG) em `/tmp/cw_l04_qa/shots/`:

| Arquivo | Viewport | Caso |
|---|---|---|
| `01-1440-sem-bloqueio.jpg` | 1440 | sem bloqueio confirmado |
| `02-1024-p03-bloqueado.jpg` | 1024 | bloqueio por incidente + quarentena |
| `03-768-sem-confirmacao.jpg` | 768 | risco e P03 indisponíveis |
| `04-390-p03-bloqueado.jpg` | 390 | bloqueio no mobile |
| `05-390-modal-critico-sistema.jpg` | 390 | modal crítico sobre a barra inferior |
| `06-390-sem-bloqueio.jpg` | 390 | sem bloqueio no mobile |
| `07-1440-pausa-manual.jpg` | 1440 | pausa manual com a ação existente |

Teclado verificado no preview: Escape fecha o overlay e o foco volta ao elemento
anterior; CTA do cartão abre o diagnóstico existente.

## 7. Limitações declaradas

- **Não verificado**: leitores de tela reais, contraste medido por ferramenta,
  iOS/Android físicos, telas que o lote não tocou (Insights, Sweep, NLP, Chart)
  além da reserva de navegação, e o comportamento das ações mutáveis em execução
  (os POSTs foram interceptados por contrato de QA, nunca enviados).
- `qa/tests/contracts.test.ts` verifica o **código-fonte** das ações existentes
  (rota/método/confirmação): é prova de não-alteração, não de execução.
- Clique sintético do automatizador não acionou alguns botões no pane; a
  verificação desses casos foi feita por `click()` programático e inspeção do
  DOM. Os screenshots são do estado real renderizado.
- Posições externas da conta continuam **sem fonte** no frontend: o card declara
  isso em vez de inventar lista ou total.
- `LOCAL_VERIFIED`: não publicado, não `OPERATIONAL_ACCEPTED`.

## 8. Build e execução

```bash
cd frontend
node qa/run-tests.mjs                                   # testes do lote
node_modules/.bin/tsc --noEmit                          # tipos
node_modules/.bin/vite build --outDir /tmp/cw_l04_qa/build --emptyOutDir
node_modules/.bin/vite --config qa/preview/vite.config.ts   # preview isolado
```

## 9. Arquivos

Novos: `src/lib/operationalState.ts`, `src/components/status/OperationalCards.tsx`,
`qa/` (runner, 4 testes, preview isolado).
Alterados: `src/App.tsx`, `src/index.css`, `src/services/api.ts`,
`components/HomeCockpit.tsx`, `components/StatusPanel.tsx`,
`components/RiskStatusBadge.tsx`, `components/DashboardPanel.tsx` e a reserva de
navegação em `RecommendationsPanel`, `DailyPnLPanel`, `AssertivenessPanel`,
`InsightsPanel`, `SweepPanel`, `TradeManager`, `ChartModal`.

Integração em `main` é etapa seguinte, após revisão: nada foi integrado aqui.
