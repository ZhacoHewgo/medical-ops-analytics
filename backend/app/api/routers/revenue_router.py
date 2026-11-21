from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.routers.outpatient_router import ERRORS
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.services.theme_service import ThemeQuery, ThemeSummary, serve_summary
from etl.jobs.ads_indicators import summarize_theme


router = APIRouter(prefix="/api/v1/revenue", tags=["收入集市"])


@router.get("/summary", response_model=ThemeSummary, responses=ERRORS, summary="查询收费与收入汇总")
def list_metrics(query: Annotated[ThemeQuery, Query()], response: Response,
                 user: Annotated[Account, Depends(get_current_user)]):
    return serve_summary(query, response, user, "revenue", partial(summarize_theme, "income"), drill=query.drill)
