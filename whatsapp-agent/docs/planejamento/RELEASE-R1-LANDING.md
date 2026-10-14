# R1 — retirada das alegações sem evidência da landing

Preparado em 14/10/2026. **Aguardando autorização.** Sem push, sem deploy, sem
alteração de configuração externa, sem backup de dados reais, sem chamada paga.

R2 (E02–E03) fica separado — checklist no §9.

---

## 1. Versão exata do R1

| Item | Valor |
|---|---|
| Worktree isolado | `…/scratchpad/r1` (o worktree e o branch do R2 ficaram intactos) |
| Branch | `release/r1-landing` |
| Base | `80d7ece8` = **`origin/main` atual** |
| Commit | **`885477d254b9c034b9b2a98395778d7d2f77acff`** |
| Push seria | fast-forward, 1 commit, sem merge |

```
 whatsapp-agent/backend/landing_content.py     |  5 ++-
 whatsapp-agent/backend/templates/landing.html | 64 +++++++++++----------------
 2 files changed, 29 insertions(+), 40 deletions(-)
```

Conferido: o conteúdo é **byte-idêntico** ao do commit `783aa0fb` original
(`git diff 783aa0fb8 HEAD -- whatsapp-agent/` vazio) e o diff **não** contém
documentação, workflow, `watchPatterns`, `Dockerfile`, testes nem qualquer
arquivo do bot de cripto.

### `origin/main` mudou — a base foi corrigida

Quando o release maior foi preparado, a base era `1b723a8f`. O remoto avançou
**17 commits** entre 30/09 e 08/10 (lote01–lote04, manual-bot, research V3),
todos do bot de cripto e do frontend; **nenhum toca `whatsapp-agent/`**. O R1
foi montado sobre o `origin/main` de hoje, não sobre a base antiga.

### Carimbo de build: **não incluído**, de propósito

`/healthz` não expõe o carimbo, então ele não serve para verificar o deploy. A
verificação do R1 é **funcional e mais forte**: buscar a própria landing e
confirmar que as alegações saíram (§6). Além disso, mexer na linha 2 do
`Dockerfile` invalida o cache a partir dali e força um `pip install` completo —
mais tempo de build e mais superfície, sem ganho. Se você preferir o carimbo
mesmo assim, é um `sed` de uma linha e um segundo commit; diga e eu preparo.

## 2. Gatilhos: o bot de cripto **não** seria reiniciado

Esta era a dúvida que bloqueava. Respondida com **registro direto**, não com
inferência sobre configuração: a API de Deployments do GitHub guarda quais
serviços a integração da Railway acionou em cada commit.

| Commit | O que tocava | Serviços que a Railway acionou |
|---|---|---|
| `0c19ed24` (build 65) | só `whatsapp-agent/` | **só** Agente de Atendimento |
| `181dc44b` (build 66) | só `whatsapp-agent/` | **só** Agente de Atendimento |
| `1b723a8f` (build 67) | só `whatsapp-agent/` | **só** Agente de Atendimento |
| `c08cdcfb` | só `whatsapp-agent/` | **só** Agente de Atendimento |
| `8565903e`, `d44be9a9`, `581ad3d4`, `6fc9bf1a`, `80d7ece8` e outros | só cripto/frontend | **só** Crypto Win PRD + DEV |
| `7d6dbe15` (merge) | ambos os lados | **os três** |

O filtro por caminho funciona **nos dois sentidos**. O R1 toca apenas
`whatsapp-agent/backend/landing_content.py` e
`whatsapp-agent/backend/templates/landing.html`, e seria um **fast-forward**
(sem merge commit, que é o único caso em que os três subiram juntos).

> **Conclusão:** pelo comportamento registrado, o R1 aciona **somente** o serviço
> `Agente de Atendimento / production`. Crypto Win PRD e DEV não são tocados.

Ressalvas honestas:

- isto é o registro do que a Railway **fez** em 10+ commits, não a leitura do
  valor de `watchPatterns`. Se a configuração mudou depois de 08/10 (último dado),
  o padrão pode não valer;
