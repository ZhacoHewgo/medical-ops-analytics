"""Node07 fixed quality checks. Private snapshots never cross report boundaries."""

import base64
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
from uuid import UUID

from etl.config import course_mapping
from etl.config.source_config import COURSE_DATASET, OdsError, SOURCES, database_options

_LOG_ROOT = Path(__file__).resolve().parents[1] / "logs"
_CONFIG = Path(__file__).resolve().parents[1] / "config/dwd_rules.json"
_RULES_VERSION = "node07-rules-v1"
LEGACY_REPORT_VERSION = "node07-quality-v1"
REPORT_VERSION = "node07-quality-v2"
REPORT_VERSIONS = (LEGACY_REPORT_VERSION, REPORT_VERSION)
_MAPPING_VERSION = course_mapping.SIM_VERSION
_COURSE_KEY_ID = "course-platform-v1-key"
PRIVACY_KEY_ERRORS = frozenset(("DWD_PRIVACY_KEY_MISSING", "DWD_PRIVACY_KEY_INVALID"))
_COMMON = "dataset_id source_row_number source_batch_date etl_batch_date etl_load_time patient_key".split()
# Only frozen fields/constants; configuration cannot introduce SQL or expressions.
_SPECS = {
    "dwd_outpatient_visit_detail": ("outpatient", "his_outpatient", "visit_id", "visit_date", "visit_id visit_date dept_code visit_type gender age fee_amount patient_name id_card phone", "age fee_amount patient_key", "visit_date age fee_amount", "dept_code visit_type gender"),
    "dwd_inpatient_visit_detail": ("inpatient", "his_inpatient", "visit_id", "admit_date", "visit_id admit_date discharge_date dept_code gender age stay_days total_fee drug_fee consumable_fee disease_code patient_name id_card phone", "discharge_date age stay_days total_fee drug_fee consumable_fee patient_key", "admit_date discharge_date age stay_days total_fee drug_fee consumable_fee", "dept_code gender disease_code"),
    "dwd_charge_detail": ("charge", "his_charge", "charge_id", "fee_date", "charge_id visit_id visit_type dept_code fee_date fee_type_code amount", "visit_id amount patient_key", "visit_id fee_date amount", "dept_code visit_type fee_type_code"),
    "dwd_emr_record_detail": ("emr", "emr_record", "record_id", "visit_date", "record_id visit_id dept_code visit_date disease_code", "visit_id patient_key", "visit_id visit_date", "dept_code disease_code"),
    "dwd_lis_report_detail": ("lis", "lis_report", "report_id", "report_date", "report_id visit_id report_date", "visit_id patient_key", "visit_id report_date", ""),
}
_TYPES = ("NULL", "DUPLICATE", "VALUE_RANGE", "VOLUME_WAVE", "RECONCILE")
_CATALOG = [(t, k) for t in _SPECS for k in _TYPES] + [(t, "MAPPING") for t, s in _SPECS.items() if s[7]] + [(t, "PRIVACY") for t in _SPECS]
_CODES = "OK OPTIONAL_MISSING SOURCE_INVALID_ID SOURCE_CONFLICTING_ID SOURCE_INVALID_PRIMARY_DATE DUPLICATE_REMOVED INVALID_VALUE REFUND_POLICY_UNCONFIRMED STAY_DAYS_POLICY_UNCONFIRMED UNMAPPED_CODE PRIVACY_VIOLATION RECONCILE_MISMATCH NO_BASELINE NO_COMPARABLE_PAIR ZERO_BASELINE VOLUME_WAVE STALE RULE_ERROR NOT_RUN".split()
_ERRORS = set("DQ_READ_RETRYABLE DQ_ARGUMENTS_INVALID DQ_CONFIG_INVALID DQ_SNAPSHOT_MISMATCH DQ_STALE DQ_RULE_ERROR DQ_INTERNAL_ERROR DQ_REPORT_PATH_INVALID DQ_REPORT_IDENTITY_CONFLICT DQ_REPORT_WRITE_FAILED DQ_GATE_FAILED DQ_INSUFFICIENT_EVIDENCE DWD_SCHEMA_INVALID DWD_SOURCE_MISMATCH DWD_PRIVACY_KEY_MISSING DWD_PRIVACY_KEY_INVALID DWD_PRIVACY_KEY_CHANGED ODS_DATABASE_URL_MISSING ODS_DATABASE_URL_INVALID".split())
_REASONS = ("invalid_id_rows", "conflicting_id_rows", "invalid_primary_date_rows")
_SUPPRESSED = ("patient_name", "id_card", "phone")
_AMOUNTS = {"fee_amount", "total_fee", "drug_fee", "consumable_fee", "amount"}
_BUSINESS_ID_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAPPING = {
    "dept_code": {**{f"SIM_DEPT_{n:02}": f"SIM_DEPT_{n:02}" for n in range(1, 5)}, **{f"SIM_科室{n}_别名": f"SIM_DEPT_{n:02}" for n in range(1, 5)}},
    "visit_type": {s: s for s in ("OUTPATIENT", "INPATIENT")},
    "gender": {s: s for s in ("M", "F")},
    "disease_code": {f"SIM_DX_{n:02}": f"SIM_DX_{n:02}" for n in range(1, 6)},
    "fee_type_code": {s: s for s in ("SERVICE", "DRUG", "CONSUMABLE")},
}


def _fail(code):
    raise OdsError(code) from None


class _ReportArgumentError(OdsError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("DQ_ARGUMENTS_INVALID", detail=reason)


def _safe(function):
    @wraps(function)
    def boundary(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _ReportArgumentError as error:
            raise error from None
        except OdsError as error:
            _fail(error.code if error.code in _ERRORS else "DQ_INTERNAL_ERROR")
        except Exception:
            _fail("DQ_INTERNAL_ERROR")
    return boundary


def _id(value, size=128):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0," + str(size - 1) + "}", value))


def _business_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip()
            and _BUSINESS_ID_CONTROL.search(value) is None)


def _day(value):
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def _count(value):
    return type(value) is int and value >= 0


def _load_snapshot(batch_date=None, *, dataset_id=None):
    from etl.jobs.clean_dwd import load_quality_snapshot
    return load_quality_snapshot(batch_date, dataset_id=dataset_id)


