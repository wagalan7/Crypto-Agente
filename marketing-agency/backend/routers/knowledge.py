from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from database import get_db
from models import KnowledgeItem, User
from auth import get_current_user, assert_client_access
from services import pdf_extract_text, pdf_summarize, humanize_clean

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

# Accepted source types — keep flexible to absorb the creator's mind
ALLOWED_SOURCES = {
    "pdf", "note", "screenshot", "idea", "book", "concept",
    "reference", "framework", "observation", "study", "worldview",
}


class KnowledgeCreate(BaseModel):
    client_id: int
    title: str
    content: str
    source_type: str = "note"
    tags: List[str] = []


class KnowledgeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    source_type: Optional[str] = None


def _serialize(k: KnowledgeItem) -> dict:
    return {
        "id": k.id,
        "client_id": k.client_id,
        "title": k.title,
        "content": k.content,
        "source_type": k.source_type,
        "tags": k.tags or [],
        "summary": k.summary or "",
        "key_insights": k.key_insights or [],
        "voice_signals": k.voice_signals or [],
        "use_count": k.use_count or 0,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


async def _enrich(item: KnowledgeItem):
    """Run AI digestion on the item's content — sets summary, key_insights, voice_signals."""
    if not item.content:
        return
    digest = await pdf_summarize(item.content, title=item.title)
    if digest.get("summary"):
        item.summary = digest["summary"]
    if digest.get("key_insights"):
        item.key_insights = digest["key_insights"]
    if digest.get("voice_signals"):
        item.voice_signals = digest["voice_signals"]


@router.get("/client/{client_id}")
def list_items(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).order_by(KnowledgeItem.created_at.desc()).all()
    return [_serialize(k) for k in items]


@router.post("/")
async def create_item(data: KnowledgeCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(data.client_id, current_user, db)
    payload = data.model_dump()
    payload["content"] = humanize_clean(payload.get("content") or "")
    if payload.get("source_type") not in ALLOWED_SOURCES:
        payload["source_type"] = "note"
    item = KnowledgeItem(**payload)
    db.add(item)
    db.commit()
    db.refresh(item)
    # Best-effort AI digestion — never block creation on failure
    try:
        await _enrich(item)
        db.commit()
        db.refresh(item)
    except Exception:
        pass
    return _serialize(item)


@router.post("/upload-pdf")
async def upload_pdf(
    client_id: int = Form(...),
    title: Optional[str] = Form(None),
    tags: Optional[str] = Form(""),  # comma-separated
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ingest a PDF: extract text → store → AI summarize → return enriched item."""
    assert_client_access(client_id, current_user, db)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Envie um arquivo .pdf")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:  # 15 MB
        raise HTTPException(400, "PDF acima de 15MB")
    text = pdf_extract_text(data)
    if not text:
        raise HTTPException(400, "Não consegui extrair texto. PDF pode ser escaneado (sem OCR).")
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    item = KnowledgeItem(
        client_id=client_id,
        title=title or file.filename.rsplit(".", 1)[0],
        content=humanize_clean(text),
        source_type="pdf",
        tags=tag_list,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    try:
        await _enrich(item)
        db.commit()
        db.refresh(item)
    except Exception:
        pass
    return _serialize(item)


@router.patch("/{item_id}")
def update_item(item_id: int, data: KnowledgeUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    k = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if not k:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(k.client_id, current_user, db)
    for f, v in data.model_dump(exclude_unset=True).items():
        if f == "content" and v is not None:
            v = humanize_clean(v)
        setattr(k, f, v)
    db.commit()
    db.refresh(k)
    return _serialize(k)


@router.post("/{item_id}/redigest")
async def redigest(item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Re-run AI summarization (e.g. after content edit)."""
    k = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if not k:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(k.client_id, current_user, db)
    await _enrich(k)
    db.commit()
    db.refresh(k)
    return _serialize(k)


@router.delete("/{item_id}")
def delete_item(item_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    k = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if not k:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(k.client_id, current_user, db)
    db.delete(k)
    db.commit()
    return {"detail": "removido"}
