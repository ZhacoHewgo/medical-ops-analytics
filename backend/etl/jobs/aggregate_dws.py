"""Daily DWS and theme ADS over a current, certified Node 8 build."""
from collections.abc import Mapping
from datetime import date as calendar_date, datetime
from decimal import Decimal, localcontext
from numbers import Number
import os
import re

import pandas as pd

from etl.config.source_config import OdsError, connect_database, database_options, validate_batch, validate_dataset
from etl.jobs import aggregate_themes, build_dwd as build, clean_dwd as clean
from etl.scheduler import scheduler


DWS = "dws_dept_daily"
ADS = "ads_outpatient_theme"
COLUMNS = ("dataset_id", "stat_date", "dept_code", "batch_date", "source_build_run_id",
           "outpatient_visits", "outpatient_fee_samples", "outpatient_total_fee", "fee_status")


def build_dws_daily(dept=None, date=None, *, dataset_id=None, batch_date=None, env="dev"):
    if isinstance(dept, pd.DataFrame):
        return _daily_frame(dept, date, dataset_id)
    dept, day = _daily_arguments(dept, date)
    if day is None:
        raise OdsError("N9_DATE_REQUIRED")
    return _run(_date_range((day, day)), dept, "daily", dataset_id, batch_date, env)


def _daily_frame(frame, requested_date, dataset_id):
    day_filter = _day(requested_date)
    if requested_date is not None and day_filter is None:
        raise OdsError("N9_DATE_INVALID") from None
    if dataset_id is not None:
        validate_dataset(dataset_id)
    if not frame.columns.is_unique:
        raise OdsError("N9_FRAME_COLUMNS_INVALID") from None
    day_column = next((name for name in ("visit_date", "stat_date", "date") if name in frame), None)
    dept_column = next((name for name in ("dept_code", "department", "dept") if name in frame), None)
    fee_column = next((name for name in ("source_total_fee", "fee_amount", "total_fee", "amount") if name in frame),
                      "source_total_fee")
    if day_column is None or dept_column is None:
        raise OdsError("N9_FRAME_COLUMNS_INVALID") from None
    fields = frame.reindex(columns=["dataset_id", day_column, dept_column, "visit_type", fee_column])
    fields = fields.astype(object).where(fields.notna(), None)
    groups = {}
    with localcontext() as context:
        context.prec = 40
        for dataset, raw_day, raw_dept, kind, raw_fee in fields.itertuples(index=False, name=None):
            if dataset is not None and dataset_id is not None and dataset != dataset_id:
                continue
            if kind == "INPATIENT":
                continue
            if kind not in (None, "OUTPATIENT"):
                raise OdsError("N9_FRAME_VISIT_TYPE_INVALID") from None
            day = _day(raw_day)
            if day is None:
                raise OdsError("N9_DATE_INVALID") from None
            if day_filter is not None and day != day_filter:
                continue
            department = _department(raw_dept, "N9_DEPARTMENT", detail=True)
            if department is None:
                raise OdsError("N9_DEPARTMENT_EMPTY") from None
            dataset = dataset_id if dataset is None else validate_dataset(dataset)
            key = (dataset, day, department)
            count, samples, amount = groups.get(key, (0, 0, Decimal("0.00")))
            if raw_fee is not None:
                try:
                    amount += clean._decimal(str(raw_fee))
                except ValueError:
                    raise OdsError("N9_FRAME_AMOUNT_INVALID") from None
                samples += 1
            groups[key] = (count + 1, samples, amount)
    rows = [(*key, count, samples, *aggregate_themes._measure(count, samples, amount))
            for key, (count, samples, amount) in groups.items()]
    return pd.DataFrame(rows, columns=COLUMNS[:3] + COLUMNS[5:])


def build_theme_marts(theme, *, dataset_id=None, batch_date=None, env="dev"):
    theme = aggregate_themes.canonical(theme)
    if theme == aggregate_themes.OUTPATIENT:
        return _run(None, None, "theme", dataset_id, batch_date, env)
    return _run(None, None, "themes_only", dataset_id, batch_date, env, themes=(theme,))


def rerun(date_range, end_date=None, *, dataset_id=None, batch_date=None, env="dev", themes=()):
    span = _date_range(date_range) if end_date is None else _date_range((date_range, end_date))
    selected = aggregate_themes.validate_selection(themes)
    return _run(span, None, "rerun", dataset_id, batch_date, env, themes=selected)


