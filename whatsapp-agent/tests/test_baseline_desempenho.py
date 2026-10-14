"""Baseline de desempenho local — referência de regressão, **não** capacidade.

O que estes números são: tempo das operações de agenda num SQLite local, numa
máquina de desenvolvimento, com um consultório e algumas centenas de consultas.

O que estes números **não** são: capacidade de produção, limite de clientes
simultâneos, nem promessa de latência. A produção roda em container com volume
de rede, uma réplica, `journal_mode=DELETE` e concorrência real de webhooks —
nada disso é reproduzido aqui. Qualquer afirmação comercial de capacidade
depende de teste de carga, que não foi feito (E26).

Os limites abaixo são folgados de propósito: servem para pegar regressão
grosseira (uma consulta O(n²) nova, por exemplo), não para medir a máquina.
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest

import calendar_service as cal
import database as db
from conftest import agora, proxima_ocorrencia

CONSULTAS = 300


@pytest.fixture
def agenda_cheia(tenant):
    base = agora().replace(hour=8, minute=0, second=0, microsecond=0)
    with db.get_conn() as conn:
        for i in range(CONSULTAS):
            quando = base + timedelta(hours=i)
            conn.execute(
                "INSERT INTO appointments (tenant_id, patient_name, phone, scheduled_at) "
                "VALUES (?,?,?,?)",
                (tenant["id"], f"Carga {i}", f"551190001{i:04d}", quando.isoformat()),
            )
    return tenant


def _medir(fn, repeticoes: int = 20) -> float:
    inicio = time.perf_counter()
    for _ in range(repeticoes):
        fn()
    return (time.perf_counter() - inicio) / repeticoes * 1000  # ms por chamada


def test_baseline_busca_de_horarios(agenda_cheia, record_property):
    ms = _medir(lambda: cal.find_slots(agenda_cheia, days_ahead=14, per_day=4))
    record_property("find_slots_ms", round(ms, 2))
    print(f"\n[baseline] find_slots(14 dias, 4/dia) com {CONSULTAS} consultas: {ms:.1f} ms")
    assert ms < 500, f"busca de horários em {ms:.0f} ms — investigar regressão"


def test_baseline_deteccao_de_conflito(agenda_cheia, record_property):
    alvo = proxima_ocorrencia(0, dias_min=7, hora=16)
    ms = _medir(lambda: db.has_conflict(agenda_cheia["id"], alvo, 50))
    record_property("has_conflict_ms", round(ms, 2))
    print(f"[baseline] has_conflict com {CONSULTAS} consultas: {ms:.1f} ms")
    assert ms < 200, f"detecção de conflito em {ms:.0f} ms — investigar regressão"


def test_baseline_oferta_persistida(agenda_cheia, record_property):
    alvo = proxima_ocorrencia(0, dias_min=7, hora=16)
    slots = [alvo + timedelta(minutes=50 * i) for i in range(5)]

    def ciclo():
        o = db.offer_create(agenda_cheia["id"], "5511900000009", slots)
        db.offer_mark_sent(o["offer_id"], "provider_accepted")
        db.offer_context(agenda_cheia["id"], "5511900000009")

    ms = _medir(ciclo)
    record_property("oferta_ciclo_ms", round(ms, 2))
    print(f"[baseline] gravar+ler oferta: {ms:.1f} ms")
    assert ms < 200


def test_has_conflict_nao_cresce_com_consultas_canceladas(agenda_cheia):
    """Regressão de corretude, não de tempo: canceladas e `missed_with_notice`
    liberam o horário."""
    alvo = agora().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=400)
    appt_id = db.create_appointment(agenda_cheia["id"], "Cancelada", "5511900000008", alvo)
    assert db.has_conflict(agenda_cheia["id"], alvo, 50)
    with db.get_conn() as conn:
        conn.execute("UPDATE appointments SET cancelled=1 WHERE id=?", (appt_id,))
    assert not db.has_conflict(agenda_cheia["id"], alvo, 50)
