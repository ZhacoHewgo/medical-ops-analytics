from hashlib import sha256
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.api.routers.outpatient_router import ERRORS
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.schemas.outpatient import Filters
from app.services.export_service import XLSX, export_excel
from app.services.theme_service import DateScope, Publication, serve_summary
from etl.jobs.multidimensional_report import ReportGrain, ReportGroup, ReportMetric, excel_rows, summarize_report


router = APIRouter(prefix="/api/v1/reports/multidimensional", tags=["多维报表"])
ReportId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class MultidimensionalQuery(DateScope):
    metrics: list[ReportMetric] = Field(min_length=1, max_length=5)
    group_by: ReportGroup = "date_department"
    grain: ReportGrain = "month"

    @field_validator("metrics")
    @classmethod
    def unique_metrics(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("Duplicate metric")
        return values


class MultidimensionalExport(MultidimensionalQuery):
    report_id: ReportId


class ReportRow(BaseModel):
    period: str | None
    department: str | None
    values: dict[ReportMetric, int | str | None]


class ReportData(BaseModel):
    report_id: ReportId
    filters: Filters
    meta: Publication
    metrics: list[ReportMetric]
    grain: ReportGrain
    group_by: ReportGroup
    totals: dict[ReportMetric, int | str | None]
    rows: list[ReportRow]


class ReportResponse(BaseModel):
    code: Literal[0]
    msg: str
    data: ReportData


def _report(query, response, user, *, export_id=None):
    def load(*args, **kwargs):
        data = summarize_report(*args, **kwargs)
        identity = sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if export_id is not None:
            if identity != export_id:
                raise HTTPException(409, "Report changed; refresh before exporting")
            return _excel(data)
        return {**data, "report_id": identity}

    return serve_summary(
        query, response, user, "multidimensional_report", load, action="export" if export_id else "query",
        metrics=query.metrics, grain=query.grain, group_by=query.group_by,
    )


@router.get("", response_model=ReportResponse, responses=ERRORS, summary="查询多维报表")
def get_report(query: Annotated[MultidimensionalQuery, Query()], response: Response,
               user: Annotated[Account, Depends(get_current_user)]):
    return _report(query, response, user)


@router.post("/export", responses={**ERRORS, 409: {"description": "报表已更新"},
                                   200: {"content": {XLSX: {"schema": {"type": "string", "format": "binary"}}}}},
             summary="导出多维报表")
def export_report(query: MultidimensionalExport, response: Response,
                  user: Annotated[Account, Depends(get_current_user)]):
    content = _report(query, response, user, export_id=query.report_id)["data"]
    return Response(content, media_type=XLSX, headers={
        "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="multidimensional.xlsx"',
    })


def _excel(data):
    total = {"period": "合计", "department": "合计" if data["group_by"] == "department" else None,
             "values": data["totals"]}
    headings, rows = excel_rows([*data["rows"], total], data["metrics"], data["group_by"])
    filters = data["filters"]
    metadata = (
        ("起始日期", filters["start_date"]), ("结束日期", filters["end_date"]),
        ("科室范围", filters["dept_code"] or "全院"), ("批次", data["meta"]["batch_date"]),
        ("构建号", data["meta"]["build_run_id"]),
    )
    return export_excel(headings, rows, metadata, sheet_name="多维报表")