def _daily_arguments(dept, date):
    left, right = _day(dept), _day(date)
    if date is None:
        if left is not None:
            return None, left
        if dept is None:
            return None, None
        if _day_like(dept):
            raise OdsError("N9_DATE_INVALID")
        return _department(dept, "N9_DEPARTMENT", detail=True), None
    if left is not None and right is None:
        return _department(date, "N9_DEPARTMENT", detail=True), left
    if left is not None and right is not None:
        raise OdsError("N9_ARGUMENTS_INVALID")
    if right is None:
        raise OdsError("N9_DATE_INVALID")
    return _department(dept, "N9_DEPARTMENT", detail=True), right


def _day_like(value):
    return isinstance(value, str) and re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}.*", value.strip()) is not None


def _type_category(value):
    """Name the rejected container or scalar family, never its content."""
    if isinstance(value, bool):
        return "OTHER"
    if isinstance(value, Number):
        return "NUMERIC"
    if isinstance(value, Mapping):
        return "MAPPING"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "SEQUENCE"
    return "OTHER"


def _department(value, code, *, detail=False):
    """Reject a department by category only; the offending value never reaches the message."""

    def reject(category):
        raise OdsError(f"{code}_{category}" if detail else code)

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        reject(f"TYPE_{_type_category(value)}")
    text = str(value).strip()
    if not text:
        reject("EMPTY")
    if len(text) > 64 or not text.isprintable():
        reject("FORMAT")
    return text


def _day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, calendar_date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return validate_batch(text)
    except OdsError:
        pass
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def _date_range(values):
    if not isinstance(values, (tuple, list)) or len(values) != 2:
        raise OdsError("N9_RANGE_INVALID")
    start, end = (_day(value) for value in values)
    if start is None or end is None:
        raise OdsError("N9_DATE_INVALID")
    if start > end:
        raise OdsError("N9_RANGE_INVALID")
    return start, end


def _scope(dataset_id, batch_date, env, prefix):
    if not isinstance(env, str) or env not in ("dev", "test"):
        raise OdsError(f"{prefix}_ENV_INVALID")
    if dataset_id is not None and (not isinstance(dataset_id, str) or not dataset_id.strip()):
        raise OdsError(f"{prefix}_DATASET_INVALID")
    batch = None
    if batch_date is not None:
        batch = _day(batch_date)
        if batch is None:
            raise OdsError(f"{prefix}_BATCH_INVALID")
    try:
        build._validate_public(dataset_id, batch, env)
    except OdsError as error:
        if error.code == "N8_RUN_CONTEXT_ACTIVE":
            raise OdsError(f"{prefix}_RUN_LOCKED") from None
        try:
            options = database_options()
        except OdsError as config:
            raise OdsError(config.code) from None
        if options["database"] != ("hospital_test" if env == "test" else "hospital"):
            raise OdsError(f"{prefix}_ENV_DATABASE_MISMATCH") from None
        raise OdsError(f"{prefix}_DATASET_INVALID") from None
    return batch


def _run(span, dept, operation, dataset_id, batch_date, env, *, themes=()):
    batch = _scope(dataset_id, batch_date, env, "N9")
    try:
        with scheduler._lock(env, "run"):
            return _transaction(span, dept, operation, dataset_id, batch, env, themes=themes)
    except scheduler.EtlError:
        raise OdsError("N9_RUN_LOCKED") from None


def _transaction(span, dept, operation, dataset_id, batch_date, env, *, themes=()):
    connection = connect_database()
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION")
                result = _aggregate(cursor, span, dept, operation, dataset_id, batch_date, env, themes=themes)
        except Exception as error:
            try:
                connection.rollback()
            except Exception:
                raise OdsError("N9_ROLLBACK_FAILED") from None
            if isinstance(error, OdsError) and error.code.startswith("N9_"):
                raise OdsError(error.code) from None
            raise OdsError("N9_TX_FAILED") from None
        try:
            connection.commit()
        except Exception:
            raise OdsError("N9_COMMIT_UNKNOWN") from None
        return result
    finally:
        clean._close(connection)


def _report(env, batch):
    directory = build._open_report_directory_readonly(env)
    try:
        return build._read_report_at(directory, build._report_name(batch))
    finally:
        os.close(directory)


