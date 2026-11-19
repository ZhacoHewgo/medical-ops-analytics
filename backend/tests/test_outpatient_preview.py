"""Isolated Node 19 rehearsal. No external DB or source patient files."""

from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from threading import Thread
from time import perf_counter, sleep

import pytest
from fastapi.testclient import TestClient

from app import outpatient_preview as preview
from app.main import create_app
from etl.jobs import build_dwd

BUILD = "b171a123-514b-4566-a955-e29c4794d21e"
BATCH = "2026-01-04"
URL = "/api/dashboard/outpatient-preview"


@pytest.fixture
def rehearsal(monkeypatch, aggregate_path, tmp_path):
    path = tmp_path / "preview.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE dwd_visit_detail (dataset_id, visit_type, visit_date, dept_code)")
        db.executemany("INSERT INTO dwd_visit_detail VALUES (?, ?, ?, ?)", [
            ("COURSE_TEST", "OUTPATIENT", "2026-01-01", "CARD"),
            ("COURSE_TEST", "OUTPATIENT", "2026-01-01", "CARD"),
            ("COURSE_TEST", "OUTPATIENT", "2026-01-02", "RESP"),
            ("COURSE_TEST", "OUTPATIENT", "2026-01-03", "CARD"),
            ("COURSE_TEST", "INPATIENT", "2026-01-01", "CARD"),
            ("OTHER", "OUTPATIENT", "2026-01-01", "OTHER_DEPT"),
        ])
    report = {"build_run_id": BUILD, "effective_start": "2026-01-01", "effective_end": "2026-01-03"}
    trace = []

    class Cursor:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, sql, params=()):
            trace.append(sql)
            if sql.startswith("SET TRANSACTION"):
                return
            if sql.startswith("START TRANSACTION"):
                self.raw.execute("BEGIN")
                return
            self.raw.execute(sql.replace("%s", "?"), params)

        def fetchall(self):
            return [(date.fromisoformat(day), dept, n) for day, dept, n in self.raw.fetchall()]

    class Connection:
        def __init__(self):
            self.db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)

        @contextmanager
        def cursor(self):
            raw = self.db.cursor()
            try:
                yield Cursor(raw)
            finally:
                raw.close()

        def rollback(self):
            trace.append("ROLLBACK")
            self.db.rollback()

        def close(self):
            self.db.close()

    def verify(cursor, *, dataset_id, batch_date, env, report):
        assert cursor.raw.connection.in_transaction
        assert (dataset_id, batch_date, env) == ("COURSE_TEST", BATCH, "test")
        assert report["build_run_id"] == BUILD
        return {"build_run_id": BUILD, "dataset_id": dataset_id, "batch_date": batch_date, "gate_status": "PASS"}

    monkeypatch.setenv("OUTPATIENT_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_DATASET_ID", "COURSE_TEST")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BATCH_DATE", BATCH)
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", BUILD)
    monkeypatch.setenv("OUTPATIENT_PREVIEW_ENV", "test")
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
    monkeypatch.setattr(preview, "database_options", lambda: {"database": "hospital_test"})
    monkeypatch.setattr(preview, "connect_database", Connection)
    monkeypatch.setattr(preview, "read_current_report", lambda *_: report.copy())
    monkeypatch.setattr(build_dwd, "verify_build_freshness_from_cursor", verify)
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        yield client, path, trace


@pytest.mark.parametrize("params,total", [
    ({}, 4),
    ({"dept_code": "CARD"}, 3),
    ({"start_date": "2026-01-01", "end_date": "2026-01-01"}, 2),
    ({"dept_code": "RESP", "start_date": "2026-01-01", "end_date": "2026-01-01"}, 0),
    ({"start_date": "2026-03-01", "end_date": "2026-03-02"}, 0),
])
def test_preview_against_independent_sql(rehearsal, params, total):
    client, path, trace = rehearsal
    response = client.get(URL, params=params)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["meta"] == {
        "source": "dwd_visit_detail", "batch_date": BATCH, "build_run_id": BUILD,
        "available_start_date": "2026-01-01", "available_end_date": "2026-01-03",
        "department_codes": ["CARD", "RESP"],
    }
    filters = data["filters"]
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT visit_date, dept_code FROM dwd_visit_detail "
            "WHERE dataset_id = 'COURSE_TEST' AND visit_type = 'OUTPATIENT' "
            "AND visit_date BETWEEN ? AND ? ORDER BY visit_date, dept_code",
            (filters["start_date"], filters["end_date"]),
        ).fetchall()
    rows = [(day, dept) for day, dept in rows if not filters["dept_code"] or dept == filters["dept_code"]]
    assert len(rows) == total == data["outpatient_visits"]
    assert data["daily_trend"] == [
        {"date": day, "visits": sum(current == day for current, _ in rows)}
        for day in sorted({day for day, _ in rows})
    ]
    assert {r["dept_code"]: r["visits"] for r in data["department_ranking"]} == {
        dept: sum(current == dept for _, current in rows) for dept in {dept for _, dept in rows}
    }
    assert trace[:2] == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
    assert trace[-1] == "ROLLBACK"


