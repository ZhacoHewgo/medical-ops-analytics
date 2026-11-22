from collections import Counter, defaultdict
from decimal import Decimal
from itertools import chain


def _scope(dataset, first, last, department, alias):
    where = f"{alias}.dataset_id=%s"
    params = (dataset,)
    if department is not None:
        where += f" AND {alias}.dept_code=%s"
        params += (department,)
    return where, (*params, first, last)


def _entry(name, label, count, value=None, group=None):
    return {"name": name, "label": label, "count": count,
            "value": None if value is None else format(value, "f"), "group": group}


def admissions(cursor, dataset, first, last, department):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        f"SELECT COUNT(*) FROM dwd_visit_detail v WHERE {scope} "
        "AND v.visit_type='INPATIENT' AND v.admit_date BETWEEN %s AND %s", params,
    )
    return cursor.fetchone()[0]


def visit_costs(cursor, dataset, first, last, department):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT v.visit_type,COUNT(*),COUNT(v.source_total_fee),AVG(v.source_total_fee) "
        f"FROM dwd_visit_detail v WHERE {scope} AND v.visit_type IN ('OUTPATIENT','INPATIENT') "
        "AND CASE WHEN v.visit_type='OUTPATIENT' THEN v.visit_date ELSE v.discharge_date END BETWEEN %s AND %s "
        "GROUP BY v.visit_type", params,
    )
    costs = {kind: (count, average if count == billed else None) for kind, count, billed, average in cursor.fetchall()}
    return [_entry(kind, label, *costs.get(kind, (0, None))) for kind, label in (
        ("OUTPATIENT", "门诊次均费用"), ("INPATIENT", "住院次均费用"),
    )]


def _charges(cursor, dataset, first, last, department):
    scope, params = _scope(dataset, first, last, department, "c")
    cursor.execute(
        "SELECT c.visit_type,c.fee_type_code,COALESCE(f.fee_type_name,c.fee_type_code),"
        "COUNT(*),COUNT(c.amount),SUM(c.amount) FROM dwd_charge_detail c "
        "LEFT JOIN dim_fee_type f ON f.dataset_id=c.dataset_id AND f.fee_type_code=c.fee_type_code "
        f"WHERE {scope} AND c.fee_date BETWEEN %s AND %s "
        "GROUP BY c.visit_type,c.fee_type_code,COALESCE(f.fee_type_name,c.fee_type_code) "
        "ORDER BY c.visit_type,c.fee_type_code",
        params,
    )
    grouped = {"OUTPATIENT": [], "INPATIENT": []}
    fee_types = []
    for visit_type, code, label, count, available, value in cursor.fetchall():
        group = visit_type if visit_type in grouped else "OTHER"
        grouped.setdefault(group, []).append((count, available, value))
        fee_types.append(_entry(code, label, count, value if available == count else None, group))
    labels = {"OUTPATIENT": "门诊收费收入", "INPATIENT": "住院收费收入", "OTHER": "其他收费"}
    by_visit = [
        _entry(group, labels[group], sum(count for count, _, _ in rows),
               sum((value for _, _, value in rows), 0) if all(count == available for count, available, _ in rows) else None)
        for group, rows in grouped.items()
    ]
    return {"visit_types": by_visit, "fee_types": fee_types}


def _inpatient_departments(cursor, dataset, first, last, department, *, available_only=False):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT v.dept_code,COALESCE(d.dept_name,v.dept_code),COUNT(*),"
        "COUNT(v.source_stay_days),AVG(v.source_stay_days) "
        "FROM dwd_visit_detail v LEFT JOIN dim_department d "
        "ON d.dataset_id=v.dataset_id AND d.dept_code=v.dept_code "
        f"WHERE {scope} AND v.visit_type='INPATIENT' AND v.discharge_date BETWEEN %s AND %s "
        "GROUP BY v.dept_code,COALESCE(d.dept_name,v.dept_code) ORDER BY COUNT(*) DESC,v.dept_code",
        params,
    )
    return [
        _entry(code, label, count, average if available_only or available == count else None)
        for code, label, count, available, average in cursor.fetchall()
    ]


def _diseases(cursor, dataset, first, last, department):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT v.disease_code,COALESCE(d.disease_name,v.disease_code,'未记录诊断'),COUNT(*) "
        "FROM dwd_visit_detail v LEFT JOIN dim_disease d "
        "ON d.dataset_id=v.dataset_id AND d.disease_code=v.disease_code "
        f"WHERE {scope} AND v.visit_type='INPATIENT' AND v.discharge_date BETWEEN %s AND %s "
        "GROUP BY v.disease_code,COALESCE(d.disease_name,v.disease_code,'未记录诊断') "
        "ORDER BY COUNT(*) DESC,v.disease_code",
        params,
    )
    return {"diagnoses": [_entry(code or "UNKNOWN", label, count) for code, label, count in cursor.fetchall()]}


