import os
import time
import json
import hmac
import hashlib
import base64
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET = os.getenv("JWT_SECRET", "mkt-agency-secret-2024")


# ── Token helpers ─────────────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_token(username: str) -> str:
    exp = int(time.time()) + 7 * 24 * 3600
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": username, "exp": exp}).encode()
    ).decode()
    sig = _sign(payload)
    return f"{payload}.{sig}"


def verify_token(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            raise ValueError("bad format")
        payload_b64, sig = parts
        if not hmac.compare_digest(_sign(payload_b64), sig):
            raise ValueError("bad signature")
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        if data["exp"] < int(time.time()):
            raise ValueError("expired")
        return data["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")


# ── Lazy import to avoid circular deps ────────────────────────────────────────

def _db():
    from db import db_get_user, db_list_users, db_add_user, db_update_user, db_delete_user
    return db_get_user, db_list_users, db_add_user, db_update_user, db_delete_user


# ── Auth ──────────────────────────────────────────────────────────────────────

def authenticate(username: str, password: str) -> str:
    db_get_user, *_ = _db()
    u = db_get_user(username)
    if u and u["pass"] == password:
        return create_token(username)
    raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")


def get_user_role(username: str) -> str:
    db_get_user, *_ = _db()
    u = db_get_user(username)
    return u["role"] if u else "user"


def require_admin(username: str):
    if get_user_role(username) != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")


# ── CRUD ──────────────────────────────────────────────────────────────────────

def list_users() -> list[dict]:
    _, db_list_users, *_ = _db()
    return db_list_users()


def add_user(username: str, password: str, role: str = "user", name: str = "") -> dict:
    db_get_user, _, db_add_user, *_ = _db()
    if db_get_user(username):
        raise HTTPException(status_code=400, detail="Usuário já existe")
    return db_add_user(username, password, role, name)


def delete_user(username: str, requester: str):
    if username == requester:
        raise HTTPException(status_code=400, detail="Não pode remover a si mesmo")
    db_get_user, _, __, ___, db_delete_user = _db()
    if not db_get_user(username):
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    db_delete_user(username)


def update_user(username: str, requester: str,
                new_username: str | None = None,
                new_password: str | None = None,
                new_name: str | None = None,
                new_role: str | None = None) -> dict:
    role = get_user_role(requester)
    if role != "admin" and username != requester:
        raise HTTPException(status_code=403, detail="Sem permissão")
    # Only admin can change role
    if new_role is not None and role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode alterar role")
    if new_role is not None and new_role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role inválida (use 'admin' ou 'user')")
    db_get_user, _, __, db_update_user, ___ = _db()
    u = db_get_user(username)
    if not u:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    # Check new username not taken
    if new_username and new_username != username:
        if db_get_user(new_username):
            raise HTTPException(status_code=400, detail="Nome de usuário já existe")
    # Prevent removing last admin
    if new_role == "user" and u.get("role") == "admin":
        from db import db_list_users
        admins = [x for x in db_list_users() if x.get("role") == "admin"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="Não é possível rebaixar o último admin")
    db_update_user(username, new_username=new_username,
                   new_password=new_password, new_name=new_name,
                   new_role=new_role)
    # Return updated record
    final = db_get_user(new_username if new_username else username)
    return {"user": final["user"], "role": final["role"], "name": final.get("name", "")}


def update_password(username: str, new_pass: str, requester: str):
    update_user(username, requester, new_password=new_pass)


# ── FastAPI dependency ────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    token = creds.credentials if creds else request.cookies.get("mkt_token")
    if not token:
        raise HTTPException(status_code=401, detail="Autenticação necessária")
    return verify_token(token)
