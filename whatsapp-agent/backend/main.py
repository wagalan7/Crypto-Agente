from __future__ import annotations
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
import caldav_service as caldav_svc
import email_service as email_svc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Versão atual dos Termos/Política — incrementar quando publicar nova versão.
TERMS_VERSION = "2026-05-18"

# ── Sentry (opcional, ativa se SENTRY_DSN estiver definido) ─────────────────
import os as _os_init
_SENTRY_DSN = _os_init.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=float(_os_init.getenv("SENTRY_TRACES_SAMPLE", "0.05")),
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[FastApiIntegration()],
            environment=_os_init.getenv("RAILWAY_ENVIRONMENT", "production"),
        )
        logger.info("Sentry inicializado")
    except Exception as e:
        logger.warning(f"Falha ao inicializar Sentry: {e}")

# ── Rate limiter manual (em memória, simples, sem dependências externas) ───
import time as _time
from collections import deque as _deque
from threading import Lock as _Lock

_rate_buckets: dict[tuple[str, str], "_deque[float]"] = {}
_rate_lock = _Lock()

# (path_prefix, limit, window_seconds). Primeira correspondência ganha.
_RATE_RULES = [
    ("/painel/login",       10, 60),    # 10 logins/min por IP
    ("/onboarding/create",  5,  60),    # 5 cadastros/min por IP
    ("/webhook/",           120, 60),   # 120 webhooks/min por IP
]


# ── TOTP (RFC 6238) — stdlib, sem dependências ──────────────────────────────
import base64 as _b64
import hmac as _hmac
import hashlib as _hashlib
import struct as _struct
import time as _ttime
import secrets as _secrets

def _b32_normalize(secret: str) -> str:
    s = secret.upper().replace(" ", "").replace("-", "")
    # base32 precisa ser múltiplo de 8 caracteres
    while len(s) % 8 != 0:
        s += "="
    return s

def totp_generate_secret() -> str:
    """Retorna secret base32 de 20 bytes (160 bits)."""
    return _b64.b32encode(_secrets.token_bytes(20)).decode("ascii").rstrip("=")

