# Fechamento ÚNICO manual/BOT — sete defeitos sobre `61920156`

Nota curta de fechamento: o que cada correção mudou, o risco que ela carrega, a
prova executada, o que continua fora de alcance e o runbook de um único deploy.
Contexto de negócio permanece em `docs/MANUAL_BOT_COEXISTENCE.md`; o fechamento
anterior está em `docs/FECHAMENTO_MANUAL_BOT_20580139.md`.

Baseline: `61920156`. Revisão de origem: seis achados numerados (F1–F7, seis
frentes) medidos com PostgreSQL real, serviços reais e bordas externas falsas.

## RED medido na baseline (antes de qualquer correção)

Árvore da baseline exportada para fixture descartável (`git archive 61920156`),
fora do repositório; nenhum arquivo pessoal tocado. Cluster PostgreSQL 16
descartável, socket Unix, TCP/DNS bloqueados. Oito presenças de defeito:

| Caso | Observado na baseline |
|---|---|
| F1 | `place_order(reduce_only="true", stop_loss=95)` → `ok=True`, 1 POST **sem** `reduceOnly`, **zero** condicionais, `sl_ok=True`, `safety_state=NOT_APPLICABLE` |
| F2 | falha externa conhecida durante a consulta de ordens, sem conseguir persistir (timeout real de 500 ms na advisory `917283`) → ack escrito como `CLOSED` com o fence local já avançado |
| F3 | observação envelhecida 21 s (limite 20 s) durante as consultas de ordens → `CLOSED` + conta desbloqueada |
| F4 | evidência aprovada em t0, carteira levando 1,7 s → POST enviado; o reexame com o avaliador P04 REAL no instante da assinatura diz `EXEC_QUOTE_STALE` (idade 1704 ms, TTL 1500 ms) |
| F5 | `release_p03_pause` com zero incidentes e `manual_validation_blocked=true` → `RELEASED` e pausa removida; `set_manual_pause(False)` também retomou |
| F6 | reconhecimento novo criado DURANTE o GET → incidente resolvido como `FLAT` pela observação velha |
| F7 | geração financeira 1 → primeiro `mark_terminal` 2 → repetição IDÊNTICA 3 |

## F1 — classificação única da redução

- **Antes:** `classify_order_action` exigia o booleano literal, mas os ramos
  seguintes liam a VERACIDADE do argumento bruto (`scope`, proteção pós-fill,
  `operation_kind`, confirmação de fechamento). `"true"` abria MARKET sem
  `reduceOnly`, sem SL, e ainda reportava `sl_ok=True`.
- **Depois:** `reduce_only` só é aceito como booleano LITERAL
  (`reduce_only_flag_is_valid`), conferido na ENTRADA do serviço — antes de
  `set_leverage`, de arredondamento com I/O, de POST ou de cancelamento. Valor
  inválido devolve `EXEC_REDUCE_ONLY_INVALID` com zero mutações. A classificação
  canônica (`acao`/`e_reducao`) passou a valer em TODOS os ramos; nenhum deles
  volta a olhar o argumento bruto. Invariante adicional: entrada MARKET com SL
  pedido e proteção **não tentada** nunca sai como `sl_ok=True`.
- **Risco:** um caller que passava `1`/`"true"` (fora do repositório) passa a ser
  recusado em vez de "funcionar por acidente". É intencional: o rótulo errado
  isentava a ordem de instalar proteção.
- **Prova:** `tests/test_manual_bot_single_closure.py::F1…` (12 valores
  inválidos × zero POST/zero mutação, dois caminhos positivos reais até o
  cliente HTTP falso) e a matriz integrada (m1/m3).

## F2 — o fence local não se perde em ordens/SQL/commit

- **Antes:** fence e pendência eram conferidos UMA vez, antes das consultas de
  ordens; o commit só reconferia a época manual. Uma falha que não conseguiu
  persistir (só o fence local avançou) deixava o `CLOSED` passar.
