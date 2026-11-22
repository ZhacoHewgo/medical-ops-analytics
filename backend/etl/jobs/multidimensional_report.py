from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from etl.config.source_config import OdsError
from etl.jobs.dashboard_details import _scope


ReportMetric = Literal["outpatient_visits", "discharges", "billing_income", "charge_amount", "ledger_income"]
ReportGroup = Literal["date", "department", "date_department"]
ReportGrain = Literal["day", "week", "month"]
METRIC_LABELS = {
    "outpatient_visits": "门诊人次",
    "discharges": "出院人次",
    "billing_income": "账单收入",
    "charge_amount": "收费明细金额",
    "ledger_income": "财务台账收入",
}
AMOUNTS = frozenset(("billing_income", "charge_amount", "ledger_income"))
LEDGER = "ledger_income"
EXCEL_NUMERIC_LIMIT = Decimal("10000000000000")


def _add(left, right):
    return None if left is None or right is None else left + right


def _format(values):
    return {key: f"{value:.2f}" if value is not None and key in AMOUNTS else value for key, value in values.items()}


def _visits(cursor, dataset, first, last, department, daily):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT service_date,dept_code,visit_type,COUNT(*),COUNT(source_total_fee),SUM(source_total_fee) "
        "FROM (SELECT dept_code,visit_type,source_total_fee,"
        "CASE WHEN visit_type='OUTPATIENT' THEN visit_date ELSE discharge_date END AS service_date "
        f"FROM dwd_visit_detail v WHERE {scope} AND visit_type IN ('OUTPATIENT','INPATIENT')) visits "
        "WHERE service_date BETWEEN %s AND %s GROUP BY service_date,dept_code,visit_type",
        params,
    )
    for day, dept, kind, count, priced, amount in cursor.fetchall():
        row = daily[day, dept]
        row["outpatient_visits" if kind == "OUTPATIENT" else "discharges"] = count
        row["billing_income"] = _add(row.get("billing_income", 0), amount if count == priced else None)


def _charges(cursor, dataset, first, last, department, daily):
    scope, params = _scope(dataset, first, last, department, "c")
    cursor.execute(
        "SELECT c.fee_date,c.dept_code,COUNT(*),COUNT(c.amount),SUM(c.amount) FROM dwd_charge_detail c "
        f"WHERE {scope} AND c.fee_date BETWEEN %s AND %s GROUP BY c.fee_date,c.dept_code",
        params,
    )
    for day, dept, count, priced, amount in cursor.fetchall():
        daily[day, dept]["charge_amount"] = amount if count == priced else None


def _ledger(cursor, dataset, batch, first, last, department, daily):
    scope = "AND dept_code=%s" if department is not None else ""
    params = (department,) if department is not None else ()
    batch_clause = "AND etl_batch_date=%s" if batch is not None else ""
    batch_params = (batch,) if batch is not None else ()
    cursor.execute(
        "WITH entries AS (SELECT stat_date,dept_code,record_id,revenue,"
        "COUNT(*) OVER (PARTITION BY record_id) AS occurrences FROM ods_finance_excel "
        f"WHERE dataset_id=%s {batch_clause}), amounts AS (SELECT stat_date,dept_code,"
        "CASE WHEN occurrences=1 AND TRIM(record_id)<>'' "
        "AND REGEXP_LIKE(revenue,'^-?[0-9]{1,16}([.][0-9]{1,2})?$') "
        "THEN CAST(revenue AS DECIMAL(18,2)) END AS amount FROM entries "
        f"WHERE stat_date BETWEEN %s AND %s {scope}) "
        "SELECT stat_date,dept_code,COUNT(*),COUNT(amount),SUM(amount) FROM amounts "
        "GROUP BY stat_date,dept_code",
        (dataset, *batch_params, first.isoformat(), last.isoformat(), *params),
    )
    invalid_days = set()
    for raw_date, dept, count, priced, amount in cursor.fetchall():
        try:
            day = date.fromisoformat(raw_date)
            if day.isoformat() != raw_date:
                raise ValueError
        except ValueError:
            for row in daily.values():
                row.pop(LEDGER, None)
            return
        if (day, dept) not in daily:
            invalid_days.add(day)
            continue
        daily[day, dept][LEDGER] = amount if count == priced else None
    if invalid_days:
        for (day, _), row in daily.items():
            if day in invalid_days:
                row.pop(LEDGER, None)


