import csv
from datetime import date
from decimal import Decimal
import subprocess
import sys

import pytest

from etl.config import source_config as sources
from etl.jobs import ads_indicators as indicators, aggregate_dws as marts, build_dims, build_dwd
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


JAN, FEB, MAR, END = "2025-01-01", "2025-02-01", "2025-03-01", "2025-03-31"
VOLUME, GROWTH = "MOPS-OUT-01", "MOPS-OUT-02"
RESULT = "ads_indicator_result"


@pytest.mark.parametrize("start,end,granularity", [
    ("2025-1-01", END, "day"), (JAN, "2024-12-31", "day"),
    (JAN, END, "year"), ("2025-01-02", END, "month"),
    (JAN, "2025-03-30", "month"),
])
def test_invalid_range_rejects_before_connect(start, end, granularity, monkeypatch):
    def unreachable():
        pytest.fail("invalid input tried to connect")
    monkeypatch.setattr(indicators, "connect_database", unreachable)
    with pytest.raises(sources.OdsError, match="N10_ARGUMENTS_INVALID"):
        indicators.run_indicators(start, end, granularity)


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
        rows[-1].update(discharge_date=END, stay_days="58")
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
            cursor.execute((root / name).read_text())
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
        print("N10_ORACLE_OK jan=2 feb=3 mar=0 mom_feb=50.000000 mom_mar=-100.000000")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            for table in (RESULT, marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (dataset,))
            db.commit()
        if report_path.exists():
            report_path.unlink()
