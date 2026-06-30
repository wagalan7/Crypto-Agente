"""
Orquestrador do BACKTEST MASSIVO (roda SÓ no DEV — gated por env no endpoint).

Itera o universo amplo (top-N perps por volume) × TFs, roda o backtest histórico
COMPLETO (desde a listagem, via data-api.binance.vision — paginação real, com MTF
e funding histórico) reusando `recommendation_backtest.backtest_symbol_tf` (mesma
simulação de outcome de produção: TP1/BE/trail/time-stop), e PERSISTE a edge por
moeda em `symbol_backtest_stats`.

Objetivo: gerar OFFLINE a amostra que a allowlist do PRD nunca consegue (pega-22:
moeda fora do universo nunca executa → nunca acumula amostra → rotação nunca
promove). O ranking daqui vira candidata à allowlist (revisão humana antes de subir).

Resumível: pula (symbol, tf) já computado dentro de `refresh_days`. Job longo
sobrevive a restart do dyno — basta re-chamar /start que ele continua de onde parou.
Sequencial (concorrência baixa) pra não estourar memória no dyno do DEV.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# Timeout por (símbolo, tf): sem isso, um fetch de dados pendurado (ex.: DEV
# geobloqueado no fapi caindo num fallback que trava) congela o sweep inteiro
# pra sempre — não lança exceção, só nunca retorna. Com timeout, o item estoura,
# conta como erro e o sweep segue. Default 600s: o 1h sobre histórico completo
# (~78k candles dos majors) leva minutos legítimos; 180s cortava BTC/ETH/SOL e
# gerava buracos nos dados. 600s cobre o pior caso sem deixar hang de verdade vivo.
_SYMBOL_TIMEOUT_S = float(os.getenv("BT_UNIVERSE_SYMBOL_TIMEOUT_S", "600"))

# Persiste cada trade simulado (resolvido) em backtest_trades, pra o cérebro
# poder aprender com o histórico inteiro do universo (calibração blended). É só
# dado de pesquisa — o scoring real só consome se CALIBRATION_INCLUDE_BACKTEST=on.
PERSIST_BACKTEST_TRADES = os.getenv("BACKTEST_PERSIST_TRADES", "true").strip().lower() in ("1", "true", "yes")
# Statuses resolvidos que viram linha de aprendizado (no_data é descartado).
_RESOLVED_BT_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2", "lost", "expired")

WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
# 2017-01-01: load_historical_ohlcv retorna só o que existe desde a listagem,
# então pedir desde aqui == "histórico completo de cada moeda".
_FULL_HISTORY_START = datetime(2017, 1, 1, tzinfo=timezone.utc)

# Allowlist EFETIVA do PRD (100 bases — rotação FASE 2, snapshot 2026-06-21).
# Usada no modo "outside": enumera o universo amplo, REMOVE estas, e backtesta
# as top-N que sobram — exatamente as candidatas a promover pra allowlist.
# DEV roda com allowlist própria (372), então não dá pra inferir o universo do
# PRD de dentro do DEV; por isso a lista vem fixada aqui (revisar quando a
# rotação do PRD mudar de forma relevante).
PRD_ALLOWLIST_BASES = frozenset({
    "AAVE", "ADA", "AERO", "AI", "ALGO", "ALLO", "APT", "ARB", "ASTER", "ASTR",
    "ATH", "ATOM", "AVAX", "BABY", "BCH", "BERA", "BLUR", "BNB", "BONK", "BSB",
    "BTC", "CFX", "CHZ", "CRV", "DASH", "DEXE", "DOGE", "DOT", "DYDX", "EIGEN",
    "ENA", "EPIC", "ETH", "ETHFI", "FARTCOIN", "FET", "FIDA", "FIL", "GALA",
    "GPS", "HBAR", "HMSTR", "HOME", "HYPE", "ICP", "ID", "INJ", "JTO", "JUP",
    "KAT", "LDO", "LINEA", "LINK", "LTC", "MEW", "MON", "NEAR", "NOT", "ONDO",
    "OP", "OPN", "ORDI", "PAXG", "PENDLE", "PENGU", "PEPE", "PIPPIN", "PUMP",
    "PYTH", "RENDER", "RUNE", "SAHARA", "SAND", "SEI", "SOL", "SPX", "STG",
    "STRK", "SUI", "SXT", "TAO", "TIA", "TON", "TRUMP", "TRX", "TURBO", "U",
    "UNI", "UTK", "VIRTUAL", "WIF", "WLD", "XAUT", "XLM", "XMR", "XPL", "XRP",
    "ZEC", "ZIL", "ZRO",
})

# --- CALIBRAÇÃO backtest×live (Fase 1, informativa/não-gating) -------------
# O backtest é OTIMISTA (sem book/funding/slippage reais). Pra traduzir o
# `wf_avg_r` (out-of-sample) num "edge calibrado" mais perto do que o bot
# realiza ao vivo, aplicamos um fator de desconto global.
#
# Derivação (2026-06-22): cruzei o backtest 4h (DEV) com o desempenho REAL do
# bot (PRD /api/rotation/symbol-stats, avg_r por símbolo) nas moedas que têm os
# DOIS lados e amostra viva ≥6 trades. Overlap = 6 majors (todas grade A no
# backtest, todas R+ ao vivo → sinal de sinal 6/6):
#   BTC 0.33 · ETH 0.91 · SOL 0.57 · TAO 0.86 · WLD 0.43 · XRP 1.16
#   mediana ratio live/bt_avg = 0.72 ; live/bt_wf = 0.79
# Variância alta por moeda (amostras vivas de 6–12 trades) → usa-se SÓ fator
# GLOBAL, nunca por moeda, e conservador (abaixo da mediana p/ margem):
CALIBRATION_FACTOR = 0.70
CALIBRATION_META = {
    "factor": CALIBRATION_FACTOR,
    "derived_at": "2026-06-22",
    "method": "ratio live_avg_r / backtest_wf_avg_r, mediana de 6 majors com "
              "amostra viva >=6 trades (BTC/ETH/SOL/TAO/WLD/XRP)",
    "overlap_n": 6,
    "ratio_median_avg": 0.72,
    "ratio_median_wf": 0.79,
    "sign_agreement": "6/6 grade-A do backtest deram R+ ao vivo",
    "uso": "informativo/screening/teto de sizing; NUNCA como EV isolado",
}


def calibrated_edge(wf_avg_r) -> float | None:
    """Edge calibrado = wf_avg_r × fator de desconto global (Fase 1).
    Aproxima o R que o bot tende a realizar ao vivo. None se sem wf."""
    if wf_avg_r is None:
        return None
    try:
        return round(float(wf_avg_r) * CALIBRATION_FACTOR, 3)
    except Exception:
        return None


def _norm_base(symbol_or_base: str) -> str:
    """Base normalizada p/ casar perp↔spot na exclusão da allowlist.
    Tira sufixo de quote E o prefixo de multiplicador '1000' (1000PEPE→PEPE,
    1000SATS→SATS) — assim a base do perp do PRD bate com a base do spot/vision."""
    try:
        from services.shadow_trade_service import _symbol_base
        b = _symbol_base(symbol_or_base)
    except Exception:
        b = (symbol_or_base or "").upper().split("/", 1)[0]
    if b.startswith("1000") and len(b) > 4:
        b = b[4:]
    return b


async def _perp_tradeable_bases() -> tuple:
    """(set, source): bases com perp USDT ativo + origem ('live'/'snapshot'/'none').
    Wrapper tolerante: qualquer falha vira (None, 'none')."""
    try:
        from services.binance_futures_service import (
            fetch_perp_tradeable_bases, perp_bases_source,
        )
        bases = await fetch_perp_tradeable_bases()
        return bases, perp_bases_source()
    except Exception as e:
        log.warning(f"[bt-universe] perp-tradeable indisponível: {e}")
        return None, "none"

# Estado de progresso em memória (lido pelo endpoint /status). Reseta no redeploy,
# mas os RESULTADOS ficam no DB — o /start retoma de onde parou.
_PROGRESS: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "computed": 0,
    "skipped": 0,
    "errors": 0,
    "current": None,
    "tfs": [],
    "limit": 0,
    "mode": "top",
    "pool": 0,
    "excluded": 0,
    "offset": 0,
}


def get_universe_status() -> dict:
    return dict(_PROGRESS)


# ── Progresso PERSISTIDO (cross-process: worker escreve, web lê) ──────────────
import time as _time  # noqa: E402

_last_persist_mono: float = 0.0
_PERSIST_MIN_INTERVAL_S = 3.0


async def _persist_progress(force: bool = False) -> None:
    """Salva o snapshot do _PROGRESS na linha singleton (id=1). Throttled por
    tempo (a não ser force=True no start/finish) pra não martelar o DB durante
    rajadas de skip. Fail-soft: nunca derruba o sweep."""
    global _last_persist_mono
    from db import DB_ENABLED
    if not DB_ENABLED:
        return
    now_mono = _time.monotonic()
    if not force and (now_mono - _last_persist_mono) < _PERSIST_MIN_INTERVAL_S:
        return
    _last_persist_mono = now_mono
    try:
        from db import get_session
        from models.sweep_progress import SweepProgress
        from sqlalchemy import select
        snap = dict(_PROGRESS)
        async with get_session() as session:
            row = (await session.execute(
                select(SweepProgress).where(SweepProgress.id == 1)
            )).scalar_one_or_none()
            if row is None:
                session.add(SweepProgress(id=1, progress=snap,
                                          updated_at=datetime.now(timezone.utc)))
            else:
                row.progress = snap
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()
    except Exception as e:
        log.warning(f"[bt-universe] persist progress falhou: {e}")


async def get_universe_status_db() -> dict:
    """Status pra API: prefere o progresso PERSISTIDO (worker), cai pro
    in-memory (caso o sweep rode no próprio processo web, legado)."""
    from db import DB_ENABLED
    mem = dict(_PROGRESS)
    if not DB_ENABLED:
        return mem
    try:
        from db import get_session
        from models.sweep_progress import SweepProgress
        from sqlalchemy import select
        async with get_session() as session:
            row = (await session.execute(
                select(SweepProgress).where(SweepProgress.id == 1)
            )).scalar_one_or_none()
        if row and isinstance(row.progress, dict) and row.progress:
            db_prog = dict(row.progress)
            db_prog["_persisted_at"] = row.updated_at.isoformat() if row.updated_at else None
            # Se o web nunca rodou sweep (total=0) mas o DB tem dados, usa o DB.
            # Se ambos têm dados, o DB (worker) é a fonte da verdade na Opção B.
            return db_prog
        return mem
    except Exception as e:
        log.warning(f"[bt-universe] read persisted progress falhou: {e}")
        return mem


def _metrics_from_trades(trades: list) -> Optional[dict]:
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t.get("status") in WIN_STATUSES)
    losses = sum(1 for t in trades if t.get("status") == "lost")
    expired = sum(1 for t in trades if t.get("status") == "expired")
    decided = wins + losses
    rv = [float(t.get("realized_r") or 0) for t in trades]
    r_wins = [r for r in rv if r > 0]
    r_loss_abs = [abs(r) for r in rv if r < 0]
    pf = (sum(r_wins) / sum(r_loss_abs)) if r_loss_abs else None
    # Walk-forward proxy: a edge PERSISTE na metade mais recente dos trades?
    # (out-of-sample barato; o número em que confiar pra promover). Walk-forward
    # formal (run_walkforward) fica pra upgrade.
    ordered = sorted(trades, key=lambda t: t.get("created_ts") or 0)
    recent = ordered[len(ordered) // 2:]
    wf_rv = [float(t.get("realized_r") or 0) for t in recent]
    return {
        "n_trades": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "wr_pct": round(wins / n * 100, 1),
        "wr_clean_pct": round(wins / decided * 100, 1) if decided else None,
        "expiry_pct": round(expired / n * 100, 1),
        "avg_r": round(sum(rv) / n, 3),
        "total_r": round(sum(rv), 2),
        "profit_factor": round(pf, 2) if pf is not None else None,
        "wf_avg_r": round(sum(wf_rv) / len(wf_rv), 3) if wf_rv else None,
        "wf_n_trades": len(wf_rv),
    }


async def _upsert_stats(symbol: str, tf: str, res: dict) -> None:
    """UPSERT de uma linha (symbol, tf) em symbol_backtest_stats."""
    from db import get_session
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select

    trades = res.get("trades", [])
    candles = int(res.get("candles") or 0)
    error = res.get("error")
    m = _metrics_from_trades(trades) or {}

    first_ts = last_ts = None
    if trades:
        ordered = sorted(trades, key=lambda t: t.get("created_ts") or 0)
        try:
            first_ts = datetime.fromtimestamp(ordered[0]["created_ts"] / 1000, tz=timezone.utc)
            last_ts = datetime.fromtimestamp(ordered[-1]["created_ts"] / 1000, tz=timezone.utc)
        except Exception:
            pass

    async with get_session() as session:
        row = (await session.execute(
            select(SymbolBacktestStats).where(
                SymbolBacktestStats.symbol == symbol,
                SymbolBacktestStats.timeframe == tf,
            )
        )).scalar_one_or_none()
        if row is None:
            row = SymbolBacktestStats(symbol=symbol, timeframe=tf)
            session.add(row)
        row.candles = candles
        row.first_ts = first_ts
        row.last_ts = last_ts
        row.full_history = True
        row.n_trades = m.get("n_trades", 0)
        row.wins = m.get("wins", 0)
        row.losses = m.get("losses", 0)
        row.expired = m.get("expired", 0)
        row.wr_pct = m.get("wr_pct")
        row.wr_clean_pct = m.get("wr_clean_pct")
        row.expiry_pct = m.get("expiry_pct")
        row.avg_r = m.get("avg_r")
        row.total_r = m.get("total_r")
        row.profit_factor = m.get("profit_factor")
        row.wf_avg_r = m.get("wf_avg_r")
        row.wf_n_trades = m.get("wf_n_trades")
        row.error = (error or None) if not trades else None
        row.computed_at = datetime.now(timezone.utc)
        await session.commit()


async def _persist_trades(symbol: str, tf: str, trades: list) -> int:
    """DELETE+INSERT dos trades RESOLVIDOS de (symbol, tf) em backtest_trades.
    Idempotente (re-rodar substitui as linhas da moeda). Fail-soft: nunca derruba
    o sweep — só loga. Retorna quantas linhas foram gravadas."""
    if not PERSIST_BACKTEST_TRADES:
        return 0
    from db import get_session
    from models.backtest_trade import BacktestTrade
    from sqlalchemy import delete

    rows = []
    for t in (trades or []):
        st = t.get("status")
        if st not in _RESOLVED_BT_STATUSES:
            continue
        bar_ts = None
        hour_utc = dow = None
        cts = t.get("created_ts")
        if cts:
            try:
                bar_ts = datetime.fromtimestamp(int(cts) / 1000, tz=timezone.utc)
                hour_utc = bar_ts.hour
                dow = bar_ts.weekday()
            except Exception:
                pass
        rows.append(BacktestTrade(
            symbol=symbol, timeframe=tf,
            tier=str(t.get("tier") or "")[:4],
            direction=str(t.get("direction") or "")[:8],
            score=float(t.get("score") or 0.0),
            rr=t.get("rr"),
            atr_pct=t.get("atr_pct"),
            status=st,
            realized_r=t.get("realized_r"),
            hour_utc=hour_utc, dow=dow,
            patterns=t.get("patterns") or None,
            bar_ts=bar_ts,
        ))
    try:
        async with get_session() as session:
            await session.execute(
                delete(BacktestTrade).where(
                    BacktestTrade.symbol == symbol,
                    BacktestTrade.timeframe == tf,
                )
            )
            if rows:
                session.add_all(rows)
            await session.commit()
        return len(rows)
    except Exception as e:
        log.warning(f"[bt-universe] persist trades {symbol} {tf} falhou: {e}")
        return 0


async def _has_backtest_trades(symbol: str, tf: str) -> bool:
    """True se já há linhas por-trade gravadas pra (symbol, tf)."""
    from db import get_session
    from models.backtest_trade import BacktestTrade
    from sqlalchemy import select, func
    try:
        async with get_session() as session:
            n = (await session.execute(
                select(func.count()).select_from(BacktestTrade).where(
                    BacktestTrade.symbol == symbol,
                    BacktestTrade.timeframe == tf,
                )
            )).scalar()
        return bool(n and n > 0)
    except Exception:
        # Tabela pode não existir ainda no 1º deploy — trata como "sem trades".
        return False


async def _load_pairs_with_trades() -> set:
    """Conjunto de (symbol, tf) que JÁ têm linhas em backtest_trades — em UMA query.
    Usado pelo pré-load do resume p/ não fazer 1 round-trip por job."""
    from db import get_session
    from models.backtest_trade import BacktestTrade
    from sqlalchemy import select
    try:
        async with get_session() as session:
            rows = (await session.execute(
                select(BacktestTrade.symbol, BacktestTrade.timeframe).distinct()
            )).all()
        return {(s, tf) for s, tf in rows}
    except Exception:
        return set()


async def _load_fresh_pairs(symbols: set, tfs: list[str], refresh_days: int) -> set:
    """Pré-carrega, em 1-2 queries, o conjunto de (symbol, tf) que JÁ estão frescos
    E completos (não precisam re-rodar). Espelha a lógica de `_already_fresh`, mas
    em LOTE.

    Motivo: a cada redeploy o `_PROGRESS` zera (in-memory) e o sweep re-anda TODOS
    os jobs chamando `_already_fresh` 1-a-1 (1 query por job) — lento e a barra
    "reinicia do zero". Com o set pré-carregado dá pra (a) semear done/skipped na
    posição REAL do resume e (b) pular o já-feito sem round-trip por job.

    Fail-soft: em qualquer erro devolve set() → cai no caminho per-job legado."""
    from db import get_session, DB_ENABLED
    if not DB_ENABLED:
        return set()
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select
    cutoff = datetime.now(timezone.utc) - timedelta(days=refresh_days)
    tfset = set(tfs)
    want = set(symbols)
    fresh: set = set()
    zero_trade_fresh: set = set()  # frescos com n_trades==0 → nada p/ backfill
    try:
        async with get_session() as session:
            rows = (await session.execute(
                select(
                    SymbolBacktestStats.symbol,
                    SymbolBacktestStats.timeframe,
                    SymbolBacktestStats.computed_at,
                    SymbolBacktestStats.error,
                    SymbolBacktestStats.n_trades,
                )
            )).all()
        for sym, tf, computed_at, error, n_trades in rows:
            if sym not in want or tf not in tfset:
                continue
            if computed_at is None or computed_at < cutoff:
                continue
            if error and not n_trades:
                continue
            if not n_trades:
                zero_trade_fresh.add((sym, tf))
            fresh.add((sym, tf))
        # Frescos com trades>0 precisam ter linhas em backtest_trades; senão re-roda
        # (espelha o guard de `_already_fresh` linha ~406).
        if PERSIST_BACKTEST_TRADES and (fresh - zero_trade_fresh):
            have_trades = await _load_pairs_with_trades()
            fresh = zero_trade_fresh | (fresh & have_trades)
    except Exception as e:
        log.warning(f"[bt-universe] preload fresh falhou ({e}); fallback per-job")
        return set()
    return fresh


async def _already_fresh(symbol: str, tf: str, refresh_days: int) -> bool:
    """True se já existe resultado de (symbol, tf) com sucesso dentro da janela."""
    from db import get_session
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select

    cutoff = datetime.now(timezone.utc) - timedelta(days=refresh_days)
    async with get_session() as session:
        row = (await session.execute(
            select(SymbolBacktestStats).where(
                SymbolBacktestStats.symbol == symbol,
                SymbolBacktestStats.timeframe == tf,
            )
        )).scalar_one_or_none()
    # Re-tenta se não existe, se está velho, OU se da última vez deu erro/0 trade.
    if row is None or row.computed_at is None:
        return False
    if row.computed_at < cutoff:
        return False
    if row.error and row.n_trades == 0:
        return False
    # Captura por-trade ligada mas ainda sem linhas gravadas (ex.: stats fresco de
    # um sweep anterior à feature) ⇒ re-roda pra preencher backtest_trades.
    if PERSIST_BACKTEST_TRADES and row.n_trades and not await _has_backtest_trades(symbol, tf):
        return False
    return True


async def run_universe_backtest(
    tfs: list[str],
    limit: int = 200,
    refresh_days: int = 7,
    step_bars: int = 1,
    exclude_bases: Optional[frozenset] = None,
    outside_n: Optional[int] = None,
    outside_offset: int = 0,
    perp_universe: bool = False,
    order_by: str = "volume",
    history_start: Optional[datetime] = None,
    include_only_bases: Optional[frozenset] = None,
) -> dict:
    """Job de background: backtest full-history de top-`limit` perps × `tfs`.
    Idempotente/resumível. Atualiza _PROGRESS. Retorna resumo final.

    Modo "fora da allowlist" (para descobrir o que PROMOVER):
      • `exclude_bases`: bases a REMOVER do universo (ex.: PRD_ALLOWLIST_BASES).
      • `outside_offset`: pula as primeiras N que sobraram (leva 2 = offset 150).
      • `outside_n`: depois de pular, fica só com as próximas N.
      Use limit alto (pool, ex.: 500) p/ ter moedas suficientes após o filtro.

    Modo "perp_universe" (varrer TODOS os perps restantes):
      • Enumera TODAS as bases com perp USDT ativo (live/snapshot), REMOVE
        `exclude_bases` (allowlist) e roda TODAS — sem outside_n/offset. Ordena
        por volume spot (liquidez primeiro) e a cauda longa em ordem alfabética.
        As já backtestadas são puladas pelo skip-fresh (resumível). É o "rodar o
        resto que é perp e não tava na lista nem na allowlist"."""
    if _PROGRESS.get("running"):
        return {"ok": False, "error": "job já em execução", "progress": get_universe_status()}

    # Enumera o universo na MESMA fonte que o loader de dados consome (evita
    # mismatch de símbolo). Espelha o load_historical_ohlcv:
    #   • COM proxy (PRD): Binance Futures via proxy de egress (universo tradável).
    #   • SEM proxy (DEV): Binance Spot via data-api.binance.vision (público, sem
    #     geobloqueio, e com histórico mais longo desde a listagem). Spot é proxy
    #     fiel do price-action histórico pro estudo offline.
    from services import binance_futures_service as _bfs
    from services.recommendation_backtest import backtest_symbol_tf, VISION_BULK

    end_dt = datetime.now(timezone.utc)
    try:
        if VISION_BULK:
            # Enumeração PROXY-FREE via listing S3 do arquivo data.binance.vision
            # (o exchangeInfo via fapi/proxy está 418-banido — e o proxy é o mesmo
            # IP do bot live, então NÃO dá pra arriscar). Símbolos EXATOS (mantém
            # prefixo 1000) p/ casar com o arquivo do vision E com o símbolo já
            # gravado em stats (dedup do skip-fresh). O slice por histórico/offset
            # (abaixo) faz o lotemento.
            from services.binance_vision_service import fetch_perp_symbols_vision
            symbols = await fetch_perp_symbols_vision()
        elif _bfs.PROXY_ENABLED:
            from services.binance_futures_service import fetch_top_volume_symbols
            symbols = await fetch_top_volume_symbols(limit=limit)
        else:
            from services.binance_vision_service import fetch_top_volume_symbols
            symbols = await fetch_top_volume_symbols(limit=limit)
    except Exception as e:
        log.error(f"[bt-universe] falha ao enumerar símbolos: {e}")
        # Registra no _PROGRESS pra o /status mostrar o motivo (senão fica "total:0"
        # mudo e parece que a task nem rodou).
        _PROGRESS.update({
            "running": False,
            "current": f"ERRO enumerar símbolos: {e}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": False, "error": f"enumerar símbolos: {e}"}

    pool_n = len(symbols)
    excluded_n = 0
    mode = "top"
    off = max(0, int(outside_offset or 0))

    # Ordenação por HISTÓRICO (listagem mais antiga primeiro = mais dados/confiança).
    # Busca o mapa {base: onboardDate} via exchangeInfo; quem não tiver data vai pro
    # fim. Se o fetch falhar (mapa vazio), mantém a ordem padrão (volume).
    onboard_map: dict = {}
    if order_by == "history":
        try:
            if VISION_BULK:
                # Datas de listagem PROXY-FREE: mês mais antigo no arquivo vision
                # (o onboard via exchangeInfo/proxy também está banido).
                from services.binance_vision_service import fetch_perp_onboard_dates_vision
                onboard_map = await fetch_perp_onboard_dates_vision(symbols)
            else:
                onboard_map = await _bfs.fetch_perp_onboard_dates()
        except Exception as e:
            log.warning(f"[bt-universe] onboard dates indisponíveis ({e}); ordem por volume")

    def _hist_sorted(syms):
        if not onboard_map:
            return syms
        return sorted(syms, key=lambda s: onboard_map.get(_norm_base(s), 1 << 62))

    if include_only_bases:
        # Modo "refino": roda SÓ as bases informadas (ex.: a allowlist do PRD),
        # tipicamente com step_bars=1 + histórico longo — afina os níveis das
        # moedas que o bot REALMENTE opera. Inverso do modo "outside". Ordena por
        # histórico (listagem antiga primeiro = mais dados). O skip-fresh torna
        # idempotente/resumível igual aos outros modos.
        mode = "refino"
        inc = {_norm_base(b) for b in include_only_bases}
        kept = [s for s in symbols if _norm_base(s) in inc]
        excluded_n = pool_n - len(kept)
        symbols = _hist_sorted(kept)
    elif perp_universe:
        # Universo = TODAS as bases com perp ativo, menos a allowlist. Ordena por
        # volume spot (as que já vieram em `symbols`), depois cauda alfabética.
        mode = "perp_universe"
        perp = await _perp_tradeable_bases()
        perp = perp[0] if isinstance(perp, tuple) else perp
        perp = perp or set()
        ex = {_norm_base(b) for b in (exclude_bases or frozenset())}
        ordered, seen = [], set()
        for s in symbols:  # spot-volume desc → liquidez primeiro
            b = _norm_base(s)
            if b in perp and b not in ex and b not in seen:
                ordered.append(s); seen.add(b)
        for b in sorted(perp - ex - seen):  # cauda longa (perp fora do top spot)
            ordered.append(f"{b}/USDT:USDT"); seen.add(b)
        pool_n = len(perp)
        excluded_n = len([b for b in perp if b in ex])
        symbols = _hist_sorted(ordered)
    elif exclude_bases:
        mode = "outside"
        ex = {_norm_base(b) for b in exclude_bases}
        kept = [s for s in symbols if _norm_base(s) not in ex]
        excluded_n = pool_n - len(kept)
        kept = _hist_sorted(kept)  # ordena ANTES do offset/slice → lotes por histórico
        symbols = kept
        if off:
            symbols = symbols[off:]
        if outside_n is not None and outside_n > 0:
            symbols = symbols[:outside_n]

    # ── Pré-load do JÁ-FEITO (resumível sem re-andar 1-a-1) ───────────────────
    # A cada redeploy o _PROGRESS zera. Antes o sweep re-caminhava TODOS os jobs
    # chamando _already_fresh 1-a-1 (1 query/job) → a barra "reiniciava do zero" e
    # demorava ~minutos só pra re-pular o já-feito. Agora carregamos o conjunto de
    # (sym,tf) frescos numa única passada e:
    #   • SEMEAMOS done/skipped na posição REAL (a barra reflete o resume, não 0/N);
    #   • só ITERAMOS os jobs PENDENTES (pula o já-feito sem round-trip por job).
    all_jobs = [(sym, tf) for sym in symbols for tf in tfs]
    total_jobs = len(all_jobs)
    fresh_set = await _load_fresh_pairs(set(symbols), list(tfs), refresh_days)
    pending = [(sym, tf) for (sym, tf) in all_jobs if (sym, tf) not in fresh_set]
    preskipped = total_jobs - len(pending)

    _PROGRESS.update({
        "running": True,
        "started_at": end_dt.isoformat(),
        "finished_at": None,
        "total": total_jobs,
        "done": preskipped,       # ← resume começa na posição real, não em 0
        "computed": 0,
        "skipped": preskipped,    # ← os já-frescos já contam como pulados
        "errors": 0,
        "current": None,
        "tfs": list(tfs),
        "limit": limit,
        "mode": mode,
        "pool": pool_n,
        "excluded": excluded_n,
        "offset": off,
    })
    log.info(f"[bt-universe] START mode={mode} pool={pool_n} excluded={excluded_n} "
             f"offset={off} → {len(symbols)} símbolos × {tfs} = {total_jobs} jobs "
             f"(pré-pulados {preskipped} frescos, pendentes {len(pending)})")
    await _persist_progress(force=True)

    # Início do histórico: override (sweep amplo usa janela curta p/ caber no
    # tempo/CPU) ou full-history (refino). Janela menor = menos barras carregadas
    # E menos iterações de scan → destrava as moedas 1h gigantes (ex.: 1000SHIB
    # tinha 45k barras em step_bars=1; o scan não terminava antes do restart).
    hist_start = history_start or _FULL_HISTORY_START

    try:
        for sym, tf in pending:
            if True:
                _PROGRESS["current"] = f"{sym} {tf}"
                # Persiste a moeda atual JÁ no início (não só após terminar) → o
                # painel mostra quem está sendo analisado e dá p/ ver onde trava.
                await _persist_progress(force=True)
                try:
                    if await _already_fresh(sym, tf, refresh_days):
                        _PROGRESS["skipped"] += 1
                        # done é incrementado UMA vez no finally (antes contava +2
                        # nos pulados — o continue ainda executa o finally).
                        continue
                    res = await asyncio.wait_for(
                        backtest_symbol_tf(
                            sym, tf, hist_start, end_dt, step_bars=step_bars
                        ),
                        timeout=_SYMBOL_TIMEOUT_S,
                    )
                    await _upsert_stats(sym, tf, res)
                    # Persiste trades resolvidos pro aprendizado blended (fail-soft).
                    await _persist_trades(sym, tf, res.get("trades", []))
                    if res.get("error") and not res.get("trades"):
                        _PROGRESS["errors"] += 1
                    else:
                        _PROGRESS["computed"] += 1
                except Exception as e:
                    log.warning(f"[bt-universe] {sym} {tf} crash: {e}")
                    _PROGRESS["errors"] += 1
                    # Persiste o motivo do crash pra aparecer no ranking/status
                    # (senão fica invisível — só conta no contador de erros).
                    try:
                        await _upsert_stats(sym, tf, {
                            "trades": [], "candles": 0,
                            "error": f"crash: {type(e).__name__}: {e}"[:250],
                        })
                    except Exception:
                        pass
                finally:
                    _PROGRESS["done"] += 1
                # Snapshot do progresso pro DB (throttled) — visível cross-process
                # (worker grava, web lê via get_universe_status_db).
                await _persist_progress()
                # Cede o loop pra não monopolizar o event loop do web dyno.
                await asyncio.sleep(0)
    finally:
        _PROGRESS["running"] = False
        _PROGRESS["finished_at"] = datetime.now(timezone.utc).isoformat()
        _PROGRESS["current"] = None
        log.info(f"[bt-universe] FIM — {_PROGRESS['computed']} computados, "
                 f"{_PROGRESS['skipped']} pulados, {_PROGRESS['errors']} erros")
        await _persist_progress(force=True)

    return {"ok": True, "progress": get_universe_status()}


def _classify_bt_error(err: str) -> str:
    """Agrupa o texto cru do erro do backtest numa categoria diagnóstica."""
    e = (err or "").lower()
    if "insuficiente candles" in e or "insufficient" in e:
        return "candles_insuficientes"   # histórico curto OU fetch voltou vazio
    if "rate-limit" in e or "ban" in e or "418" in e or "429" in e:
        return "rate_limit_ban"          # egress estrangulado/banido
    if "timeout" in e or "timed out" in e:
        return "timeout_fetch"           # proxy pendurou
    if e.startswith("crash:"):
        return "crash"                   # exceção não tratada na varredura
    if "451" in e or "403" in e or "geobloq" in e:
        return "geobloqueio"
    return "outro"


async def get_error_breakdown(limit_samples: int = 12) -> dict:
    """Diagnóstico dos erros do sweep: lê symbol_backtest_stats onde error != null,
    classifica por categoria (candles_insuficientes / rate_limit_ban / timeout_fetch
    / crash / geobloqueio / outro) e devolve contagem + amostras. Responde a pergunta
    'os 311 erros são histórico curto legítimo OU fetch falhando?'. Leitura pura."""
    from db import DB_ENABLED, get_session
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select

    if not DB_ENABLED:
        return {"enabled": False}

    async with get_session() as session:
        rows = (await session.execute(
            select(SymbolBacktestStats)
            .where(SymbolBacktestStats.error.is_not(None))
            .order_by(SymbolBacktestStats.computed_at.desc().nullslast())
        )).scalars().all()

    by_cat: dict = {}
    samples: dict = {}
    for r in rows:
        cat = _classify_bt_error(r.error or "")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        bucket = samples.setdefault(cat, [])
        if len(bucket) < limit_samples:
            bucket.append({
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "candles": r.candles,
                "error": (r.error or "")[:200],
                "computed_at": r.computed_at.isoformat() if getattr(r, "computed_at", None) else None,
            })

    return {
        "enabled": True,
        "total_errors": len(rows),
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "samples": samples,
        "legenda": {
            "candles_insuficientes": "histórico curto legítimo OU fetch voltou vazio (ver 'candles': 0 = fetch falhou; >0 = realmente curto)",
            "rate_limit_ban": "egress estrangulado/banido pela Binance",
            "timeout_fetch": "proxy pendurou além do timeout",
            "crash": "exceção não tratada na varredura",
            "geobloqueio": "IP bloqueado (451/403)",
            "outro": "não classificado",
        },
    }


async def get_ranking(
    tf: Optional[str] = None,
    min_trades: int = 30,
    sort: str = "wf_avg_r",
    limit: int = 100,
) -> dict:
    """Ranking das moedas por edge (default: walk-forward avg_R out-of-sample).
    Marca quais já estão na allowlist efetiva e sugere candidatas (fora + edge
    forte + expiry baixo). Leitura pura."""
    from db import DB_ENABLED, get_session
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select
    if not DB_ENABLED:
        return {"enabled": False}

    sort_col = {
        "wf_avg_r": SymbolBacktestStats.wf_avg_r,
        "avg_r": SymbolBacktestStats.avg_r,
        "total_r": SymbolBacktestStats.total_r,
        "wr_clean_pct": SymbolBacktestStats.wr_clean_pct,
    }.get(sort, SymbolBacktestStats.wf_avg_r)

    conds = [SymbolBacktestStats.n_trades >= min_trades]
    if tf:
        conds.append(SymbolBacktestStats.timeframe == tf)

    async with get_session() as session:
        rows = (await session.execute(
            select(SymbolBacktestStats).where(*conds)
            .order_by(sort_col.desc().nullslast())
            .limit(limit)
        )).scalars().all()

    perp_bases, perp_src = await _perp_tradeable_bases()

    ranking = []
    candidates = []
    for r in rows:
        # Casa contra a allowlist do PRD (a que importa pra "o que incluir"),
        # normalizando 1000X → X. DEV roda allowlist própria, então usar a do PRD.
        base = _norm_base(r.symbol) if r.symbol else r.symbol
        in_allow = base in PRD_ALLOWLIST_BASES
        # perp_tradeable: True/False se temos o set; None se não deu p/ checar.
        tradeable = (base in perp_bases) if perp_bases else None
        d = r.to_dict()
        d["base"] = base
        d["in_allowlist"] = in_allow
        d["perp_tradeable"] = tradeable
        d["calibrated_avg_r"] = calibrated_edge(r.wf_avg_r)
        ranking.append(d)
        # Candidata: FORA da allowlist, edge out-of-sample positiva e decente,
        # amostra ok, não dominada por expiry (trades-zumbi) E com perp negociável
        # (None = desconhecido NÃO desqualifica; só False — delistado/só-spot — sai).
        if (not in_allow and tradeable is not False
                and (r.wf_avg_r or 0) > 0.10
                and (r.avg_r or 0) > 0.10
                and (r.expiry_pct or 100) < 35):
            candidates.append(d)

    return {
        "enabled": True,
        "sort": sort,
        "tf": tf,
        "min_trades": min_trades,
        "allowlist_size": len(PRD_ALLOWLIST_BASES),
        "perp_check": perp_src if perp_bases else "indisponivel",
        "calibration": CALIBRATION_META,
        "n": len(ranking),
        "ranking": ranking,
        "candidates_to_promote": candidates,
        "nota": "candidatas = fora da allowlist PRD + perp negociável + wf_avg_r>0.10 "
                "+ avg_r>0.10 + expiry<35%. perp_tradeable=False (delistado/só-spot) "
                "sai; None = não deu p/ checar (não desqualifica). Backtest é "
                "PRÉ-FILTRO; veredito final = shadow+rotação ao vivo.",
    }


# ── Grade de edge (A/B/C/D) p/ consumo do APP (não só do bot) ──────────────────
# Traduz as métricas cruas numa nota legível pro usuário final. Conservador de
# propósito: o backtest é otimista (sem book/derivativos reais), então exige
# amostra + walk-forward positivo + expiry controlado pra dar nota alta.
def _grade_edge(r) -> tuple[str, str]:
    """Retorna (grade, motivo) a partir de uma linha SymbolBacktestStats."""
    n = r.n_trades or 0
    wf = r.wf_avg_r
    avg = r.avg_r or 0
    exp = r.expiry_pct if r.expiry_pct is not None else 100
    pf = r.profit_factor
    if n < 20:
        return "D", "amostra pequena (<20 trades)"
    if wf is None:
        return "D", "sem walk-forward"
    if exp >= 55:
        return "D", f"dominado por expiry ({exp:.0f}%)"
    # A: edge forte e PERSISTENTE out-of-sample, amostra sólida, expiry baixo.
    if wf >= 0.20 and avg >= 0.20 and n >= 40 and exp < 35 and (pf is None or pf >= 1.4):
        return "A", "edge forte e persistente (OOS)"
    if wf >= 0.10 and avg >= 0.10 and exp < 45:
        return "B", "edge positiva e razoável"
    if wf >= 0.0 and avg >= 0.0:
        return "C", "marginal / instável"
    return "D", "edge negativa no backtest"


async def get_insights(
    tf: str = "4h",
    min_trades: int = 20,
    limit: int = 300,
) -> dict:
    """Camada de APRENDIZADO PRO APP (não só pro bot): por moeda, traduz o
    backtest histórico numa leitura legível pro usuário (grade A–D, headline
    metrics, badges, span de histórico) + um sumário do universo. Leitura pura.

    Diferente de /ranking (foco operacional em candidatas a promover), aqui o
    foco é o INSIGHT: 'esta moeda tem edge provada? quão forte? há quanto tempo?'."""
    from db import DB_ENABLED, get_session
    from models.symbol_backtest_stats import SymbolBacktestStats
    from sqlalchemy import select
    if not DB_ENABLED:
        return {"enabled": False}

    conds = [SymbolBacktestStats.n_trades >= min_trades]
    if tf:
        conds.append(SymbolBacktestStats.timeframe == tf)

    async with get_session() as session:
        rows = (await session.execute(
            select(SymbolBacktestStats).where(*conds)
            .order_by(SymbolBacktestStats.wf_avg_r.desc().nullslast())
            .limit(limit)
        )).scalars().all()

    perp_bases, perp_src = await _perp_tradeable_bases()

    coins = []
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    n_positive = 0
    for r in rows:
        base = _norm_base(r.symbol) if r.symbol else r.symbol
        in_allow = base in PRD_ALLOWLIST_BASES
        tradeable = (base in perp_bases) if perp_bases else None
        grade, reason = _grade_edge(r)
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        if (r.wf_avg_r or 0) > 0:
            n_positive += 1
        badges = []
        if in_allow:
            badges.append("na_allowlist")
        elif grade in ("A", "B") and tradeable is not False:
            badges.append("candidata")
        if tradeable is False:
            badges.append("sem_perp")
        if (r.profit_factor or 0) >= 2.0:
            badges.append("pf_alto")
        if (r.expiry_pct if r.expiry_pct is not None else 100) >= 50:
            badges.append("muito_expiry")
        # Span de histórico legível.
        span_days = None
        try:
            if r.first_ts and r.last_ts:
                span_days = (r.last_ts - r.first_ts).days
        except Exception:
            pass
        coins.append({
            "symbol": r.symbol,
            "base": base,
            "tf": r.timeframe,
            "grade": grade,
            "grade_reason": reason,
            "in_allowlist": in_allow,
            "perp_tradeable": tradeable,
            "badges": badges,
            # headline metrics (o que o card do app mostra)
            "avg_r": r.avg_r,
            "wf_avg_r": r.wf_avg_r,
            "calibrated_avg_r": calibrated_edge(r.wf_avg_r),
            "win_rate_pct": r.wr_clean_pct,
            "profit_factor": r.profit_factor,
            "expiry_pct": r.expiry_pct,
            "n_trades": r.n_trades,
            "total_r": r.total_r,
            "candles": r.candles,
            "history_days": span_days,
            "first_ts": r.first_ts.isoformat() if r.first_ts else None,
            "last_ts": r.last_ts.isoformat() if r.last_ts else None,
        })

    best = coins[0] if coins else None
    n_no_perp = sum(1 for c in coins if c["perp_tradeable"] is False)
    summary = {
        "n_coins": len(coins),
        "n_positive_edge": n_positive,
        "grade_counts": grade_counts,
        "best": {"base": best["base"], "grade": best["grade"],
                 "wf_avg_r": best["wf_avg_r"]} if best else None,
        "allowlist_size": len(PRD_ALLOWLIST_BASES),
        "perp_check": perp_src if perp_bases else "indisponivel",
        "n_sem_perp": n_no_perp,
        "calibration": CALIBRATION_META,
    }
    return {
        "enabled": True,
        "tf": tf,
        "min_trades": min_trades,
        "summary": summary,
        "coins": coins,
        "legenda": {
            "A": "edge forte e persistente (out-of-sample)",
            "B": "edge positiva e razoável",
            "C": "marginal / instável",
            "D": "fraca / amostra pequena / negativa",
            "sem_perp": "sem par perpétuo USDT ativo na Binance Futures (só-spot/"
                        "delistado/rebrandeado) — não promovível mesmo com edge",
            "calibrated_avg_r": "wf_avg_r × fator de desconto global (0.70) — "
                                "aproxima o R real que o bot tende a entregar ao "
                                "vivo (backtest é otimista). Calibrado em 6 majors "
                                "com os dois lados; uso só de screening, não EV.",
        },
        "nota": "Aprendizado a partir de backtest histórico (pré-filtro, otimista: "
                "sem book/derivativos reais). perp_tradeable cruza com o "
                "exchangeInfo de futures. Não é recomendação de compra; o "
                "veredito ao vivo vem do shadow+rotação.",
    }
