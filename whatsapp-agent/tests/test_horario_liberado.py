"""Ocupação do horário × marcação de presença — regressão do R3.

Relato de origem (19/10): a paciente das 18:00 desmarcou em cima da hora, a
psicóloga marcou "faltou sem aviso" para cobrar, e o reagendamento de outro
paciente para as 18:00 foi recusado por conflito. O horário também deixava de
ser ofertado pelo agente.

A marcação de presença responde a duas perguntas diferentes — cobrança e
ocupação. Estes testes fixam as duas separadamente: "faltou sem aviso" LIBERA
o horário e CONTINUA cobrável.

O horário-alvo vem sempre da própria grade do agente: ela anda em passos da
duração da sessão a partir do relógio, então um horário fixo como 18:00 pode
nunca ser candidato — e um teste "não oferece" passaria por vacuidade.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import calendar_service as cal
import database as db
from conftest import PHONE, dia_util_distante

OUTRO = "5511988887777"


def _horario_da_grade(tenant: dict):
    dia = dia_util_distante(9).date()
    grade = [s for s in cal.find_slots(tenant, days_ahead=21) if s.date() == dia]
    assert grade, "grade vazia no dia escolhido — cenário inválido"
    return grade[len(grade) // 2]


def _ofertado(tenant: dict, quando) -> bool:
    return quando in cal.find_slots(tenant, days_ahead=21)


def test_controle_horario_vazio_e_ofertado(tenant):
    """Sem isto, os testes de 'não oferece' abaixo não provariam nada."""
    alvo = _horario_da_grade(tenant)
    assert _ofertado(tenant, alvo)
    assert not db.has_conflict(tenant["id"], alvo, tenant["session_minutes"])


@pytest.mark.parametrize("status, ocupa", [
    ("pending", True),
    ("attended", True),
    ("missed_with_notice", False),
    ("missed_no_notice", False),     # ← o defeito do R3
])
def test_ocupacao_segue_se_a_sessao_vai_acontecer(tenant, status, ocupa):
    alvo = _horario_da_grade(tenant)
    aid = db.create_appointment(tenant["id"], "Paciente Fictícia", PHONE, alvo)
    db.set_attendance(tenant["id"], aid, status)

    assert db.has_conflict(tenant["id"], alvo, tenant["session_minutes"]) is ocupa
    assert _ofertado(tenant, alvo) is (not ocupa)


def test_reagendar_outro_paciente_para_horario_de_quem_faltou(tenant):
    """O caso exato do relato: o horário de quem faltou sem aviso recebe outro
    paciente, sem conflito."""
    alvo = _horario_da_grade(tenant)
    faltou = db.create_appointment(tenant["id"], "Paciente Fictícia", PHONE, alvo)
    db.set_attendance(tenant["id"], faltou, "missed_no_notice")
    outro = db.create_appointment(tenant["id"], "Outro Fictício", OUTRO,
                                  alvo.replace(hour=max(alvo.hour - 2, 8)))

    assert not db.has_conflict(tenant["id"], alvo, tenant["session_minutes"],
                               exclude_id=outro)


def test_faltou_sem_aviso_continua_cobravel(tenant):
    """Liberar o horário NÃO pode mudar a cobrança."""
    alvo = _horario_da_grade(tenant)
    aid = db.create_appointment(tenant["id"], "Paciente Fictícia", PHONE, alvo)
    db.set_attendance(tenant["id"], aid, "missed_no_notice")

    sessoes = db.get_appointments_in_range(
        tenant["id"], (alvo - timedelta(days=1)).isoformat(),
        (alvo + timedelta(days=1)).isoformat())
    cobraveis = [a for a in sessoes
                 if not a.get("cancelled")
                 and (a.get("attendance") or "pending") != "missed_with_notice"]
    assert any(a["id"] == aid for a in cobraveis)


def test_regra_tem_fonte_unica():
    assert db.ATTENDANCE_LIBERA_HORARIO == frozenset(
        {"missed_no_notice", "missed_with_notice"})
    assert db.ATTENDANCE_LIBERA_HORARIO <= db.ATTENDANCE_VALUES
