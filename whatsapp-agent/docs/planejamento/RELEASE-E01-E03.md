# Release incremental 1 — E01–E03

Preparado em 14/10/2026. **Aguardando aprovação.** Nada foi publicado: sem push,
sem deploy, sem alteração em produção, sem mensagem real.

Branch local: `release/agente-consultorio-e01-e03`
Base: `1b723a8f` (= `origin/main` = build `2026-07-16-67` = o que está no ar)
Carimbo preparado: `2026-10-14-68`

---

## 1. O que entra

Seis commits, nesta ordem:

| # | Commit | Área | Risco |
|---|---|---|---|
| 1 | `783aa0fb` `fix(site)` | Depoimentos fabricados e métricas sem prova na landing | **Mínimo** — só texto; nenhuma mudança de comportamento |
| 2 | `52fcf73e` `chore(deploy)` | `watchPatterns` restrito; healthcheck documentado | Baixo — só vale se a Railway aplicar o arquivo |
| 3 | `f97c0174` `docs` | Plano v1.1, auditoria, `whatsapp-agent/CLAUDE.md` | Nenhum — documentação |
| 4 | `dc6dcf6c` `test` | Suíte (316), CI, config de teste | Nenhum em produção — não entra na imagem |
| 5 | `7ea95419` `fix(agenda)+feat(entrega)` | E02 seleção de horário + E03 recebimento durável | **Médio-alto** — 7 arquivos, 6 tabelas novas |
| 6 | `196de1f1` `chore(build)` | Carimbo `2026-10-14-68` | Nenhum — isolado para poder ser descartado |

Fora do release, de propósito: `.claude/settings.local.json`, `AGENTS.md`,
`.DS_Store`, `backend/static/prototipo-*.html`. O bot de cripto **não é tocado**
(`git diff --name-only` só devolve `whatsapp-agent/` e `.github/`).

## 2. Qual é o menor release coerente

### E02 sozinha: **não é possível** sem retrabalho

Verificado no código, não presumido:

- `agent._ofertar` grava via `db.offer_create`; `_resolve_slot` lê via
  `db.offer_context`. Ambas são da tabela `offers`, de **E03**.
- As funções de oferta de E02 (`save_slot_offer`, `get_slot_offer`,
  `mark_slot_offer_delivered`) **foram removidas** — a revisão de E03 mostrou
  que `mark_slot_offer_delivered(tenant, phone)` marcava a oferta errada depois
  de uma troca, e que `MAX(version)+1` não era identidade monotônica.
- `main` roteia os três webhooks por `_receber` e `_handle_message` mudou de
  assinatura para receber a decisão já tomada.

Publicar E02 isolada exigiria **reconstruir** a camada de ofertas que não existe
mais na árvore — código novo, sem teste, e que reintroduz a política rejeitada
(oferta não enviada resolvendo escolha numérica). Seria mais arriscado que
publicar o conjunto.

### O que **é** separável: a landing (commit 1)

Os depoimentos eram fixos no template, não vinham de variável. O commit 1 é
autossuficiente: nenhuma tabela, nenhuma mudança de fluxo, nenhuma alteração no
que o paciente recebe no WhatsApp.

### Recomendação

**Dois releases, em dias diferentes.**

| Release | Conteúdo | Quando |
|---|---|---|
| **R1** | commit 1 (+ 3 e 4, que não afetam execução) | assim que aprovado — resolve exposição jurídica hoje |
| **R2** | commits 2, 5 e 6 | depois de R1 observado, em janela combinada |

Publicar os seis de uma vez também é defensável e economiza uma janela. A
divisão existe porque R1 tem risco praticamente nulo e urgência real
(publicidade enganosa no ar), enquanto R2 mexe no caminho que atende paciente.

**R1 só é seguro sozinho se a Railway respeitar `watchPatterns`** — caso
contrário R1 e R2 disparam o mesmo rebuild e a divisão perde sentido. Ver §3.

## 3. Como a Railway publica — o que foi verificado e o que não

### Verificado (somente leitura, sem segredo)

