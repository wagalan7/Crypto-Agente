"""Central Estratégica — Weekly Brain + Insights."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Insight, WeeklyBrain, User
from auth import get_current_user, assert_client_access
from services import BrandBrain
from agents import WeeklyBrainAgent, InsightGeneratorAgent, parse_json_response

router = APIRouter(prefix="/strategy", tags=["strategy"])


def _serialize_wb(wb: WeeklyBrain) -> dict:
    return {
        "id": wb.id,
        "client_id": wb.client_id,
        "focus": wb.focus,
        "opportunities": wb.opportunities or [],
        "alerts": wb.alerts or [],
        "risks": wb.risks or [],
        "priorities": wb.priorities or [],
        "audience_behavior": wb.audience_behavior,
        "trends": wb.trends or [],
        "emotional_sequence": wb.emotional_sequence or [],
        "generated_at": wb.generated_at.isoformat() if wb.generated_at else None,
    }


def _serialize_insight(i: Insight) -> dict:
    return {
        "id": i.id,
        "client_id": i.client_id,
        "kind": i.kind,
        "title": i.title,
        "message": i.message,
        "evidence": i.evidence,
        "severity": i.severity,
        "is_dismissed": i.is_dismissed,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


@router.get("/weekly/{client_id}")
def get_weekly(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    wb = db.query(WeeklyBrain).filter(WeeklyBrain.client_id == client_id).order_by(WeeklyBrain.generated_at.desc()).first()
    if not wb:
        return {"exists": False}
    return {"exists": True, **_serialize_wb(wb)}


@router.post("/weekly/{client_id}/generate")
async def regenerate_weekly(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    agent = WeeklyBrainAgent()
    raw = await agent.run(agent.build_prompt(brain["text"]))
    data = parse_json_response(raw)
    if not data:
        raise HTTPException(500, f"Falha. Raw: {raw[:200]}")

    wb = WeeklyBrain(
        client_id=client_id,
        focus=data.get("focus", ""),
        opportunities=data.get("opportunities", []),
        alerts=data.get("alerts", []),
        risks=data.get("risks", []),
        priorities=data.get("priorities", []),
        audience_behavior=data.get("audience_behavior", ""),
        trends=data.get("trends", []),
        emotional_sequence=data.get("emotional_sequence", []),
    )
    db.add(wb)
    db.commit()
    db.refresh(wb)
    return _serialize_wb(wb)


@router.get("/insights/{client_id}")
def list_insights(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    items = db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == False).order_by(Insight.created_at.desc()).limit(20).all()
    return [_serialize_insight(i) for i in items]


@router.post("/insights/{client_id}/generate")
async def regenerate_insights(client_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(client_id, current_user, db)
    brain = BrandBrain(db).build(client_id)
    agent = InsightGeneratorAgent()
    raw = await agent.run(agent.build_prompt(brain["text"]))
    data = parse_json_response(raw)
    if not data or "insights" not in data:
        raise HTTPException(500, f"Falha. Raw: {raw[:200]}")

    # Dismiss old insights (refresh the set)
    db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == False).update({"is_dismissed": True})
    created = []
    for ins in data["insights"]:
        i = Insight(
            client_id=client_id,
            kind=ins.get("kind", "info"),
            title=ins.get("title", "")[:300],
            message=ins.get("message", ""),
            evidence=ins.get("evidence", ""),
            severity=ins.get("severity", "info"),
        )
        db.add(i)
        created.append(i)
    db.commit()
    return [_serialize_insight(i) for i in created]


@router.post("/insights/{insight_id}/dismiss")
def dismiss_insight(insight_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    i = db.query(Insight).filter(Insight.id == insight_id).first()
    if not i:
        raise HTTPException(404, "Não encontrado")
    assert_client_access(i.client_id, current_user, db)
    i.is_dismissed = True
    db.commit()
    return _serialize_insight(i)