@pytest.mark.parametrize("params", [
    {"start_date": "2026-02-29"}, {"start_date": "2026-1-01"},
    {"start_date": "2026-01-03", "end_date": "2026-01-01"},
    {"dept_code": "UNKNOWN"}, {"dept_code": " "}, {"dept_code": "X" * 65},
])
def test_invalid_filters(rehearsal, params):
    response = rehearsal[0].get(URL, params=params)
    assert response.status_code == response.json()["code"] == 422
    assert response.json()["data"] is None


def test_missing_connection_and_disabled_preview_preserve_csv(rehearsal, monkeypatch):
    from etl.config.source_config import OdsError

    def failed_connection():
        raise OdsError("SENSITIVE_TEST_ONLY")

    client = rehearsal[0]
    monkeypatch.setattr(preview, "connect_database", failed_connection)
    unavailable = client.get(URL)
    assert unavailable.status_code == 503
    assert "SENSITIVE_TEST_ONLY" not in unavailable.text
    monkeypatch.delenv("OUTPATIENT_PREVIEW_ENABLED")
    assert client.get(URL).status_code == 503
    assert client.get("/api/dashboard/overview").json()["data"]["outpatient_visits"] == 4
    assert client.get("/health").json() == {"status": "ok"}


def test_stale_build_rejected_before_fetching_counts(rehearsal, monkeypatch):
    client, _, trace = rehearsal
    monkeypatch.setattr(build_dwd, "verify_build_freshness_from_cursor", lambda *a, **kw: {
        "build_run_id": "expired", "dataset_id": "COURSE_TEST", "batch_date": BATCH, "gate_status": "PASS",
    })
    assert client.get(URL).status_code == 503
    assert not any(sql.startswith("SELECT") for sql in trace)


def test_report_replaced_during_read_returns_503(rehearsal, monkeypatch):
    client, _, trace = rehearsal
    reports = iter([{"build_run_id": BUILD, "effective_start": "2026-01-01", "effective_end": "2026-01-03"},
                    {"build_run_id": "changed"}])
    monkeypatch.setattr(preview, "read_current_report", lambda *_: next(reports))
    assert client.get(URL).status_code == 503
    assert trace[-1] == "ROLLBACK"


def test_preview_rejects_non_loopback(rehearsal):
    with TestClient(create_app(), client=("192.0.2.1", 50000)) as client:
        response = client.get(URL)
        assert response.status_code == 403
        assert response.json()["data"] is None
    assert not rehearsal[2]


@pytest.mark.skipif(os.getenv("MOPS_BROWSER_TEST") != "1", reason="requires Chromium and built frontend")
def test_preview_browser_and_local_latency(rehearsal, monkeypatch):
    """Serve the real app and compiled UI to Chromium, with a SQLite rehearsal source."""
    import httpx
    import uvicorn
    from starlette.staticfiles import StaticFiles

    root = Path(__file__).resolve().parents[2]
    result_dir = root / "artifacts" / "outpatient-mvp"
    app = create_app()
    connect = preview.connect_database

    def unavailable():
        from etl.config.source_config import OdsError

        raise OdsError("REHEARSAL_SOURCE_UNAVAILABLE")

    @app.post("/__rehearsal/source/{state}")
    def set_rehearsal_state(state: str):
        monkeypatch.setattr(preview, "connect_database", connect if state == "available" else unavailable)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=root / "frontend" / "dist", html=True))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", proxy_headers=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                sleep(0.025)
            assert server.started
            base = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["node", str(root / "frontend/tests/preview-browser.mjs"), base, str(result_dir)],
                check=True, capture_output=True, text=True, encoding="utf-8", timeout=90,
            )
            checks = json.loads(result.stdout)
            with httpx.Client(base_url=base, timeout=10, trust_env=False) as http:
                for _ in range(5):
                    assert http.get(URL).status_code == 200

                def request(index):
                    filters = ({}, {"dept_code": "CARD"}, {"start_date": "2026-03-01", "end_date": "2026-03-01"})[index % 3]
                    started = perf_counter()
                    response = http.get(URL, params=filters)
                    assert response.status_code == 200
                    assert response.json()["data"]["outpatient_visits"] == (4, 3, 0)[index % 3]
                    return (perf_counter() - started) * 1000

                with ThreadPoolExecutor(max_workers=5) as pool:
                    timings = sorted(pool.map(request, range(30)))
            evidence = {"source": "SQLite rehearsal fixture", "browser": checks, "concurrency": 5,
                        "requests": 30, "errors": 0, "p95_ms": round(timings[28], 2)}
            (result_dir / "rehearsal.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            print(json.dumps(evidence))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
