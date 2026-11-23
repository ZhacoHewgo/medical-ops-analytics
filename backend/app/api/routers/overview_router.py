import asyncio
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from app.api.routers.outpatient_router import ERRORS, audit_access
from app.core.auth_store import Account
from app.core.deps import get_current_user
from app.services.theme_service import DateScope, OverviewSummary, summary_arguments, summary_error
from etl.config.source_config import OdsError
from etl.jobs.ads_indicators import summarize_overview


router = APIRouter(prefix="/api/v1/overview", tags=["管理驾驶舱"])
BATCH_WAIT_SECONDS = 0.075


@dataclass
class _Batches:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: asyncio.Task | None = None


_batches = {}


async def _run_batch(key, state, args, context):
    async with state.lock:
        try:
            await asyncio.sleep(BATCH_WAIT_SECONDS)
            state.waiting = None
            return await run_in_threadpool(summarize_overview, *args, **context)
        finally:
            if state.waiting is asyncio.current_task():
                state.waiting = None
            if state.waiting is None:
                _batches.pop(key)


def _observe_result(task):
    if not task.cancelled():
        task.exception()


async def _overview_batch(args, context):
    key = (asyncio.get_running_loop(), *args, *context.items())
    state = _batches.get(key)
    if state is None:
        state = _batches[key] = _Batches()
    if state.waiting is None:
        state.waiting = asyncio.create_task(_run_batch(key, state, args, context))
        state.waiting.add_done_callback(_observe_result)
    return await asyncio.shield(state.waiting)


@router.get("/summary", response_model=OverviewSummary, responses=ERRORS, summary="读取全院运营总览")
async def get_overview(query: Annotated[DateScope, Query()], response: Response,
                       user: Annotated[Account, Depends(get_current_user)]):
    try:
        try:
            args, context = summary_arguments(query, user)
            data = await _overview_batch(args, context)
        except OdsError as error:
            raise summary_error(error, "overview") from None
    except HTTPException as error:
        await run_in_threadpool(audit_access, user, "query", user.department, error.status_code, resource="overview")
        raise
    await run_in_threadpool(audit_access, user, "query", args[2], 200, resource="overview")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}
