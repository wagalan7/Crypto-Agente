from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import Product, User
from auth import get_current_user, assert_client_access

router = APIRouter(prefix="/products", tags=["products"])


PRODUCT_TEMPLATES = [
    {
        "key": "curso_digital",
        "label": "Curso digital",
        "defaults": {
            "type": "course",
            "awareness_stage": "solution",
            "funnel_stage": "middle",
            "pains_solved": ["não sabe por onde começar", "tentou sozinho e travou", "falta método claro"],
            "desires": ["dominar o assunto sem depender de ninguém", "ter resultado previsível", "sentir confiança"],
            "objections": ["é caro", "não vou ter tempo", "será que funciona pra mim?", "já comprei curso e não usei"],
            "transformation": "Sai da posição de iniciante perdido e vira alguém que executa com método e segurança.",
        },
    },
    {
        "key": "mentoria",
        "label": "Mentoria / Consultoria",
        "defaults": {
            "type": "mentorship",
            "awareness_stage": "product",
            "funnel_stage": "bottom",
            "pains_solved": ["está estagnado", "tomando decisões sozinho e errando", "sem alguém pra guiar"],
            "desires": ["acelerar resultado", "ter um especialista do lado", "evitar erros caros"],
            "objections": ["é caro demais", "preciso resolver sozinho", "não sei se tenho perfil pra mentoria"],
            "transformation": "Sai do achismo e passa a operar com acompanhamento de quem já fez o caminho.",
        },
    },
    {
        "key": "ecommerce",
        "label": "Produto físico / E-commerce",
        "defaults": {
            "type": "product",
            "awareness_stage": "problem",
            "funnel_stage": "top",
            "pains_solved": ["incômodo recorrente do dia a dia", "produto atual não resolve direito"],
            "desires": ["uma solução prática que funcione", "qualidade que dura", "conveniência"],
            "objections": ["será que vale o preço?", "frete demora", "e se não servir?", "não conheço a marca"],
            "transformation": "Resolve o problema concreto e deixa a rotina mais leve.",
        },
    },
    {
        "key": "saas",
        "label": "SaaS / Software",
        "defaults": {
            "type": "service",
            "awareness_stage": "solution",
            "funnel_stage": "middle",
            "pains_solved": ["processo manual consome tempo", "ferramentas atuais não se conversam", "dados espalhados"],
            "desires": ["automatizar o repetitivo", "ter visibilidade do que importa", "escalar sem aumentar equipe"],
            "objections": ["mais uma ferramenta?", "curva de aprendizado", "vai integrar com o que já uso?"],
            "transformation": "Sai do operacional manual e ganha tempo pra focar no estratégico.",
        },
    },
    {
        "key": "infoproduto",
        "label": "Infoproduto / E-book",
        "defaults": {
            "type": "ebook",
            "awareness_stage": "problem",
            "funnel_stage": "top",
            "pains_solved": ["precisa de uma resposta rápida e prática", "não quer comprometer com curso longo"],
            "desires": ["entender o essencial agora", "ter um material de referência", "ganho rápido"],
            "objections": ["info de graça no Google", "vai ser raso?", "vou ler mesmo?"],
            "transformation": "Em poucas horas a pessoa entende e aplica o essencial.",
        },
    },
    {
        "key": "comunidade",
        "label": "Comunidade / Assinatura",
        "defaults": {
            "type": "community",
            "awareness_stage": "product",
            "funnel_stage": "bottom",
            "pains_solved": ["estuda sozinho e desanima", "não tem com quem trocar", "perde consistência"],
            "desires": ["fazer parte de um grupo do mesmo nível", "manter o ritmo", "ter networking"],
            "objections": ["mensalidade pesa", "vou ter tempo de participar?", "preciso de comunidade?"],
            "transformation": "Sai do isolamento e entra num ambiente que sustenta a evolução contínua.",
        },
    },
]


@router.get("/templates")
def list_templates(current_user: User = Depends(get_current_user)):
    """Predefined product blueprints. Frontend uses these to pre-fill the create form."""
    return PRODUCT_TEMPLATES


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
