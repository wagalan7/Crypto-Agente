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
    from datetime import datetime, timedelta, timezone
    from services import backtest_universe_service as bus
    tfs = [t.strip() for t in os.getenv("SWEEP_WORKER_TFS", "1h,4h").split(",") if t.strip()]
    limit = _env_int("SWEEP_WORKER_LIMIT", 500)
    refresh_days = _env_int("SWEEP_WORKER_REFRESH_DAYS", 7)
    step_bars = _env_int("SWEEP_WORKER_STEP_BARS", 1)
    order_by = os.getenv("SWEEP_WORKER_ORDER_BY", "history").strip() or "history"
    # Janela de histórico do sweep AMPLO. Full-history (2017) em 1h dá 45k+ barras
    # por moeda → o scan barra-a-barra não termina num worker de 1 vCPU e o
    # container reinicia antes (nunca passa da 1ª moeda pesada). Pré-filtro amplo
    # não precisa de 5 anos: janela curta corta barras E iterações. 0 = ilimitado
    # (full-history, comportamento antigo). Default 730d (~2 anos).
    max_hist_days = _env_int("SWEEP_WORKER_MAX_HISTORY_DAYS", 730)
    history_start = None
    if max_hist_days > 0:
        history_start = datetime.now(timezone.utc) - timedelta(days=max_hist_days)
    # Quando o REFINO da allowlist está ligado, o sweep amplo EXCLUI a allowlist
    # (modo "outside") pra não pisar no mesmo (symbol,tf) que o refino step_bars=1
    # grava (a tabela é única por symbol+tf, não distingue granularidade → sem
    # exclusão os dois ficariam sobrescrevendo um ao outro em ping-pong). Default
    # OFF: o amplo segue rodando o universo inteiro como hoje.
    exclude = bus.PRD_ALLOWLIST_BASES if _env_bool("SWEEP_WORKER_EXCLUDE_ALLOWLIST", False) else None
    log.info(f"[worker] iniciando sweep AMPLO tfs={tfs} limit={limit} "
             f"refresh_days={refresh_days} step_bars={step_bars} order_by={order_by} "
             f"max_hist_days={max_hist_days} exclude_allowlist={exclude is not None} "
             f"data_source={os.getenv('BACKTEST_DATA_SOURCE')}")
    return await bus.run_universe_backtest(
        tfs, limit=limit, refresh_days=refresh_days, step_bars=step_bars,
        order_by=order_by, history_start=history_start, exclude_bases=exclude,
    )


async def _run_refino_once() -> dict:
    """Refino step_bars=1 SÓ da allowlist (#5). Afina os níveis das moedas que o
    bot REALMENTE opera, com granularidade fina e histórico mais longo que o
    amplo. Roda ANTES do sweep amplo no ciclo (prioridade às moedas que importam).
    DESLIGADO por default (SWEEP_WORKER_REFINO_ENABLED=false) → subir o código não
    muda nada. Pra evitar ping-pong de granularidade, ligue JUNTO com
    SWEEP_WORKER_EXCLUDE_ALLOWLIST=true (aí o amplo não toca a allowlist)."""
    from datetime import datetime, timedelta, timezone
    from services import backtest_universe_service as bus
    # 4h é o TF de promoção (BT_SEED_TF) e ~1/4 das barras do 1h → mais leve no
    # worker de 1 vCPU. Quem quiser refinar 1h também: SWEEP_WORKER_REFINO_TFS=1h,4h.
    tfs = [t.strip() for t in os.getenv("SWEEP_WORKER_REFINO_TFS", "4h").split(",") if t.strip()]
    limit = _env_int("SWEEP_WORKER_LIMIT", 500)
    refresh_days = _env_int("SWEEP_WORKER_REFINO_REFRESH_DAYS", 7)
    step_bars = _env_int("SWEEP_WORKER_REFINO_STEP_BARS", 1)
    # Refino confia mais nas moedas da allowlist → janela maior que o amplo (1095d
    # ~3 anos), mas ainda bounded p/ não reabrir o hang do 1h gigante. 0 = full.
    max_hist_days = _env_int("SWEEP_WORKER_REFINO_MAX_HISTORY_DAYS", 1095)
    history_start = None
    if max_hist_days > 0:
        history_start = datetime.now(timezone.utc) - timedelta(days=max_hist_days)
    log.info(f"[worker] iniciando REFINO allowlist tfs={tfs} step_bars={step_bars} "
             f"refresh_days={refresh_days} max_hist_days={max_hist_days} "
             f"bases={len(bus.PRD_ALLOWLIST_BASES)}")
    return await bus.run_universe_backtest(
        tfs, limit=limit, refresh_days=refresh_days, step_bars=step_bars,
        order_by="history", history_start=history_start,
        include_only_bases=bus.PRD_ALLOWLIST_BASES,
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
    refino_on = _env_bool("SWEEP_WORKER_REFINO_ENABLED", False)
    while True:
        # Fase 1 (opcional): REFINO step_bars=1 da allowlist — prioridade às moedas
        # que o bot opera. Roda ANTES do amplo. Default OFF.
        if refino_on:
            try:
                rref = await _run_refino_once()
                pr = (rref or {}).get("progress", {})
                log.info(f"[worker] refino allowlist concluído: {pr.get('computed')} "
                         f"computados, {pr.get('skipped')} pulados, {pr.get('errors')} erros.")
            except Exception as e:
                import traceback
                log.error(f"[worker] refino crashou: {e}\n{traceback.format_exc()}")

        # Fase 2: sweep AMPLO (universo; exclui allowlist se EXCLUDE_ALLOWLIST=on).
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
