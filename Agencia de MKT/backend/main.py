import os
import asyncio
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from models import ProductInput
from orchestrator import run_agency
from auth import (authenticate, require_auth, require_admin,
                   list_users, add_user, delete_user, update_password, get_user_role)
from db import (init_db, save_campaign, list_campaigns, get_campaign,
                grant_access, revoke_access, get_campaign_grants)
from services.social_publisher import (
    publish_facebook, publish_instagram, publish_twitter, publish_webhook
)

app = FastAPI(title="Maga One Marketing")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

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

@app.post("/auth/users")
async def create_user(req: AddUserRequest, user: str = Depends(require_auth)):
    require_admin(user)
    return add_user(req.username, req.password, req.role)

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
    if not tasks:
        return {"results": [], "error": "Nenhuma plataforma configurada."}
    results = await asyncio.gather(*tasks)
    return {"results": [r.__dict__ for r in results]}


# ── Static SPA ────────────────────────────────────────────────

if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"status": "api only"}
