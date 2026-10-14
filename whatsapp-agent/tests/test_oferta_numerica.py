"""Escolha numérica ("quero o 1") resolvida por oferta persistida.

Regra desta etapa: o paciente continua podendo responder com um número, mas o
servidor resolve esse número por uma **oferta persistida daquela conversa**,
vinculada a IDs exatos. Nunca pela posição na lista global do calendário — foi
isso que marcou paciente em dia errado (ver `EVIDENCIAS/01.md` §3).

Se o mapeamento não existir, estiver vencido, fora da faixa ou se o horário
tiver sido ocupado nesse meio-tempo: pedir esclarecimento e **não reservar**.
"""
from __future__ import annotations

from datetime import timedelta

import calendar_service as cal
import database as db
from conftest import (PHONE, agora, confirmar_envio_da_ultima_oferta, consultas,
                      data_do_pedido, oferta_aceita_pelo_provedor,
                      oferta_vigente, proxima_ocorrencia, sid)

SEGUNDA = 0
TERCA = 1


def _oferta(tenant: dict, slots: list) -> str:
    """Oferta cujo envio o provedor aceitou — a única que pode resolver um
    número. Em E02 bastava "persistida"; em E03 exige evidência de envio."""
    return oferta_aceita_pelo_provedor(tenant, PHONE, slots)


# ── Persistência da oferta ────────────────────────────────────────────────────

def test_oferta_e_persistida_quando_o_agente_lista_horarios(tenant, modelo):
    # O alvo vem do MESMO interpretador que o sistema usa para ler
    # "segunda-feira", senão o teste depende do dia em que roda.
    texto = "quero agendar na segunda-feira"
    _d = data_do_pedido(texto)
    alvo = agora().replace(year=_d.year, month=_d.month, day=_d.day,
                           hour=9, minute=0, second=0, microsecond=0)
    oferecidos = [alvo, alvo.replace(hour=10), alvo.replace(hour=11)]
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(s) for s in oferecidos]},
        response_text="Tenho 1. 09:00  2. 10:00  3. 11:00",
    )
    import agent
    agent.process_message(tenant, PHONE, texto)

    # Montada em draft e, depois do aceite do envio, vigente — nessa ordem.
    assert oferta_vigente(tenant, PHONE) is None, "draft não pode estar vigente"
    confirmar_envio_da_ultima_oferta(tenant, PHONE)
    assert oferta_vigente(tenant, PHONE) == oferecidos


def test_oferta_e_por_conversa_nao_por_consultorio(tenant, modelo):
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7)
    _oferta(tenant, [alvo])
    assert oferta_vigente(tenant, "5511999999999") is None


# ── Resolução numérica ────────────────────────────────────────────────────────

def test_indice_resolve_pela_oferta_nao_pela_lista_global(tenant, modelo):
    """Renumeração: a oferta mostrada tem 3 opções filtradas; o "1" do paciente
    é o 1 da oferta, não o primeiro horário livre do calendário."""
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    oferecidos = [alvo, alvo.replace(hour=17), alvo.replace(hour=18)]
    _oferta(tenant, oferecidos)

    primeiro_global = cal.get_available_slots(tenant, days_ahead=10, limit=1)[0]
    assert primeiro_global != alvo, "pré-condição: o alvo não é o 1º do calendário"

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_index": 1},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(tenant, PHONE, "quero o 1")

    assert consultas(tenant, PHONE) == [alvo], (
        f"esperado o 1º da oferta ({alvo:%a %d/%m %H:%M}), obtido "
        f"{[f'{d:%a %d/%m %H:%M}' for d in consultas(tenant, PHONE)]}"
    )


def test_indice_do_meio_da_oferta(tenant, modelo):
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    oferecidos = [alvo, alvo.replace(hour=17), alvo.replace(hour=18)]
    _oferta(tenant, oferecidos)
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 2},
                     response_text="Marquei!")
    import agent
    agent.process_message(tenant, PHONE, "o 2")
    assert consultas(tenant, PHONE) == [oferecidos[1]]


def test_indice_fora_da_faixa_da_oferta_nao_reserva(tenant, modelo):
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    _oferta(tenant, [alvo, alvo.replace(hour=17)])
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 9},
                     response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero o 9")
    assert not consultas(tenant, PHONE)
    assert reply.strip()


def test_oferta_vencida_nao_reserva(tenant, modelo):
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    oid = _oferta(tenant, [alvo])
    with db.get_conn() as conn:
        conn.execute("UPDATE offers SET expires_at=? WHERE offer_id=?",
                     ((agora() - timedelta(hours=1)).isoformat(timespec="seconds"), oid))
    assert oferta_vigente(tenant, PHONE) is None

    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "o 1")
    assert not consultas(tenant, PHONE), "reservou a partir de oferta vencida"
    assert reply.strip()


def test_slot_da_oferta_ocupado_nesse_meio_tempo_nao_reserva(tenant, modelo):
    """Concorrência de mensagens no caminho afetado: entre a oferta e a escolha,
    o horário foi tomado. A disponibilidade atual tem de ser reconferida."""
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    _oferta(tenant, [alvo])
    db.create_appointment(tenant["id"], "Outro Paciente", "5511955555555", alvo)

    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "o 1")
    assert not consultas(tenant, PHONE), "reservou horário já ocupado"
    assert reply.strip()


def test_oferta_consumida_nao_reserva_de_novo(tenant, modelo):
    """Mensagem repetida ("1" duas vezes) não pode mover nem duplicar a sessão."""
    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    _oferta(tenant, [alvo, alvo.replace(hour=17)])

    import agent
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    agent.process_message(tenant, PHONE, "o 1")
    assert consultas(tenant, PHONE) == [alvo]

    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 2},
                     response_text="Marquei!")
    agent.process_message(tenant, PHONE, "o 1")
    assert consultas(tenant, PHONE) == [alvo], (
        "a oferta já consumida continuou valendo e a sessão foi movida"
    )


# ── Interação numérica preservada no atalho determinístico ────────────────────

def test_alternativas_do_atalho_deterministico_ficam_ofertadas(tenant, modelo):
    """Quando o paciente pede data+hora indisponível, o atalho oferece
    alternativas. Essas alternativas precisam ficar persistidas para que o "1"
    seguinte funcione."""
    import agent
    atual = proxima_ocorrencia(TERCA, dias_min=3, hora=10)
    db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, atual)

    alvo = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=16)
    db.create_appointment(tenant["id"], "Outro", "5511944444444", alvo)
    assert not cal.is_slot_bookable(tenant, alvo)[0]

    pedido = f"dia {alvo.strftime('%d/%m')} às {alvo.hour}h"
    reply, *_ = agent.process_message(tenant, PHONE, pedido)
    assert "não está disponível" in reply or "não encontrei" in reply

    # O atalho monta a oferta; ela nasce em draft e passa a valer quando o
    # envio for aceito. O que se exige aqui é que ela EXISTA e seja válida.
    ctx = db.offer_context(tenant["id"], PHONE)
    pendentes = ctx["pendentes"]
    assert pendentes, "o atalho ofereceu alternativas sem registrar a oferta"
    oferta = db.offer_get(pendentes[-1])["slots"]
    assert all(cal.is_slot_bookable(tenant, s, exclude_id=None)[0] for s in oferta)
