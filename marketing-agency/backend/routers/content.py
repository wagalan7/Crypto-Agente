import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from database import get_db
from models import ContentPiece, User, Product, Client, Inspiration, AgentMemory
from auth import get_current_user, assert_client_access
from agents import ProductionBriefingAgent, SectionRewriterAgent, InspirationAlignmentAgent, RepurposeAgent, VoiceScorerAgent
from agents.production_briefing import parse_json_response as parse_brief_json
from agents.inspiration_alignment import parse_json_response as parse_align_json
from agents.repurpose import parse_json_response as parse_repurpose_json
from agents.voice_scorer import parse_json_response as parse_voice_json
from services import BrandBrain

REGEN_SECTIONS = {"hook", "script", "copy", "design_brief"}

router = APIRouter(prefix="/content", tags=["content"])


class ContentCreate(BaseModel):
    client_id: int
    title: str
    format: str
    platform: str
    objective: str
    hook: Optional[str] = None
    script: Optional[str] = None
    copy: Optional[str] = None
    design_brief: Optional[str] = None
    media_url: Optional[str] = None
    trend_context: Optional[str] = None
    strategic_note: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    objective_reasoning: Optional[str] = None
    emotion_used: Optional[str] = None
    funnel_stage: Optional[str] = None
    format_reasoning: Optional[str] = None
    linked_product_id: Optional[int] = None


class ContentUpdate(BaseModel):
    title: Optional[str] = None
    hook: Optional[str] = None
    script: Optional[str] = None
    copy: Optional[str] = None
    design_brief: Optional[str] = None
    media_url: Optional[str] = None
    status: Optional[str] = None
    strategic_note: Optional[str] = None
    objective_reasoning: Optional[str] = None
    emotion_used: Optional[str] = None
    funnel_stage: Optional[str] = None
    format_reasoning: Optional[str] = None
    linked_product_id: Optional[int] = None


def _serialize(c: ContentPiece, product_name: Optional[str] = None) -> dict:
    return {
        "id": c.id,
        "client_id": c.client_id,
        "title": c.title,
        "linked_product_name": product_name,
        "format": c.format,
        "platform": c.platform,
        "objective": c.objective,
        "hook": c.hook,
        "script": c.script,
        "copy": c.copy,
        "design_brief": c.design_brief,
        "media_url": c.media_url,
        "status": c.status,
        "trend_context": c.trend_context,
        "strategic_note": c.strategic_note,
        "objective_reasoning": c.objective_reasoning,
        "emotion_used": c.emotion_used,
        "funnel_stage": c.funnel_stage,
        "format_reasoning": c.format_reasoning,
        "linked_product_id": c.linked_product_id,
        "external_post_id": c.external_post_id,
        "publish_error": c.publish_error,
        "production_brief": json.loads(c.production_brief) if c.production_brief else None,
        "voice_score": c.voice_score,
        "voice_feedback": json.loads(c.voice_feedback) if c.voice_feedback else None,
        "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/client/{client_id}")
