"""
Shadow Trade Service (#11.3) — execução "sombra" de ordens em paralelo às recs.

Quando uma rec nova é emitida (A+/A), em vez de só salvar o snapshot e esperar
o paper-trade resolver via candles, o sistema também ABRE uma RealTrade com
`source="shadow"` representando a ordem que TERIA sido enviada à exchange.

Por que "shadow":
  - Não chama `place_order` na exchange (não depende de saldo/conexão real)
  - Mas calcula qty real (risk_pct × equity_virtual / risk_distance) e grava
    todos os níveis — assim, quando você flipar `EXCHANGE_SHADOW=false`, o
    mesmo código vira execução de verdade sem refactor
  - O dashboard #10 já enxerga essas trades (mesmo shape em /api/real-trades/summary)
  - Slippage vs paper fica em zero (shadow usa entry teórico da rec) — futuro
    podemos injetar mid-price real pra simular fill

Fluxo:
  1. main.py chama `open_shadow_for_recs(recs)` depois de `save_recommendations`
  2. Pra cada rec com `_just_saved=True`, abre RealTrade(source="shadow")
  3. snapshot_service.check_open_snapshots chama `close_shadow_for_snapshot(snap)`
     quando o snapshot resolve (won_tp1/tp2/be/lost/expired)
  4. Trade fecha com mesmo R do paper — slippage zero por design

Toggle:
  EXCHANGE_SHADOW=true  (default) → modo shadow ativo, sem chamada real
  EXCHANGE_SHADOW=false           → executa de verdade via exchange_service
  EXCHANGE_SHADOW_EQUITY_USD=10000 (default) → equity virtual pra dimensionar qty

Quando ativar execução real (futuro #11.4):
  - Setar EXCHANGE_SHADOW=false
  - exchange_service.place_order() será chamado com mesmos params
  - source vira "auto" ao invés de "shadow"
  - exchange_order_id preenchido com id retornado pela corretora
  - tracker passa a monitorar order_history pra status
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Optional

from db import DB_ENABLED
from services import real_trade_service
from services import adaptive_partials_service

log = logging.getLogger(__name__)

SHADOW_ENABLED = os.getenv("EXCHANGE_SHADOW", "true").strip().lower() in ("1", "true", "yes")
# Fallback estático — usado APENAS se a exchange estiver fora do ar.
# Em condições normais, exchange_service.get_equity() lê o saldo real.
VIRTUAL_EQUITY_USD = float(os.getenv("EXCHANGE_SHADOW_EQUITY_USD", "5000"))

# ── Trava de dinheiro real (go-live #1) ─────────────────────────────────────
# O master gate de execução é SHADOW_ENABLED. Mas desligar o shadow
# (EXCHANGE_SHADOW=false) numa conta de PRODUÇÃO = dinheiro real de verdade.
# Pra blindar contra acidente (ex: alguém setou EXCHANGE_SHADOW=false sem
# perceber que a conta é mainnet), exigimos uma confirmação EXPLÍCITA: a env
# LIVE_TRADING_CONFIRM precisa bater exatamente a frase abaixo. Sem ela, o bot
# se RECUSA a executar ordens reais — loga ABORT e pula a trade. Demo/testnet
# (dinheiro fake) não exige confirmação.
_LIVE_CONFIRM_PHRASE = "ENTENDO_RISCO_DINHEIRO_REAL"
LIVE_TRADING_CONFIRM = os.getenv("LIVE_TRADING_CONFIRM", "").strip()

# ── Canary / ramp de tamanho (go-live #2) ───────────────────────────────────
# Multiplicador global aplicado ao qty SÓ em modo live. Permite começar a
# operar dinheiro real com fração do tamanho (ex: 0.1 = 10%) e subir gradual
# conforme ganha confiança. 1.0 = tamanho cheio. Não afeta shadow. Se a fração
# levar o notional abaixo do mínimo da exchange, a trade é pulada (canary muito
# pequeno pra esse símbolo).
LIVE_SIZE_MULT = max(0.0, min(float(os.getenv("LIVE_SIZE_MULT", "1.0")), 1.0))

# ── Filtro de sessão/horário (go-live, opcional) ────────────────────────────
# Gate de EXECUÇÃO (não esconde recomendações — o painel segue mostrando; só
# evita o bot AUTO-abrir posição em janelas de horário ruins, ex.: sessão
# europeia de baixa qualidade ou madrugada ilíquida). CSV de faixas em UTC no
# formato "hA-hB" (intervalo [hA, hB), com wrap em 24h). Vazio = DESLIGADO.
# Ex.: "0-6"  → bloqueia 00:00–05:59 UTC; "22-2" → bloqueia 22,23,0,1h UTC.
# Reversível sem deploy. Default OFF: sem dado provando uma sessão ruim, não
# corta trades — a infra fica pronta pra ligar quando os dados justificarem.
def _parse_block_hours(raw: str) -> set:
    out: set = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        try:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s) % 24, int(b_s) % 24
        except Exception:
            continue
        h = a
        guard = 0
        while h % 24 != b and guard < 24:
            out.add(h % 24)
            h += 1
            guard += 1
    return out


TRADE_BLOCK_HOURS_UTC: set = _parse_block_hours(os.getenv("TRADE_BLOCK_HOURS_UTC", ""))


# ── Filtro de PREGÃO para moedas lastreadas em ações (go-live) ───────────────
# Moedas tokenizadas com lastro na bolsa americana (bStocks / stock-perps:
# TSLA, NVDA, AAPL, STXX, ...) só têm fluxo/price-discovery "de verdade" quando
# a NYSE/Nasdaq está aberta. Fora do pregão (madrugada, fim de semana) ficam
# ilíquidas e o sinal degrada — foi o caso do STXX às 04:49 BRT (-1,29R).
#
# Este gate bloqueia o AUTO-abrir dessas moedas FORA do pregão regular dos EUA.
# TODAS as demais criptos seguem 24h (não são afetadas).
#
# EQUITY_BACKED_SYMBOLS  — CSV de BASES lastreadas (default = seed conhecido).
#                          Extensível sem deploy. Vazio = filtro desligado.
# EQUITY_US_HOURS_ONLY   — "true"/"false" master-toggle (default true).
# EQUITY_SESSION_ET      — janela do pregão em horário de Nova York, "HH:MM-HH:MM"
#                          (default "09:30-16:00" = pregão regular). O DST
#                          (EDT/EST) é resolvido automático via zoneinfo, então
#                          a janela UTC se ajusta sozinha ao horário de verão.
# Nota: não considera feriados de bolsa (poucos por ano); numa data de feriado
#       ainda pode operar no horário. Fail-closed: se não der pra determinar o
#       horário (erro de tz), a moeda lastreada é BLOQUEADA por segurança.
def _parse_equity_symbols(raw: str) -> set:
    out: set = set()
    for part in (raw or "").split(","):
        b = part.strip().upper()
        if b:
            out.add(b)
    return out


_EQUITY_SEED = "STXX,TSLA,NVDA,AAPL,META,GOOGL,GOOG,MSFT,AMZN,CRCL,MSTR,COIN"
EQUITY_BACKED_SYMBOLS: set = _parse_equity_symbols(
    os.getenv("EQUITY_BACKED_SYMBOLS", _EQUITY_SEED)
)
EQUITY_US_HOURS_ONLY: bool = os.getenv("EQUITY_US_HOURS_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")


def _equity_base_of(symbol: str) -> str:
    """'STXX/USDT:USDT' → 'STXX' (uppercase). Robusto a formatos sem '/'."""
    s = (symbol or "").upper().strip()
    if "/" in s:
        s = s.split("/", 1)[0]
    return s.split(":", 1)[0].strip()


def _is_equity_backed(symbol: str) -> bool:
    if not EQUITY_BACKED_SYMBOLS:
        return False
    return _equity_base_of(symbol) in EQUITY_BACKED_SYMBOLS


def _us_equity_session_open_now() -> bool:
    """True se o pregão regular dos EUA está aberto AGORA (dia útil + janela ET).

    Usa America/New_York (DST automático). Se o cálculo de timezone falhar,
    retorna False (fail-closed) — a moeda lastreada fica bloqueada por segurança.
    """
    try:
        from zoneinfo import ZoneInfo
        raw = os.getenv("EQUITY_SESSION_ET", "09:30-16:00").strip()
        o_s, c_s = raw.split("-", 1)
        oh, om = (int(x) for x in o_s.split(":"))
        ch, cm = (int(x) for x in c_s.split(":"))
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:  # 5=sáb, 6=dom
            return False
        mins = now_et.hour * 60 + now_et.minute
        return (oh * 60 + om) <= mins < (ch * 60 + cm)
    except Exception as e:  # tz indisponível / parse ruim → fail-closed
        log.warning(f"[equity-session] falha ao apurar pregão ({e}) — fail-closed")
        return False


def _exchange_is_production() -> bool:
    """True se a exchange ativa está em modo produção (dinheiro real)."""
    try:
        from services import exchange_service
        info = exchange_service.env_info()
        mode = (info.get("mode") or "").strip().lower()
        if mode:
            # binance: demo/testnet = fake; mainnet = real
            return mode == "mainnet"
        # fallback genérico: flag testnet (true = fake)
        return not bool(info.get("testnet", True))
    except Exception:
        # Na dúvida, assume produção (fail-safe → exige confirmação explícita)
        return True


def _live_money_guard() -> tuple[bool, str]:
    """
    (allowed, reason) — bloqueia execução real se a conta é produção e a
    confirmação explícita (LIVE_TRADING_CONFIRM) não foi dada.
    """
    if not _exchange_is_production():
        return True, "non-prod (demo/testnet) — sem dinheiro real"
    if LIVE_TRADING_CONFIRM == _LIVE_CONFIRM_PHRASE:
        return True, "produção confirmada (LIVE_TRADING_CONFIRM ok)"
    return False, (
        f"conta de PRODUÇÃO sem confirmação — defina "
        f"LIVE_TRADING_CONFIRM={_LIVE_CONFIRM_PHRASE} pra liberar dinheiro real"
    )


def log_boot_safety_banner() -> None:
    """
    Banner gritante no boot resumindo o estado de execução (shadow vs live,
    produção vs demo, canary, confirmação). Chamado no lifespan do app.
    """
    try:
        prod = _exchange_is_production()
        exch = os.getenv("EXCHANGE", "binance")
        if SHADOW_ENABLED:
            log.info(
                f"[boot-safety] 🟢 SHADOW ON ({exch}) — nenhuma ordem real é "
                f"enviada à exchange (sizing usa equity real só pra simular)"
            )
            return
        # Live (shadow off)
        armed, why = _live_money_guard()
        env_tag = "PRODUÇÃO/MAINNET" if prod else "demo/testnet"
        if not prod:
            log.warning(
                f"[boot-safety] 🟡 LIVE ON ({exch}, {env_tag}) — ordens enviadas, "
                f"mas dinheiro FAKE. canary×{LIVE_SIZE_MULT}"
            )
        elif armed:
            log.warning(
                "[boot-safety] 🔴🔴🔴 LIVE ON EM PRODUÇÃO — DINHEIRO REAL 🔴🔴🔴 "
                f"({exch}, {env_tag}) confirmação OK. canary×{LIVE_SIZE_MULT}. "
                f"Cada rec tier A/A+ abre posição real."
            )
        else:
            log.error(
                f"[boot-safety] ⛔ LIVE pedido em PRODUÇÃO ({exch}) MAS SEM "
                f"CONFIRMAÇÃO — ordens reais serão BLOQUEADAS até definir "
                f"LIVE_TRADING_CONFIRM={_LIVE_CONFIRM_PHRASE}. (Trades pulados.)"
            )
    except Exception as e:
        log.warning(f"[boot-safety] banner falhou: {e}")

# Guard de notional mínimo (Binance Futures: $50). Se o sizing por risco
# ficar abaixo do mínimo, inflamos o qty pra atingir — desde que isso não
# leve o risco real além de MAX_RISK_PCT_HARD. Caso contrário, pula a trade.
MIN_NOTIONAL_USD = float(os.getenv("EXCHANGE_MIN_NOTIONAL_USD", "50"))
MAX_RISK_PCT_HARD = float(os.getenv("EXCHANGE_MAX_RISK_PCT", "2.0"))

# Cap de margem por trade (% banca). Quando SL é apertado, sizing por risco
# fixo (1%) infla notional. Esse cap limita: margin_used = notional/leverage
# nunca passa de MAX_MARGIN_PCT × equity. Risco real cai abaixo do alvo, mas
# a banca não fica refém de SL apertado.
MAX_MARGIN_PCT_PER_TRADE = float(os.getenv("EXCHANGE_MAX_MARGIN_PCT", "15"))

# Cap de exposição agregada (notional somado / equity × 100). Bloqueia abrir
# nova posição se notional_total + nova_trade > esse limite. 150% = 1.5×
# banca em exposição total (com 10x lev = 15% margem agregada).
MAX_TOTAL_NOTIONAL_PCT = float(os.getenv("EXCHANGE_MAX_TOTAL_NOTIONAL_PCT", "150"))

# Cap de MARGEM agregada (capital comprometido = Σ notional/leverage, em % da
# banca). É o "manter só X% da banca aberta no máximo" — diferente do notional,
# conta o que sai de garantia, não o tamanho alavancado. Quando uma posição
# fecha, libera espaço pra novas (orçamento rotativo). 0 = desligado.
MAX_TOTAL_MARGIN_PCT = float(os.getenv("EXCHANGE_MAX_TOTAL_MARGIN_PCT", "0"))

# ── #2a Sizing por CONVICÇÃO (escala o risco/trade pela P(TP1) calibrada) ─────
# Em vez de risco fixo por tier, escala o risk_pct por um multiplicador ligado à
# convicção do setup (P(TP1) calibrada). Setup com prob alta arrisca um pouco
# mais; prob baixa, um pouco menos. SEMPRE dentro dos caps duros já existentes
# (_compute_qty aplica MAX_RISK_PCT_HARD e margem). NO-OP-SAFE: se a calibração
# não está madura (prob_tp1=None) → mult=1.0 (não mexe). LIGADO em modo DEFENSIVO
# por default (ver MULT_MAX=1.0 abaixo): só REDUZ risco em setup fraco, nunca
# aumenta. Pra liberar o lado de cima (mais risco em alta convicção) sobe
# CONVICTION_MULT_MAX via env (>1.0) — recomendado só após o teste 0.50.
CONVICTION_SIZING_ENABLED = os.getenv("CONVICTION_SIZING_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Faixa de P(TP1) mapeada linearmente pra faixa de multiplicador. lo no piso do
# gate (0.45), hi numa prob alta (0.65). Fora da faixa → clampa.
CONVICTION_PROB_LO = float(os.getenv("CONVICTION_PROB_LO", "0.45"))
CONVICTION_PROB_HI = float(os.getenv("CONVICTION_PROB_HI", "0.65"))
# Banda do multiplicador. Default DEFENSIVO: piso 0.8× (reduz nos fracos), teto
# 1.0× (NÃO aumenta acima do risco base). Subir o teto p/ >1.0 via env = liberar
# agressividade nos setups de alta convicção (recomendado só após o teste 0.50).
# Caps duros (_compute_qty) continuam mandando independente disso.
CONVICTION_MULT_MIN = float(os.getenv("CONVICTION_MULT_MIN", "0.8"))
CONVICTION_MULT_MAX = float(os.getenv("CONVICTION_MULT_MAX", "1.0"))
# ── #2 P(TP2) na convicção ── Blenda P(TP2) calibrada como sinal ADITIVO no
# fator de convicção. Peso 0.0 = NO-OP (comportamento idêntico ao TP1-only).
# Cada prob é normalizada na PRÓPRIA banda LO/HI e os fracs são misturados:
#   frac = (1-w)*frac_tp1 + w*frac_tp2 ; mult = MIN + frac*(MAX-MIN)
# Banda TP2 mais baixa que a do TP1 porque P(TP2) é sempre <= P(TP1) (subconjunto:
# correr até o TP2 é mais raro que bater o TP1). Default desligado (w=0) até
# medir a distribuição real de p_tp2_global ao vivo e calibrar a banda.
CONVICTION_TP2_WEIGHT = float(os.getenv("CONVICTION_TP2_WEIGHT", "0.0"))
CONVICTION_TP2_PROB_LO = float(os.getenv("CONVICTION_TP2_PROB_LO", "0.25"))
CONVICTION_TP2_PROB_HI = float(os.getenv("CONVICTION_TP2_PROB_HI", "0.45"))

# ── #1 Sizing por EDGE (tier A+ / funding / padrão / MTF) ────────────────────
# A convicção acima escala pela P(TP1) calibrada — mas na escala V2 a calibração
# é CHATA (score 31+ todos ~0.65-0.68), então quase não diferencia. O que de fato
# separa win-rate no histórico (learning-insights, 701 trades) é TIER A+ (~92%),
# funding em squeeze (~100%), padrão forte (~90%) e MTF alinhado (~82%) — vs
# baseline ~72%. Este multiplicador escala o risco/trade pela CONTAGEM de edges
# (rec['edge_score'], calculada read-only no recommendation_service) e dá um bônus
# extra se A+ está entre eles. Compõe MULTIPLICATIVO com a convicção e o
# LIVE_SIZE_MULT, SEMPRE dentro dos caps duros de _compute_qty. Setup SEM nenhum
# edge (tier A/B puro) leva um leve desconto (NOEDGE_MULT) — concentra banca onde
# há sinal. DEFAULT OFF (dinheiro real): liga via env após revisar.
EDGE_SIZING_ENABLED = os.getenv("EDGE_SIZING_ENABLED", "false").strip().lower() in ("1", "true", "yes")
EDGE_PER_EDGE = float(os.getenv("EDGE_PER_EDGE", "0.07"))      # +7% por edge confirmado
EDGE_APLUS_BONUS = float(os.getenv("EDGE_APLUS_BONUS", "0.06"))  # +6% extra se A+ presente
EDGE_NOEDGE_MULT = float(os.getenv("EDGE_NOEDGE_MULT", "0.85"))  # desconto p/ setup sem edge
EDGE_MULT_MIN = float(os.getenv("EDGE_MULT_MIN", "0.80"))
EDGE_MULT_MAX = float(os.getenv("EDGE_MULT_MAX", "1.30"))

# ── #4 Entrada MAKER (post-only) — economiza a taxa taker na entrada ─────────
# Em vez de entrar a MARKET (taker ~0.04%/0.05%), posta LIMIT post-only (GTX) no
# preço planejado e aguarda o fill como MAKER (taxa menor, às vezes rebate). Se
# não preencher no tempo (mercado fugiu) → cai pra MARKET (fail-safe: prefere
# entrar a ficar de fora). A proteção (SL/TP) só é colocada APÓS confirmar o fill
# (helper place_maker_entry_then_protect, desacopla entrada de proteção).
# DEFAULT OFF: dinheiro real → só liga após você revisar. Mudar só muda a entrada;
# o guard cardinal "sem stop = sem trade" e os TPs seguem idênticos.
MAKER_ENTRY_ENABLED = os.getenv("MAKER_ENTRY_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# ── #2b Orçamento de RISCO aberto agregado (soma do R em risco das posições) ──
# Diferente dos caps de notional/margem (tamanho/garantia): este soma o RISCO
# REAL em aberto — quanto a banca perde se TODAS as posições abertas baterem o
# stop ao mesmo tempo (Σ |entry−stop|×qty), em % da equity. Bloqueia nova
# entrada se open_risk + nova_trade > teto. Posição pós-TP1 (SL≥entry) conta
# risco ~0 → orçamento rotativo. 0 = DESLIGADO. Default 4 (4% da banca) — teto
# conservador do risco simultâneo em aberto; sobe/baixa via env sem deploy.
MAX_TOTAL_OPEN_RISK_PCT = float(os.getenv("EXCHANGE_MAX_TOTAL_OPEN_RISK_PCT", "4"))

# ── Direction flip (Fase 2) ────────────────────────────────────────────────
# Quando aparece rec na direção OPOSTA a um trade aberto, avalia se a reversão
# é forte o bastante pra justificar fechar a atual e abrir contra. Por padrão
# bloqueia (advisory mode) — só flipa se gate de qualidade + risco passa.
FLIP_ENABLED = os.getenv("FLIP_ENABLED", "true").strip().lower() in ("1", "true", "yes")
FLIP_MIN_SCORE_DELTA = float(os.getenv("FLIP_MIN_SCORE_DELTA", "10"))
FLIP_MIN_TIER_UPGRADE = int(os.getenv("FLIP_MIN_TIER_UPGRADE", "1"))  # nível de upgrade exigido
FLIP_MAX_CURRENT_R = float(os.getenv("FLIP_MAX_CURRENT_R", "0.3"))    # se trade atual > 0.3R, não flipa
FLIP_COOLDOWN_HOURS = float(os.getenv("FLIP_COOLDOWN_HOURS", "4"))     # min horas entre flips no mesmo símbolo

# ── TF upgrade (Fase 3) ────────────────────────────────────────────────────
# Mesma direção, TF maior: ajusta SL/TPs do trade aberto se nova rec é de
# qualidade superior. Pré-TP1 atualiza tudo; pós-TP1 só TP2 (SL fica no BE).
TF_UPGRADE_ENABLED = os.getenv("TF_UPGRADE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
TF_UPGRADE_MIN_SCORE_DELTA = float(os.getenv("TF_UPGRADE_MIN_SCORE_DELTA", "10"))
TF_UPGRADE_MIN_TIER_UPGRADE = int(os.getenv("TF_UPGRADE_MIN_TIER_UPGRADE", "1"))
TF_UPGRADE_BUFFER_PCT = float(os.getenv("TF_UPGRADE_BUFFER_PCT", "0.5"))   # SL novo precisa estar >= 0.5% do preço
TF_UPGRADE_COOLDOWN_HOURS = float(os.getenv("TF_UPGRADE_COOLDOWN_HOURS", "4"))
TF_UPGRADE_NEAR_TP1_R = float(os.getenv("TF_UPGRADE_NEAR_TP1_R", "0.3"))   # bloqueia se r_now > tp1_R - 0.3

# ── Cluster correlation cap (postmortem 28-losses/24h) ─────────────────────
# Diversos losses correlacionados em memes/AI numa mesma janela. Limita
# trades abertos simultâneos por cluster. Base symbol extraído do ticker
# (ex: PEPE/USDT:USDT → PEPE). Símbolos fora de qualquer cluster vão pra
# "other" (não compartilham cap entre si).
SYMBOL_CLUSTERS = {
    # Expandido pós-postmortem 04/06: PEOPLE, MON, MEW, PENGU, TURBO faltavam ou
    # estavam classificados errado. PEOPLE e MEW são meme. MON é gaming.
    "memes": [
        "PEPE", "DOGE", "FLOKI", "BOME", "NEIRO", "MEME", "PENGU", "MEW",
        "TURBO", "WIF", "SHIB", "BONK", "PEOPLE", "POPCAT", "BRETT", "MOG",
        "BABYDOGE", "FARTCOIN", "GOAT", "AI16Z", "ACT", "TRUMP", "MELANIA",
    ],
    "ai_gaming": [
        "GALA", "GPS", "RLS", "AI", "AIXBT", "FET", "AGIX", "RNDR",
        "MON", "BEAM", "PIXEL", "ACE", "BIGTIME", "RON",
    ],
    "l2_infra": ["LINEA", "ARB", "OP", "MATIC", "STRK", "ZK", "MANTA", "BLAST", "MODE", "SCROLL"],
    "defi": ["UNI", "AAVE", "CRV", "1INCH", "DYDX", "GMX", "SUSHI", "COMP", "MKR", "LDO", "ENA"],
    "majors": ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA"],
}
CLUSTER_MAX_OPEN = int(os.getenv("CLUSTER_MAX_OPEN", "2"))

# ── Cluster cap POR DIREÇÃO (postmortem 04/06) ─────────────────────────────
# 22 dos 33 losses do dia foram meme-short. Cluster cap total não basta —
# precisa limitar por direção. Ex: 2 longs no cluster + 2 shorts ok; 4 shorts no
# mesmo cluster, não.
CLUSTER_MAX_OPEN_PER_DIRECTION = int(os.getenv("CLUSTER_MAX_OPEN_PER_DIRECTION", "2"))

# ── Per-symbol SL cooldown (postmortem 04/06) ──────────────────────────────
# FLOKI/NEIRO/PEOPLE/GALA bateram SL múltiplas vezes seguidas (3-4× cada).
# Bloqueia novas entradas no MESMO símbolo por N horas após um SL. Override
# via env SYMBOL_SL_COOLDOWN_HOURS=0 desativa.
SYMBOL_SL_COOLDOWN_HOURS = float(os.getenv("SYMBOL_SL_COOLDOWN_HOURS", "4"))

# ── Directional regime guard (postmortem 04/06) ────────────────────────────
# Se nas últimas N horas N+ SLs aconteceram na MESMA direção, pausa novas
# entradas nessa direção por 1h. Detecta regime adverso em tempo real.
REGIME_GUARD_WINDOW_HOURS = float(os.getenv("REGIME_GUARD_WINDOW_HOURS", "2"))
REGIME_GUARD_MAX_SL = int(os.getenv("REGIME_GUARD_MAX_SL", "3"))
REGIME_GUARD_PAUSE_HOURS = float(os.getenv("REGIME_GUARD_PAUSE_HOURS", "1"))

# ── Daily SL-rate breaker (taxa de acerto diária por direção) ───────────────
# Diferente do regime guard (rajada curta), este olha a TAXA de SL do dia por
# direção. A partir de BREAKER_MIN_SAMPLE recomendações DECIDIDAS (won+lost,
# sem expired) numa direção, se a fração que deu SL >= BREAKER_SL_RATE, pausa
# ESSA direção por BREAKER_PAUSE_HOURS. Mede sobre RecommendationSnapshot
# (painel inteiro), age na execução real e na recomendação. Após a pausa, só
# conta resoluções NOVAS (começo limpo) — não re-pausa pela estatística velha.
BREAKER_MIN_SAMPLE = int(os.getenv("BREAKER_MIN_SAMPLE", "15"))
BREAKER_SL_RATE = float(os.getenv("BREAKER_SL_RATE", "0.40"))
BREAKER_PAUSE_HOURS = float(os.getenv("BREAKER_PAUSE_HOURS", "3"))
# Gatilho 2 (complementar): N SLs CONSECUTIVOS na mesma direção — mesmo que a
# taxa não tenha batido o limiar — pausa a direção (sinal de mercado indeciso/
# picotado). Independe do piso de amostra. Olha só a janela recente.
BREAKER_STREAK_SL = int(os.getenv("BREAKER_STREAK_SL", "5"))
BREAKER_STREAK_WINDOW_HOURS = float(os.getenv("BREAKER_STREAK_WINDOW_HOURS", "24"))

# ── Breaker regime-aware (repique) ──────────────────────────────────────────
# Quando o BTC está empurrando numa direção (repique de alta → LONG a favor;
# queda → SHORT a favor), a direção A FAVOR não deve ser pausada por ruído
# estatístico: o app "entende que pode entrar" no repique. Só falha REAL
# (stops consecutivos) pausa a direção favorecida — e aí exige mais stops que
# o normal. A direção CONTRA o momentum continua com o breaker padrão.
#   BREAKER_REGIME_AWARE      — liga/desliga a lógica (default on).
#   BREAKER_TREND_SKIP_RATE   — na direção a favor, ignora o gatilho de TAXA de
#                               SL (só o de streak vale). Default on.
#   BREAKER_TREND_STREAK_BONUS— stops CONSECUTIVOS extras exigidos p/ pausar a
#                               direção a favor (5 + bônus). Default 2 → 7.
BREAKER_REGIME_AWARE = os.getenv("BREAKER_REGIME_AWARE", "true").strip().lower() not in (
    "0", "false", "no", "off", "",
)
BREAKER_TREND_SKIP_RATE = os.getenv("BREAKER_TREND_SKIP_RATE", "true").strip().lower() not in (
    "0", "false", "no", "off", "",
)
BREAKER_TREND_STREAK_BONUS = int(os.getenv("BREAKER_TREND_STREAK_BONUS", "2"))

# ── Entry throttle (postmortem) ────────────────────────────────────────────
# Cooldown global + max entradas/hora pra prevenir "fome de fila" disparando
# trades em rajada quando o regime de mercado vira contra.
ENTRY_COOLDOWN_SECONDS = int(os.getenv("ENTRY_COOLDOWN_SECONDS", "300"))  # 5min
ENTRY_MAX_PER_HOUR = int(os.getenv("ENTRY_MAX_PER_HOUR", "3"))

# ── Global directional cap (postmortem) ────────────────────────────────────
# Limita exposição direcional total — não fica com 8 longs simultâneos
# quando o mercado vira pra baixo.
MAX_OPEN_PER_DIRECTION = int(os.getenv("MAX_OPEN_PER_DIRECTION", "7"))

# ── Symbol blacklist (postmortem) ──────────────────────────────────────────
# Símbolos temporariamente proibidos por má performance recente. CSV de bases
# (PEPE,NEIRO,...). Case-insensitive. Comparado contra _symbol_base(symbol).
_BLACKLIST_RAW = os.getenv("SYMBOL_BLACKLIST", "NEIRO,PEOPLE,OPN,MEME").strip()
SYMBOL_BLACKLIST: set[str] = {
    s.strip().upper() for s in _BLACKLIST_RAW.split(",") if s.strip()
}

# ── Universo de EXECUÇÃO (decoupling scan↔execução) ────────────────────────
# CSV de bases (BTC,ETH,...) que o bot pode operar com DINHEIRO REAL. Permite
# AMPLIAR o scan/observação SEM ampliar a execução: mesmo que o scan veja 300
# moedas, só executa as que estiverem aqui.
#   • VAZIO (default) = SEM restrição → executa o que o scan trouxe (= comportamento
#     atual; PRD hoje varre 60, então opera as 60). PRD INTOCADO por default.
#   • SETADO = só executa bases na lista (allowlist). Usado quando o scan ampliar.
# Aplicado SÓ em modo live (não-shadow): no DEV (shadow) observa-se tudo.
# Futuro: o motor de rotação (champion/challenger) gerencia essa lista (via DB).
EXEC_UNIVERSE_ALLOWLIST: set[str] = {
    s.strip().upper() for s in os.getenv("EXEC_UNIVERSE_ALLOWLIST", "").split(",") if s.strip()
}

# Allowlist EFETIVA usada no gate de execução. Default = env (comportamento atual,
# intocado). O motor de rotação (FASE 2) pode sobrescrever em runtime via
# `set_exec_allowlist()` quando ROTATION_AUTO_APPLY=on. Mantida em memória pra não
# bater no DB a cada rec; a rotação atualiza isto quando aplica uma mudança.
_EFFECTIVE_ALLOWLIST: set[str] = set(EXEC_UNIVERSE_ALLOWLIST)


def get_exec_allowlist() -> set[str]:
    """Allowlist de execução efetiva (env por default; rotação pode sobrescrever)."""
    return _EFFECTIVE_ALLOWLIST


def set_exec_allowlist(bases) -> None:
    """Sobrescreve a allowlist efetiva em runtime (usado pelo motor de rotação)."""
    global _EFFECTIVE_ALLOWLIST
    _EFFECTIVE_ALLOWLIST = {
        str(b).strip().upper() for b in (bases or []) if str(b).strip()
    }
    log.info(f"[exec-universe] allowlist efetiva atualizada → {len(_EFFECTIVE_ALLOWLIST)} bases")

# ── Score threshold (postmortem) ───────────────────────────────────────────
# Subimos o piso de score para 72 (era implicitamente >=65 via tier A). O
# postmortem mostrou win-rate sensivelmente melhor acima de 75, mas 75
# estava bloqueando trades demais (0 entradas em 48h). 72 = meio-termo
# pra coletar amostra mantendo qualidade. Override via env SCORE_MIN.
#
# V2 (flag SCORE_FORMULA_V2): a escala do score muda (legado 55-100 → V2 ~15-75),
# então o piso de EXECUÇÃO precisa ser rescalado — senão NENHUM auto-trade passa
# (V2 maxa ~71 < 72). 57 em V2 ≈ top ~12-15% dos candidatos de execução (p85-p90
# medido nos snapshots), preservando a MESMA seletividade efetiva do 72 legado.
# Override via env SCORE_MIN_V2.
_SCORE_FORMULA_V2 = os.getenv("SCORE_FORMULA_V2", "false").strip().lower() in ("1", "true", "yes", "on")
if _SCORE_FORMULA_V2:
    SCORE_MIN = float(os.getenv("SCORE_MIN_V2", "57"))
else:
    SCORE_MIN = float(os.getenv("SCORE_MIN", "72"))

# ── #2 Gate de qualidade combinado (score marginal EXIGE edge) ──────────────
# Na escala V2 a calibração é chata e o score quase não separa win-rate (score
# 31+ todos ~0.65-0.68 de P(TP1)). O que separa é o EDGE (A+/funding/padrão/MTF,
# learning-insights N=701). Este gate exige: na BANDA MARGINAL logo acima do
# SCORE_MIN ([SCORE_MIN, SCORE_MIN+MARGIN)), o setup precisa ter >= 1 edge — senão
# pula. Scores bem acima do piso passam livres (já são fortes). Corta justamente
# os "score 57 pelado" que tendem a virar os stops. DEFAULT OFF (dinheiro real):
# liga via env após revisar.
QUALITY_EDGE_GATE_ENABLED = os.getenv("QUALITY_EDGE_GATE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
QUALITY_EDGE_MARGIN = float(os.getenv("QUALITY_EDGE_MARGIN", "6"))   # banda marginal acima do SCORE_MIN
QUALITY_EDGE_MIN = int(os.getenv("QUALITY_EDGE_MIN", "1"))           # edges exigidos na banda

# ── Time-of-day block (postmortem 104 snapshots / 168h) ────────────────────
# HISTÓRICO: ambos os blocks nasceram de postmortems de amostra PEQUENA —
# Sessão EU (7-14 UTC) 50 trades / 42% wr / lift -21.46%; Quinta 67 (depois 124)
# trades / lift ~-10pp. Ambos foram OVERWHELMED por amostra grande e viraram
# net-positivos. Reavaliação 2026-06-19 (shadow, by_session/by_day_of_week):
#   • Quinta: 163 trades / 69.3% wr / +0.44 avg_R / +71.9R (MAIOR total da
#     semana; só -3.6pp do baseline 72.9% e MELHOR que segunda 63.2% que opera
#     livre). O lift -9.6pp evaporou.
#   • Sessão EU (= balde Europe, 7-14 UTC): 250 trades / 68.0% wr / +0.37 avg_R
#     / +92.9R. É a sessão mais fraca (avg_R abaixo do +0.48 baseline) mas
#     CLARAMENTE lucrativa. Como sessões não competem por capital (horas
#     distintas), bloquear só reduz R total.
# DECISÃO (pedido do usuário): liberar AMBOS por padrão. Reversível por env —
# re-bloqueia com BLOCK_DAYS_UTC=thu e/ou BLOCK_HOURS_UTC=7,8,9,10,11,12,13.
BLOCK_DAYS_DEFAULT = ""
BLOCK_HOURS_UTC = os.getenv("BLOCK_HOURS_UTC", "").strip()
_BLOCKED_HOURS: set[int] = set()
if BLOCK_HOURS_UTC:
    try:
        _BLOCKED_HOURS = {int(h.strip()) for h in BLOCK_HOURS_UTC.split(",") if h.strip()}
    except Exception:
        _BLOCKED_HOURS = set()

BLOCK_DAYS_UTC = os.getenv("BLOCK_DAYS_UTC", BLOCK_DAYS_DEFAULT).strip().lower()
_DAY_NAMES = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
_BLOCKED_DAYS: set[str] = set()
if BLOCK_DAYS_UTC:
    _BLOCKED_DAYS = {d.strip() for d in BLOCK_DAYS_UTC.split(",") if d.strip()}


# ── MTF aligned gate (postmortem mtf_aligned=true → 82% wr / +18.68 lift) ──
# Modo:
#   "boost"    (default) → não bloqueia; só loga preferência (futuro: boost qty)
#   "required"           → hard gate: pula se não alinhado
#   "off"                → ignora
MTF_ALIGNED_MODE = os.getenv("MTF_ALIGNED_MODE", "boost").strip().lower()
# Quando "required", quantos TFs maiores precisam estar alinhados pra contar
# como "aligned=true". Default 2 (típico: 1h+4h ambos a favor).
MTF_ALIGNED_MIN_COUNT = int(os.getenv("MTF_ALIGNED_MIN_COUNT", "2"))

# ── Funding directional filter (postmortem funding 0-0.05% → 75% wr) ───────
# Hipótese: funding extremo na mesma direção do trade = trade contra o
# sentiment dominante (mercado já enviesado) → pior expectância.
# funding_rate_pct já vem em % (ex: 0.05 = 0.05%/8h), conforme
# derivatives_service.py (round(funding * 100, 4)).
FUNDING_GATE_ENABLED = os.getenv("FUNDING_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
FUNDING_BLOCK_THRESHOLD = float(os.getenv("FUNDING_BLOCK_THRESHOLD", "0.05"))  # em %

# ── #1 Funding-EV (harvest do edge de funding em trades DIRECIONAIS) ─────────
# O bot já FILTRA funding extremo contra o sentiment (FUNDING_GATE acima). Este
# bloco vai além: contabiliza o funding que a posição vai PAGAR(−) ou COLETAR(+)
# enquanto aberta (Binance funda a cada 8h) e dobra isso na decisão:
#   • LONG paga funding quando funding>0, coleta quando <0; SHORT o inverso.
#   • ev_r>0 = posição COLETA funding (vento a favor); ev_r<0 = SANGRA funding.
# É o "funding harvest" SEM precisar de spot (cash-and-carry exigiria um
# subsistema spot novo): preferimos trades que coletam e evitamos os que sangram
# R em funding. qty cancela na conta → independe do tamanho. Tudo default OFF:
#   - GATE: skip se ev_r < -FUNDING_EV_MAX_DRAG_R (0 = gate desligado).
#   - SIZE: multiplica o risco por 1 + ev_r×K, clampado [MIN, MAX].
FUNDING_EV_ENABLED = os.getenv("FUNDING_EV_ENABLED", "false").strip().lower() in ("1", "true", "yes")
FUNDING_EV_HOLD_WINDOWS = float(os.getenv("FUNDING_EV_HOLD_WINDOWS", "2"))   # nº de janelas de 8h assumidas de hold
FUNDING_EV_MAX_DRAG_R = float(os.getenv("FUNDING_EV_MAX_DRAG_R", "0.0"))     # gate: skip se ev_r < -este. 0 = OFF
FUNDING_EV_SIZE_ENABLED = os.getenv("FUNDING_EV_SIZE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
FUNDING_EV_SIZE_K = float(os.getenv("FUNDING_EV_SIZE_K", "0.5"))             # sensibilidade do tilt de size por ev_r
FUNDING_EV_SIZE_MIN = float(os.getenv("FUNDING_EV_SIZE_MIN", "0.85"))
FUNDING_EV_SIZE_MAX = float(os.getenv("FUNDING_EV_SIZE_MAX", "1.15"))

# ── ATR gate (Fase B Lite, postmortem N=237) ───────────────────────────────
# atr_pct > 3% mostrou lift -10.6pp em n=36. Vol muito alta = SL voa.
ATR_GATE_ENABLED = os.getenv("ATR_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
ATR_BLOCK_THRESHOLD = float(os.getenv("ATR_BLOCK_THRESHOLD", "3.0"))

# ── Score adjusters (Fase B Lite, postmortem N=237) ────────────────────────
# Ajustes baseados em lift_vs_baseline. Aplicados como delta no score antes
# do SCORE_MIN gate. Cap em ±20 pra não dominar o sinal original.
SCORE_ADJUSTERS_ENABLED = os.getenv("SCORE_ADJUSTERS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
SCORE_ADJUSTER_CAP = float(os.getenv("SCORE_ADJUSTER_CAP", "20"))

# ── Proximity gate / anti-chase (alinha execução com o tracker) ─────────────
# O painel marca "perdeu o trem" quando o preço já andou >=1×ATR a favor do
# entry. Até aqui a abertura NÃO checava isso → o bot perseguia preço esticado
# (pior fill, menor expectância). Agora bloqueia abrir quando chase_atr >= teto.
# chase_atr já vem na rec (recommendation_service): signed a favor da direção.
PROXIMITY_GATE_ENABLED = os.getenv("PROXIMITY_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
PROXIMITY_MAX_ATR = float(os.getenv("PROXIMITY_MAX_ATR", "1.0"))

# ── #3 Lane de BREAKOUT com momentum (gated, DEFAULT-OFF) ────────────────────
# O proximity gate acima corta TODO setup que já andou >=1×ATR a favor — inclui
# breakouts legítimos de tendência forte (o "trem" que ainda tem pista). Quando
# a direção está A FAVOR do bias macro (repique/tendência) E há força de
# tendência (ADX), esta lane AFROUXA o teto do proximity de PROXIMITY_MAX_ATR pra
# BREAKOUT_LANE_MAX_ATR — nunca ilimitado. FAIL-CLOSED: qualquer sinal ausente →
# não afrouxa (mantém o teto normal). O anti-chase ESTRUTURAL abaixo continua
# valendo como trava externa (não pega blowoff desde a base). Só tier A/A+.
BREAKOUT_LANE_ENABLED = os.getenv("BREAKOUT_LANE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
BREAKOUT_LANE_MAX_ATR = float(os.getenv("BREAKOUT_LANE_MAX_ATR", "2.0"))   # teto afrouxado
BREAKOUT_LANE_MIN_ADX = float(os.getenv("BREAKOUT_LANE_MIN_ADX", "28"))    # força de tendência mínima

# ── Anti-chase ESTRUTURAL (gated, DEFAULT-OFF) ──────────────────────────────
# O proximity gate acima mede distância do PLANO de entrada — não pega o setup
# que nasce esticado (entry≈mercado após pernada longa, caso HYPE). Este mede o
# esticamento desde a BASE do movimento (struct_chase_atr, vem da rec). Bloqueia
# abrir quando a perna já correu >= teto em ATR. ISENTA retest re-arm (entrada
# limpa no pullback à linha rompida — aí o preço VOLTOU pra perto da base).
STRUCT_CHASE_GATE_ENABLED = os.getenv("STRUCT_CHASE_GATE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
STRUCT_CHASE_MAX_ATR = float(os.getenv("STRUCT_CHASE_MAX_ATR", "5.0"))

# ── R:R gate (geometria estrutural) ─────────────────────────────────────────
# O entry_planner já calcula stop/TP por estrutura (swing low/high, OB, pools de
# liquidez). Mas até aqui um setup com stop longe e alvo perto (R:R fraco) abria
# igual — expectância ruim. Este gate exige um R:R mínimo medido SOBRE O PRÓPRIO
# plano estrutural (entry→stop vs entry→TP). Complementa o anti-chase: aquele
# garante que o mercado não fugiu do entry; este garante que a geometria do
# setup vale o risco. Aplica em shadow e live. 0 desliga cada piso.
RR_GATE_ENABLED = os.getenv("RR_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
MIN_RR_TP1_EXEC = float(os.getenv("MIN_RR_TP1_EXEC", "0.7"))   # TP1 (parcial) >= 0.7R
MIN_RR_TP2_EXEC = float(os.getenv("MIN_RR_TP2_EXEC", "1.5"))   # TP2 (alvo final) >= 1.5R

# ── Liquidity gate (Fase 2) ─────────────────────────────────────────────────
# A allowlist já restringe execução às mais líquidas, mas é ESTÁTICA: se o
# volume de uma moeda secar ou o spread abrir, o fill sai caro (slippage real).
# Este gate mede no momento da execução: volume 24h em USD (volume_base × preço)
# e o spread bid/ask. Fail-soft — erro de dado NÃO bloqueia (allowlist+sizing
# ainda protegem). 0 desliga cada piso/teto. Aplica shadow+live.
LIQUIDITY_GATE_ENABLED = os.getenv("LIQUIDITY_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
MIN_QUOTE_VOL_24H_USD = float(os.getenv("MIN_QUOTE_VOL_24H_USD", "10000000"))  # $10M/24h
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.25"))                    # 0.25%

# ── Exec size damper (liquidez/ATR-aware) ───────────────────────────────────
# O gate de ATR/liquidez é BINÁRIO (bloqueia ou não). Mas o postmortem (LINK,
# DOGE) mostrou stop-slippage em moedas LÍQUIDAS — não é profundidade de book,
# é vol/momentum. E a feature-analysis (N=654) achou: atr_pct <1.65% rende
# ~76%/0.54R vs >1.65% ~70%/0.42R (a pior faixa, Q3 1.65–2.66%, passa pelo gate
# de 3%). Este damper REDUZ o size (não bloqueia) de forma graduada quando:
#   • atr_pct sobe de ATR_DAMP_LO→HI (size cai 1.0→ATR_DAMP_MULT_MIN), e/ou
#   • a posição vira fatia relevante do volume 24h (participação) — NO-OP nos
#     tamanhos atuais (~0.0003% do vol), futuro-proof pro ramp de size.
# DEFENSIVO (teto 1.0, só reduz), fail-soft (dado ausente = sem damp), compõe
# multiplicativo com conviction_mult e LIVE_SIZE_MULT. Flag OFF = NO-OP total.
EXEC_SIZE_DAMP_ENABLED = os.getenv("EXEC_SIZE_DAMP_ENABLED", "false").strip().lower() in ("1", "true", "yes")
ATR_DAMP_LO = float(os.getenv("ATR_DAMP_LO", "1.65"))          # %: início do damp
ATR_DAMP_HI = float(os.getenv("ATR_DAMP_HI", "3.0"))           # %: damp máximo (= block gate)
ATR_DAMP_MULT_MIN = float(os.getenv("ATR_DAMP_MULT_MIN", "0.6"))   # size mínimo por ATR
LIQ_DAMP_PART_LO = float(os.getenv("LIQ_DAMP_PART_LO", "0.003"))   # 0.3% do vol 24h: início
LIQ_DAMP_PART_HI = float(os.getenv("LIQ_DAMP_PART_HI", "0.02"))    # 2% do vol 24h: damp máx
LIQ_DAMP_MULT_MIN = float(os.getenv("LIQ_DAMP_MULT_MIN", "0.5"))   # size mínimo por participação

# ── Sizing por FAIXA de liquidez da moeda ───────────────────────────────────
# Diferente do size-damp acima (que mira PARTICIPAÇÃO notional/vol, NO-OP em
# tamanhos pequenos), este olha o VOLUME 24h absoluto da própria moeda e aplica
# "mão menor" em moedas magras. Encaixe pro piso de rotação 350: moedas no
# rank 200-350 (~$2-6M/dia) entram com size reduzido — aprendem e lucram quando
# o trade for a favor, arriscam ainda menos quando for contra. O risco já é
# fixo por trade (1R com SL), então isto só encolhe o R nas magras.
# DEFENSIVO (teto 1.0, só reduz), fail-soft (vol ausente = mão cheia), compõe
# multiplicativo com edge/conviction/LIVE_SIZE_MULT/size-damp.
LIQ_TIER_SIZING_ENABLED = os.getenv("LIQ_TIER_SIZING_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Limiares de volume 24h (USD) e multiplicadores por faixa. Acima do HI = ×1.0.
LIQ_TIER_VOL_HI = float(os.getenv("LIQ_TIER_VOL_HI", "50000000"))   # ≥$50M → mão cheia
LIQ_TIER_VOL_MID = float(os.getenv("LIQ_TIER_VOL_MID", "10000000")) # ≥$10M → ×MULT_MID
LIQ_TIER_VOL_LO = float(os.getenv("LIQ_TIER_VOL_LO", "3000000"))    # ≥$3M  → ×MULT_LO; abaixo → ×MULT_MIN
LIQ_TIER_MULT_MID = float(os.getenv("LIQ_TIER_MULT_MID", "0.75"))
LIQ_TIER_MULT_LO = float(os.getenv("LIQ_TIER_MULT_LO", "0.5"))
LIQ_TIER_MULT_MIN = float(os.getenv("LIQ_TIER_MULT_MIN", "0.35"))   # <$3M (rank ~250-350)

# ── #6 Sizing por REGIME — mão menor em regime adverso ──────────────────────
# Complementa o regime_service: quando o macro está adverso (downgrade_alt_longs
# = BTC_DOMINANT ou ALT_RISK_OFF), o regime já REBAIXA o tier do long de alt
# (A+→A→B→reject). Este gate dá o passo seguinte no SIZE: mesmo o setup que
# sobrevive ao corte de tier entra com MÃO MENOR. DEFENSIVO (só reduz),
# fail-soft (regime n/d = mão cheia), compõe multiplicativo com liq-tier/edge/
# LIVE_SIZE_MULT. DEFAULT OFF (NO-OP) — liga via env após revisar.
REGIME_SIZING_ENABLED = os.getenv("REGIME_SIZING_ENABLED", "false").strip().lower() in ("1", "true", "yes")
REGIME_SIZE_MULT_ALT_LONG = float(os.getenv("REGIME_SIZE_MULT_ALT_LONG", "0.5"))  # long de alt em regime adverso

# ── Filler FORA da allowlist (modelo de slots) ──────────────────────────────
# Quando ligado, o bot pode abrir posições FORA da allowlist de execução como
# FILLER de slot ocioso: só tier A/A+ (tier B já é cortado antes), teto de
# FILLER_FORA_MAX simultâneas e nunca passando do total de slots (prioriza
# DENTRO, que é processado primeiro no ciclo). Size reduzido (×SIZE_MULT). A
# regra DESLIGA sozinha quando a allowlist atinge FILLER_FORA_OFF_AT moedas
# (aí opera só DENTRO). DEFAULT OFF (NO-OP) — não muda nada até ligar a env.
FILLER_FORA_ENABLED = os.getenv("FILLER_FORA_ENABLED", "false").strip().lower() in ("1", "true", "yes")
FILLER_FORA_MAX = int(os.getenv("FILLER_FORA_MAX", "3"))                   # teto de posições FORA simultâneas
FILLER_FORA_SIZE_MULT = float(os.getenv("FILLER_FORA_SIZE_MULT", "0.75"))  # size FORA vs DENTRO
FILLER_FORA_OFF_AT = int(os.getenv("FILLER_FORA_OFF_AT", "350"))           # allowlist ≥ isto → desliga filler
FILLER_TOTAL_SLOTS = int(os.getenv("PORTFOLIO_MAX_OPEN_POSITIONS", "5"))   # espelha portfolio_service
# Circuit-breaker do FORA: ao acumular N stops do FORA (desde o último TP2 cheio),
# PAUSA novas entradas FORA. Exceção: se ainda no LUCRO do dia (e sem FORA aberto),
# libera 1 probe; se o probe der TP1+TP2 (closed_tp2), a contagem zera (despausa).
# Se o probe falhar e o dia sair do lucro, fica totalmente pausado até o próximo dia.
FILLER_FORA_STOP_STREAK = int(os.getenv("FILLER_FORA_STOP_STREAK", "4"))
# Teste cauteloso de size: os primeiros FILLER_FORA_TEST_N trades FORA abertos a
# partir de FILLER_FORA_TEST_START_AT usam SIZE_MULT reduzido (TEST_SIZE_MULT);
# depois de N, volta sozinho ao FILLER_FORA_SIZE_MULT normal. DEFAULT OFF (N=0).
FILLER_FORA_TEST_N = int(os.getenv("FILLER_FORA_TEST_N", "0"))               # 0 = desligado
FILLER_FORA_TEST_SIZE_MULT = float(os.getenv("FILLER_FORA_TEST_SIZE_MULT", "0.50"))
FILLER_FORA_TEST_START_AT = os.getenv("FILLER_FORA_TEST_START_AT", "").strip()  # ISO; conta só FORA abertos após isto
# Slot livre pós-TP1/BE: quando ON, posição já no breakeven (phase=='post_tp1') não
# conta nos slots DENTRO/FORA — libera vaga pra novo trade (prioriza DENTRO via ordem
# de processamento; FORA entra como filler na vaga liberada). Espelha portfolio_service
# e kill_switch. DEFAULT OFF (NO-OP).
SLOT_FREE_AFTER_TP1_BE = os.getenv("SLOT_FREE_AFTER_TP1_BE", "false").strip().lower() in ("1", "true", "yes", "on")

# ── P(TP1) gate (calibração) ────────────────────────────────────────────────
# rec.prob_tp1 = P(TP1) calibrada por bin de score (calibration_service). Pula
# setups com probabilidade calibrada baixa de bater o TP1. NO-OP-SAFE: quando a
# calibração não está madura (prob_tp1=None), não filtra nada — começa a morder
# sozinho quando amadurece. Env-tunável; 0 desliga.
PROB_TP1_GATE_ENABLED = os.getenv("PROB_TP1_GATE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
MIN_PROB_TP1_EXEC = float(os.getenv("MIN_PROB_TP1_EXEC", "0.45"))  # 45% calibrado

# ── Diagnóstico: motivo do último skip por símbolo ──────────────────────────
# Pra responder "por que a tier A não virou trade?" sem caçar log. Guarda o
# último motivo de skip por símbolo (cap de tamanho). Exposto via API.
_LAST_SKIP_REASONS: dict[str, dict] = {}
_SKIP_REASONS_MAX = 200


def _record_skip(rec: dict, gate: str, reason: str) -> None:
    """Registra por que uma rec (tier A/A+) não virou trade. Best-effort."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        sym = rec.get("symbol") or "?"
        if len(_LAST_SKIP_REASONS) >= _SKIP_REASONS_MAX and sym not in _LAST_SKIP_REASONS:
            # cap atingido: remove o mais antigo
            oldest = min(_LAST_SKIP_REASONS, key=lambda k: _LAST_SKIP_REASONS[k].get("ts", ""))
            _LAST_SKIP_REASONS.pop(oldest, None)
        _LAST_SKIP_REASONS[sym] = {
            "symbol": sym,
            "gate": gate,
            "reason": reason,
            "tier": rec.get("tier"),
            "score": rec.get("score"),
            "direction": rec.get("direction"),
            "timeframe": rec.get("timeframe"),
            "ts": _dt.now(_tz.utc).isoformat(),
        }
        # Persistência durável (contador por gate/dia) — sobrevive redeploy.
        # Fire-and-forget: nunca bloqueia nem derruba o loop de execução.
        _schedule_skip_persist(gate, reason, sym)
    except Exception:
        pass


