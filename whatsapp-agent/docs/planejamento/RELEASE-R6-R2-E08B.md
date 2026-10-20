# R6 — R2 (E01–E03) + E08B publicados juntos

Preparado em 20/10/2026. **Publicação autorizada pelo proprietário** ("autorizo"
e, na pergunta seguinte, a opção "Publicar R2 + E08B juntos", escolhida contra
a recomendação de publicar só a E08B). Ainda não publicado: o push sai no
horário combinado.

## Versão exata

| | |
|---|---|
| Branch | `integracao/r2-main-e08b` (worktree `scratchpad/integ`) |
| Candidato | `521853aead233591b3d0fe373c31d46bafbd9d0d` (o último merge, só de documentação, pode avançar este SHA; o push sempre leva o topo conferido na hora) |
| Base | `20b6d3051` = produção (R1 + R1b + R3 + R4 + R5); **fast-forward** |
| Fora de `whatsapp-agent/` | só `.github/workflows/agente-consultorio.yml`: CI de testes, `contents: read`, sem segredo e sem deploy. **Nenhum arquivo do bot de cripto** |
| Código e testes | 42 arquivos, 9.140+/208− (docs à parte) |

## O que entra

| Origem | Efeito em produção |
|---|---|
| **E02** | ofertas de horário numeradas pelo servidor ("1" reserva a opção 1 exibida); correção do dia errado; alegações retiradas do material local |
| **E03** | mensagem gravada **antes** do 2xx (sem perda silenciosa); fila de entrada com lease e retomada; **outbox** para as respostas; deduplicação por id do provedor; idempotência por evento; `GET /admin/fila` e ações de suporte (MASTER_KEY) |
| **E08B** | "Psicologia em foco" no Início no lugar da narrativa com IA; job diário às 09:00 de Brasília |
| **Correção da parada de emergência** (feita hoje, `29eddc7c3`) | desligar `INBOX_WORKER_ENABLED` e `OUTBOX_WORKER_ENABLED` agora para o processamento de verdade (antes o empurrão do webhook ignorava) |

**Para as psicólogas:** o atendimento pelo WhatsApp muda por dentro (fila
durável, ofertas numeradas); o Início troca a narrativa pelo boletim. Painel,
agenda, cobrança e páginas de venda continuam iguais.

## Bloqueadores do §9 do R1 — situação

| Item | Situação |
|---|---|
| 🔴 `INBOX_NUDGE_ON_WEBHOOK` contornava os workers desligados | ✅ **corrigido** (`29eddc7c3`), 4 testes; sem a correção, 3 falham |
| Migração do R2 sobre cópia do banco real | ✅ **executada hoje** dentro do contêiner, na cópia: ver abaixo |
| Rebase do R2 sobre o `main` e suíte verde | ✅ por **merge**: 411 de 411 (324 R2 + 83 E08B + 4 parada de emergência) |
| Backup automático de `/data/contracts/` | ⚠️ **risco aceito**: backup manual pré-R6 no lugar (abaixo); automático segue para E06 |
| `checkSuites = False` (Railway não espera CI) | ⚠️ **risco aceito** |
| `main` sem proteção de branch | ⚠️ **risco aceito** |
| **Homologação com WhatsApp e IA reais (8 cenários)** | ⚠️ **NÃO FEITA — risco aceito.** O R2 sobe "com acompanhamento próximo", **não** como integração homologada |
| Saída HTTPS da Railway para CFP e gov.br (E08B) | ⚠️ não verificada; a primeira coleta no boot mostra |

### Migração sobre cópia do banco real (20/10/2026, 15h20 BRT)

Rodado dentro do contêiner de produção (`railway ssh`): banco ativo aberto só
para leitura, cópia num diretório temporário do disco efêmero, `init_db()` da
versão candidata aplicado **na cópia**, tudo apagado no fim.

```
cópia                         44,08 MB em 0,5 s
antes                         26 tabelas · integrity_check ok
init_db                       0,33 s (1ª) · 0,00 s (2ª, idempotente)
depois                        37 tabelas · integrity_check ok · 0 violações de FK
tabelas novas (11, vazias)    applied_effects counters inbound_events offers outbox
                              pending_effects slot_offers noticias_edicoes
                              noticias_execucoes noticias_fontes_estado noticias_itens
colunas novas/removidas em tabelas existentes   nenhuma
contagens alteradas em tabelas existentes       nenhuma
contagens-chave               8 consultórios · 526 consultas · 42 pacientes · 33 contratos
banco ATIVO depois            26 tabelas · integrity_check ok
```

## Janela e acompanhamento

Depois das 22h (BRT), como R1–R5: o agendador não envia mensagens e o volume
de pacientes é baixo. A troca de contêiner deixa o atendimento **~20 s sem
resposta**. Mensagem que chegar **nesses segundos** depende de o provedor
reenviar o webhook (não garantido). Com o app no ar, a E03 grava toda mensagem
antes de responder ao provedor. **O proprietário fica disponível por ~30 min depois
do push** para acompanhar (risco aceito de integração não homologada).

## Procedimento

1. Reconferir: `origin/main` ainda é `20b6d3051`; suíte verde no topo do branch.
2. **Backup pré-R6** dentro do contêiner (`backup_pre_r6_container.py`): banco
   e contratos para o bucket privado (`consultorio/pre-release-R6/`,
   `contracts/pre-release-R6/`), baixados de volta e conferidos.
3. `git push origin integracao/r2-main-e08b:main` com checagem de `/healthz` a
   cada 5 s.

## Verificação pós-deploy

| # | Checagem |
|---|---|
| 1 | `/healthz`: `uptime` zerado, `db: ok`, 8 consultórios |
| 2 | deployments: **só** o serviço do Agente (e o frontend do cripto na Vercel, como sempre) |
| 3 | log do boot: scheduler, worker de inbox, worker de outbox e job do boletim iniciados |
| 4 | `GET /admin/fila` (MASTER_KEY): nada preso, nada `failed` |
| 5 | as 11 tabelas novas existem; contagens-chave iguais às de antes |
| 6 | `GET /admin/noticias/estado`: preenchimento inicial com as fontes reais (prova a saída HTTPS) |
| 7 | landing e páginas de venda iguais às de antes (texto visível) |
| 8 | **primeiras mensagens reais**: aparecem em `inbound_events` e saem `done`, com resposta no `outbox` aceita pelo provedor — observação, sem enviar nada por conta própria |

## Parada de emergência (agora funciona)

Na Railway, definir e reiniciar:

```
INBOX_WORKER_ENABLED=0
OUTBOX_WORKER_ENABLED=0
```

O recebimento continua gravando (nada é recusado ao provedor); nada é
processado nem enviado. `INBOX_NUDGE_ON_WEBHOOK=0` é opcional (só tira o
empurrão). Monitorar a fila em `GET /admin/fila` (RELEASE-R1 §8).

## Reversão

Nunca `reset` nem `push --force`. Um commit novo que devolve o código do
produto ao estado de `20b6d3051`:

```bash
git checkout 20b6d3051 -- whatsapp-agent/backend
git commit -m "revert: volta o backend do Agente ao R5"
git push origin HEAD:main
```

- As 11 tabelas novas ficam no banco, sem uso pelo código antigo.
- Mensagens que estiverem em `inbound_events`/`outbox` pendentes **não** são
  processadas pelo código antigo (RELEASE-R1 §8, "o que se perde"). Por isso,
  antes de reverter: parada de emergência, drenar com prazo, lista sem
  conteúdo de `GET /admin/fila`.
- Backup pré-R6 disponível para restauração, se necessário.
