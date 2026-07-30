# 🤖 Agente de IA Crypto — Prompt / Especificação Completa

> Documento de referência das funcionalidades do app, da inteligência do bot e da execução.
> Gerado a partir de leitura direta do código-fonte (branch `feat/decouple-exec-universe`).

---

## 1. Visão geral & propósito

Agente autônomo de trading de criptomoedas em **Binance Futures (mainnet, dinheiro real)**. Ele **escaneia o mercado**, **gera recomendações** (direção / entrada / alvos / stop / tier), **executa trades reais** (ou shadow), **gerencia a posição** (TP parcial, break-even, runner) e **aprende continuamente** com o próprio histórico — recalibrando probabilidades e rotacionando o universo de moedas operáveis.

**Filosofia central:** operar **a favor da tendência** de timeframes superiores, com **qualidade sobre quantidade**, tamanho de posição derivado de risco fixo, e melhoria contínua guiada por dados reais.

---

## 2. Arquitetura & infraestrutura

- **Backend:** FastAPI async (Python), dois serviços Railway separados compartilhando **um único Postgres**:
  - **WEB** (`uvicorn`): API, scan loop ao vivo (~90s), execução e gestão de trades.
  - **WORKER** (`sweep_worker.py`, config `backend/railway.worker.toml`, região Singapura junto ao Postgres): roda o **sweep de backtest** isolado, para não competir pelo GIL com o WEB. Persiste progresso na tabela `sweep_progress`.
- **Frontend:** React 18 + Vite + Tailwind + Lightweight-Charts (Vercel). Painéis: HomeCockpit, RecommendationsPanel, DashboardPanel, TradeManager, SweepPanel etc.
- **Exchange:** Binance Futures via CCXT. Flags `EXCHANGE_SHADOW` (paper) e `LIVE_TRADING_CONFIRM` (trava para ordens reais).
- **Modelos DB:** `RecommendationSnapshot`, `RealTrade`, `SweepProgress`, `RiskState`, `CalibrationVersion`, `RotationState`, `SymbolBacktestStats`, entre outros.

---

## 3. Inteligência do bot — geração de sinal

### 3.1 Indicadores (`indicator_service`)
Por candle: **RSI, MACD, EMAs (9/21/50), Bollinger, Stochastic, SuperTrend, ADX** etc. `get_indicator_signals` produz 6 votos direcionais:
- RSI (mean-reversion: >70 vende, <30 compra)
- MACD (tendência)
- EMA trend (stack 9>21>50 bull / inverso bear)
- Bollinger (mean-reversion nas bandas)
- Stochastic (mean-reversion >80/<20)
- SuperTrend (tendência)

### 3.2 Direção (`determine_direction`)
`total = score_indicadores + score_padrões×2`. `>1.0` → **LONG**, `<-1.0` → **SHORT**, senão **NEUTRAL**. Padrões pesam dobrado.

### 3.3 Padrões (`pattern_service`)
Padrões de candlestick/preço (reversão e continuação) alimentam o `determine_direction`.

### 3.4 Smart Money Concepts — IMPLEMENTADO (`smc_service`)
Order blocks, liquidez, estrutura de mercado — camada de confluência.

### 3.5 Multi-Timeframe Alignment — IMPLEMENTADO (`mtf_service`)
`analyze_mtf` confere alinhamento com 1-2 TFs superiores (mapa `MTF_MAP`, ex. 1h→[4h,1d]). Retorna `MTFAlignment` com `alignment_score` (-1 a +1), contagem a favor/contra/neutros e, por TF, `ema_aligned` (bullish/bearish/mixed via stack EMA9/21/50).

### 3.6 Derivativos — IMPLEMENTADO (`derivatives_service`)
Funding rate, open interest etc. como sinal contextual.

### 3.7 Confluência, score & tiers — IMPLEMENTADO
Sinais combinados em score de confluência; recomendações recebem **tier A+ / A / B**. Dois caminhos:
- **Batch:** `_process_symbol` / `_best_tf_for_symbol` / `get_recommendations`.
- **Live server-scan (~90s):** `_best_tf_for_symbol_server` / `get_recommendations_via_vision`.
- `SCAN_TFS = ["15m","1h","4h"]`.

