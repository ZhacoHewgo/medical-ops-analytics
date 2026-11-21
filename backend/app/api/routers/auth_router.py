"""Opt-in authentication and administrator-only account management."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StringConstraints

from app.api.responses import UNAUTHORIZED_EXAMPLE
from app.core.auth_store import (
    DB_DOWN, DEPARTMENT, USERNAME, Account, insert_user, list_users, log_action, log_admin_change,
    read_user, update_user,
)
from app.core.deps import require_role
from app.core.security import (
    create_access_token, dummy_hash, hash_account_password, store_unavailable, token_config, unauthorized,
    verify_password,
)
from etl.config.source_config import OdsError, database_cursor


router = APIRouter(prefix="/api/auth", tags=["认证"])
Username = Annotated[str, StringConstraints(pattern=USERNAME)]
Department = Annotated[str, StringConstraints(pattern=DEPARTMENT)]
Role = Literal["admin", "analyst", "department"]
Admin = Annotated[Account, Depends(require_role(("admin",)))]
MANAGEMENT_ERRORS = {401: {"description": "登录已失效",
                           "content": {"application/json": {"example": UNAUTHORIZED_EXAMPLE}}},
                     403: {"description": "仅管理员可访问"},
                     503: {"description": "账号服务或审计不可用"}}
ROLES = (
    {"code": "admin", "label": "系统管理员", "permissions": ("read_hospital", "manage_accounts", "read_operations", "run_operations")},
    {"code": "analyst", "label": "运营分析师", "permissions": ("read_hospital", "read_operations")},
    {"code": "department", "label": "科室账号", "permissions": ("read_department",)},
)


class LoginRequest(BaseModel):
    username: Username
    password: SecretStr


class NewUser(LoginRequest):
    role: Role
    department: Department | None = None


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["analyst", "department"]
    department: Department | None = None
    enabled: StrictBool
    expected_revision: str = Field(min_length=19, max_length=26)


class ManagedUser(BaseModel):
    user_id: int
    username: str
    role: Role
    department: str | None
    enabled: bool
    revision: str


class UserList(BaseModel):
    class Data(BaseModel):
        items: list[ManagedUser]
        next_after: int | None

    code: Literal[0]
    msg: str
    data: Data


class UserUpdated(BaseModel):
    code: Literal[0]
    msg: str
    data: ManagedUser


class RoleList(BaseModel):
    class Entry(BaseModel):
        code: Role
        label: str
        permissions: tuple[str, ...]

    code: Literal[0]
    msg: str
    data: tuple[Entry, ...]


def _check_department(role, department):
    if (role == "department") != (department is not None):
        raise HTTPException(422, "Department is required only for department accounts")


def _current_admin(cursor, actor, *, lock=False):
    current = read_user(cursor, user_id=actor.user_id, lock=lock)
    return (current is not None and current.enabled and current.role == "admin"
            and current.revision == actor.revision)


@router.post("/login",
             responses={401: {"description": "用户名或密码错误",
                              "content": {"application/json": {"example": UNAUTHORIZED_EXAMPLE}}}})
def login(body: LoginRequest, response: Response):
    token_config()
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            user = read_user(cursor, username=body.username)
            stored = user.password_hash if user is not None else dummy_hash()
            verified = verify_password(body.password.get_secret_value(), stored)
            allowed = user is not None and user.enabled and verified
            log_action(cursor, None if user is None else user.user_id, "success" if allowed else "denied")
    except OdsError as error:
        raise store_unavailable(error) from None
    if not allowed:
        raise unauthorized()
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user), "token_type": "bearer", "user": user.public(),
    }}


@router.post("/users", status_code=status.HTTP_201_CREATED, responses=MANAGEMENT_ERRORS,
             summary="创建平台账号")
def create_user(body: NewUser, admin: Admin, response: Response):
    _check_department(body.role, body.department)
    try:
        stored = hash_account_password(body.password.get_secret_value())
    except ValueError:
        raise HTTPException(422, "Password must be 12 to 72 UTF-8 bytes") from None
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            authorized = _current_admin(cursor, admin, lock=True)
            user_id = insert_user(cursor, body.username, stored, body.role, body.department) if authorized else None
            if user_id is not None:
                log_admin_change(cursor, admin, read_user(cursor, user_id=user_id, lock=True))
    except OdsError as error:
        raise store_unavailable(error) from None
    if not authorized:
        raise unauthorized()
    if user_id is None:
        raise HTTPException(409, "Username already exists")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": {
        "user_id": user_id, "username": body.username, "role": body.role, "department": body.department,
    }}


@router.get("/users", response_model=UserList, responses=MANAGEMENT_ERRORS, summary="分页查看平台账号")
def get_users(admin: Admin, response: Response,
              after: Annotated[int, Query(ge=0, le=2**64 - 1)] = 0,
              limit: Annotated[int, Query(ge=1, le=100)] = 50):
    try:
        with database_cursor(DB_DOWN) as cursor:
            data = list_users(cursor, after, limit) if _current_admin(cursor, admin) else None
    except OdsError:
        raise HTTPException(503, "Account list unavailable") from None
    if data is None:
        raise unauthorized()
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": data}


@router.patch("/users/{user_id}", response_model=UserUpdated,
              responses={**MANAGEMENT_ERRORS, 404: {"description": "账号不存在"},
                         409: {"description": "账号已变化或属于受保护的管理员"}},
              summary="调整账号的角色、科室和启用状态")
def change_user(user_id: Annotated[int, Path(ge=1, le=2**64 - 1)], body: UserChange,
                admin: Admin, response: Response):
    _check_department(body.role, body.department)
    target = updated = None
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            authorized = _current_admin(cursor, admin, lock=True)
            if authorized:
                target = read_user(cursor, user_id=user_id, lock=True)
            if target and target.role != "admin" and target.revision.isoformat() == body.expected_revision:
                settings = (body.role, body.department, body.enabled)
                if settings == (target.role, target.department, bool(target.enabled)):
                    updated = target
                else:
                    updated = update_user(cursor, user_id, *settings)
                    log_admin_change(cursor, admin, updated, previous=target)
    except OdsError:
        raise HTTPException(503, "Account update unavailable") from None
    if not authorized:
        raise unauthorized()
    if target is None:
        raise HTTPException(404, "Account not found")
    if updated is None:
        raise HTTPException(409, "Account is protected or has changed; refresh before updating")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": updated.management_view()}


@router.get("/roles", response_model=RoleList, responses=MANAGEMENT_ERRORS, summary="查看内置角色权限")
def get_roles(admin: Admin, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": ROLES}
