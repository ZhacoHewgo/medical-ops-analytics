"""Authorized outpatient summary and export from a certified publication."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.responses import UNAUTHORIZED_EXAMPLE, VALIDATION_EXAMPLE
from app.core.auth_store import Account, DB_DOWN, SHARED, read_user
from app.core.deps import get_current_user
from app.core.security import unauthorized
from app.schemas.outpatient import ErrorResponse, ExportRequest, OutpatientDashboardResponse, OutpatientQuery, ReportQuery, ReportResponse
from app.services.export_service import PDF, XLSX, export_report, report_rows
from app.services.indicator_service import query_indicator
from etl.config.source_config import OdsError, database_cursor


router = APIRouter(prefix="/api/v1", tags=["门诊集市"])
BINARY = {"schema": {"type": "string", "format": "binary"}}


ERRORS = {
    401: {
        "description": "登录缺失、过期或失效。"
        '示例 `{"code":401,"msg":"Authentication required","data":null}`',
        "model": ErrorResponse,
        "content": {"application/json": {"example": UNAUTHORIZED_EXAMPLE}},
    },
    403: {"description": "科室范围越权"},
    422: {
        "description": "日期、维度、指标或粒度无效。"
        '示例 `{"code":422,"msg":"Invalid query parameters","data":null}`',
        "model": ErrorResponse,
        "content": {"application/json": {"example": VALIDATION_EXAMPLE}},
    },
    503: {"description": "认证集市或审计不可用"},
}


def audit_access(user, action, department, status, *, resource="outpatient"):
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            current = read_user(cursor, user_id=user.user_id, lock=SHARED)
            valid = (current is not None and current.enabled and current.role == user.role
                     and current.department == user.department and current.revision == user.revision)
            if valid:
                cursor.execute(
                    "INSERT INTO auth_data_access (actor_id,resource,action,department,http_status) "
                    "VALUES (%s,%s,%s,%s,%s)", (user.user_id, resource, action, department, status),
                )
    except OdsError:
        raise HTTPException(503, "Access audit unavailable") from None
    if not valid:
        raise unauthorized()


def _serve(query, user, action, *, grouped=False):
    try:
        scope, document, summary = query_indicator(query, user)
        if action == "export":
            result = export_report(document, summary, query.group_by, format=query.format,
                             expected_build_run_id=query.expected_build_run_id)
        else:
            if grouped:
                summary["data"].update(group_by=query.group_by, rows=report_rows(document, query.group_by))
            else:
                summary["data"]["registration_mix"] = document.get("registration_mix", [])
                summary["data"]["outpatient_avg_cost"] = document.get("outpatient_avg_cost")
            result = summary
    except HTTPException as error:
        audit_access(user, action, user.department, error.status_code)
        raise
    audit_access(user, action, scope, 200)
    return result


@router.get("/outpatient/summary", response_model=OutpatientDashboardResponse, responses=ERRORS,
            summary="读取已完整发布的门诊日人次", operation_id="outpatient_summary_api_v1_outpatient_summary_get")
def get_outpatient_metrics(query: Annotated[OutpatientQuery, Query()], response: Response,
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
