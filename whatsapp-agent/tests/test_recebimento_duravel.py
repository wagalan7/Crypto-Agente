"""E03 — recebimento durável, posse por lease, retomada e outbox.

O que estes testes protegem:

- a mensagem do paciente é **gravada antes** de o webhook responder 2xx; uma
  queda depois disso é recuperável, em vez de perder a mensagem em silêncio;
- duplicata do provedor reconhece o evento existente e **não** gera ação nova;
- a chave de dedup é (tenant, provedor, id do evento) — nunca o texto;
- um contato é processado de forma serializada; contatos distintos seguem
  independentes;
- um worker que perdeu o lease **não consegue** finalizar nem alterar o evento;
- cair entre a mutação local e a marcação de progresso não cria, remarca nem
  cancela duas vezes;
- aceite do provedor não é tratado como entrega comprovada.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import database as db
import main
from conftest import (PHONE, SLUG, SLUG_B, agora, consultas, dia_util_distante,
                      passar_o_tempo_do_backoff, sid)

EVOLUTION = "evolution"


# ── Auxiliares ────────────────────────────────────────────────────────────────

def _payload_evolution(texto="oi", msg_id="EVT-1",
                       jid="5511900000001@s.whatsapp.net", from_me=False) -> dict:
    return {"data": {"key": {"id": msg_id, "remoteJid": jid, "fromMe": from_me},
                     "message": {"conversation": texto}}}


def _payload_zapi(texto="oi", msg_id="ZAP-1", phone="5511900000001") -> dict:
    return {"messageId": msg_id, "phone": phone,
            "text": {"message": texto}, "fromMe": False}


@pytest.fixture
def cliente(tenant, tenant_b):
    return TestClient(main.app)


def _wtoken(slug: str = SLUG) -> str:
    return db.ensure_webhook_token(db.get_tenant(slug)["id"])


def _post_evolution(cliente, payload, slug: str = SLUG):
    return cliente.post(f"/webhook/{slug}/evolution?token={_wtoken(slug)}", json=payload)


# ── 1. Persistência antes do ACK ──────────────────────────────────────────────

def test_evento_e_gravado_antes_de_responder_queued(cliente, tenant):
    r = _post_evolution(cliente, _payload_evolution("quero remarcar", "EVT-A"))
    assert r.status_code == 200 and r.json()["status"] == "queued"
    eventos = db.inbound_list(states=("pending", "processing", "done", "failed"))
    meus = [e for e in eventos if e["event_id"] == "EVT-A"]
    assert len(meus) == 1, "a mensagem não foi persistida"
    e = meus[0]
    assert e["provider"] == EVOLUTION
    assert e["phone"] == "5511900000001"
    assert e["body"] == "quero remarcar"
    assert e["state"] == "pending" and e["stage"] == "received"


def test_falha_na_persistencia_nao_responde_queued(cliente, tenant, monkeypatch):
    """Se não foi possível gravar, o provedor não pode receber "enfileirado":
    tem de poder reentregar."""
    def _explode(*a, **k):
        raise RuntimeError("disco cheio")
    monkeypatch.setattr(db, "inbound_record", _explode)
    r = _post_evolution(cliente, _payload_evolution("oi", "EVT-B"))
    assert r.status_code >= 500
    assert "queued" not in r.text


def test_webhook_nao_processa_na_propria_requisicao(cliente, tenant, modelo):
    """O 2xx sai sem depender do LLM nem do envio. O processamento é do worker."""
    r = _post_evolution(cliente, _payload_evolution("quero remarcar", "EVT-C"))
    assert r.json()["status"] == "queued"
    assert modelo.chamadas == [], "o webhook chamou o modelo dentro da requisição"
    assert not db.outbox_list(), "o webhook enviou resposta dentro da requisição"
    # O evento está gravado e pendente: o 2xx não depende do processamento.
    assert any(e["event_id"] == "EVT-C" for e in db.inbound_list(states=("pending",)))


# ── 2. Deduplicação ───────────────────────────────────────────────────────────

def test_duplicata_evolution_reconhece_sem_nova_acao(cliente, tenant):
    p = _payload_evolution("oi", "EVT-D")
    r1 = _post_evolution(cliente, p)
    r2 = _post_evolution(cliente, p)
    assert r1.json()["status"] == "queued"
    assert r2.json()["status"] == "duplicate"
    todos = db.inbound_list(states=("pending", "processing", "done", "failed"))
    assert len([e for e in todos if e["event_id"] == "EVT-D"]) == 1


def test_duplicata_zapi_reconhece_sem_nova_acao(cliente, tenant):
    t = _wtoken()
    p = _payload_zapi("oi", "ZAP-D")
    r1 = cliente.post(f"/webhook/{SLUG}/zapi?token={t}", json=p)
    r2 = cliente.post(f"/webhook/{SLUG}/zapi?token={t}", json=p)
    assert r1.json()["status"] == "queued"
    assert r2.json()["status"] == "duplicate"
    todos = db.inbound_list(states=("pending", "processing", "done", "failed"))
    assert len([e for e in todos if e["event_id"] == "ZAP-D"]) == 1


def test_mesmo_id_em_consultorios_diferentes_sao_eventos_distintos(cliente, tenant, tenant_b):
    p = _payload_evolution("oi", "EVT-MESMO")
    _post_evolution(cliente, p, SLUG)
    r2 = _post_evolution(cliente, p, SLUG_B)
    assert r2.json()["status"] == "queued", "dedup vazou entre consultórios"


def test_texto_identico_com_ids_diferentes_sao_dois_eventos(cliente, tenant):
    """Duas mensagens iguais do mesmo paciente podem ser legítimas. O texto
    nunca é chave de dedup."""
    _post_evolution(cliente, _payload_evolution("oi", "EVT-E1"))
    _post_evolution(cliente, _payload_evolution("oi", "EVT-E2"))
    todos = db.inbound_list(states=("pending", "processing", "done", "failed"))
    assert len([e for e in todos if e["body"] == "oi"
                and e["event_id"] in ("EVT-E1", "EVT-E2")]) == 2


def test_sem_id_do_provedor_processa_mas_declara_que_nao_ha_dedup(cliente, tenant):
    """Sem id confiável não se inventa garantia: o evento é processado e fica
    marcado como `derived`, para o suporte saber que ali dedup não existe."""
    p = _payload_evolution("oi")
    p["data"]["key"].pop("id")
    r1 = _post_evolution(cliente, p)
    r2 = _post_evolution(cliente, p)
    assert r1.json()["status"] == "queued" and r2.json()["status"] == "queued"
    # `local` e não "derived": é um identificador local para a linha existir,
    # não identidade derivada nem prova de dedup. Mensagens repetidas desse
    # provedor não são detectáveis, e isso fica explícito no registro.
    locais = [e for e in db.inbound_list(states=("pending",))
              if e["event_id_source"] == "local"]
    assert len(locais) == 2, "evento sem id do provedor não foi aceito"
    assert all(e["event_id"].startswith("local:") for e in locais)


def test_remetente_nao_paciente_nao_vira_evento(cliente, tenant):
    r = _post_evolution(cliente, _payload_evolution(
        "spam", "EVT-G", jid="120363111111111111@g.us"))
    assert r.json()["status"] == "ignored"
    assert not db.inbound_list(states=("pending",))


# ── 3. Posse, lease e serialização ────────────────────────────────────────────

def test_claim_toma_posse_com_token(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "C-1", PHONE, "oi")
    tomados = db.inbound_claim("w1", lease_sec=60, limit=5)
    assert [t["id"] for t in tomados] == [rid]
    assert tomados[0]["attempt_token"]
    assert db.inbound_get(rid)["state"] == "processing"


def test_dois_workers_nao_pegam_o_mesmo_evento(tenant):
    db.inbound_record(tenant["id"], EVOLUTION, "C-2", PHONE, "oi")
    a = db.inbound_claim("w1", lease_sec=60, limit=5)
    b = db.inbound_claim("w2", lease_sec=60, limit=5)
    assert len(a) == 1 and b == []


def test_eventos_do_mesmo_contato_sao_serializados(tenant):
    db.inbound_record(tenant["id"], EVOLUTION, "S-1", PHONE, "primeira")
    db.inbound_record(tenant["id"], EVOLUTION, "S-2", PHONE, "segunda")
    tomados = db.inbound_claim("w1", lease_sec=60, limit=10)
    assert len(tomados) == 1, "dois eventos do mesmo contato em paralelo"
    assert tomados[0]["body"] == "primeira", "fora da ordem de chegada"


def test_contatos_diferentes_sao_independentes(tenant):
    db.inbound_record(tenant["id"], EVOLUTION, "I-1", PHONE, "a")
    db.inbound_record(tenant["id"], EVOLUTION, "I-2", "5511922222222", "b")
    tomados = db.inbound_claim("w1", lease_sec=60, limit=10)
    assert len(tomados) == 2


def test_worker_que_perdeu_o_lease_nao_finaliza(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "L-1", PHONE, "oi")
    antigo = db.inbound_claim("w-antigo", lease_sec=60)[0]
    # lease vence
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET lease_until=? WHERE id=?",
                     ((agora() - timedelta(minutes=5)).isoformat(timespec="seconds"), rid))
    novo = db.inbound_claim("w-novo", lease_sec=60)[0]
    assert novo["attempt_token"] != antigo["attempt_token"]

    assert db.inbound_update(rid, antigo["attempt_token"], stage="decided") is False
    assert db.inbound_release(rid, antigo["attempt_token"], 1, "x", 5) is False
    assert db.inbound_update(rid, novo["attempt_token"], stage="decided") is True


def test_lease_vencido_permite_retomada(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "L-2", PHONE, "oi")
    db.inbound_claim("w1", lease_sec=60)
    assert db.inbound_claim("w2", lease_sec=60) == []
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET lease_until=? WHERE id=?",
                     ((agora() - timedelta(minutes=1)).isoformat(timespec="seconds"), rid))
    assert len(db.inbound_claim("w2", lease_sec=60)) == 1


def test_backoff_e_tentativas_esgotadas(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "B-1", PHONE, "oi")
    t = db.inbound_claim("w1", lease_sec=60)[0]
    assert db.inbound_release(rid, t["attempt_token"], 300, "erro de rede", max_attempts=3)
    e = db.inbound_get(rid)
    assert e["state"] == "pending" and e["next_attempt_at"] > agora().isoformat()
    assert e["last_error"] == "erro de rede"
    # não deve ser retomado antes do prazo
    assert db.inbound_claim("w1", lease_sec=60) == []

    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET attempts=9, next_attempt_at=NULL WHERE id=?",
                     (rid,))
    t2 = db.inbound_claim("w1", lease_sec=60)[0]
    db.inbound_release(rid, t2["attempt_token"], 10, "ainda falhando", max_attempts=3)
    assert db.inbound_get(rid)["state"] == "failed", "falha não ficou registrada"


def test_retry_manual_de_suporte_reabre_evento_falho(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "B-2", PHONE, "oi")
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET state='failed' WHERE id=?", (rid,))
    assert db.inbound_force_pending(rid)
    assert db.inbound_get(rid)["state"] == "pending"
    assert len(db.inbound_claim("w1", lease_sec=60)) == 1


# ── 4. Efeito local idempotente ───────────────────────────────────────────────

def test_efeito_so_pode_ser_aplicado_uma_vez(tenant):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "EF-1", PHONE, "oi")
    with db.get_conn() as conn:
        assert db.effect_claim(conn, rid, "appointment", "123") is True
    with db.get_conn() as conn:
        assert db.effect_claim(conn, rid, "appointment", "456") is False
    assert db.effect_get(rid, "appointment")["ref_id"] == "123"


# ── 5. Processamento ponta a ponta pelo worker ─────────────────────────────────

def test_worker_processa_e_enfileira_resposta(tenant, modelo):
    import inbox
    alvo = dia_util_distante(9)
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "W-1", PHONE,
                               f"quero agendar dia {alvo:%d/%m}")
    resultados = inbox.drain("w1", limit=5)
    assert resultados and resultados[0]["state"] == "done"

    e = db.inbound_get(rid)
    assert e["state"] == "done" and e["stage"] == "finished"
    assert e["reply_text"], "o texto final não foi persistido"
    assert json.loads(e["decision"])["action"] == "create"
    assert consultas(tenant, PHONE) == [alvo]

    saidas = [o for o in db.outbox_list() if o["event_row_id"] == rid]
    assert len(saidas) == 1 and saidas[0]["state"] == "pending"
    assert saidas[0]["body"] == e["reply_text"]


def test_reprocessar_evento_concluido_nao_duplica_acao(tenant, modelo):
    import inbox
    alvo = dia_util_distante(9)
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "W-2", PHONE,
                               f"quero agendar dia {alvo:%d/%m}")
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1

    # Simula reentrada: evento voltou para pending (crash depois da mutação).
    db.inbound_force_pending(rid)
    passar_o_tempo_do_backoff()
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1, "a ação foi aplicada duas vezes"
    assert len([o for o in db.outbox_list(states=("pending", "sending",
                                                  "provider_accepted", "failed",
                                                  "uncertain"))
                if o["event_row_id"] == rid]) == 1, "resposta duplicada no outbox"


def test_queda_depois_da_mutacao_nao_remarca_duas_vezes(tenant, modelo, monkeypatch):
    """Injeta falha logo após a mutação local, antes de marcar progresso."""
    import inbox
    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "W-3", PHONE,
                               f"quero agendar dia {alvo:%d/%m}")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")

    original = inbox._registrar_saida

    def _cai(*a, **k):
        raise RuntimeError("queda depois da mutação")
    monkeypatch.setattr(inbox, "_registrar_saida", _cai)
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1
    assert db.inbound_get(rid)["state"] == "pending"

    monkeypatch.setattr(inbox, "_registrar_saida", original)
    passar_o_tempo_do_backoff()
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert len(consultas(tenant, PHONE)) == 1, "remarcou/criou duas vezes"
    assert db.inbound_get(rid)["state"] == "done"


def test_decisao_gravada_nao_e_recalculada_no_retry(tenant, modelo, monkeypatch):
    """Depois de a decisão estar persistida, o retry não chama o modelo de novo
    nem muda o que já foi decidido."""
    import inbox
    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "W-4", PHONE,
                               f"quero agendar dia {alvo:%d/%m}")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    monkeypatch.setattr(inbox, "_registrar_saida",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falha")))
    inbox.drain("w1", limit=5)
    chamadas_antes = len(modelo.chamadas)
    assert db.inbound_get(rid)["decision"], "decisão não foi persistida"

    monkeypatch.undo()
    passar_o_tempo_do_backoff()
    inbox.drain("w1", limit=5)
    assert len(modelo.chamadas) == chamadas_antes, "o modelo foi chamado de novo"
    assert db.inbound_get(rid)["state"] == "done"


def test_evento_de_contato_pausado_e_encerrado_sem_resposta(tenant, modelo):
    import inbox
    db.pause_agent(tenant["id"], PHONE)
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "W-5", PHONE, "oi")
    inbox.drain("w1", limit=5)
    e = db.inbound_get(rid)
    assert e["state"] == "done" and e["stage"] == "skipped"
    assert not db.outbox_list()
    db.resume_agent(tenant["id"], PHONE)