- a confirmação definitiva é a do §3, item V-A.

Outros fatos verificados no GitHub (somente leitura):

- branch `main` **não é protegida** e **não há rulesets** → `git push` direto funciona;
- **0 workflows** no repositório (o CI está no R2, não aqui);
- nenhum webhook; a integração é por GitHub App (`railway-app[bot]`, `vercel[bot]`);
- `vercel[bot]` publica o frontend em **todo** commit — inclusive no R1. É o
  frontend do bot de cripto. Rebuild do frontend na Vercel **não** reinicia o bot,
  mas vale saber que acontece.

## 3. Railway: o que continua bloqueado

CLI instalado (4.45.0) mas **token expirado** (`Unauthorized`); nenhuma variável
`RAILWAY_*` no ambiente. Sem acesso de leitura ao painel.

Projeto do Agente (link vindo do registro de deployment):
`https://railway.com/project/f938655b-6654-480c-97e3-28c34dcdae05`

| # | Conferir | Caminho no painel | Evidência a registrar |
|---|---|---|---|
| **V-A** | Filtro de caminho do serviço do Agente | projeto acima → serviço → **Settings → Build** | print ou texto de `Watch Paths` / `Root Directory` |
| **V-B** | 🔴 **Volume montado** | serviço → **Volumes** (ou Settings → Volumes) | existe volume? `Mount path` é exatamente `/data`? |
| **V-C** | Branch e auto-deploy | serviço → **Settings → Source** | repo, branch (`main`?), "Auto Deploy" ligado? |
| **V-D** | Config-as-code | serviço → **Settings → Config-as-code** | caminho do arquivo; vazio = `railway.toml` **não** está em uso |
| **V-E** | Gatilho dos serviços de cripto | projetos **Crypto Win PRD** e **DEV** → serviço → Settings → Build | `Watch Paths`/`Root Directory` de cada um |
| **V-F** | Variáveis (só existência) | serviço → **Variables** | existem `FIELD_ENCRYPTION_KEY`, `MASTER_KEY`, `BASE_URL`? **Não copie valores** |

**V-B é o único bloqueador 🔴 que resta para o R1.** O `Dockerfile` cria `/data`
dentro da imagem; se não houver volume montado ali, **qualquer** deploy — inclusive
o R1, que nem toca o banco — recria o container e perde o banco dos 8 consultórios
e os PDFs de contrato.

Há evidência indireta forte de que o volume existe (três deploys seguidos em
01/10 e o `tenants_count` continua 8), mas isso **não é confirmação** e o custo de
errar é o banco inteiro. Dois minutos no painel resolvem.

V-A, V-C, V-D, V-E e V-F não bloqueiam o R1 — são o que destrava o R2 com
tranquilidade.

## 4. Backup — procedimento revisado e testado

### Correções sobre a versão anterior deste roteiro

| O que eu havia escrito | Correção |
|---|---|
| "`cp` não serve, pode sair rasgado" | **Impreciso.** Testei com escritor concorrente (60.400 linhas inseridas durante a cópia): o `cp` também saiu com `integrity_check: ok`. O correto é: `cp` **não tem garantia**; a API de backup online **tem**. Uma cópia por `cp` dar certo uma vez não prova nada |
| "baixe os dois arquivos e guarde fora da Railway" | **Removido.** Baixar o banco é levar conversa clínica, nome e CPF para o computador local. A cópia fica em **armazenamento privado persistente** |
| procedimento próprio em shell | **Trocado pelo mecanismo do próprio produto**, que já faz isso certo — abaixo |

### O produto já tem backup consistente — e cobre menos do que parece

`backup_service.py` usa `src.backup(dst)`, a **API de backup online do SQLite**:
consistente mesmo com escritor concorrente. Roda às 3h e sobe para o bucket
privado em `consultorio/<data>/consultorio-<timestamp>.db.gz`. Há o disparo
manual `POST /master/backup/run` (exige `MASTER_KEY`, idempotente por dia).

