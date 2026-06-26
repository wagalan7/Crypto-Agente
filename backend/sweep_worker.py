"""
Sweep Worker (Opção B) — entrypoint do BACKTEST MASSIVO em SERVIÇO SEPARADO.

PROBLEMA que resolve: rodar o sweep dentro do serviço web (mesmo processo do
trading AO VIVO) estrangula o event loop num dyno single-core (GIL) → o scan
loop atrasa > limite do watchdog → "Scan loop parado" e ENTRADAS atrasadas.

SOLUÇÃO: este processo roda SOZINHO o sweep, num serviço Railway separado, com
CPU/memória próprias. Zero contenção com o trading. Grava no MESMO Postgres de
PRD (mesma DATABASE_URL) → o conhecimento cai direto nas tabelas que o bot ao
vivo lê (symbol_backtest_stats, backtest_trades). NÃO há etapa DEV→PRD: o worker
JÁ está em PRD escrevendo no banco de PRD.

NÃO sobe servidor web nem os loops de trading — só init_db() + o sweep.

Start command no Railway (override no painel do serviço worker):
    python sweep_worker.py

Envs (todas opcionais, com defaults seguros):
    DATABASE_URL                 — MESMA referência do Postgres de PRD (${{Postgres.DATABASE_URL}})
    BACKTEST_DATA_SOURCE         — default forçado p/ vision_bulk (estático, sem ban)
    SWEEP_WORKER_ENABLED=true    — trava de segurança (false = sai sem rodar nada)
    SWEEP_WORKER_TFS=1h,4h
    SWEEP_WORKER_LIMIT=500
    SWEEP_WORKER_REFRESH_DAYS=7
    SWEEP_WORKER_STEP_BARS=1
    SWEEP_WORKER_ORDER_BY=history
    SWEEP_WORKER_LOOP_HOURS=24   — após concluir, espera N h e re-roda (pega novas
                                   listagens / refresca stale). 0 = roda 1x e ocioso.
"""
from __future__ import annotations
import os
import asyncio
import logging

# Defaults seguros do CONTEXTO worker — setdefault: env explícito do painel vence.
#  - vision_bulk: arquivos estáticos, ZERO rate-limit → NUNCA bane o IP (lição do lote 1)
#  - yield do GIL desligado: não há loop ao vivo pra proteger aqui → roda full speed
os.environ.setdefault("BACKTEST_DATA_SOURCE", "vision_bulk")
os.environ.setdefault("BT_UNIVERSE_YIELD_SLEEP_S", "0")
os.environ.setdefault("BACKTEST_UNIVERSE_ENABLED", "on")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("sweep_worker")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


async def _run_once() -> dict:
    from services import backtest_universe_service as bus
    tfs = [t.strip() for t in os.getenv("SWEEP_WORKER_TFS", "1h,4h").split(",") if t.strip()]
    limit = _env_int("SWEEP_WORKER_LIMIT", 500)
    refresh_days = _env_int("SWEEP_WORKER_REFRESH_DAYS", 7)
    step_bars = _env_int("SWEEP_WORKER_STEP_BARS", 1)
    order_by = os.getenv("SWEEP_WORKER_ORDER_BY", "history").strip() or "history"
    log.info(f"[worker] iniciando sweep tfs={tfs} limit={limit} "
             f"refresh_days={refresh_days} step_bars={step_bars} order_by={order_by} "
             f"data_source={os.getenv('BACKTEST_DATA_SOURCE')}")
    return await bus.run_universe_backtest(
        tfs, limit=limit, refresh_days=refresh_days, step_bars=step_bars,
        order_by=order_by,
    )


async def main() -> None:
    if not _env_bool("SWEEP_WORKER_ENABLED", True):
        log.warning("[worker] SWEEP_WORKER_ENABLED=false — saindo sem rodar nada.")
        return

    from db import DB_ENABLED, init_db
    if not DB_ENABLED:
        log.error("[worker] DATABASE_URL ausente — o worker PRECISA do mesmo "
                  "Postgres de PRD. Abortando.")
        return

    await init_db()
    log.info("[worker] DB ok. Iniciando ciclo do sweep.")

    loop_hours = _env_int("SWEEP_WORKER_LOOP_HOURS", 24)
    while True:
        try:
            res = await _run_once()
            prog = (res or {}).get("progress", {})
            log.info(f"[worker] sweep concluído: {prog.get('computed')} computados, "
                     f"{prog.get('skipped')} pulados, {prog.get('errors')} erros.")
        except Exception as e:
            import traceback
            log.error(f"[worker] sweep crashou: {e}\n{traceback.format_exc()}")

        if loop_hours <= 0:
            log.info("[worker] LOOP_HOURS=0 — rodada única concluída. Ocioso "
                     "(mantém o serviço vivo; reinicie/redeploy pra re-rodar).")
            # Ocioso pra não entrar em restart-loop do Railway (restartPolicy=always).
            while True:
                await asyncio.sleep(3600)
        else:
            wait_s = loop_hours * 3600
            log.info(f"[worker] próximo ciclo em {loop_hours}h (refresca stale / "
                     f"pega novas listagens).")
            await asyncio.sleep(wait_s)


if __name__ == "__main__":
    asyncio.run(main())
