# R06B2.1 — Fechamento do contrato de score/calibração

**Baseline:** `8ae87567` (R06B2) · **Branch:** `main` · **Escopo:** corrigir as
quatro lacunas comprovadas na revisão do R06B2. Nada mais.

Estratégia, score, tier, EMAs, stop, TP, caps, `KELLY_FRACTION`, a fórmula de
Kelly, `p_global`, PAV, shrinkage, os bins numéricos e a amostra ficam
exatamente como estavam. R06B3 não foi iniciado.

---

## Lacuna 1 — a construção do contrato podia falhar ABERTA

**Antes.** Em `_build_recommendation`, qualquer exceção no import ou no lookup
caía num `except` que zerava tudo: `prob_tp1=None`, `prob_tp2=None` e
`probability_provenance=None`. Se o score não tivesse vindo de fallback de
fórmula, o gate do R06B2 via "sem proveniência, sem fallback" e **deixava
passar**. Um erro interno virava recomendação sem contrato nenhum.

**Depois.** A falha vira um contrato inválido **explícito**:

```
status      = INVALID_CALIBRATION_CONTRACT
reason_code = PROBABILITY_LOOKUP_FAILED     (novo, no vocabulário fechado)
prob_tp1    = None      prob_tp2 = None
fallback_used  conforme o ScoreProvenance
```

Consequências, todas cobertas por teste comportamental: sizing dinâmico
suspenso, `bot_verdict` bloqueado por `calibration-contract` e autoexecução
barrada antes de qualquer ordem. **Nenhuma mensagem da exceção é propagada** —
o teste injeta uma exceção com uma string sentinela e verifica que ela não
aparece em nenhum lugar do payload serializado.

Os três literais usados nesse caminho (`_CALIB_CONTRACT_VERSION`,
`_CALIB_STATUS_INVALID`, `_CALIB_REASON_LOOKUP_FAILED`) são constantes locais de
`recommendation_service`, porque o caminho de falha não pode depender do import
que acabou de falhar. Um teste trava a igualdade com `calibration_service`.

**Nenhuma Recommendation nova fica sem `probability_provenance`.**

---

## Lacuna 2 — `status` isolado simulava um contrato válido

**Antes.** `calibration_contract_verdict` decidia essencialmente pelo campo
`status`. Um dicionário incompleto — literalmente `{"status": "READY"}` — era
aceito como se tivesse saído do lookup.

**Depois.** O helper único valida o contrato inteiro.

`READY` só passa se **todas** valerem:

- `contract_version` está em `SUPPORTED_CONTRACT_VERSIONS`;
- `reason_code` é reconhecido;
- `score_formula_effective` e `calibration_formula` são conhecidas **e iguais**;
- `bins_version` tem forma válida (`<FÓRMULA>:sha256:<64 hex>`);
- `bin_index` é `int`, não `bool`, e `>= 0`;
- `fallback_used` é **exatamente** `False`;
- `prob_tp1` e `prob_tp2` da rec são finitos em `[0,1]`;
- `prob_tp2 <= prob_tp1` com a tolerância documentada (`1e-9`).

`CALIBRATION_UNAVAILABLE` só passa sem bloquear se o envelope for reconhecido,
as duas probabilidades estiverem **ausentes** e o contrato **não** afirmar um bin.

Bloqueiam: proveniência presente mas malformada; `status` desconhecido; `READY`
incompleto; `CALIBRATION_UNAVAILABLE` com probabilidade preenchida; e
`score_provenance.fallback_used=true` com proveniência ausente. Os quatro
estados bloqueantes do R06B2 continuam bloqueantes.

**Compatibilidade.** Payload realmente legado (sem nenhuma das duas
proveniências) continua legível em visualização histórica. O caminho oficial de
autoexecução passa `require_current_contract=True` no **mesmo** helper — sem
segunda implementação da regra — e aí contrato ausente também bloqueia.
`contract_is_blocking(...)` é só um atalho booleano sobre o mesmo helper.

---

## Lacuna 3 — a versão dos bins não cobria as bordas internas

**Antes.** `bins_version` era `"<fórmula>:n<qtd>:<lo>-<hi>"`. Repartir os bins
por dentro mantendo fórmula, quantidade e extremos produzia a **mesma** string:
um score podia ser lido por uma partição diferente daquela em que a tabela foi
treinada, sem que nada acusasse.

**Depois.** `bins_fingerprint()` é o SHA-256 do JSON canônico
(`sort_keys=True`, separadores `(",", ":")`) contendo **somente**:

```json
{"bins": [[15.0, 31.0], ...], "calibration_formula": "SCORE_V2"}
```

Nenhuma data, probabilidade ou dado de amostra entra no hash — senão ele mudaria
a cada recalibração. `bins_version()` é `"<FÓRMULA>:sha256:<hex>"`.

`probability_for_score` **recomputa e confere** o fingerprint, e passou a validar
o contrato inteiro da calibração, nesta ordem:

1. `contract_version` suportada — senão `CONTRACT_VERSION_UNSUPPORTED`;
2. `total_resolved` numérico finito não-bool — senão `TOTAL_RESOLVED_INVALID`;
   abaixo de `MIN_SAMPLE_TOTAL` ⇒ `CALIBRATION_UNAVAILABLE` (não é bloqueio);
3. fórmulas conhecidas, `bins_version` presente, bins bem formados;
4. partição **ordenada** (`BINS_UNSORTED`) e **sem sobreposição**
   (`BINS_AMBIGUOUS`);
