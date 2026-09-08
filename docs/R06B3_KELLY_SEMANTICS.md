# R06B3 — Coerência do Kelly consultivo, unidades e sugestão de risco

**Baseline:** `eab20a5b` · **Branch:** `main` · **Modelo:** `TP1_BINARY_PROXY_V1`
· **Modo:** `ADVISORY_ONLY` · **Unidade:** `BANKROLL_RISK_PCT`

Fecha o F7 do R06A. Estratégia, EMA 12/26, score, tier, bins, PAV, shrinkage,
stops, alvos, parciais, `risk_pct`, `leverage`, `qty`, `LIVE_SIZE_MULT`, flags e
circuit breakers permanecem inalterados.

---

## 1. Antes e depois

### Fórmula

| | antes | depois |
|---|---|---|
| probabilidade | `p = P(TP1)` | `p = P(TP1)` (igual) |
| payoff | `b = risk_reward` do **alvo final** | `b = |tp1 − entry| / |entry − stop_loss|` |
| Kelly | `(p·b − (1−p)) / b` | `p − (1 − p)/b` |
| piso em `b` | `max(risk_reward, 0.5)` | **nenhum** |
| prob ausente | presumida por tier (`_TIER_WR_FALLBACK`) | ausência (`UNAVAILABLE`) |
| Kelly ≤ 0 | `None` | `0.0` (`NO_POSITIVE_EDGE`) |
| `raw_pct == 0` | piso ressuscitava para 0,25% | `0.0` (`ZERO_REFERENCE`) |

As duas formas de Kelly são algebricamente equivalentes **para o mesmo `b`**; o
defeito era o `b`, que vinha do alvo final enquanto `p` era do TP1. Como
`P(TP1) ≥ P(alvo final)`, a fração saía otimista.

### Unidade

Antes o app dizia *"Size sugerido X% da banca"* e o comentário do código dizia
"o TAMANHO da posição". O número nunca foi tamanho de posição, notional nem
margem: é uma **fração teórica de risco da banca**. O rótulo agora é
**"Referência de risco até o TP1"**.

---

## 2. Os dois caminhos

**Consultivo (este pacote)**

```
prob_tp1 (contrato vigente) + geometria entry/stop_loss/tp1
  → kelly_full → raw_pct → caps → edge/liq espelhados → suggested_size_pct
```

**Operacional (intocado)**

```
risk_pct → multiplicadores do executor → _compute_qty → LIVE_SIZE_MULT → ordem
```

`suggested_size_pct` **nunca** alimentou o executor, e continua sem alimentar.
Verificado por teste comportamental, não só por grep: com a mesma recomendação
operacional, variar apenas os campos consultivos entre positivo, zero e ausente
não move `exec_verdict`, `_edge_mult`, `_liq_tier_mult`, `_conviction_mult`,
`risk_pct`, `_compute_leverage` nem a `qty` de `_compute_qty`.

Espelhar edge/liq no número exibido é escolha de apresentação — não torna esta
referência igual ao tamanho executado.

---

## 3. O modelo, e o que ele assume

`kelly_full = p − (1 − p)/b`, com `+b` ganho hipotético (RR até o TP1) e `−1R`
perda hipotética. **Hipóteses, não observações:**

- **saída integral no TP1.** O bot real tem parciais, runner e trailing; o Kelly
  exato dele não é este;
- **expiração tratada como −1R.** A calibração inclui expirações no conjunto
  resolvido, mas o resultado financeiro delas não é −1R;
- **custos, funding e slippage fora.** Isto não é resultado líquido.

`score_mult`, `vol_mult` e os caps `[0,25%, 1,0%]` são **ajustes heurísticos**:
o valor ajustado não é o Kelly puro. Referência matemática do Kelly binário:
<https://theory.stanford.edu/~blynn/pr/kelly.html>.

---

## 4. Estados

| status | quando | valor |
|---|---|---|
| `READY` | contrato válido, geometria e entradas válidas, `raw_pct > 0` | número |
| `ZERO_REFERENCE` | Kelly positivo mas heurísticas zeram (`score = 0`) | `0.0` |
| `NO_POSITIVE_EDGE` | `kelly_full ≤ 0` | `0.0` |
| `UNAVAILABLE` | contrato bloqueante, sem probabilidade, geometria/preço/score/ATR inválidos, falha interna | `None` |

`None` e `0.0` são coisas diferentes: ausência de dado versus resposta do
modelo. Probabilidade zero é válida (nunca `p or fallback`).

A probabilidade só entra se `calibration_contract_verdict(...,
require_current_contract=True)` aprovar o payload **completo** — não basta
`status == READY`. `CALIBRATION_UNAVAILABLE` passa no gate operacional mas não
entrega probabilidade: aqui vira `UNAVAILABLE`, jamais tier.

ATR ausente ⇒ multiplicador neutro `1.0` com motivo explícito. ATR presente e
inválido ou não positivo ⇒ `UNAVAILABLE`.

Qualquer falha suprime **apenas esta referência** — nunca aborta a recomendação.
O `except` de contenção usa um dicionário literal, sem chamar nenhum helper, para
que a exceção não escape se o próprio helper for a causa.

---

## 5. Pós-multiplicadores

`edge` e `liq` só mordem referência **estritamente positiva**: `None` continua
`None`, zero continua zero, e nenhum piso ressuscita um zero. Multiplicador não
finito, negativo ou não numérico não vira NaN nem referência inventada — suprime
a referência com `MULTIPLIER_INVALID`.

