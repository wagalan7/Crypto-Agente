"""Infraestrutura dos testes do boletim de notícias (E08B).

Mesmas garantias da suíte principal, para valer também quando estes testes
rodam sozinhos (branch sem `tests/conftest.py`):

1. **Nunca toca produção:** `DATA_DIR` temporário, definido antes de importar
   o backend; valor que pareça produção aborta a coleta.
2. **Nunca acessa a internet:** todo HTTP passa por `HttpFalso`
   (`httpx.MockTransport`); um pedido a URL não cadastrada falha o teste.
3. **Nunca gasta API paga:** chaves de IA zeradas; o job real fica desligado.

Os feeds daqui são **fictícios** (títulos e textos inventados para teste, com a
mesma estrutura dos feeds reais conferidos em 20/10/2026).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta

import pytest

_dd = os.environ.get("DATA_DIR", "").strip()
if _dd:
    if _dd.rstrip("/") in ("/data", ".", "") or "/app" in _dd:
        raise RuntimeError(f"DATA_DIR={_dd!r} parece ser produção. Os testes recusam rodar assim.")
else:
    _dd = tempfile.mkdtemp(prefix="agente-noticias-testes-")
    os.environ["DATA_DIR"] = _dd

os.environ["GROQ_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["WHATSAPP_PROVIDER"] = "mock"
os.environ.setdefault("MASTER_KEY", "master-de-testes")
os.environ.setdefault("ADMIN_PASSWORD", "suite-de-testes")
os.environ.setdefault("INSTANCE_MONITOR_ENABLED", "0")
os.environ["NOTICIAS_JOB_ENABLED"] = "0"
os.environ.pop("NOTICIAS_FIXTURES", None)
os.environ.pop("NOTICIAS_HABILITADO", None)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import database as db      # noqa: E402
import noticias as nt      # noqa: E402
import noticias_job as nj  # noqa: E402

assert os.path.dirname(db.DB_PATH).rstrip("/") == _dd.rstrip("/"), f"banco fora do temporário: {db.DB_PATH}"
db.init_db()

from .apoio_noticias import (  # noqa: E402
    HttpFalso, URL_CFP, URL_MS, atom, em_brasilia, link_cfp, link_ms, rss2)


@pytest.fixture(autouse=True)
def _banco_limpo(monkeypatch):
    with db.get_conn() as conn:
        for t in ("noticias_itens", "noticias_fontes_estado", "noticias_edicoes", "noticias_execucoes"):
            conn.execute(f"DELETE FROM {t}")
    monkeypatch.delenv("NOTICIAS_HABILITADO", raising=False)
    monkeypatch.delenv("NOTICIAS_FIXTURES", raising=False)
    yield


@pytest.fixture
def http():
    return HttpFalso()


@pytest.fixture
def feeds_padrao(http):
    """Uma semana normal: 3 itens relevantes do CFP, 1 institucional, 1 antigo;
    no Ministério, 1 de saúde mental e 1 fora do tema. Agora = 20/10/2026 10:00
    (Brasília)."""
    agora = em_brasilia(2026, 10, 20, 10, 0)
    http.responder(URL_CFP, rss2([
        {"titulo": "CFP publica resolução fictícia sobre atendimento online", "link": link_cfp(1),
         "data": agora - timedelta(days=1), "desc": "Resolução fictícia de teste sobre atendimento psicológico online."},
        {"titulo": "Prazo fictício de recadastramento é prorrogado", "link": link_cfp(2),
         "data": agora - timedelta(days=3), "desc": "Psicólogas têm mais prazo no cenário fictício."},
        {"titulo": "Consulta pública fictícia sobre referências técnicas em psicologia", "link": link_cfp(3),
         "data": agora - timedelta(days=5)},
        {"titulo": "Nota de pesar fictícia", "link": link_cfp(4), "data": agora - timedelta(days=2)},
        {"titulo": "Orientação fictícia antiga sobre ética", "link": link_cfp(5), "data": agora - timedelta(days=9)},
    ]))
    http.responder(URL_MS, atom([
        {"titulo": "Ministério amplia rede fictícia de CAPS para saúde mental", "link": link_ms(1),
         "data": agora - timedelta(days=2)},
        {"titulo": "Ministério inaugura hospital fictício de cardiologia", "link": link_ms(2),
         "data": agora - timedelta(hours=5)},
    ]), tipo="application/atom+xml")
    return agora


@pytest.fixture
def tenants():
    """Três consultórios: dois de psicologia (dividem um boletim) e um de
    nutrição (sem boletim). Devolve {slug: tenant}."""
    import secrets
    criados = {}
    for slug, seg in (("psi-a", "psicologia"), ("psi-b", "psicologia"), ("nutri", "nutricao")):
        if not db.get_tenant(slug):
            db.create_tenant(slug=slug, name=f"Consultório fictício {slug}", psychologist_name="Profissional Fictícia")
        db.update_tenant(slug, segment=seg, dashboard_token=f"token-{slug}-{secrets.token_hex(4)}",
                         ai_narrative_enabled=1, active=1)
        criados[slug] = db.get_tenant(slug)
    return criados