5. `score_range` coincide com a primeira e a última borda — senão
   `SCORE_RANGE_MISMATCH`;
6. `bins_version` == fingerprint recomputado — senão `BINS_VERSION_MISMATCH`.

Qualquer divergência ⇒ `INVALID_CALIBRATION_CONTRACT`, sem probabilidade, sem
`p_global` e com a autoexecução bloqueada. **As bordas atuais não mudaram** — um
teste trava `SCORE_BINS_V2` e `SCORE_BINS_LEGACY` nos valores de hoje.

---

## Lacuna 4 — snapshots recalculavam a probabilidade histórica

**Antes.** `snapshot_service` chamava `prob_tp1_for_score_sync` /
`prob_tp2_for_score_sync` na hora de exibir. Esses wrappers não conhecem a
fórmula que gerou o score histórico: um snapshot de semanas atrás era
reinterpretado pela calibração de **hoje**, possivelmente de outra fórmula ou
outra partição.

**Depois.** `_extract_features` congela, dentro do `features` que já existe, um
namespace versionado:

```json
"probability_contract": {
  "schema_version": 1,
  "score_provenance": {...},
  "probability_provenance": {...},
  "prob_tp1": 0.55,
  "prob_tp2": 0.25
}
```

A exibição usa `probabilities_from_contract(payload)`, que devolve os valores
**persistidos** e só quando o contrato gravado valida como `READY` pelo mesmo
helper. Regras:

- histórico sem proveniência (registro anterior a este pacote) ⇒ `None`/`None`;
- contrato incompatível ou malformado ⇒ `None`/`None`;
- **zero legítimo permanece zero** — a checagem é por `None`, não por falsidade;
- ausência nunca vira zero.

Nada é recalculado com o cache atual: um teste troca a calibração do processo
inteiro (outra fórmula, outros bins) e prova que o valor lido não muda.

Nenhum registro histórico foi reescrito, nenhum backfill foi feito, nenhum
outcome foi aberto e o P05 não foi tocado — o namespace do P05 permanece
isolado. Sem coluna, tabela ou migration.

Os wrappers continuam **definidos** para compatibilidade, mas uma varredura de
todo o backend (fora de `tests/`) prova que só a própria definição em
`calibration_service` os menciona: nenhum caminho de recomendação, execução ou
snapshot depende deles.

---

## Extra — sizing deixou de depender de um import interno

`_compute_dynamic_size` importava `BLOCKING_PROB_STATUSES` dentro da função e,
se o import falhasse, usava um conjunto **vazio** — um erro interno virava,
silenciosamente, sizing pelo fallback por tier.

Agora o chamador decide e passa `probability_contract_blocking: bool`. Contrato
bloqueante ⇒ sem `prob_tp1`, sem `_TIER_WR_FALLBACK`, `size=None` e rationale
controlada. `READY` e `CALIBRATION_UNAVAILABLE` preservam exatamente o
comportamento do R06B2 — provado comparando a chamada com e sem o parâmetro.
A fórmula de Kelly, os caps e o fallback por tier no caso legítimo de calibração
indisponível continuam intactos.

---

## Testes

`backend/tests/test_r06b2_1_final_contract_closure.py` — **65 testes
herméticos** (rede/DNS bloqueados e contabilizados; sem exchange, banco de
produção, credencial, seed privado ou holdout; nenhuma ordem real). Sem `skip`
nem `xfail` escondendo falha.

Blocos: falha de construção (8) · veredito integral (18) · fingerprint (12) ·
snapshot imutável (10) · execução e sizing (9) · escopo (3). Os testes são
comportamentais — constroem recomendações reais, injetam exceções, trocam a
calibração do processo e conferem o resultado; grep aparece só como reforço.

Os testes do R06B2 foram ajustados ao contrato mais estrito: a fixture de
recomendação passou a produzir um contrato **bem formado** (envelope íntegro,
`bin_index` coerente, probabilidade presente só em `READY`), o parâmetro de
sizing acompanhou o rename, e dois asserts de escopo foram fixados no range de
commits do R06B2 (`14102771..8ae87567`).

---

## Limitações

- **`CALIBRATION_UNAVAILABLE` continua distinto** e não bloqueia: calibração
  imatura preserva o comportamento anterior, inclusive o fallback de sizing.
- **`p_global` não substitui probabilidade individual** — segue válido só como
  estatística agregada.
- **O congelamento vale só para snapshots novos.** Registros anteriores a este
  pacote não têm o namespace e passam a exibir ausência em vez de um número
  recalculado. Isso é a correção, não um efeito colateral: o número anterior era
  reinterpretação, não histórico.
- **A validação de `probabilities_from_contract` é sobre o contrato gravado**,
  não sobre a calibração que o produziu — o hash congelado não é reconferido
  contra uma tabela que talvez nem exista mais.
- **A prova de que `_decision_fields` usa o caminho novo é composicional**: os
  testes exercitam `_extract_features` e `probabilities_from_contract` reais e
  conferem o wiring por leitura de código, porque a função que os une
  (`get_daily_pnl`) exige banco e não roda hermeticamente.
- **A semântica financeira do Kelly continua pendente (R06B3).**
- **Nenhuma conclusão de lucratividade foi produzida.** Não houve backtest,
  recalibração de produção nem acesso a exchange.
- A tolerância `1e-9` em `prob_tp2 <= prob_tp1` segue valendo, com o mesmo risco
  operacional já registrado no R06B2: inversão maior que isso bloqueia a
  autoexecução até investigação.
- Testes travam contratos; não provam ausência de bugs.