def _digest(kind, columns, rows):
    from etl.jobs.clean_dwd import quality_digest
    return quality_digest(kind, columns, rows)


def _load_config():
    try:
        if _CONFIG.is_symlink():
            _fail("DQ_CONFIG_INVALID")
        with _CONFIG.open(encoding="utf-8") as stream:
            config = json.load(stream)
        tables = {table: dict(short=spec[0], source=spec[1], id_column=spec[2], date_column=spec[3],
                              columns=_COMMON + spec[4].split(), mapping_columns=spec[7].split())
                  for table, spec in _SPECS.items()}
        rules = [dict(rule_id=_SPECS[table][0] + "." + kind, table=table, type=kind) for table, kind in _CATALOG]
        if (not isinstance(config, dict) or set(config) != {"rules_version", "mapping_version", "approved_complete_intervals", "tables", "mappings", "rules"}
                or not _id(config["rules_version"], 64) or config["mapping_version"] != _MAPPING_VERSION
                or config["tables"] != tables or config["mappings"] != _MAPPING or config["rules"] != rules):
            _fail("DQ_CONFIG_INVALID")
        intervals = config["approved_complete_intervals"]
        if not isinstance(intervals, list) or intervals and config["rules_version"] == _RULES_VERSION:
            _fail("DQ_CONFIG_INVALID")
        seen = set()
        for item in intervals:
            if not isinstance(item, dict) or set(item) != {"dataset_id", "table", "start_date", "end_date", "basis_id"}:
                _fail("DQ_CONFIG_INVALID")
            start, end = _day(item["start_date"]), _day(item["end_date"])
            pair = (item["dataset_id"], item["table"])
            if (not _id(item["dataset_id"]) or item["table"] not in _SPECS or not _id(item["basis_id"], 64)
                    or not isinstance(item["start_date"], str) or not isinstance(item["end_date"], str)
                    or start is None or end is None or start > end or pair in seen):
                _fail("DQ_CONFIG_INVALID")
            seen.add(pair)
        return {k: config[k] for k in ("rules_version", "mapping_version", "approved_complete_intervals")}
    except OdsError:
        raise
    except Exception:
        _fail("DQ_CONFIG_INVALID")


def _stats_valid(stats, table):
    spec = _SPECS[table]
    fields = set(spec[4].split()) - set(_SUPPRESSED)
    privacy = {c for c in ("patient_id", *_SUPPRESSED) if c in SOURCES[spec[1]]["columns"]}
    if not isinstance(stats, dict) or set(stats) != {"quarantined_by_reason", "field_counts", "mapping_counts", "privacy_counts", "filled_rows", "filled_cells"}:
        _fail("DQ_CONFIG_INVALID")
    for name, keys, leaves in (("quarantined_by_reason", set(_REASONS), None),
                               ("field_counts", fields, {"missing", "invalid", "filled", "negative"}),
                               ("mapping_counts", set(spec[7].split()), {"slots", "matched", "unmapped"}),
                               ("privacy_counts", privacy, {"nonempty", "processed", "missing"})):
        group = stats[name]
        if not isinstance(group, dict) or set(group) != keys:
            _fail("DQ_CONFIG_INVALID")
        for value in group.values():
            if leaves is None:
                valid = _count(value)
            else:
                valid = isinstance(value, dict) and set(value) == leaves and all(_count(v) for v in value.values())
            if not valid:
                _fail("DQ_CONFIG_INVALID")
    if not all(_count(stats[k]) for k in ("filled_rows", "filled_cells")):
        _fail("DQ_CONFIG_INVALID")


def _validate_identity(snapshot, batch_date=None, dataset_id=None, *, rules_version=None):
    top = {"schema_version", "dataset_id", "clean_run_id", "batch_date", "rules_version", "mapping_version", "privacy_key_id", "tables"}
    if not isinstance(snapshot, dict) or set(snapshot) != top:
        _fail("DQ_SNAPSHOT_MISMATCH")
    if (snapshot["schema_version"] != "node07-snapshot-v1" or not _id(snapshot["rules_version"], 64)
            or snapshot["mapping_version"] != course_mapping.version(snapshot["dataset_id"]) or not _id(snapshot["dataset_id"])
            or not _id(snapshot["privacy_key_id"], 64) or _day(snapshot["batch_date"]) is None
            ):
        _fail("DQ_SNAPSHOT_MISMATCH")
    if rules_version is not None and snapshot["rules_version"] != rules_version:
        _fail("DQ_SNAPSHOT_MISMATCH")
    try:
        if str(UUID(snapshot["clean_run_id"])) != snapshot["clean_run_id"]:
            _fail("DQ_SNAPSHOT_MISMATCH")
    except (ValueError, TypeError, AttributeError):
        _fail("DQ_SNAPSHOT_MISMATCH")
    if (batch_date is not None and _day(batch_date) != _day(snapshot["batch_date"])) or (dataset_id is not None and dataset_id != snapshot["dataset_id"]):
        _fail("DQ_SNAPSHOT_MISMATCH")


