import csv
from datetime import date
from decimal import Decimal
import subprocess
import sys

import pandas as pd
import pytest

from sql_fixture import execute_schema

from etl.config import source_config as sources
from etl.jobs import ads_indicators as indicators, aggregate_dws as marts, build_dims, build_dwd
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows
from tests.test_accuracy_reconcile import reconcile_month


JAN, FEB, MAR, END = "2025-01-01", "2025-02-01", "2025-03-01", "2025-03-31"
VOLUME, GROWTH = "MOPS-OUT-01", "MOPS-OUT-02"
RESULT = "ads_indicator_result"


@pytest.mark.parametrize("start,end,granularity,code", [
    ("2025-1-01", END, "day", "N10_DATE_INVALID"),
    (JAN, "2024-12-31", "day", "N10_RANGE_INVALID"),
    (JAN, END, "year", "N10_GRANULARITY_INVALID"),
    (JAN, END, None, "N10_GRANULARITY_INVALID"),
    ("2025-01-02", END, "month", "N10_RANGE_INVALID"),
    (JAN, "2025-03-30", "month", "N10_RANGE_INVALID"),
])
def test_invalid_range_rejects_before_connect(start, end, granularity, code, monkeypatch):
    def unreachable():
        pytest.fail("invalid input tried to connect")
    monkeypatch.setattr(indicators, "connect_database", unreachable)
    with pytest.raises(sources.OdsError, match=code):
        indicators.run_indicators(start, end, granularity)


def test_backfill_accepts_course_positional_forms(monkeypatch):
    seen = []

    def capture(start, end, grain, dataset, batch, env, **kwargs):
        seen.append((start, end, grain))
        return [], {"status": "PASS"}

    monkeypatch.setattr(indicators, "_run", capture)
    assert indicators.rerun((JAN, "2025-01-31"), "month")["status"] == "PASS"
    assert seen[-1] == (JAN, "2025-01-31", "month")
    assert indicators.rerun(JAN, "2025-01-31", "month")["status"] == "PASS"
    assert seen[-1] == (JAN, "2025-01-31", "month")
    assert indicators.rerun((JAN, "2025-01-31"))["status"] == "PASS"
    assert seen[-1] == (JAN, "2025-01-31", "day")
    with pytest.raises(sources.OdsError, match="N10_RANGE_INVALID"):
        indicators.rerun((JAN,))
    with pytest.raises(sources.OdsError, match="N10_RANGE_INVALID"):
        indicators.rerun(JAN, "month")
    with pytest.raises(sources.OdsError, match="N10_ARGUMENTS_INVALID"):
        indicators.rerun(JAN, "2025-01-31", "month", "day")


def test_metrics_and_drill_get_distinct_safe_codes(monkeypatch):
    def unreachable():
        pytest.fail("invalid selector reached MySQL")
    monkeypatch.setattr(indicators, "connect_database", unreachable)
    with pytest.raises(sources.OdsError, match="N10_METRIC_INVALID"):
        indicators.run_indicators(JAN, END, "day", metrics=("NOPE",))
    with pytest.raises(sources.OdsError, match="N10_METRIC_INVALID"):
        indicators.run_indicators(JAN, END, "day", metrics=(indicators.DISCHARGES, indicators.DISCHARGES))
    with pytest.raises(sources.OdsError, match="N10_DRILL_INVALID"):
        indicators.summarize_theme("drg", JAN, END, None, dataset_id="DS",
                                   batch_date="2025-01-31", build_run_id="run", drill="dept")
    with pytest.raises(sources.OdsError, match="N10_DEPARTMENT_INVALID"):
        indicators.summarize_theme("drg", JAN, END, {"dept": "SIM_DEPT_01"}, dataset_id="DS",
                                   batch_date="2025-01-31", build_run_id="run")


def test_backfill_accepts_date_objects_and_keeps_explicit_batch(monkeypatch):
    seen = {}

    def capture(start, end, grain, dataset, batch, env, **kwargs):
        seen.update(start=start, end=end, grain=grain, dataset=dataset, batch=batch,
                    calculation=kwargs.get("calculation"))
        return [], {"status": "PASS"}

    monkeypatch.setattr(indicators, "_run", capture)
    assert indicators.rerun((date(2025, 1, 1), date(2025, 1, 31)), "month",
                            dataset_id="DS", batch_date="2025-01-31")["status"] == "PASS"
    assert seen == {"start": date(2025, 1, 1), "end": date(2025, 1, 31), "grain": "month",
                    "dataset": "DS", "batch": "2025-01-31", "calculation": None}
    indicators.rerun((date(2025, 1, 1), date(2025, 1, 31)), "month", dataset_id="DS",
                     batch_date="2025-01-31", metrics=(indicators.DISCHARGES,))
    assert seen["calculation"].keywords["metrics"] == (indicators.DISCHARGES,)


