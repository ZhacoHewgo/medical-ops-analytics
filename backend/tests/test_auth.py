from datetime import datetime, timezone
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.core import config, deps, security
from app.main import create_app
from etl.config import source_config as sources


LOGIN = "/api/auth/login"
USERS = "/api/auth/users"
PASSWORD = "Synthetic-only!123"


@pytest.fixture(autouse=True)
def private_jwt_file(monkeypatch, tmp_path):
    path = tmp_path / "jwt-secret"
    monkeypatch.setattr(config, "_JWT_SECRET_FILE", path)
    return path


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
        assert response.headers.get("X-Auth-Unavailable") == security.JWT_SECRET_MISSING
        assert response.json()["data"] is None and PASSWORD not in response.text


def test_bcrypt_verification_and_byte_limit():
    stored = security.hash_password(PASSWORD)
    assert len(stored) == 60 and stored.startswith(b"$2b$")
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password("wrong", stored)
    assert not security.verify_password("字" * 25, stored)
    with pytest.raises(ValueError):
        security.hash_password("字" * 25)
    short = "test123"
    assert security.verify_password(short, security.hash_password(short))
    with pytest.raises(ValueError, match="12 to 72"):
        security.hash_account_password(short)
    with pytest.raises(ValueError):
        security.hash_password("")


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
    admin_id = None
    try:
        with sources.database_cursor("AUTH_DB_UNAVAILABLE", write=True) as cursor:
            admin_id = insert_user(cursor, names["admin"], security.hash_password(PASSWORD), "admin", None)
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client, names, key, admin_id
    finally:
        with sources.connect_database(test_only=True) as db, db.cursor() as cursor:
            if audit_ids:
                cursor.executemany("DELETE FROM auth_audit WHERE audit_id=%s", [(audit_id,) for audit_id in audit_ids])
            cursor.execute("DELETE FROM auth_admin_audit WHERE actor_id=%s", (admin_id,))
            cursor.execute("DELETE FROM auth_users WHERE username IN (%s,%s,%s,%s)", (*names.values(), key))
            db.commit()


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_mysql_login_permissions_and_audit(auth_client):
    import jwt

    client, names, other, admin_id = auth_client
    from app.core.auth_store import bootstrap_admin
    assert bootstrap_admin(other, PASSWORD) is False
    with pytest.raises(ValueError, match="12 to 72"):
        bootstrap_admin(other, "test123")
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
    verified = client.get("/api/auth/session", headers=auth_header(admin))
    assert verified.status_code == 204 and verified.headers["Cache-Control"] == "no-store"
    assert not verified.content
    claims = security.decode_token(admin)
    assert claims["user_id"] == admin_id and claims["role"] == "admin"
    assert claims["exp"] - claims["iat"] == 480 * 60

    def create(actor, username, role, department=None):
        return client.post(USERS, headers=auth_header(actor), json={
            "username": username, "password": PASSWORD, "role": role, "department": department,
        })

    weak = client.post(USERS, headers=auth_header(admin), json={
        "username": other, "password": "test123", "role": "analyst",
    })
    assert weak.status_code == 422 and "12 to 72" in weak.json()["msg"]
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


PROBE_SECRET = "p" * 32
PROBE_PASSWORD = "Probe-pass-2026"


class StoreCursor:
    def __init__(self, row):
        self.row = row
        self.statements = []
        self.fetched = None
        self.lastrowid = None

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        self.fetched = self.row

    def fetchone(self):
        return self.fetched

    def fetchall(self):
        return [] if self.fetched is None else [self.fetched]


def probe_store(monkeypatch, row=None, error=None):
    from contextlib import contextmanager

    from app.api.routers import auth_router as routes

    cursor = StoreCursor(row)
    opened = []

    @contextmanager
    def database_cursor(code, *, write=False):
        opened.append(code)
        if error is not None:
            raise sources.OdsError(error)
        yield cursor

    monkeypatch.setattr(deps, "database_cursor", database_cursor)
    monkeypatch.setattr(routes, "database_cursor", database_cursor)
    return cursor, opened