def _validate_snapshot(snapshot, batch_date=None, dataset_id=None, *, rules_version=None):
    _validate_identity(snapshot, batch_date, dataset_id, rules_version=rules_version)
    if not isinstance(snapshot["tables"], dict) or set(snapshot["tables"]) != set(_SPECS):
        _fail("DQ_SNAPSHOT_MISMATCH")
    snapshot_day = _day(snapshot["batch_date"])
    for table, spec in _SPECS.items():
        entry = snapshot["tables"][table]
        if not isinstance(entry, dict) or set(entry) != {"source", "dwd", "audit"}:
            _fail("DQ_SNAPSHOT_MISMATCH")
        source_cols = list(SOURCES[spec[1]]["columns"]) + ["dataset_id", "source_row_number", "etl_batch_date"]
        for kind, expected_table, columns in (("source", SOURCES[spec[1]]["table"], source_cols), ("dwd", table, _COMMON + spec[4].split())):
            view = entry[kind]
            if (not isinstance(view, dict) or set(view) != {"table", "columns", "rows"} or view["table"] != expected_table
                    or not isinstance(view["columns"], (list, tuple)) or not isinstance(view["rows"], list)):
                _fail("DQ_SNAPSHOT_MISMATCH")
            actual = list(view["columns"])
            # Extra DWD columns are counted by PRIVACY, never copied into details.
            if kind == "source" and actual != columns or kind == "dwd" and actual[:len(columns)] != columns:
                _fail("DQ_SNAPSHOT_MISMATCH")
            column_set = set(actual)
            if len(actual) != len(column_set) or not all(isinstance(c, str) for c in actual):
                _fail("DQ_SNAPSHOT_MISMATCH")
            numbers = set()
            for row in view["rows"]:
                if not isinstance(row, dict) or set(row) != column_set or row["dataset_id"] != snapshot["dataset_id"]:
                    _fail("DQ_SNAPSHOT_MISMATCH")
                number = row["source_row_number"]
                if not _count(number) or number == 0:
                    _fail("DQ_SNAPSHOT_MISMATCH")
                if kind == "source":
                    if number in numbers or any(not isinstance(row[c], str) for c in SOURCES[spec[1]]["columns"]):
                        _fail("DQ_SNAPSHOT_MISMATCH")
                    numbers.add(number)
                    if _day(row["etl_batch_date"]) is None:
                        _fail("DQ_SNAPSHOT_MISMATCH")
                elif (_day(row["etl_batch_date"]) != snapshot_day or _day(row["source_batch_date"]) is None
                      or not isinstance(row["etl_load_time"], datetime)):
                    _fail("DQ_SNAPSHOT_MISMATCH")
        audit = entry["audit"]
        audit_keys = set("dataset_id table_name clean_run_id batch_date rules_version mapping_version privacy_key_id source_rows accepted_rows duplicate_removed quarantined_rows source_sha256 result_sha256 stats_json updated_at".split())
        if not isinstance(audit, dict) or set(audit) != audit_keys or audit["table_name"] != table:
            _fail("DQ_SNAPSHOT_MISMATCH")
        for field in ("dataset_id", "clean_run_id", "rules_version", "mapping_version", "privacy_key_id"):
            if audit[field] != snapshot[field]:
                _fail("DQ_SNAPSHOT_MISMATCH")
        if _day(audit["batch_date"]) != snapshot_day or not isinstance(audit["updated_at"], datetime):
            _fail("DQ_SNAPSHOT_MISMATCH")
        if any(not _count(audit[k]) for k in ("source_rows", "accepted_rows", "duplicate_removed", "quarantined_rows")):
            _fail("DQ_CONFIG_INVALID")
        if any(not isinstance(audit[k], str) or not re.fullmatch(r"[0-9a-f]{64}", audit[k]) for k in ("source_sha256", "result_sha256")):
            _fail("DQ_SNAPSHOT_MISMATCH")
        _stats_valid(audit["stats_json"], table)
    # ODS can contain several extraction batch dates after a range update; the
    # frozen registry batch is validated by C7, not inferred from row metadata.
    return snapshot


def _context(table, dataset_id, snapshot):
    if not isinstance(table, str) or table not in _SPECS or dataset_id is not None and not _id(dataset_id):
        _fail("DQ_ARGUMENTS_INVALID")
    if snapshot is None:
        snapshot = _load_snapshot(dataset_id=dataset_id)
    _validate_snapshot(snapshot, dataset_id=dataset_id)
    return snapshot["tables"][table], snapshot


def _columns(actual, expected):
    if not isinstance(actual, (list, tuple)) or list(actual) != list(expected):
        _fail("DQ_ARGUMENTS_INVALID")


def _empty_details(table, kind):
    """None means not calculated; ERROR/NOT_RUN preserve the frozen field shape."""
    spec = _SPECS[table]
    if kind == "NULL":
        return dict(source_invalid_id_rows=None,
                    required_fields={c: dict(null_rows=None, total_rows=None) for c in (spec[2], spec[3])},
                    optional_fields={c: dict(missing_rows=None, total_rows=None) for c in spec[5].split()})
    if kind == "DUPLICATE":
        return dict(dwd_duplicate_rows=None, source_duplicate_removed=None, source_conflicting_id_rows=None)
    if kind == "VALUE_RANGE":
        return dict(source_invalid_primary_date_rows=None,
                    fields={c: dict(invalid_source_rows=None, invalid_dwd_rows=None, negative_amount_rows=None) for c in spec[6].split()},
                    stay_days_mismatch_rows=None)
    if kind == "VOLUME_WAVE":
        return dict.fromkeys(("candidate_pairs", "evaluated_pairs", "unevaluated_pairs", "positive_baseline_pairs",
                              "zero_to_positive_pairs", "zero_to_zero_pairs", "alert_pairs", "left_boundary_unobserved",
                              "coverage_basis", "coverage_evidence_id", "coverage_start_date", "coverage_end_date"))
    if kind == "RECONCILE":
        return dict.fromkeys(("source_rows", "audit_source_rows", "dwd_rows", "audit_accepted_rows", "duplicate_removed",
                              "quarantined_rows", "conservation_delta", "row_count_delta", "source_digest_match", "result_digest_match", "metadata_match"))
    if kind == "MAPPING":
        return dict(fields={c: dict(slots=None, matched=None, unmapped=None, match_ratio=None) for c in spec[7].split()})
    if kind == "PRIVACY":
        columns = SOURCES[spec[1]]["columns"]
        return dict(forbidden_column_count=None, suppression_violation_cells=None, patient_key_format_violations=None,
                    hmac_mismatch_rows=None, fields={c: dict(nonempty=None, processed=None, missing=None)
                    for c in ("patient_id", *_SUPPRESSED) if c in columns})
    _fail("DQ_CONFIG_INVALID")


def _result(table, kind, details, causes=(), *, applicability="APPLICABLE", numerator=None, denominator=None, execution="COMPLETED"):
    if execution != "COMPLETED":
        details = _empty_details(table, kind)
        numerator = denominator = None
    reasons = dict.fromkeys(_CODES, 0)
    for code, count, status, hard in causes:
        reasons[code] += count
    rank = {"PASS": 0, "NOT_EVALUATED": 1, "WARN": 2, "FAIL": 3}
    chosen = max(causes, key=lambda c: rank[c[2]]) if causes else ("OK", 0, "PASS", False)
    status = chosen[2] if execution == "COMPLETED" else "NOT_EVALUATED"
    return dict(rule_id=_SPECS[table][0] + "." + kind, table=table, execution_status=execution, status=status,
                code=chosen[0], severity="ERROR" if status == "FAIL" or execution != "COMPLETED" else "WARNING" if status in {"WARN", "NOT_EVALUATED"} else "INFO",
                hard_block=any(c[3] for c in causes), applicability=applicability,
                numerator=numerator, denominator=denominator, details={"reason_counts": reasons, **details})


