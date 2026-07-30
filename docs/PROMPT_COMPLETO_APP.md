# 🤖 Agente de IA Crypto — Especificação Técnica Completa

> Documento de referência EXAUSTIVO das funcionalidades do app, da inteligência do bot e da execução.
> Gerado a partir de leitura direta do código-fonte (branch `feat/decouple-exec-universe`), com valores, thresholds e fórmulas reais e citações `arquivo:linha`.
> Todos os parâmetros são configuráveis por variável de ambiente (ENV) sem redeploy — crítico para operação em dinheiro real.

---

## 1. Visão geral & propósito

Agente autônomo de trading de criptomoedas em **Binance Futures (mainnet, dinheiro real)**. Ele **escaneia o mercado**, **gera recomendações** (direção / entrada / alvos / stop / tier), **executa trades reais** (ou shadow), **gerencia a posição** (TP parcial, break-even estrutural, runner) e **aprende continuamente** com o próprio histórico — recalibrando probabilidades e rotacionando o universo de moedas operáveis.

**Filosofia central:** operar **a favor da tendência** de timeframes superiores, com **qualidade sobre quantidade**, tamanho de posição derivado de **risco fixo**, e melhoria contínua guiada por **dados reais**.

---

## 2. Arquitetura & infraestrutura

- **Backend:** FastAPI async (Python 3.11), dois serviços Railway separados compartilhando **um único Postgres**:
  - **WEB** (`uvicorn main:app`): API, scan loop ao vivo (~90s), execução e gestão de trades. `restartPolicyType=always` (`backend/railway.toml`).
  - **WORKER** (`sweep_worker.py`, config `backend/railway.worker.toml`, região Singapura junto ao Postgres): roda o **sweep de backtest** isolado, para não competir pelo GIL com o WEB. Persiste progresso na tabela `sweep_progress`.
- **Exchange:** Binance Futures. Camadas: `binance_signed_service.py` (ordens assinadas), `shadow_trade_service.py` (orquestração/sizing/guards), `trade_manager_service.py` (gestão pós-abertura).
- **Flags de execução:** `EXCHANGE_SHADOW` (paper, default `true`) e `LIVE_TRADING_CONFIRM` (string exata `ENTENDO_RISCO_DINHEIRO_REAL` para liberar ordem real).
- **Frontend:** React 18 + Vite + Tailwind + Lightweight-Charts (Vercel). Painéis: HomeCockpit, RecommendationsPanel, DashboardPanel, TradeManager, SweepPanel etc.
- **Persistência (modelos DB):** `RecommendationSnapshot`, `RealTrade`, `SweepProgress`, `RiskState`, `CalibrationVersion`, `RotationState`, `SymbolBacktestStats`, entre outros.
- **IA (opcional):** Claude para análise textual contextual e coach PNL; fail-soft se `ANTHROPIC_API_KEY` ausente.

---

## 3. Inteligência do bot — geração de sinal

### 3.1 Indicadores (`indicator_service.py`)

| Indicador | Período/Param | Linha |
|---|---|---|
| RSI | 14 | `:25` |
| MACD | slow 26, fast 12, signal 9 | `:28` |
| Bollinger Bands | window 20, dev 2 | `:34` |
| EMA (campo `ema9`) | **12** | `:40` |
| EMA (campo `ema21`) | **26** | `:41` |
| EMA50 | 50 | `:42` |
| EMA200 | 200 (se len≥200) | `:43` |
| ATR | 14 | `:46` |
| ADX | 14 | `:49` |
| Stochastic | window 14, smooth 3 | `:52` |
| OBV | — | `:57` |
| Volume médio | 20 | `:60` |
| SuperTrend (manual) | período 10, mult 3.0 | `:130-131` |
| Pivot high/low | máx/mín dos últimos 20 candles | `:89-90` |

> ⚠️ Detalhe importante: os campos são nomeados `ema9`/`ema21`, mas computam **EMA(12)** e **EMA(26)** respectivamente.

**Anti-chase (deslocamento das últimas 3 velas):** `displacement_3c = (c_now − c_prev3)/c_prev3` (`:73`); `displacement_3c_atr = (c_now − c_prev3)/atr` (`:74`); `atr_pct = atr/c_now` (`:83`).

