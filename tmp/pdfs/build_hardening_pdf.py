from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "PROJETO_HARDENING_BOT.md"
OUTPUT = ROOT / "output" / "pdf" / "Projeto_Hardening_Bot_Cripto.pdf"

FONT_REG = "/System/Library/Fonts/Supplemental/Verdana.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
pdfmetrics.registerFont(TTFont("Verdana", FONT_REG))
pdfmetrics.registerFont(TTFont("Verdana-Bold", FONT_BOLD))

NAVY = colors.HexColor("#12233F")
BLUE = colors.HexColor("#1F6FEB")
CYAN = colors.HexColor("#EAF3FF")
PALE = colors.HexColor("#F5F7FA")
MID = colors.HexColor("#D0D7DE")
TEXT = colors.HexColor("#202936")
MUTED = colors.HexColor("#667085")
GREEN = colors.HexColor("#16794B")


def normalize(text: str) -> str:
    return (text.replace("\u2011", "-").replace("\u2013", "-")
                .replace("\u2014", "-").replace("→", "->")
                .replace("≤", "<=").replace("≥", ">="))


def inline(text: str) -> str:
    text = escape(normalize(text.strip()))
    text = re.sub(r"`([^`]+)`", r'<font name="Verdana" color="#9B2C2C">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r'<b>\1</b>', text)
    return text


styles = getSampleStyleSheet()
TITLE = ParagraphStyle("TitleCustom", fontName="Verdana-Bold", fontSize=25,
                       leading=31, textColor=colors.white, alignment=TA_LEFT,
                       spaceAfter=8)
SUBTITLE = ParagraphStyle("Subtitle", fontName="Verdana", fontSize=11,
                          leading=17, textColor=colors.HexColor("#DCE8FF"))
H1 = ParagraphStyle("H1Custom", fontName="Verdana-Bold", fontSize=16,
                    leading=20, textColor=NAVY, spaceBefore=13, spaceAfter=7,
                    keepWithNext=True)
H2 = ParagraphStyle("H2Custom", fontName="Verdana-Bold", fontSize=12,
                    leading=16, textColor=BLUE, spaceBefore=10, spaceAfter=5,
                    keepWithNext=True)
H3 = ParagraphStyle("H3Custom", fontName="Verdana-Bold", fontSize=10.2,
                    leading=14, textColor=NAVY, spaceBefore=8, spaceAfter=4,
                    keepWithNext=True)
BODY = ParagraphStyle("BodyCustom", fontName="Verdana", fontSize=8.6,
                      leading=13, textColor=TEXT, spaceAfter=5)
BULLET = ParagraphStyle("BulletCustom", parent=BODY, leftIndent=12, firstLineIndent=-7,
                        bulletIndent=3, spaceAfter=3)
NUMBERED = ParagraphStyle("NumberedCustom", parent=BODY, leftIndent=15,
                          firstLineIndent=-10, spaceAfter=3)
CODE = ParagraphStyle("CodeCustom", fontName="Courier", fontSize=7.2,
                      leading=10.2, textColor=colors.HexColor("#17202A"),
                      leftIndent=7, rightIndent=7, borderColor=MID,
                      borderWidth=0.5, borderPadding=7, backColor=PALE,
                      spaceBefore=4, spaceAfter=7)
NOTE = ParagraphStyle("Note", fontName="Verdana", fontSize=8.2, leading=12,
                      textColor=MUTED, leftIndent=8, borderColor=BLUE,
                      borderWidth=0, borderLeftWidth=2, borderPadding=6,
                      backColor=CYAN, spaceAfter=7)
TABLE_HEAD = ParagraphStyle("TableHead", fontName="Verdana-Bold", fontSize=6.8,
                            leading=8.8, textColor=colors.white)
TABLE_BODY = ParagraphStyle("TableBody", fontName="Verdana", fontSize=6.7,
                            leading=8.7, textColor=TEXT)


class ProjectDoc(BaseDocTemplate):
    def __init__(self, filename):
        super().__init__(filename, pagesize=A4, leftMargin=17*mm, rightMargin=17*mm,
                         topMargin=18*mm, bottomMargin=17*mm,
                         title="Projeto de Hardening e Evolução do Bot de Cripto",
                         author="Projeto Agente de IA Crypto")
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height,
                      id="normal", leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates(PageTemplate(id="main", frames=frame,
                                           onPage=self.header_footer))

    def header_footer(self, canvas, doc):
        canvas.saveState()
        if doc.page > 1:
            canvas.setStrokeColor(MID)
            canvas.setLineWidth(0.5)
            canvas.line(doc.leftMargin, A4[1]-12*mm, A4[0]-doc.rightMargin, A4[1]-12*mm)
            canvas.setFont("Verdana", 6.8)
            canvas.setFillColor(MUTED)
            canvas.drawString(doc.leftMargin, A4[1]-9.5*mm, "PROJETO DE HARDENING DO BOT")
            canvas.drawRightString(A4[0]-doc.rightMargin, 9*mm, f"Página {doc.page}")
        canvas.restoreState()


def table_from(rows):
    if not rows:
        return None
    cols = len(rows[0])
    widths_by_cols = {
        3: [31*mm, 72*mm, 70*mm],
        7: [10*mm, 56*mm, 25*mm, 15*mm, 19*mm, 23*mm, 25*mm],
        8: [9*mm, 20*mm, 18*mm, 18*mm, 12*mm, 24*mm, 31*mm, 41*mm],
    }
    widths = widths_by_cols.get(cols, [173*mm/cols]*cols)
    cooked = []
    for ridx, row in enumerate(rows):
        style = TABLE_HEAD if ridx == 0 else TABLE_BODY
        cooked.append([Paragraph(inline(cell), style) for cell in row])
    t = Table(cooked, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), NAVY),
        ("GRID", (0,0), (-1,-1), 0.35, MID),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, PALE]),
    ]))
    return t


