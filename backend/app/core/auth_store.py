"""MySQL account and login audit storage for the opt-in authentication routes."""
from argparse import ArgumentParser
from dataclasses import dataclass, field
from datetime import datetime
from getpass import getpass
import re
import sys

from pymysql.err import IntegrityError

from app.core.security import hash_password
from etl.config.source_config import OdsError, database_cursor


USERNAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
DEPARTMENT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
DB_DOWN = "AUTH_DB_UNAVAILABLE"


@dataclass(frozen=True)
class Account:
    user_id: int
    username: str
    password_hash: bytes = field(repr=False)
    role: str
    department: str | None
    enabled: bool
    revision: datetime

    def public(self):
        return {"user_id": self.user_id, "username": self.username,
                "role": self.role, "department": self.department}


def read_user(cursor, *, username=None, user_id=None, lock=False):
    condition, value = ("username", username) if username is not None else ("user_id", user_id)
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        "SELECT user_id, username, password_hash, role, department, enabled, revision "
        f"FROM auth_users WHERE {condition}=%s" + suffix, (value,),
    )
    row = cursor.fetchone()
    return None if row is None else Account(*row)


def insert_user(cursor, username, password_hash, role, department):
    try:
        cursor.execute(
            "INSERT INTO auth_users (username, password_hash, role, department) VALUES (%s,%s,%s,%s)",
            (username, password_hash, role, department),
        )
    except IntegrityError as error:
        if error.args and error.args[0] == 1062:
            return None
        raise
    return cursor.lastrowid


def log_action(cursor, actor_id, outcome):
    cursor.execute(
        "INSERT INTO auth_audit (actor_id, action, outcome) VALUES (%s,'login',%s)", (actor_id, outcome),
    )


def bootstrap_admin(username, password):
    if re.fullmatch(USERNAME, username) is None:
        raise ValueError("Invalid username")
    stored = hash_password(password)
    with database_cursor(DB_DOWN, write=True) as cursor:
        cursor.execute("SELECT user_id FROM auth_users WHERE role='admin' LIMIT 1 FOR UPDATE")
        if cursor.fetchone() is not None:
            return False
        return insert_user(cursor, username, stored, "admin", None) is not None


def main():
    parser = ArgumentParser(description="Create the initial admin after applying the auth schema.")
    parser.add_argument("username")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.exit(1, "A terminal is required for password entry\n")
    try:
        password = getpass("Admin password: ")
        if password != getpass("Confirm password: "):
            parser.exit(1, "Passwords do not match\n")
        if not bootstrap_admin(args.username, password):
            parser.exit(1, "Admin already exists or username is taken\n")
    except (OdsError, ValueError):
        parser.exit(1, "Invalid input or database unavailable\n")
    print("Initial admin created")


if __name__ == "__main__":
    main()