### 3.2 Votos de indicadores — `get_indicator_signals` (6 votos)
`signal_service.py:172-204`:

| Voto | Bullish (+1) | Bearish (−1) | Neutro (0) |
|---|---|---|---|
| RSI | < 30 | > 70 | 30–70 |
| MACD | macd > signal | macd < signal | — |
| EMA trend | ema9>ema21>ema50 | ema9<ema21<ema50 | entrelaçadas |
| Bollinger | preço ≤ bb_lower | preço ≥ bb_upper | entre bandas |
| Stochastic | K<20 e D<20 | K>80 e D>80 | resto |
| SuperTrend | direção = 1 | direção = −1 | — |

### 3.3 Direção — `determine_direction` (`signal_service.py:110-126`)
```
score = soma dos 6 votos                       # −6 .. +6
pattern_score = soma das confianças (top-3 padrões, LONG − SHORT)
total = score + pattern_score × 2.0
total > +1.0 → LONG | total < −1.0 → SHORT | senão → NEUTRAL
```
Padrões pesam **dobrado**.

### 3.4 Confirmação de candle (`signal_service.py:13-18, 67-107`)
- `REQUIRE_CANDLE_CONFIRMATION = True` (default).
- `CANDLE_CONFIRM_CLOSE_POS = 0.55` — LONG exige `close>open` e fechamento ≥55% do range; SHORT exige `close<open` e ≤45%. Falha → rebaixa para NEUTRAL.

### 3.5 Padrões gráficos (`pattern_service.py`)
`detect_all_patterns` roda 6 detectores e mantém **top-8 por confiança** (`:425-448`). Confianças-base por padrão:

| Padrão | Confiança-base | Critério-chave |
|---|---|---|
| LTA / LTB (trendlines) | r² (máx 0.99) | r²>0.7 |
| Canais (asc/desc/horizontal) | (h_r²+l_r²)/2 (máx 0.99) | r²>0.7 ambas |
| Triângulos (asc/desc/simétrico) | média r² (máx 0.97) | largura canal <0.05 |
| Wedges (asc/desc) | média r² (máx 0.95) | slopes na mesma direção |
| Double top/bottom | **0.82** | topos/fundos <2% de diferença |
| Head & Shoulders (+ inverso) | **0.78** | ombros <3% de diferença |
| Bull/Bear flag | **0.72** | impulso >5%, consolidação <3% |

**Breakout confirm** (`:350-422`): fecha além do nível-chave com tolerância 0,05% e **volume ≥1,2× média(20)** → seta `breakout_confirmed`. **Retest re-arm** (`:451-573`, flag `RETEST_REARM_ENABLED`): memoriza rompimento por `RETEST_MEMORY_BARS=10` velas, tolerância de toque `RETEST_TOL_PCT=0.4%`.

### 3.6 Smart Money Concepts — `smc_service.py`
- **Order Blocks** (`:82-99`): candle bearish seguido de ≥2 bullish com movimento >0,5%.
- **FVG** (`:143`): gap `candle[i-1].high < candle[i+1].low`.
- **Liquidity Sweeps** (`:192-225`): rompe swing high/low com pavio e fecha contrário (lookback 3 candles).
- **BOS/CHoCH + trend bias** (`:232-289`): HH+HL = bull, LH+LL = bear. BOS score 8, CHoCH score 6.

### 3.7 Multi-Timeframe (MTF) — `mtf_service.py`
`MTF_MAP` (`:24-36`): 1m→[5m,15m]; 5m→[15m,1h]; 15m→[1h,4h]; 30m→[1h,4h]; 1h→[4h,1d]; 4h→[1d,3d]; 6h/8h/12h→[1d,3d]; 1d→[3d]; 3d→[1d].
```
alignment_score = (aligned − contrary) / total     # −1 .. +1
```
Por TF superior: `ema_aligned` = bullish/bearish/mixed (stack EMA9/21/50).

### 3.8 Derivativos — `derivatives_service.py`
Funding rate e Open Interest como contexto. Funding **extremo a favor** = risco de squeeze/caça de liquidez (penaliza); funding **extremo contra** = potencial short/long squeeze a favor (bonifica). OI subindo com preço confirma direção.

