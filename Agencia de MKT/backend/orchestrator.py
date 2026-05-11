import asyncio
import json
import os
import re
from typing import AsyncIterator
import httpx
from openai import AsyncOpenAI, RateLimitError
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
    "ADS":         ["Configurando públicos", "Estruturando campanha", "Criando ad copies", "Definindo orçamento"],
    "AUTOMACAO":   ["Captura de lead", "Sequência email", "WhatsApp flow", "Remarketing"],
    "PUBLICADOR":  ["Preparando conteúdo", "Formatando para plataforma", "Verificando checklist", "Finalizando publicação"],
    "ANALYTICS":   ["Calculando métricas", "Projetando funil", "Definindo KPIs", "Gerando sugestões"],
    "REVISOR":     ["Verificando consistência", "Analisando copy e estratégia", "Avaliando criativos", "Gerando relatório final"],
}

SYSTEMS = {
    "ESTRATEGIA": "Agente Estrategista de marketing digital. PT-BR. Curto e direto. Saída: • ICP • Dor • Desejo • Oferta • Posicionamento • Promessa • CTA • Funil (3 linhas). Máx 15 linhas.",
    "COPY":       "Agente Copywriter de marketing digital. PT-BR. Alto impacto. Saída: HEADLINE 1/2/3 | HOOK VÍDEO | SCRIPT 30s (4 linhas) | LEGENDA POST | CTA | ANÚNCIO (4 linhas). Máx 15 linhas.",
    "DESIGN":     "Agente Design Diretor de marketing digital. PT-BR. Técnico. Saída: ESTILO VISUAL | PALETA (4 cores hex) | TIPOGRAFIA | REFERÊNCIA | PROMPT IA IMAGEM (inglês, detalhado) | PROMPT IA FEED. Máx 15 linhas.",
    "VIDEO":      "Agente de Vídeo de marketing digital. PT-BR. Técnico. Saída: ROTEIRO 30s (timestamps) | CENAS (3) | MOTION | PROMPT VÍDEO IA (inglês). Máx 15 linhas.",
    "SOCIAL":     "Agente Social Media de marketing digital. PT-BR. Operacional. Saída: 7 posts — DIA X | formato | Objetivo | Legenda pronta | CTA. Máx 2 linhas por legenda.",
    "ADS":        "Agente de Ads de marketing digital. PT-BR. Técnico. Saída: PLATAFORMA | PÚBLICO | ORÇAMENTO SUGERIDO (R$ por plataforma/dia) | AD COPY | OTIMIZAÇÃO | MÉTRICAS-ALVO. Inclua obrigatoriamente seção ORÇAMENTO com valor diário por plataforma. Máx 18 linhas.",
    "AUTOMACAO":  "Agente de Automação de marketing digital. PT-BR. Técnico. Saída: CAPTURA | NUTRIÇÃO D+0/1/3/5/7 | FOLLOW-UP | REMARKETING | RECUPERAÇÃO ABANDONO. Máx 15 linhas.",
    "PUBLICADOR": "Agente Publicador de marketing digital. PT-BR. Operacional. Saída: 3 peças prontas com Plataforma | Horário | Texto final | Hashtags (máx 10) + CHECKLIST 5 itens. Máx 20 linhas.",
    "ANALYTICS":  "Agente Analytics de marketing digital. PT-BR. Técnico. Saída: PROJEÇÕES (CTR/CPC/CPL/conversão/ROI) | FUNIL | KPIs (5) | OTIMIZAÇÕES (5) | ALERTAS. Máx 15 linhas.",
    "REVISOR":    "Agente Revisor Chefe de marketing digital. PT-BR. Analítico e preciso. Revise todas as entregas dos agentes. Saída: ✓ PONTOS FORTES (3 bullets) | ⚠ MELHORIAS (3 bullets) | 🚨 ALERTAS (2 bullets) | SCORE GERAL: X/10 | RESUMO EXECUTIVO (3 linhas). Máx 18 linhas.",
}


