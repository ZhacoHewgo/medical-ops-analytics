import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from app.llm import catalog
from app.llm.eval.eval import evaluate, load_cases
from app.main import create_app
from sql_fixture import execute_schema

from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, aggregate_themes, build_dims, build_dwd
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


ENDPOINT = "/api/llm/query"
EVALSET = Path(__file__).resolve().parents[1] / "app/llm/eval/evalset.json"
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


def qa_seed_rows():
    return {name: qa_seeds(name) for name in sources.SOURCES}


class _Reply:
    def __init__(self, status, data, leak=False):
        payload = {"code": 0, "msg": "ok", "data": data}
        if leak and data is not None:
            payload["data"] = {**data, "patient_id": "SIM_PATIENT_1"}
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return json.loads(self.text)


def _shift(value):
    if value is None:
        return None
    return format(Decimal(str(value)) - 1, "f")


TOKENS = {"admin": "admin-header", "department": "department-header"}


def _stub_client(cases, values, *, drift=False, leak=False):
    answers = {(case["role"], case["question"]): case for case in cases}
    roles = {token: role for role, token in TOKENS.items()}

    def reply(role, question):
        case = answers[(role, question)]
        if case["status"] != 200:
            return _Reply(case["status"], None)
        points = values.get(case["id"], [None])
        if drift:
            points = [_shift(value) for value in points]
        return _Reply(200, {
            "indicator_code": case["indicator_code"], "unit": case["unit"],
            "chart_type": case["chart_type"], "null_reason": case["null_reason"],
            "scope": case.get("scope", {"department": None}), "interpretation": "stub answer",
            "points": [{"start_date": JAN, "end_date": LAST, "value": value} for value in points],
        }, leak=leak)

    class Client:
        def post(self, url, json=None, headers=None):
            assert url == ENDPOINT
            return reply(roles[headers], json["question"])

    return Client()


def test_evalset_expands_to_fifty_cross_theme_cases():
    document = json.loads(EVALSET.read_text(encoding="utf-8"))
    cases = load_cases(EVALSET)
    assert len(cases) == 50 == len({case["id"] for case in cases})
    assert {f"QA-{index:02d}" for index in range(1, 9)} <= {case["id"] for case in cases}
    assert 0.8 <= document["threshold"] <= 1
    themes = {case["theme"] for case in cases}
    assert {"门诊", "住院", "收入", "药品", "耗材", "病种", "资源效率", "患者服务", "安全边界"} <= themes
    assert {case["role"] for case in cases} == {"admin", "department"}
    assert {case["status"] for case in cases} == {200, 403, 422, 503}
    assert [case["id"] for case in cases if case["status"] == 503] == ["QA-11"]

    answered = [case for case in cases if case["status"] == 200]
    graded = {case["indicator_code"] for case in answered}
    assert set(catalog.SERVED) <= graded and set(catalog.GAPS) & graded
    nulls = [case for case in answered if case.get("expect_null")]
    gaps = [case for case in nulls if case["indicator_code"] in catalog.GAPS]
    assert gaps and {case["null_reason"] for case in gaps} == {catalog.entry(case["indicator_code"])["reason"]
                                                               for case in gaps}
    assert [case["indicator_code"] for case in nulls if case["indicator_code"] not in catalog.GAPS] == ["MOPS-OUT-02"]
    assert all("oracle" in case for case in answered if case["indicator_code"] in catalog.SERVED)
    assert not [case["id"] for case in cases if "points" in case]
    assert {case["role"] for case in cases if case["status"] == 403} == {"admin", "department"}


def test_evalset_intents_and_scope_resolve_without_database():
    from dataclasses import dataclass

    from fastapi import HTTPException

    from app.llm import nl2sql

    @dataclass
    class Account:
        role: str
        department: str | None = None

    mismatched = []
    for case in load_cases(EVALSET):
        user = Account(case["role"], DEPT if case["role"] == "department" else None)
        try:
            intent = nl2sql.parse_intent(case["question"])
            department = nl2sql._enforce_scope(intent, user)
            produced = 200
        except HTTPException as error:
            intent, department, produced = None, None, error.status_code
        expected = 200 if case["status"] == 503 else case["status"]
        if produced != expected:
            mismatched.append((case["id"], produced, case["status"]))
        elif intent is not None and "indicator_code" in case:
            entry = catalog.entry(intent.code)
            shape = (intent.code, entry["unit"], "line" if intent.series else "number", department)
            want = (case["indicator_code"], case["unit"], case["chart_type"],
                    case.get("scope", {}).get("department"))
            if shape != want:
                mismatched.append((case["id"], shape, want))
    assert mismatched == []