### 3.9 Confluência — `confluence_service.py` (máx **300 pts**)
Pesos por categoria (`:33-46`): Trend 40 · Momentum 30 · MTF **30** · Pattern 35 · SMC 25 · MACD 20 · Volume 20 · Structure 20 · Divergence 20 · VP+VWAP 20 · Bollinger 15 · Derivatives 15 · Volatility 10.

Thresholds numéricos principais:
- **RSI:** LONG <30 → +18; 30–45 → +10; >75 → −12. SHORT >70 → +18; 55–70 → +10; <25 → −12.
- **EMAs:** stack alinhado → +18; golden/death cross (ema50 vs ema200) → +12; contra ema200 → −8.
- **ADX:** >35 → +10; 25–35 → +6; <20 → −3.
- **Volume:** >2,0× → +20; 1,5–2,0× → +14; <0,5× → −5.
- **Bollinger:** na banda (±0,5%) a favor → +15.
- **Padrões:** `base = confidence×17`, ajustado por multiplicadores empíricos (ex.: double_bottom 1,20; double_top 1,15) e de calibração postmortem 168h (ex.: descending_wedge 2,0; ltb 0,0).
- **SMC:** OB dentro da zona +10 / fora +6; FVG +6; sweep +7; BOS +8; CHoCH +6; estrutura contrária −5.
- **MTF (`:748-793`):** score ≥0,99 → +30; 0,50–0,99 → +20; 0–0,50 → +8; 0 → 0; −0,50–0 → −10; ≤−0,50 → −25.
- **Final:** `pct = clamp(total,0,300)/300 × 100`.

### 3.10 Score composto & tiers — `recommendation_service.py`
`SCAN_TFS = ["15m","1h","4h"]` (`:35`). `MIN_RR = 1.5`, `MIN_CONFIDENCE_B = 0.55`.

**Score V1** (`:485-561`): `conf×0.35 + mtf_norm×0.25 + rr×0.25 + der×0.10 + win_bonus×0.5 + breakout(+5)`, × `_htf_relevance_mult`.

**Score V2** (flag `SCORE_FORMULA_V2`, `:458-482`): `conf×0.60 + adx×0.30 + der×0.10` renormalizado sobre presentes.

**Cortes de tier:**

| Tier | Score V1 | Score V2 | MTF | R:R | Extra |
|---|---|---|---|---|---|
| A+ | ≥75 | ≥65 | ≥0,5 | ≥2,5 | padrão confirmado obrigatório, sem warning crítico |
| A | ≥65 | ≥46 | ≥0,0 | ≥2,0 | — |
| B | ≥52 | ≥18 | — | ≥1,5 | confidence ≥0,55 |

**Gates de rejeição / rebaixamento:** NEUTRAL; denylist; R:R<1,5; squeeze de liquidez (funding extremo a favor); anti-chase (deslocamento ≥5 ATR/3 velas rejeita, 3–5 ATR warning); dead market (`atr_pct<0,3%`); **volume gate** (A+ ≥1,0×, A ≥0,8×, B ≥0,6× — senão rebaixa em cascata); penalidade de derivativos (rebaixa 1 nível por condição adversa); **TF×tier gate** (`TF_MIN_TIER = "15m:A+,1h:A,4h:B"`).

### 3.11 Cálculo de níveis (`signal_service.py:52-57`)
ATR multipliers de SL/TP1/TP2/TP3 por tipo:

| Tipo | SL | TP1 | TP2 | TP3 |
|---|---|---|---|---|
| SCALP | 1,0× | 1,5× | 2,5× | 4,0× |
| DAY_TRADE | 1,5× | 2,0× | 3,5× | 5,0× |
| SWING | 2,0× | 3,0× | 5,0× | 8,0× |
| HODL | 3,0× | 5,0× | 10× | 20× |

Anti-stop-hunt: `PIVOT_BUFFER_ATR = 0.35` (snap do SL ao pivot ± 0,35 ATR). `risk_reward = |tp2−entry| / |entry−stop|`.

