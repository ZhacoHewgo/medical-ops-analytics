import os
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.routers.outpatient_router import audit_access
from app.core.deps import apply_data_scope
from app.outpatient_response import check_dates
from app.schemas.outpatient import Filters
from etl.config.source_config import OdsError, database_options, validate_dataset


class DateScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: str | None = None
    end_date: str | None = None
    dept_code: str | None = Field(default=None, min_length=1, max_length=64)


class ThemeQuery(DateScope):
    drill: Literal["department"] | None = None


class Metric(BaseModel):
    value: str | None
    null_reason: str | None


class ThemeRow(Metric):
    group: str


class ThemePublication(BaseModel):
    source: Literal["ads_income_theme", "ads_drg_theme", "ads_efficiency_theme"]
    batch_date: str
    build_run_id: str
    available_start_date: str
    available_end_date: str


class ThemeSummary(BaseModel):
    class Data(BaseModel):
        filters: Filters
        meta: ThemePublication
        metric: str
        metrics: dict[str, Metric]
        group_by: Literal["day", "department"]
        rows: list[ThemeRow]

    code: Literal[0]
    msg: str
    data: Data


def serve_summary(query, response, user, resource, load, **options):
    try:
        scope = apply_data_scope(query.dept_code, user)
        check_dates(query.start_date, query.end_date)
        env = os.environ.get("OUTPATIENT_PREVIEW_ENV", "dev")
        try:
            dataset = validate_dataset(os.environ.get("OUTPATIENT_PREVIEW_DATASET_ID", ""))
            batch = os.environ.get("OUTPATIENT_PREVIEW_BATCH_DATE")
            run = os.environ.get("OUTPATIENT_PREVIEW_BUILD_RUN_ID")
            if not batch or not run or database_options()["database"] != {"dev": "hospital", "test": "hospital_test"}.get(env):
                raise OdsError("N10_SOURCE_STALE")
            data = load(query.start_date, query.end_date, scope, env=env,
                        dataset_id=dataset, batch_date=batch, build_run_id=run, **options)
        except OdsError as error:
            if error.code in {"N10_RANGE_OUTSIDE_BUILD", "N10_DEPARTMENT_UNKNOWN"}:
                raise HTTPException(422, "Dates or department outside certified build coverage") from None
            raise HTTPException(503, f"Certified {resource} mart unavailable or incomplete") from None
    except HTTPException as error:
        audit_access(user, "query", user.department, error.status_code, resource=resource)
        raise
    audit_access(user, "query", scope, 200, resource=resource)
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}