def test_evalset_expectations_are_recomputed_not_hardcoded():
    from app.llm.eval import oracle

    rows = qa_seed_rows()
    values = {}
    for case in load_cases(EVALSET):
        if "oracle" not in case:
            continue
        points = oracle.expected_points(case["oracle"], rows)
        assert len(points) == (2 if case["oracle"].get("form") == "series" else 1)
        if case.get("expect_null"):
            assert points == [None]
        else:
            assert all(value is not None for value in points)
        values[case["id"]] = points
    assert len(values) >= 20 and len({str(value) for value in values.values()}) > 5

    moved = json.loads(json.dumps(rows))
    moved["his_outpatient"][0]["visit_date"] = FEB
    drifted = [case["id"] for case in load_cases(EVALSET) if "oracle" in case
               and oracle.expected_points(case["oracle"], moved) != values[case["id"]]]
    assert drifted
    with pytest.raises(oracle.OracleError):
        oracle.expected_points({"metric": "MOPS-DRG-99", "start": JAN, "end": LAST}, rows)


def test_eval_harness_rejects_drift_and_leakage_without_database():
    from app.llm.eval import oracle

    cases = load_cases(EVALSET)
    rows = qa_seed_rows()
    values = {case["id"]: oracle.expected_points(case["oracle"], rows) for case in cases if "oracle" in case}
    graded = [case for case in cases if "oracle" in case and not case.get("expect_null")]
    headers = TOKENS

    clean = evaluate(_stub_client(cases, values), cases, headers=headers, rows=rows)
    assert clean["passed"] and clean["correct"] == clean["total"] == 50 and clean["failures"] == []
    assert sum(item["total"] for item in clean["themes"].values()) == 50

    drifted = evaluate(_stub_client(cases, values, drift=True), cases, headers=headers, rows=rows)
    assert not drifted["passed"] and drifted["accuracy"] < drifted["threshold"]
    assert drifted["correct"] == 50 - len(graded)
    assert any("points" in problem for item in drifted["failures"] for problem in item["problems"])

    leaked = evaluate(_stub_client(cases, values, leak=True), cases, headers=headers, rows=rows)
    assert not leaked["passed"] and leaked["correct"] == 50 - len([case for case in cases if case["status"] == 200])
    assert leaked["failures"] and all("leak:patient_id" in item["problems"] for item in leaked["failures"])

    without_rows = evaluate(_stub_client(cases, values), cases, headers=headers)
    assert not without_rows["passed"] and any("oracle:unavailable" in item["problems"]
                                              for item in without_rows["failures"])


def test_eval_harness_validates_case_list(tmp_path):
    document = {"cases": [{"id": "QA-01", "role": "admin", "question": "2025-02全院门诊人次", "status": 200}]}
    path = tmp_path / "evalset.json"
    path.write_text(json.dumps(document, ensure_ascii=False))
    with pytest.raises(ValueError):
        load_cases(path)
    document["cases"][0].update({"indicator_code": "MOPS-OUT-01", "unit": "人次", "chart_type": "number"})
    path.write_text(json.dumps(document, ensure_ascii=False))
    assert load_cases(path)[0]["id"] == "QA-01"
    path.write_text(json.dumps({"cases": []}))
    with pytest.raises(ValueError):
        load_cases(path)
    with pytest.raises(ValueError):
        evaluate(_stub_client([], {}), [], headers={}, rows=None)


class _Account:
    def __init__(self, role="admin", department=None):
        self.role = role
        self.department = department


RUN_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def sql_env(monkeypatch):
    monkeypatch.setenv("ODS_DATABASE_URL", "mysql://qa:qa@127.0.0.1:13306/hospital")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_ENV", "dev")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_DATASET_ID", "SIM_QA_DATASET")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BATCH_DATE", "2025-03-01")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", RUN_ID)
    return RUN_ID


class _PlanStub:
    template = "outpatient_volume"
    places = 0
    first = date(2025, 2, 1)
    last = date(2025, 2, 28)


