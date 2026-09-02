# R05A — reconciliação observacional de P&L e risco real (SOMENTE LEITURA)

> Esta fase corresponde ao **P05 ORIGINAL do roadmap** (financeiro), não ao
> P05.x de estratégia.

## Objetivo

Mostrar, de forma honesta, a diferença entre quatro visões que hoje **não são a
mesma coisa**:

1. **risco teórico atual** — `RecommendationSnapshot.realized_r × risk_pct`;
2. **resultado financeiro registrado** — `RealTrade.pnl_usd`;
3. **resultado normalizado** — o P&L real convertido em R pela distância de
   risco real registrada, comparável ao R do snapshot;
4. **risco ainda aberto** até o stop confirmado.

O relatório revela divergências e lacunas de cobertura. **Não** altera qual
fonte aciona hoje.

## Antes

- `risk_service` calcula DD diário/semanal somando
  `realized_r × risk_pct` de `RecommendationSnapshot`;
- o kill switch diário soma `RealTrade.pnl_usd` de trades fechados no dia,
  **sem filtrar `source`**;
- as duas fontes nunca foram confrontadas.

## Depois

- relatório observacional confronta as fontes lado a lado;
- **nenhuma fonte operacional foi trocada**;
- funding continua indisponível e é declarado como tal;
- **R05B continua obrigatório** para qualquer consolidação.

## Janelas

Um único `as_of_utc`, timezone-aware e **injetável nos testes**. Carga única de
7 dias; 24h é particionada **em memória** (sem repetir consulta).

| Fonte | Campo de janela |
|---|---|
| SHADOW / teórico | `outcome_at` |
| REAL / financeiro | `closed_at` |
| comparação pareada | `RealTrade.closed_at` |

`opened_at` **nunca** é usado como fechamento. Timestamp naive é tratado
explicitamente como UTC; inválido é excluído com motivo. As bordas têm testes
determinísticos.

## Teórico (`RecommendationSnapshot`)

Quantidade, soma de `realized_r`, soma de `realized_r × risk_pct`, contagem de
wins/losses/neutros e exclusões por motivo. `realized_r=None`, `risk_pct=None`,
NaN/infinito e timestamp inválido **nunca viram zero**.

> `RecommendationSnapshot` é resultado de **setup/recomendação para pesquisa**,
> não dinheiro realizado — e não é usado como verdade financeira aqui.

## Financeiro (`RealTrade`), separado por `source`

Coortes **nunca somadas silenciosamente**: `auto` (primária), `managed`,
`manual`, `bybit`, `other` e um **total legado** explícito com todas as fontes.

Por coorte e janela: trades fechados válidos, `recorded_net_pnl_usd`, positivos,
negativos e zero, somas informativas de `entry_fee` e `exit_fee`, quantidade com
TP1 parcial, `closed_manual`, sem `recommendation_id`, exclusões por motivo,
cobertura de `pnl_usd` e cobertura de vínculo com a recomendação.

**Regras financeiras**

- `RealTrade.pnl_usd` já inclui as taxas registradas e `tp1_realized_usd`;
- taxas **não** são descontadas de novo; TP1 **não** é somado de novo;
- slippage já está refletido no fill/P&L — só a cobertura é reportada;
- funding não tem campo financeiro confiável hoje ⇒ **`UNAVAILABLE`**, nunca
  estimado;
- nada é inventado e a exchange não é consultada para completar dados.

Rótulo honesto: *"P&L líquido registrado, incluindo taxas persistidas; funding
não reconciliado"*.

## Pareamento

Apenas `RealTrade(source="auto")` fechado com **exatamente um**
`RecommendationSnapshot` ligado por `recommendation_id`.

Um par só é comparável com `pnl_usd` finito, `entry_price`, `planned_stop` e
`qty_initial` válidos (`qty` só como fallback **identificado**), `risk_pct`
finito e positivo, `realized_r` do snapshot finito e identidade única.

```
risk_dollar                   = abs(entry_price - planned_stop) × qty_initial
financial_r_from_pnl          = pnl_usd / risk_dollar
theoretical_bank_pct          = snapshot.realized_r × snapshot.risk_pct
financial_bank_pct_normalized = financial_r_from_pnl × snapshot.risk_pct
delta_r                       = financial_r_from_pnl - snapshot.realized_r
delta_bank_pct                = financial_bank_pct_normalized - theoretical_bank_pct
```

`RealTrade.realized_r`, quando existe, é confrontado **apenas** para detectar
inconsistência interna — **nunca** substitui o cálculo por `pnl_usd`.
`risk_dollar <= 0` invalida o par. Mais de um RealTrade para a mesma
recomendação ⇒ **`AMBIGUOUS_REAL_LINK`**: excluído da comparação, sem escolha
arbitrária, mantendo os P&Ls na coorte real.

Saída: pares elegíveis, comparáveis, cobertura, delta médio/mediano em R, delta
agregado normalizado, divergências por motivo, contagem de sinais divergentes.
**Nenhuma lista com dados individuais.**

| Classificação | Quando |
|---|---|
| `NOT_COMPARABLE` | cobertura insuficiente ou nenhum par |
| `ALIGNED` | cobertura suficiente e diferença dentro da tolerância |
| `DIVERGENT` | cobertura suficiente e diferença acima da tolerância |
| `UNAVAILABLE` | erro ou contrato inválido |