### 3.12 Correção anti-contra-tendência (deploy `6f6575b8`)
**Problema:** streak de 7-8 stops por viés direcional (25 shorts × 5 longs em 30d). Causa: lógica "fade the pump" + `SHORT_BRAKE` cego (só BTC) + MTF inerte ao vivo (`mtf=None`).
**Fix — CT brake por símbolo** (`regime_service.symbol_counter_trend` `:393-418` + `recommendation_service._attach_htf_ema_trend`): deriva tendência do TF superior pelo **stack de EMAs** dos TFs já escaneados (zero fetch extra), imune ao fade; **rebaixa tier** (não descarta) shorts contra TFs EMA-bullish e longs contra EMA-bearish; preserva quantidade via penalidade de seleção. Envs: `CT_BRAKE_ENABLED=1`, `CT_BRAKE_MIN_TFS=1`, `CT_BRAKE_BLOCK=false`, `CT_BRAKE_SELECT_PENALTY=12.0`.

---

## 4. Execução & gestão de trade

### 4.1 Pipeline de sizing (`shadow_trade_service.py`)
`_compute_qty` (`:3166-3267`) — ordem de aplicação:
1. `qty_nominal = (equity × risk_pct/100) / |entry−stop|`.
2. **Cap de margem/trade:** `EXCHANGE_MAX_MARGIN_PCT = 15%` → `max_notional = equity × 15% × leverage`.
3. **Cap duro de risco/trade:** `EXCHANGE_MAX_RISK_PCT = 2.0%` (aplicado em todos os caminhos).
4. **Notional mínimo:** `EXCHANGE_MIN_NOTIONAL_USD = 50` (infla se não violar o cap de risco).

Multiplicadores de convicção/edge/defensivos:
- **Conviction sizing** (`CONVICTION_SIZING_ENABLED=true`): escala pela P(TP1) calibrada; mult [0,8 → 1,0]; prob range 0,45→0,65.
- **Edge sizing** (`EDGE_SIZING_ENABLED=false`): +7%/edge, +6% A+, ×0,85 sem edge; range [0,80 → 1,30].
- **Damps:** volatilidade (ATR), **short-slip guard** (redução extra em shorts), liquidity tier (moedas magras), regime (LONG de alt em regime adverso).

Caps agregados: `MAX_TOTAL_NOTIONAL_PCT = 150%`; `MAX_TOTAL_OPEN_RISK_PCT = 4%`; `LIVE_SIZE_MULT = 1.0` (canary, clamp [0,1]).

### 4.2 Entrada real (`binance_signed_service.py`)
- **MAKER-first** (`MAKER_ENTRY_ENABLED=true`): LIMIT post-only (GTX), poll `MAKER_ENTRY_TIMEOUT_S=8s` a cada `0.8s`; fallback MARKET (`MAKER_FALLBACK_MARKET=true`).
- **Sequência `place_order` (`:965-1072`):** set_leverage (1 retry) → entry MARKET (100% qty) → pernas de proteção:
  - **SL:** `STOP_MARKET` + `closePosition=true` (algo order).
  - **TP1:** `TAKE_PROFIT_MARKET` + `reduceOnly`, qty × **`PROTECTION_TP1_QTY_PCT=0.45`** (45%).
  - **TP2:** `TAKE_PROFIT_MARKET` + `closePosition=true` (fecha os 55% restantes).

### 4.3 Trade manager (`trade_manager_service.py`, poll `TRADE_MANAGER_POLL_SECONDS=15`)
Fases: `pre_tp1` (risco 1R) → `post_tp1` (≈55% restante, SL em BE) → `runner` (opcional, ~20%).
- **Break-even estrutural** (`BE_STRUCTURAL_ENABLED=true`): ancora em swing ± `BE_STRUCT_ATR_BUFFER=0.25` ATR; teto de giveback `BE_MAX_GIVEBACK_R=0.5`; SL clampado entre `planned_stop` e `entry`.
- **Time stops** (`TIME_STOP_ENABLED=true`): scalp **240 min**, day **1440 min**, swing **10080 min** (7d).
- **Pré-TP1 protect** (`PRE_TP1_PROTECT_ENABLED=true`): aos **60%** do caminho entry→TP1 sem bater TP1, aperta SL para ~0,5R.
- **Runner** (`RUNNER_ENABLED=false` por padrão em dinheiro real): trailing chandelier `RUNNER_ATR_MULT=3.0`, lookback 22, qty 20%.
- **Auto-heal de proteção** (`PROTECTION_AUTOHEAL_ENABLED=true`): recria pernas SL/TP faltantes; grace 60s; verifica na exchange.
- **Partials adaptativos:** `adaptive_partials_service` pode sobrescrever %TP1 / mult runner / %runner por trade.

