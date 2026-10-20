"""Parada de emergência do R2 (RELEASE-R1-LANDING.md §8, bloqueador do §9).

O procedimento é desligar os workers: INBOX_WORKER_ENABLED=0 e
OUTBOX_WORKER_ENABLED=0. Antes desta correção, o empurrão do webhook
(`_drenar_entrada`) chamava `inbox.drain` e `outbox.flush` direto, sem olhar
essas variáveis: com tudo "desligado", cada mensagem recebida continuava sendo
processada (modelo, agenda) e respondida. Estes testes garantem que desligar
para de verdade — e que ligado continua funcionando.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import config
import database as db
import main
import outbox
from conftest import PHONE, SLUG, dia_util_distante, sid


def _payload(texto, msg_id):
    return {"data": {"key": {"id": msg_id, "remoteJid": f"{PHONE}@s.whatsapp.net", "fromMe": False},
                     "message": {"conversation": texto}}}


@pytest.fixture
def envios(monkeypatch):
    chamadas = []
    original = outbox.flush

    def registra(*a, **k):
        chamadas.append(a)
        return original(*a, **k)
    monkeypatch.setattr(outbox, "flush", registra)
    return chamadas


def _estado(rid):
    return db.inbound_get(rid)["state"]


def test_workers_desligados_o_empurrao_nao_processa_nem_envia(tenant, modelo, envios, monkeypatch):
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", False)
    rid, _ = db.inbound_record(tenant["id"], "evolution", "EMG-1", PHONE, "quero agendar")
    asyncio.run(main._drenar_entrada())
    assert _estado(rid) == "pending", "a mensagem foi processada com os workers desligados"
    assert modelo.chamadas == [], "o modelo foi chamado com os workers desligados"
    assert envios == [], "a saída foi despachada com os workers desligados"


def test_pelo_webhook_com_empurrao_ligado_e_workers_desligados_so_grava(tenant, modelo, envios, monkeypatch):
    """O caminho real do incidente: webhook chega, empurrão ligado (padrão de
    produção), workers desligados pelo operador."""
    monkeypatch.setattr(config, "INBOX_NUDGE_ON_WEBHOOK", True)
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", False)
    tok = db.ensure_webhook_token(db.get_tenant(SLUG)["id"])
    r = TestClient(main.app).post(f"/webhook/{SLUG}/evolution?token={tok}",
                                  json=_payload("quero remarcar", "EMG-2"))
    assert r.status_code == 200 and r.json()["status"] == "queued"   # recebimento segue gravando
    (ev,) = [e for e in db.inbound_list(states=("pending", "processing", "done", "failed"))
             if e["event_id"] == "EMG-2"]
    assert ev["state"] == "pending"
    assert modelo.chamadas == [] and envios == []


def test_workers_ligados_o_empurrao_continua_processando(tenant, modelo, envios, monkeypatch):
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", True)
    alvo = dia_util_distante(9)
    modelo.responder(action="create", data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    rid, _ = db.inbound_record(tenant["id"], "evolution", "EMG-3", PHONE, f"quero agendar dia {alvo:%d/%m}")
    asyncio.run(main._drenar_entrada())
    assert _estado(rid) == "done"
    assert len(envios) == 1


def test_so_a_saida_desligada_processa_mas_nao_envia(tenant, modelo, envios, monkeypatch):
    monkeypatch.setattr(config, "INBOX_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOX_WORKER_ENABLED", False)
    alvo = dia_util_distante(9)
    modelo.responder(action="create", data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    rid, _ = db.inbound_record(tenant["id"], "evolution", "EMG-4", PHONE, f"quero agendar dia {alvo:%d/%m}")
    asyncio.run(main._drenar_entrada())
    assert _estado(rid) == "done"
    assert envios == []
    assert [o for o in db.outbox_list() if o["event_row_id"] == rid][0]["state"] == "pending"
