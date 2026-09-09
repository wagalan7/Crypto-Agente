# R07A — Playbooks por regime: contratos e comparação offline

**Baseline:** `a50a206c` · **Branch:** `main` · **Modo:** `ANALYTICS_ONLY`,
non-promotable, sem ligação com o executor.

Organiza as regras que **já existem** por cenário de mercado, passa a registrar
o contexto necessário daqui para frente e compara três hipóteses restritas de
abstenção sobre os setups disponíveis. Não é um motor de sinais novo, não altera
o funcionamento LIVE e não afirma melhora de lucro.

---

## 1. Mapa do comportamento existente

| regra | condição | efeito | consumidor | dado persistido |
|---|---|---|---|---|
| `block_all` | `btc_24h <= RISK_OFF_BTC_24H` | **bloqueia** tudo | `should_block_recommendation` | `features.regime` (rótulo) |
| `block_alt_longs` | `dom >= ALT_DANGER_DOM` **e** `btc_24h >= ALT_DANGER_BTC_24H`; ou `ALT_RISK_OFF` com `ALT_RISKOFF_BLOCK` | **bloqueia** long não-major | `should_block_recommendation` | rótulo apenas |
| `downgrade_alt_longs` | `dom >= BTC_DOMINANT_THRESHOLD` **e** `btc_24h >= BTC_DOMINANT_MIN_BTC_24H`; ou `ALT_RISK_OFF` sem block | **rebaixa** long não-major | `recommendation_service` (tier/seleção) | rótulo apenas |
| `block_shorts` | `btc_trend_pct >= SHORT_BRAKE_TREND_PCT` **e** `SHORT_BRAKE_BLOCK` | **bloqueia** short | `should_block_recommendation` | rótulo apenas |
| `downgrade_shorts` | mesma condição, sem `SHORT_BRAKE_BLOCK` | **rebaixa** short | `recommendation_service` | rótulo apenas |
| `symbol_counter_trend` | `>= CT_BRAKE_MIN_TFS` TFs superiores EMA na direção oposta **e zero a favor** | razão textual | seleção + `_sel_key` | `mtf.higher_tfs[].ema_aligned` |
| penalidade contratendência | `symbol_counter_trend != None` | **−`CT_BRAKE_SELECT_PENALTY`** no score de SELEÇÃO | `_pick_best_signal` | não persistida |
| breakout lane / retest | `breakout_confirmed` / `retest_active` no padrão | bônus de score, re-arm de entrada | `_compute_score`, entry planner | `patterns[]`, `retest_armed` |

### As três dimensões, separadas

- **A. Regime macro** — flags de `get_regime_status`.
- **B. Tendência EMA do próprio ativo** — `higher_tfs[].ema_aligned`.
- **C. Estrutura do setup** — padrões, rompimento, retest, zona de entrada.

### Cuidados que o código exige

- **`NORMAL` não é lateralidade.** É "nenhum gatilho disparou".
- **`NORMAL` pode vir com `quality` `UNKNOWN` ou `DISABLED`** — aí é ausência de
  leitura, não mercado calmo. O classificador devolve `UNKNOWN` nesse caso.
- **`mtf.aligned_count` mede concordância com a direção do SINAL** e não
  substitui `higher_tfs[].ema_aligned`. Ele nem entra no contexto R07.
- **Nome de padrão não prova rompimento.** Só `breakout_confirmed == true`.
- **`retest_armed=None` não prova ausência de retest.**
- **`is_btc_symbol` é a política de majors vigente** e é usada como está.
  Observação registrada, não corrigida aqui: ela só reconhece major quando o
  símbolo traz separador (`BTC/USDT`, `BTC-USDT`); `BTCUSDT` colado devolve
  `False`. O R07 reproduz a política do champion em vez de divergir em silêncio.

### O que os snapshots NÃO são

Eles são **posteriores a filtros e seleção**. Não contêm os sinais vetados nem
as alternativas que o bot poderia ter escolhido no lugar. Qualquer leitura de
"quantos stops teriam sido evitados" é sobre o subconjunto que sobreviveu até
virar snapshot.

---

## 2. Contrato de contexto prospectivo

`features["r07_context"]`, gravado **somente** ao criar snapshots novos, dentro
da escrita que já existia. Sem tabela, coluna, migration, ENV, flag, endpoint,
worker ou fila.

Allowlist: `schema_version`, `source`, `captured_at`, `direction`, `is_major`,
`macro` (regime + as cinco flags + `filter_enabled` + `quality` +
`observed_at_ms`), `ct_brake` (config para interpretar o freio),
`higher_tfs[{timeframe, ema_aligned}]`,
`patterns[{type, direction, breakout_confirmed, retest_active}]`,
`entry_zone_type`, `retest_armed`, `signal_timestamp_ms`, `data_freshness`.

