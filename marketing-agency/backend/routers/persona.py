from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Persona, User, ContentPiece
from auth import get_current_user, assert_client_access
from services import BrandBrain
from agents import PersonaAgent, parse_json_response

router = APIRouter(prefix="/persona", tags=["persona"])


def _serialize(p: Persona) -> dict:
    return {
        "id": p.id,
        "client_id": p.client_id,
        "pains": p.pains or [],
        "desires": p.desires or [],
        "emotions": p.emotions or [],
        "insecurities": p.insecurities or [],
        "audience_goals": p.audience_goals or [],
        "language_patterns": p.language_patterns or "",
        "psychological_patterns": p.psychological_patterns or "",
        "audience_profile": p.audience_profile or "",
        "evidence": p.evidence or "",
        "generated_at": p.generated_at.isoformat() if p.generated_at else None,
    }


@router.get("/client/{client_id}")
def get_persona(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    p = db.query(Persona).filter(Persona.client_id == client_id).first()
    if not p:
        return {"exists": False}
    return {"exists": True, **_serialize(p)}


@router.post("/client/{client_id}/generate")
async def generate_persona(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    contents = db.query(ContentPiece).filter(ContentPiece.client_id == client_id).order_by(ContentPiece.created_at.desc()).limit(10).all()
    samples = "\n---\n".join(
        f"[{c.format}] {c.title}\nHook: {c.hook or ''}\nCopy: {(c.copy or '')[:300]}"
        for c in contents
    )
    agent = PersonaAgent()
    raw = await agent.run(agent.build_prompt(brain["text"], samples))
    data = parse_json_response(raw)
    if not data:
        raise HTTPException(500, f"Falha ao gerar persona. Raw: {raw[:200]}")

    p = db.query(Persona).filter(Persona.client_id == client_id).first()
    if not p:
        p = Persona(client_id=client_id)
        db.add(p)
    p.pains = data.get("pains", [])
    p.desires = data.get("desires", [])
    p.emotions = data.get("emotions", [])
    p.insecurities = data.get("insecurities", [])
    p.audience_goals = data.get("audience_goals", [])
    p.language_patterns = data.get("language_patterns", "")
    p.psychological_patterns = data.get("psychological_patterns", "")
    p.audience_profile = data.get("audience_profile", "")
    p.evidence = data.get("evidence", "")
    db.commit()
    db.refresh(p)
    return _serialize(p)
