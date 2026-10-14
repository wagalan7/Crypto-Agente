# 10 — Roadmap Recomendado

Esforço: **B**aixo (horas) · **M**édio (dias) · **A**lto (semanas)

---

# HORIZONTE A — Comercialização imediata (esta semana)

> Objetivo: poder vender de forma assistida sem risco de falha silenciosa.

### A1. Travar a versão da Evolution 🔴 Esforço: **B**
- **Problema:** imagem `latest`; quando a 2.4.0 virar estável, um redeploy puxa uma versão
  que exige licença e responde `503` em tudo.
- **Benefício:** elimina o risco de indisponibilidade total.
- **Solução:** fixar a tag/digest da imagem no serviço da Railway.
- **Dependências:** acesso à Railway (do cliente).
- **Riscos:** a troca dispara redeploy — fazer em horário calmo; pode pedir novo QR.
- **Aceite:** o serviço mostra versão fixa; um redeploy mantém a 2.3.7 e os clientes
  seguem conectados.

### A2. Alerta de conexão caída 🔴 Esforço: **B**
- **Problema:** 7 dias com o agente mudo sem nenhum aviso.
- **Benefício:** operação confiável; descobrir antes do cliente.
- **Solução:** estender `instance_monitor` com uma verificação **real do canal** (comando
  inofensivo que falha se o socket estiver morto) e disparar e-mail pelo `email_service`
  ao operador — e-mail, porque o WhatsApp é justamente o que estaria fora.
- **Dependências:** SMTP configurado (**NÃO VERIFICADO** se está).
- **Riscos:** falso positivo gerando ruído — usar 2 ou 3 falhas seguidas antes de alertar.
- **Aceite:** derrubar a conexão em teste gera e-mail em até 15 minutos.

### A3. Índice único em `appointments` 🔴 Esforço: **B**
- **Problema:** dupla reserva possível.
- **Benefício:** impossível dois pacientes no mesmo horário.
- **Solução:** índice único parcial em `(tenant_id, scheduled_at)` ignorando canceladas, e
  tratar a violação como conflito na camada de agendamento.
- **Dependências:** verificar duplicatas existentes antes de criar o índice.
- **Riscos:** se já houver duplicata, a migração falha — **checar antes**.
- **Aceite:** duas tentativas simultâneas no mesmo horário → uma sucede, outra recebe
  mensagem de conflito.

### A4. Tratar imagem e documento no caminho Evolution 🔴 Esforço: **M**
- **Problema:** paciente manda comprovante e ninguém vê.
- **Benefício:** acaba a perda silenciosa de mensagem.
- **Solução:** espelhar no `extract_message_evolution` o que o Z-API já faz
  (`__IMAGEM__`, `__DOCUMENTO__`), extraindo a URL da mídia do payload da Evolution.
- **Dependências:** formato de mídia da Evolution 2.3.7.
- **Riscos:** formato diferente do esperado — tratar como "não reconhecido" e **ao menos
  responder algo** ao paciente.
- **Aceite:** imagem enviada vira resposta + registro de comprovante; áudio recebe
  resposta de cortesia pedindo texto.

### A5. Ajustar a promessa comercial 🟠 Esforço: **B**
- **Problema:** material fala em secretário virtual e transcrição de áudio.
- **Benefício:** evita cancelamento por expectativa frustrada e exposição jurídica.
- **Solução:** revisar landing e material de venda; incluir no contrato a dependência de
  conexão não oficial do WhatsApp.
- **Aceite:** nenhuma promessa sem funcionalidade correspondente verificada.

### A6. Teste mínimo do núcleo 🔴 Esforço: **M**
- **Problema:** nada protege contra regressão.
- **Solução:** 5 arquivos de teste (ver `08`, item 6) + CI no push.
- **Aceite:** CI verde obrigatório antes de publicar.

---

# HORIZONTE B — Evolução competitiva (30 a 60 dias)

### B1. Base de conhecimento por consultório 🟠 Esforço: **M**
- **Problema:** agente não responde preço, convênio, endereço, política de cancelamento.
- **Benefício:** deixa de ser agendador e vira atendente — maior salto de valor percebido.
- **Solução:** tabela de perguntas e respostas por tenant, editável no painel, injetada no
  contexto; busca simples por palavra-chave já resolve no começo.
