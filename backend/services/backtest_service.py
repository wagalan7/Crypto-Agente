"""
Backtest Service — engine de backtest histórico para o sistema de recomendações.

Objetivo: dado (symbol, timeframe, start, end), replica o pipeline de análise
candle-a-candle, abre trades virtuais quando aparece um setup A+/A/B, simula
TP1/TP2/stop/trail/expiry usando exatamente a MESMA lógica de
`snapshot_service._classify_outcome_candles`, e agrega métricas profissionais.

Limitações vs produção (assumidas conscientemente — MVP):
  • Derivativos (funding/OI) = None  → tier penalty deriv não aplica
  • MTF alignment = None             → score MTF fica em baseline
  • Ticker 24h change = 0.0          → derivatives_service ficaria neutro
  • Não consulta o cache de prob_tp1 → backtest é "raw signal"

Esses fatores afetam tier em casos de borda; o backtest serve pra comparar
MUDANÇAS no engine (mudei K=2.2 → 2.5: ficou melhor?) e medir baseline
absoluto da estratégia bruta. Walk-forward virá em iteração futura.

Loop principal (por símbolo×TF):
  1. Carrega N candles históricos
  2. Para cada barra i a partir de WARMUP:
       a. df_visible = candles[:i+1]
       b. ts = build_trade_signal(df_visible, derivatives=None, mtf=None)
       c. tier = _classify_tier(ts, score)
       d. Se tier in (A+, A, B): abre trade virtual com entry/stop/tp1/tp2
       e. Simula candles[i+1 .. j] até fechar OU expirar (time stop + 48h cap)
  3. Aggrega trades em MetricsReport

Dedup: usa a mesma janela DEDUP_WINDOW_HOURS pra não abrir trades duplicados
no mesmo setup (mesmo symbol+tf+direction em 2h).
"""
from __future__ import annotations
import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

import httpx
import pandas as pd

log = logging.getLogger(__name__)

# Reusa constantes e função de outcome do snapshot_service (paridade 1:1
# com produção pra trail/BE+/time-stop).
from services.snapshot_service import (
    _classify_outcome_candles,
    _time_stop_hours,
    EXPIRY_HOURS,
    DEDUP_WINDOW_HOURS,
    REALIZED_R_TP1,
)
from services.recommendation_service import (
    _classify_tier,
    _compute_score,
)
from services.signal_service import build_trade_signal
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns
from services.binance_vision_service import to_bv

WARMUP_BARS = 200
MAX_FORWARD_BARS = 480

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


@dataclass
class _MockSnapshot:
    """Subset de RecommendationSnapshot que _classify_outcome_candles usa."""
    symbol: str
    timeframe: str
    direction: str
    entry: float
    stop_loss: float
    tp1: Optional[float]
    tp2: float
    features: Dict[str, Any]
    created_at: datetime
    tp1_hit_at: Optional[datetime] = None
    peak_price_since_tp1: Optional[float] = None


# ── Loader histórico paginado ────────────────────────────────────────────────
async def load_historical_ohlcv(
    symbol: str, timeframe: str, start_ms: int, end_ms: int,
) -> pd.DataFrame:
    if timeframe not in TF_MS:
        raise ValueError(f"timeframe desconhecido: {timeframe}")

    bv_sym = to_bv(symbol)
    step_ms = TF_MS[timeframe] * 1000
    base = "https://data-api.binance.vision"
    all_rows: List[List[Any]] = []

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "Mozilla/5.0 (CryptoAI-Backtest)"},
    ) as client:
        cur = start_ms
        while cur < end_ms:
            params = {
                "symbol": bv_sym, "interval": timeframe,
                "startTime": cur, "endTime": min(cur + step_ms, end_ms),
                "limit": 1000,
            }
            try:
                r = await client.get(f"{base}/api/v3/klines", params=params)
                r.raise_for_status()
                rows = r.json()
            except Exception as e:
                log.warning(f"[backtest] fetch falhou {symbol} {timeframe} @ {cur}: {e}")
                break
            if not rows:
                break
            all_rows.extend(rows)
            last_close_time = rows[-1][6]
            new_cur = last_close_time + 1
            if new_cur <= cur:
                break
            cur = new_cur

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


# ── Pipeline sync de geração de sinal (offline, sem await) ───────────────────
def _signal_and_tier(symbol: str, tf: str, df_visible: pd.DataFrame):
    if df_visible is None or len(df_visible) < WARMUP_BARS:
        return None
    try:
        ind = calculate_indicators(df_visible)
        patterns = detect_all_patterns(df_visible)
        ts = build_trade_signal(
            symbol, tf, df_visible, ind, patterns,
            derivatives=None, mtf=None, with_backtest=False,
        )
    except Exception:
        return None
    if ts is None:
        return None
    try:
        score = _compute_score(ts)
        tier = _classify_tier(ts, score)
    except Exception:
        return None
    if tier is None:
        return None
    return ts, score, tier


