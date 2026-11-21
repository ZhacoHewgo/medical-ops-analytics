"""Node 19: reuse rows already read in the same snapshot instead of re-querying."""

from pathlib import Path
import sys

import pytest

from etl.jobs import build_dims as dims
from etl.jobs import build_dwd as build

BACKEND = Path(__file__).resolve().parents[1]
IDENTITY = dict(dataset_id="SIM", clean_run_id="c" * 32, batch_date="2026-01-04",
                rules_version="r1", mapping_version="m1", privacy_key_id="k" * 64)
ROWS = {
    "his_outpatient": [{"visit_date": "2026-01-01"}, {"visit_date": "2026-01-02"}],
    "his_inpatient": [{"admit_date": "2026-01-03", "discharge_date": ""},
                      {"admit_date": "2026-01-04", "discharge_date": None},
                      {"admit_date": "2026-01-05", "discharge_date": "2026-01-06"}],
    "his_charge": [{"fee_date": "2026-01-07"}],
    "emr_record": [{"visit_date": "2026-01-08"}],
    "lis_report": [{"report_date": "2026-01-09"}],
}


def _snapshot():
    tables = {}
    for table, spec in dims.clean.TABLES.items():
        if spec["source"] not in ROWS:
            continue
        tables[table] = dict(source=dict(table=dims.SOURCES[spec["source"]]["table"],
                                         columns=list(dims.SOURCES[spec["source"]]["columns"]),
                                         rows=list(ROWS[spec["source"]])),
                             dwd=dict(table=table, columns=list(spec["columns"]), rows=[]),
                             audit=dict(clean_run_id=IDENTITY['clean_run_id'],
                                        source_sha256='s' * 64, result_sha256='r' * 64))
    return {**IDENTITY, "tables": tables}


def _snapshot_without_rows():
    snapshot = _snapshot()
    for entry in snapshot["tables"].values():
        entry["source"]["rows"] = None
    return snapshot


class NoQuery:
    def __init__(self, allowed=None):
        self.allowed = allowed
        self.sql = []

    def execute(self, sql, params=()):
        if self.allowed is None or self.allowed not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.sql.append(sql)

    def fetchall(self):
        return []


class SourceCursor:
    """Serves the same rows the snapshot holds, shaped like the original SELECTs."""

    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        sql = self.sql[-1]
        if "ods_his_outpatient_visit" in sql:
            return [(row["visit_date"],) for row in ROWS["his_outpatient"]]
        if "ods_his_inpatient_visit" in sql:
            return [(row["admit_date"], row["discharge_date"]) for row in ROWS["his_inpatient"]]
        if "ods_his_charge_detail" in sql:
            return [(row["fee_date"],) for row in ROWS["his_charge"]]
        if "ods_emr_record" in sql:
            return [(row["visit_date"],) for row in ROWS["emr_record"]]
        if "ods_lis_report" in sql:
            return [(row["report_date"],) for row in ROWS["lis_report"]]
        return []


class RowsCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        return list(self.rows)


def test_date_snapshot_reads_only_the_source_missing_from_the_snapshot():
    cursor = NoQuery("ods_finance_excel")
    result = dims._date_snapshot(cursor, _snapshot())
    dates = result["dates"]

    assert len(cursor.sql) == 1 and "ods_finance_excel" in cursor.sql[0]

    assert dates["his_outpatient"] == ["2026-01-01", "2026-01-02"]
    assert result["dates"]["his_inpatient"] == ["2026-01-03", "2026-01-04", "2026-01-05"]
    assert result["dates"]["inpatient_discharge"] == [None, None, "2026-01-06"]
    assert result["dates"]["his_charge"] == ["2026-01-07"]
    assert result["dates"]["emr_record"] == ["2026-01-08"]
    assert result["dates"]["lis_report"] == ["2026-01-09"]


def test_projection_matches_the_original_sql_path():
    projected = dims._date_snapshot(NoQuery("ods_finance_excel"), _snapshot())["dates"]

    cursor = SourceCursor()
    fallback = dims._date_snapshot(cursor, _snapshot_without_rows())["dates"]
    assert len(cursor.sql) == 6
    assert fallback == projected


def test_projection_and_fallback_agree_on_identical_source_rows():
    snapshot = _snapshot()
    projected = dims._date_snapshot(NoQuery("ods_finance_excel"), snapshot)["dates"]

    source_rows = [tuple(row.values()) for row in ROWS["his_outpatient"]]
    assert projected["his_outpatient"] == [row[0] for row in source_rows]
    assert projected == dims._date_snapshot(SourceCursor(), _snapshot_without_rows())["dates"]


