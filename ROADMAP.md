# Roadmap — Crypto Win até Auto-Pilot

**Início:** 31/Mai/2026 (domingo)
**Meta:** auto-trade com size cheio confiável até ~Nov/2026
**Princípio:** cada fase só avança se a anterior bater métricas mínimas.

---

## Fase 1 — Safety Nets (31/Mai → 13/Jun · 2 sem)

Pré-requisitos críticos. Sem isso, NÃO ligar nada automático.

### 1.1 Circuit breaker de drawdown · 🔴 Crítico · 1-2 dias
- [ ] Tabela `equity_curve` (timestamp, balance, daily_pnl, weekly_pnl)
- [ ] Endpoint `/api/risk/status` retorna estado atual
- [ ] Regra: se daily DD ≤ -3% OU weekly DD ≤ -6% → flag `trading_paused=True`
- [ ] Server-scan respeita flag e não emite push de novas recs
- [ ] UI badge vermelho no header quando pausado
- [ ] Reset automático na virada do dia/semana
- **Métrica de aceite:** simular 5 trades perdedores em sequência e ver pause acionado.

### 1.2 Position sizing dinâmico · 🔴 Crítico · 2-3 dias
- [ ] Campo `suggested_size_pct` no TradeSignal (% da banca)
- [ ] Fórmula: Kelly fracionado × (score/100) × (1 / atr_pct), cap entre 0.25% e 1%
- [ ] Frontend exibe size sugerido no card
- [ ] Backtest cruza: sizing fixo 1% vs dinâmico → equity curve
- **Métrica de aceite:** dinâmico não pode ter Sharpe pior que fixo no backtest.

### 1.3 Portfolio risk guard · 🔴 Crítico · 3-5 dias
- [ ] Tabela `open_positions` (symbol, direction, size_usd, opened_at)
- [ ] Limite: máx 3 posições simultâneas
- [ ] Detecta correlação: se já tem 2 longs em alts → bloqueia 3º long alt
- [ ] Cap de exposição agregada: soma de size_usd ≤ 5% da banca
- [ ] Endpoint `/api/portfolio/exposure` retorna métricas atuais
- **Métrica de aceite:** abrir 3 longs em BTC/ETH/SOL e ver 4º (em DOGE) bloqueado.

### 1.4 Failsafe & recovery · 🔴 Crítico · 2-3 dias
- [ ] Heartbeat: server escreve `last_alive_ts` a cada 60s
- [ ] Posições abertas têm `last_seen_alive` — se ficar > 5min sem update, dispara alerta crítico
- [ ] Endpoint `/api/admin/reconcile-positions` cruza DB com exchange ao subir
- [ ] Log estruturado de toda transição de estado
- **Métrica de aceite:** matar backend com posição aberta, subir de novo, posição reconciliada.

### 1.5 Painel monitor auto-trade (básico) · 🔴 Crítico · 3-4 dias
- [ ] Aba "Status" no app: trading paused? quantas posições? exposição? DD dia/semana?
- [ ] Histórico de circuit-breaker triggers
- [ ] Toggle manual: "pausar todas operações" (kill switch)
- **Métrica de aceite:** kill switch corta tudo em < 5s.

---

## Fase 2 — Validation Infra (14/Jun → 27/Jun · 2 sem)

Construir o "esqueleto" de auto-trade rodando como simulação em paralelo.

### 2.1 Paper-trade mode em produção · 🟡 Validação · 2-3 dias
- [ ] Tabela `paper_trades` espelho de operações reais que o bot "abriria"
- [ ] Cada rec tier A+/A simula: entry, stop, TPs, trailing
- [ ] Cron diário compara paper PnL vs operações manuais reais
- [ ] Dashboard: equity curve paper vs real lado-a-lado
- **Métrica de aceite:** 7 dias rodando, divergência < 10% em trades coincidentes.

### 2.2 Recalibração contínua · 🟡 Maturidade · 2-3 dias
- [ ] Job mensal re-treina PAV com últimos 90 dias de trades reais
- [ ] Snapshot do modelo anterior fica versionado
- [ ] A/B no scan: 50% recs com modelo novo, 50% antigo, compara WR
- **Métrica de aceite:** modelo novo nunca substitui antigo se Sharpe regredir.

### 2.3 Dashboard comparativo · 🟡 Validação · 2 dias
- [ ] Side-by-side: backtest histórico · paper-trade · operação real
- [ ] Por tier, mostrar WR, avgR, expectancy, max DD
- [ ] Alerta se gap entre paper e real > 20%
- **Métrica de aceite:** dashboard mostra ≥ 30 trades A+ paper antes de avançar.

---

## Fase 3 — Semi-Auto Supervisionado (28/Jun → 25/Jul · 4 sem)

Bot abre ordem real, mas pede confirmação push em < 60s. Você ainda no controle.

