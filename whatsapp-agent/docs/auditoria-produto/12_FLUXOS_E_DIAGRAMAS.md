# 12 — Fluxos e Diagramas

> Diagramas em Mermaid. Todos derivados da leitura do código, com os arquivos e funções
> indicados.

---

## 1. Arquitetura geral

```mermaid
flowchart TB
    subgraph Cliente final
        P["Paciente<br/>WhatsApp"]
    end
    subgraph "Railway — projeto separado"
        EV["Evolution API v2.3.7<br/>Baileys · 1 réplica<br/>⚠ ponto único de falha"]
    end
    subgraph "Railway — Agente de Atendimento"
        APP["FastAPI · main.py<br/>148 rotas · 1 réplica"]
        SCH["scheduler.py<br/>thread · 30 min"]
        DB[("SQLite<br/>26 tabelas · volume")]
    end
    subgraph Externos
        AI["Groq Llama 3.3 70B<br/>Anthropic Claude Haiku"]
        CAL["Google Calendar<br/>CalDAV"]
        PAY["Stripe · Mercado Pago"]
        OBS["Sentry · SMTP · S3/R2"]
    end
    subgraph Humanos
        PROF["Profissional<br/>/dashboard/{slug}?token"]
        OPER["Operador<br/>/painel · 2FA"]
    end

    P <--> EV
    EV -->|webhook| APP
    APP -->|sendText| EV
    APP <--> AI
    APP <--> DB
    APP <--> CAL
    APP <--> PAY
    APP --> OBS
    SCH --> DB
    SCH --> EV
    PROF --> APP
    OPER --> APP
```

---

## 2. Ciclo de vida de uma mensagem recebida

```mermaid
sequenceDiagram
    participant PAC as Paciente
    participant EVO as Evolution
    participant WH as webhook_evolution
    participant HM as _handle_message
    participant AG as agent.process_message
    participant LLM as Groq / Claude
    participant DB as SQLite

    PAC->>EVO: "quero remarcar"
    EVO->>WH: POST /webhook/{slug}/evolution
    WH->>WH: valida token (403 se inválido)
    WH->>WH: extract_message_evolution
    Note over WH: ⚠ áudio/imagem/PDF → None → descartada
    WH->>WH: filtra grupo/transmissão/status
    WH->>DB: webhook_seen (dedup)
    WH-->>EVO: 200 queued
    WH->>HM: BackgroundTask
    HM->>HM: spam? suspenso? pausado?
    HM->>AG: process_message
    AG->>AG: atalhos determinísticos
    alt Atalho casou
        AG->>DB: ação direta (sem IA)
    else Precisa de IA
        AG->>DB: contexto + últimas 8 mensagens
        AG->>LLM: prompt + contexto
        LLM-->>AG: JSON (intent, action, data, texto)
        AG->>DB: _execute_action
        AG->>AG: corrige data/hora no texto
    end
    AG-->>HM: resposta
    HM->>EVO: sendText
    EVO->>PAC: resposta
    HM->>DB: grava em conversations
```

---

## 3. Agendamento e remarcação (onde esteve o defeito da build 64)

```mermaid
flowchart TD
    A["Paciente: 'quero remarcar'"] --> B{"Informou dia/período?"}
    B -->|não| C["Pergunta a disponibilidade<br/>action: none"]
    C --> A
    B -->|sim| D["Backend monta HORÁRIOS DISPONÍVEIS<br/>lista COMPLETA numerada<br/>cada item com [id=...]"]
    D --> E["Modelo FILTRA e mostra 4-5 ao paciente<br/>renumerando de 1 a N"]
    E --> F["Paciente responde '1'"]
    F --> G{"Modelo devolve o quê?"}
    G -->|"slot_id (build 64+)"| H["_resolve_slot casa o id exato<br/>✅ horário correto"]
    G -->|"slot_index (legado)"| I["Indexa a lista COMPLETA<br/>⚠ o '1' do paciente ≠ '1' do backend"]
    H --> J["has_conflict"]
    I --> J
    J -->|livre| K["update_appointment<br/>zera confirmation_sent e followup_sent"]
    J -->|ocupado| L["'Não consegui remarcar'"]
    K --> M["Sincroniza Google/CalDAV"]
    M --> N["Resposta com horário NOVO em negrito<br/>antigo entre parênteses (build 64)"]
```

**Nota:** antes da build 64 o prompt mandava usar *"o número exato do display"*, enquanto o
backend indexava a lista completa — contradição direta que marcava o paciente em outro
horário. Hoje o `slot_id` resolve e o índice é só reserva.

---

## 4. Automações do agendador

