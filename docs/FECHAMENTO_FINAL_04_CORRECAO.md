# Lote 04 — correção da revisão

Data: 07/10/2026. Baseline local `db21ef1f`, branch
`worktree-lote02-pesquisa`, worktree autorizado `lote02-pesquisa`.
Estado: **LOCAL_VERIFIED_PENDING_INTEGRATION**.

## Defeitos e correções

| Defeito reproduzido | Correção no caminho consumidor |
|---|---|
| Sistema/badge ofereciam retomada apesar de P03 | `canOfferResume` compartilhado e reconferido antes do POST; P03/validação manual não são kill switch |
| HTTP 200 com `ok=false` entrava no cache | domínio validado; erro mantém diagnóstico conhecido no consumidor e invalida reutilização do cache |
| Subcontagens/listas positivas com total zero confirmavam ausência | fato positivo prevalece; incoerência vira lacuna, nunca zero confirmado |
| Fonte lenta/cache atualizava artificialmente o frescor | carimbo individual de chegada; WeakMap do P03 conserva instante da resposta, não do cache hit |
| Relógio parava durante leitura pendente | relógio local de 1s independente, sem consulta adicional; timeout de leitura 10s |
| Cleanup do Escape roubava foco a cada render | uma inscrição por montagem, callback atual via ref; foco devolvido só ao desmontar |
| Lista vazia antiga era apresentada como atual | `PositionsEmptyState` distingue confirmado/antigo/indisponível; componente real coberto por SSR |
| Histórico vazio sugeria sinal verde | texto declara somente ausência de eventos exibíveis, sem afirmar ausência de bloqueios |
| QA deixava escapar recursos diretos e métodos Request | CSP e política estrita de origem/método; transportes auxiliares contidos; API sem fixture retorna 503 |

Os controles positivos preservam pausa manual legítima, zero confirmado,
rotas/corpos/confirmações e cadências existentes. O selo continua `RISK_ONLY`:
não recebeu nova consulta P03 nem foi transformado em autorização operacional.

## Provas do estado final

- `node qa/run-tests.mjs`: **79 aprovados, zero falhas/skips, duas execuções**.
  Seis arquivos: estado, markup real, leitura/cache P03, contratos,
  ciclo de vida dos controladores e isolamento de QA.
- `tsc --noEmit`: exit 0. Build Vite: exit 0, saída temporária
  `/private/tmp/cw-l04-final.dk6yNY`; `frontend/dist` preservado.
- Navegador com fixtures sintéticas: cenário P03 sem botão Retomar em Home e
  Sistema; cenário manual com ação preservada; Escape retorna ao lançador Home;
  foco do Sistema não é removido pelas atualizações do relógio; mobile 390px
  sem scroll horizontal. Captura `/private/tmp/cw-l04-status-corrected.jpg`.
- CSP efetivamente bloqueou `https://s3.tradingview.com/tv.js` no probe local,
  com `securitypolicyviolation` visível. Header de resposta conferido. Contagem
  de bloqueios do mock não representa os bloqueios CSP de recursos diretos.
- `git diff --check`: limpo. Sem alteração de backend ou artefato dist.

## Limites e preservação

Testes de lifecycle exercitam os mesmos controladores usados pelos hooks,
com EventTarget/relógio falsos; não se apresentam como renderer DOM de React.
Markup usa React SSR, complementado pelas verificações de teclado no navegador.
Não executados: POSTs reais, conta/exchange, leitor de tela real, contraste por
ferramenta ou dispositivo físico. Não se afirma acessibilidade integral.

Permanecem avisos de build sobre tamanho do bundle e base Browserslist antiga;
nenhuma dependência foi instalada/atualizada para este pacote.

Nenhuma estratégia, score, tier, limite de risco/alavancagem, flag, ENV,
aprovação, pausa/quarentena de produção, ordem, endpoint ou backend alterado.
Arquivos pessoais e os dois prompts untracked preexistentes preservados.
Sem merge/push/deploy: integração e publicação são etapas separadas.