def test_backfill_reports_missing_database_config_not_bad_arguments(monkeypatch):
    monkeypatch.delenv("ODS_DATABASE_URL", raising=False)
    monkeypatch.delenv("ODS_TEST_DATABASE_URL", raising=False)
    with pytest.raises(sources.OdsError, match="ODS_DATABASE_URL_MISSING"):
        indicators.rerun((JAN, "2025-01-31"), "month")
    with pytest.raises(sources.OdsError, match="ODS_DATABASE_URL_MISSING"):
        indicators.run_indicators(JAN, "2025-01-31", "day")


def test_backfill_env_database_mismatch_is_reported_distinctly(monkeypatch):
    monkeypatch.setenv("ODS_DATABASE_URL", "mysql://SIM:SIM@127.0.0.1:13306/hospital_test")
    with pytest.raises(sources.OdsError, match="N10_ENV_DATABASE_MISMATCH"):
        indicators.rerun((JAN, "2025-01-31"), "month", env="dev")


def test_summary_entry_rejects_bad_dates_and_accepts_course_theme_names(monkeypatch):
    def unreachable():
        pytest.fail("invalid summary input reached MySQL")

    monkeypatch.setattr(indicators, "connect_database", unreachable)
    with pytest.raises(sources.OdsError, match="N10_DATE_INVALID"):
        indicators.summarize_inpatient("2025-1-01", None, None, dataset_id="DS",
                                       batch_date="2025-01-31", build_run_id="run")
    with pytest.raises(sources.OdsError, match="N10_THEME_INVALID"):
        indicators.summarize_theme("unknown", None, None, None, dataset_id="DS",
                                   batch_date="2025-01-31", build_run_id="run")
    calls = {}

    def capture(*args, **kwargs):
        calls.update(kwargs)
        return {"data": None}

    monkeypatch.setattr(indicators, "_run", capture)
    indicators.summarize_theme("病种", None, None, None, dataset_id="DS",
                               batch_date="2025-01-31", build_run_id="run")
    assert calls["calculation"].keywords["theme"] == "drg"
    indicators.summarize_theme("resource", None, None, None, dataset_id="DS",
                               batch_date="2025-01-31", build_run_id="run")
    assert calls["calculation"].keywords["theme"] == "efficiency"


def test_dataframe_keeps_exact_integer_values_and_missing_reason(monkeypatch):
    row = ("test", VOLUME, "ALL", "day", date.fromisoformat(JAN), date.fromisoformat(END),
           "synthetic-run", "outpatient-v1", "人次")
    large = 2 ** 53 + 1
    values = [(*row, large, None, large, "OK", None),
              (*row, None, None, None, "NULL", "ADS_NOT_PUBLISHED")]
    monkeypatch.setattr(indicators, "_run", lambda *args, **kwargs: (values, {}))
    result = indicators.calc_outpatient_volume(JAN, JAN, "day")
    assert result["metric_value"].tolist() == [large, None]
    assert result["null_reason"].tolist() == [None, "ADS_NOT_PUBLISHED"]


def test_course_calculators_default_to_daily_range(monkeypatch):
    calls = []

    def capture(start, end, grain, *args, **kwargs):
        calls.append((start, end, grain))
        return ([], {}) if kwargs.get("calculation") is None else []

    monkeypatch.setattr(indicators, "_run", capture)
    for calculate in (indicators.calc_outpatient_volume, indicators.calc_bed_usage_rate):
        assert calculate(JAN).empty
        assert calculate(JAN, FEB).empty
    assert calls == [(JAN, JAN, "day"), (JAN, FEB, "day")] * 2


