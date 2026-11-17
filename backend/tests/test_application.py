import asyncio

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.main import app, create_app, health_check, http_error, validation_error
from app.outpatient import DEFAULT_DATA_PATH


def test_create_app_returns_independent_instances():
    first, second = create_app(), create_app()
    assert isinstance(app, FastAPI)
    assert first is not second and first is not app and second is not app
    assert first.routes is not second.routes
    assert first.exception_handlers is not second.exception_handlers
    first.state.test_marker = True
    assert not hasattr(second.state, "test_marker")


def test_health_check_is_registered(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "missing.json"))
    application = create_app()
    route = next(route for route in application.routes if route.path == "/health")
    assert route.endpoint is health_check
    assert asyncio.run(health_check()) == {"status": "ok"}
    with TestClient(application) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_default_environment_configuration(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OUTPATIENT_DATA_PATH", raising=False)
    settings = get_settings()
    assert settings.database_url == ""
    assert settings.outpatient_data_path == DEFAULT_DATA_PATH


def test_environment_configuration_is_read_fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "mysql://SIM_TEST_ONLY")
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "first.json"))
    first = get_settings()
    assert first.database_url == "mysql://SIM_TEST_ONLY"
    assert first.outpatient_data_path == tmp_path / "first.json"
    assert "mysql://SIM_TEST_ONLY" not in repr(first)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "second.json"))
    second = get_settings()
    assert second.database_url == ""
    assert second.outpatient_data_path == tmp_path / "second.json"


def test_factory_keeps_business_routes_and_handlers(aggregate_path, monkeypatch):
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
    application = create_app()
    assert application.exception_handlers[StarletteHTTPException] is http_error
    assert application.exception_handlers[RequestValidationError] is validation_error
    with TestClient(application) as client:
        response = client.get("/api/dashboard/overview")
        assert response.status_code == 200
        assert response.json()["data"]["outpatient_visits"] == 4
        response = client.get("/api/dashboard/overview", params={"dept_code": "A"})
        assert response.status_code == 200
        assert response.json()["data"]["outpatient_visits"] == 1
        response = client.get("/api/dashboard/overview", params={"start_date": "2025-1-1"})
        assert response.status_code == 422
        assert response.json()["code"] == 422 and response.json()["data"] is None
        assert client.get("/not-an-endpoint").json() == {"code": 404, "msg": "Not Found", "data": None}


def test_existing_app_reads_path_changes_after_creation(aggregate_path, monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
    with TestClient(app) as client:
        assert client.get("/api/dashboard/overview").status_code == 200
        monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "missing.json"))
        response = client.get("/api/dashboard/overview")
        assert response.status_code == 503
        assert response.json()["code"] == 503 and response.json()["data"] is None
        monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
        assert client.get("/api/dashboard/overview").status_code == 200
