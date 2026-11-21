"""Node 19: shared-lock access audit and per-request freshness verification."""

import ast
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import outpatient_preview as preview
from etl.config.source_config import OdsError
from etl.jobs import build_dwd

BACKEND = Path(__file__).resolve().parents[1]
ACCOUNT = namedtuple("Account", "user_id username password_hash role department enabled revision")
REVISION = datetime(2026, 1, 4, 12, 0, 0)
BUILD = "b171a123-514b-4566-a955-e29c4794d21e"
BATCH = "2026-01-04"
REPORT = {"build_run_id": BUILD, "effective_start": "2026-01-01", "effective_end": "2026-01-03"}


def _function(path, name, namespace):
    source = Path(path).read_text(encoding="utf-8")
    body = [node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(body) == 1, (path, name)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def _constant(path, name):
    source = Path(path).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError((path, name))


class FakeCursor:
    def __init__(self, events, *, row=None, rows=()):
        self.events = events
        self.row = row
        self.rows = list(rows)
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.events.append(("sql", sql))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.events = cursor.events

    def cursor(self):
        return self._cursor

    def begin(self):
        self.events.append(("begin",))

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))


def _audit_harness(row):
    events = []
    cursor = FakeCursor(events, row=row)
    connection = FakeConnection(cursor)

    @contextmanager
    def database_cursor(code, *, write=False):
        connection.begin()
        try:
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    namespace = {
        "SHARED": _constant(BACKEND / "app/core/auth_store.py", "SHARED"),
        "DB_DOWN": "AUTH_DB_UNAVAILABLE", "Account": ACCOUNT,
        "database_cursor": database_cursor, "HTTPException": HTTPException,
        "OdsError": OdsError, "unauthorized": lambda: HTTPException(401, "Authentication required"),
    }
    _function(BACKEND / "app/core/auth_store.py", "read_user", namespace)
    audit = _function(BACKEND / "app/api/routers/outpatient_router.py", "audit_access", namespace)
    return audit, namespace, events, cursor


def _account(enabled=True, revision=REVISION, role="analyst"):
    return ACCOUNT(7, "alice", b"x", role, None, enabled, revision)


def test_audit_takes_shared_lock_in_the_same_transaction_as_the_insert():
    audit, _, events, cursor = _audit_harness((7, "alice", b"x", "analyst", None, 1, REVISION))
    audit(_account(), "query", None, 200)

    select_sql, insert_sql = cursor.sql
    assert select_sql.endswith(" FOR SHARE")
    assert "FOR UPDATE" not in select_sql
    assert insert_sql.startswith("INSERT INTO auth_data_access")
    assert [kind for kind, *_ in events] == ["begin", "sql", "sql", "commit", "close"]


@pytest.mark.parametrize("row,user", [
    ((7, "alice", b"x", "analyst", None, 0, REVISION), _account()),
    ((7, "alice", b"x", "analyst", None, 1, datetime(2026, 1, 5)), _account()),
    ((7, "alice", b"x", "admin", None, 1, REVISION), _account()),
    (None, _account()),
])
def test_revoked_or_changed_account_denies_without_audit_insert(row, user):
    audit, _, events, cursor = _audit_harness(row)
    with pytest.raises(HTTPException) as rejected:
        audit(user, "query", None, 200)

    assert rejected.value.status_code == 401
    assert len(cursor.sql) == 1
    assert cursor.sql[0].endswith(" FOR SHARE")
    assert [kind for kind, *_ in events] == ["begin", "sql", "commit", "close"]


def test_audit_lock_is_reacquired_on_every_request():
    audit, _, _, cursor = _audit_harness((7, "alice", b"x", "analyst", None, 1, REVISION))
    for _ in range(3):
        audit(_account(), "query", None, 200)

    assert len(cursor.sql) == 6
    assert all(sql.endswith(" FOR SHARE") for sql in cursor.sql[0::2])
    assert all(sql.startswith("INSERT INTO auth_data_access") for sql in cursor.sql[1::2])


