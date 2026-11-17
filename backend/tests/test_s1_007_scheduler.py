"""S6 isolated scheduling checks; no real database credentials or writes."""

import importlib
import csv
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4

import pytest

from etl.config.source_config import OdsError
from etl.scheduler import scheduler as s


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, request):
    monkeypatch.setattr(s, "_TASKS", {})
    monkeypatch.setattr(s, "_SCHEDULERS", {})
    monkeypatch.setattr(s, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(s.time, "sleep", lambda seconds: None)
    if request.node.name != "test_mysql_pipeline_short_dispatch":
        monkeypatch.setenv("ODS_DATABASE_URL", "mysql://SIM:SIM@127.0.0.1:13306/hospital_test")


def events():
    return [json.loads(line) for path in sorted(s.LOG_ROOT.glob("test/etl_*.log"))
            for line in path.read_text().splitlines()]


def fail(code):
    raise OdsError(code)


def test_order_result_and_one_context():
    called = []
    for name in ("first", "second"):
        s.register_task(name, lambda name=name: (called.append(name) or {"his_charge": 2}))
    assert s.run_all(env="test") == {"first": {"his_charge": 2}, "second": {"his_charge": 2}}
    assert called == ["first", "second"]
    rows = events()
    assert len({row["run_id"] for row in rows}) == 1
    assert all(row["attempt"] == 1 and row["env"] == "test" for row in rows)
    assert all(row["cost_ms"] >= 0 for row in rows)
    assert len(list(s.LOG_ROOT.glob("test/etl_test_*_run_all_*.log"))) == 1


@pytest.mark.parametrize("key", ["rows", "FAKE_SECRET mysql://SIM:SIM@host/hospital_test"])
def test_generic_task_original_result(key):
    result = {key: 3}
    s.register_task("job", lambda: result)
    assert s.run_task("job", env="test") is result
    assert "FAKE_SECRET" not in json.dumps(events()) and "mysql://" not in json.dumps(events())


def test_scheduled_yesterday_frozen_for_retries_new_for_next_run(monkeypatch):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    clock_calls, ranges = [], []

    class Clock:
        @staticmethod
        def now(tz):
            assert str(tz) == "Asia/Shanghai"
            clock_calls.append(1)
            return datetime(2026, 11, 16 + len(clock_calls) - 1, 2, tzinfo=tz)

    def extract(start, end, *, on_event):
        ranges.append((start, end))
        if len(ranges) == 1:
            raise OdsError("ODS_DATABASE_CONNECT_FAILED")
        return dict.fromkeys(s.SOURCES, 0)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(extractor, "run_extract_all", extract, raising=False)
    s.register_task("extract_ods", cli._extract_task(None, None, True))
    s.run_all(env="test")
    s.run_all(env="test")
    assert ranges == [("2026-11-15", "2026-11-15")] * 2 + [("2026-11-16", "2026-11-16")]


@pytest.mark.parametrize("bad", [True, False, -1, 4, 1.0, "3", None])
def test_retry_limit_invalid(bad):
    s.register_task("extract_ods", lambda: {})
    with pytest.raises(s.EtlError, match="^ETL_RETRY_INVALID$"):
        s.run_task("extract_ods", bad, env="test")


@pytest.mark.parametrize("code,limit,expected", [
    ("ODS_RANGE_LOAD_FAILED", 3, 4), ("ODS_DATABASE_CONNECT_FAILED", 3, 4),
    ("ODS_LOAD_FAILED", 3, 4), ("ODS_DATABASE_CLOSE_FAILED", 3, 4),
    ("ODS_SOURCE_DATE_INVALID", 3, 1), ("ODS_RANGE_REBASE_REQUIRED", 3, 1),
    ("ODS_UNKNOWN_FAKE_SECRET", 3, 1), ("ODS_RANGE_LOAD_FAILED", 0, 1),
])
def test_retry_classification(code, limit, expected, monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(s.time, "sleep", waits.append)
    s.register_task("extract_ods", lambda: (calls.append(1) or fail(code)))
    with pytest.raises(s.EtlError):
        s.run_task("extract_ods", limit, env="test")
    assert len(calls) == expected
    assert waits == [1] * (expected - 1)
    assert [e["attempt"] for e in events() if e["status"] == "START"] == list(range(1, expected + 1))
    assert "FAKE_SECRET" not in json.dumps(events())


def test_third_attempt_success_and_partial_source_events(monkeypatch):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    calls = []

    def extract(start, end, *, on_event):
        calls.append((start, end))
        on_event("his_outpatient", "START", None, 0)
        on_event("his_outpatient", "SUCCESS", 2, 1)
        if len(calls) < 3:
            on_event("his_charge", "FAILED", None, 1)
            raise OdsError("ODS_RANGE_LOAD_FAILED")
        return dict.fromkeys(s.SOURCES, 0) | {"his_outpatient": 2}

    monkeypatch.setattr(extractor, "run_extract_all", extract, raising=False)
    assert cli.main(["--env", "test"]) == 0
    assert calls == [(None, None)] * 3
    rows = events()
    assert [e["attempt"] for e in rows if e["status"] == "PARTIAL"] == [1, 2]
    assert all(e["committed_sources"] == {"his_outpatient": 2}
               for e in rows if e["status"] == "PARTIAL")
    assert all(e["rows"] is None and e["code"] != "ETL_OK"
               for e in rows if e["status"] in {"PARTIAL", "FAILED"})
    assert len({e["run_id"] for e in rows}) == 1


def test_dependency_failfast():
    called = []
    s.register_task("first", lambda: fail("ODS_RANGE_INVALID"))
    s.register_task("second", lambda: called.append(1))
    with pytest.raises(s.EtlError, match="^ODS_RANGE_INVALID$"):
        s.run_all(env="test")
    assert called == []
    assert events()[-1]["status"] == "BLOCKED"
    assert events()[-1]["code"] == "ETL_DEPENDENCY_BLOCKED"


def test_empty_unknown_duplicate_and_bad_names():
    with pytest.raises(s.EtlError, match="^ETL_NO_TASKS$"):
        s.run_all(env="test")
    with pytest.raises(s.EtlError, match="^ETL_TASK_UNKNOWN$"):
        s.run_task("missing", env="test")
    s.register_task("job", lambda: {})
    with pytest.raises(s.EtlError, match="^ETL_TASK_DUPLICATE$"):
        s.register_task("job", lambda: {})
    for name in ("../escape", "bad\nname", "", None):
        with pytest.raises(s.EtlError):
            s.register_task(name, lambda: {})
    with pytest.raises(s.EtlError):
        s.register_task("invalid", 3)


@pytest.mark.parametrize("env", ["dev", "../test", "prod", True])
def test_environment_gate_before_callback(env):
    calls = []
    s.register_task("job", lambda: calls.append(1))
    with pytest.raises(s.EtlError):
        s.run_task("job", env=env)
    assert calls == []


def test_logger_requires_context_and_rejects_bad_fields():
    with pytest.raises(s.EtlError, match="^ETL_RUN_CONTEXT_MISSING$"):
        s.log_write("job", "SUCCESS", 1, 0)

    def job():
        for args, kwargs in [(("../bad", "START", None, 0), {}),
                             (("job", "FAILED", 0, 0), {}),
                             (("job", "SUCCESS", True, 0), {}),
                             (("job", "SUCCESS", 1, -1), {}),
                             (("job", "FAILED", None, 0), {"code": "FAKE_SECRET"})]:
            with pytest.raises(s.EtlError):
                s.log_write(*args, **kwargs)
        return {}

    s.register_task("job", job)
    s.run_task("job", env="test")
    assert "FAKE_SECRET" not in json.dumps(events())


def test_logging_failure_prevents_callback(monkeypatch):
    called = []
    s.register_task("job", lambda: called.append(1))
    monkeypatch.setattr(s, "_append_record", lambda *args: (_ for _ in ()).throw(OSError("FAKE_SECRET")))
    with pytest.raises(s.EtlError, match="^ETL_LOG_WRITE_FAILED$"):
        s.run_task("job", env="test")
    assert called == []


def test_log_path_symlink_rejected(tmp_path):
    s.LOG_ROOT.mkdir()
    (s.LOG_ROOT / "test").symlink_to(tmp_path, target_is_directory=True)
    s.register_task("job", lambda: {})
    with pytest.raises(s.EtlError, match="^ETL_LOG_PATH_INVALID$"):
        s.run_task("job", env="test")


@pytest.mark.parametrize("argv", [["--start", "2025-01-01"], ["--end", "2025-01-01"],
    ["--schedule", "--start", "2025-01-01", "--end", "2025-01-02"],
    ["--start", "2025-1-1", "--end", "2025-01-02"], ["--env", "FAKE_SECRET"],
    ["--start", "2025-02-30", "--end", "2025-03-01"], ["--unknown", "FAKE_SECRET"]])
def test_cli_rejects_arguments_without_echo(argv, capsys):
    cli = importlib.import_module("etl.jobs.run_all")
    assert cli.main(argv) == 1
    output = capsys.readouterr()
    assert "FAKE_SECRET" not in output.out + output.err
    assert json.loads(output.out)["code"] == "ETL_ARGUMENTS_INVALID"


def test_cron_singleton_and_nextfire():
    scheduler = s.setup_schedule(env="test")
    assert scheduler is s.setup_schedule(env="test")
    assert not scheduler.running
    job, = scheduler.get_jobs()
    assert job.id == "ods_daily" and job.max_instances == 1 and job.coalesce is True
    assert job.misfire_grace_time == 3600
    now = datetime(2026, 11, 16, 3, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert job.trigger.get_next_fire_time(None, now) == now.replace(day=17, hour=2)


@pytest.mark.parametrize("raises", [False, True])
def test_short_timer_real_dispatch_and_safe_executor_logs(raises, caplog):
    scheduler = s.setup_schedule(env="test")
    scheduler.remove_all_jobs()
    finished = threading.Event()
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
    completed = []
    scheduler.add_listener(lambda event: (completed.append(event), finished.set()), EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    def callback():
        if raises:
            raise RuntimeError("FAKE_SECRET mysql://sensitive")
        return {"rows": 1}

    scheduler.add_job(callback, "date", run_date=datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(milliseconds=100))
    thread = threading.Thread(target=scheduler.start)
    with caplog.at_level(logging.DEBUG):
        thread.start()
        try:
            assert finished.wait(5), "controlled dispatcher did not fire"
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            thread.join(timeout=5)
    assert not thread.is_alive() and not scheduler.running
    assert completed[0].code == (EVENT_JOB_ERROR if raises else EVENT_JOB_EXECUTED)
    if not raises:
        assert completed[0].retval == {"rows": 1}
    assert "FAKE_SECRET" not in caplog.text
    assert "mysql://" not in caplog.text


def test_process_lock_contention_and_release():
    backend = Path(__file__).resolve().parents[1]
    code = """
import sys
from pathlib import Path
from etl.scheduler import scheduler as s
s.LOG_ROOT = Path(sys.argv[1])
s.register_task('child', lambda: {})
try:
    s.run_task('child', env='test')
except s.EtlError as error:
    print(error.code)
    raise SystemExit(7)
"""
    def job():
        result = subprocess.run([sys.executable, "-c", code, str(s.LOG_ROOT)], cwd=backend,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 7 and result.stdout.strip() == "ETL_RUN_LOCKED"
        assert "Traceback" not in result.stderr
        return {}
    s.register_task("parent", job)
    s.run_task("parent", env="test")
    result = subprocess.run([sys.executable, "-c", code, str(s.LOG_ROOT)], cwd=backend,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


def test_scheduler_lifetime_lock_across_processes():
    from apscheduler.events import EVENT_SCHEDULER_STARTED
    scheduler = s.setup_schedule(env="test")
    started = threading.Event()
    scheduler.add_listener(lambda event: started.set(), EVENT_SCHEDULER_STARTED)
    thread = threading.Thread(target=scheduler.start)
    code = """
import sys
from pathlib import Path
from etl.scheduler import scheduler as s
s.LOG_ROOT = Path(sys.argv[1])
s.BlockingScheduler._main_loop = lambda self: None
try:
    s.setup_schedule(env='test').start()
except s.EtlError as error:
    print(error.code)
    raise SystemExit(7)
"""
    command = [sys.executable, "-c", code, str(s.LOG_ROOT)]
    backend = Path(__file__).resolve().parents[1]
    thread.start()
    try:
        assert started.wait(5)
        result = subprocess.run(command, cwd=backend, capture_output=True, text=True, timeout=10)
        assert result.returncode == 7 and result.stdout.strip() == "ETL_SCHEDULER_LOCKED"
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        thread.join(timeout=5)
    assert not thread.is_alive()
    result = subprocess.run(command, cwd=backend, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


def test_post_commit_log_failure_is_permanent_partial(monkeypatch):
    original = s._append_record
    called = []

    def writer(context, record):
        if record["name"] == "his_charge" and record["status"] == "SUCCESS":
            raise OSError("FAKE_SECRET")
        original(context, record)

    def job():
        called.append(1)
        s._source_event("his_charge", "SUCCESS", 7, 1)
        return {"his_charge": 7}

    monkeypatch.setattr(s, "_append_record", writer)
    s.register_task("extract_ods", job)
    with pytest.raises(s.EtlError, match="^ETL_LOG_WRITE_FAILED$"):
        s.run_all(env="test")
    assert called == [1]
    assert events()[-1]["status"] == "PARTIAL"
    assert events()[-1]["committed_sources"] == {"his_charge": 7}


def test_x6_callback_error_bridge_is_permanent_partial(monkeypatch):
    from etl.jobs import extract_ods as extraction
    original = s._append_record
    called = []

    def writer(context, record):
        if record["name"] == "his_charge" and record["status"] == "SUCCESS":
            raise OSError("FAKE_SECRET")
        original(context, record)

    def job():
        called.append(1)
        extraction._notify(s._source_event, "his_charge", "SUCCESS", 7, s.time.monotonic())
        return {"his_charge": 7}

    monkeypatch.setattr(s, "_append_record", writer)
    s.register_task("extract_ods", job)
    with pytest.raises(s.EtlError, match="^ODS_EVENT_CALLBACK_FAILED$"):
        s.run_all(env="test")
    assert called == [1]
    assert events()[-1]["status"] == "PARTIAL"


def test_target_rechecked_before_retry(monkeypatch):
    called = []

    def job():
        called.append(1)
        monkeypatch.setenv("ODS_DATABASE_URL", "mysql://SIM:SIM@127.0.0.1:13306/hospital")
        raise OdsError("ODS_LOAD_FAILED")

    s.register_task("extract_ods", job)
    with pytest.raises(s.EtlError, match="^ETL_ENV_DATABASE_MISMATCH$"):
        s.run_all(env="test")
    assert called == [1]


def test_generic_exception_redacted_everywhere(monkeypatch, capsys, caplog):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    called = []

    def extract(*args, **kwargs):
        called.append(1)
        raise RuntimeError("FAKE_SECRET mysql://user:password@host")

    monkeypatch.setattr(extractor, "run_extract_all", extract, raising=False)
    assert cli.main(["--env", "test"]) == 1
    assert len(called) == 4
    capture = capsys.readouterr()
    output = capture.out + capture.err + caplog.text + json.dumps(events())
    assert "FAKE_SECRET" not in output and "mysql://" not in output and "Traceback" not in output
    assert json.loads(capture.out)["code"] == "ETL_TASK_FAILED"


@pytest.mark.parametrize("problem", ["secret_key", "extra_key", "missing", "empty", "negative",
                                    "bool", "float", "text", "none_count", "not_dict"])
def test_cli_requires_exact_six_safe_source_counts(problem, monkeypatch, capsys):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    result = dict.fromkeys(s.SOURCES, 0)
    key = "FAKE_SECRET mysql://SIM:SIM@host/hospital_test"
    if problem == "secret_key":
        result = {key: 1}
    elif problem == "extra_key":
        result[key] = 1
    elif problem == "missing":
        result.pop("finance_excel")
    elif problem == "empty":
        result = {}
    elif problem == "not_dict":
        result = [key]
    else:
        result["his_charge"] = {"negative": -1, "bool": True, "float": 1.0,
                                "text": key, "none_count": None}[problem]
    called = []
    monkeypatch.setattr(extractor, "run_extract_all", lambda *args, **kwargs: (called.append(1) or result), raising=False)
    assert cli.main(["--env", "test"]) == 1
    output = capsys.readouterr()
    captured = output.out + output.err + json.dumps(events())
    assert "FAKE_SECRET" not in captured and "mysql://" not in captured
    assert json.loads(output.out)["code"] == "ETL_RESULT_INVALID"
    assert called == [1]
    assert not any(event["status"] == "SUCCESS" for event in events())


def test_cli_accepts_exact_six_counts_before_success(monkeypatch, capsys):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    result = {name: index for index, name in enumerate(s.SOURCES)}
    monkeypatch.setattr(extractor, "run_extract_all", lambda *args, **kwargs: result, raising=False)
    assert cli.main(["--env", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == {"extract_ods": result}
    assert events()[-1]["status"] == "SUCCESS" and events()[-1]["rows"] == sum(result.values())


def test_manual_range_never_constructs_scheduler(monkeypatch):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")
    seen = []
    monkeypatch.setattr(s, "setup_schedule", lambda **kwargs: pytest.fail("manual started scheduler"))
    monkeypatch.setattr(extractor, "run_extract_all", lambda a, b, **kwargs: (seen.append((a, b)) or dict.fromkeys(s.SOURCES, 0)), raising=False)
    assert cli.main(["--env", "test", "--start", "2025-01-01", "--end", "2025-01-02"]) == 0
    assert seen == [("2025-01-01", "2025-01-02")]


def test_explicit_schedule_cli_starts_frontend_only(monkeypatch):
    cli = importlib.import_module("etl.jobs.run_all")
    calls = []

    class Scheduler:
        def start(self):
            calls.append("start")
            raise KeyboardInterrupt

    monkeypatch.setattr(s, "setup_schedule", lambda **kwargs: Scheduler())
    assert cli.main(["--env", "test", "--schedule"]) == 0
    assert calls == ["start"] and list(s._TASKS) == ["extract_ods"]


def test_json_fields_uuid_permissions_and_cwd_independence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s.register_task("job", lambda: {"rows": 0})
    s.run_task("job", env="test")
    for row in events():
        assert set(row) == {"timestamp", "env", "run_id", "attempt", "name", "status", "code", "rows", "cost_ms"}
        assert datetime.fromisoformat(row["timestamp"]).utcoffset() == timedelta(0)
        assert UUID(row["run_id"]).version == 4
    path, = s.LOG_ROOT.glob("test/etl_test_*_job_*.log")
    assert path.stat().st_mode & 0o777 == 0o600
    assert s._CURRENT.get() is None


@pytest.mark.parametrize("kind", ["run", "scheduler"])
def test_lock_symlink_does_not_touch_target(kind, tmp_path):
    directory = s.LOG_ROOT / "test"
    directory.mkdir(parents=True)
    target = tmp_path / "untouched"
    target.write_text("unchanged")
    (directory / f".{kind}.lock").symlink_to(target)
    with pytest.raises(s.EtlError):
        with s._lock("test", kind):
            pytest.fail("symlink lock acquired")
    assert target.read_text() == "unchanged"


def test_nested_public_run_rejected_without_deadlock():
    def outer():
        with pytest.raises(s.EtlError, match="^ETL_RUN_REENTRANT$"):
            s.run_task("inner", env="test")
        return {}
    s.register_task("outer", outer)
    s.register_task("inner", lambda: {})
    s.run_task("outer", env="test")


def test_shutdown_waits_for_inflight_job_and_holds_lifecycle_lock():
    scheduler = s.setup_schedule(env="test")
    scheduler.remove_all_jobs()
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def job():
        entered.set()
        assert release.wait(5)

    def stop():
        scheduler.shutdown(wait=True)
        stopped.set()

    def run():
        try:
            scheduler.start()
        except s.EtlError as error:
            errors.append((error.code, type(error.__context__).__name__))

    scheduler.add_job(job, "date", run_date=datetime.now(s.SHANGHAI) + timedelta(milliseconds=100))
    runner = threading.Thread(target=run)
    stopper = threading.Thread(target=stop)
    runner.start()
    try:
        assert entered.wait(5)
        stopper.start()
        assert not stopped.wait(0.1)
        with pytest.raises(s.EtlError, match="^ETL_SCHEDULER_LOCKED$"):
            with s._lock("test", "scheduler"):
                pytest.fail("lifecycle lock released before in-flight job finished")
    finally:
        release.set()
        if stopper.ident is not None:
            stopper.join(timeout=5)
        elif scheduler.running:
            scheduler.shutdown(wait=True)
        runner.join(timeout=5)
    assert stopped.is_set() and not runner.is_alive() and not stopper.is_alive()
    assert errors == []


def test_scheduler_start_failure_is_safe(monkeypatch):
    def start(self, *args, **kwargs):
        raise RuntimeError("FAKE_SECRET")
    monkeypatch.setattr(s.BlockingScheduler, "start", start)
    with pytest.raises(s.EtlError, match="^ETL_SCHEDULE_FAILED$"):
        s.setup_schedule(env="test").start()
    with s._lock("test", "scheduler"):
        pass


@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("already_stopped", [False, True])
def test_shutdown_fault_keeps_lock_until_actual_callback_drain(fault, already_stopped, monkeypatch, caplog):
    """Security F2 repro: STOPPED plus a failed APScheduler executor shutdown is not drained."""
    scheduler = s.setup_schedule(env="test")
    scheduler.remove_all_jobs()
    entered, release, shutdown_entered, finished = (threading.Event() for _ in range(4))
    errors, queued_calls, error_texts = [], [], []

    def active():
        entered.set()
        assert release.wait(5)

    now = datetime.now(s.SHANGHAI) - timedelta(milliseconds=10)
    scheduler.add_job(active, "date", id="first", run_date=now)
    scheduler.add_job(lambda: queued_calls.append("write"), "date", id="queued", run_date=now)
    executor = scheduler._executors["default"]
    original_shutdown = executor.shutdown

    def fault_shutdown(wait=True):
        shutdown_entered.set()
        raise fault("FAKE_SECRET mysql://SIM:SIM@host/hospital_test")

    def fault_loop():
        scheduler._process_jobs()
        assert entered.wait(2)
        if already_stopped:
            from apscheduler.schedulers.base import BaseScheduler
            BaseScheduler.shutdown(scheduler, wait=True)
        raise RuntimeError("FAKE_SECRET")

    def run():
        try:
            scheduler.start()
        except BaseException as error:
            errors.append(error.code if isinstance(error, s.EtlError) else type(error).__name__)
            error_texts.append(str(error))
        finally:
            finished.set()

    monkeypatch.setattr(executor, "shutdown", fault_shutdown)
    monkeypatch.setattr(scheduler, "_main_loop", fault_loop)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert entered.wait(2) and shutdown_entered.wait(2)
        assert not scheduler.running
        assert not finished.wait(0.1), "STOPPED released lifecycle ownership before actual callback drain"
        with pytest.raises(s.EtlError, match="^ETL_SCHEDULER_LOCKED$"):
            with s._lock("test", "scheduler"):
                pytest.fail("lifecycle lock acquired before callback finished")
    finally:
        release.set()
        runner.join(timeout=5)
        original_shutdown(wait=True)
    assert not runner.is_alive()
    assert errors == ["ETL_SCHEDULE_FAILED" if fault is RuntimeError else "KeyboardInterrupt"]
    assert queued_calls == []
    output = caplog.text + json.dumps(error_texts)
    assert "FAKE_SECRET" not in output and "mysql://" not in output
    with s._lock("test", "scheduler"):
        pass


@pytest.mark.parametrize("interrupts", [1, 2])
def test_interrupt_during_actual_drain_is_deferred_and_reported(interrupts, monkeypatch):
    scheduler = s.setup_schedule(env="test")
    scheduler.remove_all_jobs()
    entered, release, interrupted, finished = (threading.Event() for _ in range(4))
    errors, queued_calls, waits = [], [], []
    executor = scheduler._executors["default"]
    original_wait = executor._gate.wait

    def wait(timeout=None):
        waits.append(1)
        if len(waits) <= interrupts:
            interrupted.set()
            raise KeyboardInterrupt("FAKE_SECRET")
        return original_wait(timeout)

    def loop():
        scheduler._process_jobs()
        assert entered.wait(2)
        raise RuntimeError("FAKE_SECRET")

    def run():
        try:
            scheduler.start()
        except s.EtlError as error:
            errors.append(error.code)
        finally:
            finished.set()

    now = datetime.now(s.SHANGHAI) - timedelta(milliseconds=10)
    scheduler.add_job(lambda: (entered.set(), release.wait(5)), "date", id="first", run_date=now)
    scheduler.add_job(lambda: queued_calls.append("write"), "date", id="queued", run_date=now)
    monkeypatch.setattr(executor._gate, "wait", wait)
    monkeypatch.setattr(scheduler, "_main_loop", loop)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert entered.wait(2) and interrupted.wait(2)
        assert not finished.wait(0.1)
        with pytest.raises(s.EtlError, match="^ETL_SCHEDULER_LOCKED$"):
            with s._lock("test", "scheduler"):
                pytest.fail("interrupted wait released ownership")
    finally:
        release.set()
        runner.join(timeout=5)
        executor.drain()
    assert not runner.is_alive()
    assert errors == ["ETL_INTERRUPTED"] and queued_calls == []


@pytest.mark.parametrize("point", ["guard", "gate"])
def test_interrupt_entering_cleanup_still_drains_before_unlock(point, monkeypatch):
    scheduler = s.setup_schedule(env="test")
    scheduler.remove_all_jobs()
    entered, release, interrupted, finished = (threading.Event() for _ in range(4))
    errors, queued_calls = [], []
    executor = scheduler._executors["default"]
    original_close = executor.close_gate

    class InterruptGuard:
        def __enter__(self):
            interrupted.set()
            raise KeyboardInterrupt("FAKE_SECRET")

        def __exit__(self, *args):
            pass

    def close():
        if not interrupted.is_set():
            interrupted.set()
            raise KeyboardInterrupt("FAKE_SECRET")
        original_close()

    def loop():
        scheduler._process_jobs()
        assert entered.wait(2)
        raise RuntimeError("FAKE_SECRET")

    def run():
        try:
            scheduler.start()
        except KeyboardInterrupt as error:
            errors.append(str(error))
        finally:
            finished.set()

    now = datetime.now(s.SHANGHAI) - timedelta(milliseconds=10)
    scheduler.add_job(lambda: (entered.set(), release.wait(5)), "date", id="first", run_date=now)
    scheduler.add_job(lambda: queued_calls.append("write"), "date", id="queued", run_date=now)
    if point == "guard":
        monkeypatch.setattr(scheduler, "_shutdown_guard", InterruptGuard())
    else:
        monkeypatch.setattr(executor, "close_gate", close)
    monkeypatch.setattr(scheduler, "_main_loop", loop)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert entered.wait(2) and interrupted.wait(2)
        assert not finished.wait(0.1)
        with pytest.raises(s.EtlError, match="^ETL_SCHEDULER_LOCKED$"):
            with s._lock("test", "scheduler"):
                pytest.fail("interrupted cleanup entry released lifecycle lock")
    finally:
        release.set()
        runner.join(timeout=5)
        executor.drain()
    assert not runner.is_alive()
    assert errors == [""] and queued_calls == []


def test_manual_interrupt_has_no_traceback(monkeypatch, capsys):
    cli = importlib.import_module("etl.jobs.run_all")
    extractor = importlib.import_module("etl.jobs.extract_ods")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(extractor, "run_extract_all", interrupt, raising=False)
    assert cli.main(["--env", "test"]) == 130
    capture = capsys.readouterr()
    assert json.loads(capture.out)["code"] == "ETL_INTERRUPTED"
    assert "Traceback" not in capture.err
    assert s._CURRENT.get() is None


def test_mysql_pipeline_short_dispatch(monkeypatch):
    """Coordinator-only opt-in: existing schema, six explicit synthetic source configs."""
    if os.environ.get("ODS_S6_INTEGRATION") != "1":
        pytest.skip("S6_DB_NOT_AUTHORIZED: coordinator must explicitly opt in and inject test URL")
    from etl.config import source_config as sources
    from etl.jobs import extract_ods as extraction
    sources.database_options(test_only=True)
    monkeypatch.setenv("ODS_DATABASE_URL", os.environ["ODS_TEST_DATABASE_URL"])
    dataset = "s6_test_" + uuid4().hex
    directory = sources.GENERATED_ROOT / dataset
    assert not directory.exists()
    directory.mkdir(parents=True)
    infos = {}
    dates = {"his_outpatient": "visit_date", "his_inpatient": "admit_date", "his_charge": "fee_date",
             "emr_record": "visit_date", "lis_report": "report_date", "finance_excel": "stat_date"}
    try:
        for name, spec in sources.SOURCES.items():
            path = directory / spec["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            record = dict.fromkeys(spec["columns"], "SIM_S6")
            record[dates[name]] = "2025-01-02"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=spec["columns"])
                writer.writeheader()
                writer.writerow(record)
            infos[name] = dict(kind="csv", file_path=str(path), dataset_id=dataset, etl_batch_date="2025-01-31")
        s.register_task("extract_ods", lambda: extraction.run_extract_all(
            "2025-01-02", "2025-01-02", source_infos=infos, on_event=s._source_event))
        scheduler = s.setup_schedule(env="test")
        scheduler.remove_all_jobs()
        done = threading.Event()
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
        results = []
        scheduler.add_listener(lambda event: (results.append(event), done.set()), EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        scheduler.add_job(s.run_all, "date", kwargs={"env": "test"},
                          run_date=datetime.now(s.SHANGHAI) + timedelta(milliseconds=100))
        thread = threading.Thread(target=scheduler.start)
        thread.start()
        try:
            assert done.wait(30), "test-only database dispatcher timeout"
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            thread.join(timeout=30)
        assert not thread.is_alive()
        assert results[0].exception is None
        assert results[0].retval == {"extract_ods": dict.fromkeys(sources.SOURCES, 1)}
        with sources.database_cursor("ODS_RANGE_VERIFY_FAILED") as cursor:
            for spec in sources.SOURCES.values():
                cursor.execute(f"SELECT COUNT(*) FROM `{spec['table']}` WHERE dataset_id=%s", (dataset,))
                assert cursor.fetchone()[0] == 1
        assert len([event for event in events() if event["name"] in sources.SOURCES and event["status"] == "SUCCESS"]) == 6
    finally:
        # No registry mutation, DDL, shared-table truncation, or other dataset deletion.
        try:
            with sources.database_cursor("ODS_LOAD_FAILED", write=True) as cursor:
                for spec in sources.SOURCES.values():
                    cursor.execute(f"DELETE FROM `{spec['table']}` WHERE dataset_id=%s", (dataset,))
        finally:
            shutil.rmtree(directory)
