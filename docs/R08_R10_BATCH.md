# Lote integrado R08B · R09 · R10A

Base: `b0d52a80`, checkout principal `main`. Publicação única após integração.
As alterações pessoais preexistentes não pertencem a este lote.

## Escopo e ordem

| Ordem | Entrega | Dependência para demonstrar benefício |
|---|---|---|
| 1 | R08B: captura prospectiva dos componentes/configuração do score | novos registros após deploy |
| 2 | R09: oportunidades e tentativas distintas, primeiro veto, observação segregada | coleta e cobertura da fonte compartilhada |
| 3 | R10A: replay OHLCV e comparador offline com configuração explícita | dados adequados, custos declarados e hipótese pré-registrada |
| 4 | Relatório no painel/API existentes e CLI local | integração e testes |
| 5 | Publicação coordenada backend/Worker/frontend | regressão aprovada, schema aditivo validado |

O funil começa na lista **pós-seleção** entregue ao executor. Não contabiliza
todos os candidatos da varredura. Contadores legados por gate são eventos,
não oportunidades. Campos ausentes e lacunas ficam explícitos.

## Isolamento

- Nenhuma regra de entrada, score, tier, probabilidade, risco, stop, TP,
  alavancagem ou dimensionamento LIVE é alterada.
- Traços são observação fail-soft. Configuração/fatores vêm do ponto de
  cálculo, não de uma reconstrução com configuração atual.
- Rejeições vivem em tabelas próprias; não alimentam snapshots operacionais,
  calibração, learning, rotação, risco, PnL ou RealTrade.
- Trajetórias usam velas já buscadas, fechadas e posteriores à decisão.
  Ausência de fonte compartilhada significa ausência de evidência.
- Replay é um adaptador OHLCV, **não replay fiel do executor LIVE**. Suposições
  de entrada/saída/custos ficam versionadas. Sem custos conhecidos não há
  retorno líquido confirmado. Nenhuma promoção automática.
- Os GETs mostram coleta/cobertura, não outcomes econômicos do teste final.

## Utilização

No painel de assertividade: seção **Decisões e laboratório de estratégias**.
API já existente: `GET /api/strategy/p05/status`, chave `research_batch`.
Sem novo endpoint mutante ou botão de ativação.

CLI local, sem rede ou credenciais:

```sh
backend/.venv311/bin/python -B backend/scripts/research_replay.py --manifest
backend/.venv311/bin/python -B backend/scripts/research_replay.py dataset.json
```

O schema e as hipóteses do replay ficam em `R10A_OFFLINE_REPLAY.md`; o funil
e a observação das vetadas, em `R09_DECISION_OBSERVATION.md`; o trace, em
`R08B_SCORE_TRACE.md`. A janela final de teste deve continuar protegida;
preparar infraestrutura não é aprovação econômica de uma estratégia.

## Estado do lote (fechamento local, sem publicação)

Pendências do checkpoint concluídas nesta etapa:

- **R08A × R10A:** o replay não importa mais o laboratório no topo; só o
  comparador o carrega, no ramo estrutural. Teste de isolamento do R08A
  ajustado apenas para essa fronteira (AST + subprocesso), proibição geral mantida.
- **R10A:** guarda `MANAGEMENT_ONLY` (no máximo um parâmetro; controle A/A
  aceito), leitura só do horizonte, barra que cruza a fronteira = erro, CLI
  recusa qualquer barra do holdout. Testes dedicados, fixtures sintéticas e doc.
- **R09:** mapa explícito status R10 → cobertura (terminais processados uma vez,
  lacuna/ambiguidade nunca resolvidas, setup inválido terminal); tetos fixos de
  50.000/100.000/50.000 sob lock transacional próprio, com descarte contado e
  resolver ativo; contenção reenfileirada com limite; timeout/cancelamento
  contados; semântica de "primeiro" rotulada; filtro de símbolos para o teto de
  janelas; velas inválidas recusadas; aviso de capacidade no status e no painel.
- **Achados corrigidos durante a validação:** (1) o endpoint
  `/api/recommendations` também chama o executor e não selava o lote — o buffer
  encheria de tentativas abertas; agora sela e remove os handles `_r09_*` da
  rec, e tentativas não seladas antigas só são despejadas com o buffer cheio;
  (2) o hook `ATTEMPTED` estava dentro da janela de latência do P05.2L, o que a
  suíte completa acusou; foi movido para antes da marca, sem alterar o teste.

Validação local: suíte completa 1.793 testes (2 skips R05C históricos por
fixture privada ausente), PostgreSQL 16 descartável só por socket, `tsc`,
`py_compile` e `git diff --check`. Números e limitações no `HARDENING_LOG.md`.

Ponto de decisão aberto: o trace R08B (≈3,7–8,8 KB por recomendação) também vai
na resposta de `/api/recommendations`, porque a mesma rec alimenta o snapshot.
Não foi alterado nesta etapa.

## Publicação e rollback

Commit(s) só com arquivos do lote. Push único dispara os serviços vinculados.
Conferir commit em Railway/Vercel, saúde do scan, presença das novas tabelas
pelo mecanismo normal do app e crescimento dos contadores sem erro de coleta.
Rollback do código por commit mantém as tabelas observacionais aditivas;
não apagar registros de pesquisa nem modificar o histórico operacional.

## O que permanece para depois

R07B/Score V3 operacional, alteração de filtros e promoção LIVE dependem de
amostra prospectiva suficiente e avaliação econômica separada. R11/R12 não
estão concluídos por esta entrega. Não afrouxar critérios para produzir vencedor.