def _schedule_skip_persist(gate: str, reason: str, sym: str) -> None:
    """Agenda o upsert do contador de skip sem bloquear. Best-effort total:
    se não houver loop async rodando ou o DB estiver desabilitado, vira no-op."""
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_skip_stat(gate, reason, sym))
    except RuntimeError:
        # Sem event loop (ex.: chamada sync isolada) — ignora persistência.
        pass
    except Exception:
        pass


async def _persist_skip_stat(gate: str, reason: str, sym: str) -> None:
    """Upsert do contador (gate, dia-UTC) na tabela skip_reason_stats.
    Bounded por construção (~20 gates × N dias). Fail-soft: qualquer erro de DB
    é engolido — assertividade nunca pode afetar a execução."""
    try:
        from db import DB_ENABLED, get_session
        if not DB_ENABLED:
            return
        from datetime import datetime as _dt2, timezone as _tz2
        from sqlalchemy.dialects.postgresql import insert as _pg_insert
        from models.skip_reason_stat import SkipReasonStat
        now = _dt2.now(_tz2.utc)
        day = now.date()
        reason_s = (reason or "")[:255]
        sym_s = (sym or "")[:50]
        stmt = _pg_insert(SkipReasonStat).values(
            gate=(gate or "?")[:40], day=day, count=1,
            last_reason=reason_s, last_symbol=sym_s, last_seen=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["gate", "day"],
            set_={
                "count": SkipReasonStat.count + 1,
                "last_reason": stmt.excluded.last_reason,
                "last_symbol": stmt.excluded.last_symbol,
                "last_seen": stmt.excluded.last_seen,
            },
        )
        async with get_session() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception:
        pass


