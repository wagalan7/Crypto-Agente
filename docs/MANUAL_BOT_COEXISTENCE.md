# Convivência manual/bot na MESMA conta

Baseline: `8565903e`. Esta entrega **não** liga nada e **não** autoriza operar a
conta. Ela remove UM impedimento específico (posição manual travando o bot por
inteiro) e cria a contenção que torna essa convivência segura.

## 1. Contrato de negócio (decidido pelo usuário)

Usar a mesma conta Binance, reconhecer explicitamente uma posição aberta
manualmente e deixar o bot operar OUTROS símbolos pelos seus próprios limites,
sem consumir o orçamento nominal do bot com a posição manual.

1. Posição manual **explicitamente reconhecida** não ocupa slot nem orçamento
   nominal de risco/notional do BOT; o resultado dela não vira resultado
   automático.
2. Equity, saldo disponível e margem continuam sendo os **reais** da conta. A
   margem da posição manual **não** é somada de volta e não existe banca virtual.
3. Os limites do bot continuam iguais. A soma do risco manual com o automático
   **pode** ultrapassar o limite nominal do bot — isto **não** isola o risco
   financeiro da conta.
4. Todo o **símbolo** da posição manual fica indisponível para automação,
   inclusive direção contrária e hedge. Outros símbolos continuam sujeitos a
   TODOS os filtros existentes.
5. O bot **não administra** a posição manual: não muda alavancagem, margem, modo
   de posição, quantidade, stop/TP; não fecha, cancela nem substitui ordens dela.
6. Reconhecer exige confirmação administrativa explícita e identidade atual
   verificável. **Ausência de `RealTrade` não significa origem manual.**
7. UNKNOWN, conflito BOT/manual e erro de persistência continuam fail-closed.
8. Reconhecer **não** certifica proteção da posição manual e não inventa SL.
9. Nada de estratégia, score, calibração, tier, filtro, stop/TP automático,
   sizing, limite, universo, alavancagem ou flag existente mudou.
10. Remover o impedimento **não garante** novas entradas.

## 2. O que foi implementado

### 2.1 Identidade observável da posição

`binance_signed_service.get_positions` passou a preservar, **aditivamente**,
`position_side` e `update_time_ms` da Binance. Campo ausente/inválido vira
`None` — nunca `BOTH` e nunca relógio local.

`manual_position_service.position_fingerprint` calcula a identidade
determinística sobre: conta (escopo opaco), exchange, mercado, símbolo/quote,
lado, `positionSide`, `qty`, preço de entrada e `updateTime`. Decimais são
canônicos (`2.50` == `2.5`); `bool`, `NaN`, infinito, qty/preço ≤ 0 e parcela
ausente **não** geram fingerprint. **Mark price e P&L não entram**: a oscilação
deles não exige novo reconhecimento.

**Limitação do provedor, declarada:** a Binance USD-M **não** expõe um id de
posição. `updateTime` é a única versão temporal observável e muda a cada
alteração da posição — ele prova *mudança*, não *continuidade*. Por isso:
reabertura com a mesma qty e o mesmo ticker **não** é reconhecida
automaticamente; sem `updateTime` a identidade fica UNKNOWN e é preciso novo
reconhecimento.

### 2.2 Registro dedicado (`manual_position_acks`)

Tabela nova, pequena e **separada de `RealTrade`** — justamente para o trade
manager não assumir a gestão da posição. Guarda: escopo opaco da conta,
exchange/mercado, símbolo/quote canônicos, lado, `positionSide`, `qty` e preço
de entrada em `NUMERIC(38,18)`, `exchange_update_time_ms`, fingerprint, versão do
contrato (`MANUAL_ACK_V1`), estado `ACTIVE|INVALIDATED|CLOSED`, motivo e
identidade administrativa **não secreta**, chave do incidente vinculado,
evidências e timestamps.

Migração **aditiva e idempotente** pelo caminho oficial (`db.init_db`):
`create_all` + `CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_ack_active … WHERE
state = 'ACTIVE'` + `ALTER TABLE entry_intents ADD COLUMN IF NOT EXISTS
reserved_margin_usd`. Nenhuma linha é sobrescrita ou apagada: invalidar/encerrar
carimba `state`, `ended_at` e `ended_reason`.

Não há allowlist permanente de símbolos nem ticker hardcoded. **Múltiplas pernas
ambíguas no mesmo símbolo são RECUSADAS**, nunca condensadas numa posição
fictícia.

### 2.3 API administrativa (dois endpoints, nada genérico)