@pytest.fixture
def sql_gateway(monkeypatch):
    from app.llm import nl2sql

    seen = []

    def build(intent, user=None, **kwargs):
        seen.append(("plan", intent.code, kwargs.get("department"), getattr(user, "role", None)))
        return _PlanStub()

    def run(plan, **kwargs):
        seen.append(("execute", plan.template))
        return [(202502, 1, 0)]

    def check(plan, observation, rows):
        seen.append(("cross_check", len(rows)))

    monkeypatch.setattr(nl2sql, "generate_sql", build)
    monkeypatch.setattr(nl2sql, "execute_plan", run)
    monkeypatch.setattr(nl2sql, "_cross_check", check)
    return seen


def test_mart_readers_are_limited_to_the_served_whitelist(monkeypatch):
    from app.llm import readers
    from app.llm.nl2sql import run_nl_query

    assert set(readers.READERS) == set(catalog.SERVED)
    assert not set(readers.READERS) & set(catalog.GAPS)

    class Guard(dict):
        def __getitem__(self, key):
            raise AssertionError(f"unexpected mart read: {key}")

    monkeypatch.setattr(readers, "READERS", Guard())
    answer = run_nl_query("2025-02 全院药占比", _Account())
    assert answer["indicator_code"] == "MOPS-DRUG-02" and answer["points"][0]["value"] is None
    assert answer["null_reason"] == "FEE_CATEGORY_RULE_UNCONFIRMED"
    assert answer["chart_type"] == "number" and answer["unit"] == "%"


def test_course_summary_questions_keep_department_authorization(monkeypatch):
    from fastapi import HTTPException

    from app.llm import nl2sql, readers
    from etl.config.source_config import COURSE_DATASET

    monkeypatch.setenv("OUTPATIENT_PREVIEW_DATASET_ID", COURSE_DATASET)
    seen = []

    def summary(code, first, last, scope):
        seen.append((code, scope))
        return {"source": "ads_inpatient_theme", "batch_date": "2025-06-30", "build_run_id": RUN_ID,
                "points": [{"start_date": first.isoformat(), "end_date": last.isoformat(), "value": "34.26"}]}

    monkeypatch.setattr(readers, "course_summary", summary)
    user = _Account("department", "HN")
    with pytest.raises(HTTPException) as denied:
        nl2sql.run_nl_query("2025年5月全院床位使用率", user)
    assert denied.value.status_code == 403 and not seen
    result = nl2sql.run_nl_query("2025年5月本科室床位使用率", user)
    assert result["points"][0]["value"] == "34.26"
    assert seen == [("MOPS-INP-04", "HN")]


def test_slow_mart_read_degrades_to_503(monkeypatch, sql_gateway):
    from fastapi import HTTPException

    from app.llm import nl2sql, readers

    def slow(*args, **kwargs):
        time.sleep(0.05)
        return {"unit": "人次", "source": "stub", "batch_date": None, "build_run_id": None,
                "null_reason": None, "chart_type": "number",
                "points": [{"start_date": JAN, "end_date": LAST, "value": 1}]}

    monkeypatch.setitem(readers.READERS, "MOPS-OUT-01", slow)
    monkeypatch.setattr(nl2sql, "BUDGET_SECONDS", 0.01)
    with pytest.raises(HTTPException) as degraded:
        nl2sql.run_nl_query("2025-02 全院门诊人次", _Account())
    assert degraded.value.status_code == 503


