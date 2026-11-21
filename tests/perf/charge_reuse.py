"""Prove the two charge detail reads resolve to one read, inside one snapshot.

Node 19 reads ``dwd_charge_detail`` twice per authenticated request: once in
``clean.load_quality_snapshot_from_cursor`` (the snapshot rows) and once in
``verify_build_freshness_from_cursor`` (the stored rows that are conserved and
hashed).  This probe opens a single ``REPEATABLE READ`` read-only transaction,
performs both reads, and compares them field by field, so the reuse in
``build_dwd._charge_rows`` is accepted or reverted on evidence.

Read-only: one transaction, no DML, no file writes.  Emits counts, digests and
booleans only -- no row values, no credentials.

The metadata reads that pick the dataset run first and are closed again before
the snapshot is declared: ``connect_database()`` hands over ``autocommit=False``,
so the first SELECT already opens a transaction, and MySQL refuses
``SET TRANSACTION ISOLATION LEVEL`` with 1568 while one is open.  The snapshot
and both charge reads still share one read-only REPEATABLE READ transaction, and
its characteristics are read back inside it.

    python tests/perf/charge_reuse.py [--dataset SIM] [--sql]

Exit codes: 0 the reads are equal and the reuse sends no statement,
2 they differ (revert the reuse), 3 dataset or snapshot unavailable,
4 the shared transaction is not read-only REPEATABLE READ.
"""

import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "backend")

from etl.jobs import build_dwd as build  # noqa: E402
from etl.jobs import clean_dwd as clean  # noqa: E402

PRINT = print
TABLE = "dwd_charge_detail"
KEY = ("dataset_id", "charge_id")


