# Fechamento manual/BOT — baseline `20580139`

Correção integrada dos cinco achados de `docs/REVISAO_20580139_MANUAL_BOT.md`
(oito probes, T1–T8). Pronta para **um único deploy**. Nada foi publicado,
nenhuma posição real reconhecida e nenhuma pausa real liberada nesta execução.

## 1. Ordem única de locks

Todos os escritores deste protocolo seguem a MESMA sequência — foi a inversão
entre reserva e resolução que o PostgreSQL acusava como `deadlock detected`:

1. latch local (quando houver) **antes** de abrir a transação;
2. `BEGIN` → `pg_advisory_xact_lock(917283)` **antes** de qualquer leitura
   decisória ou row lock (`acquire_risk_lock`);
3. linha da **época da conta** (`account_margin_epochs`) antes das linhas de
   intenção (`_lock_epoch_row`);
4. linhas de reconhecimento/intenção em ordem determinística de `id`/chave;
5. escrita/prova/CAS → **commit único** → liberação.

Entram nessa ordem: `reserve`, `admit_final_risk`, `authorize_dispatch`,
`_resolve` (CONFIRMED/UNKNOWN/TERMINAL), `release_reserved`, `mark_sending`,
`register_dispatch`, `recover_stale`, `bump_margin_generation_for`,
`_transition_acks`, `_commit_validation`, `_persist_validation_failure`,
`capture_validation_context` e `_persist_acknowledgement`.

Helpers internos **recebem a sessão já aberta**: nenhum deles abre outra conexão
enquanto a atual segura a lock (isso travaria contra a própria operação). A
ordem já exigida pelo risco/P03 ao tocar `RiskState`/incidente é preservada.

## 2. Quatro colunas novas (migração aditiva e idempotente)

`manual_position_acks`:

| Coluna | Papel |
|---|---|
| `validated_revision BIGINT NULL` | revisão EXATA do reconhecimento validado |
| `validated_generation BIGINT NULL` | revisão da **validação de conta** que governa aquela prova (não é a geração financeira) |

`account_margin_epochs`:

| Coluna | Papel |
|---|---|
| `manual_validation_generation BIGINT NOT NULL DEFAULT 0` | época da validação manual daquela conta |
| `manual_validation_blocked BOOLEAN NOT NULL DEFAULT true` | estado **durável** de validação |

Os dois contadores são **independentes**: renovar prova manual não incrementa a
geração financeira, e uma carteira financeira recente não mascara falha de
validação manual. `manual_validation_blocked` **não é feature flag**: é estado
durável. Linha nova (ou vinda de `20580139`) nasce **bloqueada** e só uma leitura
COMPLETA de conta, commitada, libera — nunca há backfill com relógio local.

Testado em schema novo, em upgrade vindo de `20580139` e com `init_db` repetido;
nenhuma linha é apagada e os índices de unicidade corretos não foram alterados.

## 3. Token financeiro: produção e consumo

**Produção.** `reserve` mantém referência à linha criada, incrementa a época e
grava a geração RESULTANTE **nessa linha antes do commit**; devolve exatamente o
valor persistido. Retomada e readmissão aprovadas gravam SEMPRE a geração
resultante — inclusive sem aumento de margem (aí usa a época atual, sem bump).
Token `0` é válido; token ausente **não** é zero, e `bool`/texto/NaN/inf são
recusados (`_finite_token`).

`_margin_gate_for` mantém o fluxo: época sob lock → transação fecha → carteira
realmente fresca fora do banco → lock readquirida e época comparada.
`as_of_ms` nunca é regenerado.

**Consumo.** `authorize_dispatch` (transação CURTA sob `917283`) exige, em
conjunto: intenção existente; `SENDING` com owner correto e lease válido pelo
relógio POSTERIOR à espera; `dispatch_id` já registrado; token presente e igual à
coluna da intenção; essa coluna igual à geração FINANCEIRA vigente da conta;
validação manual não bloqueada; ownership coerente. Época inexistente **não** é
recriada como autorização. `may_dispatch` isolado não substitui esse contrato.

**Preparação × autorização final.** `_intent_dispatch_guard` virou
**PREPARAÇÃO** (identidade/estado/owner/lease e `SENDING`) e **não** exige token
vigente — é a etapa seguinte que pode renová-lo. `_intent_guarded_preflight`
grava o dispatch exato, executa quote/carteira/gates/readmissão e só então chama
a **AUTORIZAÇÃO FINAL** (`_intent_final_authorization`). O transporte recebe esse
callback por contexto INTERNO de mutação e o executa em `_signed_request` depois
do throttle **e depois do await de ownership**, imediatamente antes de assinar.
Cada tentativa maker, MARKET e filha `-mfb` tem seu dispatch gravado e é
verificada; negação devolve `_request_sent=False` para aquela requisição e não
prova ausência de fill de tentativa anterior.

