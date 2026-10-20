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

### Agora confirmado também **pela configuração**, não só pelo histórico

A ressalva que estava aqui ("isto é o registro do que a Railway fez, não a
leitura do `watchPatterns`") **caiu**. Li a configuração dos quatro serviços:

| Projeto / serviço | `rootDirectory` | `watchPatterns` | R1 dispara? |
|---|---|---|---|
| Agente de Atendimento / Agente de Atendimento WhatsApp | `whatsapp-agent` | `[]` | **sim** — é o alvo |
| Crypto Win - PRD / Crypto Win | `backend` | `[]` | não |
| Crypto Win - PRD / Crypto-Agente Worker | `backend/` | `[]` | não |
| Crypto Win - DEV / Crypto Win - DEV | `backend/` | `[]` | não |

Todos no mesmo repo `wagalan7/Crypto-Agente`, branch `main`,
`railwayConfigFile = None`. Com `watchPatterns` vazio, quem filtra é o
`rootDirectory` — e os três serviços de cripto só olham `backend/` da **raiz** do
repositório, que o R1 não toca.

Configuração e histórico concordam. O **reinício do bot de cripto não está
autorizado e também não aconteceria**.

Outros fatos verificados no GitHub (somente leitura):

- branch `main` **não é protegida** e **não há rulesets** → `git push` direto funciona;
- **0 workflows** no repositório (o CI está no R2, não aqui);
- nenhum webhook; a integração é por GitHub App (`railway-app[bot]`, `vercel[bot]`);
- `vercel[bot]` publica o frontend em **todo** commit — inclusive no R1. É o
  frontend do bot de cripto. Rebuild do frontend na Vercel **não** reinicia o bot,
  mas vale saber que acontece.

## 3. Railway: configuração verificada — nada mais bloqueia o R1

Acesso: `railway login` não roda no shell não interativo deste agente, então foi
executado no terminal do proprietário. Lá, **o fluxo pelo navegador não chegou a
abrir**: o CLI encontrou `RAILWAY_API_TOKEN` já presente no ambiente daquele
terminal e entrou com ele (sessão de `wagalan@gmail.com`). O valor do token não
foi lido, pedido nem exibido; nenhum `.env` foi lido. As consultas rodaram
naquele terminal, porque o shell do agente não tem o token. Li a configuração
**somente leitura** pela API
GraphQL e pelo shell do contêiner. Nenhum valor de variável foi lido ou impresso;
nada foi alterado, nenhum serviço reiniciado.

### V-B 🟢 — volume confirmado (era o único bloqueador do R1)

| Campo | Valor |
|---|---|
| nome | `agente-atendimento-volume` |
| estado | **READY** |
| `mountPath` | **`/data`** — exatamente o que o `Dockerfile` espera |
| serviço | Agente de Atendimento WhatsApp · ambiente `production` |

O banco e os PDFs de contrato **são persistentes**. O risco de "deploy recria o
container e perde o banco dos 8 consultórios" **não existe**.

**Espaço — e uma divergência que vale registrar.** A API relata
`currentSizeMB = 1466,6` de `sizeMB = 5000`. O `df` dentro do contêiner relata
outra coisa, e é o `df` que manda:

```
/dev/zd12032   4,6G total   318M usado   4,2G livre   7%
```

Os 318 MB se explicam inteiros: `consultorio.db` = 40,4 MB e `/data/backups` =
276 MB (ver §4). **Folga real: 4,2 GB** — nenhuma restrição de espaço para o R1.

### Demais itens (não bloqueavam o R1; destravam o R2)

| # | Item | Valor verificado |
|---|---|---|
| **V-A** 🟢 | filtro de caminho do Agente | `rootDirectory = 'whatsapp-agent'`, `watchPatterns = []` |
| **V-C** 🟢 | branch | `wagalan7/Crypto-Agente`, branch `main` |
| **V-C2** 🟢 | Auto Deploy | gatilho de repositório configurado (`repoTriggers` → `main`). Não li um campo "auto deploy" isolado; o comportamento confirma: push do R1 às 18:33:06Z → deployment criado pelo `railway-app[bot]` às 18:33:10Z; push do R3 às 17:12:42Z → deployment às 17:12:46Z. **Ligado** |
| **V-D** 🟢 | config-as-code | `railwayConfigFile = None` → **`whatsapp-agent/railway.toml` NÃO está em uso** |
| **V-E** 🟢 | gatilho dos serviços de cripto | ver §2 — todos com `rootDirectory` em `backend` |
| **V-F** 🟢 | variáveis (só existência) | `MASTER_KEY`, `FIELD_ENCRYPTION_KEY`, `BASE_URL`, os quatro `BACKUP_S3_*`, `MP_WEBHOOK_SECRET`, `SMTP_HOST`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `DATA_DIR`, `ADMIN_PASSWORD` **existem**. 45 variáveis, 31 definidas no serviço |

`FIELD_ENCRYPTION_KEY` existe → as credenciais e o PIX **não** estão em texto
puro. Era uma das suspeitas da auditoria; cai.

Ausentes (vale o padrão do código): `INBOX_WORKER_ENABLED`,
`OUTBOX_WORKER_ENABLED`, `INBOX_NUDGE_ON_WEBHOOK`. Importa para o R2 — ver §8.

### Três correções de documentação que isso produz

1. **`railway.toml` não é aplicado.** O `CLAUDE.md` deste diretório avisa que
   `watchPatterns = ["whatsapp-agent/**"]` casaria `docs/**` e que o healthcheck
   aponta para `/health` (rota inexistente). Com `railwayConfigFile = None`,
   **nenhuma das duas coisas está em vigor**: quem filtra é o `rootDirectory`, e
   não há healthcheck configurado. A pendência B2 do `STATUS.md` deve ser
   reescrita: o arquivo é inerte, não perigoso.
2. **O `Dockerfile` é o que constrói a imagem**, apesar de a API dizer
   `builder = RAILPACK` (valor padrão quando nada foi escolhido). Verificado no
   contêiner: pacotes em `/usr/local/lib/python3.11/site-packages`, **sem venv**,
   `WORKDIR /app`, Python 3.11.16 (= `python:3.11-slim`), zero vestígio de
   Railpack em `/` ou nas variáveis. A convenção do carimbo na linha 2 do
   `Dockerfile` continua válida.
3. **O carimbo de build não aparece em lugar nenhum em tempo de execução.**
   `/healthz` devolve `status`, `db`, `tenants_count`, `db_ms`, `uptime_s`,
   `rss_mb`, `ts`, `sentry` — **nenhum campo de versão**. Confirmar deploy é
   olhar `uptime_s` **zerar**. O carimbo é documental, e é por isso que o R1 não
   precisa dele (§1).

Produção agora: `uptime_s = 1.105.941` ≈ **12,8 dias** → o processo está no ar
desde ~01/10, sem nenhum deploy desde então. `db: ok`, `tenants_count: 8`.

### `checkSuites = False` nos três serviços

A Railway **não espera CI** antes de publicar — nos três serviços. O workflow do
GitHub Actions que entra no R2 não bloqueia deploy nenhum; ele informa. Item do
R2 (§9), não do R1.

## 4. Backup — executado e conferido

### São **dois** backups, não um. Descoberta desta verificação

| Onde | Quem faz | O quê | Protege de |
|---|---|---|---|
| `/data/backups/consultorio-AAAAMMDD.db` | `scheduler._run_backup()`, 3h BRT | cópia **não** comprimida, rotação de 7 dias | apagão/erro lógico — **não** protege de perda do volume (mora no mesmo volume) |
| bucket privado `consultorio/<data>/…db.gz` | `backup_service.run_backup_if_due()` | cópia comprimida, retenção 30 dias | perda do volume |

Os dois usam `src.backup(dst)`, a **API de backup online do SQLite** —
consistente mesmo com escritor concorrente. Verificado no contêiner:

```
/data/backups: 7 arquivos, 08/10 a 14/10, ~40 MB cada = 276 MB
bucket, prefixo consultorio/ : 30 objetos, ~3,9 MB cada
```

A retenção dos dois lados está funcionando. **Não há acúmulo descontrolado.**

### O backup diário de hoje já tinha subido — e isso tem consequência

`/data/.backup_state` continha `2026-10-14`, e `_mark_backup_done()` só grava
**depois** do `upload_file` bem-sucedido. Então o estado prova o upload: o objeto
`consultorio/2026-10-14/consultorio-20261014-060442.db.gz` (3,88 MB) existe.

Consequência prática: **`POST /master/backup/run` não serviria como backup
pré-deploy.** `run_backup_if_due()` começa com

```python
if _last_backup_date() == today:
    return {"status": "skipped", "reason": "already_done_today"}
```

e **não tem parâmetro de força**. Chamar o endpoint hoje devolveria `skipped` e
nenhuma cópia nova. Por isso o §4 anterior, que mandava usar esse endpoint como
proteção antes do deploy, estava errado. O caminho usado foi outro.

### Autenticação do endpoint — verificada antes de qualquer uso

```python
def _check_master_key(key: str):
    if not config.MASTER_KEY or not hmac.compare_digest(key or "", config.MASTER_KEY):
        raise HTTPException(status_code=403, detail="Acesso negado.")
```

Primeira instrução de `POST /master/backup/run`, compara em tempo constante e
**nega quando `MASTER_KEY` está vazia** (fail-closed). Não é endpoint aberto.
Nenhuma `MASTER_KEY` foi lida, inventada ou usada.

### O que foi executado — backup pré-R1

Rodado **dentro do contêiner de produção** (`railway ssh`), reaproveitando a
configuração que o próprio produto já tem, sem ler nem imprimir segredo:

| Regra | Como foi cumprida |
|---|---|
| não substituir o banco ativo | `/data/consultorio.db` só foi aberto com `mode=ro`; a cópia saiu por `src.backup(dst)` |
| não escrever no volume | tudo em `tempfile.TemporaryDirectory()` no disco efêmero (582 GB livres), apagado no fim |
| não iniciar app/workers | só `sqlite3` e `boto3`; nenhum módulo de aplicação executado |
| não baixar nada para o Mac | o arquivo nunca saiu do contêiner; só texto de resultado voltou |
| armazenamento não público | o bucket privado já em uso pelos backups diários |
| nada apagado | `_cleanup_old_backups()` **não** foi chamado |
| estado diário intacto | `.backup_state` não foi tocado → o job das 3h de amanhã roda normal |
| sem conteúdo clínico | saída só com chaves, tamanhos, sha256, contagens e `integrity_check` |

**Resultado:**

```
banco      40,38 MB -> 3,90 MB comprimido
           sha256 d0979de3f1f8696b0de85b863ce99bdb524c297a7bbce13d39aa2edca07f49e3
           -> consultorio/pre-release-R1/consultorio-20261014-181432.db.gz

contratos  25 arquivos · 0,25 MB -> tar.gz 0,07 MB
           sha256 0e807a238e1951098de13f51ecbfbe866e391fa5dc76d00d6d222650282c4afc
           -> contracts/pre-release-R1/contracts-20261014-181432.tar.gz
```

O prefixo do banco fica **sob `consultorio/`** de propósito: herda a retenção de
30 dias e não cria lixo permanente.

### 🔴 Os contratos não tinham backup nenhum — agora têm um, manual

O inventário do bucket mostrou **`contracts/` → 0 objetos**. Antes de hoje, os 25
PDFs assinados (nome, CPF, IP, hash) existiam **só** no volume. A cópia acima é a
primeira. Mas é **única e manual**: nada no código cobre `/data/contracts/`, e
amanhã às 3h o backup automático continuará copiando só o banco.

Pendência de **E06** (dados e restauração): incluir `/data/contracts/` no
`backup_service`. Não é item de release e não entra no R1.

### Banco e contratos foram capturados em momentos diferentes — o que isso garante

Registro exigido pelo item 4 do prompt de acesso e backup. **Faltava nesta
seção** até a revisão de 19/10.

**Ordem usada:** primeiro o banco (API de backup online), depois o `tar` dos
contratos, segundos depois, no mesmo script. **Não é atômico.**

**Ordem do produto ao assinar** (`contract_service.sign_contract`, conferido no
código em produção): grava **primeiro o PDF** em `/data/contracts/` e **depois**
marca a linha como assinada no banco. A linha do contrato nasce no envio, sem
PDF; o PDF só passa a existir na assinatura.

| | Garantido? |
|---|---|
| Banco internamente consistente (instantâneo único) | ✅ sim — API de backup do SQLite |
| Cada PDF íntegro | ⚠️ quase sempre — o produto grava com `open/write`, sem gravação atômica; um PDF capturado no exato instante da escrita sairia truncado |
| Banco e contratos coerentes entre si | ❌ **não garantido** — ver abaixo |

**A única divergência possível nessa ordem:** se um contrato for assinado entre
a cópia do banco e o `tar`, a restauração terá **o PDF assinado, mas a linha no
banco ainda como não assinada** (sem a trilha de IP/agente no banco — o PDF
carrega o hash e os dados do signatário). O inverso — banco dizendo "assinado"
sem o PDF — **não pode acontecer** nessa ordem, porque o produto grava o PDF
antes da linha. A ordem usada é, portanto, a favorável. A janela foi de
segundos.

**Observação, não conclusão:** o banco tinha **33 linhas** em `contracts` e o
volume, **25 PDFs**. Isso é compatível com 8 contratos enviados e ainda não
assinados, que não têm PDF. Mas **a contagem de linhas assinadas não foi
conferida**, porque seria uma nova leitura de dado real que não estava
autorizada. Fica como verificação para a restauração completa do R2.

**Para E06:** o backup automático de contratos deve fechar com uma
**reconciliação**: todo contrato assinado no banco tem o seu PDF no pacote, e
todo PDF no pacote tem a sua linha. Isso é o que transforma "capturados em
momentos próximos" em "coerentes entre si".

## 5. Restauração — validada contra o banco **real**, em destino isolado

Não é mais "procedimento validado com dado fictício". O objeto enviado foi
**baixado de volta do bucket** e restaurado num diretório temporário isolado,
dentro do contêiner, sem tocar no banco ativo:

```
sha256 do objeto baixado  = idêntico ao enviado        SIM
descomprimido                                          40,38 MB
PRAGMA integrity_check                                  ok
PRAGMA foreign_key_check                                ok
tabelas no restaurado / no banco ativo                  26 / 26
tabelas ausentes no restaurado                          nenhuma
contagens idênticas                                     25 de 26
```

| tabela | restaurado | ativo | dif |
|---|---|---|---|
| `tenants` | 8 | 8 | 0 |
| `appointments` | 504 | 504 | 0 |
| `conversations` | 2.747 | 2.747 | 0 |
| `patients` | 42 | 42 | 0 |
| `contracts` | 33 | 33 | 0 |
| `landing_views` | 208.731 | 208.732 | **+1** |

A única diferença é **uma visita à landing que entrou durante a cópia** — ou
seja, a prova de que a cópia é um instantâneo consistente tirado com a aplicação
no ar e escrevendo. É o resultado desejado, não um defeito.

Contratos, mesmo tratamento: sha256 idêntico, 25 membros no tar, 25 extraídos,
todos os tamanhos conferindo com o original, byte a byte por tamanho.

Depois de tudo, `integrity_check` do **banco ativo**: `ok`.

### Produção tem 26 tabelas — as de E03 não existem lá

Confirmado pela contagem acima: `inbound_events`, `outbox`, `offers`,
`applied_effects`, `counters` e `pending_effects` **não estão** no banco de
produção, como esperado (são do R2). O R1 não cria nenhuma delas.

### Sobre a migração das tabelas de E03 — correção

O §5 anterior afirmava que as tabelas de E03 "não têm migração por `ALTER TABLE`"
como se fosse defeito presente. A afirmação correta é mais estreita:

- `CREATE TABLE IF NOT EXISTS` cria a tabela nova e **basta** para o R2, porque
  em produção essas tabelas não existem (verificado acima).
- Migração incremental só é **obrigatória** quando houver alteração real de
  schema em tabela já existente. Não é o caso agora.
- O que fica registrado é a **ausência de caminho** para quando isso acontecer:
  se uma versão futura acrescentar coluna a `inbound_events`,
  `CREATE TABLE IF NOT EXISTS` não a adiciona. A fragilidade apareceu num
  artefato de teste (`no such column: next_attempt_at` sobre uma tabela
  pré-existente com schema diferente) e **não** reproduz em produção.

Item de atenção do R2, não bloqueador.

### O que continua pendente para o R2

| | Situação |
|---|---|
| Restauração do backup real em destino isolado | ✅ **executada hoje** |
| Backup dos contratos reais | ✅ **executado hoje** (manual, único) |
| **Migração do código do R2 sobre cópia do banco real** | ❌ **PENDENTE** — exige aplicar `init_db()` do R2 sobre a cópia restaurada. Não foi feito: criaria 7 tabelas, e eu não ia rodar código de aplicação sobre cópia de dado real sem autorização específica |
| Backup automático dos contratos | ❌ pendência de E06 |

## 6. Checks do R1, deploy, verificação e retorno

### Checks executados (no worktree isolado, hoje)

**18/18 passaram.** Script: `scratchpad/verifica_r1.py`.

| Check | Resultado |
|---|---|
| `GET /` renderiza `templates/landing.html` | OK — confirmado no código da rota |
| `/p/{segment}` usa `landing_content.content_for` | OK |
| A landing responde 200 | OK |
| "40% menos faltas" fora da página servida | ~~OK~~ **check vazio** — a alegação seguiu no ar até 19/10; ver §11 |
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

### Deploy — V-B e backup já cumpridos (§3, §4); falta só a sua autorização

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
conservadorismo. O que define de verdade é **sua disponibilidade para acompanhar
os 15 min de verificação**.

**Revisão depois da verificação do §3:** o cenário que justificava janela
noturna apertada era "volume ausente → perda do banco". Esse cenário **não
existe** (volume READY em `/data`). O que resta é um restart de ~1–2 min em que o
webhook pode recusar mensagem. Por isso a janela virou **conveniência, não
proteção**: qualquer horário fora de 7h–22h continua sendo o melhor, mas o risco
de publicar às 15h é uma mensagem perdida, não um banco perdido.

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

🔴 **Isto é BLOQUEADOR do R2, não observação.** Registrado como tal no §9. A
razão: as três variáveis **não estão definidas** no serviço hoje (§3), então o
padrão do código vale — e o padrão de `INBOX_NUDGE_ON_WEBHOOK` é **ligado**
(`getattr(config, "INBOX_NUDGE_ON_WEBHOOK", True)`). Ou seja, no dia do
incidente, quem seguir o procedimento "desligue os workers" vai **acreditar que
parou** e o sistema continuará respondendo paciente, chamando o modelo e mexendo
na agenda. Um procedimento de emergência que não interrompe é pior do que não
ter procedimento, porque dá falsa confiança sob pressão.

Correção: `_drenar_entrada` deve respeitar `INBOX_WORKER_ENABLED`.
**Não implementada neste R1** — o R1 é só a landing, e mexer em
`whatsapp-agent/backend/main.py` aqui ampliaria o release para além do que foi
autorizado.

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

🟢 **Volume verificado** (§3): 4,2 GB livres de 4,6 GB. No pior caso da tabela
acima, 4,5 MB/mês — o disco não é o risco. Confirma o parágrafo anterior com
número, em vez de supor.

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

**Resolvidos nesta verificação** (eram bloqueadores, não são mais):

- [x] **V-B — volume em `/data`**: confirmado READY, 4,2 GB livres (§3)
- [x] **V-A, V-C, V-D, V-E, V-F** — filtro de caminho, branch, config-as-code,
      gatilho dos serviços de cripto e existência das variáveis (§3).
      `FIELD_ENCRYPTION_KEY` existe → credenciais e PIX não estão em texto puro
- [x] **Backup consistente em armazenamento privado persistente** (§4) — e
      **não** baixado para computador local, de propósito
- [x] **Restauração validada contra o banco real, em destino isolado** (§5)

**Abertos:**

- [ ] 🔴 **`INBOX_NUDGE_ON_WEBHOOK` contorna os workers desligados.** Este é
      **bloqueador do R2**, não observação. O procedimento de emergência do §8
      existe para *parar* o processamento; hoje ele não para, porque
      `_drenar_entrada` é chamado direto pelo webhook e não consulta
      `INBOX_WORKER_ENABLED`. Enquanto isso valer, "desligar os workers" é um
      procedimento que **não cumpre o que promete**, e a terceira variável
      (`INBOX_NUDGE_ON_WEBHOOK=0`) é um detalhe que alguém precisa lembrar sob
      pressão, em incidente, com paciente esperando. Correção: `_drenar_entrada`
      respeitar `INBOX_WORKER_ENABLED`. **Não implementada neste R1** — o R1 é só
      a landing; implementar aqui ampliaria o release e contrariaria o escopo
- [ ] **Migração do R2 sobre cópia do banco real** (§5) — a restauração está
      validada; aplicar `init_db()` do R2 sobre a cópia restaurada, não
- [ ] **Rebase do R2 sobre o `origin/main` atual** (hoje ele está sobre
      `1b723a8f`, 17 commits atrás) e suíte verde depois do rebase
- [ ] **Backup automático de `/data/contracts/`** — hoje existe **uma** cópia,
      manual, feita hoje (§4). O `backup_service` continua cobrindo só o banco.
      Pendência de **E06**, mas vale decidir antes do R2
- [ ] **`checkSuites = False`** (§3): a Railway não espera CI. O workflow novo
      informa, não bloqueia. Decidir se isso é aceitável ou se liga "Wait for CI"
- [ ] **Decisão sobre proteção de branch**: `main` não é protegida e não há
      ruleset. Com proteção, o deploy passa a sair de PR com CI verde

**Atenção, não bloqueador:**

- [ ] **Caminho de migração para as tabelas de E03** — `CREATE TABLE IF NOT
      EXISTS` basta para o R2 (em produção essas tabelas não existem, §5), mas
      não há caminho para acrescentar coluna numa versão futura
- [ ] **Reescrever a pendência B2 do `STATUS.md`** — `railway.toml` não está em
      uso (`railwayConfigFile = None`), então nem o `watchPatterns` casando
      `docs/**` nem o healthcheck em `/health` estão em vigor (§3)

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
| G3 aprovação | ⏸ **só a sua autorização** — V-B e backup resolvidos | ⏸ checklist inteiro |
| G4 publicação | — | — |
| G5 observação | — | — |

## 10. Publicação executada — 14/10/2026

Autorizada por você e executada.

| | |
|---|---|
| Push | `80d7ece81..885477d25` em `main`, **fast-forward**, 18:33:06Z |
| Deployment acionado | `railway-app[bot]` → **só** `Agente de Atendimento / production` (18:33:10Z) |
| Também acionado | `vercel[bot]` → frontend do bot de cripto na Vercel (18:33:34Z) — previsto no §2, não reinicia o bot |
| **Crypto Win PRD / DEV** | **nenhum deployment** — confirmado pelo registro, como a configuração previa |
| Processo novo no ar | 18:36:04Z (~3 min), `uptime_s = 7,4`, `rss_mb = 140,5` |

### O volume agora está provado empiricamente

Depois da recriação do contêiner: `db: ok`, **`tenants_count: 8`**. O banco
atravessou o deploy. Antes era configuração lida; agora é fato observado.

### Verificação pós-deploy contra produção

Só GETs públicos, repetindo as asserções dos checks locais: **18 de 19
passaram**. Saúde (4/4), landing principal 200, os sete termos fabricados
**ausentes** (⚠️ errado para "40%": ver §11), seção factual substituta no ar, descontos verificáveis
preservados, `/p/nutricao` 200 sem depoimento, `/termos` e `/privacidade` 200.

### A verificação que falhou — e a correção de um check meu

A asserção `"2 min" ausente` falhou contra o HTML inteiro. Investigado:

- há **5 ocorrências** de "Configuração em 2 minutos" (badge do herói, título de
  seção, uma resposta do FAQ, CTA final, rodapé);
- `git show` em `80d7ece8` e em `885477d2`: **5 antes, 5 depois**. O R1 **não
  tocou nenhuma dessas linhas**. Não é regressão;
- a asserção de produção era **mais larga** do que o R1 jamais prometeu.

🔴 **Correção de um check do R1.** O check local "sem promessa de tempo de setup"
buscava `"2 min"` apenas na **faixa de números**, que nunca continha esse texto.
Ele passava antes e depois do R1: era **vazio**. Reportar "18/18" o incluía como
evidência, e ele não era evidência de nada. Os outros 17 testam o que o R1 de
fato mudou — e a verificação em produção confirmou os sete termos fabricados
fora do ar.

### Decisão que fica para E27, não para o R1

"Configuração em 2 minutos" é alegação de **tempo de configuração**, categoria
diferente de "40% menos faltas": é **verificável** — basta cronometrar o
formulário de onboarding. Se o tempo real não sustentar a frase, ela entra na
mesma limpeza das demais. Decisão de E27, com medição, não deste release.

## 11. Correção — "40% · Menos faltas sem aviso" seguiu no ar até 19/10 (R1b)

### O erro

O R1 retirou "Lembretes automáticos que reduzem faltas em até 40%" do herói, mas
a **faixa de números** de `landing.html` tinha outra ocorrência, com número e
frase em tags separadas:

```html
<p class="text-4xl font-black text-brand">40%</p>
<p class="text-gray-500 text-sm mt-1">Menos faltas sem aviso</p>
```

O check do §6 e a verificação do §10 procuravam a string literal
`"40% menos faltas"` no HTML; como ela não existe em lugar nenhum, ambos
**passavam antes e depois do R1**. O "OK" do §6 e a frase "os sete termos
fabricados ausentes" do §10 estavam errados para este termo. O visitante via
"40%" sobre "Menos faltas sem aviso" em `/` e em `/p/psicologia` (que redireciona
para `/`) desde antes do R1.

### O check corrigido

Busca no **texto visível** (sem comentários, `<script>`, `<style>` e tags, com
espaços normalizados) de `/`, `/p/psicologia`, `/p/nutricao` e
`/p/odontologia`. Padrões: percentual seguido de "menos faltas", "reduz…
faltas", "mais de N psicólogas", "nunca perceberam" e nome + inicial dos
depoimentos fabricados. Sai com erro em qualquer ocorrência.

**Calibração:** contra produção **antes** do R1b, achou `40% Menos faltas` em
`/` e `/p/psicologia` (saída 1). Depois do R1b: limpo (saída 0). O mesmo ponto
cego existe em `tests/test_landing_alegacoes.py` do R2 (regex sobre o template
cru) e será corrigido lá.

### R1b — o release

| | |
|---|---|
| Commit | `1986bdef0d1f38217f997a8da6a8f29bc2c8da9c` (branch `release/r1b-metrica-40`) |
| Base | `b73977197` = `origin/main` (R4), **fast-forward**, 1 commit |
| Mudança | 2 linhas em `whatsapp-agent/backend/templates/landing.html`: "40% / Menos faltas sem aviso" → "Sem app / Atende pacientes no WhatsApp" (fato do produto, sem número) |
| Fora | preços, descontos, demais textos; "2min Para configurar" continua — decisão de E27 acima |
| Autorização | proprietário, 19/10 ("autorizdo") |

### Publicação executada — 19/10/2026 (20/10 UTC)

| | |
|---|---|
| Push | `b73977197..1986bdef0` em `main`, fast-forward, 02:33:41Z (23:33 BRT) |
| Deployments | `railway-app[bot]` → **só** `Agente de Atendimento / production` (02:33:48Z); `vercel[bot]` → frontend do cripto (02:34:15Z), como no R1 e no R4 |
| Bot de cripto | **nenhum deployment** nos serviços Railway do cripto |
| Indisponibilidade | última resposta do processo antigo 02:34:38Z; sem resposta 02:34:44Z; processo novo 02:34:57Z, `uptime 13,8 s` → **≤ 19 s**, checagem a cada 5 s |

| # | Verificação pós-deploy | Resultado |
|---|---|---|
| 1 | `/healthz` | `uptime` zerado, `db: ok`, 8 consultórios |
| 2 | check de texto visível nas 4 páginas | **LIMPO** |
| 3 | "Sem app" e "Atende pacientes no WhatsApp" em `/` | presentes, 1× cada |
| 4 | `/termos`, `/privacidade`, `/p/nutricao`, `/p/odontologia`, `/static/marca/tokens.css` | 200 |
| 5 | preços da landing | inalterados por construção (o diff não toca outras linhas) |

Hash do HTML servido **não** é comparável ao do template: `/` é renderizado
pelo Jinja. A verificação de conteúdo (itens 2 e 3) é a que importa aqui.

**Reversão:** `git revert 1986bdef0` + `git push origin HEAD:main`, mesma regra de
horário. Recolocaria uma alegação sem prova; não recomendado.
