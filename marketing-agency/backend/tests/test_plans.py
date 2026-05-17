"""Unit tests for plan tier resolution + trial logic."""
from datetime import datetime, timedelta

from services.plans import get_plan, plan_status, start_trial, PLANS


class _FakeUser:
    def __init__(self, **kw):
        self.role = kw.get("role", "user")
        self.plan_tier = kw.get("plan_tier", "free")
        self.plan_status = kw.get("plan_status", "active")
        self.trial_ends_at = kw.get("trial_ends_at")
        self.stripe_customer_id = kw.get("stripe_customer_id")


def test_free_user_gets_free_plan():
    u = _FakeUser(plan_tier="free")
    assert get_plan(u).tier == "free"


def test_active_trial_grants_pro():
    u = _FakeUser(plan_tier="free", trial_ends_at=datetime.utcnow() + timedelta(days=3))
    assert get_plan(u).tier == "pro"


def test_expired_trial_falls_back():
    u = _FakeUser(plan_tier="free", trial_ends_at=datetime.utcnow() - timedelta(days=1))
    assert get_plan(u).tier == "free"


def test_master_always_agency():
    u = _FakeUser(role="master", plan_tier="free")
    assert get_plan(u).tier == "agency"


def test_pro_paid_user_keeps_pro():
    u = _FakeUser(plan_tier="pro")
    assert get_plan(u).tier == "pro"


def test_start_trial_sets_future_date():
    u = _FakeUser()
    start_trial(u, days=7)
    assert u.trial_ends_at is not None
    assert u.trial_ends_at > datetime.utcnow()


def test_plan_status_has_required_keys():
    u = _FakeUser()
    s = plan_status(u)
    assert {"tier", "label", "limits", "features", "trialing"}.issubset(s.keys())
    assert "max_clients" in s["limits"]


def test_all_three_tiers_defined():
    assert set(PLANS.keys()) == {"free", "pro", "agency"}
    assert PLANS["pro"].max_clients > PLANS["free"].max_clients
    assert PLANS["agency"].max_clients > PLANS["pro"].max_clients
