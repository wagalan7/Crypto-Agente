# 00 — Resumo Executivo

**Projeto:** Agente de Atendimento (WhatsApp SaaS para consultórios)
**Data da auditoria:** 2026-10-12
**Build em produção no momento da auditoria:** `2026-07-16-67`
**Natureza:** auditoria **somente leitura**. Nada foi implementado, alterado ou implantado.

---

## 1. Síntese do projeto

SaaS **multi-inquilino** que coloca um atendente com IA no WhatsApp de consultórios e
pequenos negócios de serviço. O agente agenda, confirma, remarca e cancela consultas,
faz o primeiro contato com pacientes novos, dispara cobrança mensal e gerencia contrato
com assinatura eletrônica. Acompanha um painel web para o profissional e um painel
operacional para o dono do negócio.

- **Backend:** Python 3.11 + FastAPI, **14.724 linhas** em 26 módulos, **148 rotas HTTP**
- **Banco:** SQLite em volume, **26 tabelas**, 45 migrações incrementais
- **IA:** Groq `llama-3.3-70b-versatile` (primário) + Anthropic `claude-haiku-4-5` (fallback e visão)
- **WhatsApp:** Evolution API (em uso na produção), Z-API e Twilio suportados
- **Hospedagem:** Railway (app) + Evolution API em **outro projeto Railway**

**Escala atual:** 8 consultórios cadastrados; **1 único** em operação real pela conexão
automática (Evolution). Volume de produção muito baixo — o sistema **ainda não foi
exercitado em escala**.

---

## 2. Principais pontos fortes

1. **Multi-inquilino de verdade.** Todo dado é escopado por `tenant_id`; não há nada
   hard-coded por cliente. Cadastrar um novo consultório não exige alterar código.
2. **Onboarding self-service completo**, com cobrança integrada (Stripe + Mercado Pago),
   suspensão automática por inadimplência e recuperação de acesso.
3. **Agendamento robusto para o caminho feliz.** Criação, remarcação, cancelamento,
   confirmação 24h, follow-up e detecção de sobreposição funcionam de ponta a ponta.
4. **Atalhos determinísticos** antes do LLM (confirmar, recusar, remarcar com data/hora
   explícita, saudação): mais barato, mais rápido e mais previsível que depender da IA.
5. **Anti-alucinação no agendamento.** O texto do LLM é reescrito com o horário canônico
   calculado do `datetime` real, impedindo que o modelo informe dia/hora errados.
6. **Contrato automático** com assinatura eletrônica (IP, user-agent, hash), PDF e
   versionamento de template — diferencial comercial relevante no nicho.
7. **Segurança de perímetro acima da média para o estágio:** `/admin/*` e `/test/*`
   exigem MASTER_KEY (fail-closed), painel com 2FA TOTP e bloqueio por tentativas,
   credenciais cifradas em repouso, dedup persistente de webhook, rate limiting por IP.

---

## 3. Problemas críticos

| # | Problema | Evidência | Impacto |
|---|---|---|---|
| 1 | **Zero testes automatizados e zero CI** | nenhum `test_*.py`, `conftest.py` ou workflow no projeto | Qualquer alteração é um risco cego. Em ~2 meses foram 19 builds, várias corrigindo regressões descobertas por clientes |
| 2 | **Mídia não funciona no provedor de produção** | `extract_message_evolution` (whatsapp_service.py:143) só lê `conversation` e `extendedTextMessage` | Paciente que manda **áudio, imagem ou PDF** é ignorado em silêncio. Comprovante de pagamento não é registrado |
| 3 | **Transcrição de áudio é código morto** | `transcribe_audio_groq` (whatsapp_service.py:494) nunca é chamada | Funcionalidade anunciada que não existe em nenhum provedor |
| 4 | **"Secretário virtual" praticamente não existe** | sem tarefas, resumo diário por WhatsApp, comandos do dono ou rotinas | O produto é um **agendador para pacientes**, não um secretário executivo |
| 5 | **Dupla reserva é possível** | `has_conflict` + `create_appointment` sem transação; **sem índice único** em `appointments` | Dois pacientes podem marcar o mesmo horário |
| 6 | **Falha silenciosa de conexão** | incidente verificado: 7 dias (09/09→16/09) com o agente mudo sem nenhum alerta | Cliente descobre por acaso; risco de cancelamento |
| 7 | **Sem controle de consumo/custo de IA** | nenhuma contagem de tokens, limite por plano ou quota | Cliente de alto volume destrói a margem sem que ninguém perceba |
| 8 | **Imagem de produção presa em `latest`** | Evolution roda `evoapicloud/evolution-api:latest` | Quando a 2.4.0 virar estável, **um redeploy derruba o produto**: ela exige licença e devolve `503` |