@pytest.mark.parametrize("code,question,unit,chart", [
    ("MOPS-OUT-01", "2025-02 全院门诊人次", "人次", "number"),
    ("MOPS-OUT-02", "2025-02 全院门诊人次环比", "%", "number"),
    ("MOPS-INP-01", "2025-02 全院出院人次", "人次", "number"),
    ("MOPS-INP-02", "2025-02 全院平均住院日", "天", "number"),
    ("MOPS-INP-06", "2025-02 全院住院次均费用", "元/人次", "number"),
    ("MOPS-INC-01", "2025-02 全院医疗收入总额", "元", "number"),
])
def test_every_served_metric_answers_through_a_stubbed_mart(code, question, unit, chart, monkeypatch, sql_gateway):
    from app.llm import nl2sql, readers

    seen = {}

    def fake(first, last, scope, user, series):
        seen.update(first=first, last=last, scope=scope, series=series)
        return {"unit": unit, "source": "stub_mart", "batch_date": "2030-01-01", "build_run_id": "stub-run",
                "null_reason": None, "chart_type": chart, "detail": (9, 6),
                "points": [{"start_date": first.isoformat(), "end_date": last.isoformat(), "value": 7}]}

    monkeypatch.setitem(readers.READERS, code, fake)
    answer = nl2sql.run_nl_query(question, _Account())
    assert [step[0] for step in sql_gateway] == ["plan", "execute", "cross_check"]
    assert sql_gateway[0][1] == code and sql_gateway[0][3] == "admin"
    assert answer["indicator_code"] == code and answer["unit"] == unit and answer["chart_type"] == chart
    assert answer["points"][0]["value"] == 7 and answer["source"] == "stub_mart"
    assert answer["batch_date"] == "2030-01-01" and answer["build_run_id"] == "stub-run"
    assert answer["scope"] == {"department": None} and answer["interpretation"]
    assert seen["scope"] is None and not seen["series"]
    assert (seen["first"].isoformat(), seen["last"].isoformat()) == (FEB, LAST)
    assert set(answer) == {"indicator_code", "unit", "scope", "chart_type", "points", "interpretation",
                           "null_reason", "source", "batch_date", "build_run_id"}
    if code == "MOPS-OUT-02":
        assert "上期 6 人次" in answer["interpretation"]


def test_monthly_series_answers_sum_the_expanded_points(monkeypatch, sql_gateway):
    from app.llm import nl2sql, readers

    def fake(first, last, scope, user, series):
        assert series is True
        return {"unit": "元", "source": "stub_mart", "batch_date": None, "build_run_id": None,
                "null_reason": None, "chart_type": "line",
                "points": [{"start_date": JAN, "end_date": "2025-01-31", "value": "10.00"},
                           {"start_date": FEB, "end_date": LAST, "value": "4.50"}]}

    monkeypatch.setitem(readers.READERS, "MOPS-INC-01", fake)
    answer = nl2sql.run_nl_query("2025 年 1 至 2 月全院每月医疗收入", _Account())
    assert answer["chart_type"] == "line" and len(answer["points"]) == 2
    assert "共 14.5 元" in answer["interpretation"]


def test_generate_sql_emits_frozen_parameterised_plans(sql_env):
    from app.llm import nl2sql

    templates = set()
    for question in ("2025-02 全院门诊人次", "2025-02 全院门诊人次环比", "2025-02 全院出院人次",
                     "2025-02 全院平均住院日", "2025-02 全院住院次均费用", "2025-02 全院医疗收入总额"):
        plan = nl2sql.generate_sql(question)
        assert plan.statement.startswith("SELECT ") and ";" not in plan.statement
        assert "2025" not in plan.statement and "全院" not in plan.statement
        assert plan.tables == (nl2sql._SQL_TEMPLATES[plan.template]["table"],)
        assert plan.tables[0] in nl2sql.SQL_TABLES
        assert set(plan.columns) <= set(nl2sql.SQL_TABLES[plan.tables[0]])
        assert plan.statement.count("%s") == len(plan.params) == 5
        assert plan.params[:2] == ("SIM_QA_DATASET", "2025-03-01") and plan.params[2] == sql_env
        templates.add(plan.template)
    assert templates == {"outpatient_volume", "inpatient_discharges", "inpatient_stay",
                         "inpatient_cost", "income_total"}
    assert set(nl2sql.SQL_TABLES) == {spec["table"] for spec in nl2sql._SQL_TEMPLATES.values()}

    series = nl2sql.generate_sql("2025 年 1 至 2 月全院每月医疗收入")
    assert series.series and series.statement.endswith("GROUP BY period ORDER BY period")
    scoped = nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department", DEPT))
    assert scoped.scope == DEPT and scoped.params[-1] == DEPT and len(scoped.params) == 6
    assert scoped.statement.endswith("AND dept_code=%s GROUP BY period ORDER BY period")
    growth = nl2sql.generate_sql("2025-02 全院门诊人次环比")
    assert growth.params[3:] == ("2025-01-01", LAST)
    assert tuple(growth) == (growth.statement, growth.params)