| Item | Valor | Como |
|---|---|---|
| Repositório | `github.com/wagalan7/Crypto-Agente` | `git remote -v` |
| Branch de produção | `main` | `origin/HEAD` |
| Produção == `origin/main` | sim, build 67 | `/healthz`: `uptime_s` 1.086.475 s ≈ 12,6 dias ⇒ início ~01/10 21h, que é o deploy da build 67 |
| App no ar | `status ok`, `db ok`, `db_ms 0.6` | `GET /healthz` |
| Consultórios | 8 | idem |
| Memória | 256 MB RSS | idem |
| Sentry | ativo | idem |
| Rota de healthcheck | `/health` responde **200** | `GET /health` |
| Comando declarado | `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}` | `railway.toml` e `Dockerfile` |
| `DATA_DIR` declarado | `/data` | `Dockerfile` |
| Builder | Dockerfile, `whatsapp-agent/Dockerfile` | `railway.toml` |

### 🔴 NÃO VERIFICADO — exige o painel da Railway

Sem acesso ao painel (CLI deslogado; nenhuma credencial de API disponível).
**Cada item abaixo é um bloqueio de aprovação, não uma observação.**

| # | Pergunta | Por que importa |
|---|---|---|
| V1 | A Railway aplica **este** `railway.toml`? (Settings → Config-as-code) | Se não, `watchPatterns` e `healthcheckPath` dele não valem nada, e a divisão R1/R2 perde sentido |
| V2 | Qual é o **Root Directory** do serviço? | Se for `whatsapp-agent`, os caminhos do `watchPatterns` precisam ser relativos a ele — hoje estão relativos à raiz do repo e **não casariam nada** |
| V3 | **Existe volume montado em `/data`?** | O `Dockerfile` cria `/data` na imagem. Sem volume, cada deploy **zera o banco**. Os 8 consultórios sobreviveram a 19 builds, o que indica volume — mas indício não é confirmação |
| V4 | **Quais serviços escutam este repositório?** | O repo é o do bot de cripto. Se os serviços `Crypto-Agente` (produção, dinheiro real) ou `Crypto-Agente-DEV` disparam em qualquer push a `main`, publicar o Agente **reinicia o bot de cripto** |
| V5 | "Wait for CI" está ligado? | Sem isso, a Railway publica antes de o CI rodar |
| V6 | `FIELD_ENCRYPTION_KEY` existe? (existência, não valor) | Sem ela, credenciais e PIX são gravados em texto puro |
| V7 | `MP_WEBHOOK_SECRET` existe? | Sem ela o webhook do Mercado Pago devolve 503 e assinaturas por MP não ativam conta |
| V8 | `SMTP_HOST` existe? | Sem SMTP não há alerta de queda por e-mail |
| V9 | Há backup recente e restaurável do volume? | É o pré-requisito do item 5 deste roteiro |

**V3 e V4 são os que podem causar dano.** V3 arrisca perder o banco; V4 arrisca
reiniciar um bot que opera com dinheiro real.

### Roteiro de conferência (feito pelo proprietário, 10 min)

1. Railway → projeto **Agente de Atendimento** → serviço → Settings:
   anotar Root Directory, Config-as-code path, Wait for CI, branch.
2. Mesmo serviço → Volumes: existe volume? Mount path é `/data`?
3. Mesmo serviço → Variables: **só conferir existência** de
   `FIELD_ENCRYPTION_KEY`, `MP_WEBHOOK_SECRET`, `SMTP_HOST`, `MASTER_KEY`,
   `BASE_URL`. Não copiar valor para lugar algum.
4. Railway → projeto **Crypto-Agente** (e `Crypto-Agente-Dev`) → serviço →
   Settings: qual repositório, qual branch, qual `watchPatterns`/Root Directory.
5. GitHub → Settings → Branches: existe proteção em `main`?

## 4. Testes e evidências

Executados hoje, do estado commitado:

| Verificação | Resultado |
|---|---|
| Suíte completa | **316 passam, 0 ignorados, 0 falhas** |
| Versão do Python vs `Dockerfile` | 3.11 = 3.11 |
| `compileall` em `backend/` | OK |
| Carimbo de build na linha 2 | `# build: 2026-10-14-68` |
| Padrão de credencial no diff | nenhum |
| Dado pessoal real no diff | nenhum |
| `.env`, `.db`, dump no release | nenhum (gitignored) |
| Bot de cripto tocado | não |
| **Homologação ponta a ponta** | **26/26** (`homologacao/homologa_e2e.py`) |
| Migração desde o esquema de E02 | OK, em cópia fictícia; nada removido |