### 3.1 Integração Bybit API · 🔴 Crítico · 5-7 dias + auditoria
- [ ] Conta sub-account com API key restrita (só trade, sem withdraw)
- [ ] Wrapper async `bybit_trader.place_order / cancel / amend`
- [ ] Idempotency keys pra evitar duplicação
- [ ] Rate-limit handling
- [ ] Auditoria de segurança: key não vaza em logs, env-only
- **Métrica de aceite:** placar 5 ordens limit + 5 market em testnet sem erro.

### 3.2 Trailing stop server-side · 🔴 Crítico · 2-3 dias
- [ ] Após TP1, sobe stop pra BE+0.2R via API
- [ ] Trail dinâmico em volatilidade alta
- [ ] Reconciliação a cada candle: posição na corretora bate com DB?
- **Métrica de aceite:** 10 trades reais com trail rodando, zero "stop órfão".

### 3.3 Confirmação push < 60s · 🔴 Crítico · 2-3 dias
- [ ] Push de "nova rec A+" agora vem com botões "✅ Executar / ❌ Cancelar"
- [ ] Se confirma → bot manda ordem real
- [ ] Se cancela ou ignora 60s → não executa
- [ ] Histórico de decisão (confirm rate, time-to-decide)
- **Métrica de aceite:** 20 confirmações reais, ordem chega em < 5s pós-confirmação.

### 3.4 Logs forenses · 🟡 Operação · 1-2 dias
- [ ] Cada decisão automática gera evento em `audit_log`
- [ ] Replay: dado um trade, mostrar todo contexto (snapshot, score, decisão)
- **Métrica de aceite:** poder explicar qualquer trade dos últimos 30 dias em < 1min.

---

## Fase 4 — Auto-Trade Size 0.25× (26/Jul → 22/Ago · 4 sem)

Liga auto sem confirmação, mas com 25% do size que você usaria manual.

### 4.1 Toggle "auto-mode" no painel · 🔴 Crítico · 1-2 dias
- [ ] Switch on/off por tier (auto só A+, ou A+ e A)
- [ ] Quando ON: scan emite ordem direto, sem push de confirmação
- [ ] Push agora é informativo ("comprei X a Y")
- **Métrica de aceite:** liga, opera 3 dias, métricas batem semi-auto.

### 4.2 News feed expansion · 🟢 Defesa · 1 sem
- [ ] Twitter/X feed via API (contas-chave: CZ, Vitalik, etc)
- [ ] On-chain alerts (whale movements > $10M)
- [ ] Funding extremo (> 0.1% / 8h) entra como blackout temporário
- **Métrica de aceite:** detectar últimos 3 hacks/eventos retroativamente.

### 4.3 Métricas rolling em produção · 🟡 Maturidade · 2-3 dias
- [ ] WR, expectancy, Sharpe rolling 30d/90d em dashboard
- [ ] Alerta se rolling 30d cair > 2σ vs backtest
- **Métrica de aceite:** 4 semanas de auto-0.25× com Sharpe ≥ 80% do backtest.

---

## Fase 5 — Scale-up Gradual (23/Ago → Nov/2026 · 3 meses)

Aumentar size só se métricas validarem.

### 5.1 Auto size 0.5× (23/Ago · 4 sem)
- [ ] Sobe pra metade do size, monitora
- **Gate:** Sharpe rolling 30d > 1.0 + max DD < 8%

### 5.2 Auto size 0.75× (~20/Set · 4 sem)
- [ ] Sobe pra 75%
- **Gate:** mais 4 sem de Sharpe > 1.0, DD < 10%

### 5.3 Auto size cheio (~18/Out · 4-6 sem observação)
- [ ] Size 100%
- [ ] Daily report automático no celular
- **Gate final:** auto-pilot só fica "ligado por padrão" depois de 6 sem 100% sem incidente operacional crítico.

---

## Riscos & gates de abortar

A QUALQUER MOMENTO se acontecer, volta uma fase:
- DD agregado > 15% em 30 dias
- Discrepância paper vs real > 30%
- Incidente operacional (exchange API quebra, posição órfã, deploy ruim em prod)
- Regime change forte (BTC dump > 30% em 1 sem)

---

## Resumo de marcos

| Marco | Data alvo | Estado |
|-------|-----------|--------|
| Safety nets prontos | **13/Jun** | Pré-requisito p/ qualquer auto |
| Paper-trade rodando | **27/Jun** | Esqueleto validado em paralelo |
| Semi-auto ligado | **25/Jul** | Bot opera, você confirma |
| Auto 0.25× | **22/Ago** | Bot decide, size pequeno |
| Auto 0.5× | **20/Set** | Confiança crescente |
| Auto 0.75× | **18/Out** | Última calibração |
| Auto full | **~Nov/2026** | Meta original |

**Tempo total:** ~5-6 meses contínuos. Realista pra cripto.

---

## Princípio orientador

> Cada fase tem um **gate quantitativo**. Não avança no "achei que tá bom".
> Auto-trade em cripto sem disciplina vira drawdown de 50% em 2 semanas.
> O objetivo não é "automatizar rápido" — é "automatizar com edge preservado".