- **Depois:** protocolo único de publicação — mutex LOCAL dos escritores manuais
  (tomado só nas entradas públicas, nunca durante GET da exchange), reconferência
  de fence/pendência/conta/escopo **depois das consultas de ordens**, de novo
  **depois da espera pela advisory lock** e **depois do commit**. Se o fence
  avançou durante o commit, a publicação inteira é compensada em transação NOVA
  serializada: a transição desta tentativa é desfeita por CAS de
  `id + revisão + estado ESCRITOS`, a prova é revogada, a conta é bloqueada e os
  símbolos afetados ficam contidos localmente. Compensação que falha devolve
  `MANUAL_ACK_COMPENSATION_UNKNOWN` e MANTÉM a contenção.
- **Risco:** a compensação pode falhar (banco fora) e aí o símbolo fica contido
  neste processo até intervenção — contenção é o lado seguro.
- **Prova:** `tests/pg_integration_manual_single.py` F2 (timeout REAL de 500 ms
  na `917283`, compensação desfazendo o `CLOSED`, compensação falhando →
  UNKNOWN + contenção, contenção por símbolo não travando proteção alheia).

## F3 — carimbos originais, idade reconferida

- **Antes:** `int(observation.get("observed_end_ms") or _now_ms())` — ausência,
  zero ou bool viravam o relógio do commit; a idade era medida uma vez, antes
  das consultas de ordens.
- **Depois:** `observation_window` valida início/fim como inteiros legítimos
  (não bool, finito, não futuro, início ≤ fim, duração dentro do limite) e
  carrega a janela ORIGINAL até o commit; `window_age_ok` reconfere a idade com
  o relógio ATUAL depois das consultas e depois da espera pela lock. A prova
  publicada usa o carimbo da OBSERVAÇÃO (`recorded_at_ms` é separado). Encerrar
  um reconhecimento exige flat do próprio símbolo **e** ausência de ordens
  provada pelas DUAS fontes, dentro da janela — evidência de ordens velha
  rebaixa o `CLOSED` para `WAITING_ORDERS` com a causa registrada.
- **Risco:** ciclos em máquina lenta podem rebaixar para `WAITING_ORDERS` mais
  vezes; o símbolo continua bloqueado e o ciclo seguinte reavalia.
- **Prova:** F3 no harness PG (21 s recusado, cinco carimbos ilegítimos
  recusados, janela válida encerrando de verdade, prova com carimbo original,
  ordens velhas não fechando).

## F4 — proposta congelada e último exame SÍNCRONO

- **Antes:** a autorização final reconferia token/lease/ownership, não a
  cotação/profundidade. Entre a aprovação do P04 e a assinatura cabiam 1,7 s de
  carteira, e o POST saía com evidência vencida.
- **Depois:** a admissão serializada que autoriza a qty final CONGELA uma
  proposta por despacho (identidade, plano quantizado, evidência temporal
  ORIGINAL de quote/profundidade/carteira, limites vigentes, token e deadline do
  lease), com hash determinístico sobre campos canônicos, gravada como chave
  NOVA no JSON existente da decisão (`decision_payload["proposals"][coid]`), sem
  tocar plano/identidade. `authorize_dispatch` passou a devolver a proposta
  PERSISTIDA (lida sob a mesma lock) e, com ela, o exame SÍNCRONO que o
  transporte roda imediatamente antes de `_build_signed_url` — sem nenhum
  `await` no meio. Esse exame recalcula o hash, confere idade/lease, **re-executa
  os avaliadores P04 REAIS** sobre a evidência original com o relógio atual e
  compara campo a campo o payload (símbolo, lado, tipo, GTX, qty, preço, COID,
  ausência de `reduceOnly`, ausência de `price`/`stopPrice` em MARKET).
  Ausência/vencimento/adulteração/payload diferente ⇒ `_request_sent=False`,
  razão explícita e ZERO POST. Só um veredito assíncrono POSITIVO produz o exame
  síncrono.
