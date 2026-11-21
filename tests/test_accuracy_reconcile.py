from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


INDICATORS = (
    "MOPS-OUT-01", "MOPS-OUT-02", "MOPS-INP-01", "MOPS-INP-02", "MOPS-INP-06", "MOPS-DRUG-02", "MOPS-CONS-02",
)
CHARGE_TOTALS = {
    "source_charge_total": ("income", "charge_amount"),
    "source_drug_charge_total": ("drug", "drug_charge_amount"),
    "source_consumable_charge_total": ("consumable", "consumable_charge_amount"),
}


def reconcile(expected, published):
    differences = [
        {"key": key, "expected": expected.get(key), "published": published.get(key)}
        for key in sorted(expected.keys() | published.keys())
        if key not in expected or key not in published or expected[key] != published[key]
    ]
    return {"compared": len(expected.keys() | published.keys()), "differences": differences,
            "passed": bool(expected or published) and not differences}


def reconcile_outpatient(cursor, dataset_id, source_batch, build_id, start_date, end_date):
    cursor.execute(
        "SELECT visit_date, dept_code, COUNT(*) FROM dwd_visit_detail "
        "WHERE dataset_id=%s AND visit_type='OUTPATIENT' AND visit_date BETWEEN %s AND %s "
        "GROUP BY visit_date, dept_code",
        (dataset_id, start_date, end_date),
    )
    expected = {(day, department): count for day, department, count in cursor.fetchall()}
    cursor.execute(
        "SELECT stat_date, dept_code, outpatient_visits FROM ads_outpatient_theme "
        "WHERE dataset_id=%s AND batch_date=%s AND source_build_run_id=%s AND stat_date BETWEEN %s AND %s",
        (dataset_id, source_batch, build_id, start_date, end_date),
    )
    published = {(day, department): count for day, department, count in cursor.fetchall()}
    return reconcile(expected, published)


def _source_metrics(cursor, dataset, prior, first, last):
    cursor.execute(
        "SELECT COUNT(CASE WHEN visit_date >= %s THEN 1 END), COUNT(CASE WHEN visit_date < %s THEN 1 END) "
        "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_type='OUTPATIENT' AND visit_date BETWEEN %s AND %s",
        (first, first, dataset, prior, last),
    )
    current, previous = cursor.fetchone()
    cursor.execute(
        "SELECT COUNT(*), IF(COUNT(*)=COUNT(source_stay_days), AVG(CAST(source_stay_days AS DECIMAL(28,8))), NULL), "
        "IF(COUNT(*)=COUNT(source_total_fee), AVG(CAST(source_total_fee AS DECIMAL(28,8))), NULL) "
        "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_type='INPATIENT' AND discharge_date BETWEEN %s AND %s",
        (dataset, first, last),
    )
    discharges, stay, cost = cursor.fetchone()
    cursor.execute(
        "SELECT IF(COUNT(*)=COUNT(c.amount), SUM(c.amount), NULL), "
        "IF(COUNT(*)=COUNT(c.amount) AND MIN(COALESCE(f.source_supported,0))=1, "
        "SUM(CASE WHEN c.fee_type_code='DRUG' THEN c.amount ELSE 0 END), NULL), "
        "IF(COUNT(*)=COUNT(c.amount) AND MIN(COALESCE(f.source_supported,0))=1, "
        "SUM(CASE WHEN c.fee_type_code='CONSUMABLE' THEN c.amount ELSE 0 END), NULL) "
        "FROM dwd_charge_detail c LEFT JOIN dim_fee_type f ON c.dataset_id=f.dataset_id "
        "AND c.fee_type_code=f.fee_type_code WHERE c.dataset_id=%s AND c.fee_date BETWEEN %s AND %s",
        (dataset, first, last),
    )
    total, drug, consumable = cursor.fetchone()
    values = (
        current, _percent(current - previous, previous), discharges, stay, cost,
        _percent(drug, total), _percent(consumable, total), total, drug, consumable,
    )
    return {key: None if value is None else Decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            for key, value in zip((*INDICATORS, *CHARGE_TOTALS), values)}


def _percent(numerator, base):
    return None if numerator is None or not base else Decimal(numerator) * 100 / base


def _published_metrics(cursor, dataset, batch, build_id, first, last):
    where = "dataset_id=%s AND batch_date=%s AND source_build_run_id=%s AND stat_date BETWEEN %s AND %s"
    keys = (dataset, batch, build_id, first, last)
    placeholders = ",".join("%s" for _ in INDICATORS)
    cursor.execute(
        "SELECT indicator_code, metric_value FROM ads_indicator_result "
        "WHERE dataset_id=%s AND source_batch=%s AND source_build_run_id=%s AND stat_date=%s "
        "AND granularity='month' AND dimension_key='ALL' AND calculation_status='OK' "
        f"AND indicator_code IN ({placeholders})",
        (*keys[:4], *INDICATORS),
    )
    actual = dict(cursor.fetchall())
    cursor.execute(f"SELECT theme, COUNT(*), SUM(rows_published) FROM ads_theme_status WHERE {where} GROUP BY theme", keys)
    publication = {theme: (days, rows) for theme, days, rows in cursor.fetchall()}
    for name, (theme, column) in CHARGE_TOTALS.items():
        cursor.execute(
            f"SELECT COUNT(*), COUNT({column}), "
            f"SUM(COALESCE({column}, 0)) FROM ads_{theme}_theme WHERE {where}",
            keys,
        )
        rows, available, amount = cursor.fetchone()
        if rows and (last.day, rows) == publication.get(theme):
            if theme != "income":
                cursor.execute(
                    f"SELECT COUNT(*) FROM ads_{theme}_theme WHERE {where} "
                    f"AND {column} IS NULL AND {theme}_status='NO_RECORDS'", keys,
                )
                available += cursor.fetchone()[0]
            if available == rows:
                actual[name] = amount
    return actual


def reconcile_month(cursor, dataset, batch, build_id, month):
    first = date.fromisoformat(month)
    if first.day != 1 or first.isoformat() != month or first.year < 2:
        raise ValueError("Use the first date of the month as YYYY-MM-DD")
    last = first.replace(day=monthrange(first.year, first.month)[1])
    prior = (first - timedelta(days=1)).replace(day=1)
    source = _source_metrics(cursor, dataset, prior, first, last)
    published = _published_metrics(cursor, dataset, batch, build_id, first, last)
    result = reconcile(source, published)
    unavailable = sorted(key for key, value in source.items() if value is None or published.get(key) is None)
    errors = [abs(source[key] - published[key]) for key in source if key not in unavailable]
    return {**result, "available": len(errors), "unavailable": unavailable,
            "max_abs_error": max(errors, default=None), "passed": result["passed"] and not unavailable}


def test_reconciliation_identifies_changed_and_missing_keys():
    expected = {("2025-01-01", "SIM_DEPT_01"): 2, ("2025-01-02", "SIM_DEPT_01"): 1}
    assert reconcile(expected, expected)["passed"]
    report = reconcile(expected, {("2025-01-01", "SIM_DEPT_01"): 1})
    assert report["compared"] == 2
    assert [item["published"] for item in report["differences"]] == [1, None]
    assert not report["passed"]
    assert not reconcile({}, {})["passed"]
    assert not reconcile({"missing": None}, {})["passed"]
