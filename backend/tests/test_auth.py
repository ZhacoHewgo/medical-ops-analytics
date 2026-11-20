from datetime import datetime, timezone
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.core import deps, security
from app.main import create_app
from etl.config import source_config as sources


LOGIN = "/api/auth/login"
USERS = "/api/auth/users"
PASSWORD = "Synthetic-only!123"


def test_auth_routes_are_opt_in(monkeypatch, aggregate_path):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setenv("OUTPATIENT_DATA_PATH", str(aggregate_path))
    with TestClient(create_app()) as client:
        assert client.post(LOGIN, json={}).status_code == 404
        assert client.get("/api/dashboard/overview").json()["data"]["outpatient_visits"] == 4


def test_auth_with_missing_signing_key_returns_unavailable(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with TestClient(create_app()) as client:
        response = client.post(LOGIN, json={"username": "synthetic_user", "password": PASSWORD})
        assert response.status_code == 503
        assert response.json()["data"] is None and PASSWORD not in response.text


def test_bcrypt_verification_and_byte_limit():
    stored = security.hash_password(PASSWORD)
    assert len(stored) == 60 and stored.startswith(b"$2b$")
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password("wrong", stored)
    assert not security.verify_password("字" * 25, stored)
    with pytest.raises(ValueError):
        security.hash_password("字" * 25)


@pytest.fixture
def auth_client(monkeypatch):
    import os
    if not os.getenv("ODS_TEST_DATABASE_URL"):
        pytest.skip("isolated MySQL test URL not configured")

    assert sources.database_options(test_only=True)["database"] == "hospital_test"
    monkeypatch.setenv("ODS_DATABASE_URL", os.environ["ODS_TEST_DATABASE_URL"])
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", token_urlsafe(48))
    monkeypatch.delenv("JWT_EXPIRE_MINUTES", raising=False)
    prefix = "test_" + uuid4().hex[:10]
    names = {role: f"{prefix}_{role}" for role in ("admin", "analyst", "department")}
    key = prefix + "_other"
    app = create_app()

    @app.get("/__scope-test")
    def scoped(dept: str | None = None, account=Depends(deps.get_current_user)):
        return {"department": deps.apply_data_scope(dept, account)}

    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        sql = sources.PROJECT_ROOT / "sql/auth/auth_tables.sql"
        for command in sql.read_text().split(";"):
            if command.strip():
                cursor.execute(command)
        cursor.execute("SELECT COUNT(*) FROM auth_users WHERE username IN (%s,%s,%s,%s)",
                       (*names.values(), key))
        assert cursor.fetchone() == (0,)
    # HTTP tests seed an explicitly isolated namespace. Bootstrap has its own empty-store guard.
    from app.core.auth_store import insert_user
    from app.api.routers import auth_router

    audit_ids = []
    real_log_action = auth_router.log_action

    def record_audit(cursor, actor, outcome):
        real_log_action(cursor, actor, outcome)
        audit_ids.append(cursor.lastrowid)

    monkeypatch.setattr(auth_router, "log_action", record_audit)
    try:
        with sources.database_cursor("AUTH_DB_UNAVAILABLE", write=True) as cursor:
            admin_id = insert_user(cursor, names["admin"], security.hash_password(PASSWORD), "admin", None)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client, names, key, admin_id
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            if audit_ids:
                cursor.executemany("DELETE FROM auth_audit WHERE audit_id=%s", [(audit_id,) for audit_id in audit_ids])
            cursor.execute("DELETE FROM auth_users WHERE username IN (%s,%s,%s,%s)", (*names.values(), key))
            db.commit()


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_mysql_login_permissions_and_audit(auth_client):
    import jwt

    client, names, other, admin_id = auth_client
    from app.core.auth_store import bootstrap_admin
    assert bootstrap_admin(other, PASSWORD) is False
    unauthenticated = client.post(USERS, json={})
    assert unauthenticated.status_code == 401 and unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    bad = client.post(LOGIN, json={"username": names["admin"], "password": "wrong-password"})
    unknown = client.post(LOGIN, json={"username": other, "password": "wrong-password"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()

    def login(username):
        response = client.post(LOGIN, json={"username": username, "password": PASSWORD})
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert PASSWORD not in response.text and "password_hash" not in response.text
        return response.json()["data"]["access_token"]

    admin = login(names["admin"])
    claims = security.decode_token(admin)
    assert claims["user_id"] == admin_id and claims["role"] == "admin"
    assert claims["exp"] - claims["iat"] == 480 * 60

    def create(actor, username, role, department=None):
        return client.post(USERS, headers=auth_header(actor), json={
            "username": username, "password": PASSWORD, "role": role, "department": department,
        })

    assert create(admin, names["analyst"], "analyst").status_code == 201
    assert create(admin, names["department"], "department", "SIM_DEPT_01").status_code == 201
    assert create(admin, names["department"], "department", "SIM_DEPT_01").status_code == 409
    assert create(admin, other, "department").status_code == 422
    analyst = login(names["analyst"])
    dept = login(names["department"])
    assert create(analyst, other, "analyst").status_code == 403
    assert create(dept, other, "analyst").status_code == 403
    very_old = int(datetime.now(timezone.utc).timestamp()) - 60
    expired = jwt.encode({**claims, "iat": very_old - 60, "exp": very_old},
                         security.get_settings().jwt_secret_key, algorithm="HS256")
    wrong_signature = jwt.encode(claims, token_urlsafe(48), algorithm="HS256")
    for invalid in (expired, wrong_signature, "malformed"):
        assert client.get("/__scope-test", headers=auth_header(invalid)).status_code == 401
    assert client.get("/__scope-test", headers=auth_header(analyst)).json()["department"] is None
    assert client.get("/__scope-test", headers=auth_header(dept)).json()["department"] == "SIM_DEPT_01"
    assert client.get("/__scope-test?dept=SIM_DEPT_02", headers=auth_header(dept)).status_code == 403
    assert client.get("/__scope-test?dept=SIM_DEPT_01,SIM_DEPT_02", headers=auth_header(dept)).status_code == 403
    assert client.get("/__scope-test?dept=SIM_DEPT_01", headers=auth_header(dept)).json()["department"] == "SIM_DEPT_01"

    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute("SELECT password_hash FROM auth_users WHERE username=%s", (names["department"],))
        stored, = cursor.fetchone()
        assert security.verify_password(PASSWORD, stored) and PASSWORD.encode() not in stored
        cursor.execute("SELECT outcome FROM auth_audit WHERE actor_id=%s AND action='login'", (admin_id,))
        assert {r[0] for r in cursor.fetchall()} == {"success", "denied"}
        cursor.execute("UPDATE auth_users SET department='SIM_DEPT_02' WHERE username=%s", (names["department"],))
        cursor.execute("UPDATE auth_users SET role='department',department='SIM_DEPT_01' WHERE username=%s",
                       (names["analyst"],))
        db.commit()
    assert client.get("/__scope-test", headers=auth_header(dept)).status_code == 401
    assert client.get("/__scope-test", headers=auth_header(analyst)).status_code == 401
    updated = login(names["department"])
    assert client.get("/__scope-test", headers=auth_header(updated)).json()["department"] == "SIM_DEPT_02"
    with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
        cursor.execute("UPDATE auth_users SET enabled=FALSE WHERE username=%s", (names["department"],))
        db.commit()
    assert client.get("/__scope-test", headers=auth_header(updated)).status_code == 401
    assert client.post(LOGIN, json={"username": names["department"], "password": PASSWORD}).status_code == 401
