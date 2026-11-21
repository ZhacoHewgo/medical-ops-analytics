"""门诊人次与自然月环比，以认证门诊集市为输入。"""
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from functools import partial
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from numbers import Real

import pandas as pd

from etl.config.source_config import OdsError, connect_database
from etl.jobs import aggregate_themes as themes, build_dwd as build, clean_dwd as clean
from etl.jobs.aggregate_dws import _day, _department, _report, _scope
from etl.jobs.quality_check import PRIVACY_KEY_ERRORS
from etl.scheduler import scheduler


VOLUME = "MOPS-OUT-01"
GROWTH = "MOPS-OUT-02"
DEFINITION = "outpatient-v1"
DISCHARGES = "MOPS-INP-01"
STAY = "MOPS-INP-02"
COST = "MOPS-INP-06"
BED = "MOPS-INP-04"
DRUG = "MOPS-DRUG-02"
CONSUMABLE = "MOPS-CONS-02"
DRG = "MOPS-DIS-03"
CMI = "MOPS-DIS-04"
THEME_METRICS = {
    DISCHARGES: ("inpatient", "人次", "sim-discharge-v1"),
    STAY: ("inpatient", "天", "sim-discharge-v1"),
    COST: ("inpatient", "元/人次", "sim-discharge-v1"),
    BED: ("efficiency", "%", "bed-usage-v1"),
    DRUG: ("drug", "%", "sim-charge-v1"),
    CONSUMABLE: ("consumable", "%", "sim-charge-v1"),
    DRG: ("drg", "%", "drg-group-rate-v1"),
    CMI: ("drg", "指数", "drg-cmi-v1"),
}
THEME_SUMMARIES = {
    "income": (("charge_amount", "charge_count"), {"net_revenue": "NET_REVENUE_RULE_UNCONFIRMED"}),
    "drg": (("source_diagnosis_cases", "discharges"), {
        "drg_grouped_cases": "OFFICIAL_DRG_GROUPING_MISSING",
        "cmi": "DRG_GROUPING_AND_WEIGHTS_MISSING",
    }),
    "efficiency": (("discharges",), {
        "available_bed_days": "BED_CAPACITY_MISSING",
        "occupied_bed_days": "BED_OCCUPANCY_RULE_UNCONFIRMED",
        "bed_usage_rate": "BED_CAPACITY_MISSING",
    }),
}
TABLE = "ads_indicator_result"
COLUMNS = ("dataset_id", "indicator_code", "dimension_key", "granularity", "stat_date",
           "source_batch", "source_build_run_id", "definition_version", "unit", "numerator",
           "denominator", "metric_value", "calculation_status", "null_reason")
SAFE_ERRORS = {
    "N10_ARGUMENTS_INVALID", "N10_SOURCE_STALE", "N10_RANGE_OUTSIDE_BUILD",
    "N10_MIGRATION_REQUIRED", "N10_RECONCILE_FAILED", "N10_ROLLBACK_FAILED", "N10_COMMIT_UNKNOWN",
    "N10_ADS_NOT_PUBLISHED", "N10_DEPARTMENT_UNKNOWN", "N10_ENV_DATABASE_MISMATCH",
    "N10_DATE_INVALID", "N10_RANGE_INVALID", "N10_GRANULARITY_INVALID", "N10_METRIC_INVALID",
    "N10_DEPARTMENT_INVALID", "N10_THEME_INVALID", "N10_DRILL_INVALID", "N10_BATCH_INVALID",
    "N10_DATASET_INVALID", "N10_ENV_INVALID",
}


class _BuildRangeError(OdsError):
    def __init__(self, requested, available):
        self.requested = requested
        self.available = available
        first, last = requested
        low, high = available
        super().__init__("N10_RANGE_OUTSIDE_BUILD",
                         detail=f"requested={first.isoformat()}..{last.isoformat()}, "
                                f"available={low.isoformat()}..{high.isoformat()}")