A homologação cobre: recebimento antes do ACK, deduplicação por reentrega,
agendamento no dia pedido, confirmação canônica, resposta pelo outbox, oferta
numérica (incluindo "a oferta não vale antes de a mensagem sair"), reinício com
retomada sem duplicar, envio incerto que não é reenviado, falha definitiva que
devolve a oferta anterior, e mensagem sem id do provedor que não muta nada.

**NÃO VERIFICADO** (precisa de conta de teste e autorização): WhatsApp real,
modelo real, Stripe/Mercado Pago, SMTP, Google/CalDAV, backup e restauração.

## 5. Procedimento de publicação

### Antes (pré-requisitos, todos obrigatórios)

- [ ] V1–V9 do §3 respondidos.
- [ ] **Backup do volume feito e verificado** (§6).
- [ ] Janela escolhida: **dia útil, 21h–22h** (fora de 7h–22h o agendador não
      envia; às 21h a janela já está no fim e o movimento é mínimo). Evitar o
      último dia do mês às 20h, quando roda a cobrança mensal, e as 3h do
      backup.
- [ ] Decisão sobre R1/R2 (§2).
- [ ] Confirmar que o bot de cripto pode ou não ser reiniciado (V4). Se puder
      ser afetado e estiver em operação com dinheiro real, **a janela tem de
      considerar isso** ou o push tem de esperar.

### Publicar

```bash
cd "<raiz do worktree>"
git log --oneline origin/main..release/agente-consultorio-e01-e03   # conferir os 6
git push origin release/agente-consultorio-e01-e03:main
```

Para R1 isolado, publicar só os três primeiros commits:

```bash
git push origin 783aa0fb:main        # landing
# depois, se quiser docs e testes no mesmo R1:
git push origin dc6dcf6c:main
```

### Verificar (nesta ordem, 10 min)

```bash
BASE=https://agente-atendimento-production.up.railway.app
# 1. a build nova subiu? instance_start deve ser POSTERIOR ao push
curl -s "$BASE/healthz" | python3 -c "
import sys,json,datetime
d=json.load(sys.stdin)
ini=int(datetime.datetime.fromisoformat(d['ts']).timestamp())-d['uptime_s']
print('db:',d['db'],'| tenants:',d['tenants_count'],
      '| subiu em:',datetime.datetime.fromtimestamp(ini))"

# 2. healthcheck
curl -s -o /dev/null -w "health %{http_code}\n" "$BASE/health"

# 3. filas vazias (precisa da MASTER_KEY)
curl -s "$BASE/admin/fila?key=$MASTER_KEY" | python3 -c "
import sys,json; print(json.load(sys.stdin)['resumo'])"
```

Critérios de sucesso:

- `db: ok` e `tenants_count: 8` — **se cair para 0, o volume não está montado:
  pare e restaure** (§6);
- `/health` 200;
- `resumo` com `entrada_presa: 0`, `saida_incerta: 0`, `pendencias_abertas: 0`;
- log de boot com `[inbox] worker iniciado` e `[outbox] worker iniciado`;
- a landing (`/`) sem os depoimentos fabricados.

### Observar (primeiras 48 h)

| Quando | O que | Onde |
|---|---|---|
| +15 min | filas vazias; nenhuma pendência | `/admin/fila` |
| +1 h | primeira conversa real completou ciclo | `/admin/fila` + painel do consultório |
| +4 h | nenhum `uncertain`; nenhum `failed` | `/admin/fila` |
| +24 h | confirmações de 24h saíram | painel, aba Agenda |
| +48 h | paciente não precisou repetir horário | aba Conversas |

Sinais de alarme que pedem rollback: `tenants_count` diferente de 8; `entrada_presa`
crescendo; paciente relatando que o agente não responde; `pendencias_abertas`
subindo sem explicação.

## 6. Backup, migração e restauração

### Migração

