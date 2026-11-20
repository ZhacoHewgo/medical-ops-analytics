import csv
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, build_dims, build_dwd
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_qa_query import DEPT, FEB, JAN, LAST, OTHER, qa_seeds
from test_s2_003_pipeline import mysql_pipeline


REPORT = "/api/llm/report"
ANOMALY = "/api/llm/anomaly"
KB = "/api/llm/kb"


def test_reports_are_protected(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        for path in (REPORT, ANOMALY, KB):
            assert client.post(path, json={}).status_code == 401
            assert client.get("/openapi.json").json()["paths"][path]["post"]["security"]


def test_rank_changes_uses_absolute_difference_and_stable_ties():
    from app.llm.report_gen import rank_changes

    rows = rank_changes({"A": 12, "B": 3, "C": 5, "D": 1}, {"A": 2, "B": 10, "C": 0, "D": 6})
    assert [(row["dept_code"], row["difference"]) for row in rows] == [("A", 10), ("B", -7), ("C", 5)]
    assert rows[2]["change_pct"] is None
    assert rows[2]["null_reason"] == "ZERO_BASE"


def test_handbook_answers_are_versioned_and_fail_closed(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.llm import kb

    answer = kb.kb_answer("门诊人次环比怎么算？")
    assert answer["status"] == "FOUND"
    ref, = answer["refs"]
    assert ref["indicator_code"] == "MOPS-OUT-02"
    assert ref["status"] == "工程候选口径"
    assert ref["excerpt_path"].endswith("metric_kb.json")
    assert kb.kb_answer("对比 MOPS-OUT-01 和 MOPS-OUT-02")["status"] == "NOT_FOUND"
    monkeypatch.setattr(kb, "HANDBOOK", tmp_path / "missing.json")
    with pytest.raises(HTTPException) as missing:
        kb.kb_answer("门诊人次")
    assert missing.value.status_code == 503


@pytest.mark.parametrize("change", [
    {"entries": []},
    {"version": None},
    {"source_sha256": "invalid"},
    {"entries": {"MOPS-OUT-01": {"title": "门诊人次", "answer": None}}},
])
def test_invalid_handbook_is_unavailable(change, tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.llm import kb

    document = json.loads(kb.HANDBOOK.read_text())
    path = tmp_path / "handbook.json"
    path.write_text(json.dumps({**document, **change}))
    monkeypatch.setattr(kb, "HANDBOOK", path)

    with pytest.raises(HTTPException) as malformed:
        kb.kb_answer("MOPS-OUT-01")
    assert malformed.value.status_code == 503


def test_mysql_reports_anomaly_kb_and_scope(mysql_pipeline, auth_client, monkeypatch, tmp_path):
    dataset, batch, selected, quality = mysql_pipeline
    root = sources.PROJECT_ROOT
    client, names, _, admin_id = auth_client
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    build_report = quality.parent / build_dwd._report_name(batch)
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

    def call(route, headers, **body):
        return client.post(route, headers=headers, json=body)

    def month(headers, value="2025-02", **extra):
        return call(REPORT, headers, type="monthly", month=value, **extra)

    def sections(response):
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        data = response.json()["data"]
        assert [item["id"] for item in data["sections"]] == [
            "overview", "trend", "department_comparison", "anomalies_and_actions",
        ]
        return data, {item["id"]: item["data"] for item in data["sections"]}

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
        assert month(department, dept_code=OTHER).status_code == 403
        assert call(ANOMALY, department, date=FEB, dept_code=OTHER).status_code == 403
        assert call(REPORT, admin, type="weekly", week="2025-W54").status_code == 422
        assert call(REPORT, admin, type="monthly", week="2025-W05", month="2025-02").status_code == 422
        assert month(admin).status_code == 503

        marts.rerun((FEB, LAST), **args)
        data, partial = sections(month(admin))
        assert data["source"] == "ads_outpatient_theme" and data["build_run_id"] == identity["build_run_id"]
        assert partial["overview"]["outpatient_visits"] == 3
        assert partial["overview"]["previous_visits"] is None
        assert partial["anomalies_and_actions"]["status"] == "PRIOR_UNAVAILABLE"
        assert call(ANOMALY, admin, date=FEB).status_code == 503

        marts.rerun((JAN, "2025-01-31"), **args)
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT EXTRACT(YEAR_MONTH FROM visit_date),dept_code,COUNT(*) "
                           "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_type='OUTPATIENT' "
                           "GROUP BY EXTRACT(YEAR_MONTH FROM visit_date),dept_code", (dataset,))
            oracle = {(m, d): n for m, d, n in cursor.fetchall()}
        assert oracle == {(202501, DEPT): 2, (202502, DEPT): 2, (202502, OTHER): 1}
        _, full = sections(month(admin))
        assert full["overview"]["outpatient_visits"] == sum(n for (m, _), n in oracle.items() if m == 202502)
        assert full["overview"]["previous_visits"] == 2 and full["overview"]["change_pct"] == "50.00"
        assert sum(row["visits"] for row in full["trend"]) == 3
        assert {row["dept_code"]: row["visits"] for row in full["department_comparison"]} == {DEPT: 2, OTHER: 1}
        anomaly = call(ANOMALY, admin, date=FEB)
        assert anomaly.status_code == 200
        top, = anomaly.json()["data"]["top_changes"]
        assert (top["dept_code"], top["previous"], top["current"], top["difference"]) == (OTHER, 0, 1, 1)
        assert top["change_pct"] is None and top["null_reason"] == "ZERO_BASE"
        data, weekly = sections(call(REPORT, admin, type="weekly", week="2025-W05"))
        assert data["period"] == {"start_date": "2025-01-27", "end_date": "2025-02-02"}
        assert weekly["overview"]["outpatient_visits"] == 4
        assert weekly["overview"]["previous_visits"] == 0 and weekly["overview"]["change_pct"] is None
        data, scoped = sections(month(department))
        assert data["scope"] == {"department": DEPT}
        assert scoped["overview"]["outpatient_visits"] == 2
        assert [row["dept_code"] for row in scoped["department_comparison"]] == [DEPT]

        knowledge = call(KB, department, question="MOPS-OUT-02 怎么算？")
        assert knowledge.status_code == 200
        assert knowledge.json()["data"]["refs"][0]["version"] == "MOPS-METRIC-v0.1"
        assert "上期" in knowledge.json()["data"]["answer"]
        miss = call(KB, admin, question="能否提供患者地址？").json()["data"]
        assert miss["status"] == "NOT_FOUND" and miss["refs"] == []
        from app.llm import kb

        malformed = tmp_path / "missing-provenance.json"
        handbook = json.loads(kb.HANDBOOK.read_text())
        del handbook["source_sha256"]
        malformed.write_text(json.dumps(handbook))
        with monkeypatch.context() as broken:
            broken.setattr(kb, "HANDBOOK", malformed)
            unavailable = call(KB, admin, question="MOPS-OUT-02")
            assert unavailable.status_code == 503 and unavailable.json()["data"] is None
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SELECT resource,http_status FROM auth_data_access WHERE actor_id=%s", (admin_id,))
            audit = set(cursor.fetchall())
            assert {("outpatient_report", 200), ("outpatient_anomaly", 200),
                    ("indicator_kb", 200), ("indicator_kb", 503)} <= audit
        marts.build_dws_daily(DEPT, FEB, **args)
        unavailable = month(department)
        assert unavailable.status_code == 503 and unavailable.json()["data"] is None
        assert call(ANOMALY, department, date=FEB).status_code == 503
        print("REPORT_SQL_OK monthly=3 prior=2 weekly=4 dept=2 top_delta=1 kb_versioned=1")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if build_report.exists():
            build_report.unlink()
