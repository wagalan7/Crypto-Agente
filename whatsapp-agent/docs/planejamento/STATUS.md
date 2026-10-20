# Status — atualizado em 20/10/2026 (E08B pronta localmente)

Plano v1.0 instalado em `whatsapp-agent/docs/planejamento/`, com revisões
**v1.1–v1.5** registradas em `REVISOES.md` (datas da baseline mantidas).
Em produção: `20b6d3051` = R1 (landing, 14/10) + R3 (horário de quem faltou,
19/10) + R4 (layout da E08A, 19/10) + R1b ("40% menos faltas" retirado da
landing, 19/10) + R5 (identidade nas páginas de venda, E27A, 20/10). Nenhuma
**etapa** do plano publicada por inteiro — R1, R1b e R3 são releases avulsos.
E08A (R4) e E27A (R5) publicadas; E27A aprovada visualmente antes do push.
**E08B** (boletim "Psicologia em foco", requisito novo, v1.5): **PRONTA
LOCALMENTE** no branch `etapa/e08b-noticias`; não homologada, não publicada.
Pendente: avaliação do proprietário (E08B), conferência no ar (R4 e R5).
Próxima etapa: E04.

## Resumo compacto

- **E01:** plano instalado, auditoria confrontada (17 confirmados, 7 refutados, 9
  novos), homologação isolada preparada, defeito de dia errado reproduzido.
- **E02:** suíte de **221 testes, 0 ignorados**, criada antes de cada correção
  (39 falhas na 1ª rodada, 28 na revisão de ofertas) e CI preparado; **defeito do
  dia errado corrigido** — pedido de segunda não grava quarta, e uma segunda
  válida fora da amostra passa a ser encontrada; alegações sem prova retiradas
  **localmente** do material comercial.
- **E02 — revisão de ofertas:** a lista numerada que o paciente vê passou a ser
  **montada pelo servidor** a partir da mesma sequência que ele persiste. Antes o
  texto vinha do modelo e os IDs de outra lista: ordem invertida ou ID descartado
  faziam o "1" do paciente reservar outra opção. A escolha numérica é lida do
  texto do paciente e **manda sobre o `slot_id` do modelo**; sem oferta válida,
  esclarece e não grava. Os 3 testes antes ignorados foram eliminados.
- **E03:** recebimento durável (evento gravado **antes** do 2xx; a Evolution não
  tinha deduplicação alguma), serialização por contato com posse por lease,
  **identidade de evento em todas as mutações** do fluxo de entrada, retomada por
  estágio sem recalcular decisão nem reaplicar ação, **outbox** com seis estados
  e ofertas que só valem depois do aceite do provedor. Consulta e ações de
  suporte sob MASTER_KEY. **298 testes, 0 ignorados.**
- **E03 — duas decisões corrigidas na revisão:** (1) "idempotente por valor" não
  bastava para confirmar/cancelar/remarcar — um retry tardio reaplicava decisão
  antiga depois de uma ação posterior; (2) os ~30 envios diretos não eram
  seguros só por terem flags — inventário e janelas agora registrados, com etapa
  responsável para cada um.
- **Correção no registro intermediário:** o procedimento de reversão que ele
  sugeria (`git checkout`) apagaria E01/E02/E03, porque nenhuma foi commitada.
  Substituído por snapshot verificável em `homologacao/ROTEIRO_E03.md` §4.
- **E03 — revisão final (14/10):** duas correções. (1) Sem id do provedor, o
  sistema **deixou de executar mutação**: antes "declarava a limitação" e
  agendava nos dois eventos de uma reentrega. Agora recebe, responde, consulta e
  esclarece, mas recusa criar/remarcar/confirmar/cancelar e a porta de contrato,
  com pendência registrada. (2) Falha ao preparar aviso **não conclui mais o
  evento** como `done` — volta à fila, e a retomada produz exatamente um registro
  de outbox por intenção. Três avisos compartilhavam a mesma chave de
  idempotência e um deles sumiria; as tags passaram a ser distintas.
  **316 testes, 0 ignorados.**
- **Capacidade:** proprietário informou disponibilidade **diária** → B1 encerrado,
  baseline de 24 semanas **confirmada**, datas inalteradas.
