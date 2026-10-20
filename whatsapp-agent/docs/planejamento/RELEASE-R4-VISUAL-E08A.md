# R4 — release visual da E08A

Preparado em 19/10/2026. **PUBLICADO em 19/10/2026, 21h12 BRT**, por autorização
explícita do proprietário ("pode fazer o push agora"), antes das 22h por decisão
dele. Registro da publicação no fim deste arquivo.

## Versão exata

| | |
|---|---|
| Commit | `b73977197b48b34447cef70cbde45b7b7b2dd8d0` |
| Branch local | `release/r4-visual-e08a` (worktree `scratchpad/e08a`) |
| Base | `c5135904e` = `origin/main` (reconferido em 19/10, 21h06 BRT); **fast-forward**, 1 commit |
| Conteúdo | 5 arquivos, todos em `whatsapp-agent/backend/`: `templates/dashboard.html`, `templates/controle.html`, `static/marca/tokens.css`, `static/marca/simbolo-assistente-256.png` e `-64.png` — 792+/408− |
| Fora do release | documentação (fica no branch do planejamento), backend, banco, migrações, autenticação, variáveis |
| Carimbo de build | não incluído — mesmo motivo do R1/R3: `/healthz` não expõe versão; a confirmação é o `uptime_s` zerar |

## Evidência

- Validação completa da E08A: `EVIDENCIAS/08A.md`.
- **Contra os arquivos do commit**, conferidos por hash e tamanho: suíte do R2
  com este layout — **324 passaram, 0 falhas**.
- Checagem final nos arquivos do release, em 390, 768 e 1440 px: 0 vazamento
  horizontal nas 6 seções; a última consulta não fica sob a barra inferior; 0
  erros de JS; render Jinja e `node --check` ok no painel e no controle.
- Empacotamento: o `.dockerignore` não exclui `static/marca/`; o Dockerfile copia
  `backend/` inteiro; a produção já serve `/static` (`manual.pdf` 200).
  `tokens.css` responde **404 hoje**, como esperado, e vira a checagem
  pós-deploy.

## O que as psicólogas vão notar

Só apresentação, mas visível para os 8 consultórios no próximo acesso:

| Mudança | Observação |
|---|---|
| cores vinho/rosé; menu lateral (computador) e barra inferior com 6 itens (celular) | o modal de boas-vindas **não reaparece**: a chave salva no navegador é a mesma |
| "Cobranças" tem item próprio; "Configurações" abre em "Geral" | antes, cobrança só era alcançada dentro de Config |
| o quadro "Hoje" aparece também com zero | antes sumia |
| o selo "Online" agora diz "Ao vivo" ou "Reconectando…" | antes era fixo e não verificava nada |
| em Agenda, a configuração fica separada em recorrente e pontual | mesmos campos, mesmo botão de salvar |

Vale um aviso curto às clientes antes ou logo depois — posso redigir.

## Riscos

| Risco | Avaliação |
|---|---|
| Quebra de função | baixo: nenhum endpoint muda; 9 fluxos pela interface conferidos em banco de teste; ids e handlers preservados |
| **Novo terceiro: Google Fonts** | o navegador de quem abre o painel passa a pedir as fontes a `fonts.googleapis.com`, que recebe o IP, o mesmo tipo de exposição que o CDN do Tailwind já tem. Se falhar, o texto aparece na fonte do sistema, sem quebrar o layout |
| Tailwind continua no CDN | **não muda com este release**: o risco do painel sem estilo (15/10) segue igual |
| Cache de navegador | o painel é HTML renderizado a cada acesso; `tokens.css` e o símbolo são novos, sem versão antiga em cache |
| Bot de cripto | não é tocado: os 3 serviços de cripto têm `rootDirectory` em `backend/` da raiz; só o serviço do Agente (`whatsapp-agent`) reconstrói, como no R1 e no R3 |

## Janela

**Depois das 22h** (BRT). A troca de contêiner deixou o serviço sem resposta
por **~10–30 s** no R3 (medido); depois das 22h o agendador não envia
mensagens. Nenhum backup extra é necessário: não há mudança de banco nem de
schema, e o backup diário (03h) segue.

## Publicação (só com autorização)

```bash
git -C <scratchpad>/e08a push origin release/r4-visual-e08a:main
```

## Verificação pós-deploy (~10 min)

1. `/healthz`: `uptime_s` zerado, `db: ok`, `tenants_count: 8`.
2. Deployment registrado **só** no serviço do Agente.
3. `GET /static/marca/tokens.css` e `/static/marca/simbolo-assistente-256.png`:
   **200**, com sha256 igual ao do commit.
4. Dentro do contêiner, somente leitura: hash de `dashboard.html` e
   `controle.html` igual ao do commit.
5. Landing (R1) intacta: `/` 200, sem os termos retirados.
6. **Proprietário** abre o próprio painel e confirma visualmente. Eu não abro
   painel de cliente: exige token real e dados de pacientes.

## Reversão

```bash
git revert b73977197b48b34447cef70cbde45b7b7b2dd8d0
git push origin HEAD:main
```

Volta os dois templates ao estado anterior. Os arquivos de `static/marca/`
saem junto, sem efeito colateral, porque nada mais os referencia. Não há dado a
restaurar. A reversão também reconstrói o serviço (outra janela de ~10–30 s),
então segue a mesma regra de horário. Nunca `reset` nem `push --force`.

## Depois do R4

O branch do R2 tem os mesmos templates de `main` antes do R4. Quando o R2 for
rebaseado em `main`, o layout entra sem conflito: a suíte do R2 com estes
arquivos já foi verificada (324/324).

## Publicação executada — 19/10/2026

| | |
|---|---|
| Push | `c5135904e..b73977197` em `main`, fast-forward, 00:12:19Z (21:12 BRT) |
| Deployments | `railway-app[bot]` → **só** `Agente de Atendimento / production` (00:12:24Z); `vercel[bot]` → frontend do cripto (00:12:54Z) |
| Bot de cripto | **nenhum deployment** |
| Processo novo | 00:13:50Z, `uptime 12,8 s`, `db: ok`, 8 consultórios |

### Janela de indisponibilidade — medida com checagem a cada 5 s

```
00:13:31Z  processo antigo responde
00:13:44Z  sem resposta (timeout de 8 s)
00:13:50Z  processo novo responde, uptime 12,8 s → subiu ~00:13:37Z
```

No máximo **~19 s** sem atendimento, entre 21h13min31s e 21h13min50s (BRT).
É consistente com o R3 (~10–30 s, medido com intervalo maior).

### Verificação pós-deploy

| # | Checagem | Resultado |
|---|---|---|
| 1 | `/healthz` | `uptime` zerado, `db: ok`, 8 consultórios |
| 2 | deployments | só o serviço do Agente |
| 3 | `tokens.css`, símbolo 256 e 64 servidos em `/static/marca/` | **200**, sha256 **idêntico** ao do commit |
| 4 | dentro do contêiner, somente leitura: `dashboard.html`, `controle.html` e os 3 estáticos | sha256 **idêntico** ao do commit, nos 5 arquivos |
| 5 | landing do R1, `/termos`, `/privacidade`, `/p/nutricao` | 200; termos retirados continuam ausentes |
| 6 | painel com consultório inexistente | 404 — rota viva, sem expor dado |
| 7 | **confirmação visual no próprio painel** | **pendente — proprietário** |