def totp_now(secret: str, step: int = 30, digits: int = 6, drift: int = 0) -> str:
    counter = int(_ttime.time() // step) + drift
    key = _b64.b32decode(_b32_normalize(secret))
    msg = _struct.pack(">Q", counter)
    h = _hmac.new(key, msg, _hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (_struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)

def totp_verify(secret: str, code: str, window: int = 1) -> bool:
    """Aceita códigos ±window passos (30s cada). Codes devem ter 6 dígitos."""
    if not secret or not code or not code.isdigit() or len(code) != 6:
        return False
    for d in range(-window, window + 1):
        if _hmac.compare_digest(totp_now(secret, drift=d), code):
            return True
    return False

def totp_provisioning_uri(secret: str, account: str, issuer: str = "Agente Consultorio") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


def _client_ip(request: Request) -> str:
    """Usa X-Forwarded-For (Railway edge) ou IP direto."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_rate_limit(ip: str, path: str) -> bool:
    """True se permitido, False se excedeu."""
    for prefix, limit, window in _RATE_RULES:
        if path.startswith(prefix):
            key = (ip, prefix)
            now = _time.time()
            with _rate_lock:
                bucket = _rate_buckets.setdefault(key, _deque())
                while bucket and bucket[0] < now - window:
                    bucket.popleft()
                if len(bucket) >= limit:
                    return False
                bucket.append(now)
            return True
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Backfill: garante webhook_token para todos os tenants existentes
    try:
        for t in db.list_tenants():
            db.ensure_webhook_token(t["id"])
    except Exception as e:
        logger.warning(f"Erro no backfill de webhook_token: {e}")
    # Reconciliação GCal: cria eventos para appointments futuros sem
    # google_event_id (consultas legadas, criadas antes da conexão com
    # o Google Calendar, ou que perderam o ID por algum motivo).
    # Roda 1× a cada boot; é idempotente (só pega quem está sem ID).
    try:
        _reconcile_gcal_events()
    except Exception as e:
        logger.warning(f"Reconciliação GCal falhou: {e}")
    scheduler.start_scheduler()
    logger.info("Agente de Atendimento iniciado")
    yield


def _reconcile_gcal_events() -> None:
    """Para cada tenant com Google Calendar conectado, cria eventos no GCal
    para appointments futuros cujo google_event_id está vazio. Persiste o
    novo event_id no banco. CalDAV recebe o mesmo tratamento se não houver
    GCal configurado."""
    from datetime import datetime as _dt
    now_iso = _dt.now().isoformat()
    total_created = 0
    for t in db.list_tenants():
        has_gcal = bool(t.get("google_refresh_token"))
        has_caldav = bool(t.get("caldav_url")) if not has_gcal else False
        if not (has_gcal or has_caldav):
            continue
        try:
            with db.get_conn() as conn:
                rows = conn.execute(
                    """SELECT id, patient_name, scheduled_at FROM appointments
                       WHERE tenant_id = ?
                         AND (google_event_id IS NULL OR google_event_id = '')
                         AND scheduled_at >= ?
                         AND (cancelled IS NULL OR cancelled = 0)
                         AND COALESCE(attendance, 'pending') != 'missed_with_notice'""",
                    (t["id"], now_iso),
                ).fetchall()
        except Exception as e:
            logger.warning(f"[reconcile] tenant {t.get('slug')}: query falhou: {e}")
            continue
        duration = t.get("session_minutes", 50)
        for r in rows:
            appt_id = r["id"]
            name = r["patient_name"] or "Paciente"
            scheduled_at = r["scheduled_at"]
            try:
                new_id = None
                if has_gcal:
                    new_id = gcal.create_event(t, name, scheduled_at, duration)
                elif has_caldav:
                    new_id = caldav_svc.create_event(t, name, scheduled_at, duration)
                if new_id:
                    db.set_appointment_google_event_id(appt_id, new_id)
                    total_created += 1
                    logger.info(f"[reconcile][{t['slug']}] appt {appt_id} ({name}) → event_id={new_id}")
            except Exception as e:
                logger.warning(f"[reconcile][{t.get('slug')}] appt {appt_id} falhou: {e}")
    if total_created:
        logger.info(f"[reconcile] {total_created} evento(s) criado(s) no calendário externo")


# ── Sentry (observabilidade) ─────────────────────────────────────────────────
if config.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=config.SENTRY_DSN,
            traces_sample_rate=0.05,        # 5% de traces (suficiente p/ diagnóstico, baixo custo)
            profiles_sample_rate=0.0,
            send_default_pii=False,         # LGPD: não enviar PII por padrão
            integrations=[StarletteIntegration(), FastApiIntegration()],
            environment=__import__("os").getenv("RAILWAY_ENVIRONMENT_NAME") or "production",
            release=__import__("os").getenv("RAILWAY_GIT_COMMIT_SHA", "")[:7] or None,
        )
        logger.info("Sentry inicializado")
    except Exception as e:
        logger.warning(f"Sentry init falhou: {e}")


app = FastAPI(title="Agente de Atendimento — Multi-Consultório", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    ip = _client_ip(request)
    if not _check_rate_limit(ip, request.url.path):
        return JSONResponse(
            {"detail": "Muitas requisições. Aguarde e tente novamente."},
            status_code=429,
        )
    return await call_next(request)


# ── Auth de superusuário para /admin/* e /test/* ────────────────────────────────
# Estes endpoints são operações administrativas (CRUD de tenants, leitura/remoção
# de conversas de QUALQUER consultório, simulação de mensagens). Antes estavam
# ABERTOS na internet. Agora exigem a MASTER_KEY (via header X-Master-Key ou ?key=).
# Fail-closed: sem MASTER_KEY configurada, tudo é negado.
# Exceção: /admin/confirmacoes/disparar é ação do dashboard e valida o token do
# painel no próprio endpoint (não a master key).
_MASTER_PROTECTED_PREFIXES = ("/admin/", "/test/")
_MASTER_EXEMPT_PATHS = {"/admin/confirmacoes/disparar"}


def _extract_master_key(request: Request) -> str:
    return (
        request.headers.get("X-Master-Key", "")
        or request.headers.get("X-Master-Token", "")
        or request.query_params.get("key", "")
    )


@app.middleware("http")
async def _admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if (
        request.method != "OPTIONS"
        and path.startswith(_MASTER_PROTECTED_PREFIXES)
        and path not in _MASTER_EXEMPT_PATHS
    ):
        key = _extract_master_key(request)
        if not config.MASTER_KEY or not hmac.compare_digest(key, config.MASTER_KEY):
            return JSONResponse({"detail": "Acesso negado."}, status_code=403)
    return await call_next(request)

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_ALLOWED_ORIGINS = [
    config.BASE_URL,
    "https://agenteconsultorio.com.br",
    "https://www.agenteconsultorio.com.br",
    "https://agente-atendimento-production.up.railway.app",
]
# Suporte a override por env var (CORS_ORIGINS=https://a.com,https://b.com)
import os as _os
_extra = _os.getenv("CORS_ORIGINS", "")
if _extra:
    _ALLOWED_ORIGINS.extend([o.strip() for o in _extra.split(",") if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(set(_ALLOWED_ORIGINS)),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Dashboard-Token", "X-Master-Token"],
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_tenant(slug: str) -> dict:
    tenant = db.get_tenant(slug)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Consultório '{slug}' não encontrado.")
    return tenant


async def _flag_receipt_and_notify(tenant: dict, phone: str, kind: str) -> None:
    """Sinaliza que chegou um comprovante do paciente (imagem/documento/PIX) para
    a psicóloga confirmar o pagamento na tela de Cobranças. NÃO marca como pago —
    a confirmação continua humana. Notifica a psicóloga só na 1ª vez do mês (sem
    spam). Nunca deixa uma falha aqui atrapalhar a resposta ao paciente."""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Zi
        now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
        month = f"{now.year:04d}-{now.month:02d}"
        newly_pending = db.flag_billing_receipt(tenant["id"], phone, month, kind)
        if not newly_pending:
            return
        try:
            pat = db.get_patient(tenant["id"], phone)
        except Exception:
            pat = None
        nome = (pat or {}).get("name") or phone
        psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
        if psy_phone:
            await wa.send_message(
                tenant, psy_phone,
                f"📎 *{nome}* enviou um comprovante de pagamento ({kind}).\n"
                f"Número: {phone}\n"
                f"Confira e, se estiver certo, marque *✓ pago* na tela de Cobranças.",
            )
    except Exception as e:
        logger.warning(f"[{tenant.get('slug')}][{phone}] Falha ao sinalizar comprovante: {e}")


async def _handle_message(tenant: dict, phone: str, text: str):
    phone = "".join(c for c in phone if c.isdigit())  # normaliza sempre

    # ── Verificar se o consultório está ativo (assinatura em dia) ────────────────
    status = tenant.get("status", "active")
    if status == "suspended" and not db.is_tenant_exempt(tenant):
        logger.warning(f"[{tenant['slug']}] Consultório suspenso — mensagem de {phone} ignorada")
        return

    # Verificar se o agente está pausado para este contato
    if db.is_agent_paused(tenant["id"], phone):
        logger.info(f"[{tenant['slug']}][{phone}] Agente pausado — mensagem ignorada")
        return

    # ── Filtro anti-spam (propagandas, links promocionais) ──────────────────────
    # Comportamento:
    # - Primeiro contato spam: responde uma vez, educadamente, e grava no histórico
    # - Spam subsequente do mesmo número: ignora silenciosamente (não responde
    #   nem polui banco). Detectado por presença de marcador "[spam/propaganda]"
    #   no histórico recente.
    # - NÃO pausa o agente permanentemente: se o paciente mandar uma mensagem
    #   legítima depois (sem padrão de spam), o agente responde normalmente.
    if _is_spam(text):
        recent = db.get_conversation_history(tenant["id"], phone, limit=8)
        already_warned = any(
            (m.get("content") or "").startswith("[spam/propaganda]")
            for m in recent
        )
        if not already_warned:
            reply = ("Olá! Aqui é o consultório. Qualquer dúvida sobre sua consulta "
                     "estou à disposição. 🙏")
            try:
                await wa.send_message(tenant, phone, reply)
            except Exception as e:
                logger.warning(f"[{tenant['slug']}][{phone}] Falha ao responder spam: {e}")
            db.save_message(tenant["id"], phone, "user", "[spam/propaganda]")
            db.save_message(tenant["id"], phone, "assistant", reply)
            logger.info(f"[{tenant['slug']}][{phone}] Spam detectado (1ª vez) — resposta educada enviada")
        else:
            logger.info(f"[{tenant['slug']}][{phone}] Spam detectado (repetido) — ignorado silenciosamente")
        return

    # ── Documento (PDF/Word) ou Comprovante PIX nativo → resposta padrão ────────
    if text in ("__DOCUMENTO__", "__COMPROVANTE_PIX__"):
        kind_label = "Comprovante PIX" if text == "__COMPROVANTE_PIX__" else "Documento"
        logger.info(f"[{tenant['slug']}][{phone}] {kind_label} recebido — tratando como comprovante")
        reply = "Obrigada pelo pagamento! 😊 Recebi o comprovante. Em breve enviarei a nota fiscal. Até a sessão!"
        await wa.send_message(tenant, phone, reply)
        db.save_message(tenant["id"], phone, "user", f"[{kind_label.lower()} enviado]")
        db.save_message(tenant["id"], phone, "assistant", reply)
        await _flag_receipt_and_notify(
            tenant, phone, "pix" if text == "__COMPROVANTE_PIX__" else "documento")
        return

    # ── Imagem → analisar por visão antes de assumir comprovante ────────────────
    if text == "__IMAGEM__" or text.startswith("__IMAGEM__:"):
        img_url = text.split(":", 1)[1] if text.startswith("__IMAGEM__:") else ""
        logger.info(f"[{tenant['slug']}][{phone}] Imagem recebida — analisando...")
        kind = await agent.classify_image(img_url) if img_url else "unknown"
        logger.info(f"[{tenant['slug']}][{phone}] Imagem classificada como: {kind}")

        if kind == "receipt":
            reply = "Obrigada pelo pagamento! 😊 Recebi o comprovante. Em breve enviarei a nota fiscal. Até a sessão!"
            db.save_message(tenant["id"], phone, "user", "[comprovante de pagamento enviado]")
            await _flag_receipt_and_notify(tenant, phone, "imagem")
        else:
            reply = ("Recebi sua imagem! 😊 No momento eu (assistente) não consigo analisar "
                     "imagens em detalhe — vou repassar para a psicóloga, que responde "
                     "assim que puder. Se for sobre agenda ou pagamento, pode me contar por texto que te ajudo. 🙏")
            db.save_message(tenant["id"], phone, "user", "[imagem enviada — não-comprovante]")
            # Avisar psicóloga
            psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
            if psy_phone:
                try:
                    await wa.send_message(tenant, psy_phone,
                        f"📷 *Paciente enviou uma imagem que não parece comprovante.*\n"
                        f"Número: {phone}\n"
                        f"Vale dar uma olhada quando puder.")
                except Exception as e:
                    logger.warning(f"[{tenant['slug']}] Falha ao notificar psicóloga sobre imagem: {e}")
        await wa.send_message(tenant, phone, reply)
        db.save_message(tenant["id"], phone, "assistant", reply)
        return

    # ── Urgência / crise → notificar psicóloga imediatamente ────────────────────
    _URGENCY_KEYWORDS = ("preciso de ajuda", "socorro", "não aguento", "nao aguento",
                         "me machucar", "suicídio", "suicidio", "desistir de tudo",
                         "não quero mais", "nao quero mais", "crise", "emergência", "emergencia")
    if any(kw in text.lower() for kw in _URGENCY_KEYWORDS):
        psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
        if psy_phone:
            urgency_notif = (
                f"🚨 *Mensagem urgente de paciente!*\n"
                f"Número: {phone}\n\n"
                f"Mensagem: _{text}_\n\n"
                f"Recomendo entrar em contato o quanto antes."
            )
            await wa.send_message(tenant, psy_phone, urgency_notif)
            logger.info(f"[{tenant['slug']}] ⚠️ Notificação de urgência enviada para psicóloga")

    # ── Áudio: NÃO transcrever mais. Apenas repassar à psicóloga. ───────────────
    # Decisão da Bruna (sigilo + qualidade): a IA não deve interpretar áudio.
    if text.startswith("__AUDIO__:"):
        logger.info(f"[{tenant['slug']}][{phone}] Áudio recebido — repassando à psicóloga sem transcrição")
        psy_name = tenant.get("psychologist_name") or "psicóloga"
        await wa.send_message(
            tenant, phone,
            f"Recebi seu áudio! 🎙️ Vou repassar para a {psy_name}, "
            f"que entra em contato em breve para te responder por aqui. 💖",
        )
        return

    # Detectar se é primeira mensagem de um novo contato (antes de salvar)
    is_first_message = len(db.get_conversation_history(tenant["id"], phone, limit=1)) == 0

    try:
        reply, resp, event = agent.process_message(tenant, phone, text)
        await wa.send_message(tenant, phone, reply)
        logger.info(f"[{tenant['slug']}][{phone}] intent={resp.intent} action={resp.action}")

        # ── Notificar psicóloga quando paciente CANCELAR ─────────────────────────
        if resp.action == "cancel" or resp.intent == "cancel":
            psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
            phone_norm = _norm_phone(phone)
            quando_txt = ""
            nome_txt = ""
            try:
                ev_data = (event or {}).get("data", {}) if event else {}
                nome_txt = ev_data.get("patient_name", "") or ""
            except Exception:
                pass
            # Busca a consulta cancelada (já marcada cancelled=1) para informar a data
            try:
                appts = db.get_appointments_by_phone(tenant["id"], phone_norm) or []
                future_cancel = [a for a in appts if a.get("cancelled")]
                if future_cancel:
                    import calendar_service as _cal
                    from datetime import datetime as _dt
                    last = sorted(future_cancel, key=lambda a: a.get("scheduled_at") or "")[-1]
                    nome_txt = nome_txt or (last.get("patient_name") or "")
                    try:
                        _d = _dt.fromisoformat(last["scheduled_at"])
                        quando_txt = _cal.format_slots([_d])[0]
                    except Exception:
                        quando_txt = ""
            except Exception as e:
                logger.warning(f"[{tenant['slug']}] Falha ao montar notificação de cancelamento: {e}")
            if psy_phone:
                nome_disp = nome_txt or "Paciente"
                linha_quando = f"\nSessão: *{quando_txt}*" if quando_txt else ""
                notif = (
                    f"❌ *Cancelamento de sessão*\n"
                    f"*{nome_disp}* avisou que vai cancelar.{linha_quando}\n"
                    f"Número: {phone_norm}\n\n"
                    f"O horário foi liberado na agenda. Se quiser aplicar a política "
                    f"de cobrança, entre em contato com o(a) paciente. 💖"
                )
                await wa.send_message(tenant, psy_phone, notif)
                logger.info(f"[{tenant['slug']}] Notificação de cancelamento enviada para psicóloga")
            try:
                await events.publish(tenant["id"], "appointment_cancelled",
                                     {"phone": phone, "patient_name": nome_txt})
            except Exception:
                pass

        # ── Notificar psicóloga quando novo paciente entrar em contato ───────────
        elif resp.intent == "new_patient":
            raw_name = resp.data.get("patient_name", "") if resp.data else ""
            # Sanitização: remove caracteres de controle, recorta para 100 chars.
            patient_name = "".join(ch for ch in (raw_name or "") if ch.isprintable()).strip()[:100]
            # Anti-alucinação: o LLM às vezes inventa nome. Só aceita se o primeiro
            # nome aparecer (case-insensitive) no texto do paciente OU no histórico
            # recente. Senão, descarta (vira "novo contato").
            if patient_name:
                first = patient_name.split()[0].lower()
                hay = (text or "").lower()
                if first not in hay:
                    try:
                        recent = db.get_conversation_history(tenant["id"], phone, limit=8)
                        hay_hist = " ".join((m.get("content") or "") for m in recent
                                            if m.get("role") == "user").lower()
                    except Exception:
                        hay_hist = ""
                    if first not in hay_hist:
                        logger.warning(
                            f"[{tenant['slug']}][{phone}] Descartando patient_name='{patient_name}' "
                            f"— não aparece no texto nem no histórico (provável alucinação do LLM)"
                        )
                        patient_name = ""
            psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
            phone_norm = _norm_phone(phone)

            # Decide se notifica: primeira mensagem OU primeira vez que aprendemos o nome
            # (paciente pode revelar o nome só depois de algumas trocas).
            existing = db.get_appointments_by_phone(tenant["id"], phone_norm)
            placeholder_exists = any(
                (a.get("scheduled_at") or "").startswith("2099-") for a in (existing or [])
            )
            name_just_learned = bool(patient_name) and not placeholder_exists
            should_notify = psy_phone and (is_first_message or name_just_learned)

            if should_notify:
                nome_display = patient_name if patient_name else "novo contato"
                notif = (
                    f"🔔 *Novo paciente!*\n"
                    f"*{nome_display}* entrou em contato pelo WhatsApp.\n"
                    f"Número: {phone_norm}\n\n"
                    f"Ele(a) está aguardando seu retorno para conhecer o processo. 😊"
                )
                await wa.send_message(tenant, psy_phone, notif)
                logger.info(f"[{tenant['slug']}] Notificação enviada para psicóloga ({psy_phone})")

            # ── Auto-registrar novo paciente na agenda (placeholder) ──────────
            if patient_name and not existing:
                from datetime import datetime as _dt
                placeholder = _dt(2099, 1, 1, 9, 0)
                db.create_appointment(tenant["id"], patient_name, phone_norm,
                                      placeholder, "Novo paciente — aguardando agendamento")
                logger.info(f"[{tenant['slug']}] Auto-registrado: {patient_name} ({phone_norm})")

            # Publicar evento no dashboard mesmo sem nome ainda
            await events.publish(tenant["id"], "new_patient", {
                "phone": phone,
                "patient_name": patient_name or phone,
            })
        # ── Paciente respondeu NÃO à confirmação → avisar psicóloga ──────────────
        elif event and event.get("type") == "confirmation_declined":
            psy_phone = _norm_phone(tenant.get("psychologist_phone", ""))
            ev_data = event.get("data", {}) or {}
            phone_norm = _norm_phone(phone)
            nome_txt = ev_data.get("patient_name", "") or "Paciente"
            quando_txt = ""
            try:
                import calendar_service as _cal
                from datetime import datetime as _dt
                if ev_data.get("scheduled_at"):
                    quando_txt = _cal.format_slots([_dt.fromisoformat(ev_data["scheduled_at"])])[0]
            except Exception:
                quando_txt = ""
            if psy_phone:
                linha_quando = f"\nSessão: *{quando_txt}*" if quando_txt else ""
                notif = (
                    f"⚠️ *Confirmação recusada*\n"
                    f"*{nome_txt}* respondeu *NÃO* ao pedido de confirmação.{linha_quando}\n"
                    f"Número: {phone_norm}\n\n"
                    f"Pode ser que precise remarcar ou cancelar — recomendo falar "
                    f"com o(a) paciente. 💖"
                )
                await wa.send_message(tenant, psy_phone, notif)
                logger.info(f"[{tenant['slug']}] Notificação de NEGATIVA enviada para psicóloga")
            try:
                await events.publish(tenant["id"], "confirmation_declined",
                                     {"phone": phone, "patient_name": ev_data.get("patient_name", "")})
            except Exception:
                pass

        elif event:
            await events.publish(tenant["id"], event["type"], event["data"])

    except Exception as e:
        logger.exception(f"[{tenant['slug']}] Erro ao processar {phone}: {e}")
        if not db.is_agent_paused(tenant["id"], phone):
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


def _validate_webhook_token(tenant: dict, request: Request) -> bool:
    """Valida o token de webhook esperado.
    - Aceita via query string ?token=, header X-Webhook-Token ou Client-Token.
    - Se o tenant ainda não tem webhook_token (legacy), aceita mas loga warning.
    """
    expected = (tenant.get("webhook_token") or "").strip()
    if not expected:
        logger.warning(f"[{tenant['slug']}] Webhook recebido SEM token configurado — modo legacy")
        return True
    provided = (
        request.query_params.get("token", "")
        or request.headers.get("X-Webhook-Token", "")
        or request.headers.get("Client-Token", "")
    ).strip()
    import hmac as _hmac
    if not provided or not _hmac.compare_digest(provided, expected):
        logger.warning(f"[{tenant['slug']}] Webhook REJEITADO — token inválido (provided={provided[:8]}...)")
        return False
    return True


# ── Idempotência do webhook Z-API (evita reprocessar mesma mensagem em retries) ──
_zapi_seen_ids: "_deque[str]" = _deque(maxlen=2000)
_zapi_seen_set: set[str] = set()
_zapi_seen_lock = _Lock()

def _zapi_already_seen(msg_id: str) -> bool:
    if not msg_id:
        return False
    with _zapi_seen_lock:
        if msg_id in _zapi_seen_set:
            return True
        if len(_zapi_seen_ids) == _zapi_seen_ids.maxlen:
            _zapi_seen_set.discard(_zapi_seen_ids[0])
        _zapi_seen_ids.append(msg_id)
        _zapi_seen_set.add(msg_id)
        return False


@app.post("/webhook/{slug}/zapi")
async def webhook_zapi(slug: str, request: Request, bg: BackgroundTasks):
    tenant = _get_tenant(slug)
    if not _validate_webhook_token(tenant, request):
        raise HTTPException(status_code=403, detail="Token de webhook inválido.")
    payload = await request.json()
    # Ignorar mensagens enviadas pelo próprio número (ecos, status, etc.)
    if payload.get("fromMe"):
        return {"status": "ignored"}
    # Deduplicação por messageId — Z-API faz retry em caso de timeout e
    # entregava a mesma mensagem 2x, gerando duas respostas do agente.
    msg_id = (
        payload.get("messageId")
        or payload.get("zaapId")
        or payload.get("id")
        or ""
    )
    if msg_id and _zapi_already_seen(msg_id):
        logger.info(f"[{slug}] Webhook duplicado ignorado (msg_id={msg_id})")
        return {"status": "duplicate"}
    result = wa.extract_message_zapi(payload)
    if not result:
        return {"status": "ignored"}
    phone, text = result
    bg.add_task(_handle_message, tenant, phone, text)
    return {"status": "queued"}


@app.post("/webhook/{slug}/zapi/sent")
async def webhook_zapi_sent(slug: str, request: Request):
    """Endpoint do 'Ao enviar' do Z-API — ignorado (pausa feita pelo Painel Mobile)."""
    return {"status": "ignored"}


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
    pix_key: Optional[str] = None
    pix_name: Optional[str] = None
    working_days: Optional[str] = None
    blocked_hours: Optional[str] = None
    blocked_hours_by_day: Optional[str] = None
    blocked_dates: Optional[str] = None
    confirmation_hour: Optional[int] = None
    confirmation_msg_template: Optional[str] = None
    followup_msg_template: Optional[str] = None
    billing_msg_template: Optional[str] = None
    psychologist_phone: Optional[str] = None
    email: Optional[str] = None
    free_until: Optional[str] = None  # data ISO (YYYY-MM-DD) de acesso gratuito
    # CalDAV
    caldav_url: Optional[str] = None
    caldav_username: Optional[str] = None
    caldav_password: Optional[str] = None
    # Z-API / WhatsApp
    evolution_instance: Optional[str] = None
    evolution_key: Optional[str] = None
    evolution_url: Optional[str] = None


@app.patch("/admin/tenants/{slug}")
def update_tenant(slug: str, body: TenantUpdate):
    _get_tenant(slug)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar.")
    # Segurança: CalDAV trafega credenciais via Basic Auth — exigir HTTPS.
    cu = (fields.get("caldav_url") or "").strip()
    if cu and not cu.lower().startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="caldav_url deve usar HTTPS (credenciais não podem trafegar em texto puro).",
        )
    db.update_tenant(slug, **fields)
    return db.get_tenant(slug)


class ZAPIConfig(BaseModel):
    instance_id: str
    token: str
    client_token: Optional[str] = ""


@app.patch("/admin/tenants/{slug}/zapi")
def configure_zapi(slug: str, body: ZAPIConfig):
    """Configura Z-API para o consultório."""
    tenant = _get_tenant(slug)
    ts.configure_zapi(slug, body.instance_id, body.token, body.client_token or "")
    wt = db.ensure_webhook_token(tenant["id"])
    return {
        "status": "configured",
        "provider": "zapi",
        "webhook_url": f"{config.BASE_URL}/webhook/{slug}/zapi?token={wt}",
        "webhook_token": wt,
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
    # America/Sao_Paulo (consultas são salvas em horário de Brasília naive).
    _br = ZoneInfo("America/Sao_Paulo")
    now_br = datetime.now(_br).replace(tzinfo=None)
    now = now_br.isoformat()
    far = now_br.replace(year=now_br.year + 1).isoformat()
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


@app.patch("/admin/{slug}/fix-phone")
def fix_phone(slug: str, old_phone: str, new_phone: str):
    """Corrige número de telefone em appointments, conversations e agent_paused."""
    tenant = _get_tenant(slug)
    tid = tenant["id"]
    with db.get_conn() as conn:
        a = conn.execute("UPDATE appointments SET phone=? WHERE tenant_id=? AND phone=?", (new_phone, tid, old_phone)).rowcount
        c = conn.execute("UPDATE conversations SET phone=? WHERE tenant_id=? AND phone=?", (new_phone, tid, old_phone)).rowcount
        p = conn.execute("UPDATE agent_paused SET phone=? WHERE tenant_id=? AND phone=?", (new_phone, tid, old_phone)).rowcount
        pt = conn.execute("UPDATE patients SET phone=? WHERE tenant_id=? AND phone=?", (new_phone, tid, old_phone)).rowcount
    return {"appointments": a, "conversations": c, "paused": p, "patients": pt}


@app.delete("/admin/{slug}/conversations/all")
def clear_all_conversations(slug: str):
    """Limpa TODO o histórico de conversas e pausa do tenant."""
    tenant = _get_tenant(slug)
    with db.get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE tenant_id = ?", (tenant["id"],)
        ).fetchone()[0]
        conn.execute("DELETE FROM conversations WHERE tenant_id = ?", (tenant["id"],))
        conn.execute("DELETE FROM agent_paused WHERE tenant_id = ?", (tenant["id"],))
    return {"status": "cleared", "deleted_messages": total}


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
    # Redirecionar consultório suspenso para página de reativação
    status = tenant.get("status", "active")
    if status == "suspended" and not db.is_tenant_exempt(tenant):
        setup_token = tenant.get("setup_token", "")
        return RedirectResponse(f"/onboarding/pagamento?token={setup_token}&suspended=1", status_code=303)
    return templates.TemplateResponse("dashboard.html", {"request": request, "tenant": tenant, "token": token})


@app.get("/dashboard/api/appointments")
def dash_appointments(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    # Inclui consultas a partir do início de hoje — assim sessões que já ocorreram
    # hoje continuam visíveis para a psicóloga marcar comparecimento depois.
    _br = ZoneInfo("America/Sao_Paulo")
    today_start = datetime.now(_br).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None).isoformat()
    far = (datetime.now(_br).replace(tzinfo=None).replace(year=datetime.now().year + 1)).isoformat()
    appts = db.get_appointments_in_range(tenant["id"], today_start, far)
    # Esconde canceladas da listagem
    appts = [a for a in appts if not a.get("cancelled")]
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

    duration = tenant.get("session_minutes", 50)
    if db.has_conflict(tenant["id"], scheduled, duration):
        raise HTTPException(status_code=409, detail="Este horário conflita com outra consulta (sobreposição de sessão).")

    phone = _norm_phone(body.phone)
    # Remove placeholder 2099 ("novo paciente aguardando agendamento") deste número
    # — agora que há sessão real, o placeholder não faz mais sentido.
    try:
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM appointments WHERE tenant_id = ? AND phone = ? AND scheduled_at >= '2099-01-01'",
                (tenant["id"], phone),
            )
    except Exception as e:
        logger.warning(f"[{tenant['slug']}] Falha ao limpar placeholder de {phone}: {e}")
    appt_id = db.create_appointment(tenant["id"], body.patient_name, phone, scheduled)

    # Sincronizar com Google Calendar
    gcal_synced = False
    try:
        event_id = gcal.create_event(tenant, body.patient_name, scheduled.isoformat(), tenant.get("session_minutes", 50))
        if event_id:
            db.set_appointment_google_event_id(appt_id, event_id)
            gcal_synced = True
            logger.info(f"[gcal][{tenant['slug']}] Evento criado: {event_id}")
    except Exception as e:
        logger.warning(f"[gcal][{tenant['slug']}] Falha ao criar evento para {body.patient_name}: {e}")

    return {"status": "created", "id": appt_id, "gcal_synced": gcal_synced}


@app.post("/dashboard/api/appointments/{appt_id}/confirm")
def dash_confirm(appt_id: int, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.confirm_appointment(tenant["id"], appt_id)
    return {"status": "confirmed"}


class AttendanceBody(BaseModel):
    status: str  # 'attended' | 'missed_no_notice' | 'missed_with_notice' | 'pending'


@app.post("/dashboard/api/appointments/{appt_id}/attendance")
def dash_attendance(appt_id: int, body: AttendanceBody, request: Request):
    """Marca o status de comparecimento de uma consulta.
    - attended: compareceu (cobra)
    - missed_no_notice: faltou sem aviso (cobra)
    - missed_with_notice: não compareceu com aviso (não cobra)
    - pending: limpa marcação
    """
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    if body.status not in db.ATTENDANCE_VALUES:
        raise HTTPException(status_code=400, detail=f"Status inválido. Use: {sorted(db.ATTENDANCE_VALUES)}")
    ok = db.set_attendance(tenant["id"], appt_id, body.status)
    if not ok:
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")
    logger.info(f"[{tenant['slug']}] Attendance set: appt={appt_id} → {body.status}")

    # Sincroniza o calendário externo. "Avisou que não vem" libera o horário,
    # então o evento DEVE sair do Google Calendar / CalDAV. Se o status for
    # revertido (Desfazer, compareceu, faltou sem aviso) e o horário ainda for
    # futuro, recria o evento para o calendário ficar consistente.
    has_gcal = bool(tenant.get("google_refresh_token"))
    has_caldav = bool(tenant.get("caldav_url")) if not has_gcal else False
    if has_gcal or has_caldav:
        try:
            appt = db.get_appointment_by_id(tenant["id"], appt_id)
            if appt:
                if body.status == "missed_with_notice":
                    ev = appt.get("google_event_id")
                    if ev:
                        try:
                            if has_gcal:
                                gcal.delete_event(tenant, ev)
                            else:
                                caldav_svc.delete_event(tenant, ev)
                        except Exception as e:
                            logger.warning(f"[cal][{tenant['slug']}] del evento {ev} falhou: {e}")
                        db.set_appointment_google_event_id(appt_id, "")
                        logger.info(f"[cal][{tenant['slug']}] appt {appt_id} removido do calendário (avisou que não vem)")
                else:
                    # Status normal: garante que o evento exista se for futuro.
                    from datetime import datetime as _dt
                    try:
                        future = _dt.fromisoformat(appt["scheduled_at"]) > _dt.now()
                    except Exception:
                        future = False
                    if future and not appt.get("google_event_id"):
                        name = appt.get("patient_name") or "Paciente"
                        duration = tenant.get("session_minutes", 50)
                        try:
                            new_id = (gcal.create_event(tenant, name, appt["scheduled_at"], duration)
                                      if has_gcal else
                                      caldav_svc.create_event(tenant, name, appt["scheduled_at"], duration))
                            if new_id:
                                db.set_appointment_google_event_id(appt_id, new_id)
                                logger.info(f"[cal][{tenant['slug']}] appt {appt_id} recriado no calendário (status={body.status})")
                        except Exception as e:
                            logger.warning(f"[cal][{tenant['slug']}] recriar evento appt {appt_id} falhou: {e}")
        except Exception as e:
            logger.warning(f"[cal][{tenant['slug']}] sync atendimento appt {appt_id} falhou: {e}")

    return {"status": "ok", "attendance": body.status}


class RenameBody(BaseModel):
    patient_name: str
    apply_all: bool = False    # se True, atualiza todas as consultas do mesmo telefone


@app.patch("/dashboard/api/appointments/{appt_id}/rename")
def dash_rename(appt_id: int, body: RenameBody, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    new_name = (body.patient_name or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Nome não pode ser vazio.")
    n = db.rename_patient(tenant["id"], appt_id, new_name, apply_all=body.apply_all)
    if n == 0:
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")
    logger.info(f"[{tenant['slug']}] Paciente renomeado em {n} consulta(s) → {new_name}")
    return {"status": "renamed", "updated": n, "patient_name": new_name}


class RescheduleBody(BaseModel):
    scheduled_at: str  # ISO datetime, ex: "2026-05-13T14:00:00"
    notify_patient: bool = True


@app.patch("/dashboard/api/appointments/{appt_id}/reschedule")
async def dash_reschedule(appt_id: int, body: RescheduleBody, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    appt = db.get_appointment_by_id(tenant["id"], appt_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")
    try:
        new_dt = datetime.fromisoformat(body.scheduled_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Data/hora inválida.")

    duration = tenant.get("session_minutes", 50)
    if db.has_conflict(tenant["id"], new_dt, duration, exclude_id=appt_id):
        raise HTTPException(status_code=409, detail="Este horário conflita com outra consulta (sobreposição de sessão).")

    # Atualiza no banco
    db.update_appointment(tenant["id"], appt_id, new_dt)
    # Resetar confirmação (nova data = pendente)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE appointments SET confirmed=0, confirmation_sent=0, followup_sent=0 WHERE id=? AND tenant_id=?",
            (appt_id, tenant["id"])
        )

    # Atualiza no Google Calendar (fallback cria evento novo se faltar event_id
    # ou se o update falhar — assim consultas criadas antes da conexão com o
    # GCal também aparecem após o reagendamento).
    if tenant.get("google_refresh_token"):
        gcal_ok = False
        try:
            if appt.get("google_event_id"):
                gcal_ok = gcal.update_event(
                    tenant, appt["google_event_id"], appt["patient_name"],
                    new_dt.isoformat(), tenant.get("session_minutes", 50),
                )
            if not gcal_ok:
                new_id = gcal.create_event(
                    tenant, appt["patient_name"], new_dt.isoformat(),
                    tenant.get("session_minutes", 50),
                )
                if new_id:
                    db.set_appointment_google_event_id(appt_id, new_id)
                    logger.info(f"[gcal] reschedule(dashboard) fallback create: novo event_id={new_id}")
        except Exception as e:
            logger.warning(f"[gcal] reagendamento falhou: {e}")
    # Atualiza no CalDAV (se Google não estiver conectado)
    elif not tenant.get("google_refresh_token"):
        try:
            cd_ok = False
            if appt.get("google_event_id"):
                cd_ok = caldav_svc.update_event(
                    tenant, appt["google_event_id"], appt["patient_name"],
                    new_dt.isoformat(), tenant.get("session_minutes", 50),
                )
            if not cd_ok:
                new_uid = caldav_svc.create_event(
                    tenant, appt["patient_name"], new_dt.isoformat(),
                    tenant.get("session_minutes", 50),
                )
                if new_uid:
                    db.set_appointment_google_event_id(appt_id, new_uid)
        except Exception as e:
            logger.warning(f"[caldav] reagendamento falhou: {e}")

    # Notifica paciente via WhatsApp
    if body.notify_patient and appt.get("phone"):
        import calendar_service as cal_svc
        formatted = cal_svc.format_slots([new_dt])[0]
        msg = (f"Olá, {appt['patient_name']}! 😊 "
               f"Sua sessão foi reagendada para *{formatted}*. "
               f"Qualquer dúvida é só responder aqui. Até lá! 🌸")
        await wa.send_message(tenant, appt["phone"], msg)

    return {"status": "rescheduled", "scheduled_at": new_dt.isoformat()}


@app.delete("/dashboard/api/appointments/{appt_id}/cancel")
def dash_cancel(appt_id: int, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    # Remover do Google Calendar antes de deletar do banco
    appt = db.get_appointment_by_id(tenant["id"], appt_id)
    if appt and appt.get("google_event_id"):
        if tenant.get("google_refresh_token"):
            try:
                gcal.delete_event(tenant, appt["google_event_id"])
            except Exception as e:
                logger.warning(f"[gcal][{tenant['slug']}] Falha ao deletar evento {appt['google_event_id']}: {e}")
        else:
            try:
                caldav_svc.delete_event(tenant, appt["google_event_id"])
            except Exception as e:
                logger.warning(f"[caldav][{tenant['slug']}] Falha ao deletar evento {appt['google_event_id']}: {e}")
    with db.get_conn() as conn:
        conn.execute("DELETE FROM appointments WHERE id = ? AND tenant_id = ?", (appt_id, tenant["id"]))
    return {"status": "cancelled"}


@app.post("/dashboard/api/caldav/test")
def dash_caldav_test(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    result = caldav_svc.test_connection(tenant)
    return result


@app.get("/dashboard/api/slots")
def dash_slots(request: Request, days: int = 14, limit: int = 30):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    slots = cal.get_available_slots(tenant, days_ahead=days, limit=limit)
    labels = cal.format_slots(slots)
    return {
        "slots": labels,  # compat
        "slots_detailed": [
            {"label": labels[i], "iso": slots[i].isoformat()}
            for i in range(len(slots))
        ],
    }


@app.get("/dashboard/api/session-counts")
def dash_session_counts(request: Request, month: str | None = None):
    """Retorna contagem de sessões 'efetivas' do mês por telefone.

    month no formato 'YYYY-MM'. Se omitido, usa o mês corrente em São Paulo.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
    if month:
        try:
            y, m = month.split("-")
            y, m = int(y), int(m)
        except Exception:
            y, m = now.year, now.month
    else:
        y, m = now.year, now.month
    start = _dt(y, m, 1).isoformat()
    end = (_dt(y + 1, 1, 1) if m == 12 else _dt(y, m + 1, 1)).isoformat()
    counts = db.get_session_counts_by_month(tenant["id"], start, end)
    return {"month": f"{y:04d}-{m:02d}", "counts": counts}


@app.get("/dashboard/api/patient-history/{phone}")
def dash_patient_history(phone: str, request: Request):
    """Histórico completo de um paciente, agrupado por mês."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    appts = db.get_full_patient_history(tenant["id"], phone)
    # Agrupa por YYYY-MM
    groups: dict[str, dict] = {}
    for a in appts:
        sched = a.get("scheduled_at") or ""
        key = sched[:7] if len(sched) >= 7 else "sem-data"
        g = groups.setdefault(key, {"month": key, "items": [], "billable": 0})
        g["items"].append(a)
        if not a.get("cancelled") and (a.get("attendance") or "pending") != "missed_with_notice":
            g["billable"] += 1
    out = sorted(groups.values(), key=lambda g: g["month"], reverse=True)
    return {"phone": phone, "months": out}


@app.get("/dashboard/api/patients")
def dash_patients(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT phone, name FROM (
               SELECT c.phone,
                 COALESCE(
                   (SELECT a.patient_name FROM appointments a
                    WHERE a.phone = c.phone AND a.tenant_id = c.tenant_id
                      AND COALESCE(a.patient_name,'') != '' LIMIT 1),
                   (SELECT p.name FROM patients p
                    WHERE p.phone = c.phone AND p.tenant_id = c.tenant_id
                      AND COALESCE(p.name,'') != '' LIMIT 1)
                 ) as name,
                 c.created_at as sort_key
               FROM conversations c WHERE c.tenant_id = ?
               UNION
               SELECT p.phone,
                 COALESCE(
                   NULLIF(p.name,''),
                   (SELECT a.patient_name FROM appointments a
                    WHERE a.phone = p.phone AND a.tenant_id = p.tenant_id LIMIT 1)
                 ) as name,
                 '1970-01-01' as sort_key
               FROM patients p WHERE p.tenant_id = ?
            ) GROUP BY phone ORDER BY sort_key DESC""",
            (tenant["id"], tenant["id"])
        ).fetchall()
    return {"patients": [dict(r) for r in rows]}


@app.post("/dashboard/api/conversation/{phone}/pause")
def dash_pause(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)  # garante dígitos-only antes de gravar
    db.pause_agent(tenant["id"], phone)
    return {"status": "paused", "phone": phone}


@app.post("/dashboard/api/conversation/{phone}/resume")
def dash_resume(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)  # garante dígitos-only antes de remover
    db.resume_agent(tenant["id"], phone)
    return {"status": "resumed", "phone": phone}


@app.get("/dashboard/api/paused")
def dash_paused(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    return {"paused": db.list_paused_phones(tenant["id"])}


# ── Painel Mobile de Controle ──────────────────────────────────────────────────

def _norm_phone(phone: str) -> str:
    """Normaliza número removendo tudo que não é dígito."""
    return "".join(c for c in phone if c.isdigit())


# ── Detecção de SPAM / propaganda ──────────────────────────────────────────────

_SPAM_DOMAINS = (
    "tiktok.com", "vm.tiktok", "gol-de-premios", "goldepremios",
    "bit.ly", "tinyurl.com", "encurtador", "shopee.com.br", "amzn.to",
    "mlb.com.br", "mercadolivre", "instagram.com/reel",
    "youtube.com/shorts", "youtu.be",
)
_SPAM_KEYWORDS = (
    "ganhe prêmio", "ganhe premio", "clique no link", "click no link",
    "promoção imperdível", "promocao imperdivel", "oferta relâmpago",
    "oferta relampago", "renda extra", "trabalhe em casa", "ganhe dinheiro",
    "loteria", "sorteio", "prêmio acumulado", "premio acumulado",
    "cashback grátis", "cashback gratis", "fature r$", "ganhe r$",
    "🎁🎁", "💰💰", "👉👉",
)


def _is_spam(text: str) -> bool:
    """Detecta mensagens promocionais / spam (links de TikTok, gol-de-premios etc.)."""
    if not text or len(text) < 5:
        return False
    t = text.lower()
    if any(d in t for d in _SPAM_DOMAINS):
        return True
    if any(k in t for k in _SPAM_KEYWORDS):
        return True
    # Heurística: mensagem muito longa (>400 chars) cheia de emojis e link http
    if len(text) > 400 and "http" in t and text.count("\n") < 3:
        return True
    return False


@app.get("/controle/{token}", response_class=HTMLResponse)
def controle_mobile(token: str, request: Request):
    """Painel leve para pausar/retomar o agente direto do celular."""
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        # Contatos de agendamentos (com nome)
        appt_rows = conn.execute(
            """SELECT DISTINCT phone, patient_name FROM appointments
               WHERE tenant_id = ? AND phone != ''""",
            (tenant["id"],)
        ).fetchall()
        # Contatos de conversas (podem não ter agendamento)
        conv_rows = conn.execute(
            """SELECT DISTINCT phone FROM conversations
               WHERE tenant_id = ? AND phone != '' AND role = 'user'""",
            (tenant["id"],)
        ).fetchall()
        # Nomes salvos manualmente (via renomear ou cadastro)
        patient_rows = conn.execute(
            """SELECT phone, name FROM patients
               WHERE tenant_id = ? AND name != '' AND name IS NOT NULL""",
            (tenant["id"],)
        ).fetchall()

    # Monta dicionário phone → nome(s) — agrupa múltiplos pacientes no mesmo número
    names_by_phone: dict[str, list[str]] = {}
    for r in appt_rows:
        p = _norm_phone(r["phone"])
        if p and r["patient_name"]:
            if p not in names_by_phone:
                names_by_phone[p] = []
            if r["patient_name"] not in names_by_phone[p]:
                names_by_phone[p].append(r["patient_name"])

    seen: dict[str, str] = {
        p: " / ".join(names) for p, names in names_by_phone.items()
    }
    # Adiciona contatos de conversas que não estão na agenda
    for r in conv_rows:
        p = _norm_phone(r["phone"])
        if p and p not in seen:
            seen[p] = ""  # sem nome — mostra só o número

    # Preenche nomes salvos em patients (renomeados via controle)
    for r in patient_rows:
        p = _norm_phone(r["phone"])
        if p and p in seen and not seen[p]:
            seen[p] = r["name"]

    # Filtra IDs de grupo do WhatsApp (não são pacientes)
    def _is_group(ph: str) -> bool:
        return bool(ph) and ("@g.us" in ph or ph.startswith("120363"))

    patients = sorted(
        [{"phone": p, "patient_name": name} for p, name in seen.items() if not _is_group(p)],
        key=lambda x: x["patient_name"].lower() if x["patient_name"] else x["phone"]
    )

    # Normaliza os phones pausados também
    paused = set(_norm_phone(ph) for ph in db.list_paused_phones(tenant["id"]))

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
    db.pause_agent(tenant["id"], _norm_phone(phone))
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/pausar-todos", response_class=HTMLResponse)
def controle_pausar_todos(token: str):
    """Pausa o agente para TODOS os contatos do tenant (agendamentos + conversas)."""
    tenant = _get_tenant_by_token(token)
    with db.get_conn() as conn:
        appt_phones = [r["phone"] for r in conn.execute(
            "SELECT DISTINCT phone FROM appointments WHERE tenant_id = ? AND phone != ''",
            (tenant["id"],)
        ).fetchall()]
        conv_phones = [r["phone"] for r in conn.execute(
            "SELECT DISTINCT phone FROM conversations WHERE tenant_id = ? AND phone != '' AND role = 'user'",
            (tenant["id"],)
        ).fetchall()]
    phones = sorted({_norm_phone(p) for p in (appt_phones + conv_phones) if p})
    n = db.pause_all_agents(tenant["id"], phones)
    logger.info(f"[{tenant['slug']}] Pausa em massa: {n} contatos pausados")
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/retomar-todos", response_class=HTMLResponse)
def controle_retomar_todos(token: str):
    """Remove TODAS as pausas do tenant — agente volta a responder todos."""
    tenant = _get_tenant_by_token(token)
    n = db.resume_all_agents(tenant["id"])
    logger.info(f"[{tenant['slug']}] Retomada em massa: {n} pausas removidas")
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/renomear/{phone}", response_class=HTMLResponse)
def controle_renomear(token: str, phone: str, patient_name: str = Form(...)):
    """Salva nome de um contato que aparecia só com número."""
    tenant = _get_tenant_by_token(token)
    p = _norm_phone(phone)
    name = (patient_name or "").strip()
    if p and name:
        n = db.rename_patient_by_phone(tenant["id"], p, name)
        logger.info(f"[{tenant['slug']}] Contato renomeado: {p} → {name} ({n} consultas afetadas)")
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/retomar/{phone}", response_class=HTMLResponse)
def controle_retomar(token: str, phone: str):
    tenant = _get_tenant_by_token(token)
    db.resume_agent(tenant["id"], _norm_phone(phone))
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/excluir/{phone}", response_class=HTMLResponse)
def controle_excluir(token: str, phone: str):
    """Remove completamente um contato: conversas, agendamentos e pausa."""
    tenant = _get_tenant_by_token(token)
    p = _norm_phone(phone)
    db.clear_conversation(tenant["id"], p)
    db.resume_agent(tenant["id"], p)
    # Remove também da tabela de agendamentos
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM appointments WHERE tenant_id = ? AND replace(replace(replace(phone,'+',''),'-',''),' ','') = ?",
            (tenant["id"], p)
        )
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.post("/controle/{token}/novo-paciente", response_class=HTMLResponse)
def controle_novo_paciente(token: str, request: Request,
                           patient_name: str = Form(...),
                           phone: str = Form(...)):
    """Cadastra um novo paciente manualmente com placeholder na agenda."""
    tenant = _get_tenant_by_token(token)
    p = _norm_phone(phone)
    if not p:
        return RedirectResponse(f"/controle/{token}", status_code=303)
    from datetime import datetime as _dt
    # Verifica se já existe
    existing = db.get_appointments_by_phone(tenant["id"], p)
    if not existing:
        placeholder = _dt(2099, 1, 1, 9, 0)
        db.create_appointment(tenant["id"], patient_name.strip(), p,
                              placeholder, "Cadastrado manualmente — aguardando agendamento")
        logger.info(f"[{tenant['slug']}] Novo paciente manual: {patient_name} ({p})")
    return RedirectResponse(f"/controle/{token}", status_code=303)


@app.get("/dashboard/api/export")
def dash_export(request: Request):
    """LGPD: exporta todos os dados do tenant em JSON (direito de portabilidade — art. 18, V)."""
    token = request.headers.get("X-Dashboard-Token", "") or request.query_params.get("token", "")
    tenant = _get_tenant_by_token(token)
    tid = tenant["id"]
    # Coleta de dados
    from datetime import datetime as _dt2
    with db.get_conn() as conn:
        appts = [dict(r) for r in conn.execute(
            "SELECT * FROM appointments WHERE tenant_id = ? ORDER BY scheduled_at", (tid,)
        ).fetchall()]
        convs = [dict(r) for r in conn.execute(
            "SELECT phone, role, content, created_at FROM conversations WHERE tenant_id = ? ORDER BY created_at",
            (tid,)
        ).fetchall()]
        try:
            billing = [dict(r) for r in conn.execute(
                "SELECT * FROM billing_logs WHERE tenant_id = ? ORDER BY sent_at", (tid,)
            ).fetchall()]
        except Exception:
            billing = []
        try:
            paused = [dict(r) for r in conn.execute(
                "SELECT * FROM paused_conversations WHERE tenant_id = ?", (tid,)
            ).fetchall()]
        except Exception:
            paused = []

    # Remove campos sensíveis do tenant antes de exportar
    tenant_safe = {k: v for k, v in dict(tenant).items()
                   if k not in {"dashboard_token", "setup_token", "webhook_token",
                                "evolution_key", "twilio_token", "google_refresh_token",
                                "caldav_password", "stripe_subscription_id"}}
    payload = {
        "exported_at": _dt2.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
        "tenant": tenant_safe,
        "appointments": appts,
        "conversations": convs,
        "billing_logs": billing,
        "paused_conversations": paused,
        "_lgpd_notice": "Exportação realizada conforme art. 18, V da LGPD. Conserve este arquivo em local seguro.",
    }
    fname = f"export-{tenant['slug']}-{_dt2.now().strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/dashboard/api/conversation/{phone}")