## 4. Observação: contexto ANTES do GET

`capture_validation_context(scope, symbol)` captura sob a lock e fecha a
transação **antes** da rede: conta/exchange/mercado, escopo `ACCOUNT|SYMBOL` e
alvo, `manual_validation_generation`, fence local de falha, e o instantâneo de
cada reconhecimento do escopo (`id`, `revision`, `fingerprint`, `state`,
`symbol`, `contract_version`), além do instante real de início.

No commit, cada linha é conferida por **CAS** (revisão + estado + fingerprint) e
a época manual é comparada. Registro criado ou substituído durante o GET não
pertence à captura antiga e **não** pode ser fechado/invalidado/validado por ela:
o veredicto é `MANUAL_ACK_STALE_CONTEXT`, que **descarta o resultado e pede novo
ciclo** — não arma pausa, não incrementa épocas e não revoga a prova do vencedor.

Callers corrigidos: `revalidate_active`, `_revalidate_manual_acks_fresh`,
`_revalidate_manual_acks`, `_detect_untracked_positions` (captura antes de
`get_positions(force=True)` e repassa o contexto), `_manual_ack_outcome` e
`recheck_untracked_manual` (contexto antes da leitura de flat e das duas
consultas de ordens). `observation_from_rows` não fabrica horário nem token:
observação pronta sem contexto compatível não publica prova nem encerra nada.

Flat → `CLOSED` continua exigindo posição fresca ausente **e** duas listagens
completas vazias (comuns **e** condicionais) dentro do MESMO contexto.
`WAITING_ORDERS` não libera símbolo.

## 5. Prova publicada e revogada atomicamente

`record_validation_proof([ids])` foi substituída por
`publish_validation_proof(context, observation, …)`: sem contexto/evidência não
há publicação, e `valid` contém **somente** linhas cuja prova foi efetivamente
COMMITADA (nunca candidatos nem quem perdeu CAS).

Uma transação aplica transições, revogações e provas coerentes. Mudança de
identidade/conta/estado incrementa `revision` e **limpa no mesmo commit**
`validated_at_ms`, `validation_scope`, `validation_account`,
`validated_revision` e `validated_generation`. `INVALIDATED`/`WAITING_ORDERS`
não recebem prova autorizadora mesmo que o fingerprint volte a coincidir — só
nova confirmação administrativa reativa.

`proof_is_valid` (usado por guard, ownership e admissão) exige em conjunto:
`ACTIVE`; `revision == validated_revision`; conta de validação == conta atual;
época da prova == época manual vigente; contrato/exchange/mercado corretos;
escopo reconhecido e aplicável (prova `SYMBOL` vale para o reconhecimento
daquele símbolo e **nunca** limpa `manual_validation_blocked` de conta);
carimbo inteiro positivo e **não futuro**; idade dentro do limite
(`VALIDATION_MAX_AGE_S = 900s`). Não há `max(0, idade)` validando futuro.

## 6. Falha conhecida bloqueia na hora e sobrevive a restart

Ao detectar resposta EXTERNA stale/erro/incompleta ou falha de persistência
(distinto do `STALE_CONTEXT` de concorrência):

1. `register_validation_failure` avança o **fence local monotônico** e registra
   a causa pendente **antes** de tentar persistir;
2. sob `917283`, `manual_validation_generation++`, `blocked=true` e revogação
   das provas alcançadas, com CAS/revisão corretos;
3. leitor iniciado **antes** da falha perde o fence e não repara autorização
   antiga, mesmo com o ACK ainda `ACTIVE`;
4. erro de banco mantém latch/causa pendente e devolve UNKNOWN — como o commit
   pode não ter ocorrido, o publicador também recusa contexto cujo fence local
   anteceda a falha;
5. quando o banco volta, `flush_pending_validation_failure` persiste a falha
   **primeiro**; só então um GET NOVO pode recuperar;
6. boot começa fechado (`reset_local_validation_state` + estado durável): TTL
   antigo não recupera permissão.

Divergência real exige nova confirmação administrativa
(`INVALIDATED`/`WAITING_ORDERS`); falha transitória pode manter `ACTIVE` com
prova revogada, recuperável por leitura nova. Ambos impedem nova exposição
enquanto a conta estiver bloqueada.

