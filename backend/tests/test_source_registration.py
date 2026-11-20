import csv
import json
import os
import shutil

from pymysql.err import OperationalError
from fastapi.testclient import TestClient

from app import operations
from app.main import create_app
from etl.config import source_config as sources
from etl.scheduler import scheduler
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


PATH = "/api/ops/sources/registration"


def test_registry_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get(PATH).status_code == 401
        assert client.put(PATH, json={}).status_code == 401


def test_mysql_registration_is_atomic_versioned_and_audited(mysql_pipeline, auth_client, monkeypatch):
    dataset, batch, selected, _ = mysql_pipeline
    client, names, _, admin_id = auth_client
    root = sources.PROJECT_ROOT
    new_dataset = dataset + "_change"
    browser_dataset = dataset + "_browser"
    directories = [sources.GENERATED_ROOT / name for name in (new_dataset, browser_dataset)]
    assert not any(path.exists() for path in directories)
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute((root / "sql/auth/source_registry_audit.sql").read_text())
        cursor.execute((root / "sql/auth/operations_audit.sql").read_text())

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    admin = login(names["admin"])
    for role, dept in (("analyst", None), ("department", "SIM_DEPT_01")):
        assert client.post(USERS, headers=admin, json={
            "username": names[role], "password": PASSWORD, "role": role, "department": dept,
        }).status_code == 201
    analyst, department = login(names["analyst"]), login(names["department"])

    def write_fixture(directory, name):
        path = directory / sources.SOURCES[name]["path"]
        assert path.is_relative_to(sources.GENERATED_ROOT) and path.resolve() == path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
            writer.writeheader()
            writer.writerows(sim_csv_rows(name))

    def version():
        response = client.get(PATH, headers=admin)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        return response.json()["data"]

    def update(**params):
        return client.put(PATH, headers=admin, json={**change, **params})

    def verify_registration(expected):
        response = client.get("/api/ops/sources", headers=analyst)
        assert response.status_code == 200 and "file_path" not in response.text and str(root) not in response.text
        result = response.json()["data"]
        assert len(result) == 6 and all(row["dataset_id"] == expected and row["batch_date"] == batch for row in result)

    monkeypatch.delenv("OPS_REGISTRY_WRITE_ENABLED", raising=False)
    before = version()
    change = {"dataset_id": new_dataset, "batch_date": batch, "expected_version": before["version"]}
    assert before["enabled"] is False and update().status_code == 403
    assert before["batches"] == [{"dataset_id": dataset, "batch_date": batch, "sources": 6}]
    for actor in (analyst, department):
        assert client.get(PATH, headers=actor).status_code == 403
        assert client.put(PATH, headers=actor, json=change).status_code == 403

    try:
        monkeypatch.setenv("OPS_REGISTRY_WRITE_ENABLED", "1")
        assert version()["enabled"] is True
        with TestClient(create_app(), client=("203.0.113.1", 50000)) as remote:
            assert remote.get(PATH, headers=admin).json()["data"]["enabled"] is False
            assert remote.put(PATH, headers=admin, json=change).status_code == 403
        assert update(dataset_id="../outside").status_code == 422
        assert update(file_path="/private/fixture").status_code == 422
        assert update(batch_date="2025-02-30").status_code == 422
        for name in selected:
            if name != "finance_excel":
                write_fixture(directories[0], name)
        assert update().status_code == 422
        assert version()["version"] == before["version"]
        verify_registration(dataset)
        write_fixture(directories[0], "finance_excel")

        with scheduler._lock("test", "run"):
            assert update().status_code == 409

        def audit_failed(*_args):
            raise OperationalError(1105, "fixture audit failure")

        with monkeypatch.context() as failure:
            failure.setattr(operations, "_record_registry_change", audit_failed)
            assert update().status_code == 503
        assert version()["version"] == before["version"]
        verify_registration(dataset)

        updated = update()
        assert updated.status_code == 200, f"Registry update returned {updated.status_code}"
        received = updated.json()["data"]
        assert received["changed"] is True and received["version"] != before["version"]
        assert version()["version"] == received["version"]
        verify_registration(new_dataset)
        assert update().status_code == 409
        unchanged = update(expected_version=received["version"])
        assert unchanged.json()["data"]["changed"] is False

        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT previous_version,new_version,previous_sources,dataset_id,batch_date "
                           "FROM source_registry_audit WHERE actor_id=%s", (admin_id,))
            rows = cursor.fetchall()
            assert len(rows) == 1
            previous, current, raw, recorded_dataset, recorded_batch = rows[0]
            assert (previous, current) == (before["version"], received["version"])
            assert (recorded_dataset, recorded_batch.isoformat()) == (new_dataset, batch)
            assert json.loads(raw) == [
                {"name": name, "dataset_id": dataset, "batch_date": batch} for name in sorted(selected)
            ]
        monkeypatch.setenv("OPS_MANUAL_RUN_ENABLED", "1")
        run = client.post("/api/ops/tasks/extract/run", headers=admin)
        assert run.status_code == 200 and run.json()["data"]["outcome"] == "SUCCESS"
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for spec in sources.SOURCES.values():
                cursor.execute(f"SELECT COUNT(*),MIN(etl_batch_date),MAX(etl_batch_date) FROM {spec['table']} "
                               "WHERE dataset_id=%s", (new_dataset,))
                count, first, last = cursor.fetchone()
                assert count == 3 and first.isoformat() == last.isoformat() == batch
        print("REGISTRY_API_OK sources=6 ods_rows=18 missing=422 role=403 stale=409 audit_rollback=1")
        if os.environ.get("MOPS_REGISTRY_BROWSER") == "1":
            from admin_browser_fixture import run_admin_browser

            for name in selected:
                write_fixture(directories[1], name)
            run_admin_browser(root, names, PASSWORD, scenario="registration",
                              scenario_env={"MOPS_REGISTRY_DATASET": browser_dataset, "MOPS_REGISTRY_BATCH": batch})
            verify_registration(browser_dataset)
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for name, info in selected.items():
                cursor.execute(
                    "UPDATE source_config SET kind=%s,file_path=%s,dataset_id=%s,etl_batch_date=%s "
                    "WHERE name=%s AND dataset_id IN (%s,%s)",
                    (*(info[key] for key in sources.CONFIG_FIELDS), name, new_dataset, browser_dataset),
                )
            for spec in sources.SOURCES.values():
                cursor.execute(f"DELETE FROM {spec['table']} WHERE dataset_id=%s", (new_dataset,))
            cursor.execute("DELETE FROM ops_action_audit WHERE actor_id=%s", (admin_id,))
            cursor.execute("DELETE FROM source_registry_audit WHERE actor_id=%s", (admin_id,))
            db.commit()
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)
