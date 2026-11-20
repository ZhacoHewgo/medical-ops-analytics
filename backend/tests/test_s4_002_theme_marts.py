import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from etl.config import source_config as sources
from etl.jobs import aggregate_dws as marts, aggregate_themes as themes, build_dims, build_dwd
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


DAY, SECOND, THIRD, LAST = ("2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04")
DEPT, OTHER = "SIM_DEPT_01", "SIM_DEPT_02"
DB_TABLES = (*themes.COLUMNS, themes.STATUS)


@pytest.mark.parametrize("value", ["income", ("unknown",), ("drug", "drug"), ("outpatient",)])
def test_invalid_theme_selection_fails_before_database(value, monkeypatch):
    def no_connection():
        pytest.fail("invalid selection reached MySQL")
    monkeypatch.setattr(marts, "connect_database", no_connection)
    with pytest.raises(sources.OdsError, match="N9_ARGUMENTS_INVALID"):
        marts.rerun((DAY, LAST), themes=value)


def read_tables(dataset):
    with sources.connect_database(test_only=True) as db, db.cursor() as cur:
        result = {}
        for table in DB_TABLES:
            order = "theme,stat_date" if table == themes.STATUS else "stat_date,dept_code"
            cur.execute(f"SELECT * FROM {table} WHERE dataset_id=%s ORDER BY {order}", (dataset,))
            columns = [column[0] for column in cur.description]
            result[table] = tuple(dict(zip(columns, row)) for row in cur.fetchall())
        return result


