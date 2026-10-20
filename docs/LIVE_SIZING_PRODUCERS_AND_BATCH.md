# Produtores nominais e contexto filler — réplica local

15/10/2026, base `1a6a4ded`, checkout principal autorizado. Somente pesquisa;
nenhum caller LIVE, flag, limite, histórico ou posição manual foi alterado.
Esta etapa **não conclui a réplica integral** solicitada para o primeiro estudo.

## 1. Produtores nominais — código original, não fórmula duplicada

Contrato `CW_LIVE_NOMINAL_SIZING_REQUEST_V1` → `_compute_leverage` real →
`_apply_btc_correlation_throttle` real quando o caller o usa →
`CW_LIVE_NOMINAL_SIZING_REPORT_V1`.

O tier é o final observado, após os ajustes/classificações do scanner. A lista
de entrada é inteira, pós-build e anterior ao throttle/sort/portfolio guard;
uma moeda removida pelo guard posteriormente ainda conta para a correlação.
O serviço valida na AST os callers, a chamada original e a cópia dos quatro
campos para Recommendation. Não aceita risco/alavancagem pré-calculados.

- `get_recommendations_from_batch` e `get_recommendations_via_vision` aplicam
  throttle; `get_recommendations` legado não o aplica. Não existe booleano
  livre para escolher o comportamento de um caller.
- Preservados os perfis por tier, caps, margem, distância e `round` do Python.
  Cada replay reconstrói o batch frio: warning deduplicado não implica que
  multiplicar risco duas vezes seria idempotente.
- `is_btc_symbol` é a fonte real, inclusive BTC/ETH e sua distinção atual
  entre símbolo com separador e colado. Não foi corrigida silenciosamente.
- Regime causal, fonte e geração são explícitos; indisponibilidade não vira
  risco zero nem regime neutro conhecido. O contrato conservador exige regime
  capturado para publicar COMPLETE_NOMINAL. A origem completa do batch não é
  certificada por hash: continua sendo uma entrada observada.

Subprocesso isolado `-I -B`, ambiente vazio, sem app/clientes/DB/rede. O
verificador também reexecuta as funções reais em namespace restrito: refazer
o hash de números ou warnings adulterados não satisfaz o contrato.

## 2. Estado filler — trechos nativos, sem substituir persistência por POST

Contrato `CW_LIVE_FILLER_BATCH_REQUEST_V1`/`REPORT_V1`. Executa somente os
trechos puros originais de `open_shadow_for_recs`, selecionados por AST:

1. Ordenação estável DENTRO antes de FORA, sem ordenar internamente por ID.
2. Gate allowlist/capacidade/freio e classificação `_is_filler`.
3. Incremento de opened/dentro/fora/test_done após retorno de persistência,
   respeitando o `continue` anterior ao incremento no caso de incidente.

Contagens, allowlist efetiva, brake e início do teste têm fontes/gerações e
relógios causais. São **saídas observadas dos leitores**, não SELECTs ou
escritores reproduzidos. `TEST_N=0` não lê/exige contador; começa em zero como
o caller. Configurações inativas não tornam leitura ausente em zero financeiro.

O histórico é um prefixo na ordem efetiva do lote. Cada retorno tem identidade
de oportunidade/símbolo/lado, client_order_id, trade id e dispatch vinculados;
reuso de identidade, duplicação ou inversão temporal é recusado. Reexecutar o
mesmo prefixo reconstrói o mesmo estado, sem incremento repetido.

`OPENED` exige retorno de trade auto/open, sem incidente. `NO_ENTRY` não
incrementa. Incidente não incrementa; UNKNOWN/fonte indisponível não fornece
contexto à próxima oportunidade. O tamanho consome test_done **antes** da
abertura; a próxima oportunidade recebe o contador atualizado.

Um POST/fill/manager seed não é usado como prova de persistência. O retorno
de `_open_trade_fail_closed` é uma observação explícita e vinculada. Qualidade
sintética/modelada não vira verdade de banco ou corretora. A política publica
`trade_persistence_replayed=false` e
`completion_scope=OBSERVED_RETURN_BOUND_TRANSITION`. O trecho de log crítico
recebe uma sentinela apenas de formatação (`NOT_EXPORTED`), nunca qty zero
econômica inventada. Não há DDL/worker/scheduler/loop de produção novo.

## 3. Ligação ao replay já existente

Os envelopes opcionais `nominal_producer` e `filler_batch` entram no pedido
upstream, seu hash de origem e o relatório `producer_refs`. Ausência mantém
o contrato anterior de inputs observados — não afirma produtores reproduzidos.
Uma vez fornecidos, não podem ser ignorados, parcializados ou substituídos.

Validação reconcilia oportunidade, símbolos exatos, preços, direção, tier,
risco/alavancagem, configuração, batch id e estado do contador. O filler só
pode consumir membros realmente presentes no relatório nominal. Ordem causal:
nominal → filler → sizing; conclusões do histórico filler não podem preceder
a disponibilidade da produção nominal. Filtro/ordem pós-portfolio continuam
observados, sem inventar a cadeia do scanner.

Fluxo provado: produtores → sizing/caps/canário nativos → admissão/POST e fills
sintéticos → gestão/ledger → estudo dos dois braços → persistência oficial →
catálogo DRAFT/restart. Quantidade e alavancagem conferidas, sem segundo canário.
GET não executa produtores, replay ou escrita. O PG verifica persistência da
pesquisa, não a existência de SL na conta real.

## 4. O que ainda impede chamar o lote integral de concluído

- Geração original de scores/tiers/calibração/elegibilidade, escolha/ordem
  final do scanner e ramos operacionais de posição existente não são
  reproduzidos por estes dois contratos.
- Escritores/refresh de learning/decay, geração do brake e contagens iniciais
  permanecem observados. Ler cache causal não prova a origem de seu treino.
- Export frio deve preservar excluídos/purgados e ordinais causais prospectivos;
  o histórico não receberá ordinais inventados.
- Caixa, saldo Binance e funding/P&L intratrade integrais seguem técnicos.

Esses itens são código/contrato/evidência por fechar, não apenas espera de
amostra. Nenhuma alegação de lucro, redução de stops, aprovação econômica,
SHADOW/CANARY ou equivalência integral. Quatro autoridades sempre falsas.

Contagens/logs finais e retomada em `LIVE_EVENT_STUDY_CHECKPOINT.md`. Sem conta,
banco externo, exchange, ordem ou mensagem real; sem ativação, push ou deploy.
