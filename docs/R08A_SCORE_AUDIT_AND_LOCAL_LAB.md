# R08A — Auditoria do score e laboratório local do Score V3

**Baseline:** `7d202144` · **Branch:** `main` · **Modo:** `LOCAL_RESEARCH_ONLY`

Preparação do P08. Entrega (A) o mapa verificável de como a pontuação é
formada, (B) a auditoria de sobreposições e contradições, (C) um laboratório
puro que compara a fórmula **bruta** V2 com **uma** ablação estrutural, e (D)
uma proposta documental por cenário de mercado.

**Não** entrega estratégia pronta para operar e **não** afirma redução de stops
ou aumento de lucro. O laboratório não é importado por nenhum caminho de
produção.

---

## 1. Mapa do caminho real da pontuação

| # | etapa | entrada | transformação | saída | consumidor | configuração | dado histórico |
|---|---|---|---|---|---|---|---|
| 1 | confluência | indicadores, padrões, SMC, derivativos, MTF | soma de fatores, `clamp(0, MAX_TOTAL)`, `pct = total/MAX_TOTAL×100` | `confluence.pct` 0–100 | fórmula V2 e legada | `confluence_service.WEIGHTS` | `features.confluence_pct` |
| 2 | **score bruto** | `confluence_pct`, `adx`, `funding_pct` | `_compute_score_v2`: normaliza, renormaliza sobre presentes, `clamp`, `round(1)` | 0–100 | `_finish_score` | `SCORE_V2_W_CONF/ADX/DER` | os três insumos existem |
| 2b | fallback legado | confluence, MTF, RR, win-rate, derivativos, breakout | `_compute_score_legacy` | 0–100 (outra escala) | `_finish_score` | pesos fixos no código | **componentes não persistidos** |
| 3 | **score-base** | score bruto | `_finish_score`: `×_htf_relevance_mult(tf)`, `clamp`, `round(1)` | 0–100, antes do bônus HTF | passo 4 | `_HTF_WEIGHT`, `HIGH_TF_PATTERNS_ENABLED` | **não isolado em `snapshot.score`** |
| 4 | bônus HTF | score-base + direções confirmadas | `_score_with_htf_confirm`: `+HIGH_TF_CONFIRM_BONUS`, teto 100 | score do candidato | seleção e candidato escolhido | `HIGH_TF_CONFIRM_BONUS` | **não persistido separadamente** |
| 5 | pontuação de **seleção** | score do passo 4 | `_sel_key`: `− CT_BRAKE_SELECT_PENALTY` se contratendência | chave para escolher o candidato, sem alterar seu score | `max(scored, key=_sel_key)` nos caminhos batch/server | `CT_BRAKE_*` | **não persistida** |
| 5b | **auto-learning multiplicativo** | score do candidato escolhido + tier provisório para lookup | `apply_score_adjustment`: multiplicador por buckets; pode bloquear o candidato | score ajustado antes do tier final | passo 6 e `Recommendation.score` | configuração de `learning_service` | resultado agregado em `snapshot.score`, **sem decomposição do ajuste** |
| 6 | tier final | score após passo 5b + gates | `_classify_tier` / `_classify_tier_vision` e gates posteriores | `A+/A/B/None` | recomendação | cortes 75/65/52 ou V2 65/46/18 | `snapshot.tier` |
| 7 | **ajuste aditivo de execução** | `rec.score` + features | `_compute_score_adjustment`: soma deltas, `clamp(±SCORE_ADJUSTER_CAP)` no delta | `rec.score + delta` | comparado a `SCORE_MIN` | `SCORE_ADJUSTERS_ENABLED`, `SCORE_ADJUSTER_CAP` | espelhado em `_execution_score` |

**Separação explícita pedida:** (1) score bruto = passo 2; (2) score-base = passo
3; (3) bônus HTF e auto-learning multiplicativo = passos 4 e 5b; (4) pontuação
de seleção = passo 5, uma chave penalizada que não substitui o score do
candidato; (5) corte de execução = passo 7 comparado a `SCORE_MIN`.

