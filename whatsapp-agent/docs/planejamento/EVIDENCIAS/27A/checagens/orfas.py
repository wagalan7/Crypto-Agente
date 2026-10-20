"""Palavra órfã nos dois títulos grandes (herói e CTA final), 13 páginas × 4 larguras.
Órfã = linha visual com uma palavra só que não fecha a frase. Também conta linhas."""
from playwright.sync_api import sync_playwright
SEGS = ["/", "/p/nutricao", "/p/fisioterapia", "/p/fonoaudiologia", "/p/odontologia", "/p/terapia",
        "/p/estetica", "/p/advocacia", "/p/contabilidade", "/p/salao", "/p/barbearia", "/p/petshop", "/p/oficina"]
JS = """(sel) => { const h = document.querySelector(sel); const r = document.createRange(); const linhas = {};
  const w = document.createTreeWalker(h, NodeFilter.SHOW_TEXT); let n;
  while ((n = w.nextNode())) for (const m of n.textContent.matchAll(/\\S+/g)) {
      r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
      const t = Math.round(r.getBoundingClientRect().top); (linhas[t] = linhas[t] || []).push(m[0]); }
  return Object.keys(linhas).sort((a,b)=>a-b).map(k => linhas[k].join(' ')); }"""
orfas = {"antes": [], "depois": []}; maxl = {}
with sync_playwright() as p:
    nav = p.chromium.launch()
    for w in (390, 768, 1024, 1440):
        for porta, tag in ((8771, "antes"), (8772, "depois")):
            pg = nav.new_page(viewport={"width": w, "height": 900})
            for c in SEGS:
                pg.goto(f"http://127.0.0.1:{porta}{c}", wait_until="networkidle")
                for sel in ("h1", ".cta-bg h2"):
                    ls = pg.evaluate(JS, sel)
                    k = (tag, w, sel); maxl[k] = max(maxl.get(k, 0), len(ls))
                    if any(len(l.split()) == 1 and not l.endswith(("?", ".")) for l in ls):
                        orfas[tag].append((w, c, sel, ls))
            pg.close()
    nav.close()
print("órfãs (de 104 títulos):", {k: len(v) for k, v in orfas.items()})
for t, v in orfas.items():
    for x in v[:3]: print(" ", t, x)
print("máximo de linhas por largura (antes → depois):")
for w in (390, 768, 1024, 1440):
    print(f"  {w:5}  herói {maxl[('antes', w, 'h1')]} → {maxl[('depois', w, 'h1')]}   CTA {maxl[('antes', w, '.cta-bg h2')]} → {maxl[('depois', w, '.cta-bg h2')]}")
