import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Client, User
from agents import StrategyAgent, AnalyticsAgent, ScriptAgent, TrendAgent, DesignAgent, AmplifierAgent, AutoCreatorAgent, parse_json_response
from services import MemoryService, fetch_site_context, generate_image_url, aspect_for_format
from models import ContentPiece
from auth import get_current_user, assert_client_access

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


class AutoCreateRequest(BaseModel):
    client_id: int
    site_url: Optional[str] = ""
    topic: Optional[str] = ""
    format: str = "post"
    platform: str = "instagram"
    objective: str = "attract"


@router.post("/strategy/stream")
async def strategy_stream(req: StrategyRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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
async def analytics_stream(req: AnalyticsRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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
async def script_stream(req: ScriptRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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
async def trend_stream(req: TrendRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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
async def design_stream(req: DesignRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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


@router.post("/auto/stream")
async def auto_create_stream(req: AutoCreateRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
    mem = MemoryService(db)
    client_context = mem.build_client_context(req.client_id)

    async def gen():
        # 1. Scrape site
        site_context = ""
        if req.site_url:
            yield _sse("status", f"Analisando site: {req.site_url}...")
            site_context = await fetch_site_context(req.site_url)

        # 2. Generate content via LLM
        yield _sse("status", "Gerando conteúdo com IA...")
        agent = AutoCreatorAgent()
        prompt = agent.build_prompt(client_context, site_context, req.topic or "", req.format, req.platform, req.objective)
        raw = ""
        async for chunk in agent.stream(prompt):
            raw += chunk
            yield _sse("chunk", chunk)

        # 3. Parse JSON
        data = parse_json_response(raw)
        if not data:
            yield _sse("error", "Falha ao interpretar resposta da IA. Tente novamente.")
            return

        # 4. Generate image
        yield _sse("status", "Gerando imagem...")
        w, h = aspect_for_format(req.format)
        image_url = generate_image_url(data.get("image_prompt", data.get("title", "")), width=w, height=h)

        # 5. Persist as ContentPiece
        content = ContentPiece(
            client_id=req.client_id,
            title=(data.get("title") or req.topic or "Conteúdo auto-gerado")[:200],
            format=req.format,
            platform=req.platform,
            objective=req.objective,
            hook=data.get("hook"),
            script=data.get("script"),
            copy=data.get("copy"),
            design_brief=data.get("design_brief"),
            media_url=image_url,
            strategic_note=f"Auto-criado a partir de: {req.site_url or 'briefing do cliente'}",
            status="pending",
        )
        db.add(content)
        db.commit()
        db.refresh(content)

        yield _sse("done", {"content_id": content.id, "image_url": image_url, "title": content.title})

    return _stream_response(gen())


@router.post("/amplifier/stream")
async def amplifier_stream(req: AmplifierRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    assert_client_access(req.client_id, current_user, db)
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
