# R06A — auditoria de EMAs e semântica de scores/probabilidades

> **Auditoria.** Nenhum cálculo, decisão, filtro, score, tier, sizing, stop, TP
> ou flag foi alterado. As únicas mudanças aplicadas são testes, esta
> documentação e **um texto** de tela comprovadamente incorreto.

Baseline: `61265ed1`. Evidência: `backend/tests/test_r06a_indicator_score_contracts.py`
(44 testes herméticos, séries sintéticas determinísticas, sem rede/exchange/DB).

**Aviso sobre os testes:** boa parte é de **caracterização** — trava e descreve o
comportamento atual, inclusive onde ele está errado. Teste verde ali **não**
significa defeito corrigido.

---

## Respostas diretas

**A. Quais períodos de EMA o sistema realmente calcula?**
`12`, `26`, `50` e `200` — gravados nos campos `ema9`, `ema21`, `ema50`,
`ema200`. Os dois primeiros nomes **não correspondem** ao período calculado.

**B. Os consumidores esperam esses mesmos períodos?**
Não. Todo consumidor lê os campos pelo nome e a documentação afirma "EMA9/21/50"
(`regime_service.py:420`). O `entry_planner` rotula a zona de pullback como
"Pullback à EMA21" usando a EMA de 26.

**C. O que significam `confidence`, `score` e as probabilidades?**
- `confidence` (0–1) é `confluence.pct / 100`: pontuação heurística de
  confluência. **Não é probabilidade de lucro.**
- `score` (0–100) é ranking heurístico, com duas fórmulas de escalas diferentes
  (V2 ≈18–71, legado 55–100) e fallback silencioso entre elas.
- `prob_tp1`/`prob_tp2` (0–1) são probabilidades calibradas de **tocar TP1** e
  de **correr até TP2**, estimadas sobre `RecommendationSnapshot` (SHADOW), não
  sobre execução financeira real.

**D. Existem erros de unidade, apresentação, fallback ou versão?**
Sim: apresentação (`confidence` chamado de "Probabilidade" — **corrigido**),
nomenclatura (EMA e `_load_real_pairs`), versão (fallback V2→legado sem
registrar a fórmula, com bins que seguem a flag), robustez (`±inf` e `NaN`) e
semântica (Kelly com evento de TP1 e RR do alvo final).

**E. Qual correção mínima e o que mudaria?**
Ver o **plano R06B** no fim. Nenhum defeito **aritmético** de EMA foi
encontrado: a implementação calcula corretamente a EMA dos períodos que recebe.
O problema é **qual período** cada nome carrega e o que os consumidores supõem.

---

## Tabela de contratos

| Campo | Cálculo atual | Significado esperado pelo nome/consumidor | Consumidores | Risco de mudar |
|---|---|---|---|---|
| `ema9` | `EMAIndicator(window=12)` | EMA de 9 períodos | `get_indicator_signals`, `confluence_service`, `mtf_service`, `recommendation_backtest` | **Alto** — muda `ema_trend`, confluência, `ema_aligned`, regime e, por consequência, score/tier |
| `ema21` | `EMAIndicator(window=26)` | EMA de 21 períodos | idem + `entry_planner` (zona de pullback) | **Alto** — muda também o **preço de entrada** planejado |
| `ema50` | `EMAIndicator(window=50)` | EMA de 50 | alinhamento, viés de longo prazo | Nenhum (correto) |
| `ema200` | `EMAIndicator(window=200)`, `None` se <200 candles | EMA de 200 | `confluence_service` (viés macro) | Nenhum (correto) |
| MACD | `12/26/9` | MACD padrão | `get_indicator_signals` | **Não mexer** — períodos legítimos |
| `confidence` | `confluence.pct / 100` (0–1) | "probabilidade" na tela | `SignalPanel` (aviso, barra, gate visual 0.75) | Baixo (texto) / Alto (se virar prob real) |
| `score` | V2 (`conf/adx/funding`) ou legado | ranking 0–100 | tier, sizing, bins de calibração | **Alto** |
| `prob_tp1` | `p_calibrated` do bin do score | P(tocar TP1) | `_compute_dynamic_size` (Kelly), card | **Alto** — é sizing |
| `prob_tp2` | `p_tp2_calibrated` | P(correr até TP2) | convicção/sizing | Alto |

---

## Achados

### F1 — `ema9`/`ema21` carregam 12/26 · severidade **ALTA** · defeito confirmado

**Arquivo:** `backend/services/indicator_service.py:40-41`

```python
ema9  = _safe(ta.trend.EMAIndicator(close, window=12).ema_indicator())
ema21 = _safe(ta.trend.EMAIndicator(close, window=26).ema_indicator())
```

