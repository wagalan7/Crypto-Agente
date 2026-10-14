"""Janela de horários e seleção de slot — regressão do defeito reproduzido em E01.

Relato de origem: o paciente pediu segunda-feira e foi confirmado para quarta.
Causas identificadas em `EVIDENCIAS/01.md` §3:

H1  a lista oferecida ao modelo é cortada por QUANTIDADE (`limit=24`), não por
    dias, então o dia pedido pode não estar entre as opções;
H2  sem `slot_id`, o fallback `slot_index` indexa essa lista e grava o primeiro
    horário livre do calendário — outro dia, em silêncio;
H3  se o modelo manda o `slot_id` do dia pedido e ele não está na lista
    truncada, o agendamento é recusado genericamente.

Os testes pedem uma **data explícita** (dd/mm) em vez de "segunda-feira": assim a
data que o teste escolhe e a que o sistema interpreta coincidem sempre,
independentemente do dia da semana em que a suíte roda. Onde o nome do dia
importa para a história, o alvo é derivado de `data_do_pedido`, que usa o mesmo
interpretador do sistema.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import calendar_service as cal
from conftest import (PHONE, agora, consultas, data_do_pedido, dia_util_distante,
                      encher_agenda, pedir, proxima_ocorrencia, sid)

SEGUNDA = 0
TERCA = 1


# ── H1: a janela tem de cobrir o dia pedido ───────────────────────────────────

def test_janela_cobre_o_dia_pedido(tenant, modelo):
    """(E01) O dia explicitamente pedido precisa aparecer entre as opções que o
    modelo pode escolher, mesmo com a agenda dos próximos dias cheia."""
    encher_agenda(tenant, dias=3)
    alvo = dia_util_distante(9)
    modelo.responder(action="none", response_text="tenho estes horários")

    import agent
    agent.process_message(tenant, PHONE, pedir(alvo))

    ctx = modelo.ultimo_contexto
    assert f"[id={alvo.strftime('%Y-%m-%d')}" in ctx, (
        f"o dia pedido ({alvo:%d/%m}) não está no contexto enviado ao modelo — "
        f"ele não tem como oferecê-lo"
    )


def test_janela_nao_e_truncada_por_quantidade_antes_de_cobrir_o_horizonte(tenant):
    """(E01) Separar horizonte (dias) de limite de quantidade: a busca tem de
    alcançar o fim do horizonte, não parar no 24º horário do calendário."""
    encher_agenda(tenant, dias=3)
    alvo = dia_util_distante(9)
    achados = cal.find_slots(tenant, days_ahead=14, on_date=alvo.date())
    assert achados, f"nenhum horário encontrado em {alvo:%d/%m} apesar de o dia estar livre"
    assert all(s.date() == alvo.date() for s in achados)


def test_dia_pedido_alem_do_horizonte_padrao_e_alcancado(tenant):
    """`on_date` estica o horizonte: pedido explícito não se perde por ficar
    além de `days_ahead`."""
    alvo = dia_util_distante(20)
    achados = cal.find_slots(tenant, days_ahead=7, on_date=alvo.date())
    assert achados and all(s.date() == alvo.date() for s in achados)


# ── H2: índice global nunca executa ação ──────────────────────────────────────

def test_pedido_de_um_dia_nao_grava_outro(tenant, modelo):
    """(E01) O caso do relato. Modelo devolve `slot_index` sem oferta
    persistida: nada pode ser gravado — e em nenhuma hipótese outro dia."""
    encher_agenda(tenant, dias=3)
    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_index": 1},
        response_text="Prontinho! Marquei sua sessão.",
    )

    import agent
    agent.process_message(tenant, PHONE, pedir(alvo))

    marcadas = consultas(tenant, PHONE)
    assert not marcadas or all(d.date() == alvo.date() for d in marcadas), (
        f"paciente pediu {alvo:%d/%m} e o sistema gravou "
        f"{[f'{d:%a %d/%m %H:%M}' for d in marcadas]}"
    )


def test_segunda_pedida_pelo_nome_do_dia_nao_grava_outro_dia(tenant, modelo):
    """Mesma história do relato, com o dia escrito por nome. O alvo vem do mesmo
    interpretador que o sistema usa, então não depende do dia de execução."""
    texto = "quero agendar na segunda-feira"
    alvo = data_do_pedido(texto)
    assert alvo is not None and alvo.weekday() == SEGUNDA

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_index": 1},
        response_text="Prontinho! Marquei sua sessão de segunda-feira.",
    )
    import agent
    agent.process_message(tenant, PHONE, texto)

    marcadas = consultas(tenant, PHONE)
    assert not marcadas or all(d.date() == alvo for d in marcadas)


def test_indice_sem_oferta_persistida_nao_reserva(tenant, modelo):
    """(E01) Sem mapeamento posição→ID daquela conversa, não se reserva."""
    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_index": 1},
        response_text="Marquei!",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(alvo))
    assert not consultas(tenant, PHONE), "reservou sem oferta persistida"
    assert reply.strip(), "paciente ficou sem resposta"


# ── H3: dia válido fora da lista truncada é aceito ────────────────────────────

def test_dia_valido_alem_dos_primeiros_slots_e_aceito(tenant, modelo):
    """(E01) Não rejeitar genericamente um dia válido só porque a lista anterior
    foi truncada: recomputar com as regras reais e aceitar."""
    encher_agenda(tenant, dias=3)
    alvo = dia_util_distante(9)
    assert alvo not in cal.get_available_slots(tenant, days_ahead=10, limit=24), (
        "pré-condição do teste: o alvo deve estar fora dos 24 primeiros horários"
    )
    ok, motivo = cal.is_slot_bookable(tenant, alvo)
    assert ok, f"pré-condição: o alvo deveria estar livre, mas veio {motivo}"

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        response_text="Prontinho! Marquei.",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(alvo))

    assert consultas(tenant, PHONE) == [alvo], (
        f"esperado {alvo:%a %d/%m %H:%M}, obtido "
        f"{[f'{d:%a %d/%m %H:%M}' for d in consultas(tenant, PHONE)]}"
    )


def test_dia_pedido_sem_vaga_informa_e_nao_reserva(tenant, modelo):
    """Se o dia pedido não tem vaga, informar e oferecer alternativas — sem
    escolher nenhuma automaticamente."""
    import database as db
    alvo = dia_util_distante(9)
    for i, s in enumerate(cal.find_slots(tenant, days_ahead=14, on_date=alvo.date())):
        db.create_appointment(tenant["id"], f"Ocupado {i}", f"551190000{3000+i}", s)
    assert not cal.is_slot_bookable(tenant, alvo)[0], "pré-condição: dia tem de estar cheio"

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        response_text="Marquei!",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(alvo))

    assert not consultas(tenant, PHONE), "reservou num dia sem vaga"
    assert reply.strip()


# ── Compatibilidade entre o pedido e o slot escolhido ─────────────────────────

def test_slot_de_outro_dia_que_o_pedido_nao_reserva(tenant, modelo):
    """(E01) O slot tem de ser compatível com a data solicitada. Não se confia
    no modelo para declarar corretamente o dia."""
    pedido_em = dia_util_distante(9)
    outro = dia_util_distante(16)
    assert outro.date() != pedido_em.date()
    assert cal.is_slot_bookable(tenant, outro)[0]

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(outro)},
        response_text="Prontinho! Marquei para o dia que você pediu.",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(pedido_em))
    assert not consultas(tenant, PHONE), (
        "o sistema aceitou um slot de outro dia que o pedido"
    )


def test_slot_fora_do_periodo_pedido_nao_reserva(tenant, modelo):
    """Pedido "de manhã" não pode virar sessão às 15h."""
    alvo = dia_util_distante(9, hora=15)
    assert cal.is_slot_bookable(tenant, alvo)[0]
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        response_text="Marquei de manhã!",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(alvo, "manhã"))
    assert not consultas(tenant, PHONE), "aceitou 15h para um pedido de manhã"


def test_slot_dentro_do_periodo_pedido_reserva(tenant, modelo):
    alvo = dia_util_distante(9, hora=9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(alvo, "manhã"))
    assert consultas(tenant, PHONE) == [alvo]


@pytest.mark.parametrize("valor", ["", "amanhã às 9h", "2026-13-45T99:99",
                                   "2026-10-19", None, 0])
def test_slot_malformado_nao_altera_agenda(tenant, modelo, valor):
    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": valor},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(alvo))
    assert not consultas(tenant, PHONE), f"slot_id={valor!r} alterou a agenda"


def test_slot_no_passado_nao_altera_agenda(tenant, modelo):
    passado = agora() - timedelta(days=3)
    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(passado)},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(tenant, PHONE, pedir(alvo))
    assert not consultas(tenant, PHONE)


def test_sem_slot_algum_nao_altera_agenda(tenant, modelo):
    """Intenção ambígua: pedir esclarecimento, nunca reservar."""
    modelo.responder(action="create", data={"patient_name": "Aurora Belmonte"},
                     response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero marcar")
    assert not consultas(tenant, PHONE)
    assert reply.strip()


# ── Confirmação canônica ──────────────────────────────────────────────────────

def test_confirmacao_canonica_vem_da_gravacao_nao_de_reescrita(tenant, modelo):
    """(E01) A confirmação é construída do horário gravado. Nunca se reescreve o
    texto do modelo para esconder uma seleção diferente da pedida."""
    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        # o modelo erra a data no texto, de propósito
        response_text="Prontinho! Sua sessão ficou marcada para Quarta, 01/01 às 07:00.",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(alvo))

    assert consultas(tenant, PHONE) == [alvo]
    canonico = cal.format_slots([alvo])[0]
    assert canonico in reply, f"confirmação sem o horário canônico: {reply!r}"
    assert "01/01" not in reply, f"texto errado do modelo sobreviveu: {reply!r}"


# ── Remarcação compartilha a mesma validação ──────────────────────────────────

def test_remarcacao_rejeita_slot_incompativel(tenant, modelo):
    import database as db
    import agent
    atual = proxima_ocorrencia(TERCA, dias_min=3, hora=10)
    appt_id = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, atual)
    pedido_em = dia_util_distante(9)
    outro = dia_util_distante(16)

    modelo.responder(
        intent="reschedule", action="update",
        data={"appointment_id": appt_id, "slot_id": sid(outro)},
        response_text="Remarquei!",
    )
    agent.process_message(tenant, PHONE,
                          f"quero remarcar para dia {pedido_em:%d/%m}")

    assert consultas(tenant, PHONE) == [atual], (
        "remarcou para um dia diferente do pedido"
    )


def test_remarcacao_aceita_dia_valido_fora_da_lista(tenant, modelo):
    import database as db
    import agent
    atual = proxima_ocorrencia(TERCA, dias_min=3, hora=10)
    db_id = db.create_appointment(tenant["id"], "Aurora Belmonte", PHONE, atual)
    encher_agenda(tenant, dias=3)
    alvo = dia_util_distante(9)

    modelo.responder(
        intent="reschedule", action="update",
        data={"appointment_id": db_id, "slot_id": sid(alvo)},
        response_text="Remarquei!",
    )
    agent.process_message(tenant, PHONE, f"quero remarcar para dia {alvo:%d/%m}")
    assert consultas(tenant, PHONE) == [alvo]


# ── Limites de expediente e fuso ──────────────────────────────────────────────

def test_limite_de_expediente_e_exclusivo_no_fim(tenant):
    """`working_hours_end` é exclusivo: 18h entra, 19h não."""
    seg = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=18)
    assert cal.is_slot_bookable(tenant, seg)[0]
    assert not cal.is_slot_bookable(tenant, seg.replace(hour=19))[0]
    assert not cal.is_slot_bookable(tenant, seg.replace(hour=7))[0]


def test_horario_bloqueado_nao_e_ofertavel(tenant):
    seg = proxima_ocorrencia(SEGUNDA, dias_min=7, hora=12)
    ok, motivo = cal.is_slot_bookable(tenant, seg)
    assert not ok and motivo == "horario_bloqueado"


def test_dia_nao_atendido_nao_e_ofertavel(tenant):
    sabado = proxima_ocorrencia(5, dias_min=7, hora=10)
    ok, motivo = cal.is_slot_bookable(tenant, sabado)
    assert not ok and motivo == "dia_nao_atendido"


def test_fuso_fixo_brt_documentado(tenant, monkeypatch):
    """O produto calcula tudo em America/Sao_Paulo, independentemente do fuso do
    processo. Fuso por consultório/paciente **não existe** — limitação conhecida
    (E01 §2.1). Este teste falha quando isso mudar, obrigando a revisão."""
    assert cal._TZ.key == "America/Sao_Paulo"
    monkeypatch.setenv("TZ", "UTC")
    a = cal.get_available_slots(tenant, days_ahead=2, limit=3)
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    b = cal.get_available_slots(tenant, days_ahead=2, limit=3)
    assert a == b, "o cálculo de horários mudou com o fuso do processo"
    import database as db
    with db.get_conn() as conn:
        colunas = {r[1] for r in conn.execute("PRAGMA table_info(tenants)")}
    assert "timezone" not in colunas, (
        "coluna de fuso apareceu: atualizar este teste e as regras de oferta"
    )


def test_modalidade_ainda_nao_e_validada(tenant, modelo):
    """Limitação documentada: não há modalidade (online/presencial) no modelo de
    dados, então ela **não** participa da validação do slot. O pedido continua
    validado por data e período. Quando E04 introduzir modalidade, este teste
    falha e as regras de oferta têm de ser revistas."""
    import database as db
    with db.get_conn() as conn:
        colunas = {r[1] for r in conn.execute("PRAGMA table_info(appointments)")}
    assert "modality" not in colunas and "modalidade" not in colunas

    alvo = dia_util_distante(9)
    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(tenant, PHONE, f"quero agendar online dia {alvo:%d/%m}")
    assert consultas(tenant, PHONE) == [alvo]
