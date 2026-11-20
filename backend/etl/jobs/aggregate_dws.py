"""Outpatient daily DWS and ADS over a current, certified Node 8 build."""
from datetime import date as calendar_date
import os

from etl.config.source_config import OdsError, connect_database, validate_batch
from etl.jobs import build_dwd as build, clean_dwd as clean
from etl.scheduler import scheduler


DWS = "dws_dept_daily"
ADS = "ads_outpatient_theme"
COLUMNS = ("dataset_id", "stat_date", "dept_code", "batch_date", "source_build_run_id",
           "outpatient_visits", "outpatient_fee_samples", "outpatient_total_fee", "fee_status")


def build_dws_daily(dept, date, *, dataset_id=None, batch_date=None, env="dev"):
    if dept is not None and (not isinstance(dept, str) or not 1 <= len(dept) <= 64 or dept.strip() != dept):
        raise OdsError("N9_ARGUMENTS_INVALID")
    return _run(_date_range((date, date)), dept, "daily", dataset_id, batch_date, env)


def build_theme_marts(theme, *, dataset_id=None, batch_date=None, env="dev"):
    if theme != "outpatient":
        raise OdsError("N9_ARGUMENTS_INVALID")
    return _run(None, None, "theme", dataset_id, batch_date, env)


def rerun(date_range, *, dataset_id=None, batch_date=None, env="dev"):
    return _run(_date_range(date_range), None, "rerun", dataset_id, batch_date, env)


def _date_range(values):
    if not isinstance(values, (tuple, list)) or len(values) != 2:
        raise OdsError("N9_ARGUMENTS_INVALID")
    try:
        start, end = map(validate_batch, values)
    except OdsError:
        raise OdsError("N9_ARGUMENTS_INVALID") from None
    if start > end:
        raise OdsError("N9_ARGUMENTS_INVALID")
    return start, end


def _run(span, dept, operation, dataset_id, batch_date, env):
    try:
        build._validate_public(dataset_id, batch_date, env)
    except OdsError as error:
        code = "N9_RUN_LOCKED" if error.code == "N8_RUN_CONTEXT_ACTIVE" else "N9_ARGUMENTS_INVALID"
        raise OdsError(code) from None
    try:
        with scheduler._lock(env, "run"):
            return _transaction(span, dept, operation, dataset_id, batch_date, env)
    except scheduler.EtlError:
        raise OdsError("N9_RUN_LOCKED") from None


def _transaction(span, dept, operation, dataset_id, batch_date, env):
    connection = connect_database()
    try:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION")
                result = _aggregate(cursor, span, dept, operation, dataset_id, batch_date, env)
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


def _aggregate(cursor, span, dept, operation, dataset_id, batch_date, env):
    try:
        dataset, batch, _ = build._lock_inputs(cursor, dataset_id)
        if batch_date is not None and batch_date != batch:
            raise OdsError("N9_ARGUMENTS_INVALID")
        report = _report(env, batch)
        identity = build.verify_build_freshness_from_cursor(cursor, dataset_id=dataset,
                                                            batch_date=batch, env=env, report=report)
    except OdsError as error:
        if error.code in ("N9_ARGUMENTS_INVALID", "N8_ARGUMENTS_INVALID"):
            raise OdsError("N9_ARGUMENTS_INVALID") from None
        raise OdsError("N9_SOURCE_STALE") from None

    low, high = report["effective_start"], report["effective_end"]
    start, end = span if span is not None else (low, high)
    if not low <= start <= end <= high:
        raise OdsError("N9_RANGE_OUTSIDE_BUILD")
    if dept is not None:
        cursor.execute("SELECT 1 FROM dim_department WHERE dataset_id=%s AND dept_code=%s", (dataset, dept))
        if cursor.fetchone() is None:
            raise OdsError("N9_UNKNOWN_DEPARTMENT")

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
    run = identity["build_run_id"]
    batch_day = calendar_date.fromisoformat(batch)
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
    if _report(env, batch) != report:
        raise OdsError("N9_SOURCE_STALE")
    return dict(dataset_id=dataset, batch_date=batch, source_build_run_id=run, start_date=start, end_date=end,
                dept_code=dept, row_counts={DWS: len(rows), ADS: len(ads_rows)}, ads_status=state, status="PASS")


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