**Reprodução:** `PeriodosDeEma.test_campo_ema9_carrega_periodo_12` e
`…_ema21_carrega_periodo_26` comparam com uma referência EMA independente
(`alpha = 2/(N+1)`) e provam que os valores batem com 12 e 26, e **não** com 9
e 21.

**Comportamento esperado:** ou o campo `ema9` contém EMA(9), ou o campo passa a
se chamar `ema12`. Hoje nome e conteúdo discordam.

**Impacto medido** (só em série sintética, `ConsumidoresDeEma.test_alinhamento_atual_difere_de_9_21_50`):
o rótulo de alinhamento muda em **0% na tendência limpa, 3% na reversão e 21% na
oscilação**. Ou seja: em mercado direcional a escolha é quase indiferente; em
mercado lateral/oscilante ela é material. Esse rótulo alimenta `ema_trend`,
confluência, `ema_aligned` do MTF, o regime e, por cascata, score e tier.

**Não está provado** que essa divergência causou os stops observados — nenhum
teste aqui liga períodos de EMA a resultado financeiro.

**Correção mínima proposta:** decidir **qual** é a intenção estratégica.
- Se a intenção era 9/21 → trocar as janelas. É **mudança de estratégia**:
  altera sinais, score, tier e o preço de entrada do `entry_planner`.
- Se a intenção era 12/26 → renomear os campos para `ema12`/`ema26` e ajustar
  documentação e rótulos. **Não** muda decisão nenhuma.

**Recomendação:** a segunda opção primeiro (renomear, risco zero de decisão), e
tratar a troca de períodos como experimento separado, com backtest.

---

### F2 — `_safe` deixa passar `±inf` · severidade **MÉDIA** · defeito confirmado

**Arquivo:** `indicator_service.py:8-12`

```python
return float(v) if pd.notna(v) else None
```

`pd.notna(float("inf"))` é `True`. Reprodução:
`test_DEFEITO_safe_deixa_passar_infinito`.

**Impacto:** um indicador infinito passa adiante e vence qualquer comparação de
alinhamento/limite. Nenhuma ocorrência real foi observada — é robustez, não
incidente conhecido.

**Correção mínima:** trocar por `math.isfinite(v)`. Não altera nenhum valor
legítimo; só transforma `inf` em `None`, que já é um caminho tratado.

---

### F3 — `close` NaN no candle mais recente é ignorado em silêncio · severidade **MÉDIA**

**Reprodução:** `test_DEFEITO_nan_no_ultimo_close_e_ignorado_silenciosamente`.

`ewm` propaga o último valor válido; `_safe` devolve um número que **parece**
atual mas ignora o candle corrompido. O consumidor não distingue "indicador do
candle atual" de "indicador do candle anterior".

**Correção mínima:** validar a integridade do último candle antes de calcular e
devolver `Indicator()` (ou marcar `stale`) quando ele for inválido.

---

### F4 — `confidence` apresentado como "Probabilidade" · severidade **ALTA** · **CORRIGIDO**

**Arquivo:** `frontend/src/components/SignalPanel/SignalPanel.tsx`

Texto anterior: *"Probabilidade X% abaixo do mínimo de 75% para operar"*, sobre
um valor que é `confluence.pct / 100` — heurística de confluência, sem
calibração e sem relação demonstrada com lucro. O mesmo campo já aparecia como
"Força do Sinal" duas linhas abaixo.

**Correção aplicada (somente texto):**

```
Força do sinal: X/100 — referência desta tela: 75/100
Este indicador não representa probabilidade de lucro.
```

Preservados: valor numérico, limite `0.75`, condições de renderização,
`ConfidenceBar`, botão "Adicionar ao Gestor de Trades", ordenação e chamadas de
API. Regressão em `ApresentacaoFrontend`.

---

### F5 — fallback V2→legado é silencioso e as escalas diferem · severidade **ALTA**

**Arquivos:** `recommendation_service.py::_compute_score`,
`calibration_service.py::SCORE_BINS`.

`_compute_score_v2` devolve `None` quando não há confluência **nem** ADX **nem**
funding; o caller cai na fórmula legada **sem registrar qual foi usada**. As
escalas são diferentes (V2 ≈18–71; legado 55–100) e os bins da calibração
seguem a mesma flag de ambiente.

**Consequência demonstrada** (`test_score_legado_fora_dos_bins_v2`): com
`SCORE_FORMULA_V2=true`, um score produzido pelo caminho legado (80, 90, 99)
cai **fora** de todos os bins V2 → `_bin_index` devolve `-1` → `prob_tp1` vira
`p_global`, perdendo toda a diferenciação por score. E `p_global` alimenta o
Kelly do sizing.

