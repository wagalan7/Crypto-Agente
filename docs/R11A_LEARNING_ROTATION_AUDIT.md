# R11A — Auditoria do aprendizado, do edge decay e da rotação

Base do lote: `51c992c2`. **Auditoria e caracterização; nada foi corrigido.**
Os quatro serviços auditados estão intactos no diff. Testes:
`backend/tests/test_r11a_learning_rotation_audit.py` (38, herméticos, mocks
integrais, sem rede/banco/notificação reais).

> Teste de caracterização fixa o comportamento ATUAL. Não aprova a política,
> não prova rentabilidade e não mede produção: **nada aqui foi consultado em
> PRD**. Defaults abaixo são os do CÓDIGO, com ENV vazio, e não comprovam o
> ambiente de produção.

## Matriz por mecanismo

| Mecanismo | Função → chamador | Fonte | Janela | Elegibilidade | Unidade da amostra | Efeito LIVE possível | Cache | Persistência | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ajuste/bloqueio por bucket | `learning_service.compute_auto_adjustments` + `apply_score_adjustment` → `recommendation_service` (2 caminhos) | `recommendation_snapshots` (papel) | `LEARNING_LOOKBACK_DAYS=0` ⇒ todo o histórico | status ∈ {won_tp1, won_tp1_be, won_tp2, lost}; `wide_*` fora; bloqueio n≥30 e WR≤30%; boost n≥20 e WR≥65%; punição n≥20 e WR<50% | 1 snapshot resolvido | multiplica o score (±25% agregado) e **descarta** a rec bloqueada | `compute_stats_by_bucket` 300s por `days` | não persiste | erro no DB → chamador segue sem ajuste e **sem bloqueio** |
| Size por moeda | `symbol_learning_service.derive_params` / `get_size_mult` → `shadow_trade_service` (só LIVE) | `symbol_backtest_stats` (sweep) | histórico completo do sweep | n≥30 e `wf_avg_r` presente; confiança ≥0,25 | 1 linha (base, TF) do sweep | multiplica `risk_pct` em 0,75–1,15 | `_CACHE` em memória, carregado no boot e no relearn | `symbol_learned_params` (upsert do melhor TF) | flag off, cache vazio ou confiança baixa → 1,0 |
| Edge decay | `edge_decay_service.maybe_refresh` / `get_mult` → `shadow_trade_service` (só LIVE, gated) | `recommendation_snapshots` (papel) | baseline 60d, recente 14d (por `outcome_at`) | `realized_r` não nulo; inclui `expired`; recente ≥8 | 1 snapshot resolvido | multiplica `qty` em 0,5–1,0 (só reduz) | TTL 1800s | não persiste | flag off/erro → mantém cache; leitura falha → 1,0 |
| Rotação do universo | `rotation_service.compute_rotation_plan` → `apply_rotation_plan` → loop 6h em `main.py` e `POST /api/rotation/apply` | `compute_symbol_stats` (mesma fonte de papel) + allowlist em memória | histórico completo (`days=0`) | promote: n≥12 e avg_r>0; demote: avg_r<−0,2; piso de liquidez top-N; histerese 3 ciclos | 1 snapshot resolvido, agregado por base | muda **quais moedas podem ser operadas** | allowlist efetiva em memória | `rotation_universe_state` | piso indisponível → promove sem piso; DB indisponível → não aplica |

Chamadores automáticos: `_rotation_loop` (boot: `prime_effective_allowlist`;
preview semanal; apply a cada 6h), `backtest_universe_service` (relearn ao fim
do sweep), `shadow_trade_service` (size por moeda e edge decay, só fora do
shadow), `recommendation_service` (ajuste/bloqueio por bucket).

## Achados

Gravidade: **A** (pode mudar dinheiro/entradas agora), **M** (distorce
evidência), **B** (rótulo/documentação). "Confirmado" = reproduzido em teste
sintético contra o código atual.

