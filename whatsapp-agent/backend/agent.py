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
from models import AgentResponse, Action, Intent

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


def _call_llm(system: str, messages: list[dict], max_tokens: int = 512,
              force_json: bool = True) -> str:
    """Chama Groq (primário, gratuito). Fallback para Anthropic se necessário.

    Quando force_json=True, ativa JSON mode na Groq (response_format=json_object)
    pra eliminar respostas em prosa que faziam o parser cair no 'não entendi'.
    """
    if config.GROQ_API_KEY:
        try:
            client = _get_groq_client()
            msgs = [{"role": "system", "content": system}] + messages
            kwargs = dict(
                model="llama-3.3-70b-versatile",
                messages=msgs,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            if force_json:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"[groq] Erro: {e} — tentando Anthropic como fallback")

    if config.ANTHROPIC_API_KEY:
        import anthropic
        client = _get_anthropic_client()
        # Para Anthropic, força JSON via prefill do assistant
        msgs = list(messages)
        if force_json:
            msgs = msgs + [{"role": "assistant", "content": "{"}]
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            system=system,
            messages=msgs,
        )
        text = resp.content[0].text
        if force_json and not text.lstrip().startswith("{"):
            text = "{" + text
        return text

    raise RuntimeError("Nenhuma chave de API configurada (GROQ_API_KEY ou ANTHROPIC_API_KEY)")


async def classify_image(image_url: str) -> str:
    """Classifica uma imagem como 'receipt' (comprovante PIX/transferência/boleto)
    ou 'other'. Usa Claude vision. Retorna 'unknown' em caso de erro/sem URL."""
    if not image_url or not config.ANTHROPIC_API_KEY:
        return "unknown"
    try:
        import base64, httpx
        # Baixa a imagem (alguns provedores exigem auth — Z-API em geral entrega URL aberta)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(image_url)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if content_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                content_type = "image/jpeg"
            data = base64.standard_b64encode(r.content).decode("ascii")

        client = _get_anthropic_client()
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=20,
            system=("Você classifica imagens recebidas no WhatsApp de um consultório. "
                    "Responda APENAS com uma palavra: 'receipt' se a imagem for um comprovante "
                    "de pagamento (PIX, transferência bancária, boleto pago, recibo, screenshot "
                    "de app de banco mostrando transação concluída). Responda 'other' para "
                    "qualquer outra coisa (foto pessoal, screenshot de rede social, documento "
                    "não-financeiro, foto de cenário, print de conversa, etc)."),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": data}},
                    {"type": "text", "text": "Esta imagem é um comprovante de pagamento? Responda 'receipt' ou 'other'."},
                ],
            }],
        )
        out = (resp.content[0].text or "").strip().lower()
        return "receipt" if "receipt" in out else "other"
    except Exception as e:
        logger.warning(f"[classify_image] Falha: {e}")
        return "unknown"


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

{pix_section}REGRA ABSOLUTA — IDENTIFICAÇÃO DO PACIENTE (verifique ANTES de qualquer resposta):
- Se o CONTEXTO contém "CONSULTA AGENDADA:" com um id (ex: "id=233") → PACIENTE JÁ CADASTRADO. NUNCA pergunte o nome. NUNCA dê boas-vindas de novo paciente. Cumprimente normalmente e ajude.
- Se o CONTEXTO contém "PACIENTE CONHECIDO:" → PACIENTE JÁ CADASTRADO. NUNCA pergunte o nome.
- Use intent "new_patient" SOMENTE quando o contexto disser literalmente "CONSULTA AGENDADA: nenhuma" E não tiver linha "PACIENTE CONHECIDO".

FLUXO PARA NOVO PACIENTE (somente se as 2 condições acima forem verdadeiras):
1. Dê boas-vindas de forma calorosa
2. Peça o nome completo
3. Após receber o nome: informe que {tenant['psychologist_name']} vai entrar em contato em breve para explicar o processo, o método e os próximos passos
4. NÃO ofereça horários — NÃO agende — NÃO explique o método
5. Use action "none" e intent "new_patient"
6. Coloque o nome do paciente em data: {{"patient_name": "..."}} assim que souber

PACIENTE COM CONSULTA AGENDADA (linha "CONSULTA AGENDADA: ... id=..."):
- Já é paciente conhecido. NUNCA peça nome.
- Cumprimente educadamente ("Olá!", "Oi!", "Tudo bem?") sem boas-vindas de novo paciente
- Se a mensagem for vaga ("oi", "olá"), responda algo como "Olá! Tudo bem? Posso ajudar com sua consulta de [DIA]?"
- Pode confirmar / remarcar / listar horários

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

