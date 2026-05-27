"""
Push Service — Web Push (W3C Push API) com VAPID.

Fluxo:
1. Frontend pede permissão e gera subscription via PushManager.
2. POST /api/push/subscribe envia subscription pro backend → salva no DB.
3. Quando aparece A+ nova, backend chama notify_new_recommendation()
   que envia push pra cada subscription ativa, usando filtro de tier.
4. Subscriptions expiradas (HTTP 410) são desativadas.

Tudo gracefully degrada se VAPID_* não estiverem definidas.
"""
from __future__ import annotations
import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import select, update

from db import DB_ENABLED, get_session
from models.push_subscription import PushSubscription

log = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@crypto-agente.app")
PUSH_ENABLED = bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY and DB_ENABLED)

if PUSH_ENABLED:
    try:
        from pywebpush import webpush, WebPushException  # type: ignore
        log.info("Push notifications habilitadas (VAPID configurado).")
    except ImportError:
        PUSH_ENABLED = False
        log.warning("pywebpush não instalado — push desabilitado.")
else:
    log.info("Push desabilitado (VAPID_* não configuradas).")


def get_public_key() -> Optional[str]:
    return VAPID_PUBLIC_KEY or None


async def save_subscription(
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
    filters: Optional[Dict[str, bool]] = None,
) -> bool:
    if not DB_ENABLED:
        return False
    filters = filters or {}
    async with get_session() as session:
        # Upsert: se endpoint já existe, reativa e atualiza filtros
        stmt = select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.p256dh = p256dh
            existing.auth = auth
            existing.user_agent = user_agent
            existing.active = True
            existing.fail_count = 0
            if "notify_a_plus" in filters:
                existing.notify_a_plus = bool(filters["notify_a_plus"])
            if "notify_a" in filters:
                existing.notify_a = bool(filters["notify_a"])
            if "notify_b" in filters:
                existing.notify_b = bool(filters["notify_b"])
        else:
            sub = PushSubscription(
                endpoint=endpoint, p256dh=p256dh, auth=auth,
                user_agent=user_agent,
                notify_a_plus=bool(filters.get("notify_a_plus", True)),
                notify_a=bool(filters.get("notify_a", True)),
                notify_b=bool(filters.get("notify_b", True)),
                active=True,
            )
            session.add(sub)
        await session.commit()
    return True


async def remove_subscription(endpoint: str) -> bool:
    if not DB_ENABLED:
        return False
    async with get_session() as session:
        await session.execute(
            update(PushSubscription).where(PushSubscription.endpoint == endpoint)
            .values(active=False)
        )
        await session.commit()
    return True


async def _send_one(sub: PushSubscription, payload: Dict[str, Any]) -> bool:
    """Envia para 1 subscription. Retorna False se subscription expirou (410)."""
    if not PUSH_ENABLED:
        return False
    try:
        # pywebpush é síncrono — roda no executor pra não bloquear o loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _sync_push, sub, payload)
        return True
    except Exception as e:
        msg = str(e)
        # 410 Gone = subscription cancelada pelo usuário → desativar
        if "410" in msg or "404" in msg:
            return False
        log.warning(f"Push falhou para {sub.endpoint[:50]}: {e}")
        raise


def _sync_push(sub: PushSubscription, payload: Dict[str, Any]):
    from pywebpush import webpush  # type: ignore
    webpush(
        subscription_info={
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        },
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_SUBJECT},
        ttl=3600,  # 1 hora
    )


async def notify_new_recommendation(rec: Dict[str, Any]) -> int:
    """
    Dispara push para todos os subscribers que aceitam o tier desta rec.
    Retorna quantos envios bem-sucedidos.
    """
    if not PUSH_ENABLED:
        return 0

    tier = rec.get("tier", "")
    if tier not in ("A+", "A", "B"):
        return 0

    # Filtro: quais subs querem esse tier?
    field_map = {"A+": "notify_a_plus", "A": "notify_a", "B": "notify_b"}
    filter_field = field_map[tier]

    async with get_session() as session:
        stmt = select(PushSubscription).where(
            (PushSubscription.active.is_(True))
            & (getattr(PushSubscription, filter_field).is_(True))
        )
        subs = (await session.execute(stmt)).scalars().all()

    if not subs:
        return 0

    symbol_short = rec.get("symbol", "").split("/")[0]
    direction = rec.get("direction", "").upper()
    leverage = rec.get("leverage", 1)
    score = rec.get("score", 0)
    rr = rec.get("risk_reward", 0)

    title = f"🚀 {tier} · {symbol_short} {direction}"
    body = (
        f"{rec.get('timeframe', '')} · {leverage}x · R:R 1:{rr}\n"
        f"Score {score:.0f} · entry {_fmt(rec.get('entry', 0))}"
    )

    payload = {
        "title": title,
        "body": body,
        "tag": f"rec-{symbol_short}-{rec.get('timeframe')}",  # deduplica notifs
        "data": {
            "symbol": rec.get("symbol"),
            "timeframe": rec.get("timeframe"),
            "tier": tier,
            "url": "/",
        },
    }

    sent = 0
    to_deactivate: List[int] = []
    for sub in subs:
        try:
            ok = await _send_one(sub, payload)
            if ok:
                sent += 1
            else:
                to_deactivate.append(sub.id)
        except Exception:
            # Erro transitório — incrementa fail_count
            async with get_session() as s2:
                await s2.execute(
                    update(PushSubscription).where(PushSubscription.id == sub.id)
                    .values(fail_count=sub.fail_count + 1)
                )
                await s2.commit()

    # Desativa subscriptions mortas
    if to_deactivate:
        async with get_session() as session:
            await session.execute(
                update(PushSubscription).where(PushSubscription.id.in_(to_deactivate))
                .values(active=False)
            )
            await session.commit()

    if sent:
        log.info(f"Push enviado pra {sent} device(s) — {tier} {symbol_short}")
    return sent


async def notify_recommendations_batch(recs: List[Dict[str, Any]], newly_saved: int) -> int:
    """
    Dispara push só para recs marcadas com `_just_saved=True` por
    save_recommendations (flag setada no próprio dict da rec quando o
    snapshot é efetivamente inserido, vs duplicata que já existia).

    Isso garante que push notifications NÃO se repitam para o mesmo setup
    que ainda está dentro da janela de dedup (2h). Limita a 5 alerts/batch
    pra não floodar.
    """
    if not PUSH_ENABLED or not recs or newly_saved == 0:
        return 0

    # Filtra só recs que foram REALMENTE inseridas nesta chamada.
    # Fallback: se a flag não existir (caller antigo), assume top-N por score.
    just_saved = [r for r in recs if r.get("_just_saved") is True]
    if not just_saved:
        # Fallback de compatibilidade (callers antigos sem a flag)
        log.warning("notify_recommendations_batch: nenhuma rec marcada _just_saved — fallback top-score")
        just_saved = sorted(recs, key=lambda r: r.get("score", 0), reverse=True)[:newly_saved]

    candidates = just_saved[:5]   # cap em 5 push/batch

    total_sent = 0
    for rec in candidates:
        try:
            total_sent += await notify_new_recommendation(rec)
        except Exception as e:
            log.warning(f"notify_new_recommendation falhou: {e}")
    return total_sent


def _fmt(n: float) -> str:
    if n >= 1000:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:.4f}"
    return f"{n:.6f}"
