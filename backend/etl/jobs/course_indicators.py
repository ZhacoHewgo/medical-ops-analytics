from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from etl.config.course_mapping import FEE_NAMES
from etl.jobs.multidimensional_report import LEDGER, daily_metrics


VISIT_TYPES = {"普通": "NORMAL", "专家": "SPECIALIST", "急诊": "EMERGENCY"}
CMI_ESTIMATE = "MOPS-DIS-SIM-01"
DRG_RATE_ESTIMATE = "MOPS-DIS-SIM-02"
METRICS = {
    "MOPS-OUT-04": ("元/人次", "course-outpatient-bill-v1"),
    "MOPS-OUT-05": ("%", "course-outpatient-type-v1"),
    "MOPS-INC-01": ("元", "course-charge-v1"),
    "MOPS-INC-02": ("%", "course-charge-month-v1"),
    "MOPS-INC-04": ("%", "course-charge-category-v1"),
    "MOPS-INC-05": ("元", "course-ledger-vs-charge-v1"),
    "MOPS-DRUG-01": ("元", "course-charge-v1"),
    "MOPS-DRUG-03": ("%", "course-inpatient-bill-v1"),
    "MOPS-CONS-01": ("元", "course-charge-v1"),
    "MOPS-CONS-03": ("%", "course-inpatient-bill-v1"),
    "MOPS-DIS-01": ("人次", "course-diagnosis-count-v1"),
    "MOPS-DIS-02": ("元/人次", "course-diagnosis-bill-v1"),
    CMI_ESTIMATE: ("指数", "course-disease-cost-cmi-v1"),
    DRG_RATE_ESTIMATE: ("%", "course-coded-discharge-v1"),
    "MOPS-SRV-05": ("%", "course-visit-emr-link-v1"),
    "MOPS-INP-SIM-01": ("人日", "course-admit-days-v1"),
    "MOPS-EFF-SIM-01": ("元/人日", "course-charge-per-patientday-v1"),
}
MISSING = {
    "MOPS-OUT-03": ("%", "NO_COMPARABLE_YEAR"),
    "MOPS-INP-03": ("床日", "BED_OCCUPANCY_RULE_UNCONFIRMED"),
    "MOPS-INP-05": ("次", "BED_CAPACITY_MISSING"),
    "MOPS-INC-03": ("%", "NO_COMPARABLE_YEAR"),
    "MOPS-DRUG-04": ("DDD/百人天", "DRUG_QUANTITY_DDD_MISSING"),
    "MOPS-CONS-04": ("元/百元", "CONSUMPTION_COST_MISSING"),
    "MOPS-DIS-05": ("指数", "DRG_REFERENCE_COST_MISSING"),
    "MOPS-DIS-06": ("指数", "DRG_REFERENCE_STAY_MISSING"),
    "MOPS-EFF-01": ("元/床日", "BED_OCCUPANCY_RULE_UNCONFIRMED"),
    "MOPS-EFF-02": ("人次/人", "PHYSICIAN_ROSTER_MISSING"),
    "MOPS-EFF-03": ("人次/人", "PHYSICIAN_ROSTER_MISSING"),
    "MOPS-EFF-04": ("%", "EQUIPMENT_RUNTIME_MISSING"),
    "MOPS-EFF-05": ("元/百元", "FIXED_ASSET_LEDGER_MISSING"),
    "MOPS-SRV-01": ("%", "APPOINTMENT_RECORDS_MISSING"),
    "MOPS-SRV-02": ("%", "CANCELLATION_RECORDS_MISSING"),
    "MOPS-SRV-03": ("%", "SATISFACTION_SURVEY_MISSING"),
    "MOPS-SRV-04": ("%", "LAB_TIMELINESS_INPUT_MISSING"),
}
MONTH_ONLY = {"MOPS-INC-02", "MOPS-OUT-03", "MOPS-INC-03"}
CODE_INFO = {**METRICS, **{code: (unit, "course-missing-v1") for code, (unit, _) in MISSING.items()}}


def _add(target, values):
    for key, value in values.items():
        current = target.get(key, 0)
        target[key] = current + value if current is not None and value is not None else None


def _amount(rows, samples, total):
    return total if rows == samples else None


