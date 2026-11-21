"""Serve the test app to Chromium on a private loopback socket."""
import os
import re
import socket
import subprocess
from threading import Thread
from time import sleep

import uvicorn
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.main import create_app


def run_admin_browser(root, names, password, *, scenario="admin", scenario_env=None):
    assert scenario in {"admin", "operations", "registration", "schedule"}
    app = create_app()
    dist = root / "frontend" / "dist"
    assert (dist / "index.html").is_file(), "Build frontend before testing admin browser routes"

    @app.get("/login")
    @app.get("/outpatient")
    @app.get("/admin/users")
    @app.get("/admin/roles")
    @app.get("/ops/sources")
    @app.get("/ops/tasks")
    def spa():
        return FileResponse(dist / "index.html")

    app.mount("/", StaticFiles(directory=dist, html=True))
    env = {**os.environ, "MOPS_BROWSER_PASSWORD": password,
           **{f"MOPS_BROWSER_{role.upper()}": name for role, name in names.items()}, **(scenario_env or {})}
    with socket.socket() as listener:
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
                ["node", str(root / f"frontend/tests/{scenario}-browser.mjs"), f"http://127.0.0.1:{port}"],
                env=env, capture_output=True, text=True, timeout=100,
            )
            if result.returncode:
                lines = re.findall(rf"{scenario}-browser\.mjs:\d+", result.stderr)
                raise AssertionError("Chromium assertion location: " + ", ".join(lines))
            evidence = result.stdout.strip()
            assert re.fullmatch(rf"{scenario.upper()}_BROWSER_OK [a-z0-9_= ]+", evidence)
            print(evidence)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
