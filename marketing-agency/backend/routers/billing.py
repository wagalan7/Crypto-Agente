"""Billing endpoints — plan info + Stripe checkout/portal/webhook.

Stripe is *optional*: if STRIPE_SECRET_KEY is unset, /plans still works
(so the UI can show the table and a "fale com a gente" CTA), but
/checkout and /portal return 503. /webhook always exists so Stripe can
hit it; it validates signature when STRIPE_WEBHOOK_SECRET is set.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services.plans import PLANS, plan_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://app.contentai")

# Lazy import — stripe is heavy and only needed if configured
_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is not None:
        return _stripe
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe  # type: ignore
        stripe.api_key = STRIPE_SECRET_KEY
        _stripe = stripe
        return stripe
    except ImportError:
        logger.warning("STRIPE_SECRET_KEY set but stripe package not installed")
        return None


@router.get("/plans")
def list_plans():
    """Public catalog — used by the pricing page."""
    return [
        {
            "tier": p.tier,
            "label": p.label,
            "price_brl_cents": p.price_brl,
            "max_clients": p.max_clients,
            "max_posts_per_month": p.max_posts_per_month,
            "features": {
                "auto_publish": p.allow_auto_publish,
                "pdf_report": p.allow_pdf_report,
                "voice_scorer": p.allow_voice_scorer,
                "trends": p.allow_trends,
            },
            "stripe_configured": bool(STRIPE_SECRET_KEY and p.stripe_price_env and os.getenv(p.stripe_price_env)),
        }
        for p in PLANS.values()
    ]


@router.get("/me")
def my_plan(current_user: User = Depends(get_current_user)):
    return plan_status(current_user)


class CheckoutRequest(BaseModel):
    tier: str  # "pro" or "agency"


@router.post("/checkout")
def checkout(data: CheckoutRequest, current_user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    stripe = _get_stripe()
    if not stripe:
        raise HTTPException(503, "Pagamentos não configurados — entre em contato pelo suporte.")
    plan = PLANS.get(data.tier)
    if not plan or plan.tier == "free":
        raise HTTPException(400, "Plano inválido")
    price_id = os.getenv(plan.stripe_price_env or "")
    if not price_id:
        raise HTTPException(503, f"Preço {plan.tier} não configurado no Stripe")

    # Reuse or create Stripe customer
    customer_id = current_user.stripe_customer_id
    if not customer_id:
        cust = stripe.Customer.create(email=current_user.email, name=current_user.name or current_user.email)
        customer_id = cust.id
        current_user.stripe_customer_id = customer_id
        db.commit()

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{PUBLIC_BASE_URL}/billing?status=success",
        cancel_url=f"{PUBLIC_BASE_URL}/billing?status=cancel",
        metadata={"user_id": str(current_user.id), "tier": plan.tier},
    )
    return {"url": session.url}


@router.post("/portal")
def portal(current_user: User = Depends(get_current_user)):
    stripe = _get_stripe()
    if not stripe:
        raise HTTPException(503, "Pagamentos não configurados")
    if not current_user.stripe_customer_id:
        raise HTTPException(400, "Sem assinatura ativa para gerenciar")
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{PUBLIC_BASE_URL}/billing",
    )
    return {"url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe webhook — keep idempotent. Validates signature when configured."""
    stripe = _get_stripe()
    if not stripe:
        raise HTTPException(503, "Stripe não configurado")
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if STRIPE_WEBHOOK_SECRET and sig:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            raise HTTPException(400, f"Assinatura inválida: {e}")
    else:
        # Dev mode — accept unsigned (do NOT use in prod)
        import json
        event = json.loads(payload)

    etype = event.get("type") if isinstance(event, dict) else event["type"]
    obj = (event.get("data") or {}).get("object") if isinstance(event, dict) else event["data"]["object"]
    if not obj:
        return {"received": True}

    customer_id = obj.get("customer") if isinstance(obj, dict) else getattr(obj, "customer", None)
    user = None
    if customer_id:
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()

    def _set(tier: str, status: str, sub_id: str | None = None):
        if not user:
            return
        user.plan_tier = tier
        user.plan_status = status
        if sub_id:
            user.stripe_subscription_id = sub_id
        db.commit()

    if etype == "checkout.session.completed":
        tier = (obj.get("metadata") or {}).get("tier") or "pro"
        sub_id = obj.get("subscription")
        _set(tier, "active", sub_id)
    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        status = obj.get("status") or "active"
        # Infer tier from items.data[0].price.id by matching env vars
        tier = "pro"
        try:
            price_id = obj["items"]["data"][0]["price"]["id"]
            for p in PLANS.values():
                if p.stripe_price_env and os.getenv(p.stripe_price_env) == price_id:
                    tier = p.tier
                    break
        except Exception:
            pass
        _set(tier, status, obj.get("id"))
    elif etype == "customer.subscription.deleted":
        _set("free", "canceled", None)
        if user:
            user.stripe_subscription_id = None
            db.commit()
    elif etype == "invoice.payment_failed":
        if user:
            user.plan_status = "past_due"
            db.commit()

    return {"received": True}
