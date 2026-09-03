# R05B — consolidação financeira dos circuit breakers

> Cutover **reversível** por flag única. Default obrigatório: **desligado**.

## Antes

- `risk_service` calculava DD diário/semanal por **snapshot teórico**
  (`realized_r × risk_pct` de `RecommendationSnapshot`) — resultado de setup,
  não dinheiro realizado;
- o kill switch diário somava `RealTrade.pnl_usd` **misturando todas as
  sources** (`auto`, `managed`, `manual`, `bybit`, …);
- o **risco ainda aberto** não participava do limite diário: uma posição viva a
  caminho do stop não contava para bloquear uma nova entrada.

## Depois — com a flag ligada

- fonte única: `RealTrade(source="auto")` · `pnl_usd` finito · janela por
  `closed_at`;
- **equity real validada** pelo contrato existente `exchange_service.get_equity()`;
- **risco aberto conhecido** até o stop confirmado;
- **risco da entrada proposta** entra no pior cenário antes do POST;
- **fail-closed** em qualquer incerteza;
- **rollback pela flag**, sem migration nem edição manual no banco.

## Núcleo financeiro único

`backend/services/financial_risk_service.py` alimenta `risk_service`,
`kill_switch_service`, `/api/risk/status` e `/api/kill-switch/status` — **não
existe fórmula duplicada entre consumidores**.

Ele **importa** as validações e fórmulas puras já aprovadas no R05A
(`_finite`, `_as_utc`, `_bump`, `_pct`, `real_cohorts`, `open_risk`) em vez de
copiá-las. A resposta pública do R05A permanece exatamente a mesma.

## Fonte e coortes

Somente `source == "auto"`, status fechado — inclusive `closed_manual` quando o
source continua `auto`. `closed_at` nunca é substituído por `opened_at`. Só
`pnl_usd` finito entra; zero financeiro legítimo continua zero. `None`, NaN,
infinito, timestamp inválido e linha inconsistente são **excluídos e
contabilizados**.

`managed`, `manual`, `bybit` e `other` **nunca** são misturados. Não existe
fallback para `RecommendationSnapshot`, para `RealTrade.realized_r` nem para
inferência de lucro pelo status (verificado por teste sobre o código).

### Taxas, TP1, slippage

`pnl_usd` já inclui as taxas persistidas e o parcial de TP1: **não** se desconta
`entry_fee`/`exit_fee` de novo, **não** se soma `tp1_realized_usd` de novo, e o
slippage já está refletido no fill. Rótulo obrigatório:
**`RECORDED_NET_EX_FUNDING`**.

### Funding

Permanece `value=null` / `reason_code="FUNDING_FIELD_UNAVAILABLE"`. Não é
estimado, não é assumido zero, não há integração improvisada com income history.
A ausência é estrutural e aparece no contrato — **sem** fabricar divergência por
trade. O resultado **não** é "P&L completo da exchange".

## Janelas

| Bloco | Tipo | Início |
|---|---|---|
| circuit breaker diário | `rolling` | `as_of − 24h` |
| circuit breaker semanal | `rolling` | `as_of − 7d` |
| kill switch diário | `calendar` | dia UTC, respeitando `KILL_DAILY_RESET_AT` |
| loss streak | `rolling` | `KILL_COOLDOWN_HOURS`, ancorado no reset diário |

Cada bloco expõe `since_utc`, `until_utc`, `window_kind`, `financial_source`,
`valid_count`, `excluded_count`, `excluded_by_reason` e `data_complete`. Rolling
e calendário nunca se misturam sem identificação.

Uma leitura usa **um único `as_of_utc`** e no máximo: 1 SELECT de fechamentos
`auto` da maior janela, 1 SELECT de posições `auto` abertas e 1 chamada ao
contrato de equity (com o cache oficial dele). Colunas explícitas, sem N+1
(verificado por AST). Um cache curto de 15 s protege `/api/risk/status` — é
proteção de frequência, não uma segunda fórmula.

## Equity

