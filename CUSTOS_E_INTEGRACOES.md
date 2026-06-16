# Custos & Integrações — Crypto Win

> Mapa das aplicações externas que o app usa e estimativa de custo mensal.
> Atualizado em 2026-06-15. **Valores são ESTIMATIVAS** — confirme nas faturas
> reais de Railway e do provedor do proxy.

---

## 1. Integrações externas (o que conecta no app)

### 💰 Pagos / com custo real

| Serviço | Pra que serve | Tem chave/login? |
|---|---|---|
| **Railway** | Hospeda o backend (FastAPI) **+ o PostgreSQL** | sim (DATABASE_URL) |
| **VPS / Proxy** (`168.144.132.241:8888`) | Proxy pra acessar a Binance Futures (contorna geo-bloqueio) | sim (BINANCE_PROXY_URL) |
| **Binance Futures** (mainnet) | Exchange onde o bot opera **dinheiro real** | sim (BINANCE_API_KEY/SECRET) |
| **Anthropic (Claude API)** | IA — **fallback legado** (só dispara se o Groq falhar) | sim (ANTHROPIC_API_KEY) |

### 🆓 Grátis / incluídos / desligados

| Serviço | Pra que serve | Custo |
|---|---|---|
| **Groq (LLM)** | IA **principal** das análises | Grátis (free tier) |
| **OKX** | Dados de mercado dos scans (preços/perpétuos públicos) | Grátis (sem chave) |
| **Yahoo Finance** (`yfinance`) | Dados macro | Grátis |
| **Telegram Bot** | Notificações (trade, kill-switch) | Grátis |
| **Web Push / VAPID** | Push no navegador | Grátis |
| **Bybit** (testnet) | Exchange secundária, só testnet — sem dinheiro real | Grátis |
| **Vercel** | Hospeda o frontend (React) | Grátis (Hobby) |
| **GitHub** | Repositório + auto-deploy | Grátis |
| **Sentry** | Monitoramento de erros — **DESLIGADO** (sem DSN) | Não usado |

---

## 2. Estimativa de custo mensal (dos pagos)

| Serviço | Faixa estimada/mês | Observação |
|---|---|---|
| **Railway** (backend + Postgres) | **US$ 10 – 25** | Hobby = US$ 5 base + uso. Backend sempre ligado + DB pequeno. Confirme na fatura. |
| **VPS / Proxy** | **US$ 3 – 10** | Depende do provedor do proxy. Custo fixo. |
| **Anthropic (fallback)** | **US$ 0 – 5** | Quase zero hoje, porque o Groq é o principal. Só gasta se o Groq cair. |
| **Binance — taxas de trading** | **variável** (ver fórmula abaixo) | Não é mensalidade; é por operação. |
| **Vercel** | US$ 0 | Plano Hobby. Pago só se virar Pro (US$ 20). |

**Fixo estimado (sem contar taxas da Binance e o capital): ~US$ 13 – 40/mês.**

### Taxas da Binance Futures (por operação)
- Taxa *taker* ≈ **0,05% por lado** → **~0,10% por trade** (entrada + saída) sobre o **nocional** (tamanho da posição, já com alavancagem).
- Volume medido: **~34 trades reais / mês**.
- **Fórmula:** `custo ≈ nocional_médio × 0,10% × 34`
- **Exemplo:** nocional médio de US$ 200/trade → `200 × 0,001 × 34 ≈ US$ 6,8/mês`. Dobra o nocional, dobra a taxa.
- Dá pra reduzir ~10% pagando taxa em BNB (se ativar).

> ⚠️ As taxas escalam com o tamanho da posição (que sobe quando você aumenta o
> `LIVE_SIZE_MULT`). Hoje em 0.50.

---

## 3. Vencimentos / lembretes automáticos

> Avisos enviados **pelo Telegram** (serviço `reminders_service.py`, checagem
> diária 10:00 BRT). Pra editar/adicionar é só mexer na lista `RENEWALS`.

| O que vence | Provedor | Dia | Ciclo | Aviso |
|---|---|---|---|---|
| **VPS / Proxy** (Binance) | Digital Clean | dia 01 | mensal | **5 dias antes** (próx.: 26/06) |
| **Revisão de fatura** | Railway + Anthropic | dia 01 | mensal | no próprio dia |

- O aviso do **proxy** é o crítico: se a VPS cair, o bot perde acesso à Binance.
- A **revisão de fatura** é só um lembrete pra conferir uso/custo do Railway e da
  Anthropic no começo do mês.

---

## 4. Resumo pra controle

Saem do bolso todo mês: **Railway**, **VPS do proxy**, **taxas da Binance** (por
trade) e — só eventualmente — **Anthropic**. Todo o resto está em tier grátis e o
Sentry está desligado.

**Pontos de atenção de custo:**
1. Railway é o maior custo fixo — vale olhar a fatura real e o gráfico de uso.
2. O proxy é um custo fixo pequeno, mas é um **ponto único de falha** (se cair, o
   bot perde acesso à Binance).
3. As taxas da Binance crescem junto com o `LIVE_SIZE_MULT` e com a frequência de
   trades.