def _candidates(entry, table):
    spec = _SPECS[table]; groups = {}; losses = dict.fromkeys(_REASONS, 0); duplicate = 0
    for row in entry["source"]["rows"]:
        key = row[spec[2]]
        if not _business_id(key):
            losses["invalid_id_rows"] += 1
        else:
            groups.setdefault(key, []).append(row)
    kept = []
    columns = SOURCES[spec[1]]["columns"]
    for rows in groups.values():
        row = rows[0]
        if len(rows) > 1:
            if any(any(other[c] != row[c] for c in columns) for other in rows[1:]):
                losses["conflicting_id_rows"] += len(rows)
                continue
            duplicate += len(rows) - 1
            row = min(rows, key=lambda r: r["source_row_number"])
        if _day(row[spec[3]]) is None:
            losses["invalid_primary_date_rows"] += 1
        else:
            kept.append(row)
    return kept, losses, duplicate


def _converted(raw, column, row, main_date):
    if _blank(raw):
        return None, "missing"
    if column.endswith("_date"):
        value = _day(raw)
        if value is None or column == "discharge_date" and value < _day(row[main_date]):
            return None, "invalid"
    elif column == "age":
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
            return None, "invalid"
        try:
            value = int(raw)
        except ValueError:
            return None, "invalid"
        if not 0 <= value <= 130:
            return None, "invalid"
    elif column in _AMOUNTS or column == "stay_days":
        if not isinstance(raw, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]{1,2})?", raw):
            return None, "invalid"
        try:
            value = Decimal(raw)
            limit = Decimal("99999999.99") if column == "stay_days" else Decimal("9999999999999999.99")
            if not value.is_finite() or abs(value) > limit or column == "stay_days" and value < 0:
                return None, "invalid"
        except InvalidOperation:
            return None, "invalid"
    elif column == "visit_id":
        if not _business_id(raw):
            return None, "invalid"
        value = raw
    else:
        value = raw
    return value, "negative" if column in _AMOUNTS and value < 0 else None


@_safe
def check_null_ratio(table, columns, *, dataset_id=None, snapshot=None):
    entry, _ = _context(table, dataset_id, snapshot); spec = _SPECS[table]
    _columns(columns, (spec[2], spec[3], *spec[5].split()))
    return _null_ratio(table, entry)


@_safe
def _null_ratio(table, entry):
    spec = _SPECS[table]
    rows = entry["dwd"]["rows"]; stats = entry["audit"]["stats_json"]
    required = {c: dict(null_rows=sum(_blank(r[c]) for r in rows), total_rows=len(rows)) for c in (spec[2], spec[3])}
    optional = {}
    for col in spec[5].split():
        missing = stats["privacy_counts"]["patient_id"]["missing"] if col == "patient_key" else stats["field_counts"][col]["missing"]
        optional[col] = dict(missing_rows=missing, total_rows=len(rows))
    loss = stats["quarantined_by_reason"]["invalid_id_rows"]
    causes = []
    if loss: causes.append(("SOURCE_INVALID_ID", loss, "FAIL", True))
    count = sum(v["null_rows"] for v in required.values())
    if count: causes.append(("INVALID_VALUE", count, "FAIL", True))
    missing = sum(v["missing_rows"] for v in optional.values())
    if missing: causes.append(("OPTIONAL_MISSING", missing, "WARN", False))
    return _result(table, "NULL", dict(source_invalid_id_rows=loss, required_fields=required, optional_fields=optional), causes)


@_safe
def check_duplicate(table, keys, *, dataset_id=None, snapshot=None):
    entry, _ = _context(table, dataset_id, snapshot); spec = _SPECS[table]
    _columns(keys, ("dataset_id", spec[2]))
    return _duplicate(table, entry)


@_safe
def _duplicate(table, entry):
    spec = _SPECS[table]
    counts = Counter((r["dataset_id"], r[spec[2]]) for r in entry["dwd"]["rows"])
    duplicates = sum(n - 1 for n in counts.values())
    audit = entry["audit"]; conflicts = audit["stats_json"]["quarantined_by_reason"]["conflicting_id_rows"]
    causes = []
    if conflicts: causes.append(("SOURCE_CONFLICTING_ID", conflicts, "FAIL", True))
    if duplicates: causes.append(("INVALID_VALUE", duplicates, "FAIL", True))
    if audit["duplicate_removed"]: causes.append(("DUPLICATE_REMOVED", audit["duplicate_removed"], "WARN", False))
    return _result(table, "DUPLICATE", dict(dwd_duplicate_rows=duplicates, source_duplicate_removed=audit["duplicate_removed"], source_conflicting_id_rows=conflicts), causes)


@_safe
def check_value_range(table, rules, *, dataset_id=None, snapshot=None):
    entry, _ = _context(table, dataset_id, snapshot)
    _columns(rules, _SPECS[table][6].split())
    return _value_range(table, entry)


