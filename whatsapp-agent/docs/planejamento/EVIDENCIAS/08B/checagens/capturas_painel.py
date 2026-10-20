"""Prévia do E08B no navegador: capturas do Início (psicologia e nutrição) em
1440/768/390 e checagens objetivas. Também renderiza os ESTADOS do quadro
(vazio, sem edição, falha, sem novidade) com dados fictícios.

Bloqueia qualquer requisição para fora de 127.0.0.1 que não seja CDN de
estilo/fonte (Tailwind e Google Fonts, como no painel publicado).

Uso: python capturas_painel.py <base-url> <dir-saida>
"""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, SAIDA = sys.argv[1].rstrip("/"), Path(sys.argv[2])
SAIDA.mkdir(parents=True, exist_ok=True)
PAINEIS = {
    "psicologia": "/dashboard/consultorio-ficticio?token=previa-e08b-psi",
    "nutricao": "/dashboard/nutri-ficticio?token=previa-e08b-nutri",
}
LARGURAS = [(1440, "computador"), (768, "tablet"), (390, "celular")]
# O modal de boas-vindas aparece na 1ª visita; a prévia simula quem já o fechou.
SEM_BOAS_VINDAS = "for (const k of ['ci_welcome_done_consultorio-ficticio', 'ci_welcome_done_nutri-ficticio']) { try { localStorage.setItem(k, '1'); } catch (e) {} }"
PERMITIDOS = re.compile(r"^(http://127\.0\.0\.1:\d+/|https://cdn\.tailwindcss\.com/|https://fonts\.(googleapis|gstatic)\.com/)")

CHECA = """() => {
  const vis = id => { const e = document.getElementById(id); return !!e && !e.classList.contains('hidden') && e.offsetParent !== null; };
  const foco = document.getElementById('foco');
  const ins = document.getElementById('home-insights');
  return {
    vazamento: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    foco_visivel: vis('foco'),
    titulo: document.getElementById('foco-titulo')?.textContent,
    narrativa_visivel: vis('hi-narrative'),
    faixa_ficticio: vis('foco-ficticio'),
    itens: document.querySelectorAll('#foco-lista > li').length,
    itens_visiveis: [...document.querySelectorAll('#foco-lista > li')].filter(li => !li.hidden).length,
    links_no_quadro: document.querySelectorAll('#foco-lista a').length,
    cards_operacionais: document.querySelectorAll('#hi-body > *').length,
    quadro_hoje: vis('agenda-summary'),
    saudacao: document.getElementById('hi-greeting')?.textContent,
    estado: document.getElementById('foco-estado')?.textContent,
    foco_depois_do_resumo: !!(ins && foco && (ins.compareDocumentPosition(foco) & Node.DOCUMENT_POSITION_FOLLOWING)),
    foco_depois_das_consultas: !!(foco && (document.getElementById('agenda-list').compareDocumentPosition(foco) & Node.DOCUMENT_POSITION_FOLLOWING)),
    foco_na_coluna_lateral: !!(foco && ins && foco.parentElement === ins.parentElement),
  };
}"""

ESTADOS = {
    "sem-edicao": {"habilitado": True, "titulo": "Psicologia em foco", "itens": [], "visiveis": 3,
                   "situacao": "sem_edicao", "verificado_em": None, "conteudo_atualizado_em": None,
                   "aviso": None, "ficticio": True, "fontes": [{"nome": "Conselho (FICTÍCIO)"}]},
    "semana-vazia": {"habilitado": True, "titulo": "Psicologia em foco", "itens": [], "visiveis": 3,
                     "situacao": "sem_recentes", "verificado_em": "2026-10-20T12:00:30+00:00",
                     "conteudo_atualizado_em": "2026-10-18T12:00:20+00:00", "aviso": None, "ficticio": True,
                     "fontes": [{"nome": "Conselho (FICTÍCIO)"}, {"nome": "Ministério (FICTÍCIO)"}]},
    "falha-com-cache": {"habilitado": True, "titulo": "Psicologia em foco", "visiveis": 3, "situacao": "ok",
                        "itens": [{"titulo": "[FICTÍCIO] Orientação sobre registro de atendimento on-line",
                                   "resumo": "Texto fictício.", "url": "https://noticias-ficticias.invalid/x",
                                   "fonte": "Conselho (FICTÍCIO)", "publicado_em": "2026-10-19T17:00:00+00:00"}],
                        "verificado_em": "2026-10-19T12:00:40+00:00", "conteudo_atualizado_em": "2026-10-19T12:00:40+00:00",
                        "aviso": {"tipo": "falha", "em": "2026-10-20T14:31:00+00:00",
                                  "texto": "Não foi possível consultar as fontes na última tentativa. Exibindo o conteúdo verificado anteriormente."},
                        "ficticio": True, "fontes": [{"nome": "Conselho (FICTÍCIO)"}]},
}