🔴 **Mas ele copia só `consultorio.db`.** Verificado no código:
`/data/contracts/` — os **PDFs de contrato assinados**, com nome, CPF, IP e
hash — **não têm backup off-site nenhum**, nem automático nem manual. Se o
volume for perdido, os contratos assinados vão com ele.

Achado desta verificação, não item de release. Fica como pendência de **E06**
(dados e restauração) e como passo manual até lá.

### Procedimento recomendado (executado por você)

**Passo 1 — banco, pelo mecanismo do produto.** Nenhum comando novo, nenhuma
cópia local:

```
POST https://agente-atendimento-production.up.railway.app/master/backup/run?key=<MASTER_KEY>
```

Esperado: `{"status": "uploaded", ...}` com a chave no bucket. `503` significa
`BACKUP_S3_*` ausente — aí **pare**: não há backup off-site, e o deploy não
deveria acontecer antes de resolver isso.

**Passo 2 — contratos, que o passo 1 não cobre.** No shell do serviço:

```bash
df -h /data
du -sh /data/contracts 2>/dev/null || echo "sem contratos ainda"
tar -czf /data/contracts-$(date +%Y%m%d-%H%M).tar.gz -C /data contracts
ls -la /data/contracts-*.tar.gz
```

Suba esse `.tar.gz` para o **mesmo bucket privado** pelo painel do provedor de
armazenamento e **apague a cópia de `/data`**:

```bash
rm -f /data/contracts-*.tar.gz
```

**Folga de espaço:** escrever no volume consome espaço. No teste, um backup
feito durante escrita intensa ficou bem maior que o banco em repouso. Exija
**livre ≥ 3× o tamanho de `/data`** antes do passo 2. Se `df` mostrar menos, não
escreva no volume: suba os contratos direto, sem o `.tar.gz` intermediário.

**Passo 3 — conferir, sem expor conteúdo.** Imprime **apenas contagens e
`integrity_check`** — nenhum nome, nenhum texto de conversa:

```bash
python - <<'FIM'
import sqlite3
c = sqlite3.connect("file:/data/consultorio.db?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("tenants","appointments","conversations","patients","contracts"):
    try: print(f"{t}: {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    except Exception as e: print(f"{t}: {e}")
FIM
```

### Regras de acesso

- A cópia do banco vive no **bucket privado já em uso** — mesma fronteira de
  segurança dos backups diários, nenhuma superfície nova.
- **Nada é baixado para computador local** e nada vai para serviço externo novo.
- Nenhuma cópia entra no repositório.
- Nenhum relatório desta etapa contém conteúdo clínico.

### Executado × não executado

| | Situação |
|---|---|
| Revisão dos comandos | ✅ feita |
| Teste do procedimento com banco **fictício**, sob escrita concorrente | ✅ `integrity_check: ok`; transações concorrentes íntegras (múltiplo exato do lote) |
| Verificação de que a saída não expõe dado clínico | ✅ só contagens |
| Medida do custo de acumulação de eventos | ✅ **158 bytes por evento** |
| **Backup do banco real** | ❌ **NÃO EXECUTADO** — exige `MASTER_KEY` ou shell do serviço (§A) |
| **Backup dos contratos reais** | ❌ **NÃO EXECUTADO** — mesmo motivo |

## 5. Restauração — procedimento validado, dado real **pendente**

### Validado hoje, com dado fictício

Reproduzi o cenário **real** de migração: gerei um banco com o código **exato da
build 67** (a que está em produção, extraída do git em `1b723a8f`), com
consultório, consultas e mensagens fictícias; depois apliquei o código atual.

```
build 67: 27 tabelas · slot_offers ausente · inbound_events ausente
          ↓ init_db() do código atual
migrou sem erro
  inbound_events, outbox, offers, counters, applied_effects,
  pending_effects, slot_offers: todas criadas
  dados preservados: 5 consultas, 5 mensagens · integrity=ok
```

