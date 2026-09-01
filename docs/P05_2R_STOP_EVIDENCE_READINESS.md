# P05.2R — monitor de prontidão da evidência de stop (SOMENTE LEITURA)

## Objetivo

Responder automaticamente, num único lugar:

- o que **já tem** evidência suficiente;
- o que ainda está **coletando** dados;
- **por que** o P05.2C continua bloqueado;
- **quando** uma hipótese poderá ser apresentada para revisão do holdout;
- qual **telemetria** ainda está incompleta.

> O monitor **não toma decisão operacional**. Não promove, não ativa, não
> executa e não abre o holdout.

## Antes

Os dados existiam, mas espalhados em seções independentes — padrões do P05.2A,
laboratório do P05.2B, trajetória do P05.1T, latência do P05.2L e amostra REAL.
O usuário precisava cruzar tudo à mão e não havia nenhuma indicação única de
prontidão nem de motivo de bloqueio.

## Depois

Um monitor único (`stop_readiness`), com trilhas independentes, motivo explícito
do bloqueio, ETA quando matematicamente possível e a indicação
`READY_FOR_P052C` — **sem abrir o holdout**.

## Contrato

`stop_readiness` foi acrescentado à resposta **existente** de
`/api/strategy/p05/status` (e ao agregado já consumido pelo `AssertivenessPanel`).

```
phase="P05.2R" · execution_mode="ANALYTICS_ONLY" · read_only=true
state · reason_code · detail · ready_for_p052c · next_action · blocked_by
holdout_status="SEALED" · holdout_outcomes_read=false
holdout_metrics_computed=false
thresholds · tracks · eta · does_not_mean · limitations · computed_at
```

## Estados

`UNAVAILABLE` · `COLLECTING` · `INSUFFICIENT_EVIDENCE` · `HYPOTHESIS_REJECTED` ·
`READY_FOR_P052C`.

`APPROVED`, `ELIGIBLE`, `PROMOTED`, `ACTIVE`, `LIVE_READY` e `AUTO_PROMOTE`
**não existem** nesta fase (verificado por teste).

### Precedência aplicada nesta ordem

1. erro estrutural / diagnóstico indisponível ⇒ `UNAVAILABLE`;
2. sem padrão persistente elegível ⇒ `COLLECTING`
   (`reason_code=NO_PERSISTENT_ADVERSE_PATTERN`);
3. padrão existe e a hipótese reprovou em critério substantivo ⇒
   `HYPOTHESIS_REJECTED`;
4. padrão existe, mas amostra/cobertura/IC insuficientes ⇒
   `INSUFFICIENT_EVIDENCE`;
5. candidato passou em todos os checks da validação ⇒ `READY_FOR_P052C`.

Reprovação substantiva **nunca** volta a ser "coletando"; ausência de evidência
**nunca** vira reprovação.

### `READY_FOR_P052C` — regra

Só quando, simultaneamente: existe padrão `PERSISTENT_ADVERSE` em eixo elegível ·
`offline_lab.status == "VALIDATION_SUPPORTED"` · existe ao menos um candidato
`VALIDATION_SUPPORTED` · o candidato continua `executable=false` ·
`promotable=false` · `requires_future_holdout_review=true` · holdout `SEALED` ·
nenhum outcome do holdout foi lido.

Se alguma dessas invariantes não for confirmada, o estado cai para
`UNAVAILABLE` com `reason_code=CONTRACT_INVARIANT_BROKEN` — prontidão nunca é
declarada por omissão.

**Significa apenas**: "há uma hipótese apoiada pela validação que pode ser
apresentada para uma futura revisão MANUAL do holdout". Não é aprovação, não é
Shadow, não é alteração LIVE.

## Trilhas

| Trilha | Fonte | Libera P05.2C? |
|---|---|---|
| `stop_pattern` | P05.2A — verdicto, padrões persistentes/não persistentes, eixos elegíveis | **sim** |
| `offline_lab` | P05.2B — hipóteses avaliadas, apoiadas, rejeitadas, insuficientes, motivo principal, `ready_for_holdout_review` | **sim** |
| `forward_path` | P05.1T — observações de `p05_path`, cobertura MAE/MFE, missing por motivo | não |
| `live_execution` | P05.2L — tentativas, cobertura, fills auditáveis, RealTrades ligados, missing por motivo | não |
| `real_sample` | REAL (`RealTrade source=auto`) — fechados, stops, taxa, expectancy, confiabilidade, vínculo, cobertura de slippage | não |

