"""A1: IDs propostos pelo modelo não autorizam ações em consultas de terceiros."""
from datetime import datetime, timedelta

import pytest

import agent
import database as db
from models import Action, AgentResponse, Intent
from conftest import PHONE, passar_o_tempo_do_backoff

AGORA = datetime(2030, 3, 4, 10)
SLOT = datetime(2030, 3, 5, 17, 20)
OUTRO = "5511900000002"
PROFISSIONAL = "5511933333333"


@pytest.fixture(autouse=True)
def cenario(tenant, relogio, monkeypatch):
    relogio(AGORA)
    obter = db.get_appointments_by_phone
    monkeypatch.setattr(db, "get_appointments_by_phone", lambda tid, phone, now_iso=None:
                        obter(tid, phone, now_iso=now_iso or AGORA.isoformat()))
    db.upsert_patient(tenant["id"], PHONE, name="Aurora Belmonte")
    db.save_message(tenant["id"], PHONE, "user", "Meu nome é Aurora Belmonte")
    chamadas = []
    import contract_service
    for nome in ("enforce_new_patient", "blocks_confirmation"):
        def gate(*a, _nome=nome, **kw):
            chamadas.append(_nome)
            return False, ""
        monkeypatch.setattr(contract_service, nome, gate)
    for modulo in (agent.gcal, agent.caldav_svc):
        monkeypatch.setattr(modulo, "delete_event", lambda *a, **kw: chamadas.append("calendar:delete"))
    return chamadas


def _consulta(t, phone=PHONE, nome="Aurora Belmonte", slot=SLOT):
    return db.create_appointment(t["id"], nome, phone, slot)


def _resposta(acao, dados):
    return AgentResponse(intent=Intent(acao), action=Action(acao), data=dados,
                         response_text="Bianca Terceira: consulta cancelada e confirmada.")


def _estado(t, ident):
    return db.get_appointment_by_id(t["id"], ident)


def _recusada(resp, reply, event):
    assert resp.action == Action.none and resp.intent == Intent.other
    assert resp.data == {} and resp.response_text == reply
    assert event is None
    assert "Bianca" not in reply and "Terceira" not in reply
    assert "dia" in reply.lower() and "horário" in reply.lower()


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
@pytest.mark.parametrize("propria", [False, True])
def test_A1_id_alheio_nao_muta_nao_cai_no_fallback_e_nao_vaza(tenant, acao, propria, cenario):
    outro_id = _consulta(tenant, OUTRO, "Bianca Terceira")
    db.set_appointment_google_event_id(outro_id, "evento-alheio-ficticio")
    propria_id = _consulta(tenant, slot=SLOT + timedelta(days=1)) if propria else None
    antes = _estado(tenant, outro_id)
    resp = _resposta(acao, {"appointment_id": outro_id})
    reply, event = agent._execute_action(tenant, resp, PHONE)
    assert _estado(tenant, outro_id) == antes
    if propria_id:
        assert _estado(tenant, propria_id)["cancelled"] == 0
        assert _estado(tenant, propria_id)["confirmed"] == 0
    _recusada(resp, reply, event)
    assert cenario == [], "recusa não pode executar gates de contrato ou calendário"


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
def test_A1_outra_empresa_e_inexistente_tem_mesma_recusa(tenant, tenant_b, acao, cenario):
    propria = _consulta(tenant)
    alheia = _consulta(tenant_b, PHONE, "Bianca Terceira")
    antes = _estado(tenant_b, alheia)
    replies = []
    for ident in (alheia, 2147483647):
        resp = _resposta(acao, {"appointment_id": ident})
        reply, event = agent._execute_action(tenant, resp, PHONE)
        _recusada(resp, reply, event)
        replies.append(reply)
    assert replies[0] == replies[1]
    assert _estado(tenant_b, alheia) == antes
    assert _estado(tenant, propria)["cancelled"] == 0
    assert _estado(tenant, propria)["confirmed"] == 0
    assert cenario == []


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
@pytest.mark.parametrize("valor", ["", 0, False, True, 1.2, [], {}, "invalido", 2**80, "9" * 5000],
                         ids=["vazio", "zero", "false", "true", "float", "lista", "dict", "texto", "overflow", "texto-enorme"])
def test_A1_id_invalido_nao_vira_fallback_nem_excecao(tenant, acao, valor, cenario):
    propria = _consulta(tenant)
    antes = _estado(tenant, propria)
    resp = _resposta(acao, {"appointment_id": valor})
    reply, event = agent._execute_action(tenant, resp, PHONE)
    _recusada(resp, reply, event)
    assert _estado(tenant, propria) == antes
    assert cenario == []


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
@pytest.mark.parametrize("texto_id", [False, True])
def test_A1_dono_com_telefone_equivalente_continua_operando(tenant, acao, texto_id):
    ident = _consulta(tenant, PHONE[2:])
    resp = _resposta(acao, {"appointment_id": str(ident) if texto_id else ident})
    agent._execute_action(tenant, resp, PHONE)
    assert _estado(tenant, ident)["cancelled" if acao == "cancel" else "confirmed"] == 1


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
@pytest.mark.parametrize("dados", [{}, {"appointment_id": None}], ids=["ausente", "null"])
def test_A1_fallback_sem_id_continua_limitado_ao_proprio_paciente(tenant, acao, dados):
    propria = _consulta(tenant)
    alheia = _consulta(tenant, OUTRO, "Bianca Terceira", SLOT + timedelta(days=1))
    resp = _resposta(acao, dados)
    agent._execute_action(tenant, resp, PHONE)
    campo = "cancelled" if acao == "cancel" else "confirmed"
    assert _estado(tenant, propria)[campo] == 1
    assert _estado(tenant, alheia)[campo] == 0


