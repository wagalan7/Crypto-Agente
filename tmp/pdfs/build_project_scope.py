from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, NextPageTemplate, KeepTogether, HRFlowable, ListFlowable, ListItem
)
from reportlab.pdfgen.canvas import Canvas
from datetime import date

OUT = "output/pdf/escopo_projeto_agente_atendimento_whatsapp.pdf"

PURPLE = HexColor("#6D28D9")
PURPLE_DARK = HexColor("#3B176A")
PURPLE_LIGHT = HexColor("#F3E8FF")
VIOLET = HexColor("#8B5CF6")
INK = HexColor("#1F2937")
MUTED = HexColor("#64748B")
LINE = HexColor("#E2E8F0")
BG = HexColor("#F8FAFC")
GREEN = HexColor("#15803D")
GREEN_BG = HexColor("#DCFCE7")
AMBER = HexColor("#B45309")
AMBER_BG = HexColor("#FEF3C7")
RED = HexColor("#B91C1C")
RED_BG = HexColor("#FEE2E2")
BLUE = HexColor("#1D4ED8")
BLUE_BG = HexColor("#DBEAFE")

pdfmetrics.registerFont(TTFont("Arial", "/System/Library/Fonts/Supplemental/Arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Italic", "/System/Library/Fonts/Supplemental/Arial Italic.ttf"))

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="CoverTitle", fontName="Arial-Bold", fontSize=28, leading=32,
                          textColor=colors.white, alignment=TA_LEFT, spaceAfter=9*mm))
styles.add(ParagraphStyle(name="CoverSub", fontName="Arial", fontSize=12, leading=18,
                          textColor=HexColor("#EDE9FE"), alignment=TA_LEFT))
styles.add(ParagraphStyle(name="H1x", fontName="Arial-Bold", fontSize=19, leading=23,
                          textColor=PURPLE_DARK, spaceBefore=2*mm, spaceAfter=5*mm))
styles.add(ParagraphStyle(name="H2x", fontName="Arial-Bold", fontSize=13, leading=16,
                          textColor=PURPLE, spaceBefore=5*mm, spaceAfter=2.5*mm))
styles.add(ParagraphStyle(name="H3x", fontName="Arial-Bold", fontSize=10.5, leading=14,
                          textColor=INK, spaceBefore=3*mm, spaceAfter=1.5*mm))
styles.add(ParagraphStyle(name="Bodyx", fontName="Arial", fontSize=9.2, leading=13.2,
                          textColor=INK, spaceAfter=2.5*mm))
styles.add(ParagraphStyle(name="Small", fontName="Arial", fontSize=7.8, leading=10.5,
                          textColor=MUTED))
styles.add(ParagraphStyle(name="Tiny", fontName="Arial", fontSize=6.7, leading=8.5,
                          textColor=INK))
styles.add(ParagraphStyle(name="TableHead", fontName="Arial-Bold", fontSize=7.5, leading=9,
                          textColor=colors.white, alignment=TA_LEFT))
styles.add(ParagraphStyle(name="TableCell", fontName="Arial", fontSize=7.2, leading=9.4,
                          textColor=INK))
styles.add(ParagraphStyle(name="TableCellBold", fontName="Arial-Bold", fontSize=7.2, leading=9.4,
                          textColor=INK))
styles.add(ParagraphStyle(name="Callout", fontName="Arial-Bold", fontSize=10.5, leading=15,
                          textColor=PURPLE_DARK, alignment=TA_LEFT))
styles.add(ParagraphStyle(name="Phase", fontName="Arial-Bold", fontSize=12, leading=15,
                          textColor=colors.white))

def P(text, style="Bodyx"):
    return Paragraph(text, styles[style])

def bullets(items, level=0):
    return ListFlowable(
        [ListItem(P(x, "Bodyx"), leftIndent=4*mm) for x in items],
        bulletType="bullet", bulletFontName="Arial", bulletFontSize=6,
        bulletColor=PURPLE, leftIndent=5*mm + level*3*mm, bulletIndent=0,
        spaceAfter=2*mm,
    )

def callout(title, text, color=PURPLE_LIGHT):
    t = Table([[P(title, "Callout"), P(text, "Bodyx")]], colWidths=[41*mm, 126*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), color),
        ("BOX", (0,0), (-1,-1), 0.6, PURPLE),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 4*mm),
        ("RIGHTPADDING", (0,0), (-1,-1), 4*mm),
        ("TOPPADDING", (0,0), (-1,-1), 3*mm),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3*mm),
    ]))
    return t

def table(data, widths, header=True, font_size=7.2, row_bgs=None):
    converted = []
    for ri, row in enumerate(data):
        converted.append([P(str(cell), "TableHead" if header and ri == 0 else "TableCell") for cell in row])
    t = Table(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    cmd = [
        ("BACKGROUND", (0,0), (-1,0), PURPLE_DARK) if header else ("BACKGROUND", (0,0), (-1,-1), colors.white),
        ("GRID", (0,0), (-1,-1), 0.35, LINE),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 2.2*mm),
        ("RIGHTPADDING", (0,0), (-1,-1), 2.2*mm),
        ("TOPPADDING", (0,0), (-1,-1), 2*mm),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2*mm),
    ]
    start = 1 if header else 0
    for i in range(start, len(data)):
        cmd.append(("BACKGROUND", (0,i), (-1,i), colors.white if i % 2 else BG))
    if row_bgs:
        for ri, color in row_bgs.items():
            cmd.append(("BACKGROUND", (0,ri), (-1,ri), color))
    t.setStyle(TableStyle(cmd))
    return t