Também validei backup → restauração em destino isolado: cópia por `.backup()`,
`integrity_check: ok`, contagens conferidas, **banco de origem intacto**.

### Fragilidade encontrada no caminho

`init_db()` falhou com `no such column: next_attempt_at` quando havia uma tabela
`inbound_events` **pré-existente com schema diferente**. Em produção isso não
acontece (a tabela não existe). Mas revela que as tabelas de E03 **não têm
migração por `ALTER TABLE`**: se uma versão futura acrescentar coluna,
`CREATE TABLE IF NOT EXISTS` não a adiciona e um índice que a referencie quebra
o boot. As tabelas de E02 já têm esse tratamento; as de E03 não.

Registrado como item do R2 (§9). Não afeta o R1, que não toca schema.

### Pendente

| | Situação |
|---|---|
| Procedimento de restauração | ✅ validado com dado fictício |
| Migração build 67 → código atual | ✅ validada com dado fictício |
| **Restauração do backup real** | ❌ **PENDENTE** — exige o backup do §4 |
| **Migração sobre cópia do banco real** | ❌ **PENDENTE** — mesmo motivo |

> Backup existente **não é** restauração validada. Para o R1 não é bloqueador
> (não toca banco nem schema); para o R2 é.

## A. Ações externas que dependem de você

Nenhuma eu consigo executar: o CLI da Railway está com token expirado
(`invalid_grant` / `Unauthorized`), e eu **não vou** ler o `.env` local nem usar
qualquer segredo para falar com produção.

### A.1 — Conferir no painel (somente leitura, ~5 min)

IDs do serviço, vindos da configuração local do CLI:

| Campo | Valor |
|---|---|
| projeto | `f938655b-6654-480c-97e3-28c34dcdae05` |
| ambiente | `cc3af85c-c782-4dbe-89d8-5642fe2434c2` (production) |
| serviço | `f196fa3e-994c-49c7-b2ba-02947e5b08b4` |
| nome | `agente-atendimento` |

Link direto:
`https://railway.com/project/f938655b-6654-480c-97e3-28c34dcdae05/service/f196fa3e-994c-49c7-b2ba-02947e5b08b4?environmentId=cc3af85c-c782-4dbe-89d8-5642fe2434c2`

| # | Onde clicar | O que me dizer |
|---|---|---|
| **A.1.1** 🔴 | serviço → **Volumes** | existe volume? **Mount path** (precisa ser `/data`)? tamanho total e usado? |
| **A.1.2** | serviço → **Settings → Source** | branch (é `main`?) e **Auto Deploy** ligado? |
| **A.1.3** | serviço → **Settings → Build** | **Watch Paths** e **Root Directory** (texto exato) |
| **A.1.4** | serviço → **Settings**, procurar *Config as code* | tem caminho de arquivo? qual? |
| **A.1.5** | serviço → **Variables** | **só sim/não**: existem `MASTER_KEY`, `FIELD_ENCRYPTION_KEY`, `BACKUP_S3_BUCKET`, `BASE_URL`? **Não me envie valores** |
| **A.1.6** | projetos **Crypto Win PRD** e **DEV** → serviço → Settings → Build | **Watch Paths** e **Root Directory** de cada um |

### A.2 — Reautenticar o CLI (opcional, resolve A.1 de uma vez)

```bash
railway login
```

Abre o navegador e renova o token; depois eu leio tudo de A.1 em modo somente
leitura. É ação sua porque exige o navegador e a sua sessão.

### A.3 — Backup (depois de A.1.1 confirmar o volume)

1. `POST /master/backup/run?key=<MASTER_KEY>` — você executa, com a sua chave
2. passos 2 e 3 do §4, no shell do serviço
3. me diga só o resultado: `status` do backup e as contagens

### A.4 — Autorizar o push

Comando exato no §6, depois de A.1.1 e A.3.

## 6. Checks do R1, deploy, verificação e retorno

### Checks executados (no worktree isolado, hoje)

**18/18 passaram.** Script: `scratchpad/verifica_r1.py`.