@_safe
def _value_range(table, entry):
    spec = _SPECS[table]
    stats = entry["audit"]["stats_json"]; fields = {}; causes = []; mismatch = 0
    loss = stats["quarantined_by_reason"]["invalid_primary_date_rows"]
    if loss: causes.append(("SOURCE_INVALID_PRIMARY_DATE", loss, "FAIL", True))
    for col in spec[6].split():
        invalid = 0; negative = 0
        for row in entry["dwd"]["rows"]:
            v = row[col]
            if v is None:
                invalid += int(col == spec[3])
                continue
            if col.endswith("_date"):
                good = type(v) is date and (col != "discharge_date" or _day(row[spec[3]]) is not None and v >= _day(row[spec[3]]))
            elif col == "age": good = type(v) is int and 0 <= v <= 130
            elif col == "visit_id": good = _business_id(v)
            else:
                limit = Decimal("99999999.99") if col == "stay_days" else Decimal("9999999999999999.99")
                good = isinstance(v, Decimal) and v.is_finite() and abs(v) <= limit and v.as_tuple().exponent >= -2 and (col != "stay_days" or v >= 0)
                if good and col in _AMOUNTS and v < 0: negative += 1
            invalid += int(not good)
        src_invalid = stats["field_counts"][col]["invalid"]
        fields[col] = dict(invalid_source_rows=src_invalid, invalid_dwd_rows=invalid, negative_amount_rows=negative)
        if src_invalid + invalid: causes.append(("INVALID_VALUE", src_invalid + invalid, "FAIL", True))
        if negative: causes.append(("REFUND_POLICY_UNCONFIRMED", negative, "WARN", False))
    if spec[0] == "inpatient":
        for row in entry["dwd"]["rows"]:
            start, end, stay = _day(row["admit_date"]), _day(row["discharge_date"]), row["stay_days"]
            if start and end and isinstance(stay, Decimal) and stay.is_finite() and stay != Decimal((end - start).days): mismatch += 1
        if mismatch: causes.append(("STAY_DAYS_POLICY_UNCONFIRMED", mismatch, "WARN", False))
    return _result(table, "VALUE_RANGE", dict(source_invalid_primary_date_rows=loss, fields=fields, stay_days_mismatch_rows=mismatch), causes)


@_safe
def check_volume_wave(table, threshold=0.3, *, dataset_id=None, snapshot=None):
    if type(threshold) not in (float, int, Decimal) or threshold != 0.3:
        _fail("DQ_ARGUMENTS_INVALID")
    entry, snapshot = _context(table, dataset_id, snapshot); spec = _SPECS[table]
    config = _load_config()
    _validate_identity(snapshot, rules_version=config["rules_version"])
    return _volume_wave(table, entry, snapshot, config)


def _volume_wave(table, entry, snapshot, config):
    spec = _SPECS[table]
    counts = Counter(_day(r[spec[3]]) for r in entry["dwd"]["rows"])
    counts.pop(None, None)
    evidence = next((i for i in config["approved_complete_intervals"] if i["dataset_id"] == snapshot["dataset_id"] and i["table"] == table), None)
    details = dict(candidate_pairs=0, evaluated_pairs=0, unevaluated_pairs=0, positive_baseline_pairs=0,
                   zero_to_positive_pairs=0, zero_to_zero_pairs=0, alert_pairs=0, left_boundary_unobserved=1,
                   coverage_basis="observed_days_only", coverage_evidence_id=None, coverage_start_date=None, coverage_end_date=None)
    start, end = (min(counts), max(counts)) if counts else (None, None)
    approved_start, approved_end = None, None
    if evidence:
        approved_start, approved_end = _day(evidence["start_date"]), _day(evidence["end_date"])
        start = min(start, approved_start) if start else approved_start
        end = max(end, approved_end) if end else approved_end
        details.update(coverage_basis="approved_complete_interval", coverage_evidence_id=evidence["basis_id"],
                       coverage_start_date=evidence["start_date"], coverage_end_date=evidence["end_date"], left_boundary_unobserved=int(start < approved_start))
    if start is not None:
        details["candidate_pairs"] = (end - start).days
        current = start
        while current < end:
            following = current + timedelta(days=1)
            a = counts.get(current); b = counts.get(following)
            if a is None and approved_start and approved_start <= current <= approved_end: a = 0
            if b is None and approved_start and approved_start <= following <= approved_end: b = 0
            if a is None or b is None:
                details["unevaluated_pairs"] += 1
            else:
                details["evaluated_pairs"] += 1
                if a > 0:
                    details["positive_baseline_pairs"] += 1
                    # Integer comparison preserves exact +/-30% boundaries.
                    details["alert_pairs"] += int(10 * abs(b - a) > 3 * a)
                elif b > 0:
                    details["zero_to_positive_pairs"] += 1; details["alert_pairs"] += 1
                else:
                    details["zero_to_zero_pairs"] += 1
            current = following
    causes = []; applicability = "APPLICABLE"
    if details["evaluated_pairs"] == 0:
        code = "NO_BASELINE" if details["candidate_pairs"] == 0 else "NO_COMPARABLE_PAIR"
        causes.append((code, 1, "NOT_EVALUATED", False)); applicability = code
    else:
        if details["zero_to_positive_pairs"]: causes.append(("ZERO_BASELINE", details["zero_to_positive_pairs"], "WARN", False))
        alerts = details["alert_pairs"] - details["zero_to_positive_pairs"]
        if alerts: causes.append(("VOLUME_WAVE", alerts, "WARN", False))
    return _result(table, "VOLUME_WAVE", details, causes, applicability=applicability, numerator=details["alert_pairs"], denominator=details["evaluated_pairs"])


def _audit_semantics(entry, table):
    """Check aggregate claims against accepted source representatives in memory."""
    spec = _SPECS[table]; audit = entry["audit"]; stats = audit["stats_json"]
    candidates, losses, duplicates = _candidates(entry, table)
    if (losses != stats["quarantined_by_reason"] or duplicates != audit["duplicate_removed"]
            or len(candidates) != audit["accepted_rows"] or sum(losses.values()) != audit["quarantined_rows"]):
        return False
    source_by_number = {r["source_row_number"]: r for r in candidates}
    if Counter(source_by_number.keys()) != Counter(r["source_row_number"] for r in entry["dwd"]["rows"]):
        return False
    for row in entry["dwd"]["rows"]:
        src = source_by_number[row["source_row_number"]]
        if row["source_batch_date"] != src["etl_batch_date"] or row[spec[2]] != src[spec[2]]:
            return False
    for col, recorded in stats["field_counts"].items():
        if col == spec[2]:
            expected = Counter()
        elif col in spec[7].split():
            expected = Counter(missing=sum(_blank(r[col]) for r in candidates))
        else:
            expected = Counter(_converted(r[col], col, r, spec[3])[1] for r in candidates)
        if any(recorded[key] != expected[key] for key in ("missing", "invalid", "negative")) or recorded["filled"] != 0:
            return False
    if stats["filled_rows"] != 0 or stats["filled_cells"] != 0:
        return False
    mappings = course_mapping.source_mappings(audit["dataset_id"], spec[1], _MAPPING)
    for col in spec[7].split():
        matched = sum(r[col] in mappings[col] for r in candidates)
        if stats["mapping_counts"][col] != dict(slots=len(candidates), matched=matched, unmapped=len(candidates) - matched): return False
    for col, counts in stats["privacy_counts"].items():
        nonempty = sum(not _blank(r[col]) for r in candidates)
        if counts != dict(nonempty=nonempty, processed=nonempty, missing=len(candidates) - nonempty): return False
    return True


