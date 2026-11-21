"""Explain the two hot read paths on the isolated instance.

Read-only.  Runs ``EXPLAIN`` for

* the five snapshot statements ``clean.load_quality_snapshot_from_cursor``
  sends, once with the bare id column and once with the pre-r5 ``BINARY`` cast,
  so the two plans come from the same connection;
* the outpatient count statements ``outpatient_preview`` sends for the
  whole-hospital view, because that path is the largest single stage budget.

Emits plan shape only: table names, access type, chosen key, estimated rows and
the ``Extra`` flags.  No row values, no credentials, no DML.

    python tests/perf/explain_snapshot.py [--dataset SIM] [--sql]

Exit codes: 0 plans as expected, 2 bare ordering still sorts, 3 dataset
ambiguous or unreadable.
"""

import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "backend")

from etl.jobs import clean_dwd as clean  # noqa: E402

PRINT = print


def statement(table, columns, id_column, *, binary=False):
    """The production statement; ``binary`` reproduces the pre-r5 ordering."""
    order = f"ORDER BY BINARY `{id_column}`" if binary else f"ORDER BY `{id_column}`"
    return ('SELECT ' + ','.join(f'`{col}`' for col in columns)
            + f" FROM `{table}` WHERE dataset_id=%s " + order)


def assert_same_shape():
    """Fail loudly if production stopped matching the statement we explain."""
    import inspect

    source = inspect.getsource(clean.load_quality_snapshot_from_cursor)
    if "WHERE dataset_id=%s ORDER BY `" not in source:
        raise SystemExit("EXPLAIN_STALE production statement changed")
    if "BINARY" in source:
        raise SystemExit("EXPLAIN_STALE production still casts BINARY")


def flags(row):
    extra = row.get("Extra") or ""
    return [part.strip() for part in extra.split(";") if part.strip()]


def plan_rows(cursor, sql, params):
    cursor.execute("EXPLAIN " + sql, params)
    return [{
        "table": row.get("table"),
        "type": row.get("type"),
        "key": row.get("key"),
        "rows": row.get("rows"),
        "flags": flags(row),
        "filesort": "Using filesort" in (row.get("Extra") or ""),
        "temporary": "Using temporary" in (row.get("Extra") or ""),
        "index_only": "Using index" in (row.get("Extra") or ""),
    } for row in cursor.fetchall()]


def dataset_id(cursor, requested):
    cursor.execute("SELECT dataset_id FROM dwd_build_audit")
    found = sorted({row["dataset_id"] for row in cursor.fetchall()})
    if requested:
        if requested not in found:
            raise SystemExit("EXPLAIN_DATASET_UNKNOWN")
        return requested
    if len(found) != 1:
        raise SystemExit("EXPLAIN_DATASET_AMBIGUOUS pass --dataset")
    return found[0]


def cert(cursor, dataset):
    cursor.execute(
        "SELECT batch_date, build_run_id FROM dwd_build_audit WHERE dataset_id=%s", (dataset,))
    row = cursor.fetchone()
    if row is None:
        raise SystemExit("EXPLAIN_DATASET_UNKNOWN")
    return row["batch_date"], row["build_run_id"]


def visit_range(cursor, dataset):
    cursor.execute(
        "SELECT MIN(visit_date), MAX(visit_date) FROM dwd_visit_detail "
        "WHERE dataset_id=%s AND visit_type=%s", (dataset, "OUTPATIENT"))
    row = cursor.fetchone()
    return (row["MIN(visit_date)"], row["MAX(visit_date)"])


def row_estimate(cursor, table):
    cursor.execute(
        "SELECT TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
    value = cursor.fetchone()
    return None if not value else value["TABLE_ROWS"]


def primary_key(cursor, table):
    cursor.execute(
        "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' "
        "ORDER BY SEQ_IN_INDEX", (table,))
    return [row["COLUMN_NAME"] for row in cursor.fetchall()]


class Capture:
    """Collects the statements a production function sends, without a database."""

    def __init__(self):
        self.sent = []

    def execute(self, sql, params=()):
        self.sent.append((sql, params))

    def fetchall(self):
        return []


def read_path_statements(dataset, batch, run_id, start, end):
    """Capture the outpatient count statements for a whole-hospital query."""
    from app import outpatient_preview

    capture = Capture()
    try:
        outpatient_preview._read_ads_counts(capture, dataset, batch, run_id, start, end, None)
    except Exception as error:  # noqa: BLE001 - probe reports instead of failing
        return [], f"{type(error).__name__}"
    return capture.sent, None


def main(argv):
    assert_same_shape()
    show_sql = "--sql" in argv
    requested = argv[argv.index("--dataset") + 1] if "--dataset" in argv else None

    import pymysql
    from etl.config.source_config import connect_database, database_options

    connection = connect_database()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SELECT VERSION()")
            PRINT("EXPLAIN_SERVER " + json.dumps(
                {"database": database_options()["database"],
                 "version": cursor.fetchone()["VERSION()"]}))
            dataset = dataset_id(cursor, requested)
            batch, run_id = cert(cursor, dataset)
            PRINT("EXPLAIN_DATASET " + json.dumps(
                {"dataset": dataset, "batch_date": str(batch)}))

            bare_sorts = 0
            binary_sorts = 0
            index_used = 0
            for table, spec in clean.TABLES.items():
                columns = spec["columns"]
                bare_sql = statement(table, columns, spec["id_column"])
                binary_sql = statement(table, columns, spec["id_column"], binary=True)
                bare = plan_rows(cursor, bare_sql, (dataset,))[0]
                binary = plan_rows(cursor, binary_sql, (dataset,))[0]
                bare_sorts += int(bare["filesort"])
                binary_sorts += int(binary["filesort"])
                index_used += int(bare["key"] is not None)
                if show_sql:
                    PRINT("EXPLAIN_SQL " + json.dumps({"table": table, "sql": bare_sql}))
                PRINT("EXPLAIN_TABLE " + json.dumps({
                    "table": table,
                    "pk": primary_key(cursor, table),
                    "order_by": spec["id_column"],
                    "rows_estimate": row_estimate(cursor, table),
                    "bare": bare,
                    "binary": binary,
                    "filesort_delta": binary["filesort"] and not bare["filesort"],
                }, sort_keys=True))

            snapshot = {"tables": len(clean.TABLES), "bare_filesort": bare_sorts,
                        "binary_filesort": binary_sorts, "bare_key_used": index_used}
            PRINT("EXPLAIN_SNAPSHOT_SUMMARY " + json.dumps(snapshot, sort_keys=True))

            first, last = visit_range(cursor, dataset)
            if first is None or last is None:
                PRINT("EXPLAIN_READ_SKIPPED " + json.dumps({"reason": "no outpatient rows"}))
            else:
                sent, error = read_path_statements(dataset, batch, run_id, first, last)
                if error:
                    PRINT("EXPLAIN_READ_SKIPPED " + json.dumps({"reason": error}))
                for index, (sql, params) in enumerate(sent):
                    if show_sql:
                        PRINT("EXPLAIN_SQL " + json.dumps({"read_path": index, "sql": sql}))
                    PRINT("EXPLAIN_READ " + json.dumps({
                        "statement": index,
                        "kind": "count" if "GROUP BY" in sql else "published",
                        "plan": plan_rows(cursor, sql, params),
                    }, sort_keys=True, default=str))

            verdict = "ok" if bare_sorts == 0 else "bare_ordering_still_sorts"
            PRINT("EXPLAIN_VERDICT " + verdict)
            return 0 if bare_sorts == 0 else 2
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
