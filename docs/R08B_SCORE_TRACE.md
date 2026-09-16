# R08B — Trace prospectivo do score

Modo: `ANALYTICS_ONLY`. Namespace `r08_score_trace`, `schema_version=1`,
`version=r08b.1`. Nenhum peso, score, tier, seleção, bloqueio, risco ou execução
foi alterado. A referência é o comportamento do commit `b0d52a80`.

## Captura e persistência

O trace é capturado **no cálculo**, não reconstruído no snapshot:

| Campo/estágio | Origem observada |
| --- | --- |
| `confluence` | Retorno de `calculate_confluence`: total, máximo, percentual, lista ordenada dos fatores com categoria/pontos/máximo/alinhamento. |
| `confluence.config` | Cópia dos `WEIGHTS`, pesos empíricos, calibração e overrides conhecidos de padrões naquele cálculo. |
| `config` | Pesos V2/legado, cortes de tier e flags/bônus HTF do cálculo do score. |
| `stages.raw_score` | Score bruto realmente produzido; componentes normalizados, numerador/denominador V2, ou componentes e bônus legados. |
| `stages.base_score` | Resultado real após relevância HTF, clamp e arredondamento; multiplicador observado. |
| `stages.htf_score` | Resultado após bônus cruzado, entrada, bônus aplicado antes do teto e bônus configurado. O caminho batch registra explicitamente que não aplica esse bônus. |
| `stages.selection_score` | Chave efetivamente avaliada pelo `max`, penalidade observada e configuração. Não substitui o score do candidato. |
| `stages.learning_score` | Entrada e score efetivamente usado pelo caller, multiplicador retornado, contagem de buckets, flags/cap/limiares presentes. Captura também bloqueio e falha. |
| `stages.final_score` | `Recommendation.score` e tier efetivamente emitidos. |
| `stages.execution_score` | Somente quando o executor chegou ao cálculo: score de entrada, delta agregado observado, resultado usado no gate e configuração recebida. Integração/persistência segregada pelo R09. |

Os buffers internos do sinal e da confluência são excluídos do `model_dump`
para não triplicar o payload. A recomendação publica uma cópia própria, que o
snapshot copia para `features['r08_score_trace']` **apenas na criação**. Isso
vale também para novos snapshots wide. Não há coluna nova, migração, backfill,
consulta, transação ou chamada de rede acrescentada pelo R08B.

O builder continua recomputando a proveniência/calibração pelo comportamento
anterior, com `capture_trace=False`; não pode sobrescrever o trace inicial
depois da inclusão de MTF sintético. Um novo cálculo limpa a anotação anterior
para não herdar trace velho caso a nova captura falhe.

## Contrato e limites

- Valores numéricos são finitos ou `null`; booleanos não são números. Campos
  numéricos ausentes aparecem em `missing_fields` em cada estágio.
- Estados: `OBSERVED`, `NOT_OBSERVED`, `NOT_APPLIED`, `UNAVAILABLE`, `BLOCKED`.
  Ausência não vira zero, e um estágio não visitado não é reconstruído.
- Allowlist recursiva, vocabulário fechado e limite de **24 KiB** por trace.
  No máximo 64 fatores; `factor_count` e `factors_truncated` explicitam cortes.
  Fatores são identificados por categoria + índice: nomes, descrições,
  warnings, textos de exceção, segredos e outcomes não são copiados.
- `learning_score.multiplier` é o retorno do learner (atualmente arredondado
  a três casas), **não** uma estimativa do produto interno antes de arredondar.
  O valor final observado tem a precisão original. Não são copiados todos os
  buckets históricos nem reconstruídos componentes internos não expostos.
- Configuração congelada representa o processo que calculou o registro, não
  comprova ENV ou deploy de produção. Configurações ausentes ficam `null`.
- Snapshots capturados antes do executor mantêm `execution_score=NOT_OBSERVED`.
  O R09 pode persistir a cópia anotada no seu payload; não se adicionou uma
  transação extra para modificar o snapshot após a execução.
- Captura é fail-soft no helper **e na fronteira do scorer/snapshot**. Não há
  dependência do laboratório/replay nos imports live.
- Tamanho medido (JSON compacto): ≈3,7 KB sem fatores, ≈6,0 KB com 30 e
  ≈8,8 KB com 64. Como a recomendação carrega sua cópia, esse volume também
  aparece na resposta de `/api/recommendations` e em `features` de cada
  snapshot novo. Registrado como ponto de decisão; não alterado.
- No executor, o hook de observação fica **antes** da marca de latência
  `attempt_started_at` (P05.2L), fora da janela medida.

API pura para o executor:

```python
append_execution_score(
    trace, recommendation_score=before, execution_score=after,
    delta=observed_delta, enabled=enabled, cap=cap, score_min=score_min,
)
```

`freeze_trace(payload)` devolve uma nova cópia sanitizada. Não lê estado atual
para preencher histórico nem copia campos desconhecidos do payload.

## Validação local

`tests/test_r08b_score_trace.py`: paridade independente V2 (120 combinações),
legado e fallback; HTF/clamp; seleção real client/server; aprendizado observado;
confluência/config congeladas; recompute tardio sem sobrescrita; snapshot
imutável; desconhecidos/não finitos/truncamento; ausência de outcome leakage;
API de execução; falhas injetadas sem alterar score/tier/recomendação; sem IO.

Também foram executadas as suítes R06B1, R06B2, R06B2.1, R06B3 e R08A. O teste
de ordem de proveniência do R06B2 foi ajustado somente para reconhecer o novo
argumento `capture_trace=False`, preservando a exigência de cálculo antes do
lookup de calibração.

```sh
cd backend
PYTHONDONTWRITEBYTECODE=1 .venv311/bin/python -B -m unittest \
  tests.test_r08b_score_trace tests.test_r06b1_safe_ema_score_contracts \
  tests.test_r06b2_score_calibration_fencing tests.test_r06b2_1_final_contract_closure \
  tests.test_r06b3_kelly_semantics tests.test_r08a_score_research -q
```

Esta entrega torna o processo auditável prospectivamente; não é replay de
histórico ausente, prova de rentabilidade, nem autorização de alterar produção.
