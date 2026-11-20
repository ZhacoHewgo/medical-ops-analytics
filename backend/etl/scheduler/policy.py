"""Versioned settings for an explicitly managed foreground scheduler."""
from dataclasses import dataclass

from etl.config.source_config import database_cursor

DB_ERROR = "OPS_SCHEDULE_POLICY_UNAVAILABLE"


@dataclass(frozen=True)
class SchedulePolicy:
    enabled: bool = True
    hour: int = 2
    minute: int = 0
    version: int = 0


def read_policy(env, cursor=None, *, lock=False):
    if cursor is None:
        with database_cursor(DB_ERROR) as reader:
            return read_policy(env, reader)
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        "SELECT enabled,run_hour,run_minute,version FROM ops_schedule_policy WHERE env=%s" + suffix, (env,),
    )
    row = cursor.fetchone()
    return SchedulePolicy(bool(row[0]), *row[1:]) if row is not None else SchedulePolicy()


def write_policy(cursor, env, actor_id, prior, enabled, hour, minute):
    version = prior.version + 1
    cursor.execute(
        "INSERT INTO ops_schedule_policy (env,enabled,run_hour,run_minute,version) VALUES (%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE enabled=%s,run_hour=%s,run_minute=%s,version=%s",
        (env, enabled, hour, minute, version, enabled, hour, minute, version),
    )
    cursor.execute(
        "INSERT INTO ops_schedule_audit (actor_id,env,previous_enabled,previous_hour,previous_minute,"
        "enabled,run_hour,run_minute,version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (actor_id, env, prior.enabled, prior.hour, prior.minute, enabled, hour, minute, version),
    )
    return SchedulePolicy(enabled, hour, minute, version)


def claim_day(env, local_day, version):
    with database_cursor(DB_ERROR, write=True) as cursor:
        policy = read_policy(env, cursor, lock=True)
        if not policy.enabled or policy.version != version:
            return False
        cursor.execute(
            "SELECT policy_version FROM ops_schedule_dispatch WHERE env=%s AND local_day=%s FOR UPDATE",
            (env, local_day),
        )
        if cursor.fetchone() is not None:
            return False
        cursor.execute(
            "INSERT INTO ops_schedule_dispatch (env,local_day,policy_version) VALUES (%s,%s,%s)",
            (env, local_day, version),
        )
        return True
