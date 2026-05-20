import asyncio
import json
import time
import traceback
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any

logging.basicConfig(level=logging.INFO)

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import TIMEFRAMES, DEFAULT_TIMEFRAME, DEFAULT_LIMIT, ANTHROPIC_API_KEY, GROQ_API_KEY
from services.binance_service import (
    get_perpetual_symbols,
    fetch_ohlcv,
    fetch_ticker,
    fetch_multiple_tickers,
    fetch_funding_rate,
    fetch_open_interest,
    close_exchange,
)
from services.indicator_service import calculate_indicators
from services.pattern_service import detect_all_patterns
from services.signal_service import build_trade_signal, determine_direction
from services.ai_service import generate_ai_analysis
from services.derivatives_service import analyze_derivatives
from services.mtf_service import analyze_mtf
from services.trade_service import get_trades, save_trades
from services.macro_service import get_btc_dominance, build_macro_context, get_global_market_data
from services.recommendation_service import (
    get_recommendations,
    get_recommendations_from_batch,
    get_recommendations_via_vision,
)
from services.snapshot_service import (
    save_recommendations,
    check_open_snapshots,
    get_daily_pnl,
    get_history_stats,
)
from services.learning_service import (
    compute_stats_by_bucket,
    lookup_historical_batch,
)
from services.push_service import (
    get_public_key as push_get_public_key,
    save_subscription as push_save_subscription,
    remove_subscription as push_remove_subscription,
    notify_recommendations_batch,
    PUSH_ENABLED,
)
from db import init_db, close_db, DB_ENABLED
from models.trade_signal import TradeSignal


_snapshot_task: Optional[asyncio.Task] = None
_scan_task: Optional[asyncio.Task] = None

SERVER_SCAN_INTERVAL = 300        # 5 min entre varreduras server-side
SERVER_SCAN_TOP_N = 30            # quantos símbolos varrer
SERVER_SCAN_INITIAL_DELAY = 45    # espera 45s após startup pra não competir com init


async def _snapshot_loop():
    """Roda check_open_snapshots a cada 5 minutos."""
    while True:
        try:
            await asyncio.sleep(300)
            await check_open_snapshots()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"snapshot_loop error: {e}")


async def _server_scan_loop():
    """
    Varredura server-side periódica (OKX → Railway funciona). Salva novos
    snapshots e dispara push notifications pras recs A+ / A novas — sem
    precisar do usuário abrir o app.
    """
    await asyncio.sleep(SERVER_SCAN_INITIAL_DELAY)
    while True:
        try:
            from services.push_service import PUSH_ENABLED as _PE
            logging.info(
                f"[server-scan] iniciando ciclo — DB={DB_ENABLED} PUSH={_PE} "
                f"top_n={SERVER_SCAN_TOP_N} fonte=binance-vision"
            )
            if not _PE and not DB_ENABLED:
                logging.info("[server-scan] DB e push ambos OFF, pulando")
                await asyncio.sleep(SERVER_SCAN_INTERVAL)
                continue

            recs = await get_recommendations_via_vision(top_n=SERVER_SCAN_TOP_N)
            recs_dict = [r.model_dump() for r in recs]

            # Distribuição por tier — útil pra diagnosticar
            by_tier = {"A+": 0, "A": 0, "B": 0}
            for r in recs_dict:
                t = r.get("tier", "")
                if t in by_tier:
                    by_tier[t] += 1
            logging.info(
                f"[server-scan] varredura completa: {len(recs)} recs "
                f"(A+={by_tier['A+']} A={by_tier['A']} B={by_tier['B']})"
            )

            # Filtra só A+ e A (B não notifica por default do usuário)
            pushable = [r for r in recs_dict if r.get("tier") in ("A+", "A")]

            newly_saved = 0
            if DB_ENABLED and recs_dict:
                try:
                    newly_saved = await save_recommendations(recs_dict) or 0
                except Exception as e:
                    logging.warning(f"[server-scan] save falhou: {e}")

            logging.info(
                f"[server-scan] {newly_saved}/{len(recs_dict)} novas (dedup 2h), "
                f"{len(pushable)} elegíveis pra push"
            )

            if _PE and newly_saved > 0 and pushable:
                try:
                    sent = await notify_recommendations_batch(pushable, len(pushable))
                    logging.info(f"[server-scan] ✅ {sent} push(es) enviados")
                except Exception as e:
                    logging.warning(f"[server-scan] push falhou: {e}")
            else:
                reason = []
                if not _PE: reason.append("push OFF")
                if newly_saved == 0: reason.append("nada novo")
                if not pushable: reason.append("nenhum A+/A")
                logging.info(f"[server-scan] sem push enviado ({', '.join(reason) or '?'})")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"[server-scan] erro: {e}", exc_info=True)

        try:
            await asyncio.sleep(SERVER_SCAN_INTERVAL)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snapshot_task, _scan_task
    if DB_ENABLED:
        try:
            await init_db()
            _snapshot_task = asyncio.create_task(_snapshot_loop())
            logging.info("Snapshot tracker iniciado (intervalo 5 min).")
        except Exception as e:
            logging.error(f"Falha ao inicializar DB: {e}")
    # Varredura server-side (OKX) — alimenta push notifications
    try:
        _scan_task = asyncio.create_task(_server_scan_loop())
        logging.info(f"Server-scan iniciado (intervalo {SERVER_SCAN_INTERVAL}s, top {SERVER_SCAN_TOP_N}).")
    except Exception as e:
        logging.warning(f"Falha ao iniciar server_scan: {e}")
    yield
    for t in (_snapshot_task, _scan_task):
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    await close_db()
    await close_exchange()
    try:
        from services import binance_vision_service as _bvs
        await _bvs.close()
    except Exception:
        pass


