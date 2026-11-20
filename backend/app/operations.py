"""Safe registry and scheduler metadata for the platform operations pages."""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import stat
import subprocess
import sys

from etl.config import source_config as sources
from etl.scheduler import scheduler

TASKS = ("extract_ods", "clean_dwd", "quality_check", "build_dwd")
STATES = frozenset(("START", "SUCCESS", "FAILED", "BLOCKED", "PARTIAL"))
DB_ERROR = "OPS_AUDIT_UNAVAILABLE"


def read_sources():
    registered = {item["name"]: item for item in sources.list_sources()}
    entries = []
    with sources.database_cursor(DB_ERROR) as cursor:
        for name in sources.SOURCES:
            info = registered.get(name)
            item = {"name": name, "registered": info is not None, "dataset_id": None,
                    "batch_date": None, "last_check": None}
            if info:
                item.update(dataset_id=info["dataset_id"], batch_date=info["etl_batch_date"])
                cursor.execute(
                    "SELECT outcome,finished_at FROM ops_action_audit WHERE action='source_check' "
                    "AND target=%s AND dataset_id=%s AND batch_date=%s AND finished_at IS NOT NULL "
                    "AND outcome IN ('SUCCESS','FAILED') ORDER BY action_id DESC LIMIT 1",
                    (name, item["dataset_id"], item["batch_date"]),
                )
                if checked := cursor.fetchone():
                    status, when = checked
                    item["last_check"] = {"outcome": status, "at": when.replace(tzinfo=timezone.utc).isoformat()}
            entries.append(item)
    return entries


def begin_action(actor, action, target, info, current_admin):
    with sources.database_cursor(DB_ERROR, write=True) as cursor:
        if not current_admin(cursor, actor, lock=True):
            return None
        dataset = info["dataset_id"] if info else None
        batch = info["etl_batch_date"] if info else None
        cursor.execute(
            "INSERT INTO ops_action_audit (actor_id,action,target,dataset_id,batch_date,outcome) "
            "VALUES (%s,%s,%s,%s,%s,'STARTED')",
            (actor.user_id, action, target, dataset, batch),
        )
        return cursor.lastrowid


def finish_action(ticket, outcome, code):
    with sources.database_cursor(DB_ERROR, write=True) as cursor:
        cursor.execute(
            "UPDATE ops_action_audit SET outcome=%s,code=%s,finished_at=CURRENT_TIMESTAMP(6) "
            "WHERE action_id=%s AND outcome='STARTED'", (outcome, code, ticket),
        )
        if cursor.rowcount != 1:
            raise sources.OdsError(DB_ERROR)


def environment():
    return {"hospital": "dev", "hospital_test": "test"}[sources.database_options()["database"]]


def _event(line, env, identity):
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("env") != env or value.get("run_id") != identity:
        return None
    if value.get("name") not in TASKS or value.get("status") not in STATES:
        return None
    if type(value.get("timestamp")) is not str:
        return None
    stamp = datetime.fromisoformat(value["timestamp"])
    if stamp.utcoffset() != timedelta(0) or not 2000 <= stamp.year <= 9999:
        return None
    code = value.get("code")
    if scheduler.EtlError(code).code != code or (code == "ETL_OK") != (value["status"] in {"START", "SUCCESS"}):
        return None
    attempt, rows, cost = value.get("attempt"), value.get("rows"), value.get("cost_ms")
    if type(attempt) is not int or not 1 <= attempt <= 4 or type(cost) is not int or cost < 0:
        return None
    if value["status"] == "SUCCESS":
        if type(rows) is not int or rows < 0:
            return None
    elif rows is not None:
        return None
    return {"name": value["name"], "status": value["status"], "run_id": identity, "code": code,
            "at": stamp.isoformat(), "rows": rows, "attempt": attempt, "cost_ms": cost}


def read_tasks(env):
    directory = scheduler.LOG_ROOT / env
    if not directory.is_absolute() or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise OSError("Unsafe task log directory")
    try:
        root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return [{"name": task, "latest": None} for task in TASKS]
    latest = {}
    pattern = re.compile(
        rf"etl_{env}_[0-9]{{8}}_(?:run_all|{'|'.join(TASKS)})_"
        r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.log"
    )
    try:
        with os.scandir(root) as entries:
            candidates = []
            for entry in entries:
                match = pattern.fullmatch(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    candidates.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name, match[1]))
        for _, name, identity in sorted(candidates, reverse=True)[:30]:
            file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(file, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 262144:
                    raise OSError("Unsafe task log file")
                while line := stream.readline(4097):
                    if len(line) > 4096:
                        raise ValueError("Task event is too large")
                    if not line.endswith(b"\n"):
                        break  # A running writer may not have completed the last event.
                    event = _event(line, env, identity)
                    if event and (event["name"] not in latest or event["at"] >= latest[event["name"]]["at"]):
                        latest[event["name"]] = event
    finally:
        os.close(root)
    return [{"name": task, "latest": latest.get(task)} for task in TASKS]


def run_extract(env):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "etl.jobs.run_all", "--env", env, "--through", "extract"],
            cwd=sources.PROJECT_ROOT / "backend", stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=90,
            env={"ODS_DATABASE_URL": os.environ.get("ODS_DATABASE_URL", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return "INDETERMINATE", "OPS_RUN_UNCONFIRMED"
    try:
        value = json.loads(result.stdout)
        if result.returncode == 0 and value["status"] == "SUCCESS" and value["code"] == "ETL_OK":
            rows = value["tasks"]["extract_ods"]
            if rows.keys() == sources.SOURCES.keys() and all(type(n) is int and n >= 0 for n in rows.values()):
                return "SUCCESS", "ETL_OK"
        if result.returncode == 1 and value["status"] == "FAILED" and value["code"] != "ETL_OK":
            return "FAILED", scheduler.EtlError(value["code"]).code
    except (ValueError, KeyError, AttributeError, TypeError):
        pass
    return "INDETERMINATE", "OPS_RUN_UNCONFIRMED"
