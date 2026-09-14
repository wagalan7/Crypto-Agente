# Correção pontual — fonte de liquidez da execução Binance

Base: `892d53f2`, checkout principal `main`. Revisão local em 14/09/2026.

## Causa e correção

O gate LIVE importava `binance_service.fetch_ticker`, cujo provedor real é OKX.
Uma resposta sem `data[0]` gerava `IndexError`; o `except` fail-soft permitia
continuar sem comprovar liquidez. Isso não explica, sozinho, a ausência de
operações: os bloqueios de sizing e calibração identificados separadamente
continuam válidos e não foram afrouxados.

- Volume LIVE agora vem de `binance_futures_service.fetch_ticker`, com o cache
  e o rate gate existentes. `quoteVolume` já é volume na moeda de cotação
  (USDT no universo suportado), sem multiplicação extra pelo preço.
- Resposta vazia, erro, símbolo divergente, preço inválido ou volume não finito
  são recusados antes de alimentar o cache. Zero de volume é conhecido, não
  ausência: reprova quando abaixo do piso configurado.
- Spread LIVE usa `exchange_service.get_execution_quote`: bookTicker Binance
  pelo mesmo cliente/BASE/modo do executor. Fonte, símbolo, bid/ask positivos e
  finitos e ausência de book cruzado são exigidos.
- Volume abaixo do piso evita a consulta de spread. Falha de leitura ou contrato
  gera skip `liquidity-gate`, antes de sizing, persistência ou ordem.
- Gate desligado ou ambos os limites em zero continuam sem consulta. SHADOW
  preserva sua fonte e comportamento legado, inclusive o fail-soft.

## Limites preservados e limitações

Nenhum piso de volume, teto de spread, score, calibração, risco, alavancagem,
sizing ou flag foi alterado. Esta correção muda deliberadamente o comportamento
LIVE diante de dado desconhecido: antes continuava; agora bloqueia a entrada.
Não atua no gerenciamento/proteção das posições já abertas.

O ticker de volume existente requer `BINANCE_PROXY_URL` configurado e consulta
Futures mainnet; sem proxy o gate falha fechado. O bookTicker segue o modo do
executor, mas o volume não foi adaptado a testnet/demo. Esta entrega valida o
contrato PRD Binance USDT, não uma fonte de volume específica de testnet.
O cache do volume continua com TTL de 60 segundos; bookTicker não usa cache.
Os produtores normais usam símbolo CCXT canônico (`BTC/USDT:USDT`); não foi
ampliado o contrato para aliases arbitrários. A revalidação final P04 continua
independente e inalterada.

Nenhuma probabilidade ou faixa de score foi ampliada. O rascunho de teste da
proposta de cobertura 75–100 foi retirado da suíte e preservado fora do projeto,
pendente de autorização explícita. Nenhuma avaliação de holdout foi feita.

## Verificação

14 testes direcionados cobrem fonte/unidade/cache, campos inválidos, limites,
exceções, livro cruzado, componentes desligados e o loop real de entrada com
sizing/ordens/DB substituídos por sentinelas. Dados válidos chegam ao gate
seguinte; dados desconhecidos param no gate de liquidez. Não há exchange ou
banco real nos testes.

Suíte completa executada 2×: em cada rodada, 1.663 testes executados, 1.661
aprovados e 2 skips preexistentes R05C (fixture auditada indisponível).
`py_compile` dos arquivos Python tocados e `git diff --check` aprovados.

O teste de escopo R08A foi limitado ao range concluído `7d202144..892d53f2`:
antes comparava a baseline com todo o checkout, classificando esta correção
operacional posterior como modificação do laboratório. Os demais testes de
isolamento R08A continuam verificando o código atual.

Publicação não faz parte desta validação local. Antes de publicar, confirmar
proxy disponível, branch/commits incluídos e estado do serviço Railway. Depois,
validar pelos skips/logs, sem forçar operação nem relaxar limites de risco.