def _atr_pct_from_signal(ts) -> Optional[float]:
    try:
        atr = ts.indicators.atr if ts.indicators else None
        if atr and ts.entry:
            return round((float(atr) / float(ts.entry)) * 100, 3)
    except Exception:
        pass
    return None


# ── Simulação de 1 trade ─────────────────────────────────────────────────────
def _simulate_trade(snap: _MockSnapshot, future_candles: pd.DataFrame, tf: str) -> Dict[str, Any]:
    time_stop_hours = _time_stop_hours(tf)
    bars_per_hour = max(1, int(3600 / (TF_MS[tf] / 1000)))
    time_stop_bars = int(time_stop_hours * bars_per_hour)
    expiry_bars = int(EXPIRY_HOURS * bars_per_hour)
    max_bars = min(MAX_FORWARD_BARS, len(future_candles), expiry_bars)

    for i in range(max_bars):
        window = future_candles.iloc[i:i+1]
        result = _classify_outcome_candles(snap, window)

        if result is None:
            # Time-stop antes de TP1
            if snap.tp1_hit_at is None and i + 1 >= time_stop_bars:
                exit_price = float(window["close"].iloc[-1])
                if snap.direction == "long":
                    move = exit_price - snap.entry
                else:
                    move = snap.entry - exit_price
                stop_dist = abs(snap.entry - snap.stop_loss) or 1e-9
                return {
                    "status": "expired", "exit_price": exit_price,
                    "exit_ts": int(window["timestamp"].iloc[-1]),
                    "realized_r": round(move / stop_dist, 3),
                    "bars_held": i + 1, "tp1_hit": False, "expired": True,
                }
            continue

        outcome_type = result[0]
        if outcome_type in ("won_tp2", "lost", "won_tp1_be"):
            return {
                "status": outcome_type, "exit_price": float(result[1]),
                "exit_ts": int(window["timestamp"].iloc[-1]),
                "realized_r": float(result[2]),
                "bars_held": i + 1,
                "tp1_hit": (outcome_type != "lost") or snap.tp1_hit_at is not None,
                "expired": False,
            }

        if outcome_type == "open_after_tp1":
            snap.tp1_hit_at = datetime.fromtimestamp(
                int(window["timestamp"].iloc[-1]) / 1000, tz=timezone.utc
            )
            snap.peak_price_since_tp1 = result[4]
        elif outcome_type == "open_update":
            snap.peak_price_since_tp1 = result[4]

    # Esgotou janela: se TP1 já tocou, conservador won_tp1; senão expired
    if max_bars == 0:
        return {"status": "no_data", "realized_r": 0, "bars_held": 0,
                "tp1_hit": False, "expired": True, "exit_price": snap.entry, "exit_ts": 0}
    last = future_candles.iloc[max_bars - 1]
    exit_price = float(last["close"])
    if snap.tp1_hit_at is not None:
        return {
            "status": "won_tp1", "exit_price": exit_price,
            "exit_ts": int(last["timestamp"]),
            "realized_r": REALIZED_R_TP1,
            "bars_held": max_bars, "tp1_hit": True, "expired": True,
        }
    if snap.direction == "long":
        move = exit_price - snap.entry
    else:
        move = snap.entry - exit_price
    stop_dist = abs(snap.entry - snap.stop_loss) or 1e-9
    return {
        "status": "expired", "exit_price": exit_price,
        "exit_ts": int(last["timestamp"]),
        "realized_r": round(move / stop_dist, 3),
        "bars_held": max_bars, "tp1_hit": False, "expired": True,
    }


# ── Loop por símbolo × TF ────────────────────────────────────────────────────
async def backtest_symbol_tf(
    symbol: str, timeframe: str,
    start_dt: datetime, end_dt: datetime,
    step_bars: int = 1,
) -> Dict[str, Any]:
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    log.info(f"[backtest] {symbol} {timeframe} {start_dt.date()}→{end_dt.date()}")
    df = await load_historical_ohlcv(symbol, timeframe, start_ms, end_ms)
    if df.empty or len(df) < WARMUP_BARS + 50:
        return {"symbol": symbol, "timeframe": timeframe, "trades": [],
                "error": f"insuficiente candles ({len(df)})"}

    log.info(f"[backtest] {symbol} {timeframe}: {len(df)} candles carregados")

    trades: List[Dict[str, Any]] = []
    last_open_per_dir: Dict[str, datetime] = {}
    dedup_delta = timedelta(hours=DEDUP_WINDOW_HOURS)
    bar_ms = TF_MS[timeframe]
    bars_per_hour = max(1, int(3600 / (bar_ms / 1000)))

    for i in range(WARMUP_BARS, len(df) - 1, step_bars):
        df_visible = df.iloc[:i + 1]
        result = _signal_and_tier(symbol, timeframe, df_visible)
        if result is None:
            continue
        ts, score, tier = result
        if not (ts.entry and ts.stop_loss and ts.tp1 and ts.tp2):
            continue

        bar_ts = datetime.fromtimestamp(int(df.iloc[i]["timestamp"]) / 1000, tz=timezone.utc)
        direction = ts.direction.value if hasattr(ts.direction, "value") else str(ts.direction)

        prev = last_open_per_dir.get(direction)
        if prev and (bar_ts - prev) < dedup_delta:
            continue
        last_open_per_dir[direction] = bar_ts

        snap = _MockSnapshot(
            symbol=symbol, timeframe=timeframe, direction=direction,
            entry=float(ts.entry), stop_loss=float(ts.stop_loss),
            tp1=float(ts.tp1) if ts.tp1 else None,
            tp2=float(ts.tp2),
            features={"atr_pct": _atr_pct_from_signal(ts)},
            created_at=bar_ts,
        )

        future = df.iloc[i + 1:i + 1 + min(MAX_FORWARD_BARS, EXPIRY_HOURS * bars_per_hour)]
        if future.empty:
            continue
        outcome = _simulate_trade(snap, future, timeframe)
        trades.append({
            "symbol": symbol, "timeframe": timeframe, "tier": tier,
            "direction": direction,
            "score": round(score, 2),
            "rr": round(ts.risk_reward, 2),
            "entry": snap.entry, "stop": snap.stop_loss,
            "tp1": snap.tp1, "tp2": snap.tp2,
            "created_ts": int(df.iloc[i]["timestamp"]),
            "created_at": bar_ts.isoformat(),
            **outcome,
        })

    return {"symbol": symbol, "timeframe": timeframe,
            "candles": len(df), "trades": trades}


