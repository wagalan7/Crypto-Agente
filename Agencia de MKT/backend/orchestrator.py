import asyncio
import json
import os
import re
from typing import AsyncIterator
import httpx
from openai import AsyncOpenAI
from models import ProductInput

client = AsyncOpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)
MODEL = "llama-3.3-70b-versatile"

AGENT_LOGS = {
    "ESTRATEGIA": ["Definindo ICP", "Mapeando dores", "Construindo funil", "Finalizando posicionamento"],
    "COPY":        ["Analisando avatar", "Gerando hooks", "Criando headlines", "Selecionando CTA"],
    "DESIGN":      ["Definindo paleta", "Criando briefing visual", "Gerando prompts imagem", "Finalizando branding"],
    "VIDEO":       ["Escrevendo roteiro", "Descrevendo cenas", "Definindo motion", "Gerando prompt vídeo IA"],
    "SOCIAL":      ["Montando calendário", "Redigindo legendas", "Selecionando hashtags", "Agendando posts"],
    "ADS":         ["Configurando públicos", "Estruturando campanha", "Criando ad copies", "Definindo otimização"],
    "AUTOMACAO":   ["Captura de lead", "Sequência email", "WhatsApp flow", "Remarketing"],
    "PUBLICADOR":  ["Preparando conteúdo", "Formatando para plataforma", "Verificando checklist", "Finalizando publicação"],
    "ANALYTICS":   ["Calculando métricas", "Projetando funil", "Definindo KPIs", "Gerando sugestões"],
}

SYSTEMS = {
    "ESTRATEGIA": "Agente Estrategista de marketing digital. PT-BR. Curto e direto. Saída: • ICP • Dor • Desejo • Oferta • Posicionamento • Promessa • CTA • Funil (3 linhas). Máx 15 linhas.",
    "COPY": "Agente Copywriter de marketing digital. PT-BR. Alto impacto. Saída: HEADLINE 1/2/3 | HOOK VÍDEO | SCRIPT 30s (4 linhas) | LEGENDA POST | CTA | ANÚNCIO (4 linhas). Máx 15 linhas.",
    "DESIGN": "Agente Design Diretor de marketing digital. PT-BR. Técnico. Saída: ESTILO VISUAL | PALETA (4 cores hex) | TIPOGRAFIA | REFERÊNCIA | PROMPT IA imagem | PROMPT IA feed. Máx 15 linhas.",
    "VIDEO": "Agente de Vídeo de marketing digital. PT-BR. Técnico. Saída: ROTEIRO 30s (timestamps) | CENAS (3) | MOTION | PROMPT VÍDEO IA (inglês). Máx 15 linhas.",
    "SOCIAL": "Agente Social Media de marketing digital. PT-BR. Operacional. Saída: 7 posts — DIA X | formato | Objetivo | Legenda pronta | CTA. Máx 2 linhas por legenda.",
    "ADS": "Agente de Ads de marketing digital. PT-BR. Técnico. Saída: PLATAFORMA | PÚBLICO | ESTRUTURA (budget/conjuntos) | AD COPY | OTIMIZAÇÃO | MÉTRICAS-ALVO. Máx 15 linhas.",
    "AUTOMACAO": "Agente de Automação de marketing digital. PT-BR. Técnico. Saída: CAPTURA | NUTRIÇÃO D+0/1/3/5/7 | FOLLOW-UP | REMARKETING | RECUPERAÇÃO ABANDONO. Máx 15 linhas.",
    "PUBLICADOR": "Agente Publicador de marketing digital. PT-BR. Operacional. Saída: 3 peças prontas com Plataforma | Horário | Texto final | Hashtags (máx 10) + CHECKLIST 5 itens. Máx 20 linhas.",
    "ANALYTICS": "Agente Analytics de marketing digital. PT-BR. Técnico. Saída: PROJEÇÕES (CTR/CPC/CPL/conversão/ROI) | FUNIL | KPIs (5) | OTIMIZAÇÕES (5) | ALERTAS. Máx 15 linhas.",
}


