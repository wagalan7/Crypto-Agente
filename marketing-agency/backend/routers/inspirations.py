import base64
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Inspiration, User
from auth import get_current_user, assert_client_access
from services import BrandBrain, fetch_site_context, vision_analyze, humanize_clean
from agents import InspirationAnalyzerAgent, parse_json_response

router = APIRouter(prefix="/inspirations", tags=["inspirations"])


class InspirationCreate(BaseModel):
    client_id: int
    source_type: str  # url / text / image
    source_value: str
    label: Optional[str] = None


def _serialize(i: Inspiration) -> dict:
    return {
        "id": i.id,
        "client_id": i.client_id,
        "source_type": i.source_type,
        "source_value": i.source_value,
        "image_url": i.image_url,
        "label": i.label,
        "analysis": i.analysis or {},
        "visual_analysis": i.visual_analysis or {},
        "adapted_brief": i.adapted_brief,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


async def _build_inspiration(
    db: Session,
    client_id: int,
    source_type: str,
    source_value: str,
    label: Optional[str],
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> Inspiration:
    # 1. Resolve textual context
    text_for_analysis = source_value
    if source_type == "url":
        text_for_analysis = await fetch_site_context(source_value, max_chars=3000)

    # 2. Run vision analysis if it's an image (image_url OR uploaded bytes)
    visual = {}
    if source_type == "image" and (image_url or image_bytes):
        visual = await vision_analyze(image_url=image_url, image_bytes=image_bytes, mime_type=mime_type)

    # 3. Run textual analysis through the existing agent
    brain = BrandBrain(db).build(client_id)
    agent = InspirationAnalyzerAgent()
    # If vision returned signals, fold them into the prompt context
    vision_block = ""
    if visual:
        vision_block = (
            "\n\nANÁLISE VISUAL (vision model):\n"
            + "\n".join(f"  {k}: {v}" for k, v in visual.items() if v)
        )
    raw = await agent.run(agent.build_prompt(
        brain["text"] + vision_block,
        source_type,
        text_for_analysis or "(imagem — sem texto extraído)",
    ))
    parsed = parse_json_response(raw) or {}
    if not parsed and not visual:
        raise HTTPException(500, f"Falha ao analisar. Raw: {raw[:200]}")

    item = Inspiration(
        client_id=client_id,
        source_type=source_type,
        source_value=source_value or (image_url or ""),
        image_url=image_url,
        label=humanize_clean(label or parsed.get("hook", "")[:80] or "Referência"),
        analysis=parsed,
        visual_analysis=visual,
        adapted_brief=humanize_clean(parsed.get("adapted_brief", "")),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/client/{client_id}")
def list_inspirations(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(Inspiration).filter(Inspiration.client_id == client_id).order_by(Inspiration.created_at.desc()).all()
    return [_serialize(i) for i in items]


@router.post("/")
async def create_inspiration(data: InspirationCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    image_url = data.source_value if data.source_type == "image" and (data.source_value or "").startswith(("http://", "https://")) else None
    item = await _build_inspiration(
        db, data.client_id, data.source_type, data.source_value, data.label,
        image_url=image_url,
    )
    return _serialize(item)


@router.post("/upload-image")
async def upload_inspiration_image(
    client_id: int = Form(...),
    label: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a screenshot/image reference. Vision model analyzes composition/aesthetics
    and the textual agent adapts it to the client's brand. Stores image inline as
    data URI (kept small — caller should compress)."""
    assert_client_access(client_id, current_user, db)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Envie uma imagem")
    raw = await file.read()
    if len(raw) > 4 * 1024 * 1024:  # 4 MB cap (data-URI lives in DB)
        raise HTTPException(400, "Imagem acima de 4MB — comprima antes")
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{file.content_type};base64,{b64}"
    item = await _build_inspiration(
        db, client_id, "image", file.filename or "screenshot", label,
        image_url=data_url,
        image_bytes=raw,
        mime_type=file.content_type,
    )
    return _serialize(item)


@router.delete("/{inspiration_id}")
def delete_inspiration(inspiration_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.query(Inspiration).filter(Inspiration.id == inspiration_id).first()
    if not item:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(item.client_id, current_user, db)
    db.delete(item)
    db.commit()
    return {"detail": "removido"}
