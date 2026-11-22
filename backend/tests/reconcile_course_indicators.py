import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import io
import json
from zipfile import ZipFile

from etl.config.source_config import COURSE_DATASET, connect_database


DEPARTMENTS = ("EK", "FC", "GK", "HN", "HX", "ICU", "JZ", "PW", "SJ", "XH")
FEE_CODES = {"XY": "DRUG", "ZC": "DRUG", "JC": "EXAM", "JY": "LAB", "ZL": "TREATMENT",
             "SS": "SURGERY", "CL": "CONSUMABLE", "CW": "BED", "HL": "NURSING"}
TYPES = {"普通": "NORMAL", "专家": "SPECIALIST", "急诊": "EMERGENCY"}
EMPTY = {
    "MOPS-OUT-03": "NO_COMPARABLE_YEAR", "MOPS-INC-03": "NO_COMPARABLE_YEAR",
    "MOPS-INP-03": "BED_OCCUPANCY_RULE_UNCONFIRMED", "MOPS-EFF-01": "BED_OCCUPANCY_RULE_UNCONFIRMED",
    "MOPS-INP-04": "BED_CAPACITY_MISSING", "MOPS-INP-05": "BED_CAPACITY_MISSING",
    "MOPS-DRUG-04": "DRUG_QUANTITY_DDD_MISSING", "MOPS-CONS-04": "CONSUMPTION_COST_MISSING",
    "MOPS-DIS-03": "OFFICIAL_DRG_GROUPING_MISSING", "MOPS-DIS-04": "DRG_GROUPING_AND_WEIGHTS_MISSING",
    "MOPS-DIS-05": "DRG_REFERENCE_COST_MISSING", "MOPS-DIS-06": "DRG_REFERENCE_STAY_MISSING",
    "MOPS-EFF-02": "PHYSICIAN_ROSTER_MISSING", "MOPS-EFF-03": "PHYSICIAN_ROSTER_MISSING",
    "MOPS-EFF-04": "EQUIPMENT_RUNTIME_MISSING", "MOPS-EFF-05": "FIXED_ASSET_LEDGER_MISSING",
    "MOPS-SRV-01": "APPOINTMENT_RECORDS_MISSING", "MOPS-SRV-02": "CANCELLATION_RECORDS_MISSING",
    "MOPS-SRV-03": "SATISFACTION_SURVEY_MISSING", "MOPS-SRV-04": "LAB_TIMELINESS_INPUT_MISSING",
}
BATCH = date(2025, 6, 30)
START = date(2025, 4, 1)


def _read(bundle, name, columns):
    with bundle.open(f"data/{name}.csv") as stream:
        for row in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig")):
            yield tuple(row[column] for column in columns)


