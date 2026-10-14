# Proteção contra publicação de revisão não testada

Preparado em E02. **Nada aqui foi aplicado** — as três partes dependem de painéis
externos (GitHub e Railway) que não são acessados por esta etapa. Até a
conferência, a configuração efetiva é **NÃO VERIFICADA**.

## O que já está no repositório

| Item | Onde | O que garante |
|---|---|---|
| Suíte de testes | `whatsapp-agent/tests/` | Roda local e no CI, sem produção e sem API paga |
| CI | `.github/workflows/agente-consultorio.yml` | Executa a suíte a cada push/PR que toque `whatsapp-agent/**` |
| Filtro de gatilho | `whatsapp-agent/railway.toml` → `watchPatterns` | Commit de documentação/teste não deveria reconstruir a produção |
| Guarda do healthcheck | `tests/test_webhook_autorizacao.py` | `healthcheckPath` do `railway.toml` tem de existir no app |
| Guarda do carimbo de build | passo do CI | Linha 2 do `Dockerfile` no formato `# build: AAAA-MM-DD-NN` |

**Um workflow existir não protege nada.** Ele reprova o commit no painel do
GitHub, mas não impede o merge nem o deploy. Isso é configuração, abaixo.

## 1. GitHub — proteger a branch `main`

Settings → Branches → Add branch ruleset, alvo `main`:

- [ ] **Require a pull request before merging** (bloqueia push direto em `main`)
- [ ] **Require status checks to pass** → marcar `Agente Consultório — testes`
- [ ] **Require branches to be up to date before merging**
- [ ] **Do not allow bypassing the above settings** (sem isso, o dono contorna tudo)
- [ ] **Block force pushes**

Consequência direta no procedimento de hoje: `git push origin HEAD:main` deixa de
funcionar e o deploy passa a sair de um PR aprovado com CI verde. É essa a troca
— menos atrito contra menos risco. Decisão do proprietário.

**Conferir** (sem alterar nada): Settings → Branches mostra os rulesets ativos;
Actions → a última execução mostra se o workflow rodou e em quais caminhos.

## 2. Railway — serviço do app

- [ ] Confirmar se o serviço usa **este** `railway.toml`. Em Settings → Config-as-code,
      ver se há um caminho de arquivo configurado e se aponta para
      `whatsapp-agent/railway.toml`. Se o campo estiver vazio, **o arquivo não
      está em efeito** e `watchPatterns`/`healthcheckPath` dele não valem nada.
- [ ] Confirmar o **Root Directory** do serviço (se for `whatsapp-agent`, os
      caminhos do `watchPatterns` precisam ser relativos a ele — hoje estão
      relativos à raiz do repositório).
- [ ] Confirmar **Wait for CI** (Settings → Deploy): com isso a Railway só
      publica depois do check verde do GitHub. É a proteção mais próxima de
      "não publicar revisão não testada".
- [ ] Confirmar se o deploy automático está ligado e para qual branch.
- [ ] Anotar os valores efetivos de `BASE_URL`, `FIELD_ENCRYPTION_KEY`,
      `MP_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_SECRET`, `SMTP_*`, `ADMIN_PASSWORD`
      e `MASTER_KEY` — **existência, não o valor.** Nunca copiar segredo para
      arquivo, mensagem ou documento.

## 3. Railway — serviço da Evolution (outro projeto)

- [ ] Trocar a imagem `evoapicloud/evolution-api:latest` por **tag ou digest fixo
      da 2.3.7**. Enquanto for `latest`, um redeploy pode puxar uma versão que
      exige licença e devolve `503` em tudo.
- [ ] **Restart**, nunca Redeploy, para sessão travada.

## Roteiro de conferência (ordem sugerida)

1. GitHub → Actions: o workflow aparece? Rodou no último push de `whatsapp-agent`?
2. GitHub → Settings → Branches: existe ruleset em `main`?
3. Railway (app) → Settings: config-as-code, root directory, wait for CI, branch.
4. Railway (app) → Variables: conferir existência das variáveis da lista acima.
5. Railway (Evolution) → Settings: imagem fixada?

Resultado de cada item vai para `docs/planejamento/EVIDENCIAS/` como
**VERIFICADO** ou **NÃO VERIFICADO**, com data. Enquanto nenhum item for
conferido, o pressuposto operacional é o pessimista: **qualquer push em `main`
publica**.
