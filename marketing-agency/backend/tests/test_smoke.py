"""Smoke tests — boot + critical endpoints respond + plan gating works."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signup_login_me(client):
    r = client.post("/auth/signup", json={"email": "u1@example.com", "password": "supersecret"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    assert token
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "u1@example.com"
    # Trial grants Pro-tier features
    assert body["plan"]["trialing"] is True


def test_signup_short_password_rejected(client):
    r = client.post("/auth/signup", json={"email": "u2@example.com", "password": "short"})
    assert r.status_code == 400


def test_duplicate_signup(client):
    client.post("/auth/signup", json={"email": "dup@example.com", "password": "supersecret"})
    r = client.post("/auth/signup", json={"email": "dup@example.com", "password": "supersecret"})
    assert r.status_code == 400


def test_login_wrong_password(client):
    client.post("/auth/signup", json={"email": "loginer@example.com", "password": "supersecret"})
    r = client.post("/auth/login", json={"email": "loginer@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_plans_endpoint_public(client):
    r = client.get("/billing/plans")
    assert r.status_code == 200
    tiers = {p["tier"] for p in r.json()}
    assert {"free", "pro", "agency"}.issubset(tiers)


def test_unauthorized_routes(client):
    # /clients should require auth
    r = client.get("/clients/")
    assert r.status_code in (401, 403)


def test_create_client_within_limit(client, signup_user):
    token, user = signup_user()
    h = {"Authorization": f"Bearer {token}"}
    # During trial, user has Pro plan = 3 clients allowed
    r1 = client.post("/clients/", json={"name": "Brand 1"}, headers=h)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/clients/", json={"name": "Brand 2"}, headers=h)
    assert r2.status_code == 200
    r3 = client.post("/clients/", json={"name": "Brand 3"}, headers=h)
    assert r3.status_code == 200
    # 4th should hit limit
    r4 = client.post("/clients/", json={"name": "Brand 4"}, headers=h)
    assert r4.status_code == 402


def test_onboarding_complete(client, signup_user):
    token, user = signup_user()
    assert user["onboarding_completed"] is False
    h = {"Authorization": f"Bearer {token}"}
    r = client.post("/auth/onboarding/complete", json={"completed": True}, headers=h)
    assert r.status_code == 200
    me = client.get("/auth/me", headers=h).json()
    assert me["onboarding_completed"] is True
