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
from services.signal_service import build_trade_signal, determine_direction
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns
from services.binance_vision_service import to_bv
from services.mtf_service import MTFAlignment, TFDirection, MTF_MAP, _direction_word
from services.derivatives_service import DerivativesData
from models.trade_signal import SignalDirection

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
    """Klines históricos paginados.

    Fonte PRIMÁRIA = fapi.binance.com via BINANCE_PROXY_URL (MESMO caminho que o
    PRD usa ao vivo — o proxy de egress contorna o geobloqueio do IP do Railway).
    Fallback (sem proxy) = data-api.binance.vision (spot), que pode estar
    geobloqueado no host. Os dois endpoints retornam o MESMO array de 12 colunas.
    """
    if timeframe not in TF_MS:
        raise ValueError(f"timeframe desconhecido: {timeframe}")

    from services import binance_futures_service as _bfs

    bv_sym = to_bv(symbol)  # ex: BTCUSDT (igual to_fut)
    all_rows: List[List[Any]] = []

    if _bfs.PROXY_ENABLED:
        # igual PRD: fapi.binance.com (futures) via proxy de egress; limit 1500.
        client = _bfs._get_client()
        url = f"{_bfs.FAPI_BASE}/fapi/v1/klines"
        page_limit = 1500
        own_client = False
    else:
        # fallback sem proxy: spot vision, limit 1000.
        client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (CryptoAI-Backtest)"},
        )
        url = "https://data-api.binance.vision/api/v3/klines"
        page_limit = 1000
        own_client = True

    # Paginação canônica da Binance: SÓ startTime (sem endTime). Assim a API
    # devolve a partir da LISTAGEM real da moeda mesmo pedindo desde 2017 — em vez
    # de retornar vazio numa janela pré-lançamento e abortar (bug do endTime fixo).
    try:
        cur = start_ms
        while cur < end_ms:
            params = {
                "symbol": bv_sym, "interval": timeframe,
                "startTime": cur, "limit": page_limit,
            }
            try:
                r = await client.get(url, params=params)
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
            # Última página (a API devolveu menos que o teto) → acabou o histórico.
            if len(rows) < page_limit:
                break
    finally:
        if own_client:
            await client.aclose()

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
    # Apara candles além de end_ms (paginar sem endTime pode passar do alvo).
    df = df[df["timestamp"] <= end_ms]
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


# ── Funding histórico (binance fapi) ─────────────────────────────────────────
# fapi.binance.com retorna lista de funding events (cada 8h). Cobertura
# >300 dias por requisição.

async def load_historical_funding(
    symbol: str, start_ms: int, end_ms: int,
) -> pd.DataFrame:
    """
    Retorna DataFrame com colunas (fundingTime, fundingRate) ordenado.
    Vazio em caso de falha — caller deve tratar.
    """
    from services import binance_futures_service as _bfs

    bv_sym = to_bv(symbol)  # ex: BTCUSDT
    base = _bfs.FAPI_BASE
    all_rows: List[List[Any]] = []

    # MESMO caminho do PRD: fapi via proxy de egress quando disponível (contorna
    # geobloqueio). Sem proxy, cliente próprio direto (pode 451 no Railway).
    if _bfs.PROXY_ENABLED:
        client = _bfs._get_client()
        own_client = False
    else:
        client = httpx.AsyncClient(timeout=20.0)
        own_client = True

    try:
        cur = start_ms
        while cur < end_ms:
            params = {
                "symbol": bv_sym,
                "startTime": cur, "endTime": end_ms,
                "limit": 1000,
            }
            try:
                r = await client.get(f"{base}/fapi/v1/fundingRate", params=params)
                r.raise_for_status()
                rows = r.json()
            except Exception as e:
                log.warning(f"[backtest] funding fetch falhou {symbol} @ {cur}: {e}")
                break
            if not rows:
                break
            for row in rows:
                all_rows.append([int(row["fundingTime"]), float(row["fundingRate"])])
            last_ft = all_rows[-1][0]
            if last_ft + 1 <= cur:
                break
            cur = last_ft + 1
    finally:
        if own_client:
            await client.aclose()
    if not all_rows:
        return pd.DataFrame(columns=["fundingTime", "fundingRate"])
    df = pd.DataFrame(all_rows, columns=["fundingTime", "fundingRate"])
    df = df.drop_duplicates(subset=["fundingTime"]).sort_values("fundingTime").reset_index(drop=True)
    return df