def dash_conversation(phone: str, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    history = db.get_conversation_history(tenant["id"], phone, limit=50)
    return {"history": history}


@app.patch("/dashboard/api/config")
async def dash_config(request: Request, body: TenantUpdate):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    fields = body.model_dump(exclude_none=True)
    # Segurança: CalDAV trafega credenciais via Basic Auth — exigir HTTPS.
    cu = (fields.get("caldav_url") or "").strip()
    if cu and not cu.lower().startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="A URL do CalDAV precisa começar com https:// (suas credenciais não podem trafegar em texto puro).",
        )
    saving_zapi = fields.get("evolution_instance") and fields.get("evolution_key")
    # Auto-set provider to zapi when Z-API credentials are provided
    if saving_zapi:
        fields["whatsapp_provider"] = "zapi"
    if fields:
        db.update_tenant(tenant["slug"], **fields)

    # Configurar webhook automaticamente ao salvar credenciais Z-API
    webhook_result = None
    if saving_zapi:
        updated_tenant = db.get_tenant(tenant["slug"])
        wt = db.ensure_webhook_token(updated_tenant["id"])
        webhook_url = f"{config.BASE_URL}/webhook/{updated_tenant['slug']}/zapi?token={wt}"
        webhook_result = await wa.configure_webhook_zapi(updated_tenant, webhook_url)

    return {"status": "updated", "webhook": webhook_result}


