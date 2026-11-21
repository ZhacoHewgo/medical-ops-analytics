"""Password hashing and access token verification."""
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import cache
from secrets import token_bytes

import bcrypt
import jwt
from fastapi import HTTPException

from app.core.config import get_settings


ISSUER = "medical-ops-analytics"
JWT_CONFIG = "configuration"
JWT_SECRET_MISSING = "configuration_secret_missing"
JWT_SECRET_SHORT = "configuration_secret_too_short"
JWT_TTL_INVALID = "configuration_ttl_invalid"
JWT_TTL_RANGE = "configuration_ttl_out_of_range"
JWT_CATEGORIES = (JWT_SECRET_MISSING, JWT_SECRET_SHORT, JWT_TTL_INVALID, JWT_TTL_RANGE)
STORE_UNCONFIGURED = "account_store_unconfigured"
STORE_INVALID = "account_store_invalid"
STORE_UNREACHABLE = "account_store_unreachable"
STORE_FAILED = "account_store_operation_failed"
STORE_CODES = {
    "ODS_DATABASE_URL_MISSING": STORE_UNCONFIGURED,
    "ODS_DEPENDENCY_MISSING": STORE_UNCONFIGURED,
    "ODS_DATABASE_URL_INVALID": STORE_INVALID,
    "ODS_TEST_DATABASE_REQUIRED": STORE_INVALID,
    "ODS_DATABASE_CONNECT_FAILED": STORE_UNREACHABLE,
}
DIAGNOSTIC = logging.getLogger("app.auth")


def unauthorized():
    return HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def unavailable(category):
    DIAGNOSTIC.warning("authentication unavailable: %s", category)
    return HTTPException(503, "Authentication unavailable", headers={"X-Auth-Unavailable": category})


def store_unavailable(error):
    return unavailable(STORE_CODES.get(getattr(error, "code", None), STORE_FAILED))


def _hash_password(password: str, minimum: int) -> bytes:
    raw = password.encode("utf-8")
    if not minimum <= len(raw) <= 72:
        raise ValueError(f"Password must be {minimum} to 72 UTF-8 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt())


def hash_password(password: str) -> bytes:
    return _hash_password(password, 1)


def hash_account_password(password: str) -> bytes:
    return _hash_password(password, 12)


def verify_password(password: str, stored: bytes) -> bool:
    try:
        raw = password.encode("utf-8")
        return 1 <= len(raw) <= 72 and bcrypt.checkpw(raw, stored)
    except ValueError:
        return False


@cache
def dummy_hash() -> bytes:
    return bcrypt.hashpw(token_bytes(32), bcrypt.gensalt())


def token_config():
    settings = get_settings()
    secret = settings.jwt_secret_key
    if not secret:
        raise unavailable(JWT_SECRET_MISSING) from None
    if len(secret.encode("utf-8")) < 32:
        raise unavailable(JWT_SECRET_SHORT) from None
    try:
        minutes = int(settings.jwt_expire_minutes)
    except (TypeError, ValueError):
        raise unavailable(JWT_TTL_INVALID) from None
    if not 1 <= minutes <= 480:
        raise unavailable(JWT_TTL_RANGE) from None
    return secret, minutes


def create_access_token(user) -> str:
    key, minutes = token_config()
    now = int(datetime.now(timezone.utc).timestamp())
    registered = {"iss": ISSUER, "iat": now, "exp": now + minutes * 60}
    if isinstance(user, Mapping):
        user_id = user.get("user_id", user.get("id"))
        role, department, revision = user.get("role"), user.get("department"), user.get("revision")
        if revision is None:
            subject = user.get("sub", user_id if user_id is not None else user.get("username"))
            if type(subject) is int and subject > 0:
                subject = str(subject)
            if not isinstance(subject, str) or not 1 <= len(subject) <= 128 or not subject.strip() or not subject.isprintable():
                raise ValueError("Invalid token subject: expected sub, user_id, id, or username")
            claims = {**registered, "sub": subject, "token_use": "subject", "rev": ""}
            if type(user_id) is int and user_id > 0:
                claims["user_id"] = user_id
            if isinstance(role, str) and role.strip():
                claims["role"] = role
            claims["department"] = department if isinstance(department, str) else None
            return jwt.encode(claims, key, algorithm="HS256")
    else:
        user_id, role, department, revision = user.user_id, user.role, user.department, user.revision
    if (type(user_id) is not int or user_id < 1 or not isinstance(role, str) or not role.strip()
            or department is not None and not isinstance(department, str)):
        raise ValueError("Invalid token subject")
    if isinstance(revision, str):
        revision = datetime.fromisoformat(revision)
    if not isinstance(revision, datetime):
        raise ValueError("Invalid account revision")
    return jwt.encode({
        **registered, "user_id": user_id, "role": role, "department": department, "rev": revision.isoformat(),
    }, key, algorithm="HS256")


def decode_token(token: str) -> dict:
    key, _ = token_config()
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], issuer=ISSUER,
                            options={"require": ["iss", "exp", "iat", "rev"]})
        if claims.get("token_use") == "subject":
            if claims["rev"] != "" or not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
                raise jwt.InvalidTokenError()
        elif (type(claims.get("user_id")) is not int or claims["user_id"] < 1
                or claims.get("role") is None or "department" not in claims):
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError:
        raise unauthorized() from None
    return claims