Constantes fixas, sem ENV: cobertura mínima **80%**, tolerância absoluta
**0,10 ponto percentual** no agregado normalizado. **A tolerância é diagnóstica
e não aciona trava.**

## Risco aberto

Consulta separada de `status="open"`, com `auto`, `managed` e demais fontes
separadas. Stop só conta como confirmado com `sl_order_id` **e** preço válido —
`sl_current_price` preferido, `planned_stop` apenas como fallback identificado.
Usa a quantidade **restante** (`qty`), nunca `qty_initial`.

```
LONG   remaining_price_risk_usd = max(entry_price - stop_price, 0) × qty
SHORT  remaining_price_risk_usd = max(stop_price - entry_price, 0) × qty
```

Stop já em lucro ⇒ risco de preço **zero**, nunca negativo.

Reportados separadamente: subtotal conhecido, taxas de entrada registradas,
posições com e sem stop confirmado, posições com dados inválidos, cobertura e
`open_risk_complete`.

Se **qualquer** posição da coorte primária `auto` tiver risco desconhecido,
`open_risk_complete=false` e o cenário agregado é `None` — risco zero **não** é
assumido. Só com toda a exposição `auto` conhecida:

```
realized_24h_plus_open_stop_scenario_usd
    = recorded_net_pnl_usd_auto_24h - remaining_price_risk_usd_auto
```

Projeção conservadora, **não previsão**, e não aciona nada.

## Fontes de controle declaradas

```
risk_service_daily_weekly.source = RecommendationSnapshot
risk_service_daily_weekly.unit   = percent_from_realized_r
kill_switch_daily.source         = RealTrade
kill_switch_daily.scope          = all_sources_currently
kill_switch_daily.unit           = recorded_pnl_usd
unified_financial_source     = false
authoritative_source_changed = false
enforcement_changed          = false
observation_only             = true
```

Conferido no código: `risk_service._compute_window_dd` soma
`realized_r * risk_pct` de `RecommendationSnapshot`;
`kill_switch_service._daily_pnl_usd` soma `RealTrade.pnl_usd` de trades fechados
no dia **sem filtro de `source`**.

## Contrato de saída

`ok` · `phase="R05A"` · `mode="OBSERVATION_ONLY"` · `as_of_utc` ·
`windows["24h"]` · `windows["7d"]` · `open_risk` · `paired_reconciliation` ·
`current_control_sources` · `data_quality` · `limitations` · `invariants`.

Métrica indisponível devolve valor `None` + `reason_code` + explicação curta.
**Nunca** NaN, infinito ou `[object Object]`. Falha em uma coorte não fabrica
zero nem derruba as demais.

## API

`GET /api/risk/reconciliation` — somente leitura, protegido pelo
`_check_admin_token` existente (header `X-Admin-Token`). Sem POST, sem parâmetro
que altere estado, sem refresh de exchange, sem `retry-now`, sem troca de fonte,
sem apply/promote/activate. Erro é fail-soft com `ok=false`, `phase=R05A`,
`mode=OBSERVATION_ONLY`, sem stack trace, ID de ordem ou dado individual.

Não entra em scan loop, digest, `/api/risk/status` nem em qualquer endpoint de
alta frequência. Admin, sob demanda — sem cache nesta fase.

## Desempenho

Máximo de **três** consultas por chamada: RealTrades fechados (7d), snapshots
relacionados/na janela e RealTrades abertos. Colunas explícitas, sem carregar
objetos ORM completos, sem N+1 (verificado por AST nos testes).

## Limitações

1. `RecommendationSnapshot` é pesquisa de setup, não dinheiro realizado.
2. O P&L é **líquido registrado**: inclui as taxas persistidas e o parcial de
   TP1, mas **funding não está reconciliado**.
3. Funding não tem campo financeiro confiável — `UNAVAILABLE`, nunca estimado.
4. Slippage já está no fill/P&L; aqui só a cobertura aparece.
5. O cenário de risco aberto é projeção conservadora, não previsão.
6. A tolerância de alinhamento é diagnóstica e não aciona trava.

## Invariantes

- Observação pura: nenhuma fonte operacional foi trocada.
- `RiskState`, circuit breakers, pausas e limites inalterados.
- Somente `SELECT`: zero escrita, rede, exchange, Telegram ou push.
- Nenhuma ordem criada, cancelada ou modificada.
- Nenhuma tabela, coluna, migration, ENV ou flag criada.
- Frontend fora deste pacote; arquivos congelados intactos.
- **R05B continua obrigatório** para qualquer consolidação de fontes.

## Fechamento da auditoria independente

- A classificação de alinhamento agora valida o agregado e cada par:
  divergências opostas ou de sinal não podem se cancelar e produzir um falso
  `ALIGNED`.
- O pareamento rejeita `side` inválido e stop estrutural incompatível com o lado.
- `risk_pct` zero ou negativo é dado inválido, nunca contribuição financeira.
- Taxas ausentes permanecem `null`; somas parciais carregam cobertura explícita.
- IDs de stop malformados não comprovam proteção e timestamps são normalizados
  para UTC.
