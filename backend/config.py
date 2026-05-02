import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "6h", "8h", "12h", "1d", "3d"]
DEFAULT_TIMEFRAME = "1d"
DEFAULT_LIMIT = 300

TRADE_TYPE_THRESHOLDS = {
    "scalp":     {"timeframes": ["1m", "5m", "15m", "30m"], "atr_mult": 0.5},
    "day_trade": {"timeframes": ["1h", "4h"],                "atr_mult": 1.5},
    "swing":     {"timeframes": ["6h", "8h", "12h", "1d"],   "atr_mult": 3.0},
    "hodl":      {"timeframes": ["3d"],                       "atr_mult": 8.0},
}
