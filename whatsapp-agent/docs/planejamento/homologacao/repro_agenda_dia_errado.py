#!/usr/bin/env python3
"""Reprodução do relato: "pedi segunda-feira e fui confirmado para quarta".

Dados fictícios, banco temporário, **modelo simulado**. Nenhuma chamada de API
paga: `agent._call_llm` é substituído por um dublê. Se a substituição falhar, o
original levanta RuntimeError por falta de chave — nunca gasta crédito.

Hipóteses testadas
------------------
H1  A lista de horários oferecida ao modelo é truncada por QUANTIDADE
    (`get_available_slots(days_ahead=10, limit=24)`), não por dias. Com a agenda
    cheia nos primeiros dias, ela cobre pouquíssimos dias de calendário — então
    o dia pedido pelo paciente simplesmente NÃO ESTÁ no contexto.

H2  Sem `slot_id` válido, `_resolve_slot` cai em `slot_index`, que indexa essa
    lista. O "1" vira o primeiro horário livre do calendário — outro dia.

H3  Se o modelo inventar um `slot_id` do dia pedido (fora da lista), o
    agendamento é recusado com mensagem genérica.

Uso:
    export DATA_DIR=/tmp/homolog-agente/dados
    python repro_agenda_dia_errado.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

_dd = os.environ.get("DATA_DIR", "")
if not _dd:
    sys.exit("Defina DATA_DIR para um diretório temporário antes de rodar.")
if _dd.rstrip("/") in ("/data", "."):
    sys.exit(f"DATA_DIR={_dd!r} parece ser produção. Use um diretório temporário.")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "..", "backend"))
sys.path.insert(0, _BACKEND)

import database as db        # noqa: E402
import calendar_service as cal  # noqa: E402
import agent                 # noqa: E402

SLUG = "repro-agenda"
DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _dia(d: datetime) -> str:
    return f"{DIAS[d.weekday()]} {d.strftime('%d/%m')}"


# ── dublê do modelo ───────────────────────────────────────────────────────────

_RESPOSTA = {"json": "{}"}


def _llm_dublê(system: str, messages: list[dict], max_tokens: int = 512,
               force_json: bool = True) -> str:
    """Devolve o JSON preparado para o cenário. Nunca acessa a rede."""
    return _RESPOSTA["json"]


agent._call_llm = _llm_dublê   # noqa: E305


# ── preparação ────────────────────────────────────────────────────────────────

def preparar() -> dict:
    db.init_db()
    if not db.get_tenant(SLUG):
        db.create_tenant(slug=SLUG, name="Consultório Repro (homologação)",
                         psychologist_name="Dra. Vitoria Alencar",
                         working_hours_start=8, working_hours_end=19,
                         session_minutes=50)
    db.update_tenant(SLUG, whatsapp_provider="mock", working_days="0,1,2,3,4",
                     blocked_hours="12,13", status="active",
                     dashboard_token="token-repro-nao-secreto")
    tenant = db.get_tenant(SLUG)

    # Idempotente: limpa o que a execução anterior deste roteiro criou, para que
    # rodar duas vezes dê o mesmo resultado. Afeta SÓ este consultório fictício.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM appointments WHERE tenant_id=?", (tenant["id"],))
        conn.execute("DELETE FROM conversations WHERE tenant_id=?", (tenant["id"],))

    # Agenda cheia hoje e amanhã (situação comum num consultório em operação).
    # Preenche com pacientes fictícios para empurrar o primeiro horário livre.
    hoje = datetime.now().date()
    amanha = hoje + timedelta(days=1)
    livres = cal.get_available_slots(tenant, days_ahead=3, limit=400)
    n = 0
    for s in livres:
        if s.date() in (hoje, amanha):
            db.create_appointment(tenant["id"], f"Paciente Fictício {n+1}",
                                  f"551190000{1000+n}", s)
            n += 1
    print(f"agenda preenchida: {n} consultas fictícias hoje e amanhã")
    return db.get_tenant(SLUG)


def relatar_janela(tenant: dict) -> list[datetime]:
    hoje = datetime.now()
    prox_segunda = hoje + timedelta(days=((0 - hoje.weekday()) % 7) or 7)

    # (a) função legada, mantida de propósito para painel e indicadores:
    #     corte CRONOLÓGICO nos primeiros N horários livres.
    legado = cal.get_available_slots(tenant, days_ahead=10, limit=24)
    dias_legado = sorted({s.date() for s in legado})
    print()
    print("── H1: janela oferecida ao modelo ──")
    print(f"[legado] get_available_slots(days_ahead=10, limit=24) → {len(legado)} "
          f"horários em {len(dias_legado)} dia(s): "
          f"{', '.join(d.strftime('%a %d/%m') for d in dias_legado)}")
    print(f"[legado] a segunda pedida ({prox_segunda:%d/%m}) está nessa lista? "
          f"{'SIM' if any(s.date() == prox_segunda.date() for s in legado) else 'NÃO'}"
          "   ← era daqui que vinha o defeito")

    # (b) caminho novo do agente: consulta o DIA PEDIDO e amostra outros dias.
    do_dia = cal.find_slots(tenant, days_ahead=30, on_date=prox_segunda.date(),
                            per_day=6)
    amostra = cal.find_slots(tenant, days_ahead=14, per_day=2, max_total=16)
    novo = sorted(set(do_dia) | set(amostra))
    dias_novo = sorted({s.date() for s in novo})
    print(f"[novo]   find_slots(dia pedido) + amostra → {len(novo)} horários em "
          f"{len(dias_novo)} dia(s)")
    tem_segunda = any(s.date() == prox_segunda.date() for s in novo)
    print(f"[novo]   a segunda pedida ({prox_segunda:%d/%m}) está no contexto? "
          f"{'SIM' if tem_segunda else 'NÃO'}")
    print(f"→ H1 {'CORRIGIDA' if tem_segunda else 'AINDA ABERTA'}: o dia pedido "
          f"{'chega' if tem_segunda else 'não chega'} ao contexto do modelo.")
    return legado


def cenario(tenant: dict, titulo: str, phone: str, texto: str, json_modelo: str):
    print()
    print(f"── {titulo} ──")
    print(f'paciente: "{texto}"')
    print(f"modelo devolve: {json_modelo}")
    _RESPOSTA["json"] = json_modelo
    # Paciente sem consulta futura, para exercitar o caminho de criação.
    reply, *_resto = agent.process_message(tenant, phone, texto)
    print(f"resposta ao paciente: {reply.strip()[:160]}")
    appts = [a for a in db.get_appointments_by_phone(tenant["id"], phone)
             if not a.get("cancelled")]
    if appts:
        marcado = datetime.fromisoformat(appts[0]["scheduled_at"])
        print(f">>> GRAVADO NA AGENDA: {_dia(marcado)} às {marcado.strftime('%H:%M')}")
        return marcado
    print(">>> NADA GRAVADO NA AGENDA")
    return None


def main() -> None:
    tenant = preparar()
    slots = relatar_janela(tenant)
    if not slots:
        sys.exit("sem horários livres — ajuste os dados fictícios")

    hoje = datetime.now()
    prox_segunda = hoje + timedelta(days=((0 - hoje.weekday()) % 7) or 7)
    pedido = "quero agendar na segunda-feira de manhã"

    # H2 — modelo sem slot_id: manda a posição que o PACIENTE viu ("o 1").
    m = cenario(
        tenant, "H2: modelo devolve slot_index (sem slot_id)",
        "5511911111111", pedido,
        '{"intent":"schedule","action":"create","data":'
        '{"patient_name":"Aurora Belmonte","slot_index":1},'
        '"response_text":"Prontinho! Marquei sua sessão de segunda-feira."}',
    )
    if m:
        pediu_segunda = m.weekday() == 0
        print(f"→ H2 {'REFUTADA' if pediu_segunda else 'CONFIRMADA'}: paciente pediu "
              f"segunda-feira e o sistema gravou {DIAS[m.weekday()]}.")

    # H3 — modelo manda slot_id da segunda pedida, que não está na lista.
    sid = prox_segunda.replace(hour=9, minute=0).strftime("%Y-%m-%dT%H:%M")
    cenario(
        tenant, "H3: modelo devolve slot_id do dia pedido (fora da janela)",
        "5511922222222", pedido,
        '{"intent":"schedule","action":"create","data":'
        f'{{"patient_name":"Ivo Cardinal","slot_id":"{sid}"}},'
        '"response_text":"Prontinho! Marquei para segunda-feira às 09:00."}',
    )

    # Controle — slot_id válido, dentro da janela: tem de acertar.
    ok_id = slots[3].strftime("%Y-%m-%dT%H:%M")
    m = cenario(
        tenant, "Controle: slot_id válido (dentro da janela)",
        "5511933333333", "pode ser esse horário",
        '{"intent":"schedule","action":"create","data":'
        f'{{"patient_name":"Nadir Quintela","slot_id":"{ok_id}"}},'
        '"response_text":"Prontinho! Marquei sua sessão."}',
    )
    if m:
        print(f"→ controle {'OK' if m == slots[3] else 'FALHOU'}: "
              f"esperado {_dia(slots[3])} {slots[3].strftime('%H:%M')}")


if __name__ == "__main__":
    main()
