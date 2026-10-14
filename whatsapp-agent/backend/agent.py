from __future__ import annotations
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import database as db
import calendar_service as cal
import google_calendar_service as gcal
import caldav_service as caldav_svc
from models import AgentResponse, Action, Intent

# Fuso de Brasília — usado pelos atalhos determinísticos (confirmação,
# remarcação, parsing de data/hora). Mantido em nível de módulo para não
# depender de variáveis locais de outras funções.
_TZ = ZoneInfo("America/Sao_Paulo")


def _now() -> datetime:
    """Agora em horário de Brasília, naive. Ponto único de leitura do relógio no
    agente, para que o tempo possa ser fixado em teste."""
    return datetime.now(_TZ).replace(tzinfo=None)


# ── Contexto do evento em processamento (E03) ──────────────────────────────────
# Cada mensagem recebida é um evento durável em `inbound_events`. O id desse
# evento viaja num contexto por thread em vez de num parâmetro atravessando
# dezenas de funções: `process_message` roda numa thread por evento
# (`asyncio.to_thread` no main), então o escopo é exatamente o certo.
#
# Para que serve: tornar a criação de consulta idempotente POR EVENTO. Uma
# reentrada (lease vencido, retry após falha, reinício do processo) não pode
# criar uma segunda consulta.
import threading as _threading
from contextlib import contextmanager as _contextmanager

_ctx = _threading.local()


@_contextmanager
def contexto_evento(event_row_id: int | None, attempt_token: str | None = None,
                    identidade_confiavel: bool = True):
    """Amarra a conversa ao evento durável e à posse (lease) do worker.

    `attempt_token` viaja junto porque a posse é validada **dentro da transação
    da mutação** (ver `database.effect_tx`), não só na troca de estágio.

    `identidade_confiavel=False` quando o provedor não mandou id de mensagem.
    Nesse caso **nenhuma mutação é executada**: sem id, uma reentrega vira outro
    evento e a marca por evento não protege nada. O agente ainda consulta,
    acolhe e esclarece; a ação fica como pendência para revisão.
    """
    anterior = getattr(_ctx, "event_row_id", None)
    tok_anterior = getattr(_ctx, "attempt_token", None)
    oferta_anterior = getattr(_ctx, "offer_id", None)
    ident_anterior = getattr(_ctx, "identidade_confiavel", True)
    _ctx.event_row_id = event_row_id
    _ctx.attempt_token = attempt_token
    _ctx.identidade_confiavel = identidade_confiavel
    _ctx.offer_id = None
    try:
        yield _ctx
    finally:
        _ctx.event_row_id = anterior
        _ctx.attempt_token = tok_anterior
        _ctx.identidade_confiavel = ident_anterior
        _ctx.offer_id = oferta_anterior


# Mutações e efeitos externos que exigem identidade confiável do evento.
_ACOES_BLOQUEAVEIS = {
    "create": "agendar",
    "update": "remarcar",
    "confirm": "confirmar a presença",
    "cancel": "cancelar",
}

_MOTIVO_SEM_IDENTIDADE = (
    "mensagem recebida sem id do provedor: uma reentrega da mesma mensagem "
    "viraria outro evento, então a proteção contra ação em dobro não vale aqui"
)


def _mutacao_permitida() -> bool:
    """Falso quando o evento não tem identidade confiável.

    Sem contexto de evento (painel, endpoint de teste, agendador) devolve
    verdadeiro: ali não há reentrega de provedor para proteger.
    """
    if _evento_atual() is None:
        return True
    return bool(getattr(_ctx, "identidade_confiavel", True))


def _bloquear_mutacao(tenant: dict, phone: str, acao: str) -> str:
    """Registra a pendência e devolve a resposta ao paciente.

    Não é ficar calado: o paciente recebe uma resposta e o pedido fica visível
    para revisão humana em `GET /admin/fila`.
    """
    rotulo = _ACOES_BLOQUEAVEIS.get(acao, acao)
    try:
        db.pendencia_registrar(
            tenant["id"], f"bloqueado:{acao}",
            f"{rotulo} não executado — {_MOTIVO_SEM_IDENTIDADE}",
            event_row_id=_evento_atual(), phone=phone)
    except Exception as e:
        logger.exception(f"[agent] falha ao registrar pendência de {acao}: {e}")
        raise
    logger.warning(f"[{tenant.get('slug')}][{phone}] mutação '{acao}' BLOQUEADA "
                   f"(evento {_evento_atual()} sem id do provedor) — pendência "
                   f"registrada para revisão")
    nome = _primeiro_nome(tenant, phone)
    saud = f"{nome}, " if nome else ""
    return (f"{saud}recebi sua mensagem! 😊 Só preciso confirmar com você antes de "
            f"{rotulo}: pode me responder esta mensagem repetindo o dia e o "
            f"horário que prefere? Assim eu registro com segurança. 🙏")


def _evento_atual() -> int | None:
    return getattr(_ctx, "event_row_id", None)


def _oferta_do_turno() -> str | None:
    return getattr(_ctx, "offer_id", None)


def _posse_atual() -> str | None:
    return getattr(_ctx, "attempt_token", None)


def _mutacao_do_evento(kind: str):
    """Decide como executar uma mutação da agenda vinda de uma mensagem.

    Devolve `(contexto, tem_evento)`. Com evento, o contexto é
    `db.effect_tx(...)`: valida a posse do lease **e** reserva o efeito na mesma
    transação da gravação. Sem evento (painel, endpoint de teste, agendador), a
    mutação segue direta — não há identidade de evento para ancorar, e isso está
    registrado como limite em EVIDENCIAS/03.md.

    Vale para TODAS as mutações do fluxo de entrada, não só a criação. "Gravar
    o mesmo valor duas vezes" não é inofensivo: reaplicar uma confirmação antiga
    depois de o paciente ter remarcado confirma a consulta errada, zera os
    marcadores de lembrete e redispara aviso.
    """
    ev, tok = _evento_atual(), _posse_atual()
    if ev is None or tok is None:
        return None, False
    return db.effect_tx(ev, tok, kind), True


def _criar_consulta_idempotente(tenant_id: int, nome: str, phone: str,
                                slot: datetime) -> tuple[int, bool]:
    """Cria a consulta **uma única vez por evento**. Devolve (id, criada_agora)."""
    ctx, tem_evento = _mutacao_do_evento("appointment:create")
    if not tem_evento:
        return db.create_appointment(tenant_id, nome, phone, slot), True
    with ctx as (conn, ja_aplicado, ref_anterior):
        if ja_aplicado:
            anterior = int(ref_anterior) if str(ref_anterior).isdigit() else 0
            logger.info(f"[agent] evento {_evento_atual()}: consulta já criada "
                        f"(id={anterior}) — não duplicando")
            return anterior, False
        appt_id = db.create_appointment(tenant_id, nome, phone, slot, conn=conn)
        db.effect_set_ref(conn, _evento_atual(), "appointment:create", appt_id)
        return int(appt_id), True


def _remarcar_idempotente(tenant_id: int, appt_id: int, slot: datetime) -> bool:
    """Remarca uma única vez por evento. Devolve se a gravação ocorreu agora."""
    ctx, tem_evento = _mutacao_do_evento("appointment:update")
    if not tem_evento:
        return db.update_appointment(tenant_id, appt_id, slot)
    with ctx as (conn, ja_aplicado, _ref):
        if ja_aplicado:
            logger.info(f"[agent] evento {_evento_atual()}: remarcação já aplicada "
                        f"— não reaplicando (não zera lembretes de novo)")
            return False
        ok = db.update_appointment(tenant_id, appt_id, slot, conn=conn)
        db.effect_set_ref(conn, _evento_atual(), "appointment:update", appt_id)
        return ok


def _confirmar_idempotente(tenant_id: int, appt_id: int) -> bool:
    ctx, tem_evento = _mutacao_do_evento("appointment:confirm")
    if not tem_evento:
        return db.confirm_appointment(tenant_id, appt_id)
    with ctx as (conn, ja_aplicado, _ref):
        if ja_aplicado:
            logger.info(f"[agent] evento {_evento_atual()}: confirmação já aplicada")
            return False
        ok = db.confirm_appointment(tenant_id, appt_id, conn=conn)
        db.effect_set_ref(conn, _evento_atual(), "appointment:confirm", appt_id)
        return ok


def _cancelar_idempotente(tenant_id: int, appt_id: int) -> bool:
    ctx, tem_evento = _mutacao_do_evento("appointment:cancel")
    if not tem_evento:
        return db.cancel_appointment(tenant_id, appt_id)
    with ctx as (conn, ja_aplicado, _ref):
        if ja_aplicado:
            logger.info(f"[agent] evento {_evento_atual()}: cancelamento já aplicado")
            return False
        ok = db.cancel_appointment(tenant_id, appt_id, conn=conn)
        db.effect_set_ref(conn, _evento_atual(), "appointment:cancel", appt_id)
        return ok


def _pendencia_externa(tenant: dict, phone: str, kind: str, erro: str) -> None:
    """Registra que um efeito externo disparado pela entrada ficou INCERTO.

    O agendamento local é mantido de propósito (*fail-open*: perder a consulta
    por causa do Google seria pior). Mas a falha não pode terminar só num log:
    fica pendência visível em `GET /admin/fila`, para alguém conferir a agenda
    externa. **Não é reexecutada automaticamente** — o efeito local já está
    marcado como aplicado, e tentar de novo às cegas duplicaria o evento no
    calendário.
    """
    try:
        db.pendencia_registrar(tenant["id"], kind, str(erro)[:400],
                               event_row_id=_evento_atual(), phone=phone,
                               state="incerto")
    except Exception as e:
        logger.exception(f"[agent] falha ao registrar pendência {kind}: {e}")
    logger.warning(f"[{tenant.get('slug')}][{phone}] efeito externo {kind} "
                   f"INCERTO: {str(erro)[:200]} — pendência registrada")


def _efeito_do_evento(kind: str) -> str | None:
    """Referência do efeito já aplicado por este evento, ou None."""
    ev = _evento_atual()
    if ev is None:
        return None
    r = db.effect_get(ev, kind)
    ref = (r or {}).get("ref_id") or ""
    return ref if str(ref).strip() else None


