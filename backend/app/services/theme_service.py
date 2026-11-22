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


class DetailRow(BaseModel):
    name: str
    label: str
    count: int
    value: str | None
    group: str | None


class Publication(BaseModel):
    source: str
    batch_date: str
    build_run_id: str
    available_start_date: str
    available_end_date: str


class ThemePublication(Publication):
    source: Literal["ads_income_theme", "ads_drg_theme", "ads_efficiency_theme"]


class ThemeSummary(BaseModel):
    class Data(BaseModel):
        filters: Filters
        meta: ThemePublication
        metric: str
        metrics: dict[str, Metric]
        group_by: Literal["day", "department"]
        rows: list[ThemeRow]
        details: dict[str, list[DetailRow]]

    code: Literal[0]
    msg: str
    data: Data


class DepartmentBeds(BaseModel):
    bed_count: int | None
    available_bed_days: int | None
    occupied_bed_days: int
    bed_usage_rate: str | None
    bed_turnover: str | None = None


class ResourceSummary(ThemeSummary):
    class Data(ThemeSummary.Data):
        department_beds: dict[str, DepartmentBeds] = Field(default_factory=dict)

    data: Data


class OverviewSummary(BaseModel):
    class BedDay(BaseModel):
        date: str
        available: int
        occupied: int

    class Activity(BaseModel):
        date: str
        kind: Literal["OUTPATIENT", "INPATIENT"]
        count: int

    class DailyCharge(BaseModel):
        date: str
        amount: str | None

    class Department(BaseModel):
        code: str
        label: str
        outpatient: int
        inpatient: int
        amount: str | None

    class Data(BaseModel):
        filters: Filters
        meta: Publication
        activity: list["OverviewSummary.Activity"]
        charges: list[DetailRow]
        charge_total: str | None
        daily_charges: list["OverviewSummary.DailyCharge"]
        departments: list["OverviewSummary.Department"]
        bed_usage_rate: Metric | None = None
        bed_trend: list["OverviewSummary.BedDay"] = Field(default_factory=list)

    code: Literal[0]
    msg: str
    data: Data


def serve_summary(query, response, user, resource, load, *, action="query", **options):
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
            if error.code in {"N10_RANGE_OUTSIDE_BUILD", "N10_DEPARTMENT_UNKNOWN", "N10_REPORT_TOO_LARGE"}:
                raise HTTPException(422, "Dates or department outside certified build coverage") from None
            raise HTTPException(503, f"Certified {resource} mart unavailable or incomplete") from None
    except HTTPException as error:
        audit_access(user, action, user.department, error.status_code, resource=resource)
        raise
    audit_access(user, action, scope, 200, resource=resource)
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}
