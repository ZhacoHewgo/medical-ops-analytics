import json
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.outpatient import strict_date, validate_aggregate


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
    meta = document["meta"]
    start = meta["available_start_date"] if start_date is None else start_date
    end = meta["available_end_date"] if end_date is None else end_date
    try:
        strict_date(start)
        strict_date(end)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be valid calendar dates in YYYY-MM-DD format") from None
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
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


def create_app() -> FastAPI:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.add_exception_handler(StarletteHTTPException, http_error)
    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_api_route("/health", health_check, methods=["GET"])
    application.add_api_route("/api/dashboard/overview", overview, methods=["GET"])
    return application


app = create_app()
