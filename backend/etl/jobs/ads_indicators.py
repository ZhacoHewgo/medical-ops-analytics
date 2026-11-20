"""门诊人次与自然月环比，以认证门诊集市为输入。"""
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from etl.config.source_config import OdsError, connect_database, validate_batch
from etl.jobs import build_dwd as build, clean_dwd as clean
from etl.jobs.aggregate_dws import _report
from etl.scheduler import scheduler


VOLUME = "MOPS-OUT-01"
GROWTH = "MOPS-OUT-02"
DEFINITION = "outpatient-v1"
TABLE = "ads_indicator_result"
COLUMNS = ("dataset_id", "indicator_code", "dimension_key", "granularity", "stat_date",
           "source_batch", "source_build_run_id", "definition_version", "unit", "numerator",
           "denominator", "metric_value", "calculation_status", "null_reason")
SAFE_ERRORS = {
    "N10_ARGUMENTS_INVALID", "N10_SOURCE_STALE", "N10_RANGE_OUTSIDE_BUILD",
    "N10_MIGRATION_REQUIRED", "N10_RECONCILE_FAILED", "N10_ROLLBACK_FAILED", "N10_COMMIT_UNKNOWN",
}


def calc_outpatient_volume(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    import pandas as pd

    rows, _ = _run(start_date, end_date, granularity, dataset_id, batch_date, env, write=False)
    return pd.DataFrame(rows, columns=COLUMNS, dtype=object)


def run_indicators(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    _, summary = _run(start_date, end_date, granularity, dataset_id, batch_date, env, write=True)
    return summary


def rerun(date_range, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    if not isinstance(date_range, (tuple, list)) or len(date_range) != 2:
        raise OdsError("N10_ARGUMENTS_INVALID")
    return run_indicators(*date_range, granularity, dataset_id=dataset_id, batch_date=batch_date, env=env)


def _month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _range(start, end, grain):
    try:
        first, last = (date.fromisoformat(validate_batch(value)) for value in (start, end))
        if (grain not in ("day", "month") or first > last
                or grain == "month" and (first.day != 1 or last != _month_end(last))):
            raise ValueError
    except (OdsError, ValueError):
        raise OdsError("N10_ARGUMENTS_INVALID") from None
    return first, last


def _run(start, end, grain, dataset_id, batch_date, env, *, write):
    span = _range(start, end, grain)
    try:
        build._validate_public(dataset_id, batch_date, env)
    except OdsError as error:
        code = "N10_RUN_LOCKED" if error.code == "N8_RUN_CONTEXT_ACTIVE" else "N10_ARGUMENTS_INVALID"
        raise OdsError(code) from None
    try:
        with scheduler._lock(env, "run"):
            return _transaction(span, grain, dataset_id, batch_date, env, write=write)
    except scheduler.EtlError:
        raise OdsError("N10_RUN_LOCKED") from None


def _transaction(span, grain, dataset_id, batch_date, env, *, write):
    connection = connect_database()
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION")
                result = _calculate(cursor, span, grain, dataset_id, batch_date, env, write=write)
        except Exception as error:
            try:
                connection.rollback()
            except Exception:
                raise OdsError("N10_ROLLBACK_FAILED") from None
            if isinstance(error, OdsError) and error.code in SAFE_ERRORS:
                raise OdsError(error.code) from None
            raise OdsError("N10_TRANSACTION_FAILED") from None
        try:
            connection.commit() if write else connection.rollback()
        except Exception:
            raise OdsError("N10_COMMIT_UNKNOWN" if write else "N10_ROLLBACK_FAILED") from None
        return result
    finally:
        clean._close(connection)


def _source(cursor, dataset_id, batch_date, env):
    try:
        dataset, batch, _ = build._lock_inputs(cursor, dataset_id)
        report = _report(env, batch)
        identity = build.verify_build_freshness_from_cursor(
            cursor, dataset_id=dataset, batch_date=batch, env=env, report=report,
        )
    except OdsError:
        raise OdsError("N10_SOURCE_STALE") from None
    if batch_date is not None and batch_date != batch:
        raise OdsError("N10_SOURCE_STALE")
    return dataset, date.fromisoformat(batch), report, identity["build_run_id"]


def _read_counts(cursor, dataset, batch, run, first, last):
    cursor.execute(
        "SELECT visit_date,dept_code,COUNT(*) FROM dwd_visit_detail WHERE dataset_id=%s "
        "AND visit_type='OUTPATIENT' AND visit_date BETWEEN %s AND %s GROUP BY visit_date,dept_code",
        (dataset, first, last),
    )
    expected = {(day, dept): count for day, dept, count in cursor.fetchall()}
    current = (dataset, batch, run, first, last)
    cursor.execute(
        "SELECT stat_date,dept_code,outpatient_visits,ads_status FROM dws_dept_daily "
        "WHERE dataset_id=%s AND batch_date=%s AND source_build_run_id=%s AND stat_date BETWEEN %s AND %s",
        current,
    )
    dws = {(day, dept): (count, status) for day, dept, count, status in cursor.fetchall()}
    cursor.execute(
        "SELECT stat_date,dept_code,outpatient_visits FROM ads_outpatient_theme "
        "WHERE dataset_id=%s AND batch_date=%s AND source_build_run_id=%s AND stat_date BETWEEN %s AND %s",
        current,
    )
    ads = {(day, dept): count for day, dept, count in cursor.fetchall()}
    return expected, dws, ads


def _period_counts(expected, dws, ads, grain):
    totals = Counter()
    missing = set()
    for day, dept in expected.keys() | dws.keys() | ads.keys():
        key = (day, dept)
        period = day if grain == "day" else day.replace(day=1)
        value = expected.get(key)
        if value is None or dws.get(key) != (value, "PASS") or ads.get(key) != value:
            missing.update(((period, dept), (period, None)))
        else:
            totals[(period, dept)] += value
            totals[(period, None)] += value
    return totals, missing


def _periods(first, last, grain):
    while True:
        yield first
        period_end = first if grain == "day" else _month_end(first)
        if period_end == last:
            break
        first = period_end + timedelta(days=1)


def _calculate(cursor, span, grain, dataset_id, batch_date, env, *, write):
    dataset, batch, report, run = _source(cursor, dataset_id, batch_date, env)
    low, high = (date.fromisoformat(report[key]) for key in ("effective_start", "effective_end"))
    first, last = span
    allowed = (low, high) if grain == "day" else (low.replace(day=1), _month_end(high))
    if not allowed[0] <= first <= last <= allowed[1]:
        raise OdsError("N10_RANGE_OUTSIDE_BUILD")
    prior = (first - timedelta(days=1)).replace(day=1) if grain == "month" and first > low else first
    expected, dws, ads = _read_counts(cursor, dataset, batch, run, max(prior, low), min(last, high))
    totals, missing = _period_counts(expected, dws, ads, grain)
    cursor.execute("SELECT dept_code FROM dim_department WHERE dataset_id=%s", (dataset,))
    scopes = [None, *(row[0] for row in cursor.fetchall())]
    rows = []

    def append(code, period, dept, unit, numerator, denominator, value, reason):
        dimension = "ALL" if dept is None else f"DEPT:{dept}"
        rows.append((dataset, code, dimension, grain, period, batch, run, DEFINITION, unit,
                     numerator, denominator, value, "OK" if reason is None else "NULL", reason))

    for period in _periods(first, last, grain):
        for dept in scopes:
            key = (period, dept)
            current = totals[key]
            reason = None
            if period < low or grain == "month" and _month_end(period) > high:
                reason = "INCOMPLETE_PERIOD"
            elif key in missing:
                reason = "ADS_NOT_PUBLISHED"
            value = current if reason is None else None
            append(VOLUME, period, dept, "人次", value, None, value, reason)
            if grain == "month" and write:
                previous = (period - timedelta(days=1)).replace(day=1)
                denominator = totals[(previous, dept)]
                numerator = current - denominator
                ratio = None
                if reason is None:
                    if previous < low or (previous, dept) in missing:
                        reason = "NO_COMPARABLE_PRIOR"
                    elif denominator == 0:
                        reason = "ZERO_BASE"
                    else:
                        ratio = (Decimal(numerator) * 100 / denominator).quantize(
                            Decimal("0.000001"), rounding=ROUND_HALF_UP,
                        )
                if reason not in (None, "ZERO_BASE"):
                    numerator = denominator = None
                append(GROWTH, period, dept, "%", numerator, denominator, ratio, reason)

    rows.sort(key=lambda row: row[1:5])
    if write:
        where = "dataset_id=%s AND indicator_code IN (%s,%s) AND granularity=%s AND stat_date BETWEEN %s AND %s"
        _write_rows(cursor, where, (dataset, VOLUME, GROWTH, grain, first, last), rows)
    if _report(env, batch.isoformat()) != report:
        raise OdsError("N10_SOURCE_STALE")
    return rows, {
        "status": "PASS", "dataset_id": dataset, "source_batch": batch.isoformat(),
        "source_build_run_id": run, "definition_version": DEFINITION, "granularity": grain,
        "start_date": first.isoformat(), "end_date": last.isoformat(), "rows": len(rows),
        "null_reasons": dict(Counter(row[-1] for row in rows if row[-1] is not None)),
    }


def _write_rows(cursor, where, params, rows):
    cursor.execute(
        "SELECT engine FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (TABLE,),
    )
    if cursor.fetchone() != ("InnoDB",):
        raise OdsError("N10_MIGRATION_REQUIRED")
    columns = ", ".join(COLUMNS)
    placeholders = ", ".join(("%s",) * len(COLUMNS))
    cursor.execute(f"DELETE FROM {TABLE} WHERE {where}", params)
    cursor.executemany(f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders})", rows)
    cursor.execute(
        f"SELECT {columns} FROM {TABLE} WHERE {where} ORDER BY indicator_code,dimension_key,granularity,stat_date",
        params,
    )
    if tuple(cursor.fetchall()) != tuple(rows):
        raise OdsError("N10_RECONCILE_FAILED")
