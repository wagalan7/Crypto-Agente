from __future__ import annotations
import json
import re
from datetime import datetime

import anthropic

import config
import database as db
import calendar_service as cal
from models import AgentResponse, Action

_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _build_system_prompt(tenant: dict) -> str:
    return f"""Você é um assistente de consultório de psicologia via WhatsApp.

FUNÇÃO:
Gerenciar agendamentos, confirmações, remarcações e atendimento inicial.

REGRAS:
- Seja breve, educado e acolhedor
- Linguagem simples e humanizada
- Não dar diagnóstico ou orientação clínica
- Não compartilhar dados de terceiros
- Se dúvida clínica ou situação sensível → encaminhar para humano
- Não insistir se o paciente não responder

HORÁRIO DE FUNCIONAMENTO: Seg–Sex, {tenant['working_hours_start']:02d}:00–{tenant['working_hours_end']:02d}:00
CONSULTÓRIO: {tenant['name']}
PSICÓLOGA: {tenant['psychologist_name']}

CAPACIDADES:
1. Confirmar consultas (24h antes)
2. Agendar novos pacientes
3. Remarcar consultas
4. Listar horários disponíveis (fornecidos no contexto)
5. Atualizar agenda (sem conflitos)

DADOS A COLETAR:
- nome completo
- disponibilidade / preferência de horário

IMPORTANTE:
- NUNCA inventar horários — use apenas os horários fornecidos no contexto
- Sempre usar dados reais da agenda fornecidos no contexto
- A IA NÃO executa ações diretamente → apenas decide qual ação tomar

FORMATO DE RESPOSTA (OBRIGATÓRIO — responda APENAS com JSON válido, sem markdown):

{{
  "intent": "confirm|schedule|reschedule|new_patient|other",
  "action": "none|list_slots|create|update|confirm",
  "data": {{}},
  "response_text": ""
}}

CAMPOS data ESPERADOS POR AÇÃO:
- list_slots: {{}}
- create: {{"patient_name": "...", "slot_index": 0}}
- update: {{"appointment_id": 1, "slot_index": 0}}
- confirm: {{"appointment_id": 1}}
- none: {{}}

EXEMPLOS DE TOM:

Confirmação:
"Olá! Tudo bem? 😊 Confirmando sua sessão amanhã às [hora]. Você pode confirmar presença?"

Reagendamento:
"Sem problemas 😊 Tenho estes horários disponíveis:\\n- [1]\\n- [2]\\n- [3]\\nQual prefere?"

Novo paciente:
"Olá! Seja bem-vindo(a) 😊 Posso te ajudar com seu agendamento. Qual é o seu nome completo?"
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON in response: {text[:200]}")


def _build_context(tenant: dict, phone: str, offered_slots: list) -> str:
    tenant_id = tenant["id"]
    lines = []

    appt = cal.get_next_appointment(tenant_id, phone)
    if appt:
        lines.append(
            f"CONSULTA AGENDADA: {cal.format_appointment(appt)} "
            f"(id={appt['id']}, confirmado={'Sim' if appt['confirmed'] else 'Não'})"
        )
    else:
        lines.append("CONSULTA AGENDADA: nenhuma")

    if offered_slots:
        lines.append("HORÁRIOS DISPONÍVEIS (use apenas estes):")
        for i, s in enumerate(cal.format_slots(offered_slots), 1):
            lines.append(f"  {i}. {s}")

    lines.append(f"DATA/HORA ATUAL: {datetime.now().strftime('%A, %d/%m/%Y %H:%M')}")
    return "\n".join(lines)


def process_message(tenant: dict, phone: str, text: str) -> tuple[str, AgentResponse]:
    tenant_id = tenant["id"]
    db.save_message(tenant_id, phone, "user", text)

    offered_slots = cal.get_available_slots(tenant, days_ahead=7, limit=6)
    context = _build_context(tenant, phone, offered_slots)

    history = db.get_conversation_history(tenant_id, phone, limit=8)
    messages = history + [
        {"role": "user", "content": f"[CONTEXTO]\n{context}\n\n[MENSAGEM DO PACIENTE]\n{text}"}
    ]

    response = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=_build_system_prompt(tenant),
        messages=messages,
    )

    raw = response.content[0].text
    parsed = _extract_json(raw)
    agent_resp = AgentResponse(**parsed)

    reply = _execute_action(tenant_id, agent_resp, offered_slots)
    db.save_message(tenant_id, phone, "assistant", reply)
    return reply, agent_resp


def _execute_action(tenant_id: int, resp: AgentResponse, offered_slots: list) -> str:
    action = resp.action
    data = resp.data
    text = resp.response_text

    if action == Action.list_slots:
        return text

    if action == Action.create:
        idx = data.get("slot_index", 0)
        name = data.get("patient_name", "Paciente")
        if 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            if not db.is_slot_taken(tenant_id, slot):
                db.create_appointment(tenant_id, name, "", slot)
                formatted = cal.format_slots([slot])[0]
                return text.replace("[slot]", formatted).replace("[hora]", formatted)
        return "Desculpe, não consegui realizar o agendamento. Pode escolher outro horário? 😊"

    if action == Action.update:
        appt_id = data.get("appointment_id")
        idx = data.get("slot_index", 0)
        if appt_id and 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            if not db.is_slot_taken(tenant_id, slot):
                db.update_appointment(tenant_id, appt_id, slot)
                formatted = cal.format_slots([slot])[0]
                return text.replace("[slot]", formatted).replace("[hora]", formatted)
        return "Não consegui remarcar. Pode escolher outro horário? 😊"

    if action == Action.confirm:
        appt_id = data.get("appointment_id")
        if appt_id:
            db.confirm_appointment(tenant_id, appt_id)

    return text