def probe_client(monkeypatch, secret=PROBE_SECRET, minutes="480"):
    from fastapi import APIRouter, FastAPI

    from app.api.routers import auth_router as routes

    monkeypatch.setenv("AUTH_ENABLED", "1")
    if secret is None:
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("JWT_SECRET_KEY", secret)
    if minutes is None:
        monkeypatch.delenv("JWT_EXPIRE_MINUTES", raising=False)
    else:
        monkeypatch.setenv("JWT_EXPIRE_MINUTES", minutes)
    application = FastAPI()
    application.include_router(routes.router)
    probe = APIRouter()

    @probe.get("/__probe")
    def probe_route(user=Depends(deps.get_current_user)):
        return {"user_id": user.user_id, "role": user.role, "department": user.department}

    application.include_router(probe)
    return TestClient(application)


def probe_account(*, role="analyst", department=None, enabled=True, revision=None, password=None):
    from app.core.auth_store import Account

    stored = security.hash_account_password(password or PROBE_PASSWORD)
    return Account(7, "probe01", stored, role, department, enabled,
                   revision or datetime(2026, 1, 1, tzinfo=timezone.utc))


def account_row(**kwargs):
    account = probe_account(**kwargs)
    return (account.user_id, account.username, account.password_hash, account.role,
            account.department, account.enabled, account.revision)


def do_login(client, password=PROBE_PASSWORD):
    return client.post(LOGIN, json={"username": "probe01", "password": password})


def test_login_issues_token_when_configuration_and_store_are_ready(monkeypatch):
    cursor, opened = probe_store(monkeypatch, account_row())
    response = do_login(probe_client(monkeypatch))
    assert response.status_code == 200
    assert opened == ["AUTH_DB_UNAVAILABLE"]
    body = response.json()
    assert body["data"]["user"] == {"user_id": 7, "username": "probe01", "role": "analyst", "department": None}
    assert "password" not in response.text
    claims = security.decode_token(body["data"]["access_token"])
    assert claims["user_id"] == 7 and claims["role"] == "analyst" and claims["iss"] == security.ISSUER
    assert any("auth_audit" in sql for sql, _ in cursor.statements)


def test_login_rejects_unknown_user_and_wrong_password(monkeypatch):
    unknown_cursor, _ = probe_store(monkeypatch, None)
    assert do_login(probe_client(monkeypatch)).status_code == 401
    assert any("auth_audit" in sql for sql, _ in unknown_cursor.statements)
    wrong_cursor, _ = probe_store(monkeypatch, account_row(password="Other-pass-2026"))
    response = do_login(probe_client(monkeypatch))
    assert response.status_code == 401
    assert any("auth_audit" in sql for sql, _ in wrong_cursor.statements)


def test_login_reports_unavailable_when_account_store_fails(monkeypatch):
    _, opened = probe_store(monkeypatch, error="ODS_DATABASE_CONNECT_FAILED")
    response = do_login(probe_client(monkeypatch))
    assert response.status_code == 503
    assert "Authentication unavailable" in response.text
    assert opened == ["AUTH_DB_UNAVAILABLE"]


def test_short_signing_key_fails_before_account_lookup(monkeypatch):
    _, opened = probe_store(monkeypatch, account_row())
    response = do_login(probe_client(monkeypatch, secret="p" * 31))
    assert response.status_code == 503
    assert opened == []


def test_private_jwt_key_signs_and_verifies_without_environment(monkeypatch, private_jwt_file):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    private_jwt_file.write_text(token_urlsafe(48) + "\n", encoding="ascii")
    private_jwt_file.chmod(0o600)
    account = probe_account()
    token = security.create_access_token(account)
    assert security.decode_token(token)["user_id"] == account.user_id

    monkeypatch.setenv("JWT_SECRET_KEY", token_urlsafe(48))
    with pytest.raises(security.HTTPException) as error:
        security.decode_token(token)
    assert error.value.status_code == 401


@pytest.mark.parametrize("kind", ["wide_permissions", "symlink"])
def test_private_jwt_key_rejects_unsafe_files(monkeypatch, private_jwt_file, kind):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    target = private_jwt_file.with_name("source") if kind == "symlink" else private_jwt_file
    target.write_text(token_urlsafe(48), encoding="ascii")
    target.chmod(0o600 if kind == "symlink" else 0o644)
    if kind == "symlink":
        private_jwt_file.symlink_to(target)
    with pytest.raises(security.HTTPException) as error:
        security.token_config()
    assert error.value.headers["X-Auth-Unavailable"] == security.JWT_SECRET_MISSING


