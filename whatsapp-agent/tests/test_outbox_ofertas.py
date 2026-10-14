"""E03 — outbox e elegibilidade das ofertas de horários.

Ponto central desta etapa, corrigindo o que E02 deixou em aberto:

**aceite do provedor não é entrega comprovada, e nenhum dos dois é a mesma
coisa que "não enviado".** Uma oferta de horários só pode resolver um "quero o
1" depois de o envio ser aceito. Envio pendente, incerto ou falho **jamais**
autoriza reserva.

E a correlação é pela identidade da oferta, não por (tenant, telefone): a
resposta atrasada do envio da oferta A não pode marcar a oferta B como entregue.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import calendar_service as cal
import database as db
from conftest import PHONE, agora, consultas, dia_util_distante, sid

OUTRO = "5511922222222"


def _slots(n=2, base_dias=9):
    """Horários fictícios num dia de expediente distante, em horas livres."""
    alvo = dia_util_distante(base_dias)
    return [alvo.replace(hour=h) for h in (9, 10, 11)][:n]


def _nova_oferta(tenant, slots=None, phone=PHONE):
    slots = slots or _slots()
    return db.offer_create(tenant["id"], phone, slots)["offer_id"], slots


def escolher(tenant, modelo, numero: int, phone: str = PHONE):
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": numero},
                     response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, phone, f"quero o {numero}")
    return reply


# ── 1. Identidade e sequência durável ─────────────────────────────────────────

def test_oferta_tem_identidade_imutavel(tenant):
    oid, slots = _nova_oferta(tenant)
    o = db.offer_get(oid)
    assert o["offer_id"] == oid and o["slots"] == slots
    assert o["state"] == "draft" and o["send_state"] == "not_sent"
    assert o["eligible"] is False, "oferta recém-criada não pode ser elegível"


def test_sequencia_e_duravel_mesmo_apagando_linhas(tenant):
    """A identidade não pode voltar atrás quando ofertas antigas são removidas —
    era o problema de MAX(version)+1."""
    o1 = db.offer_create(tenant["id"], PHONE, _slots())
    with db.get_conn() as conn:
        conn.execute("DELETE FROM offers WHERE offer_id=?", (o1["offer_id"],))
    o2 = db.offer_create(tenant["id"], PHONE, _slots())
    assert o2["seq"] > o1["seq"], "sequência reaproveitou um número já usado"


# ── 2. Elegibilidade conforme o estado de envio ───────────────────────────────

@pytest.mark.parametrize("send_state,elegivel", [
    ("not_sent", False),
    ("sending", False),
    ("uncertain", False),
    ("failed", False),
    ("provider_accepted", True),
    ("delivery_confirmed", True),
])
def test_elegibilidade_por_estado_de_envio(tenant, send_state, elegivel):
    oid, _ = _nova_oferta(tenant)
    db.offer_mark_sent(oid, send_state)
    o = db.offer_get(oid)
    assert o["eligible"] is elegivel, (
        f"send_state={send_state} deveria {'' if elegivel else 'não '}ser elegível"
    )


def test_oferta_nao_enviada_bloqueia_escolha_numerica(tenant, modelo):
    """ATUALIZA o teste de E02 que documentava o contrário. Antes, oferta não
    entregue ainda resolvia o "1" com um aviso em log. Agora **bloqueia** e
    pede esclarecimento."""
    oid, slots = _nova_oferta(tenant)
    assert db.offer_get(oid)["send_state"] == "not_sent"
    reply = escolher(tenant, modelo, 1)
    assert not consultas(tenant, PHONE), (
        "reservou contra oferta que o paciente comprovadamente não recebeu"
    )
    assert reply.strip()


def test_oferta_aceita_pelo_provedor_libera_escolha_numerica(tenant, modelo):
    oid, slots = _nova_oferta(tenant)
    db.offer_mark_sent(oid, "provider_accepted")
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [slots[0]]


def test_envio_incerto_nao_autoriza_reserva(tenant, modelo):
    oid, _ = _nova_oferta(tenant)
    db.offer_mark_sent(oid, "uncertain")
    reply = escolher(tenant, modelo, 1)
    assert not consultas(tenant, PHONE)
    assert reply.strip()


def test_envio_falho_nao_autoriza_reserva(tenant, modelo):
    oid, _ = _nova_oferta(tenant)
    db.offer_mark_sent(oid, "failed")
    assert not consultas(tenant, PHONE)
    escolher(tenant, modelo, 1)
    assert not consultas(tenant, PHONE)


# ── 3. Substituição de oferta e recuperação ───────────────────────────────────

def test_oferta_visivel_nao_e_substituida_por_uma_nao_enviada(tenant, modelo):
    """A (aceita) continua valendo enquanto B não foi enviada."""
    a_slots = _slots(2, 9)
    b_slots = _slots(2, 16)
    a = db.offer_create(tenant["id"], PHONE, a_slots)["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    b = db.offer_create(tenant["id"], PHONE, b_slots)["offer_id"]

    ctx = db.offer_context(tenant["id"], PHONE)
    assert ctx["ativa"]["offer_id"] == a, "a oferta visível foi trocada antes do envio"
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [a_slots[0]]


def test_falha_no_envio_da_nova_mantem_a_anterior(tenant, modelo):
    """Recuperação: B falhou definitivamente, o paciente nunca a viu, então A
    continua válida."""
    a_slots = _slots(2, 9)
    b_slots = _slots(2, 16)
    a = db.offer_create(tenant["id"], PHONE, a_slots)["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    b = db.offer_create(tenant["id"], PHONE, b_slots)["offer_id"]
    r = db.offer_mark_sent(b, "failed")
    assert r["acao"] == "cancelada_anterior_mantida"

    assert db.offer_get(b)["state"] == "cancelled"
    assert db.offer_context(tenant["id"], PHONE)["ativa"]["offer_id"] == a
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [a_slots[0]]


def test_envio_incerto_da_nova_torna_a_escolha_ambigua(tenant, modelo):
    """O paciente pode ter visto A ou B. Um "1" sozinho não revela qual —
    então nada é reservado e o agente esclarece."""
    a = db.offer_create(tenant["id"], PHONE, _slots(2, 9))["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    b = db.offer_create(tenant["id"], PHONE, _slots(2, 16))["offer_id"]
    db.offer_mark_sent(b, "uncertain")

    ctx = db.offer_context(tenant["id"], PHONE)
    assert ctx["ambigua"] is True
    reply = escolher(tenant, modelo, 1)
    assert not consultas(tenant, PHONE), "resolveu uma escolha ambígua"
    assert reply.strip()


def test_aceite_da_nova_supera_a_anterior(tenant, modelo):
    a_slots = _slots(2, 9)
    b_slots = _slots(2, 16)
    a = db.offer_create(tenant["id"], PHONE, a_slots)["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    b = db.offer_create(tenant["id"], PHONE, b_slots)["offer_id"]
    db.offer_mark_sent(b, "provider_accepted")

    assert db.offer_get(a)["state"] == "superseded"
    assert db.offer_context(tenant["id"], PHONE)["ativa"]["offer_id"] == b
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [b_slots[0]]


def test_resposta_de_envio_atrasada_da_oferta_a_nao_marca_a_b(tenant):
    """A correlação é por offer_id. Era isto que `mark_slot_offer_delivered(tenant,
    phone)` errava: marcava a oferta que estivesse ali no momento."""
    a = db.offer_create(tenant["id"], PHONE, _slots(2, 9))["offer_id"]
    b = db.offer_create(tenant["id"], PHONE, _slots(2, 16))["offer_id"]
    db.offer_mark_sent(b, "provider_accepted")
    # resposta do envio de A chega agora, atrasada
    db.offer_mark_sent(a, "provider_accepted")
    assert db.offer_get(b)["send_state"] == "provider_accepted"
    assert db.offer_get(a)["offer_id"] == a
    # cada oferta recebeu o seu próprio desfecho; nenhuma herdou o da outra
    assert db.offer_get(a)["send_state"] == "provider_accepted"


def test_desfecho_tardio_nao_ressuscita_oferta_consumida(tenant):
    oid, _ = _nova_oferta(tenant)
    db.offer_mark_sent(oid, "provider_accepted")
    db.offer_consume(oid)
    r = db.offer_mark_sent(oid, "delivery_confirmed")
    assert r["acao"] == "tardio_ignorado"
    assert db.offer_get(oid)["state"] == "consumed"
    assert db.offer_get(oid)["eligible"] is False


def test_oferta_expirada_nao_e_elegivel(tenant, modelo):
    oid, _ = _nova_oferta(tenant)
    db.offer_mark_sent(oid, "provider_accepted")
    with db.get_conn() as conn:
        conn.execute("UPDATE offers SET expires_at=? WHERE offer_id=?",
                     ((agora() - timedelta(minutes=1)).isoformat(timespec="seconds"), oid))
    assert db.offer_get(oid)["eligible"] is False
    escolher(tenant, modelo, 1)
    assert not consultas(tenant, PHONE)


def test_ofertas_sao_isoladas_por_contato(tenant):
    a = db.offer_create(tenant["id"], PHONE, _slots())["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    assert db.offer_context(tenant["id"], OUTRO)["ativa"] is None


# ── 4. Outbox: estados e posse ────────────────────────────────────────────────

def test_outbox_e_idempotente_por_chave(tenant):
    k = "evt-1:reply"
    i1 = db.outbox_enqueue(tenant["id"], PHONE, "oi", k)
    i2 = db.outbox_enqueue(tenant["id"], PHONE, "oi de novo", k)
    assert i1 == i2
    assert db.outbox_get(i1)["body"] == "oi", "a chave repetida sobrescreveu o corpo"


def test_outbox_claim_e_posse(tenant):
    oid = db.outbox_enqueue(tenant["id"], PHONE, "oi", "k-2")
    a = db.outbox_claim("w1", lease_sec=60, limit=5)
    assert [x["id"] for x in a] == [oid]
    assert db.outbox_claim("w2", lease_sec=60, limit=5) == []
    tok = a[0]["attempt_token"]
    assert db.outbox_mark(oid, "token-errado", "provider_accepted") is False
    assert db.outbox_mark(oid, tok, "provider_accepted", provider="evolution",
                          provider_message_id="MSG-9") is True
    o = db.outbox_get(oid)
    assert o["state"] == "provider_accepted" and o["provider_message_id"] == "MSG-9"
    assert o["sent_at"]


def test_outbox_backoff_e_falha_definitiva(tenant):
    oid = db.outbox_enqueue(tenant["id"], PHONE, "oi", "k-3")
    tok = db.outbox_claim("w1", lease_sec=60)[0]["attempt_token"]
    assert db.outbox_requeue(oid, tok, 120, "timeout", max_attempts=3) == "pending"
    assert db.outbox_get(oid)["last_error"] == "timeout"
    with db.get_conn() as conn:
        conn.execute("UPDATE outbox SET attempts=9, next_attempt_at=NULL WHERE id=?", (oid,))
    tok2 = db.outbox_claim("w1", lease_sec=60)[0]["attempt_token"]
    assert db.outbox_requeue(oid, tok2, 120, "timeout", max_attempts=3) == "failed"


def test_resolucao_manual_e_auditada(tenant):
    oid = db.outbox_enqueue(tenant["id"], PHONE, "oi", "k-4")
    tok = db.outbox_claim("w1", lease_sec=60)[0]["attempt_token"]
    db.outbox_mark(oid, tok, "uncertain", last_error="timeout")
    assert db.outbox_resolve(oid, "failed", "suporte: paciente confirmou que não recebeu")
    o = db.outbox_get(oid)
    assert o["state"] == "failed" and "suporte:" in o["resolution"]


# ── 5. Envio pelo worker, com provedor simulado ───────────────────────────────

def test_envio_aceito_marca_provider_accepted_e_ativa_a_oferta(tenant):
    import outbox
    oid_oferta, slots = _nova_oferta(tenant)
    oid = db.outbox_enqueue(tenant["id"], PHONE, "lista", "k-5", offer_id=oid_oferta)
    db.offer_set_outbox(oid_oferta, oid)

    def _aceita(tenant_, phone, body):
        return "provider_accepted", "MSG-1", ""
    res = outbox.flush("w1", limit=5, sender=_aceita)
    assert res and res[0]["state"] == "provider_accepted"
    assert db.outbox_get(oid)["provider_message_id"] == "MSG-1"
    o = db.offer_get(oid_oferta)
    assert o["state"] == "active" and o["eligible"] is True


def test_envio_incerto_marca_uncertain_e_nao_reenvia_as_cegas(tenant):
    import outbox
    oid_oferta, _ = _nova_oferta(tenant)
    oid = db.outbox_enqueue(tenant["id"], PHONE, "lista", "k-6", offer_id=oid_oferta)
    db.offer_set_outbox(oid_oferta, oid)

    def _incerto(tenant_, phone, body):
        return "uncertain", "", "timeout ao ler a resposta"
    outbox.flush("w1", limit=5, sender=_incerto)
    o = db.outbox_get(oid)
    assert o["state"] == "uncertain"
    assert db.offer_get(oid_oferta)["eligible"] is False

    chamadas = []
    outbox.flush("w2", limit=5, sender=lambda *a: chamadas.append(1) or ("provider_accepted", "", ""))
    assert chamadas == [], "reenviou às cegas uma mensagem possivelmente entregue"


def test_envio_recusado_fica_pendente_e_depois_falha(tenant):
    import outbox
    oid = db.outbox_enqueue(tenant["id"], PHONE, "oi", "k-7")

    def _recusa(tenant_, phone, body):
        return "failed", "", "numero inexistente"
    outbox.flush("w1", limit=5, sender=_recusa, max_attempts=2)
    assert db.outbox_get(oid)["state"] == "pending"
    with db.get_conn() as conn:
        conn.execute("UPDATE outbox SET next_attempt_at=NULL WHERE id=?", (oid,))
    outbox.flush("w1", limit=5, sender=_recusa, max_attempts=2)
    o = db.outbox_get(oid)
    assert o["state"] == "failed" and o["last_error"] == "numero inexistente"


def test_falha_definitiva_do_envio_cancela_a_oferta_nova(tenant):
    import outbox
    a_slots = _slots(2, 9)
    a = db.offer_create(tenant["id"], PHONE, a_slots)["offer_id"]
    db.offer_mark_sent(a, "provider_accepted")
    b = db.offer_create(tenant["id"], PHONE, _slots(2, 16))["offer_id"]
    oid = db.outbox_enqueue(tenant["id"], PHONE, "nova lista", "k-8", offer_id=b)
    db.offer_set_outbox(b, oid)

    with db.get_conn() as conn:
        conn.execute("UPDATE outbox SET attempts=9 WHERE id=?", (oid,))
    outbox.flush("w1", limit=5, max_attempts=3,
                 sender=lambda *a: ("failed", "", "recusado"))
    assert db.outbox_get(oid)["state"] == "failed"
    assert db.offer_get(b)["state"] == "cancelled"
    assert db.offer_context(tenant["id"], PHONE)["ativa"]["offer_id"] == a


def test_backpressure_limita_quantidade_por_ciclo(tenant):
    import outbox
    for i in range(8):
        db.outbox_enqueue(tenant["id"], PHONE, f"m{i}", f"k-bp-{i}")
    enviados = outbox.flush("w1", limit=3,
                            sender=lambda *a: ("provider_accepted", "", ""))
    assert len(enviados) == 3
    assert len(db.outbox_list(states=("pending",))) == 5