| Check | Resultado |
|---|---|
| `GET /` renderiza `templates/landing.html` | OK — confirmado no código da rota |
| `/p/{segment}` usa `landing_content.content_for` | OK |
| A landing responde 200 | OK |
| "40% menos faltas" fora da página servida | OK |
| "Marina R.", "Camila F.", "Letícia M." fora | OK |
| "Mais de 30 psicólogas" fora | OK |
| "nunca perceberam que era automático" fora | OK |
| "reduzem faltas" fora | OK |
| Seção factual substituta no ar | OK |
| Descontos verificáveis (15%/25%) preservados | OK |
| Landing por segmento sem "2 min para configurar" | OK |
| Landing por segmento sem depoimento | OK |
| `landing.html` e `landing_vertical.html` renderizam | OK |
| Todos os módulos de `backend/` compilam | OK |

Nota: a suíte de 316 testes é do **R2** (o commit dela não está no R1). Os checks
acima são os pertinentes ao R1 e foram rodados contra os arquivos exatos do R1.

### Deploy (só depois de V-B e do backup)

```bash
cd <…>/scratchpad/r1
git log --oneline origin/main..HEAD          # deve mostrar 1 commit: 885477d2
git diff --stat origin/main..HEAD            # deve mostrar 2 arquivos
git push origin release/r1-landing:main
```

### Verificação (5 min)

```bash
BASE=https://agente-atendimento-production.up.railway.app

# 1. subiu build nova? instance_start deve ser POSTERIOR ao push
curl -s "$BASE/healthz" | python3 -c "
import sys,json,datetime
d=json.load(sys.stdin)
ini=datetime.datetime.fromisoformat(d['ts']).timestamp()-d['uptime_s']
print('subiu em:', datetime.datetime.fromtimestamp(ini).strftime('%Y-%m-%d %H:%M'),
      '| db:', d['db'], '| consultórios:', d['tenants_count'])"

# 2. a landing publicada está limpa  ← a verificação que importa
curl -s "$BASE/" | python3 -c "
import sys
h=sys.stdin.read()
proibidos=['40% menos faltas','Marina R.','Camila F.','Letícia M.',
           'Mais de 30 psicólogas','nunca perceberam que era automático']
restaram=[p for p in proibidos if p in h]
print('alegações restantes:', restaram or 'nenhuma')
print('seção factual presente:', 'O que o agente faz' in h)"

# 3. healthcheck
curl -s -o /dev/null -w "health %{http_code}\n" "$BASE/health"
```

**Critérios de sucesso:** `db: ok`; **`consultórios: 8`**; `alegações restantes:
nenhuma`; `seção factual presente: True`; `health 200`.

🔴 **Se `consultórios` vier diferente de 8, pare** — é o sintoma de volume
ausente. Não faça mais nenhum push: restaure o backup do §4 antes de qualquer
outra coisa.

### Retorno (rollback do R1)

O R1 é texto de template. O retorno é trivial e **não tem resíduo de dados**:
nenhuma tabela, nenhuma fila, nenhum estado.

```bash
cd <…>/scratchpad/r1
git revert --no-edit 885477d2
git push origin HEAD:main
```

Em ~2 min a landing volta ao conteúdo anterior — **inclusive os depoimentos
fabricados**. Ou seja: o rollback do R1 reintroduz a exposição jurídica. Se o
problema for outro (a página quebrar, por exemplo), preferir corrigir para frente.

### Observação pós-deploy

| Quando | O que | Onde |
|---|---|---|
| +5 min | landing limpa e 200 | comando 2 acima |
| +15 min | `db: ok`, 8 consultórios | `/healthz` |
| +1 h | nenhum erro novo | Sentry |
| +24 h | agendamento e confirmações normais | painel do consultório |

O R1 não muda nenhum caminho que atende paciente. Se algo quebrar no WhatsApp
nas 24h seguintes, muito provavelmente **não é** o R1.

## 7. Janela recomendada

Jobs que não devem coincidir com o deploy (de `scheduler.py`):