- **Risco:** em máquina lenta, mais recusas `EXEC_PROPOSAL_EXPIRED`/
  `EXEC_*_STALE` (nenhuma ordem enviada). O teto de idade é a TTL que o próprio
  P04 declarou — nenhum limite novo foi inventado, e nenhuma TTL foi elevada.
- **Prova:** `F4…` nos testes puros (TTL 1500 com 1499 **e** 1501, hash estável
  no ida-e-volta do JSONB, adulteração, oito payloads divergentes, proposta maker
  não autorizando a filha `-mfb`, MARKET sem price/stop) e no harness PG (m1,
  m4, m5, m11 — carteira de 1,7 s ⇒ zero POST).

## F5 — causa manual decidida DENTRO da transação

- **Antes:** `_maybe_release_quarantine` consultava a causa manual em OUTRA
  conexão e `release_p03_pause`/`set_manual_pause(False)` só contavam incidentes.
  Zero incidentes liberava a pausa mesmo com `manual_validation_blocked=true`.
- **Depois:** `manual_cause_in_session` (helper `_in_session`, recebe a sessão
  aberta e a conta comprovada) lê época/bloqueio/causa pendente DENTRO da
  transação, travando a linha da época até o commit. Usado em
  `release_p03_pause` **e** em `set_manual_pause(False)`: causa manual
  bloqueada/pendente/ilegível devolve resultado estruturado distinto
  (`MANUAL_CAUSE_PENDING`, `kept_manual_cause=True`), mantém a pausa com
  marcador próprio e NÃO limpa o latch do operador. `_maybe_release_quarantine`
  reconfere o fence local depois do await do release antes de limpar o owner.
- **Risco:** a retomada do operador pode ser recusada enquanto a validação
  manual não for refeita. É o ponto do pacote; a retomada volta a funcionar
  assim que o ciclo oficial revalida (provado no mesmo harness).
- **Ordem de locks desta fronteira:** latch local → advisory `_P03_PAUSE_LOCK` →
  linha da época (FOR UPDATE) → `risk_state` → UM commit. Nada aqui pede
  `917283`, e o publicador manual (que pede `917283`) nunca pede
  `_P03_PAUSE_LOCK` — sem inversão possível.
- **Prova:** F5 no harness PG, inclusive a CORRIDA real (escritor concorrente
  comprovadamente bloqueado via `pg_locks`/`pg_stat_activity`), o release
  legítimo acontecendo e a falha conhecida durante o release re-armando o latch.

## F6 — recheck com CAS na MESMA transação

- **Antes:** o caminho FLAT conferia só `.ok` da leitura; o CAS do contexto e a
  resolução do incidente ficavam em transações diferentes, então um
  reconhecimento criado durante o GET não impedia o `FLAT`.
- **Depois:** `update_claimed_guarded` (nos DOIS repositórios) avalia a
  conferência DENTRO da mesma sessão/transação do UPDATE fencado do incidente.
  O guard compara, na ordem de locks oficial (advisory `917283` → época →
  reconhecimentos por id): fence local, causa pendente, contenção local,
  conta/escopo, época manual, conjunto de reconhecimentos e
  revisão/estado/fingerprint de cada um, além de reconferir a evidência de
  ordens. CAS perdido mantém o incidente com a causa, DEVOLVE o claim de forma
  fencada e agenda novo ciclo.
- **Risco:** incidentes podem demorar um ciclo a mais para resolver quando há
  concorrência — nunca resolvem indevidamente.
- **Prova:** F6 no harness PG (reconhecimento novo durante o GET impede o FLAT;
  posição aberta nunca vira FLAT; contexto estável resolve em UMA transação).

## F7 — TERMINAL idêntico é no-op econômico

- **Antes:** `_resolve` excluía só `CONFIRMED`; `TERMINAL→TERMINAL` casava,
  reescrevia razão/carimbos e incrementava a geração financeira.