def test_plain_account_read_still_takes_no_lock():
    events = []
    cursor = FakeCursor(events, row=(7, "alice", b"x", "analyst", None, 1, REVISION))
    namespace = {"Account": ACCOUNT, "SHARED": _constant(BACKEND / "app/core/auth_store.py", "SHARED")}
    read_user = _function(BACKEND / "app/core/auth_store.py", "read_user", namespace)

    assert read_user(cursor, user_id=7).user_id == 7
    assert not cursor.sql[0].endswith((" FOR UPDATE", " FOR SHARE"))


def test_management_write_keeps_exclusive_lock():
    events = []
    cursor = FakeCursor(events, row=(7, "alice", b"x", "analyst", None, 1, REVISION))
    namespace = {"Account": ACCOUNT, "SHARED": _constant(BACKEND / "app/core/auth_store.py", "SHARED")}
    _function(BACKEND / "app/core/auth_store.py", "read_user", namespace)
    update_user = _function(BACKEND / "app/core/auth_store.py", "update_user", namespace)

    update_user(cursor, 7, "analyst", None, True)
    assert cursor.sql[0].startswith("UPDATE auth_users SET")
    assert cursor.sql[1].endswith(" FOR UPDATE")


def test_management_guard_keeps_exclusive_lock():
    events = []
    cursor = FakeCursor(events, row=(7, "admin", b"x", "admin", None, 1, REVISION))
    namespace = {"Account": ACCOUNT, "SHARED": _constant(BACKEND / "app/core/auth_store.py", "SHARED")}
    _function(BACKEND / "app/core/auth_store.py", "read_user", namespace)
    guard = _function(BACKEND / "app/api/routers/auth_router.py", "_current_admin", namespace)

    actor = ACCOUNT(7, "admin", b"x", "admin", None, True, REVISION)
    assert guard(cursor, actor, lock=True) is True
    assert cursor.sql[0].endswith(" FOR UPDATE")


class ReadCursor:
    def __init__(self, track):
        self.track = track
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReadConnection:
    def __init__(self, track):
        self.track = track
        self.cursor_obj = ReadCursor(track)

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.track["rollbacks"] += 1

    def close(self):
        pass


@pytest.fixture
def load_env(monkeypatch):
    track = {"calls": [], "sql_at_verify": [], "fail_on": None, "rollbacks": 0}
    monkeypatch.setenv("OUTPATIENT_PREVIEW_DATASET_ID", "COURSE_TEST")
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BATCH_DATE", BATCH)
    monkeypatch.setenv("OUTPATIENT_PREVIEW_BUILD_RUN_ID", BUILD)
    monkeypatch.setenv("OUTPATIENT_PREVIEW_ENV", "test")
    monkeypatch.setattr(preview, "database_options", lambda: {"database": "hospital_test"})
    monkeypatch.setattr(preview, "connect_database", lambda: ReadConnection(track))
    monkeypatch.setattr(preview, "read_current_report", lambda *_: dict(REPORT))

    def verify(cursor, *, dataset_id, batch_date, env, report):
        track["calls"].append((dataset_id, batch_date, env, report["build_run_id"]))
        track["sql_at_verify"].append(list(cursor.sql))
        if track["fail_on"] is not None and len(track["calls"]) >= track["fail_on"]:
            raise OdsError("OUTPATIENT_PREVIEW_STALE")
        return {"build_run_id": BUILD, "dataset_id": dataset_id,
                "batch_date": batch_date, "gate_status": "PASS"}

    monkeypatch.setattr(build_dwd, "verify_build_freshness_from_cursor", verify)
    return track


def test_every_request_verifies_freshness(load_env):
    for _ in range(3):
        preview.load_ads()

    assert len(load_env["calls"]) == 3
    assert load_env["calls"] == [("COURSE_TEST", BATCH, "test", BUILD)] * 3
    assert all("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" in sql
               for sql in load_env["sql_at_verify"])


def test_changed_upstream_is_rejected_even_when_the_report_is_unchanged(load_env):
    preview.load_ads()
    load_env["fail_on"] = 2
    with pytest.raises(OdsError):
        preview.load_ads()

    assert len(load_env["calls"]) == 2


def test_no_cached_freshness_path_remains():
    assert not hasattr(preview, "verified_identity")
    assert not hasattr(preview, "freshness_ttl")