| Job | Quando |
|---|---|
| Confirmações e follow-up | laço de 30 min, **7h–22h** |
| Cobrança mensal | **último dia do mês, 20h** |
| Backup automático | **3h** |
| Lembretes de vencimento | **9h** (configurável) |
| Lembretes de contrato | 7h–22h |

**Recomendação: dia útil, 22h15–23h00.** Depois das 22h o agendador para de
enviar, então o restart não interrompe mensagem em voo; e não colide com o
backup das 3h nem com a cobrança das 20h. **Evitar 31/10** (último dia do mês).

O R1 é de risco mínimo, então 21h também serve — a janela mais tarde é só
conservadorismo. O que define de verdade é **sua disponibilidade para
acompanhar os 15 min de verificação**: não publique sem poder olhar o resultado,
porque o único cenário ruim (volume ausente) exige reação imediata.

## 8. Rollback do R2 — corrigido

Duas afirmações anteriores estavam erradas. As duas foram verificadas no código.

### Erro 1 — "desligar os workers para o processamento". **Não para.**

`INBOX_WORKER_ENABLED` só é lido em `inbox.start_worker()`. O empurrão do
webhook, `_drenar_entrada`, chama `inbox.drain()` e `outbox.flush()`
**diretamente**, sem consultar a variável:

```python
if bg is not None and getattr(config, "INBOX_NUDGE_ON_WEBHOOK", True):
    bg.add_task(_drenar_entrada)      # ← não olha INBOX_WORKER_ENABLED
```

Consequência: com os dois workers desligados, **cada mensagem que chega continua
sendo processada inteira** — chamada ao modelo, mutação na agenda, envio. O
procedimento anterior não interrompia nada.

**Procedimento correto: três variáveis, não duas.**

```
INBOX_WORKER_ENABLED=0
OUTBOX_WORKER_ENABLED=0
INBOX_NUDGE_ON_WEBHOOK=0      ← a que faltava
```

Os dois primeiros têm de andar juntos (o boot recusa inbox ligado com outbox
desligado). O terceiro é o que de fato interrompe.

**Item para o R2:** fazer `_drenar_entrada` respeitar `INBOX_WORKER_ENABLED`,
para que o procedimento de emergência não dependa de alguém lembrar da terceira
variável. Não implementado aqui — está fora do escopo desta preparação e entra
no checklist do §9.

### Erro 2 — "nada se perde porque as tabelas permanecem"

**Recebimento continua ativo mesmo com tudo desligado, e isso é intencional:** o
webhook grava o evento antes do 2xx, então o provedor nunca recebe erro e nenhuma
mensagem é recusada. Mas significa que `inbound_events` **acumula sem limite**
enquanto o processamento estiver parado.

Medido com banco fictício: **158 bytes por evento**.

| Volume | Acúmulo |
|---|---|
| 200 mensagens/dia | 31 KB/dia · **0,9 MB/mês** |
| 1.000 mensagens/dia | 154 KB/dia · **4,5 MB/mês** |

**Armazenamento não é o gargalho.** Com um consultório e o volume atual, parar o
processamento por dias não enche nada. O que cresce é a **dívida de atendimento**:
cada linha é um paciente que escreveu e não foi respondido.

🔴 **Tamanho do volume: NÃO VERIFICADO** (item A.1.1). Até confirmar, o limite
de armazenamento é desconhecido — mas a ordem de grandeza acima mostra que o
risco de encher o disco por acúmulo de eventos é remoto.

### Monitoramento enquanto estiver parado

```bash
# quantos pacientes estão esperando (sem conteúdo de conversa)
curl -s "$BASE/admin/fila?key=$MASTER_KEY" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('mensagens sem resposta:', len(d['entrada']))
print('respostas que não saíram:', len(d['saida']))
print('mais antiga, em minutos:', max([e['idade_min'] or 0 for e in d['entrada']], default=0))"
```

