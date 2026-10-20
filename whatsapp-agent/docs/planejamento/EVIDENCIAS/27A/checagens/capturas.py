"""Screenshots de página inteira + checagens objetivas (vazamento horizontal,
erros de JS, recursos 4xx/5xx, violeta residual nos estilos computados).

Uso: python capturas.py <base-url> <dir-saida> <prefixo> [caminhos...]
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, SAIDA, PREFIXO = sys.argv[1].rstrip("/"), Path(sys.argv[2]), sys.argv[3]
CAMINHOS = sys.argv[4:] or ["/", "/p/nutricao"]
LARGURAS = [(1440, "computador"), (768, "tablet"), (390, "celular")]
SAIDA.mkdir(parents=True, exist_ok=True)

# Tons do violeta antigo (Tailwind violet/purple + os HEX do CSS inline).
VIOLETAS = ["124, 58, 237", "168, 85, 247", "139, 92, 246", "167, 139, 250", "237, 233, 254",
            "245, 243, 255", "221, 214, 254", "109, 40, 217", "91, 33, 182", "76, 29, 149",
            "233, 213, 255", "196, 181, 253", "250, 245, 255", "26, 11, 46", "45, 22, 87", "59, 23, 129"]

JS_VIOLETA = """(violetas) => {
  const achados = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    const props = [cs.color, cs.backgroundColor, cs.borderTopColor, cs.borderLeftColor,
                   cs.backgroundImage, cs.boxShadow, cs.fill, cs.stroke];
    for (const v of props) for (const vi of violetas) if (v && v.includes(vi)) achados.add(el.tagName + '.' + (el.className.baseVal ?? el.className).toString().slice(0, 50) + ' → ' + vi);
    for (const pseudo of ['::before', '::after']) {
      const p = getComputedStyle(el, pseudo);
      for (const v of [p.borderTopColor, p.backgroundImage, p.backgroundColor])
        for (const vi of violetas) if (p.content !== 'none' && v && v.includes(vi)) achados.add(el.tagName + pseudo + ' → ' + vi);
    }
  }
  return [...achados].slice(0, 30);
}"""

relatorio = []
with sync_playwright() as p:
    nav = p.chromium.launch()
    for largura, nome in LARGURAS:
        for caminho in CAMINHOS:
            ctx = nav.new_context(viewport={"width": largura, "height": 900}, reduced_motion="reduce",
                                  device_scale_factor=1)
            pg = ctx.new_page()
            erros, falhas = [], []
            pg.on("pageerror", lambda e: erros.append(str(e)))
            pg.on("console", lambda m: erros.append(m.text) if m.type == "error" else None)
            pg.on("response", lambda r: falhas.append(f"{r.status} {r.url}") if r.status >= 400 else None)
            pg.goto(BASE + caminho, wait_until="networkidle")
            pg.wait_for_timeout(1500)
            altura = pg.evaluate("document.documentElement.scrollHeight")
            vaza = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            violeta = pg.evaluate(JS_VIOLETA, VIOLETAS)
            fontes = pg.evaluate("""() => ({h1: getComputedStyle(document.querySelector('h1')).fontFamily.split(',')[0],
                                           corpo: getComputedStyle(document.body).fontFamily.split(',')[0],
                                           carregadas: [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family + ' ' + f.weight).filter((v,i,a)=>a.indexOf(v)===i)})""")
            pg.set_viewport_size({"width": largura, "height": altura})
            pg.wait_for_timeout(400)
            slug = "inicio" if caminho == "/" else caminho.strip("/").replace("/", "-")
            arq = SAIDA / f"{PREFIXO}-{slug}-{largura}-{nome}.png"
            pg.screenshot(path=str(arq))
            relatorio.append({"pagina": caminho, "largura": largura, "altura": altura,
                              "vazamento_px": vaza, "erros_js": erros, "http_4xx_5xx": falhas,
                              "violeta": violeta, "fontes": fontes, "arquivo": arq.name})
            ctx.close()
    nav.close()

print(json.dumps(relatorio, ensure_ascii=False, indent=1))
