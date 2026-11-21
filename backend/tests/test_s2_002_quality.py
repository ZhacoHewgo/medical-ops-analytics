"""Q7 synthetic-only fixtures. Run with inherited database URLs/key removed."""
import base64
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from etl.config.source_config import OdsError, SOURCES
from etl.jobs import quality_check as q

DATASET = "SIM_Q7"
BATCH = "2025-01-03"
KEY = b"SIM-Q7-NOT-SECRET".ljust(32, b"0")
assert len(KEY) == 32
RUN = "11111111-1111-4111-8111-111111111111"
SPECS = [
    ("dwd_outpatient_visit_detail", "his_outpatient", "visit_id", "visit_date", "visit_id visit_date dept_code visit_type gender age fee_amount patient_name id_card phone"),
    ("dwd_inpatient_visit_detail", "his_inpatient", "visit_id", "admit_date", "visit_id admit_date discharge_date dept_code gender age stay_days total_fee drug_fee consumable_fee disease_code patient_name id_card phone"),
    ("dwd_charge_detail", "his_charge", "charge_id", "fee_date", "charge_id visit_id visit_type dept_code fee_date fee_type_code amount"),
    ("dwd_emr_record_detail", "emr_record", "record_id", "visit_date", "record_id visit_id dept_code visit_date disease_code"),
    ("dwd_lis_report_detail", "lis_report", "report_id", "report_date", "report_id visit_id report_date"),
]
COMMON = "dataset_id source_row_number source_batch_date etl_batch_date etl_load_time patient_key".split()
MAPPINGS = ["dept_code visit_type gender", "dept_code gender disease_code", "dept_code visit_type fee_type_code", "dept_code disease_code", ""]
NUMERICS = {"fee_amount", "stay_days", "total_fee", "drug_fee", "consumable_fee", "amount"}
SUPPRESSED = {"patient_name", "id_card", "phone"}


