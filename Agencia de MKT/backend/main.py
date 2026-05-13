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
                save_credential, get_credentials, delete_platform_credentials,
                create_scheduled_post, list_scheduled_posts,
                get_pending_posts, update_post_status, cancel_scheduled_post,
                list_alert_rules, create_alert_rule, delete_alert_rule,
                get_all_active_alert_rules, create_notification,
                list_notifications, mark_notifications_read, count_unread,
                get_client_stats)
from services.social_publisher import (
    publish_facebook, publish_instagram, publish_twitter, publish_webhook,
    publish_google_ads, toggle_google_ads_campaign,
)
from services.metrics_fetcher import (
    fetch_facebook_metrics, fetch_instagram_metrics, fetch_twitter_metrics,
    fetch_google_ads_campaigns, fetch_facebook_ad_insights, fetch_tiktok_insights,
)

app = FastAPI(title="Maga One Marketing")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ── Scheduled Posts Background Worker ────────────────────────
async def _scheduled_worker():
    """Runs every 60s, publishes posts whose scheduled_at has passed."""
    from datetime import datetime, timezone
    await asyncio.sleep(10)   # brief startup delay
    while True:
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
            posts = get_pending_posts(now)
            for post in posts:
                update_post_status(post["id"], "publishing")
                tasks = []
                creds = post["creds"]
                platforms = post["platforms"]
                text  = post["text"]
                img   = post.get("image_url", "")
                if "facebook" in platforms and creds.get("fb_page_id") and creds.get("fb_token"):
                    tasks.append(publish_facebook(text, creds["fb_page_id"], creds["fb_token"]))
                if "instagram" in platforms and creds.get("ig_user_id") and creds.get("ig_token") and img:
                    tasks.append(publish_instagram(text, img, creds["ig_user_id"], creds["ig_token"]))
                if "twitter" in platforms and creds.get("tw_api_key"):
                    tasks.append(publish_twitter(text, "", creds["tw_api_key"],
                                                  creds.get("tw_api_secret",""),
                                                  creds.get("tw_access_token",""),
                                                  creds.get("tw_access_secret","")))
                if "webhook" in platforms and creds.get("webhook_url"):
                    tasks.append(publish_webhook({"text": text, "image_url": img}, creds["webhook_url"]))
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    result_data = [r.__dict__ if hasattr(r, "__dict__") else {"error": str(r)} for r in results]
                    all_ok = all(r.get("success") for r in result_data if isinstance(r, dict))
                    update_post_status(post["id"], "published" if all_ok else "failed", {"results": result_data})
                else:
                    update_post_status(post["id"], "failed", {"error": "Nenhuma plataforma configurada"})
        except Exception as e:
            print(f"[scheduler] erro: {e}", flush=True)
        await asyncio.sleep(60)


async def _alert_worker():
    """Runs every hour, checks alert rules against live Google Ads data."""
    import smtplib, email.mime.text as _emt
    await asyncio.sleep(300)   # 5 min after startup
    while True:
        try:
            rules = get_all_active_alert_rules()
            # Group by owner to batch API calls per user
            owners: dict[str, list] = {}
            for r in rules:
                owners.setdefault(r["owner"], []).append(r)

            for owner, owner_rules in owners.items():
                creds = get_credentials(owner)
                gc = creds.get("google", {})
                if not all([gc.get("google_developer_token"), gc.get("google_customer_id"), gc.get("google_refresh_token")]):
                    continue
                campaigns = await fetch_google_ads_campaigns(
                    gc["google_developer_token"], gc["google_customer_id"],
                    gc["google_refresh_token"],
                    os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", ""),
                    gc.get("google_mcc_id", ""), "LAST_7_DAYS",
                )
                if not campaigns or (len(campaigns) == 1 and campaigns[0].error):
                    continue

                for rule in owner_rules:
                    metric = rule["metric"]
                    cond   = rule["condition"]
                    thresh = float(rule["threshold"])
                    triggered = []
                    for c in campaigns:
                        val = getattr(c, metric, None)
                        if val is None: continue
                        if (cond == "<" and float(val) < thresh) or (cond == ">" and float(val) > thresh):
                            triggered.append(f"{c.campaign_name}: {metric}={val}")
                    if triggered:
                        label = rule.get("label") or metric
                        msg = f"⚠ Alerta '{label}': {'; '.join(triggered[:3])}"
                        create_notification(owner, msg, "warning")
                        # Optional email
                        smtp_host = os.getenv("SMTP_HOST", "")
                        smtp_user = os.getenv("SMTP_USER", "")
                        smtp_pass = os.getenv("SMTP_PASS", "")
                        if smtp_host and smtp_user and owner and "@" in owner:
                            try:
                                m = _emt.MIMEText(msg)
                                m["Subject"] = f"[Maga One] Alerta de Performance"
                                m["From"] = smtp_user; m["To"] = owner
                                with smtplib.SMTP_SSL(smtp_host, int(os.getenv("SMTP_PORT", "465"))) as s:
                                    s.login(smtp_user, smtp_pass); s.send_message(m)
                            except Exception as e:
                                print(f"[alerts] email error: {e}", flush=True)
        except Exception as e:
            print(f"[alert_worker] erro: {e}", flush=True)
        await asyncio.sleep(3600)   # check every hour


