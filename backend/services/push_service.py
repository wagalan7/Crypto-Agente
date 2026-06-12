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

# Gate GLOBAL de tier para push (recs novas + outcomes).
# Por padrão NÃO envia push de tier B — só A e A+ (usuário pediu menos ruído).
# Sobrepõe o filtro por-subscription (notify_b): mesmo com notify_b=True no device,
# tier B não dispara enquanto este gate estiver desligado.
# Reversível: setar PUSH_TIER_B_ENABLED=true no Railway re-habilita push de B.
PUSH_TIER_B_ENABLED = os.getenv("PUSH_TIER_B_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# Máximo de push por batch de scan (anti-flood). Default 5 (comportamento antigo).
# Subir via env PUSH_BATCH_CAP (ex.: 25) quando se quer receber TODAS as recs
# do ciclo (bot opera + observação/wide) pra aprendizagem passiva no celular.
try:
    PUSH_BATCH_CAP = max(1, int(os.getenv("PUSH_BATCH_CAP", "5")))
except (TypeError, ValueError):
    PUSH_BATCH_CAP = 5

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


async def _fanout_push(subs: List[PushSubscription], payload: Dict[str, Any]) -> int:
    """
    Envia `payload` para todas as subscriptions EM PARALELO (asyncio.gather).
    Antes os envios eram sequenciais — com vários devices isso somava latência.

    Trata por subscription:
      - True  → sucesso (conta)
      - False → expirou (410/404) → desativa
      - Exception → erro transitório → incrementa fail_count

    Retorna quantos envios bem-sucedidos.
    """
    if not subs:
        return 0
    results = await asyncio.gather(
        *[_send_one(sub, payload) for sub in subs],
        return_exceptions=True,
    )
    sent = 0
    to_deactivate: List[int] = []
    fail_ids: List[int] = []
    for sub, res in zip(subs, results):
        if isinstance(res, Exception):
            fail_ids.append(sub.id)
        elif res is True:
            sent += 1
        else:  # False → subscription morta (410/404)
            to_deactivate.append(sub.id)

    if to_deactivate:
        async with get_session() as session:
            await session.execute(
                update(PushSubscription).where(PushSubscription.id.in_(to_deactivate))
                .values(active=False)
            )
            await session.commit()
    if fail_ids:
        async with get_session() as session:
            await session.execute(
                update(PushSubscription).where(PushSubscription.id.in_(fail_ids))
                .values(fail_count=PushSubscription.fail_count + 1)
            )
            await session.commit()
    return sent


def _sync_push(sub: PushSubscription, payload: Dict[str, Any]):
    """
    TTL: tempo máximo que o push provider (FCM/APNs/Mozilla) guarda a mensagem
    enquanto o device está offline antes de descartar.

    Outcomes (TP/SL) costumam disparar horas após a entry — se device dormir,
    push é perdido com TTL curto. Recs novas a gente quer entregar rápido ou
    descartar (notícia "fresca"). Heurística pelo tipo do payload:
      - outcome  → 12h  (TP/SL bate enquanto user dorme — entregar quando acordar)
      - rec nova → 1h   (sinal envelhece rápido em cripto)
    """
    from pywebpush import webpush  # type: ignore
    tag = (payload.get("tag") or "")
    is_outcome = tag.startswith("outcome-")
    ttl = 43200 if is_outcome else 3600  # 12h ou 1h
    webpush(
        subscription_info={
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        },
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_SUBJECT},
        ttl=ttl,
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
    if tier == "B" and not PUSH_TIER_B_ENABLED:
        return 0  # gate global: tier B não dispara push

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

    tf_short = rec.get("timeframe", "")
    focus_url = f"/?focus={symbol_short}&tf={tf_short}"
    payload = {
        "title": title,
        "body": body,
        "tag": f"rec-{symbol_short}-{tf_short}",  # deduplica notifs
        "data": {
            "symbol": rec.get("symbol"),
            "timeframe": tf_short,
            "tier": tier,
            "url": focus_url,
        },
    }

    sent = await _fanout_push(subs, payload)
    if sent:
        log.info(f"Push enviado pra {sent} device(s) — {tier} {symbol_short}")
    return sent


async def notify_recommendations_batch(recs: List[Dict[str, Any]], newly_saved: int) -> int:
    """
    Dispara push só para recs marcadas com `_just_saved=True` por
    save_recommendations (flag setada no próprio dict da rec quando o
    snapshot é efetivamente inserido, vs duplicata que já existia).

    Isso garante que push notifications NÃO se repitam para o mesmo setup
    que ainda está dentro da janela de dedup (2h). Limita a PUSH_BATCH_CAP
    alerts/batch (default 5, env-driven) pra não floodar.
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

    candidates = just_saved[:PUSH_BATCH_CAP]   # cap configurável (env PUSH_BATCH_CAP, default 5)

    total_sent = 0
    for rec in candidates:
        try:
            total_sent += await notify_new_recommendation(rec)
        except Exception as e:
            log.warning(f"notify_new_recommendation falhou: {e}")
    return total_sent


async def notify_outcome(snap, event: str) -> int:
    """
    Dispara push de SAÍDA de um trade: TP1, TP2, stop, BE+ ou expiry pós-TP1.

    Args:
        snap: RecommendationSnapshot (precisa de symbol, tier, direction,
              timeframe, realized_r)
        event: um de:
          - "tp1_partial"  (TP1 batido — parcial 50%, trail ativo)
          - "tp2"          (TP2 batido — saída total)
          - "be_plus"      (stop pós-TP1 ativado — saída em BE+/trail)
          - "expired_tp1"  (expirou pós-TP1 — parcial trava)
          - "lost"         (stop antes de TP1)

    Respeita filtro de tier do subscriber (mesma lógica de
    notify_new_recommendation).
    """
    if not PUSH_ENABLED:
        return 0
    tier = getattr(snap, "tier", "") or ""
    if tier not in ("A+", "A", "B"):
        return 0
    if tier == "B" and not PUSH_TIER_B_ENABLED:
        return 0  # gate global: tier B não dispara push (nem outcomes)

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

    symbol_short = (getattr(snap, "symbol", "") or "").split("/")[0]
    direction = (getattr(snap, "direction", "") or "").upper()
    realized_r = getattr(snap, "realized_r", 0) or 0
    tf = getattr(snap, "timeframe", "") or ""

    # Título + corpo por evento
    if event == "tp2":
        title = f"🚀 TP2 · {symbol_short} {direction}"
        body = f"{tf} · TP2 batido! +{realized_r:.1f}R fechado"
    elif event == "tp1_partial":
        title = f"🎯 TP1 · {symbol_short} {direction}"
        body = f"{tf} · TP1 batido (parcial 50%) — stop subiu pra BE+, trail ativo"
    elif event == "be_plus":
        title = f"✅ BE+ · {symbol_short} {direction}"
        body = f"{tf} · saída pós-TP1 em BE+/trail · +{realized_r:.1f}R"
    elif event == "expired_tp1":
        title = f"⏰ Expirou · {symbol_short} {direction}"
        body = f"{tf} · 48h pós-TP1 · +{realized_r:.1f}R travados na parcial"
    elif event == "lost":
        title = f"🛑 Stop · {symbol_short} {direction}"
        body = f"{tf} · stop batido · {realized_r:.1f}R"
    else:
        return 0

    focus_url = f"/?focus={symbol_short}&tf={tf}&event={event}"
    payload = {
        "title": title,
        "body": body,
        "tag": f"outcome-{symbol_short}-{tf}-{event}",
        "data": {
            "symbol": getattr(snap, "symbol", None),
            "timeframe": tf,
            "tier": tier,
            "event": event,
            "url": focus_url,
        },
    }

    sent = await _fanout_push(subs, payload)
    if sent:
        log.info(f"Push outcome ({event}) enviado pra {sent} device(s) — {tier} {symbol_short}")
    return sent


async def notify_trade_open(trade: Dict[str, Any]) -> int:
    """
    Push quando o bot abre uma trade REAL na exchange (source="auto").
    Dispara independente do tier do subscriber — execução real é evento
    crítico que merece notificação pra todos os subscribers ativos.

    `trade` precisa de: symbol, side, qty, entry_price, leverage,
                       planned_stop, planned_tp1, planned_tp2,
                       source, exchange, exchange_order_id.
    """
    if not PUSH_ENABLED:
        return 0

    source = (trade.get("source") or "").lower()
    if source not in ("auto", "shadow", "managed"):
        return 0  # ignora trades manuais (advise-only)

    async with get_session() as session:
        stmt = select(PushSubscription).where(PushSubscription.active.is_(True))
        subs = (await session.execute(stmt)).scalars().all()

    if not subs:
        return 0

    symbol_short = (trade.get("symbol") or "").split("/")[0].replace(":USDT", "")
    side = (trade.get("side") or "").upper()
    qty = trade.get("qty") or 0
    entry = trade.get("entry_price") or 0
    lev = trade.get("leverage") or 1
    sl = trade.get("planned_stop")
    tp1 = trade.get("planned_tp1")
    tp2 = trade.get("planned_tp2")
    exch = trade.get("exchange") or "?"
    notional = qty * entry if (qty and entry) else 0

    if source == "auto":
        emoji = "💵"
        prefix = "EXECUTADO"
    elif source == "managed":
        emoji = "🤝"
        prefix = "GERENCIADO"
    else:
        emoji = "👻"
        prefix = "SHADOW"

    title = f"{emoji} {prefix} · {symbol_short} {side} {lev}x"
    body_parts = [
        f"qty={_fmt(qty)} @ {_fmt(entry)} · notional ${notional:.0f}",
    ]
    if sl is not None:
        body_parts.append(f"SL {_fmt(sl)}")
    if tp1 is not None:
        body_parts.append(f"TP1 {_fmt(tp1)}")
    if tp2 is not None:
        body_parts.append(f"TP2 {_fmt(tp2)}")
    body = " · ".join(body_parts) + f"\n{exch}"

    payload = {
        "title": title,
        "body": body,
        "tag": f"trade-open-{trade.get('id') or symbol_short}",
        "data": {
            "symbol": trade.get("symbol"),
            "side": side,
            "source": source,
            "trade_id": trade.get("id"),
            "url": f"/?focus={symbol_short}",
        },
    }

    sent = await _fanout_push(subs, payload)
    if sent:
        log.info(f"Push trade-open ({source}) enviado pra {sent} device(s) — {symbol_short} {side}")
    return sent


async def notify_alert(title: str, body: str, tag: str = "alert") -> int:
    """Push de alerta operacional crítico (ex.: posição real sem stop) pra TODOS
    os subscribers ativos, independente de tier. Fail-soft: nunca levanta."""
    if not PUSH_ENABLED:
        return 0
    try:
        async with get_session() as session:
            stmt = select(PushSubscription).where(PushSubscription.active.is_(True))
            subs = (await session.execute(stmt)).scalars().all()
        if not subs:
            return 0
        payload = {
            "title": title,
            "body": body,
            "tag": tag,
            "data": {"url": "/", "alert": True},
        }
        sent = await _fanout_push(subs, payload)
        if sent:
            log.info(f"Push ALERTA enviado pra {sent} device(s) — {title}")
        return sent
    except Exception as e:
        log.warning(f"notify_alert falhou: {e}")
        return 0


def _fmt(n: float) -> str:
    if n >= 1000:
        return f"{n:,.2f}"
    if n >= 1:
        return f"{n:.4f}"
    return f"{n:.6f}"
