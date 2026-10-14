"""Guarda contra alegação comercial sem prova no material do produto.

Origem: a landing trazia depoimentos atribuídos a pessoas inexistentes e
métricas nunca apuradas ("40% menos faltas no primeiro mês", "2 horas livres por
dia"), além de "Mais de 30 psicólogas já usam" com 8 consultórios cadastrados e
1 em operação. O ESCOPO do plano proíbe exatamente isso.

Este teste falha se qualquer uma dessas afirmações voltar ao repositório. Ele
**não** diz nada sobre o site publicado — o que está no ar só muda com deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / "backend"
ARQUIVOS = [
    RAIZ / "templates" / "landing.html",
    RAIZ / "templates" / "landing_vertical.html",
    RAIZ / "landing_content.py",
    RAIZ / "main.py",
]

# Cada padrão é uma alegação que exigiria apuração para ser publicada.
PROIBIDOS = [
    (r"\d+\s*%\s*menos\s+faltas", "percentual de redução de faltas"),
    (r"\d+\s*horas?\s+livres?\s+por\s+dia", "promessa de horas economizadas"),
    (r"mais\s+de\s+\d+\s+psic[óo]logas", "contagem de clientes sem apuração"),
    (r"em\s+\d+\s+minutos\s+estava\s+tudo\s+pronto", "promessa de tempo de setup"),
    (r"\d+\s*min\b[^<\n]{0,20}configurar", "promessa de tempo de setup"),
    (r"nunca\s+perceberam\s+que\s+era\s+autom[áa]tico", "prova social fabricada"),
    (r"reduz(?:em|iu)\s+(?:drasticamente\s+)?(?:as\s+)?faltas", "resultado não apurado"),
    (r"garantia\s+de\s+(?:disponibilidade|uptime)", "garantia de disponibilidade"),
    (r"quase\s+ningu[ée]m\s+esquece", "resultado não apurado"),
]


@pytest.mark.parametrize("arquivo", [a for a in ARQUIVOS], ids=lambda p: p.name)
def test_sem_alegacao_sem_prova(arquivo: Path):
    if not arquivo.exists():
        pytest.skip(f"{arquivo.name} não existe")
    texto = arquivo.read_text(encoding="utf-8")
    # Citar a alegação para explicar por que ela saiu é legítimo; o que não pode
    # é a alegação em conteúdo exibido. Por isso comentários HTML, comentários
    # Python e docstrings são descartados antes da busca. (Nestes arquivos
    # docstring é documentação, nunca texto de tela.)
    sem_comentarios = re.sub(r"<!--.*?-->", "", texto, flags=re.S)
    sem_comentarios = re.sub(r'"""(?:.|\n)*?"""', "", sem_comentarios)
    sem_comentarios = re.sub(r"'''(?:.|\n)*?'''", "", sem_comentarios)
    sem_comentarios = re.sub(r"^\s*#.*$", "", sem_comentarios, flags=re.M)
    for padrao, descricao in PROIBIDOS:
        achado = re.search(padrao, sem_comentarios, re.IGNORECASE)
        assert not achado, (
            f"{arquivo.name}: {descricao} voltou ao material — {achado.group(0)!r}"
        )


def test_depoimentos_padrao_vazios():
    """Sem depoimento de cliente real autorizado, a lista fica vazia."""
    import main
    assert main.DEFAULT_TESTIMONIALS == []
    assert main._get_testimonials() == []


def test_landing_renderiza_sem_depoimento():
    """A página tem de funcionar com a lista vazia (sem seção órfã nem erro)."""
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    r = c.get("/")
    assert r.status_code == 200
    assert "40% menos faltas" not in r.text
    assert "Marina R." not in r.text
    assert "O que o agente faz" in r.text


def test_descontos_dos_planos_fecham_com_os_precos():
    """Percentual que PODE ser publicado é o que a aritmética comprova."""
    html = (RAIZ / "templates" / "landing.html").read_text(encoding="utf-8")
    assert "15% de desconto" in html and "25% de desconto" in html
    mensal, semestral, anual = 199, 169, 149
    assert round((1 - semestral / mensal) * 100) == 15
    assert round((1 - anual / mensal) * 100) == 25
    assert "Economize R$ 600 no ano" in html
    assert (mensal - anual) * 12 == 600
