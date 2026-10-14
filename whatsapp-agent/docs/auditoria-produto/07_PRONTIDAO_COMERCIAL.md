# 07 — Prontidão Comercial

> Evidências: `main.py` (onboarding, checkout, webhooks), `stripe_service.py`,
> `mp_service.py`, `database.py`, `crypto.py`, `backup_service.py`, `templates/termos.html`.

---

## 1. Arquitetura multiempresa

✅ **Sim, de verdade.** Tabela `tenants` com `slug` único; todas as demais tabelas
carregam `tenant_id`; as queries sempre o recebem. Não há nada hard-coded por cliente.

O tenant é resolvido por:
- `slug` na URL do webhook e do dashboard
- `dashboard_token` (comparado com `hmac.compare_digest`)

---

## 2. Dados e configurações por cliente

✅ Cada consultório tem seus próprios: horários, dias e bloqueios, duração de sessão,
provedor e credenciais de WhatsApp, chave PIX, textos de mensagem, modelo de contrato e
configurações, plano e status, integrações de calendário, segmento e rótulos.

---

## 3. Cadastrar cliente novo sem mexer em código

✅ **Sim.** `POST /onboarding/create` cria tudo: slug, tokens, status de pagamento,
registro em `audit_log`. Também há `POST /admin/tenants` (protegido por MASTER_KEY).

---

## 4. Números de WhatsApp diferentes por cliente

✅ **Sim.** Cada tenant tem instância própria. No modo automático, a instância é criada
como `tenant-{slug}` no servidor Evolution e o webhook aponta para
`/webhook/{slug}/evolution` com token exclusivo.

🟠 **Ressalva de escala:** todas as instâncias vivem no **mesmo** servidor Evolution, com
**uma réplica**. É ponto único de falha — quando ele trava, **todos** os clientes param.
Foi o que aconteceu no incidente de setembro.

---

## 5. Onboarding

| Etapa | Situação |
|---|---|
| Formulário | ✅ Dados do consultório + faturamento + aceite de termos |
| Criação da conta | ✅ Automática |
| Pagamento | ✅ Stripe ou Mercado Pago |
| Ativação | ✅ Por webhook do provedor |
| Entrega do acesso | ✅ Link com token |
| **Conectar o WhatsApp** | 🔴 **Ponto de atrito grave** (ver `06`) |
| Recuperação de acesso | ✅ `/recuperar-acesso` por e-mail |

---

## 6. Planos, limites e consumo

| Item | Situação |
|---|---|
| Planos (mensal/semestral/anual) | ✅ `STRIPE_PRICE_*`, coluna `plan` |
| Vencimento | ✅ `plan_expires_at` |
| Suspensão por inadimplência | ✅ `status='suspended'` → agente para e painel redireciona |
| Acesso gratuito/cortesia | ✅ `free_until`, `is_tenant_exempt` |
| **Limite de mensagens por plano** | ❌ **Não existe** |
| **Limite de pacientes** | ❌ Não existe |
| **Medição de consumo de IA** | ❌ **Não existe** |
| **Custo por cliente** | ❌ Não existe |

🔴 **Risco comercial direto.** Não há contagem de tokens nem de mensagens. Um cliente de
alto volume consome Groq, Anthropic e infraestrutura **sem nenhum teto e sem
visibilidade**. Os planos vendem acesso, não capacidade — e não existe dado para
diferenciá-los nem para detectar abuso.

---

## 7. Pagamentos

✅ **Stripe** (`stripe_service.py`): checkout, webhook, portal do cliente.
✅ **Mercado Pago** (`mp_service.py`): checkout e webhook — essencial no Brasil (PIX).
✅ Carrinhos abandonados visíveis no painel, com lembrete manual.

**NÃO VERIFICADO:** validação de assinatura dos webhooks de pagamento. Se o webhook da
Stripe não verificar a assinatura, um terceiro poderia ativar contas. **Recomendo
conferir antes de vender.**

---

## 8. Rastreabilidade

| Evento | Registrado |
|---|---|
| Criação de tenant | ✅ `audit_log` |
| Login admin (sucesso/falha/lockout/TOTP) | ✅ |
| Recuperação de link | ✅ |
| Cancelamento de consulta | ✅ `cancellation_log` |
| Cobrança enviada | ✅ `billing_logs` |
| Contrato (envio, assinatura, IP, hash) | ✅ |
| **Ações da profissional no painel** | ❌ Não registradas |
| **Mensagens enviadas pelo agente** | 🟡 Em `conversations`, sem status de entrega |

---

## 9. Segurança, privacidade e LGPD