@_safe
def check_reconcile(ods_table, dwd_table, clean_log, *, dataset_id=None, snapshot=None):
    entry, _ = _context(dwd_table, dataset_id, snapshot)
    if ods_table != entry["source"]["table"]:
        _fail("DQ_ARGUMENTS_INVALID")
    if clean_log != entry["audit"]:
        _fail("DQ_SNAPSHOT_MISMATCH")
    return _reconcile(dwd_table, entry)


@_safe
def _reconcile(dwd_table, entry):
    audit = entry["audit"]; source_rows = len(entry["source"]["rows"]); dwd_rows = len(entry["dwd"]["rows"])
    source_match = _digest("source", entry["source"]["columns"], entry["source"]["rows"]) == audit["source_sha256"]
    result_match = _digest("dwd", entry["dwd"]["columns"], entry["dwd"]["rows"]) == audit["result_sha256"]
    conservation = audit["source_rows"] - audit["accepted_rows"] - audit["duplicate_removed"] - audit["quarantined_rows"]
    delta = dwd_rows - audit["accepted_rows"]
    metadata = _audit_semantics(entry, dwd_table)
    details = dict(source_rows=source_rows, audit_source_rows=audit["source_rows"], dwd_rows=dwd_rows,
                   audit_accepted_rows=audit["accepted_rows"], duplicate_removed=audit["duplicate_removed"], quarantined_rows=audit["quarantined_rows"],
                   conservation_delta=conservation, row_count_delta=delta, source_digest_match=source_match, result_digest_match=result_match, metadata_match=metadata)
    causes = []
    if not source_match or not result_match: causes.append(("STALE", int(not source_match) + int(not result_match), "FAIL", True))
    if source_rows != audit["source_rows"] or conservation != 0 or delta != 0 or not metadata:
        causes.append(("RECONCILE_MISMATCH", 1, "FAIL", True))
    return _result(dwd_table, "RECONCILE", details, causes)


def _check_mapping(table, snapshot):
    entry = snapshot["tables"][table]; spec = _SPECS[table]; fields = {}; causes = []
    candidates, _, _ = _candidates(entry, table)
    sources = {r["source_row_number"]: r for r in candidates}
    mappings = course_mapping.source_mappings(snapshot["dataset_id"], spec[1], _MAPPING)
    for col in spec[7].split():
        slots = len(candidates); matched = 0
        for row in entry["dwd"]["rows"]:
            source = sources.get(row["source_row_number"])
            if source is not None and source[col] in mappings[col] and row[col] == mappings[col][source[col]]:
                matched += 1
        matched = min(matched, slots)
        fields[col] = dict(slots=slots, matched=matched, unmapped=slots - matched, match_ratio=matched / slots if slots else None)
        if matched != slots or slots == 0 and entry["dwd"]["rows"]: causes.append(("UNMAPPED_CODE", max(slots - matched, 1), "FAIL", True))
    return _result(table, "MAPPING", dict(fields=fields), causes)


def _read_course_privacy_key():
    path = Path.home() / ".medical-ops-analytics-course.key"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                _fail("DWD_PRIVACY_KEY_INVALID")
            raw = stream.read(129)
        if len(raw) > 128:
            _fail("DWD_PRIVACY_KEY_INVALID")
        return raw.strip()
    except FileNotFoundError:
        _fail("DWD_PRIVACY_KEY_MISSING")
    except OSError:
        _fail("DWD_PRIVACY_KEY_INVALID")


def _privacy_key(snapshot, *, env=None):
    raw = os.environ.get("DWD_PRIVACY_KEY")
    key_id = os.environ.get("DWD_PRIVACY_KEY_ID")
    if (raw is None and key_id is None and env == "dev" and snapshot["dataset_id"] == COURSE_DATASET
            and snapshot["privacy_key_id"] == _COURSE_KEY_ID):
        raw = _read_course_privacy_key()
        key_id = _COURSE_KEY_ID
    if not raw or not key_id:
        _fail("DWD_PRIVACY_KEY_MISSING")
    try:
        key = base64.b64decode(raw, validate=True)
        if len(key) != 32 or not _id(key_id, 64):
            _fail("DWD_PRIVACY_KEY_INVALID")
    except (ValueError, TypeError):
        _fail("DWD_PRIVACY_KEY_INVALID")
    if key_id != snapshot["privacy_key_id"]:
        _fail("DQ_SNAPSHOT_MISMATCH")
    return key