### 4.4 Guards de risco (`shadow_trade_service.py`)

| Guard | Env / valor |
|---|---|
| Cluster cap | `CLUSTER_MAX_OPEN=2`, `CLUSTER_MAX_OPEN_PER_DIRECTION=2` (clusters: memes, ai_gaming, l2_infra, defi, majors) |
| SL cooldown/símbolo | `SYMBOL_SL_COOLDOWN_HOURS=4` |
| Regime guard (rajada) | `REGIME_GUARD_WINDOW_HOURS=2`, `MAX_SL=3`, `PAUSE_HOURS=1` |
| Daily breaker | `BREAKER_MIN_SAMPLE=15`, `SL_RATE=0.40`, `PAUSE_HOURS=3`, `STREAK_SL=5`/24h |
| Breaker regime-aware | direção a favor do repique ignora gatilho de taxa (só streak) |
| Flip advisory | `FLIP_MIN_SCORE_DELTA=10`, `FLIP_MAX_CURRENT_R=0.3`, `COOLDOWN=4h` |
| TF upgrade | mesmo par, TF maior + tier superior, `MIN_SCORE_DELTA=10`, `COOLDOWN=4h` |
| Entry throttle | `ENTRY_COOLDOWN_SECONDS=300`, `ENTRY_MAX_PER_HOUR=3`, `MAX_OPEN_PER_DIRECTION=7` |
| Blacklist | `SYMBOL_BLACKLIST="NEIRO,PEOPLE,OPN,MEME"` |
| Equity-backed (ações tokenizadas) | só opera no pregão NYSE (`EQUITY_SESSION_ET=09:30-16:00`), fail-closed |

### 4.5 Shadow vs Live
- **Shadow** (`EXCHANGE_SHADOW=true`, default): grava `source="shadow"`, calcula qty real e níveis, **não** envia ordem.
- **Live** (`EXCHANGE_SHADOW=false`): exige `LIVE_TRADING_CONFIRM="ENTENDO_RISCO_DINHEIRO_REAL"`; sem isso o bot recusa e loga ABORT. Canary via `LIVE_SIZE_MULT`.

### 4.6 Circuit breaker de drawdown (`risk_service.py`)
`DAILY_DD_LIMIT_PCT = −3.0%`, `WEEKLY_DD_LIMIT_PCT = −6.0%` → pausa automática (kill switch). Auto-resume na virada de dia/semana UTC quando o DD volta ao verde. DD = Σ(`realized_r × risk_pct`) na janela.

