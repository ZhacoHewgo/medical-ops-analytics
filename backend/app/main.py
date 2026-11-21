from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routers.dashboard_router import router as dashboard_router
from app.core.config import get_settings


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


async def auth_routes_disabled():
    raise HTTPException(404, "Not Found")


def create_app() -> FastAPI:
    auth_enabled = get_settings().auth_enabled
    application = FastAPI(
        docs_url="/docs" if auth_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if auth_enabled else None,
    )
    application.add_exception_handler(StarletteHTTPException, http_error)
    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_api_route(
        "/health", health_check, methods=["GET"], tags=["系统"], summary="健康检查",
        responses={200: {"description": "服务可用", "content": {"application/json": {"example": {"status": "ok"}}}}},
    )
    application.include_router(dashboard_router)
    from app.api.routers.auth_router import router as auth_router
    from app.api.routers.disease_router import router as disease_router
    from app.api.routers.inpatient_router import router as inpatient_router
    from app.api.routers.llm_router import router as llm_router
    from app.api.routers.operations_router import router as operations_router
    from app.api.routers.outpatient_router import router as outpatient_router
    from app.api.routers.quality_router import router as quality_router
    from app.api.routers.resource_router import router as resource_router
    from app.api.routers.revenue_router import router as revenue_router
    from app.api.routers.task_router import router as task_router

    dependencies = [] if auth_enabled else [Depends(auth_routes_disabled)]
    for router in (
        auth_router, operations_router, task_router, quality_router, outpatient_router,
        inpatient_router, revenue_router, disease_router, resource_router, llm_router,
    ):
        application.include_router(router, dependencies=dependencies)
    return application


app = create_app()
