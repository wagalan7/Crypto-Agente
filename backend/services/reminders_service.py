"""Lembretes de vencimento (renovações de serviços pagos) via Telegram.

Roda 1x/dia (loop asyncio iniciado no lifespan). As datas ficam em RENEWALS —
é só editar essa lista pra adicionar/mudar/remover um vencimento (e dar deploy).

A lógica de "N dias antes" compara `(hoje + N).day` com o dia do vencimento,
então funciona em qualquer mês (28-31 dias) sem ajuste manual: pra um
vencimento todo dia 01 com aviso de 5 dias, dispara dia 26 (ou 27 em meses de
31 dias) — sempre exatamente 5 dias antes da próxima ocorrência.

Sem credenciais de Telegram = no-op (o loop nem inicia). A checagem roda uma vez
por dia num horário fixo; se o serviço reiniciar (redeploy) depois do horário do
dia, o próximo disparo é só no dia seguinte — evita lembrete duplicado.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from services.notification_service import send_telegram, TELEGRAM_ENABLED

log = logging.getLogger(__name__)

# Horário (UTC) da checagem diária. 13:00 UTC = 10:00 BRT.
CHECK_HOUR_UTC = 13

# ── Vencimentos cadastrados ────────────────────────────────────────────────
# due_day            = dia do mês em que vence
# cycle              = só rótulo informativo ("mensal" | "anual")
# remind_days_before = quantos dias antes avisar (0 = no próprio dia)
RENEWALS = [
    {
        "name": "VPS / Proxy (Binance)",
        "provider": "Digital Clean",
        "due_day": 1,
        "cycle": "mensal",
        "remind_days_before": 5,
        "note": "Proxy 168.144.132.241 — se cair, o bot perde acesso à Binance. Renovar antes do dia 01.",
    },
    {
        "name": "Revisão de fatura",
        "provider": "Railway + Anthropic",
        "due_day": 1,
        "cycle": "mensal",
        "remind_days_before": 0,
        "note": "Conferir uso/custo do Railway (backend + Postgres) e da Anthropic (fallback de IA).",
    },
]


def _due_today(r: dict, today) -> bool:
    """True se HOJE é o dia de avisar este vencimento (today + N cai no due_day)."""
    target = today + timedelta(days=int(r.get("remind_days_before", 0)))
    return target.day == int(r["due_day"])


def _format(r: dict) -> str:
    n = int(r.get("remind_days_before", 0))
    when = "HOJE" if n == 0 else f"em {n} dia(s)"
    return (
        "\U0001F4C5 *Lembrete de vencimento*\n"
        f"*{r['name']}* — {r.get('provider', '')}\n"
        f"Vence *{when}* (dia {int(r['due_day']):02d}, {r.get('cycle', 'mensal')}).\n"
        f"{r.get('note', '')}"
    )


def _seconds_until_next_check() -> float:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=CHECK_HOUR_UTC, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return (nxt - now).total_seconds()


async def loop():
    """Loop diário de lembretes. Cancelável (asyncio.CancelledError no shutdown)."""
    if not TELEGRAM_ENABLED:
        log.info("[reminders] Telegram desativado — loop de lembretes não inicia.")
        return
    log.info(
        f"[reminders] iniciado — checagem diária {CHECK_HOUR_UTC:02d}:00 UTC, "
        f"{len(RENEWALS)} vencimento(s) cadastrado(s)."
    )
    while True:
        try:
            await asyncio.sleep(_seconds_until_next_check())
            today = datetime.now(timezone.utc).date()
            for r in RENEWALS:
                try:
                    if _due_today(r, today):
                        ok = await send_telegram(_format(r), event_type="renewal")
                        log.info(f"[reminders] '{r['name']}' disparado (enviado={ok}).")
                except Exception as e:
                    log.warning(f"[reminders] falha em '{r.get('name')}': {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"[reminders] erro no loop: {e}")
            await asyncio.sleep(3600)  # backoff antes de tentar de novo