def test_direct_indicator_calculations_use_supplied_input_without_database(monkeypatch):
    def unreachable(*args, **kwargs):
        pytest.fail("direct calculation reached the database")

    monkeypatch.setattr(indicators, "_run", unreachable)
    visits = pd.DataFrame({"visit_id": ["A", "A", "B", "C"],
                           "visit_type": ["OUTPATIENT", "OUTPATIENT", "门诊", "INPATIENT"]})
    assert indicators.calc_outpatient_volume(visits) == 2
    assert indicators.calc_outpatient_volume(pd.DataFrame({"outpatient_visits": [12, 8]})) == 20
    assert indicators.calc_outpatient_volume(pd.DataFrame({"outpatient_visits": [12, None]})) is None
    assert indicators.calc_outpatient_volume(visits.iloc[:0]) is None
    beds = pd.DataFrame({"occupied_bed_days": [1, 9], "available_bed_days": [2, 10]})
    assert indicators.calc_bed_usage_rate(beds) == pytest.approx(83.333333)
    assert indicators.calc_bed_usage_rate(45, 60) == 75.0
    assert indicators.calc_bed_usage_rate(0, 60) == 0.0
    assert indicators.calc_bed_usage_rate(0, 0) is None
    assert indicators.calc_bed_usage_rate(45, None) is None
    with pytest.raises(sources.OdsError, match="N10_MEASURE_INVALID"):
        indicators.calc_bed_usage_rate(-1, 60)


def test_indicator_source_keeps_privacy_config_errors_distinct(monkeypatch):
    def verify(*args, **kwargs):
        raise sources.OdsError("DWD_PRIVACY_KEY_MISSING")

    monkeypatch.setattr(build_dwd, "_lock_inputs", lambda *args: ("SIM", JAN, None))
    monkeypatch.setattr(indicators, "_report", lambda *args: {})
    monkeypatch.setattr(build_dwd, "verify_build_freshness_from_cursor", verify)
    with pytest.raises(sources.OdsError, match="^DWD_PRIVACY_KEY_MISSING$"):
        indicators._source(None, "SIM", JAN, "dev")


def test_backfill_reports_requested_and_certified_dates_without_writing(monkeypatch):
    from unittest.mock import MagicMock

    db = MagicMock()
    monkeypatch.setattr(indicators, "connect_database", lambda: db)
    report = {"effective_start": "2025-04-01", "effective_end": "2025-06-30"}
    monkeypatch.setattr(indicators, "_source", lambda *args: ("SIM", date(2025, 6, 30), report, "run"))
    span = (date(2025, 1, 1), date(2025, 1, 3))
    with pytest.raises(sources.OdsError) as raised:
        indicators._transaction(span, "day", "SIM", None, "dev", write=True)
    assert raised.value.code == "N10_RANGE_OUTSIDE_BUILD"
    assert str(raised.value) == ("N10_RANGE_OUTSIDE_BUILD: requested=2025-01-01..2025-01-03, "
                                 "available=2025-04-01..2025-06-30")
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_rerun_disjoint_range_returns_same_no_data_result_without_writes(monkeypatch):
    from unittest.mock import MagicMock

    db = MagicMock()
    monkeypatch.setattr(indicators, "connect_database", lambda: db)
    monkeypatch.setattr(indicators, "_scope", lambda *args: None)
    monkeypatch.setattr(indicators.scheduler, "_lock", lambda *args: MagicMock())
    report = {"effective_start": "2025-04-01", "effective_end": "2025-06-30"}
    monkeypatch.setattr(indicators, "_source", lambda *args: ("SIM", date(2025, 6, 30), report, "run"))

    first = indicators.rerun(("2024-01-01", "2024-01-02"))
    assert first == indicators.rerun(("2024-01-01", "2024-01-02"))
    assert first == {
        "status": "NO_DATA", "code": "N10_RANGE_OUTSIDE_BUILD", "rows": 0, "granularity": "day",
        "start_date": "2024-01-01", "end_date": "2024-01-02",
        "available_start_date": "2025-04-01", "available_end_date": "2025-06-30",
    }
    assert db.rollback.call_count == 2
    db.commit.assert_not_called()
    assert db.cursor.return_value.__enter__.return_value.execute.call_count == 4
    with pytest.raises(sources.OdsError, match="N10_RANGE_OUTSIDE_BUILD"):
        indicators.rerun(("2025-03-31", "2025-04-01"))


def seed_rows(name):
    rows = sim_csv_rows(name)
    dates = (JAN, "2025-01-31", FEB)
    for i, row in enumerate(rows):
        for key in ("visit_date", "admit_date", "fee_date", "report_date", "stat_date", "discharge_date"):
            if key in row:
                row[key] = dates[i]
        if "stay_days" in row:
            row["stay_days"] = "0"
    if name == "his_inpatient":
        rows[1].update(discharge_date="2025-02-04", stay_days="4")
        rows[-1].update(discharge_date=END, stay_days="58")
    if name == "his_charge":
        rows.extend([
            dict(rows[2], charge_id="SIM_FEB_DRUG", fee_type_code="DRUG", amount="18.75"),
            dict(rows[2], charge_id="SIM_FEB_CONSUMABLE", fee_type_code="CONSUMABLE", amount="5.25"),
        ])
    if name == "his_outpatient":
        rows.extend([
            dict(rows[2], visit_id="SIM_OUT_EXTRA_1"),
            dict(rows[2], visit_id="SIM_OUT_EXTRA_2", dept_code="SIM_DEPT_02"),
        ])
    return rows


