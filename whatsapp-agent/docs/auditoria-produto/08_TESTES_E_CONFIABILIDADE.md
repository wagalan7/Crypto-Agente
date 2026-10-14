# 08 — Testes e Confiabilidade

---

## 1. Testes existentes

🔴 **Nenhum.** Busca por `test_*.py`, `*_test.py`, `tests/`, `conftest.py`, `pytest.ini`
e `tox.ini` em todo o projeto: **zero resultados**. Não há `.github/workflows` — **sem CI**.

Em um produto com 14.724 linhas, 148 rotas e 26 tabelas, **nada** protege contra
regressão. Para contexto: durante os últimos dois meses de operação foram publicadas 19
builds, várias delas corrigindo defeitos **descobertos pelo cliente final em produção**.

---

## 2. Verificações executadas nesta auditoria

Executei verificações **locais e não destrutivas**, sem tocar em produção. Distingo
claramente o que foi **executado** do que foi **apenas lido**.

### ✅ Executado de fato

| Verificação | Resultado |
|---|---|
| `python3 -m py_compile` em todos os módulos | ✅ Sem erro de sintaxe |
| Importação real do `agent.py` e renderização dos dois prompts | ✅ 19.693 e 11.124 caracteres, sem `NameError` |
| Renderização Jinja do `dashboard.html` (com e sem conexão automática) | ✅ Renderiza; JS validado com `node --check` |
| `_resolve_slot` — 9 casos | ✅ `slot_id` prevalece sobre índice conflitante |
| `_normalize_phone` — 15 casos, comparando antes × depois | ✅ Nenhum formato BR mudou; Portugal/EUA/Espanha preservados |
| Filtro de remetente não-paciente — 11 casos + 6 de fluxo | ✅ Grupo/status/canal barrados; paciente passa |
| Fallback de envio com servidor Evolution simulado — 4 cenários | ✅ Motivo real exposto; erro de esquema não vaza |
| Alcance do endpoint de webhook (HTTP externo) | ✅ `403` correto sem token |
| Estado do servidor Evolution (HTTP externo) | ✅ Responde, v2.3.7 |
| Leitura do código-fonte da Evolution 2.3.7 (GitHub) | ✅ Confirmou o descarte de mensagens não decifráveis |

### 📖 Apenas leitura de código (**não executado**)

Comportamento do LLM, fluxo real de agendamento ponta a ponta, sincronização com Google
Calendar/CalDAV, Stripe/Mercado Pago, envio de e-mail, backup para S3, isolamento entre
clientes sob ataque, concorrência real.

---

## 3. Análise de cenários críticos

| # | Cenário | Avaliação | Base |
|---|---|---|---|
| 1 | **Duas mensagens simultâneas do mesmo paciente** | 🟠 **Risco.** Cada webhook vira um `BackgroundTask` independente. Sem trava por telefone, duas respostas podem ser geradas com o mesmo contexto e gravar ações conflitantes | Leitura |
| 2 | **Webhook duplicado** | 🟢 **Resolvido.** `webhook_seen` persistente | Leitura |
| 3 | **Falha do provedor de IA** | 🟢 Groq → fallback Claude; JSON malformado → nova tentativa | Leitura |
| 4 | **Falha de conexão do WhatsApp** | 🔴 **Mal resolvido.** Já houve 7 dias mudo. O selo "Conectado" lê o estado do provedor, não o canal real; sem alerta | **Incidente real observado** |
| 5 | **Falha do calendário** | 🟢 *Fail-open*: erro logado, agendamento preservado; se o update falha, cria evento novo | Leitura |
| 6 | **Dois pacientes no mesmo horário** | 🔴 **Possível.** `has_conflict` e `create_appointment` em operações separadas, sem transação e **sem índice único** | Leitura |
| 7 | **Cancelamento ambíguo** | 🟠 **Risco.** Se o modelo emitir `cancel`, cancela. Sem confirmação explícita. Mitigado por ser cancelamento suave + notificação | Leitura |
| 8 | **Mensagens fora de ordem** | 🟠 Sem garantia de ordem; o contexto é relido a cada mensagem, então pode haver decisão sobre estado desatualizado | Leitura |
| 9 | **Perda de contexto** | 🟡 Janela de 8 mensagens; conversa longa perde o começo | Leitura |
| 10 | **Resposta indevida** | 🟡 Mitigado por atalhos determinísticos, correção forçada de data/hora e `_strip_forbidden_phrases` | Leitura |
| 11 | **Acesso a dados de outro cliente** | 🟢 Correto por desenho (tenant sempre do token). **Nunca testado** | Leitura |
| 12 | **Processo cai no meio do processamento** | 🔴 **Mensagem perdida.** `webhook_seen` já gravado impede o retry do provedor | Leitura |
| 13 | **Mídia recebida** | 🔴 **Descartada** no provedor de produção | Leitura + diagnóstico |

