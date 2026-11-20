"""Opt-in authentication and administrator-only account creation."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, SecretStr, StringConstraints

from app.core.auth_store import DB_DOWN, DEPARTMENT, USERNAME, insert_user, log_action, read_user
from app.core.deps import require_role
from app.core.security import (
    create_access_token, dummy_hash, hash_password, token_config, unauthorized, verify_password,
)
from etl.config.source_config import OdsError, database_cursor


router = APIRouter(prefix="/api/auth", tags=["认证"])
Username = Annotated[str, StringConstraints(pattern=USERNAME)]
Department = Annotated[str, StringConstraints(pattern=DEPARTMENT)]


class LoginRequest(BaseModel):
    username: Username
    password: SecretStr


class NewUser(LoginRequest):
    role: Literal["admin", "analyst", "department"]
    department: Department | None = None


@router.post("/login")
def login(body: LoginRequest, response: Response):
    token_config()
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            user = read_user(cursor, username=body.username)
            stored = user.password_hash if user is not None else dummy_hash()
            verified = verify_password(body.password.get_secret_value(), stored)
            allowed = user is not None and user.enabled and verified
            log_action(cursor, None if user is None else user.user_id, "success" if allowed else "denied")
    except OdsError:
        raise HTTPException(503, "Authentication unavailable") from None
    if not allowed:
        raise unauthorized()
    response.headers["Cache-Control"] = "no-store"
    return {"code": 0, "msg": "ok", "data": {
        "access_token": create_access_token(user), "token_type": "bearer", "user": user.public(),
    }}


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: NewUser, admin=Depends(require_role(("admin",)))):
    if (body.role == "department") != (body.department is not None):
        raise HTTPException(422, "Department is required only for department accounts")
    try:
        stored = hash_password(body.password.get_secret_value())
    except ValueError:
        raise HTTPException(422, "Password must be 12 to 72 UTF-8 bytes") from None
    try:
        with database_cursor(DB_DOWN, write=True) as cursor:
            current = read_user(cursor, user_id=admin.user_id, lock=True)
            authorized = current is not None and current.enabled and current.role == "admin" and current.revision == admin.revision
            user_id = insert_user(cursor, body.username, stored, body.role, body.department) if authorized else None
    except OdsError:
        raise HTTPException(503, "Authentication unavailable") from None
    if not authorized:
        raise unauthorized()
    if user_id is None:
        raise HTTPException(409, "Username already exists")
    return {"code": 0, "msg": "ok", "data": {
        "user_id": user_id, "username": body.username, "role": body.role, "department": body.department,
    }}
