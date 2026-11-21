from fastapi.testclient import TestClient

from app.main import create_app


def test_public_dashboard_rejects_an_unreadable_snapshot(api_client, monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(tmp_path / "missing.json"))
    response = api_client.get("/api/dashboard/overview")
    assert response.status_code == 503
    assert response.json()["data"] is None


def test_certified_query_modules_require_a_session(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/outpatient/summary").status_code == 401
        assert client.get("/api/v1/reports/outpatient").status_code == 401
        assert client.post("/api/v1/reports/export", json={}).status_code == 401
        assert client.post("/api/llm/query", json={"question": "2025-02全院门诊人次"}).status_code == 401
