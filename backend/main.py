import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import List, Optional

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
from models.trade_signal import TradeSignal


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_exchange()


app = FastAPI(title="Crypto AI Agent", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

    df = await fetch_ohlcv(symbol, timeframe, DEFAULT_LIMIT)
    if df.empty or len(df) < 50:
        raise HTTPException(400, "Not enough data for analysis")

    indicators = calculate_indicators(df)
    patterns = detect_all_patterns(df)
    signal = build_trade_signal(symbol, timeframe, df, indicators, patterns)

    if with_ai:
        signal.ai_analysis = await generate_ai_analysis(signal)

    return signal


@app.get("/api/multi-timeframe")
async def multi_timeframe_analysis(symbol: str, with_ai: bool = False):
    results = {}
    for tf in ["15m", "1h", "4h", "1d"]:
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
