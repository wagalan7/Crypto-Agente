from __future__ import annotations
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import events
import config
import database as db
import agent
import calendar_service as cal
import whatsapp_service as wa
import scheduler
import tenant_service as ts
import google_calendar_service as gcal
import stripe_service as stripe_svc
import mp_service as mp_svc

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
    # Verificar se o agente está pausado para este contato
    if db.is_agent_paused(tenant["id"], phone):
        logger.info(f"[{tenant['slug']}][{phone}] Agente pausado — mensagem ignorada")
        return

    # ── Áudio: transcrever antes de processar ────────────────────────────────────
    if text.startswith("__AUDIO__:"):
        audio_url = text[len("__AUDIO__:"):]
        logger.info(f"[{tenant['slug']}][{phone}] Áudio recebido — transcrevendo...")
        transcribed = await wa.transcribe_audio_groq(audio_url)
        if transcribed:
            text = transcribed
            logger.info(f"[{tenant['slug']}][{phone}] Transcrição: {text[:80]}")
        else:
            # Sem Groq configurado ou falha — pedir para o paciente digitar
            await wa.send_message(tenant, phone,
                "Recebi seu áudio! 🎙️ Mas ainda não consigo ouvir mensagens de voz. "
                "Pode me enviar a mesma mensagem em texto? Assim posso te ajudar melhor 😊")
            return

    try:
        reply, resp, event = agent.process_message(tenant, phone, text)
        await wa.send_message(tenant, phone, reply)
        logger.info(f"[{tenant['slug']}][{phone}] intent={resp.intent} action={resp.action}")
        if event:
            await events.publish(tenant["id"], event["type"], event["data"])
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

    # ── LOG COMPLETO para debug (remover depois) ─────────────────────────────────
    logger.info(f"[{slug}] ZAPI RAW payload: fromMe={payload.get('fromMe')} type={payload.get('type')} phone={payload.get('phone')} text={payload.get('text')} keys={list(payload.keys())}")

    # ── Mensagem da própria psicóloga (fromMe) → pausar/retomar agente ──────────
    self_result = wa.extract_selfmessage_zapi(payload)
    if self_result:
        phone, text, msg_id = self_result
        cmd = text.strip().lower()
        if cmd in ("retomar", "!retomar", "/retomar"):
            db.resume_agent(tenant["id"], phone)
            logger.info(f"[{tenant['slug']}] Agente retomado para {phone} via WhatsApp")
            # Deletar "retomar" do chat para não aparecer ao paciente
            bg.add_task(wa.delete_message_zapi, tenant, phone, msg_id)
        else:
            db.pause_agent(tenant["id"], phone)
            logger.info(f"[{tenant['slug']}] Agente pausado para {phone} — resposta manual detectada")
        return {"status": "self_message"}

    # ── Mensagem do paciente → processar normalmente ─────────────────────────────
    result = wa.extract_message_zapi(payload)
    if not result:
        return {"status": "ignored"}
    phone, text = result
    bg.add_task(_handle_message, tenant, phone, text)
    return {"status": "queued"}


@app.post("/webhook/{slug}/zapi/sent")
async def webhook_zapi_sent(slug: str, request: Request, bg: BackgroundTasks):
    """
    Endpoint exclusivo para o webhook 'Ao enviar' do Z-API.
    Qualquer mensagem aqui é enviada pela psicóloga — pausar o agente.
    """
    try:
        tenant = _get_tenant(slug)
        payload = await request.json()
        logger.info(f"[{slug}] ZAPI SENT payload: {payload}")

        phone = payload.get("phone", "").replace("+", "").replace("-", "")
        text = (payload.get("text") or {}).get("message", "") or ""
        msg_id = payload.get("zaapId") or payload.get("messageId") or payload.get("id") or ""

        if not phone:
            return {"status": "ignored"}

        cmd = text.strip().lower()
        if cmd in ("retomar", "!retomar", "/retomar"):
            db.resume_agent(tenant["id"], phone)
            logger.info(f"[{slug}] Agente retomado para {phone} via /sent")
            if msg_id:
                bg.add_task(wa.delete_message_zapi, tenant, phone, msg_id)
        else:
            db.pause_agent(tenant["id"], phone)
            logger.info(f"[{slug}] Agente pausado para {phone} via /sent — mensagem manual: {text[:50]}")

        return {"status": "self_message"}
    except Exception as e:
        logger.error(f"[{slug}] Erro em webhook_zapi_sent: {e}")
        return {"status": "error"}


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
    reply, resp, event = agent.process_message(tenant, msg.phone, msg.text)
    if event:
        await events.publish(tenant["id"], event["type"], event["data"])
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


