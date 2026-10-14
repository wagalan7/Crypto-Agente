"""Coerência entre a oferta EXIBIDA e a oferta PERSISTIDA.

Achado da revisão de E02: persistir os `offered_ids` do modelo não basta. O
texto que o paciente lê vinha do modelo e a oferta gravada vinha de outra lista;
nada garantia mesma ordem nem mesmo conteúdo. Dois casos concretos:

- o texto numera 1=dia A e 2=dia B, mas `offered_ids` chega `[B, A]` → o "1" do
  paciente reserva B;
- o primeiro id é inválido ou já está ocupado e é descartado, o texto continua
  com duas opções → o "1" do paciente passa a apontar para o que ele viu como 2.

Regra desta revisão: **a lista numerada visível é construída pelo servidor, a
partir da mesma sequência de instantes que ele persiste.** O texto do modelo não
é fonte de data, ordem nem numeração.
"""
from __future__ import annotations

import re
from datetime import timedelta

import calendar_service as cal
import database as db
from conftest import (PHONE, SLUG_B, agora, consultas, data_do_pedido,
                      confirmar_envio_da_ultima_oferta, dia_util_distante,
                      oferta_aceita_pelo_provedor, oferta_vigente, pedir, sid)

ITEM = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$", re.MULTILINE)


def lista_exibida(reply: str) -> list[str]:
    """Os itens numerados que o paciente vê, em ordem de numeração."""
    itens = [(int(n), t) for n, t in ITEM.findall(reply)]
    assert [n for n, _ in itens] == list(range(1, len(itens) + 1)), (
        f"numeração não é 1..N: {[n for n, _ in itens]}"
    )
    return [t for _, t in itens]


def oferta(tenant: dict, phone: str = PHONE):
    """A oferta que vale para resolver um número: enviada e aceita."""
    return oferta_vigente(tenant, phone)


def oferta_montada(tenant: dict, phone: str = PHONE):
    """A última oferta montada pelo servidor neste turno, mesmo em draft —
    é ela que o texto da resposta tem de refletir."""
    ctx = db.offer_context(tenant["id"], phone)
    ids = ctx["pendentes"] + ctx["incertas"] + ([ctx["ativa"]["offer_id"]] if ctx["ativa"] else [])
    if not ids:
        return None
    ultimo = max((db.offer_get(i) for i in ids), key=lambda o: o["seq"])
    return ultimo["slots"]


def exige_coerencia(tenant: dict, reply: str) -> list:
    """O texto visível tem de ser exatamente a oferta persistida, na ordem."""
    gravada = oferta_montada(tenant)
    assert gravada, "nada foi persistido apesar de a resposta listar horários"
    assert lista_exibida(reply) == cal.format_slots(gravada), (
        f"texto exibido difere da oferta gravada.\n"
        f"exibido: {lista_exibida(reply)}\ngravado: {cal.format_slots(gravada)}"
    )
    return gravada


def escolher(tenant: dict, modelo, numero: int, slot_id=None, phone: str = PHONE):
    """Paciente responde com um número; o modelo pode mandar um id junto."""
    data = {"patient_name": "Aurora Belmonte", "slot_index": numero}
    if slot_id is not None:
        data["slot_id"] = slot_id
    modelo.responder(action="create", data=data, response_text="Marquei!")
    import agent
    reply, *_ = agent.process_message(tenant, phone, f"quero o {numero}")
    return reply


# ── 1. Texto e IDs em ordem inversa ───────────────────────────────────────────

