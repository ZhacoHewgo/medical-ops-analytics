import re
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal

from etl.jobs import course_indicators as course


def test_course_ratios_keep_billing_and_charge_inputs_separate():
    facts = {"out_count": 2, "out_bill": Decimal("23.01"), "charges": Decimal("60.05"),
             "ledger": Decimal("54.02"), "ip_count": 2, "ip_bill": Decimal("40.00"),
             "ip_drug": Decimal("10.00"), ("linked", None): 1, ("visits", None): 2, "service_days": 3}
    assert course.measure("MOPS-OUT-04", facts, {}, None)[2] == Decimal("11.505000")
    assert course.measure("MOPS-INC-05", facts, {}, None)[2] == Decimal("-6.03")
    assert course.measure("MOPS-DRUG-03", facts, {}, None)[2] == Decimal("25")
    assert course.measure("MOPS-SRV-05", facts, {}, None)[2] == Decimal("50")
    assert course.measure("MOPS-INP-03", facts, {}, None)[-1] == "BED_OCCUPANCY_RULE_UNCONFIRMED"


def test_missing_inputs_and_incomplete_comparison_stay_distinct():
    assert course.measure("MOPS-INC-02", {"charges": 8}, {}, None)[-1] == "NO_COMPARABLE_PRIOR"
    assert course.measure("MOPS-INC-02", {"charges": 8}, {"charges": 10}, None)[2] == Decimal("-20")
    assert course.measure("MOPS-INC-05", {"charges": 8}, {}, None)[-1] == "LEDGER_MISSING_OR_INCOMPLETE"
    assert course.measure("MOPS-EFF-SIM-01", {}, {}, None)[-1] == "ZERO_BASE"
    assert course.measure("MOPS-DIS-02", {("disease_amount", "A09"): None}, {}, "A09")[-1] == "INCOMPLETE_INPUT"
    assert course.measure("MOPS-DIS-02", {("disease_amount", "UNKNOWN"): 20}, {}, "UNKNOWN")[-1] == "DISEASE_CODE_MISSING"
    assert course.measure("MOPS-SRV-05", {("ambiguous_emr", None): 2}, {}, None)[-1] == "EMR_LINK_AMBIGUOUS"


def test_derived_patient_days_cut_off_at_the_end_of_each_period():
    april = date(2025, 4, 30)
    may = date(2025, 5, 2)
    facts = {}
    course.add_patient_days(facts, [(april, may, "HN", 2)], april, may)
    assert facts == {(april, "HN"): {"service_days": 2}, (date(2025, 5, 1), "HN"): {"service_days": 2}}


def test_resource_bed_metrics_use_full_dataset_capacity_and_selected_patient_days():
    with closing(sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)) as db:
        db.create_function("REGEXP_SUBSTR", 2, lambda value, pattern: next(iter(re.findall(pattern, value or "")), None))
        db.executescript("""
            CREATE TABLE dwd_visit_detail (dataset_id, source_row_number, source_batch_date,
                                          dept_code, visit_type, admit_date DATE, discharge_date DATE);
            CREATE TABLE ods_his_inpatient_visit (dataset_id, source_row_number, etl_batch_date, bed_no);
            INSERT INTO dwd_visit_detail VALUES
                ('course', 1, '2025-06-30', 'HN', 'INPATIENT', '2025-04-01', '2025-04-09'),
                ('course', 2, '2025-06-30', 'HN', 'INPATIENT', '2025-04-30', '2025-05-03'),
                ('course', 3, '2025-06-30', 'EK', 'INPATIENT', '2025-05-02', '2025-05-04'),
                ('course', 4, '2025-06-30', 'HX', 'INPATIENT', '2025-06-02', '2025-06-05');
            INSERT INTO ods_his_inpatient_visit VALUES
                ('course', 1, '2025-06-30', 'HN50'), ('course', 2, '2025-06-30', 'HN07'),
                ('course', 3, '2025-06-30', 'EK20'), ('course', 4, '2025-06-30', 'HX');
        """)

        class Cursor:
            def execute(self, statement, params):
                self.result = db.execute(statement.replace("%s", "?").replace("UNSIGNED", "INTEGER"), params)

            def fetchall(self):
                return self.result.fetchall()

        cursor = Cursor()
        first, last = date(2025, 5, 1), date(2025, 5, 2)
        summary, departments, trend = course.resource_bed_metrics(cursor, "course", first, last, "HN", include_trend=True)
        assert summary == {
            "available_bed_days": {"value": "100", "null_reason": None},
            "occupied_bed_days": {"value": "2", "null_reason": None},
            "bed_usage_rate": {"value": "2.000000", "null_reason": None},
        }
        assert departments == {"HN": {
            "bed_count": 50, "available_bed_days": 100, "occupied_bed_days": 2, "bed_usage_rate": "2.000000",
        }}
        assert trend == [{"date": f"2025-05-0{day}", "occupied": 1, "available": 50} for day in (1, 2)]
        summary, departments, _ = course.resource_bed_metrics(cursor, "course", first, last, None)
        assert summary["available_bed_days"] == {
            "value": None, "null_reason": "BED_CAPACITY_MISSING",
        }
        assert departments["HX"]["bed_count"] is None
        db.execute("UPDATE ods_his_inpatient_visit SET bed_no='HX30' WHERE source_row_number=4")
        summary, departments, _ = course.resource_bed_metrics(cursor, "course", first, last, None)
        assert summary == {
            "available_bed_days": {"value": "200", "null_reason": None},
            "occupied_bed_days": {"value": "3", "null_reason": None},
            "bed_usage_rate": {"value": "1.500000", "null_reason": None},
        }
        assert departments["HX"] == {
            "bed_count": 30, "available_bed_days": 60, "occupied_bed_days": 0, "bed_usage_rate": "0.000000",
        }


def test_disease_estimates_use_fixed_reference_and_keep_unknowns_distinct():
    reference = {"ip_count": 4, "ip_bill": 800, ("disease_count", "A09"): 2, ("disease_amount", "A09"): 200,
                 ("disease_count", "E11"): 2, ("disease_amount", "E11"): 600}
    weights = course.estimate_weights(reference)
    assert course.estimate_cmi(reference, weights)[2] == Decimal("1")
    selected = {"ip_count": 4, ("disease_count", "A09"): 3, ("disease_count", "E11"): 1}
    assert course.estimate_cmi(selected, weights)[2] == Decimal("0.75")
    selected = {"ip_count": 4, ("disease_count", "A09"): 3, ("disease_count", "UNKNOWN"): 1}
    assert course.estimate_cmi(selected, weights)[-1] == "DISEASE_CODE_MISSING"
    assert course.estimate_drg_rate(selected)[:3] == (3, 4, Decimal("75"))
    assert course.estimate_drg_rate({})[-1] == "ZERO_BASE"
    assert course.estimate_weights({**reference, ("disease_amount", "E11"): None}) == {}


def test_consumption_index_weights_each_diagnosis_by_selected_cases():
    full = {("disease_count", "A09"): 4, ("disease_count", "E11"): 2,
            ("disease_amount", "A09"): 400, ("disease_amount", "E11"): 400}
    selected = {("disease_count", "A09"): 2, ("disease_count", "E11"): 1,
                ("disease_amount", "A09"): 300, ("disease_amount", "E11"): 100}
    assert course.consumption_index(selected, full, "disease_amount") == Decimal("1.166667")
    assert course.consumption_index(selected, {**full, ("disease_amount", "E11"): 0}, "disease_amount") is None
