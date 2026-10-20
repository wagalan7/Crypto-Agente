"""E08B — painel: leitura só do cache, coleta compartilhada pelo segmento,
isolamento de segmento, autenticação, botão "Atualizar" e substituição do
quadro narrativo (sem tocar nos cards operacionais)."""
from __future__ import annotations

import re
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database as db
import main
import noticias as nt
import noticias_job as nj
from apoio_noticias import Relogio, URL_CFP, URL_MS, em_brasilia

DASH = Path(main.__file__).parent / "templates" / "dashboard.html"


@pytest.fixture
def cliente():
    main._noticias_ultimo_pedido.clear()
    return TestClient(main.app)


def _h(tenant):
    return {"X-Dashboard-Token": tenant["dashboard_token"]}


def _gerar_edicao(http, quando):
    nj.tick("worker-1", transporte=http.transporte, relogio=Relogio(quando))


@pytest.fixture
def sem_internet(monkeypatch):
    """Qualquer tentativa de coleta durante o teste falha o teste."""
    def proibido(*a, **k):
        raise AssertionError("o painel tentou buscar notícia na internet")
    monkeypatch.setattr(nt, "baixar", proibido)


# ── Autenticação e segmento ───────────────────────────────────────────────────

def test_sem_token_ou_token_invalido_nao_le_o_boletim(cliente, tenants):
    assert cliente.get("/dashboard/api/noticias").status_code == 403
    assert cliente.get("/dashboard/api/noticias", headers={"X-Dashboard-Token": "x"}).status_code == 403
    assert cliente.post("/dashboard/api/noticias/revisar").status_code == 403


def test_operacao_exige_master_key(cliente):
    assert cliente.get("/admin/noticias/estado").status_code == 403
    assert cliente.post("/admin/noticias/psicologia/revisar").status_code == 403
    r = cliente.get("/admin/noticias/estado", headers={"X-Master-Key": "master-de-testes"})
    assert r.status_code == 200 and "pendencias" in r.json()


def test_psicologia_recebe_o_boletim_e_nutricao_nao(cliente, tenants, http, feeds_padrao):
    _gerar_edicao(http, feeds_padrao)
    psi = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json()
    assert psi["habilitado"] and psi["titulo"] == "Psicologia em foco" and psi["itens"]
    assert cliente.get("/dashboard/api/noticias", headers=_h(tenants["nutri"])).json() == {"habilitado": False}


@pytest.mark.parametrize("segmento", [None, "", "desconhecido", "psicologia-infantil", "nutricao"])
def test_segmento_desconhecido_ou_vazio_nao_recebe_psicologia_em_silencio(segmento):
    assert nt.segmento_do_tenant({"segment": segmento}) is None
    assert nj.ler_boletim({"segment": segmento}) == {"habilitado": False}


def test_mapeamento_explicito_aceita_so_a_grafia_do_segmento():
    assert nt.segmento_do_tenant({"segment": "psicologia"}) == "psicologia"
    assert nt.segmento_do_tenant({"segment": " Psicologia "}) == "psicologia"


def test_chave_geral_desliga_o_quadro(cliente, tenants, monkeypatch):
    monkeypatch.setenv("NOTICIAS_HABILITADO", "0")
    assert cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json() == {"habilitado": False}


# ── Cache e compartilhamento ──────────────────────────────────────────────────

def test_abrir_o_painel_so_le_o_cache(cliente, tenants, http, feeds_padrao, sem_internet):
    with db.get_conn() as conn:   # edição já existente, gerada pelo job
        conn.execute("""INSERT INTO noticias_edicoes VALUES ('psicologia', '[]', 'x',
                        '2026-10-20T12:00:00+00:00', '2026-10-20T12:00:00+00:00', 'diaria:2026-10-20', 0)""")
    for _ in range(5):
        for slug in ("psi-a", "psi-b"):
            assert cliente.get("/dashboard/api/noticias", headers=_h(tenants[slug])).status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM noticias_execucoes").fetchone()[0] == 0


def test_consultorios_do_mesmo_segmento_dividem_uma_coleta(cliente, tenants, http, feeds_padrao):
    _gerar_edicao(http, feeds_padrao)
    assert http.quantas(URL_CFP) == 1 and http.quantas(URL_MS) == 1
    a = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json()
    b = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-b"])).json()
    assert a == b
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM noticias_edicoes").fetchone()[0] == 1
    assert len(http.chamadas) == 2                     # e nenhuma por acesso


def test_estado_vazio_honesto_antes_da_primeira_coleta(cliente, tenants):
    d = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json()
    assert d["habilitado"] and d["itens"] == [] and d["situacao"] == "sem_edicao"
    assert d["verificado_em"] is None and d["ficticio"] is False


def test_semana_sem_noticia_relevante_fica_vazia(cliente, tenants, http, feeds_padrao):
    from apoio_noticias import atom, rss2
    http.responder(URL_CFP, rss2([]))
    http.responder(URL_MS, atom([]), tipo="application/atom+xml")
    _gerar_edicao(http, feeds_padrao)
    d = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json()
    assert d["itens"] == [] and d["situacao"] == "sem_recentes" and d["verificado_em"]


def test_no_maximo_cinco_e_tres_visiveis(cliente, tenants, http, feeds_padrao):
    _gerar_edicao(http, feeds_padrao)
    d = cliente.get("/dashboard/api/noticias", headers=_h(tenants["psi-a"])).json()
    assert len(d["itens"]) <= 5 and d["visiveis"] == 3
    for it in d["itens"]:
        assert set(it) == {"titulo", "resumo", "url", "fonte", "publicado_em"}
        assert it["url"].startswith("https://")


