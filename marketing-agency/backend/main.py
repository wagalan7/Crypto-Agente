import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import auth, clients, agents, content, analytics, calendar, social, persona, inspirations, products, knowledge, strategy, trends

app = FastAPI(title="Content Agency AI", version="2.0.0")

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