---

## 4. Grau de prontidão comercial

### Veredito: **NÃO está pronto para comercialização ampla imediata.**

| Dimensão | Nota | Comentário |
|---|---|---|
| Multi-inquilino e isolamento | 🟢 Bom | Arquitetura correta |
| Cobrança e planos | 🟢 Bom | Stripe + MP + suspensão funcionam |
| Agendamento (núcleo) | 🟡 Aceitável | Funciona, mas com risco de dupla reserva |
| Atendimento com mídia | 🔴 Quebrado | Áudio/imagem/PDF ignorados na produção |
| "Secretário virtual" | 🔴 Inexistente | Não vender essa promessa |
| Confiabilidade | 🔴 Frágil | Sem testes, sem alerta de queda |
| Observabilidade | 🟡 Parcial | Sentry + diagnóstico; sem alerta proativo |
| Conformidade LGPD | 🟡 Parcial | Base existe; faltam retenção e exclusão do titular |

**É possível vender ainda nesta semana em modo controlado** — venda assistida, poucos
clientes, acompanhamento próximo — desde que os bloqueadores do item 6 sejam resolvidos
e a promessa comercial seja ajustada ao que o produto realmente faz.

---

## 5. Principais recomendações

**Nesta semana (bloqueadores):**
1. Tratar **mídia no caminho Evolution** (áudio, imagem, documento) ou **declarar a
   limitação** no material de venda. Hoje o cliente perde mensagens de paciente.
2. **Alerta de conexão caída** por e-mail. Sem isso, um cliente pode ficar dias sem
   atendimento e você descobre pelo cancelamento.
3. **Travar a versão da Evolution** (sair do `latest`). É uma bomba-relógio de
   indisponibilidade total.
4. **Índice único** em `appointments` para eliminar dupla reserva.
5. **Ajustar a promessa comercial**: vender "atendente e agendador de WhatsApp", não
   "secretário virtual".

**Próximas 2–4 semanas:**
6. Testes automatizados nos fluxos críticos (agendar, remarcar, cancelar, confirmar,
   isolamento entre clientes) + CI.
7. Medição de consumo de IA por cliente e limites por plano.
8. LGPD: política de retenção e exclusão de dados do titular.

---

## 6. O que impede o lançamento imediato

1. **Perda silenciosa de mensagens de paciente** (mídia no Evolution). É o pior tipo de
   falha: o cliente não vê, o paciente fica sem resposta.
2. **Nenhum alerta quando o agente para.** Já aconteceu por 7 dias.
3. **Risco de indisponibilidade total** pela imagem `latest` da Evolution.
4. **Ausência de testes** em um produto que será alterado toda semana para atender
   clientes novos.
5. **Descompasso entre promessa e entrega** se for vendido como secretário virtual.

---

## 7. O que dá para melhorar ainda nesta semana

Itens de **esforço baixo** e impacto alto:

- Travar a versão da imagem da Evolution — ajuste de configuração, sem código.
- Índice único em `appointments` — uma migração.
- Alerta de conexão por e-mail — o monitor de instâncias e o serviço de e-mail já existem.
- Tratar imagem e documento no extractor da Evolution — espelhar o que o Z-API já faz.
- Revisar os textos de venda para refletir as funcionalidades reais.

Itens que **não cabem** nesta semana: testes abrangentes, medição de custo de IA,
secretário virtual, base de conhecimento.

---

## 8. Limitações desta auditoria

- **Análise estática.** Não executei o sistema em produção nem simulei conversas reais.
- **Sem acesso ao banco de produção.** Afirmações sobre dados reais vêm de endpoints de
  diagnóstico observados durante a operação, não de consulta direta.
- **Sem acesso ao painel da Railway** nem às variáveis de ambiente: valores efetivos de
  configuração (`BASE_URL`, chaves, SMTP) são **NÃO VERIFICADOS**.
- **Comportamento do LLM não foi testado.** Afirmações sobre como o modelo responde
  derivam da leitura dos prompts, não de execução.
- O código do bot de cripto que divide o mesmo repositório **não foi auditado** e está
  fora do escopo.