# ── Botão "Atualizar": revisão real × leitura do cache ────────────────────────

def test_atualizar_logo_depois_da_verificacao_so_le_o_cache(http, feeds_padrao, tenants):
    _gerar_edicao(http, feeds_padrao)
    http.chamadas.clear()
    r = nj.pedir_revisao(tenants["psi-a"], relogio=Relogio(feeds_padrao + timedelta(minutes=20)),
                         em_segundo_plano=False, transporte=http.transporte)
    assert r["acao"] == "leitura_cache" and r["proxima_revisao_em"]
    assert http.chamadas == []


def test_atualizar_depois_do_intervalo_revisa_e_e_compartilhado(http, feeds_padrao, tenants):
    _gerar_edicao(http, feeds_padrao)
    http.chamadas.clear()
    depois = Relogio(feeds_padrao + timedelta(minutes=61))
    r = nj.pedir_revisao(tenants["psi-a"], relogio=depois, em_segundo_plano=False, transporte=http.transporte)
    assert r == {"acao": "revisao_concluida", "status": "sucesso"}
    assert len(http.chamadas) == 2
    # Outro consultório do segmento, logo em seguida: só cache.
    r2 = nj.pedir_revisao(tenants["psi-b"], relogio=Relogio(depois() + timedelta(minutes=1)),
                          em_segundo_plano=False, transporte=http.transporte)
    assert r2["acao"] == "leitura_cache" and len(http.chamadas) == 2


def test_atualizar_pela_api_em_segundo_plano(cliente, tenants, http, feeds_padrao, monkeypatch):
    with db.get_conn() as conn:   # verificação antiga: o intervalo mínimo já passou
        conn.execute("""INSERT INTO noticias_edicoes VALUES ('psicologia', '[]', 'x',
                        '2026-01-01T12:00:00+00:00', '2026-01-01T12:00:00+00:00', 'diaria:2026-01-01', 0)""")
    monkeypatch.setattr(nj, "_transporte_fixtures", lambda: http.transporte)
    r = cliente.post("/dashboard/api/noticias/revisar", headers=_h(tenants["psi-a"]))
    assert r.json() == {"acao": "revisao_iniciada"}
    for t in threading.enumerate():
        if t.name == "noticias-manual":
            t.join(20)
    assert len(http.chamadas) == 2
    # Repetir o clique em seguida: 429 (limite por consultório).
    assert cliente.post("/dashboard/api/noticias/revisar", headers=_h(tenants["psi-a"])).status_code == 429


def test_atualizar_em_segmento_sem_boletim(tenants):
    assert nj.pedir_revisao(tenants["nutri"]) == {"acao": "indisponivel"}


# ── Quadro narrativo substituído; cards operacionais preservados ──────────────

@pytest.fixture
def modelo(monkeypatch):
    import agent
    import insights_service
    chamadas = []
    monkeypatch.setattr(insights_service, "build_home_insights",
                        lambda t: {"greeting": "Olá", "insights": [{"id": "today", "text": "2 consultas hoje"}]})

    def falso(*a, **k):
        chamadas.append(a)
        return "Parágrafo gerado pelo modelo (dublê)."
    monkeypatch.setattr(agent, "_call_llm", falso)
    return chamadas


def test_psicologia_nao_gera_mais_a_narrativa_nem_chama_o_modelo(cliente, tenants, modelo):
    r = cliente.get("/dashboard/api/insights/narrative", headers=_h(tenants["psi-a"]))
    assert r.json() == {"narrative": ""} and modelo == []


def test_outro_segmento_mantem_a_narrativa_como_era(cliente, tenants, modelo):
    r = cliente.get("/dashboard/api/insights/narrative", headers=_h(tenants["nutri"]))
    assert r.json()["narrative"] == "Parágrafo gerado pelo modelo (dublê)." and len(modelo) == 1


def test_cards_operacionais_continuam_vindo_da_api_de_insights(cliente, tenants, modelo):
    d = cliente.get("/dashboard/api/insights", headers=_h(tenants["psi-a"])).json()
    assert d["insights"] and d["greeting"] == "Olá"


def test_painel_renderiza_com_o_quadro_e_os_blocos_preservados(cliente, tenants):
    t = tenants["psi-a"]
    db.update_tenant("psi-a", status="active")
    html = cliente.get(f"/dashboard/psi-a?token={t['dashboard_token']}").text
    for id_ in ("foco", "foco-lista", "foco-estado", "hi-greeting", "home-insights", "hi-narrative",
                "hi-kpis", "hi-body", "agenda-summary", "agenda-list"):
        assert f'id="{id_}"' in html, id_
    # O quadro do boletim vem depois do resumo operacional (não empurra alertas).
    assert html.index('id="home-insights"') < html.index('id="foco"')


def test_conteudo_externo_so_entra_no_dom_por_textcontent():
    fonte = DASH.read_text(encoding="utf-8")
    bloco = fonte[fonte.index("function _focoRender"): fonte.index("function alternarFocoMais")]
    assert "innerHTML" not in bloco and "insertAdjacentHTML" not in bloco
    assert 'rel = "noopener noreferrer nofollow"' in bloco
    assert "^https:" in bloco                          # só link https vira <a>
    assert "d.ficticio" in bloco                       # conteúdo fictício nunca vira link
