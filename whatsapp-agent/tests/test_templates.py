"""Sanidade dos templates: Jinja renderiza e o JavaScript embutido é válido.

`dashboard.html` tem ~200 KB de JavaScript dentro do HTML, sem bundler e sem
lint. Um erro de sintaxe ali quebra o painel inteiro em produção e nada avisa
antes. Estes testes são a verificação mínima possível desse arquivo.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "backend" / "templates"
ARQUIVOS = sorted(TEMPLATES.glob("*.html"))

# Blocos <script> que não são JavaScript (JSON-LD, templates de terceiros).
NAO_JS = re.compile(r'type\s*=\s*"(?!text/javascript|module)[^"]*"', re.IGNORECASE)
SCRIPT = re.compile(r"<script([^>]*)>(.*?)</script>", re.IGNORECASE | re.S)
# Jinja dentro do JS: trocado por um literal antes de validar a sintaxe.
JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def test_existem_templates():
    assert ARQUIVOS, "nenhum template encontrado"


class _Permissivo(dict):
    """Dicionário que devolve "" para qualquer chave/atributo ausente.

    Serve para renderizar um template sem montar o contexto real: o objetivo é
    pegar erro de SINTAXE Jinja e filtro inexistente, não validar conteúdo.
    """

    def __missing__(self, key):            # tenant["x"]
        return ""

    def __getattr__(self, key):            # tenant.x — o Jinja tenta getattr
        return self[key]                   # antes de getitem; delega ao dict


def _contexto() -> dict:
    nomes = ("contract", "settings", "content", "terms", "patient",
             "appointment", "template", "data", "stats", "cfg", "site")
    ctx = {n: _Permissivo() for n in nomes}
    # `dashboard.html` formata estes dois com '%02d', então precisam ser números.
    ctx["tenant"] = _Permissivo(working_hours_start=8, working_hours_end=19,
                                session_minutes=50)
    ctx.update({
        "request": None, "testimonials": [], "slots": [], "appointments": [],
        "patients": [], "contracts": [], "segments": [], "terms_version": "",
        "on_evolution": False, "token": "", "slug": "", "error": "",
    })
    return ctx


@pytest.mark.parametrize("arquivo", ARQUIVOS, ids=lambda p: p.name)
def test_template_renderiza(arquivo: Path):
    """Renderiza com um contexto permissivo: pega erro de sintaxe do Jinja e
    filtro inexistente, que derrubam a página em produção."""
    from jinja2 import ChainableUndefined, Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                      undefined=ChainableUndefined)
    env.filters.setdefault("tojson", lambda v, **k: "{}")
    tpl = env.get_template(arquivo.name)
    saida = tpl.render(**_contexto())
    assert saida.strip(), f"{arquivo.name} renderizou vazio"


def _blocos_js(arquivo: Path) -> list[str]:
    """Blocos <script> com corpo próprio (ignora `src=` e tipos não-JS)."""
    html = arquivo.read_text(encoding="utf-8")
    blocos = []
    for atributos, corpo in SCRIPT.findall(html):
        if "src=" in atributos.lower():
            continue
        if NAO_JS.search(atributos or ""):
            continue
        if corpo.strip():
            blocos.append(corpo)
    return blocos


# Templates sem JavaScript embutido: só texto e, no máximo, <script src=...>.
# Listados explicitamente para que um template que PERCA seu JavaScript (ou que
# passe a ter JavaScript novo) apareça como falha, em vez de virar um teste
# ignorado que ninguém lê.
SEM_JS_ESPERADO = {"contrato_assinatura.html", "privacidade.html", "termos.html"}

COM_JS = [a for a in ARQUIVOS if _blocos_js(a)]


def test_inventario_de_templates_sem_javascript():
    sem_js = {a.name for a in ARQUIVOS if not _blocos_js(a)}
    assert sem_js == SEM_JS_ESPERADO, (
        "mudou o conjunto de templates sem JavaScript embutido. "
        f"Entraram: {sorted(sem_js - SEM_JS_ESPERADO)}; "
        f"saíram: {sorted(SEM_JS_ESPERADO - sem_js)}. "
        "Atualize SEM_JS_ESPERADO depois de conferir que a mudança é intencional."
    )
    assert COM_JS, "nenhum template com JavaScript embutido — filtro quebrado?"


@pytest.mark.skipif(not shutil.which("node"), reason="node não disponível")
@pytest.mark.parametrize("arquivo", COM_JS, ids=lambda p: p.name)
def test_javascript_embutido_tem_sintaxe_valida(arquivo: Path):
    blocos = _blocos_js(arquivo)
    for i, corpo in enumerate(blocos):
        # `{{ ... }}` é interpolado pelo servidor; para o parser basta um literal.
        limpo = JINJA.sub("null", corpo)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(limpo)
            caminho = f.name
        try:
            r = subprocess.run(["node", "--check", caminho],
                               capture_output=True, text=True, timeout=60)
        finally:
            Path(caminho).unlink(missing_ok=True)
        assert r.returncode == 0, (
            f"{arquivo.name}, bloco <script> #{i + 1}: {r.stderr.strip()[:400]}"
        )