def test_texto_e_ids_em_ordem_inversa_a_oferta_visivel_manda(tenant, modelo):
    a = dia_util_distante(9, hora=9)
    b = dia_util_distante(16, hora=9)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(b), sid(a)]},      # ordem INVERSA do texto
        response_text=f"Tenho:\n1. {cal.format_slots([a])[0]}\n"
                      f"2. {cal.format_slots([b])[0]}",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero agendar")
    gravada = exige_coerencia(tenant, reply)

    # O envio dessa lista é aceito pelo provedor — só então ela pode resolver
    # um número (E03). Antes disso o paciente não a recebeu.
    confirmar_envio_da_ultima_oferta(tenant, PHONE)
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [gravada[0]], (
        "o '1' do paciente não reservou a opção 1 exibida"
    )


# ── 2. Primeiro ID inválido / ocupado / duplicado ─────────────────────────────

def test_primeiro_id_invalido_nao_desloca_a_numeracao(tenant, modelo):
    b = dia_util_distante(9, hora=9)
    c = dia_util_distante(9, hora=10)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": ["2026-13-45T99:99", sid(b), sid(c)]},
        response_text="Tenho:\n1. opção inválida\n2. segunda opção\n3. terceira opção",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero agendar")
    gravada = exige_coerencia(tenant, reply)
    assert gravada == [b, c], "id inválido deveria sair da oferta"

    confirmar_envio_da_ultima_oferta(tenant, PHONE)
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [b], (
        "a numeração antiga sobreviveu ao descarte do id inválido"
    )


def test_id_ocupado_sai_da_oferta_e_do_texto(tenant, modelo):
    ocupado = dia_util_distante(9, hora=9)
    livre = dia_util_distante(9, hora=10)
    db.create_appointment(tenant["id"], "Outro", "5511955555555", ocupado)

    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(ocupado), sid(livre)]},
        response_text="Tenho duas opções: 1 e 2",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero agendar")
    gravada = exige_coerencia(tenant, reply)
    assert gravada == [livre]
    assert cal.format_slots([ocupado])[0] not in reply


def test_id_duplicado_nao_gera_duas_posicoes(tenant, modelo):
    a = dia_util_distante(9, hora=9)
    b = dia_util_distante(9, hora=10)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(a), sid(a), sid(b)]},
        response_text="Tenho três opções",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero agendar")
    gravada = exige_coerencia(tenant, reply)
    assert gravada == [a, b], "duplicata deveria ser colapsada"


# ── 3. Texto menciona horário ausente dos IDs ─────────────────────────────────

def test_horario_citado_pelo_modelo_e_ausente_dos_ids_nao_sobrevive(tenant, modelo):
    real = dia_util_distante(9, hora=9)
    inventado = dia_util_distante(23, hora=14)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(real)]},
        response_text=f"Tenho {cal.format_slots([inventado])[0]} e "
                      f"{cal.format_slots([real])[0]}",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, "quero agendar")
    exige_coerencia(tenant, reply)
    assert cal.format_slots([inventado])[0] not in reply, (
        "horário que não está na oferta continuou visível ao paciente"
    )


# ── 4. Paciente responde "1" e o modelo manda o ID da opção 2 ─────────────────

def test_escolha_numerica_vence_slot_id_contraditorio(tenant, modelo):
    a = dia_util_distante(9, hora=9)
    b = dia_util_distante(9, hora=10)
    oferta_aceita_pelo_provedor(tenant, PHONE, [a, b])

    escolher(tenant, modelo, 1, slot_id=sid(b))
    marcadas = consultas(tenant, PHONE)
    assert marcadas == [a], (
        f"paciente disse '1' (={a:%H:%M}) e o sistema usou o id do modelo "
        f"({[f'{d:%H:%M}' for d in marcadas]})"
    )


def test_escolha_numerica_sem_oferta_nao_reserva_nem_com_slot_id(tenant, modelo):
    alvo = dia_util_distante(9, hora=9)
    assert oferta(tenant) is None
    escolher(tenant, modelo, 1, slot_id=sid(alvo))
    assert not consultas(tenant, PHONE), (
        "escolha numérica sem oferta foi resolvida pelo id do modelo"
    )