@app.on_event("startup")
async def startup():
    asyncio.create_task(_scheduled_worker())
    asyncio.create_task(_alert_worker())


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


# ── Alerts ───────────────────────────────────────────────────

class AlertRuleRequest(BaseModel):
    platform:  str   = "google"
    metric:    str
    condition: str
    threshold: float
    label:     str   = ""

@app.get("/alerts")
async def get_alerts(user: str = Depends(require_auth)):
    return list_alert_rules(user)

@app.post("/alerts")
async def add_alert(req: AlertRuleRequest, user: str = Depends(require_auth)):
    rid = create_alert_rule(user, req.platform, req.metric, req.condition, req.threshold, req.label)
    return {"id": rid}

@app.delete("/alerts/{rule_id}")
async def remove_alert(rule_id: int, user: str = Depends(require_auth)):
    delete_alert_rule(rule_id, user)
    return {"ok": True}


# ── Notifications ─────────────────────────────────────────────

@app.get("/notifications")
async def get_notifications(unread_only: bool = False, user: str = Depends(require_auth)):
    return {
        "notifications": list_notifications(user, unread_only),
        "unread": count_unread(user),
    }

@app.post("/notifications/read")
async def read_notifications(user: str = Depends(require_auth)):
    mark_notifications_read(user)
    return {"ok": True}


# ── Client Stats (admin) ──────────────────────────────────────

@app.get("/admin/clients")
async def admin_clients(user: str = Depends(require_auth)):
    require_admin(user)
    return get_client_stats()


# ── Schedule ─────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    text: str
    image_url: Optional[str] = None
    platforms: list[str]
    scheduled_at: str          # ISO datetime, e.g. "2025-05-13T14:30"
    # credentials snapshot
    fb_page_id: Optional[str] = None
    fb_token: Optional[str] = None
    ig_user_id: Optional[str] = None
    ig_token: Optional[str] = None
    tw_api_key: Optional[str] = None
    tw_api_secret: Optional[str] = None
    tw_access_token: Optional[str] = None
    tw_access_secret: Optional[str] = None
    webhook_url: Optional[str] = None

@app.post("/schedule")
async def create_schedule(req: ScheduleRequest, user: str = Depends(require_auth)):
    creds = {
        "fb_page_id": req.fb_page_id, "fb_token": req.fb_token,
        "ig_user_id": req.ig_user_id, "ig_token": req.ig_token,
        "tw_api_key": req.tw_api_key, "tw_api_secret": req.tw_api_secret,
        "tw_access_token": req.tw_access_token, "tw_access_secret": req.tw_access_secret,
        "webhook_url": req.webhook_url,
    }
    pid = create_scheduled_post(user, req.text, req.image_url or "",
                                req.platforms, creds, req.scheduled_at)
    return {"id": pid, "scheduled_at": req.scheduled_at}

@app.get("/schedule")
async def get_schedule(user: str = Depends(require_auth)):
    is_admin = get_user_role(user) == "admin"
    return list_scheduled_posts(user, is_admin)

@app.delete("/schedule/{post_id}")
async def delete_schedule(post_id: int, user: str = Depends(require_auth)):
    is_admin = get_user_role(user) == "admin"
    ok = cancel_scheduled_post(post_id, user, is_admin)
    if not ok:
        raise HTTPException(404, detail="Post não encontrado ou já publicado.")
    return {"ok": True}


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
    google_keywords: Optional[str] = None   # comma-separated keywords
    google_location_id: Optional[str] = "2076"  # default: Brazil

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
        kws = [k.strip() for k in (req.google_keywords or "").split(",") if k.strip()]
        tasks.append(publish_google_ads(
            req.text,
            req.google_developer_token,
            req.google_customer_id,
            req.google_refresh_token,
            req.google_final_url or "",
            req.google_budget or "20",
            req.google_mcc_id or "",
            keywords=kws,
            location_id=req.google_location_id or "2076",
        ))
    if not tasks:
        return {"results": [], "error": "Nenhuma plataforma configurada."}
    results = await asyncio.gather(*tasks)
    return {"results": [r.__dict__ for r in results]}


# ── Reports ───────────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    campaigns: list[dict]   # campaign rows from /reports/google-ads
    platform:  str = "google"