class Recorder:
    """Forwards to the real cursor and records the statements sent through it."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.sent = []

    def execute(self, sql, params=()):
        self.sent.append(sql)
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def take(self):
        sent, self.sent = self.sent, []
        return sent


def touches(sql, table=TABLE):
    return table in sql and sql.lstrip().upper().startswith("SELECT")


def dataset_id(cursor, requested):
    cursor.execute("SELECT dataset_id FROM dwd_build_audit")
    found = sorted({row[0] for row in cursor.fetchall()})
    if requested:
        if requested not in found:
            raise SystemExit("CHARGE_REUSE_DATASET_UNKNOWN")
        return requested
    if len(found) != 1:
        raise SystemExit("CHARGE_REUSE_DATASET_AMBIGUOUS pass --dataset")
    return found[0]


def batch_of(cursor, dataset):
    cursor.execute("SELECT batch_date FROM dwd_build_audit WHERE dataset_id=%s", (dataset,))
    row = cursor.fetchone()
    if row is None:
        raise SystemExit("CHARGE_REUSE_DATASET_UNKNOWN")
    return str(row[0])


def open_read_only_snapshot(connection, cursor):
    """Close the metadata read, declare the snapshot, and report its characteristics.

    MySQL fixes transaction characteristics at START TRANSACTION: SET TRANSACTION
    ISOLATION LEVEL answers 1568 while a transaction is open, and the metadata
    SELECTs above opened one implicitly.  That rollback ends a read that wrote
    nothing and restores the session state connect_database() returned, so the
    declaration below applies to the transaction that follows.  The isolation and
    access mode are then read back on this same connection, inside that
    transaction, so the shared snapshot is shown rather than assumed.
    """
    connection.rollback()
    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
    cursor.execute("SELECT @@transaction_isolation, @@transaction_read_only")
    isolation, read_only = cursor.fetchone()
    mode = str(isolation).upper().replace("_", "-")
    try:
        read_only = None if read_only is None else bool(int(read_only))
    except (TypeError, ValueError):
        read_only = None  # Unknown representation: report the mode alone, never guess.
    return mode, read_only


def equal_rows(left, right, columns):
    """Field-for-field comparison in primary key order, independent of fetch order."""
    if len(left) != len(right):
        return False, "row_count"
    left_sorted = sorted(left, key=lambda row: tuple(str(row[c]).encode("utf-8") for c in KEY))
    right_sorted = sorted(right, key=lambda row: tuple(str(row[c]).encode("utf-8") for c in KEY))
    for index, (one, other) in enumerate(zip(left_sorted, right_sorted)):
        if [build._typed(one[c]) for c in columns] != [build._typed(other[c]) for c in columns]:
            return False, f"row_{index}"
    return True, None


def assert_same_cursor():
    """Fail loudly if production stops shaping rows the way this probe reads them."""
    import inspect

    source = inspect.getsource(clean.load_quality_snapshot_from_cursor)
    if "dict(zip(columns, row))" not in source:
        raise SystemExit("CHARGE_REUSE_STALE production row shaping changed")


def conserve_both_ways(columns, left, right):
    """The reconciliation must accept the pair in either argument order."""
    try:
        build._conserve(columns, left, right, KEY)
        build._conserve(columns, right, left, KEY)
    except build.OdsError:
        return False
    return True


def main(argv, snapshot_loader=None):
    show_sql = "--sql" in argv
    requested = argv[argv.index("--dataset") + 1] if "--dataset" in argv else None
    # Injectable only so the interface test can drive this file with a stub
    # cursor; a real run always takes the production loader.
    load_snapshot = snapshot_loader or clean.load_quality_snapshot_from_cursor

    from etl.config.source_config import connect_database, database_options

    assert_same_cursor()
    connection = connect_database()
    try:
        # Plain tuple cursor: production builds rows with dict(zip(columns, row)),
        # so a DictCursor here would compare column names against themselves.
        with connection.cursor() as raw:
            raw.execute("SELECT VERSION()")
            PRINT("CHARGE_REUSE_SERVER " + json.dumps(
                {"database": database_options()["database"], "version": raw.fetchone()[0]}))
            dataset = dataset_id(raw, requested)
            batch = batch_of(raw, dataset)
            PRINT("CHARGE_REUSE_DATASET " + json.dumps({"dataset": dataset, "batch_date": batch}))

            # One stable snapshot for both reads, matching every production caller.
            mode, read_only = open_read_only_snapshot(connection, raw)
            PRINT("CHARGE_REUSE_TX " + json.dumps({"isolation": mode, "read_only": read_only}))
            if mode != "REPEATABLE-READ" or read_only is False:
                PRINT("CHARGE_REUSE_VERDICT transaction_not_read_only_repeatable_read")
                return 4
            cursor = Recorder(raw)

            try:
                snapshot = load_snapshot(cursor, batch, dataset_id=dataset)
            except Exception as error:  # noqa: BLE001 - probe reports instead of failing
                PRINT("CHARGE_REUSE_VERDICT snapshot_unavailable " + type(error).__name__)
                return 3
            snapshot_sql = [sql for sql in cursor.take() if touches(sql)]

            columns = tuple(clean.TABLES[TABLE]["columns"])
            entry = snapshot["tables"][TABLE]["dwd"]
            snapshot_rows = entry["rows"]
            declared = tuple(entry.get("columns") or ())

            # Second read: exactly what verify_build_freshness_from_cursor used to send.
            read_rows = build._read(cursor, TABLE, columns, dataset)
            read_sql = cursor.take()

            # Production path: must answer from the snapshot and send nothing.
            reused = build._charge_rows(cursor, snapshot, columns, dataset)
            reuse_sql = cursor.take()
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()

    shape = {
        "columns": len(columns),
        "declared_columns": len(declared),
        "columns_equal": declared == columns,
        "snapshot_statements": len(snapshot_sql),
        "read_statements": len(read_sql),
        "reuse_statements": len(reuse_sql),
        "snapshot_ordered": (snapshot_sql[0].rstrip().endswith(f"ORDER BY `{clean.TABLES[TABLE]['id_column']}`")
                             if snapshot_sql else None),
        "read_ordered": ("ORDER BY" in read_sql[0].upper()) if read_sql else None,
    }
    PRINT("CHARGE_REUSE_SHAPE " + json.dumps(shape, sort_keys=True))
    if show_sql:
        for label, sent in (("snapshot", snapshot_sql), ("read", read_sql), ("reuse", reuse_sql)):
            PRINT("CHARGE_REUSE_SQL " + json.dumps({"path": label, "sql": sent}))

    same_content, first_diff = equal_rows(snapshot_rows, read_rows, columns)

    # Order-insensitive equality carried one step further: cache order differs,
    # so compare the consumption order each guard actually builds.
    def canonical(rows):
        return [[build._typed(row[c]) for c in columns] for row in
                sorted(rows, key=lambda row: tuple(str(row[c]).encode("utf-8") for c in KEY))]

    digest_equal = (build.rows_digest(columns, snapshot_rows, KEY)
                    == build.rows_digest(columns, read_rows, KEY))
    reuse_equal, reuse_diff = equal_rows(reused, read_rows, columns)

    rows = {
        "snapshot_rows": len(snapshot_rows),
        "read_rows": len(read_rows),
        "same_content": same_content,
        "first_difference": first_diff,
        "canonical_equal": canonical(snapshot_rows) == canonical(read_rows),
        "digest_equal": digest_equal,
        "conserve_both_ways": conserve_both_ways(columns, snapshot_rows, read_rows),
        "reuse_equal": reuse_equal,
        "reuse_difference": reuse_diff,
        "reuse_is_snapshot_object": reused is snapshot_rows,
    }
    PRINT("CHARGE_REUSE_ROWS " + json.dumps(rows, sort_keys=True))

    problems = []
    if not shape["columns_equal"]:
        problems.append("column_tuple_differs")
    if shape["read_statements"] != 1:
        problems.append("second_read_not_single")
    if shape["reuse_statements"] != 0:
        problems.append("reuse_still_queries")
    if not same_content:
        problems.append("rows_differ")
    if not digest_equal:
        problems.append("digest_differs")
    if not rows["conserve_both_ways"]:
        problems.append("conserve_rejects")
    if not reuse_equal:
        problems.append("reuse_rows_differ")

    PRINT("CHARGE_REUSE_VERDICT " + ("ok" if not problems else ",".join(problems)))
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
