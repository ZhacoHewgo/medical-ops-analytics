from importlib import import_module

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.outpatient import DEFAULT_DATA_PATH


def test_placeholder_imports_preserve_health_and_routes(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "missing.json"))
    for module in ("app.core.security", "app.core.deps"):
        import_module(module)

    application = create_app()
    assert {route.path for route in application.routes} == {
        "/health", "/api/dashboard/overview", "/api/dashboard/outpatient-preview",
    }
    with TestClient(application) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        response = client.get("/api/dashboard/overview")
        assert response.status_code == 503
        assert response.json()["data"] is None


def test_placeholder_imports_preserve_configuration(monkeypatch, tmp_path):
    for module in ("app.core.security", "app.core.deps"):
        import_module(module)

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OUTPATIENT_DATA_PATH", raising=False)
    settings = get_settings()
    assert settings.database_url == ""
    assert settings.outpatient_data_path == DEFAULT_DATA_PATH

    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "changed.json"))
    assert get_settings().outpatient_data_path == tmp_path / "changed.json"
