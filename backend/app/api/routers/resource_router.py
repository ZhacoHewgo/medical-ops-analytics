from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.routers.outpatient_router import ERRORS
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.services.theme_service import ResourceSummary, ThemeQuery, serve_summary
from etl.jobs.ads_indicators import summarize_theme


router = APIRouter(prefix="/api/v1/resource", tags=["资源集市"])


@router.get("/summary", response_model=ResourceSummary, responses=ERRORS, summary="查询出院人次与床位数据状态")
def list_metrics(query: Annotated[ThemeQuery, Query()], response: Response,
                 user: Annotated[Account, Depends(get_current_user)]):
    return serve_summary(query, response, user, "resource", partial(summarize_theme, "efficiency"), drill=query.drill)
