"""Shared aggregate format and strict validation; never includes source rows."""

import re
from datetime import date, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILE = "data/mock_his/his_outpatient.csv"
DEFAULT_DATA_PATH = PROJECT_ROOT / "data/derived/outpatient.json"
UNKNOWN = "__UNKNOWN__"
QUALITY_KEYS = {
    "raw_rows", "invalid_id_rows", "invalid_date_rows", "conflicting_id_rows",
    "duplicate_rows_removed", "accepted_visits", "unknown_department_visits",
}
META_KEYS = {
    "schema_version", "simulated", "source_file", "source_sha256", "generated_at",
    "available_start_date", "available_end_date", "department_codes", "quality_scope", "quality",
}


def strict_date(value: str) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("Date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("Aggregate schema or conservation check failed")


def validate_aggregate(document: object) -> dict:
    require(type(document) is dict and set(document) == {"meta", "counts"})
    meta, counts = document["meta"], document["counts"]
    require(type(meta) is dict and set(meta) == META_KEYS)
    require(meta["schema_version"] == "1.0" and meta["simulated"] is True)
    require(meta["source_file"] == SOURCE_FILE and meta["quality_scope"] == "whole_source")
    require(isinstance(meta["source_sha256"], str))
    require(re.fullmatch(r"[0-9a-f]{64}", meta["source_sha256"]) is not None)
    require(isinstance(meta["generated_at"], str))
    require(re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        meta["generated_at"],
    ) is not None)
    generated_at = datetime.fromisoformat(meta["generated_at"])
    require(generated_at.tzinfo is not None and generated_at.utcoffset() is not None)
    strict_date(meta["available_start_date"])
    strict_date(meta["available_end_date"])
    quality = meta["quality"]
    require(type(quality) is dict and set(quality) == QUALITY_KEYS)
    require(all(type(value) is int and value >= 0 for value in quality.values()))
    require(quality["accepted_visits"] > 0)
    require(quality["raw_rows"] == sum(quality[key] for key in (
        "invalid_id_rows", "invalid_date_rows", "conflicting_id_rows", "duplicate_rows_removed", "accepted_visits",
    )))
    require(type(counts) is list and bool(counts))
    dates, departments, buckets = set(), set(), set()
    total, unknown_total = 0, 0
    for item in counts:
        require(type(item) is dict and set(item) == {"date", "dept_code", "visits"})
        strict_date(item["date"])
        code, visits = item["dept_code"], item["visits"]
        require(isinstance(code, str) and bool(code) and code == code.strip())
        code.encode("utf-8")
        require(type(visits) is int and visits > 0)
        bucket = (item["date"], code)
        require(bucket not in buckets)
        buckets.add(bucket)
        dates.add(item["date"])
        departments.add(code)
        total += visits
        if code == UNKNOWN:
            unknown_total += visits
    require(type(meta["department_codes"]) is list and meta["department_codes"] == sorted(departments))
    require(meta["available_start_date"] == min(dates) and meta["available_end_date"] == max(dates))
    require(total == quality["accepted_visits"])
    require(unknown_total == quality["unknown_department_visits"])
    return document