def get_skip_reasons() -> list[dict]:
    """Snapshot dos últimos motivos de skip (mais recentes primeiro)."""
    try:
        return sorted(
            _LAST_SKIP_REASONS.values(),
            key=lambda r: r.get("ts", ""),
            reverse=True,
        )
    except Exception:
        return list(_LAST_SKIP_REASONS.values())


def exec_verdict(rec: dict) -> dict:
    """Avaliador READ-ONLY dos gates de QUALIDADE do bot (R:R, P(TP1), liquidez)
    — MESMA lógica e MESMOS limites do loop de execução, sem tocar no loop real
    nem registrar skip nem fazer I/O. Fonte única da verdade: reaproveita os
    thresholds deste módulo (RR/PROB/LIQUIDITY) pra que o app possa anexar um
    veredito a cada recomendação e mostrar "o bot operaria / não operaria" com
    exatamente o mesmo critério, sem duplicar regra que poderia divergir.

    Espera um dict com (todos opcionais, fail-soft):
        entry, stop_loss, tp1, tp2, prob_tp1, quote_vol_usd, spread_pct

    Retorna:
        {
          "ok": bool,              # passaria nos 3 gates de qualidade
          "blocked_by": str|None,  # "rr-gate" | "prob-gate" | "liquidity-gate"
          "reason": str|None,      # motivo PT-BR (mesmo texto do bot)
          "checks": {rr1, rr2, prob_tp1, quote_vol_usd, spread_pct},
        }
    Ordem de avaliação espelha o loop: R:R → P(TP1) → liquidez. Dado ausente
    (None/0) NÃO bloqueia — igual ao fail-soft do gate de liquidez no loop.
    """
    def _f(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    entry = _f(rec.get("entry"))
    stop = _f(rec.get("stop_loss"))
    tp1 = _f(rec.get("tp1"))
    tp2 = _f(rec.get("tp2"))
    prob = _f(rec.get("prob_tp1"))
    qvol = _f(rec.get("quote_vol_usd"))
    spread = _f(rec.get("spread_pct"))

    rr1 = rr2 = None
    if entry and stop is not None:
        risk = abs(entry - stop)
        if risk > 0:
            rr1 = abs(tp1 - entry) / risk if tp1 else None
            rr2 = abs(tp2 - entry) / risk if tp2 else None

    checks = {
        "rr1": round(rr1, 2) if rr1 is not None else None,
        "rr2": round(rr2, 2) if rr2 is not None else None,
        "prob_tp1": prob,
        "quote_vol_usd": qvol,
        "spread_pct": round(spread, 4) if spread is not None else None,
    }

    # ── R:R gate (geometria) ──
    if RR_GATE_ENABLED:
        if rr1 is not None and MIN_RR_TP1_EXEC > 0 and rr1 < MIN_RR_TP1_EXEC:
            return {"ok": False, "blocked_by": "rr-gate",
                    "reason": f"R:R TP1 {rr1:.2f} < mín {MIN_RR_TP1_EXEC}", "checks": checks}
        if rr2 is not None and MIN_RR_TP2_EXEC > 0 and rr2 < MIN_RR_TP2_EXEC:
            return {"ok": False, "blocked_by": "rr-gate",
                    "reason": f"R:R TP2 {rr2:.2f} < mín {MIN_RR_TP2_EXEC}", "checks": checks}

    # ── P(TP1) gate (calibração) ── no-op-safe quando prob=None (calib imatura)
    if PROB_TP1_GATE_ENABLED and MIN_PROB_TP1_EXEC > 0:
        if prob is not None and prob < MIN_PROB_TP1_EXEC:
            return {"ok": False, "blocked_by": "prob-gate",
                    "reason": f"P(TP1) {prob*100:.0f}% < mín {MIN_PROB_TP1_EXEC*100:.0f}%",
                    "checks": checks}

    # ── Liquidity gate ── fail-soft: dado ausente (None/0) não bloqueia
    if LIQUIDITY_GATE_ENABLED:
        if MIN_QUOTE_VOL_24H_USD > 0 and qvol and qvol > 0 and qvol < MIN_QUOTE_VOL_24H_USD:
            return {"ok": False, "blocked_by": "liquidity-gate",
                    "reason": f"vol 24h ${qvol/1e6:.1f}M < mín ${MIN_QUOTE_VOL_24H_USD/1e6:.1f}M",
                    "checks": checks}
        if MAX_SPREAD_PCT > 0 and spread is not None and spread > MAX_SPREAD_PCT:
            return {"ok": False, "blocked_by": "liquidity-gate",
                    "reason": f"spread {spread:.3f}% > máx {MAX_SPREAD_PCT}%", "checks": checks}

    # ── Quality-edge gate ── espelha o gate combinado do loop (banda marginal logo
    # acima do SCORE_MIN exige >= QUALITY_EDGE_MIN edges). Gated: NO-OP quando
    # QUALITY_EDGE_GATE_ENABLED=false. Só morde score >= SCORE_MIN (abaixo é o
    # gate score-min/entry_grade). Mantém app e bot contando a MESMA história.
    if QUALITY_EDGE_GATE_ENABLED and QUALITY_EDGE_MARGIN > 0:
        sc = _f(rec.get("score"))
        try:
            edges_n = int(rec.get("edge_score") or 0)
        except Exception:
            edges_n = 0
        if (
            sc is not None and sc >= SCORE_MIN
            and sc < (SCORE_MIN + QUALITY_EDGE_MARGIN) and edges_n < QUALITY_EDGE_MIN
        ):
            return {"ok": False, "blocked_by": "quality-edge-gate",
                    "reason": (f"score marginal {sc:.0f} (< {SCORE_MIN + QUALITY_EDGE_MARGIN:.0f}) "
                               f"sem edge (>= {QUALITY_EDGE_MIN})"),
                    "checks": checks}

    return {"ok": True, "blocked_by": None, "reason": None, "checks": checks}


def _get_rec_feature(rec: dict, key: str, default=None):
    """Extrai feature da rec acessando rec['signal']. Safety: nunca lança."""
    try:
        sig = rec.get("signal") or {}
        if not isinstance(sig, dict):
            return default
        if key == "mtf_aligned":
            mtf = sig.get("mtf") or {}
            return mtf.get("aligned_count", default) if isinstance(mtf, dict) else default
        if key == "mtf_score":
            mtf = sig.get("mtf") or {}
            return mtf.get("alignment_score", default) if isinstance(mtf, dict) else default
        if key == "funding_pct":
            der = sig.get("derivatives") or {}
            return der.get("funding_rate_pct", default) if isinstance(der, dict) else default
        if key == "funding_sentiment":
            der = sig.get("derivatives") or {}
            return der.get("funding_sentiment", default) if isinstance(der, dict) else default
        if key == "rsi":
            ind = sig.get("indicators") or {}
            return ind.get("rsi", default) if isinstance(ind, dict) else default
        if key == "adx":
            ind = sig.get("indicators") or {}
            return ind.get("adx", default) if isinstance(ind, dict) else default
        if key == "atr_pct":
            ind = sig.get("indicators") or {}
            atr = ind.get("atr") if isinstance(ind, dict) else None
            entry = sig.get("entry") or rec.get("entry") or 0
            if atr and entry:
                try:
                    return (float(atr) / float(entry)) * 100.0
                except Exception:
                    return default
            return default
        if key == "confluence_pct":
            conf = sig.get("confluence") or {}
            return conf.get("pct", default) if isinstance(conf, dict) else default
        if key == "patterns":
            pats = sig.get("patterns") or []
            out = []
            for p in pats if isinstance(pats, list) else []:
                if isinstance(p, dict) and p.get("type"):
                    out.append(p["type"])
            return out
    except Exception:
        return default
    return default


def _hour_bucket(hour_utc: int) -> str:
    """Buckets de hora UTC usados no feature-importance v2."""
    if 7 <= hour_utc <= 13:
        return "eu"
    if 14 <= hour_utc <= 21:
        return "us"
    if 0 <= hour_utc <= 6:
        return "asia"
    return "off"


def _compute_score_adjustment(rec: dict, now_utc: datetime) -> tuple[float, list[str]]:
    """
    Calcula delta de score baseado em features de alto lift (N=237).
    Retorna (delta, reasons[]). Cap em ±SCORE_ADJUSTER_CAP.

    Pesos calibrados a partir de feature-importance v2:
      • atr_pct > 3                   → -8   (lift -10.6pp)
      • confluence_pct < 50           → -4   (lift -4.4pp)
      • rsi < 30                      → -7   (lift -9.2pp)
      • adx > 30                      → -2   (lift -1.9pp, suave)
      • confluence_pct ∈ [50,70]      → +12  (lift +19pp, sweet spot)
      • mtf_aligned (count >=2)       → +8   (lift +12.2pp)
      • atr_pct < 1                   → +6   (lift +8.6pp)
      • adx < 20                      → +6   (lift +12.3pp, mean reversion)
      • funding_sentiment=neutral     → +6   (lift +11.6pp)
      • funding_pct ∈ [-0.05, 0.05]   → +6   (lift +9-22pp)
      • pattern: descending_channel   → +12  (lift +17.2pp)
      • pattern: descending_wedge     → +10  (lift +14.8pp)
      • pattern: inv_h&s              → +7   (lift +10.6pp)
      • pattern: double_bottom        → +6   (lift +10.3pp)
      • hour_utc ∈ asia               → +6   (lift +15.2pp)
      • hour_utc ∈ us                 → +5   (lift +10.5pp)
    """
    delta = 0.0
    reasons: list[str] = []

    atr_pct = _get_rec_feature(rec, "atr_pct")
    if atr_pct is not None:
        try:
            v = float(atr_pct)
            if v > 3.0:
                delta -= 8; reasons.append(f"atr>{v:.2f}(-8)")
            elif v < 1.0:
                delta += 6; reasons.append(f"atr<1(+6)")
        except Exception:
            pass

    conf_pct = _get_rec_feature(rec, "confluence_pct")
    if conf_pct is not None:
        try:
            v = float(conf_pct)
            if v < 50:
                delta -= 4; reasons.append(f"conf<50(-4)")
            elif 50 <= v <= 70:
                delta += 12; reasons.append(f"conf:50-70(+12)")
        except Exception:
            pass

    rsi = _get_rec_feature(rec, "rsi")
    if rsi is not None:
        try:
            v = float(rsi)
            if v < 30:
                delta -= 7; reasons.append(f"rsi<30(-7)")
        except Exception:
            pass

    adx = _get_rec_feature(rec, "adx")
    if adx is not None:
        try:
            v = float(adx)
            if v < 20:
                delta += 6; reasons.append(f"adx<20(+6)")
            elif v > 30:
                delta -= 2; reasons.append(f"adx>30(-2)")
        except Exception:
            pass

    mtf_aligned = _get_rec_feature(rec, "mtf_aligned")
    if mtf_aligned is not None:
        try:
            if int(mtf_aligned) >= 2:
                delta += 8; reasons.append(f"mtf_aligned(+8)")
        except Exception:
            pass

    funding_sent = _get_rec_feature(rec, "funding_sentiment")
    if funding_sent == "neutral":
        delta += 6; reasons.append("funding:neutral(+6)")

    funding_pct = _get_rec_feature(rec, "funding_pct")
    if funding_pct is not None:
        try:
            v = float(funding_pct)
            if -0.05 <= v <= 0.05:
                delta += 6; reasons.append(f"funding_pct∈±0.05(+6)")
        except Exception:
            pass

    patterns = _get_rec_feature(rec, "patterns") or []
    if isinstance(patterns, list):
        pattern_weights = {
            "descending_channel": (12, "desc_channel"),
            "descending_wedge": (10, "desc_wedge"),
            "inverse_head_and_shoulders": (7, "inv_h&s"),
            "double_bottom": (6, "db"),
        }
        for p in patterns:
            if p in pattern_weights:
                w, tag = pattern_weights[p]
                delta += w
                reasons.append(f"pat:{tag}(+{w})")

    bucket = _hour_bucket(now_utc.hour)
    if bucket == "asia":
        delta += 6; reasons.append("hour:asia(+6)")
    elif bucket == "us":
        delta += 5; reasons.append("hour:us(+5)")

    # Cap pra não dominar score original
    if delta > SCORE_ADJUSTER_CAP:
        delta = SCORE_ADJUSTER_CAP
    elif delta < -SCORE_ADJUSTER_CAP:
        delta = -SCORE_ADJUSTER_CAP

    return delta, reasons


def _is_blocked_time(now_utc: datetime) -> tuple[bool, str]:
    """Retorna (blocked, reason)."""
    if now_utc.hour in _BLOCKED_HOURS:
        return True, f"hour_utc={now_utc.hour}"
    dow = _DAY_NAMES.get(now_utc.weekday(), "?")
    if dow in _BLOCKED_DAYS:
        return True, f"dow={dow}"
    return False, ""


_TIER_RANK = {"B": 1, "A": 2, "A+": 3}


def _symbol_base(symbol: str) -> str:
    """Extrai a base do ticker. 'PEPE/USDT:USDT' → 'PEPE', 'BTCUSDT' → 'BTC'."""
    if not symbol:
        return ""
    s = symbol.upper().strip()
    # ccxt-style: 'BASE/QUOTE:SETTLE'
    if "/" in s:
        s = s.split("/", 1)[0]
    # plain 'BASEUSDT' / 'BASEUSD' / 'BASEUSDC'
    for suf in ("USDT", "USDC", "USD", "BUSD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def _get_symbol_cluster(symbol: str) -> str:
    """Retorna nome do cluster do símbolo, ou 'other' se não pertencer a nenhum."""
    base = _symbol_base(symbol)
    if not base:
        return "other"
    for cluster, members in SYMBOL_CLUSTERS.items():
        if base in members:
            return cluster
    return "other"


async def _last_entry_age_seconds() -> float:
    """Segundos desde o último RealTrade auto aberto (qualquer símbolo).
    Retorna inf se nunca houve trade ou DB off."""
    if not DB_ENABLED:
        return float("inf")
    try:
        from datetime import datetime, timezone
        from sqlalchemy import select, desc
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = (
                select(RealTrade.opened_at)
                .where(RealTrade.source == "auto")
                .order_by(desc(RealTrade.opened_at))
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
            if not row or row[0] is None:
                return float("inf")
            last = row[0]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last).total_seconds()
    except Exception as e:
        log.warning(f"[entry-throttle] last_entry_age falhou: {e}")
        return float("inf")


async def _count_entries_last_hour() -> int:
    """Conta RealTrade auto abertos na última hora."""
    if not DB_ENABLED:
        return 0
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, func
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        async with get_session() as session:
            stmt = select(func.count(RealTrade.id)).where(
                RealTrade.source == "auto",
                RealTrade.opened_at >= cutoff,
            )
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception as e:
        log.warning(f"[entry-throttle] count_last_hour falhou: {e}")
        return 0


async def _count_open_by_direction(direction: str) -> int:
    """Conta RealTrade auto open com side==direction (long|short)."""
    if not DB_ENABLED:
        return 0
    try:
        from sqlalchemy import select, func
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(func.count(RealTrade.id)).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == direction,
            )
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception as e:
        log.warning(f"[direction-cap] count falhou: {e}")
        return 0


async def _count_open_in_cluster(cluster: str) -> int:
    """Conta RealTrade open cujo símbolo pertence ao cluster informado."""
    if not DB_ENABLED:
        return 0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade.symbol).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            rows = (await session.execute(stmt)).all()
            return sum(1 for (sym,) in rows if _get_symbol_cluster(sym) == cluster)
    except Exception as e:
        log.warning(f"[cluster-cap] count falhou: {e}")
        return 0


async def _count_open_in_cluster_by_direction(cluster: str, direction: str) -> int:
    """Conta RealTrade open no cluster informado E na direção informada."""
    if not DB_ENABLED:
        return 0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade.symbol).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == direction,
            )
            rows = (await session.execute(stmt)).all()
            return sum(1 for (sym,) in rows if _get_symbol_cluster(sym) == cluster)
    except Exception as e:
        log.warning(f"[cluster-cap-dir] count falhou: {e}")
        return 0


async def _has_recent_sl_on_symbol(symbol: str, hours: float) -> bool:
    """True se o símbolo bateu SL nas últimas `hours` horas (RealTrade fechado)."""
    if not DB_ENABLED or hours <= 0:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with get_session() as session:
            stmt = select(RealTrade.id).where(
                RealTrade.symbol == symbol,
                RealTrade.source == "auto",
                RealTrade.status == "closed_stop",
                RealTrade.closed_at >= cutoff,
            ).limit(1)
            row = (await session.execute(stmt)).first()
            return row is not None
    except Exception as e:
        log.warning(f"[symbol-sl-cooldown] check falhou: {e}")
        return False


async def _count_recent_sl_by_direction(direction: str, hours: float) -> int:
    """Conta SLs recentes na direção informada (RealTrade closed_stop)."""
    if not DB_ENABLED or hours <= 0:
        return 0
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, func
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        async with get_session() as session:
            stmt = select(func.count(RealTrade.id)).where(
                RealTrade.source == "auto",
                RealTrade.status == "closed_stop",
                RealTrade.side == direction,
                RealTrade.closed_at >= cutoff,
            )
            return int((await session.execute(stmt)).scalar() or 0)
    except Exception as e:
        log.warning(f"[regime-guard] count falhou: {e}")
        return 0


# Estado em memória: timestamp do último SL que disparou pausa por direção.
# Quando _count_recent_sl_by_direction(d, REGIME_GUARD_WINDOW_HOURS) >= MAX_SL,
# armamos pause em _REGIME_PAUSE_UNTIL[d] = now + PAUSE_HOURS. Novas entradas
# nessa direção ficam bloqueadas até passar o timestamp.
_REGIME_PAUSE_UNTIL: dict[str, float] = {}


async def _regime_blocked(direction: str) -> tuple[bool, str]:
    """Retorna (blocked, reason). Confere pausa armada + arma nova se preciso.

    Regime-aware: se a direção está A FAVOR do momentum atual do BTC (repique),
    o limiar de SLs pra pausar sobe (BREAKER_TREND_STREAK_BONUS) — o app entende
    que pode entrar no repique e só pausa se a falha for realmente forte."""
    import time
    now = time.time()
    await _sweep_resumed_directions()
    until = _REGIME_PAUSE_UNTIL.get(direction, 0)
    if until > now:
        mins = (until - now) / 60.0
        return True, f"pausa ativa há {mins:.0f}min"

    max_sl = REGIME_GUARD_MAX_SL
    favored = False
    if BREAKER_REGIME_AWARE:
        try:
            from services.regime_service import get_market_bias, direction_favored
            if direction_favored(direction, await get_market_bias()):
                favored = True
                max_sl = REGIME_GUARD_MAX_SL + BREAKER_TREND_STREAK_BONUS
        except Exception as e:
            log.warning(f"[regime-guard] bias falhou (fail-open): {e}")

    sl_count = await _count_recent_sl_by_direction(direction, REGIME_GUARD_WINDOW_HOURS)
    if sl_count >= max_sl:
        _REGIME_PAUSE_UNTIL[direction] = now + REGIME_GUARD_PAUSE_HOURS * 3600
        reason = (
            f"{sl_count} SLs {direction} em {REGIME_GUARD_WINDOW_HOURS:.0f}h — "
            f"pausa {REGIME_GUARD_PAUSE_HOURS:.0f}h"
            + (" (a favor do repique, limiar elevado)" if favored else "")
        )
        await _notify_direction_paused(
            "regime-guard", direction, _REGIME_PAUSE_UNTIL[direction], reason
        )
        return True, reason
    return False, ""


# Estado do breaker de taxa diária. _DAILY_BREAKER_UNTIL = quando a pausa
# expira; _DAILY_BREAKER_CUTOFF = só conta recomendações criadas a partir deste
# instante (vira "now" ao armar → começo limpo após a pausa; reseta no dia UTC).
_DAILY_BREAKER_UNTIL: dict[str, float] = {}
_DAILY_BREAKER_CUTOFF: dict[str, "datetime"] = {}

