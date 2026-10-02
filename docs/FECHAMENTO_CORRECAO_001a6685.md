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
