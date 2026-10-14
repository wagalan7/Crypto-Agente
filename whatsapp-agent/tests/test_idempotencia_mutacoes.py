"""E03 — identidade de evento em TODAS as mutações do fluxo de entrada.

Correção de uma decisão errada do registro intermediário. Eu havia escrito que
confirmar, cancelar e remarcar não precisavam de marca por evento porque eram
"idempotentes por valor" — gravam um estado, não acumulam.

**Isso não basta.** O problema não é gravar duas vezes o mesmo valor: é
reaplicar uma decisão ANTIGA depois de uma ação posterior. Um retry atrasado da
confirmação, depois de o paciente ter remarcado, confirma a consulta no horário
errado, zera de novo os marcadores de lembrete (`confirmation_sent`,
`followup_sent`) e redispara aviso à profissional.

Além disso, a posse do lease é validada **dentro da transação da mutação**, não
só na troca de estágio: um worker que perdeu a posse não pode mexer na agenda.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import database as db
from conftest import PHONE, agora, consultas, dia_util_distante, pedir, sid

EVOLUTION = "evolution"


def _evento(tenant, body="oi", eid="IDEM-1"):
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, eid, PHONE, body)
    return db.inbound_claim("w1", lease_sec=120)[0]


def _marcadores(appt_id: int) -> dict:
    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT confirmed, confirmation_sent, followup_sent, cancelled, "
            "scheduled_at, COALESCE(reschedule_count,0) AS rc "
            "FROM appointments WHERE id=?", (appt_id,)).fetchone()
    return dict(r)


# ── Confirmação ───────────────────────────────────────────────────────────────

def test_confirmacao_nao_e_reaplicada_pelo_mesmo_evento(tenant):
    import agent
    alvo = dia_util_distante(9)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, alvo)
    ev = _evento(tenant, "sim", "IDEM-CONF")

    with agent.contexto_evento(ev["id"], ev["attempt_token"]):
        assert agent._confirmar_idempotente(tenant["id"], appt) is True
        assert agent._confirmar_idempotente(tenant["id"], appt) is False, (
            "a confirmação foi aplicada duas vezes pelo mesmo evento"
        )


def test_confirmacao_antiga_nao_desfaz_remarcacao_posterior(tenant):
    """O cenário que "idempotente por valor" não cobria.

    1. paciente confirma  → evento A grava confirmed=1
    2. paciente remarca   → evento B move a consulta e zera os marcadores
    3. retry do evento A  → NÃO pode confirmar de novo nem zerar nada
    """
    import agent
    inicial = dia_util_distante(9)
    novo = dia_util_distante(16)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, inicial)

    ev_a = _evento(tenant, "sim", "IDEM-A")
    with agent.contexto_evento(ev_a["id"], ev_a["attempt_token"]):
        agent._confirmar_idempotente(tenant["id"], appt)
    assert _marcadores(appt)["confirmed"] == 1
    db.inbound_update(ev_a["id"], ev_a["attempt_token"], state="done", stage="finished")

    # O contato precisa estar livre para o segundo evento ser reivindicado.
    ev_b = _evento(tenant, f"remarcar dia {novo:%d/%m}", "IDEM-B")
    with agent.contexto_evento(ev_b["id"], ev_b["attempt_token"]):
        agent._remarcar_idempotente(tenant["id"], appt, novo)
    depois_de_remarcar = _marcadores(appt)
    assert depois_de_remarcar["confirmed"] == 0
    assert depois_de_remarcar["scheduled_at"].startswith(novo.strftime("%Y-%m-%d"))
    db.inbound_update(ev_b["id"], ev_b["attempt_token"], state="done", stage="finished")

    # Retry do evento A, com posse renovada: a marca de efeito o impede.
    db.inbound_force_pending(ev_a["id"])
    ev_a2 = db.inbound_claim("w2", lease_sec=120)[0]
    assert ev_a2["id"] == ev_a["id"]
    with agent.contexto_evento(ev_a2["id"], ev_a2["attempt_token"]):
        assert agent._confirmar_idempotente(tenant["id"], appt) is False

    final = _marcadores(appt)
    assert final == depois_de_remarcar, (
        "o retry do evento antigo alterou a consulta já remarcada"
    )


# ── Remarcação ────────────────────────────────────────────────────────────────

def test_remarcacao_nao_zera_lembretes_duas_vezes(tenant):
    """`update_appointment` zera `confirmation_sent`/`followup_sent` e incrementa
    `reschedule_count`. Reaplicar inflaria a contagem e reabriria os lembretes."""
    import agent
    inicial = dia_util_distante(9)
    novo = dia_util_distante(16)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, inicial)
    with db.get_conn() as conn:
        conn.execute("UPDATE appointments SET confirmation_sent=1, followup_sent=1 "
                     "WHERE id=?", (appt,))
    ev = _evento(tenant, f"remarcar dia {novo:%d/%m}", "IDEM-REM")

    with agent.contexto_evento(ev["id"], ev["attempt_token"]):
        agent._remarcar_idempotente(tenant["id"], appt, novo)
        antes = _marcadores(appt)
        agent._remarcar_idempotente(tenant["id"], appt, novo)
    assert _marcadores(appt) == antes, "a remarcação foi reaplicada"
    assert antes["rc"] == 1, f"reschedule_count inflado: {antes['rc']}"


# ── Cancelamento ──────────────────────────────────────────────────────────────

def test_cancelamento_nao_e_reaplicado(tenant):
    import agent
    alvo = dia_util_distante(9)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, alvo)
    ev = _evento(tenant, "quero cancelar", "IDEM-CANC")
    with agent.contexto_evento(ev["id"], ev["attempt_token"]):
        assert agent._cancelar_idempotente(tenant["id"], appt) is True
        assert agent._cancelar_idempotente(tenant["id"], appt) is False


# ── Conversa (append-only) ────────────────────────────────────────────────────

def test_mensagem_nao_e_duplicada_na_aba_conversas(tenant):
    import agent
    ev = _evento(tenant, "oi", "IDEM-MSG")
    with agent.contexto_evento(ev["id"], ev["attempt_token"]):
        agent._salvar_mensagem_idempotente(tenant["id"], PHONE, "user", "oi")
        agent._salvar_mensagem_idempotente(tenant["id"], PHONE, "user", "oi")
    hist = db.get_conversation_history(tenant["id"], PHONE, limit=20)
    assert len([m for m in hist if (m.get("content") or "") == "oi"]) == 1


# ── Posse validada DENTRO da mutação ──────────────────────────────────────────

def test_worker_que_perdeu_a_posse_nao_altera_a_agenda(tenant):
    """A verificação de posse acontece na transação da gravação. Sem isso, o
    worker antigo chegaria com um objeto de evento em memória e mexeria na
    agenda depois de outro worker ter assumido."""
    import agent
    alvo = dia_util_distante(9)
    novo = dia_util_distante(16)
    appt = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, alvo)
    antigo = _evento(tenant, "remarcar", "IDEM-POSSE")

    # lease vence e outro worker assume
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET lease_until=? WHERE id=?",
                     ((agora() - timedelta(minutes=5)).isoformat(timespec="seconds"),
                      antigo["id"]))
    novo_dono = db.inbound_claim("w2", lease_sec=120)[0]
    assert novo_dono["attempt_token"] != antigo["attempt_token"]

    antes = _marcadores(appt)
    with agent.contexto_evento(antigo["id"], antigo["attempt_token"]):
        with pytest.raises(db.PosseEPerdida):
            agent._remarcar_idempotente(tenant["id"], appt, novo)
    assert _marcadores(appt) == antes, "worker sem posse alterou a consulta"

    # E o dono atual consegue.
    with agent.contexto_evento(novo_dono["id"], novo_dono["attempt_token"]):
        assert agent._remarcar_idempotente(tenant["id"], appt, novo) is True


def test_posse_perdida_tambem_bloqueia_criacao(tenant):
    import agent
    alvo = dia_util_distante(9)
    ev = _evento(tenant, "agendar", "IDEM-POSSE2")
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET attempt_token='outro' WHERE id=?",
                     (ev["id"],))
    with agent.contexto_evento(ev["id"], ev["attempt_token"]):
        with pytest.raises(db.PosseEPerdida):
            agent._criar_consulta_idempotente(tenant["id"], "Aurora", PHONE, alvo)
    assert not consultas(tenant, PHONE)


def test_sem_contexto_de_evento_a_mutacao_segue_direta(tenant):
    """Painel, endpoint de teste e agendador não têm evento para ancorar. O
    comportamento é o de sempre — limite registrado em EVIDENCIAS/03.md."""
    import agent
    alvo = dia_util_distante(9)
    appt, criada = agent._criar_consulta_idempotente(
        tenant["id"], "Aurora Belmonte", PHONE, alvo)
    assert criada and appt
    assert agent._confirmar_idempotente(tenant["id"], appt) is True
    assert agent._confirmar_idempotente(tenant["id"], appt) is True, (
        "sem evento não há marca: a segunda chamada grava de novo, como antes"
    )


# ── Reentrada no fluxo completo ───────────────────────────────────────────────

def test_reentrada_repete_a_confirmacao_sem_criar_segunda_consulta(tenant, modelo):
    """Reexecutar o evento devolve o MESMO texto e não cria outra consulta."""
    import agent
    import inbox
    alvo = dia_util_distante(9)
    rid, _ = db.inbound_record(tenant["id"], EVOLUTION, "IDEM-RE", PHONE, pedir(alvo))

    for _ in range(2):
        modelo.responder(action="create",
                         data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                         response_text="Marquei!")
    inbox.drain("w1", limit=5)
    primeiro = db.inbound_get(rid)["reply_text"]
    assert consultas(tenant, PHONE) == [alvo]

    db.inbound_force_pending(rid)
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET decision=NULL WHERE id=?", (rid,))
    inbox.drain("w1", limit=5)

    assert consultas(tenant, PHONE) == [alvo], "criou segunda consulta"
    assert db.inbound_get(rid)["reply_text"] == primeiro, (
        "a resposta mudou entre tentativas"
    )