def parse_markdown(text):
    lines = text.splitlines()
    story = []
    in_code = False
    code_lines = []
    table_rows = []
    first_heading = True

    def flush_table():
        nonlocal table_rows
        if table_rows:
            filtered = [r for r in table_rows if not all(re.fullmatch(r"[-: ]+", c) for c in r)]
            t = table_from(filtered)
            if t:
                story.extend([t, Spacer(1, 6)])
            table_rows = []

    for raw in lines:
        line = normalize(raw.rstrip())
        if line.startswith("```"):
            flush_table()
            if in_code:
                safe = "<br/>".join(escape(x if x else " ") for x in code_lines)
                story.append(Paragraph(safe, CODE))
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if line.startswith("|") and line.endswith("|"):
            table_rows.append([c.strip() for c in line.strip("|").split("|")])
            continue
        flush_table()
        if not line.strip():
            story.append(Spacer(1, 2))
        elif line.startswith("# ") and first_heading:
            first_heading = False
            story.append(Table([[Paragraph(inline(line[2:]), TITLE)],
                                [Paragraph("Plano executivo, cronograma e prompts de implementação", SUBTITLE)]],
                               colWidths=[173*mm], style=TableStyle([
                                   ("BACKGROUND", (0,0), (-1,-1), NAVY),
                                   ("BOX", (0,0), (-1,-1), 0, NAVY),
                                   ("LEFTPADDING", (0,0), (-1,-1), 13*mm),
                                   ("RIGHTPADDING", (0,0), (-1,-1), 13*mm),
                                   ("TOPPADDING", (0,0), (-1,-1), 12*mm),
                                   ("BOTTOMPADDING", (0,0), (-1,-1), 8*mm),
                               ])))
            story.append(Spacer(1, 10*mm))
            story.append(Paragraph("Documento de controle", H2))
            story.append(Paragraph("Versão: 06/08/2026 &nbsp;&nbsp;|&nbsp;&nbsp; Status inicial: planejamento", NOTE))
        elif line.startswith("# "):
            story.append(Paragraph(inline(line[2:]), H1))
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), H1))
        elif line.startswith("### "):
            story.append(Paragraph(inline(line[4:]), H2))
        elif line.startswith("#### "):
            story.append(Paragraph(inline(line[5:]), H3))
        elif re.match(r"^\d+\. ", line):
            number, content = line.split(". ", 1)
            story.append(Paragraph(f"<b>{number}.</b> {inline(content)}", NUMBERED))
        elif line.startswith("- "):
            story.append(Paragraph(f"• {inline(line[2:])}", BULLET))
        elif line.startswith("> "):
            story.append(Paragraph(inline(line[2:]), NOTE))
        else:
            story.append(Paragraph(inline(line), BODY))
    flush_table()
    return story


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    source_text = SOURCE.read_text(encoding="utf-8")
    doc = ProjectDoc(str(OUTPUT))
    doc.build(parse_markdown(source_text))
    print(OUTPUT)


if __name__ == "__main__":
    main()
