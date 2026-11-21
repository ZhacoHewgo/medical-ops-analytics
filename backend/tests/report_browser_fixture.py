"""Optional Chromium check of the application with the existing isolated ADS fixture."""
import os
from pathlib import Path
import re
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep

from openpyxl import load_workbook
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
import uvicorn

from app.main import create_app


def run_report_browser(root, names, password, *, calendar=False):
    app = create_app()
    dist = root / "frontend" / "dist"
    assert (dist / "index.html").is_file(), "Build frontend before running report browser checks"

    @app.get("/login")
    @app.get("/outpatient")
    @app.get("/reports")
    def spa():
        return FileResponse(dist / "index.html")

    app.mount("/", StaticFiles(directory=dist, html=True))
    env = {**os.environ, "MOPS_BROWSER_ANALYST": names["analyst"],
           "MOPS_BROWSER_DEPARTMENT": names["department"], "MOPS_BROWSER_PASSWORD": password}
    if calendar:
        env["MOPS_BROWSER_CALENDAR"] = "1"
    else:
        env.pop("MOPS_BROWSER_CALENDAR", None)
    with socket.socket() as listener, TemporaryDirectory() as output:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", proxy_headers=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                sleep(0.025)
            assert server.started
            result = subprocess.run(
                ["node", str(root / "frontend/tests/report-browser.mjs"), f"http://127.0.0.1:{port}", output],
                env=env, capture_output=True, text=True, timeout=100,
            )
            if result.returncode:
                lines = re.findall(r"report-browser\.mjs:\d+", result.stderr)
                raise AssertionError("Chromium assertion location: " + ", ".join(lines))
            evidence = result.stdout.strip()
            prefix = "REPORT_COMPARISON_OK" if calendar else "REPORT_BROWSER_OK"
            assert re.fullmatch(prefix + r" [a-z0-9_=, ]+", evidence)
            if calendar:
                print(evidence)
                return
            workbook = load_workbook(Path(output) / "report.xlsx", read_only=True)
            try:
                assert list(workbook["门诊人次"].values) == [
                    ("日期", "科室", "门诊人次"),
                    ("2025-01-01", "SIM_DEPT_01", 1),
                    ("2025-01-02", "SIM_DEPT_01", 1),
                    ("2025-01-03", "SIM_DEPT_01", 1),
                ]
            finally:
                workbook.close()
            print(evidence)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