O objeto de sinal inteiro **não** é salvo; `outcome`, `realized_r` e `p05_path`
**não** entram; timestamps e qualidade **não** são fabricados — valor não
booleano, `NaN` ou texto vazio viram `None`, nunca um default plausível.

### Macro sem consulta extra

`_current_regime_label` virou um wrapper de `_current_regime_state`, que faz a
**mesma** consulta única por batch e devolve `(rótulo, payload)`. Os chamadores
antigos continuam funcionando e a contagem de chamadas externas por save não
mudou — há teste para isso.

### Três instantes, separados

`captured_at` (montagem da anotação), `macro.observed_at_ms` (observação da
fonte) e o `created_at` da linha. Se a observação for **posterior** ao instante
avaliado, ela não podia ter informado aquela decisão: a dimensão macro vira
`UNKNOWN` com `MACRO_OBSERVED_AFTER_CAPTURE`. Nada é retrodatado e `created_at`
não é tocado.

### Integração tolerante

Falha da anotação devolve `{}`: a recomendação é salva do mesmo jeito, a
deduplicação/outcome/classificação não mudam, nenhum snapshot histórico é
regravado e `p05_context`, `p05_path` e `probability_contract` ficam intactos.

---

## 3. Classificador e catálogo

Classificador puro sobre campos pré-outcome. Devolve **dimensões, evidências,
campos ausentes e motivos controlados** — e não força um rótulo único quando os
cenários se sobrepõem.

**Tendência:** deduplica por timeframe (TF repetido não conta duas vezes),
conflito no mesmo TF vira `TREND_UNCERTAIN`, e "inequívoca" usa a semântica real
do freio (`>= min_tfs` na mesma direção **e zero** contra) com a config
congelada no contexto. Dado ausente/inválido nunca vira alinhamento favorável.

**Estrutura:** `horizontal_channel` produz apenas "canal horizontal detectado" —
limites do range, entrada na borda, volume e confirmação **não** são inventados.
`breakout_confirmed=True` é rompimento confirmado, e a leitura direcional exige
os dois lados. `retest_active=True` é retest registrado. Ausência produz
`UNKNOWN`/`UNCLASSIFIED` com motivo.

**Catálogo** (tendência, canal horizontal, rompimento/retest, restrição macro)
diz o que cada cenário exigiria e o que falta para reproduzi-lo. É informativo:
não é regra executável.

**Histórico sem `r07_context`** aparece como `LEGACY` na distribuição, com o que
foi realmente persistido. Tendência EMA, qualidade macro e confirmação de
rompimento **não** são reconstruídas por proxy, e não há backfill.

---

## 4. As três hipóteses congeladas

Fixadas **antes** de observar resultados, sem grid search, sem regra por símbolo
ou horário, sem combinar regras:

| id | hipótese | fonte |
|---|---|---|
| `H1_ABSTAIN_COUNTER_TREND` | abster-se de entradas contra a tendência EMA inequívoca do ativo | `higher_tfs[].ema_aligned` |
| `H2_ABSTAIN_DOWNGRADED_SHORTS` | abster-se de shorts quando `downgrade_shorts=True` | `macro.downgrade_shorts` |
| `H3_ABSTAIN_DOWNGRADED_ALT_LONGS` | abster-se de longs não-majors quando `downgrade_alt_longs=True` | `macro.downgrade_alt_longs` |

Cada uma devolve `VETO` / `KEEP` / `UNKNOWN`, exige contexto válido, tem config
versionada com hash SHA-256 determinístico, e **não** altera entrada, stop, TP,
score ou tamanho, nem adiciona operações, nem simula troca por outra moeda.
Regras de range/rompimento **não** são candidatas nesta entrega.

São testes de transformar **cautela existente** (rebaixamento) em **veto
adicional** — não mudanças já aprovadas. Quando a regra não remove nada além do
que o baseline já recusa, o resultado é `NO_INCREMENTAL_CHANGE` e nenhum
benefício lhe é atribuído.

---

## 5. Correção do holdout

`load_stop_shadow_split` materializava o SELECT de detalhes com
`outcome_at <= boundary` e só descartava ids fora do permitido **depois** de
`.all()`. Um empate de timestamp na borda trazia outcome e features de uma linha
do TESTE para dentro do processo antes do descarte.

Agora o filtro está **no próprio SELECT** (`.where(RS.id.in_(allowed_ids))`),
antes da materialização. O split, os demais filtros e a defesa por id no retorno
seguem exatamente iguais.

### Cronologia (purga R07)

Para a comparação R07, a validação exclui setups cujo `created_at` **não** seja
posterior ao último `resolved_at` do treino. Ausência de timestamp impede
confirmar a separação e a linha é purgada, não presumida boa. A purga é
reportada e vive **dentro** do R07 — as partições dos consumidores P05 não
mudam.

