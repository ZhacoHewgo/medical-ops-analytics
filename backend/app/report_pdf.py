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


BRAND = colors.HexColor("#1958FD")
INK = colors.HexColor("#17233D")
MUTED = colors.HexColor("#53647D")
LINE = colors.HexColor("#D8E2F0")
TINT = colors.HexColor("#EDF3FF")
ZEBRA = colors.HexColor("#F8F8F6")
DEPARTMENT_NAMES = {
    "EK": "儿科", "FC": "妇产科", "GK": "骨科", "HN": "心血管内科", "HX": "呼吸内科",
    "ICU": "重症医学科", "JZ": "急诊科", "PW": "普外科", "SJ": "神经内科", "XH": "消化内科",
    "__UNKNOWN__": "未标注科室",
}


@lru_cache(maxsize=1)
def _font_name():
    name = "MopsReport"
    font_path = Path(__file__).with_name("assets") / "MopsReport-Regular.ttf"
    pdfmetrics.registerFont(TTFont(name, str(font_path)))
    return name


def render_pdf(headings, rows, metadata, total):
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=72, bottomMargin=72,
        title="门诊人次报表", author="医疗运营分析平台", allowSplitting=True,
    )
    width = document.width - 12
    font = _font_name()
    body = ParagraphStyle("Body", fontName=font, textColor=INK, fontSize=10.5, leading=16, wordWrap="CJK")
    number = ParagraphStyle("Number", parent=body, alignment=TA_RIGHT)
    title = ParagraphStyle("Title", parent=body, fontSize=26, leading=36, spaceAfter=15)
    label = ParagraphStyle("Label", parent=body, fontSize=9, leading=14, textColor=MUTED)
    amount = ParagraphStyle("Total", parent=body, fontSize=29, leading=38, textColor=BRAND)
    section = ParagraphStyle("Section", parent=body, fontSize=13, leading=20, spaceAfter=7)
    info = dict(metadata)
    scope = info["科室范围"]
    if scope in DEPARTMENT_NAMES:
        scope = f"{DEPARTMENT_NAMES[scope]}（{scope}）"
    dates = f'{info["起始日期"]} 至 {info["结束日期"]}'
    grouping = " × ".join(headings[:-1])
    summary_width = width * 0.36
    summary = Table([[
        [Paragraph("门诊人次合计", label),
         Paragraph(f'{total:,}<font size="10" color="#53647D">　人次</font>', amount)],
        [Paragraph("统计期间", label), Paragraph(escape(dates), body), Spacer(1, 8),
         Paragraph("科室范围 / 汇总维度", label), Paragraph(escape(f"{scope} / {grouping}"), body)],
    ]], colWidths=[summary_width, width - summary_width], hAlign="LEFT")
    summary.setStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, 0), TINT),
        ("BOX", (0, 0), (-1, -1), 0.7, LINE),
        ("LINEAFTER", (0, 0), (0, -1), 0.7, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 15),
        ("RIGHTPADDING", (0, 0), (-1, -1), 15),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ])
    story = [
        Paragraph("门诊人次报表", title),
        summary,
        Spacer(1, 20),
        Paragraph(f"{escape(grouping)}明细", section),
    ]
    columns = [112, width - 224, 112] if len(headings) == 3 else [width - 112, 112]
    cells = [[Paragraph(escape(heading), number if index == len(headings) - 1 else body)
              for index, heading in enumerate(headings)]]
    for row in rows:
        row_cells = []
        for heading, value in zip(headings[:-1], row[:-1]):
            display = escape(str(value))
            if heading == "科室" and value in DEPARTMENT_NAMES:
                display = f'{DEPARTMENT_NAMES[value]}<font size="8.5" color="#53647D">　{display}</font>'
            row_cells.append(Paragraph(display, body))
        row_cells.append(Paragraph(f"{row[-1]:,}", number))
        cells.append(row_cells)
    table = LongTable(cells, colWidths=columns, repeatRows=1, hAlign="LEFT")
    table.setStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
        ("BACKGROUND", (0, 0), (-1, 0), TINT),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE),
    ])
    story.append(table)
    if not rows:
        story.extend([Spacer(1, 10), Paragraph("当前范围暂无门诊记录", body)])

    def page_chrome(canvas, layout):
        left = layout.leftMargin + 6
        right = left + width
        canvas.saveState()
        canvas.setFillColor(BRAND)
        canvas.roundRect(left, A4[1] - 45, 24, 3, 1.5, stroke=0, fill=1)
        canvas.setFont(font, 10)
        canvas.setFillColor(INK)
        canvas.drawString(left + 33, A4[1] - 47, "医疗运营分析平台")
        if layout.page > 1:
            canvas.drawRightString(right, A4[1] - 47, "门诊人次报表")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(left, A4[1] - 57, right, A4[1] - 57)
        canvas.line(left, 54, right, 54)
        canvas.setFont(font, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(left, 39, f'来源表  {info["来源表"]}    批次  {info["批次"]}')
        canvas.drawString(left, 26, f'构建号  {info["构建号"]}')
        canvas.drawRightString(right, 26, f"第 {layout.page} 页")
        canvas.restoreState()

    document.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return stream.getvalue()
