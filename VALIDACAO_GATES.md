# Validação dos gates novos — retomar daqui

> Nota operacional (não é doc de produto). Criada a pedido do usuário pra
> retomar a validação dos 3 itens entregues em 13/06. Ao reabrir: ler isto,
> checar produção, decidir se afrouxa/aperta cada gate.

## O que foi entregue hoje (13/06)

| Item | Commit | Tipo |
|---|---|---|
| Gate de liquidez (Fase 2) | `8e766b1c` | backend |
| Gate de P(TP1) calibrado | `8e766b1c` | backend |
| Widget do teste 0.50 no painel | `78ef77d3` | frontend |
| (contexto) RR gate | `8bed8378` | backend |
| (contexto) contador teste 0.50 + fix slippage | `b8d76b88` | backend |

Arquivo dos gates: `backend/services/shadow_trade_service.py`.

## Checklist de validação (quando o usuário chamar)

1. **Ver os skips reais por gate** — endpoint de motivos de skip
   (`_record_skip` → API). Tags pra procurar: `liquidity-gate`, `prob-gate`,
   `rr-gate`. Pergunta-chave: **algum gate está secando trade bom?**
2. **Cruzar com o contador do teste 0.50** (`/api/live-test/status`): se a
   contagem travou (poucos auto-trades desde 13/06 12:00 UTC), suspeitar de
   gate apertado demais.
3. **Olhar slippage real** dos auto-trades novos (`/api/real-trades?days=30`,
   `entry_slippage_pct`) pra confirmar se o gate de liquidez está de fato
   reduzindo slippage nas moedas ilíquidas.
4. **Confirmar P(TP1) gate não nasceu no-op cego**: se a calibração já está
   madura, ele deve aparecer em `prob-gate` skips; se nunca aparece, conferir
   se `rec.prob_tp1` está vindo populado.

## Env knobs — afrouxar / apertar sem deploy (Railway)

| Gate | Env | Default | Afrouxar | Apertar |
|---|---|---|---|---|
| Liquidez — volume | `MIN_QUOTE_VOL_24H_USD` | `10000000` ($10M) | baixar p/ `5000000` | subir p/ `25000000` |
| Liquidez — spread | `MAX_SPREAD_PCT` | `0.25` | subir p/ `0.40` | baixar p/ `0.15` |
| Liquidez — liga/desliga | `LIQUIDITY_GATE_ENABLED` | `true` | `false` | — |
| P(TP1) | `MIN_PROB_TP1_EXEC` | `0.45` | baixar p/ `0.40` | subir p/ `0.50` |
| P(TP1) — liga/desliga | `PROB_TP1_GATE_ENABLED` | `true` | `false` | — |
| R:R TP1 | `MIN_RR_TP1_EXEC` | `0.7` | baixar p/ `0.5` | subir p/ `1.0` |
| R:R TP2 | `MIN_RR_TP2_EXEC` | `1.5` | baixar p/ `1.2` | subir p/ `1.8` |
| R:R — liga/desliga | `RR_GATE_ENABLED` | `true` | `false` | — |

> Pôr env = 0 desliga o piso/teto específico sem desligar o gate inteiro.

## Veredito do bot no menu "trades recomendadas" (14/06)

Os MESMOS gates de qualidade (R:R, P(TP1), liquidez) agora vetam cada
recomendação exibida no app — **fonte única**: a função read-only
`exec_verdict(rec)` em `shadow_trade_service.py` reaproveita os limites do loop
(RR/PROB/LIQUIDITY). **NÃO toca no loop de execução real** (zero risco de
dinheiro). O `recommendation_service` anexa `bot_verdict` a cada rec usando o
ticker JÁ buscado na varredura (sem chamada externa extra → `quote_vol_usd` /
`spread_pct` agora vivem na rec).

- **Recs do bot**: selo `⛔ BOT NÃO OPERA · <gate>` agora vem de skip-reasons
  (cobre todos os gates do loop) **OU** do `bot_verdict` (R:R/P(TP1)/liquidez,
  sempre fresco, não reseta no redeploy) — skip-reasons tem prioridade.
- **Recs de observação**: ganharam selo de qualidade `✅ critério do bot` /
  `⚠ <gate>` — diz se a indicação do universo amplo atende o padrão do bot.

Validação quando reabrir: conferir no app se observação ilíquida aparece com
`⚠ liquidez baixa` e se rec do bot barrada bate com o motivo em
`/api/shadow/skip-reasons`. Mesmos env knobs da tabela acima controlam isso
(é a mesma lógica). Se o veredito divergir do skip real do bot, é bug de
fonte-única — investigar `exec_verdict`.

## BE estrutural pós-TP1 (Opção B — 14/06)

`trade_manager_service.py` → `_structural_be_stop()`. Antes: ao bater TP1 o SL ia
pro **BE exato** (= entry) e o reteste normal pós-TP1 estopava em BE antes do TP2.
Agora ancora o SL na **estrutura** (swing logo abaixo/acima do entry) com folga de
ATR. Seguro por construção: clamp `[floor, entry]`, give-back limitado pelo que a
parcial do TP1 cobre → **pior caso ≥ breakeven agregado** (matematicamente não vira
negativo). Fallback total pro BE exato se faltar estrutura/dado.

| Env | Default | Afrouxar (mais folga) | Apertar |
|---|---|---|---|
| `BE_STRUCTURAL_ENABLED` | `true` | — | `false` (volta ao BE exato) |
| `BE_STRUCT_ATR_BUFFER` | `0.25` | subir p/ `0.4` | baixar p/ `0.15` |
| `BE_MAX_GIVEBACK_R` | `0.5` | subir p/ `0.7` | baixar p/ `0.3` · `0` = BE exato |