def test_escolha_numerica_fora_da_faixa_nao_reserva_nem_com_slot_id(tenant, modelo):
    a = dia_util_distante(9, hora=9)
    b = dia_util_distante(9, hora=10)
    oferta_aceita_pelo_provedor(tenant, PHONE, [a, b])
    escolher(tenant, modelo, 5, slot_id=sid(a))
    assert not consultas(tenant, PHONE)


def test_escolha_numerica_com_oferta_expirada_nao_reserva(tenant, modelo):
    a = dia_util_distante(9, hora=9)
    oferta_aceita_pelo_provedor(tenant, PHONE, [a])
    with db.get_conn() as conn:
        conn.execute("UPDATE offers SET expires_at=? WHERE tenant_id=? AND phone=?",
                     ((agora() - timedelta(hours=1)).isoformat(timespec="seconds"),
                      tenant["id"], PHONE))
    escolher(tenant, modelo, 1, slot_id=sid(a))
    assert not consultas(tenant, PHONE), "oferta vencida autorizou reserva"


def test_oferta_substituida_resolve_pela_mais_recente(tenant, modelo):
    antiga_a = dia_util_distante(9, hora=9)
    nova_a = dia_util_distante(16, hora=9)
    oferta_aceita_pelo_provedor(tenant, PHONE, [antiga_a])
    oferta_aceita_pelo_provedor(tenant, PHONE, [nova_a])
    escolher(tenant, modelo, 1)
    assert consultas(tenant, PHONE) == [nova_a], "resolveu pela oferta substituída"


def test_oferta_e_isolada_por_contato_e_por_consultorio(tenant, tenant_b, modelo):
    a = dia_util_distante(9, hora=9)
    oferta_aceita_pelo_provedor(tenant, PHONE, [a])
    assert oferta_vigente(tenant, "5511977777777") is None
    assert oferta_vigente(tenant_b, PHONE) is None

    escolher(tenant_b, modelo, 1, phone=PHONE)
    assert not consultas(tenant_b, PHONE), (
        "oferta de um consultório foi usada noutro"
    )


# ── 5. Escolha explícita de data/hora continua valendo ────────────────────────

def test_data_hora_explicita_prevalece_sobre_a_oferta(tenant, modelo):
    """O paciente pode ignorar a lista e dizer o horário. Isso não é escolha
    numérica e não deve ser resolvido pela oferta."""
    ofertado = dia_util_distante(9, hora=9)
    escolhido = dia_util_distante(9, hora=16)
    oferta_aceita_pelo_provedor(tenant, PHONE, [ofertado])

    modelo.responder(
        action="create",
        data={"patient_name": "Aurora Belmonte", "slot_id": sid(escolhido)},
        response_text="Marquei!",
    )
    import agent
    agent.process_message(
        tenant, PHONE,
        f"prefiro dia {escolhido:%d/%m} às {escolhido.hour}h",
    )
    assert consultas(tenant, PHONE) == [escolhido]


# ── 6. Lista numerada fora de list_slots invalida a oferta ativa ──────────────

def test_lista_numerada_fora_de_list_slots_invalida_a_oferta(tenant, modelo):
    """Se o modelo volta a listar horários sem declarar ids (ação diferente de
    `list_slots`), a oferta anterior não corresponde mais ao que o paciente vê."""
    a = dia_util_distante(9, hora=9)
    oferta_aceita_pelo_provedor(tenant, PHONE, [a])
    modelo.responder(
        action="none",
        response_text="Posso te oferecer:\n1. Segunda, 01/01 às 08:00\n"
                      "2. Terça, 02/01 às 09:00",
    )
    import agent
    agent.process_message(tenant, PHONE, "tem outro horário?")
    assert oferta(tenant) is None, (
        "oferta antiga continuou ativa depois de o modelo exibir outra lista"
    )


# ── 7. Sem IDs utilizáveis → nova oferta canônica, sem agendar ────────────────