@app.get("/dashboard/api/zapi/qr")
async def dash_zapi_qr(request: Request):
    """QR code para parear o WhatsApp direto no painel (onboarding self-serve).
    Quando detecta que já conectou, garante o webhook configurado."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    if not tenant.get("evolution_instance") or not tenant.get("evolution_key"):
        return {"ok": False, "connected": None, "error": "Salve o Instance ID e o Token antes de conectar."}
    res = await wa.get_zapi_qr(tenant)
    if res.get("connected"):
        # Pareou agora → garante provider + webhook ativos (idempotente)
        try:
            updates = {}
            if (tenant.get("whatsapp_provider") or "") != "zapi":
                updates["whatsapp_provider"] = "zapi"
            if updates:
                db.update_tenant(tenant["slug"], **updates)
            wt = db.ensure_webhook_token(tenant["id"])
            webhook_url = f"{config.BASE_URL}/webhook/{tenant['slug']}/zapi?token={wt}"
            res["webhook"] = await wa.configure_webhook_zapi(tenant, webhook_url)
        except Exception as e:
            logger.warning(f"[{tenant['slug']}] pós-conexão Z-API: {e}")
    return res


# ── Cobrança ────────────────────────────────────────────────────────────────────

class PatientPriceBody(BaseModel):
    session_price: float
    email: str = ""

class PatientRenameBody(BaseModel):
    name: str

@app.patch("/dashboard/api/patients/{phone}/name")
def dash_rename_patient(phone: str, body: PatientRenameBody, request: Request):
    """Corrige o nome do paciente em todos os registros (appointments + patients).
    Útil quando o agente gravou um nome errado (ex: alucinação)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    new_name = "".join(ch for ch in (body.name or "") if ch.isprintable()).strip()[:100]
    if not new_name:
        raise HTTPException(status_code=400, detail="Nome inválido.")
    # Atualiza agendamentos casando variantes do telefone (com/sem 55 e o "9")
    # e persiste em patients — robusto ao Z-API variar o formato do número.
    updated = db.rename_patient_everywhere(tenant["id"], phone, new_name)
    logger.info(f"[{tenant['slug']}] Paciente renomeado: {phone} → '{new_name}' ({updated} agend.)")
    return {"status": "ok", "phone": phone, "name": new_name, "updated": updated}


