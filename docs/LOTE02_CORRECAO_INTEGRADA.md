# Lote 02 — fechamento da revisão integrada

05/10/2026. Base `a39ab3580928a7ffd8d36070a07e715b6ef910de`, worktree autorizado
`lote02-pesquisa`. Correção técnica local; **não é aprovação operacional ou
econômica, nem demonstração de menos stops/lucro maior**.

## Fronteiras corrigidas e provadas

| Fronteira | Correção | Prova |
|---|---|---|
| Captura incompleta | Lotes de 50, buffer de 200; admitidas/recusadas na cobertura; três vetos de tier B registrados | Caller do scanner; coletor real |
| Baseline prematura | ACCEPTED só após correlação e guard de carteira; `FINAL_SCANNER_SELECTION` obrigatório na pesquisa | Paridade do champion e recusa de estágio anterior |
| Identidade declarada sem execução | ScoreConfig explícita, versão/fingerprint reais; corpo do manifesto, população, split, custos e configs reconferidos pelo catálogo | Contratos V1 preservados; adulterações V2 recusadas |
| Features/proveniência ausentes | Produtor prospectivo com inputs locais da vela fechada; trace/config reais; HTF por fração válida a favor | Sem fetch novo; ausência/futuro/ambiguidade não viram zero |
| Fitting/OOS frágil | Validação semântica além do hash, suporte 200/30, relógio obrigatório, decisões OOS posteriores e oportunidades disjuntas | Artefato errado, expirado, revogado, suporte fabricado e cronologia inválida recusados |
| EV sem identidade | Prova OOS vinculada à gestão/dataset/custos/evento; payoff já líquido sem desconto duplicado | Contexto divergente e scalar avulso recusados |
| Pipeline apenas em helpers | Export bruto oficial → seleção → dois replays → folds → fitting → CAS/JSONB → restart → catálogo/status | Driver real + PostgreSQL 16 descartável |

## O caminho registrado

`research_pipeline.py --manifest` exige `--dataset-dir` com o export oficial
(`dataset.json` + `manifest.json`). O escopo bruto
`R09_PRE_SELECTION_POPULATION` traz aceitas e vetadas da mesma captura. O
exportador usa transação REPEATABLE READ READ ONLY, rollback e somente ids
admitidos antes do holdout. Decisão de seleção não é outcome financeiro.

`--price-windows` fornece `R13_OFFLINE_PRICE_WINDOW_V1`: hash do dataset, fonte,
grade, corte, candles completos e quotes conhecidos no instante da decisão.
Janelas futuras, IDs externos/holdout, candles inválidos e divisão temporal
incompatível são recusados. Ausência bloqueia; nunca chama a demo sintética.
`SYNTHETIC_TEST_ONLY` exige manifesto TEST_ONLY e não autoriza estudo real.
Preço histórico fornecido pelo operador é **declarado, não certificado de forma
independente**. Aceitas ainda não têm coleta automática dessas janelas/quotes.

Fitting exige `--calibration-event` e `--calibration-valid-for-ms` explícitos.
Sem pedido, não inventa evento/validade nem roda fitting. Quatro folds
expansivos usam treino e validação com purga/embargo; o teste final fica selado.
TP1/TP2 censuram encerramento por tempo sem alvo/stop observado. O EV integral
da gestão usa somente `P_NET_RESULT_POSITIVE`: a coorte censurada de TP1/TP2 não
é usada como expectativa de todas as saídas.

`--persist` publica explicitamente em `policy_simulation_state`, namespace
`r13cal:`, usando o CAS oficial. Repetição não renova artefato nem cria geração.
Releitura valida contexto completo do manifesto/pedido, preços/custos,
artefato e payoff. GET existente só lê; falha de banco é ERROR, não ausência.
Nenhum endpoint, tabela, migration, worker ou scheduler novo.

## Features: o que são e o que não são

- `structure_quality`: geometria binária de suporte/invalidação por pivô, stop
  e alvo; não representa qualidade causal ou edge estimado.
- `trigger_body_ratio`: corpo/range da última vela fechada.
- `trigger_follow_through_atr`: avanço direcional sobre uma referência única de
  padrão confirmado, anterior à abertura da vela; sem referência fica ausente.
- Distâncias: entrada/pivô e entrada/preço atual em ATR observado.
- HTF: proporção dos TFs superiores únicos e frescos a favor; não é o valor
  assinado de `alignment_score`.

Inputs OHLC ficam privados e não são serializados. Payload V1 conserva 4 KiB;
V2 aceita até 32 KiB, com overflow recusado. Coleta inativa não faz trabalho
novo. Sinal cacheado sem inputs legítimos permanece UNKNOWN até nova análise.

## Provas finais

- 129 testes focais do Lote 02 aprovados.
- Suíte completa **2.695 executados / 2.693 aprovados / 2 skips**, duas execuções
  verdes. Skips históricos R05C por fixture privada ausente, não fabricada.
- PostgreSQL 16 UTF-8 descartável, asyncpg real/socket Unix, TCP/DNS bloqueados:
  **26 verificações integradas + 19 regressões, duas execuções verdes** em
  clusters novos. Dois workers/conexões reais; CAS, repetição, restart,
  expiração/revogação e erro SQL real. Decoder JSONB com sentinela garante que o
  holdout foi contado sem materializar seus detalhes.
- `py_compile`, sintaxe do runner e `git diff --check` aprovados. Nenhum TS/TSX,
  `frontend/dist`, modelo, `db.py`, executor ou serviço signed alterado.

Durante integração, os testes revelaram um bug real adicional: trace novo sem
features ainda escolhia V1 e excedia 4 KiB. Corrigido com regressão V1 puro × V2
sem features. Ajustes no harness foram somente clock contemporâneo à captura,
estado canônico TEST_ONLY e política da própria fixture — sem retrodatar dados
ou enfraquecer filtros/contratos de produção.

## O que continua pendente, sem mascarar

1. Decisão humana sobre baseline/candidata e mudança isolada, escopo/população e
   custos comparáveis. Manifestos desta prova são TEST_ONLY.
2. Ativação supervisionada da coleta e amostra prospectiva, com cobertura real
   por feature. Não houve ativação/backfill nem consulta à produção.
3. Janelas históricas/quotes externos confiáveis para as aceitas, sem substituto
   sintético. Custos implementados são cenários BPS declarados; ledger absoluto
   é recusado como fonte não implementada neste motor.
4. Limites de aceitação OOS, evidência econômica, aprovação humana e canário.
   OOS_VALIDATED não significa aprovação; não há auto-promotion.
5. A seleção implementada é corte Score V3; substituição de núcleo/playbooks e
   adaptador LIVE não são demonstrados ou ativados por este lote.

Champion, calibração LIVE autorizada, defaults, risco/alavancagem, universo,
posição manual, histórico e quarentena preservados. Nenhuma ordem, mensagem ou
acesso à exchange/banco de produção nos testes. Publicação não ativa pesquisa.

## Reexecutar as provas

No backend do worktree, usar o Python 3.11 do projeto para `unittest discover
-s tests`. Na raiz, executar `bash backend/tests/run_pg_lote02_correction.sh`;
o runner cria e encerra apenas seu cluster temporário UTF-8, sem TCP.

Os prompts dos Lotes 03/04 permanecem separados. Estes aceites provam os casos
citados; não afirmam ausência de bugs ou aprovação financeira.