def test_sem_ids_utilizaveis_gera_nova_oferta_canonica(tenant, modelo):
    alvo = dia_util_distante(9)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": ["lixo", "2026-99-99T99:99"]},
        response_text="Tenho vários horários!",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(alvo))
    assert not consultas(tenant, PHONE), "listar horários não pode agendar"
    gravada = exige_coerencia(tenant, reply)
    assert all(s.date() == alvo.date() for s in gravada), (
        "a nova oferta deveria ser do dia pedido"
    )


def test_list_slots_respeita_o_dia_pedido(tenant, modelo):
    pedido_em = dia_util_distante(9)
    fora = dia_util_distante(16)
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(fora), sid(pedido_em)]},
        response_text="Tenho duas opções",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(pedido_em))
    gravada = exige_coerencia(tenant, reply)
    assert all(s.date() == pedido_em.date() for s in gravada), (
        "ofereceu horário de dia diferente do pedido"
    )


def test_limite_de_opcoes_exibidas(tenant, modelo):
    alvo = dia_util_distante(9)
    todos = cal.find_slots(tenant, days_ahead=14, on_date=alvo.date())
    assert len(todos) > 6, "pré-condição: o dia tem mais de 6 horários livres"
    modelo.responder(
        action="list_slots",
        data={"offered_ids": [sid(s) for s in todos]},
        response_text="Tenho muitos horários",
    )
    import agent
    reply, *_ = agent.process_message(tenant, PHONE, pedir(alvo))
    gravada = exige_coerencia(tenant, reply)
    assert len(gravada) <= 6, f"ofertou {len(gravada)} opções de uma vez"


# ── 8. Extração da escolha numérica do texto do paciente ──────────────────────

import pytest  # noqa: E402


@pytest.mark.parametrize("texto,esperado", [
    ("1", 1),
    (" 2 ", 2),
    ("o 3", 3),
    ("quero o 1", 1),
    ("pode ser o 2", 2),
    ("prefiro a opção 3", 3),
    ("a 2ª opção", 2),
    ("opcao 4", 4),
])
def test_escolha_numerica_reconhecida(texto, esperado):
    import agent
    assert agent._parse_numeric_choice(texto) == esperado


@pytest.mark.parametrize("texto", [
    "quero agendar dia 19",            # data, não escolha
    "pode ser às 15h",                 # hora, não escolha
    "dia 19/10 às 9h",                 # data + hora
    "quero agendar na segunda-feira",  # dia da semana
    "tenho 2 filhos, dá pra marcar?",  # número solto sem marcador de opção
    "obrigada!",
    "",
])
def test_nao_confunde_data_hora_ou_numero_solto_com_escolha(texto):
    import agent
    assert agent._parse_numeric_choice(texto) is None


# ── 9. Entrega da oferta — tratada em E03 ─────────────────────────────────────
#
# Os três testes que ficavam aqui descreviam o estado intermediário de E02:
# a oferta nascia "não entregue" e, mesmo assim, resolvia a escolha numérica,
# com um aviso em log. **Essa regra foi substituída.**
#
# Em E03 a elegibilidade depende do estado de ENVIO da oferta, e os cenários
# vivem em `test_outbox_ofertas.py`:
#
#   - `test_oferta_nao_enviada_bloqueia_escolha_numerica`  (substitui o antigo
#     `test_oferta_nao_entregue_ainda_resolve_limitacao_registrada`)
#   - `test_elegibilidade_por_estado_de_envio`              (os seis estados)
#   - `test_sequencia_e_duravel_mesmo_apagando_linhas`      (substitui o antigo
#     `test_versao_da_oferta_e_monotonica`, que usava MAX(version)+1)
#   - `test_envio_incerto_nao_autoriza_reserva`,
#     `test_falha_no_envio_da_nova_mantem_a_anterior`
#
# Nenhum cenário foi perdido: todos têm sucessor com a regra correta.
