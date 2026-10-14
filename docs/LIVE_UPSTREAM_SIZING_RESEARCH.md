# Dimensionamento nativo no replay — camada consumidora

Base `5d1647e5`, checkout principal, 14/10/2026. Implementação somente local
de pesquisa. O código do bot LIVE, parâmetros, limites, histórico, posição
manual e configuração operacional não foram alterados. Sem publicação.

## O que é reproduzido

Contrato `CW_LIVE_UPSTREAM_SIZING_REQUEST_V1` → trecho original AST de
`open_shadow_for_recs` → `CW_LIVE_UPSTREAM_SIZING_REPORT_V1` → admissão e
transporte isolados já existentes → gestor → ledger → estudo e catálogo.

O trecho é localizado estruturalmente, não por número de linha. Suas
instruções permanecem intactas, inclusive arredondamentos e `continue`:

1. Funding-EV e sua recusa.
2. Risco nominal × convicção × edge × histórico por moeda × funding tilt.
3. Equity observada e `_compute_qty` real (margem, teto de risco, mínimo).
4. Quantidade × damp × short-slip × liquidez × regime × edge-decay × filler
   × canário global, com teste de mínimo notional em cada estágio nativo.

Os helpers e acessores reais são carregados pela fonte, não reimplementados.
`_sizing_stack_report` continua apenas uma auditoria do caller: não vira motor
nem substitui o cálculo. `suggested_size_pct`/Kelly consultivo não é usado.

## Configuração, caches e causalidade

Todas as constantes consumidas são explícitas/congeladas. Sem completar flags
ou parâmetros com ENV do processo como se fossem histórico. A configuração
da camada é comum aos dois braços em `execution_config.sizing_stack`.

Equity, cache aprendido e cache de decay têm identidade, geração, qualidade,
instantes observado/disponível e hash do corpo. Cache vazio CAPTURED é conhecido;
UNAVAILABLE não é vazio. Dados posteriores à decisão são recusados.
As listas ordenadas preservam o desempate nativo por confiança entre TFs.

O processo isolado não herda credenciais/ENV, não carrega clientes de exchange,
não consulta banco e bloqueia rede. Erro que o código LIVE engole por fail-soft
continua registrado como lacuna: não vira COMPLETE_SIZING silencioso.
Fonte sintética continua TEST_ONLY; hashes não autenticam os fatos fornecidos.

## Integração e significado dos números

ENTRY com `sizing_stack` exige pedido upstream. Ausência não cai no cálculo
legado. Pedido e resultado casam oportunidade, símbolo EXATO, direção, TF,
relógio, preços, alavancagem, equity, configuração e hash de contexto. O símbolo
exato importa para os acessores: equivalência wire não é equivalência de cache
ou regime. PYRAMID mantém sua política própria, já provada, sem reaplicar stack.

`final_risk_pct` é o risco após multiplicadores **antes** de `_compute_qty`:
não representa o risco econômico final. Caps atuam dentro do helper e ajustes
posteriores podem reduzir qty. A admissão usa qty nativa final, depois precisão
e preflight. O canário não é aplicado outra vez. Risco financeiro é calculado
da qty/preço/stop realmente admitidos; fill e ledger continuam provas separadas.

Recusa nativa conhecida produz zero POST. Falta de cache obrigatório gera
TECHNICAL_GAP e período/braço não comparável, não operação evitada ou lucro zero.
Nenhum status desse pacote aprova estudo real, SHADOW, promoção ou LIVE.

## Limite explícito — consumidor não é produtor completo

Escopo: `AFTER_EXISTING_ELIGIBILITY_BEFORE_SIZING`. Ainda são entradas observadas:

- Risco/tier/alavancagem produzidos por `_compute_leverage` e throttle BTC do
  batch anterior; esses produtores não foram reproduzidos nesta camada.
- Elegibilidade, calibração/probabilidade e classificação `_is_filler`.
- Escritores/refresh do aprendizado e do decay; snapshot causal do cache não
  prova a origem histórica de seu treino.
- Contador `filler_test_done` no instante da oportunidade. O writer que o
  incrementa entre operações do batch ainda não é um motor desta camada.

O zero explícito em `risk_pct` conserva a convenção atual do caller (`or 1.0`),
registrada no relatório; não corrigimos silenciosamente essa política no LIVE.
Dados inválidos no contrato não são tratados como zero.

Portanto o lote integral segue LOCAL_RESEARCH_PARTIAL. Ainda faltam produtores
anteriores/estado do batch, export frio completo com ordem causal prospectiva e
carteira/caixa integral. Histórico sem ordinal real não receberá índice inventado.
Gestão, execução modelada e qualidade financeira continuam dimensões separadas.

## Provas

Focais e integração: helpers/caller reais, LONG/SHORT, caps, stack, cache ordenado,
ausência/zero, fail-soft registrado, canário uma vez, pedido/relatório adulterados,
entrada real isolada e estudo nos dois braços. PG descartável verifica somente
persistência/restart/catálogo, não garante atomicidade com a corretora.

Contagens finais e logs no `LIVE_EVENT_STUDY_CHECKPOINT.md`. Nenhuma tabela,
coluna, ENV, endpoint, scheduler de produção ou frontend novo. Nenhuma ordem,
conta, banco externo, exchange, mensagem real, ativação, push ou deploy.