def test_generate_sql_rejects_free_sql_gap_metrics_and_scope_escapes(sql_env):
    from fastapi import HTTPException

    from app.llm import nl2sql

    for question, status in (("SELECT patient_name FROM patients", 403),
                             ("2025-02 全院床位使用率", 422),
                             ("2025-02 全院门诊人次同比", 422)):
        with pytest.raises(HTTPException) as refused:
            nl2sql.generate_sql(question)
        assert refused.value.status_code == status
    with pytest.raises(HTTPException) as foreign:
        nl2sql.generate_sql("2025-02 全院门诊人次", _Account("department", DEPT))
    assert foreign.value.status_code == 403
    with pytest.raises(HTTPException) as other_dept:
        nl2sql.generate_sql(f"2025-02 科室={OTHER} 门诊人次", _Account("department", DEPT))
    assert other_dept.value.status_code == 403


def test_generate_sql_rejects_explicit_department_that_conflicts_with_the_role(sql_env):
    from fastapi import HTTPException

    from app.llm import nl2sql

    with pytest.raises(HTTPException) as conflict:
        nl2sql.generate_sql("2025-02 全院门诊人次", _Account("department", DEPT), department=OTHER)
    assert conflict.value.status_code == 403
    with pytest.raises(HTTPException) as cross:
        nl2sql.generate_sql(f"2025-02 科室={OTHER} 门诊人次", _Account("department", DEPT), department=DEPT)
    assert cross.value.status_code == 403
    with pytest.raises(HTTPException) as unbound:
        nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department"), department=DEPT)
    assert unbound.value.status_code == 403

    same = nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department", DEPT), department=DEPT)
    assert same.scope == DEPT and same.params[-1] == DEPT and len(same.params) == 6
    narrowed = nl2sql.generate_sql("2025-02 全院门诊人次", _Account("admin"), department=DEPT)
    assert narrowed.scope == DEPT and narrowed.params[-1] == DEPT
    with pytest.raises(HTTPException) as forged:
        nl2sql.generate_sql(f"2025-02 科室={DEPT} 门诊人次", _Account("analyst"), department=OTHER)
    assert forged.value.status_code == 403
    with pytest.raises(HTTPException) as malformed:
        nl2sql.generate_sql("2025-02 全院门诊人次", _Account("admin"), department=f"{DEPT} OR 1=1")
    assert malformed.value.status_code == 422


def test_validate_sql_guards_the_same_frozen_plan_used_by_execution(sql_env):
    from fastapi import HTTPException

    from app.llm import nl2sql

    plan = nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department", DEPT))
    assert nl2sql.validate_sql(plan) == (plan.statement, plan.params)
    for forged in (replace(plan, statement=plan.statement + " OR 1=1"),
                   replace(plan, scope=None),
                   replace(plan, params=plan.params[:-1]),
                   replace(plan, tables=("dwd_visit_detail",))):
        with pytest.raises(HTTPException) as refused:
            nl2sql.validate_sql(forged)
        assert refused.value.status_code == 422


def test_model_candidate_is_sealed_into_the_frozen_template(sql_env):
    from app.llm import nl2sql

    seen = {}

    def generator(question, prompt):
        seen.update(question=question, prompt=prompt)
        return {"indicator": "出院人次", "start_date": JAN, "end_date": LAST,
                "scope": "全院", "series": False, "growth": False}

    plan = nl2sql.generate_sql("2025-02 全院出院人次", _Account(), generator=generator)
    assert plan.origin == "model" and plan.metric == "MOPS-INP-01"
    assert plan.template == "inpatient_discharges"
    assert "2025" not in plan.statement and "全院" not in plan.statement
    assert plan.params[3:5] == (JAN, LAST) and len(plan.params) == 5
    assert nl2sql.validate_sql(plan) == (plan.statement, plan.params)
    assert seen["prompt"][0]["role"] == "system" and "不要输出 SQL" in seen["prompt"][0]["content"]
    assert nl2sql.generate_sql("2025-02 全院出院人次", _Account()).origin == "rules"


