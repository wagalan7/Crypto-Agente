# 04 — Secretário Virtual

> Evidências: `backend/calendar_service.py`, `google_calendar_service.py`,
> `caldav_service.py`, `scheduler.py`, `agent.py`, `database.py`.

---

## Conclusão central

**O produto não é um secretário virtual. É um agendador de consultas por WhatsApp.**

As funcionalidades de secretaria que existem são todas **voltadas ao paciente**
(agendar, confirmar, remarcar, cobrar). As funcionalidades que caracterizam um secretário
executivo — receber ordens do dono em linguagem natural, gerenciar tarefas, organizar
reuniões, produzir resumos — **não existem**.

Busca por `task|tarefa|todo|resumo do dia|briefing|secretari` nos 26 módulos retorna
apenas falsos positivos (`BackgroundTasks`, "todos os pacientes"). **Não há nenhum
módulo, tabela ou rota de secretaria executiva.**

---

## 1. Inventário

| Funcionalidade | Situação | Evidência |
|---|---|---|
| **Google Calendar** | ✅ Ponta a ponta | `google_calendar_service.py`, OAuth `/google/auth/{slug}` → `/google/callback`; eventos criados/atualizados/removidos |
| **CalDAV** (iCloud, Fastmail) | ✅ Ponta a ponta | `caldav_service.py`; teste em `/dashboard/api/caldav/test` |
| **Consulta de disponibilidade** | ✅ Ponta a ponta | `calendar_service.get_available_slots`, respeita `working_days`, `working_hours`, `blocked_hours`, `blocked_hours_by_day`, `blocked_dates`, `session_minutes` |
| **Criar compromisso** | ✅ Ponta a ponta | `Action.create` → `db.create_appointment` + sincronização externa |
| **Alterar compromisso** | ✅ Ponta a ponta | `Action.update` / `_do_reschedule` |
| **Cancelar compromisso** | ✅ Ponta a ponta | `Action.cancel` → cancelamento suave + `cancellation_log` |
| **Detecção de conflito** | 🟡 Parcial | `has_conflict` existe, mas **sem índice único e sem transação** → corrida possível |
| **Fuso horário** | 🟡 Parcial | `America/Sao_Paulo` **fixo** no código. Não há fuso por cliente nem por paciente |
| **Lembretes automáticos** | ✅ Ponta a ponta | `scheduler._run_confirmations` (~24h antes) |
| **Confirmação de presença** | ✅ Ponta a ponta | Mensagem automática + atalho determinístico na resposta |
| **Reagendamento** | ✅ Ponta a ponta | Pelo paciente no WhatsApp e pela profissional no painel |
| **Gestão de contatos** | 🟡 Parcial | `patients` com nome, telefone, preço, e-mail, CPF, endereço. Sem tags, notas ou segmentação |
| **Cadastro/atualização de informações** | ✅ | Painel + agente aprendem o nome; `rename_patient_everywhere` |
| **Listas de tarefas** | ❌ **Não existe** | Nenhuma tabela ou rota |
| **Alertas e notificações** | 🟡 Parcial | Avisos à profissional por WhatsApp em alguns eventos; **sem alerta de falha do sistema** |
| **Resumos de compromissos** | 🟡 Parcial | "Home Inteligente" no **painel** (`insights_service.py`). **Não há resumo enviado por WhatsApp** |
| **Organização de reuniões** | ❌ **Não existe** | Sem convite a múltiplos participantes, sem busca de horário comum |
| **Solicitações em linguagem natural (do dono)** | ❌ **Não existe** | O agente só atende o **paciente**. Mensagens da própria profissional são ignoradas (`fromMe`) |
| **Comandos por áudio** | ❌ **Não existe** | `transcribe_audio_groq` nunca é chamada |
| **Rotinas recorrentes** | 🟡 Parcial | O agendador tem rotinas fixas (confirmação, cobrança, backup). O **cliente não pode criar** rotinas |
| **Ações que exigem confirmação humana** | 🟡 Parcial | Comprovante de pagamento exige confirmação manual (bom). Cancelamento e remarcação **não** exigem |
| **Recuperação após falha de integração** | 🟡 Parcial | Google/CalDAV *fail-open* com fallback de criação de evento. Sem reprocessamento de mensagens perdidas |

---

## 2. O que funciona de ponta a ponta

1. Paciente pede horário → agente consulta disponibilidade real → oferece → agenda →
   grava no banco → cria evento no Google/CalDAV → confirma por mensagem
2. 24h antes → confirmação automática → paciente responde "sim" → presença confirmada
   sem passar pela IA
3. Paciente pede para remarcar → novo horário → consulta movida → evento externo
   atualizado → marcadores de lembrete zerados para o novo horário
4. Fim do mês → cobrança calculada por sessões realizadas → enviada com PIX → registrada
5. Contrato enviado → lembretes → assinatura eletrônica → PDF

---

## 3. O que é parcial

**Detecção de conflito.** `has_conflict` cobre sobreposição considerando duração, ignora
canceladas e `missed_with_notice`. Mas verificação e gravação são operações separadas, sem
transação e **sem índice único** — duas reservas simultâneas no mesmo horário passam.

**Fuso horário.** Tudo assume `America/Sao_Paulo`. Já há **paciente em Portugal**
(confirmado pelo cliente durante a auditoria). Esse paciente receberá confirmação
calculada no fuso de São Paulo, com diferença de 4–5 horas. 🔴 **Defeito real já em
produção.**

**Resumos.** Os insights existem, mas só no painel. Um secretário mandaria o resumo do dia
no WhatsApp às 7h — isso não existe.

**Alertas.** A profissional é avisada de cancelamento e de imagem não classificada. Não é
avisada quando **o agente para de funcionar** — o incidente de 7 dias comprova.

---

## 4. O que não existe

- Lista de tarefas / lembretes pessoais do profissional
- Comandos do dono por texto ou áudio ("remarca a Maria pra quinta")
- Organização de reuniões com terceiros
- Resumo diário/semanal por WhatsApp
- Rotinas configuráveis pelo cliente
- Integração com e-mail do cliente
- Triagem de recados
- Fuso horário por paciente

---

## 5. Esforço para virar "secretário virtual"

| Capacidade | Esforço | Dependências |
|---|---|---|
| Resumo diário por WhatsApp | **Baixo** | `insights_service` já calcula; falta job + template |
| Alerta de falha ao operador | **Baixo** | `instance_monitor` + `email_service` já existem |
| Comandos do dono por texto | **Médio** | Novo caminho: identificar `fromMe`/número da profissional, prompt próprio, ações administrativas |
| Transcrição de áudio | **Médio** | Função existe; falta extrair URL da mídia na Evolution e ligar o fluxo |
| Lista de tarefas | **Médio** | Tabela + CRUD + painel + comandos |
| Fuso horário por paciente | **Médio** | Coluna + propagação em todo cálculo de slot e lembrete |
| Organização de reuniões | **Alto** | Múltiplos participantes, disponibilidade cruzada, convites |

**Recomendação:** **não vender "secretário virtual" nesta semana.** Vender "atendente e
agendador de WhatsApp com IA", que é o que o produto entrega bem. A expressão secretário
pode ser reintroduzida depois dos itens de esforço baixo e médio.