def _outpatient(cursor, dataset, first, last, daily):
    cursor.execute(
        "SELECT v.visit_date,v.dept_code,o.visit_type,COUNT(*),COUNT(v.source_total_fee),SUM(v.source_total_fee) "
        "FROM dwd_visit_detail v LEFT JOIN ods_his_outpatient_visit o ON o.dataset_id=v.dataset_id "
        "AND o.source_row_number=v.source_row_number AND o.etl_batch_date=v.source_batch_date "
        "WHERE v.dataset_id=%s AND v.visit_type='OUTPATIENT' AND v.visit_date BETWEEN %s AND %s "
        "GROUP BY v.visit_date,v.dept_code,o.visit_type",
        (dataset, first, last),
    )
    for day, dept, label, count, priced, amount in cursor.fetchall():
        category = VISIT_TYPES.get((label or "").strip(), "UNKNOWN")
        _add(daily.setdefault((day, dept), {}), {"out_count": count, "out_bill": _amount(count, priced, amount),
                                                ("visit_type", category): count})


def registration_mix(cursor, dataset, first, last, department):
    scope = " AND v.dept_code=%s" if department is not None else ""
    cursor.execute(
        "SELECT o.visit_type,COUNT(*) FROM dwd_visit_detail v "
        "LEFT JOIN ods_his_outpatient_visit o ON o.dataset_id=v.dataset_id "
        "AND o.source_row_number=v.source_row_number AND o.etl_batch_date=v.source_batch_date "
        f"WHERE v.dataset_id=%s AND v.visit_type='OUTPATIENT' AND v.visit_date BETWEEN %s AND %s{scope} "
        "GROUP BY o.visit_type", (dataset, first, last, *(() if department is None else (department,))),
    )
    counts = {}
    for kind, count in cursor.fetchall():
        label = (kind or "").strip()
        label = label if label in VISIT_TYPES else "未归类"
        counts[label] = counts.get(label, 0) + count
    return [{"label": label, "count": counts[label]} for label in (*VISIT_TYPES, "未归类") if label in counts]


def _inpatient(cursor, dataset, first, last, daily, department=None):
    dept_clause = " AND v.dept_code=%s" if department is not None else ""
    params = (department,) if department is not None else ()
    cursor.execute(
        "SELECT v.discharge_date,v.dept_code,v.disease_code,COUNT(*),COUNT(v.source_total_fee),SUM(v.source_total_fee),"
        "COUNT(v.source_drug_fee),SUM(v.source_drug_fee),COUNT(v.source_consumable_fee),SUM(v.source_consumable_fee),"
        "COUNT(v.source_stay_days),SUM(v.source_stay_days) "
        "FROM dwd_visit_detail v "
        f"WHERE v.dataset_id=%s AND v.visit_type='INPATIENT' AND v.discharge_date BETWEEN %s AND %s{dept_clause} "
        "GROUP BY v.discharge_date,v.dept_code,v.disease_code",
        (dataset, first, last, *params),
    )
    for day, dept, diagnosis, count, billed, bill, drug_n, drug, material_n, material, stay_n, stay in cursor.fetchall():
        diagnosis = diagnosis or "UNKNOWN"
        _add(daily.setdefault((day, dept), {}), {
            "ip_count": count, "ip_bill": _amount(count, billed, bill), "ip_drug": _amount(count, drug_n, drug),
            "ip_material": _amount(count, material_n, material),
            ("disease_count", diagnosis): count, ("disease_amount", diagnosis): _amount(count, billed, bill),
            ("disease_stay", diagnosis): _amount(count, stay_n, stay),
        })


def _linked_visits(cursor, dataset, first, last, daily):
    cursor.execute(
        "WITH emr_keys AS (SELECT DISTINCT visit_id,patient_key,dept_code,visit_date "
        "FROM dwd_emr_record_detail WHERE dataset_id=%s AND patient_key IS NOT NULL), "
        "visits AS (SELECT visit_id,patient_key,dept_code,visit_date,visit_type,"
        "COUNT(*) OVER (PARTITION BY visit_id,patient_key,dept_code,visit_date) AS candidates "
        "FROM dwd_visit_detail WHERE dataset_id=%s AND visit_date BETWEEN %s AND %s) "
        "SELECT v.visit_date,v.dept_code,v.visit_type,COUNT(*),"
        "COUNT(CASE WHEN v.candidates=1 THEN e.visit_id END),COUNT(CASE WHEN v.candidates>1 THEN e.visit_id END) "
        "FROM visits v LEFT JOIN emr_keys e ON e.visit_id=v.visit_id "
        "AND e.patient_key=v.patient_key AND e.dept_code=v.dept_code AND e.visit_date=v.visit_date "
        "GROUP BY v.visit_date,v.dept_code,v.visit_type",
        (dataset, dataset, first, last),
    )
    for day, dept, kind, count, linked, ambiguous in cursor.fetchall():
        _add(daily.setdefault((day, dept), {}), {
            ("visits", None): count, ("linked", None): linked, ("visits", kind): count, ("linked", kind): linked,
            ("ambiguous_emr", None): ambiguous, ("ambiguous_emr", kind): ambiguous,
        })


