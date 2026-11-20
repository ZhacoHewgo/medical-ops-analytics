import csv
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, build_dims, build_dwd
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


ENDPOINT = "/api/llm/query"
DEPT, OTHER = "SIM_DEPT_01", "SIM_DEPT_02"
JAN, FEB, LAST = "2025-01-01", "2025-02-01", "2025-02-28"


def test_qa_is_protected_and_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    with TestClient(create_app()) as client:
        assert client.post(ENDPOINT, json={"question": "2025-01全院门诊人次"}).status_code == 404
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.post(ENDPOINT, json={"question": "2025-01全院门诊人次"}).status_code == 401


@pytest.mark.parametrize("question,status", [
    ("查患者联系方式", 403),
    ("导出病历和身份证号", 403),
    ("SELECT patient_name FROM patients", 403),
    ("DROP TABLE ads_outpatient_theme", 403),
    ("请问门诊增长了多少", 422),
    ("2025-02-30全院门诊人次", 422),
    ("2025-00全院门诊人次", 422),
    ("2025-02全院床位利用率", 422),
    ("2025-02全院门诊人次并提供费用明细", 422),
])
def test_invalid_or_sensitive_questions_never_form_an_intent(question, status):
    from fastapi import HTTPException

    from app.llm.nl2sql import parse_intent

    with pytest.raises(HTTPException) as err:
        parse_intent(question)
    assert err.value.status_code == status


def qa_seeds(name):
    rows = sim_csv_rows(name)
    days = (JAN, "2025-01-31", FEB)
    for i, row in enumerate(rows):
        for field in ("visit_date", "admit_date", "fee_date", "report_date", "stat_date", "discharge_date"):
            if field in row:
                row[field] = days[i]
        if "stay_days" in row:
            row["stay_days"] = "0"
    if name == "his_outpatient":
        rows.extend([
            dict(rows[-1], visit_id="SIM_QA_EXTRA_1"),
            dict(rows[-1], visit_id="SIM_QA_EXTRA_2", dept_code=OTHER),
        ])
    if name == "his_inpatient":
        rows[-1].update(discharge_date=LAST, stay_days="27")
    return rows