### A1 · Endpoints mutantes sem autenticação — A, confirmado (`R9`)
`POST /api/rotation/apply` e `POST /api/symbol-params/relearn` não chamam
`_check_admin_token` (o padrão existe em `/api/admin/force-test-trade`), e o
CORS é `allow_origins=["*"]`. O primeiro pode mudar a allowlist de execução; o
segundo escreve `symbol_learned_params` e dispara Telegram.
**R11B:** exigir o mesmo guard dos endpoints admin. **Muda entradas: sim.**

**Acompanhamento R11B1 — 2026-09-17:** corrigido nas duas rotas com
`X-Admin-Token` e `_check_admin_token` antes de importar/chamar os serviços.
Token configurado exige correspondência; token ausente em produção ou falha
ao determinar o ambiente bloqueia. A política preexistente de demo/testnet sem
token e o formato de erro `{ok:false}` foram preservados. Loops internos,
learning, allowlist e sizing não foram alterados. Testes ASGI locais usam as
funções reais das rotas com serviços falsos; nenhum POST mutante foi feito em
produção. O teste R9 passou de caracterizar a falha a exigir a proteção.

### A2 · Histerese conta chamadas, não evidência nova — A, confirmado (`R2`)
Três chamadas de `apply_rotation_plan` com **os mesmos** dados promovem; o
plano vem do histórico completo, então nenhum trade novo é necessário. Com A1,
três POSTs seguidos encurtam as ~18h previstas para segundos. `_save_state`
grava a cada ciclo, mesmo sem mudança.
**R11B:** contar ciclos por tempo/evidência (ex.: exigir `outcome_at` novo) e
ignorar repetições dentro da mesma janela. **Muda entradas: sim.**

### A3 · Edge decay some justamente no decaimento severo — A, confirmado (`E2`)
A janela "baseline" (60d) **contém** a recente (14d). Com 20 trades a +0,3R e
10 recentes a −0,1R, o corte é 0,83×; trocando os recentes para −1,0R, o
baseline cai a −0,13R, fica abaixo de `base_min=0,1` e o resultado é "não é
decay" → **sem corte**. A proteção é não monotônica no pior caso.
**R11B:** baseline excluindo a janela recente (ou teste de duas amostras).
**Muda dinheiro: sim, quando a flag estiver ligada.**

### A4 · `NaN` vira amplificação de size — A, confirmado (`S5`)
`get_size_mult` faz `max(0.75, min(1.15, nan))` → **1,15** (teto). Confiança
`NaN` também passa no corte mínimo. Depende de dado corrompido na tabela, mas
o efeito é aumentar a mão.
**R11B:** validar finitude antes do clamp; não finito → 1,0.

### A5 · `realized_r` desconhecido pode bloquear um bucket LIVE — A, confirmado (`L6`)
`realized_r=None` entra como 0 e não conta como vitória: 30 snapshots
resolvidos sem R dão WR 0% e bloqueiam `A_4h`; toda rec do bucket é
descartada. Não verifiquei se isso ocorre hoje em produção.
**R11B:** excluir R desconhecido da amostra e expor a contagem.

### M1 · Sessão/dia aprendidos e aplicados em relógios diferentes — M, confirmado (`L9`)
As estatísticas usam `features.hour_utc` = hora de **criação do snapshot**;
a aplicação deriva a chave de `sig.timestamp` = abertura da **vela** do sinal.
Vela de 12:00 UTC ("Europe") contra criação às 16:00 ("NY") não casa: o ajuste
aprendido não atinge a rec pretendida.
**R11B:** usar a mesma referência temporal nos dois lados.

### M2 · `None`/`NaN` distorcem a rotação — M, confirmado (`L1`, `L2`)
`None` completa a amostra mínima (12) e dilui a média; um único `NaN` torna a
média não finita, o veredicto vira "neutro" (um símbolo a −1R sustentado
**não** é rebaixado) e o JSON deixa de ser serializável com `allow_nan=False`.
**R11B:** sanitizar a leitura e separar "sem R" de "R zero".

