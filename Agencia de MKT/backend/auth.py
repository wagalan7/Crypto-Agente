import os
import time
import json
import hmac
import hashlib
import base64
from pathlib import Path
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET = os.getenv("JWT_SECRET", "mkt-agency-secret-2024")
USERS_FILE = Path("/tmp/mkt_users.json")


def _load_users() -> list[dict]:
    # 1. try local file (runtime additions)
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            pass
    # 2. fallback to env var
    raw = os.getenv("USERS_JSON", "")
    if raw:
        try:
            users = json.loads(raw)
            USERS_FILE.write_text(json.dumps(users))
            return users
        except Exception:
            pass
    # 3. fallback defaults
    defaults = [{"user": os.getenv("ADMIN_USER", "admin"),
                 "pass": os.getenv("ADMIN_PASS", "admin123"),
                 "role": "admin"}]
    USERS_FILE.write_text(json.dumps(defaults))
    return defaults


def _save_users(users: list[dict]):
    USERS_FILE.write_text(json.dumps(users))


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


def authenticate(username: str, password: str) -> str:
    users = _load_users()
    for u in users:
        if u["user"] == username and u["pass"] == password:
            return create_token(username)
    raise HTTPException(status_code=401, detail="Usuário ou senha incorretos")


def get_user_role(username: str) -> str:
    for u in _load_users():
        if u["user"] == username:
            return u.get("role", "user")
    return "user"


def require_admin(username: str):
    if get_user_role(username) != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")


# CRUD
def list_users() -> list[dict]:
    return [
        {"user": u["user"], "role": u.get("role", "user"), "name": u.get("name", "")}
        for u in _load_users()
    ]


def add_user(username: str, password: str, role: str = "user", name: str = "") -> dict:
    users = _load_users()
    if any(u["user"] == username for u in users):
        raise HTTPException(status_code=400, detail="Usuário já existe")
    users.append({"user": username, "pass": password, "role": role, "name": name})
    _save_users(users)
    return {"user": username, "role": role, "name": name}


def delete_user(username: str, requester: str):
    if username == requester:
        raise HTTPException(status_code=400, detail="Não pode remover a si mesmo")
    users = _load_users()
    new = [u for u in users if u["user"] != username]
    if len(new) == len(users):
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    _save_users(new)


def update_user(username: str, requester: str,
                new_username: str | None = None,
                new_password: str | None = None,
                new_name: str | None = None) -> dict:
    """Admin can update anyone. Regular user can only update their own profile."""
    role = get_user_role(requester)
    if role != "admin" and username != requester:
        raise HTTPException(status_code=403, detail="Sem permissão")
    users = _load_users()
    # Check new username not already taken
    if new_username and new_username != username:
        if any(u["user"] == new_username for u in users):
            raise HTTPException(status_code=400, detail="Nome de usuário já existe")
    for u in users:
        if u["user"] == username:
            if new_name is not None:
                u["name"] = new_name
            if new_password:
                u["pass"] = new_password
            if new_username and new_username != username:
                u["user"] = new_username
            _save_users(users)
            return {"user": u["user"], "role": u.get("role", "user"), "name": u.get("name", "")}
    raise HTTPException(status_code=404, detail="Usuário não encontrado")


def update_password(username: str, new_pass: str, requester: str):
    update_user(username, requester, new_password=new_pass)


security = HTTPBearer(auto_error=False)


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    token = creds.credentials if creds else request.cookies.get("mkt_token")
    if not token:
        raise HTTPException(status_code=401, detail="Autenticação necessária")
    return verify_token(token)