**Correção mínima:** persistir a fórmula usada (ex.: `score_formula`) junto do
score e recusar binagem quando a fórmula do score não bate com a dos bins —
devolvendo `None` (que o sizing já trata) em vez de `p_global`.

---

### F6 — `_load_real_pairs` lê SHADOW, não execução real · severidade **MÉDIA**

**Arquivo:** `calibration_service.py:291`

Docstring: *"Pares (score, status) dos trades REAIS resolvidos"* — a consulta é
`RecommendationSnapshot`, ou seja, resultado de **setup**, não dinheiro
executado. Reprodução: `test_DEFEITO_fonte_e_shadow_apesar_do_nome_real`.

Isso é **coerente** com o resto do projeto (o R05A já estabeleceu que snapshot
não é verdade financeira), mas o nome e o texto induzem ao erro.

**Correção mínima:** renomear internamente para `_load_shadow_pairs` (ou ajustar
apenas a docstring) e expor a fonte no contrato da calibração. Não muda número.

---

### F7 — Kelly mistura P(TP1) com RR do alvo final · severidade **ALTA**

**Arquivo:** `recommendation_service.py::_compute_dynamic_size`

```python
kelly = (p * b - (1.0 - p)) / b     # p = prob_tp1, b = risk_reward
```

Kelly pressupõe que `p` é a probabilidade de ganhar **`b` unidades**. Aqui `p` é
a probabilidade de **tocar o TP1**, enquanto `b` é o RR até o alvo final. Como
`P(TP1) ≥ P(alvo final)`, a fração de Kelly resultante é **otimista**.
Reprodução: `test_DEFEITO_kelly_mistura_evento_tp1_com_rr_do_alvo_final`.

Atenuantes reais já presentes: `KELLY_FRACTION`, `score_mult`, `vol_mult` e o
cap `[SIZE_MIN, SIZE_MAX]`. O viés existe, mas não é o único fator do tamanho.

**Correção mínima:** usar o par coerente — ou `p = prob_tp1` com o `b` do **TP1**,
ou `p = prob_tp2` com o `b` do alvo final; ou modelar o payoff em duas pernas.
**É mudança de sizing** e exige decisão explícita + validação.

---

### Hipóteses NÃO comprovadas

- Que a divergência 12/26 vs 9/21 tenha causado os stops recentes. Não há
  evidência: esta auditoria não tocou em resultado financeiro nem no holdout.
- Que trocar para 9/21 melhore a assertividade. Só foi medido que **muda
  rótulos** (21% em oscilação), não que melhore resultado.
- Que `p_global` por fallback de bin esteja ocorrendo em produção hoje —
  depende do valor real de `SCORE_FORMULA_V2` no ambiente, que não foi
  consultado.

### Convenção que **não** é defeito

A lib `ta` calcula EMA como `ewm(alpha=2/(N+1), adjust=False)` desde o primeiro
ponto, mascarando os `N−1` primeiros como `NaN` — não usa semente SMA. É uma
convenção legítima, reproduzida na referência independente
(`test_convencao_da_lib_e_recursiva_desde_o_inicio`). Consequência a registrar:
com histórico curto o valor ainda carrega peso do primeiro candle da janela
recebida.

---

## Plano consolidado R06B (priorizado, sem fases extras)

1. **Decidir a intenção das EMAs (F1).** Pergunta de negócio, não de código:
   9/21 ou 12/26? Enquanto não houver decisão, aplicar só o **renomeio** para
   `ema12`/`ema26` + ajuste de `regime_service`, `entry_planner` e rótulos —
   risco zero de decisão. Se a escolha for 9/21, tratar como mudança de
   estratégia com backtest dedicado.
2. **Endurecer `_safe` (F2)** com `math.isfinite` e **rejeitar candle mais
   recente inválido (F3)**. Correções pequenas, sem efeito em dado íntegro.
3. **Registrar a fórmula do score e travar a binagem (F5).** Persistir
   `score_formula` e devolver `None` — nunca `p_global` — quando o score não
   pertence à escala dos bins. Isso protege diretamente o sizing.
4. **Renomear `_load_real_pairs` → `_load_shadow_pairs` (F6)** e declarar a
   fonte no contrato da calibração. Só nomenclatura.
5. **Corrigir a semântica do Kelly (F7)** escolhendo o par (p, b) coerente.
   Último da fila por ser a única alteração que mexe em dinheiro; exige
   validação antes de qualquer ativação.

**Não há defeito aritmético comprovado na implementação da EMA.** O que existe
são defeitos de **nomenclatura/contrato** (F1, F6), **robustez** (F2, F3),
**versionamento de escala** (F5) e **semântica de evento** (F7).
