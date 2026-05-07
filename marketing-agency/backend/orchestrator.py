from typing import AsyncIterator
from models import ProductInput
from agents.strategist import StrategistAgent
from agents.copywriter import CopywriterAgent
from agents.social_media import SocialMediaAgent
from agents.design_director import DesignDirectorAgent
from agents.traffic import TrafficAgent
from agents.automation import AutomationAgent
from agents.publisher import PublisherAgent
import json


async def run_agency(data: ProductInput) -> AsyncIterator[str]:
    strategist = StrategistAgent()
    copywriter = CopywriterAgent()
    social = SocialMediaAgent()
    design = DesignDirectorAgent()
    traffic = TrafficAgent()
    automation = AutomationAgent()
    publisher = PublisherAgent()

    # Agent 1 — Estratégia
    yield _event("status", "Agente 1: Estrategista trabalhando...")
    estrategia = ""
    async for chunk in strategist.stream(
        strategist.build_prompt(data.produto, data.preco, data.publico, data.objetivo, data.plataforma, data.tom_de_voz)
    ):
        estrategia += chunk
        yield _event("chunk", {"section": "estrategia", "text": chunk})
    yield _event("section_done", "estrategia")

    # Agent 2 — Copy
    yield _event("status", "Agente 2: Copywriter trabalhando...")
    copy = ""
    async for chunk in copywriter.stream(
        copywriter.build_prompt(data.produto, data.preco, data.tom_de_voz, estrategia)
    ):
        copy += chunk
        yield _event("chunk", {"section": "copy", "text": chunk})
    yield _event("section_done", "copy")

    # Agent 3 — Conteúdo
    yield _event("status", "Agente 3: Social Media trabalhando...")
    conteudo = ""
    async for chunk in social.stream(
        social.build_prompt(data.produto, data.plataforma, estrategia, copy)
    ):
        conteudo += chunk
        yield _event("chunk", {"section": "conteudo", "text": chunk})
    yield _event("section_done", "conteudo")

    # Agent 4 — Criativos
    yield _event("status", "Agente 4: Design Diretor trabalhando...")
    criativos = ""
    async for chunk in design.stream(
        design.build_prompt(data.produto, data.tom_de_voz, data.publico, estrategia)
    ):
        criativos += chunk
        yield _event("chunk", {"section": "criativos", "text": chunk})
    yield _event("section_done", "criativos")

    # Agent 5 — Ads
    yield _event("status", "Agente 5: Tráfego Pago trabalhando...")
    ads = ""
    async for chunk in traffic.stream(
        traffic.build_prompt(data.produto, data.preco, data.publico, data.plataforma, data.objetivo, estrategia, copy)
    ):
        ads += chunk
        yield _event("chunk", {"section": "ads", "text": chunk})
    yield _event("section_done", "ads")

    # Agent 6 — Automação
    yield _event("status", "Agente 6: Automação trabalhando...")
    automacao = ""
    async for chunk in automation.stream(
        automation.build_prompt(data.produto, data.preco, estrategia, copy)
    ):
        automacao += chunk
        yield _event("chunk", {"section": "automacao", "text": chunk})
    yield _event("section_done", "automacao")

    # Agent 7 — Publicação
    yield _event("status", "Agente 7: Publicador trabalhando...")
    async for chunk in publisher.stream(
        publisher.build_prompt(data.plataforma, conteudo, copy, estrategia)
    ):
        yield _event("chunk", {"section": "publicacao", "text": chunk})
    yield _event("section_done", "publicacao")

    yield _event("done", "Agência concluiu todas as etapas.")


def _event(event_type: str, data) -> str:
    return f"data: {json.dumps({'type': event_type, 'payload': data})}\n\n"