def digest(kind, columns, rows):
    def scalar(value):
        if isinstance(value, Decimal):
            return format(value, ".2f")
        if isinstance(value, date):
            return value.isoformat()
        return value
    encoded = [[scalar(row[c]) for c in columns if c != "etl_load_time"] for row in rows]
    return hashlib.sha256(json.dumps(encoded, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def refresh(snapshot, table=None):
    for name, entry in snapshot["tables"].items():
        if table is not None and table != name:
            continue
        for kind, field in (("source", "source_sha256"), ("dwd", "result_sha256")):
            view = entry[kind]
            entry["audit"][field] = digest(kind, view["columns"], view["rows"])


def fixture(days=(("2025-01-01", 10), ("2025-01-02", 10))):
    snap = dict(schema_version="node07-snapshot-v1", dataset_id=DATASET, clean_run_id=RUN,
                batch_date=date.fromisoformat(BATCH), rules_version="node07-rules-v1",
                mapping_version="node07-sim-mapping-v1", privacy_key_id="SIM-Q7-key", tables={})
    for index, (table, source, pk, dt, fields) in enumerate(SPECS):
        columns = list(SOURCES[source]["columns"]) + ["dataset_id", "source_row_number", "etl_batch_date"]
        source_rows, dwd_rows = [], []
        for day, count in days:
            for _ in range(count):
                number = len(source_rows) + 1
                raw = dict.fromkeys(SOURCES[source]["columns"], "SIM_UNUSED")
                raw.update({pk: f"SIM_ID_{number:05}", dt: day, "patient_id": f"SIM_PATIENT_{number:05}",
                            "dept_code": "SIM_DEPT_01", "visit_type": "OUTPATIENT", "gender": "M",
                            "disease_code": "SIM_DX_01", "fee_type_code": "SERVICE", "age": "30",
                            "visit_id": f"SIM_ID_{number:05}", "discharge_date": day})
                for col in NUMERICS:
                    raw[col] = "0" if col == "stay_days" else "10.00"
                raw = {col: raw[col] for col in SOURCES[source]["columns"]}
                raw.update(dataset_id=DATASET, source_row_number=number, etl_batch_date=date(2025, 1, 1))
                source_rows.append(raw)
                msg = json.dumps(["node07-patient-v1", DATASET, raw["patient_id"]], ensure_ascii=False, separators=(",", ":")).encode()
                converted = dict(dataset_id=DATASET, source_row_number=number, source_batch_date=date(2025, 1, 1),
                                 etl_batch_date=date.fromisoformat(BATCH), etl_load_time=datetime(2025, 1, 3),
                                 patient_key=hmac.new(KEY, msg, "sha256").hexdigest())
                for col in fields.split():
                    value = raw[col]
                    if col in SUPPRESSED:
                        value = "[REDACTED]"
                    elif col.endswith("_date"):
                        value = date.fromisoformat(value)
                    elif col in NUMERICS:
                        value = Decimal(value)
                    elif col == "age":
                        value = int(value)
                    converted[col] = value
                dwd_rows.append(converted)
        fc = {col: dict(missing=0, invalid=0, filled=0, negative=0) for col in fields.split() if col not in SUPPRESSED}
        pc = {col: dict(nonempty=len(dwd_rows), processed=len(dwd_rows), missing=0)
              for col in ("patient_id", "patient_name", "id_card", "phone") if col in columns}
        stats = dict(quarantined_by_reason=dict(invalid_id_rows=0, conflicting_id_rows=0, invalid_primary_date_rows=0),
                     field_counts=fc, mapping_counts={col: dict(slots=len(dwd_rows), matched=len(dwd_rows), unmapped=0)
                                                     for col in MAPPINGS[index].split()},
                     privacy_counts=pc, filled_rows=0, filled_cells=0)
        audit = dict(dataset_id=DATASET, table_name=table, clean_run_id=RUN, batch_date=date.fromisoformat(BATCH),
                     rules_version="node07-rules-v1", mapping_version="node07-sim-mapping-v1", privacy_key_id="SIM-Q7-key",
                     source_rows=len(source_rows), accepted_rows=len(dwd_rows), duplicate_removed=0, quarantined_rows=0,
                     source_sha256="0" * 64, result_sha256="0" * 64, stats_json=stats, updated_at=datetime(2025, 1, 3))
        snap["tables"][table] = dict(source=dict(table=SOURCES[source]["table"], columns=columns, rows=source_rows),
                                     dwd=dict(table=table, columns=COMMON + fields.split(), rows=dwd_rows), audit=audit)
    refresh(snap)
    return snap


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.tmp.cleanup)
        self.config = dict(rules_version="node07-rules-v1", mapping_version="node07-sim-mapping-v1", approved_complete_intervals=[])
        self.snap = fixture()
        self.patches = [patch.dict(os.environ, {"ODS_DATABASE_URL": "mysql://SIM:SIM@127.0.0.1:13306/hospital_test",
                                               "DWD_PRIVACY_KEY": base64.b64encode(KEY).decode(), "DWD_PRIVACY_KEY_ID": "SIM-Q7-key"}, clear=True),
                        patch.object(q, "_LOG_ROOT", Path(self.tmp.name) / "logs", create=True),
                        patch.object(q, "_load_snapshot", side_effect=lambda *a, **kw: self.snap, create=True),
                        patch.object(q, "_digest", side_effect=digest, create=True),
                        patch.object(q, "_load_config", side_effect=lambda: deepcopy(self.config), create=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def report(self):
        return q.generate_quality_report(BATCH, dataset_id=DATASET, env="test")

    def path(self):
        return Path(self.tmp.name) / "logs/test/quality_report_20250103.json"

    def assert_code(self, code, call, *, detail=None):
        with self.assertRaises(OdsError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code if detail is None else f"{code}: {detail}")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_34_fixed_denominator_and_no_snapshot_mutation(self):
        before = deepcopy(self.snap)
        report = self.report()
        self.assertEqual(report.get("planned_rules"), 34)
        self.assertEqual(report.get("score_denominator_half_points"), 68)
        self.assertEqual(report.get("gate_status"), "PASS")
        self.assertTrue(self.snap == before)
        self.assertEqual(len(report["rules"]), 34)
        self.assertEqual(len({r["rule_id"] for r in report["rules"]}), 34)
        self.assertEqual(report["coverage"], 100)
        self.assertEqual(report["score"], 100)
        self.assertEqual(self.path().stat().st_mode & 0o777, 0o600)

    def test_contract_first_day_and_sparse(self):
        for days, applicability, pairs in [((("2025-01-01", 10),), "NO_BASELINE", 0),
                                            ((("2025-01-01", 10), ("2025-01-03", 10)), "NO_COMPARABLE_PAIR", 2)]:
            with self.subTest(pairs=pairs):
                self.snap = fixture(days)
                r = self.report()
                self.assertEqual(r["gate_status"], "BLOCKED_INSUFFICIENT_EVIDENCE")
                self.assertAlmostEqual(r["score"], 1450 / 17)
                self.assertEqual(r["coverage"], 100)
                self.assertAlmostEqual(r["data_evaluability"], 2900 / 34)
                self.assertEqual(r["score_upper_bound"], 100)
                waves = [rule for rule in r["rules"] if rule["rule_id"].endswith(".VOLUME_WAVE")]
                self.assertTrue(all(x["applicability"] == applicability and x["details"]["candidate_pairs"] == pairs for x in waves))

    def test_contract_zero_baseline(self):
        self.snap = fixture((("2025-01-02", 10), ("2025-01-03", 10)))
        self.config["approved_complete_intervals"] = [dict(dataset_id=DATASET, table=t[0], start_date="2025-01-01", end_date="2025-01-03", basis_id="SIM-COMPLETE") for t in SPECS]
        r = self.report()
        self.assertEqual(r["gate_status"], "PASS")
        self.assertAlmostEqual(r["score"], 1575 / 17)
        self.assertEqual(r["rule_status_counts"]["WARN"], 5)
        for rule in r["rules"]:
            if rule["rule_id"].endswith(".VOLUME_WAVE"):
                self.assertEqual(rule["code"], "ZERO_BASELINE")
                self.assertEqual(rule["details"]["zero_to_positive_pairs"], 1)
                self.assertEqual(rule["details"]["positive_baseline_pairs"], 1)
        self.assertTrue("Infinity" not in self.path().read_text())

    def test_contract_mass_quarantine(self):
        self.snap = fixture((("2025-01-01", 2), ("2025-01-02", 3)))
        for table, source, pk, dt, _ in SPECS:
            e = self.snap["tables"][table]
            original = deepcopy(e["source"]["rows"][0])
            for i in range(80):
                raw = deepcopy(original); raw[pk] = ""; e["source"]["rows"].append(raw)
            for i in range(5):
                for j in range(2):
                    raw = deepcopy(original); raw[pk] = f"SIM_CONFLICT_{i}"; raw["patient_id"] = f"SIM_CONFLICT_{j}"
                    e["source"]["rows"].append(raw)
            for i in range(5):
                raw = deepcopy(original); raw[pk] = f"SIM_BAD_DATE_{i}"; raw[dt] = "invalid"
                e["source"]["rows"].append(raw)
            for i, raw in enumerate(e["source"]["rows"], 1): raw["source_row_number"] = i
            e["audit"].update(source_rows=100, quarantined_rows=95)
            e["audit"]["stats_json"]["quarantined_by_reason"].update(invalid_id_rows=80, conflicting_id_rows=10, invalid_primary_date_rows=5)
        refresh(self.snap)
        r = self.report()
        self.assertEqual(r["gate_status"], "FAIL")
        self.assertAlmostEqual(r["score"], 825 / 17)
        self.assertEqual(r["rule_status_counts"], dict(PASS=14, WARN=5, FAIL=15, NOT_EVALUATED=0))
        self.assertTrue(all(t["source_usable_ratio"] == 0.05 for t in r["tables"].values()))

    def test_wave_30_percent_boundary_and_missing_day(self):
        for n, status in ((7, "PASS"), (13, "PASS"), (14, "WARN"), (6, "WARN")):
            self.snap = fixture((("2025-01-01", 10), ("2025-01-02", n)))
            r = q.check_volume_wave(SPECS[0][0], snapshot=self.snap)
            self.assertEqual(r["status"], status)
        self.snap = fixture((("2025-01-01", 10), ("2025-01-03", 30)))
        self.assertEqual(q.check_volume_wave(SPECS[0][0], snapshot=self.snap)["status"], "NOT_EVALUATED")

    def test_null_duplicate_value_mapping_privacy_failures(self):
        table = SPECS[0][0]
        for kind in ("null", "duplicate", "value", "mapping", "privacy", "forbidden"):
            with self.subTest(kind=kind):
                self.snap = fixture()
                e = self.snap["tables"][table]; row = e["dwd"]["rows"][0]
                if kind == "null": row["visit_id"] = None
                if kind == "duplicate": e["dwd"]["rows"].append(deepcopy(row))
                if kind == "value": row["age"] = 131
                if kind == "mapping": row["dept_code"] = "UNMAPPED"
                if kind == "privacy": row["patient_key"] = "0" * 64
                if kind == "forbidden":
                    e["dwd"]["columns"].append("diagnosis")
                    for item in e["dwd"]["rows"]: item["diagnosis"] = "SIM_PRIVATE_TEXT"
                refresh(self.snap)
                r = self.report()
                name = dict(null="NULL", duplicate="DUPLICATE", value="VALUE_RANGE", mapping="MAPPING", privacy="PRIVACY", forbidden="PRIVACY")[kind]
                rule = next(x for x in r["rules"] if x["rule_id"] == "outpatient." + name)
                self.assertEqual(rule["status"], "FAIL")
                self.assertTrue(rule["hard_block"])
                self.assertEqual(r["gate_status"], "FAIL")

    def test_optional_missing_differs_from_invalid_null(self):
        table = SPECS[0][0]; e = self.snap["tables"][table]
        e["source"]["rows"][0]["age"] = ""; e["dwd"]["rows"][0]["age"] = None
        e["audit"]["stats_json"]["field_counts"]["age"]["missing"] = 1
        refresh(self.snap)
        r = self.report()
        self.assertEqual(next(x for x in r["rules"] if x["rule_id"] == "outpatient.NULL")["status"], "WARN")
        self.assertEqual(next(x for x in r["rules"] if x["rule_id"] == "outpatient.VALUE_RANGE")["status"], "PASS")
        e["source"]["rows"][0]["age"] = "131"
        e["audit"]["stats_json"]["field_counts"]["age"].update(missing=0, invalid=1)
        refresh(self.snap)
        self.assertEqual(self.report()["gate_status"], "FAIL")

    def test_clean_log_cannot_forge_counts(self):
        table = SPECS[0][0]; e = self.snap["tables"][table]
        forged = deepcopy(e["audit"]); forged["accepted_rows"] += 1
        self.assert_code("DQ_SNAPSHOT_MISMATCH", lambda: q.check_reconcile(e["source"]["table"], table, forged, snapshot=self.snap))

    def test_stale_source_and_dwd_reports_written_before_error(self):
        for kind, col in (("source", "patient_id"), ("dwd", "age")):
            self.snap = fixture()
            self.snap["tables"][SPECS[0][0]][kind]["rows"][0][col] = "SIM_CHANGED" if kind == "source" else 40
            self.assert_code("DQ_STALE", self.report)
            r = json.loads(self.path().read_text())
            self.assertEqual(r["gate_status"], "FAIL")
            self.assertTrue(any(x["code"] == "STALE" for x in r["rules"]))

    def test_mixed_run_batch_key_and_bad_stats_safely_rejected(self):
        for field, value in (("clean_run_id", "22222222-2222-4222-8222-222222222222"), ("privacy_key_id", "SIM_OTHER"), ("batch_date", date(2025, 1, 2))):
            self.snap = fixture(); self.snap["tables"][SPECS[0][0]]["audit"][field] = value
            self.assert_code("DQ_SNAPSHOT_MISMATCH", self.report)
        self.snap = fixture(); self.snap["tables"][SPECS[0][0]]["audit"]["stats_json"]["unexpected"] = "SIM_PRIVATE"
        self.assert_code("DQ_CONFIG_INVALID", self.report)

    def test_no_pseudo_sensitive_leak_on_success_or_error(self):
        marker = "SIM_NEVER_OUTPUT_PRIVATE"
        for e in self.snap["tables"].values():
            e["source"]["rows"][0]["patient_id"] = marker
        refresh(self.snap)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            r = self.report()
        self.assertEqual(r["gate_status"], "FAIL")
        self.assertTrue(marker not in output.getvalue() + self.path().read_text())
        with patch.object(q, "check_duplicate", side_effect=ValueError(marker)):
            self.assert_code("DQ_RULE_ERROR", self.report)
        r = json.loads(self.path().read_text())
        self.assertEqual(r["gate_status"], "FAIL")
        self.assertTrue(marker not in self.path().read_text())
        self.assertTrue(any(x["execution_status"] == "ERROR" for x in r["rules"]))

    def test_arguments_whitelist_no_sql_or_environment_confusion(self):
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.check_null_ratio("select secret", ["patient_id"], snapshot=self.snap))
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.check_null_ratio(SPECS[0][0], ["diagnosis"], snapshot=self.snap))
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.check_volume_wave(SPECS[0][0], threshold=0.31, snapshot=self.snap))
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.generate_quality_report(BATCH, env="dev"),
                         detail="DATABASE_ENV_MISMATCH")
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.generate_quality_report("2025-02-30", env="test"),
                         detail="BATCH_DATE_INVALID")

    def test_report_accepts_and_normalizes_date_objects(self):
        report = q.generate_quality_report(date.fromisoformat(BATCH), dataset_id=DATASET, env="test")
        self.assertEqual(report["batch_date"], BATCH)

    def test_report_normalizes_compact_batch_dates(self):
        report = q.generate_quality_report(BATCH.replace("-", ""), dataset_id=DATASET, env="test")
        self.assertEqual(report["batch_date"], BATCH)
        self.assert_code("DQ_ARGUMENTS_INVALID", lambda: q.generate_quality_report("20250230", env="test"),
                         detail="BATCH_DATE_INVALID")

    def test_report_normalizes_unambiguous_dated_values(self):
        for batch in (20250103, datetime.fromisoformat("2025-01-03T00:00:00+08:00"),
                      " 2025-01-03T00:00:00+08:00 ", " 2025-01-03 ",
                      "2025/1/3", "2025.01.03", "2025-1-3", "2025年1月3日"):
            with self.subTest(batch=batch):
                report = q.generate_quality_report(batch, dataset_id=DATASET, env="test")
                self.assertEqual(report["batch_date"], BATCH)

    def test_report_argument_errors_identify_reason_without_input_values(self):
        cases = (
            ("SIM_PRIVATE_BATCH", "test", DATASET, "BATCH_DATE_LABEL_NOT_CALENDAR"),
            ("01/03/2025", "test", DATASET, "BATCH_DATE_INVALID_TEXT"),
            (None, "test", DATASET, "BATCH_DATE_MISSING"),
            ("", "test", DATASET, "BATCH_DATE_MISSING"),
            ("2025-01", "test", DATASET, "BATCH_DATE_MISSING_DAY"),
            ("202501", "test", DATASET, "BATCH_DATE_MISSING_DAY"),
            ({"private": "value"}, "test", DATASET, "BATCH_DATE_INVALID_TYPE"),
            (BATCH, "SIM_PRIVATE_ENV", DATASET, "REPORT_ENV_INVALID"),
            (BATCH, "test", "SIM PRIVATE DATASET", "DATASET_SELECTOR_INVALID"),
            (BATCH, "dev", DATASET, "DATABASE_ENV_MISMATCH"),
        )
        for batch, env, dataset, detail in cases:
            with self.subTest(detail=detail):
                self.assert_code("DQ_ARGUMENTS_INVALID",
                                 lambda: q.generate_quality_report(batch, env=env, dataset_id=dataset),
                                 detail=detail)

    def test_report_distinguishes_database_config_from_invalid_arguments(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assert_code("ODS_DATABASE_URL_MISSING", self.report)
        with patch.dict(os.environ, {"ODS_DATABASE_URL": "SIM_BAD_URL"}):
            self.assert_code("ODS_DATABASE_URL_INVALID", self.report)
        self.assertFalse(self.path().exists())

    def test_report_identity_conflict_and_write_failure_preserve_old(self):
        self.report(); old = self.path().read_bytes()
        self.assert_code("DQ_REPORT_IDENTITY_CONFLICT", lambda: q.generate_quality_report(BATCH, dataset_id="SIM_OTHER", env="test"))
        self.assertTrue(self.path().read_bytes() == old)
        with patch.object(q.os, "replace", side_effect=OSError("SIM_PRIVATE_OS_ERROR")):
            self.assert_code("DQ_REPORT_WRITE_FAILED", self.report)
        self.assertTrue(self.path().read_bytes() == old)
        self.assertEqual(len(list(self.path().parent.glob("*.tmp"))), 0)

    def test_existing_other_dataset_report_never_overwritten(self):
        self.report()
        previous = json.loads(self.path().read_text()); previous["dataset_id"] = "SIM_OTHER"
        self.path().write_text(json.dumps(previous))
        old = self.path().read_bytes()
        self.assert_code("DQ_REPORT_IDENTITY_CONFLICT", self.report)
        self.assertTrue(self.path().read_bytes() == old)

    def test_configuration_unknown_intervals_rejected(self):
        actual = Path(self.tmp.name) / "SIM-config.json"
        self.patches[-1].stop()
        with patch.object(q, "_CONFIG", actual):
            for item in (dict(dataset_id=DATASET, table=SPECS[0][0], start_date="2025-02-30", end_date="2025-03-01", basis_id="SIM"),
                         dict(dataset_id=DATASET, table="select secret", start_date="2025-01-01", end_date="2025-01-03", basis_id="SIM")):
                config = dict(self.config); config["approved_complete_intervals"] = [item]
                actual.write_text(json.dumps(config))
                self.assert_code("DQ_CONFIG_INVALID", q._load_config)

    def test_report_symlink_hardlink_and_corrupt_identity_rejected(self):
        self.report(); path = self.path(); old = path.read_bytes()
        sibling = path.with_name("SIM-other.json"); sibling.write_bytes(old)
        path.unlink(); path.symlink_to(sibling)
        self.assert_code("DQ_REPORT_PATH_INVALID", self.report)
        path.unlink(); os.link(sibling, path)
        self.assert_code("DQ_REPORT_PATH_INVALID", self.report)
        path.unlink(); path.write_text("{}")
        self.assert_code("DQ_REPORT_IDENTITY_CONFLICT", self.report)

    def test_rule_error_does_not_become_evidence_gap(self):
        with patch.object(q, "_volume_wave", side_effect=RuntimeError("SIM_PRIVATE")):
            self.assert_code("DQ_RULE_ERROR", self.report)
        r = json.loads(self.path().read_text())
        self.assertEqual(r["gate_status"], "FAIL")
        self.assertLess(r["coverage"], 95)

    def test_missing_key_and_invalid_key_write_error_report_without_leak(self):
        for key, code in ((None, "DWD_PRIVACY_KEY_MISSING"), ("SIM_INVALID_BASE64", "DWD_PRIVACY_KEY_INVALID"),
                          (base64.b64encode(b"SIM_SHORT").decode(), "DWD_PRIVACY_KEY_INVALID")):
            with patch.dict(os.environ):
                if key is None: os.environ.pop("DWD_PRIVACY_KEY", None)
                else: os.environ["DWD_PRIVACY_KEY"] = key
                self.assert_code(code, self.report)
                result = json.loads(self.path().read_text())
                self.assertEqual(result["gate_status"], "FAIL")
                self.assertEqual(sum(x["execution_status"] == "ERROR" for x in result["rules"]), 5)
                if key is not None: self.assertTrue(key not in self.path().read_text())

    def test_zero_privacy_slots_still_structurally_pass(self):
        for e in self.snap["tables"].values():
            for row in e["source"]["rows"]:
                for col in ("patient_id", "patient_name", "id_card", "phone"):
                    if col in row: row[col] = "  "
            for row in e["dwd"]["rows"]:
                row["patient_key"] = None
                for col in SUPPRESSED:
                    if col in row: row[col] = None
            for counts in e["audit"]["stats_json"]["privacy_counts"].values():
                counts.update(nonempty=0, processed=0, missing=20)
        refresh(self.snap)
        result = self.report()
        self.assertTrue(all(x["status"] == "PASS" for x in result["rules"] if x["rule_id"].endswith(".PRIVACY")))
        self.assertTrue(all(x["status"] == "WARN" for x in result["rules"] if x["rule_id"].endswith(".NULL")))

    def test_reconcile_fabricated_audit_and_accepted_zero_fail(self):
        e = self.snap["tables"][SPECS[0][0]]
        e["audit"]["stats_json"]["field_counts"]["age"]["invalid"] = 1
        result = self.report()
        self.assertEqual(result["gate_status"], "FAIL")
        self.assertEqual(next(x for x in result["rules"] if x["rule_id"] == "outpatient.RECONCILE")["status"], "FAIL")
        self.snap = fixture()
        e = self.snap["tables"][SPECS[0][0]]
        for row in e["source"]["rows"]: row["visit_id"] = ""
        e["dwd"]["rows"] = []
        e["audit"].update(accepted_rows=0, quarantined_rows=20)
        stats = e["audit"]["stats_json"]
        stats["quarantined_by_reason"]["invalid_id_rows"] = 20
        for counts in stats["mapping_counts"].values(): counts.update(slots=0, matched=0, unmapped=0)
        for counts in stats["privacy_counts"].values(): counts.update(nonempty=0, processed=0, missing=0)
        refresh(self.snap)
        result = self.report()
        self.assertEqual(result["gate_status"], "FAIL")
        self.assertEqual(next(x for x in result["rules"] if x["rule_id"] == "outpatient.RECONCILE")["status"], "PASS")

    def test_complete_zero_to_zero_and_partial_unknown_counts(self):
        table = SPECS[0][0]
        self.snap = fixture((("2025-01-03", 10), ("2025-01-05", 10)))
        self.config["approved_complete_intervals"] = [dict(dataset_id=DATASET, table=table, start_date="2025-01-01", end_date="2025-01-03", basis_id="SIM-COMPLETE")]
        rule = q.check_volume_wave(table, snapshot=self.snap)
        d = rule["details"]
        self.assertEqual(d["candidate_pairs"], 4)
        self.assertEqual(d["evaluated_pairs"], 2)
        self.assertEqual(d["unevaluated_pairs"], 2)
        self.assertEqual(d["zero_to_zero_pairs"], 1)
        self.assertEqual(d["zero_to_positive_pairs"], 1)

    def test_all_rule_and_report_keys_exact(self):
        report = self.report()
        rule_keys = set("rule_id table execution_status status code severity hard_block applicability numerator denominator details".split())
        self.assertTrue(all(set(x) == rule_keys for x in report["rules"]))
        self.assertTrue(all(set(x) == {"rule_id", "code", "severity"} for x in report["alerts"]))
        expected = set("schema_version env dataset_id clean_run_id batch_date rules_version mapping_version privacy_key_id generated_at planned_rules executed_rules evaluated_rules coverage data_evaluability score_numerator_half_points score_denominator_half_points score score_upper_bound rule_status_counts gate_status rules alerts tables".split())
        self.assertTrue(set(report) == expected)
        self.assertTrue(all(set(x["details"]["reason_counts"]) == set(q._CODES) for x in report["rules"]))

    def test_integer_score_and_hard_fail_override(self):
        rules = [q._result(t, k, {}) for t, k in q._CATALOG]
        for count, expected in ((6, "PASS"), (7, "FAIL")):
            altered = deepcopy(rules)
            for i in range(count): altered[i]["status"] = "WARN"
            result = q._summary(self.snap, altered, "test")
            self.assertEqual(result["gate_status"], expected)
            self.assertEqual(result["score_numerator_half_points"], 68 - count)
        rules[0].update(status="FAIL", hard_block=True)
        result = q._summary(self.snap, rules, "test")
        self.assertGreater(result["score"], 97)
        self.assertEqual(result["gate_status"], "FAIL")
        rules[0].update(status="NOT_EVALUATED", hard_block=False, execution_status="NOT_RUN")
        self.assertEqual(q._summary(self.snap, rules, "test")["gate_status"], "FAIL")

    def test_new_run_rewrite_and_environment_directory_symlink(self):
        self.report()
        self.snap["clean_run_id"] = "22222222-2222-4222-8222-222222222222"
        for e in self.snap["tables"].values(): e["audit"]["clean_run_id"] = self.snap["clean_run_id"]
        self.assertTrue(self.report()["clean_run_id"] == self.snap["clean_run_id"])
        self.path().unlink()
        self.path().parent.rmdir()
        target = Path(self.tmp.name) / "SIM-target"; target.mkdir()
        self.path().parent.symlink_to(target)
        self.assert_code("DQ_REPORT_PATH_INVALID", self.report)
        self.assertEqual(list(target.iterdir()), [])

    def test_snapshot_validation_error_persists_safe_error_report(self):
        self.snap["tables"][SPECS[0][0]]["audit"]["clean_run_id"] = "22222222-2222-4222-8222-222222222222"
        self.assert_code("DQ_SNAPSHOT_MISMATCH", self.report)
        self.assertTrue(self.path().is_file())
        report = json.loads(self.path().read_text())
        self.assertEqual(report["gate_status"], "FAIL")
        self.assertEqual(report["executed_rules"], 0)
        self.assertTrue(all(x["execution_status"] == "ERROR" for x in report["rules"]))

    def test_all_five_tables_rule_failure_fixtures(self):
        for table, source, pk, dt, fields in SPECS:
            for kind in ("NULL", "DUPLICATE", "VALUE_RANGE", "RECONCILE", "PRIVACY", "MAPPING"):
                if kind == "MAPPING" and table == SPECS[4][0]: continue
                with self.subTest(table=table, kind=kind):
                    self.snap = fixture()
                    e = self.snap["tables"][table]
                    row = e["dwd"]["rows"][0]
                    if kind == "NULL": row[pk] = None
                    elif kind == "DUPLICATE": e["dwd"]["rows"].append(deepcopy(row))
                    elif kind == "VALUE_RANGE": row[dt] = "2025-02-30"
                    elif kind == "RECONCILE": e["audit"]["accepted_rows"] += 1
                    elif kind == "PRIVACY": row["patient_key"] = "0" * 64
                    elif kind == "MAPPING": row["dept_code"] = "UNMAPPED"
                    refresh(self.snap)
                    report = self.report()
                    self.assertEqual(report["gate_status"], "FAIL")
                    rule = next(x for x in report["rules"] if x["table"] == table and x["rule_id"].endswith("." + kind))
                    self.assertEqual(rule["status"], "FAIL")
                    self.assertTrue(rule["hard_block"])

    def test_complete_duplicate_is_warn_and_source_losses_are_exclusive(self):
        table, source, pk, dt, _ = SPECS[0]
        e = self.snap["tables"][table]
        duplicate = deepcopy(e["source"]["rows"][0]); duplicate["source_row_number"] = 21
        e["source"]["rows"].append(duplicate)
        e["audit"].update(source_rows=21, duplicate_removed=1)
        refresh(self.snap)
        report = self.report()
        self.assertEqual(report["gate_status"], "PASS")
        self.assertEqual(next(x for x in report["rules"] if x["rule_id"] == "outpatient.DUPLICATE")["status"], "WARN")
        self.assertEqual(next(x for x in report["rules"] if x["rule_id"] == "outpatient.RECONCILE")["status"], "PASS")

    def test_decimal_refunds_and_age_date_edge_cases(self):
        table = SPECS[0][0]
        for raw, accepted, state in (("-0.01", Decimal("-0.01"), "WARN"),
                                      ("9999999999999999.99", Decimal("9999999999999999.99"), "PASS"),
                                      ("10000000000000000", None, "FAIL"), ("1.001", None, "FAIL"), ("NaN", None, "FAIL")):
            with self.subTest(state=state):
                self.snap = fixture(); e = self.snap["tables"][table]
                e["source"]["rows"][0]["fee_amount"] = raw
                e["dwd"]["rows"][0]["fee_amount"] = accepted
                e["audit"]["stats_json"]["field_counts"]["fee_amount"].update(invalid=int(state == "FAIL"), negative=int(state == "WARN"))
                refresh(self.snap)
                report = self.report()
                self.assertEqual(next(x for x in report["rules"] if x["rule_id"] == "outpatient.VALUE_RANGE")["status"], state)
        for age, state in ((0, "PASS"), (130, "PASS"), (131, "FAIL")):
            self.snap = fixture(); e = self.snap["tables"][table]
            e["source"]["rows"][0]["age"] = str(age)
            e["dwd"]["rows"][0]["age"] = age if state == "PASS" else None
            e["audit"]["stats_json"]["field_counts"]["age"]["invalid"] = int(state == "FAIL")
            refresh(self.snap)
            self.assertEqual(next(x for x in self.report()["rules"] if x["rule_id"] == "outpatient.VALUE_RANGE")["status"], state)

    def test_overlong_numeric_source_is_invalid_not_execution_error(self):
        e = self.snap["tables"][SPECS[0][0]]
        e["source"]["rows"][0]["age"] = "9" * 5000
        e["dwd"]["rows"][0]["age"] = None
        e["audit"]["stats_json"]["field_counts"]["age"]["invalid"] = 1
        refresh(self.snap)
        result = self.report()
        self.assertEqual(result["gate_status"], "FAIL")
        self.assertEqual(result["executed_rules"], 34)

    def test_same_snapshot_loaded_once_config_read_once(self):
        with patch.object(q, "_load_snapshot", return_value=self.snap) as loader, patch.object(q, "_load_config", return_value=self.config) as config:
            self.report()
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(config.call_count, 1)

    def test_semantically_forged_field_stats_are_reconciled(self):
        e = self.snap["tables"][SPECS[0][0]]
        e["audit"]["stats_json"]["field_counts"]["dept_code"]["invalid"] = 1
        report = self.report()
        self.assertEqual(report["gate_status"], "FAIL")
        self.assertEqual(next(x for x in report["rules"] if x["rule_id"] == "outpatient.RECONCILE")["status"], "FAIL")

    def test_unpatched_shared_digest_report_and_safe_driver_error(self):
        self.patches[3].stop()
        self.assertEqual(self.report()["gate_status"], "PASS")
        with patch.object(q, "_digest", side_effect=ValueError("SIM_NEVER_DRIVER_DETAIL")):
            self.assert_code("DQ_INTERNAL_ERROR", self.report)
        self.assertTrue("SIM_NEVER_DRIVER_DETAIL" not in self.path().read_text())

    def test_released_complete_intervals_load_in_c7_and_q7(self):
        from etl.jobs import clean_dwd
        self.patches[-1].stop()
        config = json.loads(q._CONFIG.read_text())
        version = "node07-rules-v1-SIM-Q7-release"
        config["rules_version"] = version
        config["approved_complete_intervals"] = [dict(dataset_id=DATASET, table=t[0], start_date="2025-01-01", end_date="2025-01-03", basis_id="SIM-APPROVED-Q7") for t in SPECS]
        path = Path(self.tmp.name) / "SIM-approved-config.json"
        path.write_text(json.dumps(config))
        self.snap = fixture((("2025-01-02", 10), ("2025-01-03", 10)))
        self.snap["rules_version"] = version
        for e in self.snap["tables"].values(): e["audit"]["rules_version"] = version
        with patch.object(q, "_CONFIG", path), patch.object(clean_dwd, "CONFIG_PATH", path):
            self.assertTrue(clean_dwd._config()["rules_version"] == version)
            report = self.report()
            self.assertEqual(report["gate_status"], "PASS")
            self.assertEqual(report["executed_rules"], 34)
            self.assertAlmostEqual(report["score"], 1575 / 17)
            self.assertTrue(report["rules_version"] == version)
            self.assertTrue(all(x["code"] == "ZERO_BASELINE" for x in report["rules"] if x["rule_id"].endswith(".VOLUME_WAVE")))
            self.path().unlink()
            self.snap["rules_version"] = "node07-rules-v1"
            for e in self.snap["tables"].values(): e["audit"]["rules_version"] = "node07-rules-v1"
            self.assert_code("DQ_SNAPSHOT_MISMATCH", self.report)
            self.assertFalse(self.path().exists())

    def test_release_version_cannot_change_fixed_schema_or_catalog(self):
        self.patches[-1].stop()
        original = json.loads(q._CONFIG.read_text())
        path = Path(self.tmp.name) / "SIM-invalid-config.json"
        mutations = ("bad_version", "changed_table", "extra_rule", "changed_mapping", "extra_top_key", "unversioned_interval")
        with patch.object(q, "_CONFIG", path):
            for name in mutations:
                with self.subTest(case=name):
                    config = deepcopy(original); config["rules_version"] = "node07-rules-v1-SIM-Q7-release"
                    if name == "bad_version": config["rules_version"] = "../SIM-invalid"
                    elif name == "changed_table": config["tables"][SPECS[0][0]]["columns"].append("SIM_EXTRA")
                    elif name == "extra_rule": config["rules"].append(dict(rule_id="lis.MAPPING", table=SPECS[4][0], type="MAPPING"))
                    elif name == "changed_mapping": config["mappings"]["gender"]["SIM_UNKNOWN"] = "M"
                    elif name == "extra_top_key": config["query"] = "SIM_FORBIDDEN"
                    else:
                        config["rules_version"] = "node07-rules-v1"
                        config["approved_complete_intervals"] = [dict(dataset_id=DATASET, table=SPECS[0][0], start_date="2025-01-01", end_date="2025-01-03", basis_id="SIM")]
                    path.write_text(json.dumps(config))
                    self.assert_code("DQ_CONFIG_INVALID", q._load_config)

    def test_error_and_not_run_details_preserve_all_frozen_keys(self):
        def shape(value):
            return {k: shape(v) for k, v in value.items()} if isinstance(value, dict) else None
        def leaves(value):
            if isinstance(value, dict):
                for v in value.values(): yield from leaves(v)
            else: yield value
        reference = {rule["rule_id"]: shape(rule["details"]) for rule in self.report()["rules"]}
        functions = ("check_null_ratio", "check_duplicate", "check_value_range", "_volume_wave", "check_reconcile", "_check_mapping", "_check_privacy")
        for function in functions:
            with self.subTest(function=function), patch.object(q, function, side_effect=OdsError("DQ_RULE_ERROR")):
                self.assert_code("DQ_RULE_ERROR", self.report)
                report = json.loads(self.path().read_text())
                self.assertEqual(report["gate_status"], "FAIL")
                errors = [rule for rule in report["rules"] if rule["execution_status"] == "ERROR"]
                self.assertTrue(bool(errors))
                for rule in errors:
                    self.assertTrue(shape(rule["details"]) == reference[rule["rule_id"]])
                    observational = {k: v for k, v in rule["details"].items() if k != "reason_counts"}
                    self.assertTrue(all(v is None for v in leaves(observational)))
                    self.assertEqual(rule["status"], "NOT_EVALUATED")
        for table, kind in q._CATALOG:
            rule = q._result(table, kind, {}, [("NOT_RUN", 1, "NOT_EVALUATED", True)], execution="NOT_RUN")
            self.assertTrue(shape(rule["details"]) == reference[rule["rule_id"]])
        self.snap["tables"][SPECS[0][0]]["audit"]["clean_run_id"] = "22222222-2222-4222-8222-222222222222"
        self.assert_code("DQ_SNAPSHOT_MISMATCH", self.report)
        report = json.loads(self.path().read_text())
        self.assertEqual(report["score_numerator_half_points"], 0)
        self.assertEqual(report["executed_rules"], 0)
        self.assertTrue(all(shape(rule["details"]) == reference[rule["rule_id"]] for rule in report["rules"]))

    def test_actual_config_and_shared_digest_pure_api(self):
        self.patches[-1].stop()
        cfg = q._load_config()
        self.assertEqual(cfg["rules_version"], "node07-rules-v1")
        self.assertEqual(cfg["approved_complete_intervals"], [])
        # Shared digest tested only when C7 has published the contracted function.
        from etl.jobs import clean_dwd
        if not hasattr(clean_dwd, "quality_digest"):
            self.skipTest("C7 shared pure API not published yet; no database executed")
        for e in self.snap["tables"].values():
            for kind in ("source", "dwd"):
                v = e[kind]
                self.assertTrue(clean_dwd.quality_digest(kind, v["columns"], v["rows"]) == digest(kind, v["columns"], v["rows"]))


if __name__ == "__main__":
    unittest.main()