### 3.8 Correção anti-contra-tendência (deploy `6f6575b8`)
**Problema:** streak de 7-8 stops por viés direcional (25 shorts × 5 longs em 30d). Causa: lógica "fade the pump" (3 sinais mean-reversion viram SHORT em moeda sobrecomprada) + `SHORT_BRAKE` cego (só BTC +6%/3d) + MTF inerte no caminho ao vivo (`_analyze_symbol_tf_server` fixava `mtf=None`).

**Fix — CT brake por símbolo** (`regime_service.symbol_counter_trend` + `recommendation_service._attach_htf_ema_trend`):
- Deriva tendência do TF superior pelo **stack de EMAs** dos TFs já escaneados (15m/1h/4h) — **zero fetch extra**, imune ao fade.
- Freia (por padrão **rebaixa tier**, não descarta) shorts contra TFs superiores EMA-bullish e longs contra EMA-bearish. Preserva **quantidade** via penalidade de seleção em vez de dropar o símbolo.
- Envs: `CT_BRAKE_ENABLED=1`, `CT_BRAKE_MIN_TFS=1`, `CT_BRAKE_BLOCK=false`, `CT_BRAKE_SELECT_PENALTY=12.0`.
- Ladder: A+→A / A→B / B→descarta (só se `CT_BRAKE_BLOCK=true`).

---

## 4. Execução & gestão de trade

### 4.1 Entrada
Ordem MARKET ou MAKER; define SL e alvos. Bracket com **TP1 (parcial 45%)**, **TP2** e **runner**.

### 4.2 Pipeline de sizing
12+ multiplicadores encadeados; caps duros `MAX_MARGIN_PCT_PER_TRADE=15`, `MAX_RISK_PCT_HARD=2`. Alavancagem via `set_leverage` com retries. Filler fora da allowlist a **0,75×** (só slot ocioso, tier A/A+; desliga em 350 moedas).

### 4.3 Gestão da posição (`trade_manager`, poll ~15s)
- **TP1** → parcial + move stop para **break-even**.
- **TP2** → realiza mais; runner segue.
- Time-stops e expiração.

### 4.4 Guards de risco
Same-direction guard, flip-advisory, caps de portfólio/cluster, **SL cooldown 4h**, **daily breaker**, **circuit breaker**, emergency close, alertas Telegram.

### 4.5 Shadow vs Real
`EXCHANGE_SHADOW` simula; `LIVE_TRADING_CONFIRM` habilita ordens reais.

---

## 5. Autoaprendizado, calibração, rotação & regime

- **Learning (`learning_service`):** buckets por contexto; `MIN_SAMPLE=5`, `AUTO_ADJUST` cap ±25%; bloqueia WR≤30%, boost WR≥65%.
- **Calibração (`calibration_service`):** bins + shrinkage (K=10) + isotônica (PAV) sobre `p_global`; versionada.
- **Edge decay:** janela 60d vs 14d; multiplicador com piso 0.5.
- **Rotação (`rotation_service`):** `auto_apply=true`, máx 350, histerese 3 ciclos, aplica a cada 6h (21600s), `backtest_seed` (tf=4h, max 15, min_trades 60, min_calib 0.5).
- **Regime (`regime_service`):** RISK_OFF, ALT_DANGER, BTC_DOMINANT, ALT_RISK_OFF, SHORT_BRAKE, **CT_BRAKE**.
- **Snapshot lifecycle:** `open` → `won_tp1` / `won_tp2` / `be` / `lost` / `expired`. `realized_r`: TP1=+0.6, TP2=+1.5, stop=-1.0.
- **Backtest sweep (walk-forward):** `CALIBRATION_FACTOR=0.70` (informativo/teto de sizing, nunca EV isolado). News blackout FOMC (30/60min).

---

## 6. API & frontend

- **Endpoints:** recomendações, trades, backtest/universe (`/status`, `/ranking`), rotação (`/state`), risco/regime, calibração.
- **Scan loop:** ~90s no WEB.
- **Frontend:** cockpit ao vivo, painel de recomendações (tier/entrada/alvos), gestor de trades, painel de sweep, dashboards de performance.
