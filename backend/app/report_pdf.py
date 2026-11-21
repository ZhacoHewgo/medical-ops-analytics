"""Render the authorized outpatient export rows as a printable Chinese report."""
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table


@lru_cache(maxsize=1)
def _font_name():
    name = "MopsReport"
    font_path = Path(__file__).with_name("assets") / "MopsReport-Regular.ttf"
    pdfmetrics.registerFont(TTFont(name, str(font_path)))
    return name


def render_pdf(headings, rows, metadata, total):
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=42, bottomMargin=48,
        title="门诊人次报表", allowSplitting=True,
    )
    width = document.width - 12
    body = ParagraphStyle("Body", fontName=_font_name(), fontSize=10, leading=15, wordWrap="CJK")
    number = ParagraphStyle("Number", parent=body, alignment=TA_RIGHT)
    title = ParagraphStyle("Title", parent=body, fontSize=23, leading=32, spaceAfter=10)
    cells = [[Paragraph(escape(label), body), Paragraph(escape(value), body)] for label, value in metadata]
    details = Table(cells, colWidths=[82, width - 82], hAlign="LEFT")
    details.setStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0)])
    story = [
        Paragraph("门诊人次报表", title),
        details,
        Spacer(1, 14),
        Paragraph(f"门诊人次合计 {total}", body),
        Spacer(1, 10),
    ]
    columns = [96, width - 188, 92] if len(headings) == 3 else [width - 92, 92]
    table = LongTable(
        [[Paragraph(label, number if index == len(headings) - 1 else body)
          for index, label in enumerate(headings)]]
        + [[Paragraph(escape(str(value)), number if index == len(row) - 1 else body)
            for index, value in enumerate(row)] for row in rows],
        colWidths=columns, repeatRows=1, hAlign="LEFT",
    )
    table.setStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EFF4")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCD6DD")),
    ])
    story.append(table)
    if not rows:
        story.extend([Spacer(1, 10), Paragraph("当前范围暂无门诊记录", body)])

    def page_number(canvas, layout):
        canvas.setFont(_font_name(), 9)
        canvas.drawRightString(A4[0] - layout.rightMargin, 28, str(layout.page))

    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return stream.getvalue()