- **Correção de registro:** o achado N4 de E01 (`/health` inexistente) estava
  **errado** — a rota existe. Retificado em `EVIDENCIAS/01.md` e `02.md` §5.1.
- Bloqueios abertos: 7 (B2–B9 menos B1). **B3 (contas externas) é o maior risco
  de calendário** e permanece sem confirmação.

Detalhes: `EVIDENCIAS/01.md`, `EVIDENCIAS/02.md`, `REVISOES.md`.

## Estados por etapa

PENDENTE → EM EXECUÇÃO → PRONTA LOCALMENTE → VALIDADA EM HOMOLOGAÇÃO → PUBLICADA.
Bloqueio é marcador separado com causa/responsável e impacto no calendário. CI
verde não substitui homologação.

| Etapa | Estado | Bloqueio |
|---|---|---|
| E01 | PRONTA LOCALMENTE | — |
| E02 | PRONTA LOCALMENTE | B2, B3 para avançar a VALIDADA EM HOMOLOGAÇÃO |
| E03 | PRONTA LOCALMENTE | B2, B3 para avançar a VALIDADA EM HOMOLOGAÇÃO |
| **E08A** (antecipação parcial de E08) | **PUBLICADA** em 19/10 como R4 (`b7397719`) | confirmação visual do proprietário no próprio painel |
| **E27A** (antecipação só visual de E27) | **PUBLICADA** em 20/10 como R5 (`78a1718d` + `20b6d305`) | confirmação visual do proprietário no ar |
| **E08B** (requisito novo: boletim por segmento) | **PRONTA LOCALMENTE** — `etapa/e08b-noticias`, 83 testes; integrada ao R2 em `integracao/r2-main-e08b` (407/407) | avaliação do proprietário; saída HTTPS da Railway não verificada |
| E04–E28 | PENDENTE | — (E08 e E27 parcialmente adiantadas por E08A e E27A, **não** concluídas) |

E04 e E27 **não** estão concluídas: receberam apenas transferência parcial de
conteúdo para E02 (ver `REVISOES.md`).

**Direção visual (v1.2, 19/10/2026):** identidade vinho/rosé/nude/off-white
aprovada pelo proprietário e registrada em `identidade-visual/`. É insumo de E08
(tokens, logo preparada, componentes) e de E27 (página de vendas). Nenhum estado
ou data foi alterado; nada foi aplicado ao app.

## Registro por entrega

### E01 — Instalar plano e conferir realidade

| Campo | Valor |
|---|---|
| Revisão | `1b723a8f` (build `2026-07-16-67`) |
| Critérios | Todos PASSARAM; "homologação preparada" PASSOU parcialmente (7 integrações NÃO VERIFICADAS) |
| Arquivos | Nenhum da aplicação. Só `docs/planejamento/**` e `whatsapp-agent/CLAUDE.md` |
| Revisão humana | Aceita implicitamente ao autorizar E02 |

### E02 — Regressões, CI e proteção de publicação (+ antecipação v1.1)