def test_previous_certificate_skips_the_audit_read_when_rows_are_supplied(monkeypatch):
    cursor = RowsCursor([])
    monkeypatch.setattr(dims, "_read_report", lambda *a, **k: (None, None))

    assert build._previous_certificate(cursor, "SIM", "2026-01-04", "test", []) is None
    assert not any("dwd_build_audit" in sql for sql in cursor.sql)


def test_previous_certificate_still_reads_the_audit_table_by_default(monkeypatch):
    cursor = RowsCursor([])
    monkeypatch.setattr(dims, "_read_report", lambda *a, **k: (None, None))

    assert build._previous_certificate(cursor, "SIM", "2026-01-04", "test") is None
    assert any("dwd_build_audit" in sql for sql in cursor.sql)


def test_verify_path_reuses_the_rows_it_already_read():
    source = (BACKEND / "etl/jobs/build_dwd.py").read_text(encoding="utf-8")
    assert "_previous_certificate(cursor,dataset_id,batch_date,env,audit_rows,actual_dims)" in source
    assert source.count("dims._read_rows(cursor,dataset) if dim_rows is None else dim_rows") == 2


# --------------------------------------------------------------------------- #
# charge detail: one table, one snapshot, one read
# --------------------------------------------------------------------------- #
CHARGE = "dwd_charge_detail"
CHARGE_COLUMNS = tuple(dims.clean.TABLES[CHARGE]["columns"])


def _charge_snapshot(rows, *, columns=None, with_rows=True):
    entry = {}
    if columns is not None:
        entry["columns"] = list(columns)
    if with_rows:
        entry["rows"] = rows
    return {"tables": {CHARGE: {"dwd": entry}}}


class _NoQuery:
    def execute(self, *args, **kwargs):
        raise AssertionError("the reused read must not reach the database")


def test_charge_reuse_answers_from_the_snapshot_object():
    rows = [{"dataset_id": "SIM", "charge_id": "C01"}]

    assert build._charge_rows(_NoQuery(), _charge_snapshot(rows, columns=CHARGE_COLUMNS),
                              CHARGE_COLUMNS, "SIM") is rows


def _charge_tuple_rows():
    row = {column: None for column in CHARGE_COLUMNS}
    row.update({"dataset_id": "SIM", "charge_id": "C01"})
    return [tuple(row[column] for column in CHARGE_COLUMNS)]


@pytest.mark.parametrize("snapshot", [
    _charge_snapshot(_charge_tuple_rows(), columns=None),                 # no shape declared
    _charge_snapshot(_charge_tuple_rows(), columns=CHARGE_COLUMNS[:-1]),  # column missing
    _charge_snapshot(_charge_tuple_rows(), columns=CHARGE_COLUMNS[::-1]),  # column reordered
    _charge_snapshot(_charge_tuple_rows(), columns=CHARGE_COLUMNS, with_rows=False),
    {"tables": {}},
    {},
])
def test_charge_reuse_falls_back_instead_of_trusting_an_unshared_snapshot(snapshot):
    """A snapshot that does not declare the certified shape must not shorten the proof."""
    cursor = RowsCursor(_charge_tuple_rows())

    rows = build._charge_rows(cursor, snapshot, CHARGE_COLUMNS, "SIM")

    assert len(cursor.sql) == 1 and f"FROM `{CHARGE}`" in cursor.sql[0]
    assert rows == [dict(zip(CHARGE_COLUMNS, row)) for row in _charge_tuple_rows()]


def _selected_columns(sql):
    head = sql.split(" FROM ")[0]
    return tuple(part.strip().strip("`") for part in head[len("SELECT "):].split(","))


def test_snapshot_and_stored_read_select_the_same_charge_columns(monkeypatch):
    """The reuse is only sound while both statements project the same column list."""
    snapshot_sql = next(sql for sql in _snapshot_cursor(monkeypatch).sql if f"FROM `{CHARGE}`" in sql)

    cursor = _CapturingCursor()
    build._read(cursor, CHARGE, CHARGE_COLUMNS, "SIM")
    read_sql = cursor.sql[0]

    assert _selected_columns(snapshot_sql) == _selected_columns(read_sql) == CHARGE_COLUMNS
    # Only the ordering that keeps the snapshot on the clustered index may differ.
    assert snapshot_sql.endswith(f"ORDER BY `{dims.clean.TABLES[CHARGE]['id_column']}`")
    assert "ORDER BY" not in read_sql.upper()


