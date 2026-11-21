"""Opt-in same-instance A/B stage profiling for the Node 19 dashboard path.

Not part of the acceptance gate and skipped unless asked for::

    MOPS_STAGE_AB=1 ODS_TEST_DATABASE_URL=... \\
        .venv/bin/python -m pytest backend/tests/test_node19_stage_ab.py -q -o addopts=""

It reuses the existing isolated synthetic fixture (`mysql_pipeline` +
`auth_client`), builds it once, then alternates short 50-worker windows between
the r24 query pattern (``baseline``) and r25 (``revision``) on the same server,
same MySQL instance, same credentials.  It prints aggregate timings, statement
counts and audit reconciliation only; it does not change any production file and
does not touch the 50 x 300 s gate.
"""

import os

import pytest

if os.environ.get("MOPS_STAGE_AB") != "1":
    pytest.skip("opt-in stage A/B profiling; set MOPS_STAGE_AB=1", allow_module_level=True)

import csv
import json
from pathlib import Path
import subprocess
import sys

from app.main import create_app
from sql_fixture import execute_schema

from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, build_dims, build_dwd
# Fixtures are registered for this module by explicit import only -- there is no
# conftest-level `auth_client`/`mysql_pipeline`, so both names must stay in the
# import list or setup fails with "fixture not found" before any window runs.
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows
from tests.perf.stage_ab import run_stage_ab


DAY = "2025-01-01"
LAST = "2025-01-04"
DEPT = "SIM_DEPT_01"
OTHER = "SIM_DEPT_02"
SUMMARY = "/api/v1/outpatient/summary"


def _settings():
    seconds = int(os.environ.get("MOPS_STAGE_AB_SECONDS", "20"))
    workers = int(os.environ.get("MOPS_STAGE_AB_WORKERS", "50"))
    rounds = int(os.environ.get("MOPS_STAGE_AB_ROUNDS", "1"))
    cases = tuple(name.strip() for name in os.environ.get(
        "MOPS_STAGE_AB_CASES", "summary,grouped,export").split(",") if name.strip())
    tokens = tuple(int(value) for value in os.environ.get("MOPS_STAGE_AB_TOKENS", "").split(",")
                   if value.strip()) or None
    assert seconds > 0 and 1 <= workers <= 200 and rounds > 0 and cases
    assert tokens is None or (len(tokens) >= 2 and all(1 <= value <= 200 for value in tokens))
    return dict(seconds=seconds, workers=workers, rounds=rounds, cases=cases, tokens=tokens)


def test_stage_ab_same_instance(mysql_pipeline, auth_client, monkeypatch):
    dataset, batch, selected, report_path = mysql_pipeline
    root = sources.PROJECT_ROOT
    client, names, _, _ = auth_client
    report = report_path.parent / build_dwd._report_name(batch)
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    monkeypatch.delenv("OUTPATIENT_PREVIEW_ENABLED", raising=False)
    for key, value in (("DATASET_ID", dataset), ("BATCH_DATE", batch), ("ENV", "test")):
        monkeypatch.setenv(f"OUTPATIENT_PREVIEW_{key}", value)
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        for file in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/auth/data_access_audit.sql"):
            execute_schema(cursor, root / file)
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    try:
        for name, record in selected.items():
            path = Path(record["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = sim_csv_rows(name)
            if name == "his_outpatient":
                rows.append(dict(rows[0], visit_id="SIM_OUT_DEPT_02", dept_code=OTHER))
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(rows)
        build = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=180,
        )
        assert build.returncode == 0, "Isolated build failure; raw output withheld"
        original = build_dwd.verify_current_build(**args)["build_run_id"]
        monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", original)
        admin = login(names["admin"])
        for role, dept in (("analyst", None), ("department", DEPT)):
            created = client.post(USERS, headers=admin, json={
                "username": names[role], "password": PASSWORD, "role": role, "department": dept,
            })
            assert created.status_code == 201
        analyst, department = login(names["analyst"]), login(names["department"])
        marts.rerun((DAY, LAST), **args)
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT visit_date,dept_code,COUNT(DISTINCT visit_id) FROM dwd_visit_detail "
                "WHERE dataset_id=%s AND visit_type='OUTPATIENT' GROUP BY visit_date,dept_code", (dataset,),
            )
            truth = {(day.isoformat(), dept): count for day, dept, count in cursor.fetchall()}
        assert truth, "isolated fixture produced no outpatient rows"
        response = client.get(SUMMARY, headers=analyst)
        assert response.status_code == 200 and response.json()["code"] == 0

        windows, summary = run_stage_ab(
            create_app(), analyst, department, original, names, **_settings(),
        )
        assert windows and summary
        print("STAGE_AB_CASES " + json.dumps({
            label: {"statements_per_request": item["statements_per_request"],
                    "client_p95_ms": item["client_p95_ms_median"]}
            for label, item in summary.items()}, sort_keys=True))
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if report.exists():
            report.unlink()