def _resources(cursor, dataset, first, last, department):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT CASE WHEN v.source_stay_days IS NULL THEN 4 WHEN v.source_stay_days<4 THEN 0 "
        "WHEN v.source_stay_days<8 THEN 1 WHEN v.source_stay_days<15 THEN 2 ELSE 3 END AS stay_band,"
        "COUNT(*),SUM(v.source_stay_days) FROM dwd_visit_detail v "
        f"WHERE {scope} AND v.visit_type='INPATIENT' AND v.discharge_date BETWEEN %s AND %s "
        "GROUP BY stay_band ORDER BY stay_band",
        params,
    )
    labels = ("少于 4 天", "4 至不足 8 天", "8 至不足 15 天", "15 天及以上", "缺住院日")
    bands = [_entry(str(band), labels[band], count, total) for band, count, total in cursor.fetchall()]
    departments = _inpatient_departments(cursor, dataset, first, last, department, available_only=True)
    scope, params = _scope(dataset, first, last, department, "c")
    cursor.execute(
        "SELECT c.dept_code,COALESCE(d.dept_name,c.dept_code),COUNT(*),COUNT(c.amount),SUM(c.amount) "
        "FROM dwd_charge_detail c LEFT JOIN dim_department d ON d.dataset_id=c.dataset_id AND d.dept_code=c.dept_code "
        f"WHERE {scope} AND c.fee_date BETWEEN %s AND %s "
        "GROUP BY c.dept_code,COALESCE(d.dept_name,c.dept_code) ORDER BY SUM(c.amount) DESC,c.dept_code", params,
    )
    charges = [_entry(code, label, count, amount if count == available else None)
               for code, label, count, available, amount in cursor.fetchall()]
    return {"stay_bands": bands, "departments": departments, "department_charges": charges}


def details(cursor, theme, dataset, first, last, department):
    args = (cursor, dataset, first, last, department)
    if theme == "income":
        return _charges(*args)
    if theme == "drg":
        return _diseases(*args)
    if theme == "efficiency":
        return _resources(*args)
    return {"departments": _inpatient_departments(*args)}


def overview(cursor, dataset, first, last, department, *, operations_report=False):
    scope, params = _scope(dataset, first, last, department, "v")
    cursor.execute(
        "SELECT service_date,visit_type,dept_code,dept_name,COUNT(*) FROM (SELECT v.visit_type,v.dept_code,"
        "COALESCE(d.dept_name,v.dept_code) AS dept_name,"
        "CASE WHEN v.visit_type='OUTPATIENT' THEN v.visit_date ELSE v.discharge_date END AS service_date "
        "FROM dwd_visit_detail v LEFT JOIN dim_department d "
        "ON d.dataset_id=v.dataset_id AND d.dept_code=v.dept_code "
        f"WHERE {scope} AND v.visit_type IN ('OUTPATIENT','INPATIENT')) visits "
        "WHERE service_date BETWEEN %s AND %s GROUP BY service_date,visit_type,dept_code,dept_name",
        params,
    )
    activity = Counter()
    visits = Counter()
    labels = {}
    for day, kind, code, label, count in cursor.fetchall():
        labels[code] = label
        activity[day, kind] += count
        visits[code, kind] += count

    scope, params = _scope(dataset, first, last, department, "c")
    cursor.execute(
        "SELECT c.fee_date,c.dept_code,COALESCE(d.dept_name,c.dept_code),"
        "COUNT(*),COUNT(c.amount),SUM(c.amount) FROM dwd_charge_detail c LEFT JOIN dim_department d "
        "ON d.dataset_id=c.dataset_id AND d.dept_code=c.dept_code "
        f"WHERE {scope} AND c.fee_date BETWEEN %s AND %s "
        "GROUP BY c.fee_date,c.dept_code,COALESCE(d.dept_name,c.dept_code)",
        params,
    )
    charge_days = defaultdict(list)
    charge_depts = defaultdict(list)
    for day, code, label, count, available, amount in cursor.fetchall():
        labels[code] = label
        amount = amount if available == count else None
        charge_days[day].append(amount)
        charge_depts[code].append(amount)

    def total(amounts):
        return None if None in amounts else format(sum(amounts, Decimal("0.00")), "f")

    charge_details = _charges(cursor, dataset, first, last, department)
    result = {
        "activity": [{"date": day.isoformat(), "kind": kind, "count": count}
                     for (day, kind), count in sorted(activity.items())],
        "charges": charge_details["visit_types"],
        "charge_total": total(tuple(chain.from_iterable(charge_days.values()))),
        "daily_charges": [{"date": day.isoformat(), "amount": total(amounts)}
                          for day, amounts in sorted(charge_days.items())],
        "departments": [{"code": code, "label": labels[code], "outpatient": visits[code, "OUTPATIENT"],
                         "inpatient": visits[code, "INPATIENT"],
                         "amount": total(charge_depts[code]) if code in charge_depts or not operations_report else None}
                        for code in sorted(labels)],
    }
    if operations_report:
        scope, params = _scope(dataset, first, last, department, "v")
        cursor.execute(
            f"SELECT COUNT(*),COUNT(v.source_stay_days),AVG(v.source_stay_days) FROM dwd_visit_detail v WHERE {scope} "
            "AND v.visit_type='INPATIENT' AND v.discharge_date BETWEEN %s AND %s", params,
        )
        count, recorded, stay = cursor.fetchone()
        result.update(fee_types=charge_details["fee_types"], admissions=admissions(cursor, dataset, first, last, department),
                      avg_stay_days=format(stay, "f") if count and count == recorded else None)
    return result
