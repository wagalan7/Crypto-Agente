# Crypto Win — memória do projeto

Idioma: **PT-BR** em toda comunicação. Stack: FastAPI async + React/Vite/TS +
PostgreSQL (async SQLAlchemy/asyncpg) + Binance. **Go-live mainnet: sexta 12/06.**
Regra dura: **não quebrar código que funciona.**

## Os dois ambientes (NÃO CONFUNDIR)

| Apelido oficial | Papel | Serviço Railway | URL |
|---|---|---|---|
| **PRD** | Produção. O bot opera de verdade. Sexta vai a dinheiro real. | `Crypto-Agente` (produção) | `https://crypto-agente-production.up.railway.app` |
| **Crypto-Agente-Dev** | Ambiente de **TESTES** (champion/challenger). Shadow, DB próprio, push off. Universo amplo. O bot **NÃO** opera com dinheiro. | `Crypto-Agente-DEV` (projeto Railway: `Crypto-Agente-Dev`) | `https://crypto-agente-production-c6c4.up.railway.app` |

- Sempre que eu disser "ambiente de testes" / "Dev" = **Crypto-Agente-Dev** (`-c6c4`).
- PRD e Dev têm **DBs separados** de propósito (o universo amplo do Dev não pode
  contaminar o auto-learner do PRD).

## Regra de execução (universo do bot: 60 → até ~300, por mérito)

> Mudança de regra (11/06): as 60 são o **PONTO DE PARTIDA** do canário (mais
> líquidas/seguras pra estrear dinheiro real), **NÃO o teto**. Objetivo: crescer o
> universo de execução **até ~300** (limitado por liquidez), de forma **aditiva e
> por performance** — não "trocar melhor por pior".

- **Hoje (canário): execução = top-60 por volume.** Trava de curto prazo mantida.
- **Crescimento (champion/challenger):**
  - **Promoção (aditiva, automática):** moeda fora do pool com **≥15 trades**
    resolvidos + expectancy>0 / win-rate no alvo + **piso de liquidez** → entra.
    Ex.: 60 boas + 15 de fora que também foram bem → opera **75**.
  - **Demoção (rara, "maçã podre"):** só sai quem for **muito mal** sustentado
    (ponto de partida: expectancy < −0.2R sobre ≥15 trades, por 2 ciclos). Histerese.
  - **Cadência:** semanal (junto da recalibração de segunda). Crescimento automático.
  - O `portfolio_guard` (5 posições / 2 por categoria / 5% risco) **continua** → 300
    elegíveis ≠ 300 abertas; o bot só escolhe os 5 melhores setups de um cardápio maior.
- **Decoupling scan ↔ execução (infra — branch `feat/decouple-exec-universe`):**
  - `EXEC_UNIVERSE_ALLOWLIST` (CSV de bases) em shadow_trade_service.py: **vazio =
    sem restrição** (= comportamento atual, PRD intocado); setado = só executa essas
    bases mesmo com scan amplo. Aplicado **só em live** (DEV/shadow observa tudo).
  - `PORTFOLIO_GUARD_ENABLED` (default true) em recommendation_service.py: desligar
    **só no DEV** pra o painel 👁 mostrar o universo amplo sem o cap de 5.
  - **Regra operacional dura:** NÃO ampliar `SERVER_SCAN_TOP_N` do PRD **sem antes**
    setar `EXEC_UNIVERSE_ALLOWLIST` — senão o scan amplo arrastaria a execução junto.
- **Ativação real do crescimento só PÓS-canário (1.0 estável) + dados do DEV.** Até
  lá o universo amplo (observação/aprendizado) vem do **Crypto-Agente-Dev**.
- `SERVER_SCAN_TOP_N` lido **só no boot** (recommendation_service.py:100) → mudar
  exige **redeploy/restart**.

## App — painel "Trades Recomendados"

- **🤖 BOT OPERA** = recs do PRD (as 60 que o bot executa).
- **👁 OBSERVAÇÃO** = recs do Crypto-Agente-Dev (universo amplo, só pra analisar).
- Fonte da observação: `VITE_OBSERVATION_API_URL` (default = URL do `-c6c4`).
  Degrada gracioso: se o Dev cair, mostra só as do bot.

## Operacional

- Railway CLI **deslogado** → validação só via `curl` HTTP. Não consigo mexer em
  env do Railway nem ver logs; passos no dashboard são do usuário.
- Deploy = `git push origin main` (Railway auto-deploy). Confirmar conclusão pelo
  `startup_at` em `/api/health`. Preflight de gates: `/api/live/preflight`.
- Promoção Dev→PRD (futuro): ≥20 trades · expectancy>0 · win-rate≥alvo · ~2 semanas.

## Plano de ramp do canário (tamanho da posição — `LIVE_SIZE_MULT`)

Hoje: **`LIVE_SIZE_MULT=0.25`** (25% do tamanho normal). Sobe por **evidência**, não
por calendário:

- **0.25 → 0.5:** após as **3 primeiras entradas reais** executando limpo (preço/qtd
  certos, SL+TP1 colocados, trail/BE pós-TP1 ok, sem ordem rejeitada/erro IP/proxy).
