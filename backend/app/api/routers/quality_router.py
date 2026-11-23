from datetime import date
import os
import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app import operations
from app.api.routers.operations_router import ERRORS, Reader, Result, _reply
from app.outpatient_response import check_dates
from etl.config.source_config import OdsError, validate_dataset
from etl.jobs import build_dwd
from etl.jobs.quality_check import REPORT_VERSIONS


router = APIRouter(prefix="/api/ops", tags=["数据质量"])


class QualityBatch(BaseModel):
    batch_date: date
    gate_status: Literal["PASS", "FAIL", "BLOCKED_INSUFFICIENT_EVIDENCE"]
    score: float = Field(ge=0, le=100)
    coverage: float = Field(ge=0, le=100)
    planned_rules: int = Field(ge=0)
    executed_rules: int = Field(ge=0)
    evaluated_rules: int = Field(ge=0)
    warning_rules: int = Field(ge=0, le=34)


@router.get("/quality", response_model=Result[list[QualityBatch]], responses=ERRORS,
            summary="按批次日期查看已生成的质检摘要")
def get_quality_report(actor: Reader, response: Response, start_date: Annotated[str | None, Query()] = None,
                       end_date: Annotated[str | None, Query()] = None):
    check_dates(start_date, end_date)
    try:
        default = os.environ.get("OUTPATIENT_PREVIEW_BATCH_DATE")
        first, last = start_date or default, end_date or default
        if first is None or last is None:
            raise OdsError("QUALITY_BATCH_UNCONFIGURED")
        check_dates(first, last)
        if (date.fromisoformat(last) - date.fromisoformat(first)).days > 365:
            raise HTTPException(422, "Quality report range must not exceed 366 days")
        dataset = validate_dataset(os.environ.get("OUTPATIENT_PREVIEW_DATASET_ID", ""))
        env = operations.environment()
        directory = build_dwd._open_report_directory_readonly(env)
        try:
            filenames = sorted(os.listdir(directory))
        finally:
            os.close(directory)
        reports = []
        for name in filenames:
            match = re.fullmatch(r"quality_report_([0-9]{4})([0-9]{2})([0-9]{2})\.json", name)
            if match is None:
                continue
            batch = "-".join(match.groups())
            if not first <= batch <= last:
                continue
            report, _ = build_dwd._read_quality_report(env, batch)
            if report.get("dataset_id") != dataset:
                continue
            if report.get("schema_version") not in REPORT_VERSIONS or (report.get("env"), report.get("batch_date")) != (
                env, batch,
            ):
                raise ValueError
            summary = {key: report[key] for key in QualityBatch.model_fields if key != "warning_rules"}
            summary["warning_rules"] = report["rule_status_counts"]["WARN"]
            reports.append(QualityBatch.model_validate(summary))
    except (OdsError, OSError, ValueError, AttributeError, KeyError, TypeError):
        raise HTTPException(503, "Published quality summary unavailable") from None
    return _reply(response, reports)