def reference(path):
    samples = defaultdict(Counter)

    def add(day, dept, values):
        if START <= day <= BATCH:
            assert dept in DEPARTMENTS
            samples["day", day, dept].update(values)
            samples["month", day.replace(day=1), dept].update(values)

    with ZipFile(path) as bundle:
        diagnoses = set()
        links = set(_read(bundle, "emr_record", ("visit_id", "patient_id", "dept_code", "visit_date")))
        outpatient_keys = set()
        outpatient = ("visit_id", "patient_id", "visit_date", "dept_code", "fee_amount", "visit_type")
        for visit, patient, dt, dept, bill, kind in _read(bundle, "his_outpatient", outpatient):
            outpatient_keys.add((visit, patient, dept, dt))
            add(date.fromisoformat(dt), dept, {"out": 1, "bill": Decimal(bill), ("type", TYPES[kind]): 1,
                                             ("emr_count", "OUTPATIENT"): 1,
                                             ("emr_link", "OUTPATIENT"): int((visit, patient, dept, dt) in links)})
        inpatient = ("visit_id", "patient_id", "dept_code", "admit_date", "discharge_date", "stay_days",
                     "disease_code", "total_fee", "drug_fee", "consumable_fee")
        for visit, patient, dept, admit, discharge, days, dx, bill, drug, goods in _read(bundle, "his_inpatient", inpatient):
            assert (visit, patient, dept, admit) not in outpatient_keys, "Ambiguous course visit linkage"
            diagnosed = date.fromisoformat(discharge)
            diagnoses.add(dx)
            add(diagnosed, dept, {"ip": 1, "stay": Decimal(days), "ip_bill": Decimal(bill), "ip_drug": Decimal(drug),
                                  "ip_goods": Decimal(goods), ("diagnosed", dx): 1, ("dx_bill", dx): Decimal(bill)})
            add(date.fromisoformat(admit), dept, {("emr_count", "INPATIENT"): 1,
                                                 ("emr_link", "INPATIENT"): int((visit, patient, dept, admit) in links)})
            assert (diagnosed - date.fromisoformat(admit)).days == int(days)
            for day_before_discharge in range(1, int(days) + 1):
                add(diagnosed - timedelta(days=day_before_discharge), dept, {"person_day": 1})
        for dt, dept, visit_type, category, amount in _read(
            bundle, "his_charge", ("fee_date", "dept_code", "visit_type", "fee_type_code", "amount")
        ):
            charge = Decimal(amount)
            values = {"charge": charge, ("fee", FEE_CODES[category]): charge}
            if visit_type == "住院":
                values["ip_charge"] = charge
            add(date.fromisoformat(dt), dept, values)
        for dt, dept, amount in _read(bundle, "finance_excel", ("stat_date", "dept_code", "revenue")):
            add(date.fromisoformat(dt), dept, {"ledger": Decimal(amount), "ledger_rows": 1})

    def ratio(top, bottom, modifier=1):
        if not bottom:
            return top, bottom, None, "ZERO_BASE"
        value = (Decimal(top) * modifier / bottom).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return top, bottom, value, None

    def scalar(value):
        return value, None, value, None

    baseline = Counter()
    for month in (4, 5, 6):
        for dept in DEPARTMENTS:
            baseline.update(samples["month", date(2025, month, 1), dept])

    def cmi(data):
        cases = data["ip"]
        if not cases:
            return ratio(0, 0)
        expected_cost = sum(Decimal(data["diagnosed", key]) * baseline["dx_bill", key] / baseline["diagnosed", key]
                            for key in diagnoses)
        unrounded = expected_cost * baseline["ip"] / baseline["ip_bill"]
        _, denominator, value, reason = ratio(unrounded, cases)
        return unrounded.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), denominator, value, reason

    full = {}
    for grain, periods in (("day", [START + timedelta(days=i) for i in range((BATCH - START).days + 1)]),
                           ("month", [date(2025, month, 1) for month in (4, 5, 6)])):
        for period in periods:
            previous = (period - timedelta(days=1)).replace(day=1)
            for dept in (None, *DEPARTMENTS):
                candidates = DEPARTMENTS if dept is None else (dept,)
                data, prior = Counter(), Counter()
                for item in candidates:
                    data.update(samples[grain, period, item])
                    prior.update(samples[grain, previous, item])
                scope = "ALL" if dept is None else "DEPT:" + dept
                amounts = {
                    **{key: (None, None, None, reason) for key, reason in EMPTY.items()
                       if grain == "month" or key not in {"MOPS-OUT-03", "MOPS-INC-03"}},
                    "MOPS-OUT-01": scalar(data["out"]), "MOPS-OUT-04": ratio(data["bill"], data["out"]),
                    "MOPS-INP-01": scalar(data["ip"]), "MOPS-INP-02": ratio(data["stay"], data["ip"]),
                    "MOPS-INP-06": ratio(data["ip_bill"], data["ip"]), "MOPS-INC-01": scalar(data["charge"]),
                    "MOPS-INC-05": scalar(data["ledger"] - data["charge"]),
                    "MOPS-DRUG-01": scalar(data["fee", "DRUG"]), "MOPS-DRUG-02": ratio(data["fee", "DRUG"], data["charge"], 100),
                    "MOPS-DRUG-03": ratio(data["ip_drug"], data["ip_bill"], 100),
                    "MOPS-CONS-01": scalar(data["fee", "CONSUMABLE"]),
                    "MOPS-CONS-02": ratio(data["fee", "CONSUMABLE"], data["charge"], 100),
                    "MOPS-CONS-03": ratio(data["ip_goods"], data["ip_bill"], 100),
                    "MOPS-INP-SIM-01": scalar(data["person_day"]),
                    "MOPS-DIS-SIM-01": cmi(data),
                    "MOPS-DIS-SIM-02": ratio(sum(data["diagnosed", key] for key in diagnoses), data["ip"], 100),
                    "MOPS-EFF-SIM-01": ratio(data["ip_charge"], data["person_day"]),
                }
                if grain == "month":
                    for code, key in (("MOPS-OUT-02", "out"), ("MOPS-INC-02", "charge")):
                        amounts[code] = (ratio(data[key] - prior[key], prior[key], 100) if previous >= START else
                                         (None, None, None, "NO_COMPARABLE_PRIOR"))
                for code, value in amounts.items():
                    full[code, scope, grain, period] = value
                cohort = ("OUTPATIENT", "INPATIENT")
                full["MOPS-SRV-05", scope, grain, period] = ratio(sum(data["emr_link", kind] for kind in cohort),
                                                                 sum(data["emr_count", kind] for kind in cohort), 100)
                for kind in cohort:
                    full["MOPS-SRV-05", scope + "|TYPE:" + kind, grain, period] = ratio(data["emr_link", kind],
                                                                                      data["emr_count", kind], 100)
                for category in {*TYPES.values(), "UNKNOWN"}:
                    full["MOPS-OUT-05", scope + "|TYPE:" + category, grain, period] = ratio(data["type", category], data["out"], 100)
                for category in {*FEE_CODES.values(), "UNKNOWN"}:
                    full["MOPS-INC-04", scope + "|FEE:" + category, grain, period] = ratio(data["fee", category], data["charge"], 100)
                for diagnosis in diagnoses | {"UNKNOWN"}:
                    dimension = scope + "|DX:" + diagnosis
                    cases = data["diagnosed", diagnosis]
                    full["MOPS-DIS-01", dimension, grain, period] = scalar(cases)
                    full["MOPS-DIS-02", dimension, grain, period] = (
                        ratio(data["dx_bill", diagnosis], cases) if diagnosis != "UNKNOWN" else (None, None, None, "DISEASE_CODE_MISSING")
                    )
    return full


