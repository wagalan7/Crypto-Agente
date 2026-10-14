# Reforço no replay por eventos — verificação local

Base `31523017`, checkout principal autorizado, 14/10/2026.
Este pacote não ativa candidata, coleta, SHADOW, canário ou estudo real.
Flags, limites, alavancagem, posição manual e histórico permanecem preservados.
Não há aprovação operacional nem alegação de ganho ou redução de stops.

## Fluxo e fronteiras

Entrada isolada → gestor LIVE isolado → reforço isolado → continuação do gestor
→ fills atribuídos → ledger nativo → evidência → persistência oficial.

O reforço usa os corpos locais de `_maybe_pyramid`, validação da proteção,
tetos legados, preflight P04 e exame síncrono final. Posição, SL/TP2 exatos,
saldo, profundidade, consulta da recomendação original, autoridade e escrita
são portas explícitas, vinculadas ao pedido, ordinal, hash e relógio.
Estado inválido ou prova ausente não recebe resposta positiva por default.

A persistência das portas é `RECEIPT_BOUND_SIMULATED_PORTS`: o estado local
versionado reproduz consumo/CAS e preserva risco confirmado, mas não demonstra
atomicidade com a exchange nem substitui o PostgreSQL do executor LIVE.
O teste PG deste pacote verifica a ingestão/reinício do relatório no catálogo
oficial, não transforma os recibos sintéticos em operações reais.

Após o reforço, somente quantidade e notas aplicadas pelo writer atravessam
para o gestor, com prova de hash/identidade. Base original de entrada,
quantidade inicial, relógios, fase e IDs das proteções não são reescritos.
Os fills do reforço possuem seu despacho próprio e entram no ledger uma vez.

O helper real de proteção não solicitada é executado como no-op quando todos
os níveis são ausentes. Não há confirmação de SL fabricada nesse caminho:
o reforço exige antes as proteções existentes e não pede novos níveis.

## Duas correções do caller LIVE encontradas pelos testes reais

1. `RealTrade` não possui atributo `timeframe`. O caller agora obtém o TF da
   recomendação original pela FK existente. FK/TF ausente ou inválido bloqueia
   antes da reserva. Não usa o TF da nova recomendação nem inventa `na`.
2. O preflight normal exigia um TP1 que já foi realizado. Somente o caller de
   reforço, depois de provar fase pós-TP1 e proteção, declara `tp1_completed`.
   Nesse estágio não há obrigação de TP1 futuro. Geometria/limites do TP2,
   profundidade, funding, risco, margem e autoridade continuam obrigatórios.
   Entrada normal mantém o mesmo requisito anterior de TP1.

São correções de contrato com possível impacto em reforços antes recusados,
não mudança de estratégia, risco máximo, alavancagem ou ativação de flag.
O pacote ainda não foi observado na conta e não está publicado.

## Cronologia e carteira

Os segmentos são retomados em ordem global, incluindo oportunidades distintas.
Reforço futuro não aparece na primeira entrada. Durante um envio pendente,
reserva de risco/margem/slot continua consumida; reforço não cria segundo slot.
A reserva PYRAMID vem do evento real de RESERVE, não da admissão genérica de
ENTRY. Transferência usa fills observados, não a quantidade planejada.
Repetição do mesmo evento é idempotente; divergência material é recusada.
UNKNOWN preserva capacidade comprometida. Falha financeira posterior não
apaga exposição provada nem altera o conhecimento de uma decisão anterior.

Resultado só fica disponível após todas as provas necessárias. Empate entre
traços independentes não antecipa liberação de margem ou resultado. O ledger
e o gestor mantêm fontes/valores separados; intenção aceita não prova fill.

**Limite importante:** `CONSERVATIVE_ATTRIBUTED_EXPOSURE_BOOK` não é réplica
do saldo Binance. Movimentos de caixa parciais/funding/unrealized P&L não são
reconhecidos antecipadamente: `partial_cash_recognized=false` e
`broker_balance_replica=false`. O livro usa exposição atribuída e capital
inicial explícito; confirmação integral financeira vem do ledger.
Não usar esse livro como aprovação da réplica exata solicitada pelo usuário.

## O que continua técnico, e não falta de amostra

- Sizing upstream ainda chega como input final ponto-no-tempo; os escritores,
  cache e aprendizagem anteriores não são reproduzidos integralmente.
- Export bruto precisa preservar identidades frias de todos os excluídos e
  ordem causal. Export antigo incompleto não é corrigido por backfill inventado.
- Livro de caixa/autoridade durante eventos concorrentes não demonstra saldo
  real ou atomicidade com a corretora; a persistência de portas é simulada.
- Manifesto real ratificado, dados prospectivos e autorização operacional são
  etapas posteriores. `real_study_allowed`, `authorizes_live`, `promotable` e
  `live_equivalent` continuam false; holdout continua SEALED.

## Verificação

Contagens finais, logs e estado de retomada no `LIVE_EVENT_STUDY_CHECKPOINT.md`.
170 focais consolidados OK; backend 3.179 / 3.177 aprovados / dois skips
históricos R05C. PG16/asyncpg do catálogo 50 checks OK 2×; risco retido 21 OK.
Sintaxe e diff-check aprovados. Um erro de nome de módulo em chamadas iniciais
foi corrigido no comando; a execução consolidada final terminou com exit 0.
Testes incluem positivos LONG/SHORT pelos callers reais, níveis/identidade
adulterados, reserva, retenção UNKNOWN, continuação do gestor e ledger.
Sem migração, ENV, endpoint, worker de produção ou frontend novos.
Nenhuma ordem, conta, exchange, banco externo, Telegram, push ou deploy.
