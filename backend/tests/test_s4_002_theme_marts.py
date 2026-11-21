import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from sql_fixture import execute_schema

from etl.config import source_config as sources
from etl.jobs import ads_indicators as indicators, aggregate_dws as marts, aggregate_themes as themes, build_dims, build_dwd
from test_s2_003_pipeline import mysql_pipeline, sim_csv_rows


DAY, SECOND, THIRD, LAST = ("2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04")
DEPT, OTHER = "SIM_DEPT_01", "SIM_DEPT_02"
DB_TABLES = (*themes.COLUMNS, themes.STATUS)


@pytest.mark.parametrize("value,code", [
    ("income", "N9_ARGUMENTS_INVALID"),
    (("unknown",), "N9_THEME_INVALID"),
    (("drug", "drug"), "N9_THEME_INVALID"),
    (("outpatient",), "N9_THEME_INVALID"),
    (("",), "N9_THEME_INVALID"),
])
def test_invalid_theme_selection_fails_before_database(value, code, monkeypatch):
    def no_connection():
        pytest.fail("invalid selection reached MySQL")
    monkeypatch.setattr(marts, "connect_database", no_connection)
    with pytest.raises(sources.OdsError, match=code):
        marts.rerun((DAY, LAST), themes=value)


THEME_CALLS = [
    ("outpatient", None),
    *((name, theme) for name, theme in themes.COURSE_NAMES.items() if theme != themes.OUTPATIENT),
    ("disease", "drg"),
    ("resource", "efficiency"),
    *((table, theme) for theme, table in themes.TABLES.items()),
]


@pytest.mark.parametrize("name,theme", THEME_CALLS)
def test_theme_names_match_course_and_table_names(name, theme, monkeypatch):
    calls = []

    def capture(span, dept, step, dataset_id, batch_date, env, *, themes=()):
        calls.append((step, themes))
        return {"status": "PASS"}

    monkeypatch.setattr(marts, "_run", capture)
    assert marts.build_theme_marts(name)["status"] == "PASS"
    assert calls[-1] == ("theme" if theme is None else "themes_only", () if theme is None else (theme,))
    assert theme is None or themes.TABLES[theme] in themes.COLUMNS


def test_selection_normalizes_course_names_and_rejects_collisions():
    assert themes.validate_selection(("病种", "drug")) == ("drg", "drug")
    assert themes.validate_selection(("ads_efficiency_theme",)) == ("efficiency",)
    assert themes.validate_selection((" disease ",)) == ("drg",)
    for selection in (("病种", "drg"), ("disease", "drg"), ("门诊",), ("患者服务", "service")):
        with pytest.raises(sources.OdsError, match="N9_THEME_INVALID"):
            themes.validate_selection(selection)


def test_theme_marts_report_missing_database_config(monkeypatch):
    monkeypatch.delenv("ODS_DATABASE_URL", raising=False)
    monkeypatch.delenv("ODS_TEST_DATABASE_URL", raising=False)
    with pytest.raises(sources.OdsError, match="ODS_DATABASE_URL_MISSING"):
        marts.build_theme_marts("病种")


