import csv
from io import BytesIO
import os
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from app.main import create_app
from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, build_dims, build_dwd
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


SUMMARY = "/api/v1/outpatient/summary"
EXPORT = "/api/v1/reports/export"
REPORT = "/api/v1/reports/outpatient"
DAY = "2025-01-01"
LAST = "2025-01-04"
DEPT = "SIM_DEPT_01"
OTHER = "SIM_DEPT_02"


def test_protected_ads_requires_auth_and_documents_routes(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get(SUMMARY).status_code == 401
        assert client.post(EXPORT, json={}).status_code == 401
        assert client.get(REPORT).status_code == 401
        api = client.get("/openapi.json")
        assert api.status_code == 200
        endpoint = api.json()["paths"][SUMMARY]["get"]
        assert endpoint["security"] and {"200", "401", "403", "422", "503"} <= endpoint["responses"].keys()
        assert EXPORT in api.json()["paths"]
        assert api.json()["paths"][REPORT]["get"]["security"]


def test_scoped_service_cannot_widen_a_department(monkeypatch):
    from app import outpatient_preview

    def loader(start, end, dept_code, **options):
        assert dept_code == options["allowed_department"] == DEPT
        return {"ok": True}

    monkeypatch.setattr(outpatient_preview, "_load_outpatient", loader)
    assert outpatient_preview.load_ads(allowed_department=DEPT) == {"ok": True}
    with pytest.raises(sources.OdsError, match="OUTPATIENT_ADS_SCOPE_INVALID"):
        outpatient_preview.load_ads(allowed_department=DEPT, dept_code=OTHER)


def test_excel_exports_text_cells_and_rejects_rows_over_limit():
    from fastapi import HTTPException

    from app.api.routers.outpatient_router import _excel

    row = {"date": DAY, "dept_code": "=1+1", "visits": 1}
    document = {"counts": [row], "meta": {"source": "ads_outpatient_theme",
                "batch_date": DAY, "build_run_id": "isolated-demo"}}
    summary = {"data": {"filters": {"start_date": DAY, "end_date": DAY, "dept_code": "=1+1"}}}
    result = _excel(document, summary)
    workbook = load_workbook(BytesIO(result.body), read_only=True)
    try:
        cell = list(workbook["门诊人次"].iter_rows())[1][1]
        assert cell.value == "=1+1" and cell.data_type == "s"
        assert workbook["数据来源"]["B6"].value == "=1+1"
        assert workbook["数据来源"]["B6"].data_type == "s"
    finally:
        workbook.close()
    with pytest.raises(HTTPException) as error:
        _excel({**document, "counts": [dict(row, dept_code=str(index)) for index in range(10001)]}, summary)
    assert error.value.status_code == 422


def test_ads_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    with TestClient(create_app()) as client:
        assert client.get(SUMMARY).status_code == 404
        assert client.post(EXPORT, json={}).status_code == 404
        assert client.get(REPORT).status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_mysql_ads_summary_export_and_scope(mysql_pipeline, auth_client, monkeypatch):
    dataset, batch, selected, report_path = mysql_pipeline
    root = sources.PROJECT_ROOT
    client, names, _, admin_id = auth_client
    report = report_path.parent / build_dwd._report_name(batch)
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    monkeypatch.delenv("OUTPATIENT_PREVIEW_ENABLED", raising=False)
    for key, value in (("DATASET_ID", dataset), ("BATCH_DATE", batch), ("ENV", "test")):
        monkeypatch.setenv(f"OUTPATIENT_PREVIEW_{key}", value)
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        for file in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/auth/data_access_audit.sql"):
            cursor.execute((root / file).read_text())
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    def summary(headers, **params):
        return client.get(SUMMARY, params=params, headers=headers)

    def export(headers, **params):
        return client.post(EXPORT, json=params, headers=headers)

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
        result = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=90,
        )
        assert result.returncode == 0, "Isolated build failure; raw output withheld"
        original = build_dwd.verify_current_build(**args)["build_run_id"]
        monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", original)
        admin = login(names["admin"])
        for role, dept in (("analyst", None), ("department", DEPT)):
            assert client.post(USERS, headers=admin, json={
                "username": names[role], "password": PASSWORD, "role": role, "department": dept,
            }).status_code == 201
        analyst, department = login(names["analyst"]), login(names["department"])
        assert summary(department, dept_code=OTHER).status_code == 403
        assert summary(department, dept_code=f"{DEPT},{OTHER}").status_code == 403
        assert export(department, dept_code=OTHER).status_code == 403
        assert summary(analyst, end_date="2025-01-05").status_code == 422
        assert summary(analyst, granularity="month").status_code == 422
        assert summary(analyst, start_date="2025-02-30").status_code == 422
        assert summary(analyst, drill="patient_id").status_code == 422
        assert summary(analyst, dept_code="UNKNOWN").status_code == 422
        assert summary(analyst).status_code == 503
        assert export(analyst).status_code == 503

        marts.rerun((DAY, LAST), **args)
        all_records = summary(analyst)
        assert all_records.status_code == 200 and all_records.headers["Cache-Control"] == "no-store"
        payload = all_records.json()["data"]
        assert payload["meta"]["source"] == "ads_outpatient_theme"
        assert payload["meta"]["build_run_id"] == original
        assert payload["outpatient_visits"] == 4
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT visit_date,dept_code,COUNT(DISTINCT visit_id) FROM dwd_visit_detail "
                "WHERE dataset_id=%s AND visit_type='OUTPATIENT' GROUP BY visit_date,dept_code", (dataset,),
            )
            truth = {(d.isoformat(), dept): count for d, dept, count in cursor.fetchall()}
        by_date = {row["date"]: row["visits"] for row in payload["daily_trend"]}
        assert by_date == {day: sum(n for (date, _), n in truth.items() if day == date)
                           for day, _ in truth}
        assert {r["dept_code"]: r["visits"] for r in payload["department_ranking"]} == {DEPT: 3, OTHER: 1}
        scoped = summary(department).json()["data"]
        assert scoped["outpatient_visits"] == 3 and scoped["filters"]["dept_code"] == DEPT
        assert scoped["meta"]["department_codes"] == [DEPT]
        assert {item["dept_code"] for item in scoped["department_ranking"]} == {DEPT}
        assert summary(analyst, start_date=LAST, end_date=LAST).json()["data"]["outpatient_visits"] == 0

        def grouped(headers, **params):
            return client.get(REPORT, params=params, headers=headers)

        assert grouped(department, dept_code=OTHER).status_code == 403
        assert grouped(analyst, group_by="patient").status_code == 422
        empty_report = grouped(analyst, start_date=LAST, end_date=LAST).json()["data"]
        assert empty_report["rows"] == [] and empty_report["outpatient_visits"] == 0

        for group_by, has_department, resolution, headings in (
            ("day", False, 10, ("日期", "门诊人次")),
            ("department", True, 0, ("科室", "门诊人次")),
            ("day_department", True, 10, ("日期", "科室", "门诊人次")),
            ("month_department", True, 7, ("月份", "科室", "门诊人次")),
        ):
            expected = {}
            for (day, dept), count in truth.items():
                key = (day[:resolution] if resolution else None, dept if has_department else None)
                expected[key] = expected.get(key, 0) + count
            response = grouped(analyst, group_by=group_by)
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            report_data = response.json()["data"]
            assert report_data["group_by"] == group_by and report_data["outpatient_visits"] == 4
            assert {(row["date"], row["dept_code"]): row["visits"] for row in report_data["rows"]} == expected
            download = export(analyst, group_by=group_by, expected_build_run_id=original)
            assert download.status_code == 200
            book = load_workbook(BytesIO(download.content), read_only=True)
            try:
                values = list(book["门诊人次"].values)
                assert values[0] == headings
                assert values[1:] == [
                    tuple(item for item in (row["date"], row["dept_code"], row["visits"]) if item is not None)
                    for row in report_data["rows"]
                ]
            finally:
                book.close()
        assert grouped(department, group_by="month_department").json()["data"]["rows"] == [
            {"date": "2025-01", "dept_code": DEPT, "visits": 3}
        ]
        assert export(department, expected_build_run_id="ffffffff-ffff-4fff-bfff-ffffffffffff").status_code == 409
        file = export(department)
        assert file.status_code == 200 and file.headers["Cache-Control"] == "no-store"
        assert file.headers["Content-Type"].endswith("spreadsheetml.sheet")
        workbook = load_workbook(BytesIO(file.content), read_only=True)
        try:
            rows = list(workbook["门诊人次"].values)
            assert rows[0] == ("日期", "科室", "门诊人次")
            assert {(day, dept): count for day, dept, count in rows[1:]} == {
                key: value for key, value in truth.items() if key[1] == DEPT
            }
        finally:
            workbook.close()

        if os.environ.get("MOPS_REPORT_BROWSER") == "1":
            from report_browser_fixture import run_report_browser

            run_report_browser(root, names, PASSWORD)

        marts.build_dws_daily(DEPT, DAY, **args)
        assert summary(department, start_date=DAY, end_date=DAY).status_code == 503
        assert export(department, start_date=DAY, end_date=DAY).status_code == 503
        assert summary(analyst, start_date=DAY, end_date=DAY, dept_code=OTHER).json()["data"]["outpatient_visits"] == 1

        current = build_dwd.run_build_all(**args)["build_run_id"]
        assert current != original
        assert summary(analyst).status_code == 503
        monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", current)
        marts.rerun((DAY, DAY), **args)
        assert summary(analyst).status_code == 503
        assert export(analyst).status_code == 503
        assert summary(department, start_date=DAY, end_date=DAY).json()["data"]["outpatient_visits"] == 1
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT action,http_status,department FROM auth_data_access WHERE actor_id="
                           "(SELECT user_id FROM auth_users WHERE username=%s)", (names["department"],))
            audit = set(cursor.fetchall())
            assert {("query", 200, DEPT), ("query", 403, DEPT), ("query", 503, DEPT),
                    ("export", 200, DEPT), ("export", 403, DEPT), ("export", 503, DEPT)} <= audit
        from app.api.routers import outpatient_router
        actual_load = outpatient_router.load_ads

        def revoke_during_read(*a, **kw):
            document = actual_load(*a, **kw)
            with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                cursor.execute("UPDATE auth_users SET enabled=FALSE WHERE username=%s", (names["department"],))
                db.commit()
            return document

        with monkeypatch.context() as changes:
            changes.setattr(outpatient_router, "load_ads", revoke_during_read)
            revoked = summary(department, start_date=DAY, end_date=DAY)
            assert revoked.status_code == 401 and revoked.json()["data"] is None
        print("REPORT_HTTP_OK groups=4 scoped=3 empty=0 stale_export=409")
        print("ADS_HTTP_OK full=4 department=3 empty=0 unauthorized=401 forbidden=403 pending=503 partial=503 xlsx=3")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if report.exists():
            report.unlink()


