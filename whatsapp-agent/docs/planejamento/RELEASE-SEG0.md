# SEG-0 — credencial em branco recusada e webhook Stripe sem segredo recusado

Preparado em 05/11/2026. **Publicação autorizada pelo proprietário** em 05/11/2026,
antes da janela das 22h (opção "Agora", escolhida contra a recomendação da janela).
Correção de segurança avulsa, fora da sequência de etapas (escopo E06/E07).

## Versão exata

| | |
|---|---|
| Branch | `fix/seg0-token-stripe` (worktree `.claude/worktrees/seg0-token-stripe`) |
| Candidato | `d0f77ad22` + commit do carimbo `2026-11-05-71` (o push sempre leva o topo conferido na hora) |
| Base | `2d8147c02`, referência local de `origin/main`. **Conferir no remoto antes do push.** Que esta seja a versão no ar é NÃO VERIFICADO |
| Fora de `whatsapp-agent/` | nada |
| Código | 2 arquivos, 13+/0− (`backend/database.py`, `backend/main.py`) |
| Testes | 1 arquivo novo, `tests/test_seg0_fail_closed.py`, 79 testes; suíte completa com 525 passando |
| Migração | nenhuma; sem alteração de esquema |

## O que entra

| Falha fechada | Antes | Depois |
|---|---|---|
| `dashboard_token` vazio ou só com espaços | as 71 rotas com o resolvedor respondiam como a primeira empresa sem token | 403 |
| `setup_token` vazio ou só com espaços | `/onboarding/info` devolvia o link do painel e o `webhook_token` dessa empresa; checkout e pagamento a resolviam | 404; webhooks de pagamento ignoram a referência |
| `STRIPE_WEBHOOK_SECRET` ausente ou só com espaços | evento assinado com HMAC de chave vazia era aceito e ativava empresa | 503, sem processar |

**Para as psicólogas:** quem entra pelo próprio link não percebe mudança.

## Efeitos conhecidos

- Empresa sem `dashboard_token` perde as APIs. O painel e o controle já a recusavam.
- Empresa suspensa ou pendente **sem `setup_token`** recebe 404 ao ser redirecionada
  do painel para o pagamento. Saída: `/master/onboarding-link/{slug}` gera o token
  (código lido, não exercitado). Só o painel do operador leva a esse estado.
- Sem `STRIPE_WEBHOOK_SECRET`, a Stripe recebe 503 até o segredo ser configurado.
  Pela leitura do código, eventos legítimos já falhavam com 400 nessa condição.

## Riscos herdados do R6

Não reconferidos aqui: a Railway não espera o CI (`checkSuites = False`) e a `main`
não tem proteção de branch. O push publica mesmo com o CI vermelho, por isso as
verificações abaixo rodam antes dele.

## NÃO VERIFICADO

- Se há empresa ativa sem `dashboard_token` ou sem `setup_token` em produção.
- Se `STRIPE_WEBHOOK_SECRET` está configurado em produção.
- Comportamento real com a API da Stripe e do Mercado Pago: os testes usam dublês.
- Tratamento de cabeçalho só com espaços pelo servidor HTTP real: os testes usam o
  cliente de teste.

Sondagem sem credencial, sem efeito colateral e descartando o corpo da resposta:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://agente-atendimento-production.up.railway.app/dashboard/api/appointments
```
`200` = há empresa ativa sem token de painel; `403` = nenhuma.

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://agente-atendimento-production.up.railway.app/onboarding/pagamento
```
`200` = há empresa ativa sem `setup_token`; `404` = nenhuma.

Enquanto a correção não estiver no ar, não emitir token de painel para empresa sem
`setup_token`: o token novo ficaria exposto por `/onboarding/info`.

## Janela e acompanhamento

Depois das 22h (BRT), como nos releases anteriores: o agendador não envia mensagens
e o volume de pacientes é baixo. A troca de contêiner deixa o atendimento ~20 s sem
resposta. **Exceção a decidir pelo proprietário:** se uma sondagem responder `200`,
há exposição ativa e vale publicar antes da janela.

## Procedimento

1. Commit do carimbo e deste documento.
2. Conferir que `git ls-remote origin main` ainda aponta `2d8147c02`. Se andou, parar.
3. Suíte verde no topo do branch.
4. Registrar `/healthz` de antes.
5. `git push origin HEAD:main`, nunca `--force`, com checagem de `/healthz` a cada 5 s.

Sem migração, não há backup específico deste release.

## Verificação pós-deploy

| # | Checagem | Esperado |
|---|---|---|
| 1 | `/healthz` | `instance_start = ts − uptime_s` posterior ao push; `db: ok`; contagens iguais às de antes |
| 2 | deployments | **só** o serviço do Agente (e o frontend do cripto na Vercel, como sempre) |
| 3 | `GET /dashboard/api/appointments` sem cabeçalho | `403` |
| 4 | `GET /onboarding/info?setup_token=` | `404` |
| 5 | `GET /onboarding/pagamento` | `404` |
| 6 | `POST /webhooks/stripe` sem corpo | `400` = segredo configurado; `503` = ausente |
| 7 | `GET /` (landing) | `200` |
| 8 | proprietário abre o próprio link de painel | carrega normalmente |

Nas checagens 3 a 6, registrar só o código HTTP, descartando o corpo.

## Depois do deploy (proprietário)

Para cada empresa que a sondagem indicar ou que a consulta abaixo mostrar, gerar o
`setup_token` primeiro e girar depois o token de painel e o `webhook_token`:

```sql
SELECT slug, status,
       TRIM(COALESCE(dashboard_token, '')) = '' AS sem_painel,
       TRIM(COALESCE(setup_token, '')) = '' AS sem_setup
FROM tenants
WHERE active = 1
  AND (TRIM(COALESCE(dashboard_token, '')) = '' OR TRIM(COALESCE(setup_token, '')) = '');
```

## Parada de emergência

Sem mudança: a do R6 (`INBOX_WORKER_ENABLED=0`, `OUTBOX_WORKER_ENABLED=0`).

## Reversão

Nunca `reset` nem `push --force`. Um commit novo, com novo carimbo:

```bash
git revert --no-edit d0f77ad22
git push origin HEAD:main
```

Sem migração, o banco não precisa de ajuste. **Reverter reabre as três falhas.** Se o
problema for só o 503 da Stripe, a saída é configurar o segredo, não reverter.
