"""Pytest fixtures — in-memory SQLite app + helper to create users.

Disables scheduler + Stripe so tests don't hit the network.
"""
import os
import sys

# Force SQLite + disable background jobs BEFORE app imports
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["DISABLE_SIGNUP"] = "0"
os.environ.setdefault("JWT_SECRET", "test-secret-please-change")

# Ensure backend root is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def app():
    from main import app as _app
    from database import init_db
    init_db()
    return _app


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def signup_user(client):
    """Factory that returns (token, user_dict)."""
    counter = {"n": 0}

    def _make(email: str | None = None, password: str = "test1234"):
        counter["n"] += 1
        e = email or f"test{counter['n']}@example.com"
        r = client.post("/auth/signup", json={"email": e, "password": password, "name": "Test"})
        assert r.status_code == 200, r.text
        body = r.json()
        return body["access_token"], body["user"]

    return _make