def test_disease_and_resource_reuse_the_course_physical_tables():
    import re

    script = (sources.PROJECT_ROOT / "sql/ads/ads_tables.sql").read_text(encoding="utf-8")
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", script))
    course = {*themes.TABLES.values(), "ads_outpatient_theme", themes.STATUS}
    assert declared == course
    assert set(themes.COLUMNS) | {"ads_outpatient_theme"} == declared - {themes.STATUS}
    assert themes.canonical("disease") == "drg" and themes.TABLES["drg"] == "ads_drg_theme"
    assert themes.canonical("resource") == "efficiency" and themes.TABLES["efficiency"] == "ads_efficiency_theme"
    assert re.search(r"CREATE TABLE IF NOT EXISTS ads_drg_theme \([^;]+COMMENT='disease\b", script)
    assert re.search(r"CREATE TABLE IF NOT EXISTS ads_efficiency_theme \([^;]+COMMENT='resource\b", script)


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
        for name in ("sql/dws/dws_tables.sql", "sql/ads/ads_tables.sql", "sql/ads/indicator_tables.sql"):
            execute_schema(cur, root / name)
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

        def indicator(calc, start, dimension="ALL", *, end=None, grain="day"):
            frame = calc(start, end or start, grain, **args)
            return frame.loc[frame.dimension_key == dimension].iloc[0]

        for calc, field in ((indicators.calc_avg_stay_days, 2), (indicators.calc_avg_cost, 3)):
            records = [row[field] for row in inpatient if row[0].isoformat() == SECOND]
            result = indicator(calc, SECOND)
            assert result.metric_value == (Decimal(sum(records)) / len(records)).quantize(Decimal("0.000001"))
            assert result.denominator == len(records)
            assert indicator(calc, DAY).null_reason == "ZERO_BASE"
            assert indicator(calc, DAY, end="2025-01-31", grain="month").null_reason == "INCOMPLETE_PERIOD"
        discharge = indicator(indicators.calc_inpatient_discharge, SECOND)
        assert discharge.metric_value == sum(row[0].isoformat() == SECOND for row in inpatient)
        assert indicator(indicators.calc_inpatient_discharge, DAY).metric_value == 0
        consumable = indicator(indicators.calc_consumable_ratio, DAY, f"DEPT:{OTHER}")
        assert (consumable.numerator, consumable.denominator, consumable.metric_value) == (
            Decimal("5.25"), Decimal("24.00"), Decimal("21.875000"),
        )

        selected_metrics = (indicators.DISCHARGES, indicators.CONSUMABLE, indicators.BED)
        assert indicators.run_indicators(DAY, LAST, "day", **args, metrics=selected_metrics)["status"] == "PASS"
        assert indicators.rerun((DAY, LAST), "day", **args, metrics=selected_metrics)["status"] == "PASS"
        with sources.connect_database(test_only=True) as db, db.cursor() as cur:
            cur.execute(
                "SELECT indicator_code,metric_value,null_reason FROM ads_indicator_result "
                "WHERE dataset_id=%s AND dimension_key=%s AND stat_date=%s", (dataset, f"DEPT:{OTHER}", DAY),
            )
            stored = {code: (value, reason) for code, value, reason in cur.fetchall()}
        assert stored[indicators.CONSUMABLE] == (Decimal("21.875000"), None)
        assert stored[indicators.DISCHARGES] == (Decimal("0"), None)
        assert stored[indicators.BED] == (None, "BED_CAPACITY_MISSING")

        drug = indicator(indicators.calc_drug_ratio, DAY, f"DEPT:{OTHER}")
        assert (drug.numerator, drug.denominator, drug.metric_value, drug.definition_version) == (
            Decimal("18.75"), Decimal("24.00"), Decimal("78.125000"), "sim-charge-v1",
        )
        missing_day = next(day.isoformat() for day, _, fee_type, amount in charges if fee_type == "DRUG" and amount is None)
        assert indicator(indicators.calc_drug_ratio, missing_day).null_reason == "INCOMPLETE_INPUT"
        for calc, reason in (
            (indicators.calc_bed_usage_rate, "BED_CAPACITY_MISSING"),
            (indicators.calc_cmi, "DRG_GROUPING_AND_WEIGHTS_MISSING"),
            (indicators.calc_drg_group_rate, "OFFICIAL_DRG_GROUPING_MISSING"),
        ):
            item = indicator(calc, SECOND)
            assert item.metric_value is None and item.null_reason == reason
        with sources.connect_database(test_only=True) as db, db.cursor() as cur:
            cur.execute("UPDATE ads_inpatient_theme SET discharge_stay_days=999 "
                        "WHERE dataset_id=%s AND stat_date=%s", (dataset, SECOND))
            db.commit()
        with pytest.raises(sources.OdsError, match="N10_ADS_NOT_PUBLISHED"):
            indicators.calc_avg_stay_days(SECOND, SECOND, "day", **args)

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
            for table in (*DB_TABLES, marts.DWS, marts.ADS, indicators.TABLE, build_dwd.VISIT, build_dwd.AUDIT, *build_dims._SPECS):
                cur.execute(f"DELETE FROM {table} WHERE dataset_id IN (%s,%s)", (dataset, isolated))
            db.commit()
        if report.exists():
            report.unlink()
