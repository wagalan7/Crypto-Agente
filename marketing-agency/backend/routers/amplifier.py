"""Amplifier — single unified surface for the creator's intellectual capital.

Returns knowledge + inspirations + persona + a "what the AI absorbed" digest.
This is the read endpoint the new frontend Amplifier hub binds to. Writes
still go through /knowledge, /inspirations, /persona — keeping the surface
area minimal and avoiding duplication.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from models import KnowledgeItem, Inspiration, Persona, AgentMemory, User
from auth import get_current_user, assert_client_access

router = APIRouter(prefix="/amplifier", tags=["amplifier"])


@router.get("/client/{client_id}")
def get_amplifier(client_id: int, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)

    knowledge = db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).order_by(KnowledgeItem.created_at.desc()).all()
    inspirations = db.query(Inspiration).filter(Inspiration.client_id == client_id).order_by(Inspiration.created_at.desc()).all()
    persona = db.query(Persona).filter(Persona.client_id == client_id).first()

    # Aggregate voice signals across all knowledge items — what the AI "hears"
    voice_signals: list[str] = []
    seen = set()
    for k in knowledge:
        for s in (k.voice_signals or []):
            if s and s.lower() not in seen:
                seen.add(s.lower())
                voice_signals.append(s)
            if len(voice_signals) >= 25:
                break
        if len(voice_signals) >= 25:
            break

    # Count source-type distribution to show "diet" of the mind being fed
    source_counts: dict[str, int] = {}
    for k in knowledge:
        st = k.source_type or "note"
        source_counts[st] = source_counts.get(st, 0) + 1

    # Last 5 persona refinements — show the user the IA is learning from them
    refinements = (persona.user_refinements if persona else []) or []

    # Mind digest: synthesizes how much the AI knows about this brand
    knowledge_chars = sum(len(k.content or "") for k in knowledge)
    insights_total = sum(len(k.key_insights or []) for k in knowledge)
    visuals_total = sum(1 for i in inspirations if (i.visual_analysis or {}))

    return {
        "client_id": client_id,
        "summary": {
            "knowledge_count": len(knowledge),
            "knowledge_chars": knowledge_chars,
            "insights_extracted": insights_total,
            "inspirations_count": len(inspirations),
            "visuals_analyzed": visuals_total,
            "persona_edits": persona.edit_count if persona else 0,
            "voice_signals": len(voice_signals),
        },
        "source_distribution": source_counts,
        "voice_signals": voice_signals,
        "recent_refinements": refinements[-5:],
        "knowledge": [{
            "id": k.id,
            "title": k.title,
            "source_type": k.source_type,
            "tags": k.tags or [],
            "summary": k.summary or "",
            "key_insights": k.key_insights or [],
            "voice_signals": k.voice_signals or [],
            "use_count": k.use_count or 0,
            "created_at": k.created_at.isoformat() if k.created_at else None,
        } for k in knowledge],
        "inspirations": [{
            "id": i.id,
            "label": i.label,
            "source_type": i.source_type,
            "source_value": i.source_value if i.source_type != "image" else None,
            "image_url": i.image_url,
            "analysis": i.analysis or {},
            "visual_analysis": i.visual_analysis or {},
            "adapted_brief": i.adapted_brief,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        } for i in inspirations],
    }