def calc_outpatient_volume(start_date, end_date=None, granularity="day", *, dataset_id=None, batch_date=None, env="dev"):
    if isinstance(start_date, pd.DataFrame) and end_date is None:
        frame = start_date
        if frame.empty:
            return None
        if not frame.columns.is_unique:
            raise OdsError("N10_ARGUMENTS_INVALID")
        if "outpatient_visits" in frame:
            total = _sum_measures(frame["outpatient_visits"], counts=True)
            return int(total) if total is not None else None
        if "visit_id" not in frame:
            raise OdsError("N10_ARGUMENTS_INVALID", detail="VISIT_ID_REQUIRED")
        if "visit_type" in frame:
            outpatient = frame["visit_type"].isin(("OUTPATIENT", "门诊", "普通", "专家", "急诊"))
            inpatient = frame["visit_type"].isin(("INPATIENT", "住院"))
            if not (outpatient | inpatient).all():
                raise OdsError("N10_ARGUMENTS_INVALID", detail="VISIT_TYPE_INVALID")
            frame = frame.loc[outpatient]
        if not frame["visit_id"].map(lambda value: clean._valid_id(value, integer=True)).all():
            raise OdsError("N10_ARGUMENTS_INVALID", detail="VISIT_ID_INVALID")
        key = ["dataset_id", "visit_id"] if "dataset_id" in frame else ["visit_id"]
        return len(frame.drop_duplicates(subset=key))
    rows, _ = _run(start_date, start_date if end_date is None else end_date, granularity,
                   dataset_id, batch_date, env, write=False)
    return pd.DataFrame(rows, columns=COLUMNS, dtype=object)


