"""Prévia local das páginas de venda (E27A) — sem banco, sem variáveis, sem rede de saída.

Renderiza landing.html e landing_vertical.html exatamente como as rotas de
main.py (mesmos contextos), servindo /static do mesmo backend. O include
_gtag.html (tag do Google Ads) é substituído por vazio: a prévia nunca registra
visita nem conversão na conta real.

Uso: python3 servidor.py <dir-backend> <porta>
"""
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

BACKEND, PORTA = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, BACKEND)
from landing_content import content_for  # noqa: E402

env = Environment(loader=ChoiceLoader([
    DictLoader({"_gtag.html": "<!-- gtag desligado na prévia -->"}),
    FileSystemLoader(f"{BACKEND}/templates"),
]), autoescape=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory=f"{BACKEND}/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return env.get_template("landing.html").render(request=request, testimonials=[])


@app.get("/p/{segment}", response_class=HTMLResponse)
def landing_vertical(request: Request, segment: str):
    c = content_for(segment)
    if c is None:
        return RedirectResponse("/", status_code=302)
    return env.get_template("landing_vertical.html").render(request=request, c=c)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_level="warning")
