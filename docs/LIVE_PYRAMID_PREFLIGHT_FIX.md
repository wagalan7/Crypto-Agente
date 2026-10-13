# Correção local do preflight de reforço LIVE

Base: `0e88f58f`. Escopo autorizado: corrigir o contrato de preflight do
reforço, mantendo flags, limites, alavancagem e posição manual. Nenhuma
validação local autoriza uma ordem real ou comprova lucratividade.

## Causa e correção

`_maybe_pyramid` chamava MARKET sem `entry_preflight`; `place_order` recusava
`EXEC_MARKET_PREFLIGHT_REQUIRED`. Apenas fornecer um callback permissivo seria
incorreto: o caller também ficava fora da reserva de margem e somava a qty
planejada depois de um ACK.

Agora o preflight P04B das entradas normais foi extraído, sem mudar seus
limites ou cálculos, para `_market_entry_preflight_checks`. O reforço o reutiliza
com o stop efetivo e o TP2 **já existentes**, conferidos por IDs exatos,
`closePosition`, tipo, lado e estado vivo. TP1 já realizado não é um novo alvo;
nenhuma proteção é criada, cancelada ou movida por esta correção.

A posição deve ser BOT/auto, aberta, pós-TP1, do mesmo lado/perna BOTH,
com quantidade e alavancagem frescas compatíveis com o registro. Dados
ausentes, bool, não finitos, stale ou ambíguos não autorizam reforço.

Uma intenção oficial é reservada antes do envio. A identidade usa trade/nível
e o `created_at` original, não o candle da nova recomendação. `purpose=PYRAMID`
cabe na coluna existente; o trade/nível está no playbook. O despacho preserva
`cw-pyr-<trade>-<nível>`, registrado na intenção antes do POST. O reforço não
ganha um novo slot. A reserva pendente ainda pode ser contabilizada
conservadoramente pelos consumidores legados de slots até ser vinculada.

O transporte recebe os guards oficiais de preparação, readmissão e autorização
final. Depth, filtros, spread, slippage, chase, geometria, risco, margem real,
funding e ownership continuam podendo negar o envio. Os tetos nominais já
configurados são conferidos sobre exposição combinada; após o depth usa-se o
maior preço aprovado para notional/margem. Esses checks nominais são leituras
de portfólio, não uma nova promessa de serialização de todo limite nominal.
A reserva de risco/margem continua usando a admissão serializada existente.

Só uma resposta terminal, com identidade, preço e quantidade realmente
executados, compatível com a proposta admitida, pode atualizar qty/nível.
Não se presume fill pela qty planejada. O writer usa lock de linha e confere
status, fonte, lado, símbolo, fase, qty e nível antes de somar. O preço original
de referência da gestão não é reescrito pelo caller do reforço.

## Transferência de risco

Sem correção adicional, a confirmação apagaria a reserva enquanto o cálculo
legado continuaria usando o entry original no BE. Exemplo: entry 100, SL 100,
reforço 1 a 110 — risco adicional conhecido 10 não pode virar zero.

O snapshot SQL único pós-advisory-lock e a consulta financeira ORM incluem
uma parcela `risk_only` das intenções PYRAMID confirmadas ligadas ao trade
auto aberto. Essa parcela não soma margem nem contador de slots e sai quando
o trade fecha. A própria intenção é excluída na readmissão.

É um teto **conservador**: permanece até o fechamento mesmo se o SL subir ou
o ledger depois atualizar a média. Pode superestimar risco e barrar entradas;
não é uma alegação de risco exato ou de P&L. Não se aumentou limite algum.

## Limitações deliberadas

- UNKNOWN, fill fora do preço/qty admitidos, conflito ou falha de persistência
  armam latch **antes** de tentar gravar no banco. A reserva não é liberada.
  O reforço exige reconciliação manual; não foi criada uma liquidação automática
  de reforços no P03 nem um segundo POST para contornar incerteza.
- Recusa terminal sem envio encerra a identidade daquele trade/nível. Não foi
  inventado um retry com nova identidade; retomada desse nível é uma pendência
  de contrato, não autorização para reenvio.
- Seleção candidata não recebe por omissão autoridade para ampliar um trade
  legado. O caminho candidato de reforço continua recusado.
- Mudanças diretas na exchange não são atomicamente impedidas pela lock do
  banco. As leituras frescas e os guards não eliminam esse TOCTOU.
- A réplica econômica PYRAMID ainda não tem as novas portas duráveis de
  reserva/proteção/risco. O adapter declara
  `TECHNICAL_PYRAMID_EVENT_PORTS_NOT_IMPLEMENTED`; o estudo completo declara
  `TECHNICAL_PYRAMID_CHAIN_NOT_IMPLEMENTED`. Não se simulou uma autorização
  mais fraca para retirar esses bloqueios.

## Verificação e publicação

Estado final em 13/10/2026: 50 testes focados verdes 2× (41 caller/P04/proteção/
limites/autoridade + 9 financeiros); 21 verificações PostgreSQL16/asyncpg
reais verdes 2×; backend completo 3.109 executados, 3.107 aprovados, apenas
os dois skips históricos R05C por fixture privada ausente. A extração P04
também teve paridade AST contra o corpo original da base `0e88f58f`.
Nenhum skip novo nem fixture financeira real inventada.

O teste financeiro é reproduzível com
`bash backend/tests/run_pg_live_pyramid_risk.sh`. O runner valida e encerra
somente os diretórios descartáveis criados por ele. Logs desta execução:
`/tmp/cw_pyramid_final_focus_1.log`, `/tmp/cw_pyramid_final_focus_2.log`,
`/tmp/cw_pyramid_pg_final_1.log`, `/tmp/cw_pyramid_pg_final_2.log` e
`/tmp/cw_pyramid_backend_frozen.log`.

Correções nos próprios testes/runner: a última fixture passou a chamar de fato
a autoridade final antes do POST falso; o runner recebeu o caminho explícito
de importação do backend. Não foram relaxados guards para fazê-lo passar.

Testes de caller/P04 usam fontes reais e portas falsas; a prova financeira
PostgreSQL usa cluster local descartável, driver real e socket Unix exclusivo.
Nenhuma conta, exchange, banco externo, ordem ou mensagem real foi acessada.
Nenhuma flag, ENV, tabela, coluna, frontend ou alavancagem foi alterada.
Sem push/deploy nesta etapa. Aprovação operacional permanece pendente de
publicação autorizada e observação legítima, sem forçar uma operação.
