from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api.routers.multidimensional_router import router
from app.core.deps import get_current_user
from app.services import theme_service
from app.services.export_service import export_excel
from etl.jobs.multidimensional_report import _ledger, excel_rows, group_report


def test_revenues_remain_independent_across_week_and_department():
    first, second = date(2025, 6, 29), date(2025, 6, 30)
    daily = {
        (first, "HN"): {"billing_income": Decimal("15.22"), "charge_amount": Decimal("18.01"),
                        "ledger_income": Decimal("20.30"), "discharges": 1},
        (second, "HN"): {"billing_income": Decimal("8.12"), "charge_amount": Decimal("-3.00"),
                         "ledger_income": None, "discharges": 1},
        (second, "HX"): {"billing_income": Decimal("10.00"), "charge_amount": Decimal("12.00"),
                         "ledger_income": Decimal("15.00"), "outpatient_visits": 3},
    }
    metrics = ["billing_income", "charge_amount", "ledger_income", "outpatient_visits", "discharges"]
    result = group_report(daily, metrics, "date_department", "week")
    assert [(row["period"], row["department"]) for row in result["rows"]] == [
        ("2025-W26", "HN"), ("2025-W27", "HN"), ("2025-W27", "HX"),
    ]
    assert result["totals"] == {
        "billing_income": "33.34", "charge_amount": "27.01", "ledger_income": None,
        "outpatient_visits": 3, "discharges": 2,
    }
    monthly = group_report(daily, ["billing_income", "ledger_income"], "date", "month")
    assert monthly["rows"] == [
        {"period": "2025-06", "department": None, "values": {"billing_income": "33.34", "ledger_income": None}},
    ]


def test_excel_preserves_multiple_numeric_columns_and_literal_text():
    headings, rows = excel_rows(
        [{"department": "=literal", "values": {
            "billing_income": "15.22", "charge_amount": "-3.50", "ledger_income": "12345678901234.05",
        }}],
        ["billing_income", "charge_amount", "ledger_income"],
        "department",
    )
    file = export_excel(headings, rows, (), sheet_name="多维报表")
    workbook = load_workbook(BytesIO(file), read_only=True, data_only=False)
    cells = list(workbook["多维报表"].iter_rows(min_row=2))[0]
    assert [(cell.value, cell.data_type) for cell in cells] == [
        ("=literal", "s"), (15.22, "n"), (-3.5, "n"), ("12345678901234.05", "s"),
    ]
    workbook.close()


def test_ledger_invalid_rows_do_not_affect_other_measures():
    first, second = date(2025, 6, 29), date(2025, 6, 30)
    daily = {(first, "HN"): {"billing_income": Decimal("7.00")}, (second, "HN"): {}}

    class Cursor:
        def execute(self, sql, params):
            assert "OVER (PARTITION BY record_id)" in sql
            assert params == ("SIM", second, first.isoformat(), second.isoformat())

        def fetchall(self):
            return [
                (first.isoformat(), "HN", 1, 1, Decimal("2.10")),
                (first.isoformat(), "UNKNOWN", 1, 1, Decimal("8.20")),
                (second.isoformat(), "HN", 2, 1, Decimal("2.10")),
            ]

    _ledger(Cursor(), "SIM", second, first, second, None, daily)
    result = group_report(daily, ["billing_income", "ledger_income"], "date", "day")
    assert result["totals"] == {"billing_income": "7.00", "ledger_income": None}
    assert all(row["values"]["ledger_income"] is None for row in result["rows"])


def test_query_and_export_reject_department_scope_widening(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="department", department="HN")
    audit = []
    monkeypatch.setattr(theme_service, "audit_access",
                        lambda user, action, department, status, **kwargs: audit.append((action, department, status)))
    path = "/api/v1/reports/multidimensional"
    with TestClient(app) as client:
        assert client.get(path, params={"metrics": "ledger_income", "dept_code": "HX"}).status_code == 403
        assert client.post(path + "/export", json={
            "metrics": ["ledger_income"], "dept_code": "HX", "report_id": "a" * 64,
        }).status_code == 403
    assert audit == [("query", "HN", 403), ("export", "HN", 403)]
