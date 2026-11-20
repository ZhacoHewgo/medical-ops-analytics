"""Read operations metadata and perform audited platform maintenance actions."""
from dataclasses import asdict
from ipaddress import ip_address
import os
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from app.api.routers.auth_router import Admin, _current_admin
from app.core.auth_store import Account
from app.core.deps import require_role
from app.core.security import unauthorized
from app import operations
from etl.config import source_config as sources
from etl.scheduler import policy, scheduler

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


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
    batch_date: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    expected_version: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class RegisteredBatch(BaseModel):
    dataset_id: str
    batch_date: str
    sources: int


class RegistrationState(BaseModel):
    enabled: bool
    version: str
    batches: list[RegisteredBatch]


class RegistrationReceipt(BaseModel):
    changed: bool
    version: str
    dataset_id: str
    batch_date: str


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


class ScheduleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    hour: int = Field(strict=True, ge=0, le=23)
    minute: int = Field(strict=True, ge=0, le=59)
    expected_version: int = Field(strict=True, ge=0, le=4294967295)


class ScheduleState(BaseModel):
    enabled: bool
    hour: int
    minute: int
    version: int
    can_edit: bool
    timezone: Literal["Asia/Shanghai"]


class ScheduleReceipt(ScheduleState):
    changed: bool


class RunReceipt(BaseModel):
    outcome: Literal["SUCCESS", "FAILED", "INDETERMINATE"]
    result_code: str



def _reply(response, data):
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}


def _admit(request, feature="OPS_MANUAL_RUN_ENABLED"):
    try:
        return (os.environ.get(feature) == "1"
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


@router.get("/sources/registration", response_model=Result[RegistrationState], responses=ERRORS,
            summary="读取数据源登记版本与变更权限")
def registration_status(actor: Admin, request: Request, response: Response):
    try:
        data = operations.registration_state()
    except sources.OdsError:
        raise HTTPException(503, "Source registry unavailable") from None
    return _reply(response, {**data, "enabled": _admit(request, "OPS_REGISTRY_WRITE_ENABLED")})


@router.put("/sources/registration", response_model=Result[RegistrationReceipt],
            responses={**ERRORS, 409: {"description": "登记已变化或采集正在运行"},
                       422: {"description": "批次或六类登记文件不符合采集合同"}},
            summary="核对六类文件后原子登记整个数据批次")
def register_batch(body: Registration, actor: Admin, request: Request, response: Response):
    if not _admit(request, "OPS_REGISTRY_WRITE_ENABLED"):
        raise HTTPException(403, "Registry changes require explicit local enablement")
    try:
        sources.validate_batch(body.batch_date)
    except sources.OdsError:
        raise HTTPException(422, "Invalid batch date") from None
    try:
        outcome, version = operations.switch_registration(
            actor, body.dataset_id, body.batch_date, body.expected_version, _current_admin,
        )
    except scheduler.EtlError as error:
        if error.code == "ETL_RUN_LOCKED":
            raise HTTPException(409, "Another ODS run holds the scheduler lock") from None
        raise HTTPException(503, "Registry lock unavailable") from None
    except sources.OdsError:
        raise HTTPException(503, "Source registry or audit unavailable") from None
    if outcome == "STALE":
        raise HTTPException(409, "Source registry changed; reload before switching")
    if outcome == "FILE_CHECK_FAILED":
        raise HTTPException(422, "Six validated source files are required")
    if outcome == "UNAUTHORIZED":
        raise unauthorized()
    return _reply(response, {"changed": outcome == "CHANGED", "version": version,
                            "dataset_id": body.dataset_id, "batch_date": body.batch_date})


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


@router.get("/tasks/schedule", response_model=Result[ScheduleState], responses=ERRORS, summary="查看每日采集的配置")
def schedule_status(actor: Reader, request: Request, response: Response):
    try:
        configured = policy.read_policy(operations.environment())
    except sources.OdsError:
        raise HTTPException(503, "Schedule settings unavailable") from None
    return _reply(response, {**asdict(configured), "timezone": "Asia/Shanghai",
                            "can_edit": actor.role == "admin" and _admit(request, "OPS_SCHEDULE_WRITE_ENABLED")})


@router.put("/tasks/schedule", response_model=Result[ScheduleReceipt],
            responses={**ERRORS, 409: {"description": "配置已变化或正在更新"}},
            summary="启停每日采集并调整前台托管调度时间")
def change_schedule(body: ScheduleChange, actor: Admin, request: Request, response: Response):
    if not _admit(request, "OPS_SCHEDULE_WRITE_ENABLED"):
        raise HTTPException(403, "Schedule changes require explicit local enablement")
    try:
        outcome, configured = operations.update_schedule(
            operations.environment(), actor, body.expected_version, body.enabled,
            body.hour, body.minute, _current_admin,
        )
    except scheduler.EtlError as error:
        if error.code == "ETL_SCHEDULER_LOCKED":
            raise HTTPException(409, "Schedule settings are being updated") from None
        raise HTTPException(503, "Schedule lock unavailable") from None
    except sources.OdsError:
        raise HTTPException(503, "Schedule settings or audit unavailable") from None
    if outcome == "STALE":
        raise HTTPException(409, "Schedule settings have changed; reload before editing")
    if outcome == "UNAUTHORIZED":
        raise unauthorized()
    return _reply(response, {**asdict(configured), "timezone": "Asia/Shanghai", "can_edit": True,
                            "changed": outcome == "CHANGED"})


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