NOME DO PACIENTE — REGRA ABSOLUTA:
- Use SOMENTE o nome que aparece na linha "NOME DO PACIENTE:" ou "PACIENTE CONHECIDO:" do CONTEXTO.
- NUNCA invente um nome. NUNCA use um nome aleatório (ex: "Michelle", "Maria", "João") sem ele aparecer no contexto.
- Se a linha NOME DO PACIENTE: existe → use APENAS o primeiro nome dela ao cumprimentar.
- Se a linha disser "(sem nome cadastrado)" → NÃO use nome algum, cumprimente sem nome: "Olá! 😊".
- NUNCA confunda o nome do paciente com o nome da psicóloga.

CUMPRIMENTOS E MENSAGENS SIMPLES — REGRA CRÍTICA:
- "Bom dia", "Boa tarde", "Boa noite", "Oi", "Olá", "Tudo bem?" e variações = cumprimento.
- SEMPRE responda cumprimentos com calor, mesmo sem entender intenção: "Olá! Tudo bem? 😊 Posso te ajudar com sua consulta de [DIA]?" ou "Bom dia! 😊 Tudo ótimo. Como posso ajudar?"
- NUNCA responda "não entendi" para cumprimento, mesmo curto. Se a mensagem for só saudação, pergunte gentilmente como pode ajudar.

FLUXO DE AGENDAMENTO / REMARCAÇÃO — REGRA CRÍTICA (LEIA COM ATENÇÃO):
- ANTES de listar horários, SEMPRE pergunte primeiro qual a melhor disponibilidade do paciente.
- Quando o paciente disser apenas "quero agendar", "gostaria de marcar", "preciso remarcar", "quero outro horário" SEM citar dia OU período → NÃO liste horários ainda.
  • Use action: "none" e intent: "schedule" (ou "reschedule") e pergunte de forma acolhedora:
    "Claro! 😊 Qual seria a melhor disponibilidade pra você — algum dia da semana ou período (manhã, tarde ou noite) que prefere? Assim já te mando opções que cabem na sua rotina."
- SÓ liste horários (action: "list_slots") quando o paciente JÁ tiver informado preferência de dia, período ou horário (ex: "prefiro terça à tarde", "qualquer dia de manhã", "pode ser quinta", "qualquer horário", "tanto faz", "não tenho preferência").
- Se o paciente disser explicitamente "qualquer horário", "tanto faz", "qualquer dia" → aí sim ofereça uma seleção variada (manhã + tarde + noite quando disponíveis).
- EXCEÇÃO: se o paciente já disse no MESMO pedido o que quer (ex: "quero agendar terça à tarde", "remarcar pra quinta de manhã") → pule a pergunta e já ofereça horários filtrados.
- Quando o histórico recente mostrar que VOCÊ já perguntou a disponibilidade e o paciente acabou de responder com dia/período → AGORA liste os horários filtrados. NUNCA pergunte duas vezes seguidas.

FILTRO DE PERÍODO AO OFERECER HORÁRIOS — REGRA CRÍTICA:
- Quando enfim for listar horários, observe o período que o paciente pediu: "manhã", "tarde", "noite", "fim do dia", "início da tarde", etc.
- Manhã = 06:00–11:59 | Tarde = 12:00–17:59 | Noite = 18:00–23:59
- Filtre a lista HORÁRIOS DISPONÍVEIS do contexto e ofereça APENAS os que cabem no período/dia pedidos. Mostre no máximo 4–5 opções para não sobrecarregar.
- Se NÃO houver horário no período pedido, diga claramente: "Infelizmente não tenho horários disponíveis na [parte do dia] pedida. Posso oferecer [outros períodos com horários]?" — NUNCA finja oferecer "tarde" quando só tem manhã.
- Depois de mostrar as opções filtradas, ofereça abrir o leque caso o paciente queira: "Se preferir, posso te mostrar também os outros dias/horários disponíveis. 😊"

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

Pedido de agendamento/remarcação SEM preferência (1ª resposta):
"Claro! 😊 Qual seria a melhor disponibilidade pra você — algum dia da semana ou período (manhã, tarde ou noite) que prefere? Assim já te mando opções que cabem na sua rotina."

