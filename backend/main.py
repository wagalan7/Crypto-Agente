import asyncio
import json
import os
import time
import traceback
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

logging.basicConfig(level=logging.INFO)

# ── Sentry (opcional, no-op se SENTRY_DSN não setado) ────────────────────────
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.getenv("RAILWAY_ENVIRONMENT", "production"),
            release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown")[:7],
            traces_sample_rate=0.05,         # 5% trace sampling
            profiles_sample_rate=0.0,
            integrations=[
                FastApiIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        logging.info(f"[sentry] habilitado (env={os.getenv('RAILWAY_ENVIRONMENT', 'production')})")
    except ImportError:
        logging.warning("[sentry] SENTRY_DSN setado mas sentry-sdk não instalado")
    except Exception as e:
        logging.warning(f"[sentry] init falhou: {e}")
else:
    logging.info("[sentry] desabilitado (SENTRY_DSN não setado)")

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Header
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
from services.macro_service import get_btc_dominance, build_macro_context, get_global_market_data, get_crypto_totals
from services.recommendation_service import (
    get_recommendations,
    get_recommendations_from_batch,
    get_recommendations_via_vision,
    get_recommendations_cached_for_api,
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
from services.calibration_service import get_calibration
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
_trade_manager_task: Optional[asyncio.Task] = None
_recalibration_task: Optional[asyncio.Task] = None

SERVER_SCAN_INTERVAL = 90         # 1.5 min entre varreduras server-side (era 3min — push ainda chegava com atraso perceptível quando painel fechado vs aberto)
# Quantos símbolos varrer por ciclo (top-N por volume). Universo maior = mais
# setups bons encontrados SEM baixar a régua de qualidade (cada um ainda passa
# pelos mesmos filtros/tiers). Era 40 fixo → 60 default, ajustável via env
# (SERVER_SCAN_TOP_N) sem deploy. Se o ciclo ficar lento, baixar de volta.
SERVER_SCAN_TOP_N = int(os.getenv("SERVER_SCAN_TOP_N", "60"))
SERVER_SCAN_INITIAL_DELAY = 45    # espera 45s após startup pra não competir com init

# Intervalo de checagem de snapshots abertos (detecção de TP1/TP2/SL → push).
# Era 300s (5min) — esse era o MAIOR atraso percebido nos pushes de saída: o
# preço batia o alvo e a notificação só saía no próximo ciclo. 90s entrega o
# push de TP/SL em ~1.5min. Reversível via env sem deploy (SNAPSHOT_CHECK_INTERVAL_SEC).
SNAPSHOT_CHECK_INTERVAL = int(os.getenv("SNAPSHOT_CHECK_INTERVAL_SEC", "90"))

# ── Métricas operacionais (lidas via /api/health) ────────────────────────────
_METRICS: Dict[str, Any] = {
    "startup_at": datetime.now(timezone.utc).isoformat(),
    "last_scan_at": None,
    "last_scan_ok": None,             # True / False / None
    "last_scan_error": None,
    "scans_total": 0,
    "scans_failed": 0,
    "recs_last_scan": 0,
    "recs_a_plus_last_scan": 0,
    "recs_a_last_scan": 0,
    "pushes_sent_total": 0,
    "pushes_sent_last_scan": 0,
    "last_snapshot_check_at": None,
    "last_recalibration_at": None,
}

# ── Recalibração automática da autoaprendizagem ─────────────────────────────
# A cada N dias o app reaprende com TODO o histórico (score→P(TP1) + buckets).
# Robusto a restart: o "relógio" vem do timestamp persistido da última
# recalibração no DB (não de um timer em memória).
RECALIBRATION_INTERVAL_DAYS = int(os.getenv("RECALIBRATION_INTERVAL_DAYS", "10"))
RECALIBRATION_CHECK_INTERVAL = int(os.getenv("RECALIBRATION_CHECK_INTERVAL_SEC", str(3600)))
RECALIBRATION_INITIAL_DELAY = 120  # espera 2min após boot pra não competir com init
RECALIBRATION_ENABLED = os.getenv("RECALIBRATION_AUTO_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Modo de agendamento: "weekly" (dia/hora fixos) ou "interval" (a cada N dias).
# Default weekly: toda segunda às 09h de Brasília (= 12h UTC, BRT é UTC-3, sem DST).
RECALIBRATION_MODE = os.getenv("RECALIBRATION_MODE", "weekly").strip().lower()
RECALIBRATION_WEEKLY_DOW = int(os.getenv("RECALIBRATION_WEEKLY_DOW", "0"))   # 0=segunda (Python weekday)
RECALIBRATION_WEEKLY_HOUR_UTC = int(os.getenv("RECALIBRATION_WEEKLY_HOUR_UTC", "12"))  # 12 UTC = 09h BRT


def _last_scheduled_occurrence(now: datetime, dow: int, hour: int) -> datetime:
    """Datetime mais recente (<= now) que cai no weekday `dow` às `hour`:00 UTC."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    target -= timedelta(days=(now.weekday() - dow) % 7)
    if target > now:
        target -= timedelta(days=7)
    return target


async def _snapshot_loop():
    """Roda check_open_snapshots a cada SNAPSHOT_CHECK_INTERVAL segundos (default 90s)."""
    while True:
        try:
            await asyncio.sleep(SNAPSHOT_CHECK_INTERVAL)
            await check_open_snapshots()
            _METRICS["last_snapshot_check_at"] = datetime.now(timezone.utc).isoformat()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"snapshot_loop error: {e}")


async def _recalibration_loop():
    """
    Recalibração automática a cada RECALIBRATION_INTERVAL_DAYS dias.

    Checa periodicamente (a cada RECALIBRATION_CHECK_INTERVAL) quando foi a
    última recalibração persistida no DB; se passou do intervalo, dispara
    `recalibrate()` (reaprende com todo o histórico + versiona). Baseado em
    timestamp do banco → sobrevive a restarts do Railway sem refazer cedo
    demais nem ficar "preso" se o processo cair.
    """
    if not DB_ENABLED or not RECALIBRATION_ENABLED:
        logging.info(
            f"[recalibration] loop desativado (DB={DB_ENABLED} enabled={RECALIBRATION_ENABLED})"
        )
        return
    await asyncio.sleep(RECALIBRATION_INITIAL_DELAY)
    from services import calibration_versions_service as cvs
    while True:
        try:
            last = await cvs.last_recalibration_at()
            now = datetime.now(timezone.utc)
            if RECALIBRATION_MODE == "weekly":
                occ = _last_scheduled_occurrence(
                    now, RECALIBRATION_WEEKLY_DOW, RECALIBRATION_WEEKLY_HOUR_UTC
                )
                due = last is None or last < occ
                notes = "auto-recalibração semanal (seg 09h BRT)"
            else:
                due = last is None or (now - last) >= timedelta(days=RECALIBRATION_INTERVAL_DAYS)
                notes = f"auto-recalibração {RECALIBRATION_INTERVAL_DAYS}d"
            if due:
                logging.info(
                    f"[recalibration] disparando recalibração automática "
                    f"(modo={RECALIBRATION_MODE}, última: {last})"
                )
                result = await cvs.recalibrate(notes=notes)
                if result.get("recalibrated"):
                    _METRICS["last_recalibration_at"] = now.isoformat()
                    ver = (result.get("version") or {}).get("version")
                    logging.info(
                        f"[recalibration] ✅ ok — n={result.get('total_resolved')} "
                        f"p_global={result.get('p_global')} versão={ver}"
                    )
                else:
                    logging.info(f"[recalibration] adiada: {result.get('reason')}")
            else:
                if RECALIBRATION_MODE == "weekly":
                    next_due = occ + timedelta(days=7)
                else:
                    next_due = last + timedelta(days=RECALIBRATION_INTERVAL_DAYS)
                logging.info(f"[recalibration] ainda no prazo — próxima ~{next_due.isoformat()}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"[recalibration] erro: {e}", exc_info=True)
        try:
            await asyncio.sleep(RECALIBRATION_CHECK_INTERVAL)
        except asyncio.CancelledError:
            break


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

            # Warmup calibration cache ANTES de gerar recs (preenche prob_tp1)
            try:
                await get_calibration()
            except Exception as e:
                logging.warning(f"[server-scan] calibration warmup falhou: {e}")

            recs = await get_recommendations_via_vision(top_n=SERVER_SCAN_TOP_N)
            recs_dict = [r.model_dump() for r in recs]

            # Alimenta o cache do endpoint /api/recommendations com o resultado deste
            # scan → o app passa a mostrar EXATAMENTE as recs que o bot gerou (mesma
            # fonte Binance), sem o app precisar bater na Bybit pelo celular.
            try:
                from services.recommendation_service import set_api_recommendations_cache
                set_api_recommendations_cache(recs)
            except Exception as e:
                logging.warning(f"[server-scan] set api cache falhou: {e}")

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
            pushable = [r for r in recs_dict if r.get("tier") in ("A+", "A", "B")]

            newly_saved = 0
            if DB_ENABLED and recs_dict:
                try:
                    newly_saved = await save_recommendations(recs_dict) or 0
                except Exception as e:
                    logging.warning(f"[server-scan] save falhou: {e}")

                # Shadow #11.3: abre trades sombra pras recs novas (A/A+)
                try:
                    from services.shadow_trade_service import open_shadow_for_recs
                    await open_shadow_for_recs(recs_dict)
                except Exception as e:
                    logging.warning(f"[server-scan] shadow open falhou: {e}")

            logging.info(
                f"[server-scan] {newly_saved}/{len(recs_dict)} novas (dedup 2h), "
                f"{len(pushable)} elegíveis pra push"
            )

            # Heartbeat (#6): bate antes do circuit breaker pra sinalizar
            # que o loop está vivo mesmo se nenhuma rec for emitida
            try:
                from services import heartbeat_service
                await heartbeat_service.tick("server-scan")
            except Exception as e:
                logging.warning(f"[server-scan] heartbeat falhou: {e}")

            # Circuit breaker: atualiza DD e checa se deve pausar push
            try:
                from services import risk_service
                risk_snapshot = await risk_service.update_and_check()
                trading_paused = bool(risk_snapshot.get("trading_paused"))
            except Exception as e:
                logging.warning(f"[server-scan] risk_service falhou: {e}")
                trading_paused = False
                risk_snapshot = {}

            pushes_this_scan = 0
            if trading_paused:
                logging.warning(
                    f"[server-scan] 🛑 push BLOQUEADO — circuit breaker "
                    f"({risk_snapshot.get('pause_reason', 'pause')})"
                )
            elif _PE and newly_saved > 0 and pushable:
                try:
                    pushes_this_scan = await notify_recommendations_batch(pushable, len(pushable))
                    logging.info(f"[server-scan] ✅ {pushes_this_scan} push(es) enviados")
                except Exception as e:
                    logging.warning(f"[server-scan] push falhou: {e}")
            else:
                reason = []
                if not _PE: reason.append("push OFF")
                if newly_saved == 0: reason.append("nada novo")
                if not pushable: reason.append("nenhum A+/A")
                logging.info(f"[server-scan] sem push enviado ({', '.join(reason) or '?'})")

            # Atualiza métricas operacionais (lidas via /api/health)
            _METRICS["last_scan_at"] = datetime.now(timezone.utc).isoformat()
            _METRICS["last_scan_ok"] = True
            _METRICS["last_scan_error"] = None
            _METRICS["scans_total"] += 1
            _METRICS["recs_last_scan"] = len(recs_dict)
            _METRICS["recs_a_plus_last_scan"] = by_tier["A+"]
            _METRICS["recs_a_last_scan"] = by_tier["A"]
            _METRICS["pushes_sent_last_scan"] = pushes_this_scan
            _METRICS["pushes_sent_total"] += pushes_this_scan
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.warning(f"[server-scan] erro: {e}", exc_info=True)
            _METRICS["last_scan_at"] = datetime.now(timezone.utc).isoformat()
            _METRICS["last_scan_ok"] = False
            _METRICS["last_scan_error"] = str(e)[:300]
            _METRICS["scans_failed"] += 1

        try:
            await asyncio.sleep(SERVER_SCAN_INTERVAL)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _snapshot_task, _scan_task, _trade_manager_task, _recalibration_task
    # Banner de segurança go-live — shadow vs live, produção vs demo, canary.
    try:
        from services import shadow_trade_service
        shadow_trade_service.log_boot_safety_banner()
    except Exception as e:
        logging.warning(f"Falha no boot-safety banner: {e}")
    if DB_ENABLED:
        try:
            await init_db()
            _snapshot_task = asyncio.create_task(_snapshot_loop())
            logging.info("Snapshot tracker iniciado (intervalo 5 min).")
            # go-live #4 — reconciliação posições exchange↔DB no boot (só loga drift)
            async def _reconcile_once():
                try:
                    from services import shadow_trade_service
                    await shadow_trade_service.reconcile_open_positions()
                except Exception as e:
                    logging.warning(f"Reconciliação de boot falhou: {e}")
            asyncio.create_task(_reconcile_once())
        except Exception as e:
            logging.error(f"Falha ao inicializar DB: {e}")
    # Recalibração automática da autoaprendizagem (a cada N dias)
    try:
        _recalibration_task = asyncio.create_task(_recalibration_loop())
        logging.info(
            f"Recalibração automática iniciada (a cada {RECALIBRATION_INTERVAL_DAYS}d, "
            f"enabled={RECALIBRATION_ENABLED})."
        )
    except Exception as e:
        logging.warning(f"Falha ao iniciar recalibração automática: {e}")
    # Varredura server-side (OKX) — alimenta push notifications
    try:
        _scan_task = asyncio.create_task(_server_scan_loop())
        logging.info(f"Server-scan iniciado (intervalo {SERVER_SCAN_INTERVAL}s, top {SERVER_SCAN_TOP_N}).")
    except Exception as e:
        logging.warning(f"Falha ao iniciar server_scan: {e}")
    # Trade manager — gerencia bracket TP1/TP2 + breakeven pós-TP1 (Fase 2)
    try:
        from services import trade_manager_service
        _trade_manager_task = asyncio.create_task(trade_manager_service.loop())
        logging.info("Trade manager iniciado.")
    except Exception as e:
        logging.warning(f"Falha ao iniciar trade_manager: {e}")
    yield
    for t in (_snapshot_task, _scan_task, _trade_manager_task, _recalibration_task):
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
    try:
        from services import binance_futures_service as _bfs
        await _bfs.close()
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

@app.get("/api/health")
async def health():
    """
    Healthcheck operacional — usado por UptimeRobot / dashboards.

    Status:
      • ok        → último scan rodou nos últimos 15 min e foi sucesso
      • degraded  → último scan falhou OU não rodou há mais de 15 min
      • starting  → ainda não rodou nenhum scan (boot recente)

    Retorna 200 sempre (UptimeRobot pode checar o campo `status`).
    """
    now = datetime.now(timezone.utc)
    last_at_str = _METRICS.get("last_scan_at")
    status = "starting"
    age_seconds: Optional[float] = None
    if last_at_str:
        try:
            last_at = datetime.fromisoformat(last_at_str)
            age_seconds = (now - last_at).total_seconds()
            stale = age_seconds > (SERVER_SCAN_INTERVAL * 3)  # 15 min default
            if _METRICS.get("last_scan_ok") and not stale:
                status = "ok"
            else:
                status = "degraded"
        except Exception:
            status = "degraded"

    return {
        "status": status,
        "now": now.isoformat(),
        "db_enabled": DB_ENABLED,
        "push_enabled": PUSH_ENABLED,
        "sentry_enabled": bool(_SENTRY_DSN),
        "scan_loop": {
            "interval_seconds": SERVER_SCAN_INTERVAL,
            "last_scan_at": _METRICS.get("last_scan_at"),
            "last_scan_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "last_scan_ok": _METRICS.get("last_scan_ok"),
            "last_scan_error": _METRICS.get("last_scan_error"),
            "scans_total": _METRICS.get("scans_total"),
            "scans_failed": _METRICS.get("scans_failed"),
            "recs_last_scan": _METRICS.get("recs_last_scan"),
            "recs_a_plus_last_scan": _METRICS.get("recs_a_plus_last_scan"),
            "recs_a_last_scan": _METRICS.get("recs_a_last_scan"),
            "pushes_sent_last_scan": _METRICS.get("pushes_sent_last_scan"),
            "pushes_sent_total": _METRICS.get("pushes_sent_total"),
        },
        "snapshot_loop": {
            "last_check_at": _METRICS.get("last_snapshot_check_at"),
        },
        "startup_at": _METRICS.get("startup_at"),
    }


@app.get("/api/debug/egress-ip")
async def debug_egress_ip():
    """
    Diagnóstico read-only: retorna o IP público de SAÍDA deste serviço.

    Uso: descobrir o egress IP do Railway pra colocar na whitelist da Binance
    (a permissão de Futuros exige restrição de acesso por IP). Não toca em nada,
    só faz um GET a serviços públicos de echo de IP. Tenta vários por robustez.
    """
    import httpx
    echo_urls = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/all.json",
        "https://ipinfo.io/json",
    ]
    results: list = []
    ip: Optional[str] = None
    async with httpx.AsyncClient(timeout=8.0) as client:
        for url in echo_urls:
            try:
                r = await client.get(url)
                data = r.json()
                cand = data.get("ip") or data.get("ip_addr")
                if cand and not ip:
                    ip = cand
                results.append({"source": url, "ip": cand})
            except Exception as e:  # noqa: BLE001
                results.append({"source": url, "error": str(e)[:120]})
    return {"egress_ip": ip, "checks": results}


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
        btc_dominance, btc_df, market_data, crypto_totals = await asyncio.gather(
            get_btc_dominance(),
            fetch_ohlcv("BTC/USDT:USDT", "1d", 100),
            get_global_market_data(),
            get_crypto_totals(),
            return_exceptions=True,
        )
        dominance = btc_dominance if not isinstance(btc_dominance, Exception) else None
        btc_data = btc_df if not isinstance(btc_df, Exception) else None
        mdata = market_data if not isinstance(market_data, Exception) else {}
        # Merge dos totais (TOTAL/TOTAL2/TOTAL3 + USDT.D) no mesmo dict de mercado
        if isinstance(crypto_totals, dict) and crypto_totals:
            mdata = {**mdata, **crypto_totals}

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
    """Trade Recommendations — serve as MESMAS recs que o scan loop server-side
    (Binance Vision) gera e usa pra abrir shadow/auto-trade. Assim o app reflete
    exatamente o que o bot opera, sem depender de chamadas Bybit do celular.
    Servido a partir do cache do último scan (loop a cada 90s); fallback faz um
    scan próprio se o cache estiver frio."""
    try:
        recs = await get_recommendations_cached_for_api(top_n=min(max(top_n, 5), 300))
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
        # Remove recs cujo setup (symbol+tf+direction) já foi resolvido nas
        # últimas 2h. Esses trades aparecem nos painéis de PnL (vencedores/
        # perdedores) — não devem voltar pro painel de "recomendados" como
        # se fossem oportunidades novas. Também evita push notifications
        # repetidas pelo mesmo setup.
        if DB_ENABLED and recs_dict:
            try:
                from db import get_session
                from models.recommendation_snapshot import RecommendationSnapshot
                from sqlalchemy import select, and_
                from datetime import datetime, timedelta, timezone
                cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
                resolved_statuses = ("won_tp1", "won_tp1_be", "won_tp2", "lost")
                filtered: List[Dict[str, Any]] = []
                async with get_session() as session:
                    for r in recs_dict:
                        stmt = (
                            select(RecommendationSnapshot.id)
                            .where(and_(
                                RecommendationSnapshot.symbol == r["symbol"],
                                RecommendationSnapshot.timeframe == r["timeframe"],
                                RecommendationSnapshot.direction == r["direction"],
                                RecommendationSnapshot.status.in_(resolved_statuses),
                                RecommendationSnapshot.outcome_at >= cutoff,
                            ))
                            .limit(1)
                        )
                        was_resolved = (await session.execute(stmt)).scalar_one_or_none()
                        if was_resolved is None:
                            filtered.append(r)
                        else:
                            logging.info(
                                f"Suprimindo rec já resolvida: {r['symbol']} "
                                f"{r['timeframe']} {r['direction']}"
                            )
                recs_dict = filtered
            except Exception as e:
                logging.warning(f"filtro recent_outcome falhou (segue sem filtrar): {e}")

        # Paridade com server-scan: aplica MESMOS filtros (news/regime/cooldown)
        # ANTES de salvar e disparar push. Sem isso, server-scan "engole" recs
        # silenciosamente (filtros conservadores) mas o frontend salva+pusha as
        # mesmas → user só recebe push quando abre o painel, com delay.
        # Os filtros são aplicados a uma cópia pra não distorcer o retorno da
        # API (UI continua exibindo todas as recs cruas pro user decidir).
        pushable_recs = list(recs_dict)
        try:
            # News blackout
            from services import news_filter_service as nfs
            blackout = await nfs.get_blackout_status()
            if blackout.get("active"):
                logging.info(f"[push-gate] news blackout ({blackout.get('event')}) — suprimindo push")
                pushable_recs = []
        except Exception as e:
            logging.warning(f"[push-gate] news check falhou (fail-open): {e}")
        try:
            # Regime block_all + per-rec block + downgrade alt longs
            from services import regime_service as rs
            regime = await rs.get_regime_status()
            if regime.get("block_all"):
                logging.info(f"[push-gate] regime {regime.get('regime')} block_all — suprimindo push")
                pushable_recs = []
            else:
                from services.regime_service import should_block_recommendation, is_btc_symbol
                kept = []
                for r in pushable_recs:
                    if should_block_recommendation(regime, r["symbol"], r["direction"]):
                        continue
                    if regime.get("downgrade_alt_longs") and r["direction"] == "long" and not is_btc_symbol(r["symbol"]):
                        # Fix #2 (A): rebaixamento suave — no máximo 1 nível
                        # (A+→A) e NUNCA descarta. Os dados mostram que long de
                        # alt está entre os melhores trades; descartar/cascatear
                        # jogava dinheiro bom fora. A e B passam intactos.
                        if r.get("tier") == "A+":
                            r = {**r, "tier": "A"}
                    kept.append(r)
                pushable_recs = kept
        except Exception as e:
            logging.warning(f"[push-gate] regime check falhou (fail-open): {e}")
        try:
            # Daily SL-rate breaker: se a taxa de SL do dia numa direção cruzou
            # o limiar, suprime recomendações dessa direção (e o executor já
            # bloqueia entradas reais via mesmo breaker). Avalia 1x por direção.
            from services.shadow_trade_service import _daily_sl_breaker
            dir_blocked = {}
            for d in ("long", "short"):
                if any(r["direction"] == d for r in pushable_recs):
                    blocked, reason = await _daily_sl_breaker(d)
                    if blocked:
                        dir_blocked[d] = reason
                        logging.info(f"[push-gate] daily-breaker {d}: {reason} — suprimindo {d}")
            if dir_blocked:
                pushable_recs = [r for r in pushable_recs if r["direction"] not in dir_blocked]
        except Exception as e:
            logging.warning(f"[push-gate] daily-breaker check falhou (fail-open): {e}")
        try:
            # Cooldown 6h pós-stop
            from services.snapshot_service import get_recently_stopped_symbols
            cooldown = await get_recently_stopped_symbols(hours=6)
            if cooldown:
                pushable_recs = [r for r in pushable_recs if r["symbol"] not in cooldown]
        except Exception as e:
            logging.warning(f"[push-gate] cooldown check falhou (fail-open): {e}")

        # Persistência (não bloqueia se DB indisponível) — só persiste o que
        # passou nos gates, evitando que o frontend antecipe o server-scan.
        newly_saved = 0
        if DB_ENABLED and pushable_recs:
            try:
                newly_saved = await save_recommendations(pushable_recs) or 0
            except Exception as e:
                logging.warning(f"save_recommendations falhou (segue sem persistir): {e}")
            # Shadow #11.3
            try:
                from services.shadow_trade_service import open_shadow_for_recs
                await open_shadow_for_recs(pushable_recs)
            except Exception as e:
                logging.warning(f"shadow open falhou: {e}")
        # Push notifications (só dispara pra recs novas — dedup feito por tag)
        if PUSH_ENABLED and newly_saved > 0:
            try:
                asyncio.create_task(notify_recommendations_batch(pushable_recs, newly_saved))
            except Exception as e:
                logging.warning(f"notify push falhou: {e}")
        # UI ainda recebe a lista bruta (recs_dict) pra exibição;
        # filtros são só pra push/persistência.
        return {"count": len(recs), "recommendations": recs_dict}
    except Exception as e:
        logging.error(f"recommendations-batch error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao processar recomendações: {e}")


@app.get("/api/daily-pnl")
async def daily_pnl(
    date_str: Optional[str] = Query(None, alias="date"),
    end_date_str: Optional[str] = Query(None, alias="end_date"),
):
    """P&L do dia (default = hoje em UTC).
    - ?date=YYYY-MM-DD                    → um único dia
    - ?date=YYYY-MM-DD&end_date=YYYY-MM-DD → range (inclusivo)
    """
    from datetime import date as _date
    start = None
    end = None
    if date_str:
        try:
            start = _date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(400, "date deve estar em formato YYYY-MM-DD")
    if end_date_str:
        try:
            end = _date.fromisoformat(end_date_str)
        except ValueError:
            raise HTTPException(400, "end_date deve estar em formato YYYY-MM-DD")
        if start and end < start:
            raise HTTPException(400, "end_date deve ser ≥ date")
    try:
        return await get_daily_pnl(start, end)
    except Exception as e:
        logging.error(f"daily-pnl error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter P&L: {e}")


@app.get("/api/news-status")
async def news_status(upcoming_hours: int = 24):
    """Status do filtro de notícias macro (FOMC/CPI/NFP etc).
    Retorna se há blackout ativo agora + lista de próximos eventos high-impact."""
    try:
        from services import news_filter_service as nfs
        status = await nfs.get_blackout_status()
        upcoming = await nfs.get_upcoming_events(hours=max(1, min(upcoming_hours, 168)))
        return {"status": status, "upcoming": upcoming}
    except Exception as e:
        logging.warning(f"news-status error (fail-open): {e}")
        return {"status": {"active": False, "reason": "error"}, "upcoming": []}


@app.get("/api/regime-status")
async def regime_status():
    """Status do regime macro (RISK_OFF / ALT_DANGER / BTC_DOMINANT / NORMAL).
    Indica se recs estão sendo bloqueadas/downgraded por condição de mercado."""
    try:
        from services import regime_service as rs
        status = await rs.get_regime_status()
        # Enriquece com TOTAL/TOTAL2/TOTAL3 + USDT.D (observável, não bloqueia).
        try:
            totals = await get_crypto_totals()
            if totals:
                status = {**status, "totals": totals}
        except Exception:
            pass
        return status
    except Exception as e:
        logging.warning(f"regime-status error (fail-open): {e}")
        return {"regime": "NORMAL", "btc_24h_pct": None, "btc_dominance": None,
                "block_all": False, "block_alt_longs": False,
                "downgrade_alt_longs": False, "reasons": ["error"]}


@app.get("/api/learning-insights")
async def learning_insights(days: int = 0):
    """Estatísticas agregadas por bucket (tier/TF/sessão/padrão/funding/etc.)
    + combos vencedores e perdedores. days=0 (default) = todo o histórico."""
    try:
        d = 0 if days <= 0 else max(7, min(days, 365))
        return await compute_stats_by_bucket(days=d)
    except Exception as e:
        logging.error(f"learning-insights error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter insights: {e}")


@app.get("/api/learning-auto-adjust")
async def learning_auto_adjust(days: int = 0):
    """Estado do auto-learning nível 2 — multiplicadores e blocks ativos por
    bucket. Dormente por bucket até atingir LEARNING_MIN_SAMPLE_ADJUST.
    days=0 (default) = todo o histórico."""
    try:
        from services.learning_service import compute_auto_adjustments
        d = 0 if days <= 0 else max(30, min(days, 365))
        return await compute_auto_adjustments(days=d)
    except Exception as e:
        logging.error(f"learning-auto-adjust error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter auto-adjust: {e}")


@app.get("/api/rotation/symbol-stats")
async def rotation_symbol_stats(days: int = 0):
    """Performance por SÍMBOLO (base) dos snapshots resolvidos — matéria-prima do
    motor de rotação do universo de execução (champion/challenger). Read-only, não
    decide nada. days=0 (default) = todo o histórico. Ordena por avg_r desc."""
    try:
        from services.learning_service import (
            compute_symbol_stats,
            ROTATION_MIN_SAMPLE,
            ROTATION_PROMOTE_MIN_R,
            ROTATION_DEMOTE_MAX_R,
        )
        d = 0 if days <= 0 else max(7, min(days, 365))
        stats = await compute_symbol_stats(days=d)
        rows = sorted(
            ({"symbol": k, **v} for k, v in stats.items()),
            key=lambda r: (r["sample_ok"], r["avg_r"]),
            reverse=True,
        )
        return {
            "thresholds": {
                "min_sample": ROTATION_MIN_SAMPLE,
                "promote_min_r": ROTATION_PROMOTE_MIN_R,
                "demote_max_r": ROTATION_DEMOTE_MAX_R,
            },
            "counts": {
                "symbols": len(rows),
                "promote": sum(1 for r in rows if r["verdict"] == "promote"),
                "demote": sum(1 for r in rows if r["verdict"] == "demote"),
                "neutro": sum(1 for r in rows if r["verdict"] == "neutro"),
                "amostra_pequena": sum(1 for r in rows if r["verdict"] == "amostra_pequena"),
            },
            "symbols": rows,
        }
    except Exception as e:
        logging.error(f"rotation symbol-stats error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter symbol-stats: {e}")


@app.get("/api/calibration")
async def calibration():
    """
    Tabela score → P(TP1) calibrada empiricamente.
    Retorna {enabled: false, ...} se ainda não houver amostra mínima (30 trades).
    """
    try:
        data = await get_calibration()
        if data is None:
            return {
                "enabled": False,
                "message": "Calibração ainda não disponível — precisa de pelo menos 30 trades resolvidos no histórico.",
            }
        return data
    except Exception as e:
        logging.error(f"calibration error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro ao obter calibração: {e}")


class HistoricalLookupItem(BaseModel):
    tier: str
    timeframe: str
    direction: str


class HistoricalLookupRequest(BaseModel):
    items: List[HistoricalLookupItem]
    days: int = 0  # 0 = todo o histórico


@app.post("/api/historical-lookup")
async def historical_lookup(body: HistoricalLookupRequest):
    """Recebe [{tier, tf, direction}, ...] e retorna stat histórico de cada.
    Usado pelo painel de recomendações pra exibir badge "histórico" no card."""
    try:
        keys = [{"tier": it.tier, "timeframe": it.timeframe, "direction": it.direction} for it in body.items]
        d = 0 if body.days <= 0 else max(7, min(body.days, 365))
        return await lookup_historical_batch(keys, days=d)
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


@app.get("/api/probabilities")
async def probabilities(days: int = 90, min_sample: int = 8):
    """Probabilidades empíricas P(TP1) e P(TP2) agregadas por bucket
    (tier, timeframe, direction). Usa snapshots resolvidos dos últimos
    N dias. min_sample = mínimo de trades pra considerar confiável.

    Cada bucket retorna:
      - n_total: trades resolvidos
      - p_tp1: % que tocou TP1 (won_tp1 + won_tp1_be + won_tp2)
      - p_tp2: % que atingiu TP2 cheio
      - confidence: "high" (≥30), "medium" (≥min_sample), "low" (<min)
    """
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        if not DB_ENABLED:
            return {"enabled": False, "buckets": {}}
        since = datetime.now(timezone.utc) - timedelta(days=max(7, min(days, 365)))
        async with get_session() as session:
            stmt = select(
                RecommendationSnapshot.tier,
                RecommendationSnapshot.timeframe,
                RecommendationSnapshot.direction,
                RecommendationSnapshot.status,
                RecommendationSnapshot.tp1_hit_at,
            ).where(RecommendationSnapshot.created_at >= since)
            rows = (await session.execute(stmt)).all()

        # bucket = (tier, timeframe, direction)
        agg: Dict[tuple, Dict[str, int]] = {}
        for tier, tf, direction, status, tp1_hit_at in rows:
            if status == "open":
                continue
            key = (tier, tf, direction)
            b = agg.setdefault(key, {"n_total": 0, "tp1_hits": 0, "tp2_hits": 0, "stops": 0, "expired": 0})
            b["n_total"] += 1
            if status == "won_tp2":
                b["tp1_hits"] += 1
                b["tp2_hits"] += 1
            elif status in ("won_tp1", "won_tp1_be"):
                b["tp1_hits"] += 1
            elif status == "lost":
                b["stops"] += 1
            elif status == "expired":
                # expired SEM TP1 → nada conta. expired COM TP1 → conta TP1.
                if tp1_hit_at is not None:
                    b["tp1_hits"] += 1
                else:
                    b["expired"] += 1

        buckets = {}
        for (tier, tf, direction), b in agg.items():
            n = b["n_total"]
            if n == 0:
                continue
            p_tp1 = b["tp1_hits"] / n * 100
            p_tp2 = b["tp2_hits"] / n * 100
            if n >= 30:
                conf = "high"
            elif n >= min_sample:
                conf = "medium"
            else:
                conf = "low"
            buckets[f"{tier}|{tf}|{direction}"] = {
                "tier": tier, "timeframe": tf, "direction": direction,
                "n_total": n,
                "p_tp1_pct": round(p_tp1, 1),
                "p_tp2_pct": round(p_tp2, 1),
                "p_stop_pct": round(b["stops"] / n * 100, 1),
                "confidence": conf,
            }
        return {"enabled": True, "days": days, "min_sample": min_sample, "buckets": buckets}
    except Exception as e:
        logging.error(f"probabilities error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


@app.get("/api/snapshots/open-viability")
async def open_viability():
    """Pra cada snapshot aberto, avalia se ainda vale entrar agora:
       🟢 valid    — preço perto do entry, setup ainda intacto
       🟡 wait     — preço já andou a favor, esperar pullback
       🔴 missed   — preço passou demais ou perto do stop
       🔵 tp1_done — já tocou TP1, posição com lock garantido (não entrar new)
    """
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from services.binance_service import fetch_ticker
        from sqlalchemy import select
        from datetime import datetime, timezone
        if not DB_ENABLED:
            return {"enabled": False, "items": []}
        async with get_session() as session:
            stmt = select(RecommendationSnapshot).where(RecommendationSnapshot.status == "open")
            snaps = (await session.execute(stmt)).scalars().all()

        items = []
        now = datetime.now(timezone.utc)
        # Cache de tickers pra evitar bater 2× no mesmo símbolo
        ticker_cache: dict = {}

        for snap in snaps:
            try:
                if snap.symbol not in ticker_cache:
                    t = await fetch_ticker(snap.symbol)
                    ticker_cache[snap.symbol] = t
                ticker = ticker_cache[snap.symbol]
                price = float(ticker.get("last") or ticker.get("close") or 0)
                if price <= 0:
                    continue

                feats = snap.features or {}
                atr_pct = feats.get("atr_pct")
                atr_abs = (
                    float(atr_pct) / 100.0 * float(snap.entry)
                    if atr_pct and snap.entry else None
                )

                is_long = snap.direction == "long"
                # Distância em ATR, SIGNED a favor da direção
                # (long: positivo se preço subiu acima do entry; short: positivo se desceu)
                if atr_abs and atr_abs > 0:
                    delta = (price - snap.entry) if is_long else (snap.entry - price)
                    distance_atr = round(delta / atr_abs, 2)
                else:
                    distance_atr = None

                # Distância pra stop em % do range entry→stop
                stop_range = abs(snap.entry - snap.stop_loss) or 1
                stop_progress = (
                    (snap.entry - price) / stop_range if is_long
                    else (price - snap.entry) / stop_range
                )  # 0 = no entry, 1 = no stop. Negativo = a favor.

                age_h = (now - snap.created_at).total_seconds() / 3600.0

                # Classificação
                if snap.tp1_hit_at is not None:
                    viability = "tp1_done"
                    advice = "TP1 já tocou — lock garantido. Não entrar nova posição."
                elif stop_progress >= 0.7:
                    viability = "missed"
                    advice = "Preço quase no stop — não entrar."
                elif distance_atr is None:
                    viability = "wait"
                    advice = "Sem dado de ATR. Avaliar manualmente."
                elif distance_atr >= 1.0:
                    viability = "missed"
                    advice = f"Preço já andou {distance_atr}×ATR a favor — perdeu o trem. Aguardar pullback."
                elif distance_atr >= 0.5:
                    viability = "wait"
                    advice = f"Preço {distance_atr}×ATR adiantado. Aguardar pullback até entry ±0.3×ATR."
                elif distance_atr <= -0.3:
                    viability = "wait"
                    advice = "Preço retraiu abaixo do entry — boa zona de entrada se setup ainda válido."
                else:
                    viability = "valid"
                    advice = "Preço próximo ao entry — entrada ainda viável."

                items.append({
                    "id": snap.id,
                    "symbol": snap.symbol,
                    "timeframe": snap.timeframe,
                    "direction": snap.direction,
                    "tier": snap.tier,
                    "entry": snap.entry,
                    "stop_loss": snap.stop_loss,
                    "tp1": snap.tp1,
                    "tp2": snap.tp2,
                    "current_price": price,
                    "distance_atr": distance_atr,
                    "stop_progress_pct": round(stop_progress * 100, 1),
                    "age_hours": round(age_h, 1),
                    "tp1_hit": snap.tp1_hit_at is not None,
                    "viability": viability,
                    "advice": advice,
                    "created_at": snap.created_at.isoformat() if snap.created_at else None,
                })
            except Exception as ex:
                logging.warning(f"[viability] erro em snap {snap.id} ({snap.symbol}): {ex}")
        return {"enabled": True, "count": len(items), "items": items}
    except Exception as e:
        logging.error(f"open-viability error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


@app.get("/api/debug/status-distribution")
async def status_distribution(days: int = 30):
    """Distribuição de status dos snapshots nos últimos N dias.
    Usado pra diagnosticar se TP2 está sendo atingido ou se trail está
    encerrando trades em won_tp1_be antes de chegar lá."""
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, func
        if not DB_ENABLED:
            return {"enabled": False}
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with get_session() as session:
            stmt = (
                select(RecommendationSnapshot.status, func.count())
                .where(RecommendationSnapshot.created_at >= since)
                .group_by(RecommendationSnapshot.status)
            )
            rows = (await session.execute(stmt)).all()
            # Também: quantos chegaram a tocar TP1 (tp1_hit_at != null)
            stmt2 = select(func.count()).where(
                RecommendationSnapshot.created_at >= since,
                RecommendationSnapshot.tp1_hit_at.isnot(None),
            )
            tp1_hits = (await session.execute(stmt2)).scalar() or 0
        dist = {row[0]: row[1] for row in rows}
        total = sum(dist.values())
        return {
            "enabled": True,
            "days": days,
            "total": total,
            "distribution": dist,
            "tp1_hits_total": tp1_hits,
            "won_tp2_pct_of_tp1_hits": (
                round(dist.get("won_tp2", 0) / tp1_hits * 100, 1)
                if tp1_hits else None
            ),
        }
    except Exception as e:
        logging.error(f"status-distribution error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


@app.get("/api/debug/tier-a-losses")
async def tier_a_losses(days: int = 60):
    """Drill-down dos trades tier A que stoparam (status='lost').
    Usado pra investigar paradoxo A vs B no scoring — quais features em comum?"""
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        if not DB_ENABLED:
            return {"enabled": False}
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with get_session() as session:
            stmt = (
                select(RecommendationSnapshot)
                .where(
                    RecommendationSnapshot.created_at >= since,
                    RecommendationSnapshot.tier.in_(("A", "A+")),
                    RecommendationSnapshot.status == "lost",
                )
                .order_by(RecommendationSnapshot.created_at.desc())
            )
            losses = list((await session.execute(stmt)).scalars().all())

            # Também: tier A vencedores pra comparar features
            stmt_wins = (
                select(RecommendationSnapshot)
                .where(
                    RecommendationSnapshot.created_at >= since,
                    RecommendationSnapshot.tier.in_(("A", "A+")),
                    RecommendationSnapshot.status.in_(("won_tp1", "won_tp1_be", "won_tp2")),
                )
            )
            wins = list((await session.execute(stmt_wins)).scalars().all())

        def serialize(s):
            f = s.features or {}
            return {
                "id": s.id, "symbol": s.symbol, "tf": s.timeframe, "tier": s.tier,
                "direction": s.direction, "score": s.score, "rr": s.risk_reward,
                "confidence": f.get("confidence"),
                "mtf_score": f.get("mtf_score"),
                "confluence_pct": f.get("confluence_pct"),
                "rsi": f.get("rsi"),
                "patterns": f.get("patterns"),
                "funding_pct": f.get("funding_pct"),
                "oi_change_pct": f.get("oi_change_pct"),
                "atr_pct": f.get("atr_pct"),
                "hour_utc": f.get("hour_utc"),
                "day_of_week": f.get("day_of_week"),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }

        # Aggregates pra comparar — alguns campos vêm do snapshot, outros de features
        SNAP_FIELDS = {"score", "rr"}
        def agg(arr, key):
            vals = []
            for s in arr:
                if key == "score":
                    v = s.score
                elif key == "rr":
                    v = s.risk_reward
                else:
                    v = (s.features or {}).get(key)
                if v is not None:
                    vals.append(float(v))
            if not vals:
                return None
            return {"mean": round(sum(vals) / len(vals), 2), "n": len(vals)}

        def pattern_freq(arr):
            from collections import Counter
            cnt = Counter()
            for s in arr:
                pats = (s.features or {}).get("patterns") or []
                for p in pats:
                    cnt[p] += 1
            return dict(cnt.most_common(10))

        return {
            "enabled": True,
            "days": days,
            "losses": [serialize(s) for s in losses],
            "n_losses": len(losses),
            "n_wins": len(wins),
            "compare": {
                "losses": {
                    "score": agg(losses, "score"),
                    "rr":    agg(losses, "rr"),
                    "mtf_score":      agg(losses, "mtf_score"),
                    "confluence_pct": agg(losses, "confluence_pct"),
                    "rsi":            agg(losses, "rsi"),
                    "funding_pct":    agg(losses, "funding_pct"),
                    "atr_pct":        agg(losses, "atr_pct"),
                    "patterns_top":   pattern_freq(losses),
                },
                "wins": {
                    "score": agg(wins, "score"),
                    "rr":    agg(wins, "rr"),
                    "mtf_score":      agg(wins, "mtf_score"),
                    "confluence_pct": agg(wins, "confluence_pct"),
                    "rsi":            agg(wins, "rsi"),
                    "funding_pct":    agg(wins, "funding_pct"),
                    "atr_pct":        agg(wins, "atr_pct"),
                    "patterns_top":   pattern_freq(wins),
                },
            },
        }
    except Exception as e:
        logging.error(f"tier-a-losses error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


@app.get("/api/debug/tier-wr")
async def debug_tier_wr(days: int = 90, since_iso: Optional[str] = None):
    """
    WR / n / avg_score / avg_realized_R agrupado por tier.

    Diagnóstico-chave: tier A+/A/B realmente prediz outcome diferente?
    Se WR(A+) ≈ WR(A) ≈ WR(B), tier é cosmético — score não diferencia
    qualidade. Se WR(A+) >> WR(B), filtro funciona — só falta empurrar
    mais setups pra A+ (= revisar pesos de _compute_score).

    Também breakdown por score-bin (mesmos bins da calibration) pra ver
    onde os trades realmente caem.
    """
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, and_
        if not DB_ENABLED:
            return {"enabled": False}

        WIN_STATUSES = ("won_tp1", "won_tp1_be", "won_tp2")
        RESOLVED_STATUSES = WIN_STATUSES + ("lost", "expired")

        since = datetime.now(timezone.utc) - timedelta(days=days)
        # Permite override por created_at >= since_iso (pra excluir leftovers
        # de janelas onde thresholds de tier estavam relaxados).
        created_after = None
        if since_iso:
            try:
                created_after = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            except Exception:
                raise HTTPException(400, f"since_iso inválido: {since_iso}")

        async with get_session() as session:
            conds = [
                RecommendationSnapshot.outcome_at >= since,
                RecommendationSnapshot.status.in_(RESOLVED_STATUSES),
            ]
            if created_after is not None:
                conds.append(RecommendationSnapshot.created_at >= created_after)
            stmt = select(
                RecommendationSnapshot.tier,
                RecommendationSnapshot.score,
                RecommendationSnapshot.status,
                RecommendationSnapshot.realized_r,
                RecommendationSnapshot.risk_reward,
            ).where(and_(*conds))
            rows = (await session.execute(stmt)).all()

        if not rows:
            return {"enabled": True, "days": days, "n": 0,
                    "note": "sem trades resolvidos no período"}

        def _bucket_metrics(items):
            if not items:
                return None
            n = len(items)
            wins = sum(1 for r in items if r.status in WIN_STATUSES)
            losses = sum(1 for r in items if r.status == "lost")
            expired = sum(1 for r in items if r.status == "expired")
            wr = wins / n
            scores = [r.score for r in items if r.score is not None]
            rrs = [r.risk_reward for r in items if r.risk_reward is not None]
            r_vals = [r.realized_r for r in items if r.realized_r is not None]
            r_wins = [r for r in r_vals if r > 0]
            r_losses_abs = [abs(r) for r in r_vals if r < 0]
            pf = (sum(r_wins) / sum(r_losses_abs)) if r_losses_abs else None
            return {
                "n": n, "wins": wins, "losses": losses, "expired": expired,
                "wr_pct": round(wr * 100, 1),
                "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
                "avg_rr": round(sum(rrs) / len(rrs), 2) if rrs else None,
                "total_r": round(sum(r_vals), 2) if r_vals else None,
                "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None,
                "profit_factor": round(pf, 2) if pf is not None else None,
            }

        # Por tier
        by_tier: dict[str, list] = {}
        for r in rows:
            by_tier.setdefault(r.tier or "?", []).append(r)
        tier_breakdown = {
            tier: _bucket_metrics(items)
            for tier, items in sorted(by_tier.items())
        }

        # Por score-bin (mesmos da calibration)
        SCORE_BINS = [(55, 60), (60, 65), (65, 70), (70, 75),
                      (75, 80), (80, 85), (85, 90), (90, 95), (95, 100.1)]

        def _bin_label(lo, hi, last):
            return f"[{lo}-100]" if last else f"[{lo}-{int(hi)})"

        bins_breakdown = []
        for i, (lo, hi) in enumerate(SCORE_BINS):
            in_bin = [r for r in rows
                      if r.score is not None and lo <= r.score < hi]
            m = _bucket_metrics(in_bin) or {"n": 0, "wins": 0, "losses": 0,
                                            "expired": 0, "wr_pct": None}
            bins_breakdown.append({
                "label": _bin_label(lo, hi, i == len(SCORE_BINS) - 1),
                **m,
            })

        return {
            "enabled": True,
            "days": days,
            "total_resolved": len(rows),
            "by_tier": tier_breakdown,
            "by_score_bin": bins_breakdown,
        }
    except Exception as e:
        logging.error(f"tier-wr error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


@app.get("/api/debug/direction-wr")
async def debug_direction_wr(days: int = 5):
    """WR por direção × (major vs alt). Diagnóstico do downgrade_alt_longs:
    se long-alt vence muito mais que short-alt, o viés short (regime
    BTC_DOMINANT) está penalizando os trades certos. major = BTC/ETH."""
    try:
        from db import DB_ENABLED, get_session
        from models.recommendation_snapshot import RecommendationSnapshot as RS
        from services.regime_service import is_btc_symbol
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, and_
        if not DB_ENABLED:
            return {"enabled": False}

        WIN = ("won_tp1", "won_tp1_be", "won_tp2")
        RESOLVED = WIN + ("lost", "expired")
        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with get_session() as session:
            stmt = select(
                RS.symbol, RS.direction, RS.status, RS.realized_r, RS.tier,
            ).where(and_(RS.outcome_at >= since, RS.status.in_(RESOLVED)))
            rows = (await session.execute(stmt)).all()

        if not rows:
            return {"enabled": True, "days": days, "n": 0,
                    "note": "sem trades resolvidos no período"}

        def _metrics(items):
            n = len(items)
            if n == 0:
                return {"n": 0}
            wins = sum(1 for r in items if r.status in WIN)
            losses = sum(1 for r in items if r.status == "lost")
            expired = sum(1 for r in items if r.status == "expired")
            decided = wins + losses
            r_vals = [r.realized_r for r in items if r.realized_r is not None]
            return {
                "n": n, "wins": wins, "losses": losses, "expired": expired,
                "wr_pct": round(wins / decided * 100, 1) if decided else None,
                "total_r": round(sum(r_vals), 2) if r_vals else None,
            }

        buckets: dict[str, list] = {
            "long_major": [], "long_alt": [], "short_major": [], "short_alt": [],
        }
        for r in rows:
            major = is_btc_symbol(r.symbol)
            key = f"{r.direction}_{'major' if major else 'alt'}"
            if key in buckets:
                buckets[key].append(r)

        by_direction = {
            "long": _metrics([r for r in rows if r.direction == "long"]),
            "short": _metrics([r for r in rows if r.direction == "short"]),
        }
        return {
            "enabled": True,
            "days": days,
            "total_resolved": len(rows),
            "by_direction": by_direction,
            "by_direction_x_asset": {k: _metrics(v) for k, v in buckets.items()},
        }
    except Exception as e:
        logging.error(f"direction-wr error: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Erro: {e}")


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


@app.get("/api/risk/status")
async def risk_status():
    """
    Estado atual do circuit breaker: pausado? por quê? DD diário/semanal?
    Lê estado do DB sem recomputar — chamada barata pra UI consultar.
    """
    from services import risk_service
    return await risk_service.get_status()


@app.post("/api/risk/kill-switch")
async def risk_kill_switch(paused: bool = True, reason: str | None = None):
    """
    Kill switch manual. POST com query param `paused=true|false`.
    Quando ligado, server-scan não emite push de novas recs até desligar.
    """
    from services import risk_service
    return await risk_service.set_manual_pause(paused, reason)


@app.get("/api/calibration/versions")
async def calibration_versions_list(limit: int = 20):
    """Lista versões versionadas do modelo PAV (#9), mais recentes primeiro."""
    from services import calibration_versions_service
    items = await calibration_versions_service.list_versions(limit=limit)
    return {"versions": items, "count": len(items)}


@app.get("/api/calibration/active")
async def calibration_active():
    """Versão ativa do modelo PAV atualmente em produção."""
    from services import calibration_versions_service
    return await calibration_versions_service.get_active() or {"active": None}


@app.post("/api/calibration/snapshot")
async def calibration_snapshot(notes: str | None = None, make_active: bool = True):
    """
    Cria snapshot da calibração atual (#9). Captura bins do PAV + métricas
    retroativas (WR, avgR, Sharpe) e versiona.
    """
    from services import calibration_versions_service
    result = await calibration_versions_service.snapshot_current(
        notes=notes, make_active=make_active,
    )
    if result is None:
        raise HTTPException(409, "Calibração não está pronta (amostra insuficiente).")
    return result


@app.get("/api/calibration/compare")
async def calibration_compare(version_a: str, version_b: str):
    """Diff entre duas versões do modelo: delta P por bin + delta de métricas."""
    from services import calibration_versions_service
    result = await calibration_versions_service.compare(version_a, version_b)
    if result is None:
        raise HTTPException(404, "Uma das versões não foi encontrada.")
    return result


@app.post("/api/calibration/monthly-snapshot")
async def calibration_monthly_snapshot():
    """Wrapper do cron mensal — snapshot + compara com versão anterior + log."""
    from services import calibration_versions_service
    result = await calibration_versions_service.run_monthly_snapshot()
    if result is None:
        raise HTTPException(409, "Snapshot não pôde ser criado (calibração não pronta).")
    return result


@app.post("/api/calibration/recalibrate")
async def calibration_recalibrate(notes: str | None = None, make_active: bool = True):
    """
    Recalibração manual da autoaprendizagem (#10): reaprende com TODO o
    histórico de trades resolvidos (wins totais, win com 1 TP, e losses),
    recomputa score→P(TP1) + multiplicadores/blocks de bucket, e versiona
    como nova calibração ativa. Seguro pra rodar a qualquer momento.
    """
    from services import calibration_versions_service
    return await calibration_versions_service.recalibrate(
        notes=notes, make_active=make_active,
    )


@app.get("/api/paper/equity-curve")
async def paper_equity_curve(days: int = 30):
    """
    Paper-trade equity curve (#8): P&L cumulativo diário das snapshots
    resolvidas. Hoje todo snapshot é paper (sem execução real ainda).
    """
    from services import paper_trade_service
    return await paper_trade_service.equity_curve(days=days)


@app.get("/api/paper/stats")
async def paper_stats(days: int = 30):
    """Stats por tier (WR, avgR, expectancy, streak de perdas) — paper-trade."""
    from services import paper_trade_service
    return await paper_trade_service.stats_by_tier(days=days)


@app.get("/api/paper/summary")
async def paper_summary(days: int = 30):
    """Combo equity-curve + tier stats numa chamada só — pra dashboard #10."""
    from services import paper_trade_service
    return await paper_trade_service.summary(days=days)


@app.get("/api/admin/health")
async def admin_health():
    """
    Heartbeat do backend (#6): gap desde último tick do server-scan,
    severidade (healthy/degraded/unknown) e contador de ticks.
    Gap > 5min indica que o loop está congelado/morreu.
    """
    from services import heartbeat_service
    return await heartbeat_service.get_health()


class ResolveSnapshotRequest(BaseModel):
    symbol: str
    direction: str | None = None          # "long" | "short" (opcional, filtra match)
    status: str = "lost"                  # won_tp1 | won_tp1_be | won_tp2 | lost | expired
    realized_r: float | None = None       # default por status se None
    outcome_price: float | None = None    # default = stop_loss/tp do snapshot se None
    only_open: bool = False               # se True, só resolve quando status atual == open


@app.post("/api/admin/snapshots/resolve")
async def admin_resolve_snapshot(req: ResolveSnapshotRequest):
    """
    Força a resolução do snapshot mais recente que casa com symbol (+direction):
    seta status/realized_r/outcome_price/outcome_at. Usado pra corrigir snapshots
    travados em open quando o símbolo saiu do universo da fonte (sem preço pra
    resolver outcome automaticamente).
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from db import get_session
    from models.recommendation_snapshot import RecommendationSnapshot

    default_r = {
        "won_tp2": 1.5, "won_tp1_be": 0.5, "won_tp1": 0.5,
        "lost": -1.0, "expired": 0.0,
    }
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        conds = [RecommendationSnapshot.symbol == req.symbol]
        if req.direction:
            conds.append(RecommendationSnapshot.direction == req.direction)
        if req.only_open:
            conds.append(RecommendationSnapshot.status == "open")
        stmt = (
            select(RecommendationSnapshot)
            .where(*conds)
            .order_by(RecommendationSnapshot.created_at.desc())
            .limit(1)
        )
        snap = (await session.execute(stmt)).scalar_one_or_none()
        if snap is None:
            return {"resolved": False, "error": "nenhum snapshot encontrado pro filtro"}

        prev_status = snap.status
        # outcome_price default: stop pra lost, tp2 pra won_tp2, tp1 pros tp1, senão entry
        if req.outcome_price is not None:
            outcome_price = req.outcome_price
        elif req.status == "lost":
            outcome_price = snap.stop_loss
        elif req.status == "won_tp2":
            outcome_price = snap.tp2
        elif req.status in ("won_tp1", "won_tp1_be"):
            outcome_price = snap.tp1 or snap.entry
        else:
            outcome_price = snap.entry

        snap.status = req.status
        snap.realized_r = req.realized_r if req.realized_r is not None else default_r.get(req.status, 0.0)
        snap.outcome_price = outcome_price
        snap.outcome_at = now
        snap.last_check_at = now
        await session.commit()

        return {
            "resolved": True,
            "id": snap.id,
            "symbol": snap.symbol,
            "direction": snap.direction,
            "timeframe": snap.timeframe,
            "prev_status": prev_status,
            "new_status": snap.status,
            "realized_r": snap.realized_r,
            "outcome_price": snap.outcome_price,
            "outcome_at": now.isoformat(),
        }


class DedupProtectionRequest(BaseModel):
    symbol: str
    dry_run: bool = True   # default seguro: só lista o que cancelaria


@app.post("/api/admin/protection/dedup")
async def admin_dedup_protection(req: DedupProtectionRequest):
    """
    Cancela ordens condicionais DUPLICADAS de um símbolo, mantendo 1 por perna
    (agrupa por type+side+triggerPrice≈). Limpa posições que ficaram com bracket
    em duplicata. dry_run=True (default) só reporta; dry_run=False cancela.
    """
    from services import binance_signed_service
    live = await binance_signed_service.get_open_algo_orders(req.symbol)
    if not live.get("ok"):
        return {"ok": False, "error": live.get("msg") or live.get("error")}
    orders = live.get("orders") or []

    # Agrupa por (type, side, trigger arredondado a 4 casas relativas)
    buckets: dict[tuple, list[dict]] = {}
    for o in orders:
        key = (
            (o.get("type") or "").upper(),
            (o.get("side") or "").upper(),
            round(float(o.get("trigger_price") or 0), 8),
        )
        buckets.setdefault(key, []).append(o)

    kept: list[dict] = []
    to_cancel: list[dict] = []
    for key, group in buckets.items():
        # mantém o primeiro (mais antigo na listagem), cancela o resto
        kept.append({"type": key[0], "side": key[1], "trigger": key[2], "algo_id": group[0].get("algo_id")})
        for dup in group[1:]:
            to_cancel.append({"type": key[0], "side": key[1], "trigger": key[2], "algo_id": dup.get("algo_id")})

    cancelled: list[dict] = []
    if not req.dry_run:
        for d in to_cancel:
            try:
                r = await binance_signed_service.cancel_algo_order(d["algo_id"])
                cancelled.append({**d, "ok": bool(r.get("ok")), "msg": r.get("msg") or r.get("error")})
            except Exception as e:
                cancelled.append({**d, "ok": False, "msg": str(e)})

    return {
        "ok": True,
        "symbol": req.symbol,
        "dry_run": req.dry_run,
        "total_live": len(orders),
        "legs_kept": kept,
        "duplicates_found": len(to_cancel),
        "duplicates": to_cancel,
        "cancelled": cancelled,
    }


@app.get("/api/portfolio/exposure")
async def portfolio_exposure():
    """
    Exposição atual do portfólio (proxy via snapshots open): número de
    posições, breakdown por categoria/direção, soma de risk_pct e limites
    configurados (#5).
    """
    from services import portfolio_service
    return await portfolio_service.get_exposure()


# ─── Real-trade endpoints (#11.2) ─────────────────────────────────────────────


class OpenTradeRequest(BaseModel):
    symbol: str
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    recommendation_id: int | None = None
    leverage: int | None = None
    planned_stop: float | None = None
    planned_tp1: float | None = None
    planned_tp2: float | None = None
    entry_fee: float = 0.0
    source: str = "manual"
    notes: str | None = None


class CloseTradeRequest(BaseModel):
    exit_price: float
    status: str = "closed_manual"  # closed_tp1/tp2/be/stop/manual
    exit_fee: float = 0.0
    notes: str | None = None


@app.post("/api/real-trades")
async def real_trade_open(req: OpenTradeRequest):
    """
    Registra fill real (modo shadow manual): user executa na corretora e
    informa entry_price; sistema computa slippage vs rec (#11.2).
    """
    from services import real_trade_service
    result = await real_trade_service.open_trade(
        symbol=req.symbol,
        side=req.side,
        qty=req.qty,
        entry_price=req.entry_price,
        recommendation_id=req.recommendation_id,
        leverage=req.leverage,
        planned_stop=req.planned_stop,
        planned_tp1=req.planned_tp1,
        planned_tp2=req.planned_tp2,
        entry_fee=req.entry_fee,
        source=req.source,
        notes=req.notes,
    )
    if result is None:
        raise HTTPException(503, "DB desabilitado")
    return result


@app.patch("/api/real-trades/{trade_id}/close")
async def real_trade_close(trade_id: int, req: CloseTradeRequest):
    """Fecha real-trade: informa exit_price e status; sistema calcula P&L e R."""
    from services import real_trade_service
    result = await real_trade_service.close_trade(
        trade_id=trade_id,
        exit_price=req.exit_price,
        status=req.status,
        exit_fee=req.exit_fee,
        notes=req.notes,
    )
    if result is None:
        raise HTTPException(404, "Trade não encontrado")
    return result


@app.get("/api/real-trades")
async def real_trade_list(status: str | None = None, days: int = 30, limit: int = 200):
    """Lista real-trades (default: últimos 30d, todos status)."""
    from services import real_trade_service
    items = await real_trade_service.list_trades(status=status, days=days, limit=limit)
    return {"trades": items, "count": len(items), "days": days}


@app.get("/api/real-trades/summary")
async def real_trade_summary(days: int = 30):
    """Equity curve + tier stats das execuções reais (#11.2) — mesmo shape do paper."""
    from services import real_trade_service
    return await real_trade_service.summary(days=days)


@app.get("/api/real-trades/{trade_id}")
async def real_trade_get(trade_id: int):
    from services import real_trade_service
    result = await real_trade_service.get_trade(trade_id)
    if result is None:
        raise HTTPException(404, "Trade não encontrado")
    return result


class ConfirmEntryRequest(BaseModel):
    """Confirma que o user entrou numa recomendação do painel (modo híbrido
    gerenciado). Você abre a posição na corretora; o bot coloca o bracket
    (SL + TP1 parcial 45% + TP2) e gerencia o breakeven pós-TP1 sozinho.
    Níveis vêm da rec; só o entry_price é livre. O qty é automático — lido da
    posição viva na conta — se não informado."""
    symbol: str
    side: str                         # "long" | "short"
    entry_price: float
    qty: float | None = None          # se None/0 → lê da posição na conta
    timeframe: str | None = None
    leverage: int | None = None
    planned_stop: float | None = None
    planned_tp1: float | None = None
    planned_tp2: float | None = None
    recommendation_id: int | None = None
    entry_fee: float = 0.0
    notes: str | None = None


@app.post("/api/real-trades/from-recommendation")
async def real_trade_from_recommendation(req: ConfirmEntryRequest):
    """
    Registra entrada manual a partir de uma recomendação do painel.
      - Herda SL/TP1/TP2/leverage da rec (níveis enviados pelo painel; e, se
        achar o snapshot por symbol+tf+direction, linka pra atribuição/slippage).
      - qty automático: se não informado, lê o tamanho da posição viva na conta.
      - source="manual" → entra no monitor advise-only (não dispara ordem).
    """
    from services import real_trade_service

    # 0. Idempotência: se já existe um trade gerenciado ABERTO pro mesmo
    #    símbolo+lado, NÃO cria duplicado — retorna o existente. Protege contra
    #    double-click no painel e retry-on-500 (o bot modela 1 posição por
    #    symbol+side, igual ao dedupe-open-trades). Best-effort: se a checagem
    #    falhar, segue o fluxo normal de criação.
    try:
        from db import get_session, DB_ENABLED
        if DB_ENABLED:
            from sqlalchemy import select, desc
            from models.real_trade import RealTrade
            async with get_session() as session:
                existing = (await session.execute(
                    select(RealTrade)
                    .where(RealTrade.symbol == req.symbol)
                    .where(RealTrade.side == req.side)
                    .where(RealTrade.status == "open")
                    .where(RealTrade.source == "managed")
                    .order_by(desc(RealTrade.opened_at))
                    .limit(1)
                )).scalar_one_or_none()
                if existing is not None:
                    from services.real_trade_service import _to_dict
                    log.info(
                        f"[confirm-entry] idempotente: trade #{existing.id} já "
                        f"aberto p/ {req.symbol} {req.side}; retorna existente"
                    )
                    return {
                        **(_to_dict(existing) or {}),
                        "idempotent": True,
                        "qty_source": "existente",
                        "linked_recommendation_id": existing.recommendation_id,
                        "protection": {
                            "placed": bool(existing.sl_order_id),
                            "note": "trade gerenciado já existente — não duplicado",
                        },
                    }
    except Exception as e:
        log.warning(f"[confirm-entry] checagem de idempotência falhou: {e}")

    # 1. Resolve o snapshot da rec (atribuição/tier/slippage) — best effort.
    rec_id = req.recommendation_id
    if rec_id is None:
        try:
            from db import get_session
            from sqlalchemy import select, desc
            from models.recommendation_snapshot import RecommendationSnapshot
            async with get_session() as session:
                stmt = (
                    select(RecommendationSnapshot.id)
                    .where(RecommendationSnapshot.symbol == req.symbol)
                    .where(RecommendationSnapshot.direction == req.side)
                    .where(RecommendationSnapshot.status == "open")
                    .order_by(desc(RecommendationSnapshot.created_at))
                    .limit(1)
                )
                if req.timeframe:
                    stmt = stmt.where(RecommendationSnapshot.timeframe == req.timeframe)
                rec_id = (await session.execute(stmt)).scalar_one_or_none()
        except Exception as e:
            log.warning(f"[confirm-entry] resolver snapshot falhou: {e}")
            rec_id = None

    # 2. qty automático — lê da posição viva na conta se não informado.
    qty = req.qty
    qty_source = "informado"
    if qty is None or qty <= 0:
        try:
            from services import exchange_service
            pos = await exchange_service.get_positions(symbol=req.symbol)
            if pos.get("ok"):
                for p in pos.get("positions") or []:
                    sz = abs(float(p.get("size") or 0))
                    if sz > 0:
                        qty = sz
                        qty_source = "posição"
                        break
        except Exception as e:
            log.warning(f"[confirm-entry] ler qty da posição falhou: {e}")
    if qty is None or qty <= 0:
        raise HTTPException(
            422,
            f"qty não informado e nenhuma posição aberta encontrada na conta para {req.symbol}. "
            f"Abra a posição na corretora antes de confirmar, ou informe o qty.",
        )

    # 3. Grava o RealTrade gerenciado (open_trade herda níveis da rec se
    #    rec_id existe). source="managed": o bot coloca o bracket e gerencia o
    #    ciclo de vida (mesmo caminho do auto), mas a posição é sua.
    result = await real_trade_service.open_trade(
        symbol=req.symbol,
        side=req.side,
        qty=qty,
        entry_price=req.entry_price,
        recommendation_id=rec_id,
        leverage=req.leverage,
        planned_stop=req.planned_stop,
        planned_tp1=req.planned_tp1,
        planned_tp2=req.planned_tp2,
        entry_fee=req.entry_fee,
        source="managed",
        notes=req.notes or f"confirmado do painel (qty {qty_source}) — gerenciado pelo bot",
    )
    if result is None:
        raise HTTPException(503, "DB desabilitado")

    # 4. Coloca o bracket de proteção (SL + TP1 parcial + TP2) na posição já
    #    aberta. Os níveis já foram resolvidos da rec dentro de open_trade →
    #    lê do result. Se faltar SL, não coloca (sem stop não há bracket); a
    #    auto-cura do trade-manager tentará recriar pernas faltantes nos ticks
    #    seguintes. Falha aqui NÃO derruba o registro — o trade fica gravado e
    #    o lifecycle assume.
    trade_id = result["id"]
    sl_lvl = result.get("planned_stop")
    tp1_lvl = result.get("planned_tp1")
    tp2_lvl = result.get("planned_tp2")
    protection: dict = {"placed": False}
    if sl_lvl and sl_lvl > 0:
        try:
            from services import binance_signed_service
            from db import get_session
            from sqlalchemy import select
            from models.real_trade import RealTrade

            entry_side = "Buy" if req.side == "long" else "Sell"
            prot = await binance_signed_service.place_protection_orders(
                req.symbol, entry_side, qty=qty,
                stop_loss=sl_lvl, tp1=tp1_lvl, tp2=tp2_lvl,
                client_order_id_prefix=f"cw-managed-{trade_id}",
                dedup_live=True,  # anti-dup: pula perna que já exista viva
            )
            async with get_session() as session:
                fresh = (await session.execute(
                    select(RealTrade).where(RealTrade.id == trade_id)
                )).scalar_one_or_none()
                if fresh:
                    if prot.get("sl_ok"):
                        fresh.sl_order_id = prot.get("sl_order_id")
                        fresh.sl_current_price = sl_lvl
                    if prot.get("tp1_ok") and tp1_lvl:
                        fresh.tp1_order_id = prot.get("tp1_order_id")
                    if prot.get("tp2_ok"):
                        fresh.tp2_order_id = prot.get("tp2_order_id")
                    await session.commit()
            protection = {
                "placed": bool(prot.get("sl_ok")),
                "sl_ok": prot.get("sl_ok"), "sl_msg": prot.get("sl_msg"),
                "tp1_ok": prot.get("tp1_ok"), "tp1_msg": prot.get("tp1_msg"),
                "tp1_skipped": prot.get("tp1_skipped"),
                "tp2_ok": prot.get("tp2_ok"), "tp2_msg": prot.get("tp2_msg"),
            }
            log.info(
                f"[confirm-entry] proteção #{trade_id} {req.symbol}: "
                f"SL={prot.get('sl_order_id')} TP1={prot.get('tp1_order_id')} "
                f"TP2={prot.get('tp2_order_id')}"
            )
        except Exception as e:
            log.error(f"[confirm-entry] colocar proteção #{trade_id} falhou: {e}")
            protection = {"placed": False, "error": str(e)}
    else:
        protection = {"placed": False, "error": "sem planned_stop — bracket não criado"}

    # 5. Notifica abertura do trade gerenciado (Telegram + push). O caminho
    #    auto/shadow já notifica em shadow_trade_service; o managed (confirmado
    #    do painel) não tinha notificação — adicionado aqui. Best-effort: no-op
    #    silencioso se Telegram/push não estiverem configurados; falha não
    #    derruba a resposta.
    try:
        from services.notification_service import send_telegram, fmt_trade_opened
        _notify_trade = {
            **result,
            "planned_stop": sl_lvl,
            "planned_tp1": tp1_lvl,
            "planned_tp2": tp2_lvl,
            "qty": qty,
        }
        _notify_rec = {"timeframe": req.timeframe or result.get("timeframe") or "?"}
        _bracket = "✅ bracket colocado" if protection.get("placed") else "⚠️ sem bracket (SL pendente)"
        await send_telegram(
            fmt_trade_opened(_notify_trade, _notify_rec)
            + f"\n_Gerenciado · {_bracket}_",
            event_type="open_managed",
        )
    except Exception as e:
        log.warning(f"[confirm-entry] telegram open falhou: {e}")
    try:
        from services import push_service
        await push_service.notify_trade_open({
            **result,
            "planned_stop": sl_lvl,
            "planned_tp1": tp1_lvl,
            "planned_tp2": tp2_lvl,
            "qty": qty,
            "source": "managed",
        })
    except Exception as e:
        log.warning(f"[confirm-entry] push open falhou: {e}")

    return {
        **result,
        "qty_source": qty_source,
        "linked_recommendation_id": rec_id,
        "protection": protection,
    }


# ─── Trade manager (bracket TP1/TP2 + breakeven pós-TP1, Fase 2) ─────────────


@app.get("/api/trade-manager/status")
async def trade_manager_status():
    """Snapshot de trades sob gerenciamento ativo (fase, qty, ordens condicionais)."""
    from services import trade_manager_service
    return await trade_manager_service.get_status()


@app.post("/api/trade-manager/backfill-protection")
async def trade_manager_backfill(force: bool = False):
    """
    Cria SL + TP1 + TP2 na exchange pros trades 'open' source='auto'.

    - force=false (default): só atua nos sem sl_order_id setado.
    - force=true: ignora sl_order_id (re-cria mesmo se já tem); em trades
      já pós-TP1, cria SL em entry (BE) + TP2 (pula TP1).
    """
    from services import trade_manager_service
    return await trade_manager_service.backfill_protection(force=force)


# ─── Exchange signed endpoints (#11) ──────────────────────────────────────────
# Endpoints sob /api/exchange/* usam o cliente ativo (EXCHANGE=binance|bybit).
# Aliases /api/bybit/* e /api/binance/* forçam o cliente específico — úteis
# pra debug/comparação. UI deve preferir /api/exchange/*.


@app.get("/api/exchange/env")
async def exchange_env():
    """Qual corretora está ativa + diagnóstico (testnet, key configurada)."""
    from services import exchange_service
    return exchange_service.env_info()


@app.get("/api/shadow/env")
async def shadow_env():
    """Status do shadow trade (#11.3) — se ativo, abre RealTrades sem exchange."""
    from services import shadow_trade_service
    return shadow_trade_service.env_info()


@app.get("/api/live/preflight")
async def live_preflight(symbol: str = "BTC/USDT:USDT", direction: str = "long"):
    """
    Preflight go/no-go pré-dinheiro-real. Roda READ-ONLY todos os gates que só
    ativam em live (kill-switch, funding, throttle, caps, cooldown, breaker,
    equity, etc.) e reporta ok/erro de cada um — SEM enviar nenhuma ordem.
    `ready=true` quando os gates críticos (equity real, kill-switch, exchange
    configurada) passam. Use antes de armar live e de novo na sexta.
    """
    from services import shadow_trade_service
    return await shadow_trade_service.preflight_live_checks(
        sample_symbol=symbol, sample_direction=direction
    )


@app.get("/api/exchange/reconcile")
async def exchange_reconcile():
    """
    Reconcilia posições reais na exchange × trades OPEN no DB (go-live #4).
    Não muta nada — só reporta drift: órfãos no DB (trade aberto sem posição
    viva) e posições não-gerenciadas (posição viva sem trade no DB).
    """
    from services import shadow_trade_service
    return await shadow_trade_service.reconcile_open_positions()


@app.get("/api/shadow/skip-reasons")
async def shadow_skip_reasons():
    """
    Por que cada tier A/A+ recente NÃO virou trade na exchange. Mostra o gate
    que barrou (score-min, direction-cap, cluster-cap, proximity, cooldown…),
    o motivo legível e quando. Diagnóstico pra 'a moeda é tier A e não entrou'.
    """
    from services import shadow_trade_service
    reasons = shadow_trade_service.get_skip_reasons()
    return {"ok": True, "count": len(reasons), "items": reasons}


def _check_admin_token(token: Optional[str]) -> Optional[dict]:
    """
    Auth de endpoints admin que emitem ordem real. Retorna None se liberado, ou
    um dict de erro se bloqueado.

    Regra:
      - ADMIN_API_TOKEN setado → exige header X-Admin-Token igual (sempre).
      - ADMIN_API_TOKEN vazio:
          · produção (mainnet) → BLOQUEIA (não deixa endpoint perigoso aberto).
          · demo/testnet       → libera (conveniência, dinheiro fake).
    """
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if expected:
        if (token or "").strip() != expected:
            return {"ok": False, "error": "token inválido ou ausente (header X-Admin-Token)"}
        return None
    # Sem token configurado — só libera fora de produção
    try:
        from services import shadow_trade_service
        prod = shadow_trade_service._exchange_is_production()
    except Exception:
        prod = True  # fail-safe
    if prod:
        return {
            "ok": False,
            "error": "ADMIN_API_TOKEN não configurado — endpoint admin bloqueado em produção (mainnet)",
        }
    return None


@app.post("/api/admin/force-test-trade")
async def admin_force_test_trade(
    symbol: str = "BTCUSDT",
    side: str = "Buy",            # "Buy" | "Sell"
    notional_usd: float = 50.0,
    leverage: int = 5,
    close_after: bool = True,     # se True, emite ordem oposta reduceOnly logo após
    x_admin_token: Optional[str] = Header(None),
):
    """
    Ordem REAL de teste na exchange ativa — valida que auth, perms e place_order
    funcionam end-to-end. Default: BTC Buy $50 notional 5x, fecha logo após.

    Protegido: em mainnet exige header X-Admin-Token == ADMIN_API_TOKEN. Em
    demo/testnet libera sem token (dinheiro fake). Veja _check_admin_token.
    """
    gate = _check_admin_token(x_admin_token)
    if gate is not None:
        raise HTTPException(status_code=403, detail=gate["error"])

    import httpx
    from services import exchange_service, binance_signed_service

    # 1. Fetch current price (público, sem auth) — usa o BASE da exchange ativa
    sym = symbol.upper().replace("/", "").replace(":USDT", "")
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(f"{binance_signed_service.BASE}/fapi/v1/ticker/price?symbol={sym}")
            data = r.json()
            mark = float(data.get("price") or 0)
    except Exception as e:
        return {"ok": False, "step": "fetch_price", "error": str(e)}

    if mark <= 0:
        return {"ok": False, "step": "fetch_price", "error": f"preço inválido: {data}"}

    # 2. Computa qty pra atingir o notional alvo
    qty = round(notional_usd / mark, 4)
    if qty <= 0:
        return {"ok": False, "step": "compute_qty", "mark": mark, "qty": qty,
                "error": "qty=0; aumente notional_usd"}

    # 3. Place order (Market, sem TP/SL pra teste simples)
    open_res = await exchange_service.place_order(
        symbol=sym, side=side, qty=qty,
        order_type="Market", leverage=leverage,
        client_order_id=f"cw-test-{int(__import__('time').time())}",
    )
    result = {
        "step": "place_order", "symbol": sym, "side": side, "qty": qty,
        "leverage": leverage, "mark_price_at_request": mark,
        "notional_target_usd": notional_usd,
        "open_response": open_res,
    }
    if not open_res.get("ok"):
        result["ok"] = False
        return result

    # 4. Fecha imediatamente se solicitado (reduceOnly oposto)
    if close_after:
        opposite = "Sell" if side.lower() == "buy" else "Buy"
        close_res = await exchange_service.place_order(
            symbol=sym, side=opposite, qty=qty,
            order_type="Market", reduce_only=True,
            client_order_id=f"cw-test-close-{int(__import__('time').time())}",
        )
        result["close_response"] = close_res
        result["ok"] = bool(close_res.get("ok"))
    else:
        result["ok"] = True
        result["note"] = "posição aberta na exchange — feche manualmente"

    return result


@app.post("/api/admin/telegram-test")
async def admin_telegram_test():
    """Testa Telegram. Retorna ok=False se nao configurado."""
    from services.notification_service import send_telegram, TELEGRAM_ENABLED
    if not TELEGRAM_ENABLED:
        return {"ok": False, "reason": "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID nao configurado"}
    sent = await send_telegram(
        "\U0001F916 *Teste* \u2014 Bot Crypto Win conectado ao Telegram!",
        event_type="test",
    )
    return {"ok": sent}


@app.post("/api/admin/test-push")
async def admin_test_push(kind: str = "trade_open"):
    """
    Dispara um push de teste pra TODOS subscribers ativos.
    kind: "trade_open" (default) | "outcome_tp2" | "outcome_lost" | "rec_new"
    Útil pra confirmar que push está funcionando independente do app aberto.
    """
    from services import push_service
    if kind == "trade_open":
        fake_trade = {
            "id": 0, "symbol": "BTCUSDT", "side": "long",
            "qty": 0.0009, "entry_price": 67000.0, "leverage": 5,
            "planned_stop": 66000.0, "planned_tp1": 68000.0, "planned_tp2": 69000.0,
            "source": "auto", "exchange": "binance",
        }
        sent = await push_service.notify_trade_open(fake_trade)
        return {"kind": kind, "sent": sent}
    if kind in ("outcome_tp2", "outcome_lost"):
        class _FakeSnap:
            symbol = "BTCUSDT"; tier = "A+"; direction = "long"
            timeframe = "1h"; realized_r = 2.1 if kind == "outcome_tp2" else -1.0
        event = "tp2" if kind == "outcome_tp2" else "lost"
        sent = await push_service.notify_outcome(_FakeSnap(), event)
        return {"kind": kind, "sent": sent}
    if kind == "rec_new":
        fake_rec = {
            "symbol": "BTCUSDT", "tier": "A+", "direction": "long",
            "timeframe": "1h", "leverage": 5, "score": 85.0,
            "risk_reward": 2.5, "entry": 67000.0,
        }
        sent = await push_service.notify_new_recommendation(fake_rec)
        return {"kind": kind, "sent": sent}
    return {"error": f"kind desconhecido: {kind}"}


@app.get("/api/exchange/equity")
async def exchange_equity(force: bool = False):
    """
    Saldo real da exchange ativa (Binance/Bybit) com cache de 60s.
    Usado pelo sizing de shadow/auto e exibido no dashboard.
    `force=true` ignora cache.
    """
    from services import exchange_service
    return await exchange_service.get_equity(force=force)


@app.get("/api/exchange/diagnostic")
async def exchange_diagnostic():
    """Debug verboso de auth da Bybit: query-api, wallet UNIFIED + CONTRACT.
    Mostra resposta crua de cada chamada — útil quando 'API key is invalid'
    persiste e a gente precisa entender se é permission, account type, etc."""
    from services import bybit_signed_service
    if hasattr(bybit_signed_service, "diagnostic"):
        return await bybit_signed_service.diagnostic()
    return {"error": "diagnostic não suportado pelo cliente ativo"}


@app.get("/api/exchange/diagnostic-endpoints")
async def exchange_diagnostic_endpoints():
    """Testa a MESMA chave contra testnet, demo e mainnet pra descobrir
    em qual sistema ela está registrada. Bybit tem 2 ambientes separados
    (testnet.bybit.com vs demo trading dentro da conta principal)."""
    from services import bybit_signed_service
    if hasattr(bybit_signed_service, "diagnostic_endpoints"):
        return await bybit_signed_service.diagnostic_endpoints()
    return {"error": "diagnostic_endpoints não suportado pelo cliente ativo"}


@app.get("/api/kill-switch/status")
async def kill_switch_status():
    """Estado atual do circuit breaker — checks, thresholds, motivo de bloqueio se houver.
    UI pode pollar isso pra mostrar warning quando próximo dos limites."""
    from services import kill_switch_service
    return await kill_switch_service.status()


@app.post("/api/admin/recalc-pnl-zero-entry")
async def admin_recalc_pnl_zero_entry(dry_run: bool = True):
    """
    Recalcula pnl_usd / pnl_pct / realized_r de trades fechados que ficaram
    com entry_price=0 (bug histórico — market order da Binance voltou avgPrice=0).

    Fallback em cascata:
      1. Tenta /fapi/v2/positionRisk (improvável funcionar — posição já fechou)
      2. Média entre planned_stop e planned_tp1
      3. Último recurso: exit_price (PnL = 0)

    Use dry_run=true (default) primeiro pra ver diff antes de aplicar.
    """
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.real_trade import RealTrade
    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    fixes = []
    async with get_session() as session:
        stmt = select(RealTrade).where(
            RealTrade.status != "open",
            RealTrade.entry_price <= 0,
        )
        rows = (await session.execute(stmt)).scalars().all()

        for t in rows:
            # Recupera entry com mesma cascata do real_trade_service.close_trade
            recovered = None
            if t.planned_stop and t.planned_tp1:
                recovered = (float(t.planned_stop) + float(t.planned_tp1)) / 2.0
                src = "media_stop_tp1"
            else:
                recovered = float(t.exit_price or 0)
                src = "exit_price"

            if recovered <= 0:
                fixes.append({
                    "id": t.id, "symbol": t.symbol, "skip": "no fallback",
                })
                continue

            sign = 1 if t.side == "long" else -1
            price_diff = (float(t.exit_price or 0) - recovered) * sign
            new_pnl_usd = round(price_diff * float(t.qty) - float(t.entry_fee or 0) - float(t.exit_fee or 0), 4)
            new_pnl_pct = round((price_diff / recovered) * 100, 4)
            new_r = None
            if t.planned_stop:
                risk_dist = abs(recovered - float(t.planned_stop))
                if risk_dist > 0:
                    new_r = round(price_diff / risk_dist, 3)

            fix = {
                "id": t.id, "symbol": t.symbol, "side": t.side,
                "entry_recovered": recovered, "recover_source": src,
                "exit_price": float(t.exit_price or 0),
                "old": {"pnl_usd": float(t.pnl_usd or 0), "pnl_pct": float(t.pnl_pct or 0), "realized_r": t.realized_r},
                "new": {"pnl_usd": new_pnl_usd, "pnl_pct": new_pnl_pct, "realized_r": new_r},
            }
            fixes.append(fix)

            if not dry_run:
                t.entry_price = recovered
                t.pnl_usd = new_pnl_usd
                t.pnl_pct = new_pnl_pct
                t.realized_r = new_r
                t.notes = (t.notes or "") + f" | pnl recalc (entry 0 → {recovered:.6f} via {src})"

        if not dry_run:
            await session.commit()

    return {
        "ok": True,
        "dry_run": dry_run,
        "trades_affected": len(fixes),
        "fixes": fixes,
        "note": "Use ?dry_run=false pra aplicar.",
    }


@app.post("/api/admin/dedupe-open-trades")
async def admin_dedupe_open_trades(dry_run: bool = True):
    """
    Limpa trades duplicados legados (mesmo símbolo+direção abertos em TFs
    diferentes) que ficaram de antes da Fase 1 (snapshot 1-rec-por-direção).

    Agrupa RealTrade status=open por (symbol, side). Pra cada grupo com >1
    trade, mantém o de TF maior (SCALP<DAY<SWING via snapshot_service._tf_rank).
    Empate de rank → mantém o mais recente (opened_at desc). Demais são
    fechados via trade_manager_service._close_trade (cancela algo orders +
    fecha posição na Binance demo).

    Use dry_run=true (default) primeiro pra ver o plano.
    """
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot
    from services.snapshot_service import _tf_rank
    from services import trade_manager_service

    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    groups: dict[tuple[str, str], list[dict]] = {}
    actions: list[dict] = []
    errors: list[dict] = []
    trades_to_close = 0
    trades_to_keep = 0

    async with get_session() as session:
        stmt = select(RealTrade).where(RealTrade.status == "open")
        opens = (await session.execute(stmt)).scalars().all()

        # Resolve TF de cada trade via recommendation_snapshot
        rec_ids = [t.recommendation_id for t in opens if t.recommendation_id]
        tf_by_rec: dict[int, str] = {}
        if rec_ids:
            rec_stmt = select(RecommendationSnapshot.id, RecommendationSnapshot.timeframe).where(
                RecommendationSnapshot.id.in_(rec_ids)
            )
            for rid, tf in (await session.execute(rec_stmt)).all():
                tf_by_rec[rid] = tf

        for t in opens:
            tf = tf_by_rec.get(t.recommendation_id, "") if t.recommendation_id else ""
            key = (t.symbol, t.side)
            groups.setdefault(key, []).append({
                "trade": t,
                "tf": tf,
                "rank": _tf_rank(tf),
                "opened_at": t.opened_at,
            })

        groups_with_dupes = 0
        for (symbol, side), items in groups.items():
            if len(items) <= 1:
                continue
            groups_with_dupes += 1
            # Ordena: rank desc, opened_at desc → primeiro é o "keeper"
            items.sort(key=lambda x: (x["rank"], x["opened_at"]), reverse=True)
            keep = items[0]
            close_list = items[1:]
            trades_to_keep += 1
            trades_to_close += len(close_list)

            action = {
                "symbol": symbol,
                "direction": side,
                "keep": {
                    "id": keep["trade"].id,
                    "tf": keep["tf"] or None,
                    "opened_at": keep["opened_at"].isoformat() if keep["opened_at"] else None,
                },
                "close": [
                    {
                        "id": c["trade"].id,
                        "tf": c["tf"] or None,
                        "opened_at": c["opened_at"].isoformat() if c["opened_at"] else None,
                        "reason": "lower_tf" if c["rank"] < keep["rank"] else "older_same_rank",
                    }
                    for c in close_list
                ],
            }
            actions.append(action)

            log.info(
                f"[admin-dedupe] {symbol} {side}: keep #{keep['trade'].id} "
                f"(tf={keep['tf']}), close {[c['trade'].id for c in close_list]} "
                f"(dry_run={dry_run})"
            )

            if not dry_run:
                for c in close_list:
                    try:
                        await trade_manager_service._close_trade(c["trade"], "dedupe_legacy")
                        log.info(f"[admin-dedupe] closed trade #{c['trade'].id} ({symbol})")
                    except Exception as e:
                        log.error(f"[admin-dedupe] erro fechando #{c['trade'].id}: {e}")
                        errors.append({"trade_id": c["trade"].id, "symbol": symbol, "error": str(e)})

    return {
        "ok": True,
        "dry_run": dry_run,
        "groups_with_duplicates": groups_with_dupes,
        "trades_to_close": trades_to_close,
        "trades_to_keep": trades_to_keep,
        "actions": actions,
        "errors": errors,
        "note": "Use ?dry_run=false pra executar.",
    }


@app.get("/api/admin/loss-postmortem")
async def admin_loss_postmortem(hours: int = 24):
    """
    Analisa trades perdedores das últimas N horas pra encontrar padrões.

    Junta RealTrade (status closed_stop OU pnl_usd<0) com RecommendationSnapshot
    via recommendation_id pra puxar tier/score/timeframe/features. Devolve
    agregações (por símbolo, TF, tier, hora UTC, direção), comparação score
    win vs loss, distribuição de tempo até SL, e top 20 piores trades.

    Best-effort: campos ausentes viram null, não trava o endpoint.
    """
    import statistics
    from datetime import timedelta
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.real_trade import RealTrade
    from models.recommendation_snapshot import RecommendationSnapshot

    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    hours = max(1, min(int(hours), 24 * 30))  # clamp 1h..30d
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    logging.info(f"[loss-postmortem] window={hours}h cutoff={cutoff.isoformat()}")

    async with get_session() as session:
        stmt = select(RealTrade).where(
            RealTrade.status != "open",
            RealTrade.closed_at >= cutoff,
        )
        closed = (await session.execute(stmt)).scalars().all()

        rec_ids = [t.recommendation_id for t in closed if t.recommendation_id]
        rec_by_id: dict[int, RecommendationSnapshot] = {}
        if rec_ids:
            rec_stmt = select(RecommendationSnapshot).where(
                RecommendationSnapshot.id.in_(rec_ids)
            )
            for r in (await session.execute(rec_stmt)).scalars().all():
                rec_by_id[r.id] = r

    def is_loss(t: RealTrade) -> bool:
        if t.pnl_usd is not None:
            return float(t.pnl_usd) < 0
        return t.status == "closed_stop"

    def is_win(t: RealTrade) -> bool:
        if t.pnl_usd is not None:
            return float(t.pnl_usd) > 0
        return t.status in ("closed_tp1", "closed_tp2")

    wins = [t for t in closed if is_win(t)]
    losses = [t for t in closed if is_loss(t)]
    total_closed = len(closed)

    gross_win = sum(float(t.pnl_usd or 0) for t in wins)
    gross_loss = sum(float(t.pnl_usd or 0) for t in losses)
    net_pnl = gross_win + gross_loss
    win_rate = (len(wins) / total_closed * 100) if total_closed else 0.0
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else None

    # by_symbol
    sym_stats: dict[str, dict] = {}
    for t in closed:
        s = sym_stats.setdefault(t.symbol, {"losses": 0, "total": 0, "net_pnl": 0.0})
        s["total"] += 1
        s["net_pnl"] += float(t.pnl_usd or 0)
        if is_loss(t):
            s["losses"] += 1
    by_symbol = sorted(
        [
            {
                "symbol": k,
                "losses": v["losses"],
                "total": v["total"],
                "loss_rate_pct": round(v["losses"] / v["total"] * 100, 1) if v["total"] else 0.0,
                "net_pnl": round(v["net_pnl"], 4),
            }
            for k, v in sym_stats.items()
            if v["losses"] > 0
        ],
        key=lambda x: x["losses"],
        reverse=True,
    )[:5]

    # by_timeframe
    tf_stats: dict[str, dict] = {}
    for t in losses:
        rec = rec_by_id.get(t.recommendation_id) if t.recommendation_id else None
        tf = rec.timeframe if rec else "unknown"
        s = tf_stats.setdefault(tf, {"losses": 0, "net_pnl": 0.0})
        s["losses"] += 1
        s["net_pnl"] += float(t.pnl_usd or 0)
    by_timeframe = [
        {"timeframe": k, "losses": v["losses"], "net_pnl": round(v["net_pnl"], 4)}
        for k, v in sorted(tf_stats.items(), key=lambda x: x[1]["losses"], reverse=True)
    ]

    # by_tier
    tier_stats: dict[str, dict] = {}
    for t in losses:
        rec = rec_by_id.get(t.recommendation_id) if t.recommendation_id else None
        tier = rec.tier if rec else "unknown"
        s = tier_stats.setdefault(tier, {"losses": 0, "net_pnl": 0.0})
        s["losses"] += 1
        s["net_pnl"] += float(t.pnl_usd or 0)
    by_tier = [
        {"tier": k, "losses": v["losses"], "net_pnl": round(v["net_pnl"], 4)}
        for k, v in sorted(tier_stats.items(), key=lambda x: x[1]["losses"], reverse=True)
    ]

    # by_hour_utc
    hour_stats: dict[int, int] = {}
    for t in losses:
        if t.opened_at:
            h = t.opened_at.astimezone(timezone.utc).hour if t.opened_at.tzinfo else t.opened_at.hour
            hour_stats[h] = hour_stats.get(h, 0) + 1
    by_hour_utc = [
        {"hour": h, "losses": hour_stats[h]}
        for h in sorted(hour_stats.keys())
    ]

    # by_direction
    dir_stats = {"long": {"losses": 0, "wins": 0}, "short": {"losses": 0, "wins": 0}}
    for t in closed:
        side = t.side if t.side in ("long", "short") else None
        if not side:
            continue
        if is_loss(t):
            dir_stats[side]["losses"] += 1
        elif is_win(t):
            dir_stats[side]["wins"] += 1

    # score analysis
    def _score_of(t: RealTrade) -> float | None:
        rec = rec_by_id.get(t.recommendation_id) if t.recommendation_id else None
        return float(rec.score) if rec and rec.score is not None else None

    win_scores = [s for s in (_score_of(t) for t in wins) if s is not None]
    loss_scores = [s for s in (_score_of(t) for t in losses) if s is not None]
    score_analysis = {
        "avg_score_wins": round(statistics.mean(win_scores), 2) if win_scores else None,
        "avg_score_losses": round(statistics.mean(loss_scores), 2) if loss_scores else None,
        "median_score_wins": round(statistics.median(win_scores), 2) if win_scores else None,
        "median_score_losses": round(statistics.median(loss_scores), 2) if loss_scores else None,
    }

    # time to SL
    durations_min: list[float] = []
    for t in losses:
        if t.opened_at and t.closed_at:
            try:
                d = (t.closed_at - t.opened_at).total_seconds() / 60.0
                if d >= 0:
                    durations_min.append(d)
            except Exception:
                pass
    time_to_sl = {
        "avg": round(statistics.mean(durations_min), 2) if durations_min else None,
        "median": round(statistics.median(durations_min), 2) if durations_min else None,
        "fast_losses_under_5min": sum(1 for d in durations_min if d < 5),
        "slow_losses_over_2h": sum(1 for d in durations_min if d > 120),
    }

    # loss details (top 20 piores por PnL ascendente)
    losses_sorted = sorted(losses, key=lambda t: float(t.pnl_usd or 0))[:20]
    loss_details = []
    for t in losses_sorted:
        rec = rec_by_id.get(t.recommendation_id) if t.recommendation_id else None
        dur_min = None
        if t.opened_at and t.closed_at:
            try:
                dur_min = round((t.closed_at - t.opened_at).total_seconds() / 60.0, 2)
            except Exception:
                pass
        loss_details.append({
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.side,
            "tf": rec.timeframe if rec else None,
            "tier": rec.tier if rec else None,
            "score": float(rec.score) if rec and rec.score is not None else None,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "duration_min": dur_min,
            "entry": float(t.entry_price) if t.entry_price is not None else None,
            "stop": float(t.planned_stop) if t.planned_stop is not None else None,
            "tp1": float(t.planned_tp1) if t.planned_tp1 is not None else None,
            "tp2": float(t.planned_tp2) if t.planned_tp2 is not None else None,
            "pnl_usd": round(float(t.pnl_usd), 4) if t.pnl_usd is not None else None,
            "status": t.status,
            "notes": t.notes,
        })

    logging.info(
        f"[loss-postmortem] closed={total_closed} wins={len(wins)} losses={len(losses)} "
        f"win_rate={win_rate:.1f}% net=${net_pnl:.2f}"
    )

    return {
        "ok": True,
        "window_hours": hours,
        "summary": {
            "total_trades_closed": total_closed,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "gross_win_usd": round(gross_win, 4),
            "gross_loss_usd": round(gross_loss, 4),
            "net_pnl_usd": round(net_pnl, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        },
        "by_symbol": by_symbol,
        "by_timeframe": by_timeframe,
        "by_tier": by_tier,
        "by_hour_utc": by_hour_utc,
        "by_direction": dir_stats,
        "score_analysis": score_analysis,
        "time_to_sl_minutes": time_to_sl,
        "loss_details": loss_details,
    }


@app.get("/api/admin/loss-postmortem-snapshots")
async def admin_loss_postmortem_snapshots(hours: int = 24):
    """
    Postmortem das RECOMENDAÇÕES tracked (paper/shadow) das últimas N horas.

    Análogo ao /api/admin/loss-postmortem (que opera em RealTrade), mas aqui o
    universo é RecommendationSnapshot — onde mora o tracking dos sinais que o
    painel sugere, mesmo sem trade real executado. Métrica é R multiple (paper).

    Status terminais: won_tp1, won_tp1_be, won_tp2, lost, expired. "open" sai.
    Best-effort: campos ausentes viram null.
    """
    import statistics
    from datetime import timedelta
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.recommendation_snapshot import RecommendationSnapshot

    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    hours = max(1, min(int(hours), 720))  # clamp 1h..30d
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    logging.info(f"[loss-postmortem-snap] window={hours}h cutoff={cutoff.isoformat()}")

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status != "open",
            RecommendationSnapshot.outcome_at >= cutoff,
        )
        closed = (await session.execute(stmt)).scalars().all()

    def r_of(s: RecommendationSnapshot) -> float | None:
        return float(s.realized_r) if s.realized_r is not None else None

    def is_loss(s: RecommendationSnapshot) -> bool:
        r = r_of(s)
        if r is not None:
            return r < 0
        return s.status == "lost"

    def is_win(s: RecommendationSnapshot) -> bool:
        r = r_of(s)
        if r is not None:
            return r > 0
        return s.status in ("won_tp1", "won_tp1_be", "won_tp2")

    wins = [s for s in closed if is_win(s)]
    losses = [s for s in closed if is_loss(s)]
    total_closed = len(closed)

    gross_win_R = sum((r_of(s) or 0.0) for s in wins)
    gross_loss_R = sum((r_of(s) or 0.0) for s in losses)
    net_R = gross_win_R + gross_loss_R
    win_rate = (len(wins) / total_closed * 100) if total_closed else 0.0
    profit_factor = (gross_win_R / abs(gross_loss_R)) if gross_loss_R < 0 else None

    # by_symbol — top 10 piores
    sym_stats: dict[str, dict] = {}
    for s in closed:
        d = sym_stats.setdefault(s.symbol, {"losses": 0, "total": 0, "net_R": 0.0})
        d["total"] += 1
        d["net_R"] += (r_of(s) or 0.0)
        if is_loss(s):
            d["losses"] += 1
    by_symbol = sorted(
        [
            {
                "symbol": k,
                "losses": v["losses"],
                "total": v["total"],
                "loss_rate_pct": round(v["losses"] / v["total"] * 100, 1) if v["total"] else 0.0,
                "net_R": round(v["net_R"], 4),
            }
            for k, v in sym_stats.items()
            if v["losses"] > 0
        ],
        key=lambda x: x["losses"],
        reverse=True,
    )[:10]

    # by_timeframe — agrupa em SCALP/DAY/SWING
    def _tf_bucket(tf: str | None) -> str:
        if not tf:
            return "unknown"
        t = tf.lower()
        if t in ("1m", "3m", "5m", "15m"):
            return "SCALP"
        if t in ("30m", "1h", "2h", "4h"):
            return "DAY"
        if t in ("6h", "8h", "12h", "1d", "1w", "1mo"):
            return "SWING"
        return tf
    tf_stats: dict[str, dict] = {}
    for s in losses:
        bucket = _tf_bucket(s.timeframe)
        d = tf_stats.setdefault(bucket, {"losses": 0, "net_R": 0.0})
        d["losses"] += 1
        d["net_R"] += (r_of(s) or 0.0)
    by_timeframe = [
        {"timeframe": k, "losses": v["losses"], "net_R": round(v["net_R"], 4)}
        for k, v in sorted(tf_stats.items(), key=lambda x: x[1]["losses"], reverse=True)
    ]

    # by_tier
    tier_stats: dict[str, dict] = {}
    for s in losses:
        tier = s.tier or "unknown"
        d = tier_stats.setdefault(tier, {"losses": 0, "net_R": 0.0})
        d["losses"] += 1
        d["net_R"] += (r_of(s) or 0.0)
    by_tier = [
        {"tier": k, "losses": v["losses"], "net_R": round(v["net_R"], 4)}
        for k, v in sorted(tier_stats.items(), key=lambda x: x[1]["losses"], reverse=True)
    ]

    # by_hour_utc — histograma dos LOSSES (usa created_at, equivalente ao "opened_at")
    hour_stats: dict[int, int] = {}
    for s in losses:
        ts = s.created_at
        if ts:
            h = ts.astimezone(timezone.utc).hour if ts.tzinfo else ts.hour
            hour_stats[h] = hour_stats.get(h, 0) + 1
    by_hour_utc = [
        {"hour": h, "losses": hour_stats[h]}
        for h in sorted(hour_stats.keys())
    ]

    # by_direction
    dir_stats = {"long": {"losses": 0, "wins": 0}, "short": {"losses": 0, "wins": 0}}
    for s in closed:
        side = s.direction if s.direction in ("long", "short") else None
        if not side:
            continue
        if is_loss(s):
            dir_stats[side]["losses"] += 1
        elif is_win(s):
            dir_stats[side]["wins"] += 1

    # by_leverage — bucketiza por valor exato; expõe wins+losses
    lev_stats: dict[str, dict] = {}
    for s in closed:
        lev = s.leverage if s.leverage is not None else None
        key = f"{int(lev)}x" if lev is not None else "unknown"
        d = lev_stats.setdefault(key, {"losses": 0, "wins": 0, "total": 0, "net_R": 0.0})
        d["total"] += 1
        d["net_R"] += (r_of(s) or 0.0)
        if is_loss(s):
            d["losses"] += 1
        elif is_win(s):
            d["wins"] += 1
    def _lev_sort_key(k: str) -> int:
        try:
            return int(k.rstrip("x"))
        except Exception:
            return 999
    by_leverage = [
        {
            "leverage": k,
            "wins": v["wins"],
            "losses": v["losses"],
            "total": v["total"],
            "net_R": round(v["net_R"], 4),
        }
        for k, v in sorted(lev_stats.items(), key=lambda x: _lev_sort_key(x[0]))
    ]

    # score_analysis
    win_scores = [float(s.score) for s in wins if s.score is not None]
    loss_scores = [float(s.score) for s in losses if s.score is not None]
    score_analysis = {
        "avg_score_wins": round(statistics.mean(win_scores), 2) if win_scores else None,
        "avg_score_losses": round(statistics.mean(loss_scores), 2) if loss_scores else None,
        "median_score_wins": round(statistics.median(win_scores), 2) if win_scores else None,
        "median_score_losses": round(statistics.median(loss_scores), 2) if loss_scores else None,
    }

    # time to SL (losses)
    durations_min: list[float] = []
    for s in losses:
        if s.created_at and s.outcome_at:
            try:
                d = (s.outcome_at - s.created_at).total_seconds() / 60.0
                if d >= 0:
                    durations_min.append(d)
            except Exception:
                pass
    time_to_sl = {
        "avg": round(statistics.mean(durations_min), 2) if durations_min else None,
        "median": round(statistics.median(durations_min), 2) if durations_min else None,
        "fast_losses_under_5min": sum(1 for d in durations_min if d < 5),
        "slow_losses_over_2h": sum(1 for d in durations_min if d > 120),
    }

    # loss_details top 20 piores (R ascendente)
    losses_sorted = sorted(losses, key=lambda s: (r_of(s) if r_of(s) is not None else 0.0))[:20]
    loss_details = []
    for s in losses_sorted:
        dur_min = None
        if s.created_at and s.outcome_at:
            try:
                dur_min = round((s.outcome_at - s.created_at).total_seconds() / 60.0, 2)
            except Exception:
                pass
        r_mult = r_of(s)
        pct_banca = None
        try:
            if r_mult is not None and s.risk_pct is not None:
                pct_banca = round(float(r_mult) * float(s.risk_pct), 4)
        except Exception:
            pass
        loss_details.append({
            "id": s.id,
            "symbol": s.symbol,
            "direction": s.direction,
            "tf": s.timeframe,
            "tier": s.tier,
            "score": float(s.score) if s.score is not None else None,
            "leverage": int(s.leverage) if s.leverage is not None else None,
            "opened_at": s.created_at.isoformat() if s.created_at else None,
            "closed_at": s.outcome_at.isoformat() if s.outcome_at else None,
            "duration_min": dur_min,
            "entry": float(s.entry) if s.entry is not None else None,
            "stop": float(s.stop_loss) if s.stop_loss is not None else None,
            "tp1": float(s.tp1) if s.tp1 is not None else None,
            "r_multiple": round(float(r_mult), 4) if r_mult is not None else None,
            "pct_banca_loss": pct_banca,
            "status": s.status,
        })

    logging.info(
        f"[loss-postmortem-snap] closed={total_closed} wins={len(wins)} losses={len(losses)} "
        f"win_rate={win_rate:.1f}% net_R={net_R:.2f}"
    )

    return {
        "ok": True,
        "window_hours": hours,
        "summary": {
            "unit": "R",
            "total_snapshots_closed": total_closed,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "gross_win_R": round(gross_win_R, 4),
            "gross_loss_R": round(gross_loss_R, 4),
            "net_R": round(net_R, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        },
        "by_symbol": by_symbol,
        "by_timeframe": by_timeframe,
        "by_tier": by_tier,
        "by_hour_utc": by_hour_utc,
        "by_direction": dir_stats,
        "by_leverage": by_leverage,
        "score_analysis": score_analysis,
        "time_to_sl_minutes": time_to_sl,
        "loss_details": loss_details,
    }


@app.post("/api/kill-switch/reset")
async def kill_switch_reset():
    """
    Força recálculo do kill-switch (lê PnL atualizado do DB).
    Útil depois de corrigir registros de PnL — o switch é stateless e recomputa
    no próximo check_can_trade, mas esse endpoint dá feedback imediato.
    """
    from services import kill_switch_service
    res = await kill_switch_service.check_can_trade()
    return {"ok": True, "now_allowed": res.get("allowed"), "details": res}


@app.get("/api/exchange/diagnostic-binance")
async def exchange_diagnostic_binance():
    """Diagnóstico verboso do Binance Futures (testnet ou mainnet).
    Mostra length/SHA1 de key+secret (sem vazar), clock drift e tenta
    chamadas signed pra revelar erro exato de auth."""
    from services import binance_signed_service
    if hasattr(binance_signed_service, "diagnostic"):
        return await binance_signed_service.diagnostic()
    return {"error": "diagnostic não suportado pelo cliente Binance"}


@app.get("/api/exchange/account")
async def exchange_account():
    from services import exchange_service
    res = await exchange_service.get_wallet_balance()
    if not res.get("ok"):
        raise HTTPException(502, f"Exchange: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/exchange/positions")
async def exchange_positions(symbol: str | None = None):
    from services import exchange_service
    res = await exchange_service.get_positions(symbol=symbol)
    if not res.get("ok"):
        raise HTTPException(502, f"Exchange: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/exchange/orders")
async def exchange_orders(symbol: str | None = None, limit: int = 50):
    from services import exchange_service
    res = await exchange_service.get_order_history(symbol=symbol, limit=limit)
    if not res.get("ok"):
        raise HTTPException(502, f"Exchange: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/exchange/executions")
async def exchange_executions(symbol: str | None = None, limit: int = 50):
    from services import exchange_service
    res = await exchange_service.get_executions(symbol=symbol, limit=limit)
    if not res.get("ok"):
        raise HTTPException(502, f"Exchange: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/exchange/open-algo-orders")
async def exchange_open_algo_orders(symbol: str | None = None):
    """Ordens condicionais (SL/TP) ABERTAS na corretora — pra verificar que a
    proteção das posições está realmente viva (não só registrada no DB)."""
    from services import exchange_service
    res = await exchange_service.get_open_algo_orders(symbol=symbol)
    if not res.get("ok"):
        raise HTTPException(502, f"Exchange: {res.get('error') or res.get('msg')}")
    return res


# Aliases por corretora — força o cliente específico independente do EXCHANGE


@app.get("/api/bybit/env")
async def bybit_env():
    from services import bybit_signed_service
    return bybit_signed_service.env_info()


@app.get("/api/bybit/account")
async def bybit_account():
    from services import bybit_signed_service
    res = await bybit_signed_service.get_wallet_balance()
    if not res.get("ok"):
        raise HTTPException(502, f"Bybit: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/binance/env")
async def binance_env():
    from services import binance_signed_service
    return binance_signed_service.env_info()


@app.get("/api/binance/account")
async def binance_account():
    from services import binance_signed_service
    res = await binance_signed_service.get_wallet_balance()
    if not res.get("ok"):
        raise HTTPException(502, f"Binance: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/binance/positions")
async def binance_positions(symbol: str | None = None):
    from services import binance_signed_service
    res = await binance_signed_service.get_positions(symbol=symbol)
    if not res.get("ok"):
        raise HTTPException(502, f"Binance: {res.get('error') or res.get('msg')}")
    return res


@app.get("/api/risk/events")
async def risk_events(days: int = 30, limit: int = 200):
    """
    Histórico de eventos do circuit breaker (pausas/retomadas automáticas
    e manuais) dos últimos `days` dias. Usado pelo painel Status.
    """
    from services import risk_service
    items = await risk_service.list_events(days=days, limit=limit)
    return {"events": items, "count": len(items), "days": days}


@app.post("/api/debug/push-broadcast-test")
async def debug_push_broadcast_test():
    """
    Envia um push de TESTE pra todas subscriptions ativas — não depende
    de scan/mercado. Usado pra confirmar que push funciona com app fechado.
    """
    from services.push_service import PUSH_ENABLED, _send_one
    from db import DB_ENABLED, get_session
    from models.push_subscription import PushSubscription
    from sqlalchemy import select
    if not PUSH_ENABLED or not DB_ENABLED:
        return {"enabled": False}
    async with get_session() as session:
        stmt = select(PushSubscription).where(PushSubscription.active.is_(True))
        subs = (await session.execute(stmt)).scalars().all()
    payload = {
        "title": "🧪 Teste de push",
        "body": "Se você vê isto com o app/painel fechado, push está OK.",
        "tag": "test-broadcast",
        "data": {"url": "/", "event": "test"},
    }
    sent = 0
    errors = []
    for sub in subs:
        try:
            ok = await _send_one(sub, payload)
            if ok:
                sent += 1
        except Exception as e:
            errors.append(str(e)[:120])
    return {
        "total_subscriptions": len(subs),
        "sent": sent,
        "errors": errors,
    }


@app.post("/api/debug/backfill-notify-b")
async def debug_backfill_notify_b():
    """
    One-shot: marca notify_b=True em todas subscriptions existentes que
    estavam com False (default antigo). Após este endpoint rodar, todos
    subscribers ativos passam a receber push de tier B (era 0% antes).

    Idempotente: rodar de novo não muda nada (só conta 0 affected).
    """
    from db import DB_ENABLED, get_session
    from models.push_subscription import PushSubscription
    from sqlalchemy import update, select, func
    if not DB_ENABLED:
        return {"enabled": False}
    async with get_session() as session:
        # Conta antes
        total_q = await session.execute(select(func.count(PushSubscription.id)))
        total = total_q.scalar() or 0
        off_q = await session.execute(
            select(func.count(PushSubscription.id)).where(PushSubscription.notify_b == False)
        )
        off_before = off_q.scalar() or 0

        # UPDATE
        result = await session.execute(
            update(PushSubscription)
            .where(PushSubscription.notify_b == False)
            .values(notify_b=True)
        )
        await session.commit()

        return {
            "total_subscriptions": total,
            "notify_b_off_before": off_before,
            "updated_rows": result.rowcount,
        }


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
    # Diagnóstico: qual fonte está ativa?
    from services import binance_futures_service as _bfs
    _source = "binance-futures-proxy" if _bfs.PROXY_ENABLED else "binance-vision-spot"
    _proxy_url = _bfs.PROXY_URL if _bfs.PROXY_ENABLED else None
    _symbols_probe: list = []
    try:
        if _bfs.PROXY_ENABLED:
            _symbols_probe = await _bfs.fetch_top_volume_symbols(limit=5)
        else:
            from services import binance_vision_service as _bvs
            _symbols_probe = await _bvs.fetch_top_volume_symbols(limit=5)
    except Exception as e:
        _symbols_probe = [f"probe_error: {e}"]

    # Probe extra: klines individual via Worker (endpoint que NÃO sofre geo-block bulk)
    _klines_probe: str = "skipped"
    if _bfs.PROXY_ENABLED:
        try:
            _df = await _bfs.fetch_ohlcv("BTC/USDT:USDT", "1h", 5)
            _klines_probe = f"ok rows={len(_df)} last_close={float(_df['close'].iloc[-1])}" if not _df.empty else "empty"
        except Exception as e:
            _klines_probe = f"error: {e}"

    # Probe extra: Vision spot lista (fallback do híbrido)
    _vision_list_probe: list = []
    try:
        from services import binance_vision_service as _bvs2
        _vision_list_probe = await _bvs2.fetch_top_volume_symbols(limit=5)
    except Exception as e:
        _vision_list_probe = [f"vision_error: {e}"]

    # Probe Bybit: testa se o Railway consegue falar com api.bybit.com
    # (lista + 1 candle de BTC). Se passar, Bybit pode virar default do server-scan.
    _bybit_list_probe: list = []
    _bybit_klines_probe: str = "skipped"
    try:
        from services import bybit_service as _bys
        _bybit_list_probe = await _bys.fetch_top_volume_symbols(limit=5)
        try:
            _bdf = await _bys.fetch_ohlcv("BTC/USDT:USDT", "1h", 5)
            _bybit_klines_probe = (
                f"ok rows={len(_bdf)} last_close={float(_bdf['close'].iloc[-1])}"
                if not _bdf.empty else "empty"
            )
        except Exception as ke:
            _bybit_klines_probe = f"error: {ke}"
    except Exception as e:
        _bybit_list_probe = [f"bybit_error: {e}"]

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

    pushable = [r for r in recs_dict if r.get("tier") in ("A+", "A", "B")]

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
        "source": _source,
        "proxy_url": _proxy_url,
        "symbols_probe": _symbols_probe,
        "klines_probe": _klines_probe,
        "vision_list_probe": _vision_list_probe,
        "bybit_list_probe": _bybit_list_probe,
        "bybit_klines_probe": _bybit_klines_probe,
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


@app.get("/api/admin/feature-importance")
async def admin_feature_importance(hours: int = 168):
    """
    Feature importance via win-rate por feature ativa.

    Itera RecommendationSnapshot resolvidos (status != open) na janela.
    Para cada feature do JSON `features`, agrega wins/losses quando a
    feature está "ativa" (não-null/não-zero/lista não-vazia). Lift =
    win_rate da feature - baseline.

    Numéricos: ativos quando != None e != 0. Listas (ex: patterns):
    cada item vira sua própria feature "patterns:<tipo>". Strings:
    feature "<nome>:<valor>".
    """
    from datetime import timedelta
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.recommendation_snapshot import RecommendationSnapshot

    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    hours = max(1, min(int(hours), 24 * 60))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status != "open",
            RecommendationSnapshot.outcome_at >= cutoff,
        )
        snaps = (await session.execute(stmt)).scalars().all()

    if not snaps:
        return {
            "ok": True,
            "window_hours": hours,
            "total_resolved": 0,
            "baseline_win_rate": 0.0,
            "features": [],
        }

    def is_win(s) -> bool:
        if s.realized_r is not None:
            return s.realized_r > 0
        return s.status in ("won_tp1", "won_tp1_be", "won_tp2")

    total = len(snaps)
    base_wins = sum(1 for s in snaps if is_win(s))
    baseline = (base_wins / total * 100) if total else 0.0

    # Verifica se existe campo de features (sanity check)
    sample = next((s for s in snaps if s.features), None)
    if sample is None:
        return {
            "ok": False,
            "error": "no feature column found in snapshot",
            "fields_inspected": ["features"],
        }

    # Agrega por feature
    agg: dict[str, dict[str, int]] = {}

    def _bump(key: str, win: bool):
        d = agg.setdefault(key, {"active": 0, "wins": 0, "losses": 0})
        d["active"] += 1
        if win:
            d["wins"] += 1
        else:
            d["losses"] += 1

    for s in snaps:
        feats = s.features or {}
        if not isinstance(feats, dict):
            continue
        win = is_win(s)
        for k, v in feats.items():
            if v is None:
                continue
            if isinstance(v, bool):
                if v:
                    _bump(k, win)
            elif isinstance(v, (int, float)):
                if v != 0:
                    _bump(k, win)
            elif isinstance(v, str):
                if v:
                    _bump(f"{k}:{v}", win)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, (str, int, float)) and item:
                        _bump(f"{k}:{item}", win)
                    elif isinstance(item, dict):
                        t = item.get("type") or item.get("name")
                        if t:
                            _bump(f"{k}:{t}", win)

    features_out = []
    for key, d in agg.items():
        active = d["active"]
        wins = d["wins"]
        losses = d["losses"]
        wr = (wins / active * 100) if active else 0.0
        features_out.append({
            "feature": key,
            "active_count": active,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 2),
            "lift_vs_baseline_pct": round(wr - baseline, 2),
        })
    features_out.sort(key=lambda x: x["lift_vs_baseline_pct"], reverse=True)

    return {
        "ok": True,
        "window_hours": hours,
        "total_resolved": total,
        "baseline_win_rate": round(baseline, 2),
        "features": features_out,
    }


# ─── Bucketing para feature-importance v2 ─────────────────────────────────────
NUMERIC_BUCKETS = {
    "funding_pct": [
        (-999.0, -0.05, "<-0.05"),
        (-0.05, 0.0, "-0.05_to_0"),
        (0.0, 0.05, "0_to_0.05"),
        (0.05, 999.0, ">0.05"),
    ],
    "rsi": [
        (0.0, 30.0, "<30"),
        (30.0, 50.0, "30-50"),
        (50.0, 70.0, "50-70"),
        (70.0, 100.0, ">70"),
    ],
    "adx": [
        (0.0, 20.0, "<20"),
        (20.0, 30.0, "20-30"),
        (30.0, 100.0, ">30"),
    ],
    "atr_pct": [
        (0.0, 1.0, "<1"),
        (1.0, 3.0, "1-3"),
        (3.0, 999.0, ">3"),
    ],
    "mtf_score": [
        (0.0, 60.0, "<60"),
        (60.0, 75.0, "60-75"),
        (75.0, 999.0, ">75"),
    ],
    "confluence_pct": [
        (0.0, 50.0, "<50"),
        (50.0, 70.0, "50-70"),
        (70.0, 100.0, ">70"),
    ],
    "oi_change_pct": [
        (-999.0, -5.0, "<-5"),
        (-5.0, 0.0, "-5_to_0"),
        (0.0, 5.0, "0_to_5"),
        (5.0, 999.0, ">5"),
    ],
}

HOUR_SESSIONS = {
    "asia": range(0, 7),
    "eu": range(7, 14),
    "us": range(14, 22),
    "off": range(22, 24),
}

DAY_LABELS = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


def _bucket_for(name: str, value):
    """Retorna o label do bucket pra um valor numérico, ou None se fora."""
    if value is None:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    buckets = NUMERIC_BUCKETS.get(name)
    if not buckets:
        return None
    for lo, hi, label in buckets:
        # Intervalo half-open [lo, hi); último bucket inclui o limite superior.
        if lo <= v < hi:
            return label
    # Cobertura do extremo superior
    last = buckets[-1]
    if v >= last[1]:
        return last[2]
    return None


def _session_for_hour(h: int) -> str | None:
    try:
        h = int(h)
    except Exception:
        return None
    for label, rng in HOUR_SESSIONS.items():
        if h in rng:
            return label
    return None


@app.get("/api/admin/feature-importance-v2")
async def admin_feature_importance_v2(hours: int = 168):
    """
    feature-importance v2 com bucketing de numéricos.

    Diferenças vs v1:
      • Numéricos conhecidos (rsi/adx/atr_pct/mtf_score/confluence_pct/
        funding_pct/oi_change_pct) são discretizados em buckets fixos. Cada
        bucket vira sua própria feature key "<nome>:<label>".
      • hour_utc é mapeado em sessões (asia/eu/us/off) → "hour_utc:<sess>".
      • day_of_week → "dow:<mon|tue|…>".
      • Booleans, strings (sentiment) e listas (patterns) seguem o v1.
      • Filtra buckets com active_count < 5 e reporta o count em
        excluded_low_sample_count.
    """
    from datetime import timedelta
    from sqlalchemy import select
    from db import get_session, DB_ENABLED
    from models.recommendation_snapshot import RecommendationSnapshot

    if not DB_ENABLED:
        return {"ok": False, "error": "DB desabilitado"}

    hours = max(1, min(int(hours), 24 * 60))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    logging.info(f"[feature-importance-v2] window={hours}h cutoff={cutoff.isoformat()}")

    async with get_session() as session:
        stmt = select(RecommendationSnapshot).where(
            RecommendationSnapshot.status != "open",
            RecommendationSnapshot.outcome_at >= cutoff,
        )
        snaps = (await session.execute(stmt)).scalars().all()

    if not snaps:
        return {
            "ok": True,
            "window_hours": hours,
            "total_resolved": 0,
            "baseline_win_rate": 0.0,
            "features": [],
            "excluded_low_sample_count": 0,
        }

    def is_win(s) -> bool:
        if s.realized_r is not None:
            return s.realized_r > 0
        return s.status in ("won_tp1", "won_tp1_be", "won_tp2")

    total = len(snaps)
    base_wins = sum(1 for s in snaps if is_win(s))
    baseline = (base_wins / total * 100) if total else 0.0

    sample = next((s for s in snaps if s.features), None)
    if sample is None:
        return {
            "ok": False,
            "error": "no feature column found in snapshot",
            "fields_inspected": ["features"],
        }

    agg: dict[str, dict[str, int]] = {}

    def _bump(key: str, win: bool):
        d = agg.setdefault(key, {"active": 0, "wins": 0, "losses": 0})
        d["active"] += 1
        if win:
            d["wins"] += 1
        else:
            d["losses"] += 1

    for s in snaps:
        feats = s.features or {}
        if not isinstance(feats, dict):
            continue
        win = is_win(s)
        for k, v in feats.items():
            if v is None:
                continue
            # Numéricos com bucketing
            if k in NUMERIC_BUCKETS:
                label = _bucket_for(k, v)
                if label is not None:
                    _bump(f"{k}:{label}", win)
                continue
            # hour_utc → sessão
            if k == "hour_utc":
                sess = _session_for_hour(v)
                if sess is not None:
                    _bump(f"hour_utc:{sess}", win)
                continue
            # day_of_week → label
            if k == "day_of_week":
                try:
                    lbl = DAY_LABELS.get(int(v))
                except Exception:
                    lbl = None
                if lbl is not None:
                    _bump(f"dow:{lbl}", win)
                continue
            # Booleans
            if isinstance(v, bool):
                if v:
                    _bump(k, win)
            elif isinstance(v, (int, float)):
                # Numérico não-buckable → fallback v1 (boolean: != 0)
                if v != 0:
                    _bump(k, win)
            elif isinstance(v, str):
                if v:
                    _bump(f"{k}:{v}", win)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, (str, int, float)) and item:
                        _bump(f"{k}:{item}", win)
                    elif isinstance(item, dict):
                        t = item.get("type") or item.get("name")
                        if t:
                            _bump(f"{k}:{t}", win)

    MIN_SAMPLES = 5
    excluded = 0
    features_out = []
    for key, d in agg.items():
        active = d["active"]
        if active < MIN_SAMPLES:
            excluded += 1
            continue
        wins = d["wins"]
        losses = d["losses"]
        wr = (wins / active * 100) if active else 0.0
        features_out.append({
            "feature": key,
            "active_count": active,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 2),
            "lift_vs_baseline_pct": round(wr - baseline, 2),
        })
    features_out.sort(key=lambda x: x["lift_vs_baseline_pct"], reverse=True)

    logging.info(
        f"[feature-importance-v2] resolved={total} baseline={baseline:.2f}% "
        f"features={len(features_out)} excluded_low_sample={excluded}"
    )

    return {
        "ok": True,
        "window_hours": hours,
        "total_resolved": total,
        "baseline_win_rate": round(baseline, 2),
        "features": features_out,
        "excluded_low_sample_count": excluded,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