# Estado de notificação das pausas direcionais: (mech, direction) -> until_ts
# já avisado como "pausado". Evita re-avisar a mesma pausa e permite detectar a
# retomada (quando o until expira, dispara o aviso de "retomada").
_PAUSE_NOTIFIED: dict[tuple[str, str], float] = {}


def _pause_until_for(mech: str, direction: str) -> float:
    if mech == "breaker":
        return _DAILY_BREAKER_UNTIL.get(direction, 0.0)
    if mech == "regime-guard":
        return _REGIME_PAUSE_UNTIL.get(direction, 0.0)
    return 0.0


async def _notify_direction_paused(mech: str, direction: str, until_ts: float, reason: str) -> None:
    """Avisa no Telegram que uma direção foi pausada (uma vez por pausa)."""
    key = (mech, direction)
    prev = _PAUSE_NOTIFIED.get(key)
    if prev is not None and prev >= until_ts - 1:
        return  # já avisamos esta pausa (ou uma que vai até mais tarde)
    _PAUSE_NOTIFIED[key] = until_ts
    try:
        from services.notification_service import send_telegram, fmt_direction_paused
        await send_telegram(
            fmt_direction_paused(mech, direction, until_ts, reason),
            event_type="breaker",
        )
    except Exception as e:
        log.warning(f"[breaker-notify] aviso de pausa {mech}/{direction} falhou: {e}")


async def _sweep_resumed_directions() -> None:
    """Detecta pausas que expiraram e avisa a retomada no Telegram. Chamado no
    início dos checadores de breaker — roda com frequência suficiente pra
    notificar em poucos minutos após a pausa expirar."""
    import time
    now = time.time()
    for key in list(_PAUSE_NOTIFIED.keys()):
        mech, direction = key
        if _pause_until_for(mech, direction) <= now:
            del _PAUSE_NOTIFIED[key]
            try:
                from services.notification_service import send_telegram, fmt_direction_resumed
                await send_telegram(
                    fmt_direction_resumed(mech, direction),
                    event_type="breaker",
                )
            except Exception as e:
                log.warning(f"[breaker-notify] aviso de retomada {mech}/{direction} falhou: {e}")


async def _count_today_decided_by_direction(direction: str, cutoff_dt) -> tuple[int, int]:
    """(decididas, sls) hoje na direção. Decididas = won*+lost (sem expired)."""
    if not DB_ENABLED:
        return 0, 0
    try:
        from sqlalchemy import select, func, case
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot as RS
        won = ("won_tp1", "won_tp1_be", "won_tp2")
        async with get_session() as session:
            stmt = select(
                func.count(RS.id),
                func.coalesce(func.sum(case((RS.status == "lost", 1), else_=0)), 0),
            ).where(
                RS.direction == direction,
                RS.status.in_(("lost",) + won),
                RS.created_at >= cutoff_dt,
            )
            row = (await session.execute(stmt)).one()
            return int(row[0] or 0), int(row[1] or 0)
    except Exception as e:
        log.warning(f"[daily-breaker] count falhou: {e}")
        return 0, 0


async def _trailing_sl_streak(direction: str, cutoff_dt) -> int:
    """Nº de SLs consecutivos mais recentes na direção (won* quebra a sequência).

    Olha só resoluções decididas (won*+lost) dentro da janela recente e após o
    cutoff de limpeza (pós-pausa). Conta a sequência que termina na resolução
    mais recente.
    """
    if not DB_ENABLED:
        return 0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot as RS
        won = ("won_tp1", "won_tp1_be", "won_tp2")
        stmt = select(RS.status).where(
            RS.direction == direction,
            RS.status.in_(("lost",) + won),
            RS.outcome_at >= cutoff_dt,
        ).order_by(RS.outcome_at.desc()).limit(BREAKER_STREAK_SL + BREAKER_TREND_STREAK_BONUS + 5)
        async with get_session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        streak = 0
        for st in rows:
            if st == "lost":
                streak += 1
            else:
                break
        return streak
    except Exception as e:
        log.warning(f"[daily-breaker] streak count falhou: {e}")
        return 0


async def _daily_sl_breaker(direction: str) -> tuple[bool, str]:
    """Breaker por direção. Dois gatilhos:
      1) taxa de SL do dia >= BREAKER_SL_RATE (com >= BREAKER_MIN_SAMPLE amostras);
      2) BREAKER_STREAK_SL SLs consecutivos na janela recente (mercado indeciso).
    Qualquer um pausa a direção por BREAKER_PAUSE_HOURS. (blocked, reason).
    """
    import time
    from datetime import datetime, timezone, timedelta
    now = time.time()
    await _sweep_resumed_directions()
    until = _DAILY_BREAKER_UNTIL.get(direction, 0)
    if until > now:
        mins = (until - now) / 60.0
        return True, f"pausa ativa ({mins:.0f}min restantes)"

    # Regime-aware: se a direção está A FAVOR do momentum do BTC (repique), o app
    # "entende que pode entrar" — ignora o gatilho de TAXA (só stops consecutivos
    # pausam) e exige mais stops seguidos (BREAKER_TREND_STREAK_BONUS) pra pausar.
    favored = False
    if BREAKER_REGIME_AWARE:
        try:
            from services.regime_service import get_market_bias, direction_favored
            favored = direction_favored(direction, await get_market_bias())
        except Exception as e:
            log.warning(f"[daily-breaker] bias falhou (fail-open): {e}")

    async def _arm(reason: str) -> tuple[bool, str]:
        _DAILY_BREAKER_UNTIL[direction] = now + BREAKER_PAUSE_HOURS * 3600
        _DAILY_BREAKER_CUTOFF[direction] = datetime.now(timezone.utc)
        await _notify_direction_paused(
            "breaker", direction, _DAILY_BREAKER_UNTIL[direction], reason
        )
        return True, reason

    # ── Gatilho 1: taxa de SL diária ──────────────────────────────────────
    # Pulado na direção a favor do repique (BREAKER_TREND_SKIP_RATE): ruído
    # estatístico não deve pausar o lado que o mercado está empurrando.
    if BREAKER_MIN_SAMPLE > 0 and not (favored and BREAKER_TREND_SKIP_RATE):
        # cutoff = início do dia UTC, ou após a última pausa (o mais recente)
        start_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = _DAILY_BREAKER_CUTOFF.get(direction)
        if cutoff is None or cutoff < start_day:
            cutoff = start_day
        total, sl = await _count_today_decided_by_direction(direction, cutoff)
        if total >= BREAKER_MIN_SAMPLE and total > 0:
            rate = sl / total
            if rate >= BREAKER_SL_RATE:
                return await _arm(
                    f"{sl}/{total} SL ({rate*100:.0f}%) {direction} hoje — "
                    f"pausa {BREAKER_PAUSE_HOURS:.0f}h"
                )

    # ── Gatilho 2: sequência de SLs consecutivos ──────────────────────────
    # Sempre ativo (inclusive na direção a favor): falha REAL pausa mesmo no
    # repique — mas exigindo mais stops seguidos quando a direção é favorecida.
    if BREAKER_STREAK_SL > 0:
        streak_needed = BREAKER_STREAK_SL + (BREAKER_TREND_STREAK_BONUS if favored else 0)
        window_start = datetime.now(timezone.utc) - timedelta(hours=BREAKER_STREAK_WINDOW_HOURS)
        streak_cutoff = _DAILY_BREAKER_CUTOFF.get(direction)
        if streak_cutoff is None or streak_cutoff < window_start:
            streak_cutoff = window_start
        streak = await _trailing_sl_streak(direction, streak_cutoff)
        if streak >= streak_needed:
            suffix = " (a favor do repique, limiar elevado)" if favored else " (mercado indeciso)"
            return await _arm(
                f"{streak} SL seguidos {direction} — pausa {BREAKER_PAUSE_HOURS:.0f}h{suffix}"
            )

    return False, ""


def _tf_rank_local(tf: str) -> int:
    """Mirror de snapshot_service._tf_rank — SCALP=1, DAY=2, SWING=3."""
    if not tf:
        return 0
    t = tf.strip().lower()
    if t in ("1m", "3m", "5m", "15m"):
        return 1
    if t in ("30m", "1h", "2h"):
        return 2
    return 3


async def _resolve_equity_usd() -> tuple[float, str]:
    """
    Tenta ler equity ao vivo da exchange. Em caso de falha, usa fallback estático.
    Retorna (equity_usd, source) onde source ∈ {"live","cache","fallback"}.
    """
    try:
        from services import exchange_service
        eq = await exchange_service.get_equity()
        if eq.get("ok") and eq.get("total_usd", 0) > 0:
            return float(eq["total_usd"]), eq.get("source", "live")
    except Exception as e:
        log.warning(f"[shadow] get_equity falhou: {e}")
    return VIRTUAL_EQUITY_USD, "fallback"


def env_info() -> dict:
    """Diagnóstico — quanto o shadow está ativo + equity virtual usado pra sizing."""
    prod = _exchange_is_production()
    armed, guard_reason = _live_money_guard()
    return {
        "shadow_enabled": SHADOW_ENABLED,
        "fallback_equity_usd": VIRTUAL_EQUITY_USD,
        "sizing_mode": "live (aborta se equity real indisponível em modo live)",
        "min_notional_usd": MIN_NOTIONAL_USD,
        "max_risk_pct_hard": MAX_RISK_PCT_HARD,
        # Filler FORA (allowlist) — visível pra auditar size/teto/breaker ao vivo
        "filler_fora_enabled": FILLER_FORA_ENABLED,
        "filler_fora_size_mult": FILLER_FORA_SIZE_MULT,
        "filler_fora_max": FILLER_FORA_MAX,
        "filler_fora_off_at": FILLER_FORA_OFF_AT,
        "filler_fora_stop_streak": FILLER_FORA_STOP_STREAK,
        "max_margin_pct_per_trade": MAX_MARGIN_PCT_PER_TRADE,
        "max_total_notional_pct": MAX_TOTAL_NOTIONAL_PCT,
        "max_total_margin_pct": MAX_TOTAL_MARGIN_PCT,
        "exchange_active": os.getenv("EXCHANGE", "binance"),
        # go-live safety rails
        "is_production": prod,
        "live_money_armed": (not SHADOW_ENABLED) and armed,
        "live_money_guard": guard_reason,
        "live_trading_confirmed": LIVE_TRADING_CONFIRM == _LIVE_CONFIRM_PHRASE,
        "live_size_mult": LIVE_SIZE_MULT,
        # #6 sizing por regime (DEFAULT OFF)
        "regime_sizing_enabled": REGIME_SIZING_ENABLED,
        "regime_size_mult_alt_long": REGIME_SIZE_MULT_ALT_LONG,
        # ── Stack de sizing inteligente (auditável p/ validação da fase 2) ────
        # Amplificam risco (>1.0): edge (até EDGE_MULT_MAX), funding-ev-size.
        # Defensivos (≤1.0): conviction (0.8–1.0), exec_size_damp, liq_tier.
        "conviction_sizing_enabled": CONVICTION_SIZING_ENABLED,
        "conviction_mult_range": [CONVICTION_MULT_MIN, CONVICTION_MULT_MAX],
        "conviction_tp2_weight": CONVICTION_TP2_WEIGHT,
        "edge_sizing_enabled": EDGE_SIZING_ENABLED,
        "edge_mult_range": [EDGE_MULT_MIN, EDGE_MULT_MAX],
        "edge_per_edge": EDGE_PER_EDGE,
        "edge_aplus_bonus": EDGE_APLUS_BONUS,
        "edge_noedge_mult": EDGE_NOEDGE_MULT,
        "funding_ev_enabled": FUNDING_EV_ENABLED,
        "funding_ev_max_drag_r": FUNDING_EV_MAX_DRAG_R,
        "funding_ev_size_enabled": FUNDING_EV_SIZE_ENABLED,
        "funding_ev_size_range": [FUNDING_EV_SIZE_MIN, FUNDING_EV_SIZE_MAX],
        "exec_size_damp_enabled": EXEC_SIZE_DAMP_ENABLED,
        "atr_damp_range_pct": [ATR_DAMP_LO, ATR_DAMP_HI],
        "atr_damp_mult_min": ATR_DAMP_MULT_MIN,
        "liq_tier_sizing_enabled": LIQ_TIER_SIZING_ENABLED,
        "liq_tier_mult_min": LIQ_TIER_MULT_MIN,
        # #3 lane de breakout (DEFAULT OFF)
        "breakout_lane_enabled": BREAKOUT_LANE_ENABLED,
        "breakout_lane_max_atr": BREAKOUT_LANE_MAX_ATR,
        "breakout_lane_min_adx": BREAKOUT_LANE_MIN_ADX,
        "proximity_max_atr": PROXIMITY_MAX_ATR,
        # #1 runner com trailing pós-TP2 (DEFAULT OFF) — mecânica no trade_manager
        "runner_enabled": os.getenv("RUNNER_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
        "runner_qty_pct": float(os.getenv("RUNNER_QTY_PCT", "0.20")),
        "runner_atr_mult": float(os.getenv("RUNNER_ATR_MULT", "3.0")),
        # daily SL-rate breaker (por direção)
        "breaker_min_sample": BREAKER_MIN_SAMPLE,
        "breaker_sl_rate": BREAKER_SL_RATE,
        "breaker_pause_hours": BREAKER_PAUSE_HOURS,
        "breaker_streak_sl": BREAKER_STREAK_SL,
        "breaker_streak_window_hours": BREAKER_STREAK_WINDOW_HOURS,
        # breaker regime-aware (repique)
        "breaker_regime_aware": BREAKER_REGIME_AWARE,
        "breaker_trend_skip_rate": BREAKER_TREND_SKIP_RATE,
        "breaker_trend_streak_bonus": BREAKER_TREND_STREAK_BONUS,
        "breaker_paused_directions": {
            d: round((u - __import__("time").time()) / 60.0, 1)
            for d, u in _DAILY_BREAKER_UNTIL.items()
            if u > __import__("time").time()
        },
        "regime_guard_paused_directions": {
            d: round((u - __import__("time").time()) / 60.0, 1)
            for d, u in _REGIME_PAUSE_UNTIL.items()
            if u > __import__("time").time()
        },
        "note": "Sizing: risk_pct nominal; eleva ao mín notional; capa em margin%/trade e total notional%.",
    }


async def preflight_live_checks(
    sample_symbol: str = "BTC/USDT:USDT",
    sample_direction: str = "long",
) -> dict:
    """
    Smoke-test READ-ONLY dos gates que só rodam em live (todos os blocos
    `if not SHADOW_ENABLED:`). NUNCA envia ordem — só exercita cada função de
    gate com inputs de exemplo pra garantir que nenhuma estoura exceção quando
    o primeiro trade real disparar na sexta. Também valida conectividade crítica:
    equity real (não fallback), kill-switch e config da exchange.

    Retorna {ready, all_gates_ok, checks[], env}. `ready` = gates críticos OK.
    Aditivo: não toca em nenhuma decisão de trade nem no caminho de execução.
    """
    checks: list[dict] = []

    def _add(gate: str, ok: bool, detail: str) -> None:
        checks.append({"gate": gate, "ok": bool(ok), "detail": str(detail)[:300]})

    async def _run_db(gate: str, coro) -> None:
        try:
            val = await coro
            _add(gate, True, f"ok → {val}")
        except Exception as e:
            _add(gate, False, f"EXCEPTION: {type(e).__name__}: {e}")

    # 1. Trava de dinheiro real (env)
    try:
        armed, why = _live_money_guard()
        _add("live_money_guard", True, f"armed={armed and not SHADOW_ENABLED} · {why}")
    except Exception as e:
        _add("live_money_guard", False, f"EXCEPTION: {e}")

    # 2. Kill-switch (DB/estado)
    try:
        from services import kill_switch_service
        ks = await kill_switch_service.check_can_trade()
        _add("kill_switch", True, f"allowed={ks.get('allowed')} · {ks.get('reason')}")
    except Exception as e:
        _add("kill_switch", False, f"EXCEPTION: {e}")

    # 3. Equity REAL (não pode ser fallback pra armar live) — bate na exchange
    try:
        from services import exchange_service
        eq = await exchange_service.get_equity()
        src = eq.get("source", "?")
        total = eq.get("total_usd", 0)
        is_real = bool(eq.get("ok")) and src != "fallback" and total > 0
        _add("equity_real", is_real, f"source={src} total=${total} (live exige source≠fallback)")
    except Exception as e:
        _add("equity_real", False, f"EXCEPTION: {e}")

    # 4. Exchange configurada (chaves presentes)
    try:
        from services import exchange_service
        _add("exchange_configured", exchange_service.is_configured(), str(exchange_service.env_info()))
    except Exception as e:
        _add("exchange_configured", False, f"EXCEPTION: {e}")

    # 5. Time-of-day block (lógica pura)
    try:
        blocked, reason = _is_blocked_time(datetime.now(timezone.utc))
        _add("time_block", True, f"blocked_now={blocked} · {reason}")
    except Exception as e:
        _add("time_block", False, f"EXCEPTION: {e}")

    # 6. Cluster resolver (lógica pura)
    try:
        cluster = _get_symbol_cluster(sample_symbol)
        _add("symbol_cluster", True, f"{sample_symbol} → cluster={cluster}")
    except Exception as e:
        cluster = "other"
        _add("symbol_cluster", False, f"EXCEPTION: {e}")

    # 7-16. Gates apoiados em DB (validam conectividade + queries sem abrir nada)
    await _run_db("entry_age_seconds", _last_entry_age_seconds())
    await _run_db("entries_last_hour", _count_entries_last_hour())
    await _run_db("open_by_direction", _count_open_by_direction(sample_direction))
    await _run_db("open_in_cluster", _count_open_in_cluster(cluster))
    await _run_db("open_in_cluster_dir", _count_open_in_cluster_by_direction(cluster, sample_direction))
    await _run_db("recent_sl_on_symbol", _has_recent_sl_on_symbol(sample_symbol, SYMBOL_SL_COOLDOWN_HOURS or 6.0))
    await _run_db("regime_blocked", _regime_blocked(sample_direction))
    await _run_db("daily_sl_breaker", _daily_sl_breaker(sample_direction))
    await _run_db("opposite_open_trade", _find_opposite_open_trade(sample_symbol, sample_direction))
    await _run_db("open_notional_usd", _open_notional_usd())
    await _run_db("open_margin_usd", _open_margin_usd())

    # Gates críticos: precisam passar pra armar dinheiro real com segurança.
    CRITICAL = {"equity_real", "kill_switch", "exchange_configured"}
    all_ok = all(c["ok"] for c in checks)
    crit_ok = all(c["ok"] for c in checks if c["gate"] in CRITICAL)
    failures = [c["gate"] for c in checks if not c["ok"]]

    try:
        armed_flag = (not SHADOW_ENABLED) and _live_money_guard()[0]
    except Exception:
        armed_flag = False

    return {
        "ready": crit_ok,
        "all_gates_ok": all_ok,
        "failures": failures,
        "shadow_enabled": SHADOW_ENABLED,
        "live_money_armed": armed_flag,
        "sample": {"symbol": sample_symbol, "direction": sample_direction},
        "checks": checks,
        "env": env_info(),
    }


async def _open_notional_usd() -> float:
    """Soma notional (entry × qty) dos trades reais auto abertos. Pra cap agregado."""
    if not DB_ENABLED:
        return 0.0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            rows = (await session.execute(stmt)).scalars().all()
            total = 0.0
            for t in rows:
                ep = float(t.entry_price or 0)
                q = float(t.qty or 0)
                total += ep * q
            return total
    except Exception as e:
        log.warning(f"[shadow] _open_notional_usd falhou: {e}")
        return 0.0


async def _open_auto_counts_by_allowlist() -> tuple[int, int]:
    """(n_dentro, n_fora) — posições auto ABERTAS dentro vs fora da allowlist de
    execução, pro modelo de slots do filler FORA. Propaga erro pro chamador
    (o fail-safe lá bloqueia o filler no ciclo)."""
    if not DB_ENABLED:
        return 0, 0
    from sqlalchemy import select
    from db import get_session
    from models.real_trade import RealTrade
    allow = get_exec_allowlist()
    async with get_session() as session:
        stmt = select(RealTrade).where(
            RealTrade.status == "open",
            RealTrade.source == "auto",
        )
        rows = (await session.execute(stmt)).scalars().all()
    n_dentro = n_fora = 0
    for t in rows:
        # Slot livre pós-TP1/BE: posição no breakeven não ocupa slot (libera vaga).
        if SLOT_FREE_AFTER_TP1_BE and getattr(t, "phase", None) == "post_tp1":
            continue
        base = _symbol_base(t.symbol or "")
        if allow and base and base not in allow:
            n_fora += 1
        else:
            n_dentro += 1
    return n_dentro, n_fora


async def _filler_fora_brake_state() -> tuple[int, float]:
    """(stop_streak, daily_pnl_usd) pro circuit-breaker do filler FORA.
    stop_streak = nº de stops do FORA desde o último TP2 cheio (closed_tp2 zera;
    neutros como expiry/BE/TP1-parcial nem contam nem zeram). daily_pnl_usd = soma
    de pnl_usd dos trades FECHADOS hoje (UTC). Propaga erro pro chamador (o
    fail-safe lá PAUSA o filler no ciclo)."""
    if not DB_ENABLED:
        return 0, 0.0
    from datetime import datetime, timezone
    from sqlalchemy import select, func, desc
    from db import get_session
    from models.real_trade import RealTrade
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session() as session:
        pnl = float((await session.execute(
            select(func.coalesce(func.sum(RealTrade.pnl_usd), 0.0))
            .where(RealTrade.closed_at >= start)
            .where(RealTrade.status != "open")
        )).scalar() or 0.0)
        rows = (await session.execute(
            select(RealTrade)
            .where(RealTrade.source == "auto")
            .where(RealTrade.status != "open")
            .where(RealTrade.notes.like("%[filler]%"))
            .order_by(desc(RealTrade.closed_at))
            .limit(50)
        )).scalars().all()
    streak = 0
    for t in rows:
        if t.status == "closed_tp2":
            break
        if t.status == "closed_stop":
            streak += 1
    return streak, pnl


async def _filler_fora_test_opened_count() -> int:
    """Quantos trades FORA (auto [filler]) já foram ABERTOS desde
    FILLER_FORA_TEST_START_AT — conta TODOS (abertos + fechados), pois o teste é
    sobre quantos foram disparados, não quantos seguem vivos. Sem START_AT, conta
    desde sempre. Propaga erro (fail-safe no chamador usa size de teste por garantia)."""
    if not DB_ENABLED:
        return 0
    from datetime import datetime, timezone
    from sqlalchemy import select, func
    from db import get_session
    from models.real_trade import RealTrade
    cutoff = None
    if FILLER_FORA_TEST_START_AT:
        try:
            cutoff = datetime.fromisoformat(FILLER_FORA_TEST_START_AT.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            cutoff = None
    async with get_session() as session:
        stmt = (
            select(func.count())
            .select_from(RealTrade)
            .where(RealTrade.source == "auto")
            .where(RealTrade.notes.like("%[filler]%"))
        )
        if cutoff is not None:
            stmt = stmt.where(RealTrade.opened_at >= cutoff)
        return int((await session.execute(stmt)).scalar() or 0)


async def _open_margin_usd() -> float:
    """Soma a MARGEM (notional/leverage) dos trades reais auto abertos. Pra cap
    de capital comprometido — 'X% da banca aberta no máx'."""
    if not DB_ENABLED:
        return 0.0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            rows = (await session.execute(stmt)).scalars().all()
            total = 0.0
            for t in rows:
                ep = float(t.entry_price or 0)
                q = float(t.qty or 0)
                lev = max(int(t.leverage or 1), 1)
                total += (ep * q) / lev
            return total
    except Exception as e:
        log.warning(f"[shadow] _open_margin_usd falhou: {e}")
        return 0.0


async def _open_risk_usd() -> float:
    """Soma o RISCO em aberto (USD) dos trades reais auto — quanto a banca perde
    se cada posição bater seu stop ATUAL. Pré-TP1 usa o stop planejado; pós-TP1
    o SL já está em/above entry (BE estrutural) → risco ~0 (clampa em 0, não soma
    negativo). Base do orçamento de risco agregado (#2b)."""
    if not DB_ENABLED:
        return 0.0
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            rows = (await session.execute(stmt)).scalars().all()
            total = 0.0
            for t in rows:
                ep = float(t.entry_price or 0)
                q = float(t.qty or 0)
                # SL efetivo: o atual (pós-TP1 sobe pra BE) senão o planejado.
                sl = t.sl_current_price if t.sl_current_price is not None else t.planned_stop
                if sl is None or ep <= 0 or q <= 0:
                    continue
                side = (t.side or "long").lower()
                # risco por unidade só conta se o stop está ADVERSO ao entry
                if side == "long":
                    risk_per_unit = max(0.0, ep - float(sl))
                else:
                    risk_per_unit = max(0.0, float(sl) - ep)
                total += risk_per_unit * q
            return total
    except Exception as e:
        log.warning(f"[shadow] _open_risk_usd falhou: {e}")
        return 0.0


def _conviction_mult(rec: dict) -> tuple[float, str]:
    """Multiplicador de tamanho por CONVICÇÃO (#2a). Mapeia P(TP1) calibrada
    [LO..HI] linearmente em [MIN..MAX], clampado. NO-OP-SAFE: desligado ou sem
    prob calibrada → 1.0 (não mexe). Os caps duros de _compute_qty mandam depois."""
    if not CONVICTION_SIZING_ENABLED:
        return 1.0, "disabled"
    p = rec.get("prob_tp1")
    try:
        p = float(p) if p is not None else None
    except Exception:
        p = None
    if p is None:
        return 1.0, "no-prob"  # calibração imatura — não escala
    lo, hi = CONVICTION_PROB_LO, CONVICTION_PROB_HI
    frac1 = (p - lo) / (hi - lo) if hi > lo else 0.5
    frac1 = max(0.0, min(1.0, frac1))

    # #2 blend P(TP2): peso aditivo. w=0 → idêntico ao TP1-only (NO-OP). Se a
    # prob TP2 não está madura (None) mesmo com w>0, cai pro frac do TP1 sozinho
    # (não penaliza por falta de dado).
    frac = frac1
    tag = f"p1={p*100:.0f}%"
    w = CONVICTION_TP2_WEIGHT
    if w > 0.0:
        p2 = rec.get("prob_tp2")
        try:
            p2 = float(p2) if p2 is not None else None
        except Exception:
            p2 = None
        if p2 is not None:
            lo2, hi2 = CONVICTION_TP2_PROB_LO, CONVICTION_TP2_PROB_HI
            frac2 = (p2 - lo2) / (hi2 - lo2) if hi2 > lo2 else 0.5
            frac2 = max(0.0, min(1.0, frac2))
            frac = (1.0 - w) * frac1 + w * frac2
            tag = f"p1={p*100:.0f}% p2={p2*100:.0f}% w={w:.2f}"

    mult = CONVICTION_MULT_MIN + frac * (CONVICTION_MULT_MAX - CONVICTION_MULT_MIN)
    return mult, f"{tag}→×{mult:.2f}"


def _edge_mult(rec: dict) -> tuple[float, str]:
    """#1 — Multiplicador de tamanho por EDGE (A+/funding/padrão/MTF). Escala o
    risco/trade pela contagem de edges da rec, com bônus se A+ presente e leve
    desconto se NENHUM edge. NO-OP-SAFE: desligado → 1.0. Clampa em
    [EDGE_MULT_MIN, EDGE_MULT_MAX]; os caps duros de _compute_qty mandam depois."""
    if not EDGE_SIZING_ENABLED:
        return 1.0, "disabled"
    try:
        n = int(rec.get("edge_score") or 0)
    except Exception:
        n = 0
    tags = rec.get("edge_tags") or []
    if n <= 0:
        mult = max(EDGE_MULT_MIN, min(EDGE_MULT_MAX, EDGE_NOEDGE_MULT))
        return mult, f"sem edge→×{mult:.2f}"
    raw = 1.0 + n * EDGE_PER_EDGE
    if "A+" in tags:
        raw += EDGE_APLUS_BONUS
    mult = max(EDGE_MULT_MIN, min(EDGE_MULT_MAX, raw))
    return mult, f"edges={n}[{','.join(str(t) for t in tags)}]→×{mult:.2f}"


def _funding_ev_r(rec: dict, entry: float, stop: float) -> tuple[float, float, str]:
    """#1 — Funding-EV em R: quanto a posição PAGA(−)/COLETA(+) de funding ao
    longo de FUNDING_EV_HOLD_WINDOWS janelas de 8h, normalizado pelo risco (R).

    Mecânica Binance USDT-M: funding pago a cada 8h; funding>0 → LONG paga / SHORT
    coleta. Pagamento por janela = notional × funding_rate. Em R (qty cancela):
        ev_pct = funding_pct × janelas × side_sign     (% do notional)
        ev_r   = (ev_pct/100) × (entry / |entry−stop|)
    side_sign: long=−1 (paga quando funding>0), short=+1. Retorna (ev_r, ev_pct, reason)."""
    funding = _get_rec_feature(rec, "funding_pct", default=None)
    try:
        f_pct = float(funding) if funding is not None else None
    except Exception:
        f_pct = None
    if f_pct is None or not entry or not stop:
        return 0.0, 0.0, "sem funding/preço"
    risk_dist = abs(float(entry) - float(stop))
    if risk_dist <= 0 or float(entry) <= 0:
        return 0.0, 0.0, "risk_dist=0"
    side_sign = -1.0 if rec.get("direction") == "long" else 1.0
    ev_pct = f_pct * FUNDING_EV_HOLD_WINDOWS * side_sign           # % do notional
    ev_r = (ev_pct / 100.0) * (float(entry) / risk_dist)
    verb = "coleta" if ev_r >= 0 else "paga"
    return ev_r, ev_pct, (f"funding {f_pct:+.4f}%/8h × {FUNDING_EV_HOLD_WINDOWS:g}j "
                          f"→ {verb} {ev_r:+.3f}R")


def _norm_sym(s: str) -> str:
    """'BTC/USDT:USDT' ou 'BTCUSDT' → 'BTCUSDT' (pra casar DB × exchange)."""
    if not s:
        return ""
    return s.split(":")[0].replace("/", "").upper()


async def reconcile_open_positions() -> dict:
    """
    go-live #4 — Reconcilia posições reais na exchange × trades OPEN no DB.

    Roda no boot (e exposto via endpoint). NÃO muta nada — só detecta e loga
    drift, porque fechar/abrir automaticamente no startup é arriscado. Surfa:
      - db_orphans:  trade OPEN no DB sem posição viva na exchange (fechou por
                     fora / nunca abriu) → o trade manager seguiria gerenciando
                     algo que não existe.
      - untracked:   posição viva na exchange sem trade OPEN no DB → o bot NÃO
                     está gerenciando (sem SL/TP automático da nossa parte).

    Retorna {"ok", "shadow", "db_open", "exchange_open", "db_orphans",
             "untracked", "matched"}.
    """
    report = {
        "ok": True, "shadow": SHADOW_ENABLED,
        "db_open": 0, "exchange_open": 0,
        "matched": [], "db_orphans": [], "untracked": [],
    }
    if not DB_ENABLED:
        report["ok"] = False
        report["error"] = "DB desabilitado"
        return report
    if SHADOW_ENABLED:
        # Em shadow não há posição real na exchange pra reconciliar.
        log.info("[reconcile] shadow ON — sem posições reais pra reconciliar")
        return report
    try:
        from services import exchange_service
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade

        # DB: trades reais auto OPEN
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.status == "open",
                RealTrade.source == "auto",
            )
            db_rows = (await session.execute(stmt)).scalars().all()
        db_by_sym = {_norm_sym(t.symbol): t for t in db_rows}
        report["db_open"] = len(db_rows)

        # Exchange: posições vivas
        pos_res = await exchange_service.get_positions()
        if not pos_res.get("ok"):
            report["ok"] = False
            report["error"] = f"get_positions falhou: {pos_res.get('error') or pos_res.get('msg')}"
            log.warning(f"[reconcile] {report['error']}")
            return report
        positions = pos_res.get("positions") or []
        pos_by_sym = {_norm_sym(p.get("symbol")): p for p in positions}
        report["exchange_open"] = len(positions)

        db_syms = set(db_by_sym)
        ex_syms = set(pos_by_sym)

        for s in sorted(db_syms & ex_syms):
            report["matched"].append(s)
        for s in sorted(db_syms - ex_syms):
            t = db_by_sym[s]
            report["db_orphans"].append({"symbol": s, "trade_id": t.id, "side": t.side})
        for s in sorted(ex_syms - db_syms):
            p = pos_by_sym[s]
            report["untracked"].append({
                "symbol": s, "side": p.get("side"), "size": p.get("size"),
                "entry_price": p.get("entry_price"),
            })

        # Log resumido — alto e claro quando há drift.
        if report["db_orphans"] or report["untracked"]:
            log.warning(
                f"[reconcile] ⚠ DRIFT exchange↔DB: "
                f"{len(report['db_orphans'])} órfão(s) no DB, "
                f"{len(report['untracked'])} posição(ões) não-gerenciada(s). "
                f"db_orphans={report['db_orphans']} untracked={report['untracked']}"
            )
        else:
            log.info(
                f"[reconcile] ✓ sincronizado: {len(report['matched'])} posição(ões) "
                f"casada(s), sem drift"
            )
    except Exception as e:
        report["ok"] = False
        report["error"] = str(e)
        log.warning(f"[reconcile] falhou: {e}")
    return report


# ── Direction flip helpers ──────────────────────────────────────────────────


async def _find_opposite_open_trade(symbol: str, new_direction: str):
    """Procura RealTrade auto OPEN no símbolo, direção oposta. Retorna o objeto
    ou None. Usado pra detectar se há candidato a flip."""
    if not DB_ENABLED:
        return None
    try:
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        opposite_side = "long" if new_direction == "short" else "short"
        async with get_session() as session:
            stmt = select(RealTrade).where(
                RealTrade.symbol == symbol,
                RealTrade.status == "open",
                RealTrade.source == "auto",
                RealTrade.side == opposite_side,
            )
            return (await session.execute(stmt)).scalar_one_or_none()
    except Exception as e:
        log.warning(f"[flip] busca opposite falhou {symbol}: {e}")
        return None


async def _flip_cooldown_active(symbol: str) -> bool:
    """True se houve flip nesse símbolo há menos de FLIP_COOLDOWN_HOURS horas.
    Detecta via notes contendo 'closed_flip' nos closed_at recentes."""
    if not DB_ENABLED:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=FLIP_COOLDOWN_HOURS)
        async with get_session() as session:
            stmt = select(RealTrade.id).where(
                RealTrade.symbol == symbol,
                RealTrade.closed_at >= cutoff,
                RealTrade.status.like("closed_flip%"),
            ).limit(1)
            return (await session.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        log.warning(f"[flip] cooldown check falhou {symbol}: {e}")
        return False


async def _get_mark_price(symbol: str) -> float:
    """Mark price atual do símbolo via positionRisk. 0 se falhar."""
    try:
        from services import exchange_service
        res = await exchange_service.get_positions(symbol=symbol)
        if not res.get("ok"):
            return 0.0
        for p in res.get("positions") or []:
            return float(p.get("mark_price") or 0)
    except Exception as e:
        log.warning(f"[flip] mark_price falhou {symbol}: {e}")
    return 0.0


async def _get_current_tier_score(rec_id: int) -> tuple[str, float]:
    """Tier e score da rec original que abriu o trade. ('', 0) se não achou."""
    if not DB_ENABLED or not rec_id:
        return ("", 0.0)
    try:
        from sqlalchemy import select
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.tier, RecommendationSnapshot.score).where(
                RecommendationSnapshot.id == rec_id
            )
            row = (await session.execute(stmt)).first()
            if row:
                return (row.tier or "", float(row.score or 0))
    except Exception as e:
        log.warning(f"[flip] get_current_tier_score falhou: {e}")
    return ("", 0.0)


