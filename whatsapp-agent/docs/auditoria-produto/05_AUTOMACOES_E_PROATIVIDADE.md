# 05 — Automações e Proatividade

> Evidências: `backend/scheduler.py` (779 linhas), `billing_reminders.py`,
> `instance_monitor.py`, `contract_service.py`, `database.py`.

---

## 1. O agente consegue agir sem nova mensagem do usuário?

**Sim.** Existe um agendador real que envia mensagens por iniciativa própria. Essa é uma
das partes mais maduras do produto.

`scheduler.start_scheduler()` cria uma **thread daemon** com laço a cada **30 minutos**.
Janela de envio: **7h às 22h** (`_SEND_AFTER_HOUR=7`, `_SEND_BEFORE_HOUR=22`) — fora dela
nada é enviado, o que é correto para não incomodar paciente de madrugada.

---

## 2. Inventário das automações

| Automação | Gatilho | Situação | Evidência |
|---|---|---|---|
| **Confirmação ~24h antes** | sessão no dia seguinte, respeita `confirmation_hour` | ✅ Funciona | `_run_confirmations` |
| **Follow-up no dia** | manhã do dia da sessão | ✅ Funciona | mesmo job, `_FOLLOWUP_HOUR=7` |
| **Cobrança mensal** | último dia do mês, 20h | ✅ Funciona | `_run_billing` |
| **Cobranças avulsas** | junto da mensal | ✅ Funciona | `_send_manual_billing_entries` |
| **Backup diário** | 3h | ✅ Funciona | `_run_backup` → S3/R2/B2 |
| **Lembrete de vencimento (infra + instância)** | 9h configurável | ✅ Funciona | `_run_billing_reminders` |
| **Lembretes de contrato (1º, 2º) e expiração** | prazos por consultório | ✅ Funciona | `_run_contract_reminders` |
| **Monitor de instância WhatsApp** | ~10 min | 🟡 Grava, **não alerta** | `instance_monitor.py` |
| **Aviso de falha de envio ao dono** | falha nas confirmações | 🟡 Parcial | `_notify_owner_failures` |
| **Recuperação de carrinho abandonado** | manual pelo painel | 🟡 Só manual | `/painel/api/abandoned-carts/{slug}/send-reminder` |
| **Follow-up comercial / recuperação de lead** | — | ❌ Não existe | — |
| **Resumo diário ao profissional** | — | ❌ Não existe | — |
| **Alerta de agente fora do ar** | — | ❌ **Não existe** | lacuna crítica |

---

## 3. Prevenção de duplicidade

Bem resolvida, em várias camadas:

| Mecanismo | Onde | O que evita |
|---|---|---|
| `webhook_seen` (tabela) | webhook | Reprocessar retry do provedor, **sobrevive a restart** |
| `confirmation_sent`, `followup_sent` | `appointments` | Enviar a mesma confirmação duas vezes |
| `bill_reminders_log` com `UNIQUE(kind, ref_id, due_date)` | lembretes de vencimento | Lembrete repetido |
| `contract_reminders_log` com `UNIQUE(contract_id, kind)` | contrato | Lembrete repetido |
| `billing_manual_entries.sent_at` | cobrança avulsa | Cobrar duas vezes |
| `billing_logs` por mês | cobrança | Cobrança duplicada no mês |
| `_dedup_failures` | notificação ao dono | Avisos repetidos |

**Avaliação: 🟢 este é um dos pontos mais sólidos do sistema.** O relógio de cada job é
derivado do estado no banco, não de memória — reiniciar o processo não duplica envios.

---

## 4. Recuperação de execuções interrompidas

| Cenário | Comportamento |
|---|---|
| Processo reinicia entre jobs | ✅ Retoma pelo estado do banco |
| Job falha no meio | ✅ *Fail-open* por tenant; erro logado, laço continua |
| Falha de envio de uma confirmação | 🟡 Marcada como falha e o dono é avisado; **sem nova tentativa automática** |
| Mensagem recebida perdida (processo morre no `BackgroundTask`) | ❌ **Perdida.** O `webhook_seen` impede o retry do provedor de reprocessar |

🔴 O último caso é um defeito de desenho: a deduplicação é gravada **antes** do
processamento concluir, então uma falha no meio do caminho produz perda silenciosa.
**NÃO VERIFICADO** com que frequência ocorre.

---

## 5. Disparos manuais disponíveis

- `POST /admin/confirmacoes/disparar` — forçar confirmações
- `POST /dashboard/api/billing/run` — rodar cobrança do mês
- `POST /painel/api/billing-reminders/run`
- `POST /painel/api/instances/check`
- `POST /master/backup/run`

Boa prática: toda rotina automática tem equivalente manual para suporte.

---

## 6. Restrições da API do WhatsApp

**Ponto decisivo para a estratégia comercial.**

O produto **não usa a API oficial** (WhatsApp Business Platform / Cloud API). Usa
**Evolution API**, que opera sobre **Baileys** — biblioteca não-oficial que se conecta
como "aparelho vinculado" (WhatsApp Web).

### Consequências

| Tema | API Oficial | **O que vale aqui** |
|---|---|---|
| Janela de 24h | Obrigatória | ❌ Não se aplica |
| Templates aprovados | Obrigatórios fora da janela | ❌ Não se aplica |
| Custo por conversa | Cobrado pela Meta | ✅ Sem custo por mensagem |
| Envio proativo | Só template aprovado | ✅ Livre |
| Risco de banimento | Baixo | 🔴 **Real** — número pode ser bloqueado |
| Estabilidade | Alta (SLA) | 🔴 **Baixa** — quebra quando o WhatsApp muda o protocolo |
| Continuidade | Garantida | 🔴 Depende de projeto open-source de terceiro |

**Portanto:** o agendador pode disparar confirmação e cobrança livremente — vantagem real
de custo e flexibilidade. Em troca, assume-se risco de bloqueio do número e de
indisponibilidade.

### Evidências do risco, já observadas nesta operação
1. **Sessão travada por 7 dias** (09/09→16/09) — conexão aparentava "aberta" enquanto o
   canal estava morto; nenhuma mensagem chegava. Só resolveu reiniciando o processo da
   Evolution.
2. **Evolution em v2.3.7**, última estável de dez/2025. Não há versão estável nova; a
   2.4.0 está em *release candidate* e **passa a exigir licença**.
3. **Imagem `latest`** — um redeploy pode trocar a versão sem aviso.

**Recomendação comercial:** declarar no contrato que o serviço depende de conexão não
oficial do WhatsApp, sem garantia de disponibilidade contínua, e avaliar a API oficial
como plano B para clientes maiores.

---

## 7. Consentimento e autorização

| Aspecto | Situação |
|---|---|
| Aceite de termos no onboarding | ✅ `accepted_terms_at`, `accepted_terms_version` |
| Consentimento do **paciente** para receber mensagens | ❌ **Não existe registro** |
| Opt-out do paciente ("não quero mais receber") | ❌ Não implementado |
| Pausa por contato | ✅ `agent_paused` (mas é ação da profissional, não do paciente) |

🟠 **Risco LGPD:** o paciente passa a receber mensagens automáticas sem consentimento
registrado. O agente **não reconhece pedido de descadastro** — se o paciente escrever
"pare de me mandar mensagem", não há tratamento específico; a mensagem vai para o LLM
como qualquer outra. Ver `07_PRONTIDAO_COMERCIAL.md`.