---

## 6. Baseline e comparação

O baseline chama-se, em todo o payload e na tela, **"baseline de seleção
reconstruído sobre setups SHADOW"**. Ele não reproduz integralmente as ordens
históricas: a config atual congelada não equivale à config histórica de cada
trade, e componentes sem cobertura suficiente ficam desligados dos **dois** lados
e aparecem em `component_coverage`.

Para cada hipótese: universo só com decisões de baseline **e** de regra
conhecidas; `UNKNOWN` excluído **simetricamente antes** de olhar qualquer
resultado; contagem de excluídos e cobertura sobre o universo original;
`baseline=False` nunca vira `True`; challenger = `baseline AND regra != VETO`,
com a mesma identidade de oportunidade nos dois lados.

Reutiliza `_partition_outcomes`, `_lab_metric_row`, `compute_evidence_metrics`,
`wilson_interval`, `bootstrap_paired_membership_delta_ci`,
`bootstrap_paired_stop_rate_delta_ci` e `_material_segment_regressions` — as
mesmas primitivas do P05, com seed fixa. Nenhum motor novo.

Relatado por estágio: elegíveis, afetadas, operações preservadas, **stops
removidos E wins removidos**, saídas protegidas removidas, expectancy, soma R,
profit factor, drawdown em R e comparação pareada com IC. Linhas excluídas **não
são operações evitadas** e redução por falta de dados **não é melhora**.

---

## 7. Resultado sem promoção

Estados: `UNAVAILABLE`, `INSUFFICIENT_EVIDENCE`, `NO_INCREMENTAL_CHANGE`,
`NOT_SUPPORTED`, `VALIDATION_SUPPORTED`.

`VALIDATION_SUPPORTED` exige todos os checks: cobertura mínima do P05, amostra
afetada mínima vigente, preservação mínima do laboratório de stops, expectancy
das removidas negativa com IC superior < 0, delta pareado com IC inferior > 0,
redução de stop rate sustentada pelo IC, expectancy e soma R positivas, profit
factor e drawdown não piores, e nenhuma regressão material nos segmentos
avaliáveis.

São reutilizados os **checks**, não o wrapper que exige origem
`PERSISTENT_ADVERSE` e seleciona hipóteses pela validação — esse marcador não se
aplica ao R07 e **não foi falsificado** para reaproveitar a função.

`VALIDATION_SUPPORTED` **não** abre o holdout, **não** libera o P05.2C, **não**
cria experimento, **não** gera `promotion_plan` e **não** permite execução.
Falta de dado é resultado válido, não autorização para baixar pisos.

---

## 8. Integração e frontend

`regime_playbooks` entra no diagnóstico de stops já existente, recebendo as
**mesmas** linhas de treino/validação e usando o **mesmo** cache. Nenhum loader,
consulta ou cache paralelo; nenhuma rota nova; `main.py` não foi tocado. Erro na
seção deixa só ela `UNAVAILABLE`, preserva as demais, não fica cacheado como
sucesso e não afeta `stop_readiness`.

No `AssertivenessPanel` há uma seção pequena com cenários e cobertura,
hipóteses e comparação, **stops removidos e wins removidos**, desconhecidos e
limitações, e a frase *"Somente análise — nenhuma estratégia foi alterada"*. Sem
botão, sem painel novo, sem editar `frontend/dist`.

---

## 9. Limitações

- Snapshots são pós-filtro e pós-seleção (ver §1).
- O contexto é do **save**, não necessariamente o do scanner/executor.
- Baseline é reconstrução, não replay de carteira nem validação final.
- R de setup não é lucro líquido; REAL nunca é somado ao SHADOW.
- Abster-se de um contexto adverso remove também os wins dele.
- Hipóteses são exploratórias e múltiplas: não há vencedor, causalidade nem
  aprovação estatística final.
- Histórico anterior ao R07A não tem contexto e não é reconstruído.
- A política de majors tem a sensibilidade a formato de símbolo descrita em §1.
- **Cobertura em produção não foi consultada** — consulta externa está fora
  desta implementação, e nenhum número de produção é afirmado aqui.
- **Nenhuma redução real de stops ou de prejuízo foi demonstrada.** Os números
  desta entrega vêm de dados sintéticos nos testes e do que houver no ambiente
  quando o diagnóstico rodar.

---

## 10. Critérios para uma etapa futura (não iniciada)

Uma eventual R07B precisaria, no mínimo: cobertura de `r07_context` acumulada
sobre uma janela relevante (o contrato só existe a partir daqui, sem backfill);
uma hipótese com `VALIDATION_SUPPORTED` estável em execuções sucessivas; e uma
decisão explícita sobre o holdout, que continua selado. Nada disso foi iniciado
neste pacote.
