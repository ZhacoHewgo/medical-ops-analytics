"""Same-instance A/B stage profiler for the certified outpatient fixture.

Opt-in diagnostic only: nothing here is imported by the default suite.
``backend/tests/test_node19_stage_ab.py`` calls :func:`run_stage_ab` and skips
itself unless ``MOPS_STAGE_AB=1``.

Design constraints (see ``node19-stage-ab-r4-20261121.md``):

* No production file is edited.  Every probe is a runtime rebinding of a module
  attribute, put back on teardown.
* Baseline and revision run in the *same* process, same MySQL instance, same
  fixture, same client, alternating windows -- so machine-load drift between
  rounds cannot be mistaken for a code effect.
* Only aggregate wall/CPU milliseconds, statement counts and audit
  reconciliation are printed.  No SQL text, rows, credentials or patient fields.
* The formal gate (50 concurrency x 300 s, P95 targets) is unchanged and is NOT
  reproduced here.  This is a short attribution probe, not the acceptance run.
"""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from statistics import mean, median
import subprocess
import sys
from threading import Barrier, Event, Lock, Thread
from time import monotonic, perf_counter, process_time, sleep, thread_time

import httpx
import uvicorn


CURRENT = ContextVar("mops_stage_accumulator", default=None)
VARIANT = {"name": "revision"}
RESTORE = {"variant": None}
LOG_PATH = os.environ.get("MOPS_STAGE_AB_LOG", "")
# Worker-thread cap owned by this probe; ``limiter`` is None until a token run starts.
TOKEN = {"value": None, "limiter": None, "original": None}
# How many sync handlers are executing right now / at peak, within one window.
PEAK = {"now": 0, "peak": 0}
PEAK_LOCK = Lock()


