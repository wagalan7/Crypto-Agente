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

## 4. Backup consistente, antes de qualquer deploy

### O que precisa ser copiado

`DATA_DIR=/data` guarda **três** coisas, não só o banco:

| Caminho | O que é | Sensibilidade |
|---|---|---|
| `/data/consultorio.db` | banco inteiro: consultas, conversas, pacientes, cobranças | 🔴 conteúdo clínico |
| `/data/contracts/` | PDFs de contrato assinados (nome, CPF, IP, hash) | 🔴 dado pessoal e contratual |
| `/data/.backup_state` | estado do backup automático | baixa |

### `cp` não serve

O app usa `journal_mode=DELETE` com `busy_timeout=30000`. Copiar o arquivo com
`cp` enquanto há escrita em curso pode capturar um estado rasgado — o arquivo
abre, mas com transação pela metade. O caminho correto é a **API de backup
online do SQLite**, que é consistente mesmo com escritor concorrente.

A imagem é `python:3.11-slim`: traz o **módulo** `sqlite3`, não o binário
`sqlite3` da linha de comando. Então o backup usa a biblioteca padrão.

### Procedimento (executado por você, no shell do serviço na Railway)

```bash
# 1. espaço livre no volume — o backup ocupa ~o tamanho do banco
df -h /data

# 2. backup consistente do banco (API de backup online)
python - <<'PY'
import sqlite3, datetime, os
sel = f"/data/backup-{datetime.datetime.now():%Y%m%d-%H%M}.db"
o = sqlite3.connect("/data/consultorio.db")
d = sqlite3.connect(sel)
with d:
    o.backup(d)          # consistente, sem parar o serviço
d.close(); o.close()
print("gerado:", sel, os.path.getsize(sel), "bytes")
PY

# 3. integridade do que foi gerado (não imprime conteúdo)
python - <<'PY'
import sqlite3, glob
ultimo = sorted(glob.glob("/data/backup-*.db"))[-1]
c = sqlite3.connect(f"file:{ultimo}?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
print("consultórios:", c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0])
print("consultas:", c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0])
PY

# 4. contratos
tar -czf /data/contracts-$(date +%Y%m%d-%H%M).tar.gz -C /data contracts 2>/dev/null \
  && ls -la /data/contracts-*.tar.gz || echo "sem diretório contracts"
```

Depois **baixe os dois arquivos** (painel da Railway → serviço → Files, ou
`railway run`/`railway ssh` quando reautenticado) e **guarde fora da Railway**.
Em seguida **apague as cópias de `/data`**, para não ocupar o volume:

```bash
rm -f /data/backup-*.db /data/contracts-*.tar.gz
```

### Regras de privacidade, não negociáveis

- As cópias **não entram no repositório** (`*.db` e `*.tar.gz` já são ignorados,
  mas o lugar delas é fora do git).
- Nenhum relatório, evidência ou log desta etapa contém conteúdo de conversa,
  nome de paciente ou CPF. Os comandos acima imprimem **apenas contagens e o
  resultado do `integrity_check`**.
- **Eu não executei nenhum desses comandos** e não copiei dado real: isso exige
  autorização específica sua, e o acesso é seu.

## 5. Validação da restauração — **PENDENTE**

> **Backup existente não é restauração validada.** O backup automático das 3h vai
> para S3/R2/B2 e **nunca foi restaurado** (achado de E01, item aberto de E06).
> Enquanto o teste abaixo não rodar, a restauração segue **NÃO VALIDADA**.

Roteiro, em ambiente isolado, sobre a **cópia** baixada no §4:

```bash
# diretório temporário, fora do repositório e fora da Railway
mkdir -p ~/restauracao-teste && cd ~/restauracao-teste
cp <backup-baixado>.db ./consultorio.db

# 1. integridade e contagens (sem imprimir conteúdo)
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:consultorio.db?mode=ro", uri=True)
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
for t in ("tenants","appointments","conversations","patients","contracts"):
    try: print(f"{t}: {c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
    except Exception as e: print(f"{t}: {e}")
PY

# 2. a migração do R2 passa sobre o banco real? (para o R1 não é necessário)
DATA_DIR=~/restauracao-teste /tmp/homolog-agente/venv/bin/python -c \
 "import sys; sys.path.insert(0,'<worktree>/whatsapp-agent/backend'); \
  import database as d; d.init_db(); print('migrou; consultórios:', len(d.list_tenants()))"

# 3. apagar a cópia quando terminar
cd ~ && rm -rf ~/restauracao-teste
```

