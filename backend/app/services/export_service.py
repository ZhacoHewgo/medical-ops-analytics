from io import BytesIO

from fastapi import HTTPException, Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from reportlab.pdfbase.pdfdoc import PDFError
from reportlab.platypus import LayoutError

from app.report_pdf import render_pdf

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"


def report_rows(document, group_by):
    counts = {}
    for row in document["counts"]:
        day = None if group_by == "department" else row["date"]
        if group_by == "month_department":
            day = day[:7]
        department = None if group_by == "day" else row["dept_code"]
        key = (day, department)
        counts[key] = counts.get(key, 0) + row["visits"]
        if len(counts) > 10000:
            raise HTTPException(422, "Report exceeds 10000 rows; choose a smaller date range")
    return [{"date": day, "dept_code": department, "visits": value}
            for (day, department), value in sorted(counts.items())]


def export_report(document, summary, group_by="day_department", *, format="xlsx", expected_build_run_id=None):
    meta = document["meta"]
    if expected_build_run_id is not None and str(expected_build_run_id) != meta["build_run_id"]:
        raise HTTPException(409, "Publication changed; refresh the report before exporting")
    rows = report_rows(document, group_by)
    headings = {
        "day": ("日期", "门诊人次"),
        "department": ("科室", "门诊人次"),
        "day_department": ("日期", "科室", "门诊人次"),
        "month_department": ("月份", "科室", "门诊人次"),
    }[group_by]
    cells = [
        tuple(value for value in (row["date"], row["dept_code"], row["visits"]) if value is not None)
        for row in rows
    ]
    filters = summary["data"]["filters"]
    metadata = (
        ("来源表", meta["source"]), ("批次", meta["batch_date"]), ("构建号", meta["build_run_id"]),
        ("起始日期", filters["start_date"]), ("结束日期", filters["end_date"]),
        ("科室范围", filters["dept_code"] or "全院"),
    )
    if format == "pdf":
        content = export_pdf(headings, cells, metadata, summary["data"]["outpatient_visits"])
        media_type = PDF
    else:
        content = export_excel(headings, cells, metadata)
        media_type = XLSX
    return Response(content, media_type=media_type, headers={
        "Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="outpatient.{format}"',
    })


def export_excel(headings, rows, metadata):
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("门诊人次")
    sheet.append(headings)
    for row in rows:
        cells = []
        for value in row[:-1]:
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            cells.append(cell)
        sheet.append([*cells, row[-1]])
    source = workbook.create_sheet("数据来源")
    for label, value in metadata:
        cell = WriteOnlyCell(source, value=value)
        cell.data_type = "s"
        source.append((label, cell))
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def export_pdf(headings, rows, metadata, total):
    try:
        return render_pdf(headings, rows, metadata, total)
    except (OSError, ValueError, PDFError, LayoutError):
        raise HTTPException(503, "Report PDF unavailable") from None
