from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.routers.outpatient_router import ERRORS
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.services.theme_service import DateScope, OverviewSummary, serve_summary
from etl.jobs.ads_indicators import summarize_overview


router = APIRouter(prefix="/api/v1/overview", tags=["管理驾驶舱"])


@router.get("/summary", response_model=OverviewSummary, responses=ERRORS, summary="读取全院运营总览")
def get_overview(query: Annotated[DateScope, Query()], response: Response,
                 user: Annotated[Account, Depends(get_current_user)]):
    return serve_summary(query, response, user, "overview", summarize_overview)