Validar: ver no log `[trade-manager] … BE estrutural: SL @ …` vs `BE exato`, e
checar `closed_be` caindo / `closed_tp2` subindo nos auto-trades pós-deploy.

## Painel de assertividade + persistência de skips (#1 — 14/06)

Objetivo: **medir antes de afrouxar**. Antes os skip-reasons viviam só em
memória (`_LAST_SKIP_REASONS`) e zeravam a cada redeploy — impossível saber qual
gate mais barrou na semana. Agora:

- **Persistência durável** — `models/skip_reason_stat.py` (`skip_reason_stats`):
  contador **por gate por dia-UTC** (upsert ON CONFLICT, limitado a ~20 gates ×
  N dias). `_record_skip` agenda fire-and-forget (`_persist_skip_stat`); fail-soft
  total — nunca bloqueia/derruba o loop. Sobrevive redeploy.
- **Endpoint** `/api/shadow/assertiveness?days=30&gate_days=7` (read-only,
  `services/assertiveness_service.py`): cruza dinheiro real (auto-trades
  resolvidos: win-rate, TP1/TP2 hit, expectancy R, P&L USD, por status), shadow
  (snapshots resolvidos — amostra ampla, base da calibração), gates (contadores
  persistidos), e maturidade da calibração.
- **Painel** `🛡️ Assertividade` no app (`AssertivenessPanel.tsx`).

Validar quando reabrir: abrir o painel, ver se "Gates que mais barraram" começa
a acumular pós-deploy desta versão (tabela nova → vazia até o primeiro skip
persistido). Cruzar o gate dominante aqui com a decisão de afrouxar/apertar na
tabela de env knobs acima — **este painel é a fonte do "está secando trade bom?"**.

## Sizing por convicção + orçamento de risco agregado (#2 — 14/06)

Ambos em `shadow_trade_service.py`, **default DESLIGADO** (dinheiro real → opt-in
explícito; zero mudança de comportamento no deploy). Os caps duros já existentes
(`_compute_qty`: `EXCHANGE_MAX_RISK_PCT`, margem) continuam mandando.

- **#2a Sizing por convicção** (`_conviction_mult`): escala o `risk_pct`/trade
  pela P(TP1) calibrada — setup de alta convicção arrisca um pouco mais, fraco um
  pouco menos. Mapeia P(TP1) `[LO..HI]` → `[MIN..MAX]`, clampado. **NO-OP-SAFE**:
  desligado ou calibração imatura (`prob_tp1=None`) → ×1.0.
- **#2b Orçamento de risco aberto** (`_open_risk_usd` + cap no loop): bloqueia
  nova entrada se Σ risco em aberto (`|entry−stop|×qty`) + risco novo > teto da
  banca. Conta a PERDA potencial se tudo estopar junto (≠ notional/margem).
  Posição pós-TP1 (SL≥entry) conta risco ~0 → orçamento rotativo. Skip tag
  `risk-budget` (aparece no painel de assertividade).

| Gate/feature | Env | Default | Afrouxar | Apertar |
|---|---|---|---|---|
| Convicção — liga/desliga | `CONVICTION_SIZING_ENABLED` | `true` | `false` | — |
| Convicção — piso P(TP1) | `CONVICTION_PROB_LO` | `0.45` | — | subir |
| Convicção — teto P(TP1) | `CONVICTION_PROB_HI` | `0.65` | baixar | subir |
| Convicção — mult mín | `CONVICTION_MULT_MIN` | `0.8` | subir p/ `0.9` | baixar p/ `0.7` |
| Convicção — mult máx | `CONVICTION_MULT_MAX` | `1.0` (defensivo) | subir p/ `1.25` (libera upside) | manter `1.0` |
| Risco agregado | `EXCHANGE_MAX_TOTAL_OPEN_RISK_PCT` | `4` (4% banca) | subir o teto | baixar p/ `3` · `0`=off |

> **Config ATIVA desde 15/06**: os dois seguros agora sobem LIGADOS por default.
> Convicção em **modo defensivo** (`MULT_MAX=1.0` → só reduz risco em setup
> fraco, NUNCA aumenta) + orçamento de risco agregado em **4%** da banca. Pra
> liberar o lado de cima (mais risco em alta convicção) subir `CONVICTION_MULT_MAX`
> via env (>1.0) — recomendado só após o teste 0.50. **TP2: pendente** —
> P(TP2) calibrada pra blendar no multiplicador (passo aditivo, a combinar/implantar).

Validar quando reabrir: com convicção ON (default), ver no log `[conviction] …
risco X% → Y% (prob=..)` — com `MULT_MAX=1.0` o `Y` nunca passa do `X` base — e
confirmar que `_compute_qty` ainda clampa pelos caps duros (risco real nunca >
`EXCHANGE_MAX_RISK_PCT`). Com risco agregado em 4%, ver `BLOCKED risk-budget`
quando a banca já tem muito risco aberto.

## Hipótese de trabalho

Defaults foram postos **conservadores** de propósito (pra não secar o teste
dos 10 auto-trades). Expectativa: filtram só o pior (ilíquido / geometria
fraca / baixa P(TP1)). Se o fluxo de trades cair demais durante o teste 0.50,
o primeiro suspeito é o **gate de liquidez** ($10M pode ser alto p/ algumas
das 69) — afrouxar p/ $5M antes de mexer nos outros.
