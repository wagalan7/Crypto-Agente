# R11B2 — Integridade numérica do aprendizado e do multiplicador por moeda

Base do lote: `ec5c2fd9`. Corrige **A4**, **A5** e **M2** do R11A e a origem
numérica associada ao A4. Serviços alterados: `learning_service.py` e
`symbol_learning_service.py`. Testes: `backend/tests/test_r11b2_learning_numeric_safety.py`.

> Dado **válido** preserva fórmula, limite, arredondamento e decisão. Dado
> **inválido** deixa de contribuir — e isso pode mudar ajustes de score,
> bloqueios, multiplicadores e veredictos que antes usavam esse lixo. Não se
> afirma aqui que toda decisão LIVE continua idêntica.

## Contrato numérico

Aceita `int`/`float` reais e finitos. Recusa, **antes** de converter, comparar,
rankear, somar ou clampar: `bool`, string (mesmo numérica), objeto, `None`,
`NaN` e `±inf`. Zero legítimo continua válido quando o campo permite; `value or 0`
não é mais usado para transformar ausência em evidência. Contagens são inteiros
não negativos (float integral finito é normalizado; `bool` nunca é contagem).
Confiança e percentil ficam em [0,1]; `expiry_pct` em [0,100]; `wf_avg_r` pode
ser negativo ou zero, sem piso artificial de edge positiva. Resultado derivado
também precisa ser finito — overflow não vira multiplicador no teto nem JSON
com `NaN`.

O fallback do multiplicador aprendido é sempre **`1.0` + motivo controlado**,
que significa **não aplicar esta camada**. Não é risco zero, não garante
segurança da operação e não desliga nenhuma outra proteção. `0.0` nunca é usado
como substituto de informação ausente, e não existe bloqueio global novo.

## Estatísticas: R inválido sai uma vez, antes de tudo

Um único particionador (`_partition_r`) roda nos quatro caminhos de leitura —
`compute_stats_by_bucket`, `lookup_historical_for`, `lookup_historical_batch`,
`compute_symbol_stats` — antes de denominadores, buckets, totais, médias,
wins/losses, amostra mínima e veredictos. Nenhuma query, fonte, janela ou status
foi adicionado; `_resolved_conditions` está intacto.

Diagnóstico aditivo, conciliável e sem dado individual:

```json
"data_quality": {"total_raw": 10, "total_valid": 5, "excluded_total": 5,
                 "excluded_by_reason": {"ausente": 1, "tipo_invalido": 2, "nao_finito": 2}}
```

- `compute_stats_by_bucket`: `total_trades` = válidos; qualidade no envelope.
- Lookups: qualidade do grupo pedido, inclusive quando **todos** são inválidos.
- `compute_symbol_stats`: `{base: stat}` preservado, com a qualidade **dentro**
  de cada stat — nunca uma chave global que a rotação leria como moeda.
- Grupo sem válidos: `trades=0`, `sample_ok=False`, médias e taxa `None`
  (ausência, não performance zero), veredicto `amostra_pequena` — sem boost,
  block, promote ou demote. `total_raw` distingue vazio de tudo-excluído.
- Zero REALMENTE registrado continua na amostra, com a convenção de
  wins/losses de cada função preservada (não se redesenhou a taxonomia).
- Cache mantém chave, TTL e invalidação; guarda o resultado já saneado. Erro de
  banco continua propagando e **não** é cacheado como conjunto vazio válido.
- `compute_auto_adjustments` ignora bucket com `n`/WR inválidos, mesmo injetado
  por cache ou teste, e conta em `invalid_buckets` — sem fabricar boost/block.

### Exemplos (antes → agora)

| Entrada | Antes | Agora |
| --- | --- | --- |
| 6 wins + 6 R ausentes | n=12, média 0,5R, amostra OK, **promote** | n=6, média 1,0R, `amostra_pequena`, 6 excluídos |
| 11 perdas + 1 NaN | média NaN, veredicto `neutro` | n=11, `amostra_pequena` (mínimo 12 inalterado) |
| 12 perdas + 1 NaN | média NaN, `neutro`, JSON quebrava | n=12, média −1,0R, **demote** pelo critério existente |
| 30 resolvidos sem R | WR 0% → bucket `A_4h` **bloqueado**, recs descartadas | sem bucket, sem bloqueio, score intacto |
| 30 perdas válidas | bucket bloqueado | bucket bloqueado (igual) |

## Size: última barreira no acessor

`get_size_mult` valida também linhas antigas do banco e do cache, sem exigir
relearn nem limpeza de tabela:

- Flag OFF, cache vazio e confiança baixa: no-op de sempre.
- Confiança finita em [0,1] e multiplicador finito **positivo** são validados
  antes do clamp. Inválido, ausente, zero ou negativo ⇒ `(1.0, motivo)`.
  Multiplicador positivo finito mantém o clamp existente, sem cap novo.
