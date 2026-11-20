"""Isolated MySQL check of the inaugural outpatient mart and bounded backfill."""
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from etl.config import source_config as sources
from etl.jobs import aggregate_dws as mart, build_dims, build_dwd
from etl.scheduler import scheduler
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


DAY = "2025-01-01"
FIRST_DEPT = "SIM_DEPT_01"
SECOND_DEPT = "SIM_DEPT_02"
DWS, ADS = "dws_dept_daily", "ads_outpatient_theme"


@pytest.mark.parametrize("function,args", [
    ("rerun", ("2025-01-01",)),
    ("rerun", (("2025-01-03", "2025-01-01"),)),
    ("rerun", (("2025-1-01", "2025-01-03"),)),
    ("build_dws_daily", (" ", DAY)),
    ("build_dws_daily", (None, None)),
    ("build_theme_marts", ("unknown",)),
])
def test_bad_request_before_connection(function, args, monkeypatch):
    def unexpected_connection():
        pytest.fail("invalid arguments reached the database")

    monkeypatch.setattr(mart, "connect_database", unexpected_connection)
    with pytest.raises(sources.OdsError, match="N9_ARGUMENTS_INVALID"):
        getattr(mart, function)(*args)


def read_rows(dataset):
    with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
        result = {}
        for table in (DWS, ADS):
            cursor.execute(f"SELECT * FROM {table} WHERE dataset_id=%s ORDER BY stat_date, dept_code", (dataset,))
            result[table] = tuple(cursor.fetchall())
        return result


def oracle(dataset):
    # Aggregate independently from visit rows, without reusing the job's GROUP BY.
    with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT visit_date, dept_code, source_total_fee FROM dwd_visit_detail "
            "WHERE dataset_id=%s AND visit_type='OUTPATIENT'", (dataset,),
        )
        buckets = {}
        for day, dept, fee in cursor.fetchall():
            buckets.setdefault((day, dept), []).append(fee)
        return {
            key: (len(fees), sum(fee is not None for fee in fees),
                  sum(fees, Decimal("0.00")) if None not in fees else None,
                  "INCOMPLETE_INPUT" if None in fees else None)
            for key, fees in buckets.items()
        }


