from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from database import get_db
from models import User, Client, ClientAccess
from auth import hash_password, verify_password, create_token, get_current_user, require_master
from typing import Optional, List

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreate(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    role: str = "user"


class GrantAccessRequest(BaseModel):
    user_id: int
    client_id: int


def _serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email.lower().strip()).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email ou senha incorretos")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta desativada")
    token = create_token(user.id, user.email, user.role)
    return {"access_token": token, "token_type": "bearer", "user": _serialize_user(user)}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)


@router.get("/users")
def list_users(current_user: User = Depends(require_master), db: Session = Depends(get_db)):
    users = db.query(User).filter(User.id != current_user.id).order_by(User.created_at).all()
    return [_serialize_user(u) for u in users]


@router.post("/users")
def create_user(data: UserCreate, current_user: User = Depends(require_master), db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email.lower()).first():
        raise HTTPException(400, "Email já cadastrado")
    user = User(
        email=data.email.lower().strip(),
        password_hash=hash_password(data.password),
        name=data.name or data.email.split("@")[0],
        role=data.role if data.role in ("master", "admin", "user") else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize_user(user)


@router.post("/grant-access")
def grant_access(data: GrantAccessRequest, current_user: User = Depends(require_master), db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == data.client_id).first()
    if not client:
        raise HTTPException(404, "Cliente não encontrado")
    # Only the owner (master) can grant access to their own clients
    if client.owner_id != current_user.id:
        raise HTTPException(403, "Apenas o dono do cliente pode conceder acesso")
    existing = db.query(ClientAccess).filter(
        ClientAccess.client_id == data.client_id,
        ClientAccess.user_id == data.user_id,
    ).first()
    if existing:
        return {"detail": "Acesso já concedido"}
    db.add(ClientAccess(client_id=data.client_id, user_id=data.user_id, granted_by=current_user.id))
    db.commit()
    return {"detail": "Acesso concedido"}


@router.get("/access/{client_id}")
def list_access(client_id: int, current_user: User = Depends(require_master), db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client or client.owner_id != current_user.id:
        raise HTTPException(403, "Sem permissão")
    grants = db.query(ClientAccess).filter(ClientAccess.client_id == client_id).all()
    return [{"user_id": g.user_id, "client_id": g.client_id} for g in grants]


@router.delete("/revoke-access")
def revoke_access(data: GrantAccessRequest, current_user: User = Depends(require_master), db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == data.client_id).first()
    if not client or client.owner_id != current_user.id:
        raise HTTPException(403, "Sem permissão")
    db.query(ClientAccess).filter(
        ClientAccess.client_id == data.client_id,
        ClientAccess.user_id == data.user_id,
    ).delete()
    db.commit()
    return {"detail": "Acesso revogado"}
