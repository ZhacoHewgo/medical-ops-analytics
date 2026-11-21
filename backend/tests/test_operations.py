import csv
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import operations
from etl.config import source_config as sources
from etl.scheduler import scheduler
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


def test_operations_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get("/api/ops/sources").status_code == 401
        assert client.get("/api/ops/tasks").status_code == 401
        assert client.post("/api/ops/sources/his_outpatient/check").status_code == 401
        assert client.post("/api/ops/tasks/extract/run").status_code == 401


def test_task_summary_omits_raw_fields_and_rejects_unsafe_files(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path)
    assert all(item["latest"] is None for item in operations.read_tasks("test"))
    directory = tmp_path / "test"
    directory.mkdir()
    run_id = str(uuid4())
    log = directory / f"etl_test_20250101_run_all_{run_id}.log"
    row = {
        "timestamp": "2025-01-01T00:00:00+00:00", "env": "test",
        "name": "extract_ods", "run_id": run_id, "attempt": 1,
        "status": "SUCCESS", "code": "ETL_OK", "rows": 18, "cost_ms": 3,
        "raw_error": "PRIVATE_FIXTURE_SENTINEL",
    }
    log.write_text(json.dumps(row) + "\n" + '{"incomplete":', encoding="utf-8")
    done = operations.read_tasks("test")[0]["latest"]
    assert done == {"name": "extract_ods", "run_id": run_id, "status": "SUCCESS",
                    "code": "ETL_OK", "at": row["timestamp"], "rows": 18, "attempt": 1, "cost_ms": 3}
    os.link(log, directory / "hardlink")
    with pytest.raises(OSError):
        operations.read_tasks("test")


def test_manual_runner_has_no_unconfirmed_success_and_inherits_only_ods_connection(monkeypatch):
    def run(args, **options):
        assert args[1:] == ["-m", "etl.jobs.run_all", "--env", "test", "--through", "extract"]
        assert set(options["env"]) == {"ODS_DATABASE_URL", "PYTHONDONTWRITEBYTECODE"}
        assert options["timeout"] == 90
        return CompletedProcess(args, 0, stdout=b'{"status":"SUCCESS","code":"ETL_OK","tasks":{}}')

    monkeypatch.setattr(operations.subprocess, "run", run)
    assert operations.run_extract("test") == ("INDETERMINATE", "OPS_RUN_UNCONFIRMED")


def test_mysql_operations_registry_manual_run_and_audit(mysql_pipeline, auth_client, monkeypatch):
    dataset, batch, selected, _ = mysql_pipeline
    client, names, _, admin_id = auth_client
    root = sources.PROJECT_ROOT
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute((root / "sql/auth/operations_audit.sql").read_text())

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    admin = login(names["admin"])
    for role, department in (("analyst", None), ("department", "SIM_DEPT_01")):
        assert client.post(USERS, headers=admin, json={
            "username": names[role], "password": PASSWORD, "role": role, "department": department,
        }).status_code == 201
    analyst = login(names["analyst"])
    department = login(names["department"])
    check = "/api/ops/sources/his_outpatient/check"
    submit = "/api/ops/tasks/extract/run"

    try:
        monkeypatch.delenv("OPS_MANUAL_RUN_ENABLED", raising=False)
        assert client.post(submit, headers=admin).status_code == 403
        for path in ("/api/ops/sources", "/api/ops/tasks"):
            assert client.get(path, headers=analyst).status_code == 200
            assert client.get(path, headers=department).status_code == 403
        for actor in (analyst, department):
            assert client.post(submit, headers=actor).status_code == 403
            assert client.post(check, headers=actor).status_code == 403
        assert client.post("/api/ops/sources/unregistered/check", headers=admin).status_code == 404
        assert client.post(check, headers=admin).json()["data"]["outcome"] == "FAILED"

        monkeypatch.setenv("OPS_MANUAL_RUN_ENABLED", "1")
        failed = client.post(submit, headers=admin)
        assert failed.status_code == 200 and failed.json()["data"]["outcome"] == "FAILED"
        latest = client.get("/api/ops/tasks", headers=analyst).json()["data"]["items"][0]["latest"]
        assert latest["status"] == "FAILED" and latest["rows"] is None

        for name, record in selected.items():
            path = Path(record["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(sim_csv_rows(name))
        assert client.post(check, headers=admin).json()["data"]["outcome"] == "SUCCESS"
        data = client.get("/api/ops/sources", headers=analyst)
        assert data.headers["Cache-Control"] == "no-store"
        assert "file_path" not in data.text and "SIM_PRIVATE" not in data.text and str(root) not in data.text
        registry = {item["name"]: item for item in data.json()["data"]}
        assert set(registry) == set(selected) and all(item["registered"] for item in registry.values())
        assert registry["his_outpatient"]["last_check"]["outcome"] == "SUCCESS"

        with TestClient(create_app(), client=("203.0.113.1", 50000)) as remote:
            assert remote.post(submit, headers=admin).status_code == 403
            assert remote.get("/api/ops/tasks", headers=admin).json()["data"]["can_run"] is False
        with scheduler._lock("test", "run"):
            assert client.post(submit, headers=admin).status_code == 409
        assert client.post(submit, headers=admin).json()["data"] == {"outcome": "SUCCESS", "result_code": "ETL_OK"}
        summary = client.get("/api/ops/tasks", headers=analyst).json()["data"]
        assert summary["can_run"] is False
        run = next(result["latest"] for result in summary["items"] if result["name"] == "extract_ods")
        assert run["rows"] == 18 and run["status"] == "SUCCESS"
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for spec in sources.SOURCES.values():
                cursor.execute(f"SELECT COUNT(*) FROM {spec['table']} WHERE dataset_id=%s", (dataset,))
                assert cursor.fetchone() == (3,)
            cursor.execute("SELECT action,outcome,code FROM ops_action_audit WHERE actor_id=%s ORDER BY action_id",
                           (admin_id,))
            audits = list(cursor.fetchall())
            assert audits == [
                ("source_check", "FAILED", "OPS_FILE_CHECK_FAILED"),
                ("extract_run", "FAILED", failed.json()["data"]["result_code"]),
                ("source_check", "SUCCESS", "ETL_OK"),
                ("extract_run", "FAILED", "ETL_RUN_LOCKED"),
                ("extract_run", "SUCCESS", "ETL_OK"),
            ]
        print("OPERATIONS_API_OK sources=6 ods_rows=18 audit=5 logged_failure=1 locked=409 reader=403")
        if os.environ.get("MOPS_OPS_BROWSER") == "1":
            from admin_browser_fixture import run_admin_browser
            run_admin_browser(root, names, PASSWORD, scenario="operations")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("DELETE FROM ops_action_audit WHERE actor_id=%s", (admin_id,))
            db.commit()