app = FastAPI(title="Crypto AI Agent", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── REST ENDPOINTS ────────────────────────────────────────────────────────────

@app.get("/api/symbols")
async def get_symbols():
    symbols = await get_perpetual_symbols()
    return {"symbols": symbols, "count": len(symbols)}


@app.get("/api/tickers")
async def get_tickers(symbols: str = Query(..., description="Comma-separated symbols")):
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    tickers = await fetch_multiple_tickers(symbol_list)
    return {"tickers": tickers}


@app.get("/api/ohlcv")
async def get_ohlcv(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    limit: int = DEFAULT_LIMIT,
):
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {TIMEFRAMES}")
    df = await fetch_ohlcv(symbol, timeframe, limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "data": df.to_dict(orient="records"),
    }


@app.get("/api/analyze")
async def analyze(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    with_ai: bool = True,
):
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {TIMEFRAMES}")

    try:
        df = await fetch_ohlcv(symbol, timeframe, DEFAULT_LIMIT)
    except Exception as e:
        logging.error(f"fetch_ohlcv error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao buscar dados: {e}")

    if df.empty or len(df) < 50:
        raise HTTPException(400, "Not enough data for analysis")

    try:
        indicators = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        # Derivativos + MTF em paralelo (não bloqueia se falhar)
        try:
            ticker = await fetch_ticker(symbol)
            price_change_24h = ticker.get("change", 0.0)
            current_price = float(df["close"].iloc[-1])
            primary_dir = determine_direction(indicators, patterns, current_price)
            derivatives, mtf = await asyncio.gather(
                analyze_derivatives(symbol, price_change_24h),
                analyze_mtf(symbol, timeframe, primary_dir),
            )
        except Exception:
            derivatives = None
            mtf = None
        signal = build_trade_signal(symbol, timeframe, df, indicators, patterns, derivatives=derivatives, mtf=mtf)
    except Exception as e:
        logging.error(f"analysis error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro na análise: {e}")

    if with_ai:
        try:
            macro = await macro_context(symbol)
            mc_text = macro.get("context_text", "")
        except Exception:
            mc_text = ""
        analysis, critique = await generate_ai_analysis(signal, mc_text)
        signal.ai_analysis = analysis
        signal.ai_critique = critique

    return signal


class CandleData(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class AnalyzeDataRequest(BaseModel):
    symbol: str
    timeframe: str
    candles: List[CandleData]
    with_ai: bool = True


@app.post("/api/analyze-data")
async def analyze_data(body: AnalyzeDataRequest):
    """Recebe OHLCV direto do frontend (Binance browser) e retorna análise."""
    if len(body.candles) < 50:
        raise HTTPException(400, "Not enough data for analysis")

    df = pd.DataFrame([c.model_dump() for c in body.candles])
    df = df.astype({
        "timestamp": int, "open": float, "high": float,
        "low": float, "close": float, "volume": float,
    })

    try:
        indicators = calculate_indicators(df)
        patterns = detect_all_patterns(df)
        try:
            ticker = await fetch_ticker(body.symbol)
            price_change_24h = ticker.get("change", 0.0)
            current_price = float(df["close"].iloc[-1])
            primary_dir = determine_direction(indicators, patterns, current_price)
            derivatives, mtf = await asyncio.gather(
                analyze_derivatives(body.symbol, price_change_24h),
                analyze_mtf(body.symbol, body.timeframe, primary_dir),
            )
        except Exception:
            derivatives = None
            mtf = None
        signal = build_trade_signal(body.symbol, body.timeframe, df, indicators, patterns, derivatives=derivatives, mtf=mtf)
    except Exception as e:
        logging.error(f"analyze_data error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro na análise: {e}")

    if body.with_ai:
        try:
            macro = await macro_context(body.symbol)
            mc_text = macro.get("context_text", "")
        except Exception:
            mc_text = ""
        analysis, critique = await generate_ai_analysis(signal, mc_text)
        signal.ai_analysis = analysis
        signal.ai_critique = critique

    return signal


@app.get("/api/macro")
async def macro_context(symbol: str = "BTC/USDT:USDT"):
    """Retorna contexto macro: BTC + dominância + DXY + S&P500 + Nasdaq."""
    try:
        btc_dominance, btc_df, market_data = await asyncio.gather(
            get_btc_dominance(),
            fetch_ohlcv("BTC/USDT:USDT", "1d", 100),
            get_global_market_data(),
            return_exceptions=True,
        )
        dominance = btc_dominance if not isinstance(btc_dominance, Exception) else None
        btc_data = btc_df if not isinstance(btc_df, Exception) else None
        mdata = market_data if not isinstance(market_data, Exception) else {}

        btc_direction = "neutro"
        btc_rsi = btc_adx = btc_st = None
        if btc_data is not None and len(btc_data) >= 50:
            btc_ind = calculate_indicators(btc_data)
            btc_rsi = btc_ind.rsi
            btc_adx = btc_ind.adx
            btc_st = btc_ind.supertrend_direction
            from services.signal_service import determine_direction
            current = float(btc_data["close"].iloc[-1])
            dir_val = determine_direction(btc_ind, [], current)
            btc_direction = dir_val.value

        context = build_macro_context(btc_direction, btc_rsi, btc_adx, btc_st, dominance, symbol, mdata)
        return {
            "btc_direction": btc_direction,
            "btc_rsi": btc_rsi,
            "btc_adx": btc_adx,
            "btc_supertrend": btc_st,
            "btc_dominance": dominance,
            "market_data": mdata,
            "context_text": context,
        }
    except Exception as e:
        logging.error(f"macro_context error: {e}")
        return {"btc_direction": "neutro", "btc_dominance": None, "market_data": {}, "context_text": ""}


@app.get("/api/best-timeframe")
async def best_timeframe_analysis(symbol: str, with_ai: bool = False):
    """Analisa múltiplos TFs e retorna o de maior confluência."""
    tfs = ["15m", "30m", "1h", "4h", "6h", "8h", "1d"]

    async def _try_tf(tf: str):
        try:
            df = await fetch_ohlcv(symbol, tf, DEFAULT_LIMIT)
            if len(df) < 50:
                return None
            ind = calculate_indicators(df)
            pats = detect_all_patterns(df)
            sig = build_trade_signal(symbol, tf, df, ind, pats)
            # Score: confidence + pattern bonus + ADX bonus
            score = sig.confidence
            if sig.patterns:
                score += min(len(sig.patterns) * 0.05, 0.15)
            if ind.adx and ind.adx > 25:
                score += 0.10
            return (tf, sig, score)
        except Exception:
            return None

    results = await asyncio.gather(*[_try_tf(tf) for tf in tfs])
    valid = [r for r in results if r is not None]
    if not valid:
        raise HTTPException(400, "Nenhum TF com dados suficientes")

    best_tf, best_sig, best_score = max(valid, key=lambda x: x[2])
    if with_ai:
        analysis, critique = await generate_ai_analysis(best_sig)
        best_sig.ai_analysis = analysis
        best_sig.ai_critique = critique

    return {
        "best_timeframe": best_tf,
        "score": round(best_score, 3),
        "signal": best_sig,
        "all_scores": {r[0]: round(r[2], 3) for r in valid},
    }


@app.get("/api/recommendations")
async def recommendations(top_n: int = 30):
    """Trade Recommendations — varre top-N perpétuos por volume, escolhe melhor
    TF por símbolo, classifica em tiers A+/A/B. Cache 90s no service."""
    try:
        recs = await get_recommendations(top_n=min(max(top_n, 5), 50))
        return {"count": len(recs), "recommendations": [r.model_dump() for r in recs]}
    except Exception as e:
        logging.error(f"recommendations error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao gerar recomendações: {e}")


class RecommendationBatchItem(BaseModel):
    symbol: str
    timeframe: str
    candles: List[CandleData]


class RecommendationBatchRequest(BaseModel):
    items: List[RecommendationBatchItem]


@app.post("/api/recommendations-batch")
async def recommendations_batch(body: RecommendationBatchRequest):
    """Recebe candles (já baixados pelo browser da Bybit) em lote, agrupa por
    símbolo, escolhe melhor TF, classifica em tiers A+/A/B. Backend nunca
    chama a Bybit (Railway leva 403)."""
    try:
        items = [
            {"symbol": it.symbol, "timeframe": it.timeframe,
             "candles": [c.model_dump() for c in it.candles]}
            for it in body.items
        ]
        recs = await get_recommendations_from_batch(items)
        recs_dict = [r.model_dump() for r in recs]
        # Persistência (não bloqueia se DB indisponível)
        newly_saved = 0
        if DB_ENABLED and recs_dict:
            try:
                newly_saved = await save_recommendations(recs_dict) or 0
            except Exception as e:
                logging.warning(f"save_recommendations falhou (segue sem persistir): {e}")
        # Push notifications (só dispara pra recs novas — dedup feito por tag)
        if PUSH_ENABLED and newly_saved > 0:
            try:
                asyncio.create_task(notify_recommendations_batch(recs_dict, newly_saved))
            except Exception as e:
                logging.warning(f"notify push falhou: {e}")
        return {"count": len(recs), "recommendations": recs_dict}
    except Exception as e:
        logging.error(f"recommendations-batch error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao processar recomendações: {e}")


@app.get("/api/daily-pnl")
async def daily_pnl(date_str: Optional[str] = Query(None, alias="date")):
    """P&L do dia (default = hoje em UTC). Use ?date=YYYY-MM-DD pra outros dias."""
    from datetime import date as _date
    target = None
    if date_str:
        try:
            target = _date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(400, "date deve estar em formato YYYY-MM-DD")
    try:
        return await get_daily_pnl(target)
    except Exception as e:
        logging.error(f"daily-pnl error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter P&L: {e}")


@app.get("/api/learning-insights")
async def learning_insights(days: int = 60):
    """Estatísticas agregadas por bucket (tier/TF/sessão/padrão/funding/etc.)
    + combos vencedores e perdedores."""
    try:
        return await compute_stats_by_bucket(days=max(7, min(days, 365)))
    except Exception as e:
        logging.error(f"learning-insights error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter insights: {e}")


class HistoricalLookupItem(BaseModel):
    tier: str
    timeframe: str
    direction: str


class HistoricalLookupRequest(BaseModel):
    items: List[HistoricalLookupItem]
    days: int = 60


@app.post("/api/historical-lookup")
async def historical_lookup(body: HistoricalLookupRequest):
    """Recebe [{tier, tf, direction}, ...] e retorna stat histórico de cada.
    Usado pelo painel de recomendações pra exibir badge "histórico" no card."""
    try:
        keys = [{"tier": it.tier, "timeframe": it.timeframe, "direction": it.direction} for it in body.items]
        return await lookup_historical_batch(keys, days=max(7, min(body.days, 365)))
    except Exception as e:
        logging.error(f"historical-lookup error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao buscar histórico: {e}")


@app.get("/api/history-stats")
async def history_stats(days: int = 30):
    """Stats agregadas dos últimos N dias — alimenta planejador da banca."""
    try:
        return await get_history_stats(days=max(7, min(days, 180)))
    except Exception as e:
        logging.error(f"history-stats error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter stats: {e}")


@app.get("/api/debug/vision-pipeline")
async def debug_vision_pipeline():
    """Roda cada estágio do pipeline Vision e reporta onde quebra."""
    from services import binance_vision_service as bvs
    from services.recommendation_service import (
        _analyze_symbol_tf_via_vision, _compute_score, _classify_tier, SCAN_TFS,
    )
    from models.trade_signal import SignalDirection

    stages: Dict[str, Any] = {}

    # 1. Top symbols
    try:
        symbols = await bvs.fetch_top_volume_symbols(limit=10)
        stages["fetch_top_volume_symbols"] = {"ok": True, "count": len(symbols), "sample": symbols[:5]}
    except Exception as e:
        stages["fetch_top_volume_symbols"] = {"ok": False, "error": str(e)[:300]}
        return stages

    if not symbols:
        return stages

    # 2. OHLCV de um símbolo
    test_sym = symbols[0]
    try:
        df = await bvs.fetch_ohlcv(test_sym, "1h", 100)
        stages["fetch_ohlcv"] = {"ok": True, "symbol": test_sym, "rows": len(df),
                                  "last_close": float(df["close"].iloc[-1]) if len(df) else None}
    except Exception as e:
        stages["fetch_ohlcv"] = {"ok": False, "error": str(e)[:300]}
        return stages

    # 3. Análise por símbolo: filtra estágio a estágio
    per_symbol_results = []
    for sym in symbols[:10]:
        sym_info: Dict[str, Any] = {"symbol": sym, "tfs": {}}
        for tf in SCAN_TFS:
            try:
                sig = await _analyze_symbol_tf_via_vision(sym, tf)
                if sig is None:
                    sym_info["tfs"][tf] = {"signal": None}
                    continue
                score = _compute_score(sig)
                tier = _classify_tier(sig, score)
                sym_info["tfs"][tf] = {
                    "direction": sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction),
                    "confidence": round(sig.confidence, 2),
                    "rr": sig.risk_reward,
                    "score": score,
                    "tier": tier,
                    "neutral": sig.direction == SignalDirection.NEUTRAL,
                }
            except Exception as e:
                sym_info["tfs"][tf] = {"error": str(e)[:200]}
        per_symbol_results.append(sym_info)

    stages["per_symbol"] = per_symbol_results

    # Resumo
    tier_counts = {"A+": 0, "A": 0, "B": 0, "None": 0}
    rr_distribution = []
    for s in per_symbol_results:
        for tf, info in s["tfs"].items():
            if "tier" in info:
                key = info["tier"] if info["tier"] else "None"
                tier_counts[key] = tier_counts.get(key, 0) + 1
                if info.get("rr") is not None:
                    rr_distribution.append(info["rr"])
    stages["summary"] = {
        "tier_counts": tier_counts,
        "rr_min": min(rr_distribution) if rr_distribution else None,
        "rr_max": max(rr_distribution) if rr_distribution else None,
        "rr_avg": round(sum(rr_distribution)/len(rr_distribution), 2) if rr_distribution else None,
    }
    return stages


@app.get("/api/debug/binance-reachability")
async def debug_binance_reachability():
    """
    Testa se algum endpoint Binance/alternativo responde do Railway.
    Útil pra escolher fonte de dados pro server-scan.
    """
    import httpx
    targets = [
        # Binance Futures (provavelmente bloqueado)
        ("fapi.binance.com", "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        ("fapi1.binance.com", "https://fapi1.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        ("fapi2.binance.com", "https://fapi2.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        ("fapi3.binance.com", "https://fapi3.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        # Binance Spot
        ("api.binance.com", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        ("api1.binance.com", "https://api1.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        # Binance Data (sem geo-block na teoria)
        ("data-api.binance.vision", "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"),
        # Bybit
        ("api.bybit.com", "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=60&limit=2"),
        # OKX (controle — sabemos que funciona)
        ("www.okx.com", "https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1H&limit=2"),
    ]
    results = []
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for name, url in targets:
            try:
                r = await client.get(url)
                preview = r.text[:120]
                results.append({
                    "host": name, "status": r.status_code,
                    "ok": r.status_code == 200,
                    "preview": preview,
                })
            except Exception as e:
                results.append({"host": name, "status": None, "ok": False, "error": str(e)[:200]})
    return {"results": results}


@app.post("/api/push/test-scan")
async def push_test_scan():
    """
    DIAGNÓSTICO: dispara uma varredura server-side AGORA (via Binance Vision)
    e retorna o que achou. Útil pra verificar por que o loop não está mandando push.
    """
    try:
        recs = await get_recommendations_via_vision(top_n=SERVER_SCAN_TOP_N)
    except Exception as e:
        raise HTTPException(500, f"varredura falhou: {e}")

    recs_dict = [r.model_dump() for r in recs]
    by_tier = {"A+": 0, "A": 0, "B": 0}
    for r in recs_dict:
        t = r.get("tier", "")
        if t in by_tier:
            by_tier[t] += 1

    pushable = [r for r in recs_dict if r.get("tier") in ("A+", "A")]

    newly_saved = 0
    if DB_ENABLED and recs_dict:
        try:
            newly_saved = await save_recommendations(recs_dict) or 0
        except Exception as e:
            logging.warning(f"test-scan save falhou: {e}")

    sent = 0
    if PUSH_ENABLED and newly_saved > 0 and pushable:
        try:
            sent = await notify_recommendations_batch(pushable, len(pushable))
        except Exception as e:
            logging.warning(f"test-scan push falhou: {e}")

    return {
        "push_enabled": PUSH_ENABLED,
        "db_enabled": DB_ENABLED,
        "total_recs": len(recs),
        "by_tier": by_tier,
        "pushable_count": len(pushable),
        "newly_saved": newly_saved,
        "pushes_sent": sent,
        "top_samples": [
            {"symbol": r["symbol"], "tier": r["tier"], "tf": r["timeframe"],
             "rr": r["risk_reward"], "score": r["score"], "direction": r["direction"]}
            for r in recs_dict[:10]
        ],
    }


@app.get("/api/push/vapid-public-key")
async def push_vapid_public_key():
    """Frontend usa esta chave pra gerar a subscription do PushManager."""
    key = push_get_public_key()
    return {"enabled": PUSH_ENABLED, "public_key": key or ""}


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: Dict[str, str]
    user_agent: Optional[str] = None
    filters: Optional[Dict[str, bool]] = None


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSubscribeRequest):
    if not PUSH_ENABLED:
        raise HTTPException(503, "Push não habilitado no backend (VAPID_* ausentes ou DB off).")
    p256dh = body.keys.get("p256dh", "")
    auth = body.keys.get("auth", "")
    if not (body.endpoint and p256dh and auth):
        raise HTTPException(400, "endpoint, keys.p256dh e keys.auth são obrigatórios")
    ok = await push_save_subscription(
        endpoint=body.endpoint, p256dh=p256dh, auth=auth,
        user_agent=body.user_agent, filters=body.filters or {},
    )
    return {"ok": ok}


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribeRequest):
    ok = await push_remove_subscription(body.endpoint)
    return {"ok": ok}


@app.get("/api/multi-timeframe")
async def multi_timeframe_analysis(symbol: str, with_ai: bool = False):
    results = {}
    for tf in ["15m", "30m", "1h", "4h", "8h", "1d"]:
        try:
            df = await fetch_ohlcv(symbol, tf, DEFAULT_LIMIT)
            if len(df) >= 50:
                ind = calculate_indicators(df)
                patterns = detect_all_patterns(df)
                signal = build_trade_signal(symbol, tf, df, ind, patterns)
                results[tf] = signal
        except Exception:
            pass
    return results


@app.get("/api/trades/{user_id}")
async def load_trades(user_id: str):
    """Carrega trades sincronizados do usuário."""
    return {"trades": get_trades(user_id)}


@app.post("/api/trades/{user_id}")
async def sync_trades(user_id: str, body: dict):
    """Sincroniza (sobrescreve) todos os trades do usuário."""
    trades = body.get("trades", [])
    save_trades(user_id, trades)
    return {"ok": True, "count": len(trades)}


@app.post("/api/validate-drawing")
async def validate_drawing(body: dict):
    """IA valida padrões desenhados pelo usuário no gráfico."""
    symbol = body.get("symbol", "UNKNOWN")
    timeframe = body.get("timeframe", "1h")
    drawings = body.get("drawings", [])

    if not drawings:
        return {"analysis": "Nenhum desenho encontrado. Desenhe linhas no gráfico e tente novamente."}

    # Describe drawings in natural language
    desc_lines = []
    for d in drawings:
        dtype = d.get("type", "")
        if dtype == "hline":
            price = d.get("price", 0)
            label = d.get("label", "")
            desc_lines.append(f"• Linha horizontal em {price:.6g} ({label})")
        elif dtype == "trendline":
            p1 = d.get("p1", {})
            p2 = d.get("p2", {})
            price1 = p1.get("price", 0)
            price2 = p2.get("price", 0)
            direction = "ascendente" if price2 > price1 else "descendente"
            desc_lines.append(
                f"• Linha de tendência {direction}: de {price1:.6g} para {price2:.6g}"
            )
        elif dtype == "fibonacci":
            p1 = d.get("p1", {})
            p2 = d.get("p2", {})
            high_price = max(p1.get("price", 0), p2.get("price", 0))
            low_price = min(p1.get("price", 0), p2.get("price", 0))
            desc_lines.append(
                f"• Fibonacci: de {low_price:.6g} até {high_price:.6g} (range: {(high_price-low_price):.6g})"
            )
        elif dtype == "rectangle":
            p1 = d.get("p1", {})
            p2 = d.get("p2", {})
            high_price = max(p1.get("price", 0), p2.get("price", 0))
            low_price = min(p1.get("price", 0), p2.get("price", 0))
            desc_lines.append(
                f"• Retângulo entre {low_price:.6g} e {high_price:.6g}"
            )

    drawings_desc = "\n".join(desc_lines)

    if not GROQ_API_KEY and not ANTHROPIC_API_KEY:
        return {
            "analysis": (
                f"Desenhos detectados em {symbol} ({timeframe}):\n\n{drawings_desc}\n\n"
                "ℹ️ Análise IA não disponível. Configure a variável GROQ_API_KEY para habilitar a validação inteligente de padrões."
            )
        }

    prompt = f"""Você é um analista técnico sênior. O trader marcou os seguintes níveis/estruturas no gráfico de {symbol} (timeframe {timeframe}):

{drawings_desc}

Analise com precisão e siga esta estrutura OBRIGATÓRIA em português claro:

---
🔍 IDENTIFICAÇÃO DO PADRÃO
Diga exatamente o que esses desenhos representam: suporte/resistência, linha de tendência, canal, triângulo, topo/fundo duplo, range, retração de Fibonacci, etc. Seja específico — indique se é bullish ou bearish e por quê.

---
✅ CONCORDO / ❌ NÃO CONCORDO / ⏳ AGUARDAR
Declare claramente se concorda com o setup desenhado.
- Se CONCORDO: explique a consistência técnica (quantas vezes o nível foi testado, força da estrutura).
- Se NÃO CONCORDO: explique o erro técnico e NÃO gere o setup.
- Se AGUARDAR: diga exatamente o que precisa acontecer (ex: "aguardar fechamento acima de X").

---
📊 CONFLUÊNCIAS (somente se concordar)
Liste 2–4 fatores que CONFIRMAM o setup. Use estruturas reais dos preços fornecidos, como:
• "O nível X.XX coincide com retração 61.8% de Fibonacci do movimento anterior"
• "Zona de suporte testada múltiplas vezes → alta probabilidade de reação"
• "Linha de tendência ascendente intacta há N candles"
• "Nível confluente com média móvel de 50/200 períodos"
• "RSI em sobrevenda → pressão compradora provável"

---
🎯 SETUP COMPLETO (somente se concordar)

Direção: LONG / SHORT
Tipo: Scalp / Day Trade / Swing

📍 Entrada: [preço exato]
Motivo: [ex: rompimento confirmado de X.XX com fechamento acima / teste de suporte em X.XX / pullback para linha de tendência]

🛑 Stop Loss: [preço exato]
Motivo OBRIGATÓRIO — escolha o mais adequado:
• "Abaixo do fundo anterior em [X.XX]" — invalida a estrutura de alta
• "Abaixo da linha de suporte/tendência que serviu de base"
• "Abaixo do nível 61.8% de Fibonacci em [X.XX]" — invalidação da retração
• "Abaixo da zona de suporte/resistência histórica em [X.XX]"
• "Acima do topo anterior em [X.XX]" (para SHORT)
Risco em %: [X.XX%] do capital na posição

🎯 Alvo 1: [preço exato] — probabilidade ~X%
Motivo OBRIGATÓRIO:
• "Resistência anterior testada em [X.XX]"
• "Extensão 127.2% de Fibonacci em [X.XX]"
• "Topo anterior / zona de supply em [X.XX]"
• "Primeira resistência significativa em [X.XX]"

🎯 Alvo 2: [preço exato] — probabilidade ~X%
Motivo: [estrutura técnica que justifica — resistência, Fibonacci 161.8%, topo histórico, etc.]

🎯 Alvo 3: [preço exato] — probabilidade ~X% (se o setup tiver potencial estendido)
Motivo: [extensão máxima do movimento baseada em estrutura]

📐 Risco/Retorno: 1:[X] — [aceitável ≥ 1:2 / excelente ≥ 1:3]

---
🚦 RECOMENDAÇÃO FINAL
OPERAR AGORA / AGUARDAR CONFIRMAÇÃO / NÃO OPERAR

Se aguardar: "Aguardar [evento específico] em [timeframe] antes de entrar."
Se não operar: "Risco principal: [motivo concreto]."

Seja direto, objetivo e sempre justifique cada nível com estrutura de mercado real."""

    try:
        from services.ai_service import call_ai
        result = await call_ai(system="Você é um analista técnico sênior de criptomoedas.", user=prompt, max_tokens=1200)
        return {"analysis": result}
    except Exception as e:
        logging.error(f"validate_drawing error: {e}")
        return {
            "analysis": (
                f"Desenhos em {symbol} ({timeframe}):\n\n{drawings_desc}\n\n"
                f"Erro ao chamar IA: {str(e)[:200]}"
            )
        }


@app.post("/api/nlp-coach")
async def nlp_coach(body: dict):
    """Coach de PNL em tempo real para gestão emocional do trader."""
    estado = body.get("estado", "calmo")
    intensidade = body.get("intensidade", 3)
    contexto = body.get("contexto", "")
    historico = body.get("historico", [])

    historico_txt = ""
    if historico:
        historico_txt = "\n\nHistórico emocional desta sessão:\n" + "\n".join(
            f"• {h.get('hora', '')} — {h.get('estado', '')} (intensidade {h.get('intensidade', '')})"
            for h in historico[-5:]
        )

    prompt = f"""Você é um coach especialista em Programação Neurolinguística (PNL) aplicada ao trading. Seu papel é ajudar o trader a gerenciar seu estado emocional em tempo real, usando técnicas de PNL comprovadas.

ESTADO ATUAL DO TRADER:
• Emoção: {estado}
• Intensidade: {intensidade}/5
• Contexto: {contexto if contexto else "Não informado"}{historico_txt}

Responda em português com esta estrutura OBRIGATÓRIA:

---
🧠 DIAGNÓSTICO DO ESTADO
Em 1-2 frases, identifique o que está acontecendo neurologicamente e como isso afeta as decisões de trading agora. Seja direto e empático.

---
⚡ TÉCNICA DE PNL IMEDIATA
Nome da técnica (ex: Ancoragem, Reencadramento, Dissociação, Rapport Interno, Swish Pattern, etc.)

Passo a passo (máx 4 passos curtos):
1. [ação física ou mental específica — 10-30 segundos]
2. ...
3. ...
4. [resultado esperado]

---
💡 REENCADRAMENTO PARA TRADING
Uma frase poderosa que muda a perspectiva agora. Exemplos de estrutura:
• "Em vez de ver [problema], veja [oportunidade]"
• "Traders profissionais usam [este estado] como sinal para [ação]"
• "Este momento de [emoção] é exatamente quando [insight]"

---
🎯 AÇÃO PRÁTICA AGORA
O que fazer com o trading nos próximos 5-15 minutos:
• OPERAR / PAUSAR / REDUZIR TAMANHO / FECHAR POSIÇÕES
• Justificativa curta baseada no estado emocional atual

---
🔋 AFIRMAÇÃO DE ESTADO
Uma afirmação em 1ª pessoa, presente, positiva e específica para trading. (máx 2 linhas)

Linguagem direta, calorosa e profissional. Máximo 300 palavras."""

    if not GROQ_API_KEY and not ANTHROPIC_API_KEY:
        return {
            "coaching": (
                f"Estado: {estado} (intensidade {intensidade}/5)\n\n"
                "ℹ️ Coaching IA não disponível. Configure GROQ_API_KEY para habilitar o coach de PNL."
            )
        }

    try:
        from services.ai_service import call_ai
        result = await call_ai(
            system="Você é um coach especialista em Programação Neurolinguística (PNL) aplicada ao trading.",
            user=prompt,
            max_tokens=800,
        )
        return {"coaching": result}
    except Exception as e:
        logging.error(f"nlp_coach error: {e}")
        return {"coaching": f"Erro ao gerar coaching: {str(e)[:200]}"}


@app.get("/api/market-data")
async def market_data(symbol: str):
    ticker, funding, oi = await asyncio.gather(
        fetch_ticker(symbol),
        fetch_funding_rate(symbol),
        fetch_open_interest(symbol),
        return_exceptions=True,
    )
    return {
        "ticker": ticker if isinstance(ticker, dict) else {},
        "funding_rate": funding if not isinstance(funding, Exception) else None,
        "open_interest": oi if not isinstance(oi, Exception) else None,
    }


@app.get("/api/watchlist/analyze")
async def watchlist_analyze(
    symbols: str = Query(...),
    timeframe: str = DEFAULT_TIMEFRAME,
):
    """Analyze multiple symbols and return quick signals."""
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()][:20]
    results = []
    for sym in symbol_list:
        try:
            df = await fetch_ohlcv(sym, timeframe, 150)
            if len(df) >= 50:
                ind = calculate_indicators(df)
                patterns = detect_all_patterns(df)
                sig = build_trade_signal(sym, timeframe, df, ind, patterns)
                results.append({
                    "symbol": sym,
                    "direction": sig.direction,
                    "confidence": sig.confidence,
                    "signal_strength": sig.signal_strength,
                    "trade_type": sig.trade_type,
                    "rsi": ind.rsi,
                    "patterns_count": len(patterns),
                })
        except Exception:
            pass
    return {"results": results}


# ─── WEBSOCKET ──────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, symbol: str):
        await ws.accept()
        self.active.setdefault(symbol, []).append(ws)

    def disconnect(self, ws: WebSocket, symbol: str):
        if symbol in self.active:
            self.active[symbol] = [w for w in self.active[symbol] if w != ws]

    async def broadcast(self, symbol: str, data: dict):
        if symbol not in self.active:
            return
        dead = []
        for ws in self.active[symbol]:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws, symbol)


manager = ConnectionManager()


@app.websocket("/ws/price/{symbol}")
async def websocket_price(websocket: WebSocket, symbol: str):
    await manager.connect(websocket, symbol)
    try:
        while True:
            try:
                ticker = await fetch_ticker(symbol)
                await websocket.send_json({"type": "ticker", "data": ticker})
            except Exception:
                pass
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        manager.disconnect(websocket, symbol)


@app.websocket("/ws/analysis/{symbol}")
async def websocket_analysis(websocket: WebSocket, symbol: str, timeframe: str = "1h"):
    await manager.connect(websocket, symbol)
    try:
        while True:
            try:
                tf = timeframe
                df = await fetch_ohlcv(symbol, tf, DEFAULT_LIMIT)
                if len(df) >= 50:
                    ind = calculate_indicators(df)
                    patterns = detect_all_patterns(df)
                    signal = build_trade_signal(symbol, tf, df, ind, patterns)
                    await websocket.send_json({
                        "type": "analysis",
                        "data": signal.model_dump(),
                    })
            except Exception:
                pass
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        manager.disconnect(websocket, symbol)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