- TF exato existente mas inválido ⇒ no-op: **não** se troca de TF para
  contornar a invalidez. Sem TF exato, o fallback por maior confiança continua,
  agora só entre linhas válidas (ordem de desempate preservada); nenhuma
  válida ⇒ no-op.
- O refresh **não** descarta a linha inválida — a chave exata continua
  existindo, e quem protege é o acessor.
- Metadado opcional presente e inválido desqualifica a linha; ausente em linha
  legada não cria requisito novo de amostra ou proveniência.
- Limites desta camada não finitos ou incoerentes ⇒ no-op com motivo; nada de
  consertar ENV em silêncio.

| Linha no cache | Antes | Agora |
| --- | --- | --- |
| `size_quality_mult = NaN` | `max(0,75, min(1,15, NaN))` = **1,15** (amplificava a mão) | `1.0` — "linha aprendida inválida" |
| `confidence = NaN` | passava no corte e aplicava 0,8 | `1.0` |
| `size_quality_mult = 0.0` | virava 1,0 rotulado "neutro" | `1.0` — inválido explícito |
| `mult = 2.0` / `0.10` | 1,15 / 0,75 | 1,15 / 0,75 (clamp preservado) |
| TF exato NaN + outro TF 1,15 | usava o outro (1,15) | `1.0`, sem fallback |

## Relearn: não contaminar de novo

`_eligible_metrics` valida amostra, `wf_avg_r`, contagem OOS e expiry antes de
rankear; `"nan"` e `True` deixam de virar número. Ausência de `wf_n_trades` e
`expiry_pct` conserva os defaults legados da fórmula (0), sem apresentá-los
como observação comprovada; valor presente e inválido não recebe default.
`derive_params` valida o percentil antes do mapa de rank e valida a saída —
inválido ⇒ `None`, nunca dicionário meio certo.

O relearn exclui linhas inválidas antes de escolher o melhor TF e de calcular a
distribuição do universo, e separa no resumo existente `skipped_invalid` de
`skipped_small`. Uma linha ruim não derruba o lote nem envenena o ranking.
Upsert, commit, fontes e fórmulas para dados válidos ficam como estavam — e o
upsert **não** foi executado contra banco real neste pacote. Multiplicador
anterior inválido vira `old=None` na notificação: entra como "nova", não como
delta financeiro inventado. `status()` serializa cópia segura (campo inválido
⇒ `None` + `invalid_fields`), sem tocar modelo, banco ou cache.

Exemplo do teste: 5 linhas de backtest (2 numericamente inválidas, 1 com
amostra pequena) ⇒ `learned=2`, `skipped_invalid=2`, `skipped_small=1`, e os
percentis calculados só com as válidas (0,75 e 0,25 ⇒ multiplicadores 1,0562 e
0,9062, pela mesma fórmula de antes).

## Validação

`test_r11b2_learning_numeric_safety.py` (31 testes, herméticos, serviços reais
com sessões/linhas sintéticas e notificador falso): RED antes da correção
(37 falhas + 9 erros), GREEN depois. Cobre cache com NaN/±inf/bool/string/
ausente/zero/negativo; TF exato inválido com outro TF amplificador; fallback
ignorando inválidas e mantendo empate; confiança e caps nas bordas; flag OFF;
linha legada sem metadados; metadado presente não finito; limites quebrados;
derivação e ranking com inválidas no meio; refresh seguido do acessor; as seis
combinações de amostra do R11A; mistura de R válido/None/NaN/inf/bool/string
nas quatro leituras; contagens reconciliadas; bandas de ajuste e bloqueio com
dado válido inalteradas; coerência lookup individual × batch; cache, invalidação
e número de queries preservados; `json.dumps(..., allow_nan=False)`; paridade de
derivação; e plano de rotação dry-run com serviços externos falsos.

Suítes R11A (asserts de L1/L2/L6/S2 e das partes inválidas de S5 atualizados,
com antes/depois registrado no próprio teste) e R11B1 verdes. Suíte completa:
1.917 executados, 1.915 aprovados, 2 skips R05C históricos por fixture privada
ausente. `py_compile` e `git diff --check` limpos; frontend intacto.

## Pendências

**M3 continua aberto**: `relearn_all_from_history` só faz upsert do melhor TF,
então linhas de outros timeframes e de bases que ficaram inelegíveis
permanecem na tabela e no cache. Este pacote passou a recusá-las quando são
numericamente inválidas, mas uma linha **finita e antiga** continua sendo
usada, inclusive pelo fallback entre timeframes — que não foi alterado.

Também seguem abertos do R11A: A2 (histerese contando chamadas), A3 (janela do
edge decay que contém a janela recente), M1, M4–M8 e B1–B3. Nada aqui
demonstra redução de stops ou aumento de lucro.
