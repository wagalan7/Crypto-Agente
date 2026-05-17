from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import SocialAccount, ContentPiece, User
from auth import get_current_user, assert_client_access
from services import meta_publisher
from services.plans import assert_feature
from services.audit import log_action
from fastapi import Request

router = APIRouter(prefix="/social", tags=["social"])


class SocialAccountCreate(BaseModel):
    client_id: int
    platform: str  # "instagram" or "facebook"
    account_id: str
    account_name: Optional[str] = None
    access_token: str


class SocialAccountUpdate(BaseModel):
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    access_token: Optional[str] = None
    is_active: Optional[bool] = None


def _mask(token: str) -> str:
    if not token or len(token) < 12:
        return "***"
    return token[:6] + "..." + token[-4:]


def _serialize(acc: SocialAccount) -> dict:
    return {
        "id": acc.id,
        "client_id": acc.client_id,
        "platform": acc.platform,
        "account_id": acc.account_id,
        "account_name": acc.account_name,
        "access_token_preview": _mask(acc.access_token),
        "is_active": acc.is_active,
        "last_error": acc.last_error,
        "expires_at": acc.expires_at.isoformat() if acc.expires_at else None,
        "updated_at": acc.updated_at.isoformat() if acc.updated_at else None,
    }


@router.get("/client/{client_id}")
def list_accounts(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    accounts = db.query(SocialAccount).filter(SocialAccount.client_id == client_id).all()
    return [_serialize(a) for a in accounts]


@router.post("/")
def create_account(req: SocialAccountCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
    if req.platform not in ("instagram", "facebook"):
        raise HTTPException(400, "platform must be 'instagram' or 'facebook'")
    # one account per (client, platform) — upsert
    existing = db.query(SocialAccount).filter(
        SocialAccount.client_id == req.client_id,
        SocialAccount.platform == req.platform,
    ).first()
    if existing:
        existing.account_id = req.account_id
        existing.account_name = req.account_name
        existing.access_token = req.access_token
        existing.is_active = True
        existing.last_error = None
        acc = existing
    else:
        acc = SocialAccount(
            client_id=req.client_id,
            platform=req.platform,
            account_id=req.account_id,
            account_name=req.account_name,
            access_token=req.access_token,
        )
        db.add(acc)
    db.commit()
    db.refresh(acc)
    return _serialize(acc)


@router.patch("/{account_id}")
def update_account(account_id: int, req: SocialAccountUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
    if not acc:
        raise HTTPException(404, "Account not found")
    assert_client_access(acc.client_id, current_user, db)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(acc, field, value)
    db.commit()
    db.refresh(acc)
    return _serialize(acc)


@router.delete("/{account_id}")
def delete_account(account_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
    if not acc:
        raise HTTPException(404, "Account not found")
    assert_client_access(acc.client_id, current_user, db)
    db.delete(acc)
    db.commit()
    return {"ok": True}


@router.post("/{account_id}/test")
async def test_account(account_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
    if not acc:
        raise HTTPException(404, "Account not found")
    assert_client_access(acc.client_id, current_user, db)
    ok, msg = await meta_publisher.test_account(acc)
    if ok:
        acc.last_error = None
        acc.is_active = True
    else:
        acc.last_error = msg
    db.commit()
    return {"ok": ok, "message": msg}


@router.post("/publish/{content_id}")
async def publish_content(content_id: int, request: Request,
                            current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    content = db.query(ContentPiece).filter(ContentPiece.id == content_id).first()
    if not content:
        raise HTTPException(404, "Content not found")
    assert_client_access(content.client_id, current_user, db)
    assert_feature(current_user, "auto_publish")

    # Map content platform → social account platform
    platform = (content.platform or "").lower()
    if platform not in ("instagram", "facebook"):
        raise HTTPException(400, f"Auto-publish only supported for instagram/facebook, got '{content.platform}'")

    acc = db.query(SocialAccount).filter(
        SocialAccount.client_id == content.client_id,
        SocialAccount.platform == platform,
        SocialAccount.is_active == True,
    ).first()
    if not acc:
        raise HTTPException(400, f"No active {platform} account connected for this client")

    try:
        external_id = await meta_publisher.publish(acc, content)
    except meta_publisher.PublishError as e:
        content.publish_error = str(e)
        acc.last_error = str(e)
        db.commit()
        log_action(db, user=current_user, action="social.publish.failed",
                    client_id=content.client_id, target_type="content_piece", target_id=content.id,
                    meta={"platform": platform, "error": str(e)[:300]}, request=request)
        raise HTTPException(502, str(e))

    content.external_post_id = external_id
    content.status = "published"
    content.published_at = datetime.utcnow()
    content.publish_error = None
    db.commit()
    log_action(db, user=current_user, action="social.publish",
                client_id=content.client_id, target_type="content_piece", target_id=content.id,
                meta={"platform": platform, "external_id": external_id}, request=request)
    return {"ok": True, "external_post_id": external_id, "status": "published"}