---

## 4. Defeitos confirmados em produção durante o período auditado

Esta lista é factual — todos foram observados, diagnosticados e corrigidos:

| Defeito | Causa raiz | Build |
|---|---|---|
| Salvar card Z-API derrubava o envio | 3 colunas compartilhadas entre provedores; o save trocava `whatsapp_provider` | 57 |
| Token da instância inválido → `HTTP 401` no envio | `create` com instância existente devolvia token vazio e o banco mantinha o antigo | 58 |
| Webhook em domínio sem certificado | `BASE_URL` apontava para o domínio raiz, sem TLS válido | 59 |
| Falha de gravação do webhook invisível | retorno de `set_webhook` descartado | 60 |
| Sessão "zumbi" por 7 dias | Canal morto com estado "aberto"; só restart do processo resolve | 62–63 |
| Agente marcava horário **diferente** do escolhido | Prompt mandava usar o número do display; backend indexava a lista completa | 64 |
| Erro de envio indecifrável | Fallback v1 sobrescrevia o motivo real | 65 |
| Status/canais tratados como paciente | Filtro só existia no caminho Z-API | 66 |
| Paciente de Portugal impossível | `55` colado em qualquer número | 67 |

**Padrão identificado:** a maioria é **falha silenciosa** — o sistema seguia aparentando
funcionar enquanto perdia mensagens ou gravava dados errados. Isso é consequência direta
da ausência de testes e de alertas.

> **Correção de registro:** ao propor a build 66 afirmei que mensagens de grupo estavam
> consumindo chamadas de IA e criando conversas falsas. Revisando o código,
> `_handle_message` **já barrava** grupos modernos (prefixo `120363`) e identificadores com
> mais de 15 dígitos. O ganho real da build 66 foi barrar **status/stories e
> canais/newsletters**, que de fato passavam pelo filtro antigo (`status@broadcast` vira
> `phone="status"`, que escapava de todas as condições). A correção é legítima, mas menor
> do que descrevi na hora.

---

## 5. Pontos fortes de confiabilidade

1. **Idempotência bem feita** nas automações (ver `05`)
2. ***Fail-open* consistente** — integrações externas falham sem derrubar o fluxo
3. **Dedup persistente** de webhook
4. **Cancelamento suave** — nada é apagado de verdade
5. **Diagnóstico embutido** que permitiu resolver o incidente de setembro sem acesso a logs
6. **Backup diário** automatizado

---

## 6. Recomendação mínima de testes

Se for para escrever apenas **um** conjunto de testes antes de vender, que seja este:

```
tests/
├── test_agendamento.py      # criar, remarcar, cancelar, conflito, slot_id
├── test_isolamento.py       # tenant A não acessa dados de tenant B
├── test_webhook.py          # token, dedup, grupos, mídia, fromMe
├── test_telefone.py         # _normalize_phone (BR e exterior)
└── test_scheduler.py        # confirmação e cobrança não duplicam
```

Esforço estimado: **2 a 3 dias**. Cobriria todos os defeitos da tabela do item 4 — ou
seja, teria evitado a maior parte do retrabalho dos últimos dois meses.
