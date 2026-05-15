import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Client
from agents import StrategyAgent, AnalyticsAgent, ScriptAgent, TrendAgent, DesignAgent, AmplifierAgent
from services import MemoryService

router = APIRouter(prefix="/agents", tags=["agents"])


def _sse(event_type: str, payload) -> str:
    return f"data: {json.dumps({'type': event_type, 'payload': payload})}\n\n"


def _stream_response(gen):
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class StrategyRequest(BaseModel):
    client_id: int
    period: Optional[str] = "semanal"


class ScriptRequest(BaseModel):
    client_id: int
    topic: str
    format: str
    platform: str
    objective: str


class TrendRequest(BaseModel):
    client_id: int
    current_trends: str


class DesignRequest(BaseModel):
    client_id: int
    content_topic: str
    format: str
    platform: str
    references: Optional[str] = ""


class AmplifierRequest(BaseModel):
    client_id: int
    raw_idea: str


class AnalyticsRequest(BaseModel):
    client_id: int
    metrics_data: str


def _require_client(client_id: int, db: Session) -> Client:
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        raise HTTPException(404, "Client not found")
    return client


@router.post("/strategy/stream")
async def strategy_stream(req: StrategyRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    agent = StrategyAgent()
    prompt = agent.build_prompt(context, req.period)

    async def gen():
        yield _sse("status", "Agente de Estratégia trabalhando...")
        result = ""
        async for chunk in agent.stream(prompt):
            result += chunk
            yield _sse("chunk", chunk)
        mem.store(req.client_id, "strategy", "last_strategy", result[:500])
        yield _sse("done", "Estratégia gerada.")

    return _stream_response(gen())


@router.post("/analytics/stream")
async def analytics_stream(req: AnalyticsRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    history = mem.get(req.client_id, "analytics", "last_insights") or ""
    agent = AnalyticsAgent()
    prompt = agent.build_prompt(req.metrics_data, context, history)

    async def gen():
        yield _sse("status", "Agente de Analytics trabalhando...")
        result = ""
        async for chunk in agent.stream(prompt):
            result += chunk
            yield _sse("chunk", chunk)
        mem.store(req.client_id, "analytics", "last_insights", result[:500])
        yield _sse("done", "Análise concluída.")

    return _stream_response(gen())


@router.post("/script/stream")
async def script_stream(req: ScriptRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    patterns = mem.build_winning_patterns(req.client_id)
    agent = ScriptAgent()
    prompt = agent.build_prompt(req.topic, context, req.format, req.platform, req.objective, patterns)

    async def gen():
        yield _sse("status", "Agente de Roteiro trabalhando...")
        async for chunk in agent.stream(prompt):
            yield _sse("chunk", chunk)
        yield _sse("done", "Roteiro gerado.")

    return _stream_response(gen())


@router.post("/trend/stream")
async def trend_stream(req: TrendRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    agent = TrendAgent()
    prompt = agent.build_prompt(context, req.current_trends)

    async def gen():
        yield _sse("status", "Agente de Trends analisando...")
        async for chunk in agent.stream(prompt):
            yield _sse("chunk", chunk)
        yield _sse("done", "Análise de trends concluída.")

    return _stream_response(gen())


@router.post("/design/stream")
async def design_stream(req: DesignRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    agent = DesignAgent()
    prompt = agent.build_prompt(req.content_topic, req.format, req.platform, context, req.references)

    async def gen():
        yield _sse("status", "Agente de Design criando briefing...")
        async for chunk in agent.stream(prompt):
            yield _sse("chunk", chunk)
        yield _sse("done", "Briefing visual gerado.")

    return _stream_response(gen())


@router.post("/amplifier/stream")
async def amplifier_stream(req: AmplifierRequest, db: Session = Depends(get_db)):
    _require_client(req.client_id, db)
    mem = MemoryService(db)
    context = mem.build_client_context(req.client_id)
    agent = AmplifierAgent()
    prompt = agent.build_prompt(req.raw_idea, context)

    async def gen():
        yield _sse("status", "Amplificador de ideias trabalhando...")
        async for chunk in agent.stream(prompt):
            yield _sse("chunk", chunk)
        yield _sse("done", "Ideia amplificada.")

    return _stream_response(gen())