Seis tabelas novas — `inbound_events`, `outbox`, `offers`, `counters`,
`applied_effects`, `pending_effects` — mais duas colunas em `slot_offers`. Tudo
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … DEFAULT`, aplicado no boot por
`init_db()`. **Nada é removido e nenhuma coluna existente muda de tipo.**
`slot_offers` fica obsoleta e intocada — **não apagar** supondo que está vazia;
a verificação é um `SELECT COUNT(*)` no banco de produção.

Testada em cópia fictícia com o esquema de E02
(`tests/test_fluxo_ponta_a_ponta.py::test_migracao_desde_o_schema_de_e02`):
as tabelas novas aparecem, as linhas antigas permanecem.

### Backup antes do deploy (passo do proprietário)

Pela Railway: criar snapshot do volume, **ou** baixar o arquivo
`/data/consultorio.db` pelo shell do serviço. Guardar fora da Railway.

O backup automático às 3h vai para S3/R2/B2 — **a restauração nunca foi
testada** (achado de E01, item aberto de E06). Por isso o backup manual antes
deste deploy não é opcional.

### Teste de restauração, isolado (recomendado antes do deploy)

```bash
mkdir -p /tmp/restauracao && cp <backup>/consultorio.db /tmp/restauracao/
DATA_DIR=/tmp/restauracao /tmp/homolog-agente/venv/bin/python -c \
  "import sys; sys.path.insert(0,'whatsapp-agent/backend'); \
   import database as d; d.init_db(); \
   print('migrou:', d.get_conn and len(d.list_tenants()), 'consultórios')"