@pytest.mark.skipif(os.environ.get("MOPS_REPORT_BROWSER") != "1", reason="requires Chromium and built frontend")
def test_mysql_browser_report_comparison(mysql_pipeline, auth_client, monkeypatch):
    from report_browser_fixture import run_report_browser

    dataset, batch, selected, report_path = mysql_pipeline
    root = sources.PROJECT_ROOT
    client, names, _, _ = auth_client
    report = report_path.parent / build_dwd._report_name(batch)
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    for key, value in (("DATASET_ID", dataset), ("BATCH_DATE", batch), ("ENV", "test")):
        monkeypatch.setenv(f"OUTPATIENT_PREVIEW_{key}", value)
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        for file in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/auth/data_access_audit.sql"):
            cursor.execute((root / file).read_text())
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()

    days = ("2024-02-01", "2024-02-02", "2025-01-15", "2025-02-01")
    last = "2025-02-28"
    try:
        for name, record in selected.items():
            path = Path(record["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = sim_csv_rows(name)
            last_row = rows[-1].copy()
            for field in ("visit_id", "charge_id", "record_id", "report_id"):
                if field in last_row:
                    last_row[field] = f"SIM_{field}_4"
            rows.append(last_row)
            for day, row in zip(days, rows, strict=True):
                for field in ("visit_date", "admit_date", "fee_date", "report_date", "stat_date", "discharge_date"):
                    if field in row:
                        row[field] = day
                if "stay_days" in row:
                    row["stay_days"] = "0"
            if name == "his_inpatient":
                rows[-1].update(discharge_date=last, stay_days="27")
            if name == "his_outpatient":
                rows.append(dict(rows[-1], visit_id="SIM_CURRENT_EXTRA", dept_code=OTHER))
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(rows)
        result = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=90,
        )
        if result.returncode:
            try:
                code = json.loads(result.stdout)["code"]
            except (ValueError, KeyError, TypeError):
                code = "NO_SENTINEL"
            assert isinstance(code, str)
            print("DASHBOARD_DIAGNOSTIC", code.lower())
        assert result.returncode == 0, "Isolated calendar build failure; raw output withheld"
        monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", build_dwd.verify_current_build(**args)["build_run_id"])
        auth = client.post(LOGIN, json={"username": names["admin"], "password": PASSWORD})
        assert auth.status_code == 200
        admin = auth_header(auth.json()["data"]["access_token"])
        assert client.post(USERS, headers=admin, json={
            "username": names["analyst"], "password": PASSWORD, "role": "analyst", "department": None,
        }).status_code == 201
        marts.rerun((days[0], last), **args)
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT EXTRACT(YEAR_MONTH FROM visit_date), COUNT(*) FROM dwd_visit_detail "
                "WHERE dataset_id=%s AND visit_type='OUTPATIENT' GROUP BY EXTRACT(YEAR_MONTH FROM visit_date)",
                (dataset,),
            )
            assert dict(cursor.fetchall()) == {202402: 2, 202501: 1, 202502: 2}
        run_report_browser(root, names, PASSWORD, calendar=True)
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if report.exists():
            report.unlink()
