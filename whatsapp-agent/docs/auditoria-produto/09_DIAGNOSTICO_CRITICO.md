# 09 — Diagnóstico Crítico

Legenda de severidade: 🔴 crítica · 🟠 alta · 🟡 média · 🟢 baixa
Esforço: **B**aixo (horas) · **M**édio (dias) · **A**lto (semanas)

---

## 1. Os 20 maiores pontos fracos

| # | Ponto fraco | Sev. | Impacto comercial | Esforço | Evidência |
|---|---|---|---|---|---|
| 1 | **Mídia descartada no provedor de produção** (áudio, imagem, PDF) | 🔴 | Perda de mensagem de paciente; cliente perde confiança | M | `extract_message_evolution` só lê texto |
| 2 | **Zero testes e zero CI** | 🔴 | Toda entrega é risco; retrabalho constante | M–A | Nenhum arquivo de teste |
| 3 | **Nenhum alerta quando o agente para** | 🔴 | 7 dias mudo sem ninguém saber; risco de cancelamento | B | Incidente 09/09→16/09 |
| 4 | **Conversas clínicas em texto puro no banco** | 🔴 | Exposição LGPD (dado sensível de saúde) | M | `conversations.content`; cripto só em credenciais |
| 5 | **Imagem `latest` na Evolution** | 🔴 | Indisponibilidade total quando a 2.4.0 (com licença) virar estável | B | `evoapicloud/evolution-api:latest` |
| 6 | **Dupla reserva possível** | 🔴 | Dois pacientes no mesmo horário | B | Sem índice único; sem transação |
| 7 | **Transcrição de áudio é código morto** | 🟠 | Funcionalidade anunciada que não existe | M | `transcribe_audio_groq` nunca chamada |
| 8 | **"Secretário virtual" não existe** | 🟠 | Promessa comercial sem lastro | A | Sem tarefas, rotinas ou comandos do dono |
| 9 | **Sem base de conhecimento** | 🟠 | Agente não responde preço, convênio, endereço | M | Nenhuma estrutura de FAQ |
| 10 | **Sem medição de consumo de IA** | 🟠 | Margem imprevisível; sem diferenciar planos | M | Nenhuma contagem |
| 11 | **Fuso horário fixo** (`America/Sao_Paulo`) | 🟠 | **Já há paciente em Portugal** recebendo horário errado | M | `_TZ` fixo |
| 12 | **Ponto único de falha na Evolution** | 🟠 | Um travamento derruba **todos** os clientes | A | 1 servidor, 1 réplica |
| 13 | **Fluxo de conexão do WhatsApp confuso** | 🟠 | Trava o onboarding; horas de suporte por cliente | M | Dois cards quase idênticos |
| 14 | **Sem consentimento/opt-out do paciente** | 🟠 | Risco LGPD | M | Nenhum registro |
| 15 | **Mensagem perdida se o processo cair** | 🟠 | Perda silenciosa | M | `webhook_seen` gravado antes de concluir |
| 16 | **Token do painel não expira e vai na URL** | 🟠 | Acesso indevido se o link vazar | M | `dashboard_token` |
| 17 | **`main.py` com 4.600 linhas e 148 rotas** | 🟠 | Lentidão e risco em cada alteração | A | — |
| 18 | **Agendador acoplado ao processo web** | 🟡 | Impede escalar horizontalmente | M | thread em `scheduler.py` |
| 19 | **Sem gestão de usuários/papéis** | 🟡 | Consultório com equipe compartilha um link | M | — |
| 20 | **Não dá para responder o paciente pelo painel** | 🟡 | Profissional alterna entre painel e celular | M | — |

---

## 2. Os 10 riscos técnicos mais relevantes

| # | Risco | Prob. | Impacto | Mitigação atual |
|---|---|---|---|---|
| 1 | **Evolution trava e todos param** | Alta (já ocorreu) | Crítico | Nenhuma — só manual |
| 2 | **Redeploy puxa a 2.4.0 com licença** | Média | Crítico | Nenhuma |
| 3 | **WhatsApp bloqueia o número do cliente** | Média | Crítico | Nenhuma |
| 4 | **Regressão por falta de teste** | Alta | Alto | Nenhuma |
| 5 | **Baileys quebra com mudança do WhatsApp** | Média | Crítico | Depende de terceiro |
| 6 | **SQLite trava sob concorrência** | Baixa hoje, alta com escala | Alto | Volume baixo |
| 7 | **Vazamento de dados sensíveis** | Baixa | Crítico (LGPD) | Credenciais cifradas; conversas não |
| 8 | **Dupla reserva** | Média | Médio | `has_conflict` (insuficiente) |
| 9 | **Custo de IA sem teto** | Média | Médio | Nenhuma |
| 10 | **Perda de dados** | Baixa | Crítico | Backup diário (**restauração NÃO TESTADA**) |

