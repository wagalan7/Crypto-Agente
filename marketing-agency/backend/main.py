import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import auth, clients, agents, content, analytics, calendar, social

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


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.on_event("startup")
async def startup():
    init_db()
    _seed_users()


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