O `snapshot.score` guarda **`rec.score`**, que já pode incluir o bônus HTF e o
auto-learning multiplicativo aplicado **antes do tier final**. Não representa
necessariamente nem o score bruto (passo 2), nem o score-base (passo 3). Esse
score também participa da ordenação final das recomendações por tier/score,
mas não armazena a chave penalizada da seleção do passo 5. O executor parte de
`rec.score` e depois soma os adjusters do passo 7: o número comparado a
`SCORE_MIN` pode ser diferente (ou igual, se o ajuste estiver desligado/zerado).

Rastreabilidade no checkout: `recommendation_service.py:1204,2415` (bônus HTF),
`:2222–2243,2618–2635` (auto-learning antes do tier final), `:1937,2680`
(construção da recomendação) e `snapshot_service.py:1377,1542`
(`score=float(rec["score"])`). Os arquivos estão em `backend/services/`.

> Os valores de configuração citados são os do **checkout local**. Não são, e
> não devem ser apresentados como, configuração confirmada de produção — a
> produção lê ENV no boot dela e este pacote não a consultou.

---

## 2. Auditoria — achados

Categorias usadas, para não exagerar: **sobreposição estrutural** (o mesmo
indicador entra em mais de uma camada), **reforço intencional** (repetição
deliberada e coerente), **assimetria comprovada** (tratamento desigual
demonstrável) e **hipótese não demonstrada** (suspeita sem prova).

Reutilizar o mesmo indicador **não é, por si, defeito**.

### A1 — ADX: camadas com sinais opostos · *assimetria comprovada*

O ADX entra três vezes: na confluência (`ADX>35 → +10`, `25<ADX≤35 → +6`,
`ADX<20 → −3`, em pontos do agregado antes da normalização;
`confluence_service.py:256–276`), como componente externo da V2 (linear até a
saturação, mais é melhor) e no ajuste de execução
(`adx < 20 → +6`, `adx > 30 → −2`).

```
Dois cálculos independentes (não encadeados):
  V2: conf=70, sem funding      |  ajuste isolado: entrada fixa 60, só ADX
  ADX  5 → V2 bruta 50.0       |  60 + 6 = 66.0
  ADX 45 → V2 bruta 76.7       |  60 − 2 = 58.0
```

Os pesos explícitos da V2 são `conf=0.60, adx=0.30, der=0.10`. No teste isolado,
`_exec(adx=...)` **não recebe confluência**, apesar de a V2 usar `conf=70`;
as demais features, inclusive horário, também estão ausentes. A entrada 60 é
fixada artificialmente e o cap do delta é 20. A fórmula bruta sobe **+26,7
pontos** com ADX alto; o ajuste isolado cai **8 pontos**. Isso comprova sinais
opostos entre essas camadas, **não** uma queda do score final do bot. Mantida a
mesma entrada e sem outros deltas, o ADX alto exige 8 pontos a mais dessa entrada
para atingir o mesmo `SCORE_MIN`.

Como contraste, um exemplo local **acoplado de apenas duas etapas** alimenta o
espelho do executor (`_execution_score`) com a V2 bruta e passa `conf=70` e ADX
nas duas etapas, mantendo os pesos acima, cap 20 e demais features ausentes:

```
ADX  5 → V2 50.0 + confluência 12 + ADX 6 = 68.0
ADX 45 → V2 76.7 + confluência 12 − ADX 2 = 86.7
```

Nesse recorte o resultado **sobe 18,7 pontos**. A confluência permanece fixa,
sem recalcular seu ADX interno; HTF, seleção, auto-learning, tiers e demais gates
ficam de fora. **Não é replay completo do bot nem evidência de lucro/prejuízo.**
Que a oposição entre camadas *cause* prejuízo é **hipótese não demonstrada** —
os pesos do ajuste vêm de um estudo de lift (N=237) citado no código, que não
foi reauditado aqui.

