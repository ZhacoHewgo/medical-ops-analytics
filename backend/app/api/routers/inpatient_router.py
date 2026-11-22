from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from app.api.routers.outpatient_router import ERRORS
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.schemas.outpatient import Filters
from app.services.theme_service import DateScope, DetailRow, Metric, serve_summary
from etl.jobs.ads_indicators import summarize_inpatient


router = APIRouter(prefix="/api/v1/inpatient", tags=["住院集市"])


class DischargeDay(BaseModel):
    date: str
    discharges: int
    avg_stay_days: str | None = None


class InpatientPublication(BaseModel):
    source: Literal["ads_inpatient_theme"]
    batch_date: str
    build_run_id: str
    available_start_date: str
    available_end_date: str


class InpatientSummary(BaseModel):
    class Data(BaseModel):
        filters: Filters
        discharges: int
        admissions: int | None = None
        bed_usage_rate: Metric | None = None
        avg_stay_days: Metric
        avg_cost: Metric
        daily_trend: list[DischargeDay]
        meta: InpatientPublication
        details: dict[str, list[DetailRow]]

    code: Literal[0]
    msg: str
    data: Data


@router.get("/summary", response_model=InpatientSummary, responses=ERRORS, summary="读取已发布的住院运营汇总")
def get_inpatient_metrics(query: Annotated[DateScope, Query()], response: Response,
                          user: Annotated[Account, Depends(get_current_user)]):
    return serve_summary(query, response, user, "inpatient", summarize_inpatient)
