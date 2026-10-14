# 11 — Inventário Funcional

Classificação:
- **✅ IMPLEMENTADO E VERIFICADO** — código existe e foi confirmado por execução local ou observação em produção
- **🔵 IMPLEMENTADO, MAS NÃO TESTADO** — código completo e coerente; sem verificação de execução
- **🟡 PARCIALMENTE IMPLEMENTADO** — funciona em parte ou só em um caminho
- **❌ NÃO IMPLEMENTADO** — não existe
- **❓ NÃO VERIFICADO** — não foi possível avaliar

---

## 1. Atendimento e conversação

| Recurso | Status | Evidência |
|---|---|---|
| Receber mensagem de texto (Evolution) | ✅ | `extract_message_evolution`; observado em produção |
| Receber mensagem de texto (Z-API) | 🔵 | `extract_message_zapi` |
| Receber mensagem (Twilio) | ❓ | `extract_message_twilio`; sem cliente usando |
| Responder com IA | ✅ | Groq + fallback Claude; conversas reais observadas |
| Atalho: confirmação de presença | 🔵 | `_try_deterministic_confirm` |
| Atalho: recusa | 🔵 | `_try_deterministic_decline` |
| Atalho: remarcação com data/hora | 🔵 | `_try_deterministic_reschedule` |
| Atalho: saudação | 🔵 | `_is_simple_greeting` |
| Contexto (consulta, nome, slots, histórico) | ✅ | `_build_context`; verificado ao auditar `slot_id` |
| Memória de curto prazo (8 mensagens) | 🔵 | `get_conversation_history(limit=8)` |
| Memória de longo prazo | ❌ | — |
| Base de conhecimento / FAQ | ❌ | — |
| Filtro de spam | 🔵 | `_is_spam` |
| Filtro de grupo/transmissão/status | ✅ | Verificado com 17 casos executados |
| Dedup de webhook | 🔵 | `webhook_seen` |
| Anti-alucinação de data/hora | ✅ | Regex de correção; validado na build 64 |
| Proteção contra prompt injection | ❌ | — |
| Escalonamento automático para humano | ❌ | — |

## 2. Mídia

| Recurso | Status | Evidência |
|---|---|---|
| Imagem — Z-API | 🔵 | `__IMAGEM__` + `classify_image` |
| Imagem — **Evolution (produção)** | ❌ | Extractor só lê texto |
| Documento/PDF — Z-API | 🔵 | `__DOCUMENTO__` |
| Documento/PDF — **Evolution** | ❌ | — |
| Comprovante PIX nativo — Z-API | 🔵 | `__COMPROVANTE_PIX__` |
| Comprovante PIX — **Evolution** | ❌ | — |
| **Transcrição de áudio** | ❌ | `transcribe_audio_groq` **nunca chamada** |
| Classificação de imagem por visão | 🔵 | `classify_image` (Claude) |
| Sinalização de comprovante | 🔵 | `billing_receipts` + confirmação humana |

## 3. Agenda

| Recurso | Status | Evidência |
|---|---|---|
| Consultar disponibilidade | ✅ | `get_available_slots` |
| Criar consulta | ✅ | `Action.create` |
| Remarcar | ✅ | `Action.update` / `_do_reschedule`; verificado em produção |
| Cancelar (suave) | 🔵 | `cancel_appointment` + `cancellation_log` |
| Confirmar presença | ✅ | Observado em produção |
| Marcar comparecimento | 🔵 | `attendance` |
| Detectar conflito | 🟡 | `has_conflict` sem índice único → corrida |
| Guarda anti-duplicata | 🔵 | `create` vira remarcação se já há 1 consulta |
| Identificar horário por id | ✅ | `_resolve_slot`, 9 casos executados |
| Bloqueios (dias, horas, datas, por dia da semana) | 🔵 | Colunas em `tenants` |
| Fuso horário por cliente/paciente | ❌ | `America/Sao_Paulo` fixo |
| Lista de espera / encaixe | ❌ | — |

## 4. Integrações de calendário

| Recurso | Status |
|---|---|
| Google Calendar — OAuth | 🔵 |
| Google Calendar — criar/atualizar/excluir | 🔵 |
| CalDAV — criar/atualizar/excluir | 🔵 |
| CalDAV — teste de conexão | 🔵 |
| Recuperação se o evento sumiu | 🔵 (cria novo) |

## 5. Automações

| Recurso | Status | Evidência |
|---|---|---|
| Confirmação ~24h antes | ✅ | Observada em produção |
| Follow-up no dia | 🔵 | `_FOLLOWUP_HOUR` |
| Janela de envio 7h–22h | 🔵 | — |
| Cobrança mensal | 🔵 | `_run_billing` |
| Cobranças avulsas | 🔵 | `billing_manual_entries` |
| Backup diário | 🔵 | `backup_service`; **restauração nunca testada** |
| Lembrete de vencimento | 🔵 | `billing_reminders` |
| Lembretes de contrato | 🔵 | `contract_reminders_log` |
| Monitor de instância | 🟡 | Grava, **não alerta** |
| **Alerta de falha ao operador** | ❌ | — |
| Resumo diário por WhatsApp | ❌ | — |
| Follow-up comercial / recuperação de lead | ❌ | — |
| Idempotência das automações | 🔵 | Vários mecanismos |

