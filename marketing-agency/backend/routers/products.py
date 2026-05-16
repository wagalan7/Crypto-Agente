from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import Product, User
from auth import get_current_user, assert_client_access

router = APIRouter(prefix="/products", tags=["products"])


class ProductCreate(BaseModel):
    client_id: int
    name: str
    type: str = "service"
    price: Optional[str] = None
    description: Optional[str] = None
    pains_solved: List[str] = []
    desires: List[str] = []
    objections: List[str] = []
    transformation: Optional[str] = None
    awareness_stage: Optional[str] = None
    funnel_stage: Optional[str] = None
    is_primary: bool = False


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    price: Optional[str] = None
    description: Optional[str] = None
    pains_solved: Optional[List[str]] = None
    desires: Optional[List[str]] = None
    objections: Optional[List[str]] = None
    transformation: Optional[str] = None
    awareness_stage: Optional[str] = None
    funnel_stage: Optional[str] = None
    is_primary: Optional[bool] = None
    is_active: Optional[bool] = None


def _serialize(p: Product) -> dict:
    return {
        "id": p.id,
        "client_id": p.client_id,
        "name": p.name,
        "type": p.type,
        "price": p.price,
        "description": p.description,
        "pains_solved": p.pains_solved or [],
        "desires": p.desires or [],
        "objections": p.objections or [],
        "transformation": p.transformation,
        "awareness_stage": p.awareness_stage,
        "funnel_stage": p.funnel_stage,
        "is_primary": p.is_primary,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/client/{client_id}")
def list_products(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(Product).filter(Product.client_id == client_id).order_by(Product.is_primary.desc(), Product.created_at.desc()).all()
    return [_serialize(p) for p in items]


@router.post("/")
def create_product(data: ProductCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    if data.is_primary:
        # Demote existing primaries
        db.query(Product).filter(Product.client_id == data.client_id, Product.is_primary == True).update({"is_primary": False})
    p = Product(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.patch("/{product_id}")
def update_product(product_id: int, data: ProductUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(p.client_id, current_user, db)
    update = data.model_dump(exclude_unset=True)
    if update.get("is_primary"):
        db.query(Product).filter(Product.client_id == p.client_id, Product.is_primary == True).update({"is_primary": False})
    for k, v in update.items():
        setattr(p, k, v)
    db.commit()
    db.refresh(p)
    return _serialize(p)


@router.delete("/{product_id}")
def delete_product(product_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(p.client_id, current_user, db)
    db.delete(p)
    db.commit()
    return {"detail": "removido"}