Limiares sugeridos: acima de **20 mensagens** ou **120 minutos** na mais antiga,
pare de esperar e responda à mão — ou reaplique o R2. Não existe mecanismo para
"recusar" mensagem sem perdê-la; o que existe é pausar o agente por contato
(`agent_paused`), que silencia o agente para aquele paciente sem apagar nada.

### Procedimento, na ordem

**1. Interromper o processamento** — as três variáveis acima. O recebimento
segue gravando; nada é recusado ao provedor.

**2. Tentar drenar, com prazo.** Se o provedor responder, uma passada do outbox
resolve. Se não responder em ~10 min, **não insista** — era isso que o
procedimento anterior assumia como sempre possível, e durante queda do provedor
não é.

**3. Preservar.** A mensagem do paciente (`inbound_events.body`) é conteúdo
clínico: exportá-la para CSV ou relatório seria tirar dado sensível do banco.
Então a preservação é o **backup do §4** — ele já contém `inbound_events`,
`outbox`, `offers` e `pending_effects` inteiros, com o conteúdo, sob as mesmas
regras do banco. Para **agir**, o operador precisa só da lista **sem conteúdo**,
que `GET /admin/fila` já devolve, salva fora do repositório.

**4. Revert e carimbo novo.**

**5. Recuperar.** Duas saídas, e elas não são equivalentes:

- **reaplicar o R2** (revert do revert) e religar as três variáveis: as filas
  retomam de onde pararam, porque o estado está no banco e a idempotência por
  evento impede ação em dobro. **É o único caminho que processa a fila.**
- **ficar no código antigo**: a fila **não é processada**. Ver abaixo.

### O que se perde ao ficar no código antigo — sem rodeio

**O código antigo não conhece `inbound_events`, `outbox`, `offers` nem
`pending_effects`.** Ele não lê, não processa e não esvazia essas tabelas. As
linhas continuam lá, intactas e inúteis até o R2 voltar.

| Resíduo | Consequência real no código antigo |
|---|---|
| `inbound_events` `pending` | **A resposta automática a essas mensagens não acontece.** O paciente escreveu e o agente não vai responder. Só contato manual resolve |
| `outbox` `pending` | A resposta já redigida **não é enviada** |
| `outbox` `uncertain` | Continua sem saber se chegou — decisão humana, item por item |
| `offers` `active` | Degradação: todo "quero o 1" passa a pedir que o paciente repita o horário |
| `pending_effects` | Perdem a tela (`/admin/fila` não existe no código antigo) — daí a lista do passo 3 |
| Consultas criadas/remarcadas | **Permanecem.** Nenhuma consulta é perdida |

Em uma frase: **o dado persiste; o atendimento automático daquelas mensagens
não.** E o número de mensagens afetadas depende de quanto tempo levar entre o
problema e o passo 1 — por isso o passo 1 vem antes de tudo.

## 9. Checklist para liberar o R2

### Bloqueadores

- [ ] **V-B — volume em `/data` confirmado** (§3). Mesmo bloqueador do R1
- [ ] **V-A, V-C, V-D, V-E** — filtro de caminho, branch/auto-deploy,
      config-as-code e gatilho dos serviços de cripto (§3)
- [ ] **V-F** — `FIELD_ENCRYPTION_KEY` existe? Sem ela, credenciais e PIX estão
      em texto puro hoje
- [ ] **Backup do §4 feito e baixado**
- [ ] **Restauração validada** (§5) — inclui rodar a migração do R2 sobre a
      cópia do banco real
- [ ] **Rebase do R2 sobre o `origin/main` atual** (hoje ele está sobre
      `1b723a8f`, 17 commits atrás) e suíte verde depois do rebase
- [ ] **`_drenar_entrada` respeitar `INBOX_WORKER_ENABLED`** — hoje o empurrão do
      webhook processa mesmo com os workers desligados, o que torna o
      procedimento de emergência dependente de uma terceira variável (§8)
- [ ] **Migração por `ALTER TABLE` para as tabelas de E03** — hoje elas só têm
      `CREATE TABLE IF NOT EXISTS`, sem caminho para acrescentar coluna depois (§5)