def _salvar_mensagem_idempotente(tenant_id: int, phone: str, role: str, texto: str) -> None:
    """`conversations` é append-only: sem identidade de evento, um retry
    duplicaria a mensagem na aba Conversas do painel."""
    ctx, tem_evento = _mutacao_do_evento(f"conversation:{role}")
    if not tem_evento:
        db.save_message(tenant_id, phone, role, texto)
        return
    try:
        with ctx as (conn, ja_aplicado, _ref):
            if ja_aplicado:
                return
            db.save_message(tenant_id, phone, role, texto, conn=conn)
    except db.PosseEPerdida:
        raise


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
        # client.messages.create é SÍNCRONO/bloqueante. classify_image roda no
        # event loop (via _handle_message) — chamar direto congela o loop e trava
        # o painel durante a análise da imagem. Offload p/ thread.
        resp = await asyncio.to_thread(
            client.messages.create,
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


def _is_psychology(tenant: dict) -> bool:
    """True para o segmento clínico padrão (psicologia). Usado para preservar
    100% o comportamento atual: quando True, todos os textos/prompts saem
    exatamente como sempre foram. False = segmento genérico (Track B)."""
    return (tenant.get('segment') or 'psicologia').strip().lower() in ('', 'psicologia')


def _generic_service_noun(tenant: dict) -> str:
    """Substantivo NEUTRO (masculino, sem concordância de gênero) para respostas
    determinísticas de segmentos não-psicologia. Usamos 'agendamento' fixo para
    evitar bugs de concordância ('sessão remarcada' vs 'serviço remarcado')."""
    return "agendamento"


def _generic_professional(tenant: dict) -> str:
    """Como se referir ao profissional em respostas determinísticas genéricas."""
    return (tenant.get("psychologist_name") or "").strip() or "o responsável"


def _confirm_today_tail(reply: str, slot, tenant: dict, appt_id=None) -> str:
    """Pós-processa a confirmação de (re)agendamento. Quando a sessão é HOJE não
    faz sentido prometer "lembro um dia antes" (não há véspera) — remove essa
    promessa, confirma direto e já marca a consulta como confirmada (dispensa o
    followup da manhã). Fora o mesmo-dia, o texto volta intacto. Cobre tanto o
    texto do LLM quanto os fallbacks determinísticos."""
    try:
        if slot.date() != _now().date():
            return reply
    except Exception:
        return reply
    if appt_id is not None:
        try:
            # Mesma proteção das demais confirmações: identidade por evento.
            # Sem ela, um retry tardio reconfirmaria uma consulta já remarcada.
            _confirmar_idempotente(tenant["id"], int(appt_id))
        except db.PosseEPerdida:
            raise
        except Exception as e:
            logger.warning(f"[{tenant.get('slug')}] confirmação mesmo-dia falhou: {e}")
    # Remove qualquer promessa de lembrete da véspera, em qualquer variação
    cleaned = re.sub(
        r"\s*(?:e\s+)?vou te lembrar um dia antes[,.]?\s*(?:combinado)?\s*[!?]?\s*💖?",
        "", reply, flags=re.IGNORECASE).rstrip(" .!💖")
    return f"{cleaned}. Como é para *hoje*, já deixei sua presença confirmada. Até logo! 💖".strip()


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

    # Dispatch por segmento (Track B). 'psicologia' (default) devolve o prompt
    # clínico EXATAMENTE como sempre foi — zero mudança para os consultórios
    # atuais. Qualquer outro segmento usa o prompt genérico parametrizado.
    if not _is_psychology(tenant):
        return _generic_system_prompt(tenant, pix_section)

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

TOM E LINGUAGEM — SOE COMO UMA PESSOA, NÃO COMO UM ROBÔ:
- Fale como uma secretária simpática e próxima falaria no WhatsApp: leve, calorosa e natural.
- Use contrações e linguagem coloquial do dia a dia ("tá", "pra", "prontinho", "combinado?", "sem problema", "é só me chamar").
- Varie as frases — NÃO repita sempre a mesma estrutura ("Olá, {{nome}}! 😊 ...") em toda mensagem. Alterne aberturas: "Oi, {{nome}}!", "Prontinho!", "Perfeito!", "Que bom!", conforme o momento.
- Chame o paciente pelo primeiro nome de vez em quando, com naturalidade — não em toda frase.
- Emojis com moderação (1, no máximo 2 por mensagem), pra dar calor sem exagero.
- Evite jargão robótico e formalidade fria ("prezado", "solicito", "informamos que", "conforme supracitado"). Escreva como você falaria de verdade.
- Seja concisa: mensagens curtas soam mais humanas que parágrafos longos.
- O coração padrão do assistente é 💖 (use esse, não outros corações).

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
- NUNCA invente um nome. NUNCA escreva um nome próprio que não apareça literalmente no CONTEXTO desta mensagem.
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

AO CONCLUIR UM AGENDAMENTO OU REMARCAÇÃO (action create/update) — REGRA CRÍTICA:
- Responda confirmando o horário marcado de forma calorosa e natural (com o primeiro nome do paciente).
- NÃO peça "Você pode confirmar presença?" NEM "responda SIM" nesse momento. A confirmação de presença é enviada AUTOMATICAMENTE pelo sistema cerca de 1 dia ANTES da sessão — não agora, na hora de marcar.
- Diga, com leveza, que você vai lembrar o paciente perto da data.
- Exemplo de tom: "Prontinho, {{primeiro_nome}}! 😊 Sua sessão ficou marcada para [slot]. Vou te lembrar um dia antes, combinado? Qualquer coisa, é só me chamar. 💖"
- NUNCA escreva "confirmar presença", "responda SIM" ou cobre confirmação logo após marcar.

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
- Exemplo: "Fico feliz que você entrou em contato 💖 Sua mensagem chegou para a {tenant['psychologist_name']} e ela vai te responder o quanto antes. Você não está sozinho(a). 🌸"
- Use action "none" e intent "other"

CONTEÚDO PESSOAL / DESABAFO — REGRA ABSOLUTA (LEIA COM ATENÇÃO):
- Se o paciente compartilhar QUALQUER conteúdo pessoal, desabafo, queixa,
  relato sobre família, filhos, parceiro(a), comportamento, escola, trabalho,
  sentimento, opinião pessoal, dúvida sobre conduta clínica, pedido de
  orientação sobre como agir, ou QUALQUER assunto que NÃO seja estritamente
  agendamento/remarcação/confirmação/pagamento → NÃO TENTE RESPONDER.
- Sinais típicos: "minha filha…", "meu marido…", "estou perdendo a cabeça",
  "ela está irritada", "não sei o que fazer", "queria conversar sobre…",
  "compartilhar com você", "estou me sentindo", relatos longos do dia, etc.
- Sua ÚNICA resposta nesses casos deve ser, de forma calorosa mas curta:
  "Olá! 😊 Recebi sua mensagem. Vou repassar para a {tenant['psychologist_name']},
   que entra em contato em breve para conversar com você com a atenção que isso merece. 💖"
- Use intent "other" e action "none". NÃO tente acolher, opinar, orientar,
  validar sentimentos, dar conselhos, nem fazer perguntas de aprofundamento.
- NÃO mencione confirmação de consulta. NÃO ofereça horários. NÃO faça
  triagem clínica. Apenas a frase de repasse acima.
- EXCEÇÃO: se for crise/risco grave (auto-lesão, suicídio, urgência médica)
  → use a regra "Mensagem de urgência / crise emocional" acima.

PRIMEIRO CONTATO / NOVO PACIENTE — REGRA ABSOLUTA:
- Se a pessoa estiver fazendo primeiro contato e perguntar "como funciona a terapia",
  "qual o método", "atende on-line", "atende criança/adolescente/adulto",
  "qual o valor", "quanto custa", "tem convênio", "onde fica", "qual horário",
  "qual a abordagem", ou QUALQUER pergunta sobre o serviço/funcionamento:
  → NÃO tente explicar nada. NÃO invente. NÃO ofereça a primeira sessão.
- Resposta ÚNICA: "Olá! 😊 Recebi sua mensagem. Vou repassar para a
  {tenant['psychologist_name']}, que entra em contato em breve para te passar
  essas informações com calma. 💖"
- intent "new_patient" (se for o caso) ou "other", action "none".

PERGUNTAS SOBRE VALOR / PAGAMENTO / PRAZO — REGRA ABSOLUTA:
- Se perguntarem "qual o valor", "até quando posso pagar", "como pago",
  "tem desconto", "parcela", "qual a forma de pagamento", ou qualquer
  pergunta sobre dinheiro/cobrança que NÃO seja resposta a uma cobrança
  já enviada: NÃO responda com valor nem prazo.
- Resposta ÚNICA: "Vou entrar em contato com a {tenant['psychologist_name']}
  para te passar essa informação. 💖"
- action "none".

FRASES PROIBIDAS (NUNCA escreva isso):
- "Quer que eu a notifique para ela te responder?" / "Quer que eu avise ela?"
  → em vez disso: "Vou entrar em contato com a {tenant['psychologist_name']}
  para te passar a informação."
- "Te abraço" / "Um abraço" / "Abraço" (com ou sem emoji 💖)
  → não usar despedidas afetivas. Encerre com "💖" sozinho ou sem nada.
- "O que você precisa conversar com a {tenant['psychologist_name']}?" /
  "Qual o assunto?" / "Pode me contar o que está acontecendo?"
  → SIGILO. NUNCA pergunte o motivo, o assunto, o que a pessoa quer
  conversar, nem peça detalhes. A IA não tem acesso ao conteúdo clínico.
  Se a pessoa quiser falar com a psicóloga, apenas: "Claro! 😊 Vou avisar
  a {tenant['psychologist_name']} e ela te responde em breve por aqui. 💖"
- "Como posso te ajudar hoje?" / "No que posso ajudar?" no fim das frases
  → deixe a pessoa puxar o assunto.

CONFIRMAÇÃO FUTURA (paciente disse que vai confirmar depois):
- Se o paciente disser "posso confirmar amanhã?", "te aviso depois",
  "confirmo mais tarde", "deixa eu ver e te falo": NÃO diga "te esperamos
  quarta" nem dê como confirmado. Apenas: "Claro, sem problema! 😊
  Fico no aguardo da sua confirmação. 💖"
- action "none".

CANCELAMENTO — REGRA ABSOLUTA (NÃO CONFUNDIR COM REMARCAÇÃO):
- Se o paciente disser que quer/precisa CANCELAR ou DESMARCAR e NÃO pedir
  outro horário no lugar → é CANCELAMENTO, não remarcação.
- Sinais de cancelamento: "vou precisar cancelar", "quero cancelar",
  "preciso cancelar", "tenho que cancelar", "vou cancelar", "cancelei",
  "vou desmarcar", "preciso desmarcar", "não vou poder ir", "não consigo ir",
  "não vou conseguir", "vou ter que faltar".
- NUNCA, em hipótese alguma, ofereça novos horários, pergunte disponibilidade,
  ou tente remarcar quando o paciente está CANCELANDO. Ele NÃO pediu outro dia.
- Ação: use intent "cancel" e action "cancel" com
  data: {{"appointment_id": ID_DA_CONSULTA}} (use o ID da próxima consulta
  do paciente fornecido no contexto).
- response_text deve apenas acolher e avisar que a {tenant['psychologist_name']}
  vai entrar em contato — SEM oferecer reagendamento, SEM cobrar, SEM citar
  política. Exemplo: "Entendi, {{primeiro_nome}}! 😊 Vou avisar a
  {tenant['psychologist_name']} sobre o cancelamento e ela entra em contato
  com você em breve. 💖"
- DIFERENÇA: se o paciente pedir para MUDAR/REMARCAR para outro dia/horário
  ("preciso mudar para quinta", "dá pra remarcar?") → aí sim é remarcação
  (intent "reschedule"), e aí pergunte a disponibilidade normalmente.

FORMATO DE RESPOSTA (OBRIGATÓRIO — responda APENAS com JSON válido, sem markdown):

{{
  "intent": "confirm|schedule|reschedule|cancel|new_patient|other",
  "action": "none|list_slots|create|update|confirm|cancel",
  "data": {{}},
  "response_text": ""
}}

CAMPOS data ESPERADOS POR AÇÃO:
- list_slots: {{"offered_ids": ["2026-09-21T15:00", "2026-09-21T16:00"]}}
- create: {{"patient_name": "...", "slot_id": "2026-09-21T15:00"}}
- update: {{"appointment_id": 1, "slot_id": "2026-09-21T15:00"}}
- confirm: {{"appointment_id": 1}}
- cancel: {{"appointment_id": 1}}     ← cancelar a consulta (NÃO oferecer outro horário)
- none: {{}}

REGRA CRÍTICA — slot_id (como dizer QUAL horário o paciente escolheu):
Cada item de HORÁRIOS DISPONÍVEIS vem com um id entre colchetes. Copie o id do
item escolhido, EXATAMENTE como está. Ex.: se a lista tem
"  7. Segunda, 21/09 às 15:00  [id=2026-09-21T15:00]" e o paciente escolheu esse
horário, envie "slot_id": "2026-09-21T15:00".
NUNCA use a posição/numeração que VOCÊ mostrou ao paciente. Você mostra uma
lista filtrada (4-5 opções), então o "1" que ele vê quase nunca é o "1" da lista
acima — mandar a posição marca o paciente no horário ERRADO.
NUNCA invente um id: use somente ids que aparecem na lista.

REGRA CRÍTICA — offered_ids (obrigatório em list_slots):
Em list_slots, escolha quais horários oferecer e coloque os ids em
"offered_ids", NA ORDEM em que quer que o paciente os veja. Máximo 5.
Ex.: "offered_ids": ["2026-09-21T15:00", "2026-09-22T10:00"].
NÃO escreva a lista de horários no response_text: o sistema monta a mensagem
numerada a partir de offered_ids, para que o número que o paciente vê seja
exatamente o horário que ele vai receber. Em list_slots o response_text é
ignorado — pode deixá-lo vazio.
Se você não tiver ids para oferecer, use action "none" e pergunte o dia ou o
período em vez de listar horários no texto.

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

Ao marcar / remarcar uma sessão (NÃO peça confirmação de presença agora):
"Prontinho, [primeiro_nome]! 😊 Sua sessão ficou marcada para [slot]. Vou te lembrar um dia antes, combinado? 💖"
- ATENÇÃO: se a sessão for para HOJE (mesmo dia), NÃO diga "lembro um dia antes" — não há véspera. Confirme direto, ex.: "Prontinho! ✅ Sua sessão de hoje ficou às [hora]. Como é para hoje, já está confirmada. Até logo! 💖"

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


def _generic_system_prompt(tenant: dict, pix_section: str) -> str:
    """Prompt genérico multi-segmento (Track B).

    Usado quando tenant['segment'] != 'psicologia'. Mantém EXATAMENTE o mesmo
    contrato de saída JSON (intent/action/data/response_text + slot_index) que o
    prompt clínico, para que todo o downstream (_extract_json, _execute_action,
    _build_context) funcione sem alteração. Diferença: terminologia
    parametrizada e SEM as cláusulas clínicas (crise, desabafo/sigilo, método
    terapêutico) — que não se aplicam a outros segmentos.
    """
    prof = (tenant.get('professional_label') or '').strip() or 'profissional'
    cli = (tenant.get('client_noun') or '').strip() or 'cliente'
    svc = (tenant.get('service_noun') or '').strip() or 'atendimento'
    biz = (tenant.get('business_type') or '').strip() or 'estabelecimento'
    nome_prof = (tenant.get('psychologist_name') or '').strip() or prof
    Cli = cli[:1].upper() + cli[1:]
    Biz = biz[:1].upper() + biz[1:]

    return f"""Você é um assistente de atendimento de {biz} via WhatsApp.

FUNÇÃO:
Gerenciar agendamentos, confirmações, remarcações, cancelamentos e primeiro contato com novos {cli}s.

REGRAS:
- Seja breve, educado e acolhedor
- Linguagem simples e humanizada
- NÃO invente informações que você não tem (valores, preços, procedimentos, políticas, disponibilidade fora do contexto) → repasse para {nome_prof}
- Não compartilhar dados de terceiros
- Se for assunto sensível, fora do escopo de agendamento, reclamação, ou algo que você não sabe responder → encaminhar para um humano ({nome_prof})
- Não insistir se o {cli} não responder

TOM E LINGUAGEM — SOE COMO UMA PESSOA, NÃO COMO UM ROBÔ:
- Fale como um(a) atendente simpático(a) e próximo(a) falaria no WhatsApp: leve, cordial e natural.
- Use contrações e linguagem coloquial do dia a dia ("tá", "pra", "prontinho", "combinado?", "sem problema", "é só me chamar").
- Varie as frases — NÃO repita sempre a mesma estrutura em toda mensagem. Alterne aberturas: "Oi, {{nome}}!", "Prontinho!", "Perfeito!", "Que bom!", conforme o momento.
- Chame o {cli} pelo primeiro nome de vez em quando, com naturalidade — não em toda frase.
- Emojis com moderação (1, no máximo 2 por mensagem), pra dar calor sem exagero.
- Evite jargão robótico e formalidade fria ("prezado", "solicito", "informamos que", "conforme supracitado"). Escreva como você falaria de verdade.
- Seja conciso: mensagens curtas soam mais humanas que parágrafos longos.

HORÁRIO DE FUNCIONAMENTO: Seg–Sex, {tenant['working_hours_start']:02d}:00–{tenant['working_hours_end']:02d}:00
{Biz}: {tenant['name']}
RESPONSÁVEL: {nome_prof}

IMPORTANTE: use o nome do(a) responsável EXATAMENTE como fornecido acima, sem adicionar títulos como "Dr." ou "Dra." se não estiverem no nome.

{pix_section}REGRA ABSOLUTA — IDENTIFICAÇÃO DO {Cli.upper()} (verifique ANTES de qualquer resposta):
- Se o CONTEXTO contém "CONSULTA AGENDADA:" com um id (ex: "id=233") → {Cli.upper()} JÁ CADASTRADO. NUNCA pergunte o nome. NUNCA dê boas-vindas de novo {cli}. Cumprimente normalmente e ajude.
- Se o CONTEXTO contém "PACIENTE CONHECIDO:" → {Cli.upper()} JÁ CADASTRADO. NUNCA pergunte o nome.
- Use intent "new_patient" SOMENTE quando o contexto disser literalmente "CONSULTA AGENDADA: nenhuma" E não tiver linha "PACIENTE CONHECIDO".

FLUXO PARA NOVO {Cli.upper()} (somente se as 2 condições acima forem verdadeiras):
1. Dê boas-vindas de forma calorosa
2. Peça o nome completo
3. Após receber o nome: informe que {nome_prof} vai dar sequência ao atendimento e passar as próximas informações
4. Coloque o nome do {cli} em data: {{"patient_name": "..."}} assim que souber
5. Use action "none" e intent "new_patient"

{Cli.upper()} COM AGENDAMENTO (linha "CONSULTA AGENDADA: ... id=..."):
- Já é {cli} conhecido. NUNCA peça nome.
- Cumprimente educadamente ("Olá!", "Oi!", "Tudo bem?") sem boas-vindas de novo {cli}
- Se a mensagem for vaga ("oi", "olá"), responda algo como "Olá! Tudo bem? Posso ajudar com seu {svc} de [DIA]?"
- Pode confirmar / remarcar / cancelar / listar horários

{Cli.upper()} CONHECIDO SEM AGENDAMENTO FUTURO (quando aparecer "PACIENTE CONHECIDO" no contexto):
- NÃO peça o nome — você já sabe quem é
- Cumprimente pelo nome e pergunte como pode ajudar
- Pode oferecer horários se pedir agendamento
- Trate como {cli} retornante, não como novo {cli}

NOME DO {Cli.upper()} — REGRA ABSOLUTA:
- Use SOMENTE o nome que aparece na linha "NOME DO PACIENTE:" ou "PACIENTE CONHECIDO:" do CONTEXTO.
- NUNCA invente um nome. NUNCA escreva um nome próprio que não apareça literalmente no CONTEXTO desta mensagem.
- Se a linha disser "(sem nome cadastrado)" → NÃO use nome algum, cumprimente sem nome: "Olá! 😊".
- NUNCA confunda o nome do {cli} com o nome do(a) responsável.

CUMPRIMENTOS E MENSAGENS SIMPLES — REGRA CRÍTICA:
- "Bom dia", "Boa tarde", "Boa noite", "Oi", "Olá", "Tudo bem?" e variações = cumprimento.
- SEMPRE responda cumprimentos com calor: "Olá! Tudo bem? 😊 Posso te ajudar com seu {svc} de [DIA]?" ou "Bom dia! 😊 Como posso ajudar?"
- NUNCA responda "não entendi" para cumprimento. Se a mensagem for só saudação, pergunte gentilmente como pode ajudar.

FLUXO DE AGENDAMENTO / REMARCAÇÃO — REGRA CRÍTICA (LEIA COM ATENÇÃO):
- ANTES de listar horários, SEMPRE pergunte primeiro qual a melhor disponibilidade do {cli}.
- Quando o {cli} disser apenas "quero agendar", "gostaria de marcar", "preciso remarcar", "quero outro horário" SEM citar dia OU período → NÃO liste horários ainda.
  • Use action: "none" e intent: "schedule" (ou "reschedule") e pergunte de forma acolhedora:
    "Claro! 😊 Qual seria a melhor disponibilidade pra você — algum dia da semana ou período (manhã, tarde ou noite) que prefere? Assim já te mando opções que cabem na sua rotina."
- SÓ liste horários (action: "list_slots") quando o {cli} JÁ tiver informado preferência de dia, período ou horário (ex: "prefiro terça à tarde", "qualquer dia de manhã", "pode ser quinta", "qualquer horário", "tanto faz").
- EXCEÇÃO: se o {cli} já disse no MESMO pedido o que quer (ex: "quero agendar terça à tarde") → pule a pergunta e já ofereça horários filtrados.
- Quando o histórico recente mostrar que VOCÊ já perguntou a disponibilidade e o {cli} acabou de responder com dia/período → AGORA liste os horários filtrados. NUNCA pergunte duas vezes seguidas.

FILTRO DE PERÍODO AO OFERECER HORÁRIOS — REGRA CRÍTICA:
- Quando enfim for listar horários, observe o período que o {cli} pediu: "manhã", "tarde", "noite", etc.
- Manhã = 06:00–11:59 | Tarde = 12:00–17:59 | Noite = 18:00–23:59
- Filtre a lista HORÁRIOS DISPONÍVEIS do contexto e ofereça APENAS os que cabem no período/dia pedidos. Mostre no máximo 4–5 opções.
- Se NÃO houver horário no período pedido, diga claramente e ofereça outros períodos disponíveis — NUNCA finja oferecer um horário que não está no contexto.

AO CONCLUIR UM AGENDAMENTO OU REMARCAÇÃO (action create/update) — REGRA CRÍTICA:
- Responda confirmando o horário marcado de forma calorosa e natural (com o primeiro nome do {cli}).
- NÃO peça "Você pode confirmar presença?" NEM "responda SIM" nesse momento. A confirmação é enviada AUTOMATICAMENTE pelo sistema cerca de 1 dia ANTES — não agora.
- Diga, com leveza, que você vai lembrar perto da data.
- Exemplo: "Prontinho, {{primeiro_nome}}! 😊 Seu {svc} ficou marcado para [slot]. Vou te lembrar um dia antes, combinado? 💖"

CONFIRMAÇÃO — REGRAS CRÍTICAS:
- Se o {cli} responder com QUALQUER expressão positiva APÓS receber mensagem de confirmação → use action: "confirm" e data: {{"appointment_id": ID}}
- Exemplos que SÃO confirmação: "SIM", "sim", "ok", "confirmo", "confirmado", "pode ser", "estarei lá", "claro", "com certeza", "pode", "combinado", "perfeito"
- NUNCA reenvie "Você pode confirmar?" se o {cli} já respondeu positivamente
- NUNCA use action "none" quando o {cli} estiver confirmando presença
- Se for HOJE: "Ótimo! ✅ Presença confirmada. Até mais tarde! 😊" — NUNCA "Até amanhã" para hoje
- Se for AMANHÃ: "Ótimo! ✅ Presença confirmada. Até amanhã! 😊"
- Outro dia: "Ótimo! ✅ Presença confirmada. Até [dia da semana]! 😊"

MENSAGENS CASUAIS — REGRA CRÍTICA:
- Mensagem casual, comentário, emoji, agradecimento ou cumprimento que NÃO seja sobre agendamento → responda de forma natural e acolhedora, action: "none"
- NUNCA interprete mensagem casual como confirmação de agendamento
- EXCEÇÃO: se a casual vier LOGO APÓS uma mensagem de confirmação do sistema (consulta não confirmada), então "sim", "ok" etc. = ação "confirm"

REMARCAÇÃO — REGRAS CRÍTICAS:
- Só use action "update" quando o {cli} EXPLICITAMENTE pedir para remarcar ("quero remarcar", "preciso mudar o horário", "não posso nesse dia")
- "Sim", "ok", "confirmo" NUNCA acionam remarcação
- Após listar horários, só remarque quando o {cli} ESCOLHER um horário específico
- NUNCA remarque sem o {cli} confirmar o novo horário escolhido

CANCELAMENTO — REGRA ABSOLUTA (NÃO CONFUNDIR COM REMARCAÇÃO):
- Se o {cli} disser que quer/precisa CANCELAR ou DESMARCAR e NÃO pedir outro horário no lugar → é CANCELAMENTO.
- Sinais: "quero cancelar", "preciso cancelar", "vou cancelar", "vou desmarcar", "não vou poder ir", "não consigo ir".
- NUNCA ofereça novos horários nem tente remarcar quando o {cli} está CANCELANDO.
- Use intent "cancel" e action "cancel" com data: {{"appointment_id": ID}}.

SITUAÇÕES ESPECIAIS:

Atraso:
- Se o {cli} avisar atraso de até 25 minutos: aceite com tranquilidade, confirme que é HOJE e no mesmo horário.
- NUNCA diga "até amanhã" para algo de hoje.
- Exemplo: "Sem problema! 😊 Te esperamos hoje às [hora], pode vir com calma."
- Atraso maior que 25 minutos: sugira remarcar gentilmente.

Comprovante de pagamento / PIX:
- Se o {cli} enviar um comprovante: agradeça e informe que dará sequência.
- Exemplo: "Obrigado pelo pagamento! 😊 Recebi o comprovante. Até o {svc}!"

ASSUNTO FORA DO ESCOPO / PERGUNTA QUE VOCÊ NÃO SABE — REGRA ABSOLUTA:
- Se perguntarem valores, preços, prazos, procedimentos, políticas, ou qualquer coisa que NÃO seja agendar/remarcar/confirmar/cancelar → NÃO invente.
- Resposta: "Vou repassar para {nome_prof}, que te responde em breve com essa informação. 💖"
- Use action "none".

IMPORTANTE:
- NUNCA inventar horários — use apenas os horários fornecidos no contexto
- Sempre usar dados reais da agenda fornecidos no contexto
- A IA NÃO executa ações diretamente → apenas decide qual ação tomar

FORMATO DE RESPOSTA (OBRIGATÓRIO — responda APENAS com JSON válido, sem markdown):

{{
  "intent": "confirm|schedule|reschedule|cancel|new_patient|other",
  "action": "none|list_slots|create|update|confirm|cancel",
  "data": {{}},
  "response_text": ""
}}

CAMPOS data ESPERADOS POR AÇÃO:
- list_slots: {{"offered_ids": ["2026-09-21T15:00", "2026-09-21T16:00"]}}
- create: {{"patient_name": "...", "slot_id": "2026-09-21T15:00"}}
- update: {{"appointment_id": 1, "slot_id": "2026-09-21T15:00"}}
- confirm: {{"appointment_id": 1}}
- cancel: {{"appointment_id": 1}}     ← cancelar o agendamento (NÃO oferecer outro horário)
- none: {{}}

REGRA CRÍTICA — slot_id (como dizer QUAL horário o {cli} escolheu):
Cada item de HORÁRIOS DISPONÍVEIS vem com um id entre colchetes. Copie o id do
item escolhido, EXATAMENTE como está. Ex.: se a lista tem
"  7. Segunda, 21/09 às 15:00  [id=2026-09-21T15:00]" e o {cli} escolheu esse
horário, envie "slot_id": "2026-09-21T15:00".
NUNCA use a posição/numeração que VOCÊ mostrou ao {cli}. Você mostra uma lista
filtrada (4-5 opções), então o "1" que ele vê quase nunca é o "1" da lista
acima — mandar a posição marca o {cli} no horário ERRADO.
NUNCA invente um id: use somente ids que aparecem na lista.

REGRA CRÍTICA — offered_ids (obrigatório em list_slots):
Em list_slots, escolha quais horários oferecer ao {cli} e coloque os ids em
"offered_ids", NA ORDEM em que quer que ele os veja. Máximo 5.
NÃO escreva a lista de horários no response_text: o sistema monta a mensagem
numerada a partir de offered_ids, para que o número que o {cli} vê seja
exatamente o horário que ele vai receber. Em list_slots o response_text é
ignorado.
Se você não tiver ids para oferecer, use action "none" e pergunte o dia ou o
período em vez de listar horários no texto.

EXEMPLOS DE TOM:

Ao marcar / remarcar (NÃO peça confirmação de presença agora):
"Prontinho, [primeiro_nome]! 😊 Seu {svc} ficou marcado para [slot]. Vou te lembrar um dia antes, combinado? 💖"
- ATENÇÃO: se for para HOJE (mesmo dia), NÃO diga "lembro um dia antes" — confirme direto, ex.: "Prontinho! ✅ Como é para hoje, já está confirmado. Até logo! 💖"

Pedido de agendamento SEM preferência (1ª resposta):
"Claro! 😊 Qual seria a melhor disponibilidade pra você — algum dia da semana ou período (manhã, tarde ou noite) que prefere?"

Novo {cli}:
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
        now_br = _now()
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
            # Checar histórico de agendamentos passados — casa variantes do número
            # (com/sem DDI 55 e dígito 9 extra), igual ao painel, para reconhecer
            # o paciente mesmo se o nome foi gravado noutro formato de telefone.
            _vars = db._phone_variants(phone) | ({phone} if phone else set())
            with db.get_conn() as conn:
                if _vars:
                    _ph = ",".join("?" * len(_vars))
                    past = conn.execute(
                        f"SELECT patient_name FROM appointments WHERE tenant_id=? "
                        f"AND phone IN ({_ph}) AND COALESCE(patient_name,'') != '' "
                        f"ORDER BY scheduled_at DESC LIMIT 1",
                        (tenant_id, *_vars)
                    ).fetchone()
                else:
                    past = None
            if past:
                lines.append(f"PACIENTE CONHECIDO: {past['patient_name']} (já foi paciente, sem consulta futura agendada)")

    if offered_slots:
        lines.append("HORÁRIOS DISPONÍVEIS (use apenas estes):")
        # O [id=...] é o que o agente devolve em slot_id. A numeração NÃO serve
        # para identificar o horário: o agente mostra ao paciente uma lista
        # filtrada (4-5 opções), então o "1" que o paciente vê raramente é o
        # "1" desta lista — usar a posição marcava o paciente noutro horário.
        for i, (s, raw) in enumerate(zip(cal.format_slots(offered_slots), offered_slots), 1):
            lines.append(f"  {i}. {s}  [id={raw.strftime('%Y-%m-%dT%H:%M')}]")

    now = _now()
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


# Confirmações afirmativas curtas — tratadas de forma DETERMINÍSTICA, sem LLM.
# Motivo: o fluxo de confirmação é o mais frequente e crítico; deixá-lo
# 100% dependente do LLM fazia o "Sim" às vezes virar sugestão de horário
# ou não persistir confirmado=1. Aqui é à prova de regressão do prompt.
_AFFIRMATION_TOKENS = {
    "sim", "ssim", "siim", "simm", "siimm", "simmm", "sii", "sim sim",
    "sinm", "sm", "ok", "okay", "okk", "okey", "ock", "isso", "isso mesmo",
    "confirmo", "confirmado", "confirmada", "confirma", "confirmar",
    "confirmo sim", "sim confirmo", "confirmado sim", "sim confirmado",
    "pode confirmar", "pode ser", "pode sim", "claro", "com certeza",
    "perfeito", "combinado", "positivo", "quero confirmar", "confirmadissimo",
    "ta confirmado", "tá confirmado", "estarei la", "estarei lá", "vou sim",
    "to la", "tô lá", "estarei presente", "presença confirmada", "confirmando",
    "confirmadinho", "sim quero", "sim por favor", "sim pode", "podeser",
}
_AFFIRMATION_EMOJIS = {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "✅", "🙏", "🙏🏻", "👌", "👌🏻"}


def _is_affirmation(text: str) -> bool:
    """Detecta confirmação afirmativa PURA (sem outra intenção embutida).
    Exige correspondência exata a um token afirmativo — assim 'sim, mas quero
    remarcar' NÃO casa e cai no LLM normalmente."""
    if not text:
        return False
    t = text.strip().lower()
    if len(t) > 30 or any(ch.isdigit() for ch in t):
        return False
    # Emoji-only afirmativo
    stripped_emoji = t.strip()
    if stripped_emoji in _AFFIRMATION_EMOJIS:
        return True
    cleaned = "".join(ch for ch in t if ch.isalpha() or ch.isspace()).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        # pode ser só emoji (👍✅) — checar se algum char é emoji afirmativo
        return any(c in _AFFIRMATION_EMOJIS for c in t)
    # Guarda contra negação/ressalva embutida
    if any(neg in cleaned for neg in ("nao", "não", "mas ", "porem", "porém", "remarc", "cancel", "outro", "depois")):
        return False
    return cleaned in _AFFIRMATION_TOKENS


def _try_deterministic_confirm(tenant: dict, phone: str, text: str) -> str | None:
    """Se o paciente respondeu afirmativamente E existe uma consulta futura
    aguardando confirmação (não confirmada, não cancelada, e o sistema JÁ
    enviou o pedido de confirmação), confirma de forma determinística e
    devolve a resposta canônica. Caso contrário, devolve None (segue p/ LLM)."""
    if not _is_affirmation(text):
        return None
    tenant_id = tenant["id"]
    try:
        appt = cal.get_next_appointment(tenant_id, phone)
    except Exception:
        return None
    if not appt:
        return None
    # Cancelada → não trata aqui (deixa o LLM/fluxo normal).
    if appt.get("cancelled"):
        return None
    pedimos = bool(appt.get("confirmation_sent")) or bool(appt.get("followup_sent"))
    if not pedimos:
        return None
    # Nome SEMPRE da fonte determinística (o agendamento), nunca do LLM —
    # evita o caso em que o LLM cumprimentava com um nome errado/aleatório.
    nome = (appt.get("patient_name") or "").split()[0] if appt.get("patient_name") else ""
    # Já confirmada antes → re-afirmação ("Confirmado" de novo). Responde
    # determinístico para NÃO cair no LLM (que podia hallucinar o nome). Não
    # regrava nada no banco.
    if appt.get("confirmed"):
        saud = f"Perfeito, {nome}! 😊" if nome else "Perfeito! 😊"
        logger.info(f"[{tenant['slug']}][{phone}] RE-CONFIRMAÇÃO determinística id={appt['id']}")
        return f"{saud} Sua presença já está confirmada. Até lá! 🌸"
    try:
        _confirmar_idempotente(tenant_id, appt["id"])
    except Exception as e:
        logger.warning(f"[{tenant['slug']}][{phone}] confirm determinístico falhou: {e}")
        return None
    logger.info(f"[{tenant['slug']}][{phone}] CONFIRMAÇÃO DETERMINÍSTICA id={appt['id']}")
    # Monta resposta canônica conforme o dia.
    try:
        from zoneinfo import ZoneInfo as _ZI
        appt_dt = datetime.fromisoformat(appt["scheduled_at"])
        now_br = _now()
        delta_days = (appt_dt.date() - now_br.date()).days
        if delta_days <= 0:
            quando = "Até mais tarde!"
        elif delta_days == 1:
            quando = "Até amanhã!"
        else:
            dia = _DIAS_PT[appt_dt.weekday()].split("-")[0]
            quando = f"Até {dia}!"
    except Exception:
        quando = "Até lá!"
    return f"Ótimo! ✅ Presença confirmada. {quando} 😊"


# Negativas CURTAS e isoladas a um pedido de confirmação ("NÃO", "n", "nops"…).
# NÃO incluímos frases verbais de cancelamento ("não vou poder ir", "não consigo
# ir") de propósito: essas seguem para o fluxo de CANCELAMENTO do LLM, que de
# fato cancela e libera o horário. Aqui tratamos só o "NÃO" seco, que o LLM
# vinha confundindo com desabafo e respondendo "vou repassar para a Bruna".
_DECLINE_TOKENS = {
    "nao", "não", "naao", "naão", "naum", "naun", "nãoo", "naoo", "nn",
    "nops", "nope", "negativo", "n", "ainda nao", "ainda não",
    "nao confirmo", "não confirmo", "nao posso confirmar", "não posso confirmar",
    "nao vou confirmar", "não vou confirmar", "agora nao", "agora não",
    "hoje nao", "hoje não", "infelizmente nao", "infelizmente não",
}


def _is_confirmation_decline(text: str) -> bool:
    """True só para negativas curtas e isoladas (sem data/horário, sem pedido
    de remarcar embutido). Espelha _is_affirmation, mas para o 'NÃO'."""
    if not text:
        return False
    t = text.strip().lower()
    if len(t) > 25 or any(ch.isdigit() for ch in t):
        return False
    cleaned = "".join(ch for ch in t if ch.isalpha() or ch.isspace()).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return False
    # Se vier pedido de remarcar/horário embutido, NÃO é negativa pura.
    if any(w in cleaned for w in ("remarc", "outro", "dia", "hora", "manha", "manhã",
                                  "tarde", "noite", "pode ser", "consigo", "vou poder",
                                  "desmarc", "cancel")):
        return False
    return cleaned in _DECLINE_TOKENS


def _try_deterministic_decline(tenant: dict, phone: str, text: str):
    """Se o paciente respondeu 'NÃO' (seco) a um pedido de confirmação que o
    sistema JÁ enviou, devolve (reply, event) acolhendo e sinalizando para a
    psicóloga. NÃO cancela nem remarca — a Bruna decide (sigilo + segurança).
    Caso contrário devolve None (segue para o LLM)."""
    if not _is_confirmation_decline(text):
        return None
    tenant_id = tenant["id"]
    try:
        appt = cal.get_next_appointment(tenant_id, phone)
    except Exception:
        return None
    if not appt:
        return None
    if appt.get("confirmed") or appt.get("cancelled"):
        return None
    pedimos = bool(appt.get("confirmation_sent")) or bool(appt.get("followup_sent"))
    if not pedimos:
        return None
    nome = (appt.get("patient_name") or "").split()[0] if appt.get("patient_name") else ""
    saud = f"Entendi, {nome}! 😊" if nome else "Entendi! 😊"
    if _is_psychology(tenant):
        psic = tenant.get("psychologist_name") or "a psicóloga"
        reply = (f"{saud} Sem problema. Vou avisar a {psic} e ela entra em contato "
                 f"com você em breve por aqui. 💖")
    else:
        prof = (tenant.get("psychologist_name") or "").strip() or "o responsável"
        reply = (f"{saud} Sem problema. Vou avisar {prof} e já retornam o contato "
                 f"com você por aqui. 💖")
    logger.info(f"[{tenant['slug']}][{phone}] NEGATIVA DETERMINÍSTICA à confirmação id={appt['id']}")
    event = {"type": "confirmation_declined", "data": {
        "phone": phone,
        "patient_name": appt.get("patient_name") or "",
        "scheduled_at": appt.get("scheduled_at") or "",
    }}
    return reply, event


_WEEKDAYS_PT = {
    "segunda-feira": 0, "segunda": 0, "seg": 0,
    "terça-feira": 1, "terca-feira": 1, "terça": 1, "terca": 1, "ter": 1,
    "quarta-feira": 2, "quarta": 2, "qua": 2,
    "quinta-feira": 3, "quinta": 3, "qui": 3,
    "sexta-feira": 4, "sexta": 4, "sex": 4,
    "sábado": 5, "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}


def _parse_time_br(t: str):
    """Extrai (hora, minuto) de um texto PT-BR. None se não achar."""
    m = re.search(r"\b(\d{1,2})[:h](\d{2})\b", t)          # 16:30 / 16h30
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    m = re.search(r"\b(\d{1,2})\s*h(?:oras|rs|r)?\b", t)   # 16h / 16 horas / 16hrs
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h, 0
    m = re.search(r"[àa]s\s+(\d{1,2})\b(?!\s*/)", t)        # às 16 (não "às 16/06")
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h, 0
    if "meio dia" in t or "meio-dia" in t or "meiodia" in t:
        return 12, 0
    return None


def _parse_date_br(t: str, now: datetime):
    """Extrai uma data (date) de um texto PT-BR. None se não achar."""
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)   # dd/mm(/aaaa)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        ystr = m.group(3)
        y = (int(ystr) + 2000 if int(ystr) < 100 else int(ystr)) if ystr else now.year
        try:
            cand = datetime(y, mo, d).date()
        except ValueError:
            return None
        if not ystr and cand < now.date():     # sem ano e já passou → ano que vem
            try:
                cand = datetime(y + 1, mo, d).date()
            except ValueError:
                return None
        return cand
    if "depois de amanha" in t or "depois de amanhã" in t:
        return (now + timedelta(days=2)).date()
    if "amanha" in t or "amanhã" in t:
        return (now + timedelta(days=1)).date()
    if re.search(r"\bhoje\b", t):
        return now.date()
    m = re.search(r"\bdia\s+(\d{1,2})\b", t)                       # "dia 20"
    if m:
        d = int(m.group(1))
        try:
            cand = datetime(now.year, now.month, d).date()
        except ValueError:
            return None
        if cand < now.date():
            mo, y = now.month + 1, now.year
            if mo > 12:
                mo, y = 1, y + 1
            try:
                cand = datetime(y, mo, d).date()
            except ValueError:
                return None
        return cand
    for name, wd in _WEEKDAYS_PT.items():                         # nomes de dia
        if re.search(rf"\b{name}\b", t):
            ahead = (wd - now.weekday()) % 7
            if ahead == 0:
                ahead = 7      # "terça" quando hoje é terça → próxima terça
            return (now + timedelta(days=ahead)).date()
    return None


_SAUDACOES_RE = re.compile(r"\b(bom\s*dia|boa\s*tarde|boa\s*noite)\b", re.IGNORECASE)

# "manhã" não pode casar dentro de "amanhã" — daí o olhar-para-trás.
_PERIODO_RE = (
    ("manha", re.compile(r"(?<![a-zà-ú])manh[ãa]|\bmatutino\b", re.IGNORECASE)),
    ("tarde", re.compile(r"\btarde\b|\bvespertino\b", re.IGNORECASE)),
    ("noite", re.compile(r"\bnoite\b|\bnoitinha\b|\bnoturno\b", re.IGNORECASE)),
)


def _periodo_da_hora(hora: int) -> str | None:
    """Período do dia de uma hora: manhã 6–11, tarde 12–17, noite 18–23."""
    return cal.periodo_da_hora(hora)


def _parse_period_br(t: str) -> str | None:
    """Período do dia pedido no texto. None quando o paciente não restringiu.

    Saudações são removidas antes: "boa tarde, quero agendar segunda" não é um
    pedido de tarde.
    """
    if not t:
        return None
    limpo = _SAUDACOES_RE.sub(" ", t)
    for nome, rx in _PERIODO_RE:
        if rx.search(limpo):
            return nome
    return None


def _parse_request(text: str, now: datetime) -> dict:
    """O que o paciente pediu explicitamente, extraído por regra — não pelo
    modelo. É o que permite exigir que o horário escolhido seja compatível com
    o pedido em vez de confiar no modelo para declarar o dia certo."""
    t = (text or "").strip().lower()
    return {"date": _parse_date_br(t, now), "period": _parse_period_br(t)}


def _slot_atende_pedido(slot: datetime, pedido: dict | None) -> bool:
    """O horário escolhido respeita a data e o período que o paciente pediu?"""
    if not pedido:
        return True
    alvo = pedido.get("date")
    if alvo is not None and slot.date() != alvo:
        return False
    periodo = pedido.get("period")
    if periodo and _periodo_da_hora(slot.hour) != periodo:
        return False
    return True


# Escolha de opção pelo número: "1", "o 2", "quero o 3", "a 2ª opção".
# Exige um marcador de opção ou que a mensagem seja só o número — "tenho 2
# filhos" não é escolha. Mensagens com data ou hora são tratadas como escolha
# explícita de horário, não de posição.
_SO_NUMERO_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")
_ESCOLHA_RE = (
    re.compile(r"\b(?:op[çc][ãa]o|opcao|alternativa|hor[áa]rio)\s*(?:n[º°]?\s*)?(\d{1,2})\b",
               re.IGNORECASE),
    re.compile(r"\b(\d{1,2})\s*[ªº]?\s*(?:op[çc][ãa]o|opcao|alternativa)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:quero|prefiro|pode\s+ser|vai\s+ser|fico\s+com|escolho|seria)\s+"
               r"(?:[oa]\s+)?(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"(?:^|[\s,;])[oa]\s+(\d{1,2})\b", re.IGNORECASE),
)


def _parse_numeric_choice(text: str) -> int | None:
    """Número da opção que o paciente escolheu, ou None.

    Quando devolve um número, esse número é a fonte da verdade da escolha: ele
    tem de ser resolvido pela oferta persistida daquela conversa. Um `slot_id`
    do modelo que discorde não pode prevalecer em silêncio.
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _SO_NUMERO_RE.match(t)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    baixo = t.lower()
    # Data ou hora explícitas = escolha de horário, não de posição na lista.
    if _parse_time_br(baixo) or _parse_date_br(baixo, _now()):
        return None
    for rx in _ESCOLHA_RE:
        m = rx.search(baixo)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 20:
                return n
    return None


_LISTA_HORARIOS_RE = re.compile(
    r"^\s*\d+\s*[.)]\s*.*?\d{1,2}[:h]\d{2}", re.MULTILINE)


def _parece_lista_de_horarios(texto: str) -> bool:
    """O texto apresenta ao paciente uma lista numerada de horários?"""
    return len(_LISTA_HORARIOS_RE.findall(texto or "")) >= 2


def _parse_slot_id(raw) -> datetime | None:
    """Converte o carimbo `[id=AAAA-MM-DDTHH:MM]` em datetime. Estrito: qualquer
    coisa fora desse formato é recusada, porque um id que o backend não entende
    nunca pode virar agendamento."""
    if not isinstance(raw, str):
        return None
    txt = raw.strip().replace(" ", "T")[:16]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", txt):
        return None
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        return None


def _do_reschedule(tenant: dict, phone: str, appt: dict, slot: datetime) -> str:
    """Move a consulta para `slot`, sincroniza Google Calendar/CalDAV e devolve
    a confirmação. Reusa a mesma lógica comprovada de Action.update."""
    tenant_id = tenant["id"]
    appt_id = appt["id"]
    duration_min = tenant.get("session_minutes", 50)
    patient_name = appt.get("patient_name", "Paciente") or "Paciente"
    _remarcar_idempotente(tenant_id, appt_id, slot)
    # Oferta atendida: não pode continuar valendo para uma mensagem seguinte.
    db.offer_cancel_drafts(tenant_id, phone)
    if tenant.get("google_refresh_token"):
        try:
            gcal_ok = False
            if appt.get("google_event_id"):
                gcal_ok = gcal.update_event(tenant, appt["google_event_id"],
                                            patient_name, slot.isoformat(), duration_min)
            if not gcal_ok:
                new_id = gcal.create_event(tenant, patient_name, slot.isoformat(), duration_min)
                if new_id:
                    db.set_appointment_google_event_id(int(appt_id), new_id)
        except Exception as e:
            logger.warning(f"[gcal] reschedule determinístico falhou: {e}")
            _pendencia_externa(tenant, phone, "calendar:update", e)
    else:
        try:
            cd_ok = False
            if appt.get("google_event_id"):
                cd_ok = caldav_svc.update_event(tenant, appt["google_event_id"],
                                                patient_name, slot.isoformat(), duration_min)
            if not cd_ok:
                new_uid = caldav_svc.create_event(tenant, patient_name, slot.isoformat(), duration_min)
                if new_uid:
                    db.set_appointment_google_event_id(int(appt_id), new_uid)
        except Exception as e:
            logger.warning(f"[caldav] reschedule determinístico falhou: {e}")
            _pendencia_externa(tenant, phone, "calendar:update", e)
    formatted = cal.format_slots([slot])[0]
    nome = patient_name.split()[0] if patient_name else ""
    saud = f"Prontinho, {nome}! ✅" if nome else "Prontinho! ✅"
    logger.info(f"[{tenant['slug']}][{phone}] REMARCAÇÃO DETERMINÍSTICA id={appt_id} → {slot.isoformat()}")
    if _is_psychology(tenant):
        reply = f"{saud} Sua sessão foi remarcada para *{formatted}*. Vou te lembrar um dia antes, combinado? 💖"
    else:
        svc = _generic_service_noun(tenant)
        reply = f"{saud} Seu {svc} foi remarcado para *{formatted}*. Vou te lembrar um dia antes, combinado? 💖"
    return _confirm_today_tail(reply, slot, tenant, appt_id)


def _try_deterministic_reschedule(tenant: dict, phone: str, text: str):
    """Paciente informou DATA + HORA explícitas (ex.: 'Dia 20/06 às 16h') e tem
    uma consulta futura → remarca direto se o horário estiver livre, ou oferece
    alternativas próximas se não. Antes o LLM ignorava a data e perguntava de
    novo a disponibilidade. Devolve (reply, event) ou None (segue p/ LLM)."""
    if not text:
        return None
    t = text.strip().lower()
    if "?" in t or len(t) > 50:
        return None
    # Cancelamento/desabafo não são remarcação — deixa o fluxo próprio cuidar.
    if any(w in t for w in ("cancel", "desmarc", "nao vou", "não vou", "nao posso", "não posso")):
        return None
    now_br = _now()
    tm = _parse_time_br(t)
    dd = _parse_date_br(t, now_br)
    if not tm or not dd:
        return None
    target = datetime(dd.year, dd.month, dd.day, tm[0], tm[1])
    tenant_id = tenant["id"]
    try:
        appt = cal.get_next_appointment(tenant_id, phone)
    except Exception:
        return None
    # Só remarca quem JÁ tem consulta real futura (não placeholder de novo paciente).
    if not appt or appt.get("cancelled"):
        return None
    if (appt.get("scheduled_at") or "").startswith("2099-"):
        return None
    nome = (appt.get("patient_name") or "").split()[0] if appt.get("patient_name") else ""
    ok, motivo = cal.is_slot_bookable(tenant, target, exclude_id=int(appt["id"]))
    event = {"type": "new_message", "data": {"phone": phone, "intent": "reschedule"}}
    if ok:
        reply = _do_reschedule(tenant, phone, appt, target)
        return reply, event
    # Horário pedido indisponível → oferecer próximos (determinístico).
    fmt_target = cal.format_slots([target])[0]
    alts = cal.suggest_slots_near(tenant, target, n=3)
    logger.info(f"[{tenant['slug']}][{phone}] data explícita indisponível ({motivo}): {target.isoformat()}")
    if alts:
        # As alternativas ficam PERSISTIDAS como oferta da conversa: é isso que
        # permite ao paciente responder "o 1" em seguida e o servidor resolver
        # esse número para o instante exato que ele viu.
        abre = f"Poxa, {nome}! " if nome else ""
        reply = _ofertar(
            tenant, phone, alts,
            f"{abre}O horário {fmt_target} não está disponível. 😕\nPosso te oferecer:",
        )
    else:
        db.offer_cancel_drafts(tenant_id, phone)
        reply = (f"O horário {fmt_target} não está disponível e não encontrei horários "
                 f"próximos. Quer me dizer outro dia ou período? 😊")
    return reply, event


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
    # NÃO oferecer ajuda no fim — deixa o paciente puxar o assunto.
    # Pedido da Bruna: evita "no que posso te ajudar" toda hora.
    return f"Olá, {nome}! 😊" if nome else "Olá! 😊"


def process_message(tenant: dict, phone: str, text: str) -> tuple[str, AgentResponse]:
    """
    Processa uma mensagem e retorna (reply, agent_response).
    A publicação de eventos SSE é feita pelo chamador (main.py) no contexto async.
    """
    tenant_id = tenant["id"]
    # Zera o marcador de "oferta montada neste turno". `contexto_evento` já faz
    # isso no caminho durável, mas `process_message` também é chamado direto
    # (endpoint de teste, painel): sem o reset, o valor de uma chamada anterior
    # na mesma thread sobreviveria e a guarda de coerência seria pulada.
    _ctx.offer_id = None
    _salvar_mensagem_idempotente(tenant_id, phone, "user", text)

    # ── Atalho determinístico para saudações ────────────────────────────────────
    # Antes o LLM às vezes devolvia JSON malformado para "Olá" e caíamos no
    # fallback "Desculpe, não entendi". Saudação simples nunca precisa de LLM.
    if _is_simple_greeting(text):
        reply = _greeting_reply(tenant, phone)
        _salvar_mensagem_idempotente(tenant_id, phone, "assistant", reply)
        logger.info(f"[{tenant['slug']}][{phone}] saudação simples — resposta determinística")
        return reply, AgentResponse(intent=Intent.other, action=Action.none,
                                     response_text=reply, data={}), None

    # ── Sem identidade confiável do evento, os atalhos que MUTAM não rodam ─────
    # Os atalhos determinísticos (confirmar, recusar, remarcar com data/hora)
    # gravam direto no banco, antes de o modelo entrar. Se o evento não tem id
    # do provedor, uma reentrega da mesma mensagem viraria outro evento e a
    # marca por evento não protege nada.
    #
    # Em vez de adivinhar a intenção pelo texto e recusar tudo, a mensagem segue
    # o caminho normal: consultar, acolher e esclarecer continuam liberados, e a
    # recusa acontece só se a decisão for de fato uma mutação (ver as guardas em
    # `_execute_action`). Bloquear por palpite barrava até "me mostra os
    # horários", que não altera nada.
    _atalhos_liberados = _mutacao_permitida()

    # ── Atalho determinístico para CONFIRMAÇÃO ("Sim", "Ok", "Confirmo"…) ───────
    # Fluxo mais crítico e frequente: não pode depender do LLM (regredia toda
    # vez que o prompt crescia). Só dispara quando há consulta futura pendente
    # à qual o sistema JÁ pediu confirmação — então é seguro confirmar.
    _det_confirm = (_try_deterministic_confirm(tenant, phone, text)
                    if _atalhos_liberados else None)
    if _det_confirm is not None:
        _salvar_mensagem_idempotente(tenant_id, phone, "assistant", _det_confirm)
        logger.info(f"[{tenant['slug']}][{phone}] confirmação determinística — sem LLM")
        return _det_confirm, AgentResponse(
            intent=Intent.confirm, action=Action.confirm,
            response_text=_det_confirm, data={}), {
                "type": "appointment_confirmed", "data": {"phone": phone}}

    # ── Atalho determinístico para NEGATIVA ("Não" seco) à confirmação ──────────
    # Antes o LLM confundia "NÃO" com desabafo e respondia "vou repassar para a
    # Bruna" (errado no contexto). Agora acolhe e sinaliza para a psicóloga,
    # sem cancelar nem remarcar (ela decide). Só dispara se há confirmação
    # pendente que o sistema já pediu.
    _det_decline = (_try_deterministic_decline(tenant, phone, text)
                    if _atalhos_liberados else None)
    if _det_decline is not None:
        _reply_d, _event_d = _det_decline
        _salvar_mensagem_idempotente(tenant_id, phone, "assistant", _reply_d)
        logger.info(f"[{tenant['slug']}][{phone}] negativa determinística — sem LLM")
        return _reply_d, AgentResponse(
            intent=Intent.other, action=Action.none,
            response_text=_reply_d, data={}), _event_d

    # ── Atalho determinístico para REMARCAÇÃO com DATA+HORA explícitas ──────────
    # "Dia 20/06 às 16h", "amanhã às 15h", "terça 10h" → o LLM ignorava a data e
    # perguntava de novo a disponibilidade. Agora, se o paciente tem consulta
    # futura, remarca direto (se livre) ou oferece horários próximos.
    _det_resched = (_try_deterministic_reschedule(tenant, phone, text)
                    if _atalhos_liberados else None)
    if _det_resched is not None:
        _reply_r, _event_r = _det_resched
        _salvar_mensagem_idempotente(tenant_id, phone, "assistant", _reply_r)
        logger.info(f"[{tenant['slug']}][{phone}] remarcação determinística — sem LLM")
        return _reply_r, AgentResponse(
            intent=Intent.reschedule, action=Action.update,
            response_text=_reply_r, data={}), _event_r

    # ── Horários que vão para o contexto ───────────────────────────────────────
    # O pedido do paciente (data e período) é extraído por REGRA, não pelo
    # modelo. Se ele pediu um dia, as opções desse dia vão para o contexto, mais
    # uma amostra de outros dias. Sem pedido explícito, vai uma amostra
    # equilibrada: poucas opções por dia ao longo do horizonte.
    #
    # O que isso corrige: antes eram os 24 PRIMEIROS horários livres do
    # calendário. Com a agenda dos próximos dias cheia, os 24 se esgotavam em
    # dois dias e o dia pedido pelo paciente nunca chegava ao contexto — o
    # agente então marcava outro dia (EVIDENCIAS/01.md §3).
    pedido = _parse_request(text, _now())
    # Escolha de opção pelo número, lida do texto do PACIENTE. É a fonte da
    # verdade da escolha; o `slot_id` do modelo não pode contrariá-la em
    # silêncio. Mensagem com data/hora explícitas não é escolha de posição.
    escolha = _parse_numeric_choice(text)
    if escolha is not None:
        logger.info(f"[{tenant['slug']}][{phone}] paciente escolheu a opção {escolha}")
    _appt_atual = cal.get_next_appointment(tenant_id, phone)
    _excl_id = None
    if _appt_atual and not str(_appt_atual.get("scheduled_at", "")).startswith("2099-"):
        _excl_id = _appt_atual.get("id")

    offered_slots: list = []
    if pedido["date"] is not None:
        offered_slots = cal.find_slots(
            tenant, days_ahead=30, on_date=pedido["date"], period=pedido["period"],
            per_day=6, exclude_id=_excl_id,
        )
    outros = cal.find_slots(
        tenant, days_ahead=14, period=pedido["period"], per_day=2, max_total=16,
        exclude_id=_excl_id,
    )
    for s in outros:
        if s not in offered_slots:
            offered_slots.append(s)
    offered_slots.sort()
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
            if _is_psychology(tenant):
                if any(k in t for k in ("agend", "marc", "horário", "horario", "consult", "sess", "atend")):
                    resp_text = ("Recebi sua mensagem! 😊 Para te ajudar com o agendamento, "
                                 "vou repassar para a psicóloga, que entra em contato em breve.")
                elif any(k in t for k in ("cancelar", "desmarcar", "não vou poder", "nao vou poder")):
                    resp_text = ("Entendi! 😊 Vou avisar a psicóloga sobre o cancelamento "
                                 "e ela entra em contato com você em breve. 💖")
                elif any(k in t for k in ("remarcar", "mudar")):
                    resp_text = ("Anotado! 😊 Vou repassar seu pedido para a psicóloga, "
                                 "que retorna em breve para combinar com você.")
                elif any(k in t for k in ("valor", "preço", "preco", "quanto", "pagar", "pagamento")):
                    resp_text = ("Sobre valores e pagamento, prefiro que a psicóloga te explique "
                                 "diretamente. Já estou avisando ela! 😊")
                else:
                    resp_text = ("Recebi sua mensagem! 😊 Vou repassar para a psicóloga, "
                                 "que responde assim que puder.")
            else:
                _p = _generic_professional(tenant)
                if any(k in t for k in ("agend", "marc", "horário", "horario", "consult", "sess", "atend")):
                    resp_text = (f"Recebi sua mensagem! 😊 Para te ajudar com o agendamento, "
                                 f"vou repassar para {_p}, que entra em contato em breve.")
                elif any(k in t for k in ("cancelar", "desmarcar", "não vou poder", "nao vou poder")):
                    resp_text = (f"Entendi! 😊 Vou avisar {_p} sobre o cancelamento "
                                 f"e já retornam o contato com você em breve. 💖")
                elif any(k in t for k in ("remarcar", "mudar")):
                    resp_text = (f"Anotado! 😊 Vou repassar seu pedido para {_p}, "
                                 f"que retorna em breve para combinar com você.")
                elif any(k in t for k in ("valor", "preço", "preco", "quanto", "pagar", "pagamento")):
                    resp_text = (f"Sobre valores e pagamento, prefiro que {_p} te explique "
                                 f"diretamente. Já estou avisando! 😊")
                else:
                    resp_text = (f"Recebi sua mensagem! 😊 Vou repassar para {_p}, "
                                 f"que responde assim que puder.")
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
                if _is_psychology(tenant):
                    agent_resp.response_text = f"Olá! 😊 Tudo bem? Posso ajudar com sua sessão de {quando}?"
                else:
                    agent_resp.response_text = f"Olá! 😊 Tudo bem? Posso ajudar com seu {_generic_service_noun(tenant)} de {quando}?"
            except Exception:
                agent_resp.response_text = "Olá! 😊 Tudo bem? Posso ajudar com algo?"
        else:
            agent_resp.response_text = f"Olá, {nome}! 😊 Como posso ajudar?"
        agent_resp.intent = Intent.other
        agent_resp.action = Action.none
        agent_resp.data = {}

    reply, event = _execute_action(tenant, agent_resp, phone, pedido, escolha)
    reply = _strip_forbidden_phrases(reply)

    # ── Guarda de coerência ───────────────────────────────────────────────────
    # Se a resposta apresenta uma lista numerada de horários mas nenhuma oferta
    # foi montada neste turno, essa lista é texto livre do modelo: a oferta
    # ativa (de antes) não corresponde ao que o paciente está vendo. Manter a
    # antiga faria o "1" seguinte apontar para outra coisa.
    if _parece_lista_de_horarios(reply):
        if _oferta_do_turno() is None:
            logger.warning(f"[{tenant['slug']}][{phone}] resposta lista horários sem "
                           f"oferta nova — invalidando as ofertas anteriores")
            db.offer_invalidate(tenant_id, phone, "lista fora do servidor")

    # Oferta efetivamente usada numa reserva: encerrada para não valer de novo.
    _consumida = getattr(_ctx, "oferta_consumida", None)
    if _consumida:
        db.offer_consume(_consumida)
        _ctx.oferta_consumida = None

    _salvar_mensagem_idempotente(tenant_id, phone, "assistant", reply)
    return reply, agent_resp, event


# ══════════════════════════════════════════════════════════════════════════════
# E03 — interface usada pelo processamento durável (inbox.py)
# ══════════════════════════════════════════════════════════════════════════════
#
# `process_message` continua sendo a implementação única da conversa. O que E03
# acrescenta é a separação entre **decidir** e **aplicar**, para que o worker
# possa gravar o progresso entre as duas e retomar do ponto certo:
#
#   decidir()  → chama o modelo (ou resolve pelo atalho determinístico) e
#                devolve algo serializável, que é persistido no evento.
#   aplicar()  → executa a decisão e devolve o texto final e a oferta criada.
#
# Por que o atalho determinístico fica em `decidir`: ele não usa o modelo, e as
# mutações que faz (confirmar, cancelar, remarcar para um horário específico)
# são idempotentes **por valor** — gravam um estado, não acumulam. Repetir é
# inofensivo. A única mutação que acumula é a CRIAÇÃO de consulta, e essa está
# protegida por evento em `_criar_consulta_idempotente`.


def decidir(tenant: dict, phone: str, text: str,
            event_row_id: int | None = None,
            attempt_token: str | None = None,
            identidade_confiavel: bool = True) -> dict:
    """Decide o que responder. Devolve um dicionário serializável.

    Para o caminho do modelo, a decisão guardada é o JSON que ele produziu —
    então um retry **não** chama o modelo de novo nem muda o que foi decidido.

    As mutações acontecem aqui dentro, cada uma com identidade de evento e com a
    posse validada na própria transação. Por isso a ordem importa: a mutação
    ocorre ANTES de a decisão ser persistida. Se o processo cair entre as duas,
    o retry reexecuta e a marca de efeito impede aplicar de novo.
    """
    with contexto_evento(event_row_id, attempt_token, identidade_confiavel):
        reply, resp, event = process_message(tenant, phone, text)
        return {
            "kind": "resolvida",
            "intent": str(getattr(resp.intent, "value", resp.intent)),
            "action": str(getattr(resp.action, "value", resp.action)),
            "data": resp.data or {},
            "reply": reply,
            "offer_id": _oferta_do_turno(),
            "event": event,
        }


def aplicar(tenant: dict, phone: str, decisao: dict,
            event_row_id: int | None = None) -> dict:
    """Devolve o texto final e a oferta associada à decisão já tomada.

    A decisão de `decidir` já embute a execução (é `process_message`), então
    aqui não há segunda mutação: reaplicar seria justamente o risco que a
    marca de idempotência por evento existe para evitar. O retorno é o que o
    worker precisa para enfileirar a resposta no outbox.
    """
    reply = decisao.get("reply") or ""
    return {"reply": reply, "offer_id": decisao.get("offer_id"),
            "event": decisao.get("event"), "action": decisao.get("action")}


# ── Sanitização final: remove frases proibidas que o LLM insiste em escrever ────
import re as _re_san

_FORBIDDEN_PATTERNS = [
    # "Te abraço! 💖" / "Um abraço" / "Abraço(s)" no fim
    _re_san.compile(r"\s*(?:te\s+abra[çc]o|um\s+abra[çc]o|abra[çc]os?)[\s!.,💖💗🩷🌸😊]*$", _re_san.IGNORECASE),
    # "Quer que eu (a) notifique/avise..."
    _re_san.compile(r"\s*Quer que eu\s+a?\s*notifi\w+[^?]*\?", _re_san.IGNORECASE),
    _re_san.compile(r"\s*Quer que eu\s+a?\s*avis\w+[^?]*\?", _re_san.IGNORECASE),
    # "O que você precisa conversar..." (quebra de sigilo)
    _re_san.compile(r"\s*(?:O\s+que|Qual)\s+(?:voc[êe]\s+)?(?:precisa|quer|gostaria\s+de)\s+(?:conversar|falar|tratar)[^?]*\?", _re_san.IGNORECASE),
    # "Como posso te ajudar hoje?" / "No que posso ajudar?"
    _re_san.compile(r"\s*(?:Como|No que)\s+posso\s+(?:te\s+)?ajud\w+[^?]*\?", _re_san.IGNORECASE),
    # "Pode me contar o que está acontecendo"
    _re_san.compile(r"\s*Pode me contar[^?.!]*[?.!]", _re_san.IGNORECASE),
]


def _strip_forbidden_phrases(text: str) -> str:
    if not text:
        return text
    out = text
    # Aplica várias passadas porque uma frase pode vir colada na outra.
    for _ in range(3):
        prev = out
        for pat in _FORBIDDEN_PATTERNS:
            out = pat.sub("", out)
        out = out.strip()
        if out == prev:
            break
    # Se ficou só pontuação/emoji solto no final, limpa.
    out = _re_san.sub(r"[\s]+([!?.])", r"\1", out).strip()
    return out or "Olá! 😊 Vou repassar sua mensagem. 💖"


def _resolve_slot(data: dict, tenant: dict, phone: str,
                  pedido: dict | None = None, exclude_id: int | None = None,
                  escolha: int | None = None):
    """Resolve o horário que o paciente escolheu. Devolve (slot|None, origem, motivo).

    Regras, na ordem:

    0. **Escolha numérica do paciente** (`escolha`, extraída do texto DELE, não
       do modelo). Quando existe, ela é a fonte da verdade e só pode ser
       resolvida pela oferta persistida daquela conversa. Um `slot_id` do modelo
       que discorde é registrado e **ignorado** — deixá-lo prevalecer era
       exatamente como o paciente acabava reservando outra opção.
    1. **`slot_id`** — instante exato. É validado contra as regras REAIS de
       disponibilidade (`is_slot_bookable`), não contra a lista que foi para o
       contexto. Assim uma segunda-feira legítima que ficou fora da amostra
       continua agendável, em vez de ser recusada genericamente.
    2. **`slot_index`** — a posição que o paciente viu ("quero o 1"). Só é
       aceita se existir uma **oferta persistida daquela conversa**, e resolve
       para o instante exato gravado nela. Nunca indexa a lista global do
       calendário: era isso que marcava o paciente em outro dia, em silêncio.
    3. Nada resolvível → não se altera a agenda; o chamador pede esclarecimento.

    Em todos os casos o horário também precisa ser compatível com o que o
    paciente pediu (data e período), porque o modelo não é fonte confiável para
    declarar o dia que ele mesmo escolheu.
    """
    slug = tenant.get("slug")

    if escolha is not None:
        ctx = db.offer_context(tenant["id"], phone)
        if ctx["ambigua"]:
            # Existe uma oferta posterior cujo envio ficou INCERTO: o paciente
            # pode ter visto uma lista ou outra, e um "1" sozinho não revela
            # qual. Sem referência confiável à mensagem citada, resolver pela
            # mais recente seria adivinhar. Esclarecer é a única saída correta.
            logger.warning(f"[{slug}][{phone}] opção {escolha} AMBÍGUA entre "
                           f"versões de oferta (incertas={len(ctx['incertas'])})")
            return None, "escolha_numerica", "oferta_ambigua"
        info = ctx["ativa"]
        if not info:
            pend = len(ctx["pendentes"])
            logger.warning(f"[{slug}][{phone}] paciente escolheu a opção {escolha} "
                           f"mas não há oferta comprovadamente entregue "
                           f"(rascunhos/pendentes={pend})")
            return None, "escolha_numerica", "sem_oferta"
        oferta = info["slots"]
        if not 1 <= escolha <= len(oferta):
            logger.warning(f"[{slug}][{phone}] opção {escolha} fora da oferta "
                           f"{info['offer_id'][:8]} ({len(oferta)} opções)")
            return None, "escolha_numerica", "fora_da_oferta"
        slot = oferta[escolha - 1]
        do_modelo = _parse_slot_id(data.get("slot_id"))
        if do_modelo is not None and do_modelo != slot:
            logger.warning(
                f"[{slug}][{phone}] modelo mandou slot_id {do_modelo.isoformat()} "
                f"mas o paciente escolheu a opção {escolha} = {slot.isoformat()} "
                f"(oferta {info['offer_id'][:8]}) — usando a oferta"
            )
        if not _slot_atende_pedido(slot, pedido):
            return None, "escolha_numerica", "incompativel_com_pedido"
        ok, motivo = cal.is_slot_bookable(tenant, slot, exclude_id=exclude_id)
        if not ok:
            logger.info(f"[{slug}][{phone}] opção {escolha} ficou indisponível: {motivo}")
            return None, "escolha_numerica", motivo
        _ctx.oferta_consumida = info["offer_id"]
        return slot, "escolha_numerica", "ok"

    bruto = data.get("slot_id")
    if bruto not in (None, "", 0):
        slot = _parse_slot_id(bruto)
        if slot is None:
            logger.warning(f"[{slug}][{phone}] slot_id ilegível: {bruto!r}")
            return None, "slot_id", "formato_invalido"
        if not _slot_atende_pedido(slot, pedido):
            logger.warning(
                f"[{slug}][{phone}] slot_id {slot.isoformat()} não atende o pedido "
                f"(data={pedido.get('date')}, período={pedido.get('period')})"
            )
            return None, "slot_id", "incompativel_com_pedido"
        ok, motivo = cal.is_slot_bookable(tenant, slot, exclude_id=exclude_id)
        if not ok:
            logger.info(f"[{slug}][{phone}] slot_id {slot.isoformat()} indisponível: {motivo}")
            return None, "slot_id", motivo
        return slot, "slot_id", "ok"

    bruto = data.get("slot_index")
    if bruto is not None:
        try:
            idx = int(bruto) - 1
        except (TypeError, ValueError):
            logger.warning(f"[{slug}][{phone}] slot_index inválido: {bruto!r}")
            return None, "slot_index", "indice_invalido"
        _ctx_of = db.offer_context(tenant["id"], phone)
        oferta = (_ctx_of["ativa"] or {}).get("slots") if not _ctx_of["ambigua"] else None
        if not oferta:
            logger.warning(f"[{slug}][{phone}] slot_index={bruto} sem oferta "
                           f"persistida — pedindo esclarecimento")
            return None, "slot_index", "sem_oferta"
        if not 0 <= idx < len(oferta):
            logger.warning(f"[{slug}][{phone}] slot_index={bruto} fora da oferta "
                           f"({len(oferta)} opções)")
            return None, "slot_index", "fora_da_oferta"
        slot = oferta[idx]
        if not _slot_atende_pedido(slot, pedido):
            return None, "slot_index", "incompativel_com_pedido"
        ok, motivo = cal.is_slot_bookable(tenant, slot, exclude_id=exclude_id)
        if not ok:
            logger.info(f"[{slug}][{phone}] opção {bruto} da oferta ficou "
                        f"indisponível: {motivo}")
            return None, "slot_index", motivo
        return slot, "slot_index", "ok"

    return None, "ausente", "ausente"


# Motivos de recusa que significam "o horário existe, mas não dá" — nesses casos
# vale oferecer alternativas. Os demais pedem esclarecimento.
_MOTIVO_PT = {
    "ocupado": "esse horário já está reservado",
    "passado": "esse horário já passou",
    "dia_nao_atendido": "não atendemos nesse dia",
    "fora_expediente": "esse horário está fora do expediente",
    "horario_bloqueado": "esse horário não está disponível na agenda",
    "data_bloqueada": "essa data está bloqueada na agenda",
}


# Máximo de opções numa oferta. Mais que isso vira parede de texto no WhatsApp.
_MAX_OFERTA = 6


def _ofertar(tenant: dict, phone: str, slots: list, abertura: str) -> str:
    """Monta o texto numerado que o paciente vai ver **e** persiste exatamente a
    mesma sequência, na mesma ordem.

    Esta é a única forma de exibir horários ao paciente. O texto do modelo não
    serve como oferta: ele podia numerar 1=dia A e 2=dia B enquanto os ids
    chegavam em outra ordem — ou um id era descartado por inválido e a numeração
    do texto continuava a antiga. Nos dois casos o "1" do paciente reservava
    outra opção, em silêncio.
    """
    slots = list(slots)[:_MAX_OFERTA]
    # A oferta nasce em `draft`: ela só passa a valer para resolver "quero o 1"
    # quando o envio for aceito pelo provedor (E03). Enquanto isso, a oferta
    # que o paciente JÁ recebeu continua sendo a válida.
    nova = db.offer_create(tenant["id"], phone, slots, event_row_id=_evento_atual())
    _ctx.offer_id = nova["offer_id"]
    linhas = "\n".join(f"  {i}. {s}" for i, s in enumerate(cal.format_slots(slots), 1))
    logger.info(f"[{tenant.get('slug')}][{phone}] oferta {nova['offer_id'][:8]} "
                f"(seq {nova['seq']}, draft) com {len(slots)} opção(ões): "
                f"{[s.isoformat() for s in slots]}")
    return f"{abertura}\n{linhas}\n\nQual prefere? 😊"


def _confirmacao_criacao(tenant: dict, nome: str, slot: datetime,
                         appt_id: int) -> str:
    """Confirmação canônica de um agendamento criado.

    Extraída para que a reentrada (evento que já criou a consulta) produza
    EXATAMENTE o mesmo texto da primeira execução — a resposta persistida no
    evento não pode variar entre tentativas.
    """
    formatted = cal.format_slots([slot])[0]
    _nome1 = (nome or "").split()[0] if (nome or "").strip() else ""
    _saud = f"Prontinho, {_nome1}! ✅" if _nome1 else "Prontinho! ✅"
    if _is_psychology(tenant):
        reply = (f"{_saud} Sua sessão ficou marcada para *{formatted}*. "
                 f"Vou te lembrar um dia antes, combinado? 💖")
    else:
        reply = (f"{_saud} Seu {_generic_service_noun(tenant)} ficou marcado "
                 f"para *{formatted}*. Vou te lembrar um dia antes, combinado? 💖")
    return _confirm_today_tail(reply, slot, tenant, appt_id)


def _primeiro_nome(tenant: dict, phone: str) -> str:
    try:
        p = db.get_patient(tenant["id"], phone)
        nome = (p.get("name") if p else "") or ""
        return nome.split()[0] if nome.strip() else ""
    except Exception:
        return ""


def _abertura_oferta(tenant: dict, phone: str, pedido: dict | None) -> str:
    """Abertura canônica da oferta: escrita pelo servidor, com o dia e o período
    que o paciente pediu. Não reaproveita o texto do modelo, que é justamente a
    fonte de divergência que esta etapa eliminou."""
    pedido = pedido or {}
    nome = _primeiro_nome(tenant, phone)
    saud = f"Claro, {nome}! 😊 " if nome else "Claro! 😊 "
    alvo = pedido.get("date")
    periodo = {"manha": "de manhã", "tarde": "à tarde", "noite": "à noite"}.get(
        pedido.get("period") or "", "")
    if alvo is not None:
        dia = cal.format_slots(
            [datetime(alvo.year, alvo.month, alvo.day, 12, 0)])[0].split(" às ")[0]
        alvo_txt = f"{dia}" + (f" {periodo}" if periodo else "")
        return f"{saud}Para {alvo_txt} tenho estes horários:"
    if periodo:
        return f"{saud}{periodo.capitalize()} tenho estes horários:"
    return f"{saud}Tenho estes horários:"


def _ids_ofertaveis(data: dict, tenant: dict, pedido: dict | None,
                    exclude_id: int | None = None) -> list:
    """Dos ids que o modelo declarou, os que podem de fato ser oferecidos.

    Mantém a ordem declarada, descarta formato inválido, duplicata, horário
    incompatível com o pedido e horário indisponível agora. O que sobrar é ao
    mesmo tempo o que o paciente vê e o que fica persistido — por isso o texto
    nunca pode ser reaproveitado do modelo.
    """
    brutos = data.get("offered_ids") or data.get("slot_ids") or []
    if not isinstance(brutos, (list, tuple)):
        return []
    vistos, saida = set(), []
    for b in brutos:
        s = _parse_slot_id(b)
        if s is None or s in vistos:
            continue
        if not _slot_atende_pedido(s, pedido):
            continue
        if not cal.is_slot_bookable(tenant, s, exclude_id=exclude_id)[0]:
            continue
        vistos.add(s)
        saida.append(s)
        if len(saida) >= _MAX_OFERTA:
            break
    return saida


def _resposta_sem_agendar(tenant: dict, phone: str, pedido: dict | None,
                          motivo: str, exclude_id: int | None = None) -> str:
    """Resposta quando NÃO se pode agendar. Informa, oferece alternativas e
    deixa a escolha com o paciente — nunca escolhe por ele."""
    pedido = pedido or {}
    alvo = pedido.get("date")
    periodo = pedido.get("period")

    if alvo is not None:
        no_dia = cal.find_slots(tenant, days_ahead=30, on_date=alvo, period=periodo,
                                per_day=5, exclude_id=exclude_id)
        rotulo = alvo.strftime("%d/%m")
        if no_dia:
            # Mesma abertura canônica do caminho de list_slots, para o paciente
            # ver sempre o mesmo formato de oferta.
            return _ofertar(tenant, phone, no_dia,
                            _abertura_oferta(tenant, phone, pedido))
        explicacao = _MOTIVO_PT.get(motivo, "não tenho vaga nesse horário")
        alvo_dt = datetime(alvo.year, alvo.month, alvo.day, 9, 0)
        perto = cal.suggest_slots_near(tenant, alvo_dt, n=3)
        if perto:
            return _ofertar(
                tenant, phone, perto,
                f"Em {rotulo} {explicacao} — não encontrei vaga nesse dia. 😕\n"
                f"Posso te oferecer:",
            )
        db.offer_cancel_drafts(tenant["id"], phone)
        return (f"Em {rotulo} {explicacao} e não encontrei horários próximos. "
                f"Quer me dizer outro dia ou período? 😊")

    # Sem data no pedido: não adivinhar. Ofertar uma amostra de dias e perguntar.
    amostra = cal.find_slots(tenant, days_ahead=14, period=periodo,
                             per_day=2, max_total=6, exclude_id=exclude_id)
    if amostra:
        return _ofertar(tenant, phone, amostra,
                        "Para eu marcar certinho, me diga qual destes serve:")
    db.offer_cancel_drafts(tenant["id"], phone)
    return ("Não consegui identificar o horário que você quer. "
            "Me diz o dia e o período que prefere? 😊")


def _execute_action(tenant: dict, resp: AgentResponse, phone: str = "",
                    pedido: dict | None = None,
                    escolha: int | None = None) -> tuple[str, dict | None]:
    """Executa a ação e retorna (reply_text, evento_opcional).

    `pedido` é o que o paciente pediu, extraído por regra em `_parse_request`
    (data e período). Serve para exigir que o horário escolhido seja compatível
    com o pedido, sem depender do modelo para isso.

    `escolha` é o número de opção que o paciente digitou, extraído do texto dele
    em `_parse_numeric_choice`. Quando presente, manda sobre o `slot_id` do
    modelo.
    """
    tenant_id = tenant["id"]
    action = resp.action
    data = resp.data
    text = resp.response_text

    if action == Action.list_slots:
        # A lista numerada que o paciente vê é construída AQUI, pelo servidor, a
        # partir da mesma sequência de instantes que fica persistida. O
        # `response_text` do modelo é descartado de propósito: ele podia numerar
        # numa ordem e declarar os ids em outra, ou citar horário que não está
        # na oferta — e então o "1" do paciente reservava outra coisa.
        evento = {"type": "new_message", "data": {"phone": phone, "intent": resp.intent}}
        ofertados = _ids_ofertaveis(data, tenant, pedido)
        if ofertados:
            return _ofertar(tenant, phone, ofertados,
                            _abertura_oferta(tenant, phone, pedido)), evento
        # Nenhum id utilizável: não dá para confiar no texto do modelo nem
        # manter a oferta anterior. Monta uma oferta nova, validada, com texto
        # canônico — sem agendar nada.
        logger.warning(f"[{tenant['slug']}][{phone}] list_slots sem id utilizável "
                       f"(declarados={data.get('offered_ids') or data.get('slot_ids')!r}) "
                       f"— gerando oferta canônica")
        db.offer_cancel_drafts(tenant_id, phone)
        return _resposta_sem_agendar(tenant, phone, pedido, "sem_ids"), evento

    if action == Action.cancel:
        if not _mutacao_permitida():
            return _bloquear_mutacao(tenant, phone, "cancel"), None
        # Cancelamento suave: marca cancelled=1 (para lembretes), remove do
        # calendário externo e avisa a psicóloga (notificação feita no main.py
        # via resp.intent == cancel). NUNCA oferece outro horário.
        appt_id = data.get("appointment_id")
        appt = None
        if appt_id:
            try:
                appt = db.get_appointment_by_id(tenant_id, int(appt_id))
            except (TypeError, ValueError):
                appt = None
        # Fallback: se o LLM não passou ID, pega a próxima consulta do paciente.
        if not appt and phone:
            appt = cal.get_next_appointment(tenant_id, phone)
        if appt:
            try:
                _cancelar_idempotente(tenant_id, appt["id"])
            except Exception as e:
                logger.warning(f"[{tenant['slug']}][{phone}] cancel_appointment falhou: {e}")
            # Remover do Google Calendar / CalDAV
            if appt.get("google_event_id"):
                try:
                    if tenant.get("google_refresh_token"):
                        gcal.delete_event(tenant, appt["google_event_id"])
                    else:
                        caldav_svc.delete_event(tenant, appt["google_event_id"])
                except Exception as e:
                    logger.warning(f"[gcal/caldav] delete (cancel) falhou: {e}")
            logger.info(f"[{tenant['slug']}][{phone}] Consulta {appt['id']} cancelada pelo paciente")
        else:
            logger.info(f"[{tenant['slug']}][{phone}] Cancelamento sem consulta futura encontrada")
        # Resposta: acolhe e avisa que a psicóloga entra em contato. NUNCA
        # oferece reagendamento nem cita política de cobrança.
        psy = tenant.get("psychologist_name") or "psicóloga"
        nome = ""
        if appt and appt.get("patient_name"):
            nome = appt["patient_name"].split()[0]
        reply = text or ""
        # Sanitiza: se o LLM escorregou e ofereceu horário/disponibilidade,
        # substitui por mensagem segura de cancelamento.
        low = reply.lower()
        if (not reply.strip()
                or "disponibilidade" in low or "horário" in low or "horario" in low
                or "remarc" in low or "qual dia" in low or "opções" in low or "opcoes" in low):
            saud = f"Entendi, {nome}!" if nome else "Entendi!"
            if _is_psychology(tenant):
                reply = (f"{saud} 😊 Vou avisar a {psy} sobre o cancelamento e ela "
                         f"entra em contato com você em breve. 💖")
            else:
                prof = (tenant.get("psychologist_name") or "").strip() or "o responsável"
                reply = (f"{saud} 😊 Vou avisar {prof} sobre o cancelamento e já "
                         f"retornam o contato com você em breve. 💖")
        return reply, {"type": "appointment_cancelled",
                       "data": {"phone": phone,
                                "patient_name": (appt or {}).get("patient_name", "")}}

    if action == Action.create:
        if not _mutacao_permitida():
            return _bloquear_mutacao(tenant, phone, "create"), None
        # ── Reentrada: este evento já criou a consulta ────────────────────────
        # Um retry (lease vencido, falha depois da mutação, reinício) não pode
        # passar de novo pelas verificações de conflito: a vaga agora está
        # ocupada pela própria consulta criada antes, e o fluxo tomaria outro
        # caminho. Reconstrói a confirmação canônica a partir do que foi gravado.
        _ja = _efeito_do_evento("appointment:create")
        if _ja:
            _appt_ant = db.get_appointment_by_id(tenant_id, int(_ja))
            if _appt_ant:
                _slot_ant = datetime.fromisoformat(_appt_ant["scheduled_at"])
                logger.info(f"[{tenant['slug']}][{phone}] reentrada: consulta "
                            f"{_ja} já criada por este evento — repetindo a "
                            f"confirmação sem nova gravação")
                return _confirmacao_criacao(
                    tenant, _appt_ant.get("patient_name") or "", _slot_ant,
                    int(_ja)), None

        # ── Fase 4b: auto-envio + trava p/ pacientes NOVOS (opt-in, OFF, fail-open) ──
        try:
            import contract_service as _cs_gate
            _bN, _bmsgN = _cs_gate.enforce_new_patient(tenant, phone, "scheduling")
            if _bN:
                logger.info(f"[{tenant['slug']}][{phone}] agendamento bloqueado: paciente novo, contrato auto-enviado")
                return _bmsgN, None
        except Exception:
            logger.exception("enforce_new_patient scheduling fail-open")
        # ── Fase 4: bloqueio por contrato (opt-in, OFF por padrão, fail-open) ──
        try:
            import contract_service as _cs_gate
            _blocked, _bmsg = _cs_gate.blocks_scheduling(tenant, phone)
            if _blocked:
                logger.info(f"[{tenant['slug']}][{phone}] agendamento bloqueado: contrato pendente")
                return _bmsg, None
        except Exception:
            logger.exception("gate scheduling fail-open")
        name = data.get("patient_name", "Paciente")
        slot, _origem, _motivo = _resolve_slot(data, tenant, phone, pedido, escolha=escolha)
        if slot is not None:
            duration = tenant.get("session_minutes", 50)
            # ── GUARDA ANTI-DUPLICATA (reagendamento silencioso) ─────────────────
            # Se o paciente JÁ tem UMA consulta futura real e o LLM escolheu
            # "create" (porque ele escolheu um horário SEM dizer "remarcar" com
            # todas as letras), MOVEMOS a consulta existente em vez de criar uma
            # 2ª. No consultório o paciente tem 1 sessão futura via autoatendimento
            # (confirmações e cobrança assumem "a próxima consulta"). Só aplica com
            # EXATAMENTE 1 consulta ativa: 0 (paciente novo) → cria normal;
            # 2+ (já ambíguo) → deixa como está.
            try:
                _future = [a for a in db.get_appointments_by_phone(tenant_id, phone)
                           if not a.get("cancelled")]
            except Exception:
                _future = []
            if len(_future) == 1 and not db.has_conflict(
                    tenant_id, slot, duration, exclude_id=int(_future[0]["id"])):
                reply = _do_reschedule(tenant, phone, _future[0], slot)
                logger.info(f"[{tenant['slug']}][{phone}] create→remarcação "
                            f"(evita duplicata) id={_future[0]['id']} → {slot.isoformat()}")
                return reply, {"type": "new_message",
                               "data": {"phone": phone, "intent": "reschedule"}}
            if not db.has_conflict(tenant_id, slot, duration):
                appt_id, _criada = _criar_consulta_idempotente(
                    tenant_id, name, phone, slot)
                # Sincronizar com Google Calendar (se conectado)
                try:
                    event_id = gcal.create_event(tenant, name, slot.isoformat(), duration)
                    if event_id:
                        db.set_appointment_google_event_id(appt_id, event_id)
                except Exception as e:
                    logger.warning(f"[gcal] create_event falhou: {e}")
                    _pendencia_externa(tenant, phone, "calendar:create", e)
                # Sincronizar com CalDAV (se configurado)
                try:
                    caldav_uid = caldav_svc.create_event(tenant, name, slot.isoformat(), duration)
                    if caldav_uid:
                        # Reutilizamos google_event_id para CalDAV quando Google não está conectado
                        if not tenant.get("google_refresh_token"):
                            db.set_appointment_google_event_id(appt_id, caldav_uid)
                except Exception as e:
                    logger.warning(f"[caldav] create_event falhou: {e}")
                    _pendencia_externa(tenant, phone, "calendar:create", e)
                # ── Confirmação CANÔNICA ──────────────────────────────────────
                # Construída a partir do horário efetivamente gravado, depois da
                # gravação. Antes o texto do modelo era reescrito por regex para
                # encaixar o horário escolhido pelo backend — o que apresentava
                # um agendamento em outro dia como se fosse o pedido. A troca de
                # texto escondia o defeito em vez de impedi-lo.
                formatted = cal.format_slots([slot])[0]
                db.offer_cancel_drafts(tenant_id, phone)
                reply = _confirmacao_criacao(tenant, name, slot, appt_id)
                logger.info(f"[{tenant['slug']}] Agendamento criado: {name} | origem={_origem} | slot={formatted}")
                event = {"type": "new_appointment", "data": {"patient_name": name, "slot": formatted, "phone": phone}}
                return reply, event
        logger.warning(f"[{tenant['slug']}][{phone}] agendamento NÃO realizado "
                       f"(origem={_origem}, motivo={_motivo})")
        return _resposta_sem_agendar(tenant, phone, pedido, _motivo), None

    if action == Action.update:
        if not _mutacao_permitida():
            return _bloquear_mutacao(tenant, phone, "update"), None
        appt_id = data.get("appointment_id")
        try:
            _excl = int(appt_id) if appt_id else None
        except (TypeError, ValueError):
            _excl = None
        slot, _origem, _motivo = _resolve_slot(data, tenant, phone, pedido,
                                               exclude_id=_excl, escolha=escolha)
        if appt_id and slot is not None:
            duration = tenant.get("session_minutes", 50)
            if not db.has_conflict(tenant_id, slot, duration, exclude_id=int(appt_id)):
                # Buscar google_event_id antes de atualizar
                appt = db.get_appointment_by_id(tenant_id, appt_id)
                _remarcar_idempotente(tenant_id, int(appt_id), slot)
                # ── Sincronizar com Google Calendar / CalDAV ─────────────────
                # Antes só atualizava se já existisse google_event_id; isso
                # quebrava remarcações de pacientes cuja consulta foi criada
                # ANTES da integração com o GCal (event_id vazio). Agora:
                #   1. Se houver event_id → tenta update
                #   2. Se update falhar OU event_id estiver vazio → cria evento
                #      novo no horário novo e persiste o event_id.
                # O evento antigo (se órfão) fica no GCal e a Bruna remove
                # manualmente — preferível a perder o novo agendamento.
                patient_name = appt.get("patient_name", "Paciente") if appt else "Paciente"
                duration_min = tenant.get("session_minutes", 50)
                gcal_ok = False
                if tenant.get("google_refresh_token"):
                    try:
                        if appt and appt.get("google_event_id"):
                            gcal_ok = gcal.update_event(
                                tenant, appt["google_event_id"], patient_name,
                                slot.isoformat(), duration_min,
                            )
                        if not gcal_ok:
                            new_id = gcal.create_event(
                                tenant, patient_name, slot.isoformat(), duration_min,
                            )
                            if new_id:
                                db.set_appointment_google_event_id(int(appt_id), new_id)
                                gcal_ok = True
                                logger.info(f"[gcal] reschedule fallback create: novo event_id={new_id}")
                    except Exception as e:
                        logger.warning(f"[gcal] sincronização de update falhou: {e}")
                        _pendencia_externa(tenant, phone, "calendar:update", e)
                # CalDAV apenas quando Google não está ativo
                if not tenant.get("google_refresh_token"):
                    try:
                        cd_ok = False
                        if appt and appt.get("google_event_id"):
                            cd_ok = caldav_svc.update_event(
                                tenant, appt["google_event_id"], patient_name,
                                slot.isoformat(), duration_min,
                            )
                        if not cd_ok:
                            new_uid = caldav_svc.create_event(
                                tenant, patient_name, slot.isoformat(), duration_min,
                            )
                            if new_uid:
                                db.set_appointment_google_event_id(int(appt_id), new_uid)
                    except Exception as e:
                        logger.warning(f"[caldav] sincronização de update falhou: {e}")
                        _pendencia_externa(tenant, phone, "calendar:update", e)
                # ── Confirmação CANÔNICA ──────────────────────────────────────
                # Montada do horário gravado, depois da gravação bem-sucedida.
                # O horário NOVO vem primeiro e em negrito; o antigo fica num
                # parêntese no fim. Antes a frase abria com o horário antigo
                # ("Sua consulta de 10:00 foi remarcada para 15:00") e o
                # paciente lia o antigo como se fosse o novo. E antes disso o
                # texto do modelo era reescrito por regex, o que mascarava
                # remarcação para um dia que o paciente nunca escolheu.
                formatted = cal.format_slots([slot])[0]
                db.offer_cancel_drafts(tenant_id, phone)
                old_fmt = cal.format_appointment(appt) if appt else ""
                if _is_psychology(tenant):
                    reply = f"Prontinho! ✅ Sua consulta foi remarcada para *{formatted}*."
                else:
                    _svc = _generic_service_noun(tenant)
                    reply = f"Prontinho! ✅ Seu {_svc} foi remarcado para *{formatted}*."
                if old_fmt:
                    reply += f" (Antes era {old_fmt}.)"
                reply += " Vou te lembrar um dia antes, combinado? 💖"
                reply = _confirm_today_tail(reply, slot, tenant, appt_id)
                return reply, {"type": "new_message", "data": {"phone": phone, "intent": "reschedule"}}
        logger.warning(f"[{tenant['slug']}][{phone}] remarcação NÃO realizada "
                       f"(origem={_origem}, motivo={_motivo})")
        return _resposta_sem_agendar(tenant, phone, pedido, _motivo, exclude_id=_excl), None

    if action == Action.confirm:
        if not _mutacao_permitida():
            return _bloquear_mutacao(tenant, phone, "confirm"), None
        # ── Fase 4b: auto-envio + trava p/ pacientes NOVOS (opt-in, OFF, fail-open) ──
        try:
            import contract_service as _cs_gate
            _bN, _bmsgN = _cs_gate.enforce_new_patient(tenant, phone, "confirmation")
            if _bN:
                logger.info(f"[{tenant['slug']}][{phone}] confirmação bloqueada: paciente novo, contrato auto-enviado")
                return _bmsgN, None
        except Exception:
            logger.exception("enforce_new_patient confirmation fail-open")
        # ── Fase 4: bloqueio por contrato (opt-in, OFF por padrão, fail-open) ──
        try:
            import contract_service as _cs_gate
            _blocked, _bmsg = _cs_gate.blocks_confirmation(tenant, phone)
            if _blocked:
                logger.info(f"[{tenant['slug']}][{phone}] confirmação bloqueada: contrato pendente")
                return _bmsg, None
        except Exception:
            logger.exception("gate confirmation fail-open")
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
                    # Lista amigável e NUMERADA (nunca expor id interno do banco
                    # ao paciente). O paciente responde com a data/horário e o LLM
                    # resolve o appointment_id na próxima rodada.
                    lista = "\n".join(
                        f"  {i}. {cal.format_appointment(a)}"
                        for i, a in enumerate(futuras[:5], start=1)
                    )
                    if _is_psychology(tenant):
                        msg = f"Você tem mais de uma sessão agendada 😊 Qual você quer confirmar?\n\n{lista}\n\nÉ só me dizer a data. 💖"
                    else:
                        msg = f"Você tem mais de um {_generic_service_noun(tenant)} agendado 😊 Qual você quer confirmar?\n\n{lista}\n\nÉ só me dizer a data. 💖"
                    return (msg, None)
            except Exception as e:
                logger.warning(f"[confirm] fallback get_appointments_by_phone falhou: {e}")
        if appt_id:
            _confirmar_idempotente(tenant_id, int(appt_id))
            logger.info(f"[{tenant['slug']}][{phone}] confirmed appointment id={appt_id}")
        else:
            logger.warning(f"[{tenant['slug']}][{phone}] Action.confirm sem appointment_id e sem próxima consulta")

    return text, {"type": "new_message", "data": {"phone": phone, "intent": resp.intent}}
