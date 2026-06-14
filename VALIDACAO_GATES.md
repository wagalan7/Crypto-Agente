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

## Hipótese de trabalho

Defaults foram postos **conservadores** de propósito (pra não secar o teste
dos 10 auto-trades). Expectativa: filtram só o pior (ilíquido / geometria
fraca / baixa P(TP1)). Se o fluxo de trades cair demais durante o teste 0.50,
o primeiro suspeito é o **gate de liquidez** ($10M pode ser alto p/ algumas
das 69) — afrouxar p/ $5M antes de mexer nos outros.
