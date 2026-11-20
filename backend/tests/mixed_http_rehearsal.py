"""Opt-in concurrent HTTP rehearsal for the certified outpatient fixture."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import json
from math import ceil
import socket
import subprocess
import sys
from threading import Barrier, Event, Thread
from typing import Callable
from time import monotonic, sleep

import httpx
from openpyxl import load_workbook
import uvicorn


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str
    headers: dict
    status: int
    validate: Callable[[httpx.Response], None]
    options: dict
    audit: tuple | None


@contextmanager
def _server(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", proxy_headers=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                sleep(0.025)
            assert server.started, "The isolated HTTP server did not start"
            yield url
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive(), "The isolated HTTP server did not stop"


def _cases(analyst, department, truth, build, dept, other, empty_day):
    scoped = {key: count for key, count in truth.items() if key[1] == dept}

    def success(response):
        assert response.headers["Cache-Control"] == "no-store"
        value = response.json()
        assert value["code"] == 0
        data = value["data"]
        assert data["meta"]["build_run_id"] == build
        return data

    def summary(expected, scope):
        def validate(response):
            data = success(response)
            days, units = Counter(), Counter()
            for (day, unit), count in expected.items():
                days[day] += count
                units[unit] += count
            assert data["filters"]["dept_code"] == scope
            if scope:
                assert data["meta"]["department_codes"] == [scope]
            assert data["outpatient_visits"] == sum(expected.values())
            assert [(row["date"], row["visits"]) for row in data["daily_trend"]] == sorted(days.items())
            assert {row["dept_code"]: row["visits"] for row in data["department_ranking"]} == units
        return validate

    def grouped(response):
        data = success(response)
        assert data["group_by"] == "day_department" and data["filters"]["dept_code"] == dept
        assert data["meta"]["department_codes"] == [dept]
        assert sorted((row["date"], row["dept_code"], row["visits"]) for row in data["rows"]) == scoped_rows

    scoped_rows = sorted((day, unit, value) for (day, unit), value in scoped.items())
    expected_workbook = [("日期", "科室", "门诊人次"), *scoped_rows]

    def export(response):
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Type"].endswith("spreadsheetml.sheet")
        book = load_workbook(BytesIO(response.content), read_only=True)
        try:
            assert list(book["门诊人次"].values) == expected_workbook
            assert dict(book["数据来源"].values)["构建号"] == build
        finally:
            book.close()

    def rejected(response):
        value = response.json()
        assert value["code"] == response.status_code and value["data"] is None

    path = "/api/v1/outpatient/summary"
    return (
        Case("summary_hospital", "GET", path, analyst, 200, summary(truth, None), {}, ("query", 200)),
        Case("summary_department", "GET", path, department, 200, summary(scoped, dept), {}, ("query", 200)),
        Case("summary_empty", "GET", path, analyst, 200, summary({}, None),
             {"params": {"start_date": empty_day, "end_date": empty_day}}, ("query", 200)),
        Case("grouped", "GET", "/api/v1/reports/outpatient", department, 200, grouped,
             {"params": {"group_by": "day_department"}}, ("query", 200)),
        Case("xlsx", "POST", "/api/v1/reports/export", department, 200, export,
             {"json": {"expected_build_run_id": build}}, ("export", 200)),
        Case("auth_missing", "GET", path, {}, 401, rejected, {}, None),
        Case("cross_department", "GET", path, department, 403, rejected,
             {"params": {"dept_code": other}}, ("query", 403)),
        Case("invalid_date", "GET", path, analyst, 422, rejected,
             {"params": {"end_date": "2025-01-05"}}, ("query", 422)),
    )


def _percentile(samples, percentile):
    if not samples:
        return None
    samples = sorted(samples)
    return round(samples[ceil(len(samples) * percentile / 100) - 1], 2)


def _request(client, case, *, record=False):
    headers = {**case.headers, "x-rehearsal-case": case.name} if record else case.headers
    started = monotonic()
    try:
        response = client.request(case.method, case.path, headers=headers, **case.options)
        elapsed = (monotonic() - started) * 1000
        if response.status_code != case.status:
            return elapsed, f"unexpected_http_{response.status_code}"
        case.validate(response)
    except Exception as error:
        return (monotonic() - started) * 1000, type(error).__name__
    return elapsed, None


def _drive(url, cases, seconds, workers):
    ready = Barrier(workers + 1)
    begin = Event()
    deadline = 0

    def work(index):
        samples = {case.name: [] for case in cases}
        errors = Counter()
        with httpx.Client(base_url=url, timeout=12, trust_env=False) as client:
            ready.wait(timeout=30)
            if begin.wait(timeout=30):
                while monotonic() < deadline:
                    case = cases[index % len(cases)]
                    elapsed, error = _request(client, case, record=True)
                    samples[case.name].append(elapsed)
                    if error is not None:
                        errors[(case.name, error)] += 1
                    index += 1
        return samples, errors

    all_samples = {case.name: [] for case in cases}
    errors = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [pool.submit(work, index) for index in range(workers)]
        ready.wait(timeout=30)
        started = monotonic()
        deadline = started + seconds
        begin.set()
        for future in pending:
            samples, failures = future.result()
            for name, values in samples.items():
                all_samples[name].extend(values)
            errors.update(failures)
    return {
        "elapsed_seconds": round(monotonic() - started, 3),
        "samples": all_samples,
        "errors_by_case": {f"{name}/{reason}": count for (name, reason), count in errors.items()},
    }


class _TimedApp:
    def __init__(self, app, cases):
        self.app = app
        self.samples = {case.name.encode("ascii"): [] for case in cases}

    async def __call__(self, scope, receive, send):
        durations = None
        if scope["type"] == "http":
            marker = dict(scope["headers"]).get(b"x-rehearsal-case")
            durations = self.samples.get(marker)
        if durations is None:
            await self.app(scope, receive, send)
            return

        started = monotonic()

        async def measured_send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                durations.append((monotonic() - started) * 1000)

        await self.app(scope, receive, measured_send)


def rehearse(app, analyst, department, truth, build, dept, other, empty_day, audit_counts, *, seconds):
    cases = _cases(analyst, department, truth, build, dept, other, empty_day)
    workers = 50
    payload = {
        "analyst": analyst, "department": department,
        "truth": [[day, unit, count] for (day, unit), count in truth.items()],
        "build": build, "dept": dept, "other": other, "empty_day": empty_day,
        "seconds": seconds, "workers": workers,
    }
    server_app = _TimedApp(app, cases)
    with _server(server_app) as url:
        with httpx.Client(base_url=url, timeout=12, trust_env=False) as warm:
            for case in cases:
                assert _request(warm, case)[1] is None, f"Warm-up verification failed for {case.name}"
        before = audit_counts()
        load = subprocess.run(
            [sys.executable, __file__],
            input=json.dumps({**payload, "url": url}), text=True, capture_output=True, check=False,
            env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout=seconds + 60,
        )
        assert load.returncode == 0, f"Isolated load process exited {load.returncode}"
        client_result = json.loads(load.stdout)
    daily = {}
    expected_audit = Counter()
    for case in cases:
        client_samples = client_result["samples"][case.name]
        server_samples = server_app.samples[case.name.encode("ascii")]
        daily[case.name] = {
            "expected_http": case.status, "client_requests": len(client_samples), "server_requests": len(server_samples),
            **{f"client_p{p}_ms": _percentile(client_samples, p) for p in (50, 95, 99)},
            **{f"server_p{p}_ms": _percentile(server_samples, p) for p in (50, 95, 99)},
        }
        if case.audit:
            expected_audit[case.audit] += len(client_samples)
    audit_delta = audit_counts() - before
    request_match = all(0 < row["client_requests"] == row["server_requests"] for row in daily.values())
    result = {
        "concurrency": workers, "window_seconds": seconds, "elapsed_seconds": client_result["elapsed_seconds"],
        "requests": sum(row["client_requests"] for row in daily.values()),
        "errors": sum(client_result["errors_by_case"].values()),
        "errors_by_case": client_result["errors_by_case"], "request_match": request_match,
        "audit_rows": sum(audit_delta.values()), "audit_match": audit_delta == expected_audit, "cases": daily,
    }
    print("SPLIT_LOAD_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    assert request_match and not result["errors"] and result["audit_match"], "Split HTTP rehearsal did not reconcile"


if __name__ == "__main__":
    config = json.load(sys.stdin)
    cases = _cases(
        config["analyst"], config["department"],
        {(day, unit): count for day, unit, count in config["truth"]},
        config["build"], config["dept"], config["other"], config["empty_day"],
    )
    print(json.dumps(_drive(config["url"], cases, config["seconds"], config["workers"])))
