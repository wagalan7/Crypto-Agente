"""Plan tiers + enforcement.

Single source of truth for what each plan allows. Used by:
  - signup (assigns `free` + 7-day trial of `pro`)
  - middleware-style checks in clients/content routers
  - billing UI

Keep limits low enough that an honest free user converts; high enough
that nobody hits them in the first 10 minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import User, Client, ContentPiece

PlanTier = Literal["free", "pro", "agency"]


@dataclass(frozen=True)
class Plan:
    tier: PlanTier
    label: str
    price_brl: int  # in cents
    max_clients: int
    max_posts_per_month: int
    allow_auto_publish: bool
    allow_pdf_report: bool
    allow_voice_scorer: bool
    allow_trends: bool
    stripe_price_env: str | None  # env var holding the Stripe price ID


PLANS: dict[str, Plan] = {
    "free": Plan(
        tier="free",
        label="Grátis",
        price_brl=0,
        max_clients=1,
        max_posts_per_month=10,
        allow_auto_publish=False,
        allow_pdf_report=False,
        allow_voice_scorer=False,
        allow_trends=True,
        stripe_price_env=None,
    ),
    "pro": Plan(
        tier="pro",
        label="Pro",
        price_brl=9700,  # R$ 97
        max_clients=3,
        max_posts_per_month=120,
        allow_auto_publish=True,
        allow_pdf_report=True,
        allow_voice_scorer=True,
        allow_trends=True,
        stripe_price_env="STRIPE_PRICE_PRO",
    ),
    "agency": Plan(
        tier="agency",
        label="Agency",
        price_brl=29700,  # R$ 297
        max_clients=15,
        max_posts_per_month=600,
        allow_auto_publish=True,
        allow_pdf_report=True,
        allow_voice_scorer=True,
        allow_trends=True,
        stripe_price_env="STRIPE_PRICE_AGENCY",
    ),
}


def get_plan(user: User) -> Plan:
    """Return the effective plan, treating an active trial as `pro`."""
    if user.role == "master":
        # Masters bypass plan limits (internal/founder accounts).
        return PLANS["agency"]
    tier = (user.plan_tier or "free").lower()
    # Trial grace
    if user.trial_ends_at and user.trial_ends_at > datetime.utcnow() and tier == "free":
        return PLANS["pro"]
    return PLANS.get(tier, PLANS["free"])


def plan_status(user: User) -> dict:
    p = get_plan(user)
    now = datetime.utcnow()
    trialing = bool(user.trial_ends_at and user.trial_ends_at > now and (user.plan_tier or "free") == "free")
    return {
        "tier": p.tier,
        "label": p.label,
        "price_brl_cents": p.price_brl,
        "limits": {
            "max_clients": p.max_clients,
            "max_posts_per_month": p.max_posts_per_month,
        },
        "features": {
            "auto_publish": p.allow_auto_publish,
            "pdf_report": p.allow_pdf_report,
            "voice_scorer": p.allow_voice_scorer,
            "trends": p.allow_trends,
        },
        "trialing": trialing,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
        "stripe_customer_id": user.stripe_customer_id,
        "status": user.plan_status or "active",
    }


def assert_can_create_client(user: User, db: Session) -> None:
    p = get_plan(user)
    count = db.query(func.count(Client.id)).filter(Client.owner_id == user.id).scalar() or 0
    if count >= p.max_clients:
        raise HTTPException(
            status_code=402,
            detail=f"Limite do plano {p.label}: {p.max_clients} cliente(s). Faça upgrade para criar mais.",
        )


def assert_can_create_content(user: User, db: Session) -> None:
    p = get_plan(user)
    first_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Count posts created by anyone, on any client this user owns.
    count = (
        db.query(func.count(ContentPiece.id))
        .join(Client, Client.id == ContentPiece.client_id)
        .filter(Client.owner_id == user.id, ContentPiece.created_at >= first_of_month)
        .scalar() or 0
    )
    if count >= p.max_posts_per_month:
        raise HTTPException(
            status_code=402,
            detail=f"Limite do plano {p.label}: {p.max_posts_per_month} posts/mês. Faça upgrade.",
        )


def assert_feature(user: User, feature: str) -> None:
    """Raise 402 if the user's plan does not allow a feature."""
    p = get_plan(user)
    allowed = {
        "auto_publish": p.allow_auto_publish,
        "pdf_report": p.allow_pdf_report,
        "voice_scorer": p.allow_voice_scorer,
        "trends": p.allow_trends,
    }
    if not allowed.get(feature, False):
        raise HTTPException(status_code=402, detail=f"Recurso '{feature}' requer plano superior.")


def start_trial(user: User, days: int = 7) -> None:
    user.trial_ends_at = datetime.utcnow() + timedelta(days=days)
