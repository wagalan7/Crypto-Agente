# 06 — Painel e Experiência do Usuário

> Evidências: `backend/templates/dashboard.html` (201 KB), `admin_panel.html` (51 KB),
> `controle.html`, `onboarding.html`, rotas `/dashboard/*` e `/painel/*`.
> **Limitação:** avaliação por leitura de código e de capturas de tela obtidas durante a
> operação. **Não houve teste de usabilidade com usuários.**

---

## 1. Painel do profissional — `/dashboard/{slug}?token=`

### Abas

| Aba | Recursos |
|---|---|
| **Agenda** | Home Inteligente (cards clicáveis), lista de consultas, criar/remarcar/cancelar, marcar comparecimento, renomear paciente, exportar |
| **Horários** | Slots livres, configuração de disponibilidade |
| **Conversas** | Histórico por paciente, pausar/retomar agente, excluir conversa, link de controle |
| **Contratos** | Configurações, modelo versionado, enviar/reenviar, **busca**, **excluir**, **editar nome**, PDF |
| **Config** | 6 sub-seções: Geral, WhatsApp, Mensagens, Cobrança, Integrações, Conta |

### Cobranças
Prévia do mês, valor por paciente, ajustes manuais (`billing_overrides`), cobranças
avulsas, marcar como pago, comprovantes recebidos, pausar cobrança (global ou por
paciente), exportação e diagnóstico.

**Avaliação: 🟢 funcionalmente rico** — cobre o ciclo financeiro do consultório melhor que
a maioria dos concorrentes de agendamento.

---

## 2. Facilidade para configurar um agente novo

Fluxo: onboarding → pagamento → painel → **conectar WhatsApp**.

🔴 **O passo de conectar o WhatsApp é o maior problema de usabilidade do produto.**
Evidência direta coletada em produção:

- Existem **dois caminhos concorrentes** na mesma tela: card verde "⚡ Conectar WhatsApp
  (automático)" e card "📲 WhatsApp (Z-API)" + "🔗 Conectar o WhatsApp".
- **Os dois têm botão verde escrito "Conectar WhatsApp"**.
- As três colunas do banco são compartilhadas, então o card Z-API aparecia
  **pré-preenchido com dados do Evolution** — e salvar ali **trocava o provedor e
  derrubava o envio** (corrigido na build 57).
- O cliente clicou repetidamente no card errado e recebeu `HTTP 400` sem explicação.

Correções já aplicadas (builds 57, 62, 63): cards Z-API recolhidos para contas
automáticas, aviso explicativo, botão "Refazer conexão (novo QR)" e mensagens de erro
traduzidas. **Mesmo assim o fluxo continua confuso** — a arquitetura de dois provedores
compartilhando as mesmas colunas é a causa raiz e não foi removida.

---

## 3. Personalização por empresa

| Item | Disponível |
|---|---|
| Nome do consultório e do profissional | ✅ |
| Horário de funcionamento, dias, bloqueios por dia/data | ✅ |
| Duração da sessão | ✅ |
| Segmento e rótulos | ✅ (via banco; **não exposto no painel** — NÃO VERIFICADO) |
| Textos das mensagens automáticas | ✅ |
| Chave PIX | ✅ |
| Modelo de contrato | ✅ versionado |
| **Personalidade/tom do agente** | ❌ fixo no prompt |
| **Base de conhecimento (FAQ)** | ❌ não existe |
| Logo/cores | ❌ |

---

## 4. Visualização de conversas e intervenção humana

✅ Histórico por paciente, pausar/retomar, excluir, SSE para atualização ao vivo
(`/dashboard/stream/{slug}`), página `/controle/{token}` para ações rápidas no celular.

❌ **Não é possível responder ao paciente pelo painel.** A profissional pausa o agente e
responde pelo WhatsApp dela. Para um produto de atendimento, essa é uma ausência sentida.

---

## 5. Relatórios e indicadores

**Home Inteligente** (`insights_service.py`) — 10 indicadores: `today`, `occupancy`,
`confirmations`, `no_shows`, `reschedules`, `cancellations`, `reminder_effectiveness`,
`no_return`, `revenue`, `contracts`. Desde a build 54 os cards levam à aba relevante.

Faltam: evolução histórica, comparação entre períodos, gráficos, exportação de relatório.

---

## 6. Permissões de usuários

🔴 **Não existe gestão de usuários no painel do profissional.** Um consultório = um token.
Não há papéis (recepcionista × profissional), múltiplos acessos ou trilha de quem fez o
quê **dentro** do consultório (o `audit_log` cobre só eventos administrativos).

Um consultório com duas secretárias compartilha o mesmo link.

---

## 7. Experiência em dispositivos móveis

O painel é usado no celular (confirmado por capturas). Há navegação inferior com cinco
ícones e layout responsivo com Tailwind.

Problemas observados:
- Mensagens de erro longas (JSON cru do provedor) estouravam a tela — parcialmente
  corrigido nas builds 60 e 65
- Dois cards de conexão quase idênticos agravam a confusão na tela pequena
- Página pesada: 201 KB de HTML com JS inline, sem divisão de código

---

## 8. Problemas de usabilidade identificados

| # | Problema | Gravidade | Evidência |
|---|---|---|---|
| 1 | Dois caminhos de conexão de WhatsApp quase idênticos | 🔴 Alta | Incidente real, horas perdidas pelo cliente |
| 2 | Erros técnicos crus exibidos ao usuário | 🟠 Média | `HTTP 400 {"status":400,...}` na tela |
| 3 | Selo "Conectado" mentia por 7 dias | 🔴 Alta | Lê só o estado do provedor, não o canal real |
| 4 | Não dá para responder ao paciente pelo painel | 🟠 Média | Ausência de recurso |
| 5 | Sem gestão de usuários/papéis | 🟠 Média | — |
| 6 | Token na URL como única credencial | 🟠 Média | Vaza por histórico, não expira |
| 7 | Contratos mostravam número no lugar do nome | 🟡 Baixa | Corrigido nas builds 55 e 56 |
| 8 | Campo de telefone sem orientação de formato | 🟡 Baixa | Causou falha com número de Portugal; corrigido na build 67 |

---

## 9. Painel operacional do dono — `/painel`

Abas: `overview`, `tenants`, `subs` (assinaturas), `abandoned` (carrinhos), `health`
(instâncias), `content` (conteúdo do site), `audit`, `security` (2FA).

**Avaliação: 🟢 bem resolvido para o estágio.** Dá visão de negócio, contas de
infraestrutura, saúde das conexões e auditoria. O que falta é **alerta ativo** — hoje é
preciso abrir o painel para descobrir que algo caiu.
