import json
from ipaddress import ip_address

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.outpatient import validate_aggregate
from app.outpatient_response import check_dates, summarize_outpatient
from app.outpatient_preview import load_preview
from etl.config.source_config import OdsError


async def http_error(request, error):
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
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
    auth_enabled = get_settings().auth_enabled
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url="/openapi.json" if auth_enabled else None)
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
    if auth_enabled:
        from app.api.routers.auth_router import router as auth_router
        from app.api.routers.outpatient_router import router as outpatient_router

        application.include_router(auth_router)
        application.include_router(outpatient_router)
    return application


app = create_app()
