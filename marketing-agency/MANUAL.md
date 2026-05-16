# Manual de Funcionamento — ContentAI Agency

**URL de acesso:** https://content-agency-ai-production.up.railway.app

---

## Índice

1. [Acesso e Login](#1-acesso-e-login)
2. [Hierarquia de Usuários](#2-hierarquia-de-usuários)
3. [Clientes](#3-clientes)
4. [Dashboard](#4-dashboard)
5. [Agentes de IA](#5-agentes-de-ia)
6. [Conteúdo](#6-conteúdo)
7. [Calendário](#7-calendário)
8. [Analytics](#8-analytics)
9. [Authority Score](#9-authority-score)
10. [Fluxo de Trabalho Recomendado](#10-fluxo-de-trabalho-recomendado)
11. [Gerenciar Equipe](#11-gerenciar-equipe)

---

## 1. Acesso e Login

Acesse a plataforma em:
**https://content-agency-ai-production.up.railway.app**

### Usuários pré-cadastrados (Master)

| Email | Senha | Papel |
|-------|-------|-------|
| wagalan@gmail.com | @l61310788 | Master |
| brunaparolin6@gmail.com | 231981 | Master |

### Observações
- O token de sessão dura **30 dias** — não precisa fazer login toda vez
- Em caso de erro "Email ou senha incorretos", verifique maiúsculas/minúsculas na senha

---

## 2. Hierarquia de Usuários

A plataforma possui três níveis de acesso:

### Master
- Cria e gerencia todos os próprios clientes
- Cria contas de Admin e User
- Concede e revoga acesso de outros usuários a seus clientes
- Vê apenas seus próprios clientes (não vê clientes de outros Masters, a menos que haja permissão concedida)

### Admin
- Acessa apenas os clientes que um Master liberou para ele
- Pode usar todos os recursos (agentes, conteúdo, calendário, analytics)
- Não pode criar usuários nem conceder acessos

### User
- Igual ao Admin, mas com permissões de visualização mais limitadas conforme configuração
- Acessa apenas o que foi explicitamente liberado

---

## 3. Clientes

A tela inicial lista todos os seus clientes.

### Criar um novo cliente

Clique em **"+ Novo Cliente"** e preencha:

| Campo | Descrição | Exemplo |
|-------|-----------|---------|
| Nome * | Nome do cliente ou marca | "Thiago Fitness" |
| Nicho | Mercado de atuação | "Fitness, Emagrecimento" |
| Público-alvo | Descrição do cliente ideal | "Mulheres 30-45 anos que querem emagrecer sem academia" |
| Tom de voz | Como o cliente se comunica | "Direto, motivacional, sem enrolação" |
| Personalidade | Traços da marca/pessoa | "Autêntico, polêmico, provocador" |
| Posicionamento | Diferencial único | "Único método que emagrece sem dieta restritiva" |
| Plataformas | Onde está presente | Instagram, TikTok, YouTube... |
| Objetivos | O que quer alcançar | Crescer seguidores, Vender produto... |

> **Quanto mais completo o perfil, melhor a qualidade das saídas dos agentes.**

---

## 4. Dashboard

Ao clicar em um cliente, você entra no Dashboard com:

- **Authority Score** — pontuação 0-100 de autoridade digital do cliente
- **Métricas dos últimos 30 dias** — views, compartilhamentos, salvamentos, retenção
- **Próximos 7 dias** — resumo do calendário de publicações
- **Ações rápidas** — atalhos para as principais funcionalidades

### Indicador do Authority Score

| Cor | Faixa | Significado |
|-----|-------|-------------|
| Verde | 75–100 | Alta Autoridade |
| Violeta | 50–74 | Em Crescimento |
| Âmbar | 25–49 | Iniciando |
| Cinza | 0–24 | Sem dados suficientes |

---

## 5. Agentes de IA

Esta é a seção central da plataforma. Todos os agentes usam o modelo **Llama 3.3 70B** via Groq e geram resposta em tempo real (streaming).

### Como usar
1. Vá em **Agentes** no menu lateral
2. Escolha a aba do agente desejado
3. Preencha os campos (se houver)
4. Clique no botão de execução
5. A resposta aparece em tempo real — pode copiar quando terminar

---

### Agente 1 — Estratégia de Conteúdo

**O que faz:** Cria a estratégia editorial completa para o período.

**Entrada:** Clique em "Gerar Estratégia" (usa o perfil do cliente automaticamente)

**Saída:**
- Resumo do posicionamento
- Mix de conteúdo semanal (% autoridade / conexão / vendas)
- 5 pilares temáticos
- Distribuição semanal por objetivo
- Próxima ação imediata

**Quando usar:** No início de cada semana ou período de planejamento.

---

### Agente 2 — Roteiro (Script)

**O que faz:** Cria o roteiro completo de um vídeo com estrutura de retenção.

**Entradas:**
- Tema do vídeo (obrigatório)
- Formato: Reels, Shorts, YouTube, Carousel, Story, Post
- Plataforma
- Objetivo: Atrair, Conectar, Autoridade, Vender, Quebrar objeção

**Saída:**
- **HOOK (0–3s):** Primeira frase de impacto
- **CONTEXTO (3–10s):** Por que continuar assistindo
- **DESENVOLVIMENTO:** Corpo do vídeo com micro-retenções a cada 15–20s
- **CTA:** Chamada para ação final

---

### Agente 3 — Trends

**O que faz:** Analisa tendências e filtra as que fazem sentido para o cliente.

**Entrada:** Cole a lista de trends atuais (pode ser texto livre do TikTok, Instagram, etc.)

**Saída:**
- Top 3 trends com pontuação (relevância, alcance, alinhamento 0–10)
- Como adaptar cada trend ao cliente
- Trends para evitar (com justificativa)
- Oportunidade urgente (se a janela for curta)

---

### Agente 4 — Design

**O que faz:** Cria briefing visual completo para um conteúdo específico.

**Entradas:**
- Tema do conteúdo
- Formato (Carousel, Post, Story, Thumbnail)
- Plataforma
- Referências visuais (opcional)

**Saída:**
- Conceito criativo central
- Paleta de cores (com códigos hex)
- Estrutura slide a slide (para carousels)
- Tipografia, elementos de identidade
- 2 prompts em inglês para geração de imagem com IA (Midjourney/DALL-E)

---

### Agente 5 — Amplificador de Ideias

**O que faz:** Transforma uma ideia bruta em conceito estratégico de conteúdo.

**Entrada:** Escreva a ideia crua do cliente — pode ser vaga, informal, incompleta.

**Saída:**
- Ideia original (o que foi dito)
- Núcleo estratégico (o que realmente está sendo comunicado)
- Ângulo amplificado (versão elevada)
- Impacto emocional (dor/desejo que ressoa)
- Objetivo estratégico
- Formatos sugeridos (top 2 com justificativa)
- Sugestão de hook

**Exemplo:**
> *Entrada:* "quero falar sobre começar um negócio sem dinheiro"
> *Saída:* Ângulo estratégico sobre liberdade financeira com prova social, formato Reels, hook "Eu comecei com R$0 e hoje faturei..."

---

### Agente 6 — Analytics

**O que faz:** Analisa as métricas e gera insights estratégicos.

**Entrada:** Cole os dados de métricas (pode ser texto livre com os números)

**Saída:**
- Resumo de performance
- Top 3 conteúdos que performaram + motivo
- Padrões detectados (formato, horário, tipo de hook, CTA que converte)
- Insights acionáveis (3–5 recomendações diretas)
- Ajuste estratégico para a próxima semana
- Cálculo do Authority Score com justificativa

---

### Agentes Adicionais (via API)

Os agentes abaixo são acessíveis via API e integrados ao fluxo automático:

| Agente | Função |
|--------|--------|
| **Estrategista** | Funil de vendas, ICP, proposta de valor |
| **Copywriter** | Headlines, hooks, copy de anúncio, legenda |
| **Social Media** | Calendário 7 posts com formato/objetivo/legenda/CTA |
| **Design Director** | Identidade visual da marca (paleta, tipografia) |
| **Tráfego Pago** | Campanha de anúncios (público, copy, budget, KPIs) |
| **Automação** | Sequência de nutrição, captura de leads, remarketing |
| **Publisher** | Conteúdo final pronto para publicar com hashtags |

---

## 6. Conteúdo

Armazena todas as peças de conteúdo criadas para o cliente.

### Status do conteúdo

```
Pendente → Aprovado → Gravado → Publicado
```

| Status | Ação disponível |
|--------|----------------|
| Pendente | Aprovar |
| Aprovado | Marcar como Gravado |
| Gravado | Marcar como Publicado |

### Filtros disponíveis
- Todos
- Pendente
- Aprovado
- Gravado
- Publicado

### O que cada peça contém
- **Hook** — primeira frase de impacto
- **Roteiro** — script completo
- **Copy** — legenda e texto para post
- **Briefing de Design** — instruções visuais
- **Nota Estratégica** — observação interna
- **Plataforma + Formato + Objetivo**

---

## 7. Calendário

Organiza as publicações no tempo.

### Gerar semana
1. Escolha a **frequência** (3 a 7 posts por semana)
2. Selecione o **período** (7, 14 ou 30 dias)
3. Clique em **"Gerar semana"**
   - O sistema cria slots vazios com formatos e objetivos variados automaticamente

### Vincular conteúdo a um slot
- Clique no slot
- Escolha um conteúdo da lista para vincular

### Status dos slots

| Status | Significado |
|--------|-------------|
| Planejado | Slot criado, aguardando conteúdo |
| Pronto | Conteúdo gravado/editado, pronto para publicar |
| Publicado | Já foi ao ar |

### Visualizações
- **Lista** — disponível em mobile e desktop
- **Grade** — calendário semanal colunar (somente desktop)

---

## 8. Analytics

Registra e analisa as métricas de performance.

### Adicionar métricas
Clique em **"Adicionar Métricas"** e informe:

| Campo | Tipo |
|-------|------|
| Plataforma | Instagram, TikTok, YouTube... |
| Views | Número |
| Likes | Número |
| Comentários | Número |
| Compartilhamentos | Número |
| Salvamentos | Número |
| Alcance | Número |
| Retenção | % |
| CTR | % |
| Conversão | % |

> Ao salvar, o **Authority Score é recalculado automaticamente**.

### O que você vê
- Totais do período selecionado (7d / 14d / 30d)
- Médias de retenção, CTR e conversão
- Barras comparativas de engajamento
- Histórico cronológico das últimas 50 entradas

---

## 9. Authority Score

**Escala de 0 a 100** que mede o nível de autoridade digital do cliente com base nos últimos 30 dias de métricas.

### Como é calculado
Pondera automaticamente:
- Volume de views (30%)
- Taxa de engajamento — likes + comentários + shares + saves (30%)
- Taxa de retenção média (20%)
- Taxa de conversão (20%)

### Como melhorar o score
1. Publique com consistência (use o calendário)
2. Crie conteúdos com alto valor (use os agentes)
3. Monitore padrões (use o agente de Analytics)
4. Registre as métricas regularmente

---

## 10. Fluxo de Trabalho Recomendado

### Semana padrão de produção de conteúdo

```
Segunda-feira — Planejamento
├── Agentes → Estratégia: gerar estratégia da semana
├── Agentes → Trends: filtrar tendências relevantes
└── Calendário → Gerar semana (5x/semana)

Terça-feira — Criação
├── Agentes → Amplificador: elevar ideias brutas
├── Agentes → Roteiro: criar scripts dos vídeos
└── Agentes → Design: briefing visual

Quarta-feira — Produção
├── Conteúdo: criar peças com hook + roteiro + copy
└── Calendário: vincular conteúdos aos slots

Quinta-feira — Revisão
└── Conteúdo → Aprovar peças (Pendente → Aprovado)

Sexta-feira — Publicação
├── Conteúdo → Marcar como Gravado → Publicado
└── Analytics → Registrar métricas das peças publicadas anteriormente

Domingo — Análise
├── Agentes → Analytics: analisar métricas da semana
└── Dashboard → Verificar evolução do Authority Score
```

---

## 11. Gerenciar Equipe

Disponível apenas para usuários **Master**.

### Criar novo usuário
Via API (temporariamente):
```
POST /auth/users
{
  "email": "colaborador@email.com",
  "password": "senha123",
  "name": "Nome",
  "role": "admin"  // ou "user"
}
```

### Conceder acesso a um cliente
```
POST /auth/grant-access
{
  "client_id": 1,
  "user_id": 2
}
```

### Revogar acesso
```
DELETE /auth/revoke-access
{
  "client_id": 1,
  "user_id": 2
}
```

> Interface de gerenciamento de equipe diretamente na plataforma está planejada como próxima funcionalidade.

---

## Dicas de uso

- **Perfil completo = melhores saídas:** Quanto mais detalhado o perfil do cliente (tom, personalidade, posicionamento), mais preciso será o resultado dos agentes.
- **Use o Amplificador antes do Roteiro:** Ele eleva a ideia bruta para um ângulo estratégico que alimenta melhor o roteiro.
- **Registre métricas toda semana:** O Authority Score e o Agente de Analytics dependem de dados consistentes para gerar insights valiosos.
- **Calendário primeiro:** Gere a semana no calendário antes de criar conteúdos — os slots guiam quais formatos e objetivos produzir.
- **Siga o status do conteúdo:** O fluxo Pendente → Aprovado → Gravado → Publicado mantém o time alinhado sobre o que está pronto.

---

*ContentAI Agency — Plataforma de Autoridade Digital*
*Versão 2.0 — Maio 2026*
