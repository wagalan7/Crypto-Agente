from __future__ import annotations
import json
import logging
import re
from datetime import datetime

import config
import database as db
import calendar_service as cal
import google_calendar_service as gcal
import caldav_service as caldav_svc
from models import AgentResponse, Action

logger = logging.getLogger(__name__)

_groq_client = None
_anthropic_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=config.GROQ_API_KEY)
    return _groq_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _call_llm(system: str, messages: list[dict], max_tokens: int = 512) -> str:
    """Chama Groq (primário, gratuito). Fallback para Anthropic se necessário."""
    if config.GROQ_API_KEY:
        try:
            client = _get_groq_client()
            msgs = [{"role": "system", "content": system}] + messages
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=msgs,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"[groq] Erro: {e} — tentando Anthropic como fallback")

    if config.ANTHROPIC_API_KEY:
        import anthropic
        client = _get_anthropic_client()
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return resp.content[0].text

    raise RuntimeError("Nenhuma chave de API configurada (GROQ_API_KEY ou ANTHROPIC_API_KEY)")


def _build_system_prompt(tenant: dict) -> str:
    if tenant.get('pix_key'):
        pix_section = (
            "PIX PARA PAGAMENTO:\n"
            f"- Chave PIX: {tenant['pix_key']}\n"
            f"- Titular: {tenant['pix_name']}\n"
            "- Quando perguntarem sobre pagamento ou PIX, forneça essas informações exatas.\n\n"
        )
    else:
        pix_section = ""

    return f"""Você é um assistente de consultório de psicologia via WhatsApp.

FUNÇÃO:
Gerenciar agendamentos, confirmações, remarcações e primeiro contato com novos pacientes.

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

IMPORTANTE: use o nome da psicóloga EXATAMENTE como fornecido acima, sem adicionar títulos como "Dra." ou "Dr.".

{pix_section}FLUXO PARA NOVO PACIENTE (quando "CONSULTA AGENDADA: nenhuma" e NÃO há "PACIENTE CONHECIDO" no contexto):
1. Dê boas-vindas de forma calorosa
2. Peça o nome completo
3. Após receber o nome: informe que {tenant['psychologist_name']} vai entrar em contato em breve para explicar o processo, o método e os próximos passos
4. NÃO ofereça horários — NÃO agende — NÃO explique o método
5. Use action "none" e intent "new_patient"
6. Coloque o nome do paciente em data: {{"patient_name": "..."}} assim que souber

PACIENTE CONHECIDO SEM CONSULTA FUTURA (quando aparecer "PACIENTE CONHECIDO" no contexto):
- NÃO peça o nome — você já sabe quem é
- Cumprimente pelo nome e pergunte como pode ajudar
- Pode oferecer horários se pedir agendamento
- Trate como paciente retornante, não como novo paciente

CAPACIDADES (apenas para pacientes JÁ CADASTRADOS com consulta):
1. Confirmar consultas (24h antes)
2. Remarcar consultas
3. Listar horários disponíveis (fornecidos no contexto)
4. Atualizar agenda (sem conflitos)

CONFIRMAÇÃO — REGRAS CRÍTICAS:
- Se o paciente responder com QUALQUER expressão positiva APÓS receber mensagem de confirmação → use action: "confirm" e data: {{"appointment_id": ID_DA_CONSULTA}}
- Exemplos que SÃO confirmação: "SIM", "sim", "siim", "siimm", "simm", "ok", "confirmo", "confirmado", "pode ser", "tô lá", "estarei lá", "claro", "com certeza", "sem dúvidas", "com certeza", "pode", "vai", "vou estar", "estarei", "confirmar sim", "Confirmo simm", "pode sim", "ótimo", "tudo bem", "perfeito", "combinado"
- NUNCA reenvie a pergunta "Você pode confirmar presença?" se o paciente já respondeu com algo positivo
- NUNCA use action "none" quando o paciente estiver confirmando presença
- NUNCA envie mensagem de confirmação proativamente durante conversa normal — confirmações automáticas são enviadas apenas pelo sistema agendador, NUNCA por você durante um chat casual
- Se a consulta for HOJE: responda "Ótimo! ✅ Presença confirmada. Até mais tarde! 😊" — NUNCA diga "Até amanhã" para sessões do dia atual
- Se a consulta for AMANHÃ: responda "Ótimo! ✅ Presença confirmada. Até amanhã! 😊"
- Se a consulta for outro dia: responda "Ótimo! ✅ Presença confirmada. Até [dia da semana]! 😊"
- Para saber se é hoje ou amanhã, compare a data da consulta com DATA/HORA ATUAL fornecida no contexto
- REGRA ABSOLUTA: "Sem dúvidas" + "Sim" = CONFIRMAR. NUNCA interpretar como pedido de remarcação.

MENSAGENS CASUAIS — REGRA CRÍTICA:
- Se o paciente mandar uma mensagem casual, comentário, emoji, cumprimento ou qualquer mensagem que NÃO seja sobre agendamento → responda de forma natural e acolhedora, action: "none"
- NUNCA interprete mensagem casual como pedido de confirmação de consulta
- NUNCA envie proativamente mensagem de confirmação em resposta a uma mensagem casual
- Exemplos de mensagens casuais: "Obrigada!", "Até mais!", "Foi ótimo!", "Bom dia!", "Tô bem", "Até logo", "oii bru" → responda brevemente e com calor, NÃO pergunte sobre confirmação
- EXCEÇÃO: se a mensagem casual vier LOGO APÓS uma mensagem de confirmação enviada pelo sistema (contexto mostra consulta não confirmada), então "siimm", "sim", "ok" etc. = ação "confirm"

REMARCAÇÃO — REGRAS CRÍTICAS:
- Só use action "update" quando o paciente EXPLICITAMENTE pedir para remarcar com uma frase clara: "quero remarcar", "preciso mudar o horário", "não posso nesse dia"
- "Sim", "ok", "confirmo" e variantes positivas NUNCA devem acionar remarcação
- Após listar horários disponíveis, só remarque quando o paciente ESCOLHER um horário específico (ex: "quero o 2", "pode ser segunda às 14h")
- NUNCA remarque automaticamente sem o paciente confirmar o novo horário escolhido

SESSÃO PRÓXIMA (quando=EM BREVE ou HOJE):
- Se o contexto mostrar "quando=EM BREVE — em X minutos (HOJE)" ou "quando=HOJE", a sessão é hoje
- Se o paciente mandar qualquer mensagem (ex: "to disponivel", "cheguei", "a caminho") e a sessão for HOJE → entenda como check-in pré-sessão
- Responda acolhendo: "Ótimo! Te esperamos daqui a pouco 😊" ou "Perfeito, a {tenant['psychologist_name']} já está te aguardando!"
- NUNCA diga "Até amanhã" se quando=HOJE ou quando=EM BREVE
- Se quando=JÁ PASSOU → sessão já ocorreu, trate mensagem como pós-sessão normalmente

IMPORTANTE:
- NUNCA inventar horários — use apenas os horários fornecidos no contexto
- Sempre usar dados reais da agenda fornecidos no contexto
- A IA NÃO executa ações diretamente → apenas decide qual ação tomar

SITUAÇÕES ESPECIAIS:

Atraso:
- Se o paciente avisar que vai se atrasar até 25 minutos: aceite com tranquilidade, confirme que a sessão é HOJE e no mesmo horário.
- NUNCA diga "até amanhã" ou "te vejo amanhã" para sessões de hoje — isso confunde o paciente.
- Exemplo: "Sem problema! 😊 Te esperamos hoje às [hora], pode vir com calma."
- Se o atraso for maior que 25 minutos: sugira remarcar gentilmente.

Comprovante de pagamento / PIX:
- Se o paciente enviar um comprovante de transferência, pagamento ou documento: agradeça e informe que a nota fiscal será enviada em breve.
- Exemplo: "Obrigada pelo pagamento! 😊 Recebi o comprovante. Em breve enviarei a nota fiscal. Até a sessão!"

Mensagem de urgência / crise emocional:
- Se o paciente expressar sofrimento intenso, crise, pensamentos de se machucar ou pedir ajuda urgente: responda com acolhimento, diga que a {tenant['psychologist_name']} foi notificada e vai entrar em contato o quanto antes.
- NUNCA minimize o sofrimento ou dê orientação clínica.
- Exemplo: "Fico feliz que você entrou em contato 💙 Sua mensagem chegou para a {tenant['psychologist_name']} e ela vai te responder o quanto antes. Você não está sozinho(a). 🌸"
- Use action "none" e intent "other"

FORMATO DE RESPOSTA (OBRIGATÓRIO — responda APENAS com JSON válido, sem markdown):

{{
  "intent": "confirm|schedule|reschedule|new_patient|other",
  "action": "none|list_slots|create|update|confirm",
  "data": {{}},
  "response_text": ""
}}

CAMPOS data ESPERADOS POR AÇÃO:
- list_slots: {{}}
- create: {{"patient_name": "...", "slot_index": 1}}   ← número exibido ao paciente (1 = primeiro, 2 = segundo, etc.)
- update: {{"appointment_id": 1, "slot_index": 1}}     ← mesmo padrão: número do item na lista
- confirm: {{"appointment_id": 1}}
- none: {{}}

REGRA CRÍTICA — slot_index:
Use EXATAMENTE o número que aparece na lista de horários (1, 2, 3...).
Se o paciente escolheu "o 5" ou "o horário 5", use slot_index: 5.
NUNCA subtraia 1 ou faça qualquer conversão — use o número exato do display.

SITUAÇÕES ESPECIAIS:

Paciente avisando atraso:
- Aceite o atraso com tranquilidade SE for de até 25 minutos
- NUNCA use expressões como "até amanhã" ou "te vejo amanhã" — isso confunde o paciente sobre a data da sessão
- Confirme a sessão de hoje, deixando claro que é HOJE mesmo
- Exemplo: "Sem problema! 😊 Te esperamos hoje às [hora], pode chegar."
- Se o atraso for maior que 25 minutos, sugira remarcar gentilmente

Paciente enviando comprovante de pagamento (PIX, transferência, etc.):
- Agradeça de forma calorosa
- Informe que a nota fiscal será enviada em breve
- action: "none"
- Exemplo: "Recebido! 🙏 Obrigada pelo pagamento. A nota fiscal será enviada em breve para você."

EXEMPLOS DE TOM:

Confirmação:
"Olá! Tudo bem? 😊 Confirmando sua sessão amanhã às [hora]. Você pode confirmar presença?"

Atraso pequeno:
"Sem problema! 😊 Te esperamos hoje às [hora], pode chegar."

Comprovante de pagamento:
"Recebido! 🙏 Obrigada pelo pagamento. A nota fiscal será enviada em breve."

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
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    tenant_id = tenant["id"]
    lines = []

    appt = cal.get_next_appointment(tenant_id, phone)
    if appt:
        appt_dt = datetime.fromisoformat(appt["scheduled_at"])
        now_br = datetime.now(_TZ).replace(tzinfo=None)
        diff_min = (appt_dt - now_br).total_seconds() / 60

        if diff_min < 0:
            timing = "JÁ PASSOU (sessão já ocorreu hoje)"
        elif diff_min <= 30:
            timing = f"EM BREVE — em {int(diff_min)} minutos (HOJE)"
        elif diff_min <= 120:
            timing = f"HOJE — em {int(diff_min/60)}h{int(diff_min%60)}min"
        elif diff_min <= 1440:
            timing = "HOJE"
        elif diff_min <= 2880:
            timing = "AMANHÃ"
        else:
            timing = f"em {int(diff_min/1440)} dias"

        lines.append(
            f"CONSULTA AGENDADA: {cal.format_appointment(appt)} "
            f"(id={appt['id']}, confirmado={'Sim' if appt['confirmed'] else 'Não'}, quando={timing})"
        )
    else:
        lines.append("CONSULTA AGENDADA: nenhuma")
        # Verificar se é paciente conhecido mesmo sem consulta futura
        patient = db.get_patient(tenant_id, phone)
        if patient and patient.get("name"):
            lines.append(f"PACIENTE CONHECIDO: {patient['name']} (já cadastrado, sem consulta futura agendada)")
        else:
            # Checar histórico de agendamentos passados
            with db.get_conn() as conn:
                past = conn.execute(
                    "SELECT patient_name FROM appointments WHERE tenant_id=? AND phone=? ORDER BY scheduled_at DESC LIMIT 1",
                    (tenant_id, phone)
                ).fetchone()
            if past:
                lines.append(f"PACIENTE CONHECIDO: {past['patient_name']} (já foi paciente, sem consulta futura agendada)")

    if offered_slots:
        lines.append("HORÁRIOS DISPONÍVEIS (use apenas estes):")
        for i, s in enumerate(cal.format_slots(offered_slots), 1):
            lines.append(f"  {i}. {s}")

    lines.append(f"DATA/HORA ATUAL: {datetime.now(_TZ).strftime('%A, %d/%m/%Y %H:%M')}")
    return "\n".join(lines)


def process_message(tenant: dict, phone: str, text: str) -> tuple[str, AgentResponse]:
    """
    Processa uma mensagem e retorna (reply, agent_response).
    A publicação de eventos SSE é feita pelo chamador (main.py) no contexto async.
    """
    tenant_id = tenant["id"]
    db.save_message(tenant_id, phone, "user", text)

    offered_slots = cal.get_available_slots(tenant, days_ahead=10, limit=6)
    context = _build_context(tenant, phone, offered_slots)

    history = db.get_conversation_history(tenant_id, phone, limit=8)
    messages = history + [
        {"role": "user", "content": f"[CONTEXTO]\n{context}\n\n[MENSAGEM DO PACIENTE]\n{text}"}
    ]

    raw = _call_llm(
        system=_build_system_prompt(tenant),
        messages=messages,
        max_tokens=512,
    )
    parsed = _extract_json(raw)
    agent_resp = AgentResponse(**parsed)

    reply, event = _execute_action(tenant, agent_resp, offered_slots, phone)
    db.save_message(tenant_id, phone, "assistant", reply)
    return reply, agent_resp, event


def _execute_action(tenant: dict, resp: AgentResponse,
                    offered_slots: list, phone: str = "") -> tuple[str, dict | None]:
    """Executa a ação e retorna (reply_text, evento_opcional)."""
    tenant_id = tenant["id"]
    action = resp.action
    data = resp.data
    text = resp.response_text

    if action == Action.list_slots:
        return text, {"type": "new_message", "data": {"phone": phone, "intent": resp.intent}}

    if action == Action.create:
        # slot_index vem do LLM como número do display (1-based).
        # Converter para 0-based antes de indexar offered_slots.
        idx_raw = data.get("slot_index", 1)
        idx = int(idx_raw) - 1  # 1-based → 0-based
        name = data.get("patient_name", "Paciente")
        if 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            if not db.is_slot_taken(tenant_id, slot):
                appt_id = db.create_appointment(tenant_id, name, phone, slot)
                duration = tenant.get("session_minutes", 50)
                # Sincronizar com Google Calendar (se conectado)
                try:
                    event_id = gcal.create_event(tenant, name, slot.isoformat(), duration)
                    if event_id:
                        db.set_appointment_google_event_id(appt_id, event_id)
                except Exception as e:
                    logger.warning(f"[gcal] create_event falhou: {e}")
                # Sincronizar com CalDAV (se configurado)
                try:
                    caldav_uid = caldav_svc.create_event(tenant, name, slot.isoformat(), duration)
                    if caldav_uid:
                        # Reutilizamos google_event_id para CalDAV quando Google não está conectado
                        if not tenant.get("google_refresh_token"):
                            db.set_appointment_google_event_id(appt_id, caldav_uid)
                except Exception as e:
                    logger.warning(f"[caldav] create_event falhou: {e}")
                formatted = cal.format_slots([slot])[0]
                # Forçar o horário correto na resposta — o LLM pode ter escrito
                # um horário diferente no texto. Substituímos qualquer menção
                # ao slot pelo horário real armazenado.
                reply = text.replace("[slot]", formatted).replace("[hora]", formatted)
                logger.info(f"[{tenant['slug']}] Agendamento criado: {name} | slot_index={idx_raw}(raw)→{idx}(0based) | slot={formatted}")
                event = {"type": "new_appointment", "data": {"patient_name": name, "slot": formatted, "phone": phone}}
                return reply, event
        logger.warning(f"[{tenant['slug']}] slot_index inválido: {idx_raw}(raw)→{idx}(0based) | total slots={len(offered_slots)}")
        return "Desculpe, não consegui realizar o agendamento. Pode escolher outro horário? 😊", None

    if action == Action.update:
        appt_id = data.get("appointment_id")
        idx_raw = data.get("slot_index", 1)
        idx = int(idx_raw) - 1  # 1-based (display) → 0-based
        if appt_id and 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            if not db.is_slot_taken(tenant_id, slot):
                # Buscar google_event_id antes de atualizar
                appt = db.get_appointment_by_id(tenant_id, appt_id)
                db.update_appointment(tenant_id, appt_id, slot)
                # Sincronizar com Google Calendar
                try:
                    if appt and appt.get("google_event_id") and tenant.get("google_refresh_token"):
                        gcal.update_event(tenant, appt["google_event_id"],
                                          appt.get("patient_name", "Paciente"),
                                          slot.isoformat(), tenant.get("session_minutes", 50))
                except Exception as e:
                    logger.warning(f"[gcal] update_event falhou: {e}")
                # Sincronizar com CalDAV (se configurado e Google não estiver ativo)
                try:
                    if appt and appt.get("google_event_id") and not tenant.get("google_refresh_token"):
                        caldav_svc.update_event(tenant, appt["google_event_id"],
                                                appt.get("patient_name", "Paciente"),
                                                slot.isoformat(), tenant.get("session_minutes", 50))
                except Exception as e:
                    logger.warning(f"[caldav] update_event falhou: {e}")
                formatted = cal.format_slots([slot])[0]
                reply = text.replace("[slot]", formatted).replace("[hora]", formatted)
                return reply, {"type": "new_message", "data": {"phone": phone, "intent": "reschedule"}}
        return "Não consegui remarcar. Pode escolher outro horário? 😊", None

    if action == Action.confirm:
        appt_id = data.get("appointment_id")
        if appt_id:
            db.confirm_appointment(tenant_id, appt_id)

    return text, {"type": "new_message", "data": {"phone": phone, "intent": resp.intent}}
