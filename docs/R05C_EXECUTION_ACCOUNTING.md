# R05C — contabilização por EXECUÇÕES REAIS

> Corrige a **origem** dos números financeiros das novas operações `auto`.
> Nenhuma linha histórica foi alterada e nenhuma rotina de backfill foi criada.

## Antes

Os números financeiros vinham de estimativas do executor, não das execuções:

- **abertura**: `entry_actual` começava no preço TEÓRICO e podia permanecer
  teórico quando `avgPrice` vinha ausente ou zero; `entry_fee` era `0.0` fixo;
  `qty` continuava a PLANEJADA quando `executedQty` faltava;
- **parcial TP1**: aproximada, e uma entrada real positiva não substituía uma
  entrada teórica positiva;
- **runner**: reduzia `qty`, mas a segunda parcial não era contabilizada;
- **fechamento**: escolhia o *último fill do lado*, sem vínculo exato nem média
  ponderada, e a taxa de saída não era repassada;
- **time-stop**: consultava uma posição já zerada e usava o `entry` como saída;
- **snapshot expirado**: fechava com `snap.outcome_price` e `exit_fee=0.0`;
- `get_executions` perdia `commissionAsset` e `realizedPnl` e convertia comissão
  ausente em zero.

Resultado auditado (7 operações): o app registrava **+1,3896**, enquanto as
execuções reais somavam **−0,81641839** sem funding. O total registrado **não é
oracle** — é justamente o defeito.

## Depois

Uma única origem: os fills confirmados pela exchange.

```
entry_price   = Σ(price × qty) / Σ(qty) dos fills de ENTRADA
qty_initial   = quantidade EXECUTADA da ordem inicial (nunca a planejada)
qty           = exposição restante (a contabilidade nunca a restaura)
exit_price    = média ponderada das saídas ATRIBUÍDAS
gross         = Σ(realizedPnl) dos fills atribuídos
fees_by_asset = comissões de TODOS os fills, uma vez por exec_id
net_trade     = gross − comissões confirmadas na moeda de liquidação
funding_net   = Σ(FUNDING_FEE atribuíveis), preservando o sinal
net_including_funding = net_trade + funding_net   (só com AMBOS conhecidos)
realized_r    = net_trade / (|entry_fill − planned_stop| × qty_initial)
```

`last_exit_price` guarda separadamente o preço da última perna, para quem
realmente precisar dele.

### `pnl_usd` e funding

`pnl_usd` **preserva o contrato legado**: líquido de execuções e comissões,
**EXCLUINDO funding**. O nome é legado — o ativo é registrado como `USDT`, sem
afirmar conversão cambial para USD. Funding e "total incluindo funding" são
campos **distintos** e nunca são somados a `pnl_usd`. A fonte e a semântica
operacional do R05B não mudaram.

`entry_fee` são as comissões da entrada; `exit_fee`, as de **todas** as saídas.
São informativas — `pnl_usd` já as descontou. `tp1_realized_usd` permanece
compatível e informativo: o TP1 **não** é somado de novo nem usado para
esconder o TP2.

### Ausência nunca é zero

| Situação | Resultado |
|---|---|
| comissão ausente | `fees_complete=false`, `net_trade=null` (`FEES_INCOMPLETE`) |
| comissão em BNB/outro ativo | mantida por ativo, `FEE_ASSET_CONVERSION_UNAVAILABLE`; nunca subtraída de USDT |
| funding não consultado / paginação incompleta | `funding_net=null`, `funding_state=PENDING` |
| janela de funding consultada e vazia | zero **provado** |
| nenhum fill atribuído | `NO_ATTRIBUTED_FILL` — conjunto vazio não prova P&L zero |
| fechamento desconhecido | estado `PENDING`/`PARTIAL`; a saída **não** é inventada no entry/mark/stop/TP |

`COMMISSION`, transferências, bônus e rebates **não** viram funding nem PnL:
`COMMISSION` já vem descontada dos fills e somá-la seria desconto duplo.

## Identidade e atribuição

Identidade = exchange + símbolo **com quote exata** (`BTCUSDT` ≠ `BTCUSDC`) +
`positionSide` + `orderId` + `exec_id`. IDs são **strings** — nunca convertidos
por float. `clientOrderId` é fallback de **identidade**, jamais autorização para
reenviar ordem. `algoId` ≠ `orderId`.

