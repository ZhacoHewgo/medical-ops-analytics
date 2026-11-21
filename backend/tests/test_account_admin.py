import os

import pytest
from fastapi.testclient import TestClient
from pymysql.err import OperationalError

from app.main import create_app
from etl.config import source_config as sources
from test_auth import LOGIN, PASSWORD, USERS, auth_client, auth_header


ROLES = "/api/auth/roles"
UNIT = "SIM_DEPT_01"
OTHER_UNIT = "SIM_DEPT_02"


def test_admin_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    with TestClient(create_app()) as client:
        assert client.get(ROLES).status_code == 401
        assert client.get(USERS).status_code == 401
        assert client.patch(USERS + "/1", json={}).status_code == 401


def test_mysql_managed_accounts_are_scoped_audited_and_versioned(auth_client, monkeypatch):
    client, names, spare, admin_id = auth_client

    def login(name):
        response = client.post(LOGIN, json={"username": name, "password": PASSWORD})
        assert response.status_code == 200
        return auth_header(response.json()["data"]["access_token"])

    admin = login(names["admin"])

    def create(name, role, department=None):
        return client.post(USERS, headers=admin, json={
            "username": name, "password": PASSWORD, "role": role, "department": department,
        })

    assert create(names["analyst"], "analyst").status_code == 201
    assert create(names["department"], "department", UNIT).status_code == 201
    analyst = login(names["analyst"])
    department = login(names["department"])

    def accounts():
        response = client.get(USERS, headers=admin)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert PASSWORD not in response.text and "password_hash" not in response.text
        return {row["username"]: row for row in response.json()["data"]["items"]}

    def update(account, **changes):
        settings = {key: account[key] for key in ("role", "department", "enabled")}
        return client.patch(f"{USERS}/{account['user_id']}", headers=admin, json={
            **settings, "expected_revision": account["revision"], **changes,
        })

    rows = accounts()
    target = rows[names["department"]]
    assert target["enabled"] is True and target["department"] == UNIT
    assert set(target) == {"user_id", "username", "role", "department", "enabled", "revision"}
    first = client.get(USERS, headers=admin, params={"limit": 1}).json()["data"]
    assert len(first["items"]) == 1 and first["next_after"] == first["items"][0]["user_id"]
    later = client.get(USERS, headers=admin, params={"after": first["next_after"], "limit": 1}).json()["data"]
    assert later["items"][0]["user_id"] > first["next_after"]
    definitions = client.get(ROLES, headers=admin).json()["data"]
    assert {role["code"] for role in definitions} == {"admin", "analyst", "department"}
    assert {role["code"] for role in definitions if "manage_accounts" in role["permissions"]} == {"admin"}

    for principal in (analyst, department):
        assert client.get(USERS, headers=principal).status_code == 403
        assert client.get(ROLES, headers=principal).status_code == 403
        assert client.patch(f"{USERS}/{target['user_id']}", headers=principal, json={}).status_code == 403

    assert update(rows[names["admin"]], role="analyst", enabled=False).status_code == 409
    assert update(target, department=None).status_code == 422
    assert update(target, role="admin", department=None).status_code == 422
    assert update(target, enabled="false").status_code == 422
    assert update(target, user_id=1).status_code == 422
    assert update(target).status_code == 200

    from app.api.routers import auth_router

    def audit_unavailable(*_args, **_kwargs):
        raise OperationalError(1105, "fixture audit failure")

    with monkeypatch.context() as failure:
        failure.setattr(auth_router, "log_admin_change", audit_unavailable)
        assert create(spare, "analyst").status_code == 503
        assert update(target, enabled=False).status_code == 503
    assert accounts()[names["department"]] == target
    assert spare not in accounts()

    changed = update(target, department=OTHER_UNIT)
    assert changed.status_code == 200
    assert client.get("/__scope-test", headers=department).status_code == 401
    assert client.get("/__scope-test", headers=login(names["department"])).json()["department"] == OTHER_UNIT
    assert update(target, enabled=False).status_code == 409

    current = changed.json()["data"]
    disabled = update(current, enabled=False)
    assert disabled.status_code == 200 and disabled.json()["data"]["enabled"] is False
    assert client.post(LOGIN, json={"username": names["department"], "password": PASSWORD}).status_code == 401
    restored = update(disabled.json()["data"], enabled=True, role="analyst", department=None)
    assert restored.status_code == 200 and restored.json()["data"]["department"] is None
    assert client.get("/__scope-test", headers=login(names["department"])).json()["department"] is None
    assert client.get("/__scope-test", headers=department).status_code == 401

    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute(
            "SELECT action,previous_role,previous_department,previous_enabled,role,department,enabled "
            "FROM auth_admin_audit WHERE actor_id=%s AND target_id=%s ORDER BY audit_id",
            (admin_id, target["user_id"]),
        )
        assert list(cursor.fetchall()) == [
            ("create", None, None, None, "department", UNIT, 1),
            ("update", "department", UNIT, 1, "department", OTHER_UNIT, 1),
            ("update", "department", OTHER_UNIT, 1, "department", OTHER_UNIT, 0),
            ("update", "department", OTHER_UNIT, 0, "analyst", None, 1),
        ]
    print("ACCOUNT_ADMIN_OK roles=3 denial=403 stale=409 revocation=401 audit=4 rollback=2")


@pytest.mark.skipif(os.environ.get("MOPS_ADMIN_BROWSER") != "1", reason="requires Chromium and built frontend")
def test_mysql_browser_admin_users(auth_client):
    from admin_browser_fixture import run_admin_browser

    client, names, _, admin_id = auth_client
    login = client.post(LOGIN, json={"username": names["admin"], "password": PASSWORD})
    assert login.status_code == 200
    admin = auth_header(login.json()["data"]["access_token"])
    assert client.post(USERS, headers=admin, json={
        "role": "analyst", "username": names["analyst"], "password": PASSWORD,
    }).status_code == 201
    run_admin_browser(sources.PROJECT_ROOT, names, PASSWORD)

    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute("SELECT action,department,enabled FROM auth_admin_audit WHERE actor_id=%s AND target_id=("
                       "SELECT user_id FROM auth_users WHERE username=%s) ORDER BY audit_id",
                       (admin_id, names["department"]))
        assert list(cursor.fetchall()) == [
            ("create", UNIT, 1), ("update", OTHER_UNIT, 1), ("update", OTHER_UNIT, 0), ("update", OTHER_UNIT, 1),
        ]
