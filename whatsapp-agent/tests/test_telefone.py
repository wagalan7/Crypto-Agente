"""Normalização de telefone — BR e internacional.

Defeito de origem (build 67): a regra antiga era "se não começa com 55, prefixa
55", o que transformava +351 968 035 444 (Portugal) em 55351968035444 e o
WhatsApp respondia `exists:false`. A regra atual decide por COMPRIMENTO.
"""
from __future__ import annotations

import pytest

from whatsapp_service import _normalize_phone


@pytest.mark.parametrize("entrada,esperado", [
    # ── Brasil ────────────────────────────────────────────────────────────────
    ("11987654321", "5511987654321"),      # celular, 11 dígitos com 9
    ("1133334444", "551133334444"),        # fixo, 10 dígitos
    ("5511987654321", "5511987654321"),    # já com DDI
    ("551133334444", "551133334444"),
    ("+55 11 98765-4321", "5511987654321"),
    ("(11) 98765-4321", "5511987654321"),
    ("0 11 98765 4321", "5511987654321"),   # 0 de operadora
    # DDD 55 (Santa Maria/RS): o 55 é DDD, não DDI
    ("55987654321", "5555987654321"),
])
def test_brasil(entrada, esperado):
    assert _normalize_phone(entrada) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("351968035444", "351968035444"),        # Portugal
    ("+351 968 035 444", "351968035444"),
    ("00351968035444", "351968035444"),      # 00 internacional
    ("12125551234", "12125551234"),          # EUA: 11 dígitos, 3º != 9
    ("34612345678", "34612345678"),          # Espanha
    ("5215512345678", "5215512345678"),      # México
])
def test_internacional_nao_recebe_55(entrada, esperado):
    assert _normalize_phone(entrada) == esperado


@pytest.mark.parametrize("entrada", ["", None, "   ", "abc", "+++", "0", "00"])
def test_entradas_vazias_devolvem_vazio(entrada):
    assert _normalize_phone(entrada) == ""


def test_colisao_conhecida_eua_com_9_na_terceira_posicao():
    """Limitação conhecida e aceita: 11 dígitos com 9 na 3ª posição é tratado
    como celular brasileiro. Um número dos EUA de área '2 9 0' cai nessa regra.
    Documentado para que uma mudança futura seja consciente, não corrigido aqui."""
    assert _normalize_phone("12901234567") == "5512901234567"