`sizing_provenance.final_pct` acompanha o valor **final**, depois de edge/liq e
arredondamento: `final_pct == suggested_size_pct` sempre. `raw_pct` continua
sendo o bruto do modelo, antes dos espelhamentos.

Contrato serializado: `version`, `model`, `mode`, `unit`, `status`,
`reason_code`, `probability_used`, `rr_tp1`, `kelly_full`, `raw_pct`,
`final_pct`, `source`, `limitations`. Vocabulário de motivos fechado; campo não
calculável é `null`, nunca zero fabricado; todo número serializado é finito.
Sem tabela, coluna ou migration.

---

## 6. Comparação numérica (score 80, ATR 2%, `vol_mult = 1,0`)

| caso | Kelly antigo | raw antigo | Kelly novo | raw novo | final |
|---|---|---|---|---|---|
| `p=.52`, RR1 = 1, RRfinal = 3 | 0,36 | **7,20%** | 0,04 | **0,80%** | 0,80% |
| `p=.70`, RR1 = 1, RRfinal = 3 | 0,60 | **12,00%** | 0,40 | **8,00%** | **1,00%** |
| `p=.50`, RR1 = 1 | +0,167 | 3,33% | 0,00 | 0,00% | **0,00%** |
| `p=.70`, RR1 = 0,40 | +0,10 (piso `b=0,5`) | 2,00% | **−0,05** | 0,00% | **0,00%** |
| `score = 0`, `p=.70`, RR1 = 1 | 0,40 | 0,00% | 0,40 | 0,00% | **0,00%** (antes 0,25%) |

O segundo caso é o motivo de não confiar no valor final para julgar a correção:
o bruto caiu de 12% para 8%, mas o teto de 1% iguala os dois finais. Uma
correção real pode ficar invisível se só o número exibido for comparado.

---

## 7. Testes

`backend/tests/test_r06b3_kelly_semantics.py` — **50 testes herméticos**
(rede/DNS bloqueados e contabilizados; sem exchange, banco, credencial, holdout
ou ordem real), com a matemática de referência reimplementada dentro do teste.

Blocos: fórmula e geometria (18) · contrato e ausência (9) · integração completa
(8) · independência operacional (6) · frontend e rotulagem (8). A integração
exercita `_build_recommendation` **real** e valida o retorno serializado
inteiro, não um helper isolado.

### Testes anteriores alterados, e por quê

Todos caracterizavam deliberadamente o defeito agora corrigido:

| teste | era | virou |
|---|---|---|
| `test_DEFEITO_kelly_mistura_evento_tp1_com_rr_do_alvo_final` (R06A) | travava `b = RR final` | `test_CORRIGIDO_kelly_usa_o_payoff_do_proprio_tp1` |
| `test_kelly_negativo_nao_vira_tamanho_zero` (R06A) | Kelly ≤ 0 ⇒ `None` | `test_CORRIGIDO_kelly_negativo_vira_zero_e_nao_ausencia` |
| `test_sizing_usa_prob_tp1_com_fallback_por_tier` (R06A) | prob ausente ⇒ tier | `test_CORRIGIDO_sizing_consultivo_nao_presume_prob_por_tier` |
| `test_kelly_e_sizing_nao_mudaram` (R06B1) | chamava a assinatura antiga | mantém as **constantes**, usa a nova semântica |
| bloco `Sizing` + `test_calibracao_indisponivel_preserva_o_comportamento_anterior` (R06B2) | esperava fallback por tier | espera ausência |
| `test_kelly_e_caps_inalterados`, `test_sizing_nao_depende_mais_de_import_interno`, `test_falha_suspende_o_sizing_dinamico`, bloco `Sizing` (R06B2.1) | expressão e texto antigos | expressão nova + **teste comportamental** do `except` de contenção |

Preservados sem alteração: proteção do contrato de probabilidade, constância dos
caps, ausência de rede, garantias de execução e o hotfix `eab20a5b` que passa
`prob_tp2` ao `bot_verdict`.

Dois asserts de escopo do R06B2.1 foram fixados no range de commits daquele
pacote (`8ae87567..eab20a5b`), como já se fez nas fases anteriores.

Um teste de integração precisou fixar `sys.modules["services.shadow_trade_service"]`:
`test_p03_execution_reconciliation` instala um módulo falso e, no teardown,
**remove** a chave em vez de restaurar a original — sem isso o patch mira um
objeto de módulo diferente conforme a ordem dos testes. Isso é uma fragilidade
de isolamento preexistente naquele arquivo, contornada aqui, não corrigida lá.

---

## 8. Limitações

- **A calibração é de setups, não deste modelo.** `P(TP1)` vem de snapshots
  resolvidos cujo desfecho inclui parciais e expirações; usá-la num modelo
  binário de saída integral é uma aproximação declarada.
- **Expirações como −1R é hipótese**, não resultado observado.
- **Sem custos, funding ou slippage** — não é resultado líquido.
- **Histórico sem proveniência individual** (herdado do R06B2.1): pares
  anteriores ao R06B1 não carregam a fórmula que gerou seu score.
- **Nenhuma comprovação de melhora de lucro ou redução de stops.** Não houve
  backtest, recalibração nem acesso a exchange. O que este pacote prova é
  coerência de unidade e de semântica, não desempenho.
- **Nenhuma integração nova ao dimensionamento real** foi criada, e o zero
  consultivo **não** é bloqueio operacional.
- Testes travam contratos; não provam ausência de bugs.