@app.patch("/dashboard/api/patients/{phone}/price")
def dash_set_patient_price(phone: str, body: PatientPriceBody, request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    db.upsert_patient(tenant["id"], phone, session_price=body.session_price, email=body.email)
    return {"status": "ok"}

@app.get("/dashboard/api/patients/{phone}/billing-info")
def dash_patient_billing_info(phone: str, request: Request, month: str | None = None):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    patient = db.get_patient(tenant["id"], phone)
    # Override do mês (se informado) — ajuste manual de valor combinado
    override = None
    if month:
        ov = db.get_billing_override(tenant["id"], phone, month)
        if ov:
            override = {"total_amount": ov["total_amount"], "note": ov.get("note", "")}
    return {
        "session_price": patient["session_price"] if patient else 0,
        "email": patient["email"] if patient else "",
        "billing_paused": bool(patient.get("billing_paused")) if patient else False,
        "override": override,
    }


@app.post("/dashboard/api/patients/{phone}/billing-pause")
async def dash_patient_billing_pause(phone: str, request: Request):
    """Pausa/retoma a cobrança automática de UM paciente (Feature: pausa individual)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    body = await request.json()
    paused = bool(body.get("paused"))
    db.set_patient_billing_paused(tenant["id"], phone, paused)
    logger.info(f"[{tenant['slug']}] Cobrança paciente {phone} → {'pausada' if paused else 'retomada'}")
    return {"status": "ok", "phone": phone, "billing_paused": paused}


@app.post("/dashboard/api/patients/{phone}/billing-override")
async def dash_set_billing_override(phone: str, request: Request):
    """Define/ajusta o valor TOTAL cobrado de um paciente num mês específico
    (desconto combinado ou complemento de sessão). Substitui nº sessões × preço."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    body = await request.json()
    month = (body.get("month") or "").strip()
    if not month or len(month) != 7:
        raise HTTPException(status_code=400, detail="Mês inválido (use YYYY-MM).")
    try:
        total = float(body.get("total_amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Valor total inválido.")
    if total < 0:
        raise HTTPException(status_code=400, detail="Valor não pode ser negativo.")
    note = "".join(ch for ch in (body.get("note") or "") if ch.isprintable()).strip()[:200]
    db.set_billing_override(tenant["id"], phone, month, total, note)
    logger.info(f"[{tenant['slug']}] Override cobrança {phone} {month} → R${total:.2f}")
    return {"status": "ok", "phone": phone, "month": month, "total_amount": total, "note": note}


@app.delete("/dashboard/api/patients/{phone}/billing-override")
def dash_delete_billing_override(phone: str, request: Request, month: str):
    """Remove o ajuste manual de valor — volta ao cálculo padrão."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    phone = _norm_phone(phone)
    db.delete_billing_override(tenant["id"], phone, month)
    return {"status": "ok", "phone": phone, "month": month}


# ── Cobranças avulsas (manuais) ─────────────────────────────────────────────────

@app.post("/dashboard/api/billing/manual")
async def dash_add_manual_billing(request: Request):
    """Inclui uma cobrança AVULSA no mês: sessão extra fora da agenda ou
    paciente novo que nunca falou pelo WhatsApp profissional. Entra na prévia
    e no total; se tiver telefone, é enviada no próximo disparo (uma vez)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    month = (body.get("month") or "").strip()
    if not month or len(month) != 7:
        raise HTTPException(status_code=400, detail="Mês inválido (use YYYY-MM).")
    name = "".join(ch for ch in (body.get("patient_name") or "") if ch.isprintable()).strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Informe o nome do paciente.")
    raw_phone = (body.get("phone") or "").strip()
    phone = _norm_phone(raw_phone) if raw_phone else ""
    try:
        total = float(body.get("total_amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Valor total inválido.")
    if total < 0:
        raise HTTPException(status_code=400, detail="Valor não pode ser negativo.")
    try:
        sessions = int(body.get("sessions_count") or 1)
    except (TypeError, ValueError):
        sessions = 1
    if sessions < 0:
        sessions = 0
    note = "".join(ch for ch in (body.get("note") or "") if ch.isprintable()).strip()[:200]
    entry_id = db.add_manual_billing_entry(tenant["id"], month, name, phone, sessions, total, note)
    logger.info(f"[{tenant['slug']}] ➕ Cobrança avulsa {name} {month} → R${total:.2f} (phone={phone or '—'})")
    return {"status": "ok", "id": entry_id}


@app.patch("/dashboard/api/billing/manual/{entry_id}")
async def dash_update_manual_billing(entry_id: int, request: Request):
    """Edita uma cobrança avulsa (nome, telefone, sessões, valor, nota)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    fields: dict = {}
    if "patient_name" in body:
        nm = "".join(ch for ch in (body.get("patient_name") or "") if ch.isprintable()).strip()[:120]
        if not nm:
            raise HTTPException(status_code=400, detail="Nome não pode ficar vazio.")
        fields["patient_name"] = nm
    if "phone" in body:
        rp = (body.get("phone") or "").strip()
        fields["phone"] = _norm_phone(rp) if rp else ""
    if "total_amount" in body:
        try:
            t = float(body.get("total_amount"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Valor total inválido.")
        if t < 0:
            raise HTTPException(status_code=400, detail="Valor não pode ser negativo.")
        fields["total_amount"] = t
    if "sessions_count" in body:
        try:
            fields["sessions_count"] = max(0, int(body.get("sessions_count") or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Nº de sessões inválido.")
    if "note" in body:
        fields["note"] = "".join(ch for ch in (body.get("note") or "") if ch.isprintable()).strip()[:200]
    db.update_manual_billing_entry(tenant["id"], entry_id, **fields)
    return {"status": "ok", "id": entry_id}


@app.delete("/dashboard/api/billing/manual/{entry_id}")
def dash_delete_manual_billing(entry_id: int, request: Request):
    """Remove uma cobrança avulsa."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.delete_manual_billing_entry(tenant["id"], entry_id)
    return {"status": "ok", "id": entry_id}


@app.delete("/dashboard/api/patients/{phone}")
def dash_delete_patient(phone: str, request: Request):
    """Exclui DEFINITIVAMENTE um paciente (caso de desistência): conversas,
    agendamentos, cadastro, pausas e overrides. Preserva o histórico financeiro
    já enviado (billing_logs)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    # Mantém o telefone cru (como gravado) — delete_patient_completely casa variantes
    removed = db.delete_patient_completely(tenant["id"], phone)
    logger.info(f"[{tenant['slug']}] 🗑 Paciente excluído {phone} → {removed}")
    return {"status": "ok", "phone": phone, "removed": removed}


@app.delete("/dashboard/api/conversation/{phone}")
def dash_clear_conversation(phone: str, request: Request):
    """Apaga SOMENTE as mensagens da conversa (mantém paciente/agendamentos)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    db.clear_conversation(tenant["id"], phone)
    logger.info(f"[{tenant['slug']}] 🧹 Conversa apagada {phone}")
    return {"status": "ok", "phone": phone}

@app.get("/dashboard/api/stats")
def dash_stats(request: Request, month: str | None = None):
    """Indicadores do mês: sessões, receita, taxa de presença, pacientes ativos."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
    if month:
        try:
            y, m = [int(x) for x in month.split("-")]
        except Exception:
            y, m = now.year, now.month
    else:
        y, m = now.year, now.month
    start = _dt(y, m, 1).isoformat()
    end = (_dt(y + 1, 1, 1) if m == 12 else _dt(y, m + 1, 1)).isoformat()
    now_str = now.isoformat()
    stats = db.get_dashboard_stats(tenant["id"], start, end, now_str)
    stats["month"] = f"{y:04d}-{m:02d}"
    return stats


@app.get("/dashboard/api/billing/export")
def dash_billing_export(request: Request, month: str | None = None):
    """Exporta cobrança do mês como CSV."""
    import csv, io
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi
    from fastapi.responses import StreamingResponse
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
    if month:
        try:
            y, m = [int(x) for x in month.split("-")]
        except Exception:
            y, m = now.year, now.month
    else:
        y, m = now.year, now.month
    start = _dt(y, m, 1).isoformat()
    end = (_dt(y + 1, 1, 1) if m == 12 else _dt(y, m + 1, 1)).isoformat()
    now_str = now.isoformat()
    # Busca sessões cobráveis por paciente
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT a.phone, a.patient_name,
                   COUNT(*) as sessions,
                   p.session_price
            FROM appointments a
            LEFT JOIN patients p ON p.phone = a.phone AND p.tenant_id = a.tenant_id
            WHERE a.tenant_id=? AND a.scheduled_at>=? AND a.scheduled_at<?
              AND a.cancelled=0
              AND COALESCE(a.attendance,'pending') != 'missed_with_notice'
              AND a.scheduled_at <= ?
            GROUP BY a.phone
            ORDER BY a.patient_name
        """, (tenant["id"], start, end, now_str)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Paciente", "Telefone", "Sessões", "Valor unit. (R$)", "Total (R$)"])
    for r in rows:
        price = r["session_price"] or 0
        total = price * r["sessions"]
        writer.writerow([
            r["patient_name"] or "",
            r["phone"] or "",
            r["sessions"],
            f"{price:.2f}".replace(".", ","),
            f"{total:.2f}".replace(".", ","),
        ])
    output.seek(0)
    filename = f"cobranca_{y:04d}-{m:02d}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/dashboard/api/billing/logs")
def dash_billing_logs(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    logs = db.get_billing_logs(tenant["id"])
    return {"logs": logs}

@app.post("/dashboard/api/billing/run")
async def dash_billing_run(request: Request):
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    month_str = body.get("month")  # "2026-05" or None for current
    results = await scheduler.run_billing_now(tenant["id"], month_str)
    return {"results": results, "total_sent": sum(1 for r in results if r["sent"])}


@app.get("/dashboard/api/billing/pause-status")
def dash_billing_pause_status(request: Request):
    """Estado da pausa GLOBAL de cobrança do consultório."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    return {"paused": db.is_tenant_billing_paused(tenant["id"])}


@app.post("/dashboard/api/billing/pause")
async def dash_billing_set_pause(request: Request):
    """Pausa/retoma o envio de TODAS as cobranças (automáticas e manuais)."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    paused = bool(body.get("paused"))
    db.set_tenant_billing_paused(tenant["id"], paused)
    logger.info(f"[{tenant['slug']}] Cobrança global → {'pausada' if paused else 'ativa'}")
    return {"status": "ok", "paused": paused}


@app.post("/dashboard/api/billing/paid")
async def dash_billing_set_paid(request: Request):
    """Marca/desmarca um paciente (da agenda) como PAGO no mês. Só controle de
    recebimento — NÃO altera a cobrança nem dispara nada."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    phone = (body.get("phone") or "").strip()
    month = (body.get("month") or "").strip()
    paid = bool(body.get("paid"))
    if not phone or not month:
        raise HTTPException(status_code=400, detail="phone e month são obrigatórios")
    db.set_billing_paid(tenant["id"], phone, month, paid)
    return {"status": "ok", "phone": phone, "month": month, "paid": paid}


@app.post("/dashboard/api/billing/manual/{entry_id}/paid")
async def dash_billing_manual_set_paid(entry_id: int, request: Request):
    """Marca/desmarca uma cobrança AVULSA como paga. Só controle de recebimento."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    paid = bool(body.get("paid"))
    if db.get_manual_billing_entry(tenant["id"], entry_id) is None:
        raise HTTPException(status_code=404, detail="Cobrança avulsa não encontrada")
    db.set_manual_billing_paid(tenant["id"], entry_id, paid)
    return {"status": "ok", "id": entry_id, "paid": paid}


@app.post("/dashboard/api/billing/receipt/dismiss")
async def dash_billing_dismiss_receipt(request: Request):
    """Dispensa o aviso de comprovante recebido de um paciente no mês (falso
    alarme ou já resolvido). Só remove a sinalização — não altera pagamento."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    body = await request.json()
    phone = (body.get("phone") or "").strip()
    month = (body.get("month") or "").strip()
    if not phone or not month:
        raise HTTPException(status_code=400, detail="phone e month são obrigatórios")
    db.dismiss_billing_receipt(tenant["id"], phone, month)
    return {"status": "ok", "phone": phone, "month": month}


@app.get("/dashboard/api/billing/preview")
def dash_billing_preview(request: Request, month: str | None = None):
    """Prévia da cobrança do mês com base na AGENDA (calendário).

    Espelha exatamente o que o disparo manual cobraria (mesma contagem de
    sessões, variantes de telefone, overrides, dedup e pausas), SEM enviar.
    Mostra também contatos que têm sessão no mês mas ainda não têm valor
    cadastrado (para a psicóloga não esquecer de cobrar ninguém)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi
    from calendar import monthrange as _mr
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
    if month:
        try:
            y, m = [int(x) for x in month.split("-")]
        except Exception:
            y, m = now.year, now.month
    else:
        y, m = now.year, now.month
    month_str = f"{y:04d}-{m:02d}"
    month_start = f"{y:04d}-{m:02d}-01T00:00:00"
    last_day = _mr(y, m)[1]
    month_end = f"{y:04d}-{m:02d}-{last_day:02d}T23:59:59"
    now_str = now.isoformat(timespec="seconds")
    tid = tenant["id"]
    global_paused = db.is_tenant_billing_paused(tid)

    # Todas as linhas do mês (para o detalhamento por status de cada paciente —
    # inclui canceladas/faltas com aviso, que NÃO entram na conta mas a
    # psicóloga pode querer reincluir corrigindo a marcação). Read-only.
    raw_month = db.get_month_appointments_raw(tid, month_start, month_end, now_str)

    # Telefones já marcados como PAGOS neste mês (só-dígitos, tolerante a variantes).
    # Puramente informativo — não afeta cálculo nem disparo.
    paid_phones = db.get_paid_phones_for_month(tid, month_str)
    # Telefones que enviaram COMPROVANTE (imagem/documento/PIX) e aguardam
    # confirmação humana → mapeia p/ tipo. Só sinalização, não marca pago.
    pending_receipts = db.get_pending_receipts_for_month(tid, month_str)

    items = []
    billed_ids: set = set()
    # 1) Pacientes com valor cadastrado → exatamente o que seria cobrado
    for patient in db.get_patients_with_price(tid):
        phone = patient["phone"]
        if not phone:
            continue
        sessions = db.get_valid_sessions_for_month(tid, phone, month_start, month_end, now_str)
        sessions = [s for s in sessions if s.get("id") not in billed_ids]
        if not sessions:
            continue
        for s in sessions:
            if s.get("id") is not None:
                billed_ids.add(s["id"])
        count = len(sessions)
        unit = float(patient.get("session_price") or 0)
        override = db.get_billing_override(tid, phone, month_str)
        if override is not None:
            total = float(override["total_amount"])
        else:
            total = count * unit
        paused = bool(
            patient.get("billing_paused")
            or db.is_patient_billing_paused(tid, phone)
            or db.is_agent_paused(tid, phone)
        )
        name = sessions[0].get("patient_name") or patient.get("name") or phone
        # Detalhamento por status (das que ENTRAM na conta = sessions deduplicadas)
        breakdown = {
            "realizadas": sum(1 for s in sessions if (s.get("attendance") or "pending") == "attended"),
            "nao_marcadas": sum(1 for s in sessions if (s.get("attendance") or "pending") == "pending"),
            "faltou_sem_aviso": sum(1 for s in sessions if (s.get("attendance") or "pending") == "missed_no_notice"),
        }
        # Lista de TODAS as sessões do mês deste paciente (variantes de telefone),
        # com status — para auditar/corrigir cada linha direto na tela.
        pvariants = db._phone_variants(phone) | {db._norm_digits(phone)}
        session_rows = sorted(
            ({
                "id": r.get("id"),
                "scheduled_at": r.get("scheduled_at"),
                "attendance": r.get("attendance") or "pending",
                "cancelled": int(r.get("cancelled") or 0),
                "future": bool(r.get("future")),
                "counts": bool(r.get("counts_for_billing")),
            } for r in raw_month
              if db._norm_digits(r.get("phone") or "") in pvariants),
            key=lambda x: x["scheduled_at"] or "",
        )
        items.append({
            "phone": phone, "patient_name": name, "sessions": count,
            "unit_price": unit, "total": total,
            "has_override": override is not None,
            "paused": paused or global_paused,
            "already_sent": db.billing_already_sent(tid, phone, month_str),
            "no_price": False,
            "paid": db._norm_digits(phone) in paid_phones,
            "receipt": pending_receipts.get(db._norm_digits(phone)),
            "breakdown": breakdown,
            "session_rows": session_rows,
        })

    # 2) Contatos com sessão no mês mas SEM valor cadastrado (avisa, não cobra)
    leftover: dict[str, dict] = {}
    for a in db.get_all_billable_appointments_for_month(tid, month_start, month_end, now_str):
        if a.get("id") in billed_ids:
            continue
        ph = a.get("phone") or ""
        if not ph:
            continue
        g = leftover.setdefault(ph, {"phone": ph, "patient_name": a.get("patient_name") or ph, "sessions": 0})
        g["sessions"] += 1
        if a.get("patient_name") and (not g["patient_name"] or g["patient_name"] == ph):
            g["patient_name"] = a["patient_name"]
    for g in leftover.values():
        items.append({
            "phone": g["phone"], "patient_name": g["patient_name"], "sessions": g["sessions"],
            "unit_price": 0.0, "total": 0.0, "has_override": False,
            "paused": False, "already_sent": False, "no_price": True,
            "paid": db._norm_digits(g["phone"]) in paid_phones,
            "receipt": pending_receipts.get(db._norm_digits(g["phone"])),
        })

    items.sort(key=lambda x: (x["no_price"], (x["patient_name"] or "").lower()))

    # 3) Cobranças AVULSAS (manuais) do mês — sessão extra fora da agenda ou
    #    paciente novo. Entram como linhas próprias (manual=True) e no total.
    manual = []
    for e in db.get_manual_billing_entries(tid, month_str):
        manual.append({
            "id": e["id"],
            "phone": e.get("phone") or "",
            "patient_name": e.get("patient_name") or (e.get("phone") or "Avulsa"),
            "sessions": int(e.get("sessions_count") or 0),
            "unit_price": 0.0,
            "total": float(e.get("total_amount") or 0),
            "note": e.get("note") or "",
            "has_override": False,
            "paused": global_paused,
            "already_sent": bool(e.get("sent_at")),
            "no_price": False,
            "manual": True,
            "paid": bool(e.get("paid_at")),
            "receipt": pending_receipts.get(db._norm_digits(e.get("phone") or "")) if e.get("phone") else None,
            "can_send": bool(e.get("phone")),
        })

    billable_rows = [
        it for it in (items + manual)
        if not it.get("paused") and not it.get("no_price")
    ]
    grand_total = sum(it["total"] for it in billable_rows)
    # "A receber" = total das linhas cobráveis que AINDA NÃO foram marcadas como pagas.
    unpaid_total = sum(it["total"] for it in billable_rows if not it.get("paid"))
    paid_total = grand_total - unpaid_total

    # Histórico do que JÁ foi enviado neste mês de referência (auditoria).
    history = db.get_billing_logs_for_month(tid, month_str)
    history_total = sum(float(h.get("total_amount") or 0) for h in history)

    return {"month": month_str, "items": items, "manual": manual,
            "grand_total": grand_total, "unpaid_total": unpaid_total,
            "paid_total": paid_total, "global_paused": global_paused,
            "history": history, "history_total": history_total}


@app.get("/dashboard/api/billing/diagnose")
def dash_billing_diagnose(request: Request, month: str | None = None):
    """Diagnóstico READ-ONLY da contagem de sessões do mês (não envia, não altera).

    Mostra, paciente por paciente com valor cadastrado:
    - quantas sessões a cobrança ESTÁ contando (com dedup, = o que o painel mostra)
    - quantas sessões casam com o telefone SEM dedup (raw)
    - cada linha do mês (data, telefone gravado, presença, cancelada, futura)
    e detecta as 2 causas clássicas de divergência:
    - COLISÃO: uma mesma sessão casa com >1 paciente (variantes de telefone se
      sobrepõem → a 1ª em ordem alfabética "rouba" → outra fica a menos)
    - DUPLICADA: 2+ linhas pro mesmo telefone+horário (conta a mais)

    Aceita token via header X-Dashboard-Token ou ?token= (abrível no navegador)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _Zi
    from calendar import monthrange as _mr
    token = request.headers.get("X-Dashboard-Token", "") or request.query_params.get("token", "")
    tenant = _get_tenant_by_token(token)
    now = _dt.now(_Zi("America/Sao_Paulo")).replace(tzinfo=None)
    if month:
        try:
            y, m = [int(x) for x in month.split("-")]
        except Exception:
            y, m = now.year, now.month
    else:
        y, m = now.year, now.month
    month_str = f"{y:04d}-{m:02d}"
    month_start = f"{y:04d}-{m:02d}-01T00:00:00"
    last_day = _mr(y, m)[1]
    month_end = f"{y:04d}-{m:02d}-{last_day:02d}T23:59:59"
    now_str = now.isoformat(timespec="seconds")
    tid = tenant["id"]

    # 1) Por paciente com valor: casamento por variantes (espelha a cobrança),
    #    SEM o dedup de billed_ids → mostra o "bruto" que cada um reivindica.
    id_to_claimers: dict = {}
    patients_out = []
    for patient in db.get_patients_with_price(tid):
        phone = patient["phone"]
        if not phone:
            continue
        sessions = db.get_valid_sessions_for_month(tid, phone, month_start, month_end, now_str)
        rows = []
        for s in sessions:
            sid = s.get("id")
            rows.append({
                "id": sid,
                "scheduled_at": s.get("scheduled_at"),
                "phone_gravado": s.get("phone"),
                "nome_gravado": s.get("patient_name"),
                "attendance": s.get("attendance"),
                "cancelled": s.get("cancelled"),
            })
            if sid is not None:
                id_to_claimers.setdefault(sid, []).append(patient.get("name") or phone)
        patients_out.append({
            "name": patient.get("name") or phone,
            "phone_cadastrado": phone,
            "variantes": sorted(db._phone_variants(phone)),
            "raw_match": len(sessions),
            "sessions": rows,
        })

    # 2) Replica o dedup REAL da cobrança (1º paciente alfabético fica com a sessão)
    #    para mostrar o "como está sendo cobrado" ao lado do bruto.
    billed_ids: set = set()
    billed_count: dict = {}
    for patient in db.get_patients_with_price(tid):
        phone = patient["phone"]
        if not phone:
            continue
        sessions = db.get_valid_sessions_for_month(tid, phone, month_start, month_end, now_str)
        sessions = [s for s in sessions if s.get("id") not in billed_ids]
        for s in sessions:
            if s.get("id") is not None:
                billed_ids.add(s["id"])
        billed_count[patient.get("name") or phone] = len(sessions)
    for p in patients_out:
        p["cobrado_agora"] = billed_count.get(p["name"], 0)

    # 3) COLISÕES: sessões reivindicadas por mais de um paciente cadastrado.
    collisions = [
        {"appointment_id": sid, "reivindicada_por": names}
        for sid, names in id_to_claimers.items() if len(names) > 1
    ]

    # 4) DUPLICADAS: mesma pessoa (telefone normalizado) + mesmo horário, 2+ linhas.
    raw_month = db.get_month_appointments_raw(tid, month_start, month_end, now_str)
    dup_map: dict = {}
    for a in raw_month:
        if not a.get("counts_for_billing"):
            continue
        key = (db._norm_digits(a.get("phone") or ""), a.get("scheduled_at") or "")
        dup_map.setdefault(key, []).append(a)
    duplicates = [
        {"phone": k[0], "scheduled_at": k[1],
         "ids": [x["id"] for x in v], "nomes": sorted({x.get("patient_name") or "" for x in v})}
        for k, v in dup_map.items() if len(v) > 1
    ]

    return {
        "month": month_str,
        "patients": sorted(patients_out, key=lambda x: (x["name"] or "").lower()),
        "collisions": collisions,
        "duplicates": duplicates,
        "raw_month_rows": raw_month,
        "totais": {
            "pacientes_com_valor": len(patients_out),
            "colisoes": len(collisions),
            "duplicadas": len(duplicates),
            "linhas_no_mes": len(raw_month),
        },
    }


@app.post("/admin/confirmacoes/disparar")
async def disparar_confirmacoes(request: Request):
    """Dispara confirmações de amanhã agora mesmo (ignora restrição de horário).

    Ação do dashboard: exige um token de painel válido (X-Dashboard-Token ou
    ?token=). Isenta da MASTER_KEY (ver _admin_auth_middleware)."""
    token = request.headers.get("X-Dashboard-Token", "") or request.query_params.get("token", "")
    _get_tenant_by_token(token)  # 403 se o token não corresponder a nenhum consultório
    results = await scheduler.run_confirmations_now()
    return {"enviados": len([r for r in results if r["sent"]]), "detalhes": results}


@app.post("/admin/security/encrypt-existing")
def encrypt_existing_data(include_conversations: bool = False):
    """Cifra em repouso os dados legados (texto puro) já gravados. Idempotente.
    Protegido pela MASTER_KEY (via _admin_auth_middleware). Requer
    FIELD_ENCRYPTION_KEY configurada. include_conversations=true também migra o
    histórico de conversas (mais demorado)."""
    import crypto
    if not crypto.is_enabled():
        raise HTTPException(
            status_code=400,
            detail="FIELD_ENCRYPTION_KEY não configurada — criptografia inativa.",
        )
    result = db.encrypt_existing_data(include_conversations=include_conversations)
    return {"status": "ok", **result}


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

@app.get("/termos", response_class=HTMLResponse)
def termos_de_uso(request: Request):
    return templates.TemplateResponse("termos.html", {"request": request})


@app.get("/privacidade", response_class=HTMLResponse)
def politica_privacidade(request: Request):
    return templates.TemplateResponse("privacidade.html", {"request": request})


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request):
    return templates.TemplateResponse("onboarding.html", {"request": request})


class OnboardingCreate(BaseModel):
    # Dados do consultório
    name: str
    psychologist_name: str
    working_hours_start: int = 8
    working_hours_end: int = 18
    session_minutes: int = 50
    # Dados de faturamento (obrigatórios)
    full_name: str            # Nome completo do responsável
    email: str
    phone: str                # Telefone de contato
    cpf_cnpj: str             # CPF ou CNPJ
    billing_zip: str          # CEP
    billing_address: str      # Rua
    billing_number: str       # Número
    billing_complement: str = ""
    billing_neighborhood: str # Bairro
    billing_city: str
    billing_state: str        # UF
    accept_terms: bool = False  # Aceite dos Termos de Uso e Política de Privacidade (LGPD)


def _only_digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _validate_cpf_cnpj(value: str) -> bool:
    digits = _only_digits(value)
    return len(digits) in (11, 14)


@app.post("/onboarding/create", status_code=201)
def onboarding_create(request: Request, body: OnboardingCreate):
    # Validação básica dos campos obrigatórios
    required = {
        "Nome do consultório": body.name,
        "Nome do responsável": body.full_name,
        "E-mail": body.email,
        "Telefone": body.phone,
        "CPF/CNPJ": body.cpf_cnpj,
        "CEP": body.billing_zip,
        "Endereço": body.billing_address,
        "Número": body.billing_number,
        "Bairro": body.billing_neighborhood,
        "Cidade": body.billing_city,
        "Estado (UF)": body.billing_state,
    }
    for label, value in required.items():
        if not (value or "").strip():
            raise HTTPException(status_code=400, detail=f"Campo obrigatório: {label}")

    if "@" not in body.email or "." not in body.email:
        raise HTTPException(status_code=400, detail="E-mail inválido.")
    if not _validate_cpf_cnpj(body.cpf_cnpj):
        raise HTTPException(status_code=400, detail="CPF/CNPJ inválido (use 11 dígitos para CPF ou 14 para CNPJ).")
    if len(_only_digits(body.phone)) < 10:
        raise HTTPException(status_code=400, detail="Telefone inválido (informe DDD + número).")
    if len(_only_digits(body.billing_zip)) != 8:
        raise HTTPException(status_code=400, detail="CEP deve ter 8 dígitos.")
    if len(body.billing_state.strip()) != 2:
        raise HTTPException(status_code=400, detail="UF deve ter 2 letras (ex: SP).")
    if not body.accept_terms:
        raise HTTPException(status_code=400, detail="É necessário aceitar os Termos de Uso e a Política de Privacidade.")

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

    # Gerar tokens e salvar todos os dados
    dash_token = secrets.token_urlsafe(24)
    setup_token = secrets.token_urlsafe(24)
    db.update_tenant(
        slug,
        dashboard_token=dash_token,
        setup_token=setup_token,
        email=body.email,
        full_name=body.full_name,
        phone=_only_digits(body.phone),
        cpf_cnpj=_only_digits(body.cpf_cnpj),
        billing_zip=_only_digits(body.billing_zip),
        billing_address=body.billing_address,
        billing_number=body.billing_number,
        billing_complement=body.billing_complement,
        billing_neighborhood=body.billing_neighborhood,
        billing_city=body.billing_city,
        billing_state=body.billing_state.upper().strip()[:2],
        status="pending_payment",
        accepted_terms_at=datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
        accepted_terms_version=TERMS_VERSION,
    )

    db.audit_log("tenant_created", actor=body.email, target=slug, ip=_client_ip(request),
                 details=f"name={body.name}, plan=pending_payment")

    # Enviar e-mail de boas-vindas com link de pagamento
    try:
        email_svc.send_welcome_email(
            email=body.email,
            name=body.full_name or body.psychologist_name or body.name,
            setup_token=setup_token,
        )
    except Exception as _e:
        logger.warning(f"[onboarding] Falha ao enviar e-mail de boas-vindas: {_e}")

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
    wt = db.ensure_webhook_token(tenant["id"])
    return {
        "name": tenant["name"],
        "slug": slug,
        "dashboard_url": f"{base}/dashboard/{slug}?token={dash_token}" if dash_token else "",
        "controle_url": f"{base}/controle/{dash_token}" if dash_token else "",
        "webhook_url": f"{base}/webhook/{slug}/zapi?token={wt}",
        "webhook_token": wt,
    }


# ── Recuperação de acesso ──────────────────────────────────────────────────────

class LinkRecoveryBody(BaseModel):
    email: str


@app.get("/recuperar-acesso", response_class=HTMLResponse)
def recuperar_acesso_page(request: Request):
    return templates.TemplateResponse("recuperar_acesso.html", {"request": request})


@app.post("/onboarding/recuperar-acesso")
def recuperar_acesso(body: LinkRecoveryBody, request: Request):
    """Envia o link do painel para o e-mail cadastrado."""
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="E-mail inválido.")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE lower(email) = ? AND status = 'active'",
            (email,)
        ).fetchone()

    if not row:
        # Resposta genérica para não vazar informação
        return {"status": "ok", "message": "Se o e-mail estiver cadastrado, você receberá as instruções."}

    tenant = dict(row)
    slug = tenant["slug"]
    dash_token = tenant.get("dashboard_token", "")
    name = tenant.get("full_name") or tenant.get("psychologist_name") or tenant.get("name") or ""

    if dash_token:
        try:
            email_svc.send_link_recovery_email(
                email=tenant["email"],
                name=name,
                slug=slug,
                dashboard_token=dash_token,
            )
        except Exception as _e:
            logger.warning(f"[recuperar-acesso] Falha ao enviar e-mail: {_e}")

    db.audit_log("link_recovery", actor=email, target=slug, ip=_client_ip(request))
    return {"status": "ok", "message": "Se o e-mail estiver cadastrado, você receberá as instruções."}


# ── Painel Master ──────────────────────────────────────────────────────────────

def _check_master_key(key: str):
    if not config.MASTER_KEY or not hmac.compare_digest(key or "", config.MASTER_KEY):
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
        wt = db.ensure_webhook_token(t["id"])
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
            "webhook_url": f"{base}/webhook/{t['slug']}/zapi?token={wt}",
        })
    return {"tenants": result}


@app.post("/master/backup/run")
async def master_run_backup(key: str = ""):
    """Dispara backup off-site agora (idempotente — só sobe se ainda não subiu hoje).
    Requer MASTER_KEY. Retorna 503 se BACKUP_S3_* não estiver configurado.
    """
    _check_master_key(key)
    try:
        import backup_service
        result = backup_service.run_backup_if_due()
        if result.get("status") == "skipped" and result.get("reason") == "not_configured":
            raise HTTPException(status_code=503, detail="Backup S3 não configurado (BACKUP_S3_* env vars ausentes).")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup falhou: {e}")


@app.get("/master/sentry-test")
async def master_sentry_test(key: str = ""):
    """Dispara um erro de propósito para validar integração com Sentry.
    Requer MASTER_KEY. Use uma vez após configurar SENTRY_DSN e depois ignore.
    """
    _check_master_key(key)
    raise RuntimeError("Sentry test error — se você está vendo isso no Sentry, está funcionando ✓")


@app.post("/master/tenants/{slug}/fix-zapi-webhook")
async def master_fix_zapi_webhook(slug: str, key: str = ""):
    """Atualiza a URL de webhook no Z-API para a URL atual (com ?token=...).

    Usado quando a validação de token foi adicionada e o Z-API ficou apontando
    pra URL antiga. Configura os três callbacks (received/delivery/sent) via
    API REST do Z-API. Requer MASTER_KEY.
    """
    _check_master_key(key)
    tenant = _get_tenant(slug)
    instance_id = tenant.get("evolution_instance", "")
    zapi_token = tenant.get("evolution_key", "")
    client_token = tenant.get("evolution_url", "")  # convenção interna: client_token vive aqui
    if not instance_id or not zapi_token:
        raise HTTPException(status_code=400, detail="Tenant sem credenciais Z-API configuradas.")
    wt = db.ensure_webhook_token(tenant["id"])
    base = config.BASE_URL
    webhook_url = f"{base}/webhook/{slug}/zapi?token={wt}"

    import httpx
    headers = {"Content-Type": "application/json"}
    if client_token:
        headers["Client-Token"] = client_token

    endpoints = {
        "received":      f"https://api.z-api.io/instances/{instance_id}/token/{zapi_token}/update-webhook-received",
        "received-delivery": f"https://api.z-api.io/instances/{instance_id}/token/{zapi_token}/update-webhook-delivery",
        "message-status": f"https://api.z-api.io/instances/{instance_id}/token/{zapi_token}/update-webhook-message-status",
    }
    results = {}
    async with httpx.AsyncClient(timeout=15) as cli:
        for name, url in endpoints.items():
            try:
                r = await cli.put(url, json={"value": webhook_url}, headers=headers)
                results[name] = {"status": r.status_code, "body": r.text[:200]}
            except Exception as e:
                results[name] = {"error": str(e)}
    return {"slug": slug, "webhook_url": webhook_url, "zapi_results": results}


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


@app.get("/dashboard/api/google/status")
def google_status(request: Request):
    """Testa se o Google Calendar está funcionando para este tenant."""
    token = request.headers.get("X-Dashboard-Token", "")
    tenant = _get_tenant_by_token(token)
    if not tenant.get("google_refresh_token"):
        return {"connected": False, "reason": "Sem token de autorização — conecte o Google Calendar nas configurações."}
    service = gcal._get_service(tenant)
    if not service:
        return {"connected": False, "reason": "Falha ao autenticar — token pode ter expirado ou sido revogado. Desconecte e reconecte."}
    try:
        cal_id = tenant.get("google_calendar_id") or "primary"
        info = service.calendars().get(calendarId=cal_id).execute()
        return {"connected": True, "calendar": info.get("summary", cal_id)}
    except Exception as e:
        return {"connected": False, "reason": str(e)}


# ── Pagamento ──────────────────────────────────────────────────────────────────

@app.get("/onboarding/pagamento", response_class=HTMLResponse)
def payment_page(request: Request, token: str = "", cancelled: int = 0, suspended: int = 0):
    tenant = db.get_tenant_by_setup_token(token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Link inválido.")
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "setup_token": token,
        "tenant": tenant,
        "cancelled": bool(cancelled),
        "suspended": bool(suspended),
    })


@app.post("/checkout/stripe")
async def checkout_stripe(request: Request):
    form = await request.form()
    setup_token = form.get("setup_token", "")
    plan = form.get("plan", "mensal")
    tenant = db.get_tenant_by_setup_token(setup_token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Token inválido.")
    try:
        url = stripe_svc.create_checkout_session(tenant, plan=plan)
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
    plan = form.get("plan", "mensal")
    tenant = db.get_tenant_by_setup_token(setup_token)
    if not tenant:
        raise HTTPException(status_code=404, detail="Token inválido.")
    try:
        url = mp_svc.create_subscription(tenant, plan=plan)
        return RedirectResponse(url, status_code=303)
    except Exception as e:
        logger.exception(f"MP checkout error: {e}")
        raise HTTPException(status_code=500, detail="Erro ao iniciar pagamento. Tente novamente.")


@app.post("/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    # Validação opcional: se MP_WEBHOOK_SECRET estiver configurado, exige x-signature válida
    secret = _os.getenv("MP_WEBHOOK_SECRET", "")
    if not secret:
        # Falha fechada: sem secret, qualquer um poderia ativar/suspender tenants.
        logger.error("[mp] Webhook REJEITADO — MP_WEBHOOK_SECRET não configurado")
        raise HTTPException(status_code=503, detail="Webhook não configurado.")
    x_sig = request.headers.get("x-signature", "")
    x_req = request.headers.get("x-request-id", "")
    raw   = await request.body()
    if not _verify_mp_signature(secret, x_sig, x_req, raw, request.query_params.get("data.id", "")):
        logger.warning(f"[mp] Webhook REJEITADO — assinatura inválida")
        raise HTTPException(status_code=403, detail="Assinatura inválida.")
    import json as _json
    data = _json.loads(raw.decode("utf-8") or "{}")
    result = mp_svc.handle_webhook(data)
    return result


def _verify_mp_signature(secret: str, x_signature: str, x_request_id: str, body: bytes, data_id: str) -> bool:
    """Verifica HMAC-SHA256 conforme docs do Mercado Pago.
    x-signature vem no formato: 'ts=NNN,v1=HEX_HMAC'. O manifest é:
    'id:DATA_ID;request-id:REQ_ID;ts:TS;'"""
    import hmac as _hmac, hashlib as _hashlib
    try:
        parts = dict(p.split("=", 1) for p in x_signature.split(",") if "=" in p)
        ts = parts.get("ts", "")
        v1 = parts.get("v1", "")
        if not ts or not v1:
            return False
        manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
        expected = _hmac.new(secret.encode(), manifest.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(expected, v1)
    except Exception as e:
        logger.warning(f"[mp] erro validando assinatura: {e}")
        return False


# ── Landing page (com tracking + depoimentos dinâmicos) ────────────────────────

import json as _json

DEFAULT_TESTIMONIALS = [
    {"name": "Marina R.", "role": "Psicóloga clínica · São Paulo", "initial": "M", "stars": 5,
     "text": "Antes eu ficava ansiosa no intervalo entre sessões, sempre olhando o WhatsApp. Hoje o agente cuida disso e eu consigo <strong>realmente descansar</strong> entre os atendimentos."},
    {"name": "Camila F.", "role": "Terapeuta · Rio de Janeiro", "initial": "C", "stars": 5, "highlight": True,
     "text": "Tive <strong>40% menos faltas no primeiro mês</strong>. O lembrete automático com a política de cobrança fez uma diferença enorme. Vale muito mais do que pago."},
    {"name": "Letícia M.", "role": "Psicóloga · Curitiba", "initial": "L", "stars": 5,
     "text": "Minha preocupação era parecer fria para os pacientes. Mas o agente escreve de um jeito tão humanizado que <strong>vários pacientes nem perceberam</strong> que era automático."},
    {"name": "Bruna P.", "role": "Psicóloga · Florianópolis", "initial": "B", "stars": 5,
     "text": "Ganhei <strong>2 horas livres por dia</strong>. Os pacientes adoraram a agilidade nas respostas, e eu consegui pegar mais sessões com a agenda otimizada."},
    {"name": "Patrícia S.", "role": "Psicanalista · Belo Horizonte", "initial": "P", "stars": 5,
     "text": "Comecei achando que ia ser uma dor de cabeça configurar, mas em <strong>10 minutos estava tudo pronto</strong>. O suporte respondeu rapidíssimo no WhatsApp."},
    {"name": "Fernanda A.", "role": "Psicóloga infantil · Porto Alegre", "initial": "F", "stars": 5,
     "text": "O melhor é a confirmação na noite anterior — <strong>quase ninguém esquece a sessão</strong>. Reduziu drasticamente os no-shows que eram um problema crônico para mim."},
]


def _get_testimonials() -> list[dict]:
    raw = db.get_site_content("testimonials", "")
    if not raw:
        return DEFAULT_TESTIMONIALS
    try:
        data = _json.loads(raw)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return DEFAULT_TESTIMONIALS


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    # Tracking não-bloqueante
    try:
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        ref = request.headers.get("referer", "")
        db.record_landing_view(ip=ip, user_agent=ua, referrer=ref)
    except Exception as e:
        logger.warning(f"[landing] tracking falhou: {e}")
    return templates.TemplateResponse(
        "landing.html",
        {"request": request, "testimonials": _get_testimonials()},
    )


# ════════════════════════════════════════════════════════════════════════════
# Painel Admin (login + dashboard + APIs)
# ════════════════════════════════════════════════════════════════════════════

_ADMIN_COOKIE = "admin_session"


def _require_admin(request: Request) -> dict:
    """Lê cookie de sessão e valida. Retorna a sessão ou levanta 401."""
    token = request.cookies.get(_ADMIN_COOKIE, "")
    session = db.admin_get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Não autenticado.")
    return session


@app.get("/painel/login", response_class=HTMLResponse)
def painel_login_page(request: Request, erro: str = ""):
    return templates.TemplateResponse("admin_login.html", {"request": request, "erro": erro})


class AdminLoginBody(BaseModel):
    username: str
    password: str
    totp: Optional[str] = ""


@app.post("/painel/login")
def painel_login(request: Request, body: AdminLoginBody):
    username = body.username.strip()
    ip = _client_ip(request)
    # Lockout: 8 falhas em 15 min para mesmo user OU IP
    if db.is_account_locked(username, ip, threshold=8, minutes=15):
        db.audit_log("admin_login_locked", actor=username, ip=ip)
        raise HTTPException(status_code=429, detail="Muitas tentativas falhas. Aguarde 15 minutos.")
    admin = db.admin_verify_login(username, body.password)
    if not admin:
        db.record_login_attempt(username, ip, success=False)
        db.audit_log("admin_login_failed", actor=username, ip=ip)
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")
    # Se TOTP estiver ativo, exigir código
    totp = db.admin_get_totp(admin["username"])
    if totp and totp["enabled"]:
        code = (body.totp or "").strip()
        if not code:
            raise HTTPException(status_code=401, detail="2FA_REQUIRED")
        if not totp_verify(totp["secret"], code):
            db.record_login_attempt(username, ip, success=False)
            db.audit_log("admin_login_failed", actor=username, ip=ip, details="invalid_totp")
            raise HTTPException(status_code=401, detail="Código 2FA inválido.")
    db.record_login_attempt(username, ip, success=True)
    db.clear_login_attempts(username)
    db.audit_log("admin_login_success", actor=admin["username"], ip=ip)
    token = db.admin_create_session(admin["username"], days=7)
    resp = JSONResponse({"ok": True, "username": admin["username"]})
    resp.set_cookie(
        _ADMIN_COOKIE, token,
        httponly=True, samesite="lax", max_age=7 * 24 * 3600,
        secure=config.BASE_URL.startswith("https"),
    )
    return resp


@app.post("/painel/logout")
def painel_logout(request: Request):
    token = request.cookies.get(_ADMIN_COOKIE, "")
    if token:
        db.admin_delete_session(token)
    resp = RedirectResponse("/painel/login", status_code=303)
    resp.delete_cookie(_ADMIN_COOKIE)
    return resp


@app.get("/painel", response_class=HTMLResponse)
def painel_home(request: Request):
    token = request.cookies.get(_ADMIN_COOKIE, "")
    if not db.admin_get_session(token):
        return RedirectResponse("/painel/login", status_code=303)
    return templates.TemplateResponse("admin_panel.html", {"request": request})


# ── APIs do painel ────────────────────────────────────────────────────────────

@app.get("/painel/api/stats")
def painel_api_stats(request: Request):
    _require_admin(request)
    return db.admin_stats_overview()


@app.get("/painel/api/subscriptions")
def painel_api_subscriptions(request: Request):
    _require_admin(request)
    return {"subscriptions": db.admin_list_subscriptions()}


@app.get("/painel/api/abandoned-carts")
def painel_api_abandoned(request: Request, hours_min: int = 1):
    _require_admin(request)
    items = db.admin_list_abandoned_carts(hours_min=hours_min)
    base = config.BASE_URL
    for it in items:
        st = it.get("setup_token") or ""
        it["payment_url"] = f"{base}/onboarding/pagamento?token={st}" if st else ""
    return {"carts": items}


@app.get("/painel/api/tenants")
def painel_api_tenants(request: Request):
    _require_admin(request)
    items = db.admin_list_all_tenants()
    base = config.BASE_URL
    for it in items:
        dt = it.get("dashboard_token") or ""
        st = it.get("setup_token") or ""
        it["dashboard_url"] = f"{base}/dashboard/{it['slug']}?token={dt}" if dt else ""
        it["payment_url"]   = f"{base}/onboarding/pagamento?token={st}" if st else ""
    return {"tenants": items}


@app.get("/painel/api/tenant/{slug}")
def painel_api_tenant_detail(slug: str, request: Request):
    _require_admin(request)
    t = db.admin_get_tenant_full(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Não encontrado.")
    # Não retorna senhas
    for k in ("twilio_token", "evolution_key", "caldav_password"):
        t.pop(k, None)
    return t


class AdminTenantAction(BaseModel):
    free_until: Optional[str] = None    # ISO date
    plan: Optional[str] = None          # mensal/semestral/anual
    status: Optional[str] = None        # active/suspended


@app.post("/painel/api/tenant/{slug}/action")
def painel_api_tenant_action(slug: str, body: AdminTenantAction, request: Request):
    _require_admin(request)
    tenant = db.get_tenant(slug)
    if not tenant:
        raise HTTPException(status_code=404, detail="Não encontrado.")

    updates = {}
    if body.status:
        if body.status not in ("active", "suspended", "pending_payment"):
            raise HTTPException(status_code=400, detail="Status inválido.")
        updates["status"] = body.status
    if body.plan:
        if body.plan not in ("mensal", "semestral", "anual"):
            raise HTTPException(status_code=400, detail="Plano inválido.")
        updates["plan"] = body.plan
    if body.free_until is not None:
        updates["free_until"] = body.free_until or None

    if not updates:
        raise HTTPException(status_code=400, detail="Nada para atualizar.")

    db.update_tenant(slug, **updates)
    logger.info(f"[admin] tenant {slug} atualizado: {updates}")
    return {"ok": True, "updates": updates}


# ── Lembretes de vencimento (contas do operador + Z-API por consultório) ────────

@app.get("/painel/api/op-bills")
def painel_api_op_bills_list(request: Request):
    _require_admin(request)
    return {"bills": db.op_bills_list(only_active=False)}


class OpBillBody(BaseModel):
    label: str
    due_date: str                       # YYYY-MM-DD
    amount: Optional[float] = 0.0
    recurrence: Optional[str] = "monthly"  # none | monthly | yearly
    notes: Optional[str] = ""
    active: Optional[int] = 1


@app.post("/painel/api/op-bills")
def painel_api_op_bills_add(body: OpBillBody, request: Request):
    _require_admin(request)
    if not body.label.strip():
        raise HTTPException(status_code=400, detail="Informe o nome da conta.")
    try:
        datetime.fromisoformat(body.due_date[:10])
    except Exception:
        raise HTTPException(status_code=400, detail="Data de vencimento inválida (use AAAA-MM-DD).")
    if (body.recurrence or "monthly") not in ("none", "monthly", "yearly"):
        raise HTTPException(status_code=400, detail="Recorrência inválida.")
    bill_id = db.op_bill_add(
        label=body.label, due_date=body.due_date, amount=body.amount or 0.0,
        recurrence=body.recurrence or "monthly", notes=body.notes or "",
    )
    logger.info(f"[admin] op_bill criada: {body.label} venc {body.due_date}")
    return {"ok": True, "id": bill_id}


@app.put("/painel/api/op-bills/{bill_id}")
def painel_api_op_bills_update(bill_id: int, body: OpBillBody, request: Request):
    _require_admin(request)
    if body.due_date:
        try:
            datetime.fromisoformat(body.due_date[:10])
        except Exception:
            raise HTTPException(status_code=400, detail="Data de vencimento inválida.")
    ok = db.op_bill_update(
        bill_id, label=body.label, due_date=body.due_date, amount=body.amount or 0.0,
        recurrence=body.recurrence or "monthly", notes=body.notes or "",
        active=1 if body.active is None else int(body.active),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return {"ok": True}


@app.delete("/painel/api/op-bills/{bill_id}")
def painel_api_op_bills_delete(bill_id: int, request: Request):
    _require_admin(request)
    if not db.op_bill_delete(bill_id):
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return {"ok": True}


class ZapiExpiresBody(BaseModel):
    zapi_expires_at: Optional[str] = None   # YYYY-MM-DD ou null para limpar


@app.post("/painel/api/tenant/{slug}/zapi-expires")
def painel_api_set_zapi_expires(slug: str, body: ZapiExpiresBody, request: Request):
    _require_admin(request)
    if not db.get_tenant(slug):
        raise HTTPException(status_code=404, detail="Consultório não encontrado.")
    val = (body.zapi_expires_at or "").strip() or None
    if val:
        try:
            datetime.fromisoformat(val[:10])
        except Exception:
            raise HTTPException(status_code=400, detail="Data inválida (use AAAA-MM-DD).")
    db.update_tenant(slug, zapi_expires_at=val)
    logger.info(f"[admin] {slug} zapi_expires_at = {val}")
    return {"ok": True, "zapi_expires_at": val}


@app.post("/painel/api/billing-reminders/run")
async def painel_api_billing_reminders_run(request: Request):
    """Dispara a varredura de lembretes de vencimento agora (teste manual)."""
    _require_admin(request)
    import billing_reminders
    sent = await billing_reminders.run_reminders(force=True)
    return {"ok": True, "enviados": len(sent), "detalhes": sent}


# ── Saúde das instâncias de WhatsApp (monitor Z-API) ────────────────────────────

@app.get("/painel/api/instances/health")
def painel_api_instances_health(request: Request):
    """Estado conhecido (última checagem) de cada instância monitorada."""
    _require_admin(request)
    rows = {r["tenant_id"]: r for r in db.instance_health_all()}
    out = []
    for t in db.list_tenants():
        if (t.get("whatsapp_provider") or "").lower() != "zapi":
            continue
        h = rows.get(t["id"], {})
        conn = h.get("connected")
        out.append({
            "slug": t["slug"],
            "name": t.get("name"),
            "connected": (None if conn is None else bool(conn)),
            "down_since": h.get("down_since"),
            "last_checked": h.get("last_checked"),
            "last_error": h.get("last_error", ""),
        })
    return {"instances": out}


@app.post("/painel/api/instances/check")
async def painel_api_instances_check(request: Request):
    """Força uma varredura de saúde agora e retorna o resultado."""
    _require_admin(request)
    import instance_monitor
    res = await instance_monitor.monitor_once()
    return {"ok": True, "resultado": res}


@app.get("/painel/api/instances/{slug}/status")
async def painel_api_instance_status_live(slug: str, request: Request):
    """Consulta o status AO VIVO da instância Z-API de um consultório."""
    _require_admin(request)
    t = db.get_tenant(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Consultório não encontrado.")
    import whatsapp_service as _wa
    st = await _wa.get_zapi_status(t)
    return {"slug": slug, **st}


@app.get("/painel/api/content/{key}")
def painel_api_content_get(key: str, request: Request):
    _require_admin(request)
    raw = db.get_site_content(key, "")
    return {"key": key, "value": raw}


class ContentBody(BaseModel):
    value: str


@app.put("/painel/api/content/{key}")
def painel_api_content_put(key: str, body: ContentBody, request: Request):
    _require_admin(request)
    # Validar JSON se for testimonials (lista de objetos com campos esperados)
    if key == "testimonials":
        try:
            data = _json.loads(body.value)
            if not isinstance(data, list):
                raise ValueError("JSON deve ser uma lista")
            for item in data:
                if not all(k in item for k in ("name", "role", "text")):
                    raise ValueError("Cada depoimento precisa ter: name, role, text")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"JSON inválido: {e}")
    db.set_site_content(key, body.value)
    return {"ok": True}


@app.get("/painel/api/content-defaults/testimonials")
def painel_api_testimonials_default(request: Request):
    _require_admin(request)
    return {"defaults": DEFAULT_TESTIMONIALS}


# ── Health ─────────────────────────────────────────────────────────────────────

@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    """Healthcheck simples (uptime probe). Aceita HEAD para UptimeRobot etc."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """Healthcheck profundo: testa conexão com o banco."""
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()
        return {
            "status": "ok",
            "db": "ok",
            "tenants_count": int(row["n"]) if row else 0,
            "ts": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
            "sentry": bool(_os.getenv("SENTRY_DSN", "").strip()),
        }
    except Exception as e:
        logger.error(f"healthz falhou: {e}")
        return JSONResponse(
            {"status": "degraded", "db": "fail", "error": str(e)[:200]},
            status_code=503,
        )


@app.get("/painel/api/audit")
def painel_api_audit(request: Request, limit: int = 100):
    _require_admin(request)
    return {"items": db.audit_list(limit=min(limit, 500))}


# ── 2FA TOTP do admin ────────────────────────────────────────────────────────

@app.get("/painel/api/2fa/status")
def painel_2fa_status(request: Request):
    session = _require_admin(request)
    info = db.admin_get_totp(session["username"]) or {"secret": "", "enabled": False}
    return {"enabled": info["enabled"]}


@app.post("/painel/api/2fa/setup")
def painel_2fa_setup(request: Request):
    """Gera secret novo e retorna URI para QR code (não ativa ainda)."""
    session = _require_admin(request)
    secret = totp_generate_secret()
    # Salva como NÃO habilitado — só ativa após verificação
    db.admin_set_totp(session["username"], secret, enabled=False)
    uri = totp_provisioning_uri(secret, account=session["username"])
    return {"secret": secret, "otpauth_uri": uri}


class Confirm2FABody(BaseModel):
    code: str


@app.post("/painel/api/2fa/enable")
def painel_2fa_enable(request: Request, body: Confirm2FABody):
    session = _require_admin(request)
    info = db.admin_get_totp(session["username"])
    if not info or not info["secret"]:
        raise HTTPException(status_code=400, detail="Configure o 2FA primeiro (chame /setup).")
    if not totp_verify(info["secret"], body.code.strip()):
        raise HTTPException(status_code=400, detail="Código inválido. Tente novamente.")
    db.admin_set_totp(session["username"], info["secret"], enabled=True)
    db.audit_log("admin_2fa_enabled", actor=session["username"], ip=_client_ip(request))
    return {"enabled": True}


@app.post("/painel/api/2fa/disable")
def painel_2fa_disable(request: Request, body: Confirm2FABody):
    session = _require_admin(request)
    info = db.admin_get_totp(session["username"])
    if not info or not info["enabled"]:
        return {"enabled": False}
    # Para desabilitar exige código válido (anti-sequestro de sessão)
    if not totp_verify(info["secret"], body.code.strip()):
        raise HTTPException(status_code=400, detail="Código inválido.")
    db.admin_disable_totp(session["username"])
    db.audit_log("admin_2fa_disabled", actor=session["username"], ip=_client_ip(request))
    return {"enabled": False}