def test_mysql_other_themes_certification_and_backfill(mysql_pipeline, monkeypatch):
    dataset, batch, selection, quality = mysql_pipeline
    root = sources.PROJECT_ROOT
    report = quality.parent / build_dwd._report_name(batch)
    args = dict(dataset_id=dataset, batch_date=batch, env="test")
    isolated = "THEME_TEST_" + uuid4().hex
    with sources.connect_database(test_only=True) as db, db.cursor() as cur:
        for name in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/ads/theme_tables.sql"):
            for statement in (root / name).read_text().split(";"):
                if statement.strip():
                    cur.execute(statement)
        assert all(not rows for rows in read_tables(dataset).values())
        assert all(not rows for rows in read_tables(isolated).values())
    assert build_dims.initialize_dim_schema() and build_dwd.initialize_schema()
    try:
        for name, spec in selection.items():
            path = Path(spec["file_path"])
            assert path.resolve().is_relative_to(sources.GENERATED_ROOT / dataset)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = sim_csv_rows(name)
            if name == "his_charge":
                rows[0].update(dept_code=OTHER, fee_type_code="DRUG", amount="20.00")
                rows.append(dict(rows[0], charge_id="SIM_EXTRA_DRUG", amount="-1.25"))
                rows.append(dict(rows[0], charge_id="SIM_EXTRA_CONSUMABLE", fee_type_code="CONSUMABLE", amount="5.25"))
                rows.append(dict(rows[1], charge_id="SIM_MISSING_DRUG_AMOUNT", fee_type_code="DRUG", amount=""))
            with path.open("x", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=sources.SOURCES[name]["columns"])
                writer.writeheader()
                writer.writerows(rows)
        process = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", "test", "--through", "dwd"],
            cwd=root / "backend", capture_output=True, timeout=90,
        )
        assert process.returncode == 0, "Isolated full-build failure; raw output withheld"
        identity = build_dwd.verify_current_build(**args)
        run = identity["build_run_id"]
        single = marts.build_theme_marts("inpatient", **args)
        assert single["row_counts"]["ads_inpatient_theme"] == 3 and "ads_status" not in single
        assert not read_tables(dataset)["ads_income_theme"]

        all_themes = tuple(themes.TABLES)
        result = marts.rerun((DAY, LAST), themes=all_themes, **args)
        assert result["source_build_run_id"] == run
        assert set(result["theme_states"]) == set(all_themes)
        assert result["row_counts"][themes.STATUS] == len(all_themes) * 4
        baseline = read_tables(dataset)

        def group(table, day, dept):
            return next(row for row in baseline[table] if row["stat_date"].isoformat()==day and row["dept_code"]==dept)

        with sources.connect_database(test_only=True) as db, db.cursor() as cur:
            cur.execute("SELECT discharge_date,dept_code,source_stay_days,source_total_fee FROM dwd_visit_detail "
                        "WHERE dataset_id=%s AND visit_type='INPATIENT' AND discharge_date IS NOT NULL", (dataset,))
            inpatient = cur.fetchall()
            cur.execute("SELECT fee_date,dept_code,fee_type_code,amount FROM dwd_charge_detail WHERE dataset_id=%s",
                        (dataset,))
            charges = cur.fetchall()
            cur.execute(
                "INSERT INTO ads_income_theme (dataset_id,stat_date,dept_code,batch_date,source_build_run_id,"
                "charge_count,amount_samples,charge_amount,net_revenue_reason) "
                "VALUES (%s,%s,%s,%s,%s,1,1,7.50,'UNCONFIRMED_RULE')", (isolated, DAY, DEPT, batch, run),
            )
            db.commit()
        untouched = read_tables(isolated)
        discharges = {}
        for day, dept, stay, fee in inpatient:
            discharges.setdefault((day, dept), []).append((stay, fee))
        assert {(r["stat_date"], r["dept_code"]) for r in baseline["ads_inpatient_theme"]} == discharges.keys()
        for (day, dept), values in discharges.items():
            row = group("ads_inpatient_theme", day.isoformat(), dept)
            assert (row["discharges"], row["discharge_stay_days"], row["discharge_total_fee"]) == (
                len(values), sum(v[0] for v in values), sum(v[1] for v in values),
            )
        assert all(r["stat_date"].isoformat()!=DAY for r in baseline["ads_inpatient_theme"])

        charge_groups = {}
        for day, dept, fee_type, amount in charges:
            charge_groups.setdefault((day, dept), []).append((fee_type, amount))
        for table in ("ads_income_theme", "ads_drug_theme", "ads_consumable_theme"):
            assert {(r["stat_date"], r["dept_code"]) for r in baseline[table]} == charge_groups.keys()
        for (day, dept), values in charge_groups.items():
            amounts = [amount for _, amount in values]
            total = sum(amounts) if None not in amounts else None
            income = group("ads_income_theme", day.isoformat(), dept)
            assert (income["charge_count"], income["charge_amount"], income["net_revenue"]) == (len(values), total, None)
            assert income["amount_samples"] == sum(amount is not None for amount in amounts)
            assert income["charge_status"] == (None if total is not None else "INCOMPLETE_INPUT")
            for kind in ("drug", "consumable"):
                row = group(f"ads_{kind}_theme", day.isoformat(), dept)
                by_kind = [amount for code, amount in values if code==kind.upper()]
                assert row["charge_amount"] == total and row[f"{kind}_count"] == len(by_kind)
                expected = sum(by_kind) if by_kind and None not in by_kind else None
                assert row[f"{kind}_charge_amount"] == expected
                reason = "NO_RECORDS" if not by_kind else "INCOMPLETE_INPUT" if None in by_kind else None
                assert row[f"{kind}_status"] == reason
        assert group("ads_income_theme", DAY, OTHER)["charge_amount"] == Decimal("24.00")
        assert group("ads_drug_theme", DAY, OTHER)["drug_charge_amount"] == Decimal("18.75")
        assert group("ads_consumable_theme", DAY, OTHER)["consumable_charge_amount"] == Decimal("5.25")
        disease = group("ads_drg_theme", SECOND, DEPT)
        assert (disease["discharges"], disease["source_diagnosis_cases"], disease["drg_grouped_cases"]) == (1, 1, None)
        resources = group("ads_efficiency_theme", SECOND, DEPT)
        assert (resources["discharges"], resources["available_bed_days"], resources["occupied_bed_days"]) == (1, None, None)
        service = group("ads_service_theme", DAY, DEPT)
        assert (service["outpatient_visits"], service["inpatient_admissions"], service["emr_link_rate"]) == (1, 1, None)
        assert any(row["theme"]=="drg" and row["data_status"]=="HOLD" for row in baseline[themes.STATUS])
        assert any(row["theme"]=="inpatient" and row["stat_date"].isoformat()==DAY
                   and row["rows_published"]==0 and row["data_status"]=="READY" for row in baseline[themes.STATUS])

        marts.rerun((DAY, LAST), themes=all_themes, **args)
        assert read_tables(dataset)==baseline and read_tables(isolated)==untouched
        with sources.connect_database(test_only=True) as db, db.cursor() as cur:
            cur.execute("UPDATE ads_income_theme SET charge_amount=999 WHERE dataset_id=%s AND stat_date=%s", (dataset, DAY))
            db.commit()
        modified = read_tables(dataset)
        original = themes._replace

        def fail_after_write(cursor, table, columns, where, keys, rows):
            original(cursor, table, columns, where, keys, rows)
            if table=="ads_drug_theme":
                raise sources.OdsError("N9_RECONCILE_FAILED")

        with monkeypatch.context() as patched:
            patched.setattr(themes, "_replace", fail_after_write)
            with pytest.raises(sources.OdsError, match="N9_RECONCILE_FAILED"):
                marts.rerun((DAY, DAY), themes=("income","drug"), **args)
        assert read_tables(dataset)==modified
        marts.rerun((DAY, DAY), themes=("income","drug"), **args)
        assert read_tables(dataset)==baseline
        assert read_tables(isolated)==untouched

        missing = report.with_suffix(".withheld")
        assert not missing.exists()
        report.rename(missing)
        try:
            with pytest.raises(sources.OdsError, match="N9_SOURCE_STALE"):
                marts.rerun((DAY, DAY), themes=all_themes, **args)
        finally:
            missing.rename(report)
        assert read_tables(dataset)==baseline
        assert build_dwd.verify_current_build(**args)==identity

        next_run = build_dwd.run_build_all(**args)["build_run_id"]
        assert next_run != run
        marts.rerun((DAY, DAY), themes=("income",), **args)
        replaced = read_tables(dataset)
        assert [row["source_build_run_id"] for row in replaced["ads_income_theme"]
                if row["stat_date"].isoformat()==DAY] == [next_run]
        assert all(row["source_build_run_id"]==run for row in replaced["ads_income_theme"]
                   if row["stat_date"].isoformat()!=DAY)
        assert replaced["ads_drug_theme"] == baseline["ads_drug_theme"]
        assert [row["source_build_run_id"] for row in replaced[themes.STATUS]
                if row["theme"]=="income" and row["stat_date"].isoformat()==DAY] == [next_run]
        print("N9B_ORACLE_OK themes=7 inpatient=3 separated_charges=24.00 drug=18.75 consumable=5.25")
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cur:
            for table in (*DB_TABLES, marts.DWS, marts.ADS, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cur.execute(f"DELETE FROM {table} WHERE dataset_id IN (%s,%s)", (dataset, isolated))
            db.commit()
        if report.exists():
            report.unlink()
