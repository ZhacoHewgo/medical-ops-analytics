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


def read_counts(cursor, dataset, start, end):
    cursor.execute(
        "SELECT visit_date, dept_code, COUNT(*) "
        "FROM dwd_visit_detail WHERE dataset_id = %s AND visit_type = %s "
        "AND visit_date BETWEEN %s AND %s "
        "GROUP BY visit_date, dept_code ORDER BY visit_date, dept_code",
        (dataset, "OUTPATIENT", start, end),
    )
    return [
        {"date": day.isoformat(), "dept_code": department, "visits": count}
        for day, department, count in cursor.fetchall()
    ]


def load_preview():
    """Returns aggregates only. No default data or partially certified builds."""
    if os.environ.get("OUTPATIENT_PREVIEW_ENABLED") != "1":
        raise OdsError("OUTPATIENT_PREVIEW_DISABLED")
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
            counts = read_counts(cursor, dataset, report["effective_start"], report["effective_end"])
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
            "source": "dwd_visit_detail",
            "batch_date": batch,
            "build_run_id": run_id,
            "available_start_date": min((row["date"] for row in counts), default=report["effective_start"]),
            "available_end_date": max((row["date"] for row in counts), default=report["effective_end"]),
            "department_codes": sorted({row["dept_code"] for row in counts}),
        },
        "counts": counts,
    }
