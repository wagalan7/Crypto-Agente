# 01 — Visão Geral do Produto

> Evidências: `backend/main.py`, `backend/segments.py`, `backend/landing_content.py`,
> `backend/templates/*.html`, `backend/config.py`.

---

## 1. Nome e proposta

- **Nome comercial:** Agente de Atendimento
- **Domínio:** `agenteconsultorio.com.br` (o domínio **com `www`** é o que tem certificado
  válido; o domínio raiz falha no TLS — ver `02_ARQUITETURA_TECNICA.md`)
- **Proposta:** um atendente com IA que trabalha no WhatsApp do consultório, agendando e
  confirmando sessões, respondendo pacientes e cobrando mensalidades, sem intervenção do
  profissional.

A promessa aparece na landing (`templates/landing.html`) e nos textos do painel. O
posicionamento efetivo observado no código é **"secretária de agendamento por WhatsApp"**.

---

## 2. Público-alvo e segmentos

O produto nasceu para **psicologia** e foi generalizado para 13 segmentos
(`backend/segments.py`):

| Segmento | Profissional | Cliente | Serviço |
|---|---|---|---|
| `psicologia` *(padrão)* | — | paciente | sessão |
| `nutricao` | Nutricionista | paciente | consulta |
| `fisioterapia` | Fisioterapeuta | paciente | sessão |
| `fonoaudiologia` | Fonoaudiólogo(a) | paciente | sessão |
| `odontologia` | Dentista | paciente | consulta |
| `terapia` | Terapeuta | paciente | sessão |
| `estetica` | Esteticista | cliente | atendimento |
| `advocacia` | Advogado(a) | cliente | atendimento |
| `contabilidade` | Contador(a) | cliente | atendimento |
| `salao` | Cabeleireiro(a) | cliente | atendimento |
| `barbearia` | Barbeiro | cliente | atendimento |
| `petshop` | — | cliente | atendimento |
| `oficina` | Mecânico | cliente | serviço |

**Decisão de arquitetura relevante:** o prompt de psicologia é preservado integralmente
(`_build_system_prompt`, `agent.py:175`); qualquer outro segmento usa
`_generic_system_prompt` (`agent.py:485`), parametrizado por rótulos. Isso protege os
clientes atuais de regressão ao ampliar o alcance.

**Escala real:** 8 consultórios cadastrados (`tenants_count` em `/healthz`), **apenas 1**
operando pela conexão automática. O produto **não foi exercitado em escala**.

---

## 3. Problemas que o agente resolve

1. **Perda de paciente por demora na resposta** — responde 24h/dia.
2. **Agenda remarcada no WhatsApp** — remarca sozinho, sem o profissional parar a sessão.
3. **Faltas (no-show)** — confirmação automática ~24h antes e follow-up no dia.
4. **Cobrança mensal manual** — conta as sessões, calcula e envia com chave PIX.
5. **Contrato de prestação de serviços** — envio, lembrete, assinatura eletrônica e PDF.
6. **Falta de visão do negócio** — painel com insights (ocupação, faltas, receita).

---

## 4. Funcionalidades

### Principais (núcleo do valor)
- Atendimento por WhatsApp com IA
- Agendamento, remarcação, cancelamento e confirmação de presença
- Lembrete 24h antes e follow-up no dia
- Cobrança mensal automática com PIX
- Contrato automático com assinatura eletrônica
- Painel web do profissional

### Secundárias
- Sincronização com Google Calendar e CalDAV
- Pausa do agente por contato (assumir a conversa)
- Página de controle rápido por link (`/controle/{token}`)
- Home Inteligente (insights administrativos) — `backend/insights_service.py`
- Exportação de dados (agenda e cobranças)
- Painel operacional do dono (`/painel`) com assinaturas, carrinhos abandonados,
  contas de infraestrutura, saúde das instâncias, auditoria e 2FA

### Anunciadas mas **não funcionais** (ver `11_INVENTARIO_FUNCIONAL.md`)
- **Transcrição de áudio** — função existe, nunca é chamada
- **Leitura de imagem/documento no provedor de produção** — só o Z-API trata
- **Secretário virtual** (tarefas, rotinas, comandos do dono) — inexistente

---

## 5. Jornada de contratação de um novo cliente

Evidência: `main.py` rotas `/onboarding*`, `/checkout/*`, `/webhooks/*`.

1. Visitante chega na landing (`GET /`) ou numa landing por segmento (`GET /p/{segment}`)
2. `GET /onboarding` → formulário (dados do consultório + faturamento + aceite de termos)
3. `POST /onboarding/create` → cria o tenant com `status="pending_payment"`,
   gera `slug`, `dashboard_token`, `setup_token` e `webhook_token`; grava em `audit_log`
