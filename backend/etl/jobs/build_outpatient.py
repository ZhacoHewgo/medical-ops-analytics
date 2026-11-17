"""Read-only CSV ingestion, whole-ID conflict isolation, aggregate-only output."""

import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.outpatient import (
    DEFAULT_DATA_PATH, PROJECT_ROOT, QUALITY_KEYS, SOURCE_FILE, UNKNOWN,
    strict_date, validate_aggregate,
)


def build_outpatient(source: Path, target: Path) -> dict:
    raw = source.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = reader.fieldnames
    if not columns or not {"visit_id", "visit_date", "dept_code"}.issubset(columns) or len(set(columns)) != len(columns):
        raise ValueError("CSV required columns missing or repeated")
    quality = dict.fromkeys(sorted(QUALITY_KEYS), 0)
    groups = defaultdict(list)
    for values in reader:
        if None in values or any(value is None for value in values.values()):
            raise ValueError("CSV row has wrong number of fields")
        normalized = {key: value.strip() for key, value in values.items()}
        quality["raw_rows"] += 1
        visit_id = normalized["visit_id"]
        if not visit_id:
            quality["invalid_id_rows"] += 1
            continue
        try:
            strict_date(normalized["visit_date"])
        except ValueError:
            quality["invalid_date_rows"] += 1
            continue
        groups[visit_id].append(tuple(normalized[column] for column in columns))
    date_index, dept_index = columns.index("visit_date"), columns.index("dept_code")
    counts = Counter()
    for rows in groups.values():
        if any(value != rows[0] for value in rows[1:]):
            quality["conflicting_id_rows"] += len(rows)
            continue
        quality["duplicate_rows_removed"] += len(rows) - 1
        quality["accepted_visits"] += 1
        code = rows[0][dept_index] or UNKNOWN
        quality["unknown_department_visits"] += int(code == UNKNOWN)
        counts[(rows[0][date_index], code)] += 1
    if not counts:
        raise ValueError("CSV has no accepted visits")
    dates = [day for day, _ in counts]
    document = {
        "meta": {
            "schema_version": "1.0", "simulated": True,
            "source_file": SOURCE_FILE, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "available_start_date": min(dates), "available_end_date": max(dates),
            "department_codes": sorted({code for _, code in counts}),
            "quality_scope": "whole_source", "quality": quality,
        },
        "counts": [
            {"date": day, "dept_code": code, "visits": count}
            for (day, code), count in sorted(counts.items())
        ],
    }
    validate_aggregate(document)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return document


def main() -> int:
    try:
        document = build_outpatient(PROJECT_ROOT / SOURCE_FILE, DEFAULT_DATA_PATH)
    except (OSError, ValueError, UnicodeError, csv.Error):
        print("Outpatient build failed: source unavailable, invalid CSV, no valid visits, or output write failed.", file=sys.stderr)
        return 1
    print(json.dumps({"outpatient_visits": document["meta"]["quality"]["accepted_visits"], "meta": document["meta"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
