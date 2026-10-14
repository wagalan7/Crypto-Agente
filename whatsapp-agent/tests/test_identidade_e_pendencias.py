"""E03 — revisão final: identidade do evento e recuperação de falhas nos avisos.

Dois achados da revisão, ambos legítimos:

**A. Declarar a limitação não era política conservadora.** Quando o provedor não
manda id de mensagem, duas entregas idênticas viram dois eventos locais
distintos. O registro dizia que isso era "limitação declarada" — mas o sistema
seguia **executando mutações** nos dois, então uma reentrega duplicava
agendamento. Agora: sem identidade confiável o evento é recebido e fica visível,
pode consultar/acolher/esclarecer, mas **não executa** criar, remarcar,
confirmar, cancelar nem efeitos financeiros/contratuais. Fica pendência
operacional com motivo.

**B. Falha ao preparar aviso desaparecia num evento `done`.** `process_event`
capturava a exceção, registrava no log e concluía o evento como `done/finished`.
Agora a falha devolve o evento à fila, e a retomada não repete mutação,
resposta nem avisos já enfileirados.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import database as db
import main
from conftest import (PHONE, SLUG, consultas, dia_util_distante,
                      passar_o_tempo_do_backoff, pedir, sid)

EVOLUTION = "evolution"


@pytest.fixture
def cliente(tenant, tenant_b):
    return TestClient(main.app)


def _post_sem_id(cliente, texto, slug=SLUG, phone="5511900000001"):
    """Webhook da Evolution sem `data.key.id` — provedor sem id confiável."""
    t = db.ensure_webhook_token(db.get_tenant(slug)["id"])
    return cliente.post(
        f"/webhook/{slug}/evolution?token={t}",
        json={"data": {"key": {"remoteJid": f"{phone}@s.whatsapp.net",
                               "fromMe": False},
                       "message": {"conversation": texto}}})


def _post_com_id(cliente, texto, msg_id, slug=SLUG, phone="5511900000001"):
    t = db.ensure_webhook_token(db.get_tenant(slug)["id"])
    return cliente.post(
        f"/webhook/{slug}/evolution?token={t}",
        json={"data": {"key": {"id": msg_id, "remoteJid": f"{phone}@s.whatsapp.net",
                               "fromMe": False},
                       "message": {"conversation": texto}}})


# ══════════════════════════════════════════════════════════════════════════════
# A. Sem identidade confiável → recebe, responde, mas NÃO muta
# ══════════════════════════════════════════════════════════════════════════════

def test_duas_entregas_identicas_sem_id_nao_agendam(cliente, tenant, modelo):
    """O caso que a "limitação declarada" não resolvia: a mesma mensagem
    reentregue duas vezes não pode gerar dois agendamentos — nem um."""
    import inbox
    alvo = dia_util_distante(9)
    _post_sem_id(cliente, pedir(alvo))
    _post_sem_id(cliente, pedir(alvo))

    for _ in range(2):
        modelo.responder(action="create",
                         data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                         response_text="Marquei!")
    inbox.drain("w1", limit=5)
    passar_o_tempo_do_backoff()
    inbox.drain("w1", limit=5)

    assert consultas(tenant, PHONE) == [], (
        "evento sem id do provedor executou agendamento automático"
    )


def test_sem_id_responde_ao_paciente_pedindo_confirmacao(cliente, tenant, modelo):
    """Bloquear a mutação não é ficar calado: o paciente recebe resposta."""
    import inbox
    alvo = dia_util_distante(9)
    _post_sem_id(cliente, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)

    saidas = db.outbox_list(states=("pending",))
    assert saidas, "o paciente ficou sem resposta"
    assert not consultas(tenant, PHONE)


def test_sem_id_registra_pendencia_operacional_com_motivo(cliente, tenant, modelo):
    import inbox
    alvo = dia_util_distante(9)
    _post_sem_id(cliente, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)

    pend = db.pendencia_list(states=("pendente",))
    minhas = [p for p in pend if p["tenant_id"] == tenant["id"]]
    assert minhas, "nenhuma pendência registrada para revisão"
    assert minhas[0]["kind"].startswith("bloqueado:")
    assert "sem id" in (minhas[0]["detail"] or "").lower()


@pytest.mark.parametrize("acao,dados", [
    ("create", {"patient_name": "Aurora Belmonte"}),
    ("update", {"appointment_id": 1}),
    ("confirm", {"appointment_id": 1}),
    ("cancel", {"appointment_id": 1}),
])
def test_sem_id_bloqueia_todas_as_mutacoes(tenant, modelo, acao, dados):
    import agent
    alvo = dia_util_distante(9)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, alvo)
    antes = _estado_consulta(appt)
    dados = dict(dados)
    if "appointment_id" in dados:
        dados["appointment_id"] = appt
    dados["slot_id"] = sid(dia_util_distante(16))

    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "local:semid", PHONE,
                               "faz isso aí", event_id_source="local")
    ev = db.inbound_claim("w1", lease_sec=120)[0]
    modelo.responder(action=acao, data=dados, response_text="Feito!")
    agent.decidir(tenant, PHONE, "faz isso aí", event_row_id=ev["id"],
                  attempt_token=ev["attempt_token"], identidade_confiavel=False)

    assert _estado_consulta(appt) == antes, f"ação {acao} foi executada sem id confiável"


def _estado_consulta(appt_id: int) -> dict:
    with db.get_conn() as conn:
        r = conn.execute("SELECT scheduled_at, confirmed, cancelled FROM appointments "
                         "WHERE id=?", (appt_id,)).fetchone()
    return dict(r)


def test_sem_id_nao_dispara_porta_de_contrato(tenant, modelo, monkeypatch):
    """Efeito contratual (auto-envio de contrato a paciente novo) também fica
    bloqueado: é efeito externo disparado pela entrada."""
    import agent
    import contract_service
    chamadas = []
    monkeypatch.setattr(contract_service, "enforce_new_patient",
                        lambda *a, **k: chamadas.append(a) or (False, ""))

    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "local:contrato", PHONE,
                               "quero agendar", event_id_source="local")
    ev = db.inbound_claim("w1", lease_sec=120)[0]
    modelo.responder(action="create",
                     data={"patient_name": "Aurora", "slot_id": sid(dia_util_distante(9))},
                     response_text="Marquei!")
    agent.decidir(tenant, PHONE, "quero agendar", event_row_id=ev["id"],
                  attempt_token=ev["attempt_token"], identidade_confiavel=False)
    assert chamadas == [], "a porta de contrato rodou num evento sem identidade"


def test_sem_id_ainda_pode_listar_horarios(tenant, modelo):
    """Consultar e esclarecer continuam liberados — não é uma mutação."""
    import agent
    alvo = dia_util_distante(9)
    opcoes = [alvo.replace(hour=h) for h in (9, 10)]
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "local:lista", PHONE,
                               pedir(alvo), event_id_source="local")
    ev = db.inbound_claim("w1", lease_sec=120)[0]
    modelo.responder(action="list_slots",
                     data={"offered_ids": [sid(s) for s in opcoes]},
                     response_text="x")
    d = agent.decidir(tenant, PHONE, pedir(alvo), event_row_id=ev["id"],
                      attempt_token=ev["attempt_token"], identidade_confiavel=False)
    assert d["offer_id"], "listar horários deveria continuar funcionando"
    assert "1. " in d["reply"]


def test_com_ids_distintos_mensagens_identicas_seguem_independentes(cliente, tenant, modelo):
    """A proteção não pode punir o caso legítimo: duas mensagens iguais com ids
    diferentes são dois pedidos reais e ambos agem."""
    import inbox
    alvo = dia_util_distante(9)
    outro = dia_util_distante(16)
    _post_com_id(cliente, pedir(alvo), "ID-OK-1")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert consultas(tenant, PHONE) == [alvo]

    _post_com_id(cliente, pedir(outro), "ID-OK-2")
    modelo.responder(intent="reschedule", action="update",
                     data={"appointment_id": _ultima_consulta(tenant),
                           "slot_id": sid(outro)},
                     response_text="Remarquei!")
    inbox.drain("w1", limit=5)
    assert consultas(tenant, PHONE) == [outro], (
        "mensagem legítima com id próprio deixou de agir"
    )


def _ultima_consulta(tenant: dict) -> int:
    a = db.get_appointments_by_phone(tenant["id"], PHONE)
    return a[0]["id"]


def test_pendencia_aparece_no_suporte_e_pode_ser_resolvida(cliente, tenant, modelo):
    import inbox
    alvo = dia_util_distante(9)
    _post_sem_id(cliente, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)

    r = cliente.get(f"/admin/fila?key={config.MASTER_KEY}")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["pendencias"], "a pendência não aparece na consulta de suporte"
    assert corpo["resumo"]["pendencias_abertas"] >= 1
    pid = corpo["pendencias"][0]["id"]

    r2 = cliente.post(f"/admin/fila/pendencia/{pid}/resolver?key={config.MASTER_KEY}",
                      json={"motivo": "confirmado por telefone com a paciente"})
    assert r2.status_code == 200
    assert db.pendencia_list(states=("pendente",)) == [] or all(
        p["id"] != pid for p in db.pendencia_list(states=("pendente",)))


# ══════════════════════════════════════════════════════════════════════════════
# B. Falha ao preparar avisos não conclui o evento
# ══════════════════════════════════════════════════════════════════════════════

def _evento_com_cancelamento(tenant, modelo, eid="AVISO-1"):
    """Prepara um evento cuja decisão gera aviso à profissional."""
    db.update_tenant(SLUG, psychologist_phone="5511933333333")
    t = db.get_tenant(SLUG)
    alvo = dia_util_distante(9)
    appt = db.create_appointment(t["id"], "Aurora Belmonte", PHONE, alvo)
    rid, _ = db.inbound_record(t["id"], EVOLUTION, eid, PHONE, "quero cancelar")
    modelo.responder(intent="cancel", action="cancel",
                     data={"appointment_id": appt}, response_text="Cancelado.")
    return t, rid, appt


def test_falha_antes_do_primeiro_aviso_nao_conclui_o_evento(tenant, modelo, monkeypatch):
    import inbox
    t, rid, _appt = _evento_com_cancelamento(tenant, modelo, "AVISO-A")

    def _cai(*a, **k):
        raise RuntimeError("falha ao preparar aviso")
    monkeypatch.setattr(inbox, "_registrar_avisos", _cai)
    inbox.drain("w1", limit=5)

    e = db.inbound_get(rid)
    assert e["state"] != "done", "evento concluído apesar de o aviso ter falhado"
    assert e["state"] == "pending" and "aviso" in (e["last_error"] or "").lower()


def test_retomada_apos_falha_de_aviso_nao_duplica_nada(tenant, modelo, monkeypatch):
    """Exatamente um registro de outbox por intenção, depois da retomada."""
    import inbox
    t, rid, appt = _evento_com_cancelamento(tenant, modelo, "AVISO-B")

    monkeypatch.setattr(inbox, "_registrar_avisos",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falha")))
    inbox.drain("w1", limit=5)
    assert db.inbound_get(rid)["state"] == "pending"
    respostas = [o for o in db.outbox_list() if o["event_row_id"] == rid
                 and o["kind"] == "reply"]
    assert len(respostas) == 1, "a resposta não foi persistida antes do aviso"
    cancelado_apos_falha = _estado_consulta(appt)["cancelled"]

    monkeypatch.undo()
    passar_o_tempo_do_backoff()
    modelo.responder(intent="cancel", action="cancel",
                     data={"appointment_id": appt}, response_text="Cancelado.")
    inbox.drain("w1", limit=5)

    e = db.inbound_get(rid)
    assert e["state"] == "done" and e["stage"] == "finished"
    assert _estado_consulta(appt)["cancelled"] == cancelado_apos_falha, (
        "a mutação foi reaplicada na retomada"
    )
    saidas = [o for o in db.outbox_list() if o["event_row_id"] == rid]
    por_chave = {}
    for o in saidas:
        por_chave[o["idempotency_key"]] = por_chave.get(o["idempotency_key"], 0) + 1
    assert all(n == 1 for n in por_chave.values()), f"intenção duplicada: {por_chave}"
    assert len([o for o in saidas if o["kind"] == "reply"]) == 1
    assert len([o for o in saidas if o["kind"] == "notification"]) == 1


def test_falha_depois_de_parte_dos_avisos_retoma_sem_duplicar(tenant, modelo, monkeypatch):
    """Falha entre enfileirar o aviso e marcar o estágio: a retomada não gera um
    segundo aviso, porque a chave de idempotência é por intenção."""
    import inbox
    t, rid, appt = _evento_com_cancelamento(tenant, modelo, "AVISO-C")

    original = inbox._marcar_avisado

    def _cai_depois(*a, **k):
        raise RuntimeError("queda após enfileirar o aviso")
    monkeypatch.setattr(inbox, "_marcar_avisado", _cai_depois)
    inbox.drain("w1", limit=5)

    avisos = [o for o in db.outbox_list() if o["event_row_id"] == rid
              and o["kind"] == "notification"]
    assert len(avisos) == 1, "o aviso não foi enfileirado antes da queda"
    assert db.inbound_get(rid)["state"] == "pending"

    monkeypatch.setattr(inbox, "_marcar_avisado", original)
    passar_o_tempo_do_backoff()
    modelo.responder(intent="cancel", action="cancel",
                     data={"appointment_id": appt}, response_text="Cancelado.")
    inbox.drain("w1", limit=5)

    assert db.inbound_get(rid)["state"] == "done"
    avisos2 = [o for o in db.outbox_list() if o["event_row_id"] == rid
               and o["kind"] == "notification"]
    assert len(avisos2) == 1, "a retomada duplicou o aviso"


def test_excecao_interna_no_handle_message_nao_e_engolida(tenant, modelo, monkeypatch):
    """No caminho durável, `_handle_message` não pode mascarar erro com a
    resposta de desculpas: o evento tem de voltar à fila."""
    import inbox
    t, rid, _appt = _evento_com_cancelamento(tenant, modelo, "AVISO-D")
    monkeypatch.setattr(main, "_notificar",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp caiu")))
    inbox.drain("w1", limit=5)
    assert db.inbound_get(rid)["state"] == "pending"
    assert "smtp caiu" in (db.inbound_get(rid)["last_error"] or "")


def test_tags_de_aviso_sao_distintas_por_intencao(tenant):
    """Chaves iguais fariam dois avisos diferentes colidirem e um sumir."""
    import re
    from pathlib import Path
    fonte = (Path(main.__file__)).read_text(encoding="utf-8")
    tags = re.findall(r'_notificar\([^)]*?,\s*event_row_id,\s*"([a-z_]+)"\)', fonte)
    assert tags, "nenhuma chamada de _notificar encontrada"
    assert len(tags) == len(set(tags)), f"tags repetidas entre intenções: {tags}"


# ══════════════════════════════════════════════════════════════════════════════
# C. Calendário disparado pela entrada
# ══════════════════════════════════════════════════════════════════════════════

def test_falha_de_calendario_registra_pendencia_em_vez_de_so_log(tenant, modelo, monkeypatch):
    import google_calendar_service as gcal
    import inbox
    db.update_tenant(SLUG, google_refresh_token="token-ficticio")
    t = db.get_tenant(SLUG)
    monkeypatch.setattr(gcal, "create_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gcal fora do ar")))

    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(t["id"], EVOLUTION, "CAL-1", PHONE, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)

    # A consulta foi criada (fail-open é deliberado), mas a falha não fica só em log.
    assert consultas(t, PHONE) == [alvo]
    pend = [p for p in db.pendencia_list() if p["event_row_id"] == rid]
    assert pend, "falha de calendário não gerou pendência visível"
    assert pend[0]["kind"].startswith("calendar:")
    assert pend[0]["state"] == "incerto"
    db.update_tenant(SLUG, google_refresh_token="")


def test_pendencia_de_calendario_nao_e_reexecutada_as_cegas(tenant, modelo, monkeypatch):
    """A retomada não tenta criar o evento de calendário outra vez: a consulta já
    está marcada como efeito aplicado, e a pendência fica para resolução."""
    import google_calendar_service as gcal
    import inbox
    db.update_tenant(SLUG, google_refresh_token="token-ficticio")
    t = db.get_tenant(SLUG)
    tentativas = []
    monkeypatch.setattr(gcal, "create_event",
                        lambda *a, **k: tentativas.append(1) or "evt-1")

    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(t["id"], EVOLUTION, "CAL-2", PHONE, pedir(alvo))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert len(tentativas) == 1

    db.inbound_force_pending(rid)
    passar_o_tempo_do_backoff()
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET decision=NULL WHERE id=?", (rid,))
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("w1", limit=5)
    assert len(tentativas) == 1, "o evento de calendário foi criado duas vezes"
    db.update_tenant(SLUG, google_refresh_token="")