### A2 — Confluência não-monotônica no ajuste · *assimetria comprovada*

`conf < 50 → −4`; `50 ≤ conf ≤ 70 → +12`; `conf > 70 → 0`.

Aqui também se isola o ajuste: entrada fixa em 60, cap 20 e apenas
`confluence_pct` presente, sem ADX, funding, horário ou demais features. Os
valores abaixo **não** usam a V2 bruta como entrada:

```
conf 49.9 → 60 + ajuste = 56.0
conf 50.0 → 60 + ajuste = 72.0     (+16 num passo de 0,1)
conf 70.0 → 60 + ajuste = 72.0
conf 70.1 → 60 + ajuste = 60.0     (−12 num passo de 0,1)
```

Entre 70,0 e 70,1 **mais** confluência produz **menos** score nesse cálculo
isolado, enquanto a V2 bruta, calculada separadamente só com confluência, anda
no sentido contrário (70,0 → 70,1). São dois degraus de borda do adjuster, um
deles com inversão de sentido. Isso não mede o pipeline completo nem o efeito
econômico; outros deltas e o cap podem mudar o efeito observado no executor.

### A3 — Funding: três camadas e uma assimetria de lado · *sobreposição + assimetria*

O funding aparece na confluência (categoria `derivatives`, interpretada **com** a
direção: `extreme_long` penaliza long, `bearish_squeeze` favorece short…), de
novo na V2 (`der_n`, que **não recebe direção**) e ainda no ajuste
(`funding_sentiment == neutral → +6` **e** `|funding| ≤ 0.05 → +6`, dois deltas
para fatos quase equivalentes).

```
funding = −0.10, conf = 70:
  V2 bruta (long)  = 74.3
  V2 bruta (short) = 74.3     ← idêntico; der_n = 100 nos dois casos
```

Funding negativo **sempre** eleva o componente da V2, seja a operação long ou
short — porque `_compute_score_v2` não recebe o lado. Na confluência, o mesmo
funding tem leitura direcional. Isso é assimetria de tratamento entre camadas,
comprovada numericamente.

### A4 — Saturação esconde diferença real · *sobreposição estrutural*

```
conf=70: ADX 50 · 75 · 100 → todos 80.0
conf=70: funding 0.05 · 0.5 · 5.0 → todos 60.0
```

`adx_n` satura em ADX 50 e `der_n` satura em |funding| 0,05. Acima disso a
fórmula bruta é cega a diferenças reais de mercado.

### A5 — Arredondamento pode esconder mudança · *sobreposição estrutural*

```
conf 70.00 · 70.01 · 70.04 → todos 70.0
```

O `round(…, 1)` final absorve variações abaixo de 0,05 ponto. Com peso efetivo
0,6 na confluência, isso equivale a ignorar até ~0,08 ponto de confluência.

### A6 — Ausência renormaliza e muda o peso efetivo · *reforço intencional*

```
conf=70, adx=30, funding=0.0 → peso efetivo da confluência = 0.600
conf=70, sem adx, sem funding → peso efetivo da confluência = 1.000
```

Isto é **deliberado** (`dado faltante NÃO ancora em 50`, como o próprio código
diz) e está correto. Registrado aqui porque muda a interpretação: dois snapshots
com a mesma confluência podem ter scores diferentes só pela presença dos outros
componentes.

### A7 — Score persistido e score do gate são etapas distintas · *sobreposição estrutural*

O passo 4 pode somar `HIGH_TF_CONFIRM_BONUS` e o passo 5b pode aplicar um
multiplicador de auto-learning, **antes** do tier final. `snapshot.score`
persiste esse `rec.score`, não o passo 3 isolado. A penalidade de seleção do
passo 5 afeta a escolha, mas não é deduzida do score persistido. Só depois o
executor soma até `±SCORE_ADJUSTER_CAP` (passo 7), se habilitado. Portanto,
comparar diretamente `snapshot.score` com `SCORE_MIN` ignora esse ajuste
posterior — é por isso que `strategy_evidence_service._execution_score`
existe. Com delta zero/desligado, os números podem coincidir.

