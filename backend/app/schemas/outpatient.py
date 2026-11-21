from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    code: int
    msg: str
    data: None


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