def _charges(cursor, dataset, first, last, daily):
    cursor.execute(
        "SELECT fee_date,dept_code,visit_type,fee_type_code,COUNT(*),COUNT(amount),SUM(amount) "
        "FROM dwd_charge_detail WHERE dataset_id=%s AND fee_date BETWEEN %s AND %s "
        "GROUP BY fee_date,dept_code,visit_type,fee_type_code",
        (dataset, first, last),
    )
    for day, dept, visit_type, fee_type, count, priced, amount in cursor.fetchall():
        result = _amount(count, priced, amount)
        fee_type = fee_type if fee_type in FEE_NAMES else "UNKNOWN"
        values = {"charges": result, ("fee_amount", fee_type): result}
        if visit_type == "INPATIENT":
            values["inpatient_charges"] = result
        _add(daily.setdefault((day, dept), {}), values)


def add_patient_days(daily, intervals, first, after_last):
    for admit, discharge, dept, count in intervals:
        day = max(admit, first)
        while day < min(discharge, after_last):
            _add(daily.setdefault((day, dept), {}), {"service_days": count})
            day += timedelta(days=1)


def _patient_days(cursor, dataset, first, last, daily, department=None):
    dept_clause = " AND dept_code=%s" if department is not None else ""
    cursor.execute(
        "SELECT admit_date,discharge_date,dept_code,COUNT(*) FROM dwd_visit_detail "
        f"WHERE dataset_id=%s AND visit_type='INPATIENT' AND admit_date<=%s AND discharge_date>%s{dept_clause} "
        "GROUP BY admit_date,discharge_date,dept_code",
        (dataset, last, first) + ((department,) if department is not None else ()),
    )
    add_patient_days(daily, cursor.fetchall(), first, last + timedelta(days=1))


def resource_bed_metrics(cursor, dataset, first, last, department, *, include_trend=False):
    dept_clause = " AND v.dept_code=%s" if department is not None else ""
    cursor.execute(
        "SELECT v.dept_code,MAX(CAST(REGEXP_SUBSTR(TRIM(o.bed_no), '[0-9]+$') AS UNSIGNED)) "
        "FROM dwd_visit_detail v LEFT JOIN ods_his_inpatient_visit o ON o.dataset_id=v.dataset_id "
        "AND o.source_row_number=v.source_row_number AND o.etl_batch_date=v.source_batch_date "
        f"WHERE v.dataset_id=%s AND v.visit_type='INPATIENT'{dept_clause} GROUP BY v.dept_code",
        (dataset, department) if department is not None else (dataset,),
    )
    capacities = dict(cursor.fetchall())
    daily = {}
    _patient_days(cursor, dataset, first, last, daily, department)
    occupied = dict.fromkeys(capacities, 0)
    for (_, code), facts in daily.items():
        occupied[code] += facts["service_days"]
    days = (last - first).days + 1

    def measure(capacity, used):
        available = capacity * days if capacity else None
        _, _, usage, _ = _ratio(used, available, 100)
        return {
            "available_bed_days": available,
            "occupied_bed_days": used,
            "bed_usage_rate": format(usage, "f") if usage is not None else None,
        }

    by_department = {
        code: {"bed_count": capacity or None, **measure(capacity, occupied[code])}
        for code, capacity in sorted(capacities.items())
    }
    total_capacity = sum(capacities.values()) if capacities and all(capacities.values()) else None
    summary = {
        key: {"value": str(value) if value is not None else None,
              "null_reason": "BED_CAPACITY_MISSING" if value is None else None}
        for key, value in measure(total_capacity, sum(occupied.values())).items()
    }
    trend = []
    if include_trend and total_capacity:
        occupancy = {}
        for (day, _), facts in daily.items():
            occupancy[day] = occupancy.get(day, 0) + facts["service_days"]
        trend = [{"date": day.isoformat(), "occupied": occupancy.get(day, 0), "available": total_capacity}
                 for day in (first + timedelta(days=offset) for offset in range(days))]
    return summary, by_department, trend