def reconcile(path, test_only=False, batch=BATCH):
    expected = reference(path)
    with connect_database(test_only=test_only) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT build_run_id FROM dwd_build_audit WHERE dataset_id=%s AND batch_date=%s", (COURSE_DATASET, batch))
        (build_run,) = cursor.fetchone()
        cursor.execute(
            "SELECT indicator_code,dimension_key,granularity,stat_date,numerator,denominator,metric_value,"
            "null_reason,calculation_status,source_build_run_id "
            "FROM ads_indicator_result WHERE dataset_id=%s AND source_batch=%s AND stat_date BETWEEN %s AND %s",
            (COURSE_DATASET, batch, START, BATCH),
        )
        published = {tuple(row[:4]): tuple(row[4:]) for row in cursor.fetchall()}
    mismatches = []
    for key, target in expected.items():
        actual = published.get(key)
        if actual != (*target, "NULL" if target[2] is None else "OK", build_run):
            mismatches.append(key)
    extras = published.keys() - expected.keys()
    if mismatches or extras:
        first = [tuple(str(part) for part in key) for key in sorted(set(mismatches) | extras)[:3]]
        raise SystemExit(f"Reconciliation failed: {len(mismatches)} differences, {len(extras)} extra rows, examples {first}")
    numeric = {key[0] for key, result in published.items() if result[2] is not None}
    return {
        "sql_rows": dict(Counter(key[2] for key in published)), "checked": len(published), "differences": 0,
        "numeric_codes": sorted(numeric), "all_null_codes": sorted({key[0] for key in published} - numeric),
        "null_reasons": dict(sorted(Counter(result[3] for result in published.values() if result[3]).items())),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--test-only", action="store_true")
    parser.add_argument("--batch", type=date.fromisoformat, default=BATCH)
    args = parser.parse_args()
    print(json.dumps(reconcile(args.archive, args.test_only, args.batch), ensure_ascii=False))