def phase_box(code, title, weeks, objective, color):
    data = [[P(f"{code}  {title}", "Phase"), P(weeks, "Phase")],
            [P(f"<b>Objetivo.</b> {objective}", "Bodyx"), ""]]
    t = Table(data, colWidths=[132*mm, 35*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), color),
        ("SPAN", (0,1), (1,1)),
        ("BOX", (0,0), (-1,-1), 0.6, color),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("ALIGN", (1,0), (1,0), "RIGHT"),
        ("LEFTPADDING", (0,0), (-1,-1), 3*mm),
        ("RIGHTPADDING", (0,0), (-1,-1), 3*mm),
        ("TOPPADDING", (0,0), (-1,-1), 2.5*mm),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2.5*mm),
    ]))
    return t

class NumberedCanvas(Canvas):
    def __init__(self, *args, **kwargs):
        Canvas.__init__(self, *args, **kwargs)
        self._saved = []
    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()
    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.draw_page_number(total)
            Canvas.showPage(self)
        Canvas.save(self)
    def draw_page_number(self, total):
        if self._pageNumber == 1:
            return
        self.setStrokeColor(LINE)
        self.line(20*mm, 15*mm, 190*mm, 15*mm)
        self.setFont("Arial", 7.5)
        self.setFillColor(MUTED)
        self.drawString(20*mm, 10*mm, "Agente de Atendimento - Escopo do Projeto")
        self.drawRightString(190*mm, 10*mm, f"{self._pageNumber} / {total}")