Válida somente com resposta em dicionário, `ok is True`, `total_usd` finito e
> 0, `source != "fallback"` e `age_sec` dentro do TTL oficial do serviço.

```
financial_dd_pct = financial_pnl_usd / total_equity_usd * 100
```

Sem saldo estático nem snapshot como denominador. Ausente, stale, fallback,
inválida ou exceção ⇒ qualidade `UNKNOWN`: não grava DD zero, não sobrescreve o
último valor persistido, não executa auto-resume e — com o modo financeiro
ativo — bloqueia nova exposição.

## Flag

`R05_FINANCIAL_BREAKER_ENABLED`, **default `false`**, única env criada. Qualquer
valor não reconhecido é `false`.

- **`false`**: comportamento operacional legado permanece; o núcleo aparece só
  como diagnóstico; nenhum bloqueio novo; limites atuais inalterados.
- **`true`**: `risk_service` e `kill_switch_service` usam o mesmo núcleo;
  snapshots deixam de acionar circuit breaker; toda falha essencial é
  fail-closed.
- **rollback para `false`**: restaura o legado imediatamente, sem migration nem
  edição manual no banco.

Nenhum threshold novo: `DAILY_DD_LIMIT_PCT`, `WEEKLY_DD_LIMIT_PCT`,
`KILL_MAX_DAILY_LOSS_PCT` e `KILL_MAX_DAILY_LOSS_USD` continuam sendo os limites.

## Risk service

Com a flag ligada, `daily_dd_pct` vem do P&L financeiro `auto` de 24h e
`weekly_dd_pct` de 7d; `daily_trades`/`weekly_trades` contam apenas linhas
financeiras válidas; o motivo da pausa identifica **"P&L financeiro registrado"**.

Eventos continuam em `RiskEvent`. Nenhuma state machine nova, nenhum singleton
novo, nenhuma tabela nova.

Qualidade incompleta: não fabrica zero, **não libera pausa automática**, não cria
pausa a partir de número velho, e preserva os valores anteriores como
`last_confirmed` marcado `stale`. O bloqueio de nova exposição fica com o kill
switch.

Preservados integralmente: advisory lock P03, incidente P03 aberto ⇒ pausa,
pausa manual, ownership P03, rollover diário/semanal e histórico de eventos. Uma
pausa R05B **nunca** libera pausa manual, P03 ou de outro owner.

## Kill switch

Com a flag ligada, o P&L diário considera somente `source="auto"` (com
`closed_manual` de `auto` incluído) e o streak usa apenas `pnl_usd` finito de
`auto` — sem fallback para `realized_r` nem para o status `closed_stop`.

Regra do streak: `pnl_usd < 0` é loss; `>= 0` quebra o streak. **TP1 positivo
não transforma um P&L final negativo em win.** `pnl_usd` ausente ou inválido ⇒
`UNKNOWN` e fail-closed.

Preservados sem alteração conceitual: kill switch manual, máximo de posições
abertas, máximo de entradas diárias, cooldown e o Telegram existente com sua
deduplicação. Os checks não financeiros continuam conservadores sobre a conta
inteira; nenhum cap foi afrouxado.

## Risco aberto

Contrato aprovado no R05A, coorte `auto`: `side` exatamente `long`/`short`,
entry e qty finitos e positivos, `sl_order_id` string não vazia,
`sl_current_price` prioritário, `planned_stop` só como fallback contabilizado e
quantidade **restante** (`qty`), nunca `qty_initial`.

```
LONG   price_risk_usd = max(entry_price - stop_price, 0) * qty
SHORT  price_risk_usd = max(stop_price - entry_price, 0) * qty
```

A `entry_fee` já registrada da posição aberta entra no pior cenário — ela ainda
não está dentro do `pnl_usd` de um fechamento. Zero posições `auto` é risco
**conhecido zero**; posição com risco desconhecido ⇒ `open_risk_complete=false`,
cenário agregado `null` e bloqueio de nova exposição com a flag ligada. Risco
zero nunca é assumido.