async def _evaluate_flip_gate(current_trade, new_rec: dict) -> tuple[bool, str]:
    """
    Avalia se rec na direção oposta justifica flip automático.
    Retorna (should_flip, reason).
    """
    if not FLIP_ENABLED:
        return (False, "FLIP_ENABLED=false")

    # 1. Fase: nunca flipa pós-TP1 (lock garantido seria destruído)
    phase = getattr(current_trade, "phase", None) or "pre_tp1"
    if phase != "pre_tp1":
        return (False, f"phase={phase} (pós-TP1 nunca flipa)")

    # 2. Cooldown
    if await _flip_cooldown_active(current_trade.symbol):
        return (False, f"cooldown ativo (último flip < {FLIP_COOLDOWN_HOURS}h)")

    # 3. Qualidade — tier upgrade OU score delta
    new_tier = new_rec.get("tier") or ""
    new_score = float(new_rec.get("score") or 0)
    cur_tier, cur_score = await _get_current_tier_score(current_trade.recommendation_id)
    tier_delta = _TIER_RANK.get(new_tier, 0) - _TIER_RANK.get(cur_tier, 0)
    score_delta = new_score - cur_score
    tier_ok = tier_delta >= FLIP_MIN_TIER_UPGRADE
    score_ok = score_delta >= FLIP_MIN_SCORE_DELTA
    if not (tier_ok or score_ok):
        return (False, (
            f"qualidade insuficiente: tier {cur_tier}→{new_tier} (Δ{tier_delta}, "
            f"precisa ≥{FLIP_MIN_TIER_UPGRADE}), score {cur_score:.0f}→{new_score:.0f} "
            f"(Δ{score_delta:+.0f}, precisa ≥{FLIP_MIN_SCORE_DELTA})"
        ))

    # 4. R atual — não flipa trade já ganhando bem
    mark = await _get_mark_price(current_trade.symbol)
    entry = float(current_trade.entry_price or 0)
    planned_stop = float(current_trade.planned_stop or 0)
    if mark > 0 and entry > 0 and planned_stop > 0:
        sign = 1 if current_trade.side == "long" else -1
        risk_dist = abs(entry - planned_stop)
        if risk_dist > 0:
            r_now = ((mark - entry) * sign) / risk_dist
            if r_now > FLIP_MAX_CURRENT_R:
                return (False, f"trade atual ganhando {r_now:+.2f}R > {FLIP_MAX_CURRENT_R}R (deixa fluir)")

    return (True, f"approved: tier {cur_tier}→{new_tier} (Δ{tier_delta}), score Δ{score_delta:+.0f}")


async def _execute_flip(current_trade) -> bool:
    """
    Fecha trade atual via market (reduceOnly), cancela ordens condicionais,
    marca como closed_flip no DB. Retorna True se conseguiu.
    """
    from services import exchange_service, real_trade_service
    symbol = current_trade.symbol
    try:
        # 1. Cancela algo orders pendentes (SL/TP1/TP2)
        for oid_field in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
            oid = getattr(current_trade, oid_field, None)
            if oid:
                try:
                    await exchange_service.cancel_algo_order(str(oid))
                except Exception as e:
                    log.warning(f"[flip] cancel {oid_field}={oid} falhou: {e}")

        # 2. Market close (reduceOnly)
        close_side = "Sell" if current_trade.side == "long" else "Buy"
        close_res = await exchange_service.place_order(
            symbol=symbol,
            side=close_side,
            qty=float(current_trade.qty),
            order_type="Market",
            reduce_only=True,
            client_order_id=f"cw-flip-{current_trade.id}",
        )
        if not close_res.get("ok"):
            log.error(f"[flip] market close falhou trade#{current_trade.id}: {close_res.get('msg') or close_res.get('error')}")
            return False

        # 3. Exit price aproximado via avgPrice
        result = close_res.get("result") or {}
        exit_price = float(result.get("avgPrice") or 0) or await _get_mark_price(symbol) or float(current_trade.entry_price or 0)

        # 4. Fecha no DB
        await real_trade_service.close_trade(
            trade_id=current_trade.id,
            exit_price=exit_price,
            status="closed_flip",
            notes=f"auto-flip: fechado pra reversão de direção",
        )
        log.info(f"[flip] EXECUTED close trade#{current_trade.id} {symbol} {current_trade.side} → flipping")
        return True
    except Exception as e:
        log.error(f"[flip] erro flipando trade#{current_trade.id}: {e}")
        return False


# ── TF upgrade helpers (Fase 3) ─────────────────────────────────────────────