| Método | Rota | Papel |
|---|---|---|
| GET | `/api/admin/manual-positions/candidates` | posições frescas + fingerprint de confirmação |
| POST | `/api/admin/manual-positions/acknowledge` | reconhece UMA posição |

Ambos passam por `_check_admin_token`. O POST usa schema **estrito**
(`extra="forbid"`) e exige `confirm` booleano **literal** — `"true"`, `1` e
`"on"` são recusados. Não existe `resolve`, `clear-pause`, `enable-live` ou
`execute`, e nenhuma UI foi criada. Nenhuma resposta ou log carrega segredo,
credencial ou stack trace.

Ordem do POST:

1. confirmação literal → conta comprovada → **leitura fresca da exchange (fora
   de transação)**;
2. perna ÚNICA do símbolo e fingerprint idêntico ao informado;
3. ordens condicionais abertas atribuíveis ao BOT (listagem indisponível =
   prova inconclusiva = bloqueio);
4. transação sob a **MESMA advisory lock `917283`** da admissão de entrada, onde
   a idade da leitura é validada e as provas decisórias são **relidas**:
   ausência de `RealTrade` BOT/`managed` aberto, ausência de intenção pendente,
   ausência de intenção tocada **depois** da leitura (reserva concorrente) e
   ausência de incidente de execução conflitante;
5. gravação do registro **e** vínculo com o incidente `UNTRACKED_POSITION` no
   MESMO commit.

Repetir a mesma confirmação é idempotente. Confirmação divergente com registro
ativo é recusada. **Sucesso do POST não é autorização de entrada** — a resposta
diz isso explicitamente.

### 2.4 Reconciliação (fluxo P03 oficial, sem reconciliador novo)

- Estado terminal novo `MANUAL_ACKNOWLEDGED`, aplicável **apenas** ao incidente
  `UNTRACKED_POSITION` da posição manual comprovada. Posição aberta **nunca**
  vira `FLAT` nem `PROTECTED`.
- `MANUAL_ACKNOWLEDGED` **não** entra em `_TERMINAL_SAFE`: ele não prova nada
  sobre uma ordem despachada pelo bot e não liquida intenção de entrada.
  `ENTRY_UNKNOWN`, cleanup, fill positivo, prova conflitante e
  `PERSISTENCE_FAILURE` continuam intocados.
- No boot (`_detect_untracked_positions`), os reconhecimentos são revalidados
  contra a leitura fresca **antes** de o scan ser considerado seguro. Registro
  ilegível ou leitura incerta ⇒ `UNKNOWN` + quarentena.
- Identidade divergente ⇒ `INVALIDATED` e a contenção reabre no ciclo seguinte.
- A liberação da pausa continua sendo a do P03 (`_maybe_release_quarantine`),
  owner-aware: pausa manual e owners P02/legacy são preservados.
- Fechamento comprovado encerra o registro (`CLOSED`) **somente** depois de
  provar, com leitura fresca, que não restam ordens/condicionais do operador —
  e **nenhuma delas é cancelada**.
- Conflito com adoção `managed` é recusado explicitamente; manual reconhecida
  nunca vira `managed` em segundo plano.

### 2.5 Guard ÚNICO de propriedade

`manual_position_service.ownership_guard(symbol, action=…)` responde por
conta/símbolo e é composto **na borda do transporte** (`binance_signed_service`),
antes da primeira mutação:

`set_leverage`, `place_order`, `place_maker_entry_then_protect`,
`place_protection_orders`, `cancel_order` e `cancel_algo_order`.

Isso cobre entrada normal e maker, fallback MARKET, flip, hedge, pyramiding, TF
upgrade, instalação/substituição/cancelamento de proteção, fechamento,
time-stop, BE/trailing, autoheal/backfill e poeira — todos passam por essas
funções. Bloquear só o POST de entrada seria insuficiente: **`set_leverage` já
alteraria a posição manual**. O guard roda **de novo no preflight final**, depois
do throttle e imediatamente antes do POST, inclusive em retries e no fallback.

Regras fail-closed: registro ilegível bloqueia; símbolo reconhecido bloqueia nos
dois lados; símbolo **desconhecido** pelo chamador, havendo reconhecimento ativo,
bloqueia (prova inconclusiva). Sem reconhecimento ativo, nada muda.

O guard **não** consulta a exchange: ele é chamado de dentro de locks/semáforos
da própria requisição, onde I/O HTTP recursivo é proibido. A prova fresca é
obtida fora deles (reconhecimento e ciclo do reconciliador).

