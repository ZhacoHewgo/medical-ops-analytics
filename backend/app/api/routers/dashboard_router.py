from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.responses import VALIDATION_EXAMPLE
from app.core.cache import load_demo_aggregate
from app.core.config import get_settings
from app.outpatient_response import check_dates, summarize_outpatient
from app.outpatient_preview import load_preview
from etl.config.source_config import OdsError


router = APIRouter(prefix="/api/dashboard")


@router.get("/overview", summary="门诊驾驶舱", tags=["门诊驾驶舱"],
            responses={503: {"description": "门诊聚合不可用或已失效，需重建"}})
async def get_overview(start_date: str | None = None, end_date: str | None = None, dept_code: str | None = None):
    try:
        path = get_settings().outpatient_data_path
        document = load_demo_aggregate(path)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise HTTPException(status_code=503, detail="Outpatient aggregate unavailable or invalid; rebuild required") from None
    return summarize_outpatient(document, start_date, end_date, dept_code)


@router.get(
    "/outpatient-preview", summary="汇总已认证宽表的门诊人次", tags=["门诊预览"],
    responses={403: {"description": "仅本机预览"},
               422: {"description": "筛选无效",
                     "content": {"application/json": {"example": VALIDATION_EXAMPLE}}},
               503: {"description": "认证构建不可用或已过期"}},
)
def outpatient_preview(
    request: Request, response: Response, start_date: str | None = None, end_date: str | None = None,
    dept_code: str | None = None,
):
    try:
        local = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise HTTPException(status_code=403, detail="Outpatient preview is local only")
    check_dates(start_date, end_date)
    if dept_code is not None and (not dept_code.strip() or len(dept_code) > 64):
        raise HTTPException(status_code=422, detail="Invalid dept_code")
    try:
        document = load_preview(start_date=start_date, end_date=end_date, dept_code=dept_code)
    except (OdsError, ImportError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(status_code=503, detail="Certified outpatient build unavailable or stale") from None
    meta = document["meta"]
    first, last = meta["available_start_date"], meta["available_end_date"]
    if any(day is not None and not first <= day <= last for day in (start_date, end_date)):
        raise HTTPException(status_code=422, detail="Dates must be within certified build coverage")
    response.headers["Cache-Control"] = "no-store"
    return summarize_outpatient(document, start_date, end_date, dept_code)