def list_content(
    client_id: int,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assert_client_access(client_id, current_user, db)
    q = db.query(ContentPiece).filter(ContentPiece.client_id == client_id)
    if status:
        q = q.filter(ContentPiece.status == status)
    contents = q.order_by(ContentPiece.created_at.desc()).all()
    # Resolve product names for linked content
    product_ids = {c.linked_product_id for c in contents if c.linked_product_id}
    name_by_id: dict[int, str] = {}
    if product_ids:
        for p in db.query(Product).filter(Product.id.in_(product_ids)).all():
            name_by_id[p.id] = p.name
    return [_serialize(c, name_by_id.get(c.linked_product_id) if c.linked_product_id else None) for c in contents]


@router.post("/")
def create_content(data: ContentCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    content = ContentPiece(**data.model_dump())
    db.add(content)
    db.commit()
    db.refresh(content)
    return _serialize(content)


@router.get("/{content_id}")
def get_content(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    pname = None
    if c.linked_product_id:
        p = db.query(Product).filter(Product.id == c.linked_product_id).first()
        pname = p.name if p else None
    return _serialize(c, pname)


@router.patch("/{content_id}")
def update_content(content_id: int, data: ContentUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(c, field, value)
    if data.status == "published" and not c.published_at:
        c.published_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _serialize(c)


@router.post("/{content_id}/approve")
async def approve_content(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    c.status = "approved"
    # Auto-generate production briefing on approve if missing — turns "approved"
    # from a status flag into an actionable shooting plan.
    if not c.production_brief:
        try:
            client = db.query(Client).filter(Client.id == c.client_id).first()
            agent = ProductionBriefingAgent()
            prompt = agent.build_prompt(
                title=c.title or "",
                format=c.format or "post",
                platform=c.platform or "instagram",
                hook=c.hook or "",
                script=c.script or "",
                design_brief=c.design_brief or "",
                copy=c.copy or "",
                emotion=c.emotion_used or "",
                tone=(client.tone if client else "") or "",
            )
            raw = await agent.run(prompt)
            brief = parse_brief_json(raw)
            if brief:
                c.production_brief = json.dumps(brief, ensure_ascii=False)
        except Exception:
            # Never fail approve because of briefing — silent fallback
            pass
    db.commit()
    return _serialize(c)


class RegenerateSectionRequest(BaseModel):
    section: str  # hook | script | copy | design_brief
    instruction: Optional[str] = None  # optional steer ("mais vulnerável", "mais curto", etc)


@router.post("/{content_id}/regenerate-section")
async def regenerate_section(content_id: int, req: RegenerateSectionRequest,
                              current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if req.section not in REGEN_SECTIONS:
        raise HTTPException(400, f"Seção inválida. Use: {sorted(REGEN_SECTIONS)}")
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)

    brain = BrandBrain(db).build(c.client_id)
    agent = SectionRewriterAgent()
    prompt = agent.build_prompt(
        brand_brain_text=brain["text"],
        section=req.section,
        current_value=getattr(c, req.section) or "",
        title=c.title or "",
        format=c.format or "post",
        platform=c.platform or "instagram",
        objective=c.objective or "",
        emotion=c.emotion_used or "",
        instruction=req.instruction or "",
    )
    new_value = (await agent.run(prompt) or "").strip()
    # Strip accidental code fences or leading labels
    if new_value.startswith("```"):
        lines = new_value.split("\n")
        new_value = "\n".join(lines[1:-1] if len(lines) >= 2 else lines).strip()
    if not new_value:
        raise HTTPException(500, "IA retornou vazio")
    setattr(c, req.section, new_value)
    db.commit()
    db.refresh(c)
    return _serialize(c)


class HookVariationsRequest(BaseModel):
    count: int = 3
    instruction: Optional[str] = None  # optional vibe ("3 estilos: pergunta, dor, curiosidade")


HOOK_STYLES = [
    "Comece com uma pergunta direta que toca uma dor da persona.",
    "Comece com uma afirmação polêmica/contraintuitiva que gera tensão.",
    "Comece com uma cena visual concreta (1-2 substantivos + verbo de ação) que prende em 3 palavras.",
    "Comece com vulnerabilidade — admita algo que a audiência sente mas não verbaliza.",
    "Comece com um número específico ou estatística que choca.",
]


@router.post("/{content_id}/hook-variations")
async def generate_hook_variations(content_id: int, req: HookVariationsRequest,
                                    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate N hook variations in parallel so the user can pick the strongest.

    Each variation uses a different stylistic angle. Returns a list — frontend
    presents them side-by-side and the user calls /select-hook to commit one.
    """
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)

    n = max(2, min(req.count or 3, 5))
    brain = BrandBrain(db).build(c.client_id)
    agent = SectionRewriterAgent()

    styles = HOOK_STYLES[:n]
    custom = req.instruction or ""

    async def gen_one(style: str):
        prompt = agent.build_prompt(
            brand_brain_text=brain["text"],
            section="hook",
            current_value=c.hook or "",
            title=c.title or "",
            format=c.format or "post",
            platform=c.platform or "instagram",
            objective=c.objective or "",
            emotion=c.emotion_used or "",
            instruction=f"{style} {custom}".strip(),
        )
        try:
            raw = await agent.run(prompt)
            return (raw or "").strip().strip('"').strip("'")
        except Exception:
            return ""

    results = await asyncio.gather(*[gen_one(s) for s in styles])
    variations = [
        {"style": styles[i].split(".")[0], "hook": v}
        for i, v in enumerate(results) if v
    ]
    if not variations:
        raise HTTPException(500, "IA não retornou variações")
    return {"current": c.hook, "variations": variations}


class SelectHookRequest(BaseModel):
    hook: str
    style: Optional[str] = None  # which A/B variation style won (logged for learning)


@router.post("/{content_id}/select-hook")
def select_hook(content_id: int, req: SelectHookRequest,
                 current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    if not (req.hook or "").strip():
        raise HTTPException(400, "Hook vazio")
    c.hook = req.hook.strip()
    # Learning loop: register which style the user picked so BrandBrain can
    # surface "hook patterns that won" to future generators.
    if req.style:
        db.add(AgentMemory(
            client_id=c.client_id,
            agent_type="hook_style_winner",
            memory_key=req.style.strip()[:200],
            memory_value=c.hook[:500],
            is_active=True,
        ))
    db.commit()
    db.refresh(c)
    return _serialize(c)


class BulkApproveRequest(BaseModel):
    ids: List[int]


@router.post("/bulk/approve")
async def bulk_approve(req: BulkApproveRequest,
                        current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Approve many at once. Production briefs are generated in parallel but
    failures don't block the approval — same fallback policy as single approve.
    """
    if not req.ids:
        return {"approved": [], "failed": []}
    contents = db.query(ContentPiece).filter(ContentPiece.id.in_(req.ids)).all()
    approved: list[int] = []
    failed: list[dict] = []

    # Authorize each (skip silently if user lacks access)
    accessible: list[ContentPiece] = []
    for c in contents:
        try:
            assert_client_access(c.client_id, current_user, db)
            accessible.append(c)
        except Exception:
            failed.append({"id": c.id, "reason": "sem acesso"})

    # Mark approved synchronously
    for c in accessible:
        c.status = "approved"
        approved.append(c.id)
    db.commit()

    # Generate briefs in parallel (best-effort)
    async def make_brief(c: ContentPiece):
        if c.production_brief:
            return
        try:
            client = db.query(Client).filter(Client.id == c.client_id).first()
            agent = ProductionBriefingAgent()
            prompt = agent.build_prompt(
                title=c.title or "", format=c.format or "post", platform=c.platform or "instagram",
                hook=c.hook or "", script=c.script or "", design_brief=c.design_brief or "",
                copy=c.copy or "", emotion=c.emotion_used or "",
                tone=(client.tone if client else "") or "",
            )
            raw = await agent.run(prompt)
            brief = parse_brief_json(raw)
            if brief:
                c.production_brief = json.dumps(brief, ensure_ascii=False)
        except Exception:
            pass

    await asyncio.gather(*[make_brief(c) for c in accessible])
    db.commit()
    return {"approved": approved, "failed": failed}


class BulkDeleteRequest(BaseModel):
    ids: List[int]


@router.post("/bulk/delete")
def bulk_delete(req: BulkDeleteRequest,
                 current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not req.ids:
        return {"deleted": 0}
    contents = db.query(ContentPiece).filter(ContentPiece.id.in_(req.ids)).all()
    n = 0
    for c in contents:
        try:
            assert_client_access(c.client_id, current_user, db)
            db.delete(c)
            n += 1
        except Exception:
            continue
    db.commit()
    return {"deleted": n}


@router.post("/{content_id}/voice-score")
async def voice_score(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Score how on-brand the piece sounds (0-100) and persist on the row.

    Cheap call — used both on-demand from the UI and as a quality gate after
    auto-generation. Returns the full updated piece so the frontend re-renders.
    """
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    client = db.query(Client).filter(Client.id == c.client_id).first()

    agent = VoiceScorerAgent()
    prompt = agent.build_prompt(
        tone=(client.tone if client else "") or "",
        personality=(client.personality if client else "") or "",
        positioning=(client.positioning if client else "") or "",
        hook=c.hook or "",
        script=c.script or "",
        copy=c.copy or "",
    )
    raw = await agent.run(prompt)
    data = parse_voice_json(raw)
    if not data:
        raise HTTPException(500, "IA não retornou pontuação válida")
    try:
        score = max(0, min(100, int(data.get("score") or 0)))
    except Exception:
        score = 0
    feedback = {
        "verdict": data.get("verdict") or "",
        "weakest_part": data.get("weakest_part"),
        "fix_hint": data.get("fix_hint") or "",
    }
    c.voice_score = score
    c.voice_feedback = json.dumps(feedback, ensure_ascii=False)
    db.commit()
    db.refresh(c)
    return _serialize(c)


class RepurposeRequest(BaseModel):
    target_format: str
    target_platform: str
    instruction: Optional[str] = None


@router.post("/{content_id}/repurpose")
async def repurpose_content(content_id: int, req: RepurposeRequest,
                             current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate a NEW ContentPiece adapted to a different format/platform.

    Inherits strategic core (objective, funnel_stage, emotion, linked product)
    but rewrites hook/script/copy/design_brief in the language of the target.
    Returns the freshly created piece.
    """
    src = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not src:
        raise HTTPException(404, "Content not found")
    assert_client_access(src.client_id, current_user, db)

    tgt_fmt = (req.target_format or "").strip().lower()
    tgt_plat = (req.target_platform or "").strip().lower()
    if not tgt_fmt or not tgt_plat:
        raise HTTPException(400, "target_format e target_platform são obrigatórios")
    if tgt_fmt == (src.format or "").lower() and tgt_plat == (src.platform or "").lower():
        raise HTTPException(400, "Destino igual à origem — nada pra adaptar")

    brain = BrandBrain(db).build(src.client_id)
    agent = RepurposeAgent()
    prompt = agent.build_prompt(
        brand_brain_text=brain["text"],
        source_title=src.title or "",
        source_format=src.format or "post",
        source_platform=src.platform or "instagram",
        source_hook=src.hook or "",
        source_script=src.script or "",
        source_copy=src.copy or "",
        source_design_brief=src.design_brief or "",
        source_emotion=src.emotion_used or "",
        source_funnel=src.funnel_stage or "",
        source_objective=src.objective or "",
        target_format=tgt_fmt,
        target_platform=tgt_plat,
        instruction=req.instruction or "",
    )
    raw = await agent.run(prompt)
    data = parse_repurpose_json(raw)
    if not data or not data.get("hook"):
        raise HTTPException(500, "IA não retornou adaptação válida")

    new_piece = ContentPiece(
        client_id=src.client_id,
        title=data.get("title") or src.title,
        format=tgt_fmt,
        platform=tgt_plat,
        objective=src.objective,
        hook=data.get("hook"),
        script=data.get("script"),
        copy=data.get("copy"),
        design_brief=data.get("design_brief"),
        emotion_used=src.emotion_used,
        funnel_stage=src.funnel_stage,
        objective_reasoning=src.objective_reasoning,
        format_reasoning=data.get("adaptation_notes") or f"Adaptado de '{src.title}' ({src.format}/{src.platform})",
        strategic_note=f"♻ Reaproveitamento de #{src.id}. {data.get('adaptation_notes') or ''}".strip(),
        linked_product_id=src.linked_product_id,
        status="pending",
    )
    db.add(new_piece)
    db.commit()
    db.refresh(new_piece)
    return _serialize(new_piece)


@router.post("/{content_id}/inspiration-alignment")
async def inspiration_alignment(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Score how well this generated post lands the saved inspirations.

    Returns alignment_score, strengths, divergences and a concrete adjustment
    suggestion. If the client has no inspirations cadastrated, returns 0 with
    a hint to add some.
    """
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)

    inspirations = db.query(Inspiration).filter(Inspiration.client_id == c.client_id).order_by(Inspiration.created_at.desc()).all()
    if not inspirations:
        return {
            "best_match": None,
            "alignment_score": 0,
            "strengths": [],
            "divergences": ["Você ainda não cadastrou inspirações pra essa marca."],
            "adjustment_suggestion": "Adicione referências (URLs, prints ou textos) na aba Inspirações pra IA aprender o tom desejado.",
        }

    insp_payload = [
        {"label": i.label, "analysis": i.analysis or {}, "adapted_brief": i.adapted_brief or ""}
        for i in inspirations
    ]
    brain = BrandBrain(db).build(c.client_id)
    agent = InspirationAlignmentAgent()
    prompt = agent.build_prompt(
        brand_brain_text=brain["text"],
        title=c.title or "",
        format=c.format or "post",
        platform=c.platform or "instagram",
        hook=c.hook or "",
        script=c.script or "",
        copy=c.copy or "",
        design_brief=c.design_brief or "",
        inspirations=insp_payload,
    )
    raw = await agent.run(prompt)
    result = parse_align_json(raw)
    if not result:
        raise HTTPException(500, "IA não retornou análise válida")
    # sanitize
    score = result.get("alignment_score") or 0
    try:
        score = max(0, min(100, int(score)))
    except Exception:
        score = 0
    return {
        "best_match": result.get("best_match"),
        "alignment_score": score,
        "strengths": result.get("strengths") or [],
        "divergences": result.get("divergences") or [],
        "adjustment_suggestion": result.get("adjustment_suggestion") or "",
    }


@router.post("/{content_id}/regenerate-brief")
async def regenerate_brief(content_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    c = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not c:
        raise HTTPException(404, "Content not found")
    assert_client_access(c.client_id, current_user, db)
    client = db.query(Client).filter(Client.id == c.client_id).first()
    agent = ProductionBriefingAgent()
    prompt = agent.build_prompt(
        title=c.title or "",
        format=c.format or "post",
        platform=c.platform or "instagram",
        hook=c.hook or "",
        script=c.script or "",
        design_brief=c.design_brief or "",
        copy=c.copy or "",
        emotion=c.emotion_used or "",
        tone=(client.tone if client else "") or "",
    )
    raw = await agent.run(prompt)
    brief = parse_brief_json(raw)
    if not brief:
        raise HTTPException(500, "Falha ao gerar briefing de produção")
    c.production_brief = json.dumps(brief, ensure_ascii=False)
    db.commit()
    return _serialize(c)