Reagendamento APÓS o paciente informar preferência (ex: "terça à tarde"):
"Perfeito! 😊 Para terça à tarde, tenho estas opções:\\n1. [horário]\\n2. [horário]\\n3. [horário]\\nQual prefere? Se quiser, posso abrir o leque para outros dias também."

Novo paciente:
"Olá! Seja bem-vindo(a) 😊 Posso te ajudar com seu agendamento. Qual é o seu nome completo?"
"""


def _extract_json(text: str) -> dict:
    """Extrai JSON da resposta do LLM, tolerante a code fences e prefixos."""
    text = (text or "").strip()
    # Remove cercas de código markdown (```json ... ``` ou ``` ... ```)
    if text.startswith("```"):
        # Tira primeira linha (``` ou ```json) e última (```)
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Tenta parse direto
    try:
        return json.loads(text)
    except Exception:
        pass
    # Procura o primeiro `{` e tenta achar `}` correspondente (balanceando)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON in response: {text[:200]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"Unbalanced JSON in response: {text[:200]}")


_DIAS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
            "sexta-feira", "sábado", "domingo"]
_MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
             "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def _build_context(tenant: dict, phone: str, offered_slots: list) -> str:
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    tenant_id = tenant["id"]
    lines = []

    appt = cal.get_next_appointment(tenant_id, phone)
    if appt:
        appt_dt = datetime.fromisoformat(appt["scheduled_at"])
        now_br = datetime.now(_TZ).replace(tzinfo=None)
        diff_min = (appt_dt - now_br).total_seconds() / 60

        # Usa comparação por data de calendário (não por minutos) para evitar
        # marcar "amanhã 9h" como "HOJE" quando consultado às 10h da véspera.
        today_d = now_br.date()
        appt_d = appt_dt.date()
        delta_days = (appt_d - today_d).days
        if diff_min < 0:
            timing = "JÁ PASSOU (sessão já ocorreu hoje)"
        elif diff_min <= 30:
            timing = f"EM BREVE — em {int(diff_min)} minutos (HOJE)"
        elif delta_days == 0:
            timing = f"HOJE — em {int(diff_min/60)}h{int(diff_min%60)}min"
        elif delta_days == 1:
            timing = "AMANHÃ"
        else:
            timing = f"em {delta_days} dias"

        patient_name = (appt.get("patient_name") or "").strip() or "(sem nome cadastrado)"
        lines.append(f"NOME DO PACIENTE: {patient_name}")
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

    now = datetime.now(_TZ)
    amanha = now + timedelta(days=1)
    hoje_str = f"{_DIAS_PT[now.weekday()]}, {now.day} de {_MESES_PT[now.month-1]} de {now.year}"
    amanha_str = f"{_DIAS_PT[amanha.weekday()]}, {amanha.strftime('%d/%m')}"
    lines.append(
        f"DATA/HORA ATUAL: {hoje_str} — {now.strftime('%H:%M')} "
        f"(HOJE é {_DIAS_PT[now.weekday()]}; AMANHÃ é {amanha_str}). "
        "Sempre use ESTA referência ao falar de dias da semana — NUNCA invente."
    )
    return "\n".join(lines)


_GREETING_TOKENS = {
    "oi", "olá", "ola", "ooi", "oii", "oiii", "oie", "opa", "eai", "e ai", "ei",
    "bom dia", "boa tarde", "boa noite",
    "tudo bem", "tudo bem?", "td bem", "td bm",
    "boa", "blz", "beleza",
}

def _is_simple_greeting(text: str) -> bool:
    """Detecta saudação curta sem outra intenção embutida.
    Critério: até 25 chars, sem dígitos, e composto apenas por uma das
    expressões de cumprimento (com emojis/pontuação opcionais)."""
    if not text:
        return False
    t = text.strip().lower()
    if len(t) > 25 or any(ch.isdigit() for ch in t):
        return False
    # Remove emojis/pontuação básica
    cleaned = "".join(ch for ch in t if ch.isalpha() or ch.isspace() or ch == "?").strip()
    cleaned = " ".join(cleaned.split())  # colapsa espaços
    if not cleaned:
        return False
    return cleaned in _GREETING_TOKENS


def _greeting_reply(tenant: dict, phone: str) -> str:
    """Resposta determinística para saudações — não passa pelo LLM."""
    tenant_id = tenant["id"]
    # Tenta usar primeiro nome se já cadastrado.
    nome = ""
    try:
        patient = db.get_patient(tenant_id, phone)
        if patient and patient.get("name"):
            nome = patient["name"].split()[0]
    except Exception:
        pass
    if not nome:
        try:
            appt = cal.get_next_appointment(tenant_id, phone)
            if appt and appt.get("patient_name"):
                nome = appt["patient_name"].split()[0]
        except Exception:
            pass
    saud = f"Olá, {nome}!" if nome else "Olá!"
    return f"{saud} 😊 Tudo bem? Como posso te ajudar hoje?"


def process_message(tenant: dict, phone: str, text: str) -> tuple[str, AgentResponse]:
    """
    Processa uma mensagem e retorna (reply, agent_response).
    A publicação de eventos SSE é feita pelo chamador (main.py) no contexto async.
    """
    tenant_id = tenant["id"]
    db.save_message(tenant_id, phone, "user", text)

    # ── Atalho determinístico para saudações ────────────────────────────────────
    # Antes o LLM às vezes devolvia JSON malformado para "Olá" e caíamos no
    # fallback "Desculpe, não entendi". Saudação simples nunca precisa de LLM.
    if _is_simple_greeting(text):
        reply = _greeting_reply(tenant, phone)
        db.save_message(tenant_id, phone, "assistant", reply)
        logger.info(f"[{tenant['slug']}][{phone}] saudação simples — resposta determinística")
        return reply, AgentResponse(intent=Intent.other, action=Action.none,
                                     response_text=reply, data={}), None

    # limit aumentado de 6 → 24 para garantir cobertura de manhã, tarde e noite
    # em vários dias, permitindo ao LLM filtrar quando paciente pede "tarde"/"manhã"
    offered_slots = cal.get_available_slots(tenant, days_ahead=10, limit=24)
    context = _build_context(tenant, phone, offered_slots)

    # Flag: paciente é conhecido (tem consulta futura OU já cadastrado)?
    known_patient = ("CONSULTA AGENDADA:" in context and "id=" in context) or "PACIENTE CONHECIDO:" in context
    logger.info(f"[{tenant['slug']}][{phone}] known_patient={known_patient} | ctx={context.splitlines()[0] if context else ''!r}")

    history = db.get_conversation_history(tenant_id, phone, limit=8)
    messages = history + [
        {"role": "user", "content": f"[CONTEXTO]\n{context}\n\n[MENSAGEM DO PACIENTE]\n{text}"}
    ]

    system_prompt = _build_system_prompt(tenant)
    raw = _call_llm(system=system_prompt, messages=messages, max_tokens=512)
    # Parse defensivo: LLM pode devolver JSON malformado, prosa misturada,
    # ou campos fora do schema. Tenta 1 retry com prompt reforçado antes do
    # fallback humanizado.
    try:
        parsed = _extract_json(raw)
        agent_resp = AgentResponse(**parsed)
    except Exception as e:
        logger.warning(
            f"[{tenant['slug']}][{phone}] Falha JSON do LLM (tent.1): {e} | raw={raw[:200]!r}"
        )
        # Retry: reforça que precisa ser JSON válido
        try:
            retry_msgs = messages + [
                {"role": "assistant", "content": raw or ""},
                {"role": "user", "content":
                    "Sua resposta anterior não era JSON válido. Responda AGORA "
                    "APENAS com um objeto JSON no formato exigido (intent, action, "
                    "response_text, data). Sem texto fora do JSON, sem ```."}
            ]
            raw2 = _call_llm(system=system_prompt, messages=retry_msgs, max_tokens=512)
            parsed = _extract_json(raw2)
            agent_resp = AgentResponse(**parsed)
            logger.info(f"[{tenant['slug']}][{phone}] Retry JSON OK")
        except Exception as e2:
            logger.warning(
                f"[{tenant['slug']}][{phone}] Falha JSON (tent.2): {e2} | raw={raw[:200]!r}"
            )
            # Fallback humanizado — escolhe baseado na intenção provável do texto
            t = (text or "").lower()
            if any(k in t for k in ("agend", "marc", "horário", "horario", "consult", "sess", "atend")):
                resp_text = ("Recebi sua mensagem! 😊 Para te ajudar com o agendamento, "
                             "vou repassar para a psicóloga, que entra em contato em breve.")
            elif any(k in t for k in ("remarcar", "desmarcar", "cancelar", "mudar")):
                resp_text = ("Anotado! 😊 Vou repassar seu pedido para a psicóloga, "
                             "que retorna em breve para combinar com você.")
            elif any(k in t for k in ("valor", "preço", "preco", "quanto", "pagar", "pagamento")):
                resp_text = ("Sobre valores e pagamento, prefiro que a psicóloga te explique "
                             "diretamente. Já estou avisando ela! 😊")
            else:
                resp_text = ("Recebi sua mensagem! 😊 Vou repassar para a psicóloga, "
                             "que responde assim que puder.")
            logger.error(
                f"[{tenant['slug']}][{phone}] FALLBACK humanizado acionado "
                f"para texto={text[:80]!r} — verificar logs do LLM acima"
            )
            agent_resp = AgentResponse(
                intent=Intent.other, action=Action.none,
                response_text=resp_text, data={},
            )

    # Guard-rail: se LLM tratou paciente conhecido como novo, corrigir
    if known_patient and agent_resp.intent == Intent.new_patient:
        logger.warning(
            f"[{tenant['slug']}][{phone}] LLM alucinou new_patient para paciente conhecido — corrigindo. "
            f"context_head={context[:200]!r}"
        )
        patient = db.get_patient(tenant_id, phone)
        nome = (patient.get("name") if patient else None) or "tudo bem"
        appt = cal.get_next_appointment(tenant_id, phone)
        if appt:
            from datetime import datetime as _dt
            try:
                dt = _dt.fromisoformat(appt["scheduled_at"])
                quando = cal.format_slots([dt])[0]
                agent_resp.response_text = f"Olá! 😊 Tudo bem? Posso ajudar com sua sessão de {quando}?"
            except Exception:
                agent_resp.response_text = "Olá! 😊 Tudo bem? Posso ajudar com algo?"
        else:
            agent_resp.response_text = f"Olá, {nome}! 😊 Como posso ajudar?"
        agent_resp.intent = Intent.other
        agent_resp.action = Action.none
        agent_resp.data = {}

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
        try:
            idx = int(idx_raw) - 1  # 1-based → 0-based
        except (TypeError, ValueError):
            logger.warning(f"[{tenant['slug']}][{phone}] slot_index inválido do LLM: {idx_raw!r}")
            return "Não consegui identificar o horário escolhido. Pode repetir, por favor? 😊", None
        name = data.get("patient_name", "Paciente")
        if 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            duration = tenant.get("session_minutes", 50)
            if not db.has_conflict(tenant_id, slot, duration):
                appt_id = db.create_appointment(tenant_id, name, phone, slot)
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
        try:
            idx = int(idx_raw) - 1  # 1-based (display) → 0-based
        except (TypeError, ValueError):
            logger.warning(f"[{tenant['slug']}][{phone}] slot_index inválido (update): {idx_raw!r}")
            return "Não consegui identificar o horário escolhido. Pode repetir, por favor? 😊", None
        if appt_id and 0 <= idx < len(offered_slots):
            slot = offered_slots[idx]
            duration = tenant.get("session_minutes", 50)
            if not db.has_conflict(tenant_id, slot, duration, exclude_id=int(appt_id)):
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
        # Fallback: se o LLM não devolveu appointment_id, pega a próxima consulta
        # do paciente — MAS só se for a ÚNICA futura. Se houver 2+ consultas
        # futuras, é ambíguo qual confirmar — peça desambiguação ao paciente
        # em vez de confirmar a errada.
        if not appt_id:
            try:
                futuras = db.get_appointments_by_phone(tenant_id, phone)
                if len(futuras) == 1:
                    appt_id = futuras[0].get("id")
                elif len(futuras) > 1:
                    logger.warning(
                        f"[{tenant['slug']}][{phone}] Action.confirm ambíguo: {len(futuras)} consultas futuras, "
                        f"sem appointment_id do LLM — pedindo desambiguação"
                    )
                    lista = "\n".join(
                        f"  • {cal.format_appointment(a)} (id={a['id']})"
                        for a in futuras[:5]
                    )
                    return (
                        f"Você tem mais de uma sessão agendada. Qual você quer confirmar?\n\n{lista}",
                        None,
                    )
            except Exception as e:
                logger.warning(f"[confirm] fallback get_appointments_by_phone falhou: {e}")
        if appt_id:
            db.confirm_appointment(tenant_id, appt_id)
            logger.info(f"[{tenant['slug']}][{phone}] confirmed appointment id={appt_id}")
        else:
            logger.warning(f"[{tenant['slug']}][{phone}] Action.confirm sem appointment_id e sem próxima consulta")

    return text, {"type": "new_message", "data": {"phone": phone, "intent": resp.intent}}