## Pior cenário

```
worst_case_daily_usd = realized_daily_pnl_usd
                     - open_price_risk_usd
                     - open_entry_fees_usd
                     - proposed_trade_risk_usd
```

```
LONG   proposed_trade_risk_usd = max(final_entry - stop, 0) * final_qty
SHORT  proposed_trade_risk_usd = max(stop - final_entry, 0) * final_qty
```

Atingir **ou** ultrapassar o limite diário bloqueia **antes do POST**, com reason
code estável `FINANCIAL_WORST_CASE_LIMIT`. São rejeitados como `UNKNOWN`: side
inválido, entry/stop/qty ausentes, NaN/infinito, stop estruturalmente
incompatível com o lado, risco negativo e resposta financeira inválida.

## Preflight

Integrado ao preflight **P04 já existente** — nenhum preflight paralelo.

- **MAKER**: usa `final_limit_price` e `final_qty`; o check financeiro roda
  depois das validações puras, e o POST continua sendo a próxima mutação.
- **MARKET/fallback**: usa a qty final reduzida e o preço conservador aprovado
  pelo depth; o check roda depois do depth e antes do POST. Cada retry/fallback
  revalida.

Se o check negar, lançar ou devolver contrato inválido: **zero POST, zero
entry**, com reason code explicável. MAKER e fallback MARKET continuam
desligados — apenas a compatibilidade segura foi mantida. Pyramiding, hedge e TF
upgrade seguem desligados e não foram ampliados.

## API

Nenhum endpoint novo. Os contratos existentes foram **expandidos de forma
backward-compatible**.

`GET /api/risk/status` acrescenta `metric_source`, `cutover_enabled`,
`financial_quality`, `financial_reason_code`, `financial_as_of_utc`,
`daily_pnl_usd`, `weekly_pnl_usd`, `equity_usd`, `equity_source`,
`open_risk_usd`, `open_risk_complete`, `funding` e `last_confirmed` — mantendo
`daily_dd_pct`, `weekly_dd_pct`, `daily_trades`, `weekly_trades`, limites, pausa
e ownership.

`GET /api/kill-switch/status` expõe o **mesmo** snapshot financeiro, sem fórmula
independente.

`current_control_sources()` do R05A passou a declarar corretamente se o cutover
está ligado; as demais métricas do relatório R05A não mudaram.

O frontend **não** foi alterado: ele já consome os campos persistidos de
`/api/risk/status`.

## Fail-closed

Com a flag ligada, bloqueiam nova exposição: falha de banco, P&L `auto` ausente
ou inválido, equity inválida/stale/fallback, risco aberto incompleto, cenário
não calculável, payload financeiro inválido, exceção no check e divergência
interna de unidade ou sinal.

Nenhuma falha vira P&L zero, risco zero, equity zero válida, `allowed=true` ou
auto-resume.

A divergência entre snapshot teórico e execução real do R05A continua
**diagnóstica** — não bloqueia sozinha, porque snapshots não são verdade
financeira.

## Limitações

1. `RECORDED_NET_EX_FUNDING`: líquido das taxas persistidas, **sem funding
   reconciliado**.
2. Funding continua estruturalmente indisponível e nunca é estimado.
3. O DD percentual depende de equity válida; sem ela a métrica é `UNKNOWN`, não
   zero.
4. O pior cenário é projeção conservadora até o stop confirmado.
5. O cutover nasce desligado: em produção o comportamento legado só muda por
   decisão explícita.

## Invariantes

- Estratégia, score, indicadores, gates de sinal, stop, TP e sizing inalterados.
- Nenhuma ordem criada, cancelada ou modificada; nenhuma chamada nova à exchange
  além do contrato de equity já existente.
- Nenhuma tabela, coluna ou migration; **uma única** env nova, com default
  `false`.
- MAKER, fallback MARKET, TF upgrade e pyramiding continuam desligados.
- P03, pausa manual e ownership preservados; R05B nunca libera pausa de outro
  owner.
