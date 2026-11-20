"""Read the pinned Node 8 build and aggregate its outpatient rows in one snapshot."""

import os
from uuid import UUID

from app.outpatient import strict_date
from etl.config.source_config import OdsError, connect_database, database_options, validate_dataset


def read_current_report(build, environment, batch):
    directory = build._open_report_directory_readonly(environment)
    try:
        return build._read_report_at(directory, build._report_name(batch))
    finally:
        os.close(directory)


def read_counts(cursor, dataset, start, end, dept_code):
    sql = (
        "SELECT visit_date, dept_code, COUNT(*) "
        "FROM dwd_visit_detail WHERE dataset_id = %s AND visit_type = %s "
        "AND visit_date BETWEEN %s AND %s"
    )
    params = (dataset, "OUTPATIENT", start, end)
    if dept_code is not None:
        sql += " AND dept_code = %s"
        params += (dept_code,)
    cursor.execute(sql + " GROUP BY visit_date, dept_code ORDER BY visit_date, dept_code", params)
    return [
        {"date": day.isoformat(), "dept_code": department, "visits": count}
        for day, department, count in cursor.fetchall()
    ]


def load_preview(*, start_date=None, end_date=None, dept_code=None):
    """Returns aggregates only. No default data or partially certified builds."""
    if os.environ.get("OUTPATIENT_PREVIEW_ENABLED") != "1":
        raise OdsError("OUTPATIENT_PREVIEW_DISABLED")
    return _load_outpatient(start_date, end_date, dept_code, source="dwd_visit_detail")


def _read_ads_counts(cursor, dataset, batch, run_id, start, end, dept_code):
    expected = read_counts(cursor, dataset, start, end, dept_code)
    sql = (
        "SELECT a.stat_date,a.dept_code,a.outpatient_visits FROM ads_outpatient_theme a "
        "JOIN dws_dept_daily d ON d.dataset_id=a.dataset_id AND d.stat_date=a.stat_date "
        "AND d.dept_code=a.dept_code AND d.batch_date=a.batch_date "
        "AND d.source_build_run_id=a.source_build_run_id AND d.outpatient_visits=a.outpatient_visits "
        "AND d.ads_status='PASS' WHERE a.dataset_id=%s AND a.batch_date=%s "
        "AND a.source_build_run_id=%s AND a.stat_date BETWEEN %s AND %s"
    )
    params = (dataset, batch, run_id, start, end)
    if dept_code is not None:
        sql += " AND a.dept_code=%s"
        params += (dept_code,)
    cursor.execute(sql + " ORDER BY a.stat_date,a.dept_code", params)
    actual = [{"date": day.isoformat(), "dept_code": dept, "visits": count}
              for day, dept, count in cursor.fetchall()]
    if actual != expected:
        raise OdsError("OUTPATIENT_ADS_INCOMPLETE")
    return actual


def load_ads(*, start_date=None, end_date=None, dept_code=None, allowed_department=None):
    if allowed_department is not None:
        if dept_code is not None and dept_code != allowed_department:
            raise OdsError("OUTPATIENT_ADS_SCOPE_INVALID")
        dept_code = allowed_department
    return _load_outpatient(start_date, end_date, dept_code, source="ads_outpatient_theme",
                            allowed_department=allowed_department)


def _load_outpatient(start_date, end_date, dept_code, *, source, allowed_department=None):
    from pymysql import MySQLError

    from etl.jobs import build_dwd as build
    dataset = validate_dataset(os.environ.get("OUTPATIENT_PREVIEW_DATASET_ID", ""))
    batch = os.environ.get("OUTPATIENT_PREVIEW_BATCH_DATE", "")
    strict_date(batch)
    run_id = os.environ.get("OUTPATIENT_PREVIEW_BUILD_RUN_ID", "")
    if str(UUID(run_id)) != run_id:
        raise OdsError("OUTPATIENT_PREVIEW_ID_INVALID")
    environment = os.environ.get("OUTPATIENT_PREVIEW_ENV", "dev")
    if environment not in {"dev", "test"}:
        raise OdsError("OUTPATIENT_PREVIEW_ENV_INVALID")
    if database_options()["database"] != {"dev": "hospital", "test": "hospital_test"}[environment]:
        raise OdsError("OUTPATIENT_PREVIEW_ENV_INVALID")
    report = read_current_report(build, environment, batch)
    if not report or report["build_run_id"] != run_id:
        raise OdsError("OUTPATIENT_PREVIEW_STALE")

    connection = connect_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            identity = build.verify_build_freshness_from_cursor(
                cursor, dataset_id=dataset, batch_date=batch, env=environment, report=report
            )
            if (
                identity["build_run_id"] != run_id
                or identity["dataset_id"] != dataset
                or identity["batch_date"] != batch
                or identity["gate_status"] != "PASS"
            ):
                raise OdsError("OUTPATIENT_PREVIEW_STALE")
            first, last = report["effective_start"], report["effective_end"]
            if source == "ads_outpatient_theme":
                sql = "SELECT dept_code FROM dim_department WHERE dataset_id = %s"
                if allowed_department is None:
                    cursor.execute(sql, (dataset,))
                else:
                    cursor.execute(sql + " AND dept_code = %s", (dataset, allowed_department))
            else:
                cursor.execute(
                    "SELECT DISTINCT dept_code FROM dwd_visit_detail "
                    "WHERE dataset_id = %s AND visit_type = %s AND visit_date BETWEEN %s AND %s",
                    (dataset, "OUTPATIENT", first, last),
                )
            departments = sorted(row[0] for row in cursor.fetchall())
            start = first if start_date is None else start_date
            end = last if end_date is None else end_date
            counts = []
            if first <= start <= end <= last and (dept_code is None or dept_code in departments):
                if source == "ads_outpatient_theme":
                    counts = _read_ads_counts(cursor, dataset, batch, run_id, start, end, dept_code)
                else:
                    counts = read_counts(cursor, dataset, start, end, dept_code)
        # Reject a certificate replaced while the snapshot was being read.
        if read_current_report(build, environment, batch) != report:
            raise OdsError("OUTPATIENT_PREVIEW_STALE")
    except MySQLError:
        raise OdsError("OUTPATIENT_PREVIEW_READ_FAILED") from None
    finally:
        try:
            connection.rollback()
        except MySQLError:
            raise OdsError("OUTPATIENT_PREVIEW_READ_FAILED") from None
        finally:
            try:
                connection.close()
            except MySQLError:
                pass

    return {
        "meta": {
            "source": source,
            "batch_date": batch,
            "build_run_id": run_id,
            "available_start_date": report["effective_start"],
            "available_end_date": report["effective_end"],
            "department_codes": departments,
        },
        "counts": counts,
    }