def test_A1_confirmacao_sem_id_com_varias_consultas_pergunta(tenant):
    ids = [_consulta(tenant), _consulta(tenant, slot=SLOT + timedelta(days=1))]
    reply, event = agent._execute_action(tenant, _resposta("confirm", {}), PHONE)
    assert "qual" in reply.lower() and event is None
    assert all(_estado(tenant, ident)["confirmed"] == 0 for ident in ids)


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
def test_A1_sem_telefone_nao_autoriza_id(tenant, acao, cenario):
    ident = _consulta(tenant)
    antes = _estado(tenant, ident)
    resp = _resposta(acao, {"appointment_id": ident})
    reply, event = agent._execute_action(tenant, resp, "")
    _recusada(resp, reply, event)
    assert _estado(tenant, ident) == antes
    assert cenario == []


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
def test_A1_fluxo_duravel_recusado_nao_notifica_nem_aplica_efeito(tenant, modelo, acao, monkeypatch):
    import inbox
    import main
    db.update_tenant(tenant["slug"], psychologist_phone=PROFISSIONAL)
    t = db.get_tenant(tenant["slug"])
    propria = _consulta(t)
    alheia = _consulta(t, OUTRO, "Bianca Terceira", SLOT + timedelta(days=1))
    antes = [_estado(t, propria), _estado(t, alheia)]
    async def proibido(*a, **kw):
        pytest.fail("recusa tentou enviar mensagem fora do outbox")
    monkeypatch.setattr(main.wa, "send_message", proibido)
    rid, _ = db.inbound_record(t["id"], "evolution", f"A1-RECUSA-{acao}", PHONE,
                              "Por favor faça a alteração na consulta informada")
    modelo.responder(intent=acao, action=acao, data={"appointment_id": alheia},
                     response_text="Bianca Terceira: alteração concluída.")
    resultados = inbox.drain("a1-worker", limit=1)
    assert resultados and db.inbound_get(rid)["state"] == "done"
    saidas = [o for o in db.outbox_list() if o["event_row_id"] == rid]
    assert len(saidas) == 1 and saidas[0]["kind"] == "reply"
    assert "Bianca" not in str(saidas[0])
    with db.get_conn() as conn:
        # Registrar as mensagens recebida/respondida é permitido; a ação
        # recusada não pode produzir um efeito de consulta.
        assert conn.execute("SELECT COUNT(*) FROM applied_effects WHERE event_id=? AND kind LIKE 'appointment:%'",
                            (rid,)).fetchone()[0] == 0
    assert [_estado(t, propria), _estado(t, alheia)] == antes


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
def test_A1_evento_legitimo_reprocessado_nao_repete_efeito_ou_aviso(tenant, modelo, acao):
    import inbox
    db.update_tenant(tenant["slug"], psychologist_phone=PROFISSIONAL)
    t = db.get_tenant(tenant["slug"])
    ident = _consulta(t)
    rid, _ = db.inbound_record(t["id"], "evolution", f"A1-REPLAY-{acao}", PHONE,
                              "Por favor faça a alteração na minha consulta")
    modelo.responder(intent=acao, action=acao, data={"appointment_id": ident}, response_text="Tudo certo.")
    inbox.drain("a1-worker", limit=1)
    assert db.inbound_get(rid)["state"] == "done"
    antes = [o["id"] for o in db.outbox_list() if o["event_row_id"] == rid]
    assert antes
    db.inbound_force_pending(rid)
    passar_o_tempo_do_backoff()
    inbox.drain("a1-worker", limit=1)
    assert db.inbound_get(rid)["state"] == "done"
    assert [o["id"] for o in db.outbox_list() if o["event_row_id"] == rid] == antes
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM applied_effects WHERE event_id=? AND kind=?",
                            (rid, f"appointment:{acao}")).fetchone()[0] == 1


@pytest.mark.parametrize("acao", ["cancel", "confirm"])
def test_A1_evento_sem_identidade_confiavel_preserva_recusa(tenant, modelo, acao):
    ident = _consulta(tenant)
    rid, _ = db.inbound_record(tenant["id"], "evolution", f"A1-SEM-ID-{acao}", PHONE,
                              "Por favor altere minha consulta", event_id_source="local")
    ev = db.inbound_claim("a1-worker", lease_sec=120)[0]
    modelo.responder(intent=acao, action=acao, data={"appointment_id": ident}, response_text="Feito.")
    decisao = agent.decidir(tenant, PHONE, "Por favor altere minha consulta", event_row_id=rid,
                           attempt_token=ev["attempt_token"], identidade_confiavel=False)
    assert _estado(tenant, ident)["cancelled"] == _estado(tenant, ident)["confirmed"] == 0
    assert decisao["action"] == "none" and decisao["intent"] == "other"
    assert decisao["event"] is None