# ── Métricas ─────────────────────────────────────────────────────────────────
def compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"total_trades": 0}
    n = len(trades)
    r_values = [t["realized_r"] for t in trades]
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    flats = [r for r in r_values if r == 0]
    win_rate = (len(wins) / n) * 100
    total_r = sum(r_values)
    sum_wins = sum(wins)
    sum_losses = abs(sum(losses)) or 1e-9
    profit_factor = sum_wins / sum_losses
    avg_r = total_r / n
    avg_win = (sum_wins / len(wins)) if wins else 0
    avg_loss = (sum(losses) / len(losses)) if losses else 0
    expectancy = (len(wins) / n) * avg_win + (len(losses) / n) * avg_loss
    mean = avg_r
    var = sum((r - mean) ** 2 for r in r_values) / n
    std = math.sqrt(var) if var > 0 else 1e-9
    sharpe_r = mean / std

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_values:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    status_dist: Dict[str, int] = {}
    for t in trades:
        status_dist[t["status"]] = status_dist.get(t["status"], 0) + 1

    return {
        "total_trades": n,
        "wins": len(wins), "losses": len(losses), "flats": len(flats),
        "win_rate_pct": round(win_rate, 1),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 3),
        "avg_win_r": round(avg_win, 3),
        "avg_loss_r": round(avg_loss, 3),
        "expectancy_r": round(expectancy, 3),
        "profit_factor": round(profit_factor, 2),
        "sharpe_r": round(sharpe_r, 3),
        "max_dd_r": round(max_dd, 2),
        "status_dist": status_dist,
    }


def aggregate_report(all_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_tier: Dict[str, List[Dict[str, Any]]] = {"A+": [], "A": [], "B": []}
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        if t["tier"] in by_tier:
            by_tier[t["tier"]].append(t)
        by_symbol.setdefault(t["symbol"], []).append(t)
    return {
        "summary": compute_metrics(all_trades),
        "by_tier": {k: compute_metrics(v) for k, v in by_tier.items() if v},
        "by_symbol": {k: compute_metrics(v) for k, v in by_symbol.items() if v},
        "trades_count": len(all_trades),
    }


async def run_backtest(
    symbols: List[str], timeframes: List[str],
    days_back: int = 90, step_bars: int = 1,
    end_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    end_dt = end_dt or datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    sem = asyncio.Semaphore(3)

    async def _bounded(sym: str, tf: str):
        async with sem:
            try:
                return await backtest_symbol_tf(sym, tf, start_dt, end_dt, step_bars=step_bars)
            except Exception as e:
                log.warning(f"[backtest] {sym} {tf} crash: {e}")
                return {"symbol": sym, "timeframe": tf, "trades": [], "error": str(e)}

    tasks = [_bounded(s, tf) for s in symbols for tf in timeframes]
    results = await asyncio.gather(*tasks)

    all_trades = []
    per_pair = []
    for r in results:
        trades = r.get("trades", [])
        all_trades.extend(trades)
        per_pair.append({
            "symbol": r.get("symbol"),
            "timeframe": r.get("timeframe"),
            "candles": r.get("candles"),
            "trades_count": len(trades),
            "metrics": compute_metrics(trades) if trades else {},
            "error": r.get("error"),
        })

    report = aggregate_report(all_trades)
    report["per_pair"] = per_pair
    report["params"] = {
        "symbols": symbols, "timeframes": timeframes,
        "days_back": days_back, "step_bars": step_bars,
        "start": start_dt.isoformat(), "end": end_dt.isoformat(),
    }
    return report