def _build_derivatives_offline(
    funding_df: Optional[pd.DataFrame],
    df_candles: pd.DataFrame,
    as_of_ms: int,
    bar_ms: int,
) -> Optional[DerivativesData]:
    """
    Constrói DerivativesData no ponto-no-tempo `as_of_ms`:
      - funding_rate: mais recente fundingTime ≤ as_of_ms
      - oi_change_24h_pct: None (sem fonte histórica acessível confiável)
      - price_change_24h: derivado dos próprios candles
    Mesma lógica de threshold de derivatives_service.analyze_derivatives.
    """
    if funding_df is None or funding_df.empty:
        return None

    # Funding mais recente <= as_of
    prior = funding_df[funding_df["fundingTime"] <= as_of_ms]
    if prior.empty:
        return None
    funding = float(prior["fundingRate"].iloc[-1])
    f_pct = funding * 100

    # Price change 24h dos candles
    bars_24h = max(1, int(24 * 3600 * 1000 / bar_ms))
    if len(df_candles) < bars_24h + 1:
        price_change_24h = 0.0
    else:
        last_close = float(df_candles["close"].iloc[-1])
        prev_close = float(df_candles["close"].iloc[-1 - bars_24h])
        price_change_24h = ((last_close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    data = DerivativesData(
        funding_rate=funding,
        funding_rate_pct=round(f_pct, 4),
        open_interest=None,
        oi_change_24h_pct=None,
    )
    # Funding sentiment (mesmos thresholds de derivatives_service)
    if f_pct > 0.05:
        data.funding_sentiment = "extreme_long"
        data.warnings.append(f"Funding extremamente positivo ({f_pct:.3f}%) — risco de long-squeeze.")
    elif f_pct > 0.02:
        data.funding_sentiment = "bullish_squeeze"
    elif f_pct < -0.05:
        data.funding_sentiment = "extreme_short"
        data.warnings.append(f"Funding extremamente negativo ({f_pct:.3f}%) — risco de short-squeeze.")
    elif f_pct < -0.02:
        data.funding_sentiment = "bearish_squeeze"
    else:
        data.funding_sentiment = "neutral"
    # OI sentiment: sem dado histórico confiável → neutral
    data.oi_sentiment = "neutral"
    data.description = f"Funding {f_pct:+.3f}%/8h"
    return data


# ── MTF offline: usa dfs higher TFs pré-carregados ───────────────────────────
def _compute_mtf_offline(
    higher_dfs: Dict[str, pd.DataFrame],
    primary_tf: str,
    primary_dir: SignalDirection,
    as_of_ts_ms: int,
) -> Optional[MTFAlignment]:
    if not higher_dfs:
        return None
    valid: List[TFDirection] = []
    for tf_h, df_h in higher_dfs.items():
        sub = df_h[df_h["timestamp"] <= as_of_ts_ms]
        if sub.empty or len(sub) < 50:
            continue
        try:
            ind = calculate_indicators(sub)
            pats = detect_all_patterns(sub)
            current = float(sub["close"].iloc[-1])
            dir_enum = determine_direction(ind, pats, current)
            dir_word = _direction_word(dir_enum)
            ema_label = None
            if ind.ema9 and ind.ema21 and ind.ema50:
                if ind.ema9 > ind.ema21 > ind.ema50:
                    ema_label = "bullish"
                elif ind.ema9 < ind.ema21 < ind.ema50:
                    ema_label = "bearish"
                else:
                    ema_label = "mixed"
            valid.append(TFDirection(
                timeframe=tf_h, direction=dir_word,
                rsi=ind.rsi, ema_aligned=ema_label, adx=ind.adx,
                description=f"{tf_h}: {dir_word.upper()}",
            ))
        except Exception:
            continue
    if not valid:
        return None
    primary_word = _direction_word(primary_dir)
    aligned = sum(1 for r in valid if r.direction == primary_word and primary_word != "neutral")
    contrary = sum(1 for r in valid if r.direction != primary_word and r.direction != "neutral" and primary_word != "neutral")
    neutral = sum(1 for r in valid if r.direction == "neutral")
    total = len(valid)
    score = (aligned - contrary) / total if total > 0 else 0.0
    return MTFAlignment(
        primary_tf=primary_tf, primary_direction=primary_word,
        higher_tfs=valid, alignment_score=round(score, 2),
        aligned_count=aligned, contrary_count=contrary,
        neutral_count=neutral, summary=f"backtest MTF {aligned}/{total}",
    )


# ── Pipeline sync de geração de sinal (offline, sem await) ───────────────────
def _signal_and_tier(
    symbol: str, tf: str, df_visible: pd.DataFrame,
    higher_dfs: Optional[Dict[str, pd.DataFrame]] = None,
    funding_df: Optional[pd.DataFrame] = None,
):
    if df_visible is None or len(df_visible) < WARMUP_BARS:
        return None
    try:
        ind = calculate_indicators(df_visible)
        patterns = detect_all_patterns(df_visible)
        # Build sinal sem MTF/deriv primeiro pra pegar a direção
        ts_raw = build_trade_signal(
            symbol, tf, df_visible, ind, patterns,
            derivatives=None, mtf=None, with_backtest=False,
        )
        if ts_raw is None:
            return None
        as_of = int(df_visible["timestamp"].iloc[-1])
        # Calcular MTF offline com base na direção primária
        mtf = None
        if higher_dfs:
            mtf = _compute_mtf_offline(higher_dfs, tf, ts_raw.direction, as_of)
        # Derivatives offline (funding hist) — None se sem fonte
        derivatives = None
        if funding_df is not None and not funding_df.empty:
            derivatives = _build_derivatives_offline(
                funding_df, df_visible, as_of, TF_MS[tf],
            )
        # Rebuild sinal com MTF + derivatives
        ts = build_trade_signal(
            symbol, tf, df_visible, ind, patterns,
            derivatives=derivatives, mtf=mtf, with_backtest=False,
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

    # Pré-carrega higher TFs pra MTF offline
    higher_tfs = MTF_MAP.get(timeframe, [])
    higher_dfs: Dict[str, pd.DataFrame] = {}
    for tf_h in higher_tfs:
        if tf_h not in TF_MS:
            continue
        try:
            df_h = await load_historical_ohlcv(symbol, tf_h, start_ms, end_ms)
            if not df_h.empty:
                higher_dfs[tf_h] = df_h
                log.info(f"[backtest] {symbol} higher {tf_h}: {len(df_h)} candles")
        except Exception as e:
            log.warning(f"[backtest] higher {tf_h} fetch falhou: {e}")

    # Pré-carrega funding rate histórica (fapi.binance.com)
    funding_df: Optional[pd.DataFrame] = None
    try:
        funding_df = await load_historical_funding(symbol, start_ms, end_ms)
        if not funding_df.empty:
            log.info(f"[backtest] {symbol} funding: {len(funding_df)} events")
        else:
            funding_df = None
    except Exception as e:
        log.warning(f"[backtest] funding fetch falhou: {e}")
        funding_df = None

    trades: List[Dict[str, Any]] = []
    last_open_per_dir: Dict[str, datetime] = {}
    dedup_delta = timedelta(hours=DEDUP_WINDOW_HOURS)
    bar_ms = TF_MS[timeframe]
    bars_per_hour = max(1, int(3600 / (bar_ms / 1000)))

    for i in range(WARMUP_BARS, len(df) - 1, step_bars):
        # Cede o event loop a cada ~250 barras: a varredura full-history (≈19k
        # barras p/ BTC) é CPU-bound e síncrona — sem isto o web dyno fica MUDO
        # (timeout no /status) durante todo o símbolo. sleep(0) deixa o servidor
        # respirar entre blocos sem mudar o resultado.
        if i % 250 == 0:
            await asyncio.sleep(0)
        df_visible = df.iloc[:i + 1]
        result = _signal_and_tier(
            symbol, timeframe, df_visible,
            higher_dfs=higher_dfs, funding_df=funding_df,
        )
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

        # int(): EXPIRY_HOURS é float (168.0) → o fim do slice viraria float e o
        # iloc estoura "positional indexing ... with these indexers [N.0] of type float".
        fwd_bars = int(min(MAX_FORWARD_BARS, EXPIRY_HOURS * bars_per_hour))
        future = df.iloc[i + 1:i + 1 + fwd_bars]
        if future.empty:
            continue
        outcome = _simulate_trade(snap, future, timeframe)
        # Patterns presentes no signal (lista de pattern_type strings).
        pat_types: List[str] = []
        try:
            for p in (ts.patterns or []):
                pt = getattr(p, "type", None)
                if pt is not None:
                    pat_types.append(pt.value if hasattr(pt, "value") else str(pt))
        except Exception:
            pass
        trades.append({
            "symbol": symbol, "timeframe": timeframe, "tier": tier,
            "direction": direction,
            "score": round(score, 2),
            "rr": round(ts.risk_reward, 2),
            "entry": snap.entry, "stop": snap.stop_loss,
            "tp1": snap.tp1, "tp2": snap.tp2,
            "patterns": pat_types,
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
    sum_losses_abs = abs(sum(losses))
    profit_factor = (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")
    avg_r = total_r / n
    avg_win = (sum_wins / len(wins)) if wins else 0
    avg_loss = (sum(losses) / len(losses)) if losses else 0
    expectancy = (len(wins) / n) * avg_win + (len(losses) / n) * avg_loss
    mean = avg_r
    var = sum((r - mean) ** 2 for r in r_values) / n
    std = math.sqrt(var) if var > 0 else 0.0
    sharpe_r = (mean / std) if std > 0 else float("inf") if mean > 0 else 0.0

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
        "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else None,
        "sharpe_r": round(sharpe_r, 3) if math.isfinite(sharpe_r) else None,
        "max_dd_r": round(max_dd, 2),
        "status_dist": status_dist,
    }


# ── Simulador de equity ──────────────────────────────────────────────────────
# Converte trades R-based em curva de equity em $. Premissa: risco fixo em %
# do balance atual a cada trade (composição). PnL_$ = balance_atual * (R/100)
# * risk_pct. Modela como conta real cresceria/sofreria com a estratégia.
#
# Notas:
# - Não considera fees nem slippage (em futuros perp BTC/USDT taker ~0.05%,
#   slip ~0.02% — em 50 trades isso vira ~3-4% de drag. Ajuste mental: subtrair
#   ~0.07% × n_trades do retorno final).
# - Trades fora de ordem por símbolo são sequenciados por created_ts global.
# - Account DD% mede pico→vale na curva de equity, não em R.

def compute_equity_curve(
    trades: List[Dict[str, Any]],
    starting_balance: float = 10000.0,
    risk_pct: float = 1.0,
    fee_pct_per_side: float = 0.0,
    slippage_pct_per_side: float = 0.0,
) -> Dict[str, Any]:
    """
    Simula curva de equity com risco composto.
    `fee_pct_per_side`: taxa por entrada/saída em % do notional (ex: 0.05 = 0.05%).
    `slippage_pct_per_side`: slip por entrada/saída em % (ex: 0.02).
    Custo total por trade = 2 × (fee + slip) × leverage_implícito do notional.

    Premissa simplificada: notional = risk_usd / stop_distance_pct, então
    custo_pct_do_balance = 2 × (fee+slip) × (risk_pct / stop_distance_pct).
    Como não temos stop_distance_pct por trade aqui (não foi persistido no
    trade dict), aproximamos: custo_usd = 2 × (fee+slip)/100 × notional,
    e notional ≈ risk_usd / 0.01 (assume stop ~1% — conservador). Resultado:
    custo_usd = (fee+slip)/100 × risk_usd × 2 / 0.01 = (fee+slip)*2*risk_usd*100.

    Versão tighter: usa stop_distance_pct se trade tiver. Fallback 1%.
    """
    if not trades:
        return {
            "starting_balance": starting_balance,
            "final_balance": starting_balance,
            "total_pnl_usd": 0.0, "total_return_pct": 0.0,
            "max_account_dd_pct": 0.0, "max_account_dd_usd": 0.0,
            "n_trades": 0, "avg_pnl_usd": 0.0,
            "winning_streak_max": 0, "losing_streak_max": 0,
            "best_trade_usd": 0.0, "worst_trade_usd": 0.0,
            "fees_total_usd": 0.0, "fee_drag_pct": 0.0,
            "equity_curve": [],
        }
    sorted_trades = sorted(trades, key=lambda t: t["created_ts"])
    risk_frac = risk_pct / 100.0
    cost_per_side_frac = (fee_pct_per_side + slippage_pct_per_side) / 100.0
    balance = starting_balance
    peak = balance
    max_dd_pct = 0.0
    max_dd_usd = 0.0
    cur_win_streak = 0
    cur_lose_streak = 0
    max_win_streak = 0
    max_lose_streak = 0
    best = float("-inf")
    worst = float("inf")
    fees_total = 0.0
    curve: List[Dict[str, Any]] = [{"ts": sorted_trades[0]["created_ts"], "balance": round(balance, 2)}]
    pnls: List[float] = []
    for t in sorted_trades:
        r = t["realized_r"]
        risk_usd = balance * risk_frac
        # Notional implícito: risk_usd / stop_distance_frac.
        # stop_distance ≈ |entry - stop|/entry. Se ausente, fallback 1%.
        entry = t.get("entry")
        stop = t.get("stop")
        if entry and stop and entry > 0:
            stop_dist_frac = abs(entry - stop) / entry
        else:
            stop_dist_frac = 0.01
        if stop_dist_frac <= 0:
            stop_dist_frac = 0.01
        notional = risk_usd / stop_dist_frac
        cost_usd = 2 * cost_per_side_frac * notional  # entry + exit
        gross_pnl = risk_usd * r
        pnl_usd = gross_pnl - cost_usd
        fees_total += cost_usd
        balance += pnl_usd
        pnls.append(pnl_usd)
        if pnl_usd > best: best = pnl_usd
        if pnl_usd < worst: worst = pnl_usd
        if r > 0:
            cur_win_streak += 1; cur_lose_streak = 0
            if cur_win_streak > max_win_streak: max_win_streak = cur_win_streak
        elif r < 0:
            cur_lose_streak += 1; cur_win_streak = 0
            if cur_lose_streak > max_lose_streak: max_lose_streak = cur_lose_streak
        if balance > peak: peak = balance
        dd_usd = peak - balance
        dd_pct = (dd_usd / peak * 100) if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_usd = dd_usd
        curve.append({"ts": t.get("exit_ts") or t["created_ts"], "balance": round(balance, 2)})

    total_pnl = balance - starting_balance
    total_ret_pct = (total_pnl / starting_balance * 100) if starting_balance > 0 else 0
    avg_pnl = sum(pnls) / len(pnls)
    fee_drag_pct = (fees_total / starting_balance * 100) if starting_balance > 0 else 0
    return {
        "starting_balance": starting_balance,
        "final_balance": round(balance, 2),
        "total_pnl_usd": round(total_pnl, 2),
        "total_return_pct": round(total_ret_pct, 2),
        "max_account_dd_pct": round(max_dd_pct, 2),
        "max_account_dd_usd": round(max_dd_usd, 2),
        "n_trades": len(sorted_trades),
        "avg_pnl_usd": round(avg_pnl, 2),
        "winning_streak_max": max_win_streak,
        "losing_streak_max": max_lose_streak,
        "best_trade_usd": round(best, 2),
        "worst_trade_usd": round(worst, 2),
        "risk_pct": risk_pct,
        "fee_pct_per_side": fee_pct_per_side,
        "slippage_pct_per_side": slippage_pct_per_side,
        "fees_total_usd": round(fees_total, 2),
        "fee_drag_pct": round(fee_drag_pct, 2),
        "equity_curve": curve,
    }


def pattern_breakdown(all_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Breakdown WR/PF/R por pattern_type. Cada trade pode ter múltiplos
    patterns; conta cada pattern individualmente (overlap esperado).

    Útil pra responder: "double_bottom realmente ganha mais que descending
    wedge? Vale o peso na confluência?"
    """
    by_pattern: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        pats = t.get("patterns") or []
        if not pats:
            by_pattern.setdefault("__no_pattern__", []).append(t)
            continue
        for p in pats:
            by_pattern.setdefault(p, []).append(t)
    out = {}
    for pname, trs in by_pattern.items():
        m = compute_metrics(trs)
        out[pname] = m
    return out


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
        "by_pattern": pattern_breakdown(all_trades) if all_trades else {},
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
    # Anexa lista plana de trades pra walkforward consumir; CLI remove antes
    # de salvar JSON pra não inflar o arquivo.
    report["_all_trades"] = all_trades
    return report


# ── Walk-forward ─────────────────────────────────────────────────────────────
# A estratégia é rule-based (sem parâmetros pra "treinar"), então walk-forward
# aqui mede **robustez temporal**: divide o range em N janelas e mede métricas
# por janela. Stability score = % janelas com R>0. Mostra se a estratégia
# degrada em regimes diferentes (bull/bear/chop) ou é consistente.

def _slice_trades_by_window(
    trades: List[Dict[str, Any]], start_ms: int, end_ms: int,
) -> List[Dict[str, Any]]:
    return [t for t in trades if start_ms <= t["created_ts"] < end_ms]


def walkforward_report(
    all_trades: List[Dict[str, Any]],
    start_dt: datetime, end_dt: datetime, n_folds: int,
) -> Dict[str, Any]:
    if n_folds < 2:
        raise ValueError("n_folds deve ser ≥ 2")
    total_ms = int((end_dt - start_dt).total_seconds() * 1000)
    fold_ms = total_ms // n_folds

    folds: List[Dict[str, Any]] = []
    fold_total_r: List[float] = []
    fold_wr: List[float] = []
    positive_folds = 0
    empty_folds = 0

    for k in range(n_folds):
        f_start_ms = int(start_dt.timestamp() * 1000) + k * fold_ms
        f_end_ms = f_start_ms + fold_ms if k < n_folds - 1 else int(end_dt.timestamp() * 1000)
        slice_trades = _slice_trades_by_window(all_trades, f_start_ms, f_end_ms)
        metrics = compute_metrics(slice_trades)
        f_start = datetime.fromtimestamp(f_start_ms / 1000, tz=timezone.utc)
        f_end = datetime.fromtimestamp(f_end_ms / 1000, tz=timezone.utc)
        if slice_trades:
            fold_total_r.append(metrics["total_r"])
            fold_wr.append(metrics["win_rate_pct"])
            if metrics["total_r"] > 0:
                positive_folds += 1
        else:
            empty_folds += 1
        folds.append({
            "fold": k + 1,
            "start": f_start.isoformat(), "end": f_end.isoformat(),
            "days": round((f_end - f_start).total_seconds() / 86400, 1),
            "metrics": metrics,
        })

    # Stability: % de folds com trades que foram positivos
    folds_with_trades = n_folds - empty_folds
    stability_pct = (positive_folds / folds_with_trades * 100) if folds_with_trades > 0 else 0.0

    if fold_total_r:
        mean_r = sum(fold_total_r) / len(fold_total_r)
        var_r = sum((r - mean_r) ** 2 for r in fold_total_r) / len(fold_total_r)
        std_r = math.sqrt(var_r)
        consistency = (mean_r / std_r) if std_r > 0 else float("inf")
    else:
        mean_r = std_r = consistency = 0.0

    if fold_wr:
        mean_wr = sum(fold_wr) / len(fold_wr)
        var_wr = sum((w - mean_wr) ** 2 for w in fold_wr) / len(fold_wr)
        std_wr = math.sqrt(var_wr)
    else:
        mean_wr = std_wr = 0.0

    return {
        "n_folds": n_folds,
        "folds_with_trades": folds_with_trades,
        "empty_folds": empty_folds,
        "positive_folds": positive_folds,
        "stability_pct": round(stability_pct, 1),
        "fold_total_r_mean": round(mean_r, 2),
        "fold_total_r_std": round(std_r, 2),
        "consistency_ratio": round(consistency, 2) if math.isfinite(consistency) else None,
        "fold_wr_mean": round(mean_wr, 1),
        "fold_wr_std": round(std_wr, 1),
        "folds": folds,
    }


# ── Param sweep ──────────────────────────────────────────────────────────────
# Roda o mesmo dataset histórico variando 1 constante (ex.: ATR_TRAIL_K,
# MIN_RR) e reporta métricas comparativas. Útil pra A/B testing rápido:
# "subir K de 2.2 pra 2.5 ajuda ou piora?".
#
# Aceita "module.CONST" e faz monkey-patch via setattr no módulo. Funções
# leem do globals do módulo a cada call, então o patch tem efeito imediato
# nas próximas iterações.

_SWEEPABLE_PARAMS: Dict[str, Any] = {
    "MIN_RR":            ("services.recommendation_service", float),
    "MIN_CONFIDENCE_B":  ("services.recommendation_service", float),
    "ATR_TRAIL_K":       ("services.snapshot_service", float),
    "BE_PLUS_LOCK_R":    ("services.snapshot_service", float),
    "EXPIRY_HOURS":      ("services.snapshot_service", int),
    "DEDUP_WINDOW_HOURS":("services.snapshot_service", int),
}


def _resolve_param(param_name: str):
    if param_name not in _SWEEPABLE_PARAMS:
        raise ValueError(
            f"param '{param_name}' não é sweepável. Suportados: "
            f"{sorted(_SWEEPABLE_PARAMS.keys())}"
        )
    mod_path, caster = _SWEEPABLE_PARAMS[param_name]
    import importlib
    module = importlib.import_module(mod_path)
    return module, param_name, caster


async def run_param_sweep(
    symbols: List[str], timeframes: List[str],
    param_name: str, values: List[Any],
    days_back: int = 90, step_bars: int = 1,
    n_folds: int = 0,
    end_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    module, attr, caster = _resolve_param(param_name)
    original = getattr(module, attr)
    log.info(f"[sweep] {param_name} original={original}, testando {values}")

    variants: List[Dict[str, Any]] = []
    try:
        for v in values:
            v_cast = caster(v)
            setattr(module, attr, v_cast)
            log.info(f"[sweep] === {param_name} = {v_cast} ===")
            if n_folds and n_folds >= 2:
                report = await run_walkforward(
                    symbols=symbols, timeframes=timeframes,
                    days_back=days_back, step_bars=step_bars,
                    n_folds=n_folds, end_dt=end_dt,
                )
            else:
                report = await run_backtest(
                    symbols=symbols, timeframes=timeframes,
                    days_back=days_back, step_bars=step_bars, end_dt=end_dt,
                )
                report.pop("_all_trades", None)
            variants.append({
                "value": v_cast,
                "summary": report["summary"],
                "trades_count": report["trades_count"],
                "by_tier": report["by_tier"],
                "walkforward": report.get("walkforward"),
                "_all_trades": report.get("_all_trades", []),
            })
    finally:
        setattr(module, attr, original)
        log.info(f"[sweep] restaurado {param_name} = {original}")

    return {
        "param": param_name,
        "original_value": original,
        "variants": variants,
        "params": {
            "symbols": symbols, "timeframes": timeframes,
            "days_back": days_back, "step_bars": step_bars,
            "n_folds": n_folds,
        },
    }


async def run_walkforward(
    symbols: List[str], timeframes: List[str],
    days_back: int = 90, step_bars: int = 1,
    n_folds: int = 6,
    end_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Walk-forward analysis: roda backtest no range completo, depois fatia os
    trades em N janelas temporais iguais e reporta métricas por janela +
    estatísticas de robustez (stability, consistency).
    """
    report = await run_backtest(
        symbols=symbols, timeframes=timeframes,
        days_back=days_back, step_bars=step_bars, end_dt=end_dt,
    )
    end_dt_actual = datetime.fromisoformat(report["params"]["end"])
    start_dt_actual = datetime.fromisoformat(report["params"]["start"])
    all_trades = report.get("_all_trades", [])
    wf = walkforward_report(all_trades, start_dt_actual, end_dt_actual, n_folds)
    report["walkforward"] = wf
    # CLI decide se preserva _all_trades (pra equity) ou descarta antes
    # de salvar o JSON.
    return report