def test_model_candidate_cannot_smuggle_sql_or_escape_its_role(sql_env):
    from fastapi import HTTPException

    from app.llm import nl2sql

    def raw_sql(question, prompt):
        return {"indicator": "门诊人次", "start_date": JAN, "end_date": LAST,
                "sql": "SELECT * FROM ads_outpatient_theme"}

    plan = nl2sql.generate_sql("2025-02 全院门诊人次", _Account(), generator=raw_sql)
    assert plan.origin == "model-fallback" and plan.template == "outpatient_volume"
    assert "SELECT * FROM" not in plan.statement

    def free_text(question, prompt):
        return "SELECT 1"

    assert nl2sql.generate_sql("2025-02 全院门诊人次", _Account(),
                               generator=free_text).origin == "model-fallback"

    def timeout(question, prompt):
        raise TimeoutError("model budget exhausted")

    assert nl2sql.generate_sql("2025-02 全院门诊人次", _Account(),
                               generator=timeout).origin == "model-fallback"

    def foreign(question, prompt):
        return {"indicator": "门诊人次", "start_date": JAN, "end_date": LAST,
                "scope": f"科室={OTHER}"}

    with pytest.raises(HTTPException) as refused:
        nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department", DEPT), generator=foreign)
    assert refused.value.status_code == 403


def test_run_nl_query_uses_the_candidate_only_when_it_agrees_with_the_rules(sql_env, monkeypatch):
    from app.llm import nl2sql, readers

    def fake(first, last, scope, user, series):
        return {"unit": "人次", "source": "stub_mart", "batch_date": None, "build_run_id": None,
                "null_reason": None, "chart_type": "number",
                "points": [{"start_date": first.isoformat(), "end_date": last.isoformat(), "value": 7}]}

    monkeypatch.setitem(readers.READERS, "MOPS-OUT-01", fake)
    monkeypatch.setattr(nl2sql, "_cross_check", lambda plan, observation, rows: None)
    origins = []

    def execute(plan, **kwargs):
        origins.append(plan.origin)
        return [(202502, 7, 0)]

    monkeypatch.setattr(nl2sql, "execute_plan", execute)

    def agree(question, prompt):
        return {"indicator": "门诊人次", "start_date": FEB, "end_date": LAST, "scope": "全院"}

    answer = nl2sql.run_nl_query("2025-02 全院门诊人次", _Account(), generator=agree)
    assert answer["indicator_code"] == "MOPS-OUT-01" and origins == ["model"]

    def disagree(question, prompt):
        return {"indicator": "医疗收入总额", "start_date": FEB, "end_date": LAST, "scope": "全院"}

    answer = nl2sql.run_nl_query("2025-02 全院门诊人次", _Account(), generator=disagree)
    assert answer["indicator_code"] == "MOPS-OUT-01" and origins == ["model", "model-fallback"]

    def wrong_date(question, prompt):
        return {"indicator": "门诊人次", "start_date": JAN, "end_date": LAST, "scope": "全院"}

    answer = nl2sql.run_nl_query("2025-02 全院门诊人次", _Account(), generator=wrong_date)
    assert answer["points"][0]["start_date"] == FEB and origins[-1] == "model-fallback"