def test_mysql_outpatient_mart_and_bounded_backfill(mysql_pipeline, monkeypatch):
    dataset, batch, selection, _ = mysql_pipeline
    root = sources.PROJECT_ROOT
    other = "N9_OTHER_" + uuid4().hex
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    report_path = root / "backend/etl/logs/test" / build_dwd._report_name(batch)
    assert not report_path.exists()
    with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
        for relative in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql"):
            cursor.execute((root / relative).read_text())
        assert read_rows(dataset) == {DWS: (), ADS: ()}
        assert read_rows(other) == {DWS: (), ADS: ()}

    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()
    try:
        for name, info in selection.items():
            path = Path(info["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            rows = sim_csv_rows(name)
            if name == "his_outpatient":
                rows.extend([
                    dict(rows[0], visit_id="SIM_EXTRA_SAME_DEPT", fee_amount="4.75"),
                    dict(rows[0], visit_id="SIM_EXTRA_OTHER_DEPT", dept_code=SECOND_DEPT, fee_amount=""),
                ])
            if name == "his_charge":
                rows.append(dict(rows[0], charge_id="SIM_EXTRA_CHARGE", amount="1.25"))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(rows)

        process = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=90,
        )
        assert process.returncode == 0, "Isolated full-build failure; raw output withheld"
        identity = build_dwd.verify_current_build(**args)
        run_id = identity["build_run_id"]
        expected = oracle(dataset)
        assert len(expected) == 4
        assert expected[(date.fromisoformat(DAY), FIRST_DEPT)] == (2, 2, Decimal("15.00"), None)
        assert expected[(date.fromisoformat(DAY), SECOND_DEPT)] == (1, 0, None, "INCOMPLETE_INPUT")
        with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM dwd_charge_detail WHERE dataset_id=%s AND fee_date=%s",
                           (dataset, DAY))
            assert cursor.fetchone()[0] == 2
            for table in (DWS, ADS):
                extra = ", ads_status" if table == DWS else ""
                value = ", 'PASS'" if table == DWS else ""
                cursor.execute(
                    f"INSERT INTO {table} (dataset_id, stat_date, dept_code, batch_date, source_build_run_id, "
                    f"outpatient_visits, outpatient_fee_samples, outpatient_total_fee, fee_status{extra}) "
                    f"VALUES (%s,%s,%s,%s,%s,1,1,0.00,NULL{value})",
                    (other, DAY, FIRST_DEPT, batch, run_id),
                )
            connection.commit()
        untouched = read_rows(other)
        whole = (DAY, "2025-01-04")
        def assert_current(run, scope=expected):
            output = read_rows(dataset)
            for table, rows in output.items():
                assert {(row[1], row[2]): row[5:9] for row in rows if row[4] == run} == scope
                if table == DWS:
                    assert all(row[9] == "PASS" for row in rows if row[4] == run)
            return output

        assert mart.rerun(whole, **args)["row_counts"] == {DWS: 4, ADS: 4}
        baseline = assert_current(run_id)
        assert mart.rerun(whole, **args)["status"] == "PASS"
        assert read_rows(dataset) == baseline

        pending = mart.build_dws_daily(FIRST_DEPT, DAY, **args)
        assert pending["ads_status"] == "PENDING" and pending["row_counts"] == {DWS: 1, ADS: 0}
        staged = read_rows(dataset)
        scope_key = (date.fromisoformat(DAY), FIRST_DEPT)
        assert [row[9] for row in staged[DWS] if row[1:3] == scope_key] == ["PENDING"]
        for table in (DWS, ADS):
            assert [row for row in staged[table] if row[1:3] != scope_key] == [
                row for row in baseline[table] if row[1:3] != scope_key
            ]
        assert len(staged[ADS]) == 3
        assert mart.build_theme_marts("outpatient", **args)["status"] == "PASS"
        assert read_rows(dataset) == baseline

        assert mart.rerun(("2025-01-04", "2025-01-04"), **args)["row_counts"] == {DWS: 0, ADS: 0}
        with pytest.raises(sources.OdsError, match="N9_RANGE_OUTSIDE_BUILD"):
            mart.rerun(("2025-01-01", "2025-01-05"), **args)
        with pytest.raises(sources.OdsError, match="N9_UNKNOWN_DEPARTMENT"):
            mart.build_dws_daily("UNKNOWN", DAY, **args)
        assert read_rows(dataset) == baseline

        with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE {DWS} SET outpatient_visits=9 WHERE dataset_id=%s AND stat_date=%s AND dept_code=%s",
                           (dataset, DAY, FIRST_DEPT))
            connection.commit()
        before_failure = read_rows(dataset)
        original = mart._replace_rows
        def fail_ads(cursor, table, where, bound, rows):
            if table == ADS:
                raise sources.OdsError("N9_RECONCILE_FAILED")
            original(cursor, table, where, bound, rows)

        with monkeypatch.context() as isolated:
            isolated.setattr(mart, "_replace_rows", fail_ads)
            with pytest.raises(sources.OdsError, match="N9_RECONCILE_FAILED"):
                mart.rerun((DAY, DAY), **args)
        assert read_rows(dataset) == before_failure
        mart.rerun((DAY, DAY), **args)
        assert assert_current(run_id) == baseline
        assert read_rows(other) == untouched
        assert oracle(dataset) == expected
        assert build_dwd.verify_current_build(**args) == identity
        with scheduler._lock("test", "run"):
            with pytest.raises(sources.OdsError, match="N9_RUN_LOCKED"):
                mart.rerun((DAY, DAY), **args)
        assert read_rows(dataset) == baseline

        withheld = report_path.with_suffix(".withheld")
        assert not withheld.exists()
        report_path.rename(withheld)
        try:
            with pytest.raises(sources.OdsError, match="N9_SOURCE_STALE"):
                mart.rerun((DAY, DAY), **args)
            assert read_rows(dataset) == baseline
        finally:
            withheld.rename(report_path)

        second = build_dwd.run_build_all(**args)["build_run_id"]
        assert second != run_id
        with pytest.raises(sources.OdsError, match="N9_DWS_NOT_CURRENT"):
            mart.build_theme_marts("outpatient", **args)
        mart.rerun((DAY, DAY), **args)
        replaced = assert_current(second, {key: row for key, row in expected.items() if key[0].isoformat() == DAY})
        for table in (DWS, ADS):
            assert [row for row in replaced[table] if row[1].isoformat() != DAY] == [
                row for row in baseline[table] if row[1].isoformat() != DAY
            ]
        mart.rerun(whole, **args)
        assert_current(second)
        assert read_rows(other) == untouched
        assert oracle(dataset) == expected
        known_fee = sum((row[2] for row in expected.values() if row[2] is not None), Decimal("0.00"))
        print(f"N9_ORACLE_OK keys={len(expected)} visits={sum(row[0] for row in expected.values())} "
              f"valid_fees={sum(row[1] for row in expected.values())} known_fee={known_fee:.2f} "
              f"null_keys={sum(row[2] is None for row in expected.values())}")
    finally:
        with sources.connect_database(test_only=True) as connection, connection.cursor() as cursor:
            for table in (DWS, ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                for owner in ((dataset, other) if table in (DWS, ADS) else (dataset,)):
                    cursor.execute(f"DELETE FROM {table} WHERE dataset_id=%s", (owner,))
            connection.commit()
        if report_path.exists():
            report_path.unlink()