| Campo | Valor |
|---|---|
| Revisão | parte de `1b723a8f`; alterações **não commitadas** |
| Reprodução falha antes / passa depois | **PASSOU** — 39 falhas → 0 |
| Pedido segunda não grava quarta | **PASSOU** |
| Segunda válida além dos primeiros 24 slots é encontrada | **PASSOU** |
| Ausência de vaga/ID ou ambiguidade não altera agenda | **PASSOU** |
| Escolha numérica só com oferta e mapeamento exato | **PASSOU** (inclui renumeração, oferta vencida, slot ocupado no meio-tempo, oferta consumida) |
| Limites de dia, virada de mês/ano, períodos, ID incompatível | **PASSOU** |
| Fuso | **PASSOU como limitação documentada** (`America/Sao_Paulo` fixo; sem fuso por consultório/paciente) |
| Modalidade | **NÃO VERIFICADA** — não existe no modelo de dados; é E04 |
| Confirmação canônica sem reescrita de texto | **PASSOU** |
| Concorrência | **PASSOU no caminho afetado** (oferta). Concorrência de escrita com duas conexões: **não coberta**, é E04 |
| Regressões relacionadas e checks (E02) | **PASSOU** — 221 testes, **0 ignorados**; `compileall`; Jinja de 15 templates; `node --check` nos 12 templates com JS embutido |
| Texto exibido = oferta persistida | **PASSOU** — `test_oferta_coerencia.py` (34 casos) |
| "1" aponta para a opção 1 exibida, ou não grava | **PASSOU** — inclui `slot_id` contraditório, oferta ausente/vencida/substituída, fora da faixa |
| Oferta gravada antes do envio | **NÃO RESOLVIDO, registrado** — `delivered` + log; fechar exige outbox = **requisito de E03** |
| CI preparado | **PASSOU** — workflow escopado; YAML validado; passos de shell executados localmente. Execução no GitHub: **NÃO VERIFICADA** |
| Configuração externa | **NÃO VERIFICADA** — Railway e GitHub não acessados |
| Alegações sem prova retiradas localmente | **PASSOU** — com guarda automatizada |
| Mudança de planejamento registrada | **PASSOU** — `REVISOES.md` v1.1, datas mantidas |
| Nenhum commit/push/deploy | **PASSOU** |
| Testes executados | `pytest` (183), `compileall`, `node --check`, reprodução E01, seed de homologação, validação do YAML |
| Arquivos alterados | 7 de aplicação (+550 −152): `agent.py`, `calendar_service.py`, `database.py`, `main.py`, `landing_content.py`, `templates/landing.html`, `railway.toml` |
| Arquivos novos | `tests/` (9), `pytest.ini`, `requirements-dev.txt`, workflow de CI, `REVISOES.md`, `PROTECAO_PUBLICACAO.md`, `ROTEIRO_E02.md`, `EVIDENCIAS/02.md` |
| Migração / reversão | `slot_offers` **aditiva** (`CREATE TABLE IF NOT EXISTS`); reversão por `git revert` sem passo de banco — `ROTEIRO_E02.md` §4 |
| Baseline de desempenho | `find_slots` 3,9 ms · `has_conflict` 0,5 ms · oferta 0,6 ms (local; **não** é capacidade de produção) |
| Revisão humana | Pendente: aceite de E02 |
| Riscos | (1) Sem `offered_ids` do modelo, resposta numérica pede esclarecimento em vez de reservar — mais atrito, menos risco. (2) O texto da lista passou a ser do servidor: o modelo perde liberdade de redação nesse trecho. Acompanhar (`ROTEIRO_E02.md` §3) |
| Gate comercial | 🔴 O site publicado **ainda exibe** os depoimentos fabricados: a retirada é local e só vale após deploy |

## Bloqueios abertos

| # | Assunto | Responsável | Necessário até |
|---|---|---|---|
| ~~B1~~ | ~~Capacidade semanal~~ | — | **ENCERRADO 12/10** — disponibilidade diária informada |
| B2 | Gatilho de publicação; conferir config efetiva na Railway e proteção de branch no GitHub | proprietário | antes do primeiro commit/deploy |
| B3 | Contas de homologação (WhatsApp de teste, sandbox de pagamento, SMTP, bucket, chave de IA para avaliar o modelo) | proprietário | semana 2–3, para não travar R1 |
| B4 | Depoimentos: remover do ar (exige deploy) ou substituir por prova real autorizada | proprietário | antes de qualquer venda nova |
| B5 | Regras de agenda e modalidade | proprietário | antes de E04 |
| B6 | Papéis e o que cada um não pode ver | proprietário | antes de E07 |
| B7 | Definições de financeiro | proprietário | antes de E13 |
| B8 | Modelos documentais e revisão profissional | proprietário | antes de E15 |
| B9 | Ajuste da promessa comercial | proprietário | antes de R1 |

### E03 — Recebimento durável e envio rastreável