### A8 — RSI/Stochastic de reversão vs. tendência EMA · *hipótese não demonstrada*

A confluência premia RSI sobrevendido e Stoch baixo (mean-reversion), enquanto o
ajuste de execução aplica `rsi < 30 → −7` e o freio contratendência penaliza
entrar contra a EMA dos TFs superiores. Há tensão conceitual entre "comprar o
fundo" e "seguir a tendência", mas **não** demonstrei que ela produz pior
resultado — isso exigiria a comparação econômica que este pacote não faz.

### A9 — MTF em duas camadas · *hipótese não demonstrada*

`mtf` tem peso 30 na confluência, e o passo 4 soma um bônus separado por
confirmação de TF alto. Pode ser reforço intencional; medir se é redundância
exigiria decompor a confluência histórica, que **não** é persistida.

---

## 3. O laboratório

`backend/services/score_research_service.py`. `execution_mode =
LOCAL_RESEARCH_ONLY`, `promotable = false`, `calibrated = false`.

**Entradas** explícitas e versionadas: `confluence_pct`, `adx`, `funding_pct`,
`direction` (só descritiva) e os pesos por argumento. Nenhuma função matemática
lê ENV, relógio, banco, arquivo, cache ou rede.

**Contratos:** zero legítimo é aceito; `None` não é zero; `bool`, string
numérica, `NaN` e infinito são recusados; valores fora dos domínios documentados
(`confluence_pct` e `adx` em 0–100, `funding_pct` em −100–100) são recusados.
Pesos precisam ser finitos, não negativos e com soma positiva — configuração
inválida **não** é corrigida em silêncio.

Mesmo pesos individualmente finitos podem causar overflow: a soma dos pesos,
o acumulador ponderado e o quociente bruto precisam permanecer finitos **antes
do clamp**. Caso contrário, o laboratório retorna `INVALID_CONFIG` /
`WEIGHTS_ARITHMETIC_OVERFLOW`, `score=None` e nenhuma contribuição, sem fabricar
score 100. A comparação fica indisponível, com `delta=None`. Esse reforço é
local ao laboratório e não altera a fórmula de produção.

**Saída:** `schema_version`, `formula_id`, `status`, `reason_code`, `score` ou
`None`, componentes normalizados, pesos efetivos, contribuições, componentes
ausentes, `config_hash` (SHA-256 de JSON canônico), `execution_mode`,
`promotable=false`, `calibrated=false`. Nenhum resultado futuro entra no hash.
Falha nunca produz score neutro, fallback para outra fórmula, nem probabilidade.

### Baseline: "V2 bruta sob configuração explícita"

```
conf_n = confluence_pct
adx_n  = clamp(adx, 0, 50) / 50 × 100
der_n  = 50 − clamp(funding_pct / 0.05, −1, +1) × 50
score  = round(clamp(Σ wᵢ·xᵢ / Σ wᵢ, 0, 100), 1)   sobre os PRESENTES
```

Paridade verificada contra `_compute_score_v2` **real** em matriz de entradas
válidas, em todas as 8 combinações de ausência, com quatro conjuntos de pesos
alternativos e nas bordas de clamp/arredondamento. Não é replay fiel do bot nem
o score histórico final: passos 3–7 ficam de fora de propósito.

### Ablação: `SCORE_V3_CONF_ONLY_ABLATION`

Score bruto = `confluence_pct`. Não adiciona ADX nem funding **por fora** da
confluência; sem confluência ⇒ `UNAVAILABLE`, sem renormalização e sem
substituto.

**Finalidade:** medir quanto as camadas externas de ADX/funding deslocam a
pontuação em relação ao agregado de confluência.

```
conf=70, sem funding:
  ADX  5 → V2 50.0 · ablação 70.0 · delta +20.0
  ADX 45 → V2 76.7 · ablação 70.0 · delta  −6.7
```

