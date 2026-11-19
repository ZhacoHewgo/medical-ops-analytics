import json
from collections import Counter
from ipaddress import ip_address

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.outpatient import strict_date, validate_aggregate
from app.outpatient_preview import load_preview
from etl.config.source_config import OdsError


async def http_error(request, error):
    return JSONResponse(
        status_code=error.status_code,
        content={"code": error.status_code, "msg": str(error.detail), "data": None},
    )


async def validation_error(request, error):
    return JSONResponse(status_code=422, content={"code": 422, "msg": "Invalid query parameters", "data": None})


async def health_check():
    return {"status": "ok"}


async def overview(start_date: str | None = None, end_date: str | None = None, dept_code: str | None = None):
    try:
        path = get_settings().outpatient_data_path
        document = validate_aggregate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise HTTPException(status_code=503, detail="Outpatient aggregate unavailable or invalid; rebuild required") from None
    return summarize_outpatient(document, start_date, end_date, dept_code)


def check_dates(start, end):
    try:
        for day in (start, end):
            if day is not None:
                strict_date(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be valid calendar dates in YYYY-MM-DD format") from None
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")


def summarize_outpatient(document, start_date, end_date, dept_code):
    meta = document["meta"]
    start = meta["available_start_date"] if start_date is None else start_date
    end = meta["available_end_date"] if end_date is None else end_date
    check_dates(start, end)
    if dept_code is not None and dept_code not in meta["department_codes"]:
        raise HTTPException(status_code=422, detail="Unknown dept_code")
    daily, departments = Counter(), Counter()
    for item in document["counts"]:
        if start <= item["date"] <= end and (dept_code is None or item["dept_code"] == dept_code):
            daily[item["date"]] += item["visits"]
            departments[item["dept_code"]] += item["visits"]
    return {"code": 0, "msg": "ok", "data": {
        "filters": {"start_date": start, "end_date": end, "dept_code": dept_code},
        "outpatient_visits": sum(daily.values()),
        "daily_trend": [{"date": day, "visits": count} for day, count in sorted(daily.items())],
        "department_ranking": [
            {"dept_code": code, "visits": count}
            for code, count in sorted(departments.items(), key=lambda item: (-item[1], item[0]))
        ],
        "meta": meta,
    }}


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


def create_app() -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.add_exception_handler(StarletteHTTPException, http_error)
    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_api_route("/health", health_check, methods=["GET"])
    application.add_api_route("/api/dashboard/overview", overview, methods=["GET"])
    application.add_api_route(
        "/api/dashboard/outpatient-preview", outpatient_preview, methods=["GET"],
        summary="汇总已认证宽表的门诊人次", tags=["门诊预览"],
        responses={403: {"description": "仅本机预览"}, 422: {"description": "筛选无效"},
                   503: {"description": "认证构建不可用或已过期"}},
    )
    return application


app = create_app()