- **Riscos:** inchar o prompt — limitar aos itens relevantes.
- **Aceite:** profissional cadastra 10 perguntas e o agente responde todas corretamente.

### B2. Transcrição de áudio de verdade 🟠 Esforço: **M**
- **Solução:** extrair a URL do áudio no webhook da Evolution e ligar
  `transcribe_audio_groq`, que já existe.
- **Aceite:** áudio de 30s vira texto e segue o fluxo normal.

### B3. Medição de consumo e custo de IA 🟠 Esforço: **M**
- **Solução:** registrar tokens e provedor por mensagem, por tenant; painel de consumo;
  limite por plano com aviso antes do corte.
- **Aceite:** operador vê custo por cliente no mês e o sistema avisa em 80% do limite.

### B4. Criptografar conversas 🔴 Esforço: **M**
- **Problema:** dado sensível de saúde em texto puro.
- **Solução:** reusar o Fernet já existente para `conversations.content`, com migração.
- **Riscos:** perda de chave = perda de histórico — exige procedimento de custódia.
- **Aceite:** registros novos ilegíveis sem a chave; painel segue funcionando.

### B5. Consentimento e opt-out 🟠 Esforço: **M**
- **Solução:** registrar consentimento na primeira interação; reconhecer pedido de
  descadastro e respeitar.
- **Aceite:** "pare de me mandar mensagem" interrompe automações para aquele contato.

### B6. Resumo diário no WhatsApp 🟡 Esforço: **B**
- **Solução:** job às 7h usando o `insights_service`, que já calcula tudo.
- **Aceite:** profissional recebe agenda e pendências do dia.

### B7. Fuso horário por paciente 🟠 Esforço: **M**
- **Aceite:** paciente em Portugal recebe confirmação no horário local correto.

### B8. Caixa de entrada no painel 🟡 Esforço: **M**
- **Aceite:** profissional responde pelo painel sem abrir o WhatsApp.

---

# HORIZONTE C — Diferenciação avançada (60 a 180 dias)

### C1. Secretário executivo de verdade — Esforço: **A**
Comandos do dono por texto e áudio ("remarca a Maria pra quinta", "como está minha
semana?"), com caminho e permissões próprios.
**Aceite:** 20 comandos frequentes executados corretamente.

### C2. Lista de espera e encaixe automático — Esforço: **M**
Quando alguém cancela, o agente oferece o horário a quem está esperando.
**Aceite:** cancelamento gera oferta automática e o primeiro a aceitar fica com o horário.

### C3. Reativação de pacientes inativos — Esforço: **M**
O insight `no_return` já identifica quem sumiu; falta a campanha com aprovação humana.
**Aceite:** profissional aprova uma lista e o agente conduz o retorno.

### C4. Relatório mensal automático — Esforço: **M**
PDF com ocupação, faltas, receita e evolução, enviado todo mês.
**Aceite:** relatório chega sem intervenção.

### C5. API oficial do WhatsApp como opção — Esforço: **A**
Plano B para clientes que não aceitam risco de bloqueio.
**Aceite:** um cliente operando pela API oficial sem mudança no restante.

### C6. Múltiplos profissionais por consultório — Esforço: **A**
Agenda por profissional, com papéis.
**Aceite:** clínica com 3 profissionais opera com agendas separadas.

### C7. Avaliação contínua de qualidade do agente — Esforço: **M**
Conjunto de conversas de referência executado a cada alteração de prompt.
**Aceite:** alterar prompt mostra o efeito antes de publicar.

### C8. Escala horizontal — Esforço: **A**
Tirar o agendador do processo web, adotar fila real e avaliar Postgres.
**Aceite:** duas réplicas sem duplicar envio.

---

## Sequência sugerida

```
Semana 1   A1 · A2 · A3 · A5        (baixo esforço, elimina os riscos agudos)
Semana 2   A4 · A6                  (para a perda de mensagem e cria a rede de proteção)
Mês 1      B1 · B2 · B6             (salto de valor percebido)
Mês 2      B3 · B4 · B5 · B7        (sustentabilidade e conformidade)
Mês 3+     C conforme tração comercial
```

**Observação final:** o Horizonte A inteiro cabe em cerca de uma semana de trabalho e
muda o produto de "funciona mas pode falhar em silêncio" para "funciona e avisa quando
não funciona". É o pré-requisito honesto para vender.
