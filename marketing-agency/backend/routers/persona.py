from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import Persona, User, ContentPiece, AgentMemory
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
        "user_refinements": p.user_refinements or [],
        "edit_count": p.edit_count or 0,
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
    """Generates (or regenerates) the persona but PRESERVES user_refinements
    so the IA's new version is biased by the user's prior edits."""
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    contents = db.query(ContentPiece).filter(ContentPiece.client_id == client_id).order_by(ContentPiece.created_at.desc()).limit(10).all()
    samples = "\n---\n".join(
        f"[{c.format}] {c.title}\nHook: {c.hook or ''}\nCopy: {(c.copy or '')[:300]}"
        for c in contents
    )

    existing = db.query(Persona).filter(Persona.client_id == client_id).first()
    refinements = (existing.user_refinements if existing else []) or []
    # Feed prior refinements into the prompt — IA aprende com as edições
    refinement_block = ""
    if refinements:
        recent = refinements[-8:]
        refinement_block = (
            "\n\nREFINAMENTOS QUE O USUÁRIO JÁ FEZ MANUALMENTE (preserve essa direção):\n"
            + "\n".join(f"  - [{r.get('field')}] {r.get('note') or ''}: {str(r.get('after'))[:120]}" for r in recent)
        )

    agent = PersonaAgent()
    raw = await agent.run(agent.build_prompt(brain["text"] + refinement_block, samples))
    data = parse_json_response(raw)
    if not data:
        raise HTTPException(500, f"Falha ao gerar persona. Raw: {raw[:200]}")

    p = existing or Persona(client_id=client_id)
    if not existing:
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


class PersonaUpdate(BaseModel):
    pains: Optional[List[str]] = None
    desires: Optional[List[str]] = None
    emotions: Optional[List[str]] = None
    insecurities: Optional[List[str]] = None
    audience_goals: Optional[List[str]] = None
    language_patterns: Optional[str] = None
    psychological_patterns: Optional[str] = None
    audience_profile: Optional[str] = None
    note: Optional[str] = None  # optional user explanation: "audience is older than IA assumed"


EDITABLE_FIELDS = {
    "pains", "desires", "emotions", "insecurities", "audience_goals",
    "language_patterns", "psychological_patterns", "audience_profile",
}


@router.patch("/client/{client_id}")
def update_persona(client_id: int, data: PersonaUpdate,
                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """User-driven refinement. Stores a diff in user_refinements so future
    PersonaAgent runs preserve this human-in-the-loop direction.
    Also writes to AgentMemory so other agents pick it up immediately."""
    assert_client_access(client_id, current_user, db)
    p = db.query(Persona).filter(Persona.client_id == client_id).first()
    if not p:
        raise HTTPException(404, "Persona ainda não gerada. Gere primeiro.")

    changes = data.model_dump(exclude_unset=True)
    note = changes.pop("note", None)
    refinements = list(p.user_refinements or [])
    now_iso = datetime.utcnow().isoformat()

    for field, new_value in changes.items():
        if field not in EDITABLE_FIELDS:
            continue
        before = getattr(p, field)
        if before == new_value:
            continue
        refinements.append({
            "field": field,
            "before": before,
            "after": new_value,
            "note": note,
            "at": now_iso,
        })
        setattr(p, field, new_value)

    p.user_refinements = refinements[-40:]  # cap history
    p.edit_count = (p.edit_count or 0) + 1

    # Mirror to AgentMemory so BrandBrain surfaces it across all agents
    if note:
        db.add(AgentMemory(
            client_id=client_id,
            agent_type="persona_refinement",
            memory_key=f"edit:{p.edit_count}",
            memory_value=note[:500],
            is_active=True,
        ))

    db.commit()
    db.refresh(p)
    return _serialize(p)