def PRINT(line, *, flush=True):
    """Emit one aggregate line; pytest captures stdout, so optionally tee to a file.

    ``flush`` mirrors ``print``'s keyword so call sites can use either form; the log
    file is opened/closed per line and is therefore always on disk before returning.
    """
    print(line, flush=flush)
    if LOG_PATH:
        with open(LOG_PATH, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")

# Inclusive buckets that together account for one server-side request.
TOP_LEVEL = ("auth.token", "auth.db", "data", "render.export", "render.rows", "audit.db")

CASES = {
    "summary": {"method": "GET", "path": "/api/v1/outpatient/summary", "options": {}, "audit": ("query", 200)},
    "grouped": {"method": "GET", "path": "/api/v1/reports/outpatient",
                "options": {"params": {"group_by": "day_department"}}, "audit": ("query", 200)},
    "export": {"method": "POST", "path": "/api/v1/reports/export", "options": {}, "audit": ("export", 200)},
}


# --------------------------------------------------------------------------- #
# stage accumulator
# --------------------------------------------------------------------------- #
@contextmanager
def stage(name):
    accumulator = CURRENT.get()
    if accumulator is None:
        yield
        return
    wall, cpu = perf_counter(), thread_time()
    try:
        yield
    finally:
        entry = accumulator["stages"].get(name)
        if entry is None:
            entry = accumulator["stages"][name] = {"wall": 0.0, "cpu": 0.0, "calls": 0}
        entry["wall"] += (perf_counter() - wall) * 1000
        entry["cpu"] += (thread_time() - cpu) * 1000
        entry["calls"] += 1


def _timed(name, function, *, peak=False):
    def probe(*args, **kwargs):
        if peak:
            with _peak(name):
                return function(*args, **kwargs)
        with stage(name):
            return function(*args, **kwargs)

    probe.__wrapped__ = function
    probe.__name__ = getattr(function, "__name__", name)
    return probe


@contextmanager
def _peak(name):
    """Time a stage and record how many of them run at the same time.

    The probe sits on the single call every sync request makes inside its worker
    thread, so the peak is bounded by the token pool: it is the evidence that a
    token setting actually took effect (see ``STAGE_AB_TOKENS_INVALID``).
    """
    with PEAK_LOCK:
        PEAK["now"] += 1
        PEAK["peak"] = max(PEAK["peak"], PEAK["now"])
    try:
        with stage(name):
            yield
    finally:
        with PEAK_LOCK:
            PEAK["now"] -= 1


def reset_peak():
    with PEAK_LOCK:
        PEAK["now"], PEAK["peak"] = 0, 0
    return PEAK["peak"]


def set_thread_tokens(tokens):
    """Cap the worker threads the app under test may use; test process only.

    ``starlette.concurrency.run_in_threadpool`` awaits
    ``anyio.to_thread.run_sync(func)`` without a limiter, and anyio's own
    ``current_default_thread_limiter()`` cannot be read outside a running event
    loop.  Pinning the module attribute gives this probe an explicit limiter and
    a deterministic read-back, without touching a production default.
    """
    import anyio
    import anyio.to_thread as to_thread

    if TOKEN["limiter"] is None:
        TOKEN["limiter"] = anyio.CapacityLimiter(int(tokens))
        TOKEN["original"] = to_thread.run_sync

        async def run_sync(function, *args, abandon_on_cancel=False, cancellable=None, limiter=None):
            return await TOKEN["original"](function, *args, abandon_on_cancel=abandon_on_cancel,
                                           cancellable=cancellable,
                                           limiter=limiter if limiter is not None else TOKEN["limiter"])

        to_thread.run_sync = run_sync
    TOKEN["limiter"].total_tokens = int(tokens)
    TOKEN["value"] = int(tokens)
    return TOKEN["limiter"].total_tokens


def thread_tokens():
    """Read the cap back from the limiter the requests actually borrow from."""
    return None if TOKEN["limiter"] is None else TOKEN["limiter"].total_tokens


def unset_thread_tokens():
    import anyio.to_thread as to_thread

    if TOKEN["original"] is not None:
        to_thread.run_sync = TOKEN["original"]
    TOKEN.update(value=None, limiter=None, original=None)


def _timed_context(name, factory):
    @contextmanager
    def probe(*args, **kwargs):
        with stage(name):
            with factory(*args, **kwargs) as value:
                yield value

    probe.__wrapped__ = factory
    return probe


# --------------------------------------------------------------------------- #
# instrumentation
# --------------------------------------------------------------------------- #
def _patch_sql():
    import pymysql.cursors as cursors

    original = {}
    for method in ("execute", "fetchall", "fetchone"):
        original[method] = getattr(cursors.Cursor, method)

    def execute(self, query, args=None):
        accumulator = CURRENT.get()
        started = perf_counter()
        try:
            return original["execute"](self, query, args)
        finally:
            if accumulator is not None:
                accumulator["db_ms"] += (perf_counter() - started) * 1000
                accumulator["db_calls"] += 1

    def fetchall(self):
        accumulator = CURRENT.get()
        started = perf_counter()
        try:
            return original["fetchall"](self)
        finally:
            if accumulator is not None:
                accumulator["db_ms"] += (perf_counter() - started) * 1000

    def fetchone(self):
        accumulator = CURRENT.get()
        started = perf_counter()
        try:
            return original["fetchone"](self)
        finally:
            if accumulator is not None:
                accumulator["db_ms"] += (perf_counter() - started) * 1000

    cursors.Cursor.execute = execute
    cursors.Cursor.fetchall = fetchall
    cursors.Cursor.fetchone = fetchone

    def restore():
        for method, function in original.items():
            setattr(cursors.Cursor, method, function)

    return restore


def _targets():
    from app import outpatient_preview
    from app.api.routers import outpatient_router as router
    from app.core import deps
    from app.services import indicator_service
    from etl.jobs import build_dims, build_dwd, clean_dwd

    return [
        # function probes: (module, attribute, stage name[, "peak"])
        (deps, "decode_token", "auth.token"),
        (deps, "read_user", "auth.read"),
        (router, "read_user", "audit.read"),
        (router, "query_indicator", "data"),
        (router, "export_report", "render.export"),
        (router, "report_rows", "render.rows"),
        # One call per authenticated request, inside the worker thread: its peak is
        # bounded by the token pool.
        (indicator_service, "load_ads", "data.load", "peak"),
        (outpatient_preview, "read_current_report", "data.report"),
        (outpatient_preview, "read_counts", "data.group_by"),
        (outpatient_preview, "_read_ads_counts", "data.counts"),
        (build_dwd, "verify_build_freshness_from_cursor", "verify"),
        (build_dwd, "_verify_schema", "verify.schema"),
        (build_dwd, "_read_quality_report", "verify.quality_report"),
        (build_dwd, "_previous_certificate", "verify.cert"),
        (build_dwd, "_read", "verify.reads"),
        (build_dwd, "rows_digest", "verify.digest"),
        (build_dwd, "_input_digest", "verify.input_digest"),
        (build_dwd, "_stats", "verify.stats"),
        (build_dwd, "_conserve", "verify.conserve"),
        (clean_dwd, "load_quality_snapshot_from_cursor", "verify.snapshot"),
        (build_dims, "_date_snapshot", "verify.dates"),
        (build_dims, "_read_rows", "verify.dims_read"),
        (build_dims, "dimension_rows", "verify.desired"),
        (outpatient_preview, "connect_database", "data.connect"),
    ]


def _context_targets():
    from app.api.routers import outpatient_router as router
    from app.core import deps

    return [(deps, "database_cursor", "auth.db"), (router, "database_cursor", "audit.db")]


def install():
    """Install every probe.  Returns a restore callable."""
    replaced = []
    for entry in _targets():
        module, attribute, name = entry[:3]
        function = getattr(module, attribute)
        replaced.append((module, attribute, function))
        setattr(module, attribute, _timed(name, function, peak=len(entry) > 3))
    for module, attribute, name in _context_targets():
        function = getattr(module, attribute)
        replaced.append((module, attribute, function))
        setattr(module, attribute, _timed_context(name, function))
    restore_sql = _patch_sql()

    def restore():
        for module, attribute, function in reversed(replaced):
            setattr(module, attribute, function)
        restore_sql()

    return restore


def set_variant(name):
    """Switch the measurement variant without touching production source.

    ``revision`` -- as currently delivered: verification reuses rows already read
    in the same snapshot (certificate rows, dimension rows, date rows and the
    charge detail rows).

    ``baseline`` -- the reuse sites are fed the way r24 fed them: the certificate
    check is called with four positional arguments (so it reads the four
    dimension tables and ``dwd_build_audit`` itself), ``_date_snapshot`` receives
    a snapshot whose ``source.rows`` are temporarily hidden, forcing the original
    per-table SQL, and ``_charge_rows`` is bypassed so the charge table is read
    twice again.

    Both variants keep the same transaction, permissions, checks and audit; only
    the number of round trips differs.  Statement counts are asserted to differ,
    so the switch cannot silently no-op.
    """
    if RESTORE["variant"] is not None:
        RESTORE["variant"]()
        RESTORE["variant"] = None

    from etl.jobs import build_dims, build_dwd

    replaced = []
    if name == "baseline":
        certificate = build_dwd._previous_certificate

        def legacy_certificate(cursor, dataset, batch, env, *already_read):
            return certificate(cursor, dataset, batch, env)

        build_dwd._previous_certificate = legacy_certificate
        replaced.append((build_dwd, "_previous_certificate", certificate))

        dates = build_dims._date_snapshot

        def legacy_dates(cursor, snapshot):
            held = []
            tables = snapshot.get("tables") if isinstance(snapshot, dict) else None
            for entry in (tables or {}).values():
                source = entry.get("source") if isinstance(entry, dict) else None
                if isinstance(source, dict) and "rows" in source:
                    held.append((source, source.pop("rows")))
            try:
                return dates(cursor, snapshot)
            finally:
                for source, rows in held:
                    source["rows"] = rows

        build_dims._date_snapshot = legacy_dates
        replaced.append((build_dims, "_date_snapshot", dates))

        charge_rows = build_dwd._charge_rows

        def legacy_charge_rows(cursor, snapshot, columns, dataset):
            # Resolved at call time so the timed ``_read`` probe still wraps it.
            return build_dwd._read(cursor, "dwd_charge_detail", columns, dataset)

        build_dwd._charge_rows = legacy_charge_rows
        replaced.append((build_dwd, "_charge_rows", charge_rows))
    elif name != "revision":
        raise ValueError(f"unknown variant {name!r}")
    VARIANT["name"] = name

    def restore():
        for module, attribute, function in replaced:
            setattr(module, attribute, function)

    RESTORE["variant"] = restore


# --------------------------------------------------------------------------- #
# ASGI measurement
# --------------------------------------------------------------------------- #
class Measure:
    def __init__(self, app):
        self.app = app
        self.records = []
        self._lock = Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accumulator = {"stages": {}, "db_ms": 0.0, "db_calls": 0, "variant": VARIANT["name"],
                       "token": TOKEN["value"]}
        token = CURRENT.set(accumulator)
        started = perf_counter()
        status = {"code": None}

        async def measured_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            accumulator["wall_ms"] = (perf_counter() - started) * 1000
            accumulator["status"] = status["code"]
            CURRENT.reset(token)
            with self._lock:
                self.records.append(accumulator)

    def drain(self):
        with self._lock:
            records, self.records = self.records, []
        return records


# --------------------------------------------------------------------------- #
# load generator (child process, so the measured server keeps its CPU)
# --------------------------------------------------------------------------- #
def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = max(1, -(-len(ordered) * percentile // 100))
    return round(ordered[index - 1], 2)


def generate(config):
    url, method, path = config["url"], config["method"], config["path"]
    headers, options = config["headers"], config.get("options") or {}
    expected_status, seconds = int(config["expected_status"]), float(config["seconds"])
    workers = int(config["workers"])
    ready, begin = Barrier(workers + 1), Event()
    deadline = [0.0]
    samples, errors = [], Counter()
    guard = Lock()

    def work():
        local = []
        with httpx.Client(base_url=url, timeout=30, trust_env=False) as client:
            try:
                ready.wait(timeout=90)
            except Exception:                                   # noqa: BLE001 - diagnostic
                return
            if not begin.wait(timeout=90):
                return
            while monotonic() < deadline[0]:
                started = perf_counter()
                failure = None
                try:
                    response = client.request(method, path, headers=headers, **options)
                    body = response.content
                    if response.status_code != expected_status:
                        failure = f"http_{response.status_code}"
                    elif response.headers.get("cache-control") != "no-store":
                        failure = "cache_header"
                    elif method == "GET" and json.loads(body).get("code") != 0:
                        failure = "payload_code"
                except Exception as error:                      # noqa: BLE001 - diagnostic
                    failure = type(error).__name__
                local.append((perf_counter() - started) * 1000)
                if failure is not None:
                    with guard:
                        errors[failure] += 1
        with guard:
            samples.extend(local)

    threads = [Thread(target=work) for _ in range(workers)]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=90)
        deadline[0] = monotonic() + seconds
        begin.set()
    finally:
        for thread in threads:
            thread.join(timeout=seconds + 120)
    return {
        "count": len(samples),
        "errors": dict(errors),
        "workers_alive": any(thread.is_alive() for thread in threads),
        "p50_ms": _percentile(samples, 50), "p95_ms": _percentile(samples, 95),
        "p99_ms": _percentile(samples, 99),
        "max_ms": round(max(samples), 2) if samples else None,
        "mean_ms": round(mean(samples), 2) if samples else None,
    }


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #
@contextmanager
def _server(app):
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", proxy_headers=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(400):
                if server.started:
                    break
                sleep(0.025)
            assert server.started, "isolated HTTP server did not start"
            yield f"http://{host}:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            assert not thread.is_alive(), "isolated HTTP server did not stop"


def _stage_table(records):
    table = {}
    for name in sorted({name for record in records for name in record["stages"]}):
        present = [record["stages"][name] for record in records if name in record["stages"]]
        table[name] = {
            "p50_ms": _percentile([entry["wall"] for entry in present], 50),
            "p95_ms": _percentile([entry["wall"] for entry in present], 95),
            "cpu_p50_ms": _percentile([entry["cpu"] for entry in present], 50),
            "cpu_p95_ms": _percentile([entry["cpu"] for entry in present], 95),
            "calls_mean": round(mean([entry["calls"] for entry in present]), 2),
        }
    return table


def _derive(records):
    """Add the two derived buckets so every millisecond is attributable once."""
    for record in records:
        stages = record["stages"]
        covered = sum(stages[name]["wall"] for name in TOP_LEVEL if name in stages)
        stages["dispatch"] = {"wall": max(0.0, record["wall_ms"] - covered), "cpu": 0.0, "calls": 1}
        audit, read = stages.get("audit.db"), stages.get("audit.read")
        if audit is not None:
            stages["audit.write"] = {
                "wall": max(0.0, audit["wall"] - (read["wall"] if read else 0.0)),
                "cpu": max(0.0, audit["cpu"] - (read["cpu"] if read else 0.0)),
                "calls": 1,
            }
    return records


def _window(measured, url, case_name, headers, build, seconds, workers, audit_counts, *, token=None):
    case = CASES[case_name]
    options = dict(case["options"])
    if case_name == "export":
        options["json"] = {"expected_build_run_id": build}
    payload = {"url": url, "method": case["method"], "path": case["path"], "headers": headers,
               "options": options, "expected_status": 200, "seconds": seconds, "workers": workers}
    variant = VARIANT["name"]
    measured.drain()
    reset_peak()
    before = audit_counts()
    cpu_started, wall_started = process_time(), monotonic()
    child = subprocess.run([sys.executable, os.path.abspath(__file__), "--load"],
                           input=json.dumps(payload), text=True, capture_output=True, check=False,
                           env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout=seconds + 180)
    cpu_seconds, wall_seconds = process_time() - cpu_started, monotonic() - wall_started
    assert child.returncode == 0, f"load child exited {child.returncode}: {child.stderr[-400:]}"
    result = json.loads(child.stdout)
    delta = audit_counts() - before
    peak = PEAK["peak"]
    records = _derive([record for record in measured.drain()
                       if record["variant"] == variant and record.get("token") == token])

    server_p95 = _percentile([record["wall_ms"] for record in records], 95)
    stages = _stage_table(records)

    window = {
        "case": case_name, "variant": variant, "tokens": token,
        "peak_in_handler": peak,
        "requests": result["count"], "server_records": len(records),
        "errors": result["errors"], "workers_alive": result["workers_alive"],
        "audit_rows": sum(delta.values()), "audit_expected": result["count"],
        "audit_match": delta == Counter({case["audit"]: result["count"]}),
        "client_p50_ms": result["p50_ms"], "client_p95_ms": result["p95_ms"], "client_p99_ms": result["p99_ms"],
        "server_p50_ms": _percentile([record["wall_ms"] for record in records], 50),
        "server_p95_ms": server_p95,
        "outside_server_ms": (round(result["p95_ms"] - server_p95, 2)
                              if result["p95_ms"] is not None and server_p95 is not None else None),
        "dispatch_ms_p95": stages.get("dispatch", {}).get("p95_ms"),
        "statements_per_request": round(mean([record["db_calls"] for record in records]), 2) if records else None,
        "db_ms_p95": _percentile([record["db_ms"] for record in records], 95),
        "process_cpu_percent": round(100 * cpu_seconds / wall_seconds, 1) if wall_seconds else None,
        "stages": stages,
    }
    PRINT("STAGE_AB_WINDOW " + json.dumps(window, sort_keys=True), flush=True)
    return window


def _audit_reader(names):
    from etl.config import source_config as sources

    def counts():
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute(
                "SELECT action,http_status,COUNT(*) FROM auth_data_access WHERE resource='outpatient' "
                "AND actor_id IN (SELECT user_id FROM auth_users WHERE username IN (%s,%s)) "
                "GROUP BY action,http_status", (names["analyst"], names["department"]),
            )
            return Counter({(action, status): total for action, status, total in cursor.fetchall()})

    return counts


def _capacity(workers):
    """Read-only headroom check before raising the worker-thread cap.

    Auth, data and audit each open their own connection and close it before the
    next stage starts, so one request holds at most one connection at a time; the
    ceiling to compare against is therefore the client count, not twice it.
    """
    from etl.config import source_config as sources

    try:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            cursor.execute("SHOW VARIABLES LIKE %s", ("max_connections",))
            row = cursor.fetchone()
            if row is None:
                return {"error": "max_connections unreadable"}
            ceiling = int(row[1])
            status = {}
            for name in ("Threads_connected", "Threads_running"):
                cursor.execute("SHOW STATUS LIKE %s", (name,))
                status[name.lower()] = int(cursor.fetchone()[1])
    except Exception as error:  # noqa: BLE001 - report, never fail the run
        return {"error": type(error).__name__}
    return dict(status, max_connections=ceiling, workers=workers,
                peak_connections_per_request=1,
                headroom=ceiling - status["threads_connected"],
                enough=headroom_ok(ceiling - status["threads_connected"], workers))


def headroom_ok(headroom, workers):
    return headroom >= workers


def _token_failures(windows, tokens):
    """A token comparison is only valid if the cap actually bound the workers."""
    low, high = min(tokens), max(tokens)
    at_low = [window for window in windows if window["tokens"] == low]
    at_high = [window for window in windows if window["tokens"] == high]
    if not at_low or not at_high:
        return ["token comparison needs at least one window per setting"]
    peak_low = max(window["peak_in_handler"] for window in at_low)
    peak_high = max(window["peak_in_handler"] for window in at_high)
    failures = []
    if peak_low > low:
        failures.append(f"cap {low} was exceeded (peak {peak_low}); the limiter is not binding")
    if high > low and peak_high <= low:
        failures.append(f"cap {high} never lifted handler concurrency above {low} "
                        f"(peak {peak_high}); the setting did not take effect")
    return failures


def run_stage_ab(app, analyst, department, build, names, *, seconds=20, workers=50,
                 rounds=1, cases=("summary", "grouped", "export"), tokens=None):
    """Alternate windows on one instance and print aggregates.

    ``tokens`` switches the experiment axis: instead of baseline/revision it
    alternates the worker-thread cap (e.g. ``(40, 50)``) with the revision code
    path fixed, so only capacity changes between the two windows.
    """
    cpus = os.cpu_count() or 1
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    audit_counts = _audit_reader(names)
    measured = Measure(app)
    windows = []
    PRINT("STAGE_AB_CONFIG " + json.dumps(
        {"seconds": seconds, "workers": workers, "rounds": rounds, "cases": list(cases), "cpus": cpus,
         "tokens": list(tokens) if tokens else None}))
    capacity = _capacity(workers)
    PRINT("STAGE_AB_CAPACITY " + json.dumps(capacity, sort_keys=True), flush=True)
    if tokens and not capacity.get("enough", False):
        PRINT("STAGE_AB_INVALID connection headroom is below the client count "
              f"({capacity.get('headroom')} < {workers})", flush=True)
        raise AssertionError("not enough connection headroom for the token comparison")

    uninstall = install()
    try:
        with _server(measured) as url:
            with httpx.Client(base_url=url, timeout=120, trust_env=False) as warm:
                for name in cases:
                    options = dict(CASES[name]["options"])
                    if name == "export":
                        options["json"] = {"expected_build_run_id": build}
                    headers = analyst if name == "summary" else department
                    response = warm.request(CASES[name]["method"], CASES[name]["path"], headers=headers, **options)
                    assert response.status_code == 200, f"warm-up failed for {name}: {response.status_code}"
            measured.drain()

            for case_index, name in enumerate(cases):
                headers = analyst if name == "summary" else department
                for index in range(rounds):
                    order = ("baseline", "revision")
                    sequence = order if (index + case_index) % 2 == 0 else order[::-1]
                    if tokens:
                        set_variant("revision")
                        choices = list(tokens) if (index + case_index) % 2 == 0 else list(tokens)[::-1]
                        for value in choices:
                            actual = set_thread_tokens(value)
                            assert actual == value, f"limiter read-back {actual} != {value}"
                            window = _window(measured, url, name, headers, build, seconds,
                                             workers, audit_counts, token=value)
                            window["round"] = index + 1
                            window["cpu_of_cores"] = round((window["process_cpu_percent"] or 0.0) / 100 / cpus, 2)
                            window["token_readback"] = thread_tokens()
                            windows.append(window)
                        continue
                    for variant in sequence:
                        set_variant(variant)
                        window = _window(measured, url, name, headers, build, seconds, workers, audit_counts)
                        window["round"] = index + 1
                        window["cpu_of_cores"] = round((window["process_cpu_percent"] or 0.0) / 100 / cpus, 2)
                        windows.append(window)
                set_variant("revision")
    finally:
        set_variant("revision")
        unset_thread_tokens()
        uninstall()

    for window in windows:
        window["group"] = f"tokens={window['tokens']}" if tokens else window["variant"]

    summary = {}
    for name in cases:
        for label in sorted({window["group"] for window in windows if window["case"] == name}):
            group = [window for window in windows if window["case"] == name and window["group"] == label]
            if not group:
                continue
            summary[f"{name}/{label}"] = {
                "windows": len(group),
                "requests": sum(window["requests"] for window in group),
                "errors": sum(sum(window["errors"].values()) for window in group),
                "audit_match": all(window["audit_match"] for window in group),
                "client_p95_ms_median": median([window["client_p95_ms"] or 0.0 for window in group]),
                "server_p95_ms_median": median([window["server_p95_ms"] or 0.0 for window in group]),
                "statements_per_request": round(mean([window["statements_per_request"] or 0.0 for window in group]), 2),
                "stages_p95_ms": {
                    stage_name: median([window["stages"].get(stage_name, {}).get("p95_ms") or 0.0 for window in group])
                    for stage_name in sorted({key for window in group for key in window["stages"]})
                },
                "cpu_of_cores_median": median([window["cpu_of_cores"] for window in group]),
            }
    PRINT("STAGE_AB_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)

    failures = []
    for window in windows:
        label = f"{window['case']}/{window['group']}/r{window['round']}"
        if window["errors"]:
            failures.append(f"{label} errors={window['errors']}")
        if not window["requests"]:
            failures.append(f"{label} produced no requests")
        if window["requests"] != window["server_records"]:
            failures.append(f"{label} client={window['requests']} server={window['server_records']}")
        if not window["audit_match"]:
            failures.append(f"{label} audit={window['audit_rows']} expected={window['audit_expected']}")
        if window["workers_alive"]:
            failures.append(f"{label} load workers did not stop")
        if tokens and window.get("token_readback") != window["tokens"]:
            failures.append(f"{label} limiter read-back {window.get('token_readback')} != {window['tokens']}")
    if tokens:
        failures.extend(_token_failures(windows, tokens))
    else:
        for name in cases:
            baseline = summary.get(f"{name}/baseline", {}).get("statements_per_request")
            revision = summary.get(f"{name}/revision", {}).get("statements_per_request")
            if baseline is None or revision is None or baseline <= revision:
                failures.append(f"{name} A/B switch did not change statement counts "
                                f"(baseline={baseline} revision={revision})")
    if failures:
        for line in failures:
            PRINT("STAGE_AB_INVALID " + line, flush=True)
        raise AssertionError("; ".join(failures))
    PRINT(f"STAGE_AB_OK windows={len(windows)}", flush=True)
    return windows, summary


if __name__ == "__main__" and sys.argv[1:2] == ["--load"]:
    PRINT(json.dumps(generate(json.loads(sys.stdin.read()))))