- [ ] **Backup de `/data/contracts/`** — os PDFs assinados não têm backup
      off-site nenhum (§4). Pendência de **E06**, mas vale decidir antes do R2
- [ ] **Decisão sobre proteção de branch**: `main` não é protegida e não há
      ruleset. Com proteção, o deploy passa a sair de PR com CI verde

### Teste mínimo com integração real — o que o R2 ainda não tem

A evidência atual do R2 são **26 verificações com provedor simulado e modelo
programado** e **316 testes com dublê**. Isso valida o backend, **não** a
integração: nada ali prova que o modelo real emite `offered_ids`, nem que a
Evolution entrega.

**Nenhuma dessas evidências deve ser apresentada como homologação de integração.**

Para fechar essa lacuna, o mínimo é:

| # | Cenário | Como |
|---|---|---|
| 1 | Paciente pede um dia e recebe horários daquele dia | número de homologação manda "quero agendar dia DD/MM" |
| 2 | "1" reserva a opção 1 exibida | responder `1` e conferir a agenda |
| 3 | O modelo real emite `offered_ids` | ver no log se aparece "oferta … draft" ou o aviso de ausência |
| 4 | Pedido de dia sem vaga recebe alternativas | encher o dia no painel antes |
| 5 | Remarcação por data/hora explícita | "dia DD/MM às 16h" |
| 6 | Reinício no meio da conversa | mandar mensagem e reiniciar o serviço em seguida |
| 7 | Deduplicação real | não forçável à mão — observar no log se aparecer |
| 8 | Latência da primeira resposta | cronometrar os cenários 1 e 2 |

**O que precisa ser provido (decisões suas):**

| Recurso | Detalhe | Efeito / risco |
|---|---|---|
| **Número de WhatsApp de homologação** | chip pré-pago só para teste | 🔴 a instância de teste subiria no **mesmo servidor Evolution** da produção. Se o WhatsApp banir o número de teste, o risco para o número de produção não é zero (mesmo servidor/IP). A alternativa segura é um segundo servidor Evolution, que tem custo |
| **Instância Evolution de teste** | `tenant-homologacao` no servidor existente | reversível: apagar a instância |
| **Consultório fictício em produção** | ou um segundo serviço só para homologação | 🔴 criar tenant de teste **no banco de produção** mistura dado de teste com real. Preferível um serviço separado apontando para um volume próprio |
| **Chave de IA com orçamento** | Groq (primário) e/ou Anthropic (fallback) | custo abaixo |

**Custo previsto do modelo, medido no código:** o prompt de psicologia tem
**20.358 caracteres ≈ 5.100 tokens** de entrada por mensagem, mais contexto e
histórico, com `max_tokens=512` de saída. Para os 8 cenários, estimando ~25
trocas:

- **entrada ≈ 140 mil tokens**
- **saída ≈ 6 mil tokens**

No Groq, nesse volume, a ordem de grandeza é de **centavos de dólar** — bem
abaixo de US$ 1. **Confirme o preço corrente na tabela do provedor antes de
autorizar**: eu não tenho como verificar a tarifa de hoje, e não vou afirmar um
número que não posso conferir. Com o fallback da Anthropic o custo por token é
maior, mas nesse volume continua pequeno.

**Autorização necessária, explicitamente:** usar chave de IA com crédito real e
um número de WhatsApp de homologação. Sem as duas, os oito cenários permanecem
**NÃO VERIFICADOS** e o R2 pode, no máximo, ser publicado com acompanhamento
próximo — não como "integração homologada".

### Gates

| Gate | R1 | R2 |
|---|---|---|
| G1 implementação local | ✅ 18/18 checks | ✅ 316 testes + 26 simulados |
| G2 homologação | ✅ a landing é verificável sem integração externa | ⏸ depende do teste mínimo acima |
| G3 aprovação | ⏸ só V-B + backup | ⏸ checklist inteiro |
| G4 publicação | — | — |
| G5 observação | — | — |
