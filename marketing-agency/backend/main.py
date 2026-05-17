import os
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import auth, clients, agents, content, analytics, calendar, social, persona, inspirations, products, knowledge, strategy, trends, billing

logger = logging.getLogger(__name__)

# --- Sentry (optional) -----------------------------------------------------
# Enable by setting SENTRY_DSN. Captures unhandled exceptions + slow requests.
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("SENTRY_ENV", "production"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE", "0.1")),
            integrations=[FastApiIntegration(), StarletteIntegration()],
            send_default_pii=False,
            release=os.getenv("RAILWAY_GIT_COMMIT_SHA"),
        )
        logger.info("Sentry initialized")
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")

# --- Rate limiting ---------------------------------------------------------
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

def _key_func(request: Request) -> str:
    # Rate-limit by authenticated user when possible, else by IP
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return f"user:{auth_header[7:32]}"  # token prefix is a stable per-user key
    return get_remote_address(request)

limiter = Limiter(key_func=_key_func, default_limits=["120/minute"])

app = FastAPI(title="Content Agency AI", version="2.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(clients.router)
app.include_router(agents.router)
app.include_router(content.router)
app.include_router(analytics.router)
app.include_router(calendar.router)
app.include_router(social.router)
app.include_router(persona.router)
app.include_router(inspirations.router)
app.include_router(products.router)
app.include_router(knowledge.router)
app.include_router(strategy.router)
app.include_router(trends.router)
app.include_router(billing.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.on_event("startup")
async def startup():
    init_db()
    _seed_users()
    # Background jobs: auto-publish due slots + auto-fetch IG metrics.
    # Safe to disable via DISABLE_SCHEDULER=1 (useful for local dev / tests).
    if os.getenv("DISABLE_SCHEDULER") != "1":
        from services.scheduler import start_scheduler
        start_scheduler()


@app.on_event("shutdown")
async def shutdown():
    if os.getenv("DISABLE_SCHEDULER") != "1":
        from services.scheduler import stop_scheduler
        stop_scheduler()


def _seed_users():
    from database import SessionLocal
    from models import User
    from auth import hash_password

    SEED = [
        {"email": "wagalan@gmail.com", "password": os.getenv("SEED_WAGNER_PASSWORD", "@l61310788"), "name": "Wagner", "role": "master"},
        {"email": "brunaparolin6@gmail.com", "password": os.getenv("SEED_BRUNA_PASSWORD", "231981"), "name": "Bruna", "role": "master"},
    ]

    db = SessionLocal()
    try:
        for s in SEED:
            if not db.query(User).filter(User.email == s["email"]).first():
                db.add(User(
                    email=s["email"],
                    password_hash=hash_password(s["password"]),
                    name=s["name"],
                    role=s["role"],
                ))
        db.commit()
    finally:
        db.close()


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
