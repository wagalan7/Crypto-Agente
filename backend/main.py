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

from config import TIMEFRAMES, DEFAULT_TIMEFRAME, DEFAULT_LIMIT
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
from services.signal_service import build_trade_signal
from services.ai_service import generate_ai_analysis
from services.trade_service import get_trades, save_trades
from services.macro_service import get_btc_dominance, build_macro_context, get_global_market_data
from models.trade_signal import TradeSignal


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_exchange()


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
        signal = build_trade_signal(symbol, timeframe, df, indicators, patterns)
    except Exception as e:
        logging.error(f"analysis error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro na análise: {e}")

    if with_ai:
        try:
            macro = await macro_context(symbol)
            mc_text = macro.get("context_text", "")
        except Exception:
            mc_text = ""
        signal.ai_analysis = await generate_ai_analysis(signal, mc_text)

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
        signal = build_trade_signal(body.symbol, body.timeframe, df, indicators, patterns)
    except Exception as e:
        logging.error(f"analyze_data error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro na análise: {e}")

    if body.with_ai:
        try:
            macro = await macro_context(body.symbol)
            mc_text = macro.get("context_text", "")
        except Exception:
            mc_text = ""
        signal.ai_analysis = await generate_ai_analysis(signal, mc_text)

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
        best_sig.ai_analysis = await generate_ai_analysis(best_sig)

    return {
        "best_timeframe": best_tf,
        "score": round(best_score, 3),
        "signal": best_sig,
        "all_scores": {r[0]: round(r[2], 3) for r in valid},
    }


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