def _check_privacy(table, snapshot, *, env=None):
    entry = snapshot["tables"][table]; spec = _SPECS[table]; key = _privacy_key(snapshot, env=env)
    forbidden = len(set(entry["dwd"]["columns"]) - set(_COMMON + spec[4].split()))
    suppression = 0; shape = 0; mismatch = 0
    sources = {r["source_row_number"]: r for r in entry["source"]["rows"]}
    for row in entry["dwd"]["rows"]:
        source = sources.get(row["source_row_number"])
        for col in _SUPPRESSED:
            if col in row:
                expected = None if source is not None and _blank(source[col]) else "[REDACTED]"
                suppression += int(row[col] != expected)
        patient = row["patient_key"]
        if patient is not None and (not isinstance(patient, str) or not re.fullmatch(r"[0-9a-f]{64}", patient)): shape += 1
        if source is None:
            mismatch += 1; continue
        if _blank(source["patient_id"]):
            expected = None
        else:
            message = json.dumps(["node07-patient-v1", snapshot["dataset_id"], source["patient_id"]], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            expected = hmac.new(key, message, digestmod="sha256").hexdigest()
        mismatch += int(patient != expected)
    fields = {col: dict(counts) for col, counts in entry["audit"]["stats_json"]["privacy_counts"].items()}
    stats_bad = sum(int(v["nonempty"] != v["processed"]) for v in fields.values())
    total = forbidden + suppression + shape + mismatch + stats_bad
    causes = [("PRIVACY_VIOLATION", total, "FAIL", True)] if total else []
    return _result(table, "PRIVACY", dict(forbidden_column_count=forbidden, suppression_violation_cells=suppression,
                                         patient_key_format_violations=shape, hmac_mismatch_rows=mismatch, fields=fields), causes)


def _summary(snapshot, rules, env, *, include_tables=True, schema_version=REPORT_VERSION):
    counts = Counter(r["status"] for r in rules)
    p, w, f, n = (counts[s] for s in ("PASS", "WARN", "FAIL", "NOT_EVALUATED"))
    executed = sum(r["execution_status"] == "COMPLETED" for r in rules)
    points = 2 * p + w
    if schema_version == REPORT_VERSION:
        points += sum(r["status"] == "WARN" and r["code"] == "VOLUME_WAVE"
                      and r["rule_id"].endswith(".VOLUME_WAVE") for r in rules)
    empty = not include_tables or any(snapshot["tables"][t]["audit"]["accepted_rows"] == 0 for t in _SPECS)
    if empty or any(r["hard_block"] or r["execution_status"] != "COMPLETED" for r in rules) or 100 * executed < 95 * 34:
        gate = "FAIL"
    elif 100 * points >= 90 * 68:
        gate = "PASS"
    elif n > 0 and 100 * (points + 2 * n) >= 90 * 68:
        gate = "BLOCKED_INSUFFICIENT_EVIDENCE"
    else:
        gate = "FAIL"
    tables = {}
    for table in _SPECS if include_tables else ():
        audit = snapshot["tables"][table]["audit"]
        item = {k: audit[k] for k in ("source_rows", "accepted_rows", "duplicate_removed", "quarantined_rows")}
        for name, key in (("source_usable_ratio", "accepted_rows"), ("quarantine_ratio", "quarantined_rows"), ("duplicate_ratio", "duplicate_removed")):
            item[name] = audit[key] / audit["source_rows"] if audit["source_rows"] else None
        # stats shape and leaf types have already passed strict validation.
        item["stats_json"] = json.loads(json.dumps(audit["stats_json"], allow_nan=False))
        tables[table] = item
    alerts = []
    for rule in rules:
        for code, value in rule["details"]["reason_counts"].items():
            if code != "OK" and value:
                alerts.append(dict(rule_id=rule["rule_id"], code=code, severity=rule["severity"]))
    return dict(schema_version=schema_version, env=env, dataset_id=snapshot["dataset_id"], clean_run_id=snapshot["clean_run_id"],
                batch_date=_day(snapshot["batch_date"]).isoformat(), rules_version=snapshot["rules_version"], mapping_version=snapshot["mapping_version"],
                privacy_key_id=snapshot["privacy_key_id"], generated_at=datetime.now(timezone.utc).isoformat(), planned_rules=34,
                executed_rules=executed, evaluated_rules=p + w + f, coverage=100 * executed / 34, data_evaluability=100 * (p + w + f) / 34,
                score_numerator_half_points=points, score_denominator_half_points=68, score=100 * points / 68, score_upper_bound=100 * (points + 2 * n) / 68,
                rule_status_counts={s: counts[s] for s in ("PASS", "WARN", "FAIL", "NOT_EVALUATED")}, gate_status=gate, rules=rules, alerts=alerts, tables=tables)


def _directory():
    """Open each path component without following symlinks; create log dirs only."""
    root = Path(_LOG_ROOT)
    if not root.is_absolute() or ".." in root.parts:
        _fail("DQ_REPORT_PATH_INVALID")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(root.parts[1:], 1):
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                # Only the fixed logs root may be missing, not project ancestors.
                if index != len(root.parts) - 1:
                    _fail("DQ_REPORT_PATH_INVALID")
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        _fail("DQ_REPORT_PATH_INVALID")


def _write_report(report):
    directory = None; root = None; temporary = None
    try:
        root = _directory()
        env = report["env"]
        try:
            os.mkdir(env, mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        try:
            directory = os.open(env, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError:
            _fail("DQ_REPORT_PATH_INVALID")
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = "quality_report_" + report["batch_date"].replace("-", "") + ".json"
        old_stat = None
        try:
            old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            old = None
        except OSError:
            _fail("DQ_REPORT_PATH_INVALID")
        if old is not None:
            with os.fdopen(old, "r", encoding="utf-8") as stream:
                old_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(old_stat.st_mode) or old_stat.st_nlink != 1:
                    _fail("DQ_REPORT_PATH_INVALID")
                try:
                    if old_stat.st_size > 2_000_000:
                        _fail("DQ_REPORT_IDENTITY_CONFLICT")
                    previous = json.load(stream)
                    identity = ("env", "dataset_id", "batch_date", "rules_version", "privacy_key_id")
                    if not isinstance(previous, dict) or any(previous.get(k) != report[k] for k in identity):
                        _fail("DQ_REPORT_IDENTITY_CONFLICT")
                    versions = (previous.get("schema_version"), report["schema_version"])
                    if versions[0] != versions[1] and versions != REPORT_VERSIONS:
                        _fail("DQ_REPORT_IDENTITY_CONFLICT")
                    if previous.get("mapping_version") != report["mapping_version"] and not (
                        report["dataset_id"] == COURSE_DATASET and previous.get("gate_status") == "FAIL"
                        and previous.get("mapping_version") == _MAPPING_VERSION
                        and report["mapping_version"] == course_mapping.COURSE_VERSION
                    ):
                        _fail("DQ_REPORT_IDENTITY_CONFLICT")
                    if str(UUID(previous.get("clean_run_id"))) != previous.get("clean_run_id"):
                        _fail("DQ_REPORT_IDENTITY_CONFLICT")
                    if (report["gate_status"] == "PASS" and stat.S_IMODE(old_stat.st_mode) == 0o600
                            and old_stat.st_uid == os.geteuid()):
                        timestamp = previous.get("generated_at")
                        try:
                            valid_time = isinstance(timestamp, str) and datetime.fromisoformat(timestamp).tzinfo is not None
                        except ValueError:
                            valid_time = False
                        if valid_time:
                            candidate = dict(report, generated_at=timestamp)
                            if json.dumps(candidate, sort_keys=True) == json.dumps(previous, sort_keys=True):
                                report["generated_at"] = timestamp
                                return
                except OdsError:
                    raise
                except Exception:
                    _fail("DQ_REPORT_IDENTITY_CONFLICT")
        temporary = ".quality-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(report, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
            temp_stat = os.fstat(stream.fileno())
        published = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(published.st_mode) or published.st_nlink != 1 or (published.st_dev, published.st_ino) != (temp_stat.st_dev, temp_stat.st_ino):
            _fail("DQ_REPORT_PATH_INVALID")
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (old_stat is None) != (current is None) or old_stat is not None and (current.st_dev, current.st_ino, current.st_nlink, current.st_mtime_ns, current.st_size) != (old_stat.st_dev, old_stat.st_ino, 1, old_stat.st_mtime_ns, old_stat.st_size):
            _fail("DQ_REPORT_IDENTITY_CONFLICT")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    except OdsError:
        raise
    except Exception:
        _fail("DQ_REPORT_WRITE_FAILED")
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)
        if root is not None: os.close(root)


def _report_batch_day(value):
    if isinstance(value, datetime):
        value = value.date()
    if type(value) is date:
        return value
    if value is None:
        raise _ReportArgumentError("BATCH_DATE_MISSING")
    if type(value) is int and 10000101 <= value <= 99991231:
        value = str(value)
    if not isinstance(value, str):
        raise _ReportArgumentError("BATCH_DATE_INVALID_TYPE")
    value = value.strip()
    if not value:
        raise _ReportArgumentError("BATCH_DATE_MISSING")
    if re.fullmatch(r"[0-9]{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if re.fullmatch(r"[0-9]{4}[-/.]?[0-9]{2}", value):
        raise _ReportArgumentError("BATCH_DATE_MISSING_DAY")
    if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:", value):
        stamp = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(stamp).date()
        except ValueError:
            raise _ReportArgumentError("BATCH_DATE_INVALID_TIMESTAMP") from None
    if re.fullmatch(r"[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日", value):
        value = value[:-1].replace("年", "-").replace("月", "-")
    numeric = re.fullmatch(r"([0-9]{4})([-/.])([0-9]{1,2})\2([0-9]{1,2})", value)
    if numeric:
        year, _, month, day = numeric.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            raise _ReportArgumentError("BATCH_DATE_INVALID") from None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]{0,127}", value):
        raise _ReportArgumentError("BATCH_DATE_LABEL_NOT_CALENDAR")
    raise _ReportArgumentError("BATCH_DATE_INVALID_TEXT")


@_safe
def generate_quality_report(batch_date, *, dataset_id=None, env="dev"):
    selector = batch_date.strip() if isinstance(batch_date, str) else None
    if selector in (COURSE_DATASET, "latest"):
        if selector == COURSE_DATASET:
            if dataset_id is not None and dataset_id != COURSE_DATASET:
                raise _ReportArgumentError("DATASET_SELECTOR_INVALID")
            dataset_id = COURSE_DATASET
        batch_date = None
    else:
        try:
            batch_date = _report_batch_day(batch_date).isoformat()
        except _ReportArgumentError as error:
            if (error.reason != "BATCH_DATE_LABEL_NOT_CALENDAR" or env != "dev"
                    or dataset_id not in (None, COURSE_DATASET)
                    or re.search(r"[0-9]{4}[-_. ]?[0-9]{1,2}[-_. ]?[0-9]{1,2}", selector)):
                raise
            dataset_id = COURSE_DATASET
            batch_date = None
    if env not in ("dev", "test"):
        raise _ReportArgumentError("REPORT_ENV_INVALID")
    if dataset_id is not None and not _id(dataset_id):
        raise _ReportArgumentError("DATASET_SELECTOR_INVALID")
    if database_options()["database"] != ("hospital_test" if env == "test" else "hospital"):
        raise _ReportArgumentError("DATABASE_ENV_MISMATCH")
    config = _load_config()
    snapshot = _load_snapshot(batch_date, dataset_id=dataset_id)
    # A selector mismatch must not overwrite a report belonging to another dataset.
    if dataset_id is not None and isinstance(snapshot, dict) and snapshot.get("dataset_id") != dataset_id:
        _fail("DQ_REPORT_IDENTITY_CONFLICT")
    try:
        _validate_snapshot(snapshot, batch_date=batch_date, dataset_id=dataset_id, rules_version=config["rules_version"])
    except OdsError as error:
        code = error.code if error.code in _ERRORS else "DQ_INTERNAL_ERROR"
        # Only a validated top-level identity can name an error report. Unknown
        # identity cannot safely overwrite an existing env/batch report.
        try:
            _validate_identity(snapshot, batch_date, dataset_id, rules_version=config["rules_version"])
        except Exception:
            _fail(code)
        rules = [_result(t, k, {}, [("RULE_ERROR", 1, "NOT_EVALUATED", True)], execution="ERROR") for t, k in _CATALOG]
        _write_report(_summary(snapshot, rules, env, include_tables=False))
        _fail(code)
    rules = []; error_code = None
    for table, kind in _CATALOG:
        entry = snapshot["tables"][table]; spec = _SPECS[table]
        try:
            if kind == "NULL": result = check_null_ratio(table, (spec[2], spec[3], *spec[5].split()), snapshot=snapshot)
            elif kind == "DUPLICATE": result = check_duplicate(table, ("dataset_id", spec[2]), snapshot=snapshot)
            elif kind == "VALUE_RANGE": result = check_value_range(table, spec[6].split(), snapshot=snapshot)
            elif kind == "VOLUME_WAVE": result = _volume_wave(table, entry, snapshot, config)
            elif kind == "RECONCILE": result = check_reconcile(entry["source"]["table"], table, entry["audit"], snapshot=snapshot)
            elif kind == "MAPPING": result = _check_mapping(table, snapshot)
            else: result = _check_privacy(table, snapshot, env=env)
            if result["code"] == "STALE": error_code = "DQ_STALE"
        except Exception as error:
            code = error.code if isinstance(error, OdsError) and error.code in _ERRORS else "DQ_RULE_ERROR"
            if error_code is None: error_code = code
            result = _result(table, kind, {}, [("RULE_ERROR", 1, "NOT_EVALUATED", True)], execution="ERROR")
        rules.append(result)
    report = _summary(snapshot, rules, env)
    _write_report(report)
    if error_code is not None:
        _fail(error_code)
    return report
