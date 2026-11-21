from fastapi.testclient import TestClient
import pytest

from app.core.security import hash_account_password
from app.main import create_app


@pytest.mark.parametrize("password", ["short", "a" * 73])
def test_password_length_limits_utf8_bytes(password):
    with pytest.raises(ValueError, match="12 to 72 UTF-8 bytes"):
        hash_account_password(password)


def test_protected_dashboard_rejects_missing_and_invalid_credentials(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-only-local-signing-key-000000")
    with TestClient(create_app()) as client:
        for headers in ({}, {"Authorization": "Bearer invalid-local-token"}):
            response = client.get("/api/v1/outpatient/summary", headers=headers)
            assert response.status_code == 401
            assert response.json()["data"] is None
            assert response.headers["WWW-Authenticate"] == "Bearer"