def test_verify_path_reads_the_charge_table_once():
    source = (BACKEND / "etl/jobs/build_dwd.py").read_text(encoding="utf-8")
    body = source.split("def verify_build_freshness_from_cursor")[1].split("\ndef ")[0]

    assert "stored_charge=_charge_rows(cursor,snapshot,columns,dataset_id)" in body
    assert "_read(cursor,'dwd_charge_detail'" not in body
    assert "_read(cursor,VISIT,VISIT_COLUMNS,dataset_id)" in body


# --------------------------------------------------------------------------- #
# charge reuse probe: its transaction preamble must be legal on a real server
# --------------------------------------------------------------------------- #
def _charge_probe():
    """Load the charge reuse probe without making it a package import."""
    import importlib.util

    path = BACKEND.parent / "tests/perf/charge_reuse.py"
    spec = importlib.util.spec_from_file_location("charge_reuse_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ProbeConnection:
    """Mirrors the two server rules the probe runs under.

    autocommit is off, so the first statement opens a transaction, and MySQL
    rejects SET TRANSACTION ISOLATION LEVEL with 1568 while any transaction is
    open -- including the one a metadata SELECT started.
    """

    def __init__(self, rows, mode=("REPEATABLE-READ", 1)):
        self.rows = rows
        self.mode = mode
        self.events = []
        self.in_transaction = False
        self.closed = False

    def cursor(self):
        return _ProbeCursor(self)

    def rollback(self):
        self.events.append("ROLLBACK")
        self.in_transaction = False

    def close(self):
        self.closed = True


class _ProbeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        from pymysql.err import OperationalError

        if sql.upper().startswith("SET TRANSACTION") and self.connection.in_transaction:
            raise OperationalError(1568, "Transaction characteristics can't be changed "
                                         "while a transaction is in progress")
        self.connection.events.append(sql)
        self.connection.in_transaction = True
        self._rows = self._answer(sql)

    def _answer(self, sql):
        if sql == "SELECT VERSION()":
            return [("8.4.13",)]
        if sql.startswith("SELECT dataset_id FROM dwd_build_audit"):
            return [("SIM",)]
        if sql.startswith("SELECT batch_date FROM dwd_build_audit"):
            return [("2026-01-04",)]
        if "@@transaction_isolation" in sql:
            return [self.connection.mode]
        if sql.upper().startswith("SET TRANSACTION") or sql.upper().startswith("START TRANSACTION"):
            return []
        if f"`{CHARGE}`" in sql:
            return [tuple(row[column] for column in _selected_columns(sql))
                    for row in self.connection.rows]
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _probe_rows():
    from decimal import Decimal

    first = {column: None for column in CHARGE_COLUMNS}
    first.update({"dataset_id": "SIM", "charge_id": "C01", "amount": Decimal("12.30")})
    return [first, dict(first, charge_id="C02", amount=None)]


def _probe_snapshot(rows):
    """Stands in for the quality snapshot: one ordered read, same columns as production."""

    def load(cursor, batch, *, dataset_id):
        cursor.execute("SELECT " + ",".join(f"`{column}`" for column in CHARGE_COLUMNS)
                       + f" FROM `{CHARGE}` WHERE dataset_id=%s ORDER BY "
                       + f"`{dims.clean.TABLES[CHARGE]['id_column']}`", (dataset_id,))
        served = [dict(zip(CHARGE_COLUMNS, row)) for row in cursor.fetchall()]
        return {"tables": {CHARGE: {"dwd": {"columns": list(CHARGE_COLUMNS), "rows": served}}}}

    return load


def _probe_wiring(monkeypatch, connection):
    from etl.config import source_config as sources

    monkeypatch.setattr(sources, "connect_database", lambda **kwargs: connection)
    monkeypatch.setattr(sources, "database_options", lambda **kwargs: {"database": "hospital_test"})


def test_probe_stub_reproduces_the_server_rule_it_enforces():
    """If the fake tolerated this, the ordering test below would prove nothing."""
    from pymysql.err import OperationalError

    probe = _charge_probe()
    connection = _ProbeConnection(_probe_rows())
    cursor = connection.cursor()
    probe.dataset_id(cursor, "SIM")
    assert connection.in_transaction

    with pytest.raises(OperationalError) as error:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    assert error.value.args[0] == 1568


def test_open_read_only_snapshot_ends_the_metadata_read_first():
    probe = _charge_probe()
    connection = _ProbeConnection(_probe_rows())
    cursor = connection.cursor()
    probe.dataset_id(cursor, "SIM")

    assert probe.open_read_only_snapshot(connection, cursor) == ("REPEATABLE-READ", True)
    assert connection.events[1:4] == ["ROLLBACK",
                                      "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                                      "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"]
    assert connection.events[4].startswith("SELECT @@transaction_isolation")
    assert connection.in_transaction


def test_charge_probe_declares_the_snapshot_after_closing_the_metadata_read(monkeypatch):
    probe = _charge_probe()
    rows = _probe_rows()
    connection = _ProbeConnection(rows)
    _probe_wiring(monkeypatch, connection)

    assert probe.main(["--dataset", "SIM"], snapshot_loader=_probe_snapshot(rows)) == 0

    events = connection.events
    assert events[0] == "SELECT VERSION()"
    assert events[3] == "ROLLBACK"
    assert events[4] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
    assert events[5] == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
    assert sum(event.startswith("SET TRANSACTION") for event in events) == 1
    # Snapshot read plus the stored read the reuse replaces; nothing from the reuse.
    assert len([event for event in events if f"FROM `{CHARGE}`" in event]) == 2
    assert connection.closed


@pytest.mark.parametrize("mode", [("REPEATABLE-READ", 0), ("READ-COMMITTED", 1)])
def test_charge_probe_refuses_a_transaction_that_is_not_read_only_repeatable_read(mode, monkeypatch):
    probe = _charge_probe()
    rows = _probe_rows()
    connection = _ProbeConnection(rows, mode=mode)
    _probe_wiring(monkeypatch, connection)

    assert probe.main(["--dataset", "SIM"], snapshot_loader=_probe_snapshot(rows)) == 4
    assert not any(f"FROM `{CHARGE}`" in event for event in connection.events)


def _audit_row():
    from datetime import datetime
    tables = {name: 0 for name in dims.clean.TABLES}
    stats = dict(visit_rows=0, charge_rows=0, missing_total_rows=0, missing_component_rows=0,
                 negative_residual_rows=0, missing_calendar_span_rows=0,
                 disease_not_applicable_rows=0,
                 ods_rows=dict(tables), base_rows=dict(tables),
                 duplicate_removed=dict(tables), quarantined_rows=dict(tables),
                 association_counts={key: dict(slots=0, matched=0, not_applicable=0)
                                     for key in ("department", "date", "disease", "fee_type", "visit")})
    return dict(dataset_id="SIM", model_version=build.MODEL_VERSION,
                mapping_version=dims.MAPPING_VERSION, build_run_id="1" * 8 + "-1111-4111-8111-" + "1" * 12,
                clean_run_id="2" * 8 + "-2222-4222-8222-" + "2" * 12,
                batch_date=__import__("datetime").date(2026, 1, 4),
                requested_start=None, requested_end=None,
                effective_start=__import__("datetime").date(2026, 1, 1),
                effective_end=__import__("datetime").date(2026, 1, 3),
                updated_at=datetime(2026, 1, 4, 2, 0, 0), privacy_key_id="a" * 64,
                build_scope="ALL", input_sha256="b" * 64, dim_sha256="c" * 64,
                visit_sha256="d" * 64, charge_sha256="e" * 64, stats_json=stats)


def test_previous_certificate_reuses_supplied_dim_rows(monkeypatch):
    certificate = dict(dataset_id="SIM", template_version="v1", date_min="2026-01-01",
                       date_max="2026-01-03", required_date_start="2026-01-01",
                       required_date_end="2026-01-03", row_counts={})
    dim_rows = {name: [] for name in dims._SPECS}
    monkeypatch.setattr(build, "_validate_stats_shape", lambda *a: None)
    monkeypatch.setattr(dims, "_dimension_range", lambda *a: certificate)
    monkeypatch.setattr(dims, "_bridge_certificate_rows", lambda cert, dataset: dim_rows)

    result = build._previous_certificate(NoQuery(), "SIM", "2026-01-04", "test",
                                         [_audit_row()], dim_rows)
    assert result["dataset_id"] == "SIM"


# --------------------------------------------------------------------------- #
# snapshot reads: ORDER BY must be servable by the clustered index
# --------------------------------------------------------------------------- #
DDL = BACKEND.parent / "sql/dwd/dwd_tables.sql"


def _primary_keys():
    """Parse PRIMARY KEY columns per table so the ORDER BY stays index-servable."""
    keys, table = {}, None
    for line in DDL.read_text(encoding="utf-8").splitlines():
        head = line.strip()
        if head.startswith("CREATE TABLE"):
            table = head.split("`")[1]
        elif head.startswith("PRIMARY KEY") and table:
            inner = head[head.index("(") + 1:head.index(")")]
            keys[table] = tuple(part.strip().strip("`") for part in inner.split(","))
    return keys


class _CapturingCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        return []


def _snapshot_cursor(monkeypatch):
    from datetime import date
    from etl.jobs import clean_dwd as clean

    audit = dict(clean_run_id="c" * 32, batch_date=date(2026, 1, 4), rules_version="r1",
                 mapping_version="m1", privacy_key_id="k" * 64,
                 source_sha256="s" * 64, result_sha256="r" * 64)
    monkeypatch.setattr(clean, "_config", lambda: dict(rules_version="r1", mapping_version="m1"))
    monkeypatch.setattr(clean, "_registry", lambda cursor, dataset: ("SIM", "2026-01-04", None))
    monkeypatch.setattr(clean, "_verify", lambda cursor: None)
    monkeypatch.setattr(clean, "_audits", lambda cursor, dataset: {t: dict(audit) for t in clean.TABLES})
    monkeypatch.setattr(clean, "_source", lambda cursor, table, dataset: [])
    cursor = _CapturingCursor()
    clean.load_quality_snapshot_from_cursor(cursor, "2026-01-04", dataset_id="SIM")
    return cursor


def test_snapshot_orders_by_the_primary_key_column_without_a_binary_cast(monkeypatch):
    from etl.jobs import clean_dwd as clean

    cursor = _snapshot_cursor(monkeypatch)
    keys = _primary_keys()

    assert len(cursor.sql) == len(clean.TABLES)
    for sql in cursor.sql:
        assert "BINARY" not in sql.upper(), f"cast defeats index order: {sql}"
        table = sql.split("FROM `")[1].split("`")[0]
        column = clean.TABLES[table]["id_column"]
        assert sql.endswith(f"ORDER BY `{column}`"), sql
        assert keys[table][1] == column, f"{table} ORDER BY must stay on the second PK column"


def test_snapshot_id_columns_cannot_carry_padding(monkeypatch):
    """utf8mb4_bin is PAD SPACE; business ids are rejected unless already stripped."""
    from etl.jobs import quality_check as quality

    for spec in dims.clean.TABLES.values():
        assert spec["id_column"]
    assert quality._business_id("SIM_VISIT_01")
    assert not quality._business_id("SIM_VISIT_01 ")


def _probe():
    """Load the EXPLAIN probe without making it a package import."""
    import importlib.util

    path = BACKEND.parent / "tests/perf/explain_snapshot.py"
    spec = importlib.util.spec_from_file_location("explain_snapshot_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explain_probe_explains_the_production_statement(monkeypatch):
    """The probe must EXPLAIN the statement the snapshot actually sends."""
    from etl.jobs import clean_dwd as clean

    probe = _probe()
    assert probe.assert_same_shape() is None

    captured = _snapshot_cursor(monkeypatch).sql
    expected = [probe.statement(table, spec["columns"], spec["id_column"])
                for table, spec in clean.TABLES.items()]
    assert captured == expected
    assert all(sql.endswith(f"ORDER BY `{clean.TABLES[sql.split('FROM `')[1].split('`')[0]]['id_column']}`")
               for sql in captured)


def test_explain_probe_reaches_the_outpatient_count_statement():
    """The read-path half must capture the real GROUP BY, not a paraphrase."""
    probe = _probe()
    sent, error = probe.read_path_statements("SIM", "2026-01-04", "b" * 8,
                                             "2026-01-01", "2026-01-31")
    assert error is None and len(sent) == 2

    count_sql, count_params = sent[0]
    assert "GROUP BY visit_date, dept_code ORDER BY visit_date, dept_code" in count_sql
    assert "visit_type = %s" in count_sql and "BINARY" not in count_sql.upper()
    assert count_params == ("SIM", "OUTPATIENT", "2026-01-01", "2026-01-31")

    published_sql = sent[1][0]
    assert "ads_outpatient_theme" in published_sql and "dws_dept_daily" in published_sql


def test_rows_digest_and_conserve_are_independent_of_input_order():
    rows = [dict(visit_id="SIM_A", amount=1), dict(visit_id="SIM_B", amount=2)]
    columns = ("visit_id", "amount")
    assert build.rows_digest(list(columns), rows, ("visit_id",)) == \
        build.rows_digest(list(columns), rows[::-1], ("visit_id",))

    build._conserve(columns, rows, rows[::-1], ("visit_id",))

