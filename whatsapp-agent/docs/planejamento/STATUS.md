# Status — atualizado em 14/10/2026 (E03, revisão final)

Plano v1.0 instalado em `whatsapp-agent/docs/planejamento/`, com revisão **v1.1**
registrada em `REVISOES.md` (datas mantidas). Revisão de código de partida:
`1b723a8f` (build `2026-07-16-67`). Nenhuma etapa publicada.
Etapa atual: **E03 — PRONTA LOCALMENTE**. Próxima: E04, após aceite de E03.

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
| E04–E28 | PENDENTE | — |

E04 e E27 **não** estão concluídas: receberam apenas transferência parcial de
conteúdo para E02 (ver `REVISOES.md`).

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

## Próximas ações

1. Proprietário revisa o diff de E01→E03 e decide **B2** (como publicar com
   proteção). Recomendação: **commitar E01→E03 juntas num branch** — hoje não há
   commit separando as etapas, e por isso nenhuma reversão por git é possível.
2. Decidir **B4**: a correção da landing só chega ao público com deploy.
3. Resolver **B3** (contas de homologação) — é o que trava sair de "pronta
   localmente" para "validada em homologação" em E01, E02 e E03.
4. Com o aceite, enviar `prompts/04.txt`. **E04 não foi iniciada.**