```

Isso aplica a migração do release sobre uma **cópia** do banco real e mostra se
ela passa. Não toca o volume de produção.

## 7. Rollback — voltar o código não é reversão suficiente

```bash
git revert --no-commit 196de1f1 7ea954196 52fcf73e7
# novo carimbo (a Railway precisa de mudança para reconstruir)
sed -i '' '2s/.*/# build: 2026-10-14-69/' whatsapp-agent/Dockerfile
git add -A whatsapp-agent/ && git commit -m "revert: voltar E02+E03 (build 69)"
git push origin HEAD:main
```

### O que o revert NÃO resolve — tratar antes

| Resíduo | O que acontece ao voltar | O que fazer antes |
|---|---|---|
| `inbound_events` em `pending`/`processing` | O código antigo **não conhece a tabela**: essas mensagens de paciente ficam sem resposta, para sempre | **Drenar a fila antes do revert**: esperar `/admin/fila` com `entrada` vazia. Se não der, exportar a lista (id, telefone, idade) e responder manualmente |
| `outbox` em `pending`/`sending` | Respostas prontas que ninguém envia | Esperar a fila de saída vazia. `uncertain` exige decisão humana por item |
| `outbox` em `uncertain` | Fica para sempre; o paciente pode ou não ter recebido | Resolver cada um com motivo antes do revert |
| `offers` em `active` | O antigo lê `slot_offers`, que estará vazia ⇒ todo "quero o 1" passa a pedir esclarecimento até o paciente repetir o horário | Degradação aceitável. Avisar o consultório |
| `pending_effects` abertas | Perdem visibilidade (sem `/admin/fila` no código antigo) | Exportar antes: `GET /admin/fila` e guardar o JSON |
| `applied_effects` | Inofensiva se ignorada | Nada |
| Consultas criadas/remarcadas no período | **Permanecem** — são linhas normais de `appointments` | Nada. Nenhum dado de paciente se perde no revert |

**Ordem do rollback:** drenar filas → exportar pendências → revert → novo
carimbo → push → conferir `/healthz` e `tenants_count` → avisar o consultório
de que a resposta numérica vai pedir confirmação por alguns dias.

Se o problema for **perda do banco** (volume ausente), o rollback é
restauração, não revert: parar o serviço, restaurar o arquivo no volume, subir.

## 8. Configuração dos workers

Nenhuma variável precisa ser criada: todas têm padrão seguro. Criar só para
mudar comportamento.

| Variável | Padrão | Quando mexer |
|---|---|---|
| `INBOX_WORKER_ENABLED` | `1` | desligar só para investigar |
| `OUTBOX_WORKER_ENABLED` | `1` | ⚠️ **o boot recusa inbox ligado com outbox desligado** |
| `INBOX_WORKER_INTERVAL_SEC` | 10 | laço de recuperação |
| `INBOX_LEASE_SEC` | 180 | subir se aparecer `posse perdida` no log |
| `INBOX_MAX_ATTEMPTS` | 5 | — |
| `OUTBOX_BATCH` | 10 | vazão por ciclo |
| `OFFER_TTL_MIN` | 180 | validade da oferta de horários |
| `INBOX_NUDGE_ON_WEBHOOK` | `1` | desligar aumenta a latência da 1ª resposta |

## 9. Bloqueadores de publicação

### 🔴 Impedem aprovação

| # | Bloqueador | Por quê | Resolve como |
|---|---|---|---|
| B-R1 | **Volume em `/data` não confirmado** (V3) | Sem volume, o deploy zera o banco de 8 consultórios | Conferir no painel + backup manual |
| B-R2 | **Serviços do bot de cripto podem escutar o mesmo repo** (V4) | Push a `main` pode reiniciar um bot que opera com dinheiro real | Conferir `watchPatterns`/branch dos serviços de cripto |
| B-R3 | **Backup pré-deploy não feito** | A restauração automática nunca foi testada | Snapshot manual do volume |
| B-R4 | **Config efetiva da Railway desconhecida** (V1, V2, V5) | Sem isso não se sabe o que o push realmente dispara | Roteiro do §3 |

### 🟠 Riscos conhecidos, não impeditivos — com decisão registrada

| Risco | Situação | Decisão proposta |
|---|---|---|
| **Agenda concorrente (dupla reserva)** | `has_conflict` e a gravação ainda **não são atômicos**. O release não piora isso: serializa por contato, mas dois pacientes diferentes em paralelo continuam podendo pegar o mesmo horário | Publicar com o risco **declarado**; é trabalho de **E04**. Com 1 consultório e volume baixo, a probabilidade é pequena mas real |
| **Autenticação** | Token do painel na URL e sem expiração; webhook de tenant legado aceito sem token; webhook do Twilio **sem** verificação de assinatura | Nenhum piora com o release. **E06**. Nenhum cliente usa Twilio |
| **Proteção dos dados** | Conversas clínicas em texto puro; `FIELD_ENCRYPTION_KEY` não confirmada (V6) | Nenhum piora com o release. Confirmar V6 agora; cifrar conversas é **E06** |
| **Integrações não verificadas** | WhatsApp real, modelo real, Stripe/MP, SMTP, Google/CalDAV, restauração | Primeiras 48h de observação próxima. Não contar como verificado |
| **Latência da primeira resposta** | O processamento saiu do request para o worker | O empurrão no webhook mantém a latência; observar item 8 do §5 |
| **Atrito da resposta numérica** | Se o modelo não declarar `offered_ids`, o "1" pede esclarecimento | Observar; corrigir no prompt, **não** voltando a resolver por posição |

Nenhum destes é considerado resolvido por constar neste documento.

## 10. Gates do release incremental

Aplicável a este e aos próximos releases. **Preserva o escopo e não altera as
datas da baseline v1.0.**

| Gate | Nome | Critério objetivo | Quem |
|---|---|---|---|
| G1 | Implementação local | Suíte e checks obrigatórios verdes; diff revisável; snapshot verificável | Claude |
| G2 | Homologação | Roteiro ponta a ponta executado com provedor simulado **e** o que depende de conta externa marcado NÃO VERIFICADO | Claude |
| G3 | Aprovação | Bloqueadores 🔴 resolvidos; riscos 🟠 com decisão registrada; janela e backup definidos | Proprietário |
| G4 | Publicação | Push do commit exato aprovado; carimbo novo; confirmação por `/healthz` | Proprietário autoriza, Claude acompanha |
| G5 | Observação | 48 h de acompanhamento; filas vazias; nenhum `uncertain`/`failed`; cliente sem queixa | ambos |

Regras: G3 **não** é automático a partir de G2 — homologação local não é
autorização. Um release só vira "publicado" depois de G4 confirmado por
`/healthz`. Enquanto G5 não fecha, a etapa seguinte não começa a publicar.

**Impacto nas datas:** nenhuma data da baseline muda. E01–E03 seguem como
semanas 1 e 2. R1 (piloto de atendimento, meta 08/11) continua dependendo do
bloqueio **B3** (contas de homologação), e **este release não substitui** aquela
validação: o que ele faz é levar as correções ao cliente atual.

## 11. Decisões e acessos que dependem do proprietário

1. **Conferir V1–V9** no painel da Railway e no GitHub (§3). É o que destrava a
   aprovação.
2. **Escolher R1+R2 ou release único** (§2).
3. **Fazer o backup do volume** e, de preferência, o teste de restauração (§6).
4. **Autorizar a janela** e confirmar se o bot de cripto pode ser reiniciado.
5. **Decidir sobre a proteção de branch** (`PROTECAO_PUBLICACAO.md`): com ela, o
   deploy passa a sair de um PR com CI verde; sem ela, `git push` direto segue
   funcionando. É troca de atrito por risco.
6. **Avisar o consultório** de que, se houver rollback, a resposta numérica vai
   pedir confirmação por alguns dias.
