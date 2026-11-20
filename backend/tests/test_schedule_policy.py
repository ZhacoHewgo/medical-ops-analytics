import csv
from datetime import datetime, timedelta
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

from apscheduler.events import EVENT_JOB_EXECUTED
from apscheduler.triggers.interval import IntervalTrigger
from pymysql.cursors import Cursor
from pymysql.err import OperationalError
from fastapi.testclient import TestClient

from app.main import create_app
from etl.config import source_config as sources
from etl.jobs import extract_ods, run_all
from etl.scheduler import policy, scheduler
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows

PATH = "/api/ops/tasks/schedule"


def test_schedule_management_requires_authentication(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get(PATH).status_code == 401
        assert client.put(PATH, json={}).status_code == 401


def test_managed_mode_requires_explicit_schedule(capsys):
    assert run_all.main(["--managed-schedule", "--env", "test"]) == 1
    assert '"code": "ETL_ARGUMENTS_INVALID"' in capsys.readouterr().out


def wait_for(predicate):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        if value := predicate():
            return value
        sleep(0.02)
    raise AssertionError("Live foreground scheduler did not refresh")


def test_mysql_live_schedule_refresh_dispatch_and_audit(mysql_pipeline, auth_client, monkeypatch, tmp_path):
    dataset, batch, selected, _ = mysql_pipeline
    client, names, _, admin_id = auth_client
    root = sources.PROJECT_ROOT
    today = datetime.now(scheduler.SHANGHAI).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        script = (root / "sql/auth/schedule_policy.sql").read_text()
        for command in script.split(";"):
            if command.strip():
                cursor.execute(command)
        cursor.execute("SELECT version FROM ops_schedule_policy WHERE env='test'")
        assert cursor.fetchone() is None, "The isolated schedule policy is already owned"
        cursor.execute("SELECT policy_version FROM ops_schedule_dispatch WHERE env='test' AND local_day=%s", (today,))
        assert cursor.fetchone() is None, "The isolated dispatch claim is already owned"

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

    def settings(headers=admin):
        response = client.get(PATH, headers=headers)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        return response.json()["data"]

    def change(version, enabled, hour, minute, headers=admin):
        return client.put(PATH, headers=headers, json={
            "expected_version": version, "enabled": enabled, "hour": hour, "minute": minute,
        })

    monkeypatch.setattr(scheduler, "_TASKS", {})
    monkeypatch.setattr(scheduler, "_SCHEDULERS", {})
    monkeypatch.setattr(scheduler, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.delenv("OPS_SCHEDULE_WRITE_ENABLED", raising=False)
    controller = thread = None
    try:
        settings0 = settings()
        assert settings0 == {"enabled": True, "hour": 2, "minute": 0, "version": 0,
                             "timezone": "Asia/Shanghai", "can_edit": False}
        assert change(0, False, 2, 0).status_code == 403
        monkeypatch.setenv("OPS_SCHEDULE_WRITE_ENABLED", "1")
        assert settings()["can_edit"] is True and settings(analyst)["can_edit"] is False
        assert client.get(PATH, headers=department).status_code == 403
        assert change(0, False, 2, 0, analyst).status_code == 403
        assert change(0, False, 2, 0, department).status_code == 403
        assert change(0, True, 24, 1).status_code == 422
        with TestClient(create_app(), client=("203.0.113.2", 50000)) as remote:
            assert remote.put(PATH, headers=admin, json={
                "expected_version": 0, "enabled": False, "hour": 2, "minute": 0,
            }).status_code == 403

        for name, record in selected.items():
            path = Path(record["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = sim_csv_rows(name)
            for row in rows:
                row[extract_ods.SOURCE_DATES[name]] = yesterday
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(rows)

        original = Cursor.execute

        def fail_audit(cursor, query, args=None):
            if "INSERT INTO ops_schedule_audit" in query:
                raise OperationalError(1105, "fixture audit failure")
            return original(cursor, query, args)

        with monkeypatch.context() as failure:
            failure.setattr(Cursor, "execute", fail_audit)
            assert change(0, False, 2, 0).status_code == 503
        assert settings()["version"] == 0

        scheduler.register_task("extract_ods", run_all._extract_task(None, None, True))
        controller = scheduler.setup_schedule(env="test", managed=True)
        default = scheduler.setup_schedule(env="test")
        assert default.get_job("ods_daily").kwargs == {"env": "test"}
        controller.reschedule_job("ods_schedule_sync", trigger=IntervalTrigger(seconds=0.15))
        thread = Thread(target=controller.start)
        thread.start()
        wait_for(lambda: controller.running)
        assert controller.get_job("ods_daily").kwargs["version"] == 0
        assert change(0, False, 2, 0).json()["data"]["version"] == 1
        wait_for(lambda: controller.get_job("ods_daily") is None)
        assert not policy.claim_day("test", today, 0)

        later = datetime.now(scheduler.SHANGHAI) + timedelta(hours=3)
        assert change(1, True, later.hour, later.minute).json()["data"]["version"] == 2
        assert change(1, True, 0, 0).status_code == 409
        actual = wait_for(lambda: (job := controller.get_job("ods_daily")) and job.kwargs["version"] == 2 and job)
        fire = actual.trigger.get_next_fire_time(None, datetime.now(scheduler.SHANGHAI))
        assert fire.tzinfo == scheduler.SHANGHAI and (fire.hour, fire.minute) == (later.hour, later.minute)

        finished = Event()
        controller.add_listener(lambda event: finished.set() if event.job_id == "controlled_dispatch" else None,
                                EVENT_JOB_EXECUTED)
        controller.add_job(actual.func, "date", kwargs=actual.kwargs, id="controlled_dispatch",
                           run_date=datetime.now(scheduler.SHANGHAI) + timedelta(milliseconds=100))
        assert finished.wait(20), "Controlled scheduler did not dispatch"
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for name, spec in sources.SOURCES.items():
                date_col = extract_ods.SOURCE_DATES[name]
                cursor.execute(f"SELECT COUNT(*),MIN({date_col}),MAX({date_col}) FROM {spec['table']} WHERE dataset_id=%s",
                               (dataset,))
                assert cursor.fetchone() == (3, yesterday, yesterday)
            cursor.execute("SELECT policy_version FROM ops_schedule_dispatch WHERE env='test' AND local_day=%s", (today,))
            assert cursor.fetchone() == (2,)
            cursor.execute("SELECT version,enabled FROM ops_schedule_audit WHERE env='test' AND actor_id=%s ORDER BY version",
                           (admin_id,))
            assert list(cursor.fetchall()) == [(1, 0), (2, 1)]
        assert change(2, True, (later.hour + 1) % 24, later.minute).json()["data"]["version"] == 3
        assert not policy.claim_day("test", today, 3)
        assert len(list(scheduler.LOG_ROOT.glob("test/etl_*_run_all_*.log"))) == 1
        controller.shutdown(wait=True)
        thread.join(timeout=5)
        assert not thread.is_alive()

        print("SCHEDULE_RUNTIME_OK live_pause=1 live_resume=1 cron_change=1 ods_rows=18 duplicate=0 audit_rollback=1")
        if os.environ.get("MOPS_SCHEDULE_BROWSER") == "1":
            from admin_browser_fixture import run_admin_browser

            run_admin_browser(root, names, PASSWORD, scenario="schedule")
            assert settings()["hour"] == 4 and settings()["minute"] == 45 and settings()["enabled"]
    finally:
        if controller is not None and controller.running:
            controller.shutdown(wait=True)
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("DELETE FROM ops_schedule_dispatch WHERE env='test' AND local_day=%s AND policy_version=2",
                           (today,))
            cursor.execute(
                "DELETE p FROM ops_schedule_policy p JOIN ops_schedule_audit a ON p.env=a.env AND p.version=a.version "
                "WHERE p.env='test' AND a.actor_id=%s", (admin_id,),
            )
            cursor.execute("DELETE FROM ops_schedule_audit WHERE actor_id=%s", (admin_id,))
            db.commit()