async def _find_same_direction_open_trade(symbol: str, new_direction: str):
    """Procura RealTrade auto OPEN no símbolo, MESMA direção. Retorna o mais
    recente (por opened_at desc) ou None. Usado para detectar candidato a TF
    upgrade."""
    if not DB_ENABLED:
        return None
    try:
        from sqlalchemy import select, desc
        from db import get_session
        from models.real_trade import RealTrade
        same_side = "long" if new_direction == "long" else "short"
        async with get_session() as session:
            stmt = (
                select(RealTrade)
                .where(RealTrade.symbol == symbol)
                .where(RealTrade.status == "open")
                .where(RealTrade.source == "auto")
                .where(RealTrade.side == same_side)
                .order_by(desc(RealTrade.opened_at))
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()
    except Exception as e:
        log.warning(f"[tf-upgrade] busca same-direction falhou {symbol}: {e}")
        return None


async def _upgrade_cooldown_active(symbol: str) -> bool:
    """True se houve TF upgrade nesse símbolo há menos de TF_UPGRADE_COOLDOWN_HOURS.
    Detecta via notes contendo 'tf_upgrade' no trade aberto (atualizamos notes
    quando upgrade roda) — janela vale por trade vivo."""
    if not DB_ENABLED:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select
        from db import get_session
        from models.real_trade import RealTrade
        cutoff = datetime.now(timezone.utc) - timedelta(hours=TF_UPGRADE_COOLDOWN_HOURS)
        async with get_session() as session:
            stmt = select(RealTrade.id).where(
                RealTrade.symbol == symbol,
                RealTrade.updated_at >= cutoff,
                RealTrade.notes.like("%tf_upgrade%"),
            ).limit(1)
            return (await session.execute(stmt)).scalar_one_or_none() is not None
    except Exception as e:
        log.warning(f"[tf-upgrade] cooldown check falhou {symbol}: {e}")
        return False


async def _get_rec_timeframe(rec_id: int) -> str:
    """Lê timeframe da snapshot original. '' se não achou."""
    if not DB_ENABLED or not rec_id:
        return ""
    try:
        from sqlalchemy import select
        from db import get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        async with get_session() as session:
            stmt = select(RecommendationSnapshot.timeframe).where(
                RecommendationSnapshot.id == rec_id
            )
            row = (await session.execute(stmt)).first()
            if row:
                return row.timeframe or ""
    except Exception as e:
        log.warning(f"[tf-upgrade] get_rec_timeframe falhou: {e}")
    return ""


async def _evaluate_upgrade_gate(current_trade, new_rec: dict, mark_price: float) -> tuple[bool, str, dict]:
    """
    Avalia se rec na mesma direção, em TF maior, justifica ajuste de SL/TPs.
    Retorna (allow, reason, ctx). ctx contém phase, novos níveis, qty, tiers/scores.
    """
    ctx: dict = {}
    if not TF_UPGRADE_ENABLED:
        return (False, "TF_UPGRADE_ENABLED=false", ctx)

    # 1. TF estritamente maior
    new_tf = (new_rec.get("timeframe") or "").strip()
    cur_tf = await _get_rec_timeframe(current_trade.recommendation_id)
    cur_rank = _tf_rank_local(cur_tf)
    new_rank = _tf_rank_local(new_tf)
    if new_rank <= cur_rank:
        return (False, f"TF não maior: {cur_tf}(r{cur_rank}) → {new_tf}(r{new_rank})", ctx)

    # 2. Qualidade — tier upgrade OU score delta
    new_tier = new_rec.get("tier") or ""
    new_score = float(new_rec.get("score") or 0)
    cur_tier, cur_score = await _get_current_tier_score(current_trade.recommendation_id)
    tier_delta = _TIER_RANK.get(new_tier, 0) - _TIER_RANK.get(cur_tier, 0)
    score_delta = new_score - cur_score
    tier_ok = tier_delta >= TF_UPGRADE_MIN_TIER_UPGRADE
    score_ok = score_delta >= TF_UPGRADE_MIN_SCORE_DELTA
    if not (tier_ok or score_ok):
        return (False, (
            f"qualidade insuficiente: tier {cur_tier}→{new_tier} (Δ{tier_delta}, "
            f"precisa ≥{TF_UPGRADE_MIN_TIER_UPGRADE}), score {cur_score:.0f}→{new_score:.0f} "
            f"(Δ{score_delta:+.0f}, precisa ≥{TF_UPGRADE_MIN_SCORE_DELTA})"
        ), ctx)

    # 3. Cooldown
    if await _upgrade_cooldown_active(current_trade.symbol):
        return (False, f"cooldown ativo (último upgrade < {TF_UPGRADE_COOLDOWN_HOURS}h)", ctx)

    # 4. Fase do trade atual
    phase = getattr(current_trade, "phase", None) or "pre_tp1"

    # 5. Near-TP1 block (só pré-TP1 importa)
    entry = float(current_trade.entry_price or 0)
    planned_stop = float(current_trade.planned_stop or 0)
    planned_tp1 = float(current_trade.planned_tp1 or 0)
    sign = 1 if current_trade.side == "long" else -1
    if phase == "pre_tp1" and mark_price > 0 and entry > 0 and planned_stop > 0 and planned_tp1 > 0:
        risk_dist_old = abs(entry - planned_stop)
        if risk_dist_old > 0:
            r_now = ((mark_price - entry) * sign) / risk_dist_old
            tp1_r = ((planned_tp1 - entry) * sign) / risk_dist_old
            if r_now > (tp1_r - TF_UPGRADE_NEAR_TP1_R):
                return (False, (
                    f"near-TP1: r_now={r_now:+.2f} > tp1_R({tp1_r:.2f}) - "
                    f"{TF_UPGRADE_NEAR_TP1_R} (deixa TP1 disparar)"
                ), ctx)

    # 6. Geometria do SL novo — distância mark→stop deve ser >= BUFFER_PCT% do preço
    new_stop = float(new_rec.get("stop_loss") or 0)
    if mark_price > 0 and new_stop > 0:
        sl_dist_pct = abs(mark_price - new_stop) / mark_price * 100.0
        if sl_dist_pct < TF_UPGRADE_BUFFER_PCT:
            return (False, (
                f"SL novo muito colado: dist {sl_dist_pct:.2f}% < buffer "
                f"{TF_UPGRADE_BUFFER_PCT}% (mark={mark_price}, stop={new_stop})"
            ), ctx)

    # 7. Direção do novo SL deve ser coerente com o lado (long: stop<mark; short: stop>mark)
    if new_stop > 0 and mark_price > 0:
        if current_trade.side == "long" and new_stop >= mark_price:
            return (False, f"SL novo {new_stop} >= mark {mark_price} em long (inválido)", ctx)
        if current_trade.side == "short" and new_stop <= mark_price:
            return (False, f"SL novo {new_stop} <= mark {mark_price} em short (inválido)", ctx)

    # 8. Novos níveis
    sig = new_rec.get("signal") or {}
    new_tp1 = None
    if isinstance(sig, dict):
        try:
            new_tp1 = float(sig.get("tp1")) if sig.get("tp1") is not None else None
        except Exception:
            new_tp1 = None
    new_tp2 = float(new_rec.get("tp2") or 0) or None

    # 9. Qty: pré-TP1 pode recalcular respeitando cap 3% de risco
    new_qty = None
    if phase == "pre_tp1":
        try:
            equity_usd, _src = await _resolve_equity_usd()
            risk_dist_new = abs(entry - new_stop) if new_stop > 0 else 0
            if risk_dist_new > 0 and equity_usd > 0:
                # Mantém o risco atual da rec (cap em 3%)
                risk_pct_new = min(float(new_rec.get("risk_pct") or 1.0), 3.0)
                lev = int(current_trade.leverage or 1)
                sizing = _compute_qty(entry, new_stop, risk_pct_new, equity_usd, leverage=lev)
                if sizing is not None and sizing["status"] != "skip":
                    new_qty = float(sizing["qty"])
        except Exception as e:
            log.warning(f"[tf-upgrade] sizing recalc falhou {current_trade.symbol}: {e}")

    ctx.update({
        "phase": phase,
        "new_qty": new_qty,
        "new_stop": new_stop or None,
        "new_tp1": new_tp1,
        "new_tp2": new_tp2,
        "tier_old": cur_tier,
        "tier_new": new_tier,
        "score_old": cur_score,
        "score_new": new_score,
        "tf_old": cur_tf,
        "tf_new": new_tf,
    })
    return (True, (
        f"approved: TF {cur_tf}→{new_tf}, tier {cur_tier}→{new_tier} "
        f"(Δ{tier_delta}), score Δ{score_delta:+.0f}, phase={phase}"
    ), ctx)


async def _execute_tf_upgrade(current_trade, new_rec: dict, ctx: dict) -> bool:
    """
    Ajusta SL/TPs do trade aberto refletindo TF/níveis novos.
      - pre_tp1: cancela SL+TP1+TP2 → recoloca bracket completo (com qty nova)
      - post_tp1: cancela só TP2 → recoloca TP2 novo (SL fica no BE intocado)
    Atualiza DB: planned_*, qty, *_order_id, recommendation_id, notes.
    """
    from services import exchange_service, binance_signed_service
    from sqlalchemy import select, desc
    from datetime import datetime, timezone
    from db import get_session
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot

    symbol = current_trade.symbol
    phase = ctx.get("phase") or "pre_tp1"
    new_stop = ctx.get("new_stop")
    new_tp1 = ctx.get("new_tp1")
    new_tp2 = ctx.get("new_tp2")
    new_qty = ctx.get("new_qty")
    tf_old = ctx.get("tf_old") or ""
    tf_new = ctx.get("tf_new") or ""

    try:
        # 1. Resolve recommendation_id da nova rec (último snapshot do símbolo/dir/tf)
        new_rec_id = None
        if DB_ENABLED:
            try:
                async with get_session() as session:
                    stmt = (
                        select(RecommendationSnapshot.id)
                        .where(RecommendationSnapshot.symbol == symbol)
                        .where(RecommendationSnapshot.direction == new_rec.get("direction"))
                        .where(RecommendationSnapshot.timeframe == new_rec.get("timeframe"))
                        .order_by(desc(RecommendationSnapshot.created_at))
                        .limit(1)
                    )
                    new_rec_id = (await session.execute(stmt)).scalar_one_or_none()
            except Exception as e:
                log.warning(f"[tf-upgrade] resolve new_rec_id falhou {symbol}: {e}")

        # 2. Cancela ordens condicionais conforme a fase
        cancel_fields = ("sl_order_id", "tp1_order_id", "tp2_order_id") if phase == "pre_tp1" else ("tp2_order_id",)
        for oid_field in cancel_fields:
            oid = getattr(current_trade, oid_field, None)
            if oid:
                try:
                    res = await exchange_service.cancel_algo_order(str(oid))
                    if not res.get("ok"):
                        log.warning(
                            f"[tf-upgrade] cancel {oid_field}={oid} {symbol}: "
                            f"{res.get('msg') or res.get('error')}"
                        )
                except Exception as e:
                    log.warning(f"[tf-upgrade] cancel {oid_field}={oid} falhou: {e}")

        # 3. Recoloca ordens
        entry_side = "Buy" if current_trade.side == "long" else "Sell"
        qty_for_brackets = float(new_qty if (phase == "pre_tp1" and new_qty and new_qty > 0) else current_trade.qty)

        new_sl_oid = None
        new_tp1_oid = None
        new_tp2_oid = None

        if phase == "pre_tp1":
            prot = await binance_signed_service.place_protection_orders(
                symbol, entry_side, qty=qty_for_brackets,
                stop_loss=new_stop,
                tp1=new_tp1,
                tp2=new_tp2,
                client_order_id_prefix=f"cw-tfu-{current_trade.id}",
            )
            if not prot.get("sl_ok"):
                log.error(
                    f"[tf-upgrade] CRITICAL: novo SL falhou {symbol} #{current_trade.id}: "
                    f"{prot.get('sl_msg')} — trade pode estar SEM proteção"
                )
                return False
            new_sl_oid = prot.get("sl_order_id")
            new_tp1_oid = prot.get("tp1_order_id")
            new_tp2_oid = prot.get("tp2_order_id")
            if not prot.get("tp2_ok"):
                log.warning(f"[tf-upgrade] {symbol} TP2 novo falhou: {prot.get('tp2_msg')}")
            if prot.get("tp1_skipped"):
                log.warning(f"[tf-upgrade] {symbol} TP1 skip (qty parcial=0)")
            elif not prot.get("tp1_ok"):
                log.warning(f"[tf-upgrade] {symbol} TP1 novo falhou: {prot.get('tp1_msg')}")
        else:
            # post_tp1: só TP2 — SL@BE fica intocado, qty atual já é a remanescente
            prot = await binance_signed_service.place_protection_orders(
                symbol, entry_side, qty=float(current_trade.qty),
                stop_loss=None,
                tp1=None,
                tp2=new_tp2,
                client_order_id_prefix=f"cw-tfu-{current_trade.id}",
            )
            if not prot.get("tp2_ok"):
                log.error(
                    f"[tf-upgrade] TP2 novo falhou post-TP1 {symbol} #{current_trade.id}: "
                    f"{prot.get('tp2_msg')}"
                )
                return False
            new_tp2_oid = prot.get("tp2_order_id")

        # 4. Atualiza DB
        if DB_ENABLED:
            async with get_session() as session:
                fresh = (await session.execute(
                    select(RealTrade).where(RealTrade.id == current_trade.id)
                )).scalar_one_or_none()
                if fresh is None:
                    return False
                if phase == "pre_tp1":
                    if new_stop:
                        fresh.planned_stop = new_stop
                        fresh.sl_current_price = new_stop
                    if new_tp1 is not None:
                        fresh.planned_tp1 = new_tp1
                    if new_tp2 is not None:
                        fresh.planned_tp2 = new_tp2
                    if new_qty and new_qty > 0:
                        fresh.qty = qty_for_brackets
                        fresh.qty_initial = qty_for_brackets
                    if new_sl_oid:
                        fresh.sl_order_id = new_sl_oid
                    if new_tp1_oid:
                        fresh.tp1_order_id = new_tp1_oid
                    if new_tp2_oid:
                        fresh.tp2_order_id = new_tp2_oid
                else:
                    if new_tp2 is not None:
                        fresh.planned_tp2 = new_tp2
                    if new_tp2_oid:
                        fresh.tp2_order_id = new_tp2_oid
                if new_rec_id:
                    fresh.recommendation_id = new_rec_id
                tag = f"tf_upgrade {phase} {tf_old}->{tf_new}"
                fresh.notes = (fresh.notes + " | " + tag) if fresh.notes else tag
                fresh.updated_at = datetime.now(timezone.utc)
                await session.commit()

        log.info(
            f"[tf-upgrade] {symbol} #{current_trade.id} {phase} {tf_old}->{tf_new} "
            f"score {ctx.get('score_old', 0):.0f}->{ctx.get('score_new', 0):.0f}"
        )
        return True
    except Exception as e:
        log.error(f"[tf-upgrade] erro upgrade trade#{current_trade.id} {symbol}: {e}", exc_info=True)
        return False


def _compute_qty(
    entry: float, stop: float, risk_pct: float, equity_usd: float,
    leverage: int = 1,
) -> Optional[dict]:
    """
    Dimensiona a posição com guard de notional mínimo + cap de risco máximo.

    Fluxo:
      1. qty_nominal = (equity × risk_pct/100) / |entry−stop|
      2. notional_nominal = qty_nominal × entry
      3. Se notional_nominal >= MIN_NOTIONAL_USD → usa nominal (status="ok")
      4. Senão, qty_inflated = MIN_NOTIONAL_USD / entry
         - Calcula risco real = qty_inflated × |entry−stop| / equity × 100
         - Se risco_real <= MAX_RISK_PCT_HARD → usa inflated (status="inflated")
         - Senão → status="skip" (rec descartada)

    Retorna dict com {qty, status, notional, risk_pct_real, reason} ou None
    se rec é inválida (risk_dist=0).
    """
    risk_dist = abs(entry - stop)
    if risk_dist <= 0:
        return None

    risk_usd_target = equity_usd * (risk_pct / 100.0)
    qty_nominal = risk_usd_target / risk_dist
    notional_nominal = qty_nominal * entry

    # Cap de margem por trade — se notional/lev > max_margin% × equity, reduz qty.
    # Isso protege quando SL é apertado (risk_dist pequeno → qty explode).
    lev = max(int(leverage or 1), 1)
    max_margin_usd = equity_usd * (MAX_MARGIN_PCT_PER_TRADE / 100.0)
    max_notional_by_margin = max_margin_usd * lev
    capped_reason = None
    if notional_nominal > max_notional_by_margin:
        qty_capped = max_notional_by_margin / entry
        risk_capped_usd = qty_capped * risk_dist
        risk_pct_capped = (risk_capped_usd / equity_usd) * 100.0
        capped_reason = (
            f"margin cap: notional ${notional_nominal:.0f} → ${max_notional_by_margin:.0f} "
            f"(margem {MAX_MARGIN_PCT_PER_TRADE}% × lev {lev}); "
            f"risco real {risk_pct:.2f}% → {risk_pct_capped:.2f}%"
        )
        qty_nominal = qty_capped
        notional_nominal = qty_capped * entry
        risk_pct = risk_pct_capped  # reflete risco real reduzido

    if notional_nominal >= MIN_NOTIONAL_USD:
        return {
            "qty": round(qty_nominal, 6),
            "status": "capped" if capped_reason else "ok",
            "notional_usd": round(notional_nominal, 2),
            "risk_pct_real": round(risk_pct, 3),
            "reason": capped_reason or "nominal sizing",
        }

    # Inflar pro mínimo
    qty_inflated = MIN_NOTIONAL_USD / entry
    risk_inflated_usd = qty_inflated * risk_dist
    risk_pct_inflated = (risk_inflated_usd / equity_usd) * 100.0

    if risk_pct_inflated <= MAX_RISK_PCT_HARD:
        return {
            "qty": round(qty_inflated, 6),
            "status": "inflated",
            "notional_usd": round(qty_inflated * entry, 2),
            "risk_pct_real": round(risk_pct_inflated, 3),
            "reason": f"inflated to min notional ${MIN_NOTIONAL_USD:.0f}; risk {risk_pct:.2f}% → {risk_pct_inflated:.2f}%",
        }

    return {
        "qty": round(qty_inflated, 6),
        "status": "skip",
        "notional_usd": round(qty_inflated * entry, 2),
        "risk_pct_real": round(risk_pct_inflated, 3),
        "reason": f"would inflate risk to {risk_pct_inflated:.2f}% > cap {MAX_RISK_PCT_HARD:.2f}%",
    }


def _exec_size_damp(rec: dict, notional_usd: float) -> tuple[float, str]:
    """Multiplicador DEFENSIVO de tamanho (≤1.0) por vol (atr_pct) + participação
    no volume 24h. Retorna (mult, reason). Fail-soft: dado ausente = sem damp.
    NÃO bloqueia — só reduz. Flag OFF = (1.0, 'off')."""
    if not EXEC_SIZE_DAMP_ENABLED:
        return 1.0, "off"

    # componente ATR (a com suporte empírico)
    m_atr, tag_atr = 1.0, ""
    atr = _get_rec_feature(rec, "atr_pct")
    try:
        a = float(atr) if atr is not None else None
    except Exception:
        a = None
    if a is not None and ATR_DAMP_HI > ATR_DAMP_LO and a > ATR_DAMP_LO:
        frac = min(1.0, (a - ATR_DAMP_LO) / (ATR_DAMP_HI - ATR_DAMP_LO))
        m_atr = 1.0 - frac * (1.0 - ATR_DAMP_MULT_MIN)
        tag_atr = f"atr={a:.2f}%→×{m_atr:.2f}"

    # componente participação (notional / volume 24h) — NO-OP nos tamanhos atuais
    m_liq, tag_liq = 1.0, ""
    try:
        qvol = float(rec.get("quote_vol_usd")) if rec.get("quote_vol_usd") is not None else None
    except Exception:
        qvol = None
    if (qvol and qvol > 0 and notional_usd and notional_usd > 0
            and LIQ_DAMP_PART_HI > LIQ_DAMP_PART_LO):
        part = notional_usd / qvol
        if part > LIQ_DAMP_PART_LO:
            frac = min(1.0, (part - LIQ_DAMP_PART_LO) / (LIQ_DAMP_PART_HI - LIQ_DAMP_PART_LO))
            m_liq = 1.0 - frac * (1.0 - LIQ_DAMP_MULT_MIN)
            tag_liq = f"part={part*100:.2f}%→×{m_liq:.2f}"

    mult = min(m_atr, m_liq)
    if mult >= 1.0:
        return 1.0, "sem damp"
    tag = " ".join(t for t in (tag_atr, tag_liq) if t)
    return round(mult, 4), f"{tag} ⇒ ×{mult:.2f}"


def _liq_tier_mult(rec: dict) -> tuple[float, str]:
    """Multiplicador de tamanho por FAIXA de volume 24h da moeda (≤1.0).
    Mão menor em moedas magras (liberadas pelo piso de rotação 350). Retorna
    (mult, reason). Fail-soft: vol ausente = mão cheia. Flag OFF = (1.0, 'off')."""
    if not LIQ_TIER_SIZING_ENABLED:
        return 1.0, "off"
    try:
        qvol = float(rec.get("quote_vol_usd")) if rec.get("quote_vol_usd") is not None else None
    except Exception:
        qvol = None
    if qvol is None or qvol <= 0:
        return 1.0, "vol n/d → mão cheia"
    if qvol >= LIQ_TIER_VOL_HI:
        return 1.0, f"vol ${qvol/1e6:.0f}M ≥ ${LIQ_TIER_VOL_HI/1e6:.0f}M → cheia"
    if qvol >= LIQ_TIER_VOL_MID:
        m = LIQ_TIER_MULT_MID
    elif qvol >= LIQ_TIER_VOL_LO:
        m = LIQ_TIER_MULT_LO
    else:
        m = LIQ_TIER_MULT_MIN
    return round(m, 4), f"vol ${qvol/1e6:.1f}M → ×{m:.2f}"


def _regime_size_mult(rec: dict, regime: dict | None) -> tuple[float, str]:
    """#6 — Multiplicador de tamanho por REGIME macro (≤1.0). Mão menor em
    LONG de alt quando o regime está adverso (downgrade_alt_longs: BTC_DOMINANT
    ou ALT_RISK_OFF). Recebe o regime já buscado (cache 10min, 1 fetch/lote).
    Retorna (mult, reason). Fail-soft: regime n/d ou flag OFF = (1.0, ...).
    Não toca BTC/ETH nem shorts — só o caso que historicamente sangra."""
    if not REGIME_SIZING_ENABLED:
        return 1.0, "off"
    if not regime or not isinstance(regime, dict):
        return 1.0, "regime n/d → mão cheia"
    if not regime.get("downgrade_alt_longs"):
        return 1.0, f"regime {regime.get('regime', '?')} → cheia"
    direction = (rec.get("direction") or "").strip().lower()
    symbol = rec.get("symbol") or ""
    try:
        from services.regime_service import is_btc_symbol
        is_btc = is_btc_symbol(symbol)
    except Exception:
        is_btc = False
    if direction == "long" and not is_btc:
        return round(REGIME_SIZE_MULT_ALT_LONG, 4), (
            f"regime {regime.get('regime', '?')} → long de alt ×{REGIME_SIZE_MULT_ALT_LONG:.2f}"
        )
    return 1.0, f"regime {regime.get('regime', '?')} → cheia (não-alt-long)"


def _sizing_stack_report(rec: dict, regime: dict | None, notional_usd: float) -> tuple[float, str]:
    """#2 — Relatório COMPOSTO do stack de sizing inteligente (auditoria/validação).
    Chama cada multiplicador (que já retorna 1.0/'off' quando desligado) e devolve
    o net multiplicativo + um breakdown legível. PURO e sem efeito colateral: não
    altera nenhuma decisão — só consolida num ponto o que hoje é logado disperso,
    tornando a composição conviction×edge×damp×liq×regime auditável e validável.

    Nota: conviction/edge escalam risk_pct ANTES do _compute_qty (caps duros
    entram no meio); damp/liq/regime escalam a qty DEPOIS. O net aqui é um resumo
    do stack de multiplicadores ATIVOS, não o fator exato pós-caps — serve pra ver
    quais camadas agiram e com que intensidade, não pra recomputar a qty."""
    net = 1.0
    parts: list[str] = []
    try:
        for name, (m, _why) in (
            ("conviction", _conviction_mult(rec)),
            ("edge", _edge_mult(rec)),
            ("damp", _exec_size_damp(rec, notional_usd)),
            ("liq", _liq_tier_mult(rec)),
            ("regime", _regime_size_mult(rec, regime)),
        ):
            try:
                mf = float(m)
            except Exception:
                mf = 1.0
            if abs(mf - 1.0) > 1e-9:
                net *= mf
                parts.append(f"{name}×{mf:.2f}")
    except Exception as e:
        return 1.0, f"erro no relatório: {e}"
    return round(net, 4), (" · ".join(parts) if parts else "todos ×1.00")


async def _breakout_lane_qualifies(rec: dict) -> tuple[bool, str]:
    """#3 — Este setup é um BREAKOUT de tendência forte A FAVOR do bias macro?
    Se sim, o proximity gate pode usar o teto afrouxado (BREAKOUT_LANE_MAX_ATR).
    FAIL-CLOSED: qualquer sinal ausente/fraco → (False, motivo). Não decide sozinho
    a entrada — só LIBERA o teto; os demais gates (struct-chase, RR, ATR) seguem.
    Retorna (qualifica, motivo)."""
    if not BREAKOUT_LANE_ENABLED:
        return False, "off"
    try:
        direction = (rec.get("direction") or "").strip().lower()
        if direction not in ("long", "short"):
            return False, "direção n/d"
        if rec.get("tier") not in ("A+", "A"):
            return False, f"tier {rec.get('tier')} (só A/A+)"
        # Força de tendência: ADX >= piso. Ausente → fail-closed.
        adx = _get_rec_feature(rec, "adx")
        try:
            adx_f = float(adx) if adx is not None else None
        except Exception:
            adx_f = None
        if adx_f is None or adx_f < BREAKOUT_LANE_MIN_ADX:
            return False, f"adx {adx_f if adx_f is not None else 'n/d'} < {BREAKOUT_LANE_MIN_ADX}"
        # Bias macro a favor da direção (repique/tendência alinhada).
        from services.regime_service import get_market_bias, direction_favored
        bias = await get_market_bias()
        if not direction_favored(direction, bias):
            return False, f"bias {bias.get('bias', '?')} não favorece {direction}"
        return True, f"breakout {direction} adx={adx_f:.0f} bias={bias.get('bias', '?')}"
    except Exception as e:
        log.warning(f"[breakout-lane] {rec.get('symbol')} qualificação falhou (fail-closed): {e}")
        return False, "erro"


async def open_shadow_for_recs(recs: list[dict]) -> int:
    """
    Pra cada rec marcada com `_just_saved=True` e tier A/A+, abre uma RealTrade.

    Modos:
      SHADOW_ENABLED=True  → source="shadow" (sem chamar exchange)
      SHADOW_ENABLED=False → source="auto" + chama exchange_service.place_order()
                              (passa pelo kill_switch_service.check_can_trade primeiro)

    Idempotente: snapshot_service.save_recommendations dedupa antes.
    """
    if not DB_ENABLED or not recs:
        return 0
    mode = "shadow" if SHADOW_ENABLED else "live"
    log.debug(f"[shadow] processando {len(recs)} recs em modo={mode}")

    # #6 — busca o regime UMA vez por lote (cache 10min no regime_service).
    # Só quando o sizing por regime está ligado e em LIVE. Fail-soft.
    _regime_cached: dict = {}
    if not SHADOW_ENABLED and REGIME_SIZING_ENABLED:
        try:
            from services.regime_service import get_regime_status
            _regime_cached = await get_regime_status()
        except Exception as e:
            log.warning(f"[regime-size] fetch regime falhou (fail-open mão cheia): {e}")

    # ── Filler FORA da allowlist: snapshot dos slots ocupados (auto abertos)
    # separando DENTRO/FORA + prioriza recs DENTRO antes de FORA no ciclo
    # (sorted estável). Tudo NO-OP quando FILLER_FORA_ENABLED=OFF.
    _flr_n_dentro = 0
    _flr_n_fora = 0
    _flr_stop_streak = 0
    _flr_daily_pnl = 0.0
    _flr_test_done = 0
    if not SHADOW_ENABLED and FILLER_FORA_ENABLED:
        try:
            _flr_n_dentro, _flr_n_fora = await _open_auto_counts_by_allowlist()
        except Exception as e:
            log.warning(f"[filler-fora] contagem inicial falhou (fail-safe bloqueia filler): {e}")
            _flr_n_dentro, _flr_n_fora = FILLER_TOTAL_SLOTS, FILLER_FORA_MAX
        try:
            _flr_stop_streak, _flr_daily_pnl = await _filler_fora_brake_state()
        except Exception as e:
            log.warning(f"[filler-fora] brake-state falhou (fail-safe PAUSA filler): {e}")
            _flr_stop_streak, _flr_daily_pnl = FILLER_FORA_STOP_STREAK, 0.0
        if FILLER_FORA_TEST_N > 0:
            try:
                _flr_test_done = await _filler_fora_test_opened_count()
            except Exception as e:
                log.warning(f"[filler-fora] contagem do teste falhou (fail-safe usa size de teste): {e}")
                _flr_test_done = 0
        _allow_sort = get_exec_allowlist()
        if _allow_sort:
            recs = sorted(
                recs,
                key=lambda r: 0 if (_b := _symbol_base(r.get("symbol", ""))) and _b in _allow_sort else 1,
            )

    opened = 0
    for rec in recs:
        try:
            if not rec.get("_just_saved"):
                continue
            tier = rec.get("tier")
            if tier not in ("A+", "A"):
                continue

            # ── Proximity gate / anti-chase: não persegue preço esticado.
            # chase_atr signed a favor da direção (vem da rec). >= teto = "perdeu
            # o trem" → skip pra não abrir com fill ruim atrás de movimento.
            if PROXIMITY_GATE_ENABLED:
                _chase = rec.get("chase_atr")
                try:
                    _chase_f = float(_chase) if _chase is not None else None
                except Exception:
                    _chase_f = None
                # #3 Lane de breakout: em tendência forte a favor, afrouxa o teto
                # (fail-closed — só sobe o teto se qualificar). O struct-chase gate
                # abaixo segue como trava externa contra blowoff estrutural.
                _prox_ceiling = PROXIMITY_MAX_ATR
                if (BREAKOUT_LANE_ENABLED and _chase_f is not None
                        and _chase_f >= PROXIMITY_MAX_ATR):
                    _bl_ok, _bl_reason = await _breakout_lane_qualifies(rec)
                    if _bl_ok:
                        _prox_ceiling = BREAKOUT_LANE_MAX_ATR
                        log.info(
                            f"[breakout-lane] {rec.get('symbol')} {_bl_reason} → teto "
                            f"proximity {PROXIMITY_MAX_ATR}→{BREAKOUT_LANE_MAX_ATR}×ATR"
                        )
                if _chase_f is not None and _chase_f >= _prox_ceiling:
                    reason = (
                        f"preço {_chase_f:.2f}×ATR a favor (>= {_prox_ceiling}) — perdeu o trem"
                    )
                    log.info(f"[proximity-gate] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "proximity", reason)
                    continue

            # ── Anti-chase ESTRUTURAL: perna esticada desde a base → skip.
            # Isenta retest re-arm (entrada limpa no pullback, preço já voltou).
            if STRUCT_CHASE_GATE_ENABLED and not rec.get("retest_armed"):
                _sc = rec.get("struct_chase_atr")
                try:
                    _sc_f = float(_sc) if _sc is not None else None
                except Exception:
                    _sc_f = None
                if _sc_f is not None and _sc_f >= STRUCT_CHASE_MAX_ATR:
                    reason = (
                        f"perna {_sc_f:.2f}×ATR desde a base (>= {STRUCT_CHASE_MAX_ATR}) — esticado, risco de topo"
                    )
                    log.info(f"[struct-chase-gate] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "struct_chase", reason)
                    continue

            # ── ATR gate (Fase B Lite): atr_pct > 3 → -10.6pp lift, skip.
            if ATR_GATE_ENABLED:
                _atr_pct = _get_rec_feature(rec, "atr_pct")
                if _atr_pct is not None:
                    try:
                        if float(_atr_pct) > ATR_BLOCK_THRESHOLD:
                            reason = f"atr_pct={float(_atr_pct):.2f} > {ATR_BLOCK_THRESHOLD} (vol alta)"
                            log.info(f"[atr-gate] {rec.get('symbol')} {reason} — skip")
                            _record_skip(rec, "atr-gate", reason)
                            continue
                    except Exception:
                        pass

            # ── R:R gate (geometria estrutural): stop longe + alvo perto =
            # expectância ruim. Exige R:R mínimo a TP1 e TP2 medido sobre o
            # plano (entry/stop/TP do entry_planner). Mantém o stop/TP intactos;
            # só RECUSA setups com geometria fraca — "estrutural ou nada".
            if RR_GATE_ENABLED:
                try:
                    _entry = float(rec.get("entry") or 0)
                    _stop = float(rec.get("stop_loss") or 0)
                    _sig = rec.get("signal") or {}
                    _tp1 = _sig.get("tp1") if isinstance(_sig, dict) else None
                    _tp2 = rec.get("tp2")
                    _risk = abs(_entry - _stop)
                    if _entry > 0 and _risk > 0:
                        _rr1 = abs(float(_tp1) - _entry) / _risk if _tp1 else None
                        _rr2 = abs(float(_tp2) - _entry) / _risk if _tp2 else None
                        if _rr1 is not None and MIN_RR_TP1_EXEC > 0 and _rr1 < MIN_RR_TP1_EXEC:
                            reason = f"R:R TP1 {_rr1:.2f} < mín {MIN_RR_TP1_EXEC}"
                            log.info(f"[rr-gate] {rec.get('symbol')} {reason} — skip")
                            _record_skip(rec, "rr-gate", reason)
                            continue
                        if _rr2 is not None and MIN_RR_TP2_EXEC > 0 and _rr2 < MIN_RR_TP2_EXEC:
                            reason = f"R:R TP2 {_rr2:.2f} < mín {MIN_RR_TP2_EXEC}"
                            log.info(f"[rr-gate] {rec.get('symbol')} {reason} — skip")
                            _record_skip(rec, "rr-gate", reason)
                            continue
                except Exception as e:
                    log.warning(f"[rr-gate] {rec.get('symbol')} check falhou: {e}")

            # ── P(TP1) gate (calibração): pula baixa probabilidade calibrada de
            # bater o TP1. No-op-safe quando prob_tp1=None (calib imatura).
            if PROB_TP1_GATE_ENABLED and MIN_PROB_TP1_EXEC > 0:
                _p = rec.get("prob_tp1")
                try:
                    _p = float(_p) if _p is not None else None
                except Exception:
                    _p = None
                if _p is not None and _p < MIN_PROB_TP1_EXEC:
                    reason = f"P(TP1) {_p*100:.0f}% < mín {MIN_PROB_TP1_EXEC*100:.0f}%"
                    log.info(f"[prob-gate] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "prob-gate", reason)
                    continue

            # ── Score threshold (postmortem): piso configurável + adjusters.
            try:
                rec_score = float(rec.get("score") or 0)
            except Exception:
                rec_score = 0.0

            # Score adjusters (Fase B Lite): aplica delta calibrado.
            if SCORE_ADJUSTERS_ENABLED:
                from datetime import datetime as _dt, timezone as _tz
                _delta, _reasons = _compute_score_adjustment(rec, _dt.now(_tz.utc))
                if _delta != 0:
                    log.info(
                        f"[score-adj] {rec.get('symbol')} base={rec_score:.1f} "
                        f"delta={_delta:+.1f} → {rec_score + _delta:.1f} "
                        f"[{', '.join(_reasons)}]"
                    )
                    rec_score += _delta

            if rec_score < SCORE_MIN:
                reason = f"score {rec_score:.0f} < mínimo {SCORE_MIN:.0f}"
                log.info(f"[score-min] {rec.get('symbol')} {reason} — skip")
                _record_skip(rec, "score-min", reason)
                continue

            # ── #2 Gate de qualidade combinado: na banda marginal logo acima do
            # SCORE_MIN, exige >= QUALITY_EDGE_MIN edges (A+/funding/padrão/MTF).
            # Score já é >= SCORE_MIN aqui; só morde quem está colado no piso SEM
            # nenhum edge. Default OFF.
            if QUALITY_EDGE_GATE_ENABLED and QUALITY_EDGE_MARGIN > 0:
                if rec_score < (SCORE_MIN + QUALITY_EDGE_MARGIN):
                    try:
                        _edges_n = int(rec.get("edge_score") or 0)
                    except Exception:
                        _edges_n = 0
                    if _edges_n < QUALITY_EDGE_MIN:
                        reason = (
                            f"score marginal {rec_score:.0f} (< {SCORE_MIN + QUALITY_EDGE_MARGIN:.0f}) "
                            f"sem edge (>= {QUALITY_EDGE_MIN} exigido)"
                        )
                        log.info(f"[quality-edge-gate] {rec.get('symbol')} {reason} — skip")
                        _record_skip(rec, "quality-edge-gate", reason)
                        continue

            # ── Universo de execução (allowlist): só opera real bases permitidas.
            # Vazio = sem restrição (comportamento atual). Aplicado só em live.
            # FILLER FORA: quando ligado, libera bases FORA como filler de slot
            # ocioso (tier A/A+ — tier B já cortado acima), teto min(FILLER_FORA_MAX,
            # slots livres = total − DENTRO) e desliga quando a allowlist atinge
            # FILLER_FORA_OFF_AT. Marca rec["_is_filler"] pra size reduzido depois.
            _exec_allow = get_exec_allowlist()
            if not SHADOW_ENABLED and _exec_allow:
                base = _symbol_base(rec["symbol"])
                if base and base not in _exec_allow:
                    _flr_ok = False
                    _flr_cap = 0
                    if FILLER_FORA_ENABLED and len(_exec_allow) < FILLER_FORA_OFF_AT:
                        _flr_cap = min(FILLER_FORA_MAX, FILLER_TOTAL_SLOTS - _flr_n_dentro)
                        # Circuit-breaker: N stops do FORA → pausa entradas FORA.
                        # Exceção: ainda no lucro do dia e sem FORA aberto → 1 probe;
                        # TP1+TP2 (closed_tp2) zera o streak no próximo ciclo (despausa).
                        if _flr_stop_streak >= FILLER_FORA_STOP_STREAK:
                            if _flr_daily_pnl > 0 and _flr_n_fora == 0:
                                _flr_cap = min(_flr_cap, 1)  # 1 probe enquanto no lucro
                                log.info(
                                    f"[filler-fora] PAUSADO ({_flr_stop_streak} stops) — "
                                    f"libera 1 probe (lucro dia ${_flr_daily_pnl:.2f})"
                                )
                            else:
                                _flr_cap = 0  # pausa total
                                log.info(
                                    f"[filler-fora] PAUSADO ({_flr_stop_streak} stops, "
                                    f"pnl dia ${_flr_daily_pnl:.2f}, fora_open={_flr_n_fora}) — bloqueia FORA"
                                )
                        if _flr_n_fora < _flr_cap:
                            _flr_ok = True
                    if _flr_ok:
                        rec["_is_filler"] = True
                        log.info(
                            f"[filler-fora] {rec['symbol']} {base} FORA liberado como filler "
                            f"(fora_open={_flr_n_fora} cap={_flr_cap} dentro={_flr_n_dentro})"
                        )
                    else:
                        reason = f"{base} fora do universo de execução (allowlist)"
                        log.info(f"[exec-universe] {rec['symbol']} {reason} — skip")
                        _record_skip(rec, "exec-universe", reason)
                        continue

            # ── Symbol blacklist (postmortem): pula símbolos banidos.
            if not SHADOW_ENABLED:
                base = _symbol_base(rec["symbol"])
                if base and base in SYMBOL_BLACKLIST:
                    reason = f"símbolo {base} na blacklist"
                    log.info(f"[blacklist] {rec['symbol']} skip")
                    _record_skip(rec, "blacklist", reason)
                    continue

            # ── Liquidity gate (Fase 2): volume 24h em USD + spread bid/ask no
            # momento da execução. Protege o fill em moedas que secaram ou com
            # spread largo (slippage real). Fail-soft: erro de dado não bloqueia.
            if LIQUIDITY_GATE_ENABLED and (MIN_QUOTE_VOL_24H_USD > 0 or MAX_SPREAD_PCT > 0):
                try:
                    from services.binance_service import fetch_ticker as _fetch_ticker
                    _t = await _fetch_ticker(rec["symbol"])
                    _last = float(_t.get("last") or 0)
                    _vol_base = float(_t.get("volume") or 0)
                    _usd_vol = _vol_base * _last
                    if MIN_QUOTE_VOL_24H_USD > 0 and _usd_vol > 0 and _usd_vol < MIN_QUOTE_VOL_24H_USD:
                        reason = f"vol 24h ${_usd_vol/1e6:.1f}M < mín ${MIN_QUOTE_VOL_24H_USD/1e6:.1f}M"
                        log.info(f"[liquidity-gate] {rec['symbol']} {reason} — skip")
                        _record_skip(rec, "liquidity-gate", reason)
                        continue
                    _bid = float(_t.get("bid") or 0)
                    _ask = float(_t.get("ask") or 0)
                    if MAX_SPREAD_PCT > 0 and _bid > 0 and _ask > 0:
                        _mid = (_bid + _ask) / 2
                        _spread_pct = (_ask - _bid) / _mid * 100 if _mid > 0 else 0
                        if _spread_pct > MAX_SPREAD_PCT:
                            reason = f"spread {_spread_pct:.3f}% > máx {MAX_SPREAD_PCT}%"
                            log.info(f"[liquidity-gate] {rec['symbol']} {reason} — skip")
                            _record_skip(rec, "liquidity-gate", reason)
                            continue
                except Exception as e:
                    log.warning(f"[liquidity-gate] {rec.get('symbol')} check falhou (fail-soft): {e}")

            # ── Time-of-day block (postmortem -21% lift EU / -12% lift quinta).
            if not SHADOW_ENABLED:
                now_utc = datetime.now(timezone.utc)
                blocked, reason = _is_blocked_time(now_utc)
                if blocked:
                    log.info(f"[time-block] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "time-block", reason)
                    continue

            # ── Funding directional filter (postmortem: funding 0-0.05% = 75% wr).
            # Bloqueia trade contra sentiment já super-extremo na mesma direção.
            if not SHADOW_ENABLED and FUNDING_GATE_ENABLED:
                funding = _get_rec_feature(rec, "funding_pct", default=None)
                try:
                    funding_val = float(funding) if funding is not None else None
                except Exception:
                    funding_val = None
                if funding_val is not None:
                    direction = rec.get("direction")
                    if direction == "long" and funding_val > FUNDING_BLOCK_THRESHOLD:
                        reason = f"funding {funding_val:.4f}% > {FUNDING_BLOCK_THRESHOLD}% (long contra sentiment)"
                        log.info(f"[funding-gate] {rec.get('symbol')} {reason} — skip")
                        _record_skip(rec, "funding-gate", reason)
                        continue
                    if direction == "short" and funding_val < -FUNDING_BLOCK_THRESHOLD:
                        reason = f"funding {funding_val:.4f}% < -{FUNDING_BLOCK_THRESHOLD}% (short contra sentiment)"
                        log.info(f"[funding-gate] {rec.get('symbol')} {reason} — skip")
                        _record_skip(rec, "funding-gate", reason)
                        continue

            # ── MTF aligned gate (postmortem +18.68 lift / 82% wr quando alinhado).
            if not SHADOW_ENABLED and MTF_ALIGNED_MODE != "off":
                mtf_aligned_raw = _get_rec_feature(rec, "mtf_aligned", default=None)
                try:
                    aligned_count = int(mtf_aligned_raw) if mtf_aligned_raw is not None else None
                except Exception:
                    aligned_count = None
                is_aligned = aligned_count is not None and aligned_count >= MTF_ALIGNED_MIN_COUNT
                if MTF_ALIGNED_MODE == "required":
                    if not is_aligned:
                        reason = f"MTF não alinhado ({aligned_count}/{MTF_ALIGNED_MIN_COUNT} TFs)"
                        log.info(f"[mtf-gate] {rec.get('symbol')} {reason} mode=required — skip")
                        _record_skip(rec, "mtf-gate", reason)
                        continue
                elif MTF_ALIGNED_MODE == "boost":
                    if is_aligned:
                        log.info(
                            f"[mtf-gate] {rec.get('symbol')} aligned_count={aligned_count} "
                            f"— preferred (boost mode, sem bloqueio)"
                        )

            # ── Entry throttle (postmortem): cooldown global + max/hora.
            if not SHADOW_ENABLED:
                age = await _last_entry_age_seconds()
                last_hour = await _count_entries_last_hour()
                if age < ENTRY_COOLDOWN_SECONDS or last_hour >= ENTRY_MAX_PER_HOUR:
                    reason = (
                        f"throttle: cooldown {age:.0f}s/{ENTRY_COOLDOWN_SECONDS}s, "
                        f"última hora {last_hour}/{ENTRY_MAX_PER_HOUR}"
                    )
                    log.info(f"[entry-throttle] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "entry-throttle", reason)
                    continue

            # ── Global directional cap (postmortem): max longs/shorts.
            if not SHADOW_ENABLED:
                dir_count = await _count_open_by_direction(rec["direction"])
                if dir_count >= MAX_OPEN_PER_DIRECTION:
                    reason = f"{rec['direction']} cheio: {dir_count}/{MAX_OPEN_PER_DIRECTION} abertos"
                    log.info(f"[direction-cap] {rec.get('symbol')} {reason} — skip")
                    _record_skip(rec, "direction-cap", reason)
                    continue

            # ── Cluster correlation cap (postmortem): bloqueia se já há
            # CLUSTER_MAX_OPEN trades abertos num cluster correlacionado.
            if not SHADOW_ENABLED:
                cluster = _get_symbol_cluster(rec["symbol"])
                if cluster != "other":
                    open_in_cluster = await _count_open_in_cluster(cluster)
                    if open_in_cluster >= CLUSTER_MAX_OPEN:
                        reason = f"cluster {cluster} cheio: {open_in_cluster}/{CLUSTER_MAX_OPEN}"
                        log.info(f"[cluster-cap] {rec['symbol']} {reason} — skip")
                        _record_skip(rec, "cluster-cap", reason)
                        continue
                    # Cap por direção dentro do cluster (postmortem 04/06):
                    # 22 dos 33 SLs do dia foram meme-short. Impede empilhar.
                    open_in_cluster_dir = await _count_open_in_cluster_by_direction(
                        cluster, rec["direction"]
                    )
                    if open_in_cluster_dir >= CLUSTER_MAX_OPEN_PER_DIRECTION:
                        reason = (
                            f"cluster {cluster} {rec['direction']} cheio: "
                            f"{open_in_cluster_dir}/{CLUSTER_MAX_OPEN_PER_DIRECTION}"
                        )
                        log.info(f"[cluster-cap-dir] {rec['symbol']} {reason} — skip")
                        _record_skip(rec, "cluster-cap-dir", reason)
                        continue

            # ── Per-symbol SL cooldown (postmortem 04/06): bloqueia retry no
            # mesmo símbolo dentro de X horas após SL. FLOKI/NEIRO/PEOPLE/GALA
            # bateram SL 3-4× cada no mesmo dia.
            if not SHADOW_ENABLED and SYMBOL_SL_COOLDOWN_HOURS > 0:
                if await _has_recent_sl_on_symbol(rec["symbol"], SYMBOL_SL_COOLDOWN_HOURS):
                    reason = f"bateu SL nas últimas {SYMBOL_SL_COOLDOWN_HOURS:.0f}h"
                    log.info(f"[symbol-sl-cooldown] {rec['symbol']} {reason} — skip")
                    _record_skip(rec, "symbol-sl-cooldown", reason)
                    continue

            # ── Directional regime guard (postmortem 04/06): se 3+ SLs na
            # direção nas últimas 2h, pausa essa direção 1h. Detecta regime
            # adverso (mercado andando contra o viés do bot).
            if not SHADOW_ENABLED:
                blocked, reason = await _regime_blocked(rec["direction"])
                if blocked:
                    log.info(f"[regime-guard] {rec['direction']} bloqueado: {reason} — skip")
                    _record_skip(rec, "regime-guard", f"regime adverso: {reason}")
                    continue

            # ── Daily SL-rate breaker: se a taxa de SL do dia nessa direção
            # cruzou o limiar (>= BREAKER_MIN_SAMPLE decididas, >= BREAKER_SL_RATE
            # delas em SL), pausa entradas reais nessa direção por BREAKER_PAUSE_HOURS.
            if not SHADOW_ENABLED:
                blocked, reason = await _daily_sl_breaker(rec["direction"])
                if blocked:
                    log.info(f"[daily-breaker] {rec['direction']} bloqueado: {reason} — skip")
                    _record_skip(rec, "daily-sl-breaker", reason)
                    continue

            # ── Direction flip (Fase 2): se há trade aberto na direção oposta,
            # avalia gate. Passa → fecha atual primeiro. Bloqueia → advisory
            # (não abre, snapshot fica como referência informativa).
            if not SHADOW_ENABLED:
                opposite = await _find_opposite_open_trade(rec["symbol"], rec["direction"])
                if opposite is not None:
                    should_flip, reason = await _evaluate_flip_gate(opposite, rec)
                    if should_flip:
                        log.info(
                            f"[flip] {rec['symbol']} {opposite.side}→{rec['direction']}: {reason}"
                        )
                        ok = await _execute_flip(opposite)
                        if not ok:
                            log.warning(f"[flip] {rec['symbol']} falhou — pulando entrada nova")
                            continue
                        # flip executado — segue fluxo abrindo a nova direção
                    else:
                        log.info(
                            f"[flip] {rec['symbol']} ADVISORY (não executa): {reason}"
                        )
                        _record_skip(rec, "flip-advisory", f"trade oposto aberto, flip negado: {reason}")
                        # Flip negado: a rec oposta foi deixada fluir por
                        # save_recommendations só pra alimentar este avaliador.
                        # Como não disparou, expira o snapshot pra não poluir o
                        # painel com par long+short do mesmo símbolo.
                        try:
                            from services import snapshot_service as _snap
                            await _snap.expire_open_snapshot(
                                rec["symbol"], rec["direction"], reason="flip_advisory"
                            )
                        except Exception as _e:
                            log.debug(f"[flip] expire snapshot advisory falhou {rec['symbol']}: {_e}")
                        continue

                # ── TF upgrade (Fase 3): se já há trade aberto na MESMA
                # direção e a nova rec é de TF maior + qualidade superior,
                # ajusta SL/TPs do trade vivo em vez de abrir um segundo.
                same_dir = await _find_same_direction_open_trade(rec["symbol"], rec["direction"])
                if same_dir is not None:
                    mark = await _get_mark_price(rec["symbol"])
                    allow, reason, ctx = await _evaluate_upgrade_gate(same_dir, rec, mark)
                    if allow:
                        log.info(
                            f"[tf-upgrade] {rec['symbol']} #{same_dir.id}: {reason}"
                        )
                        ok = await _execute_tf_upgrade(same_dir, rec, ctx)
                        if not ok:
                            log.warning(
                                f"[tf-upgrade] {rec['symbol']} falhou — não abre trade novo"
                            )
                        # Seja sucesso ou falha do upgrade, NÃO abre um segundo trade
                        # na mesma direção. Pula pra próxima rec.
                        continue
                    else:
                        log.info(
                            f"[tf-upgrade] {rec['symbol']} SKIP upgrade ({reason}); "
                            f"trade existente continua — não abre duplicata"
                        )
                        # Trade já aberto na mesma direção; não abre paralelo
                        continue

            entry = float(rec.get("entry") or 0)
            stop = float(rec.get("stop_loss") or 0)
            risk_pct = float(rec.get("risk_pct") or 1.0)
            # ── #1 Funding-EV: calcula uma vez (qty-independente) e reusa no gate
            # e no tilt de size. Stash na rec pra não recalcular. Default OFF.
            _fund_ev_r = 0.0
            if not SHADOW_ENABLED and FUNDING_EV_ENABLED:
                try:
                    _fund_ev_r, _fund_ev_pct, _fund_ev_reason = _funding_ev_r(rec, entry, stop)
                    rec["_funding_ev_r"] = _fund_ev_r
                    # GATE: posição vai SANGRAR funding além do teto → não vale o edge.
                    if FUNDING_EV_MAX_DRAG_R > 0 and _fund_ev_r < -FUNDING_EV_MAX_DRAG_R:
                        reason = (f"funding-EV {_fund_ev_r:+.3f}R < -{FUNDING_EV_MAX_DRAG_R}R "
                                  f"({_fund_ev_reason})")
                        log.info(f"[funding-ev] {rec.get('symbol')} {reason} — skip")
                        _record_skip(rec, "funding-ev", reason)
                        continue
                except Exception as _e:
                    log.warning(f"[funding-ev] {rec.get('symbol')} cálculo falhou: {_e}")
                    _fund_ev_r = 0.0
            # #2a — sizing por convicção: escala o risco/trade pela P(TP1)
            # calibrada (dentro dos caps duros de _compute_qty). No-op se
            # desligado ou calibração imatura.
            _conv_mult, _conv_reason = _conviction_mult(rec)
            if _conv_mult != 1.0:
                _risk_before = risk_pct
                risk_pct = round(risk_pct * _conv_mult, 4)
                log.info(
                    f"[conviction] {rec.get('symbol')} risco {_risk_before:.2f}% "
                    f"→ {risk_pct:.2f}% ({_conv_reason})"
                )
            # #1 — sizing por EDGE (A+/funding/padrão/MTF). Compõe multiplicativo
            # com a convicção, dentro dos caps duros de _compute_qty. Default OFF.
            _edge_m, _edge_reason = _edge_mult(rec)
            if _edge_m != 1.0:
                _risk_before_e = risk_pct
                risk_pct = round(risk_pct * _edge_m, 4)
                log.info(
                    f"[edge-sizing] {rec.get('symbol')} risco {_risk_before_e:.2f}% "
                    f"→ {risk_pct:.2f}% ({_edge_reason})"
                )
            # Autoaprimoramento por HISTÓRICO COMPLETO (pós-sweep): multiplicador de
            # size por-moeda destilado da edge de todo o histórico (symbol_learned_
            # params). Compõe multiplicativo; caps duros de _compute_qty mandam depois.
            # Default OFF (SYMBOL_LEARNING_SIZE_ENABLED) — no-op até revisão humana.
            if not SHADOW_ENABLED:
                try:
                    from services import symbol_learning_service as _sls
                    _hist_m, _hist_reason = _sls.get_size_mult(
                        rec.get("symbol") or "", rec.get("timeframe")
                    )
                    if _hist_m != 1.0:
                        _risk_before_h = risk_pct
                        risk_pct = round(risk_pct * _hist_m, 4)
                        log.info(
                            f"[symbol-learning] {rec.get('symbol')} risco {_risk_before_h:.2f}% "
                            f"→ {risk_pct:.2f}% ({_hist_reason})"
                        )
                except Exception as _e:
                    log.warning(f"[symbol-learning] sizing por histórico falhou: {_e}")
            # #1 — tilt de size por Funding-EV: aumenta quem COLETA funding,
            # reduz quem PAGA. Compõe multiplicativo com convicção/edge; caps
            # duros de _compute_qty mandam depois. Default OFF.
            if not SHADOW_ENABLED and FUNDING_EV_ENABLED and FUNDING_EV_SIZE_ENABLED:
                _fund_m = 1.0 + _fund_ev_r * FUNDING_EV_SIZE_K
                _fund_m = max(FUNDING_EV_SIZE_MIN, min(FUNDING_EV_SIZE_MAX, _fund_m))
                if _fund_m != 1.0:
                    _risk_before_f = risk_pct
                    risk_pct = round(risk_pct * _fund_m, 4)
                    log.info(
                        f"[funding-ev-size] {rec.get('symbol')} risco {_risk_before_f:.2f}% "
                        f"→ {risk_pct:.2f}% (ev={_fund_ev_r:+.3f}R ×{_fund_m:.2f})"
                    )
            equity_usd, equity_src = await _resolve_equity_usd()
            # go-live #5 — em modo LIVE, nunca dimensiona dinheiro real com
            # equity fictício. Se a exchange falhou (source="fallback"), aborta
            # a trade em vez de usar o VIRTUAL_EQUITY_USD estático.
            if not SHADOW_ENABLED and equity_src == "fallback":
                log.error(
                    f"[shadow→live] {rec.get('symbol')} ABORT: equity ao vivo "
                    f"indisponível (fallback estático ${equity_usd:.0f}) — não "
                    f"dimensiona dinheiro real com equity fictício"
                )
                continue
            lev = int(rec.get("leverage") or 1)
            sizing = _compute_qty(entry, stop, risk_pct, equity_usd, leverage=lev)
            if sizing is None:
                log.warning(f"[shadow] {rec.get('symbol')} risk_dist=0 — pulando")
                continue
            log.info(
                f"[shadow] sizing {rec.get('symbol')}: equity=${equity_usd:.2f} "
                f"({equity_src}) → qty={sizing['qty']} notional=${sizing['notional_usd']} "
                f"risk_real={sizing['risk_pct_real']}% status={sizing['status']} ({sizing['reason']})"
            )
            if sizing["status"] == "skip":
                log.warning(
                    f"[shadow] {rec.get('symbol')} SKIP: {sizing['reason']} "
                    f"(would-be notional=${sizing['notional_usd']})"
                )
                continue
            qty = sizing["qty"]
            notional_effective = float(sizing["notional_usd"])

            # ── Exec size damper (liquidez/ATR-aware) — DEFENSIVO, flag OFF=NO-OP.
            # Reduz (não bloqueia) o size em vol alta (atr_pct) / posição grande vs
            # vol 24h. Fail-soft. Compõe com conviction e LIVE_SIZE_MULT. Se o damp
            # jogar o notional abaixo do mínimo da exchange, pula (vol alta demais).
            if not SHADOW_ENABLED and EXEC_SIZE_DAMP_ENABLED:
                _dmp, _dmp_reason = _exec_size_damp(rec, notional_effective)
                if _dmp < 1.0:
                    _qty_pre = qty
                    qty = round(qty * _dmp, 6)
                    notional_effective = qty * entry
                    log.info(
                        f"[size-damp] {rec.get('symbol')} qty {_qty_pre}→{qty} ({_dmp_reason})"
                    )
                    if notional_effective < MIN_NOTIONAL_USD:
                        log.warning(
                            f"[size-damp] {rec.get('symbol')} SKIP: notional pós-damp "
                            f"${notional_effective:.0f} < mín ${MIN_NOTIONAL_USD:.0f}"
                        )
                        _record_skip(rec, "size-damp", f"notional pós-damp < mín ({_dmp_reason})")
                        continue

            # ── Sizing por faixa de liquidez — mão menor em moeda magra.
            # Só LIVE. DEFENSIVO (só reduz). Se jogar abaixo do mínimo, pula
            # (moeda magra demais pro size atual — sem fill ruim).
            if not SHADOW_ENABLED and LIQ_TIER_SIZING_ENABLED:
                _lt, _lt_reason = _liq_tier_mult(rec)
                if _lt < 1.0:
                    _qty_pre_lt = qty
                    qty = round(qty * _lt, 6)
                    notional_effective = qty * entry
                    log.info(
                        f"[liq-tier] {rec.get('symbol')} qty {_qty_pre_lt}→{qty} ({_lt_reason})"
                    )
                    if notional_effective < MIN_NOTIONAL_USD:
                        log.warning(
                            f"[liq-tier] {rec.get('symbol')} SKIP: notional pós-tier "
                            f"${notional_effective:.0f} < mín ${MIN_NOTIONAL_USD:.0f}"
                        )
                        _record_skip(rec, "liq-tier", f"notional pós-tier < mín ({_lt_reason})")
                        continue

            # ── #6 Sizing por REGIME — mão menor em long de alt sob regime
            # adverso. Só LIVE. DEFENSIVO (só reduz). Compõe após liq-tier. Se
            # jogar abaixo do mínimo, pula (regime adverso + size pequeno).
            if not SHADOW_ENABLED and REGIME_SIZING_ENABLED:
                _rg, _rg_reason = _regime_size_mult(rec, _regime_cached)
                if _rg < 1.0:
                    _qty_pre_rg = qty
                    qty = round(qty * _rg, 6)
                    notional_effective = qty * entry
                    log.info(
                        f"[regime-size] {rec.get('symbol')} qty {_qty_pre_rg}→{qty} ({_rg_reason})"
                    )
                    if notional_effective < MIN_NOTIONAL_USD:
                        log.warning(
                            f"[regime-size] {rec.get('symbol')} SKIP: notional pós-regime "
                            f"${notional_effective:.0f} < mín ${MIN_NOTIONAL_USD:.0f}"
                        )
                        _record_skip(rec, "regime-size", f"notional pós-regime < mín ({_rg_reason})")
                        continue

            # Filler FORA: size reduzido (×FILLER_FORA_SIZE_MULT) só pra posições
            # marcadas como filler de slot ocioso. Aplica ANTES do canary global.
            if not SHADOW_ENABLED and FILLER_FORA_ENABLED and rec.get("_is_filler"):
                _qty_full_flr = qty
                # Teste cauteloso: os primeiros FILLER_FORA_TEST_N FORA usam size
                # reduzido de teste; depois volta sozinho ao mult normal.
                _flr_in_test = FILLER_FORA_TEST_N > 0 and _flr_test_done < FILLER_FORA_TEST_N
                _flr_mult = FILLER_FORA_TEST_SIZE_MULT if _flr_in_test else FILLER_FORA_SIZE_MULT
                qty = round(qty * _flr_mult, 6)
                notional_effective = qty * entry
                if notional_effective < MIN_NOTIONAL_USD:
                    log.warning(
                        f"[filler-fora] {rec.get('symbol')} SKIP: size×{_flr_mult} "
                        f"→ notional ${notional_effective:.0f} < mín ${MIN_NOTIONAL_USD:.0f} "
                        f"(qty cheio {_qty_full_flr})"
                    )
                    _record_skip(rec, "filler-fora", "notional pós-filler < mín")
                    continue
                log.info(
                    f"[filler-fora] {rec.get('symbol')}: qty {_qty_full_flr} → {qty} "
                    f"(×{_flr_mult}{' TESTE ' + str(_flr_test_done + 1) + '/' + str(FILLER_FORA_TEST_N) if _flr_in_test else ''}); "
                    f"notional ${notional_effective:.0f}"
                )

            # go-live #2 — canary/ramp: em LIVE, escala o tamanho por um
            # multiplicador global pra começar pequeno e subir gradual. Não
            # afeta shadow. Se a fração jogar o notional abaixo do mínimo da
            # exchange, pula (canary pequeno demais pra esse símbolo).
            if not SHADOW_ENABLED and LIVE_SIZE_MULT < 1.0:
                qty_full = qty
                qty = round(qty * LIVE_SIZE_MULT, 6)
                notional_effective = qty * entry
                if notional_effective < MIN_NOTIONAL_USD:
                    log.warning(
                        f"[shadow→live] {rec.get('symbol')} SKIP canary: "
                        f"size×{LIVE_SIZE_MULT} → notional ${notional_effective:.0f} "
                        f"< mín ${MIN_NOTIONAL_USD:.0f} (qty cheio {qty_full})"
                    )
                    continue
                log.info(
                    f"[shadow→live] canary {rec.get('symbol')}: qty {qty_full} → "
                    f"{qty} (×{LIVE_SIZE_MULT}); notional ${notional_effective:.0f}"
                )

            # ── #2 Auditoria composta do stack de sizing (validável) ─────────
            # Consolida num único log o que as camadas de sizing inteligente
            # aplicaram (conviction×edge×damp×liq×regime). Só live; puro, sem
            # efeito na qty. Facilita validar a composição antes de ligar mais
            # camadas (ex.: EDGE_SIZING) com confiança.
            if not SHADOW_ENABLED:
                try:
                    _stack_net, _stack_bd = _sizing_stack_report(
                        rec, _regime_cached, notional_effective
                    )
                    log.info(
                        f"[sizing-stack] {rec.get('symbol')}: net×{_stack_net:.2f} "
                        f"({_stack_bd}); risk_pct={risk_pct:.2f}% qty={qty} "
                        f"notional=${notional_effective:.0f}"
                    )
                except Exception as _e:
                    log.warning(f"[sizing-stack] {rec.get('symbol')} relatório falhou: {_e}")

            # Cap de exposição agregada — bloqueia se total notional > X% banca
            try:
                open_notional = await _open_notional_usd()
                new_notional = notional_effective
                total_after = open_notional + new_notional
                cap_usd = equity_usd * (MAX_TOTAL_NOTIONAL_PCT / 100.0)
                if total_after > cap_usd:
                    log.warning(
                        f"[shadow] {rec.get('symbol')} BLOCKED total-notional cap: "
                        f"open=${open_notional:.0f} + new=${new_notional:.0f} = "
                        f"${total_after:.0f} > cap ${cap_usd:.0f} "
                        f"({MAX_TOTAL_NOTIONAL_PCT}% × equity ${equity_usd:.0f})"
                    )
                    continue
            except Exception as e:
                log.warning(f"[shadow] total-notional check falhou: {e}")

            # Cap de MARGEM agregada — "manter só X% da banca aberta no máx".
            # Conta capital comprometido (notional/lev), não o notional cheio.
            # Posições fechadas liberam orçamento pra novas. 0 = desligado.
            if MAX_TOTAL_MARGIN_PCT > 0:
                try:
                    open_margin = await _open_margin_usd()
                    new_margin = notional_effective / max(int(lev or 1), 1)
                    margin_after = open_margin + new_margin
                    margin_cap = equity_usd * (MAX_TOTAL_MARGIN_PCT / 100.0)
                    if margin_after > margin_cap:
                        log.warning(
                            f"[shadow] {rec.get('symbol')} BLOCKED total-margin cap: "
                            f"open=${open_margin:.0f} + new=${new_margin:.0f} = "
                            f"${margin_after:.0f} > cap ${margin_cap:.0f} "
                            f"({MAX_TOTAL_MARGIN_PCT}% × equity ${equity_usd:.0f})"
                        )
                        continue
                except Exception as e:
                    log.warning(f"[shadow] total-margin check falhou: {e}")

            # #2b — Cap de RISCO aberto agregado: bloqueia se a soma do risco em
            # aberto (Σ |entry−stop|×qty) + o risco desta trade passar do teto da
            # banca. Diferente do notional/margem — conta a PERDA potencial se
            # tudo estopar junto. Posições pós-TP1 já contam risco ~0 (BE) →
            # orçamento rotativo. 0 = desligado (default).
            if MAX_TOTAL_OPEN_RISK_PCT > 0:
                try:
                    open_risk = await _open_risk_usd()
                    new_risk = abs(entry - stop) * qty
                    risk_after = open_risk + new_risk
                    risk_cap = equity_usd * (MAX_TOTAL_OPEN_RISK_PCT / 100.0)
                    if risk_after > risk_cap:
                        reason = (
                            f"risco aberto ${open_risk:.0f} + novo ${new_risk:.0f} = "
                            f"${risk_after:.0f} > teto ${risk_cap:.0f} "
                            f"({MAX_TOTAL_OPEN_RISK_PCT}% × equity ${equity_usd:.0f})"
                        )
                        log.warning(f"[shadow] {rec.get('symbol')} BLOCKED risk-budget: {reason}")
                        _record_skip(rec, "risk-budget", reason)
                        continue
                except Exception as e:
                    log.warning(f"[shadow] risk-budget check falhou: {e}")

            # Snapshot_id é setado em save_recommendations? Não — o `_just_saved`
            # flag é booleano. Precisamos do id do snapshot recém-criado pra
            # linkar. Resolvemos olhando o registro: filtra por symbol+direction
            # mais recente.
            from sqlalchemy import select, desc
            from db import get_session
            from models.recommendation_snapshot import RecommendationSnapshot

            async with get_session() as session:
                stmt = (
                    select(RecommendationSnapshot.id)
                    .where(RecommendationSnapshot.symbol == rec["symbol"])
                    .where(RecommendationSnapshot.direction == rec["direction"])
                    .where(RecommendationSnapshot.timeframe == rec["timeframe"])
                    .order_by(desc(RecommendationSnapshot.created_at))
                    .limit(1)
                )
                snap_id = (await session.execute(stmt)).scalar_one_or_none()

            if snap_id is None:
                log.warning(f"[shadow] snapshot_id não achado pra {rec.get('symbol')} — pulando")
                continue

            side = "long" if rec.get("direction") == "long" else "short"
            tp1 = None
            sig = rec.get("signal") or {}
            if isinstance(sig, dict):
                tp1 = sig.get("tp1")

            tp2 = float(rec.get("tp2") or 0) or None

            # ─── LIVE EXECUTION (kill-switch + exchange call) ────────────
            exchange_order_id = None
            client_order_id = None
            exchange_name = os.getenv("EXCHANGE", "binance")
            source = "shadow"
            entry_actual = entry
            # entry_actual cai no entry TEÓRICO até a corretora devolver avgPrice
            # real. Só marcamos fill real quando avg>0 — senão o slippage seria 0%
            # falso (mascara o slippage real). Telemetria fica None até backfill.
            entry_is_real_fill = False

            # Partials adaptativos (por-trade) — defaults no escopo externo; o
            # cálculo real acontece no passo 1d (só no fluxo live). Em shadow o
            # fechamento vem do snapshot, não do trade_manager, então não aplica.
            _adaptive = None
            _adaptive_idx = None
            _open_tp1_pct = 0.45  # fração TP1 usada na abertura (default = fixo)

            if not SHADOW_ENABLED:
                # 0. Trava de dinheiro real (go-live #1) — produção exige
                #    confirmação explícita. Sem ela, NÃO envia ordem real.
                armed, guard_why = _live_money_guard()
                if not armed:
                    log.error(
                        f"[shadow→live] ⛔ BLOCKED {rec['symbol']} {side}: {guard_why}"
                    )
                    continue

                # 1. Kill-switch
                from services import kill_switch_service
                ks = await kill_switch_service.check_can_trade()
                if not ks.get("allowed"):
                    log.warning(
                        f"[shadow→live] BLOCKED {rec['symbol']} {side}: {ks.get('reason')}"
                    )
                    continue

                # 1b. Filtro de sessão/horário (opcional, off por padrão)
                if TRADE_BLOCK_HOURS_UTC:
                    _hr = datetime.now(timezone.utc).hour
                    if _hr in TRADE_BLOCK_HOURS_UTC:
                        log.info(
                            f"[shadow→live] BLOCKED {rec['symbol']} {side}: "
                            f"sessão UTC {_hr}h em janela bloqueada {sorted(TRADE_BLOCK_HOURS_UTC)}"
                        )
                        continue

                # 1c. Pregão para moedas lastreadas em ações (bStocks/stock-perps)
                #     — só operam no horário regular da bolsa dos EUA. Demais
                #     criptos seguem 24h (não caem aqui).
                if EQUITY_US_HOURS_ONLY and _is_equity_backed(rec["symbol"]):
                    if not _us_equity_session_open_now():
                        log.info(
                            f"[shadow→live] BLOCKED {rec['symbol']} {side}: "
                            f"moeda lastreada em ação ({_equity_base_of(rec['symbol'])}) "
                            f"fora do pregão regular EUA "
                            f"({os.getenv('EQUITY_SESSION_ET', '09:30-16:00')} ET, dia útil)"
                        )
                        continue

                # 1d. Partials adaptativos (por-trade) — decide fração do TP1,
                #     tamanho do runner e largura do trailing conforme a convicção
                #     e a volatilidade DESTA operação. Precisa ser ANTES da ordem
                #     pra a fração do TP1 valer na parcial enviada à corretora.
                if adaptive_partials_service.is_enabled():
                    try:
                        _atr_pct = _get_rec_feature(rec, "atr_pct")
                        _adaptive = adaptive_partials_service.compute(
                            tier=rec.get("tier"),
                            score=rec.get("score"),
                            edge_score=rec.get("edge_score"),
                            prob_tp2=rec.get("prob_tp2"),
                            atr_pct=_atr_pct,
                        )
                        if _adaptive:
                            _open_tp1_pct = float(_adaptive.get("tp1_qty_pct") or 0.45)
                            _tc = adaptive_partials_service.test_count()
                            _cnt = await real_trade_service.count_adaptive_test_trades()
                            if _cnt < _tc:
                                _adaptive_idx = _cnt + 1
                            log.info(
                                f"[adaptive-partials] {rec['symbol']}: {_adaptive['reason']}"
                                + (f" (🧪 teste {_adaptive_idx}/{_tc})" if _adaptive_idx else "")
                            )
                    except Exception as e:
                        log.warning(f"[adaptive-partials] wiring falhou: {e}")

                # 2. Exchange order
                from services import exchange_service
                exch_side = "Buy" if side == "long" else "Sell"
                client_order_id = f"cw-{snap_id}"  # crypto-win + snap id
                # #4: entrada MAKER (post-only) quando ligada E o helper existe na
                # exchange ativa (Binance). Posta LIMIT GTX no entry planejado e só
                # protege após o fill; se não preencher → fallback MARKET interno.
                # Mantém o MESMO shape de retorno (sl_ok/tp1_ok/tp2_ok) → o resto do
                # fluxo (guard "sem stop", captura de IDs) não muda.
                _maker_fn = getattr(exchange_service, "place_maker_entry_then_protect", None)
                if MAKER_ENTRY_ENABLED and _maker_fn is not None:
                    order_res = await _maker_fn(
                        symbol=rec["symbol"],
                        side=exch_side,
                        qty=qty,
                        limit_price=float(entry),
                        stop_loss=stop,
                        take_profit=tp2,
                        tp1=float(tp1) if tp1 is not None else None,
                        tp1_qty_pct=_open_tp1_pct,
                        leverage=int(rec.get("leverage") or 1),
                        client_order_id=client_order_id,
                    )
                    if isinstance(order_res, dict) and order_res.get("ok"):
                        log.info(
                            f"[shadow→live] entrada {rec['symbol']} via "
                            f"{'MAKER' if order_res.get('was_maker') and not order_res.get('fell_back_to_market') else 'MARKET(fallback)'}"
                        )
                else:
                    order_res = await exchange_service.place_order(
                        symbol=rec["symbol"],
                        side=exch_side,
                        qty=qty,
                        order_type="Market",
                        stop_loss=stop,
                        take_profit=tp2,  # TP2 — alvo final (closePosition=true)
                        tp1=float(tp1) if tp1 is not None else None,  # bracket 45/55 quando ambos vierem
                        tp1_qty_pct=_open_tp1_pct,  # fração adaptativa (ou 0.45 fixo)
                        leverage=int(rec.get("leverage") or 1),
                        client_order_id=client_order_id,
                    )
                if not order_res.get("ok"):
                    log.error(
                        f"[shadow→live] place_order falhou {rec['symbol']}: "
                        f"{order_res.get('msg') or order_res.get('error')}"
                    )
                    continue

                result = order_res.get("result") or {}
                exchange_order_id = str(result.get("orderId") or result.get("orderID") or "")
                # Binance retorna avgPrice; Bybit retorna em outro campo
                avg = result.get("avgPrice") or result.get("avgFillPrice")
                if avg:
                    try:
                        _avg_f = float(avg)
                        if _avg_f > 0:
                            entry_actual = _avg_f
                            entry_is_real_fill = True  # fill real confirmado
                    except Exception:
                        pass
                source = "auto"

                # Captura IDs das ordens condicionais pro trade manager (Fase 2)
                sl_oid = order_res.get("sl_order_id")
                tp1_oid = order_res.get("tp1_order_id")
                tp2_oid = order_res.get("tp2_order_id")
                if not order_res.get("sl_ok"):
                    # Dinheiro real NUNCA pode ficar sem stop. Se o SL falhou,
                    # fecha a posição a mercado imediatamente (reduce_only) em vez
                    # de deixá-la nua. Regra de ouro: "sem stop = sem trade".
                    log.error(
                        f"[shadow→live] ⚠ {rec['symbol']} ABERTO SEM STOP — "
                        f"fechando a mercado por segurança"
                    )
                    close_side = "Sell" if exch_side == "Buy" else "Buy"
                    closed_ok = False
                    try:
                        close_res = await exchange_service.place_order(
                            symbol=rec["symbol"],
                            side=close_side,
                            qty=qty,
                            order_type="Market",
                            reduce_only=True,
                            client_order_id=f"cw-nostop-{snap_id}",
                        )
                        closed_ok = bool(close_res.get("ok"))
                        if not closed_ok:
                            log.critical(
                                f"[shadow→live] 🚨 {rec['symbol']} FALHA AO FECHAR posição sem stop: "
                                f"{close_res.get('msg') or close_res.get('error')}"
                            )
                    except Exception as _e:
                        log.critical(
                            f"[shadow→live] 🚨 {rec['symbol']} erro ao fechar posição sem stop: {_e}"
                        )
                    # Alerta push imediato (crítico) — você precisa saber na hora.
                    try:
                        from services import push_service
                        _sym = rec["symbol"].split("/")[0]
                        if closed_ok:
                            await push_service.notify_alert(
                                title=f"🛡️ {_sym}: entrada sem stop foi FECHADA",
                                body="O stop não foi criado na corretora. A posição foi fechada a mercado por segurança — sem exposição desprotegida.",
                                tag=f"nostop-{snap_id}",
                            )
                        else:
                            await push_service.notify_alert(
                                title=f"🚨 {_sym}: POSIÇÃO SEM STOP — AÇÃO MANUAL",
                                body="O stop falhou E o fechamento automático falhou. Há uma posição real SEM proteção. Feche manualmente AGORA.",
                                tag=f"nostop-crit-{snap_id}",
                            )
                    except Exception:
                        pass
                    # Não registra trade aberto: posição foi fechada (ou exige
                    # intervenção manual). Pula pro próximo rec.
                    continue
                if order_res.get("tp1_skipped"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP1 skip (qty parcial=0); 100% no TP2")
                elif not order_res.get("tp1_ok"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP1 falhou (sem parcial)")
                if not order_res.get("tp2_ok"):
                    log.warning(f"[shadow→live] {rec['symbol']} TP2 falhou")

                log.info(
                    f"[shadow→live] EXECUTED {rec['symbol']} {exch_side} qty={qty} "
                    f"order_id={exchange_order_id} avg={entry_actual} "
                    f"SL={sl_oid} TP1={tp1_oid} TP2={tp2_oid}"
                )

            # IDs das ordens condicionais (só existem no fluxo "auto"; em shadow ficam None)
            _sl_oid = locals().get("sl_oid") if source == "auto" else None
            _tp1_oid = locals().get("tp1_oid") if source == "auto" else None
            _tp2_oid = locals().get("tp2_oid") if source == "auto" else None

            trade = await real_trade_service.open_trade(
                symbol=rec["symbol"],
                side=side,
                qty=qty,
                entry_price=entry_actual,
                recommendation_id=snap_id,
                leverage=int(rec.get("leverage") or 1),
                planned_stop=stop,
                planned_tp1=float(tp1) if tp1 is not None else None,
                planned_tp2=tp2,
                entry_fee=0.0,
                source=source,
                exchange=exchange_name,
                exchange_order_id=exchange_order_id,
                client_order_id=client_order_id,
                notes=f"{source} auto-open (tier {tier})" + (" [filler]" if rec.get("_is_filler") else ""),
                entry_is_real_fill=entry_is_real_fill,
                sl_order_id=_sl_oid,
                tp1_order_id=_tp1_oid,
                tp2_order_id=_tp2_oid,
                sl_current_price=stop,
                adaptive_tp1_qty_pct=(_adaptive or {}).get("tp1_qty_pct"),
                adaptive_runner_atr_mult=(_adaptive or {}).get("runner_atr_mult"),
                adaptive_runner_qty_pct=(_adaptive or {}).get("runner_qty_pct"),
                adaptive_test_idx=_adaptive_idx,
            )
            if trade is not None:
                opened += 1
                # Atualiza slots do filler DENTRO do batch (DB só reflete pós-commit):
                # mantém o teto FORA correto entre recs do mesmo ciclo.
                if not SHADOW_ENABLED and FILLER_FORA_ENABLED:
                    if rec.get("_is_filler"):
                        _flr_n_fora += 1
                        if FILLER_FORA_TEST_N > 0:
                            _flr_test_done += 1
                    else:
                        _flr_n_dentro += 1
                log.info(
                    f"[{source}] OPEN {rec['symbol']} {side} qty={qty} entry={entry_actual} "
                    f"SL={stop} TP1={tp1} TP2={tp2} (snap={snap_id})"
                )
                # Push só pra execução real (auto). Shadow fica silencioso pra
                # não floodar enquanto o sistema simula em paralelo.
                if source == "auto":
                    try:
                        from services import push_service
                        await push_service.notify_trade_open({
                            **trade,
                            "planned_stop": stop,
                            "planned_tp1": float(tp1) if tp1 is not None else None,
                            "planned_tp2": tp2,
                        })
                    except Exception as e:
                        log.warning(f"[shadow] push trade-open falhou: {e}")
                    # Contador do teste canário a 0.50 (#N/alvo + marco)
                    try:
                        from services import live_test_service
                        await live_test_service.on_auto_trade_opened({
                            **trade,
                            "_is_filler": bool(rec.get("_is_filler")),
                            "planned_stop": stop,
                            "planned_tp1": float(tp1) if tp1 is not None else None,
                            "planned_tp2": tp2,
                        })
                    except Exception as e:
                        log.warning(f"[shadow] live-test contador falhou: {e}")
                    # Telegram notify (desacoplado - no-op se nao configurado)
                    try:
                        from services.notification_service import (
                            send_telegram,
                            fmt_trade_opened,
                        )
                        await send_telegram(
                            fmt_trade_opened(
                                {
                                    **trade,
                                    "planned_stop": stop,
                                    "planned_tp1": float(tp1) if tp1 is not None else None,
                                    "planned_tp2": tp2,
                                },
                                rec,
                            ),
                            event_type="open",
                        )
                    except Exception as e:
                        log.warning(f"[notify] telegram open falhou: {e}")
        except Exception as e:
            log.warning(f"[shadow] falha abrindo trade pra {rec.get('symbol')}: {e}")

    if opened:
        log.info(f"[shadow] trades abertos: {opened}")
    return opened


# Mapeia status interno do snapshot → status do RealTrade
_STATUS_MAP = {
    "won_tp2": "closed_tp2",
    "won_tp1": "closed_tp1",
    "won_tp1_be": "closed_be",
    "lost": "closed_stop",
    "expired": "closed_manual",  # sem hit, fecha "neutro"
}


async def close_shadow_for_snapshot(snap) -> bool:
    """
    Chamado por snapshot_service.check_open_snapshots quando um snap resolve.
    Procura o RealTrade shadow ligado e fecha com o mesmo outcome.

    Retorna True se fechou algo, False senão (não existia trade shadow).
    """
    if not DB_ENABLED or snap is None:
        return False
    if snap.status not in _STATUS_MAP:
        return False
    if snap.outcome_price is None:
        return False

    from sqlalchemy import select
    from db import get_session
    from models.real_trade import RealTrade

    async with get_session() as session:
        stmt = (
            select(RealTrade)
            .where(RealTrade.recommendation_id == snap.id)
            .where(RealTrade.source.in_(("shadow", "auto")))
            .where(RealTrade.status == "open")
        )
        trade = (await session.execute(stmt)).scalar_one_or_none()
        if trade is None:
            return False

    # FIX CRÍTICO: paper-trade NÃO fecha trades reais (source="auto").
    # Antes, snap resolvendo via candle simulado fechava o RealTrade no DB,
    # mas a posição na exchange seguia aberta (preço só passou perto do TP,
    # não bateu o trigger real). Resultado: DB "closed" + posição órfã +
    # PnL errado calculado com exit=planned_tp2 e entry possivelmente 0.
    #
    # Comportamento correto:
    #   - source="shadow": fecha via paper (simulação é a fonte da verdade)
    #   - source="auto" + qualquer outcome (tp1/tp2/be/stop): NÃO fecha,
    #     deixa o trade_manager (que poll a exchange) detectar qty=0 e fechar.
    #   - source="auto" + expired: ainda emite market close (snap expirou,
    #     posição precisa ser fechada explicitamente — não há trigger pendente).
    if trade.source == "auto" and snap.status != "expired":
        log.debug(
            f"[shadow] skip close paper-resolved trade#{trade.id} {trade.symbol} "
            f"source=auto snap={snap.status} — trade_manager cuida via polling"
        )
        return False

    new_status = _STATUS_MAP[snap.status]
    # Se foi execução real (auto) com TP/SL já emitidos como ordens separadas,
    # o exchange resolveu sozinho — só atualizamos o DB pra refletir.
    # Se snap.status=expired (não bateu nada), pode ser que a posição esteja
    # aberta na exchange ainda; pra esse caso emitimos market close.
    if trade.source == "auto" and snap.status == "expired":
        try:
            from services import exchange_service
            close_side = "Sell" if trade.side == "long" else "Buy"
            close_res = await exchange_service.place_order(
                symbol=trade.symbol,
                side=close_side,
                qty=float(trade.qty),
                order_type="Market",
                reduce_only=True,
                client_order_id=f"cw-close-{trade.id}",
            )
            if not close_res.get("ok"):
                log.warning(
                    f"[live] close_position falhou trade#{trade.id}: "
                    f"{close_res.get('msg') or close_res.get('error')}"
                )
        except Exception as e:
            log.warning(f"[live] erro fechando posição #{trade.id}: {e}")

    await real_trade_service.close_trade(
        trade_id=trade.id,
        exit_price=float(snap.outcome_price),
        status=new_status,
        exit_fee=0.0,
        notes=f"{trade.source} auto-close from snap #{snap.id} ({snap.status})",
    )
    log.info(
        f"[shadow] CLOSE trade#{trade.id} {snap.symbol} → {new_status} "
        f"@ {snap.outcome_price} (snap_status={snap.status})"
    )
    return True