O passo 2 é o teste de migração do R2 **contra dado real**, que hoje só foi feito
contra um banco fictício. Ele é pré-requisito do R2, não do R1.

Registro: **não executado nesta sessão** — depende do backup do §4, que depende
da sua autorização.

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

## 8. Rollback do R2 — revisado

O roteiro anterior dizia "drenar as filas antes do revert". **Isso pode ser
impossível**: se o provedor de WhatsApp está fora — que é justamente quando as
filas enchem — não há como drenar o outbox. Era uma premissa errada.

### Procedimento revisado

**Passo 1 — parar de aceitar trabalho novo, sem perder nada.**

```
INBOX_WORKER_ENABLED=0
OUTBOX_WORKER_ENABLED=0
```

Os dois juntos (o boot recusa inbox ligado com outbox desligado). Os webhooks
continuam **gravando** os eventos: nada é recusado ao provedor, nada se perde,
e o processamento simplesmente para. Eventos em `processing` voltam a `pending`
quando o lease vence.

**Passo 2 — tentar drenar, com limite de tempo.** Se o provedor responde, uma
passada do outbox resolve. Se não responde em ~10 min, **não insista**: siga.

**Passo 3 — preservar as pendências. A cópia fica no banco, não num arquivo.**

A mensagem do paciente (`inbound_events.body`) é conteúdo clínico. Exportá-la
para um CSV ou para um relatório seria vazar dado sensível para fora do banco.
Então:

- a preservação é o **backup do §4** — ele já contém `inbound_events`, `outbox`,
  `offers` e `pending_effects` inteiros, com o conteúdo, protegido pelas mesmas
  regras do banco;
- para **agir**, o operador precisa só de uma lista **sem conteúdo**: quem ficou
  sem resposta. Isso o `GET /admin/fila` já devolve (ele não expõe `body`).

```bash
# lista sem conteúdo clínico, para contato manual
curl -s "$BASE/admin/fila?key=$MASTER_KEY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('— mensagens sem resposta —')
for e in d['entrada']:
    print(f\"evento {e['id']}  {e['idade_min']} min  estágio {e['stage']}\")
print('— respostas que não saíram —')
for o in d['saida']:
    print(f\"saída {o['id']}  {o['state']}  {o['kind']}\")
print('— pendências —')
for p in d['pendencias']:
    print(f\"{p['id']}  {p['kind']}  {p['detalhe'][:60]}\")
" > ~/pendencias-$(date +%Y%m%d-%H%M).txt
```

Esse arquivo tem **id, idade, estágio e telefone mascarado** — nenhum texto de
conversa. Guardar fora do repositório.

**Passo 4 — revert e novo carimbo.**

```bash
git revert --no-commit <commits do R2>
sed -i '' '2s/.*/# build: <nova data>-NN/' whatsapp-agent/Dockerfile
git commit -m "revert: voltar E02+E03" && git push origin HEAD:main
```

**Passo 5 — recuperar depois.** Quando o provedor voltar, duas opções:

- **reaplicar o R2** (`git revert` do revert) e ligar os workers: as filas
  retomam de onde pararam, porque o estado está no banco e a idempotência por
  evento impede ação em dobro. É o caminho preferido;
- **ficar no código antigo**: aí as mensagens presas **não** serão respondidas
  automaticamente. O operador usa a lista do passo 3 e responde à mão, abrindo o
  painel para ver o que cada paciente escreveu.

### O que se perde, dito com clareza

**As tabelas permanecerem não significa que nada se perde.** No código antigo:

| Resíduo | Consequência real |
|---|---|
| `inbound_events` `pending` | **A resposta automática a essas mensagens é perdida.** O paciente escreveu e não vai receber resposta do agente. Só contato manual resolve |
| `outbox` `pending` | A resposta já redigida **não será enviada** |
| `outbox` `uncertain` | Continua sem saber se chegou — decisão humana, item por item |
| `offers` `active` | Degradação: todo "quero o 1" passa a pedir que o paciente repita o horário |
| `pending_effects` | Perdem a tela (`/admin/fila` não existe no código antigo) — daí o arquivo do passo 3 |
| Consultas criadas/remarcadas | **Permanecem.** Nenhuma consulta é perdida |

Em uma frase: **o dado persiste, o atendimento automático daquelas mensagens
não.** Quantas mensagens isso representa depende do momento — por isso o
passo 1 vem antes de tudo, para que o número pare de crescer.

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