```mermaid
flowchart LR
    T["Thread daemon<br/>laço de 30 min"] --> A{"7h ≤ hora < 23h?"}
    A -->|não| Z["Pula envios"]
    A -->|sim| B["_run_confirmations<br/>24h antes + follow-up"]
    T --> C{"último dia do mês<br/>e 20h?"}
    C -->|sim| D["_run_billing"]
    T --> E{"3h?"}
    E -->|sim| F["_run_backup → S3"]
    T --> G{"hora do lembrete?"}
    G -->|sim| H["_run_billing_reminders"]
    T --> I["_run_contract_reminders"]

    B --> J[("marcadores<br/>confirmation_sent<br/>followup_sent")]
    D --> K[("billing_logs<br/>por mês")]
    H --> L[("bill_reminders_log<br/>UNIQUE")]
    I --> M[("contract_reminders_log<br/>UNIQUE")]
```

Idempotência: cada job verifica o estado no banco antes de enviar — reiniciar o processo
não duplica mensagem.

---

## 5. Onboarding de um novo cliente

```mermaid
flowchart TD
    A["Landing /"] --> B["GET /onboarding"]
    B --> C["POST /onboarding/create<br/>status=pending_payment<br/>gera slug e tokens"]
    C --> D["GET /onboarding/pagamento"]
    D --> E{"Provedor"}
    E -->|Stripe| F["POST /checkout/stripe"]
    E -->|Mercado Pago| G["POST /checkout/mercadopago"]
    F --> H["POST /webhooks/stripe"]
    G --> I["POST /webhooks/mercadopago"]
    H --> J["status=active"]
    I --> J
    J --> K["GET /onboarding/sucesso<br/>link com dashboard_token"]
    K --> L["Painel → Config → WhatsApp"]
    L --> M{"Conectar"}
    M -->|"⚡ automático"| N["Cria instância tenant-{slug}<br/>grava webhook e token<br/>devolve QR"]
    M -->|"📲 Z-API manual"| O["Cliente cola credenciais"]
    N --> P["Lê o QR no celular"]
    O --> P
    P --> Q["Agente operando"]

    style M fill:#ffe6e6
    style O fill:#ffe6e6
```

🔴 Em vermelho o ponto de maior atrito: dois caminhos concorrentes, com botões quase
idênticos, compartilhando as mesmas três colunas do banco.

---

## 6. Contrato — ciclo completo

```mermaid
stateDiagram-v2
    [*] --> pendente: create_contract_for_patient
    pendente --> enviado: send_contract (WhatsApp)
    enviado --> enviado: lembrete 1 e 2
    enviado --> assinado: paciente assina<br/>(nome, CPF, IP, hash)
    enviado --> recusado: paciente recusa
    enviado --> expirado: passou expire_days
    expirado --> enviado: resend_contract
    recusado --> enviado: resend_contract
    assinado --> desatualizado: nova versão do modelo
    desatualizado --> enviado: reenvio
    assinado --> [*]: PDF disponível
```

---

## 7. Isolamento entre clientes

```mermaid
flowchart TD
    R["Requisição ao painel"] --> T["X-Dashboard-Token<br/>ou ?token="]
    T --> G["_get_tenant_by_token"]
    G -->|não achou| X["403"]
    G -->|achou| TEN["tenant (com id)"]
    TEN --> Q["Todas as queries<br/>recebem tenant_id"]
    Q --> DB[("SQLite")]

    W["Webhook"] --> S["slug na URL"]
    S --> TK["valida webhook_token"]
    TK -->|inválido| X2["403"]
    TK -->|"válido ou legado sem token ⚠"| TEN

    style X fill:#e6ffe6
    style X2 fill:#e6ffe6
```

O cliente **nunca** informa `tenant_id`; ele é sempre derivado do token. Desenho correto —
porém **sem nenhum teste** que comprove.

---

## 8. Pontos únicos de falha

```mermaid
flowchart TD
    subgraph "🔴 Falha derruba TODOS os clientes"
        A["Evolution API<br/>1 servidor, 1 réplica"]
        B["Processo FastAPI<br/>1 réplica + agendador embutido"]
        C["SQLite<br/>arquivo único"]
    end
    subgraph "🟠 Falha derruba 1 cliente"
        D["Sessão WhatsApp do consultório"]
        E["Credenciais de calendário"]
    end
    subgraph "🟡 Degrada, não derruba"
        F["Groq → fallback Claude"]
        G["Google/CalDAV → fail-open"]
        H["SMTP, S3, Sentry"]
    end

    A --> Z["Sem atendimento"]
    B --> Z
    C --> Z
    D --> Y["Um consultório parado"]
    E --> Y
```

O incidente de setembro é o caso **A**: o processo da Evolution travou e o único cliente
em operação ficou 7 dias sem atendimento, **sem nenhum alerta**.
