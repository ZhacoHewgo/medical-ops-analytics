"""Shared authentication and department scope checks for protected routes."""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.auth_store import DB_DOWN, read_user
from app.core.security import decode_token, unauthorized
from etl.config.source_config import OdsError, database_cursor


bearer = HTTPBearer(auto_error=False)


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None:
        raise unauthorized()
    claims = decode_token(credentials.credentials)
    try:
        with database_cursor(DB_DOWN) as cursor:
            user = read_user(cursor, user_id=claims["user_id"])
    except OdsError:
        raise HTTPException(503, "Authentication unavailable") from None
    if (user is None or not user.enabled or user.role != claims["role"]
            or user.department != claims["department"] or user.revision.isoformat() != claims["rev"]):
        raise unauthorized()
    return user


def require_role(roles):
    def need_role(user=Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(403, "Insufficient permissions")
        return user
    return need_role


def apply_data_scope(department: str | None, user) -> str | None:
    if user.role == "department":
        if not user.department or (department is not None and department != user.department):
            raise HTTPException(403, "Department outside authorized scope")
        return user.department
    if user.role not in {"admin", "analyst"}:
        raise HTTPException(403, "Insufficient permissions")
    return department
