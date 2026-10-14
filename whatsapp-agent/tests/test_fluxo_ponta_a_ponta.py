"""E03 — fluxo completo e suporte operacional.

webhook → evento durável → worker → ação → outbox → estado da oferta,
com provedor simulado. Nenhum `AttributeError`, nenhum envio paralelo oculto.

Inclui também a migração desde o schema de E02 numa cópia fictícia do banco e os
acessos negativos da consulta de suporte.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import database as db
import main
from conftest import (PHONE, SLUG, SLUG_B, agora, consultas, dia_util_distante,
                      passar_o_tempo_do_backoff, pedir, sid)

EVOLUTION = "evolution"


@pytest.fixture
def cliente(tenant, tenant_b):
    return TestClient(main.app)


def _post(cliente, texto, msg_id, slug=SLUG, phone="5511900000001"):
    t = db.ensure_webhook_token(db.get_tenant(slug)["id"])
    return cliente.post(
        f"/webhook/{slug}/evolution?token={t}",
        json={"data": {"key": {"id": msg_id, "remoteJid": f"{phone}@s.whatsapp.net",
                               "fromMe": False},
                       "message": {"conversation": texto}}})


class ProvedorFalso:
    """Provedor simulado. Registra o que recebeu e devolve o desfecho pedido."""

    def __init__(self, desfecho="provider_accepted", erro=""):
        self.enviados: list[tuple] = []
        self.desfecho = desfecho
        self.erro = erro

    def __call__(self, tenant, phone, body):
        self.enviados.append((tenant["slug"], phone, body))
        mid = f"MSG-{len(self.enviados)}"
        return self.desfecho, (mid if self.desfecho.startswith("provider") else ""), self.erro


# ── Fluxo completo ────────────────────────────────────────────────────────────

def test_webhook_worker_acao_outbox_e_oferta(cliente, tenant, modelo):
    """Caminho feliz ponta a ponta, com oferta de horários."""
    import inbox
    import outbox
    alvo = dia_util_distante(9)
    opcoes = [alvo.replace(hour=h) for h in (9, 10, 11)]

    # 1) chega a mensagem → evento persistido, nada processado ainda
    r = _post(cliente, pedir(alvo), "E2E-1")
    assert r.json()["status"] == "queued"
    assert modelo.chamadas == [] and not db.outbox_list()

    # 2) worker processa: o modelo oferece horários
    modelo.responder(action="list_slots",
                     data={"offered_ids": [sid(s) for s in opcoes]},
                     response_text="ignorado pelo servidor")
    assert inbox.drain("w1", limit=5)[0]["state"] == "done"

    ev = [e for e in db.inbound_list(states=("done",)) if e["event_id"] == "E2E-1"][0]
    assert ev["stage"] == "finished" and ev["offer_id"]
    assert db.offer_get(ev["offer_id"])["state"] == "draft", (
        "a oferta não pode valer antes de a mensagem sair"
    )

    # 3) outbox envia e o provedor aceita → a oferta passa a valer
    provedor = ProvedorFalso()
    enviados = outbox.flush("w1", limit=5, sender=provedor)
    assert [e["state"] for e in enviados] == ["provider_accepted"]
    assert len(provedor.enviados) == 1
    corpo = provedor.enviados[0][2]
    assert "1. " in corpo and "2. " in corpo, "a lista canônica não foi enviada"

    oferta = db.offer_get(ev["offer_id"])
    assert oferta["state"] == "active" and oferta["eligible"] is True
    assert oferta["slots"] == opcoes

    # 4) o paciente responde "1" → reserva a opção 1 exibida
    r = _post(cliente, "quero o 1", "E2E-2")
    assert r.json()["status"] == "queued"
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert consultas(tenant, PHONE) == [opcoes[0]]
    assert db.offer_get(ev["offer_id"])["state"] == "consumed"

    # 5) a confirmação sai pelo outbox, não por envio direto
    outbox.flush("w1", limit=5, sender=provedor)
    assert len(provedor.enviados) == 2
    assert "ficou marcada" in provedor.enviados[1][2]


def test_envio_incerto_impede_que_o_numero_reserve(cliente, tenant, modelo):
    """O paciente pode não ter recebido a lista: um "1" não pode reservar."""
    import inbox
    import outbox
    alvo = dia_util_distante(9)
    opcoes = [alvo.replace(hour=9), alvo.replace(hour=10)]
    _post(cliente, pedir(alvo), "E2E-INC")
    modelo.responder(action="list_slots",
                     data={"offered_ids": [sid(s) for s in opcoes]},
                     response_text="x")
    inbox.drain("w1", limit=5)
    outbox.flush("w1", limit=5, sender=ProvedorFalso("uncertain", "timeout"))

    _post(cliente, "quero o 1", "E2E-INC2")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert not consultas(tenant, PHONE), "reservou contra oferta de envio incerto"


def test_nenhum_envio_direto_no_caminho_de_entrada(cliente, tenant, modelo, monkeypatch):
    """Guarda contra envio paralelo oculto: durante o processamento de uma
    mensagem recebida, `whatsapp_service.send_message*` não pode ser chamado."""
    import inbox
    import whatsapp_service as wa
    chamadas = []

    async def _proibido(*a, **k):
        chamadas.append(a)
        return True
    monkeypatch.setattr(wa, "send_message", _proibido)
    monkeypatch.setattr(wa, "send_message_ex", _proibido)

    alvo = dia_util_distante(9)
    _post(cliente, pedir(alvo), "E2E-DIR")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert chamadas == [], f"houve envio direto fora do outbox: {chamadas}"
    assert len(db.outbox_list(states=("pending",))) >= 1


def test_aviso_a_profissional_vai_para_o_outbox(cliente, tenant, modelo):
    """Notificação é efeito do processamento: durável e idempotente, com chave
    por evento."""
    import inbox
    db.update_tenant(SLUG, psychologist_phone="5511933333333")
    tenant = db.get_tenant(SLUG)
    alvo = dia_util_distante(9)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, alvo)

    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "E2E-NOTIF", PHONE,
                               "quero cancelar")
    modelo.responder(intent="cancel", action="cancel",
                     data={"appointment_id": appt}, response_text="Cancelado.")
    inbox.drain("w1", limit=5)

    avisos = [o for o in db.outbox_list() if o["kind"] == "notification"]
    assert avisos, "o aviso de cancelamento não foi enfileirado"
    assert avisos[0]["idempotency_key"].startswith(f"inbound:{rid}:notify:")

    # Reprocessar não gera um segundo aviso.
    db.inbound_force_pending(rid)
    passar_o_tempo_do_backoff()
    modelo.responder(intent="cancel", action="cancel",
                     data={"appointment_id": appt}, response_text="Cancelado.")
    inbox.drain("w1", limit=5)
    assert len([o for o in db.outbox_list() if o["kind"] == "notification"]) == len(avisos)


def test_contatos_independentes_no_fluxo_completo(cliente, tenant, modelo):
    import inbox
    alvo = dia_util_distante(9)
    _post(cliente, pedir(alvo), "E2E-C1", phone="5511900000001")
    _post(cliente, pedir(alvo), "E2E-C2", phone="5511944444444")
    for _ in range(2):
        modelo.responder(action="none", response_text="ok")
    resultados = inbox.drain("w1", limit=5)
    assert len(resultados) == 2 and all(r["state"] == "done" for r in resultados)


def test_evento_de_outro_consultorio_nao_mistura(cliente, tenant, tenant_b, modelo):
    import inbox
    _post(cliente, "oi", "E2E-T", slug=SLUG)
    _post(cliente, "oi", "E2E-T", slug=SLUG_B)
    for _ in range(2):
        modelo.responder(action="none", response_text="ok")
    inbox.drain("w1", limit=5)
    por_tenant = {}
    for e in db.inbound_list(states=("done",)):
        if e["event_id"] == "E2E-T":
            por_tenant[e["tenant_id"]] = e
    assert len(por_tenant) == 2


# ── Suporte operacional ───────────────────────────────────────────────────────

def test_consulta_de_fila_exige_master_key(cliente, tenant):
    assert cliente.get("/admin/fila").status_code == 403
    assert cliente.get("/admin/fila?key=errada").status_code == 403
    r = cliente.get(f"/admin/fila?key={config.MASTER_KEY}")
    assert r.status_code == 200 and "entrada" in r.json()


def test_acoes_de_suporte_exigem_master_key(cliente, tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "SUP-1", PHONE, "oi")
    assert cliente.post(f"/admin/fila/entrada/{rid}/retry",
                        json={"motivo": "x"}).status_code == 403
    oid = db.outbox_enqueue(tenant["id"], PHONE, "oi", "sup-k1")
    assert cliente.post(f"/admin/fila/saida/{oid}/resolver",
                        json={"motivo": "x"}).status_code == 403


def test_fila_nao_expoe_texto_de_mensagem(cliente, tenant):
    db.inbound_record(tenant["id"], EVOLUTION, "SUP-2", PHONE,
                      "conteúdo clínico sensível")
    r = cliente.get(f"/admin/fila?key={config.MASTER_KEY}")
    assert "conteúdo clínico sensível" not in r.text


def test_retry_de_suporte_exige_motivo_e_fica_auditado(cliente, tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "SUP-3", PHONE, "oi")
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET state='failed' WHERE id=?", (rid,))
    assert cliente.post(f"/admin/fila/entrada/{rid}/retry?key={config.MASTER_KEY}",
                        json={"motivo": "  "}).status_code == 400
    r = cliente.post(f"/admin/fila/entrada/{rid}/retry?key={config.MASTER_KEY}",
                     json={"motivo": "paciente reclamou que não respondeu"})
    assert r.status_code == 200 and db.inbound_get(rid)["state"] == "pending"
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='inbox_retry'"
                         ).fetchone()["n"]
    assert n >= 1, "o retry manual não ficou auditado"


def test_resolucao_de_envio_incerto_e_auditada_e_atualiza_a_oferta(cliente, tenant):
    alvo = dia_util_distante(9)
    oferta = db.offer_create(tenant["id"], PHONE, [alvo])["offer_id"]
    oid = db.outbox_enqueue(tenant["id"], PHONE, "lista", "sup-k2", offer_id=oferta)
    tok = db.outbox_claim("w1", lease_sec=60)[0]["attempt_token"]
    db.outbox_mark(oid, tok, "uncertain", last_error="timeout")

    r = cliente.post(f"/admin/fila/saida/{oid}/resolver?key={config.MASTER_KEY}",
                     json={"motivo": "paciente confirmou que não recebeu",
                           "estado": "failed"})
    assert r.status_code == 200
    o = db.outbox_get(oid)
    assert o["state"] == "failed" and "não recebeu" in o["resolution"]
    assert db.offer_get(oferta)["state"] == "cancelled"


def test_retry_de_suporte_nao_reaplica_efeito(cliente, tenant, modelo):
    """Reprocessar depois de resolver não pode criar uma segunda consulta."""
    import inbox
    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "SUP-4", PHONE, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1

    cliente.post(f"/admin/fila/entrada/{rid}/retry?key={config.MASTER_KEY}",
                 json={"motivo": "verificação de suporte"})
    passar_o_tempo_do_backoff()
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1, "o retry de suporte duplicou a consulta"


# ── Inicialização ─────────────────────────────────────────────────────────────

def test_boot_recusa_inbox_ligado_com_outbox_desligado(monkeypatch):
    """Essa combinação acumularia respostas que ninguém envia — falha silenciosa
    para o paciente. O boot tem de recusar."""
    import asyncio
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", False)

    async def _subir():
        async with main.lifespan(main.app):
            pass
    with pytest.raises(RuntimeError, match="nunca enviadas"):
        asyncio.run(_subir())


def test_workers_nao_duplicam_threads(monkeypatch):
    import inbox
    import outbox
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", True)
    try:
        inbox.start_worker(); inbox.start_worker()
        outbox.start_worker(); outbox.start_worker()
        import threading
        nomes = [t.name for t in threading.enumerate()]
        assert nomes.count("inbox-worker") == 1
        assert nomes.count("outbox-worker") == 1
    finally:
        inbox.stop_worker()
        outbox.stop_worker()


@pytest.mark.parametrize("nome,valor,esperado", [
    ("INBOX_LEASE_SEC", "5", 30),        # abaixo do mínimo → ajustado
    ("INBOX_LEASE_SEC", "99999", 1800),  # acima do máximo → ajustado
    ("INBOX_LEASE_SEC", "abc", 180),     # inválido → padrão
    ("INBOX_LEASE_SEC", "240", 240),     # dentro da faixa
])
def test_configuracao_valida_limites(monkeypatch, nome, valor, esperado):
    monkeypatch.setenv(nome, valor)
    assert config._int_env(nome, 180, 30, 1800) == esperado


# ── Migração desde o schema de E02 ────────────────────────────────────────────

def test_migracao_desde_o_schema_de_e02(tmp_path):
    """Cria um banco com o schema de E02 (incluindo `slot_offers` com dados
    fictícios) e aplica `init_db` do código atual, num processo separado.

    Verifica que as tabelas novas aparecem e que **nada** da E02 é removido: a
    `slot_offers` continua existindo, com as linhas intactas. Não se remove
    tabela supondo que não há dado real.
    """
    destino = tmp_path / "dados"
    destino.mkdir()
    antigo = destino / "consultorio.db"
    conn = sqlite3.connect(antigo)
    conn.executescript("""
        CREATE TABLE tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE,
                              name TEXT, psychologist_name TEXT,
                              working_hours_start INTEGER, working_hours_end INTEGER,
                              session_minutes INTEGER, whatsapp_provider TEXT);
        CREATE TABLE slot_offers (tenant_id INTEGER NOT NULL, phone TEXT NOT NULL,
                                  slots TEXT NOT NULL, created_at TEXT NOT NULL,
                                  expires_at TEXT NOT NULL,
                                  UNIQUE(tenant_id, phone));
        INSERT INTO tenants (slug, name, psychologist_name, working_hours_start,
                             working_hours_end, session_minutes, whatsapp_provider)
             VALUES ('antigo', 'Consultório Antigo', 'Dra. Fictícia', 8, 19, 50, 'mock');
        INSERT INTO slot_offers VALUES
             (1, '5511900000001', '["2026-11-03T09:00"]', '2026-11-01T10:00',
              '2026-11-01T13:00');
    """)
    conn.commit(); conn.close()

    backend = str(Path(__file__).resolve().parents[1] / "backend")
    env = dict(os.environ, DATA_DIR=str(destino), ADMIN_PASSWORD="x", MASTER_KEY="x",
               GROQ_API_KEY="", ANTHROPIC_API_KEY="")
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import database as d; d.init_db(); print('ok')", backend],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"init_db falhou: {r.stderr[-800:]}"

    conn = sqlite3.connect(antigo)
    tabelas = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"inbound_events", "outbox", "offers", "counters",
            "applied_effects"} <= tabelas, "tabelas de E03 não foram criadas"
    assert "slot_offers" in tabelas, "tabela de E02 foi removida"
    assert conn.execute("SELECT COUNT(*) FROM slot_offers").fetchone()[0] == 1, (
        "linhas de E02 foram perdidas na migração"
    )
    # O tenant antigo continua legível (as migrações só acrescentam colunas).
    assert conn.execute("SELECT slug FROM tenants").fetchone()[0] == "antigo"
    conn.close()