@app.patch("/admin/tenants/{slug}/rename")
def rename_tenant(slug: str, new_slug: str):
    _get_tenant(slug)
    if db.get_tenant(new_slug):
        raise HTTPException(status_code=409, detail=f"Slug '{new_slug}' já está em uso.")
    with db.get_conn() as conn:
        conn.execute("UPDATE tenants SET slug = ? WHERE slug = ?", (new_slug, slug))
    return {"status": "renamed", "old_slug": slug, "new_slug": new_slug}


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


@app.get("/dashboard/stream/{slug}")
async def dashboard_stream(slug: str, token: str = ""):
    tenant = _get_tenant(slug)
    if not tenant.get("dashboard_token") or tenant["dashboard_token"] != token:
        raise HTTPException(status_code=403, detail="Token inválido.")
    return StreamingResponse(
        events.subscribe(tenant["id"]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


class ManualAppointment(BaseModel):
    patient_name: str
    phone: str
    scheduled_at: str  # ISO: "2025-05-10T14:00:00"


@app.post("/dashboard/api/appointments", status_code=201)
def dash_create_appointment(body: ManualAppointment, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    try:
        from datetime import datetime as dt
        scheduled = dt.fromisoformat(body.scheduled_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Data/hora inválida. Use formato ISO: 2025-05-10T14:00:00")

    if db.is_slot_taken(tenant["id"], scheduled):
        raise HTTPException(status_code=409, detail="Já existe uma consulta neste horário.")

    phone = body.phone.strip().replace(" ", "").replace("-", "")
    appt_id = db.create_appointment(tenant["id"], body.patient_name, phone, scheduled)

    # Sincronizar com Google Calendar
    try:
        event_id = gcal.create_event(tenant, body.patient_name, scheduled.isoformat(), tenant.get("session_minutes", 50))
        if event_id:
            db.set_appointment_google_event_id(appt_id, event_id)
    except Exception:
        pass

    return {"status": "created", "id": appt_id}


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
    # Remover do Google Calendar antes de deletar do banco
    appt = db.get_appointment_by_id(tenant["id"], appt_id)
    if appt and appt.get("google_event_id"):
        try:
            gcal.delete_event(tenant, appt["google_event_id"])
        except Exception:
            pass
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


@app.post("/dashboard/api/conversation/{phone}/pause")
def dash_pause(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.pause_agent(tenant["id"], phone)
    return {"status": "paused", "phone": phone}


@app.post("/dashboard/api/conversation/{phone}/resume")
def dash_resume(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.resume_agent(tenant["id"], phone)
    return {"status": "resumed", "phone": phone}


@app.get("/dashboard/api/paused")
def dash_paused(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    return {"paused": db.list_paused_phones(tenant["id"])}


# ── Painel Mobile de Controle ──────────────────────────────────────────────────

@app.get("/controle/{token}", response_class=HTMLResponse)
def controle_mobile(token: str, request: Request):
    """Painel leve para pausar/retomar o agente direto do celular."""
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT phone, patient_name FROM appointments
               WHERE tenant_id = ? ORDER BY patient_name""",
            (tenant["id"],)
        ).fetchall()
    patients = [dict(r) for r in rows]
    paused = set(db.list_paused_phones(tenant["id"]))
    return templates.TemplateResponse("controle.html", {
        "request": request,
        "tenant": tenant,
        "token": token,
        "patients": patients,
        "paused": paused,
    })


@app.post("/controle/{token}/pausar/{phone}", response_class=HTMLResponse)
def controle_pausar(token: str, phone: str):
    tenant = _get_tenant_by_token(token)
    db.pause_agent(tenant["id"], phone)
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/retomar/{phone}", response_class=HTMLResponse)
def controle_retomar(token: str, phone: str):
    tenant = _get_tenant_by_token(token)
    db.resume_agent(tenant["id"], phone)
    return RedirectResponse(f"/controle/{token}", status_code=303)


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


@app.post("/admin/confirmacoes/disparar")
async def disparar_confirmacoes():
    """Dispara confirmações de amanhã agora mesmo (ignora restrição de horário)."""
    results = await scheduler.run_confirmations_now()
    return {"enviados": len([r for r in results if r["sent"]]), "detalhes": results}


@app.post("/admin/tenants/{slug}/dashboard-token")
def generate_dashboard_token(slug: str):
    """Gera ou regenera o token de acesso ao painel."""
    _get_tenant(slug)
    token = secrets.token_urlsafe(24)
    db.update_tenant(slug, dashboard_token=token)
    base = config.BASE_URL
    return {
        "token": token,
        "dashboard_url": f"{base}/dashboard/{slug}?token={token}",
        "controle_url": f"{base}/controle/{token}",
    }


# ── Onboarding público ─────────────────────────────────────────────────────────

@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request):
    return templates.TemplateResponse("onboarding.html", {"request": request})


class OnboardingCreate(BaseModel):
    name: str
    psychologist_name: str = "Psicóloga"
    email: str = ""
    working_hours_start: int = 8
    working_hours_end: int = 18
    session_minutes: int = 50


@app.post("/onboarding/create", status_code=201)
def onboarding_create(body: OnboardingCreate):
    try:
        tenant = ts.create_tenant(
            name=body.name,
            psychologist_name=body.psychologist_name,
            working_hours_start=body.working_hours_start,
            working_hours_end=body.working_hours_end,
            session_minutes=body.session_minutes,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    slug = tenant["slug"]

    # Gerar tokens e salvar email
    dash_token = secrets.token_urlsafe(24)
    setup_token = secrets.token_urlsafe(24)
    db.update_tenant(slug, dashboard_token=dash_token, setup_token=setup_token,
                     email=body.email, status="pending_payment")

    return {"slug": slug, "setup_token": setup_token}


@app.get("/onboarding/sucesso", response_class=HTMLResponse)
def onboarding_sucesso(request: Request):
    return templates.TemplateResponse("onboarding_success.html", {"request": request})


@app.get("/onboarding/info")
def onboarding_info(setup_token: str):
    tenant = db.get_tenant_by_setup_token(setup_token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Token inválido.")
    slug = tenant["slug"]
    dash_token = tenant.get("dashboard_token", "")
    base = config.BASE_URL
    return {
        "name": tenant["name"],
        "slug": slug,
        "dashboard_url": f"{base}/dashboard/{slug}?token={dash_token}" if dash_token else "",
        "controle_url": f"{base}/controle/{dash_token}" if dash_token else "",
        "webhook_url": f"{base}/webhook/{slug}/zapi",
    }


# ── Painel Master ──────────────────────────────────────────────────────────────

def _check_master_key(key: str):
    if not config.MASTER_KEY or key != config.MASTER_KEY:
        raise HTTPException(status_code=403, detail="Acesso negado.")


@app.get("/master", response_class=HTMLResponse)
def master_panel(request: Request, key: str = ""):
    _check_master_key(key)
    return templates.TemplateResponse("master.html", {"request": request, "master_key": key})


@app.get("/master/tenants")
def master_list_tenants(key: str = ""):
    _check_master_key(key)
    tenants = db.list_tenants()
    base = config.BASE_URL
    result = []
    for t in tenants:
        dash_token = t.get("dashboard_token", "")
        result.append({
            "id": t["id"],
            "slug": t["slug"],
            "name": t["name"],
            "psychologist_name": t["psychologist_name"],
            "working_hours_start": t["working_hours_start"],
            "working_hours_end": t["working_hours_end"],
            "session_minutes": t["session_minutes"],
            "whatsapp_provider": t["whatsapp_provider"],
            "dashboard_url": f"{base}/dashboard/{t['slug']}?token={dash_token}" if dash_token else "",
        })
    return {"tenants": result}


@app.get("/master/onboarding-link/{slug}")
def master_onboarding_link(slug: str, key: str = ""):
    _check_master_key(key)
    tenant = _get_tenant(slug)
    setup_token = tenant.get("setup_token", "")
    if not setup_token:
        # Gerar se não existir
        setup_token = secrets.token_urlsafe(24)
        db.update_tenant(slug, setup_token=setup_token)
    base = config.BASE_URL
    return {"url": f"{base}/onboarding/sucesso?token={setup_token}"}


# ── Google Calendar OAuth ──────────────────────────────────────────────────────

@app.get("/google/auth/{slug}")
def google_auth(slug: str, token: str = ""):
    """Inicia o fluxo OAuth2 do Google Calendar para o consultório."""
    tenant = _get_tenant(slug)
    if not tenant.get("dashboard_token") or tenant["dashboard_token"] != token:
        raise HTTPException(status_code=403, detail="Token inválido.")
    redirect_uri = f"{config.BASE_URL}/google/callback"
    url = gcal.get_auth_url(slug, redirect_uri)
    return RedirectResponse(url)


@app.get("/google/callback", response_class=HTMLResponse)
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Recebe o callback do Google e salva o refresh_token."""
    if error:
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:60px">
        <h2>❌ Erro ao conectar Google Calendar</h2>
        <p style="color:#666">{error}</p>
        <a href="/" style="color:#E91E8C">Voltar</a>
        </body></html>""")

    redirect_uri = f"{config.BASE_URL}/google/callback"
    success = gcal.exchange_code(state, code, redirect_uri)

    if not success:
        return HTMLResponse("""
        <html><body style="font-family:sans-serif;text-align:center;padding:60px">
        <h2>⚠️ Não foi possível obter o token de atualização</h2>
        <p style="color:#666">Tente desconectar e reconectar sua conta Google.</p>
        </body></html>""")

    tenant = db.get_tenant(state)
    dash_token = tenant.get("dashboard_token", "") if tenant else ""
    dash_url = f"{config.BASE_URL}/dashboard/{state}?token={dash_token}" if tenant else "/"

    return HTMLResponse(f"""
    <html><head><meta http-equiv="refresh" content="3;url={dash_url}"></head>
    <body style="font-family:sans-serif;text-align:center;padding:60px">
    <div style="font-size:48px">📅</div>
    <h2 style="color:#111">Google Calendar conectado!</h2>
    <p style="color:#666">Os agendamentos serão sincronizados automaticamente.<br>
    Redirecionando para o painel...</p>
    </body></html>""")


@app.post("/dashboard/api/google/disconnect")
def google_disconnect(request: Request):
    """Remove a conexão com o Google Calendar."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.update_tenant(tenant["slug"], google_refresh_token="")
    return {"status": "disconnected"}


# ── Pagamento ──────────────────────────────────────────────────────────────────

@app.get("/onboarding/pagamento", response_class=HTMLResponse)
def payment_page(request: Request, token: str = "", cancelled: int = 0):
    tenant = db.get_tenant_by_setup_token(token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Link inválido.")
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "setup_token": token,
        "tenant": tenant,
        "cancelled": bool(cancelled),
    })


@app.post("/checkout/stripe")
async def checkout_stripe(request: Request):
    form = await request.form()
    setup_token = form.get("setup_token", "")
    tenant = db.get_tenant_by_setup_token(setup_token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Token inválido.")
    try:
        url = stripe_svc.create_checkout_session(tenant)
        return RedirectResponse(url, status_code=303)
    except Exception as e:
        logger.exception(f"Stripe checkout error: {e}")
        raise HTTPException(status_code=500, detail="Erro ao iniciar pagamento. Tente novamente.")


@app.post("/webhooks/stripe")
async def webhook_stripe(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        result = stripe_svc.handle_webhook(payload, sig)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/dashboard/api/billing-portal")
def billing_portal(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    # Tentar Stripe primeiro, depois Mercado Pago
    url = stripe_svc.get_billing_portal_url(tenant) or mp_svc.get_manage_url(tenant)
    if not url:
        raise HTTPException(status_code=404, detail="Sem assinatura vinculada.")
    return {"url": url}


@app.post("/checkout/mercadopago")
async def checkout_mercadopago(request: Request):
    form = await request.form()
    setup_token = form.get("setup_token", "")
    tenant = db.get_tenant_by_setup_token(setup_token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Token inválido.")
    try:
        url = mp_svc.create_subscription(tenant)
        return RedirectResponse(url, status_code=303)
    except Exception as e:
        logger.exception(f"MP checkout error: {e}")
        raise HTTPException(status_code=500, detail="Erro ao iniciar pagamento. Tente novamente.")


@app.post("/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    data = await request.json()
    result = mp_svc.handle_webhook(data)
    return result


# ── Landing page ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    tenants = db.list_tenants()
    return {"status": "ok", "tenants_active": len(tenants)}