## 6. Cobrança do paciente

| Recurso | Status |
|---|---|
| Cálculo por sessões realizadas | 🔵 |
| Preço por paciente | 🔵 |
| Ajuste manual do mês | 🔵 |
| Cobrança avulsa | 🔵 |
| Marcar como pago | 🔵 |
| Comprovantes recebidos | 🟡 (só Z-API) |
| Pausar cobrança (global/por paciente) | 🔵 |
| PIX na mensagem | 🔵 |
| Exportação | 🔵 |
| Diagnóstico de cobrança | 🔵 |
| Link de pagamento (cartão/boleto) | ❌ |

## 7. Contratos

| Recurso | Status |
|---|---|
| Modelo versionado por consultório | 🔵 |
| Envio por WhatsApp | ✅ (observado) |
| Assinatura eletrônica (nome, CPF, IP, user-agent, hash) | 🔵 |
| Geração de PDF | 🔵 |
| Lembretes e expiração | 🔵 |
| Travar agendamento sem contrato | 🔵 (desligado por padrão) |
| Auto-envio para pacientes novos | 🔵 (desligado por padrão) |
| Busca na lista | ✅ (build 55) |
| Excluir contrato | ✅ (build 55) |
| Editar nome do paciente | ✅ (build 56) |

## 8. Painel do profissional

| Recurso | Status |
|---|---|
| Acesso por token | ✅ |
| Agenda e CRUD de consultas | ✅ |
| Home Inteligente (10 indicadores) | ✅ |
| Cards clicáveis | ✅ (build 54) |
| Horários e disponibilidade | 🔵 |
| Conversas + pausar/retomar | 🔵 |
| Atualização ao vivo (SSE) | 🔵 |
| Página `/controle/{token}` | 🔵 |
| Configurações (6 sub-seções) | 🔵 |
| Conexão de WhatsApp por QR | 🟡 (funciona, fluxo confuso) |
| Diagnóstico de WhatsApp | ✅ (builds 60–62) |
| Refazer conexão | 🟡 (não cura sessão travada; orienta restart) |
| **Responder paciente pelo painel** | ❌ |
| **Papéis e múltiplos usuários** | ❌ |
| Relatórios históricos | ❌ |

## 9. Painel do operador

| Recurso | Status |
|---|---|
| Login + 2FA TOTP + lockout | 🔵 |
| Visão geral e assinaturas | 🔵 |
| Carrinhos abandonados + lembrete manual | 🔵 |
| Gestão de tenants | 🔵 |
| Contas de infraestrutura | 🔵 |
| Saúde das instâncias | 🔵 |
| Conteúdo do site | 🔵 |
| Auditoria | 🔵 |

## 10. Comercial e multiempresa

| Recurso | Status |
|---|---|
| Multi-inquilino | ✅ |
| Onboarding self-service | 🔵 |
| Stripe | 🔵 |
| Mercado Pago | 🔵 |
| Planos e vencimento | 🔵 |
| Suspensão por inadimplência | 🔵 |
| Acesso de cortesia | 🔵 |
| Recuperação de acesso | 🔵 |
| 13 segmentos | 🔵 |
| **Limites por plano** | ❌ |
| **Medição de consumo de IA** | ❌ |
| Validação de assinatura dos webhooks de pagamento | ❓ |

## 11. Segurança e conformidade

| Recurso | Status |
|---|---|
| Credenciais cifradas em repouso | 🔵 (⚠ depende de `FIELD_ENCRYPTION_KEY` — ❓ em produção) |
| MASTER_KEY em `/admin` e `/test` | 🔵 |
| 2FA no painel | 🔵 |
| Rate limiting por IP | 🔵 (em memória) |
| Token de webhook por tenant | 🟡 (legado aceito sem token) |
| Auditoria de eventos admin | 🔵 |
| Termos e Privacidade | 🔵 |
| Aceite versionado | 🔵 |
| **Conversas cifradas** | ❌ |
| **Consentimento do paciente** | ❌ |
| **Opt-out** | ❌ |
| **Política de retenção** | ❌ |
| Backup | 🔵 (restauração ❓) |

## 12. Secretário virtual

| Recurso | Status |
|---|---|
| Lista de tarefas | ❌ |
| Comandos do dono (texto) | ❌ |
| Comandos do dono (áudio) | ❌ |
| Resumo diário/semanal | ❌ |
| Organização de reuniões | ❌ |
| Rotinas configuráveis pelo cliente | ❌ |
| Triagem de recados | ❌ |
| Integração com e-mail do cliente | ❌ |

---

## Resumo quantitativo

| Status | Qtd. aprox. |
|---|---|
| ✅ Implementado e verificado | 20 |
| 🔵 Implementado, não testado | 60 |
| 🟡 Parcialmente implementado | 10 |
| ❌ Não implementado | 28 |
| ❓ Não verificado | 5 |

**Leitura:** o produto tem **muita** funcionalidade construída (80 recursos implementados).
O problema não é falta de código — é que **apenas 20 têm alguma verificação**, e as
ausências se concentram justamente no que foi prometido comercialmente (secretário,
áudio, mídia) e no que sustenta a operação (alertas, limites, conformidade).
