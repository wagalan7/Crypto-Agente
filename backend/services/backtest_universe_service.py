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
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
# 2017-01-01: load_historical_ohlcv retorna só o que existe desde a listagem,
# então pedir desde aqui == "histórico completo de cada moeda".
_FULL_HISTORY_START = datetime(2017, 1, 1, tzinfo=timezone.utc)

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
}


def get_universe_status() -> dict:
    return dict(_PROGRESS)


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
    return True


async def run_universe_backtest(
    tfs: list[str],
    limit: int = 200,
    refresh_days: int = 7,
    step_bars: int = 1,
) -> dict:
    """Job de background: backtest full-history de top-`limit` perps × `tfs`.
    Idempotente/resumível. Atualiza _PROGRESS. Retorna resumo final."""
    if _PROGRESS.get("running"):
        return {"ok": False, "error": "job já em execução", "progress": get_universe_status()}

    # Enumera o universo na MESMA fonte que o backtest consome (Binance Futures
    # via proxy de egress, igual PRD) — evita mismatch de símbolo (moeda top na
    # OKX que não existe na Binance) e cobre exatamente o universo tradável + além.
    from services.binance_futures_service import fetch_top_volume_symbols
    from services.recommendation_backtest import backtest_symbol_tf

    end_dt = datetime.now(timezone.utc)
    try:
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

    _PROGRESS.update({
        "running": True,
        "started_at": end_dt.isoformat(),
        "finished_at": None,
        "total": len(symbols) * len(tfs),
        "done": 0,
        "computed": 0,
        "skipped": 0,
        "errors": 0,
        "current": None,
        "tfs": list(tfs),
        "limit": limit,
    })
    log.info(f"[bt-universe] START {len(symbols)} símbolos × {tfs} = {len(symbols)*len(tfs)} jobs")

    try:
        for sym in symbols:
            for tf in tfs:
                _PROGRESS["current"] = f"{sym} {tf}"
                try:
                    if await _already_fresh(sym, tf, refresh_days):
                        _PROGRESS["skipped"] += 1
                        _PROGRESS["done"] += 1
                        continue
                    res = await backtest_symbol_tf(
                        sym, tf, _FULL_HISTORY_START, end_dt, step_bars=step_bars
                    )
                    await _upsert_stats(sym, tf, res)
                    if res.get("error") and not res.get("trades"):
                        _PROGRESS["errors"] += 1
                    else:
                        _PROGRESS["computed"] += 1
                except Exception as e:
                    log.warning(f"[bt-universe] {sym} {tf} crash: {e}")
                    _PROGRESS["errors"] += 1
                finally:
                    _PROGRESS["done"] += 1
                # Cede o loop pra não monopolizar o event loop do web dyno.
                await asyncio.sleep(0)
    finally:
        _PROGRESS["running"] = False
        _PROGRESS["finished_at"] = datetime.now(timezone.utc).isoformat()
        _PROGRESS["current"] = None
        log.info(f"[bt-universe] FIM — {_PROGRESS['computed']} computados, "
                 f"{_PROGRESS['skipped']} pulados, {_PROGRESS['errors']} erros")

    return {"ok": True, "progress": get_universe_status()}


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

    try:
        from services.shadow_trade_service import get_exec_allowlist, _symbol_base
        allow = get_exec_allowlist()
    except Exception:
        allow, _symbol_base = set(), lambda s: s

    ranking = []
    candidates = []
    for r in rows:
        base = _symbol_base(r.symbol) if r.symbol else r.symbol
        in_allow = bool(allow) and base in allow
        d = r.to_dict()
        d["base"] = base
        d["in_allowlist"] = in_allow
        ranking.append(d)
        # Candidata: FORA da allowlist, edge out-of-sample positiva e decente,
        # amostra ok e não dominada por expiry (trades-zumbi).
        if (not in_allow and (r.wf_avg_r or 0) > 0.10
                and (r.avg_r or 0) > 0.10
                and (r.expiry_pct or 100) < 35):
            candidates.append(d)

    return {
        "enabled": True,
        "sort": sort,
        "tf": tf,
        "min_trades": min_trades,
        "allowlist_size": len(allow),
        "n": len(ranking),
        "ranking": ranking,
        "candidates_to_promote": candidates,
        "nota": "candidatas = fora da allowlist + wf_avg_r>0.10 + avg_r>0.10 + "
                "expiry<35%. Backtest é PRÉ-FILTRO; veredito final = shadow+rotação "
                "ao vivo (derivativos/book reais) antes de size cheio.",
    }
