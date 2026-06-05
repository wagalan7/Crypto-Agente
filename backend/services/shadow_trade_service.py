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

log = logging.getLogger(__name__)

SHADOW_ENABLED = os.getenv("EXCHANGE_SHADOW", "true").strip().lower() in ("1", "true", "yes")
# Fallback estático — usado APENAS se a exchange estiver fora do ar.
# Em condições normais, exchange_service.get_equity() lê o saldo real.
VIRTUAL_EQUITY_USD = float(os.getenv("EXCHANGE_SHADOW_EQUITY_USD", "5000"))

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

# ── Score threshold (postmortem) ───────────────────────────────────────────
# Subimos o piso de score para 72 (era implicitamente >=65 via tier A). O
# postmortem mostrou win-rate sensivelmente melhor acima de 75, mas 75
# estava bloqueando trades demais (0 entradas em 48h). 72 = meio-termo
# pra coletar amostra mantendo qualidade. Override via env SCORE_MIN.
SCORE_MIN = float(os.getenv("SCORE_MIN", "72"))

# ── Time-of-day block (postmortem 104 snapshots / 168h) ────────────────────
# Sessão EU (7-14 UTC) mostrou 50 trades / 42% wr / lift -21.46%.
# Quinta-feira mostrou 67 trades / 50.75% wr / lift -12.72%. Bloqueia ambos
# por padrão; override via env (string vazia desativa).
BLOCK_HOURS_UTC = os.getenv("BLOCK_HOURS_UTC", "7,8,9,10,11,12,13").strip()
_BLOCKED_HOURS: set[int] = set()
if BLOCK_HOURS_UTC:
    try:
        _BLOCKED_HOURS = {int(h.strip()) for h in BLOCK_HOURS_UTC.split(",") if h.strip()}
    except Exception:
        _BLOCKED_HOURS = set()

BLOCK_DAYS_UTC = os.getenv("BLOCK_DAYS_UTC", "").strip().lower()  # vazio por padrão — bloqueio por dia exige >= 4 semanas de dados pra ter sinal estatisticamente válido
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

