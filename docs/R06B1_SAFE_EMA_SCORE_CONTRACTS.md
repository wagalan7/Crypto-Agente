# R06B1 — Contratos seguros de EMA, dados finitos e proveniência do score

**Baseline:** `1f336a0f` (R06A) · **Branch:** `main` · **Escopo:** correção dos
quatro contratos que o R06A já tinha *provado*. Nada de estratégia nova.

Este incremento **não** troca 12/26 por 9/21, **não** mexe em Kelly, sizing,
calibração, tiers ou limites, e **não** roda backtest. Para série de entrada
válida e finita, todo número e toda decisão do bot continuam idênticos. A única
mudança decisória permitida — e feita — é **falhar fechado** quando o dado
necessário é inválido ou não finito.

---

## 1. Nomenclatura das EMAs (corrige F1 do R06A)

### Antes
Os campos `ema9` e `ema21` guardavam, respectivamente, `EMA(12)` e `EMA(26)`.
O **valor** sempre esteve certo; o **nome** é que mentia — e a mentira se
propagou para comentários (`EMA9/21/50` no `regime_service`), rótulos de
confluência (`"EMAs alinhadas em alta (9>21>50)"`) e para a zona de pullback do
`entry_planner` (`"Pullback à EMA21"`).

### Depois
Campos canônicos, com o nome igual ao período realmente calculado:

| canônico | período | alias legado |
|---|---|---|
| `ema12`  | 12  | `ema9`  |
| `ema26`  | 26  | `ema21` |
| `ema50`  | 50  | — |
| `ema200` | 200 | — |

Regras do alias, garantidas por um `model_validator` no `Indicator`:

- `ema9 == ema12` e `ema21 == ema26`, **sempre** — construção, cópia ou
  desserialização;
- o alias **não recalcula nada**: continua havendo exatamente quatro chamadas de
  `EMAIndicator`, com `window=12/26/50/200`;
- payload antigo que só traz `ema9`/`ema21` **preenche** o canônico (nada quebra);
- quando ambos chegam, o canônico vence e o legado é reescrito como cópia dele;
- a serialização publica **os seis campos**, para o consumidor migrar no ritmo dele.

**MACD 12/26/9 permanece exatamente como estava** — os períodos 12/26 dele
sempre foram legítimos e não têm relação com este renomeio.

### Consumidores migrados
`indicator_service`, `mtf_service`, `confluence_service`,
`recommendation_service`, `recommendation_backtest`, `entry_planner`,
`ai_service` e o `regime_service` (comentário). Nenhum leitor `ind.ema9` /
`ind.ema21` sobrou no backend. Textos visíveis corrigidos junto:
`(9>21>50)` → `(12>26>50)` e `Pullback à EMA21` → `Pullback à EMA26`.

### Frontend
`Indicator` (TS) declara canônicos **e** legados. O `SignalPanel` lê
`ema12 ?? ema9` e `ema26 ?? ema21`, então funciona tanto com o payload novo
quanto com o antigo. O rótulo exibido já era **EMA 12/26/50** e continua igual;
`confidence`, o limite `0.75`, a `ConfidenceBar` e os botões estão intactos.

---

## 2. Finitude (corrige F2 do R06A)

`_safe` filtrava `NaN` mas devolvia `±inf` como se fosse número. Agora existe um
`_finite` escalar, e `_safe` passa por ele:

- aceita **somente** número real e finito;
- `NaN`, `+inf` e `-inf` viram **ausência** (`None`), nunca `0.0`;
- `bool` não é indicador (`True` é `int` em Python, mas não é preço);
- `str`, `Decimal`, dict e objeto também viram ausência.

O mesmo `_finite` foi aplicado onde o código convertia direto com `float(...)` e
deixava `NaN` passar: `volume_avg`, `volume_last`, `volume_ratio`,
`displacement_3c`, `displacement_3c_atr`, `atr_pct`, `pivot_high`, `pivot_low` e
a banda final do Supertrend. No Supertrend, banda não finita agora anula
**preço e direção** juntos — publicar só a direção seria afirmar tendência sem a
evidência que a sustenta.

**Ausência nunca vira zero.** Para dado válido, tudo isso é no-op.

---

## 3. Candle mais recente inválido (corrige F3 do R06A)

### Antes
Um `close` `NaN` no candle mais novo era **silenciosamente ignorado**: a lib `ta`
propaga o último valor válido, e o indicador saía com cara de atual. O consumidor
não tinha como saber que o candle mais recente estava corrompido — e isso podia
virar falsa confirmação de sinal.

### Depois
`_last_close_is_valid(df)` roda **antes** de calcular qualquer indicador. Se o
`close` mais recente estiver ausente, `NaN` ou infinito, `calculate_indicators`
devolve o **contrato vazio já existente** — o mesmo `Indicator()` que o sistema
retorna para histórico insuficiente (`len(df) < 50`). Nenhum pipeline paralelo de
candles foi criado; o caminho de fail-closed e os contratos de freshness do P04
continuam sendo os mesmos.

Consequência verificada em teste: com candle inválido, `get_indicator_signals`
devolve `{}`, o rótulo de alinhamento é `None` e a confluência não soma o fator
de EMA. Furo no **meio** da série não é escopo deste gate — só o candle mais
recente.

O cálculo da lib `ta` para dado válido (inicialização `ewm(adjust=False)`,
arredondamento em 6 casas) **não foi tocado**.

---

## 4. Proveniência do score (corrige F5 parcialmente — parte observável)

