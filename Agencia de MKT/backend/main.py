import os
import asyncio
import secrets
import urllib.parse
import httpx
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from models import ProductInput
from orchestrator import run_agency
from auth import (authenticate, require_auth, require_admin,
                   list_users, add_user, delete_user, update_password, update_user, get_user_role,
                   verify_token)
from db import (init_db, db_seed_users, save_campaign, list_campaigns, get_campaign,
                grant_access, revoke_access, get_campaign_grants,
                save_credential, get_credentials, delete_platform_credentials)
from services.social_publisher import (
    publish_facebook, publish_instagram, publish_twitter, publish_webhook, publish_google_ads
)
from services.metrics_fetcher import (
    fetch_facebook_metrics, fetch_instagram_metrics, fetch_twitter_metrics
)

app = FastAPI(title="Maga One Marketing")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

# Seed users from USERS_JSON env var or admin defaults (only if DB is empty)
_seed: list[dict] = []
_users_json = os.getenv("USERS_JSON", "")
if _users_json:
    try:
        import json as _json
        _seed = _json.loads(_users_json)
    except Exception:
        pass
if not _seed:
    _seed = [{"user": os.getenv("ADMIN_USER", "admin"),
              "pass": os.getenv("ADMIN_PASS", "admin123"),
              "role": "admin", "name": ""}]
db_seed_users(_seed)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSETS_DIR = os.path.join(STATIC_DIR, "assets")


# ── Auth ──────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/auth/login")
async def login(req: LoginRequest):
    token = authenticate(req.username, req.password)
    return {"token": token}

@app.get("/auth/me")
async def me(user: str = Depends(require_auth)):
    return {"user": user, "role": get_user_role(user)}

@app.get("/auth/users")
async def get_users(user: str = Depends(require_auth)):
    require_admin(user)
    return list_users()

class AddUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    name: str = ""

@app.post("/auth/users")
async def create_user(req: AddUserRequest, user: str = Depends(require_auth)):
    require_admin(user)
    return add_user(req.username, req.password, req.role, req.name)

class UpdateUserRequest(BaseModel):
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    name: Optional[str] = None

@app.patch("/auth/users/{username}")
async def patch_user(username: str, req: UpdateUserRequest, user: str = Depends(require_auth)):
    return update_user(username, requester=user,
                       new_username=req.new_username,
                       new_password=req.new_password,
                       new_name=req.name)

@app.delete("/auth/users/{username}")
async def remove_user(username: str, user: str = Depends(require_auth)):
    require_admin(user)
    delete_user(username, requester=user)
    return {"ok": True}

class UpdatePassRequest(BaseModel):
    username: str
    new_password: str

@app.patch("/auth/users/password")
async def change_password(req: UpdatePassRequest, user: str = Depends(require_auth)):
    update_password(req.username, req.new_password, requester=user)
    return {"ok": True}


# ── Credentials ───────────────────────────────────────────────

class SaveCredentialRequest(BaseModel):
    platform: str
    credentials: dict  # {key: value}

@app.get("/credentials")
async def get_user_credentials(user: str = Depends(require_auth)):
    return get_credentials(user)

@app.post("/credentials")
async def save_user_credentials(req: SaveCredentialRequest, user: str = Depends(require_auth)):
    for key, value in req.credentials.items():
        if value is not None and str(value).strip():
            save_credential(user, req.platform, key, str(value).strip())
    return {"ok": True}

@app.delete("/credentials/{platform}")
async def delete_user_credentials(platform: str, user: str = Depends(require_auth)):
    delete_platform_credentials(user, platform)
    return {"ok": True}