- **0.5 → 1.0 (oficial):** próximas **10 trades OU 1 semana (o que vier primeiro)**
  com tudo ok e expectancy não-negativa.
- Cada subida = você muda a env no Railway + redeploy; eu valido via curl.

## Kill-switch / circuit breaker (já implementado: `kill_switch_service.py`)

Roda **antes de cada ordem real**; bloqueia **novas entradas** (NÃO fecha as abertas).
Estado derivado de `RealTrade` (sobrevive restart). Envs:
- `KILL_SWITCH` (manual global) · `KILL_MAX_OPEN_POSITIONS=5`
- **Perda diária**: `KILL_MAX_DAILY_LOSS_PCT=4` ← **decidido** (4% × equity ≈ $112/dia
  no início; escala com a banca). Conta **PnL realizado** dos fechados hoje (reset 00h
  UTC). Fallback `KILL_MAX_DAILY_LOSS_USD=200` se equity indisponível. Avisa Telegram.
- `KILL_MAX_CONSEC_LOSSES=3` (cooldown `KILL_COOLDOWN_HOURS=12`) · `KILL_MAX_DAILY_TRADES=20`

## Protocolo de operação assistida (pós go-live)

1. **1ª entrada real (sexta):** acompanhar ao vivo cada entrada — preço/qtd certos no
   tamanho 0.25, SL+TP1 colocados, saldo coerente, sem rejeição/erro IP-proxy. Falhou →
   **pausa imediata** e diagnóstico.
2. **Pós-TP1:** confirmar break-even movido + trailing seguindo + parcial realizada.
3. **Guardas ao vivo:** 5 posições / 2 por categoria / 5% risco; kill-switch à mão.
4. **Cadência:** sexta = intensivo (loop + kill-switch, aviso por evento); fim de cada
   dia = revisão (trades, PnL, expectancy, anomalias + decisão de ramp); semanal =
   revisão maior + recalibração de segunda + decisão 0.5→1.0.
5. **Diário de bordo:** registrar marcos (1ª entrada, 1º TP1, mudança de ramp, incidentes).

## Recalibração da autoaprendizagem (calibração P(TP1) + buckets)

- Reaprende com TODO o histórico resolvido (score→P(TP1) via shrinkage bayesiano
  + isotônica) e versiona/compara por Sharpe. Fail-open, **não toca execução**.
- Cadência atual: **semanal — toda segunda às 09h BRT (= 12:00 UTC)**.
  Implementado em `_recalibration_loop` (backend/main.py); modo via env:
  - `RECALIBRATION_MODE=weekly` (default) | `interval`
  - `RECALIBRATION_WEEKLY_DOW=0` (0=segunda) · `RECALIBRATION_WEEKLY_HOUR_UTC=12`
  - check a cada 1h (`RECALIBRATION_CHECK_INTERVAL_SEC`, default 3600).
  - Relógio vem do `last_recalibration_at` no DB → robusto a restart, não dispara 2×.
- Manual a qualquer momento: `POST /api/calibration/recalibrate` (seguro).

## Roadmap pós-go-live (NÃO antes de sexta)

- **[A] Aprender do universo amplo (observação/DEV), não só das 60.** Hoje os DBs
  PRD/DEV são separados de propósito (não contaminar o learner do PRD). Desenhar
  uma "ponte" controlada DEV→aprendizado, validar no DEV antes de encostar no PRD.
- **[C] TP/SL adaptativo por símbolo** (histórico do símbolo ajusta alvo/stop
  inicial). Hoje TP/SL inicial é fixo por ATR; só trail+BE adaptam pós-TP1.
- **App do DEV** (ver seção acima) — fazer na sexta após a migração.

## Pendência — "app do DEV" (fazer SEXTA, após a migração)

Objetivo: dar ao usuário um app apontando 100% pro **DEV** (`-c6c4`), com os
mesmos painéis, pra validar testes futuros (champion/challenger) e dar OK.

- Frontend roda na **Vercel** (não é servido pelo Railway). A API base é
  escolhida no **build** via `VITE_API_URL` (sem env = PRD por padrão).
  `frontend/vercel.json` → build `npm run build`, output `dist`.
- Solução: **segundo projeto Vercel** (mesmo repo, pasta `frontend`), só mudam
  as envs de build:
  - `VITE_API_URL = https://crypto-agente-production-c6c4.up.railway.app` (DEV)
  - `VITE_OBSERVATION_API_URL = ` (vazio — não remesclar observação)
  - Ganha URL própria (ex.: `crypto-agente-dev.vercel.app`).
- **Zero impacto no PRD** (projeto Vercel separado, aditivo). Sem mudança de código.
- Eu **não** mexo na Vercel (igual Railway) → criar projeto + setar envs é passo
  do usuário no painel; eu passo o passo a passo.
- Cosmético (não bloqueia): no app do DEV o marcador 🤖/👁 é conceito do PRD →
  tudo apareceria como 🤖 (lá o bot é shadow). Se o usuário quiser, trocar a
  label pra algo tipo "🧪 TESTE" só no app do DEV.
