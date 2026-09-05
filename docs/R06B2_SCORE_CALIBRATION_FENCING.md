# R06B2 — Cerca entre score, calibração e autoexecução

**Baseline:** `14102771` (R06B1) · **Branch:** `main` · **Escopo:** impedir que um
score produzido por uma fórmula seja interpretado pelos bins de outra.

Nada de estratégia nova. Score, tier, EMAs, stop, TP, caps, `KELLY_FRACTION`,
`CONVICTION_MULT_MAX` e a fórmula de Kelly ficam exatamente como estavam.

---

## 1. O defeito

Comprovado no R06A (F5) e tornado observável no R06B1:

```
SCORE_V2 solicitada
  → cálculo V2 indisponível
  → fallback silencioso para LEGACY_V1        (R06B1 passou a registrar isto)
  → os bins da calibração continuam sendo os V2
  → o lookup devolvia p_global (ou a probabilidade de um bin de outro contrato)
  → esse número alimentava o gate de P(TP1) e o sizing como se fosse calibrado
```

`p_global` é a taxa de acerto **agregada** de toda a tabela. Usá-la como
probabilidade **individual** de um score sem bin compatível é inventar
informação — e essa informação entrava no Kelly.

## 2. Depois

Score, fórmula, bins e probabilidade passam a ter contrato explícito. A
incompatibilidade bloqueia **somente a autoexecução afetada**; o resto do
sistema segue igual.

---

## 3. Contrato do lookup

`CalibrationProbabilityResult` (NamedTuple, em `calibration_service`):

| campo | conteúdo |
|---|---|
| `prob_tp1` / `prob_tp2` | preenchidos **somente** em `READY`; ausência (`None`) em todo o resto |
| `status` | vocabulário fechado (abaixo) |
| `reason_code` | vocabulário fechado, granular |
| `score` | o score avaliado, já validado como número finito |
| `score_formula_effective` | a fórmula que **de fato** produziu o score |
| `calibration_formula` | a fórmula que os **bins aceitam** |
| `bins_version` | identificador estável da configuração de bins |
| `bin_index` | índice do bin, quando existe exatamente um |
| `fallback_used` | veio do `ScoreProvenance` do R06B1 |

A função `probability_for_score(score, score_formula_effective, calibration, *,
fallback_used)` é **pura**: recebe a calibração já carregada e não consulta
banco, rede, ENV nem exchange. Um teste faz o grep sobre o código (sem
docstrings) provando isso.

Ordem de avaliação, fixa e determinística:

```
score inválido → calibração ausente → contrato inválido → mismatch
  → fora do range → READY
```

### Estados

| status | quando | probabilidade | bloqueia autoexecução |
|---|---|---|---|
| `READY` | tudo confere | do bin | não |
| `CALIBRATION_UNAVAILABLE` | cache vazio, sem bins, amostra < `MIN_SAMPLE_TOTAL` | ausente | **não** |
| `FORMULA_MISMATCH` | `formula_effective != calibration_formula` | ausente | sim |
| `SCORE_OUT_OF_RANGE` | fórmula compatível, score sem bin | ausente | sim |
| `INVALID_SCORE` | `None`, `bool`, string, `NaN`, `±inf` | ausente | sim |
| `INVALID_CALIBRATION_CONTRACT` | metadata malformada, probabilidade fora de `[0,1]`, `TP2 > TP1`, bins ausentes/ambíguos, fórmula desconhecida | ausente | sim |

`READY` exige **todas** as condições: score finito; `formula_effective`
presente e reconhecida; calibração existente; `calibration_formula` presente e
reconhecida; as duas iguais; score em **exatamente um** bin; probabilidades
finitas em `[0,1]`; e `prob_tp2 <= prob_tp1`.

**`CALIBRATION_UNAVAILABLE` não é `FORMULA_MISMATCH`.** Calibração imatura é o
estado normal de quem ainda não tem amostra: mantém o comportamento operacional
anterior, não bloqueia nada e não é chamada de `READY`.

---

## 4. Identidade dos bins

`compute_calibration_from_pairs` passa a declarar:

- `contract_version` — `r06b2.1`
- `calibration_formula` — `SCORE_V2` ou `LEGACY_V1`, seguindo a mesma flag dos bins
- `bins_version` — derivado da configuração: `"<fórmula>:n<qtd>:<lo>-<hi>"`,
  estável entre execuções e diferente entre V2 e legado