- **Depois:** `classify_resolution` distingue transição EFETIVA, estado-alvo JÁ
  aplicado, evidência CONTRADITÓRIA e transição PROIBIDA. Repetição idêntica é
  sucesso idempotente sem UPDATE, sem época nova e sem tocar
  reserva/vínculo/carimbos/owner alheio. `CONFIRMED` não é rebaixado e
  `TERMINAL` não é reaberto como `UNKNOWN`; vínculo contábil diferente no mesmo
  estado exige reconciliação. O mesmo veredito virou backstop de corrida no
  próprio `WHERE` do UPDATE.
- **Risco:** um reconciliador que dependia do incremento por repetição deixa de
  recebê-lo — era justamente o evento econômico inexistente.
- **Prova:** predicado puro (`F7…`) e harness PG (11→12→12, dois
  reconciliadores simultâneos + restart com UM incremento, token de outra
  entrada ainda autorizando depois da repetição, reservas somadas sem retirada
  dupla).

## Dois achados da auto-revisão do diff (corrigidos aqui)

1. **Conexão nova sob a `917283`.** A primeira versão da reconferência pós-lock
   em `_commit_validation` chamava `register_validation_failure` DENTRO da
   transação — isto é, pedia a mesma advisory lock em outra conexão e travaria.
   Agora o latch local é armado ali (síncrono) e a persistência da causa
   acontece em `_publish_validation`, com a sessão anterior já FECHADA. O caminho
   passou a ter teste próprio (`f3_idade_reconferida_depois_da_espera_pela_lock`,
   com `asyncio.wait_for` — se voltar a travar, o teste falha).
2. **Contenção local na fronteira transacional.** `ownership_guard` honrava a
   contenção de símbolo, mas `check_ownership_in_session` (usada pela admissão e
   pelo despacho) não. Corrigido, com teste
   (`f2_contencao_vale_na_fronteira_transacional`) que também prova que a
   contenção é POR SÍMBOLO e não trava proteção de outro.

## Limitações que permanecem

- **Falha local não commitada não é durável nem conhecida por outro processo.**
  O fence e a contenção de símbolo são por PROCESSO; outro processo só vê a
  causa depois que ela é persistida (época manual + `blocked`). Reinício com o
  estado local zerado não recupera autoridade: o estado durável manda.
- **TOCTOU com a exchange continua.** Mudanças feitas direto na corretora não
  incrementam época nenhuma; a leitura fresca continua obrigatória e não existe
  atomicidade entre nossa transação e a Binance.
- A proposta congelada prova o que ESTE processo admitiu; ela não impede que a
  corretora rejeite a ordem depois, e não substitui a reconciliação P03.
- Reconhecer uma posição manual continua não protegendo nada: o bot não instala
  nem cancela proteção dela.
- Nada aqui liga operação real, reconhece posição real ou libera pausa real.

## Runbook de UM único deploy posterior

1. **Boot fechado:** subir com a validação manual bloqueada (default durável) e
   sem liberar pausa. Nenhum passo deste runbook reconhece posição.
2. **Migrações:** `init_db` idempotente — este pacote NÃO adiciona coluna,
   índice ou tabela. Rodar normalmente e confirmar que nada novo foi criado.
3. **Health:** `/api/health` com `startup_at` novo; `/api/live/preflight` para os
   gates.
4. **Validação manual autenticada** só quando autorizado pelo operador: ciclo
   oficial (captura → GET fresco → validação completa) é o único caminho que
   desbloqueia a conta.
5. **Observar um ciclo** de reconciliação e um ciclo de entradas antes de
   qualquer ramp. Em caso de `MANUAL_CAUSE_PENDING`, `EXEC_PROPOSAL_*` ou
   `MANUAL_ACK_*`, ler a razão estruturada — todas são fail-closed e nenhuma
   exige intervenção na exchange.
