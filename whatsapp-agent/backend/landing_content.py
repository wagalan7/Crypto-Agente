"""Conteúdo das landings por vertical (Track B — aditivo).

Gera o "pacote de conteúdo" (copy) de uma landing dedicada por segmento, a
partir dos termos resolvidos em ``segments.resolve_terms``. É usado só pela
rota ``GET /p/{segment}`` + ``landing_vertical.html``.

Regra de honestidade: para segmentos sem clientes reais, NÃO inventamos
depoimentos nem métricas ("40% menos faltas", "30 psicólogas"). A landing de
psicologia (``/`` + ``landing.html``) continua byte-idêntica e é a única com
prova social nominal. Aqui usamos copy de benefício, não de prova fabricada.
"""
from __future__ import annotations

from segments import resolve_terms, normalize_segment, SEGMENT_PRESETS, _pluralize_pt


def _art(business_type: str) -> dict[str, str]:
    """Artigos/preposições PT-BR para o tipo de negócio (o/a, do/da, no/na)."""
    fem = business_type.strip().lower().endswith("a")  # clínica, barbearia, oficina
    if fem:
        return {"o": "a", "do": "da", "no": "na", "seu": "sua"}
    return {"o": "o", "do": "do", "no": "no", "seu": "seu"}


def content_for(segment: str | None) -> dict | None:
    """Pacote de conteúdo da landing de um segmento.

    Retorna ``None`` para psicologia (usa a landing flagship em ``/``) ou para
    segmento inválido — a rota trata isso redirecionando para ``/``.
    """
    seg = normalize_segment(segment)
    if seg == "psicologia":
        return None

    terms = resolve_terms({"segment": seg})
    prof = terms["professional_label"]                 # ex.: "Nutricionista"
    prof_l = prof.lower()
    client = terms["client_noun"]                      # ex.: "paciente" / "cliente" / "tutor"
    clients = _pluralize_pt(client)                    # pacientes / clientes / tutores
    service = terms["service_noun"]                    # consulta / sessão / atendimento...
    services = terms["service_noun_plural"]            # consultas / sessões...
    biz = terms["business_type"]                       # consultório / escritório / salão...
    a = _art(biz)
    display = SEGMENT_PRESETS[seg]["display"]

    return {
        "segment": seg,
        "brand_line2": "ATENDIMENTO",
        "whatsapp": "5511968439527",
        "onboarding_link": f"/onboarding?segment={seg}",

        # termos crus (para o chat-demo do template)
        "t_professional": prof,
        "t_client": client,
        "t_clients": clients,
        "t_service": service,
        "t_services": services,
        "t_business": biz,

        # ── SEO ──
        "seo_title": f"Agente no WhatsApp para {display} — IA que agenda e confirma",
        "seo_description": (
            f"Agente de IA que agenda, confirma e remarca {services} automaticamente "
            f"pelo WhatsApp {a['do']} {biz}. Menos faltas, mais tempo para "
            f"os {clients}."
        ),

        # ── HERO ──
        "hero_pill": "Atendimento automático no seu WhatsApp",
        "hero_title_1": "Sua agenda cheia.",
        "hero_title_2": "Sua cabeça livre.",
        "hero_subtitle": (
            f"Um agente de IA que fica no WhatsApp {a['do']} {biz} — "
            f"agenda, confirma e remarca {services} automaticamente, "
            f"enquanto você cuida dos seus {clients}."
        ),
        "cta_primary": "🚀 Quero meu agente agora",
        "hero_img_alt": f"Agente de {display} atendendo {clients} no WhatsApp",

        # ── NÚMEROS (honestos, sem métrica fabricada) ──
        "numbers": [
            {"value": "24h", "label": "Disponível todo dia"},
            {"value": "2 min", "label": "Para configurar"},
            {"value": "R$ 0", "label": "Custo de secretária"},
            {"value": "100%", "label": "No seu próprio número"},
        ],

        # ── PROBLEMA ──
        "problem_kicker": "Você se identifica?",
        "problem_headline": "Você está no meio de um atendimento.",
        "problem_paragraphs": [
            f"O celular vibra. Mensagem de {client}. Você ignora e tenta focar.",
            "O atendimento termina e você pega o celular. "
            "<strong class=\"text-gray-900\">Três mensagens esperando.</strong>",
            f"Uma querendo marcar {service}. Uma confirmando. Uma pedindo para remarcar.",
            f"Você responde correndo, com o próximo {client} já esperando.",
        ],
        "problem_final": "Isso acontece todos os dias.",
        "pain_cards": [
            {"emoji": "😤", "title": "Tempo e foco perdidos",
             "text": "Ficar de olho no WhatsApp entre um atendimento e outro tira "
                     "sua atenção do que realmente importa."},
            {"emoji": "💸", "title": "Horário vago não volta",
             "text": "Cada falta sem aviso é um horário reservado que ninguém ocupa — "
                     "e você não recupera."},
            {"emoji": "⏰", "title": "Trabalho repetitivo",
             "text": f"Confirmar, remarcar e responder dúvidas sobre {services} — "
                     "tarefas que não precisam ser suas."},
        ],

        # ── ANTES × DEPOIS ──
        "before": [
            f"Responde mensagens durante o expediente, perdendo o foco",
            f"Esquece de confirmar {clients} — falta sem aviso",
            "Agenda manual em bloco, planilha ou papel",
            f"Remarca {services} manualmente ao longo do dia",
            "Termina o dia esgotado(a) — mente ainda no celular",
        ],
        "after": [
            "Agente responde em segundos — você focado(a) no atendimento",
            f"Confirmações automáticas 24h antes para todos os {clients}",
            "Agenda digital, atualizada em tempo real com Google Calendar",
            f"Remarcações feitas pelo próprio agente, sem você tocar",
            "Fim do dia leve — energia para o que realmente importa",
        ],

        # ── COMO FUNCIONA ──
        "steps": [
            {"emoji": "⚡", "badge": "PASSO 1", "title": f"Configure {a['seu']} {biz}",
             "text": f"Nome, {prof_l}, horários de atendimento e duração de cada "
                     f"{service}. Um formulário simples."},
            {"emoji": "📱", "badge": "PASSO 2", "title": "Conecte seu WhatsApp",
             "text": "Integre com seu número via Z-API com um QR code. "
                     "Seu número, sua identidade."},
            {"emoji": "🎉", "badge": "PASSO 3", "title": "Relaxe e atenda",
             "text": "O agente cuida de tudo. Você acompanha pelo painel no "
                     "celular ou computador."},
        ],

        # ── RECURSOS ──
        "features_subtitle": f"Uma ferramenta pensada para o dia a dia de quem atende {clients}",
        "features": [
            {"emoji": "🤖", "title": "IA com linguagem humana",
             "text": f"Conversa naturalmente com os {clients}, com emojis e tom "
                     "acolhedor. Não parece robô."},
            {"emoji": "✅", "title": "Confirmações automáticas",
             "text": f"Envia confirmação 24h antes para todos os {clients} e "
                     "registra a resposta no painel."},
            {"emoji": "📅", "title": "Google Calendar integrado",
             "text": f"Cada {service} agendado aparece automaticamente na sua "
                     "agenda do Google. Sem copiar nada."},
            {"emoji": "🔄", "title": "Remarcação automática",
             "text": f"{client.capitalize()} pediu para remarcar? O agente oferece "
                     "novos horários e atualiza a agenda sozinho."},
            {"emoji": "📊", "title": "Painel de controle",
             "text": "Visualize agenda, conversas e histórico pelo celular. "
                     "Pause o agente com um clique."},
            {"emoji": "💳", "title": "Informa PIX e comprovantes",
             "text": "Responde sobre pagamento com seus dados de PIX e aceita "
                     "comprovantes automaticamente."},
            {"emoji": "💰", "title": "Cobrança mensal automática",
             "text": f"No fim do mês, o sistema envia o resumo de {services} e o "
                     f"valor total para cada {client} — sem você fazer nada."},
            {"emoji": "🔀", "title": "Reagendamento pelo painel",
             "text": f"Remarque qualquer {service} direto pelo painel, com "
                     f"notificação automática ao {client} via WhatsApp."},
        ],

        # ── PROVA SOCIAL: honesto → sem depoimentos nominais fabricados ──
        "testimonials": [],

        # ── FAQ (adaptado por termos) ──
        "faq": [
            {"q": "Preciso saber de tecnologia para configurar?",
             "a": f"Não. A configuração é um formulário simples — nome {a['do']} "
                  f"{biz}, seu nome e horários de atendimento. Em menos de 2 minutos "
                  "está pronto e funcionando."},
            {"q": "O agente usa o meu número de WhatsApp?",
             "a": f"Sim. Você conecta o seu próprio número via Z-API. Os {clients} "
                  "conversam diretamente com o seu número, como se fosse você. "
                  "Você mantém controle total."},
            {"q": f"Vai parecer robótico para os meus {clients}?",
             "a": f"Não. O agente usa linguagem natural, com emojis e tom acolhedor. "
                  f"A maioria dos {clients} nem percebe que é automático."},
            {"q": "E se o agente responder algo errado?",
             "a": f"O agente só faz o que foi programado: agendar, confirmar, informar "
                  "PIX e lidar com atrasos. Situações delicadas ele encaminha para "
                  f"você. E você pode pausar o agente para qualquer {client} com 1 clique."},
            {"q": "Posso cancelar a qualquer momento?",
             "a": "Sim. Não há contrato de fidelidade. Cancele quando quiser, "
                  "sem multa e sem burocracia."},
        ],

        # ── CTA FINAL ──
        "final_headline_1": "Pronto(a) para atender",
        "final_headline_2": "no automático?",
        "final_subtitle": (
            "Configure seu agente agora e comece a atender no automático ainda hoje. "
            "Menos de 2 minutos para começar."
        ),
    }