def published(dataset):
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute(
            "SELECT indicator_code, dimension_key, granularity, stat_date, source_build_run_id, "
            "numerator, denominator, metric_value, null_reason FROM ads_indicator_result "
            "WHERE dataset_id=%s ORDER BY indicator_code, dimension_key, granularity, stat_date",
            (dataset,),
        )
        return tuple(cursor.fetchall())


def test_mysql_outpatient_indicators_and_bounded_rerun(mysql_pipeline, monkeypatch):
    dataset, batch, selection, report = mysql_pipeline
    root = sources.PROJECT_ROOT
    report_path = report.parent / build_dwd._report_name(batch)
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        for name in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/ads/indicator_tables.sql"):
            execute_schema(cursor, root / name)
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()
    assert not published(dataset)
    try:
        from pathlib import Path
        for name, info in selection.items():
            path = Path(info["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                csv_rows = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                csv_rows.writeheader()
                csv_rows.writerows(seed_rows(name))
        complete = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=90,
        )
        assert complete.returncode == 0, "Isolated build failure; raw output withheld"
        identity = build_dwd.verify_current_build(**args)
        current = identity["build_run_id"]

        def keyed():
            return {(code, scope, grain, day.isoformat()): (run, numerator, denominator, value, reason)
                    for code, scope, grain, day, run, numerator, denominator, value, reason in published(dataset)}

        def at(code, day, scope="ALL", grain="month"):
            return keyed()[(code, scope, grain, day)]

        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT EXTRACT(YEAR_MONTH FROM visit_date), COUNT(DISTINCT visit_id) "
                "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_type='OUTPATIENT' "
                "GROUP BY EXTRACT(YEAR_MONTH FROM visit_date)", (dataset,),
            )
            oracle = dict(cursor.fetchall())
        assert oracle == {202501: 2, 202502: 3}

        marts.rerun((JAN, "2025-01-31"), **args)
        indicators.run_indicators(JAN, END, "month", **args)
        assert at(VOLUME, JAN)[3:] == (Decimal("2"), None)
        assert at(VOLUME, FEB)[3:] == (None, "ADS_NOT_PUBLISHED")
        assert at(GROWTH, FEB)[3:] == (None, "ADS_NOT_PUBLISHED")
        assert at(VOLUME, MAR)[3:] == (Decimal("0"), None)

        marts.rerun((FEB, END), **args)
        indicators.rerun((FEB, END), "month", **args)
        assert at(VOLUME, FEB)[3:] == (Decimal(oracle[202502]), None)
        assert at(GROWTH, JAN)[3:] == (None, "NO_COMPARABLE_PRIOR")
        assert at(GROWTH, FEB) == (current, 1, 2, Decimal("50"), None)
        assert at(GROWTH, MAR) == (current, -3, 3, Decimal("-100"), None)
        assert at(GROWTH, FEB, "DEPT:SIM_DEPT_02")[3:] == (None, "ZERO_BASE")
        # A row in only one publication table must not turn an empty day into a certified zero.
        for table, department, extra in (
            (marts.ADS, "SIM_DEPT_01", ""),
            (marts.DWS, "SIM_DEPT_02", ", ads_status"),
        ):
            with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {table} ({', '.join(marts.COLUMNS)}{extra}) "
                    "SELECT dataset_id, %s, dept_code, batch_date, source_build_run_id, "
                    f"outpatient_visits, outpatient_fee_samples, outpatient_total_fee, fee_status{extra} "
                    f"FROM {table} WHERE dataset_id=%s AND stat_date=%s AND dept_code=%s",
                    (END, dataset, FEB, department),
                )
                assert cursor.rowcount == 1
                db.commit()
            try:
                empty = indicators.calc_outpatient_volume(END, END, "day", **args).set_index("dimension_key")
                for scope in ("ALL", f"DEPT:{department}"):
                    actual = empty.loc[scope, ["metric_value", "null_reason"]].tolist()
                    assert actual == [None, "ADS_NOT_PUBLISHED"], f"{table} orphan must remain NULL"
            finally:
                with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE dataset_id=%s AND stat_date=%s AND dept_code=%s",
                        (dataset, END, department),
                    )
                    db.commit()

        baseline = published(dataset)
        indicators.rerun((FEB, END), "month", **args)
        assert published(dataset) == baseline

        daily = indicators.calc_outpatient_volume(FEB, FEB, "day", **args)
        assert daily.loc[daily["dimension_key"]=="ALL","metric_value"].iloc[0] == Decimal("3")
        assert published(dataset) == baseline
        indicators.run_indicators(FEB, FEB, "day", **args)
        assert at(VOLUME, FEB, grain="day")[3:] == (Decimal("3"), None)
        daily_baseline = published(dataset)

        original = indicators._write_rows
        def fail_after_write(cursor, where, params, rows):
            original(cursor, where, params, rows)
            raise sources.OdsError("N10_RECONCILE_FAILED")

        marts.build_dws_daily("SIM_DEPT_01", FEB, **args)
        with monkeypatch.context() as patches:
            patches.setattr(indicators, "_write_rows", fail_after_write)
            with pytest.raises(sources.OdsError, match="N10_RECONCILE_FAILED"):
                indicators.rerun((FEB, "2025-02-28"), "month", **args)
        assert published(dataset) == daily_baseline
        indicators.rerun((FEB, "2025-02-28"), "month", **args)
        assert at(VOLUME, FEB)[3:] == (None, "ADS_NOT_PUBLISHED")
        assert at(VOLUME, FEB, "DEPT:SIM_DEPT_02")[3:] == (Decimal("1"), None)
        assert [row for row in published(dataset) if row[2]!="month" or row[3]!=date.fromisoformat(FEB)] == [
            row for row in daily_baseline if row[2]!="month" or row[3]!=date.fromisoformat(FEB)
        ]

        preserved = published(dataset)
        missing = report_path.with_suffix(".withheld")
        assert not missing.exists()
        report_path.rename(missing)
        try:
            with pytest.raises(sources.OdsError, match="N10_SOURCE_STALE"):
                indicators.rerun((FEB, "2025-02-28"), "month", **args)
        finally:
            missing.rename(report_path)
        assert published(dataset) == preserved
        assert build_dwd.verify_current_build(**args) == identity

        latest = build_dwd.run_build_all(**args)["build_run_id"]
        assert latest != current
        marts.rerun((FEB, "2025-02-28"), **args)
        indicators.rerun((FEB, "2025-02-28"), "month", **args)
        assert at(VOLUME, FEB) == (latest, 3, None, Decimal("3"), None)
        assert at(GROWTH, FEB) == (latest, None, None, None, "NO_COMPARABLE_PRIOR")
        assert at(VOLUME, JAN)[0] == current
        assert at(VOLUME, FEB, grain="day")[0] == current
        feb_end = "2025-02-28"
        marts.rerun((JAN, feb_end), **args, themes=("inpatient", "income", "drug", "consumable"))
        indicators.run_indicators(
            FEB, feb_end, "month", **args,
            metrics=(indicators.DISCHARGES, indicators.STAY, indicators.COST, indicators.DRUG, indicators.CONSUMABLE),
        )
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            result = reconcile_month(cursor, dataset, batch, latest, FEB)
            assert result == {
                "compared": 10, "differences": [], "available": 10, "unavailable": [],
                "max_abs_error": Decimal("0"), "passed": True,
            }
            stale = reconcile_month(cursor, dataset, batch, current, FEB)
            assert not stale["passed"] and stale["available"] == 0
            cursor.execute(
                "UPDATE ads_indicator_result SET metric_value=metric_value+1 WHERE dataset_id=%s "
                "AND indicator_code=%s AND dimension_key='ALL' AND granularity='month' AND stat_date=%s",
                (dataset, indicators.CONSUMABLE, FEB),
            )
            assert cursor.rowcount == 1
            mismatched = reconcile_month(cursor, dataset, batch, latest, FEB)
            assert not mismatched["passed"] and mismatched["max_abs_error"] == Decimal("1")
            assert [row["key"] for row in mismatched["differences"]] == [indicators.CONSUMABLE]
            db.rollback()
        print("N19_MONTHLY_RECONCILIATION_OK metrics=10 max_absolute_error=0 stale_and_corruption_rejected")
        print("N10_ORACLE_OK jan=2 feb=3 mar=0 mom_feb=50.000000 mom_mar=-100.000000")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (RESULT, marts.DWS, marts.ADS, *marts.aggregate_themes.COLUMNS,
                          marts.aggregate_themes.STATUS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            db.commit()
        if report_path.exists():
            report_path.unlink()