**Isto não prova** que toda dupla contagem foi eliminada (o ADX continua dentro
da confluência), nem que a confluência isolada opera melhor, nem que o candidato
deva substituir a V2. É uma ablação estrutural provisória — **não** é o Score V3
definitivo. Não há busca de pesos, grade de parâmetros, variante por símbolo,
limiar escolhido pelo resultado, outro candidato, nem regra de entrada/saída.

Quando um dos lados não é calculável, a comparação é **indisponível** e o delta
é `None` — nunca zero.

---

## 4. Proposta por cenário (documental, não ativada)

Reutilizando as definições e cautelas do R07A — `NORMAL` não significa
lateralização; `mtf.aligned_count` não é tendência EMA; padrão tipado não prova
rompimento; falta de contexto não é cenário favorável.

| cenário | famílias de evidência que **poderiam** pesar diferente | prova necessária antes de qualquer mudança |
|---|---|---|
| tendência alinhada | seguimento (EMA, ADX, MTF) acima de reversão (RSI/Stoch) | mostrar que o par (tendência, reversão) tem expectativas separáveis nesse subconjunto |
| contratendência | reversão e estrutura acima de seguimento | separar o efeito de **pontuação** do efeito de **abstenção** (H1 do R07A) |
| canal horizontal detectado | estrutura e VP/VWAP acima de tendência | limites do range, entrada na borda e volume — **nada disso é persistido hoje** |
| rompimento/retest comprovado | confirmação e volume acima de mean-reversion | `breakout_confirmed`/`retest_active` com direção, em amostra suficiente |
| contexto desconhecido | nenhuma — manter a fórmula vigente | por contrato, ausência não vira cenário favorável |

**Nenhum multiplicador ou peso por regime foi inventado.** A ablação **não** foi
combinada com H1/H2/H3, e a proposta **não** virou veto nem score operacional. A
mudança de score e a política de abstenção precisam continuar distinguíveis para
que uma avaliação futura consiga separar seus efeitos.

---

## 5. Limites do histórico

Os snapshots guardam `confluence_pct`, `adx` e `funding_pct` — o suficiente para
investigar a fórmula bruta V2 sob pesos explícitos. **Não** guardam: pesos e
configuração históricos, decomposição individual da confluência, bônus HTF
efetivamente aplicado, configuração e resultado do auto-learning, nem todos os
componentes do fallback legado.

Portanto, e por contrato deste pacote: não decompor confluência histórica por
estimativa; não reconstruir fatores ausentes; não atribuir diferença de score a
uma causa sem prova; não usar a configuração atual como se fosse a histórica;
não usar `snapshot.score` como referência da fórmula bruta.

**Nenhum dado de produção foi carregado.** Não há loader, coleta nova nem
backfill. O holdout não foi acessado.

Probabilidades e tiers: a ablação **não** usa os bins da V2, `prob_tp1`/`prob_tp2`
não são recalculados, Kelly não é aplicado, nenhum tier é atribuído e nenhum
tamanho é sugerido. **Score é pontuação, não probabilidade de lucro.** Diferença
de pontuação **não** estima lucro, stops evitados ou volume real.

---

## 6. Próximo passo possível (não implementado)

Para que a ablação deixasse de ser estrutural e virasse candidata avaliável
seria preciso, no mínimo: (a) persistir a decomposição da confluência e a
configuração vigente no instante da recomendação, para que a comparação
histórica deixe de depender de estimativa; (b) uma comparação econômica com o
mesmo rigor do R07A — universo comparável, `UNKNOWN` excluído simetricamente,
bootstrap pareado, treino/validação separados e holdout selado; e (c) uma
decisão explícita sobre as contradições A1 e A2, que hoje são de **camada**, não
de fórmula — mudar a V2 sem mexer no ajuste de execução pode não alterar nada no
gate. Nada disso foi iniciado aqui.

**R08A pode estar tecnicamente concluído sem provar que o candidato melhora
resultados** — e é esse o caso.