def _aggregate(cursor, span, dept, operation, dataset_id, batch_date, env, *, themes=()):
    try:
        dataset, batch, _ = build._lock_inputs(cursor, dataset_id)
        if batch_date is not None and batch_date != batch:
            raise OdsError("N9_BATCH_INVALID")
        report = _report(env, batch)
        identity = build.verify_build_freshness_from_cursor(cursor, dataset_id=dataset,
                                                            batch_date=batch, env=env, report=report)
    except OdsError as error:
        if error.code == "N9_BATCH_INVALID":
            raise OdsError("N9_BATCH_INVALID") from None
        if error.code in ("N9_ARGUMENTS_INVALID", "N8_ARGUMENTS_INVALID"):
            raise OdsError("N9_DATASET_INVALID") from None
        raise OdsError("N9_SOURCE_STALE") from None

    low, high = report["effective_start"], report["effective_end"]
    if span is not None:
        start, end = span
        date_source = "ARGUMENT"
    else:
        start, end = low, high
        date_source = "BUILD_RANGE"
    if not low <= start <= end <= high:
        raise OdsError("N9_RANGE_OUTSIDE_BUILD")
    if dept is not None:
        cursor.execute("SELECT 1 FROM dim_department WHERE dataset_id=%s AND dept_code=%s", (dataset, dept))
        if cursor.fetchone() is None:
            raise OdsError("N9_UNKNOWN_DEPARTMENT")

    run = identity["build_run_id"]
    batch_day = calendar_date.fromisoformat(batch)
    row_counts = {}
    summary = dict(dataset_id=dataset, batch_date=batch, source_build_run_id=run, start_date=start, end_date=end,
                   date_source=date_source, dept_code=dept, row_counts=row_counts, status="PASS")
    if operation != "themes_only":
        counts, state = _outpatient_write(cursor, dataset, batch_day, run, start, end, dept, operation)
        row_counts.update(counts)
        summary["ads_status"] = state
    if themes:
        new_counts, states = aggregate_themes.publish(cursor, themes, dataset, batch_day, run, start, end)
        row_counts.update(new_counts)
        summary["theme_states"] = states
    if _report(env, batch) != report:
        raise OdsError("N9_SOURCE_STALE")
    return summary


def _outpatient_write(cursor, dataset, batch_day, run, start, end, dept, operation):
    cursor.execute(
        "SELECT table_name, engine FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND table_name IN (%s, %s)", (DWS, ADS),
    )
    if dict(cursor.fetchall()) != {DWS: "InnoDB", ADS: "InnoDB"}:
        raise OdsError("N9_MIGRATION_REQUIRED")

    where = "dataset_id=%s AND stat_date BETWEEN %s AND %s"
    bound = (dataset, start, end)
    dept_filter = ""
    if dept is not None:
        dept_filter = " AND dept_code=%s"
        where += dept_filter
        bound += (dept,)
    cursor.execute(
        "SELECT visit_date, dept_code, COUNT(*), COUNT(source_total_fee), "
        "CASE WHEN COUNT(source_total_fee)=COUNT(*) THEN SUM(source_total_fee) END, "
        "CASE WHEN COUNT(source_total_fee)<COUNT(*) THEN 'INCOMPLETE_INPUT' END "
        "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_date BETWEEN %s AND %s "
        "AND visit_type='OUTPATIENT'" + dept_filter + " GROUP BY visit_date, dept_code ORDER BY visit_date, dept_code",
        bound,
    )
    rows = [(dataset, day, department, batch_day, run, count, samples, fee, reason)
            for day, department, count, samples, fee, reason in cursor.fetchall()]
    columns = ", ".join(COLUMNS)
    if operation == "theme":
        cursor.execute(f"SELECT {columns} FROM {DWS} WHERE {where} ORDER BY stat_date, dept_code", bound)
        if tuple(cursor.fetchall()) != tuple(rows):
            raise OdsError("N9_DWS_NOT_CURRENT")

    state = "PENDING" if operation == "daily" else "PASS"
    ads_rows = [] if operation == "daily" else rows
    _replace_rows(cursor, DWS, where, bound, [(*row, state) for row in rows])
    _replace_rows(cursor, ADS, where, bound, ads_rows)
    return {DWS: len(rows), ADS: len(ads_rows)}, state


def _replace_rows(cursor, table, where, bound, rows):
    columns = COLUMNS + ("ads_status",) if table == DWS else COLUMNS
    names = ", ".join(columns)
    cursor.execute(f"DELETE FROM {table} WHERE {where}", bound)
    if rows:
        placeholders = ", ".join(("%s",) * len(columns))
        cursor.executemany(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", rows)
    cursor.execute(f"SELECT {names} FROM {table} WHERE {where} ORDER BY stat_date, dept_code", bound)
    if tuple(cursor.fetchall()) != tuple(rows):
        raise OdsError("N9_RECONCILE_FAILED")