Um fill só é atribuído quando pertence a uma **ordem conhecida** da operação —
nunca por coincidência de símbolo/lado/horário. Fill não atribuível ⇒
`AMBIGUOUS`. Quantidade não conservada ⇒ `PARTIAL`. Operações sobrepostas,
reversão ou origem incerta produzem estado explicável, não atribuição arbitrária.

### Origem do fechamento

`source=auto` é preservado mesmo quando a saída foi externa. `clientOrderId` com
prefixo do bot ⇒ `BOT_MANAGED`; qualquer outro (ex.: app de terceiro com prefixo
`ios_`) ⇒ `EXTERNAL_OR_UNKNOWN` — sem concluir autoria humana específica e sem
inventar BE/TP/SL. **Motivo operacional, origem e sinal do resultado são coisas
distintas**: lucro não prova TP; prejuízo não prova execução de SL.

## Schema e estados

Uma única coluna nullable `real_trades.execution_accounting` (JSONB), com
migração **aditiva e idempotente** no mecanismo real do repo
(`ADD COLUMN IF NOT EXISTS`). Sem tabela paralela, sem backfill automático, sem
default que marque legado como confirmado, sem DDL em produção.

```
schema_version · state · reason_code · source · settlement_asset
identity · orders · fills · funding · funding_state
coverage (quantidades, completude de janelas, atribuição, position_flat)
totals (gross, fees_by_asset, net_trade, funding_net, net_including_funding,
        entry_avg_price, exit_avg_price, last_exit_price, entry/exit fee)
realized_r · close_origin · provisional · conflicts
attempts · next_retry_at · last_error · updated_at
```

Estados **contábeis**, separados da máquina de estados operacional:

| Estado | Significado |
|---|---|
| `CONFIRMED` | execuções completas, quantidade conservada, `net_trade` conhecido |
| `PARTIAL` | parte comprovada, parte pendente |
| `PENDING` | ainda sem evidência suficiente |
| `AMBIGUOUS` | fills não exclusivos / origem incerta |
| `CONFLICT` | mesmo `exec_id` com conteúdo materialmente divergente |
| `FAILED` | tentativas esgotadas — visível para revisão |
| `LEGACY_UNVERIFIED` | registro anterior ao R05C |

`NULL` em registro antigo é `LEGACY_UNVERIFIED`, **nunca** confirmação
retroativa. A completude do funding é separada da completude das execuções:
funding pendente **não** apaga um `net_trade` já confirmado.

## Idempotência e concorrência

O merge é transacional, com `SELECT … FOR UPDATE`, e os acumulados são sempre
**recalculados** do conjunto deduplicado — nunca incrementados. O I/O da
exchange acontece **fora** da transação.

- mesmo `exec_id` idêntico ⇒ no-op;
- mesmo `exec_id` materialmente divergente ⇒ `CONFLICT`, preservando o primeiro
  confirmado (sem sobrescrita silenciosa);
- dois workers com respostas diferentes ⇒ nenhum evento perdido, nenhum valor
  duplicado, nenhuma regressão de fonte confirmada para estimativa;
- respostas fora de ordem e reinício ⇒ mesmo resultado.

Ao confirmar o fill real, a entrada provisória é substituída **mesmo sendo
positiva**, e slippage e valores dependentes são recalculados.

Uma chamada legada de `close_trade` **não** sobrescreve contabilidade
confirmada: o fechamento operacional segue normalmente (status, `closed_at`),
mas `exit_price`, `exit_fee`, `pnl_usd` e `realized_r` ficam preservados.

## Coleta

Somente `GET`, reutilizando o transporte assinado, a configuração de conta, a
normalização, o cooldown, o timeout e o rate limit existentes. Nenhum cliente ou
SDK novo.

- `GET /fapi/v1/userTrades` — `symbol` + `startTime`/`endTime`, janelas ≤ 7 dias,
  `limit ≤ 1000`. `fromId` **nunca** é enviado junto com `startTime`/`endTime`.
- `GET /fapi/v1/order` — identidade, quantidade e status das ordens.
- `GET /fapi/v1/income` com `incomeType=FUNDING_FEE`, janelas explícitas e
  paginação.

