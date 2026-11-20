"""Password hashing and access token verification."""
from datetime import datetime, timezone
from functools import cache
from secrets import token_bytes

import bcrypt
import jwt
from fastapi import HTTPException

from app.core.config import get_settings


ISSUER = "medical-ops-analytics"


def unauthorized():
    return HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def hash_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if not 12 <= len(raw) <= 72:
        raise ValueError("Password must be 12 to 72 UTF-8 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt())


def verify_password(password: str, stored: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored)
    except ValueError:
        return False


@cache
def dummy_hash() -> bytes:
    return bcrypt.hashpw(token_bytes(32), bcrypt.gensalt())


def token_config():
    settings = get_settings()
    try:
        minutes = int(settings.jwt_expire_minutes)
        if not 1 <= minutes <= 480 or len(settings.jwt_secret_key.encode("utf-8")) < 32:
            raise ValueError
    except ValueError:
        raise HTTPException(503, "Authentication unavailable") from None
    return settings.jwt_secret_key, minutes


def create_access_token(user) -> str:
    key, minutes = token_config()
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode({
        "iss": ISSUER, "iat": now, "exp": now + minutes * 60, "user_id": user.user_id,
        "role": user.role, "department": user.department, "rev": user.revision.isoformat(),
    }, key, algorithm="HS256")


def decode_token(token: str) -> dict:
    key, _ = token_config()
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], issuer=ISSUER,
                            options={"require": ["iss", "exp", "iat", "user_id", "role", "rev"]})
        if type(claims["user_id"]) is not int or claims["user_id"] < 1 or "department" not in claims:
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError:
        raise unauthorized() from None
    return claims
