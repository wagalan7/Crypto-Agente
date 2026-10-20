"""No fim da página, o botão flutuante do WhatsApp cobre algum link do rodapé?"""
from playwright.sync_api import sync_playwright
JS = """() => { window.scrollTo(0, document.documentElement.scrollHeight);
  const f = document.querySelector('.whatsapp-float').getBoundingClientRect();
  return [...document.querySelectorAll('footer a')].filter(a => { const r = a.getBoundingClientRect();
    return !(r.right <= f.left || r.left >= f.right || r.bottom <= f.top || r.top >= f.bottom); }).map(a => a.textContent.trim()); }"""
with sync_playwright() as p:
    nav = p.chromium.launch()
    for w, h in ((390, 844), (768, 1024), (1024, 768), (1280, 800), (1440, 900)):
        for porta, tag in ((8771, "antes"), (8772, "depois")):
            pg = nav.new_page(viewport={"width": w, "height": h})
            res = {}
            for c in ("/", "/p/nutricao"):
                pg.goto(f"http://127.0.0.1:{porta}{c}", wait_until="networkidle"); pg.wait_for_timeout(300)
                res[c] = pg.evaluate(JS)
            print(f"{w:5} {tag:6} links do rodapé cobertos: {res}")
            pg.close()
    nav.close()
