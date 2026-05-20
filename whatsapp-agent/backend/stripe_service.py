from __future__ import annotations
import logging
from datetime import datetime, timezone
import stripe
import config
import database as db

logger = logging.getLogger(__name__)


# Metadados de cada plano: label, variável de ambiente do price_id, valor total cobrado
PLANS = {
    "mensal": {
        "label":     "Mensal",
        "price_env": "STRIPE_PRICE_MENSAL",
        "mode":      "subscription",
        "valor":     "R$ 199/mês",
        "resumo":    "cobrado mensalmente",
    },
    "semestral": {
        "label":     "Semestral",
        "price_env": "STRIPE_PRICE_SEMESTRAL",
        "mode":      "subscription",
        "valor":     "R$ 1.014 a cada 6 meses",
        "resumo":    "cobrado semestralmente",
    },
    "anual": {
        "label":     "Anual",
        "price_env": "STRIPE_PRICE_ANUAL",
        "mode":      "subscription",
        "valor":     "R$ 1.788/ano",
        "resumo":    "cobrado anualmente",
    },
}


def _get_price_id(plan: str) -> str:
    env_key = PLANS.get(plan, PLANS["mensal"])["price_env"]
    price_id = getattr(config, env_key, "") or config.STRIPE_PRICE_ID
    return price_id


def _configured() -> bool:
    return bool(config.STRIPE_SECRET_KEY and (
        config.STRIPE_PRICE_MENSAL or config.STRIPE_PRICE_ID
    ))


