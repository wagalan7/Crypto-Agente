from __future__ import annotations
import logging
import stripe
import config
import database as db

logger = logging.getLogger(__name__)


def _configured() -> bool:
    return bool(config.STRIPE_SECRET_KEY and config.STRIPE_PRICE_ID)


def create_checkout_session(tenant: dict) -> str:
    """Cria uma Stripe Checkout Session e retorna a URL de pagamento."""
    stripe.api_key = config.STRIPE_SECRET_KEY
    base = config.BASE_URL
    setup_token = tenant.get("setup_token", "")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
        customer_email=tenant.get("email") or None,
        client_reference_id=setup_token,
        success_url=f"{base}/onboarding/sucesso?token={setup_token}&paid=1",
        cancel_url=f"{base}/onboarding/pagamento?token={setup_token}&cancelled=1",
        metadata={"setup_token": setup_token, "slug": tenant["slug"]},
        subscription_data={"metadata": {"setup_token": setup_token, "slug": tenant["slug"]}},
        locale="pt-BR",
        currency="brl",
    )
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
        # Buscar tenant pelo subscription_id
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            db.update_tenant(tenant["slug"], status="suspended")
            logger.info(f"[stripe] Tenant {tenant['slug']} suspenso — sub={sub_id}")

    elif event_type == "customer.subscription.resumed":
        subscription = event["data"]["object"]
        sub_id = subscription["id"]
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            db.update_tenant(tenant["slug"], status="active")
            logger.info(f"[stripe] Tenant {tenant['slug']} reativado — sub={sub_id}")

    return {"received": True}


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