async def fetch_page_content(url: str) -> str:
    """Fetch a sales page URL and extract readable text."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; MKT-Agency/1.0)"})
            html = resp.text
        # Remove script / style blocks
        html = re.sub(r'<(script|style|noscript)[^>]*>.*?</(script|style|noscript)>',
                      '', html, flags=re.DOTALL | re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r'<[^>]+>', ' ', html)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:3000]
    except Exception as exc:
        return f"(Não foi possível acessar a página: {exc})"


def _ev(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def run_agent(name: str, user_msg: str, queue: asyncio.Queue) -> str:
    logs = AGENT_LOGS.get(name, ["Processando..."])
    system = SYSTEMS[name]

    await queue.put(_ev({"type": "agent_event", "payload": {
        "agent": name, "status": "thinking", "task": logs[0], "progress": 5, "logs": [],
    }}))

    result = ""
    try:
        stream = await client.chat.completions.create(
            model=MODEL,
            max_tokens=900,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ],
            stream=True,
        )
        await queue.put(_ev({"type": "agent_event", "payload": {
            "agent": name, "status": "generating", "task": logs[1] if len(logs) > 1 else logs[0],
            "progress": 20, "logs": logs[:1],
        }}))
        chunk_count = 0
        async for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if not text:
                continue
            result += text
            await queue.put(_ev({"type": "chunk", "payload": {"agent": name, "text": text}}))
            chunk_count += 1
            if chunk_count % 25 == 0:
                pct = min(25 + (chunk_count // 25) * 12, 90)
                li = min(chunk_count // 35, len(logs) - 1)
                await queue.put(_ev({"type": "agent_event", "payload": {
                    "agent": name, "status": "generating", "task": logs[li],
                    "progress": pct, "logs": logs[:li + 1],
                }}))
    except Exception as e:
        await queue.put(_ev({"type": "agent_event", "payload": {
            "agent": name, "status": "error", "task": str(e), "progress": 0, "logs": [],
        }}))
        return ""

    await queue.put(_ev({"type": "agent_event", "payload": {
        "agent": name, "status": "completed", "task": "Concluído", "progress": 100, "logs": logs,
    }}))
    return result


def ctx(memory: dict, keys: list) -> str:
    return "\n".join(f"{k}:\n{memory[k][:250]}" for k in keys if k in memory)


async def run_agency(data: ProductInput) -> AsyncIterator[str]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    memory: dict[str, str] = {}

    async def fill():
        # Fetch sales page content if provided
        page_section = ""
        if data.pagina_vendas and data.pagina_vendas.strip():
            await queue.put(_ev({"type": "status", "payload": "Lendo página de vendas..."}))
            content = await fetch_page_content(data.pagina_vendas.strip())
            page_section = f"\n\nPÁGINA DE VENDAS (conteúdo extraído):\n{content}\n"

        # Phase 1 — Estratégia
        await queue.put(_ev({"type": "status", "payload": "Fase 1 — Estratégia"}))
        memory["ESTRATEGIA"] = await run_agent("ESTRATEGIA", queue=queue, user_msg=
            f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPÚBLICO: {data.publico}\n"
            f"OBJETIVO: {data.objetivo}\nPLATAFORMA: {data.plataforma}\nTOM: {data.tom_de_voz}"
            f"{page_section}\nGere estratégia completa baseada nessas informações.")

        # Phase 2 — Copy + Design + Video (paralelo)
        await queue.put(_ev({"type": "status", "payload": "Fase 2 — Copy, Design e Vídeo (paralelo)"}))
        r = await asyncio.gather(
            run_agent("COPY", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nTOM: {data.tom_de_voz}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie copies de alto impacto."),
            run_agent("DESIGN", queue=queue, user_msg=f"PRODUTO: {data.produto}\nTOM: {data.tom_de_voz}\nPÚBLICO: {data.publico}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie briefing visual completo. Inclua obrigatoriamente: PROMPT IA IMAGEM (em inglês, detalhado para geração com Midjourney/DALL-E)."),
            run_agent("VIDEO", queue=queue, user_msg=f"PRODUTO: {data.produto}\nTOM: {data.tom_de_voz}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie roteiro e prompts de vídeo."),
        )
        memory["COPY"], memory["DESIGN"], memory["VIDEO"] = r

        # Phase 3 — Social + Ads + Automação (paralelo)
        await queue.put(_ev({"type": "status", "payload": "Fase 3 — Social, Ads e Automação (paralelo)"}))
        r = await asyncio.gather(
            run_agent("SOCIAL", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPLATAFORMA: {data.plataforma}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie calendário 7 posts."),
            run_agent("ADS", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPÚBLICO: {data.publico}\nPLATAFORMA: {data.plataforma}\nOBJETIVO: {data.objetivo}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie campanha ads."),
            run_agent("AUTOMACAO", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie fluxo automação."),
        )
        memory["SOCIAL"], memory["ADS"], memory["AUTOMACAO"] = r

        # Phase 4 — Publicador
        await queue.put(_ev({"type": "status", "payload": "Fase 4 — Publicador"}))
        memory["PUBLICADOR"] = await run_agent("PUBLICADOR", queue=queue,
            user_msg=f"PLATAFORMA: {data.plataforma}\n{ctx(memory,['SOCIAL','COPY','ESTRATEGIA'])}\nPrepare peças.")

        # Phase 5 — Analytics
        await queue.put(_ev({"type": "status", "payload": "Fase 5 — Analytics"}))
        memory["ANALYTICS"] = await run_agent("ANALYTICS", queue=queue,
            user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPLATAFORMA: {data.plataforma}\nOBJETIVO: {data.objetivo}\n{ctx(memory,['ESTRATEGIA','ADS'])}\nGere projeções e KPIs.")

        await queue.put(_ev({"type": "done", "payload": "Agência concluiu todas as fases."}))
        await queue.put(None)

    asyncio.create_task(fill())
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
