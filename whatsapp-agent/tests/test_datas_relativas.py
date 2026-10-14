"""Interpretação de data, hora e período em PT-BR.

São funções puras (recebem o "agora" por parâmetro), então os casos de virada de
mês e de ano são deterministas — não dependem do dia em que a suíte roda.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import agent

# Quarta-feira, 21/10/2026 — referência fixa para todos os casos relativos.
REF = datetime(2026, 10, 21, 10, 0)


@pytest.mark.parametrize("texto,esperado", [
    ("dia 23/10 às 16h", date(2026, 10, 23)),
    ("23/10/26", date(2026, 10, 23)),
    ("23/10/2026", date(2026, 10, 23)),
    ("hoje", date(2026, 10, 21)),
    ("amanhã", date(2026, 10, 22)),
    ("amanha", date(2026, 10, 22)),
    ("depois de amanhã", date(2026, 10, 23)),
    ("dia 30", date(2026, 10, 30)),
])
def test_datas_absolutas_e_relativas(texto, esperado):
    assert agent._parse_date_br(texto, REF) == esperado


@pytest.mark.parametrize("texto,esperado", [
    ("segunda", date(2026, 10, 26)),
    ("segunda-feira", date(2026, 10, 26)),
    ("terça", date(2026, 10, 27)),
    ("terca", date(2026, 10, 27)),
    ("quinta", date(2026, 10, 22)),
    ("sexta", date(2026, 10, 23)),
])
def test_nomes_de_dia_apontam_para_a_proxima_ocorrencia(texto, esperado):
    assert agent._parse_date_br(texto, REF) == esperado


def test_mesmo_dia_da_semana_vai_para_a_semana_seguinte():
    """REF é quarta. "quarta" significa a próxima, não hoje."""
    assert agent._parse_date_br("quarta", REF) == date(2026, 10, 28)


def test_virada_de_mes():
    ref = datetime(2026, 10, 31, 9, 0)
    assert agent._parse_date_br("dia 2", ref) == date(2026, 11, 2)


def test_virada_de_ano_por_dia_do_mes():
    ref = datetime(2026, 12, 28, 9, 0)
    assert agent._parse_date_br("dia 5", ref) == date(2027, 1, 5)


def test_virada_de_ano_por_data_sem_ano():
    ref = datetime(2026, 12, 28, 9, 0)
    assert agent._parse_date_br("05/01", ref) == date(2027, 1, 5)


def test_data_sem_ano_que_ja_passou_vai_para_o_ano_seguinte():
    assert agent._parse_date_br("10/03", REF) == date(2027, 3, 10)


@pytest.mark.parametrize("texto", ["30/02", "31/04", "dia 32", "sem data aqui"])
def test_datas_impossiveis_ou_ausentes_devolvem_none(texto):
    assert agent._parse_date_br(texto, REF) is None


@pytest.mark.parametrize("texto,esperado", [
    ("16:30", (16, 30)),
    ("16h30", (16, 30)),
    ("16h", (16, 0)),
    ("16 horas", (16, 0)),
    ("às 16", (16, 0)),
    ("as 9", (9, 0)),
    ("meio dia", (12, 0)),
    ("meio-dia", (12, 0)),
])
def test_horas(texto, esperado):
    assert agent._parse_time_br(texto) == esperado


def test_hora_nao_confunde_data_com_hora():
    """"às 16/06" é data, não "às 16"."""
    assert agent._parse_time_br("às 16/06") is None


@pytest.mark.parametrize("texto", ["sem hora", "99:99", "25h"])
def test_horas_invalidas(texto):
    assert agent._parse_time_br(texto) is None


# ── Período do dia ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("texto,esperado", [
    ("segunda de manhã", "manha"),
    ("prefiro pela manha", "manha"),
    ("segunda à tarde", "tarde"),
    ("pode ser de tarde", "tarde"),
    ("só à noite", "noite"),
    ("depois das 18, à noitinha", "noite"),
    ("qualquer horário", None),
    ("tanto faz", None),
])
def test_periodo(texto, esperado):
    assert agent._parse_period_br(texto) == esperado


@pytest.mark.parametrize("hora,periodo", [
    (6, "manha"), (9, "manha"), (11, "manha"),
    (12, "tarde"), (15, "tarde"), (17, "tarde"),
    (18, "noite"), (21, "noite"), (23, "noite"),
])
def test_faixas_de_periodo(hora, periodo):
    assert agent._periodo_da_hora(hora) == periodo


def test_pedido_combina_data_e_periodo():
    pedido = agent._parse_request("quero agendar na segunda de manhã", REF)
    assert pedido["date"] == date(2026, 10, 26)
    assert pedido["period"] == "manha"


def test_pedido_vazio_quando_nao_ha_preferencia():
    pedido = agent._parse_request("quero marcar uma sessão", REF)
    assert pedido["date"] is None and pedido["period"] is None