Uma **página cheia não é prova de completude**: o cursor avança pelo maior
timestamp da página e preserva fills no mesmo milissegundo; se o cursor não
avança, a janela é declarada **incompleta**. Sem evidência completa ⇒
`PENDING`/`PARTIAL`. Não se busca todo o histórico da conta.

Orçamento de **12 chamadas por operação**, backoff **persistido**
(60 s → 6 h) e no máximo **6 tentativas**; a exaustão vira `FAILED`, visível
para revisão, sem retry infinito.

`avgPrice=0` ou status de envio incerto **nunca** motivam uma segunda entrada —
ACK não é fill; a reconciliação P03 continua sendo o caminho para execução/lado
incertos.

## Integração

- **Abertura**: todo trade `auto` **nasce** no contrato R05C, em `PENDING`, sem
  nenhuma consulta à exchange — a semente é gravada na mesma transação do
  `open_trade`.
- **Fechamento**: `close_trade` apenas libera a reconciliação (sem I/O).
- **Reconciliação**: roda no **fim** do `_tick` já existente do trade manager,
  **depois** da proteção das posições abertas, em lote de no máximo **5**,
  selecionando somente registros aderentes ao schema novo e ainda pendentes.
  Registros legados nunca são varridos. **Nenhum loop, worker, fila, scheduler
  ou endpoint novo.**

Proteção primeiro, contabilidade depois: nenhuma consulta financeira fica entre
a entrada e o SL, e nenhuma falha contábil atrasa colocação de SL, fechamento de
emergência, renovação de lease ou limpeza P02/P03. A atualização contábil é
idempotente e **não** envia mensagem de fechamento nem reexecuta reconciliação
de outcomes/snapshots.

## Leitores

O R05A passou a distinguir **"valor registrado"** de **"execução
reconciliada"**: `execution_reconciled`, `execution_reconciled_pct`,
`execution_states`, `legacy_unverified`, `fees_reconciled` e
`funding_reconciled_usd` (separado, nunca somado ao P&L). `fees_complete` só é
`true` quando **todas** as linhas têm taxa presente **e** execução reconciliada
— nunca por causa de `entry_fee`/`exit_fee` iguais a `0.0` por default.

Valores históricos são preservados **como registrados**, explicitamente não
verificados, sem regravação. O R05B continua com sua fonte, flags, limites e
política inalterados — **não há cutover neste pacote**.

## Limites conhecidos

1. Comissão em ativo diferente do de liquidação não é convertida: fica por
   ativo, com `FEE_ASSET_CONVERSION_UNAVAILABLE`, e `net_trade` permanece `null`.
2. Funding é atribuído por símbolo/janela da operação; com exposição sobreposta
   no mesmo símbolo a atribuição não é demonstrável e fica pendente.
3. As 7 operações históricas **não** foram corrigidas: seguem como registradas
   e `LEGACY_UNVERIFIED`.
4. `position_flat` com contabilidade `PENDING` é um estado legítimo — o trade é
   operacionalmente fechado, a contabilidade continua aberta.

## Migração e rollback

Migração: `ALTER TABLE real_trades ADD COLUMN IF NOT EXISTS
execution_accounting JSONB` — aditiva, idempotente, sem default e sem DDL
manual em produção.

Rollback **sem apagar evidência**: basta parar de popular a coluna (reverter os
serviços). A coluna permanece com o que já foi confirmado; nenhum leitor exige
sua presença, e `NULL` continua significando `LEGACY_UNVERIFIED`. Não é
necessário — nem recomendado — dropar a coluna: isso destruiria a única prova
das execuções reais já reconciliadas.

## Verificação

- **7 casos auditados + totais** reproduzidos pelo núcleo real, tolerância
  `0.00000001` USDT (execução local contra a fixture privada, não versionada).
- **Fixture sintética equivalente** versionada em
  `backend/tests/fixtures/r05c_synthetic_cases.json`: mesmos cenários, com
  valores, IDs, símbolos e datas distintos; nenhum dado da conta.
- **PostgreSQL real descartável** (`tests/run_pg_r05c.sh`): migração idempotente,
  merge com bloqueio de linha, concorrência entre dois workers, conflito de
  `exec_id`, guarda do `close_trade` legado e pendente que não vira zero.