def daily_metrics(cursor, dataset, batch, first, last, department, metrics, *, row_limit=10000):
    if department is None:
        cursor.execute("SELECT dept_code FROM dim_department WHERE dataset_id=%s ORDER BY dept_code", (dataset,))
        departments = [row[0] for row in cursor.fetchall()]
    else:
        departments = [department]
    days = (last - first).days + 1
    if row_limit is not None and days * len(departments) > row_limit:
        raise OdsError("N10_REPORT_TOO_LARGE")
    daily = {(first + timedelta(days=offset), dept): {} for offset in range(days) for dept in departments}
    if {"outpatient_visits", "discharges", "billing_income"}.intersection(metrics):
        _visits(cursor, dataset, first, last, department, daily)
    if "charge_amount" in metrics:
        _charges(cursor, dataset, first, last, department, daily)
    if LEDGER in metrics:
        _ledger(cursor, dataset, batch, first, last, department, daily)
    return daily


def _period(day, grain):
    if grain == "week":
        year, week, _ = day.isocalendar()
        return f"{year:04d}-W{week:02d}"
    return day.isoformat()[:7] if grain == "month" else day.isoformat()


def group_report(daily, metrics, group_by, grain):
    totals = {metric: None if not daily and metric == LEDGER else 0 for metric in metrics}
    grouped = {}
    for (day, dept), row in daily.items():
        key = (None if group_by == "department" else _period(day, grain), None if group_by == "date" else dept)
        target = grouped.setdefault(key, dict.fromkeys(metrics, 0))
        for metric in metrics:
            value = row.get(metric, None if metric == LEDGER else 0)
            target[metric] = _add(target[metric], value)
            totals[metric] = _add(totals[metric], value)
    return {
        "totals": _format(totals),
        "rows": [{"period": period, "department": dept, "values": _format(row)}
                 for (period, dept), row in sorted(grouped.items())],
    }


def income_totals(cursor, dataset, batch, first, last, department):
    metrics = ("billing_income", LEDGER)
    totals = group_report(daily_metrics(cursor, dataset, batch, first, last, department, metrics, row_limit=None),
                          metrics, "department", "day")["totals"]
    return [{"name": key, "label": METRIC_LABELS[key], "count": 0, "value": value, "group": None}
            for key, value in totals.items()]


def summarize_report(start, end, department, *, dataset_id, batch_date, build_run_id, env, metrics, group_by, grain):
    from etl.jobs.ads_indicators import _report, _run, _summary_scope

    def calculate(cursor, span, day_grain, dataset, batch, environment, *, write):
        dataset, batch, report, first, last, context = _summary_scope(
            cursor, span, day_grain, dataset, batch, environment, department=department,
            build_run_id=build_run_id, source="multidimensional_report",
        )
        daily = daily_metrics(cursor, dataset, batch, first, last, department, metrics)
        result = group_report(daily, metrics, group_by, grain)
        if _report(environment, batch.isoformat()) != report:
            raise OdsError("N10_SOURCE_STALE")
        return {**context, **result, "metrics": metrics, "group_by": group_by, "grain": grain}

    return _run(start, end, "day", dataset_id, batch_date, env, write=False,
                open_dates=True, calculation=calculate)


def excel_rows(rows, metrics, group_by):
    dates = group_by != "department"
    departments = group_by != "date"
    headings = (["期间"] if dates else []) + (["科室"] if departments else []) + [
        METRIC_LABELS[key] + ("（元）" if key in AMOUNTS else "") for key in metrics
    ]
    for_row = []
    for row in rows:
        inputs = ([row["period"]] if dates else []) + ([row["department"]] if departments else [])
        for key in metrics:
            value = row["values"][key]
            if value is not None and key in AMOUNTS and abs(Decimal(value)) < EXCEL_NUMERIC_LIMIT:
                value = Decimal(value)
            inputs.append(value)
        for_row.append(inputs)
    return headings, for_row