def _ratio(numerator, denominator, multiplier=1):
    if numerator is None or denominator is None:
        return None, None, None, "INCOMPLETE_INPUT"
    if denominator == 0:
        return numerator, denominator, None, "ZERO_BASE"
    value = (Decimal(numerator) * multiplier / denominator).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return numerator, denominator, value, None


def _scalar(value):
    return value, None, value, "INCOMPLETE_INPUT" if value is None else None


def _disease_counts(facts):
    return {key[1]: count for key, count in facts.items() if isinstance(key, tuple) and key[0] == "disease_count"}


def estimate_weights(reference):
    count, total = reference.get("ip_count", 0), reference.get("ip_bill")
    counts = _disease_counts(reference)
    if not count or total is None or total <= 0 or counts.get("UNKNOWN") or sum(counts.values()) != count:
        return {}
    hospital_average = Decimal(total) / count
    weights = {}
    for code, cases in counts.items():
        amount = reference.get(("disease_amount", code))
        if amount is None or amount < 0:
            return {}
        weights[code] = Decimal(amount) / cases / hospital_average
    return weights


def estimate_cmi(facts, weights):
    count = facts.get("ip_count", 0)
    if not count:
        return _ratio(0, 0)
    counts = _disease_counts(facts)
    if counts.get("UNKNOWN") or sum(counts.values()) != count:
        return None, None, None, "DISEASE_CODE_MISSING"
    if not counts.keys() <= weights.keys():
        return None, None, None, "CMI_REFERENCE_INCOMPLETE"
    weighted = sum(weights[code] * cases for code, cases in counts.items())
    _, denominator, value, reason = _ratio(weighted, count)
    return weighted.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), denominator, value, reason


def estimate_drg_rate(facts):
    count = facts.get("ip_count", 0)
    return _ratio(count - facts.get(("disease_count", "UNKNOWN"), 0), count, 100)


def _inpatient_facts(cursor, dataset, first, last, department=None):
    daily, total = {}, {}
    _inpatient(cursor, dataset, first, last, daily, department)
    for facts in daily.values():
        _add(total, facts)
    return total


def consumption_index(selected, reference, key):
    counts = _disease_counts(selected)
    reference_counts = _disease_counts(reference)
    if not counts or "UNKNOWN" in counts:
        return None
    weighted = Decimal(0)
    for code, count in counts.items():
        actual = selected.get((key, code))
        baseline = reference.get((key, code))
        if actual is None or baseline is None or baseline <= 0 or code not in reference_counts:
            return None
        weighted += Decimal(actual) * reference_counts[code] / baseline
    return _ratio(weighted, sum(counts.values()))[2]


def disease_estimates(cursor, dataset, first, last, department, low, high):
    reference = _inpatient_facts(cursor, dataset, low, high)
    selected = reference if (first, last, department) == (low, high, None) else _inpatient_facts(
        cursor, dataset, first, last, department,
    )
    return [
        {"name": name, "label": label, "count": selected.get("ip_count", 0),
         "value": format(value, "f") if value is not None else None, "group": "estimate"}
        for name, label, value in (
            ("cmi_estimate", "CMI", estimate_cmi(selected, estimate_weights(reference))[2]),
            ("drg_rate_estimate", "DRG 入组率", estimate_drg_rate(selected)[2]),
            ("cost_index", "费用消耗指数", consumption_index(selected, reference, "disease_amount")),
            ("time_index", "时间消耗指数", consumption_index(selected, reference, "disease_stay")),
        )
    ]


def measure(code, facts, prior, category):
    if code in MISSING:
        return None, None, None, MISSING[code][1]
    if code == "MOPS-INC-02":
        if prior is None or "charges" not in prior:
            return None, None, None, "NO_COMPARABLE_PRIOR"
        current, previous = facts.get("charges", 0), prior["charges"]
        difference = current - previous if current is not None and previous is not None else None
        return _ratio(difference, previous, 100)
    if code == "MOPS-INC-05":
        ledger, charged = facts.get("ledger"), facts.get("charges", 0)
        if ledger is None:
            return None, None, None, "LEDGER_MISSING_OR_INCOMPLETE"
        return _scalar(ledger - charged if charged is not None else None)
    if code == "MOPS-DIS-02" and category == "UNKNOWN":
        return None, None, None, "DISEASE_CODE_MISSING"
    if code == "MOPS-SRV-05" and facts.get(("ambiguous_emr", category), 0):
        return None, None, None, "EMR_LINK_AMBIGUOUS"
    amounts = {"MOPS-INC-01": "charges", "MOPS-DRUG-01": ("fee_amount", "DRUG"),
               "MOPS-CONS-01": ("fee_amount", "CONSUMABLE"), "MOPS-DIS-01": ("disease_count", category),
               "MOPS-INP-SIM-01": "service_days"}
    if code in amounts:
        return _scalar(facts.get(amounts[code], 0))
    numerator, denominator, multiplier = {
        "MOPS-OUT-04": ("out_bill", "out_count", 1),
        "MOPS-OUT-05": (("visit_type", category), "out_count", 100),
        "MOPS-INC-04": (("fee_amount", category), "charges", 100),
        "MOPS-DRUG-03": ("ip_drug", "ip_bill", 100),
        "MOPS-CONS-03": ("ip_material", "ip_bill", 100),
        "MOPS-DIS-02": (("disease_amount", category), ("disease_count", category), 1),
        "MOPS-SRV-05": (("linked", category), ("visits", category), 100),
        "MOPS-EFF-SIM-01": ("inpatient_charges", "service_days", 1),
    }[code]
    return _ratio(facts.get(numerator, 0), facts.get(denominator, 0), multiplier)


