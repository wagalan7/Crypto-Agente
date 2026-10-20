"""E08B — seleção: janela de 7 dias pela data de publicação, datas ausentes ou
futuras, deduplicação por URL e tema, prioridade e limite de destaques."""
from __future__ import annotations

from datetime import timedelta

import noticias as nt
from .apoio_noticias import em_brasilia, link_cfp, link_ms

AGORA = em_brasilia(2026, 10, 20, 10)
FONTES = {f.id: f for f in (nt.CFP, nt.MINISTERIO_SAUDE)}


def _it(n, titulo, horas_atras=None, fonte="cfp", resumo="", url=None):
    link = url or (link_cfp(n) if fonte == "cfp" else link_ms(n))
    f = FONTES[fonte]
    u = nt.url_publica(link, f)
    return nt.Item(fonte_id=fonte, url=u[0], chave=u[1], titulo=titulo, resumo=resumo,
                   publicado_em=None if horas_atras is None else AGORA - timedelta(hours=horas_atras))


def test_janela_de_sete_dias_corridos():
    dentro = _it(1, "CFP orienta psicólogas (fictício)", horas_atras=7 * 24 - 1)
    fora = _it(2, "CFP orienta psicólogos sobre prazo antigo (fictício)", horas_atras=7 * 24 + 1)
    sel, descartes = nt.selecionar([dentro, fora], FONTES, AGORA)
    assert [i.chave for i in sel] == [dentro.chave]
    assert descartes == {"publicado há mais de 7 dias": 1}


def test_sem_data_e_data_futura_nao_entram_como_desta_semana():
    sem_data = _it(1, "Resolução fictícia sem data", horas_atras=None)
    futuro = _it(2, "Resolução fictícia do futuro", horas_atras=-48)
    ok = _it(3, "Resolução fictícia válida", horas_atras=1)
    sel, descartes = nt.selecionar([sem_data, futuro, ok], FONTES, AGORA)
    assert [i.chave for i in sel] == [ok.chave]
    assert descartes == {"sem data de publicação": 1, "data de publicação no futuro": 1}


def test_tolerancia_pequena_para_relogio_da_fonte_adiantado():
    quase = _it(1, "Nota técnica fictícia", horas_atras=-0.1)   # 6 min no "futuro"
    assert nt.selecionar([quase], FONTES, AGORA)[0]


def test_url_repetida_com_rastreio_barra_ou_fragmento_conta_uma_vez():
    a = _it(1, "Resolução fictícia A", horas_atras=2, url="https://site.cfp.org.br/noticia-x/")
    b = _it(1, "Resolução fictícia A", horas_atras=2, url="https://site.cfp.org.br/noticia-x?utm_medium=email")
    c = _it(1, "Resolução fictícia A", horas_atras=2, url="https://site.cfp.org.br/noticia-x/#topo")
    sel, descartes = nt.selecionar([a, b, c], FONTES, AGORA)
    assert len(sel) == 1 and descartes == {"URL repetida": 2}


def test_mesmo_tema_em_duas_fontes_vira_um_destaque():
    a = _it(1, "Nova resolução fictícia sobre atendimento psicológico online publicada", horas_atras=3)
    b = _it(2, "Resolução fictícia sobre atendimento psicológico online é publicada",
            horas_atras=1, fonte="ms-saude")
    sel, descartes = nt.selecionar([a, b], FONTES, AGORA)
    assert len(sel) == 1 and descartes == {"mesmo tema de outro destaque": 1}


def test_fonte_geral_so_entra_com_tema_de_saude_mental():
    caps = _it(1, "Ministério amplia CAPS fictícios", horas_atras=2, fonte="ms-saude")
    cardio = _it(2, "Ministério inaugura hospital fictício de cardiologia", horas_atras=1, fonte="ms-saude")
    sel, descartes = nt.selecionar([caps, cardio], FONTES, AGORA)
    assert [i.chave for i in sel] == [caps.chave]
    assert descartes == {"fora do tema": 1}


def test_agenda_institucional_nao_vira_destaque_so_por_ser_recente():
    pesar = _it(1, "Nota de sentir: psicóloga fictícia", horas_atras=1)
    reuniao = _it(2, "CFP participa de reunião fictícia", horas_atras=2)
    norma = _it(3, "CFP publica resolução fictícia sobre testes psicológicos", horas_atras=100)
    sel, descartes = nt.selecionar([pesar, reuniao, norma], FONTES, AGORA)
    assert [i.chave for i in sel] == [norma.chave]
    assert descartes == {"agenda institucional": 2}


def test_prioridade_normativa_antes_de_recencia():
    recente = _it(1, "Psicologia fictícia em debate no seminário", horas_atras=1)
    normativa = _it(2, "Prazo fictício para psicólogas termina na sexta", horas_atras=90)
    sel, _ = nt.selecionar([recente, normativa], FONTES, AGORA)
    assert [i.chave for i in sel] == [normativa.chave, recente.chave]


def test_no_maximo_cinco_e_nunca_completa_com_item_fraco():
    temas = ["avaliação", "supervisão", "prontuário", "teleatendimento", "estágio",
             "publicidade", "honorários", "sigilo"]
    muitos = [_it(i, f"Resolução fictícia sobre {tema}", horas_atras=i) for i, tema in enumerate(temas, 1)]
    assert len(nt.selecionar(muitos, FONTES, AGORA)[0]) == 5
    # Só 1 relevante: 1 destaque, e não 3 completados com o que sobrou.
    um = [_it(1, "Resolução fictícia", horas_atras=1), _it(2, "Nota de pesar fictícia", horas_atras=1)]
    assert len(nt.selecionar(um, FONTES, AGORA)[0]) == 1


def test_nada_relevante_retorna_vazio():
    assert nt.selecionar([], FONTES, AGORA) == ([], {})


def test_assinatura_muda_so_quando_o_conteudo_muda():
    a = [_it(1, "Resolução fictícia", horas_atras=1)]
    b = [_it(1, "Resolução fictícia", horas_atras=5)]          # mesma notícia, data diferente
    c = [_it(1, "Resolução fictícia corrigida", horas_atras=1)]
    assert nt.assinatura(a) == nt.assinatura(b)
    assert nt.assinatura(a) != nt.assinatura(c)


def test_termo_norma_em_nome_proprio_nao_conta_como_normativo():
    """"Norma" é nome próprio no feed real do CFP (nota de 13/10): não pode
    pontuar como norma."""
    it = _it(1, "Homenagem fictícia à psicóloga Norma Fictícia", horas_atras=1)
    pontos, motivos = nt.pontuar(it, nt.CFP)
    assert "normativo/prazo" not in motivos
