import csv
import json
from io import BytesIO
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from app.main import create_app
from sql_fixture import execute_schema

from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, aggregate_themes as themes, build_dims, build_dwd
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


SUMMARY = "/api/v1/outpatient/summary"
INPATIENT = "/api/v1/inpatient/summary"
THEME_PATHS = {name: f"/api/v1/{name}/summary" for name in ("revenue", "disease", "resource")}
EXPORT = "/api/v1/reports/export"
REPORT = "/api/v1/reports/outpatient"
DAY = "2025-01-01"
LAST = "2025-01-04"
DEPT = "SIM_DEPT_01"
OTHER = "SIM_DEPT_02"


def test_protected_ads_requires_auth_and_documents_routes(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        summary_denied = client.get(SUMMARY)
        export_denied = client.post(EXPORT, json={})
        docs = client.get("/docs")
        assert docs.status_code == 200 and "/openapi.json" in docs.text
        api = client.get("/openapi.json")
        assert api.status_code == 200
        contract = api.json()
        paths = contract["paths"]
        for endpoint, denied in ((paths[SUMMARY]["get"], summary_denied), (paths[EXPORT]["post"], export_denied)):
            assert denied.status_code == 401
            errors = endpoint["responses"]
            assert endpoint["security"] and {"200", "401", "403", "422", "503"} <= errors.keys()
            for code in ("401", "422"):
                ref = errors[code]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
                error_schema = contract["components"]["schemas"][ref]
                assert set(error_schema["required"]) == {"code", "msg", "data"}
                assert error_schema["properties"]["data"]["type"] == "null"
            assert json.loads(errors["401"]["description"].split("`")[1]) == denied.json()
            assert json.loads(errors["422"]["description"].split("`")[1]) == {
                "code": 422, "msg": "Invalid query parameters", "data": None,
            }
        assert client.get(INPATIENT).status_code == 401
        assert paths[INPATIENT]["get"]["security"]
        for path in THEME_PATHS.values():
            assert client.get(path).status_code == 401
            assert paths[path]["get"]["security"]
        assert client.get(REPORT).status_code == 401
        assert paths[REPORT]["get"]["security"]
        assert set(paths[EXPORT]["post"]["responses"]["200"]["content"]) == {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/pdf",
        }


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

    from app.services.export_service import export_report as _export

    row = {"date": DAY, "dept_code": "=1+1", "visits": 1}
    document = {"counts": [row], "meta": {"source": "ads_outpatient_theme",
                "batch_date": DAY, "build_run_id": "isolated-demo"}}
    summary = {"data": {"filters": {"start_date": DAY, "end_date": DAY, "dept_code": "=1+1"}}}
    result = _export(document, summary)
    workbook = load_workbook(BytesIO(result.body), read_only=True)
    try:
        cell = list(workbook["门诊人次"].iter_rows())[1][1]
        assert cell.value == "=1+1" and cell.data_type == "s"
        assert workbook["数据来源"]["B6"].value == "=1+1"
        assert workbook["数据来源"]["B6"].data_type == "s"
    finally:
        workbook.close()
    with pytest.raises(HTTPException) as error:
        _export({**document, "counts": [dict(row, dept_code=str(index)) for index in range(10001)]}, summary)
    assert error.value.status_code == 422


def test_ads_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    with TestClient(create_app()) as client:
        assert client.get(SUMMARY).status_code == 404
        assert client.post(EXPORT, json={}).status_code == 404
        assert client.get(REPORT).status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        paths = client.app.openapi()["paths"]
        assert {"/api/auth/login", SUMMARY, INPATIENT, *THEME_PATHS.values()} <= paths.keys()
        assert paths[SUMMARY]["get"]["security"]
        assert client.get("/api/ops/quality").status_code == 404
        assert client.post("/api/auth/login", json={"username": "synthetic_user", "password": "test-only"}).status_code == 404


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
            execute_schema(cursor, root / file)
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
        assert export(department, format="pdf", dept_code=OTHER).status_code == 403
        assert export(department, format="pdf", end_date="2025-01-05").status_code == 422
        assert summary(analyst, end_date="2025-01-05").status_code == 422
        invalid_grain = summary(analyst, granularity="month")
        assert invalid_grain.status_code == 422
        example = client.get("/openapi.json").json()["paths"][SUMMARY]["get"]["responses"]["422"]
        assert invalid_grain.json() == json.loads(example["description"].split("`")[1])
        assert summary(analyst, start_date="2025-02-30").status_code == 422
        assert summary(analyst, drill="patient_id").status_code == 422
        assert summary(analyst, dept_code="UNKNOWN").status_code == 422
        assert summary(analyst).status_code == 503
        assert export(analyst).status_code == 503
        assert export(analyst, format="pdf").status_code == 503

        quality_path = "/api/ops/quality"
        assert client.get(quality_path).status_code == 401
        assert client.get(quality_path, headers=department).status_code == 403
        assert client.get(quality_path, headers=analyst, params={"start_date": "2025-02-30"}).status_code == 422
        quality = client.get(quality_path, headers=analyst)
        assert quality.status_code == 200
        quality_rows = quality.json()["data"]
        assert len(quality_rows) == 1
        assert quality_rows[0]["batch_date"] == batch and quality_rows[0]["gate_status"] == "PASS"
        assert quality_rows[0]["executed_rules"] == 34
        assert set(quality_rows[0]) == {
            "batch_date", "gate_status", "score", "coverage", "planned_rules", "executed_rules", "evaluated_rules",
        }
        assert client.get(INPATIENT, headers=analyst).status_code == 503
        assert client.get(INPATIENT, headers=department, params={"dept_code": OTHER}).status_code == 403
        for path in THEME_PATHS.values():
            assert client.get(path, headers=analyst).status_code == 503
            assert client.get(path, params={"dept_code": OTHER}, headers=department).status_code == 403
        marts.rerun((DAY, LAST), themes=("inpatient", "income", "drg", "efficiency"), **args)
        inpatient = client.get(INPATIENT, headers=department)
        assert inpatient.status_code == 200 and inpatient.headers["cache-control"] == "no-store"
        inpatient_data = inpatient.json()["data"]
        assert inpatient_data["filters"] == {"dept_code": DEPT, "start_date": DAY, "end_date": LAST}
        assert inpatient_data["meta"]["build_run_id"] == original
        assert inpatient_data["discharges"] > 0
        assert sum(row["discharges"] for row in inpatient_data["daily_trend"]) == inpatient_data["discharges"]
        assert inpatient_data["avg_stay_days"]["value"] is not None
        assert client.get(INPATIENT, headers=analyst, params={"dept_code": "UNKNOWN"}).status_code == 422
        empty = client.get(INPATIENT, headers=analyst, params={"start_date": DAY, "end_date": DAY}).json()["data"]
        assert empty["discharges"] == 0 and empty["avg_stay_days"] == {"value": None, "null_reason": "ZERO_BASE"}
        def theme_response(name, headers, **params):
            response = client.get(THEME_PATHS[name], params=params, headers=headers)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            return response.json()["data"]

        revenue = theme_response("revenue", analyst)
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT SUM(amount) FROM dwd_charge_detail WHERE dataset_id=%s", (dataset,))
            charge_total, = cursor.fetchone()
        assert Decimal(revenue["metrics"]["charge_amount"]["value"]) == charge_total
        assert revenue["metrics"]["net_revenue"] == {"value": None, "null_reason": "NET_REVENUE_RULE_UNCONFIRMED"}

        disease = theme_response("disease", department, drill="department")
        assert disease["filters"]["dept_code"] == DEPT
        assert {row["group"] for row in disease["rows"]} == {DEPT}
        assert disease["metrics"]["source_diagnosis_cases"]["value"] == str(inpatient_data["discharges"])
        assert disease["metrics"]["drg_grouped_cases"]["null_reason"] == "OFFICIAL_DRG_GROUPING_MISSING"
        resource = theme_response("resource", analyst, start_date=DAY, end_date=DAY)
        assert resource["metrics"]["discharges"] == {"value": "0", "null_reason": None}
        assert resource["metrics"]["bed_usage_rate"] == {"value": None, "null_reason": "BED_CAPACITY_MISSING"}
        assert resource["rows"] == []
        assert client.get(THEME_PATHS["revenue"], headers=analyst, params={"drill": "patient_id"}).status_code == 422

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
            pdf = export(analyst, format="pdf", group_by=group_by, expected_build_run_id=original)
            assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
        assert grouped(department, group_by="month_department").json()["data"]["rows"] == [
            {"date": "2025-01", "dept_code": DEPT, "visits": 3}
        ]
        assert export(department, expected_build_run_id="ffffffff-ffff-4fff-bfff-ffffffffffff").status_code == 409

        empty = {"start_date": LAST, "end_date": LAST}
        for table, extra in ((marts.ADS, ""), (marts.DWS, ", ads_status")):
            with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {table} ({', '.join(marts.COLUMNS)}{extra}) "
                    "SELECT dataset_id,%s,dept_code,batch_date,source_build_run_id,"
                    f"outpatient_visits,outpatient_fee_samples,outpatient_total_fee,fee_status{extra} "
                    f"FROM {table} WHERE dataset_id=%s AND stat_date=%s AND dept_code=%s",
                    (LAST, dataset, DAY, DEPT),
                )
                assert cursor.rowcount == 1
                db.commit()
            try:
                for endpoint in (summary, export):
                    for headers in (analyst, department):
                        rejected = endpoint(headers, **empty)
                        assert rejected.status_code == 503, f"{table} single publication must return 503"
                        assert rejected.json()["data"] is None
                unaffected = summary(analyst, dept_code=OTHER, **empty)
                assert unaffected.status_code == 200
                assert unaffected.json()["data"]["outpatient_visits"] == 0
            finally:
                with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE dataset_id=%s AND stat_date=%s AND dept_code=%s",
                        (dataset, LAST, DEPT),
                    )
                    db.commit()
        assert summary(analyst, **empty).json()["data"]["outpatient_visits"] == 0
        print("ADS_PUBLICATION_OK ads_only=503 dws_only=503 other_department=0 restored=0 endpoints=2")

        assert export(department, format="pdf",
                      expected_build_run_id="ffffffff-ffff-4fff-bfff-ffffffffffff").status_code == 409
        for params in ({}, {"start_date": LAST, "end_date": LAST}):
            pdf = export(department, format="pdf", expected_build_run_id=original, **params)
            assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
            assert pdf.headers["Content-Type"] == "application/pdf"
            assert pdf.headers["Cache-Control"] == "no-store"
            assert pdf.headers["Content-Disposition"] == 'attachment; filename="outpatient.pdf"'
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

        if seconds := int(os.environ.get("MOPS_MIXED_HTTP_SECONDS", "0")):
            from collections import Counter

            from tests.perf.bench import rehearse

            assert seconds > 0

            def audits():
                with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                    cursor.execute(
                        "SELECT action,http_status,COUNT(*) FROM auth_data_access WHERE resource='outpatient' "
                        "AND actor_id IN (SELECT user_id FROM auth_users WHERE username IN (%s,%s)) "
                        "GROUP BY action,http_status", (names["analyst"], names["department"]),
                    )
                    return Counter({(action, status): total for action, status, total in cursor.fetchall()})

            rehearse(create_app(), analyst, department, truth, original, DEPT, OTHER, LAST, audits, seconds=seconds)

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
        from app.services import indicator_service
        actual_load = indicator_service.load_ads

        def revoke_during_read(*a, **kw):
            document = actual_load(*a, **kw)
            with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                cursor.execute("UPDATE auth_users SET enabled=FALSE WHERE username=%s", (names["department"],))
                db.commit()
            return document

        with monkeypatch.context() as changes:
            changes.setattr(indicator_service, "load_ads", revoke_during_read)
            revoked = summary(department, start_date=DAY, end_date=DAY)
            assert revoked.status_code == 401 and revoked.json()["data"] is None
        print("REPORT_HTTP_OK groups=4 scoped=3 empty=0 stale_export=409")
        print("ADS_HTTP_OK full=4 department=3 empty=0 unauthorized=401 forbidden=403 pending=503 partial=503 xlsx=3")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, *themes.COLUMNS, themes.STATUS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
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
            execute_schema(cursor, root / file)
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