def rows(cursor, dataset, batch, run, grain, periods, scopes, metrics, low, high):
    first, last = periods[0][0], periods[-1][1]
    read_first = max(low, (first - timedelta(days=1)).replace(day=1)) if grain == "month" and first > low else max(low, first)
    read_last = min(last, high)
    weights = estimate_weights(_inpatient_facts(cursor, dataset, low, high)) if CMI_ESTIMATE in metrics else {}
    daily = {}
    if any(code in METRICS for code in metrics):
        _outpatient(cursor, dataset, read_first, read_last, daily)
        _inpatient(cursor, dataset, read_first, read_last, daily)
        _charges(cursor, dataset, read_first, read_last, daily)
        _patient_days(cursor, dataset, read_first, read_last, daily)
        if "MOPS-SRV-05" in metrics:
            _linked_visits(cursor, dataset, read_first, read_last, daily)
        if "MOPS-INC-05" in metrics:
            ledger = daily_metrics(cursor, dataset, None, read_first, read_last, None, (LEDGER,), row_limit=None)
            for (day, dept), values in ledger.items():
                daily.setdefault((day, dept), {})["ledger"] = values.get(LEDGER)
    diagnoses = ()
    if {"MOPS-DIS-01", "MOPS-DIS-02"}.intersection(metrics):
        cursor.execute("SELECT disease_code FROM dim_disease WHERE dataset_id=%s ORDER BY disease_code", (dataset,))
        diagnoses = tuple(code for (code,) in cursor.fetchall() if code != "UNKNOWN") + ("UNKNOWN",)
    dimensions = {
        "MOPS-OUT-05": ("TYPE", (*VISIT_TYPES.values(), "UNKNOWN")),
        "MOPS-INC-04": ("FEE", (*FEE_NAMES, "UNKNOWN")),
        "MOPS-DIS-01": ("DX", diagnoses),
        "MOPS-DIS-02": ("DX", diagnoses),
        "MOPS-SRV-05": ("TYPE", (None, "OUTPATIENT", "INPATIENT")),
    }
    facts = {}
    for (day, dept), values in daily.items():
        period = day.replace(day=1) if grain == "month" else day
        _add(facts.setdefault((period, None), {}), values)
        _add(facts.setdefault((period, dept), {}), values)
    for period, end in periods:
        previous = (period - timedelta(days=1)).replace(day=1)
        complete = low <= period and end <= high
        for scope in scopes:
            base = "ALL" if scope is None else f"DEPT:{scope}"
            current = facts.get((period, scope), {})
            prior = facts.get((previous, scope)) if previous >= low else None
            for code in metrics:
                if grain != "month" and code in MONTH_ONLY:
                    continue
                unit, version = CODE_INFO[code]
                kind, categories = dimensions.get(code, (None, (None,)))
                for category in categories:
                    dimension = base if category is None else f"{base}|{kind}:{category}"
                    if not complete:
                        result = None, None, None, "INCOMPLETE_PERIOD"
                    elif code == CMI_ESTIMATE:
                        result = estimate_cmi(current, weights)
                    elif code == DRG_RATE_ESTIMATE:
                        result = estimate_drg_rate(current)
                    else:
                        result = measure(code, current, prior, category)
                    numerator, denominator, value, reason = result
                    yield (dataset, code, dimension, grain, period, batch, run, version, unit, numerator,
                           denominator, value, "NULL" if reason else "OK", reason)
