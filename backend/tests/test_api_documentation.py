import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from app.api.responses import UNAUTHORIZED_EXAMPLE, VALIDATION_EXAMPLE
from app.main import create_app


SECRET = "p" * 32

PUBLIC_NO_401 = {
    ("GET", "/api/dashboard/overview"),
    ("GET", "/api/dashboard/outpatient-preview"),
    ("GET", "/health"),
}

LOGIN_CREDENTIAL_401 = {
    ("POST", "/api/auth/login"),
}

VALIDATION_422 = {
    ("GET", "/api/dashboard/outpatient-preview"),
    ("GET", "/api/v1/disease/summary"),
    ("GET", "/api/v1/inpatient/summary"),
    ("GET", "/api/v1/outpatient/summary"),
    ("GET", "/api/v1/overview/summary"),
    ("GET", "/api/v1/reports/outpatient"),
    ("GET", "/api/v1/reports/multidimensional"),
    ("GET", "/api/v1/resource/summary"),
    ("GET", "/api/v1/revenue/summary"),
    ("POST", "/api/llm/anomaly"),
    ("POST", "/api/llm/kb"),
    ("POST", "/api/llm/query"),
    ("POST", "/api/llm/report"),
    ("POST", "/api/v1/reports/export"),
    ("POST", "/api/v1/reports/multidimensional/export"),
    ("PUT", "/api/ops/sources/registration"),
}


def schema_operations(schema):
    operations = {}
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                operations[(method.upper(), path)] = operation
    return operations


def example_of(operation, code):
    body = operation.get("responses", {}).get(str(code), {})
    return body.get("content", {}).get("application/json", {}).get("example")


def call_unauthenticated(client, method, path):
    path = path.replace("{user_id}", "1").replace("{name}", "extract")
    if method == "GET":
        return client.get(path)
    return {"POST": client.post, "PUT": client.put, "PATCH": client.patch}[method](path, json={})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    return create_app()


@pytest.fixture
def operations(enabled):
    return schema_operations(enabled.openapi())


def test_schema_keeps_documented_surface(operations):
    assert len(operations) == 32
    assert len({path for _, path in operations}) == 29


def test_every_operation_has_tag_and_summary(operations):
    missing = [key for key, operation in operations.items() if not operation.get("tags") or not operation.get("summary")]
    assert missing == []


def test_public_and_loopback_endpoints_document_no_401(operations):
    for key in PUBLIC_NO_401:
        assert "401" not in operations[key]["responses"], key


def test_authenticated_endpoints_document_401_example(operations):
    for key, operation in operations.items():
        if key in PUBLIC_NO_401:
            continue
        assert "401" in operation["responses"], key
        assert example_of(operation, 401) is not None, key


def test_unauthenticated_requests_match_401_schema(enabled, operations):
    client = TestClient(enabled)
    for key, operation in operations.items():
        if key in LOGIN_CREDENTIAL_401:
            continue
        response = call_unauthenticated(client, *key)
        if "401" in operation["responses"]:
            assert response.status_code == 401, f"{key} expected 401, got {response.status_code}"
        else:
            assert response.status_code != 401, f"{key} should not be 401, got {response.status_code}"


def test_login_rejects_wrong_credentials_with_401(enabled, monkeypatch):
    from app.api.routers import auth_router

    monkeypatch.setattr(auth_router, "database_cursor",
                        lambda *args, **kwargs: contextlib.nullcontext(enter_result=object()))
    monkeypatch.setattr(auth_router, "read_user", lambda cursor, username=None: None)
    monkeypatch.setattr(auth_router, "dummy_hash", lambda: b"stub")
    monkeypatch.setattr(auth_router, "verify_password", lambda password, stored: False)
    monkeypatch.setattr(auth_router, "log_action", lambda *args, **kwargs: None)

    response = TestClient(enabled).post(
        "/api/auth/login", json={"username": "ghost", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_422_examples_only_on_validation_interfaces(operations):
    actual = {key for key, operation in operations.items() if example_of(operation, 422) is not None}
    assert actual == VALIDATION_422


@pytest.mark.parametrize("code,example", [(401, UNAUTHORIZED_EXAMPLE), (422, VALIDATION_EXAMPLE)])
def test_error_examples_match_response_envelope(operations, code, example):
    for key, operation in operations.items():
        body = example_of(operation, code)
        if body is None:
            continue
        assert body["code"] == code
        assert set(body) <= set(example)
        assert {"code", "msg"} <= set(body)


def test_error_examples_do_not_leak_internals(operations):
    dumped = json.dumps(list(operations.values()), ensure_ascii=False)
    for probe in ("ODS_", "mysql://", "auth_audit", "Traceback"):
        assert probe not in dumped


def test_docs_follow_the_auth_switch(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_docs_stay_closed_without_explicit_enable(monkeypatch, value):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("AUTH_ENABLED", value)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_disabled_app_hides_business_routes(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    client = TestClient(create_app())
    assert client.get("/health").status_code == 200
    assert client.get("/api/ops/sources").status_code == 404