4. `GET /onboarding/pagamento` → escolha do plano (mensal / semestral / anual)
5. `POST /checkout/stripe` **ou** `POST /checkout/mercadopago`
6. Webhook de confirmação (`POST /webhooks/stripe` / `/webhooks/mercadopago`) ativa a conta
7. `GET /onboarding/sucesso` entrega o link do painel (com `dashboard_token`)
8. No painel, o cliente conecta o WhatsApp lendo um QR code

**Observação:** o passo 8 é onde a experiência mais falha hoje — há dois caminhos de
conexão concorrentes (automático e Z-API manual) e o cliente se confunde entre eles.
Ver `06_PAINEL_E_EXPERIENCIA.md`.

---

## 6. Jornada do administrador (profissional)

Acesso: `GET /dashboard/{slug}?token=...` — **sem senha**, o token na URL é a credencial
(`main.py:1228`, compara com `hmac.compare_digest`).

Abas (`templates/dashboard.html`):

| Aba | Função |
|---|---|
| **Agenda** | Consultas, criar/remarcar/cancelar, marcar comparecimento, Home Inteligente |
| **Horários** | Slots livres e configuração de disponibilidade |
| **Conversas** | Histórico por paciente, pausar/retomar o agente, assumir o atendimento |
| **Contratos** | Enviar, reenviar, acompanhar assinatura, editar modelo, buscar, excluir |
| **Config** | Geral, WhatsApp, Mensagens, Cobrança, Integrações, Conta |

---

## 7. Jornada do paciente (WhatsApp)

1. Paciente manda mensagem para o número do consultório
2. Evolution/Z-API entrega no webhook (`POST /webhook/{slug}/evolution`)
3. Filtros: token do webhook, grupo/transmissão, spam, tenant suspenso, agente pausado
4. Atalhos determinísticos (confirmação, recusa, remarcação explícita, saudação)
5. Se nada casar: LLM com contexto (consulta agendada, nome, horários livres, histórico)
6. Ação executada no banco (criar/remarcar/confirmar/cancelar/listar)
7. Resposta enviada pelo provedor

**Caminho do paciente novo:** o agente dá boas-vindas, pede o nome completo e informa que
o profissional entrará em contato. **Não agenda e não oferece horários** — decisão de
produto explícita no prompt (`agent.py`, seção "FLUXO PARA NOVO PACIENTE").

---

## 8. Diferenciais reais

1. **Contrato com assinatura eletrônica integrado ao WhatsApp** — raro em concorrentes de
   agendamento; apoiado em `contract_service.py` (616 linhas) com hash, IP e PDF.
2. **Cobrança mensal automática** que conta sessões efetivamente realizadas, com ajustes
   manuais, avulsos e controle de pagamento.
3. **Atalhos determinísticos** — confirmar presença não depende do LLM, o que reduz custo
   e elimina uma classe inteira de erro.
4. **Correção forçada de data/hora** na resposta do modelo (anti-alucinação).
5. **Multi-segmento** sem alterar o comportamento do segmento original.
6. **Diagnóstico embutido** (`/dashboard/api/whatsapp/diagnose`) que mostra conexão,
   webhook gravado no provedor e último webhook recebido.

---

## 9. Limitações atuais

| Limitação | Natureza |
|---|---|
| Áudio, imagem e documento ignorados no provedor de produção | Funcional — **crítica** |
| Transcrição de áudio não implementada de fato | Funcional |
| Sem funcionalidades de secretário virtual | Escopo |
| Sem base de conhecimento / FAQ do negócio | Inteligência |
| Memória curta: últimas 8 mensagens, sem memória de longo prazo | Inteligência |
| Sem tratamento de fuso horário por cliente (America/Sao_Paulo fixo) | Funcional |
| Dupla reserva possível sob concorrência | Confiabilidade |
| Sem testes automatizados | Engenharia |
| Sem alerta de queda de conexão | Operação |
| Sem medição de consumo de IA por cliente | Comercial |
| Dependente de WhatsApp não-oficial (Baileys via Evolution) | Risco de plataforma |

**Sobre o último item:** a Evolution API usa biblioteca não-oficial. Não há garantia de
continuidade e o WhatsApp pode bloquear números. Isso precisa estar no contrato com o
cliente. **NÃO VERIFICADO:** se os termos de uso atuais (`templates/termos.html`) cobrem
esse risco.
