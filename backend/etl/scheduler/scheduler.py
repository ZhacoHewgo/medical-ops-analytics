"""Single-host serial runs, bounded retries, and metadata-only durable logs."""

from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import stat
from threading import Condition, RLock
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.executors.base import BaseExecutor, run_job
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from etl.config.source_config import OdsError, SOURCES, database_options
from etl.scheduler import policy


LOG_ROOT = Path(__file__).resolve().parents[1] / "logs"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_TASKS = {}
_SCHEDULERS = {}
_CURRENT = ContextVar("etl_run", default=None)
_RETRYABLE = frozenset({"ODS_DATABASE_CONNECT_FAILED", "ODS_LOAD_FAILED",
                        "ODS_RANGE_LOAD_FAILED", "ODS_DATABASE_CLOSE_FAILED"})
_NODE07_RETRYABLE = frozenset({'DWD_TX_RETRYABLE', 'DQ_READ_RETRYABLE'})
_NODE07_CODES = frozenset("""
DWD_ARGUMENTS_INVALID DWD_CONFIG_INVALID DWD_SCHEMA_INVALID DWD_INPUT_EMPTY
DWD_SOURCE_MISMATCH DWD_PRIVACY_KEY_MISSING DWD_PRIVACY_KEY_INVALID
DWD_PRIVACY_KEY_CHANGED DWD_TX_RETRYABLE DWD_TX_FAILED DWD_ROLLBACK_FAILED
DWD_COMMIT_UNKNOWN DWD_INTERNAL_ERROR DQ_READ_RETRYABLE DQ_ARGUMENTS_INVALID
DQ_CONFIG_INVALID DQ_SNAPSHOT_MISMATCH DQ_STALE DQ_RULE_ERROR DQ_INTERNAL_ERROR
DQ_REPORT_PATH_INVALID DQ_REPORT_IDENTITY_CONFLICT DQ_REPORT_WRITE_FAILED
DQ_GATE_FAILED DQ_INSUFFICIENT_EVIDENCE
""".split())
_NODE08_CODES = frozenset("""
N8_ARGUMENTS_INVALID N8_RANGE_NOT_FULL N8_SCHEMA_INVALID N8_MIGRATION_REQUIRED
N8_INPUT_EMPTY N8_DATE_INVALID N8_DATE_RANGE_INVALID N8_DIMENSION_INVALID N8_UNKNOWN_CODE
N8_VISIT_REFERENCE_INVALID N8_VISIT_AMBIGUOUS N8_PATIENT_MISMATCH N8_UPSTREAM_GATE_FAILED
N8_STALE N8_RECONCILE_FAILED N8_AMOUNT_OVERFLOW N8_TX_RETRYABLE N8_TX_FAILED
N8_ROLLBACK_FAILED N8_COMMIT_UNKNOWN N8_REPORT_PATH_INVALID N8_REPORT_IDENTITY_CONFLICT
N8_REPORT_WRITE_FAILED N8_RUN_CONTEXT_ACTIVE N8_INTERNAL_ERROR
""".split())
_ODS_CODES = _NODE07_CODES | _RETRYABLE | frozenset("""
ODS_ARGUMENTS_INVALID ODS_BATCH_INVALID ODS_CONFIG_INVALID ODS_COUNT_MISMATCH
ODS_CSV_HEADER_INVALID ODS_CSV_READ_FAILED ODS_CSV_ROW_INVALID ODS_DATABASE_URL_INVALID
ODS_DATABASE_URL_MISSING ODS_DATASET_INVALID ODS_DDL_FAILED ODS_DDL_READ_FAILED
ODS_DEPENDENCY_MISSING ODS_PATH_INVALID ODS_RECORD_FIELDS_INVALID ODS_REGISTER_FAILED
ODS_REGISTRY_READ_FAILED ODS_ROLLBACK_FAILED ODS_ROW_NUMBER_INVALID ODS_SCHEMA_INVALID
ODS_SNAPSHOT_EMPTY ODS_SNAPSHOT_INVALID ODS_SNAPSHOT_MIXED ODS_SOURCE_CHECK_FAILED
ODS_SOURCE_INVALID ODS_SOURCE_NOT_REGISTERED ODS_TABLE_INVALID ODS_TEST_DATABASE_REQUIRED
ODS_TEXT_INVALID ODS_RANGE_INVALID ODS_SOURCE_DATE_INVALID ODS_RANGE_REBASE_REQUIRED
ODS_RANGE_VERIFY_FAILED ODS_CONFIG_MIXED ODS_EVENT_CALLBACK_FAILED ODS_EVENT_CALLBACK_INVALID
ODS_XLSX_READ_FAILED ODS_XLSX_SHEET_INVALID ODS_XLSX_HEADER_INVALID ODS_XLSX_TEXT_INVALID
""".split())
_ETL_CODES = frozenset("""
ETL_OK ETL_TASK_FAILED ETL_ODS_FAILED ETL_TASK_INVALID ETL_TASK_UNKNOWN ETL_TASK_DUPLICATE
ETL_NO_TASKS ETL_RETRY_INVALID ETL_ENV_INVALID ETL_ENV_DATABASE_MISMATCH ETL_RUN_LOCKED
ETL_SCHEDULER_LOCKED ETL_LOG_PATH_INVALID ETL_LOG_WRITE_FAILED ETL_RUN_CONTEXT_MISSING
ETL_LOG_EVENT_INVALID ETL_DEPENDENCY_BLOCKED ETL_SOURCE_FAILED ETL_TASK_PARTIAL
ETL_RESULT_INVALID ETL_ARGUMENTS_INVALID ETL_SCHEDULE_FAILED ETL_RUN_REENTRANT ETL_INTERRUPTED
""".split())