def calc_inpatient_discharge(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(DISCHARGES, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_bed_usage_rate(start_date, end_date=None, granularity="day", *, dataset_id=None, batch_date=None, env="dev"):
    if isinstance(start_date, pd.DataFrame) and end_date is None:
        columns = ("occupied_bed_days", "available_bed_days")
        if not start_date.columns.is_unique or not set(columns) <= set(start_date.columns):
            raise OdsError("N10_ARGUMENTS_INVALID", detail="BED_DAYS_REQUIRED")
        occupied, available = (_sum_measures(start_date[column]) for column in columns)
        return _bed_rate(occupied, available)
    if isinstance(start_date, (Real, Decimal)) or isinstance(end_date, (Real, Decimal)):
        return _bed_rate(_sum_measures((start_date,)), _sum_measures((end_date,)))
    end = start_date if end_date is None else end_date
    return _theme_frame(BED, start_date, end, granularity, dataset_id, batch_date, env)


def _sum_measures(values, *, counts=False):
    total = None
    for value in values:
        if value is None or value is pd.NA:
            return None
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise OdsError("N10_MEASURE_INVALID") from None
        if number.is_nan():
            return None
        if not number.is_finite() or number < 0 or counts and number != number.to_integral_value():
            raise OdsError("N10_MEASURE_INVALID") from None
        total = number if total is None else total + number
    return total


def _bed_rate(occupied, available):
    if occupied is None or available is None or available == 0:
        return None
    return float(_decimal_ratio(occupied, available, 100))


def calc_avg_stay_days(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(STAY, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_avg_cost(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(COST, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_drug_ratio(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(DRUG, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_consumable_ratio(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(CONSUMABLE, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_cmi(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(CMI, start_date, end_date, granularity, dataset_id, batch_date, env)


def calc_drg_group_rate(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev"):
    return _theme_frame(DRG, start_date, end_date, granularity, dataset_id, batch_date, env)


def _theme_frame(metric, start, end, grain, dataset, batch, env):
    import pandas as pd

    rows = _run(start, end, grain, dataset, batch, env, write=False,
                calculation=partial(_calculate_theme, metric=metric))
    return pd.DataFrame(rows, columns=COLUMNS, dtype=object)


def summarize_inpatient(start, end, department, *, dataset_id, batch_date, build_run_id, env="dev"):
    department = _department(department, "N10_DEPARTMENT_INVALID")
    return _run(start, end, "day", dataset_id, batch_date, env, write=False, open_dates=True,
                calculation=partial(_calculate_inpatient_summary, department=department, build_run_id=build_run_id))


def summarize_theme(theme, start, end, department, *, dataset_id, batch_date, build_run_id, env="dev", drill=None):
    if drill not in (None, "department"):
        raise OdsError("N10_DRILL_INVALID")
    try:
        theme = themes.canonical(theme)
    except OdsError:
        raise OdsError("N10_THEME_INVALID") from None
    if theme not in THEME_SUMMARIES:
        raise OdsError("N10_THEME_INVALID")
    department = _department(department, "N10_DEPARTMENT_INVALID")
    calculation = partial(_calculate_theme_summary, theme=theme, department=department,
                          build_run_id=build_run_id, drill=drill)
    return _run(start, end, "day", dataset_id, batch_date, env, write=False, open_dates=True,
                calculation=calculation)


def run_indicators(start_date, end_date, granularity, *, dataset_id=None, batch_date=None, env="dev", metrics=()):
    if isinstance(metrics, str):
        metrics = (metrics,)
    if not isinstance(metrics, (tuple, list)):
        raise OdsError("N10_ARGUMENTS_INVALID")
    if any(not isinstance(code, str) or code not in THEME_METRICS for code in metrics):
        raise OdsError("N10_METRIC_INVALID")
    if len(metrics) != len(set(metrics)):
        raise OdsError("N10_METRIC_INVALID")
    calculation = partial(_calculate, metrics=tuple(metrics)) if metrics else None
    _, summary = _run(start_date, end_date, granularity, dataset_id, batch_date, env,
                      write=True, calculation=calculation)
    return summary


def rerun(date_range, granularity=None, *extra, dataset_id=None, batch_date=None, env="dev", metrics=()):
    start, end, grain = _rerun_arguments(date_range, granularity, extra)
    try:
        return run_indicators(start, end, grain, dataset_id=dataset_id, batch_date=batch_date,
                              env=env, metrics=metrics)
    except _BuildRangeError as error:
        first, last = error.requested
        low, high = error.available
        if first <= high and last >= low:
            raise
        return {
            "status": "NO_DATA", "code": error.code, "rows": 0, "granularity": _grain(grain),
            "start_date": first.isoformat(), "end_date": last.isoformat(),
            "available_start_date": low.isoformat(), "available_end_date": high.isoformat(),
        }


def _rerun_arguments(date_range, granularity, extra):
    if isinstance(date_range, (tuple, list)):
        if extra:
            raise OdsError("N10_ARGUMENTS_INVALID")
        if len(date_range) != 2:
            raise OdsError("N10_RANGE_INVALID")
        return date_range[0], date_range[1], "day" if granularity is None else granularity
    if extra:
        if len(extra) > 1 or granularity is None:
            raise OdsError("N10_ARGUMENTS_INVALID")
        return date_range, granularity, extra[0]
    if granularity is None:
        raise OdsError("N10_RANGE_INVALID")
    if isinstance(granularity, str) and granularity.strip().lower() in ("day", "month"):
        raise OdsError("N10_RANGE_INVALID")
    return date_range, granularity, "day"


def _month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _grain(value):
    if not isinstance(value, str):
        raise OdsError("N10_GRANULARITY_INVALID")
    text = value.strip().lower()
    if text not in ("day", "month"):
        raise OdsError("N10_GRANULARITY_INVALID")
    return text


def _range(start, end, grain):
    grain = _grain(grain)
    first, last = _day(start), _day(end)
    if first is None or last is None:
        raise OdsError("N10_DATE_INVALID")
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    if first > last:
        raise OdsError("N10_RANGE_INVALID")
    if grain == "month" and (first.day != 1 or last != _month_end(last)):
        raise OdsError("N10_RANGE_INVALID")
    return first, last


def _run(start, end, grain, dataset_id, batch_date, env, *, write, calculation=None, open_dates=False):
    grain = _grain(grain)
    if open_dates:
        span = []
        for value in (start, end):
            if value is None:
                span.append(None)
                continue
            day = _day(value)
            if day is None:
                raise OdsError("N10_DATE_INVALID")
            span.append(day)
        if span[0] is not None and span[1] is not None and span[0] > span[1]:
            raise OdsError("N10_RANGE_INVALID")
        span = tuple(span)
    else:
        span = _range(start, end, grain)
    batch = _scope(dataset_id, batch_date, env, "N10")
    try:
        with scheduler._lock(env, "run"):
            return _transaction(span, grain, dataset_id, batch, env, write=write, calculation=calculation)
    except scheduler.EtlError:
        raise OdsError("N10_RUN_LOCKED") from None


def _transaction(span, grain, dataset_id, batch_date, env, *, write, calculation=None):
    connection = connect_database()
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION")
                result = (calculation or _calculate)(cursor, span, grain, dataset_id, batch_date, env, write=write)
        except Exception as error:
            try:
                connection.rollback()
            except Exception:
                raise OdsError("N10_ROLLBACK_FAILED") from None
            if isinstance(error, _BuildRangeError):
                raise error from None
            if isinstance(error, OdsError) and (error.code in SAFE_ERRORS or error.code in PRIVACY_KEY_ERRORS):
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
    except OdsError as error:
        if error.code in PRIVACY_KEY_ERRORS:
            raise OdsError(error.code) from None
        if error.code == "N8_ARGUMENTS_INVALID":
            raise OdsError("N10_DATASET_INVALID") from None
        raise OdsError("N10_SOURCE_STALE") from None
    if batch_date is not None and batch_date != batch:
        raise OdsError("N10_BATCH_INVALID")
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


def _calculate(cursor, span, grain, dataset_id, batch_date, env, *, write, metrics=()):
    dataset, batch, report, run = _source(cursor, dataset_id, batch_date, env)
    low, high = (date.fromisoformat(report[key]) for key in ("effective_start", "effective_end"))
    first, last = span
    allowed = (low, high) if grain == "day" else (low.replace(day=1), _month_end(high))
    if not allowed[0] <= first <= last <= allowed[1]:
        raise _BuildRangeError(span, allowed)
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

    if metrics:
        rows.extend(_theme_rows(cursor, span, grain, dataset, batch, run, low, high, scopes, metrics))
    rows.sort(key=lambda row: row[1:5])
    if write:
        codes = (VOLUME, GROWTH, *metrics)
        placeholders = ",".join("%s" for _ in codes)
        where = (f"dataset_id=%s AND indicator_code IN ({placeholders}) "
                 "AND granularity=%s AND stat_date BETWEEN %s AND %s")
        _write_rows(cursor, where, (dataset, *codes, grain, first, last), rows)
    if _report(env, batch.isoformat()) != report:
        raise OdsError("N10_SOURCE_STALE")
    return rows, {
        "status": "PASS", "dataset_id": dataset, "source_batch": batch.isoformat(),
        "source_build_run_id": run, "definition_version": DEFINITION, "granularity": grain,
        "start_date": first.isoformat(), "end_date": last.isoformat(), "rows": len(rows),
        "null_reasons": dict(Counter(row[-1] for row in rows if row[-1] is not None)),
    }


def _read_theme(cursor, theme, dataset, batch, run, first, last):
    table = themes.TABLES[theme]
    columns = ("stat_date", "dept_code", *themes.COLUMNS[table][len(themes.KEYS):])
    keys = (dataset, batch, run, first, last)
    where = "dataset_id=%s AND batch_date=%s AND source_build_run_id=%s AND stat_date BETWEEN %s AND %s"
    cursor.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where} ORDER BY stat_date,dept_code", keys)
    published = tuple(cursor.fetchall())
    cursor.execute(f"SELECT stat_date,rows_published FROM {themes.STATUS} WHERE {where} AND theme=%s",
                   (*keys, theme))
    counted = Counter(row[0] for row in published)
    recorded = dict(cursor.fetchall())
    if recorded != {day: counted[day] for day in _periods(first, last, "day")}:
        raise OdsError("N10_ADS_NOT_PUBLISHED")
    source_keys = (dataset, first, last)
    expected = themes._charge_groups(cursor, source_keys) if theme in {"drug", "income", "consumable"} else themes._discharges(cursor, source_keys)
    if published != tuple(expected[theme]):
        raise OdsError("N10_ADS_NOT_PUBLISHED")
    return [dict(zip(columns, row)) for row in published]


def _decimal_ratio(numerator, denominator, factor=1):
    return (Decimal(numerator) * factor / denominator).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _ratio(metric, rows):
    reason = {BED: "BED_CAPACITY_MISSING", DRG: "OFFICIAL_DRG_GROUPING_MISSING",
              CMI: "DRG_GROUPING_AND_WEIGHTS_MISSING"}.get(metric)
    if reason:
        return None, None, None, reason
    if metric == DISCHARGES:
        count = sum(row["discharges"] for row in rows)
        return count, None, count, None
    fee_kind = {DRUG: "drug", CONSUMABLE: "consumable"}.get(metric)
    if fee_kind:
        status = f"{fee_kind}_status"
        if any(row[status] == "UNCONFIRMED_RULE" for row in rows):
            return None, None, None, "UNCONFIRMED_RULE"
        if any(row["charge_status"] is not None or row[status] not in (None, "NO_RECORDS") for row in rows):
            return None, None, None, "INCOMPLETE_INPUT"
        numerator = sum(row[f"{fee_kind}_charge_amount"] or 0 for row in rows)
        denominator = sum(row["charge_amount"] for row in rows)
    else:
        value, status = ("discharge_stay_days", "stay_status") if metric == STAY else ("discharge_total_fee", "fee_status")
        denominator = sum(row["discharges"] for row in rows)
        if any(row[status] is not None for row in rows):
            return None, denominator, None, "INCOMPLETE_INPUT"
        numerator = sum(row[value] for row in rows)
    if denominator == 0:
        return numerator, denominator, None, "ZERO_BASE"
    return numerator, denominator, _decimal_ratio(numerator, denominator, 100 if fee_kind else 1), None


def _calculate_theme(cursor, span, grain, dataset_id, batch_date, env, *, write, metric):
    first, last = span
    dataset, batch, report, run = _source(cursor, dataset_id, batch_date, env)
    low, high = (date.fromisoformat(report[key]) for key in ("effective_start", "effective_end"))
    allowed = (low, high) if grain == "day" else (low.replace(day=1), _month_end(high))
    if not allowed[0] <= first <= last <= allowed[1]:
        raise _BuildRangeError(span, allowed)
    cursor.execute("SELECT dept_code FROM dim_department WHERE dataset_id=%s ORDER BY dept_code", (dataset,))
    scopes = [None, *(row[0] for row in cursor.fetchall())]
    rows = _theme_rows(cursor, span, grain, dataset, batch, run, low, high, scopes, (metric,))
    if _report(env, batch.isoformat()) != report:
        raise OdsError("N10_SOURCE_STALE")
    return rows


def _theme_rows(cursor, span, grain, dataset, batch, run, low, high, scopes, metrics):
    first, last = span
    groups_by_theme = {}
    for name in {THEME_METRICS[code][0] for code in metrics}:
        groups = {}
        for item in _read_theme(cursor, name, dataset, batch, run, max(first, low), min(last, high)):
            day, dept = item["stat_date"], item["dept_code"]
            period = day if grain == "day" else day.replace(day=1)
            for scope in (None, dept):
                groups.setdefault((period, scope), []).append(item)
        groups_by_theme[name] = groups
    rows = []
    for metric in metrics:
        name, unit, version = THEME_METRICS[metric]
        groups = groups_by_theme[name]
        for period in _periods(first, last, grain):
            complete = low <= period and (grain == "day" or _month_end(period) <= high)
            for scope in scopes:
                numerator, denominator, value, reason = (
                    _ratio(metric, groups.get((period, scope), ())) if complete else (None, None, None, "INCOMPLETE_PERIOD")
                )
                dimension = "ALL" if scope is None else f"DEPT:{scope}"
                rows.append((dataset, metric, dimension, grain, period, batch, run, version, unit, numerator,
                             denominator, value, "NULL" if reason else "OK", reason))
    return rows


def _summary_rows(cursor, span, grain, dataset_id, batch_date, env, *, theme, department, build_run_id):
    dataset, batch, report, run = _source(cursor, dataset_id, batch_date, env)
    if run != build_run_id:
        raise OdsError("N10_SOURCE_STALE")
    low, high = report["effective_start"], report["effective_end"]
    first, last = _range(span[0] if span[0] is not None else low,
                         span[1] if span[1] is not None else high, grain)
    start, end = first.isoformat(), last.isoformat()
    if not low <= start <= end <= high:
        raise OdsError("N10_RANGE_OUTSIDE_BUILD")
    if department is not None:
        cursor.execute("SELECT 1 FROM dim_department WHERE dataset_id=%s AND dept_code=%s", (dataset, department))
        if cursor.fetchone() is None:
            raise OdsError("N10_DEPARTMENT_UNKNOWN")
    source = _read_theme(cursor, theme, dataset, batch, run, first, last)
    rows = source if department is None else [row for row in source if row["dept_code"] == department]
    if _report(env, batch.isoformat()) != report:
        raise OdsError("N10_SOURCE_STALE")
    return rows, {
        "filters": {"start_date": start, "end_date": end, "dept_code": department},
        "meta": {"source": themes.TABLES[theme], "batch_date": batch.isoformat(), "build_run_id": run,
                 "available_start_date": low, "available_end_date": high},
    }


def _calculate_inpatient_summary(cursor, span, grain, dataset_id, batch_date, env, *, write, department, build_run_id):
    rows, context = _summary_rows(cursor, span, grain, dataset_id, batch_date, env,
                                  theme="inpatient", department=department, build_run_id=build_run_id)
    daily = Counter()
    for row in rows:
        daily[row["stat_date"]] += row["discharges"]
    result = {}
    for name, metric in (("avg_stay_days", STAY), ("avg_cost", COST)):
        _, _, value, reason = _ratio(metric, rows)
        result[name] = {"value": None if value is None else format(value, "f"), "null_reason": reason}
    return {
        **context, **result, "discharges": sum(daily.values()),
        "daily_trend": [{"date": day.isoformat(), "discharges": value} for day, value in sorted(daily.items())],
    }


def _theme_total(rows, key):
    values = [row[key] for row in rows]
    if None in values:
        return {"value": None, "null_reason": "INCOMPLETE_INPUT"}
    total = sum(values)
    return {"value": str(total), "null_reason": None}


def _calculate_theme_summary(cursor, span, grain, dataset_id, batch_date, env, *, write, theme, department,
                             build_run_id, drill):
    rows, context = _summary_rows(cursor, span, grain, dataset_id, batch_date, env,
                                  theme=theme, department=department, build_run_id=build_run_id)
    available, missing = THEME_SUMMARIES[theme]
    metrics = {key: _theme_total(rows, key) for key in available}
    metrics.update({key: {"value": None, "null_reason": reason} for key, reason in missing.items()})
    by_group = {}
    for row in rows:
        group = row["dept_code"] if drill == "department" else row["stat_date"].isoformat()
        by_group.setdefault(group, []).append(row)
    return {**context, "metric": available[0], "metrics": metrics,
            "group_by": drill or "day",
            "rows": [{"group": key, **_theme_total(items, available[0])} for key, items in sorted(by_group.items())]}


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
