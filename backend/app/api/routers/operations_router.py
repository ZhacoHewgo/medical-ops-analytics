"""Read operations metadata and perform audited platform maintenance actions."""
from ipaddress import ip_address
import os
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.routers.auth_router import Admin, _current_admin
from app.core.auth_store import Account
from app.core.deps import require_role
from app.core.security import unauthorized
from app import operations
from etl.config import source_config as sources

router = APIRouter(prefix="/api/ops", tags=["数据源与任务"])
Reader = Annotated[Account, Depends(require_role(("admin", "analyst")))]
Payload = TypeVar("Payload")
ERRORS = {401: {"description": "登录已失效"}, 403: {"description": "角色或环境不允许"},
          503: {"description": "登记、任务状态或审计不可用"}}
CheckOutcome = Literal["SUCCESS", "FAILED"]


class Result(BaseModel, Generic[Payload]):
    code: Literal[0]
    msg: str
    data: Payload


class FileCheck(BaseModel):
    outcome: CheckOutcome
    at: str


class SourceStatus(BaseModel):
    name: str
    registered: bool
    dataset_id: str | None
    batch_date: str | None
    last_check: FileCheck | None


class CheckReceipt(BaseModel):
    name: str
    outcome: CheckOutcome


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



def _reply(response, data):
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}


def _admit(request):
    try:
        return (os.environ.get("OPS_MANUAL_RUN_ENABLED") == "1"
                and request.client is not None and ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


def _start(actor, action, name, info=None):
    try:
        ticket = operations.begin_action(actor, action, name, info, _current_admin)
    except sources.OdsError:
        raise HTTPException(503, "Operations audit unavailable") from None
    if ticket is None:
        raise unauthorized()
    return ticket


def _finish(ticket, outcome, code):
    try:
        operations.finish_action(ticket, outcome, code)
    except sources.OdsError:
        raise HTTPException(503, "Operation result could not be recorded; check status before retrying") from None


@router.get("/sources", response_model=Result[list[SourceStatus]], responses=ERRORS, summary="查看已登记的数据源和最近检查")
def source_list(actor: Reader, response: Response):
    try:
        data = operations.read_sources()
    except sources.OdsError:
        raise HTTPException(503, "Source registry unavailable") from None
    return _reply(response, data)


@router.post("/sources/{name}/check", response_model=Result[CheckReceipt],
             responses={**ERRORS, 404: {"description": "未知数据源"}, 409: {"description": "数据源未登记"}},
             summary="检查登记文件可读取且符合格式")
def source_check(name: str, actor: Admin, response: Response):
    if name not in sources.SOURCES:
        raise HTTPException(404, "Unknown source")
    try:
        info = sources.get_source(name)
    except sources.OdsError as error:
        if error.code == "ODS_SOURCE_NOT_REGISTERED":
            raise HTTPException(409, "Source is not registered") from None
        raise HTTPException(503, "Source registry unavailable") from None
    ticket = _start(actor, "source_check", name, info)
    try:
        sources.read_source(name, info)
    except sources.OdsError:
        outcome, code = "FAILED", "OPS_FILE_CHECK_FAILED"
    else:
        outcome, code = "SUCCESS", "ETL_OK"
    _finish(ticket, outcome, code)
    return _reply(response, {"name": name, "outcome": outcome})


@router.get("/tasks", response_model=Result[TaskStatus], responses=ERRORS, summary="查看采集、清洗、质检与宽表的运行摘要")
def task_list(actor: Reader, request: Request, response: Response):
    try:
        items = operations.read_tasks(operations.environment())
    except (sources.OdsError, OSError, ValueError, TypeError, RecursionError):
        raise HTTPException(503, "Task status unavailable") from None
    return _reply(response, {"items": items, "can_run": actor.role == "admin" and _admit(request)})


@router.post("/tasks/extract/run", response_model=Result[RunReceipt],
             responses={**ERRORS, 409: {"description": "已有采集任务占用调度锁"}},
             summary="手动执行一次现有 ODS 采集任务")
def run_task(actor: Admin, request: Request, response: Response):
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
