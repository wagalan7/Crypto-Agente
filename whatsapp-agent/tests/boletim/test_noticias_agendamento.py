"""E08B — agendamento das 09:00 (America/Sao_Paulo), lease, recuperação,
falha com backoff e cache. Relógio fixo e HTTP falso: nada aqui acessa a
internet nem depende do horário real."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import timedelta

import httpx
import pytest

import database as db
import noticias_job as nj
from .apoio_noticias import HttpFalso, Relogio, URL_CFP, URL_MS, atom, em_brasilia, link_cfp, link_ms, rss2


def _tick(http, relogio, dono="worker-1"):
    return nj.tick(dono, transporte=http.transporte, relogio=relogio)


def _execucoes():
    with db.get_conn() as conn:
        return {r["chave"]: dict(r) for r in conn.execute("SELECT * FROM noticias_execucoes").fetchall()}


def _edicao():
    with db.get_conn() as conn:
        r = conn.execute("SELECT * FROM noticias_edicoes WHERE segmento='psicologia'").fetchone()
    return dict(r) if r else None


def _tenant_psi():
    return {"segment": "psicologia"}


@pytest.fixture
def com_edicao_de_ontem(http, feeds_padrao):
    """Estado de um dia normal: o boletim já existe, gerado na véspera."""
    _tick(http, Relogio(em_brasilia(2026, 10, 19, 10)))
    http.chamadas.clear()
    return feeds_padrao


# ── Horário ───────────────────────────────────────────────────────────────────

def test_antes_das_9h_nao_roda(http, com_edicao_de_ontem):
    assert _tick(http, Relogio(em_brasilia(2026, 10, 20, 8, 59))) == []
    assert http.chamadas == []


def test_as_9h_roda_a_diaria_uma_vez(http, com_edicao_de_ontem):
    rel = Relogio(em_brasilia(2026, 10, 20, 9, 0))
    (r,) = _tick(http, rel)
    assert r["chave"] == "diaria:2026-10-20" and r["status"] == "sucesso" and r["atraso_s"] == 0
    assert http.quantas(URL_CFP) == 1 and http.quantas(URL_MS) == 1
    rel.avancar(minutes=1)
    assert _tick(http, rel) == []                        # na volta seguinte, nada
    assert len(http.chamadas) == 2


def test_fim_de_semana_tambem_roda(http, com_edicao_de_ontem):
    sabado = em_brasilia(2026, 10, 24, 9, 0)
    assert sabado.astimezone(nj.TZ).weekday() == 5
    (r,) = _tick(http, Relogio(sabado))
    assert r["chave"] == "diaria:2026-10-24"


def test_fuso_do_servidor_nao_altera_o_horario(http, com_edicao_de_ontem, monkeypatch):
    """Servidor em outro fuso (Tóquio): continua 09:00 de Brasília (12:00 UTC)."""
    if not hasattr(time, "tzset"):
        pytest.skip("sem tzset nesta plataforma")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        assert time.strftime("%Z") in ("JST", "+09")
        assert _tick(http, Relogio(em_brasilia(2026, 10, 20, 8, 59))) == []
        (r,) = _tick(http, Relogio(em_brasilia(2026, 10, 20, 9, 0)))
        assert r["chave"] == "diaria:2026-10-20"
        assert _execucoes()["diaria:2026-10-20"]["agendada_para"] == "2026-10-20T12:00:00+00:00"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# ── Recuperação ───────────────────────────────────────────────────────────────

def test_app_fora_as_9h_recupera_ao_voltar_e_registra_o_atraso(http, com_edicao_de_ontem):
    (r,) = _tick(http, Relogio(em_brasilia(2026, 10, 20, 11, 30)))
    assert r["chave"] == "diaria:2026-10-20"
    assert r["atraso_s"] == 2 * 3600 + 30 * 60          # 2h30 de atraso, registrado
    ex = _execucoes()["diaria:2026-10-20"]
    assert ex["agendada_para"] == "2026-10-20T12:00:00+00:00"
    assert ex["concluida_em"] == "2026-10-20T14:30:00+00:00"
    # O quadro mostra o horário real da verificação, não "09:00".
    assert nj.ler_boletim(_tenant_psi(), em_brasilia(2026, 10, 20, 11, 31))["verificado_em"] == "2026-10-20T14:30:00+00:00"


def test_app_fora_o_dia_inteiro_recupera_o_dia_perdido_e_depois_segue(http, com_edicao_de_ontem):
    (r,) = _tick(http, Relogio(em_brasilia(2026, 10, 21, 8, 30)))   # voltou antes das 9h do dia seguinte
    assert r["chave"] == "diaria:2026-10-20" and r["atraso_s"] == 23 * 3600 + 30 * 60
    (r2,) = _tick(http, Relogio(em_brasilia(2026, 10, 21, 9, 0)))
    assert r2["chave"] == "diaria:2026-10-21" and r2["atraso_s"] == 0


def test_primeiro_preenchimento_roda_uma_vez_e_cobre_o_horario(http, feeds_padrao):
    rel = Relogio(em_brasilia(2026, 10, 20, 8, 0))          # primeiro deploy, antes das 9h
    (r,) = _tick(http, rel)
    assert r["chave"] == "inicial"
    rel.avancar(minutes=30)
    assert _tick(http, rel) == []                          # não repete, nem recupera a de ontem
    assert len(http.chamadas) == 2
    rel.avancar(minutes=30)                                # 09:00
    (r2,) = _tick(http, rel)
    assert r2["chave"] == "diaria:2026-10-20"


# ── Concorrência e lease ──────────────────────────────────────────────────────

def test_dois_workers_ao_mesmo_tempo_buscam_uma_vez(com_edicao_de_ontem):
    http = HttpFalso()
    agora = em_brasilia(2026, 10, 20, 9, 0)
    http.responder(URL_CFP, rss2([{"titulo": "Resolução fictícia", "link": link_cfp(1), "data": agora}]))
    http.responder(URL_MS, atom([]), tipo="application/atom+xml")
    largada = threading.Barrier(2)
    resultados = {}

    def worker(nome):
        largada.wait()
        resultados[nome] = nj.tick(nome, transporte=http.transporte, relogio=Relogio(agora))

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("worker-1", "worker-2")]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    executaram = [n for n, r in resultados.items() if r]
    assert len(executaram) == 1
    assert http.quantas(URL_CFP) == 1 and http.quantas(URL_MS) == 1
    assert _execucoes()["diaria:2026-10-20"]["tentativas"] == 1


def test_lease_de_execucao_interrompida_e_retomado_depois_de_vencer(http, com_edicao_de_ontem):
    agora = em_brasilia(2026, 10, 20, 9, 0)
    # Um processo tomou o lease e morreu antes de concluir (restart no meio).
    assert nj._reivindicar("psicologia", "diaria:2026-10-20", nj.agendada_para(agora.astimezone(nj.TZ).date()),
                           agora, "processo-morto") == 1
    rel = Relogio(agora + timedelta(minutes=5))
    assert _tick(http, rel) == []                         # lease ainda vale: ninguém mexe
    assert http.chamadas == []
    rel.avancar(minutes=6)                                 # lease de 10 min venceu
    (r,) = _tick(http, rel)
    assert r["status"] == "sucesso" and r["tentativa"] == 2


def test_resultado_de_quem_perdeu_o_lease_e_descartado(http, com_edicao_de_ontem):
    agora = em_brasilia(2026, 10, 20, 9, 0)
    chave = "diaria:2026-10-20"
    assert nj._reivindicar("psicologia", chave, agora, agora, "lento") == 1
    with db.get_conn() as conn:                            # outro processo assumiu (lease vencido)
        conn.execute("UPDATE noticias_execucoes SET dono_lease='rapido' WHERE chave=?", (chave,))
    antes = _edicao()
    r = nj.executar("psicologia", chave, 1, "lento", transporte=http.transporte, relogio=Relogio(agora))
    assert r["status"] == "lease_perdido"
    assert _edicao() == antes


def test_nenhuma_transacao_aberta_durante_o_http(com_edicao_de_ontem):
    """Enquanto a fonte "responde", outro processo consegue escrever no banco."""
    escritas = []

    class Lento(HttpFalso):
        def _servir(self, req):
            c = sqlite3.connect(db.DB_PATH, timeout=0.5)
            try:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE noticias_fontes_estado SET itens_no_feed = itens_no_feed")
                c.commit()
                escritas.append("ok")
            finally:
                c.close()
            return super()._servir(req)

    http = Lento()
    agora = em_brasilia(2026, 10, 20, 9, 0)
    http.responder(URL_CFP, rss2([]))
    http.responder(URL_MS, atom([]), tipo="application/atom+xml")
    _tick(http, Relogio(agora))
    assert escritas == ["ok", "ok"]


# ── Falhas ────────────────────────────────────────────────────────────────────

def test_falha_total_preserva_edicao_e_retenta_com_backoff_ate_esgotar(com_edicao_de_ontem):
    http = HttpFalso()
    http.responder(URL_CFP, "", status=503)
    http.falhar(URL_MS, httpx.ConnectTimeout("fora do ar"))
    edicao_anterior = _edicao()
    rel = Relogio(em_brasilia(2026, 10, 20, 9, 0))
    esperas = []
    for tentativa in range(1, nj.TENTATIVAS_MAX + 1):
        (r,) = _tick(http, rel)
        assert r["tentativa"] == tentativa
        ex = _execucoes()["diaria:2026-10-20"]
        if tentativa < nj.TENTATIVAS_MAX:
            assert r["status"] == "falha"
            prox = nj._de_iso(ex["proxima_tentativa"])
            esperas.append(prox - rel())
            rel.instante = prox - timedelta(minutes=1)
            assert _tick(http, rel) == []                  # antes do backoff, nada
            rel.instante = prox
        else:
            assert r["status"] == "esgotada" and ex["proxima_tentativa"] is None
    assert esperas == list(nj.BACKOFF)
    rel.avancar(hours=3)
    assert _tick(http, rel) == []                          # esgotada: para até o dia seguinte
    assert _edicao() == edicao_anterior                    # a edição anterior ficou intacta
    b = nj.ler_boletim(_tenant_psi(), rel())
    assert b["aviso"]["tipo"] == "falha" and b["itens"]    # aviso + conteúdo anterior
    assert nj.estado_operacional()["pendencias"][0]["status"] == "esgotada"


def test_falha_de_uma_fonte_nao_apaga_os_itens_bons_da_outra(http, feeds_padrao):
    rel = Relogio(em_brasilia(2026, 10, 20, 10, 0))
    _tick(http, rel)
    titulos = [i["titulo"] for i in nj.ler_boletim(_tenant_psi(), rel())["itens"]]
    assert any("CAPS" in t for t in titulos)               # item do Ministério na edição
    http.falhar(URL_MS, httpx.ReadTimeout("lento"))
    rel.instante = em_brasilia(2026, 10, 21, 9, 0)
    (r,) = _tick(http, rel)
    assert r["status"] == "parcial"
    b = nj.ler_boletim(_tenant_psi(), rel())
    assert any("CAPS" in i["titulo"] for i in b["itens"])  # continua: coletado ontem, ainda recente
    assert b["aviso"]["tipo"] == "parcial" and "Ministério da Saúde" in b["aviso"]["texto"]


# ── Cache ─────────────────────────────────────────────────────────────────────

def test_sem_novidade_atualiza_a_verificacao_mas_nao_o_conteudo(http, feeds_padrao):
    _tick(http, Relogio(em_brasilia(2026, 10, 20, 10, 0)))
    e1 = _edicao()
    _tick(http, Relogio(em_brasilia(2026, 10, 21, 9, 0)))
    e2 = _edicao()
    assert e2["assinatura"] == e1["assinatura"]
    assert e2["conteudo_atualizado_em"] == e1["conteudo_atualizado_em"]
    assert e2["verificado_em"] != e1["verificado_em"]
    b = nj.ler_boletim(_tenant_psi(), em_brasilia(2026, 10, 21, 9, 1))
    assert b["conteudo_atualizado_em"] != b["verificado_em"]   # o painel mostra os dois


def test_com_novidade_atualiza_os_dois(http, feeds_padrao):
    _tick(http, Relogio(em_brasilia(2026, 10, 20, 10, 0)))
    http.responder(URL_CFP, rss2([{"titulo": "Nova resolução fictícia sobre supervisão", "link": link_cfp(99),
                                   "data": em_brasilia(2026, 10, 21, 7, 0)}]))
    _tick(http, Relogio(em_brasilia(2026, 10, 21, 9, 0)))
    e = _edicao()
    assert e["conteudo_atualizado_em"] == e["verificado_em"] == "2026-10-21T12:00:00+00:00"
    assert "supervisão" in e["itens_json"]


def test_cache_vencido_nao_apresenta_noticia_antiga_como_recente(http, feeds_padrao):
    _tick(http, Relogio(em_brasilia(2026, 10, 20, 10, 0)))
    # Oito dias depois, sem nenhuma verificação nova (fontes fora do ar):
    b = nj.ler_boletim(_tenant_psi(), em_brasilia(2026, 10, 28, 12, 0))
    assert b["itens"] == [] and b["situacao"] == "sem_recentes"
    assert b["aviso"]["tipo"] == "desatualizado"


def test_retencao_remove_itens_muito_antigos(http, feeds_padrao):
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO noticias_itens (chave, fonte_id, url, titulo, publicado_em, primeira_coleta, ultima_coleta)
                        VALUES ('https://site.cfp.org.br/velha', 'cfp', 'https://site.cfp.org.br/velha/', 'Velha fictícia',
                                '2026-07-01T12:00:00+00:00', '2026-07-01T12:00:00+00:00', '2026-07-01T12:00:00+00:00')""")
    _tick(http, Relogio(em_brasilia(2026, 10, 20, 10, 0)))
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM noticias_itens WHERE chave LIKE '%velha'").fetchone()[0] == 0


def test_modulo_desligado_nao_roda_nada(http, feeds_padrao, monkeypatch):
    monkeypatch.setenv("NOTICIAS_HABILITADO", "0")
    assert _tick(http, Relogio(em_brasilia(2026, 10, 20, 10, 0))) == []
    assert http.chamadas == []


def test_modo_previa_recusa_banco_de_producao(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTICIAS_FIXTURES", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", "/data/consultorio.db")
    assert nj._dir_fixtures() is None
    assert not any(f.ficticia for f in nj.fontes_do_segmento("psicologia"))