@pytest.mark.parametrize("secret", ["   ", "p" * 31])
def test_short_signing_key_reports_unavailable(monkeypatch, secret):
    _, opened = probe_store(monkeypatch, account_row())
    assert do_login(probe_client(monkeypatch, secret=secret)).status_code == 503
    assert opened == []


@pytest.mark.parametrize("minutes", [None, "", "   "])
def test_empty_expire_minutes_uses_documented_default(monkeypatch, minutes):
    probe_store(monkeypatch, account_row())
    client = probe_client(monkeypatch, minutes=minutes)
    assert do_login(client).status_code == 200
    assert security.token_config()[1] == 480


@pytest.mark.parametrize("minutes", ["0", "481", "-5", "abc", "8h"])
def test_invalid_expire_minutes_reports_unavailable(monkeypatch, minutes):
    _, opened = probe_store(monkeypatch, account_row())
    assert do_login(probe_client(monkeypatch, minutes=minutes)).status_code == 503
    assert opened == []


def test_token_signed_with_another_key_is_rejected_not_unavailable(monkeypatch):
    probe_store(monkeypatch, account_row())
    client = probe_client(monkeypatch)
    forged = security.create_access_token(probe_account())
    monkeypatch.setenv("JWT_SECRET_KEY", "q" * 32)
    response = client.get("/__probe", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_current_user_requires_token_and_returns_scope(monkeypatch):
    probe_store(monkeypatch, account_row(role="department", department="SIM_DEPT_03"))
    client = probe_client(monkeypatch)
    assert client.get("/__probe").status_code == 401
    assert client.get("/__probe", headers={"Authorization": "Bearer junk"}).status_code == 401
    token = do_login(client).json()["data"]["access_token"]
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).json() == {
        "user_id": 7, "role": "department", "department": "SIM_DEPT_03"}


def test_mapping_token_without_revision_can_be_verified_but_cannot_authenticate(monkeypatch):
    client = probe_client(monkeypatch)
    _, opened = probe_store(monkeypatch, account_row())
    token = security.create_access_token({"user_id": 7, "role": "analyst", "department": None})
    assert security.decode_token(token)["user_id"] == 7
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert opened == []


@pytest.mark.parametrize("identity", [{"sub": "synthetic-user"}, {"username": "synthetic-user"},
                                       {"user_id": "synthetic-user"}])
def test_subject_only_token_verifies_but_cannot_authenticate(monkeypatch, identity):
    client = probe_client(monkeypatch)
    _, opened = probe_store(monkeypatch, account_row())
    token = security.create_access_token(identity)
    assert security.decode_token(token)["sub"] == "synthetic-user"
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert opened == []