def _get_rec_feature(rec: dict, key: str, default=None):
    """Extrai feature da rec acessando rec['signal'] (que carrega mtf/derivatives).
    Suporta:
      - 'mtf_aligned'   → signal.mtf.aligned_count (int) ou None
      - 'funding_pct'   → signal.derivatives.funding_rate_pct (% já em pct) ou None
      - 'hour_utc'      → derivado de datetime.now() (uso interno)
    Safety: nunca lança."""
    try:
        sig = rec.get("signal") or {}
        if not isinstance(sig, dict):
            return default
        if key == "mtf_aligned":
            mtf = sig.get("mtf") or {}
            if not isinstance(mtf, dict):
                return default
            return mtf.get("aligned_count", default)
        if key == "funding_pct":
            der = sig.get("derivatives") or {}
            if not isinstance(der, dict):
                return default
            return der.get("funding_rate_pct", default)
    except Exception:
        return default
    return default


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
    """Retorna (blocked, reason). Confere pausa armada + arma nova se preciso."""
    import time
    now = time.time()
    until = _REGIME_PAUSE_UNTIL.get(direction, 0)
    if until > now:
        mins = (until - now) / 60.0
        return True, f"pausa ativa há {mins:.0f}min"
    sl_count = await _count_recent_sl_by_direction(direction, REGIME_GUARD_WINDOW_HOURS)
    if sl_count >= REGIME_GUARD_MAX_SL:
        _REGIME_PAUSE_UNTIL[direction] = now + REGIME_GUARD_PAUSE_HOURS * 3600
        return True, (
            f"{sl_count} SLs {direction} em {REGIME_GUARD_WINDOW_HOURS:.0f}h — "
            f"pausa {REGIME_GUARD_PAUSE_HOURS:.0f}h"
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
    return {
        "shadow_enabled": SHADOW_ENABLED,
        "fallback_equity_usd": VIRTUAL_EQUITY_USD,
        "sizing_mode": "live (com fallback estático em erro)",
        "min_notional_usd": MIN_NOTIONAL_USD,
        "max_risk_pct_hard": MAX_RISK_PCT_HARD,
        "max_margin_pct_per_trade": MAX_MARGIN_PCT_PER_TRADE,
        "max_total_notional_pct": MAX_TOTAL_NOTIONAL_PCT,
        "exchange_active": os.getenv("EXCHANGE", "binance"),
        "note": "Sizing: risk_pct nominal; eleva ao mín notional; capa em margin%/trade e total notional%.",
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

    opened = 0
    for rec in recs:
        try:
            if not rec.get("_just_saved"):
                continue
            tier = rec.get("tier")
            if tier not in ("A+", "A"):
                continue

            # ── Score threshold (postmortem): piso configurável.
            try:
                rec_score = float(rec.get("score") or 0)
            except Exception:
                rec_score = 0.0
            if rec_score < SCORE_MIN:
                log.info(
                    f"[score-min] {rec.get('symbol')} score={rec_score:.0f} < "
                    f"{SCORE_MIN:.0f} — skip"
                )
                continue

            # ── Symbol blacklist (postmortem): pula símbolos banidos.
            if not SHADOW_ENABLED:
                base = _symbol_base(rec["symbol"])
                if base and base in SYMBOL_BLACKLIST:
                    log.info(f"[blacklist] {rec['symbol']} skip")
                    continue

            # ── Time-of-day block (postmortem -21% lift EU / -12% lift quinta).
            if not SHADOW_ENABLED:
                now_utc = datetime.now(timezone.utc)
                blocked, reason = _is_blocked_time(now_utc)
                if blocked:
                    log.info(f"[time-block] {rec.get('symbol')} {reason} — skip")
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
                        log.info(
                            f"[funding-gate] {rec.get('symbol')} long blocked "
                            f"funding={funding_val:.4f}% > {FUNDING_BLOCK_THRESHOLD}% — skip"
                        )
                        continue
                    if direction == "short" and funding_val < -FUNDING_BLOCK_THRESHOLD:
                        log.info(
                            f"[funding-gate] {rec.get('symbol')} short blocked "
                            f"funding={funding_val:.4f}% < -{FUNDING_BLOCK_THRESHOLD}% — skip"
                        )
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
                        log.info(
                            f"[mtf-gate] {rec.get('symbol')} aligned_count={aligned_count} "
                            f"< {MTF_ALIGNED_MIN_COUNT} mode=required — skip"
                        )
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
                    log.info(
                        f"[entry-throttle] cooldown={age:.0f}s "
                        f"last_hour={last_hour}/{ENTRY_MAX_PER_HOUR} — skip"
                    )
                    continue

            # ── Global directional cap (postmortem): max longs/shorts.
            if not SHADOW_ENABLED:
                dir_count = await _count_open_by_direction(rec["direction"])
                if dir_count >= MAX_OPEN_PER_DIRECTION:
                    log.info(
                        f"[direction-cap] {rec['direction']} "
                        f"{dir_count}/{MAX_OPEN_PER_DIRECTION} — skip"
                    )
                    continue

            # ── Cluster correlation cap (postmortem): bloqueia se já há
            # CLUSTER_MAX_OPEN trades abertos num cluster correlacionado.
            if not SHADOW_ENABLED:
                cluster = _get_symbol_cluster(rec["symbol"])
                if cluster != "other":
                    open_in_cluster = await _count_open_in_cluster(cluster)
                    if open_in_cluster >= CLUSTER_MAX_OPEN:
                        log.info(
                            f"[cluster-cap] {rec['symbol']} cluster={cluster} "
                            f"{open_in_cluster}/{CLUSTER_MAX_OPEN} — skip"
                        )
                        continue
                    # Cap por direção dentro do cluster (postmortem 04/06):
                    # 22 dos 33 SLs do dia foram meme-short. Impede empilhar.
                    open_in_cluster_dir = await _count_open_in_cluster_by_direction(
                        cluster, rec["direction"]
                    )
                    if open_in_cluster_dir >= CLUSTER_MAX_OPEN_PER_DIRECTION:
                        log.info(
                            f"[cluster-cap-dir] {rec['symbol']} cluster={cluster} "
                            f"{rec['direction']}={open_in_cluster_dir}/"
                            f"{CLUSTER_MAX_OPEN_PER_DIRECTION} — skip"
                        )
                        continue

            # ── Per-symbol SL cooldown (postmortem 04/06): bloqueia retry no
            # mesmo símbolo dentro de X horas após SL. FLOKI/NEIRO/PEOPLE/GALA
            # bateram SL 3-4× cada no mesmo dia.
            if not SHADOW_ENABLED and SYMBOL_SL_COOLDOWN_HOURS > 0:
                if await _has_recent_sl_on_symbol(rec["symbol"], SYMBOL_SL_COOLDOWN_HOURS):
                    log.info(
                        f"[symbol-sl-cooldown] {rec['symbol']} bateu SL nas últimas "
                        f"{SYMBOL_SL_COOLDOWN_HOURS:.0f}h — skip"
                    )
                    continue

            # ── Directional regime guard (postmortem 04/06): se 3+ SLs na
            # direção nas últimas 2h, pausa essa direção 1h. Detecta regime
            # adverso (mercado andando contra o viés do bot).
            if not SHADOW_ENABLED:
                blocked, reason = await _regime_blocked(rec["direction"])
                if blocked:
                    log.info(
                        f"[regime-guard] {rec['direction']} bloqueado: {reason} — skip"
                    )
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
            equity_usd, equity_src = await _resolve_equity_usd()
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

            # Cap de exposição agregada — bloqueia se total notional > X% banca
            try:
                open_notional = await _open_notional_usd()
                new_notional = float(sizing["notional_usd"])
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

            if not SHADOW_ENABLED:
                # 1. Kill-switch
                from services import kill_switch_service
                ks = await kill_switch_service.check_can_trade()
                if not ks.get("allowed"):
                    log.warning(
                        f"[shadow→live] BLOCKED {rec['symbol']} {side}: {ks.get('reason')}"
                    )
                    continue

                # 2. Exchange order
                from services import exchange_service
                exch_side = "Buy" if side == "long" else "Sell"
                client_order_id = f"cw-{snap_id}"  # crypto-win + snap id
                order_res = await exchange_service.place_order(
                    symbol=rec["symbol"],
                    side=exch_side,
                    qty=qty,
                    order_type="Market",
                    stop_loss=stop,
                    take_profit=tp2,  # TP2 — alvo final (closePosition=true)
                    tp1=float(tp1) if tp1 is not None else None,  # bracket 45/55 quando ambos vierem
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
                        entry_actual = float(avg)
                    except Exception:
                        pass
                source = "auto"

                # Captura IDs das ordens condicionais pro trade manager (Fase 2)
                sl_oid = order_res.get("sl_order_id")
                tp1_oid = order_res.get("tp1_order_id")
                tp2_oid = order_res.get("tp2_order_id")
                if not order_res.get("sl_ok"):
                    log.error(
                        f"[shadow→live] ⚠ {rec['symbol']} ABERTO SEM STOP — "
                        f"posição precisa atenção manual"
                    )
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
                notes=f"{source} auto-open (tier {tier})",
                sl_order_id=_sl_oid,
                tp1_order_id=_tp1_oid,
                tp2_order_id=_tp2_oid,
                sl_current_price=stop,
            )
            if trade is not None:
                opened += 1
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