---

## 3. Gargalos de inteligência

1. **Sem base de conhecimento** — o agente só agenda; qualquer pergunta sobre o negócio
   fica sem resposta útil. É o gargalo mais percebido pelo paciente.
2. **Memória de 8 mensagens** — sem continuidade entre conversas.
3. **Prompts gigantes em modelo pequeno** — 19,7 mil caracteres num Llama 3.3 70B;
   instruções do fim tendem a ser ignoradas.
4. **Sem confirmação antes de ação destrutiva** — cancelar é decisão solitária do modelo.
5. **Sem escalonamento automático** — o agente nunca chama o humano por iniciativa própria.
6. **Sem proteção contra prompt injection**.
7. **Sem avaliação de qualidade** — não há métrica de acerto, nem conjunto de casos.

---

## 4. Gargalos de experiência

1. **Conexão do WhatsApp** — maior atrito do onboarding.
2. **Selo "Conectado" não confiável** — mentiu por 7 dias.
3. **Erros técnicos crus na tela** — parcialmente corrigido.
4. **Sem resposta pelo painel**.
5. **Sem papéis de usuário**.
6. **Página de 201 KB** com JS inline.
7. **Relatórios sem histórico** — fotografia do momento, sem evolução.

---

## 5. Funcionalidades importantes ausentes

| Funcionalidade | Por que importa |
|---|---|
| Base de conhecimento por cliente | Agente deixa de ser só agendador |
| Transcrição de áudio (de fato) | Metade dos pacientes manda áudio |
| Mídia no provedor de produção | Hoje é perda de mensagem |
| Alerta de falha | Confiança operacional |
| Resumo diário por WhatsApp | Valor percebido diário |
| Caixa de entrada no painel | Fecha o ciclo de atendimento |
| Papéis de usuário | Consultório com secretária |
| Relatórios históricos | Justifica a mensalidade |
| Fuso horário por paciente | Atendimento online/exterior |
| Medição de consumo | Sustenta planos diferenciados |

---

## 6. Funcionalidades existentes que precisam melhorar

| Funcionalidade | Problema | Esforço |
|---|---|---|
| Conexão do WhatsApp | Dois caminhos confusos; estado mentiroso | M |
| Detecção de conflito | Sem índice único | B |
| Home Inteligente | Sem histórico nem gráfico | M |
| Prompts | Grandes, repetitivos, duplicados entre segmentos | M |
| Cobrança | Boa, mas sem link de pagamento (só PIX manual) | M |
| Contratos | Bons; falta lembrete ao profissional de pendências | B |
| Diagnóstico | Excelente, mas escondido (só por URL manual) | B |

---

## 7. Diferenciais que aumentariam o valor comercial

1. **Base de conhecimento por consultório** — o salto de agendador para atendente.
2. **Transcrição de áudio** — diferencial concreto no Brasil.
3. **Resumo diário no WhatsApp** — o cliente vê valor todo dia.
4. **Relatório mensal automático** — justifica a renovação.
5. **Caixa de entrada unificada** no painel.
6. **Reativação de pacientes inativos** — o insight `no_return` já existe; falta a ação.
7. **Lista de espera** — encaixar paciente quando alguém cancela.
8. **Confirmação por link** (botão em vez de texto) — aumenta a taxa de confirmação.

---

## 8. Obstáculos para vender **nesta semana**

| # | Obstáculo | Dá para resolver em 1 semana? |
|---|---|---|
| 1 | Mídia descartada na produção | 🟡 Parcialmente — dá para tratar imagem e documento |
| 2 | Sem alerta de queda | ✅ Sim (esforço baixo) |
| 3 | Imagem `latest` | ✅ Sim (configuração) |
| 4 | Dupla reserva | ✅ Sim (uma migração) |
| 5 | Promessa de "secretário virtual" | ✅ Sim (ajuste de texto) |
| 6 | Conversas sem criptografia | ❌ Não — exige migração cuidadosa |
| 7 | Zero testes | 🟡 Parcialmente — dá para cobrir o núcleo |
| 8 | Fuso horário | ❌ Não |
| 9 | Consentimento LGPD | 🟡 Parcialmente — dá para registrar o aceite |

**Conclusão:** cinco dos nove obstáculos são resolvíveis nesta semana, e são justamente os
de maior risco operacional imediato. Os demais exigem um plano de 30 a 60 dias e devem ser
tratados como **limitação conhecida e declarada** ao cliente, não como surpresa.