def cover(canvas, doc):
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(PURPLE_DARK)
    canvas.rect(0, 0, w, h, fill=1, stroke=0)
    canvas.setFillColor(PURPLE)
    canvas.circle(w-22*mm, h-22*mm, 58*mm, fill=1, stroke=0)
    canvas.setFillColor(VIOLET)
    canvas.circle(w+8*mm, h-6*mm, 39*mm, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#A78BFA"))
    canvas.roundRect(20*mm, 30*mm, 5*mm, 95*mm, 2.5*mm, fill=1, stroke=0)
    canvas.restoreState()

doc = BaseDocTemplate(
    OUT, pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
    topMargin=18*mm, bottomMargin=20*mm,
    title="Escopo do Projeto - Agente de Atendimento e Assistente Inteligente",
    author="Codex + Alan Malta",
    subject="Plano de evolução, cronograma e prioridades"
)
frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
doc.addPageTemplates([
    PageTemplate(id="cover", frames=frame, onPage=cover),
    PageTemplate(id="body", frames=frame),
])

story = []
story += [Spacer(1, 42*mm), P("PROJETO DE EVOLUÇÃO", "CoverSub"), Spacer(1, 5*mm),
          P("Agente de Atendimento +<br/>Assistente Inteligente", "CoverTitle"),
          P("Escopo executivo e técnico · prioridades · cronograma · critérios de aceite", "CoverSub"),
          Spacer(1, 73*mm),
          P("Versão 1.0", "CoverSub"),
          P("Planejamento acelerado: 10 semanas · execução com Claude Code · entregas incrementais", "CoverSub"),
          P("05 de agosto de 2026", "CoverSub"),
          NextPageTemplate("body"), PageBreak()]

story += [P("1. Resumo executivo", "H1x"),
          P("Este projeto transforma o produto atual em uma plataforma com <b>duas inteligências integradas</b>: um agente que atende pacientes e clientes e um assistente inteligente que ajuda o profissional a administrar o negócio. A estratégia não é competir apenas por respostas com IA; é resolver a rotina operacional, organizar decisões e demonstrar resultado.", "Bodyx"),
          callout("Tese do produto", "O agente deve <b>resolver, não apenas responder</b>. O assistente deve <b>analisar, recomendar e executar com autorização</b>. Ambos compartilham dados confiáveis, políticas e auditoria."),
          P("Objetivos do programa", "H2x"),
          bullets([
              "Eliminar as principais fontes de erros intermitentes: duplicidade, mensagens fora de ordem, conflitos de agenda, falhas silenciosas e divergência com calendários externos.",
              "Criar uma experiência diferenciada: memória útil, fila de espera, recorrência, central de pendências, transferência com resumo e autonomia progressiva.",
              "Entregar um assistente do profissional capaz de responder perguntas sobre o negócio, produzir briefings, identificar riscos e executar comandos autorizados.",
              "Demonstrar valor financeiro: faltas evitadas, vagas recuperadas, tempo poupado, cobranças acompanhadas e receita preservada.",
              "Preparar o SaaS para crescer com segurança, observabilidade, testes e arquitetura compatível com múltiplos clientes."
          ]),
          P("Marcos previstos", "H2x"),
          table([
              ["Marco", "Prazo", "Resultado esperado"],
              ["M1 - Operação estabilizada", "Semana 3", "Conversas ordenadas, agenda protegida, erros rastreáveis e fluxo crítico testado."],
              ["M2 - Assistente inteligente", "Semana 4", "Consultas em linguagem natural, briefing, ações com aprovação e handoff."],
              ["M3 - Diferenciação operacional", "Semana 6", "Fila de espera, recorrência, memória e central de pendências."],
              ["M4 - Produto orientado a resultado", "Semana 7", "Prevenção de faltas, recomendações e métricas financeiras."],
              ["M5 - V1 validada", "Semana 10", "Segurança, piloto, documentação e operação comercial padronizada."],
          ], [42*mm, 25*mm, 100*mm]),
          P("Premissa de planejamento", "H2x"),
          P("Cronograma estimado para <b>um desenvolvedor principal usando Claude Code de forma intensiva</b>, com validação semanal do responsável pelo produto e acesso a um cliente-piloto. A IA acelera implementação, testes, refatoração e documentação. Observação em produção, validação humana e janela de piloto não devem ser eliminadas, pois protegem agenda, pagamentos e experiência de clientes reais.", "Bodyx"),
          PageBreak()]

story += [P("2. Situação atual e decisão inicial", "H1x"),
          P("O produto já possui agenda, confirmação, remarcação, cancelamento, cobrança, contratos, áudio, imagem, dashboard, integrações e rotinas administrativas. Isso é uma vantagem relevante: o projeto parte de uma base funcional e de uso real, não de uma ideia inicial.", "Bodyx"),
          callout("Atenção antes de desenvolver", "A documentação descreve a versão avançada de aproximadamente 1.706 linhas no agente, enquanto a área de trabalho aberta contém uma versão antiga de aproximadamente 190 linhas. A primeira atividade obrigatória é definir a <b>branch canônica</b>, confirmar a build realmente implantada e impedir que melhorias sejam feitas sobre uma cópia desatualizada.", AMBER_BG),
          P("Forças atuais", "H2x"),
          bullets([
              "Produto multi-tenant já em uso e com cliente real.",
              "Conhecimento acumulado sobre psicologia e outros negócios com agenda.",
              "Ações determinísticas para fluxos críticos e proteções contra algumas alucinações.",
              "Cobrança, contratos e calendário dentro do mesmo produto.",
              "Dashboard administrativo e dados suficientes para evoluir para inteligência operacional."
          ]),
          P("Fragilidades prioritárias", "H2x"),
          table([
              ["Risco", "Efeito percebido", "Tratamento planejado"],
              ["Mensagem atual duplicada no contexto", "IA interpreta repetição e responde fora de contexto", "Correção do histórico e teste automatizado."],
              ["Mensagens paralelas do mesmo telefone", "Respostas fora de ordem e ações concorrentes", "Fila por tenant+telefone e agrupamento curto."],
              ["Reserva não atômica", "Dois clientes podem disputar o mesmo horário", "Transação e proteção no banco."],
              ["Slots atravessam dias pela duração", "Horários com minutos estranhos ou fora do expediente", "Gerador diário determinístico."],
              ["IA síncrona no fluxo principal", "Lentidão, timeout e retries", "Worker/fila e limites de tempo."],
              ["Jobs dentro da aplicação", "Duplicidade em deploy ou múltiplas réplicas", "Reserva idempotente e worker dedicado."],
              ["Calendário externo sem reconciliação", "Sistema e Google/CalDAV divergem", "Outbox, retry e status de sincronização."],
              ["SQLite sob concorrência crescente", "Locks e falhas intermitentes", "WAL no curto prazo e migração planejada."],
          ], [43*mm, 55*mm, 69*mm]),
          P("Princípios de execução", "H2x"),
          bullets([
              "Não ampliar funcionalidades antes de estabilizar os fluxos que alteram agenda, contrato e pagamento.",
              "Toda ação crítica deve ser validada pelo código, idempotente, auditável e testável.",
              "Entregas pequenas em piloto; ativação gradual por tenant e por funcionalidade.",
              "A IA interpreta e redige; o sistema determina o que é verdade e o que foi executado."
          ]),
          PageBreak()]

story += [P("3. Arquitetura de produto desejada", "H1x"),
          P("O desenho-alvo separa conversação de execução. Essa separação reduz alucinações e permite trocar modelos de IA sem comprometer a agenda ou o financeiro.", "Bodyx"),
          table([
              ["Camada", "Responsabilidade", "Exemplo"],
              ["Entrada e fila", "Normalizar, deduplicar, agrupar e ordenar mensagens", "Três mensagens curtas viram uma única interação."],
              ["Estado da conversa", "Registrar intenção, dados coletados, pendências e próximo passo", "Aguardando escolha de horário; não pede nome novamente."],
              ["IA de compreensão", "Classificar intenção e extrair informações com confiança", "Data desejada, período, serviço, motivo de handoff."],
              ["Motor de políticas", "Decidir o que pode ser feito e quando exige humano", "Pagamento nunca é confirmado apenas por imagem."],
              ["Ferramentas operacionais", "Consultar/reservar agenda, contrato, cobrança e calendário", "Reserva atômica usando token de slot."],
              ["IA de redação", "Produzir mensagem natural usando apenas dados validados", "Confirma o horário retornado pela agenda."],
              ["Entrega e outbox", "Enviar, repetir com segurança e registrar entrega", "Mensagem com status pendente, enviada ou falhou."],
              ["Auditoria e qualidade", "Explicar decisões, medir erros e criar regressões", "Correção do cliente vira teste permanente."],
          ], [34*mm, 70*mm, 63*mm]),
          P("Contrato estruturado de decisão", "H2x"),
          P("A resposta interna da IA deve conter intenção, confiança, dados coletados, dados faltantes, operação solicitada e necessidade de handoff. Ela não deve afirmar que uma consulta foi marcada antes da ferramenta confirmar. Horários devem ser selecionados por <b>token temporário</b>, não por índice numérico frágil.", "Bodyx"),
          callout("Regra de ouro", "Uma mensagem elegante nunca pode esconder uma operação que falhou. Primeiro o sistema executa e confirma; depois a IA comunica."),
          P("Estados mínimos da conversa", "H2x"),
          table([
              ["Estado", "Entrada esperada", "Saída segura"],
              ["Identificação", "Nome ou reconhecimento pelo telefone", "Cliente identificado ou handoff."],
              ["Coleta de preferência", "Dia, período, serviço ou profissional", "Consulta de agenda filtrada."],
              ["Oferta de horários", "Escolha entre tokens válidos", "Reserva temporária/validação."],
              ["Confirmação da ação", "Aceite explícito quando necessário", "Criação ou remarcação atômica."],
              ["Pendência humana", "Assunto sensível, negociação ou baixa confiança", "Resumo e transferência."],
              ["Concluído", "Operação confirmada", "Mensagem final + próximo lembrete."],
          ], [42*mm, 57*mm, 68*mm]),
          PageBreak()]

story += [P("4. Produto com duas inteligências integradas", "H1x"),
          P("A experiência será dividida por público e responsabilidade. O <b>Agente de Atendimento</b> conversa com pacientes e clientes. O <b>Assistente Inteligente</b> conversa com o profissional, interpreta os dados do negócio e o ajuda a decidir e agir. Eles compartilham a mesma fonte de verdade, mas usam permissões, políticas e prompts separados.", "Bodyx"),
          table([
              ["Dimensão", "Agente de Atendimento", "Assistente Inteligente"],
              ["Usuário", "Paciente ou cliente no WhatsApp", "Profissional, gestor ou equipe autorizada"],
              ["Missão", "Atender, orientar e concluir jornadas", "Organizar, analisar, recomendar e executar"],
              ["Exemplos", "Agendar, remarcar, confirmar, cancelar, contrato e cobrança", "Briefing, pendências, ocupação, previsão, bloqueios e ações"],
              ["Autonomia", "Definida por fluxo e risco", "Consulta livre; alterações com política de aprovação"],
              ["Memória", "Preferências e contexto do relacionamento", "Preferências operacionais e decisões do profissional"],
              ["Saída segura", "Mensagem baseada em operação confirmada", "Resposta com evidência, recomendação e impacto"],
          ], [30*mm, 68.5*mm, 68.5*mm]),
          P("Capacidades do Assistente Inteligente", "H2x"),
          table([
              ["Capacidade", "Exemplos de uso"],
              ["Perguntas sobre o negócio", "Como está minha agenda amanhã? Quem ainda não confirmou? Quanto tenho previsto para receber?"],
              ["Briefing proativo", "Atendimentos do dia, riscos, vagas, pagamentos, contratos e conversas aguardando humano."],
              ["Recomendações", "Preencher uma vaga, contatar paciente sem retorno, abrir faixa extra ou revisar aumento de faltas."],
              ["Comandos operacionais", "Bloquear período, preparar mensagens, oferecer vaga, pausar agente e criar lembrete."],
              ["Aprovação inteligente", "Executar consultas automaticamente; pedir confirmação para envio, alteração de agenda ou ação financeira."],
              ["Explicação", "Mostrar dados usados, regra aplicada, impacto previsto e resultado da execução."],
              ["Resumo de handoff", "Entregar ao profissional contexto, intenção, tentativas anteriores e decisão necessária."],
          ], [48*mm, 119*mm]),
          P("Exemplos de interação", "H2x"),
          bullets([
              "'Quais pacientes estão sem próxima sessão e costumavam vir semanalmente?'",
              "'Bloqueie sexta à tarde. Antes de avisar os pacientes afetados, mostre a mensagem para eu aprovar.'",
              "'Encontrei uma vaga amanhã às 17h. Quem da fila é compatível e quanto de receita podemos recuperar?'",
              "'Faça um fechamento do dia e separe apenas o que precisa da minha decisão.'"
          ]),
          callout("Diferencial central", "Um agente cuida dos clientes; um assistente ajuda o profissional a cuidar do negócio. A integração entre os dois transforma conversas em operação, decisão e resultado."),
          PageBreak()]

story += [P("5. Cronograma mestre - 10 semanas", "H1x"),
          P("As fases são sequenciais onde existe dependência crítica, mas pesquisa, design e preparação de testes podem ocorrer em paralelo. Cada marco deve ser colocado em produção apenas após piloto e validação dos critérios de aceite.", "Bodyx")]

gantt = [["Frente", "1", "2-3", "4", "5-6", "7", "8", "9-10"]]
rows = [
    ("F0 Alinhamento", [1,0,0,0,0,0,0]),
    ("F1 Confiabilidade", [1,1,0,0,0,0,0]),
    ("F2 Qualidade/controle", [0,0,1,0,0,0,0]),
    ("F3 Diferenciação", [0,0,0,1,0,0,0]),
    ("F4 Inteligência", [0,0,0,0,1,0,0]),
    ("F5 Escala/segurança", [0,0,0,0,0,1,0]),
    ("F6 Rollout", [0,0,0,0,0,0,1]),
]
for label, flags in rows:
    gantt.append([label] + [("●" if f else "") for f in flags])
gt = table(gantt, [45*mm] + [17.4*mm]*7)
gt.setStyle(TableStyle([
    ("ALIGN", (1,1), (-1,-1), "CENTER"),
    ("TEXTCOLOR", (1,1), (-1,-1), PURPLE),
    ("FONTSIZE", (1,1), (-1,-1), 12),
]))
story += [gt, Spacer(1, 4*mm),
          table([
              ["Semanas", "Fase", "Entrega de negócio"],
              ["1", "F0 - Alinhamento", "Versão canônica, baseline, métricas e plano de rollout."],
              ["2-3", "F1 - Confiabilidade", "Fluxos críticos ordenados, atômicos e observáveis."],
              ["4", "F2 - Assistente e controle", "Assistente inteligente, autonomia, handoff e aprendizado."],
              ["5-6", "F3 - Diferenciação", "Espera, recorrência, memória e central de pendências."],
              ["7", "F4 - Inteligência", "Prevenção, recomendações e resultado financeiro mensurável."],
              ["8", "F5 - Escala", "Banco/filas/segurança/onboarding e operação preparada para crescimento."],
              ["9-10", "F6 - Rollout", "Piloto ampliado, documentação, treinamento e lançamento da V1."],
          ], [25*mm, 52*mm, 90*mm]),
          P("Cadência recomendada", "H2x"),
          bullets([
              "Segunda: planejamento e escolha das histórias da semana.",
              "Quarta: demonstração interna com conversas sintéticas e regressões.",
              "Sexta: piloto controlado, revisão de métricas, erros e decisão de liberação.",
              "Toda alteração de fluxo crítico exige feature flag, rollback e teste de regressão."
          ]),
          PageBreak()]

story += [P("6. Fase 0 - Alinhamento e linha de base", "H1x"),
          phase_box("F0", "Fundação do projeto", "Semana 1", "Definir exatamente qual versão será evoluída, como medir qualidade e como implantar sem interromper o cliente atual.", PURPLE_DARK),
          P("Entregas", "H2x"),
          bullets([
              "Confirmar repositório, branch canônica, commit e build em produção.",
              "Inventariar configurações do cliente atual sem copiar dados pessoais para ambientes de teste.",
              "Criar staging isolado, dados sintéticos e feature flags por tenant.",
              "Extrair 30 a 50 jornadas anonimizadas: saudação, novo cliente, agenda, confirmação, remarcação, cancelamento, pagamento, áudio e imagem.",
              "Definir painel mínimo de observabilidade: latência, fallback, erros, duplicidades, falha de envio e falha de sincronização.",
              "Registrar baseline de sete dias para comparar antes/depois."
          ]),
          P("Critérios de aceite", "H2x"),
          bullets([
              "Existe uma única referência documentada para produção e desenvolvimento.",
              "Staging recebe webhooks de teste sem atingir pacientes reais.",
              "Suite inicial reproduz ao menos os erros históricos conhecidos.",
              "É possível desativar qualquer funcionalidade nova por tenant sem novo deploy."
          ]),
          P("Métricas-base", "H2x"),
          table([
              ["Métrica", "Definição", "Meta após F1"],
              ["Taxa de erro técnico", "Interações que terminam em fallback por exceção", "< 0,5%"],
              ["Ação incorreta", "Agenda/confirm./cancel. diferente da intenção validada", "0 em fluxos críticos testados"],
              ["Resposta duplicada", "Duas respostas para a mesma mensagem/evento", "< 0,1%"],
              ["Latência P95", "95% das respostas abaixo do limite", "< 8 s texto simples"],
              ["Entrega rastreável", "Mensagens com status final conhecido", "> 99%"],
          ], [42*mm, 85*mm, 40*mm]),
          PageBreak()]

story += [P("7. Fase 1 - Confiabilidade operacional", "H1x"),
          phase_box("F1", "Estável para operar", "Semanas 2-3", "Eliminar as falhas intermitentes que prejudicam confiança e criar garantias técnicas para agenda e mensagens.", RED),
          P("Backlog P0 - ordem de execução", "H2x"),
          table([
              ["#", "Implementação", "Dependência", "Aceite resumido"],
              ["1", "Corrigir duplicação da mensagem atual no histórico", "F0", "Cada mensagem aparece uma vez no contexto."],
              ["2", "Fila e trava por tenant+telefone", "1", "Mensagens do mesmo contato processadas em ordem."],
              ["3", "Agrupamento de mensagens por 2-4 segundos", "2", "Mensagens consecutivas formam uma intenção."],
              ["4", "Idempotência uniforme em todos os webhooks", "2", "Mesmo evento nunca gera duas ações."],
              ["5", "Gerador diário de slots + término no expediente", "F0", "Slots começam na grade configurada e não extrapolam."],
              ["6", "Reserva atômica e token temporário de slot", "5", "Concorrência não cria choque de agenda."],
              ["7", "Worker para IA e timeouts controlados", "2", "Webhook responde rápido e trabalho continua na fila."],
              ["8", "Outbox de envio e status de entrega", "4", "Falha não é registrada como mensagem entregue."],
              ["9", "Outbox de Google/CalDAV + reconciliação", "6", "Divergência aparece e é retentada."],
              ["10", "Jobs com claim atômico e idempotência", "8", "Deploy/múltipla instância não duplica disparos."],
              ["11", "SQLite WAL, busy timeout e retry curto", "F0", "Locks transitórios não derrubam o fluxo."],
              ["12", "Logs correlacionados e alerta por severidade", "2", "Uma interação é rastreável de ponta a ponta."],
          ], [8*mm, 66*mm, 28*mm, 65*mm]),
          P("Testes obrigatórios", "H2x"),
          bullets([
              "Paciente envia três mensagens rápidas; recebe uma resposta coerente.",
              "Dois pacientes tentam o último horário simultaneamente; apenas um agenda.",
              "Webhook é reenviado após timeout; nenhuma resposta ou ação é duplicada.",
              "Modelo demora/falha; webhook não trava e o usuário recebe fallback controlado.",
              "Google Calendar falha; agenda interna permanece consistente e a pendência fica visível.",
              "Deploy ocorre durante janela de confirmação; nenhuma mensagem é enviada duas vezes."
          ]),
          callout("Saída da fase", "Ao final da semana 3, o produto deve ser seguro para operar com o cliente atual, mesmo antes das novas funcionalidades de diferenciação.", GREEN_BG),
          PageBreak()]

story += [P("8. Fase 2 - Assistente inteligente, qualidade e controle", "H1x"),
          phase_box("F2", "Assistente e qualidade controlável", "Semana 4", "Entregar o núcleo do assistente do profissional e transformar regras dispersas em estados, políticas, aprovações e testes.", BLUE),
          P("Escopo P1", "H2x"),
          table([
              ["Frente", "Entrega", "Valor"],
              ["Estado conversacional", "Estados explícitos, campos coletados, faltantes e próximo passo", "Menos repetição e menos dependência do histórico textual."],
              ["Decisão estruturada", "Intenção, confiança, operação solicitada e handoff", "A IA não afirma ação antes da ferramenta."],
              ["Chat do profissional", "Perguntas naturais sobre agenda, pacientes, financeiro e pendências", "Acesso rápido sem navegar por várias telas."],
              ["Ferramentas do assistente", "Consultas seguras e comandos operacionais com aprovação", "Ajuda prática, não apenas respostas."],
              ["Briefing inicial", "Resumo do dia e itens que exigem decisão", "Profissional começa o dia orientado."],
              ["Autonomia progressiva", "Sugestão, assistido, autônomo e proativo por assunto", "Adoção gradual e confiança."],
              ["Handoff inteligente", "Detecção de sensibilidade/frustração/baixa confiança + resumo", "Humano assume com contexto."],
              ["Feedback de resposta", "Correta, poderia melhorar, errada + motivo", "Aprendizado operacional."],
              ["Regressões automáticas", "Correção aprovada vira caso de teste", "Erro corrigido não retorna."],
              ["Linha do tempo", "Decisão, ação, sincronização e entrega", "Transparência e suporte rápido."],
              ["Modo sombra inicial", "IA sugere sem enviar e compara com humano", "Onboarding seguro."],
          ], [40*mm, 75*mm, 52*mm]),
          P("Critérios de aceite", "H2x"),
          bullets([
              "Toda ação crítica registra confiança, política aplicada, resultado e mensagem enviada.",
              "O profissional consulta agenda, confirmações, pendências e previsão sem comandos técnicos.",
              "Comandos de alteração mostram impacto e pedem aprovação conforme a política configurada.",
              "O cliente configura autonomia por categoria sem suporte técnico.",
              "Transferências chegam com resumo, intenção, dados relevantes e pendência exata.",
              "Feedback negativo cria item revisável e pode gerar teste permanente.",
              "Modo sombra produz relatório de cobertura e divergência antes da ativação."
          ]),
          PageBreak()]

story += [P("9. Fase 3 - Diferenciação operacional", "H1x"),
          phase_box("F3", "Operação inteligente vertical", "Semanas 5-6", "Resolver tarefas que plataformas genéricas normalmente exigem configurar manualmente e tornar o produto parte da rotina diária.", GREEN),
          P("Escopo P1", "H2x"),
          table([
              ["Módulo", "Comportamento principal", "Métrica de sucesso"],
              ["Fila de espera", "Registra preferência, encontra vaga compatível, oferece e reserva por prazo", "% de cancelamentos preenchidos."],
              ["Recorrência", "Cria séries semanais/quinzenais, identifica conflitos e trata exceções", "% de séries sem ajuste manual."],
              ["Memória útil", "Preferência de período, modalidade, frequência e nome preferido", "Redução de mensagens até conclusão."],
              ["Central de pendências", "Transforma conversas em tarefas priorizadas com ação rápida", "Pendências resolvidas e tempo poupado."],
              ["Serviços e durações", "Catálogo por tenant, preço informativo, duração e profissional", "% de agendamentos autônomos."],
              ["Exceções de agenda", "Feriados, férias, horários especiais e antecedência mínima", "Zero oferta fora das regras."],
              ["Múltiplos profissionais", "Agenda, serviço, regras e disponibilidade por profissional", "Tenants aptos a ampliar equipe."],
              ["Resumo diário inicial", "Agenda, não confirmados, vagas, pagamentos e contratos", "Abertura/uso do briefing."],
          ], [39*mm, 83*mm, 45*mm]),
          P("Fluxo de fila de espera - aceite mínimo", "H2x"),
          bullets([
              "Paciente registra interesse por dia, período, profissional e modalidade.",
              "Cancelamento dispara correspondência apenas para candidatos compatíveis e autorizados.",
              "Oferta usa reserva temporária; a vaga não é prometida simultaneamente a várias pessoas sem regra explícita.",
              "Ao aceitar, agenda e calendário são atualizados atomicamente; demais interessados recebem tratamento configurável.",
              "Profissional visualiza receita potencial recuperada."
          ]),
          callout("Diferencial percebido", "O cliente deixa de comprar um chatbot e passa a contratar uma operação administrativa especializada, pronta para funcionar sem desenhar fluxos técnicos."),
          PageBreak()]

story += [P("10. Fase 4 - Inteligência e resultado", "H1x"),
          phase_box("F4", "Produto orientado a resultado", "Semana 7", "Usar dados operacionais para prevenir problemas, recomendar ações, recuperar receita e demonstrar retorno sobre o investimento.", AMBER),
          P("Escopo P2", "H2x"),
          bullets([
              "Briefing diário completo: agenda, confirmações, vagas, fila de espera, contratos, comprovantes e exceções.",
              "Assistente responde perguntas comparativas e explica os dados usados em cada conclusão.",
              "Recomendações proativas priorizadas por urgência, impacto e confiança, sempre com ação sugerida.",
              "Prevenção de faltas baseada em sinais operacionais, nunca em diagnóstico sensível.",
              "Sugestão de preenchimento de buracos e melhor ordem de oferta de horários.",
              "Reativação assistida de clientes inativos, sempre com consentimento, frequência limitada e aprovação configurável.",
              "Cobrança conversacional: promessa de pagamento, divergência, pedido de total e encaminhamento humano.",
              "Painel de resultado com tempo poupado, faltas evitadas, vagas recuperadas e receita preservada.",
              "Experimentos controlados de mensagens para confirmação e preenchimento de vagas."
          ]),
          P("Indicadores principais", "H2x"),
          table([
              ["Indicador", "Cálculo", "Uso"],
              ["Resolução autônoma segura", "Conversas concluídas sem humano e sem correção / elegíveis", "Qualidade da automação."],
              ["Mensagens até conclusão", "Média por jornada concluída", "Eficiência e naturalidade."],
              ["Taxa de confirmação", "Consultas confirmadas / confirmações enviadas", "Efetividade dos lembretes."],
              ["Taxa de falta", "Faltas / consultas realizadas no período", "Impacto preventivo."],
              ["Vagas recuperadas", "Cancelamentos preenchidos pela fila", "Receita recuperada."],
              ["Tempo administrativo poupado", "Ações autônomas x tempo médio validado", "ROI para o cliente."],
              ["Correções por 100 conversas", "Feedbacks negativos / conversas x 100", "Saúde do agente."],
              ["Handoff útil", "Transferências resolvidas sem pedir contexto novamente", "Qualidade da colaboração humano-IA."],
          ], [47*mm, 72*mm, 48*mm]),
          PageBreak()]

story += [P("11. Fase 5 - Escala, segurança e produto SaaS", "H1x"),
          phase_box("F5", "Pronto para crescer", "Semana 8", "Reduzir risco operacional e transformar implantação, suporte e governança em processos repetíveis.", PURPLE),
          P("Escopo técnico e operacional", "H2x"),
          table([
              ["Frente", "Implementações"],
              ["Banco e concorrência", "Plano de migração para PostgreSQL; migrações testadas; rollback; índices e isolamento por tenant."],
              ["Filas e workers", "Mensagens, jobs, sincronização e mídia fora do processo web, com retry e dead-letter queue."],
              ["Segurança de webhook", "Token obrigatório para legados, validação oficial por provedor, rotação e alerta de rejeição."],
              ["Acesso", "Evitar token persistente em URL, sessões seguras, expiração, revogação e perfis de permissão."],
              ["LGPD", "Inventário de dados, retenção, exportação, exclusão, base legal, minimização e trilha de acesso."],
              ["Observabilidade", "SLOs, alertas acionáveis, correlação, custos de IA e painel de saúde por tenant."],
              ["Onboarding", "Assistente de configuração, modo sombra, checklist, teste de número e validação da agenda."],
              ["Suporte", "Diagnóstico automático, pacote de evidências sem dados sensíveis e playbooks de incidente."],
          ], [43*mm, 124*mm]),
          P("SLOs sugeridos para V1", "H2x"),
          table([
              ["Compromisso", "Objetivo"],
              ["Disponibilidade mensal", "99,5% na V1; evoluir para 99,9% conforme receita e arquitetura."],
              ["Perda de mensagem aceita", "0; toda entrada válida possui estado final rastreável."],
              ["Duplicidade de ação crítica", "0; idempotência verificada por teste e métrica."],
              ["Resposta de texto P95", "Até 8 segundos em condição normal."],
              ["Recuperação de job", "Retry automático; alerta após limite; retomada manual segura."],
              ["RPO / RTO", "RPO até 24h inicialmente; RTO até 4h, com evolução planejada."],
          ], [67*mm, 100*mm]),
          PageBreak()]

story += [P("12. Fase 6 - Piloto ampliado e lançamento", "H1x"),
          phase_box("F6", "Rollout da V1", "Semanas 9-10", "Validar o produto com uso real, concluir documentação e liberar comercialmente sem colocar todos os clientes em risco ao mesmo tempo.", PURPLE_DARK),
          P("Estratégia de liberação", "H2x"),
          table([
              ["Etapa", "Público", "Critério para avançar"],
              ["Interna", "Dados sintéticos e equipe", "100% dos testes P0; nenhuma falha crítica aberta."],
              ["Sombra", "Cliente atual, sem envio automático novo", "Divergência conhecida e autonomia recomendada."],
              ["Canário", "10-20% das conversas elegíveis", "7 dias sem ação crítica incorreta."],
              ["Piloto", "Cliente atual completo + 1 ou 2 convidados", "14 dias dentro das metas operacionais."],
              ["V1", "Novos clientes do segmento prioritário", "Onboarding, suporte e rollback documentados."],
          ], [26*mm, 60*mm, 81*mm]),
          P("Materiais de lançamento", "H2x"),
          bullets([
              "Manual curto de configuração e operação diária.",
              "Guia de autonomia e quando o humano assume.",
              "Checklist de onboarding e teste de agenda/WhatsApp.",
              "Política de privacidade, retenção e uso responsável de IA.",
              "Painel de resultado e relatório mensal compartilhável.",
              "Playbook de suporte, incidentes e rollback."
          ]),
          P("Definição de pronto da V1", "H2x"),
          bullets([
              "Nenhuma ação crítica incorreta no piloto de 14 dias.",
              "Erros técnicos abaixo de 0,5% e duplicidade abaixo de 0,1%.",
              "Cliente consegue configurar agenda, autonomia e handoff sem alteração de código.",
              "Resultado operacional é mensurável e compreensível no painel.",
              "Suporte consegue diagnosticar uma interação sem acessar conteúdo desnecessário."
          ]),
          PageBreak()]

story += [P("13. Backlog consolidado por prioridade", "H1x"),
          P("A classificação usa: <b>P0</b> bloqueia confiabilidade; <b>P1</b> cria o núcleo do produto; <b>P2</b> aumenta diferenciação/resultado; <b>P3</b> é expansão posterior.", "Bodyx"),
          table([
              ["Pri.", "Itens"],
              ["P0", "Branch/build canônica; histórico sem duplicação; fila por contato; agrupamento; dedup; slots corretos; reserva atômica; worker de IA; outbox; jobs idempotentes; logs; testes críticos."],
              ["P1", "Estados; assistente do profissional; consultas naturais; ferramentas com aprovação; briefing; autonomia progressiva; handoff; feedback; regressões; fila de espera; recorrência; memória; pendências."],
              ["P2", "Recomendações proativas; explicações; múltiplos profissionais; prevenção de faltas; otimização de agenda; cobrança conversacional; métricas de ROI; PostgreSQL; filas duráveis; onboarding."],
              ["P3", "Multicanal; white-label; marketplace de integrações; voz/ligação; campanhas avançadas; expansão ampla de segmentos; recomendações preditivas sofisticadas."],
          ], [16*mm, 151*mm]),
          P("Fora do escopo inicial", "H2x"),
          bullets([
              "Diagnóstico, aconselhamento clínico ou decisão médica.",
              "Confirmação automática de pagamento baseada apenas em imagem/comprovante.",
              "Campanhas massivas sem consentimento, governança e templates oficiais.",
              "Expansão simultânea e profunda para todos os 13 segmentos.",
              "Aplicativo móvel nativo antes de validar a experiência web responsiva.",
              "Substituição completa do atendimento humano em situações sensíveis ou ambíguas."
          ]),
          P("Segmentos recomendados", "H2x"),
          P("Priorizar inicialmente <b>psicologia, nutrição e fisioterapia</b>, que compartilham agenda recorrente, confirmação, faltas, cobrança por sessão e necessidade de comunicação cuidadosa. A expansão deve ocorrer por pacotes verticais validados, não apenas por troca de rótulos no prompt.", "Bodyx"),
          PageBreak()]

story += [P("14. Governança e papéis", "H1x"),
          table([
              ["Papel", "Responsabilidades"],
              ["Responsável pelo produto", "Prioridade, regras de negócio, validação semanal, aprovação de piloto e decisão de rollout."],
              ["Desenvolvimento", "Arquitetura, implementação, testes, observabilidade, migração e documentação técnica."],
              ["Cliente-piloto", "Validar linguagem, regras, exceções, feedback de respostas e impacto na rotina."],
              ["Suporte/operação", "Monitorar alertas, diagnosticar falhas, executar playbooks e registrar padrões recorrentes."],
              ["Privacidade/segurança", "Revisar LGPD, acessos, retenção, fornecedores, contratos e resposta a incidentes."],
          ], [50*mm, 117*mm]),
          P("Ritos de governança", "H2x"),
          bullets([
              "Revisão semanal de produto: métricas, feedbacks, decisões e riscos.",
              "Revisão quinzenal de qualidade: amostra anonimizada de conversas e regressões.",
              "Revisão mensal de segurança, custos de IA e saúde das integrações.",
              "Go/no-go formal antes de aumentar a autonomia ou adicionar tenants ao piloto."
          ]),
          P("Definition of Done para cada história", "H2x"),
          bullets([
              "Regra de negócio e comportamento de falha documentados.",
              "Teste unitário e teste de jornada relevante.",
              "Log e métrica suficientes para diagnóstico.",
              "Feature flag e rollback quando houver impacto em produção.",
              "Interface revisada em desktop e celular quando aplicável.",
              "Validação em staging e piloto antes da liberação ampla."
          ]),
          PageBreak()]

story += [P("15. Riscos e mitigação", "H1x"),
          table([
              ["Risco", "Prob.", "Impacto", "Mitigação"],
              ["Construir na branch errada", "Alta", "Crítico", "F0 obrigatória; branch protegida e build identificável."],
              ["Adicionar funções antes de estabilizar", "Alta", "Alto", "Congelar P2/P3 até critérios da F1."],
              ["Velocidade da IA reduzir revisão", "Média", "Crítico", "Pull requests pequenos, testes, feature flags e go/no-go humano."],
              ["Cliente-piloto pouco disponível", "Média", "Alto", "Rito semanal curto; dados anonimizados; decisões registradas."],
              ["Mudança de modelo altera comportamento", "Alta", "Alto", "Contrato estruturado, regressões e avaliação antes do deploy."],
              ["Custos crescem com contexto/áudio", "Média", "Médio", "Roteamento de modelos, cache, atalhos e orçamento por tenant."],
              ["Migração de banco interrompe operação", "Média", "Alto", "Ensaio, dupla leitura quando necessário, backup e rollback."],
              ["Automação proativa gera rejeição", "Média", "Alto", "Consentimento, frequência, horário, opt-out e piloto."],
              ["Escopo se espalha por 13 segmentos", "Alta", "Alto", "Três verticais prioritárias e critérios de expansão."],
              ["Dependência de provedor WhatsApp", "Média", "Alto", "Abstração, monitor, retry e plano de contingência."],
          ], [52*mm, 18*mm, 20*mm, 77*mm]),
          P("Gatilhos para replanejamento", "H2x"),
          bullets([
              "Mais de 20% da capacidade semanal consumida por incidentes durante duas semanas.",
              "Falha crítica de agenda ou privacidade no piloto.",
              "Mudança de provedor/WhatsApp que afete templates ou webhooks.",
              "Necessidade de múltiplos profissionais confirmada antes da fila de espera.",
              "Ausência de baseline suficiente para medir benefício."
          ]),
          PageBreak()]

story += [P("16. Próximos passos imediatos", "H1x"),
          callout("Decisão recomendada", "Iniciar a Fase 0 e não desenvolver novas funções na branch atual até confirmar a versão canônica e a build efetivamente implantada.", AMBER_BG),
          P("Primeiros 10 dias úteis", "H2x"),
          table([
              ["Dia", "Ação", "Resultado"],
              ["1", "Confirmar branch, commit, build e ambiente", "Fonte única de verdade."],
              ["2", "Mapear jornada do cliente atual e incidentes", "Lista real de falhas e prioridades."],
              ["3", "Criar staging e dados sintéticos", "Ambiente seguro de validação."],
              ["4-5", "Montar regressões de conversas", "Baseline automatizado."],
              ["6", "Corrigir mensagem duplicada e correlação", "Contexto limpo e rastreável."],
              ["7-8", "Implementar fila por contato e agrupamento", "Conversas ordenadas e naturais."],
              ["9", "Implementar dedup uniforme e testes", "Retries não duplicam ações."],
              ["10", "Piloto interno + revisão de métricas", "Decisão de avançar para agenda atômica."],
          ], [14*mm, 78*mm, 75*mm]),
          P("Decisões que o responsável pelo produto deverá validar", "H2x"),
          bullets([
              "Quais três segmentos serão priorizados na primeira versão comercial.",
              "Qual nível de autonomia será padrão para novos clientes.",
              "Se a fila de espera oferece a vaga para uma pessoa por vez ou para um grupo com regra de prioridade.",
              "Quais indicadores serão mostrados no relatório mensal de valor.",
              "Em que momento a migração para PostgreSQL será obrigatória por volume ou receita."
          ]),
          Spacer(1, 7*mm), HRFlowable(width="100%", thickness=1, color=PURPLE), Spacer(1, 5*mm),
          P("Resultado esperado", "H2x"),
          P("Ao final do programa, o produto deverá operar com <b>duas inteligências integradas</b>: um agente atende clientes com naturalidade e garantias técnicas; um assistente organiza o negócio, responde perguntas, recomenda ações e executa comandos autorizados. Ambos aprendem com correções e provam o impacto financeiro gerado. Esse conjunto - confiabilidade, especialização vertical, inteligência administrativa e resultado mensurável - constitui o diferencial competitivo central.", "Bodyx"),
          Spacer(1, 12*mm),
          P("Documento elaborado a partir da documentação técnica e da auditoria do código disponível. Estimativas devem ser recalibradas após a Fase 0.", "Small")]

doc.build(story, canvasmaker=NumberedCanvas)
print(OUT)