**Recuperação (§7.2).** Uma observação `ACCOUNT` completa, iniciada após a época
vigente, limpa `blocked` somente se todos os reconhecimentos bloqueantes atuais
estiverem comprovados ou corretamente encerrados, sem contexto novo não avaliado,
CAS perdido, persistência incompleta ou estado inválido restante — provas e
recuperação no MESMO commit. Conta sem ACKs ainda exige scan completo. A causa
manual pendente é consultada por `reconcile_due`, pelo boot e por
`_maybe_release_quarantine`: **zero incidentes não remove causa manual**, e a
recuperação remove apenas o owner/causa deste pacote, preservando pausa manual,
P02, legacy, outros incidentes e owners.

## 7. Redução classificada uma única vez

`classify_order_action(reduce_only=…)` exige o booleano **literal** `True`:
string, número ou objeto não recebem isenção. A classificação é derivada UMA vez
em `place_order` e usada no guard inicial, no preflight e no guard final, e o
payload real carrega `reduceOnly=true` quando classificado assim.

Payload contraditório (`reduce_only` com `leverage`, `stop_loss`, `tp1` ou
`take_profit` de abertura) é recusado com `EXEC_REDUCE_ONLY_CONTRADICTORY`
**antes de qualquer mutação**. Redução/proteção BOT comprovada em OUTRO símbolo
não depende do token financeiro de nova entrada nem da prova manual alheia, mas
continua exigindo ownership do próprio símbolo e os guards de lease existentes.
O `mutation_guard` de proteção roda **também** na fronteira pós-throttle de cada
POST/retry/fallback; negação não vira SL instalado nem cancelamento confirmado.
Registro ilegível nunca é lista vazia segura; símbolo manual bloqueia toda
mutação nos dois lados.

## 8. Boot e legado

- Boot começa **fechado**: exige captura, GET e commit novos.
- Reconhecimento legado sem prova completa permanece **sem autorização** até o
  próximo ciclo real (nunca há backfill).
- Linha de época vinda de `20580139` entra com `manual_validation_blocked=true`
  e `manual_validation_generation=0`.
- Legado ambíguo (duas linhas bloqueantes na mesma identidade) fica
  **fail-closed** (`MANUAL_ACK_REGISTRY_AMBIGUOUS`), sem escolher linha.
- Conta sem credencial comprovada: subsistema **inerte**
  (`MANUAL_VALIDATION_INERT`) — sem conta não há reconhecimento, e o transporte
  já recusa por `is_configured`, enquanto nenhuma intenção pode ser reservada.
- Reconhecimentos de OUTRA conta não bloqueiam nem autorizam a conta vigente; o
  histórico delas é preservado.

## 9. Limites externos reais

- A geração é **local**: mudanças feitas direto na corretora não a incrementam.
  A leitura fresca continua obrigatória e a janela externa residual permanece —
  **não há atomicidade com a exchange**, e este pacote não promete isso.
- Nenhuma transação ou lock de banco é mantida durante HTTP.
- `updateTime` prova alteração, não continuidade: reabertura coincidente em
  ticker/qty exige nova confirmação.
- O custo conservador reservado na margem é limite superior, não contabilidade.
- Reconhecer **não** certifica proteção da posição manual.

## 10. Rollback conservador

Rollback **não** apaga reconhecimento, provas ou histórico, e **não** libera
pausa pendente. Para desfazer: invalidar o reconhecimento (divergência de
identidade já faz isso), o que leva a `INVALIDATED` — símbolo segue bloqueado, a
posição volta a ser `UNTRACKED` e a conta fica aguardando ciclo completo.
Reativar exige confirmação NOVA (que grava `SUPERSEDED` no anterior) ou
encerramento comprovado.

## 11. Runbook de publicação (NÃO executado nesta entrega)

1. Deploy ÚNICO do commit final.
2. Conferir as migrações: quatro colunas novas presentes e índices de unicidade
   intactos (`uq_manual_ack_open`, `uq_margin_epoch_identity`).
3. Boot/health e estado de contenção: a conta entra **bloqueada**; confirmar que
   o ciclo oficial publica prova e limpa `manual_validation_blocked`.
4. `GET /api/admin/manual-positions/candidates` autenticado, com estado fresco.
5. Confirmação real **somente quando o operador solicitar** — ela avança o fence
   manual e deixa a conta aguardando o próximo ciclo completo.
6. Observar o ciclo e confirmar preservação de pausas, owners, limites e filtros.

Nenhuma ordem de teste real é criada em qualquer passo.
