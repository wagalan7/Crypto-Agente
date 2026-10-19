# R3 — "faltou sem aviso" libera o horário

Publicado em **19/10/2026**, autorizado pelo proprietário para publicação imediata.

## Relato de origem

A paciente das 18:00 desmarcou em cima da hora. A psicóloga marcou
"faltou sem aviso", para cobrar. Depois, ao reagendar outro paciente das
17:00 para as 18:00, o painel recusou: *"Este horário conflita com outra
consulta (sobreposição de sessão)"*.

## Causa

A marcação de presença responde a duas perguntas diferentes, e a ocupação
estava presa à regra de cobrança:

| Estado | Cobra? | Libera o horário — antes | depois |
|---|---|---|---|
| Cancelada | não | sim | sim |
| Avisou que não vem (`missed_with_notice`) | não | sim | sim |
| **Faltou sem aviso** (`missed_no_notice`) | **sim** | **não** | **sim** |
| Compareceu / pendente | sim | não | não |

A mesma regra decidia o que o agente oferece. Por isso cada falta cobrada
virava um horário morto, tanto para a psicóloga quanto para o bot.

## O que mudou

`ATTENDANCE_LIBERA_HORARIO` em `database.py` passou a ser a fonte única dos
estados em que a sessão não acontece. Ela é usada nos dois pontos que decidem
ocupação:

- `has_conflict`: criar e reagendar pelo painel;
- disponibilidade em `calendar_service`: os horários que o agente oferece.

**Ficaram intactos, de propósito:** a cobrança (faltou sem aviso continua
cobrável), as estatísticas, a sincronização com Google Agenda/CalDAV e o envio
de confirmações.

## Versão

| | |
|---|---|
| Commit | `c5135904e259573c6160c5f7577045ef634d8435` |
| Base | `9b2c4bf72` (`origin/main`), fast-forward |
| Conteúdo | `database.py` + `calendar_service.py` — 21+/9− |
| No branch do R2 | `d6ee5670a` (a mesma correção, aplicada dentro do `find_slots` reescrito) + `d2de32622` (teste de regressão) |

## Evidência antes de publicar

- **Checks em banco fictício: 15/15** no código corrigido. No código de
  produção anterior, os dois checks de comportamento **falham**, reproduzindo o
  relato. Pendente, compareceu, cancelada e cobrança passam nos dois.
- **Check de controle:** o horário testado é tirado da própria grade do agente,
  e um check prova que ele é ofertado quando vazio. A primeira versão do teste
  usava 18:00 fixo, que a grade daquele dia nunca gerava (ela avança em passos
  da duração da sessão a partir do relógio). Com isso, "o bot não oferece"
  passava sem provar nada. Foi corrigido antes de qualquer conclusão.
- **Suíte do R2: 324 testes, 0 falhas** (316 anteriores + 8 novos). Contra o
  código sem a correção, o teste novo falha exatamente nos casos certos.

## Publicação

| | |
|---|---|
| Push | `9b2c4bf72..c5135904e` em `main`, 17:12:42Z |
| Deployments | `railway-app[bot]` → **só** Agente de Atendimento / production; `vercel[bot]` → frontend do cripto |
| Bot de cripto | **nenhum deployment** |
| Processo novo | 17:15:22Z, `uptime 23,4 s`, `db: ok`, 8 consultórios |
| Código no ar | conferido **dentro do contêiner**: constante presente, `has_conflict` e `calendar_service` usam a constante e não há string antiga solta |
| Regressão | landing do R1 intacta; `/`, `/p/nutricao`, `/termos`, `/privacidade` em 200 |

## Janela de indisponibilidade — medida, não suposta

Desta vez **uma checagem do `/healthz` falhou** durante a troca:

```
17:14:51Z  processo antigo responde
17:15:12Z  sem resposta (timeout de 10 s)
17:15:22Z  processo novo responde, uptime 23,4 s → subiu ~17:14:59Z
```

O processo antigo parou antes de o novo estar pronto para atender. A janela
ficou entre **~10 e ~30 s**. No R1 nenhuma checagem falhou, mas o intervalo
era de 15 s, sem resolução para afirmar zero, como registrado na época.

Consequência prática: uma mensagem de WhatsApp que chegasse nesses segundos
pode ter se perdido (a Evolution não reenvia). Para os próximos releases, a
regra de publicar **fora de 7h–22h** deixa de ser só conservadorismo: há
indisponibilidade real e medida na troca. É coerente com o volume montado,
porque dois contêineres não compartilham o mesmo volume ao mesmo tempo.

## Confirmações para consultas marcadas como falta — decidido: manter

`get_pending_confirmations_for_tomorrow` continua pedindo confirmação para
consultas marcadas como "faltou sem aviso". Se a psicóloga registrar hoje a
falta de uma consulta de amanhã, o agente envia a essa paciente "confirme sua
consulta de amanhã".

A recomendação técnica era excluir esse caso. Em 19/10 o proprietário
**decidiu manter o comportamento atual**. Nenhuma mudança de código. Fica
registrado aqui para que não volte a aparecer como defeito em auditoria futura.