`cancel_algo_order` ganhou o parâmetro **aditivo** `symbol=`; todos os chamadores
passam o símbolo do trade/incidente.

`trade_manager_service._fetch_exchange_position` deixou de escolher a **primeira**
posição do símbolo: agora exige lado (e `positionSide` quando disponível), e
pernas ambíguas viram leitura **incerta** (`None`), que os chamadores já tratam
sem mutar nada. Em one-way, posição agregada manual+BOT **não** é separável: o
símbolo inteiro fica bloqueado, nada é estimado.

### 2.6 Orçamento BOT separado; margem real obrigatória

Auditoria das coortes: posição manual **não é `RealTrade`**, então não entra em
slot, risco aberto, notional, P&L diário, streak nem contagem diária — nem nas
coortes que filtram `source='auto'`, nem nas que não filtram (kill-switch). O
tratamento de `managed` e das demais fontes ficou intacto, e um trade
`source=auto` encerrado manualmente **continua automático**. Nenhuma fórmula,
denominador de equity, limite ou default foi alterado.

Gate de margem REAL, novo e **independente** do orçamento nominal:

- `entry_intent_service.MarginGate` (disponível, requerido, `as_of_ms`, idade
  máxima, `complete`) e `_margin_reason`;
- `entry_intents.reserved_margin_usd` (coluna aditiva) entra no snapshot ÚNICO
  de admissão (`financial_risk_service`), lido sob a MESMA lock — duas propostas
  concorrentes não gastam o mesmo saldo livre;
- `shadow_trade_service._proposed_margin_usd` = `notional / leverage` (a MESMA
  fórmula do cap de margem já existente) + custo conservador de ida e volta
  (`MARGIN_COST_BPS_PER_SIDE = 5`, limite superior da taxa taker — só torna o
  gate mais severo);
- a carteira é lida **fora** da transação (`get_equity(force=True)`) e a **idade
  da leitura é validada dentro dela**: carteira velha vira `FREE_MARGIN_STALE`,
  não "atual";
- `available_usd` já reflete a margem em uso na conta (inclusive a da manual):
  ela **não** é somada de volta nem descontada de novo;
- ausência vira `FREE_MARGIN_UNKNOWN` — nunca zero, nunca estimativa favorável;
- a margem FINAL (após arredondamentos) é readmitida antes do dispatch
  (`admit_final_risk`), substituindo a reserva menor; timeout/resultado ambíguo
  retém a reserva pela reconciliação que já existe;
- alavancagem **não** é alterada para fazer uma proposta caber.

Limites P03/R05 e kill-switch continuam independentes: passar em um não dispensa
os outros.

## 3. Motivos de bloqueio (todos auditáveis)

| Código | Significado |
|---|---|
| `MANUAL_ACK_CONFIRM_NOT_LITERAL` | `confirm` não é o booleano `true` |
| `MANUAL_ACK_NO_ACCOUNT` | conta/credencial não comprovada |
| `MANUAL_ACK_POSITION_UNKNOWN` | leitura stale/rate-limited/erro |
| `MANUAL_ACK_POSITION_ABSENT` | posição não existe na leitura fresca |
| `MANUAL_ACK_AMBIGUOUS_LEGS` | mais de uma perna no símbolo |
| `MANUAL_ACK_IDENTITY_INCOMPLETE` | falta lado/perna/qty/entrada/`updateTime` |
| `MANUAL_ACK_FINGERPRINT_MISMATCH` | identidade mudou desde o GET |
| `MANUAL_ACK_READ_TOO_OLD` | leitura velha demais para decidir |
| `MANUAL_ACK_BOT_TRADE_PRESENT` | `RealTrade` auto/`managed` aberto no símbolo |
| `MANUAL_ACK_INTENT_PENDING` | intenção pendente (ou tocada após a leitura) |
| `MANUAL_ACK_INCIDENT_CONFLICT` | incidente de execução aberto no símbolo |
| `MANUAL_ACK_BOT_ORDERS_PRESENT` | condicional aberta atribuível ao bot |
| `MANUAL_ACK_ORDERS_UNKNOWN` | listagem de ordens indisponível |
| `MANUAL_ACK_CONFLICTING_ACTIVE` | já existe reconhecimento ativo divergente |
| `MANUAL_ACK_DB_UNAVAILABLE` | persistência indisponível |
| `MANUAL_POSITION_SYMBOL_BLOCKED` | símbolo com posição manual reconhecida |
| `MANUAL_ACK_REGISTRY_UNAVAILABLE` | registro ilegível (fail-closed) |
| `MANUAL_OWNERSHIP_SYMBOL_UNKNOWN` | mutação sem símbolo com ack ativo |
| `FREE_MARGIN_UNKNOWN` / `FREE_MARGIN_STALE` / `INSUFFICIENT_FREE_MARGIN` | gate de margem real |

