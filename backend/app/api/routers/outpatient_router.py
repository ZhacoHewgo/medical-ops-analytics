"""Authorized outpatient summary and Excel export from a certified publication."""
from io import BytesIO
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth_store import Account, DB_DOWN, read_user
from app.core.deps import apply_data_scope, get_current_user
from app.core.security import unauthorized
from app.outpatient_response import check_dates, summarize_outpatient
from app.outpatient_preview import load_ads
from etl.config.source_config import OdsError, database_cursor


router = APIRouter(prefix="/api/v1", tags=["门诊集市"])
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
class ErrorResponse(BaseModel):
    code: int
    msg: str
    data: None


ERRORS = {
    401: {
        "description": "登录缺失、过期或失效。"
        '示例 `{"code":401,"msg":"Authentication required","data":null}`',
        "model": ErrorResponse,
    },
    403: {"description": "科室范围越权"},
    422: {
        "description": "日期、维度、指标或粒度无效。"
        '示例 `{"code":422,"msg":"Invalid query parameters","data":null}`',
        "model": ErrorResponse,
    },
    503: {"description": "认证集市或审计不可用"},
}


class OutpatientQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["MOPS-OUT-01"] = "MOPS-OUT-01"
    granularity: Literal["day"] = "day"
    start_date: str | None = None
    end_date: str | None = None
    dept_code: str | None = Field(default=None, max_length=64)


class ExcelRequest(OutpatientQuery):
    format: Literal["xlsx"] = "xlsx"


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


def _excel(document, summary):
    if len(document["counts"]) > 10000:
        raise HTTPException(422, "Export exceeds 10000 rows; choose a smaller date range")
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("门诊人次")
    sheet.append(("日期", "科室", "门诊人次"))
    for row in document["counts"]:
        department = WriteOnlyCell(sheet, value=row["dept_code"])
        department.data_type = "s"
        sheet.append((row["date"], department, row["visits"]))
    source = workbook.create_sheet("数据来源")
    meta, filters = document["meta"], summary["data"]["filters"]
    for label, value in (
        ("来源表", meta["source"]), ("批次", meta["batch_date"]), ("构建号", meta["build_run_id"]),
        ("起始日期", filters["start_date"]), ("结束日期", filters["end_date"]),
    ):
        source.append((label, value))
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return Response(stream.getvalue(), media_type=XLSX, headers={
        "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="outpatient.xlsx"',
    })


def _serve(query, user, action):
    try:
        scope, document, summary = _certified_data(query, user)
        result = summary if action == "query" else _excel(document, summary)
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


@router.post("/reports/export", response_class=Response, summary="导出已授权门诊聚合 Excel",
             responses={**ERRORS, 200: {"content": {XLSX: {"schema": {"type": "string", "format": "binary"}}}}})
def export_outpatient(query: ExcelRequest, user: Annotated[Account, Depends(get_current_user)]):
    return _serve(query, user, "export")