- `score_range` — faixa coberta
- `source` e `total_resolved` — já existiam

Os literais de fórmula são repetidos em `calibration_service` para não importar
`recommendation_service` (evita ciclo); um teste do R06B2 trava a igualdade com
`ScoreProvenance.formula_*` dos dois lados.

### Honestidade sobre a amostra

`calibration_formula` diz **qual fórmula os bins aceitam** — não é uma
afirmação retroativa de que cada par histórico tenha proveniência conhecida.
Pares resolvidos antes do R06B1 não carregam a fórmula que gerou seu score.
Isso fica registrado em `pairs_formula_provenance = "UNVERSIONED_PRE_R06B1"`.
A fórmula individual **não é inferida pelo valor do score** e nenhum snapshot
histórico foi reescrito. Nenhuma coluna, tabela ou migration foi criada.

---

## 5. Remoção do fallback para `p_global`

Os três lookups legados (`prob_tp1_for_score` async, `prob_tp1_for_score_sync`,
`prob_tp2_for_score_sync`) deixaram de devolver `p_global` / `p_tp2_global`
quando o score não cai em nenhum bin: agora devolvem ausência.

`p_global` **continua existindo** na tabela de calibração e no preview A/B —
como estatística agregada, que é o que ele sempre foi. O que acabou foi o uso
dele como probabilidade individual.

Esses wrappers permanecem para compatibilidade de leitura e diagnóstico, mas
**não conhecem a fórmula efetiva**, então não têm cerca. Nenhum caminho
operacional novo deve depender deles: o caminho com contrato é
`probability_for_score`.

---

## 6. Integração na recomendação

Em `_build_recommendation`, o `ScoreProvenance` passou a ser calculado **antes**
do lookup, e é o `formula_effective` dele — não a flag global — que governa qual
conjunto de bins pode ler aquele score.

A `Recommendation` ganhou `probability_provenance` (JSON já serializado, sem
coluna) com exatamente: `contract_version`, `status`, `reason_code`,
`score_formula_effective`, `calibration_formula`, `bins_version`, `bin_index`,
`fallback_used`. Sem stack trace, sem mensagem crua de exceção, sem dado pessoal.

- `READY` → `prob_tp1`/`prob_tp2` preenchidos;
- qualquer outro estado → ambos `None` (ausência, nunca zero);
- payload antigo (sem os campos) continua legível no backend e no frontend.

---

## 7. Bloqueio da autoexecução

`prob_tp1=None` sozinho **não** bloqueava nada — o gate de P(TP1) é no-op-safe
para calibração imatura. Retornar `None` não encerraria o defeito.

Por isso existe um helper único e puro: `calibration_contract_verdict(rec)`,
definido **uma única vez** em `calibration_service` e reusado por:

1. `exec_verdict` (o veredito que o app exibe),
2. a avaliação de qualidade que alimenta `entry_grade`,
3. o loop oficial de autoexecução (`open_shadow_for_recs`).

No `shadow_trade_service` há apenas uma ponte (`_calibration_contract_verdict`),
**fail-closed**: se o contrato não puder ser avaliado — import ou execução
falhando — a rec não vira ordem. A regra não é reimplementada em lugar nenhum.

Bloqueia com o código controlado **`calibration-contract`** quando:

- o status é `FORMULA_MISMATCH`, `SCORE_OUT_OF_RANGE`, `INVALID_SCORE` ou
  `INVALID_CALIBRATION_CONTRACT`;
- ou a rec traz `score_provenance.fallback_used = true` mas o
  `probability_provenance` está ausente, desconhecido ou malformado.

Não bloqueia quando o estado é explícito e válido: `READY` ou
`CALIBRATION_UNAVAILABLE`.

O gate roda entre o news-gate e o R:R gate — **antes de qualquer criação de
ordem** (`place_order` e `place_maker_entry_then_protect`), o que é travado por
um teste de ordenação sobre o código-fonte da função. O skip é registrado em
`_record_skip(rec, "calibration-contract", …)` com status e motivo legíveis,
sem segredo.

---

## 8. Sizing

`_compute_dynamic_size` ganhou um parâmetro keyword-only `probability_status`.
Em estado bloqueante:

- **não** usa `prob_tp1`;
- **não** cai no `_TIER_WR_FALLBACK` (esse fallback existe para calibração
  imatura, não para probabilidade inválida ou de outra fórmula);
