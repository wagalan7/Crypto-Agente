"""
News Filter — bloqueia novas recomendações em torno de eventos macro de alto
impacto (FOMC, CPI, NFP, etc.) que historicamente disparam volatilidade
extrema e estopam setups técnicos bons.

Fonte: Forex Factory community mirror (JSON, sem auth, atualizado semanalmente).
Filtramos por país (USD/EUR/GBP afetam crypto) + impacto (High).

Janela de blackout:
  - 30min ANTES do evento
  - 30min DEPOIS
  - FOMC press conference / Fed funds rate: 60min depois (mais volátil)

Toggle: NEWS_FILTER_ENABLED env var (default "1" = ativo).

API pública:
  await get_blackout_status() -> {
      "active": bool,
      "event": str | None,
      "country": str | None,
      "minutes_until_event": int | None,
      "minutes_until_resume": int | None,
      "impact": str | None,
  }
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import httpx

log = logging.getLogger(__name__)

FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Países cujas notícias afetam crypto (USD predominante, EUR/GBP secundário)
RELEVANT_COUNTRIES = {"USD", "EUR", "GBP"}

# Janela de blackout em minutos (antes, depois)
BLACKOUT_DEFAULT = (30, 30)
# Eventos super sensíveis: blackout maior (FOMC tem press conf de 1h)
EXTENDED_EVENTS_KEYWORDS = (
    "fomc",
    "federal funds",
    "fed chair",
    "rate decision",
    "interest rate decision",
    "press conference",
)
BLACKOUT_EXTENDED = (30, 60)

NEWS_FILTER_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "1").strip() not in (
    "0", "false", "False", "no", "off", "",
)

# Cache: refetch da agenda a cada 1h é suficiente (a lista é semanal)
_cache: Dict[str, Any] = {"ts": 0, "events": []}
CACHE_TTL = 3600


def _is_extended_event(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in EXTENDED_EVENTS_KEYWORDS)


async def _fetch_calendar() -> List[Dict[str, Any]]:
    """Busca a agenda. Retorna lista filtrada por país + impacto High."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(FOREX_FACTORY_URL)
            r.raise_for_status()
            raw = r.json()
    except Exception as e:
        log.warning(f"[news] fetch falhou: {e}")
        return []

    filtered: List[Dict[str, Any]] = []
    for ev in raw:
        impact = (ev.get("impact") or "").strip()
        country = (ev.get("country") or "").strip().upper()
        if impact != "High":
            continue
        if country not in RELEVANT_COUNTRIES:
            continue
        date_str = ev.get("date")
        if not date_str:
            continue
        try:
            # Forex Factory usa ISO com timezone (-04:00 = NY)
            event_dt = datetime.fromisoformat(date_str)
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        title = ev.get("title", "")
        before_min, after_min = (
            BLACKOUT_EXTENDED if _is_extended_event(title) else BLACKOUT_DEFAULT
        )
        filtered.append({
            "title": title,
            "country": country,
            "impact": impact,
            "event_dt": event_dt,
            "blackout_start": event_dt - timedelta(minutes=before_min),
            "blackout_end": event_dt + timedelta(minutes=after_min),
            "before_min": before_min,
            "after_min": after_min,
        })

    filtered.sort(key=lambda x: x["event_dt"])
    log.info(f"[news] {len(filtered)} eventos high-impact carregados")
    return filtered


async def _get_events() -> List[Dict[str, Any]]:
    now = time.time()
    if _cache["events"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["events"]
    events = await _fetch_calendar()
    if events:
        _cache["events"] = events
        _cache["ts"] = now
    elif _cache["events"]:
        # Falhou fetch mas tem cache antigo — usa stale (melhor que nada)
        log.info("[news] usando cache stale")
    return _cache["events"]


async def get_blackout_status() -> Dict[str, Any]:
    """
    Verifica se AGORA está dentro de uma janela de blackout.

    Se filter desabilitado, retorna {active: False}.
    Se sem eventos carregados, retorna {active: False} (fail-open: prefiro
    perder filtro a bloquear sistema todo por falha de rede).
    """
    if not NEWS_FILTER_ENABLED:
        return {"active": False, "reason": "disabled"}

    events = await _get_events()
    if not events:
        return {"active": False, "reason": "no_data"}

    now = datetime.now(timezone.utc)

    # Procura evento que tem now dentro de [blackout_start, blackout_end]
    for ev in events:
        if ev["blackout_start"] <= now <= ev["blackout_end"]:
            mins_until_event = int((ev["event_dt"] - now).total_seconds() / 60)
            mins_until_resume = int((ev["blackout_end"] - now).total_seconds() / 60) + 1
            return {
                "active": True,
                "event": ev["title"],
                "country": ev["country"],
                "impact": ev["impact"],
                "minutes_until_event": mins_until_event,
                "minutes_until_resume": mins_until_resume,
                "blackout_window_min": ev["before_min"] + ev["after_min"],
            }

    # Não está em blackout — informa o próximo evento (UI mostra contagem)
    next_ev = None
    for ev in events:
        if ev["event_dt"] > now:
            next_ev = ev
            break

    if next_ev:
        mins_until = int((next_ev["event_dt"] - now).total_seconds() / 60)
        return {
            "active": False,
            "next_event": next_ev["title"],
            "next_country": next_ev["country"],
            "next_impact": next_ev["impact"],
            "minutes_until_next": mins_until,
        }

    return {"active": False}


async def get_upcoming_events(hours: int = 24) -> List[Dict[str, Any]]:
    """Lista próximos eventos high-impact nas próximas N horas (pra UI)."""
    events = await _get_events()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    upcoming = []
    for ev in events:
        if now <= ev["event_dt"] <= cutoff:
            upcoming.append({
                "title": ev["title"],
                "country": ev["country"],
                "impact": ev["impact"],
                "event_iso": ev["event_dt"].isoformat(),
                "minutes_until": int((ev["event_dt"] - now).total_seconds() / 60),
            })
    return upcoming