### 4.7 Alertas Telegram (`notification_service.py`)
Envs `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (fail-soft se ausentes), timeout 5s. Eventos: open, tp1, close, time_stop, direction_paused/resumed. Tags: teste adaptativo, dentro/fora da allowlist (filler).

### 4.8 Máquina de estado do trade (`RealTrade`)
`open → closed_tp1 | closed_tp2 | closed_be | closed_stop | closed_manual`. Reclassificação automática por fill real (lucro nunca vira "stop"; prejuízo nunca vira "tp"). P&L usa **`qty_initial`** como base de risco/notional; TP1 parcial embolsado é persistido para não se perder num BE/stop posterior.

---

## 5. Autoaprendizado, calibração, rotação & regime

### 5.1 Learning (`learning_service.py`)
Buckets em 9 dimensões: tier, tf, direction, tier_tf, session (Asia/Europe/NY/Off), dow, pattern, funding, symbol. `MIN_SAMPLE_BUCKET=5`, `WINNING=0.60`, `LOSING=0.40`, lookback 0 (todo histórico).
- **Auto-adjust/block** (`:445-565`): `MIN_SAMPLE_ADJUST=20`, `MIN_SAMPLE_BLOCK=30`, `BLOCK_WR_MAX=30%` (mult 0,0), `BOOST_WR_MIN=65%`, `ADJUST_CAP=±0.25`. Multiplicadores combinados por produto, clampados a ±25% agregado.
- **Rotação (learning):** `ROTATION_MIN_SAMPLE=12`, `PROMOTE_MIN_R=0.0`, `DEMOTE_MAX_R=−0.2` (ejeta "maçã podre").

### 5.2 Calibração (`calibration_service.py`)
`MIN_SAMPLE_TOTAL=30`, `SHRINKAGE_K=10`, cache 10 min. Bins de score (9 faixas — legacy 55–100 ou V2 15–75). Shrinkage bayesiano por bin: `p_shrunk = (K·p_global + n·p_obs)/(K+n)` (`:187`) → **PAV isotônico** (monotonicidade). Win-sets: TP1 = {won_tp1, won_tp1_be, won_tp2}; TP2 = {won_tp2}. Exclui `expired` resolvido em <30 min (fast void). Versionada em `CalibrationVersion`.

### 5.3 Edge decay (`edge_decay_service.py`)
Baseline `WINDOW_DAYS=60` vs `RECENT_DAYS=14`, `MIN_SAMPLE=8`. Interpola o multiplicador de `r_floor=0.0` até `r_full=−0.3` → mult de 1,0 a `MULT_MIN=0.5`. TTL 30 min. Default OFF.

### 5.4 Rotação de universo (`rotation_service.py`)
`AUTO_APPLY=true`, `MAX_UNIVERSE=350`, `HYSTERESIS_CYCLES=3` (promove/demota só após 3 ciclos consecutivos), `APPLY_INTERVAL=21600s (6h)`, `CHECK_INTERVAL=3600s`, `LIQ_FLOOR_TOP_N=350`. Demoção primeiro (libera espaço), depois promoção respeitando o teto.
- **Backtest-seed** (`BT_SEED`): tf `4h`, máx 15, `MIN_TRADES=60`, `MIN_CALIB≥0.65`. Preenche slots ociosos com moedas de forte edge no backtest.
- Preview semanal (segunda 12 UTC).

### 5.5 Regime (`regime_service.py`)

| Regime | Condição | Ação |
|---|---|---|
| RISK_OFF | BTC 24h ≤ −5% | bloqueia TODAS as recs |
| ALT_DANGER | dom ≥56% **e** BTC 24h ≥3% | bloqueia LONGS em alts |
| BTC_DOMINANT | dom ≥55% **e** BTC 24h ≥1,5% | downgrade de alt-longs |
| ALT_RISK_OFF | USDT.D ≥5% **e** dom ≥55% | downgrade de alt-longs (ou bloqueia se `ALT_RISKOFF_BLOCK`) |
| SHORT_BRAKE | BTC trend ≥6% em 3d | downgrade shorts (ou bloqueia) |
| CT_BRAKE | ≥1 TF superior EMA contra o sinal, 0 a favor | downgrade + penalidade de seleção |

Market bias curto: BTC ±1,0% em 6h → up/down/neutral.

### 5.6 Snapshot lifecycle (`snapshot_service.py`)
`open → won_tp1 | won_tp1_be | won_tp2 | lost | expired`. `realized_r`: TP1/BE = **+0.6**, TP2 = **+1.5**, stop = **−1.0**. Time-stops por TF (15m=4h, 1h=12h, 4h+=168h/7d, 3d=504h/21d). Dedup 2h; expiry teto 168h. Trail pós-TP1: `ATR_TRAIL_K=2.2`, `BE_PLUS_LOCK_R=0.2`, ativa após peak avançar ≥0,5 ATR além do TP1.

### 5.7 Backtest sweep / walk-forward
`CALIBRATION_FACTOR=0.70` (derivado 2026-06-22 da razão live/backtest de 6 majors; `calibrated_edge = wf_avg_r × 0.70`) — uso **informativo / teto de sizing**, nunca EV isolado. `WARMUP_BARS=200`, `MAX_FORWARD_BARS=480`, `SCAN_WINDOW_BARS=800`, timeout 600s/símbolo. Co-tenancy yield: 200 ms busy / 30 ms sleep (~13% de CPU cedida ao WEB).

### 5.8 News blackout (`news_filter_service.py`)
Default (30 min antes, 30 min depois); **FOMC/Fed** estende para (30, 60). Países USD/EUR/GBP, só high-impact. Fonte Forex Factory mirror, cache 1h.

---

## 6. API, banco & frontend

### 6.1 API (WEB, FastAPI)
Famílias de endpoints: recomendações; trades reais; **backtest/universe** (`/api/backtest/universe/status`, `/ranking`); **rotação** (`/api/rotation/state`); risco/regime; calibração; análise técnica e macro; admin/config. Scan loop ao vivo a cada ~90s; WORKER separado roda o sweep.

### 6.2 Modelos DB (principais colunas)
- **RealTrade:** symbol, side, source (manual/auto/shadow), qty, qty_initial, leverage, notional_usd, entry_price, opened_at, planned_stop/tp1/tp2, status, phase, sl/tp1/tp2_order_id, sl_current_price, adaptive_* , pyramiding_level, hedge_for.
- **RecommendationSnapshot:** direção, tf, tier, score, entrada/stop/alvos, status, realized_r, timestamps.
- **SweepProgress, RiskState, CalibrationVersion, RotationState, SymbolBacktestStats** — progresso do sweep, estado de risco/kill-switch, versões de calibração, universo/histerese, stats de backtest por símbolo/TF.

### 6.3 Frontend (React 18 + Vite + Tailwind + Lightweight-Charts)
Painéis: **HomeCockpit** (visão ao vivo), **RecommendationsPanel** (tier/entrada/alvos), **DashboardPanel** (performance), **TradeManager** (gestão de trades), **SweepPanel** (progresso/ranking do backtest), além de chart, drawings e coach PNL.

### 6.4 Principais grupos de ENVs
- **Execução/sizing:** `EXCHANGE_SHADOW`, `LIVE_TRADING_CONFIRM`, `EXCHANGE_MAX_RISK_PCT`, `EXCHANGE_MAX_MARGIN_PCT`, `LIVE_SIZE_MULT`, `CONVICTION_*`, `EDGE_*`.
- **Gestão:** `TRADE_MANAGER_POLL_SECONDS`, `PROTECTION_TP1_QTY_PCT`, `BE_*`, `TIME_STOP_*`, `RUNNER_*`, `PRE_TP1_PROTECT_*`.
- **Guards:** `CLUSTER_*`, `SYMBOL_SL_COOLDOWN_HOURS`, `BREAKER_*`, `REGIME_GUARD_*`, `FLIP_*`, `ENTRY_*`, `SYMBOL_BLACKLIST`.
- **Sinal/tiers:** `SCORE_FORMULA_V2`, `TF_MIN_TIER`, `CT_BRAKE_*`, `RETEST_REARM_*`.
- **Aprendizado/rotação/regime:** `LEARNING_*`, `CALIBRATION_*`, `EDGE_DECAY_*`, `ROTATION_*`, `BT_SEED_*`, regime thresholds (`RISK_OFF_*`, `ALT_DANGER_*`, `SHORT_BRAKE_*` etc.), `NEWS_FILTER_ENABLED`.
- **Integrações:** `BINANCE_API_KEY/SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN/CHAT_ID`, `DATABASE_URL`.

---

## 7. Fluxo de ponta a ponta (resumo operacional)

1. **Scan (~90s):** para cada símbolo do universo, escaneia 15m/1h/4h → indicadores, padrões, SMC, MTF, derivativos.
2. **Confluência & score:** monta score (0–300 → %), aplica V1/V2, classifica tier A+/A/B com gates (R:R, volume, anti-chase, TF×tier).
3. **Filtros de regime:** RISK_OFF/ALT_DANGER/BTC_DOMINANT/ALT_RISK_OFF/SHORT_BRAKE/CT_BRAKE + news blackout + learning auto-block.
4. **Snapshot:** persiste recomendação; lifecycle acompanha até won/lost/expired; alimenta calibração e learning.
5. **Execução:** sizing (risco fixo + caps + convicção/edge/damps) → entrada MAKER-first → SL + TP1 45% + TP2 55%.
6. **Gestão:** BE estrutural pós-TP1, pré-TP1 lock, time-stops, auto-heal, runner opcional; guards e circuit breaker vigiando.
7. **Aprendizado:** calibração isotônica de P(TP1)/P(TP2), edge-decay, rotação de universo (auto-apply 6h) e backtest sweep contínuo no WORKER retroalimentam o sizing e a seleção.