- devolve `size = None` com rationale controlada: *"Calibração incompatível com
  a fórmula deste score — sizing dinâmico suspenso"*.

Em `READY` e em `CALIBRATION_UNAVAILABLE`, o resultado é **idêntico** ao de
antes — provado por teste que compara a chamada com e sem o parâmetro.

A fórmula de Kelly não mudou; ela apenas não é alcançada no estado bloqueante.
No sizing por convicção do `shadow_trade_service`, sem probabilidade o
multiplicador já é `1.0` (no-op) e a entrada já foi barrada pelo helper comum —
nenhum multiplicador contorna o bloqueio. `KELLY_FRACTION`,
`CONVICTION_MULT_MAX`, os caps e as faixas de tamanho estão inalterados, e
nenhuma flag foi ativada.

---

## 9. Frontend

Componentes existentes, sem novo layout:

- `READY` → P(TP1)/P(TP2) como sempre;
- estado bloqueante → **sem percentual**, com o texto curto *"Calibração
  incompatível com a fórmula deste score."*;
- `CALIBRATION_UNAVAILABLE` → *"Calibração ainda indisponível"*, só onde há
  espaço adequado;
- payload antigo (sem `probability_provenance`) → nada muda.

Nenhum código interno é exibido, nada de `[object Object]`, nenhum botão é
bloqueado no cliente — a decisão oficial continua no backend. Filtros,
ordenação e score intocados; `frontend/dist` não foi editado.

---

## 10. Testes

Arquivo novo `backend/tests/test_r06b2_score_calibration_fencing.py` — **70
testes herméticos** (rede/DNS bloqueados e contabilizados; sem exchange, banco
de produção, credencial, seed privado ou holdout; nenhuma ordem real). Sem
`skip` e sem `xfail` escondendo falha.

Blocos: contrato do lookup (21) · identidade dos bins (5) · remoção do
`p_global` (5) · integração na recomendação (8) · bloqueio da autoexecução (10)
· sizing (6) · regressão e escopo (6) · frontend (6). Um teste varre **todos**
os casos não-`READY` e prova que nenhum devolve `p_global`.

No R06A, o teste que caracterizava o fallback para `p_global` virou
`test_CORRIGIDO_fora_do_range_nao_cai_mais_no_global`. Dois asserts de escopo do
R06B1 que comparavam a árvore de trabalho com `1f336a0f` foram fixados no range
de commits do R06B1 (`1f336a0f..14102771`) — mesma garantia, sem quebrar a cada
fase autorizada seguinte.

---

## 11. Limitações — o que este pacote NÃO resolve

- **`CALIBRATION_UNAVAILABLE` não é `FORMULA_MISMATCH`** e não bloqueia nada:
  calibração imatura preserva o comportamento anterior, inclusive o fallback de
  sizing por tier.
- **`p_global` não é substituto de probabilidade individual** — segue válido
  apenas como estatística agregada.
- **Dados históricos anteriores ao R06B1 não têm proveniência individual
  garantida.** A cerca protege o lookup daqui pra frente; ela não reconstrói a
  origem dos pares que treinaram a tabela, e nada foi inferido pelo valor.
- **A semântica financeira do Kelly continua pendente (R06B3):** `p = prob_tp1`
  (tocar o TP1) com `b = risk_reward` (alvo final) segue otimista.
- **Nenhuma conclusão de lucratividade foi produzida.** Não houve backtest,
  recalibração de produção nem acesso a exchange.
- A checagem `prob_tp2 <= prob_tp1` usa folga de `1e-9` para arredondamento.
  Se alguma tabela real produzir inversão maior que isso, o estado será
  `INVALID_CALIBRATION_CONTRACT` — fail-closed, o que bloqueia autoexecução até
  a causa ser investigada. É o lado seguro, mas é um risco operacional real e
  fica registrado aqui.
- Testes travam contratos; não provam ausência de bugs.

---

## 12. Próximo passo (R06B3)

Corrigir a semântica financeira do Kelly: hoje `p` é a probabilidade de **tocar
o TP1** e `b` é o R:R até o **alvo final**, o que superestima a fração. Com o
contrato de probabilidade já cercado, dá para escolher explicitamente entre
usar `p = P(TP1)` com `b` do TP1, ou uma probabilidade do alvo final — e medir
o efeito no tamanho antes de mudar qualquer coisa que mexa em dinheiro.