async def fetch_page_content(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            resp = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; MagaOne/1.0)"})
            html = resp.text
        html = re.sub(r'<(script|style|noscript)[^>]*>.*?</(script|style|noscript)>',
                      '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:3000]
    except Exception as exc:
        return f"(Não foi possível acessar a página: {exc})"


def _ev(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def run_agent(name: str, user_msg: str, queue: asyncio.Queue) -> str:
    logs   = AGENT_LOGS.get(name, ["Processando..."])
    system = SYSTEMS[name]

    await queue.put(_ev({"type": "agent_event", "payload": {
        "agent": name, "status": "thinking", "task": logs[0], "progress": 5, "logs": [],
    }}))

    result = ""
    for attempt in range(4):          # até 4 tentativas com backoff
        try:
            stream = await client.chat.completions.create(
                model=MODEL,
                max_tokens=800,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ],
                stream=True,
            )
            await queue.put(_ev({"type": "agent_event", "payload": {
                "agent": name, "status": "generating",
                "task": logs[1] if len(logs) > 1 else logs[0],
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
                    li  = min(chunk_count // 35, len(logs) - 1)
                    await queue.put(_ev({"type": "agent_event", "payload": {
                        "agent": name, "status": "generating", "task": logs[li],
                        "progress": pct, "logs": logs[:li + 1],
                    }}))
            break   # sucesso — sai do loop de tentativas

        except RateLimitError:
            wait = 8 * (attempt + 1)   # 8s, 16s, 24s
            await queue.put(_ev({"type": "agent_event", "payload": {
                "agent": name, "status": "thinking",
                "task": f"Limite de requisições — aguardando {wait}s...",
                "progress": 10, "logs": [],
            }}))
            await asyncio.sleep(wait)

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
    return "\n".join(f"{k}:\n{memory[k][:300]}" for k in keys if k in memory)


async def run_agency(data: ProductInput) -> AsyncIterator[str]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    memory: dict[str, str] = {}

    async def fill():
        # Fetch sales page if provided
        page_section = ""
        if data.pagina_vendas and data.pagina_vendas.strip():
            await queue.put(_ev({"type": "status", "payload": "Lendo página de vendas..."}))
            content = await fetch_page_content(data.pagina_vendas.strip())
            page_section = f"\n\nPÁGINA DE VENDAS:\n{content}\n"

        # Phase 1 — Estratégia
        await queue.put(_ev({"type": "status", "payload": "Fase 1 — Estratégia"}))
        memory["ESTRATEGIA"] = await run_agent("ESTRATEGIA", queue=queue, user_msg=
            f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPÚBLICO: {data.publico}\n"
            f"OBJETIVO: {data.objetivo}\nPLATAFORMA: {data.plataforma}\nTOM: {data.tom_de_voz}"
            f"{page_section}\nGere estratégia completa.")

        # Phase 2 — Copy + Design + Video (escalonado para evitar rate limit)
        await queue.put(_ev({"type": "status", "payload": "Fase 2 — Copy, Design e Vídeo (paralelo)"}))

        async def run_copy():
            return await run_agent("COPY", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nTOM: {data.tom_de_voz}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie copies de alto impacto.")
        async def run_design():
            await asyncio.sleep(3)
            return await run_agent("DESIGN", queue=queue, user_msg=f"PRODUTO: {data.produto}\nTOM: {data.tom_de_voz}\nPÚBLICO: {data.publico}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie briefing visual. Inclua PROMPT IA IMAGEM detalhado em inglês.")
        async def run_video():
            await asyncio.sleep(6)
            return await run_agent("VIDEO", queue=queue, user_msg=f"PRODUTO: {data.produto}\nTOM: {data.tom_de_voz}\n{ctx(memory,['ESTRATEGIA'])}{page_section}\nCrie roteiro e prompts de vídeo.")

        r = await asyncio.gather(run_copy(), run_design(), run_video())
        memory["COPY"], memory["DESIGN"], memory["VIDEO"] = r

        # Phase 3 — Social + Ads + Automação (escalonado)
        await queue.put(_ev({"type": "status", "payload": "Fase 3 — Social, Ads e Automação (paralelo)"}))

        async def run_social():
            return await run_agent("SOCIAL", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPLATAFORMA: {data.plataforma}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie calendário 7 posts.")
        async def run_ads():
            await asyncio.sleep(3)
            return await run_agent("ADS", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPÚBLICO: {data.publico}\nPLATAFORMA: {data.plataforma}\nOBJETIVO: {data.objetivo}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie campanha ads com orçamento por plataforma.")
        async def run_automacao():
            await asyncio.sleep(6)
            return await run_agent("AUTOMACAO", queue=queue, user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\n{ctx(memory,['ESTRATEGIA','COPY'])}\nCrie fluxo de automação.")

        r = await asyncio.gather(run_social(), run_ads(), run_automacao())
        memory["SOCIAL"], memory["ADS"], memory["AUTOMACAO"] = r

        # Phase 4 — Publicador
        await queue.put(_ev({"type": "status", "payload": "Fase 4 — Publicador"}))
        memory["PUBLICADOR"] = await run_agent("PUBLICADOR", queue=queue,
            user_msg=f"PLATAFORMA: {data.plataforma}\n{ctx(memory,['SOCIAL','COPY','ESTRATEGIA'])}\nPrepare peças prontas para publicação.")

        # Phase 5 — Analytics
        await queue.put(_ev({"type": "status", "payload": "Fase 5 — Analytics"}))
        memory["ANALYTICS"] = await run_agent("ANALYTICS", queue=queue,
            user_msg=f"PRODUTO: {data.produto}\nPREÇO: {data.preco}\nPLATAFORMA: {data.plataforma}\nOBJETIVO: {data.objetivo}\n{ctx(memory,['ESTRATEGIA','ADS'])}\nGere projeções e KPIs.")

        # Phase 6 — Revisão Final
        await queue.put(_ev({"type": "status", "payload": "Fase 6 — Revisão Final"}))
        memory["REVISOR"] = await run_agent("REVISOR", queue=queue,
            user_msg=f"Revise toda a campanha:\n{ctx(memory,['ESTRATEGIA','COPY','DESIGN','ADS','PUBLICADOR','ANALYTICS'])}\nGere análise crítica completa.")

        await queue.put(_ev({"type": "done", "payload": "Maga One concluiu todas as fases."}))
        await queue.put(None)

    asyncio.create_task(fill())
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