### Antes
Com `SCORE_FORMULA_V2` ligada, se a V2 não fosse computável o código caía na
fórmula legada **em silêncio**. As duas escalas são diferentes (legado 55–100,
V2 ≈18–71) e o número seguia adiante sem dizer quem o produziu.

### Depois
`compute_score_with_provenance(sig)` devolve um `ScoreProvenance` com:

| campo | conteúdo |
|---|---|
| `score` | o score **base** da fórmula (antes do bônus aditivo de confirmação HTF) |
| `formula_requested` | `SCORE_V2` ou `LEGACY_V1` |
| `formula_effective` | a que de fato calculou |
| `fallback_used` | `true` só quando a V2 foi pedida e não deu |
| `fallback_reason` | código controlado, hoje só `V2_NO_COMPONENTS` |

Matriz de comportamento:

| situação | requested | effective | fallback |
|---|---|---|---|
| V2 calculada | `SCORE_V2` | `SCORE_V2` | `false` |
| V2 impossível | `SCORE_V2` | `LEGACY_V1` | `true` + `V2_NO_COMPONENTS` |
| legado pedido | `LEGACY_V1` | `LEGACY_V1` | `false` |

`_compute_score(sig)` continua existindo e devolvendo o mesmo `float` de sempre —
é um wrapper sobre a proveniência, para não quebrar os consumidores que só querem
o número (`main.py`, `recommendation_backtest`, o próprio scan loop).

A proveniência é gravada em `Recommendation.score_provenance`, **dentro do JSON
que já era serializado**. Nenhuma coluna, tabela, migration, ENV, flag, endpoint,
worker ou scheduler foi criada. O motivo é vocabulário fechado: nunca mensagem
livre de exceção, caminho de arquivo ou dado pessoal.

**O que este incremento deliberadamente NÃO faz:** mudar o score numérico, os
bins, a probabilidade retornada, o fallback global, o Kelly ou o sizing. O
objetivo aqui é tornar a incompatibilidade **observável e testável** para o
R06B2 poder bloqueá-la com segurança.

---

## 5. Nomenclatura da calibração (corrige F6 do R06A)

`_load_real_pairs` lia `RecommendationSnapshot` — o registro de **recomendações**
(SHADOW), não o financeiro. O nome sugeria dinheiro executado.

- novo nome: **`_load_shadow_pairs`**;
- `_load_real_pairs` sobrevive como **wrapper legado** que só faz
  `return await _load_shadow_pairs()` — sem duplicar a query;
- o caminho de calibração chama o nome novo;
- fonte, filtros (`RESOLVED_STATUSES`, `_not_fast_void`, `LOOKBACK_DAYS`),
  amostra e resultado são **exatamente** os de antes;
- nenhuma consulta a `RealTrade` foi introduzida.

Nada de calibração real foi executado, cache não foi limpo, holdout não foi
aberto, seed privado não foi carregado e nenhuma probabilidade mudou.

---

## 6. Testes

Arquivo novo: `backend/tests/test_r06b1_safe_ema_score_contracts.py` — **58
testes herméticos** (rede/DNS bloqueados e contabilizados; sem exchange, banco de
produção, credencial, seed privado ou outcome de holdout). Sem `skip` e sem
`xfail` escondendo falha.

Cobertura por bloco: EMA canônica (12 testes) · paridade de decisões (8) ·
finitude (8) · candle inválido (7) · proveniência do score (8) · nomenclatura da
calibração (5) · frontend (5) · escopo do pacote (4). Uma referência de EMA
independente é reimplementada dentro do teste, e os greps de contrato passam por
um extrator que remove comentários e docstrings — para o `assertNotIn` não casar
com a própria prosa que explica o contrato.

`test_r06a_indicator_score_contracts.py` foi **atualizado**: os testes de
caracterização de F2, F3 e F6 viraram testes de correção
(`test_CORRIGIDO_*`), e os que liam os nomes legados passaram a ler os canônicos.
Dois asserts de escopo do R06A que comparavam a **árvore de trabalho** com
`61265ed1` foram fixados no **range de commits do R06A** (`61265ed1..1f336a0f`) —
a garantia original continua a mesma, mas deixa de quebrar a cada fase autorizada
seguinte. Em `test_hardening_characterization.py`, um duplo sintético de
`Indicator` migrou para os nomes canônicos.

---

## 7. Limitações — o que este pacote NÃO prova

- **9/21 não foi testado como challenger.** Nenhuma comparação foi executada.
- **12/26 continua sendo o comportamento do champion**, agora com o nome certo.
- **Nenhuma conclusão de lucratividade foi produzida.** Não houve backtest e não
  há evidência ligando qualquer coisa aqui a resultado financeiro.
- **Score/bin incompatível ainda não é bloqueado** — F5 segue aberta na parte que
  importa; o R06B1 só a tornou visível. Isso é o R06B2.
- **A semântica financeira do Kelly (F7) segue como está** — `p = prob_tp1` com
  `b = risk_reward` do alvo final continua otimista, e será tratado depois.
- Furo de dado no **meio** da série não é coberto pelo gate de candle.
- Estes testes travam contratos; não provam ausência de bugs.

---

## 8. Próximo passo (R06B2)

Bloquear a binagem incompatível com segurança, agora que a proveniência existe:
usar `formula_effective` para recusar calibrar/binar um score legado sob bins V2
(e vice-versa), decidindo explicitamente o comportamento quando isso acontecer —
fail-closed em `prob_tp1` (devolver ausência) em vez do atual `p_global`
silencioso, que hoje entra no Kelly como se fosse informação.