@app.get("/auth/google-ads/accounts")
async def list_google_ads_accounts(user: str = Depends(require_auth)):
    """Busca Customer IDs acessíveis usando o Refresh Token já salvo."""
    creds         = get_credentials(user)
    google_creds  = creds.get("google", {})
    refresh_token = google_creds.get("google_refresh_token", "")
    dev_token     = google_creds.get("google_developer_token", "")

    if not refresh_token:
        raise HTTPException(400, detail="Conecte o Google Ads via OAuth primeiro.")
    if not dev_token:
        raise HTTPException(400, detail="Insira o Developer Token antes de buscar contas.")

    # Troca refresh token por access token
    async with httpx.AsyncClient() as c:
        tr = await c.post(GOOGLE_TOKEN_URL, data={
            "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        })
        tokens = tr.json()

    access_token = tokens.get("access_token", "")
    if not access_token:
        raise HTTPException(400, detail=f"Erro ao renovar token: {tokens.get('error', 'desconhecido')}")

    # Lista contas acessíveis
    async with httpx.AsyncClient() as c:
        resp = await c.get(
            "https://googleads.googleapis.com/v19/customers:listAccessibleCustomers",
            headers={
                "Authorization":  f"Bearer {access_token}",
                "developer-token": dev_token,
            }
        )
        data = resp.json()

    if "error" in data:
        raise HTTPException(400, detail=data["error"].get("message", str(data["error"])))

    customers = [name.split("/")[-1] for name in data.get("resourceNames", [])]
    return {"customers": customers}


@app.get("/auth/google-ads/diagnose")
async def diagnose_google_ads(user: str = Depends(require_auth)):
    """Diagnóstico passo a passo das credenciais Google Ads."""
    creds        = get_credentials(user)
    gc           = creds.get("google", {})
    refresh_token = gc.get("google_refresh_token", "")
    dev_token    = gc.get("google_developer_token", "")
    customer_id  = gc.get("google_customer_id", "").replace("-", "").replace(" ", "")
    mcc_id       = gc.get("google_mcc_id", "").replace("-", "").replace(" ", "")

    result = {
        "customer_id": customer_id,
        "mcc_id": mcc_id,
        "has_dev_token": bool(dev_token),
        "has_refresh_token": bool(refresh_token),
        "steps": []
    }

    # Step 1: Get access token
    async with httpx.AsyncClient(timeout=20) as c:
        tr = await c.post(GOOGLE_TOKEN_URL, data={
            "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        })
        tokens = tr.json()
    access_token = tokens.get("access_token", "")
    result["steps"].append({
        "step": "1_access_token",
        "ok": bool(access_token),
        "detail": "OK" if access_token else tokens.get("error_description", tokens.get("error", str(tokens)))
    })
    if not access_token:
        return result

    base_headers = {
        "Authorization":   f"Bearer {access_token}",
        "developer-token": dev_token,
        "Content-Type":    "application/json",
    }
    if mcc_id:
        base_headers["login-customer-id"] = mcc_id

    # Step 2: List accessible customers
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            "https://googleads.googleapis.com/v19/customers:listAccessibleCustomers",
            headers=base_headers,
        )
        try:
            d = r.json()
        except Exception:
            d = {"raw": r.text[:300]}
    customers = [n.split("/")[-1] for n in d.get("resourceNames", [])]
    result["steps"].append({
        "step": "2_list_customers",
        "ok": bool(customers),
        "customers": customers,
        "detail": d.get("error", {}).get("message", "OK") if "error" in d else f"{len(customers)} contas encontradas"
    })

    # Step 3: Get customer resource
    if customer_id:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"https://googleads.googleapis.com/v19/customers/{customer_id}",
                headers=base_headers,
            )
            try:
                d = r.json()
            except Exception:
                d = {"raw": r.text[:300]}
        result["steps"].append({
            "step": "3_get_customer",
            "http_status": r.status_code,
            "ok": r.status_code == 200,
            "detail": d.get("error", {}).get("message", "OK") if r.status_code != 200 else
                      f"status={d.get('status','?')} descriptiveName={d.get('descriptiveName','?')}"
        })

    return result


# ── Google Ads OAuth ──────────────────────────────────────────

# In-memory state store (maps random state → username)
_oauth_states: dict[str, str] = {}

APP_URL = os.getenv("APP_URL", "https://agencia-marketing-ia-production-a5ba.up.railway.app")
GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
GOOGLE_ADS_SCOPE  = "https://www.googleapis.com/auth/adwords"

@app.get("/auth/google-ads/start")
async def google_ads_start(token: str = ""):
    """Accepts JWT token as query param so the browser can navigate directly."""
    if not token:
        raise HTTPException(401, detail="Token ausente")
    user = verify_token(token)   # raises 401 if invalid
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(400, detail="GOOGLE_CLIENT_ID não configurado. Adicione nas variáveis do Railway.")
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = user
    redirect_uri = f"{APP_URL}/auth/google-ads/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_ADS_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return RedirectResponse(GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params))

@app.get("/auth/google-ads/callback")
async def google_ads_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/?google_ads=error&msg=" + urllib.parse.quote(error))
    user = _oauth_states.pop(state, None)
    if not user:
        return RedirectResponse("/?google_ads=error&msg=state_invalido")
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri  = f"{APP_URL}/auth/google-ads/callback"
    async with httpx.AsyncClient() as c:
        resp = await c.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")
    if refresh_token:
        save_credential(user, "google", "google_refresh_token", refresh_token)
        return RedirectResponse("/?google_ads=ok")
    return RedirectResponse("/?google_ads=error&msg=" + urllib.parse.quote(str(tokens)))


# ── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Agency ────────────────────────────────────────────────────

@app.post("/agency/run")
async def run(data: ProductInput, user: str = Depends(require_auth)):
    return StreamingResponse(
        run_agency(data),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Campaigns ────────────────────────────────────────────────

class SaveCampaignRequest(BaseModel):
    produto: str
    input_data: dict
    result_data: dict

@app.post("/campaigns")
async def create_campaign(req: SaveCampaignRequest, user: str = Depends(require_auth)):
    cid = save_campaign(user, req.produto, req.input_data, req.result_data)
    return {"id": cid}

@app.get("/campaigns")
async def get_campaigns_list(user: str = Depends(require_auth)):
    is_admin = get_user_role(user) == "admin"
    return list_campaigns(user, is_admin)

@app.get("/campaigns/{campaign_id}")
async def get_campaign_detail(campaign_id: int, user: str = Depends(require_auth)):
    is_admin = get_user_role(user) == "admin"
    data = get_campaign(campaign_id, user, is_admin)
    if not data:
        raise HTTPException(status_code=404, detail="Campanha não encontrada ou sem acesso")
    return data

class GrantRequest(BaseModel):
    granted_to: str

@app.post("/campaigns/{campaign_id}/grant")
async def grant_campaign(campaign_id: int, req: GrantRequest, user: str = Depends(require_auth)):
    require_admin(user)
    grant_access(campaign_id, req.granted_to, user)
    return {"ok": True}

@app.delete("/campaigns/{campaign_id}/grant/{username}")
async def revoke_campaign(campaign_id: int, username: str, user: str = Depends(require_auth)):
    require_admin(user)
    revoke_access(campaign_id, username)
    return {"ok": True}

@app.get("/campaigns/{campaign_id}/grants")
async def list_grants(campaign_id: int, user: str = Depends(require_auth)):
    require_admin(user)
    return get_campaign_grants(campaign_id)


# ── Publish ───────────────────────────────────────────────────

class PublishRequest(BaseModel):
    text: str
    image_url: Optional[str] = None
    platforms: list[str]
    fb_page_id: Optional[str] = None
    fb_token: Optional[str] = None
    ig_user_id: Optional[str] = None
    ig_token: Optional[str] = None
    tw_api_key: Optional[str] = None
    tw_api_secret: Optional[str] = None
    tw_access_token: Optional[str] = None
    tw_access_secret: Optional[str] = None
    webhook_url: Optional[str] = None
    google_developer_token: Optional[str] = None
    google_customer_id: Optional[str] = None
    google_refresh_token: Optional[str] = None
    tiktok_access_token: Optional[str] = None
    tiktok_advertiser_id: Optional[str] = None
    google_final_url: Optional[str] = None
    google_budget: Optional[str] = None
    google_mcc_id: Optional[str] = None

@app.post("/agency/publish")
async def publish(req: PublishRequest, user: str = Depends(require_auth)):
    tasks = []
    if "facebook" in req.platforms and req.fb_page_id and req.fb_token:
        tasks.append(publish_facebook(req.text, req.fb_page_id, req.fb_token))
    if "instagram" in req.platforms and req.ig_user_id and req.ig_token and req.image_url:
        tasks.append(publish_instagram(req.text, req.image_url, req.ig_user_id, req.ig_token))
    if "twitter" in req.platforms and req.tw_api_key:
        tasks.append(publish_twitter(req.text, "", req.tw_api_key, req.tw_api_secret or "",
                                     req.tw_access_token or "", req.tw_access_secret or ""))
    if "webhook" in req.platforms and req.webhook_url:
        tasks.append(publish_webhook({"text": req.text, "image_url": req.image_url}, req.webhook_url))
    if ("google" in req.platforms and req.google_developer_token
            and req.google_customer_id and req.google_refresh_token):
        tasks.append(publish_google_ads(
            req.text,
            req.google_developer_token,
            req.google_customer_id,
            req.google_refresh_token,
            req.google_final_url or "",
            req.google_budget or "20",
            req.google_mcc_id or "",
        ))
    if not tasks:
        return {"results": [], "error": "Nenhuma plataforma configurada."}
    results = await asyncio.gather(*tasks)
    return {"results": [r.__dict__ for r in results]}


# ── Metrics ───────────────────────────────────────────────────

class MetricsFetchRequest(BaseModel):
    posts: list[dict]   # [{platform, post_id, token, bearer_token?}]

@app.post("/metrics/fetch")
async def fetch_campaign_metrics(req: MetricsFetchRequest, user: str = Depends(require_auth)):
    tasks = []
    for p in req.posts:
        platform = p.get("platform")
        post_id  = p.get("post_id", "")
        if platform == "facebook":
            tasks.append(fetch_facebook_metrics(post_id, p.get("token", "")))
        elif platform == "instagram":
            tasks.append(fetch_instagram_metrics(post_id, p.get("token", "")))
        elif platform == "twitter":
            tasks.append(fetch_twitter_metrics(post_id, p.get("bearer_token", p.get("token", ""))))
    results = await asyncio.gather(*tasks)
    return {"metrics": [r.__dict__ for r in results]}


# ── Static SPA ────────────────────────────────────────────────

if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"status": "api only"}
