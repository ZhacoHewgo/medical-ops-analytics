from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import operations
from app.api.routers.auth_router import Admin
from app.api.routers.operations_router import ERRORS, Reader, Result, _admit, _finish, _reply, _start
from etl.config import source_config as sources


router = APIRouter(prefix="/api/ops", tags=["任务"])


class TaskEvent(BaseModel):
    name: str
    run_id: str
    status: Literal["START", "SUCCESS", "FAILED", "BLOCKED", "PARTIAL"]
    code: str
    at: str
    rows: int | None
    attempt: int
    cost_ms: int


class TaskRow(BaseModel):
    name: str
    latest: TaskEvent | None


class TaskStatus(BaseModel):
    items: list[TaskRow]
    can_run: bool


class RunReceipt(BaseModel):
    outcome: Literal["SUCCESS", "FAILED", "INDETERMINATE"]
    result_code: str



@router.get("/tasks", response_model=Result[TaskStatus], responses=ERRORS, summary="查看采集、清洗、质检与宽表的运行摘要")
def get_task_status(actor: Reader, request: Request, response: Response):
    try:
        items = operations.read_tasks(operations.environment())
    except (sources.OdsError, OSError, ValueError, TypeError, RecursionError):
        raise HTTPException(503, "Task status unavailable") from None
    return _reply(response, {"items": items, "can_run": actor.role == "admin" and _admit(request)})


@router.post("/tasks/extract/run", response_model=Result[RunReceipt],
             responses={**ERRORS, 409: {"description": "已有采集任务占用调度锁"}},
             summary="手动执行一次现有 ODS 采集任务")
def trigger_task(actor: Admin, request: Request, response: Response):
    if not _admit(request):
        raise HTTPException(403, "Manual runs require explicit local enablement")
    try:
        env = operations.environment()
    except sources.OdsError:
        raise HTTPException(503, "Task environment unavailable") from None
    ticket = _start(actor, "extract_run", "extract_ods")
    outcome, code = operations.run_extract(env)
    _finish(ticket, outcome, code)
    if code == "ETL_RUN_LOCKED":
        raise HTTPException(409, "Another ODS run holds the scheduler lock")
    return _reply(response, {"outcome": outcome, "result_code": code})