relatorio = {}
with sync_playwright() as p:
    nav = p.chromium.launch()
    for seg, caminho in PAINEIS.items():
        for largura, nome in LARGURAS:
            ctx = nav.new_context(viewport={"width": largura, "height": 900}, reduced_motion="reduce")
            externos = []
            ctx.route(re.compile(r".*"), lambda rota: rota.continue_() if PERMITIDOS.match(rota.request.url)
                      else (externos.append(rota.request.url), rota.abort()))
            ctx.add_init_script(SEM_BOAS_VINDAS)
            pg = ctx.new_page()
            erros = []
            pg.on("pageerror", lambda e: erros.append(str(e)))
            pg.on("console", lambda m: erros.append(m.text) if m.type == "error" and "ERR_FAILED" not in m.text else None)
            pg.goto(BASE + caminho, wait_until="load")
            pg.wait_for_timeout(3000)
            r = pg.evaluate(CHECA)
            r.update({"erros_js": erros, "requisicoes_externas_bloqueadas": externos})
            altura = pg.evaluate("document.documentElement.scrollHeight")
            pg.set_viewport_size({"width": largura, "height": altura})
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(SAIDA / f"{seg}-inicio-{largura}-{nome}.png"))
            if seg == "psicologia":
                pg.set_viewport_size({"width": largura, "height": 900})
                pg.locator("#foco").screenshot(path=str(SAIDA / f"psicologia-quadro-{largura}-{nome}.png"))
            relatorio[f"{seg}-{largura}"] = r
            ctx.close()

    # Estados do quadro (dados fictícios montados no próprio navegador)
    ctx = nav.new_context(viewport={"width": 1280, "height": 1400}, reduced_motion="reduce")
    ctx.route(re.compile(r".*"), lambda rota: rota.continue_() if PERMITIDOS.match(rota.request.url) else rota.abort())
    ctx.add_init_script(SEM_BOAS_VINDAS)
    pg = ctx.new_page()
    pg.goto(BASE + PAINEIS["psicologia"], wait_until="load")
    pg.wait_for_timeout(3000)
    for nome, dado in ESTADOS.items():
        pg.evaluate("d => _focoRender(d)", dado)
        pg.locator("#foco").screenshot(path=str(SAIDA / f"estado-{nome}.png"))
        relatorio[f"estado-{nome}"] = pg.evaluate("() => ({lista: document.getElementById('foco-lista').innerText, "
                                                   "aviso: document.getElementById('foco-aviso').innerText, "
                                                   "estado: document.getElementById('foco-estado').innerText})")
    # Botão "Atualizar" logo após a verificação: deve dizer que só releu o cache.
    pg.evaluate("() => carregarFoco()")
    pg.wait_for_timeout(800)
    pg.click("#foco-atualizar")
    pg.wait_for_timeout(2500)
    relatorio["botao-atualizar"] = pg.evaluate("() => document.getElementById('foco-msg').innerText")
    pg.locator("#foco").screenshot(path=str(SAIDA / "estado-atualizar-leitura-cache.png"))
    ctx.close()
    nav.close()

print(json.dumps(relatorio, ensure_ascii=False, indent=1))
