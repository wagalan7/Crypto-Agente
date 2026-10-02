# Correção integrada sobre `001a6685` — observação, autoridade local, payload

Quatro defeitos reproduzidos na revisão de `001a6685`, corrigidos juntos com o
RED medido ANTES na mesma árvore. Não reimplementa F1–F7 (contrato anterior em
`docs/FECHAMENTO_UNICO_MANUAL_BOT_61920156.md`).

## A — uma observação ORIGINAL e COMPLETA (R1 e R2)

Arquitetura vigente: contexto antes da leitura → GET com início/fim ORIGINAIS →
normalização completa → validação temporal → `revalidate_active` → decisão do
scan. **Nenhum filtro antecede a validação da resposta completa.**

- `_detect_untracked_positions` mede `observed_start_ms` imediatamente antes de
  `get_positions(force=True)` e `observed_end_ms` imediatamente depois, no mesmo
  relógio em ms do contrato manual, e passa a resposta **integral** para a
  revalidação. O prefiltro `_finite(size) or 0` saiu: era ele que transformava
  `NaN` em zero e apagava a linha antes de qualquer prova.
- `observation_from_rows` exige `started_ms` **e** `ended_ms`. Ausência ou
  incoerência ⇒ observação INCOMPLETA/UNKNOWN; o tempo de processamento nunca
  vira prova de frescor.
- `normalize_positions`: `None`/coleção desconhecida **não** é lista vazia;
  linha malformada, `size` bool/NaN/inf/ausente e quantidade **negativa**
  (impossível no formato normalizado) são incompletude. `size` ZERO finito é
  legítimo e não exige campos de posição ativa. Linha ATIVA sem símbolo, lado,
  perna, entrada ou `updateTime` verificáveis torna a OBSERVAÇÃO incompleta —
  e continua visível no inventário administrativo como INELEGÍVEL, em vez de
  desaparecer do painel do operador.
- Só depois da resposta completa e temporalmente válida o scan seleciona as
  exposições não-zero, pela quantidade canônica (`_exposicoes_nao_zero`), e
  mantém as linhas RAW porque é `size`/`symbol` desse formato que ele consome.
- Incompletude, >20 s, stale/rate-limit/erro ou relógio incoerente ⇒ UNKNOWN,
  `_boot_scan_safe=False` e contenção oficial. Nunca CLOSED/prova/liberação.

## B — autoridade local até o handoff (R3)

`authorize_dispatch` captura, **antes de qualquer await**, um token imutável de
autoridade local (fence manual, pendência, símbolo) e o confere já na entrada.
A identidade imutável validada é vinculada depois **sem** recapturar o fence. As
verificações duráveis (lease, dispatch, geração financeira, ownership, proposta)
continuam sob `917283`. A reconferência final do token ORIGINAL acontece
**depois de sair totalmente do contexto da sessão** — `rollback` e
`close`/`__aexit__` suspendem e era exatamente aí que uma falha nova passava.
O token viaja no veredito e é reconferido no exame SÍNCRONO pré-assinatura,
junto com idade/lease/P04/identidade/payload; depois dele não existe await
decisório. Persistir ou limpar a pendência **não** ressuscita a autorização: o
fence antigo permanece inválido e só preparação/readmissão/autorização NOVAS
voltam a enviar. `register_validation_failure` continua registrando fence/pending
sincronamente antes de tentar persistir.

## C — identidade e payload inteiros (R4)

A proposta persistida íntegra e a identidade imutável são a referência; os params
recebidos nunca definem o esperado. O símbolo WIRE efetivo é comparado com o
derivado da proposta pela conversão REAL do transporte (`BASE-QUOTE-SETTLE` da
identidade → forma de mercado → `to_binance`), então `DELTA/USDT:USDT` e
`DELTAUSDT` são equivalentes e `DELTAUSDC` não. Conta/exchange/mercado/intenção
do contexto interno são conferidos contra proposta e identidade. `reduceOnly`,
`closePosition` e `stopPrice` são recusados por **presença** (não por
veracidade); `positionSide` só passa quando compatível com o modo admitido, e a
omissão saudável do builder one-way é aceita. MARKET não leva price/timeInForce;
LIMIT exige preço e TIF admitidos. O transporte valida e assina **a mesma cópia
local** dos params finais. Mismatch NEGA — nada é “consertado”. SL/TP/redução
seguem outro contrato e não passam pela whitelist de entrada.

## Limites que permanecem

- O fence é LOCAL: outro processo ou uma ação direta na corretora **não** são
  atomicamente impedidos por ele. Falha não commitada não é durável nem
  conhecida por outro processo.
- Não há atomicidade com a exchange; a leitura fresca continua obrigatória.
- Nada aqui liga operação real, reconhece posição real ou libera pausa real.

## A03 — fechamento do parser HTTP sobre `efd3dfe9` (02/10/2026)

A defesa anterior no formato normalizado não bastava: o parser real usava
`res["result"] or []` e `positionAmt or 0`. Um corpo HTTP `null`, uma quantidade
ausente ou `false` desapareciam ANTES da validação manual e podiam fechar um
reconhecimento ACTIVE como se a conta estivesse comprovadamente flat.

- `get_positions` agora exige uma lista explícita e valida TODAS as linhas e
  quantidades antes de filtrar zero/símbolo. Ausência, booleano, tipo inválido,
  NaN/infinito, overflow ou conversão de quantidade não-zero para zero devolvem
  `ok=false`, `quality=UNKNOWN`, `complete=false`, `positions=null` e
  `POSITION_RISK_INVALID_PAYLOAD`. Nenhum subconjunto é publicado.
- Lista vazia explícita e quantidade zero finita continuam válidas. Quantidade
  HTTP negativa finita continua SHORT legítimo; o `size` normalizado é positivo.
  Campos ativos que não podem ser convertidos também recusam a leitura inteira.
- Uma resposta inválida expira a autoridade fresh do cache anterior. O último
  snapshot fica disponível apenas pelo caminho stale já existente de cooldown;
  um GET completo válido restaura o cache. A sentinela interna de expiração não
  aparece como Infinity no diagnóstico público (`cache_age_s=null`).
- No boot, o motivo do parser percorre `_revalidate_manual_acks(source_ok=False)`
  com o contexto e a janela ORIGINAIS: revoga a autoridade local e persiste a
  causa pelo fluxo oficial, antes de retornar UNKNOWN e armar a quarentena.
  Não há segundo GET. Recuperação ocorre somente pelo ciclo oficial após nova
  leitura válida; não há limpeza manual de latch/pausa/época nos testes.

Prova RED: os primeiros 12 testes de parser na baseline produziram 23 falhas e
7 erros. A suíte final inclui 14 testes novos com HTTP sintético passando pelo
transporte/parser reais, controles saudáveis, invalidação de cache e recuperação.
O harness do boot deixou de fabricar a resposta normalizada para esses casos.

Validação após a última alteração de código: 166 testes focais aprovados 2×;
suíte completa 2.500 executados, 2.498 aprovados e os 2 skips históricos R05C por
fixture privada ausente. PostgreSQL 16 real descartável, socket Unix e TCP/DNS
bloqueados: boot 46 + dispatch 32 + fechamento 73 = 151 verificações, todas
aprovadas 2×. Compilação dos quatro arquivos Python e diff-check aprovados.

Escopo: dois serviços, teste novo, harness do boot e documentação. Sem schema,
flag, ENV, estratégia, sizing, limite, frontend ou default novo. Main não foi
alterado; nenhum merge, push, deploy ou acesso à conta real nesta correção.
As limitações externas de fence local/TOCTOU descritas acima permanecem.