| Campo | Valor |
|---|---|
| Persistência antes do ACK | **PASSOU** — nos três webhooks; falha de gravação não devolve "queued" |
| Deduplicação | **PASSOU** por `(tenant, provedor, id)`. Sem id do provedor: **não deduplicável**, declarado e visível, não disfarçado |
| Serialização por contato | **PASSOU** — contatos e consultórios independentes |
| Posse por lease na mutação | **PASSOU** — worker sem posse não altera a agenda |
| Idempotência de todas as mutações | **PASSOU** — criar, remarcar, confirmar, cancelar, gravar conversa |
| Retomada sem repetir ação | **PASSOU** — decisão reaproveitada; confirmação idêntica entre tentativas |
| Outbox com seis estados | **PASSOU** — incerto não reenvia às cegas |
| Ofertas correlacionadas por identidade | **PASSOU** — desfecho atrasado de A não marca B |
| Elegibilidade conservadora | **PASSOU** — não enviado/incerto/falho bloqueiam; ambiguidade esclarece |
| Falha mantém a oferta anterior | **PASSOU** |
| Suporte sob autorização | **PASSOU** — 403 sem chave, motivo obrigatório, auditado |
| Migração desde E02 | **PASSOU** — cópia fictícia; nada removido |
| Reversão de dados | **ANALISADA**, não declarada segura: resíduos em `inbound_events`/`outbox`/`offers` descritos no roteiro |
| Nenhuma falha inexplicada | **PASSOU** — 316 testes, 0 ignorados |
| Ausência de id não autoriza mutação | **PASSOU** — 4 ações bloqueadas + porta de contrato; consulta liberada |
| Falha de aviso não some em evento `done` | **PASSOU** — antes do 1º aviso, depois do enfileiramento e exceção interna |
| Retomada não duplica efeito | **PASSOU** — exatamente 1 registro de outbox por intenção |
| Efeito externo de calendário | **PASSOU** — pendência durável, sem reexecução às cegas |
| Integração externa | **NÃO VERIFICADA** (B2, B3) |
| Nenhum commit/push/deploy | **PASSOU** |
| Efeitos fora da garantia | Mídia (**E05**), jobs do agendador (**E06**), avisos de pagamento (**E06** validação / **E14** consolidação), Twilio sem autenticação (**E06**), atomicidade de conflito (**E04**), escala horizontal (**E26**/**E28**) |

## R1 — landing · **PUBLICADO em 14/10/2026**

Confirmado: push `80d7ece81..885477d25`; deployment só no serviço do Agente;
processo novo no ar às 18:36:04Z com `db: ok` e 8 consultórios; verificação em
produção registrada em `RELEASE-R1-LANDING.md` §10. O texto abaixo é o estado de
**antes** da publicação, mantido como histórico.

## R3 — "faltou sem aviso" libera o horário · **PUBLICADO em 19/10/2026**

Confirmado: push `9b2c4bf72..c5135904e`; deployment só no serviço do Agente;
código conferido dentro do contêiner. Detalhes e a janela de indisponibilidade
medida (~10–30 s) em `RELEASE-R3-HORARIO-LIVRE.md`.

## R1 — landing (estado antes da publicação, histórico)

Worktree isolado, branch `release/r1-landing`, commit **`885477d2`** sobre o
`origin/main` **atual** (`80d7ece8`). Dois arquivos, 29+/40−: só a landing.
Detalhes em `RELEASE-R1-LANDING.md`.

- **Gatilho do cripto: RESOLVIDO.** O registro de deployments do GitHub mostra
  que commits que tocam só `whatsapp-agent/` acionam **apenas** o serviço do
  Agente; os de cripto acionam apenas Crypto Win PRD/DEV. Só o merge commit de
  02/10 subiu os três. O R1 é fast-forward e toca só `whatsapp-agent/backend/`.
- **Bloqueador restante: volume em `/data`** não confirmado (V-B). Vale para
  qualquer deploy, não só o R1.
- Checks do R1: **18/18**, rodados contra os arquivos exatos do commit.
- `main` não é protegida e não há ruleset; 0 workflows no repositório.
- Restauração de backup: **PENDENTE** — backup existente não é restauração
  validada.

## Release incremental 1 — E01–E03 (reposicionado como R2)

Branch local `release/agente-consultorio-e01-e03`, 6 commits sobre `1b723a8f`.
Carimbo preparado `2026-10-14-68`. **Nada publicado.** Detalhes, procedimento,
rollback e bloqueadores em `RELEASE-E01-E03.md`.

| Gate | Situação |
|---|---|
| G1 implementação local | ✅ 316 testes, checks verdes, snapshot verificável |
| G2 homologação | ✅ 26/26 no roteiro ponta a ponta com provedor simulado; integrações reais **NÃO VERIFICADAS** |
| G3 aprovação | ⏸ **4 bloqueadores 🔴** — volume em `/data`, serviços de cripto no mesmo repo, backup pré-deploy, configuração efetiva da Railway |
| G4 publicação | — |
| G5 observação | — |

~~**R2 precisa de rebase** sobre o `origin/main` atual (hoje está 17 commits
atrás).~~ **Resolvido por merge em 20/10:** `integracao/r2-main-e08b` (R2 + `main` +
E08B), 407 de 407 testes (`EVIDENCIAS/08B.md` §13). **Menor release coerente:** E02 **não** é publicável sozinha (a camada de
ofertas foi reescrita sobre o esquema de E03 e as funções de E02 foram
removidas). O que é separável de verdade é a **correção da landing** (commit 1),
autossuficiente e de risco mínimo. Recomendação: R1 = landing + docs + testes;
R2 = código de E02+E03 em janela combinada.

**Impacto nas datas:** nenhum. A baseline v1.0 segue intacta; R1 do cronograma
(piloto, 08/11) continua dependendo de B3.

## Próximas ações

1. ✅ Proprietário pediu o release visual (19/10) — **R4 preparado**:
   commit `b73977197`, fast-forward sobre `c5135904e`, 5 arquivos
   (`RELEASE-R4-VISUAL-E08A.md`).
2. ✅ **R4 publicado** em 19/10, 21h12 BRT; verificação pós-deploy ok, com os 5
   arquivos idênticos ao commit dentro do contêiner. **Falta:** o proprietário
   confirmar visualmente no próprio painel.
3. Se a avaliação e os ajustes passarem de 25/10, registrar o impacto em E04
   (`REVISOES.md` v1.3).
4. Depois, enviar `prompts/04.txt`. **E04 não foi iniciada.**
5. Seguem abertos: **B3** (contas de homologação) e os bloqueadores do R2
   (`RELEASE-R1-LANDING.md` §9, incluindo `INBOX_NUDGE_ON_WEBHOOK`).
6. ✅ Resolvidos desde 14/10: conferência da Railway (volume, filtros, variáveis —
   `RELEASE-R1-LANDING.md` §3) e **B4** (a landing corrigida está no ar pelo R1
   **e pelo R1b**: o R1 deixou "40% · Menos faltas sem aviso" na faixa de
   números, retirado em 19/10, 23h33 BRT — `RELEASE-R1-LANDING.md` §11).
7. **E27A** (aplicação só visual da identidade à página de vendas) antecipada
   por decisão do proprietário (19/10, `REVISOES.md` v1.4) — **pronta
   localmente** em 20/10: commits `78a1718dc` + `20b6d3051`, fast-forward sobre `1986bdef0`;
   texto, oferta e preços idênticos nas 13 páginas; capturas antes/depois e
   scripts em `EVIDENCIAS/27A/`. ✅ Aprovada pelo proprietário e **publicada como
   R5** em 20/10, 00h24 BRT. Produção idêntica à prévia nas 13 páginas e
   ~19 s sem resposta na troca de contêiner (`RELEASE-R5-VISUAL-LANDING.md`).
8. Textos encontrados na E27A para decisão do proprietário em E27 (meta
   "Reduza faltas", "Os pacientes não percebem que é automático", "2 minutos",
   valores de mercado, 💜) — lista em `EVIDENCIAS/27A.md`.
9. Corrigir no R2 o ponto cego de `tests/test_landing_alegacoes.py` (busca no
   template cru, não no texto visível).
10. **E08B** pronta localmente (20/10): substitui a narrativa com IA do Início
    por "Psicologia em foco" (CFP + Ministério da Saúde, 09:00 de Brasília,
    cache por segmento). Evidências em `EVIDENCIAS/08B.md`. Posição do quadro
    aprovada pelo proprietário (20/10). Integrada ao R2 em 20/10:
    `integracao/r2-main-e08b` = R2 + `main` + E08B por merge, **407 de 407
    testes** (`EVIDENCIAS/08B.md` §13). **Falta:**
    avaliação do proprietário; integração com o R2; publicação só com
    autorização. **Não avançar para outra etapa automaticamente.**
