from typing import Any


UNAUTHORIZED_EXAMPLE: dict[str, Any] = {"code": 401, "msg": "Authentication required", "data": None}
VALIDATION_EXAMPLE: dict[str, Any] = {"code": 422, "msg": "Invalid query parameters", "data": None}
