from collections import Counter
from datetime import datetime

from app.operations import task_events
from etl.config.source_config import validate_batch
from etl.scheduler.scheduler import SHANGHAI


def collect_trial_metrics(start, end, *, env="dev"):
    first, last = validate_batch(start), validate_batch(end)
    if first > last:
        raise ValueError("Invalid trial date range")
    latest = {}
    for event in task_events(env, limit=None):
        day = datetime.fromisoformat(event["at"]).astimezone(SHANGHAI).date().isoformat()
        if first <= day <= last:
            key = event["run_id"], event["name"]
            if key not in latest or event["at"] >= latest[key]["at"]:
                latest[key] = event
    states = Counter(event["status"] for event in latest.values())
    completed = sum(count for status, count in states.items() if status != "START")
    return {
        "start": first, "end": last, "env": env, "task_status_counts": dict(states),
        "completed_tasks": completed, "successful_tasks": states["SUCCESS"],
        "success_rate": states["SUCCESS"] / completed if completed else None,
        "reconciliation_measured": False,
    }
