"""The product standard for PDFs (18 Nov 2026): a black header band with the gold wordmark, one type
scale (the web's 14/16/20/28 px as 10.5/12/15/21 pt; nothing smaller), three greys, light grey bands.
Receipts and earnings statements build on it so every document looks like the web receipt."""
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import TableStyle

INK = colors.HexColor('#121212')
TEXT_2 = colors.HexColor('#4A4A4A')
TEXT_3 = colors.HexColor('#6B6B6B')
BAND = colors.HexColor('#F6F6F6')
LINE = colors.HexColor('#E4E4E4')
GOLD = colors.HexColor('#F5C451')
SMALL, BODY, TITLE, HEADING = 10.5, 12, 15, 21
BAND_H = 20 * mm
#: Top margin that clears the band.
TOP = BAND_H + 10 * mm


def styles():
    return {
        'heading': ParagraphStyle('sg-heading', fontName='Helvetica-Bold', fontSize=HEADING, leading=27, textColor=INK, spaceAfter=4),
        'title': ParagraphStyle('sg-title', fontName='Helvetica-Bold', fontSize=TITLE, leading=20, textColor=INK, spaceBefore=14, spaceAfter=6),
        'body': ParagraphStyle('sg-body', fontName='Helvetica', fontSize=SMALL, leading=15, textColor=TEXT_2),
        'cell': ParagraphStyle('sg-cell', fontName='Helvetica', fontSize=SMALL, leading=14, textColor=INK),
        'note': ParagraphStyle('sg-note', fontName='Helvetica', fontSize=SMALL, leading=15, textColor=TEXT_3),
    }


def _base():
    return [('FONTNAME', (0, 0), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, -1), SMALL),
            ('LEADING', (0, 0), (-1, -1), 14), ('TEXTCOLOR', (0, 0), (-1, -1), INK), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, LINE), ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]


def table(header=True, totals=(), right_from=None):
    """A data table: grey header band, hairlines, bold banded total rows (row indexes), numbers right."""
    cmds = _base()
    if header:
        cmds += [('BACKGROUND', (0, 0), (-1, 0), BAND), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                 ('TEXTCOLOR', (0, 0), (-1, 0), TEXT_2)]
    for i in totals:
        cmds += [('BACKGROUND', (0, i), (-1, i), BAND), ('FONTNAME', (0, i), (-1, i), 'Helvetica-Bold')]
    if right_from is not None:
        cmds += [('ALIGN', (right_from, 0), (-1, -1), 'RIGHT')]
    return TableStyle(cmds)


def facts():
    """Label and value rows (who, when, where): grey labels, ink values."""
    return TableStyle(_base() + [('TEXTCOLOR', (0, 0), (0, -1), TEXT_3)])


def band(title, brand='SaaradhiGo'):
    """Draws the black header band on every page: the wordmark left, the document's name right."""
    def draw(canvas, doc):
        w, h = doc.pagesize
        canvas.saveState()
        canvas.setFillColor(colors.black)
        canvas.rect(0, h - BAND_H, w, BAND_H, stroke=0, fill=1)
        canvas.setFillColor(GOLD)
        canvas.setFont('Helvetica-Bold', TITLE)
        canvas.drawString(doc.leftMargin, h - BAND_H / 2 - 5, brand)
        canvas.setFillColor(colors.white)
        canvas.setFont('Helvetica', SMALL)
        canvas.drawRightString(w - doc.rightMargin, h - BAND_H / 2 - 4, title)
        canvas.restoreState()
    return draw
