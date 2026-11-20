"""Authorized outpatient summary and export from a certified publication."""
from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from pydantic import BaseModel, ConfigDict, Field
from reportlab.pdfbase.pdfdoc import PDFError
from reportlab.platypus import LayoutError

from app.core.auth_store import Account, DB_DOWN, read_user
from app.core.deps import apply_data_scope, get_current_user
from app.core.security import unauthorized
from app.outpatient_response import check_dates, summarize_outpatient
from app.outpatient_preview import load_ads
from app.report_pdf import render_pdf
from etl.config.source_config import OdsError, database_cursor


router = APIRouter(prefix="/api/v1", tags=["门诊集市"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"
BINARY = {"schema": {"type": "string", "format": "binary"}}
ERRORS = {
    401: {"description": "登录缺失、过期或失效"},
    403: {"description": "科室范围越权"},
    422: {"description": "日期、维度、指标或粒度无效"},
    503: {"description": "认证集市或审计不可用"},
}


class OutpatientQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["MOPS-OUT-01"] = "MOPS-OUT-01"
    granularity: Literal["day"] = "day"
    start_date: str | None = None
    end_date: str | None = None
    dept_code: str | None = Field(default=None, max_length=64)


ReportGroup = Literal["day", "department", "day_department", "month_department"]


class ReportQuery(OutpatientQuery):
    group_by: ReportGroup = "day_department"


class ExportRequest(ReportQuery):
    format: Literal["xlsx", "pdf"] = "xlsx"
    expected_build_run_id: UUID | None = None


class Filters(BaseModel):
    start_date: str
    end_date: str
    dept_code: str | None


class DayCount(BaseModel):
    date: str
    visits: int


class DepartmentCount(BaseModel):
    dept_code: str
    visits: int


class Publication(BaseModel):
    source: Literal["ads_outpatient_theme"]
    batch_date: str
    build_run_id: str
    available_start_date: str
    available_end_date: str
    department_codes: list[str]


class SummaryResponse(BaseModel):
    class Data(BaseModel):
        filters: Filters
        outpatient_visits: int
        daily_trend: list[DayCount]
        department_ranking: list[DepartmentCount]
        meta: Publication

    code: Literal[0]
    msg: str
    data: Data


class ReportRow(BaseModel):
    date: str | None
    dept_code: str | None
    visits: int


class ReportResponse(SummaryResponse):
    class Data(SummaryResponse.Data):
        group_by: ReportGroup
        rows: list[ReportRow]

    data: Data


def audit_access(user, action, department, status):
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            current = read_user(cursor, user_id=user.user_id, lock=True)
            valid = (current is not None and current.enabled and current.role == user.role
                     and current.department == user.department and current.revision == user.revision)
            if valid:
                cursor.execute(
                    "INSERT INTO auth_data_access (actor_id,resource,action,department,http_status) "
                    "VALUES (%s,'outpatient',%s,%s,%s)", (user.user_id, action, department, status),
                )
    except OdsError:
        raise HTTPException(503, "Access audit unavailable") from None
    if not valid:
        raise unauthorized()


def _certified_data(query, user):
    scope = apply_data_scope(query.dept_code, user)
    check_dates(query.start_date, query.end_date)
    if scope is not None and (not scope.strip() or len(scope) > 64):
        raise HTTPException(422, "Invalid dept_code")
    try:
        document = load_ads(start_date=query.start_date, end_date=query.end_date, dept_code=scope,
                            allowed_department=user.department if user.role == "department" else None)
    except (OdsError, ImportError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, "Certified outpatient mart unavailable or incomplete") from None
    meta = document["meta"]
    low, high = meta["available_start_date"], meta["available_end_date"]
    if any(day is not None and not low <= day <= high for day in (query.start_date, query.end_date)):
        raise HTTPException(422, "Dates must be within certified build coverage")
    summary = summarize_outpatient(document, query.start_date, query.end_date, scope)
    return scope, document, summary


def _report_rows(document, group_by):
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


def _export(document, summary, group_by="day_department", *, format="xlsx", expected_build_run_id=None):
    meta = document["meta"]
    if expected_build_run_id is not None and str(expected_build_run_id) != meta["build_run_id"]:
        raise HTTPException(409, "Publication changed; refresh the report before exporting")
    rows = _report_rows(document, group_by)
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
        try:
            content = render_pdf(headings, cells, metadata, summary["data"]["outpatient_visits"])
        except (OSError, ValueError, PDFError, LayoutError):
            raise HTTPException(503, "Report PDF unavailable") from None
        media_type = PDF
    else:
        content = _excel(headings, cells, metadata)
        media_type = XLSX
    return Response(content, media_type=media_type, headers={
        "Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="outpatient.{format}"',
    })


def _excel(headings, rows, metadata):
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


def _serve(query, user, action, *, grouped=False):
    try:
        scope, document, summary = _certified_data(query, user)
        if action == "export":
            result = _export(document, summary, query.group_by, format=query.format,
                             expected_build_run_id=query.expected_build_run_id)
        else:
            if grouped:
                summary["data"].update(group_by=query.group_by, rows=_report_rows(document, query.group_by))
            result = summary
    except HTTPException as error:
        audit_access(user, action, user.department, error.status_code)
        raise
    audit_access(user, action, scope, 200)
    return result


@router.get("/outpatient/summary", response_model=SummaryResponse, responses=ERRORS,
            summary="读取已完整发布的门诊日人次")
def outpatient_summary(query: Annotated[OutpatientQuery, Query()], response: Response,
                       user: Annotated[Account, Depends(get_current_user)]):
    result = _serve(query, user, "query")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/reports/outpatient", response_model=ReportResponse, responses=ERRORS,
            summary="按日期、月份或科室组合查询门诊报表")
def outpatient_report(query: Annotated[ReportQuery, Query()], response: Response,
                      user: Annotated[Account, Depends(get_current_user)]):
    result = _serve(query, user, "query", grouped=True)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/reports/export", response_class=Response, summary="导出已授权门诊聚合 Excel 或 PDF",
             responses={**ERRORS, 409: {"description": "数据发布批次已变化，请重新查询"},
                        200: {"content": {XLSX: BINARY, PDF: BINARY}}})
def export_outpatient(query: ExportRequest, user: Annotated[Account, Depends(get_current_user)]):
    return _serve(query, user, "export")