def test_mapping_token_with_current_account_revision_keeps_existing_contract(monkeypatch):
    client = probe_client(monkeypatch)
    account = probe_account()
    probe_store(monkeypatch, account_row())
    token = security.create_access_token(dict(account.public(), revision=account.revision.isoformat()))
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_current_user_rejects_revoked_account_and_stale_revision(monkeypatch):
    client = probe_client(monkeypatch)
    token = security.create_access_token(probe_account())
    probe_store(monkeypatch, account_row(enabled=False))
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    probe_store(monkeypatch, account_row(revision=datetime(2026, 2, 2, tzinfo=timezone.utc)))
    assert client.get("/__probe", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_current_user_reports_unavailable_when_store_fails(monkeypatch):
    client = probe_client(monkeypatch)
    token = security.create_access_token(probe_account())
    probe_store(monkeypatch, error="ODS_DATABASE_CONNECT_FAILED")
    response = client.get("/__probe", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert "Authentication unavailable" in response.text


@pytest.mark.parametrize("role,department,given,expected", [
    ("department", "SIM_DEPT_03", "SIM_DEPT_03", "SIM_DEPT_03"),
    ("department", "SIM_DEPT_03", None, "SIM_DEPT_03"),
    ("analyst", None, "SIM_DEPT_03", "SIM_DEPT_03"),
    ("admin", None, None, None),
])
def test_department_scope_is_applied(role, department, given, expected):
    assert deps.apply_data_scope(given, probe_account(role=role, department=department)) == expected


@pytest.mark.parametrize("role,department,given", [
    ("department", "SIM_DEPT_03", "SIM_DEPT_04"),
    ("department", None, None),
])
def test_department_scope_is_denied(role, department, given):
    with pytest.raises(Exception) as error:
        deps.apply_data_scope(given, probe_account(role=role, department=department))
    assert getattr(error.value, "status_code", None) == 403


@pytest.mark.parametrize("size,allowed", [(11, False), (12, True), (72, True), (73, False), (0, False)])
def test_account_password_policy_boundaries(size, allowed):
    password = "a" * size
    if allowed:
        assert security.verify_password(password, security.hash_account_password(password))
    else:
        with pytest.raises(ValueError):
            security.hash_account_password(password)


CATEGORIES = set(security.JWT_CATEGORIES) | {
    security.STORE_UNCONFIGURED, security.STORE_INVALID,
    security.STORE_UNREACHABLE, security.STORE_FAILED}


@pytest.mark.parametrize("secret,error,category", [
    (None, None, security.JWT_SECRET_MISSING),
    ("", None, security.JWT_SECRET_MISSING),
    ("p" * 31, None, security.JWT_SECRET_SHORT),
    (PROBE_SECRET, "ODS_DATABASE_URL_MISSING", security.STORE_UNCONFIGURED),
    (PROBE_SECRET, "ODS_DEPENDENCY_MISSING", security.STORE_UNCONFIGURED),
    (PROBE_SECRET, "ODS_DATABASE_URL_INVALID", security.STORE_INVALID),
    (PROBE_SECRET, "ODS_TEST_DATABASE_REQUIRED", security.STORE_INVALID),
    (PROBE_SECRET, "ODS_DATABASE_CONNECT_FAILED", security.STORE_UNREACHABLE),
    (PROBE_SECRET, "AUTH_DB_UNAVAILABLE", security.STORE_FAILED),
    (PROBE_SECRET, "ODS_ROLLBACK_FAILED", security.STORE_FAILED),
    (PROBE_SECRET, "ODS_DATABASE_CLOSE_FAILED", security.STORE_FAILED),
    (PROBE_SECRET, "UNEXPECTED_CODE", security.STORE_FAILED),
])
def test_login_unavailable_category_is_fixed(monkeypatch, secret, error, category):
    if error is None:
        probe_store(monkeypatch, account_row())
    else:
        probe_store(monkeypatch, error=error)
    response = do_login(probe_client(monkeypatch, secret=secret))
    assert response.status_code == 503
    assert response.json()["detail"] == "Authentication unavailable"
    assert response.headers["X-Auth-Unavailable"] == category


@pytest.mark.parametrize("error,category", [
    ("ODS_DATABASE_URL_MISSING", security.STORE_UNCONFIGURED),
    ("ODS_DATABASE_CONNECT_FAILED", security.STORE_UNREACHABLE),
    ("AUTH_DB_UNAVAILABLE", security.STORE_FAILED),
])
def test_current_user_unavailable_category_is_fixed(monkeypatch, error, category):
    client = probe_client(monkeypatch)
    token = security.create_access_token(probe_account())
    probe_store(monkeypatch, error=error)
    response = client.get("/__probe", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.headers["X-Auth-Unavailable"] == category


def test_unavailable_category_is_logged(monkeypatch, caplog):
    probe_store(monkeypatch, account_row())
    with caplog.at_level("WARNING", logger="app.auth"):
        do_login(probe_client(monkeypatch, secret="s" * 31))
    assert "authentication unavailable: configuration_secret_too_short" in caplog.text


def test_unavailable_exposes_no_store_internals(monkeypatch):
    probe_store(monkeypatch, error="AUTH_DB_UNAVAILABLE")
    response = do_login(probe_client(monkeypatch))
    category = response.headers["X-Auth-Unavailable"]
    assert category in CATEGORIES
    exposed = category + response.text
    for probe in ("ODS_", "AUTH_DB", "mysql", "auth_users", "auth_audit", "traceback", "write", "read"):
        assert probe not in exposed


def test_store_operation_failure_is_not_split_into_read_or_write(monkeypatch):
    read_client = probe_client(monkeypatch)
    token = security.create_access_token(probe_account())
    probe_store(monkeypatch, error="AUTH_DB_UNAVAILABLE")
    read_response = read_client.get("/__probe", headers={"Authorization": f"Bearer {token}"})
    login_response = do_login(probe_client(monkeypatch))
    assert read_response.status_code == 503 and login_response.status_code == 503
    assert read_response.headers["X-Auth-Unavailable"] == security.STORE_FAILED
    assert login_response.headers["X-Auth-Unavailable"] == security.STORE_FAILED
    assert "read" not in security.STORE_FAILED and "write" not in security.STORE_FAILED


def test_unavailable_log_carries_category_only(monkeypatch, caplog):
    probe_store(monkeypatch, error="AUTH_DB_UNAVAILABLE")
    with caplog.at_level("WARNING", logger="app.auth"):
        do_login(probe_client(monkeypatch))
    messages = [record.getMessage() for record in caplog.records if record.name == "app.auth"]
    assert messages == ["authentication unavailable: account_store_operation_failed"]
    for probe in (PROBE_SECRET, "AUTH_DB", "ODS_", "mysql"):
        assert probe not in caplog.text


def test_jwt_categories_keep_configuration_prefix():
    assert all(category.startswith(security.JWT_CONFIG) for category in security.JWT_CATEGORIES)
    assert security.JWT_CONFIG not in security.JWT_CATEGORIES
    assert len(set(security.JWT_CATEGORIES)) == 4


@pytest.mark.parametrize("secret,category", [
    (None, security.JWT_SECRET_MISSING),
    ("", security.JWT_SECRET_MISSING),
    ("   ", security.JWT_SECRET_SHORT),
    ("p" * 31, security.JWT_SECRET_SHORT),
    ("p" * 32, None),
    ("密" * 10, security.JWT_SECRET_SHORT),
    ("密" * 11, None),
])
def test_secret_length_reports_fixed_category(monkeypatch, secret, category):
    probe_store(monkeypatch, account_row())
    response = do_login(probe_client(monkeypatch, secret=secret))
    if category is None:
        assert response.status_code == 200
        assert "access_token" in response.json()["data"]
    else:
        assert response.status_code == 503
        assert response.json()["detail"] == "Authentication unavailable"
        assert response.headers["X-Auth-Unavailable"] == category


@pytest.mark.parametrize("minutes,category", [
    (None, None),
    ("", None),
    ("   ", None),
    ("1", None),
    ("480", None),
    ("8h", security.JWT_TTL_INVALID),
    ("abc", security.JWT_TTL_INVALID),
    ("480.0", security.JWT_TTL_INVALID),
    ("0", security.JWT_TTL_RANGE),
    ("481", security.JWT_TTL_RANGE),
    ("-5", security.JWT_TTL_RANGE),
])
def test_expire_minutes_reports_fixed_category(monkeypatch, minutes, category):
    probe_store(monkeypatch, account_row())
    response = do_login(probe_client(monkeypatch, minutes=minutes))
    if category is None:
        assert response.status_code == 200
    else:
        assert response.status_code == 503
        assert response.headers["X-Auth-Unavailable"] == category


def test_secret_is_reported_before_expire_minutes(monkeypatch):
    probe_store(monkeypatch, account_row())
    response = do_login(probe_client(monkeypatch, secret="p" * 31, minutes="abc"))
    assert response.status_code == 503
    assert response.headers["X-Auth-Unavailable"] == security.JWT_SECRET_SHORT


def test_jwt_categories_never_carry_values(monkeypatch, caplog):
    probe_store(monkeypatch, account_row())
    short_secret = "s" * 31
    with caplog.at_level("WARNING", logger="app.auth"):
        response = do_login(probe_client(monkeypatch, secret=short_secret, minutes="9999"))
    assert response.headers["X-Auth-Unavailable"] == security.JWT_SECRET_SHORT
    assert short_secret not in caplog.text and short_secret not in response.text
    for value in ("9999", "480", "31", "password"):
        assert value not in response.headers["X-Auth-Unavailable"]