### M3 · Linhas aprendidas ficam obsoletas — M, confirmado (`S7`)
`relearn_all_from_history` faz upsert só do **melhor TF** por base. Linhas de
outros TFs e de bases que ficaram inelegíveis continuam na tabela e no cache,
e `get_size_mult` ainda as usa (inclusive o fallback "melhor TF por
confiança", que aplica a edge de um timeframe diferente).
**R11B:** expirar/marcar linhas não reaprendidas e decidir explicitamente se o
fallback entre timeframes é desejado.

### M4 · Direção inválida conta em dobro no edge decay — M, confirmado (`E5`)
Linhas com direção fora de {long, short} são acumuladas duas vezes na chave
`(símbolo, "any")`: 4 linhas viram 8 e destravam o mínimo de amostra, criando
um corte que não existiria com direção válida.

### M5 · Divergência entre allowlist em memória e universo do DB — M, confirmado (`R6`)
O plano lê a allowlist **em memória** e o apply lê o universo do **DB**. Se
divergirem (ex.: `prime_effective_allowlist` falhou no boot), uma base ruim que
só existe no DB nunca é proposta para demoção — e assim nunca sai.

### M6 · Piso de liquidez é fail-open — M, confirmado (`R4`)
Fonte de volume indisponível → conjunto vazio → promoção **sem** piso (a
histerese continua). Está comentado no código; fica registrado como risco.

### M7 · Cache do edge decay consulta a cada chamada quando ninguém está em decay — M, confirmado (`E6`)
`maybe_refresh` só respeita o TTL se o cache estiver **não vazio**; com
ninguém em decay, cada lote refaz a consulta de 60 dias.

### M8 · Unidade da amostra é o snapshot — M, confirmado (`L5`, `L4`)
TFs, direções e formatos do mesmo símbolo somam na mesma base, e repetições do
mesmo setup contam como evidência independente. Bordas: n=11 não julga; avg_r
exatamente 0 ou −0,2 é "neutro".

### B1 · Documentação contradiz os defaults do código — B, confirmado (`S1`/`R12`)
Com ENV vazio: `SYMBOL_LEARNING_SIZE_ENABLED=True`, `ROTATION_AUTO_APPLY=True`,
`ROTATION_HYSTERESIS_CYCLES=3`, `ROTATION_MIN_SAMPLE=12`,
`LEARNING_AUTO_ADJUST/BLOCK=True`, `LEARNING_LOOKBACK_DAYS=0`,
`EDGE_DECAY_ENABLED=False`, `BT_SEED_ENABLED=False`. Mas os textos dizem
"default OFF" (docstring do symbol learning e comentário no executor),
"ROTATION_AUTO_APPLY=off (default)" (loop em `main.py`), "NÍVEL 2 (preparado,
não ativo)" (learning) e, no AGENTS.md, "≥15 trades" e "2 ciclos".
**R11B:** alinhar texto e código (sem alterar política neste lote).

### B2 · Rótulo "dormente" para bucket com amostra — B, confirmado (`L7`)
Bucket com n≥20 e WR entre 50% e 65% não recebe multiplicador e é contado como
"dormente", como se faltasse amostra.

### B3 · Populações diferentes entre mecanismos — B, confirmado (`L3`, `E3`)
O learning ignora `expired` e trata `realized_r=None` como 0; o edge decay
inclui `expired` e exige `realized_r` não nulo. Ambos usam SHADOW/papel —
**não** é execução financeira REAL.

## Prioridade sugerida para o R11B

1. A1 (autenticação) — corrigido no acompanhamento R11B1 acima.
2. A2 + M5 (unidade da histerese e fonte do universo).
3. A3 (janela do edge decay) antes de a flag ser ligada em produção.
4. A4/A5/M2 (dados não finitos e R desconhecido).
5. M1/M3/M4/M6/M7 e depois B1–B3.

O pacote original R11A não corrigiu políticas. No acompanhamento R11B1,
somente A1 foi corrigido; os demais achados continuam pendentes. Nenhum item
acima demonstra, por si, redução de stops ou aumento de lucro.