## 4. Passo a passo administrativo (runbook — NÃO executado nesta entrega)

1. **Deploy conferido** (`/api/health` com `startup_at` novo).
2. `GET /api/admin/manual-positions/candidates` com `X-Admin-Token`. Conferir
   que a posição aparece, que `identity_complete` é `true` e anotar o
   `fingerprint`.
3. `POST /api/admin/manual-positions/acknowledge` com
   `{"symbol": "...", "fingerprint": "<o do passo 2>", "confirm": true,
   "reason": "...", "identity_note": "..."}`.
   Fingerprint velho, confirmação não literal ou rastro do bot **recusam**.
4. **Observar o ciclo oficial**: o incidente `UNTRACKED_POSITION` daquele
   símbolo deve terminar em `MANUAL_ACKNOWLEDGED`
   (`GET /api/execution-incidents/status`) e a pausa P03 só cai quando **todos**
   os incidentes estiverem resolvidos.
5. **Confirmar preservação**: a posição manual segue intacta (qty, alavancagem,
   ordens), as pausas de outros owners continuam, e os filtros do bot seguem
   valendo nos demais símbolos.

**Rollback não é liberar pausa.** Para desfazer: invalidar o reconhecimento (a
identidade divergir já faz isso sozinho) — a posição volta a ser `UNTRACKED`, a
contenção reabre e o bot para de operar novos símbolos até o fluxo P03 liberar.
Nenhum histórico é apagado.

## 5. Teste local

PostgreSQL 16 descartável, UTF-8, socket Unix, TCP/DNS bloqueados. A exchange é
falsa; a lógica é a real.

```bash
PGBIN=/opt/homebrew/opt/postgresql@16/bin
SOCK=/tmp/cw-mbot-sock.$(python3 -c 'import secrets;print(secrets.token_hex(4))')
DATA=$(mktemp -d /tmp/cw-pgdata.XXXXXX); mkdir -p "$SOCK"
LC_ALL=C $PGBIN/initdb -D "$DATA" -U mbot --auth=trust \
  --encoding=UTF8 --lc-collate=C --lc-ctype=C
$PGBIN/pg_ctl -D "$DATA" -o "-k $SOCK -c listen_addresses=''" -w start
$PGBIN/createdb -h "$SOCK" -U mbot mbotdb
cd backend && MANUALBOT_TEST_SOCKET="$SOCK" PYTHONDONTWRITEBYTECODE=1 \
  .venv311/bin/python -B tests/pg_integration_manual_coexistence.py
$PGBIN/pg_ctl -D "$DATA" -m immediate stop; rm -rf "$DATA" "$SOCK"
```

```bash
cd backend && PYTHONDONTWRITEBYTECODE=1 \
  .venv311/bin/python -B -m unittest tests.test_manual_bot_coexistence -v
```

## 6. TOCTOU e limites assumidos

- **Janela inevitável.** Entre a última leitura e uma ação que o operador faça
  direto na Binance existe um intervalo. A advisory lock deste processo **não**
  impede o usuário de operar pela corretora. O contrato é: detectar divergência,
  conter NOVAS entradas e **nunca** "corrigir" fechando ou alterando posição
  alheia.
- **`updateTime` não é id de posição.** Ele prova alteração, não continuidade.
  Reabertura coincidente em ticker/qty **não** é reconhecida automaticamente.
- **Ordens de entrada comuns** (não condicionais) não são listadas pelo cliente
  Binance deste projeto. A cobertura vem do lado do banco: o bot grava o id
  EFETIVO de cada despacho **antes** do POST, então qualquer ordem do bot tem
  intenção pendente ou incidente — e ambos já bloqueiam o reconhecimento.
- **Margem:** o custo reservado é um **limite superior conservador**, não um
  número de contabilidade. O gate protege o saldo livre; ele não substitui
  R05/R05D nem a contabilidade com funding.
- **Reconhecer não protege.** A posição manual pode estar sem SL: isso é
  decisão do operador e o bot não instala nem cancela proteção dela.
- Esta entrega não foi executada contra a conta real, não emitiu ordem, não
  liberou pausa de produção e não fez deploy.
