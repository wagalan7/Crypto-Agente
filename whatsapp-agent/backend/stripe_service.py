from __future__ import annotations
import logging
import stripe
import config
import database as db

logger = logging.getLogger(__name__)


PLANS = {
    "mensal":    {"label": "Mensal",    "price_env": "STRIPE_PRICE_MENSAL",    "mode": "subscription"},
    "semestral": {"label": "Semestral", "price_env": "STRIPE_PRICE_SEMESTRAL", "mode": "subscription"},
    "anual":     {"label": "Anual",     "price_env": "STRIPE_PRICE_ANUAL",     "mode": "subscription"},
}


def _get_price_id(plan: str) -> str:
    env_key = PLANS.get(plan, PLANS["mensal"])["price_env"]
    price_id = getattr(config, env_key, "") or config.STRIPE_PRICE_ID
    return price_id


def _configured() -> bool:
    return bool(config.STRIPE_SECRET_KEY and (
        config.STRIPE_PRICE_MENSAL or config.STRIPE_PRICE_ID
    ))


def create_checkout_session(tenant: dict, plan: str = "mensal") -> str:
    """Cria uma Stripe Checkout Session para o plano selecionado."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    base = config.BASE_URL
    setup_token = tenant.get("setup_token", "")
    price_id = _get_price_id(plan)
    plan_label = PLANS.get(plan, PLANS["mensal"])["label"]

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=tenant.get("email") or None,
        client_reference_id=setup_token,
        success_url=f"{base}/onboarding/sucesso?token={setup_token}&paid=1",
        cancel_url=f"{base}/onboarding/pagamento?token={setup_token}&cancelled=1",
        metadata={"setup_token": setup_token, "slug": tenant["slug"], "plan": plan},
        subscription_data={"metadata": {"setup_token": setup_token, "slug": tenant["slug"], "plan": plan}},
        locale="pt-BR",
        currency="brl",
    )
    db.update_tenant(tenant["slug"], plan=plan)
    logger.info(f"[stripe] Checkout criado para {tenant['slug']} — plano {plan_label}")
    return session.url


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    """Processa evento do Stripe e atualiza o tenant."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, config.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError as e:
        raise ValueError(f"Assinatura inválida: {e}")

    event_type = event["type"]
    logger.info(f"[stripe] Evento recebido: {event_type}")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        setup_token = session.get("client_reference_id") or session.get("metadata", {}).get("setup_token")
        subscription_id = session.get("subscription", "")
        customer_id = session.get("customer", "")

        if setup_token:
            tenant = db.get_tenant_by_setup_token(setup_token)
            if tenant:
                db.update_tenant(tenant["slug"],
                                 status="active",
                                 stripe_customer_id=customer_id,
                                 stripe_subscription_id=subscription_id)
                logger.info(f"[stripe] Tenant {tenant['slug']} ativado — sub={subscription_id}")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        subscription = event["data"]["object"]
        sub_id = subscription["id"]
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            if db.is_tenant_exempt(tenant):
                logger.info(f"[stripe] Tenant {tenant['slug']} isento (free_until) — suspensão ignorada")
            else:
                db.update_tenant(tenant["slug"], status="suspended")
                logger.info(f"[stripe] Tenant {tenant['slug']} suspenso — sub={sub_id}")
                _notify_suspended(tenant)

    elif event_type == "customer.subscription.resumed":
        subscription = event["data"]["object"]
        sub_id = subscription["id"]
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            db.update_tenant(tenant["slug"], status="active")
            logger.info(f"[stripe] Tenant {tenant['slug']} reativado — sub={sub_id}")
            _notify_reactivated(tenant)

    return {"received": True}


def _notify_suspended(tenant: dict):
    """Envia WhatsApp para a psicóloga avisando que o acesso foi suspenso."""
    import asyncio, config as _cfg
    psy_phone = tenant.get("psychologist_phone", "")
    setup_token = tenant.get("setup_token", "")
    if not psy_phone:
        return
    msg = (
        f"⚠️ *Consultório Inteligente — Acesso suspenso*\n\n"
        f"Olá! O acesso do consultório *{tenant['name']}* foi suspenso por falta de pagamento.\n\n"
        f"O agente está pausado e não responderá seus pacientes até a assinatura ser renovada.\n\n"
        f"Para reativar, acesse:\n"
        f"{_cfg.BASE_URL}/onboarding/pagamento?token={setup_token}\n\n"
        f"Dúvidas? Fale com o suporte: wa.me/5511968439527"
    )
    try:
        import whatsapp_service as wa_svc
        loop = asyncio.new_event_loop()
        loop.run_until_complete(wa_svc.send_message(tenant, psy_phone, msg))
        loop.close()
    except Exception as e:
        logger.warning(f"[stripe] Falha ao notificar suspensão para {psy_phone}: {e}")


def _notify_reactivated(tenant: dict):
    """Envia WhatsApp para a psicóloga avisando que o acesso foi reativado."""
    import asyncio, config as _cfg
    psy_phone = tenant.get("psychologist_phone", "")
    if not psy_phone:
        return
    msg = (
        f"✅ *Consultório Inteligente — Acesso reativado!*\n\n"
        f"Ótimas notícias! O acesso do consultório *{tenant['name']}* foi reativado com sucesso.\n\n"
        f"O agente já está respondendo seus pacientes normalmente. 🎉\n\n"
        f"Acesse seu painel: {_cfg.BASE_URL}/dashboard/{tenant['slug']}?token={tenant.get('dashboard_token','')}"
    )
    try:
        import whatsapp_service as wa_svc
        loop = asyncio.new_event_loop()
        loop.run_until_complete(wa_svc.send_message(tenant, psy_phone, msg))
        loop.close()
    except Exception as e:
        logger.warning(f"[stripe] Falha ao notificar reativação para {psy_phone}: {e}")


def _get_tenant_by_stripe_sub(subscription_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE stripe_subscription_id = ?", (subscription_id,)
        ).fetchone()
    return dict(row) if row else None


def get_billing_portal_url(tenant: dict) -> str | None:
    """Retorna URL do portal de cobrança Stripe para o cliente gerenciar a assinatura."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    customer_id = tenant.get("stripe_customer_id", "")
    if not customer_id:
        return None
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{config.BASE_URL}/dashboard/{tenant['slug']}?token={tenant.get('dashboard_token', '')}",
        )
        return session.url
    except Exception as e:
        logger.warning(f"[stripe] Erro ao criar portal: {e}")
        return None
