from pathlib import Path
import re


def test_application_health(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_canonical_ads_schema():
    sql = (Path(__file__).resolve().parents[1] / "sql/ads/ads_tables.sql").read_text()
    matches = re.findall(r"CREATE TABLE IF NOT EXISTS (ads_\w+_theme) \(", sql)
    assert len(matches) == len(set(matches)) == 8


def test_trial_counts_terminal_tasks_and_keeps_unmeasured_reconciliation(monkeypatch):
    from etl.jobs import trial_check

    events = [
        {"at": "2026-11-19T16:00:00+00:00", "run_id": "sim-a", "name": "extract_ods", "status": "START"},
        {"at": "2026-11-19T16:00:01+00:00", "run_id": "sim-a", "name": "extract_ods", "status": "SUCCESS"},
        {"at": "2026-11-19T16:00:02+00:00", "run_id": "sim-b", "name": "extract_ods", "status": "FAILED"},
        {"at": "2026-11-20T16:00:00+00:00", "run_id": "sim-c", "name": "extract_ods", "status": "SUCCESS"},
    ]
    monkeypatch.setattr(trial_check, "task_events", lambda env, limit: iter(events))
    result = trial_check.collect_trial_metrics("2026-11-20", "2026-11-20", env="test")
    assert result["task_status_counts"] == {"SUCCESS": 1, "FAILED": 1}
    assert result["success_rate"] == 0.5
    assert result["reconciliation_measured"] is False


def test_monthly_comparison_checks_actual_calendar_periods():
    from app.services.indicator_service import compare_yoy_mom

    january = compare_yoy_mom({"2024-01": 4, "2024-12": 0, "2025-01": 6})["2025-01"]
    assert january == {
        "mom": None, "mom_reason": "ZERO_BASE", "yoy": "50.00", "yoy_reason": None,
    }