class EtlError(Exception):
    """Fixed reason codes only; never retain original exception text."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in _ETL_CODES | _ODS_CODES | _NODE08_CODES else "ETL_TASK_FAILED"
        super().__init__(self.code)


def _safe_code(error):
    if isinstance(error, EtlError):
        return error.code if type(error.code) is str and error.code in _ETL_CODES | _ODS_CODES | _NODE08_CODES else "ETL_TASK_FAILED"
    if isinstance(error, OdsError):
        return error.code if type(error.code) is str and error.code in _ODS_CODES | _NODE08_CODES else "ETL_ODS_FAILED"
    return "ETL_TASK_FAILED"


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise EtlError("ETL_TASK_INVALID")
    return value


def _environment(env):
    if type(env) is not str or env not in {"dev", "test"}:
        raise EtlError("ETL_ENV_INVALID")
    try:
        database = database_options()["database"]
    except Exception as error:
        raise EtlError(_safe_code(error)) from None
    if database != {"dev": "hospital", "test": "hospital_test"}[env]:
        raise EtlError("ETL_ENV_DATABASE_MISMATCH")


def _directory(env):
    directory = LOG_ROOT / env
    try:
        if not LOG_ROOT.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents)):
            raise EtlError("ETL_LOG_PATH_INVALID")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise EtlError("ETL_LOG_PATH_INVALID")
    except EtlError:
        raise
    except OSError:
        raise EtlError("ETL_LOG_WRITE_FAILED") from None
    return directory


def _open_file(path, flags):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            os.close(fd)
            raise EtlError("ETL_LOG_PATH_INVALID")
        return fd
    except OSError:
        raise EtlError("ETL_LOG_WRITE_FAILED") from None


@contextmanager
def _lock(env, kind):
    fd = _open_file(_directory(env) / f".{kind}.lock", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EtlError("ETL_RUN_LOCKED" if kind == "run" else "ETL_SCHEDULER_LOCKED") from None
        except OSError:
            raise EtlError("ETL_LOG_WRITE_FAILED") from None
        yield
    finally:
        os.close(fd)


@dataclass
class _Run:
    env: str
    run_id: str
    stream: object
    attempt: int = 1
    committed: dict = field(default_factory=dict)
    scheduled_date: str | None = None
    pipeline: dict | None = None


@contextmanager
def _run_context(env, root):
    if _CURRENT.get() is not None:
        raise EtlError("ETL_RUN_REENTRANT")
    _environment(env)
    with _lock(env, "run"):
        run_id = str(uuid4())
        day = datetime.now(SHANGHAI).strftime("%Y%m%d")
        path = _directory(env) / f"etl_{env}_{day}_{root}_{run_id}.log"
        fd = _open_file(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                token = _CURRENT.set(_Run(env, run_id, stream))
                try:
                    yield
                finally:
                    _CURRENT.reset(token)
        except OSError:
            raise EtlError("ETL_LOG_WRITE_FAILED") from None


def _append_record(context, record):
    context.stream.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    context.stream.flush()
    os.fsync(context.stream.fileno())


def log_write(name, status, rows, cost_ms, *, code="ETL_OK"):
    context = _CURRENT.get()
    if context is None:
        raise EtlError("ETL_RUN_CONTEXT_MISSING")
    _name(name)
    if (type(status) is not str or status not in {"START", "SUCCESS", "FAILED", "BLOCKED", "PARTIAL"}
            or type(code) is not str or code not in _ETL_CODES | _ODS_CODES | _NODE08_CODES
            or type(cost_ms) is not int or cost_ms < 0
            or (status == "SUCCESS" and (type(rows) is not int or rows < 0))
            or (status != "SUCCESS" and rows is not None)
            or (status in {"FAILED", "BLOCKED", "PARTIAL"} and code == "ETL_OK")):
        raise EtlError("ETL_LOG_EVENT_INVALID")
    record = dict(timestamp=datetime.now(timezone.utc).isoformat(), env=context.env,
                  run_id=context.run_id, attempt=context.attempt, name=name,
                  status=status, code=code, rows=rows, cost_ms=cost_ms)
    if status == "PARTIAL":
        record["committed_sources"] = dict(context.committed)
    try:
        _append_record(context, record)
    except Exception:
        raise EtlError("ETL_LOG_WRITE_FAILED") from None


def _source_event(name, status, rows, cost_ms):
    context = _CURRENT.get()
    if context is None:
        raise EtlError("ETL_RUN_CONTEXT_MISSING")
    if (type(name) is not str or name not in SOURCES or type(status) is not str
            or status not in {"START", "SUCCESS", "FAILED", "BLOCKED"}):
        raise EtlError("ETL_LOG_EVENT_INVALID")
    if status == "START":
        _environment(context.env)
    if status == "SUCCESS" and type(rows) is int and rows >= 0:
        # SUCCESS callback happens after commit, even if its durable log subsequently fails.
        context.committed[name] = rows
    code = {"START": "ETL_OK", "SUCCESS": "ETL_OK", "FAILED": "ETL_SOURCE_FAILED",
            "BLOCKED": "ETL_DEPENDENCY_BLOCKED"}[status]
    log_write(name, status, rows, cost_ms, code=code)


def register_task(name, func):
    _name(name)
    if not callable(func):
        raise EtlError("ETL_TASK_INVALID")
    if name in _TASKS:
        raise EtlError("ETL_TASK_DUPLICATE")
    _TASKS[name] = func


def _execute(name, max_retry):
    context = _CURRENT.get()
    for attempt in range(1, max_retry + 2):
        context.attempt = attempt
        context.committed.clear()
        started = time.monotonic()
        # Durable START is the logging preflight before any task can write to the DB.
        log_write(name, "START", None, 0)
        try:
            _environment(context.env)
            result = _TASKS[name]()
            if (type(result) is not dict or any(type(key) is not str
                    or type(value) is not int or value < 0 for key, value in result.items())):
                raise EtlError("ETL_RESULT_INVALID")
            log_write(name, "SUCCESS", sum(result.values()), _cost(started))
            return result
        except Exception as error:
            code = _safe_code(error)
            if name == "build_dwd":
                retry = isinstance(error, OdsError) and code == "N8_TX_RETRYABLE"
            else:
                retry = (isinstance(error, OdsError) and code in _RETRYABLE | _NODE07_RETRYABLE
                         or not isinstance(error, (OdsError, EtlError)))
            log_write(name, "FAILED", None, _cost(started), code=code)
            if context.committed:
                log_write(name, "PARTIAL", None, _cost(started), code="ETL_TASK_PARTIAL")
            if not retry or attempt > max_retry:
                raise EtlError(code) from None
        time.sleep(1)


def _cost(started):
    return max(0, int((time.monotonic() - started) * 1000))


def run_task(name, max_retry=3, *, env="dev"):
    _name(name)
    if name not in _TASKS:
        raise EtlError("ETL_TASK_UNKNOWN")
    if type(max_retry) is not int or not 0 <= max_retry <= 3:
        raise EtlError("ETL_RETRY_INVALID")
    with _run_context(env, name):
        return _execute(name, max_retry)


def run_all(*, env="dev"):
    names = tuple(_TASKS)
    if not names:
        raise EtlError("ETL_NO_TASKS")
    with _run_context(env, "run_all"):
        results = {}
        for index, name in enumerate(names):
            try:
                results[name] = _execute(name, 3)
            except EtlError:
                for blocked in names[index + 1:]:
                    log_write(blocked, "BLOCKED", None, 0, code="ETL_DEPENDENCY_BLOCKED")
                raise
        return results


class _SafeLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = "ETL_SCHEDULER_ERROR" if record.levelno >= logging.WARNING else "ETL_SCHEDULER_EVENT"
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def _scheduler_logger(suffix):
    logger = logging.getLogger(f"etl.safe_scheduler.{suffix}")
    if not any(isinstance(item, _SafeLogFilter) for item in logger.filters):
        logger.addFilter(_SafeLogFilter())
    return logger


class _SafeExecutor(BaseExecutor):
    def __init__(self):
        super().__init__()
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._gate = Condition()
        self._stopping = False
        self._pending = 0

    def start(self, scheduler, alias):
        super().start(scheduler, alias)
        self._logger = _scheduler_logger("executor")

    def _do_submit_job(self, job, run_times):
        with self._gate:
            if self._stopping:
                raise EtlError("ETL_SCHEDULE_FAILED")
            self._pending += 1
        try:
            self._pool.submit(self._dispatch, job, run_times)
        except BaseException:
            with self._gate:
                self._pending -= 1
                self._gate.notify_all()
            raise

    def _dispatch(self, job, run_times):
        with self._gate:
            admitted = not self._stopping
        try:
            try:
                events = run_job(job, job._jobstore_alias, run_times, self._logger.name) if admitted else []
            except BaseException as error:
                self._run_job_error(job.id, error, error.__traceback__)
            else:
                self._run_job_success(job.id, events)
        finally:
            with self._gate:
                self._pending -= 1
                self._gate.notify_all()

    def close_gate(self):
        with self._gate:
            self._stopping = True

    def drain(self):
        interrupted = False
        while True:
            try:
                self.close_gate()
                with self._gate:
                    while self._pending:
                        self._gate.wait()
                self._pool.shutdown(wait=True)
                break
            except KeyboardInterrupt:
                # A second Ctrl-C must not release ownership while admitted callbacks still run.
                interrupted = True
        if interrupted:
            raise EtlError("ETL_INTERRUPTED")

    def shutdown(self, wait=True):
        self.drain()


class _LockedScheduler(BlockingScheduler):
    def __init__(self, env, **kwargs):
        self.env = env
        self._shutdown_guard = RLock()
        self._owned_executor = _SafeExecutor()
        super().__init__(executors={"default": self._owned_executor}, **kwargs)

    def shutdown(self, wait=True):
        try:
            try:
                with self._shutdown_guard:
                    self._owned_executor.close_gate()
                    if self.running:
                        # Drain the dispatch pass before STOPPED changes remove_job's lookup behavior.
                        with self._jobstores_lock:
                            self.pause()
                        super().shutdown(wait=True)
            finally:
                # Also drain if STOPPED or interrupted while acquiring the cleanup guard/gate.
                try:
                    self._owned_executor.drain()
                finally:
                    self.state = STATE_STOPPED
                    if self._event is not None:
                        self.wakeup()
        except KeyboardInterrupt:
            raise KeyboardInterrupt from None
        except EtlError:
            raise
        except Exception:
            raise EtlError("ETL_SCHEDULE_FAILED") from None

    def start(self, *args, **kwargs):
        try:
            _environment(self.env)
            with _lock(self.env, "scheduler"):
                try:
                    super().start(*args, **kwargs)
                finally:
                    self.shutdown(wait=True)
        except KeyboardInterrupt:
            raise KeyboardInterrupt from None
        except EtlError:
            raise
        except Exception:
            raise EtlError("ETL_SCHEDULE_FAILED") from None


def _daily_run(env):
    try:
        run_all(env=env)
    except Exception as error:
        print(json.dumps(dict(code=_safe_code(error), status="FAILED")))


def _managed_daily_run(env, version):
    try:
        if policy.claim_day(env, datetime.now(SHANGHAI).date(), version):
            _daily_run(env)
    except OdsError:
        _scheduler_logger("scheduler").warning("ETL_SCHEDULE_FAILED")


def _add_daily_job(scheduler, env, hour, minute, *, version=None):
    kwargs = {"env": env} if version is None else {"env": env, "version": version}
    callback = _daily_run if version is None else _managed_daily_run
    scheduler.add_job(callback, CronTrigger(hour=hour, minute=minute, timezone=SHANGHAI),
                      kwargs=kwargs, id="ods_daily", name="ods_daily",
                      replace_existing=True, max_instances=1, coalesce=True,
                      misfire_grace_time=3600)


def _refresh_managed_daily(scheduler, env):
    configured = policy.read_policy(env)
    job = scheduler.get_job("ods_daily")
    if not configured.enabled:
        if job is not None:
            scheduler.remove_job("ods_daily")
    elif job is None or job.kwargs.get("version") != configured.version:
        _add_daily_job(scheduler, env, configured.hour, configured.minute, version=configured.version)


def setup_schedule(*, env="dev", managed=False):
    _environment(env)
    key = (env, "managed") if managed else env
    if key not in _SCHEDULERS:
        scheduler = _LockedScheduler(env, timezone=SHANGHAI,
                                     jobstores={"default": MemoryJobStore()},
                                     logger=_scheduler_logger("scheduler"))
        if managed:
            try:
                _refresh_managed_daily(scheduler, env)
            except OdsError:
                raise EtlError("ETL_SCHEDULE_FAILED") from None
            scheduler.add_job(_refresh_managed_daily, IntervalTrigger(seconds=10),
                              kwargs={"scheduler": scheduler, "env": env},
                              id="ods_schedule_sync", name="ods_schedule_sync",
                              max_instances=1, coalesce=True, misfire_grace_time=10)
        else:
            _add_daily_job(scheduler, env, 2, 0)
        _SCHEDULERS[key] = scheduler
    return _SCHEDULERS[key]