@pytest.mark.parametrize("scope, sent_scope", [("全院", "全院"), ("科室=PRIVATE123", "科室=DEPT")])
def test_remote_model_receives_only_the_validated_question_fields(monkeypatch, scope, sent_scope):
    from app.llm import model_client, nl2sql

    monkeypatch.setenv("LLM_API_BASE_URL", "https://model.example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "demo")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    question = f"附注示例 2025-02 {scope}门诊人次"

    async def complete(url, model, key, timeout, messages):
        assert url.host == "model.example.test"
        assert model == "demo" and key == "" and timeout == 8
        payload = json.loads(messages[-1]["content"])
        assert payload["question"] == f"2025-02-01 至 2025-02-28 {sent_scope} MOPS-OUT-01"
        return {"indicator": "MOPS-OUT-01", "scope": sent_scope}

    monkeypatch.setattr(model_client, "_completion", complete)
    generator = model_client.configured_generator()
    assert generator(question, nl2sql.candidate_prompt(question)) == {"indicator": "MOPS-OUT-01", "scope": scope}


def test_execute_plan_rejects_forged_or_unlisted_plans(sql_env):
    from fastapi import HTTPException

    from app.llm import nl2sql

    valid = nl2sql.generate_sql("2025-02 全院门诊人次")
    forged = [replace(valid, template="outpatient_volume; DROP TABLE ads_outpatient_theme"),
              replace(valid, statement=valid.statement + " OR 1=1"),
              replace(valid, params=valid.params + ("extra",)),
              replace(valid, tables=("dwd_visit_detail",)),
              replace(valid, columns=("patient_id",)),
              replace(valid, params=(valid.params[0], "2025-02-30", *valid.params[2:]))]
    for plan in forged:
        with pytest.raises(HTTPException) as refused:
            nl2sql.execute_plan(plan)
        assert refused.value.status_code == 422
    scoped = nl2sql.generate_sql("2025-02 本科室门诊人次", _Account("department", DEPT))
    with pytest.raises(HTTPException):
        nl2sql.execute_plan(replace(scoped, params=scoped.params[:-1] + ("x' OR 1=1 --",)))
    with pytest.raises(HTTPException):
        nl2sql.execute_plan(object())


def test_execute_plan_binds_parameters_in_a_read_only_snapshot(sql_env, monkeypatch):
    from app.llm import nl2sql
    from etl.config import source_config as sources

    log, state = [], {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            log.append((statement, params))

        def fetchall(self):
            return [(202502, 3, 0)]

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            state["rollback"] = True

        def close(self):
            state["close"] = True

    monkeypatch.setattr(sources, "connect_database", lambda **kwargs: Connection())
    plan = nl2sql.generate_sql("2025-02 全院门诊人次")
    assert nl2sql.execute_plan(plan) == [(202502, 3, 0)]
    assert ("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY", None) in log
    statement, params = log[-1]
    assert statement == plan.statement and params == plan.params
    assert not any("2025-02" in text or "全院" in text for text, _ in log)
    assert state == {"rollback": True, "close": True}


def test_execute_plan_times_out_as_503(sql_env, monkeypatch):
    from fastapi import HTTPException

    from app.llm import nl2sql
    from etl.config import source_config as sources

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            pass

        def fetchall(self):
            time.sleep(0.05)
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sources, "connect_database", lambda **kwargs: Connection())
    with pytest.raises(HTTPException) as degraded:
        nl2sql.execute_plan(nl2sql.generate_sql("2025-02 全院门诊人次"), budget=0.01)
    assert degraded.value.status_code == 503


def test_execute_plan_degrades_on_connection_and_query_errors(sql_env, monkeypatch):
    from fastapi import HTTPException

    from app.llm import nl2sql
    from etl.config import source_config as sources

    plan = nl2sql.generate_sql("2025-02 全院门诊人次")

    def offline(**kwargs):
        raise sources.OdsError("ODS_DATABASE_CONNECT_FAILED")

    monkeypatch.setattr(sources, "connect_database", offline)
    with pytest.raises(HTTPException) as down:
        nl2sql.execute_plan(plan)
    assert down.value.status_code == 503

    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            if statement.startswith("SELECT "):
                raise RuntimeError("query failed")

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Broken()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sources, "connect_database", lambda **kwargs: Connection())
    with pytest.raises(HTTPException) as failed:
        nl2sql.execute_plan(plan)
    assert failed.value.status_code == 503


def test_run_nl_query_degrades_when_the_generated_sql_disagrees(sql_env, monkeypatch):
    from fastapi import HTTPException

    from app.llm import nl2sql, readers

    def fake(first, last, scope, user, series):
        return {"unit": "人次", "source": "stub_mart", "batch_date": None, "build_run_id": None,
                "null_reason": None, "chart_type": "number",
                "points": [{"start_date": first.isoformat(), "end_date": last.isoformat(), "value": 9}]}

    monkeypatch.setitem(readers.READERS, "MOPS-OUT-01", fake)
    monkeypatch.setattr(nl2sql, "execute_plan", lambda plan, **kwargs: [(202502, 3, 0)])
    with pytest.raises(HTTPException) as degraded:
        nl2sql.run_nl_query("2025-02 全院门诊人次", _Account())
    assert degraded.value.status_code == 503


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
            execute_schema(cursor, root / name)
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
        marts.rerun((JAN, LAST), themes=("inpatient", "income"), **args)

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

        cases = load_cases(root / "backend/app/llm/eval/evalset.json")
        result = evaluate(client, cases, headers={"admin": admin, "department": department},
                          rows=qa_seed_rows())
        assert result["correct"] == result["total"] == 50 and result["failures"] == []
        assert result["themes"]["门诊"]["correct"] == result["themes"]["门诊"]["total"]

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
            for table in (marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT,
                          *aggregate_themes.TABLES.values(), aggregate_themes.STATUS, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            cursor.execute("DELETE FROM auth_data_access WHERE actor_id IN (SELECT user_id FROM auth_users "
                           "WHERE username IN (%s,%s,%s))", tuple(names.values()))
            db.commit()
        if report.exists():
            report.unlink()