@app.post("/reports/optimize")
async def optimize_campaigns(req: OptimizeRequest, user: str = Depends(require_auth)):
    """Single lightweight AI call to generate optimization suggestions from campaign data."""
    from openai import AsyncOpenAI
    if not req.campaigns:
        raise HTTPException(400, detail="Nenhuma campanha para analisar.")

    # Build compact summary — avoid sending full raw data
    lines = []
    for c in req.campaigns[:10]:   # max 10 campaigns
        if c.get("error"):
            continue
        lines.append(
            f"- {c.get('campaign_name','?')[:40]} | status={c.get('status','?')} "
            f"| impressões={c.get('impressions',0)} | cliques={c.get('clicks',0)} "
            f"| CTR={c.get('ctr',0)}% | CPC=R${c.get('avg_cpc',0)} "
            f"| gasto=R${c.get('cost',0)} | conversões={c.get('conversions',0)}"
        )
    if not lines:
        raise HTTPException(400, detail="Sem dados válidos para analisar.")

    summary = "\n".join(lines)
    prompt = (
        f"Analise estas campanhas Google Ads e dê 5 sugestões práticas de otimização em PT-BR. "
        f"Seja direto, uma linha por sugestão com emoji. Dados:\n{summary}"
    )

    groq = AsyncOpenAI(
        api_key=os.getenv("GROQ_API_KEY", ""),
        base_url="https://api.groq.com/openai/v1",
    )
    resp = await groq.chat.completions.create(
        model="llama-3.1-8b-instant",
        max_tokens=300,
        messages=[
            {"role": "system", "content": "Você é um especialista em Google Ads. Responda em PT-BR, conciso."},
            {"role": "user",   "content": prompt},
        ],
    )
    suggestions = resp.choices[0].message.content or "Sem sugestões geradas."
    return {"suggestions": suggestions, "tokens_used": resp.usage.total_tokens if resp.usage else 0}



class CampaignStatusRequest(BaseModel):
    campaign_id: str
    new_status:  str   # "ENABLED" or "PAUSED"

@app.post("/reports/google-ads/toggle")
async def toggle_google_campaign(req: CampaignStatusRequest, user: str = Depends(require_auth)):
    """Pause or activate a Google Ads campaign."""
    creds = get_credentials(user)
    gc = creds.get("google", {})
    dev_token     = gc.get("google_developer_token", "")
    customer_id   = gc.get("google_customer_id", "")
    refresh_token = gc.get("google_refresh_token", "")
    mcc_id        = gc.get("google_mcc_id", "")
    if not all([dev_token, customer_id, refresh_token]):
        raise HTTPException(400, detail="Credenciais Google Ads incompletas.")
    result = await toggle_google_ads_campaign(
        campaign_id=req.campaign_id,
        new_status=req.new_status,
        developer_token=dev_token,
        customer_id=customer_id,
        refresh_token=refresh_token,
        client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        mcc_id=mcc_id,
    )
    if not result.get("success"):
        raise HTTPException(400, detail=result.get("error", "Erro ao alterar status"))
    return result


@app.get("/reports/google-ads")
async def report_google_ads(
    date_range: str = "LAST_30_DAYS",
    user: str = Depends(require_auth),
):
    """Fetch campaign performance from Google Ads API for the authenticated user."""
    creds = get_credentials(user)
    gc = creds.get("google", {})
    dev_token    = gc.get("google_developer_token", "")
    customer_id  = gc.get("google_customer_id", "")
    refresh_token = gc.get("google_refresh_token", "")
    mcc_id       = gc.get("google_mcc_id", "")
    if not all([dev_token, customer_id, refresh_token]):
        raise HTTPException(400, detail="Credenciais Google Ads incompletas.")
    results = await fetch_google_ads_campaigns(
        developer_token=dev_token,
        customer_id=customer_id,
        refresh_token=refresh_token,
        client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        mcc_id=mcc_id,
        date_range=date_range,
    )
    return {"campaigns": [r.__dict__ for r in results]}


@app.get("/reports/facebook")
async def report_facebook(
    date_preset: str = "last_30d",
    user: str = Depends(require_auth),
):
    """Fetch Facebook Page insights for the authenticated user."""
    creds = get_credentials(user)
    fc = creds.get("facebook", {})
    page_id = fc.get("fb_page_id", "")
    token   = fc.get("fb_token", "")
    if not page_id or not token:
        raise HTTPException(400, detail="Credenciais Facebook incompletas.")
    results = await fetch_facebook_ad_insights(page_id, token, date_preset)
    return {"insights": results}


@app.get("/reports/tiktok")
async def report_tiktok(
    date_range: str = "LAST_30_DAYS",
    user: str = Depends(require_auth),
):
    """Fetch TikTok Ads campaign performance for the authenticated user."""
    creds = get_credentials(user)
    tc = creds.get("tiktok", {})
    access_token  = tc.get("tiktok_access_token", "")
    advertiser_id = tc.get("tiktok_advertiser_id", "")
    if not access_token or not advertiser_id:
        raise HTTPException(400, detail="Credenciais TikTok incompletas. Configure em Credenciais → TikTok.")
    results = await fetch_tiktok_insights(access_token, advertiser_id, date_range)
    return {"campaigns": results}


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
