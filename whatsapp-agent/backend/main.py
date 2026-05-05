from __future__ import annotations
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import config
import database as db
import agent
import calendar_service as cal
import whatsapp_service as wa
import scheduler
import tenant_service as ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start_scheduler()
    logger.info("Agente de Atendimento iniciado")
    yield


app = FastAPI(title="Agente de Atendimento — Multi-Consultório", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_tenant(slug: str) -> dict:
    tenant = db.get_tenant(slug)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Consultório '{slug}' não encontrado.")
    return tenant


async def _handle_message(tenant: dict, phone: str, text: str):
    try:
        reply, resp = agent.process_message(tenant, phone, text)
        await wa.send_message(tenant, phone, reply)
        logger.info(f"[{tenant['slug']}][{phone}] intent={resp.intent} action={resp.action}")
    except Exception as e:
        logger.exception(f"[{tenant['slug']}] Erro ao processar {phone}: {e}")
        await wa.send_message(tenant, phone,
            "Desculpe, tive um problema técnico. Tente novamente em instantes 😊")


# ── Webhooks (por tenant via slug na URL) ──────────────────────────────────────

@app.post("/webhook/{slug}/evolution")
async def webhook_evolution(slug: str, request: Request, bg: BackgroundTasks):
    tenant = _get_tenant(slug)
    payload = await request.json()
    result = wa.extract_message_evolution(payload)
    if not result:
        return {"status": "ignored"}
    phone, text = result
    bg.add_task(_handle_message, tenant, phone, text)
    return {"status": "queued"}


@app.post("/webhook/{slug}/zapi")
async def webhook_zapi(slug: str, request: Request, bg: BackgroundTasks):
    tenant = _get_tenant(slug)
    payload = await request.json()
    result = wa.extract_message_zapi(payload)
    if not result:
        return {"status": "ignored"}
    phone, text = result
    bg.add_task(_handle_message, tenant, phone, text)
    return {"status": "queued"}


@app.post("/webhook/{slug}/twilio")
async def webhook_twilio(slug: str, request: Request, bg: BackgroundTasks):
    tenant = _get_tenant(slug)
    form = await request.form()
    result = wa.extract_message_twilio(dict(form))
    if not result:
        return PlainTextResponse("", status_code=200)
    phone, text = result
    bg.add_task(_handle_message, tenant, phone, text)
    return PlainTextResponse("", status_code=200)


# ── Teste local ────────────────────────────────────────────────────────────────

class TestMessage(BaseModel):
    phone: str
    text: str


@app.post("/test/{slug}/message")
async def test_message(slug: str, msg: TestMessage):
    """Simula uma mensagem sem WhatsApp real — para desenvolvimento."""
    tenant = _get_tenant(slug)
    reply, resp = agent.process_message(tenant, msg.phone, msg.text)
    return {
        "tenant": slug,
        "reply": reply,
        "intent": resp.intent,
        "action": resp.action,
        "data": resp.data,
    }


# ── Admin — Tenants ────────────────────────────────────────────────────────────

class TenantCreate(BaseModel):
    name: str
    psychologist_name: str = "Psicóloga"
    working_hours_start: int = 7
    working_hours_end: int = 21
    session_minutes: int = 50
    slug: Optional[str] = None


class WhatsAppConfig(BaseModel):
    provider: str
    evolution_url: Optional[str] = ""
    evolution_key: Optional[str] = ""
    evolution_instance: Optional[str] = ""
    twilio_sid: Optional[str] = ""
    twilio_token: Optional[str] = ""
    twilio_from: Optional[str] = ""


@app.post("/admin/tenants", status_code=201)
def create_tenant(body: TenantCreate):
    try:
        tenant = ts.create_tenant(**body.model_dump(exclude_none=True))
        return {"slug": tenant["slug"], "id": tenant["id"], "name": tenant["name"]}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/admin/tenants")
def list_tenants():
    tenants = db.list_tenants()
    return {"tenants": [{"slug": t["slug"], "name": t["name"], "id": t["id"]} for t in tenants]}


@app.get("/admin/tenants/{slug}")
def get_tenant(slug: str):
    t = _get_tenant(slug)
    safe = {k: v for k, v in t.items() if k not in ("twilio_token", "evolution_key")}
    return safe


@app.patch("/admin/tenants/{slug}/whatsapp")
def configure_whatsapp(slug: str, body: WhatsAppConfig):
    _get_tenant(slug)
    updated = ts.configure_whatsapp(slug, body.provider, **body.model_dump(exclude={"provider"}, exclude_none=True))
    return {"status": "updated", "provider": updated["whatsapp_provider"]}


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    psychologist_name: Optional[str] = None
    working_hours_start: Optional[int] = None
    working_hours_end: Optional[int] = None
    session_minutes: Optional[int] = None


@app.patch("/admin/tenants/{slug}")
def update_tenant(slug: str, body: TenantUpdate):
    _get_tenant(slug)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar.")
    db.update_tenant(slug, **fields)
    return db.get_tenant(slug)


class ZAPIConfig(BaseModel):
    instance_id: str
    token: str
    client_token: Optional[str] = ""


@app.patch("/admin/tenants/{slug}/zapi")
def configure_zapi(slug: str, body: ZAPIConfig):
    """Configura Z-API para o consultório."""
    _get_tenant(slug)
    ts.configure_zapi(slug, body.instance_id, body.token, body.client_token or "")
    return {
        "status": "configured",
        "provider": "zapi",
        "webhook_url": f"https://agente-atendimento-production.up.railway.app/webhook/{slug}/zapi",
    }


# ── Admin — Agenda ─────────────────────────────────────────────────────────────

@app.get("/admin/{slug}/slots")
def list_slots(slug: str, days: int = 7):
    tenant = _get_tenant(slug)
    slots = cal.get_available_slots(tenant, days_ahead=days, limit=20)
    return {"tenant": slug, "slots": cal.format_slots(slots)}


@app.get("/admin/{slug}/appointments")
def list_appointments(slug: str):
    tenant = _get_tenant(slug)
    now = datetime.now().isoformat()
    far = datetime.now().replace(year=datetime.now().year + 1).isoformat()
    appts = db.get_appointments_in_range(tenant["id"], now, far)
    return {"tenant": slug, "appointments": appts}


@app.get("/admin/{slug}/conversation/{phone}")
def get_conversation(slug: str, phone: str):
    tenant = _get_tenant(slug)
    history = db.get_conversation_history(tenant["id"], phone, limit=50)
    return {"tenant": slug, "phone": phone, "history": history}


@app.delete("/admin/{slug}/conversation/{phone}")
def clear_conversation(slug: str, phone: str):
    tenant = _get_tenant(slug)
    db.clear_conversation(tenant["id"], phone)
    return {"status": "cleared"}


# ── Dashboard ──────────────────────────────────────────────────────────────────

def _get_tenant_by_token(token: str) -> dict:
    tenant = db.get_tenant_by_token(token)
    if not tenant:
        raise HTTPException(status_code=403, detail="Acesso negado.")
    return tenant


@app.get("/dashboard/{slug}", response_class=HTMLResponse)
def dashboard(slug: str, request: Request, token: str = ""):
    tenant = _get_tenant(slug)
    if not tenant.get("dashboard_token") or tenant["dashboard_token"] != token:
        raise HTTPException(status_code=403, detail="Token inválido.")
    return templates.TemplateResponse("dashboard.html", {"request": request, "tenant": tenant, "token": token})


@app.get("/dashboard/api/appointments")
def dash_appointments(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    now = datetime.now().isoformat()
    far = datetime.now().replace(year=datetime.now().year + 1).isoformat()
    appts = db.get_appointments_in_range(tenant["id"], now, far)
    return {"appointments": appts}


@app.post("/dashboard/api/appointments/{appt_id}/confirm")
def dash_confirm(appt_id: int, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.confirm_appointment(tenant["id"], appt_id)
    return {"status": "confirmed"}


@app.delete("/dashboard/api/appointments/{appt_id}/cancel")
def dash_cancel(appt_id: int, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM appointments WHERE id = ? AND tenant_id = ?", (appt_id, tenant["id"]))
    return {"status": "cancelled"}


@app.get("/dashboard/api/slots")
def dash_slots(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    slots = cal.get_available_slots(tenant, days_ahead=7, limit=20)
    return {"slots": cal.format_slots(slots)}


@app.get("/dashboard/api/patients")
def dash_patients(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT c.phone,
               (SELECT a.patient_name FROM appointments a
                WHERE a.phone = c.phone AND a.tenant_id = c.tenant_id LIMIT 1) as name
               FROM conversations c WHERE c.tenant_id = ? ORDER BY c.created_at DESC""",
            (tenant["id"],)
        ).fetchall()
    return {"patients": [dict(r) for r in rows]}


@app.get("/dashboard/api/conversation/{phone}")
def dash_conversation(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    history = db.get_conversation_history(tenant["id"], phone, limit=50)
    return {"history": history}


@app.patch("/dashboard/api/config")
def dash_config(request: Request, body: TenantUpdate):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    fields = body.model_dump(exclude_none=True)
    if fields:
        db.update_tenant(tenant["slug"], **fields)
    return {"status": "updated"}


@app.post("/admin/tenants/{slug}/dashboard-token")
def generate_dashboard_token(slug: str):
    """Gera ou regenera o token de acesso ao painel."""
    _get_tenant(slug)
    token = secrets.token_urlsafe(24)
    db.update_tenant(slug, dashboard_token=token)
    base = "https://agente-atendimento-production.up.railway.app"
    return {
        "token": token,
        "url": f"{base}/dashboard/{slug}?token={token}",
    }


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    tenants = db.list_tenants()
    return {"status": "ok", "tenants_active": len(tenants)}