def _iso_from_timestamp(ts) -> str | None:
    """Converte Unix timestamp do Stripe para ISO date string (YYYY-MM-DD)."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


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

    # ── Pagamento inicial concluído ────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        setup_token = (
            session.get("client_reference_id")
            or session.get("metadata", {}).get("setup_token")
        )
        subscription_id = session.get("subscription", "")
        customer_id = session.get("customer", "")

        if setup_token:
            tenant = db.get_tenant_by_setup_token(setup_token)
            if tenant:
                # Buscar subscription para pegar current_period_end
                expires_at = None
                if subscription_id:
                    try:
                        sub = stripe.Subscription.retrieve(subscription_id)
                        expires_at = _iso_from_timestamp(sub.get("current_period_end"))
                    except Exception as e:
                        logger.warning(f"[stripe] Não foi possível buscar sub {subscription_id}: {e}")

                db.update_tenant(tenant["slug"],
                                 status="active",
                                 stripe_customer_id=customer_id,
                                 stripe_subscription_id=subscription_id,
                                 plan_expires_at=expires_at)
                logger.info(
                    f"[stripe] Tenant {tenant['slug']} ativado — sub={subscription_id} "
                    f"expira={expires_at}"
                )
                _send_activation_email(tenant, expires_at)

    # ── Assinatura renovada (cobrança recorrente bem-sucedida) ─────────────────
    elif event_type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        sub_id = subscription["id"]
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            expires_at = _iso_from_timestamp(subscription.get("current_period_end"))
            # Atualizar data de expiração e garantir status active se estava suspenso
            updates = {"plan_expires_at": expires_at}
            if subscription.get("status") == "active":
                updates["status"] = "active"
            db.update_tenant(tenant["slug"], **updates)
            logger.info(
                f"[stripe] Sub atualizada — tenant={tenant['slug']} "
                f"status={subscription.get('status')} expira={expires_at}"
            )

    # ── Renovação paga com sucesso (invoice) ───────────────────────────────────
    elif event_type == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription", "")
        if sub_id:
            tenant = _get_tenant_by_stripe_sub(sub_id)
            if tenant and tenant.get("status") != "active":
                # Pode ocorrer quando pagamento atrasado é quitado
                try:
                    sub = stripe.Subscription.retrieve(sub_id)
                    expires_at = _iso_from_timestamp(sub.get("current_period_end"))
                    db.update_tenant(tenant["slug"], status="active", plan_expires_at=expires_at)
                    logger.info(f"[stripe] Tenant {tenant['slug']} reativado via invoice — expira={expires_at}")
                    _notify_reactivated(tenant)
                except Exception as e:
                    logger.warning(f"[stripe] Erro ao processar invoice.payment_succeeded: {e}")

    # ── Assinatura cancelada ou pausada ────────────────────────────────────────
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

    # ── Assinatura retomada ────────────────────────────────────────────────────
    elif event_type == "customer.subscription.resumed":
        subscription = event["data"]["object"]
        sub_id = subscription["id"]
        tenant = _get_tenant_by_stripe_sub(sub_id)
        if tenant:
            expires_at = _iso_from_timestamp(subscription.get("current_period_end"))
            db.update_tenant(tenant["slug"], status="active", plan_expires_at=expires_at)
            logger.info(f"[stripe] Tenant {tenant['slug']} reativado — sub={sub_id} expira={expires_at}")
            _notify_reactivated(tenant)

    return {"received": True}


def _plan_info(tenant: dict) -> dict:
    """Retorna metadados do plano atual do tenant."""
    plan_key = tenant.get("plan") or "mensal"
    return PLANS.get(plan_key, PLANS["mensal"])


def _send_activation_email(tenant: dict, expires_at: str | None = None):
    """Envia e-mail de ativação após pagamento confirmado."""
    email = tenant.get("email", "")
    if not email:
        return
    try:
        import email_service as email_svc
        plan = _plan_info(tenant)
        name = tenant.get("full_name") or tenant.get("psychologist_name") or tenant.get("name") or ""
        email_svc.send_activation_email(
            email=email,
            name=name,
            slug=tenant["slug"],
            dashboard_token=tenant.get("dashboard_token", ""),
            setup_token=tenant.get("setup_token", ""),
            plan_label=plan["label"],
            expires_at=expires_at,
        )
    except Exception as e:
        logger.warning(f"[stripe] Falha ao enviar e-mail de ativação para {email}: {e}")


def _notify_suspended(tenant: dict):
    """Envia WhatsApp para a psicóloga avisando que o acesso foi suspenso."""
    import asyncio
    psy_phone = tenant.get("psychologist_phone", "")
    setup_token = tenant.get("setup_token", "")
    if not psy_phone:
        return

    plan = _plan_info(tenant)
    expires_at = tenant.get("plan_expires_at", "")
    expires_str = ""
    if expires_at:
        try:
            d = datetime.fromisoformat(expires_at)
            expires_str = f"\nSeu plano venceu em *{d.strftime('%d/%m/%Y')}*."
        except Exception:
            pass

    msg = (
        f"⚠️ *Consultório Inteligente — Acesso suspenso*\n\n"
        f"Olá! O acesso do consultório *{tenant['name']}* foi suspenso.\n"
        f"{expires_str}\n\n"
        f"📋 *Plano anterior:* {plan['label']} ({plan['valor']})\n\n"
        f"O agente está *pausado* e não responderá seus pacientes até a assinatura ser renovada.\n\n"
        f"Para reativar agora, acesse:\n"
        f"{config.BASE_URL}/onboarding/pagamento?token={setup_token}&suspended=1\n\n"
        f"Dúvidas? Fale com o suporte:\nwa.me/5511968439527"
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
    import asyncio
    psy_phone = tenant.get("psychologist_phone", "")
    if not psy_phone:
        return

    plan = _plan_info(tenant)
    expires_at = tenant.get("plan_expires_at", "")
    expires_str = ""
    if expires_at:
        try:
            d = datetime.fromisoformat(expires_at)
            expires_str = f"\n📅 Próxima cobrança: *{d.strftime('%d/%m/%Y')}*"
        except Exception:
            pass

    msg = (
        f"✅ *Consultório Inteligente — Acesso reativado!*\n\n"
        f"Ótimas notícias! O acesso do consultório *{tenant['name']}* foi reativado.\n\n"
        f"📋 *Plano:* {plan['label']} ({plan['valor']}, {plan['resumo']})"
        f"{expires_str}\n\n"
        f"O agente já está respondendo seus pacientes normalmente. 🎉\n\n"
        f"Acesse seu painel:\n"
        f"{config.BASE_URL}/dashboard/{tenant['slug']}?token={tenant.get('dashboard_token','')}"
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