`SAMPLE_LIMITED`, `MIXED` e `LOW_COVERAGE` **nunca** contam como prontos.
Pisos informativos das trilhas de telemetria: 30 observações e 80% de cobertura —
elas são diagnósticas e, sozinhas, **não** liberam o P05.2C (coberto por teste).
REAL e SHADOW nunca são somados.

## ETA

Reutiliza `estimate_eta` do P05.1R:

- histórico observado mínimo de **7 dias** (`P051R_MIN_OBSERVED_DAYS_FOR_RATE`);
- taxa calculada só com timestamps já disponíveis;
- arredondamento para cima, nunca negativo;
- taxa zero ⇒ `eta_days=null`; histórico jovem ⇒ `eta_days=null`;
- hipótese reprovada substantivamente ⇒ `eta_days=null` (mais dado não desfaz
  contradição);
- `COLLECTING` ⇒ `null`, porque o surgimento de um padrão persistente não é
  previsível por contagem;
- `READY_FOR_P052C` ⇒ `0`, já disponível para revisão manual;
- `eta_reason` **sempre** presente.

Nenhum outcome do holdout é acessado para calcular ETA. ETA é aproximação, nunca
promessa.

## Holdout

O monitor consome **apenas** agregados já produzidos pelo P05.2A/P05.2B e
telemetria sem outcome. Não recebe `split["test"]`, não lê `realized_r`, status
ou métrica do teste, não escolhe candidato pelo teste e não abre o teste. Teste
com **sentinela** explode se qualquer outcome selado for tocado.

## Zero escrita

Função **pura e síncrona**: sem `session.add`, `commit`, `flush`, `merge`,
`delete` ou `update`; sem `StrategyExperiment`; sem alteração de snapshot ou
`RealTrade`. Um teste com sessão falsa falha se qualquer método de escrita for
chamado.

## API e cache

Nenhum endpoint novo. O campo entra na resposta já existente de
`/api/strategy/p05/status` e no agregado `/api/shadow/assertiveness`, calculado
sobre os blocos que aquelas rotas já obtiveram dos caches single-flight
existentes — **sem consulta nova e sem cache paralelo**. Fail-soft: uma trilha
que falha vira `UNAVAILABLE` com motivo legível, as demais continuam, o status
não cai e uma resposta estruturalmente inválida nunca declara prontidão. Sem
POST, sem auth nova, sem execução sob demanda, sem `retry-now`, sem
`open-holdout`.

## Frontend

Seção compacta **"Prontidão para a próxima etapa"** no `AssertivenessPanel`:
estado atual, explicação em linguagem simples, as cinco trilhas com o respectivo
detalhe, ETA quando disponível, próxima ação recomendada e o selo
**🔒 Holdout protegido**.

Textos fixos: *"Pronto para P05.2C não significa aprovado."*, *"O holdout final
continua fechado."*, *"Nenhuma alteração foi aplicada à estratégia."*,
*"Telemetria suficiente não prova causalidade."*

Sem botão; nada de Aplicar, Promover, Ativar, Abrir holdout, Executar ou Testar
agora; nenhum `[object Object]`; `frontend/dist` não foi editado.

## Limitações

1. Monitor observacional: não decide, não promove e não executa.
2. Telemetria suficiente **não prova causalidade**.
3. Trajetória e latência são diagnósticas e, sozinhas, não liberam o P05.2C.
4. ETA é aproximação por contagem, nunca promessa de data.
5. REAL e SHADOW nunca são somados.
6. O teste final permanece SELADO e nenhum outcome dele é lido.

## Invariantes

- Champion LIVE idêntico; score, tier, gate, stop, TP e sizing inalterados.
- Nenhuma regra contextual, experimento, Shadow ou promoção.
- Nenhuma ordem, exchange, credencial ou SDK de provider.
- Nenhum scheduler, worker, queue ou notificação externa.
- Nenhuma tabela, coluna, migration, env, flag ou endpoint novo.