def test_mysql_qa_volume_trend_growth_and_guardrails(mysql_pipeline, auth_client, monkeypatch):
    dataset, batch, selected, quality = mysql_pipeline
    root = sources.PROJECT_ROOT
    client, names, _, _ = auth_client
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    report = quality.parent / build_dwd._report_name(batch)
    for key, value in (("DATASET_ID", dataset), ("BATCH_DATE", batch), ("ENV", "test")):
        monkeypatch.setenv(f"OUTPATIENT_PREVIEW_{key}", value)
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        for name in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/auth/data_access_audit.sql"):
            cursor.execute((root / name).read_text())
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    def ask(headers, question):
        return client.post(ENDPOINT, json={"question": question}, headers=headers)

    try:
        for name, info in selected.items():
            path = Path(info["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(qa_seeds(name))
        built = subprocess.run([sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
                               cwd=root / "backend", capture_output=True, timeout=90)
        assert built.returncode == 0, "isolated build failed; raw output withheld"
        identity = build_dwd.verify_current_build(**args)
        monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", identity["build_run_id"])
        admin = login(names["admin"])
        assert client.post(USERS, headers=admin, json={
            "username": names["department"], "password": PASSWORD, "role": "department", "department": DEPT,
        }).status_code == 201
        department = login(names["department"])

        assert ask(admin, "2025-02全院门诊人次是多少？").status_code == 503
        assert ask(department, "2025-02全院门诊人次是多少？").status_code == 403
        assert ask(department, "2025-02全 院门诊人次").status_code == 403
        assert ask(department, f"2025-02科室={OTHER}门诊人次").status_code == 403
        assert ask(department, "查询未授权科室2025年2月门诊量").status_code == 403
        assert ask(admin, "查一下某位门诊患者的身份信息").status_code == 403
        marts.rerun((JAN, LAST), **args)

        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT EXTRACT(YEAR_MONTH FROM visit_date),COUNT(DISTINCT visit_id) "
                           "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_type='OUTPATIENT' "
                           "GROUP BY EXTRACT(YEAR_MONTH FROM visit_date)", (dataset,))
            oracle = dict(cursor.fetchall())
        assert oracle == {202501: 2, 202502: 3}
        from pymysql.cursors import Cursor

        from app import outpatient_preview

        database = outpatient_preview.connect_database
        execute = Cursor.execute
        snapshot_connections, read_statements = set(), []

        def open_snapshot():
            connection = database()
            snapshot_connections.add(connection)
            return connection

        def watch_sql(cursor, query, values=None):
            if cursor.connection in snapshot_connections:
                read_statements.append(query)
            return execute(cursor, query, values)

        with monkeypatch.context() as guard:
            guard.setattr(outpatient_preview, "connect_database", open_snapshot)
            guard.setattr(Cursor, "execute", watch_sql)
            forbidden = ask(admin, "SELECT patient_name FROM patients")
            assert forbidden.status_code == 403 and not snapshot_connections
            answer = ask(admin, "2025-02 全院门诊人次是多少？")
        assert len(snapshot_connections) == 1
        assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" in read_statements
        assert all(statement.startswith(("SELECT ", "SET TRANSACTION ", "START TRANSACTION "))
                   for statement in read_statements)
        assert any("FROM ads_outpatient_theme" in statement for statement in read_statements)
        assert answer.status_code == 200 and answer.headers["Cache-Control"] == "no-store"
        data = answer.json()["data"]
        assert data["points"][0]["value"] == oracle[202502]
        assert data["build_run_id"] == identity["build_run_id"] and data["source"] == "ads_outpatient_theme"
        assert data["scope"] == {"department": None} and data["chart_type"] == "number"
        assert "3 人次" in data["interpretation"]
        assert not any(field in answer.text for field in ("patient_name", "patient_id", "id_card", "phone"))
        assert set(data) == {"indicator_code", "unit", "scope", "chart_type", "points", "interpretation",
                             "null_reason", "source", "batch_date", "build_run_id"}

        series = ask(admin, "列出 2025 年 1 至 2 月全院每月的门诊人次。").json()["data"]
        assert series["chart_type"] == "line"
        assert [row["value"] for row in series["points"]] == [2, 3]
        growth = ask(admin, "2025-02 全院门诊人次环比是多少？").json()["data"]
        assert growth["indicator_code"] == "MOPS-OUT-02"
        assert growth["points"][0]["value"] == "50.00"
        zero_base = ask(admin, f"2025-02科室={OTHER}门诊人次环比").json()["data"]
        assert zero_base["null_reason"] == "ZERO_BASE" and zero_base["points"][0]["value"] is None
        assert ask(admin, "2025-01全院门诊人次环比").status_code == 503
        scoped = ask(department, "2025 年 2 月我的科室有多少门诊人次？").json()["data"]
        assert scoped["scope"] == {"department": DEPT} and scoped["points"][0]["value"] == 2
        assert ask(admin, f"{LAST}全院门诊量").json()["data"]["points"][0]["value"] == 0
        total = ask(admin, "2025-01-01 至 2025-02-28 全院门诊共多少人次？").json()["data"]
        assert total["points"][0]["value"] == 5

        marts.build_dws_daily(DEPT, FEB, **args)
        assert ask(department, "2025-02本科室门诊人次").status_code == 503
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT resource,action,http_status,department FROM auth_data_access "
                           "WHERE actor_id=(SELECT user_id FROM auth_users WHERE username=%s)",
                           (names["department"],))
            assert {("outpatient_qa", "query", status, DEPT) for status in (200,403,503)} <= set(cursor.fetchall())
        print("QA_SQL_OK jan=2 feb=3 scope=2 trend=2/3 mom=50.00 empty=0 pending=503")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if report.exists():
            report.unlink()