### O que existe
- ✅ Credenciais e chave PIX **cifradas em repouso** (Fernet, `crypto.py`), transparente
  no `database.py`
- ✅ `/admin/*` e `/test/*` exigem MASTER_KEY, *fail-closed*
- ✅ Painel com 2FA TOTP, lockout e sessões com expiração
- ✅ Rate limiting por IP (login, onboarding, webhook)
- ✅ Dedup persistente de webhook
- ✅ Páginas de Termos e Privacidade
- ✅ Aceite de termos versionado
- ✅ Backup para S3/R2/B2
- ✅ Diagnóstico mascara tokens e número pareado

### Lacunas de LGPD

| # | Lacuna | Gravidade |
|---|---|---|
| 1 | **Sem consentimento do paciente** para receber mensagens automáticas | 🔴 Alta |
| 2 | **Sem opt-out** — "pare de me mandar mensagem" não é tratado | 🔴 Alta |
| 3 | **Sem política de retenção** — conversas e dados ficam indefinidamente | 🟠 Média |
| 4 | **Sem exclusão a pedido do titular** — há exclusão por paciente no painel, mas não um fluxo formal de direito do titular | 🟠 Média |
| 5 | **Conteúdo de conversas em texto puro** no banco (`conversations`) — são dados de saúde, categoria sensível | 🔴 Alta |
| 6 | **Sem registro de operações de tratamento** | 🟠 Média |
| 7 | **Token do painel não expira** e vai na URL | 🟠 Média |
| 8 | Papel de operador × controlador não documentado no contrato | 🟠 Média |

🔴 **Item 5 merece destaque:** o produto trata conversas entre psicólogo e paciente —
**dado pessoal sensível** pelo art. 5º, II da LGPD. Credenciais são cifradas, mas o
**conteúdo clínico não é**. Para um produto vendido a psicólogos, isso é
desproporcional e é o maior risco jurídico identificado.

---

## 10. Gestão de credenciais

- Credenciais de cliente: cifradas no banco (Fernet com `MASTER_KEY`)
- Credenciais da plataforma: variáveis de ambiente na Railway
- Rotação de chave: ❌ não há procedimento
- `FIELD_ENCRYPTION_KEY` ausente → aviso em log e gravação **em texto puro** (observado ao
  importar o módulo localmente). **NÃO VERIFICADO** se está definida em produção — se não
  estiver, a criptografia não está ativa.

---

## 11. Demonstração sem comprometer produção

🟡 **Parcial.** Existe `whatsapp_provider='mock'` (só loga) e `POST /test/{slug}/message`
para simular mensagem — bom para demonstrar o fluxo conversacional.

❌ Não há ambiente de homologação separado, nem dados de demonstração prontos
(`seed.py` existe mas é um seed básico). Demonstrar hoje significa criar um tenant no
**mesmo** banco de produção.

---

## 12. Obstáculos para comercializar imediatamente

### 🔴 Bloqueadores

1. **Mídia descartada no provedor de produção** — paciente manda áudio/imagem/PDF e
   ninguém vê. É perda de mensagem de cliente final.
2. **Sem alerta de queda** — já houve 7 dias de agente mudo sem ninguém saber.
3. **Imagem `latest` da Evolution** — risco de indisponibilidade total a qualquer redeploy.
4. **Conversas clínicas sem criptografia** — exposição jurídica desproporcional.
5. **Sem testes automatizados** em produto que muda toda semana.

### 🟠 Riscos sérios (não impedem venda assistida)

6. Dupla reserva possível
7. Fuso horário fixo (já há paciente em Portugal)
8. Sem consentimento/opt-out do paciente
9. Sem medição de consumo de IA
10. Ponto único de falha no servidor Evolution
11. Validação de webhook de pagamento não verificada

### 🟡 Ajustes de posicionamento

12. **Não vender como "secretário virtual"** — essa capacidade não existe
13. **Não prometer transcrição de áudio** — não está ligada
14. Declarar no contrato a dependência de conexão não oficial do WhatsApp

---

## 13. Veredito

**Vender para muitos clientes nesta semana: NÃO.**
**Vender de forma assistida, para poucos clientes selecionados: SIM, com condições.**

Condições mínimas:
- resolver bloqueadores 2 e 3 (baixo esforço, alto impacto)
- tratar ou declarar o bloqueador 1
- ajustar a promessa comercial (itens 12 a 14)
- combinar acompanhamento próximo nas primeiras semanas

A base multiempresa, a cobrança e o núcleo de agendamento **estão prontos**. O que não
está pronto é a **operação**: perceber falhas, garantir que nada se perca e provar que
alterações não quebram o que funciona.
